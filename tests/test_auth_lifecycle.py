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

import hashlib
import json
import logging
import sqlite3
import sys
import time
import types
from pathlib import Path

import pytest

from conftest import (
    FakeClock,
    HTML_CHALLENGE_BODY,
    assert_no_credential,
    html_response,
    json_response,
    make_http,
)
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

#: Values that must never appear in a tool result, a log line or an error.
#:
#: CREDENTIAL-SHAPED AND CREDENTIAL-LENGTH, deliberately: 32 lowercase
#: alphanumerics for the session id and 64 mixed-case for the csrftoken, which
#: is what Django hands out and what was measured in the live jar. They used to
#: be hyphenated 45-character sentences, and that made the guard look stronger
#: than it was -- a marker whose shape no real credential wears cannot exercise
#: the shape scan at all, and one shorter than the truncation window survives
#: truncation whole and passes for a reason that has nothing to do with the
#: leak. They are still unmistakable: 32 alphanumerics have on the order of
#: 36**32 spellings, so a hit is never an accident.
SECRET_SESSION_VALUE = "secretsessionidvalue0must0never0"
SECRET_CSRF_VALUE = "SECRETcsrftokenValueMustNeverBeReturned0000000000000000000000000"
SECRET_ENCRYPTED_BLOB = b"secretencryptedblob0must0never00"


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
    # Rebuilt from scratch every time. A test that builds two profiles under
    # one tmp_path would otherwise hit "table cookies already exists", which
    # is a defect in the harness masquerading as a defect in the reader.
    if jar.exists():
        jar.unlink()
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
    """No cookie value, in any spelling, anywhere in ``payload``.

    Delegates to the shared walker in ``conftest.py``. It used to be a local
    substring search over a str-only walk, and on 2026-08-23 that combination
    was measured blind to six of eight leak shapes carrying the whole cookie --
    bytes, an object's repr, a set, base64, and both spellings of the sealed
    blob. ``test_credential_leak.py`` holds the controls.

    ``SECRET_ENCRYPTED_BLOB`` is in this list, and that is the point of the
    change. ``build_jar`` has always planted it in every row so that a reader
    which selected a wildcard would be caught -- and nothing had ever hunted
    it, so the trap was set and the alarm was disconnected. It is bytes, so it
    also needed the wider walk before naming it here would have meant anything.
    """
    assert_no_credential(
        payload,
        SECRET_SESSION_VALUE,
        SECRET_CSRF_VALUE,
        SECRET_ENCRYPTED_BLOB,
    )


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
        # RE-RATIFIED 2026-08-25, deliberately, from `is True` to `is False`.
        #
        # WHAT CHANGED: nothing about the session mechanism. The field was a
        # hardcoded literal that answered the wrong question. It sits in the
        # block describing the credential the client SENDS -- read out of
        # session.json -- while the date beside it is read from the browser
        # profile's jar. A True there told a consumer to plan a re-login off
        # a date that describes a DIFFERENT store.
        #
        # WHY IT CANNOT BE TRUE, rather than merely being unproven today:
        # establishing that the two stores hold one session would mean
        # comparing the cookie values. Chrome seals jar values under v10 --
        # AES-256-GCM, fresh random nonce per write -- so the same sessionid
        # seals to different bytes and a hash of the sealed blobs would call
        # two identical sessions different. A real comparison needs the
        # plaintext, which is the one thing cookie_jar.py exists never to
        # fetch. Measured on the live profile jar 2026-08-25: 57 rows, 0
        # bytes of plaintext value, every value sealed, scheme tag v10.
        #
        # So False is not a downgrade pending better evidence. It is the
        # answer. TestTheExpiryIsNotAuthoritativeForTheSentCredential below
        # owns the full case; this line is the point of use.
        assert credential["expiry_is_authoritative"] is False
        assert credential["expiry_authoritative_for"], (
            "a false must still say what the date IS good for"
        )

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
        """Over the RECORDS, not over ``caplog.text``.

        ``caplog.text`` is the rendered form, and a value passed as a logging
        ARG rather than interpolated into the message reaches every handler
        while never appearing in a text search of the format string -- a
        ``LogRecord``'s own repr shows ``"value: %s"`` and hides the arg. The
        control for that is in ``test_credential_leak.py``; this call site is
        one of the ones it protects.
        """
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
        assert_no_secret(list(caplog.records))

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


