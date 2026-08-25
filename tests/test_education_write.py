"""The education write, held down at the wire.

Education is a filter on a reverse marketplace -- degree, institute and
graduation year are what employers search on -- so this is the same class of
leverage as the job-search profile, and it carries a failure mode neither of
the two writes before it has.

WHAT MAKES THIS RESOURCE DIFFERENT, and why every assertion below is on the
sent body rather than on the returned dict:

1. **The read and the write are DIFFERENT SHAPES.** The GET returns
   ``university`` as an expanded object; the captured request sends a bare
   resource URI. So "echo the read back verbatim" -- the rule that makes the
   skills and jsp writes safe -- would NOT reproduce the measured request here.
   Exactly one transformation is permitted, it is quoted from the site's own
   ``constructEducationObj``, and ``_guard_education_untouched`` is what turns
   "exactly one" from a claim in a docstring into something that stops a write.

2. **Both spellings of the degree ride the same row.** ``degree`` is a URI and
   ``current_degree`` is an expanded object, simultaneously. That looks like
   something to tidy up. Tidying it would be inventing a request nobody has
   sent, so it is pinned here as a property instead.

3. **graduation_year is a STRING on the wire** while ``id`` on the same row is
   an integer. A write that sent 2021 would be a guess; a verify step that
   compared the string it sent against the integer the server may return would
   report every successful year change as a failure. Both directions are
   asserted.

4. **Whether an omitted ROW is deleted is NOT MEASURED**, and unlike the two
   sibling writes there is no way to make the question moot by reading harder:
   the resource that shares this save action does delete omitted rows, while
   education additionally carries its own ``deleted_objects`` channel, which
   argues the other way. Every row therefore rides every write, which is
   correct under both readings -- and that is asserted on the payload, on a
   TWO-row fixture, because a one-row fixture cannot tell "sends every row"
   apart from "sends the edited one".

5. **The removal channel is unmeasured and stays unbuilt.** It is sent empty
   and there is no argument that fills it.

The harness is local rather than imported, on the same reasoning
``test_jsp_write.py`` gives for its own: the routes each file needs to leave
UNWIRED are what turn a stray request into a named failure, and they differ.
Here the profile detail route is deliberately absent -- an education write has
no business reading the candidate record -- so a writer that reached for the
candidate id fails with "Unmocked request" rather than passing quietly.

Nothing here touches the network or the real ``_state/``: conftest builds every
client on an ``httpx.MockTransport``, makes the genuine transports raise, and
redirects ``INSTAHYRE_HOME`` to a tmp dir per test.
"""

from __future__ import annotations

import copy
import json

import pytest

from conftest import fixture_json, make_client
from instahyre_server import constants as C
from instahyre_server.errors import ApiError, InvalidFilter
from instahyre_server.profile_write import WriteRefused, snapshots_dir

# ---------------------------------------------------------------------------
# The captured world
# ---------------------------------------------------------------------------

#: TWO rows, ten keys each. See the fixture's own _note for why it is separate
#: from education.json rather than an edit to it.
EDUCATION = fixture_json("education_write.json")
ROWS = EDUCATION["objects"]
TARGET = ROWS[0]
OTHER = ROWS[1]

TARGET_ID = TARGET["id"]
OTHER_ID = OTHER["id"]

SKILL_PAYLOAD = fixture_json("skill_model.json")
PROFILE = fixture_json("candidate_profile.json")
CANDIDATE_ID = PROFILE["id"]
PROFILE_PATH = C.EP_PROFILE.format(candidate_id=CANDIDATE_ID)

WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")

CSRF_VALUE = "csrf-token-that-must-never-be-echoed-1234567890"

#: The ten keys the wire capture carried, read off the recording rather than
#: restated here -- a hand-copied list would drift from the artefact it claims
#: to describe, which is the failure the register itself exists to prevent.
WIRE = json.loads(
    (
        __import__("pathlib").Path(__file__).parent
        / "fixtures"
        / "write_contracts"
        / "education.json"
    ).read_text(encoding="ascii")
)
WIRE_ROW_KEYS = tuple(WIRE["row_keys"])

assert TARGET_ID != OTHER_ID, "the fixture's two rows must be distinguishable"


# ---------------------------------------------------------------------------
# The fake write surface
# ---------------------------------------------------------------------------


