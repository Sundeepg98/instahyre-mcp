"""The three inbox writes admitted on 2026-08-25, held to their contracts.

WHY THIS IS A SEPARATE FILE. ``test_writes.py`` is the argument for the write
tier as a whole -- the confirm gate, the recipient preview, the referral
consent problem -- and the carve-out assertions that say how wide the inbox
door is still live there, because that is where the door is described. What
lives here is narrower and has its own hazards, and each of the three is a
hazard the other write surfaces do not have:

1. **A GET THAT MUTATES.** ``mark_all_read`` is declared
   ``{method:'GET',url:url+"mark_all_read"}`` and clears unread state across
   the whole inbox. Every instrument in this repo that watches for writes
   watches for POST/PUT/PATCH/DELETE, so this surface is INVISIBLE to all of
   them -- ``test_every_post_call_site_in_the_package_targets_a_measured_
   endpoint`` cannot see it by construction. A census shaped the other way is
   therefore not optional here, and it is in this file.

2. **A BODY FIELD THAT IS A RESOURCE URI.** ``toggle_message_read`` names the
   conversation by ``conv.resource_uri``, not by the integer id every other
   inbox call uses. A URI assembled from an id would look right in a preview
   and be a different request on the wire, so the assertion is that the value
   sent is the string the SERVER supplied, character for character.

3. **A KEY THAT IS NOT THE ARGUMENT NAME.** The star body's field is
   ``star_conv``. ``starred`` is what the RESPONSE carries back, and no shipped
   caller sends a key by that name -- so a test that asserted on ``starred``
   would be certifying the misreading rather than the contract.

WHAT THIS FILE CANNOT CLAIM, and says so once here rather than hedging in every
docstring: **none of these three has ever run against live data.** His inbox
holds zero conversations (measured 2026-08-23, authenticated, 200). Every
fixture below with a conversation in it is HAND-BUILT from the frontend
contract, exactly as ``conversations_populated.json`` is and says it is. So
these tests measure that this package sends what Instahyre's own JavaScript
says it sends -- they do not and cannot measure what Instahyre does with it.

THE INSTRUMENTS ARE BORROWED ON PURPOSE. ``NoWriteHTTP``, ``BodyRecorder``,
``describe`` and ``write_requests`` come from ``test_writes.py`` rather than
being reimplemented, because they are already SHOWN DISCRIMINATING there --
``test_the_no_send_assertion_can_fail__CONTROL`` points the detonating double
at a deliberately ungated write and watches it fire. A re-implementation would
prove only that the copy works. The one instrument that could not be borrowed
is the one for the mutating GET, because nothing in this package had a reason
to build one before; it is defined below and has its own control.
"""

from __future__ import annotations

import ast
import copy
import inspect
import pathlib
import re

import httpx
import pytest

from conftest import fixture_json, make_client
from instahyre_server import constants as C
from instahyre_server import server as server_module
from instahyre_server import writes as writes_module
from instahyre_server.errors import InstahyreError, NotFound
from instahyre_server.inbox import MutatingPathRefused, guard_read_only
from instahyre_server.writes import NothingToDo, Writer
from test_writes import (
    BodyRecorder,
    NoWriteHTTP,
    WriteAttempted,
    describe,
    reply_job_routes,
    write_requests,
)

CONVERSATIONS = fixture_json("conversations_populated.json")
EDUCATION = fixture_json("education.json")
PROFILE = fixture_json("candidate_profile.json")
PROFILE_PATH = C.EP_PROFILE.format(
    candidate_id=int(re.search(r"/candidate/(\d+)", EDUCATION["objects"][0]["candidate"]).group(1))
)

#: Row 0: unread and UNSTARRED. Row 1: read and STARRED. Having both states in
#: the fixture is what lets every toggle below be tested in both directions --
#: a suite that only ever starred an unstarred thread would pass against an
#: implementation that ignores its argument and hardcodes True.
UNSTARRED_UNREAD = CONVERSATIONS["objects"][0]
STARRED_READ = CONVERSATIONS["objects"][1]
CONV_ID = UNSTARRED_UNREAD["id"]
CONV_URI = UNSTARRED_UNREAD["resource_uri"]
CONV_JOB_ID = UNSTARRED_UNREAD["job_id"]

#: HAND-BUILT, and it has to be. The live counts capture
#: (``conversation_counts.json``) reports zero unread, because his inbox is
#: empty -- and zero unread is precisely the state in which Instahyre's own
#: caller refuses to issue mark_all_read at all. So a fixture reporting a
#: non-zero count is the only way to exercise the path past that gate, and it
#: is invented rather than captured. Field names come from the shipped
#: ``markAllReadCallback``, which reads ``response.conv_count.unread``.
COUNTS_WITH_UNREAD = {
    "conv_count": {"unread": 1, "starred": 1, "starred_unread": 0},
    "success": True,
}
COUNTS_ALL_READ = {
    "conv_count": {"unread": 0, "starred": 1, "starred_unread": 0},
    "success": True,
}

