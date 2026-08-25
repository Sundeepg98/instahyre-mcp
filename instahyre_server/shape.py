"""Shaping: turn fat API payloads into the smallest thing that still answers.

The operator's whole reason for wanting an MCP over a browser is that reading a
DOM is a recurring per-use cost. A tool that hands back the raw payload just
moves that cost, so every function here exists to throw bytes away: one search
record goes from ~900 bytes of JSON to ~150, and a 3.6 KB job detail to a few
hundred plus as much description as the caller actually asked for.
"""

from __future__ import annotations

import html as _html
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from . import constants as C

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NL_RE = re.compile(r"\n{3,}")
_BLOCK_END_RE = re.compile(r"</(p|div|li|h[1-6]|tr|ul|ol|table|section)\s*>", re.I)
_BREAK_RE = re.compile(r"<br\s*/?>", re.I)
_LIST_ITEM_RE = re.compile(r"<li[^>]*>", re.I)

MAX_SKILLS_IN_LIST = 8
DEFAULT_DESCRIPTION_CHARS = 1200


def strip_html(raw: Optional[str]) -> str:
    """HTML -> readable plain text, preserving paragraph and bullet structure.

    Instahyre wraps job descriptions in a full ``<html><body>`` document with a
    median of ~1,800 characters of real prose inside.
    """
    if not raw:
        return ""
    text = _BREAK_RE.sub("\n", raw)
    text = _LIST_ITEM_RE.sub("\n- ", text)
    text = _BLOCK_END_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = _html.unescape(text)
    text = text.replace("\xa0", " ")
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _NL_RE.sub("\n\n", text)
    return text.strip()


def truncate(text: str, limit: Optional[int]) -> tuple[str, bool]:
    """Cut at a word boundary. Returns ``(text, was_truncated)``."""
    if limit is None or limit <= 0 or len(text) <= limit:
        return text, False
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip() + " ...", True


def size_band(employee_count: Optional[int]) -> Optional[str]:
    """Map an ``employee_count`` bucket to small / medium / large."""
    if employee_count is None:
        return None
    return C.EMPLOYEE_BUCKET_TO_BAND.get(employee_count)


def _as_list(value: Any) -> list[str]:
    """``locations`` is a comma-joined string on search and a list on detail."""
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value)]


def shape_search_record(obj: dict, *, max_skills: int = MAX_SKILLS_IN_LIST) -> dict:
    """One search result -> a compact record.

    ``score`` / ``is_strong_match`` / ``reviewed_at`` / ``interview_status`` are
    null for anonymous callers and populated once a session exists, so they are
    emitted only when they carry something.
    """
    employer = obj.get("employer") or {}
    skills = _as_list(obj.get("keywords"))
    record: dict[str, Any] = {
        "id": obj.get("id"),
        "title": obj.get("title") or obj.get("candidate_title"),
        "company": employer.get("company_name"),
        "locations": _as_list(obj.get("locations")),
        "skills": skills[:max_skills],
    }
    if len(skills) > max_skills:
        record["skills_more"] = len(skills) - max_skills

    band = size_band(employer.get("employee_count"))
    if band:
        record["company_size"] = band
    if employer.get("company_founded"):
        record["founded"] = employer["company_founded"]
    if employer.get("id"):
        record["company_id"] = employer["id"]
    if obj.get("public_url"):
        record["url"] = _absolute(obj["public_url"])

    # Authenticated-only fields. Present == the session is live.
    for key, out_key in (
        ("score", "match_score"),
        ("is_strong_match", "strong_match"),
        ("reviewed_at", "reviewed_at"),
        ("interview_status", "interview_status"),
    ):
        if obj.get(key) is not None:
            record[out_key] = obj[key]
    return record


