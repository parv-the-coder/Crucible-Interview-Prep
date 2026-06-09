"""Domain enums.

These are emitted as native Postgres ENUM types so the database rejects a bad
value even when something writes to it outside the application.

Use ``pg_enum()`` to build the column type -- never ``sqlalchemy.Enum``
directly. See the note on that function for the bug it prevents.
"""

from __future__ import annotations

import enum

from sqlalchemy import Enum as SAEnum


def pg_enum[E: enum.Enum](enum_cls: type[E], name: str) -> SAEnum:
    """Native Postgres ENUM that stores the member VALUE, not its NAME.

    This matters more than it looks. By default SQLAlchemy persists
    ``SessionStatus.ACTIVE`` as the string ``"ACTIVE"`` (the member *name*),
    not ``"active"`` (its value). Any hand-written SQL -- a partial index
    predicate like ``WHERE status = 'active'``, a migration backfill, an
    analytics query -- would then silently match zero rows, and a partial index
    that matches nothing is not an error, just an index that never gets used.

    ``values_callable`` makes the stored representation the lowercase value,
    which is what the rest of the schema and the API contract assume.
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=True,
        values_callable=lambda cls: [member.value for member in cls],
        validate_strings=True,
    )


class UserRole(enum.StrEnum):
    STUDENT = "student"
    INTERVIEWER = "interviewer"
    ADMIN = "admin"


class QuestionType(enum.StrEnum):
    CODE = "code"
    MCQ = "mcq"
    SQL = "sql"
    SYSTEM_DESIGN = "system_design"
    BEHAVIORAL = "behavioral"


class Difficulty(enum.StrEnum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class Language(enum.StrEnum):
    PYTHON = "python"
    JAVASCRIPT = "javascript"
    CPP = "cpp"
    JAVA = "java"
    GO = "go"
    SQL = "sql"


class SubmissionStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TestCaseOutcome(enum.StrEnum):
    PASSED = "passed"
    WRONG_ANSWER = "wrong_answer"
    TIMEOUT = "timeout"
    RUNTIME_ERROR = "runtime_error"
    COMPILE_ERROR = "compile_error"
    MEMORY_EXCEEDED = "memory_exceeded"
    OUTPUT_TRUNCATED = "output_truncated"
    INTERNAL_ERROR = "internal_error"


class SessionStatus(enum.StrEnum):
    ACTIVE = "active"
    SUBMITTED = "submitted"
    AUTO_SUBMITTED = "auto_submitted"
    EXPIRED = "expired"
    ABANDONED = "abandoned"


class ViolationKind(enum.StrEnum):
    TAB_BLUR = "tab_blur"
    FULLSCREEN_EXIT = "fullscreen_exit"
    PASTE_LARGE = "paste_large"
    COPY = "copy"
    DEVTOOLS = "devtools"
    MULTIPLE_SESSIONS = "multiple_sessions"


class ViolationAction(enum.StrEnum):
    WARNED = "warned"
    LOGGED = "logged"
    AUTO_SUBMITTED = "auto_submitted"


class RoomStatus(enum.StrEnum):
    WAITING = "waiting"
    LIVE = "live"
    ENDED = "ended"


class RoomRole(enum.StrEnum):
    INTERVIEWER = "interviewer"
    CANDIDATE = "candidate"
    OBSERVER = "observer"


class AIPurpose(enum.StrEnum):
    CODE_REVIEW = "code_review"
    FOLLOW_UP = "follow_up"
    HINT = "hint"
    QUESTION_GEN = "question_gen"
    BEHAVIORAL_GRADE = "behavioral_grade"
    SESSION_DEBRIEF = "session_debrief"