#: What the site's own callback reads off the mark_all_read response.
MARK_ALL_READ_CLEARED = {"conv_count": {"unread": 0, "starred": 1, "starred_unread": 0}}


class GetRecorder:
    """A route that records the QUERY of a GET and answers with ``response``.

    ``BodyRecorder`` cannot stand in here and the reason is the point of this
    whole surface: it parses ``request.content`` as JSON, and this request has
    no content at all. A mutation carried entirely in a query string needs an
    instrument that looks at the query string.
    """

    def __init__(self, response=None):
        self.response = {"success": True} if response is None else response
        self.calls: list = []

    def __call__(self, request):
        if request.method != "GET":
            raise AssertionError(
                "route expected GET, received %s -- the captured contract for this "
                "action is a GET and nothing else" % request.method
            )
        self.calls.append(dict(request.url.params.multi_items()))
        return self.response


# ---------------------------------------------------------------------------
# The instrument the mutating GET needs, and its control
# ---------------------------------------------------------------------------


class MutatingGetAttempted(BaseException):
    """Raised the instant a GET aimed at a gated path reaches the client.

    ``BaseException`` for the same reason ``WriteAttempted`` is: the thing
    being tested is a gate, and an alarm that ordinary ``except Exception``
    handling could swallow is an alarm with a mute button on it.
    """


class NoMutatingGetHTTP(NoWriteHTTP):
    """``NoWriteHTTP``, plus: a GET to a SENDABLE path detonates.

    THE GAP THIS FILLS IS STRUCTURAL, not incidental. Every other gate check in
    this package watches the write VERBS, and ``mark_all_read`` is a GET -- so
    a preview that quietly issued it would look, to every existing instrument,
    exactly like a preview reading the conversation list. Ordinary reads still
    pass straight through, which is what makes this a discriminator rather than
    a blanket refusal: the previews below legitimately read the list and the
    counts, and those reads must succeed for the test to be testing anything.
    """

    def get(self, path, **kwargs):
        if path in C.SENDABLE_INBOX_PATHS:
            self.attempts.append(("GET", path, kwargs))
            raise MutatingGetAttempted(
                "GET %s -- a gated mutating path was requested." % path
            )
        return self.inner.get(path, **kwargs)


def test_the_mutating_get_double_fires_on_the_path_it_watches__CONTROL():
    """The control for the instrument above. Both halves are asserted: it must
    fire on the gated GET, and it must NOT fire on the ordinary reads the
    previews depend on -- a double that refused everything would make every
    "nothing was sent" result below vacuous."""
    client = inbox_write_client()
    http = NoMutatingGetHTTP(client.http)

    with pytest.raises(MutatingGetAttempted):
        http.get(C.EP_MARK_ALL_READ, params={"page_loaded_at": "x"})

    assert http.get(C.EP_CONVERSATIONS, params={"limit": 1, "offset": 0})
    assert http.attempts == [("GET", C.EP_MARK_ALL_READ, {"params": {"page_loaded_at": "x"}})]


# ---------------------------------------------------------------------------
# Client constructors
# ---------------------------------------------------------------------------


def inbox_write_client(
    *,
    conversations=CONVERSATIONS,
    counts=COUNTS_WITH_UNREAD,
    routes=None,
    csrf="csrf-for-the-inbox-write",
):
    """A client wired for the reads these writes make, and no write route.

    A write route is added only when a test asks for one, so a request that
    escaped a gate lands on an unmocked path and is recorded as an
    ``AssertionError`` rather than being quietly answered.
    """
    table = {
        C.EP_EDUCATION: EDUCATION,
        PROFILE_PATH: PROFILE,
        C.EP_CONVERSATIONS: conversations,
        C.EP_CONVERSATION_COUNT: counts,
    }
    table.update(reply_job_routes())
    table.update(routes or {})
    client = make_client(table, with_taxonomy=False)
    if csrf:
        client.http.cookies.set("csrftoken", csrf, domain="www.instahyre.com")
    return client


def detonating(**kwargs):
    """``(writer, http, client)`` whose writer explodes on ANY mutation.

    Both verbs are covered -- the two POSTs and the mutating GET -- which is
    the whole reason this file has its own constructor rather than reusing
    ``detonating_reply_writer``.
    """
    client = inbox_write_client(**kwargs)
    http = NoMutatingGetHTTP(client.http)
    writer = Writer(http, client.store, client.inbound, client.inbox)
    return writer, http, client


def gated_requests(client):
    """Every recorded request aimed at a path on the sendable allowlist.

    The second instrument, independent of the detonating double: it reads the
    MOCK TRANSPORT's record rather than the module's ``http`` object, so the
    two fail at different distances from a bug. Verb-agnostic on purpose --
    this is the one check in the suite that would still see mark_all_read if
    somebody sent it as a GET.
    """
    return [
        (r.method, r.url.path)
        for r in client.routes.requests
        if any(r.url.path.endswith(p) for p in C.SENDABLE_INBOX_PATHS)
    ]


# ===========================================================================
# 1. Starring
# ===========================================================================


