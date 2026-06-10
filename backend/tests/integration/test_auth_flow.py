"""Auth flows that can only be tested against a real database.

The refresh-rotation tests here exist because of a bug that unit tests could
not have caught: the reuse-detection revocation was being rolled back by the
very exception that reported it, so detection logged a warning, returned a
401, and left every stolen token in the family still valid. Verifying it
requires a real transaction boundary.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.integration]

AUTH = "/api/v1/auth"


async def refresh(client, token: str):
    return await client.post(f"{AUTH}/refresh", json={"refresh_token": token})


async def test_signup_returns_a_usable_access_token(client, registered_user) -> None:
    response = await client.get(
        f"{AUTH}/me", headers={"Authorization": f"Bearer {registered_user['access_token']}"}
    )
    assert response.status_code == 200
    assert response.json()["email"] == registered_user["email"]


async def test_signin_is_generic_for_wrong_password_and_unknown_account(client) -> None:
    """Different messages would turn the login form into an enumeration oracle."""
    unknown = await client.post(
        f"{AUTH}/signin",
        json={"email": "nobody-does-not-exist@crucible-itest.dev", "password": "whatever-123"},
    )
    wrong = await client.post(
        f"{AUTH}/signin", json={"email": "student@crucible.dev", "password": "definitely-wrong"}
    )
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["error"]["message"] == wrong.json()["error"]["message"]


async def test_refresh_rotates_the_token(client, registered_user) -> None:
    response = await refresh(client, registered_user["refresh_token"])
    assert response.status_code == 200
    assert response.json()["refresh_token"] != registered_user["refresh_token"]


async def test_rotated_token_cannot_be_replayed(client, registered_user) -> None:
    original = registered_user["refresh_token"]
    assert (await refresh(client, original)).status_code == 200

    replay = await refresh(client, original)
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "refresh_token_reused"


async def test_replay_revokes_the_entire_family(client, registered_user) -> None:
    """Regression: the revocation must survive the exception that reports it.

    The request-scoped session rolls back on any exception, so revoking the
    family and then raising a 401 silently undid the revocation. The stolen
    token was rejected, the *current* token stayed valid, and the account was
    still compromised -- while the logs claimed otherwise.
    """
    stolen = registered_user["refresh_token"]

    first = await refresh(client, stolen)
    current = first.json()["refresh_token"]

    second = await refresh(client, current)
    assert second.status_code == 200
    newest = second.json()["refresh_token"]

    # The attacker replays the token they stole.
    assert (await refresh(client, stolen)).status_code == 401

    # Every token in the family must now be dead, including the legitimate
    # user's most recent one. Being logged out is the correct outcome.
    assert (await refresh(client, newest)).status_code == 401
    assert (await refresh(client, current)).status_code == 401


async def test_signout_revokes_only_that_session(client, registered_user) -> None:
    second_login = await client.post(
        f"{AUTH}/signin",
        json={"email": registered_user["email"], "password": registered_user["password"]},
    )
    other_device = second_login.json()["tokens"]["refresh_token"]

    signed_out = await client.post(
        f"{AUTH}/signout", json={"refresh_token": registered_user["refresh_token"]}
    )
    assert signed_out.status_code == 204

    # Signing out on one device must not sign the user out everywhere.
    assert (await refresh(client, other_device)).status_code == 200
    assert (await refresh(client, registered_user["refresh_token"])).status_code == 401
