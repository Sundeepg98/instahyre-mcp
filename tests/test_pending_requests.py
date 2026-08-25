"""The leaderboard cluster -- the one channel where INSTAHYRE asks HIM.

WHY THIS FILE IS SEPARATE. Every other write surface in this package is a
request he initiates: an application, a reply, a ticket, a profile edit. These
three endpoints run the other way. ``show_verify_modal`` is Instahyre putting a
question to the account holder -- were you hired at this company -- and the
answer is a TERMINAL status change on a channel that, until 2026-08-25, nothing
in this server could see at all. ``get_opportunity_info`` carries the lighter
one: how did that opportunity go.

THE CHANNEL IS EMPTY, AND THAT IS THE CENTRAL FACT THIS FILE IS BUILT AROUND.
Read from his own signed-in browser on 2026-08-25, all three answering HTTP
200:

    show_verify_modal        -> {"data": []}
    verify_hired_candidate   -> {"objects": [], "meta": {...}}
    get_opportunity_info     -> {"show_modal": false}

So nothing here has ever been exercised against live data, and this file says
so rather than implying a test that did not happen. EMPTY IS NOT ABSENT: the
routes exist, they authenticate, and they answer. What follows from it is the
property the whole gate rests on -- because both writes validate their id
against a LIVE re-read of the endpoint that offers it, and both those reads are
empty, EVERY WRITE IN THIS CLUSTER REFUSES TODAY. That is not a limitation
being worked around. It is the gate, and it gets a test of its own
(``test_every_write_refuses_today_because_the_live_channel_is_empty``).

THE CLAIMS DEFENDED HERE, one control each in
``scripts/pending_requests_controls.py``:

  NOTHING PENDING IS A POSITIVE RESULT. The read says so in words, never as an
  error and never as a bare empty dict a caller could read as failure.
  A FAILED READ STILL FAILS. The mirror, and the one that matters more: a
  lapsed session must never arrive looking like "nothing pending".
  THE READ COVERS ALL THREE ROUTES, because a caller who had to know three
  route names would never have looked.
  THE PREVIEW IS INERT. confirm=False issues no write at all, on both writes.
  THE ID COMES FROM A LIVE READ. A fabricated hired_id or rating_uri cannot be
  submitted, and the check is a re-read rather than a remembered list.
  THE BODY IS THE CAPTURED ONE, and so is the QUERY STRING -- which on this
  cluster is not a detail. ``submit_rating`` declares Angular action params, so
  its three fields ride the query as well as the body, and reproducing one half
  only would be a guessed request.
  THE SITE'S OWN TWO RATING GUARDS ARE REPRODUCED, not invented, and are
  labelled as theirs.
  CHOICE IS NOT GIVEN A MEANING IT HAS NOT BEEN MEASURED TO HAVE.
  CSRF IS REQUIRED, as on every other write.
  THE DOOR IS A NAMED ALLOWLIST OF TWO, and the collection url is deliberately
  not on it -- which is the only thing keeping ``add_joining_date`` unreachable.

A NOTE ON WHAT COUNTS AS RED, inherited from the sibling controls: pytest exits
1 when a test fails and 4 when a node id does not exist, so only exit 1 is RED.

Nothing here contacts Instahyre. Strict ASCII, like every file in this package.
"""

from __future__ import annotations

import inspect
import json

import pytest

from conftest import API_PREFIX, json_response, make_client
from instahyre_server import constants as C
from instahyre_server import server as server_module
from instahyre_server import writes as writes_module
from instahyre_server.errors import InstahyreError, NotFound
from instahyre_server.writes import (
    ConfirmationRequired,
    NotSendable,
    NothingToDo,
    Writer,
)
from test_writes import EDUCATION, NoWriteHTTP, WriteAttempted, describe, write_requests

# ===========================================================================
# The payloads
# ===========================================================================

#: EXACTLY WHAT THE LIVE ROUTES ANSWERED on 2026-08-25, authenticated, 200.
#: These are the DEFAULT in every client below, so the default state of this
#: file is his real one, and a test that wants a populated channel has to say
#: so explicitly. That direction matters: it makes "the writes refuse today"
#: the thing being measured rather than a thing being arranged around.
LIVE_EMPTY_MODAL = {"data": []}
LIVE_EMPTY_QUEUE = {
    "meta": {"offset": 0, "limit": 20, "total_count": 0, "previous": None, "next": None},
    "objects": [],
}
LIVE_NOTHING_OFFERED = {"show_modal": False}

#: HAND-BUILT AND ADMITTING IT. No populated record of either shape has ever
#: been read, so every field name below comes from Instahyre's own reader --
#: the block that fills $scope.verifyHireData names rec_name, company_name,
#: designation, month, day, hired_id, can_image, company_image and
#: ask_me_later_at -- and every VALUE is invented. The ids are repdigit runs on
#: purpose: nothing in this file may look like one of his real record keys.
HIRED_ID = "7777777"
POPULATED_MODAL = {
    "data": [
        {
            "hired_id": HIRED_ID,
            "rec_name": "A Recruiter",
            "company_name": "Northwind Robotics",
            "designation": "Senior Backend Engineer",
            "month": "August",
            "day": 11,
            "can_image": "https://example.invalid/candidate.png",
            "company_image": "https://example.invalid/employer.png",
            "ask_me_later_at": None,
        }
    ]
}

