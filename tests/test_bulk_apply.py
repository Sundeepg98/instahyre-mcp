"""Bulk apply -- the one-way door that was banned, and the gate that replaced the ban.

WHY THIS FILE IS SEPARATE, AND WHY IT IS THE STRICTEST ONE HERE. Both paths
this tool reaches sat in ``constants.FORBIDDEN_ENDPOINTS`` under the only words
in this package that ever said "at any evidence level". The ruling on
2026-08-25 is that whatever is technically POSSIBLE gets built; the contract
for bulk apply ships whole in Instahyre's own JavaScript, so it was possible,
so it exists. Lifting a permanent ban is not a refactor, and the assertions
that used to pin that ban were not deleted to make room -- each was re-ratified
where it lives, with the change recorded in its own docstring.

THE ARGUMENT THE GATE IS MAKING, because every test below is a clause of it. A
confirm-gated bulk apply is NOT inherently more dangerous than N confirm-gated
single applies. It is the same N applications, to the same employers, equally
permanent, sent by the same account. What it removes is N-1 CONFIRMATIONS --
that is the entire delta, and so the entire job of the gate is handing back
what the collapse takes:

  * the PREVIEW NAMES EVERY OPPORTUNITY, one line each, restoring the sight of
    each item that N separate previews would have given. A caller confirming
    "12 opportunities" without seeing which twelve is the failure this tool
    exists to prevent, so the naming is not cosmetic and is tested as a gate.
  * ``expected_count`` restores the ARITHMETIC -- a second statement of intent,
    made independently of the list, so a list that changed length between the
    preview and the call fails loudly instead of applying.
  * ``MAX_BULK_APPLY`` bounds the BLAST RADIUS, and over the cap is a REFUSAL.
    Never a truncation: applying to the first ten of twenty-five and reporting
    success is the single worst thing this tool could do, because it is
    indistinguishable from working.
  * the ID LIST IS THE CALLER'S. Nothing here assembles one. No "apply to all",
    no filter argument, no "top N by score", no default that means everything.

AND THE DEFAULT POINTS THE OTHER WAY FROM THE SITE'S. Instahyre's own bulk
modal opens with everything already ticked --
``angular.forEach($scope.oppValues,function(oppVal){oppVal.isSelected=true;})``
-- so on their page "apply to everything shown" is what happens if you press
the button, and deselecting is the work. This tool selects NOTHING and refuses
an empty list rather than reading it as "all". That inversion is tested.

APPLICATIONS CANNOT BE WITHDRAWN. Instahyre's FAQ says the application is sent
automatically by the system: no undo, no support path, the employer sees it
immediately. There is no bulk DECLINE either, and that is structural rather
than chosen -- the bulk body has no ``is_interested`` key at all.

WHAT THIS FILE CANNOT CLAIM. No bulk apply has ever been sent by this server,
and unlike the inbox writes the reason is not that the control is unreachable:
the site renders it readily. It is that pressing it APPLIES, irreversibly, to
whatever is selected -- which the site has pre-selected. So the capture
technique that recorded the other bodies (drive the control, abort at the
router) cannot be pointed at this one without risking the exact event it would
be measuring. Every fixture here is HAND-BUILT from the shipped contract. These
tests measure that this package sends what Instahyre's own JavaScript says it
sends; they do not and cannot measure what Instahyre does with it.

THE INSTRUMENTS ARE BORROWED ON PURPOSE. ``NoWriteHTTP``, ``BodyRecorder``,
``describe`` and ``write_requests`` come from ``test_writes.py``, where
``test_the_no_send_assertion_can_fail__CONTROL`` already points the detonating
double at a deliberately ungated write and watches it fire. Re-implementing
them here would prove only that the copy works.

Companies and roles below are INVENTED. The captured queue fixture carries real
employer names because it is a capture; nothing hand-built in this package does.
"""

from __future__ import annotations

import inspect

import pytest

from conftest import make_client
from instahyre_server import constants as C
from instahyre_server import server as server_module
from instahyre_server import writes as writes_module
from instahyre_server.inbox import MutatingPathRefused, guard_read_only
from instahyre_server.writes import (
    MAX_BULK_APPLY,
    ConfirmationRequired,
    NothingToDo,
    Writer,
)
from instahyre_server.errors import NotFound
from test_writes import BodyRecorder, NoWriteHTTP, describe, write_requests

