"""The only code in this package that can CHANGE his live profile, held down.

The Instahyre profile is not a document -- it is the surface employers search.
It generates his entire inbound match queue, so a skill silently dropped from
it is not a cosmetic regression: it is a queue that quietly stops arriving,
with no error anywhere to explain why. Two failures matter more than any other
here, and neither one announces itself:

* **a silent no-op** -- the request goes out, the server answers 200, and
  nothing changed; and
* **a blanking write** -- a field or a skill row that existed before the write
  is not there after it.

Every assertion below is therefore written against the WIRE
(``client.routes.requests`` and the recorded PATCH bodies), never against the
return value alone. A test that only read the result would pass unchanged
against an implementation that writes first and reports failure afterwards --
which is precisely the bug that is invisible in review and permanent on a live
profile. This is the same rule ``test_inbound_safety.py`` is built on, applied
to the write surface that came after it.

What is pinned here:

1. **The confirm gate holds.** ``update_skills``, ``update_fields`` and
   ``restore_skills`` issue zero PATCH/POST/DELETE without ``confirm=True``,
   and ``confirm`` defaults to False in every signature.
2. **The write is add-only.** The PATCH body echoes every existing row back
   exactly as the server returned it, and new rows carry nothing but
   ``candidate`` and ``name``. That is the whole safety argument -- the end
   state is identical whether the server reads ``{objects: [...]}`` as a
   replacement set or as an additive tastypie patch -- so both readings are
   implemented in the fake below and the write is asserted under each.
3. **Validation refuses AND allows.** Every limit is shown rejecting a value
   and passing the one next to it; a limit only ever seen refusing could be a
   blanket refusal.
4. **A snapshot exists on disk before the request goes out.** Proven by
   listing the snapshot directory from inside the PATCH route -- at the instant
   the write reaches the wire -- and by rejecting a write and finding the
   restore point still there.
5. **A 200 is not success.** A write whose read-back disagrees reports
   ``verified: False`` with what is missing, rather than a receipt.
6. **A restore can only ever delete what the snapshot does not contain.**

Nothing here touches the network or the real ``_state/``: conftest builds every
client on an ``httpx.MockTransport``, makes the genuine transports raise, and
redirects ``INSTAHYRE_HOME`` to a tmp dir -- so the snapshots these tests write
land in tmp and are asserted to.
"""

from __future__ import annotations

import copy
import inspect
import json
import pathlib

import httpx
import pytest

from conftest import API_PREFIX, FakeClock, fixture_json, json_response, make_client
from instahyre_server import constants as C
from instahyre_server import profile_write as profile_write_module
from instahyre_server import server as server_module
from instahyre_server.errors import InvalidFilter
from instahyre_server.profile_write import (
    JSP_LEVEL_FIELDS,
    WRITABLE_SCALARS,
    ProfileWriter,
    WriteRefused,
    snapshots_dir,
)

# ---------------------------------------------------------------------------
# The captured world
# ---------------------------------------------------------------------------

#: The live shape of ``candidate_skill_model``: four rows, four keys each.
SKILL_PAYLOAD = fixture_json("skill_model.json")
SKILL_ROWS = SKILL_PAYLOAD["objects"]
SKILL_NAMES = [row["name"] for row in SKILL_ROWS]

#: The candidate id is recovered from the education collection (the only
#: collection route that publishes an owner uri), and the profile detail route
#: is keyed by it. Both fixtures are the suite's existing ones, unmodified.
EDUCATION = fixture_json("education.json")
PROFILE = fixture_json("candidate_profile.json")
CANDIDATE_ID = PROFILE["id"]
CANDIDATE_URI = "/api/v1/candidate_misc/profile/candidate/%d" % CANDIDATE_ID

#: The owner named ON the skill rows, which is deliberately NOT the candidate
#: id the education record yields. Keeping the two apart is load-bearing: it is
#: what tells a writer that DERIVES the candidate uri apart from one that
#: copies it off whatever row the server happened to return. See
#: ``test_a_new_row_names_the_candidate_the_writer_derived...`` below, which
#: fails loudly if the two are ever sanitised to the same value.
ROW_OWNER_URI = SKILL_ROWS[0]["candidate"]

#: Read and write share one path on the candidate resource -- ``EP_PROFILE``
#: and ``EP_PROFILE_PATCH`` are the same string -- so one route serves both
#: verbs and the fake branches on the method.
PROFILE_PATH = C.EP_PROFILE.format(candidate_id=CANDIDATE_ID)

WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")

#: Unmistakable if it ever leaks into a preview that a human might paste.
CSRF_VALUE = "csrf-token-that-must-never-be-echoed-1234567890"

#: Exactly at the platform limit, and exactly one over it. Both derived from
#: the constant so a change to the cap moves both together.
AT_LIMIT_SKILL = "S" * C.MAX_SKILL_NAME_CHARS
OVER_LIMIT_SKILL = "S" * (C.MAX_SKILL_NAME_CHARS + 1)

NEW_SKILL = "Express.js"
NEW_TITLE = "Senior Software Engineer"

#: A row present on the profile but absent from the snapshot -- the only kind
#: of row a restore is ever allowed to delete.
EXTRA_ROW = {
    "resource_uri": "/api/v1/candidate_misc/profile/candidate_skill_model/98765432",
    "candidate": ROW_OWNER_URI,
    "id": 98765432,
    "name": "Rust",
}


