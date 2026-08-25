"""The five captured write surfaces, held to the gate their module promises.

``writes.py`` reaches three places nothing else in this package reaches: a human
support queue, a saved-search row on his account, and -- the one that matters --
the inboxes of real people who know him, over his name, with no unsend anywhere
in Instahyre's product. The module's own docstring says the gate "sends NOTHING"
and that there is "no path through this module that sends without having first
been able to show" the recipients. Everything below exists to turn those two
sentences into measurements.

The shape of the argument, inherited from ``test_inbound_safety.py`` and
``test_profile_write.py`` and sharpened in one place:

1. **The gate is asserted at the WIRE, not at the return value.** A test that
   only caught the refusal would pass against an implementation that sends
   first and refuses afterwards. So every gate test runs against an HTTP double
   whose ``post`` and ``patch`` DETONATE, and additionally reads the recorded
   request list off the mock transport. Two independent instruments, because a
   gate is exactly the kind of thing that gets certified by an instrument that
   cannot fail.

2. **Every checker this file relies on is SHOWN DISCRIMINATING.** The four
   ``__CONTROL`` tests each build the broken artifact by hand -- an ungated
   function, a preview that counts three and lists two, a body whose sender is
   an invitee, a flag-only PATCH -- and assert the checker rejects it while
   accepting the real one. A check that has only ever been seen passing
   certifies nothing, which is this repo's standing rule and the reason the
   checkers are named functions rather than inline expressions: the control has
   to be able to run the SAME code the real assertion runs.

3. **The code is tied to the recording, not to a paraphrase of it.**
   ``test_the_body_matches_the_wire_capture`` reads
   ``fixtures/write_contracts/support_ticket.json`` -- the request the page
   itself serialized, aborted at the router -- and compares keys and the
   resource-URI SHAPE of ``candidate`` against what this code builds. That is
   what stops the module drifting away from the only evidence it has.

Nothing here touches the network or the real ``_state/``: conftest builds every
client on an ``httpx.MockTransport``, makes the genuine transports raise, and
redirects ``INSTAHYRE_HOME`` to a tmp dir. The last test in this file asserts
both halves of that premise out loud.
"""

from __future__ import annotations

import copy
import inspect
import json
import re

import httpx
import pytest

from conftest import credential_strings_in, fixture_json, make_client
from instahyre_server import constants as C
from instahyre_server import server as server_module
from instahyre_server import writes as writes_module
from instahyre_server.errors import InstahyreError
from instahyre_server.writes import MAX_INVITES_PER_CALL, NothingToDo, Writer

# ---------------------------------------------------------------------------
# Fixtures and the routes every writer needs
# ---------------------------------------------------------------------------

EDUCATION = fixture_json("education.json")
PROFILE = fixture_json("candidate_profile.json")

#: Recovered the same way the package recovers it -- off the education row's
#: owner URI -- rather than retyped, so a change to the fixture cannot leave
#: this file quietly asserting against a candidate that no longer exists.
CANDIDATE_ID = int(
    re.search(r"/candidate/(\d+)", EDUCATION["objects"][0]["candidate"]).group(1)
)
PROFILE_PATH = C.EP_PROFILE.format(candidate_id=CANDIDATE_ID)

#: His own identity, as ``_referrer`` reads it. Pinned from the fixture so the
#: "the sender is HIM" assertions compare against one source.
REFERRER_NAME = PROFILE["user"]["full_name"]
REFERRER_EMAIL = PROFILE["user"]["email"]

#: The wire capture. This is the request Instahyre's own page built and
#: serialized on 2026-08-23, recorded and aborted at the router.
SUPPORT_CAPTURE = fixture_json("write_contracts/support_ticket.json")

WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")

# --- saved searches, hand-built and admitting it ----------------------------
#
# HAND-BUILT, NOT CAPTURED, and for the same reason the shipped
# ``saved_searches_populated.json`` fixture says so in its own payload: this
# account holds ZERO saved searches, so no live record exists to golden. That
# fixture cannot be used here, because it deliberately omits ``search_string``
# -- a field name it had no evidence for -- and ``toggle_job_alert`` refuses to
# build a body without one. The ids and the query strings below are invented.
# What is NOT invented is the pair of field names the toggle actually turns on
# (``search_string`` and ``job_alert_enabled_at``), which are the two keys
# ``constants.SAVED_SEARCH_TOGGLE_BODY_KEYS`` records off the shipped bundle.

#: Three counted filters (``job_categories`` is excluded by the gate), so the
#: enable gate passes on this row.
GATE_PASSING_QUERY = "job_functions=3&locations=7&min_experience=4&job_categories=9"

#: One counted filter, so the enable gate refuses on this row.
GATE_FAILING_QUERY = "job_functions=3&job_categories=9"

PASSING_ID = 91
FAILING_ID = 92

SAVED_SEARCH_ROWS = [
    {
        "id": PASSING_ID,
        "resource_uri": "%s%s%d" % ("/api/v1", C.EP_SAVED_SEARCH_DETAIL, PASSING_ID),
        "search_string": GATE_PASSING_QUERY,
        "job_alert_enabled_at": None,
    },
    {
        "id": FAILING_ID,
        "resource_uri": "%s%s%d" % ("/api/v1", C.EP_SAVED_SEARCH_DETAIL, FAILING_ID),
        "search_string": GATE_FAILING_QUERY,
        "job_alert_enabled_at": "2026-08-01T06:12:00+00:00",
    },
]


def saved_search_envelope(rows):
    """The tastypie envelope, copied in shape from the real empty capture."""
    return {
        "meta": {
            "offset": 0,
            "limit": 50,
            "total_count": len(rows),
            "previous": None,
            "next": None,
        },
        "objects": copy.deepcopy(rows),
    }


# ---------------------------------------------------------------------------
# The HTTP double that DETONATES on a write
# ---------------------------------------------------------------------------


class WriteAttempted(BaseException):
    """Raised the instant a gated path reaches ``post`` or ``patch``.

    Derived from ``BaseException`` rather than ``Exception`` ON PURPOSE. The
    thing being tested is a safety gate, and the failure mode that matters is a
    write that escapes and is then swallowed by some ``except Exception`` on the
    way back up -- the MCP ``@handled`` decorator is exactly such a handler. A
    detonation that could be caught by ordinary error handling would be an alarm
    with a mute button on it.
    """


class NoWriteHTTP:
    """An HTTP double whose ``post`` and ``patch`` raise instead of sending.

    ``get`` delegates to the real mocked client underneath, so the reads these
    paths legitimately make (the profile, the education row, the saved-search
    list) still answer and the code under test runs its real course. Only the
    write verbs are replaced.

    WHY BOTH THIS AND THE RECORDED REQUEST LIST. The route table already turns
    an unmocked write into an ``AssertionError``, but that is a check on a
    transport this file could accidentally wire up; this is a check on the
    ``http`` object the module actually holds. They fail at different distances
    from the bug, and the pair is what makes "no non-GET AT ALL" a measurement
    rather than a hope. Its own ability to fire is measured by
    ``test_the_no_send_assertion_can_fail__CONTROL``.
    """

    def __init__(self, inner) -> None:
        self.inner = inner
        self.attempts: list = []

    # -- reads pass through -------------------------------------------------

    def get(self, path, **kwargs):
        return self.inner.get(path, **kwargs)

    def __getattr__(self, name):
        # Anything else the module reaches for -- cookies, base_url -- comes
        # from the real client. Only the two write verbs are overridden, and
        # they are defined explicitly below so this fallback can never serve
        # them.
        return getattr(self.inner, name)

    # -- writes detonate ----------------------------------------------------

    def post(self, path, **kwargs):
        self.attempts.append(("POST", path, kwargs))
        raise WriteAttempted("POST %s -- a gated path sent a write." % path)

    def patch(self, path, **kwargs):
        self.attempts.append(("PATCH", path, kwargs))
        raise WriteAttempted("PATCH %s -- a gated path sent a write." % path)


