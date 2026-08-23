"""Session persistence, and an auth check that is allowed to say no.

``instahyre_auth_status`` must be able to return **false**. That sounds
obvious; the sibling Naukri server cannot do it -- it reports "logged in"
whenever a profile directory exists on disk, which is a claim about the
filesystem dressed up as a claim about the session. Here the answer comes from
an actual request to an endpoint that 401s when logged out, so a false is a
measurement.

Nothing in this module ever writes a password anywhere. The only thing that
touches disk is the cookie jar, and it is gitignored.
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from . import constants as C
from .cache import default_db_path
from .errors import AuthRequired, InstahyreError
from .http import InstahyreHTTP

SESSION_COOKIE = "sessionid"
CSRF_COOKIE = "csrftoken"


def session_path() -> Path:
    return default_db_path().parent / "session.json"


def browser_profile_path(create: bool = True) -> Path:
    """Where the persistent Chrome profile lives.

    ``create=False`` asks the question without answering it: it returns the
    path and makes no directory. That matters for ``instahyre_session_info``,
    which reports where the profile WOULD be and reads its cookie jar. Creating
    an empty directory there would turn the reader's honest "no profile has
    ever been signed in" into the far more confusing "the profile is there but
    holds no cookie database", which reads like corruption rather than absence.

    THE PATH IS DEFINED ONCE, here, for both branches. A caller computing
    ``default_db_path().parent / "browser_profile"`` for itself is how two
    spellings of one directory start to drift apart.
    """
    path = default_db_path().parent / "browser_profile"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


class SessionStore:
    """Reads and writes the cookie jar. Cookies only -- never credentials."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or session_path()

    def load_into(self, http: InstahyreHTTP) -> bool:
        """Restore a saved jar. Returns whether a session cookie was present."""
        data = self.read()
        if not data:
            return False
        for name, value in (data.get("cookies") or {}).items():
            http.cookies.set(name, value, domain="www.instahyre.com")
        return SESSION_COOKIE in (data.get("cookies") or {})

    def read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_from(self, http: InstahyreHTTP, *, method: str) -> dict:
        jar = {name: value for name, value in http.cookies.items()}
        payload = {
            "saved_at": time.time(),
            "method": method,
            "cookies": jar,
            "has_session": SESSION_COOKIE in jar,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        _restrict_permissions(self.path)
        return payload

    def clear(self) -> bool:
        if self.path.exists():
            self.path.unlink()
            return True
        return False


def _restrict_permissions(path: Path) -> None:
    """Best-effort owner-only. Windows ACLs are not POSIX modes, so this is a
    floor, not a guarantee -- the real protection is that it is gitignored."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def check_auth(http: InstahyreHTTP) -> dict:
    """Ask the server, not the filesystem, whether we are logged in.

    ``/api/v1/job_category/`` answers ``401 {"logged_out": true}`` anonymously
    and 200 with data when a session is live. One cheap request, no ambiguity.
    """
    has_cookie = bool(http.cookies.get(SESSION_COOKIE))
    try:
        payload = http.get(C.EP_JOB_CATEGORY)
    except AuthRequired:
        return {
            "authenticated": False,
            "reason": (
                "Session cookie expired or rejected. Call instahyre_login again."
                if has_cookie
                else "No session on disk. Call instahyre_login to sign in."
            ),
            "session_cookie_present": has_cookie,
            "checked_against": "GET /api/v1/job_category/ (401 when logged out)",
        }
    except InstahyreError as exc:
        return {
            "authenticated": None,
            "reason": f"Could not determine session state: {exc.message}",
            "error": exc.kind,
            "session_cookie_present": has_cookie,
        }

    count = len(payload.get("objects", [])) if isinstance(payload, dict) else 0
    return {
        "authenticated": True,
        "session_cookie_present": has_cookie,
        "checked_against": "GET /api/v1/job_category/ (401 when logged out)",
        "job_categories_visible": count,
    }


def login_with_password(http: InstahyreHTTP, email: str, password: str) -> dict:
    """Sign in over the API. No browser involved.

    Instahyre hands out a ``csrftoken`` cookie on **every** API response, so the
    token can be seeded from a cheap taxonomy call rather than the HTML login
    page -- which matters, because the HTML page is Cloudflare-gated for a
    non-browser client while ``/api/v1/*`` is not.

    The password is used for exactly one request and is never logged, cached or
    written to disk.
    """
    if not email or not password:
        raise AuthRequired("Both email and password are required.")

    if not http.cookies.get(CSRF_COOKIE):
        http.get(C.EP_INDUSTRY_TYPE)  # cheapest call that seeds the CSRF cookie

    payload = http.post(
        C.EP_LOGIN,
        json_body={"email": email, "password": password},
        extra_headers={"Origin": C.SITE_BASE},
    )
    if not http.cookies.get(SESSION_COOKIE):
        raise AuthRequired(
            "Login was accepted but no session cookie came back. If this account uses "
            "Google sign-in, use instahyre_login_browser instead.",
            response_keys=sorted(payload)[:8] if isinstance(payload, dict) else None,
        )
    return payload if isinstance(payload, dict) else {}


def cookies_from_browser_state(state: dict) -> dict[str, str]:
    """Pull the instahyre.com cookies out of a Playwright storage_state dict."""
    out: dict[str, str] = {}
    for cookie in state.get("cookies", []):
        domain = (cookie.get("domain") or "").lstrip(".")
        if domain.endswith("instahyre.com"):
            out[cookie["name"]] = cookie["value"]
    return out


def apply_cookies(http: InstahyreHTTP, cookies: dict[str, str]) -> None:
    for name, value in cookies.items():
        http.cookies.set(name, value, domain="www.instahyre.com")
