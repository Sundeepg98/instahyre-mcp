"""The resume record, saved searches, and one retired falsehood.

Three things are on trial here and they share a theme: what the server tells a
caller about a resource it can only partly see.

**The resume.** ``shape_profile`` reduced an eleven-key record to
``has_resume: true``, which throws away the one field that decides whether
recruiters open it at all -- ``is_fresh``. The record is readable on a detail
route, and only a detail route: the collection is 405, so the id has to come
off the profile payload. The absent-resume case is the load-bearing one, and it
gets a diagnosis instead of an exception or a shrug.

**Saved searches.** He has zero, so every populated assertion below runs on a
fixture that says ``_synthetic`` at the top and means it. Zero also has to be
told apart from a dead session, and the test that proves the difference is the
one where the session IS dead.

**The retired claim.** ``unread_messages`` shipped "no endpoint lists
conversations, so threads must be read on the website" while this same package
shipped three tools that read them. The tripwire at the bottom is not a unit
test: it scans the package's own string literals so the sentence cannot come
back in a docstring, a note or a returned value.
"""

from __future__ import annotations

import ast
import copy
import json
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from conftest import fixture_json, json_response, make_client
from instahyre_server import constants as C
from instahyre_server import shape
from instahyre_server.errors import ApiError, AuthRequired

EDUCATION = C.EP_EDUCATION
SAVED = C.EP_SAVED_SEARCHES

#: The candidate id every profile fixture belongs to, and the resume id its
#: ``resume`` object names. Both are read from the fixture, never hardcoded
#: into the implementation.
CANDIDATE_ID = 9999999
PROFILE = C.EP_PROFILE.format(candidate_id=CANDIDATE_ID)
RESUME_ID = 7770003
RESUME = C.EP_RESUME.format(resume_id=RESUME_ID)

#: The collection route. VERIFIED 405 live, which is why nothing here may ever
#: request it -- there is no listing to fall back on when an id is missing.
RESUME_COLLECTION = "/candidate_misc/profile/resume/"

PACKAGE_DIR = pathlib.Path(C.__file__).resolve().parent


@pytest.fixture
def education() -> dict:
    return fixture_json("education.json")


@pytest.fixture
def profile_payload() -> dict:
    return fixture_json("candidate_profile.json")


@pytest.fixture
def resume_record(profile_payload: dict) -> dict:
    """The resume detail payload, taken from the profile's own embedded copy.

    The live detail route returned exactly these keys on 2026-08-21, so the
    embedded object doubles as the golden record for the route -- and using it
    keeps the two halves of the contract from drifting apart in the fixtures.
    """
    return copy.deepcopy(profile_payload["resume"])


def resume_client(education: dict, profile: dict, record: dict | None = None):
    routes = {EDUCATION: education, PROFILE: profile}
    if record is not None:
        routes[RESUME] = record
    return make_client(routes)


# ---------------------------------------------------------------------------
# The fixtures have to be what they claim, or nothing below proves anything
# ---------------------------------------------------------------------------


def test_the_profile_fixture_really_carries_a_resume_object(profile_payload):
    resume = profile_payload["resume"]

    assert resume["id"] == RESUME_ID
    assert resume["resource_uri"].endswith(str(RESUME_ID))
    assert set(resume) >= {"title", "uploaded_on", "is_fresh", "conversion_status", "pdf_file"}


def test_the_captured_saved_searches_are_really_empty():
    assert fixture_json("saved_searches.json")["objects"] == []


def test_the_populated_saved_search_fixture_admits_it_is_synthetic():
    """A synthetic fixture that does not say so is a forged capture.

    The repo's convention: ``_capture`` records a real URL and timestamp,
    ``_synthetic`` says hand-built. This one may never wear the first key.
    """
    payload = fixture_json("saved_searches_populated.json")

    assert "_capture" not in payload
    assert "HAND-BUILT, NOT CAPTURED" in payload["_synthetic"]
    assert len(payload["objects"]) == 3


