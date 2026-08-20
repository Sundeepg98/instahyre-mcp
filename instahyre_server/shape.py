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
    if jsp.get("notice_period") is not None:
        profile["notice_period_days"] = jsp["notice_period"]
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
