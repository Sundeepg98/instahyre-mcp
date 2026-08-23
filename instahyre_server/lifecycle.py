"""The auth LIFECYCLE: what the credential is, how it renews, how it ends.

Implements sections 1, 2 and 3 of the four-server auth contract
(``mcp-servers/_audit/2026-08-23-auth-contract.md``). Field names there are
NORMATIVE and are reproduced here exactly; where this platform genuinely
differs the difference gets its own named field and its own sentence, never a
familiar field quietly redefined.

THE ONE RULE EVERYTHING ELSE HANGS OFF
--------------------------------------
``authenticated`` comes from :func:`~instahyre_server.session.check_auth` and
from nowhere else. Never from a cookie being present, never from
``session.json`` existing, never from a profile directory being on disk. This
server already shipped that bug once: ``login_via_browser`` treated "a
``sessionid`` cookie appeared" as "the operator signed in", and Django hands a
``sessionid`` to ANONYMOUS visitors, so the condition was already true while
the login page was still loading. ``check_auth`` returns True, False or None
and each of the three means something different; this module passes all three
through untouched and NEVER collapses None into False.

THE TWO STORES -- the fact this module exists to keep straight
--------------------------------------------------------------
An Instahyre session lives in two unrelated places, and they answer different
halves of "how is my login doing":

1. ``_state/session.json`` -- cookie NAME/VALUE pairs and nothing else. This is
   what the httpx client sends. It records NO expiry, so it cannot say when the
   session dies.
2. ``_state/browser_profile`` -- the persistent Chrome profile
   ``instahyre_login_browser`` signs in to. Its SQLite cookie jar carries
   Chrome's ``expires_utc`` for every row, so the date exists there.

So the expiry reported by :func:`session_info` is read from (2) while the
cookie in use comes from (1). They are usually the same login. They do not have
to be: sign in again in the browser and (2) moves while (1) sits still. That is
said in ``credential.expiry_source`` in plain words rather than smoothed over,
because a date that describes a different session than the one in use is
exactly the quiet substitution this contract exists to stop -- and there is no
way to detect it from here, since the jar reader never fetches a cookie VALUE
and so cannot compare the two.

WHY A RENEW IS POSSIBLE AT ALL HERE
-----------------------------------
Because those two layers really are two layers. The contract ruled instahyre a
YES for ``reauth`` on measured evidence (2026-08-23): the persistent profile's
jar holds ``sessionid`` at +57.7 days and ``csrftoken`` at +363.6 days, both
persistent rows.

The seam :func:`reauth` drives is
:func:`~instahyre_server.auth.reharvest_from_profile` -- headless, no password,
no window, no human, and it loads the candidate opportunities page rather than
the login form. That last part is a ruling, not a detail (wave lead,
2026-08-23): a tool whose entire claim is "this is not a login" should not
fetch the login URL, and sending a browser that is carrying a live session to a
sign-in page is a needless risk against the one profile the operator depends
on. The first draft of this module drove ``login_via_browser(headless=True)``
instead, which was safe by every test but went to ``/login/`` to do it.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

from . import cookie_jar
from .auth import AUTH_ENDPOINT_NOTE, reharvest_from_profile
from .errors import InstahyreError
from .paths import display_path, relativise_prose
from .session import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    SessionStore,
    apply_cookies,
    check_auth,
)

#: The contract's ``server`` field. One spelling, shared by all three tools.
SERVER = "instahyre"

#: Said in the same words wherever it is said, because it is the sentence this
#: whole module exists to enforce.
COOKIE_IS_NOT_A_SESSION = (
    "a cookie in the jar is NOT a session. Instahyre issues a sessionid to "
    "signed-out visitors, so its presence means only that a jar exists; only "
    "the live check establishes that Instahyre still honours it."
)

_ON_EXPIRY = (
    "authenticated tools raise [auth_required] carrying the reason -- they "
    "never return an empty result instead. Recover with instahyre_reauth "
    "first: it re-harvests the persistent browser profile silently, with no "
    "password and no window. If the profile is dead too, instahyre_login_browser "
    "opens a window for a real sign-in, and instahyre_login takes an email and "
    "password directly."
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _scrub(text: Any, *paths: Any) -> Any:
    """Relativise every local path this module knows it may have emitted.

    The jar reader builds its messages as ``"...%s..." % profile_dir``, so the
    path is INSIDE the prose where no field rename can reach it. ``tests/
    test_path_hygiene.py`` walks every string of every offline tool payload, so
    an unscrubbed message here fails the suite rather than shipping.
    """
    return relativise_prose(text, known=[str(p) for p in paths if p])


def _snapshot_bytes(path: Path) -> Optional[bytes]:
    """The saved session file EXACTLY as it is on disk, or None if absent.

    Bytes, not the parsed dict. A restore has to put back the same file, and a
    round trip through ``json.loads`` / ``json.dumps`` is not the same file --
    key order, indentation and float formatting are all free to move. The test
    that matters asserts byte-identity, and it can only do that if the restore
    is byte-level.
    """
    try:
        return path.read_bytes()
    except OSError:
        return None


def _restore_store(path: Path, snapshot: Optional[bytes]) -> bool:
    """Put ``path`` back to ``snapshot``. Returns whether it now matches.

    ``snapshot is None`` means there was no file, so the restore is a delete.
    Never raises: this runs on the failure path of a renew, and an exception
    here would replace "the renew failed" with a traceback about tidying up.
    """
    try:
        if snapshot is None:
            if path.exists():
                path.unlink()
            return not path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(snapshot)
        return path.read_bytes() == snapshot
    except OSError:
        return False


def _restore_cookies(http: Any, snapshot: dict) -> None:
    """Put the client's jar back exactly as it was, entry for entry."""
    try:
        http.cookies.clear()
        apply_cookies(http, snapshot)
    except Exception:  # tidying up must never outrank the verdict it follows
        pass


