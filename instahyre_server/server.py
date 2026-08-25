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
            # This entry used to read "the inbox exposes an unread count only;
            # threads need a conv_id no endpoint lists". That was retired by
            # inbox.py, which found the conversation list at
            # /inbox_page/candidate_conversation -- so the conv_id IS listed and
            # bodies ARE readable. A stale "cannot" in this block is the same
            # defect as a false "can": both send a caller somewhere untrue.
            "message_bodies": (
                "READABLE, contrary to what this field said before 2026-08-23: "
                "instahyre_list_conversations lists threads and instahyre_read_conversation "
                "returns their bodies. What is genuinely absent is a not-found signal -- a "
                "conv_id that is not his answers 200 with an empty list, so the id is "
                "cross-checked against his own conversation list rather than trusted."
            ),
            "opportunity_detail_route": "there is none; a queue record is found by scanning the queue",
            # Added 2026-08-24. It sits HERE, not under deliberately_not_built,
            # because the two blocks answer different questions: this one says
            # what the platform cannot do, that one says what we chose not to
            # build. "Edit work experience" fails at the first question, and a
            # reader who only checked the second would come away thinking it
            # was a judgement call.
            "work_experience": (
                "There is NO work-experience record on this platform to read or edit. The "
                "profile payload carries 42 keys and none is a work-experience block, and "
                "the signed-in profile page renders no control for one -- its editors are "
                "preference, skills, current_company, internship, education, social and "
                "diversity_info. A route spelled PUT .../onboarding_workex/:id does exist "
                "and misleads: its only caller passes the ENTIRE candidate object and reads "
                "back current_company and companies_to_block, so it writes the candidate, "
                "not a workex row. Those fields are already writable, sparsely, through "
                "instahyre_update_profile. See deliberately_not_built.work_experience_edit."
            ),
        },
        "deliberately_not_built": {
            "apply_bulk": (
                "BUILT ON 2026-08-25, and this entry is kept rather than moved because "
                "what it used to say is part of the record: 'Instahyre's API has it. It "
                "is a one-way door across a whole queue at once and is permanently out "
                "of scope.' Both its paths sat in FORBIDDEN_ENDPOINTS, the only ban in "
                "this package that said 'at any evidence level'. The ruling is that "
                "whatever is technically possible gets built; the contract ships whole "
                "in Instahyre's own JavaScript, so it was possible, so it exists -- as "
                "instahyre_apply_bulk, gated harder than anything else here. The "
                "hazard the ban named is real and is now handled by rails instead of a "
                "blocklist: the caller passes an explicit id list (nothing assembles "
                "one), a cap of 10 REFUSES a longer list rather than truncating it, "
                "every id is validated against the live pending queue, the preview "
                "names every company and role, and an expected_count stated separately "
                "from the list must match what resolved. The read tier is untouched -- "
                "apply_bulk is still in MUTATING_PATH_MARKERS and the reader still "
                "refuses both paths."
            ),
            "profile_writes_beyond_the_job_search_profile": (
                "THE OLD REFUSAL HERE IS RETIRED, and it is worth saying why rather "
                "than just deleting it. It read: the job-search-profile sub-object "
                "needs the whole object PUT back, 'a contract this server has not "
                "verified'. That named an UNVERIFIED contract, not an unknowable one, "
                "and a contract can be verified -- so it was, out of Instahyre's own "
                "$resource factory and both calling functions. Notice period, salary, "
                "preferred locations, job type and job-search status are now writable "
                "through instahyre_update_job_search_profile. The full replacement is "
                "handled by never omitting a key: the object is read, only the named "
                "fields are replaced, everything else is echoed back verbatim, and a "
                "guard refuses any body narrower than the read. What is STILL not "
                "written, each for a stated reason rather than for nerve: career "
                "stage (it cascades into four fields the caller did not name), "
                "is_salary_hidden (gated behind a salary and experience threshold this "
                "account does not meet, so the site does not offer him the control), "
                "is_immediate_joinee (server-derived; zero write sites across ten "
                "bundles), and the related objects job_function, industry_types and "
                "languages (sent EXPANDED rather than as ids, which is a wider "
                "contract than the five fields that were asked for)."
            ),
            "work_experience_edit": (
                "NOT BUILT, and the reason changed on 2026-08-24. The earlier record "
                "said no caller for PUT onboarding_workex exists in any shipped "
                "bundle. That was wrong -- $scope.onBoardingProfileSave calls it, on "
                "the ONBOARDING page rather than the profile page, which is why a "
                "profile-page search missed it. Reading the caller closed the surface "
                "harder than the missing evidence had: the body is $scope.candidate, "
                "the ENTIRE candidate object, and the response is read back for "
                "current_company and companies_to_block. The route named 'workex' "
                "writes the CANDIDATE. There is no work-experience record on this "
                "platform to edit, which is why the profile page renders no control "
                "for one and the 42-key profile payload contains no such block. The "
                "fields it does reach are already writable by sparse PATCH through "
                "instahyre_update_profile, so building it would trade a narrow write "
                "for a whole-object one at zero gain."
            ),
            "inbox_writes": (
                "ALL FOUR MEASURED INBOX WRITES ARE NOW REACHABLE, and the write side "
                "still runs on an allowlist -- now of FOUR NAMED URLS rather than one. "
                "The distinction is the whole design: SENDABLE_INBOX_PATHS grew by "
                "named constants, one per captured contract, and NOT by relaxing into a "
                "prefix, a regex or an 'anything under /resume_modal' rule, because a "
                "rule admits members nobody has read. instahyre_reply_to_conversation, "
                "instahyre_star_conversation, instahyre_mark_conversation_read and "
                "instahyre_mark_all_conversations_read are the four; each defaults to "
                "confirm=False and sends nothing without an explicit confirm=True. "
                "WHAT THIS RETIRES, quoted so the change reads as a change: starring "
                "and marking read were previously described here as having 'no branch "
                "that could construct them', and were held unbuilt on VALUE rather than "
                "on evidence. The ruling changed on 2026-08-25 -- whatever is "
                "technically possible gets built -- and all three were possible, "
                "because their contracts ship in Instahyre's own JavaScript. "
                "WHAT DID NOT CHANGE. The read tier is byte-for-byte unchanged and "
                "still refuses all five mutating path markers including send_message, "
                "so a reader cannot reach a write path by editing a constant. Bulk "
                "apply remains permanently out of scope at any evidence level. And the "
                "honest caveat that was already true is still true: his inbox holds "
                "ZERO conversations (measured 2026-08-23, authenticated, 200), so none "
                "of the four has ever been exercised against live data, and "
                "mark_all_read cannot be -- the platform's own caller refuses when the "
                "unread count is zero. "
                "THE ONE WORTH KNOWING ABOUT THIS RESOURCE: mark_all_read is a GET that "
                "MUTATES -- confirmed in the factory itself, "
                "mark_all_read:{method:'GET',url:url+'mark_all_read'} -- which is why "
                "both guards key on the PATH and never on the verb, and why that GET "
                "carries the same confirm gate as any POST here. Its cost is real and "
                "the preview names it: marking read DESTROYS the only signal separating "
                "a new recruiter message from an old one, and there is no bulk undo -- "
                "only a per-thread mark_unread against the list the preview printed."
            ),
        },
        # instahyre_mark_all_conversations_read joined on 2026-08-25 and it is
        # the one entry here that is not literally un-undoable: each thread can
        # be pushed back with mark_unread=True. It is listed anyway, because
        # the thing it destroys is not the flag but the KNOWLEDGE of which
        # threads carried it -- nothing on the platform records that, so the
        # only way back is the list its own preview printed. Its two siblings,
        # instahyre_star_conversation and instahyre_mark_conversation_read, are
        # genuinely reversible from information the account still holds and are
        # deliberately NOT listed: a list that included everything gated would
        # stop meaning anything.
        "irreversible_tools": [
            "instahyre_apply",
            "instahyre_decline_opportunity",
            "instahyre_reply_to_conversation",
            "instahyre_mark_all_conversations_read",
        ],
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

    THERE IS NOW A BULK APPLY, and this paragraph used to say the opposite --
    "there is deliberately no bulk apply in this server". See
    instahyre_apply_bulk. Prefer THIS tool anyway when applying to one thing:
    it is the narrower instrument, and one application per confirmation is the
    shape a human can actually check. Reach for the bulk tool only when several
    have already been chosen and named.

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


