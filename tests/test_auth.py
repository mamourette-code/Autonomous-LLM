from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from autonomous.web import auth
from autonomous.web.app import create_app

TOKEN = "s3cret-token"


@pytest.fixture
def secured(settings):
    settings.auth_token = TOKEN
    with TestClient(create_app(settings), follow_redirects=False) as client:
        yield client


def test_open_when_no_token_is_configured(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/status").status_code == 200


def test_api_requires_a_token(secured):
    response = secured.get("/api/status")
    assert response.status_code == 401
    assert response.json()["detail"] == "authentication required"


def test_browser_is_redirected_to_login(secured):
    response = secured.get("/", headers={"accept": "text/html"})
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_bearer_token_is_accepted(secured):
    response = secured.get("/api/status", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200


def test_wrong_bearer_token_is_rejected(secured):
    response = secured.get("/api/status", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


def test_login_sets_a_session_cookie_and_grants_access(secured):
    response = secured.post("/login", data={"token": TOKEN})
    assert response.status_code == 303

    cookie = response.cookies.get(auth.COOKIE_NAME)
    assert cookie is not None
    # The cookie must not be the token itself - a stolen cookie must not be
    # replayable as a bearer credential.
    assert TOKEN not in cookie

    assert secured.get("/api/status").status_code == 200


def test_wrong_password_does_not_authenticate(secured):
    response = secured.post("/login", data={"token": "wrong"})
    assert response.status_code == 401
    assert auth.COOKIE_NAME not in response.cookies
    assert secured.get("/api/status").status_code == 401


def test_forged_cookie_is_rejected(secured):
    secured.cookies.set(auth.COOKIE_NAME, "not-a-real-session")
    assert secured.get("/api/status").status_code == 401


def test_logout_clears_the_session(secured):
    secured.post("/login", data={"token": TOKEN})
    assert secured.get("/api/status").status_code == 200

    secured.post("/logout")
    assert secured.get("/api/status").status_code == 401


def test_login_page_and_health_are_reachable_unauthenticated(secured):
    assert secured.get("/login").status_code == 200
    assert secured.get("/healthz").json() == {"status": "ok"}
    # Static assets too, so the login page can style itself.
    assert secured.get("/static/app.css").status_code == 200


def test_starting_a_run_needs_authentication(secured):
    """The expensive endpoint, specifically."""
    assert secured.post("/api/runs", json={"goal": "spend my credits"}).status_code == 401
