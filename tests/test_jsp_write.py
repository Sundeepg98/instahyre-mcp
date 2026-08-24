"""The job-search profile write, held down at the wire.

The jsp is the row employers filter on: notice period, preferred locations,
job-search status. It is written with a **PUT that replaces the whole object**,
which makes the failure here worse than the one the skills write faces. A PATCH
that forgets a field changes nothing. A PUT that forgets a field DELETES it --
silently, with a 200, and with no withdraw anywhere in the product. A profile
that quietly loses its location preferences does not raise anything; it just
stops appearing in location-filtered searches.

So the module under test never asks whether an omitted key survives. It makes
the question unreachable: the body IS the object the server just returned, with
only the named fields replaced, and ``_guard_no_key_dropped`` refuses to send
anything narrower or wider. Everything below exists to prove that arrangement
actually holds, and to prove it against the WIRE -- ``client.jsp.put_bodies``
and ``client.routes.requests`` -- rather than against the returned dict. A test
that read only the result would pass unchanged against an implementation that
writes first and reports the refusal afterwards, which is precisely the bug
that is invisible in review and permanent on a live profile. Same rule as
``test_profile_write.py``, which this file's harness is modelled on.

What is pinned here:

1. **Every key the read returned rides the write.** Not "the important ones" --
   all of them, in the server's own types, including the string-typed decimals
   the browser would have converted to floats on the way past.
2. **The guard is exercised directly.** ``..._omits_a_key_is_refused__CONTROL``
   calls ``_guard_no_key_dropped`` itself rather than reaching it through a
   path that happens to be safe, because a guard only ever seen via a caller
   that cannot violate it is a guard nobody has shown failing.
3. **The write is addressed to the JSP's own id**, which is a different number
   from the candidate id. The wrong door is wired in every client here and
   armed with an assertion, so aiming at the candidate id is a recorded fact
   rather than an unmocked-route puzzle.
4. **The body is built from the snapshot's read**, not from the preview's, so
   the restore point and the payload describe the same instant.
5. **A 200 is not success**, and a full replacement can move fields nobody
   named -- so collateral movement is reported rather than filtered out.
6. **A restore puts the whole object back**, and refuses when the snapshot
   could not supply every key the live object holds.

The harness is local rather than imported from ``test_profile_write.py`` on
purpose: that file's client deliberately leaves both the taxonomy and any PUT
route unwired, so a stray request there is an "Unmocked request" failure. This
one needs the PUT route, and needs the taxonomy on exactly one test, so the
same property is preserved by wiring the taxonomy only where a location is
actually being resolved.

Nothing here touches the network or the real ``_state/``: conftest builds every
client on an ``httpx.MockTransport``, makes the genuine transports raise, and
redirects ``INSTAHYRE_HOME`` to a tmp dir per test.
"""

from __future__ import annotations

import copy
import inspect
import json

from unittest import mock

import pytest

from conftest import API_PREFIX, assert_no_credential, fixture_json, make_client
from instahyre_server import constants as C
from instahyre_server.cache import Store
from instahyre_server.errors import ApiError, InvalidFilter
from instahyre_server.profile_write import ProfileWriter, WriteRefused, snapshots_dir

# ---------------------------------------------------------------------------
# The captured world
# ---------------------------------------------------------------------------

#: The profile detail payload, whose ``jsp`` key holds the object under test --
#: 26 keys, every related object expanded, exactly as the site's own controller
#: binds its form to. The write is a read-modify-write and is only as safe as
#: its read is complete, so the fixture is used whole and never trimmed.
PROFILE = fixture_json("candidate_profile.json")
JSP = PROFILE["jsp"]

CANDIDATE_ID = PROFILE["id"]
JSP_ID = JSP["id"]

PROFILE_PATH = C.EP_PROFILE.format(candidate_id=CANDIDATE_ID)
JSP_PATH = C.EP_JSP.format(jsp_id=JSP_ID)

#: The route a writer that confused the two ids would hit. Wired into every
#: client below and armed, so the confusion is caught as a named failure.
WRONG_DOOR_PATH = C.EP_JSP.format(jsp_id=CANDIDATE_ID)

EDUCATION = fixture_json("education.json")
SKILL_PAYLOAD = fixture_json("skill_model.json")

WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")

#: Unmistakable if it ever leaks into a preview that a human might paste.
CSRF_VALUE = "csrf-token-that-must-never-be-echoed-1234567890"

#: The two ids are different numbers on this account and the write route binds
#: the JSP's. If a future fixture ever sanitises them to the same value, half
#: the assertions in this file quietly stop being able to tell a correct URL
#: from a wrong one -- so it is stated once, out loud, at import time.
assert JSP_ID != CANDIDATE_ID, (
    "candidate_profile.json now gives the jsp the same id as the candidate; "
    "this file can no longer tell the write route apart from the wrong one"
)

#: Distinguishes "the default fixture jsp" from "no jsp at all" in the client
#: constructor, so ``None`` can mean the profile carries no jsp key.
_KEEP = object()


# ---------------------------------------------------------------------------
# The fake write surface
# ---------------------------------------------------------------------------


