"""lifecycle.py -- session_info, reauth and logout, and the honesty they owe.

THE BUG THESE TESTS EXIST TO KEEP DEAD is the one this server already shipped:
``authenticated`` derived from a cookie's PRESENCE instead of from a live
request. Django hands a ``sessionid`` to anonymous visitors, so "the cookie is
there" was already true while the login page was still loading, and the tool
reported success while the operator was reaching for the keyboard.

The contract at ``mcp-servers/_audit/2026-08-23-auth-contract.md`` turns that
into four rules, and every one of them has a test here that goes RED under
``scripts/presence_is_auth_control.py`` -- a build in which the verdict comes
from the cookie again. The control is the proof these are measurements and not
decoration; its measured counts are in its own docstring. If a test here stays
green under it, that test is not testing what it claims to.

    1. ``authenticated`` is null, NOT false, when the check could not run.
    2. ``expired`` is null, NOT false, when no expiry could be read.
    3. ``verify_live=False`` costs no network and no browser.
    4. A harvested ``sessionid`` that the endpoint rejects is NOT a renewal --
       and the failed renew leaves the saved session byte-identical.

NOTHING HERE STARTS A REAL BROWSER OR TOUCHES THE REAL ``_state``. Playwright is
faked at the ``sys.modules`` seam (the harness is imported from
``test_auth.py`` rather than copied, so there is one fake browser in this repo
and not two), ``INSTAHYRE_HOME`` is redirected per test by ``conftest``, and
every cookie jar read here is against a Chrome database this file builds in a
tmp dir.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import time
import types
from pathlib import Path

import pytest

from conftest import FakeClock, HTML_CHALLENGE_BODY, html_response, json_response, make_http
from instahyre_server import auth, cookie_jar, lifecycle
from instahyre_server import server as server_module
from instahyre_server.session import CSRF_COOKIE, SESSION_COOKIE, SessionStore
from test_auth import (
    ANON_COOKIES,
    ANON_SESSION,
    LIVE_COOKIES,
    LIVE_SESSION,
    CATEGORY,
    FakeBrowser,
    auth_http,
    install_fake_playwright,
)

DAY_S = 86400.0

#: Values that must never appear in a tool result, a log line or an error. They
#: are deliberately unmistakable: a substring search for them cannot match by
#: accident, so a hit is always a real leak.
SECRET_SESSION_VALUE = "SECRET-sessionid-value-must-never-be-returned"
SECRET_CSRF_VALUE = "SECRET-csrftoken-value-must-never-be-returned"
SECRET_ENCRYPTED_BLOB = b"SECRET-encrypted-blob-must-never-be-selected"


# ---------------------------------------------------------------------------
# A real Chrome cookie jar, built on disk
# ---------------------------------------------------------------------------

#: Chrome's own column set, near enough. ``value`` and ``encrypted_value`` are
#: here ON PURPOSE and are filled with the secrets above: a reader that ever
#: selects a wildcard, or names the wrong column, hands them straight to the
#: leak assertions below.
_CHROME_SCHEMA = """
CREATE TABLE cookies (
    creation_utc INTEGER NOT NULL,
    host_key TEXT NOT NULL,
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    path TEXT NOT NULL,
    expires_utc INTEGER NOT NULL,
    is_secure INTEGER NOT NULL,
    is_httponly INTEGER NOT NULL,
    last_access_utc INTEGER NOT NULL,
    has_expires INTEGER NOT NULL DEFAULT 1,
    is_persistent INTEGER NOT NULL DEFAULT 1,
    priority INTEGER NOT NULL DEFAULT 1,
    encrypted_value BLOB DEFAULT '',
    samesite INTEGER NOT NULL DEFAULT -1,
    source_scheme INTEGER NOT NULL DEFAULT 0
);
"""


def to_webkit(posix_seconds: float) -> int:
    """POSIX seconds -> Chrome's ``expires_utc`` (microseconds since 1601)."""
    return int((posix_seconds + cookie_jar.WEBKIT_EPOCH_OFFSET_S) * 1_000_000)


def build_jar(profile_dir: Path, rows) -> Path:
    """Write a Chrome-shaped cookie database at ``<profile>/Default/Network/Cookies``.

    ``rows`` are ``(host_key, name, expires_utc, has_expires, is_persistent)``.
    Every row carries one of the secret values above, because a jar with no
    secret in it cannot prove the reader is not fetching one.
    """
    jar = profile_dir / "Default" / "Network" / "Cookies"
    jar.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(jar))
    try:
        con.executescript(_CHROME_SCHEMA)
        for host_key, name, expires_utc, has_expires, is_persistent in rows:
            secret = (
                SECRET_SESSION_VALUE if name == SESSION_COOKIE else SECRET_CSRF_VALUE
            )
            con.execute(
                "INSERT INTO cookies (creation_utc, host_key, name, value, path, "
                "expires_utc, is_secure, is_httponly, last_access_utc, has_expires, "
                "is_persistent, priority, encrypted_value) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    to_webkit(time.time()),
                    host_key,
                    name,
                    secret,
                    "/",
                    expires_utc,
                    1,
                    1,
                    to_webkit(time.time()),
                    has_expires,
                    is_persistent,
                    1,
                    SECRET_ENCRYPTED_BLOB,
                ),
            )
        con.commit()
    finally:
        con.close()
    return jar


