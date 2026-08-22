"""The inbox: conversation threads and message bodies. Read-only, no browser.

This module exists because a previous build reported message bodies as "not
possible". They are possible. What was actually true is narrower and much less
interesting: *the conversation list is not in the namespace we looked in*. The
message resource lives under ``/resume_modal/emails/``, so its sibling
conversation resource was assumed to live there too; both spellings 404, and
two misses became a platform limitation.

The list is at ``/inbox_page/candidate_conversation``. It was found by opening
the inbox in the authenticated browser and recording what the page fetched --
the page cannot render without it, so it had to exist. That is the honest role
the browser played here: it answered a question the API could not be *asked*.
It is not in the data path. ``/inbox_page/*`` is an ``/api/v1/*`` route like
any other, Cloudflare-exempt, and every function below is plain httpx.

**Nothing here mutates, and that is enforced rather than intended.** Four inbox
endpoints do mutate, and one of them is genuinely nasty: ``mark_all_read`` is a
**GET** that bulk-clears unread state, sitting on the same resource prefix as
the list call. Every request this module makes goes through
:func:`guard_read_only` first, which refuses any path naming a mutating action.

The message endpoint has **no not-found signal**. A ``conv_id`` that is not his
answers 200 with ``{"objects": [], "meta": {...}}`` -- captured live against two
foreign ids on 2026-08-22 -- which is key for key what a real thread with
nothing said in it would look like. Passing that through as a successful read
of nothing is the failure ``errors.py`` forbids, so
:meth:`Inbox.read_conversation` resolves the ambiguity against his own
conversation list, exactly as ``Inbound.find_opportunity`` resolves an
opportunity id against his queue, and raises when the id is not there.

The one thing this module cannot promise: whether *fetching* a thread marks it
read on the server. The frontend sends no mark-read request -- it decrements
the badge locally and optimistically -- which is only coherent if the server
does it on the message GET. That is a strong inference and it is not a
measurement, so :meth:`Inbox.read_conversation` says so in its docstring rather
than quietly hoping.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from . import constants as C
from . import shape
from .cache import Store
from .errors import ApiError, InstahyreError, InvalidFilter, NotFound
from .http import InstahyreHTTP

log = logging.getLogger("instahyre.inbox")

#: How much of a message body to return before truncating. Bodies are recruiter
#: emails -- often signature-heavy -- and the whole point of this server over a
#: browser is that reading should not cost thousands of tokens.
DEFAULT_BODY_CHARS = 1500


class MutatingPathRefused(InstahyreError):
    kind = "mutating_path_refused"


def _as_int(value: Any, field: str) -> int:
    """Typed conversion. Every other bad input on this module raises
    ``InvalidFilter`` with a field name; a bare ``int()`` here let ``limit`` and
    ``offset`` escape as an untyped ValueError that the MCP error mapper cannot
    classify."""
    try:
        return int(value)
    except (TypeError, ValueError):
        raise InvalidFilter(
            f"{field} must be a whole number, got {value!r}.", field=field
        ) from None


def guard_read_only(path: str) -> str:
    """Refuse any inbox path that names a mutating action. Returns the path.

    A LIVE guard: it inspects the string actually about to be requested, so it
    fires on a constant that gets edited, a caller that hand-builds a path, and
    a suffix smuggled in behind a query string. It is a substring test on
    purpose -- ``mark_all_read`` must not become reachable by appending a
    trailing slash to it.

    The guard exists because of a specific hazard rather than as a gesture:
    ``GET /inbox_page/candidate_conversation/mark_all_read`` shares its prefix
    with the list endpoint and wipes his unread flags with no body and no
    confirmation. Any "walk the resource and see what is under it" probe would
    trip it.
    """
    lowered = path.lower()
    for marker in C.MUTATING_PATH_MARKERS:
        if marker in lowered:
            raise MutatingPathRefused(
                f"Refusing to request {path}: it names '{marker}', which mutates. This "
                "server's inbox tier is read-only -- it never sends, replies, stars, "
                "marks read, or bulk-applies.",
                marker=marker,
            )
    return path


class Inbox:
    """Read-only reader for the candidate inbox."""

    def __init__(self, http: InstahyreHTTP, store: Store, client: Any = None) -> None:
        self.http = http
        self.store = store
        self.client = client

    # -- conversations -----------------------------------------------------

    def list_conversations(
        self,
        *,
        status: Optional[str] = None,
        unread_only: bool = False,
        starred_only: bool = False,
        query: Optional[str] = None,
        limit: int = C.CONV_DEFAULT_LIMIT,
        offset: int = 0,
        include_job: bool = True,
    ) -> dict:
        """The conversation list, shaped, with company and role joined in.

        A conversation record carries no company, recruiter or subject -- that
        was verified by enumerating every property the site's own code reads off
        one. The site joins the job in from a separate endpoint, so this does
        the same when ``include_job`` is set, at one cached request per distinct
        job.
        """
        params: dict[str, Any] = {
            "limit": max(1, _as_int(limit, "limit")),
            "offset": max(0, _as_int(offset, "offset")),
        }

        if status is not None:
            key = str(status).strip().lower().replace(" ", "_").replace("-", "_")
            if key not in C.CONV_STATUS:
                raise InvalidFilter(
                    f"Unknown conversation status {status!r}. Use one of: "
                    + ", ".join(sorted(C.CONV_STATUS))
                    + ". Omit it entirely for all conversations -- the site sends no "
                    "status key at all for 'All', and a status=0 has never been seen "
                    "by their server.",
                    field="status",
                )
            params["status"] = C.CONV_STATUS[key]

        if unread_only and starred_only:
            raise InvalidFilter(
                "unread_only and starred_only are mutually exclusive in the site's own "
                "UI; asking for both is a filter combination nobody has tested.",
                field="unread_only",
            )
        # Only ever the literal true. The frontend omits these keys rather than
        # sending false, so a false here would be an untested wire value.
        if unread_only:
            params["unread"] = True
        if starred_only:
            params["starred"] = True
        if query:
            params["query"] = query

        payload = self.http.get(guard_read_only(C.EP_CONVERSATIONS), params=params)
        if not isinstance(payload, dict) or "objects" not in payload:
            raise ApiError(
                "The conversation list came back without an 'objects' key; its contract "
                "has changed.",
                path=C.EP_CONVERSATIONS,
            )

        objects = payload.get("objects") or []
        records = [shape_conversation(obj) for obj in objects]
        if include_job:
            self._attach_jobs(records)

        counts = self._counts_quietly()
        out: dict[str, Any] = {
            "conversations": records,
            "count": len(records),
            "offset": params["offset"],
            "limit": params["limit"],
            "filters_applied": {
                k: v for k, v in params.items() if k not in ("limit", "offset")
            }
            or None,
            "unread_total": counts.get("unread"),
            "starred_total": counts.get("starred"),
        }
        if not records:
            out["diagnosis"] = self._diagnose_empty(params, counts)
        return out

    def _attach_jobs(self, records: list[dict]) -> None:
        """Join company and role in from the public job endpoint.

        Best-effort by design: a job that has been pulled down should cost the
        conversation its company name, not the whole listing.
        """
        if self.client is None:
            return
        for record in records:
            job_id = record.get("job_id")
            if not job_id:
                continue
            try:
                job = self.client.get_job(int(job_id), description_chars=0)
            except InstahyreError as exc:
                record["company"] = None
                record["job_lookup_error"] = exc.kind
                log.info("job %s could not be joined onto a conversation: %s", job_id, exc.kind)
                continue
            record["company"] = job.get("company")
            record["title"] = job.get("title")
            record["locations"] = job.get("locations")

    def _counts_quietly(self) -> dict:
        """Unread/starred totals. A failure here must not fail the listing."""
        try:
            return self.conversation_counts()
        except InstahyreError as exc:
            log.info("conversation counts unavailable: %s", exc.kind)
            return {}

    def _diagnose_empty(self, params: dict, counts: dict) -> str:
        """Say WHY the list is empty. An empty list is never left bare.

        The distinction that matters to a caller: filters hid everything, the
        page ran off the end, or the inbox is genuinely bare. A session problem
        cannot reach here -- a dead session raises AuthRequired upstream -- so
        an empty result from this method is a real measurement of his inbox.
        """
        filters = {k: v for k, v in params.items() if k not in ("limit", "offset")}
        if filters:
            return (
                "No conversations matched these filters. Filters applied: "
                + ", ".join(f"{k}={v}" for k, v in sorted(filters.items()))
                + ". Drop them to see the whole inbox."
            )
        if params.get("offset"):
            return (
                f"Nothing at offset {params['offset']} -- you have paged past the end of "
                "the inbox."
            )
        unread = counts.get("unread")
        if unread:
            return (
                f"The list is empty but the server reports {unread} unread. That is a "
                "contradiction worth re-checking rather than trusting."
            )
        return (
            "His Instahyre inbox is genuinely empty -- no conversations at all, not a "
            "filter artefact and not an expired session (the request was authenticated "
            "and answered 200). On a reverse marketplace this means no employer has "
            "opened a thread yet; inbound interest shows up in "
            "instahyre_list_opportunities and instahyre_recruiter_activity first, and "
            "messages only follow later."
        )

    def conversation_counts(self) -> dict:
        """Unread / starred / starred-unread totals for the inbox."""
        payload = self.http.get(guard_read_only(C.EP_CONVERSATION_COUNT), params={"limit": 1, "offset": 0})
        if not isinstance(payload, dict) or "conv_count" not in payload:
            raise ApiError(
                "The conversation count endpoint answered without a 'conv_count' key.",
                path=C.EP_CONVERSATION_COUNT,
            )
        counts = payload.get("conv_count") or {}
        return {
            "unread": counts.get("unread"),
            "starred": counts.get("starred"),
            "starred_unread": counts.get("starred_unread"),
        }

    # -- one thread --------------------------------------------------------

    def read_conversation(
        self,
        conv_id: int,
        *,
        body_chars: Optional[int] = DEFAULT_BODY_CHARS,
        include_gated: bool = False,
    ) -> dict:
        """Every message in one thread, oldest-first, as text.

        ``show_message`` is a gate the site applies with a ``break``, not a
        ``continue`` -- it stops at the first falsy one and discards it and
        everything after. This mirrors that by default and reports how many were
        withheld, so the count is never silently short.

        An envelope with no records at all is never returned bare, because the
        endpoint has no not-found signal of its own: the id is cross-checked
        against his conversation list and raises :class:`NotFound` when it is
        not there, or comes back carrying a ``diagnosis`` saying which silence
        it is. That check costs one extra request and runs ONLY on that answer
        -- a thread with records in it has already proved it exists.
        """
        try:
            conv_id_int = int(conv_id)
        except (TypeError, ValueError):
            raise InvalidFilter(
                f"conv_id must be the integer 'id' from instahyre_list_conversations, "
                f"not {conv_id!r}.",
                field="conv_id",
            ) from None

        payload = self.http.get(guard_read_only(C.EP_MESSAGES), params={"conv_id": conv_id_int})
        if not isinstance(payload, dict) or "objects" not in payload:
            raise ApiError(
                "The message endpoint answered without an 'objects' key; its contract "
                "has changed.",
                path=C.EP_MESSAGES,
            )

        # unsent_messages are prepended by the site itself; the site's own code
        # concatenates them unguarded, so the key is always present.
        raw = list(payload.get("unsent_messages") or []) + list(payload.get("objects") or [])
        messages: list[dict] = []
        gated = 0
        for index, obj in enumerate(raw):
            if not isinstance(obj, dict):
                continue
            if not obj.get("show_message", True) and not include_gated:
                # The site stops here and discards the whole tail, so the count
                # of what was withheld is everything from here on -- not one.
                # Counting one and breaking made this number always 0 or 1 while
                # the docstring promised the count was never short, which is a
                # worse failure than not reporting it at all.
                gated = len(raw) - index
                break
            if not obj.get("show_message", True):
                gated += 1
            messages.append(shape_message(obj, body_chars=body_chars))

        # Gate on ``raw``, not on ``messages``: a thread whose records were all
        # withheld by show_message also lands on count 0, but it demonstrably
        # exists, and cross-checking it would buy a request to confirm what
        # withheld_by_show_message already says.
        diagnosis = self._diagnose_empty_thread(conv_id_int, payload) if not raw else None

        out: dict[str, Any] = {
            "conv_id": conv_id_int,
            "messages": messages,
            "count": len(messages),
            "withheld_by_show_message": gated,
            "starred": payload.get("starred"),
            "recipients": payload.get("recipients"),
            "read_side_effect": (
                "UNVERIFIED. This request sends nothing and asks for nothing to change, "
                "but Instahyre's own page decrements its unread badge locally without "
                "ever calling a mark-read endpoint -- which only makes sense if the "
                "server marks a thread read when its messages are fetched. Treat "
                "reading a thread as POSSIBLY marking it read. It has not been possible "
                "to test: the inbox has no conversations to test on."
            ),
        }
        # Present exactly when the thread came back with nothing in it, which is
        # the same rule list_conversations follows for its own diagnosis.
        if diagnosis:
            out["diagnosis"] = diagnosis
        return out

    def _diagnose_empty_thread(self, conv_id: int, payload: dict) -> str:
        """Say WHICH silence an empty thread is, or raise :class:`NotFound`.

        The endpoint answers 200 for an id that is not his, so the verdict has
        to come from somewhere else: his own conversation list, which is where
        a ``conv_id`` comes from in the first place. That makes "not his" a
        measurement rather than an inference off the envelope. Two other
        outcomes are possible and both return rather than raise -- the id IS
        his and the thread really is empty, or the cross-check could not be
        completed, in which case both readings ship and neither is chosen.
        """
        frame = self._frame_note(payload)
        undecided: Optional[str] = None
        try:
            known = self._conversation_ids()
        except InstahyreError as exc:
            log.info("conversation id cross-check unavailable: %s", exc.kind)
            known, undecided = None, "the list request failed with %s" % exc.kind
        else:
            if known is None:
                undecided = "his inbox runs past the %d conversations this check walks" % (
                    C.CONV_ID_CHECK_PAGE * C.CONV_ID_CHECK_MAX_PAGES
                )

        if undecided is not None:
            return (
                "Conversation %d came back with no messages, and which silence that is could "
                "not be established: the cross-check against instahyre_list_conversations "
                "could not be completed -- %s. Both readings stay open -- a real thread with "
                "nothing said in it, or a conv_id that is not his -- because the message "
                "endpoint cannot tell them apart on its own: a foreign id answers 200 with an "
                "empty objects list. What is known is that %s." % (conv_id, undecided, frame)
            )

        if str(conv_id) in known:
            return (
                "Conversation %d is his -- instahyre_list_conversations lists it -- so this is "
                "a real thread with no messages in it, not a bad id. Worth noting anyway: %s."
                % (conv_id, frame)
            )

        raise NotFound(
            "No conversation %d in this account's inbox. It is not in "
            "instahyre_list_conversations, which is the only place a conv_id comes from, and "
            "that list was walked in full (%d conversation(s)). The message endpoint cannot "
            "report this itself -- an id that is not his answers 200 with an empty objects "
            "list, which is why this used to come back as a successful read of nothing. "
            "Corroborating: %s." % (conv_id, len(known), frame),
            conv_id=conv_id,
            checked_against="instahyre_list_conversations",
            conversations_in_inbox=len(known),
        )

    def _frame_note(self, payload: dict) -> str:
        """What the envelope itself says about whether a thread was there."""
        present = [key for key in C.MSG_THREAD_FRAME if key in payload]
        if present:
            return (
                "the envelope did carry a thread frame (" + ", ".join(present) + "), which "
                "points the other way"
            )
        return (
            "the envelope carried no thread frame either -- no "
            + ", ".join(C.MSG_THREAD_FRAME)
            + ", absent rather than null, which is what the live not-found capture looks like"
        )

    def _conversation_ids(self) -> Optional[set]:
        """Every conversation id in his inbox, as strings. ``None`` = did not finish.

        Unfiltered on purpose: a status or unread filter would hide a thread
        that IS his and turn the cross-check into a false not-found, which is a
        worse bug than the one it exists to fix. Ids are compared as STRINGS
        because this API is not consistent about the type -- an opportunity id
        arrives quoted, a conversation id bare -- and find_opportunity settled
        that question the same way.

        ``None`` means the walk hit its page cap, never "not in the inbox".
        """
        ids: set = set()
        offset = 0
        for _ in range(C.CONV_ID_CHECK_MAX_PAGES):
            payload = self.http.get(
                guard_read_only(C.EP_CONVERSATIONS),
                params={"limit": C.CONV_ID_CHECK_PAGE, "offset": offset},
            )
            if not isinstance(payload, dict) or "objects" not in payload:
                raise ApiError(
                    "The conversation list came back without an 'objects' key; its contract "
                    "has changed.",
                    path=C.EP_CONVERSATIONS,
                )
            objects = payload.get("objects") or []
            ids.update(
                str(obj["id"])
                for obj in objects
                if isinstance(obj, dict) and obj.get("id") is not None
            )
            # Two terminators, because neither covers the other: meta.next is
            # what tastypie itself says, and a short page ends the walk even if
            # that key ever stops being sent. Neither has been seen to fail --
            # his inbox is empty, so only the first page has ever been walked --
            # which is exactly why the cheap belt is worth having here.
            if not objects or not (payload.get("meta") or {}).get("next"):
                return ids
            offset += len(objects)
        return None


# ---------------------------------------------------------------------------
# Shaping. Field names come from the frontend's own property accesses, not from
# a guess at what an inbox record ought to look like.
# ---------------------------------------------------------------------------


def shape_conversation(obj: dict) -> dict:
    """One conversation record, compact.

    ``is_latest_msg_read`` is inverted into ``unread`` because every caller
    wants to know what needs attention, and a double negative at the call site
    is how off-by-one reading bugs happen.
    """
    return {
        "id": obj.get("id"),
        "job_id": obj.get("job_id"),
        "opportunity_id": obj.get("opportunity_id"),
        "unread": (not obj["is_latest_msg_read"]) if "is_latest_msg_read" in obj else None,
        "starred": obj.get("is_starred"),
        "preview": shape.strip_html(obj.get("latest_message")) or None,
        "last_message_at": obj.get("latest_msg_at"),
    }


def shape_message(obj: dict, *, body_chars: Optional[int] = DEFAULT_BODY_CHARS) -> dict:
    """One message, as text.

    The body is ``content_html`` -- there is no plain-text field -- so it is
    stripped to text here. The direction flag is ``is_owner`` (true means he
    wrote it), with ``is_automated_message`` as a distinct third category
    rather than a kind of sender: an InstaBot message is not a recruiter, and
    collapsing the two would overstate how much human interest exists.
    """
    body, truncated = shape.truncate(shape.strip_html(obj.get(C.MSG_BODY_FIELD)), body_chars)

    if obj.get(C.MSG_DIRECTION_FIELD):
        sender = "you"
    elif obj.get("is_automated_message"):
        sender = "Instahyre (automated)"
    else:
        sender = ((obj.get("from_user") or {}).get("first_name")) or "recruiter"

    record: dict[str, Any] = {
        "from": sender,
        "from_me": bool(obj.get(C.MSG_DIRECTION_FIELD)),
        "automated": bool(obj.get("is_automated_message")),
        "sent_at": obj.get("created_at_date_time"),
        "body": body,
    }
    if truncated:
        record["body_truncated"] = True
    if obj.get("scheduled_on") and not obj.get("sent_on"):
        record["scheduled_not_yet_sent"] = True
    return record