def test_a_star_without_confirm_issues_nothing_at_all():
    writer, http, client = detonating()

    preview = writer.star_conversation(CONV_ID, True)

    assert preview["confirmed"] is False
    assert http.attempts == []
    assert describe(write_requests(client)) == []
    assert gated_requests(client) == []


def test_omitting_confirm_entirely_is_a_refusal_not_a_send():
    writer, http, _ = detonating()

    assert inspect.signature(Writer.star_conversation).parameters["confirm"].default is False
    writer.star_conversation(CONV_ID, True)
    assert http.attempts == []


def test_a_confirmed_star_posts_once_to_the_star_path_and_nowhere_else():
    recorder = BodyRecorder({"starred": True})
    client = inbox_write_client(routes={C.EP_STAR_CONVERSATION: recorder})

    client.writer.star_conversation(CONV_ID, True, confirm=True)

    assert describe(write_requests(client)) == [
        ("POST", "/api/v1" + C.EP_STAR_CONVERSATION)
    ]
    assert len(recorder.calls) == 1


def test_the_star_body_is_the_candidate_branch_key_for_key():
    """The payload decision, asserted rather than described.

    Both shipped callers build this body and both add ``can_user`` only under
    ``if(profileType!=="candidate")``. This account is a candidate, so the body
    is exactly two keys -- and the assertion is on the WHOLE key set, not on
    the presence of the two, because a third key sneaking in is the failure
    that would go unnoticed.
    """
    writer, _, _ = detonating()

    body = writer.star_conversation(CONV_ID, True)["would_send"]["body"]

    assert sorted(body) == sorted(C.STAR_CONVERSATION_BODY_KEYS)
    assert sorted(body) == ["job_id", "star_conv"]
    assert "can_user" not in body
    assert body["star_conv"] is True
    assert body["job_id"] == CONV_JOB_ID


def test_the_star_body_never_carries_a_key_called_starred():
    """The misreading this contract invites, refused explicitly.

    ``starred`` appears in ``toggleStarConversation`` only as
    ``response.starred`` -- the field read BACK off the reply. A body carrying
    a ``starred`` key would be a request no Instahyre client has ever made, and
    it is exactly the shape somebody reconstructing the contract from memory
    would produce.
    """
    writer, _, _ = detonating()

    for wanted in (True, False):
        conv = CONV_ID if wanted else STARRED_READ["id"]
        body = writer.star_conversation(conv, wanted)["would_send"]["body"]
        assert "starred" not in body
        assert body["star_conv"] is wanted


def test_the_job_id_is_copied_from_the_record_not_from_the_argument():
    """``conv.job_id`` is a server-supplied field on the conversation record.
    Row 1 has a different job to row 0, so an implementation that hardcoded or
    guessed one fails here."""
    writer, _, _ = detonating()

    body = writer.star_conversation(STARRED_READ["id"], False)["would_send"]["body"]

    assert body["job_id"] == STARRED_READ["job_id"]
    assert STARRED_READ["job_id"] != CONV_JOB_ID, "the fixture no longer discriminates"


def test_the_body_that_goes_out_is_exactly_the_body_the_preview_promised():
    recorder = BodyRecorder({"starred": True})
    client = inbox_write_client(routes={C.EP_STAR_CONVERSATION: recorder})
    promised = client.writer.star_conversation(CONV_ID, True)["would_send"]

    client.writer.star_conversation(CONV_ID, True, confirm=True)

    assert recorder.calls == [promised["body"]]


def test_the_star_preview_names_the_current_state_and_the_new_one():
    writer, _, _ = detonating()

    preview = writer.star_conversation(CONV_ID, True)

    assert preview["currently_starred"] is False
    assert preview["would_become"] is True
    assert preview["thread"]["company"] is not None


def test_starring_something_already_starred_is_refused_and_sends_nothing():
    """A request the site never makes: its controls only ever invert the state
    they read."""
    writer, http, client = detonating()

    with pytest.raises(NothingToDo) as excinfo:
        writer.star_conversation(STARRED_READ["id"], True, confirm=True)

    assert "already starred" in str(excinfo.value)
    assert http.attempts == []
    assert gated_requests(client) == []


def test_unstarring_something_already_unstarred_is_refused():
    """The other direction. One direction alone would pass against a check that
    only ever fires on True."""
    writer, http, _ = detonating()

    with pytest.raises(NothingToDo):
        writer.star_conversation(CONV_ID, False, confirm=True)
    assert http.attempts == []


def test_a_confirmed_star_without_a_csrf_token_refuses_before_sending():
    client = inbox_write_client(csrf=None)
    http = NoMutatingGetHTTP(client.http)
    writer = Writer(http, client.store, client.inbound, client.inbox)

    with pytest.raises(writes_module.ConfirmationRequired):
        writer.star_conversation(CONV_ID, True, confirm=True)

    assert http.attempts == []
    assert gated_requests(client) == []