def _ungated_support_ticket(http, message):
    """A DELIBERATELY UNGATED write, and the only reason it exists is the control.

    No ``confirm`` parameter, no preview, no refusal -- it just sends. This is
    the broken variant ``test_the_no_send_assertion_can_fail__CONTROL`` points
    the harness at, so that "the harness saw no write" is a statement about the
    gate rather than a statement about a harness that cannot see writes.
    """
    return http.post(C.EP_SUPPORT_QUERY, json_body={"message": message})


# ---------------------------------------------------------------------------
# Client constructors
# ---------------------------------------------------------------------------


class BodyRecorder:
    """A route that records the body it was handed and answers with ``response``."""

    def __init__(self, response=None, *, expect="POST"):
        self.response = {"success": True} if response is None else response
        self.expect = expect
        self.calls: list = []

    def __call__(self, request):
        if request.method != self.expect:
            raise AssertionError(
                "route expected %s, received %s" % (self.expect, request.method)
            )
        self.calls.append(json.loads(request.content))
        return self.response


def writer_client(extra_routes=None, *, rows=SAVED_SEARCH_ROWS, contacts=None):
    """A client with every READ a writer needs, and no write route wired.

    Taxonomy is left unwired for the same reason ``test_profile_write`` leaves
    it unwired: a write has no business resolving a location, so a stray
    taxonomy read is a loud "Unmocked request" rather than a silent success.
    A write route is only added when a test explicitly asks for one -- so a
    write that escaped a gate hits an unmocked path and is recorded as a fact.
    """
    routes = {
        C.EP_EDUCATION: EDUCATION,
        PROFILE_PATH: PROFILE,
        C.EP_SAVED_SEARCHES: saved_search_envelope(rows),
        C.EP_REFERRAL_CONTACTS: {"data": list(contacts or [])},
    }
    routes.update(extra_routes or {})
    return make_client(routes, with_taxonomy=False)


def detonating_writer(**kwargs):
    """``(writer, http)`` where the writer's ``http`` explodes on any write.

    The double is installed on the WRITER only. ``Inbound`` reads through the
    genuine mocked client underneath, so the reads a preview makes are real
    reads and the recorded request list stays the true record of the wire.
    """
    client = writer_client(**kwargs)
    http = NoWriteHTTP(client.http)
    writer = Writer(http, client.store, client.inbound)
    return writer, http, client


def write_requests(client):
    return [r for r in client.routes.requests if r.method in WRITE_METHODS]


def describe(requests):
    return [(r.method, r.url.path) for r in requests]


# ---------------------------------------------------------------------------
# The named checkers
#
# Each of these is used by a real assertion AND by its control. That sharing is
# the point: a control that re-implemented the check would only prove its own
# copy discriminates, which is not the claim being made.
# ---------------------------------------------------------------------------


def recipients_named(preview) -> list:
    """Every address the preview actually NAMES, read out of ``would_contact``."""
    return [entry["email"] for entry in preview["would_contact"]]


def preview_names_everyone(preview, expected) -> bool:
    """True iff the preview NAMES every expected recipient -- not merely counts them.

    Both halves are required, and the second is the one with teeth: a preview
    that reported ``recipient_count: 3`` while listing two people would be a
    consent gate that has quietly stopped obtaining consent for somebody. Shown
    rejecting exactly that in
    ``test_the_recipient_list_is_not_a_count__CONTROL``.
    """
    named = recipients_named(preview)
    return sorted(named) == sorted(expected) and preview["recipient_count"] == len(named)


def sender_is_the_referrer(body, referrer_email) -> bool:
    """True iff the body's top-level identity is HIS and no invitee wears it.

    ``writes.py`` warns about this misreading in as many words: the top-level
    ``name``/``email`` are the REFERRER, and ``friends`` are the invitees. A
    body that swapped them would mail his contacts a referral from one of
    themselves, using his session. Shown rejecting the swapped body in
    ``test_the_top_level_name_and_email_are_the_REFERRER__CONTROL``.
    """
    invitees = {
        (friend.get("email") or "").lower() for friend in body.get("friends") or []
    }
    return body.get("email") == referrer_email and body.get("email", "").lower() not in invitees


def patch_body_is_complete(body) -> bool:
    """True iff the PATCH is the shape ``toggleAlerts`` sends -- flag AND query.

    Instahyre's own toggle always sends ``search_string`` alongside
    ``job_alert_enabled_at``. A flag-only ``{id, job_alert_enabled_at}`` is a
    request the site never makes, and this server's whole premise is that it
    only ever sends requests the site makes. Shown rejecting the flag-only body
    in ``test_the_patch_body_is_not_flag_only__CONTROL``.
    """
    return (
        tuple(sorted(body)) == tuple(sorted(C.SAVED_SEARCH_TOGGLE_BODY_KEYS))
        and bool(body.get("search_string"))
    )


#: A mention of "frequency" is only acceptable if it is a DENIAL. Matches both
#: the ``no_frequency`` key and the sentence under it; matches nothing that
#: offers a schedule.
FREQUENCY_IS_DENIED = re.compile(r"no[ _](alert[ _])?frequency", re.IGNORECASE)


def frequency_mentions(payload) -> list:
    """``(trail, text)`` for every string anywhere in ``payload`` saying "frequency".

    Uses conftest's wide walker rather than a str-only walk, so a mention
    hiding in a dict KEY -- which is exactly where the honest one lives -- is
    still seen.
    """
    return [
        (trail, text)
        for trail, text in credential_strings_in(payload)
        if "frequency" in text.lower()
    ]


# ===========================================================================
# A. The gate, on every one of the five
# ===========================================================================

#: The four gated writers, by name, with an argument tuple that reaches the
#: send. Named rather than positional so a failure says which surface leaked.
GATED_WRITES = [
    ("support_ticket", ("Something is broken.",)),
    ("toggle_job_alert", (PASSING_ID, True)),
    ("referral_link", ()),
    ("send_referral_invites", (["someone@example.com"],)),
]

GATED_TOOLS = [
    server_module.instahyre_support_ticket,
    server_module.instahyre_toggle_job_alert,
    server_module.instahyre_referral_link,
    server_module.instahyre_send_referral_invites,
]


@pytest.mark.parametrize("method_name,_args", GATED_WRITES, ids=[n for n, _ in GATED_WRITES])
def test_confirm_defaults_to_false_on_every_writer_method(method_name, _args):
    """Read out of the SIGNATURE, never out of the docstring.

    A docstring that says "the default is False" is a claim; the signature is
    the thing that decides. They are asserted apart because they can disagree,
    and the disagreement is invisible in review.
    """
    parameters = inspect.signature(getattr(Writer, method_name)).parameters
    assert parameters["confirm"].default is False


@pytest.mark.parametrize("tool", GATED_TOOLS, ids=lambda t: t.__name__)
def test_every_gated_mcp_tool_defaults_confirm_to_false(tool):
    """The same question one layer up, where the model actually calls in."""
    assert inspect.signature(tool).parameters["confirm"].default is False


def test_the_contacts_tool_has_no_confirm_because_it_is_a_read():
    """A confirm on a read would be theatre, and theatre teaches a caller to
    click through gates. Its absence is asserted rather than assumed."""
    assert "confirm" not in inspect.signature(Writer.referral_contacts).parameters
    assert "confirm" not in inspect.signature(server_module.instahyre_referral_contacts).parameters


def test_referral_contacts_issues_only_a_get():
    writer, http, client = detonating_writer(
        contacts=[{"name": "Ann", "email": "ann@example.com", "preselect": False}]
    )

    writer.referral_contacts()

    assert http.attempts == [], "the read reached a write verb"
    assert describe(write_requests(client)) == []
    assert [r.method for r in client.routes.requests] == ["GET"]