#: The uri is the platform's own handle, so this file never assembles one in a
#: tool call -- it reads it back out of the offer, exactly as a caller would.
RATING_URI = "/api/v1/candidate_opportunities/candidate_matching/8888888"
RATING_OFFERED = {
    "show_modal": True,
    "data": {"resource_uri": RATING_URI, "asked_before": False},
}
RATING_OFFERED_ASKED_BEFORE = {
    "show_modal": True,
    "data": {"resource_uri": RATING_URI, "asked_before": True},
}

CSRF = "csrf-for-the-leaderboard-writes"

LAPSED = json_response({"logged_out": True}, status=401)


class RequestRecorder:
    """A route that records METHOD, PATH, QUERY and BODY, and answers.

    ``BodyRecorder`` in ``test_writes`` records only the body, and on this
    cluster that would miss the finding: ``submit_rating`` declares
    action-level Angular params, so half of what it sends is in the query
    string. A recorder that could not see the query would certify a request
    that was half reproduced.
    """

    def __init__(self, response=None, *, expect="POST"):
        self.response = {"success": True} if response is None else response
        self.expect = expect
        self.calls: list = []

    def __call__(self, request):
        if request.method != self.expect:
            raise AssertionError(
                "route expected %s, received %s" % (self.expect, request.method)
            )
        body = json.loads(request.content) if request.content else None
        self.calls.append(
            {
                "method": request.method,
                "path": request.url.path,
                "query": dict(request.url.params),
                "body": body,
            }
        )
        return self.response

    @property
    def only(self) -> dict:
        assert len(self.calls) == 1, "expected exactly one call, saw %d" % len(self.calls)
        return self.calls[0]


def leaderboard_client(
    *,
    modal=LIVE_EMPTY_MODAL,
    queue=LIVE_EMPTY_QUEUE,
    offer=LIVE_NOTHING_OFFERED,
    csrf=CSRF,
    send=None,
):
    """A client wired for the three reads, and no write route unless asked.

    Taxonomy is unwired for the same reason every other write file unwires it:
    nothing in this cluster has any business resolving a location, so a stray
    taxonomy read is a loud "Unmocked request" rather than a silent success.
    The write routes are absent by default, so a write that escaped a gate
    lands on an unmocked path and is recorded as a fact rather than swallowed.
    """
    routes = {
        # candidate_id is recovered off the education collection, exactly as
        # the package recovers it, so show_verify_modal's id parameter is a
        # real derivation here rather than a constant typed twice.
        C.EP_EDUCATION: EDUCATION,
        C.EP_VERIFY_HIRED_SHOW_MODAL: modal,
        C.EP_VERIFY_HIRED: queue,
        C.EP_CANDIDATE_RATING_INFO: offer,
    }
    routes.update(send or {})
    client = make_client(routes, with_taxonomy=False)
    if csrf:
        client.http.cookies.set("csrftoken", csrf, domain="www.instahyre.com")
    return client


def writer_for(client) -> Writer:
    return Writer(client.http, client.store, client.inbound, client.inbox)


def detonating_writer_for(client):
    """``(writer, http)`` whose writer explodes on any write verb.

    Reads go through the genuine mocked client underneath, so the three reads a
    preview makes are real reads and the recorded request list stays the true
    record of the wire.
    """
    http = NoWriteHTTP(client.http)
    return Writer(http, client.store, client.inbound, client.inbox), http


# ===========================================================================
# 1. THE READ -- nothing pending is a RESULT
# ===========================================================================


def test_an_empty_channel_reports_nothing_pending_as_a_positive_result():
    """The headline claim, and the reason this tool exists in this shape.

    A caller asks this because a terminal status change might be waiting. The
    one answer that would cost him that is "I could not tell" wearing the
    clothes of "nothing". So the empty case is required to be UNAMBIGUOUS: a
    boolean that is False rather than absent, a count that is 0 rather than
    missing, and a sentence that says nothing is pending in words.
    """
    result = writer_for(leaderboard_client()).pending_requests()

    assert result["anything_pending"] is False
    assert result["pending_count"] == 0
    assert "NOTHING PENDING" in result["summary"]
    assert "not asking him anything" in result["summary"]


def test_the_empty_result_is_not_an_empty_dict():
    """An empty dict and a clean read are different facts.

    The failure this guards is a tool that returns ``{}`` when nothing is
    pending: a caller cannot tell that from a tool that fell over, and this
    package has been bitten before by a path that answered a problem with an
    innocent empty value.
    """
    result = writer_for(leaderboard_client()).pending_requests()

    assert result, "an empty channel must still return a populated answer"
    assert set(result) >= {
        "anything_pending",
        "pending_count",
        "summary",
        "hire_checks",
        "hire_verification_queue",
        "opportunity_rating",
        "empty_is_a_result_not_an_error",
    }
    assert "never stands in for" in result["empty_is_a_result_not_an_error"]


def test_the_empty_read_raises_nothing():
    """Stated as its own assertion because the opposite is a tempting design.

    An endpoint answering "no" is not an error condition, and a tool that
    raised on an empty channel would train its caller to stop calling it --
    which on this particular channel means never noticing a hire check.
    """
    writer_for(leaderboard_client()).pending_requests()


