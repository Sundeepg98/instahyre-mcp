"""What code is this process actually running -- asked directly, not inferred.

WHY THIS EXISTS
---------------
A fix committed to disk changes nothing for a server that is already up. On
2026-08-21 a stale process was misdiagnosed as a regression four separate
times, because every "did the fix load" check available that day was a
BEHAVIOURAL FINGERPRINT: does this field appear, is that count right. A
fingerprint cannot tell "the fix is absent" from "the fix is present in a
process that predates it", and those two need opposite responses -- one wants a
patch, the other wants a restart.

``instahyre_server_info()`` now answers it outright. Compare ``build.code
.commit`` against ``git rev-parse HEAD`` on disk: equal means the running code
IS the committed code, different means the process is stale and reading its
behaviour proves nothing until it is restarted.

THE FREEZE IS THE CONTRACT, NOT AN OPTIMISATION
-----------------------------------------------
A per-call ``git rev-parse`` run from a stale process reports the NEW commit
sitting on disk. That is strictly worse than reporting nothing: it reads as
confirmation that the fix is loaded, and the thing it confirms is false. So
:mod:`instahyre_server.buildinfo` resolves at IMPORT, into module constants,
and ``instahyre_server_info`` only reads them.

``test_the_stamp_is_not_re_resolved_per_call`` is the guard, and it is written
so it CANNOT pass against a re-resolving build: it makes ``subprocess.run``
raise, so any implementation that shells out on the request path dies loudly
rather than quietly returning a fresh answer. It was run against exactly such a
build before this commit landed -- see the handoff for the verbatim failure.

TWO REPOSITORIES, TWO STAMPS
----------------------------
Instahyre's scoring arithmetic is jobcore's, installed editable from a sibling
checkout. A stale jobcore is exactly as invisible as a stale server and moves
scores just as silently, so it is stamped separately rather than folded in.
"""

from __future__ import annotations

from instahyre_server import server as server_module


class TestTheBuildBlockIsPresentAndFrozen:

    def test_the_stamp_is_not_re_resolved_per_call(self, monkeypatch):
        """Two calls, and git is forbidden. A re-resolving build dies here.

        The monkeypatch is the whole instrument. ``stamp()`` memoises, so a
        naive "call it twice and compare" would pass against a build that calls
        ``resolve()`` per request too -- the values would agree, because the
        working tree does not move between two calls a microsecond apart.
        Making the subprocess RAISE removes that escape: an implementation that
        touches git on the request path cannot return at all.
        """
        import jobcore.buildinfo as jobcore_buildinfo

        def forbidden(*args, **kwargs):
            raise AssertionError(
                "git was executed on the request path -- the build stamp is "
                "being re-resolved per call, which is exactly the defect that "
                "makes a stale process report the commit sitting on disk"
            )

        monkeypatch.setattr(jobcore_buildinfo.subprocess, "run", forbidden)

        first = server_module.instahyre_server_info()
        second = server_module.instahyre_server_info()

        assert first["build"]["code"] == second["build"]["code"]
        assert first["build"]["jobcore"] == second["build"]["jobcore"]

    def test_the_payload_carries_a_commit_and_a_dirty_flag(self):
        """A commit alone is half an answer.

        ``commit`` says which commit the process was started from; ``dirty``
        says whether the loaded code differs from it. Uncommitted edits are the
        normal state during a fix, so a stamp that reported only the hash would
        claim the process matches a commit it does not match.
        """
        info = server_module.instahyre_server_info()

        code = info["build"]["code"]
        assert code["source"] == "git", (
            "this checkout is a git work tree, so an 'unknown' stamp here is a "
            "resolution failure, not a legitimate degradation: %r" % (code,)
        )
        assert isinstance(code["commit"], str) and len(code["commit"]) == 12
        assert code["commit_full"].startswith(code["commit"])
        assert isinstance(code["dirty"], bool)
        assert isinstance(code["dirty_files"], int)
        assert code["resolved_at"]

    def test_jobcore_is_stamped_separately_from_instahyre(self):
        """Two checkouts, two commits, one payload.

        The scoring arithmetic lives in jobcore and is installed editable from
        a sibling checkout, so it moves independently. Folding both into one
        stamp would make a stale jobcore invisible -- the server's own commit
        would match disk and the numbers would still be wrong.
        """
        info = server_module.instahyre_server_info()

        code = info["build"]["code"]
        core = info["build"]["jobcore"]
        assert core["source"] == "git"
        assert core["commit"], "jobcore was not stamped"
        assert core["commit"] != code["commit"], (
            "instahyre and jobcore reported the same commit, which means one "
            "stamp is standing in for both and a stale jobcore would be invisible"
        )

    def test_the_process_block_reports_this_pid_and_a_moving_uptime(self):
        """Uptime is derived per call, deliberately, and the pid identifies WHICH
        process is the stale one when two are running."""
        import os

        first = server_module.instahyre_server_info()["build"]["process"]
        second = server_module.instahyre_server_info()["build"]["process"]

        assert first["pid"] == os.getpid()
        assert first["started_at"]
        assert second["uptime_seconds"] >= first["uptime_seconds"]


