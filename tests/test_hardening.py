"""Three defects found by mutation testing, and the tests that pin the fixes.

Each of these shipped green. The suites covering them were thorough and passed
on the first run, which is exactly why the modules were mutated afterwards: a
test that has never been shown failing is a claim, not a measurement. Two
survived mutants and one instrumented probe produced the three defects below.

The provenance matters more than the fixes, so it is recorded here rather than
in a commit message nobody will read again:

1. **A restore could delete every skill on the profile.** ``snapshot_id``
   reaches ``load_snapshot`` straight from an agent-callable MCP tool. It was
   interpolated into a path with no validation, so ``"../not-a-snapshot"``
   resolved OUTSIDE the snapshots directory; and a record with no
   ``candidate_skills`` was read as "the snapshot held zero skills", which
   ``restore_skills`` faithfully carried out by deleting all four of his. The
   method's own docstring promised this could not happen -- the delete is bound
   to "ids the snapshot does not contain", and that bound is vacuous when the
   snapshot contains nothing.

2. **``withheld_by_show_message`` could not count past one.** The gate
   incremented by one and broke, so a thread gated at message 2 of 40 reported
   one withheld message and dropped 39. The docstring promised the count was
   "never silently short".

3. **``limit`` and ``offset`` raised an untyped ``ValueError``.** Every other
   bad input on that module becomes an ``InvalidFilter`` carrying a field name;
   these two escaped the package's error taxonomy entirely, so the MCP layer
   could not classify them.
"""

from __future__ import annotations

import json

import pytest

from conftest import fixture_json, make_client
from instahyre_server import constants as C
from instahyre_server.errors import InvalidFilter
from instahyre_server.profile_write import WriteRefused, snapshots_dir

SKILLS = fixture_json("skill_model.json")
PROFILE = fixture_json("candidate_profile.json")
EDUCATION = fixture_json("education.json")


def writer_client():
    """A client whose profile/skills reads are wired, so a restore can run.

    The write routes are deliberately NOT wired: a write that escapes a guard
    becomes a loud "Unmocked request" from the route table rather than a
    silently swallowed no-op.
    """
    client = make_client(
        {
            C.EP_SKILL_MODEL: SKILLS,
            C.EP_EDUCATION: EDUCATION,
            C.EP_PROFILE.format(candidate_id=9999999): PROFILE,
        }
    )
    client.http.cookies.set("csrftoken", "tok", domain="www.instahyre.com")
    return client


def write_requests(client):
    return [r for r in client.routes.requests if r.method in ("POST", "PATCH", "DELETE")]


# ---------------------------------------------------------------------------
# 1. The restore that could empty the profile
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    [
        "../not-a-snapshot",
        "../../etc/passwd",
        "..\\..\\windows",
        "not an id",
        "",
        "/absolute",
        "1755780000-pre-skills-write/../escape",
    ],
)
def test_a_snapshot_id_that_is_not_an_id_is_refused_before_any_file_is_read(bad_id):
    """Untrusted input naming a file, on the one operation that deletes rows."""
    client = writer_client()
    # A real snapshot must exist, or the method short-circuits on "none exist"
    # and this would pass without testing anything.
    client.profile_writer.snapshot(label="real")

    with pytest.raises(WriteRefused) as excinfo:
        client.profile_writer.restore_skills(bad_id, confirm=True)

    assert "snapshot" in str(excinfo.value).lower()
    assert write_requests(client) == [], (
        "a rejected snapshot id still reached the wire: %s" % (write_requests(client),)
    )


def test_a_wellformed_file_inside_the_directory_is_still_refused_if_it_is_not_a_snapshot_id():
    """The case that separates the two guards, and the reason this test exists.

    The path-escape cases above are all caught by the containment check even
    with the id pattern disabled, so they prove the refusal happens without
    proving WHICH guard did it -- a mutation run showed exactly that, by killing
    the pattern check and watching them all still pass.

    This input escapes containment (the file really is inside the directory) and
    escapes the exists check (it really is there) and carries real skills, so
    only the id pattern stands between it and a restore from an arbitrary file.
    """
    client = writer_client()
    client.profile_writer.snapshot(label="real")
    stray = "arbitrary file"
    (snapshots_dir() / f"{stray}.json").write_text(
        json.dumps({"snapshot_id": stray, "candidate_skills": SKILLS["objects"]}),
        encoding="utf-8",
    )

    with pytest.raises(WriteRefused) as excinfo:
        client.profile_writer.restore_skills(stray, confirm=True)

    assert "not a snapshot id" in str(excinfo.value).lower()
    assert write_requests(client) == []


