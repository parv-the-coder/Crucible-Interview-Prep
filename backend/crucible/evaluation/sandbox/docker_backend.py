"""Docker-backed sandbox.

Every mitigation here exists because of a specific attack. The full threat
model, with the escape each control blocks, is in docs/06-sandbox-deep-dive.md.
Summary of what an executed program is denied:

===========================  =================================================
Control                      Blocks
===========================  =================================================
network_disabled             Exfiltrating the answer key; reverse shells;
                             mining; using our IP to attack third parties
cap_drop=ALL                 CAP_SYS_ADMIN mount tricks, CAP_NET_RAW sniffing,
                             CAP_SYS_PTRACE inspecting neighbouring processes
no-new-privileges            setuid binaries in the image escalating to root
read_only rootfs             Overwriting interpreters/libraries to persist
                             into the *next* submission's run
tmpfs workspace              Filling the host disk; writes survive nothing
pids_limit                   Fork bombs (`while true; do :& done`)
mem_limit == memswap_limit   Escaping the memory cap by swapping
user=nobody                  Everything root-only, including the above
ulimit fsize                 Writing a petabyte file into the tmpfs
ulimit nofile                Exhausting host file descriptors
ulimit core                  Dumping core into the workspace
output byte cap              A print loop streaming GBs *out* of the container
wall-clock timeout           Infinite loops
===========================  =================================================

What this does NOT stop: a kernel 0-day. Container isolation is a shared-kernel
boundary. Defeating that class of attack needs gVisor or a microVM, which is
why the backend is behind an interface -- see the ADR.
"""

from __future__ import annotations

import contextlib
import io
import shlex
import socket
import tarfile
import threading
import time
import uuid
from typing import Any

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
from crucible.evaluation.sandbox.languages import (
    SOURCE_DIR,
    WORKDIR,
    LanguageProfile,
    get_profile,
)

log = get_logger(__name__)

STDIN_PATH = f"{WORKDIR}/stdin"
# Exit code 137 == 128 + SIGKILL. The kernel OOM killer and `docker kill` both
# produce it, so it is a hint to inspect OOMKilled, never a conclusion.
SIGKILL_EXIT = 137
IDLE_COMMAND = ["sleep", "infinity"]

# A clean container holds 2 processes: the idle `sleep`, and the probe reading
# this number. The headroom absorbs a transient docker exec helper; anything
# above it means a previous submission left processes behind.
IDLE_PID_BUDGET = 6

# Markers that mean "this source does not parse", per interpreted runtime.
_SYNTAX_MARKERS = (
    "SyntaxError",
    "IndentationError",
    "TabError",
    "Unexpected token",
    "Unexpected identifier",
)


def _is_syntax_error(stderr: str) -> bool:
    return any(marker in stderr for marker in _SYNTAX_MARKERS)


class DockerUnavailableError(RuntimeError):
    """The daemon is unreachable -- infrastructure failure, not user error."""


