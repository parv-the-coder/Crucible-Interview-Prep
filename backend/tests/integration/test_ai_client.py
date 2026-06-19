"""AI client behaviour that needs a real database.

Budget counting and caching are both implemented as queries against the
ai_interactions ledger, so they cannot be tested without one.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select

from crucible.ai import AIBudgetExceededError, AIRequest, complete, complete_or_none
from crucible.ai.fake import FakeProvider
from crucible.ai.prompts import CODE_REVIEW_SCHEMA
from crucible.db.enums import AIPurpose
from crucible.db.models import AIInteraction
from crucible.db.session import get_sync_session_factory

pytestmark = [pytest.mark.integration]


@pytest.fixture
def sync_db():
    session = get_sync_session_factory()()
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def ai_user(sync_db):
    """A real user row with no AI history.

    Has to be a real row: ai_interactions.user_id is a foreign key, which is
    exactly the constraint that stops the ledger accumulating orphaned usage
    nobody can attribute.
    """
    from crucible.core.security import hash_password
    from crucible.db.models import User

    user = User(
        email=f"ai-{uuid.uuid4().hex[:12]}@crucible-itest.dev",
        display_name="AI Test",
        password_hash=hash_password("integration-test-password-9"),
    )
    sync_db.add(user)
    sync_db.commit()

    yield user.id

    sync_db.rollback()
    sync_db.execute(delete(AIInteraction).where(AIInteraction.user_id == user.id))
    sync_db.execute(delete(User).where(User.id == user.id))
    sync_db.commit()


def request_for(user_id, prompt: str = "review this") -> AIRequest:
    return AIRequest(
        prompt=prompt,
        purpose=AIPurpose.CODE_REVIEW,
        schema=CODE_REVIEW_SCHEMA,
        user_id=user_id,
    )


def test_a_successful_call_is_recorded_in_the_ledger(sync_db, ai_user) -> None:
    """The ledger is what budgets are counted from and what makes an
    AI-authored grade reproducible if it is disputed."""
    response = complete(sync_db, request_for(ai_user), provider=FakeProvider())
    sync_db.commit()

    assert response.ok
    rows = (
        sync_db.execute(select(AIInteraction).where(AIInteraction.user_id == ai_user))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].ok is True
    assert rows[0].prompt_tokens > 0
    assert rows[0].response


def test_an_identical_call_is_served_from_cache(sync_db, ai_user) -> None:
    """Regrading the same submission must not be paid for twice."""
    provider = FakeProvider()
    first = complete(sync_db, request_for(ai_user), provider=provider)
    sync_db.commit()
    second = complete(sync_db, request_for(ai_user), provider=provider)
    sync_db.commit()

    assert first.cached is False
    assert second.cached is True
    assert second.data == first.data

    rows = (
        sync_db.execute(select(AIInteraction).where(AIInteraction.user_id == ai_user))
        .scalars()
        .all()
    )
    # Both calls are logged, but only one was billable.
    assert len(rows) == 2
    assert sum(1 for r in rows if not r.cached) == 1


def test_a_different_prompt_is_not_served_from_cache(sync_db, ai_user) -> None:
    provider = FakeProvider()
    complete(sync_db, request_for(ai_user, "prompt one"), provider=provider)
    sync_db.commit()
    second = complete(sync_db, request_for(ai_user, "prompt two"), provider=provider)
    sync_db.commit()
    assert second.cached is False


def test_the_daily_budget_is_enforced(sync_db, ai_user, monkeypatch) -> None:
    from crucible.core import config

    monkeypatch.setattr(config.settings, "ai_daily_budget_per_user", 2)
    provider = FakeProvider()

    for n in range(2):
        complete(sync_db, request_for(ai_user, f"unique {n}"), provider=provider)
        sync_db.commit()

    with pytest.raises(AIBudgetExceededError):
        complete(sync_db, request_for(ai_user, "unique 3"), provider=provider)


def test_cached_calls_do_not_count_against_the_budget(sync_db, ai_user, monkeypatch) -> None:
    """A cache hit costs nothing, so charging for it would be wrong."""
    from crucible.core import config

    monkeypatch.setattr(config.settings, "ai_daily_budget_per_user", 2)
    provider = FakeProvider()

    complete(sync_db, request_for(ai_user, "same"), provider=provider)
    sync_db.commit()
    for _ in range(5):
        response = complete(sync_db, request_for(ai_user, "same"), provider=provider)
        sync_db.commit()
        assert response.cached


def test_a_failed_call_is_logged_and_does_not_count(sync_db, ai_user) -> None:
    from crucible.ai import AIError

    with pytest.raises(AIError):
        complete(sync_db, request_for(ai_user), provider=FakeProvider(fail=True))
    sync_db.commit()

    rows = (
        sync_db.execute(select(AIInteraction).where(AIInteraction.user_id == ai_user))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].ok is False
    assert rows[0].error


def test_complete_or_none_degrades_instead_of_raising(sync_db, ai_user) -> None:
    """This is the property that keeps AI out of the critical path.

    A provider outage must never turn into a failed submission or a 500 on a
    page the candidate needs.
    """
    assert complete_or_none(sync_db, request_for(ai_user), provider=FakeProvider(fail=True)) is None
    sync_db.commit()


def test_ai_disabled_short_circuits(sync_db, ai_user, monkeypatch) -> None:
    from crucible.core import config

    monkeypatch.setattr(config.settings, "ai_enabled", False)
    assert complete_or_none(sync_db, request_for(ai_user), provider=FakeProvider()) is None
