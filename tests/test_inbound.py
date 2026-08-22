"""inbound.py -- the authenticated tier's read paths.

Three rules are on trial here, and they are the three the module was written
around. The ranking is computed over the WHOLE queue before a page is cut, so
"the best match" means the best of 228 and not the best of the five that
happened to arrive first. An empty queue comes back with a diagnosis saying
which of the three genuinely different silences it is. And drift in the API
contract raises, because a changed contract reported as "nothing found" is the
one failure this package refuses to ship.

The writes -- apply_preview and submit_interest -- are deliberately not here.
"""

from __future__ import annotations

import pytest

from conftest import fixture_json, make_client
from instahyre_server import constants as C
from instahyre_server.errors import ApiError, InvalidFilter, NotFound
from instahyre_server.inbound import CandidateIdUnavailable

QUEUE = C.EP_OPPORTUNITIES
QUEUE_FULL = C.EP_OPPORTUNITIES_FULL
EDUCATION = C.EP_EDUCATION
NAVBAR = C.EP_OPP_NAVBAR_COUNT
ACTIVITY = C.EP_ACTIVITY
ACTIVITY_COUNTS = C.EP_ACTIVITY_COUNTS
FILTER_COUNTS = C.EP_OPP_FILTER_COUNTS

#: The candidate id every profile fixture belongs to.
CANDIDATE_ID = 9999999
PROFILE = C.EP_PROFILE.format(candidate_id=CANDIDATE_ID)
SETTINGS = C.EP_SETTINGS.format(candidate_id=CANDIDATE_ID)

#: The first record in opportunities_pending.json: Thena, job 438148.
THENA_OPP = "6100000003"
THENA_JOB = 438148


def siblings_path(opportunity_id: str) -> str:
    return C.EP_OPP_SIBLINGS.format(opportunity_id=opportunity_id)


def detail_path(job_id: int) -> str:
    return C.EP_JOB_DETAIL.format(job_id=job_id)


def ranked_ids(payload: dict) -> list:
    """The fixture's own ids, highest score first, ties left in file order."""
    return [str(o["id"]) for o in sorted(payload["objects"], key=lambda o: -float(o["score"]))]


@pytest.fixture
def pending() -> dict:
    return fixture_json("opportunities_pending.json")


@pytest.fixture
def empty_queue() -> dict:
    return fixture_json("opportunities_empty.json")


@pytest.fixture
def education() -> dict:
    return fixture_json("education.json")


# ---------------------------------------------------------------------------
# candidate_id -- the whole authenticated tier is downstream of this
# ---------------------------------------------------------------------------


def test_the_candidate_id_is_recovered_off_the_education_collection_as_an_integer(education):
    """No browser and no HTML page: one collection GET, and the owner uri on a row."""
    client = make_client({EDUCATION: education})

    cid = client.inbound.candidate_id()

    assert cid == CANDIDATE_ID
    assert isinstance(cid, int), "the uri is parsed, not passed through as a string"


def test_the_candidate_id_is_recovered_once_and_then_cached(education):
    client = make_client({EDUCATION: education})

    client.inbound.candidate_id()
    client.inbound.candidate_id()

    assert client.routes.count(EDUCATION) == 1


def test_an_education_collection_with_no_rows_raises_instead_of_returning_nothing(education):
    """A missing id must never read as an empty profile."""
    client = make_client(
        {EDUCATION: {"objects": [], "meta": dict(education["meta"], total_count=0)}}
    )

    result = "sentinel"
    with pytest.raises(CandidateIdUnavailable) as excinfo:
        result = client.inbound.candidate_id()

    assert result == "sentinel", "candidate_id returned instead of raising"
    assert "education" in excinfo.value.message


# ---------------------------------------------------------------------------
# list_opportunities -- the ordering guarantee
# ---------------------------------------------------------------------------


def test_opportunities_come_back_ordered_by_match_score_descending(pending):
    client = make_client({QUEUE: pending})

    result = client.inbound.list_opportunities()

    scores = [r["match_score"] for r in result["opportunities"]]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] == 4.5, "the top of the queue, not the top of the file"


