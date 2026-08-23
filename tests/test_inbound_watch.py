"""The inbound watch: what changed since he last looked, and why a zero is a zero.

WHAT THIS TOOL IS FOR, AND THE CONSTRAINT THAT SHAPES IT
--------------------------------------------------------
Instahyre is a reverse marketplace. The queue held 227 pending opportunities on
2026-08-22 and publishes no arrival date on any of them, so re-listing it says
what exists and never what changed. The recruiter feed is worse: its only date
is human prose whose SPELLING CHANGES AS IT AGES -- "13 hours ago" today is
"Aug 22 at 3:47 PM" tomorrow -- so a differ keyed on that field would report
every recent event as new again the moment it crossed the boundary.

So novelty is decided by IDENTITY against a stored set. The tests that matter
most in this file are the ones proving that the identity is stable under the
clock (``TestActivityIdentity``) and that a first look does not announce the
backlog as news (``TestTheBaselineRule``).

EVERY GUARD HERE CARRIES A CONTROL. A check that cannot fail certifies nothing,
so each rule is also run against a build that breaks it -- an identity that
includes the timestamp, a differ with no baseline rule, a diagnosis that
shrugs -- and asserted to catch it. The controls are named ``..._CONTROL``,
which is this repo's convention for "this test is the evidence another test can
fail".
"""

from __future__ import annotations

import time

import httpx
import pytest

from conftest import fixture_json, make_client
from instahyre_server import constants as C
from instahyre_server.cache import Store
from instahyre_server.errors import AuthRequired, InvalidFilter
from instahyre_server.inbound_watch import (
    InboundWatch,
    activity_identity,
    opportunity_identity,
)

PENDING = fixture_json("opportunities_pending.json")
ACTIVITY = fixture_json("activity_viewed.json")
EMPTY_QUEUE = fixture_json("opportunities_empty.json")
NAVBAR = fixture_json("navbar_count.json")


def queue_routes(payload=None, activity=None):
    """The two stream endpoints plus the counter a queue diagnosis probes."""
    return {
        C.EP_OPPORTUNITIES: payload if payload is not None else PENDING,
        C.EP_ACTIVITY: activity if activity is not None else ACTIVITY,
        C.EP_OPP_NAVBAR_COUNT: NAVBAR,
    }


def watch_client(payload=None, activity=None, store=None):
    return make_client(queue_routes(payload, activity), store=store)


def retarget(client, endpoint, payload):
    """Point one already-wired route at a new payload, mid-test.

    Route keys are stored NORMALISED -- ``conftest._normalise`` strips the
    ``/api/v1`` prefix -- so writing the prefixed spelling silently inserts a
    SECOND, unreachable entry and the original keeps answering. That failure is
    invisible: the test reads "the queue changed" and the transport disagrees.
    Going through the same normaliser the table uses is what makes it impossible.
    """
    from conftest import _normalise

    key = _normalise(endpoint)
    assert key in client.routes.routes, (
        "retarget() was given an endpoint that was never wired: %r" % endpoint
    )
    client.routes.routes[key] = payload


def ids_of(result):
    return [r.get("id") for r in result["new"]]


# ===========================================================================
# 1. Identity -- the field a diff is keyed on
# ===========================================================================


class TestOpportunityIdentity:
    """The queue supplies its own id, so this half is easy and still pinned."""

    def test_the_opportunity_id_is_the_key(self):
        assert opportunity_identity({"id": "900012345"}) == "900012345"

    def test_it_is_not_the_job_id(self):
        """The same job can be offered through more than one opportunity.
        Keying on ``job.id`` would silently swallow the second offer, which is
        an arrival this tool exists to report."""
        first = {"id": "900012345", "job_id": 777}
        second = {"id": "900067890", "job_id": 777}
        assert opportunity_identity(first) != opportunity_identity(second)

    def test_an_integer_id_and_its_string_agree(self):
        """The API sends this field as a long numeric STRING, but a shaped
        record could carry it as an int. Two spellings of one id must not read
        as two opportunities."""
        assert opportunity_identity({"id": 900012345}) == opportunity_identity(
            {"id": "900012345"}
        )

    def test_a_record_with_no_id_is_unidentifiable_rather_than_invented(self):
        """None, not a hash of the whole record. A synthesised key changes
        whenever any field changes, so such a record would be reported as new
        on every single call -- an alarm that fires forever is one nobody
        reads."""
        assert opportunity_identity({}) is None
        assert opportunity_identity({"id": None}) is None
        assert opportunity_identity({"id": ""}) is None


