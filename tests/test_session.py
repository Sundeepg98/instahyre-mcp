"""session.py -- an auth check that is allowed to say no, and a cookies-only jar.

``check_auth`` must be able to return false, and it must do so from a
measurement (a request to an endpoint that 401s) rather than from the presence
of a file on disk. Both halves are tested here.
"""

from __future__ import annotations

import json

import httpx
import pytest

from conftest import HTML_CHALLENGE_BODY, fixture_json, html_response, json_response, make_http
from instahyre_server import constants as C
from instahyre_server.errors import AuthRequired
from instahyre_server.session import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    SessionStore,
    apply_cookies,
    check_auth,
    cookies_from_browser_state,
    login_with_password,
)

CATEGORY = C.EP_JOB_CATEGORY
JOB_CATEGORIES = {
    "objects": [
        {"id": 1, "name": "Software Engineering", "is_tech": True},
        {"id": 29, "name": "Accounting and Finance", "is_tech": False},
    ],
    "meta": {"total_count": 2},
}


# ---------------------------------------------------------------------------
# check_auth -- the tool must be able to say no
# ---------------------------------------------------------------------------


def test_check_auth_returns_false_with_a_reason_when_the_endpoint_401s():
    """A false here is a measurement, not a guess about the filesystem."""
    http = make_http({CATEGORY: json_response(fixture_json("error_401.json"), status=401)})

    status = check_auth(http)

    assert status["authenticated"] is False
    assert status["session_cookie_present"] is False
    assert "instahyre_login" in status["reason"]
    assert status["checked_against"] == "GET /api/v1/job_category/ (401 when logged out)"


def test_check_auth_distinguishes_an_expired_cookie_from_no_cookie_at_all():
    http = make_http({CATEGORY: json_response({"logged_out": True}, status=401)})
    http.cookies.set(SESSION_COOKIE, "stale-value", domain="www.instahyre.com")

    status = check_auth(http)

    assert status["authenticated"] is False
    assert status["session_cookie_present"] is True
    assert "expired or rejected" in status["reason"]


def test_check_auth_returns_true_on_a_200():
    http = make_http({CATEGORY: JOB_CATEGORIES})
    http.cookies.set(SESSION_COOKIE, "live-value", domain="www.instahyre.com")

    status = check_auth(http)

    assert status["authenticated"] is True
    assert status["session_cookie_present"] is True
    assert status["job_categories_visible"] == 2


def test_check_auth_says_unknown_rather_than_false_when_it_cannot_tell():
    """A Cloudflare challenge is not a logged-out verdict, and must not pose as one."""
    http = make_http({CATEGORY: html_response(HTML_CHALLENGE_BODY, status=403)})

    status = check_auth(http)

    assert status["authenticated"] is None, "unknown must not collapse into false"
    assert status["error"] == "challenge_detected"


def test_check_auth_costs_exactly_one_request():
    http = make_http({CATEGORY: JOB_CATEGORIES})
    check_auth(http)
    assert http.routes.count() == 1


# ---------------------------------------------------------------------------
# SessionStore
# ---------------------------------------------------------------------------


def test_save_from_and_load_into_round_trip_cookies(tmp_path):
    path = tmp_path / "session.json"
    source = make_http({})
    source.cookies.set(SESSION_COOKIE, "sess-abc123", domain="www.instahyre.com")
    source.cookies.set(CSRF_COOKIE, "csrf-xyz789", domain="www.instahyre.com")

    saved = SessionStore(path).save_from(source, method="password")

    assert saved["has_session"] is True
    assert saved["method"] == "password"
    assert path.exists()

    target = make_http({})
    assert target.cookies.get(SESSION_COOKIE) is None
    had_session = SessionStore(path).load_into(target)

    assert had_session is True
    assert target.cookies.get(SESSION_COOKIE) == "sess-abc123"
    assert target.cookies.get(CSRF_COOKIE) == "csrf-xyz789"


def test_the_saved_file_holds_cookies_only_and_no_password_field(tmp_path):
    path = tmp_path / "session.json"
    http = make_http({})
    http.cookies.set(SESSION_COOKIE, "sess-abc123", domain="www.instahyre.com")

    SessionStore(path).save_from(http, method="password")

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload) == {"saved_at", "method", "cookies", "has_session"}
    assert "password" not in payload, "a password field must never be written"
    assert set(payload["cookies"]) == {SESSION_COOKIE}
    # "method": "password" is a label for how we signed in, not a credential.
    assert payload["method"] == "password"


def test_a_real_password_never_reaches_the_saved_file(tmp_path):
    """Nothing in this module ever writes a password anywhere. Proven with a
    full login, using a password distinctive enough to grep for."""
    secret = "Zq7-Passphrase-Never-Persist"

    def login(request: httpx.Request):
        assert secret in request.content.decode("utf-8"), "the login POST must carry it"
        return httpx.Response(
            200, json={}, headers={"set-cookie": "sessionid=sess-live; Path=/"}
        )

    path = tmp_path / "session.json"
    http = make_http({C.EP_LOGIN: login})
    http.cookies.set(CSRF_COOKIE, "seeded", domain="www.instahyre.com")

    login_with_password(http, "someone@example.com", secret)
    SessionStore(path).save_from(http, method="password")

    raw = path.read_text(encoding="utf-8")
    assert secret not in raw
    assert "someone@example.com" not in raw
    assert json.loads(raw)["cookies"] == {"csrftoken": "seeded", "sessionid": "sess-live"}