def test_the_whole_queue_is_ranked_before_the_page_is_cut(pending):
    """The assertion that separates an honest ranking from a plausible lie.

    A page-then-sort implementation returns the first two records in the file
    and sorts those two. This fixture's top two by score are NOT its first two,
    so the two implementations cannot agree here.
    """
    file_order = [str(o["id"]) for o in pending["objects"]]
    by_score = ranked_ids(pending)
    assert by_score[:2] != file_order[:2], "fixture no longer proves the point"

    client = make_client({QUEUE: pending})
    result = client.inbound.list_opportunities(limit=2)

    assert [r["id"] for r in result["opportunities"]] == by_score[:2]
    assert result["count_returned"] == 2


def test_the_queue_costs_exactly_one_request_whatever_the_page_size_is(pending):
    small = make_client({QUEUE: pending})
    small.inbound.list_opportunities(limit=2)

    large = make_client({QUEUE: pending})
    large.inbound.list_opportunities(limit=6)

    assert small.routes.count(QUEUE) == 1
    assert large.routes.count(QUEUE) == 1


def test_the_limit_on_the_wire_is_the_maximum_and_not_the_callers_limit(pending):
    """limit slices locally; the request itself always asks for the whole queue."""
    client = make_client({QUEUE: pending})

    client.inbound.list_opportunities(limit=2)

    sent = client.routes.last_params(QUEUE)
    assert sent["limit"] == [str(C.OPP_MAX_LIMIT)] == ["1000"]
    assert sent["offset"] == ["0"], "paging is local, so the wire offset never moves"


def test_offset_slices_the_ranked_queue_locally_and_advertises_the_next_page(pending):
    by_score = ranked_ids(pending)
    client = make_client({QUEUE: pending})

    result = client.inbound.list_opportunities(limit=2, offset=2)

    assert [r["id"] for r in result["opportunities"]] == by_score[2:4]
    assert result["offset"] == 2
    assert result["next_offset"] == 4
    assert client.routes.count(QUEUE) == 1, "a second page is a slice, not a request"


def test_the_last_page_carries_no_next_offset(pending):
    client = make_client({QUEUE: pending})

    result = client.inbound.list_opportunities(limit=2, offset=4)

    assert result["count_returned"] == 2
    assert "next_offset" not in result


def test_each_interest_name_is_translated_to_the_facet_integer_the_queue_wants(pending):
    """The obvious-looking "status" param is accepted and ignored by this API."""
    client = make_client({QUEUE: pending})

    client.inbound.list_opportunities(interest="pending")
    assert client.routes.last_params(QUEUE)["interest_facet"] == ["0"]

    client.inbound.list_opportunities(interest="interested")
    assert client.routes.last_params(QUEUE)["interest_facet"] == ["1"]

    client.inbound.list_opportunities(interest="not_interested")
    assert client.routes.last_params(QUEUE)["interest_facet"] == ["2"]


def test_an_unknown_interest_is_refused_before_any_request_is_made(pending):
    client = make_client({QUEUE: pending})

    with pytest.raises(InvalidFilter) as excinfo:
        client.inbound.list_opportunities(interest="maybe")

    assert excinfo.value.field == "interest"
    assert client.routes.count() == 0, "a bad filter must not cost a request"


def test_full_queue_reads_the_wider_resource_and_the_default_reads_the_narrow_one(pending):
    """The two queue resources disagree by ~15 records; the flag picks which."""
    client = make_client({QUEUE: pending, QUEUE_FULL: fixture_json("opportunities_full.json")})

    narrow = client.inbound.list_opportunities(full_queue=False)
    wide = client.inbound.list_opportunities(full_queue=True)

    assert client.routes.count(QUEUE) == 1
    assert client.routes.count(QUEUE_FULL) == 1
    assert narrow["total_matching"] == 228
    assert wide["total_matching"] == 238


def test_the_queue_location_filter_uses_the_singular_spelling_not_the_search_one(pending):
    """jobLocations is job_search's spelling. Sent here it would filter nothing."""
    client = make_client({QUEUE: pending})

    client.inbound.list_opportunities(location="Bangalore")

    sent = client.routes.last_params(QUEUE)
    assert sent["location"] == ["Bangalore"]
    assert "jobLocations" not in sent


