"""What changed in the inbound queue since a human last looked.

WHY THIS IS THE TOOL THIS PLATFORM NEEDED
-----------------------------------------
Instahyre is a reverse marketplace: employers put a candidate into a curated
queue and recruiters open his resume. Nothing about that is initiated by him, so
the whole value of the account is in noticing that something arrived. As of
2026-08-22 the queue held **227 pending opportunities** and had last recalculated
nine days earlier, and nothing in this server could answer "what is different
since yesterday". Listing 227 items again is not an answer to that question.

THERE IS NO DATE TO DIFF ON. This is the constraint that shapes everything here:

* The opportunity queue publishes no posting date and no arrival date. This
  server's own ``jobs.first_seen`` exists precisely because the API has no such
  field to read.
* The recruiter-activity feed publishes ``action_date`` as **pre-formatted human
  prose** -- "13 hours ago" for recent events, "Aug 17 at 3:47 PM" for older
  ones. The format CHANGES AS TIME PASSES: today's "13 hours ago" is tomorrow's
  "Aug 22 at ...". Diffing on that string would report every recent event as new
  again the moment it aged across the boundary, which is worse than reporting
  nothing.

So novelty is decided by IDENTITY against a stored set, never by timestamp. See
:func:`activity_identity` for the harder half of that.

WHAT THIS IS NOT
----------------
**This is not a scheduler and it does not run unattended.** There is no thread,
no timer, no loop; every function here runs only when a caller invokes it, and
returns. That is a deliberate design decision and not an oversight:

    An Instahyre application CANNOT BE WITHDRAWN. Their UI has no confirmation
    dialog -- every modal fires after the POST is already accepted -- so this
    server's confirm gate is the only brake that exists. A background loop in
    this package would be a place for that authority to accumulate, and
    ``tests/test_scoring_policy.py`` holds a behavioural invariant asserting
    that no such place exists.

The cost of that decision is real and worth stating plainly rather than hiding:
this notices new inbound the next time somebody asks, not while he is away. If
unattended notification is wanted it is a separate decision about a separate
risk, and it belongs to the operator, not to this module.

THE BASELINE RULE
-----------------
The first read of a stream records what is there and reports **zero new**, with
``baseline_established`` set. It does not report 227 items as news. A watcher
whose first answer is the entire backlog teaches its reader to ignore it, and
the reader is then also ignoring the second answer, which was the real one.
"""

from __future__ import annotations

import hashlib
from typing import Any, Optional

from .cache import Store
from .errors import InvalidFilter

#: The streams this module can watch. Values are what a caller passes; the key
#: is what is written to ``watch_seen.stream``, so renaming one here would
#: silently reset that stream's memory -- hence they are pinned in one place.
STREAMS = ("opportunities", "activity")

#: How many records to pull per stream when looking for news. The queue arrives
#: ranked, so the head is where an arrival lands; the API caps a page anyway.
DEFAULT_LIMIT = 50


def opportunity_identity(record: dict) -> Optional[str]:
    """The stable id of one queue record, or None if it has none.

    Instahyre supplies this one: ``id`` on a queue record is the opportunity id,
    a long numeric STRING, and it is stable across reads. It is deliberately NOT
    ``job.id`` -- the same job can be offered through more than one opportunity,
    and keying on the job id would silently swallow the second offer.

    Returns None rather than inventing a key for a record with no id. A record
    that cannot be identified is reported as unidentifiable further up instead
    of being counted as new on every single call, which is what a hash-of-
    everything fallback would do.
    """
    value = record.get("id")
    if value is None or value == "":
        return None
    return str(value)


def activity_identity(event: dict) -> Optional[str]:
    """A derived id for one recruiter-activity event.

    THE FEED SUPPLIES NO PER-EVENT ID. Measured 2026-08-23 against the captured
    fixture: ``resource_uri`` is present on every row and is the COLLECTION uri,
    byte-identical across all of them (1 unique value over 3 rows), so it
    identifies the endpoint and not the event. There is no other candidate key.

    So the identity is built from the fields that describe WHO acted on WHAT:
    recruiter id, recruiting company, job title, hiring company. Hashed rather
    than concatenated so that a title containing the separator cannot collide
    with a different pair.

    ``when`` IS DELIBERATELY EXCLUDED, and this is the load-bearing decision in
    the module. ``action_date`` is human prose whose spelling changes with the
    clock -- "13 hours ago" becomes "Aug 22 at 3:47 PM" tomorrow -- so including
    it would make every recent event re-fire as new once it aged, on every
    account, forever.

    THE COST, STATED: a second view by the same recruiter on the same role does
    not register as news. That is a genuine loss of signal and it is the right
    trade -- the question this feed is read for is "who has engaged that had
    not before", and a watcher that cried new on every clock tick would answer
    nothing at all. If repeat views ever matter, they need a real event id from
    the API, which does not currently exist.
    """
    parts = [
        event.get("recruiter_id"),
        event.get("recruiter_company"),
        event.get("job_title"),
        event.get("hiring_company"),
    ]
    if all(p in (None, "") for p in parts):
        return None
    blob = "\x1f".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