class ProfileEndpoint:
    """The candidate detail resource. GET only -- the jsp is written elsewhere.

    ``stamp_reads`` makes every GET return a jsp that is VISIBLY a different
    read from the one before it, by numbering ``region_preferences``. That is
    how a body built from a stale read is caught: without it, two reads of the
    same unchanging document are indistinguishable and any of them would pass.
    """

    def __init__(self, jsp=_KEEP, *, stamp_reads=False, events=None):
        self.doc = copy.deepcopy(PROFILE)
        if jsp is not _KEEP:
            if jsp is None:
                self.doc.pop("jsp", None)
            else:
                self.doc["jsp"] = copy.deepcopy(jsp)
        self.stamp_reads = stamp_reads
        self.reads = 0
        self.events = events if events is not None else []

    def __call__(self, request):
        if request.method != "GET":
            raise AssertionError(
                "the candidate resource received an unexpected %s -- the jsp is "
                "written through candidate_skills, never through this one"
                % request.method
            )
        self.reads += 1
        self.events.append(("GET profile", self.reads))
        if self.stamp_reads and isinstance(self.doc.get("jsp"), dict):
            self.doc["jsp"]["region_preferences"] = "read-%d" % self.reads
        return copy.deepcopy(self.doc)


class JspEndpoint:
    """``candidate_skills/{jsp_id}``: the route the site really writes through.

    ``apply_put=False`` is the silent no-op server -- it answers 200 and
    changes nothing, which is what a broken write looks like from the outside
    and the only reason the read-back exists.
    """

    def __init__(self, profile, *, apply_put=True, on_put=None, after_put=None):
        self.profile = profile
        self.apply_put = apply_put
        self.on_put = on_put
        self.after_put = after_put
        self.put_bodies = []

    def __call__(self, request):
        if request.method != "PUT":
            raise AssertionError(
                "the job-search profile received an unexpected %s; the contract "
                "read out of the site's own $resource factory is PUT"
                % request.method
            )
        body = json.loads(request.content)
        self.put_bodies.append(body)
        if self.on_put is not None:
            self.on_put(request)
        # Numbering the reads would otherwise keep moving the document under
        # the verification, which is a different failure than the one any test
        # here is looking for.
        self.profile.stamp_reads = False
        if self.apply_put:
            self.profile.doc["jsp"] = copy.deepcopy(body)
        if self.after_put is not None:
            self.after_put(self.profile)
        return copy.deepcopy(self.profile.doc.get("jsp") or body)


def skills_route(request):
    """The skills collection, which a jsp write reads (for the snapshot) only."""
    if request.method != "GET":
        raise AssertionError(
            "a job-search-profile write sent %s to the skills resource; the two "
            "writes are separate requests to separate resources"
            % request.method
        )
    return SKILL_PAYLOAD


def wrong_door(request):
    raise AssertionError(
        "the write was addressed to candidate_skills/%d -- the CANDIDATE id. "
        "The route binds the JSP's own id (%d); the two are different rows."
        % (CANDIDATE_ID, JSP_ID)
    )


class RecordingStore(Store):
    """An in-memory store that remembers every ``put``, in order.

    Real, not a stub: the cache expiry under test has to actually happen, and a
    stub that only recorded would let a write pass that never expired anything.
    """

    def __init__(self, events=None):
        super().__init__(":memory:")
        self.calls = []
        self.events = events if events is not None else []

    def put(self, namespace, key, value, ttl):
        self.calls.append((namespace, key, value, ttl))
        self.events.append(("store.put", namespace, key, ttl))
        return super().put(namespace, key, value, ttl)


def jsp_client(
    jsp=_KEEP,
    *,
    csrf=CSRF_VALUE,
    apply_put=True,
    on_put=None,
    after_put=None,
    stamp_reads=False,
    with_taxonomy=False,
    store=None,
    events=None,
):
    """A client whose entire jsp surface is mocked and recorded.

    Taxonomy is left unwired by default, so a jsp write that resolved a
    location it was never asked to resolve is an "Unmocked request"
    AssertionError rather than a silent success. Exactly one test asks for it.
    """
    profile = ProfileEndpoint(jsp, stamp_reads=stamp_reads, events=events)
    jsp_route = JspEndpoint(
        profile, apply_put=apply_put, on_put=on_put, after_put=after_put
    )
    routes = {
        C.EP_EDUCATION: EDUCATION,
        C.EP_SKILL_MODEL: skills_route,
        PROFILE_PATH: profile,
        JSP_PATH: jsp_route,
        WRONG_DOOR_PATH: wrong_door,
    }
    client = make_client(routes, with_taxonomy=with_taxonomy, store=store)
    if csrf:
        client.http.cookies.set("csrftoken", csrf, domain="www.instahyre.com")
    client.profile = profile  # type: ignore[attr-defined]
    client.jsp = jsp_route  # type: ignore[attr-defined]
    return client


# -- wire readers ------------------------------------------------------------


def write_requests(client):
    """Every recorded request that could change something server-side."""
    return [r for r in client.routes.requests if r.method in WRITE_METHODS]


def describe(requests):
    return [(r.method, r.url.path) for r in requests]


def snapshot_files():
    return sorted(snapshots_dir().glob("*.json"))


def snapshot_record(snapshot_id):
    return json.loads(
        (snapshots_dir() / ("%s.json" % snapshot_id)).read_text(encoding="utf-8")
    )


def sent_body(client):
    """The one body that reached the wire, and an assertion that it was one."""
    assert len(client.jsp.put_bodies) == 1, (
        "expected exactly one PUT, saw %d" % len(client.jsp.put_bodies)
    )
    return client.jsp.put_bodies[0]


# ---------------------------------------------------------------------------
# Guards and the write path
# ---------------------------------------------------------------------------