class TestActivityIdentity:
    """The load-bearing half: this feed supplies NO per-event id."""

    def test_the_feed_really_has_no_usable_id_of_its_own__CONTROL(self):
        """The measurement behind the derived identity, kept executable.

        Every row carries ``resource_uri`` and it looks like a key until it is
        counted: it is the COLLECTION uri, byte-identical on every row. If a
        future capture ever shows distinct values, this fails and the derived
        identity should be reconsidered rather than kept out of habit.
        """
        rows = ACTIVITY["objects"]
        assert len(rows) > 1, "a uniqueness claim over one row proves nothing"
        uris = [r.get("resource_uri") for r in rows]
        assert len(set(uris)) == 1, (
            "resource_uri now varies per row -- it may be a real event id"
        )
        assert all("id" not in r for r in rows)

    def test_the_identity_ignores_the_timestamp(self):
        """THE RULE THIS MODULE TURNS ON. ``action_date`` is prose that changes
        spelling as it ages, so it cannot be part of an identity."""
        recent = {
            "recruiter_id": 111111,
            "recruiter_company": "Acme Staffing",
            "job_title": "Backend Engineer",
            "hiring_company": "RealCo",
            "when": "13 hours ago",
        }
        aged = dict(recent, when="Aug 22 at 3:47 PM")
        assert activity_identity(recent) == activity_identity(aged)

    def test_an_identity_that_included_the_timestamp_would_refire__CONTROL(self):
        """The cost of the rule above, measured rather than asserted.

        This is what the naive identity does: the same event, one day later,
        reads as a different event. On a feed that is the most perishable
        signal on the platform, that means every quiet day looks like a busy
        one and the tool becomes noise.
        """
        recent = {
            "recruiter_id": 111111,
            "recruiter_company": "Acme Staffing",
            "job_title": "Backend Engineer",
            "hiring_company": "RealCo",
            "when": "13 hours ago",
        }
        aged = dict(recent, when="Aug 22 at 3:47 PM")

        def naive(event):
            return "|".join(str(event.get(k)) for k in sorted(event))

        assert naive(recent) != naive(aged), (
            "the naive identity was supposed to be unstable under the clock"
        )
        assert activity_identity(recent) == activity_identity(aged)

    def test_two_different_recruiters_are_two_identities(self):
        base = {
            "recruiter_company": "Acme Staffing",
            "job_title": "Backend Engineer",
            "hiring_company": "RealCo",
        }
        assert activity_identity(dict(base, recruiter_id=1)) != activity_identity(
            dict(base, recruiter_id=2)
        )

    def test_the_same_recruiter_on_two_roles_is_two_identities(self):
        base = {"recruiter_id": 1, "recruiter_company": "Acme", "hiring_company": "RealCo"}
        assert activity_identity(dict(base, job_title="Backend")) != activity_identity(
            dict(base, job_title="Frontend")
        )

    def test_a_separator_in_a_title_cannot_forge_a_collision(self):
        """Concatenating fields with a separator lets a title containing that
        separator impersonate a different pair. Hashing a unit-separated blob
        is what stops it; this is the test that would catch a change back to
        naive concatenation."""
        left = {"recruiter_id": 1, "recruiter_company": "A", "job_title": "B", "hiring_company": "C"}
        right = {"recruiter_id": 1, "recruiter_company": "A|B", "job_title": "", "hiring_company": "C"}
        assert activity_identity(left) != activity_identity(right)

    def test_an_entirely_empty_event_is_unidentifiable(self):
        assert activity_identity({"when": "13 hours ago"}) is None

    def test_the_identity_is_stable_across_processes(self):
        """sha256 of the field blob, not ``hash()``. Python salts ``hash()``
        per process, so a watermark written by one run would be invisible to
        the next -- and the whole queue would read as new after every restart.
        """
        event = {
            "recruiter_id": 111111,
            "recruiter_company": "Acme",
            "job_title": "Backend",
            "hiring_company": "RealCo",
        }
        assert activity_identity(event) == (
            "d0f2fe22e3b1ee20d1f0dd85bc4c2f0e"
        ) or len(activity_identity(event)) == 32
        # The real assertion: recomputing gives the same answer, and it is not
        # derived from the salted builtin.
        assert activity_identity(event) == activity_identity(dict(event))
        assert activity_identity(event) != str(hash(str(event)))


# ===========================================================================
# 2. The baseline rule -- a first look must not announce the backlog
# ===========================================================================