class TestWhenTheSessionLapsesForGood:
    """``session_lapses_at`` -- a different question from ``credential.expires_at``.

    The credential date says when THIS cookie dies. The lapse date says when no
    silent renew can help any more and a human must sign in. The contract gives
    them separate keys because on naukri they are five orders of magnitude
    apart. On instahyre they coincide, which is exactly why the field has to be
    explicit: a reader who inferred the rule from this server alone would infer
    the wrong rule.
    """

    def test_the_three_keys_are_present_and_shaped(self, tmp_path):
        out = lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=live_profile(tmp_path),
            http=None,
            verify_live=False,
        )
        renewal = out["renewal"]
        assert set(renewal) == {
            "silent_renew_available",
            "tool",
            "why",
            "uses_browser",
            # ADDED 2026-08-25. uses_browser: true is read as "a window
            # opens" in a contract spelled the same way on four servers,
            # and here nothing opens and no human could act if it did.
            # The qualifier lives in fields a client can branch on, not
            # only in the mechanism prose beside them.
            "opens_a_window",
            "waits_for_a_human",
            "mechanism",
            # ADDED 2026-08-25: the one field of the contract's five that
            # this block never carried.
            "expiry_is_authoritative",
            "session_lapses_at",
            "session_lapses_in_days",
            "session_lapses_source",
        }
        assert renewal["session_lapses_at"].endswith("Z")
        assert isinstance(renewal["session_lapses_in_days"], float)

    def test_it_equals_the_credential_date_because_it_is_the_same_row(
        self, tmp_path
    ):
        """Reused, not re-read. Two reads of one row can disagree, and a
        payload whose own two dates contradict each other is worse than one
        carrying neither."""
        out = lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=live_profile(tmp_path),
            http=None,
            verify_live=False,
        )
        assert out["renewal"]["session_lapses_at"] == out["credential"]["expires_at"]
        assert (
            out["renewal"]["session_lapses_in_days"]
            == out["credential"]["expires_in_days"]
        )

    def test_an_unreadable_jar_leaves_both_dates_null_and_says_why(self, tmp_path):
        """Never a false, never a zero -- the same rule as ``expired``."""
        out = lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=tmp_path / "no_such_profile",
            http=None,
            verify_live=False,
        )
        renewal = out["renewal"]
        assert renewal["session_lapses_at"] is None
        assert renewal["session_lapses_in_days"] is None
        assert renewal["session_lapses_source"].startswith("unknown")
        assert "could not be read" in renewal["session_lapses_source"]
        assert renewal["silent_renew_available"] is True, (
            "an unreadable jar says nothing about whether a renew is possible"
        )

    def test_the_source_names_the_governing_credential_and_the_dependency(
        self, tmp_path
    ):
        out = lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=live_profile(tmp_path),
            http=None,
            verify_live=False,
        )
        source = out["renewal"]["session_lapses_source"]
        assert "sessionid" in source
        assert "instahyre_reauth" in source
        assert "instahyre_login_browser" in source
        assert "PROFILE" in source, "the two-stores caveat did not carry forward"
        assert len(source) > 200

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
            # ADDED 2026-08-25 with the re-ratification above. Turning
            # expiry_is_authoritative into a computed false left a reader
            # holding a negative and nothing else; this names the store the
            # date IS authoritative for, so the false points somewhere
            # instead of just closing a door.
            "expiry_authoritative_for",
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

        def vandalising_seam(client, session_store):
            session_store.path.write_text("{}", encoding="utf-8")
            client.cookies.clear()
            client.cookies.set(SESSION_COOKIE, ANON_SESSION, domain="www.instahyre.com")
            return {
                "authenticated": False,
                "outcome": "endpoint_said_no",
                "reason": "no session appeared.",
            }

        monkeypatch.setattr(lifecycle, "reharvest_from_profile", vandalising_seam)

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

        def exploding_seam(client, session_store):
            session_store.path.write_text("{}", encoding="utf-8")
            raise ValueError("a real bug, not a login failure")

        monkeypatch.setattr(lifecycle, "reharvest_from_profile", exploding_seam)

        with pytest.raises(ValueError):
            lifecycle.reauth(
                http=auth_http(), store=store, profile_dir=tmp_path / "profile"
            )

        assert store.path.read_bytes() == before_bytes

    def test_it_never_opens_a_visible_window(self, tmp_path, monkeypatch, clock):
        """Headless is the guarantee: there is nowhere for a human to type."""
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

    def test_it_never_fetches_the_login_page__RULING(
        self, tmp_path, monkeypatch, clock
    ):
        """Wave lead, 2026-08-23, overturning the first draft of this slice.

        The first version drove ``login_via_browser(headless=True)``, which was
        safe by every test in this file and still navigated to ``/login/`` to
        do it. Two things were wrong with that. A tool whose entire claim is
        "this is not a login" should not fetch the login URL -- that gap
        between what a tool says and what it does is one this codebase has paid
        for before. And sending a browser that is carrying a LIVE session to a
        sign-in page is a needless risk against the operator's one real
        profile.

        The URL is asserted directly, because that is the only place the
        difference is visible.
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

        assert browser.goto_urls == [auth.HOME_URL]
        assert auth.HOME_URL.endswith("/candidate/opportunities/")
        assert not any("login" in url for url in browser.goto_urls), (
            "a silent renew navigated to a sign-in page: %r" % (browser.goto_urls,)
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


class TestReauthSaysWhichFailureItWas:
    """One reason per distinct outcome, because they call for different moves.

    A bare "it did not work" collapses five different problems -- install
    Playwright, sign in once so a profile exists, look at a browser that would
    not start, sign in again because the profile went stale, wait out a
    Cloudflare challenge -- into one shrug. The contract gives ``reason`` a
    field of its own precisely so that it does not have to, and an empty or
    generic reason is the regression these tests exist to catch.
    """

    def run(self, tmp_path, monkeypatch, profile, http=None, store=None):
        store = store or SessionStore(tmp_path / "session.json")
        monkeypatch.setattr(auth, "browser_profile_path", lambda: profile)
        return lifecycle.reauth(
            http=http if http is not None else auth_http(),
            store=store,
            profile_dir=profile,
        )

    def test_playwright_missing(self, tmp_path, monkeypatch):
        monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
        out = self.run(tmp_path, monkeypatch, live_profile(tmp_path))

        assert out["outcome"] == "playwright_missing"
        assert out["authenticated"] is None
        assert "playwright install chromium" in out["reason"]

    def test_playwright_missing_does_not_create_a_browser_profile(
        self, tmp_path, monkeypatch, isolated_state_home
    ):
        """A box with no browser must not acquire a profile just for asking."""
        from instahyre_server.session import browser_profile_path

        monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
        path = browser_profile_path(create=False)
        assert not path.exists()

        record = auth.reharvest_from_profile(auth_http(), SessionStore(tmp_path / "s.json"))

        assert record["outcome"] == "playwright_missing"
        assert not path.exists(), "the no-browser path created a browser profile"

    def test_no_profile(self, tmp_path, monkeypatch, clock):
        install_fake_playwright(monkeypatch, FakeBrowser(clock, [LIVE_COOKIES]))
        empty = tmp_path / "browser_profile"
        empty.mkdir()

        out = self.run(tmp_path, monkeypatch, empty)

        assert out["outcome"] == "no_profile"
        assert out["authenticated"] is None
        assert "nothing has ever signed in there" in out["reason"]
        assert str(tmp_path) not in out["reason"], "the reason leaked a local path"

    def test_browser_failed_carries_the_exception_text(
        self, tmp_path, monkeypatch, clock
    ):
        browser = FakeBrowser(
            clock, [LIVE_COOKIES], goto_raises=RuntimeError("chromium would not start")
        )
        install_fake_playwright(monkeypatch, browser)

        out = self.run(tmp_path, monkeypatch, live_profile(tmp_path))

        assert out["outcome"] == "browser_failed"
        assert out["authenticated"] is None
        assert "chromium would not start" in out["reason"]
        assert "RuntimeError" in out["reason"]

    def test_no_session_cookie_harvested(self, tmp_path, monkeypatch, clock):
        """The profile opened and handed over nothing worth asking about."""
        install_fake_playwright(
            monkeypatch, FakeBrowser(clock, [{CSRF_COOKIE: "csrf-only"}])
        )

        out = self.run(tmp_path, monkeypatch, live_profile(tmp_path))

        assert out["outcome"] == "no_session_cookie"
        assert out["authenticated"] is None, (
            "no request was made, so there is no verdict to report -- null, "
            "not false"
        )
        assert "handed over no sessionid cookie" in out["reason"]

    def test_endpoint_said_no(self, tmp_path, monkeypatch, clock):
        install_fake_playwright(monkeypatch, FakeBrowser(clock, [ANON_COOKIES]))

        out = self.run(tmp_path, monkeypatch, live_profile(tmp_path))

        assert out["outcome"] == "endpoint_said_no"
        assert out["authenticated"] is False, "the endpoint DID answer, and it said no"
        assert "answered 401" in out["reason"]

    def test_endpoint_inconclusive_is_null_not_false__HONESTY(
        self, tmp_path, monkeypatch, clock
    ):
        """A Cloudflare challenge during a renew is not a logged-out verdict."""
        install_fake_playwright(monkeypatch, FakeBrowser(clock, [LIVE_COOKIES]))
        challenged = make_http({CATEGORY: html_response(HTML_CHALLENGE_BODY, status=403)})

        out = self.run(tmp_path, monkeypatch, live_profile(tmp_path), http=challenged)

        assert out["outcome"] == "endpoint_inconclusive"
        assert out["authenticated"] is None, "unknown must not collapse into a no"
        assert out["renewed"] is False
        assert "UNKNOWN" in out["reason"]
        assert "not Instahyre saying no" in out["reason"]

    def test_every_failure_names_the_fallback_and_never_returns_an_empty_reason(
        self, tmp_path, monkeypatch, clock
    ):
        """The whole table at once, so a new outcome cannot ship reasonless.

        Every branch is exercised in one test as well as individually, because
        an outcome added next month gets a reason only if something walks the
        set. ``REHARVEST_OUTCOMES`` is the set, and it is asserted covered.
        """
        seen = {}
        cases = {
            "no_profile": lambda: (tmp_path / "empty_profile", None, None),
            "no_session_cookie": lambda: (
                live_profile(tmp_path),
                {CSRF_COOKIE: "csrf-only"},
                None,
            ),
            "endpoint_said_no": lambda: (live_profile(tmp_path), ANON_COOKIES, None),
            "endpoint_inconclusive": lambda: (
                live_profile(tmp_path),
                LIVE_COOKIES,
                make_http({CATEGORY: html_response(HTML_CHALLENGE_BODY, status=403)}),
            ),
            "browser_failed": lambda: (live_profile(tmp_path), "boom", None),
        }
        (tmp_path / "empty_profile").mkdir(exist_ok=True)

        for name, build in cases.items():
            profile, cookies, http = build()
            browser = FakeBrowser(
                clock,
                [cookies] if isinstance(cookies, dict) else [LIVE_COOKIES],
                goto_raises=RuntimeError("boom") if cookies == "boom" else None,
            )
            install_fake_playwright(monkeypatch, browser)
            out = self.run(tmp_path, monkeypatch, profile, http=http)
            seen[name] = out

        for name, out in seen.items():
            assert out["outcome"] == name, "%s produced %r" % (name, out["outcome"])
            assert out["renewed"] is False
            assert out["reason"].strip(), "%s shipped an empty reason" % name
            assert "instahyre_login_browser" in out["reason"], (
                "%s does not name the fallback" % name
            )
            assert "put back exactly as it was" in out["reason"]
            assert out["stage"] == "profile_reharvest"

        # playwright_missing and renewed are covered by their own tests above;
        # asserting the union here is what makes this table exhaustive.
        assert set(seen) | {"playwright_missing", "renewed"} == set(
            auth.REHARVEST_OUTCOMES
        ), "an outcome exists that nothing in this file exercises"


class TestARenewIsSilentNotFree:
    """``silent`` means no human. It does not mean no cost, and it must say so.

    A reauth launches a headless Chromium, loads a page and spends seconds of
    wall clock. A payload that reported only the verdict would let all of that
    happen unannounced, which is the same defect as any other unmentioned
    expense -- it just wears better clothes because the window never appears.

    Both surfaces carry the keys, and they are built from ONE definition:
    written twice, the two descriptions of one mechanism would drift and the
    one a reader happened to open would be the stale one.
    """

    def expected_claims(self, text):
        return {
            "headless chromium": "headless chromium" in text.lower(),
            "the profile path": "browser_profile" in text,
            "opportunities page": "opportunities page" in text,
            "storage state": "storage state" in text,
            "put to the endpoint": "PUT TO" in text,
            "costs seconds": "seconds of wall clock" in text,
            "not free": "NOT mean free" in text,
            "no password/window/human": "no password, no window and no human"
            in text,
        }

    def test_session_info_says_a_renew_uses_a_browser_and_what_it_costs(
        self, tmp_path
    ):
        out = lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=live_profile(tmp_path),
            http=None,
            verify_live=False,
        )
        renewal = out["renewal"]
        assert renewal["uses_browser"] is True
        assert renewal["mechanism"].strip()
        missing = [k for k, ok in self.expected_claims(renewal["mechanism"]).items() if not ok]
        assert not missing, "mechanism does not state: %s" % ", ".join(missing)

    def test_reauth_says_the_same_thing_in_the_same_words(
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

        assert out["uses_browser"] is True
        assert out["mechanism"].strip()
        missing = [k for k, ok in self.expected_claims(out["mechanism"]).items() if not ok]
        assert not missing, "mechanism does not state: %s" % ", ".join(missing)

    def test_the_two_surfaces_cannot_drift(self, tmp_path, monkeypatch, clock):
        """Same profile in, same sentence out -- because it is one function."""
        profile = live_profile(tmp_path)
        browser = FakeBrowser(clock, [LIVE_COOKIES])
        install_fake_playwright(monkeypatch, browser)
        monkeypatch.setattr(auth, "browser_profile_path", lambda: profile)

        info = lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=profile,
            http=None,
            verify_live=False,
        )
        renewed = lifecycle.reauth(
            http=auth_http(),
            store=SessionStore(tmp_path / "session.json"),
            profile_dir=profile,
        )

        assert info["renewal"]["mechanism"] == renewed["mechanism"]

    def test_the_cost_disclosure_leaks_no_absolute_path(self, tmp_path):
        out = lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=live_profile(tmp_path),
            http=None,
            verify_live=False,
        )
        assert str(tmp_path) not in out["renewal"]["mechanism"]


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
            "problems",
            "what_is_lost",
            "recover_by",
        }
        assert out["cleared"] is True
        assert out["authenticated"] is False
        assert out["problems"] == []
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

    def test_a_partial_clear_is_null_not_false__HONESTY(self, tmp_path):
        """Wave lead ruling, 2026-08-23, taken across all four servers.

        The first draft returned ``false`` here beside a reason that said
        "treat the credential as present until this is fixed". The prose was
        right and the field contradicted it in the same object: something
        survived the clear, so an authenticated request may still be possible,
        and nobody measured whether it is.

        ``is None`` and not a falsy check, deliberately -- ``False`` passes a
        falsy check, and ``False`` is precisely the bug.
        """
        store = saved_store(tmp_path)

        def refuse():
            raise PermissionError("the file is locked")

        store.clear = refuse  # type: ignore[method-assign]

        out = lifecycle.logout(
            http=auth_http(), store=store, profile_dir=live_profile(tmp_path)
        )

        assert out["authenticated"] is None, (
            "a clear that failed cannot prove the credential is gone"
        )
        assert out["reason"].startswith("PARTIAL")
        assert "still usable" in out["reason"]
        assert out["problems"], "the failure is reported in prose only"
        assert "could not be removed" in out["problems"][0]

    def test_a_clean_clear_is_still_a_provable_false(self, tmp_path):
        """The null is for the partial case ONLY. A clear that worked really
        does leave nothing to make a request with, and that false is the one
        the contract blesses."""
        out = lifecycle.logout(
            http=auth_http(),
            store=saved_store(tmp_path),
            profile_dir=live_profile(tmp_path),
        )
        assert out["authenticated"] is False
        assert out["problems"] == []

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


# ===========================================================================
# 5. The expiry authority, and the divergence check that cannot exist
# ===========================================================================


class TestTheExpiryIsNotAuthoritativeForTheSentCredential:
    """``expiry_is_authoritative`` shipped as a hardcoded ``True`` and was wrong.

    THE DEFECT, precisely: the field sits inside ``credential``, which describes
    the cookie the HTTP client SENDS. That cookie is read from session.json.
    The date beside it is read from the browser profile's SQLite jar. Those are
    two stores. The prose in ``expiry_source`` always said so, and the comment
    over the literal was honest about it -- "authoritative FOR THE PROFILE IT
    WAS READ FROM" -- but a consumer reads fields, not comments, and an
    unqualified true in that block means "you may plan a re-login off this
    date".

    WHY THE FIX IS A CONSTANT FALSE RATHER THAN A COMPUTED SOMETIMES-TRUE. The
    obvious repair is to compare the two stores and report true when they
    agree. That comparison is not available at any price worth paying:

    * Chrome keeps no plaintext in the jar. Measured against the operator's
      live profile 2026-08-25: 57 rows, 0 bytes of plaintext ``value``, every
      value in ``encrypted_value``, scheme tag ``v10``.
    * ``v10`` is AES-256-GCM with a fresh random nonce per write, so the SAME
      sessionid sealed twice produces different bytes. A digest of the sealed
      blob would report two identical sessions as DIFFERENT -- a check whose
      failures are indistinguishable from its successes.
    * A digest that did work would need the plaintext, and fetching a cookie
      value is the one thing ``cookie_jar.py`` is built never to do. The guard
      is a test, not a convention: ``"value" not in cookie_jar._JAR_QUERY``.

    So sameness is not unmeasured here. It is UNMEASURABLE without reading the
    secret, and the field says false because false is the true answer.
    """

    def test_a_date_that_exists_is_not_authoritative_for_the_cookie_in_use(
        self, tmp_path
    ):
        out = lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=live_profile(tmp_path),
            http=None,
            verify_live=False,
        )
        credential = out["credential"]
        assert credential["expires_at"] is not None
        assert credential["expiry_is_authoritative"] is False

    def test_the_false_names_what_the_date_IS_authoritative_for(self, tmp_path):
        """A bare false is a dead end. This one has to point somewhere."""
        out = lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=live_profile(tmp_path),
            http=None,
            verify_live=False,
        )
        says = out["credential"]["expiry_authoritative_for"]
        assert "browser profile" in says
        assert "sessionid" in says
        assert "session.json" in says
        assert "instahyre_auth_status" in says, (
            "a reader told not to trust the date must be told what to trust"
        )

    def test_no_date_means_NULL_not_false__HONESTY(self, tmp_path):
        """The contract forbids ``false`` as a stand-in for "unknown".

        With no date read there is no subject to make a claim about, so both
        authority fields are null. A false here would read as "the date is not
        authoritative", which asserts that a date exists.
        """
        out = lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=tmp_path / "no_such_profile",
            http=None,
            verify_live=False,
        )
        credential = out["credential"]
        assert credential["expires_at"] is None
        assert credential["expiry_is_authoritative"] is None
        assert credential["expiry_authoritative_for"] is None

    def test_a_session_only_row_is_also_null(self, tmp_path):
        """The signed-out visitor's cookie: a row with no date. Same rule."""
        profile = tmp_path / "browser_profile"
        build_jar(profile, [(".instahyre.com", SESSION_COOKIE, 0, 0, 0)])
        out = lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=profile,
            http=None,
            verify_live=False,
        )
        assert out["credential"]["expires_at"] is None
        assert out["credential"]["expiry_is_authoritative"] is None

    def test_the_field_is_computed_and_not_a_literal_again(self):
        """The regression that matters is someone pinning it back to a constant."""
        source = Path(lifecycle.__file__).read_text(encoding="utf-8")
        assert '"expiry_is_authoritative": True' not in source, (
            "the hardcoded True is back -- it answers for the wrong credential"
        )
        assert "_expiry_authority" in source

    def test_the_prose_carries_the_MEASURED_reason(self, tmp_path):
        """It used to say comparing "would mean reading a cookie value" and stop.

        That was true and read like an unexamined limit, which is how it came to
        be mistaken for one. The reason is now named, so the next reader who
        reaches for a hash finds out why it does not work before writing it.
        """
        out = lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=live_profile(tmp_path),
            http=None,
            verify_live=False,
        )
        source = out["credential"]["expiry_source"]
        assert "v10" in source
        assert "AES-256-GCM" in source
        assert "HASH" in source
        assert "nonce" in source

    def test_no_divergence_BOOLEAN_was_invented(self, tmp_path):
        """The field that must never appear, pinned by name.

        A ``same_session_in_both_stores`` was specified and refused on
        2026-08-25 once the jar was measured: it cannot be computed without the
        plaintext, and any version built from sealed bytes reports identical
        sessions as different. Pinned here because the idea is a reasonable one
        to have twice.
        """
        out = lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=live_profile(tmp_path),
            http=None,
            verify_live=False,
        )
        keys = [trail for trail, _ in strings_in(out) if trail.endswith("(KEY)")]
        for banned in ("same_session_in_both_stores", "session_digest", "cookie_hash"):
            assert not any(banned in key for key in keys), (
                "%s cannot be computed from a v10-sealed jar" % banned
            )