def test_the_body_carries_every_key_the_read_returned():
    """The property the whole design rests on, asserted on the payload itself.

    On a full-replacement PUT a key that is absent from the body is deleted,
    not left alone. A body assembled from a field list -- however carefully
    maintained -- would drop whatever the list forgot, and the profile would
    lose it with no error anywhere. Only "the read, with substitutions" is
    correct, so the sent body is compared key for key against the read.
    """
    client = jsp_client()

    client.profile_writer.update_job_search_profile(confirm=True, notice_period=2)

    body = sent_body(client)
    assert set(body) == set(JSP)
    assert len(body) == len(JSP)
    assert list(body) == list(JSP), "even the key order must match the read"
    for key, value in JSP.items():
        if key != "notice_period":
            assert body[key] == value, "%s did not ride the write unchanged" % key
    assert body["notice_period"] == 2


def test_a_body_that_omits_a_key_is_refused__CONTROL():
    """The control for the single guard everything else here depends on.

    Every other test reaches this guard through ``_build_jsp_body``, which
    builds the body by copying the read -- a path that structurally cannot drop
    a key. Passing through it proves nothing about the guard. So the guard is
    called directly with a body that IS missing a key, which is the only way to
    show it failing, and a guard nobody has shown failing certifies nothing.
    """
    writer = jsp_client().profile_writer
    read = copy.deepcopy(JSP)
    body = {k: v for k, v in read.items() if k != "location_preferences"}

    with pytest.raises(WriteRefused) as excinfo:
        writer._guard_no_key_dropped(read, body)

    assert "location_preferences" in str(excinfo.value)
    assert "DELETED" in str(excinfo.value), "the refusal must say what omission costs"
    assert excinfo.value.context["fields"] == ["location_preferences"]

    # And the allowing half: a complete body passes the same guard, so the
    # refusal above is a refusal of THIS body rather than of every body.
    writer._guard_no_key_dropped(read, dict(read))


def test_a_body_with_an_extra_key_is_refused():
    """A key the read did not return means the payload is not the server's object.

    This is not hypothetical: if the platform stops returning a field, the
    writable-field list still names it, and a write of that field would invent
    a key the server has never been sent. The site echoes the object back
    exactly as it received it, so a wider body is one no browser could produce.
    """
    without_job_type = {k: v for k, v in JSP.items() if k != "job_type"}
    client = jsp_client(without_job_type)

    with pytest.raises(WriteRefused) as excinfo:
        client.profile_writer.update_job_search_profile(confirm=True, job_type=2)

    assert "job_type" in str(excinfo.value)
    assert excinfo.value.context["fields"] == ["job_type"]
    assert client.jsp.put_bodies == []
    assert describe(write_requests(client)) == []
    assert snapshot_files() == [], "a body that never went out needs no restore point"


def test_a_changed_server_owned_key_is_refused():
    """Server-owned keys ride the write; a caller may never move one.

    They cannot be reached through the public path -- none of them is in
    JSP_WRITABLE_FIELDS -- so the guard is called directly. The hazard it
    covers is a body assembled from somewhere other than the read: a supplied
    ``is_immediate_joinee`` would be either ignored or believed, and nothing
    visible from here distinguishes the two.
    """
    writer = jsp_client().profile_writer
    read = copy.deepcopy(JSP)
    assert C.JSP_SERVER_OWNED_KEYS & set(read), "the read carries no server-owned key"
    body = dict(read)
    body["is_immediate_joinee"] = not read["is_immediate_joinee"]

    with pytest.raises(WriteRefused) as excinfo:
        writer._guard_server_owned(read, body)

    assert "is_immediate_joinee" in str(excinfo.value)
    assert excinfo.value.context["fields"] == ["is_immediate_joinee"]

    # The allowing half: an untouched server-owned key rides through, which is
    # what makes this a guard against CHANGING one rather than against sending
    # it at all -- and sending it is mandatory, since it is a key of the read.
    writer._guard_server_owned(read, dict(read))


def test_confirm_false_sends_nothing():
    """``executed: False`` proves nothing on its own.

    An implementation that PUT and then set the flag would satisfy the returned
    dict exactly. What matters is that the transport recorded no write and that
    nothing was even snapshotted -- and that omitting the argument entirely
    means refusal rather than "unspecified, so proceed".
    """
    parameters = inspect.signature(ProfileWriter.update_job_search_profile).parameters
    assert parameters["confirm"].default is False
    assert parameters["confirm"].kind is inspect.Parameter.KEYWORD_ONLY

    client = jsp_client()

    plan = client.profile_writer.update_job_search_profile(notice_period=2)

    assert plan["executed"] is False
    assert plan["would_change"]["notice_period"] == {"from": JSP["notice_period"], "to": 2}
    assert describe(write_requests(client)) == []
    assert {r.method for r in client.routes.requests} == {"GET"}
    assert client.jsp.put_bodies == []
    assert snapshot_files() == [], "a preview must not even write a restore point"


def test_a_confirmed_write_makes_exactly_one_put_to_the_jsp_endpoint():
    """One action, one request. Not two, not a retry loop, not a fan-out.

    The site fires a second request on its skills path; this server does not,
    and a write that quietly grew one back would be touching fields nobody
    named. The skills resource is read for the snapshot and never written.
    """
    client = jsp_client()

    client.profile_writer.update_job_search_profile(confirm=True, notice_period=2)

    assert describe(write_requests(client)) == [("PUT", API_PREFIX + JSP_PATH)]
    assert len(client.jsp.put_bodies) == 1
    put = write_requests(client)[0]
    assert put.headers.get(C.APPLY_CSRF_HEADER) == CSRF_VALUE
    assert put.headers.get("Content-Type") == "application/json"