@pytest.mark.parametrize("method_name,args", GATED_WRITES, ids=[n for n, _ in GATED_WRITES])
def test_without_confirm_no_non_get_request_is_issued_at_all(method_name, args):
    """The load-bearing half of the gate, on all four surfaces.

    The returned preview proves nothing on its own: an implementation that sent
    the request and then described it would satisfy any assertion about the
    return value. What matters is that neither instrument saw a write -- the
    detonator never fired, and the transport recorded none.
    """
    writer, http, client = detonating_writer()

    result = getattr(writer, method_name)(*args)

    assert http.attempts == [], "confirm=False reached a write verb"
    assert describe(write_requests(client)) == []
    assert result.get("confirmed") is False


@pytest.mark.parametrize("method_name,args", GATED_WRITES, ids=[n for n, _ in GATED_WRITES])
def test_omitting_confirm_entirely_means_refusal_not_unspecified(method_name, args):
    """Omission must mean "do not send", not "unspecified, so proceed"."""
    writer, http, _client = detonating_writer()

    getattr(writer, method_name)(*args)

    assert http.attempts == []


def test_the_no_send_assertion_can_fail__CONTROL():
    """The harness above is shown DISCRIMINATING, both halves in one test.

    Without this, ``test_without_confirm_no_non_get_request_is_issued_at_all``
    is a green light with nothing behind it: a double whose ``post`` was never
    reachable would report exactly the same silence as a gate that works.

    Good variant: the real, gated ``support_ticket`` -- no detonation.
    Broken variant: ``_ungated_support_ticket``, which has no gate at all --
    detonates, and the attempt is recorded with the endpoint it aimed at.
    """
    writer, http, client = detonating_writer()

    # The GOOD half: the gate holds and the harness stays silent.
    writer.support_ticket("Something is broken.")
    assert http.attempts == []
    assert describe(write_requests(client)) == []

    # The BROKEN half: the same harness, an ungated caller, and the alarm fires.
    with pytest.raises(WriteAttempted) as excinfo:
        _ungated_support_ticket(http, "Something is broken.")

    assert C.EP_SUPPORT_QUERY in str(excinfo.value)
    assert [(method, path) for method, path, _kwargs in http.attempts] == [
        ("POST", C.EP_SUPPORT_QUERY)
    ], "the harness recorded the write it caught"


# ===========================================================================
# B. send_referral_invites -- the one that reaches real people
# ===========================================================================

THREE_ADDRESSES = ["ann@example.com", "bo@example.com", "cy@example.com"]


def test_the_preview_names_every_single_recipient():
    """Three addresses in, three people named. Not a count, not a sample.

    This is the assertion the whole confirm gate on this surface exists to
    support: the human is being asked to consent to a specific list of people,
    so the list has to be in front of him.
    """
    writer, _http, _client = detonating_writer()

    preview = writer.send_referral_invites(THREE_ADDRESSES)

    assert preview_names_everyone(preview, THREE_ADDRESSES)
    assert recipients_named(preview) == THREE_ADDRESSES
    assert preview["recipient_count"] == 3


def test_the_recipient_list_is_not_a_count__CONTROL():
    """``preview_names_everyone`` is shown rejecting a preview that counts three
    and lists two.

    The broken payload is built by hand rather than produced, because the
    module does not currently produce it -- which is the whole point: the
    checker has to be able to catch a regression that has not happened yet. A
    checker that only read ``recipient_count`` would pass this payload, and a
    caller reading it would consent to three invitations while seeing two
    names.
    """
    counts_three_lists_two = {
        "would_contact": [
            {"name": None, "email": "ann@example.com", "how": "typed address, no name sent"},
            {"name": None, "email": "bo@example.com", "how": "typed address, no name sent"},
        ],
        "recipient_count": 3,
    }
    honest = {
        "would_contact": [
            {"name": None, "email": address, "how": "typed address, no name sent"}
            for address in THREE_ADDRESSES
        ],
        "recipient_count": 3,
    }

    assert not preview_names_everyone(counts_three_lists_two, THREE_ADDRESSES), (
        "the checker accepted a preview that counted three people and named two"
    )
    assert preview_names_everyone(honest, THREE_ADDRESSES), (
        "the checker rejected an honest preview, so its refusal above means nothing"
    )


def test_a_malformed_address_raises_and_sends_nothing():
    """BOTH halves are asserted, because either alone is satisfiable by a bug.

    The raise alone would pass against an implementation that sent the batch and
    complained afterwards; the silence alone would pass against one that
    dropped the bad address and mailed the rest.
    """
    writer, http, client = detonating_writer()

    with pytest.raises(NothingToDo) as excinfo:
        writer.send_referral_invites(["ann@example.com", "not-an-email"])

    assert "not-an-email" in excinfo.value.message
    assert http.attempts == []
    assert describe(write_requests(client)) == []


def test_duplicates_are_removed_before_the_count():
    """Case and stray spaces do not make a second person.

    Deduplication has to happen BEFORE the count, or the preview shows a number
    the send does not produce -- and on this surface the number IS the consent.
    """
    writer, _http, _client = detonating_writer()

    preview = writer.send_referral_invites(["a@x.com", "A@X.com "])

    assert recipients_named(preview) == ["a@x.com"]
    assert preview["recipient_count"] == 1
    assert preview["would_send"]["body"]["email_list"] == "a@x.com"


def test_spaces_are_stripped_from_anywhere_not_just_the_ends():
    """``"a b@x.com"`` becomes ``"ab@x.com"``, which is what Instahyre does.

    ``constructInvitationsDict`` runs ``item.replace(/ /g,'')`` -- every space,
    not the ends. A ``strip()`` would preview ``"a b@x.com"`` and the platform
    would act on ``"ab@x.com"``: a different person, previewed as the intended
    one. The interior space is the case that separates the two implementations,
    so it is the case asserted.
    """
    writer, _http, _client = detonating_writer()

    preview = writer.send_referral_invites(["a b@x.com"])

    assert recipients_named(preview) == ["ab@x.com"]
    assert preview["would_send"]["body"]["email_list"] == "ab@x.com"
    assert preview["would_send"]["body"]["friends"] == [{"name": None, "email": "ab@x.com"}]
    assert C.REFERRAL_STRIPS_ALL_SPACES is True


def test_more_than_the_cap_raises_and_sends_nothing():
    writer, http, client = detonating_writer()
    too_many = ["p%d@example.com" % index for index in range(MAX_INVITES_PER_CALL + 1)]

    with pytest.raises(NothingToDo) as excinfo:
        writer.send_referral_invites(too_many)

    assert str(MAX_INVITES_PER_CALL) in excinfo.value.message
    assert http.attempts == []
    assert describe(write_requests(client)) == []


def test_an_empty_list_raises_and_sends_nothing():
    """Nobody to invite is a refusal, not an empty send. A request with an empty
    ``friends`` list is still a request, and this surface does not make ones
    nobody asked for."""
    writer, http, client = detonating_writer()

    with pytest.raises(NothingToDo):
        writer.send_referral_invites([])

    assert http.attempts == []
    assert describe(write_requests(client)) == []


def test_every_sent_friend_carries_a_name_of_exactly_none():
    """``is None``, not merely falsy.

    Instahyre's typed-invite path sends ``{'name': null, ...}``. An empty string
    would be a different body, and a GUESSED name would be worse than either --
    it dresses up the consent this gate exists to obtain.
    """
    invites = BodyRecorder()
    client = writer_client({C.EP_REFERRAL_INVITES: invites})

    client.writer.send_referral_invites(THREE_ADDRESSES, confirm=True)

    assert len(invites.calls) == 1
    friends = invites.calls[0]["friends"]
    assert [friend["email"] for friend in friends] == THREE_ADDRESSES
    for friend in friends:
        assert friend["name"] is None
        assert sorted(friend) == ["email", "name"]