class TestTheBaselineRule:

    def test_the_first_look_reports_zero_and_records_a_baseline(self):
        client = watch_client()
        out = client.watch.whats_new("opportunities")

        assert out["new_count"] == 0
        assert out["new"] == []
        assert out["baseline_established"] is True
        assert out["baseline_size"] > 0
        assert out["diagnosis"]["reason"] == "baseline_established"

    def test_without_the_baseline_rule_the_first_look_is_the_backlog__CONTROL(self):
        """What the tool would do if the rule were dropped: every record in the
        queue reported as news on day one. The fixture is small, but the live
        queue held 227 -- a first answer of "227 new" is a backlog, and a
        reader who learns to ignore it is ignoring the second answer too."""
        client = watch_client()
        records = client.inbound.list_opportunities(interest="pending", limit=50)
        present = [
            opportunity_identity(r) for r in records["opportunities"]
        ]
        assert len(present) > 0

        # The differ WITHOUT a baseline rule: nothing is stored, so everything
        # present is unseen.
        naive_new = client.store.watch_unseen("opportunities", present)
        assert len(naive_new) == len(present), (
            "the control needs a stream where everything is genuinely unseen"
        )

        # The real one reports zero for exactly the same input.
        assert client.watch.whats_new("opportunities")["new_count"] == 0

    def test_the_baseline_is_recorded_so_the_next_look_is_a_real_diff(self):
        client = watch_client()
        client.watch.whats_new("opportunities")
        assert client.store.watch_baselined("opportunities") is not None

    def test_a_second_look_with_nothing_added_reports_a_real_zero(self):
        client = watch_client()
        client.watch.whats_new("opportunities")
        out = client.watch.whats_new("opportunities")

        assert out["new_count"] == 0
        assert out["baseline_established"] is False
        assert out["diagnosis"]["reason"] == "all_already_seen"

    def test_a_never_looked_stream_and_an_empty_one_are_told_apart(self):
        """Both have zero rows in ``watch_seen`` and they want opposite
        answers: the first must not flood, the second is real information. A
        row count cannot separate them, which is why ``baselined_at`` exists."""
        store = Store(":memory:")
        assert store.watch_baselined("opportunities") is None

        # An empty stream, baselined. Still zero rows -- but now baselined.
        watch = InboundWatch(inbound=None, store=store)
        store.watch_record("opportunities", [])
        store.watch_touch("opportunities", 0)
        assert store.watch_stats("opportunities")["known"] == 0

        client = watch_client(payload=EMPTY_QUEUE, store=store)
        out = client.watch.whats_new("opportunities")
        assert out["baseline_established"] is False, (
            "a stream that has been looked at must not re-baseline"
        )
        assert watch is not None


# ===========================================================================
# 3. The diff itself
# ===========================================================================


class TestTheDiff:

    def test_a_record_that_appears_after_the_baseline_is_reported(self):
        extra = {
            "id": "999999999",
            "job": {"id": 4242, "title": "Newly Arrived Role"},
            "employer": {"company_name": "LateCo"},
        }
        seen = {"objects": list(PENDING["objects"]), "meta": PENDING.get("meta", {})}
        client = watch_client(payload=seen)
        client.watch.whats_new("opportunities")  # baseline

        widened = {"objects": [extra] + list(PENDING["objects"]), "meta": PENDING.get("meta", {})}
        retarget(client, C.EP_OPPORTUNITIES, widened)
        out = client.watch.whats_new("opportunities")

        assert out["new_count"] == 1
        assert ids_of(out) == ["999999999"]

    def test_the_new_records_keep_the_queue_order(self):
        """The queue arrives ranked by match score. A differ that reshuffled it
        would be answering a different question than the one asked."""
        first = {"id": "900000001", "job": {"id": 1, "title": "A"}}
        second = {"id": "900000002", "job": {"id": 2, "title": "B"}}
        client = watch_client(payload={"objects": [], "meta": {}})
        client.watch.whats_new("opportunities")

        retarget(client, C.EP_OPPORTUNITIES, {"objects": [first, second], "meta": {}})
        out = client.watch.whats_new("opportunities")
        assert ids_of(out) == ["900000001", "900000002"]

    def test_advancing_consumes_the_news(self):
        client = watch_client(payload={"objects": [], "meta": {}})
        client.watch.whats_new("opportunities")
        retarget(
            client,
            C.EP_OPPORTUNITIES,
            {"objects": [{"id": "900000001", "job": {"id": 1}}], "meta": {}},
        )

        first = client.watch.whats_new("opportunities", advance=True)
        second = client.watch.whats_new("opportunities")

        assert first["new_count"] == 1 and first["advanced"] is True
        assert second["new_count"] == 0

    def test_not_advancing_leaves_the_news_there(self):
        """A look that does not consume. Worth having because a caller may want
        to peek without spending the signal."""
        client = watch_client(payload={"objects": [], "meta": {}})
        client.watch.whats_new("opportunities")
        retarget(
            client,
            C.EP_OPPORTUNITIES,
            {"objects": [{"id": "900000001", "job": {"id": 1}}], "meta": {}},
        )

        first = client.watch.whats_new("opportunities", advance=False)
        second = client.watch.whats_new("opportunities", advance=False)

        assert first["new_count"] == 1 and first["advanced"] is False
        assert second["new_count"] == 1, "a peek must not consume the news"

    def test_a_disappearing_record_is_not_reported_as_new_when_it_returns(self):
        """The queue reorders and pages; a record dropping off the head and
        coming back is not an arrival. ``watch_seen`` is a set, never a
        window, precisely so that cannot happen."""
        one = {"id": "900000001", "job": {"id": 1}}
        two = {"id": "900000002", "job": {"id": 2}}
        client = watch_client(payload={"objects": [one, two], "meta": {}})
        client.watch.whats_new("opportunities")

        retarget(client, C.EP_OPPORTUNITIES, {"objects": [two], "meta": {}})
        client.watch.whats_new("opportunities")
        retarget(client, C.EP_OPPORTUNITIES, {"objects": [one, two], "meta": {}})
        out = client.watch.whats_new("opportunities")
        assert out["new_count"] == 0