def live_profile(tmp_path: Path, *, session_days=57.7, csrf_days=363.6) -> Path:
    """A profile whose jar looks like the operator's real one, measured 2026-08-23."""
    profile = tmp_path / "browser_profile"
    now = time.time()
    build_jar(
        profile,
        [
            (
                ".instahyre.com",
                SESSION_COOKIE,
                to_webkit(now + session_days * DAY_S),
                1,
                1,
            ),
            (
                ".instahyre.com",
                CSRF_COOKIE,
                to_webkit(now + csrf_days * DAY_S),
                1,
                1,
            ),
        ],
    )
    return profile


def saved_store(tmp_path: Path, cookies=None) -> SessionStore:
    """A ``SessionStore`` with a session.json already written, secrets and all."""
    store = SessionStore(tmp_path / "session.json")
    payload = {
        "saved_at": 1_700_000_000.0,
        "method": "browser",
        "cookies": cookies
        if cookies is not None
        else {
            SESSION_COOKIE: SECRET_SESSION_VALUE,
            CSRF_COOKIE: SECRET_CSRF_VALUE,
        },
        "has_session": True,
    }
    store.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return store


def strings_in(payload, _trail="") -> list:
    """Every string anywhere in ``payload``, with the path that reached it.

    Dict KEYS are walked as well as values -- a secret in a key is still a
    secret, and a walker that only visited values would be a check that cannot
    fail for a whole class of payload.
    """
    out = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            here = "%s.%s" % (_trail, key)
            if isinstance(key, str):
                out.append((here + " (KEY)", key))
            out.extend(strings_in(value, here))
    elif isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            out.extend(strings_in(value, "%s[%d]" % (_trail, index)))
    elif isinstance(payload, str):
        out.append((_trail or "<root>", payload))
    return out


def assert_no_secret(payload) -> None:
    hits = [
        "%s = %r" % (where, text)
        for where, text in strings_in(payload)
        for secret in (SECRET_SESSION_VALUE, SECRET_CSRF_VALUE)
        if secret in text
    ]
    assert not hits, "a cookie VALUE reached a tool result:\n  " + "\n  ".join(hits)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """A hand-cranked clock inside ``auth``, so a bounded wait costs nothing."""
    fake = FakeClock()
    monkeypatch.setattr(auth, "time", fake)
    monkeypatch.setattr(auth, "POLL_INTERVAL_S", 2.5, raising=False)
    monkeypatch.setattr(auth, "RECHECK_INTERVAL_S", 10.0, raising=False)
    return fake


@pytest.fixture
def exploding_playwright(monkeypatch: pytest.MonkeyPatch) -> list:
    """Playwright that RAISES if anything asks it for a browser.

    A test that claims "no browser was launched" is worth nothing if the only
    evidence is that no window appeared -- a headless one would not have
    appeared either. This makes the launch itself the failure.
    """
    launches: list = []

    def boom():
        launches.append("sync_playwright() was called")
        raise AssertionError(
            "a browser was launched by a call that promised not to launch one"
        )

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = boom  # type: ignore[attr-defined]
    parent = types.ModuleType("playwright")
    parent.sync_api = sync_api  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", parent)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    return launches


# ===========================================================================
# 1. cookie_jar -- the ported reader
# ===========================================================================


