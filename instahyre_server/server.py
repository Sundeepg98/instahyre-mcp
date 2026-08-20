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

from . import constants as C
from . import scoring, shape
from .cache import Store, default_db_path
from .client import InstahyreClient
from .errors import InstahyreError
from .http import InstahyreHTTP
from .session import SessionStore, check_auth, login_with_password

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
    my_skills: list[str],
    my_experience_years: Optional[float] = None,
    my_location: Optional[str] = None,
    skills: Optional[list[str]] = None,
    job_functions: Optional[list[str]] = None,
    locations: Optional[list[str]] = None,
    experience_years: Optional[int] = None,
    company_size: Optional[str] = None,
    exclude_agencies: bool = False,
    top_n: int = 10,
) -> dict:
    """Search, then rank the results by fit against your own skills. No login needed.

    Ranks ONE page -- the 35 jobs the server returns for these filters, not the
    whole matching set. ``total_matching`` says how many exist; narrow the
    filters if that number is large and you want the genuine top of the field.

    The API's own ``sort`` parameter is inert, so this is the only ordering that
    works. Scoring uses the shared ``jobcore`` engine when it is available -- the
    same one the Naukri server uses, so scores are comparable across boards --
    and the output names which engine produced them. Expect absolute scores to
    run low: Instahyre lists many keywords per job, and the skill component is
    the share of the JOB's skills you cover, so the useful signal is the
    ordering rather than the number.

    Skill matching runs on the search result's keyword list; experience uses the
    job's band where a cached detail supplies it. No salary component can ever
    contribute, because Instahyre publishes no salary data.

    Args:
        my_skills: Your skills, e.g. ["Node.js", "TypeScript", "AWS"].
        my_experience_years: Your years of experience, for the band check.
        top_n: How many ranked jobs to return.
    """
    client = get_client()
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
        if job.get("posted_by_agency") is not None:
            entry["posted_by_agency"] = job["posted_by_agency"]
        ranked.append(entry)
    ranked.sort(key=lambda r: (r["fit_score"] is None, -(r["fit_score"] or 0)))
    return {
        "ranked_jobs": ranked[:top_n],
        "scored": len(ranked),
        "total_matching": result.get("total_matching"),
        "scoring_engine": scoring.ENGINE,
        "note": "Ranked locally -- the API's own sort parameter is accepted but ignored.",
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
def instahyre_logout() -> dict:
    """Forget the saved session cookies on this machine.

    Local only -- it clears the cookie jar, it does not end the session on
    Instahyre's side or touch the browser profile.
    """
    client = get_client()
    had = get_sessions().clear()
    client.http.cookies.clear()
    return {"cleared": had, "authenticated": False}


@mcp.tool()
@handled
def instahyre_server_info() -> dict:
    """What this server is, what it has cached, and what it deliberately cannot do.

    Useful when a search behaves unexpectedly -- it reports the local index
    size, request count this process, and the known-absent data fields.
    """
    client = get_client()
    return {
        "server": "instahyre",
        "tier": "public tools always live; authenticated tools need instahyre_login",
        "requests_this_process": client.http.request_count,
        "min_seconds_between_requests": client.http.min_interval,
        "scoring_engine": scoring.ENGINE,
        "index": client.store.index_stats(),
        "state_dir": str(default_db_path().parent),
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
            "profile_writes": (
                "The read shape is verified; the WRITE contract is not, and verifying it means "
                "writing to the live profile that generates every match. "
                "instahyre_preview_profile_update shows the request instead."
            ),
        },
        "irreversible_tools": ["instahyre_apply", "instahyre_decline_opportunity"],
        "page_size": C.PAGE_SIZE,
        "opportunity_page_size": C.OPP_DEFAULT_LIMIT,
    }



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
def instahyre_inbound_digest(rank_against_my_profile: bool = True, top_n: int = 8) -> dict:
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
            digest["scored_against_skills"] = my_skills
            for entry in top:
                verdict = scoring.score_job(
                    job_skills=entry.get("skills") or [],
                    profile_skills=my_skills,
                    profile_years=None,
                )
                entry["fit_score"] = verdict.get("overall_score")
                entry["matched_skills"] = (verdict.get("skill_match") or {}).get("matched")
            digest["scoring_engine"] = scoring.ENGINE
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


@mcp.tool()
@handled
def instahyre_preview_profile_update(
    current_designation: Optional[str] = None,
    current_company: Optional[str] = None,
    total_experience: Optional[int] = None,
    notice_period_days: Optional[int] = None,
) -> dict:
    """Build the profile update that WOULD be sent -- and stop there, on purpose.

    This tool never writes. That is a deliberate limit, not an oversight, and
    the reason is worth stating plainly: the write contract for the profile
    could not be verified without performing a real write on his live profile,
    and the profile is what generates his entire match queue. A PATCH with a
    field shape guessed slightly wrong could blank a field or return 200 while
    changing nothing -- and a silent no-op is the exact failure class this
    server exists to refuse.

    So it returns the request an update tool would issue, and the update itself
    stays a two-second job on the website where it is visible and correctable.

    What IS known: the profile GET shape (every field, verified live) and the
    detail route. What is NOT known: whether a PATCH suffices or whether the
    ``submit/`` sub-action must follow it, and which fields are writable.

    Args:
        current_designation: Job title to set.
        current_company: Employer to set.
        total_experience: Years of experience to set.
        notice_period_days: Notice period in days.
    """
    return get_client().inbound.profile_update_preview(
        current_designation=current_designation,
        current_company=current_company,
        total_experience=total_experience,
        notice_period_days=notice_period_days,
    )

def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