@pytest.mark.parametrize(
    "route",
    [C.EP_VERIFY_HIRED_SHOW_MODAL, C.EP_VERIFY_HIRED, C.EP_CANDIDATE_RATING_INFO],
)
def test_a_read_that_fails_raises_instead_of_reporting_nothing_pending(route):
    """THE MIRROR OF THE HEADLINE CLAIM, and the more important half.

    "Nothing pending" is only worth anything if a broken read cannot produce
    it. Each of the three routes is failed in turn with a lapsed session, and
    each time the typed error has to reach the caller rather than being
    absorbed into a cheerful empty answer.
    """
    routes = {
        C.EP_VERIFY_HIRED_SHOW_MODAL: LIVE_EMPTY_MODAL,
        C.EP_VERIFY_HIRED: LIVE_EMPTY_QUEUE,
        C.EP_CANDIDATE_RATING_INFO: LIVE_NOTHING_OFFERED,
        route: LAPSED,
    }
    client = leaderboard_client(
        modal=routes[C.EP_VERIFY_HIRED_SHOW_MODAL],
        queue=routes[C.EP_VERIFY_HIRED],
        offer=routes[C.EP_CANDIDATE_RATING_INFO],
    )

    with pytest.raises(InstahyreError):
        writer_for(client).pending_requests()


def test_the_read_covers_all_three_routes_in_one_call():
    """The whole point of a single tool over three endpoints.

    A caller who had to know three route names in advance would never have
    asked the question, which is precisely how a hire check sits unanswered.
    Pinned by name so dropping one is a visible edit rather than a quiet
    narrowing of the channel.
    """
    client = leaderboard_client()
    writer_for(client).pending_requests()

    paths = client.routes.paths
    assert C.EP_VERIFY_HIRED_SHOW_MODAL in paths
    assert C.EP_VERIFY_HIRED in paths
    assert C.EP_CANDIDATE_RATING_INFO in paths


def test_the_read_issues_no_write_of_any_kind():
    """It is a read. Asserted on the wire rather than on the docstring."""
    client = leaderboard_client()
    writer, http = detonating_writer_for(client)

    writer.pending_requests()

    assert describe(write_requests(client)) == []
    assert http.attempts == []


def test_the_modal_read_carries_the_candidate_id_in_the_query_string():
    """``show_verify_modal({id:$scope.candidateId})``, reproduced.

    The id is the CANDIDATE's, not a hire's, and it is recovered the way the
    package recovers it everywhere else rather than typed in. A read that
    dropped it would be a different request than the one the browser makes.
    """
    client = leaderboard_client()
    writer_for(client).pending_requests()

    modal_requests = [
        r for r in client.routes.requests if r.url.path.endswith("show_verify_modal")
    ]
    assert len(modal_requests) == 1
    assert dict(modal_requests[0].url.params) == {
        "id": str(client.inbound.candidate_id())
    }


def test_a_populated_channel_names_the_hire_check_rather_than_counting_it():
    """A count is not an answer on a channel that asks about employment.

    The caller has to see WHICH company is being asked about before answering,
    so the company, the role and the recruiter are required in the read
    itself -- not left to a second call nobody makes.
    """
    result = writer_for(
        leaderboard_client(modal=POPULATED_MODAL, offer=RATING_OFFERED)
    ).pending_requests()

    assert result["anything_pending"] is True
    assert result["pending_count"] == 2
    check = result["hire_checks"]["pending"][0]
    assert check["hired_id"] == HIRED_ID
    assert check["company"] == "Northwind Robotics"
    assert check["designation"] == "Senior Backend Engineer"
    assert check["recruiter"] == "A Recruiter"
    assert result["opportunity_rating"]["pending"] is True
    assert result["opportunity_rating"]["rating_uri"] == RATING_URI


def test_the_read_does_not_echo_the_two_image_urls():
    """Named in the shipped reader, deliberately not returned.

    ``can_image`` is a URL to his own photograph and ``company_image`` is a
    logo; neither helps anyone decide how to answer, and echoing a personal
    asset URL into a tool result buys nothing. Their PRESENCE is reported,
    because that says something about the payload, and their values are not.
    """
    result = writer_for(leaderboard_client(modal=POPULATED_MODAL)).pending_requests()
    check = result["hire_checks"]["pending"][0]

    assert check["has_candidate_image"] is True
    assert check["has_company_image"] is True
    assert "example.invalid" not in json.dumps(result)


def test_an_absent_data_key_is_reported_apart_from_an_empty_one():
    """Instahyre's own reader treats them differently, so this does too.

    ``if(response.data===undefined){return;}`` -- a payload with no ``data``
    key is a shape the site declines to act on, while ``data: []`` is a
    definite "nothing pending". They arrive looking identical to anything that
    only counts, and only one of them answered the question.
    """
    empty = writer_for(leaderboard_client()).pending_requests()
    absent = writer_for(leaderboard_client(modal={})).pending_requests()

    assert empty["hire_checks"]["data_key_present"] is True
    assert absent["hire_checks"]["data_key_present"] is False
    assert empty["anything_pending"] is False
    assert absent["anything_pending"] is False