def test_every_record_carries_the_opportunity_id_and_the_job_id_as_different_things(pending):
    """Only the opportunity id can be applied to; only the job id has a public page."""
    client = make_client({QUEUE: pending})

    first = client.inbound.list_opportunities()["opportunities"][0]

    assert isinstance(first["id"], str)
    assert isinstance(first["job_id"], int)
    assert first["id"] != str(first["job_id"])


def test_the_result_names_its_ordering_and_when_the_queue_was_recalculated(pending):
    client = make_client({QUEUE: pending})

    result = client.inbound.list_opportunities()

    assert "ranked across the whole queue" in result["ordering"]
    assert result["queue_recalculated_at"] == "2026-08-13T12:55:30.716597+00:00"


# ---------------------------------------------------------------------------
# An empty queue is explained, never shrugged at
# ---------------------------------------------------------------------------


def test_an_empty_queue_never_comes_back_without_a_diagnosis(empty_queue):
    client = make_client({QUEUE: empty_queue, NAVBAR: fixture_json("navbar_count.json")})

    result = client.inbound.list_opportunities()

    assert result["opportunities"] == []
    diagnosis = result["diagnosis"]
    assert diagnosis["reason"] != "unknown"
    assert diagnosis["explanation"]


def test_a_zero_navbar_count_is_reported_as_no_inbound_interest_yet(empty_queue):
    """Nothing has ever been matched. That is information, not a failure."""
    client = make_client({QUEUE: empty_queue, NAVBAR: {"success": True, "count": 0}})

    diagnosis = client.inbound.list_opportunities()["diagnosis"]

    assert diagnosis["reason"] == "no_inbound_interest_yet"
    assert "no employer has been matched" in diagnosis["explanation"]


def test_an_empty_filtered_view_blames_the_filters_and_names_them(empty_queue):
    client = make_client({QUEUE: empty_queue, NAVBAR: fixture_json("navbar_count.json")})

    diagnosis = client.inbound.list_opportunities(location="Bangalore")["diagnosis"]

    assert diagnosis["reason"] == "filters"
    assert diagnosis["filters_applied"] == {"location": "Bangalore"}
    assert "228 opportunities exist in total" in diagnosis["explanation"]


def test_an_empty_interested_view_is_reported_as_nothing_actioned_yet(empty_queue):
    """228 sit in the queue and none was applied to. Also a fact, not an error."""
    client = make_client({QUEUE: empty_queue, NAVBAR: fixture_json("navbar_count.json")})

    diagnosis = client.inbound.list_opportunities(interest="interested")["diagnosis"]

    assert diagnosis["reason"] == "no_action_taken"
    assert "none is marked 'interested'" in diagnosis["explanation"]


# ---------------------------------------------------------------------------
# Recruiter activity -- the most perishable signal on the platform
# ---------------------------------------------------------------------------


def test_each_activity_kind_is_translated_to_the_facet_integer_the_api_demands():
    """Omitting activity_facet is a 400, so this mapping is load-bearing."""
    client = make_client({ACTIVITY: fixture_json("activity_viewed.json")})

    client.inbound.activity("viewed")
    assert client.routes.last_params(ACTIVITY)["activity_facet"] == ["0"]

    client.inbound.activity("contacted")
    assert client.routes.last_params(ACTIVITY)["activity_facet"] == ["1"]

    client.inbound.activity("not_shortlisted")
    assert client.routes.last_params(ACTIVITY)["activity_facet"] == ["2"]


def test_an_unknown_activity_kind_is_refused_before_any_request_is_made():
    client = make_client({ACTIVITY: fixture_json("activity_viewed.json")})

    with pytest.raises(InvalidFilter) as excinfo:
        client.inbound.activity("ghosted")

    assert excinfo.value.field == "kind"
    assert client.routes.count() == 0


def test_an_event_keeps_the_recruiting_agency_and_the_hiring_company_apart():
    """Recro opened the resume; the role is at CheQ. Collapsing the two would
    misattribute every event on an agency-heavy platform."""
    client = make_client({ACTIVITY: fixture_json("activity_viewed.json")})

    event = client.inbound.activity("viewed")["events"][0]

    assert event["recruiter"] == "Anjali Venkatesh"
    assert event["recruiter_company"] == "Recro"
    assert event["hiring_company"] == "CheQ"
    assert event["recruiter_company"] != event["hiring_company"]
    assert event["job_title"] == "Senior Backend Engineer"


