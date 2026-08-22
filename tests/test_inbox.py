"""inbox.py -- the read-only inbox tier, and the guard that keeps it read-only.

Four properties are on trial in this file, and the first one is the reason the
module exists at all.

1. **The read-only guard fires, and the guard also lets real traffic past.**
   Four inbox endpoints mutate and one of them -- ``mark_all_read`` -- is a
   **GET** sharing a prefix with the list call, so an ordinary "walk the
   resource" probe would silently wipe his unread flags. The guard is therefore
   tested in BOTH directions: every marker refused, including behind a query
   string, a trailing slash and a change of case; and the three real read paths
   passed through unchanged. A guard that cannot fire certifies nothing, and a
   guard that refuses everything is not a guard either.

2. **Every request the module makes is a read.** Asserted on the recorded wire
   rather than on intent, and additionally on the module's own AST, so a method
   added later that skips the guard is a test failure rather than a discovery.

3. **The shaping does not lie about state.** ``is_latest_msg_read`` is INVERTED
   into ``unread``; getting that backwards would report every answered thread as
   needing attention and every ignored one as handled. Both directions are
   pinned.

4. **An empty inbox is diagnosed, never shrugged at.** His inbox really is
   empty -- ``conversations_empty.json`` is the live capture -- so the empty
   path is the COMMON path here, not the edge case, and it has to say which
   silence it is.

The populated fixtures are synthetic and say so in a top-level ``_synthetic``
key: there is no real thread to capture, and inventing one and calling it a
capture would be the exact dishonesty this package is built against. Their
field names come from the frontend contract transcribed in ``constants``.

No network is touched: conftest builds every client on an ``httpx.MockTransport``
and makes the real transports raise, so an unmocked path is an AssertionError
rather than a real request.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from conftest import fixture_json, html_response, json_response, make_client
from instahyre_server import constants as C
from instahyre_server import inbox as inbox_module
from instahyre_server.errors import ApiError, AuthRequired, InvalidFilter, NotFound
from instahyre_server.inbox import (
    DEFAULT_BODY_CHARS,
    MutatingPathRefused,
    guard_read_only,
    shape_conversation,
)

CONVERSATIONS = C.EP_CONVERSATIONS
COUNTS = C.EP_CONVERSATION_COUNT
MESSAGES = C.EP_MESSAGES

#: The live capture: his inbox holds zero conversations, verified 2026-08-21.
EMPTY = fixture_json("conversations_empty.json")

#: Hand-built from the frontend contract -- see the fixture's own _synthetic key.
POPULATED = fixture_json("conversations_populated.json")
THREAD = fixture_json("conversation_messages.json")
COUNT_PAYLOAD = fixture_json("conversation_counts.json")

#: The live capture of what the message endpoint answers for a conv_id that is
#: NOT his: 200, an empty objects list, and no thread frame at all. Captured
#: 2026-08-22 against conv_id 1 and 999999999 -- see the fixture's _capture key.
NOT_HIS = fixture_json("conversation_messages_not_found.json")

#: The first record is UNREAD and unstarred; the second is READ and starred.
#: The inversion test needs both values present, so this is asserted, not hoped.
UNREAD_CONV = POPULATED["objects"][0]
STARRED_CONV = POPULATED["objects"][1]
CONV_ID = UNREAD_CONV["id"]

#: Invented companies and titles for the job join. Real names never enter a
#: fixture or a test in this package.
JOBS = {
    601001: ("Northwind Analytics", "Senior Backend Engineer"),
    601002: ("Larkspur Systems", "Platform Engineer"),
    601003: ("Fernway Labs", "Node.js Developer"),
}


def detail_path(job_id: int) -> str:
    return C.EP_JOB_DETAIL.format(job_id=job_id)


def job_detail(job_id: int) -> dict:
    """A minimal public job payload, shaped the way shape_detail reads one."""
    company, title = JOBS[job_id]
    return {
        "id": job_id,
        "title": title,
        "hiring_company_name": company,
        "recruiter_company_name": company,
        "locations": ["Bangalore"],
        "keywords": "Node.js, TypeScript",
        "job_function_names": ["Backend Development"],
        "is_active": True,
    }


def job_routes() -> dict:
    return {detail_path(job_id): job_detail(job_id) for job_id in JOBS}


def inbox_client(routes=None, *, conversations=POPULATED, counts=COUNT_PAYLOAD, thread=None):
    """A client wired for the inbox only.

    Taxonomy is deliberately NOT wired: nothing in this tier resolves a filter
    server-side, so a taxonomy request here should be a loud "Unmocked request"
    rather than a quietly satisfied one. Job detail routes are likewise absent
    unless a test asks for them, so an unexpected join is equally loud.
    """
    table = {CONVERSATIONS: conversations, COUNTS: counts}
    if thread is not None:
        table[MESSAGES] = thread
    table.update(routes or {})
    return make_client(table, with_taxonomy=False)


# ---------------------------------------------------------------------------
# The read-only guard, shown failing
# ---------------------------------------------------------------------------


def test_the_marker_list_the_guard_runs_on_is_not_empty():
    """The precondition for every other guard test. An empty tuple would make
    guard_read_only a pass-through and every refusal test below vacuous."""
    assert len(C.MUTATING_PATH_MARKERS) == 5
    assert "mark_all_read" in C.MUTATING_PATH_MARKERS


def test_the_read_only_guard_refuses_every_mutating_marker_the_package_knows_about():
    for marker in C.MUTATING_PATH_MARKERS:
        path = "/inbox_page/candidate_conversation/" + marker
        with pytest.raises(MutatingPathRefused) as excinfo:
            guard_read_only(path)
        assert excinfo.value.context["marker"] == marker


def test_a_mutating_marker_hiding_behind_a_query_string_is_still_refused():
    """A substring test on purpose: equality against a constant would miss this."""
    for marker in C.MUTATING_PATH_MARKERS:
        path = "/inbox_page/candidate_conversation/" + marker + "?limit=10&offset=0"
        with pytest.raises(MutatingPathRefused):
            guard_read_only(path)


def test_a_mutating_marker_wearing_a_trailing_slash_is_still_refused():
    """The named hazard: mark_all_read must not become reachable by appending /."""
    for marker in C.MUTATING_PATH_MARKERS:
        with pytest.raises(MutatingPathRefused):
            guard_read_only("/inbox_page/candidate_conversation/" + marker + "/")


def test_a_mutating_marker_in_a_different_case_is_still_refused():
    for marker in C.MUTATING_PATH_MARKERS:
        with pytest.raises(MutatingPathRefused):
            guard_read_only(("/inbox_page/candidate_conversation/" + marker).upper())


def test_all_four_real_mutating_inbox_endpoints_are_refused_by_their_full_path():
    """The control set is the package's own list of what mutates, so a fifth
    endpoint discovered later cannot be added to one list and forgotten in the
    other without this failing."""
    assert len(C.MUTATING_INBOX_PATHS) == 4
    for path in sorted(C.MUTATING_INBOX_PATHS):
        with pytest.raises(MutatingPathRefused):
            guard_read_only(path)


def test_the_refusal_names_the_marker_it_fired_on_and_says_the_tier_is_read_only():
    with pytest.raises(MutatingPathRefused) as excinfo:
        guard_read_only("/inbox_page/candidate_conversation/mark_all_read")

    assert excinfo.value.kind == "mutating_path_refused"
    assert "mark_all_read" in excinfo.value.message
    assert "read-only" in excinfo.value.message


# ---------------------------------------------------------------------------
# ... and the other direction: the guard passes the real read paths
# ---------------------------------------------------------------------------


def test_the_guard_lets_the_three_real_read_endpoints_through_unchanged():
    """A guard that refuses everything is as useless as one that refuses nothing."""
    for path in (C.EP_CONVERSATIONS, C.EP_CONVERSATION_COUNT, C.EP_MESSAGES):
        assert guard_read_only(path) == path


def test_the_guard_passes_a_read_path_that_carries_its_own_query_string():
    path = C.EP_MESSAGES + "?conv_id=5101"
    assert guard_read_only(path) == path


def test_the_guard_passes_the_count_endpoint_even_though_it_sits_under_the_list_prefix():
    """count and mark_all_read are siblings on one prefix. Only one is refused."""
    assert guard_read_only(C.EP_CONVERSATION_COUNT) == C.EP_CONVERSATION_COUNT
    with pytest.raises(MutatingPathRefused):
        guard_read_only(C.EP_CONVERSATIONS + "/mark_all_read")


# ---------------------------------------------------------------------------
# Every request goes through the guard -- on the wire, and in the source
# ---------------------------------------------------------------------------


def test_the_only_paths_the_inbox_ever_requests_are_the_three_read_endpoints():
    """Asserted on the recorded wire, not on the module's intentions."""
    client = inbox_client(thread=THREAD)

    client.inbox.list_conversations(include_job=False)
    client.inbox.conversation_counts()
    client.inbox.read_conversation(CONV_ID)

    assert set(client.routes.paths) == {CONVERSATIONS, COUNTS, MESSAGES}
    assert all(request.method == "GET" for request in client.routes.requests)
    for path in client.routes.paths:
        for marker in C.MUTATING_PATH_MARKERS:
            assert marker not in path.lower()