# ---------------------------------------------------------------------------
# Hand-built queue fixtures
# ---------------------------------------------------------------------------

#: Invented employers, in the order they are handed out. Long enough to build a
#: queue past the cap, because "over the cap refuses" cannot be tested on a
#: six-record fixture.
COMPANIES = [
    ("Northwind Analytics", "Senior Backend Engineer"),
    ("Larkspur Systems", "Platform Engineer"),
    ("Fernway Labs", "Node.js Developer"),
    ("Coldbrook Data", "Staff Engineer"),
    ("Marlowe Interactive", "Fullstack Engineer"),
    ("Ashgrove Robotics", "Backend Engineer"),
    ("Pemberton Cloud", "Site Reliability Engineer"),
    ("Quillon Health", "TypeScript Engineer"),
    ("Rothwell Media", "API Engineer"),
    ("Stonecrest Payments", "Senior Engineer"),
    ("Thackeray Mobility", "Services Engineer"),
    ("Underhill Logistics", "Principal Engineer"),
    ("Vexley Retail", "Node Engineer"),
    ("Wrenfield Energy", "Backend Developer"),
    ("Yarrow Biotech", "Software Engineer"),
]

#: The two ids on a queue record are NOT interchangeable, and the bulk body is
#: built from a DIFFERENT one on each branch: ``job.id`` on ES, ``id`` on
#: legacy. They are deliberately far apart numerically so a test that got them
#: the wrong way round could not accidentally pass.
FIRST_OPPORTUNITY_ID = 6100000000
FIRST_JOB_ID = 430000


def opportunity(index: int) -> dict:
    """One hand-built queue record, shaped like the captured ones."""
    company, title = COMPANIES[index % len(COMPANIES)]
    return {
        "id": str(FIRST_OPPORTUNITY_ID + index),
        "job": {
            "id": FIRST_JOB_ID + index,
            "title": title,
            "locations": ["Bangalore"],
            "keywords": "Node.js, TypeScript",
            "is_active": True,
        },
        "employer": {"id": 42000 + index, "company_name": company},
        "interview_status": 0,
        "score": 90.0 - index,
        "is_active": True,
    }


def queue_payload(records: list, *, total_count=None) -> dict:
    """A tastypie envelope around ``records``.

    ``total_count`` defaults to the number of records, so the read reports
    itself COMPLETE. A test that wants the truncated case passes a larger one.
    """
    return {
        "meta": {
            "offset": 0,
            "limit": C.OPP_MAX_LIMIT,
            "total_count": len(records) if total_count is None else total_count,
            "previous": None,
            "next": None,
        },
        "objects": list(records),
    }


class ShrinkingQueue:
    """A pending-queue route that DROPS ids once the apply has been sent.

    This is the instrument for the post-write verification, and it has to be a
    stateful route rather than a fixed payload: the check being tested is "read
    the queue again and see which ones left", which is unfalsifiable against a
    payload that never changes. ``applied`` is filled by the test at the moment
    it wants the queue to start answering differently.
    """

    def __init__(self, records, *, total_count=None):
        self.records = list(records)
        self.total_count = total_count
        self.applied: set = set()
        self.calls = 0

    def __call__(self, request):
        self.calls += 1
        live = [r for r in self.records if str(r["id"]) not in self.applied]
        return queue_payload(live, total_count=self.total_count)


def bulk_client(records, *, csrf="csrf-for-the-bulk-apply", send_route=None, queue=None):
    """A client wired for the queue reads a bulk apply makes, and no write route.

    Taxonomy is unwired for the same reason the other write files unwire it: a
    bulk apply has no business resolving a location, so a stray taxonomy read
    is a loud "Unmocked request" rather than a silent success. The write route
    is absent unless a test asks for one, so a write that escaped the gate
    lands on an unmocked path and is recorded as a fact.
    """
    routes = {C.EP_OPPORTUNITIES: queue if queue is not None else queue_payload(records)}
    if send_route is not None:
        routes[bulk_path()] = send_route
    client = make_client(routes, with_taxonomy=False)
    if csrf:
        client.http.cookies.set("csrftoken", csrf, domain="www.instahyre.com")
    return client


def writer_for(client) -> Writer:
    return Writer(client.http, client.store, client.inbound, client.inbox)