class TestTheCookieJarReader:
    """The port of ``linkedin/linkedin_server/cookie_jar.py``, held to its rules."""

    def test_a_persistent_row_yields_its_posix_expiry(self, tmp_path):
        profile = live_profile(tmp_path)
        records = cookie_jar.read_jar(profile, [SESSION_COOKIE, CSRF_COOKIE])

        by_name = {r["name"]: r["expires"] for r in records}
        assert set(by_name) == {SESSION_COOKIE, CSRF_COOKIE}
        assert 57.0 < (by_name[SESSION_COOKIE] - time.time()) / DAY_S < 58.0
        assert 363.0 < (by_name[CSRF_COOKIE] - time.time()) / DAY_S < 364.0

    def test_the_webkit_epoch_offset_is_applied_exactly(self, tmp_path):
        """Every wrong reading of ``expires_utc`` still looks like a date.

        1601 (no offset), 2396 (read as Unix microseconds) and the year
        400,000,000 (read as Unix seconds) are all plausible-looking answers,
        so this pins the one correct arithmetic against a known instant.
        """
        profile = tmp_path / "profile"
        target = 1_800_000_000.0  # 2027-01-15T08:00:00Z
        build_jar(
            profile,
            [(".instahyre.com", SESSION_COOKIE, to_webkit(target), 1, 1)],
        )
        (record,) = cookie_jar.read_jar(profile, [SESSION_COOKIE])
        assert abs(record["expires"] - target) < 1.0
        assert (
            time.strftime("%Y-%m-%d", time.gmtime(record["expires"])) == "2027-01-15"
        )

    def test_a_session_only_row_reports_minus_one_not_a_date(self, tmp_path):
        """The anonymous ``sessionid`` shape: it dies with the browser."""
        profile = tmp_path / "profile"
        build_jar(profile, [(".instahyre.com", SESSION_COOKIE, 0, 0, 0)])
        (record,) = cookie_jar.read_jar(profile, [SESSION_COOKIE])
        assert record["expires"] == -1.0

    def test_a_lookalike_domain_is_not_instahyre(self, tmp_path):
        """``notinstahyre.com`` is a different site that can set the same name."""
        profile = tmp_path / "profile"
        now = time.time()
        build_jar(
            profile,
            [
                (".notinstahyre.com", SESSION_COOKIE, to_webkit(now + DAY_S), 1, 1),
                (".www.instahyre.com", CSRF_COOKIE, to_webkit(now + DAY_S), 1, 1),
            ],
        )
        records = cookie_jar.read_jar(profile, [SESSION_COOKIE, CSRF_COOKIE])
        assert [r["name"] for r in records] == [CSRF_COOKIE]

    def test_a_missing_profile_directory_raises_rather_than_returning_empty(
        self, tmp_path
    ):
        """An empty list would be indistinguishable from "no cookies", and the
        two mean opposite things to the caller."""
        with pytest.raises(cookie_jar.CookieJarUnavailableError) as excinfo:
            cookie_jar.read_jar(tmp_path / "nope", [SESSION_COOKIE])
        assert "does not exist" in str(excinfo.value)

    def test_a_profile_with_no_jar_file_raises_and_says_which_case_it_is(
        self, tmp_path
    ):
        profile = tmp_path / "profile"
        (profile / "Default").mkdir(parents=True)
        with pytest.raises(cookie_jar.CookieJarUnavailableError) as excinfo:
            cookie_jar.read_jar(profile, [SESSION_COOKIE])
        assert "never been signed in" in str(excinfo.value)

    def test_a_file_that_is_not_a_cookie_database_raises(self, tmp_path):
        profile = tmp_path / "profile"
        jar = profile / "Default" / "Network" / "Cookies"
        jar.parent.mkdir(parents=True)
        jar.write_bytes(b"this is not sqlite")
        with pytest.raises(cookie_jar.CookieJarUnavailableError) as excinfo:
            cookie_jar.read_jar(profile, [SESSION_COOKIE])
        assert "does not look like a Chrome cookie database" in str(excinfo.value)

    def test_sqlite_is_never_handed_the_live_file(self, tmp_path, monkeypatch):
        """THE RULE, ASSERTED AT THE SEAM. sqlite writes to a database it opens.

        The path under test is the operator's real signed-in profile, which
        Chrome may have open right now, so a read that opens it directly can
        replay a journal or take a lock on it. Recording every path
        ``sqlite3.connect`` is handed is the only way to prove the original is
        not among them.
        """
        profile = live_profile(tmp_path)
        jar = profile / "Default" / "Network" / "Cookies"
        opened: list = []
        real_connect = sqlite3.connect

        def recording_connect(target, *args, **kwargs):
            opened.append(str(target))
            return real_connect(target, *args, **kwargs)

        monkeypatch.setattr(cookie_jar.sqlite3, "connect", recording_connect)
        cookie_jar.read_jar(profile, [SESSION_COOKIE])

        assert opened, "the reader opened nothing at all -- this test is blind"
        assert str(jar) not in opened, (
            "the LIVE cookie jar was opened; only a copy may be. Opened: %r" % opened
        )

    def test_the_copy_is_deleted_even_when_the_query_blows_up(self, tmp_path):
        profile = tmp_path / "profile"
        jar = profile / "Default" / "Network" / "Cookies"
        jar.parent.mkdir(parents=True)
        sqlite3.connect(str(jar)).close()  # valid sqlite, no cookies table
        before = set(Path(__import__("tempfile").gettempdir()).glob("instahyre-cookie-jar-*"))
        with pytest.raises(cookie_jar.CookieJarUnavailableError):
            cookie_jar.read_jar(profile, [SESSION_COOKIE])
        after = set(Path(__import__("tempfile").gettempdir()).glob("instahyre-cookie-jar-*"))
        assert after <= before, "a temp copy of the cookie jar was left behind"

    def test_no_cookie_value_column_is_ever_named(self):
        """The query is the enforcement point, so the query is what is asserted."""
        assert "value" not in cookie_jar._JAR_QUERY
        assert "*" not in cookie_jar._JAR_QUERY

    def test_the_returned_records_carry_no_value_at_all(self, tmp_path):
        profile = live_profile(tmp_path)
        records = cookie_jar.read_jar(profile, [SESSION_COOKIE, CSRF_COOKIE])
        assert all(set(r) == {"name", "expires"} for r in records)
        assert_no_secret(records)


# ===========================================================================
# 2. session_info
# ===========================================================================