def test_the_top_level_name_and_email_are_the_REFERRER__CONTROL():
    """``sender_is_the_referrer`` is shown rejecting the swapped body.

    This is the specific misreading ``writes.py`` warns about: the top-level
    identity is HIS, and ``friends`` are the invitees. A body that put an
    invitee's address at the top level would send his contacts a referral
    apparently from one of themselves, over his session -- and it would still be
    a 200. Nothing about the response would say anything was wrong, which is why
    the check lives here.
    """
    invites = BodyRecorder()
    client = writer_client({C.EP_REFERRAL_INVITES: invites})

    client.writer.send_referral_invites(THREE_ADDRESSES, confirm=True)
    real_body = invites.calls[0]

    # The GOOD half: the real body passes, and the sender is provably him.
    assert sender_is_the_referrer(real_body, REFERRER_EMAIL)
    assert real_body["name"] == REFERRER_NAME
    assert real_body["email"] == REFERRER_EMAIL
    assert real_body["email"] not in [friend["email"] for friend in real_body["friends"]]

    # The BROKEN half: swap an invitee into the sender slot; the check refuses.
    swapped = copy.deepcopy(real_body)
    swapped["email"] = THREE_ADDRESSES[0]
    swapped["name"] = None
    assert not sender_is_the_referrer(swapped, REFERRER_EMAIL), (
        "the checker accepted a body whose sender was one of the invitees"
    )


def test_a_confirmed_send_posts_the_captured_path_and_exactly_the_captured_keys():
    invites = BodyRecorder()
    client = writer_client({C.EP_REFERRAL_INVITES: invites})

    result = client.writer.send_referral_invites(THREE_ADDRESSES, confirm=True)

    posted = [r for r in client.routes.requests if r.method == "POST"]
    assert [r.url.path for r in posted] == ["/api/v1" + C.EP_REFERRAL_INVITES]
    assert tuple(sorted(invites.calls[0])) == tuple(sorted(C.REFERRAL_INVITE_BODY_KEYS))
    assert result["confirmed"] is True
    assert result["sent"]["body"] == invites.calls[0], (
        "what the result reports as sent is what actually went out"
    )


# ===========================================================================
# C. toggle_job_alert
# ===========================================================================


def test_zero_saved_searches_is_diagnosed_rather_than_shrugged_at():
    """A real zero and a failed read must not be the same answer.

    So the assertion is on the EXPLANATION, not just the conclusion: the result
    has to name the mechanism that makes a swallowed failure unreachable -- the
    200, the tastypie envelope, the ``AuthRequired`` a dead session raises
    inside the HTTP client -- because a reader can check a mechanism and cannot
    check an assurance.
    """
    writer, http, client = detonating_writer(rows=[])

    result = writer.toggle_job_alert(PASSING_ID, True)

    assert result["changed"] is False
    assert result["saved_search_count"] == 0
    assert result["diagnosis"]["reason"] == "never_saved_one"

    why = result["why_nothing_to_toggle"].lower()
    for mechanism in ("200", "envelope", "authrequired", "apierror"):
        assert mechanism in why, "the explanation does not name %r" % mechanism

    assert http.attempts == []
    assert describe(write_requests(client)) == []


def test_an_unknown_id_raises_names_the_ids_that_exist_and_sends_nothing():
    writer, http, client = detonating_writer()

    with pytest.raises(NothingToDo) as excinfo:
        writer.toggle_job_alert(404404, True)

    message = excinfo.value.message
    assert "404404" in message
    for row in SAVED_SEARCH_ROWS:
        assert str(row["id"]) in message, "the refusal did not name id %r" % row["id"]
    assert http.attempts == []
    assert describe(write_requests(client)) == []


def test_enabling_below_the_filter_gate_refuses_and_sends_nothing():
    """Asserted with ``confirm=True``, deliberately.

    The filter gate is not the confirm gate. Running it with confirm already
    given is what shows it is a second, independent refusal rather than the
    first one wearing a different message.
    """
    writer, http, client = detonating_writer()

    result = writer.toggle_job_alert(FAILING_ID, True, confirm=True)

    assert result["changed"] is False
    assert result["gate"]["passes"] is False
    assert result["gate"]["non_empty_filters_counted"] < C.SAVED_SEARCH_ALERT_MIN_FILTERS
    assert "canEnableJobAlerts" in result["refused"]
    assert "would_send" not in result, "a refusal must not read as a pending request"
    assert http.attempts == []
    assert describe(write_requests(client)) == []


def test_the_patch_body_is_not_flag_only__CONTROL():
    """``patch_body_is_complete`` is shown rejecting the flag-only body.

    Instahyre's ``toggleAlerts`` always sends the query string alongside the
    flag. A ``{id, job_alert_enabled_at}`` PATCH is a request the site never
    makes -- and a server that sends requests the site never makes has left the
    only evidence it has. The broken body is built by hand because the module
    does not produce it, which is exactly why the checker has to be shown
    catching it.
    """
    patches = BodyRecorder(response={"id": PASSING_ID}, expect="PATCH")
    detail = C.EP_SAVED_SEARCH_DETAIL + str(PASSING_ID)
    client = writer_client({detail: patches})

    preview = client.writer.toggle_job_alert(PASSING_ID, True)
    client.writer.toggle_job_alert(PASSING_ID, True, confirm=True)

    # The GOOD half, on both the promised body and the one that went out.
    assert patch_body_is_complete(preview["would_send"]["body"])
    assert patch_body_is_complete(patches.calls[0])
    assert patches.calls[0]["search_string"] == GATE_PASSING_QUERY
    assert patches.calls[0] == preview["would_send"]["body"], (
        "what went out is what the preview promised"
    )

    # The BROKEN half: a flag-only PATCH, refused.
    flag_only = {"id": PASSING_ID, "job_alert_enabled_at": True}
    assert not patch_body_is_complete(flag_only), (
        "the checker accepted a flag-only PATCH the site never sends"
    )
    # And a body that carries the key but empties it is refused too, so the
    # check is about the VALUE reaching the wire rather than the key existing.
    assert not patch_body_is_complete(
        {"id": PASSING_ID, "job_alert_enabled_at": True, "search_string": ""}
    )


def test_disabling_is_not_blocked_by_the_filter_gate():
    """The gate guards ENABLING only, and that asymmetry is Instahyre's.

    A gate that also blocked disabling would trap an alert on a search he can no
    longer switch off -- which would be this server inventing a restriction the
    platform does not have. Run on the row that FAILS the gate, so a symmetric
    implementation would be caught here.
    """
    writer, http, client = detonating_writer()

    result = writer.toggle_job_alert(FAILING_ID, False)

    assert "refused" not in result
    assert result["gate"]["passes"] is False, "this row really does fail the gate"
    assert result["would_send"]["body"]["job_alert_enabled_at"] is False
    assert result["would_become"] is False
    assert http.attempts == []
    assert describe(write_requests(client)) == []


def test_no_result_from_this_tool_ever_offers_an_alert_frequency():
    """Every mention of "frequency" anywhere in any result is a DENIAL.

    ``SAVED_SEARCH_HAS_FREQUENCY`` is False: no field for one exists in the
    resource, the toggle, or the UI. The failure this guards against is not a
    bug in the request -- it is a result that reads as though a daily-or-weekly
    choice were available, which would send the caller looking for a control
    that does not exist.

    The walker is conftest's wide one, so a mention hiding in a dict KEY is
    seen; and the preview is asserted to carry at least one denial, so a
    refactor that deleted the note cannot make this pass vacuously.
    """
    patches = BodyRecorder(response={"id": PASSING_ID}, expect="PATCH")
    detail = C.EP_SAVED_SEARCH_DETAIL + str(PASSING_ID)
    client = writer_client({detail: patches})
    empty = writer_client(rows=[])

    results = [
        client.writer.toggle_job_alert(PASSING_ID, True),
        client.writer.toggle_job_alert(FAILING_ID, False),
        client.writer.toggle_job_alert(FAILING_ID, True, confirm=True),
        client.writer.toggle_job_alert(PASSING_ID, True, confirm=True),
        empty.writer.toggle_job_alert(PASSING_ID, True),
    ]

    for result in results:
        for trail, text in frequency_mentions(result):
            assert FREQUENCY_IS_DENIED.search(text), (
                "%s offers a frequency this platform does not have: %r" % (trail, text)
            )

    assert frequency_mentions(results[0]), (
        "the preview no longer denies a frequency at all, so this test just "
        "passed without checking anything"
    )
    assert C.SAVED_SEARCH_HAS_FREQUENCY is False