def test_the_collection_read_carries_its_truncation_fact():
    """An empty list and a truncated read are opposite facts.

    They arrive looking identical, and this cluster is one where the wrong
    reading is expensive: "no hire checks" and "the first page held none" are
    not the same sentence.
    """
    truncated = dict(LIVE_EMPTY_QUEUE)
    truncated["meta"] = dict(LIVE_EMPTY_QUEUE["meta"], total_count=3)

    result = writer_for(leaderboard_client(queue=truncated)).pending_requests()

    assert result["hire_verification_queue"]["complete"] is False
    assert result["hire_verification_queue"]["total_reported"] == 3


def test_the_read_says_it_has_never_seen_this_channel_populated():
    """The honesty rail. Every payload shape in this cluster is read out of
    shipped JavaScript, not out of a response anybody has seen, and a caller
    weighing what it returns is entitled to know that."""
    result = writer_for(leaderboard_client()).pending_requests()

    assert "2026-08-25" in result["never_seen_populated"]
    assert "never been read non-empty" in result["never_seen_populated"]


# ===========================================================================
# 2. THE GATE THAT FIRES TODAY -- an empty live read refuses every write
# ===========================================================================


def test_every_write_refuses_today_because_the_live_channel_is_empty():
    """THE MOST IMPORTANT TEST IN THIS FILE, and the one that is worth having
    precisely because it currently passes for a reason nobody arranged.

    Both writes validate their id against a live re-read. Both live reads are
    empty on this account -- measured, 2026-08-25, 200 on all three routes --
    so both writes refuse every call that could be made today. This is the
    gate working, not a defect and not a gap in coverage, and the property is
    asserted directly so that a future change which quietly stopped
    revalidating would fail here rather than start sending.
    """
    writer, http = detonating_writer_for(leaderboard_client())

    with pytest.raises(NotFound):
        writer.answer_hire_check(HIRED_ID, 0, confirm=True)
    with pytest.raises(NotFound):
        writer.rate_opportunity(RATING_URI, 5, confirm=True)

    assert http.attempts == [], "a refused write still reached the wire"


# ===========================================================================
# 3. WRITE ONE -- answering a hire check
# ===========================================================================


def test_answering_without_confirm_issues_no_write_at_all():
    """The default is a preview and the preview is INERT.

    Asserted on the wire, not on the return value: a check that only read the
    dict would pass against an implementation that previewed AND sent.
    """
    client = leaderboard_client(modal=POPULATED_MODAL)
    writer, http = detonating_writer_for(client)

    preview = writer.answer_hire_check(HIRED_ID, 0)

    assert preview["confirmed"] is False
    assert describe(write_requests(client)) == []
    assert http.attempts == []


def test_the_preview_shows_the_exact_request_that_would_be_sent():
    client = leaderboard_client(modal=POPULATED_MODAL)

    preview = writer_for(client).answer_hire_check(HIRED_ID, 0)
    would = preview["would_send"]

    assert would["method"] == "POST"
    assert would["url"] == C.API_BASE + C.EP_VERIFY_HIRED_SUBMIT_RESPONSE
    assert would["json_body"] == {"id": HIRED_ID, "choice": 0}
    assert would["query_string"] == {"id": HIRED_ID}


def test_the_preview_names_the_company_being_answered_about():
    """A confirm given against an id is a confirm given against nothing.

    This is an employment outcome. The company and the role have to be in
    front of the person pressing send, or the consent the gate obtained is
    consent to a number.
    """
    preview = writer_for(
        leaderboard_client(modal=POPULATED_MODAL)
    ).answer_hire_check(HIRED_ID, 0)

    assert preview["answering"]["company"] == "Northwind Robotics"
    assert preview["answering"]["designation"] == "Senior Backend Engineer"


def test_a_fabricated_hired_id_is_refused_by_name():
    """THE LIVE-VALIDATION GATE. An id that the channel is not currently
    offering cannot be submitted, and the refusal names it rather than failing
    silently or sending anyway."""
    client = leaderboard_client(modal=POPULATED_MODAL)
    writer, http = detonating_writer_for(client)

    with pytest.raises(NotFound) as excinfo:
        writer.answer_hire_check("1234321", 0, confirm=True)

    assert "1234321" in str(excinfo.value)
    assert http.attempts == []


def test_the_refusal_says_the_channel_is_empty_when_it_is():
    """A refusal that reads like a rejected id, when the truth is that nothing
    is pending, teaches the wrong lesson. The two cases are told apart."""
    writer = writer_for(leaderboard_client())

    with pytest.raises(NotFound) as excinfo:
        writer.answer_hire_check(HIRED_ID, 0, confirm=True)

    assert "EMPTY" in str(excinfo.value)


def test_the_modal_is_re_read_for_the_check_rather_than_remembered():
    """A remembered list is a gate that stops guarding the moment anything
    changes underneath it. Each call re-reads."""
    client = leaderboard_client(modal=POPULATED_MODAL)
    writer = writer_for(client)

    writer.answer_hire_check(HIRED_ID, 0)
    first = client.routes.count(C.EP_VERIFY_HIRED_SHOW_MODAL)
    writer.answer_hire_check(HIRED_ID, 0)
    second = client.routes.count(C.EP_VERIFY_HIRED_SHOW_MODAL)

    assert first >= 1 and second > first


