"""cache.py -- the TTL store, the job index, and the corpus tracker.

The job index is the only clock this platform has: Instahyre publishes no
posting date anywhere, so ``first_seen`` is the closest thing that will ever
exist. Anything that could quietly overwrite it is tested here.

Every Store is ``:memory:`` or a tmp_path file, so the real ``_state/`` dir is
never touched.
"""

from __future__ import annotations

import pytest

from conftest import FakeClock
from instahyre_server import cache as cache_module
from instahyre_server.cache import Store


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """A hand-cranked clock, so two writes can never share a timestamp."""
    fake = FakeClock()
    monkeypatch.setattr(cache_module, "time", fake)
    return fake


# ---------------------------------------------------------------------------
# TTL cache
# ---------------------------------------------------------------------------


def test_put_get_round_trip(store):
    store.put("search", "k", {"objects": [1, 2, 3]}, 60)
    assert store.get("search", "k") == {"objects": [1, 2, 3]}


def test_get_on_a_missing_key_is_none(store):
    assert store.get("search", "never-written") is None


def test_namespaces_do_not_collide(store):
    store.put("search", "k", "search-value", 60)
    store.put("detail", "k", "detail-value", 60)
    assert store.get("search", "k") == "search-value"
    assert store.get("detail", "k") == "detail-value"


def test_an_expired_entry_returns_none(store):
    store.put("search", "stale", {"a": 1}, -1)
    assert store.get("search", "stale") is None


def test_an_entry_expires_when_the_clock_passes_its_ttl(clock, store):
    store.put("search", "k", {"a": 1}, 60)
    clock.tick(59)
    assert store.get("search", "k") == {"a": 1}
    clock.tick(2)
    assert store.get("search", "k") is None


def test_put_overwrites_an_existing_key(store):
    store.put("search", "k", "old", 60)
    store.put("search", "k", "new", 60)
    assert store.get("search", "k") == "new"


def test_purge_expired_removes_only_the_dead_entries(store):
    store.put("search", "live", 1, 600)
    store.put("search", "dead", 2, -1)
    assert store.purge_expired() == 1
    assert store.get("search", "live") == 1


def test_a_store_survives_reopening_a_tmp_path_file(tmp_path):
    path = tmp_path / "state" / "instahyre.db"
    first = Store(path)
    first.put("search", "k", {"a": 1}, 600)
    first.close()

    second = Store(path)
    assert second.get("search", "k") == {"a": 1}
    second.close()


# ---------------------------------------------------------------------------
# Job index
# ---------------------------------------------------------------------------

RECORDS = [
    {"id": 1, "title": "Java Engineer", "company": "Wissen", "company_id": 20108,
     "locations": ["Bangalore"], "skills": ["Java"], "founded": 2015},
    {"id": 2, "title": "SDE 2", "company": "Amazon", "company_id": 377,
     "locations": ["Bangalore", "Chennai"], "skills": ["AWS", "Java"]},
]


def test_upsert_jobs_reports_new_and_seen(store):
    assert store.upsert_jobs(RECORDS) == (2, 2)


def test_upsert_jobs_reports_nothing_new_the_second_time(store):
    store.upsert_jobs(RECORDS)
    assert store.upsert_jobs(RECORDS) == (0, 2)


def test_upsert_jobs_counts_only_the_genuinely_new_ones(store):
    store.upsert_jobs(RECORDS)
    new, seen = store.upsert_jobs(RECORDS + [{"id": 3, "title": "New", "company": "C"}])
    assert (new, seen) == (1, 3)


def test_upsert_jobs_never_moves_first_seen_but_does_advance_last_seen(clock, store):
    """first_seen is this platform's only posting-date proxy. It is written
    once and must never be touched again."""
    store.upsert_jobs(RECORDS)
    original = {row["id"]: row for row in store.jobs_first_seen_after(0)}

    clock.tick(3600)
    store.upsert_jobs(RECORDS)
    later = {row["id"]: row for row in store.jobs_first_seen_after(0)}

    for job_id in (1, 2):
        assert later[job_id]["first_seen"] == original[job_id]["first_seen"], "first_seen moved"
        assert later[job_id]["last_seen"] > original[job_id]["last_seen"], "last_seen did not move"
        assert later[job_id]["last_seen"] - later[job_id]["first_seen"] == 3600


def test_upsert_jobs_refreshes_the_mutable_columns(store):
    store.upsert_jobs([{"id": 1, "title": "Old Title", "company": "A"}])
    store.upsert_jobs([{"id": 1, "title": "New Title", "company": "A"}])
    row = store.jobs_first_seen_after(0)[0]
    assert row["title"] == "New Title"


def test_upsert_jobs_skips_records_with_no_id(store):
    new, seen = store.upsert_jobs([{"title": "orphan"}, {"id": 5, "title": "real"}])
    assert (new, seen) == (1, 2)
    assert [r["id"] for r in store.jobs_first_seen_after(0)] == [5]


def test_upsert_jobs_round_trips_list_columns_as_lists(store):
    store.upsert_jobs(RECORDS)
    rows = {row["id"]: row for row in store.jobs_first_seen_after(0)}
    assert rows[2]["locations"] == ["Bangalore", "Chennai"]
    assert rows[2]["skills"] == ["AWS", "Java"]


def test_jobs_first_seen_after_filters_by_the_cutoff(clock, store):
    store.upsert_jobs([{"id": 1, "title": "old", "company": "A"}])
    cutoff = clock.tick(10)
    store.upsert_jobs([{"id": 2, "title": "new", "company": "B"}])

    assert [r["id"] for r in store.jobs_first_seen_after(cutoff)] == [2]
    assert len(store.jobs_first_seen_after(0)) == 2