def http_calls_in(source: str):
    """(count, offenders) for every ``self.http.<verb>(...)`` in a source string.

    An offender is a call whose first argument is not ``guard_read_only(...)``.
    """
    total = 0
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if not (isinstance(owner, ast.Attribute) and owner.attr == "http"):
            continue
        total += 1
        first = node.args[0] if node.args else None
        guarded = (
            isinstance(first, ast.Call)
            and isinstance(first.func, ast.Name)
            and first.func.id == "guard_read_only"
        )
        if not guarded:
            offenders.append(ast.unparse(node))
    return total, offenders


def test_every_http_call_in_the_inbox_module_source_is_wrapped_in_the_guard():
    """The wire test above only covers the methods it calls. This covers the file."""
    source = pathlib.Path(inbox_module.__file__).resolve().read_text(encoding="utf-8")

    total, offenders = http_calls_in(source)

    assert offenders == [], "an inbox request bypasses guard_read_only"
    # 4 since the not-found cross-check: list, counts, messages, and
    # _conversation_ids' own paged read of the list. That fourth call site is
    # exactly what this assertion is for -- it fired, it was re-checked, and it
    # goes through the guard like the other three.
    assert total == 4, "the module's request count changed; re-check the new call site"


def test_the_unguarded_call_scanner_catches_an_unguarded_call_when_it_is_shown_one():
    """The control. A scanner that cannot fail certifies nothing."""
    total, offenders = http_calls_in(
        "class Fake:\n"
        "    def go(self):\n"
        "        return self.http.get(C.EP_CONVERSATIONS, params={})\n"
    )

    assert total == 1
    assert len(offenders) == 1
    assert "self.http.get" in offenders[0]


