"""The login paths, and the completion signal they are allowed to trust.

The bug this file exists to keep dead: ``login_via_browser`` treated "a
``sessionid`` cookie appeared" as "the operator is signed in". Django hands a
``sessionid`` to **anonymous** visitors, so that condition is already true the
instant the login page finishes loading. The tool saw it on its first poll,
closed the window after 40 seconds, and reported ``authenticated: true`` while
the operator was still reaching for the keyboard -- an ``instahyre_auth_status``
call one second later returned false.

A completion signal that cannot distinguish success from its absence is not a
signal. The only honest one is an authenticated request coming back 200, which
is what ``check_auth`` already measures against ``/api/v1/job_category/``.

Nothing here starts a real browser. Playwright is faked at the ``sys.modules``
seam -- the same seam the deferred ``from playwright.sync_api import
sync_playwright`` inside ``auth`` resolves through -- and the mock transport
answers 401 or 200 based on **which sessionid value the request carries**, so
"the endpoint says logged out" is a property of the cookie, not of a counter.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from conftest import (
    HTML_CHALLENGE_BODY,
    FakeClock,
    html_response,
    json_response,
    make_http,
)
from instahyre_server import auth
from instahyre_server import constants as C
from instahyre_server.auth import login_via_browser, refresh_from_profile
from instahyre_server.session import SESSION_COOKIE, SessionStore

CATEGORY = C.EP_JOB_CATEGORY

# The two sessionid values that matter. Django cycles the session key on login,
# so the value the browser holds before and after signing in is not the same --
# which is exactly why "a cookie exists" was never evidence of anything.
ANON_SESSION = "anonymous-sessionid-issued-to-a-visitor"
LIVE_SESSION = "signed-in-sessionid-after-real-login"

JOB_CATEGORIES = {
    "objects": [
        {"id": 1, "name": "Software Engineering", "is_tech": True},
        {"id": 29, "name": "Accounting and Finance", "is_tech": False},
    ],
    "meta": {"total_count": 2},
}

ANON_COOKIES = {SESSION_COOKIE: ANON_SESSION, "csrftoken": "csrf-anon"}
LIVE_COOKIES = {SESSION_COOKIE: LIVE_SESSION, "csrftoken": "csrf-live"}


# ---------------------------------------------------------------------------
# The route that tells the truth: 200 only for a signed-in session cookie
# ---------------------------------------------------------------------------


def category_route(request):
    """``/job_category/`` as Instahyre really behaves.

    401 ``{"logged_out": true}`` for anyone without a *signed-in* session --
    including a visitor holding a perfectly real anonymous ``sessionid``.
    """
    cookie_header = request.headers.get("cookie", "")
    if f"{SESSION_COOKIE}={LIVE_SESSION}" in cookie_header:
        return json_response(JOB_CATEGORIES)
    return json_response({"logged_out": True}, status=401)


def auth_http(**kwargs):
    return make_http({CATEGORY: category_route}, **kwargs)


# ---------------------------------------------------------------------------
# A scriptable stand-in for a persistent Chromium context
# ---------------------------------------------------------------------------


class FakeBrowserClosed(Exception):
    """Stands in for Playwright's TargetClosedError."""


class FakePage:
    def __init__(self, session: "FakeBrowser") -> None:
        self.session = session

    def goto(self, url, **kwargs):
        if self.session.closed:
            raise FakeBrowserClosed("Target page, context or browser has been closed")
        self.session.goto_urls.append(url)
        if self.session.goto_raises is not None:
            raise self.session.goto_raises

    def wait_for_timeout(self, milliseconds):
        # The pre-fix loop paced itself with this; advancing the fake clock
        # keeps that path terminating instead of spinning.
        self.session.clock.sleep(milliseconds / 1000.0)

    def is_closed(self):
        return self.session.closed