# ---------------------------------------------------------------------------
# resume_info: the id comes from the profile, never from a guess
# ---------------------------------------------------------------------------


def test_the_resume_id_is_taken_from_the_profile_and_the_detail_route_is_read(
    education, profile_payload, resume_record
):
    client = resume_client(education, profile_payload, resume_record)

    result = client.inbound.resume_info()

    assert result["resume_id"] == RESUME_ID
    assert client.routes.count(RESUME) == 1
    assert result["has_resume"] is True


def test_the_resume_collection_route_is_never_requested(
    education, profile_payload, resume_record
):
    """It is HTTP 405. A fallback listing does not exist to be tried."""
    client = resume_client(education, profile_payload, resume_record)

    client.inbound.resume_info()

    assert RESUME_COLLECTION not in client.routes.paths
    for path in client.routes.paths:
        assert not path.endswith("/resume/")


def test_the_id_is_recovered_from_the_resource_uri_when_the_record_omits_it(
    education, profile_payload, resume_record
):
    """``id`` and ``resource_uri`` both name it; losing one is not fatal."""
    profile = copy.deepcopy(profile_payload)
    profile["resume"].pop("id")
    client = resume_client(education, profile, resume_record)

    result = client.inbound.resume_info()

    assert result["resume_id"] == RESUME_ID


def test_a_resume_object_naming_no_id_at_all_is_drift_and_raises(
    education, profile_payload
):
    """Contract drift is loud here for the same reason it is everywhere else in
    this package: the alternative is inventing an id and requesting it."""
    profile = copy.deepcopy(profile_payload)
    profile["resume"] = {"title": "resume.pdf"}
    client = resume_client(education, profile)

    with pytest.raises(ApiError) as excinfo:
        client.inbound.resume_info()

    assert "resume" in excinfo.value.message


# ---------------------------------------------------------------------------
# What the record says, and what it refuses to say
# ---------------------------------------------------------------------------


def test_the_result_carries_title_upload_date_status_and_a_download_url(
    education, profile_payload, resume_record
):
    client = resume_client(education, profile_payload, resume_record)

    result = client.inbound.resume_info()

    assert result["title"] == resume_record["title"]
    assert result["uploaded_on"] == resume_record["uploaded_on"]
    assert result["conversion_status"] == "Converted"
    assert result["download_url"] == resume_record["pdf_file"]


def test_is_fresh_is_reported_as_instahyres_own_verdict_and_says_what_it_costs(
    education, profile_payload, resume_record
):
    """``is_fresh`` is the load-bearing field: staleness is shown to recruiters."""
    client = resume_client(education, profile_payload, resume_record)

    result = client.inbound.resume_info()

    assert result["is_fresh"] is True
    assert "recruiter" in result["freshness_note"].lower()


def test_the_freshness_cutoff_is_reported_as_unpublished_rather_than_invented(
    education, profile_payload, resume_record
):
    """No bundle, page or API field names the number of days that flips the
    flag. A tool that printed one would be reporting a guess as a measurement."""
    client = resume_client(education, profile_payload, resume_record)

    result = client.inbound.resume_info()

    assert "unpublished" in result["freshness_note"].lower()


def test_the_age_in_days_is_derived_from_the_upload_timestamp():
    """Exact arithmetic, at the shaping layer, against an injected clock."""
    raw = {
        "id": 1,
        "title": "resume.pdf",
        "uploaded_on": "2026-08-13T10:59:44+00:00",
        "is_fresh": True,
        "conversion_status": "Converted",
        "url": "https://media.instahyre.com/resume/1/x/resume.pdf",
    }
    now = datetime(2026, 8, 22, 10, 59, 44, tzinfo=timezone.utc)

    assert shape.shape_resume(raw, now=now)["age_days"] == 9
    assert shape.shape_resume(raw, now=now + timedelta(days=331))["age_days"] == 340