# ===========================================================================
# D. support_ticket -- tie the code to the wire capture
# ===========================================================================


def test_the_body_matches_the_wire_capture():
    """The recording is the contract, and this is the test that keeps them tied.

    Two things are compared, and the second is the one a guess got wrong: the
    KEY SET, and the fact that ``candidate`` is a RESOURCE URI STRING rather
    than the integer id it looks like it should be. The expected prefix is
    derived from the capture rather than retyped, so this cannot drift into
    asserting against a path the recording never contained.
    """
    writer, _http, _client = detonating_writer()

    body = writer.support_ticket("The opportunities page will not load.")["would_send"]["body"]

    # Same keys as the request the page itself serialized.
    assert sorted(body) == sorted(SUPPORT_CAPTURE["body"])
    assert sorted(body) == sorted(SUPPORT_CAPTURE["body_keys"])
    assert sorted(body) == sorted(C.SUPPORT_QUERY_BODY_KEYS)

    # candidate is a resource URI, not an integer.
    captured_uri = SUPPORT_CAPTURE["body"]["candidate"]
    prefix = captured_uri[: captured_uri.index("<")]
    assert isinstance(body["candidate"], str), "candidate went out as %r" % type(body["candidate"])
    assert not isinstance(body["candidate"], int)
    assert body["candidate"].startswith(prefix)
    assert re.fullmatch(re.escape(prefix) + r"\d+", body["candidate"]), (
        "candidate %r is not the shape the capture recorded" % body["candidate"]
    )
    assert body["candidate"] == prefix + str(CANDIDATE_ID)

    # And the URL and method the capture recorded, carried through.
    would_send = writer.support_ticket("x")["would_send"]
    assert would_send["method"] == SUPPORT_CAPTURE["method"]
    assert would_send["url"] == SUPPORT_CAPTURE["url"]
    assert would_send["content_type"] == SUPPORT_CAPTURE["content_type"]


@pytest.mark.parametrize("message", ["", "   ", "\n\t "])
def test_an_empty_or_whitespace_only_message_raises_and_sends_nothing(message):
    """A blank ticket is a real ticket in a human queue saying nothing."""
    writer, http, client = detonating_writer()

    with pytest.raises(NothingToDo):
        writer.support_ticket(message, confirm=True)

    assert http.attempts == []
    assert describe(write_requests(client)) == []


def test_attachments_are_always_empty():
    """The site's form takes files; this tool does not send them. Asserted on
    the preview AND on the wire, so "always" covers the path that actually
    reaches Instahyre rather than only the one that describes it."""
    tickets = BodyRecorder(response={"id": 5})
    client = writer_client({C.EP_SUPPORT_QUERY: tickets})

    preview = client.writer.support_ticket("Something is broken.")
    client.writer.support_ticket("Something is broken.", confirm=True)

    assert preview["would_send"]["body"]["attachments"] == []
    assert tickets.calls[0]["attachments"] == []


# ===========================================================================
# E. referral_contacts
# ===========================================================================


def test_an_empty_contacts_payload_is_diagnosed_and_sends_nothing():
    """"Never granted Google access" and "granted but empty" are different
    answers, and the diagnosis has to separate them.

    They lead to opposite next actions -- one is a consent screen he has never
    seen, the other is an address book with nothing in it -- so collapsing them
    into one empty list would send him to the wrong place.
    """
    writer, http, client = detonating_writer(contacts=[])

    result = writer.referral_contacts()

    assert result["contacts"] == []
    assert result["count"] == 0
    diagnosis = result["diagnosis"]
    assert diagnosis["reason"] == "no_contacts_returned"

    explanation = diagnosis["explanation"].lower()
    assert "never granted" in explanation
    assert "returned nothing" in explanation
    assert "shape changed" in explanation
    assert "401" in explanation, "the explanation does not rule out a dead session"

    assert http.attempts == []
    assert describe(write_requests(client)) == []


def test_the_preselect_flag_is_reported_and_never_acted_on():
    """Instahyre ticks these boxes for you. This reports the flag and ticks none.

    A default-selected recipient is a recipient nobody chose, and on this
    surface a recipient is a real person receiving mail over his name. So the
    assertion is not merely that nothing is sent -- it is that no SELECTION
    exists anywhere in the result for a later call to pick up.
    """
    writer, http, client = detonating_writer(
        contacts=[
            {"name": "Ann", "email": "ann@example.com", "preselect": True},
            {"name": "Bo", "email": "bo@example.com", "preselect": False},
        ]
    )

    result = writer.referral_contacts()

    # The flag is REPORTED, per contact.
    assert [c["instahyre_would_preselect"] for c in result["contacts"]] == [True, False]
    assert [c["email"] for c in result["contacts"]] == ["ann@example.com", "bo@example.com"]

    # And nobody is selected: the result publishes these keys and no other, so
    # there is no chosen-recipient list hiding beside the report.
    assert sorted(result) == ["contacts", "count", "preselect_note", "sends_nothing"]
    assert "acts on none of them" in result["preselect_note"]
    assert http.attempts == []
    assert describe(write_requests(client)) == []


# ===========================================================================
# Replying to a recruiter -- the one inbox write, and the carve-out that
# admits it
# ===========================================================================
#
# Two separate claims live here and they are easy to conflate, so they are
# tested apart:
#
#   THE CARVE-OUT IS EXACTLY FOUR NAMED PATHS WIDE. It was one until
#   2026-08-25, when starring, marking one thread read and clearing unread
#   across the inbox were built on contracts captured two days earlier. What
#   this file asserts is not the size for its own sake but the WAY it grew:
#   four LITERAL path strings, written out here rather than imported, so that
#   repointing a constant fails this file instead of following it -- and no
#   prefix, no regex and no URL-family rule anywhere, so a fifth Instahyre
#   action is unreachable until somebody reads its contract and names it.
#
#   THE READ TIER DID NOT MOVE. ``guard_read_only`` still refuses all five
#   markers -- ``send_message``, ``star_conversation``, ``toggle_message_read``
#   and ``mark_all_read`` among them -- so the read side cannot reach ANY write
#   path even now that all four exist. That is the assertion that keeps the two
#   doors separate: the write channel is not a hole in the read guard, it is a
#   different door with its own lock.

CONVERSATIONS = fixture_json("conversations_populated.json")
THREAD = fixture_json("conversation_messages.json")
CONV_COUNTS = fixture_json("conversation_counts.json")
REPLY_CONV_ID = CONVERSATIONS["objects"][0]["id"]

#: The literals the allowlist must equal. Written out here, not imported, so
#: that repointing a constant fails this file instead of following it.
SEND_PATH_LITERAL = "/resume_modal/emails/message/send_message/"
STAR_PATH_LITERAL = "/resume_modal/emails/message/star_conversation"
TOGGLE_READ_PATH_LITERAL = "/resume_modal/emails/message/toggle_message_read"
MARK_ALL_READ_PATH_LITERAL = "/inbox_page/candidate_conversation/mark_all_read"

