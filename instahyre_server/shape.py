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