def test_a_confirmed_answer_sends_the_captured_body_and_the_captured_query():
    """The body from both shipped callers, and the id repeated in the query
    because the resource declares ``{id:'@id'}``. Both halves, measured on the
    wire rather than read off the preview."""
    recorder = RequestRecorder()
    client = leaderboard_client(
        modal=POPULATED_MODAL,
        send={C.EP_VERIFY_HIRED_SUBMIT_RESPONSE: recorder},
    )

    result = writer_for(client).answer_hire_check(HIRED_ID, 0, confirm=True)

    assert result["confirmed"] is True
    sent = recorder.only
    assert sent["method"] == "POST"
    assert sent["body"] == {"id": HIRED_ID, "choice": 0}
    assert sent["query"] == {"id": HIRED_ID}
    assert sorted(sent["body"]) == sorted(C.HIRE_RESPONSE_BODY_KEYS)


def test_the_answer_goes_to_the_named_path_and_no_other():
    recorder = RequestRecorder()
    client = leaderboard_client(
        modal=POPULATED_MODAL,
        send={C.EP_VERIFY_HIRED_SUBMIT_RESPONSE: recorder},
    )

    writer_for(client).answer_hire_check(HIRED_ID, 0, confirm=True)

    posted = [r.url.path for r in client.routes.requests if r.method == "POST"]
    assert posted == [API_PREFIX + C.EP_VERIFY_HIRED_SUBMIT_RESPONSE]


def test_answering_without_a_csrf_token_refuses():
    client = leaderboard_client(modal=POPULATED_MODAL, csrf=None)
    writer, http = detonating_writer_for(client)

    with pytest.raises(ConfirmationRequired):
        writer.answer_hire_check(HIRED_ID, 0, confirm=True)

    assert http.attempts == []


def test_the_preview_refuses_to_give_choice_a_meaning_it_has_not_measured():
    """The finding this cluster turned up, kept in front of the caller.

    Only ``choice=0`` has a shipped caller. ``setCandidateChoice`` produces
    every other value and is called nowhere in any captured bundle, so which
    integer means "yes, I was hired" is NOT known. A preview that offered a
    plausible mapping would be a guess wearing the clothes of a contract.
    """
    preview = writer_for(
        leaderboard_client(modal=POPULATED_MODAL)
    ).answer_hire_check(HIRED_ID, 3)

    assert preview["choice"] == 3
    assert preview["choice_is_the_measured_dismiss_value"] is False
    assert "will not guess" in preview["choice_meaning"]
    assert "setCandidateChoice" in preview["choice_meaning"]

    dismiss = writer_for(
        leaderboard_client(modal=POPULATED_MODAL)
    ).answer_hire_check(HIRED_ID, C.HIRE_CHOICE_DISMISS)
    assert dismiss["choice_is_the_measured_dismiss_value"] is True


def test_a_boolean_choice_is_refused_rather_than_widened_to_one():
    """``True`` would become ``choice=1`` -- a value whose meaning is exactly
    what is not known here. Refusing costs a round trip; widening spends an
    irreversible answer on a guess."""
    writer = writer_for(leaderboard_client(modal=POPULATED_MODAL))

    with pytest.raises(NothingToDo):
        writer.answer_hire_check(HIRED_ID, True)


@pytest.mark.parametrize("bad", ["2", 2.0, None, [2]])
def test_a_non_integer_choice_is_refused(bad):
    writer = writer_for(leaderboard_client(modal=POPULATED_MODAL))

    with pytest.raises(NothingToDo):
        writer.answer_hire_check(HIRED_ID, bad)


def test_the_outcome_is_read_back_from_state_not_from_the_response():
    """Nobody has ever seen this endpoint's reply, so its shape is not
    evidence. The modal is: an answered check should stop being offered."""
    recorder = RequestRecorder()

    class DropsTheCheck:
        """Answers the modal populated, then empty once the answer is sent."""

        def __init__(self):
            self.answered = False

        def __call__(self, request):
            if self.answered:
                return LIVE_EMPTY_MODAL
            return POPULATED_MODAL

    modal = DropsTheCheck()

    def record_and_drop(request):
        modal.answered = True
        return recorder(request)

    client = leaderboard_client(
        modal=modal, send={C.EP_VERIFY_HIRED_SUBMIT_RESPONSE: record_and_drop}
    )

    result = writer_for(client).answer_hire_check(HIRED_ID, 0, confirm=True)

    assert result["verification"]["ok"] is True
    assert result["verification"]["still_offered"] is False


def test_a_check_still_offered_after_the_answer_warns_and_does_not_advise_resending():
    recorder = RequestRecorder()
    client = leaderboard_client(
        modal=POPULATED_MODAL,
        send={C.EP_VERIFY_HIRED_SUBMIT_RESPONSE: recorder},
    )

    result = writer_for(client).answer_hire_check(HIRED_ID, 0, confirm=True)

    assert result["verification"]["ok"] is False
    assert "Do NOT re-send" in result["verification"]["warning"]


# ===========================================================================
# 4. WRITE TWO -- rating an opportunity, and where its fields go
# ===========================================================================


def test_rating_without_confirm_issues_no_write_at_all():
    client = leaderboard_client(offer=RATING_OFFERED)
    writer, http = detonating_writer_for(client)

    preview = writer.rate_opportunity(RATING_URI, 5)

    assert preview["confirmed"] is False
    assert describe(write_requests(client)) == []
    assert http.attempts == []