@mcp.tool()
@handled
def instahyre_apply_bulk(
    opportunity_ids: list,
    expected_count: int,
    confirm: bool = False,
) -> dict:
    """Apply to SEVERAL opportunities in ONE request. IRREVERSIBLE, every one.

    THE MOST DESTRUCTIVE TOOL ON THIS SERVER. Instahyre applications CANNOT BE
    WITHDRAWN -- their FAQ says the application is sent automatically by the
    system, so there is no undo, no support path, and every employer on the
    list sees it immediately. One call here spends several of those at once.

    WHY THE GATE IS SHAPED THE WAY IT IS. A confirm-gated bulk apply is not
    inherently more dangerous than N confirm-gated single applies -- it is the
    same N applications, to the same employers, equally permanent. What it
    removes is N-1 CONFIRMATIONS, and every rail here exists to hand back
    exactly what that collapse takes away:

    * The PREVIEW NAMES EVERY OPPORTUNITY -- company, role, id, one line each
      -- restoring the sight of each item that N separate previews would have
      given. Never confirm against a count; read the lines.
    * ``expected_count`` restores the ARITHMETIC. State how many applications
      are intended, independently of the list. If the list changed length
      between the preview and the call, the count is the only thing that
      notices, and the call fails instead of applying.
    * A HARD CAP OF 10 bounds the blast radius, and over the cap is a REFUSAL,
      never a truncation -- applying to the first ten of twenty-five and
      reporting success is the exact failure this tool must not have.
    * THE ID LIST IS YOURS. This tool never assembles it. There is no "apply to
      all", no filter, no "top N by score". Rank with instahyre_rank_jobs
      first if you want a ranked selection, show him the names, then pass ids.

    Note which way the site's own default points: Instahyre's bulk modal opens
    with EVERY opportunity pre-selected. This tool does the opposite and
    selects nothing -- an empty list is refused, not read as "everything".

    Every id is checked against his CURRENT pending queue, re-read for the
    purpose, before anything is sent; an id that is not there is a refusal
    naming it. Duplicates are refused rather than deduplicated. After the
    write, the queue is re-read and the result says which applications
    actually took -- the response shape is unknown territory, so state is the
    evidence rather than a status code.

    THERE IS NO BULK DECLINE and there never will be: the bulk body has no
    is_interested key at all, so this endpoint is apply-only by construction.

    Args:
        opportunity_ids: An EXPLICIT list of opportunity ids from
            instahyre_list_opportunities. Maximum 10. Never assembled here.
        expected_count: How many applications you intend to send. Must equal
            the number that resolves, or nothing is sent.
        confirm: Must be True to actually send. False (the default) returns the
            full preview, naming every opportunity, and issues no write at all.
    """
    return get_client().writer.bulk_apply(
        opportunity_ids, expected_count, confirm=confirm
    )


# ---------------------------------------------------------------------------
# TIER 2b -- the channel that asks HIM something.
#
# Every other tool on this server is him asking Instahyre. These three read and
# answer the one channel where INSTAHYRE ASKS HIM: were you hired at this
# company, and how did that opportunity go. The first of those is a TERMINAL
# status change, and until 2026-08-25 nothing here could see it.
#
# MEASURED EMPTY, 2026-08-25, from his own signed-in session, all three routes
# answering 200: show_verify_modal -> {"data": []}, verify_hired_candidate ->
# {"objects": [], "meta": {...}}, get_opportunity_info -> {"show_modal": false}.
# Nothing in this cluster has ever been exercised against live data. The
# channel is EMPTY, not absent -- and because both writes validate their id
# against a live re-read, both refuse today. That is the gate, not a bug.
# ---------------------------------------------------------------------------