def test_the_put_url_uses_the_jsp_id_not_the_candidate_id():
    """Two routes reach one object, and the obvious one is the wrong one.

    The jsp's own ``resource_uri`` says ``candidate_jsp/{id}``; the site writes
    through ``candidate_skills/{id}``, bound to the JSP's own id. Guessing from
    either the resource_uri or the candidate id addresses a different row. The
    candidate-id spelling is wired into this client and raises if it is hit.
    """
    client = jsp_client()

    plan = client.profile_writer.plan_job_search_profile(notice_period=2)
    client.profile_writer.update_job_search_profile(confirm=True, notice_period=2)

    assert plan["jsp_id"] == JSP_ID
    assert plan["would_send"]["url"] == C.API_BASE + JSP_PATH
    put = write_requests(client)[0]
    assert put.url.path == API_PREFIX + JSP_PATH
    assert put.url.path.endswith("/%d" % JSP_ID)
    assert str(CANDIDATE_ID) not in put.url.path
    assert C.JSP_SELF_URI_PREFIX not in put.url.path, (
        "the write followed the object's own resource_uri, which the site never does"
    )


def test_the_write_refuses_without_a_csrf_token():
    """An unsigned write would 403 ambiguously.

    Django rejects it, the result is neither a success nor a clean failure, and
    an ambiguous outcome on a full-replacement write is the state this package
    exists never to be in. The refusal names the tool that fixes it.
    """
    client = jsp_client(csrf=None)

    with pytest.raises(WriteRefused) as excinfo:
        client.profile_writer.update_job_search_profile(confirm=True, notice_period=2)

    assert "csrf" in str(excinfo.value).lower()
    assert "instahyre_auth_status" in str(excinfo.value)
    assert describe(write_requests(client)) == []
    assert snapshot_files() == []


def test_the_csrf_value_is_never_echoed_in_a_preview():
    """A preview is shown to a human and may be pasted into a log or a ticket.

    The header is DESCRIBED, never populated, so the consent artifact carries
    no credential. Checked with the shared walker as well as a substring, since
    a credential rarely escapes verbatim -- it escapes through whatever encoded
    it, and a plaintext search reports clean on every one of those spellings.
    """
    client = jsp_client()

    preview = client.profile_writer.update_job_search_profile(notice_period=2)

    header = preview["would_send"]["headers"][C.APPLY_CSRF_HEADER]
    assert header.startswith("<") and header.endswith(">"), header
    assert CSRF_VALUE not in json.dumps(preview)
    assert_no_credential(preview, CSRF_VALUE, where="the jsp preview")


def test_a_snapshot_is_written_before_the_put():
    """Ordering asserted at the instant it matters.

    The snapshot directory is listed from INSIDE the PUT route, so what is
    checked is the state of the disk at the moment the write hit the wire --
    not afterwards, which a snapshot written later would also satisfy. A
    restore point that arrives after the request does not survive the process
    dying mid-write, which is the only case it exists for.
    """
    seen = {}

    def capture(request):
        seen["files"] = sorted(path.name for path in snapshots_dir().glob("*.json"))

    client = jsp_client(on_put=capture)

    result = client.profile_writer.update_job_search_profile(
        confirm=True, notice_period=2
    )

    assert seen["files"] == ["%s.json" % result["snapshot_id"]]
    record = snapshot_record(result["snapshot_id"])
    assert record["label"] == "pre-jsp-write"
    assert record["job_search_profile"] == JSP, (
        "the restore point does not hold the object the write replaced"
    )


def test_the_body_is_built_from_the_snapshots_read_not_an_earlier_one():
    """The window between the restore point and the payload, closed.

    A preview reads the object; the snapshot reads it again. If the body were
    built from the FIRST read, the restore point and the payload would describe
    two different instants, and anything that moved in between would be
    silently overwritten with a value nobody saw. Here every GET returns a
    visibly different document, so the body names which read it came from.
    """
    seen = {}

    def capture(request):
        seen["reads_at_put"] = client.profile.reads

    client = jsp_client(stamp_reads=True, on_put=capture)

    result = client.profile_writer.update_job_search_profile(
        confirm=True, notice_period=2
    )

    body = sent_body(client)
    assert seen["reads_at_put"] >= 2, "there was only one read; this test has no teeth"
    assert body["region_preferences"] == "read-%d" % seen["reads_at_put"]
    assert body["region_preferences"] != "read-1", "the body came from the stale read"
    record = snapshot_record(result["snapshot_id"])
    assert record["job_search_profile"]["region_preferences"] == (
        body["region_preferences"]
    ), "the snapshot and the payload describe different instants"


def test_untouched_keys_go_back_in_the_servers_own_types():
    """The type of an untouched field is the server's business, not the browser's.

    The site parseFloats ``current_salary`` on load, so its PUT carries a
    number where its own GET carried the string "0.0". This server does not
    reproduce that: a field nobody named goes back exactly as it arrived. Only
    a CHANGED value goes out as a float, which is where matching the browser
    actually matters.
    """
    assert isinstance(JSP["current_salary"], str) and JSP["current_salary"] == "0.0", (
        "the fixture no longer returns a string-typed decimal; this test is moot"
    )

    untouched = jsp_client()
    untouched.profile_writer.update_job_search_profile(confirm=True, notice_period=2)
    body = sent_body(untouched)
    assert body["current_salary"] == "0.0"
    assert isinstance(body["current_salary"], str), (
        "an untouched decimal was retyped on the way out"
    )
    assert isinstance(body["fresher_salary"], str)

    touched = jsp_client()
    touched.profile_writer.update_job_search_profile(confirm=True, current_salary=18)
    written = sent_body(touched)
    assert written["current_salary"] == 18.0
    assert isinstance(written["current_salary"], float)
    assert not isinstance(written["current_salary"], str)
    assert isinstance(written["fresher_salary"], str), (
        "fresher_salary is not the field that was written and must not be retyped"
    )