#: THE WHOLE ALLOWLIST, spelled out. Each entry is here because a request body
#: was captured for it first; the set is an enumeration, never a rule.
SENDABLE_LITERALS = frozenset(
    {
        SEND_PATH_LITERAL,
        STAR_PATH_LITERAL,
        TOGGLE_READ_PATH_LITERAL,
        MARK_ALL_READ_PATH_LITERAL,
    }
)

#: Paths that must STAY unreachable from the INBOX write channel. Three are
#: SIBLINGS of admitted paths on the same prefixes, which is where a
#: rule-shaped guard would leak; two are the bulk applies.
#:
#: THE BULK PAIR IS NOW SPELLED OUT rather than read from
#: ``C.FORBIDDEN_ENDPOINTS``, which is empty since the 2026-08-25 ruling built
#: bulk apply. Deriving them from that set was fine while it was a ban and
#: became silent the moment it was not: the list would simply have lost two
#: entries and this parametrize would have gone on passing, having stopped
#: checking the two paths it most needed to.
#:
#: THEY BELONG HERE MORE THAN BEFORE, NOT LESS. While bulk apply was forbidden,
#: "the inbox guard refuses it" was one refusal among many. Now that it is a
#: REACHABLE write with real permissions, this is the assertion that the inbox
#: door cannot spend them -- two doors, two allowlists, neither reaching the
#: other's paths.
STILL_REFUSED = [
    "/candidate_opportunities/candidate_matching/apply_bulk/",
    "/candidate_opportunities/candidate_opportunity/apply_bulk/",
    "/resume_modal/emails/message/message_count/",
    "/resume_modal/emails/message/get_candidates_star_status",
    "/inbox_page/candidate_conversation/search/",
]


#: Invented companies. Real employer names never enter a fixture in this
#: package. The preview joins these in so it can say WHICH thread is being
#: replied to, which is half of what makes the consent informed.
REPLY_JOBS = {
    601001: ("Northwind Analytics", "Senior Backend Engineer"),
    601002: ("Larkspur Systems", "Platform Engineer"),
    601003: ("Fernway Labs", "Node.js Developer"),
}


def reply_job_routes() -> dict:
    routes = {}
    for job_id, (company, title) in REPLY_JOBS.items():
        routes[C.EP_JOB_DETAIL.format(job_id=job_id)] = {
            "id": job_id,
            "title": title,
            "hiring_company_name": company,
            "recruiter_company_name": company,
            "locations": ["Bangalore"],
            "keywords": "Node.js, TypeScript",
            "is_active": True,
        }
    return routes


def reply_client(*, thread=THREAD, conversations=CONVERSATIONS, send_route=None):
    """A client wired for the inbox reads a reply makes, plus an optional send.

    The send route is absent unless a test asks for it, so a send that escaped
    the confirm gate lands on an unmocked path and is recorded as a fact rather
    than quietly answered.
    """
    routes = {
        C.EP_EDUCATION: EDUCATION,
        PROFILE_PATH: PROFILE,
        C.EP_CONVERSATIONS: conversations,
        C.EP_CONVERSATION_COUNT: CONV_COUNTS,
        C.EP_MESSAGES: thread,
    }
    routes.update(reply_job_routes())
    if send_route is not None:
        routes[C.EP_SEND_MESSAGE] = send_route
    client = make_client(routes, with_taxonomy=False)
    client.http.cookies.set("csrftoken", "csrf-for-the-reply", domain="www.instahyre.com")
    return client


def detonating_reply_writer(**kwargs):
    """``(writer, http, client)`` whose writer explodes on any write verb."""
    client = reply_client(**kwargs)
    http = NoWriteHTTP(client.http)
    writer = Writer(http, client.store, client.inbound, client.inbox)
    return writer, http, client


# -- the carve-out ----------------------------------------------------------


def test_the_sendable_allowlist_holds_exactly_the_four_named_paths():
    """Pinned to literals. Asserting it equals the constants it is built from
    would be true no matter what those constants were changed to.

    THE SIZE MOVED FROM 1 TO 4 ON 2026-08-25 and the edit is deliberate, so
    what was admitted and why belongs here, where the next person looks:

      star_conversation   -- {star_conv, job_id}, two shipped callers agreeing.
      toggle_message_read -- {conversation: <resource_uri>, mark_unread}.
      mark_all_read       -- a GET whose arguments ride the query string.

    All three had their contracts read whole out of Instahyre's own JavaScript
    on 2026-08-23 and were then withheld on VALUE, not on evidence. The ruling
    changed: whatever is technically possible gets built. What did NOT change
    is that every entry is a NAMED constant -- if this set ever becomes a
    startswith, a regex, or a comprehension over a URL family, this assertion
    is the one that has to be deleted to allow it, and deleting it is visible.
    """
    assert C.SENDABLE_INBOX_PATHS == SENDABLE_LITERALS
    assert len(C.SENDABLE_INBOX_PATHS) == 4
    assert C.EP_SEND_MESSAGE == SEND_PATH_LITERAL
    assert C.EP_STAR_CONVERSATION == STAR_PATH_LITERAL
    assert C.EP_TOGGLE_MESSAGE_READ == TOGGLE_READ_PATH_LITERAL
    assert C.EP_MARK_ALL_READ == MARK_ALL_READ_PATH_LITERAL


def test_the_allowlist_and_the_read_tiers_own_mutation_list_agree():
    """Two lists written independently, cross-checked against each other.

    ``MUTATING_INBOX_PATHS`` holds four LITERAL strings and predates all of
    this; ``SENDABLE_INBOX_PATHS`` is built from four constants. They now
    describe the same four URLs from opposite directions -- one saying "these
    mutate, so the read tier must refuse them", the other saying "these mutate,
    so the write tier may send them" -- and a typo in a new constant therefore
    surfaces as a disagreement here rather than as a quiet 404 later.
    """
    assert set(C.MUTATING_INBOX_PATHS) == set(C.SENDABLE_INBOX_PATHS)


def test_the_send_path_keeps_the_trailing_slash_its_siblings_do_not_have():
    """The factory declares send_message WITH a slash and its two siblings
    without. Django answers a slashless POST with a 301 that drops the body,
    and this client does not follow redirects."""
    assert C.EP_SEND_MESSAGE.endswith("/")
    for path in STILL_REFUSED:
        if "star_conversation" in path or "toggle_message_read" in path:
            assert not path.endswith("/")


@pytest.mark.parametrize("path", STILL_REFUSED)
def test_paths_outside_the_named_four_are_refused_by_the_send_guard(path):
    """The widening test. If somebody ever admits a FIFTH inbox mutation, this
    is where it fails -- and it fails per-path, so the message names which.

    The list it runs on is chosen for the specific failure a GROWING allowlist
    invites: three of these are siblings of admitted paths on the same
    prefixes, so a guard relaxed into "anything under /resume_modal/emails/
    message" or "anything under the conversation resource" would still pass the
    admitted four while letting these through unnoticed.
    """
    with pytest.raises(writes_module.NotSendable) as excinfo:
        writes_module._guard_sendable(path)
    assert "named sendable inbox paths" in str(excinfo.value)


@pytest.mark.parametrize(
    "path",
    [
        "/candidate_opportunities/candidate_matching/apply_bulk/",
        C.EP_SEND_MESSAGE + "x",
        C.EP_SEND_MESSAGE.rstrip("/"),
        C.EP_STAR_CONVERSATION + "/",
        C.EP_MARK_ALL_READ + "?page_loaded_at=x",
        "",
    ],
)
def test_the_send_guard_is_an_allowlist_not_a_blocklist(path):
    """Anything that is not one of the named values is refused -- a path nobody
    thought to list, and admitted paths spelled slightly wrong.

    The slash cases are not pedantry. ``send_message`` is declared WITH a
    trailing slash and its two siblings WITHOUT one, so each admitted spelling
    is the one Instahyre publishes and the near-miss beside it is a different
    request: Django answers a slashless POST with a 301 that drops the body.
    The query-string case is the same argument aimed at the GET -- the guard
    compares the path it is handed, so a caller that folds parameters into the
    path string is refused rather than silently matched.
    """
    with pytest.raises(writes_module.NotSendable):
        writes_module._guard_sendable(path)