# ---------------------------------------------------------------------------
# Shaping -- the inversion is the field most likely to be got backwards
# ---------------------------------------------------------------------------


def test_the_fixture_carries_both_read_states_or_the_inversion_test_proves_nothing():
    assert UNREAD_CONV["is_latest_msg_read"] is False
    assert STARRED_CONV["is_latest_msg_read"] is True
    assert STARRED_CONV["is_starred"] is True


def test_an_unread_conversation_is_reported_as_unread_not_as_is_latest_msg_read():
    client = inbox_client()

    first = client.inbox.list_conversations(include_job=False)["conversations"][0]

    assert first["unread"] is True


def test_a_conversation_whose_latest_message_was_read_is_reported_as_not_unread():
    """The other half of the inversion. One direction alone would pass against
    an implementation that hardcodes True."""
    client = inbox_client()

    second = client.inbox.list_conversations(include_job=False)["conversations"][1]

    assert second["unread"] is False


def test_the_raw_is_latest_msg_read_flag_never_reaches_the_caller():
    """Shipping both spellings would put the double negative back at the call site."""
    client = inbox_client()

    records = client.inbox.list_conversations(include_job=False)["conversations"]

    assert all("is_latest_msg_read" not in record for record in records)
    assert all("is_starred" not in record for record in records)


def test_a_conversation_record_with_no_read_flag_reports_unread_as_unknown_not_as_read():
    """An absent field is not a measurement, and must not be reported as one."""
    assert shape_conversation({"id": 5101})["unread"] is None


def test_is_starred_and_latest_message_and_latest_msg_at_are_renamed_on_the_way_out():
    client = inbox_client()

    records = client.inbox.list_conversations(include_job=False)["conversations"]

    assert records[1]["starred"] == STARRED_CONV["is_starred"] is True
    assert records[0]["starred"] is False
    assert records[0]["preview"] == UNREAD_CONV["latest_message"]
    assert records[0]["last_message_at"] == UNREAD_CONV["latest_msg_at"] == "21 Aug"


def test_a_preview_carrying_markup_is_stripped_to_text_before_it_is_previewed():
    record = shape_conversation(dict(UNREAD_CONV, latest_message="<p>Call on <b>Thursday</b>?</p>"))

    assert record["preview"] == "Call on Thursday?"


def test_each_record_carries_the_conversation_id_and_the_job_id_and_the_opportunity_id():
    """Three different ids. read_conversation takes the first, the job join the
    second, and the opportunity tier the third."""
    client = inbox_client()

    first = client.inbox.list_conversations(include_job=False)["conversations"][0]

    assert first["id"] == UNREAD_CONV["id"] == 5101
    assert first["job_id"] == UNREAD_CONV["job_id"] == 601001
    assert first["opportunity_id"] == UNREAD_CONV["opportunity_id"] == 7101


# ---------------------------------------------------------------------------
# Filter emission -- the frontend's rules are strict and are copied exactly
# ---------------------------------------------------------------------------


def test_the_in_process_status_goes_out_as_the_integer_one():
    client = inbox_client()

    client.inbox.list_conversations(status="in_process", include_job=False)

    assert client.routes.last_params(CONVERSATIONS)["status"] == ["1"]


def test_the_closed_by_recruiter_status_goes_out_as_the_integer_two():
    client = inbox_client()

    client.inbox.list_conversations(status="closed_by_recruiter", include_job=False)

    assert client.routes.last_params(CONVERSATIONS)["status"] == ["2"]


def test_omitting_the_status_sends_no_status_key_at_all_rather_than_a_zero():
    """The site's 'All' is falsy, so it omits the key; status=0 is a wire value
    nobody has ever seen their server handle."""
    client = inbox_client()

    result = client.inbox.list_conversations(include_job=False)

    assert "status" not in client.routes.last_params(CONVERSATIONS)
    assert result["filters_applied"] is None