def test_a_snapshot_holding_no_skills_is_refused_rather_than_emptying_the_profile():
    """The bound on the delete is 'ids the snapshot does not contain'. That is
    vacuous when the snapshot contains nothing, which turns a restore into a
    wipe. This is the exact shape the probe exploited."""
    client = writer_client()
    real = client.profile_writer.snapshot(label="real")

    # Hand-write an empty snapshot with a perfectly well-formed id.
    empty_id = "1755780001-empty"
    (snapshots_dir() / f"{empty_id}.json").write_text(
        json.dumps({"snapshot_id": empty_id, "candidate_skills": [], "skill_names": []}),
        encoding="utf-8",
    )

    with pytest.raises(WriteRefused) as excinfo:
        client.profile_writer.restore_skills(empty_id, confirm=True)

    message = str(excinfo.value).lower()
    assert "no skills" in message and "delete every skill" in message
    assert write_requests(client) == []
    # The control: the legitimate snapshot alongside it still restores.
    result = client.profile_writer.restore_skills(real["snapshot_id"], confirm=False)
    assert result["would_restore_to"], "the guard must not refuse a real snapshot too"


def test_a_snapshot_that_is_not_json_is_refused_not_treated_as_empty():
    client = writer_client()
    client.profile_writer.snapshot(label="real")
    broken_id = "1755780002-broken"
    (snapshots_dir() / f"{broken_id}.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(WriteRefused):
        client.profile_writer.restore_skills(broken_id, confirm=True)
    assert write_requests(client) == []


def test_a_real_snapshot_id_still_loads_so_the_guard_is_not_refusing_everything():
    """The control for all three refusals above. A guard that says no to
    everything is as useless as one that says yes."""
    client = writer_client()
    snap = client.profile_writer.snapshot(label="pre-skills-write")

    loaded = client.profile_writer.load_snapshot(snap["snapshot_id"])

    assert loaded["snapshot_id"] == snap["snapshot_id"]
    assert len(loaded["candidate_skills"]) == len(SKILLS["objects"])


# ---------------------------------------------------------------------------
# 2. The gate that could not count past one
# ---------------------------------------------------------------------------


def _thread(messages):
    return {"objects": messages, "unsent_messages": [], "recipients": "x", "starred": False}


def _msg(n, show=True):
    return {
        "show_message": show,
        "content_html": "<p>message %d</p>" % n,
        "is_owner": False,
        "is_automated_message": False,
        "from_user": {"first_name": "Recruiter", "id": n},
        "to_user": {"full_name": "Candidate", "id": 1},
        "cc_emails": [],
        "created_at_date_time": "2026-08-20T10:00:00",
        "conversation_id": 77,
    }


def test_the_withheld_count_reports_the_whole_discarded_tail_not_just_one():
    """Gated at index 1 of 6: five records are discarded, so five is the honest
    number. The version this pins always said one."""
    thread = _thread([_msg(0), _msg(1, show=False)] + [_msg(n) for n in range(2, 6)])
    client = make_client({C.EP_MESSAGES: thread})

    out = client.inbox.read_conversation(77)

    assert out["count"] == 1, "the break must still discard the tail"
    assert out["withheld_by_show_message"] == 5, (
        "reported %s withheld, but 5 records were dropped" % out["withheld_by_show_message"]
    )


def test_a_thread_with_nothing_gated_reports_zero_withheld():
    """The control: the counter must be able to be zero."""
    client = make_client({C.EP_MESSAGES: _thread([_msg(0), _msg(1), _msg(2)])})

    out = client.inbox.read_conversation(77)

    assert out["count"] == 3
    assert out["withheld_by_show_message"] == 0


def test_include_gated_returns_the_whole_thread_and_still_counts_the_gated_ones():
    client = make_client({C.EP_MESSAGES: _thread([_msg(0), _msg(1, show=False), _msg(2)])})

    out = client.inbox.read_conversation(77, include_gated=True)

    assert out["count"] == 3
    assert out["withheld_by_show_message"] == 1


# ---------------------------------------------------------------------------
# 3. Untyped paging errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field, kwargs", [("limit", {"limit": "abc"}), ("offset", {"offset": None})])
def test_a_bad_paging_value_raises_the_packages_own_error_type(field, kwargs):
    """A bare ValueError escapes the error taxonomy, so the MCP layer cannot
    label it and the caller gets an unclassified crash instead of a filter
    complaint."""
    client = make_client({C.EP_CONVERSATIONS: fixture_json("conversations_empty.json")})

    with pytest.raises(InvalidFilter) as excinfo:
        client.inbox.list_conversations(**kwargs)

    assert excinfo.value.context.get("field") == field
    assert client.routes.requests == [], "a bad argument still hit the wire"