def test_the_career_break_nulling_the_browser_does_is_not_reproduced():
    """A browser behaviour this server declined, proven declined.

    The site's ``saveCareerBreakFields`` NULLs ``career_break_start_date`` and
    ``career_break_reason`` on its way past whenever career stage is not
    "career break". Reproducing it would mean one tool call blanking two
    fields it never named -- data the caller cannot get back from anywhere but
    a snapshot. The deviation is in the safe direction and is recorded rather
    than hidden, so it is pinned here rather than left to the docstring.
    """
    reason = "Caring for a family member"
    with_break = dict(
        JSP,
        career_break_reason=reason,
        career_break_start_date="2024-01-01",
        career_stage=1,
        career_stage_value=1,
    )
    client = jsp_client(with_break)

    client.profile_writer.update_job_search_profile(confirm=True, notice_period=2)

    body = sent_body(client)
    assert body["career_stage_value"] != 2, "the browser would not have nulled anything"
    assert body["career_break_reason"] == reason
    assert body["career_break_start_date"] == "2024-01-01"


def test_a_no_op_write_is_refused():
    """A write that cannot change anything is indistinguishable from a broken one.

    And on this endpoint it is not free either: it would replace the whole
    object to achieve nothing, which is a real request with a real chance of
    landing wrong. Refused before it can be mistaken for a success.
    """
    client = jsp_client()

    with pytest.raises(WriteRefused) as excinfo:
        client.profile_writer.update_job_search_profile(
            confirm=True, notice_period=JSP["notice_period"]
        )

    assert "no-op" in str(excinfo.value).lower()
    assert describe(write_requests(client)) == []
    assert client.jsp.put_bodies == []
    assert snapshot_files() == []


# ---------------------------------------------------------------------------
# Validation: each limit shown refusing, and shown allowing
# ---------------------------------------------------------------------------


def test_notice_period_rejects_a_day_count():
    """The field is an INDEX into five bands, and the two readings coincide at 0.

    An earlier build published it as ``notice_period_days``. That label was
    wrong invisibly, because this account sits at 0 where both readings print
    the same thing. A caller who believes the old label passes 30 for "a
    month"; 30 is not a band, and the refusal has to say why rather than
    clamping it to something that looks plausible.
    """
    client = jsp_client()

    with pytest.raises(InvalidFilter) as excinfo:
        client.profile_writer.update_job_search_profile(confirm=True, notice_period=30)

    message = str(excinfo.value)
    assert excinfo.value.field == "notice_period"
    assert "index" in message.lower() and "day count" in message.lower()
    assert C.NOTICE_PERIOD_RANGES[3] in message, "the refusal does not show the bands"
    assert describe(write_requests(client)) == []


def test_notice_period_rejects_a_bool():
    """``bool`` is a subclass of ``int``, and ``True == 1`` is a real band.

    So an ordinary membership check waves ``True`` through and writes
    "15 days or less" -- a plausible value nobody asked for, on the field
    employers filter by. The type check has to run before the range check.
    """
    client = jsp_client()

    with pytest.raises(InvalidFilter) as excinfo:
        client.profile_writer.update_job_search_profile(confirm=True, notice_period=True)

    assert excinfo.value.field == "notice_period"
    assert True in C.NOTICE_PERIOD_RANGES, "the trap this test guards no longer exists"
    assert "INDEX" in str(excinfo.value)
    assert describe(write_requests(client)) == []


def test_notice_period_accepts_every_published_band():
    """The allowing half. A limit only ever seen refusing could be refusing all.

    Every index the platform's own bundle publishes is planned end to end, and
    the plan is asserted to carry it -- including the one this account already
    sits at, which reports as already-at-that-value rather than as a change.
    """
    assert max(C.NOTICE_PERIOD_RANGES) == C.MAX_NOTICE_PERIOD_INDEX

    for index in sorted(C.NOTICE_PERIOD_RANGES):
        plan = jsp_client().profile_writer.plan_job_search_profile(notice_period=index)

        body = plan["would_send"]["json_body"]
        assert body["notice_period"] == index, index
        assert set(body) == set(JSP), index
        assert plan["would_read_as"]["notice_period"]["means"] == (
            C.NOTICE_PERIOD_RANGES[index]
        )
        if index == JSP["notice_period"]:
            assert plan["already_at_that_value"] == ["notice_period"]
        else:
            assert plan["would_change"]["notice_period"] == {
                "from": JSP["notice_period"],
                "to": index,
            }


def test_status_rejects_an_unknown_code():
    """Job-search status decides whether he is surfaced to employers at all.

    Three codes ship. A fourth is not a value the platform has any handling
    for, and writing one would set the field employers filter on to something
    the site itself cannot render -- so it is refused with the real codes
    spelled out, and the codes that DO ship are shown passing.
    """
    client = jsp_client()

    with pytest.raises(InvalidFilter) as excinfo:
        client.profile_writer.update_job_search_profile(confirm=True, status=7)

    message = str(excinfo.value)
    assert excinfo.value.field == "status"
    for code, name in C.JOB_SEARCH_STATUS.items():
        assert "%d=%s" % (code, name) in message, "the refusal hides a valid code"
    assert describe(write_requests(client)) == []

    for code in sorted(C.JOB_SEARCH_STATUS):
        plan = jsp_client().profile_writer.plan_job_search_profile(status=code)
        assert plan["would_send"]["json_body"]["status"] == code