class TestTheDocstringSaysWhatToDoWithIt:

    def test_it_names_the_comparison_that_detects_a_stale_process(self):
        """A field nobody knows how to read is not an instrument.

        The tool's own docstring has to carry the procedure, because the agent
        reading it has no other channel: compare against ``git rev-parse HEAD``,
        and on a mismatch RESTART rather than keep debugging behaviour.
        """
        doc = server_module.instahyre_server_info.__doc__ or ""

        assert "git rev-parse HEAD" in doc
        assert "stale" in doc.lower()
        assert "restart" in doc.lower()


# ---------------------------------------------------------------------------
# The diagnostic must be inert
# ---------------------------------------------------------------------------
#
# A tool reached for when the server is ALREADY suspect must not change the
# server while answering. ``instahyre_server_info()`` used to call
# ``get_client()``, which assigns the process-wide client to a module global,
# opens the sqlite store and creates the state directory -- as a side effect of
# being asked WHAT CODE IT IS RUNNING.
#
# That is not theoretical. It escaped this file and broke
# ``test_server.py::test_listing_tools_makes_no_request_and_builds_no_client``,
# a test in another file with nothing to do with build stamps, and the first fix
# was a conftest fixture that put the global back afterwards -- papering over the
# side effect instead of removing it. The fixture is gone; the tool no longer
# builds anything.


class TestTheDiagnosticIsInert:

    def test_it_builds_no_client(self, monkeypatch):
        """Asking what code is running must not construct a network client."""
        monkeypatch.setattr(server_module, "_client", None)
        monkeypatch.setattr(server_module, "_sessions", None)

        server_module.instahyre_server_info()

        assert server_module._client is None, (
            "instahyre_server_info() built the process-wide client as a side "
            "effect of being called"
        )
        assert server_module._sessions is None

    def test_it_still_answers_the_staleness_question_with_no_client(self, monkeypatch):
        """Inert must not mean useless.

        The three things a suspect server is interrogated for -- which code,
        which config, since when -- are exactly the three that do not need a
        client, so refusing to build one costs the diagnostic nothing.
        """
        monkeypatch.setattr(server_module, "_client", None)

        info = server_module.instahyre_server_info()

        assert info["build"]["code"]["commit"]
        assert info["build"]["jobcore"]["commit"]
        assert info["build"]["process"]["pid"]
        assert info["config"]["scoring_hash"]
        assert info["server"] == "instahyre"

    def test_the_index_says_absent_rather_than_reporting_an_empty_one(
        self, monkeypatch
    ):
        """"Nothing indexed" and "not read" are different answers.

        Reporting zeros for an index nobody opened would be a number that means
        something else -- it reads as a measurement of an empty index, and
        "your index is empty" sends a reader somewhere completely different
        from "this tool did not look".
        """
        monkeypatch.setattr(server_module, "_client", None)

        index = server_module.instahyre_server_info()["index"]

        assert "jobs_indexed" not in index, (
            "an index that was never opened is being reported as measured"
        )
        assert any("not read" in str(v) for v in index.values()), index

    def test_it_makes_no_request_even_when_a_client_does_exist__PIN(
        self, monkeypatch
    ):
        """A pin, not a red-first guard: this already held before the change.

        It is here because the change above moved the client from "built on
        demand" to "used if present", and the property that actually matters --
        this tool costs zero requests -- must survive that move. The CONTROL
        below shows the instrument can move, so this is a measurement rather
        than a check that cannot fail.
        """
        from conftest import make_client

        client = make_client({}, with_taxonomy=False)
        monkeypatch.setattr(server_module, "_client", client)

        server_module.instahyre_server_info()

        assert client.routes.count() == 0

    def test_the_request_counter_moves_for_a_tool_that_does_fetch__CONTROL(
        self, monkeypatch
    ):
        """The instrument above, shown capable of a non-zero reading."""
        from conftest import make_client

        from instahyre_server import constants as C

        client = make_client({C.EP_JOB_FUNCTION: {"objects": []}}, with_taxonomy=False)
        monkeypatch.setattr(server_module, "_client", client)

        server_module.instahyre_list_job_functions()

        assert client.routes.count() > 0