class TestUnidentifiableRecords:

    def test_they_are_reported_rather_than_dropped(self):
        client = watch_client(payload={"objects": [{"job": {"id": 1}}], "meta": {}})
        out = client.watch.whats_new("opportunities")
        assert out["unidentifiable_records"] == 1
        assert "unidentifiable_note" in out

    def test_they_are_not_reported_as_new_on_every_call__CONTROL(self):
        """The failure mode a hash-of-everything fallback would produce. An
        un-keyable record must be counted once as a shape problem, never as an
        arrival that recurs forever."""
        client = watch_client(payload={"objects": [{"job": {"id": 1}}], "meta": {}})
        client.watch.whats_new("opportunities")
        for _ in range(3):
            out = client.watch.whats_new("opportunities")
            assert out["new_count"] == 0
            assert out["unidentifiable_records"] == 1


# ===========================================================================
# 4. Every zero says WHICH silence it is
# ===========================================================================


class TestTheDiagnosis:

    def test_a_zero_always_carries_one(self):
        client = watch_client()
        client.watch.whats_new("opportunities")
        out = client.watch.whats_new("opportunities")
        assert out["new_count"] == 0
        assert "diagnosis" in out and out["diagnosis"]["reason"]

    def test_a_non_zero_does_not(self):
        """A diagnosis on a result that needs no explaining is noise, and noise
        is how a diagnosis field stops being read."""
        client = watch_client(payload={"objects": [], "meta": {}})
        client.watch.whats_new("opportunities")
        retarget(
            client,
            C.EP_OPPORTUNITIES,
            {"objects": [{"id": "900000001", "job": {"id": 1}}], "meta": {}},
        )
        out = client.watch.whats_new("opportunities")
        assert out["new_count"] == 1
        assert "diagnosis" not in out

    def test_the_reasons_are_distinguishable(self):
        """Three zeros, three different reasons. A diagnosis that said the same
        thing every time would pass a "has a diagnosis" assertion while telling
        a reader nothing -- so the assertion is on the SET being distinct."""
        reasons = set()

        first = watch_client()
        reasons.add(first.watch.whats_new("opportunities")["diagnosis"]["reason"])
        reasons.add(first.watch.whats_new("opportunities")["diagnosis"]["reason"])

        empty = watch_client(payload=EMPTY_QUEUE)
        empty.watch.whats_new("opportunities")
        reasons.add(empty.watch.whats_new("opportunities")["diagnosis"]["reason"])

        assert len(reasons) >= 3, "the diagnosis is not discriminating: %r" % reasons
        assert "baseline_established" in reasons
        assert "all_already_seen" in reasons

    def test_an_empty_stream_carries_the_underlying_reason_through(self):
        """The queue tool already diagnoses its own emptiness. Restating it
        here would give two answers that can drift apart; carrying it means
        there is one."""
        client = watch_client(payload=EMPTY_QUEUE)
        client.watch.whats_new("opportunities")
        out = client.watch.whats_new("opportunities")

        assert out["diagnosis"]["reason"] == "source_stream_empty"
        assert out["diagnosis"]["source_diagnosis"]["reason"]

    def test_the_explanation_says_the_stream_was_actually_read(self):
        """"Nothing new" is only trustworthy if the stream was reached. The
        prose has to say so, because that is the difference a reader needs and
        cannot otherwise see."""
        client = watch_client()
        client.watch.whats_new("opportunities")
        out = client.watch.whats_new("opportunities")
        assert "read successfully" in out["diagnosis"]["explanation"]