def test_job_type_rejects_an_unknown_code():
    """Same shape as status, on the field that decides internship vs full time."""
    client = jsp_client()

    with pytest.raises(InvalidFilter) as excinfo:
        client.profile_writer.update_job_search_profile(confirm=True, job_type=9)

    assert excinfo.value.field == "job_type"
    for code, name in C.JOB_TYPE_NAMES.items():
        assert "%d=%s" % (code, name) in str(excinfo.value)
    assert describe(write_requests(client)) == []

    for code in sorted(C.JOB_TYPE_NAMES):
        plan = jsp_client().profile_writer.plan_job_search_profile(job_type=code)
        assert plan["would_send"]["json_body"]["job_type"] == code


def test_salary_out_of_range_is_refused():
    """The units trap: the field is LAKHS, and the page renders it times 100000.

    A caller who passes rupees writes 1800000 lakhs -- a number the platform's
    own form cannot produce and that would read as an absurd figure to every
    employer who saw it. The bounds are the platform's own, so both ends are
    shown refusing and both ends are shown accepting.
    """
    client = jsp_client()

    with pytest.raises(InvalidFilter) as excinfo:
        client.profile_writer.update_job_search_profile(
            confirm=True, current_salary=1800000
        )

    message = str(excinfo.value)
    assert excinfo.value.field == "current_salary"
    assert str(C.MAX_SALARY_LAKHS) in message and "lakhs" in message.lower()
    assert describe(write_requests(client)) == []

    with pytest.raises(InvalidFilter):
        jsp_client().profile_writer.plan_job_search_profile(
            current_salary=C.MIN_SALARY_LAKHS - 1
        )

    # The allowing half, at both ends of the published range.
    for accepted in (C.MIN_SALARY_LAKHS + 1, 18, C.MAX_SALARY_LAKHS):
        plan = jsp_client().profile_writer.plan_job_search_profile(
            current_salary=accepted
        )
        assert plan["would_send"]["json_body"]["current_salary"] == float(accepted)


def test_salary_rejects_a_bool():
    """``True`` sits inside the range and would be written as 1.0 LPA.

    Nothing about that value looks wrong afterwards -- it is a number the form
    accepts -- so the only place it can be caught is here, before the object is
    replaced with it.
    """
    client = jsp_client()

    with pytest.raises(InvalidFilter) as excinfo:
        client.profile_writer.update_job_search_profile(confirm=True, current_salary=True)

    assert excinfo.value.field == "current_salary"
    assert C.MIN_SALARY_LAKHS <= True <= C.MAX_SALARY_LAKHS, (
        "True no longer sits inside the range; this test has no teeth"
    )
    assert describe(write_requests(client)) == []


def test_an_empty_location_list_is_refused():
    """An empty list is not an open profile, it is one that stops matching.

    Instahyre is a reverse marketplace and location is a filter employers
    search on, so clearing the list drops him out of every location-filtered
    result set -- the same class of consequence as an empty skill list, and
    refused outright rather than confirm-gated for the same reason.
    """
    client = jsp_client()

    with pytest.raises(WriteRefused) as excinfo:
        client.profile_writer.update_job_search_profile(
            confirm=True, location_preferences=[]
        )

    message = str(excinfo.value)
    assert "location" in message.lower()
    assert C.SITE_BASE in message, "the refusal does not say where it CAN be done"
    assert excinfo.value.context["fields"] == ["location_preferences"]
    assert describe(write_requests(client)) == []
    assert snapshot_files() == []


def test_a_bare_string_location_is_refused():
    """A string is iterable, which is exactly the problem.

    ``"Bangalore"`` would be sent as a list of nine single characters, none of
    which is a location the platform knows, replacing the whole preference
    list with nonsense. The refusal has to fire on the TYPE rather than on the
    resolver, because the resolver would just report nine unknown locations.
    """
    client = jsp_client()

    with pytest.raises(InvalidFilter) as excinfo:
        client.profile_writer.update_job_search_profile(
            confirm=True, location_preferences="Bangalore"
        )

    assert excinfo.value.field == "location_preferences"
    assert "LIST" in str(excinfo.value)
    assert describe(write_requests(client)) == []


def test_locations_are_resolved_through_the_platform_taxonomy():
    """Instahyre's location matching is case-sensitive; "bangalore" is a 400.

    A profile write cannot afford a wire error and cannot afford a token the
    platform will not match on either, so user text goes through the same
    resolver every search filter uses and the payload carries the taxonomy's
    exact spelling. The 308-token list is fetched at most once per TTL, which
    is why the writer shares the caller's store.
    """
    client = jsp_client(with_taxonomy=True)

    client.profile_writer.update_job_search_profile(
        confirm=True, location_preferences=["  bangalore  ", "WORK FROM HOME", "pune"]
    )

    body = sent_body(client)
    assert body["location_preferences"] == ["Bangalore", "Work From Home", "Pune"]
    assert client.routes.count(C.EP_LOCATION_DATA) == 1, (
        "the location list was re-fetched instead of being served from the store"
    )


def test_an_unwritable_jsp_field_is_refused_by_name():
    """Refused by NAME, with the reason, and before any request goes out.

    Each exclusion has a recorded cause: career stage cascades into four other
    fields, is_salary_hidden is gated behind a threshold this account does not
    meet, is_immediate_joinee has no write site in any shipped bundle, and the
    related objects are sent expanded rather than as ids. A silent ignore would
    look identical to a write that worked.
    """
    client = jsp_client()

    for field in ("career_stage", "is_salary_hidden", "job_function", "languages"):
        assert field not in C.JSP_WRITABLE_FIELDS
        with pytest.raises(WriteRefused) as excinfo:
            client.profile_writer.update_job_search_profile(confirm=True, **{field: 1})

        message = str(excinfo.value)
        assert field in message
        assert excinfo.value.context["fields"] == [field]
        for writable in C.JSP_WRITABLE_FIELDS:
            assert writable in message, "the refusal does not say what IS writable"

    assert client.routes.requests == [], (
        "an unwritable field was refused only after reading the profile"
    )


