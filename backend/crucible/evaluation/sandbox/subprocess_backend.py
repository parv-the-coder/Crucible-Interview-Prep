"""Local subprocess sandbox -- DEVELOPMENT ONLY.

This exists so the platform runs on a laptop without a container runtime. It
applies POSIX resource limits (rlimits), drops into a scratch directory and
scrubs the environment, which contains an *honest* program. It does not contain
a *hostile* one:

  - no filesystem namespace, so the program can read anything the API user can,
    including .env and the source of this repository;
  - no network namespace, so it can open sockets;
  - no PID namespace, so it can see and signal other processes;
  - rlimits are per-process, so a fork bomb evades RLIMIT_AS entirely.

``capabilities.production_safe`` is False and the health endpoint reports it, so
a deployment running this cannot look healthy while being wide open. Selecting
it outside local/test raises at construction. See docs/06 for why we ship it
anyway rather than making Docker a hard prerequisite.
"""

from __future__ import annotations

import contextlib
import os
import resource
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from crucible.core.config import settings
from crucible.core.logging import get_logger
from crucible.evaluation.sandbox.base import (
    ExecOutcome,
    ExecutionRequest,
    ExecutionResult,
    SandboxBackend,
    SandboxCapabilities,
    truncate_output,
)
from crucible.evaluation.sandbox.languages import LanguageProfile, get_profile

log = get_logger(__name__)

# Markers that mean "this source does not parse", per interpreted runtime.
_SYNTAX_MARKERS = (
    "SyntaxError",
    "IndentationError",
    "TabError",
    "SyntaxError:",  # node
    "Unexpected token",  # node
    "Unexpected identifier",
)


def _is_syntax_error(stderr: str) -> bool:
    return any(marker in stderr for marker in _SYNTAX_MARKERS)


class UnsafeSandboxError(RuntimeError):
    """Raised when the insecure backend is selected outside local development."""


def _make_preexec(memory_mb: int, cpu_seconds: int, pids: int):
    """Build the child-side setup that runs between fork() and exec().

    Everything here must be async-signal-safe and must not allocate Python
    objects that could deadlock -- it runs in a forked child.
    """

    def _preexec() -> None:
        # NOTE: the new session comes from Popen(start_new_session=True).
        # Calling os.setsid() here as well raises EPERM -- the process is
        # already a session leader -- and Popen reports that as the opaque
        # "Exception occurred in preexec_fn", which is a miserable debug.

        as_bytes = memory_mb * 1024 * 1024
        limits: list[tuple[int, tuple[int, int]]] = [
            (resource.RLIMIT_AS, (as_bytes, as_bytes)),
            # CPU limit backs up the wall-clock timeout: it catches a busy loop
            # even if the parent's timer somehow fails.
            (resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1)),
            (resource.RLIMIT_FSIZE, (32 * 1024 * 1024, 32 * 1024 * 1024)),
            (resource.RLIMIT_CORE, (0, 0)),
            (resource.RLIMIT_NOFILE, (256, 256)),
            # NPROC is per-UID, not per-process. Since dev runs everything as
            # the same user, setting it low would cap the *developer's* whole
            # login session, so it goes last and is allowed to fail.
            (resource.RLIMIT_NPROC, (pids, pids)),
        ]
        for res_id, values in limits:
            try:
                resource.setrlimit(res_id, values)
            except (ValueError, OSError):
                # A limit we cannot lower is not worth aborting the run over;
                # this backend is best-effort by definition.
                continue

    return _preexec


def _drain(stream, limit: int, sink: list[bytes], flag: list[bool]) -> None:
    """Read a pipe up to ``limit`` bytes, then stop and raise the flag.

    subprocess.communicate() reads until EOF, so a program printing in a loop
    is buffered *in the worker's memory* in full before anything truncates it.
    The container memory cap does not help -- the data has already left the
    container. This reader is what stops one bad submission OOM-ing the worker.
    """
    total = 0
    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                return
            if total >= limit:
                flag[0] = True
                # Keep draining and discarding: if we stop reading, the pipe
                # buffer fills and the child blocks in write() instead of
                # dying, and the timeout path never sees it exit.
                continue
            room = limit - total
            sink.append(chunk[:room])
            total += len(chunk)
            if total >= limit:
                flag[0] = True
    except (ValueError, OSError):
        return