def test_a_star_is_verified_from_the_responses_own_starred_field():
    """The one inbox write that can check itself out of its own reply: the site
    assigns ``response.starred`` straight onto the conversation."""
    client = inbox_write_client(routes={C.EP_STAR_CONVERSATION: BodyRecorder({"starred": True})})

    result = client.writer.star_conversation(CONV_ID, True, confirm=True)

    assert result["verified"] is True
    assert "response's own 'starred' field" in result["verified_by"]
    assert "warning" not in result


def test_a_star_whose_response_disagrees_is_reported_unverified_not_successful():
    """A 200 is not the outcome. The response here says the star did NOT take,
    and the result must say so rather than echoing the request back as fact."""
    client = inbox_write_client(routes={C.EP_STAR_CONVERSATION: BodyRecorder({"starred": False})})

    result = client.writer.star_conversation(CONV_ID, True, confirm=True)

    assert result["verified"] is False
    assert "warning" in result


def test_a_star_response_with_no_starred_field_says_the_read_back_was_unavailable():
    client = inbox_write_client(routes={C.EP_STAR_CONVERSATION: BodyRecorder({"success": True})})

    result = client.writer.star_conversation(CONV_ID, True, confirm=True)

    assert result["verified"] is False
    assert "no 'starred' field" in result["verified_by"]


# ===========================================================================
# 2. Marking one conversation read or unread
# ===========================================================================


def test_a_mark_read_without_confirm_issues_nothing_at_all():
    writer, http, client = detonating()

    preview = writer.mark_conversation_read(STARRED_READ["id"], True)

    assert preview["confirmed"] is False
    assert http.attempts == []
    assert describe(write_requests(client)) == []
    assert gated_requests(client) == []


def test_mark_read_defaults_confirm_to_false():
    writer, http, _ = detonating()

    assert (
        inspect.signature(Writer.mark_conversation_read).parameters["confirm"].default
        is False
    )
    writer.mark_conversation_read(STARRED_READ["id"], True)
    assert http.attempts == []


def test_a_confirmed_mark_read_posts_once_to_the_toggle_path_and_nowhere_else():
    recorder = BodyRecorder({"success": True})
    client = inbox_write_client(routes={C.EP_TOGGLE_MESSAGE_READ: recorder})

    client.writer.mark_conversation_read(STARRED_READ["id"], True, confirm=True)

    posts = describe(write_requests(client))
    assert posts == [("POST", "/api/v1" + C.EP_TOGGLE_MESSAGE_READ)]
    assert len(recorder.calls) == 1


def test_the_toggle_body_names_the_conversation_by_resource_uri_not_by_id():
    """THE assertion for this surface.

    ``markUnread`` sends ``{conversation: conv.resource_uri, mark_unread: ...}``
    and that value is a string the SERVER put on the record. An id -- or a URI
    this code assembled from an id -- would render identically in a preview and
    be a different request on the wire, so both halves are checked: the value
    IS the server's string, and it is not the integer in any spelling.
    """
    writer, _, _ = detonating()

    body = writer.mark_conversation_read(CONV_ID, False)["would_send"]["body"]

    assert sorted(body) == sorted(C.TOGGLE_MESSAGE_READ_BODY_KEYS)
    assert sorted(body) == ["conversation", "mark_unread"]
    assert body["conversation"] == CONV_URI
    assert body["conversation"] != CONV_ID
    assert body["conversation"] != str(CONV_ID)


def test_the_toggle_body_sends_the_flag_it_was_given_in_both_directions():
    writer, _, _ = detonating()

    marked = writer.mark_conversation_read(STARRED_READ["id"], True)["would_send"]["body"]
    unmarked = writer.mark_conversation_read(CONV_ID, False)["would_send"]["body"]

    assert marked["mark_unread"] is True
    assert unmarked["mark_unread"] is False
    assert marked["conversation"] == STARRED_READ["resource_uri"]


def test_the_preview_says_which_of_the_two_values_has_a_shipped_caller():
    """The evidence for ``true`` and the evidence for ``false`` are not the
    same, and a preview that presented them as equivalent would be letting the
    unmeasured value borrow the measured one's standing."""
    writer, _, _ = detonating()

    true_side = writer.mark_conversation_read(STARRED_READ["id"], True)
    false_side = writer.mark_conversation_read(CONV_ID, False)

    assert "only value with a shipped caller" in true_side["evidence_for_this_value"]
    assert "NO shipped caller" in false_side["evidence_for_this_value"]
    assert "never been observed" in false_side["evidence_for_this_value"]


def test_marking_read_warns_that_it_destroys_the_unread_signal_and_unread_does_not():
    """Asymmetric on purpose: unread is the only flag separating a new
    recruiter message from an old one, so clearing it costs information and
    restoring it costs none."""
    writer, _, _ = detonating()

    clearing = writer.mark_conversation_read(CONV_ID, False)
    restoring = writer.mark_conversation_read(STARRED_READ["id"], True)

    assert "losing_the_unread_signal" in clearing
    assert "losing_the_unread_signal" not in restoring