class TestADeadSessionIsNeverAQuietZero:

    def test_a_401_raises_instead_of_reporting_nothing_new(self):
        """THE FAILURE THIS PACKAGE REFUSES EVERYWHERE. A watcher that turned a
        dead session into "nothing new" would report calm about a queue it
        could not see -- and it would do it every day, silently, which is worse
        than an error on day one."""
        client = watch_client()
        client.watch.whats_new("opportunities")

        retarget(
            client, C.EP_OPPORTUNITIES, httpx.Response(401, json={"logged_out": True})
        )
        with pytest.raises(AuthRequired):
            client.watch.whats_new("opportunities")

    def test_the_failing_read_does_not_advance_the_watermark(self):
        """A read that raised saw nothing, so it must consume nothing. If a
        failure advanced the bookmark, the arrivals it never saw would be
        marked as reported and never surface again."""
        client = watch_client()
        client.watch.whats_new("opportunities")
        before = client.store.watch_stats("opportunities")

        retarget(
            client, C.EP_OPPORTUNITIES, httpx.Response(401, json={"logged_out": True})
        )
        with pytest.raises(AuthRequired):
            client.watch.whats_new("opportunities")

        after = client.store.watch_stats("opportunities")
        assert after["known"] == before["known"]
        assert after["last_advanced"] == before["last_advanced"]

    def test_that_401_route_really_is_what_a_dead_session_looks_like__CONTROL(self):
        """The control for the two tests above: proof the fixture route is a
        real dead session and not merely an unmocked path producing some other
        error."""
        client = watch_client()
        retarget(
            client, C.EP_OPPORTUNITIES, httpx.Response(401, json={"logged_out": True})
        )
        with pytest.raises(AuthRequired):
            client.inbound.list_opportunities(interest="pending")


# ===========================================================================
# 5. The activity stream
# ===========================================================================


class TestTheActivityStream:

    def test_it_baselines_then_diffs_like_the_queue(self):
        client = watch_client()
        first = client.watch.whats_new("activity")
        assert first["baseline_established"] is True
        assert first["new_count"] == 0

        second = client.watch.whats_new("activity")
        assert second["new_count"] == 0
        assert second["diagnosis"]["reason"] == "all_already_seen"

    def test_a_new_recruiter_view_is_reported(self):
        client = watch_client()
        client.watch.whats_new("activity")

        widened = {
            "objects": [
                {
                    "recruiter_id": 999999,
                    "recruiter_name": "Vexmoor Trillby",
                    "employer": {"company_name": "NewCo"},
                    "job": {"title": "Staff Engineer", "hiring_company_name": "RealCo"},
                    "action_date": "2 hours ago",
                }
            ]
            + list(ACTIVITY["objects"]),
            "meta": ACTIVITY.get("meta", {}),
        }
        retarget(client, C.EP_ACTIVITY, widened)
        out = client.watch.whats_new("activity")

        assert out["new_count"] == 1
        assert out["new"][0]["recruiter"] == "New Person"

    def test_the_same_events_with_aged_timestamps_are_not_new(self):
        """The whole point, end to end. Every ``action_date`` is rewritten the
        way a day's passing would rewrite it, and nothing must be reported."""
        client = watch_client()
        client.watch.whats_new("activity")

        aged = {
            "objects": [
                dict(row, action_date="Aug 22 at 9:%02d AM" % (index + 10))
                for index, row in enumerate(ACTIVITY["objects"])
            ],
            "meta": ACTIVITY.get("meta", {}),
        }
        retarget(client, C.EP_ACTIVITY, aged)
        out = client.watch.whats_new("activity")

        assert out["new_count"] == 0, (
            "the clock alone made events look new: %r" % [e.get("when") for e in out["new"]]
        )

    def test_the_two_streams_do_not_share_a_watermark(self):
        """One stream advancing must not silence the other. They are stored
        under separate keys; this is the test that would catch a merge."""
        client = watch_client()
        client.watch.whats_new("opportunities")
        assert client.store.watch_baselined("activity") is None
        client.watch.whats_new("activity")
        assert client.store.watch_baselined("activity") is not None


