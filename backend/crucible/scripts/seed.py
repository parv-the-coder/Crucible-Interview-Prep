"""Seed the database.

Idempotent: re-running updates existing rows rather than duplicating them, so
it is safe to run against a database that already has data.

    python -m crucible.scripts.seed
    python -m crucible.scripts.seed --reset     # drop all data first
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import delete, select

from crucible.core.logging import configure_logging, get_logger
from crucible.core.security import hash_password
from crucible.db.enums import Difficulty, QuestionType, UserRole
from crucible.db.models import (
    AIInteraction,
    InterviewRoom,
    Question,
    RefreshToken,
    Submission,
    TestCase,
    TestSession,
    TopicMastery,
    User,
)
from crucible.db.session import sync_session_scope
from crucible.scripts.seed_data import QUESTIONS

log = get_logger(__name__)

DEMO_USERS = [
    ("admin@crucible.dev", "Platform Admin", UserRole.ADMIN, "admin-password-123"),
    ("interviewer@crucible.dev", "Sam Interviewer", UserRole.INTERVIEWER, "interviewer-pass-123"),
    ("student@crucible.dev", "Alex Candidate", UserRole.STUDENT, "student-password-123"),
]


def reset_data(db) -> None:
    """Delete everything. Order matters -- children before parents."""
    for model in (
        AIInteraction,
        Submission,
        TestSession,
        InterviewRoom,
        TopicMastery,
        RefreshToken,
        TestCase,
        Question,
        User,
    ):
        db.execute(delete(model))
    log.info("seed.reset_complete")


def seed_users(db) -> dict[str, User]:
    users: dict[str, User] = {}
    for email, name, role, password in DEMO_USERS:
        user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if user is None:
            user = User(
                email=email, display_name=name, password_hash=hash_password(password), role=role
            )
            db.add(user)
        else:
            user.display_name = name
            user.role = role
            user.password_hash = hash_password(password)
        users[email] = user
    db.flush()
    return users


def seed_questions(db, author: User) -> tuple[int, int]:
    created = updated = 0
    for spec in QUESTIONS:
        question = db.execute(
            select(Question).where(Question.slug == spec["slug"])
        ).scalar_one_or_none()

        if question is None:
            question = Question(slug=spec["slug"])
            db.add(question)
            created += 1
        else:
            # Replace test cases wholesale so an edited seed does not leave
            # stale cases behind and silently change a question's scoring.
            question.test_cases.clear()
            db.flush()
            updated += 1

        question.title = spec["title"]
        question.prompt = spec["prompt"]
        question.constraints_md = spec.get("constraints_md", "")
        question.type = QuestionType(spec["type"])
        question.difficulty = Difficulty(spec["difficulty"])
        question.topic = spec["topic"]
        question.tags = spec.get("tags", [])
        question.time_limit_ms = spec.get("time_limit_ms", 5000)
        question.memory_limit_mb = spec.get("memory_limit_mb", 256)
        question.allowed_languages = spec.get("allowed_languages", [])
        question.starter_code = spec.get("starter_code", {})
        question.reference_solution = spec.get("reference_solution")
        question.payload = spec.get("payload", {})
        question.created_by_id = author.id
        question.is_active = True

        for ordinal, case in enumerate(spec.get("test_cases", [])):
            question.test_cases.append(
                TestCase(
                    ordinal=ordinal,
                    stdin=case.get("stdin", ""),
                    expected_stdout=case["expected_stdout"],
                    is_sample=case.get("is_sample", False),
                    weight=case.get("weight", 1.0),
                    explanation=case.get("explanation", ""),
                )
            )
    db.flush()
    return created, updated


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the Crucible database")
    parser.add_argument("--reset", action="store_true", help="delete all data first")
    args = parser.parse_args()

    configure_logging()

    with sync_session_scope() as db:
        if args.reset:
            reset_data(db)
        users = seed_users(db)
        created, updated = seed_questions(db, users["admin@crucible.dev"])

    print("\nSeed complete.")
    print(f"  questions : {created} created, {updated} updated")
    print("\n  Sign in with:")
    for email, _, role, password in DEMO_USERS:
        print(f"    {role.value:12s} {email:28s} {password}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
