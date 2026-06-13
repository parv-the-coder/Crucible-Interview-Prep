"""Sandbox behaviour tests.

Every case here is an attack or a failure mode the platform must survive. They
run against the subprocess backend because it needs no daemon; the same suite
is parametrised over the Docker backend in tests/integration when one is
available.
"""

from __future__ import annotations

import subprocess
import time
import uuid

import pytest

from crucible.evaluation.sandbox import ExecOutcome, ExecutionRequest
from crucible.evaluation.sandbox.base import truncate_output


def run(sandbox, source: str, stdin: str = "", timeout: int = 3, language: str = "python"):
    return sandbox.execute(
        ExecutionRequest(language=language, source=source, stdin=stdin, timeout_seconds=timeout)
    )


# ------------------------------------------------------------- happy path ---


def test_runs_a_correct_program(sandbox) -> None:
    result = run(sandbox, "print('hello world')")
    assert result.outcome is ExecOutcome.OK
    assert result.stdout.strip() == "hello world"
    assert result.exit_code == 0


def test_pipes_stdin_to_the_program(sandbox) -> None:
    source = "import sys\nprint(sum(int(x) for x in sys.stdin.read().split()))"
    result = run(sandbox, source, stdin="1 2 3 4 5")
    assert result.outcome is ExecOutcome.OK
    assert result.stdout.strip() == "15"


def test_program_that_ignores_stdin_still_succeeds(sandbox) -> None:
    """Closing an unread stdin pipe raises EPIPE -- it must not be an error."""
    result = run(sandbox, "print('done')", stdin="x" * 100_000)
    assert result.outcome is ExecOutcome.OK


# --------------------------------------------------------- failure modes ---


def test_syntax_error_is_reported_as_compile_error(sandbox) -> None:
    """Not 'every test failed'. The distinction is the whole point of feedback."""
    result = run(sandbox, "def broken(:\n    pass")
    assert result.outcome is ExecOutcome.COMPILE_ERROR
    assert "SyntaxError" in result.stderr


def test_runtime_error_is_distinguished_from_a_wrong_answer(sandbox) -> None:
    result = run(sandbox, "raise ValueError('boom')")
    assert result.outcome is ExecOutcome.RUNTIME_ERROR
    assert "ValueError" in result.stderr
    assert result.exit_code != 0


def test_infinite_loop_is_killed_at_the_timeout(sandbox) -> None:
    result = run(sandbox, "while True:\n    pass", timeout=2)
    assert result.outcome is ExecOutcome.TIMEOUT
    # Must not overshoot: an unbounded run blocks a worker slot indefinitely.
    assert result.duration_ms < 4000


def test_memory_bomb_is_capped(sandbox) -> None:
    result = run(sandbox, "x = bytearray(900 * 1024 * 1024)\nprint(len(x))")
    assert result.outcome is ExecOutcome.MEMORY_EXCEEDED
    assert "900" not in result.stdout


def test_sleeping_program_hits_the_wall_clock_not_the_cpu_limit(sandbox) -> None:
    """A CPU rlimit alone would let `sleep(999)` hold a worker forever."""
    result = run(sandbox, "import time\ntime.sleep(30)", timeout=2)
    assert result.outcome is ExecOutcome.TIMEOUT
    assert result.duration_ms < 4000


# -------------------------------------------------------- resource abuse ---


def test_output_flood_is_bounded_and_does_not_grow_the_worker(sandbox) -> None:
    """The container memory cap cannot help: the bytes have left the sandbox.

    Without a bounded reader, communicate() buffers the whole stream in the
    worker process and one submission can OOM the worker itself.
    """
    from crucible.core.config import settings

    result = run(sandbox, "for _ in range(10**8):\n    print('A' * 100)", timeout=2)
    assert result.outcome in (ExecOutcome.TIMEOUT, ExecOutcome.OUTPUT_TRUNCATED)
    # Cap plus the truncation notice, nothing like the gigabytes produced.
    assert len(result.stdout) < settings.sandbox_max_output_bytes + 200


def test_stderr_flood_is_bounded_too(sandbox) -> None:
    from crucible.core.config import settings

    source = "import sys\nwhile True:\n    sys.stderr.write('E' * 1000)"
    result = run(sandbox, source, timeout=2)
    assert len(result.stderr) < settings.sandbox_max_output_bytes + 200


def test_forked_children_are_contained_and_leave_no_orphans(sandbox) -> None:
    """A submission must not leave a process running after it returns.

    Two independent controls can stop this, and either is a pass:
      * RLIMIT_NPROC refuses the fork outright (EAGAIN -> runtime error), or
      * the timeout kills the whole process *group*, not just the direct child.

    The mechanism is an implementation detail. What must hold is that the run
    is bounded and nothing survives it -- killing only the direct child would
    orphan the grandchild and leak a process per submission.
    """
    # Generated at run time: a literal marker would also match the pytest
    # command line that contains this file's source, and self-match.
    marker = f"orphan-probe-{uuid.uuid4().hex}"
    source = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', \"import time; time.sleep(60)  # {marker}\"])\n"
        "time.sleep(60)\n"
    )
    result = run(sandbox, source, timeout=2)

    assert result.outcome in (ExecOutcome.TIMEOUT, ExecOutcome.RUNTIME_ERROR)
    assert result.duration_ms < 5000

    # Nothing from this run may still be alive.
    time.sleep(0.3)
    surviving = subprocess.run(
        ["pgrep", "-fa", marker], capture_output=True, text=True, check=False
    ).stdout.strip()
    assert not surviving, f"orphaned process survived the sandbox run:\n{surviving}"


def test_fork_bomb_does_not_take_down_the_host(sandbox) -> None:
    """The classic. v1 had no pid limit at all, so this was a live DoS."""
    source = "import os\nwhile True:\n    os.fork()\n"
    result = run(sandbox, source, timeout=3)
    assert result.outcome in (
        ExecOutcome.RUNTIME_ERROR,
        ExecOutcome.TIMEOUT,
        ExecOutcome.MEMORY_EXCEEDED,
    )
    assert result.duration_ms < 6000


# --------------------------------------------------------------- contract ---


def test_unsupported_language_is_rejected(sandbox) -> None:
    with pytest.raises(ValueError, match="Unsupported language"):
        run(sandbox, "puts 'hi'", language="brainfuck")


def test_backend_reports_its_own_isolation_honestly(sandbox) -> None:
    """A backend that cannot isolate must not claim it can."""
    caps = sandbox.capabilities
    assert caps.name == "subprocess"
    assert caps.production_safe is False
    assert caps.isolates_filesystem is False
    assert caps.notes, "an unsafe backend must document why"


@pytest.mark.parametrize(
    ("text", "limit", "expect_truncated"),
    [("abc", 10, False), ("a" * 100, 10, True), ("", 10, False)],
)
def test_truncate_output(text: str, limit: int, expect_truncated: bool) -> None:
    out, truncated = truncate_output(text, limit)
    assert truncated is expect_truncated
    if not truncated:
        assert out == text


def test_truncate_output_never_splits_a_multibyte_character() -> None:
    """Slicing bytes mid-character yields mojibake in the candidate's output."""
    out, truncated = truncate_output("é" * 100, 11)
    assert truncated
    out.encode("utf-8")  # must round-trip
    assert "�" not in out.split("...")[0]
