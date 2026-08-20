"""The one place Playwright is allowed to exist.

A browser buys exactly one thing here: Google OAuth, which is a redirect dance
through accounts.google.com that no HTTP client can complete. Everything else
-- password login included -- goes over plain HTTP, because Instahyre hands out
a CSRF token on every API response and ``/api/v1/*`` is not Cloudflare-gated.

So this module opens a **visible** window, lets the operator sign in with their
own hands, harvests the cookies, and closes. It never types a credential and it
never fetches job data.

**The completion condition is an authenticated request, never a cookie.** This
module used to stop the moment a ``sessionid`` cookie appeared, which Django
issues to *anonymous* visitors -- so the condition was already true when the
login page finished loading. The window closed after one poll and the tool
reported success while the operator was still reaching for the keyboard. A
signal that cannot distinguish success from its absence is not a signal, so
every claim here now comes from :func:`~instahyre_server.session.check_auth`,
the same endpoint ``instahyre_auth_status`` measures against.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from .errors import InstahyreError
from .http import InstahyreHTTP
from .session import (
    SESSION_COOKIE,
    SessionStore,
    apply_cookies,
    browser_profile_path,
    check_auth,
    cookies_from_browser_state,
)

log = logging.getLogger("instahyre.auth")

LOGIN_URL = "https://www.instahyre.com/login/"
HOME_URL = "https://www.instahyre.com/candidate/opportunities/"
DEFAULT_WAIT_S = 300

#: How often the loop wakes up and looks at the browser. Local and free.
POLL_INTERVAL_S = 2.5

#: How often the loop spends an API request when the browser's cookies have not
#: moved. A cookie change re-checks immediately -- Django cycles the session key
#: on login, so the normal case is caught within one tick -- and this only
#: bounds the odd case where a session goes live without the jar changing.
#: It also keeps a 300s wait to ~21 requests instead of ~120.
RECHECK_INTERVAL_S = 15.0

#: What every claim in this module is measured against.
AUTH_ENDPOINT_NOTE = "GET /api/v1/job_category/ (401 when logged out)"

#: Callback signature: ``(elapsed_seconds, total_seconds, message)``.
ProgressHook = Callable[[float, float, str], None]

_WAITING_MESSAGE = (
    "Waiting for you to sign in at " + LOGIN_URL + " -- the window stays open "
    "until a signed-in session is confirmed."
)


class BrowserUnavailable(InstahyreError):
    kind = "browser_unavailable"


# ---------------------------------------------------------------------------
# Small helpers, each doing one thing the loop should not have to spell out
# ---------------------------------------------------------------------------


def _report(hook: Optional[ProgressHook], elapsed: float, total: float, message: str) -> None:
    """Progress is cosmetic. It may never take the login down with it."""
    if hook is None:
        return
    try:
        hook(round(elapsed, 1), float(total), message)
    except Exception as exc:  # a dead client is not a failed sign-in
        log.debug("progress hook raised, ignoring: %s: %s", type(exc).__name__, exc)


def _snapshot(http: InstahyreHTTP) -> dict[str, str]:
    return {name: value for name, value in http.cookies.items()}


def _restore(http: InstahyreHTTP, snapshot: dict[str, str]) -> None:
    """Put the jar back exactly as it was.

    A sign-in attempt that fails must not cost the operator a session that was
    already working -- and an anonymous cookie harvested from a stale profile
    would otherwise overwrite a live one.
    """
    http.cookies.clear()
    apply_cookies(http, snapshot)


def _verify(http: InstahyreHTTP, cookies: dict[str, str]) -> dict:
    """The only completion condition: an authenticated request coming back 200."""
    apply_cookies(http, cookies)
    return check_auth(http)


def _live_page(context: Any, page: Any) -> Any:
    """The page worth watching, or ``None`` when the window is gone.

    A sign-in can close the page it started on -- an OAuth redirect may land in
    a fresh tab -- so a closed page with living siblings means "follow the
    sibling", not "the operator gave up". Any question we cannot get an answer
    to counts as the window being gone, because the alternative is hanging.
    """
    try:
        if page is not None and not page.is_closed():
            return page
    except Exception:
        pass
    try:
        pages = list(context.pages)
    except Exception:
        return None
    return pages[0] if pages else None


def _close_quietly(context: Any) -> None:
    try:
        context.close()
    except Exception as exc:  # already gone is the normal case here
        log.debug("closing the browser context raised: %s: %s", type(exc).__name__, exc)


# ---------------------------------------------------------------------------
# The wait
# ---------------------------------------------------------------------------


def _wait_for_signed_in_session(
    context: Any,
    page: Any,
    http: InstahyreHTTP,
    *,
    started: float,
    wait_seconds: int,
    on_progress: Optional[ProgressHook],
) -> dict:
    """Poll until the API says we are signed in, the window dies, or time runs out.

    Returns a record of what happened. It never claims a session it did not
    measure, and it never closes the window early on a cookie.
    """
    record: dict[str, Any] = {
        "status": None,
        "checks": 0,
        "window_closed": False,
        "session_cookie_seen": False,
    }
    checked_cookies: Optional[dict[str, str]] = None
    last_check_at = 0.0
    harvested: dict[str, str] = {}

    while True:
        elapsed = time.time() - started
        _report(on_progress, elapsed, wait_seconds, _WAITING_MESSAGE)

        page = _live_page(context, page)
        if page is None:
            record["window_closed"] = True
            break

        try:
            harvested = cookies_from_browser_state(context.storage_state())
        except Exception as exc:
            if _live_page(context, page) is None:
                record["window_closed"] = True
                break
            raise BrowserUnavailable(
                "The browser stopped responding while waiting for sign-in: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        if SESSION_COOKIE in harvested:
            record["session_cookie_seen"] = True

        now = time.time()
        # A cookie is only ever a reason to *ask*. It is never an answer.
        worth_asking = SESSION_COOKIE in harvested and (
            checked_cookies is None
            or harvested != checked_cookies
            or (now - last_check_at) >= RECHECK_INTERVAL_S
        )
        if worth_asking:
            checked_cookies = dict(harvested)
            last_check_at = now
            record["checks"] += 1
            status = _verify(http, harvested)
            record["status"] = status
            if status.get("authenticated") is True:
                return record
            if status.get("error") == "challenge_detected":
                # The tripwire says stop and reassess. That cannot mean
                # "ask a hundred more times".
                log.warning("Cloudflare challenged the auth check; abandoning the wait")
                break

        elapsed = time.time() - started
        if elapsed >= wait_seconds:
            break
        time.sleep(min(POLL_INTERVAL_S, max(wait_seconds - elapsed, 0.0)))

    # No final catch-up check here on purpose: a cookie set that differs from
    # the last one checked always satisfies ``worth_asking``, so a newly
    # signed-in jar is put to the endpoint on the same tick it is harvested.
    # There is no unchecked state left to rescue, and a rescue that cannot
    # fire is worse than none -- it reads like a safety net.
    return record


def login_via_browser(
    http: InstahyreHTTP,
    store: SessionStore,
    *,
    wait_seconds: int = DEFAULT_WAIT_S,
    headless: bool = False,
    on_progress: Optional[ProgressHook] = None,
) -> dict:
    """Open Instahyre's login page and wait for the operator to actually sign in.

    Uses a **persistent** profile directory, so a later run usually finds the
    session already live and returns in one request without asking for anything.

    The window stays open until ``/api/v1/job_category/`` answers 200 or
    ``wait_seconds`` runs out. Nothing else counts: a ``sessionid`` cookie is a
    reason to ask the endpoint, never an answer.

    Args:
        wait_seconds: how long to leave the window open for a human to finish.
        headless: only useful for re-checking an already-live persistent
            profile; a headless window cannot complete an interactive sign-in.
        on_progress: optional ``(elapsed, total, message)`` hook, called once
            per poll. Exceptions from it are swallowed.

    Returns:
        ``authenticated: True`` only when the endpoint said so. ``False`` on a
        timeout or a window the operator closed, with a ``reason``. ``None``
        when the state could not be determined at all -- unknown does not
        collapse into false here any more than it does in ``check_auth``.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserUnavailable(
            "Playwright is not installed. Either run `pip install playwright && "
            "playwright install chromium`, or use instahyre_login with an email and "
            "password, which needs no browser at all."
        ) from exc

    profile_dir = browser_profile_path()
    started = time.time()
    before = _snapshot(http)

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(profile_dir),
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
            log.info(
                "browser open at %s -- waiting up to %ss for a confirmed sign-in",
                LOGIN_URL,
                wait_seconds,
            )
            record = _wait_for_signed_in_session(
                context,
                page,
                http,
                started=started,
                wait_seconds=wait_seconds,
                on_progress=on_progress,
            )
        finally:
            _close_quietly(context)

    elapsed = round(time.time() - started, 1)
    status = record["status"] or {}
    common = {
        "method": "browser",
        "profile_dir": str(profile_dir),
        "elapsed_seconds": elapsed,
        "checks_run": record["checks"],
        "checked_against": status.get("checked_against", AUTH_ENDPOINT_NOTE),
    }

    if status.get("authenticated") is True:
        saved = store.save_from(http, method="browser")
        log.info("signed-in session confirmed after %ss", elapsed)
        return {
            "authenticated": True,
            "cookies_captured": sorted(saved.get("cookies", {})),
            "verified_by": status.get("checked_against"),
            **common,
        }

    # Nothing below this line got a 200. Put the jar back and say so plainly.
    _restore(http, before)

    if status.get("authenticated") is None and status:
        return {
            "authenticated": None,
            "reason": (
                "Could not determine whether the sign-in succeeded: "
                f"{status.get('reason', 'the auth check did not return a verdict')}"
            ),
            "error": status.get("error"),
            **common,
        }

    if record["window_closed"]:
        reason = (
            "The browser window was closed before a signed-in session could be confirmed. "
            "If you did finish signing in, the profile kept it: call instahyre_auth_status, "
            "or run this tool again and it will confirm in about a second. Otherwise leave "
            "the window open until this tool returns on its own."
        )
    else:
        reason = (
            f"No signed-in session appeared within {wait_seconds}s. The window was open "
            f"at {LOGIN_URL} -- sign in there (password or 'Continue with Google') and "
            "call this tool again with a longer wait_seconds if you need more time."
        )
        if record["session_cookie_seen"]:
            reason += (
                " A sessionid cookie was present the whole time, but it was an anonymous "
                "one -- Instahyre issues those to signed-out visitors."
            )

    return {
        "authenticated": False,
        "reason": reason,
        "window_closed": record["window_closed"],
        "session_cookie_present": record["session_cookie_seen"],
        **common,
    }


