"""The authenticated tier: inbound interest, application state, profile.

Instahyre is a REVERSE marketplace. Outbound search (the public tier) is the
part that looks like every other job board; this module is the part that does
not. Employers put a candidate in a curated queue, recruiters open his resume,
and the whole game is noticing quickly. Everything here serves that.

Three rules run through the file.

**No browser.** The public tier's defining choice was plain HTTP, and the
authenticated tier keeps it. That looked impossible at first -- every profile
route is detail-only, the candidate id is injected into an HTML page, and the
HTML paths are Cloudflare-gated. :func:`Inbound.candidate_id` is the way
through: one collection endpoint does answer GET and every row names its owner.

**A failure never looks like an empty result.** A 401 raises ``AuthRequired``.
An empty queue returns an empty list *with a diagnosis attached* saying whether
filters emptied it or the queue really is bare.

**The one-way door stays shut.** :meth:`Inbound.apply_preview` builds the exact
request and returns it. :meth:`Inbound.submit_interest` is the only function in
this package that can POST to it, it cannot be reached without an explicit
confirmation from the caller, and it refuses point-blank to touch the bulk
endpoint.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable, Optional

from . import constants as C
from . import shape
from .cache import Store
from .errors import ApiError, InstahyreError, InvalidFilter, NotFound
from .http import InstahyreHTTP
# The inbox's read-only guard, borrowed rather than reimplemented. ``inbox``
# imports nothing from this module, so the direction is one-way and there is no
# cycle. It is a substring check on the path actually about to be requested,
# which is worth having on any path built by formatting an id into a template.
from .inbox import guard_read_only

log = logging.getLogger("instahyre.inbound")

#: Where the candidate id is cached once recovered. Namespace, not a file.
_ID_NS = "identity"
_ID_KEY = "candidate_id"
_ID_TTL = 30 * 24 * 3600

_CANDIDATE_URI_RE = re.compile(r"/candidate_misc/profile/candidate/(\d+)")
_RESUME_URI_RE = re.compile(r"/candidate_misc/profile/resume/(\d+)")



class CandidateIdUnavailable(InstahyreError):
    """The numeric candidate id could not be recovered.

    Every profile and settings route needs it and none of them will hand it
    over. This is raised instead of returning an empty profile, because an
    empty profile would read as "you have not filled anything in".
    """

    kind = "candidate_id_unavailable"


class ConfirmationRequired(InstahyreError):
    """A caller reached an irreversible action without confirming it."""

    kind = "confirmation_required"


class Inbound:
    """Authenticated reads, plus the two guarded writes.

    Holds no session of its own: it borrows the client's :class:`InstahyreHTTP`,
    so ``instahyre_logout`` really does disable everything here, and there is
    exactly one cookie jar in the process.
    """

    def __init__(self, http: InstahyreHTTP, store: Store, client: Any = None) -> None:
        self.http = http
        self.store = store
        self.client = client  # for public job detail; optional so tests can omit it
        self._candidate_id: Optional[int] = None

    # -- identity ----------------------------------------------------------

    def candidate_id(self, *, refresh: bool = False) -> int:
        """The numeric candidate id, recovered without a browser.

        Profile and settings are detail-only routes -- a GET on the collection
        is HTTP 405 -- so nothing there works until this returns. The id itself
        is only ever rendered into an authenticated HTML page, and those are
        Cloudflare-gated: 403 to a plain client, an interstitial to headless
        Chromium.

        The way through is that ``/candidate_misc/profile/education`` *is* a
        collection, it *does* answer GET, and every row carries its owner's
        ``resource_uri``. One request, then cached for 30 days.
        """
        if self._candidate_id is not None and not refresh:
            return self._candidate_id
        if not refresh:
            cached = self.store.get(_ID_NS, _ID_KEY)
            if isinstance(cached, int):
                self._candidate_id = cached
                return cached

        payload = self.http.get(C.EP_EDUCATION, params={"limit": 5})
        objects = (payload or {}).get("objects") or []
        for row in objects:
            match = _CANDIDATE_URI_RE.search(str(row.get("candidate") or ""))
            if match:
                found = int(match.group(1))
                self._candidate_id = found
                self.store.put(_ID_NS, _ID_KEY, found, _ID_TTL)
                return found

        raise CandidateIdUnavailable(
            "Could not recover the candidate id. It is normally read off the education "
            "record, but that came back with "
            f"{len(objects)} row(s) and none named an owner. If the Instahyre profile has "
            "no education entry at all, add one on the website -- every profile and "
            "settings route needs this id and no endpoint publishes it directly.",
            education_rows=len(objects),
        )

    # -- the inbound queue -------------------------------------------------

    def list_opportunities(
        self,
        *,
        interest: str = "pending",
        location: Optional[str] = None,
        industry_id: Optional[int] = None,
        company_size: Optional[str] = None,
        job_type: Optional[str] = None,
        limit: int = C.OPP_DEFAULT_LIMIT,
        offset: int = 0,
        full_queue: bool = False,
        use_cache: bool = True,
    ) -> dict:
        """A page of the curated queue, ordered by Instahyre's score.

        The whole queue is fetched, ordered, and only then sliced. That is a
        deliberate inversion of the usual page-then-sort, and it is the
        difference between an honest ranking and a plausible-looking lie: the
        server returns the queue in its own order, so sorting a 5-record page
        and presenting the top of it as "the best match" actually reports the
        best of an arbitrary five. Asking for 5 used to surface a 4.5 while a
        16.05 sat further down the same queue.

        It costs nothing to do properly -- the queue is a few hundred records,
        limit goes to 1000, so it was always one request either way.
        """
        params = self._queue_params(
            interest=interest,
            location=location,
            industry_id=industry_id,
            company_size=company_size,
            job_type=job_type,
            limit=C.OPP_MAX_LIMIT,
            offset=0,
        )
        path = C.EP_OPPORTUNITIES_FULL if full_queue else C.EP_OPPORTUNITIES
        payload = self._queue_request(path, params, use_cache=use_cache)

        meta = payload.get("meta") or {}
        objects = payload.get("objects") or []
        records = [shape.shape_opportunity(o) for o in objects]
        records, dropped = shape.dedupe(records)
        records.sort(key=lambda r: -(r.get("match_score") or 0))

        offset = max(0, int(offset))
        page = records[offset : offset + max(1, int(limit))]

        # Feed the local index so "what is new since last time" can work at all
        # -- Instahyre publishes no date on any job, here or on the public tier.
        self.store.upsert_jobs(
            [
                {
                    "id": r["job_id"],
                    "title": r.get("title"),
                    "company": r.get("company"),
                    "company_id": r.get("company_id"),
                    "company_size": r.get("company_size"),
                    "locations": r.get("locations"),
                }
                for r in records
                if r.get("job_id")
            ]
        )

        result: dict[str, Any] = {
            "opportunities": page,
            "count_returned": len(page),
            "total_matching": meta.get("total_count"),
            "queue_size_after_filters": len(records),
            "offset": offset,
            "interest": interest,
            "ordering": "highest match_score first, ranked across the whole queue",
            "queue_recalculated_at": payload.get("calculation_done_at"),
        }
        if offset + len(page) < len(records):
            result["next_offset"] = offset + len(page)
        if dropped:
            result["duplicates_dropped"] = dropped
        if not page:
            result["diagnosis"] = self._diagnose_empty_queue(interest, params, path)
        return result

    def raw_queue(self, *, interest: str = "pending", use_cache: bool = True) -> dict:
        """The queue payload UNSHAPED, for callers that need the full keyword lists.

        :meth:`list_opportunities` is the tool-facing read and it shapes every
        record, which caps ``skills`` at ``shape.MAX_SKILLS_IN_LIST`` (8) and
        moves the rest to ``skills_more``. That is right for a list a human
        reads and wrong for anything that COUNTS skills: on this account two
        records in a six-record sample already carry 10 and 11 keywords, so an
        analysis fed shaped records silently reports lower bounds.

        Same request, same params, same cache entry as
        :meth:`list_opportunities` -- this is the identical call without the
        shaping, not a second fetch.
        """
        params = self._queue_params(
            interest=interest,
            location=None,
            industry_id=None,
            company_size=None,
            job_type=None,
            limit=C.OPP_MAX_LIMIT,
            offset=0,
        )
        return self._queue_request(C.EP_OPPORTUNITIES, params, use_cache=use_cache)

    def _queue_params(
        self,
        *,
        interest: str,
        location: Optional[str],
        industry_id: Optional[int],
        company_size: Optional[str],
        job_type: Optional[str],
        limit: int,
        offset: int,
    ) -> dict:
        """Queue filters. Deliberately NOT shared with the public search builder.

        The two contracts differ in ways that fail silently rather than loudly:
        the queue wants singular ``location`` and ``industry_type`` where search
        wants plural ``jobLocations`` and ``industry_types``. Passing search's
        spelling here filters nothing at all and looks like a wide result.
        """
        if interest not in C.INTEREST_FACET:
            raise InvalidFilter(
                f"interest must be one of {sorted(C.INTEREST_FACET)}, not {interest!r}.",
                field="interest",
            )
        params: dict[str, Any] = {
            "interest_facet": C.INTEREST_FACET[interest],
            # A bare queue request is HTTP 400 with an empty body. Always sent.
            "limit": max(1, min(int(limit), C.OPP_MAX_LIMIT)),
            "offset": max(0, int(offset)),
        }
        if location:
            params["location"] = location
        if industry_id is not None:
            params["industry_type"] = int(industry_id)
        if company_size is not None:
            from .taxonomy import resolve_company_size

            resolved = resolve_company_size(company_size)
            if resolved:
                params["company_size"] = resolved
        if job_type is not None:
            from .taxonomy import resolve_job_type

            resolved = resolve_job_type(job_type)
            if resolved:
                params["job_type"] = resolved
        return params

    def _queue_request(self, path: str, params: dict, *, use_cache: bool = True) -> dict:
        key = f"{path}|" + "&".join(f"{k}={params[k]}" for k in sorted(params))
        if use_cache:
            hit = self.store.get("opportunities", key)
            if hit is not None:
                return hit
        payload = self.http.get(path, params=params)
        if not isinstance(payload, dict) or "objects" not in payload:
            raise ApiError(
                "The opportunity queue returned a payload with no 'objects' list -- the API "
                f"contract has changed. Top-level keys: "
                f"{sorted(payload)[:10] if isinstance(payload, dict) else type(payload).__name__}",
                path=path,
            )
        self.store.put("opportunities", key, payload, C.TTL_OPPORTUNITIES)
        return payload

    def _diagnose_empty_queue(self, interest: str, params: dict, path: str) -> dict:
        """Say WHY the queue came back empty. Never shrug.

        Three genuinely different situations look identical from a caller's
        side: no employer has ever engaged, the filters excluded everything, or
        he simply has not acted on anything yet so a non-pending view is bare.
        """
        note: dict[str, Any] = {"reason": "unknown"}
        extra = {k: v for k, v in params.items() if k not in ("limit", "offset", "interest_facet")}
        try:
            total = self.navbar_count()
        except InstahyreError as exc:
            note["reason"] = "could_not_probe"
            note["explanation"] = (
                f"The queue is empty for these filters and the total could not be checked ({exc.message})."
            )
            return note

        if total == 0:
            note["reason"] = "no_inbound_interest_yet"
            note["explanation"] = (
                "The curated queue is empty on this account -- no employer has been matched "
                "to this profile yet. This is real information about the account, not a "
                "failure. Instahyre recalculates the queue on a batch cycle; a fuller "
                "profile is the only lever that changes the result."
            )
            return note

        if extra:
            note["reason"] = "filters"
            note["explanation"] = (
                f"{total} opportunities exist in total, so the filters {sorted(extra)} emptied "
                "this view rather than the queue being bare."
            )
            note["filters_applied"] = extra
            return note

        if interest != "pending":
            note["reason"] = "no_action_taken"
            note["explanation"] = (
                f"{total} opportunities are in the queue but none is marked '{interest}'. "
                "That means no application and no decline has been recorded on this account."
            )
            return note

        note["reason"] = "all_actioned"
        note["explanation"] = (
            f"{total} opportunities exist and every one has already been actioned."
        )
        return note

    def opportunity_counts(self) -> dict:
        """The queue's own facet block: one request, no records."""
        payload = self.http.get(C.EP_OPP_FILTER_COUNTS, params={"interest_facet": 0})
        if not isinstance(payload, dict) or not payload.get("success"):
            raise ApiError(
                "fetch_filter_counts did not report success; its contract has changed.",
                path=C.EP_OPP_FILTER_COUNTS,
            )
        return shape.shape_opportunity_counts(payload)

    def navbar_count(self) -> int:
        """The number the website shows in its own navbar badge. One request."""
        payload = self.http.get(C.EP_OPP_NAVBAR_COUNT)
        count = (payload or {}).get("count")
        if not isinstance(count, int):
            raise ApiError(
                f"fetch_navbar_count returned no integer count (keys: {sorted(payload or {})}).",
                path=C.EP_OPP_NAVBAR_COUNT,
            )
        return count

    def find_opportunity(self, opportunity_id: str, *, full_queue: bool = False) -> dict:
        """Locate one queue record by id.

        There is NO detail route for an opportunity -- ``candidate_matching/<id>``
        is a 400, not a 404 -- so the record is found by scanning the queue. That
        costs one request for the whole queue and it is cached, so it is cheaper
        than it sounds, and it is the only way that exists.
        """
        wanted = str(opportunity_id)
        params = self._queue_params(
            interest="pending",
            location=None,
            industry_id=None,
            company_size=None,
            job_type=None,
            limit=C.OPP_MAX_LIMIT,
            offset=0,
        )
        path = C.EP_OPPORTUNITIES_FULL if full_queue else C.EP_OPPORTUNITIES
        for facet in ("pending", "interested", "not_interested"):
            params["interest_facet"] = C.INTEREST_FACET[facet]
            payload = self._queue_request(path, dict(params))
            for obj in payload.get("objects") or []:
                if str(obj.get("id")) == wanted:
                    return obj
        if not full_queue:
            # The two queue resources disagree by ~15 records; try the wider one
            # before concluding the id does not exist.
            return self.find_opportunity(opportunity_id, full_queue=True)
        raise NotFound(
            f"No opportunity {opportunity_id!r} in this account's queue. Opportunity ids are "
            "long numeric strings from instahyre_list_opportunities -- a job id from "
            "instahyre_search_jobs will not match one.",
            opportunity_id=wanted,
        )

    def get_opportunity(self, opportunity_id: str, *, full_description: bool = False) -> dict:
        """One opportunity, composed: queue record + public job detail + siblings.

        Three requests at worst, fewer when cached. The queue record carries the
        match score and the application state; the public job detail carries the
        description, experience band and named recruiter; the sibling call says
        what else that employer has open for him.
        """
        raw = self.find_opportunity(opportunity_id)
        record = shape.shape_opportunity(raw)
        record["interview_status_meaning"] = C.INTERVIEW_STATUS_NAMES.get(
            raw.get("interview_status"), "unknown"
        )

        job_id = record.get("job_id")
        if job_id and self.client is not None:
            try:
                detail = self.client.get_job(
                    job_id,
                    description_chars=None if full_description else shape.DEFAULT_DESCRIPTION_CHARS,
                )
            except NotFound:
                record["job_detail_unavailable"] = (
                    "The public job page for this opportunity no longer exists, so the "
                    "description and recruiter are unavailable. The match itself is still valid."
                )
            else:
                for key in (
                    "description",
                    "description_truncated",
                    "experience_years",
                    "recruiter",
                    "posted_by_agency",
                    "agency_name",
                    "job_functions",
                    "is_active",
                    "url",
                ):
                    if detail.get(key) is not None:
                        record[key] = detail[key]

        try:
            siblings = self.siblings(opportunity_id)
        except InstahyreError as exc:
            record["other_roles_unavailable"] = exc.message
        else:
            if siblings:
                record["other_roles_at_this_employer"] = siblings

        record["salary"] = None
        record["salary_note"] = (
            "Instahyre publishes no salary data on any job (measured: 0% disclosure)."
        )
        return record

    def siblings(self, opportunity_id: str) -> list[dict]:
        """Other roles the same employer has matched him to.

        Carries the experience band, which the queue record itself does not.
        """
        path = C.EP_OPP_SIBLINGS.format(opportunity_id=opportunity_id)
        payload = self.http.get(path)
        if not isinstance(payload, dict) or "opp_data" not in payload:
            raise ApiError(
                "opps_from_this_company returned no 'opp_data' key; its contract has changed.",
                path=path,
            )
        return [shape.shape_sibling_role(row) for row in payload.get("opp_data") or []]

    # -- recruiter activity ------------------------------------------------

    def activity(self, kind: str = "viewed", *, limit: int = 25) -> dict:
        """Who acted on this profile, and when. The most perishable signal here."""
        if kind not in C.ACTIVITY_FACET:
            raise InvalidFilter(
                f"kind must be one of {sorted(C.ACTIVITY_FACET)}, not {kind!r}.", field="kind"
            )
        facet = C.ACTIVITY_FACET[kind]
        payload = self.http.get(
            C.EP_ACTIVITY, params={"activity_facet": facet, "limit": max(1, int(limit))}
        )
        if not isinstance(payload, dict) or "objects" not in payload:
            raise ApiError(
                "employer_activity returned no 'objects' list; its contract has changed.",
                path=C.EP_ACTIVITY,
            )
        events = [shape.shape_activity(o) for o in payload.get("objects") or []]
        meta = payload.get("meta") or {}
        return {
            "kind": kind,
            "meaning": C.ACTIVITY_FACET_LABELS[facet],
            "events": events,
            "count_returned": len(events),
            "total": meta.get("total_count"),
            "timing_note": (
                "action_date arrives pre-formatted for a human ('13 hours ago', "
                "'Aug 17 at 3:47 PM'). Instahyre publishes no machine timestamp, so these "
                "cannot be sorted or compared reliably -- read them, do not compute on them."
            ),
        }

    def activity_counts(self) -> dict:
        """All three activity tabs in one request."""
        payload = self.http.get(C.EP_ACTIVITY_COUNTS)
        counts = (payload or {}).get("facet_counts")
        if not isinstance(counts, dict):
            raise ApiError(
                "fetch_facet_counts returned no 'facet_counts' block; its contract has changed.",
                path=C.EP_ACTIVITY_COUNTS,
            )
        out = {}
        for raw_key, value in counts.items():
            try:
                facet = int(raw_key)
            except (TypeError, ValueError):
                continue
            name = C.ACTIVITY_FACET_NAMES.get(facet, f"facet_{facet}")
            out[name] = {"count": value, "meaning": C.ACTIVITY_FACET_LABELS.get(facet)}
        return out

    def unread_messages(self) -> dict:
        """Unread recruiter messages -- the COUNT, and where the threads are read.

        This returns only the count because that is the only thing it asks for,
        which is a scope, not a limit: ``instahyre_list_conversations``
        enumerates the threads and ``instahyre_read_conversation`` returns their
        bodies, both over plain httpx with no browser.

        This method used to report the bodies as unreachable. That was wrong --
        the conversation list is in a different namespace from the one the
        original search checked -- and the full correction is recorded above
        ``EP_CONVERSATIONS`` in ``constants.py``.
        """
        payload = self.http.get(C.EP_MESSAGE_COUNT, params=dict(C.MESSAGE_COUNT_PARAMS))
        count = (payload or {}).get("message_count")
        if not isinstance(count, int):
            raise ApiError(
                f"message_count returned no integer (keys: {sorted(payload or {})}).",
                path=C.EP_MESSAGE_COUNT,
            )
        return {
            "unread_messages": count,
            "read_them_at": C.SITE_BASE + "/candidate/inbox/",
            "limitation": (
                "This tool returns the count only. The threads themselves ARE reachable over "
                "the API: instahyre_list_conversations lists them and hands back the conv_id "
                "that instahyre_read_conversation needs for a thread's messages. What the "
                "count does not carry is any breakdown -- no per-thread, per-company or "
                "per-recruiter split -- so it answers 'how many' and nothing else."
            ),
        }

    def saved_searches(self) -> dict:
        """Saved job SEARCHES, and the alert toggle each one carries.

        Instahyre has no saved or bookmarked JOBS at all -- no bookmark feature,
        and no job-side endpoint to build one from. What exists is a capped list
        of saved searches, each carrying a single boolean alert.

        This account has ZERO saved searches, so the populated path has never
        been exercised against the live API and every field beyond the alert
        flag is unmeasured -- see :func:`shape.shape_saved_search`. Zero comes
        back with a diagnosis, because "he has never saved one" and "the read
        failed" must be different answers rather than the same empty list.
        """
        # limit=50 against a cap of 5 is deliberate. The cap was READ off the
        # frontend bundle, not measured on the server, so asking for exactly
        # five would mean trusting an unverified number enough to truncate. If
        # this account ever holds more, the tool sees them and the count says so.
        payload = self.http.get(guard_read_only(C.EP_SAVED_SEARCHES), params={"limit": 50})
        objects = (payload or {}).get("objects")
        if objects is None:
            raise ApiError(
                "saved_job_searches returned no 'objects' list; its contract has changed.",
                path=C.EP_SAVED_SEARCHES,
            )
        meta = (payload or {}).get("meta") or {}
        records = [shape.shape_saved_search(row) for row in objects]
        result: dict[str, Any] = {
            "saved_searches": records,
            "count": len(records),
            "max_saved_searches": C.MAX_SAVED_SEARCHES,
            "alerts_on_count": sum(1 for row in records if row["alerts_on"]),
            "note": (
                "Instahyre has no bookmark or saved-job feature -- only saved searches. "
                "There is nothing to list on the job side and no endpoint to build it from. "
                "A job alert is not a separate object either: it is a boolean TOGGLE on one "
                f"of these saved searches ({C.SAVED_SEARCH_ALERT_FIELD}), it can only be "
                f"switched on for a search carrying at least "
                f"{C.SAVED_SEARCH_ALERT_MIN_FILTERS} filters, and an account holds at most "
                f"{C.MAX_SAVED_SEARCHES} saved searches. No alert frequency exists anywhere "
                "in the product -- there is no schedule field of any kind on any of these "
                "records -- so never offer one."
            ),
        }
        if not records:
            result["diagnosis"] = _no_saved_searches_diagnosis(meta)
        return result

    # -- profile and settings ----------------------------------------------

    def _raw_profile(self, *, use_cache: bool = True) -> dict:
        """The unshaped profile payload, cached for ``TTL_PROFILE``.

        Split out of :meth:`profile` because :meth:`resume_info` needs a field
        the shaping deliberately throws away -- ``shape_profile`` reduces the
        whole resume resource to ``has_resume`` -- and re-fetching the same
        payload to see it would be a second request for bytes already in hand.
        """
        cid = self.candidate_id()
        key = str(cid)
        raw = self.store.get("profile", key) if use_cache else None
        if raw is None:
            raw = self.http.get(C.EP_PROFILE.format(candidate_id=cid))
            if not isinstance(raw, dict) or "id" not in raw:
                raise ApiError(
                    f"The profile for candidate {cid} came back without an 'id'; unexpected shape.",
                    path=C.EP_PROFILE.format(candidate_id=cid),
                )
            self.store.put("profile", key, raw, C.TTL_PROFILE)
        return raw

    def profile(self, *, use_cache: bool = True) -> dict:
        """The full candidate profile, shaped, with a completeness assessment."""
        return shape.shape_profile(self._raw_profile(use_cache=use_cache))

    def resume_info(self, *, use_cache: bool = True) -> dict:
        """The uploaded resume: how old it is, and whether Instahyre calls it fresh.

        TWO requests, and the split is deliberate. The resume id is published in
        exactly one place -- the profile payload's own ``resume`` object -- and
        the collection route is HTTP 405, so there is nothing to enumerate and
        nothing to guess from. But the profile is cached for fifteen minutes,
        and a cached copy of the thing being asked about is not an answer. So
        the ID comes from the cached profile, where it is stable, and the
        RECORD is read live off the detail route.

        An account with no resume has no ``resume`` key at all. That returns a
        diagnosis rather than an exception or an empty dict: it is a fact about
        the account, and on a platform whose entire signal is recruiters opening
        a resume, it is the most consequential fact there is.
        """
        raw = self._raw_profile(use_cache=use_cache)
        record = raw.get("resume")
        if not isinstance(record, dict) or not record:
            return _no_resume_result()

        resume_id = _resume_id_from(record)
        if resume_id is None:
            raise ApiError(
                "The profile carries a 'resume' object that names no id -- no 'id' key and "
                f"no resource_uri to parse one out of (keys present: {sorted(record)}). The "
                "resume collection route is HTTP 405, so there is no listing to recover the "
                "id from, and this server will not invent one to request.",
                path=C.EP_PROFILE.format(candidate_id=raw.get("id")),
            )

        path = guard_read_only(C.EP_RESUME.format(resume_id=resume_id))
        payload = self.http.get(path)
        if not isinstance(payload, dict) or "id" not in payload:
            raise ApiError(
                f"The resume record {resume_id} came back without an 'id'; its contract has "
                "changed.",
                path=path,
            )
        return shape.shape_resume(payload)

    def account_settings(self) -> dict:
        """Account settings: visibility, notifications, blocked employers.

        The password fields Instahyre echoes back in this payload are dropped
        before anything is returned or cached.
        """
        cid = self.candidate_id()
        raw = self.http.get(C.EP_SETTINGS.format(candidate_id=cid))
        if not isinstance(raw, dict):
            raise ApiError(
                "candidate_settings returned a non-object payload; its contract has changed.",
                path=C.EP_SETTINGS.format(candidate_id=cid),
            )
        return shape.shape_settings(raw)

    def profile_skills(self) -> list[str]:
        """His own skills, for scoring, so a caller never has to retype them."""
        return self.profile_for_scoring()["skills"]

    def profile_for_scoring(self) -> dict:
        """His own skills AND years, for scoring. Best-effort, never raises.

        Two callers need the same answer for different halves of a score, so
        they share one method rather than two lookups of one profile.

        Swallowing the error is the point, not an oversight: the tools that
        reach for this have already been told they can run without a session,
        so a missing one must cost them the FALLBACK and nothing else. They
        each decide what an empty answer means -- ``instahyre_rank_jobs`` turns
        it back into the same "no skills to score against" it has always
        raised, which is a far more useful thing for a caller to read than an
        auth failure out of a tool that never asked them to log in.

        ``unavailable`` carries the error kind when the read failed and None
        when it succeeded, so a caller can tell "no session" from "a profile
        with no skills on it" -- different problems, different fixes.
        """
        try:
            profile = self.profile()
        except InstahyreError as exc:
            log.info("account profile unavailable for scoring: %s", exc.kind)
            return {"skills": [], "years": None, "unavailable": exc.kind}
        return {
            "skills": list(profile.get("skills") or []),
            # shape_profile's name for the raw payload's ``total_experience``.
            "years": profile.get("total_experience_years"),
            "unavailable": None,
        }

    # -- the one-way door --------------------------------------------------

    def apply_preview(self, opportunity_id: str, *, is_interested: bool) -> dict:
        """Build the exact request an apply (or decline) WOULD send. Sends nothing.

        The body is transcribed from Instahyre's own shipped dispatcher, which
        builds one payload for both actions and switches on a single boolean.
        No request of this shape has ever been executed by this package -- the
        contract comes from reading their code, never from watching a response,
        because the only way to watch one is to send a real application to a
        real employer that cannot be withdrawn.
        """
        raw = self.find_opportunity(opportunity_id)
        record = shape.shape_opportunity(raw)
        path, body = build_apply_request(raw, is_interested=is_interested)

        already = C.INTERVIEW_STATUS_NAMES.get(raw.get("interview_status"), "unknown")
        return {
            "would_send": {
                "method": "POST",
                "url": C.API_BASE + path,
                "json_body": body,
                "headers": {
                    "Content-Type": "application/json",
                    C.APPLY_CSRF_HEADER: "<from the csrftoken cookie>",
                    "Referer": C.SITE_BASE + "/",
                },
            },
            "curl_equivalent": _curl_for(path, body),
            "action": "APPLY" if is_interested else "DECLINE",
            "opportunity": {
                "id": record.get("id"),
                "job_id": record.get("job_id"),
                "title": record.get("title"),
                "company": record.get("company"),
                "locations": record.get("locations"),
                "match_score": record.get("match_score"),
            },
            "current_state": already,
            "irreversible": True,
            "warning": (
                "Instahyre applications CANNOT be withdrawn -- their FAQ says the application "
                "is sent automatically by the system. Declining is equally final. Nothing has "
                "been sent. To go through with it, call again with confirm=True."
            ),
            "contract_provenance": (
                "Request shape read from Instahyre's shipped frontend dispatcher, then "
                "RE-READ INDEPENDENTLY on 2026-08-21 -- which corrected it twice. The "
                "first reading paired the ES body with the non-ES URL (a combination the "
                "frontend never produces, because the same flag switches the service as "
                "well as the body) and omitted is_activity_page_job entirely. Both are "
                "fixed above. It has still never been executed by this server, so the "
                "RESPONSE shape remains unknown; the frontend's success handler reads "
                "opp_id, applied_on and is_visible_on_list_page, and there is no success "
                "flag to check."
            ),
            "branch": "ES (candidate_matching)" if C.APPLY_BRANCH_ES else "legacy (candidate_opportunity)",
            "branch_evidence": (
                "Measured, not assumed: this account's opportunities page fetches "
                "/candidate_matching and /candidate_matching/fetch_filter_counts, and "
                "only the ES service builds that second URL. Re-check any time with "
                "instahyre_verify_apply_target."
            ),
            "no_client_side_brake_upstream": (
                "Worth knowing before trusting the website as a safety net: Instahyre's "
                "own UI has NO confirmation dialog on this action. Every modal in that "
                "flow fires AFTER the POST has already been accepted. The confirm=True "
                "gate here is a stricter guard than the site's."
            ),
        }

    def submit_interest(
        self, opportunity_id: str, *, is_interested: bool, confirm: bool = False
    ) -> dict:
        """Actually POST an apply or a decline. The only write in this package.

        Four things have to be true before anything leaves this process:
        ``confirm=True``, the opportunity is not already in the state being
        asked for, the target path is not on the forbidden list, and the session
        carries a CSRF token. Each of those can genuinely fail -- none is a
        decorative check.

        What this deliberately does NOT do is require that a human saw the
        preview. It cannot: nothing here can observe what the caller showed
        anyone. The gate is one boolean and the calling agent is trusted to
        preview first, which is why every docstring on the way in says so.
        """
        preview = self.apply_preview(opportunity_id, is_interested=is_interested)
        if not confirm:
            raise ConfirmationRequired(
                "Refusing to send an irreversible action without confirm=True.",
                action=preview["action"],
            )

        # Refuse to spend an irreversible action twice. Instahyre may or may not
        # dedupe server-side -- unknown, and unknowable without sending one to
        # find out -- so the refusal happens here, where it costs nothing.
        already = raw_status = preview.get("current_state")
        wanted = "you expressed interest" if is_interested else "you declined"
        if already == wanted:
            raise ConfirmationRequired(
                f"This opportunity is already marked '{raw_status}'. Refusing to send the "
                "same irreversible action a second time -- it cannot improve the outcome and "
                "may reach the employer twice.",
                action=preview["action"],
                current_state=raw_status,
            )

        # A LIVE guard on the path ACTUALLY about to be requested -- the one the
        # builder produced for this specific opportunity, not a constant read
        # again. That distinction is what makes it a check rather than a
        # decoration: it fires on an edited constant, on a hand-built path, and
        # on a branch that resolves somewhere unexpected.
        #
        # The version this replaces compared two distinct constants and was
        # False by construction. It also guarded only ONE of the two bulk URLs,
        # and the one it missed -- candidate_matching/apply_bulk/ -- is the one
        # this account's branch resolves to.
        target = preview["would_send"]["url"]
        path = target[len(C.API_BASE) :] if target.startswith(C.API_BASE) else target
        # THE FIRST HALF OF THIS TEST NOW MATCHES NOTHING, AND IT IS KEPT
        # ANYWAY. FORBIDDEN_ENDPOINTS is empty since the 2026-08-25 ruling
        # lifted the bulk-apply ban, so it is the MARKER half that fires --
        # "apply_bulk" is still in MUTATING_PATH_MARKERS and always will be.
        # The empty set is left in the condition because re-banning a path is a
        # thing a future ruling may want to do in one line, and a guard that
        # had dropped its blocklist clause would silently not honour it.
        if path in C.FORBIDDEN_ENDPOINTS or any(
            marker in path.lower() for marker in C.MUTATING_PATH_MARKERS
        ):
            raise InstahyreError(
                f"Refusing to POST to {path} from the SINGLE-apply path: it names a "
                "mutating action this door may not reach. Bulk apply is no longer out "
                "of scope for this server -- it is instahyre_apply_bulk, which has its "
                "own two-entry allowlist, a hard cap, live validation of every id and a "
                "preview that names every opportunity. None of that is reachable by "
                "pointing single apply at a bulk URL, which is what this refusal stops.",
            )
        if path not in (C.EP_APPLY_ES, C.EP_APPLY_LEGACY):
            raise InstahyreError(
                f"Refusing to POST to {path}: it is not one of the two known apply "
                "endpoints. This server has exactly two POST targets and this is neither.",
            )

        # An unsigned write would be rejected by Django and surface as an
        # unexplained 403 on the one call where a confusing error is most
        # expensive. Refuse before sending, and say what to do about it.
        if not self.http.cookies.get("csrftoken"):
            raise ConfirmationRequired(
                "Refusing to send an irreversible action without a CSRF token -- the session "
                "is not carrying one, so this POST would be rejected and the result would be "
                "ambiguous. Run instahyre_auth_status, and instahyre_login if it says the "
                "session has expired.",
                action=preview["action"],
            )

        # Post exactly what the preview displayed. Rebuilding the body here
        # would let the shown request and the sent request drift apart, which on
        # an irreversible action is the worst possible place for a divergence.
        body = preview["would_send"]["json_body"]

        # The branch is spelled out as two literal call sites rather than one
        # call on a computed path. That is deliberate and it is not redundancy:
        # the suite statically scans every write in this package and requires
        # each to name its endpoint as a bare constant, so "which endpoints can
        # this package POST to" stays a question answerable without running it.
        # The guard above has already proved `path` is one of these two.
        if path == C.EP_APPLY_ES:
            response = self.http.post(
                C.EP_APPLY_ES, json_body=body, extra_headers={"Origin": C.SITE_BASE}
            )
        else:
            response = self.http.post(
                C.EP_APPLY_LEGACY, json_body=body, extra_headers={"Origin": C.SITE_BASE}
            )
        log.warning(
            "irreversible action sent: %s on opportunity %s",
            preview["action"],
            opportunity_id,
        )
        return {
            "sent": True,
            "action": preview["action"],
            "opportunity": preview["opportunity"],
            "irreversible": True,
            "response": response if isinstance(response, dict) else {"raw": str(response)[:200]},
        }