def test_activity_warns_that_its_dates_are_pre_formatted_prose():
    """'13 hours ago' cannot be sorted, compared or subtracted from."""
    client = make_client({ACTIVITY: fixture_json("activity_viewed.json")})

    result = client.inbound.activity("viewed")

    assert result["events"][0]["when"] == "13 hours ago"
    assert "action_date arrives pre-formatted" in result["timing_note"]


def test_activity_counts_names_all_three_tabs_and_says_what_each_one_means():
    client = make_client({ACTIVITY_COUNTS: fixture_json("activity_counts.json")})

    counts = client.inbound.activity_counts()

    assert sorted(counts) == ["contacted", "not_shortlisted", "viewed"]
    assert counts["viewed"] == {"count": 3, "meaning": "viewed your resume"}
    assert counts["contacted"]["meaning"] == "contacted you"
    assert counts["not_shortlisted"]["meaning"] == "did not shortlist you"


# ---------------------------------------------------------------------------
# Profile and settings
# ---------------------------------------------------------------------------


def test_the_profile_resolves_the_candidate_id_first_and_then_asks_for_that_id(education):
    """Every profile route is detail-only, so the id has to be found first."""
    client = make_client({EDUCATION: education, PROFILE: fixture_json("candidate_profile.json")})

    client.inbound.profile()

    assert client.routes.paths[0] == EDUCATION
    assert client.routes.paths[1].endswith("/9999999")
    assert client.routes.count() == 2


def test_the_profile_carries_a_completeness_block_that_ranks_the_gaps(education):
    client = make_client({EDUCATION: education, PROFILE: fixture_json("candidate_profile.json")})

    completeness = client.inbound.profile()["completeness"]

    assert completeness["score"] == "7/8"
    assert completeness["percent"] == 88
    assert [gap["field"] for gap in completeness["gaps"]] == ["phone verified"]
    assert all(gap["why_it_matters"] for gap in completeness["gaps"])


def test_profile_skills_are_a_real_list_and_not_the_comma_joined_string(education):
    client = make_client({EDUCATION: education, PROFILE: fixture_json("candidate_profile.json")})

    profile = client.inbound.profile()

    assert profile["skills"] == ["RabbitMQ", "Scala", "Svelte", "Kotlin"]
    assert profile["current_company"] == "Halcyon Grid Technologies"
    assert profile["total_experience_years"] == 7


def test_the_profile_is_fetched_once_and_served_from_the_store_after_that(education):
    client = make_client({EDUCATION: education, PROFILE: fixture_json("candidate_profile.json")})

    client.inbound.profile()
    client.inbound.profile()

    assert client.routes.count(PROFILE) == 1
    assert client.routes.count(EDUCATION) == 1


def test_account_settings_never_emits_the_password_fields_the_api_echoes_back(education):
    """Instahyre really does return password fields on this GET. They are
    stripped before anything is returned, cached or logged."""
    raw = fixture_json("candidate_settings.json")
    assert {"password", "current_password", "confirm_password"} <= set(
        raw
    ), "the fixture must carry the password keys or this test proves nothing"
    client = make_client({EDUCATION: education, SETTINGS: raw})

    settings = client.inbound.account_settings()

    assert [key for key in settings if "password" in key] == ["has_password_login"]
    assert "password" not in settings
    assert "current_password" not in settings
    assert "confirm_password" not in settings


# ---------------------------------------------------------------------------
# Composition, siblings, counts
# ---------------------------------------------------------------------------


def test_get_opportunity_merges_the_public_job_detail_into_the_queue_record(pending):
    """The queue record has the score and the state; only the public page has
    the description and the named recruiter."""
    detail = dict(fixture_json("detail_direct.json"), id=THENA_JOB)
    client = make_client(
        {
            QUEUE: pending,
            detail_path(THENA_JOB): detail,
            siblings_path(THENA_OPP): fixture_json("sibling_roles.json"),
        }
    )

    record = client.inbound.get_opportunity(THENA_OPP)

    assert record["id"] == THENA_OPP
    assert record["job_id"] == THENA_JOB
    assert record["match_score"] == 2.475
    assert record["description"]
    assert record["recruiter"]["name"] == "Shalini Krishnan"