class InboundWatch:
    """The differ. Holds no state of its own -- everything is in the store."""

    def __init__(self, inbound: Any, store: Store) -> None:
        self.inbound = inbound
        self.store = store

    # -- reading a stream --------------------------------------------------

    def _read_stream(self, stream: str, limit: int) -> tuple[list, list, dict]:
        """Fetch one stream. Returns ``(records, identities, context)``.

        ``context`` carries whatever the diagnosis will need to explain a zero,
        gathered here because it is free at read time and would cost a second
        request later.

        Errors are NOT caught. ``AuthRequired`` in particular must reach the
        caller -- a dead session that came back as an empty diff would report
        "nothing new" about a queue this server could not see, which is the one
        failure mode this whole package is built to refuse.

        ``use_cache=False`` IS LOAD-BEARING, not a preference. The queue read is
        cached for ``C.TTL_OPPORTUNITIES`` (five minutes), which is right for a
        human paging through a list and fatal for a differ: two calls inside the
        window would compare a payload against ITSELF and report "nothing new"
        with total confidence, whatever had arrived. A watcher that reads its
        own cache is a check that cannot fail. ``TestTheWatchDoesNotReadItsOwnCache``
        holds the control.

        The activity feed is not cached at all -- ``Inbound.activity`` goes
        straight to ``http.get`` -- so there is nothing to bypass there, and no
        flag is passed rather than one that would silently become a no-op.
        """
        if stream == "opportunities":
            payload = self.inbound.list_opportunities(
                interest="pending", limit=limit, use_cache=False
            )
            records = payload.get("opportunities") or []
            identities = [opportunity_identity(r) for r in records]
            context = {
                "total": payload.get("total"),
                "count_returned": payload.get("count_returned", len(records)),
                "source_diagnosis": payload.get("diagnosis"),
            }
        elif stream == "activity":
            payload = self.inbound.activity(kind="viewed", limit=limit)
            records = payload.get("events") or []
            identities = [activity_identity(r) for r in records]
            context = {
                "total": payload.get("total"),
                "count_returned": payload.get("count_returned", len(records)),
                "source_diagnosis": None,
            }
        else:
            raise InvalidFilter(
                "stream must be one of %s, not %r." % (sorted(STREAMS), stream),
                field="stream",
            )
        return records, identities, context

    def whats_new(
        self,
        stream: str = "opportunities",
        *,
        limit: int = DEFAULT_LIMIT,
        advance: bool = True,
    ) -> dict:
        """What appeared in ``stream`` since this stream was last advanced.

        Args:
            stream: "opportunities" (the curated pending queue) or "activity"
                (recruiters who opened the resume).
            limit: how many records to pull. The queue is ranked, so news lands
                near the head.
            advance: mark what was returned as seen. Defaults True, because
                that is what "what is new" means and because NOTHING IS
                DESTROYED by it -- the bookmark moves, the opportunities stay
                fully readable through ``instahyre_list_opportunities``. Pass
                False to look without consuming.

        Returns a dict that always carries ``new`` (a list, possibly empty),
        ``new_count``, and -- on any zero -- a ``diagnosis`` naming WHICH
        silence this is. Never a bare empty list.
        """
        if stream not in STREAMS:
            raise InvalidFilter(
                "stream must be one of %s, not %r." % (sorted(STREAMS), stream),
                field="stream",
            )
        baselined_at = self.store.watch_baselined(stream)
        first_ever = baselined_at is None

        records, identities, context = self._read_stream(stream, limit)

        # A record with no usable identity is REPORTED, never silently dropped
        # and never counted as new. Counting it as new would make it news again
        # on every call; dropping it quietly would hide a contract change in the
        # one field this module depends on.
        unidentifiable = sum(1 for i in identities if i is None)
        usable = [i for i in identities if i is not None]
        by_identity = {
            identity: record
            for identity, record in zip(identities, records)
            if identity is not None
        }

        unseen = self.store.watch_unseen(stream, usable)

        out: dict[str, Any] = {
            "stream": stream,
            "checked_at_records": len(records),
            "total_in_stream": context.get("total"),
            "baseline_established": first_ever,
            "advanced": False,
            "new": [],
            "new_count": 0,
        }
        if unidentifiable:
            out["unidentifiable_records"] = unidentifiable
            out["unidentifiable_note"] = (
                "%d record(s) carried no field this module can identify them by, so "
                "they are neither reported as new nor remembered. That is a change in "
                "the API's shape, not an empty result -- see inbound_watch."
                % unidentifiable
            )

        if first_ever:
            # THE BASELINE READ. Everything present is recorded and NOTHING is
            # reported as news; see the module docstring.
            recorded = self.store.watch_record(stream, usable)
            self.store.watch_touch(stream, 0)
            out["advanced"] = True
            out["baseline_size"] = recorded
            out["diagnosis"] = {
                "reason": "baseline_established",
                "explanation": (
                    "This is the first look at the '%s' stream, so there was nothing to "
                    "compare against. %d record(s) are now the baseline and were NOT "
                    "reported as new -- a first answer of '%d new' would be the backlog, "
                    "not news. The next call reports real changes."
                    % (stream, recorded, recorded)
                ),
            }
            return out

        out["new"] = [by_identity[i] for i in unseen]
        out["new_count"] = len(unseen)

        if advance and unseen:
            self.store.watch_record(stream, unseen)
            out["advanced"] = True
        self.store.watch_touch(stream, len(unseen))

        if not unseen:
            out["diagnosis"] = self._diagnose_nothing_new(stream, records, context)
        return out

    def _diagnose_nothing_new(self, stream: str, records: list, context: dict) -> dict:
        """Say WHICH silence a zero is. Never shrug.

        Four genuinely different situations produce ``new_count == 0`` and they
        want four different reactions from a reader:

        * the stream was READ and everything in it had been seen before -- the
          ordinary quiet week, and real information;
        * the stream itself came back EMPTY because filters or the account state
          emptied it, which the underlying tool already diagnosed and whose
          diagnosis is carried through here rather than restated;
        * the stream came back empty because no employer has ever engaged;
        * the session is dead -- which cannot reach here at all, because a 401
          raises ``AuthRequired`` inside the HTTP client and this method is only
          called on a successful read. That is stated rather than checked
          precisely so a future edit that starts swallowing the exception has to
          delete this sentence to do it.
        """
        if records:
            return {
                "reason": "all_already_seen",
                "explanation": (
                    "%d record(s) are in the '%s' stream and every one had already been "
                    "reported. Nothing arrived since the last look. This is a real zero: "
                    "the stream was read successfully." % (len(records), stream)
                ),
            }
        carried = context.get("source_diagnosis")
        if carried:
            return {
                "reason": "source_stream_empty",
                "explanation": (
                    "The '%s' stream itself came back empty, so there was nothing to "
                    "compare. The reason belongs to the underlying read and is carried "
                    "through unchanged rather than restated." % stream
                ),
                "source_diagnosis": carried,
            }
        return {
            "reason": "stream_empty_no_reason_given",
            "explanation": (
                "The '%s' stream returned no records and the underlying read offered no "
                "diagnosis for that. Treat this as unexplained rather than as an "
                "absence of interest -- it means this module could not establish which "
                "silence it is looking at." % stream
            ),
        }

    # -- introspection -----------------------------------------------------

    def status(self, stream: Optional[str] = None) -> dict:
        """What the watch remembers, per stream. Makes no request.

        Reads only local bookkeeping on purpose: "when did I last look" must be
        answerable while the session is dead, which is exactly when someone asks
        it.
        """
        if stream is not None and stream not in STREAMS:
            raise InvalidFilter(
                "stream must be one of %s, not %r." % (sorted(STREAMS), stream),
                field="stream",
            )
        wanted = [stream] if stream else list(STREAMS)
        streams = {}
        for name in wanted:
            stats = self.store.watch_stats(name)
            stats["watched"] = stats["baselined_at"] is not None
            streams[name] = stats
        return {
            "streams": streams,
            "unattended": False,
            "how_it_runs": (
                "This watch runs only when called. There is no background poll and no "
                "timer in this package -- an Instahyre application cannot be withdrawn, "
                "so nothing here is allowed to act while nobody is watching."
            ),
        }

    def forget(self, stream: str) -> dict:
        """Drop one stream's memory, so the next read re-baselines.

        Re-baselines rather than floods: the next call records what is there and
        reports zero, which is what makes this safe to offer.
        """
        if stream not in STREAMS:
            raise InvalidFilter(
                "stream must be one of %s, not %r." % (sorted(STREAMS), stream),
                field="stream",
            )
        removed = self.store.watch_forget(stream)
        return {
            "stream": stream,
            "forgotten": removed,
            "next_read": (
                "The next look at '%s' establishes a fresh baseline and reports zero "
                "new. It will not report the backlog as news." % stream
            ),
        }