def _cookie_names(http: Any) -> dict:
    try:
        return {name: value for name, value in http.cookies.items()}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Expiry, read from the profile's jar
# ---------------------------------------------------------------------------


def _expiry_facts(expires: Optional[float], *, now: float) -> dict:
    """Turn one jar row's ``expires`` into the three contract expiry fields.

    ``expires`` is POSIX seconds, ``-1.0`` for a row that dies with the browser,
    or None when the row was not in the jar at all.

    ``expired`` is ``true`` ONLY when a knowable date is in the past. Both
    "there is no such row" and "the row carries no date" report ``null``,
    because the contract forbids ``false`` as a stand-in for "unknown" -- a
    false there reads as "not expired", which is a claim, and no claim was
    measured.
    """
    if expires is None or expires <= 0:
        return {"expires_at": None, "expires_in_days": None, "expired": None}
    remaining = expires - now
    return {
        "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires)),
        "expires_in_days": round(remaining / 86400.0, 1),
        "expired": remaining <= 0,
    }


def _read_profile_jar(profile_dir: Any) -> tuple[dict, Optional[str]]:
    """``({name: expires}, error_text)`` from the persistent profile's jar.

    Never raises. A jar that cannot be read is an UNKNOWN expiry, reported as
    such with the reader's own reason attached -- not a missing cookie, and
    certainly not an unexpired one.
    """
    try:
        records = cookie_jar.read_jar(profile_dir, [SESSION_COOKIE, CSRF_COOKIE])
    except cookie_jar.CookieJarUnavailableError as exc:
        return {}, _scrub(str(exc), profile_dir)
    except Exception as exc:  # pragma: no cover - defensive
        return {}, _scrub("%s: %s" % (type(exc).__name__, exc), profile_dir)

    out: dict = {}
    for record in records:
        name = record.get("name")
        if name is not None and name not in out:
            out[name] = record.get("expires")
    return out, None


