"""client.py -- filter assembly, shaping, and the silent-empty guard.

The one rule under test throughout: an empty result must be explainable, and a
failure must never arrive dressed as an empty result.
"""

from __future__ import annotations

from typing import Optional

import httpx
import pytest

from conftest import (
    HTML_404_BODY,
    fixture_json,
    html_response,
    json_response,
    make_client,
)
from instahyre_server import constants as C
from instahyre_server.client import _next_offset
from instahyre_server.errors import ApiError, InvalidFilter, NotFound

SEARCH = C.EP_JOB_SEARCH


def detail_path(job_id: int) -> str:
    return C.EP_JOB_DETAIL.format(job_id=job_id)


def search_route(default, *, by_skills: Optional[dict] = None):
    """A /job_search/ handler that answers differently per skills filter.

    ``by_skills`` maps a tuple of skill values to a payload; the empty tuple is
    the no-skills probe. Anything unlisted falls back to ``default``.
    """
    table = {tuple(sorted(k)): v for k, v in (by_skills or {}).items()}

    def handler(request: httpx.Request):
        key = tuple(sorted(request.url.params.get_list("skills")))
        return json_response(table.get(key, default))

    return handler


# ---------------------------------------------------------------------------
# build_params
# ---------------------------------------------------------------------------


def test_build_params_produces_the_exact_wire_query():
    client = make_client()

    params = client.build_params(
        skills=["Node.js", "TypeScript"],
        job_functions=["Backend Development"],
        locations=["bangalore"],
        company_size="large",
        job_type="full_time",
        experience_years=5,
        offset=35,
    )

    assert params == {
        "skills": ["Node.js", "TypeScript"],
        "job_functions": [10],
        "jobLocations": ["Bangalore"],
        "company_size": 2,
        "job_type": 1,
        "years": 5,
        "offset": 35,
        "limit": 35,
    }


def test_build_params_always_sends_the_35_limit():
    client = make_client()
    assert client.build_params()["limit"] == 35
    assert client.build_params(limit=100)["limit"] == 35, "the server ceilings at 35"
    assert client.build_params(limit=5)["limit"] == 5


def test_build_params_omits_a_zero_offset():
    assert "offset" not in make_client().build_params()


def test_build_params_accepts_a_bare_string_for_list_filters():
    client = make_client()
    params = client.build_params(skills="Node.js", locations="Bangalore")
    assert params["skills"] == ["Node.js"]
    assert params["jobLocations"] == ["Bangalore"]


def test_build_params_rejects_a_non_integer_experience():
    with pytest.raises(InvalidFilter) as excinfo:
        make_client().build_params(experience_years="five")
    assert excinfo.value.field == "years"


def test_build_params_surfaces_a_bad_location_as_a_suggestion_not_a_request():
    """The resolver fires before the wire, so no request is wasted on a typo."""
    client = make_client()
    with pytest.raises(InvalidFilter) as excinfo:
        client.build_params(locations=["Banglore"])
    assert "Did you mean" in excinfo.value.message
    assert client.routes.count(SEARCH) == 0


def test_build_params_reaches_the_wire_with_repeated_keys(search_payload):
    client = make_client({SEARCH: search_payload})
    client.search(skills=["Node.js", "TypeScript"], locations=["bangalore"])

    sent = client.routes.last_params(SEARCH)
    assert sent["skills"] == ["Node.js", "TypeScript"]
    assert sent["jobLocations"] == ["Bangalore"]
    assert sent["limit"] == ["35"]


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_returns_shaped_jobs_with_totals_and_paging(search_payload):
    client = make_client({SEARCH: search_payload})

    result = client.search(locations=["Bangalore"], job_functions=["Backend Development"])

    assert result["count_returned"] == 35
    assert result["total_matching"] == 1086
    assert result["offset"] == 0
    assert result["page_size"] == 35
    assert result["next_offset"] == 35
    assert "diagnosis" not in result, "a full page needs no explanation"

    first = result["jobs"][0]
    assert first["id"] == 432558
    assert first["company"] == "Ridgeline Analytics"
    assert "employer" not in first, "records are shaped, never raw"
    assert "resource_uri" not in first


def test_search_flags_duplicate_postings(search_payload):
    client = make_client({SEARCH: search_payload})
    result = client.search()
    flagged = [job for job in result["jobs"] if "duplicate_ids" in job]
    assert len(flagged) == 2
    assert {job["title"] for job in flagged} == {"Lead Engineer"}


def test_search_writes_every_record_into_the_local_index(search_payload):
    client = make_client({SEARCH: search_payload})
    client.search()
    assert client.store.index_stats()["jobs_indexed"] == 35