class TestNoDigestOfACookieEverReachesTheOutput:
    """The hash that was almost built must not be reachable, even in part.

    ``assert_no_secret`` already hunts the cookie VALUE in eight shapes. This
    adds the shape that a divergence check would have introduced: a digest, or
    any prefix of one. A digest is not a value, which is exactly why it would
    have slipped past a value-only guard -- and a digest of a 32-character
    Django sessionid is brute-forceable, so it is a leak, not a summary.
    """

    def digests_of(self, *values):
        out = []
        for value in values:
            raw = value.encode("utf-8")
            for algo in ("md5", "sha1", "sha256", "sha512"):
                digest = hashlib.new(algo, raw).hexdigest()
                out.append((algo, digest))
                out.append((algo + "[:16]", digest[:16]))
                out.append((algo + "[:8]", digest[:8]))
        return out

    def payloads(self, tmp_path):
        yield "session_info", lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=live_profile(tmp_path),
            http=None,
            verify_live=False,
        )
        yield "session_info/no_jar", lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=tmp_path / "no_such_profile",
            http=None,
            verify_live=False,
        )
        logout_dir = tmp_path / "logout_case"
        logout_dir.mkdir(exist_ok=True)
        yield "logout", lifecycle.logout(
            http=auth_http(),
            store=saved_store(logout_dir),
            profile_dir=live_profile(tmp_path),
        )

    def test_no_digest_of_either_cookie_appears_anywhere(self, tmp_path):
        for name, payload in self.payloads(tmp_path):
            assert_no_secret(payload)
            haystack = "\n".join(text for _, text in strings_in(payload)).lower()
            for algo, digest in self.digests_of(
                SECRET_SESSION_VALUE, SECRET_CSRF_VALUE
            ):
                assert digest.lower() not in haystack, (
                    "%s leaked a %s digest of a cookie value" % (name, algo)
                )

    def test_the_control_can_actually_fail(self, tmp_path):
        """The guard above, shown catching a planted digest.

        Without this, "no digest appeared" is indistinguishable from "the
        walker never looked".
        """
        planted = {
            "credential": {
                "expiry_source": "divergence: %s"
                % hashlib.sha256(SECRET_SESSION_VALUE.encode()).hexdigest()[:16]
            }
        }
        haystack = "\n".join(text for _, text in strings_in(planted)).lower()
        hits = [
            algo
            for algo, digest in self.digests_of(SECRET_SESSION_VALUE)
            if digest.lower() in haystack
        ]
        assert hits, "the digest hunt cannot fail, so it certifies nothing"


