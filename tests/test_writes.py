"""The four captured write surfaces, held to the gate their module promises.

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