def shape_detail(obj: dict, *, description_chars: Optional[int] = DEFAULT_DESCRIPTION_CHARS) -> dict:
    """A job detail -> a compact record with a derived agency verdict.

    The agency flag is free and exact: a detail object carries **either**
    ``agency_function_names`` or ``job_function_names``, never both, and in
    45/45 sampled records that key choice agreed with
    ``recruiter_company_name != hiring_company_name`` with zero disagreement.
    """
    description = strip_html(obj.get("description"))
    body, truncated = truncate(description, description_chars)

    hiring = obj.get("hiring_company_name")
    recruiter_company = obj.get("recruiter_company_name")
    is_agency = "agency_function_names" in obj

    functions = obj.get("agency_function_names") or obj.get("job_function_names") or []
    if not functions and isinstance(obj.get("job_function_dict"), dict):
        functions = [f for group in obj["job_function_dict"].values() for f in group]

    record: dict[str, Any] = {
        "id": obj.get("id"),
        "title": obj.get("title") or obj.get("candidate_title"),
        "company": hiring,
        "locations": _as_list(obj.get("locations")),
        "skills": _as_list(obj.get("keywords")),
        "experience_years": _experience_range(obj),
        "job_functions": functions,
        "category": obj.get("job_category"),
        "is_active": obj.get("is_active"),
        "is_internship": bool(obj.get("is_internship")),
        "accepts_outstation": obj.get("accept_outstation"),
        "posted_by_agency": is_agency,
        "recruiter": _recruiter(obj),
        "description": body,
    }
    if truncated:
        record["description_truncated"] = True
        record["description_full_chars"] = len(description)
    if is_agency and recruiter_company and recruiter_company != hiring:
        record["agency_name"] = recruiter_company
    if obj.get("opportunity_url"):
        record["url"] = _absolute(obj["opportunity_url"])
    # Salary is absent from this platform entirely -- 0 of 1,235 records carried
    # any pay field. Saying so once beats an agent hunting for it every time.
    record["salary"] = None
    record["salary_note"] = "Instahyre publishes no salary data on any job (measured: 0% disclosure)."
    return record


def _experience_range(obj: dict) -> Optional[str]:
    lo, hi = obj.get("workex_min"), obj.get("workex_max")
    if lo is None and hi is None:
        return None
    if lo is not None and hi is not None:
        return f"{lo}-{hi}"
    return f"{lo}+" if lo is not None else f"<={hi}"


def _recruiter(obj: dict) -> Optional[dict]:
    name = obj.get("recruiter_name")
    if not name:
        return None
    out = {"name": name}
    if obj.get("recruiter_designation"):
        out["designation"] = obj["recruiter_designation"]
    if obj.get("recruiter_company_name"):
        out["company"] = obj["recruiter_company_name"]
    return out


def _absolute(url: str) -> str:
    if url.startswith("http"):
        return url
    return C.SITE_BASE + url


# -- facets -----------------------------------------------------------------


def shape_meta(meta: dict, *, industry_names: Optional[dict[int, str]] = None) -> dict:
    """The free faceted aggregates that ride along on every search response.

    Two things worth knowing about this block: the ``top_*`` lists are
    truncated to four entries server-side, and ``job_experience_levels`` bands
    OVERLAP -- they sum to well above the total and are not a histogram.
    """
    out: dict[str, Any] = {
        "total_count": meta.get("total_count"),
        "returned": meta.get("limit"),
        "offset": meta.get("offset"),
    }
    if meta.get("top_job_functions_count"):
        out["top_job_functions"] = [
            {"id": f.get("id"), "name": f.get("name"), "count": f.get("count")}
            for f in meta["top_job_functions_count"]
        ]
    if meta.get("top_locations_count"):
        out["top_locations"] = [
            {"name": loc.get("location"), "count": loc.get("count")}
            for loc in meta["top_locations_count"]
        ]
    if meta.get("top_companies_count"):
        out["top_companies"] = [
            {"name": c.get("name"), "count": c.get("count")} for c in meta["top_companies_count"]
        ]
    if meta.get("top_industry_types_count"):
        # The "name" key here holds an industry *id*, not a name. Resolve it
        # when we have the taxonomy, and say plainly when we do not.
        out["top_industries"] = [
            {
                "id": item.get("name"),
                "name": (industry_names or {}).get(item.get("name")),
                "count": item.get("count"),
            }
            for item in meta["top_industry_types_count"]
        ]
    if meta.get("company_size_count"):
        out["by_company_size"] = meta["company_size_count"]
    if meta.get("job_type_counts"):
        out["by_job_type"] = meta["job_type_counts"]
    if meta.get("job_experience_levels"):
        out["by_experience_level"] = meta["job_experience_levels"]
        out["experience_levels_note"] = (
            "These bands overlap and do not partition the corpus -- they sum above total_count."
        )
    if meta.get("max_experience") is not None:
        out["max_experience"] = meta["max_experience"]
    out["facets_note"] = "Server truncates every top_* list to 4 entries."
    return out