class EducationEndpoint:
    """The education collection: GET returns the rows, PATCH replaces them.

    ``apply_patch=False`` is the silent no-op server -- it answers 200 and
    changes nothing, which is what a broken write looks like from the outside
    and the only reason the read-back exists.

    ``stamp_reads`` makes each GET visibly a different read from the one before
    it, so a body built from a stale read is catchable: without it two reads of
    an unchanging document are indistinguishable and any of them would pass.
    """

    def __init__(self, rows=None, *, apply_patch=True, on_patch=None, after_patch=None,
                 stamp_reads=False, drop_row=None, envelope=True, after_read=None):
        self.rows = copy.deepcopy(ROWS if rows is None else rows)
        self.apply_patch = apply_patch
        self.on_patch = on_patch
        self.after_patch = after_patch
        self.stamp_reads = stamp_reads
        self.drop_row = drop_row
        self.envelope = envelope
        # Fires AFTER the response is built, so a mutation made on read N is
        # visible from read N+1 onwards. That is what lets a test put the world
        # in motion BETWEEN two reads the same call makes.
        self.after_read = after_read
        self.patch_bodies = []
        self.reads = 0

    def __call__(self, request):
        if request.method == "GET":
            self.reads += 1
            if not self.envelope:
                return {"meta": {"total_count": 0}}
            if self.stamp_reads and self.rows:
                self.rows[0]["specialization"] = (
                    "/api/v1/candidate_misc/profile/specializations/%d" % self.reads
                )
            payload = {
                "objects": copy.deepcopy(self.rows),
                "meta": {"total_count": len(self.rows)},
            }
            if self.after_read is not None:
                self.after_read(self, self.reads)
            return payload
        if request.method != "PATCH":
            raise AssertionError(
                "the education collection received an unexpected %s; the contract "
                "captured off the wire is PATCH" % request.method
            )
        body = json.loads(request.content)
        self.patch_bodies.append(body)
        if self.on_patch is not None:
            self.on_patch(request)
        self.stamp_reads = False
        if self.apply_patch:
            self.rows = copy.deepcopy(body["objects"])
            if self.drop_row is not None:
                self.rows = [r for r in self.rows if r.get("id") != self.drop_row]
        if self.after_patch is not None:
            self.after_patch(self)
        return {"objects": copy.deepcopy(self.rows)}


def skills_route(request):
    """The skills collection, which an education write reads for the snapshot."""
    if request.method != "GET":
        raise AssertionError(
            "an education write sent %s to the skills resource; they are separate "
            "writes to separate resources" % request.method
        )
    return SKILL_PAYLOAD


def education_client(rows=None, *, csrf=CSRF_VALUE, **kwargs):
    """A client whose entire education surface is mocked and recorded.

    The taxonomy is left unwired: an education write resolves nothing against
    it, so a request there is an "Unmocked request" AssertionError rather than a
    silent success.
    """
    endpoint = EducationEndpoint(rows, **kwargs)
    routes = {
        C.EP_EDUCATION: endpoint,
        C.EP_SKILL_MODEL: skills_route,
        PROFILE_PATH: lambda request: copy.deepcopy(PROFILE),
    }
    client = make_client(routes, with_taxonomy=False)
    if csrf:
        client.http.cookies.set("csrftoken", csrf, domain="www.instahyre.com")
    client.education = endpoint  # type: ignore[attr-defined]
    return client


# -- wire readers ------------------------------------------------------------


def write_requests(client):
    return [r for r in client.routes.requests if r.method in WRITE_METHODS]


def describe(requests):
    return [(r.method, r.url.path) for r in requests]


def sent_body(client):
    """The one body that reached the wire, and an assertion that it was one."""
    assert len(client.education.patch_bodies) == 1, (
        "expected exactly one PATCH, saw %d" % len(client.education.patch_bodies)
    )
    return client.education.patch_bodies[0]


def row_by_id(objects, row_id):
    for row in objects:
        if row.get("id") == row_id:
            return row
    raise AssertionError("row %r is not in the payload: %s" % (row_id, objects))


# ---------------------------------------------------------------------------
# The confirm gate
# ---------------------------------------------------------------------------


def test_a_preview_sends_nothing_at_all():
    """confirm=False is not "a safer write". It is zero requests to the wire."""
    client = education_client()

    plan = client.profile_writer.update_education(TARGET_ID, graduation_year=2021)

    assert plan["executed"] is False
    assert write_requests(client) == [], describe(client.routes.requests)
    assert client.education.patch_bodies == []
    assert list(snapshots_dir().glob("*.json")) == [], (
        "a preview wrote a snapshot; nothing has happened yet to snapshot"
    )


def test_a_preview_shows_the_exact_request_that_would_be_sent():
    client = education_client()

    plan = client.profile_writer.update_education(TARGET_ID, graduation_year=2021)

    request = plan["would_send"]
    assert request["method"] == "PATCH"
    assert request["url"].endswith(C.EP_EDUCATION)
    assert set(request["json_body"]) == {"objects", "deleted_objects"}
    assert plan["would_change"]["graduation_year"] == {"from": 2019, "to": "2021"}
    assert CSRF_VALUE not in json.dumps(plan), "the token leaked into the preview"