@mcp.tool()
@handled
def instahyre_pending_requests() -> dict:
    """What is Instahyre ASKING YOU right now? Reads only; changes nothing.

    THE ONLY PLACE THE PLATFORM PUTS A QUESTION TO HIM. Everywhere else on this
    server he is the one initiating -- searching, applying, replying. This
    channel runs the other way, and the question it carries is terminal: were
    you hired at this company. There is also a lighter one, how did that
    opportunity go, which feeds their leaderboard.

    ONE CALL COVERS THE WHOLE CHANNEL. Three endpoints answer three parts of
    one question and are read together, because a caller who had to know all
    three route names in advance would simply never look -- which is exactly
    how a hire check sits unanswered.

    NOTHING PENDING IS A RESULT. When the channel is empty this returns
    ``anything_pending: False`` and a sentence saying so in as many words. It
    is never an error and never a bare empty dict: "I could not tell" dressed
    up as "nothing" is the one answer that would cost him the thing this tool
    exists to catch. The mirror holds too -- a read that genuinely fails raises
    instead of returning, so "nothing pending" can never be what a lapsed
    session looks like.

    IT IS EMPTY TODAY, and that was measured rather than assumed: all three
    routes answered 200 and empty on 2026-08-25 from his own signed-in session.
    So this cluster has never been read populated, and the shape of a populated
    record comes from Instahyre's shipped JavaScript rather than from anything
    anybody has seen.
    """
    return get_client().writer.pending_requests()


@mcp.tool()
@handled
def instahyre_answer_hire_check(
    hired_id: str,
    choice: int,
    confirm: bool = False,
) -> dict:
    """Answer ONE "were you hired here?" question. A TERMINAL status change.

    This is the platform asking whether he took a job at a named company, and
    this tool sends the answer. It is the only write on this server that
    reports an OUTCOME rather than an intent, and nothing in Instahyre's
    product edits or retracts one.

    THE ID MUST COME FROM A LIVE READ. Before anything is sent the hire-check
    modal is re-read and ``hired_id`` must be one of the checks it is currently
    offering. A fabricated id is impossible to submit -- not discouraged,
    impossible -- and today, with that read empty on this account, EVERY call
    refuses by name. That refusal is correct behaviour: there is no hire check
    pending, so there is nothing to answer.

    WHAT choice MEANS IS NOT KNOWN, AND IS NOT GUESSED. Only 0 has a shipped
    caller -- Instahyre's own ``closeResponse`` sends ``choice:0``, the dismiss
    branch. Every other value is produced by a function that is defined in
    their JavaScript and called nowhere in it, because its callers live in an
    HTML template no capture holds. So this server does not know which integer
    means "yes, I was hired", says so in every preview, and will not invent
    one. Do not tell him a value means something it has not been measured to
    mean.

    ``confirm=False`` (the default) sends nothing and returns the exact
    request, naming the company and role being answered about.

    Args:
        hired_id: The ``hired_id`` of a check reported by
            instahyre_pending_requests. Validated against a live re-read.
        choice: The integer answer. 0 is the measured dismiss value; any other
            value's meaning is unmeasured and the preview says so.
        confirm: Must be True to actually send.
    """
    return get_client().writer.answer_hire_check(hired_id, choice, confirm=confirm)


@mcp.tool()
@handled
def instahyre_rate_opportunity(
    rating_uri: str,
    rating: int = None,
    ask_later: bool = False,
    confirm: bool = False,
) -> dict:
    """Rate ONE opportunity 1-5, or defer the question. Cannot be withdrawn.

    A rating is a judgement recorded against a named employer and it feeds
    Instahyre's leaderboard. There is no shipped path that edits or withdraws
    one.

    THE URI MUST COME FROM A LIVE READ. The rating offer is re-read before
    anything is sent and ``rating_uri`` must equal the ``resource_uri`` it is
    currently offering. A fabricated uri cannot be submitted, and today -- with
    that endpoint answering ``show_modal: false`` on this account -- EVERY call
    refuses. Nothing is pending, which is the normal state of this channel.

    TWO OF THE RAILS HERE ARE INSTAHYRE'S OWN, reproduced rather than invented,
    and the distinction is kept because a rail of ours dressed up as theirs is
    misleading: their page refuses to submit a rating with no rating, and
    refuses to defer a second time once it has already been asked to. Both
    refusals happen here, both read off the live payload.

    ``confirm=False`` (the default) sends nothing and returns the exact
    request -- including the detail worth seeing once: the three fields ride
    the QUERY STRING as well as the JSON body, because Instahyre declares them
    as action-level ``params``, and reproducing only one half would be a
    guessed request.

    Args:
        rating_uri: The ``resource_uri`` reported by
            instahyre_pending_requests. Validated against a live re-read.
        rating: 1 to 5. May be omitted only when ask_later is True.
        ask_later: Defer instead of rating. Sends a null rating, exactly as
            the site's own defer branch does.
        confirm: Must be True to actually send.
    """
    return get_client().writer.rate_opportunity(
        rating_uri, rating, ask_later=ask_later, confirm=confirm
    )


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
def instahyre_reply_to_conversation(
    conv_id: int, message: str, confirm: bool = False
) -> dict:
    """Send a reply into one recruiter conversation. IRREVERSIBLE -- a person reads it.

    THIS IS THE ONLY INBOX WRITE THIS SERVER HAS. Starring, marking read and
    bulk mark-all-read exist on Instahyre's API and remain unreachable here --
    not because they are on a blocklist, but because the write path has an
    allowlist of exactly one URL and none of them is it.

    THERE IS NO UNSEND. Instahyre has no unsend, no edit and no delete anywhere
    in its product. The recipient is a named person at a company he may want to
    work for, and the message goes from his name and his address.

    So ``confirm=False`` (the default) sends NOTHING and instead returns, for
    him to read before deciding: the recipients as the SERVER reports them, the
    thread's company and role, his message exactly as typed, and the precise
    body that would go on the wire. Re-run with ``confirm=True`` to send that.

    ``conv_id`` comes from instahyre_list_conversations. An id that is not his
    is refused rather than sent to -- the message endpoint answers 200 for a
    foreign id, so the id is cross-checked against his own conversation list
    first.

    Rails worth knowing, because they are THIS SERVER'S and not the platform's
    (Instahyre's own compose form validates nothing at all): an empty or
    whitespace-only message is refused, a message over 4000 characters is
    refused, attachments are never sent, and after a send the thread is re-read
    to confirm the message is actually there -- a 200 is not treated as
    delivery. If that confirmation fails the result says so and tells you NOT to
    retry blindly, because a retry that duplicates a delivered message cannot be
    undone either.

    The request contract was read whole out of Instahyre's own inbox controller
    JavaScript. It has never been observed on a wire and currently cannot be:
    his inbox holds zero conversations, and the compose form only exists inside
    a selected thread.

    Args:
        conv_id: Conversation id from instahyre_list_conversations.
        message: The reply text. Plain text; line breaks are preserved.
        confirm: Must be True to actually send. False returns a preview.
    """
    return get_client().writer.reply_to_conversation(
        conv_id, message, confirm=confirm
    )