class TestSessionInfoOffline:
    """``verify_live=False``: free, and honest about being free."""

    def test_authenticated_is_null_and_says_why(self, tmp_path):
        out = lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=live_profile(tmp_path),
            http=None,
            verify_live=False,
        )
        assert out["server"] == "instahyre"
        assert out["authenticated"] is None
        assert out["live_check"]["attempted"] is False
        assert out["live_check"]["completed"] is False
        assert (
            out["live_check"]["why_not"]
            == "not attempted: this call asked for the offline answer"
        )
        assert "NOT because Instahyre said no" in out["live_check"]["what_it_means"]

    def test_it_makes_no_request_and_launches_no_browser(
        self, tmp_path, exploding_playwright
    ):
        """Both halves, with fakes that RAISE rather than merely count."""
        http = make_http({})  # every route unmocked: any request is an error
        out = lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=live_profile(tmp_path),
            http=http,
            verify_live=False,
        )
        assert http.routes.count() == 0
        assert exploding_playwright == []
        assert out["authenticated"] is None

    def test_the_tool_does_not_even_build_a_client(
        self, tmp_path, monkeypatch, exploding_playwright
    ):
        """The offline answer must not change the thing it is describing."""
        monkeypatch.setattr(server_module, "_client", None)
        monkeypatch.setattr(server_module, "_sessions", None)

        out = server_module.instahyre_session_info(verify_live=False)

        assert server_module._client is None, "the offline call built a client"
        assert server_module._sessions is None
        assert out["authenticated"] is None
        assert exploding_playwright == []


class TestSessionInfoLive:
    """``verify_live=True``: the verdict is a measurement, and null is not false."""

    def test_a_200_is_reported_as_authenticated_true(self, tmp_path):
        http = auth_http()
        http.cookies.set(SESSION_COOKIE, LIVE_SESSION, domain="www.instahyre.com")
        out = lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=live_profile(tmp_path),
            http=http,
            verify_live=True,
        )
        assert out["authenticated"] is True
        assert out["live_check"] == {
            "attempted": True,
            "completed": True,
            "endpoint": auth.AUTH_ENDPOINT_NOTE,
            "what_it_means": (
                "the endpoint was asked and answered 200, so 'authenticated' "
                "above is a measurement"
            ),
        }
        assert "why_not" not in out["live_check"]

    def test_a_401_is_reported_as_a_measured_false(self, tmp_path):
        """A false here is allowed BECAUSE a server said no, and only then."""
        http = auth_http()
        http.cookies.set(SESSION_COOKIE, ANON_SESSION, domain="www.instahyre.com")
        out = lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=live_profile(tmp_path),
            http=http,
            verify_live=True,
        )
        assert out["authenticated"] is False
        assert out["live_check"]["completed"] is True
        assert "measured NO" in out["live_check"]["what_it_means"]

    def test_an_undetermined_check_is_null_not_false__HONESTY(self, tmp_path):
        """RULE 1. A Cloudflare challenge is not a logged-out verdict.

        The cookie is present and looks perfect. Under a build that reads the
        verdict off the cookie this returns ``true``; under a build that treats
        any non-200 as a no it returns ``false``. Both are claims nobody
        measured, so the only correct answer is null with a reason.
        """
        http = make_http({CATEGORY: html_response(HTML_CHALLENGE_BODY, status=403)})
        http.cookies.set(SESSION_COOKIE, LIVE_SESSION, domain="www.instahyre.com")

        out = lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=live_profile(tmp_path),
            http=http,
            verify_live=True,
        )

        assert out["authenticated"] is None, "unknown must not collapse into a verdict"
        assert out["live_check"]["attempted"] is True
        assert out["live_check"]["completed"] is False
        assert out["live_check"]["why_not"]
        assert "NOT because Instahyre said no" in out["live_check"]["what_it_means"]

    def test_a_check_that_raises_is_null_with_the_real_failure_text(
        self, tmp_path, monkeypatch
    ):
        """The contract's fall-back branch: attempted true, completed false."""

        def exploding_check(http):
            raise RuntimeError("the transport fell over")

        monkeypatch.setattr(lifecycle, "check_auth", exploding_check)

        out = lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=live_profile(tmp_path),
            http=make_http({}),
            verify_live=True,
        )

        assert out["authenticated"] is None
        assert out["live_check"]["attempted"] is True
        assert "the transport fell over" in out["live_check"]["why_not"]