def test_the_three_fields_ride_the_query_string_AND_the_body():
    """THE DETAIL THAT WOULD OTHERWISE BE A GUESSED REQUEST.

    ``submit_rating`` declares ``params:{rating_uri:'@rating_uri',
    ask_later:'@ask_later', rating:'@rating'}`` on the ACTION. In AngularJS
    those are URL parameters: any key that is not a ``:name`` placeholder in
    the action's url is copied into the request's params and serialized into
    the QUERY STRING. Neither url here has a placeholder. And because the
    method is POST, the same object is ALSO the request data, with the
    ``@``-params extracted back out of it -- so both halves go, and a
    reproduction of either half alone would be a request the browser never
    makes.

    Measured on the wire, both halves in one assertion, because the failure
    worth catching is a change that quietly drops one of them.
    """
    recorder = RequestRecorder()
    client = leaderboard_client(
        offer=RATING_OFFERED, send={C.EP_CANDIDATE_RATING_SUBMIT: recorder}
    )

    writer_for(client).rate_opportunity(RATING_URI, 4, confirm=True)

    sent = recorder.only
    assert sent["body"] == {"rating_uri": RATING_URI, "ask_later": False, "rating": 4}
    assert sent["query"] == {
        "rating_uri": RATING_URI,
        "ask_later": "false",
        "rating": "4",
    }
    assert sorted(sent["body"]) == sorted(C.RATING_BODY_KEYS)
    assert sorted(sent["query"]) == sorted(C.RATING_QUERY_KEYS)


def test_the_ask_later_branch_sends_a_null_rating_in_the_body_and_drops_it_from_the_query():
    """``$scope.rating`` is null until a star is clicked, and the defer branch
    sends it that way. Angular's own parameter serializer omits a null from the
    query while ``$http`` keeps it in the JSON body, and this client does the
    same -- so the two halves legitimately differ here, and that difference is
    pinned rather than tidied."""
    recorder = RequestRecorder()
    client = leaderboard_client(
        offer=RATING_OFFERED, send={C.EP_CANDIDATE_RATING_SUBMIT: recorder}
    )

    writer_for(client).rate_opportunity(RATING_URI, None, ask_later=True, confirm=True)

    sent = recorder.only
    assert sent["body"] == {
        "rating_uri": RATING_URI,
        "ask_later": True,
        "rating": None,
    }
    assert sent["query"] == {"rating_uri": RATING_URI, "ask_later": "true"}
    assert "rating" not in sent["query"]

    # AND THE PREVIEW HAS TO SAY THE SAME THING. A preview that printed a
    # rating in a query string the wire will not carry would be describing a
    # request nobody sends -- on the surface whose entire gate is the preview.
    preview = writer_for(leaderboard_client(offer=RATING_OFFERED)).rate_opportunity(
        RATING_URI, None, ask_later=True
    )
    assert "rating" not in preview["would_send"]["query_string"]
    assert preview["would_send"]["json_body"]["rating"] is None


def test_a_rating_submission_with_no_rating_is_refused__THEIR_RULE():
    """Instahyre's own guard, reproduced and labelled as theirs:
    ``if(!ask_later && $scope.rating==null){showRatingError=true;return;}``."""
    writer = writer_for(leaderboard_client(offer=RATING_OFFERED))

    with pytest.raises(NothingToDo) as excinfo:
        writer.rate_opportunity(RATING_URI, None)

    assert "their rule reproduced" in str(excinfo.value)


def test_a_second_ask_later_is_refused__THEIR_RULE():
    """``if(ask_later && $scope.opportunity_info.asked_before){return;}`` --
    read off the LIVE payload rather than remembered, because a remembered
    flag is a guard that resets when the caller does."""
    client = leaderboard_client(offer=RATING_OFFERED_ASKED_BEFORE)
    writer, http = detonating_writer_for(client)

    with pytest.raises(NothingToDo) as excinfo:
        writer.rate_opportunity(RATING_URI, None, ask_later=True, confirm=True)

    assert "asked_before" in str(excinfo.value)
    assert http.attempts == []


@pytest.mark.parametrize("bad", [0, 6, -1, 100])
def test_a_rating_outside_one_to_five_is_refused(bad):
    """The bounds are read off the controller, not guessed from a widget."""
    writer = writer_for(leaderboard_client(offer=RATING_OFFERED))

    with pytest.raises(NothingToDo):
        writer.rate_opportunity(RATING_URI, bad)


@pytest.mark.parametrize("bad", ["4", 4.0, True, [4]])
def test_a_non_integer_rating_is_refused(bad):
    writer = writer_for(leaderboard_client(offer=RATING_OFFERED))

    with pytest.raises(NothingToDo):
        writer.rate_opportunity(RATING_URI, bad)


def test_a_fabricated_rating_uri_is_refused_by_name():
    """THE LIVE-VALIDATION GATE on this surface. The offer names one uri; any
    other is a judgement aimed at an employer nobody meant to judge."""
    client = leaderboard_client(offer=RATING_OFFERED)
    writer, http = detonating_writer_for(client)

    with pytest.raises(NotFound) as excinfo:
        writer.rate_opportunity("/api/v1/candidate_opportunities/x/9999999", 5, confirm=True)

    assert RATING_URI in str(excinfo.value)
    assert http.attempts == []