def _expiry_source(
    profile_dir: Any,
    store_path: Any,
    *,
    jar_error: Optional[str],
    expires: Optional[float],
    name: str = SESSION_COOKIE,
) -> str:
    """Where the date came from, or why there is not one. Never a shrug.

    FOUR cases, not two, and they do not share a sentence because an operator
    acts differently on each. The one that is easy to lose is the third: a row
    that IS in the jar but carries no date, because it is a session-only cookie
    that dies with the browser. Reporting that under the "here is where the
    date came from" sentence would describe an expiry that is null -- prose
    claiming an answer the fields next to it do not have.
    """
    profile = display_path(str(profile_dir))
    stored = display_path(str(store_path))
    if jar_error:
        return (
            "unknown -- the expiry is not knowable from here. It is not in %s, "
            "which stores cookie name/value pairs and no dates at all, and the "
            "persistent browser profile's cookie jar could not be read: %s"
            % (stored, jar_error)
        )
    if expires is None:
        return (
            "unknown -- the persistent browser profile's cookie jar at %s was "
            "read and holds no %s row for instahyre.com at all, so nothing has "
            "ever signed in there. %s records no dates, so there is no second "
            "place to look." % (profile, name, stored)
        )
    if expires <= 0:
        return (
            "unknown -- the persistent browser profile's cookie jar at %s holds "
            "a %s row, but a session-only one: it carries no expiry and dies "
            "with the browser process, which is how Instahyre stores the cookie "
            "it hands a signed-out visitor. %s records no dates either."
            % (profile, name, stored)
        )
    return (
        "the persistent browser profile's cookie jar at %s, read from a COPY "
        "with no browser launched and no cookie value fetched. READ THIS AS A "
        "FACT ABOUT THAT PROFILE: the cookie this server actually sends comes "
        "from %s, which records no expiry, and the two stores can hold "
        "different sessions -- signing in again in the browser moves the "
        "profile while the saved jar sits still. Nothing here can tell them "
        "apart, because comparing them would mean reading a cookie value."
        % (profile, stored)
    )


# ---------------------------------------------------------------------------
# The credential block, shared by session_info and reauth
# ---------------------------------------------------------------------------


def _credential_blocks(
    store: SessionStore, profile_dir: Any, *, now: Optional[float] = None
) -> tuple[dict, list, Optional[str]]:
    """``(credential, supporting, jar_error)`` -- the contract's two cookie blocks.

    Presence comes from the SAVED STORE, because that is the jar the HTTP
    client sends from and therefore the one that decides whether a request can
    be made at all. Expiry comes from the profile's jar, because the store has
    no dates. Both halves say where they came from.
    """
    now = time.time() if now is None else now
    saved = store.read()
    saved_cookies = saved.get("cookies") or {}

    jar, jar_error = _read_profile_jar(profile_dir)
    session_expiry = _expiry_facts(jar.get(SESSION_COOKIE), now=now)
    csrf_expiry = _expiry_facts(jar.get(CSRF_COOKIE), now=now)

    present = SESSION_COOKIE in saved_cookies
    credential = {
        "kind": "cookie",
        "name": SESSION_COOKIE,
        "present": present,
        # A Django session cookie is an opaque signed blob in a cookie, not a
        # JWT and not a bearer token. "cookie" is the contract's spelling for
        # exactly that.
        "format": "cookie" if present else "absent",
        **session_expiry,
        "expiry_source": _expiry_source(
            profile_dir,
            store.path,
            jar_error=jar_error,
            expires=jar.get(SESSION_COOKIE),
        ),
        # Instahyre is not known to revoke a sessionid before its cookie
        # expiry: the row is a plain Django session cookie and nothing measured
        # on 2026-08-23 showed a shorter server-side life. The date is
        # therefore reported as authoritative FOR THE PROFILE IT WAS READ FROM
        # -- which is what expiry_source above is careful to name.
        "expiry_is_authoritative": True,
    }
    supporting = [
        {
            "name": CSRF_COOKIE,
            "role": "csrf",
            "present": CSRF_COOKIE in saved_cookies,
            **csrf_expiry,
        }
    ]
    return credential, supporting, jar_error