def test_index_stats_counts_what_is_there(store):
    store.upsert_jobs(RECORDS)
    store.record_agency_flag(1, True, "Recro")
    stats = store.index_stats()
    assert stats["jobs_indexed"] == 2
    assert stats["distinct_companies"] == 2
    assert stats["agency_flags_cached"] == 1


# ---------------------------------------------------------------------------
# Agency flags
# ---------------------------------------------------------------------------


def test_record_agency_flag_and_read_it_back(store):
    store.record_agency_flag(438126, True, "Recro", workex_min=2, workex_max=5)

    flags = store.agency_flags([438126], max_age=3600)

    assert flags == {
        438126: {
            "is_agency": True,
            "agency_name": "Recro",
            "workex_min": 2,
            "workex_max": 5,
        }
    }


def test_record_agency_flag_stores_a_false_verdict_as_false_not_missing(store):
    """"Not an agency" is an answer, and must survive the round trip."""
    store.record_agency_flag(432558, False)
    flags = store.agency_flags([432558], max_age=3600)
    assert flags[432558]["is_agency"] is False
    assert flags[432558]["agency_name"] is None


def test_agency_flags_excludes_entries_older_than_max_age(clock, store):
    store.record_agency_flag(1, True, "Recro")
    clock.tick(7200)

    assert store.agency_flags([1], max_age=3600) == {}
    assert 1 in store.agency_flags([1], max_age=10800)


def test_agency_flags_on_an_empty_id_list_is_empty(store):
    assert store.agency_flags([], max_age=3600) == {}


def test_agency_flags_ignores_ids_that_were_never_flagged(store):
    store.upsert_jobs(RECORDS)
    assert store.agency_flags([1, 2], max_age=3600) == {}


def test_record_agency_flag_upserts_onto_an_indexed_job(store):
    store.upsert_jobs(RECORDS)
    store.record_agency_flag(1, True, "Recro")

    row = {r["id"]: r for r in store.jobs_first_seen_after(0)}[1]
    assert row["is_agency"] is True
    assert row["agency_name"] == "Recro"
    assert row["title"] == "Java Engineer", "the search-side columns were not clobbered"


def test_record_agency_flag_overwrites_a_previous_verdict(store):
    store.record_agency_flag(1, True, "Recro")
    store.record_agency_flag(1, False, None)
    assert store.agency_flags([1], max_age=3600)[1]["is_agency"] is False


# ---------------------------------------------------------------------------
# Corpus tracker
# ---------------------------------------------------------------------------


def test_corpus_history_returns_newest_first(clock, store):
    store.record_corpus("all", 13455, 439000)
    clock.tick(60)
    store.record_corpus("all", 13470, 439100)
    clock.tick(60)
    store.record_corpus("all", 13502, 439260)

    history = store.corpus_history("all")

    assert [row["total_count"] for row in history] == [13502, 13470, 13455]
    assert [row["max_id"] for row in history] == [439260, 439100, 439000]
    assert history[0]["ts"] > history[1]["ts"] > history[2]["ts"]


def test_corpus_history_is_scoped_to_its_label(clock, store):
    store.record_corpus("all", 100, 1)
    clock.tick(1)
    store.record_corpus("jobLocations=Bangalore", 50, 2)

    assert len(store.corpus_history("all")) == 1
    assert store.corpus_history("jobLocations=Bangalore")[0]["total_count"] == 50
    assert store.corpus_history("never-recorded") == []


def test_corpus_history_respects_its_limit(clock, store):
    for i in range(5):
        store.record_corpus("all", 100 + i, i)
        clock.tick(1)
    assert len(store.corpus_history("all", limit=3)) == 3


def test_corpus_readings_survive_a_reopen(tmp_path, clock):
    path = tmp_path / "instahyre.db"
    first = Store(path)
    first.record_corpus("all", 13455, 439000)
    first.close()

    second = Store(path)
    assert second.corpus_history("all")[0]["total_count"] == 13455
    second.close()


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


def test_default_db_path_follows_the_instahyre_home_env(isolated_state_home):
    """The autouse redirect is what keeps tests off the real state dir."""
    assert cache_module.default_db_path().parent == isolated_state_home


def test_in_memory_store_writes_no_file(tmp_path):
    db = Store(":memory:")
    db.put("search", "k", 1, 60)
    db.upsert_jobs(RECORDS)
    assert str(db.path) == ":memory:"
    assert list(tmp_path.rglob("*.db")) == [], "an in-memory Store touched the disk"
    db.close()


def test_the_repo_state_dir_is_not_touched_by_the_suite():
    """The real _state/ holds live session cookies. Tests stay out of it.

    Asserts the directory is UNCHANGED, not that it is absent -- the server
    creates it legitimately in normal use, so absence is not the invariant.
    Creation by a test shows up as a new key; a write shows up as a new mtime.
    """
    from conftest import REAL_STATE_AT_COLLECTION, _state_dir_snapshot

    now = _state_dir_snapshot()
    created = sorted(set(now) - set(REAL_STATE_AT_COLLECTION))
    modified = sorted(
        name
        for name, mtime in now.items()
        if name in REAL_STATE_AT_COLLECTION and REAL_STATE_AT_COLLECTION[name] != mtime
    )
    assert not created, "the test run created real state files: %s" % created
    assert not modified, "the test run wrote to real state files: %s" % modified