class FakeBrowser:
    """One persistent context, driven by a script of cookie states.

    ``cookie_script`` is handed out one entry per ``storage_state()`` call, the
    final entry repeating forever -- so a test says "anonymous, anonymous,
    anonymous, then signed in" and the operator's sign-in lands on poll four.
    """

    def __init__(
        self,
        clock,
        cookie_script,
        *,
        close_after=None,
        goto_raises=None,
        state_raises=None,
    ) -> None:
        self.clock = clock
        self.cookie_script = list(cookie_script)
        self.close_after = close_after
        self.goto_raises = goto_raises
        self.state_raises = state_raises
        self.closed = False
        self.state_calls = 0
        self.goto_urls: list[str] = []
        self.launch_args: tuple = ()
        self.launch_kwargs: dict = {}
        self.close_calls = 0
        self.pages: list[FakePage] = []

    # -- the Playwright surface this module actually uses ------------------

    def new_page(self):
        page = FakePage(self)
        self.pages.append(page)
        return page

    def storage_state(self):
        if self.closed:
            raise FakeBrowserClosed("Target page, context or browser has been closed")
        if self.state_raises is not None:
            raise self.state_raises
        index = min(self.state_calls, len(self.cookie_script) - 1)
        cookies = self.cookie_script[index]
        self.state_calls += 1
        if self.close_after is not None and self.state_calls >= self.close_after:
            self.close_window()
        return {
            "cookies": [
                {"name": name, "value": value, "domain": ".instahyre.com", "path": "/"}
                for name, value in cookies.items()
            ]
            + [{"name": "unrelated", "value": "x", "domain": ".google.com", "path": "/"}]
        }

    def close(self):
        self.close_calls += 1
        self.closed = True

    # -- test-side controls ------------------------------------------------

    def close_window(self):
        """The operator clicking the X, as distinct from us calling close()."""
        self.closed = True
        self.pages.clear()


def install_fake_playwright(monkeypatch: pytest.MonkeyPatch, browser: FakeBrowser) -> None:
    """Resolve ``from playwright.sync_api import sync_playwright`` to the fake.

    ``auth`` imports Playwright lazily inside the function, so replacing the
    module entry is enough and no production seam is needed for it.
    """

    class _Launcher:
        def launch_persistent_context(self, *args, **kwargs):
            browser.launch_args = args
            browser.launch_kwargs = kwargs
            return browser

    class _Playwright:
        chromium = _Launcher()

    class _Manager:
        def __enter__(self):
            return _Playwright()

        def __exit__(self, *exc):
            return False

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: _Manager()  # type: ignore[attr-defined]
    sync_api.Error = FakeBrowserClosed  # type: ignore[attr-defined]
    parent = types.ModuleType("playwright")
    parent.sync_api = sync_api  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "playwright", parent)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """A hand-cranked clock inside ``auth``, so a 300s wait costs nothing."""
    fake = FakeClock()
    monkeypatch.setattr(auth, "time", fake)
    monkeypatch.setattr(auth, "POLL_INTERVAL_S", 2.5, raising=False)
    monkeypatch.setattr(auth, "RECHECK_INTERVAL_S", 10.0, raising=False)
    return fake


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "session.json")


# ---------------------------------------------------------------------------
# THE BUG. An anonymous cookie is not a login.
# ---------------------------------------------------------------------------


def test_an_anonymous_session_cookie_is_never_reported_as_a_login(
    monkeypatch, clock, store
):
    """The exact reproduction: ``sessionid`` present, endpoint answers 401.

    Pre-fix this returned ``{"authenticated": true, "cookies_captured":
    ["csrftoken", "sessionid"]}`` after one poll and closed the window on the
    operator. The window may close; the success claim may not survive.
    """
    browser = FakeBrowser(clock, [ANON_COOKIES])
    install_fake_playwright(monkeypatch, browser)
    http = auth_http()

    result = login_via_browser(http, store, wait_seconds=20)

    assert result["authenticated"] is not True, (
        "an anonymous sessionid was reported as a completed login -- this is the bug"
    )
    assert result["authenticated"] is False
    assert "reason" in result and result["reason"]
    assert not store.read(), "an unverified session must never be written to disk"