@mcp.tool()
@handled
def instahyre_star_conversation(
    conv_id: int, starred: bool, confirm: bool = False
) -> dict:
    """Star or unstar one conversation. Reversible, and reaches nobody.

    A star is a private bookmark on his own inbox: no recruiter is notified and
    no message is sent. It is still gated -- ``confirm=False`` (the default)
    sends NOTHING and returns the exact request that would go out, the thread's
    company and role, and the current starred state.

    THE WIRE FIELD IS ``star_conv``, NOT ``starred``. The argument here is
    named for what it means; the body key is named for what Instahyre's own
    client sends. ``starred`` is the field the RESPONSE carries back, and no
    shipped caller ever sends a key by that name -- so do not "fix" the
    mismatch.

    ``can_user`` is deliberately absent from the body: Instahyre's two callers
    add it only when the profile type is not "candidate", and this account is a
    candidate. Sending it would be sending a field his own browser never sends.

    NEVER RUN AGAINST LIVE DATA. His inbox holds zero conversations (measured
    2026-08-23, authenticated, 200), so this has been exercised against
    fixtures only. The request contract was read whole out of Instahyre's
    shipped JavaScript; no response to it has ever been observed.

    Args:
        conv_id: Conversation id from instahyre_list_conversations.
        starred: True to star, False to unstar. Sent as ``star_conv``.
        confirm: Must be True to actually send. False returns a preview.
    """
    return get_client().writer.star_conversation(conv_id, starred, confirm=confirm)


@mcp.tool()
@handled
def instahyre_mark_conversation_read(
    conv_id: int, mark_unread: bool, confirm: bool = False
) -> dict:
    """Mark one conversation unread, or read. Reaches nobody. Reversible.

    ``confirm=False`` (the default) sends NOTHING and returns the exact body
    that would go out.

    MARKING UNREAD IS THE SAFE DIRECTION HERE, which is the opposite of how the
    two usually read. Unread is the only signal separating a new recruiter
    message from an old one, so clearing it destroys information and restoring
    it destroys none.

    EVIDENCE IS NOT SYMMETRIC BETWEEN THE TWO VALUES, and the preview says so
    every time. ``mark_unread=True`` is what Instahyre's own ``markUnread``
    sends and is the only value with a shipped caller anywhere.
    ``mark_unread=False`` has none: the site has no mark-read control at all --
    it marks a thread read implicitly when the thread is fetched -- so that
    value has never been observed on the wire.

    The body names the conversation by RESOURCE URI, not by id, and this server
    copies the URI the server itself returned on the record rather than
    assembling one.

    NEVER RUN AGAINST LIVE DATA -- zero conversations in his inbox (measured
    2026-08-23, authenticated, 200). Fixtures only.

    Args:
        conv_id: Conversation id from instahyre_list_conversations.
        mark_unread: True to mark it unread, False to mark it read.
        confirm: Must be True to actually send. False returns a preview.
    """
    return get_client().writer.mark_conversation_read(
        conv_id, mark_unread, confirm=confirm
    )