class TestSessionInfoExpiry:
    """The date comes from the profile's jar, and says so."""

    def test_a_readable_jar_supplies_the_expiry(self, tmp_path):
        out = lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=live_profile(tmp_path),
            http=None,
            verify_live=False,
        )
        credential = out["credential"]
        assert credential["kind"] == "cookie"
        assert credential["name"] == SESSION_COOKIE
        assert credential["present"] is True
        assert credential["format"] == "cookie"
        assert credential["expired"] is False
        assert 57.0 < credential["expires_in_days"] < 58.0
        assert credential["expires_at"].endswith("Z")
        assert credential["expiry_is_authoritative"] is True

    def test_the_expiry_source_names_BOTH_stores_and_does_not_blur_them(
        self, tmp_path
    ):
        """The quiet substitution this field exists to prevent.

        The date describes the BROWSER PROFILE's session. The cookie the client
        sends comes from session.json. A reader who is not told that will read
        the date as governing the cookie in use, and on the day the two hold
        different sessions they will be wrong with no way to notice.
        """
        store = saved_store(tmp_path)
        out = lifecycle.session_info(
            store=store,
            profile_dir=live_profile(tmp_path),
            http=None,
            verify_live=False,
        )
        source = out["credential"]["expiry_source"]
        assert "browser profile" in source
        assert "session.json" in source
        assert "different sessions" in source
        assert "no cookie value fetched" in source

    def test_an_unreadable_jar_leaves_expired_NULL_not_false__HONESTY(self, tmp_path):
        """RULE 2. ``false`` there reads as "not expired", which is a claim.

        No expiry was read, so no claim about expiry may be made. The reason
        the jar could not be read is carried instead.
        """
        out = lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=tmp_path / "no_such_profile",
            http=None,
            verify_live=False,
        )
        credential = out["credential"]
        assert credential["expires_at"] is None
        assert credential["expires_in_days"] is None
        assert credential["expired"] is None, "unknown must not become 'not expired'"
        assert credential["present"] is True, (
            "the cookie IS in the store; only its expiry is unknown, and the two "
            "facts must not be collapsed"
        )
        assert credential["expiry_source"].startswith("unknown")
        assert "could not be read" in credential["expiry_source"]

    def test_a_session_only_row_is_also_null_not_false(self, tmp_path):
        profile = tmp_path / "profile"
        build_jar(profile, [(".instahyre.com", SESSION_COOKIE, 0, 0, 0)])
        out = lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=profile,
            http=None,
            verify_live=False,
        )
        assert out["credential"]["expired"] is None
        assert out["credential"]["expires_at"] is None
        assert "session-only" in out["credential"]["expiry_source"]

    def test_an_expiry_in_the_past_is_reported_as_expired(self, tmp_path):
        profile = live_profile(tmp_path, session_days=-3.0)
        out = lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=profile,
            http=None,
            verify_live=False,
        )
        assert out["credential"]["expired"] is True
        assert out["credential"]["expires_in_days"] < 0

    def test_the_csrf_token_is_reported_as_a_supporting_credential(self, tmp_path):
        out = lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=live_profile(tmp_path),
            http=None,
            verify_live=False,
        )
        (csrf,) = out["supporting"]
        assert csrf["name"] == CSRF_COOKIE
        assert csrf["role"] == "csrf"
        assert csrf["present"] is True
        assert 363.0 < csrf["expires_in_days"] < 364.0
        assert csrf["expired"] is False

    def test_an_absent_credential_is_format_absent_not_a_guess(self, tmp_path):
        store = SessionStore(tmp_path / "session.json")
        out = lifecycle.session_info(
            store=store,
            profile_dir=live_profile(tmp_path),
            http=None,
            verify_live=False,
        )
        assert out["credential"]["present"] is False
        assert out["credential"]["format"] == "absent"


class TestSessionInfoLeaksNothing:

    def test_no_cookie_value_reaches_the_result(self, tmp_path):
        """Both stores are stuffed with unmistakable secrets first."""
        out = lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=live_profile(tmp_path),
            http=None,
            verify_live=False,
        )
        assert_no_secret(out)

    def test_no_cookie_value_reaches_a_log_line_or_an_error(self, tmp_path, caplog):
        with caplog.at_level(logging.DEBUG):
            lifecycle.session_info(
                store=saved_store(tmp_path),
                profile_dir=live_profile(tmp_path),
                http=None,
                verify_live=False,
            )
            # And the failure path, where a message is composed by hand.
            lifecycle.session_info(
                store=saved_store(tmp_path),
                profile_dir=tmp_path / "gone",
                http=None,
                verify_live=False,
            )
        assert SECRET_SESSION_VALUE not in caplog.text
        assert SECRET_CSRF_VALUE not in caplog.text

    def test_no_absolute_local_path_reaches_any_field(self, tmp_path):
        """THREE fields render a path, and one of them does it inside prose.

        ``durability.stored_in`` and ``renewal.why`` are rendered on purpose;
        ``credential.expiry_source`` carries whatever the cookie-jar reader
        said, which is built as ``"...%s..." % profile_dir`` and so is invisible
        to any field-level fix. Measured 2026-08-23 with ``display_path``
        disabled: all three leak, and the suite-wide walker in
        ``test_path_hygiene.py`` sees all three. The exact-substring form is
        used here rather than a drive-letter regex because CI runs ubuntu,
        where a drive-letter check cannot fire at all.
        """
        out = lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=tmp_path / "no_such_profile",
            http=None,
            verify_live=False,
        )
        hits = [
            "%s = %r" % (where, text)
            for where, text in strings_in(out)
            if str(tmp_path) in text
        ]
        assert not hits, "an absolute local path reached:\n  " + "\n  ".join(hits)
        assert out["durability"]["stored_in"].endswith("session.json")
        assert out["durability"]["survives_server_restart"] is True
        assert out["durability"]["survives_machine_reboot"] is True

    def test_renewal_points_at_reauth_and_says_why_it_is_possible(self, tmp_path):
        out = lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=live_profile(tmp_path),
            http=None,
            verify_live=False,
        )
        assert out["renewal"]["silent_renew_available"] is True
        assert out["renewal"]["tool"] == "instahyre_reauth"
        assert "persistent Chrome profile" in out["renewal"]["why"]
        assert "instahyre_reauth" in out["on_expiry"]
        assert "instahyre_login_browser" in out["on_expiry"]

    def test_every_contract_key_is_present(self, tmp_path):
        out = lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=live_profile(tmp_path),
            http=None,
            verify_live=False,
        )
        assert set(out) == {
            "server",
            "authenticated",
            "checked_against",
            "live_check",
            "credential",
            "supporting",
            "credential_source",
            "durability",
            "renewal",
            "on_expiry",
        }
        assert set(out["credential"]) == {
            "kind",
            "name",
            "present",
            "format",
            "expires_at",
            "expires_in_days",
            "expired",
            "expiry_source",
            "expiry_is_authoritative",
        }