def synthetic_rows(count):
    """``count`` skill rows in the captured shape, for cap arithmetic."""
    return [
        {
            "resource_uri": "/api/v1/candidate_misc/profile/candidate_skill_model/%d"
            % (37840000 + index),
            "candidate": ROW_OWNER_URI,
            "id": 37840000 + index,
            "name": "Skill %02d" % index,
        }
        for index in range(count)
    ]


# ---------------------------------------------------------------------------
# The fake write surface
# ---------------------------------------------------------------------------


class SkillEndpoint:
    """A stand-in for ``candidate_skill_model`` that records every write.

    ``patch_mode`` is the ambiguity the module under test designs around.
    Instahyre's contract does not say whether ``{objects: [...]}`` REPLACES the
    skill set or ADDS to it, and the module's whole safety argument is that an
    add-only echo makes the two readings produce the same end state. Both are
    implemented here so that claim can be asserted rather than believed.
    """

    def __init__(
        self,
        rows=None,
        *,
        patch_mode="replace",
        patch_status=200,
        apply_patch=True,
        on_patch=None,
    ):
        self.rows = copy.deepcopy(list(SKILL_ROWS if rows is None else rows))
        self.patch_mode = patch_mode
        self.patch_status = patch_status
        self.apply_patch = apply_patch
        self.on_patch = on_patch
        self.patch_bodies = []
        self.deleted_ids = []
        self._next_id = 90000001

    # -- the collection route ---------------------------------------------

    def __call__(self, request):
        if request.method == "GET":
            meta = dict(SKILL_PAYLOAD["meta"], total_count=len(self.rows))
            return {"meta": meta, "objects": copy.deepcopy(self.rows)}
        if request.method == "PATCH":
            body = json.loads(request.content)
            self.patch_bodies.append(body)
            if self.on_patch is not None:
                self.on_patch(request)
            if self.patch_status != 200:
                return json_response(
                    {"objects": ["Rejected by the server."]}, status=self.patch_status
                )
            if self.apply_patch:
                self._apply(body.get("objects") or [])
            return {"objects": copy.deepcopy(self.rows)}
        raise AssertionError(
            "the skills collection received an unexpected %s" % request.method
        )

    # -- one route per row id, so an over-broad delete is a recorded fact --

    def row_deleter(self, row_id):
        def handler(request):
            assert request.method == "DELETE", (
                "row %d was hit with %s, not DELETE" % (row_id, request.method)
            )
            self.deleted_ids.append(row_id)
            self.rows = [row for row in self.rows if row.get("id") != row_id]
            return httpx.Response(204)

        return handler

    # -- the two possible server semantics ---------------------------------

    def _apply(self, objects):
        if self.patch_mode == "replace":
            self.rows = [self._materialise(obj) for obj in objects]
            return
        by_id = {row["id"]: row for row in self.rows}
        for obj in objects:
            row = self._materialise(obj)
            if row["id"] in by_id:
                by_id[row["id"]].update(row)
            else:
                self.rows.append(row)

    def _materialise(self, obj):
        if obj.get("id") is not None:
            return copy.deepcopy(obj)
        new_id = self._next_id
        self._next_id += 1
        row = dict(obj)
        row["id"] = new_id
        row["resource_uri"] = "%s%s/%d" % (API_PREFIX, C.EP_SKILL_MODEL, new_id)
        return row


class ProfileEndpoint:
    """The candidate detail resource: GET reads it, PATCH writes into it."""

    def __init__(self, *, apply_patch=True):
        self.doc = copy.deepcopy(PROFILE)
        self.apply_patch = apply_patch
        self.patch_bodies = []

    def __call__(self, request):
        if request.method == "GET":
            return copy.deepcopy(self.doc)
        if request.method == "PATCH":
            body = json.loads(request.content)
            self.patch_bodies.append(body)
            if self.apply_patch:
                self.doc.update(body)
            return copy.deepcopy(self.doc)
        raise AssertionError(
            "the candidate resource received an unexpected %s" % request.method
        )


def writer_client(
    rows=None,
    *,
    csrf=CSRF_VALUE,
    delete_ids=(),
    profile_applies=True,
    **skill_kwargs,
):
    """A client whose entire profile-write surface is mocked and recorded.

    Taxonomy is deliberately left unwired: a profile write has no business
    resolving a location, so a stray taxonomy read is an "Unmocked request"
    AssertionError rather than a silent success.
    """
    skills = SkillEndpoint(rows, **skill_kwargs)
    profile = ProfileEndpoint(apply_patch=profile_applies)
    routes = {
        C.EP_EDUCATION: EDUCATION,
        C.EP_SKILL_MODEL: skills,
        PROFILE_PATH: profile,
    }
    for row_id in delete_ids:
        routes["%s/%d" % (C.EP_SKILL_MODEL, row_id)] = skills.row_deleter(row_id)

    client = make_client(routes, with_taxonomy=False)
    if csrf:
        client.http.cookies.set("csrftoken", csrf, domain="www.instahyre.com")
    client.skills = skills  # type: ignore[attr-defined]
    client.profile = profile  # type: ignore[attr-defined]
    return client