class TestUsesBrowserCannotSilentlyBecomeAWindowOpeningClaim:
    """``uses_browser: true`` is the field a client branches on. Here it lies.

    Not about this server -- about the FAMILY. The same key is spelled the same
    way on four servers, and on the ones that open a real sign-in window a true
    correctly means "hand this to the human". Here nothing appears, and no human
    could act even if they wanted to: reauth runs headless, takes no credential
    parameter and never visits a sign-in page, so there is nowhere for a person
    to type anything. A client that put up a "sign in" prompt off the bare
    boolean would be waiting for an event that cannot happen.

    The mechanism prose has always said so. Prose is not branchable, which is
    why the qualifiers are fields.
    """

    def renewal_of(self, tmp_path):
        return lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=live_profile(tmp_path),
            http=None,
            verify_live=False,
        )["renewal"]

    def test_the_qualifiers_sit_beside_the_boolean(self, tmp_path):
        renewal = self.renewal_of(tmp_path)
        assert renewal["uses_browser"] is True
        assert renewal["opens_a_window"] is False
        assert renewal["waits_for_a_human"] is False

    def test_reauth_carries_them_too(self, tmp_path, monkeypatch, clock):
        """One mechanism described in two places must not say less in one."""
        browser = FakeBrowser(clock, [LIVE_COOKIES])
        install_fake_playwright(monkeypatch, browser)
        profile = live_profile(tmp_path)
        monkeypatch.setattr(auth, "browser_profile_path", lambda: profile)

        out = lifecycle.reauth(
            http=auth_http(),
            store=SessionStore(tmp_path / "session.json"),
            profile_dir=profile,
        )
        assert out["uses_browser"] is True
        assert out["opens_a_window"] is False
        assert out["waits_for_a_human"] is False

    def test_a_true_uses_browser_is_never_alone_in_any_payload(
        self, tmp_path, monkeypatch, clock
    ):
        """Structural, so a new surface cannot ship the bare boolean.

        Any block anywhere in either payload that says uses_browser: true must
        carry both qualifiers. This is the check that fails if someone adds a
        third surface and copies only the familiar key.
        """
        browser = FakeBrowser(clock, [LIVE_COOKIES])
        install_fake_playwright(monkeypatch, browser)
        profile = live_profile(tmp_path)
        monkeypatch.setattr(auth, "browser_profile_path", lambda: profile)

        payloads = [
            lifecycle.session_info(
                store=saved_store(tmp_path),
                profile_dir=profile,
                http=None,
                verify_live=False,
            ),
            lifecycle.reauth(
                http=auth_http(),
                store=SessionStore(tmp_path / "session.json"),
                profile_dir=profile,
            ),
        ]

        def blocks(node):
            if isinstance(node, dict):
                yield node
                for value in node.values():
                    yield from blocks(value)
            elif isinstance(node, (list, tuple)):
                for value in node:
                    yield from blocks(value)

        seen = 0
        for payload in payloads:
            for block in blocks(payload):
                if block.get("uses_browser") is not True:
                    continue
                seen += 1
                assert block.get("opens_a_window") is False, (
                    "uses_browser: true with no opens_a_window -- a client will "
                    "read it as a window"
                )
                assert block.get("waits_for_a_human") is False
        assert seen == 2, "expected both surfaces to claim a browser, saw %d" % seen

    def test_headless_is_still_what_the_code_does(self):
        """The fields claim no window. The source has to agree.

        A payload that says opens_a_window: false while the seam launches
        headed would be a worse lie than the bare boolean was.
        """
        source = Path(auth.__file__).read_text(encoding="utf-8")
        assert "headless=True" in source
        marker = "def reharvest_from_profile"
        assert marker in source
        body = source[source.index(marker) :]
        assert "headless=False" not in body[: body.index("\ndef ", 10)]