def detonating_writer_for(client):
    """``(writer, http)`` whose writer explodes on any write verb.

    Reads still go through the genuine mocked client underneath, so the queue
    reads a preview makes are real reads and the recorded request list stays
    the true record of the wire.
    """
    http = NoWriteHTTP(client.http)
    return Writer(http, client.store, client.inbound, client.inbox), http


def bulk_path() -> str:
    return C.EP_APPLY_BULK_ES if C.APPLY_BRANCH_ES else C.EP_APPLY_BULK_LEGACY


def ids_for(indexes) -> list:
    return [str(FIRST_OPPORTUNITY_ID + i) for i in indexes]


def queue_reads(client) -> int:
    return client.routes.count(C.EP_OPPORTUNITIES)


# ===========================================================================
# GATE 1 -- confirm=True is required, and without it NOTHING is requested
# ===========================================================================


def test_a_bulk_apply_without_confirm_issues_no_write_at_all():
    """The default is a preview and the preview is INERT.

    Asserted on the WIRE, not on the return value. A test that only checked the
    returned dict would pass against an implementation that POSTs first and
    describes itself afterwards, which is the bug that is invisible in review
    and permanent in production.
    """
    client = bulk_client([opportunity(i) for i in range(4)])
    writer, http = detonating_writer_for(client)

    result = writer.bulk_apply(ids_for([0, 1, 2]), 3)

    assert result["confirmed"] is False
    assert http.attempts == []
    assert write_requests(client) == [], describe(client.routes.requests)


def test_the_preview_shows_the_exact_request_that_would_be_sent():
    """The consent is only informed if what is shown is what would go out."""
    client = bulk_client([opportunity(i) for i in range(4)])
    writer, _ = detonating_writer_for(client)

    preview = writer.bulk_apply(ids_for([0, 1]), 2)

    assert preview["would_send"]["method"] == "POST"
    assert preview["would_send"]["url"] == C.API_BASE + bulk_path()
    assert preview["would_send"]["json_body"] == {
        C.BULK_APPLY_BODY_KEY_ES: [FIRST_JOB_ID, FIRST_JOB_ID + 1]
    }
    assert preview["irreversible"] is True
    assert "cannot be withdrawn" in preview["warning"].lower()


def test_the_confirm_default_is_false_on_the_tool_and_on_the_method():
    """Omission must mean refusal, at both layers a caller can reach."""
    for func in (server_module.instahyre_apply_bulk, Writer.bulk_apply):
        parameters = inspect.signature(func).parameters
        assert parameters["confirm"].default is False, func


# ===========================================================================
# GATE 2 -- expected_count is required and must equal what resolved
# ===========================================================================


def test_expected_count_has_no_default_so_it_cannot_be_omitted():
    """A second confirmation with a default value is not a second
    confirmation. It has to be stated, at the tool boundary and below it."""
    for func in (server_module.instahyre_apply_bulk, Writer.bulk_apply):
        parameter = inspect.signature(func).parameters["expected_count"]
        assert parameter.default is inspect.Parameter.empty, func


@pytest.mark.parametrize("stated", [2, 4, 0, None])
def test_a_wrong_expected_count_refuses_and_sends_nothing(stated):
    """Both directions are refused -- too low and too high.

    This is the check that notices a list which changed length between the
    preview and the confirm. Three ids resolve; anything but three is a
    disagreement between what the caller counted and what would actually
    happen, and on an irreversible action a disagreement is a refusal.
    """
    client = bulk_client([opportunity(i) for i in range(4)])
    writer, http = detonating_writer_for(client)

    with pytest.raises(ConfirmationRequired) as excinfo:
        writer.bulk_apply(ids_for([0, 1, 2]), stated, confirm=True)

    assert "expected_count" in str(excinfo.value)
    assert http.attempts == []
    assert write_requests(client) == [], describe(client.routes.requests)


def test_the_count_is_compared_against_what_resolved_not_against_the_argument():
    """It matches the number of APPLICATIONS, which is the number a human is
    being asked to agree to -- not the length of a list that might contain
    something that does not resolve."""
    client = bulk_client([opportunity(i) for i in range(4)])
    writer, _ = detonating_writer_for(client)

    preview = writer.bulk_apply(ids_for([0, 1, 2]), 3)

    assert preview["would_apply_to_count"] == 3 == len(preview["would_apply_to"])