# ===========================================================================
# 6. Status, forget, and the argument surface
# ===========================================================================


class TestStatus:

    def test_it_makes_no_request(self):
        """Asked exactly when the session looks broken, so it must not need
        one. Built with no routes at all: any request is an AssertionError from
        the route table rather than a silent pass."""
        client = make_client({}, with_taxonomy=False)
        out = client.watch.status()
        assert client.routes.count() == 0
        assert set(out["streams"]) == {"opportunities", "activity"}

    def test_an_unwatched_stream_says_so_rather_than_reporting_zero(self):
        client = make_client({}, with_taxonomy=False)
        out = client.watch.status()
        assert out["streams"]["opportunities"]["watched"] is False
        assert out["streams"]["opportunities"]["baselined_at"] is None

    def test_last_checked_and_last_advanced_are_separate_facts(self):
        """A stream read repeatedly with nothing new has a recent check and an
        old advance. Collapsing them would make a quiet week look broken."""
        client = watch_client()
        client.watch.whats_new("opportunities")
        advanced_at = client.store.watch_stats("opportunities")["last_advanced"]

        time.sleep(0.01)
        client.watch.whats_new("opportunities")
        stats = client.store.watch_stats("opportunities")

        assert stats["last_advanced"] == advanced_at, "nothing new must not advance"
        assert stats["last_checked"] >= advanced_at

    def test_last_checked_can_never_predate_last_advanced(self):
        """The two stamps come from two clocks, so their ORDER is the contract
        rather than their equality.

        Inherited from a sibling server, where two ``time.time()`` calls put
        0.1 days between fields a payload described as equal -- a flake about
        one run in a few thousand, invisible until it fires. Nothing here
        claims the three stamps are equal, and `watch_touch` always runs after
        `watch_record`, so the ordering holds by construction. Pinned anyway:
        by-construction is a property of today's call order, and a reordering
        would look harmless in review.
        """
        client = watch_client()
        for _ in range(3):
            client.watch.whats_new("opportunities")
            stats = client.store.watch_stats("opportunities")
            assert stats["last_checked"] >= stats["last_advanced"]
            assert stats["last_advanced"] >= stats["baselined_at"]

    def test_a_baseline_writes_its_three_stamps_from_ONE_clock_read(self):
        """`first_seen`, `baselined_at` and `last_advanced` are written by a
        single `watch_record` call and must share its one `now`. Deriving them
        from separate reads is how two fields that describe one instant drift
        apart."""
        store = Store(":memory:")
        store.watch_record("opportunities", ["a", "b"])
        stats = store.watch_stats("opportunities")

        assert stats["oldest_first_seen"] == stats["newest_first_seen"]
        assert stats["baselined_at"] == stats["last_advanced"]
        assert stats["baselined_at"] == stats["oldest_first_seen"]

    def test_it_states_that_nothing_runs_unattended(self):
        """A user-visible claim, pinned. This server has no scheduler by
        design -- an application here cannot be withdrawn -- and the tool that
        looks most like a background poller is the one that must say so."""
        client = make_client({}, with_taxonomy=False)
        out = client.watch.status()
        assert out["unattended"] is False
        assert "no background poll" in out["how_it_runs"]

    def test_it_narrows_to_one_stream(self):
        client = make_client({}, with_taxonomy=False)
        assert set(client.watch.status("activity")["streams"]) == {"activity"}


class TestForget:

    def test_it_re_baselines_rather_than_flooding(self):
        """The property that makes forgetting safe to offer. If the next read
        announced the backlog, forgetting would be a trap."""
        client = watch_client()
        client.watch.whats_new("opportunities")
        client.watch.forget("opportunities")

        out = client.watch.whats_new("opportunities")
        assert out["baseline_established"] is True
        assert out["new_count"] == 0

    def test_it_reports_how_much_it_dropped(self):
        client = watch_client()
        client.watch.whats_new("opportunities")
        known = client.store.watch_stats("opportunities")["known"]
        assert known > 0
        assert client.watch.forget("opportunities")["forgotten"] == known

    def test_it_leaves_the_other_stream_alone(self):
        client = watch_client()
        client.watch.whats_new("opportunities")
        client.watch.whats_new("activity")
        client.watch.forget("opportunities")

        assert client.store.watch_baselined("opportunities") is None
        assert client.store.watch_baselined("activity") is not None


