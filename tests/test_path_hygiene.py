"""No tool result publishes this machine's directory layout.

WHAT WAS MEASURED
-----------------
On 2026-08-21 a live call to ``instahyre_config()`` against the running server
returned, verbatim::

    "source": "D:\\\\Sundeep\\\\projects\\\\job-hunting\\\\config\\\\jobhunt.json"
    "config_status": "loaded from D:\\\\Sundeep\\\\projects\\\\job-hunting\\\\config\\\\jobhunt.json"
    "searched": ["D:\\\\Sundeep\\\\projects\\\\job-hunting\\\\config\\\\jobhunt.json"]

That is wrong twice over: it publishes the operator's directory layout into any
shared transcript or future public release, and it is paid for in tokens on
every response that carries it. The sibling Naukri server already returns
``"../../config/jobhunt.json"`` for the same call.

THE RULING IS RELATIVISE, NOT DELETE
------------------------------------
"Where is the config file even?" is a real, documented use of these tools --
``instahyre_config``'s own docstring points at ``searched`` as the answer to
"why is my file not being read". A ``null`` there trades a leak for a different
defect: a field that looks like an answer and is not one. So every path is
rendered through :func:`instahyre_server.paths.display_path`, which is
``jobcore.paths.display_path`` anchored on this checkout.

WHY THE BASENAME IS NOT AN OPTION, AND WHY THAT IS TESTED
---------------------------------------------------------
The cheapest "fix" is to print ``Path(raw).name``. It collapses every entry of
a "paths I searched" list to the identical string ``jobhunt.json``, which is
strictly worse than saying nothing -- the list stops distinguishing the four
places that were tried. ``test_two_different_searched_paths_do_not_collapse``
exists to fail against that shortcut specifically.

TWO DETECTORS, AND WHICH ONE IS PRIMARY
---------------------------------------
``payload_contains`` -- does the exact path the fixture created appear anywhere
in the payload -- is the PRIMARY assertion. ``absolute_paths_in``, the
drive-letter regex, is a second opinion.

That order was not the original design; it is a correction. A drive-letter
regex CAN ONLY FIRE ON WINDOWS, and instahyre's CI runs ubuntu, where a leaked
path is ``/tmp/pytest-of-runner/...`` and carries no drive letter. jobcore's CI
caught the identical shape on 2026-08-22: every leak assertion passed on the
Linux half of the matrix while detecting nothing at all. This file was written
on a Windows box and would have been green-and-blind on the runner.

The fixture geometry is part of the instrument. ``production_geometry`` builds
``<tmp>/mcp-servers/instahyre`` with the config two levels up at
``<tmp>/config/jobhunt.json`` and rebinds the anchor to it, mirroring the live
layout. Without that, an exact-string check would FALSE-POSITIVE on Linux:
``/tmp/x`` relativised against ``/home/runner/work/r/r`` is
``../../../../tmp/x``, which contains ``/tmp/x``. Measured before relying on it.

EVERY SCANNER HERE HAS A CONTROL
--------------------------------
This file follows the rule ``test_scoring_policy.py`` is built on: a guard that
has never been shown failing is a claim, not a measurement, and six bugs in
this family last week were checks that could not fail. Both detectors are
pointed at input they must trip on, and -- because a control that can only fail
on Windows is not a control on the runner -- one of them
(``..._sees_a_posix_leak_the_regex_cannot__CONTROL``) is written in POSIX shape
and asserts the two detectors DISAGREE exactly where the blindness lives.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from conftest import fixture_json, json_response, make_client
from instahyre_server import constants as C
from instahyre_server import paths as paths_module
from instahyre_server import policy
from instahyre_server import server as server_module

#: A Windows absolute path: a drive letter, a colon, a separator. This is the
#: exact shape the live sweep found, and the one a downstream reader would
#: recognise as somebody's home directory.
#:
#: THE LOOKBEHIND IS LOAD-BEARING, and it was added because the bare pattern
#: fired on real output the first time this ran. A drive letter is ONE letter,
#: so ``[A-Za-z]:[\\/]`` also matches the ``s:/`` inside ``https://`` -- and
#: ``instahyre_update_skills``'s preview legitimately publishes
#: ``https://www.instahyre.com/api/v1/...``, which is a URL the caller needs,
#: not a leak. A scanner that cried wolf on every URL would have been muted or
#: deleted within a day, so it is made exact instead: the character before the
#: drive letter must not itself be a letter.
ABSOLUTE_LOCAL = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]")

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# The walker
# ---------------------------------------------------------------------------


def absolute_paths_in(payload, _trail="") -> list[str]:
    """Every string anywhere in ``payload`` that carries a drive letter.

    Walks dict values, list/tuple items AND dict KEYS -- a leak in a key is
    still a leak, and a walker that only visited values would be a check that
    cannot fail for a whole class of payload.

    Returns ``["<json path> = <value>"]`` rather than a bare bool so a failure
    names WHERE the leak is, which is the difference between a finding and a
    puzzle.
    """
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            here = "%s.%s" % (_trail, key)
            if isinstance(key, str) and ABSOLUTE_LOCAL.search(key):
                found.append("%s (KEY) = %r" % (here, key))
            found.extend(absolute_paths_in(value, here))
    elif isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            found.extend(absolute_paths_in(value, "%s[%d]" % (_trail, index)))
    elif isinstance(payload, str):
        if ABSOLUTE_LOCAL.search(payload):
            found.append("%s = %r" % (_trail or "<root>", payload))
    return found


def payload_contains(payload, needle: str, _trail: str = "") -> list[str]:
    """Every place the EXACT string ``needle`` appears anywhere in ``payload``.

    THIS IS THE PRIMARY LEAK DETECTOR. :func:`absolute_paths_in` is a second
    opinion, and the reason is a defect CI found on 2026-08-22 in this exact
    kind of scanner: **a drive-letter regex can only fire on Windows.** On the
    ubuntu runner the leaked path is ``/tmp/pytest-of-runner/...``, which
    carries no drive letter, so every regex-based leak assertion passed while
    detecting absolutely nothing -- green, and certifying nothing. instahyre's
    CI runs ubuntu, so the strongest instrument in this file was blind exactly
    where it runs.

    Searching for the path the fixture actually created is platform-independent
    and strictly stronger: it fails on a real leak on either OS, and it cannot
    pass by being unable to see.

    THE FIXTURE GEOMETRY IS PART OF THE INSTRUMENT -- see
    :func:`production_geometry`. A bare substring search is only exact if a
    CORRECT rendering cannot contain the needle, and against the real checkout
    on a Linux runner it can: ``/tmp/x`` relativised against
    ``/home/runner/work/r/r`` is ``../../../../tmp/x``, which contains
    ``/tmp/x``. Measured, not assumed. So the leak tests anchor on a
    constructed checkout where the correct answer is ``../../config/jobhunt
    .json`` and the needle cannot appear in it by accident.
    """
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            here = "%s.%s" % (_trail, key)
            if isinstance(key, str) and needle in key:
                found.append("%s (KEY)" % here)
            found.extend(payload_contains(value, needle, here))
    elif isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            found.extend(payload_contains(value, needle, "%s[%d]" % (_trail, index)))
    elif isinstance(payload, str) and needle in payload:
        found.append("%s = %r" % (_trail or "<root>", payload))
    return found


def production_geometry(tmp_path, monkeypatch, *, content: str):
    """A temp checkout laid out exactly like the real one, with a config beside it.

    Mirrors production: ``<root>/mcp-servers/instahyre`` with the shared config
    at ``<root>/config/jobhunt.json``, two levels up. That is the geometry the
    live leak was found in, and it is the one where a correct rendering is the
    literal string ``../../config/jobhunt.json`` -- on every OS, with no
    component of the absolute path left in it.

    Rebinding ``paths.CHECKOUT_ROOT`` is what makes that possible. It is read
    at CALL time by ``display_path``, including through ``policy``'s
    import-by-name, so one ``setattr`` moves every render site at once and
    ``monkeypatch`` puts it back.

    Returns ``(checkout, config_path)``.
    """
    checkout = tmp_path / "mcp-servers" / "instahyre"
    checkout.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "config" / "jobhunt.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(content, encoding="utf-8")

    monkeypatch.setattr(paths_module, "CHECKOUT_ROOT", checkout)
    monkeypatch.setenv("JOBHUNT_CONFIG", str(config))
    policy.invalidate_cache()
    return checkout, config


def assert_no_leak(payloads: dict, config) -> None:
    """Both detectors over every payload, the exact-string one first.

    Reported together so a failure says which instrument saw it: if the needle
    check fires and the regex does not, the payload is leaking on a platform
    the regex cannot see -- which is the whole reason there are two.
    """
    leaks: list[str] = []
    for call, payload in payloads.items():
        for needle in (str(config), str(config.parent)):
            for hit in payload_contains(payload, needle):
                leaks.append("%s -> [exact %r] %s" % (call, needle, hit))
        for hit in absolute_paths_in(payload):
            leaks.append("%s -> [drive-letter] %s" % (call, hit))
    assert not leaks, (
        "%d absolute local path(s) reached a tool result:\n  %s"
        % (len(leaks), "\n  ".join(leaks))
    )


# ---------------------------------------------------------------------------
# Offline tool payloads
# ---------------------------------------------------------------------------


def writer_client():
    """A client whose whole profile-write surface is mocked, for one real write.

    Taxonomy is left unwired on purpose: a profile write has no business
    resolving a location, so a stray taxonomy read fails as an unmocked request
    rather than passing quietly.
    """
    education = fixture_json("education.json")
    profile = fixture_json("candidate_profile.json")
    skill_payload = fixture_json("skill_model.json")
    rows = copy.deepcopy(skill_payload["objects"])
    next_id = [90000001]

    def skills(request):
        if request.method == "PATCH":
            body = json.loads(request.content)
            materialised = []
            for obj in body.get("objects") or []:
                row = dict(obj)
                if row.get("id") is None:
                    row["id"] = next_id[0]
                    next_id[0] += 1
                materialised.append(row)
            rows[:] = materialised
            return {"objects": copy.deepcopy(rows)}
        return {
            "meta": dict(skill_payload["meta"], total_count=len(rows)),
            "objects": copy.deepcopy(rows),
        }

    client = make_client(
        {
            C.EP_EDUCATION: education,
            C.EP_SKILL_MODEL: skills,
            C.EP_PROFILE.format(candidate_id=profile["id"]): profile,
        },
        with_taxonomy=False,
    )
    client.http.cookies.set("csrftoken", "csrf-value", domain="www.instahyre.com")
    return client


def offline_tool_payloads(monkeypatch) -> dict:
    """Every tool result this suite can build without a network or a browser.

    Keyed by the call that produced it, so a failure names the tool rather than
    an index into a list.

    ``instahyre_rank_jobs`` is in here for a reason that is easy to miss: it
    renders no path of its own and mentions no file anywhere in its source. It
    is on this list because it spreads ``policy.summary()`` into its result,
    and so does ``instahyre_inbound_digest``. A leak that enters through that
    shared block surfaces in a SCORING tool, which is the last place a reader
    would think to look for the machine's directory layout.
    """
    payloads = {
        "instahyre_config()": server_module.instahyre_config(),
    }
    for section in policy.SECTIONS:
        payloads["instahyre_config(section=%r)" % section] = (
            server_module.instahyre_config(section=section)
        )

    ranker = make_client(
        {C.EP_JOB_SEARCH: json_response(fixture_json("search_backend_blr.json"))}
    )
    monkeypatch.setattr(server_module, "_client", ranker)
    payloads["instahyre_rank_jobs()"] = server_module.instahyre_rank_jobs(
        my_skills=["Java"], top_n=3
    )
    payloads["instahyre_server_info()"] = server_module.instahyre_server_info()

    writer = writer_client()
    monkeypatch.setattr(server_module, "_client", writer)
    payloads["instahyre_update_skills(confirm=False)"] = (
        server_module.instahyre_update_skills(["Express.js"], confirm=False)
    )
    payloads["instahyre_update_skills(confirm=True)"] = (
        server_module.instahyre_update_skills(["Express.js"], confirm=True)
    )
    return payloads


# ---------------------------------------------------------------------------
# 1. The suite-wide assertion
# ---------------------------------------------------------------------------


class TestNoToolResultCarriesAnAbsoluteLocalPath:

    def test_no_offline_tool_payload_carries_an_absolute_local_path(
        self, tmp_path, monkeypatch
    ):
        """The whole surface at once, not one field at a time.

        A per-field assertion only ever covers the fields somebody remembered.
        This walks every string in every payload, so a path added to a new tool
        next month is caught by a test written today.

        Run on the production geometry with a config that FOUND something,
        because the interesting branch is the one that found it: with no file,
        ``source`` is ``None`` and the leak has nothing to leak.
        """
        _, config = production_geometry(
            tmp_path,
            monkeypatch,
            content=json.dumps(
                {"config_version": 1, "revision": 1,
                 "scoring": {"weights": {"skills": 0.7, "experience": 0.3}}}
            ),
        )

        assert_no_leak(offline_tool_payloads(monkeypatch), config)

    def test_the_scanner_trips_on_an_absolute_path__CONTROL(self):
        """The guard above, shown failing. Without this it is a claim.

        Three placements, because a walker that only visited dict values would
        pass the first and miss the other two.
        """
        assert absolute_paths_in({"source": r"D:\Sundeep\projects\jobhunt.json"})
        assert absolute_paths_in({"searched": [r"C:\Users\Dell\.jobhunt\jobhunt.json"]})
        assert absolute_paths_in({r"D:\leak": "value"})

        assert not absolute_paths_in({"source": "../../config/jobhunt.json"})
        assert not absolute_paths_in({"source": "~/.jobhunt/jobhunt.json"})
        assert not absolute_paths_in({"source": ".../pytest-944/config/jobhunt.json"})
        assert not absolute_paths_in({"source": None, "searched": []})

    def test_the_exact_string_detector_sees_a_posix_leak_the_regex_cannot__CONTROL(
        self,
    ):
        """The two detectors, shown disagreeing exactly where it matters.

        This is the whole reason the primary detector exists, written as a
        measurement instead of an argument. A POSIX leak carries no drive
        letter, so the regex returns clean on it -- on the ubuntu runner that
        silence WAS the bug: every leak assertion in this file passed while
        seeing nothing.
        """
        posix_leak = {
            "config_error": "/tmp/pytest-of-runner/pytest-0/config/jobhunt.json"
                            " is not valid JSON: line 1 column 61"
        }
        needle = "/tmp/pytest-of-runner/pytest-0/config/jobhunt.json"

        assert payload_contains(posix_leak, needle), (
            "the primary detector cannot see a POSIX leak"
        )
        assert not absolute_paths_in(posix_leak), (
            "the drive-letter regex has started matching POSIX paths; if that "
            "is intentional this control needs rewriting, and if it is not, it "
            "is now firing on every absolute URL path in every payload"
        )

    def test_the_exact_string_detector_ignores_a_correct_rendering__CONTROL(self):
        """It must stay silent on the ANSWER, or it would fail every fix.

        The production geometry is what makes this true: rendered against a
        checkout two levels below the config, the correct answer shares no
        substring with the absolute path.
        """
        rendered = {"source": "../../config/jobhunt.json"}

        assert not payload_contains(rendered, "/tmp/x/config/jobhunt.json")
        assert not payload_contains(rendered, r"D:\Sundeep\config\jobhunt.json")

    def test_the_scanner_does_not_trip_on_a_url__CONTROL(self):
        """The false positive this scanner really produced, pinned as a case.

        ``instahyre_update_skills``'s preview publishes the endpoint it would
        write to, and the first run of the guard above reported that URL as a
        leak: a drive letter is one character, so the naive pattern matches the
        ``s:/`` in ``https://``. The URL is not a leak -- it is the field that
        makes the preview auditable -- so the pattern was made exact rather
        than the payload made quieter.
        """
        assert not absolute_paths_in(
            {"would_send": {"url": "https://www.instahyre.com/api/v1/candidate_misc"}}
        )
        assert not absolute_paths_in({"url": "http://localhost:8765/resume.html"})
        assert not absolute_paths_in({"uri": "file:///etc/hosts"})


# ---------------------------------------------------------------------------
# 2. Relativised, not collapsed
# ---------------------------------------------------------------------------


class TestTheRenderedPathIsStillAnAnswer:

    def test_two_different_searched_paths_do_not_collapse(self, tmp_path):
        """Rules out the basename shortcut.

        ``Path(raw).name`` would render both of these as ``jobhunt.json``. A
        ``searched`` list whose entries are all the same string cannot answer
        the question it exists to answer -- which of the places I looked was
        the one you meant.
        """
        from instahyre_server import paths

        first = tmp_path / "alpha" / "jobhunt.json"
        second = tmp_path / "beta" / "jobhunt.json"

        rendered_first = paths.display_path(str(first))
        rendered_second = paths.display_path(str(second))

        assert rendered_first != rendered_second, (
            "two different paths rendered identically as %r -- this is the "
            "basename collapse the tail form exists to prevent" % (rendered_first,)
        )
        assert not ABSOLUTE_LOCAL.search(rendered_first)
        assert not ABSOLUTE_LOCAL.search(rendered_second)

    def test_the_config_source_still_names_jobhunt_json(self, tmp_path, monkeypatch):
        """An answer, not a null.

        Deleting the field would also pass a leak scanner. It would not tell a
        reader which file is in force, which is the documented reason the field
        is there.

        Both detectors, exact-string first, so this holds on the ubuntu runner
        as well as here.
        """
        _, config = production_geometry(
            tmp_path,
            monkeypatch,
            content=json.dumps({"config_version": 1, "revision": 1}),
        )

        report = server_module.instahyre_config()

        assert report["source"], "the source was emptied rather than relativised"
        assert report["source"].endswith("jobhunt.json")
        assert not payload_contains(report, str(config))
        assert not payload_contains(report, str(config.parent))
        assert not ABSOLUTE_LOCAL.search(report["source"])
        assert report["source"] == "../../config/jobhunt.json", (
            "the production geometry must render exactly as the live one does; "
            "got %r" % (report["source"],)
        )
        assert report["source"] in report["config_status"], (
            "config_status carries a different path from source -- it is built "
            "from the raw value and was not relativised with it"
        )
        assert not ABSOLUTE_LOCAL.search(report["config_status"])

    def test_the_shared_config_two_levels_up_renders_as_a_relative_path(self):
        """The exact live case, rendered exactly as the Naukri server renders it.

        The shared ``jobhunt.json`` sits two directories above this checkout
        (``mcp-servers/instahyre`` -> ``job-hunting/config``). That is the path
        the live sweep caught, and ``../../config/jobhunt.json`` is what the
        sibling server already returns for it.

        Asserted against a CONSTRUCTED path rather than the operator's real
        file: this must give the same answer on a CI runner that has no such
        file, and a test that reads his live config would score differently on
        two machines.
        """
        from instahyre_server import paths

        shared = REPO_ROOT.parent.parent / "config" / "jobhunt.json"

        assert paths.display_path(str(shared)) == "../../config/jobhunt.json"

    def test_a_path_under_neither_the_checkout_nor_home_keeps_a_tail(self):
        """The last resort still distinguishes, and still carries no drive letter.

        This is the form a Linux CI runner hits for ``/tmp`` -- under neither
        anchor -- and the one whose absence made an earlier version of this
        function fall back to the bare basename on the runner and only there.
        """
        from instahyre_server import paths

        rendered = paths.display_path(
            str(Path(REPO_ROOT.anchor or "/") / "nowhere" / "at" / "all" / "jobhunt.json")
        )

        assert rendered.endswith("jobhunt.json")
        assert not ABSOLUTE_LOCAL.search(rendered)
        assert rendered != "jobhunt.json", "collapsed to the bare basename"


# ---------------------------------------------------------------------------
# 3. The anchor is defined once
# ---------------------------------------------------------------------------


class TestTheAnchorIsDefinedOnce:

    def test_the_checkout_root_is_this_repository(self):
        from instahyre_server import paths

        assert paths.CHECKOUT_ROOT == REPO_ROOT
        assert (paths.CHECKOUT_ROOT / "instahyre_server" / "server.py").exists()


# ---------------------------------------------------------------------------
# 4. The ERROR path -- where the leak survived both path fields being clean
# ---------------------------------------------------------------------------
#
# Relativising the path FIELDS was not enough, and the gap is not obvious from
# reading the fields. jobcore's loader COMPOSES its messages with the absolute
# path already baked into the sentence --
#
#     f"{path} is not valid JSON: {exc}"
#     f"cannot read {path}: {exc}"
#     f"could not append to {ledger}: {exc}"
#
# -- and those strings reach a caller through ``config_error``, which is then
# interpolated into ``config_status``. So the healthy path rendered
# "../../config/jobhunt.json" while ONE unparseable file put the whole machine
# layout back on the wire.
#
# Worse, ``policy.summary()`` surfaces ``config_error`` verbatim, and that block
# is spread into ``instahyre_rank_jobs`` and ``instahyre_inbound_digest``. The
# leak therefore reappears in tools that mention no file anywhere in their
# source, which is exactly where nobody would look for it.
#
# The fix passes ``display`` DOWN into jobcore rather than post-processing more
# fields here, so there is one place a path is rendered and no list of fields to
# keep in sync.


#: Not JSON, and unmistakably so. The parse error jobcore reports for this
#: names a line and column, which is content the fix must NOT destroy.
BROKEN_JSON = '{"config_version": 1, "scoring": {"weights": {"skills": 0.7,,,}}'


def point_at_broken_config(tmp_path, monkeypatch):
    """Aim the loader at an unparseable file and return its path."""
    path = tmp_path / "config" / "jobhunt.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(BROKEN_JSON, encoding="utf-8")
    monkeypatch.setenv("JOBHUNT_CONFIG", str(path))
    policy.invalidate_cache()
    return path


class TestTheErrorPathCarriesNoPathEither:

    def test_the_broken_config_really_does_produce_an_error__CONTROL(
        self, tmp_path, monkeypatch
    ):
        """Without this, the guard below could pass by testing nothing.

        If ``BROKEN_JSON`` ever became parseable -- a typo, a jobcore change
        that tolerates trailing commas -- then ``config_error`` would be None,
        there would be no prose to leak, and a scan of the payload would come
        back clean while proving absolutely nothing. So the precondition is
        asserted as its own test rather than assumed.

        This control is platform-independent on purpose. A control that can
        only fail on Windows is not a control on an ubuntu runner, which is the
        defect that made this whole retrofit necessary.
        """
        _, config = production_geometry(tmp_path, monkeypatch, content=BROKEN_JSON)
        loaded = policy.current()

        assert loaded.config_error, "the fixture parsed cleanly; it is not a fixture"
        assert str(config) in loaded.config_error, (
            "jobcore no longer bakes the absolute path into the message, so "
            "this whole test class is testing a defect that no longer exists"
        )
        assert payload_contains({"e": loaded.config_error}, str(config)), (
            "the exact-string detector cannot see the very leak it exists for"
        )

    def test_an_unparseable_config_leaks_no_absolute_path_through_prose(
        self, tmp_path, monkeypatch
    ):
        """The same walkers as the healthy path, pointed at the error path."""
        _, config = production_geometry(tmp_path, monkeypatch, content=BROKEN_JSON)

        assert_no_leak(offline_tool_payloads(monkeypatch), config)

    def test_the_error_is_still_an_answer(self, tmp_path, monkeypatch):
        """Scrubbed into uselessness is the same defect class, not a fix.

        Three things must survive: the file must still be NAMED, the message
        must still say what went WRONG, and it must still carry the parser's
        own detail. An error that says only "config error" cannot be acted on,
        and swapping a leak for that is not an improvement.
        """
        _, config = production_geometry(tmp_path, monkeypatch, content=BROKEN_JSON)

        report = server_module.instahyre_config()
        error = report["config_error"]

        assert error, "the error was emptied rather than relativised"
        assert not payload_contains({"e": error}, str(config))
        assert not ABSOLUTE_LOCAL.search(error)
        assert "jobhunt.json" in error, "the message no longer names the file"
        assert "not valid JSON" in error, "the message no longer says what went wrong"
        assert any(char.isdigit() for char in error), (
            "the parser's own detail (line/column) was scrubbed out with the path"
        )

        status = report["config_status"]
        assert not payload_contains({"s": status}, str(config))
        assert not ABSOLUTE_LOCAL.search(status)
        assert "jobhunt.json" in status

    def test_the_scoring_tools_do_not_leak_it_through_the_shared_summary(
        self, tmp_path, monkeypatch
    ):
        """``policy.summary()`` is spread into tools that render no path.

        Named separately from the walker above because the mechanism is
        different and worth pinning on its own: this is not a path FIELD that
        somebody forgot to render, it is a whole error SENTENCE riding into a
        scoring result on a shared provenance block.
        """
        _, config = production_geometry(tmp_path, monkeypatch, content=BROKEN_JSON)
        client = make_client(
            {C.EP_JOB_SEARCH: json_response(fixture_json("search_backend_blr.json"))}
        )
        monkeypatch.setattr(server_module, "_client", client)

        ranked = server_module.instahyre_rank_jobs(my_skills=["Java"], top_n=3)

        assert ranked["config_error"], (
            "rank_jobs stopped reporting the config error at all -- silence is "
            "not the fix; a wrong score with no explanation is worse than a leak"
        )
        assert not payload_contains(ranked, str(config)), payload_contains(
            ranked, str(config)
        )
        assert not absolute_paths_in(ranked), absolute_paths_in(ranked)
        assert "jobhunt.json" in ranked["config_error"]