def test_the_unverified_cookie_is_never_saved_or_left_in_the_jar(
    monkeypatch, clock, store
):
    """A failed attempt must leave the process exactly as it found it."""
    browser = FakeBrowser(clock, [ANON_COOKIES])
    install_fake_playwright(monkeypatch, browser)
    http = auth_http()

    login_via_browser(http, store, wait_seconds=20)

    assert http.cookies.get(SESSION_COOKIE) != ANON_SESSION
    assert not store.path.exists()


def test_the_endpoint_is_actually_asked_rather_than_the_cookie_jar(
    monkeypatch, clock, store
):
    """Pre-fix the tool made **zero** API requests -- it never asked anyone."""
    browser = FakeBrowser(clock, [ANON_COOKIES])
    install_fake_playwright(monkeypatch, browser)
    http = auth_http()

    login_via_browser(http, store, wait_seconds=20)

    assert http.routes.count(CATEGORY) >= 1, "the completion condition never hit the API"


# ---------------------------------------------------------------------------
# The happy paths
# ---------------------------------------------------------------------------


def test_a_live_session_is_confirmed_and_saved(monkeypatch, clock, store):
    """Cookie present AND the endpoint answers 200 -> success."""
    browser = FakeBrowser(clock, [LIVE_COOKIES])
    install_fake_playwright(monkeypatch, browser)
    http = auth_http()

    result = login_via_browser(http, store, wait_seconds=300)

    assert result["authenticated"] is True
    assert result["method"] == "browser"
    assert SESSION_COOKIE in result["cookies_captured"]
    assert http.cookies.get(SESSION_COOKIE) == LIVE_SESSION
    saved = store.read()
    assert saved["has_session"] is True
    assert saved["cookies"][SESSION_COOKIE] == LIVE_SESSION


def test_the_window_stays_open_until_the_operator_actually_signs_in(
    monkeypatch, clock, store
):
    """Three polls of an anonymous session, then the real one.

    This is the operator's live case. Pre-fix the window closed on poll one
    with the anonymous cookie; the fix has to keep it open and keep asking.
    """
    browser = FakeBrowser(clock, [ANON_COOKIES, ANON_COOKIES, ANON_COOKIES, LIVE_COOKIES])
    install_fake_playwright(monkeypatch, browser)
    http = auth_http()

    result = login_via_browser(http, store, wait_seconds=300)

    assert result["authenticated"] is True
    assert browser.state_calls >= 4, "the window was closed before the sign-in landed"
    assert http.cookies.get(SESSION_COOKIE) == LIVE_SESSION
    assert store.read()["cookies"][SESSION_COOKIE] == LIVE_SESSION


def test_elapsed_seconds_measures_time_to_real_auth(monkeypatch, clock, store):
    """The 40.5s the operator saw was time-to-a-cookie. It has to mean the
    thing it is named after."""
    browser = FakeBrowser(clock, [ANON_COOKIES, ANON_COOKIES, ANON_COOKIES, LIVE_COOKIES])
    install_fake_playwright(monkeypatch, browser)

    result = login_via_browser(auth_http(), store, wait_seconds=300)

    assert result["authenticated"] is True
    assert result["elapsed_seconds"] >= 7.5, (
        "success was declared before the three anonymous polls could have elapsed"
    )


def test_the_browser_is_closed_on_the_way_out_either_way(monkeypatch, clock, store):
    browser = FakeBrowser(clock, [LIVE_COOKIES])
    install_fake_playwright(monkeypatch, browser)
    login_via_browser(auth_http(), store, wait_seconds=300)
    assert browser.close_calls == 1

    failing = FakeBrowser(clock, [ANON_COOKIES])
    install_fake_playwright(monkeypatch, failing)
    login_via_browser(auth_http(), store, wait_seconds=20)
    assert failing.close_calls == 1