# ===========================================================================
# GATE 3 -- the caller passes the list; the tool never assembles one
# ===========================================================================


def test_the_tool_offers_no_way_to_select_a_list_instead_of_passing_one():
    """No filter, no "apply to all", no "top N", no default meaning everything.

    Keyed on the SIGNATURE rather than on behaviour, because this is a claim
    about what cannot be asked for. A parameter named here would be a way to
    have the tool choose employers, and choosing is the caller's job.
    """
    parameters = inspect.signature(server_module.instahyre_apply_bulk).parameters
    assert sorted(parameters) == ["confirm", "expected_count", "opportunity_ids"]
    assert parameters["opportunity_ids"].default is inspect.Parameter.empty
    # The same claim one layer down, so the tool cannot stay narrow while the
    # method it delegates to grows a selection knob that something else calls.
    method = inspect.signature(Writer.bulk_apply).parameters
    assert sorted(method) == ["confirm", "expected_count", "opportunity_ids", "self"]
    # And the promise is made to the caller in the description the MCP client
    # actually shows, not only in a comment nobody downstream can read.
    doc = " ".join((server_module.instahyre_apply_bulk.__doc__ or "").lower().split())
    assert "never assembles" in doc or "this tool never assembles it" in doc


def test_the_body_contains_only_ids_the_caller_named():
    """The queue holds ten; three were asked for; three are sent.

    The failure this excludes is a builder that resolves the caller's ids and
    then sends the queue -- which would look correct in a preview built from
    the same wrong list.
    """
    client = bulk_client([opportunity(i) for i in range(10)])
    writer, _ = detonating_writer_for(client)

    preview = writer.bulk_apply(ids_for([2, 5, 7]), 3)

    assert preview["would_send"]["json_body"][C.BULK_APPLY_BODY_KEY_ES] == [
        FIRST_JOB_ID + 2,
        FIRST_JOB_ID + 5,
        FIRST_JOB_ID + 7,
    ]


# ===========================================================================
# GATE 4 -- the cap REFUSES; it never truncates
# ===========================================================================


def test_the_cap_is_a_named_constant_and_is_well_under_his_queue():
    """Ten, against a pending queue of roughly thirty. The number is asserted
    so that raising it is an edit somebody made on purpose."""
    assert MAX_BULK_APPLY == 10


def test_over_the_cap_refuses_rather_than_truncating():
    """THE MOST IMPORTANT TEST IN THIS FILE.

    Silently applying to the first ten of a longer list is the one failure that
    is indistinguishable from success: the caller is told applications were
    sent, having asked for more, and the difference is invisible without
    re-reading the queue. So the assertion is not merely that fewer than
    fifteen were sent -- it is that NOTHING was sent and the refusal names both
    numbers.
    """
    client = bulk_client([opportunity(i) for i in range(15)])
    writer, http = detonating_writer_for(client)

    with pytest.raises(NothingToDo) as excinfo:
        writer.bulk_apply(ids_for(range(15)), 15, confirm=True)

    message = str(excinfo.value)
    assert "15" in message and str(MAX_BULK_APPLY) in message
    assert "truncation" in message.lower() or "not a truncation" in message.lower()
    assert http.attempts == []
    assert write_requests(client) == [], describe(client.routes.requests)


def test_one_over_the_cap_is_refused_too():
    """The boundary, from the wrong side. Eleven is not "about ten"."""
    client = bulk_client([opportunity(i) for i in range(12)])
    writer, _ = detonating_writer_for(client)

    with pytest.raises(NothingToDo):
        writer.bulk_apply(ids_for(range(MAX_BULK_APPLY + 1)), MAX_BULK_APPLY + 1)


def test_exactly_the_cap_is_allowed():
    """The permitting half. A cap that refused its own value would be a cap of
    nine wearing the number ten, and no test that only ever saw refusals would
    notice."""
    client = bulk_client([opportunity(i) for i in range(12)])
    writer, _ = detonating_writer_for(client)

    preview = writer.bulk_apply(ids_for(range(MAX_BULK_APPLY)), MAX_BULK_APPLY)

    assert preview["would_apply_to_count"] == MAX_BULK_APPLY