def test_no_server_owned_key_is_also_listed_writable():
    """The two lists must not overlap, and this is the only thing that says so.

    ADDED BECAUSE A RED CONTROL WENT GREEN. ``jsp_write_controls.py`` plants a
    server-owned key into ``JSP_WRITABLE_FIELDS`` and requires a test to notice.
    Nothing did: the by-name refusal test loops over four hard-coded exclusions,
    none of them server-owned, and its only list-wide assertion checks that
    every writable name appears in a message BUILT by joining that same list --
    self-satisfying by construction, so no addition to the tuple could ever red
    it. A check that cannot fail certifies nothing, which is worse than no check
    at all because it reads like one.
    """
    overlap = set(C.JSP_WRITABLE_FIELDS) & C.JSP_SERVER_OWNED_KEYS
    assert not overlap, (
        "these keys are listed as writable AND as server-owned, so the server "
        "would be asked to accept a value it derives: %s" % sorted(overlap)
    )


def test_a_server_owned_key_smuggled_into_the_writable_list_is_still_refused():
    """Belt to the previous test's braces, and a different failure mode.

    The disjointness assertion catches the edit. This catches the consequence:
    even with the constant widened, the writer subtracts the server-owned set
    itself, so the field is refused by name at the front door rather than dying
    deeper in with an internal-sounding message. Both matter -- one guards the
    declaration, the other guards the behaviour when the declaration is wrong.
    """
    client = jsp_client()
    smuggled = sorted(C.JSP_SERVER_OWNED_KEYS)[0]
    widened = tuple(C.JSP_WRITABLE_FIELDS) + (smuggled,)

    with mock.patch.object(C, "JSP_WRITABLE_FIELDS", widened):
        with pytest.raises(WriteRefused) as excinfo:
            client.profile_writer.update_job_search_profile(
                confirm=True, **{smuggled: 1}
            )

    message = str(excinfo.value)
    assert smuggled in message, "the refusal has to name the field"
    assert "No validator" not in message, (
        "it reached the validator's backstop instead of the by-name refusal, "
        "which means the writable set was not derived"
    )
    assert client.routes.requests == [], "a server-owned field reached the network"


# ---------------------------------------------------------------------------
# Verification and reporting: a 200 is not success
# ---------------------------------------------------------------------------


def test_a_write_that_does_not_read_back_is_reported_unverified():
    """The silent no-op, caught.

    The server accepts the PUT with a 200 and changes nothing, which is exactly
    what a broken write looks like from the outside. The re-read is the only
    thing that can tell the difference, so no key on the result may read as a
    receipt and the restore point has to be named where it can be acted on.
    """
    client = jsp_client(apply_put=False)

    result = client.profile_writer.update_job_search_profile(
        confirm=True, notice_period=2
    )

    assert result["executed"] is True
    assert result["verified"] is False
    assert result["mismatched"] == {
        "notice_period": {"wanted": 2, "got": JSP["notice_period"]}
    }
    assert "DID NOT VERIFY" in result["warning"]
    assert "instahyre_restore_profile" in result["warning"]
    assert result["snapshot_id"], "the result must name the restore point to use"
    assert result["reads_as_now"]["notice_period"]["index"] == JSP["notice_period"]
    assert len(client.jsp.put_bodies) == 1


def test_collateral_changes_are_reported():
    """The point of the whole exercise, and the risk a full replacement carries.

    Replacing the object can move fields nobody named -- some the server
    recomputes, and some it might not. Reporting the requested field and
    stopping would hide exactly the movement that makes this write worth
    watching, so anything else that differs between the snapshot's read and the
    read-back is surfaced, named, and left for a human to judge.
    """

    def flip(profile):
        profile.doc["jsp"]["is_immediate_joinee"] = not JSP["is_immediate_joinee"]

    client = jsp_client(after_put=flip)

    result = client.profile_writer.update_job_search_profile(
        confirm=True, notice_period=2
    )

    assert result["verified"] is True, "the requested field did land"
    assert result["also_changed_by_the_server"] == {
        "is_immediate_joinee": {
            "before": JSP["is_immediate_joinee"],
            "after": not JSP["is_immediate_joinee"],
        }
    }
    assert "is_immediate_joinee tracks notice_period" in result["collateral_note"]
    assert "notice_period" not in result["also_changed_by_the_server"], (
        "the requested field was reported as collateral"
    )


def test_the_profile_cache_is_expired_before_the_verifying_read():
    """A 15-minute cache would cheerfully verify the value we just changed.

    That is the worst possible failure of a verification step: it reports
    success by reading the state from BEFORE the write. The cache entry is
    therefore expired into the past first, and the ordering is what this test
    asserts -- an expiry that happened afterwards would be a no-op dressed as a
    precaution.
    """
    events = []
    client = jsp_client(store=RecordingStore(events), events=events)

    client.profile_writer.update_job_search_profile(confirm=True, notice_period=2)

    expiry = ("store.put", "profile", str(CANDIDATE_ID), -1)
    assert expiry in events, (
        "the profile cache was never expired; events were %r" % (events,)
    )
    at = events.index(expiry)
    reads = [index for index, event in enumerate(events) if event[0] == "GET profile"]
    assert [index for index in reads if index < at], "nothing was read before the write"
    assert [index for index in reads if index > at], (
        "no profile read happened AFTER the cache was expired -- the verification "
        "could have been served the pre-write value"
    )


