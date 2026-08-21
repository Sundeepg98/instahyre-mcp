"""The API facade the tools call. Resolves filters, shapes results, keeps state.

One rule runs through everything here: **an empty result must be explainable.**
Instahyre validates most filters server-side and answers a bad value with HTTP
400, which is loud and fine. But ``skills`` is not validated -- a typo returns
HTTP 200 with ``total_count: 0``, indistinguishable from a real dry spell. That
one gap is why :meth:`InstahyreClient.diagnose_empty` exists.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable, Optional

from . import constants as C
from . import shape
from .cache import Store, default_db_path
from .errors import InvalidFilter, NotFound
from .http import InstahyreHTTP
from .taxonomy import Taxonomy, resolve_company_size, resolve_job_type

log = logging.getLogger("instahyre.client")

AGENCY_FLAG_MAX_AGE = C.TTL_DETAIL


class InstahyreClient:
    """Stateful facade over the Instahyre API: HTTP + cache + taxonomy + shaping."""

    def __init__(
        self,
        http: Optional[InstahyreHTTP] = None,
        store: Optional[Store] = None,
    ) -> None:
        self.http = http or InstahyreHTTP()
        self.store = store or Store(default_db_path())
        self.taxonomy = Taxonomy(self.http, self.store)
        # The authenticated tier. Shares this client's cookie jar deliberately,
        # so instahyre_logout disables it and there is one session per process.
        from .inbound import Inbound

        self.inbound = Inbound(self.http, self.store, self)

        # The inbox reads conversations and message bodies. Same jar, same
        # rules, and no browser: /inbox_page/* is an ordinary API route.
        from .inbox import Inbox

        self.inbox = Inbox(self.http, self.store, self)

        # The only tier that can change his account. It leans on inbound for
        # the candidate id rather than recovering it a second way.
        from .profile_write import ProfileWriter

        self.profile_writer = ProfileWriter(self.http, self.store, self.inbound)

    def close(self) -> None:
        self.http.close()
        self.store.close()

    # -- filter assembly ---------------------------------------------------

    def build_params(
        self,
        *,
        skills: Optional[Iterable[str] | str] = None,
        job_functions: Optional[Iterable[Any] | Any] = None,
        locations: Optional[Iterable[str] | str] = None,
        companies: Optional[str] = None,
        industries: Optional[Iterable[Any] | Any] = None,
        company_size: Optional[Any] = None,
        job_type: Optional[Any] = None,
        experience_years: Optional[int] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> dict:
        """User-facing arguments -> the exact query the API accepts.

        Every value that Instahyre validates is resolved here first, so a
        capitalisation slip becomes a suggestion instead of a wire error.
        """
        params: dict[str, Any] = {}
        if skills is not None:
            params["skills"] = [skills] if isinstance(skills, str) else list(skills)
        if job_functions is not None:
            params["job_functions"] = self.taxonomy.resolve_job_functions(job_functions)
        if locations is not None:
            params["jobLocations"] = self.taxonomy.resolve_locations(locations)
        if industries is not None:
            params["industry_types"] = self.taxonomy.resolve_industries(industries)
        if companies is not None:
            params["companies"] = companies
        size = resolve_company_size(company_size)
        if size is not None:
            params["company_size"] = size
        jtype = resolve_job_type(job_type)
        if jtype is not None:
            params["job_type"] = jtype
        if experience_years is not None:
            if not isinstance(experience_years, int) or isinstance(experience_years, bool):
                raise InvalidFilter("experience_years must be a whole number.", field="years")
            params["years"] = experience_years
        if offset:
            params["offset"] = offset
        # limit is sent for completeness but the server floors AND ceilings it
        # at 35, so the response, not the request, is the source of truth.
        params["limit"] = min(int(limit or C.PAGE_SIZE), C.PAGE_SIZE)
        return params

    # -- search ------------------------------------------------------------

    def raw_search(self, params: dict, *, use_cache: bool = True) -> dict:
        key = _cache_key(params)
        if use_cache:
            hit = self.store.get("search", key)
            if hit is not None:
                return hit
        payload = self.http.get(C.EP_JOB_SEARCH, params=params)
        if not isinstance(payload, dict) or "objects" not in payload:
            # Shape drift, not an empty result. Say which.
            from .errors import ApiError

            raise ApiError(
                "job_search returned a payload without an 'objects' list -- the API contract "
                f"has changed. Top-level keys: {sorted(payload)[:10] if isinstance(payload, dict) else type(payload).__name__}",
                path=C.EP_JOB_SEARCH,
            )
        self.store.put("search", key, payload, C.TTL_SEARCH)
        return payload

    def search(
        self,
        *,
        enrich_agency: bool = False,
        exclude_agencies: bool = False,
        max_skills: int = shape.MAX_SKILLS_IN_LIST,
        use_cache: bool = True,
        **filters: Any,
    ) -> dict:
        """Run one page of search and return a compact, explained result."""
        params = self.build_params(**filters)
        payload = self.raw_search(params, use_cache=use_cache)
        meta = payload.get("meta") or {}
        objects = payload.get("objects") or []

        records = [shape.shape_search_record(o, max_skills=max_skills) for o in objects]
        records, dropped = shape.dedupe(records)
        records = shape.collapse_duplicates(records)

        self.store.upsert_jobs(records)
        self.store.record_corpus(
            _corpus_label(params),
            meta.get("total_count"),
            max((r["id"] for r in records if r.get("id")), default=None),
        )

        agency_stats = None
        if enrich_agency or exclude_agencies:
            records, agency_stats = self.enrich_with_agency(records, drop_agencies=exclude_agencies)

        total = meta.get("total_count")
        result: dict[str, Any] = {
            "jobs": records,
            "count_returned": len(records),
            "total_matching": total,
            "offset": meta.get("offset", 0),
            "page_size": meta.get("limit"),
        }
        next_offset = _next_offset(meta)
        if next_offset is not None:
            result["next_offset"] = next_offset
        if dropped:
            result["duplicates_dropped"] = dropped
        if agency_stats:
            result["agency_filter"] = agency_stats
        if not objects:
            result["diagnosis"] = self.diagnose_empty(params)
        return result

    def _next_offset(self, meta: dict) -> Optional[int]:  # pragma: no cover - thin alias
        return _next_offset(meta)

    # -- the silent-empty guard -------------------------------------------

    def diagnose_empty(self, params: dict) -> dict:
        """Explain a zero-result search instead of shrugging at it.

        Instahyre rejects a bad location, company, industry, size or experience
        value with a 400 -- those never reach here. ``skills`` is the exception:
        an unknown skill returns 200 with zero results and looks exactly like a
        genuinely empty market. So when a search comes back empty and a skills
        filter was in play, re-run without it and name the culprit.
        """
        note = {
            "reason": "unknown",
            "explanation": "The query was accepted and genuinely matched no live jobs.",
        }
        skills = params.get("skills")
        if not skills:
            return note

        probe = {k: v for k, v in params.items() if k != "skills"}
        try:
            payload = self.raw_search(probe)
        except Exception as exc:  # diagnosis must never mask the real answer
            note["explanation"] += f" (Could not probe further: {exc})"
            return note

        without_skills = (payload.get("meta") or {}).get("total_count") or 0
        if without_skills == 0:
            note["reason"] = "no_jobs_for_other_filters"
            note["explanation"] = (
                "Zero jobs match even without the skills filter, so the other filters are "
                "what emptied it."
            )
            return note

        # Which of the skills is the dead one? Test each alone against the same
        # base filters. Bounded to four probes so this can never run away.
        dead: list[str] = []
        live: list[str] = []
        for skill in list(skills)[:4]:
            try:
                single = self.raw_search({**probe, "skills": [skill]})
            except Exception:
                continue
            if ((single.get("meta") or {}).get("total_count") or 0) == 0:
                dead.append(skill)
            else:
                live.append(skill)
        note["reason"] = "skills_filter"
        note["explanation"] = (
            f"{without_skills} jobs match the other filters, so the skills filter emptied the "
            "result. Instahyre does not validate skill names -- an unrecognised one silently "
            "returns zero rather than an error."
        )
        if dead:
            note["skills_matching_nothing"] = dead
        if live:
            note["skills_that_do_match"] = live
        return note

    # -- job detail --------------------------------------------------------

    def get_job(
        self, job_id: int, *, description_chars: Optional[int] = shape.DEFAULT_DESCRIPTION_CHARS
    ) -> dict:
        """Full detail for one job. Raises :class:`NotFound` for a bad id."""
        raw = self.raw_detail(job_id)
        record = shape.shape_detail(raw, description_chars=description_chars)
        self.store.record_agency_flag(
            job_id,
            record.get("posted_by_agency", False),
            record.get("agency_name"),
            raw.get("workex_min"),
            raw.get("workex_max"),
        )
        return record

    def raw_detail(self, job_id: int, *, use_cache: bool = True) -> dict:
        key = str(job_id)
        if use_cache:
            hit = self.store.get("detail", key)
            if hit is not None:
                return hit
        payload = self.http.get(C.EP_JOB_DETAIL.format(job_id=job_id))
        if not isinstance(payload, dict) or "id" not in payload:
            from .errors import ApiError

            raise ApiError(
                f"Job detail for {job_id} came back without an 'id' field -- unexpected shape.",
                path=C.EP_JOB_DETAIL.format(job_id=job_id),
            )
        self.store.put("detail", key, payload, C.TTL_DETAIL)
        return payload

    # -- agency enrichment -------------------------------------------------

    def enrich_with_agency(
        self, records: list[dict], *, drop_agencies: bool = False, budget: int = C.PAGE_SIZE
    ) -> tuple[list[dict], dict]:
        """Attach the agency-vs-direct verdict, fetching details where needed.

        The flag is exact and free *per detail request* -- but it does not ride
        on search results, so this costs one request per job not already in the
        cache. Cached verdicts (6 h) make a repeated search nearly free, and
        ``instahyre_sync_index`` exists partly to pre-warm this.
        """
        ids = [r["id"] for r in records if r.get("id")]
        known = self.store.agency_flags(ids, AGENCY_FLAG_MAX_AGE)
        fetched = 0
        failed = 0
        for record in records:
            job_id = record.get("id")
            if job_id is None:
                continue
            info = known.get(job_id)
            if info is None and fetched < budget:
                try:
                    raw = self.raw_detail(job_id)
                except NotFound:
                    record["posted_by_agency"] = None
                    record["agency_unknown_reason"] = "job no longer exists"
                    failed += 1
                    continue
                except Exception as exc:
                    log.warning("agency enrichment failed for %s: %s", job_id, exc)
                    record["posted_by_agency"] = None
                    record["agency_unknown_reason"] = str(exc)[:120]
                    failed += 1
                    continue
                fetched += 1
                is_agency = "agency_function_names" in raw
                agency_name = raw.get("recruiter_company_name")
                if not is_agency or agency_name == raw.get("hiring_company_name"):
                    agency_name = None
                self.store.record_agency_flag(
                    job_id, is_agency, agency_name, raw.get("workex_min"), raw.get("workex_max")
                )
                info = {
                    "is_agency": is_agency,
                    "agency_name": agency_name,
                    "workex_min": raw.get("workex_min"),
                    "workex_max": raw.get("workex_max"),
                }
            if info is None:
                record["posted_by_agency"] = None
                record["agency_unknown_reason"] = "detail budget exhausted"
                continue
            record["posted_by_agency"] = info["is_agency"]
            if info.get("agency_name"):
                record["agency_name"] = info["agency_name"]
            if info.get("workex_min") is not None or info.get("workex_max") is not None:
                record["experience_years"] = _range_str(info.get("workex_min"), info.get("workex_max"))

        stats = {
            "details_fetched": fetched,
            "from_cache": len(known),
            "unresolved": failed,
        }
        if drop_agencies:
            before = len(records)
            kept = [r for r in records if r.get("posted_by_agency") is False]
            unknown = [r for r in records if r.get("posted_by_agency") is None]
            stats["agency_postings_removed"] = before - len(kept) - len(unknown)
            stats["kept_direct_employer_only"] = True
            if unknown:
                stats["dropped_as_unverifiable"] = len(unknown)
            records = kept
        return records, stats

    # -- company -----------------------------------------------------------

    def get_company(self, name: str, *, limit: int = C.PAGE_SIZE) -> dict:
        """One employer's live jobs plus their profile block.

        ``companies`` matches an exact name string, so an unknown name is a 400
        rather than an empty list. That 400 is *data* -- it means "no such
        employer on Instahyre" -- and is reported as ``exists: false``, never
        raised as an error and never confused with "employer exists, zero jobs".
        """
        try:
            payload = self.raw_search(self.build_params(companies=name, limit=limit))
        except InvalidFilter as exc:
            if exc.field == "companies":
                return {
                    "exists": False,
                    "company": name,
                    "message": (
                        f"Instahyre has no employer under the exact name '{name}'. The filter "
                        "matches the full name string, not a substring -- try the company's "
                        "registered name, or search by keyword instead."
                    ),
                }
            raise

        objects = payload.get("objects") or []
        meta = payload.get("meta") or {}
        records = [shape.shape_search_record(o) for o in objects]
        records, _ = shape.dedupe(records)
        self.store.upsert_jobs(records)

        profile: dict[str, Any] = {}
        if objects:
            employer = objects[0].get("employer") or {}
            profile = {
                "id": employer.get("id"),
                "name": employer.get("company_name"),
                "tagline": employer.get("company_tagline"),
                "founded": employer.get("company_founded"),
                "size": shape.size_band(employer.get("employee_count")),
                "about": employer.get("instahyre_note"),
            }
        return {
            "exists": True,
            "company": profile or {"name": name},
            "open_jobs": meta.get("total_count", len(records)),
            "jobs": records,
            "note": (
                "Employer is known to Instahyre but has no live postings right now."
                if not records
                else None
            ),
        }

    # -- market stats ------------------------------------------------------

    def market_stats(self, **filters: Any) -> dict:
        """The facet block only -- no job records at all.

        Costs exactly one request. The facets ride along free on every search
        response, so "how many backend roles in Bangalore right now, by company
        size and seniority" is a single call.
        """
        params = self.build_params(**filters)
        payload = self.raw_search(params)
        meta = payload.get("meta") or {}
        stats = shape.shape_meta(meta, industry_names=self.taxonomy.industry_names())
        objects = payload.get("objects") or []
        max_id = max((o.get("id") or 0 for o in objects), default=None)
        self.store.record_corpus(_corpus_label(params), meta.get("total_count"), max_id)
        history = self.store.corpus_history(_corpus_label(params), limit=8)
        if len(history) > 1:
            stats["tracked_readings"] = len(history)
            stats["previous_total"] = history[1].get("total_count")
            if stats["previous_total"] is not None and stats.get("total_count") is not None:
                stats["change_since_previous"] = stats["total_count"] - stats["previous_total"]
        stats["freshness_note"] = (
            "Instahyre exposes no posting date on any endpoint. Job ids are sequential, so "
            "max_id tracked over time is the only freshness instrument that exists."
        )
        if max_id:
            stats["max_job_id_seen"] = max_id
        return stats

    # -- index sync --------------------------------------------------------

    def sync_index(self, *, max_pages: int = 5, **filters: Any) -> dict:
        """Page through a slice and record every job, so "new since last run" works.

        This is the clock the API refuses to provide: ``first_seen`` in the
        local index is the closest thing to a posting date Instahyre will ever
        give, and it only becomes true once we start writing it down.
        """
        started = time.time()
        offset = filters.pop("offset", 0)
        seen_total = 0
        new_total = 0
        pages = 0
        total_matching = None
        max_id = None
        for _ in range(max(1, max_pages)):
            params = self.build_params(offset=offset, **filters)
            payload = self.raw_search(params, use_cache=False)
            meta = payload.get("meta") or {}
            objects = payload.get("objects") or []
            if total_matching is None:
                total_matching = meta.get("total_count")
            records = [shape.shape_search_record(o) for o in objects]
            records, _ = shape.dedupe(records)
            new, seen = self.store.upsert_jobs(records)
            new_total += new
            seen_total += seen
            pages += 1
            page_max = max((r.get("id") or 0 for r in records), default=0)
            max_id = max(max_id or 0, page_max) or None
            nxt = _next_offset(meta)
            if nxt is None or not objects:
                break
            offset = nxt

        self.store.record_corpus(_corpus_label(self.build_params(**filters)), total_matching, max_id)
        newly = self.store.jobs_first_seen_after(started, limit=25)
        return {
            "pages_fetched": pages,
            "jobs_seen": seen_total,
            "new_since_last_sync": new_total,
            "total_matching": total_matching,
            "new_jobs": [
                {
                    "id": j["id"],
                    "title": j["title"],
                    "company": j["company"],
                    "locations": j["locations"],
                }
                for j in newly
            ],
            "index": self.store.index_stats(),
            "elapsed_seconds": round(time.time() - started, 1),
        }


# -- helpers ----------------------------------------------------------------


def _range_str(lo: Optional[int], hi: Optional[int]) -> Optional[str]:
    if lo is None and hi is None:
        return None
    if lo is not None and hi is not None:
        return f"{lo}-{hi}"
    return f"{lo}+" if lo is not None else f"<={hi}"


def _cache_key(params: dict) -> str:
    parts = []
    for key in sorted(params):
        value = params[key]
        if isinstance(value, (list, tuple)):
            value = ",".join(str(v) for v in sorted(map(str, value)))
        parts.append(f"{key}={value}")
    return "&".join(parts)


def _corpus_label(params: dict) -> str:
    """A stable name for one filter slice, so its totals form a time series."""
    meaningful = {k: v for k, v in params.items() if k not in ("limit", "offset")}
    return _cache_key(meaningful) if meaningful else "all"


def _next_offset(meta: dict) -> Optional[int]:
    """Derive the next offset. ``meta.next`` is a URL; the arithmetic is safer."""
    if not meta.get("next"):
        return None
    offset = meta.get("offset") or 0
    page = meta.get("limit") or C.PAGE_SIZE
    total = meta.get("total_count")
    nxt = offset + page
    if total is not None and nxt >= total:
        return None
    return nxt