def _durability(store: SessionStore) -> dict:
    return {
        "stored_in": display_path(str(store.path)),
        "survives_server_restart": True,
        "survives_machine_reboot": True,
        "why": (
            "the cookies are written to a file on disk, not held in this "
            "process, so stopping the server or rebooting the machine leaves "
            "them exactly where they were. What ends the session is Instahyre "
            "expiring it, a sign-out, or the file being deleted -- which is "
            "what instahyre_logout does."
        ),
    }


def _renewal(
    profile_dir: Any, *, credential: dict, jar_error: Optional[str]
) -> dict:
    """How the credential renews, and the date past which it cannot.

    ``session_lapses_at`` is a DIFFERENT QUESTION from
    ``credential.expires_at`` and the contract gives it its own key for that
    reason (wave lead, 2026-08-23, uniform across all four servers). The
    credential date says when THIS cookie dies; the lapse date says when no
    silent renew can help any more and a human has to sign in. On naukri those
    two are five orders of magnitude apart -- ``nauk_at`` measured at +0.02
    days against a refresh cookie good for +188 -- so a client that treated
    them as one field would rank the family badly wrong.

    ON INSTAHYRE THEY COINCIDE, and the reason is worth more than the number.
    The profile's ``sessionid`` is simultaneously the expiry this tool can read
    AND the thing :func:`reauth` would renew FROM. When it lapses there is
    nothing left to re-harvest, so the silent path dies with it.

    The value is REUSED from the credential block rather than re-read from the
    jar. Two reads of the same row can return different answers -- the browser
    may write between them -- and a payload whose two dates disagree with each
    other is worse than one that carries neither.
    """
    return {
        "silent_renew_available": True,
        "tool": "instahyre_reauth",
        "why": (
            "the persistent Chrome profile at %s holds its OWN sessionid, and "
            "that one outlives the copy saved in the file above: it is a "
            "persistent row with a long expiry, and the browser keeps it "
            "refreshed. instahyre_reauth re-opens that profile headless, "
            "re-harvests its cookies and puts the result to the live endpoint "
            "before believing any of it. No password, no window, no human."
            % display_path(str(profile_dir))
        ),
        "session_lapses_at": credential["expires_at"],
        "session_lapses_in_days": credential["expires_in_days"],
        "session_lapses_source": _session_lapses_source(
            profile_dir, jar_error=jar_error, expires_at=credential["expires_at"]
        ),
    }


def _session_lapses_source(
    profile_dir: Any, *, jar_error: Optional[str], expires_at: Optional[str]
) -> str:
    """Which credential governs the lapse, BY NAME, and what the date is not."""
    if expires_at is None:
        return (
            "unknown -- the date past which no silent renew can help is the "
            "expiry of the persistent browser profile's sessionid, and that "
            "row could not be read: %s"
            % (jar_error or "the profile holds no persistent sessionid row")
        )
    return (
        "the persistent browser profile's own sessionid -- the SAME row "
        "credential.expires_at reports, which is why the two dates are equal "
        "here rather than by coincidence. That row governs the lapse because "
        "it is both the expiry this tool can see AND the thing instahyre_reauth "
        "renews FROM: once it is gone there is nothing left to re-harvest and a "
        "human must sign in through instahyre_login_browser. THE SAME CAVEAT AS "
        "credential.expiry_source APPLIES -- this is a fact about the PROFILE's "
        "session, while the cookie this server actually sends comes from the "
        "saved store, which records no expiry at all. Do not read this date as "
        "a guarantee about the credential currently in use."
    )


# ---------------------------------------------------------------------------
# 1. session_info
# ---------------------------------------------------------------------------