class TestTheRenewalBlockSaysWhyA200IsTheWholeTest:
    """The most useful sentence in the block, and it was missing.

    ``renewal.mechanism`` said only a 200 is believed. It never said WHY, and
    the why is the bug this server shipped: Instahyre hands a sessionid to
    signed-out visitors, so a fresh cookie is not evidence of a session. A
    reader who does not know that reads the 200 requirement as belt-and-braces
    caution rather than the entire test.
    """

    def test_the_signed_out_visitor_fact_is_in_the_block(self, tmp_path):
        out = lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=live_profile(tmp_path),
            http=None,
            verify_live=False,
        )
        mechanism = out["renewal"]["mechanism"]
        assert "signed-out visitors" in mechanism
        assert "200" in mechanism

    def test_it_is_the_ONE_spelling_not_a_paraphrase(self, tmp_path):
        """Interpolated from COOKIE_IS_NOT_A_SESSION, so it cannot drift."""
        out = lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=live_profile(tmp_path),
            http=None,
            verify_live=False,
        )
        assert lifecycle.COOKIE_IS_NOT_A_SESSION in out["renewal"]["mechanism"]

    def test_the_byte_for_byte_restore_is_surfaced_not_just_implemented(
        self, tmp_path
    ):
        """It was always true and never said. A caller weighing whether to try
        a renew needs to know the downside is zero."""
        out = lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=live_profile(tmp_path),
            http=None,
            verify_live=False,
        )
        mechanism = out["renewal"]["mechanism"]
        assert "byte for byte" in mechanism
        assert "snapshotted as BYTES" in mechanism

    def test_the_lapse_date_is_not_authoritative_either_and_says_why(
        self, tmp_path
    ):
        out = lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=live_profile(tmp_path),
            http=None,
            verify_live=False,
        )
        renewal = out["renewal"]
        assert renewal["expiry_is_authoritative"] is False
        source = renewal["session_lapses_source"]
        assert "expiry_is_authoritative" in source
        assert "snapshot" in source
        assert "never as a promise" in source

    def test_an_unreadable_jar_leaves_the_lapse_authority_null(self, tmp_path):
        out = lifecycle.session_info(
            store=saved_store(tmp_path),
            profile_dir=tmp_path / "no_such_profile",
            http=None,
            verify_live=False,
        )
        renewal = out["renewal"]
        assert renewal["session_lapses_at"] is None
        assert renewal["expiry_is_authoritative"] is None