def test_an_unparseable_upload_timestamp_costs_the_age_and_nothing_else():
    raw = {"id": 1, "title": "r.pdf", "uploaded_on": "sometime last year", "is_fresh": False}

    shaped = shape.shape_resume(raw, now=datetime(2026, 8, 22, tzinfo=timezone.utc))

    assert shaped["age_days"] is None
    assert shaped["is_fresh"] is False
    assert shaped["uploaded_on"] == "sometime last year"


def test_the_download_url_falls_back_to_the_raw_upload_when_there_is_no_pdf():
    raw = {"id": 1, "title": "r.docx", "url": "https://media.instahyre.com/resume/1/x/r.docx"}

    assert shape.shape_resume(raw)["download_url"] == raw["url"]


def test_the_derived_renderings_are_not_offered_as_the_download():
    """``watermark_file`` and the gzipped ``html_file`` are Instahyre's own
    conversions, not the document he uploaded."""
    raw = {
        "id": 1,
        "title": "r.pdf",
        "watermark_file": "https://media.instahyre.com/resume/1/x/watermark_pdf/r.pdf",
        "html_file": "https://media.instahyre.com/resume/1/x/html/r.html.jgz",
    }

    shaped = shape.shape_resume(raw)

    assert shaped["download_url"] is None
    assert "jgz" not in json.dumps(shaped)


# ---------------------------------------------------------------------------
# No resume on file -- the load-bearing case
# ---------------------------------------------------------------------------


def test_an_account_with_no_resume_gets_a_diagnosis_not_an_exception(
    education, profile_payload
):
    """An account with no resume has no ``resume`` key at all -- absent, not
    null. That must read as a fact about the account, never as a failure."""
    profile = copy.deepcopy(profile_payload)
    profile.pop("resume")
    client = resume_client(education, profile)

    result = client.inbound.resume_info()

    assert result["has_resume"] is False
    assert result["diagnosis"]["reason"] == "no_resume_on_file"
    assert "no resume" in result["diagnosis"]["explanation"].lower()


def test_the_no_resume_result_is_never_an_empty_dict(education, profile_payload):
    """The keys stay put across both cases so a caller branches once, not twice."""
    profile = copy.deepcopy(profile_payload)
    profile.pop("resume")
    client = resume_client(education, profile)

    result = client.inbound.resume_info()

    assert result
    assert set(result) >= {
        "has_resume",
        "resume_id",
        "title",
        "uploaded_on",
        "age_days",
        "is_fresh",
        "conversion_status",
        "download_url",
        "diagnosis",
    }
    assert result["is_fresh"] is None
    assert result["download_url"] is None


def test_no_resume_means_no_request_for_one(education, profile_payload):
    """There is no id, so there is nothing to look up -- and the collection is
    405, so there is nothing to enumerate either."""
    profile = copy.deepcopy(profile_payload)
    profile.pop("resume")
    client = resume_client(education, profile)

    client.inbound.resume_info()

    assert client.routes.count(RESUME) == 0
    assert set(client.routes.paths) == {EDUCATION, PROFILE}


def test_the_diagnosis_says_where_to_fix_it(education, profile_payload):
    profile = copy.deepcopy(profile_payload)
    profile.pop("resume")
    client = resume_client(education, profile)

    result = client.inbound.resume_info()

    assert C.SITE_BASE in result["diagnosis"]["fix"]


def test_a_null_resume_key_is_treated_the_same_as_an_absent_one(
    education, profile_payload
):
    profile = copy.deepcopy(profile_payload)
    profile["resume"] = None
    client = resume_client(education, profile)

    result = client.inbound.resume_info()

    assert result["has_resume"] is False
    assert result["diagnosis"]["reason"] == "no_resume_on_file"


# ---------------------------------------------------------------------------
# Saved searches: the count, the cap, and what an alert actually is
# ---------------------------------------------------------------------------