def test_a_confirmed_write_makes_exactly_one_patch_to_the_education_collection():
    client = education_client()

    client.profile_writer.update_education(
        TARGET_ID, graduation_year=2021, confirm=True
    )

    writes = write_requests(client)
    assert len(writes) == 1, describe(client.routes.requests)
    assert writes[0].method == "PATCH"
    assert writes[0].url.path.endswith(C.EP_EDUCATION)


# ---------------------------------------------------------------------------
# The body: every row, every key, the right types
# ---------------------------------------------------------------------------


def test_every_row_the_read_returned_rides_the_write():
    """The property that stands between one edit and a deleted education entry.

    Whether an omitted row is deleted by this resource is NOT measured, and the
    two available readings disagree -- so the payload is built to be correct
    under both. A one-row fixture could not tell this apart from "sends the row
    it edited", which is why the fixture has two.
    """
    client = education_client()

    client.profile_writer.update_education(
        TARGET_ID, graduation_year=2021, confirm=True
    )

    objects = sent_body(client)["objects"]
    assert len(objects) == len(ROWS) == 2
    assert sorted(r["id"] for r in objects) == sorted(r["id"] for r in ROWS)


def test_the_row_that_was_not_edited_rides_unchanged_apart_from_the_collapse():
    client = education_client()

    client.profile_writer.update_education(
        TARGET_ID, graduation_year=2021, confirm=True
    )

    rode = row_by_id(sent_body(client)["objects"], OTHER_ID)
    for key, value in OTHER.items():
        if key == "university":
            continue
        assert rode[key] == value, "%s changed on a row nobody edited" % key
    assert rode["university"] == OTHER["university"]["resource_uri"]


def test_the_body_carries_every_key_the_read_returned_and_no_other():
    client = education_client()

    client.profile_writer.update_education(
        TARGET_ID, graduation_year=2021, confirm=True
    )

    for row in sent_body(client)["objects"]:
        assert set(row) == set(TARGET), "the row's key set drifted from the read"
        assert len(row) == 10


def test_the_row_key_set_is_the_one_the_wire_capture_recorded():
    """The fixture and the recording have to agree, or one of them is fiction."""
    client = education_client()

    client.profile_writer.update_education(
        TARGET_ID, graduation_year=2021, confirm=True
    )

    row = row_by_id(sent_body(client)["objects"], TARGET_ID)
    assert tuple(sorted(row)) == tuple(sorted(WIRE_ROW_KEYS))


def test_graduation_year_is_sent_as_a_string():
    """A STRING, because that is what the browser sent. An int would be a guess."""
    client = education_client()

    client.profile_writer.update_education(
        TARGET_ID, graduation_year=2021, confirm=True
    )

    row = row_by_id(sent_body(client)["objects"], TARGET_ID)
    assert row["graduation_year"] == "2021"
    assert isinstance(row["graduation_year"], str)
    assert row["graduation_year"] != 2021


def test_a_graduation_year_passed_as_a_string_is_sent_the_same_way():
    client = education_client()

    client.profile_writer.update_education(
        TARGET_ID, graduation_year="2021", confirm=True
    )

    row = row_by_id(sent_body(client)["objects"], TARGET_ID)
    assert row["graduation_year"] == "2021"


def test_current_degree_stays_expanded_while_degree_stays_a_uri():
    """Both spellings of the degree ride the same row. Do not normalise them."""
    client = education_client()

    client.profile_writer.update_education(
        TARGET_ID, graduation_year=2021, confirm=True
    )

    row = row_by_id(sent_body(client)["objects"], TARGET_ID)
    assert isinstance(row["current_degree"], dict)
    assert row["current_degree"] == TARGET["current_degree"]
    assert isinstance(row["degree"], str)
    assert row["degree"] == TARGET["degree"]
    assert row["current_degree"]["resource_uri"] == row["degree"]


def test_the_university_is_collapsed_from_the_expanded_object_to_its_uri():
    """The ONE transformation, and the reason a verbatim echo is wrong here."""
    client = education_client()

    client.profile_writer.update_education(
        TARGET_ID, graduation_year=2021, confirm=True
    )

    row = row_by_id(sent_body(client)["objects"], TARGET_ID)
    assert isinstance(TARGET["university"], dict), "the fixture's read must be expanded"
    assert row["university"] == TARGET["university"]["resource_uri"]
    assert isinstance(row["university"], str)


