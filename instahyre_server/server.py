"""The MCP surface.

Every docstring here is written for the agent that has to choose between these
tools, so each one says what it is *for* and what it costs in requests. Results
are shaped, never raw -- the whole point of this server over a browser is that
reading a job board should not cost thousands of tokens every time.
"""

from __future__ import annotations

import functools
import logging
import time
from typing import Any, Optional

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from . import buildinfo, constants as C
from . import lifecycle, policy, scoring, shape, skillgap
from .cache import Store, default_db_path
from .paths import display_path
from .client import InstahyreClient
from .errors import InstahyreError, InvalidFilter
from .http import InstahyreHTTP
from .session import (
    SESSION_COOKIE,
    SessionStore,
    browser_profile_path,
    check_auth,
    login_with_password,
)

log = logging.getLogger("instahyre.server")

mcp = FastMCP(
    "instahyre",
    instructions=(
        "Instahyre job search and application tools. Instahyre is a REVERSE marketplace: "
        "employers initiate contact, so the highest-value surface is inbound interest, not "
        "outbound blasting. Two facts to hold on to: the platform publishes NO salary data on "
        "any job, and NO posting dates -- do not go looking for either. Applications CANNOT be "
        "withdrawn once sent."
    ),
)

_client: Optional[InstahyreClient] = None
_sessions: Optional[SessionStore] = None


def get_client() -> InstahyreClient:
    """One lazily-built client per process, with any saved session restored."""
    global _client, _sessions
    if _client is None:
        http = InstahyreHTTP()
        _sessions = SessionStore()
        _sessions.load_into(http)
        _client = InstahyreClient(http=http, store=Store(default_db_path()))
    return _client


def get_sessions() -> SessionStore:
    get_client()
    assert _sessions is not None
    return _sessions