def restorable_client(**kwargs):
    """A client whose profile carries one row MORE than its only snapshot.

    Every row id is given a delete route, including the four the snapshot
    holds, so a restore that deleted too much would be RECORDED and caught by
    an assertion instead of dying on an unmocked route.
    """
    ids = [row["id"] for row in SKILL_ROWS] + [EXTRA_ROW["id"]]
    client = writer_client(delete_ids=ids, **kwargs)
    client.profile_writer.snapshot(label="baseline")
    client.skills.rows = copy.deepcopy(SKILL_ROWS + [EXTRA_ROW])
    return client


# -- wire readers ------------------------------------------------------------


def write_requests(client):
    """Every recorded request that could change something server-side."""
    return [r for r in client.routes.requests if r.method in WRITE_METHODS]


def describe(requests):
    return [(r.method, r.url.path) for r in requests]


def requests_by_method(client, method):
    return [r for r in client.routes.requests if r.method == method]


def snapshot_files():
    return sorted(snapshots_dir().glob("*.json"))


def snapshot_record(snapshot_id):
    return json.loads(
        (snapshots_dir() / ("%s.json" % snapshot_id)).read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------------------
# The confirm gate
# ---------------------------------------------------------------------------


def test_update_skills_without_confirm_sends_no_write_request_at_all():
    """The load-bearing half of the gate.

    ``executed: False`` in the returned dict proves nothing on its own -- an
    implementation that PATCHed and then set the flag would satisfy it exactly.
    What matters is that the transport recorded no write, and that nothing was
    even snapshotted.
    """
    client = writer_client()

    plan = client.profile_writer.update_skills(add=[NEW_SKILL], confirm=False)

    assert plan["executed"] is False
    assert describe(write_requests(client)) == []
    assert {r.method for r in client.routes.requests} == {"GET"}
    assert snapshot_files() == [], "a preview must not even write a restore point"


def test_update_skills_with_confirm_omitted_entirely_still_writes_nothing():
    """Omission must mean refusal, not "unspecified, so proceed"."""
    client = writer_client()

    plan = client.profile_writer.update_skills([NEW_SKILL])

    assert plan["executed"] is False
    assert describe(write_requests(client)) == []


def test_update_fields_without_confirm_sends_no_write_request_at_all():
    client = writer_client()

    preview = client.profile_writer.update_fields(current_designation=NEW_TITLE)

    assert preview["executed"] is False
    assert preview["would_send"]["method"] == "PATCH"
    assert preview["would_send"]["json_body"] == {"current_designation": NEW_TITLE}
    assert describe(write_requests(client)) == []
    assert {r.method for r in client.routes.requests} == {"GET"}
    assert snapshot_files() == []


def test_restore_skills_without_confirm_sends_no_write_request_at_all():
    client = restorable_client()
    before = snapshot_files()

    result = client.profile_writer.restore_skills(confirm=False)

    assert result["executed"] is False
    assert result["would_restore_to"] == SKILL_NAMES
    assert result["would_drop"] == [EXTRA_ROW["name"]]
    assert result["current_skills"] == SKILL_NAMES + [EXTRA_ROW["name"]]
    assert describe(write_requests(client)) == []
    assert snapshot_files() == before, "a restore preview must not snapshot either"


@pytest.mark.parametrize(
    "method_name", ["update_skills", "update_fields", "restore_skills"]
)
def test_confirm_defaults_to_false_in_every_write_signature(method_name):
    parameters = inspect.signature(getattr(ProfileWriter, method_name)).parameters
    assert parameters["confirm"].default is False
    assert parameters["confirm"].kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.parametrize(
    "tool",
    [
        server_module.instahyre_update_skills,
        server_module.instahyre_update_profile,
        server_module.instahyre_restore_profile,
    ],
)
def test_every_profile_write_tool_defaults_confirm_to_false(tool):
    """The agent-facing surface gets the same default as the method beneath it."""
    assert inspect.signature(tool).parameters["confirm"].default is False


@pytest.mark.parametrize("preview_of", ["skills", "fields"])
def test_a_preview_never_echoes_the_real_csrf_token(preview_of):
    """A preview is shown to a human and may be logged. The header is
    described, never populated."""
    client = writer_client()

    if preview_of == "skills":
        preview = client.profile_writer.update_skills([NEW_SKILL], confirm=False)
    else:
        preview = client.profile_writer.update_fields(current_designation=NEW_TITLE)

    header = preview["would_send"]["headers"][C.APPLY_CSRF_HEADER]
    assert header.startswith("<") and header.endswith(">"), header
    assert CSRF_VALUE not in json.dumps(preview)


# ---------------------------------------------------------------------------
# Add-only: the core safety argument
# ---------------------------------------------------------------------------


def test_the_patch_body_echoes_every_existing_skill_row_byte_for_byte():
    """The property that makes the write safe under BOTH server semantics.

    If ``objects`` is a replacement set, an echo that lost a row would delete
    that skill. If it is an additive patch, the echo is harmless. Only an exact
    echo is correct under both, so it is compared row by row against the
    fixture the server returned -- key order included.
    """
    client = writer_client()

    client.profile_writer.update_skills([NEW_SKILL], confirm=True)

    body = client.skills.patch_bodies[0]
    assert list(body) == ["objects"]
    echoed = body["objects"][: len(SKILL_ROWS)]
    assert echoed == SKILL_ROWS
    for sent, original in zip(echoed, SKILL_ROWS):
        assert list(sent) == list(original), "even the key order must match"
    assert len(body["objects"]) == len(SKILL_ROWS) + 1


def test_the_body_that_goes_out_is_exactly_the_body_the_preview_promised():
    """A human consents to the preview. If the sent body could differ from it,
    the consent was given for something other than what went out."""
    client = writer_client()

    promised = client.profile_writer.update_skills([NEW_SKILL], confirm=False)
    client.profile_writer.update_skills([NEW_SKILL], confirm=True)

    sent = client.skills.patch_bodies[0]
    assert sent == promised["would_send"]["json_body"]
    assert [list(row) for row in sent["objects"]] == [
        list(row) for row in promised["would_send"]["json_body"]["objects"]
    ]


def test_a_new_skill_row_carries_exactly_the_candidate_and_name_keys():
    client = writer_client()

    client.profile_writer.update_skills([NEW_SKILL], confirm=True)

    new_row = client.skills.patch_bodies[0]["objects"][-1]
    assert sorted(new_row) == ["candidate", "name"]
    assert new_row["name"] == NEW_SKILL
    assert new_row["candidate"] == CANDIDATE_URI


def test_a_new_row_names_the_candidate_the_writer_derived_not_one_copied_off_a_row():
    """New rows must name the candidate the server told us we ARE.

    The fixture's rows deliberately carry a different owner uri than the
    education record yields, so an implementation that lifted ``candidate`` off
    whatever row came back would produce a visibly different value here. If the
    two are ever sanitised to the same id this test loses its teeth, so it says
    so out loud first.
    """
    assert ROW_OWNER_URI != CANDIDATE_URI, (
        "skill_model.json and education.json now name the same candidate; this "
        "test can no longer tell a derived uri apart from a copied one"
    )
    client = writer_client()

    plan = client.profile_writer.update_skills([NEW_SKILL], confirm=False)

    new_row = plan["would_send"]["json_body"]["objects"][-1]
    assert new_row["candidate"] == CANDIDATE_URI
    assert new_row["candidate"] != ROW_OWNER_URI
    assert client.routes.count(C.EP_EDUCATION) == 1, "the id came off the wire"


@pytest.mark.parametrize(
    "add",
    [
        [NEW_SKILL],
        ["AWS"],
        ["Kubernetes", "kubernetes"],
        [OVER_LIMIT_SKILL],
        [""],
        ["   "],
        SKILL_NAMES + [NEW_SKILL],
        [NEW_SKILL] + ["Skill %02d" % i for i in range(30)],
        [],
    ],
)
def test_no_existing_skill_is_ever_dropped_whatever_is_asked_for(add):
    """Whatever the request does -- duplicates, rejects, cap overflow, nothing
    at all -- the four rows already on the profile survive it untouched."""
    plan = writer_client().profile_writer.plan_skills(add)

    objects = plan["would_send"]["json_body"]["objects"]
    assert objects[: len(SKILL_ROWS)] == SKILL_ROWS
    assert [row.get("name") for row in objects][: len(SKILL_ROWS)] == SKILL_NAMES
    assert plan["current_skills"] == SKILL_NAMES


def test_a_confirmed_write_makes_exactly_one_patch_and_no_other_write():
    """One action, one request. Not two, not a retry loop, not a fan-out."""
    client = writer_client()

    client.profile_writer.update_skills([NEW_SKILL], confirm=True)

    assert describe(write_requests(client)) == [
        ("PATCH", API_PREFIX + C.EP_SKILL_MODEL)
    ]
    assert len(client.skills.patch_bodies) == 1
    assert client.skills.deleted_ids == []


def test_the_write_carries_the_csrf_token_from_the_cookie():
    client = writer_client()

    client.profile_writer.update_skills([NEW_SKILL], confirm=True)

    patch = requests_by_method(client, "PATCH")[0]
    assert patch.headers.get(C.APPLY_CSRF_HEADER) == CSRF_VALUE
    assert patch.headers.get("Content-Type") == "application/json"


# ---------------------------------------------------------------------------
# Validation: each limit shown refusing, and shown allowing
# ---------------------------------------------------------------------------


def test_a_skill_name_over_the_character_limit_is_rejected_not_truncated():
    """Truncation would write a mangled skill nobody asked for. The name is
    refused whole, and reported, so the caller can fix it."""
    plan = writer_client().profile_writer.plan_skills([OVER_LIMIT_SKILL])

    assert plan["would_add"] == []
    assert plan["rejected"] == [
        {
            "skill": OVER_LIMIT_SKILL,
            "why": "longer than the platform's %d-character limit"
            % C.MAX_SKILL_NAME_CHARS,
        }
    ]
    names = [row.get("name") for row in plan["would_send"]["json_body"]["objects"]]
    assert names == SKILL_NAMES
    assert OVER_LIMIT_SKILL not in names
    assert AT_LIMIT_SKILL not in names, "the name was cut down to fit instead of refused"


def test_a_skill_name_of_exactly_the_character_limit_is_allowed():
    """The allowing half. A limit only ever seen refusing could be refusing
    everything."""
    plan = writer_client().profile_writer.plan_skills([AT_LIMIT_SKILL])

    assert len(AT_LIMIT_SKILL) == C.MAX_SKILL_NAME_CHARS
    assert plan["would_add"] == [AT_LIMIT_SKILL]
    assert plan["rejected"] == []
    assert plan["would_send"]["json_body"]["objects"][-1]["name"] == AT_LIMIT_SKILL


def test_an_empty_or_blank_skill_name_is_rejected_rather_than_written():
    plan = writer_client().profile_writer.plan_skills(["", "   ", NEW_SKILL])

    assert plan["would_add"] == [NEW_SKILL]
    assert [entry["why"] for entry in plan["rejected"]] == ["empty", "empty"]


def test_skills_over_the_platform_cap_are_dropped_and_reported():
    extras = ["Skill %02d" % index for index in range(1, 21)]
    room = C.MAX_SKILLS - len(SKILL_NAMES)

    plan = writer_client().profile_writer.plan_skills(extras)

    assert plan["would_add"] == extras[:room]
    assert plan["dropped_over_platform_cap"] == extras[room:]
    assert plan["resulting_skill_count"] == C.MAX_SKILLS
    assert plan["platform_cap"] == C.MAX_SKILLS


@pytest.mark.parametrize("requested", [1, 15, 16, 17, 40])
def test_the_resulting_skill_count_never_exceeds_the_platform_cap(requested):
    extras = ["Skill %02d" % index for index in range(requested)]

    plan = writer_client().profile_writer.plan_skills(extras)

    assert plan["resulting_skill_count"] <= C.MAX_SKILLS
    assert len(plan["would_send"]["json_body"]["objects"]) <= C.MAX_SKILLS
    assert len(plan["would_add"]) + len(plan["dropped_over_platform_cap"]) == requested


def test_a_profile_already_at_the_cap_refuses_the_write_and_sends_nothing():
    client = writer_client(synthetic_rows(C.MAX_SKILLS))

    with pytest.raises(WriteRefused):
        client.profile_writer.update_skills([NEW_SKILL], confirm=True)

    assert describe(write_requests(client)) == []
    assert snapshot_files() == []


def test_a_skill_already_on_the_profile_is_skipped_as_already_on_profile():
    """Case-insensitively: "rabbitmq" is the skill that is already there."""
    plan = writer_client().profile_writer.plan_skills(["rabbitmq"])

    assert plan["skipped_already_on_profile"] == ["rabbitmq"]
    assert plan["skipped_repeated_in_request"] == []
    assert plan["would_add"] == []


def test_a_skill_listed_twice_in_one_request_is_skipped_as_repeated_in_request():
    plan = writer_client().profile_writer.plan_skills(["Kubernetes", "kubernetes"])

    assert plan["would_add"] == ["Kubernetes"]
    assert plan["skipped_repeated_in_request"] == ["kubernetes"]
    assert plan["skipped_already_on_profile"] == []


def test_the_two_skip_buckets_are_never_conflated():
    """They mean different things and lead to different fixes: one is a no-op,
    the other is a mistake in the caller's own input. Collapsing them into one
    "skipped" list hides the second."""
    plan = writer_client().profile_writer.plan_skills(
        ["RabbitMQ", "Kubernetes", "kubernetes", "Scala"]
    )

    assert plan["skipped_already_on_profile"] == ["RabbitMQ", "Scala"]
    assert plan["skipped_repeated_in_request"] == ["kubernetes"]
    assert plan["would_add"] == ["Kubernetes"]
    assert (
        set(plan["skipped_already_on_profile"]) & set(plan["skipped_repeated_in_request"])
        == set()
    )


def test_an_empty_skill_list_with_confirm_refuses_rather_than_sending_a_no_op():
    """A no-op write is indistinguishable from a broken one, so it is refused
    before it can be mistaken for a success."""
    client = writer_client()

    with pytest.raises(WriteRefused) as excinfo:
        client.profile_writer.update_skills([], confirm=True)

    assert "no-op" in str(excinfo.value).lower()
    assert describe(write_requests(client)) == []
    assert snapshot_files() == []


def test_a_request_of_nothing_but_duplicates_refuses_and_sends_nothing():
    client = writer_client()

    with pytest.raises(WriteRefused):
        client.profile_writer.update_skills(["RabbitMQ", "scala", "RabbitMQ"], confirm=True)

    assert describe(write_requests(client)) == []
    assert snapshot_files() == []


def test_a_confirmed_write_without_a_csrf_token_refuses_before_sending():
    """An unsigned write would 403 ambiguously, and an ambiguous result on a
    profile write is the state this package exists to never be in."""
    client = writer_client(csrf=None)

    with pytest.raises(WriteRefused) as excinfo:
        client.profile_writer.update_skills([NEW_SKILL], confirm=True)

    assert "csrf" in str(excinfo.value).lower()
    assert "instahyre_auth_status" in str(excinfo.value)
    assert describe(write_requests(client)) == []
    assert snapshot_files() == []


def test_a_confirmed_field_write_without_a_csrf_token_refuses_before_sending():
    client = writer_client(csrf=None)

    with pytest.raises(WriteRefused) as excinfo:
        client.profile_writer.update_fields(confirm=True, current_designation=NEW_TITLE)

    assert "csrf" in str(excinfo.value).lower()
    assert describe(write_requests(client)) == []
    assert snapshot_files() == []


# ---------------------------------------------------------------------------
# Snapshots: the restore point exists before the request does
# ---------------------------------------------------------------------------


def test_the_snapshot_is_on_disk_before_the_patch_reaches_the_wire():
    """Ordering asserted at the instant it matters.

    The snapshot directory is listed from INSIDE the PATCH route, so what is
    checked is the state of the disk at the moment the write hit the wire --
    not the state afterwards, which a snapshot written later would also satisfy.
    """
    seen = {}

    def capture(request):
        seen["files"] = sorted(path.name for path in snapshots_dir().glob("*.json"))

    client = writer_client(on_patch=capture)

    result = client.profile_writer.update_skills([NEW_SKILL], confirm=True)

    assert seen["files"] == ["%s.json" % result["snapshot_id"]]


def test_the_snapshot_survives_a_write_the_server_rejects():
    """The restore point has to outlive the request, or it is not one."""
    client = writer_client(patch_status=400)

    with pytest.raises(InvalidFilter):
        client.profile_writer.update_skills([NEW_SKILL], confirm=True)

    written = snapshot_files()
    assert len(written) == 1, [path.name for path in written]
    record = json.loads(written[0].read_text(encoding="utf-8"))
    assert record["label"] == "pre-skills-write"
    assert record["candidate_skills"] == SKILL_ROWS
    assert len(requests_by_method(client, "PATCH")) == 1, "the write really was attempted"


def test_the_snapshot_holds_the_skill_rows_exactly_as_the_server_returned_them():
    """A restore point that reshaped the rows could not restore them."""
    client = writer_client()

    summary = client.profile_writer.snapshot(label="manual")

    record = snapshot_record(summary["snapshot_id"])
    assert record["candidate_skills"] == SKILL_ROWS
    assert record["skill_names"] == SKILL_NAMES
    assert record["label"] == "manual"
    assert summary["skills_captured"] == len(SKILL_ROWS)
    assert summary["skill_names"] == SKILL_NAMES
    assert summary["scalars_captured"] == sorted(WRITABLE_SCALARS)
    assert record["scalars"] == {
        key: PROFILE[key] for key in WRITABLE_SCALARS if key in PROFILE
    }


def test_the_snapshot_carries_no_phone_no_email_and_no_name():
    """A snapshot is a rollback tool. Personal data in a file on disk is a
    liability that buys nothing -- and the profile it is taken FROM carries all
    three, so their absence is a decision, not an accident."""
    assert PROFILE["phone"] and PROFILE["user"]["email"] and PROFILE["user"]["full_name"]
    client = writer_client()

    summary = client.profile_writer.snapshot(label="manual")

    text = pathlib.Path(summary["path"]).read_text(encoding="utf-8")
    record = json.loads(text)
    for key in ("phone", "alternate_phone", "user", "email", "full_name", "profile_image"):
        assert key not in record, "the snapshot carries %s" % key
    for value in (
        PROFILE["phone"],
        PROFILE["user"]["email"],
        PROFILE["user"]["full_name"],
    ):
        assert value not in text, "a personal value reached the snapshot file"


def test_snapshots_land_in_the_redirected_home_and_never_in_the_repo_state_dir(
    isolated_state_home,
):
    """``_state/`` holds his live session. Snapshots written by a test must be
    in tmp, and the assertion is made on the path the writer actually used."""
    client = writer_client()

    summary = client.profile_writer.snapshot(label="manual")

    path = pathlib.Path(summary["path"])
    assert path.exists()
    assert path.parent == isolated_state_home / "profile_snapshots"
    assert isolated_state_home in path.parents

    real_state = (
        pathlib.Path(profile_write_module.__file__).resolve().parent.parent / "_state"
    )
    assert not (real_state / "profile_snapshots" / path.name).exists()


def test_list_snapshots_returns_what_was_written_newest_first(monkeypatch):
    """Ordering is by WHEN, not by name.

    The labels below sort in the exact reverse of the order they were taken in,
    so a listing that fell back to alphabetical would come out backwards.
    """
    clock = FakeClock()
    monkeypatch.setattr(profile_write_module, "time", clock)
    client = writer_client()

    oldest = client.profile_writer.snapshot(label="zzz-oldest")
    clock.tick(5)
    middle = client.profile_writer.snapshot(label="mmm-middle")
    clock.tick(5)
    newest = client.profile_writer.snapshot(label="aaa-newest")

    listed = client.profile_writer.list_snapshots()

    assert [entry["snapshot_id"] for entry in listed] == [
        newest["snapshot_id"],
        middle["snapshot_id"],
        oldest["snapshot_id"],
    ]
    assert [entry["label"] for entry in listed] == [
        "aaa-newest",
        "mmm-middle",
        "zzz-oldest",
    ]
    taken = [entry["taken_at"] for entry in listed]
    assert taken == sorted(taken, reverse=True)
    assert all(entry["skills"] == SKILL_NAMES for entry in listed)


# ---------------------------------------------------------------------------
# A 200 is not success
# ---------------------------------------------------------------------------


def test_a_write_that_does_not_read_back_reports_itself_unverified():
    """The silent no-op, caught.

    The server accepts the PATCH with a 200 and changes nothing -- which is
    exactly what a broken write looks like from the outside. The re-read is the
    only thing that can tell the difference, so the result must say so.
    """
    client = writer_client(apply_patch=False)

    result = client.profile_writer.update_skills([NEW_SKILL], confirm=True)

    assert result["executed"] is True
    assert result["verified"] is False
    assert result["missing_after_write"] == [NEW_SKILL.lower()]
    assert result["unexpected_after_write"] == []
    assert result["skills_now"] == SKILL_NAMES
    assert result["skill_count_now"] == len(SKILL_NAMES)
    assert "DID NOT VERIFY" in result["warning"]
    assert result["snapshot_id"], "the result must name the restore point to use"
    assert len(requests_by_method(client, "PATCH")) == 1


def test_the_unverified_result_never_reads_as_a_receipt():
    """No key on an unverified result may say the change landed."""
    client = writer_client(apply_patch=False)

    result = client.profile_writer.update_skills([NEW_SKILL], confirm=True)

    assert result["verified"] is not True
    assert NEW_SKILL not in result["skills_now"]
    assert "restore" in result["warning"].lower()


@pytest.mark.parametrize("patch_mode", ["replace", "additive"])
def test_a_write_that_reads_back_correctly_reports_verified_true(patch_mode):
    """Run under both possible server semantics.

    This is the module's central claim made falsifiable: an add-only echo lands
    the same end state whether ``objects`` replaces the set or adds to it.
    """
    client = writer_client(patch_mode=patch_mode)

    result = client.profile_writer.update_skills([NEW_SKILL], confirm=True)

    assert result["verified"] is True
    assert result["executed"] is True
    assert result["added"] == [NEW_SKILL]
    assert result["skills_now"] == SKILL_NAMES + [NEW_SKILL]
    assert result["skill_count_now"] == len(SKILL_NAMES) + 1
    assert "warning" not in result
    assert "missing_after_write" not in result


def test_the_verification_is_a_second_read_of_the_skills_resource():
    """The read-back has to be a real request, not the PATCH's own response
    echoed back -- a server that echoes an unsaved payload would "verify"."""
    client = writer_client()

    client.profile_writer.update_skills([NEW_SKILL], confirm=True)

    methods = [r.method for r in client.routes.requests]
    assert methods.index("PATCH") < len(methods) - 1, "nothing was read after the write"
    reads_after = [
        r
        for r in client.routes.requests[methods.index("PATCH") + 1 :]
        if r.method == "GET" and r.url.path == API_PREFIX + C.EP_SKILL_MODEL
    ]
    assert len(reads_after) == 1


# ---------------------------------------------------------------------------
# update_fields: a narrow door, and it says why
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", sorted(JSP_LEVEL_FIELDS))
def test_a_job_search_profile_field_is_refused_by_name_and_sends_nothing(field):
    """These live on a sub-object that has to be PUT back whole. Writing one
    through the sparse candidate PATCH would blank its neighbours, so it is
    refused by name with the reason rather than silently ignored."""
    client = writer_client()

    with pytest.raises(WriteRefused) as excinfo:
        client.profile_writer.update_fields(confirm=True, **{field: 30})

    assert JSP_LEVEL_FIELDS[field] in str(excinfo.value)
    assert excinfo.value.context["fields"] == [field]
    assert client.routes.requests == [], describe(client.routes.requests)


def test_an_unknown_field_is_refused_and_the_message_lists_what_is_writable():
    client = writer_client()

    with pytest.raises(WriteRefused) as excinfo:
        client.profile_writer.update_fields(confirm=True, favourite_colour="blue")

    message = str(excinfo.value)
    assert "favourite_colour" in message
    for writable in WRITABLE_SCALARS:
        assert writable in message, "the refusal does not say what IS writable"
    assert client.routes.requests == []


@pytest.mark.parametrize(
    "field, value",
    [
        ("total_experience", "five"),
        ("total_experience", 5.5),
        ("current_company", 5),
        ("current_designation", ["Engineer"]),
    ],
)
def test_a_wrong_type_is_refused_and_sends_nothing(field, value):
    client = writer_client()

    with pytest.raises(InvalidFilter) as excinfo:
        client.profile_writer.update_fields(confirm=True, **{field: value})

    assert excinfo.value.field == field
    assert client.routes.requests == []


def test_a_boolean_is_not_accepted_where_a_number_is_wanted():
    """``bool`` is a subclass of ``int``, so the ordinary isinstance check
    would wave ``True`` through and write 1 year of experience."""
    client = writer_client()

    with pytest.raises(InvalidFilter) as excinfo:
        client.profile_writer.update_fields(confirm=True, total_experience=True)

    assert "boolean" in str(excinfo.value).lower()
    assert client.routes.requests == []


@pytest.mark.parametrize("fields", [{}, {"current_company": None}])
def test_a_field_write_with_nothing_to_change_refuses_rather_than_patching(fields):
    client = writer_client()

    with pytest.raises(InvalidFilter):
        client.profile_writer.update_fields(confirm=True, **fields)

    assert client.routes.requests == []


def test_a_field_write_patches_exactly_the_sparse_dict_and_nothing_else():
    """The blanking failure, pinned.

    A PATCH that carried the whole profile back would rewrite every field it
    touched, and any field it got wrong would be overwritten with the wrong
    value. Only the changed key may appear in the body.
    """
    client = writer_client()

    result = client.profile_writer.update_fields(confirm=True, current_designation=NEW_TITLE)

    patches = requests_by_method(client, "PATCH")
    assert [r.url.path for r in patches] == [API_PREFIX + PROFILE_PATH]
    body = json.loads(patches[0].content)
    assert body == {"current_designation": NEW_TITLE}
    assert list(body) == ["current_designation"]
    for untouched in ("current_company", "total_experience", "phone", "user", "id"):
        assert untouched not in body, "%s rode along on a sparse PATCH" % untouched
    assert client.profile.patch_bodies == [{"current_designation": NEW_TITLE}]
    assert result["executed"] is True
    assert result["updated"] == ["current_designation"]
    assert result["verified"] is True
    assert result["mismatched"] is None


def test_a_field_write_snapshots_first_and_names_the_snapshot_in_its_result():
    client = writer_client()

    result = client.profile_writer.update_fields(confirm=True, total_experience=6)

    record = snapshot_record(result["snapshot_id"])
    assert record["label"] == "pre-field-write"
    assert record["scalars"]["total_experience"] == PROFILE["total_experience"]
    assert record["candidate_skills"] == SKILL_ROWS


def test_a_field_write_that_does_not_take_reports_itself_unverified():
    """The same silent no-op, on the scalar path."""
    client = writer_client(profile_applies=False)

    result = client.profile_writer.update_fields(confirm=True, current_designation=NEW_TITLE)

    assert result["verified"] is False
    assert result["mismatched"] == {
        "current_designation": {
            "wanted": NEW_TITLE,
            "got": PROFILE["current_designation"],
        }
    }


# ---------------------------------------------------------------------------
# restore_skills: removal, bounded by the snapshot
# ---------------------------------------------------------------------------


def test_a_restore_removes_the_extra_row_using_only_the_patch():
    """Removal happens through the replacement PATCH, and through nothing else.

    This test used to assert a second stage that DELETEd each row the snapshot
    did not contain. Two live measurements retired it: DELETE on this resource
    answers **405, Allow: GET,PATCH** -- so that stage could never have worked --
    and PATCH is a **full replacement set**, so it had nothing left to do. What
    is pinned now is the property that actually holds: one PATCH, no DELETE, and
    the extra row gone.
    """
    client = restorable_client(patch_mode="replace")

    result = client.profile_writer.restore_skills(confirm=True)

    assert requests_by_method(client, "DELETE") == [], (
        "DELETE is 405 on this resource; a restore must never reach for it"
    )
    assert client.skills.deleted_ids == []
    assert result["dropped"] == [EXTRA_ROW["name"]]
    assert result["restored_to"] == SKILL_NAMES
    assert result["skills_now"] == SKILL_NAMES
    assert result["verified"] is True


def test_a_restore_reports_unverified_if_the_server_does_not_replace():
    """The honest failure mode, now that the PATCH is the only mechanism.

    If the resource ever stopped behaving as a replacement set, the extra row
    would survive the restore. There is no second stage to catch that any more,
    so the restore must NOTICE and say so rather than report success.
    """
    client = restorable_client(patch_mode="additive")

    result = client.profile_writer.restore_skills(confirm=True)

    assert result["verified"] is False
    assert EXTRA_ROW["name"] in result["skills_now"]
    assert "did not land exactly" in result["note"]


def test_a_restore_patches_the_snapshot_rows_back_exactly_as_captured():
    client = restorable_client()

    client.profile_writer.restore_skills(confirm=True)

    assert client.skills.patch_bodies == [{"objects": SKILL_ROWS}]


def test_a_restore_snapshots_the_state_it_is_about_to_replace():
    """Restoring is itself a write, so it leaves its own way back."""
    client = restorable_client()
    before = {path.name for path in snapshot_files()}

    client.profile_writer.restore_skills(confirm=True)

    added = [path for path in snapshot_files() if path.name not in before]
    assert len(added) == 1, [path.name for path in snapshot_files()]
    record = json.loads(added[0].read_text(encoding="utf-8"))
    assert record["label"] == "pre-restore"
    assert record["skill_names"] == SKILL_NAMES + [EXTRA_ROW["name"]]


def test_restoring_with_no_snapshot_on_disk_refuses_and_explains_why():
    client = writer_client()
    assert snapshot_files() == []

    with pytest.raises(WriteRefused) as excinfo:
        client.profile_writer.restore_skills(confirm=True)

    assert "no snapshot to restore from" in str(excinfo.value).lower()
    assert client.routes.requests == [], describe(client.routes.requests)


def test_restoring_from_an_unknown_snapshot_id_names_the_tool_that_lists_them():
    client = restorable_client()

    with pytest.raises(WriteRefused) as excinfo:
        client.profile_writer.restore_skills("1700000000-does-not-exist", confirm=True)

    assert "instahyre_list_profile_snapshots" in str(excinfo.value)
    assert describe(write_requests(client)) == []


# ---------------------------------------------------------------------------
# The premise every assertion above rests on
# ---------------------------------------------------------------------------


def test_no_assertion_in_this_file_could_have_reached_the_real_instahyre():
    """Both halves matter: the mock really served these calls, so the recorded
    request list is the true record of what the package tried to send -- and
    the genuine transport really is blocked, so a route this file forgot to
    mock could not have quietly gone out over the wire."""
    client = writer_client()

    client.profile_writer.plan_skills([NEW_SKILL])
    assert client.routes.requests, "the mock transport served the plan's reads"

    with pytest.raises(AssertionError, match="real network"):
        httpx.HTTPTransport().handle_request(
            httpx.Request("PATCH", C.API_BASE + C.EP_SKILL_MODEL)
        )