def refresh_from_profile(http: InstahyreHTTP, store: SessionStore) -> Optional[dict]:
    """Silently re-harvest cookies from the persistent profile, if one is live.

    This is the transparent-refresh path: no window is shown, and a failure is
    reported as None rather than raised, because the caller always has the
    interactive path to fall back on.

    Same rule as the interactive path -- a dead profile still holds an
    anonymous ``sessionid``, so the cookies are verified against the API before
    anything is applied or saved, and the jar is put back if they do not check
    out.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    profile_dir = browser_profile_path()
    if not any(profile_dir.iterdir()):
        return None
    before = _snapshot(http)
    try:
        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(str(profile_dir), headless=True)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(HOME_URL, wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_timeout(1_500)
                harvested = cookies_from_browser_state(context.storage_state())
            finally:
                _close_quietly(context)
    except Exception as exc:  # a silent refresh must never take the server down
        log.info("silent session refresh failed: %s", exc)
        return None

    if SESSION_COOKIE not in harvested:
        return None

    status = _verify(http, harvested)
    if status.get("authenticated") is not True:
        _restore(http, before)
        log.info(
            "the browser profile holds no live session (%s)",
            status.get("reason") or status.get("error") or "endpoint said no",
        )
        return None

    store.save_from(http, method="browser-refresh")
    return {
        "authenticated": True,
        "method": "browser-refresh",
        "verified_by": status.get("checked_against"),
    }