# -- de-duplication ---------------------------------------------------------


def dedupe(records: Iterable[dict]) -> tuple[list[dict], int]:
    """Drop repeat ids, preserving order. Returns ``(records, dropped)``."""
    seen: set[Any] = set()
    out: list[dict] = []
    dropped = 0
    for rec in records:
        key = rec.get("id")
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        out.append(rec)
    return out, dropped


def collapse_duplicates(records: list[dict]) -> list[dict]:
    """Flag the same role listed under several ids.

    Real and material: one 841-job sample held 41 ``(company, title)`` pairs
    living under more than one id, one of them under seven. Without this the
    operator scrolls the same job repeatedly and assumes the tool is broken.
    """
    groups: dict[tuple[Any, Any], list[Any]] = {}
    for rec in records:
        groups.setdefault((rec.get("company"), rec.get("title")), []).append(rec.get("id"))
    for rec in records:
        ids = groups.get((rec.get("company"), rec.get("title")), [])
        if len(ids) > 1:
            rec["duplicate_ids"] = [i for i in ids if i != rec.get("id")]
    return records


# ===========================================================================
# AUTHENTICATED TIER
# ===========================================================================


def shape_opportunity(obj: dict, *, max_skills: int = MAX_SKILLS_IN_LIST) -> dict:
    """One curated-queue record -> a compact opportunity.

    Two ids live on this object and they are not interchangeable. ``id`` is the
    OPPORTUNITY id -- a long numeric string, and the only thing an apply or a
    decline accepts. ``job.id`` is the ordinary integer job id that the public
    tier uses. Mixing them up is the obvious mistake here, so both are emitted
    under names that cannot be confused.
    """
    employer = obj.get("employer") or {}
    job = obj.get("job") or {}
    skills = _as_list(job.get("keywords"))

    record: dict[str, Any] = {
        "id": str(obj.get("id")) if obj.get("id") is not None else None,
        "job_id": job.get("id"),
        "title": job.get("title") or job.get("candidate_title"),
        "company": employer.get("company_name") or job.get("hiring_company_name"),
        "locations": _as_list(job.get("locations")),
        "skills": skills[:max_skills],
        "match_score": _as_float(obj.get("score")),
        "status": C.INTEREST_FACET_NAMES.get(obj.get("interview_status"), "unknown"),
    }
    if len(skills) > max_skills:
        record["skills_more"] = len(skills) - max_skills
    if employer.get("id"):
        record["company_id"] = employer["id"]
    band = size_band(employer.get("employee_count"))
    if band:
        record["company_size"] = band
    if employer.get("company_founded"):
        record["founded"] = employer["company_founded"]
    if employer.get("company_tagline"):
        record["tagline"] = employer["company_tagline"]
    if employer.get("instahyre_note"):
        record["about"], _ = truncate(employer["instahyre_note"], 260)
    if obj.get("is_strong_match"):
        record["strong_match"] = True
    if obj.get("is_location_match") is False:
        record["location_match"] = False
    if obj.get("is_active") is False:
        record["is_active"] = False
    if obj.get("reviewed_at"):
        record["reviewed_at"] = obj["reviewed_at"]
    if obj.get("message"):
        record["recruiter_message"] = obj["message"]
    url = job.get("opportunity_url")
    if url:
        record["url"] = _absolute(url)
    return record


#: What Instahyre's queue score actually is, said once per result rather than
#: on every record. It is a raw relevance float, not a percentage and not the
#: INSTAMATCH high/medium/low banding the public bundle advertises.
SCORE_NOTE = (
    "match_score is Instahyre's own relevance float, not a percentage. Observed range on "
    "this account: 0.82 to 16.05, median 2.46. Use it to rank, not to read as a grade."
)


