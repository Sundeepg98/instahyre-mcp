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

WHAT IS NOT HERE. No profile-image upload: its contract is captured but
reproducing the browser's body needs a WebP encoder at width<=800 that this
package has no dependency for, and the CREATE branch was never exercised. No
questionnaire answers and no workex PUT: those two are still in
``UNVERIFIED_WRITE_SURFACES`` and the register says why.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional
from urllib.parse import parse_qsl

from . import constants as C
from .errors import InstahyreError

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
    """Allow exactly one POST target on the inbox resource. Returns the path.

    An ALLOWLIST, not a blocklist, and the difference is the whole point of the
    carve-out. A blocklist that had been narrowed to let ``send_message``
    through would also let through anything nobody thought to add to it -- and
    the mark-all-read trap on this very resource is the proof that this API has
    mutating routes which do not look like writes (it is a **GET**).

    So this asks the opposite question: is this THE one path this package may
    POST to? Star, toggle-read, mark-all-read and bulk apply are refused here
    not because they are listed but because they are not the single allowed
    value, which is a property that survives someone adding a new action to the
    API tomorrow.
    """
    if path not in C.SENDABLE_INBOX_PATHS:
        raise NotSendable(
            "Refusing to POST to %r. This server has exactly ONE sendable inbox path "
            "(%s) and this is not it. Replying is the only inbox write that was built; "
            "starring, marking read and bulk mark-read remain unreachable by design, "
            "not by omission." % (path, C.EP_SEND_MESSAGE),
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
    """The five captured write surfaces, each behind a confirm gate."""

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

        THE ONLY INBOX WRITE THIS SERVER HAS, and it is reached through an
        allowlist of exactly one path rather than through a hole in the
        read-only guard. Starring, marking read and bulk mark-read are not
        merely still refused -- there is no branch here that could construct
        them. :data:`constants.MUTATING_PATH_MARKERS` is unchanged and the read
        tier still refuses all five markers, ``send_message`` included.

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
        # The doorway. Everything that reaches the wire from this module goes
        # through it, and it is the ONLY place a POST target is decided -- which
        # is what makes "reply and nothing else" a structural property rather
        # than a promise. It is not self-certifying: the allowlist is pinned to
        # a LITERAL path in tests/test_writes.py, so editing EP_SEND_MESSAGE
        # fails there rather than sliding through here.
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