def test_a_status_spelled_with_a_space_or_a_dash_is_normalised_rather_than_refused():
    client = inbox_client()

    client.inbox.list_conversations(status="In Process", include_job=False)
    assert client.routes.last_params(CONVERSATIONS)["status"] == ["1"]

    client.inbox.list_conversations(status="closed-by-recruiter", include_job=False)
    assert client.routes.last_params(CONVERSATIONS)["status"] == ["2"]


def test_unread_only_goes_out_as_the_literal_true():
    client = inbox_client()

    result = client.inbox.list_conversations(unread_only=True, include_job=False)

    assert client.routes.last_params(CONVERSATIONS)["unread"] == ["true"]
    assert result["filters_applied"] == {"unread": True}


def test_unread_only_left_off_sends_no_unread_key_at_all_and_never_unread_false():
    client = inbox_client()

    client.inbox.list_conversations(unread_only=False, include_job=False)

    sent = client.routes.last_params(CONVERSATIONS)
    assert "unread" not in sent
    assert sorted(sent) == ["limit", "offset"]


def test_starred_only_goes_out_as_the_literal_true():
    client = inbox_client()

    result = client.inbox.list_conversations(starred_only=True, include_job=False)

    assert client.routes.last_params(CONVERSATIONS)["starred"] == ["true"]
    assert result["filters_applied"] == {"starred": True}


def test_starred_only_left_off_sends_no_starred_key_at_all_and_never_starred_false():
    client = inbox_client()

    client.inbox.list_conversations(starred_only=False, include_job=False)

    assert "starred" not in client.routes.last_params(CONVERSATIONS)


def test_a_free_text_query_is_forwarded_and_named_in_filters_applied():
    client = inbox_client()

    result = client.inbox.list_conversations(query="backend", include_job=False)

    assert client.routes.last_params(CONVERSATIONS)["query"] == ["backend"]
    assert result["filters_applied"] == {"query": "backend"}


def test_an_unknown_status_is_refused_before_any_request_and_names_the_valid_values():
    client = inbox_client()

    result = "sentinel"
    with pytest.raises(InvalidFilter) as excinfo:
        result = client.inbox.list_conversations(status="archived")

    assert result == "sentinel", "list_conversations returned instead of raising"
    assert excinfo.value.field == "status"
    for name in C.CONV_STATUS:
        assert name in excinfo.value.message
    assert client.routes.count() == 0, "a bad filter must not cost a request"


def test_asking_for_unread_and_starred_together_is_refused_before_any_request():
    """A combination the site's own UI cannot produce, so nobody has tested it."""
    client = inbox_client()

    result = "sentinel"
    with pytest.raises(InvalidFilter) as excinfo:
        result = client.inbox.list_conversations(unread_only=True, starred_only=True)

    assert result == "sentinel", "list_conversations returned instead of raising"
    assert excinfo.value.field == "unread_only"
    assert client.routes.count() == 0


def test_limit_and_offset_are_always_on_the_wire_and_are_not_counted_as_filters():
    client = inbox_client()

    result = client.inbox.list_conversations(limit=25, offset=10, include_job=False)

    sent = client.routes.last_params(CONVERSATIONS)
    assert sent["limit"] == ["25"]
    assert sent["offset"] == ["10"]
    assert result["limit"] == 25
    assert result["offset"] == 10
    assert result["filters_applied"] is None


# ---------------------------------------------------------------------------
# The empty inbox -- his real one, and it is diagnosed rather than shrugged at
# ---------------------------------------------------------------------------


def test_the_captured_empty_inbox_really_is_empty_or_the_tests_below_prove_nothing():
    assert EMPTY["objects"] == []
    assert COUNT_PAYLOAD["conv_count"]["unread"] == 0


def test_an_empty_inbox_never_comes_back_without_a_diagnosis():
    client = inbox_client(conversations=EMPTY)

    result = client.inbox.list_conversations(include_job=False)

    assert result["conversations"] == []
    assert result["count"] == 0
    assert result["diagnosis"]
    assert result["unread_total"] == 0
    assert result["starred_total"] == 0


def test_a_filtered_empty_result_blames_the_filters_and_a_bare_one_blames_the_inbox():
    """The distinction that matters to a caller: filters hid everything, or
    there is genuinely nothing there. One string for both would be useless."""
    filtered = inbox_client(conversations=EMPTY).inbox.list_conversations(
        status="in_process", include_job=False
    )["diagnosis"]
    bare = inbox_client(conversations=EMPTY).inbox.list_conversations(include_job=False)[
        "diagnosis"
    ]

    assert filtered != bare
    assert "No conversations matched these filters" in filtered
    assert "status=1" in filtered, "the diagnosis names the filters it blames"
    assert "genuinely empty" in bare
    assert "no employer has opened a thread yet" in bare


def test_a_populated_inbox_carries_no_diagnosis_key_at_all():
    client = inbox_client()

    result = client.inbox.list_conversations(include_job=False)

    assert "diagnosis" not in result


