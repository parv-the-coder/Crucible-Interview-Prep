"""Sandbox backend selection.

Callers ask for ``get_sandbox()`` and never name a concrete backend, so
swapping Docker for gVisor or Firecracker later touches exactly one function.
"""

from __future__ import annotations

import atexit
import threading

from crucible.core.config import settings
from crucible.core.logging import get_logger
from crucible.evaluation.sandbox.base import (
    ExecOutcome,
    ExecutionRequest,
    ExecutionResult,
    SandboxBackend,
    SandboxCapabilities,
)
from crucible.evaluation.sandbox.languages import (
    PROFILES,
    LanguageProfile,
    get_profile,
    required_images,
    supported_languages,
)

log = get_logger(__name__)

_instance: SandboxBackend | None = None
_lock = threading.Lock()


def build_sandbox(backend: str | None = None) -> SandboxBackend:
    """Construct a backend without touching the process-wide singleton."""
    name = (backend or settings.sandbox_backend).lower()

    if name == "subprocess":
        from crucible.evaluation.sandbox.subprocess_backend import SubprocessSandbox

        return SubprocessSandbox()

    raise ValueError(f"Unknown sandbox backend: {name!r}")


def get_sandbox() -> SandboxBackend:
    """Process-wide singleton.

    One instance per worker process: the Docker client holds a connection pool
    and the warm-container pool must not be duplicated per task.
    """
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = build_sandbox()
                atexit.register(_instance.shutdown)
    return _instance


def reset_sandbox() -> None:
    """Tear down the singleton (used by tests and by worker shutdown)."""
    global _instance
    with _lock:
        if _instance is not None:
            _instance.shutdown()
            _instance = None


__all__ = [
    "PROFILES",
    "ExecOutcome",
    "ExecutionRequest",
    "ExecutionResult",
    "LanguageProfile",
    "SandboxBackend",
    "SandboxCapabilities",
    "build_sandbox",
    "get_profile",
    "get_sandbox",
    "required_images",
    "reset_sandbox",
    "supported_languages",
]