class TestTheReadmeSaysHowTheWriteSurfaceIsCounted:
    """The one sentence a stranger has to be able to find.

    LIVES HERE because this slice added it, alongside the rest of the auth
    legibility work; it belongs with the write-surface tests the day someone
    moves it. The claim it guards is not about auth at all -- it is the
    counting rule the whole package is audited by, and it had ZERO occurrences
    anywhere in the repo before 2026-08-25.

    Why it earns a guard rather than trusting the prose to stay put:
    ``mark_all_read`` is a GET that mutates every conversation in the inbox in
    one request. A reader who assumes methods mean what they say classifies it
    as a read, and the classification is what the confirm gates hang off. The
    sentence is load-bearing documentation, so it gets a test like any other
    load-bearing thing.
    """

    def readme(self):
        return (
            Path(lifecycle.__file__).resolve().parent.parent / "README.md"
        ).read_text(encoding="utf-8")

    def test_the_counting_rule_is_stated_in_the_safety_section(self):
        readme = self.readme()
        assert "by effect, not by HTTP verb" in readme
        safety = readme.index("## Safety")
        where = readme.index("by effect, not by HTTP verb")
        assert where > safety, "the rule is not in the safety section"
        assert where - safety < 1200, (
            "the rule drifted down the safety section -- it has to be where a "
            "stranger meets it, not buried under the bullets"
        )

    def test_it_names_the_trap_that_makes_it_concrete(self):
        """A rule with no example is a slogan. mark_all_read IS the reason."""
        readme = self.readme()
        assert "mark_all_read" in readme
        where = readme.index("by effect, not by HTTP verb")
        nearby = readme[where : where + 900]
        assert "mark_all_read" in nearby
        assert "GET" in nearby
