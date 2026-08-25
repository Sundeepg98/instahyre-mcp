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
        "captured": "2026-08-23",
    }


class Writer:
    """The eight captured write surfaces, each behind a confirm gate."""

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
