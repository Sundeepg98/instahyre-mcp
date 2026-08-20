"""The MCP surface.

Every docstring here is written for the agent that has to choose between these
tools, so each one says what it is *for* and what it costs in requests. Results
are shaped, never raw -- the whole point of this server over a browser is that
reading a job board should not cost thousands of tokens every time.
"""

from __future__ import annotations

import functools
import logging
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
    saved = get_sessions().save_from(client.http, method="password")
    status = check_auth(client.http)
    return {
        "authenticated": status.get("authenticated"),
        "method": "password",
        "session_saved": saved.get("has_session"),
        "verified_by": status.get("checked_against"),
    }


@mcp.tool()
@handled
def instahyre_login_browser(wait_seconds: int = 300) -> dict:
    """Open a browser window so you can sign in by hand -- for Google sign-in.

    Use this when the account uses "Continue with Google", which is a redirect
    flow no HTTP client can complete. A visible Chromium window opens on
    Instahyre's login page against a persistent profile; sign in there and this
    returns as soon as the session cookie appears. The profile persists, so
    later runs usually find the session already live.

    This is the ONLY tool in this server that starts a browser. All data
    fetching is plain HTTP.

    Args:
        wait_seconds: How long to leave the window open for you to finish.
    """
    from .auth import login_via_browser

    client = get_client()
    return login_via_browser(client.http, get_sessions(), wait_seconds=wait_seconds)


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
        "tier": "public tools live; authenticated tier gated on login",
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
        },
        "page_size": C.PAGE_SIZE,
    }


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