def test_an_empty_page_past_the_end_is_reported_as_paging_and_not_as_an_empty_inbox():
    client = inbox_client(conversations=EMPTY)

    diagnosis = client.inbox.list_conversations(offset=90, include_job=False)["diagnosis"]

    assert "paged past the end" in diagnosis
    assert "90" in diagnosis


# ---------------------------------------------------------------------------
# read_conversation
# ---------------------------------------------------------------------------


def test_the_conv_id_is_the_only_thing_the_message_endpoint_is_asked_for():
    client = inbox_client(thread=THREAD)

    client.inbox.read_conversation(CONV_ID)

    assert client.routes.last_params(MESSAGES) == {"conv_id": [str(CONV_ID)]}
    assert client.routes.count(MESSAGES) == 1


def test_each_message_is_shaped_as_from_and_from_me_and_automated_and_sent_at_and_body():
    client = inbox_client(thread=THREAD)

    messages = client.inbox.read_conversation(CONV_ID)["messages"]

    assert [m["from"] for m in messages] == ["Priya", "you", "Instahyre (automated)"]
    assert [m["from_me"] for m in messages] == [False, True, False]
    assert [m["automated"] for m in messages] == [False, False, True]
    assert messages[0]["sent_at"] == "2026-08-18T09:14:02+00:00"
    assert messages[0]["body"].startswith("Hi Alex,")


def test_a_message_the_candidate_wrote_is_attributed_to_him_and_not_to_its_from_user():
    """is_owner true means he wrote it, whatever first_name the record carries."""
    client = inbox_client(thread=THREAD)

    owner = client.inbox.read_conversation(CONV_ID)["messages"][1]

    assert owner["from"] == "you"
    assert owner["from_me"] is True


def test_an_automated_message_is_a_third_category_and_not_a_recruiter():
    """Counting InstaBot as a recruiter would overstate how much human interest
    exists, which is the only number this whole tier is for."""
    client = inbox_client(thread=THREAD)

    automated = client.inbox.read_conversation(CONV_ID)["messages"][2]

    assert automated["automated"] is True
    assert automated["from"] == "Instahyre (automated)"
    assert automated["from_me"] is False


def test_message_bodies_are_stripped_to_text_so_no_markup_reaches_the_caller():
    """content_html is the only body field on the wire; there is no plain-text one."""
    client = inbox_client(thread=THREAD)

    messages = client.inbox.read_conversation(CONV_ID)["messages"]

    assert "<" in THREAD["objects"][0]["content_html"], "fixture no longer proves the point"
    for message in messages:
        assert "<" not in message["body"]
        assert ">" not in message["body"]
    assert "R&D" in messages[0]["body"], "html entities are unescaped, not left encoded"
    assert "- Stack: Node.js, TypeScript, PostgreSQL" in messages[0]["body"]


def test_a_body_over_the_character_budget_is_cut_and_flagged():
    client = inbox_client(thread=THREAD)

    messages = client.inbox.read_conversation(CONV_ID, body_chars=100)["messages"]

    assert messages[0]["body_truncated"] is True
    assert len(messages[0]["body"]) <= 104, "the marker adds 4 chars, nothing else"
    assert messages[0]["body"].endswith(" ...")


def test_a_body_inside_the_character_budget_is_not_flagged_as_truncated():
    """The control: body_truncated must mean something, so it cannot be always-on."""
    client = inbox_client(thread=THREAD)

    messages = client.inbox.read_conversation(CONV_ID, body_chars=100)["messages"]

    assert "body_truncated" not in messages[1]
    assert messages[1]["body"] == "Thursday after 4pm works. Sending over my availability now."


def test_the_default_body_budget_leaves_these_short_recruiter_bodies_intact():
    client = inbox_client(thread=THREAD)

    messages = client.inbox.read_conversation(CONV_ID)["messages"]

    assert DEFAULT_BODY_CHARS == 1500
    assert all("body_truncated" not in message for message in messages)


def test_the_show_message_gate_stops_the_loop_and_the_hidden_message_is_counted():
    """The site's render loop breaks on the first falsy show_message rather than
    skipping it, so mirroring it means the tail goes too. The count is what
    stops that from being silent."""
    client = inbox_client(thread=THREAD)

    result = client.inbox.read_conversation(CONV_ID)

    assert THREAD["objects"][-1]["show_message"] is False, "fixture no longer gates"
    assert result["count"] == 3
    assert len(result["messages"]) == 3
    assert result["withheld_by_show_message"] == 1
    assert all("Gated by the site" not in m["body"] for m in result["messages"])


def test_include_gated_returns_the_message_the_site_itself_would_hide():
    client = inbox_client(thread=THREAD)

    result = client.inbox.read_conversation(CONV_ID, include_gated=True)

    assert result["count"] == 4
    assert result["messages"][-1]["body"].startswith("Gated by the site")