# ===========================================================================
# COMPACT PROJECTION -- what a caller needs to DECIDE, not everything we know
# ===========================================================================
#
# MEASURED, on this account's real pending queue on 2026-08-25, thirty rows:
# ``opportunities`` was 20,902 bytes, about 5,200 tokens of a caller's context
# for one default call, and the ranking of what made it big is the argument for
# this projection rather than any opinion about verbosity::
#
#     about         6,497 bytes  (31%)   employer blurb, IDENTICAL for every
#                                        role at that employer
#     url           2,916               derivable from the id, one call later
#     skills        1,893               the role's actual content
#     tagline       1,520               employer one-liner, same per employer
#     title         1,015
#     locations       897
#     company_size    700
#     company         697
#     strong_match    600   present on 30 of 30 rows, so it separates nothing
#     match_score     586
#     status          570   equal to the ``interest`` echoed once per result
#     company_id      565
#     id              540
#     job_id          478
#     founded         450
#
# Two thirds of that payload describes EMPLOYERS. Picking between opportunities
# needs identity, place and rank -- and one thing that says what the role is.
# ``instahyre_get_opportunity`` already assembles the rest from three sources,
# so nothing here is lost; it is only no longer paid for thirty times over on a
# call that was asked to help choose.

#: Every compact row carries exactly these, and no compact row omits one.
COMPACT_OPPORTUNITY_FIELDS = ("id", "company", "title", "locations", "match_score")

#: The ONE differentiator, capped. Titles on this platform repeat ("Software
#: Engineer" eleven times in one thirty-row sample) and ``match_score`` orders
#: but does not describe, so the keywords are what separates two adjacent rows.
#: Three, not eight: measured at 63 bytes per row for eight and 28 for three,
#: and the first three are the ones Instahyre itself ranked on.
COMPACT_MAX_SKILLS = 3

#: NEGATIVE flags only, and they are not a second differentiator -- they are
#: the reason a compact row cannot simply be the five fields above. Each is
#: emitted by ``shape_opportunity`` ONLY when it is false, so each costs zero
#: bytes on a healthy row and is decision-critical on the row that has it: a
#: dead posting presented as live, or an out-of-area role presented as local,
#: is a WRONG answer rather than a smaller one. Neither appeared on any of the
#: thirty rows measured above, so keeping them cost nothing measurable.
COMPACT_WARNING_FIELDS = ("is_active", "location_match")

#: What ``detail`` accepts. "full" is today's shape, byte for byte.
DETAIL_MODES = ("compact", "full")


def compact_opportunity(
    record: dict, *, max_skills: int = COMPACT_MAX_SKILLS, keep: Iterable[str] = ()
) -> dict:
    """One shaped queue record -> the smallest row that can still be chosen between.

    A PROJECTION of :func:`shape_opportunity`, never a second shaping path.
    It takes a record that function already built and drops from it, so the two
    cannot drift into disagreeing about what a field means -- there is one
    place where a queue object becomes a record, and this narrows the result.

    Args:
        record: an already-shaped opportunity.
        max_skills: how many keywords survive. ``skills_more`` reports the
            remainder rather than letting the list end silently.
        keep: extra keys the caller added AFTER shaping and needs back --
            ``instahyre_inbound_digest`` scores these rows, so it names
            ``fit_score``, ``matched_skills`` and ``explain``. Nothing is kept
            that the caller did not name.
    """
    out: dict[str, Any] = {}
    for field in COMPACT_OPPORTUNITY_FIELDS:
        out[field] = record.get(field)
    skills = record.get("skills") or []
    if skills:
        out["skills"] = list(skills[:max_skills])
        # The count, not the list: a reader who needs all of them asks for
        # detail="full" or opens the opportunity. A list that just stops is
        # how a caller concludes a role wants three things and no more.
        hidden = max(0, len(skills) - max_skills) + int(record.get("skills_more") or 0)
        if hidden:
            out["skills_more"] = hidden
    for field in COMPACT_WARNING_FIELDS:
        if field in record:
            out[field] = record[field]
    for field in keep:
        if field in record:
            out[field] = record[field]
    return out


def project_opportunities(
    records: list, detail: str, *, keep: Iterable[str] = ()
) -> list:
    """Apply ``detail`` to a list of shaped records.

    ``"full"`` returns the same list object's records untouched. Raises on any
    other spelling rather than silently serving one mode while the caller
    believes it asked for the other -- a compact row that a caller read as full
    is a row missing fields nobody was told about.
    """
    mode = str(detail).strip().lower()
    if mode not in DETAIL_MODES:
        raise ValueError(
            "detail must be one of %s, not %r" % (", ".join(DETAIL_MODES), detail)
        )
    if mode == "full":
        return records
    return [compact_opportunity(r, keep=keep) for r in records]