def test_the_login_page_is_the_page_that_opens(monkeypatch, clock, store):
    browser = FakeBrowser(clock, [LIVE_COOKIES])
    install_fake_playwright(monkeypatch, browser)

    login_via_browser(auth_http(), store, wait_seconds=300)

    assert browser.goto_urls == [auth.LOGIN_URL]
    assert browser.launch_kwargs.get("headless") is False, "the operator must see it"


# ---------------------------------------------------------------------------
# Timeout, and the states that are not success
# ---------------------------------------------------------------------------


def test_a_timeout_returns_false_with_a_reason_rather_than_success(
    monkeypatch, clock, store
):
    browser = FakeBrowser(clock, [{}])  # no cookies at all, ever
    install_fake_playwright(monkeypatch, browser)

    result = login_via_browser(auth_http(), store, wait_seconds=20)

    assert result["authenticated"] is False
    assert "20" in result["reason"], "the reason should name the wait it exhausted"
    assert result["session_cookie_present"] is False


def test_the_window_is_left_open_for_the_whole_wait(monkeypatch, clock, store):
    """``wait_seconds`` is a promise to the human standing at the keyboard."""
    browser = FakeBrowser(clock, [ANON_COOKIES])
    install_fake_playwright(monkeypatch, browser)

    result = login_via_browser(auth_http(), store, wait_seconds=60)

    assert result["authenticated"] is False
    assert result["elapsed_seconds"] >= 55, (
        "the window closed early -- elapsed %s of a 60s wait" % result["elapsed_seconds"]
    )


def test_a_closed_window_is_reported_plainly_not_hung(monkeypatch, clock, store):
    """The operator closing the window himself is an answer, not a crash."""
    browser = FakeBrowser(clock, [ANON_COOKIES], close_after=2)
    install_fake_playwright(monkeypatch, browser)

    result = login_via_browser(auth_http(), store, wait_seconds=300)

    assert result["authenticated"] is False
    assert result.get("window_closed") is True
    assert "close" in result["reason"].lower()
    assert result["elapsed_seconds"] < 300


def test_a_sign_in_finished_just_before_the_window_closed_still_counts(
    monkeypatch, clock, store
):
    """Signed in, then closed the window before the next poll. That is a login."""
    browser = FakeBrowser(clock, [ANON_COOKIES, LIVE_COOKIES], close_after=2)
    install_fake_playwright(monkeypatch, browser)
    http = auth_http()

    result = login_via_browser(http, store, wait_seconds=300)

    assert result["authenticated"] is True
    assert store.read()["cookies"][SESSION_COOKIE] == LIVE_SESSION


def test_a_cloudflare_challenge_is_not_a_logged_out_verdict(monkeypatch, clock, store):
    """Unknown must not collapse into false, here as in ``check_auth``."""
    browser = FakeBrowser(clock, [ANON_COOKIES])
    install_fake_playwright(monkeypatch, browser)
    http = make_http({CATEGORY: html_response(HTML_CHALLENGE_BODY, status=403)})

    result = login_via_browser(http, store, wait_seconds=300)

    assert result["authenticated"] is None
    assert result["error"] == "challenge_detected"
    assert not store.read()


def test_a_challenge_stops_the_polling_instead_of_hammering_it(
    monkeypatch, clock, store
):
    """"Stop and reassess" cannot mean "ask 120 more times"."""
    browser = FakeBrowser(clock, [ANON_COOKIES])
    install_fake_playwright(monkeypatch, browser)
    http = make_http({CATEGORY: html_response(HTML_CHALLENGE_BODY, status=403)})

    login_via_browser(http, store, wait_seconds=300)

    assert http.routes.count(CATEGORY) == 1