@mcp.tool()
@handled
def instahyre_mark_all_conversations_read(confirm: bool = False) -> dict:
    """Clear the unread flag across the WHOLE inbox. A GET, and gated like a send.

    WHY A GET IS GATED, since that is the part that looks like a category
    error: this GET MUTATES. Instahyre declares it
    ``mark_all_read:{method:'GET',url:url+"mark_all_read"}`` on the same
    resource -- and the same URL prefix -- as the conversation list, so the most
    reasonable-looking way to explore this API ("GET everything under the
    resource and see what comes back") wipes his unread state with no request
    body and no confirmation. A gate that keyed on the verb would wave it
    through. ``confirm`` here means exactly what it means on a POST: with
    ``confirm=False`` nothing is requested at all.

    WHAT IT COSTS. One call clears every unread flag in the inbox. There is no
    bulk undo. Each thread can be pushed back individually with
    instahyre_mark_conversation_read(conv_id, mark_unread=True) -- but only
    against the ``would_affect`` list this preview prints, because nothing else
    records which threads were unread beforehand. So read that list first.

    NO FILTER ARGUMENTS, on purpose. Instahyre's caller sends ``buildFilters()``
    plus a ``page_loaded_at`` timestamp as query parameters, and
    ``buildFilters()`` returns an empty dict on the default "All conversations"
    view. The widest call is the one this tool's name promises and the only one
    whose filter dict needs no choosing; a narrowed sweep would be a filter
    combination nobody measured.

    It refuses when the server reports no unread conversations, because
    Instahyre's own caller refuses in exactly that case.

    NEVER RUN AGAINST LIVE DATA, and on this account it currently cannot be:
    the inbox holds zero conversations (measured 2026-08-23, authenticated,
    200), so the unread count is zero and the platform's own gate would refuse
    to issue the request.

    Args:
        confirm: Must be True to actually send. False returns a preview naming
            every thread that would lose its unread flag.
    """
    return get_client().writer.mark_all_conversations_read(confirm=confirm)


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
def instahyre_update_skills(
    add: list[str], remove: Optional[list[str]] = None, confirm: bool = False
) -> dict:
    """Add and remove skills on his live Instahyre profile. The highest-leverage write.

    Instahyre is a reverse marketplace: employers search for candidates, so the
    skill list is the surface he is found ON. A short or badly-chosen list
    throttles every future match cycle rather than one application.

    THE PLATFORM CAPS THE LIST AT 20 AND HE IS AT THE CAP, so in practice every
    addition is a SWAP -- which is why ``remove`` exists and why both happen in
    one request. Removing a skill that appears in none of his matched jobs to
    make room for one that appears in most of them is the single cheapest move
    available on this platform.

    Both directions run through the same mechanism: the resource is a full
    replacement set, so a skill is removed by being left out of the list. There
    is no delete request (DELETE answers 405 here), and every skill that is
    meant to survive is echoed back exactly as the server returned it, in its
    original order.

    The rails on removal, since it is the destructive direction:
      * names match EXACTLY, case-insensitively -- never as a substring, so
        removing "System Design" cannot also take "System Design Patterns";
      * a name that is not on the profile is reported, never silently ignored;
      * adding and removing the same skill in one call is refused outright;
      * removing every skill is refused -- a profile with no skills is not a
        short profile, it is an unfindable one.

    With ``confirm=False`` (the default) nothing is sent: it returns the exact
    request, what would be added, what would be REMOVED, and what was skipped. A
    snapshot is written to disk before any write, and instahyre_restore_profile
    puts it back -- a removed skill returns under a new id, since its original
    row is gone.

    Each name is capped at 50 characters. Anything over the platform cap is
    dropped from the request and reported, not silently truncated.

    The write verifies itself by re-reading the profile afterwards -- a 200 is
    not treated as success, and a removal that did not take is named as such.

    Args:
        add: Skill names to add, e.g. ["Python", "Express.js", "MongoDB"].
        remove: Skill names to remove, e.g. ["Backend Development"]. Exact
            names as they appear in instahyre_get_profile.
        confirm: Must be True to actually write. False returns a preview.
    """
    return get_client().profile_writer.update_skills(add, remove, confirm=confirm)


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

    Notice period, salary, preferred locations, job type and job-search status
    are NOT writable HERE, and are refused by name pointing at the tool that can
    write them: they live on a sub-object that has to be sent back whole, which
    is a different request to a different resource. Use
    instahyre_update_job_search_profile for those.

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
def instahyre_update_job_search_profile(
    notice_period: Optional[int] = None,
    current_salary: Optional[float] = None,
    location_preferences: Optional[list[str]] = None,
    status: Optional[int] = None,
    job_type: Optional[int] = None,
    confirm: bool = False,
) -> dict:
    """Write the job-search profile: notice period, salary, locations, status, job type.

    THESE ARE FILTERS, NOT DECORATION. Instahyre is a reverse marketplace --
    employers search for candidates rather than the other way round -- so notice
    period and preferred locations decide whether he appears in a result set at
    all, not how he reads once he is in one. A stale notice period silently
    removes him from every urgent-hire search.

    This tool was refused until 2026-08-24, and the refusal was honest: these
    fields live on a sub-object the platform replaces WHOLE, and that contract
    had not been verified. It has been now, out of Instahyre's own $resource
    factory and both of its calling functions.

    HOW THE FULL REPLACEMENT IS MADE SAFE. The endpoint deletes any key the
    payload omits. So the payload never omits one: the object is read, only the
    fields named here are replaced, and every other key is echoed back exactly
    as the server returned it. A guard refuses the request outright if the body
    it is about to send does not carry every key the read returned -- so the
    dangerous case is unreachable rather than merely avoided.

    NOTICE PERIOD IS AN INDEX, NOT A NUMBER OF DAYS. 0 Immediately, 1 fifteen
    days or less, 2 one month or less, 3 two months or less, 4 three months or
    less. Passing 30 for "thirty days" is refused, not rounded.

    SALARY IS IN LAKHS per annum: 18 means 18 LPA, not 1800000.

    With confirm=False nothing is sent and the whole payload is shown, including
    the keys that ride along untouched. A snapshot is written before any write,
    and the result reports not only whether the requested fields took, but ALSO
    what else moved -- a full replacement can shift server-derived neighbours,
    and reporting only the fields that were asked for would hide exactly that.

    Args:
        notice_period: 0-4, an INDEX into the platform's bands. Not days.
        current_salary: Current annual salary in LAKHS (0-250).
        location_preferences: Preferred work locations. Replaces the whole list;
            names are resolved against the platform's own location taxonomy, and
            an empty list is refused rather than sent.
        status: 0 actively looking, 1 passively looking, 2 not looking.
        job_type: 0 both, 1 full-time only, 2 internships only.
        confirm: Must be True to actually write.
    """
    return get_client().profile_writer.update_job_search_profile(
        confirm=confirm,
        notice_period=notice_period,
        current_salary=current_salary,
        location_preferences=location_preferences,
        status=status,
        job_type=job_type,
    )