def test_a_toggle_that_would_change_nothing_is_refused():
    writer, http, client = detonating()

    with pytest.raises(NothingToDo):
        writer.mark_conversation_read(CONV_ID, True, confirm=True)
    with pytest.raises(NothingToDo):
        writer.mark_conversation_read(STARRED_READ["id"], False, confirm=True)

    assert http.attempts == []
    assert gated_requests(client) == []


def test_a_confirmed_toggle_without_a_csrf_token_refuses_before_sending():
    client = inbox_write_client(csrf=None)
    http = NoMutatingGetHTTP(client.http)
    writer = Writer(http, client.store, client.inbound, client.inbox)

    with pytest.raises(writes_module.ConfirmationRequired):
        writer.mark_conversation_read(CONV_ID, False, confirm=True)

    assert http.attempts == []
    assert gated_requests(client) == []


def test_the_toggle_body_that_goes_out_is_the_body_the_preview_promised():
    recorder = BodyRecorder({"success": True})
    client = inbox_write_client(routes={C.EP_TOGGLE_MESSAGE_READ: recorder})
    promised = client.writer.mark_conversation_read(CONV_ID, False)["would_send"]

    client.writer.mark_conversation_read(CONV_ID, False, confirm=True)

    assert recorder.calls == [promised["body"]]


def test_a_toggle_is_verified_by_re_reading_the_conversation_record():
    """The response shape for this action is NOT in the captured contract --
    the site's own callback reads nothing off it -- so a re-read is the only
    honest check available."""
    after = copy.deepcopy(CONVERSATIONS)
    after["objects"][0]["is_latest_msg_read"] = True
    calls = {"n": 0}

    def conversations_route(request):
        # First read serves the pre-write state, every read after it serves the
        # post-write state. Without that the "verified" branch would be true of
        # a fixture that never changed, which certifies nothing.
        calls["n"] += 1
        return CONVERSATIONS if calls["n"] == 1 else after

    client = inbox_write_client(
        conversations=conversations_route,
        routes={C.EP_TOGGLE_MESSAGE_READ: BodyRecorder({"success": True})},
    )

    result = client.writer.mark_conversation_read(CONV_ID, False, confirm=True)

    assert result["verified"] is True
    assert "re-read of the conversation record" in result["verified_by"]
    assert "warning" not in result


def test_a_toggle_whose_re_read_still_shows_the_old_state_is_reported_unverified():
    """The fixture never changes here, so the read-back must NOT report
    success. This is the control for the test above: together they show the
    check discriminating rather than always agreeing."""
    client = inbox_write_client(
        routes={C.EP_TOGGLE_MESSAGE_READ: BodyRecorder({"success": True})}
    )

    result = client.writer.mark_conversation_read(CONV_ID, False, confirm=True)

    assert result["verified"] is False
    assert "not what was asked for" in result["verified_by"]
    assert "warning" in result


# ===========================================================================
# 3. Marking the WHOLE inbox read -- the GET that mutates
# ===========================================================================


def test_mark_all_read_without_confirm_issues_no_request_to_the_gated_path():
    """The gate that matters most, and the one every verb-shaped instrument in
    this repo is blind to. Asserted twice: on the writer's own http object,
    which detonates on a gated GET, and on the mock transport's record, which
    is verb-agnostic."""
    writer, http, client = detonating()

    preview = writer.mark_all_conversations_read()

    assert preview["confirmed"] is False
    assert http.attempts == []
    assert gated_requests(client) == []
    assert describe(write_requests(client)) == []
    assert client.routes.requests, "the preview really did read the inbox"


def test_mark_all_read_defaults_confirm_to_false():
    writer, http, _ = detonating()

    assert (
        inspect.signature(Writer.mark_all_conversations_read).parameters["confirm"].default
        is False
    )
    writer.mark_all_conversations_read()
    assert http.attempts == []


def test_a_confirmed_sweep_makes_exactly_one_GET_to_the_mark_all_read_path():
    recorder = GetRecorder(MARK_ALL_READ_CLEARED)
    client = inbox_write_client(routes={C.EP_MARK_ALL_READ: recorder})

    client.writer.mark_all_conversations_read(confirm=True)

    hits = gated_requests(client)
    assert hits == [("GET", "/api/v1" + C.EP_MARK_ALL_READ)]
    # And still nothing on any write verb: the danger here is precisely that
    # the mutation does not look like one.
    assert describe(write_requests(client)) == []


def test_the_sweep_sends_page_loaded_at_and_nothing_else():
    """``buildFilters()`` returns an empty dict on the default view, so the
    query is one parameter. A stray filter would narrow the sweep in a way
    nobody chose, and an extra parameter would be a request the site never
    makes."""
    client = inbox_write_client(
        routes={C.EP_MARK_ALL_READ: GetRecorder(MARK_ALL_READ_CLEARED)}
    )

    client.writer.mark_all_conversations_read(confirm=True)

    params = client.routes.last_params(C.EP_MARK_ALL_READ)
    assert sorted(params) == sorted(C.MARK_ALL_READ_QUERY_KEYS)
    assert sorted(params) == ["page_loaded_at"]
    assert len(params["page_loaded_at"]) == 1