def _resume_id_from(record: dict) -> Optional[int]:
    """The resume id, off ``id`` or out of ``resource_uri``. Never invented.

    The live record names it twice, so either source alone is enough. The
    fallback is here because the collection route is HTTP 405: there is no
    listing to recover an id from, so a record whose ``id`` key went missing
    would otherwise take the whole resource down with it while the id sat in
    the ``resource_uri`` in the same object.
    """
    raw_id = record.get("id")
    if isinstance(raw_id, bool):
        return None
    if isinstance(raw_id, int):
        return raw_id
    if isinstance(raw_id, str) and raw_id.strip().isdigit():
        return int(raw_id.strip())
    match = _RESUME_URI_RE.search(str(record.get("resource_uri") or ""))
    return int(match.group(1)) if match else None


def _no_resume_result() -> dict:
    """The absent-resume answer: the same keys, empty, plus a diagnosis.

    The key set does not change between the two cases, so a caller branches
    once on ``has_resume`` instead of defending against a missing key on every
    field. The diagnosis is what stops "no resume" being read as "the lookup
    broke" -- on this platform those are opposite facts, and only one of them
    is something he can fix.
    """
    return {
        "has_resume": False,
        "resume_id": None,
        "title": None,
        "uploaded_on": None,
        "age_days": None,
        "is_fresh": None,
        "conversion_status": None,
        "download_url": None,
        "diagnosis": {
            "reason": "no_resume_on_file",
            "explanation": (
                "There is no resume on this account. The profile payload came back with no "
                "'resume' key at all -- absent, not null and not an empty object -- which is "
                "exactly how an account that has never uploaded one reads. Nothing was "
                "requested for it: there is no id to look up, and the resume collection "
                "route answers HTTP 405, so there is no listing to search either."
            ),
            "why_it_matters": (
                "Recruiters open the resume and the activity feed counts those opens, so an "
                "account without one is invisible on the single signal this platform runs on."
            ),
            "fix": "Upload one at " + C.SITE_BASE + "/candidate/profile/",
        },
    }


