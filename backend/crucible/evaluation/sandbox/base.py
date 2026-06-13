"""Sandbox contract.

Everything above this layer -- evaluation strategies, the live-room "run"
button, the AI reference-solution checker -- talks only to this interface. That
is what makes the backend swappable: Docker today, gVisor or Firecracker later,
with no change to callers. See docs/06-sandbox-deep-dive.md.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from types import TracebackType


class ExecOutcome(enum.StrEnum):
    """Why an execution ended.

    Distinguishing these matters: a timeout and a wrong answer look identical
    to a naive runner, but one means "your algorithm is too slow" and the other
    means "your algorithm is wrong", and telling a candidate the wrong one is
    worse than saying nothing.
    """

    OK = "ok"
    TIMEOUT = "timeout"
    MEMORY_EXCEEDED = "memory_exceeded"
    RUNTIME_ERROR = "runtime_error"
    COMPILE_ERROR = "compile_error"
    OUTPUT_TRUNCATED = "output_truncated"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    language: str
    source: str
    stdin: str = ""
    timeout_seconds: int | None = None
    memory_mb: int | None = None
    # Compilation is cached per (language, source) within one submission so a
    # 20-case problem compiles once, not 20 times.
    compile_only: bool = False


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    outcome: ExecOutcome
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    peak_memory_kb: int = 0
    truncated: bool = False
    # True when a warm container served this run, for pool-efficiency metrics.
    pool_hit: bool = False
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome is ExecOutcome.OK


@dataclass
class SandboxCapabilities:
    """What isolation the active backend actually provides.

    Surfaced on the health endpoint. A backend that cannot isolate must say so
    loudly rather than quietly pretending -- the subprocess fallback is safe
    for local dev and unsafe for anything else.
    """

    name: str
    isolates_filesystem: bool
    isolates_network: bool
    isolates_pids: bool
    enforces_memory: bool
    drops_privileges: bool
    production_safe: bool
    notes: list[str] = field(default_factory=list)


class SandboxBackend(ABC):
    """Executes untrusted source code and returns what happened."""

    @property
    @abstractmethod
    def capabilities(self) -> SandboxCapabilities: ...

    @abstractmethod
    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """Run one program to completion. Must never raise for *user* errors.

        A syntax error, an infinite loop or a fork bomb are all normal results
        described by ExecOutcome. Exceptions are reserved for infrastructure
        failure (the daemon is down), which is a different alert.
        """

    @abstractmethod
    def healthy(self) -> bool: ...

    def warmup(self) -> None:  # noqa: B027 - optional hook, not every backend needs one
        """Optional: pre-pull images, pre-start containers."""

    def shutdown(self) -> None:  # noqa: B027 - optional hook, not every backend needs one
        """Optional: release pooled resources."""

    def __enter__(self) -> SandboxBackend:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.shutdown()


def truncate_output(text: str, limit: int) -> tuple[str, bool]:
    """Cap output size.

    A program printing in an infinite loop will happily stream gigabytes. We
    read at most ``limit`` bytes and mark the result truncated -- the container
    memory cap does not help here because the data is leaving the container.
    """
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text, False
    clipped = encoded[:limit].decode("utf-8", errors="ignore")
    return clipped + f"\n... [output truncated at {limit} bytes]", True