def test_a_university_that_is_already_a_uri_is_passed_through_unchanged():
    rows = copy.deepcopy(ROWS)
    rows[0]["university"] = "/api/v1/candidate_misc/profile/universities/41007"
    client = education_client(rows)

    client.profile_writer.update_education(
        TARGET_ID, graduation_year=2021, confirm=True
    )

    row = row_by_id(sent_body(client)["objects"], TARGET_ID)
    assert row["university"] == "/api/v1/candidate_misc/profile/universities/41007"


def test_a_custom_university_with_no_uri_is_not_nulled():
    """The middle branch of the site's own collapse, which is easy to lose.

    A typed institute reaches the scope as an object with no resource_uri --
    that is what updateCustomUniversity exists to repair afterwards. Collapsing
    it to null here would silently unset the institute on a row this tool was
    never asked to touch.
    """
    rows = copy.deepcopy(ROWS)
    rows[0]["university"] = {"name": "An Institute Typed By Hand"}
    client = education_client(rows)

    client.profile_writer.update_education(
        TARGET_ID, graduation_year=2021, confirm=True
    )

    row = row_by_id(sent_body(client)["objects"], TARGET_ID)
    assert row["university"] == {"name": "An Institute Typed By Hand"}


def test_the_deleted_objects_channel_is_sent_empty():
    """Removal is unmeasured, so the channel is present and never filled."""
    client = education_client()

    client.profile_writer.update_education(
        TARGET_ID, graduation_year=2021, confirm=True
    )

    assert sent_body(client)["deleted_objects"] == []


def test_no_argument_anywhere_can_fill_the_deleted_objects_channel():
    """An unmeasured branch is not a feature waiting for a caller."""
    import inspect

    signature = inspect.signature(
        type(education_client().profile_writer).update_education
    )
    assert set(signature.parameters) == {
        "self",
        "education_id",
        "graduation_year",
        "gpa",
        "grading_scale",
        "confirm",
    }


# ---------------------------------------------------------------------------
# The guards, exercised directly
# ---------------------------------------------------------------------------


def test_a_row_that_omits_a_key_is_refused__CONTROL():
    """The control for the guard everything above depends on.

    Every other test reaches this guard through ``_education_body_row``, which
    builds the row by copying the read -- a path that structurally cannot drop a
    key. Passing through it proves nothing. So the guard is called directly with
    a row that IS missing a key, which is the only way to show it failing.
    """
    writer = education_client().profile_writer
    read = copy.deepcopy(TARGET)
    body = {k: v for k, v in read.items() if k != "grading_scale"}

    with pytest.raises(WriteRefused) as excinfo:
        writer._guard_education_keys(read, body)

    assert "grading_scale" in str(excinfo.value)
    assert excinfo.value.context["fields"] == ["grading_scale"]


def test_a_row_that_adds_a_key_is_refused__CONTROL():
    """The mirror. ``removable`` is the realistic one: the site's own code
    stamps it onto every row after the first, so a body that copied the browser
    too faithfully would carry a key the server never returned."""
    writer = education_client().profile_writer
    read = copy.deepcopy(TARGET)
    body = dict(read, removable=True)

    with pytest.raises(WriteRefused) as excinfo:
        writer._guard_education_keys(read, body)

    assert "removable" in str(excinfo.value)
    assert excinfo.value.context["fields"] == ["removable"]


def test_a_second_transformation_is_refused__CONTROL():
    """Exactly one value may change without being named, and it is university.

    This is what makes the collapse safe to perform at all: without it, "the
    body is the read plus one measured transformation" is a sentence in a
    docstring rather than something that stops a write.
    """
    writer = education_client().profile_writer
    read = copy.deepcopy(TARGET)
    body = dict(read)
    body["university"] = read["university"]["resource_uri"]
    body["current_degree"] = read["current_degree"]["resource_uri"]

    with pytest.raises(WriteRefused) as excinfo:
        writer._guard_education_untouched(read, body, set())

    assert "current_degree" in str(excinfo.value)


def test_a_university_transformed_some_other_way_is_refused__CONTROL():
    writer = education_client().profile_writer
    read = copy.deepcopy(TARGET)
    body = dict(read)
    body["university"] = read["university"]["name"]

    with pytest.raises(WriteRefused) as excinfo:
        writer._guard_education_untouched(read, body, set())

    assert "university" in str(excinfo.value)
    assert excinfo.value.context["fields"] == ["university"]