# ===========================================================================
# 3. reauth
# ===========================================================================


class TestReauth:
    """A silent renew, and the discipline that keeps it from costing anything."""

    def test_a_live_profile_renews_and_saves(self, tmp_path, monkeypatch, clock):
        browser = FakeBrowser(clock, [LIVE_COOKIES])
        install_fake_playwright(monkeypatch, browser)
        profile = live_profile(tmp_path)
        monkeypatch.setattr(auth, "browser_profile_path", lambda: profile)
        store = SessionStore(tmp_path / "session.json")
        http = auth_http()

        out = lifecycle.reauth(http=http, store=store, profile_dir=profile)

        assert out["renewed"] is True
        assert out["authenticated"] is True
        assert out["stage"] == "profile_reharvest"
        assert out["checked_against"] == auth.AUTH_ENDPOINT_NOTE
        assert store.read()["cookies"][SESSION_COOKIE] == LIVE_SESSION
        assert http.cookies.get(SESSION_COOKIE) == LIVE_SESSION

    def test_a_harvested_cookie_the_endpoint_rejects_is_NOT_a_renewal__HONESTY(
        self, tmp_path, monkeypatch, clock
    ):
        """RULE 4, AND THE ONE THAT MATTERS MOST. Both halves are asserted.

        The profile hands over a real, well-formed ``sessionid``. Instahyre
        answers 401, because it is the anonymous one Django gives every
        visitor. A build that reads the verdict off the harvest reports
        ``renewed: true`` AND overwrites session.json with the anonymous
        cookie -- so the failure does not merely mislead, it destroys the
        working session it was called to protect.

        So: ``renewed`` false, AND the saved file byte-identical to what it was.
        """
        store = saved_store(tmp_path)
        before_bytes = store.path.read_bytes()

        browser = FakeBrowser(clock, [ANON_COOKIES])
        install_fake_playwright(monkeypatch, browser)
        profile = live_profile(tmp_path)
        monkeypatch.setattr(auth, "browser_profile_path", lambda: profile)
        http = auth_http()
        http.cookies.set(SESSION_COOKIE, LIVE_SESSION, domain="www.instahyre.com")

        out = lifecycle.reauth(http=http, store=store, profile_dir=profile)

        assert out["renewed"] is False, (
            "a harvested sessionid was treated as a renewal -- it is a reason to "
            "ASK, never an answer"
        )
        assert out["authenticated"] is False
        assert store.path.read_bytes() == before_bytes, (
            "a failed renew overwrote the saved session; it must be put back "
            "byte for byte"
        )
        assert http.cookies.get(SESSION_COOKIE) == LIVE_SESSION, (
            "the client's working cookie was replaced by an anonymous one"
        )
        assert out["previous_credential_restored"] is True

    def test_the_restore_really_runs_and_is_not_merely_a_no_op(
        self, tmp_path, monkeypatch
    ):
        """The seam is made to WRITE before it fails, so the restore has work.

        Without this the byte-identity assertion above would pass on a build
        with no restore at all, because ``login_via_browser`` happens not to
        write on its own failure path. A guarantee that holds only because the
        callee currently behaves is not a guarantee.
        """
        store = saved_store(tmp_path)
        before_bytes = store.path.read_bytes()
        profile = live_profile(tmp_path)
        http = auth_http()
        http.cookies.set(SESSION_COOKIE, LIVE_SESSION, domain="www.instahyre.com")

        def vandalising_seam(client, session_store, **kwargs):
            session_store.path.write_text("{}", encoding="utf-8")
            client.cookies.clear()
            client.cookies.set(SESSION_COOKIE, ANON_SESSION, domain="www.instahyre.com")
            return {"authenticated": False, "reason": "no session appeared"}

        monkeypatch.setattr(lifecycle, "login_via_browser", vandalising_seam)

        out = lifecycle.reauth(http=http, store=store, profile_dir=profile)

        assert out["renewed"] is False
        assert store.path.read_bytes() == before_bytes
        assert http.cookies.get(SESSION_COOKIE) == LIVE_SESSION

    def test_a_failed_renew_leaves_no_file_where_there_was_none(
        self, tmp_path, monkeypatch, clock
    ):
        """Restoring "nothing" is a delete, not a shrug."""
        store = SessionStore(tmp_path / "session.json")
        browser = FakeBrowser(clock, [ANON_COOKIES])
        install_fake_playwright(monkeypatch, browser)
        profile = live_profile(tmp_path)
        monkeypatch.setattr(auth, "browser_profile_path", lambda: profile)

        out = lifecycle.reauth(http=auth_http(), store=store, profile_dir=profile)

        assert out["renewed"] is False
        assert not store.path.exists()

    def test_an_unexpected_exception_still_puts_the_snapshot_back(
        self, tmp_path, monkeypatch
    ):
        store = saved_store(tmp_path)
        before_bytes = store.path.read_bytes()

        def exploding_seam(client, session_store, **kwargs):
            session_store.path.write_text("{}", encoding="utf-8")
            raise ValueError("a real bug, not a login failure")

        monkeypatch.setattr(lifecycle, "login_via_browser", exploding_seam)

        with pytest.raises(ValueError):
            lifecycle.reauth(
                http=auth_http(), store=store, profile_dir=tmp_path / "profile"
            )

        assert store.path.read_bytes() == before_bytes

    def test_it_never_opens_a_visible_window_and_never_waits_for_a_human(
        self, tmp_path, monkeypatch, clock
    ):
        """The two properties that stop this becoming a login in disguise.

        ``headless=True`` means there is nowhere for a human to type, and a
        bounded wait means nothing is waiting for one to try. Asserted at the
        launch call, which is where a regression would actually show up.
        """
        browser = FakeBrowser(clock, [LIVE_COOKIES])
        install_fake_playwright(monkeypatch, browser)
        profile = live_profile(tmp_path)
        monkeypatch.setattr(auth, "browser_profile_path", lambda: profile)

        lifecycle.reauth(
            http=auth_http(),
            store=SessionStore(tmp_path / "session.json"),
            profile_dir=profile,
        )

        assert browser.launch_kwargs.get("headless") is True, (
            "reauth opened a VISIBLE window: %r" % (browser.launch_kwargs,)
        )
        assert lifecycle.REAUTH_WAIT_S <= 30, (
            "a long wait is a wait for a human, and there is no window for one "
            "to act in"
        )

    def test_it_takes_no_credential_parameter_at_all(self):
        """Not "does not use one" -- cannot be handed one."""
        import asyncio

        tools = asyncio.run(server_module.mcp.list_tools())
        tool = next(t for t in tools if t.name == "instahyre_reauth")
        assert tool.parameters.get("properties", {}) == {}

    def test_a_missing_playwright_is_a_reason_not_a_traceback(
        self, tmp_path, monkeypatch
    ):
        """"I could not renew, run this instead" IS the useful answer here."""
        monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
        store = saved_store(tmp_path)
        before_bytes = store.path.read_bytes()

        out = lifecycle.reauth(
            http=auth_http(), store=store, profile_dir=live_profile(tmp_path)
        )

        assert out["renewed"] is False
        assert out["authenticated"] is None, (
            "a browser that could not start is not Instahyre saying no"
        )
        assert "instahyre_login_browser" in out["reason"]
        assert store.path.read_bytes() == before_bytes

    def test_the_failure_reason_names_the_fallback_tool(
        self, tmp_path, monkeypatch, clock
    ):
        browser = FakeBrowser(clock, [ANON_COOKIES])
        install_fake_playwright(monkeypatch, browser)
        profile = live_profile(tmp_path)
        monkeypatch.setattr(auth, "browser_profile_path", lambda: profile)

        out = lifecycle.reauth(
            http=auth_http(),
            store=SessionStore(tmp_path / "session.json"),
            profile_dir=profile,
        )

        assert "instahyre_login_browser" in out["reason"]
        assert "put back" in out["reason"]

    def test_the_result_carries_the_credential_block_and_no_value(
        self, tmp_path, monkeypatch, clock
    ):
        browser = FakeBrowser(clock, [LIVE_COOKIES])
        install_fake_playwright(monkeypatch, browser)
        profile = live_profile(tmp_path)
        monkeypatch.setattr(auth, "browser_profile_path", lambda: profile)

        out = lifecycle.reauth(
            http=auth_http(),
            store=SessionStore(tmp_path / "session.json"),
            profile_dir=profile,
        )

        assert out["credential"]["name"] == SESSION_COOKIE
        assert out["credential"]["present"] is True
        assert out["method"]
        assert_no_secret(out)
        assert LIVE_SESSION not in json.dumps(out)


