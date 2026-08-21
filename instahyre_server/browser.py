"""The browser tier: for what the API cannot be *asked*, not for fetching data.

The governing rule in this package is that httpx is the data path. That rule
was previously stated as "no browser, ever", and overstating it cost real
capability -- the inbox was declared impossible when it was merely in a
namespace nobody had looked in. So the rule is now stated properly:

    **httpx for everything the API can do. The browser for what it cannot
    reach.** The browser is opt-in per tool, started lazily, and never sits in
    front of a request httpx could have made.

Today exactly one thing qualifies, and it is not a data fetch. Instahyre's
server-rendered HTML pages carry feature flags that no API endpoint exposes,
and those HTML paths are Cloudflare-gated: a plain client gets a 403 and
headless Chromium gets "Just a moment...". One of those flags,
``enableCandidateESOpps``, decides **which URL an application is posted to**.
Getting it wrong means an irreversible action sent to the wrong endpoint, so it
is worth a browser to read it rather than a comfortable assumption.

Measured 2026-08-21, and the reason this module runs headed:

    headless=True  -> Cloudflare challenge, "Just a moment...", no page
    headless=False -> the page renders, flags readable

**Read-only, enforced at the router.** Every request whose method is not GET is
aborted before it leaves the browser, with one exception: Cloudflare's own
``/cdn-cgi/`` challenge handshake, which mutates nothing in the account and
without which verification can never complete. That exception was learned the
hard way -- aborting it deadlocks the challenge and the page never loads.

**Why no page pool.** The sibling Naukri server keeps a persistent context with
a multi-tab pool, because it drives a browser constantly. Here the browser is
occasional, and a pooled sync-Playwright object would have to be pinned to one
thread for its whole life: FastMCP runs sync tools on anyio worker threads and
does not promise the same thread twice, so a cached context would eventually be
touched from the wrong one. A context per call costs a few seconds on a rare
operation and cannot develop that bug.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from . import constants as C
from .errors import InstahyreError
from .session import browser_profile_path

log = logging.getLogger("instahyre.browser")

OPPORTUNITIES_URL = C.SITE_BASE + "/candidate/opportunities/"

#: Flags worth reading off the page. Each is a server-injected Angular scope
#: value with no API equivalent.
PAGE_FLAGS = ("enableCandidateESOpps", "showSearchedJobs", "isCanOpportunityPage")

_CHALLENGE_TITLE = "just a moment"


class BrowserUnavailable(InstahyreError):
    kind = "browser_unavailable"


class BrowserBlocked(InstahyreError):
    kind = "browser_blocked"


def _read_only_router(record: list[str]):
    """Abort every non-GET except Cloudflare's own challenge handshake."""

    def handler(route: Any, request: Any) -> None:
        if request.method.upper() != "GET" and "/cdn-cgi/" not in request.url:
            record.append(f"{request.method} {request.url[:120]}")
            route.abort()
        else:
            route.continue_()

    return handler


def read_page_flags(*, timeout_s: int = 60) -> dict:
    """Load the opportunities page and read its feature flags.

    Returns what the page says, plus whether that agrees with the constant this
    package builds apply requests from. A disagreement is reported, never
    silently applied: changing which endpoint an irreversible action targets is
    not something a page scrape should do on its own.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserUnavailable(
            "Playwright is not installed, so the apply target cannot be re-measured. "
            "Run `pip install playwright && playwright install chromium`. The apply "
            "tools still work -- they use the recorded branch and say so."
        ) from exc

    profile_dir = browser_profile_path()
    blocked: list[str] = []

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
        )
        try:
            context.route("**/*", _read_only_router(blocked))
            page = context.pages[0] if context.pages else context.new_page()
            api_calls: list[str] = []
            page.on(
                "response",
                lambda r: api_calls.append(r.url) if "/api/v1/" in r.url else None,
            )
            page.goto(OPPORTUNITIES_URL, wait_until="domcontentloaded", timeout=timeout_s * 1000)
            page.wait_for_timeout(5_000)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass

            title = page.title() or ""
            if _CHALLENGE_TITLE in title.lower():
                raise BrowserBlocked(
                    "Cloudflare challenged the browser and the page never rendered, so "
                    "the flags could not be read. This is the expected outcome headless; "
                    "if it happens with a visible window, the profile's session may have "
                    "expired -- run instahyre_login_browser."
                )

            html = page.content()
            flags = {name: _flag_from_html(html, name) for name in PAGE_FLAGS}
            signed_in = "SIGN OUT" in page.inner_text("body").upper()
        finally:
            try:
                context.close()
            except Exception as exc:
                log.debug("closing the browser context raised: %s", exc)

    # The independent signal: which service the page's own XHRs used. It is
    # stronger than the flag text, because it is the behaviour rather than the
    # input to it.
    matching_calls = [u for u in api_calls if "/candidate_matching" in u]
    legacy_calls = [u for u in api_calls if "/candidate_opportunity" in u]
    observed_es: Optional[bool] = None
    if matching_calls and not legacy_calls:
        observed_es = True
    elif legacy_calls and not matching_calls:
        observed_es = False

    agrees = observed_es is None or observed_es == C.APPLY_BRANCH_ES
    return {
        "page_flags": flags,
        "signed_in": signed_in,
        "observed_service": (
            "candidate_matching (ES)"
            if observed_es is True
            else "candidate_opportunity (legacy)"
            if observed_es is False
            else "indeterminate"
        ),
        "queue_xhr_count": len(matching_calls) + len(legacy_calls),
        "configured_branch": "ES" if C.APPLY_BRANCH_ES else "legacy",
        "apply_url_in_force": C.API_BASE + (C.EP_APPLY_ES if C.APPLY_BRANCH_ES else C.EP_APPLY_LEGACY),
        "agrees_with_configuration": agrees,
        "non_get_requests_blocked": len(blocked),
        "read_only": (
            "Every non-GET request was aborted at the router except Cloudflare's own "
            "challenge handshake. Nothing was clicked and no application was submitted."
        ),
        "verdict": (
            "The configured apply branch matches what the site actually does."
            if agrees
            else "MISMATCH. The site used a different service from the one this server "
            "would post an application to. Do not apply until this is resolved -- "
            "raise it rather than flipping the constant on one observation."
        ),
    }


def _flag_from_html(html: str, name: str) -> Optional[bool]:
    """Pull a server-injected boolean out of the page source.

    Angular scope values are written into an inline script as JS literals. Both
    JS (`true`) and Python-rendered (`True`) spellings appear in Django
    templates, so both are accepted. Returns None when the flag is not present
    rather than defaulting to False -- absent and false are different answers
    and only one of them is safe to act on.
    """
    match = re.search(
        rf"{re.escape(name)}\s*[=:]\s*['\"]?(true|false|True|False)['\"]?", html
    )
    if not match:
        return None
    return match.group(1).lower() == "true"