def thread_gated_in_the_middle() -> dict:
    """The fixture's own four records, with the gated one moved to index 1."""
    objects = THREAD["objects"]
    return dict(THREAD, objects=[objects[0], objects[3], objects[1], objects[2]])


def test_the_gate_discards_everything_after_it_and_not_only_the_gated_message():
    """break, not continue -- and with the gated record LAST, where the fixture
    puts it, the two are indistinguishable. Moving it into the middle is the
    only arrangement that tells them apart, so this is the test that makes the
    one above mean something.
    """
    client = inbox_client(thread=thread_gated_in_the_middle())

    result = client.inbox.read_conversation(CONV_ID)

    assert result["count"] == 1, "a continue would have returned three"
    assert [m["from"] for m in result["messages"]] == ["Priya"]


def test_include_gated_recovers_the_whole_tail_the_break_would_have_discarded():
    client = inbox_client(thread=thread_gated_in_the_middle())

    result = client.inbox.read_conversation(CONV_ID, include_gated=True)

    assert result["count"] == 4
    assert [m["from"] for m in result["messages"]] == [
        "Priya",
        "Priya",
        "you",
        "Instahyre (automated)",
    ]


def test_a_thread_with_nothing_gated_reports_zero_withheld():
    """The control on the counter: a non-zero count has to mean something."""
    ungated = dict(THREAD, objects=THREAD["objects"][:3])
    client = inbox_client(thread=ungated)

    result = client.inbox.read_conversation(CONV_ID)

    assert result["count"] == 3
    assert result["withheld_by_show_message"] == 0


def test_unsent_messages_are_prepended_ahead_of_the_delivered_ones():
    """The site concatenates them in that order, unguarded. Appending them
    instead would put a draft after the reply it has not answered yet."""
    draft = {
        "show_message": True,
        "content_html": "<p>Draft the site has not delivered yet.</p>",
        "is_owner": True,
        "is_automated_message": False,
        "from_user": {"first_name": "Alex", "id": 990022},
        "to_user": None,
        "non_instahyre_to_user": None,
        "cc_emails": [],
        "created_at_date_time": "2026-08-17T08:00:00+00:00",
        "conversation_id": CONV_ID,
    }
    client = inbox_client(thread=dict(THREAD, unsent_messages=[draft]))

    messages = client.inbox.read_conversation(CONV_ID)["messages"]

    assert messages[0]["body"] == "Draft the site has not delivered yet."
    assert messages[1]["from"] == "Priya", "the delivered thread still follows, in order"
    assert len(messages) == 4


def test_the_thread_carries_its_envelope_fields_and_the_conversation_id_it_was_asked_for():
    client = inbox_client(thread=THREAD)

    result = client.inbox.read_conversation(CONV_ID)

    assert result["conv_id"] == CONV_ID
    assert result["starred"] is False
    assert result["recipients"] == "Priya (Northwind Analytics)"


def test_the_thread_says_its_read_side_effect_is_unverified_rather_than_promising_none():
    """The frontend never calls a mark-read endpoint yet decrements its badge,
    which is only coherent if the server marks read on this GET. Strong
    inference, no measurement -- so it ships labelled, not assumed."""
    client = inbox_client(thread=THREAD)

    note = client.inbox.read_conversation(CONV_ID)["read_side_effect"]

    assert note.startswith("UNVERIFIED")
    assert "POSSIBLY marking it read" in note


def test_a_non_integer_conv_id_is_refused_before_any_request_is_made():
    """Dialling out with garbage in the query is how an endpoint teaches you its
    contract at the cost of a request. Cheaper to refuse it here."""
    client = inbox_client(thread=THREAD)

    result = "sentinel"
    with pytest.raises(InvalidFilter) as excinfo:
        result = client.inbox.read_conversation("not-an-id")

    assert result == "sentinel", "read_conversation returned instead of raising"
    assert excinfo.value.field == "conv_id"
    assert "instahyre_list_conversations" in excinfo.value.message
    assert client.routes.count() == 0


def test_a_none_conv_id_is_refused_the_same_way_rather_than_becoming_conv_id_none():
    client = inbox_client(thread=THREAD)

    with pytest.raises(InvalidFilter):
        client.inbox.read_conversation(None)

    assert client.routes.count() == 0


def test_a_conv_id_that_is_a_numeric_string_is_accepted_and_sent_as_an_integer():
    """The control on the refusal above: it rejects garbage, not digits."""
    client = inbox_client(thread=THREAD)

    result = client.inbox.read_conversation(str(CONV_ID))

    assert result["conv_id"] == CONV_ID
    assert isinstance(result["conv_id"], int)
    assert client.routes.last_params(MESSAGES)["conv_id"] == [str(CONV_ID)]


# ---------------------------------------------------------------------------
# An empty thread is never left ambiguous
#
# The message endpoint has NO not-found signal. A conv_id that is not his
# answers 200 with ``{"objects": [], "meta": {...}}`` -- captured live, twice,
# see NOT_HIS -- which is key for key what a real thread with nothing said in
# it would look like. Returning ``ok`` with ``messages: []`` for both is the
# one thing errors.py forbids: a failure that looks like an empty result.
# ---------------------------------------------------------------------------