#: How much of a prose entry a SUMMARY view keeps. Long enough to carry the
#: verdict each of these blocks opens with -- "BUILT ON 2026-08-25", "NOT
#: BUILT", "READABLE, contrary to what this field said before" -- because a
#: summary that kept only the key NAMES would turn an entry recording that
#: something IS reachable into a line in a list headed "not available".
SUMMARY_HEAD_CHARS = 120


def headline(text: Any, *, limit: int = SUMMARY_HEAD_CHARS) -> Any:
    """The opening of a prose block, for a summary that points at the full text.

    Cuts on a word boundary and says so with a trailing marker, so a reader can
    tell a shortened entry from a complete one. Anything that is not a string
    is returned untouched -- this narrows prose, it does not reshape data.

    NOT a replacement for the prose. Every caller of this pairs it with the
    parameter that returns the entry verbatim, and the summary names that
    parameter. Shortening documentation is only acceptable while the long form
    is one call away; a summary that is the ONLY remaining copy is a deletion
    wearing a smaller name.

    A PURE TRUNCATION, and that is a deliberate constraint rather than an
    implementation detail. An entry already inside the limit comes back BYTE
    FOR BYTE -- no added punctuation, no tidying -- so a test can assert that
    every short form is a literal prefix of its long form, and no rewrite that
    softened a verdict or dropped a correction could pass. An earlier draft
    appended a full stop to entries it was otherwise leaving alone; that is a
    small thing to get wrong and a small thing to notice, which is exactly the
    kind of edit that erodes documentation nobody is checking.
    """
    if not isinstance(text, str):
        return text
    if len(text) <= limit:
        return text
    head = text.split(". ")[0]
    if len(head) < len(text):
        # The split ate the terminator that ended this sentence; put it back,
        # because the result is a whole sentence and should read as one.
        head += "."
    if len(head) <= limit:
        return head
    return head[:limit].rsplit(" ", 1)[0] + " [...]"


def summarise_prose(block: dict, *, limit: int = SUMMARY_HEAD_CHARS) -> dict:
    """A dict of prose entries -> the same keys, each cut to its opening."""
    return {key: headline(value, limit=limit) for key, value in block.items()}


def shape_sibling_role(row: dict) -> dict:
    """A sibling role from opps_from_this_company.

    Worth having for one reason: it carries the experience band, which the
    queue record itself does not.
    """
    out: dict[str, Any] = {
        "opportunity_id": str(row.get("id")) if row.get("id") is not None else None,
        "title": row.get("job_title"),
        "locations": _as_list(row.get("job_locations")),
    }
    band = _experience_range(
        {"workex_min": row.get("job_workex_min"), "workex_max": row.get("job_workex_max")}
    )
    if band:
        out["experience_years"] = band
    return out


def shape_opportunity_counts(payload: dict) -> dict:
    """The queue facet block -> named, readable counts."""
    status = payload.get("status_counts") or {}
    out: dict[str, Any] = {
        "total": status.get("total"),
        "by_status": {
            C.INTEREST_FACET_NAMES.get(int(k), f"facet_{k}"): v
            for k, v in status.items()
            if k != "total" and str(k).lstrip("-").isdigit()
        },
    }
    if payload.get("location_counts"):
        out["top_locations"] = [
            {"name": r.get("name"), "count": r.get("count")} for r in payload["location_counts"]
        ]
    if payload.get("industry_counts"):
        out["top_industries"] = [
            {"id": r.get("industry_id"), "name": r.get("name"), "count": r.get("count")}
            for r in payload["industry_counts"]
        ]
    if payload.get("top_companies"):
        out["top_companies"] = [
            {"id": r.get("id"), "name": r.get("name"), "count": r.get("count")}
            for r in payload["top_companies"]
        ]
    if payload.get("top_job_functions"):
        out["top_job_functions"] = [
            {"id": r.get("id"), "name": r.get("name"), "count": r.get("count")}
            for r in payload["top_job_functions"]
        ]
    if isinstance(payload.get("company_counts"), dict):
        out["by_company_size"] = {
            C.COMPANY_SIZE_NAMES.get(int(k), f"band_{k}"): v
            for k, v in payload["company_counts"].items()
            if str(k).lstrip("-").isdigit()
        }
    if isinstance(payload.get("job_type_counts"), dict):
        out["by_job_type"] = {
            C.JOB_TYPE_NAMES.get(int(k), f"type_{k}"): v
            for k, v in payload["job_type_counts"].items()
            if str(k).lstrip("-").isdigit()
        }
    if payload.get("experience"):
        out["experience_band_in_queue"] = payload["experience"]
    out["facets_note"] = "Server truncates every top_* list to 4 entries."
    return out