def test_saved_searches_reports_the_count_and_the_platform_cap():
    client = make_client({SAVED: fixture_json("saved_searches_populated.json")})

    result = client.inbound.saved_searches()

    assert result["count"] == 3
    assert result["max_saved_searches"] == C.MAX_SAVED_SEARCHES == 5


def test_each_saved_search_says_whether_its_alert_is_switched_on():
    """An alert is a boolean on the search, so it is read off the search."""
    client = make_client({SAVED: fixture_json("saved_searches_populated.json")})

    result = client.inbound.saved_searches()

    assert [row["alerts_on"] for row in result["saved_searches"]] == [True, False, True]
    assert result["alerts_on_count"] == 2


def test_a_saved_search_record_is_passed_through_rather_than_renamed():
    """Only ``job_alert_enabled_at`` has evidence behind it. Every other key is
    unmeasured, so the row is forwarded whole instead of reshaped into names
    nobody has seen on a live record."""
    payload = fixture_json("saved_searches_populated.json")
    client = make_client({SAVED: payload})

    result = client.inbound.saved_searches()

    for shaped, raw in zip(result["saved_searches"], payload["objects"]):
        assert set(shaped) >= set(raw)
        for key, value in raw.items():
            assert shaped[key] == value


def test_the_note_carries_the_three_facts_the_bundle_audit_established():
    client = make_client({SAVED: fixture_json("saved_searches.json")})

    note = client.inbound.saved_searches()["note"].lower()

    assert "toggle" in note
    assert "3 filters" in note
    assert "5 saved searches" in note
    assert "frequency" in note


def test_the_note_still_says_instahyre_has_no_saved_jobs():
    """The one true sentence in the old note. It survives the rewrite."""
    client = make_client({SAVED: fixture_json("saved_searches.json")})

    assert "no bookmark or saved-job feature" in client.inbound.saved_searches()["note"]


def test_no_saved_search_is_given_a_frequency_it_does_not_have():
    """There is no schedule field in the product. The note has to SAY so -- an
    agent that is not told will look for one -- while the records themselves
    must not sprout a key that would answer the question falsely."""
    client = make_client({SAVED: fixture_json("saved_searches_populated.json")})

    result = client.inbound.saved_searches()

    for row in result["saved_searches"]:
        assert not [k for k in row if "frequen" in k.lower() or "schedul" in k.lower()]
    assert "no alert frequency exists" in result["note"].lower()
    assert C.SAVED_SEARCH_HAS_FREQUENCY is False


# ---------------------------------------------------------------------------
# Zero saved searches is a fact about the account, not a swallowed failure
# ---------------------------------------------------------------------------


def test_zero_saved_searches_comes_back_with_a_diagnosis():
    client = make_client({SAVED: fixture_json("saved_searches.json")})

    result = client.inbound.saved_searches()

    assert result["saved_searches"] == []
    assert result["count"] == 0
    assert result["diagnosis"]["reason"] == "never_saved_one"
    assert result["diagnosis"]["total_count_reported"] == 0


def test_the_diagnosis_rules_out_the_dead_session_by_naming_what_would_happen():
    client = make_client({SAVED: fixture_json("saved_searches.json")})

    explanation = client.inbound.saved_searches()["diagnosis"]["explanation"]

    assert "AuthRequired" in explanation


def test_a_populated_result_carries_no_diagnosis():
    client = make_client({SAVED: fixture_json("saved_searches_populated.json")})

    assert "diagnosis" not in client.inbound.saved_searches()


def test_a_dead_session_raises_instead_of_returning_zero_saved_searches():
    """The control for the diagnosis above. If a 401 could reach the empty-list
    branch, "he has never saved one" would be a lie the tool tells calmly."""
    client = make_client({SAVED: json_response({"logged_out": True}, status=401)})

    with pytest.raises(AuthRequired):
        client.inbound.saved_searches()


def test_a_payload_without_objects_is_drift_and_not_zero_saved_searches():
    client = make_client({SAVED: {"meta": {"total_count": 0}}})

    with pytest.raises(ApiError) as excinfo:
        client.inbound.saved_searches()

    assert "no 'objects' list" in excinfo.value.message