# ---------------------------------------------------------------------------
# What is refused, by name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field", ["university", "degree", "current_degree", "specialization"]
)
def test_a_related_field_is_refused_by_name_with_its_reason(field):
    """Refused, and the refusal SAYS WHICH FIELD and why -- the way
    plan_job_search_profile refuses its own non-writable fields. A generic
    'unsupported' would send the caller to the website with no idea what to
    change or why the tool would not do it."""
    writer = education_client().profile_writer

    with pytest.raises(WriteRefused) as excinfo:
        writer.plan_education(TARGET_ID, **{field: "anything"})

    message = str(excinfo.value)
    assert field in message
    assert "taxonomy" in message
    assert excinfo.value.context["fields"] == [field]


def test_the_writable_set_and_the_related_set_never_overlap():
    """The edit this refusal is actually vulnerable to, made inert and pinned.

    A CONTROL RUN FOUND THIS. Adding "specialization" to
    EDUCATION_WRITABLE_FIELDS was planted expecting the refusal above to go red,
    and it did not: plan_education checks the related set FIRST, so a taxonomy
    field named in both is still refused and the edit is inert. That is the
    right ordering -- it is the same lesson JSP_WRITABLE_FIELDS records -- but
    inertness that nothing asserts is inertness one reordering away from being
    gone, and the plant proved the sets can disagree without anything noticing.

    So the disagreement itself is what is pinned. A field that is both
    "writable" and "a taxonomy row we refuse to resolve" is a contradiction in
    the register, whichever check happens to win at runtime.
    """
    overlap = sorted(
        set(C.EDUCATION_WRITABLE_FIELDS) & set(C.EDUCATION_RELATED_FIELDS)
    )
    assert overlap == [], (
        "%s is named as writable AND as a related object this server refuses to "
        "resolve. Whichever check fires first, the register now says two "
        "different things about the same field." % overlap
    )
    owned = sorted(set(C.EDUCATION_WRITABLE_FIELDS) & C.EDUCATION_SERVER_OWNED_KEYS)
    assert owned == [], "%s is named as writable AND as server-owned" % owned


@pytest.mark.parametrize("field", ["id", "resource_uri", "candidate"])
def test_a_server_owned_field_is_refused_by_name(field):
    writer = education_client().profile_writer

    with pytest.raises(WriteRefused) as excinfo:
        writer.plan_education(TARGET_ID, **{field: "anything"})

    assert field in str(excinfo.value)
    assert excinfo.value.context["fields"] == [field]


def test_a_write_with_no_fields_at_all_is_refused():
    writer = education_client().profile_writer

    with pytest.raises(InvalidFilter):
        writer.plan_education(TARGET_ID)


def test_an_unknown_education_id_is_refused_and_names_the_real_ones():
    client = education_client()

    with pytest.raises(InvalidFilter) as excinfo:
        client.profile_writer.update_education(999, graduation_year=2021)

    assert str(TARGET_ID) in str(excinfo.value)
    assert str(OTHER_ID) in str(excinfo.value)
    assert write_requests(client) == []


@pytest.mark.parametrize("year", [1979, 1800, 3000])
def test_a_graduation_year_outside_the_platforms_own_list_is_refused(year):
    client = education_client()

    with pytest.raises(InvalidFilter):
        client.profile_writer.update_education(TARGET_ID, graduation_year=year)

    assert write_requests(client) == []


def test_a_graduation_year_that_is_not_a_year_is_refused():
    client = education_client()

    with pytest.raises(InvalidFilter):
        client.profile_writer.update_education(TARGET_ID, graduation_year="last June")


@pytest.mark.parametrize("value", [True, "8.5", -1, 0, 1000])
def test_a_nonsensical_gpa_is_refused(value):
    client = education_client()

    with pytest.raises(InvalidFilter):
        client.profile_writer.update_education(TARGET_ID, gpa=value)

    assert write_requests(client) == []


def test_a_field_the_read_did_not_return_is_refused_rather_than_added():
    """The 8-key world, which is what the shared read fixture still shows.

    If a row arrives without gpa, setting it would mean adding a key the read
    did not return -- and this package's whole position is that such a body has
    never been sent to the platform. Refuse and report, rather than write into
    a world that was not measured.
    """
    rows = copy.deepcopy(ROWS)
    for row in rows:
        row.pop("gpa")
        row.pop("grading_scale")
    client = education_client(rows)

    with pytest.raises(WriteRefused) as excinfo:
        client.profile_writer.update_education(TARGET_ID, gpa=8.5)

    message = str(excinfo.value)
    assert "gpa" in message
    # WHICH refusal fired is the point, not merely that one did. The row-shape
    # guard downstream would also stop this, and its message names gpa too --
    # so an assertion on the name alone cannot tell the two apart, and a plant
    # that removed this check would look defended while reporting the wrong
    # diagnosis. This refusal is about the WORLD ("the row the server returned
    # does not carry it"); the guard's is about the payload.
    assert "does not carry" in message
    assert excinfo.value.context["fields"] == ["gpa"]
    assert write_requests(client) == []