class SubprocessSandbox(SandboxBackend):
    def __init__(self, *, allow_unsafe: bool = False) -> None:
        if settings.environment not in ("local", "test") and not allow_unsafe:
            raise UnsafeSandboxError(
                "SANDBOX_BACKEND=subprocess provides no isolation and is refused "
                f"in environment={settings.environment!r}. Install Docker or set "
                "SANDBOX_BACKEND=docker."
            )
        log.warning(
            "sandbox.insecure_backend_selected",
            reason="subprocess backend has no namespace isolation",
            environment=settings.environment,
        )

    @property
    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            name="subprocess",
            isolates_filesystem=False,
            isolates_network=False,
            isolates_pids=False,
            enforces_memory=True,  # rlimit RLIMIT_AS, per-process only
            drops_privileges=False,
            production_safe=False,
            notes=[
                "DEV ONLY: executed code can read the host filesystem.",
                "rlimits are per-process; a fork bomb evades RLIMIT_AS.",
                "No network isolation -- executed code can reach the internet.",
            ],
        )

    def healthy(self) -> bool:
        return True

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        profile = get_profile(request.language)
        started = time.perf_counter()
        try:
            result = self._execute(profile, request)
        except Exception as exc:
            log.exception("sandbox.subprocess.internal_error", error=str(exc))
            result = ExecutionResult(
                outcome=ExecOutcome.INTERNAL_ERROR,
                exit_code=None,
                stdout="",
                stderr="",
                duration_ms=int((time.perf_counter() - started) * 1000),
                detail=str(exc),
            )
        return result

    def _execute(self, profile: LanguageProfile, request: ExecutionRequest) -> ExecutionResult:
        timeout = request.timeout_seconds or settings.sandbox_timeout_seconds
        memory_mb = request.memory_mb or settings.sandbox_memory_mb

        interpreter = shutil.which(profile.run_cmd[0]) if not profile.compile_cmd else None
        if not profile.compile_cmd and interpreter is None:
            return ExecutionResult(
                outcome=ExecOutcome.INTERNAL_ERROR,
                exit_code=None,
                stdout="",
                stderr="",
                duration_ms=0,
                detail=f"{profile.run_cmd[0]!r} is not installed on this host",
            )

        workdir = Path(tempfile.mkdtemp(prefix="crucible-box-"))
        try:
            src_dir = workdir / "src"
            src_dir.mkdir()
            (src_dir / profile.source_filename).write_text(request.source, encoding="utf-8")

            # Paths in the profiles are container-absolute; rewrite them to the
            # scratch directory so one set of profiles serves both backends.
            def localise(argv: list[str]) -> list[str]:
                return [a.replace("/box", str(workdir)) for a in argv]

            env = {
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "HOME": str(workdir),
                "TMPDIR": str(workdir),
                "LANG": "C.UTF-8",
                **profile.env,
            }

            if profile.compile_cmd:
                compiled = self._spawn(
                    localise(profile.compile_cmd),
                    stdin_data="",
                    cwd=workdir,
                    env=env,
                    timeout=settings.sandbox_compile_timeout_seconds,
                    memory_mb=max(memory_mb, 512),
                )
                if compiled[0] != 0:
                    stderr, trunc = truncate_output(
                        compiled[2] or compiled[1], settings.sandbox_max_output_bytes
                    )
                    return ExecutionResult(
                        outcome=(ExecOutcome.TIMEOUT if compiled[4] else ExecOutcome.COMPILE_ERROR),
                        exit_code=compiled[0],
                        stdout="",
                        stderr=stderr,
                        duration_ms=compiled[3],
                        truncated=trunc,
                        detail="compilation failed",
                    )
                if request.compile_only:
                    return ExecutionResult(
                        outcome=ExecOutcome.OK,
                        exit_code=0,
                        stdout="",
                        stderr="",
                        duration_ms=compiled[3],
                    )

            code, out, err, ms, timed_out, peak_kb = self._spawn(
                localise(profile.run_cmd),
                stdin_data=request.stdin,
                cwd=workdir,
                env=env,
                timeout=timeout,
                memory_mb=memory_mb,
                want_rusage=True,
            )

            stdout, out_trunc = truncate_output(out, settings.sandbox_max_output_bytes)
            stderr, err_trunc = truncate_output(err, settings.sandbox_max_output_bytes)

            if timed_out:
                outcome = ExecOutcome.TIMEOUT
            elif not profile.compile_cmd and _is_syntax_error(stderr):
                # Reported as a compile error even though Python/Node have no
                # separate compile step: telling a candidate "test 1 failed"
                # when their file does not parse is actively misleading.
                outcome = ExecOutcome.COMPILE_ERROR
            elif (
                code == -signal.SIGKILL
                or (code is not None and code < 0 and peak_kb >= memory_mb * 1024 * 0.9)
                or "MemoryError" in err
                or "std::bad_alloc" in err
            ):
                outcome = ExecOutcome.MEMORY_EXCEEDED
            elif code != 0:
                outcome = ExecOutcome.RUNTIME_ERROR
            elif out_trunc or err_trunc:
                outcome = ExecOutcome.OUTPUT_TRUNCATED
            else:
                outcome = ExecOutcome.OK

            return ExecutionResult(
                outcome=outcome,
                exit_code=code,
                stdout=stdout,
                stderr=stderr,
                duration_ms=ms,
                peak_memory_kb=peak_kb,
                truncated=out_trunc or err_trunc,
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _spawn(
        self,
        argv: list[str],
        *,
        stdin_data: str,
        cwd: Path,
        env: dict[str, str],
        timeout: int,
        memory_mb: int,
        want_rusage: bool = False,
    ):
        started = time.perf_counter()
        before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss if want_rusage else 0

        proc = subprocess.Popen(  # noqa: S603 - argv list, never a shell string
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd),
            env=env,
            preexec_fn=_make_preexec(memory_mb, timeout, settings.sandbox_pids_limit),
            start_new_session=True,
        )

        limit = settings.sandbox_max_output_bytes
        out_buf: list[bytes] = []
        err_buf: list[bytes] = []
        out_over = [False]
        err_over = [False]

        readers = [
            threading.Thread(
                target=_drain, args=(proc.stdout, limit, out_buf, out_over), daemon=True
            ),
            threading.Thread(
                target=_drain, args=(proc.stderr, limit, err_buf, err_over), daemon=True
            ),
        ]
        for t in readers:
            t.start()

        try:
            if stdin_data:
                proc.stdin.write(stdin_data.encode())
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            # The program exited without reading stdin. Normal, not an error.
            pass

        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            if proc.poll() is None:
                # Kill the whole process group: killing only the direct child
                # leaves any grandchildren it spawned running forever.
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=2)

        for t in readers:
            t.join(timeout=2)

        out = b"".join(out_buf).decode("utf-8", errors="replace")
        err = b"".join(err_buf).decode("utf-8", errors="replace")
        if out_over[0]:
            out += f"\n... [output truncated at {limit} bytes]"
        if err_over[0]:
            err += f"\n... [output truncated at {limit} bytes]"

        duration_ms = int((time.perf_counter() - started) * 1000)
        peak_kb = 0
        if want_rusage:
            after = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
            peak_kb = max(0, after - before)  # Linux reports ru_maxrss in KiB

        if want_rusage:
            return proc.returncode, out or "", err or "", duration_ms, timed_out, peak_kb
        return proc.returncode, out or "", err or "", duration_ms, timed_out