@pytest.mark.parametrize("path", sorted(SENDABLE_LITERALS))
def test_the_send_guard_admits_each_of_the_paths_it_is_supposed_to(path):
    """The other half of the limit, per path. A guard only ever seen refusing
    could be a blanket refusal, and all four tools would be dead on arrival."""
    assert writes_module._guard_sendable(path) == path


def test_the_read_tier_still_refuses_every_path_the_write_tier_now_admits():
    """The read-only guard did NOT shrink when the write door widened.

    This is the assertion the 2026-08-25 build most needed, because the
    tempting way to build three inbox writes is to relax the guard standing in
    the way. Nothing was relaxed: all five markers stand, and every one of the
    four paths the write tier may now send to is still refused outright by the
    read tier. A reader that could reach a write path by editing a constant is
    the hazard this keeps closed, and it is checked against the LIVE guard
    rather than against the constant the guard consults.
    """
    from instahyre_server.inbox import MutatingPathRefused, guard_read_only

    assert len(C.MUTATING_PATH_MARKERS) == 5
    assert "send_message" in C.MUTATING_PATH_MARKERS
    for marker in C.MUTATING_PATH_MARKERS:
        with pytest.raises(MutatingPathRefused):
            guard_read_only("/inbox_page/candidate_conversation/" + marker)
    for path in sorted(SENDABLE_LITERALS):
        with pytest.raises(MutatingPathRefused):
            guard_read_only(path)


# -- the gate ---------------------------------------------------------------


def test_a_reply_without_confirm_issues_no_write_at_all():
    writer, http, client = detonating_reply_writer()

    preview = writer.reply_to_conversation(REPLY_CONV_ID, "Thanks, keen to talk.")

    assert preview["confirmed"] is False
    assert http.attempts == []
    assert describe(write_requests(client)) == []


def test_omitting_confirm_entirely_is_a_refusal_not_a_send():
    writer, http, _ = detonating_reply_writer()
    signature = inspect.signature(Writer.reply_to_conversation)

    assert signature.parameters["confirm"].default is False
    writer.reply_to_conversation(REPLY_CONV_ID, "hello")
    assert http.attempts == []


def test_the_preview_names_the_recipients_the_server_reported():
    """Consent on an irreversible send needs the real recipient, taken off the
    thread the server returned -- not one this code assembled."""
    writer, _, _ = detonating_reply_writer()

    preview = writer.reply_to_conversation(REPLY_CONV_ID, "hello")

    assert preview["recipients"] == THREAD["recipients"]
    assert preview["recipients"], "a preview with no recipient is not consent"


def test_the_preview_shows_the_message_as_typed_and_the_body_that_would_go():
    writer, _, _ = detonating_reply_writer()
    typed = "Hi Priya, that role sounds interesting."

    preview = writer.reply_to_conversation(REPLY_CONV_ID, typed)

    assert preview["message_as_typed"] == typed
    assert preview["would_send"]["method"] == "POST"
    assert preview["would_send"]["url"] == C.API_BASE + SEND_PATH_LITERAL
    assert preview["would_send"]["body"]["conv_id"] == REPLY_CONV_ID


def test_the_preview_names_the_company_and_role_the_thread_belongs_to():
    writer, _, _ = detonating_reply_writer()

    preview = writer.reply_to_conversation(REPLY_CONV_ID, "hello")

    assert preview["thread"]["company"] is not None
    assert preview["thread"]["title"] is not None


def test_the_preview_says_plainly_that_it_cannot_be_taken_back():
    writer, _, _ = detonating_reply_writer()

    preview = writer.reply_to_conversation(REPLY_CONV_ID, "hello")

    assert "no unsend" in preview["irreversible"]
    assert "NOTHING HAS BEEN SENT" in preview["next"]


def test_the_preview_declares_its_evidence_class_as_shipped_not_wire():
    """The one surface in this register that reaches a person AND has never
    been observed on a wire. A preview that let that pass unstated would be
    borrowing the confidence of the wire-captured surfaces beside it."""
    writer, _, _ = detonating_reply_writer()

    preview = writer.reply_to_conversation(REPLY_CONV_ID, "hello")

    assert preview["contract"]["evidence_class"] == C.CONTRACT_SHIPPED
    assert "never been serialized" in preview["contract"]["what_that_means"]


# -- the body ---------------------------------------------------------------


def test_the_body_carries_exactly_the_three_captured_keys():
    writer, _, _ = detonating_reply_writer()

    body = writer.reply_to_conversation(REPLY_CONV_ID, "hello")["would_send"]["body"]

    assert sorted(body) == sorted(C.SEND_MESSAGE_BODY_KEYS)
    assert sorted(body) == ["attachments", "content", "conv_id"]


def test_the_preview_states_the_content_type_this_client_actually_sends():
    """Not the one the browser sends. The capture records
    ``application/json;charset=utf-8`` -- AngularJS $resource's default, not
    something Instahyre asked for -- while this client sends the bare value.
    A preview that quoted the capture would be describing a request it is not
    about to make, on the one surface where the preview IS the consent.
    """
    writer, _, _ = detonating_reply_writer()

    preview = writer.reply_to_conversation(REPLY_CONV_ID, "hello")

    assert preview["would_send"]["content_type"] == "application/json"
    assert "charset=utf-8" in preview["content_type_differs_from_the_capture"]


def test_attachments_are_always_empty_because_the_element_shape_is_unmeasured():
    writer, _, _ = detonating_reply_writer()

    body = writer.reply_to_conversation(REPLY_CONV_ID, "hello")["would_send"]["body"]

    assert body["attachments"] == []


def test_html_special_characters_are_escaped_rather_than_sent_as_markup():
    """``content`` is HTML. An unescaped ``<`` in his message is either
    swallowed by the renderer or interpreted as a tag, and this send cannot be
    taken back."""
    writer, _, _ = detonating_reply_writer()

    body = writer.reply_to_conversation(
        REPLY_CONV_ID, "R&D on <script> and 5 > 3"
    )["would_send"]["body"]

    assert "<script>" not in body["content"]
    assert "&amp;" in body["content"]
    assert "&lt;script&gt;" in body["content"]
    assert "&gt;" in body["content"]


def test_line_breaks_survive_as_paragraphs_rather_than_being_reflowed():
    """The preview shows the text he typed, so silently joining his lines would
    send something other than what he agreed to."""
    writer, _, _ = detonating_reply_writer()

    body = writer.reply_to_conversation(
        REPLY_CONV_ID, "Hi Priya,\n\nYes, keen.\nBest,\nAlex"
    )["would_send"]["body"]

    assert body["content"].count("<p>") == 5
    assert "<p>Hi Priya,</p>" in body["content"]
    assert "<p>Yes, keen.</p>" in body["content"]
    assert "<p><br></p>" in body["content"]


def test_the_body_that_goes_out_is_exactly_the_body_the_preview_promised():
    """The preview IS the consent. A send that differed from it -- in either
    direction -- obtained agreement for a different message."""
    recorder = BodyRecorder({"content": "<p>ok</p>"})
    client = reply_client(send_route=recorder)
    promised = client.writer.reply_to_conversation(REPLY_CONV_ID, "ok")["would_send"]

    client.writer.reply_to_conversation(REPLY_CONV_ID, "ok", confirm=True)

    assert recorder.calls == [promised["body"]]


def test_a_confirmed_reply_posts_to_the_one_sendable_path_and_nowhere_else():
    recorder = BodyRecorder({"content": "<p>ok</p>"})
    client = reply_client(send_route=recorder)

    client.writer.reply_to_conversation(REPLY_CONV_ID, "ok", confirm=True)

    assert describe(write_requests(client)) == [("POST", "/api/v1" + SEND_PATH_LITERAL)]