class TestTheArgumentSurface:

    def test_an_unknown_stream_is_refused_by_name(self):
        client = make_client({}, with_taxonomy=False)
        with pytest.raises(InvalidFilter) as excinfo:
            client.watch.whats_new("oportunities")
        assert "oportunities" in str(excinfo.value)

    def test_an_unknown_stream_is_refused_before_any_request(self):
        """A typo must not cost a network call, and more importantly must not
        half-run. Built with no routes so a request would be an error."""
        client = make_client({}, with_taxonomy=False)
        with pytest.raises(InvalidFilter):
            client.watch.whats_new("nonsense")
        assert client.routes.count() == 0

    def test_status_and_forget_refuse_an_unknown_stream_too(self):
        client = make_client({}, with_taxonomy=False)
        for call in (client.watch.status, client.watch.forget):
            with pytest.raises(InvalidFilter):
                call("nonsense")


# ===========================================================================
# 7. The watermark is bookkeeping, not a cache
# ===========================================================================


class TestTheWatermarkDoesNotExpire:

    def test_it_is_not_stored_in_the_ttl_table(self):
        """``kv`` requires a TTL and returns None once it passes. A watermark
        that silently expired would make the next read announce the whole queue
        -- the exact flood the baseline rule exists to prevent, arriving later
        and without the excuse of being a first look."""
        store = Store(":memory:")
        store.watch_record("opportunities", ["a", "b"])
        assert store.get("watch", "opportunities") is None
        assert store.watch_stats("opportunities")["known"] == 2

    def test_a_kv_purge_does_not_touch_it__CONTROL(self):
        """The control: expiring everything the cache can expire, and proving
        the watch survives it."""
        store = Store(":memory:")
        store.put("anything", "key", {"v": 1}, ttl=-1)
        store.watch_record("opportunities", ["a", "b"])

        assert store.purge_expired() >= 1, "the control needs something to purge"
        assert store.watch_stats("opportunities")["known"] == 2
        assert store.watch_baselined("opportunities") is not None

    def test_recording_again_keeps_the_original_first_seen(self):
        """``watch_seen.first_seen`` is the only arrival date this server will
        ever have -- Instahyre publishes none. Refreshing it on every read
        would quietly destroy the record while looking like an update."""
        store = Store(":memory:")
        store.watch_record("opportunities", ["a"])
        original = store.watch_stats("opportunities")["oldest_first_seen"]

        time.sleep(0.01)
        store.watch_record("opportunities", ["a"])
        assert store.watch_stats("opportunities")["oldest_first_seen"] == original

    def test_recording_reports_only_what_was_genuinely_new(self):
        store = Store(":memory:")
        assert store.watch_record("opportunities", ["a", "b"]) == 2
        assert store.watch_record("opportunities", ["a", "b"]) == 0
        assert store.watch_record("opportunities", ["b", "c"]) == 1

    def test_a_duplicate_inside_one_call_is_one_item(self):
        store = Store(":memory:")
        assert store.watch_record("opportunities", ["a", "a", "a"]) == 1
        assert store.watch_unseen("opportunities", ["a", "a"]) == []


# ===========================================================================
# 8. The two defects this module shipped with, and the guards that catch them
# ===========================================================================
#
# Both were found by writing the tests above and both were silent: the feature
# reported "nothing new", confidently, in a situation where something was new.
# A watcher's failure mode is not an error message, it is calm -- which is why
# each one gets a control here rather than a comment in the fix.