def session_info(
    *,
    store: SessionStore,
    profile_dir: Any,
    http: Any = None,
    verify_live: bool = True,
) -> dict:
    """The contract's section-1 report: what the credential is and how it is.

    Two modes, and the difference is the whole point:

    * ``verify_live=True`` puts the question to Instahyre. ``authenticated`` is
      a measurement -- true, false, or null when the endpoint could not be made
      to answer either way.
    * ``verify_live=False`` costs NO network and NO browser. ``authenticated``
      is null and ``live_check.why_not`` says the offline answer is what was
      asked for. It is null rather than false because nothing said no.

    Args:
        store: the saved cookie jar. Read, never written.
        profile_dir: the persistent Chrome profile. Its cookie jar is read from
            a COPY for expiry dates only; no browser is launched in either mode.
        http: the live client. Required only when ``verify_live`` is true.
        verify_live: whether to spend one request on the live check.
    """
    credential, supporting, jar_error = _credential_blocks(store, profile_dir)

    authenticated: Optional[bool] = None
    live_check: dict
    if not verify_live:
        live_check = {
            "attempted": False,
            "completed": False,
            "endpoint": AUTH_ENDPOINT_NOTE,
            "why_not": "not attempted: this call asked for the offline answer",
            "what_it_means": (
                "'authenticated' is null because nothing was asked, NOT because "
                "Instahyre said no. Call instahyre_session_info(verify_live=True) "
                "or instahyre_auth_status for a verdict. " + COOKIE_IS_NOT_A_SESSION
            ),
        }
    else:
        status, failure = _live_status(http, store, profile_dir)
        authenticated = status.get("authenticated") if status else None
        if authenticated is not None:
            live_check = {
                "attempted": True,
                "completed": True,
                "endpoint": AUTH_ENDPOINT_NOTE,
                "what_it_means": (
                    "the endpoint was asked and answered 200, so 'authenticated' "
                    "above is a measurement"
                    if authenticated
                    else
                    "the endpoint was asked and answered 401, so 'authenticated' "
                    "above is a measured NO: %s"
                    % (status.get("reason") or "the session was rejected")
                ),
            }
        else:
            why = failure or (status or {}).get("reason") or (
                "the auth check returned no verdict"
            )
            live_check = {
                "attempted": True,
                "completed": False,
                "endpoint": AUTH_ENDPOINT_NOTE,
                "why_not": why,
                "what_it_means": (
                    "'authenticated' is null because the live check could not be "
                    "completed, NOT because Instahyre said no. The cookie facts "
                    "below are the only thing measured here, and "
                    + COOKIE_IS_NOT_A_SESSION
                ),
            }

    return {
        "server": SERVER,
        "authenticated": authenticated,
        "checked_against": AUTH_ENDPOINT_NOTE,
        "live_check": live_check,
        "credential": credential,
        "supporting": supporting,
        "credential_source": (
            "the on-disk store, read without a browser -- expiry comes from a "
            "DIFFERENT store, the persistent browser profile's own cookie jar; "
            "see credential.expiry_source"
        ),
        "durability": _durability(store),
        "renewal": _renewal(
            profile_dir, credential=credential, jar_error=jar_error
        ),
        "on_expiry": _ON_EXPIRY,
    }


def _live_status(
    http: Any, store: SessionStore, profile_dir: Any
) -> tuple[Optional[dict], Optional[str]]:
    """Run the live check, or say in plain text why it could not run.

    Returns ``(status, failure_text)``; exactly one of the two is meaningful.
    An exception here is a live_check that did not complete -- it is never
    allowed to become ``authenticated: false``, which is the single substitution
    this contract was written to prevent.
    """
    if http is None:
        return None, (
            "no HTTP client was available to make the request with, so nothing "
            "was asked"
        )
    try:
        return check_auth(http), None
    except InstahyreError as exc:
        return None, _scrub(
            "the auth check failed: [%s] %s" % (exc.kind, exc.message),
            store.path,
            profile_dir,
        )
    except Exception as exc:  # pragma: no cover - defensive
        return None, _scrub(
            "the auth check raised %s: %s" % (type(exc).__name__, exc),
            store.path,
            profile_dir,
        )