def test_search_records_a_corpus_reading(search_payload):
    client = make_client({SEARCH: search_payload})
    client.search(locations=["Bangalore"])
    history = client.store.corpus_history("jobLocations=Bangalore")
    assert history[0]["total_count"] == 1086


def test_search_is_served_from_cache_the_second_time(search_payload):
    client = make_client({SEARCH: search_payload})
    client.search(locations=["Bangalore"])
    client.search(locations=["Bangalore"])
    assert client.routes.count(SEARCH) == 1


def test_search_can_bypass_the_cache(search_payload):
    client = make_client({SEARCH: search_payload})
    client.search(locations=["Bangalore"], use_cache=False)
    client.search(locations=["Bangalore"], use_cache=False)
    assert client.routes.count(SEARCH) == 2


def test_search_propagates_a_400_rather_than_returning_empty():
    client = make_client({SEARCH: json_response({"years": ["Invalid years"]}, status=400)})
    with pytest.raises(InvalidFilter):
        client.search(experience_years=99)


# ---------------------------------------------------------------------------
# _next_offset
# ---------------------------------------------------------------------------


def test_next_offset_is_none_when_the_page_reaches_the_total():
    meta = {"next": "/api/v1/job_search?offset=1095", "offset": 1060, "limit": 35, "total_count": 1086}
    assert _next_offset(meta) is None


def test_next_offset_is_none_on_the_exact_boundary():
    meta = {"next": "/api/v1/job_search?offset=70", "offset": 35, "limit": 35, "total_count": 70}
    assert _next_offset(meta) is None


def test_next_offset_advances_by_the_page_size(search_payload):
    assert _next_offset(search_payload["meta"]) == 35


def test_next_offset_is_none_when_the_server_offers_no_next():
    assert _next_offset({"next": None, "offset": 0, "limit": 35, "total_count": 1086}) is None


def test_next_offset_alias_on_the_client_agrees(search_payload):
    client = make_client()
    assert client._next_offset(search_payload["meta"]) == 35


def test_search_omits_next_offset_on_the_last_page(search_payload):
    payload = {"objects": search_payload["objects"], "meta": dict(search_payload["meta"])}
    payload["meta"]["next"] = None
    client = make_client({SEARCH: payload})
    assert "next_offset" not in client.search()


# ---------------------------------------------------------------------------
# The silent-empty guard
# ---------------------------------------------------------------------------


def test_an_empty_result_from_a_dead_skill_is_diagnosed(search_payload, empty_payload):
    """Instahyre answers an unknown skill with 200 + total_count 0. Without a
    diagnosis that is indistinguishable from a genuinely dry market."""
    client = make_client(
        {
            SEARCH: search_route(
                empty_payload,
                by_skills={
                    (): search_payload,  # the no-skills probe
                    ("Java",): search_payload,  # a skill that does match
                    ("Nodejs-Typo",): empty_payload,  # the dead one
                },
            )
        }
    )

    result = client.search(skills=["Nodejs-Typo", "Java"])

    assert result["jobs"] == []
    assert result["total_matching"] == 0
    diagnosis = result["diagnosis"]
    assert diagnosis["reason"] == "skills_filter"
    assert diagnosis["skills_matching_nothing"] == ["Nodejs-Typo"]
    assert diagnosis["skills_that_do_match"] == ["Java"]
    assert "1086 jobs match the other filters" in diagnosis["explanation"]


def test_the_diagnosis_probes_are_bounded(search_payload, empty_payload):
    """One probe without skills, then at most four single-skill probes."""
    client = make_client(
        {SEARCH: search_route(empty_payload, by_skills={(): search_payload})}
    )

    client.search(skills=["a", "b", "c", "d", "e", "f"])

    # 1 real search + 1 no-skills probe + 4 single-skill probes.
    assert client.routes.count(SEARCH) == 6


def test_an_empty_result_blames_the_other_filters_when_they_are_the_cause(empty_payload):
    client = make_client({SEARCH: empty_payload})

    result = client.search(skills=["Java"], locations=["Bangalore"])

    assert result["diagnosis"]["reason"] == "no_jobs_for_other_filters"
    assert "without the skills filter" in result["diagnosis"]["explanation"]


def test_an_empty_result_with_no_skills_filter_is_reported_as_genuinely_empty(empty_payload):
    client = make_client({SEARCH: empty_payload})

    result = client.search(locations=["Bangalore"])

    assert result["diagnosis"]["reason"] == "unknown"
    assert "genuinely matched no live jobs" in result["diagnosis"]["explanation"]
    assert client.routes.count(SEARCH) == 1, "no probe is worth making without a skills filter"


