from __future__ import annotations

import os

# Must be set before crucible.core.config is imported anywhere.
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("LOG_LEVEL", "CRITICAL")
os.environ.setdefault("SANDBOX_BACKEND", "subprocess")
os.environ.setdefault("AI_PROVIDER", "fake")

import pytest


@pytest.fixture(scope="session")
def sandbox():
    from crucible.evaluation.sandbox import build_sandbox

    sb = build_sandbox("subprocess")
    yield sb
    sb.shutdown()
