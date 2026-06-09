"""SQLAlchemy models.

Every model must be imported here: Alembic's autogenerate walks
``Base.metadata``, and a model that is never imported is invisible to it --
which silently produces migrations that drop nothing and create nothing.
"""

from crucible.db.base import Base
from crucible.db.models.ai import AIInteraction
from crucible.db.models.question import Question, TestCase
from crucible.db.models.room import InterviewRoom, RoomEvent, RoomParticipant
from crucible.db.models.session import SessionItem, TestSession, Violation
from crucible.db.models.submission import Submission, SubmissionResult
from crucible.db.models.user import RefreshToken, TopicMastery, User

__all__ = [
    "AIInteraction",
    "Base",
    "InterviewRoom",
    "Question",
    "RefreshToken",
    "RoomEvent",
    "RoomParticipant",
    "SessionItem",
    "Submission",
    "SubmissionResult",
    "TestCase",
    "TestSession",
    "TopicMastery",
    "User",
    "Violation",
]