def handled(func):
    """Turn a typed error into an MCP tool error.

    This is the guard against the failure mode that has already bitten this
    codebase twice: a tool that swallows a problem and returns an innocent
    empty list. Here a failure raises, and the caller sees ``isError``.
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any):
        try:
            return func(*args, **kwargs)
        except InstahyreError as exc:
            detail = " ".join(
                f"{k}={v}" for k, v in exc.context.items() if v is not None and k != "path"
            )
            raise ToolError(f"[{exc.kind}] {exc.message}{(' (' + detail + ')') if detail else ''}")

    return wrapper


def progress_reporter():
    """A best-effort bridge from a blocking tool to the client's progress bar.

    Only ``instahyre_login_browser`` needs it, and only because it can sit for
    five minutes with nothing to show for itself. FastMCP runs sync tools in an
    anyio worker thread, so the async ``Context`` methods have to be driven back
    into the event loop through ``anyio.from_thread``.

    Every part of that is optional -- there may be no request context, no
    progress token, or a client that has hung up -- so this returns ``None``
    when it cannot wire itself up, and the hook it returns is called inside a
    guard on the other side. Progress never decides anything.
    """
    try:
        from fastmcp.server.dependencies import get_context

        ctx = get_context()
        if ctx is None:
            return None
        from anyio import from_thread
    except Exception:  # no request context, or a FastMCP that works differently
        return None

    def report(elapsed: float, total: float, message: str) -> None:
        from_thread.run(ctx.report_progress, elapsed, total, message)

    return report


# ---------------------------------------------------------------------------
# TIER 1 -- public. No login required.
# ---------------------------------------------------------------------------


@mcp.tool()
@handled
def instahyre_search_jobs(
    skills: Optional[list[str]] = None,
    job_functions: Optional[list[str]] = None,
    locations: Optional[list[str]] = None,
    company: Optional[str] = None,
    industries: Optional[list[str]] = None,
    company_size: Optional[str] = None,
    experience_years: Optional[int] = None,
    job_type: Optional[str] = None,
    exclude_agencies: bool = False,
    show_agency_flag: bool = False,
    offset: int = 0,
) -> dict:
    """Search live Instahyre jobs. The main discovery tool; no login needed.

    Returns up to 35 compact job records (the server floors AND ceilings the
    page at 35, so this is not tunable) plus ``total_matching`` and a
    ``next_offset`` for paging.

    Args:
        skills: Skill names, ORed. NOTE: Instahyre does not validate these -- an
            unrecognised skill silently matches nothing. If the result comes
            back empty, the ``diagnosis`` field names which skill was dead.
        job_functions: Names or ids, e.g. ["Backend Development"]. ORed.
            Call instahyre_list_job_functions for all 58.
        locations: City names, e.g. ["Bangalore"]. ORed. Case is corrected
            automatically. Use "Work From Home" for remote -- it is the only
            remote token that exists, and it covers ~8.6% of the corpus.
        company: An exact employer name. A name Instahyre does not know is
            reported as such, not silently ignored.
        company_size: "small" (1-10 staff), "medium" (~50), or "large" (200+).
        experience_years: Your years of experience; returns roles open to it.
        job_type: "full_time" (99.7% of the corpus) or "internship".
        exclude_agencies: Keep only postings made by the hiring company itself.
            ~84% of Instahyre postings come from third-party staffing agencies,
            so this is the single most useful quality filter -- but the flag
            lives on the job detail, not the search result, so switching this on
            costs up to one extra request per job on the page (cached 6h
            afterwards, and pre-warmed by instahyre_sync_index).
        show_agency_flag: Annotate each job with the agency verdict without
            filtering any out. Same request cost as exclude_agencies.
        offset: Page offset. Pass the ``next_offset`` from the previous call.

    There is NO sort argument on purpose: the API accepts a `sort` parameter and
    demonstrably ignores it. Use instahyre_rank_jobs to order results by fit.
    """
    client = get_client()
    return client.search(
        skills=skills,
        job_functions=job_functions,
        locations=locations,
        companies=company,
        industries=industries,
        company_size=company_size,
        experience_years=experience_years,
        job_type=job_type,
        offset=offset,
        exclude_agencies=exclude_agencies,
        enrich_agency=show_agency_flag,
    )


@mcp.tool()
@handled
def instahyre_get_job(job_id: int, full_description: bool = False) -> dict:
    """Full detail for one job: description, experience band, recruiter, agency verdict.

    Adds what search cannot show -- the job description, the ``workex_min``/
    ``workex_max`` band, the named recruiter, and whether a staffing agency
    posted it. Costs one request (cached 6h).

    A job id that does not exist raises a not_found error rather than returning
    an empty record.

    Args:
        job_id: The numeric id from a search result.
        full_description: Return the whole description instead of the first
            ~1200 characters. Descriptions run ~1,800 characters on average.
    """
    client = get_client()
    return client.get_job(
        job_id, description_chars=None if full_description else shape.DEFAULT_DESCRIPTION_CHARS
    )


@mcp.tool()
@handled
def instahyre_get_company(name: str) -> dict:
    """An employer's profile and every live job they have open.

    Doubles as a membership oracle: ``exists: false`` is a definite "Instahyre
    has no employer under this exact name", distinct from an employer that
    exists with zero live postings. Matching is on the full name string, not a
    substring. One request.

    Args:
        name: The employer's name as Instahyre spells it, e.g. "Amazon".
    """
    return get_client().get_company(name)


@mcp.tool()
@handled
def instahyre_list_job_functions(tech_only: bool = False) -> dict:
    """The 58 job functions, with ids, for use as a ``job_functions`` filter.

    Cached 30 days; effectively free after the first call.

    Args:
        tech_only: Return only functions under a technical category.
    """
    rows = get_client().taxonomy.job_functions()
    if tech_only:
        rows = [r for r in rows if r.get("is_tech")]
    return {"count": len(rows), "job_functions": rows}


@mcp.tool()
@handled
def instahyre_list_locations(search: Optional[str] = None) -> dict:
    """The location tokens Instahyre accepts, grouped by state or region.

    Worth calling before a search with an unusual city: the API validates
    locations server-side and is case-sensitive, so "bangalore" is a hard 400
    while "Bangalore" is 7,000+ jobs. Cached 30 days.

    Args:
        search: Filter the list, case-insensitively.
    """
    rows = get_client().taxonomy.locations()
    if search:
        needle = search.casefold()
        rows = [r for r in rows if needle in r["value"].casefold() or needle in r["name"].casefold()]
    grouped: dict[str, list[str]] = {}
    for row in rows:
        grouped.setdefault(row.get("group") or "other", []).append(row["value"])
    return {
        "count": len(rows),
        "by_group": grouped,
        "note": "'Work From Home' is the only remote token. Hybrid is not modelled at all.",
    }


@mcp.tool()
@handled
def instahyre_list_industries() -> dict:
    """The 74 industry types, with ids, for the ``industries`` filter.

    Also the lookup that turns the industry facet block (which returns bare ids)
    into names. Cached 30 days.
    """
    rows = get_client().taxonomy.industries()
    return {"count": len(rows), "industries": rows}


@mcp.tool()
@handled
def instahyre_market_stats(
    skills: Optional[list[str]] = None,
    job_functions: Optional[list[str]] = None,
    locations: Optional[list[str]] = None,
    industries: Optional[list[str]] = None,
    company_size: Optional[str] = None,
    experience_years: Optional[int] = None,
) -> dict:
    """Market aggregates for a slice, with no job records at all. One request.

    Answers "how many backend roles are open in Bangalore right now, split by
    company size and seniority" in a single call, because Instahyre attaches
    faceted counts to every search response for free.

    Two caveats the output repeats, because they are easy to misread: every
    ``top_*`` list is truncated to 4 entries server-side, and the experience
    bands OVERLAP -- they are not a histogram and they sum above the total.

    Repeated calls build a time series in the local index, so a second call on
    the same filters reports the change since the previous reading.
    """
    return get_client().market_stats(
        skills=skills,
        job_functions=job_functions,
        locations=locations,
        industries=industries,
        company_size=company_size,
        experience_years=experience_years,
    )


@mcp.tool()
@handled
def instahyre_sync_index(
    skills: Optional[list[str]] = None,
    job_functions: Optional[list[str]] = None,
    locations: Optional[list[str]] = None,
    experience_years: Optional[int] = None,
    company_size: Optional[str] = None,
    max_pages: int = 5,
) -> dict:
    """Page through a slice, record every job locally, and report what is NEW.

    This is the closest thing to a "posted today" feed that Instahyre allows:
    the API exposes no date field anywhere, so the local ``first_seen``
    timestamp written by this tool is the only freshness signal that will ever
    exist. Run it on a slice you care about, then run it again later -- the
    second run tells you what appeared in between.

    Costs one request per page (default 5 pages = 175 jobs), paced ~1.2s apart.

    Args:
        max_pages: Pages to walk, 35 jobs each. Keep modest; this is the only
            tool here that makes more than a couple of requests.
    """
    return get_client().sync_index(
        skills=skills,
        job_functions=job_functions,
        locations=locations,
        experience_years=experience_years,
        company_size=company_size,
        max_pages=max_pages,
    )


@mcp.tool()
@handled
def instahyre_rank_jobs(
    my_skills: Optional[list[str]] = None,
    my_experience_years: Optional[float] = None,
    my_location: Optional[str] = None,
    skills: Optional[list[str]] = None,
    job_functions: Optional[list[str]] = None,
    locations: Optional[list[str]] = None,
    experience_years: Optional[int] = None,
    company_size: Optional[str] = None,
    exclude_agencies: bool = False,
    top_n: int = 10,
    explain: bool = False,
) -> dict:
    """Search, then rank the results by fit against your own skills.

    Login is OPTIONAL, and which half you get depends only on how you call it.
    Pass ``my_skills`` -- or put them in the shared config -- and this needs no
    session at all. Omit them and it falls back to the skills on his live
    Instahyre profile, which does need one; with no session that fallback comes
    back empty and the tool raises the same "no skills to score against" it
    always has, never an auth failure. ``skills_source`` in the result names
    which of the three actually supplied the numbers.

    Ranks ONE page -- the 35 jobs the server returns for these filters, not the
    whole matching set. ``total_matching`` says how many exist; narrow the
    filters if that number is large and you want the genuine top of the field.

    The API's own ``sort`` parameter is inert, so this is the only ordering that
    works. Scoring is the shared ``jobcore`` engine -- the same one the Naukri
    and Uplers servers use, under the same ``jobhunt.json`` policy, so a fit
    score means the same thing on all three. Expect absolute scores to run low:
    Instahyre lists many keywords per job, and the skill component is the share
    of the JOB's skills you cover, so the useful signal is the ordering rather
    than the number.

    Skill matching runs on the search result's keyword list; experience uses the
    job's band where a cached detail supplies it. No salary component can ever
    contribute, because Instahyre publishes no salary data.

    ``scoring_hash`` in the result identifies the weights, bonuses and bands
    that produced these numbers; two rankings whose ``scoring_hash`` differs are
    not directly comparable. ``policy_rev`` / ``policy_hash`` are the wider
    config fingerprint -- they also cover the candidate half, so they can differ
    between two rankings that ARE comparable. ``instahyre_config()`` says what
    changed.

    Args:
        my_skills: Your skills, e.g. ["Node.js", "TypeScript", "AWS"]. Omit to
            fall back, in that order, to ``candidate.skills`` from the shared
            config and then to the skills on his live Instahyre profile (the
            ones instahyre_get_profile reports). ``skills_source`` in the
            result says which one won; all three empty is the error.
        my_experience_years: Your years of experience, for the band check.
            Omit to fall back to ``candidate.years_experience`` and then to the
            profile's total experience. ``experience_years_source`` names the
            winner. Unlike the skills, no value here is not an error -- the
            experience half simply scores as unknown.
        my_location: Your city. Omit to use ``candidate.locations``.
        top_n: How many ranked jobs to return.
        explain: Add the arithmetic behind each score to that job's own row --
            the weights, the base skills/experience split, every bonus and the
            cap, and the ``scoring_hash`` it was computed under. Costs no extra
            request: this is working the engine already did and normally
            discards. Off by default because the block is several times the
            size of the row it explains.
    """
    snapshot = policy.current()
    scoring_args = policy.scoring_args(snapshot)
    client = get_client()

    # The account profile is fetched at most once, by whichever of the two
    # fallbacks below reaches it first -- skills and years are independent, and
    # a config supplying one but not the other must not cost two requests. With
    # no session cookie it is not fetched AT ALL: this tool works without a
    # login, and spending a request to be told 401 on every public-tier call
    # would be a quiet tax on that. A missing cookie is not a guess -- nothing
    # authenticated can succeed without one.
    signed_in = bool(client.http.cookies.get(SESSION_COOKIE))
    account: Optional[dict] = None

    def account_profile() -> dict:
        nonlocal account
        if account is None:
            account = (
                client.inbound.profile_for_scoring()
                if signed_in
                else {"skills": [], "years": None, "unavailable": "no_session_cookie"}
            )
        return account

    # Every source is REPORTED, because a ranking whose caller cannot tell
    # whether it scored against their argument, the shared config or the live
    # account is not a number anyone can act on.
    skills_source = "argument"
    if not my_skills:
        my_skills = list(snapshot.candidate.skills)
        skills_source = "shared_config"
    if not my_skills:
        # The one this tool used to skip. It could already read the profile --
        # instahyre_get_profile is right there on the same client -- and simply
        # never asked, so a bare call died on an empty config block while his
        # twenty account skills sat one request away.
        my_skills = list(account_profile()["skills"])
        skills_source = "account_profile"
    if not my_skills:
        # Name all three, and say which silence the third one is: "no session"
        # and "a profile with no skills on it" are different problems with
        # different fixes, and a caller cannot act on them being merged.
        unavailable = account_profile()["unavailable"]
        if unavailable == "no_session_cookie":
            why = "not attempted, there is no session -- instahyre_auth_status confirms"
        elif unavailable:
            why = "could not be read: %s" % unavailable
        else:
            why = "read, and it lists none"
        raise InvalidFilter(
            "No skills to score against, and all three sources came up empty: the my_skills "
            "argument, candidate.skills in the shared config (instahyre_config() says where "
            "that file is, or would be), and the skills on his live Instahyre account profile "
            "(instahyre_get_profile -- %s). Passing my_skills=[...] needs neither of the "
            "other two." % why,
            field="my_skills",
        )

    experience_years_source: Optional[str] = "argument"
    if my_experience_years is None:
        my_experience_years = snapshot.candidate.years_experience
        experience_years_source = "shared_config"
    if my_experience_years is None:
        my_experience_years = account_profile()["years"]
        experience_years_source = "account_profile"
    if my_experience_years is None:
        # Not an error: the experience half scores as unknown and the skills
        # half still ranks. Naming no source is the honest report of that.
        experience_years_source = None

    result = client.search(
        skills=skills,
        job_functions=job_functions,
        locations=locations,
        experience_years=experience_years,
        company_size=company_size,
        exclude_agencies=exclude_agencies,
        enrich_agency=exclude_agencies,
        max_skills=50,
    )
    ranked = []
    for job in result["jobs"]:
        lo, hi = scoring.parse_experience_range(job.get("experience_years"))
        verdict = scoring.score_job(
            job_skills=job.get("skills") or [],
            profile_skills=my_skills,
            experience_min=lo,
            experience_max=hi,
            profile_years=my_experience_years,
            job_location=(job.get("locations") or [None])[0],
            profile_location=my_location,
            explain=explain,
            **scoring_args,
        )
        entry = {
            "id": job.get("id"),
            "title": job.get("title"),
            "company": job.get("company"),
            "locations": job.get("locations"),
            "fit_score": verdict.get("overall_score"),
            "recommendation": verdict.get("recommendation"),
            "matched_skills": (verdict.get("skill_match") or {}).get("matched"),
        }
        if explain:
            # Indexed, not ``.get()``: if jobcore ever stops emitting the block
            # the caller asked for, that must raise rather than quietly land a
            # ``None`` under the key.
            entry["explain"] = verdict["explain"]
        if job.get("posted_by_agency") is not None:
            entry["posted_by_agency"] = job["posted_by_agency"]
        ranked.append(entry)
    ranked.sort(key=lambda r: (r["fit_score"] is None, -(r["fit_score"] or 0)))
    return {
        "ranked_jobs": ranked[:top_n],
        "scored": len(ranked),
        "total_matching": result.get("total_matching"),
        "scoring_engine": scoring.ENGINE,
        "scored_against_skills": list(my_skills),
        "skills_source": skills_source,
        "scored_against_experience_years": my_experience_years,
        "experience_years_source": experience_years_source,
        "note": "Ranked locally -- the API's own sort parameter is accepted but ignored.",
        **policy.summary(snapshot),
    }


# ---------------------------------------------------------------------------
# AUTH -- the gate to the authenticated tier.
# ---------------------------------------------------------------------------


@mcp.tool()
@handled
def instahyre_auth_status() -> dict:
    """Is there a live Instahyre session? Answered by asking the server.

    Returns ``authenticated: false`` when logged out -- this checks an endpoint
    that 401s without a session rather than looking for a file on disk, so a
    false here is a measurement, not a guess. One cheap request.
    """
    client = get_client()
    status = check_auth(client.http)
    saved = get_sessions().read()
    if saved.get("saved_at"):
        status["session_saved_at"] = saved["saved_at"]
        status["login_method"] = saved.get("method")
    return status


@mcp.tool()
@handled
def instahyre_login(email: str, password: str) -> dict:
    """Sign in with an Instahyre email and password. No browser required.

    Instahyre issues a CSRF token on every API response, so this authenticates
    over plain HTTP -- no Playwright, no window, ~2 seconds. The session cookie
    is saved to disk and reused across restarts. The password is used for a
    single request and is never logged, cached or written anywhere.

    If the account signs in with Google instead, use instahyre_login_browser.

    Args:
        email: The account's email address.
        password: The account password.
    """
    client = get_client()
    login_with_password(client.http, email, password)
    # Verify BEFORE writing anything. The POST coming back 200 with a cookie is
    # not a session -- and saving first meant a login the server went on to
    # reject still overwrote a jar that had been working.
    status = check_auth(client.http)
    if status.get("authenticated") is not True:
        return {
            "authenticated": status.get("authenticated"),
            "method": "password",
            "session_saved": False,
            "reason": status.get("reason"),
            "verified_by": status.get("checked_against"),
        }
    saved = get_sessions().save_from(client.http, method="password")
    return {
        "authenticated": True,
        "method": "password",
        "session_saved": saved.get("has_session"),
        "verified_by": status.get("checked_against"),
    }


@mcp.tool()
@handled
def instahyre_login_browser(wait_seconds: int = 300) -> dict:
    """Open a browser window so you can sign in by hand -- for Google sign-in.

    THIS TOOL BLOCKS AND THE WINDOW STAYS OPEN. It returns only when an
    authenticated API call actually succeeds, or when ``wait_seconds`` runs
    out -- up to five minutes by default. Tell the human to go and sign in;
    do not treat the delay as a hang and do not call it again in parallel.

    Use it when the account uses "Continue with Google", which is a redirect
    flow no HTTP client can complete. A visible Chromium window opens on
    Instahyre's login page against a persistent profile. The profile persists,
    so later runs usually find the session already live and return in about a
    second.

    A ``sessionid`` cookie is NOT the finish line -- Instahyre issues one to
    signed-out visitors, so this polls ``/api/v1/job_category/`` until it
    answers 200. On a timeout, or if you close the window, it returns
    ``authenticated: false`` with a reason rather than claiming success.

    This is the ONLY tool in this server that starts a browser. All data
    fetching is plain HTTP.

    Args:
        wait_seconds: How long to leave the window open for you to finish.
    """
    from .auth import login_via_browser

    client = get_client()
    return login_via_browser(
        client.http,
        get_sessions(),
        wait_seconds=wait_seconds,
        on_progress=progress_reporter(),
    )


@mcp.tool()
@handled
def instahyre_session_info(verify_live: bool = True) -> dict:
    """What the session is, how long it has left, and how to renew it.

    THE FIELD TO READ FIRST IS ``live_check``, not ``authenticated``.
    ``authenticated`` is true or false only when the endpoint actually
    answered; it is **null** whenever the check could not be completed, and a
    null is not a no. ``live_check.why_not`` says which it is.

    ``credential.expires_at`` comes from a place worth knowing about. The saved
    cookie jar this server sends from stores names and values only -- it holds
    no dates at all -- so the expiry is read out of the persistent browser
    profile's own SQLite cookie jar, from a COPY, with no browser launched and
    no cookie value ever fetched. Those are two different stores and they can
    hold two different sessions; ``credential.expiry_source`` says so in full
    rather than blurring them. ``expired`` is null, never false, when no date
    could be read.

    No credential VALUE is returned by this tool, in any field, ever. Name,
    presence, format and expiry only.

    Args:
        verify_live: True spends ONE request asking Instahyre for a verdict.
            False costs nothing at all -- no network, no browser -- and reports
            the on-disk facts with ``authenticated`` null. Use False when the
            question is "what have I got saved", True when it is "does it work".
    """
    if not verify_live:
        # Deliberately NOT get_client(). The offline answer must cost nothing,
        # and building the client would open sqlite, restore the cookies and
        # install a process-wide global -- changing the thing being described.
        return lifecycle.session_info(
            store=_sessions if _sessions is not None else SessionStore(),
            profile_dir=browser_profile_path(create=False),
            http=None,
            verify_live=False,
        )
    client = get_client()
    return lifecycle.session_info(
        store=get_sessions(),
        profile_dir=browser_profile_path(create=False),
        http=client.http,
        verify_live=True,
    )


@mcp.tool()
@handled
def instahyre_reauth() -> dict:
    """Renew the session silently from the browser profile. No password, no window.

    Use this FIRST when a tool reports auth_required. The persistent Chrome
    profile that instahyre_login_browser signed in to keeps its own long-lived
    ``sessionid``, and that one outlives the copy saved beside this server. This
    re-opens that profile HEADLESS, re-harvests its cookies and puts them to
    the live endpoint. It takes no arguments, opens no window, types nothing,
    and cannot wait for a human -- there would be nowhere for one to act.

    ``renewed: true`` requires the endpoint to have answered 200. A fresh
    ``sessionid`` appearing is NOT enough and is never treated as enough:
    Instahyre issues one to signed-out visitors, which is exactly the bug this
    server shipped once and now tests against.

    On any other outcome the previously saved session is put back byte for
    byte, so a failed renew cannot cost a session that was already working, and
    ``reason`` names instahyre_login_browser as the way back in.
    """
    client = get_client()
    return lifecycle.reauth(
        http=client.http,
        store=get_sessions(),
        profile_dir=browser_profile_path(create=False),
    )


@mcp.tool()
@handled
def instahyre_logout() -> dict:
    """Forget the saved session cookies on this machine.

    Local only. It clears the saved cookie jar and this process's cookies; it
    does NOT end the session on Instahyre's side -- this server has no sign-out
    call -- and it does NOT touch the persistent browser profile.

    That last part is why ``recover_by`` names instahyre_reauth first: the
    profile still holds a live sessionid, so getting back in usually costs one
    silent headless re-harvest and no password.
    """
    return lifecycle.logout(
        http=get_client().http,
        store=get_sessions(),
        profile_dir=browser_profile_path(create=False),
    )


@mcp.tool()
@handled
def instahyre_server_info() -> dict:
    """What this server is, what code it is running, and what it cannot do.

    START HERE WHEN BEHAVIOUR DISAGREES WITH THE SOURCE. ``build.code.commit``
    is the commit this PROCESS was started from, resolved once at import and
    frozen. Compare it against the checkout on disk::

        git rev-parse HEAD          # run inside the instahyre checkout

    Equal means the running code IS the committed code, and a bug you can see
    here is a real bug. DIFFERENT means the process is stale: the fix is on
    disk and this server has never loaded it, so nothing you observe about its
    behaviour is evidence about the current source. RESTART THE SERVER FIRST --
    debugging a stale process is how one bug got re-diagnosed as a regression
    four separate times in a single day.

    Two more fields carry the same warning. ``build.code.dirty`` says the
    loaded code differs from that commit, which is normal mid-fix and means the
    hash alone does not identify what is running. ``build.jobcore`` is the
    SEPARATE commit of the scoring library: this server's fit scores are
    jobcore's arithmetic, installed editable from a sibling checkout, so a
    stale jobcore moves every score while this server's own commit matches
    disk perfectly. Check both.

    ``build.process.started_at`` and ``pid`` say which process and since when,
    which is what tells two accidentally-running servers apart. Under
    ``config``, ``policy_hash`` and ``scoring_hash`` fingerprint the config the
    same code is running under -- the same commit scores differently under two
    ``jobhunt.json`` files, and this server shares that file with the Naukri
    and Uplers servers.

    Also reports the local index size, request count this process, and the
    known-absent data fields. Costs no request.
    """
    # DELIBERATELY NOT get_client(). This tool is reached for when the server is
    # already suspect, and building the client would open the sqlite store,
    # restore the session cookies and install a process-wide global -- changing
    # the thing under investigation in order to describe it. It reads the client
    # if one exists and reports honestly when one does not.
    #
    # NOT a claim of total inertness, and the difference is worth stating rather
    # than glossing: default_db_path() below still mkdirs the state directory.
    # That is pre-existing, idempotent, and creates no file; what is removed
    # here is the client, the sqlite connection and the global.
    client = _client
    if client is None:
        live = {
            "requests_this_process": 0,
            "min_seconds_between_requests": C.DEFAULT_MIN_INTERVAL_S,
            # NOT zeros. An index nobody opened is not an empty index, and
            # reporting {"jobs_indexed": 0} would send a reader hunting for a
            # sync problem that does not exist.
            "index": {
                "status": "not read -- no client exists in this process yet",
                "why": (
                    "this tool does not build one; call any data tool first, "
                    "or instahyre_sync_index to populate the index"
                ),
            },
        }
    else:
        live = {
            "requests_this_process": client.http.request_count,
            "min_seconds_between_requests": client.http.min_interval,
            "index": client.store.index_stats(),
        }
    return {
        "server": "instahyre",
        "tier": "public tools always live; authenticated tools need instahyre_login",
        # Frozen at import -- see instahyre_server.buildinfo. Re-resolving here
        # would make a stale process report the commit sitting on disk, which
        # reads as confirmation that the fix is loaded and is false.
        "build": buildinfo.build_block(),
        "scoring_engine": scoring.ENGINE,
        "scoring_engine_version": scoring.ENGINE_VERSION,
        "config": policy.summary(),
        "state_dir": display_path(str(default_db_path().parent)),
        **live,
        "not_available_on_this_platform": {
            "salary": "0% of jobs disclose pay; no structured field exists",
            "posting_date": "no date field on any endpoint; use instahyre_sync_index instead",
            "sort": "the API accepts a sort parameter and ignores it; use instahyre_rank_jobs",
            "applicant_counts": "no competition signal is published",
            "hybrid_vs_onsite": "only 'Work From Home' is modelled; the rest is unlabelled",
            "saved_jobs": "no bookmark feature exists; only saved SEARCHES, and no job-side equivalent",
            "message_bodies": "the inbox exposes an unread count only; threads need a conv_id no endpoint lists",
            "opportunity_detail_route": "there is none; a queue record is found by scanning the queue",
        },
        "deliberately_not_built": {
            "apply_bulk": (
                "Instahyre's API has it. It is a one-way door across a whole queue at once and "
                "is permanently out of scope."
            ),
            "profile_writes_beyond_skills_and_three_scalars": (
                "Skills and three candidate-level scalars are writable and verified. "
                "Everything on the job-search-profile sub-object (notice period, salary, "
                "preferred locations, job-search status) is NOT: those need the whole "
                "object PUT back, a contract this server has not verified, so it refuses "
                "them by name rather than guessing."
            ),
            "inbox_writes": (
                "Sending, replying, starring and marking read all exist on the API and "
                "none is reachable here -- every inbox request is checked against a list "
                "of mutating path fragments first."
            ),
        },
        "irreversible_tools": ["instahyre_apply", "instahyre_decline_opportunity"],
        "page_size": C.PAGE_SIZE,
        "opportunity_page_size": C.OPP_DEFAULT_LIMIT,
    }


@mcp.tool()
@handled
def instahyre_config(section: Optional[str] = None) -> dict:
    """The scoring policy this server is using, and where it came from.

    Read-only, and free -- it opens one small JSON file. Call it when a fit
    score looks wrong, when two rankings disagree, or before trusting a
    comparison against the Naukri or Uplers servers: all three read the SAME
    ``jobhunt.json``, so a score means the same thing on all three only when
    the ``scoring_hash`` matches. Both fingerprints are reported, and they
    answer different questions -- ``scoring_hash`` covers the arithmetic alone
    and is the one every scored result stamps, while ``policy_hash`` also
    covers the candidate block, so two comparable scores can sit under two
    different ``policy_hash`` values.

    ``source: null`` means no config file was found and the built-in defaults
    are in force -- which is the shipped behaviour, not a fault. ``searched``
    then lists every path that was tried, so "why is my file not being read"
    is answerable without guessing.

    Worth knowing about what this file CANNOT do. ``tier_c_refusals`` names
    any key the loader refused to take from the file at all: agent enablement,
    agent mode and apply thresholds live in Python and are not loadable, on
    any server in this family. This server additionally has no agent, no
    scheduler and no unattended apply path -- ``instahyre_apply`` needs
    ``confirm=True`` from a human every time, and no config value changes that.

    Writes are deliberately absent here. Edit the file, or use the Naukri
    server's write tool; this server reads.

    Args:
        section: Narrow to "candidate", "scoring", "server" or "provenance".
            Omit for everything.
    """
    try:
        return policy.report(section)
    except ValueError as exc:
        raise InvalidFilter(str(exc), field="section") from exc


# ---------------------------------------------------------------------------
# TIER 2 -- authenticated. The inbound side, which is what this platform is for.
# ---------------------------------------------------------------------------
#
# Instahyre is a REVERSE marketplace: employers put a candidate into a curated
# queue and recruiters open his resume. Outbound search is the commodity half;
# everything below is the half that is actually scarce. Ordered by value:
# triage the queue, see who looked, then tune the profile that feeds both.


@mcp.tool()
@handled
def instahyre_inbound_digest(
    rank_against_my_profile: bool = True, top_n: int = 8, explain: bool = False
) -> dict:
    """What needs attention on Instahyre today. Start here.

    One call answers the only question this platform really poses: has anyone
    engaged, and what should be looked at first. Pulls the queue badge, who
    viewed the resume, unread recruiter messages, the top-scoring untouched
    opportunities, and anything that appeared since the last run.

    Costs about five requests. Read this before instahyre_list_opportunities --
    it is the same data, already triaged.

    Args:
        rank_against_my_profile: Also score each opportunity with the shared
            jobcore engine using the skills on his own Instahyre profile, so
            scores are comparable with the Naukri server's. Costs one extra
            (cached) request. Instahyre's own score still drives the order.
        top_n: How many opportunities to surface.
        explain: Add the arithmetic behind each jobcore score to that
            opportunity's own row -- the weights, the base skills/experience
            split, every bonus and the cap, and the ``scoring_hash`` it was
            computed under. Costs no extra request: this is working the engine
            already did and normally discards. Does nothing when
            ``rank_against_my_profile`` is False, because then no jobcore score
            was computed and there is nothing to explain. Off by default
            because the block is several times the size of the row it explains.
    """
    client = get_client()
    inbound = client.inbound
    digest: dict[str, Any] = {}

    digest["opportunities_waiting"] = inbound.navbar_count()
    digest["recruiter_activity"] = inbound.activity_counts()

    viewed = inbound.activity("viewed", limit=10)
    if viewed["events"]:
        digest["who_looked_at_you"] = viewed["events"]
        digest["activity_timing_note"] = viewed["timing_note"]
    else:
        digest["who_looked_at_you"] = []
        digest["activity_note"] = "No recruiter has opened this resume yet."

    digest["messages"] = inbound.unread_messages()

    queue = inbound.list_opportunities(interest="pending", limit=C.OPP_MAX_LIMIT)
    top = queue["opportunities"][:top_n]

    if rank_against_my_profile and top:
        my_skills = inbound.profile_skills()
        if my_skills:
            snapshot = policy.current()
            scoring_args = policy.scoring_args(snapshot)
            digest["scored_against_skills"] = my_skills
            for entry in top:
                verdict = scoring.score_job(
                    job_skills=entry.get("skills") or [],
                    profile_skills=my_skills,
                    profile_years=None,
                    explain=explain,
                    **scoring_args,
                )
                entry["fit_score"] = verdict.get("overall_score")
                entry["matched_skills"] = (verdict.get("skill_match") or {}).get("matched")
                if explain:
                    # Reachable only inside this branch, which is the point:
                    # with rank_against_my_profile False there is no jobcore
                    # score on these rows, so there is nothing to explain.
                    # Indexed, not ``.get()`` -- a missing block must raise.
                    entry["explain"] = verdict["explain"]
            digest["scoring_engine"] = scoring.ENGINE
            digest.update(policy.summary(snapshot))
        else:
            digest["scoring_note"] = (
                "No skills on the Instahyre profile, so nothing to score against. "
                "instahyre_get_profile lists the gaps."
            )

    digest["top_opportunities"] = top
    digest["queue_total"] = queue.get("total_matching")
    digest["score_note"] = shape.SCORE_NOTE
    if queue.get("queue_recalculated_at"):
        digest["queue_recalculated_at"] = queue["queue_recalculated_at"]
    if queue.get("diagnosis"):
        digest["queue_diagnosis"] = queue["diagnosis"]

    new_jobs = client.store.jobs_first_seen_after(time.time() - 86400, limit=15)
    digest["first_seen_in_last_24h"] = [
        {"job_id": j["id"], "title": j["title"], "company": j["company"]} for j in new_jobs
    ]
    digest["freshness_note"] = (
        "Instahyre publishes no posting or match date anywhere, so 'first seen' is this "
        "server's own local record. It only becomes meaningful once these tools have run "
        "more than once."
    )
    return digest


@mcp.tool()
@handled
def instahyre_list_opportunities(
    interest: str = "pending",
    location: Optional[str] = None,
    company_size: Optional[str] = None,
    job_type: Optional[str] = None,
    industry_id: Optional[int] = None,
    limit: int = 30,
    offset: int = 0,
    include_unindexed: bool = False,
) -> dict:
    """The curated queue: roles employers matched TO him, with match scores.

    This is the inbound side and it is where this platform pays. Unlike
    instahyre_search_jobs, every record here was selected by Instahyre for this
    profile and carries a real match score. Returned highest score first.

    One request. An empty result always says why it is empty.

    Args:
        interest: "pending" (not yet acted on), "interested" (already applied),
            or "not_interested" (already declined). Applications and declines
            are the same queue in a different state -- there is no separate
            applications endpoint on this platform.
        location: A single city, e.g. "Bangalore", or "Work From Home".
        company_size: "small", "medium" or "large".
        job_type: "full_time" or "internship".
        industry_id: An industry id from instahyre_opportunity_counts.
        limit: Records per page, up to 1000. The queue is small enough that
            asking for all of it in one call is normal here.
        include_unindexed: Instahyre serves this queue from two resources that
            disagree. The default matches the count shown on the website; this
            switches to the wider one, which returns roughly 15 more records
            that their search index has dropped.
    """
    return get_client().inbound.list_opportunities(
        interest=interest,
        location=location,
        company_size=company_size,
        job_type=job_type,
        industry_id=industry_id,
        limit=limit,
        offset=offset,
        full_queue=include_unindexed,
    )


@mcp.tool()
@handled
def instahyre_get_opportunity(opportunity_id: str, full_description: bool = False) -> dict:
    """Everything about one matched opportunity, assembled from three sources.

    The queue record gives the match score and whether it has been actioned;
    the public job page adds the description, experience band, named recruiter
    and agency verdict; a third call lists what else that employer has open for
    him. Two or three requests, cached.

    Args:
        opportunity_id: The long numeric string ``id`` from
            instahyre_list_opportunities -- NOT the ``job_id``. A job id will
            not resolve here and raises rather than returning an empty record.
        full_description: Return the whole description instead of ~1200 chars.
    """
    return get_client().inbound.get_opportunity(
        opportunity_id, full_description=full_description
    )


@mcp.tool()
@handled
def instahyre_opportunity_counts() -> dict:
    """Facet counts across the whole queue: status, location, industry, employer.

    One request, no records. The fastest way to see the shape of the inbound
    pipeline -- how many are untouched, where they are, and which employers are
    most active -- before pulling any of it.

    ``by_status`` is also the application ledger: "interested" is how many were
    applied to, "not_interested" how many were declined.
    """
    return get_client().inbound.opportunity_counts()


@mcp.tool()
@handled
def instahyre_recruiter_activity(kind: str = "viewed", limit: int = 25) -> dict:
    """Who opened this resume, and for which role. The most perishable signal here.

    A recruiter who looked today is reachable in a way they will not be next
    week, which is exactly what a human checking a website daily handles badly.
    One request.

    Note the two different companies on each event: ``recruiter_company`` is who
    made the search (usually a staffing agency on this platform) and
    ``hiring_company`` is who the role is actually for. They are frequently not
    the same firm.

    Args:
        kind: "viewed" (opened the resume), "contacted" (reached out), or
            "not_shortlisted" (looked and passed). Those three are exactly the
            tabs the website shows.
        limit: Maximum events to return.
    """
    inbound = get_client().inbound
    result = inbound.activity(kind, limit=limit)
    result["all_tabs"] = inbound.activity_counts()
    return result


@mcp.tool()
@handled
def instahyre_list_applications() -> dict:
    """Every Instahyre application and decline on this account, with its state.

    There is no applications endpoint on this platform -- an application is a
    queue record in a different state -- so this reads both states and reports
    them together. Two requests.

    Zero applications is a real and common answer here: on a reverse
    marketplace the normal move is to wait for the queue and act selectively,
    not to accumulate applications.
    """
    inbound = get_client().inbound
    applied = inbound.list_opportunities(interest="interested", limit=C.OPP_MAX_LIMIT)
    declined = inbound.list_opportunities(interest="not_interested", limit=C.OPP_MAX_LIMIT)
    return {
        "applied": applied["opportunities"],
        "applied_count": applied.get("total_matching"),
        "declined": declined["opportunities"],
        "declined_count": declined.get("total_matching"),
        "note": (
            "Instahyre applications cannot be withdrawn, so this list only ever grows. "
            "Saved or bookmarked jobs do not exist on this platform at all."
        ),
    }


@mcp.tool()
@handled
def instahyre_get_profile() -> dict:
    """His Instahyre profile, plus what is missing and what each gap costs.

    The profile is the only lever on this platform that compounds: it decides
    which employers ever see him, so a gap here suppresses every future match
    cycle rather than one application. Gaps are ranked by consequence.

    Phone and email are deliberately NOT returned -- only whether they are on
    file. Two requests on a cold cache, one afterwards.
    """
    return get_client().inbound.profile()


@mcp.tool()
@handled
def instahyre_account_settings() -> dict:
    """Visibility, notification and blocked-employer settings.

    Worth checking when the queue looks thin: a private profile stops employers
    finding him at all, and blocked employers are silently excluded.

    Instahyre echoes password fields in this payload. They are stripped before
    this returns and are never cached or logged.
    """
    return get_client().inbound.account_settings()


@mcp.tool()
@handled
def instahyre_apply(opportunity_id: str, confirm: bool = False) -> dict:
    """Apply to ONE matched opportunity. IRREVERSIBLE -- read this first.

    Instahyre applications CANNOT BE WITHDRAWN. Their own FAQ says the
    application is sent automatically by the system, so there is no undo, no
    support path, and the employer sees it immediately. Treat every call as
    permanent and as carrying his real reputation.

    With ``confirm=False`` (the default) NOTHING is sent: it returns the exact
    request that would go out, plus the role, the employer and the match score,
    so a human can look before anything happens. Always show that preview and
    get an explicit yes before calling again with ``confirm=True``.

    There is deliberately no bulk apply in this server. Instahyre's API has one;
    exposing it would make a single call irreversible across a whole queue.

    A note on provenance, because it matters for an action that cannot be
    undone: the request shape was read out of Instahyre's own shipped frontend,
    never by sending a trial application. No request of this shape has ever been
    executed by this server, so the response is unverified territory.

    Args:
        opportunity_id: The ``id`` from instahyre_list_opportunities.
        confirm: Must be True to actually send. False returns a preview only.
    """
    inbound = get_client().inbound
    if not confirm:
        return inbound.apply_preview(opportunity_id, is_interested=True)
    return inbound.submit_interest(opportunity_id, is_interested=True, confirm=True)


@mcp.tool()
@handled
def instahyre_decline_opportunity(opportunity_id: str, confirm: bool = False) -> dict:
    """Mark ONE opportunity "not interested". Also irreversible.

    This is not a soft dismissal. It is the same endpoint an application uses
    with the boolean flipped, it is permanent, and it feeds Instahyre's matching
    algorithm -- so it shapes which employers are shown in future cycles.

    ``confirm=False`` (the default) sends nothing and returns the exact request
    that would go out.

    Args:
        opportunity_id: The ``id`` from instahyre_list_opportunities.
        confirm: Must be True to actually send.
    """
    inbound = get_client().inbound
    if not confirm:
        return inbound.apply_preview(opportunity_id, is_interested=False)
    return inbound.submit_interest(opportunity_id, is_interested=False, confirm=True)


# ---------------------------------------------------------------------------
# TIER 3 -- the inbox. Read-only, plain HTTP, no browser.
# ---------------------------------------------------------------------------


@mcp.tool()
@handled
def instahyre_list_conversations(
    status: Optional[str] = None,
    unread_only: bool = False,
    starred_only: bool = False,
    query: Optional[str] = None,
    limit: int = 10,
    offset: int = 0,
    include_job: bool = True,
) -> dict:
    """Recruiter conversation threads in his Instahyre inbox.

    READ-ONLY. This sends nothing, replies to nothing, and stars nothing. It
    also cannot: every request is checked against a list of mutating paths
    first, which matters more than it sounds -- Instahyre's "mark all read" is a
    GET sharing this endpoint's prefix, so an ordinary-looking exploration of
    the resource would wipe his unread flags.

    A conversation record carries no company or recruiter name; the site joins
    those in from the job. This does the same when ``include_job`` is on, at one
    cached request per distinct job. Turn it off for a fast, bare list.

    An empty result always comes with a diagnosis saying whether filters emptied
    it or the inbox is genuinely bare -- and a dead session raises rather than
    returning an innocent empty list. NO BROWSER: this is plain HTTP.

    Args:
        status: "in_process" or "closed_by_recruiter". Omit for all threads --
            the site sends no status key at all for "All", so this does not either.
        unread_only: Only threads whose latest message is unread.
        starred_only: Only starred threads. Mutually exclusive with unread_only.
        query: Free-text search across conversations.
        limit: Threads per page. The site's own default is 10.
        offset: Paging offset.
        include_job: Join company, role and locations in from the job endpoint.
    """
    return get_client().inbox.list_conversations(
        status=status,
        unread_only=unread_only,
        starred_only=starred_only,
        query=query,
        limit=limit,
        offset=offset,
        include_job=include_job,
    )


@mcp.tool()
@handled
def instahyre_read_conversation(
    conv_id: int, body_chars: int = 1500, include_gated: bool = False
) -> dict:
    """Every message in one thread, as text, oldest first.

    READ-ONLY in the sense that it sends no data and asks for no change. But be
    straight about the side effect, because it has NOT been ruled out: the site
    marks a thread read WITHOUT calling any mark-read endpoint -- it just
    decrements the badge locally -- which is only coherent if the server marks
    it read when the messages are fetched. So **fetching a thread may mark it
    read on Instahyre's side.** That could not be tested: his inbox has no
    conversations to test against. Treat it as probable.

    Bodies arrive as HTML and are returned as plain text. Direction is reported
    as ``from_me``, and automated Instahyre messages are flagged separately from
    real recruiters -- an InstaBot message is not inbound interest.

    Args:
        conv_id: The ``id`` from instahyre_list_conversations.
        body_chars: Truncate each body to this many characters.
        include_gated: Include messages the site's own renderer withholds. The
            site stops at the first one and discards the rest; this mirrors that
            by default and always reports how many were withheld.
    """
    return get_client().inbox.read_conversation(
        conv_id, body_chars=body_chars, include_gated=include_gated
    )


@mcp.tool()
@handled
def instahyre_inbox_counts() -> dict:
    """Unread, starred and starred-unread totals for the inbox. One request."""
    return get_client().inbox.conversation_counts()


# ---------------------------------------------------------------------------
# TIER 4 -- profile writes. These CHANGE his account.
# ---------------------------------------------------------------------------


@mcp.tool()
@handled
def instahyre_update_skills(add: list[str], confirm: bool = False) -> dict:
    """Add skills to his live Instahyre profile. This is the highest-leverage write.

    Instahyre is a reverse marketplace: employers search for candidates, so the
    skill list is the surface he is found ON. A short list throttles every
    future match cycle rather than one application.

    ADD-ONLY, and deliberately so. Existing skills are echoed back exactly as
    the server returned them and nothing is ever removed. That makes the result
    identical whether Instahyre treats the payload as a full replacement set or
    as an ordinary additive patch -- an ambiguity in their contract that this
    designs around instead of testing on his live profile.

    With ``confirm=False`` (the default) nothing is sent: it returns the exact
    request, what would be added, and what was skipped. A snapshot is written to
    disk before any write, and instahyre_restore_profile puts it back.

    The platform caps skills at 20 and each name at 50 characters. Anything over
    the cap is dropped from the request and reported, not silently truncated.

    The write verifies itself by re-reading the profile afterwards -- a 200 is
    not treated as success.

    Args:
        add: Skill names to add, e.g. ["Express.js", "MongoDB", "Docker"].
        confirm: Must be True to actually write. False returns a preview.
    """
    return get_client().profile_writer.update_skills(add, confirm=confirm)


@mcp.tool()
@handled
def instahyre_update_profile(
    current_designation: Optional[str] = None,
    current_company: Optional[str] = None,
    total_experience: Optional[int] = None,
    confirm: bool = False,
) -> dict:
    """Update candidate-level profile fields. Writes when confirm=True.

    Only these three fields are writable here, and the limit is deliberate.
    They are scalars on the candidate record, which is the only shape the sparse
    PATCH is verified for -- Instahyre's own frontend does exactly this, one key
    at a time, in four independent places.

    Notice period, salary, preferred locations and job-search status are NOT
    writable and are refused by name with the reason: they live on a sub-object
    that has to be sent back whole, which is a wider contract than anything
    verified. Change those on the website.

    Snapshots first, verifies after.

    Args:
        current_designation: Job title.
        current_company: Employer name.
        total_experience: Years of experience.
        confirm: Must be True to actually write.
    """
    return get_client().profile_writer.update_fields(
        confirm=confirm,
        current_designation=current_designation,
        current_company=current_company,
        total_experience=total_experience,
    )


@mcp.tool()
@handled
def instahyre_restore_profile(snapshot_id: Optional[str] = None, confirm: bool = False) -> dict:
    """Put his skill list back to a snapshot taken before a write.

    Restoring is the one operation that must REMOVE rows, so it runs in two
    stages: the snapshot's rows are written back, then anything present that the
    snapshot does not contain is deleted individually by id. That delete is
    bounded hard -- it can only ever touch an id absent from the snapshot, so a
    restore cannot remove a skill that predates this server.

    Args:
        snapshot_id: From instahyre_list_profile_snapshots. Defaults to the newest.
        confirm: Must be True to actually restore.
    """
    return get_client().profile_writer.restore_skills(snapshot_id, confirm=confirm)


@mcp.tool()
@handled
def instahyre_list_profile_snapshots() -> dict:
    """Restore points taken before profile writes. Local files, no request."""
    snapshots = get_client().profile_writer.list_snapshots()
    return {
        "snapshots": snapshots,
        "count": len(snapshots),
        "note": (
            "Written automatically before every write. An empty list means this server "
            "has never written to the profile."
        ),
    }


@mcp.tool()
@handled
def instahyre_verify_apply_target() -> dict:
    """Re-measure which endpoint an application would be posted to. Opens a browser.

    THIS IS THE ONE DATA-PATH TOOL THAT USES A BROWSER, and it is worth saying
    why, because everything else here is plain HTTP by design.

    Instahyre has two apply endpoints, and a server-injected page flag decides
    which one an account uses -- it switches the URL and the request body
    together. No API endpoint exposes that flag; it lives in a server-rendered
    HTML page, and those pages are Cloudflare-gated (a plain client gets 403,
    headless Chromium gets a challenge). Since applications cannot be withdrawn,
    the target endpoint is worth a browser rather than an assumption.

    A visible window opens on the opportunities page, reads the flags, and
    closes. Every non-GET request is aborted at the router, so it physically
    cannot apply to anything. Nothing is clicked.

    Reports agreement or MISMATCH against the branch this server is configured
    for. It never changes that configuration on its own.
    """
    from .browser import read_page_flags

    return read_page_flags()


@mcp.tool()
@handled
def instahyre_skill_gap(
    my_skills: Optional[list[str]] = None,
    top_n: int = 15,
    interest: str = "pending",
) -> dict:
    """Which skill, added to your profile, would put you in the most match sets.

    THE question on this platform, and it is not the question a job board asks.
    Instahyre is employer-initiated: the scarce resource is being IN the match
    set, and the profile skill list is capped at 20 by the platform. So the
    useful move is not "apply to more" -- it is to work out which twenty skills
    buy the most inbound. This reads the whole curated queue and answers that.

    Both halves are returned, and the second one is the half people forget:

    * ``missing_skills`` -- what the queue asks for that you do not list,
      ranked by how many of its jobs ask. These are the additions.
    * ``covered_skills`` and ``dead_weight_skills`` -- which of your own skills
      the queue actually demands, and which appear in ZERO of its jobs. With
      the cap at 20 and no free slots, every addition is a SWAP, and the dead
      weight names what to swap out.

    ``skill_slots`` says whether you have room at all. ``duplicate_slots``
    catches two of your rows that normalise to the same skill -- they cost two
    slots and buy one.

    One request. Scored with the shared ``jobcore`` taxonomy, so "Node.js" on
    your profile and "nodejs" on a job are the same skill and never show up as
    a gap. Percentages exclude jobs that declare no keywords at all, and
    ``jobs_with_no_keywords`` reports how many that was.

    Profile changes reach the queue on Instahyre's own batch cycle, not
    immediately -- ``instahyre_get_profile`` reports when it last recalculated.

    Args:
        my_skills: Your skills. Omit to use the skills on your live Instahyre
            profile, which is what the match queue is actually computed
            against and therefore the right default.
        top_n: How many gap rows to return. The full count is always reported
            separately, so a truncated list never reads as complete.
        interest: Which slice of the queue to analyse -- "pending" (the default,
            and the one that matters: roles still open to you), "interested" or
            "not_interested".
    """
    snapshot = policy.current()
    inbound = get_client().inbound

    # RAW records, deliberately. The shaped ones cap skills at 8 and move the
    # overflow to skills_more, which is right for a list a human reads and
    # wrong for anything that counts -- it would report lower bounds that look
    # like measurements.
    payload = inbound.raw_queue(interest=interest)
    records = payload.get("objects") or []

    skills_source = "argument"
    if not my_skills:
        my_skills = inbound.profile_skills()
        skills_source = "account_profile"

    result = skillgap.analyse_gap(
        records, my_skills, top_n=top_n, **policy.scoring_args(snapshot)
    )
    result["skills_source"] = skills_source
    result["interest"] = interest
    result["queue_recalculated_at"] = payload.get("calculation_done_at")
    return result


@mcp.tool()
@handled
def instahyre_resume_info() -> dict:
    """The resume recruiters actually open: how old it is, and its download URL.

    Worth its own tool on this platform specifically. Every signal here runs
    through the resume -- the recruiter-activity tab is literally called
    "viewed your resume" -- and Instahyre carries its own ``is_fresh`` verdict
    on the file, which is a thing recruiters can filter on and a thing a stale
    upload silently loses.

    ``instahyre_get_profile`` reduces all of this to ``has_resume: true``. This
    returns the record: title, upload date, age in days, Instahyre's freshness
    verdict, conversion status and the URL.

    No resume on file comes back as ``has_resume: false`` with a diagnosis and
    the same keys, not as an error and not as an empty result.
    """
    return get_client().inbound.resume_info()


@mcp.tool()
@handled
def instahyre_saved_searches() -> dict:
    """Your saved searches and their alert toggles. The only "saved" thing here.

    Read the shape before reaching for this: Instahyre has NO bookmarked or
    saved JOBS -- no bookmark feature and no job-side endpoint to build one
    from. A saved SEARCH is the only saveable object, and a job alert is not a
    separate object either: it is one boolean on a saved search.

    The constraints are the platform's, not this server's: at most 5 saved
    searches, alerts only on a search carrying at least 3 filters, and no
    frequency field anywhere in the product -- there is no daily-or-weekly
    choice to make, so do not offer one.

    On a reverse marketplace this is a minor surface by construction: the
    inbound queue is where the value is, and an outbound alert competes with
    it rather than adding to it. Zero saved searches is a normal answer and
    comes back with a diagnosis saying so.
    """
    return get_client().inbound.saved_searches()


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