def test_the_same_value_it_already_holds_is_refused_rather_than_written():
    """A no-op write is indistinguishable from a broken one -- and this one
    would still send every education row to achieve nothing."""
    client = education_client()

    with pytest.raises(WriteRefused) as excinfo:
        client.profile_writer.update_education(
            TARGET_ID, graduation_year=2019, confirm=True
        )

    # Named, because there are TWO no-op refusals on this path -- this one, at
    # the preview stage, and a second after the snapshot for the case where the
    # row moved in between. Asserting only "it raised" cannot tell them apart,
    # and this one is the one that refuses BEFORE a snapshot file is written.
    assert "Nothing to write" in str(excinfo.value)
    assert write_requests(client) == []
    assert list(snapshots_dir().glob("*.json")) == []


def test_a_row_that_moved_between_the_preview_and_the_write_is_refused():
    """The second no-op refusal, which covers a different instant.

    The body is built from the snapshot's read, not the preview's, so a row that
    reached the requested value in between makes the write a no-op that the
    first check could not have seen. It refuses rather than sending a payload
    that would replace every row to change nothing.

    The world is moved BETWEEN the two reads one call makes: the planning read
    still shows the old year, and the snapshot's read -- the one the body is
    actually built from -- shows the new one.
    """

    def move_after_the_planning_read(endpoint, read_number):
        if read_number == 1:
            endpoint.rows[0]["graduation_year"] = "2021"

    client = education_client(after_read=move_after_the_planning_read)

    with pytest.raises(WriteRefused) as excinfo:
        client.profile_writer.update_education(
            TARGET_ID, graduation_year=2021, confirm=True
        )

    assert "Between the preview and the write" in str(excinfo.value)
    assert write_requests(client) == []


def test_the_year_already_on_the_profile_is_recognised_across_the_type_change():
    """2019 and "2019" are the same year, and the string is a serialization
    detail rather than a change. Without this the int the server returns could
    never compare equal to the string the wire wants, and asking for the year
    already on file would send a pointless live write."""
    client = education_client()

    plan = client.profile_writer.update_education(TARGET_ID, graduation_year="2019")

    assert plan["would_change"] == {}
    assert plan["already_at_that_value"] == ["graduation_year"]


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


def test_a_confirmed_write_without_a_csrf_token_refuses_before_the_wire():
    client = education_client(csrf=None)

    with pytest.raises(WriteRefused) as excinfo:
        client.profile_writer.update_education(
            TARGET_ID, graduation_year=2021, confirm=True
        )

    assert "CSRF" in str(excinfo.value)
    assert write_requests(client) == [], "the refusal came AFTER the request"
    assert list(snapshots_dir().glob("*.json")) == [], (
        "a snapshot was taken for a write that was then refused"
    )


def test_the_write_sends_the_csrf_header():
    seen = {}
    client = education_client(on_patch=lambda request: seen.update(request.headers))

    client.profile_writer.update_education(
        TARGET_ID, graduation_year=2021, confirm=True
    )

    assert seen.get(C.APPLY_CSRF_HEADER.lower()) == CSRF_VALUE


# ---------------------------------------------------------------------------
# Snapshot and verification
# ---------------------------------------------------------------------------


def test_a_snapshot_carrying_the_education_rows_is_written_before_the_request():
    order = []
    client = education_client(
        on_patch=lambda request: order.append(
            "patch after %d snapshots" % len(list(snapshots_dir().glob("*.json")))
        )
    )

    result = client.profile_writer.update_education(
        TARGET_ID, graduation_year=2021, confirm=True
    )

    assert order == ["patch after 1 snapshots"], "the request went out first"
    record = json.loads(
        (snapshots_dir() / ("%s.json" % result["snapshot_id"])).read_text(
            encoding="utf-8"
        )
    )
    assert [r["id"] for r in record["education"]] == [TARGET_ID, OTHER_ID]
    assert record["education"][0]["graduation_year"] == 2019, (
        "the snapshot holds the PRE-write value or it is not a restore point"
    )


def test_the_body_is_built_from_the_snapshots_read_not_the_previews():
    """The restore point and the payload must describe the same instant.

    ``stamp_reads`` makes every GET visibly different, so a body assembled from
    an earlier read carries a stale marker and this catches it.
    """
    client = education_client(stamp_reads=True)

    result = client.profile_writer.update_education(
        TARGET_ID, graduation_year=2021, confirm=True
    )

    record = json.loads(
        (snapshots_dir() / ("%s.json" % result["snapshot_id"])).read_text(
            encoding="utf-8"
        )
    )
    sent = row_by_id(sent_body(client)["objects"], TARGET_ID)
    assert sent["specialization"] == record["education"][0]["specialization"]