# ---------------------------------------------------------------------------
# 2. logout
# ---------------------------------------------------------------------------


def logout(*, http: Any, store: SessionStore, profile_dir: Any) -> dict:
    """The contract's section-2 shape. Clears the LOCAL credential only.

    Semantics are unchanged from the tool this replaces: the saved jar and this
    process's cookies go, the browser profile stays, and nothing is ended on
    Instahyre's side. Only the reported fields are new.

    This is the ONE place ``authenticated: false`` is stated without a live
    check, and the contract blesses it for a reason worth stating: after this
    runs there is no credential left to make an authenticated request WITH, so
    the false is provable from here rather than measured over there.

    THAT ARGUMENT ONLY HOLDS WHEN THE CLEAR ACTUALLY SUCCEEDED. If any part of
    it failed -- a locked session file is the realistic case -- then something
    IS left, an authenticated request could still be made with it, and the
    false loses the very thing that justified it. So a partial clear reports
    ``authenticated: null`` (wave lead ruling, 2026-08-23, taken across all
    four servers). The first draft returned false here beside a reason that
    said "treat the credential as present until this is fixed": the prose was
    right and the field contradicted it in the same object.

    Never raises. A logout that fails with a traceback leaves the operator not
    knowing whether their credential is gone, which is the worst of the three
    possible outcomes.
    """
    problems: list[str] = []

    had_cookies = bool(_cookie_names(http)) if http is not None else False

    had_file = False
    try:
        had_file = store.clear()
    except Exception as exc:
        problems.append(
            _scrub(
                "the saved session file could not be removed (%s: %s)"
                % (type(exc).__name__, exc),
                store.path,
            )
        )

    if http is not None:
        try:
            http.cookies.clear()
        except Exception as exc:  # pragma: no cover - defensive
            problems.append(
                "this process's in-memory cookies could not be cleared (%s: %s)"
                % (type(exc).__name__, exc)
            )

    if problems:
        # NOT false. Something survived, so an authenticated request may still
        # be possible and nobody measured whether it is.
        authenticated = None
        reason = (
            "PARTIAL: %s. Anything that survived is still usable, so treat the "
            "credential as present until this is fixed." % "; ".join(problems)
        )
    else:
        authenticated = False
        reason = (
            "no credential is left in this process or on disk, so no authenticated "
            "request can be made from here. That is why this false needs no live "
            "check: there is nothing left to check with."
        )

    return {
        "cleared": bool(had_file or had_cookies),
        "scope": (
            "the saved cookie jar at %s and this process's in-memory cookies. "
            "The persistent browser profile at %s is NOT touched, and the "
            "session on Instahyre's side is not ended -- this server has no "
            "sign-out call and does not pretend to."
            % (display_path(str(store.path)), display_path(str(profile_dir)))
        ),
        "authenticated": authenticated,
        "reason": reason,
        # The same facts as the reason prose, in a form a caller can branch on
        # without parsing English. Empty list on a clean clear.
        "problems": problems,
        "what_is_lost": (
            "the httpx client's way in. Every authenticated tool -- the inbound "
            "queue, the inbox, the profile, applications -- reports "
            "[auth_required] until a credential is restored. Nothing on "
            "Instahyre's side changes: applications already sent stay sent, and "
            "any browser still signed in stays signed in."
        ),
        "recover_by": (
            "instahyre_reauth first. The browser profile was left alone, so its "
            "sessionid is still there and can be re-harvested silently with no "
            "password. If that profile is dead too, instahyre_login_browser "
            "opens a window for a real sign-in, and instahyre_login takes an "
            "email and password."
        ),
    }


# ---------------------------------------------------------------------------
# 3. reauth
# ---------------------------------------------------------------------------