def test_page_loaded_at_is_spelled_the_way_javascript_spells_it():
    """``new Date().toISOString()`` -- UTC, exactly three fractional digits, a
    literal ``Z``. Python's default ``isoformat()`` emits six digits and a
    ``+00:00`` offset instead, and a server that parses one spelling and not
    the other would silently drop the race guard rather than reject it."""
    client = inbox_write_client(
        routes={C.EP_MARK_ALL_READ: GetRecorder(MARK_ALL_READ_CLEARED)}
    )

    client.writer.mark_all_conversations_read(confirm=True)

    stamp = client.routes.last_params(C.EP_MARK_ALL_READ)["page_loaded_at"][0]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", stamp), stamp


def test_the_preview_names_every_thread_that_would_lose_its_unread_flag():
    """There is no bulk undo, and nothing on the platform records which threads
    were unread. The preview's list is the only way back, so it has to name
    them rather than count them."""
    writer, _, _ = detonating()

    preview = writer.mark_all_conversations_read()

    named = [entry["conv_id"] for entry in preview["would_affect"]]
    assert named == [CONV_ID]
    assert preview["would_affect_count"] == 1
    assert preview["unread_total"] == 1
    assert "no bulk undo" in preview["how_to_undo"]
    assert "instahyre_mark_conversation_read" in preview["how_to_undo"]


def test_the_preview_says_out_loud_that_a_get_can_mutate():
    writer, _, _ = detonating()

    preview = writer.mark_all_conversations_read()

    assert preview["would_send"]["method"] == "GET"
    assert preview["would_send"]["body"] is None
    assert "mutates" in preview["a_get_that_mutates"].lower()
    assert "NOTHING HAS BEEN SENT" in preview["next"]


def test_the_sweep_refuses_when_the_server_reports_nothing_unread():
    """Instahyre's own gate: ``if(inboxService.getMarkAllAsReadCount())``, which
    is ``conv_count.unread || 0``. At zero the site issues no request, so
    neither does this."""
    all_read = copy.deepcopy(CONVERSATIONS)
    for obj in all_read["objects"]:
        obj["is_latest_msg_read"] = True
    writer, http, client = detonating(conversations=all_read, counts=COUNTS_ALL_READ)

    result = writer.mark_all_conversations_read(confirm=True)

    assert result["confirmed"] is False
    assert result["changed"] is False
    assert "getMarkAllAsReadCount" in result["why_nothing_to_do"]
    assert http.attempts == []
    assert gated_requests(client) == []


def test_a_zero_count_beside_an_unread_row_refuses_rather_than_picking_a_side():
    """A contradiction is not a green light. The count says nothing to do and a
    row says otherwise; a bulk mutation is the last place to resolve that by
    taking the reading that lets it proceed."""
    writer, http, client = detonating(counts=COUNTS_ALL_READ)

    result = writer.mark_all_conversations_read(confirm=True)

    assert result["confirmed"] is False
    assert "CONTRADICTION" in result["why_nothing_to_do"]
    assert http.attempts == []
    assert gated_requests(client) == []


def test_an_unreadable_count_with_no_unread_rows_refuses_rather_than_claiming_zero():
    """'Nothing observed to do' is not 'nothing to do'. A count endpoint that
    cannot answer must not be reported as a clean inbox."""
    all_read = copy.deepcopy(CONVERSATIONS)
    for obj in all_read["objects"]:
        obj["is_latest_msg_read"] = True
    writer, http, client = detonating(
        conversations=all_read, counts=httpx.Response(500, json={"error": "boom"})
    )

    result = writer.mark_all_conversations_read(confirm=True)

    assert result["confirmed"] is False
    assert result["unread_total"] is None
    assert "could not be read" in result["why_nothing_to_do"]
    assert http.attempts == []
    assert gated_requests(client) == []


def test_a_confirmed_sweep_without_a_csrf_token_refuses_before_requesting():
    """A GET is exempt from Django's CSRF check, so this rail is THIS SERVER'S
    and the code says so. It is applied anyway because the request bulk-mutates
    and an ambiguous outcome on a bulk mutation is the worst result available
    here."""
    client = inbox_write_client(csrf=None)
    http = NoMutatingGetHTTP(client.http)
    writer = Writer(http, client.store, client.inbound, client.inbox)

    with pytest.raises(writes_module.ConfirmationRequired):
        writer.mark_all_conversations_read(confirm=True)

    assert http.attempts == []
    assert gated_requests(client) == []


def test_the_sweep_is_verified_against_the_field_instahyres_own_callback_reads():
    client = inbox_write_client(
        routes={C.EP_MARK_ALL_READ: GetRecorder(MARK_ALL_READ_CLEARED)}
    )

    result = client.writer.mark_all_conversations_read(confirm=True)

    assert result["verified"] is True
    assert "conv_count.unread" in result["verified_by"]
    assert result["unread_after"] == 0
    assert result["unread_before"] == 1
    assert "warning" not in result


