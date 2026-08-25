"""The writes whose contracts were captured on 2026-08-23.

Every request in this module was MEASURED before a line of it was written --
either recorded off the wire by ``scripts/capture_write_contracts.py`` and
aborted at the router, or read whole out of Instahyre's own shipped JavaScript.
``constants.CAPTURED_WRITE_CONTRACTS`` says which, per surface, and this module
carries that stamp through into every preview it returns. The distinction is
not decoration: a body that has been serialized by the page is stronger
evidence than a body a factory says the page would build, and a caller
deciding whether to press send deserves to know which one it is looking at.

THE GATE IS THE SAME EVERYWHERE. ``confirm=False`` is the default and it sends
NOTHING -- it returns the exact request that would go out, and for invitations
it returns the exact list of people who would receive one. There is no path
through this module that sends without having first been able to show that.

WHY THE INVITE TOOL IS SHAPED THE WAY IT IS. ``send_invites`` mails real people
who know him, from his address, and Instahyre has no unsend anywhere in its
product. Unlike an application -- which at worst wastes a slot -- there is no
version of a wrong invite that is merely a wasted resource. So it refuses an
empty list, refuses a malformed address rather than "trying", deduplicates
before counting, caps the batch, and prints the recipients before it will take
a confirm. The one thing it deliberately does NOT do is invent a name: the
typed-invite path in Instahyre's own client sends ``{'name': null, 'email':
...}``, and a preview that showed a guessed name would be dressing up the
consent it is supposed to be obtaining.

THE INBOX GREW FROM ONE WRITE TO FOUR ON 2026-08-25. Starring, marking one
thread read, and clearing unread across the whole inbox used to be described
here as having "no branch that could construct them". They have one now, and
the sentence that retired is worth keeping visible: the refusal rested on the
absence of a measured request, and every one of the three ships its contract in
Instahyre's own JavaScript, so the absence was never the real state of the
evidence. What did NOT change is the shape of the door -- ``SENDABLE_INBOX_PATHS``
grew by three NAMED constants, never by loosening into a prefix or a regex --
and what did not change either is the read tier, which still refuses all five
mutating markers. One of the three is a **GET**, and it is gated like a send;
see :meth:`Writer.mark_all_conversations_read` for why that is not a category
error.

BULK APPLY LANDED ON 2026-08-25 AND IT IS THE MOST DESTRUCTIVE THING IN THIS
PACKAGE. Both its paths sat in ``FORBIDDEN_ENDPOINTS`` under the words "must
never be built, at any evidence level"; the ruling is that whatever is
technically possible gets built, its contract ships whole in Instahyre's own
JavaScript, and so it is built. The ban's protection did not evaporate -- it
moved into the gate, which is the feature. A bulk apply is not more dangerous
than N single applies; it is the same N applications MINUS N-1 confirmations,
so the gate's whole job is handing back what the collapse takes: the preview
NAMES every opportunity, an ``expected_count`` the caller states independently
must match what resolved, every id is checked against the LIVE pending queue,
and ``MAX_BULK_APPLY`` refuses an over-long list rather than truncating it.
The caller supplies the ids; nothing here assembles them. See
:meth:`Writer.bulk_apply`. The read tier did not move: ``apply_bulk`` is still
in ``MUTATING_PATH_MARKERS`` and ``guard_read_only`` still refuses both paths.

THE LEADERBOARD CLUSTER LANDED ON 2026-08-25 AND IT IS THE ONLY CHANNEL HERE
THAT RUNS THE OTHER WAY. Everything above is him talking to Instahyre; these
three read and answer a channel where INSTAHYRE ASKS HIM -- were you hired at
this company, and how did that opportunity go -- and the first of those is a
TERMINAL status change that no other tool on this server could see. All three
routes were measured EMPTY on 2026-08-25 from his own signed-in session, all
200: ``{"data": []}``, ``{"objects": [], "meta": {...}}``, and
``{"show_modal": false}``. EMPTY IS NOT ABSENT, and the consequence is stated
rather than discovered: both writes validate their id against a LIVE re-read of
the endpoint that offers it, so with those reads empty EVERY WRITE IN THE
CLUSTER REFUSES TODAY. That is the gate working. Two of the actions on the same
factories are deliberately NOT built -- the ask-me-later PATCH, whose body is
known and recorded because its caller ships, and ``add_joining_date``, which
has no caller anywhere and therefore no body to record. See
:func:`_guard_leaderboard_sendable` for why an allowlist rather than a
blocklist is what keeps the second one unreachable.

WHAT IS NOT HERE. No profile-image upload: its contract is captured but
reproducing the browser's body needs a WebP encoder at width<=800 that this
package has no dependency for, and the CREATE branch was never exercised. No
questionnaire answers and no workex PUT: those two are still in
``UNVERIFIED_WRITE_SURFACES`` and the register says why.
"""

from __future__ import annotations

import datetime
import logging
import re
from typing import Any, Optional
from urllib.parse import parse_qsl

from . import constants as C
from .errors import InstahyreError, NotFound

log = logging.getLogger("instahyre.writes")

#: The most invitations one call will send. Not a platform limit -- no measured
#: one exists -- but a deliberate rail on the surface with the highest
#: reputational cost and no undo. A larger batch is several calls, each of which
#: prints its recipients and takes its own confirm.
MAX_INVITES_PER_CALL = 10

#: Deliberately permissive. This is not address validation, which cannot be
#: done client-side; it is a check that a typo has not produced something that
#: obviously cannot be an address. Refusing here is cheap. Sending a malformed
#: invite is not recoverable.
_ADDRESS_SHAPE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


#: The longest single reply this tool will send. Instahyre publishes no limit
#: and its own page enforces none, so this is a rail of ours: on a surface with
#: no undo, the most expensive realistic failure is not a typo but a generated
#: body that ran away, and a cap turns that into a refusal instead of a wall of
#: text in a recruiter's inbox.
MAX_REPLY_CHARS = 4000


#: The most opportunities one bulk apply will send to. THE HARD CAP, and the
#: single most load-bearing number in this module.
#:
#: IT IS SIZED AGAINST HIS QUEUE, NOT AGAINST THE PLATFORM. His pending queue
#: runs to roughly thirty. Ten is deliberately well under that, so "apply to
#: the whole queue" is not something this tool can do in one call, or in two.
#: Instahyre publishes no bulk limit of its own; if it has one, it is not this,
#: and this is not pretending to be it.
#:
#: OVER THE CAP IS A REFUSAL, NEVER A TRUNCATION. Silently applying to the
#: first ten of twenty-five is the precise failure this tool exists not to
#: have: the caller would be told ten applications were sent, having asked for
#: twenty-five, with fifteen of them left in a state nobody can distinguish
#: from "sent" without re-reading. A refusal costs one round trip. A truncation
#: costs ten irreversible applications aimed at a list the caller never
#: confirmed.
MAX_BULK_APPLY = 10


class ConfirmationRequired(InstahyreError):
    kind = "confirmation_required"


class NothingToDo(InstahyreError):
    kind = "nothing_to_do"


class NotSendable(InstahyreError):
    """A POST was aimed at a path that is not the one sendable path."""

    kind = "not_sendable"


def _guard_sendable(path: str) -> str:
    """Allow only the named inbox mutation targets. Returns the path.

    An ALLOWLIST, not a blocklist, and the difference is the whole point of the
    carve-out. A blocklist that had been narrowed to let these through would
    also let through anything nobody thought to add to it -- and the
    mark-all-read trap on this very resource is the proof that this API has
    mutating routes which do not look like writes (it is a **GET**, which is
    exactly why this guard is asked about a PATH and never about a verb).

    So this asks the opposite question: is this path ONE OF THE NAMED paths
    this package may send to? Bulk apply is refused here not because it is
    listed but because it is not one of the allowed values, which is a property
    that survives someone adding a new action to the API tomorrow.

    THE SET GREW FROM ONE TO FOUR ON 2026-08-25, by named entry. It did not
    grow by becoming a rule: there is no prefix test, no regex and no "anything
    under /resume_modal/emails/message" clause here, because a rule admits
    members nobody has read. Each of the four is a constant whose request body
    was captured first, and ``tests/test_writes.py`` pins the set to literal
    strings so repointing a constant fails there rather than following it here.
    """
    if path not in C.SENDABLE_INBOX_PATHS:
        raise NotSendable(
            "Refusing to send to %r. This server has exactly %d named sendable inbox "
            "paths (%s) and this is not one of them. The set is an allowlist of named "
            "constants, not a rule about a URL family, so a new Instahyre action is "
            "unreachable here until somebody reads its contract and names it."
            % (
                path,
                len(C.SENDABLE_INBOX_PATHS),
                ", ".join(sorted(C.SENDABLE_INBOX_PATHS)),
            ),
            path=path,
        )
    return path


def _guard_bulk_apply_sendable(path: str) -> str:
    """Allow only the two named bulk-apply targets. Returns the path.

    A SECOND DOOR, NOT A WIDER ONE. :func:`_guard_sendable` governs the inbox
    and must go on refusing these two paths; this one governs bulk apply and
    refuses every inbox path. Neither can reach the other's members, so a bug
    in one surface cannot spend the other's permissions -- which is worth more
    here than the tidiness of a single combined set would be.

    THE PATH IS CHECKED, NOT THE CONSTANT NAME. The caller hands over the path
    it is about to request, computed from the branch, so this fires on an
    edited constant, on a hand-built string, and on a branch that resolves
    somewhere unexpected. Asking "is this one of two named values" is a
    question that keeps its meaning when Instahyre adds an action tomorrow;
    asking "is this on the forbidden list" is a question that does not.
    """
    if path not in C.SENDABLE_BULK_APPLY_PATHS:
        raise NotSendable(
            "Refusing to send a bulk apply to %r. This server has exactly %d named "
            "bulk-apply paths (%s) and this is not one of them."
            % (
                path,
                len(C.SENDABLE_BULK_APPLY_PATHS),
                ", ".join(sorted(C.SENDABLE_BULK_APPLY_PATHS)),
            ),
            path=path,
        )
    return path


def _guard_leaderboard_sendable(path: str) -> str:
    """Allow only the two named leaderboard targets. Returns the path.

    A THIRD DOOR, NOT A WIDER ONE, on exactly the reasoning that gave bulk
    apply its own set instead of folding it into the inbox's:
    :func:`_guard_sendable` refuses these two, :func:`_guard_bulk_apply_sendable`
    refuses them, and this one refuses every member of both. Three small
    enumerated sets that each refuse the others' members beat one large set in
    which a bug on any surface can spend every surface's permissions.

    THE MEMBER THIS SET DOES NOT HAVE IS THE INTERESTING ONE.
    ``EP_VERIFY_HIRED`` -- the collection url -- is READ by
    :meth:`Writer._live_hire_queue` and is absent here, which is the only thing
    standing between this package and ``add_joining_date``. That action POSTs to
    the SAME url the collection GET reads, so no rule about the path could
    admit the read and refuse the write; an allowlist can, because it is asked
    about the exact value and not about a family. And it must refuse it: the
    name occurs exactly once in all ten captured bundles, in the factory
    declaration, with no caller anywhere, so nothing states what its body would
    be.
    """
    if path not in C.SENDABLE_LEADERBOARD_PATHS:
        raise NotSendable(
            "Refusing to send a leaderboard write to %r. This server has exactly %d "
            "named leaderboard paths (%s) and this is not one of them. Note in "
            "particular that the verify_hired_candidate COLLECTION url is not a "
            "member: it is read, never written, which is what keeps add_joining_date "
            "-- an action with no caller in any captured bundle, and therefore no "
            "known body -- unreachable."
            % (
                path,
                len(C.SENDABLE_LEADERBOARD_PATHS),
                ", ".join(sorted(C.SENDABLE_LEADERBOARD_PATHS)),
            ),
            path=path,
        )
    return path


#: Characters that change meaning inside the HTML body the editor produces.
_HTML_ESCAPES = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"))


def _as_message_html(text: str) -> str:
    """Turn typed text into the HTML shape the compose editor sends.

    ``content`` is the Quill editor's HTML, not plain text -- so handing the
    endpoint a raw string is a guess about how the server renders it. Escaping
    first and wrapping each paragraph in ``<p>`` is the conservative reading:
    an ampersand or a less-than in his message survives as itself instead of
    being swallowed or, worse, interpreted, and a blank line becomes a
    paragraph break rather than disappearing.

    Escaping runs BEFORE the tags are added, and ``&`` is escaped first, or the
    escapes would escape each other.

    One paragraph PER LINE, with a blank line becoming ``<p><br></p>``, because
    that is what Quill itself produces. Collapsing a run of lines into one
    paragraph would silently reflow his message -- and a tool that quietly
    rewrites what a person is about to send to a recruiter has broken the
    consent the preview obtained, since the preview shows the text he typed.
    """
    out = text.replace("\r\n", "\n").replace("\r", "\n")
    for raw, escaped in _HTML_ESCAPES:
        out = out.replace(raw, escaped)
    lines = out.strip("\n").split("\n")
    return "".join(
        "<p>%s</p>" % (line.strip() or "<br>") for line in lines
    )