def test_the_cap_is_checked_before_the_queue_is_read():
    """An over-long list costs no request, and -- the real point -- its refusal
    cannot be confused with a resolution failure."""
    client = bulk_client([opportunity(i) for i in range(15)])
    writer, _ = detonating_writer_for(client)

    with pytest.raises(NothingToDo):
        writer.bulk_apply(ids_for(range(15)), 15)

    assert queue_reads(client) == 0


# ===========================================================================
# GATE 5 -- every id is validated against the CURRENT pending queue
# ===========================================================================


def test_an_id_that_is_not_in_the_pending_queue_is_refused_by_name():
    """A stale id must not slip through inside a bulk body.

    Naming it matters as much as refusing it: in a list of ten, "one of these
    is not valid" is not actionable, and the caller's next move would be to
    guess.
    """
    client = bulk_client([opportunity(i) for i in range(4)])
    writer, http = detonating_writer_for(client)
    stale = str(FIRST_OPPORTUNITY_ID + 99)

    with pytest.raises(NotFound) as excinfo:
        writer.bulk_apply(ids_for([0, 1]) + [stale], 3, confirm=True)

    assert stale in str(excinfo.value)
    assert http.attempts == []
    assert write_requests(client) == [], describe(client.routes.requests)


def test_an_id_that_has_already_been_actioned_is_not_pending_and_is_refused():
    """The realistic version of the stale id: he applied in the browser five
    minutes ago, so the opportunity has left the pending facet."""
    records = [opportunity(i) for i in range(4)]
    already = records.pop(1)
    client = bulk_client(records)
    writer, _ = detonating_writer_for(client)

    with pytest.raises(NotFound) as excinfo:
        writer.bulk_apply([str(already["id"]), str(records[0]["id"])], 2, confirm=True)

    assert str(already["id"]) in str(excinfo.value)


def test_the_pending_queue_is_re_read_rather_than_served_from_cache():
    """A cached queue would let an opportunity actioned minutes ago validate.

    Everything else in this package is happy with a queue a few minutes old.
    This one is deciding whether an irreversible application is about to be
    aimed at something that is still there, so it pays for a fresh read.
    """
    client = bulk_client([opportunity(i) for i in range(4)])
    writer, _ = detonating_writer_for(client)
    client.inbound.raw_queue(interest="pending")
    before = queue_reads(client)

    writer.bulk_apply(ids_for([0, 1]), 2)

    assert queue_reads(client) > before


def test_a_truncated_queue_read_says_so_in_the_refusal():
    """A short read and an empty queue are opposite facts that arrive looking
    identical. Refusing is the safe direction either way; giving the wrong
    REASON sends the caller hunting for an id that exists."""
    records = [opportunity(i) for i in range(4)]
    client = bulk_client(records, queue=None)
    client.routes.routes["/candidate_opportunities/candidate_matching"] = queue_payload(
        records, total_count=228
    )
    writer, _ = detonating_writer_for(client)

    with pytest.raises(NotFound) as excinfo:
        writer.bulk_apply([str(FIRST_OPPORTUNITY_ID + 99)], 1)

    assert "TRUNCATED" in str(excinfo.value)


# ===========================================================================
# GATE 6 -- the preview NAMES every opportunity, one line each
# ===========================================================================


def test_the_preview_names_every_opportunity_one_line_each():
    """A caller confirming "3 opportunities" without seeing which three is the
    failure mode this tool exists to prevent. So the naming is a gate, not
    presentation: company, role and id, one line per application."""
    client = bulk_client([opportunity(i) for i in range(6)])
    writer, _ = detonating_writer_for(client)

    preview = writer.bulk_apply(ids_for([0, 2, 4]), 3)

    lines = preview["would_apply_to_lines"]
    assert len(lines) == 3
    for index, line in zip((0, 2, 4), lines):
        company, role = COMPANIES[index]
        assert company in line
        assert role in line
        assert str(FIRST_OPPORTUNITY_ID + index) in line