def reauth(*, http: Any, store: SessionStore, profile_dir: Any) -> dict:
    """The contract's section-3 silent renew. No password, no window, no human.

    The order is the whole safety property, so it is spelled out:

    1. SNAPSHOT the saved session file, as bytes, before anything runs.
    2. Re-open the persistent profile HEADLESS through
       :func:`~instahyre_server.auth.reharvest_from_profile`, which loads the
       candidate opportunities page -- NOT the login form -- and harvests the
       profile's cookies.
    3. VERIFY them against the live endpoint. That seam does this itself, and
       it is the reason this function drives it rather than harvesting alone:
       a harvested ``sessionid`` is a reason to ASK, never an answer.
    4. ``renewed: true`` and a saved jar ONLY when the endpoint answered 200.
    5. On ANY other outcome put the snapshot back, byte for byte, and put the
       client's cookies back with it. A failed renew must never cost a session
       that was already working.

    Headless is not an optimisation, it is the guarantee: no window can open,
    so no human can be waited for, so this can never quietly become an
    interactive login wearing a different name. It takes no credential
    parameter for the same reason, and the seam never visits a sign-in page.

    Step 5 is done here as well as in the seam. The seam restores the client's
    cookies on the paths that touched them, and never writes the store except
    on a proven success -- but this function's contract is that the snapshot
    goes back, and a guarantee that rests on a callee's internals is not a
    guarantee. The two are idempotent together.
    """
    snapshot = _snapshot_bytes(store.path)
    cookies_before = _cookie_names(http)

    record: dict
    try:
        record = reharvest_from_profile(http, store)
    except InstahyreError as exc:  # pragma: no cover - the seam returns, never raises
        record = {
            "authenticated": None,
            "outcome": "seam_raised",
            "reason": _scrub(
                "[%s] %s" % (exc.kind, exc.message), store.path, profile_dir
            ),
        }
    except Exception:
        # An unexpected bug still may not cost a working session.
        _restore_store(store.path, snapshot)
        _restore_cookies(http, cookies_before)
        raise

    authenticated = record.get("authenticated")
    renewed = authenticated is True

    if renewed:
        restored = False
    else:
        restored = _restore_store(store.path, snapshot)
        _restore_cookies(http, cookies_before)

    credential, supporting, _ = _credential_blocks(store, profile_dir)

    return {
        "renewed": renewed,
        "authenticated": authenticated,
        "method": (
            "re-opened the persistent browser profile headless, loaded the "
            "candidate opportunities page -- never the login form -- and "
            "re-harvested its cookies, then put them to the live endpoint. No "
            "password was used, no window was opened, and no sign-in page was "
            "visited."
        ),
        "stage": record.get("stage") or "profile_reharvest",
        # The same fact as the reason prose, in a form a caller can branch on
        # without parsing English. One of auth.REHARVEST_OUTCOMES.
        "outcome": record.get("outcome"),
        "checked_against": record.get("checked_against") or AUTH_ENDPOINT_NOTE,
        "reason": _reauth_reason(record, renewed=renewed),
        "previous_credential_restored": restored,
        "credential": credential,
        "supporting": supporting,
    }


def _reauth_reason(record: dict, *, renewed: bool) -> str:
    """Why the renew landed where it did, naming the fallback when it failed.

    The seam supplies a distinct reason for every distinct outcome, so this
    passes it through rather than replacing it with a generic sentence -- the
    difference between "install Playwright", "sign in once so a profile
    exists" and "the profile went stale" is the whole value of the field. The
    fallback tools are named on every failure, whichever one it was.
    """
    detail = record.get("reason")
    if renewed:
        return detail or (
            "the profile's cookies were put to %s and it answered 200."
            % AUTH_ENDPOINT_NOTE
        )
    if not detail:  # pragma: no cover - the seam always supplies one
        detail = "the endpoint did not accept the profile's cookies"
    return (
        "no silent renew was possible: %s The previous saved session was put "
        "back exactly as it was, so nothing was lost. Run "
        "instahyre_login_browser -- it opens a window so a real sign-in can "
        "happen -- or instahyre_login with an email and password." % detail
    )