@mcp.tool()
@handled
def instahyre_update_education(
    education_id: int,
    graduation_year: Optional[int] = None,
    gpa: Optional[float] = None,
    grading_scale: Optional[float] = None,
    confirm: bool = False,
) -> dict:
    """Update one education row: graduation year, GPA, grading scale.

    DEGREE, UNIVERSITY AND SPECIALIZATION ARE FILTERS on a reverse marketplace,
    which is why education is worth writing at all -- but they are NOT writable
    here and are refused by name if asked for. Each is a related-object id, so
    changing one means resolving a row out of a taxonomy the site's own page had
    already loaded, and for the institute an autocomplete that can CREATE a row.
    That is a wider contract than the one that was captured, and nobody has
    measured it. Change those on the website; this tool moves the three fields
    that are plain values on the row.

    THE CONTRACT IS WIRE-CAPTURED, on 2026-08-25, from his own signed-in browser
    and aborted at the router before it left the machine. Two things it settled
    that no reading of the shipped code alone would have:

      * The READ and the WRITE are different shapes. The GET returns the
        university as an expanded object and the request sends it as a bare
        resource URI. That single transformation is the site's own, and it is
        the only value this server changes that it was not asked to change.
      * graduation_year travels as a STRING. Passing 2021 here is fine -- it
        goes out as "2021", which is what the browser sent.

    EVERY EDUCATION ROW RIDES EVERY WRITE, not just the one being edited.
    Whether a row omitted from the payload is deleted by this resource is not
    measured: the sibling resource that shares its save action DOES delete
    omitted rows, while education additionally has its own removal channel,
    which argues the other way. Sending every row is correct either way, so that
    is what happens. THIS TOOL CANNOT DELETE A ROW. Its removal channel goes out
    empty on every call and no argument here can fill it -- that is a property of
    the signature, not a default that can be flipped. Removing a row is
    instahyre_remove_education, which is a separate tool because it is a separate
    consequence.

    With confirm=False nothing is sent and the whole payload is shown, including
    the rows that ride along untouched. A snapshot is written before any write,
    and instahyre_restore_profile with scope="education" puts every row back.
    The result reports not only whether the requested fields took but ALSO
    whether anything else moved -- on this resource that is a finding, not an
    expected recomputation.

    Args:
        education_id: The id of the row to edit, from instahyre_get_profile or
            from this tool's own preview. Rows are addressed by id, never by
            position.
        graduation_year: Four-digit year. The accepted range is the site's own
            year list: 1980 to three years ahead of now.
        gpa: Grade point average. Only ever OBSERVED as null on the wire and
            named in no shipped bundle, so its accepted range is unmeasured --
            the write verifies itself by re-reading.
        grading_scale: The scale the gpa is out of, e.g. 10 or 4.
        confirm: Must be True to actually write. False returns a preview.
    """
    return get_client().profile_writer.update_education(
        education_id,
        graduation_year=graduation_year,
        gpa=gpa,
        grading_scale=grading_scale,
        confirm=confirm,
    )


@mcp.tool()
@handled
def instahyre_remove_education(
    education_id: int,
    confirm: bool = False,
) -> dict:
    """Delete one education row from his profile. This one does not come back.

    IT IS THE SAME REQUEST AS AN EDIT, read the other way. One PATCH carries
    every OTHER row verbatim in ``objects`` while the row being removed is
    named in ``deleted_objects`` by its resource_uri and is absent from
    ``objects``. That is both halves of the site's own removeEmptyRow -- push,
    then splice, then save -- and the server refuses to send a payload where
    the two halves disagree. There is no DELETE verb on this resource.

    THE EVIDENCE IS SHIPPED SOURCE, NOT WIRE, and the distinction is the
    reason this tool verifies instead of trusting. The envelope was captured
    off his own browser on 2026-08-25 with ``deleted_objects`` EMPTY, so no
    removal has ever been serialized or answered. What settles the element is
    the page's own handler, which pushes education.resource_uri and nothing
    else: the list holds resource URI STRINGS, not ids and not row objects. The
    write therefore re-reads the collection afterwards and reports whether the
    row is actually gone -- a 200 with the row still present is a finding about
    this resource, not a success.

    THE LAST EDUCATION ROW CANNOT BE REMOVED THROUGH THIS TOOL, at any confirm
    value. On a reverse marketplace employers filter on degree and institute,
    so a profile with no education is not a shorter profile -- it is one that
    drops out of the filtered result sets it would otherwise appear in. This
    account has exactly one row. That refusal arrives on the preview too,
    rather than at the end of a confirm dance for a write that can never run.

    THIS IS NOT UNDOABLE, and the snapshot does not make it so. A snapshot is
    written before the request and holds the row whole, but
    instahyre_restore_profile with scope="education" REFUSES to send a row
    whose id the server no longer has -- whether this resource re-creates it,
    ignores it or rejects the whole payload is unmeasured, and guessing would
    risk the rows still standing. Even with that refusal lifted, a re-added row
    gets a new id and a new resource_uri. The VALUES can be copied back out of
    the snapshot by hand at the website; the row cannot be restored.

    With confirm=False nothing is sent at all and the preview names exactly
    which row would go, what it currently reads as, and which rows would
    remain. Read ``would_remove`` before confirming.

    Args:
        education_id: The id of the row to delete, from instahyre_get_profile
            or from this tool's own preview. Rows are addressed by id, never
            by position, and an id that is not on the profile is refused by
            name rather than quietly doing nothing.
        confirm: Must be True to actually delete. False returns a preview and
            sends no request.
    """
    return get_client().profile_writer.update_education(
        education_id, remove=True, confirm=confirm
    )