def test_a_rating_is_refused_outright_when_nothing_is_offered():
    """``show_modal: false`` is the live state on this account, and it means
    there is nothing to rate -- said plainly rather than as a rejected uri."""
    client = leaderboard_client(offer=LIVE_NOTHING_OFFERED)
    writer, http = detonating_writer_for(client)

    with pytest.raises(NotFound) as excinfo:
        writer.rate_opportunity(RATING_URI, 5, confirm=True)

    assert "not currently offering" in str(excinfo.value)
    assert http.attempts == []


def test_the_offer_is_re_read_for_the_check_rather_than_remembered():
    client = leaderboard_client(offer=RATING_OFFERED)
    writer = writer_for(client)

    writer.rate_opportunity(RATING_URI, 5)
    first = client.routes.count(C.EP_CANDIDATE_RATING_INFO)
    writer.rate_opportunity(RATING_URI, 5)
    second = client.routes.count(C.EP_CANDIDATE_RATING_INFO)

    assert first >= 1 and second > first


def test_rating_without_a_csrf_token_refuses():
    client = leaderboard_client(offer=RATING_OFFERED, csrf=None)
    writer, http = detonating_writer_for(client)

    with pytest.raises(ConfirmationRequired):
        writer.rate_opportunity(RATING_URI, 5, confirm=True)

    assert http.attempts == []


def test_the_rating_verification_does_not_call_a_still_offered_ask_later_a_failure():
    """The honest limit, stated in the result rather than hidden by it.

    Deferring is not answering, so an ask-later that succeeded may leave the
    same uri on offer. ``still_offered`` is reported as a fact and the reading
    is spelled out, instead of a green tick that would be wrong or a red one
    that would be alarming.
    """
    recorder = RequestRecorder()
    client = leaderboard_client(
        offer=RATING_OFFERED, send={C.EP_CANDIDATE_RATING_SUBMIT: recorder}
    )

    result = writer_for(client).rate_opportunity(
        RATING_URI, None, ask_later=True, confirm=True
    )

    assert result["verification"]["still_offered"] is True
    assert "ASK LATER may legitimately still be offered" in result["verification"]["how"]


# ===========================================================================
# 5. THE DOOR -- a named allowlist of two, and what is deliberately not on it
# ===========================================================================


def test_the_allowlist_is_exactly_the_two_named_paths():
    """Pinned to literal strings, so repointing a constant fails HERE rather
    than following the constant into the guard."""
    assert C.SENDABLE_LEADERBOARD_PATHS == frozenset(
        {
            "/leaderboard/verify_hired_candidate/submit_response",
            "/leaderboard/candidate_rating/submit_rating",
        }
    )


def test_the_collection_url_is_not_sendable_which_is_what_blocks_add_joining_date():
    """THE MEMBER THIS SET DOES NOT HAVE IS THE POINT.

    ``add_joining_date`` POSTs to the SAME url the collection GET reads, so no
    rule about the path could admit the read and refuse the write. An
    allowlist can, because it is asked about an exact value. And it must:
    that action's name occurs exactly once in all ten captured bundles -- the
    factory declaration -- with no caller anywhere, so nothing states what its
    body would be.
    """
    assert C.EP_VERIFY_HIRED not in C.SENDABLE_LEADERBOARD_PATHS

    with pytest.raises(NotSendable):
        writes_module._guard_leaderboard_sendable(C.EP_VERIFY_HIRED)


def test_the_three_allowlists_each_refuse_the_others_members():
    """Three small enumerated sets that cannot reach each other beat one large
    set in which a bug on any surface spends every surface's permissions."""
    for path in C.SENDABLE_LEADERBOARD_PATHS:
        assert path not in C.SENDABLE_INBOX_PATHS
        assert path not in C.SENDABLE_BULK_APPLY_PATHS
        with pytest.raises(NotSendable):
            writes_module._guard_sendable(path)
        with pytest.raises(NotSendable):
            writes_module._guard_bulk_apply_sendable(path)

    for path in C.SENDABLE_INBOX_PATHS | C.SENDABLE_BULK_APPLY_PATHS:
        with pytest.raises(NotSendable):
            writes_module._guard_leaderboard_sendable(path)


def test_no_module_posts_to_the_collection_url():
    """The census, aimed at this cluster. ``add_joining_date`` would be a POST
    at ``EP_VERIFY_HIRED``; there is none, and the package-wide POST census in
    ``tests/test_inbound_safety.py`` enumerates the targets that do exist."""
    from test_inbound_safety import package_sources, post_call_sites

    targets = {target for _, _, target in post_call_sites(package_sources())}
    assert "C.EP_VERIFY_HIRED" not in targets
    assert "C.EP_VERIFY_HIRED_SUBMIT_RESPONSE" in targets
    assert "C.EP_CANDIDATE_RATING_SUBMIT" in targets


def test_the_ask_me_later_patch_is_recorded_and_unbuilt():
    """A caller EXISTS for it, so its body is known and written down --
    ``verifyHiredCandidateService.update({id:hired_id, ask_me_later:true})``.
    It stays unbuilt because it postpones a question nobody is being asked,
    and the record is what stops the next session re-deriving it."""
    assert C.VERIFY_HIRED_ASK_ME_LATER_METHOD == "PATCH"
    assert C.VERIFY_HIRED_ASK_ME_LATER_BODY_KEYS == ("id", "ask_me_later")
    assert not hasattr(Writer, "ask_me_later")

    from test_inbound_safety import package_sources, receiver_is_the_http_client
    import ast

    for name, text in package_sources().items():
        for node in ast.walk(ast.parse(text, filename=name)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "patch"
                and receiver_is_the_http_client(node.func.value)
            ):
                first = node.args[0] if node.args else None
                spelled = ast.unparse(first) if first is not None else ""
                assert "VERIFY_HIRED" not in spelled, (
                    "%s issues a PATCH at the verify-hired resource; the "
                    "ask-me-later action is recorded as UNBUILT" % name
                )