# -- the rails, each shown refusing AND allowing -----------------------------


@pytest.mark.parametrize("message", ["", "   ", "\n\n", "\t "])
def test_an_empty_or_blank_reply_is_refused_and_sends_nothing(message):
    """Instahyre's own page would send this -- it validates nothing -- which is
    exactly why this refuses."""
    writer, http, client = detonating_reply_writer()

    with pytest.raises(NothingToDo) as excinfo:
        writer.reply_to_conversation(REPLY_CONV_ID, message, confirm=True)

    assert "no unsend" in str(excinfo.value)
    assert http.attempts == []
    assert describe(write_requests(client)) == []


def test_a_reply_over_the_length_cap_is_refused_and_one_at_the_cap_is_allowed():
    """A limit only ever seen refusing could be a blanket refusal."""
    writer, http, _ = detonating_reply_writer()

    with pytest.raises(NothingToDo):
        writer.reply_to_conversation(
            REPLY_CONV_ID, "x" * (writes_module.MAX_REPLY_CHARS + 1), confirm=True
        )
    assert http.attempts == []

    at_limit = writer.reply_to_conversation(
        REPLY_CONV_ID, "x" * writes_module.MAX_REPLY_CHARS
    )
    assert at_limit["confirmed"] is False


def test_a_conv_id_that_is_not_his_is_refused_before_anything_is_sent():
    """The message endpoint answers 200 for a foreign id, so the refusal has to
    come from cross-checking his own conversation list."""
    from instahyre_server.errors import NotFound

    not_his = fixture_json("conversation_messages_not_found.json")
    writer, http, client = detonating_reply_writer(thread=not_his)

    with pytest.raises(NotFound):
        writer.reply_to_conversation(999999999, "hello", confirm=True)

    assert http.attempts == []
    assert describe(write_requests(client)) == []


def test_a_confirmed_reply_without_a_csrf_token_refuses_before_sending():
    client = reply_client()
    client.http.cookies.clear()
    http = NoWriteHTTP(client.http)
    writer = Writer(http, client.store, client.inbound, client.inbox)

    with pytest.raises(writes_module.ConfirmationRequired):
        writer.reply_to_conversation(REPLY_CONV_ID, "hello", confirm=True)

    assert http.attempts == []


def test_a_writer_built_without_an_inbox_refuses_rather_than_sending_blind():
    """It cannot show who a reply would reach, so it does not send. Stated as a
    wiring bug rather than as a platform limit."""
    client = reply_client()
    writer = Writer(client.http, client.store, client.inbound)

    with pytest.raises(InstahyreError) as excinfo:
        writer.reply_to_conversation(REPLY_CONV_ID, "hello", confirm=True)

    assert "wiring bug" in str(excinfo.value)


# -- a 200 is not a delivery ------------------------------------------------


def test_a_send_is_verified_by_re_reading_the_thread():
    """Same rule the profile writes hold: the status code is not the outcome.

    THIS IS ALSO THE CONTROL FOR ``include_gated``, which is why the fixture
    matters more than usual here. ``show_message`` is a gate the site applies
    with a ``break`` -- the ordinary read stops at the first falsy one and
    discards it and everything after. This fixture HAS such a message, and a
    reply lands after it, so a verification that read the thread the ordinary
    way is structurally blind to what it just sent and reports a delivered
    message as unconfirmed. That is the reading most likely to produce a
    duplicate send to a real person, and there is no unsend.

    The precondition is asserted rather than assumed: if the fixture ever loses
    its gated message this test keeps passing while testing nothing.
    """
    sent_text = "Yes, Thursday works for me."
    assert any(
        not msg.get("show_message", True) for msg in THREAD["objects"]
    ), "the fixture no longer contains a gated message, so this is not a control"

    after = copy.deepcopy(THREAD)
    after["objects"].append(
        {
            "content_html": "<p>%s</p>" % sent_text,
            "is_owner": True,
            "show_message": True,
            "created_at_date_time": "2026-08-23T12:00:00",
        }
    )
    client = reply_client(thread=after, send_route=BodyRecorder({"content": "ok"}))

    result = client.writer.reply_to_conversation(
        REPLY_CONV_ID, sent_text, confirm=True
    )

    assert result["verified"] is True
    assert "re-read of the thread" in result["verified_by"]
    assert "warning" not in result


def test_a_send_that_cannot_be_confirmed_says_so_and_says_do_not_retry():
    """The dangerous half. A retry that duplicates a delivered message cannot be
    undone either, so an unconfirmed send must not read as a failed one."""
    client = reply_client(send_route=BodyRecorder({"content": "ok"}))

    result = client.writer.reply_to_conversation(
        REPLY_CONV_ID, "A line that is nowhere in the fixture thread.", confirm=True
    )

    assert result["verified"] is False
    assert "Do NOT simply retry" in result["warning"]
    assert "duplicate" in result["warning"]


def test_the_reply_tool_is_declared_irreversible_by_the_server_itself():
    """A server that can send and does not say so is worse than one that never
    could -- and the mirror: it must not claim the inbox is unwritable.

    RE-POINTED 2026-08-25, when ``deliberately_not_built`` moved behind
    ``section=`` so it costs once instead of on every call. This test caught
    that move, which is the whole reason it is written against the CONTENT
    rather than against the tool's default shape. It now asserts both halves of
    what "moved" has to mean: the prose is reachable and byte-for-byte intact
    under its section, and the default view still says the block exists and
    names the call that returns it. A summary that was the last remaining copy
    would pass neither.
    """
    info = server_module.instahyre_server_info()
    assert "instahyre_reply_to_conversation" in info["irreversible_tools"]

    narrowed = server_module.instahyre_server_info(section="deliberately_not_built")
    inbox = narrowed["deliberately_not_built"]["inbox_writes"]
    assert "ALL FOUR MEASURED INBOX WRITES ARE NOW REACHABLE" in inbox
    assert "allowlist" in inbox
    # The mirror of the mirror, added 2026-08-25: the block must not go on
    # claiming that starring and marking read are unbuilt now that they are
    # built. A server describing a capability it HAS as one it refuses is the
    # same defect as the reverse, pointed the other way.
    assert "instahyre_star_conversation" in inbox
    assert "instahyre_mark_conversation_read" in inbox
    assert "instahyre_mark_all_conversations_read" in inbox

    # RELOCATED, NOT REMOVED. The default view keeps the entry and its verdict,
    # and states where the rest is -- so a reader of the cheap view is never
    # left believing the short line is all there ever was.
    summary = info["deliberately_not_built"]
    assert "inbox_writes" in summary
    assert "ALL FOUR MEASURED INBOX WRITES ARE NOW REACHABLE" in summary["inbox_writes"]
    assert "section='deliberately_not_built'" in summary["_full_text"]


# ===========================================================================
# The premise underneath every assertion above
# ===========================================================================


def test_no_assertion_in_this_file_could_have_reached_the_real_instahyre():
    """Both halves matter: the mock really served these calls, so the recorded
    request list is the true record of what this package tried to send -- and
    the genuine transport really is blocked, so a route this file forgot to mock
    could not have quietly gone out over the wire."""
    client = writer_client()

    client.writer.send_referral_invites(THREE_ADDRESSES)
    assert client.routes.requests, "the mock transport served the preview's reads"

    with pytest.raises(AssertionError, match="real network"):
        httpx.HTTPTransport().handle_request(
            httpx.Request("POST", C.API_BASE + C.EP_REFERRAL_INVITES)
        )


def test_the_module_under_test_is_the_one_this_file_names():
    """A cheap guard against the whole file testing a stale import: the Writer
    these tests exercise is the one ``client.writer`` is built from."""
    assert isinstance(writer_client().writer, writes_module.Writer)
    assert issubclass(NothingToDo, InstahyreError)