def shape_activity(obj: dict) -> dict:
    """One recruiter-activity event.

    The employer block here is the RECRUITING entity, which on this platform is
    usually a staffing agency, while ``job.hiring_company_name`` is who the role
    is actually for. Collapsing the two would quietly misattribute every event,
    so both are kept and named for what they are.
    """
    employer = obj.get("employer") or {}
    job = obj.get("job") or {}
    out: dict[str, Any] = {
        "when": obj.get("action_date"),
        "recruiter": obj.get("recruiter_name"),
        "recruiter_company": employer.get("company_name"),
        "job_title": job.get("title") or job.get("candidate_title"),
        "hiring_company": job.get("hiring_company_name"),
    }
    if obj.get("recruiter_id"):
        out["recruiter_id"] = obj["recruiter_id"]
    if job.get("url"):
        out["url"] = _absolute(job["url"])
    return out


#: Profile fields that decide whether employers can find him, in the order a
#: human would fix them. Each entry is (key, label, why it matters).
PROFILE_CHECKS = (
    ("resume", "resume uploaded", "recruiters open the resume; the activity feed counts those opens"),
    ("main_skills", "skills listed", "skills are the primary input to the match score"),
    ("total_experience", "years of experience", "filters him out of every experience-banded search when missing"),
    ("current_designation", "current job title", "shown on every match; blank reads as an incomplete profile"),
    ("current_company", "current employer", "shown on every match"),
    ("education", "education added", "also the only endpoint that reveals the candidate id"),
    ("number_verified_at", "phone verified", "unverified numbers reduce recruiter contact"),
    ("profile_image_src", "profile photo", "cosmetic, but a blank avatar reads as an abandoned profile"),
)


def shape_profile(raw: dict) -> dict:
    """The candidate profile -> what actually affects being found.

    Personal contact details are reported as present/absent rather than echoed.
    An agent choosing between jobs has no use for his phone number, and every
    tool result is one more place it could end up.
    """
    user = raw.get("user") or {}
    jsp = raw.get("jsp") or {}
    skills = _as_list(raw.get("main_skills"))
    profile: dict[str, Any] = {
        "candidate_id": raw.get("id"),
        "name": user.get("full_name"),
        "current_company": raw.get("current_company"),
        "current_designation": raw.get("current_designation"),
        "total_experience_years": raw.get("total_experience"),
        "skills": skills,
        "primary_job_function": raw.get("job_function_skills"),
        "previous_companies": _as_list(raw.get("previous_companies")),
        "education": [
            {
                "degree": (e.get("current_degree") or {}).get("name"),
                "university": (e.get("university") or {}).get("name"),
                "graduation_year": e.get("graduation_year"),
            }
            for e in raw.get("education") or []
        ],
        "visible_to_employers": raw.get("is_private") is False,
        "open_to_offers": raw.get("is_hireable"),
        "blocked_employers": [
            b.get("company_name") for b in raw.get("companies_to_block") or [] if b.get("company_name")
        ],
        "has_resume": bool(raw.get("resume")),
        "phone_verified": bool(raw.get("number_verified_at")),
        "contact_details_on_file": sorted(
            f for f in C.CONTACT_FIELDS if raw.get(f) or user.get(f)
        ),
        "queue_recalculated_at": raw.get("calculation_done_at"),
    }
    # The job-search status arrives WITH its own label on the profile payload,
    # so there is no enum to guess here -- unlike the settings endpoint, which
    # returns the bare integer and nothing to decode it with.
    if jsp.get("status_string"):
        profile["job_search_status"] = jsp["status_string"]
    # NOT DAYS. `notice_period` is an INDEX into NOTICE_PERIOD_RANGES -- 3 means
    # "2 months or less", not three days -- and this shipped as
    # `notice_period_days` until the bundle constant was read on 2026-08-24. The
    # mislabel was invisible on this account because it sits at 0, where both
    # readings print the same number. The band is emitted beside the index so a
    # reader never has to know which one it is.
    if jsp.get("notice_period") is not None:
        profile["notice_period"] = jsp["notice_period"]
        profile["notice_period_band"] = C.NOTICE_PERIOD_RANGES.get(
            jsp["notice_period"], "unrecognised band %r" % (jsp["notice_period"],)
        )
    if jsp.get("location_preferences"):
        profile["preferred_locations"] = list(jsp["location_preferences"])
    if jsp.get("is_immediate_joinee") is not None:
        profile["immediate_joiner"] = jsp["is_immediate_joinee"]
    profile["completeness"] = _profile_completeness(raw)
    profile["privacy_note"] = (
        "Phone, alternate phone and email are on file but are deliberately not returned "
        "by this tool -- only which of them exist."
    )
    return profile