def test_other_write_paths_still_do_not_fetch_the_education_collection():
    """The snapshot takes education rows only when it is handed them.

    A snapshot that always fetched the collection would add a third request to
    every skills write and every jsp write, for a section neither can touch --
    and would break the assertion in test_profile_write.py that the candidate id
    came off the wire exactly once.

    THE COUNTER IS PRIMED FIRST, and that is the whole subtlety of this test.
    The education collection is ALSO where the candidate id is recovered from,
    so a naive "reads == 0" would be measuring the id lookup and would fail
    against a correct implementation. Priming it separates the two: after the id
    is cached, a snapshot that does not want education rows must add nothing.
    """
    client = education_client()
    client.inbound.candidate_id()
    primed = client.education.reads
    assert primed == 1, "the id lookup itself should have read the collection once"

    record, summary = client.profile_writer.take_snapshot(label="plain")

    assert "education" not in record
    assert summary["education_captured"] is False
    assert client.education.reads == primed, (
        "the snapshot fetched the education collection for a write that cannot "
        "touch it"
    )


def test_a_write_that_did_not_take_is_reported_unverified():
    """A 200 is not success. The silent no-op server is what this defends."""
    client = education_client(apply_patch=False)

    result = client.profile_writer.update_education(
        TARGET_ID, graduation_year=2021, confirm=True
    )

    assert result["verified"] is False
    assert "graduation_year" in result["mismatched"]
    assert "DID NOT VERIFY" in result["warning"]


def test_a_write_that_took_is_verified_by_re_reading():
    client = education_client()

    result = client.profile_writer.update_education(
        TARGET_ID, graduation_year=2021, confirm=True
    )

    assert result["executed"] is True
    assert result["verified"] is True
    assert result["updated"] == ["graduation_year"]
    assert C.EP_EDUCATION in result["verified_by"]
    assert client.education.reads >= 2, "the write never re-read anything"


def test_a_year_read_back_as_an_integer_still_verifies():
    """The false alarm that would get a verify step deleted.

    The write sends "2021" and the server may well answer 2021. Comparing raw
    would report every successful year change as unverified.
    """

    def coerce(endpoint):
        for row in endpoint.rows:
            if row["id"] == TARGET_ID:
                row["graduation_year"] = int(row["graduation_year"])

    client = education_client(after_patch=coerce)

    result = client.profile_writer.update_education(
        TARGET_ID, graduation_year=2021, confirm=True
    )

    assert result["verified"] is True
    assert result["mismatched"] is None


def test_the_write_verifies_whether_the_server_echoes_the_uri_or_re_expands_it():
    """Which spelling of university comes back after a PATCH is not measured.

    The payload sends a resource URI; an ordinary GET returns the expanded
    object. Whether the response to the write itself is one or the other has
    never been recorded, so both are made to verify -- otherwise every single
    education write would report the institute as collateral movement on every
    row, and a verify step that cries wolf on every run is a verify step
    somebody deletes.
    """

    def re_expand(endpoint):
        for row in endpoint.rows:
            if isinstance(row["university"], str):
                row["university"] = copy.deepcopy(TARGET["university"])

    client = education_client(after_patch=re_expand)

    result = client.profile_writer.update_education(
        TARGET_ID, graduation_year=2021, confirm=True
    )

    assert result["verified"] is True
    assert result["also_changed_by_the_server"] is None


def test_a_genuinely_different_institute_is_still_reported():
    """The other half of the pair above, and the one that keeps it honest.

    Tolerating the two spellings of the SAME university must not become
    tolerating a different one. If it did, the field employers filter on could
    be silently swapped underneath a write and nothing would say so.
    """

    def swap(endpoint):
        for row in endpoint.rows:
            if row["id"] == TARGET_ID:
                row["university"] = {
                    "resource_uri": "/api/v1/candidate_misc/profile/universities/41007",
                    "id": 41007,
                    "name": "A Different Institute Entirely",
                }
                row["university"]["resource_uri"] = (
                    "/api/v1/candidate_misc/profile/universities/9999999"
                )

    client = education_client(after_patch=swap)

    result = client.profile_writer.update_education(
        TARGET_ID, graduation_year=2021, confirm=True
    )

    assert result["verified"] is False
    assert "university" in result["also_changed_by_the_server"][str(TARGET_ID)]