@mcp.tool()
@handled
def instahyre_restore_profile(
    snapshot_id: Optional[str] = None,
    confirm: bool = False,
    scope: str = "skills",
) -> dict:
    """Put his skill list -- or his job-search profile -- back to a snapshot.

    ONE PATCH does the whole job, in both directions. The resource is a full
    replacement set, so writing the snapshot's rows back simultaneously restores
    what is missing and removes what is extra. No delete request is made or
    needed -- DELETE answers 405 on this resource, so the two-stage version this
    tool once described could never have worked and has been removed.

    A skill that was REMOVED since the snapshot comes back as a NEW row: its
    original id no longer exists server-side, so it is sent in the same shape
    Instahyre's own client uses for a newly typed skill. The name is restored,
    which is what employers search on; the id is not the original, and the
    result says so under ``recreated``.

    With ``confirm=False`` it shows exactly what would be restored, dropped and
    re-created, and sends nothing. Restoring is itself a write, so it takes its
    own snapshot first.

    ``scope="job_search_profile"`` restores the OTHER half instead: notice
    period, salary, preferred locations, status and every neighbouring key, by
    PUTting the snapshot's object back whole. That works because a
    full-replacement resource makes the snapshot a valid body -- restoring is
    the same request as writing, with older contents. A snapshot taken before
    2026-08-24 holds no job-search profile and is refused for this scope rather
    than partially applied, because a partial PUT deletes what it cannot supply.

    The two scopes are separate calls on purpose. They are different resources
    and different requests, and one flag that fired both would make a
    half-failure impossible to describe.

    ``scope="education"`` restores every education row from a snapshot, by
    PATCHing them all back. Only a snapshot taken by an education write holds
    them -- no other path fetches that collection -- so a skills or jsp snapshot
    is refused for this scope rather than half-applied. A row that has been
    DELETED since the snapshot makes the whole restore refuse: sending a row
    whose id the server no longer has is unmeasured on this resource, and
    guessing could take the surviving rows with it.

    Args:
        snapshot_id: From instahyre_list_profile_snapshots. Defaults to the newest.
        confirm: Must be True to actually restore.
        scope: "skills" (default), "job_search_profile" or "education".
    """
    writer = get_client().profile_writer
    if scope == "skills":
        return writer.restore_skills(snapshot_id, confirm=confirm)
    if scope == "job_search_profile":
        return writer.restore_job_search_profile(snapshot_id, confirm=confirm)
    if scope == "education":
        return writer.restore_education(snapshot_id, confirm=confirm)
    raise InvalidFilter(
        'scope must be "skills", "job_search_profile" or "education", not %r. '
        "They are different resources and different requests; there is "
        "deliberately no value that restores more than one at once." % (scope,),
        field="scope",
    )


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
            "has never written to the profile. Check jsp_captured before restoring "
            "with scope='job_search_profile': snapshots taken before 2026-08-24 hold "
            "skills and scalars only, and restoring a job-search profile from one is "
            "refused rather than half-applied. Check education_captured the same way "
            "before scope='education': ONLY an education write captures those rows, "
            "because no other write path fetches that collection."
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

    THE MOVE THIS TOOL EXISTS TO SET UP: take the top row of
    ``missing_skills``, take a row of ``dead_weight_skills``, and hand both to
    instahyre_update_skills in ONE call -- ``add=[...]``, ``remove=[...]``. That
    is the swap, it is a single request, and it is reversible via
    instahyre_restore_profile. Reading this tool and then only ever adding is
    what leaves an account stuck at the cap with dead rows in it.

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


# ---------------------------------------------------------------------------
# TIER 2 -- the inbound watch. What changed since he last looked.
# ---------------------------------------------------------------------------
#
# The queue holds hundreds of records and publishes no arrival date, so
# "list it again" cannot answer "what is different today". These three tools
# do, by remembering identities rather than timestamps -- the recruiter feed's
# only date is human prose that changes spelling as it ages.
#
# NONE OF THESE RUNS UNATTENDED. There is no poll and no timer anywhere in this
# package; an application here cannot be withdrawn, so nothing is allowed to
# act while nobody is watching. See instahyre_server/inbound_watch.py.


@mcp.tool()
@handled
def instahyre_whats_new(
    stream: str = "opportunities",
    limit: int = 50,
    advance: bool = True,
) -> dict:
    """What arrived in the inbound queue since you last looked. Start here daily.

    This is the tool for the question the queue cannot answer by itself. The
    pending queue runs to hundreds of records and Instahyre publishes no
    arrival date on any of them, so re-listing it tells you what exists, never
    what changed. This diffs against what has already been reported to you.

    THE FIRST CALL ON A STREAM REPORTS ZERO, deliberately. It records what is
    there as a baseline instead of announcing the whole backlog as news --
    a first answer of "227 new" is a backlog, and a reader who learns to ignore
    it is also ignoring the second answer, which was the real one. The reply
    says so in `baseline_established`.

    Nothing is destroyed by advancing. The bookmark moves; every opportunity
    stays fully readable through instahyre_list_opportunities.

    Args:
        stream: "opportunities" for the curated pending queue -- roles employers
            matched to him. "activity" for recruiters who opened the resume,
            which is the most perishable signal on the platform.
        limit: How many records to pull. The queue is ranked, so arrivals land
            near the head; the default covers a normal day comfortably.
        advance: Mark what is returned as seen, so the next call reports only
            what came after it. Pass False to look without consuming.

    A zero always carries a `diagnosis` naming which silence it is: everything
    already seen, the stream itself empty (with the underlying reason carried
    through), or a first baseline. A dead session raises rather than returning
    a quiet zero.

    On "activity": a repeat view by the same recruiter on the same role does
    not count as new. The feed publishes no per-event id and its only date is
    prose that changes as it ages ("13 hours ago" becomes "Aug 22 at ..."), so
    novelty is keyed on who acted on what. This is a measured limit of their
    API, not a choice about what matters.
    """
    return get_client().watch.whats_new(stream, limit=limit, advance=advance)


@mcp.tool()
@handled
def instahyre_watch_status(stream: Optional[str] = None) -> dict:
    """What the watch remembers, and when it last ran. Makes no request.

    Answers "when did I last look at this" without touching the network, which
    matters because that question is usually asked when something seems wrong
    with the session -- and a status tool that needed a live session to report
    on a dead one would be useless exactly when it is needed.

    `last_checked` and `last_advanced` are different facts and are reported
    separately: a stream read ten times with nothing new has a recent check and
    an old advance, and collapsing the two would make a quiet week look like a
    broken tool.

    Args:
        stream: Narrow to "opportunities" or "activity". Omit for both.
    """
    return get_client().watch.status(stream)


