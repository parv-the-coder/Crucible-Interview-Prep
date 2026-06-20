"""Timed test sessions against a real database.

The invariant these protect: the server owns the clock and the answer key.
Both are things a client could otherwise lie about or read.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.integration]

API = "/api/v1"


async def _auth(client, registered_user) -> dict[str, str]:
    return {"Authorization": f"Bearer {registered_user['access_token']}"}


async def _start(client, headers, **overrides):
    body = {"question_count": 3, "duration_minutes": 15}
    body.update(overrides)
    return await client.post(f"{API}/sessions", json=body, headers=headers)


async def test_starting_a_session_locks_in_questions(client, registered_user):
    headers = await _auth(client, registered_user)
    response = await _start(client, headers)
    assert response.status_code == 201, response.text

    body = response.json()
    assert len(body["items"]) == 3
    assert body["status"] == "active"
    # Server-computed, and close to the requested duration.
    assert 800 < body["seconds_remaining"] <= 900


async def test_session_never_returns_the_answer_key(client, registered_user):
    """The whole point of hidden test cases."""
    headers = await _auth(client, registered_user)
    body = (await _start(client, headers, question_count=5)).json()

    serialised = str(body)
    for item in body["items"]:
        question = item["question"]
        # Only sample cases are ever present.
        assert all(tc["expected_stdout"] is not None for tc in question["sample_test_cases"])
        # MCQ answer keys and SQL expected rows must not appear.
        assert "correct" not in str(question["public_payload"])
        assert "expected_rows" not in str(question["public_payload"])
    assert "reference_solution" not in serialised


async def test_only_one_session_can_be_active(client, registered_user):
    """Two open tests means reading one while the other's clock runs."""
    headers = await _auth(client, registered_user)
    assert (await _start(client, headers)).status_code == 201

    second = await _start(client, headers)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "session_already_active"


async def test_impossible_time_budget_is_refused(client, registered_user):
    headers = await _auth(client, registered_user)
    response = await _start(client, headers, question_count=25, duration_minutes=5)
    assert response.status_code == 422
    assert "too short" in response.json()["error"]["message"]


async def test_draft_autosave_does_not_create_a_submission(client, registered_user):
    """Autosave runs on every keystroke; it must stay cheap and ungraded."""
    headers = await _auth(client, registered_user)
    session = (await _start(client, headers)).json()
    item = session["items"][0]

    saved = await client.put(
        f"{API}/sessions/{session['id']}/items/{item['id']}/draft",
        json={"language": "python", "code": "print('wip')"},
        headers=headers,
    )
    assert saved.status_code == 204

    reloaded = (await client.get(f"{API}/sessions/{session['id']}", headers=headers)).json()
    stored = next(i for i in reloaded["items"] if i["id"] == item["id"])
    assert stored["draft_code"] == "print('wip')"
    assert stored["final_submission_id"] is None

    submissions = (await client.get(f"{API}/submissions", headers=headers)).json()
    assert submissions["items"] == []


async def test_third_violation_auto_submits(client, registered_user):
    """Two warnings, then the session closes itself.

    One accidental tab switch should not end a test; a pattern should.
    """
    headers = await _auth(client, registered_user)
    session = (await _start(client, headers)).json()
    url = f"{API}/sessions/{session['id']}/violations"

    first = await client.post(url, json={"kind": "tab_blur", "detail": {}}, headers=headers)
    second = await client.post(url, json={"kind": "copy", "detail": {}}, headers=headers)
    third = await client.post(url, json={"kind": "devtools", "detail": {}}, headers=headers)

    assert first.json()["action"] == "warned"
    assert second.json()["action"] == "warned"
    assert third.json()["action"] == "auto_submitted"
    assert third.json()["running_count"] == 3


async def test_writes_are_refused_after_the_session_closes(client, registered_user):
    headers = await _auth(client, registered_user)
    session = (await _start(client, headers)).json()
    item = session["items"][0]
    url = f"{API}/sessions/{session['id']}/violations"

    for kind in ("tab_blur", "copy", "devtools"):
        await client.post(url, json={"kind": kind, "detail": {}}, headers=headers)

    late = await client.put(
        f"{API}/sessions/{session['id']}/items/{item['id']}/draft",
        json={"code": "sneaking this in"},
        headers=headers,
    )
    assert late.status_code == 409
    assert late.json()["error"]["code"] == "session_closed"


async def test_submitting_queues_every_draft(client, registered_user):
    """An answer typed but never explicitly run still counts.

    Losing someone's work because they did not press a second button is
    indefensible, so submit finalises drafts rather than only explicit runs.
    """
    headers = await _auth(client, registered_user)
    session = (await _start(client, headers)).json()

    for item in session["items"]:
        await client.put(
            f"{API}/sessions/{session['id']}/items/{item['id']}/draft",
            json={"language": "python", "code": "print(1)"},
            headers=headers,
        )

    result = await client.post(f"{API}/sessions/{session['id']}/submit", headers=headers)
    assert result.status_code == 200
    body = result.json()
    assert body["status"] == "submitted"
    assert body["questions_attempted"] == 3


async def test_another_users_session_is_not_found(client, registered_user):
    """404 not 403 -- 403 would confirm the id is real."""
    headers = await _auth(client, registered_user)
    session = (await _start(client, headers)).json()

    other = await client.post(
        f"{API}/auth/signup",
        json={
            "email": f"other-{session['id'][:8]}@crucible-itest.dev",
            "display_name": "Other",
            "password": "another-test-password-1",
        },
    )
    other_headers = {"Authorization": f"Bearer {other.json()['tokens']['access_token']}"}

    response = await client.get(f"{API}/sessions/{session['id']}", headers=other_headers)
    assert response.status_code == 404