def test_a_collateral_field_the_server_moved_is_reported():
    """Every row rides this write, so every row is somewhere the server could
    move something nobody named. On this resource that is a FINDING -- unlike
    the jsp, no key here is known to be derived from another."""

    def meddle(endpoint):
        for row in endpoint.rows:
            if row["id"] == OTHER_ID:
                row["specialization"] = "/api/v1/candidate_misc/profile/specializations/99"

    client = education_client(after_patch=meddle)

    result = client.profile_writer.update_education(
        TARGET_ID, graduation_year=2021, confirm=True
    )

    assert result["verified"] is False
    assert str(OTHER_ID) in result["also_changed_by_the_server"]
    assert "specialization" in result["also_changed_by_the_server"][str(OTHER_ID)]
    assert "FINDING" in result["collateral_note"]


def test_a_row_that_vanished_across_the_write_is_reported_as_gone():
    """The measurement nobody has made, if it ever happens: it would mean
    omission-is-deletion is real on this resource. Every row rides the payload
    so it should not, which is exactly why a row going missing must be loud."""
    client = education_client(drop_row=OTHER_ID)

    result = client.profile_writer.update_education(
        TARGET_ID, graduation_year=2021, confirm=True
    )

    assert result["verified"] is False
    assert result["also_changed_by_the_server"][str(OTHER_ID)]["after"] == "GONE"


def test_an_education_collection_with_no_objects_key_stops_the_write():
    client = education_client(envelope=False)

    with pytest.raises(ApiError):
        client.profile_writer.update_education(TARGET_ID, graduation_year=2021)

    assert write_requests(client) == []


def test_an_empty_education_collection_stops_the_write():
    client = education_client([])

    with pytest.raises(ApiError) as excinfo:
        client.profile_writer.update_education(TARGET_ID, graduation_year=2021)

    assert "empty" in str(excinfo.value).lower()
    assert write_requests(client) == []


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


def test_a_snapshot_without_education_rows_is_refused_for_this_scope():
    """A restore that guessed the rows it could not supply would be worse than
    no restore -- the same rule the jsp restore follows."""
    client = education_client()
    client.profile_writer.snapshot(label="skills-only")

    with pytest.raises(WriteRefused) as excinfo:
        client.profile_writer.restore_education(confirm=True)

    assert "education" in str(excinfo.value)
    assert write_requests(client) == []


def test_a_restore_puts_every_row_back_and_verifies():
    client = education_client()
    result = client.profile_writer.update_education(
        TARGET_ID, graduation_year=2021, confirm=True
    )
    assert result["verified"] is True

    restored = client.profile_writer.restore_education(
        result["snapshot_id"], confirm=True
    )

    assert restored["executed"] is True
    assert restored["verified"] is True
    assert client.education.rows[0]["graduation_year"] in (2019, "2019")


def test_a_restore_preview_sends_nothing():
    client = education_client()
    result = client.profile_writer.update_education(
        TARGET_ID, graduation_year=2021, confirm=True
    )
    before = len(client.education.patch_bodies)

    preview = client.profile_writer.restore_education(result["snapshot_id"])

    assert preview["executed"] is False
    assert len(client.education.patch_bodies) == before


def test_a_restore_refuses_when_a_row_has_been_deleted_since_the_snapshot():
    """Sending a row whose id the server no longer has is unmeasured here.

    Skills learned the answer by watching a row disappear; education has not,
    and guessing could take the rows that ARE still there down with it.
    """
    client = education_client()
    result = client.profile_writer.update_education(
        TARGET_ID, graduation_year=2021, confirm=True
    )
    client.education.rows = [r for r in client.education.rows if r["id"] != OTHER_ID]
    before = len(client.education.patch_bodies)

    with pytest.raises(WriteRefused) as excinfo:
        client.profile_writer.restore_education(result["snapshot_id"], confirm=True)

    assert str(OTHER_ID) in str(excinfo.value)
    assert len(client.education.patch_bodies) == before


def test_a_restore_that_would_change_nothing_is_refused():
    client = education_client()
    result = client.profile_writer.update_education(
        TARGET_ID, graduation_year=2021, confirm=True
    )
    client.profile_writer.restore_education(result["snapshot_id"], confirm=True)

    with pytest.raises(WriteRefused):
        client.profile_writer.restore_education(result["snapshot_id"], confirm=True)


def test_the_snapshot_listing_says_which_snapshots_can_answer_this_scope():
    client = education_client()
    client.profile_writer.snapshot(label="skills-only")
    client.profile_writer.update_education(
        TARGET_ID, graduation_year=2021, confirm=True
    )

    listed = {entry["label"]: entry for entry in client.profile_writer.list_snapshots()}

    assert listed["skills-only"]["education_captured"] is False
    assert listed["pre-education-write"]["education_captured"] is True
    assert listed["pre-education-write"]["education_rows"] == 2