def test_a_conv_id_that_is_not_in_his_inbox_is_raised_as_not_found_not_returned_empty():
    """The bug. His inbox is empty, so 4242 cannot be his; the endpoint still
    answers 200 with an empty envelope, and the tool used to pass that straight
    through as a successful read of nothing."""
    client = inbox_client(thread=NOT_HIS, conversations=EMPTY)

    result = "sentinel"
    with pytest.raises(NotFound) as excinfo:
        result = client.inbox.read_conversation(4242)

    assert result == "sentinel", "read_conversation returned instead of raising"
    assert excinfo.value.kind == "not_found"
    assert "instahyre_list_conversations" in excinfo.value.message
    assert excinfo.value.context["conv_id"] == 4242


def test_the_not_found_verdict_is_the_conversation_list_and_not_the_bare_envelope():
    """The cross-check is what makes the verdict a measurement rather than an
    inference off three absent keys, so it has to have actually happened."""
    client = inbox_client(thread=NOT_HIS, conversations=EMPTY)

    with pytest.raises(NotFound):
        client.inbox.read_conversation(4242)

    assert client.routes.count(CONVERSATIONS) == 1
    # Unfiltered: a status or unread filter could hide a thread that IS his and
    # turn this into a false not-found.
    assert set(client.routes.last_params(CONVERSATIONS)) == {"limit", "offset"}


def test_an_id_that_is_in_his_own_list_is_a_real_thread_and_is_diagnosed_not_refused():
    """The control on the refusal: it has to be able to NOT fire. Same empty
    envelope, but the id is in his conversation list, so this is a genuine
    thread with nothing said in it."""
    client = inbox_client(thread=NOT_HIS, conversations=POPULATED)

    result = client.inbox.read_conversation(CONV_ID)

    assert result["count"] == 0
    assert result["messages"] == []
    assert str(CONV_ID) in result["diagnosis"]
    assert "not_found" not in result["diagnosis"]


def test_the_empty_thread_diagnosis_reports_that_the_envelope_carried_no_thread_frame():
    """recipients / starred / unsent_messages are the frame a real thread's
    payload carries. The live not-found capture has none of the three, and that
    evidence is the caller's, not just the module's."""
    client = inbox_client(thread=NOT_HIS, conversations=POPULATED)

    diagnosis = client.inbox.read_conversation(CONV_ID)["diagnosis"]

    assert "recipients" in diagnosis
    assert "starred" in diagnosis


def test_a_cross_check_that_cannot_be_completed_reports_both_readings():
    """When the list cannot be read, "not his" is not knowable -- and guessing
    it would be a worse bug than the one this fixes. Both readings ship."""
    client = inbox_client(
        thread=NOT_HIS, conversations=json_response({"detail": "boom"}, status=500)
    )

    result = client.inbox.read_conversation(4242)

    assert result["count"] == 0
    diagnosis = result["diagnosis"]
    assert "instahyre_list_conversations" in diagnosis
    assert "could not" in diagnosis.lower()


def test_a_thread_that_came_back_with_messages_is_never_cross_checked():
    """The cross-check costs a request, so it may only run on the one answer
    that needs it. A thread with records in it has already proved it exists."""
    client = inbox_client(thread=THREAD)

    result = client.inbox.read_conversation(CONV_ID)

    assert result["count"] == 3
    assert "diagnosis" not in result
    assert client.routes.count(CONVERSATIONS) == 0


def test_a_thread_whose_records_were_all_gated_is_not_cross_checked_either():
    """count is 0 here too, but the envelope had four records, so the thread
    demonstrably exists and withheld_by_show_message already says why."""
    gated_first = dict(THREAD, objects=[dict(obj, show_message=False) for obj in THREAD["objects"]])
    client = inbox_client(thread=gated_first)

    result = client.inbox.read_conversation(CONV_ID)

    assert result["count"] == 0
    assert result["withheld_by_show_message"] == 4
    assert "diagnosis" not in result
    assert client.routes.count(CONVERSATIONS) == 0


def test_the_read_side_effect_note_survives_on_the_not_found_path_free_result():
    """The honest UNVERIFIED note must not be lost to the new branch."""
    client = inbox_client(thread=NOT_HIS, conversations=POPULATED)

    result = client.inbox.read_conversation(CONV_ID)

    assert result["read_side_effect"].startswith("UNVERIFIED")
    assert "POSSIBLY marking it read" in result["read_side_effect"]


# ---------------------------------------------------------------------------
# An expired session is never an empty inbox
# ---------------------------------------------------------------------------


def unauthorised() -> object:
    return json_response(fixture_json("error_401.json"), status=401)