def test_every_named_opportunity_carries_its_company_role_and_both_ids():
    """The structured half of the same claim, so a caller that renders the list
    itself gets the same facts the lines carry."""
    client = bulk_client([opportunity(i) for i in range(6)])
    writer, _ = detonating_writer_for(client)

    preview = writer.bulk_apply(ids_for([1, 3]), 2)

    for index, entry in zip((1, 3), preview["would_apply_to"]):
        company, role = COMPANIES[index]
        assert entry["company"] == company
        assert entry["role"] == role
        assert entry["opportunity_id"] == str(FIRST_OPPORTUNITY_ID + index)
        assert entry["job_id"] == FIRST_JOB_ID + index


def test_the_preview_never_reports_a_count_without_the_names():
    """The count and the list cannot come apart: whatever number is displayed,
    that many opportunities are named beside it."""
    client = bulk_client([opportunity(i) for i in range(8)])
    writer, _ = detonating_writer_for(client)

    preview = writer.bulk_apply(ids_for([0, 1, 2, 3, 4]), 5)

    assert preview["would_apply_to_count"] == 5
    assert len(preview["would_apply_to"]) == 5
    assert len(preview["would_apply_to_lines"]) == 5


# ===========================================================================
# GATE 7 -- an empty list and duplicates are REFUSED, not repaired
# ===========================================================================


def test_an_empty_list_is_refused_and_never_read_as_everything():
    """Instahyre's own modal pre-selects the whole queue, so "nothing selected
    means everything" is a real interpretation ON THIS PLATFORM. It is the
    dangerous one, and this tool takes the opposite default."""
    client = bulk_client([opportunity(i) for i in range(4)])
    writer, http = detonating_writer_for(client)

    with pytest.raises(NothingToDo):
        writer.bulk_apply([], 0, confirm=True)

    assert http.attempts == []
    assert write_requests(client) == [], describe(client.routes.requests)
    assert queue_reads(client) == 0


def test_a_duplicate_id_is_refused_rather_than_deduplicated():
    """Deduplicating would send fewer applications than the expected_count that
    was confirmed -- the same class of silent-arithmetic bug as truncating an
    over-long list, arriving from the other side."""
    client = bulk_client([opportunity(i) for i in range(4)])
    writer, http = detonating_writer_for(client)
    repeated = str(FIRST_OPPORTUNITY_ID)

    with pytest.raises(NothingToDo) as excinfo:
        writer.bulk_apply([repeated, str(FIRST_OPPORTUNITY_ID + 1), repeated], 3, confirm=True)

    assert repeated in str(excinfo.value)
    assert http.attempts == []
    assert write_requests(client) == [], describe(client.routes.requests)


@pytest.mark.parametrize("bad", [None, "6100000000", {"id": 1}])
def test_something_that_is_not_a_list_is_refused(bad):
    """A bare id string is the plausible mistake, and it would otherwise
    iterate into a list of characters."""
    client = bulk_client([opportunity(i) for i in range(4)])
    writer, _ = detonating_writer_for(client)

    with pytest.raises(NothingToDo):
        writer.bulk_apply(bad, 1, confirm=True)


def test_a_blank_entry_is_refused_rather_than_skipped():
    """Skipping it would change how many applications are sent without changing
    the count anybody agreed to."""
    client = bulk_client([opportunity(i) for i in range(4)])
    writer, _ = detonating_writer_for(client)

    with pytest.raises(NothingToDo):
        writer.bulk_apply([str(FIRST_OPPORTUNITY_ID), "  "], 2)


# ===========================================================================
# GATE 8 -- CSRF, as on every other write
# ===========================================================================


def test_a_confirmed_bulk_apply_without_a_csrf_token_refuses_before_sending():
    """An unsigned write would be rejected by Django and surface as an
    unexplained 403 on the one call where an ambiguous error is most
    expensive -- did any of them go through?"""
    client = bulk_client([opportunity(i) for i in range(4)], csrf=None)
    writer, http = detonating_writer_for(client)

    with pytest.raises(ConfirmationRequired) as excinfo:
        writer.bulk_apply(ids_for([0, 1]), 2, confirm=True)

    assert "csrf" in str(excinfo.value).lower()
    assert http.attempts == []
    assert write_requests(client) == [], describe(client.routes.requests)


# ===========================================================================
# The confirmed call: ONE post, right path, right body
# ===========================================================================