# ===========================================================================
# 4. logout
# ===========================================================================


class TestLogout:
    """Local only, contract-shaped, and it may never raise."""

    def test_it_clears_the_file_and_the_cookies_and_reports_the_contract_shape(
        self, tmp_path
    ):
        store = saved_store(tmp_path)
        http = auth_http()
        http.cookies.set(SESSION_COOKIE, LIVE_SESSION, domain="www.instahyre.com")

        out = lifecycle.logout(
            http=http, store=store, profile_dir=live_profile(tmp_path)
        )

        assert set(out) == {
            "cleared",
            "scope",
            "authenticated",
            "reason",
            "what_is_lost",
            "recover_by",
        }
        assert out["cleared"] is True
        assert out["authenticated"] is False
        assert not store.path.exists()
        assert http.cookies.get(SESSION_COOKIE) is None

    def test_the_false_is_justified_by_there_being_nothing_left_to_check_with(
        self, tmp_path
    ):
        """The ONE place a false needs no live check, and it says why."""
        out = lifecycle.logout(
            http=auth_http(),
            store=saved_store(tmp_path),
            profile_dir=live_profile(tmp_path),
        )
        assert "no credential is left" in out["reason"]
        assert "nothing left to check with" in out["reason"]

    def test_cleared_is_false_when_there_was_nothing_to_clear(self, tmp_path):
        out = lifecycle.logout(
            http=make_http({}),
            store=SessionStore(tmp_path / "session.json"),
            profile_dir=live_profile(tmp_path),
        )
        assert out["cleared"] is False
        assert out["authenticated"] is False

    def test_recover_by_names_reauth_FIRST_because_the_profile_was_not_touched(
        self, tmp_path
    ):
        out = lifecycle.logout(
            http=auth_http(),
            store=saved_store(tmp_path),
            profile_dir=live_profile(tmp_path),
        )
        recover = out["recover_by"]
        assert "instahyre_reauth" in recover
        assert "instahyre_login_browser" in recover
        assert recover.index("instahyre_reauth") < recover.index(
            "instahyre_login_browser"
        )

    def test_the_browser_profile_survives_a_logout(self, tmp_path):
        profile = live_profile(tmp_path)
        jar = profile / "Default" / "Network" / "Cookies"
        lifecycle.logout(
            http=auth_http(), store=saved_store(tmp_path), profile_dir=profile
        )
        assert jar.is_file(), "logout deleted the browser profile's cookie jar"
        assert "is NOT touched" in out_scope(tmp_path, profile)

    def test_the_scope_says_what_was_not_ended(self, tmp_path):
        out = lifecycle.logout(
            http=auth_http(),
            store=saved_store(tmp_path),
            profile_dir=live_profile(tmp_path),
        )
        assert "not ended" in out["scope"]
        assert "session.json" in out["scope"]

    def test_it_never_raises_even_when_the_file_cannot_be_removed(self, tmp_path):
        store = saved_store(tmp_path)

        def refuse():
            raise PermissionError("the file is locked")

        store.clear = refuse  # type: ignore[method-assign]

        out = lifecycle.logout(
            http=auth_http(), store=store, profile_dir=live_profile(tmp_path)
        )

        assert out["authenticated"] is False
        assert out["reason"].startswith("PARTIAL")
        assert "still usable" in out["reason"]

    def test_it_leaks_no_cookie_value(self, tmp_path):
        http = auth_http()
        http.cookies.set(SESSION_COOKIE, SECRET_SESSION_VALUE, domain="www.instahyre.com")
        out = lifecycle.logout(
            http=http, store=saved_store(tmp_path), profile_dir=live_profile(tmp_path)
        )
        assert_no_secret(out)