def test_get_opportunity_on_an_unknown_id_explains_the_two_kinds_of_id(pending):
    """A job id pasted where an opportunity id belongs is the obvious mistake."""
    client = make_client({QUEUE: pending, QUEUE_FULL: fixture_json("opportunities_full.json")})

    result = "sentinel"
    with pytest.raises(NotFound) as excinfo:
        result = client.inbound.get_opportunity(str(THENA_JOB))

    assert result == "sentinel", "get_opportunity returned instead of raising"
    assert "a job id from" in excinfo.value.message
    assert "instahyre_list_opportunities" in excinfo.value.message


def test_siblings_maps_each_row_to_an_id_a_title_and_an_experience_band():
    """The band is why this call exists: the queue record carries no such field."""
    client = make_client({siblings_path(THENA_OPP): fixture_json("sibling_roles.json")})

    rows = client.inbound.siblings(THENA_OPP)

    assert len(rows) == 14
    assert rows[0] == {
        "opportunity_id": "6175103713",
        "title": "AI Automation Developer",
        "locations": ["Bangalore"],
        "experience_years": "5-9",
    }


def test_an_employer_with_no_other_roles_yields_an_empty_list_and_no_error():
    client = make_client({siblings_path(THENA_OPP): fixture_json("sibling_roles_empty.json")})

    assert client.inbound.siblings(THENA_OPP) == []


def test_opportunity_counts_names_the_statuses_instead_of_returning_bare_integers():
    client = make_client({FILTER_COUNTS: fixture_json("opportunity_counts.json")})

    counts = client.inbound.opportunity_counts()

    assert counts["total"] == 238
    assert counts["by_status"] == {"pending": 238, "interested": 0, "not_interested": 0}
    assert counts["top_companies"][0] == {"id": 48328, "name": "Arctic Wolf", "count": 15}


def test_unread_messages_returns_the_count_and_points_at_the_thread_tools():
    """The count is this tool's SCOPE, not a limit -- the threads are readable.

    The name and docstring here used to say the threads were unreachable, which
    was true of an earlier build and false since the conversation list was
    found (see the correction above ``EP_CONVERSATIONS`` in constants). The
    assertion below never changed: ``conv_id`` appears in the message either
    way, because it is the thing the caller needs in BOTH stories. That is
    worth noticing -- an assertion that survives the fact it was pinning is not
    pinning it, and ``test_resume_saved.py`` carries the tripwire that is.
    """
    client = make_client({C.EP_MESSAGE_COUNT: fixture_json("message_count.json")})

    result = client.inbound.unread_messages()

    assert result["unread_messages"] == 0
    assert "conv_id" in result["limitation"]
    assert result["read_them_at"].endswith("/candidate/inbox/")


def test_saved_searches_explains_that_saved_jobs_do_not_exist_on_this_platform():
    client = make_client({C.EP_SAVED_SEARCHES: fixture_json("saved_searches.json")})

    result = client.inbound.saved_searches()

    assert result["saved_searches"] == []
    assert result["count"] == 0
    assert "no bookmark or saved-job feature" in result["note"]


# ---------------------------------------------------------------------------
# Contract drift is loud
# ---------------------------------------------------------------------------


def test_a_queue_payload_without_objects_is_drift_and_not_an_empty_queue():
    client = make_client({QUEUE: {"meta": {"total_count": 228}}})

    with pytest.raises(ApiError) as excinfo:
        client.inbound.list_opportunities()

    assert "no 'objects' list" in excinfo.value.message


def test_a_navbar_count_without_an_integer_is_drift_and_not_a_count_of_zero():
    client = make_client({NAVBAR: {"success": True}})

    with pytest.raises(ApiError) as excinfo:
        client.inbound.navbar_count()

    assert "no integer count" in excinfo.value.message


def test_activity_counts_without_a_facet_block_is_drift_and_not_three_empty_tabs():
    client = make_client({ACTIVITY_COUNTS: {"success": True}})

    with pytest.raises(ApiError) as excinfo:
        client.inbound.activity_counts()

    assert "no 'facet_counts' block" in excinfo.value.message