def test_add_joining_date_records_that_it_has_no_caller():
    """Recorded as prose rather than as a path tripwire, and that choice is
    the finding: its path IS the read's path, so a fragment-shaped guard would
    have to ban the read to name the write."""
    reason = C.VERIFY_HIRED_ADD_JOINING_DATE_HAS_NO_CALLER
    assert "Nothing calls it" in reason
    assert "SENDABLE_LEADERBOARD_PATHS" in reason


# ===========================================================================
# 6. THE MCP SURFACE
# ===========================================================================


@pytest.mark.parametrize(
    "tool",
    [server_module.instahyre_answer_hire_check, server_module.instahyre_rate_opportunity],
)
def test_both_write_tools_default_confirm_to_false(tool):
    assert inspect.signature(tool).parameters["confirm"].default is False


def test_the_read_tool_has_no_confirm_and_takes_no_arguments():
    """It reads. A confirm on a read would teach a caller that reading is
    dangerous here, and an argument would imply there is something to choose."""
    assert inspect.signature(server_module.instahyre_pending_requests).parameters == {}


def test_the_write_tools_carry_their_evidence_class_into_the_preview():
    """SHIPPED, not WIRE, and the caller is told which. A body read out of a
    factory and a body recorded off the wire are different things, and this
    package keeps them apart all the way to the surface."""
    hire = writer_for(leaderboard_client(modal=POPULATED_MODAL)).answer_hire_check(
        HIRED_ID, 0
    )
    rating = writer_for(leaderboard_client(offer=RATING_OFFERED)).rate_opportunity(
        RATING_URI, 5
    )

    for preview in (hire, rating):
        assert preview["contract"]["evidence_class"] == C.CONTRACT_SHIPPED
        assert preview["contract"]["captured"] == "2026-08-25"
        assert "never been serialized" in preview["contract"]["what_that_means"]


def test_both_previews_say_the_surface_has_never_been_run_live():
    hire = writer_for(leaderboard_client(modal=POPULATED_MODAL)).answer_hire_check(
        HIRED_ID, 0
    )
    rating = writer_for(leaderboard_client(offer=RATING_OFFERED)).rate_opportunity(
        RATING_URI, 5
    )

    assert "ever been sent by this server" in hire["never_run_live"]
    assert "ever been sent by this server" in rating["never_run_live"]


# ===========================================================================
# 7. THE CONTROLS -- each instrument shown discriminating
# ===========================================================================


def test_the_no_write_assertion_can_actually_fail__CONTROL():
    """``describe(write_requests(client)) == []`` is the load-bearing check in
    four tests above. A route table that recorded nothing would make all four
    pass while a write went out, so it is shown catching one."""
    recorder = RequestRecorder()
    client = leaderboard_client(
        modal=POPULATED_MODAL, send={C.EP_VERIFY_HIRED_SUBMIT_RESPONSE: recorder}
    )

    writer_for(client).answer_hire_check(HIRED_ID, 0, confirm=True)

    assert describe(write_requests(client)) != []


def test_the_recorder_sees_a_query_string__CONTROL():
    """The query half of the central finding rests entirely on this recorder.
    A recorder blind to the query would certify a half-reproduced request as
    correct, so it is shown reading one."""
    recorder = RequestRecorder()
    client = leaderboard_client(
        offer=RATING_OFFERED, send={C.EP_CANDIDATE_RATING_SUBMIT: recorder}
    )

    writer_for(client).rate_opportunity(RATING_URI, 2, confirm=True)

    assert recorder.only["query"], "the recorder reported no query string at all"
    assert recorder.only["query"] != recorder.only["body"], (
        "the recorder is echoing the body as the query; it is not reading the url"
    )


def test_the_detonating_writer_really_detonates__CONTROL():
    """``http.attempts == []`` appears in six refusal tests. A double that had
    stopped raising would make every one of them vacuous.

    ``WriteAttempted`` derives from ``BaseException`` on purpose -- an alarm
    that ``except Exception`` could mute is an alarm with a mute button --
    so it is caught by name here rather than as an ordinary error.
    """
    client = leaderboard_client()
    writer, http = detonating_writer_for(client)

    with pytest.raises(WriteAttempted):
        http.post(C.EP_CANDIDATE_RATING_SUBMIT, json_body={})

    assert http.attempts, "the double recorded no attempt"


def test_a_populated_modal_really_does_populate__CONTROL():
    """Every refusal test above runs against an EMPTY channel by default. If
    the populated payload did not populate, the tests that are supposed to
    reach a preview would be passing for the wrong reason -- refused, not
    allowed."""
    result = writer_for(leaderboard_client(modal=POPULATED_MODAL)).pending_requests()

    assert result["anything_pending"] is True
    assert result["hire_checks"]["count"] == 1