# ---------------------------------------------------------------------------
# The retired claim, and the tripwire that keeps it retired
# ---------------------------------------------------------------------------

#: Every spelling of the sentence this package shipped before the conversation
#: list was found. Three variants, not one, because the claim was written three
#: different ways in two files and a scanner pinned to a single phrasing would
#: have caught only the copy someone happened to grep for.
RETIRED_INBOX_CLAIMS = (
    "no endpoint lists conversations",
    "no endpoint anywhere enumerates conversations",
    "no endpoint enumerates conversations",
    "threads must be read on",
    "the bodies cannot be read",
)


def string_literals(sources: dict[str, str]):
    """Every string literal in ``sources``, docstrings INCLUDED.

    The write-surface scanners in ``test_inbound_safety.py`` deliberately skip
    docstrings, because a URL cannot be smuggled out through ``__doc__``. A
    false CLAIM can: the sentence this file exists to keep out shipped in a
    docstring and in a returned literal at the same time, and an MCP client
    reads the docstring as the tool's description.

    Comments are invisible to ``ast``, and that exclusion is the right one
    rather than a limitation. ``constants.py`` still quotes the retired claim,
    in a comment, in order to refute it -- that record is what stops the
    correction being re-litigated, and a ``#`` line is never returned to a
    caller.
    """
    for name, text in sorted(sources.items()):
        for node in ast.walk(ast.parse(text, filename=name)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                yield name, node.lineno, node.value


def claim_hits(sources: dict[str, str]):
    return sorted(
        (name, lineno, claim)
        for name, lineno, value in string_literals(sources)
        for claim in RETIRED_INBOX_CLAIMS
        if claim in value.lower()
    )


def package_sources() -> dict[str, str]:
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(PACKAGE_DIR.glob("*.py"))
    }


def test_no_string_the_package_can_return_still_carries_the_retired_claim():
    """The tripwire. Not a unit test -- a regression fence around a falsehood.

    The conversation list is at ``/inbox_page/candidate_conversation`` and this
    package ships three tools that read it. Any string that still tells a
    caller otherwise is wrong at the moment it is written.
    """
    hits = claim_hits(package_sources())

    assert hits == [], "a retired inbox claim is back in a shipped string: %s" % (hits,)


def test_the_tripwire_reports_a_planted_claim():
    """The control. A scanner never shown failing certifies nothing, so it is
    run against a source that says the forbidden thing in both places it once
    said it -- and against a comment, which it must NOT flag."""
    synthetic = {
        "rogue.py": (
            '"""Unread count only: no endpoint lists conversations."""\n'
            'LIMITATION = "threads must be read on the website"\n'
            "# no endpoint lists conversations -- quoted here to refute it\n"
        )
    }

    hits = claim_hits(synthetic)

    assert [(name, claim) for name, _, claim in hits] == [
        ("rogue.py", "no endpoint lists conversations"),
        ("rogue.py", "threads must be read on"),
    ]


def test_unread_messages_points_at_the_conversation_tools_not_at_the_website():
    """``instahyre_inbound_digest`` ships this string on every call."""
    client = make_client({C.EP_MESSAGE_COUNT: fixture_json("message_count.json")})

    result = client.inbound.unread_messages()

    assert result["unread_messages"] == 0
    limitation = result["limitation"]
    assert "instahyre_list_conversations" in limitation
    assert "instahyre_read_conversation" in limitation
    for claim in RETIRED_INBOX_CLAIMS:
        assert claim not in limitation.lower()


def test_the_unread_messages_docstring_names_the_reader_that_replaced_it():
    from instahyre_server.inbound import Inbound

    doc = (Inbound.unread_messages.__doc__ or "").lower()

    assert "conversation" in doc
    for claim in RETIRED_INBOX_CLAIMS:
        assert claim not in doc