def test_a_sweep_that_cannot_be_confirmed_says_so_and_says_do_not_re_run():
    """The dangerous half. A second sweep cannot undo the first; it can only
    widen it, so an unconfirmed sweep must not read as a failed one."""
    client = inbox_write_client(
        routes={C.EP_MARK_ALL_READ: GetRecorder({"success": True})}
    )

    result = client.writer.mark_all_conversations_read(confirm=True)

    assert result["verified"] is False
    assert "Do NOT simply re-run it" in result["warning"]


def test_a_writer_without_an_inbox_refuses_to_sweep_rather_than_mutating_blind():
    client = inbox_write_client()
    writer = Writer(client.http, client.store, client.inbound)

    with pytest.raises(InstahyreError) as excinfo:
        writer.mark_all_conversations_read(confirm=True)

    assert "wiring bug" in str(excinfo.value)


# ===========================================================================
# Shared rails: the id has to be his, and the record has to carry the field
# ===========================================================================


@pytest.mark.parametrize("call", ["star", "mark_read"])
def test_a_conv_id_that_is_not_his_is_refused_before_anything_is_sent(call):
    """The same rule the reply tool follows. His own conversation list is the
    only place a conv_id legitimately comes from, and a write aimed at a
    foreign id is the failure with no undo."""
    writer, http, client = detonating()

    with pytest.raises(NotFound):
        if call == "star":
            writer.star_conversation(999999999, True, confirm=True)
        else:
            writer.mark_conversation_read(999999999, True, confirm=True)

    assert http.attempts == []
    assert gated_requests(client) == []


def test_a_record_with_no_resource_uri_is_refused_rather_than_having_one_built():
    """The refusal that keeps the URI honest. Assembling one from the id would
    produce a preview that looks right and a request that is not the site's."""
    stripped = copy.deepcopy(CONVERSATIONS)
    stripped["objects"][0].pop("resource_uri")
    writer, http, client = detonating(conversations=stripped)

    with pytest.raises(InstahyreError) as excinfo:
        writer.mark_conversation_read(CONV_ID, False, confirm=True)

    assert "will not assemble one" in str(excinfo.value)
    assert http.attempts == []
    assert gated_requests(client) == []


def test_a_record_with_no_job_id_is_refused_rather_than_starred_without_one():
    stripped = copy.deepcopy(CONVERSATIONS)
    stripped["objects"][0].pop("job_id")
    writer, http, client = detonating(conversations=stripped)

    with pytest.raises(InstahyreError) as excinfo:
        writer.star_conversation(CONV_ID, True, confirm=True)

    assert "requires one" in str(excinfo.value)
    assert http.attempts == []
    assert gated_requests(client) == []


# ===========================================================================
# The read tier did not move, and the GET census the other instruments cannot do
# ===========================================================================


@pytest.mark.parametrize("path", sorted(C.SENDABLE_INBOX_PATHS))
def test_the_read_guard_still_refuses_every_one_of_these_paths(path):
    """Per path, so a failure names which one leaked. The write channel is a
    separate door, not a hole in this guard."""
    with pytest.raises(MutatingPathRefused):
        guard_read_only(path)


def test_the_read_tiers_marker_list_is_byte_for_byte_what_it_was():
    """Nothing about building three inbox writes was allowed to shrink the read
    tier's refusal list. Pinned to literals rather than to itself."""
    assert C.MUTATING_PATH_MARKERS == (
        "mark_all_read",
        "send_message",
        "star_conversation",
        "toggle_message_read",
        "apply_bulk",
    )


def test_the_forbidden_endpoints_set_is_untouched():
    """Both apply_bulk spellings, banned at any evidence level. This build had
    no business near them, and the assertion says so out loud."""
    assert C.FORBIDDEN_ENDPOINTS == frozenset(
        {
            "/candidate_opportunities/candidate_opportunity/apply_bulk/",
            "/candidate_opportunities/candidate_matching/apply_bulk/",
        }
    )
    assert not (C.FORBIDDEN_ENDPOINTS & C.SENDABLE_INBOX_PATHS)


def package_sources():
    root = pathlib.Path(writes_module.__file__).resolve().parent
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(root.glob("*.py"))}


def mutating_get_call_sites(sources):
    """``(file, line, target)`` for every ``http.get(C.X)`` whose X is gated.

    Resolves the constant NAME against ``constants`` and reports the call only
    when it resolves to a path on the sendable allowlist -- so this counts
    mutating GETs and ignores the many ordinary ones. Receiver-attributed the
    same way ``test_inbound_safety`` attributes its write verbs: only the HTTP
    client can reach the account.
    """
    hits = []
    for name, text in sources.items():
        for node in ast.walk(ast.parse(text, filename=name)):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "get":
                continue
            owner = node.func.value
            is_client = (isinstance(owner, ast.Name) and owner.id == "http") or (
                isinstance(owner, ast.Attribute) and owner.attr == "http"
            )
            if not is_client or not node.args:
                continue
            first = node.args[0]
            target = None
            if isinstance(first, ast.Attribute) and isinstance(first.value, ast.Name):
                target = first.attr
            elif isinstance(first, ast.Constant) and isinstance(first.value, str):
                target = None
                if first.value in C.SENDABLE_INBOX_PATHS:
                    hits.append((name, node.lineno, repr(first.value)))
                continue
            if target and getattr(C, target, None) in C.SENDABLE_INBOX_PATHS:
                hits.append((name, node.lineno, "C." + target))
    return hits