def _profile_completeness(raw: dict) -> dict:
    """Rank the gaps by what they cost him, not by how many boxes are ticked."""
    filled, gaps = [], []
    for key, label, why in PROFILE_CHECKS:
        value = raw.get(key)
        if isinstance(value, (list, tuple)):
            present = len(value) > 0
        elif isinstance(value, str):
            present = bool(value.strip())
        else:
            present = value is not None and value is not False
        (filled if present else gaps).append({"field": label, "why_it_matters": why})
    total = len(PROFILE_CHECKS)
    return {
        "score": f"{len(filled)}/{total}",
        "percent": round(100 * len(filled) / total) if total else 0,
        "gaps": gaps,
        "note": (
            "Instahyre computes the match queue on a batch cycle, so a profile change shows "
            "up on the NEXT recalculation, not immediately."
        ),
    }


#: The two URLs on a resume record that resolve to the document itself, in the
#: order a human wants them: ``pdf_file`` is Instahyre's PDF rendering and
#: ``url`` the file as uploaded. The record carries two MORE -- ``watermark_file``
#: and ``html_file`` -- and neither is ever offered as the download: they are
#: Instahyre's own derived copies, and the second is a gzipped HTML conversion
#: (``.jgz``) that nothing opens as a document.
RESUME_DOWNLOAD_FIELDS = ("pdf_file", "url")

#: Written once, here, because two things have to agree about it: the flag is
#: the platform's verdict and the cutoff behind it is not published. See
#: ``constants.RESUME_FRESHNESS_CUTOFF_PUBLISHED``.
RESUME_FRESHNESS_NOTE = (
    "is_fresh is Instahyre's OWN verdict on this file, not a computation of this "
    "server's, and it is not cosmetic: the platform surfaces resume staleness to "
    "recruiters, and recruiter resume-opens are what the activity feed counts. The "
    "cutoff that flips the flag is UNPUBLISHED -- no bundle constant, help page or "
    "API field names the number of days -- so age_days is reported beside the flag "
    "and neither one is derived from the other."
)


def _age_in_days(stamp: Any, *, now: Optional[datetime] = None) -> Optional[int]:
    """Whole days between an ISO-8601 timestamp and now, or None.

    Returns None rather than raising, and that is the whole design: an
    unreadable timestamp must cost the derived age and NOTHING else. The
    load-bearing field is the platform's own ``is_fresh`` verdict, which does
    not depend on this parsing succeeding.

    The one captured value is ``2026-08-13T10:59:44+00:00``. A trailing ``Z``
    is normalised anyway rather than trusted not to appear -- it costs one
    replace, and a capture in the other spelling would otherwise silently
    become None.
    """
    if not isinstance(stamp, str) or not stamp.strip():
        return None
    text = stamp.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    # Clamped at zero on purpose: ``timedelta.days`` floors toward negative
    # infinity, so two seconds of clock skew against the server would report an
    # age of -1 rather than 0.
    return max(0, (reference - parsed).days)