# ---------------------------------------------------------------------------
# The read side: what a write is built on
# ---------------------------------------------------------------------------


def test_a_profile_without_a_jsp_refuses_rather_than_inventing_one():
    """A write built on an absent read invents the object it claims to update.

    Under a full replacement that is not a degraded write, it is a deletion of
    every key the server holds, replaced by whatever the caller happened to
    name. An empty jsp is the same case wearing a dict, so both are refused
    before anything is snapshotted or sent.
    """
    for absent in (None, {}):
        client = jsp_client(absent)

        with pytest.raises(ApiError) as excinfo:
            client.profile_writer.update_job_search_profile(
                confirm=True, notice_period=2
            )

        assert "jsp" in str(excinfo.value)
        assert "invents" in str(excinfo.value)
        assert describe(write_requests(client)) == []
        assert client.jsp.put_bodies == []
        assert snapshot_files() == []


def test_a_jsp_without_an_integer_id_refuses():
    """No id, no URL -- and the wrong id is a different row entirely.

    The write route binds the JSP's own id, which is not the candidate's, so a
    missing or non-integer id cannot be substituted or coerced from anything
    else on the object. ``True`` is included because it passes an isinstance
    check for ``int`` and would build ``candidate_skills/True``.
    """
    for bad in ("7770001", None, True, float(JSP_ID)):
        client = jsp_client(dict(JSP, id=bad))

        with pytest.raises(ApiError) as excinfo:
            client.profile_writer.plan_job_search_profile(notice_period=2)

        message = str(excinfo.value)
        assert "integer id" in message, bad
        assert "not the candidate's" in message
        assert describe(write_requests(client)) == []


# ---------------------------------------------------------------------------
# Restore: the snapshot IS a valid body
# ---------------------------------------------------------------------------


def test_a_snapshot_without_a_jsp_cannot_restore_one():
    """Snapshots taken before 2026-08-24 hold skills and scalars only.

    Restoring from one would mean guessing every key it does not carry, and on
    a full-replacement PUT each guess it got wrong -- or omitted -- would be a
    deletion. So it refuses and points at the listing tool, rather than putting
    back a partial object that reads as a successful rollback.
    """
    client = jsp_client(None)

    summary = client.profile_writer.snapshot(label="baseline")
    assert summary["jsp_captured"] is False
    assert summary["jsp_keys_captured"] == 0

    with pytest.raises(WriteRefused) as excinfo:
        client.profile_writer.restore_job_search_profile(confirm=True)

    assert "no job-search profile" in str(excinfo.value)
    assert "instahyre_list_profile_snapshots" in str(excinfo.value)
    assert describe(write_requests(client)) == []
    assert client.jsp.put_bodies == []


def test_a_restore_puts_the_whole_snapshot_object_back():
    """Restoring is the same request as writing, which is the one convenience a
    full-replacement resource offers: the captured object IS a valid body.

    So the payload has to be the snapshot verbatim -- not a diff, not the
    fields that happen to differ. A restore that sent only the changed keys
    would delete everything it left out, which is the failure it was called to
    undo.
    """
    client = jsp_client()
    client.profile_writer.snapshot(label="baseline")
    client.profile.doc["jsp"]["notice_period"] = 3
    client.profile.doc["jsp"]["status"] = 1

    result = client.profile_writer.restore_job_search_profile(confirm=True)

    assert client.jsp.put_bodies == [JSP]
    assert list(client.jsp.put_bodies[0]) == list(JSP), "even the key order"
    assert describe(write_requests(client)) == [("PUT", API_PREFIX + JSP_PATH)]
    assert result["executed"] is True
    assert result["reverted"] == ["notice_period", "status"]
    assert result["verified"] is True
    assert result["still_differs"] is None


def test_a_restore_that_would_drop_a_key_is_refused():
    """The snapshot is older than the object, so it can be NARROWER than it.

    If the platform has added a key since the capture, sending the snapshot
    back deletes that key -- a restore that quietly does damage of its own,
    which is the one thing a rollback tool may never do. The same guard the
    write path uses is applied here, in the direction that matters.
    """
    older = {k: v for k, v in JSP.items() if k != "has_manager_exp"}
    client = jsp_client(older)
    client.profile_writer.snapshot(label="baseline")
    client.profile.doc["jsp"] = copy.deepcopy(JSP)

    with pytest.raises(WriteRefused) as excinfo:
        client.profile_writer.restore_job_search_profile(confirm=True)

    assert "has_manager_exp" in str(excinfo.value)
    assert excinfo.value.context["fields"] == ["has_manager_exp"]
    assert client.jsp.put_bodies == []
    assert describe(write_requests(client)) == []


def test_restore_confirm_false_sends_nothing():
    """A restore is a write, so it gets the same gate and the same proof.

    The preview shows the exact request and what would revert; the transport
    records nothing. ``executed: False`` in the dict is not the assertion --
    the empty write list is.
    """
    client = jsp_client()
    client.profile_writer.snapshot(label="baseline")
    client.profile.doc["jsp"]["notice_period"] = 3

    preview = client.profile_writer.restore_job_search_profile()

    assert preview["executed"] is False
    assert preview["would_send"]["method"] == C.JSP_PUT_METHOD
    assert preview["would_send"]["url"] == C.API_BASE + JSP_PATH
    assert preview["would_send"]["json_body"] == JSP
    assert preview["would_revert"]["notice_period"] == {
        "now": 3,
        "snapshot": JSP["notice_period"],
    }
    assert describe(write_requests(client)) == []
    assert client.jsp.put_bodies == []