def _no_saved_searches_diagnosis(meta: dict) -> dict:
    """Zero saved searches: say why it is a fact and not a swallowed failure.

    Two situations would look identical if this shrugged -- he has never saved
    one, or the read failed and the failure arrived wearing an empty list. The
    second is structurally unreachable, and the explanation names the mechanism
    rather than asserting the conclusion, so a reader can check it.
    """
    return {
        "reason": "never_saved_one",
        "explanation": (
            "No saved search has ever been created on this account. This is not a failed "
            "read wearing an empty list: a dead session cannot reach this branch, because a "
            "401 raises AuthRequired inside the HTTP client, and a payload that arrived "
            "without an 'objects' list raises ApiError before the count is taken. The "
            "request answered 200 with a tastypie envelope."
        ),
        "total_count_reported": meta.get("total_count"),
    }


def build_apply_request(raw: dict, *, is_interested: bool) -> tuple[str, dict]:
    """The exact (path, body) the frontend would send for this opportunity.

    Split out of the preview so that the preview and the send cannot drift
    apart: there is one builder, and ``submit_interest`` posts what the preview
    displayed rather than reassembling it.

    The branch is the whole story here. ``enableCandidateESOpps`` switches the
    ``$resource`` service, so the URL and the body's id key move TOGETHER:

    * ES     -> ``candidate_matching/apply/``   with ``job_id``
    * legacy -> ``candidate_opportunity/apply/`` with ``id``

    Mixing them, which is what this package did until the contract was re-read,
    produces a request the site never makes.
    """
    body: dict[str, Any] = {"is_interested": is_interested}
    job_id = (raw.get("job") or {}).get("id")
    opp_id = raw.get("id")

    if C.APPLY_BRANCH_ES:
        path = C.EP_APPLY_ES
        if job_id is None:
            raise ApiError(
                "This opportunity has no job.id, which the ES apply body is built from. "
                "Refusing to guess a body for an irreversible action.",
                path=path,
            )
        body["job_id"] = job_id
    else:
        path = C.EP_APPLY_LEGACY
        body["id"] = opp_id if opp_id is not None else None
        # The frontend re-adds job_id only when it has no opportunity id (or is
        # acting on a searched job). Sending it unconditionally, as this package
        # used to, is not what the site does.
        if opp_id is None:
            if job_id is None:
                raise ApiError(
                    "This record has neither an opportunity id nor a job id; there is no "
                    "apply body to build.",
                    path=path,
                )
            body["job_id"] = job_id

    # Set unconditionally by the frontend on every call, on both branches. It is
    # true only for a deep link from the activity page carrying matching
    # ?opp_id and ?job_id query params, which is never how this server applies.
    body["is_activity_page_job"] = False
    return path, body


def _curl_for(path: str, body: dict) -> str:
    """The request as a copy-pasteable curl, so a human can eyeball it.

    Included because "show the operator the exact request" is only true if the
    thing shown is legible. A nested JSON dict in a tool result is a shape; a
    curl line is the request.
    """
    payload = json.dumps(body, sort_keys=True)
    return (
        f"curl -X POST '{C.API_BASE}{path}' "
        f"-H 'Content-Type: application/json' "
        f"-H '{C.APPLY_CSRF_HEADER}: <csrftoken cookie>' "
        f"-H 'Referer: {C.SITE_BASE}/' "
        f"--cookie 'sessionid=<yours>; csrftoken=<yours>' "
        f"-d '{payload}'"
    )


def _next_offset(meta: dict, returned: int) -> Optional[int]:
    offset = meta.get("offset") or 0
    total = meta.get("total_count")
    nxt = offset + (meta.get("limit") or returned or 0)
    if total is None or nxt >= total or returned == 0:
        return None
    return nxt