def test_a_failing_probe_never_masks_the_real_answer(empty_payload):
    """The diagnosis is a nicety; it must not turn an answer into an error."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return json_response(empty_payload)
        return json_response({"detail": "nope"}, status=403)

    client = make_client({SEARCH: handler})
    result = client.search(skills=["Java"])

    assert result["jobs"] == []
    assert result["diagnosis"]["reason"] == "unknown"
    assert "Could not probe further" in result["diagnosis"]["explanation"]


# ---------------------------------------------------------------------------
# raw_search contract
# ---------------------------------------------------------------------------


def test_raw_search_raises_api_error_when_objects_is_missing():
    """Shape drift is not an empty result, and must not be reported as one."""
    client = make_client({SEARCH: {"meta": {"total_count": 10}}})

    with pytest.raises(ApiError) as excinfo:
        client.raw_search({"limit": 35})

    assert "without an 'objects' list" in excinfo.value.message
    assert "meta" in excinfo.value.message


def test_raw_search_raises_api_error_on_a_list_payload():
    client = make_client({SEARCH: [1, 2, 3]})
    with pytest.raises(ApiError):
        client.raw_search({"limit": 35})


def test_raw_search_accepts_a_legitimately_empty_objects_list(empty_payload):
    """Zero jobs is a valid answer; only a missing key is drift."""
    client = make_client({SEARCH: empty_payload})
    assert client.raw_search({"limit": 35})["objects"] == []


# ---------------------------------------------------------------------------
# get_job
# ---------------------------------------------------------------------------


def test_get_job_shapes_a_real_detail(detail_direct):
    client = make_client({detail_path(432558): detail_direct})

    record = client.get_job(432558)

    assert record["id"] == 432558
    assert record["company"] == "Ridgeline Analytics"
    assert record["posted_by_agency"] is False
    assert record["salary"] is None
    assert "<" not in record["description"]


def test_get_job_caches_the_agency_verdict(detail_agency):
    client = make_client({detail_path(438126): detail_agency})

    client.get_job(438126)

    cached = client.store.agency_flags([438126], max_age=C.TTL_DETAIL)
    assert cached[438126]["is_agency"] is True
    assert cached[438126]["agency_name"] == "Recro"
    assert cached[438126]["workex_min"] == 2


def test_get_job_on_a_404_raises_not_found_and_returns_nothing():
    """The headline failure this server refuses to make."""
    client = make_client({detail_path(999999): html_response(HTML_404_BODY, status=404)})

    result = "sentinel"
    with pytest.raises(NotFound) as excinfo:
        result = client.get_job(999999)

    assert result == "sentinel", "get_job returned instead of raising"
    assert excinfo.value.kind == "not_found"


def test_get_job_is_cached_after_the_first_fetch(detail_direct):
    client = make_client({detail_path(432558): detail_direct})
    client.get_job(432558)
    client.get_job(432558)
    assert client.routes.count(detail_path(432558)) == 1


def test_raw_detail_raises_api_error_when_the_id_field_is_missing():
    client = make_client({detail_path(1): {"title": "no id here"}})
    with pytest.raises(ApiError) as excinfo:
        client.raw_detail(1)
    assert "without an 'id' field" in excinfo.value.message


# ---------------------------------------------------------------------------
# get_company
# ---------------------------------------------------------------------------


def test_get_company_reports_an_unknown_name_as_exists_false_without_raising():
    """The 400 is data: it means "no such employer", not "the call failed"."""
    client = make_client({SEARCH: json_response(fixture_json("error_400_company.json"), status=400)})

    result = client.get_company("Definitely Not A Company")

    assert result["exists"] is False
    assert result["company"] == "Definitely Not A Company"
    assert "no employer under the exact name" in result["message"]
    assert client.routes.count(SEARCH) == 1, "a 400 must not be retried"


def test_get_company_returns_exists_true_with_jobs_for_a_real_employer():
    client = make_client({SEARCH: fixture_json("company_amazon.json")})

    result = client.get_company("Amazon")

    assert result["exists"] is True
    assert result["open_jobs"] == 212
    assert len(result["jobs"]) == 35
    assert result["company"]["name"] == "Amazon"
    assert result["company"]["founded"] == 1994
    assert result["company"]["size"] == "large"
    assert result["note"] is None


def test_get_company_distinguishes_zero_live_jobs_from_no_such_employer(empty_payload):
    client = make_client({SEARCH: empty_payload})

    result = client.get_company("Quiet Employer")

    assert result["exists"] is True
    assert result["jobs"] == []
    assert "no live postings right now" in result["note"]


def test_get_company_re_raises_a_400_about_a_different_field():
    """Only a companies-400 means "unknown employer". Anything else is an error."""
    client = make_client({SEARCH: json_response({"years": ["Invalid years"]}, status=400)})
    with pytest.raises(InvalidFilter):
        client.get_company("Amazon")


def test_get_company_indexes_the_jobs_it_saw():
    client = make_client({SEARCH: fixture_json("company_amazon.json")})
    client.get_company("Amazon")
    assert client.store.index_stats()["jobs_indexed"] == 35


# ---------------------------------------------------------------------------
# Agency enrichment
# ---------------------------------------------------------------------------


def test_exclude_agencies_drops_agency_postings(search_payload, detail_direct, detail_agency):
    page = {"objects": search_payload["objects"][:2], "meta": dict(search_payload["meta"])}
    client = make_client(
        {
            SEARCH: page,
            detail_path(432558): detail_direct,
            detail_path(438126): detail_agency,
        }
    )

    result = client.search(exclude_agencies=True)

    assert [job["id"] for job in result["jobs"]] == [432558]
    assert result["agency_filter"]["agency_postings_removed"] == 1
    assert result["agency_filter"]["details_fetched"] == 2


def test_show_agency_flag_annotates_without_filtering(search_payload, detail_direct, detail_agency):
    page = {"objects": search_payload["objects"][:2], "meta": dict(search_payload["meta"])}
    client = make_client(
        {
            SEARCH: page,
            detail_path(432558): detail_direct,
            detail_path(438126): detail_agency,
        }
    )

    result = client.search(enrich_agency=True)

    by_id = {job["id"]: job for job in result["jobs"]}
    assert by_id[432558]["posted_by_agency"] is False
    assert by_id[438126]["posted_by_agency"] is True
    assert by_id[438126]["agency_name"] == "Recro"
    assert by_id[438126]["experience_years"] == "2-5"


def test_enrichment_marks_a_vanished_job_unknown_rather_than_guessing(search_payload):
    page = {"objects": search_payload["objects"][:1], "meta": dict(search_payload["meta"])}
    client = make_client({SEARCH: page, detail_path(432558): html_response(HTML_404_BODY, 404)})

    result = client.search(enrich_agency=True)

    job = result["jobs"][0]
    assert job["posted_by_agency"] is None
    assert job["agency_unknown_reason"] == "job no longer exists"
    assert result["agency_filter"]["unresolved"] == 1


def test_enrichment_reuses_a_cached_verdict(search_payload, detail_direct):
    page = {"objects": search_payload["objects"][:1], "meta": dict(search_payload["meta"])}
    client = make_client({SEARCH: page, detail_path(432558): detail_direct})

    client.search(enrich_agency=True, use_cache=False)
    client.search(enrich_agency=True, use_cache=False)

    assert client.routes.count(detail_path(432558)) == 1


# ---------------------------------------------------------------------------
# market_stats and sync_index
# ---------------------------------------------------------------------------


def test_market_stats_costs_one_search_request_and_returns_no_jobs(search_payload):
    client = make_client({SEARCH: search_payload})

    stats = client.market_stats(locations=["Bangalore"])

    assert "jobs" not in stats
    assert stats["total_count"] == 1086
    assert stats["max_job_id_seen"] == max(o["id"] for o in search_payload["objects"])
    assert client.routes.count(SEARCH) == 1


def test_market_stats_resolves_industry_facet_ids(search_payload):
    client = make_client({SEARCH: search_payload})
    stats = client.market_stats()
    assert stats["top_industries"][0]["name"] == "Computer Software / IT / Internet"


def test_market_stats_reports_the_change_since_the_previous_reading(search_payload):
    client = make_client({SEARCH: search_payload})
    client.market_stats(locations=["Bangalore"])
    stats = client.market_stats(locations=["Bangalore"])

    assert stats["tracked_readings"] == 2
    assert stats["previous_total"] == 1086
    assert stats["change_since_previous"] == 0


def test_sync_index_walks_pages_and_reports_what_is_new(search_payload):
    page_two = {
        "objects": [dict(obj, id=obj["id"] + 500_000) for obj in search_payload["objects"]],
        "meta": dict(search_payload["meta"], offset=35, next=None),
    }
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return json_response(search_payload if calls["n"] == 1 else page_two)

    client = make_client({SEARCH: handler})
    result = client.sync_index(max_pages=5)

    assert result["pages_fetched"] == 2
    assert result["jobs_seen"] == 70
    assert result["new_since_last_sync"] == 70
    assert result["total_matching"] == 1086
    assert len(result["new_jobs"]) == 25, "the report caps the listing at 25"
    assert result["index"]["jobs_indexed"] == 70


def test_a_second_sync_reports_nothing_new(search_payload):
    payload = {"objects": search_payload["objects"], "meta": dict(search_payload["meta"], next=None)}
    client = make_client({SEARCH: payload})

    client.sync_index(max_pages=3)
    second = client.sync_index(max_pages=3)

    assert second["jobs_seen"] == 35
    assert second["new_since_last_sync"] == 0
    assert second["new_jobs"] == []