def test_no_get_call_site_aims_at_a_mutating_path_outside_the_gated_one():
    """The census the POST census is structurally blind to.

    ``test_every_post_call_site_in_the_package_targets_a_measured_endpoint``
    enumerates the write VERBS, and this package's most dangerous request is a
    GET. So the surface it cannot see is enumerated here instead: exactly one
    ``http.get`` in the whole package may aim at a path on the sendable
    allowlist, it lives in ``writes.py``, and it is the gated sweep.
    """
    hits = mutating_get_call_sites(package_sources())

    assert [(name, target) for name, _, target in hits] == [
        ("writes.py", "C.EP_MARK_ALL_READ")
    ], "a GET aimed at a mutating inbox path appeared: %s" % (hits,)


def test_the_mutating_get_census_reports_a_planted_one__CONTROL():
    """A census that has only ever been seen returning the expected answer
    certifies nothing. Both spellings a real leak could take are planted: a
    constant reference and a bare literal."""
    synthetic = {
        "rogue.py": (
            "def go(http):\n"
            "    http.get(C.EP_CONVERSATIONS, params={})\n"
            "    http.get(C.EP_STAR_CONVERSATION, params={})\n"
            '    http.get("/inbox_page/candidate_conversation/mark_all_read")\n'
        )
    }

    hits = mutating_get_call_sites(synthetic)

    assert [target for _, _, target in hits] == [
        "C.EP_STAR_CONVERSATION",
        "'/inbox_page/candidate_conversation/mark_all_read'",
    ]


# ===========================================================================
# The MCP surface
# ===========================================================================


@pytest.mark.parametrize(
    "tool",
    [
        server_module.instahyre_star_conversation,
        server_module.instahyre_mark_conversation_read,
        server_module.instahyre_mark_all_conversations_read,
    ],
)
def test_every_new_inbox_write_tool_defaults_confirm_to_false(tool):
    assert inspect.signature(tool).parameters["confirm"].default is False


@pytest.mark.parametrize(
    "tool",
    [
        server_module.instahyre_star_conversation,
        server_module.instahyre_mark_conversation_read,
        server_module.instahyre_mark_all_conversations_read,
    ],
)
def test_every_new_tool_admits_in_its_docstring_that_it_never_ran_live(tool):
    """The honesty requirement, enforced rather than trusted. His inbox holds
    zero conversations, so a docstring that read as though the tool had been
    exercised would be borrowing confidence from a test that never happened."""
    doc = tool.__doc__ or ""
    assert "NEVER RUN AGAINST LIVE DATA" in doc, tool.__name__
    assert "zero conversations" in doc, tool.__name__


def test_the_sweep_tool_explains_in_its_own_docstring_why_a_GET_is_gated():
    doc = server_module.instahyre_mark_all_conversations_read.__doc__ or ""

    assert "WHY A GET IS GATED" in doc
    assert "MUTATES" in doc
    assert "mark_all_read:{method:'GET'" in doc


def test_the_server_declares_the_sweep_irreversible_and_not_its_two_siblings():
    """A list that included everything gated would stop meaning anything. The
    sweep is listed because what it destroys is not a flag but the knowledge of
    which threads carried one; star and per-thread read are reversible from
    information the account still holds."""
    info = server_module.instahyre_server_info()

    assert "instahyre_mark_all_conversations_read" in info["irreversible_tools"]
    assert "instahyre_star_conversation" not in info["irreversible_tools"]
    assert "instahyre_mark_conversation_read" not in info["irreversible_tools"]


# ===========================================================================
# The premise underneath every assertion above
# ===========================================================================


def test_no_assertion_in_this_file_could_have_reached_the_real_instahyre():
    """Both halves: the mock really served these calls, so the recorded request
    list is the true record of what this package tried to send -- and the
    genuine transport really is blocked, so a route this file forgot to mock
    could not have quietly gone out over the wire."""
    writer, _, client = detonating()

    writer.star_conversation(CONV_ID, True)
    assert client.routes.requests, "the mock transport served the preview's reads"

    with pytest.raises(AssertionError, match="real network"):
        httpx.HTTPTransport().handle_request(
            httpx.Request("GET", C.API_BASE + C.EP_MARK_ALL_READ)
        )


def test_the_detonating_double_can_actually_fire__CONTROL():
    """Inherited from ``test_writes.py`` and re-pointed at this file's own
    double, because "the harness saw no write" is only a statement about the
    gate if the harness can see one."""
    writer, http, _ = detonating()

    with pytest.raises(WriteAttempted):
        http.post(C.EP_STAR_CONVERSATION, json_body={})
    with pytest.raises(MutatingGetAttempted):
        http.get(C.EP_MARK_ALL_READ, params={})
    assert len(http.attempts) == 2