def test_a_failed_attempt_does_not_clobber_a_live_session(monkeypatch, clock, store):
    """A stale profile must not cost the operator the session he already had."""
    browser = FakeBrowser(clock, [ANON_COOKIES])
    install_fake_playwright(monkeypatch, browser)
    http = auth_http()
    http.cookies.set(SESSION_COOKIE, LIVE_SESSION, domain="www.instahyre.com")

    result = login_via_browser(http, store, wait_seconds=20)

    assert result["authenticated"] is False
    assert http.cookies.get(SESSION_COOKIE) == LIVE_SESSION, (
        "an anonymous browser cookie overwrote a working session"
    )


def test_polling_is_paced_rather_than_one_request_per_tick(monkeypatch, clock, store):
    """A 300s wait must not become 120 identical requests at the API."""
    browser = FakeBrowser(clock, [ANON_COOKIES])
    install_fake_playwright(monkeypatch, browser)
    http = auth_http()

    login_via_browser(http, store, wait_seconds=300)

    assert http.routes.count(CATEGORY) <= 40, (
        "%d requests in a 300s wait is a polling storm" % http.routes.count(CATEGORY)
    )
    assert http.routes.count(CATEGORY) >= 2, "it has to keep checking, not check once"


def test_progress_is_reported_while_the_window_waits(monkeypatch, clock, store):
    """The operator saw no sign the tool was waiting. Now there is a hook."""
    browser = FakeBrowser(clock, [ANON_COOKIES])
    install_fake_playwright(monkeypatch, browser)
    seen: list[tuple] = []

    login_via_browser(
        auth_http(),
        store,
        wait_seconds=60,
        on_progress=lambda elapsed, total, message: seen.append((elapsed, total, message)),
    )

    assert len(seen) >= 2
    assert all(total == 60 for _, total, _ in seen)
    assert seen[0][0] < seen[-1][0], "elapsed must advance"
    assert any("sign in" in message.lower() for _, _, message in seen)


def test_a_broken_progress_callback_cannot_break_the_login(monkeypatch, clock, store):
    def explode(*args):
        raise RuntimeError("the MCP client hung up")

    browser = FakeBrowser(clock, [LIVE_COOKIES])
    install_fake_playwright(monkeypatch, browser)

    result = login_via_browser(auth_http(), store, wait_seconds=60, on_progress=explode)

    assert result["authenticated"] is True


def test_playwright_missing_is_a_typed_error_naming_the_alternative(monkeypatch, store):
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)

    with pytest.raises(auth.BrowserUnavailable) as excinfo:
        login_via_browser(auth_http(), store, wait_seconds=5)

    assert "instahyre_login" in str(excinfo.value)


# ---------------------------------------------------------------------------
# refresh_from_profile -- the silent path, same rule
# ---------------------------------------------------------------------------


def test_the_silent_refresh_verifies_before_it_claims_a_session(
    monkeypatch, clock, store, tmp_path
):
    """A dead profile still holds an anonymous ``sessionid``. It is not a session."""
    browser = FakeBrowser(clock, [ANON_COOKIES])
    install_fake_playwright(monkeypatch, browser)
    profile = tmp_path / "browser_profile"
    profile.mkdir()
    (profile / "Default").mkdir()
    monkeypatch.setattr(auth, "browser_profile_path", lambda: profile)
    http = auth_http()

    assert refresh_from_profile(http, store) is None
    assert not store.read(), "an unverified refresh must not overwrite the saved jar"


def test_the_silent_refresh_returns_a_session_when_the_profile_is_really_live(
    monkeypatch, clock, store, tmp_path
):
    browser = FakeBrowser(clock, [LIVE_COOKIES])
    install_fake_playwright(monkeypatch, browser)
    profile = tmp_path / "browser_profile"
    profile.mkdir()
    (profile / "Default").mkdir()
    monkeypatch.setattr(auth, "browser_profile_path", lambda: profile)
    http = auth_http()

    result = refresh_from_profile(http, store)

    assert result is not None
    assert result["authenticated"] is True
    assert store.read()["cookies"][SESSION_COOKIE] == LIVE_SESSION