def test_a_confirmed_bulk_apply_makes_exactly_one_post_to_the_bulk_endpoint():
    """One action, one request. Not one per opportunity, not a retry loop."""
    records = [opportunity(i) for i in range(4)]
    queue = ShrinkingQueue(records)
    recorder = BodyRecorder({"success": True})
    client = bulk_client(records, send_route=recorder, queue=queue)
    writer = writer_for(client)

    result = writer.bulk_apply(ids_for([0, 1]), 2, confirm=True)

    posts = [r for r in client.routes.requests if r.method == "POST"]
    assert len(posts) == 1, describe(client.routes.requests)
    assert posts[0].url.path.endswith(bulk_path())
    assert result["confirmed"] is True


def test_the_body_carries_exactly_one_key_and_it_is_the_branch_key():
    """The factory sets job_ids on ES and opp_ids on legacy, never both."""
    records = [opportunity(i) for i in range(4)]
    recorder = BodyRecorder({"success": True})
    client = bulk_client(records, send_route=recorder, queue=ShrinkingQueue(records))
    writer = writer_for(client)

    writer.bulk_apply(ids_for([0, 1]), 2, confirm=True)

    assert recorder.calls == [{C.BULK_APPLY_BODY_KEY_ES: [FIRST_JOB_ID, FIRST_JOB_ID + 1]}]
    assert list(recorder.calls[0]) == ["job_ids"]


def test_the_body_has_no_is_interested_key_because_there_is_no_bulk_decline():
    """Structural, not chosen. Bulk is apply-only: the shipped builder sets one
    id key and nothing else, so there is no boolean here to flip into a mass
    decline -- which would reshape which employers he is matched with in future
    cycles and is equally permanent."""
    records = [opportunity(i) for i in range(4)]
    recorder = BodyRecorder({"success": True})
    client = bulk_client(records, send_route=recorder, queue=ShrinkingQueue(records))
    writer = writer_for(client)

    writer.bulk_apply(ids_for([0]), 1, confirm=True)

    assert "is_interested" not in recorder.calls[0]
    assert "is_activity_page_job" not in recorder.calls[0]


def test_the_legacy_branch_sends_opp_ids_to_the_legacy_url(monkeypatch):
    """The URL and the id key move TOGETHER, on the same flag that switches
    single apply. Pairing an ES body with a legacy URL is a request the site
    never makes, and this package has made that exact mistake before."""
    monkeypatch.setattr(C, "APPLY_BRANCH_ES", False)
    records = [opportunity(i) for i in range(4)]
    recorder = BodyRecorder({"success": True})
    client = bulk_client(records, send_route=recorder, queue=ShrinkingQueue(records))
    writer = writer_for(client)

    writer.bulk_apply(ids_for([0, 1]), 2, confirm=True)

    posts = [r for r in client.routes.requests if r.method == "POST"]
    assert posts[0].url.path.endswith(C.EP_APPLY_BULK_LEGACY)
    assert list(recorder.calls[0]) == ["opp_ids"]
    assert recorder.calls[0]["opp_ids"] == ids_for([0, 1])


def test_the_branch_is_the_single_apply_flag_and_not_a_second_mechanism():
    """One flag, or two flags that can disagree. The second is how the ES body
    got paired with the legacy URL the first time."""
    source = inspect.getsource(writes_module._build_bulk_apply_request)
    assert "if C.APPLY_BRANCH_ES:" in source
    # THE STRUCTURAL HALF, and the one that would catch a second mechanism
    # being introduced elsewhere: constants holds exactly ONE branch flag, so
    # there is nothing for a bulk-specific one to be spelled as.
    branch_flags = sorted(name for name in dir(C) if "BRANCH" in name)
    assert branch_flags == ["APPLY_BRANCH_ES"], branch_flags


# ===========================================================================
# GATE 9 -- after the write, re-read and report which actually took
# ===========================================================================


def test_the_result_reports_which_applications_actually_took():
    """The response is NOT the evidence. No bulk apply has ever been sent from
    here, so nobody knows what its reply looks like. The queue is understood:
    an opportunity that has been applied to leaves the pending facet."""
    records = [opportunity(i) for i in range(4)]
    queue = ShrinkingQueue(records)
    wanted = ids_for([0, 1])

    def route(request):
        queue.applied.update(wanted)
        return {"success": True}

    client = bulk_client(records, send_route=route, queue=queue)
    writer = writer_for(client)

    result = writer.bulk_apply(wanted, 2, confirm=True)

    verification = result["verification"]
    assert verification["ok"] is True
    assert verification["still_pending"] == []
    assert [entry["opportunity_id"] for entry in verification["applied"]] == wanted
    assert verification["applied"][0]["company"] == COMPANIES[0][0]