@mcp.tool()
@handled
def instahyre_watch_forget(stream: str, confirm: bool = False) -> dict:
    """Drop one stream's memory so the next look re-baselines. Confirm-gated.

    Gated because it destroys something that cannot be recovered. This server's
    record of when an opportunity first appeared is the ONLY arrival date that
    will ever exist for it -- Instahyre publishes none -- so forgetting a stream
    discards history the platform cannot re-supply. The queue itself is
    untouched; only the watch's own bookkeeping is deleted.

    It re-baselines rather than floods: the next call records what is there and
    reports zero new. That is what makes it safe to offer at all.

    Args:
        stream: "opportunities" or "activity".
        confirm: Must be True to delete. False returns what WOULD be deleted
            and removes nothing.
    """
    client = get_client()
    if not confirm:
        stats = client.watch.status(stream)["streams"][stream]
        return {
            "stream": stream,
            "confirmed": False,
            "would_forget": stats["known"],
            "forgotten": 0,
            "warning": (
                "Nothing was deleted. This would discard %d remembered identity(ies) "
                "and the first-seen dates attached to them, which Instahyre cannot "
                "re-supply. Re-run with confirm=True to proceed."
                % stats["known"]
            ),
            "current": stats,
        }
    return client.watch.forget(stream)


# ---------------------------------------------------------------------------
# TIER 3 -- the writes whose contracts were captured on 2026-08-23.
# ---------------------------------------------------------------------------
#
# Each of these was blocked until its real request had been recorded, because a
# write with a guessed body is worse than no tool: a wrong guess usually 400s
# harmlessly, a half-right guess succeeds and does something nobody chose, and
# on this platform the second case is permanent.
#
# ``constants.CAPTURED_WRITE_CONTRACTS`` says, per surface, whether the body was
# recorded off the wire or read out of Instahyre's shipped JavaScript, and every
# preview below carries that stamp. Two of the original six are still NOT here:
# a screening questionnaire can only be opened by pressing Apply on a real
# opportunity, and the workex PUT has no caller and no control to intercept.


@mcp.tool()
@handled
def instahyre_support_ticket(message: str, confirm: bool = False) -> dict:
    """Raise a support ticket with Instahyre. A person reads it; there is no delete.

    The whole request was recorded off the wire, which settled two details a
    guess would have got wrong: the candidate is named by RESOURCE URI rather
    than by integer id, and the URL carries no trailing slash even though the
    site's own factory declares one.

    With ``confirm=False`` (the default) NOTHING is sent -- it returns the exact
    request, including the message text as it would arrive. Attachments are
    always empty: the site's form takes files, this tool does not send them.

    Args:
        message: What to tell support. An empty message is refused rather than
            opening a blank ticket in a human queue.
        confirm: Must be True to actually raise it.
    """
    return get_client().writer.support_ticket(message, confirm=confirm)


@mcp.tool()
@handled
def instahyre_toggle_job_alert(
    saved_search_id: int, enable: bool, confirm: bool = False
) -> dict:
    """Turn email alerts on or off for one saved search. Reversible.

    A job alert is not an object on this platform -- it is one boolean on a
    saved search -- so this reads the row first and sends the query string back
    alongside the flag, which is what Instahyre's own toggle does. A flag-only
    update is a request the site never makes.

    Two platform constraints, both measured and neither this server's
    invention: alerts need a search carrying at least three filters, and there
    is NO frequency field anywhere in the product. Do not offer a daily-or-
    weekly choice; none exists.

    Zero saved searches is a normal answer here and comes back with a diagnosis
    that separates a real zero from a failed read, plus where to create one.

    Args:
        saved_search_id: The id from instahyre_saved_searches.
        enable: True to turn alerts on, False to turn them off.
        confirm: Must be True to actually apply it. False previews the exact
            PATCH and changes nothing.
    """
    return get_client().writer.toggle_job_alert(
        saved_search_id, enable, confirm=confirm
    )


@mcp.tool()
@handled
def instahyre_referral_link(confirm: bool = False) -> dict:
    """Ask Instahyre for his own referral link. Contacts nobody.

    It is a POST, so it is gated like every other write here, but nothing
    leaves his account: the response hands back a referral URL. Use this before
    inviting anyone -- the link is what a referral is actually made of.

    Args:
        confirm: Must be True to send the request.
    """
    return get_client().writer.referral_link(confirm=confirm)


@mcp.tool()
@handled
def instahyre_referral_contacts() -> dict:
    """Who Instahyre would offer as invitees, from his Google contacts. A READ.

    This exists so that inviting people can be an informed decision rather than
    a typed guess. It is a GET in Instahyre's own client -- reading the list
    sends nothing to anyone -- and it reports the ``preselect`` flag Instahyre
    attaches to some contacts WITHOUT acting on it. A default-selected
    recipient is a recipient nobody chose.

    An empty list comes back with a diagnosis separating "never granted Google
    access" from "granted but empty" from "shape changed". This server never
    drives the Google consent screen.
    """
    return get_client().writer.referral_contacts()


@mcp.tool()
@handled
def instahyre_send_referral_invites(
    emails: list[str], confirm: bool = False
) -> dict:
    """Invite people to Instahyre from his account. IRREVERSIBLE -- read this.

    These are real people who know him. The mail carries his name and his
    address, and Instahyre has no unsend anywhere in its product. Unlike an
    application, which at worst wastes a slot, there is no version of a wrong
    invitation that costs nothing.

    So: with ``confirm=False`` (the default) NOTHING is sent, and the result
    names every single recipient. Show that list to him and get an explicit yes
    before calling again with ``confirm=True``. A malformed address is refused
    rather than attempted, duplicates are removed before the count, and one
    call will not send more than ten.

    Names are sent as null, because that is exactly what Instahyre's own typed-
    invite path does. Use instahyre_referral_contacts to see the names it holds.

    Args:
        emails: The addresses to invite.
        confirm: Must be True to actually send. False returns the recipient
            list and sends nothing.
    """
    return get_client().writer.send_referral_invites(emails, confirm=confirm)


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