def _iso_millis_now() -> str:
    """The moment, spelled the way JavaScript's ``toISOString()`` spells it.

    MEASURED, not chosen: ``$scope.pageLoadTimestamp=new Date().toISOString()``
    in Instahyre's inbox controller, and that value is handed to mark_all_read
    as ``page_loaded_at``. ``toISOString`` always emits UTC with exactly three
    fractional digits and a literal ``Z``, so this matches it rather than
    emitting Python's default ``+00:00`` offset spelling -- a server that
    parses one and not the other would otherwise turn a race guard into a
    silent no-op, and a no-op race guard on a bulk mutation widens the sweep.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (now.microsecond // 1000)


def _contract(surface: str) -> dict:
    """The evidence stamp a preview carries, straight from the register."""
    entry = C.CAPTURED_WRITE_CONTRACTS[surface]
    return {
        "evidence_class": entry["evidence"],
        "what_that_means": (
            "Recorded off the wire from the real signed-in browser and aborted before "
            "it was delivered. This is the request the page itself built."
            if entry["evidence"] == C.CONTRACT_WIRE
            else "Read whole out of Instahyre's shipped JavaScript -- the factory and "
            "the calling function, verbatim. It is what the browser WOULD build; it "
            "has never been serialized, so it is weaker than a wire capture."
        ),
        # READ OFF THE ENTRY, with the capture day as the default. It was a bare
        # literal until apply_bulk joined the register: every surface before it
        # was read or recorded on 2026-08-23, and a stamp that said so
        # unconditionally would have dated the one contract read later to a day
        # nobody read it on. A provenance field that can be wrong about
        # provenance is worse than none.
        "captured": entry.get("captured", "2026-08-23"),
    }


class Writer:
    """The eleven captured write surfaces, each behind a confirm gate.

    Bulk apply is the ninth and it is the only one whose gate is the feature
    rather than the toll -- see :meth:`bulk_apply`.

    The tenth and eleventh joined on 2026-08-25 and they are the only two here
    that ANSWER rather than ask: see :meth:`answer_hire_check` and
    :meth:`rate_opportunity`, plus the read that finds them,
    :meth:`pending_requests`. Their gate carries one rail the others do not --
    the id is validated against a LIVE re-read of the very endpoint that offers
    it -- and since all three routes in that cluster are measured EMPTY, both
    writes refuse today.
    """

    def __init__(self, http: Any, store: Any, inbound: Any, inbox: Any = None) -> None:
        self.http = http
        self.store = store
        self.inbound = inbound
        self.inbox = inbox

    # -- who he is, for the bodies that name the sender --------------------

    def _referrer(self) -> dict:
        """His own name and address, as Instahyre's own client reads them.

        The shipped invite caller falls back to ``$scope.candidate.user
        .full_name`` and ``.email`` when the referral form is blank, and that
        is the same payload this reads. It is returned rather than hidden
        because it is HIS address on an outbound mail: a consent gate that
        concealed the From line would be showing him less than the recipient
        sees.
        """
        raw = self.inbound._raw_profile()
        user = raw.get("user") or {}
        name = user.get("full_name") or user.get("first_name")
        email = user.get("email")
        if not email:
            raise InstahyreError(
                "The profile payload carries no user email, so the referrer fields "
                "this request needs cannot be filled. Instahyre's own client reads "
                "them from the same place (candidate.user.email), so this is a "
                "contract change rather than a missing setting."
            )
        return {"name": name, "email": email}

    # -- 0. replying to a recruiter (SHIPPED) ------------------------------

    def reply_to_conversation(
        self, conv_id: int, message: str, *, confirm: bool = False
    ) -> dict:
        """Send one reply into one recruiter thread. A person reads this.

        THE ONLY INBOX WRITE THAT REACHES ANOTHER PERSON. It is reached through
        a named allowlist rather than through a hole in the read-only guard,
        and it is the only entry on that allowlist a recruiter ever sees the
        result of -- starring, marking read and clearing unread joined the list
        on 2026-08-25 and all three are private to his own account.
        :data:`constants.MUTATING_PATH_MARKERS` is unchanged and the read tier
        still refuses all five markers, ``send_message`` included.

        WHAT THE PLATFORM CHECKS BEFORE SENDING: nothing. Instahyre's own
        ``addMessage`` has one guard and it is a double-click latch -- no
        empty-content test, no length test, no closed-thread rule. So every rail
        below is this server's, not theirs, and the two are not conflated.

        WHAT CANNOT BE UNDONE: all of it. There is no unsend, no edit and no
        delete anywhere in Instahyre's product, and the recipient is a person at
        a company he may want to work for. ``confirm=False`` therefore returns
        the recipients as the SERVER reports them, the thread's company and
        role, and the exact bytes of the body -- and sends nothing.

        The contract is SHIPPED, not WIRE: read whole out of Instahyre's inbox
        controller bundle. A wire capture is not merely absent, it is currently
        impossible -- his inbox holds zero conversations, the compose form only
        renders inside a selected thread, and the page's own send function
        dereferences the selected conversation before building a request.
        """
        if self.inbox is None:
            raise InstahyreError(
                "This Writer was built without an inbox, so it cannot read the thread a "
                "reply would go to -- and it will not send into a thread it could not "
                "read. This is a wiring bug, not a platform limit."
            )

        text = (message or "").strip()
        if not text:
            raise NothingToDo(
                "An empty reply would put a blank message in a recruiter's inbox under "
                "his name, and there is no unsend. Instahyre's own page would send it -- "
                "it validates nothing -- which is exactly why this refuses. Say "
                "something."
            )
        if len(text) > MAX_REPLY_CHARS:
            raise NothingToDo(
                "That reply is %d characters. This tool caps a single message at %d -- "
                "not a platform limit (none is published) but a rail on a surface with no "
                "undo, where a runaway generated body is the failure that costs the most. "
                "Send something shorter, or send it from the website."
                % (len(text), MAX_REPLY_CHARS)
            )

        # Read the thread FIRST. Two things come out of it that a preview cannot
        # honestly fabricate: whether the id is even his -- the message endpoint
        # answers 200 for a foreign id, so read_conversation cross-checks it
        # against his own conversation list and raises -- and the recipients, as
        # the SERVER reports them rather than as this code guesses.
        thread = self.inbox.read_conversation(conv_id, body_chars=200)
        recipients = thread.get("recipients")
        context = self._thread_context(conv_id)

        body = {
            "conv_id": int(conv_id),
            "content": _as_message_html(text),
            "attachments": [],
        }
        # The doorway. Every inbox request that reaches the wire from this
        # module goes through it, and it is the only place an inbox target is
        # decided -- which is what makes "these four and nothing else" a
        # structural property rather than a promise. It is not self-certifying:
        # the allowlist is pinned to LITERAL paths in tests/test_writes.py, so
        # editing EP_SEND_MESSAGE fails there rather than sliding through here.
        _guard_sendable(C.EP_SEND_MESSAGE)

        preview = {
            "conv_id": int(conv_id),
            "would_send": {
                "method": "POST",
                "url": C.API_BASE + C.EP_SEND_MESSAGE,
                "content_type": "application/json",
                "body": body,
            },
            "recipients": recipients,
            "thread": context,
            "message_as_typed": text,
            "contract": _contract("inbox_reply"),
            "irreversible": (
                "There is no unsend, no edit and no delete on Instahyre. A person at "
                "this company reads whatever is sent, from his name and his address."
            ),
            "attachments": (
                "Always empty, and refused rather than supported. The page populates "
                "this array from an uploader that ships in no bundle, so the ELEMENT "
                "SHAPE is unmeasured -- and a guessed element on an irreversible send "
                "is the one thing this server's whole write register exists to refuse."
            ),
            "no_subject": (
                "The candidate compose form has no subject field -- the literal has zero "
                "hits in the inbox controller. A reply carries body text only."
            ),
            "content_type_differs_from_the_capture": (
                "This client sends 'application/json'; the browser sends "
                "'application/json;charset=utf-8', which is AngularJS $resource's default "
                "rather than anything Instahyre asked for -- the bundle sets no header and "
                "overrides no transform. The charset is a parameter with a utf-8 default, "
                "so the two are the same request to a Django server. Recorded because a "
                "reader comparing this preview against the captured contract would "
                "otherwise find a difference and have to guess whether it mattered."
            ),
        }
        if not confirm:
            preview["confirmed"] = False
            preview["next"] = (
                "NOTHING HAS BEEN SENT. Read 'recipients' and 'message_as_typed' above, "
                "then re-run with confirm=True to send exactly that. There is no way to "
                "take it back afterwards."
            )
            return preview

        if not self.http.cookies.get("csrftoken"):
            raise ConfirmationRequired(
                "Refusing to send without a CSRF token -- Django would reject it and the "
                "result would be ambiguous on a surface where an ambiguous result is the "
                "worst outcome. Run instahyre_auth_status."
            )

        log.warning("sending a reply into conversation %s", conv_id)
        response = self.http.post(C.EP_SEND_MESSAGE, json_body=body)

        # A 200 is not a delivery. Re-read the thread and look for what was
        # sent, exactly as every profile write re-reads rather than trusting the
        # status -- and say plainly when the read-back cannot confirm it, rather
        # than returning a receipt for something unobserved.
        verification = self._verify_reply(conv_id, text)
        result = {
            "confirmed": True,
            "conv_id": int(conv_id),
            "sent": preview["would_send"],
            "recipients": recipients,
            "thread": context,
            "response": response,
            "verified": verification["ok"],
            "verified_by": verification["how"],
        }
        if not verification["ok"]:
            result["warning"] = (
                "THE SEND WAS ACCEPTED BUT COULD NOT BE CONFIRMED by re-reading the "
                "thread. Do NOT simply retry -- if the first one landed, a retry sends a "
                "duplicate to a real person and there is no unsend. Open "
                + C.SITE_BASE
                + "/candidate/inbox/ and look before doing anything else."
            )
        return result

    def _thread_context(self, conv_id: int) -> dict:
        """Which company and role this thread is, for the consent preview.

        Best-effort and explicitly so: a company name that could not be joined
        must not stop a reply, but the preview has to say it is missing rather
        than quietly showing a thread with no employer on it.
        """
        try:
            listing = self.inbox.list_conversations(limit=C.CONV_DEFAULT_LIMIT)
        except InstahyreError as exc:
            return {"lookup_failed": exc.kind, "company": None, "title": None}
        for record in listing.get("conversations") or []:
            if str(record.get("id")) == str(conv_id):
                return {
                    "company": record.get("company"),
                    "title": record.get("title"),
                    "latest_message": record.get("latest_message"),
                }
        return {
            "not_on_the_first_page": True,
            "company": None,
            "title": None,
            "note": (
                "The thread was readable but did not appear on the first page of the "
                "conversation list, so its company could not be joined in. The reply "
                "still goes to the recipients named above."
            ),
        }

    def _verify_reply(self, conv_id: int, text: str) -> dict:
        """Look for the sent message in the thread. Never raises.

        ``include_gated=True`` is load-bearing and was found by a test rather
        than reasoned about. ``show_message`` is a gate the site applies with a
        ``break``: the default read stops at the first falsy one and discards it
        AND EVERYTHING AFTER IT. A reply lands at the end of the thread, so any
        gated message anywhere ahead of it makes the read-back blind to it --
        and this check would then report a delivered message as unconfirmed,
        which is the reading most likely to provoke a duplicate send.

        Verification is a machine comparison, not a display, so it reads the
        whole thread. ``body_chars=None`` is here for the same reason: a
        truncated body could cut the needle in half.
        """
        try:
            thread = self.inbox.read_conversation(
                conv_id, body_chars=None, include_gated=True
            )
        except InstahyreError as exc:
            return {
                "ok": False,
                "how": "the read-back failed with %s, so delivery is unconfirmed" % exc.kind,
            }
        needle = " ".join(text.split())[:80].lower()
        for msg in thread.get("messages") or []:
            haystack = " ".join(str(msg.get("body") or "").split()).lower()
            if needle and needle in haystack:
                return {
                    "ok": True,
                    "how": "re-read of the thread found the message that was sent",
                }
        return {
            "ok": False,
            "how": (
                "re-read of the thread did not find the message that was sent; it may "
                "not be indexed yet, or it may not have been stored"
            ),
        }

    # -- 0b. the three inbox writes admitted 2026-08-25 --------------------
    #
    # NONE OF THESE THREE HAS EVER RUN AGAINST LIVE DATA, and every docstring
    # below says so rather than leaving a reader to assume it. His inbox holds
    # ZERO conversations (measured 2026-08-23, authenticated, 200), so there is
    # nothing to star, nothing to mark read, and -- because the site's own gate
    # keys on the unread count -- nothing for mark-all-read to do either. What
    # was measured is the CONTRACT, out of Instahyre's shipped JavaScript. What
    # was not measured is any response, any status code, and any effect.

    def _raw_conversation(self, conv_id: int) -> dict:
        """The conversation record AS THE SERVER RETURNED IT, or refuse.

        Read raw rather than through :meth:`Inbox.list_conversations` because
        the shaped record deliberately drops the field ``toggle_message_read``
        needs and this code must never invent: ``resource_uri``. That value is
        a tastypie URI the SERVER supplies; assembling one from an id would be
        a guess wearing the shape of a measurement, on a body whose whole
        contract is that the field takes a URI and not an id.

        It also refuses an id that is not his, for the same reason
        :meth:`reply_to_conversation` does: a write aimed at a foreign id is
        the failure with no undo, and his own conversation list is the only
        place a ``conv_id`` legitimately comes from.

        Paged with the same two constants and the same two terminators the
        not-found cross-check uses, so the two walks cannot drift apart. A walk
        that hits the page cap REFUSES rather than reporting "not his" -- on a
        write, "I could not finish looking" and "it is not there" must not
        collapse into the same answer.
        """
        target = str(conv_id)
        offset = 0
        for _ in range(C.CONV_ID_CHECK_MAX_PAGES):
            payload = self.http.get(
                C.EP_CONVERSATIONS,
                params={"limit": C.CONV_ID_CHECK_PAGE, "offset": offset},
            )
            if not isinstance(payload, dict) or "objects" not in payload:
                raise InstahyreError(
                    "The conversation list came back without an 'objects' key, so the "
                    "record this write has to build its body from could not be read. "
                    "Nothing was sent."
                )
            objects = payload.get("objects") or []
            for obj in objects:
                if isinstance(obj, dict) and str(obj.get("id")) == target:
                    return obj
            if not objects or not (payload.get("meta") or {}).get("next"):
                raise NotFound(
                    "No conversation %r in this account's inbox. It is not in "
                    "instahyre_list_conversations, which is the only place a conv_id "
                    "comes from, and that list was walked in full. Nothing was sent."
                    % (conv_id,),
                    conv_id=conv_id,
                    checked_against="instahyre_list_conversations",
                )
            offset += len(objects)
        raise InstahyreError(
            "His inbox runs past the %d conversations this walk covers, so whether "
            "conversation %r is his could not be established. A write does not proceed "
            "on 'probably'. Nothing was sent."
            % (C.CONV_ID_CHECK_PAGE * C.CONV_ID_CHECK_MAX_PAGES, conv_id)
        )

    def _require_csrf(self, what: str) -> None:
        """Refuse to send without a CSRF cookie. Same rail on all three.

        On the two POSTs this is Django's own requirement and a missing token
        would produce a rejection that reads like a platform verdict. On the
        mark-all-read GET it is NOT Django's requirement -- a GET is exempt --
        and it is applied anyway, deliberately: that request bulk-mutates, an
        ambiguous outcome on a bulk mutation is the worst outcome available
        here, and the cookie is the cheapest pre-flight evidence that the
        session behind it is real. Stated rather than implied, because a rail
        of ours dressed up as the platform's is the thing this package's whole
        write register exists to avoid.
        """
        if not self.http.cookies.get("csrftoken"):
            raise ConfirmationRequired(
                "Refusing to %s without a CSRF token. Run instahyre_auth_status." % what
            )

    def star_conversation(
        self, conv_id: int, starred: bool, *, confirm: bool = False
    ) -> dict:
        """Star or unstar one conversation. Reversible, and reaches nobody.

        THE PAYLOAD SHAPE, AND WHY THIS ONE. Two shipped callers build this
        body and each branches on ``profileType``:
        ``inboxService.markUnstarred`` sends ``{star_conv:false, job_id}`` and
        adds ``can_user`` only under ``if(profileType!=="candidate")``;
        ``inboxService.toggleStarConversation`` sends
        ``{star_conv:!Boolean(selectedConv.starred), job_id}`` on the candidate
        branch and adds ``can_user`` on the other. THIS ACCOUNT IS A CANDIDATE,
        so both callers collapse to the SAME two-key body and this server sends
        that: ``{star_conv, job_id}``. ``can_user`` is a limited_candidate
        resource URI naming the other party and is recruiter-side only --
        sending it would be sending a field his own browser never sends.

        THE KEY IS ``star_conv``. ``starred`` appears in the toggle only as
        ``response.starred``, the field read back OFF the reply. No shipped
        caller sends a ``starred`` key, so the tool's ``starred`` ARGUMENT and
        the body's ``star_conv`` FIELD are deliberately spelled differently
        here and must not be reconciled.

        NEVER EXERCISED AGAINST LIVE DATA. His inbox holds zero conversations
        (measured 2026-08-23, authenticated, 200), so this has run against
        fixtures only. The contract is SHIPPED; no response, no status code and
        no effect has been observed.
        """
        starred = bool(starred)
        record = self._raw_conversation(conv_id)
        job_id = record.get("job_id")
        if job_id is None:
            raise InstahyreError(
                "Conversation %r carries no job_id, and the captured body requires one "
                "-- both shipped callers read it straight off the conversation record. "
                "Refusing to send a shape the site does not produce. Nothing was sent."
                % (conv_id,)
            )
        current = record.get("is_starred")
        if isinstance(current, bool) and current == starred:
            raise NothingToDo(
                "Conversation %r is already %s, so this would send a request the site "
                "never makes: its own control only ever inverts the current state "
                "(markUnstarred sends false on a starred thread, the toggle sends the "
                "negation of what it read). Nothing was sent."
                % (conv_id, "starred" if starred else "unstarred")
            )

        body = {"star_conv": starred, "job_id": job_id}
        _guard_sendable(C.EP_STAR_CONVERSATION)

        preview = {
            "conv_id": int(conv_id),
            "would_send": {
                "method": "POST",
                "url": C.API_BASE + C.EP_STAR_CONVERSATION,
                "content_type": "application/json",
                "body": body,
            },
            "currently_starred": current,
            "would_become": starred,
            "thread": self._thread_context(conv_id),
            "contract": _contract("inbox_star"),
            "reversible": "Yes. Run this again with starred inverted.",
            "reaches_nobody": (
                "A star is a private bookmark on his own inbox. No recruiter is "
                "notified and no message is sent."
            ),
            "the_body_key_is_not_the_argument_name": (
                "The wire field is 'star_conv'. 'starred' is what the RESPONSE carries "
                "back; no shipped caller ever sends a key by that name."
            ),
            "can_user_is_omitted_on_purpose": (
                "Both callers add can_user only when profileType is not 'candidate'. "
                "This account is a candidate, so the field is absent here exactly as it "
                "is absent from his own browser's request."
            ),
            "never_run_live": (
                "His inbox holds zero conversations (measured 2026-08-23, "
                "authenticated, 200), so this tool has never been exercised against "
                "real data. The contract was read out of Instahyre's JavaScript; no "
                "response to it has ever been observed."
            ),
        }
        if not confirm:
            preview["confirmed"] = False
            preview["next"] = (
                "NOTHING HAS BEEN SENT. Re-run with confirm=True to set star_conv=%r on "
                "conversation %r." % (starred, int(conv_id))
            )
            return preview

        self._require_csrf("change a star")
        log.warning("setting star_conv=%r on conversation %s", starred, conv_id)
        response = self.http.post(C.EP_STAR_CONVERSATION, json_body=body)

        # This is the one inbox write that verifies itself out of its own
        # reply: the site reads `response.starred` and assigns it straight onto
        # the conversation. So the check is a comparison against what the
        # server said, not against what this code hoped -- and when the field
        # is absent the result says the read-back was unavailable rather than
        # reporting an unobserved success.
        observed = (
            response.get(C.STAR_CONVERSATION_RESPONSE_FIELD)
            if isinstance(response, dict)
            else None
        )
        result = {
            "confirmed": True,
            "conv_id": int(conv_id),
            "sent": preview["would_send"],
            "response": response,
            "verified": observed == starred,
            "verified_by": (
                "the response's own 'starred' field, which is what Instahyre's client "
                "reads to update its UI"
                if isinstance(observed, bool)
                else "the response carried no 'starred' field, so the new state is "
                "unconfirmed"
            ),
        }
        if not result["verified"]:
            result["warning"] = (
                "The request was accepted but the new starred state could not be "
                "confirmed from the response. Re-read with "
                "instahyre_list_conversations before sending it again -- this is "
                "reversible, so a wrong state is fixable, but a blind retry is still a "
                "second write nobody asked for."
            )
        return result

    def mark_conversation_read(
        self, conv_id: int, mark_unread: bool, *, confirm: bool = False
    ) -> dict:
        """Mark one conversation unread, or read. Reaches nobody.

        THE BODY TAKES A RESOURCE URI, NOT AN ID. ``inboxService.markUnread``
        sends ``{conversation: conv.resource_uri, mark_unread: true}``, and
        ``resource_uri`` is a tastypie string the SERVER supplies on the
        conversation record -- the same convention the support ticket names its
        candidate by. This code reads the record and copies that string
        verbatim; it never assembles one from the id, because an assembled URI
        is a guess in the shape of a measurement.

        ONLY ``mark_unread=True`` HAS A SHIPPED CALLER. That is an evidence gap
        and it is stated rather than smoothed over: across every bundle the one
        caller of this action sends the literal ``true``. The site has no
        mark-read button at all -- it marks read implicitly, by fetching a
        thread -- so ``False`` is a value nobody has been observed sending. It
        is accepted here, and the preview says which of the two is measured.

        MARKING UNREAD IS THE SAFE DIRECTION and marking read is not, which is
        the opposite of how the two usually read: unread is the only signal
        separating a new recruiter message from an old one, so clearing it
        destroys information while restoring it destroys none.

        NEVER EXERCISED AGAINST LIVE DATA -- zero conversations in his inbox
        (measured 2026-08-23, authenticated, 200). Fixtures only.
        """
        mark_unread = bool(mark_unread)
        record = self._raw_conversation(conv_id)
        resource_uri = record.get("resource_uri")
        if not resource_uri:
            raise InstahyreError(
                "Conversation %r carries no resource_uri, and the captured body names "
                "the conversation by URI rather than by id. This server will not "
                "assemble one: a fabricated resource URI is a guess wearing the shape "
                "of a measurement. Nothing was sent." % (conv_id,)
            )
        read_now = record.get("is_latest_msg_read")
        if isinstance(read_now, bool) and read_now == (not mark_unread):
            raise NothingToDo(
                "Conversation %r is already marked %s, so there is nothing to change. "
                "Nothing was sent."
                % (conv_id, "unread" if mark_unread else "read")
            )

        body = {"conversation": resource_uri, "mark_unread": mark_unread}
        _guard_sendable(C.EP_TOGGLE_MESSAGE_READ)

        preview = {
            "conv_id": int(conv_id),
            "would_send": {
                "method": "POST",
                "url": C.API_BASE + C.EP_TOGGLE_MESSAGE_READ,
                "content_type": "application/json",
                "body": body,
            },
            "currently_unread": (not read_now) if isinstance(read_now, bool) else None,
            "would_become_unread": mark_unread,
            "thread": self._thread_context(conv_id),
            "contract": _contract("inbox_mark_read"),
            "reversible": "Yes. Run this again with mark_unread inverted.",
            "reaches_nobody": (
                "Read state is his own. No recruiter is notified and no message is "
                "sent."
            ),
            "the_conversation_field_is_a_uri": (
                "'conversation' carries %r -- the resource_uri the server returned on "
                "this record, copied verbatim. It is not the integer id, and this "
                "server never builds one itself." % resource_uri
            ),
            "evidence_for_this_value": (
                "mark_unread=true is what Instahyre's own markUnread sends and is the "
                "only value with a shipped caller anywhere."
                if mark_unread
                else "mark_unread=false has NO shipped caller. Instahyre has no "
                "mark-read control at all -- it marks a thread read implicitly when "
                "the thread is fetched -- so this value has never been observed on "
                "the wire, and it is offered here without that evidence rather than "
                "with it."
            ),
            "never_run_live": (
                "His inbox holds zero conversations (measured 2026-08-23, "
                "authenticated, 200), so this tool has never been exercised against "
                "real data."
            ),
        }
        if not mark_unread:
            preview["losing_the_unread_signal"] = (
                "Marking read destroys the only flag separating a new recruiter "
                "message from an old one on this thread. It is reversible -- run this "
                "again with mark_unread=True -- but nothing else records that the "
                "thread was unread."
            )
        if not confirm:
            preview["confirmed"] = False
            preview["next"] = (
                "NOTHING HAS BEEN SENT. Re-run with confirm=True to send exactly the "
                "body above."
            )
            return preview

        self._require_csrf("change a read flag")
        log.warning("setting mark_unread=%r on conversation %s", mark_unread, conv_id)
        response = self.http.post(C.EP_TOGGLE_MESSAGE_READ, json_body=body)

        # A 200 is not the outcome. The response shape for this action is NOT
        # in the captured contract -- the site's callback reads nothing off it,
        # it just decrements a badge locally -- so the only honest check is a
        # re-read of the record, and the only honest report when that read
        # fails is that it failed.
        verification = self._verify_read_flag(conv_id, mark_unread)
        result = {
            "confirmed": True,
            "conv_id": int(conv_id),
            "sent": preview["would_send"],
            "response": response,
            "verified": verification["ok"],
            "verified_by": verification["how"],
        }
        if not verification["ok"]:
            result["warning"] = (
                "The request was accepted but the new read state could not be "
                "confirmed. This is reversible, so check "
                "instahyre_list_conversations before sending anything else."
            )
        return result

    def _verify_read_flag(self, conv_id: int, mark_unread: bool) -> dict:
        """Re-read the record and compare the flag. Never raises."""
        try:
            record = self._raw_conversation(conv_id)
        except InstahyreError as exc:
            return {
                "ok": False,
                "how": "the read-back failed with %s, so the new state is unconfirmed"
                % exc.kind,
            }
        read_now = record.get("is_latest_msg_read")
        if not isinstance(read_now, bool):
            return {
                "ok": False,
                "how": "the re-read record carried no is_latest_msg_read flag, so the "
                "new state is unconfirmed",
            }
        if read_now == (not mark_unread):
            return {
                "ok": True,
                "how": "re-read of the conversation record shows is_latest_msg_read=%r"
                % read_now,
            }
        return {
            "ok": False,
            "how": "re-read of the conversation record still shows "
            "is_latest_msg_read=%r, which is not what was asked for" % read_now,
        }

    def mark_all_conversations_read(self, *, confirm: bool = False) -> dict:
        """Clear the unread flag across the WHOLE inbox. A GET, and gated like a send.

        WHY A GET IS GATED. Because this one mutates. Instahyre declares it
        ``mark_all_read:{method:'GET',url:url+"mark_all_read"}`` on the same
        ``$resource`` -- and the same URL prefix -- as the conversation list,
        so the single most reasonable-looking way to explore this API ("GET
        everything under the resource and see what comes back") silently wipes
        his unread state with no request body and no confirmation. A gate that
        keyed on the VERB would wave this straight through, which is why both
        guards in this package key on the PATH instead, and why ``confirm``
        here means exactly what it means on a POST: nothing is requested at all
        until it is True.

        WHAT IT COSTS. Unread is the only signal separating a new recruiter
        message from an old one, and one call clears every one of them. Per-
        thread undo exists -- instahyre_mark_conversation_read with
        mark_unread=True -- but only against the list this preview printed;
        nothing else remembers which threads were unread. So the preview names
        them before it will take a confirm.

        WHAT GOES ON THE WIRE. ``buildFilters()`` plus ``page_loaded_at``, as
        query parameters. ``buildFilters()`` was read whole out of the bundle
        and returns an EMPTY dict on the default "All conversations" view with
        no search text, which is why this tool takes no filter arguments -- the
        widest call is the one its name promises and the only one whose filter
        dict needs no choosing. ``page_loaded_at`` is
        ``new Date().toISOString()``, stamped by the site when the conversation
        LIST is fetched; its job is to leave anything that arrived after that
        read alone. This reproduces both halves: it reads the list, stamps that
        read, and sends that stamp.

        NEVER EXERCISED AGAINST LIVE DATA, and on this account it cannot be:
        zero conversations means zero unread, and Instahyre's own caller
        refuses to issue the request when the unread count is zero.
        """
        if self.inbox is None:
            raise InstahyreError(
                "This Writer was built without an inbox, so it cannot read the list "
                "this request has to be stamped against or name what would change. It "
                "will not bulk-mutate blind. This is a wiring bug, not a platform "
                "limit."
            )

        listing = self.inbox.list_conversations(
            limit=C.CONV_ID_CHECK_PAGE, include_job=False
        )
        # Stamped AFTER the response, which is where the site stamps it: the
        # assignment lives inside loadConv's .then() handler. Stamping before
        # the read would claim a moment earlier than the data, and this field's
        # entire job is to bound what the sweep is allowed to touch.
        page_loaded_at = _iso_millis_now()

        records = listing.get("conversations") or []
        unread = [r for r in records if r.get("unread")]
        unread_total = listing.get("unread_total")

        # Instahyre's own gate, reproduced rather than invented:
        # `if(inboxService.getMarkAllAsReadCount())`, and that count is
        # `conv_count.unread || 0` -- the COUNT alone, never the rows. At zero
        # the site sends nothing at all, so neither does this.
        #
        # The count is trusted over the rows on purpose, and only in this
        # direction. A count of zero beside a row that looks unread is a
        # CONTRADICTION, and a bulk mutation is the last place to resolve one
        # by picking the answer that lets it proceed.
        counted = unread_total if isinstance(unread_total, int) else None
        if counted == 0:
            note = (
                "The server reports no unread conversations, and Instahyre's own "
                "markAllRead refuses to issue the request in exactly this case "
                "(`if(inboxService.getMarkAllAsReadCount())`, which is "
                "conv_count.unread || 0). Nothing was sent. On this account that is "
                "also the standing answer: the inbox holds zero conversations, "
                "measured 2026-08-23 while authenticated, 200."
            )
            if unread:
                note += (
                    " NOTE A CONTRADICTION: the count says zero while %d row(s) in the "
                    "list carry an unread flag. This refuses rather than picking the "
                    "reading that would let a bulk mutation through -- check "
                    "instahyre_inbox_counts against instahyre_list_conversations."
                    % len(unread)
                )
            return {
                "confirmed": False,
                "changed": False,
                "unread_total": unread_total,
                "would_affect": [],
                "why_nothing_to_do": note,
                "diagnosis": listing.get("diagnosis"),
            }
        if counted is None and not unread:
            return {
                "confirmed": False,
                "changed": False,
                "unread_total": None,
                "would_affect": [],
                "why_nothing_to_do": (
                    "The unread COUNT could not be read -- the count endpoint did not "
                    "answer with a usable total -- and no conversation in the list "
                    "carries an unread flag either. That is 'nothing observed to do', "
                    "which is not the same statement as 'nothing to do', so it refuses "
                    "rather than reporting a clean sweep of an inbox it could not "
                    "measure. Nothing was sent."
                ),
                "diagnosis": listing.get("diagnosis"),
            }

        params = {"page_loaded_at": page_loaded_at}
        _guard_sendable(C.EP_MARK_ALL_READ)

        preview = {
            "would_send": {
                "method": C.MARK_ALL_READ_METHOD,
                "url": C.API_BASE + C.EP_MARK_ALL_READ,
                "query": params,
                "body": None,
            },
            "unread_total": unread_total,
            "would_affect": [
                {
                    "conv_id": r.get("id"),
                    "preview": r.get("preview"),
                    "last_message_at": r.get("last_message_at"),
                }
                for r in unread
            ],
            "would_affect_count": len(unread),
            "contract": _contract("inbox_mark_all_read"),
            "a_get_that_mutates": (
                "THIS GET MUTATES. It is gated anyway -- harder than most POSTs here -- "
                "because the method says nothing about what it does: Instahyre "
                "declares mark_all_read:{method:'GET',...} on the same resource prefix "
                "as the conversation list, so a routine walk of that resource would "
                "clear his unread flags with no body and no warning."
            ),
            "no_filters_are_sent": (
                "buildFilters() returns an empty dict on the default view, so this "
                "sends page_loaded_at and nothing else -- the widest form of the "
                "action, which is what the name says. There is no narrowing argument "
                "on this tool because a narrowed sweep would be a filter dict nobody "
                "measured."
            ),
            "page_loaded_at_is_a_race_guard": (
                "It is the moment the conversation list above was read. Anything that "
                "arrives after it is outside what this request claims to cover, which "
                "is why the value is taken from this call's own read rather than from "
                "the clock alone."
            ),
            "how_to_undo": (
                "There is no bulk undo. Each thread can be pushed back with "
                "instahyre_mark_conversation_read(conv_id, mark_unread=True), but only "
                "against the 'would_affect' list above -- nothing else records which "
                "threads were unread before this ran."
            ),
            "counts_may_exceed_the_list": (
                "'would_affect' is drawn from the first %d conversations. If "
                "unread_total is larger than would_affect_count, the sweep clears "
                "threads this preview did not name." % C.CONV_ID_CHECK_PAGE
            ),
            "never_run_live": (
                "His inbox holds zero conversations (measured 2026-08-23, "
                "authenticated, 200), so this tool has never been exercised against "
                "real data and Instahyre's own gate would refuse to issue it."
            ),
        }
        if counted is None:
            preview["the_unread_count_was_unavailable"] = (
                "The count endpoint did not answer with a usable total, so 'would_"
                "affect' below is drawn from the conversation list alone. The sweep is "
                "not bounded by that list -- it clears whatever the server considers "
                "unread -- so treat the named threads as a floor, not as the whole "
                "cost."
            )
        if not confirm:
            preview["confirmed"] = False
            preview["next"] = (
                "NOTHING HAS BEEN SENT. Read 'would_affect' above -- those threads lose "
                "their unread flag -- then re-run with confirm=True."
            )
            return preview

        self._require_csrf("clear the whole inbox's unread state")
        log.warning(
            "clearing unread across the inbox (%s unread reported)", unread_total
        )
        response = self.http.get(C.EP_MARK_ALL_READ, params=params)

        verification = self._verify_mark_all_read(response)
        result = {
            "confirmed": True,
            "changed": True,
            "sent": preview["would_send"],
            "affected": preview["would_affect"],
            "unread_before": unread_total,
            "response": response,
            "verified": verification["ok"],
            "verified_by": verification["how"],
            "unread_after": verification["unread_after"],
        }
        if not verification["ok"]:
            result["warning"] = (
                "The request was accepted but the new unread total could not be "
                "confirmed. Do NOT simply re-run it -- a second sweep cannot undo the "
                "first and would only widen it. Read instahyre_inbox_counts."
            )
        return result

    def _verify_mark_all_read(self, response: Any) -> dict:
        """Two independent readings of the new unread total. Never raises.

        The response's own ``conv_count`` is what Instahyre's callback reads
        (``response.conv_count.unread``), and a fresh count endpoint read is
        the second opinion. They are reported separately rather than merged: if
        they disagree, that disagreement is the finding.
        """
        from_response = None
        if isinstance(response, dict):
            counts = response.get(C.MARK_ALL_READ_RESPONSE_COUNT_KEY)
            if isinstance(counts, dict):
                from_response = counts.get(C.MARK_ALL_READ_GATE_COUNT_FIELD)

        re_read: Any = None
        how_re_read = None
        try:
            re_read = self.inbox.conversation_counts().get("unread")
        except InstahyreError as exc:
            how_re_read = "the count re-read failed with %s" % exc.kind

        for value, how in (
            (from_response, "the response's own conv_count.unread, which is the field "
                            "Instahyre's own callback reads"),
            (re_read, "a fresh read of the conversation count endpoint"),
        ):
            if value == 0:
                return {"ok": True, "how": how, "unread_after": value}

        return {
            "ok": False,
            "how": (
                "neither reading confirmed a cleared inbox (response said %r, re-read "
                "said %r%s)"
                % (
                    from_response,
                    re_read,
                    "" if how_re_read is None else "; " + how_re_read,
                )
            ),
            "unread_after": re_read if re_read is not None else from_response,
        }

    # -- 0b. BULK APPLY: the one-way door, widened and railed ---------------

    def _live_pending_index(self) -> tuple[dict, dict]:
        """``({str(id): raw record}, meta)`` for the CURRENT pending queue.

        ``use_cache=False`` is the whole point and is not a performance
        oversight. Everything else in this package is happy to read a queue
        that is a few minutes old; this one is deciding whether an irreversible
        application is about to be aimed at something that is still there. A
        cached queue would let an opportunity he actioned in the browser two
        minutes ago validate cleanly here.

        The meta block is returned alongside so the caller can tell a genuinely
        empty pending queue from a truncated read. They are opposite facts and
        they arrive looking identical -- a missing id would be refused either
        way, which is the safe direction, but the REASON given would be a lie
        in the second case.
        """
        payload = self.inbound.raw_queue(interest="pending", use_cache=False)
        objects = payload.get("objects") or []
        index = {}
        for obj in objects:
            raw_id = obj.get("id")
            if raw_id is not None:
                index[str(raw_id)] = obj
        meta = payload.get("meta") or {}
        return index, {
            "pending_records_read": len(objects),
            "pending_total_reported": meta.get("total_count"),
            "complete": (
                meta.get("total_count") is None
                or len(objects) >= meta.get("total_count")
            ),
        }

    def bulk_apply(
        self,
        opportunity_ids: Optional[list],
        expected_count: Optional[int],
        *,
        confirm: bool = False,
    ) -> dict:
        """Apply to SEVERAL opportunities in ONE request. IRREVERSIBLE, all of them.

        WHY THE GATE IS SHAPED THE WAY IT IS -- read this before the arguments.
        A confirm-gated bulk apply is not inherently more dangerous than N
        confirm-gated single applies: it is the same N applications, to the
        same employers, with the same permanence. What it removes is N-1
        CONFIRMATIONS. That is the entire delta, and every rail below exists to
        give back specifically what the collapse takes away:

        * the PREVIEW names every opportunity, restoring the sight of each item
          that N separate previews would have given;
        * ``expected_count`` restores the ARITHMETIC -- a second, independent
          statement of how many applications are intended, so a list that
          changed length between preview and confirm fails loudly instead of
          applying;
        * ``MAX_BULK_APPLY`` bounds the BLAST RADIUS, because the one thing N
          single applies cannot do is spend the whole queue on one typo;
        * the id list is the CALLER'S, always. Nothing in this package
          assembles it. There is no "apply to all", no filter argument, no
          "top N by score" -- a caller who wants a ranked selection ranks
          first, reads the names, and passes ids.

        AND NOTE WHICH WAY THE SITE'S OWN DEFAULT POINTS. Instahyre's bulk
        modal opens with everything pre-selected --
        ``angular.forEach($scope.oppValues,function(oppVal){oppVal.isSelected=
        true;})`` -- so on their page the default action is "apply to
        everything shown" and deselecting is the work. This tool deliberately
        does the opposite and selects NOTHING: the empty list is refused rather
        than treated as "all".

        APPLICATIONS CANNOT BE WITHDRAWN. Instahyre's own FAQ says the
        application is sent automatically by the system, so there is no undo,
        no support path, and every employer on the list sees it immediately.
        There is no bulk decline and there must never be one: the bulk body has
        no ``is_interested`` key at all, so this endpoint is apply-only by
        construction rather than by our choice.

        THE PATHS THIS REACHES WERE PERMANENTLY BANNED until 2026-08-25. See
        ``constants.FORBIDDEN_ENDPOINTS`` for the ban, the ruling that lifted
        it, and where the protection it provided actually went.

        Args:
            opportunity_ids: An EXPLICIT list of opportunity ids from
                instahyre_list_opportunities. Never assembled here.
            expected_count: How many applications the caller intends. Must
                equal the number that resolved, or nothing is sent.
            confirm: Must be True to send. False returns the full preview and
                issues no write at all.
        """
        wanted = _normalise_bulk_ids(opportunity_ids)

        # THE CAP IS CHECKED BEFORE THE QUEUE IS READ, so an over-long list is
        # refused without spending a request, and -- more importantly -- the
        # refusal cannot be confused with a resolution failure.
        if len(wanted) > MAX_BULK_APPLY:
            raise NothingToDo(
                "Refusing to bulk apply to %d opportunities: the cap is %d. This is a "
                "REFUSAL, not a truncation -- applying to the first %d of your %d and "
                "reporting success is the exact failure this tool is built not to "
                "have. Send a shorter list, or several calls, each with its own "
                "preview and its own confirm."
                % (len(wanted), MAX_BULK_APPLY, MAX_BULK_APPLY, len(wanted)),
                requested=len(wanted),
                cap=MAX_BULK_APPLY,
            )

        pending, queue_meta = self._live_pending_index()
        missing = [opp_id for opp_id in wanted if opp_id not in pending]
        if missing:
            raise NotFound(
                "Refusing to bulk apply: %d of the %d ids given are not in his CURRENT "
                "pending queue -- %s. An id that has already been actioned, expired out "
                "of the queue, or was simply mistyped must not ride along inside a bulk "
                "body where nothing would name it again. The pending queue was re-read "
                "for this check and holds %d record(s)%s."
                % (
                    len(missing),
                    len(wanted),
                    ", ".join(missing),
                    queue_meta["pending_records_read"],
                    ""
                    if queue_meta["complete"]
                    else " out of %r reported -- the read was TRUNCATED, so a missing id "
                    "here may exist further down the queue rather than not exist"
                    % (queue_meta["pending_total_reported"],),
                ),
                missing=missing,
            )

        records = [pending[opp_id] for opp_id in wanted]
        resolved = len(records)

        # THE SECOND, INDEPENDENT CONFIRMATION. It is compared against what
        # RESOLVED rather than against len(opportunity_ids), because the
        # resolved count is the number of applications that would actually be
        # sent -- and that is the number a human is being asked to agree to.
        if expected_count is None or int(expected_count) != resolved:
            raise ConfirmationRequired(
                "expected_count=%r does not match the %d opportunit%s that would be "
                "applied to (from %d id(s) given). Nothing has been sent. This check "
                "exists because a bulk apply collapses N confirmations into one: if "
                "the list changed length between the preview and this call, the count "
                "is the only thing that notices."
                % (
                    expected_count,
                    resolved,
                    "y" if resolved == 1 else "ies",
                    len(wanted),
                ),
                expected_count=expected_count,
                would_apply_to=resolved,
            )

        path, body = _build_bulk_apply_request(records)
        from . import shape

        shaped = [shape.shape_opportunity(raw) for raw in records]
        preview = {
            "would_send": {
                "method": "POST",
                "url": C.API_BASE + path,
                "json_body": body,
                "headers": {
                    "Content-Type": "application/json",
                    C.APPLY_CSRF_HEADER: "<from the csrftoken cookie>",
                    "Referer": C.SITE_BASE + "/",
                },
            },
            "action": "BULK APPLY",
            "would_apply_to_count": resolved,
            # NAMED, NEVER COUNTED. A caller who confirms "12 opportunities"
            # without seeing which twelve is the failure mode this whole tool
            # is built around, so the list is not optional, not truncated, and
            # not summarised.
            "would_apply_to": [
                {
                    "opportunity_id": record.get("id"),
                    "job_id": record.get("job_id"),
                    "company": record.get("company"),
                    "role": record.get("title"),
                    "match_score": record.get("match_score"),
                }
                for record in shaped
            ],
            "would_apply_to_lines": [
                "%s -- %s (opportunity %s, job %s)"
                % (
                    record.get("company") or "<unnamed company>",
                    record.get("title") or "<untitled role>",
                    record.get("id"),
                    record.get("job_id"),
                )
                for record in shaped
            ],
            "branch": (
                "ES (candidate_matching)" if C.APPLY_BRANCH_ES else "legacy (candidate_opportunity)"
            ),
            "body_carries_exactly_one_key": (
                "The bulk builder sets job_ids on the ES branch and opp_ids on the "
                "legacy branch, never both, and there is no is_interested key -- bulk "
                "is apply-only, so no bulk DECLINE exists to be built."
            ),
            "irreversible": True,
            "cap": MAX_BULK_APPLY,
            "queue_read": queue_meta,
            "contract": _contract("apply_bulk"),
            "warning": (
                "Instahyre applications CANNOT be withdrawn -- their FAQ says the "
                "application is sent automatically by the system. This sends %d of "
                "them in ONE request. Read every line of would_apply_to_lines above "
                "before confirming; that list is the only place each application is "
                "named." % resolved
            ),
            "the_site_pre_selects_everything": (
                "Instahyre's own bulk modal opens with every opportunity already "
                "ticked. This tool selects nothing by default and refuses an empty "
                "list, which is the opposite default on purpose."
            ),
            "never_run_live": (
                "No bulk apply has ever been sent by this server, so the RESPONSE "
                "shape is unknown territory. The request was read out of Instahyre's "
                "shipped JavaScript; it has never been serialized by a browser here, "
                "because the only way to make the site build one is to actually apply."
            ),
        }
        if not confirm:
            preview["confirmed"] = False
            preview["next"] = (
                "NOTHING HAS BEEN SENT. Re-run with confirm=True and "
                "expected_count=%d to apply to all %d, permanently."
                % (resolved, resolved)
            )
            return preview

        self._require_csrf("send a bulk apply")

        # A LIVE guard on the path actually about to be requested, asked of an
        # allowlist of two rather than of a blocklist. The single-apply guard in
        # inbound.submit_interest still refuses these same paths from its side,
        # and the READ tier still refuses them via MUTATING_PATH_MARKERS -- this
        # is the only door in the package that opens onto them.
        _guard_bulk_apply_sendable(path)

        log.warning(
            "irreversible BULK APPLY sent: %d opportunities (%s)",
            resolved,
            ", ".join(wanted),
        )
        # Two literal call sites rather than one call on a computed path, for
        # the same reason submit_interest spells its branch out twice: the
        # suite's POST census requires every write to name its endpoint as a
        # bare constant, so "which endpoints can this package POST to" stays
        # answerable without running it. The guard above has already proved
        # `path` is one of these two.
        if path == C.EP_APPLY_BULK_ES:
            response = self.http.post(
                C.EP_APPLY_BULK_ES, json_body=body, extra_headers={"Origin": C.SITE_BASE}
            )
        else:
            response = self.http.post(
                C.EP_APPLY_BULK_LEGACY, json_body=body, extra_headers={"Origin": C.SITE_BASE}
            )

        return {
            "confirmed": True,
            "sent": preview["would_send"],
            "requested_count": resolved,
            "requested": preview["would_apply_to"],
            "response": response if isinstance(response, dict) else {"raw": str(response)[:200]},
            "irreversible": True,
            "verification": self._verify_bulk_apply(wanted, shaped),
        }

    def _verify_bulk_apply(self, wanted: list, shaped: list) -> dict:
        """Re-read the pending queue and say WHICH applications actually took.

        THE RESPONSE IS NOT THE EVIDENCE HERE, and this is the one surface
        where that matters most. No bulk apply has ever been sent by this
        server, so nobody knows what a bulk response looks like -- whether it
        reports per-id outcomes, a count, or just a 200. Trusting it would be
        trusting a shape nobody has seen. The queue, on the other hand, is
        already understood: an opportunity that has been applied to LEAVES the
        pending facet. So the check is "which of these are no longer pending",
        which is a reading of state rather than of a status code.

        Never raises. A verification that could take down the report of an
        irreversible action already sent would destroy the only record of what
        happened at the exact moment it is most needed.
        """
        titles = {record.get("id"): record for record in shaped}
        try:
            pending, _ = self._live_pending_index()
        except InstahyreError as exc:
            return {
                "ok": False,
                "how": "the pending queue could not be re-read (%s), so which "
                "applications took is UNKNOWN. Read instahyre_list_opportunities "
                "before doing anything else -- do NOT re-send." % exc.kind,
                "applied": None,
                "still_pending": None,
            }

        applied = [opp_id for opp_id in wanted if opp_id not in pending]
        still_pending = [opp_id for opp_id in wanted if opp_id in pending]
        result = {
            "ok": not still_pending,
            "how": (
                "each id was looked for in a fresh read of the pending queue; an "
                "opportunity that has been applied to leaves that facet"
            ),
            "applied": [
                {
                    "opportunity_id": opp_id,
                    "company": (titles.get(opp_id) or {}).get("company"),
                    "role": (titles.get(opp_id) or {}).get("title"),
                }
                for opp_id in applied
            ],
            "still_pending": still_pending,
        }
        if still_pending:
            result["warning"] = (
                "%d of %d are STILL in the pending queue after the request. That may "
                "mean they did not take, or that Instahyre's queue has not caught up "
                "yet. Do NOT re-send on this evidence -- a second bulk apply to an "
                "opportunity that did take cannot be undone either. Re-read with "
                "instahyre_list_opportunities first."
                % (len(still_pending), len(wanted))
            )
        return result

    # -- the leaderboard cluster: the channel that asks HIM something ------
    #
    # THE INVERSION THIS SECTION EXISTS FOR. Every other method on this class
    # is him initiating something -- a reply, an application, a ticket. These
    # three read and answer a channel where INSTAHYRE IS THE ONE ASKING, and
    # the question it asks is terminal: were you hired at this company. Until
    # 2026-08-25 nothing in this server could see that a hire check existed.
    #
    # THE CHANNEL IS EMPTY TODAY, AND THAT IS A MEASUREMENT RATHER THAN AN
    # ASSUMPTION. Read from his own signed-in browser on 2026-08-25, all three
    # answering 200:
    #
    #   show_verify_modal       -> {"data": []}
    #   verify_hired_candidate  -> {"objects": [], "meta": {...}}
    #   get_opportunity_info    -> {"show_modal": false}
    #
    # So nothing in this cluster has ever been exercised against live data, and
    # every docstring here says so instead of implying a test that did not
    # happen. EMPTY IS NOT ABSENT: the endpoints exist, answer, and are
    # authenticated -- there is simply nothing pending on this account today.
    # One consequence is worth stating plainly rather than discovering later:
    # because both writes validate their id against a LIVE re-read, and both
    # live reads are empty, EVERY WRITE IN THIS SECTION REFUSES TODAY. That is
    # the gate working, not a defect.

    def _live_hire_checks(self) -> dict:
        """``show_verify_modal``, read fresh, with the two empty cases kept apart.

        ``use_cache`` does not arise here -- this endpoint is not cached
        anywhere in the package -- but the freshness requirement is the same one
        :meth:`_live_pending_index` documents: this read decides whether an
        answer is about to be sent about a hire that may already have been
        answered in the browser.

        ABSENT AND EMPTY ARE DIFFERENT FACTS and are reported as different
        facts. Instahyre's own reader tests ``if(response.data===undefined)``
        and returns before touching the list, so a payload with no ``data`` key
        is a shape the site itself treats as "say nothing", while ``data: []``
        is a definite "nothing pending". They arrive looking identical to a
        caller that only counts, and only one of them means the channel
        answered the question.
        """
        payload = self.http.get(
            C.EP_VERIFY_HIRED_SHOW_MODAL,
            params={"id": self.inbound.candidate_id()},
        )
        if not isinstance(payload, dict):
            return {"records": [], "data_key_present": False, "shape_was_unexpected": True}
        data = payload.get("data")
        return {
            "records": list(data) if isinstance(data, list) else [],
            "data_key_present": "data" in payload,
            "shape_was_unexpected": "data" in payload and not isinstance(data, list),
        }

    def _live_hire_queue(self) -> dict:
        """The ``verify_hired_candidate`` COLLECTION, read fresh.

        A DIFFERENT PROVENANCE FROM ITS SIBLING, and the difference is recorded
        rather than smoothed over. ``show_verify_modal`` has a shipped caller
        that this package copies; the collection ``get`` action is declared on
        the same factory and NO shipped code calls it. What makes it readable
        is that it was probed directly on 2026-08-25 and answered 200 with a
        tastypie envelope. So the modal read reproduces the browser, and this
        one does not reproduce anything -- it reads a route that exists.

        The truncation fact is carried for the same reason
        :meth:`_live_pending_index` carries it: an empty list and a truncated
        read are opposite facts that arrive looking identical.
        """
        payload = self.http.get(C.EP_VERIFY_HIRED)
        objects = payload.get("objects") if isinstance(payload, dict) else None
        meta = (payload.get("meta") if isinstance(payload, dict) else None) or {}
        records = list(objects) if isinstance(objects, list) else []
        total = meta.get("total_count")
        return {
            "records": records,
            "records_read": len(records),
            "total_reported": total,
            "complete": total is None or len(records) >= total,
        }

    def _live_rating_offer(self) -> dict:
        """``get_opportunity_info``, read fresh.

        ``show_modal`` is the site's own gate -- ``if(response.show_modal)`` --
        and ``data`` carries the two fields the rating write needs:
        ``resource_uri``, which IS the ``rating_uri`` the write sends, and
        ``asked_before``, which the site consults before it will accept a
        second ask-later. Both are read live rather than remembered, because a
        remembered ``asked_before`` is a guard that stops guarding the moment
        the caller restarts.

        On this account today the payload is ``{"show_modal": false}`` with no
        ``data`` object at all, so ``resource_uri`` is ``None`` and no
        ``rating_uri`` can match it.
        """
        payload = self.http.get(C.EP_CANDIDATE_RATING_INFO)
        if not isinstance(payload, dict):
            return {
                "show_modal": False,
                "resource_uri": None,
                "asked_before": None,
                "data_key_present": False,
            }
        data = payload.get("data")
        data = data if isinstance(data, dict) else {}
        return {
            "show_modal": bool(payload.get("show_modal")),
            "resource_uri": data.get("resource_uri"),
            "asked_before": data.get("asked_before"),
            "data_key_present": "data" in payload,
        }

    def pending_requests(self) -> dict:
        """What is Instahyre asking him RIGHT NOW. Reads only; changes nothing.

        ONE CALL ACROSS THE WHOLE CHANNEL, because the channel is the unit a
        person cares about. Three endpoints answer three halves of one
        question, and a caller who had to know all three names in advance
        would never have asked -- which is exactly how a hire check goes
        unanswered for a month.

        NOTHING PENDING IS A RESULT, NOT A FAILURE, and this method's whole
        contract is refusing to blur those two. An empty channel returns
        ``anything_pending: False`` with a sentence saying so, alongside the
        three reads that produced it -- never an error, never a bare empty
        dict a caller could read as "the tool did not work". The distinction
        matters more here than anywhere else in this package: the reason to
        call this is to find out whether a terminal status change is waiting,
        and "I could not tell" dressed as "nothing" is the one answer that
        would cost him the thing this tool exists to catch.

        A READ THAT FAILS STILL FAILS. If a session has lapsed or a route
        answers something unusable, the typed error propagates and the caller
        sees an error -- because the mirror of the paragraph above is just as
        important: "nothing pending" must never be what a broken read looks
        like.
        """
        checks = self._live_hire_checks()
        queue = self._live_hire_queue()
        offer = self._live_rating_offer()

        hire_checks = [_shape_hire_check(record) for record in checks["records"]]
        rating_pending = bool(offer["show_modal"] and offer["resource_uri"])
        pending_count = len(hire_checks) + (1 if rating_pending else 0)

        result = {
            "anything_pending": bool(pending_count),
            "pending_count": pending_count,
            "hire_checks": {
                "pending": hire_checks,
                "count": len(hire_checks),
                "source": C.EP_VERIFY_HIRED_SHOW_MODAL,
                "the_question": (
                    "Instahyre is asking whether he was HIRED at this company. "
                    "Answering it is a terminal status change and it is the only "
                    "question this platform puts to him directly."
                ),
                "answer_with": "instahyre_answer_hire_check",
                "data_key_present": checks["data_key_present"],
            },
            "hire_verification_queue": {
                "records": queue["records"],
                "count": queue["records_read"],
                "total_reported": queue["total_reported"],
                "complete": queue["complete"],
                "source": C.EP_VERIFY_HIRED,
                "provenance": (
                    "This collection route has NO caller in any captured bundle. It "
                    "was probed directly on 2026-08-25 and answered 200 with a "
                    "tastypie envelope, so it is read as a route that exists rather "
                    "than as a reproduction of anything the browser does."
                ),
            },
            "opportunity_rating": {
                "pending": rating_pending,
                "rating_uri": offer["resource_uri"],
                "asked_before": offer["asked_before"],
                "show_modal": offer["show_modal"],
                "scale": "%d to %d" % (C.RATING_SCALE_MIN, C.RATING_SCALE_MAX),
                "source": C.EP_CANDIDATE_RATING_INFO,
                "answer_with": "instahyre_rate_opportunity",
            },
        }

        if pending_count:
            result["summary"] = (
                "%d thing(s) pending: %d hire check(s) and %s"
                % (
                    pending_count,
                    len(hire_checks),
                    "an opportunity rating" if rating_pending else "no rating request",
                )
            )
        else:
            result["summary"] = (
                "NOTHING PENDING. Instahyre is not asking him anything right now. "
                "All three endpoints answered and all three answered empty: no hire "
                "check, no verification row, and show_modal is false on the rating "
                "offer. This is a clean result, not a failed read -- a read that "
                "failed would have raised rather than returned this."
            )
        result["empty_is_a_result_not_an_error"] = (
            "anything_pending is False only when every read SUCCEEDED and returned "
            "nothing. Any read that fails raises a typed error instead, so this "
            "field never stands in for 'could not tell'."
        )
        result["what_this_channel_is"] = (
            "The only surface on Instahyre where the platform asks HIM something "
            "rather than the other way round. It carries a terminal status change "
            "(hired) that no other tool on this server can see."
        )
        result["never_seen_populated"] = (
            "This cluster has never been read non-empty. Measured empty on "
            "2026-08-25 from his own signed-in session, all three routes 200, so "
            "the shape of a POPULATED record is read out of Instahyre's shipped "
            "JavaScript rather than out of a response anybody has seen."
        )
        return result

    def answer_hire_check(
        self, hired_id: Any, choice: Any, *, confirm: bool = False
    ) -> dict:
        """Answer ONE "were you hired here?" question. A TERMINAL status change.

        WHAT THIS ACTUALLY DOES, said before the arguments. Instahyre is asking
        whether he took a job at a named company, and this sends the answer. It
        is the only write in this package that reports an OUTCOME rather than
        an intent, and there is no shipped path anywhere in their product that
        edits or retracts one.

        THE ID MUST COME FROM A LIVE READ. Before anything is sent,
        ``show_verify_modal`` is re-read and ``hired_id`` must be one of the
        checks it currently offers. A fabricated id is therefore impossible to
        submit -- not discouraged, impossible -- and today, with that read
        empty on this account, EVERY call here refuses. That refusal is the
        correct behaviour and is tested as a gate rather than worked around.

        WHAT IS NOT KNOWN, AND IS NOT GUESSED. The MEANING of ``choice`` is
        unmeasured except for 0. ``$scope.closeResponse`` sends ``choice:0`` --
        that is the dismiss branch. Every other value comes from
        ``setCandidateChoice``, which is defined in the shipped bundle and
        called nowhere in any of the ten captured ones; its callers are
        ng-click attributes in an HTML template no capture holds. So this
        method sends the integer it is handed, prints in the preview that only
        0 is measured, and refuses to label an unmeasured value as if it meant
        "yes".

        Args:
            hired_id: The ``hired_id`` of a check currently offered by
                instahyre_pending_requests. Validated against a live re-read.
            choice: The integer answer. Only 0 (dismiss) has a shipped caller;
                see the preview's own warning about the rest.
            confirm: Must be True to send. False (the default) returns the
                exact request that would go out and issues nothing at all.
        """
        wanted = _normalise_hired_id(hired_id)
        answer = _normalise_choice(choice)

        checks = self._live_hire_checks()
        offered = {
            str(record.get("hired_id")): record
            for record in checks["records"]
            if record.get("hired_id") is not None
        }
        if wanted not in offered:
            raise NotFound(
                "Refusing to answer hire check %r: it is not one of the %d check(s) "
                "Instahyre is currently offering%s. The modal was re-read for this "
                "call rather than remembered, because an answer aimed at a check that "
                "has already been answered -- or that never existed -- is a report "
                "about a job nobody asked about, and it cannot be retracted.%s"
                % (
                    wanted,
                    len(offered),
                    "" if offered else " (the channel is EMPTY -- no hire check is "
                    "pending on this account, which is the normal state and not an "
                    "error)",
                    ""
                    if checks["data_key_present"]
                    else " Note also that the payload carried NO data key at all, which "
                    "is the shape Instahyre's own reader treats as 'say nothing' "
                    "rather than as 'nothing pending'.",
                ),
                hired_id=wanted,
                offered=sorted(offered),
            )

        record = _shape_hire_check(offered[wanted])
        body = {"id": wanted, "choice": answer}
        query = {"id": wanted}
        preview = {
            "would_send": {
                "method": "POST",
                "url": C.API_BASE + C.EP_VERIFY_HIRED_SUBMIT_RESPONSE,
                "query_string": query,
                "json_body": body,
                "headers": {
                    "Content-Type": "application/json",
                    C.APPLY_CSRF_HEADER: "<from the csrftoken cookie>",
                    "Referer": C.SITE_BASE + "/",
                },
            },
            "action": "ANSWER A HIRE CHECK",
            "answering": record,
            "choice": answer,
            "why_the_id_is_in_two_places": (
                "The resource declares {id:'@id'} as its paramDefaults, so Angular "
                "extracts id out of the body and repeats it in the QUERY STRING while "
                "the body still carries it. Both halves are reproduced because both "
                "halves are what the browser sends; see "
                "constants.ANGULAR_ACTION_PARAMS_RIDE_THE_QUERY_STRING for which half "
                "is captured off disk and which is library behaviour."
            ),
            "choice_meaning": C.HIRE_CHOICE_MEANINGS_ARE_UNMEASURED,
            "choice_is_the_measured_dismiss_value": answer == C.HIRE_CHOICE_DISMISS,
            "irreversible": True,
            "contract": _contract("hire_check"),
            "warning": (
                "This reports an EMPLOYMENT OUTCOME to the platform and there is no "
                "shipped path that edits or retracts one. Read 'answering' above and "
                "make sure it is the right company before confirming."
            ),
            "never_run_live": (
                "No hire-check answer has ever been sent by this server, and none can "
                "be until Instahyre offers a check. The request was read out of two "
                "shipped callers; it has never been serialized by a browser here."
            ),
        }
        if not confirm:
            preview["confirmed"] = False
            preview["next"] = (
                "NOTHING HAS BEEN SENT. Re-run with confirm=True to answer hire check "
                "%s with choice=%d." % (wanted, answer)
            )
            return preview

        self._require_csrf("answer a hire check")
        _guard_leaderboard_sendable(C.EP_VERIFY_HIRED_SUBMIT_RESPONSE)

        log.warning("irreversible HIRE CHECK answer sent: id=%s choice=%s", wanted, answer)
        response = self.http.post(
            C.EP_VERIFY_HIRED_SUBMIT_RESPONSE,
            params=query,
            json_body=body,
            extra_headers={"Origin": C.SITE_BASE},
        )

        return {
            "confirmed": True,
            "sent": preview["would_send"],
            "answered": record,
            "choice": answer,
            "response": response if isinstance(response, dict) else {"raw": str(response)[:200]},
            "irreversible": True,
            "verification": self._verify_hire_check(wanted),
        }

    def _verify_hire_check(self, hired_id: str) -> dict:
        """Re-read the modal and say whether the check has left it.

        THE RESPONSE IS NOT THE EVIDENCE. Nobody has ever seen this endpoint's
        reply, so trusting its shape would be trusting something unmeasured at
        the exact moment an irreversible thing has already happened. The modal
        is the readable state: a check that has been answered should stop being
        offered.

        Never raises, for the same reason :meth:`_verify_bulk_apply` never
        does -- a verification that could destroy the report of an action
        already taken fails precisely when the report matters most.
        """
        try:
            checks = self._live_hire_checks()
        except InstahyreError as exc:
            return {
                "ok": False,
                "how": "the hire-check modal could not be re-read (%s), so whether "
                "the answer registered is UNKNOWN. Do NOT re-send; run "
                "instahyre_pending_requests before doing anything else." % exc.kind,
                "still_offered": None,
            }
        still = [
            str(record.get("hired_id"))
            for record in checks["records"]
            if str(record.get("hired_id")) == hired_id
        ]
        result = {
            "ok": not still,
            "how": (
                "the modal was re-read; an answered check should no longer be offered"
            ),
            "still_offered": bool(still),
        }
        if still:
            result["warning"] = (
                "Hire check %s is STILL being offered after the answer. That may mean "
                "it did not register, or that Instahyre has not caught up. Do NOT "
                "re-send on this evidence -- an employment outcome reported twice "
                "cannot be un-reported either." % hired_id
            )
        return result

    def rate_opportunity(
        self,
        rating_uri: Any,
        rating: Any = None,
        *,
        ask_later: bool = False,
        confirm: bool = False,
    ) -> dict:
        """Rate ONE opportunity 1-5, or ask to be asked later.

        THE SITE'S OWN TWO GUARDS ARE REPRODUCED, not improved on, because a
        rail of ours dressed up as the platform's is the confusion this
        package's register exists to avoid. ``$scope.submitRating`` refuses to
        send when ``ask_later`` is false and no rating was picked, and refuses
        to send a SECOND ask-later once ``asked_before`` is set on the live
        payload. Both refusals happen here, both read off the live payload
        rather than off memory.

        THE URI MUST COME FROM A LIVE READ. ``get_opportunity_info`` is re-read
        before anything is sent and ``rating_uri`` must equal the
        ``resource_uri`` it currently offers. A fabricated uri cannot be
        submitted, and today -- with that endpoint answering
        ``{"show_modal": false}`` on this account -- EVERY call here refuses.

        WHERE THE FIELDS GO. All three ride the QUERY STRING, because
        ``submit_rating`` declares them as action-level ``params``, and the same
        object is ALSO the JSON body. Reproducing one half only would be a
        guessed request; see
        ``constants.ANGULAR_ACTION_PARAMS_RIDE_THE_QUERY_STRING``. ``rating``
        goes out as ``null`` on the ask-later branch on purpose -- that is
        ``$scope.rating``, which is null until a star is clicked -- and this
        client drops a null from the query string exactly as Angular's own
        parameter serializer does, while keeping it in the body.

        Args:
            rating_uri: The ``resource_uri`` currently offered by
                instahyre_pending_requests. Validated against a live re-read.
            rating: 1 to 5. May be None only when ask_later is True.
            ask_later: Send the defer answer instead of a rating.
            confirm: Must be True to send. False (the default) returns the
                exact request that would go out and issues nothing at all.
        """
        wanted = _normalise_rating_uri(rating_uri)
        score = _normalise_rating(rating, ask_later=bool(ask_later))
        defer = bool(ask_later)

        offer = self._live_rating_offer()
        if not offer["show_modal"] or not offer["resource_uri"]:
            raise NotFound(
                "Refusing to rate %r: Instahyre is not currently offering a rating. "
                "get_opportunity_info was re-read for this call and answered "
                "show_modal=%r with %s. Nothing is pending, which is the normal state "
                "of this channel on this account and not an error -- but it means "
                "there is no opportunity to rate and no uri that could match."
                % (
                    wanted,
                    offer["show_modal"],
                    "no resource_uri"
                    if not offer["resource_uri"]
                    else "resource_uri %r" % offer["resource_uri"],
                ),
                rating_uri=wanted,
            )
        if wanted != offer["resource_uri"]:
            raise NotFound(
                "Refusing to rate %r: that is not the opportunity Instahyre is asking "
                "about. The live offer names %r. The uri is re-read rather than "
                "remembered because a rating aimed at the wrong opportunity is a "
                "judgement recorded against an employer nobody meant to judge."
                % (wanted, offer["resource_uri"]),
                rating_uri=wanted,
                offered=offer["resource_uri"],
            )
        if defer and offer["asked_before"]:
            raise NothingToDo(
                "Refusing to ask later a second time: the live payload says "
                "asked_before=%r. This is Instahyre's own rule, not ours -- "
                "$scope.submitRating returns without sending when ask_later is set "
                "and asked_before is true -- and it is read off the live payload "
                "rather than remembered." % (offer["asked_before"],),
                rating_uri=wanted,
            )

        body = {"rating_uri": wanted, "ask_later": defer, "rating": score}
        # THE SAME THREE FIELDS, IN BOTH PLACES, which is what Angular does
        # here -- with one asymmetry that is real rather than tidy. A null
        # rating is DROPPED from the query string and KEPT in the body, by
        # Angular's own parameter serializer and by this client's alike. The
        # query is therefore filtered here rather than at the transport, so
        # that the PREVIEW shows what will actually go out: a preview that
        # printed rating=None in a query string the wire would not carry would
        # be describing a request nobody sends, on a surface whose whole gate
        # is the preview.
        query = {key: value for key, value in body.items() if value is not None}
        preview = {
            "would_send": {
                "method": "POST",
                "url": C.API_BASE + C.EP_CANDIDATE_RATING_SUBMIT,
                "query_string": query,
                "json_body": body,
                "headers": {
                    "Content-Type": "application/json",
                    C.APPLY_CSRF_HEADER: "<from the csrftoken cookie>",
                    "Referer": C.SITE_BASE + "/",
                },
            },
            "action": "ASK LATER" if defer else "SUBMIT A RATING",
            "rating": score,
            "ask_later": defer,
            "rating_uri": wanted,
            "asked_before": offer["asked_before"],
            "scale": "%d to %d" % (C.RATING_SCALE_MIN, C.RATING_SCALE_MAX),
            "why_the_fields_are_in_two_places": (
                "submit_rating declares rating_uri, ask_later and rating as "
                "action-level params, which in Angular are URL parameters and ride "
                "the query string, while the same object is also the POST data. Both "
                "halves are reproduced; see "
                "constants.ANGULAR_ACTION_PARAMS_RIDE_THE_QUERY_STRING for which half "
                "is captured off disk and which is library behaviour."
            ),
            "irreversible": True,
            "contract": _contract("opportunity_rating"),
            "warning": (
                "A rating is a judgement recorded against a named employer and there "
                "is no shipped path that edits or withdraws one."
            ),
            "never_run_live": (
                "No rating has ever been sent by this server, and none can be until "
                "Instahyre offers one. The request was read out of the shipped "
                "caller; it has never been serialized by a browser here."
            ),
        }
        if not confirm:
            preview["confirmed"] = False
            preview["next"] = (
                "NOTHING HAS BEEN SENT. Re-run with confirm=True to %s."
                % ("ask later" if defer else "submit a rating of %s" % score)
            )
            return preview

        self._require_csrf("submit an opportunity rating")
        _guard_leaderboard_sendable(C.EP_CANDIDATE_RATING_SUBMIT)

        log.warning(
            "irreversible OPPORTUNITY RATING sent: uri=%s rating=%s ask_later=%s",
            wanted,
            score,
            defer,
        )
        response = self.http.post(
            C.EP_CANDIDATE_RATING_SUBMIT,
            params=query,
            json_body=body,
            extra_headers={"Origin": C.SITE_BASE},
        )

        return {
            "confirmed": True,
            "sent": preview["would_send"],
            "rating": score,
            "ask_later": defer,
            "rating_uri": wanted,
            "response": response if isinstance(response, dict) else {"raw": str(response)[:200]},
            "irreversible": True,
            "verification": self._verify_rating(wanted),
        }

    def _verify_rating(self, rating_uri: str) -> dict:
        """Re-read the offer and say whether it has stopped being made.

        State, not status code, for the same reason every other verification
        here reads state: nobody has seen this endpoint's reply.

        THE ONE HONEST LIMIT, stated rather than papered over: an ask-later
        that succeeded may legitimately leave the SAME uri on offer, since
        deferring is not answering. So a still-offered uri is reported as a
        fact and not as a failure, and the caller is told which reading applies.
        """
        try:
            offer = self._live_rating_offer()
        except InstahyreError as exc:
            return {
                "ok": False,
                "how": "the rating offer could not be re-read (%s), so whether the "
                "rating registered is UNKNOWN. Do NOT re-send; run "
                "instahyre_pending_requests first." % exc.kind,
                "still_offered": None,
            }
        still = bool(offer["show_modal"]) and offer["resource_uri"] == rating_uri
        return {
            "ok": not still,
            "how": (
                "get_opportunity_info was re-read; a rated opportunity should stop "
                "being offered. An ASK LATER may legitimately still be offered, so "
                "still_offered is a fact here rather than a verdict"
            ),
            "still_offered": still,
            "asked_before": offer["asked_before"],
        }

    # -- 1. support tickets (WIRE) -----------------------------------------

    def support_ticket(self, message: str, *, confirm: bool = False) -> dict:
        """Raise a support ticket. Reaches a human queue and cannot be retracted.

        The whole body was wire-captured, including the detail a guess would
        have got wrong twice over: the candidate is named by RESOURCE URI, not
        by integer id, and the wire URL carries no trailing slash even though
        the factory declares one.
        """
        text = (message or "").strip()
        if not text:
            raise NothingToDo(
                "A support ticket with an empty message would open a real ticket in a "
                "human queue saying nothing. Say what the problem is."
            )

        candidate_id = self.inbound.candidate_id()
        body = {
            "candidate": "/api/v1" + C.EP_LIMITED_CANDIDATE + str(candidate_id),
            "message": text,
            "attachments": [],
        }
        preview = {
            "would_send": {
                "method": "POST",
                "url": C.API_BASE + C.EP_SUPPORT_QUERY,
                "content_type": C.SUPPORT_QUERY_CONTENT_TYPE,
                "body": body,
            },
            "contract": _contract("support_tickets"),
            "irreversible": (
                "This opens a ticket with Instahyre's support team. There is no delete "
                "and no edit -- a person reads whatever is sent."
            ),
            "attachments": (
                "Always empty. The site's form accepts files; this tool does not send "
                "them, so nothing on this machine is uploaded by accident."
            ),
        }
        if not confirm:
            preview["confirmed"] = False
            preview["next"] = "Re-run with confirm=True to actually raise it."
            return preview

        response = self.http.post(C.EP_SUPPORT_QUERY, json_body=body)
        return {
            "confirmed": True,
            "sent": preview["would_send"],
            "response": response,
            "note": (
                "Raised. Field errors, if any, arrive nested under a %r key -- that "
                "envelope is part of the captured contract." % C.SUPPORT_QUERY_ERROR_ENVELOPE
            ),
        }

    # -- 2. saved-search job alerts (SHIPPED) ------------------------------

    def toggle_job_alert(
        self, saved_search_id: int, enable: bool, *, confirm: bool = False
    ) -> dict:
        """Turn email alerts on or off for one saved search.

        Reads first, always. The PATCH body carries ``search_string`` alongside
        the flag -- Instahyre's ``toggleAlerts`` is not a flag-only update, and
        a flag-only PATCH is a request the site never makes -- so the current
        row has to be in hand before anything can be built.
        """
        listing = self.inbound.saved_searches()
        rows = listing.get("saved_searches") or []
        if not rows:
            return {
                "confirmed": False,
                "changed": False,
                "saved_search_count": 0,
                "diagnosis": listing.get("diagnosis"),
                "why_nothing_to_toggle": (
                    "An alert is a field ON a saved search, so with zero saved searches "
                    "there is no row to act on. This is a real zero, not a swallowed "
                    "failure: the read answered 200 with a tastypie envelope, a dead "
                    "session would have raised AuthRequired inside the HTTP client, and "
                    "a payload without an 'objects' list would have raised ApiError "
                    "before the count was taken."
                ),
                "fix": (
                    "Save a search first, at " + C.SITE_BASE + "/search-jobs -- set at "
                    "least three filters, press Show results, then Save Search. Note "
                    "that the save-search control renders hidden on the opportunities "
                    "page; the search page is where it is reachable."
                ),
            }

        row = next((r for r in rows if r.get("id") == saved_search_id), None)
        if row is None:
            raise NothingToDo(
                "No saved search with id %r on this account. Ids that exist: %s. "
                "Nothing was sent." % (saved_search_id, sorted(r.get("id") for r in rows))
            )

        search_string = row.get("search_string")
        if not search_string:
            raise InstahyreError(
                "Saved search %r carries no search_string, and the captured PATCH body "
                "requires it -- toggleAlerts sends the flag alongside the query, never "
                "on its own. Refusing to send a shape the site does not produce."
                % saved_search_id
            )

        gate = self._alert_gate(search_string)
        if enable and not gate["passes"]:
            return {
                "confirmed": False,
                "changed": False,
                "gate": gate,
                "refused": (
                    "Instahyre's own client refuses to enable alerts below three "
                    "filters -- canEnableJobAlerts forces the flag false rather than "
                    "sending it. Sending it anyway would be a request the site never "
                    "makes, which is the exact failure this server exists to avoid."
                ),
                "fix": "Add filters to this saved search, then try again.",
            }

        body = {
            "id": saved_search_id,
            "search_string": search_string,
            "job_alert_enabled_at": bool(enable),
        }
        preview = {
            "would_send": {
                "method": C.SAVED_SEARCH_TOGGLE_METHOD,
                "url": C.API_BASE + C.EP_SAVED_SEARCH_DETAIL + str(saved_search_id),
                "body": body,
            },
            "currently": row.get("alerts_on"),
            "would_become": bool(enable),
            "gate": gate,
            "contract": _contract("saved_search_alert_toggle"),
            "no_frequency": (
                "There is no alert frequency on this platform. No field for one exists "
                "in the resource, the toggle, or the UI -- do not offer a schedule."
            ),
            "reversible": "Yes. Run this again with enable inverted.",
        }
        if not confirm:
            preview["confirmed"] = False
            preview["next"] = "Re-run with confirm=True to apply it."
            return preview

        response = self.http.patch(
            C.EP_SAVED_SEARCH_DETAIL + str(saved_search_id), json_body=body
        )
        return {
            "confirmed": True,
            "changed": True,
            "sent": preview["would_send"],
            "response": response,
            "note": (
                "Field errors, if any, arrive nested under a %r key."
                % C.SAVED_SEARCH_ERROR_ENVELOPE
            ),
        }

    def _alert_gate(self, search_string: str) -> dict:
        """Approximate Instahyre's ``canEnableJobAlerts``, and say that it is one.

        The measured rule is: at least three non-empty filters drawn from
        ``sidebarFilterFields``, with ``job_categories`` always excluded and
        ``job_type`` excluded unless the candidate is a fresher. The exclusions
        are in hand; the sidebarFilterFields LIST is not -- it does not appear
        in the captured evidence. So this counts every non-empty parameter
        except the two exclusions, which can only ever count HIGH: a parameter
        the sidebar does not own inflates the count rather than suppressing it.

        Stated rather than hidden, because a gate that reports a number it
        cannot fully justify is worth less than one that reports the number and
        its error direction.
        """
        pairs = parse_qsl(search_string, keep_blank_values=False)
        counted = sorted(
            {key for key, value in pairs if value not in (None, "") and key not in C.SAVED_SEARCH_ALERT_GATE_EXCLUDES}
        )
        return {
            "non_empty_filters_counted": len(counted),
            "fields": counted,
            "required": C.SAVED_SEARCH_ALERT_MIN_FILTERS,
            "passes": len(counted) >= C.SAVED_SEARCH_ALERT_MIN_FILTERS,
            "excluded_from_the_count": list(C.SAVED_SEARCH_ALERT_GATE_EXCLUDES),
            "approximation": (
                "This count can only run HIGH. The real gate counts only fields in the "
                "client's sidebarFilterFields list, which is not in the captured "
                "evidence, so a parameter the sidebar does not own is counted here and "
                "would not be counted there."
            ),
        }

    # -- 3. referrals (SHIPPED) --------------------------------------------

    def referral_link(self, *, confirm: bool = False) -> dict:
        """Ask Instahyre for his own referral link. Mails nobody.

        It is a POST, so it is gated like every other write here, but the blast
        radius is his own account: the response hands back a ``referral_url``
        and a name, and nothing is sent to anyone.
        """
        who = self._referrer()
        body = {"name": who["name"], "email": who["email"], "referral_url": ""}
        preview = {
            "would_send": {
                "method": "POST",
                "url": C.API_BASE + C.EP_REFERRAL,
                "body": body,
            },
            "contacts_nobody": (
                "This asks for a link and nothing else. No invitation is sent and no "
                "third party is contacted."
            ),
            "contract": _contract("referrals"),
        }
        if not confirm:
            preview["confirmed"] = False
            preview["next"] = "Re-run with confirm=True to request the link."
            return preview

        response = self.http.post(C.EP_REFERRAL, json_body=body)
        url = response.get("referral_url") if isinstance(response, dict) else None
        return {
            "confirmed": True,
            "referral_url": url,
            "response": response,
            "note": (
                "referral_url round-trips: it goes out empty and comes back populated."
            ),
        }

    def referral_contacts(self) -> dict:
        """The Gmail contacts Instahyre would offer as invitees. A READ.

        This is a GET in Instahyre's own client, and that fact is what makes an
        honest invite gate possible at all: the list of who WOULD be contacted
        can be read without sending anything. Each contact carries a
        server-supplied ``preselect`` flag, which the site honours by ticking
        those boxes for you -- so it is reported here rather than applied.
        """
        payload = self.http.get(C.EP_REFERRAL_CONTACTS)
        contacts = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(contacts, list):
            contacts = []

        result = {
            "contacts": [
                {
                    "name": c.get("name"),
                    "email": c.get("email"),
                    "instahyre_would_preselect": bool(c.get("preselect")),
                }
                for c in contacts
                if isinstance(c, dict)
            ],
            "count": len(contacts),
            "sends_nothing": (
                "A read. Instahyre's own client fetches this list with GET before any "
                "invitation exists."
            ),
            "preselect_note": (
                "Instahyre marks some contacts preselected and the site ticks them "
                "automatically. This tool reports the flag and acts on none of them -- "
                "a default-selected recipient is a recipient nobody chose."
            ),
        }
        if not result["count"]:
            result["diagnosis"] = {
                "reason": "no_contacts_returned",
                "explanation": (
                    "The request answered, and its 'data' list was empty or absent. The "
                    "three situations this distinguishes: he has never granted Instahyre "
                    "access to his Google contacts, in which case the site's own flow "
                    "opens a Google consent window that this server deliberately does "
                    "not drive; the grant exists but returned nothing; or the payload "
                    "shape changed. A dead session cannot reach this branch -- a 401 "
                    "raises AuthRequired inside the HTTP client."
                ),
                "grant_flow": (
                    "Granting happens in a browser at " + C.SITE_BASE + "/invite/ . This "
                    "server never drives an OAuth consent screen; the capture harness "
                    "aborts every request to a third-party identity host on purpose."
                ),
            }
        return result

    def send_referral_invites(
        self, emails: list[str], *, confirm: bool = False
    ) -> dict:
        """Invite people to Instahyre from his account. IRREVERSIBLE.

        These are real people who know him, the mail comes from his name, and
        Instahyre has no unsend. The preview names every recipient, and there
        is no path through this function that sends without having been able to
        produce that list first.
        """
        cleaned, rejected = _normalise_addresses(emails)
        if rejected:
            raise NothingToDo(
                "These do not look like email addresses and nothing was sent: %s. An "
                "invitation cannot be recalled, so a malformed address is refused here "
                "rather than attempted." % rejected
            )
        if not cleaned:
            raise NothingToDo(
                "No addresses given, so there is nobody to invite and nothing to show "
                "you. Nothing was sent."
            )
        if len(cleaned) > MAX_INVITES_PER_CALL:
            raise NothingToDo(
                "%d addresses is more than this tool will send in one call (cap %d). "
                "The cap is not a platform limit; it is a rail on the one surface here "
                "that reaches other people permanently. Split it, and each call will "
                "show you its own recipients." % (len(cleaned), MAX_INVITES_PER_CALL)
            )

        who = self._referrer()
        # Exactly the shape Instahyre's typed-invite path builds. `name` is null
        # per constructInvitationsDict; inventing one would be dressing up the
        # consent this gate exists to obtain.
        friends = [{"name": None, "email": address} for address in cleaned]
        body = {
            "friends": friends,
            "email_list": ",".join(cleaned),
            "name": who["name"],
            "email": who["email"],
        }

        preview = {
            "would_contact": [
                {"name": None, "email": address, "how": "typed address, no name sent"}
                for address in cleaned
            ],
            "recipient_count": len(cleaned),
            "from": {"name": who["name"], "email": who["email"]},
            "would_send": {
                "method": "POST",
                "url": C.API_BASE + C.EP_REFERRAL_INVITES,
                "body": body,
            },
            "irreversible": (
                "An invitation cannot be unsent. Instahyre has no retraction anywhere "
                "in its product, and these arrive in the inboxes of people who know "
                "him, over his name. Unlike an application, there is no version of "
                "this that is merely a wasted slot."
            ),
            "names_are_null_on_purpose": (
                "Instahyre's typed-invite path sends {'name': null, 'email': ...}. This "
                "sends the same. Use instahyre_referral_contacts to see the names "
                "Instahyre itself holds for these addresses."
            ),
            "contract": _contract("referrals"),
        }
        if not confirm:
            preview["confirmed"] = False
            preview["next"] = (
                "Read the recipient list above. Re-run with confirm=True only if every "
                "address on it should receive an invitation from him."
            )
            return preview

        response = self.http.post(C.EP_REFERRAL_INVITES, json_body=body)
        return {
            "confirmed": True,
            "contacted": preview["would_contact"],
            "recipient_count": len(cleaned),
            "sent": preview["would_send"],
            "response": response,
            "note": "Sent. There is no unsend.",
        }


# ---------------------------------------------------------------------------
# The leaderboard cluster's normalisers
#
# EACH ONE REFUSES RATHER THAN REPAIRS, which is the same rule the bulk-apply
# normaliser follows and for the same reason: a repaired argument is a
# different request than the one the caller read in the preview, and both of
# these surfaces are irreversible.
# ---------------------------------------------------------------------------


def _shape_hire_check(record: Any) -> dict:
    """One hire-check row, named the way a person would name it.

    THE FIELD NAMES ARE THE SITE'S, read off its own reader rather than
    invented: ``rec_name``, ``company_name``, ``designation``, ``month``,
    ``day``, ``hired_id``, ``can_image``, ``company_image``, ``ask_me_later_at``
    -- every one of them assigned in the shipped block that populates
    ``$scope.verifyHireData``.

    THE TWO IMAGE URLS ARE NAMED HERE AND DELIBERATELY NOT ECHOED. The site
    reads them to draw two avatars in a modal; they carry nothing a person
    deciding how to answer would use, and one of them is a URL to his own
    photograph. Their PRESENCE is reported, because that is the part that says
    something about the payload; their values are not, because echoing a
    personal asset URL into a tool result buys nothing.

    NEVER SEEN POPULATED. Every field below comes from shipped source. The live
    endpoint answered ``{"data": []}`` on 2026-08-25, so no record of this
    shape has ever been read.
    """
    row = record if isinstance(record, dict) else {}
    return {
        "hired_id": row.get("hired_id"),
        "company": row.get("company_name"),
        "designation": row.get("designation"),
        "recruiter": row.get("rec_name"),
        "joining_day": row.get("day"),
        "joining_month": row.get("month"),
        "ask_me_later_at": row.get("ask_me_later_at"),
        "has_candidate_image": bool(row.get("can_image")),
        "has_company_image": bool(row.get("company_image")),
    }


def _normalise_hired_id(hired_id: Any) -> str:
    """One non-empty id, as a string. Refuses anything else."""
    if isinstance(hired_id, bool) or hired_id is None:
        raise NothingToDo(
            "A hire check needs an id. Read one off instahyre_pending_requests -- "
            "this server will not invent one, and an id that did not come from a "
            "live read cannot be submitted anyway.",
            hired_id=repr(hired_id),
        )
    text = str(hired_id).strip()
    if not text:
        raise NothingToDo(
            "A hire check needs a non-empty id. Read one off "
            "instahyre_pending_requests.",
            hired_id=repr(hired_id),
        )
    return text


def _normalise_choice(choice: Any) -> int:
    """The answer, as an integer, with NO meaning attached to it.

    A BOOLEAN IS REFUSED even though Python would happily widen it, because
    ``True`` would silently become ``choice=1`` -- a value whose meaning is
    exactly what this cluster does not know. A caller who typed a boolean meant
    something, and quietly turning it into an unmeasured integer on an
    irreversible surface is the class of repair this module refuses everywhere
    else.
    """
    if isinstance(choice, bool) or not isinstance(choice, int):
        raise NothingToDo(
            "choice must be an integer. %r is not one, and it is not coerced: only "
            "choice=0 has a shipped caller (the dismiss branch), so this server "
            "cannot tell you what any other value means and will not turn a "
            "different type into one. %s" % (choice, C.HIRE_CHOICE_MEANINGS_ARE_UNMEASURED),
            choice=repr(choice),
        )
    return choice


def _normalise_rating_uri(rating_uri: Any) -> str:
    """One non-empty resource uri, as a string. Refuses anything else."""
    if not isinstance(rating_uri, str) or not rating_uri.strip():
        raise NothingToDo(
            "rating_uri must be the non-empty resource_uri that "
            "instahyre_pending_requests reports. This server never assembles one: "
            "the uri is the platform's own handle for the opportunity being rated, "
            "and a constructed one would name a different row or none at all.",
            rating_uri=repr(rating_uri),
        )
    return rating_uri.strip()


def _normalise_rating(rating: Any, *, ask_later: bool) -> Optional[int]:
    """1 to 5, or None -- and None is only allowed on the ask-later branch.

    THIS IS INSTAHYRE'S RULE, reproduced rather than invented:
    ``if(!ask_later && $scope.rating==null){$scope.showRatingError=true;return;}``
    Their page refuses to submit a rating that has no rating, so this refuses
    it too, at the same place and for the same reason.

    The bounds are read off the controller -- ``LOWEST_RATING=1``,
    ``HIGHEST_RATING=5``, and ``ratingSelected`` walking ``i=1..5`` -- not
    guessed from the number of stars in a screenshot.
    """
    if rating is None:
        if not ask_later:
            raise NothingToDo(
                "A rating submission needs a rating. Instahyre's own page refuses "
                "this exact case (submitRating returns early when ask_later is false "
                "and the rating is null), so this is their rule reproduced, not a "
                "rail of ours. Pass rating=%d..%d, or ask_later=True to defer."
                % (C.RATING_SCALE_MIN, C.RATING_SCALE_MAX),
                rating=None,
                ask_later=ask_later,
            )
        return None
    if isinstance(rating, bool) or not isinstance(rating, int):
        raise NothingToDo(
            "rating must be an integer between %d and %d. %r is not one, and it is "
            "not coerced." % (C.RATING_SCALE_MIN, C.RATING_SCALE_MAX, rating),
            rating=repr(rating),
        )
    if not C.RATING_SCALE_MIN <= rating <= C.RATING_SCALE_MAX:
        raise NothingToDo(
            "rating must be between %d and %d; %d is outside the scale Instahyre's "
            "own widget can produce. The bounds are read off its controller "
            "(LOWEST_RATING=%d, HIGHEST_RATING=%d), so an out-of-range value is a "
            "request the site could not have made."
            % (
                C.RATING_SCALE_MIN,
                C.RATING_SCALE_MAX,
                rating,
                C.RATING_SCALE_MIN,
                C.RATING_SCALE_MAX,
            ),
            rating=rating,
        )
    return rating


def _normalise_bulk_ids(opportunity_ids: Optional[list]) -> list:
    """The caller's id list, checked into shape. Never rewritten into a valid one.

    THREE REFUSALS, and none of them is a repair. That is the distinction that
    matters on this surface: every "helpful" normalisation available here --
    dropping a duplicate, skipping a blank, coercing a stray type -- silently
    changes HOW MANY applications get sent, and how many is the number the
    caller is being asked to confirm. So each one raises instead.

    * EMPTY IS REFUSED rather than treated as "all". Instahyre's own modal
      pre-selects every opportunity, so "nothing selected means everything" is
      a real interpretation on this platform and it is the wrong one.
    * DUPLICATES ARE REFUSED rather than deduped. A list with a repeat in it is
      a list the caller did not mean; deduping it would apply to fewer
      employers than the count they confirmed, which is the same class of
      silent-arithmetic bug as truncating an over-long list.
    * A NON-ID IS REFUSED rather than skipped. Ids are the long numeric strings
      the queue publishes; anything else is compared as text and would simply
      fail to resolve later, one step further from the mistake.
    """
    if opportunity_ids is None or isinstance(opportunity_ids, (str, bytes, dict)):
        raise NothingToDo(
            "opportunity_ids must be a LIST of opportunity ids from "
            "instahyre_list_opportunities, not %s. This tool never assembles the list "
            "itself." % type(opportunity_ids).__name__
        )

    cleaned: list = []
    for raw in opportunity_ids:
        if isinstance(raw, bool) or not isinstance(raw, (str, int)):
            raise NothingToDo(
                "opportunity_ids contains %r, which is not an opportunity id. Ids are "
                "the long numeric strings instahyre_list_opportunities returns."
                % (raw,)
            )
        text = str(raw).strip()
        if not text:
            raise NothingToDo(
                "opportunity_ids contains a blank entry. It is refused rather than "
                "skipped: skipping it would change how many applications are sent "
                "without changing the count anybody agreed to."
            )
        cleaned.append(text)

    if not cleaned:
        raise NothingToDo(
            "Refusing a bulk apply with an empty list. An empty selection is NOT "
            "'apply to everything' here -- Instahyre's own modal pre-selects the whole "
            "queue, so that reading exists on this platform and it is the dangerous "
            "one. Pass the explicit ids to apply to."
        )

    duplicated = sorted({item for item in cleaned if cleaned.count(item) > 1})
    if duplicated:
        raise NothingToDo(
            "opportunity_ids contains duplicate id(s): %s. Refused rather than "
            "deduplicated -- silently shrinking the list would send fewer "
            "applications than the expected_count that was confirmed."
            % ", ".join(duplicated),
            duplicated=duplicated,
        )
    return cleaned


def _build_bulk_apply_request(records: list) -> tuple[str, dict]:
    """The exact (path, body) the frontend would send for these opportunities.

    ONE BODY KEY, AND THE SAME FLAG PICKS IT AND THE URL. Instahyre's builder,
    verbatim::

        service.apply_bulk=function(scope,selectedOpps){
          const data={};
          if(isESOppsEnabled(scope)){data.job_ids=selectedOpps.map((o)=>o.job_id);}
          else{data.opp_ids=selectedOpps.map((o)=>o.id);}
          return getService(scope).apply_bulk({},data);};

    ``isESOppsEnabled`` is ``enableCandidateESOpps``, the SAME flag that
    switches the single-apply ``$resource`` service -- so this reads
    :data:`constants.APPLY_BRANCH_ES` and does not introduce a second branch
    mechanism. Two flags that could disagree is exactly how this package once
    paired the ES body with the legacy URL, a combination the frontend never
    produces.

    THE VALUES ARE COPIED, NOT CONVERTED, which is the same rule the
    single-apply builder follows and the same one the job-search profile
    follows: what the server returned goes back exactly as it was returned. On
    the ES branch that yields the ordinary integer job ids. On the legacy
    branch it yields whatever the queue published for ``id``, which on this
    account is a numeric STRING -- and the site does not convert it either, it
    maps ``o.id`` straight off the object. Coercing would be inventing a step
    the browser does not take.
    """
    if C.APPLY_BRANCH_ES:
        path = C.EP_APPLY_BULK_ES
        values = []
        for raw in records:
            job_id = (raw.get("job") or {}).get("id")
            if job_id is None:
                raise NothingToDo(
                    "Opportunity %r has no job.id, which the ES bulk body is built "
                    "from. Refusing to guess an id inside an irreversible bulk apply, "
                    "where a wrong entry is one application to the wrong employer and "
                    "nothing would name it again." % (raw.get("id"),)
                )
            values.append(job_id)
        return path, {C.BULK_APPLY_BODY_KEY_ES: values}

    path = C.EP_APPLY_BULK_LEGACY
    values = []
    for raw in records:
        opp_id = raw.get("id")
        if opp_id is None:
            raise NothingToDo(
                "A queue record carries no id, so the legacy bulk body cannot name it. "
                "Refusing to send a bulk apply with a hole in it."
            )
        values.append(opp_id)
    return path, {C.BULK_APPLY_BODY_KEY_LEGACY: values}


def _normalise_addresses(emails: Optional[list]) -> tuple[list, list]:
    """Clean, dedupe and shape-check. Returns ``(accepted, rejected)``.

    Spaces are stripped from ANYWHERE in the address, not just the ends,
    because that is what Instahyre's ``constructInvitationsDict`` does
    (``item.replace(/ /g,'')``) -- matching it means the address this tool
    previews is the address the platform would act on.

    Duplicates are removed BEFORE the count and before the preview: inviting
    the same person twice is a reputational cost, not a rounding error, and a
    list that showed the duplicate would be an honest preview of a bad request
    rather than a good one.
    """
    accepted: list = []
    rejected: list = []
    seen = set()
    for raw in emails or []:
        if not isinstance(raw, str):
            rejected.append(raw)
            continue
        address = raw.replace(" ", "")
        if not address:
            continue
        if not _ADDRESS_SHAPE.match(address):
            rejected.append(raw)
            continue
        key = address.lower()
        if key in seen:
            continue
        seen.add(key)
        accepted.append(address)
    return accepted, rejected