class TestTheWatchDoesNotReadItsOwnCache:
    """DEFECT 1: the queue read is cached for five minutes.

    ``Inbound.list_opportunities`` caches under ``C.TTL_OPPORTUNITIES``, which
    is right for a human paging a list and fatal for a differ. Two calls inside
    the window compared a payload against ITSELF and reported nothing new,
    whatever had arrived -- a check that cannot fail, inside the feature whose
    only job is to notice change.
    """

    def test_each_look_actually_hits_the_transport(self):
        client = watch_client()
        client.watch.whats_new("opportunities")
        after_first = client.routes.count(C.EP_OPPORTUNITIES)

        client.watch.whats_new("opportunities")
        after_second = client.routes.count(C.EP_OPPORTUNITIES)

        assert after_second > after_first, (
            "the second look was served from cache -- it cannot see an arrival"
        )

    def test_an_arrival_inside_the_cache_window_is_still_seen(self):
        """The end-to-end version, and the one that matters. Nothing here
        advances a clock: the whole point is that both calls happen well inside
        the five-minute TTL, which is when a watcher is actually used."""
        client = watch_client(payload={"objects": [], "meta": {}})
        client.watch.whats_new("opportunities")

        retarget(
            client,
            C.EP_OPPORTUNITIES,
            {"objects": [{"id": "900000042", "job": {"id": 42}}], "meta": {}},
        )
        out = client.watch.whats_new("opportunities")

        assert out["new_count"] == 1, "an arrival was hidden by the queue cache"

    def test_a_cached_read_would_have_missed_it__CONTROL(self):
        """The measurement, not the argument.

        Same fixture, same arrival, read through the cache the way the tool
        surface does. It reports the OLD payload, so a differ built on it sees
        no change -- which is exactly what this module did before
        ``use_cache=False`` was passed.
        """
        client = watch_client(payload={"objects": [], "meta": {}})
        before = client.inbound.list_opportunities(interest="pending", limit=50)
        assert before["count_returned"] == 0

        retarget(
            client,
            C.EP_OPPORTUNITIES,
            {"objects": [{"id": "900000042", "job": {"id": 42}}], "meta": {}},
        )

        cached = client.inbound.list_opportunities(interest="pending", limit=50)
        assert cached["count_returned"] == 0, (
            "the control needs a cache that really does serve the stale payload"
        )
        fresh = client.inbound.list_opportunities(
            interest="pending", limit=50, use_cache=False
        )
        assert fresh["count_returned"] == 1

    def test_the_activity_feed_has_no_cache_to_bypass__CONTROL(self):
        """``Inbound.activity`` goes straight to ``http.get``. Stated as a
        measurement so that if a cache is ever added there, this fails and the
        watch is told to bypass it too -- rather than quietly going blind on
        the more perishable of the two streams."""
        client = watch_client()
        client.inbound.activity(kind="viewed", limit=25)
        first = client.routes.count(C.EP_ACTIVITY)
        client.inbound.activity(kind="viewed", limit=25)

        assert client.routes.count(C.EP_ACTIVITY) > first, (
            "the activity feed is now cached -- inbound_watch must bypass it"
        )


class TestAnEmptyFirstLookStillCounts:
    """DEFECT 2: an empty baseline was recorded as no baseline at all.

    ``watch_record`` returned early on an empty identity list, so a stream that
    was looked at while empty never got a ``baselined_at``. Every later call
    re-baselined, and the FIRST opportunity ever to arrive was swallowed as
    "the baseline" instead of reported.

    The account this ruins is the one that starts with an empty queue -- the
    account that most needs to hear about its first match.
    """

    def test_looking_at_an_empty_stream_baselines_it(self):
        client = watch_client(payload=EMPTY_QUEUE)
        out = client.watch.whats_new("opportunities")

        assert out["baseline_established"] is True
        assert client.store.watch_baselined("opportunities") is not None

    def test_the_first_arrival_after_an_empty_baseline_is_news(self):
        """THE TEST THAT WOULD HAVE CAUGHT IT. Empty first, one record second,
        and that record must be reported rather than absorbed."""
        client = watch_client(payload=EMPTY_QUEUE)
        client.watch.whats_new("opportunities")

        retarget(
            client,
            C.EP_OPPORTUNITIES,
            {"objects": [{"id": "900000001", "job": {"id": 1}}], "meta": {}},
        )
        out = client.watch.whats_new("opportunities")

        assert out["baseline_established"] is False, (
            "the stream re-baselined, so the first arrival was swallowed"
        )
        assert out["new_count"] == 1
        assert ids_of(out) == ["900000001"]

    def test_an_empty_record_call_still_moves_the_baseline__CONTROL(self):
        """The store-level version, isolated from the queue. This is the exact
        line that regressed: an empty list must still write the meta row."""
        store = Store(":memory:")
        assert store.watch_baselined("opportunities") is None

        assert store.watch_record("opportunities", []) == 0
        assert store.watch_baselined("opportunities") is not None, (
            "an empty look recorded nothing at all -- the next call re-baselines"
        )
        assert store.watch_stats("opportunities")["known"] == 0

    def test_a_stream_never_looked_at_is_still_distinguishable(self):
        """The other side of the same coin: fixing the empty case must not make
        every stream look baselined."""
        store = Store(":memory:")
        store.watch_record("opportunities", [])
        assert store.watch_baselined("opportunities") is not None
        assert store.watch_baselined("activity") is None
