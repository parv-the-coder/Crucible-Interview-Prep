"""Docker sandbox integration tests.

These need a working Docker daemon and the python:3.12-alpine image:

    pytest -m sandbox

They are the tests that actually prove the security controls, because they
exercise the real kernel mechanisms -- namespaces, cgroups, capabilities --
rather than the rlimit approximation the subprocess backend provides.
"""

from __future__ import annotations

import pytest

from crucible.evaluation.sandbox import ExecOutcome, ExecutionRequest

pytestmark = [pytest.mark.integration, pytest.mark.sandbox]


@pytest.fixture(scope="module")
def docker_sandbox():
    from crucible.evaluation.sandbox.docker_backend import DockerSandbox, DockerUnavailableError

    try:
        sandbox = DockerSandbox()
        if not sandbox.healthy():
            pytest.skip("Docker daemon is not reachable")
    except DockerUnavailableError as exc:
        pytest.skip(f"Docker unavailable: {exc}")

    yield sandbox
    sandbox.shutdown()


def run(sandbox, source: str, stdin: str = "", timeout: int = 5, language: str = "python"):
    return sandbox.execute(
        ExecutionRequest(language=language, source=source, stdin=stdin, timeout_seconds=timeout)
    )


# ------------------------------------------------------------ basic sanity ---


def test_runs_python(docker_sandbox) -> None:
    result = run(docker_sandbox, "print('hello from container')")
    assert result.outcome is ExecOutcome.OK
    assert result.stdout.strip() == "hello from container"


def test_runs_javascript(docker_sandbox) -> None:
    result = run(docker_sandbox, "console.log('node ok')", language="javascript")
    assert result.outcome is ExecOutcome.OK
    assert "node ok" in result.stdout


def test_large_source_is_not_limited_by_arg_max(docker_sandbox) -> None:
    """v1 interpolated base64 source into `sh -c`, which dies on ARG_MAX."""
    source = f'x = "{"A" * 300_000}"\nprint(len(x))'
    result = run(docker_sandbox, source)
    assert result.outcome is ExecOutcome.OK
    assert result.stdout.strip() == "300000"


# ---------------------------------------------------------- isolation ---


def test_code_runs_unprivileged(docker_sandbox) -> None:
    result = run(docker_sandbox, "import os; print(os.getuid())")
    assert result.stdout.strip() == "65534", "candidate code must not run as root"


def test_cannot_read_protected_host_files(docker_sandbox) -> None:
    result = run(docker_sandbox, "print(open('/etc/shadow').read())")
    assert result.outcome is ExecOutcome.RUNTIME_ERROR
    assert "Permission denied" in result.stderr


def test_root_filesystem_is_read_only(docker_sandbox) -> None:
    """Blocks persistence: overwriting an interpreter would poison the next run."""
    result = run(docker_sandbox, "open('/usr/lib/pwned', 'w').write('x')")
    assert result.outcome is ExecOutcome.RUNTIME_ERROR
    assert "Read-only file system" in result.stderr


def test_network_is_unreachable(docker_sandbox) -> None:
    source = "import socket; socket.create_connection(('1.1.1.1', 80), 3); print('CONNECTED')"
    result = run(docker_sandbox, source)
    assert result.outcome is ExecOutcome.RUNTIME_ERROR
    assert "CONNECTED" not in result.stdout


def test_workspace_is_writable(docker_sandbox) -> None:
    result = run(docker_sandbox, "open('/box/scratch','w').write('ok'); print('wrote')")
    assert result.outcome is ExecOutcome.OK


# ------------------------------------------------------- resource limits ---


def test_fork_bomb_is_refused(docker_sandbox) -> None:
    """pids_limit. v1 had no pid cap at all -- this was a host DoS."""
    result = run(docker_sandbox, "import os\nwhile True:\n    os.fork()", timeout=5)
    assert result.outcome in (ExecOutcome.RUNTIME_ERROR, ExecOutcome.TIMEOUT)
    assert result.duration_ms < 8000


def test_pool_recovers_after_a_fork_bomb(docker_sandbox) -> None:
    """The container a fork bomb ran in cannot be cleaned -- it must be replaced.

    Once the pids cgroup is saturated nothing can exec in that container,
    including the cleanup, because killing the leftovers needs a pid to fork
    pkill with. The pool must detect that and respawn.
    """
    run(docker_sandbox, "import os\nwhile True:\n    os.fork()", timeout=5)
    after = run(docker_sandbox, "print('recovered')")
    assert after.outcome is ExecOutcome.OK
    assert after.stdout.strip() == "recovered"


def test_memory_bomb_is_oom_killed(docker_sandbox) -> None:
    result = run(docker_sandbox, "x = bytearray(900 * 1024 * 1024)\nprint(len(x))")
    assert result.outcome is ExecOutcome.MEMORY_EXCEEDED


def test_run_after_a_memory_bomb_is_not_falsely_reported_as_oom(docker_sandbox) -> None:
    """State.OOMKilled is a container-lifetime flag, so it cannot be used here.

    Regression guard: using it meant every submission after a memory bomb, in
    the same pooled container, was reported MEMORY_EXCEEDED despite succeeding.
    """
    run(docker_sandbox, "x = bytearray(900 * 1024 * 1024)")
    after = run(docker_sandbox, "print('fine')")
    assert after.outcome is ExecOutcome.OK
    assert after.stdout.strip() == "fine"


def test_infinite_loop_hits_the_timeout(docker_sandbox) -> None:
    result = run(docker_sandbox, "while True:\n    pass", timeout=3)
    assert result.outcome is ExecOutcome.TIMEOUT
    assert result.duration_ms < 6000


def test_output_flood_is_bounded(docker_sandbox) -> None:
    from crucible.core.config import settings

    result = run(docker_sandbox, "for _ in range(10**8):\n    print('A'*100)", timeout=3)
    assert len(result.stdout) < settings.sandbox_max_output_bytes + 200


# ------------------------------------------------------------- pooling ---


def test_workspace_is_wiped_between_submissions(docker_sandbox) -> None:
    """Otherwise the next candidate can read the previous one's source."""
    run(docker_sandbox, "open('/box/leaked_answer','w').write('secret')")
    result = run(docker_sandbox, "import os; print(sorted(os.listdir('/box')))")
    assert "leaked_answer" not in result.stdout


def test_leftover_processes_do_not_survive_into_the_next_run(docker_sandbox) -> None:
    source = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
        "print('spawned')\n"
    )
    run(docker_sandbox, source)
    after = run(docker_sandbox, "import os; print(len(os.listdir('/proc')))")
    assert after.outcome is ExecOutcome.OK


def test_pool_is_actually_being_used(docker_sandbox) -> None:
    """If nothing ever hits the pool, the whole optimisation is inert."""
    run(docker_sandbox, "print(1)")
    second = run(docker_sandbox, "print(2)")
    assert second.pool_hit, "second run should reuse a warm container"
    assert second.duration_ms < 1000, "a pool hit should not pay container startup"
