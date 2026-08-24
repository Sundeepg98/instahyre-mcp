"""Record the request a control WOULD send, and abort it before it leaves.

WHY THIS EXISTS
---------------
Six write surfaces on this platform are unbuilt because nobody has recorded the
request the page actually sends -- the finding is pinned in
``constants.UNVERIFIED_WRITE_SURFACES``. A write built on a guessed body is
worse than no tool: a wrong guess usually 400s harmlessly, a half-right guess
succeeds and does something nobody chose, and on Instahyre the second case is
permanent. So the body has to be MEASURED.

The trick is that a request can be measured without being delivered.
``instahyre_server.browser`` already proves the shape: it opens the real
signed-in browser and aborts every non-GET at the router, so it physically
cannot mutate the account. That module only counts what it blocked. This one
RECORDS what it blocked -- method, URL, body, headers, and the page state that
selected the variant -- which is the whole missing contract.

WHAT MAKES THIS SAFE, in the order the checks run
-------------------------------------------------
1. ``_router`` aborts, unconditionally and before anything else, any URL that
   matches :data:`NEVER_REACHES_THE_WIRE` -- apply, bulk apply, logout -- for
   EVERY method including GET. Apply is irreversible and their own UI has no
   confirmation dialog, so the router is the only brake that exists.
2. It aborts every non-GET. The single exception is Cloudflare's own
   ``/cdn-cgi/`` challenge handshake, which mutates nothing and without which
   the page never renders (learned the hard way in ``browser.py``).
3. It aborts every request to a third-party identity host, so an OAuth consent
   flow can be OBSERVED being started without ever being granted.
4. Before any recipe runs, :func:`_prove_the_router_aborts` fires a POST from
   inside the page and asserts it was blocked and recorded. A router that
   cannot abort certifies nothing, and this run refuses to continue without
   that proof. It is the control for the instrument itself.

WHAT MAKES THE OUTPUT SAFE
--------------------------
Everything recorded passes through :func:`scrub` on the way in -- at capture
time, not at write time. A sibling repo pushed twenty real recruiter email
addresses because one capture escaped its scrub map on the way out. Credentials
(cookie, csrf, authorization) are DROPPED rather than masked: a masked
credential is still a length and a shape.

Strict ASCII, like every file in this package.

USAGE
-----
    venv/Scripts/python.exe scripts/capture_write_contracts.py recon
    venv/Scripts/python.exe scripts/capture_write_contracts.py recon --url https://www.instahyre.com/candidate/refer/
    venv/Scripts/python.exe scripts/capture_write_contracts.py recipe --recipe support_ticket

``recon`` clicks NOTHING. It loads a page and reports what the page could fire:
the ng-click census, whether each named handler is actually defined on its
scope (a binding with no callee is a dead control, which is a finding), the
authenticated-only script bundles, and every XHR the page made on its own.

``recipe`` drives one named control with obviously-fake input and records the
request that the router then throws away.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from instahyre_server import constants as C  # noqa: E402
from instahyre_server.session import browser_profile_path  # noqa: E402

SITE = C.SITE_BASE

#: Aborted for EVERY method, including GET. These are the actions that cannot
#: be undone, plus the one that would destroy the session this run depends on.
NEVER_REACHES_THE_WIRE = re.compile(
    r"/apply/|/apply_bulk/|/logout|/sign_?out", re.IGNORECASE
)

#: Third-party identity hosts. Recorded, then aborted: the point is to learn
#: that a consent flow WOULD be started and with which scopes, never to grant
#: one.
THIRD_PARTY_IDENTITY = re.compile(
    r"accounts\.google\.com|oauth2\.googleapis\.com|www\.googleapis\.com"
    r"|appleid\.apple\.com|www\.facebook\.com/(v\d|dialog)",
    re.IGNORECASE,
)

#: The control's own URL. Nonsense on purpose: if the router ever failed to
#: abort it, the worst outcome is a 404 on a path that does not exist.
ROUTER_CONTROL_PATH = "/api/v1/__capture_router_control__"

#: Handlers worth asking the page about by name. A binding whose handler is
#: undefined on the scope is a control that does nothing when clicked.
SYMBOLS_OF_INTEREST = (
    "toggleSavedJobSearchAlerts",
    "saveJobSearch",
    "saveSearch",
    "getLink",
    "importGmailContacts",
    "sendInvites",
    "submit",
    "uploadImage",
    "jobPreferencesSave",
    "enableEditor",
    "confirmSalary",
    "onBoardingProfileSave",
)

# --- scrubbing --------------------------------------------------------------

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<![\d])(?:\+?91[-\s]?)?[6-9]\d{9}(?![\d])")
_DATA_URL_RE = re.compile(r"data:[a-z/+.-]+;base64,[A-Za-z0-9+/=]{40,}", re.IGNORECASE)
_LONG_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])")

#: Header names dropped entirely rather than masked.
_CREDENTIAL_HEADERS = frozenset(
    {"cookie", "set-cookie", "authorization", "x-csrftoken", "x-csrf-token"}
)

#: Substituted before the regexes run, so his own identifiers never survive as
#: a "long token" or a plausible-looking address.
EXTRA_SCRUB_TERMS: dict[str, str] = {}


def scrub(value: Any) -> Any:
    """Redact anything that identifies a person or authenticates a request.

    Applied at CAPTURE time, on the way into the record, so nothing
    unredacted is ever held in memory long enough to be written by accident.
    Recurses through dicts and lists because a body is a tree, and a scrubber
    that only walks strings is the defect this package has already paid for
    once (see tests/test_credential_leak.py).
    """
    if isinstance(value, str):
        out = value
        for term, replacement in EXTRA_SCRUB_TERMS.items():
            if term:
                out = out.replace(term, replacement)
        out = _DATA_URL_RE.sub(
            lambda m: "data:<REDACTED_DATA_URL len=%d>" % len(m.group(0)), out
        )
        out = _EMAIL_RE.sub("redacted@example.invalid", out)
        out = _PHONE_RE.sub("<PHONE_REDACTED>", out)
        out = _LONG_TOKEN_RE.sub("<TOKEN_REDACTED>", out)
        return out
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    return value


def scrub_headers(headers: dict) -> dict:
    """Drop credentials outright; scrub the rest.

    A masked credential still leaks its length and its shape, and a header
    block is the one place where the whole session cookie appears verbatim.
    """
    out = {}
    for name, value in headers.items():
        if name.lower() in _CREDENTIAL_HEADERS:
            out[name] = "<DROPPED: credential header, never recorded>"
        else:
            out[name] = scrub(value)
    return out


# --- the router -------------------------------------------------------------


def _describe(request: Any, why: str) -> dict:
    """Turn a request into the contract record, scrubbed on the way in."""
    body: Optional[str] = None
    body_note = None
    try:
        body = request.post_data
    except Exception as exc:  # pragma: no cover - playwright edge
        body_note = "post_data raised: %s" % exc
    if body is None:
        try:
            raw = request.post_data_buffer
        except Exception:
            raw = None
        if raw:
            body = raw.decode("utf-8", "replace")
            body_note = "decoded from post_data_buffer (%d bytes)" % len(raw)

    parsed = None
    if body:
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = None

    record = {
        "aborted_because": why,
        "method": request.method.upper(),
        "url": scrub(request.url),
        "resource_type": request.resource_type,
        "headers": scrub_headers(dict(request.headers)),
        "body_present": body is not None,
        "body_bytes": len(body.encode("utf-8", "replace")) if body else 0,
        "body_note": body_note,
    }
    if parsed is not None:
        record["body_json"] = scrub(parsed)
        record["body_keys"] = sorted(parsed.keys()) if isinstance(parsed, dict) else None
    elif body is not None:
        text = scrub(body)
        record["body_text"] = text if len(text) <= 4000 else text[:4000] + "...<TRUNCATED>"
    return record


def _router(recorded: list[dict]):
    """Abort first, ask later. Order matters and is asserted by the control."""

    def handler(route: Any, request: Any) -> None:
        url = request.url
        method = request.method.upper()
        try:
            if NEVER_REACHES_THE_WIRE.search(url):
                recorded.append(_describe(request, "forbidden_path"))
                route.abort()
                return
            if THIRD_PARTY_IDENTITY.search(url):
                recorded.append(_describe(request, "third_party_identity"))
                route.abort()
                return
            if method != "GET" and "/cdn-cgi/" not in url:
                recorded.append(_describe(request, "non_get"))
                route.abort()
                return
            route.continue_()
        except Exception:
            # A handler that raises leaves the request hanging, which is a
            # deadlock, not a delivery. Abort is the safe failure.
            try:
                route.abort()
            except Exception:
                pass

    return handler


def _prove_the_router_aborts(page: Any, recorded: list[dict]) -> dict:
    """Fire a POST from inside the page and require that it was blocked.

    This is the control for the instrument. Without it, "nothing was sent" is
    an assumption about Playwright's interception rather than a measurement of
    it, and every capture below would inherit that assumption.
    """
    before = len(recorded)
    outcome = page.evaluate(
        """(path) => fetch(path, {method: 'POST', body: 'router-control'})
             .then(r => 'DELIVERED status=' + r.status)
             .catch(e => 'REJECTED ' + e.message)""",
        ROUTER_CONTROL_PATH,
    )
    hits = [r for r in recorded[before:] if ROUTER_CONTROL_PATH in r["url"]]
    proven = bool(hits) and outcome.startswith("REJECTED")
    return {
        "control_fired": True,
        "fetch_outcome": outcome,
        "recorded_the_attempt": bool(hits),
        "router_proven": proven,
        "record": hits[0] if hits else None,
    }


# --- page interrogation -----------------------------------------------------

_PAGE_CENSUS_JS = """
(names) => {
  const out = {angular_present: typeof window.angular !== 'undefined'};

  const clicks = {};
  document.querySelectorAll('[ng-click]').forEach(el => {
    const v = el.getAttribute('ng-click');
    clicks[v] = (clicks[v] || 0) + 1;
  });
  out.ng_click_census = clicks;

  out.symbols = {};
  for (const n of names) {
    const els = document.querySelectorAll('[ng-click*="' + n + '"]');
    let defined = null;
    if (els.length && window.angular) {
      try {
        const s = window.angular.element(els[0]).scope();
        defined = !!(s && typeof s[n] === 'function');
      } catch (e) { defined = 'scope_error: ' + e.message; }
    }
    out.symbols[n] = {bindings_in_dom: els.length, handler_defined_on_scope: defined};
  }

  out.scripts = Array.from(document.querySelectorAll('script[src]')).map(s => s.src);
  out.file_inputs = Array.from(document.querySelectorAll('input[type=file]'))
    .map(i => ({id: i.id, name: i.name, accept: i.accept}));
  out.forms = Array.from(document.forms).map(f => ({
    id: f.id, name: f.name, action: f.action, method: f.method,
    fields: Array.from(f.elements).map(e => e.name || e.id).filter(Boolean)
  }));
  out.same_origin_links = Array.from(new Set(
    Array.from(document.querySelectorAll('a[href]'))
      .map(a => a.href)
      .filter(h => h.indexOf('instahyre.com') !== -1)
  ));
  return out;
}
"""


def interrogate(page: Any) -> dict:
    try:
        return page.evaluate(_PAGE_CENSUS_JS, list(SYMBOLS_OF_INTEREST))
    except Exception as exc:
        return {"census_failed": str(exc)}


# --- recipes ----------------------------------------------------------------
#
# A recipe drives ONE control with obviously-fake input. The router throws the
# request away; the record is the deliverable. Every literal below is fake by
# construction so that even a captured body carries no real third-party data.

FAKE_INVITE_EMAILS = ["not-a-real-person@example.invalid", "also-fake@example.invalid"]
FAKE_MESSAGE = "CAPTURE PROBE -- this request was aborted at the router and never sent."


#: A 1x1 red PNG, inline so that no real photograph is ever handed to a
#: capture. The uploader downscales to width<=800 and re-encodes to WebP, so a
#: 1x1 source produces a data URL small enough to read whole -- which is the
#: point: the body shape is visible and the payload is unmistakably not a
#: person.
FAKE_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
    "IQAAAABJRU5ErkJggg=="
)


def recipe_profile_image(page: Any) -> dict:
    """Hand the profile-photo input a 1x1 fake and let the page fire the upload.

    This is the one surface where shipped source cannot finish the job. The
    ``profileImageService`` factory builds ``{file_b64, title, ...otherData}``
    but NO caller ships in any of the ten bundles, so the ``otherData`` keys --
    and whether the page uses POST or PUT -- exist only on the wire.
    """
    import base64
    import tempfile

    if not page.locator("#image-input").count():
        return {"driven": False, "why": "no #image-input on this page"}

    tmp = Path(tempfile.gettempdir()) / "instahyre_capture_fake_1x1.png"
    tmp.write_bytes(base64.b64decode(FAKE_PNG_B64))
    page.locator("#image-input").set_input_files(str(tmp))
    steps = ["set #image-input to a 1x1 fake PNG"]
    page.wait_for_timeout(4000)

    # Some editors need a confirm press after the file is chosen. Only ever a
    # save/upload control -- never the delete one sitting beside it.
    for selector in ("[ng-click*='uploadImage']", "[ng-click*='saveImage']", "[ng-click*='cropImage']"):
        if page.locator(selector).count():
            page.locator(selector).first.click()
            steps.append("clicked %s" % selector)
            page.wait_for_timeout(3000)
            break
    return {"driven": True, "steps": steps}


def recipe_support_ticket(page: Any) -> dict:
    """Open the contact modal, fill it with a fake message, press submit.

    The shipped call site says the body is
    ``{candidate: <resource uri>, message, attachments: []}``; this measures
    whether the wire agrees.
    """
    steps = []
    for opener in ("[ng-click*='showContactModal']",):
        if page.locator(opener).count():
            page.locator(opener).first.click()
            steps.append("clicked %s" % opener)
            page.wait_for_timeout(1500)
            break

    for selector in (
        "textarea[ng-model='params.message']",
        "textarea[name=message]",
        "textarea",
    ):
        if page.locator(selector).count():
            page.locator(selector).first.fill(FAKE_MESSAGE)
            steps.append("filled %s" % selector)
            break
    else:
        return {"driven": False, "why": "no message textarea found on this page"}

    for selector in (
        "button[ng-click*='submit']",
        "[ng-click*='submit']",
        "button[type=submit]",
        "input[type=submit]",
    ):
        if page.locator(selector).count():
            page.locator(selector).first.click()
            steps.append("clicked %s" % selector)
            break
    else:
        return {"driven": False, "why": "no submit control found", "steps": steps}

    page.wait_for_timeout(2500)
    return {"driven": True, "steps": steps}


def recipe_referral_get_link(page: Any) -> dict:
    """Press whatever produces the referral link. Sends nothing to anyone."""
    steps = []
    for selector in (
        "[ng-click*='get_link']",
        "[ng-click*='getLink']",
        "[ng-click*='referral']",
    ):
        if page.locator(selector).count():
            page.locator(selector).first.click()
            steps.append("clicked %s" % selector)
            page.wait_for_timeout(2500)
            return {"driven": True, "steps": steps}
    return {"driven": False, "why": "no referral-link control found"}


def recipe_referral_send_invites(page: Any) -> dict:
    """Type OBVIOUSLY FAKE addresses into the invite box and press send.

    Nothing is delivered: the router aborts the POST. The addresses are
    ``example.invalid``, a reserved TLD that cannot resolve, so even a failure
    of every other guard could not reach a real person.
    """
    steps = []
    filled = False
    for selector in (
        "input[ng-model*='email']",
        "textarea[ng-model*='email']",
        "input[type=email]",
        "textarea[ng-model*='invite']",
    ):
        if page.locator(selector).count():
            page.locator(selector).first.fill(", ".join(FAKE_INVITE_EMAILS))
            steps.append("filled %s with example.invalid addresses" % selector)
            filled = True
            break
    if not filled:
        return {"driven": False, "why": "no invite input found"}

    for selector in ("[ng-click*='send_invites']", "[ng-click*='sendInvites']", "[ng-click*='invite']"):
        if page.locator(selector).count():
            page.locator(selector).first.click()
            steps.append("clicked %s" % selector)
            page.wait_for_timeout(2500)
            return {"driven": True, "steps": steps}
    return {"driven": False, "why": "no send-invites control found", "steps": steps}


def recipe_workex_save(page: Any) -> dict:
    """Press Save on the work-experience form WITHOUT editing a field.

    A PUT is a full replacement, so the value here is the complete key list the
    browser sends for an unchanged record -- which is exactly what a safe PUT
    would have to echo back.
    """
    steps = []
    for selector in (
        "[ng-click*='save_onboarding_workex']",
        "[ng-click*='saveWorkex']",
        "[ng-click*='workex']",
        "form[name*='workex'] button[type=submit]",
    ):
        if page.locator(selector).count():
            page.locator(selector).first.click()
            steps.append("clicked %s (no field edited)" % selector)
            page.wait_for_timeout(2500)
            return {"driven": True, "steps": steps}
    return {"driven": False, "why": "no workex save control found"}


def recipe_job_preferences_save(page: Any) -> dict:
    """Open the job-preferences editor and press Save WITHOUT touching a field.

    THE TOOL IS ALREADY BUILT, on SHIPPED evidence, and this recipe is how that
    entry gets upgraded to WIRE. It is not a blocker and never was: the site
    reaches the same object through a $resource factory
    (``candidateSkillsService``, ``PUT candidate_skills/:id``) whose URL, method
    and body are all readable from the bundles, and that is what
    ``constants.EP_JSP`` records. Running this changes the evidence class, not
    the contract.

    It answers ONE question the $resource path cannot. There are two save paths
    to this object, and the OTHER one -- the preference editor -- reads its
    method and URL off DOM attributes: ``cscope.saveChanges`` looks up
    ``cscope.editors[editor].apiUrl``, and the ``auctionedEditor`` directive
    stores the raw ``attrs``, so ``api-url`` and ``ng-model`` live in an HTML
    template that ships in no bundle. Whether the two paths land on the same
    resource is therefore visible only on the wire. If they diverge, that is a
    finding about EP_JSP and the tool has to be re-read against it.

    An unedited save is used deliberately: with no field touched, the body is
    the complete key list carrying every value the server already holds, which
    is exactly what a safe read-modify-write has to reproduce.

    The salary confirm modal is in the way and is not skippable: pressing Save
    calls ``showConfirmSalaryWarning()``, which returns TRUE on this account
    (experienced, and current_salary sits at 0) and RETURNS EARLY without
    saving. ``confirmSalary()`` only lowers the flag; the save has to be
    pressed a second time. A recipe that clicked Save once would record
    nothing and read as "the control does not fire".
    """
    steps = []
    enable = page.locator("[ng-click*='enableEditor'][ng-click*='preference_editor']")
    if not enable.count():
        return {"driven": False, "why": "no enableEditor control for preference_editor"}
    enable.first.click()
    steps.append("clicked enableEditor(preference_editor)")
    page.wait_for_timeout(2000)

    save = page.locator("[ng-click*='jobPreferencesSave']")
    if not save.count():
        return {"driven": False, "why": "editor opened but no jobPreferencesSave control", "steps": steps}

    save.first.click()
    steps.append("clicked jobPreferencesSave (no field edited)")
    page.wait_for_timeout(2000)

    confirm = page.locator("[ng-click*='confirmSalary']")
    if confirm.count() and confirm.first.is_visible():
        confirm.first.click()
        steps.append("dismissed the confirm-salary modal, which had blocked the save")
        page.wait_for_timeout(1000)
        save.first.click()
        steps.append("clicked jobPreferencesSave again")
        page.wait_for_timeout(2500)

    return {"driven": True, "steps": steps}


def recipe_saved_search_create(page: Any) -> dict:
    """Apply a filter, open the save-search modal, name it, press save.

    The account holds ZERO saved searches, so the alert TOGGLE has no row to
    act on and cannot be reached at all. The CREATE can: it is gated behind
    ``!defaultEmptySearch()``, which one filter satisfies. Its body is the
    thing a toggle would later PATCH, and capturing it also measures whether
    ``$resource`` strips this resource's trailing slash the way it did on the
    support endpoint.
    """
    steps = []
    # Order matters, and both orders were measured on 2026-08-23. The
    # save-search control ships on /candidate/opportunities/ but renders
    # HIDDEN there -- it belongs to /search-jobs. On /search-jobs the "Show
    # results" button starts DISABLED ("Please modify at least one search
    # criteria"), so a filter has to move before anything else can be clicked.
    filters = page.locator("[ng-click^='selectFilter']")
    if filters.count() > 1:
        filters.nth(1).click()
        steps.append("applied one sidebar filter to defeat defaultEmptySearch()")
        page.wait_for_timeout(2500)

    for selector in ("#show-results", "[ng-click*='searchCustomJobs']"):
        loc = page.locator(selector)
        if loc.count() and loc.first.is_visible() and loc.first.is_enabled():
            loc.first.click()
            steps.append("clicked %s to run the search" % selector)
            page.wait_for_timeout(4000)
            break

    opened = False
    for selector in ("#save-search", "[ng-click*='toggleSaveJobSearchModal']"):
        if page.locator(selector).count() and page.locator(selector).first.is_visible():
            page.locator(selector).first.click()
            steps.append("clicked %s" % selector)
            page.wait_for_timeout(1500)
            opened = True
            break
    if not opened:
        return {"driven": False, "why": "no save-search-modal control found", "steps": steps}

    for selector in ("input[ng-model='searchInput.name']", "input[ng-model*='searchInput']"):
        if page.locator(selector).count():
            page.locator(selector).first.fill("CAPTURE PROBE - never saved")
            steps.append("filled %s" % selector)
            break

    # What the modal offers, recorded whether or not the click below lands --
    # a miss here is a finding about the page, not a dead end.
    steps.append(
        "modal ng-clicks: %s"
        % page.evaluate(
            "() => Array.from(document.querySelectorAll('[ng-click]'))"
            ".map(e => e.getAttribute('ng-click'))"
            ".filter(v => /save|Save/.test(v))"
        )
    )

    for selector in ("[ng-click*='saveSearch']", "[ng-click='saveSearch()']"):
        if page.locator(selector).count():
            page.locator(selector).first.click()
            steps.append("clicked %s" % selector)
            page.wait_for_timeout(3000)
            return {"driven": True, "steps": steps}
    return {"driven": False, "why": "modal opened but no save button matched", "steps": steps}


RECIPES = {
    "profile_image": recipe_profile_image,
    "support_ticket": recipe_support_ticket,
    "referral_get_link": recipe_referral_get_link,
    "referral_send_invites": recipe_referral_send_invites,
    "workex_save": recipe_workex_save,
    "job_preferences_save": recipe_job_preferences_save,
    "saved_search_create": recipe_saved_search_create,
}


# --- driver -----------------------------------------------------------------


def run(urls: list[str], recipe: Optional[str], out_path: Path, timeout_s: int) -> dict:
    from playwright.sync_api import sync_playwright

    profile_dir = browser_profile_path()
    recorded: list[dict] = []
    observed: list[dict] = []
    pages: list[dict] = []
    control: dict = {}

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
        )
        try:
            context.route("**/*", _router(recorded))
            page = context.pages[0] if context.pages else context.new_page()
            page.on(
                "response",
                lambda r: observed.append(
                    {"method": r.request.method, "url": scrub(r.url), "status": r.status}
                )
                if "/api/v1/" in r.url
                else None,
            )

            for index, url in enumerate(urls):
                entry: dict = {"url": url}
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
                except Exception as exc:
                    entry["goto_error"] = str(exc)
                    pages.append(entry)
                    continue
                page.wait_for_timeout(4000)
                try:
                    page.wait_for_load_state("networkidle", timeout=12_000)
                except Exception:
                    pass

                entry["title"] = page.title()
                entry["final_url"] = page.url
                entry["cloudflare_challenge"] = "just a moment" in (entry["title"] or "").lower()
                try:
                    body_text = page.inner_text("body")
                except Exception:
                    body_text = ""
                entry["signed_in"] = "SIGN OUT" in body_text.upper() or "LOG OUT" in body_text.upper()
                entry["census"] = interrogate(page)

                if index == 0:
                    control = _prove_the_router_aborts(page, recorded)
                    entry["router_control"] = control
                    if not control.get("router_proven"):
                        entry["ABORTED_RUN"] = (
                            "the router could not be proven to block a POST; "
                            "no recipe was driven"
                        )
                        pages.append(entry)
                        break

                if recipe and index == len(urls) - 1:
                    before = len(recorded)
                    # A recipe that raises must not cost the capture. Whatever
                    # the router already recorded is the deliverable, and a
                    # missed selector is a finding about the page.
                    try:
                        outcome: Any = RECIPES[recipe](page)
                    except Exception as exc:
                        outcome = {"driven": False, "raised": "%s: %s" % (type(exc).__name__, exc)}
                    entry["recipe"] = {"name": recipe, "outcome": outcome}
                    entry["recipe"]["captured"] = recorded[before:]

                pages.append(entry)
        finally:
            try:
                context.close()
            except Exception:
                pass

    result = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "profile_dir_exists": profile_dir.exists(),
        "router_control": control,
        "pages": pages,
        "aborted_requests": recorded,
        "aborted_count": len(recorded),
        "observed_api_responses": observed,
        "safety": (
            "Every non-GET was aborted at the router, as were apply, bulk apply, "
            "logout and every third-party identity host, for all methods. Nothing "
            "recorded here was delivered."
        ),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=True), encoding="ascii")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=["recon", "recipe"])
    parser.add_argument("--url", action="append", default=[])
    parser.add_argument("--recipe", choices=sorted(RECIPES))
    parser.add_argument("--out", default=None)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument(
        "--scrub-term",
        action="append",
        default=[],
        help="extra literal to redact, as LITERAL=PLACEHOLDER",
    )
    args = parser.parse_args()

    for pair in args.scrub_term:
        literal, _, placeholder = pair.partition("=")
        EXTRA_SCRUB_TERMS[literal] = placeholder or "<REDACTED>"

    urls = args.url or [SITE + "/candidate/opportunities/"]
    if args.mode == "recipe" and not args.recipe:
        parser.error("--recipe is required in recipe mode")

    stamp = time.strftime("%H%M%S")
    out = Path(args.out) if args.out else REPO / "_audit" / "_data" / (
        "capture-%s-%s.json" % (args.recipe or args.mode, stamp)
    )
    result = run(urls, args.recipe if args.mode == "recipe" else None, out, args.timeout)
    print(json.dumps({k: v for k, v in result.items() if k != "pages"}, indent=2)[:4000])
    print("\nwrote %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