def test_an_application_that_did_not_take_is_reported_still_pending():
    """Half-landed is the outcome that most needs saying out loud, and the
    report must not suggest re-sending: a second bulk apply to one that DID
    take cannot be undone either."""
    records = [opportunity(i) for i in range(4)]
    queue = ShrinkingQueue(records)
    wanted = ids_for([0, 1])

    def route(request):
        queue.applied.add(wanted[0])
        return {"success": True}

    client = bulk_client(records, send_route=route, queue=queue)
    writer = writer_for(client)

    result = writer.bulk_apply(wanted, 2, confirm=True)

    verification = result["verification"]
    assert verification["ok"] is False
    assert verification["still_pending"] == [wanted[1]]
    assert "do not re-send" in verification["warning"].lower()


def test_the_verification_re_reads_the_queue_after_the_post():
    """It is a fresh read, not the one the preview already made."""
    records = [opportunity(i) for i in range(4)]
    queue = ShrinkingQueue(records)
    client = bulk_client(records, send_route={"success": True}, queue=queue)
    writer = writer_for(client)

    writer.bulk_apply(ids_for([0]), 1, confirm=True)

    assert queue.calls >= 2


# ===========================================================================
# The doors: a named allowlist of two, and a read tier that never moved
# ===========================================================================


@pytest.mark.parametrize(
    "path",
    [
        "/candidate_opportunities/candidate_matching/apply_bulk",
        "/candidate_opportunities/candidate_matching/apply_bulk/x",
        "/candidate_opportunities/candidate_matching/apply/",
        "/resume_modal/emails/message/send_message/",
        "",
    ],
)
def test_the_bulk_door_refuses_anything_that_is_not_one_of_the_two_named_paths(path):
    """An allowlist, not a blocklist. The slashless spelling is not pedantry:
    the factory declares the trailing slash on both services, and Django
    answers a slashless POST with a 301 that drops the body -- so a near-miss
    would send an EMPTY bulk apply rather than failing."""
    with pytest.raises(writes_module.NotSendable):
        writes_module._guard_bulk_apply_sendable(path)


@pytest.mark.parametrize("path", sorted(C.SENDABLE_BULK_APPLY_PATHS))
def test_the_bulk_door_admits_each_path_it_is_supposed_to(path):
    """The permitting half. A guard only ever seen refusing might refuse
    everything, which would be a broken tool rather than a safe one."""
    assert writes_module._guard_bulk_apply_sendable(path) == path


@pytest.mark.parametrize("path", sorted(C.SENDABLE_BULK_APPLY_PATHS))
def test_the_read_tier_still_refuses_both_bulk_paths(path):
    """THE THING THAT DID NOT MOVE. ``apply_bulk`` never left
    ``MUTATING_PATH_MARKERS``, so reaching a write from the reader stays
    impossible -- the write side is a separate door, not a hole in this
    guard."""
    with pytest.raises(MutatingPathRefused):
        guard_read_only(path)


def test_single_apply_still_cannot_reach_a_bulk_path():
    """Two doors, and the narrow one did not widen. Restated here because this
    is the file a reader opens to find out what bulk apply can do."""
    assert not (C.SENDABLE_BULK_APPLY_PATHS & C.SENDABLE_INBOX_PATHS)
    for path in C.SENDABLE_BULK_APPLY_PATHS:
        assert path not in (C.EP_APPLY_ES, C.EP_APPLY_LEGACY)


def test_the_docstring_admits_the_tool_has_never_run_live():
    """It has not, and the reason is worth keeping visible: the only way to
    make the site build a bulk apply is to actually apply."""
    # Whitespace-normalised on purpose: a docstring assertion that depends on
    # where a line happens to wrap tests the formatter, not the promise.
    doc = " ".join((server_module.instahyre_apply_bulk.__doc__ or "").lower().split())
    assert "cannot be withdrawn" in doc
    assert "no bulk decline" in doc
    method_doc = " ".join((Writer.bulk_apply.__doc__ or "").split())
    assert "N-1 CONFIRMATIONS" in method_doc, "the reasoning has left the docstring"
