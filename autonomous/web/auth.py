"""Optional shared-token authentication.

Off by default: with no ``AUTH_TOKEN`` the app behaves exactly as before, which
is right for a panel bound to 127.0.0.1. Set a token the moment the panel is
reachable from anywhere else - it can read your inbox, spend your API credits
and call your configured services.

Two ways in, both constant-time compared:

* ``Authorization: Bearer <token>`` for scripts and curl.
* A signed, HttpOnly session cookie issued by the login form, for the browser.

The cookie holds a signature of the token rather than the token itself, so a
stolen cookie cannot be replayed as a bearer credential elsewhere.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

COOKIE_NAME = "autonomous_session"
# Paths reachable without a session, so the login page can render and a load
# balancer can health-check.
PUBLIC_PATHS = ("/login", "/healthz", "/static/")


def session_value(token: str) -> str:
    """A stable, non-reversible marker that the holder knew the token."""
    return hashlib.sha256(f"autonomous-session:{token}".encode()).hexdigest()


def is_authorised(request: Request, token: str) -> bool:
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer ") and secrets.compare_digest(
        header.removeprefix("Bearer ").strip(), token
    ):
        return True
    cookie = request.cookies.get(COOKIE_NAME)
    return bool(cookie and hmac.compare_digest(cookie, session_value(token)))


def wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Autonomous — sign in</title><link rel="stylesheet" href="/static/app.css"></head>
<body>
<main class="panel" style="max-width:22rem;padding-top:4rem">
  <section class="board">
    <div class="board-head"><h2>Sign in</h2></div>
    <form method="post" action="/login">
      <input type="password" name="token" placeholder="Access token" autofocus
             autocomplete="current-password"
             style="font:inherit;color:inherit;border-radius:8px;border:1px solid var(--line);
                    background:var(--surface-1);padding:.5rem .65rem;width:100%">
      <div class="form-row">
        <button type="submit">Sign in</button>
        <span class="hint">{message}</span>
      </div>
    </form>
  </section>
</main>
</body></html>
"""


def login_page(message: str = "", status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(LOGIN_PAGE.format(message=message), status_code=status_code)


def set_session_cookie(response: Response, token: str, *, secure: bool) -> None:
    response.set_cookie(
        COOKIE_NAME,
        session_value(token),
        httponly=True,
        samesite="lax",
        # Only over HTTPS when the panel is served over HTTPS; a Secure cookie
        # on plain http would simply never be sent back.
        secure=secure,
        max_age=60 * 60 * 24 * 30,
    )


def redirect_to_login() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)
