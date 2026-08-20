"""The one place Playwright is allowed to exist.

A browser buys exactly one thing here: Google OAuth, which is a redirect dance
through accounts.google.com that no HTTP client can complete. Everything else
-- password login included -- goes over plain HTTP, because Instahyre hands out
a CSRF token on every API response and ``/api/v1/*`` is not Cloudflare-gated.

So this module opens a **visible** window, lets the operator sign in with their
own hands, harvests the cookies, and closes. It never types a credential and it
never fetches job data.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from .errors import InstahyreError
from .http import InstahyreHTTP
from .session import (
    SESSION_COOKIE,
    SessionStore,
    apply_cookies,
    browser_profile_path,
    cookies_from_browser_state,
)

log = logging.getLogger("instahyre.auth")

LOGIN_URL = "https://www.instahyre.com/login/"
HOME_URL = "https://www.instahyre.com/candidate/opportunities/"
DEFAULT_WAIT_S = 300


class BrowserUnavailable(InstahyreError):
    kind = "browser_unavailable"


def login_via_browser(
    http: InstahyreHTTP,
    store: SessionStore,
    *,
    wait_seconds: int = DEFAULT_WAIT_S,
    headless: bool = False,
) -> dict:
    """Open Instahyre's login page and wait for the operator to sign in.

    Uses a **persistent** profile directory, so a later run usually finds the
    session already live and returns without asking for anything.

    Args:
        wait_seconds: how long to leave the window open for a human to finish.
        headless: only useful for re-checking an already-live persistent
            profile; a headless window cannot complete an interactive sign-in.
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
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(profile_dir),
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)

            deadline = started + wait_seconds
            harvested: dict[str, str] = {}
            while time.time() < deadline:
                harvested = cookies_from_browser_state(context.storage_state())
                if SESSION_COOKIE in harvested:
                    break
                page.wait_for_timeout(2_000)

            if SESSION_COOKIE not in harvested:
                raise InstahyreError(
                    f"No session cookie appeared within {wait_seconds}s. The window was open "
                    "-- sign in there (password or 'Continue with Google') and call this tool "
                    "again.",
                )
            apply_cookies(http, harvested)
            saved = store.save_from(http, method="browser")
        finally:
            context.close()

    return {
        "authenticated": True,
        "method": "browser",
        "profile_dir": str(profile_dir),
        "cookies_captured": sorted(saved.get("cookies", {})),
        "elapsed_seconds": round(time.time() - started, 1),
    }


def refresh_from_profile(http: InstahyreHTTP, store: SessionStore) -> Optional[dict]:
    """Silently re-harvest cookies from the persistent profile, if one is live.

    This is the transparent-refresh path: no window is shown, and a failure is
    reported as None rather than raised, because the caller always has the
    interactive path to fall back on.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    profile_dir = browser_profile_path()
    if not any(profile_dir.iterdir()):
        return None
    try:
        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(str(profile_dir), headless=True)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(HOME_URL, wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_timeout(1_500)
                harvested = cookies_from_browser_state(context.storage_state())
            finally:
                context.close()
    except Exception as exc:  # a silent refresh must never take the server down
        log.info("silent session refresh failed: %s", exc)
        return None

    if SESSION_COOKIE not in harvested:
        return None
    apply_cookies(http, harvested)
    store.save_from(http, method="browser-refresh")
    return {"authenticated": True, "method": "browser-refresh"}