def _tar_bytes(files: dict[str, str], mode: int = 0o644) -> bytes:
    """Pack files for put_archive.

    Source is delivered as a tar stream rather than interpolated into a shell
    command. v1 base64-encoded the code into an `sh -c` string; that works
    until the payload is large enough to blow the ARG_MAX limit, and it makes
    the shell a participant in handling untrusted input.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = mode
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _stdin_wrapper(argv: list[str]) -> list[str]:
    """Wrap argv so the program reads stdin from a file.

    ``sh -c 'exec "$@" < file' sh <argv...>`` passes the command through
    argv positional parameters, so nothing user-controlled is ever parsed by
    the shell. The redirect target is a constant path we chose.
    """
    return ["sh", "-c", f'exec "$@" < {shlex.quote(STDIN_PATH)}', "sh", *argv]


class DockerSandbox(SandboxBackend):
    """Executes code in hardened containers, optionally from a warm pool."""

    def __init__(self, *, use_pool: bool | None = None) -> None:
        try:
            import docker
        except ImportError as exc:  # pragma: no cover
            raise DockerUnavailableError("docker SDK is not installed") from exc

        self._docker = docker
        try:
            self._client = docker.from_env(timeout=max(30, settings.sandbox_timeout_seconds * 3))
        except Exception as exc:
            raise DockerUnavailableError(f"cannot reach the Docker daemon: {exc}") from exc

        self._use_pool = settings.sandbox_pool_enabled if use_pool is None else use_pool
        self._pool: dict[str, list[_PooledContainer]] = {}
        self._lock = threading.Lock()
        self._closed = False

    # ------------------------------------------------------------ contract --

    @property
    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            name="docker",
            isolates_filesystem=True,
            isolates_network=True,
            isolates_pids=True,
            enforces_memory=True,
            drops_privileges=True,
            production_safe=True,
            notes=[
                "Shares the host kernel; a kernel exploit escapes.",
                "Use gVisor (runsc) or Firecracker for untrusted multi-tenant load.",
            ],
        )

    def healthy(self) -> bool:
        try:
            self._client.ping()
            return True
        except Exception:
            return False

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        profile = get_profile(request.language)
        started = time.perf_counter()
        try:
            result = self._execute(profile, request)
        except DockerUnavailableError:
            raise
        except Exception as exc:  # infrastructure hiccup, not user error
            log.exception("sandbox.internal_error", language=profile.id, error=str(exc))
            result = ExecutionResult(
                outcome=ExecOutcome.INTERNAL_ERROR,
                exit_code=None,
                stdout="",
                stderr="",
                duration_ms=int((time.perf_counter() - started) * 1000),
                detail=str(exc),
            )

        return result

    # ------------------------------------------------------------ internals --

    def _execute(self, profile: LanguageProfile, request: ExecutionRequest) -> ExecutionResult:
        timeout = request.timeout_seconds or settings.sandbox_timeout_seconds
        memory_mb = request.memory_mb or settings.sandbox_memory_mb

        # _acquire_ready, not _acquire: the former runs prepare(), which wipes
        # the workspace and verifies the container is usable. Calling _acquire
        # directly hands over a container still holding the previous
        # submission's files.
        runner, pool_hit = self._acquire_ready(profile, memory_mb)

        try:
            runner.put_files(
                {
                    f"src/{profile.source_filename}": request.source,
                    "stdin": request.stdin,
                }
            )

            if profile.compile_cmd:
                compile_timeout = settings.sandbox_compile_timeout_seconds
                compiled = runner.run(profile.compile_cmd, timeout=compile_timeout, env=profile.env)
                if compiled.exit_code != 0:
                    stderr, truncated = truncate_output(
                        compiled.stderr or compiled.stdout, settings.sandbox_max_output_bytes
                    )
                    return ExecutionResult(
                        outcome=(
                            ExecOutcome.TIMEOUT if compiled.timed_out else ExecOutcome.COMPILE_ERROR
                        ),
                        exit_code=compiled.exit_code,
                        stdout="",
                        stderr=stderr,
                        duration_ms=compiled.duration_ms,
                        truncated=truncated,
                        pool_hit=pool_hit,
                        detail="compilation failed",
                    )
                if request.compile_only:
                    return ExecutionResult(
                        outcome=ExecOutcome.OK,
                        exit_code=0,
                        stdout="",
                        stderr="",
                        duration_ms=compiled.duration_ms,
                        pool_hit=pool_hit,
                    )

            run = runner.run(_stdin_wrapper(profile.run_cmd), timeout=timeout, env=profile.env)
            peak_kb, oom_count, live_pids = runner.read_counters()

            # Processes outlived the run: a fork bomb still spawning, or
            # something backgrounded. Such a container cannot be cleaned from
            # inside -- killing the leftovers needs a free pid to fork with --
            # so it is retired rather than handed to the next submission.
            if live_pids > IDLE_PID_BUDGET:
                log.warning(
                    "sandbox.processes_survived_run",
                    language=profile.id,
                    pids=live_pids,
                    action="retiring container",
                )
                runner.retire()
            # Delta against the baseline taken in prepare(), never a
            # container-lifetime flag -- see read_counters().
            oom = oom_count > runner.oom_baseline or (
                run.exit_code == SIGKILL_EXIT and peak_kb >= memory_mb * 1024 * 0.95
            )

            stdout, out_trunc = truncate_output(run.stdout, settings.sandbox_max_output_bytes)
            stderr, err_trunc = truncate_output(run.stderr, settings.sandbox_max_output_bytes)

            if run.timed_out:
                outcome = ExecOutcome.TIMEOUT
            elif not profile.compile_cmd and _is_syntax_error(stderr):
                outcome = ExecOutcome.COMPILE_ERROR
            elif oom:
                outcome = ExecOutcome.MEMORY_EXCEEDED
            elif run.exit_code != 0:
                outcome = ExecOutcome.RUNTIME_ERROR
            elif out_trunc or err_trunc:
                outcome = ExecOutcome.OUTPUT_TRUNCATED
            else:
                outcome = ExecOutcome.OK

            if outcome is ExecOutcome.MEMORY_EXCEEDED:
                # Retire the container rather than recycle it.
                #
                # Two reasons. Hygiene: a container the kernel OOM-killed has
                # had processes terminated at arbitrary points and is not a
                # clean surface for the next candidate.
                #
                # And accounting: resetting memory.peak only lowers it to
                # memory.current, which stays pinned near the limit because
                # page cache from the allocation outlives the process. Every
                # later run in this container would then report the previous
                # submission's peak as its own.
                runner.retire()

            return ExecutionResult(
                outcome=outcome,
                exit_code=run.exit_code,
                stdout=stdout,
                stderr=stderr,
                duration_ms=run.duration_ms,
                peak_memory_kb=peak_kb,
                truncated=out_trunc or err_trunc,
                pool_hit=pool_hit,
            )
        finally:
            self._release(profile, runner)

    def _acquire(self, profile: LanguageProfile, memory_mb: int) -> tuple[_PooledContainer, bool]:
        if self._use_pool:
            with self._lock:
                bucket = self._pool.get(profile.id, [])
                while bucket:
                    candidate = bucket.pop()
                    if candidate.alive() and candidate.uses < settings.sandbox_pool_max_reuses:
                        return candidate, True
                    candidate.destroy()
        return self._spawn(profile, memory_mb), False

    def _acquire_ready(
        self, profile: LanguageProfile, memory_mb: int
    ) -> tuple[_PooledContainer, bool]:
        """Obtain a container that is verified usable, not merely running.

        A pooled container can be alive but unusable. The clearest case is a
        fork bomb: once the pids cgroup is exhausted nothing can exec in that
        container -- including the cleanup that would fix it, and including a
        root exec, because the pid limit is container-wide rather than
        per-user. `docker exec` then fails with "resource temporarily
        unavailable" for every subsequent submission routed to it.

        prepare() wipes the workspace and reads the pid count in one round
        trip, and anything that fails either is discarded. Respawning costs a
        few hundred milliseconds; handing the next candidate a dirty or dead
        container costs them their submission.
        """
        last_error: Exception | None = None
        for _ in range(3):
            runner, pool_hit = self._acquire(profile, memory_mb)
            try:
                if runner.prepare():
                    return runner, pool_hit
            except Exception as exc:
                last_error = exc
            log.warning(
                "sandbox.container_unusable",
                language=profile.id,
                reason="prepare() failed after acquire",
                action="discarding and respawning",
            )
            runner.destroy()

        raise DockerUnavailableError(
            f"could not obtain a usable sandbox container for {profile.id}: {last_error}"
        )

    def _release(self, profile: LanguageProfile, runner: _PooledContainer) -> None:
        runner.uses += 1
        if not self._use_pool or self._closed:
            runner.destroy()
            return
        # A container that hit its reuse cap is destroyed rather than recycled:
        # bounded reuse limits how far any residue from a previous submission
        # could ever travel. The probe additionally catches a container that is
        # still "running" but can no longer exec -- see _acquire_ready.
        # No probe here: the next acquire calls prepare(), which resets and
        # checks in the same round trip. Probing on release would double the
        # cost to catch a problem 200ms earlier.
        if runner.uses >= settings.sandbox_pool_max_reuses or not runner.alive():
            runner.destroy()
            return
        with self._lock:
            bucket = self._pool.setdefault(profile.id, [])
            if len(bucket) >= settings.sandbox_pool_size_per_language:
                runner.destroy()
                return
            bucket.append(runner)

    def _spawn(self, profile: LanguageProfile, memory_mb: int) -> _PooledContainer:
        docker = self._docker
        # Compiled languages need to exec the artefact they produce, so their
        # workspace cannot be noexec. Interpreted languages get noexec, which
        # stops a dropped binary from being run at all.
        exec_flag = "exec" if profile.needs_exec_mount else "noexec"
        tmpfs_opts = f"rw,{exec_flag},nosuid,nodev,size={settings.sandbox_tmpfs_mb}m,mode=1777"

        host_config: dict[str, Any] = {
            "mem_limit": f"{memory_mb}m",
            # Equal to mem_limit => swap is disabled. Without this a program can
            # simply page out and sail past the memory cap.
            "memswap_limit": f"{memory_mb}m",
            "oom_kill_disable": False,
            "nano_cpus": int(settings.sandbox_cpu_quota * 1_000_000_000),
            "pids_limit": settings.sandbox_pids_limit,
            "network_disabled": True,
            "read_only": True,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "tmpfs": {WORKDIR: tmpfs_opts, "/tmp": "rw,noexec,nosuid,nodev,size=8m"},  # noqa: S108
            "ulimits": [
                docker.types.Ulimit(name="nofile", soft=256, hard=256),
                docker.types.Ulimit(name="fsize", soft=32 * 1024 * 1024, hard=32 * 1024 * 1024),
                docker.types.Ulimit(name="core", soft=0, hard=0),
                docker.types.Ulimit(
                    name="nproc",
                    soft=settings.sandbox_pids_limit,
                    hard=settings.sandbox_pids_limit,
                ),
            ],
            # The container's OWN idle process (sleep infinity) runs as root;
            # every candidate exec runs as 65534 (see _PooledContainer.run).
            #
            # This is not a weakening. It exists because reset_workspace()
            # kills leftover candidate processes with `pkill -u 65534`, and if
            # PID 1 also ran as 65534 that pkill would kill the container it
            # was cleaning -- the next exec then fails with a confusing
            # "tar exit 1" rather than anything resembling the real cause.
            #
            # Root here is heavily defanged anyway: cap_drop=ALL,
            # no-new-privileges, read-only rootfs. And candidate code never
            # runs in this process -- it only ever runs in an exec as 65534.
            "user": "root",
            "working_dir": WORKDIR,
            "environment": {**profile.env, "HOME": WORKDIR, "TMPDIR": "/tmp"},  # noqa: S108
            "labels": {"crucible.sandbox": "1", "crucible.language": profile.id},
            "name": f"crucible-{profile.id}-{uuid.uuid4().hex[:10]}",
            "detach": True,
        }

        try:
            container = self._client.containers.run(profile.image, IDLE_COMMAND, **host_config)
        except self._docker.errors.ImageNotFound:
            log.info("sandbox.pulling_image", image=profile.image)
            self._client.images.pull(profile.image)
            container = self._client.containers.run(profile.image, IDLE_COMMAND, **host_config)
        return _PooledContainer(self._client, container, profile)

    # ------------------------------------------------------------ lifecycle --

    def warmup(self) -> None:
        """Pull images and pre-start one container per enabled language.

        Called on worker boot so the first real submission does not pay image
        pull + container start, which is seconds, not milliseconds.

        Only enabled languages are warmed. Pulling every toolchain we *can*
        run is several gigabytes, most of it for languages a given deployment
        never serves.
        """
        from crucible.evaluation.sandbox.languages import PROFILES

        enabled = [
            PROFILES[name] for name in settings.sandbox_enabled_languages if name in PROFILES
        ]
        for image in sorted({p.image for p in enabled}):
            try:
                self._client.images.get(image)
            except Exception:
                log.info("sandbox.warmup.pull", image=image)
                try:
                    self._client.images.pull(image)
                except Exception as exc:
                    log.warning("sandbox.warmup.pull_failed", image=image, error=str(exc))

        if not self._use_pool:
            return

        for profile in enabled:
            try:
                runner = self._spawn(profile, settings.sandbox_memory_mb)
                self._release(profile, runner)
            except Exception as exc:
                log.warning("sandbox.warmup.spawn_failed", language=profile.id, error=str(exc))

    def shutdown(self) -> None:
        self._closed = True
        with self._lock:
            for bucket in self._pool.values():
                for runner in bucket:
                    runner.destroy()
            self._pool.clear()


class _ExecOutput:
    __slots__ = ("duration_ms", "exit_code", "stderr", "stdout", "timed_out", "truncated")

    def __init__(
        self,
        exit_code: int | None,
        stdout: str,
        stderr: str,
        duration_ms: int,
        timed_out: bool,
        truncated: bool = False,
    ) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.duration_ms = duration_ms
        self.timed_out = timed_out
        self.truncated = truncated


class _PooledContainer:
    """One long-lived container we exec into repeatedly."""

    def __init__(self, client: Any, container: Any, profile: LanguageProfile) -> None:
        self._client = client
        self.container = container
        self.profile = profile
        self.uses = 0
        self.peak_baseline_kb = 0
        self.oom_baseline = 0

    def alive(self) -> bool:
        try:
            self.container.reload()
            return bool(self.container.status == "running")
        except Exception:
            # Unreachable container == not reusable. Nothing to log: this is
            # the expected result after the daemon restarts.
            return False

    def retire(self) -> None:
        """Mark this container as not reusable; _release() will destroy it."""
        self.uses = settings.sandbox_pool_max_reuses

    # Cleanup and all three counters in ONE exec.
    #
    # These used to be five separate exec_run calls, and each Docker exec is a
    # round trip over the daemon socket costing ~60ms. Measured, the
    # bookkeeping around an execution cost more than the execution: a trivial
    # program took ~750ms in a pooled container, of which ~600ms was us asking
    # the daemon questions one at a time.
    #
    # The shell prints three lines we parse positionally. Slightly uglier than
    # five tidy methods, and about 5x faster.
    _PREPARE = (
        # Kill anything the previous submission left running. Safe to target
        # uid 65534 broadly because the container's own PID 1 runs as root.
        "pkill -9 -u 65534 2>/dev/null; "
        f"rm -rf {WORKDIR}/* {WORKDIR}/.[!.]* 2>/dev/null; "
        f"mkdir -p {SOURCE_DIR}; "
        # Best-effort peak reset. Docker mounts /sys/fs/cgroup read-only, so
        # this usually fails; we read the value back either way.
        # Braces so the *shell's* own error is suppressed too. With
        # `echo 0 > file 2>/dev/null` the redirect is opened before the
        # redirection applies, so sh prints "Read-only file system" to its own
        # stderr and that line lands in the output we parse.
        "{ echo 0 > /sys/fs/cgroup/memory.peak; } 2>/dev/null; "
        "cat /sys/fs/cgroup/pids.current 2>/dev/null || echo -1; "
        "cat /sys/fs/cgroup/memory.peak 2>/dev/null || echo 0; "
        "awk '/^oom_kill /{print $2}' /sys/fs/cgroup/memory.events 2>/dev/null || echo 0"
    )

    # Read after every run. pids.current is in here specifically to catch a
    # container whose processes outlived the execution -- see read_counters().
    _COUNTERS = (
        "cat /sys/fs/cgroup/memory.peak 2>/dev/null || echo 0; "
        "awk '/^oom_kill /{print $2}' /sys/fs/cgroup/memory.events 2>/dev/null || echo 0; "
        "cat /sys/fs/cgroup/pids.current 2>/dev/null || echo -1"
    )

    def _sh(self, script: str) -> list[str]:
        """Run a shell snippet as root and return the numeric lines it printed.

        Only integer lines are kept, deliberately. exec_run merges stdout and
        stderr, so any warning the shell emits would otherwise shift every
        value in a positional parse -- which is exactly the bug this replaced:
        one "Read-only file system" line made the pid count parse as -1 and
        every container look unusable.

        Filtering to integers means the counters are identified by what they
        are rather than by where they landed.
        """
        result = self.container.exec_run(["sh", "-c", script], user="root")
        if result.exit_code != 0:
            return []
        raw = result.output
        if isinstance(raw, tuple):
            raw = raw[0] or b""
        lines = raw.decode(errors="replace").splitlines()
        return [line.strip() for line in lines if line.strip().lstrip("-").isdigit()]

    @staticmethod
    def _as_int(values: list[str], index: int, default: int = 0) -> int:
        try:
            return int(values[index])
        except (IndexError, ValueError):
            return default

    def prepare(self) -> bool:
        """Reset the workspace, capture baselines, and confirm reusability.

        Returns False when the container cannot be handed to another
        submission. The clearest case is a fork bomb: once the pids cgroup is
        saturated nothing can exec there -- including the cleanup, and
        including as root, because the limit is container-wide. Such a
        container cannot be recovered from inside and must be destroyed.

        Note the check is on pids.current, not "can I exec something trivial".
        An `exec true` succeeds in the single free slot a bomb leaves while a
        real run, which needs a shell plus an interpreter, does not.
        """
        try:
            values = self._sh(self._PREPARE)
        except Exception as exc:
            log.warning("sandbox.prepare_failed", error=str(exc))
            return False

        if not values:
            return False

        pids = self._as_int(values, 0, default=-1)
        if pids < 0 or pids > IDLE_PID_BUDGET:
            log.warning("sandbox.container_saturated", pids=pids, budget=IDLE_PID_BUDGET)
            return False

        self.peak_baseline_kb = self._as_int(values, 1) // 1024
        self.oom_baseline = self._as_int(values, 2)
        return True

    def read_counters(self) -> tuple[int, int, int]:
        """Peak RSS delta, OOM-kill count, and processes still alive.

        Peak is only meaningful as a delta above the baseline captured in
        prepare(): memory.peak is a container-lifetime high-water mark and the
        reset normally fails, so a reused container whose earlier submission
        peaked higher reports 0 rather than a wrong number.

        The OOM count is exact, which is why MEMORY_EXCEEDED is decided by it
        and not by the peak.

        The pid count is the important one, and it is read *after* the run for
        a specific reason. prepare() checks pids too, but its own `pkill` frees
        them momentarily -- so a fork bomb that is still spawning passes that
        check and then saturates the container again before the next
        submission runs. Measuring afterwards catches a container that a
        program left processes in, and the caller retires it.
        """
        values = self._sh(self._COUNTERS)
        if not values:
            # Could not even exec: the container is saturated or gone.
            return 0, self.oom_baseline, 10**6
        peak_kb = self._as_int(values, 0) // 1024
        oom = self._as_int(values, 1)
        pids = self._as_int(values, 2, default=10**6)
        return max(0, peak_kb - self.peak_baseline_kb), oom, pids

    def put_files(self, files: dict[str, str]) -> None:
        """Stream files into the workspace via `tar -x` on stdin.

        The obvious call is container.put_archive(), but the Docker API
        rejects it outright on any container created with read_only=True --
        "container rootfs is marked read-only" -- even when the destination is
        a writable tmpfs mount. The check is on the container, not the path.

        Dropping read_only to make put_archive work would trade a real
        security control for convenience, so instead we exec `tar -x` and write
        the archive to its stdin. Content never touches a command line, so
        there is no ARG_MAX ceiling and no shell parsing of untrusted bytes.

        Runs as root so extraction cannot fail on permissions; the workspace is
        mode 1777 and files land 0644, readable by uid 65534.
        """
        if not self.alive():
            raise RuntimeError("sandbox container is not running")

        archive = _tar_bytes(files)
        api = self._client.api
        exec_id = api.exec_create(
            self.container.id,
            ["tar", "-x", "-C", WORKDIR],
            stdin=True,
            stdout=True,
            stderr=True,
            user="root",
        )["Id"]

        sock = api.exec_start(exec_id, socket=True, demux=False)
        raw = getattr(sock, "_sock", sock)
        try:
            raw.sendall(archive)
            # Half-close so tar sees EOF and exits. Without this it blocks
            # waiting for more input and the exec never completes.
            raw.shutdown(socket.SHUT_WR)
            while raw.recv(8192):
                pass
        finally:
            with contextlib.suppress(Exception):
                sock.close()

        exit_code = api.exec_inspect(exec_id).get("ExitCode")
        if exit_code not in (0, None):
            raise RuntimeError(f"failed to stage workspace files (tar exit {exit_code})")

    def run(
        self, argv: list[str], *, timeout: int, env: dict[str, str] | None = None
    ) -> _ExecOutput:
        """Exec argv inside the container with a hard wall-clock timeout.

        The Docker exec API has no timeout, so we run it on a worker thread and
        abandon it on expiry. Abandoning alone would leak a running process, so
        the timeout path also kills the process tree inside the container.
        """
        started = time.perf_counter()
        box: dict[str, Any] = {}
        api = self._client.api
        limit = settings.sandbox_max_output_bytes

        def _target() -> None:
            try:
                exec_id = api.exec_create(
                    self.container.id,
                    argv,
                    environment=env or {},
                    workdir=WORKDIR,
                    user="65534:65534",
                    stdout=True,
                    stderr=True,
                )["Id"]
                box["exec_id"] = exec_id

                # Streamed, not buffered. exec_run() accumulates the whole
                # output in the worker's memory before returning, so a program
                # printing in a loop OOMs the *worker* -- the container memory
                # cap is irrelevant because the bytes have already left it.
                out_parts: list[bytes] = []
                err_parts: list[bytes] = []
                out_n = err_n = 0
                truncated = False

                for out_chunk, err_chunk in api.exec_start(exec_id, stream=True, demux=True):
                    if out_chunk:
                        if out_n < limit:
                            out_parts.append(out_chunk[: limit - out_n])
                        out_n += len(out_chunk)
                    if err_chunk:
                        if err_n < limit:
                            err_parts.append(err_chunk[: limit - err_n])
                        err_n += len(err_chunk)
                    if out_n >= limit and err_n >= limit:
                        truncated = True
                        break

                truncated = truncated or out_n > limit or err_n > limit
                stdout = b"".join(out_parts).decode("utf-8", errors="replace")
                stderr = b"".join(err_parts).decode("utf-8", errors="replace")
                if truncated:
                    suffix = f"\n... [output truncated at {limit} bytes]"
                    if out_n > limit:
                        stdout += suffix
                    if err_n > limit:
                        stderr += suffix

                box["stdout"] = stdout
                box["stderr"] = stderr
                box["truncated"] = truncated
                box["exit_code"] = api.exec_inspect(exec_id).get("ExitCode")
            except Exception as exc:
                box["error"] = exc

        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        thread.join(timeout)
        duration_ms = int((time.perf_counter() - started) * 1000)

        if thread.is_alive():
            self._kill_running_processes()
            return _ExecOutput(None, "", "", duration_ms, timed_out=True)

        if "error" in box:
            raise box["error"]

        return _ExecOutput(
            box.get("exit_code"),
            box.get("stdout", ""),
            box.get("stderr", ""),
            duration_ms,
            timed_out=False,
            truncated=bool(box.get("truncated")),
        )

    def _kill_running_processes(self) -> None:
        try:
            self._sh("pkill -9 -u 65534 2>/dev/null; true")
        except Exception as exc:
            # If we cannot clean it, the container is not safe to reuse.
            log.warning("sandbox.kill_failed", error=str(exc))
            self.retire()

    def destroy(self) -> None:
        # Already-removed containers raise; that is the desired end state.
        with contextlib.suppress(Exception):
            self.container.remove(force=True)