def shape_resume(raw: dict, *, now: Optional[datetime] = None) -> dict:
    """The uploaded resume record -> whether it is still doing its job.

    ``shape_profile`` reduces this whole resource to ``has_resume: true``,
    which is the right size for a completeness check and the wrong size for the
    question that actually matters. ``is_fresh`` is the field with
    consequences: Instahyre shows resume staleness to recruiters, and the
    activity feed counts resume opens, so a stale file is felt on the one
    signal this platform runs on.

    ``now`` is injectable so the age arithmetic is testable against a fixed
    clock rather than against whatever day the suite happens to run on.
    """
    download = None
    for field in RESUME_DOWNLOAD_FIELDS:
        if raw.get(field):
            download = raw[field]
            break
    return {
        "has_resume": True,
        "resume_id": raw.get("id"),
        "title": raw.get("title"),
        "uploaded_on": raw.get("uploaded_on"),
        "age_days": _age_in_days(raw.get("uploaded_on"), now=now),
        "is_fresh": raw.get("is_fresh"),
        "conversion_status": raw.get("conversion_status"),
        "download_url": download,
        "freshness_note": RESUME_FRESHNESS_NOTE,
    }


def shape_saved_search(row: dict) -> dict:
    """One saved search, forwarded whole with one derived key added.

    The row is forwarded WHOLE, which is the opposite of what every other
    shaper in this file does. The reason is evidence, not laziness: this
    account has ZERO saved searches, so no live record has ever been captured,
    and renaming keys nobody has seen into a tidy shape would publish a
    contract this server has not measured.

    WHAT IS KNOWN ABOUT THE ROW, as of the 2026-08-23 capture pass. Four field
    NAMES now have shipped source behind them rather than one: the
    authenticated-tier bundle's ``getSavedJobSearches`` reads ``id``, ``name``
    and ``search_string`` off each row, ``toggleAlerts`` writes
    ``job_alert_enabled_at``, and ``showRecommendedJobs`` reads ``created_at``.
    ``search_string`` is an HTTP-query-serialized STRING, not an object -- the
    client parses it with its own deserializer. That is still a reading of
    their JavaScript, not a captured payload, so the row keeps being forwarded
    rather than reshaped.
    """
    out = dict(row)
    # An "_at" field: present and non-null means the toggle is on. There is no
    # boolean beside it and no frequency anywhere in the product -- an alert IS
    # this one timestamp, or it is off.
    out["alerts_on"] = bool(row.get(C.SAVED_SEARCH_ALERT_FIELD))
    return out


def shape_settings(raw: dict) -> dict:
    """Account settings, with the password fields removed before anything else.

    Instahyre echoes ``password``, ``current_password`` and ``confirm_password``
    back in this GET. They are dropped here, first, so no later code path can
    log, cache or return them.
    """
    clean = {k: v for k, v in raw.items() if k not in C.SETTINGS_NEVER_EMIT}
    out: dict[str, Any] = {
        "visible_to_employers": clean.get("is_private") is False,
        # The integer arrives bare here with nothing to decode it. The PROFILE
        # payload carries the same status WITH Instahyre's own label, so this
        # reports the code and points there rather than inventing a name.
        "job_search_status_code": clean.get("job_search_status"),
        "job_search_status_label_from": "instahyre_get_profile publishes the label for this code",
        # NOT the same question the profile answers, and naming them alike made
        # the two tools look like they contradicted each other. "has_valid_*"
        # means the value passes format validation; the profile's
        # "number_verified_at" means he actually completed OTP verification. He
        # can, and does, have a well-formed number that was never verified.
        "email_format_valid": clean.get("has_valid_email"),
        "phone_format_valid": clean.get("has_valid_number"),
        "has_password_login": clean.get("has_usable_password"),
        "whatsapp_enabled": clean.get("whatsapp_enabled"),
        "notification_channels": clean.get("notification_channels"),
        "blocked_employers": [
            b.get("company_name") for b in clean.get("companies_to_block") or [] if b.get("company_name")
        ],
        "email_unsubscribes": {
            key.replace("_email_unsubscribed_at", ""): value
            for key, value in clean.items()
            if key.endswith("_email_unsubscribed_at")
        },
        "contact_details_on_file": sorted(f for f in C.CONTACT_FIELDS if clean.get(f)),
    }
    out["security_note"] = (
        "Instahyre returns password fields in this payload. They are stripped before this "
        "tool returns and are never cached or logged."
    )
    return out


def _as_float(value: Any) -> Optional[float]:
    """Queue scores arrive as decimal STRINGS ('2.475'), not numbers."""
    if value is None:
        return None
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None