def test_an_expired_session_raises_instead_of_returning_an_empty_conversation_list():
    """The single worst failure this tier could have: 'you have no messages'
    when the truth is 'we are logged out'."""
    client = make_client(
        {CONVERSATIONS: unauthorised(), COUNTS: COUNT_PAYLOAD}, with_taxonomy=False
    )

    result = "sentinel"
    with pytest.raises(AuthRequired) as excinfo:
        result = client.inbox.list_conversations(include_job=False)

    assert result == "sentinel", "list_conversations returned instead of raising"
    assert excinfo.value.kind == "auth_required"
    assert "instahyre_login" in excinfo.value.message


def test_an_expired_session_on_the_message_endpoint_raises_instead_of_an_empty_thread():
    client = make_client({MESSAGES: unauthorised()}, with_taxonomy=False)

    result = "sentinel"
    with pytest.raises(AuthRequired):
        result = client.inbox.read_conversation(CONV_ID)

    assert result == "sentinel", "read_conversation returned instead of raising"


def test_an_expired_session_on_the_count_endpoint_raises_when_it_is_asked_directly():
    client = make_client({COUNTS: unauthorised()}, with_taxonomy=False)

    with pytest.raises(AuthRequired):
        client.inbox.conversation_counts()


def test_a_dead_count_endpoint_does_not_take_the_conversation_listing_down_with_it():
    """Counts are a garnish on the listing. Losing them must cost the totals,
    not the messages."""
    client = inbox_client(counts=unauthorised())

    result = client.inbox.list_conversations(include_job=False)

    assert result["count"] == 3
    assert result["unread_total"] is None
    assert result["starred_total"] is None


# ---------------------------------------------------------------------------
# The job join
# ---------------------------------------------------------------------------


def test_include_job_false_makes_zero_job_detail_requests():
    """A conversation record carries no company, so the join is a real cost --
    one request per distinct job. Turning it off has to actually turn it off."""
    client = inbox_client()

    result = client.inbox.list_conversations(include_job=False)

    assert client.routes.paths == [CONVERSATIONS, COUNTS]
    assert all("employer_public_jobs" not in path for path in client.routes.paths)
    assert all("company" not in record for record in result["conversations"])


def test_include_job_true_joins_the_company_and_the_title_onto_every_conversation():
    client = inbox_client(job_routes())

    result = client.inbox.list_conversations(include_job=True)

    joined = [(r["company"], r["title"]) for r in result["conversations"]]
    assert joined == [JOBS[601001], JOBS[601002], JOBS[601003]]
    assert [path for path in client.routes.paths if "employer_public_jobs" in path] == [
        detail_path(601001),
        detail_path(601002),
        detail_path(601003),
    ]


def test_two_conversations_on_one_job_cost_one_detail_request_between_them():
    """The join is cached per job id, so a busy thread pair is not two requests."""
    objects = [dict(POPULATED["objects"][0]), dict(POPULATED["objects"][1], job_id=601001)]
    client = inbox_client(
        {detail_path(601001): job_detail(601001)},
        conversations=dict(POPULATED, objects=objects),
    )

    result = client.inbox.list_conversations(include_job=True)

    assert client.routes.count(detail_path(601001)) == 1
    assert [r["company"] for r in result["conversations"]] == [JOBS[601001][0]] * 2


def test_a_job_that_cannot_be_fetched_costs_its_conversation_a_company_not_the_listing():
    """Best-effort by design: a pulled listing must not delete the thread from
    the answer, and the caller is told which one lost its join."""
    routes = job_routes()
    routes[detail_path(601002)] = html_response()
    client = inbox_client(routes)

    records = client.inbox.list_conversations(include_job=True)["conversations"]

    assert len(records) == 3
    assert records[1]["company"] is None
    assert records[1]["job_lookup_error"] == "not_found"
    assert records[0]["company"] == JOBS[601001][0]
    assert records[2]["company"] == JOBS[601003][0]


# ---------------------------------------------------------------------------
# Contract drift is loud
# ---------------------------------------------------------------------------


def test_a_conversation_payload_without_objects_is_drift_and_not_an_empty_inbox():
    client = inbox_client(conversations={"meta": EMPTY["meta"]})

    with pytest.raises(ApiError) as excinfo:
        client.inbox.list_conversations(include_job=False)

    assert "'objects' key" in excinfo.value.message
    assert excinfo.value.context["path"] == CONVERSATIONS


def test_a_count_payload_without_conv_count_is_drift_and_not_three_zeroes():
    client = inbox_client(counts={"success": True})

    with pytest.raises(ApiError) as excinfo:
        client.inbox.conversation_counts()

    assert "'conv_count' key" in excinfo.value.message


def test_a_message_payload_without_objects_is_drift_and_not_an_empty_thread():
    client = inbox_client(thread={"unsent_messages": [], "starred": False})

    with pytest.raises(ApiError) as excinfo:
        client.inbox.read_conversation(CONV_ID)

    assert "'objects' key" in excinfo.value.message
    assert excinfo.value.context["path"] == MESSAGES