def test_load_into_reports_false_when_there_is_no_session_cookie(tmp_path):
    path = tmp_path / "session.json"
    http = make_http({})
    http.cookies.set(CSRF_COOKIE, "csrf-only", domain="www.instahyre.com")
    SessionStore(path).save_from(http, method="password")

    target = make_http({})
    assert SessionStore(path).load_into(target) is False
    assert target.cookies.get(CSRF_COOKIE) == "csrf-only"


def test_read_on_a_missing_file_is_an_empty_dict(tmp_path):
    assert SessionStore(tmp_path / "nope.json").read() == {}


def test_read_on_a_corrupt_file_is_an_empty_dict_not_a_crash(tmp_path):
    path = tmp_path / "session.json"
    path.write_text("{ this is not json", encoding="utf-8")
    assert SessionStore(path).read() == {}


def test_load_into_on_a_missing_file_returns_false(tmp_path):
    assert SessionStore(tmp_path / "nope.json").load_into(make_http({})) is False


def test_clear_removes_the_file_and_reports_whether_it_existed(tmp_path):
    path = tmp_path / "session.json"
    store = SessionStore(path)
    assert store.clear() is False

    http = make_http({})
    http.cookies.set(SESSION_COOKIE, "sess", domain="www.instahyre.com")
    store.save_from(http, method="password")

    assert store.clear() is True
    assert not path.exists()


def test_session_path_lives_under_the_state_dir(isolated_state_home):
    from instahyre_server.session import session_path

    assert session_path() == isolated_state_home / "session.json"


# ---------------------------------------------------------------------------
# Cookie helpers
# ---------------------------------------------------------------------------


def test_cookies_from_browser_state_keeps_only_instahyre_cookies():
    state = {
        "cookies": [
            {"name": "sessionid", "value": "abc", "domain": ".instahyre.com"},
            {"name": "csrftoken", "value": "xyz", "domain": "www.instahyre.com"},
            {"name": "SID", "value": "google", "domain": ".google.com"},
        ]
    }
    assert cookies_from_browser_state(state) == {"sessionid": "abc", "csrftoken": "xyz"}


def test_cookies_from_browser_state_on_an_empty_state():
    assert cookies_from_browser_state({}) == {}


def test_apply_cookies_populates_the_jar():
    http = make_http({})
    apply_cookies(http, {"sessionid": "abc", "csrftoken": "xyz"})
    assert http.cookies.get("sessionid") == "abc"


# ---------------------------------------------------------------------------
# login_with_password
# ---------------------------------------------------------------------------


def test_login_seeds_the_csrf_token_then_posts_and_keeps_the_session():
    def login(request: httpx.Request):
        assert request.method == "POST"
        return httpx.Response(
            200,
            json={"email": "someone@example.com"},
            headers={
                "content-type": "application/json",
                "set-cookie": "sessionid=sess-live; Path=/",
            },
        )

    http = make_http(
        {
            C.EP_INDUSTRY_TYPE: httpx.Response(
                200,
                json=fixture_json("industry_types.json"),
                headers={"set-cookie": "csrftoken=seeded; Path=/"},
            ),
            C.EP_LOGIN: login,
        }
    )

    login_with_password(http, "someone@example.com", "hunter2")

    assert http.routes.paths == [C.EP_INDUSTRY_TYPE, C.EP_LOGIN]
    assert http.cookies.get(SESSION_COOKIE) == "sess-live"


def test_login_sends_the_csrf_header_once_the_cookie_exists():
    seen = {}

    def login(request: httpx.Request):
        seen["csrf"] = request.headers.get("x-csrftoken")
        return httpx.Response(
            200, json={}, headers={"set-cookie": "sessionid=sess-live; Path=/"}
        )

    http = make_http({C.EP_LOGIN: login})
    http.cookies.set(CSRF_COOKIE, "already-here", domain="www.instahyre.com")

    login_with_password(http, "someone@example.com", "hunter2")

    assert seen["csrf"] == "already-here"
    assert http.routes.count(C.EP_INDUSTRY_TYPE) == 0, "no need to reseed the token"


def test_login_without_a_session_cookie_raises_rather_than_claiming_success():
    http = make_http({C.EP_LOGIN: {"detail": "ok"}})
    http.cookies.set(CSRF_COOKIE, "seeded", domain="www.instahyre.com")

    with pytest.raises(AuthRequired) as excinfo:
        login_with_password(http, "someone@example.com", "hunter2")

    assert "instahyre_login_browser" in excinfo.value.message


def test_login_requires_both_email_and_password():
    http = make_http({})
    with pytest.raises(AuthRequired):
        login_with_password(http, "", "hunter2")
    with pytest.raises(AuthRequired):
        login_with_password(http, "someone@example.com", "")
    assert http.routes.count() == 0