def test_the_silent_refresh_leaves_a_live_jar_alone_when_the_profile_is_dead(
    monkeypatch, clock, store, tmp_path
):
    browser = FakeBrowser(clock, [ANON_COOKIES])
    install_fake_playwright(monkeypatch, browser)
    profile = tmp_path / "browser_profile"
    profile.mkdir()
    (profile / "Default").mkdir()
    monkeypatch.setattr(auth, "browser_profile_path", lambda: profile)
    http = auth_http()
    http.cookies.set(SESSION_COOKIE, LIVE_SESSION, domain="www.instahyre.com")

    assert refresh_from_profile(http, store) is None
    assert http.cookies.get(SESSION_COOKIE) == LIVE_SESSION


# ---------------------------------------------------------------------------
# instahyre_login -- the password path shares the rule
# ---------------------------------------------------------------------------


def login_routes(status_after_login=200):
    """The three endpoints a password login touches, honest about the cookie.

    The login POST sets whichever sessionid the test asks for; the category
    endpoint then judges it, exactly as the live server would.
    """
    import httpx

    def login(request: httpx.Request):
        response = json_response({"success": True}, status=200)
        value = LIVE_SESSION if status_after_login == 200 else ANON_SESSION
        response.headers["set-cookie"] = (
            f"{SESSION_COOKIE}={value}; Domain=.instahyre.com; Path=/"
        )
        return response

    from conftest import fixture_json

    return {
        C.EP_LOGIN: login,
        C.EP_INDUSTRY_TYPE: fixture_json("industry_types.json"),
        CATEGORY: category_route,
    }


def test_instahyre_login_does_not_save_a_session_it_could_not_verify(
    monkeypatch, store
):
    """The password path verified *after* writing to disk, so a login that the
    server rejects still clobbered the saved jar on its way to reporting false."""
    from instahyre_server import server as server_module
    from instahyre_server.cache import Store
    from instahyre_server.client import InstahyreClient

    http = make_http(login_routes(status_after_login=401))
    monkeypatch.setattr(
        server_module, "_client", InstahyreClient(http=http, store=Store(":memory:"))
    )
    monkeypatch.setattr(server_module, "_sessions", store)

    result = server_module.instahyre_login("someone@example.com", "not-a-real-password")

    assert result["authenticated"] is False
    assert not store.read(), "a rejected login was persisted anyway"


def test_instahyre_login_saves_and_reports_a_verified_session(monkeypatch, store):
    from instahyre_server import server as server_module
    from instahyre_server.cache import Store
    from instahyre_server.client import InstahyreClient

    http = make_http(login_routes(status_after_login=200))
    monkeypatch.setattr(
        server_module, "_client", InstahyreClient(http=http, store=Store(":memory:"))
    )
    monkeypatch.setattr(server_module, "_sessions", store)

    result = server_module.instahyre_login("someone@example.com", "not-a-real-password")

    assert result["authenticated"] is True
    assert result["session_saved"] is True
    assert store.read()["cookies"][SESSION_COOKIE] == LIVE_SESSION


# ---------------------------------------------------------------------------
# What the operator is told
# ---------------------------------------------------------------------------


def test_the_progress_bridge_declines_rather_than_raises_outside_a_request():
    """No MCP request in flight is the normal case in a test, and a plausible
    one in production. It must be a shrug, not an exception."""
    from instahyre_server.server import progress_reporter

    assert progress_reporter() is None


def test_the_browser_login_docstring_says_it_blocks():
    """The operator's complaint was that nothing told him to wait. The tool
    description is the one thing every client is guaranteed to show."""
    from instahyre_server.server import instahyre_login_browser

    doc = instahyre_login_browser.__doc__ or ""

    assert "BLOCKS" in doc, "an agent reading this must know it will sit there"
    assert "cookie is NOT the finish line" in doc
    assert "authenticated: false" in doc