def out_scope(tmp_path: Path, profile: Path) -> str:
    """The scope sentence, recomputed for the assertion above."""
    return lifecycle.logout(
        http=auth_http(),
        store=SessionStore(tmp_path / "session-2.json"),
        profile_dir=profile,
    )["scope"]


# ===========================================================================
# 5. The modules themselves
# ===========================================================================


def test_the_lifecycle_source_is_strict_ascii():
    raw = (Path(lifecycle.__file__)).read_bytes()
    assert all(b < 128 for b in raw), "non-ASCII byte in lifecycle.py"


def test_the_cookie_jar_source_is_strict_ascii():
    raw = (Path(cookie_jar.__file__)).read_bytes()
    assert all(b < 128 for b in raw), "non-ASCII byte in cookie_jar.py"


def test_the_offline_profile_path_is_not_created_by_asking_for_it(
    isolated_state_home, monkeypatch
):
    """``session_info`` must not conjure a profile directory in order to
    report that there is not one -- the reader's message would flip from
    "never signed in" to "there but empty", which reads like corruption."""
    from instahyre_server.session import browser_profile_path

    path = browser_profile_path(create=False)
    assert not path.exists()
    monkeypatch.setattr(server_module, "_client", None)
    monkeypatch.setattr(server_module, "_sessions", None)
    server_module.instahyre_session_info(verify_live=False)
    assert not path.exists()
