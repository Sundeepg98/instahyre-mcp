"""No tool result publishes this machine's directory layout.

WHAT WAS MEASURED
-----------------
On 2026-08-21 a live call to ``instahyre_config()`` against the running server
returned, verbatim::

    "source": "D:\\\\workspace\\\\projects\\\\job-hunting\\\\config\\\\jobhunt.json"
    "config_status": "loaded from D:\\\\workspace\\\\projects\\\\job-hunting\\\\config\\\\jobhunt.json"
    "searched": ["D:\\\\workspace\\\\projects\\\\job-hunting\\\\config\\\\jobhunt.json"]

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
import os
import re
from pathlib import Path, PureWindowsPath

import pytest

from conftest import fixture_json, json_response, make_client
from instahyre_server import constants as C
from instahyre_server import paths as paths_module
from instahyre_server import policy
from instahyre_server import profile_write
from instahyre_server import server as server_module
from instahyre_server.paths import repr_spelling

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

# THE PATTERN, ASSERTED AT IMPORT -- because a detector that cannot fire is
# worse than no detector, and this one has a known way of silently losing half
# its character class. Measured on 2026-08-22: a bash heredoc through an agent
# harness collapsed ``\\`` to ``\`` in the file it wrote, turning ``[\\/]``
# into ``[\/]`` -- forward slash ONLY. The suite stayed green and the drive
# letter detector reported CLEAN on a genuine Windows leak. A mangled copy of
# this file must now raise at collection rather than certify nothing.
#
# Behavioural, not a string comparison: what matters is that both branches
# still match and the URL lookbehind still holds, whatever the source looks
# like.
assert ABSOLUTE_LOCAL.search("C:" + chr(92) + "Users"), (
    "the BACKSLASH branch of ABSOLUTE_LOCAL is dead -- this file was written "
    "by something that collapsed its escapes, and every Windows leak "
    "assertion below is now blind"
)
assert ABSOLUTE_LOCAL.search("C:/Users"), "the forward-slash branch is dead"
assert not ABSOLUTE_LOCAL.search("https://www.instahyre.com/api"), (
    "the lookbehind is gone and every URL is about to be reported as a leak"
)

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


def needles_for(config) -> list[tuple[str, str]]:
    r"""Every SPELLING of the fixture's own paths, not just the filesystem one.

    A path has more than one correct spelling, and a scrubber that knows only
    the filesystem one is blind to the rest. ``OSError.__str__`` renders its
    ``filename`` through ``repr()``, so an unreadable ``jobhunt.json`` reaches
    a tool result spelled ``C:\\Users\\...`` -- the same path, doubled
    separators -- and an exact-substring search for ``C:\Users\...`` finds
    nothing in it and returns CLEAN.

    Measured on 2026-08-22 against ``instahyre_config()``: ONE sentence
    carrying a correctly relativised ``../../config/jobhunt.json`` and this
    machine's full absolute layout at the same time, because the second half
    came from ``{exc}``. The primary detector said CLEAN; only the
    drive-letter second opinion fired -- and that is the instrument that
    cannot fire at all on the ubuntu runner.

    Adding the spelling HERE rather than at each call site is the point: every
    existing ``assert_no_leak`` caller inherits it, including the suite-wide
    walker, so a repr leak in a tool added next month is caught by a needle
    written today.

    Returns ``[(label, needle)]``; the label is what a failure prints, so a
    reader can tell which spelling saw it. On POSIX the two spellings of an
    ordinary path are identical and the second is deduplicated away, which is
    why this costs nothing there.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in (str(config), str(config.parent)):
        for label, needle in (("exact", raw), ("exact-repr", repr_spelling(raw))):
            if needle and needle not in seen:
                seen.add(needle)
                out.append((label, needle))
    return out


def assert_no_leak(payloads: dict, config) -> None:
    """Both detectors over every payload, the exact-string one first.

    Reported together so a failure says which instrument saw it: if the needle
    check fires and the regex does not, the payload is leaking on a platform
    the regex cannot see -- which is the whole reason there are two.

    The exact-string detector is run once per SPELLING -- see
    :func:`needles_for`.
    """
    leaks: list[str] = []
    for call, payload in payloads.items():
        for label, needle in needles_for(config):
            for hit in payload_contains(payload, needle):
                leaks.append("%s -> [%s %r] %s" % (call, label, needle, hit))
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

    ``instahyre_session_info(verify_live=False)`` is in here because it renders
    THREE paths by hand -- the saved session file, the browser profile, and
    whatever the cookie-jar reader says when it cannot read one -- and the
    third arrives inside composed prose (``"...%s..." % profile_dir``) where no
    field rename can reach it. That is the same shape as the ``{exc}`` leak
    measured on 2026-08-22. It is also the only offline payload here that runs
    with no client at all, which is the mode it will actually be called in.
    """
    payloads = {
        "instahyre_config()": server_module.instahyre_config(),
        "instahyre_session_info(verify_live=False)": (
            server_module.instahyre_session_info(verify_live=False)
        ),
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
        assert absolute_paths_in({"source": r"D:\workspace\projects\jobhunt.json"})
        assert absolute_paths_in({"searched": [r"C:\Users\user\.jobhunt\jobhunt.json"]})
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
        assert not payload_contains(rendered, r"D:\workspace\config\jobhunt.json")

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


# ---------------------------------------------------------------------------
# 5. The REPR spelling -- one path, two spellings, and the scrubber knew one
# ---------------------------------------------------------------------------
#
# Section 4 fixed the leak that survived in PROSE. This is the leak that
# survived the fix for section 4, and the mechanism is one line of CPython:
# ``OSError.__str__`` renders its ``filename`` through ``%R``, i.e. ``repr()``.
# So a Windows path arrives in the message with every separator DOUBLED, and
# the exact-substring pass -- ours and jobcore's alike -- looks for the
# single-separator form, finds nothing, and passes the payload through clean.
#
# Measured on 2026-08-22, on the production geometry, with a config file that
# existed but could not be read::
#
#     "config_status": "error: cannot read ../../config/jobhunt.json:
#                       [Errno 13] Permission denied:
#                       'C:\\Users\\user\\...\\config\\jobhunt.json'"
#
# ONE sentence, two halves, opposite verdicts. The ``{path}`` half is
# correctly relativised -- the substitution machinery ran and worked. The
# ``{exc}`` half is the SAME path in the other spelling, untouched.
#
# WHAT THIS IS NOT: a second renderer, and not the hand post-processing that
# was deleted on 2026-08-22 for rendering three fields and missing three.
# There is still exactly one rendering rule (``display_path``) and the
# substitution is still exact. One more SPELLING of the same needle, offered
# to the same primitive -- substituting only what jobcore's own pass
# structurally could not see, because ``Loaded.known_paths`` holds the path as
# the filesystem spells it.
#
# WHERE IT BITES, MEASURED RATHER THAN ASSUMED. On POSIX ``repr()`` of an
# ordinary path is the identity, so jobcore's existing single-spelling pass
# already scrubs ``config_status`` clean on the ubuntu runner and this class
# does not occur there at all. The config leak is WINDOWS-ONLY, and on Windows
# only the drive-letter second opinion caught it. ``load_snapshot`` below is
# the opposite case: nothing scrubs its ``{exc}`` on any platform, so that one
# leaks on the runner too.


def deny_reads_of(monkeypatch, target) -> None:
    """Make one file unreadable, the way a lock-holding editor makes it unreadable.

    A real ``PermissionError`` built from the ``(errno, strerror, filename)``
    triple the OS supplies -- which is exactly the object CPython constructs,
    and the reason its ``__str__`` renders the filename through ``repr``. The
    live condition it stands in for was produced on 2026-08-22 with a second
    handle opened ``CreateFileW(..., dwShareMode=0)``; that is not portable to
    the runner and the exception object is identical either way.

    Scoped to ONE path on purpose. A blanket failure would break the fixture
    reads in the payload walk as well, and the test would then pass by having
    destroyed its own subject.
    """
    real_read_bytes = Path.read_bytes
    wanted = str(target)

    def denied(self):
        if str(self) == wanted:
            raise PermissionError(13, "Permission denied", wanted)
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", denied)


class TestTheReprSpellingIsTheSamePath:

    def test_an_oserror_spells_its_filename_with_doubled_backslashes(self, tmp_path):
        r"""The mechanism, pinned against an exception CPython actually built.

        Everything in this section rests on one behaviour: ``OSError.__str__``
        renders ``filename`` through ``%R``. This is the only test here that
        checks that claim against a REAL exception rather than against
        :func:`instahyre_server.paths.repr_spelling` -- which the fix and the
        detector both use, so if that helper and reality ever disagreed every
        other test in this section would agree with the helper and miss it.

        The doubling half is guarded on the separator, per the brief: on POSIX
        the two spellings of an ordinary path are byte-identical and there is
        nothing to double. If a future Python stops repr-ing the filename, the
        FIRST assertion fails loudly on every platform, and the extra needles
        here and in ``instahyre_server.paths`` can then be retired KNOWINGLY
        rather than left in as cargo nobody dares touch.

        This one passes on the day it is written. It is a mechanism pin, not a
        red-first guard, and saying so is cheaper than letting a later reader
        assume it ever caught anything.
        """
        raw = str(tmp_path / "config" / "jobhunt.json")
        message = str(OSError(13, "Permission denied", raw))

        assert repr_spelling(raw) in message, (
            "OSError no longer renders its filename through repr(). The repr "
            "needle in needles_for() and the second spelling in "
            "instahyre_server.paths.relativise_prose are now dead weight and "
            "can be retired -- deliberately, now that this has been noticed. "
            "Got: %r" % (message,)
        )

        if os.sep == chr(92):
            assert raw not in message, (
                "the single-separator spelling is in the message after all, "
                "so the blindness this whole section exists for is gone"
            )
            assert repr_spelling(raw) != raw
            assert chr(92) + chr(92) in repr_spelling(raw), (
                "the separators are no longer doubled: %r" % (repr_spelling(raw),)
            )
        else:
            assert repr_spelling(raw) == raw, (
                "repr() has started escaping an ordinary POSIX path, so the "
                "second spelling is no longer a free no-op on this platform "
                "and needs its own coverage: %r" % (repr_spelling(raw),)
            )

    def test_the_two_detectors_disagree_on_a_repr_leak__CONTROL(self):
        r"""The opposite number of the POSIX control, and the PAIR is the finding.

        ``..._sees_a_posix_leak_the_regex_cannot__CONTROL`` shows the primary
        detector catching what the regex cannot. This shows the reverse: on a
        repr-spelled Windows path the PRIMARY is blind and only the second
        opinion fires. Two blind spots facing opposite directions is what
        makes "there are two detectors" a measurement rather than a slogan,
        and it is why neither may be dropped for being redundant.

        Literal strings and no fixture, so it measures the same thing on the
        ubuntu runner as it does here.
        """
        single = r"C:\Users\user\config\jobhunt.json"
        doubled = repr_spelling(single)
        payload = {
            "config_error": "cannot read ../../config/jobhunt.json: "
                            "[Errno 13] Permission denied: '%s'" % (doubled,)
        }

        assert doubled != single, "the fixture is not a repr leak"
        assert not payload_contains(payload, single), (
            "the primary detector has started seeing the repr spelling on its "
            "own; if that is intentional, needles_for() is now redundant"
        )
        assert payload_contains(payload, doubled), (
            "the repr spelling is not in the payload -- the fixture is wrong, "
            "not the detector"
        )
        assert absolute_paths_in(payload), (
            "the drive-letter regex cannot see a repr-spelled Windows path "
            "either, which would leave this class with NO detector at all on "
            "any platform"
        )

    def test_assert_no_leak_catches_a_repr_leak_the_regex_cannot__CONTROL(self):
        r"""The new needle, shown catching something NOTHING ELSE could catch.

        The planted path is a UNC path and that choice is the whole control:
        ``\\fileserver\share\...`` carries no drive letter, so
        ``absolute_paths_in`` is silent on it and the only instrument that can
        possibly fire is the repr needle. Planted against a ``C:\`` path this
        would pass on the strength of the second opinion and prove nothing
        about the thing it is named for.

        ``PureWindowsPath`` rather than ``Path`` so the shape is identical on
        the runner, where ``Path`` would read the whole string as a single
        filename and hand back ``.`` as its parent -- a needle that appears in
        ``jobhunt.json`` and would make this pass for the wrong reason.
        """
        config = PureWindowsPath(r"\\fileserver\share\config\jobhunt.json")
        doubled = repr_spelling(str(config))
        leaking = {
            "config_error": "cannot read ../../config/jobhunt.json: "
                            "[Errno 13] Permission denied: '%s'" % (doubled,)
        }
        payloads = {"instahyre_config()": leaking}

        assert not absolute_paths_in(leaking), (
            "the planted path has a drive letter after all, so the second "
            "opinion could carry this control and it would measure nothing"
        )
        assert not payload_contains(leaking, str(config)), (
            "the filesystem spelling is present too, so the primary detector "
            "could carry this control on its own"
        )

        with pytest.raises(AssertionError) as caught:
            assert_no_leak(payloads, config)

        assert "exact-repr" in str(caught.value), (
            "assert_no_leak failed for some reason other than the repr "
            "needle: %s" % (caught.value,)
        )


class TestAnUnreadableFileLeaksNoReprSpelledPath:

    def test_an_unreadable_config_leaks_no_repr_spelled_path(
        self, tmp_path, monkeypatch
    ):
        """The end-to-end case: a real OSError, all the way to a tool result.

        Asserted with all three instruments by name, because the point of this
        section is that they do NOT agree with each other -- reporting only
        the aggregate would hide which one was carrying the assertion.
        """
        _, config = production_geometry(
            tmp_path,
            monkeypatch,
            content=json.dumps({"config_version": 1, "revision": 1}),
        )
        deny_reads_of(monkeypatch, config)
        policy.invalidate_cache()

        payloads = offline_tool_payloads(monkeypatch)
        report = payloads["instahyre_config()"]

        assert report["config_error"], (
            "the unreadable branch was not taken, so there is no prose to "
            "leak and this test proves nothing"
        )
        assert "cannot read" in report["config_error"], (
            "a different error branch was taken: %r" % (report["config_error"],)
        )

        single = str(config)
        doubled = repr_spelling(single)
        assert not payload_contains(report, single), payload_contains(report, single)
        assert not payload_contains(report, doubled), payload_contains(report, doubled)
        assert not ABSOLUTE_LOCAL.search(report["config_error"]), (
            "config_error still carries an absolute local path: %r"
            % (report["config_error"],)
        )
        assert not ABSOLUTE_LOCAL.search(report["config_status"]), (
            "config_status still carries an absolute local path: %r"
            % (report["config_status"],)
        )

        # The whole offline surface, because summary() is spread into the
        # scoring tools and this error rides along with it.
        assert_no_leak(payloads, config)

        # Still an answer. Scrubbing the errno out with the path would swap
        # this defect for the one section 4 already ruled against.
        assert "jobhunt.json" in report["config_error"], (
            "the message no longer names the file"
        )
        assert "Errno" in report["config_error"], (
            "the operating system's own reason was scrubbed out with the path"
        )

    def test_an_unreadable_snapshot_leaks_no_repr_spelled_path(
        self, tmp_path, monkeypatch, isolated_state_home
    ):
        r"""``load_snapshot``'s ``({exc})`` -- the site with no scrubber at all.

        Different from the config site in a way worth stating rather than
        leaving for a reader to work out. jobcore DOES scrub ``config_status``;
        it simply cannot see the repr spelling, so that leak is Windows-only.
        Here NOTHING scrubs the exception, so the absolute snapshot path ships
        on every platform -- this test is red on the ubuntu runner too.

        No mock and no live tool. A DIRECTORY named like a snapshot file makes
        ``read_text`` raise a genuine ``OSError`` carrying the full path, and
        ``load_snapshot`` refuses before anything is sent anywhere: the
        irreversible surface of this server is never approached.
        """
        client = writer_client()
        snapshots = profile_write.snapshots_dir()
        bad = snapshots / "1755780000-not-a-file.json"
        bad.mkdir(parents=True, exist_ok=True)

        with pytest.raises(profile_write.WriteRefused) as caught:
            client.profile_writer.load_snapshot("1755780000-not-a-file")

        message = str(caught.value)
        payload = {"error": message}

        assert "could not be read as JSON" in message, (
            "the OSError branch was not the one taken, so there is no "
            "exception text to leak: %r" % (message,)
        )
        for label, needle in needles_for(bad):
            assert not payload_contains(payload, needle), (
                "[%s %r] %s" % (label, needle, payload_contains(payload, needle))
            )
        assert not absolute_paths_in(payload), absolute_paths_in(payload)

        assert bad.name in message, "the message no longer names the snapshot"


class TestAnUnparseableConfigIsNeverReportedAsLoaded:

    def test_an_unparseable_config_does_not_report_as_loaded__PIN(
        self, tmp_path, monkeypatch
    ):
        """Four failure branches, and none of them claims success.

        A PIN, not a bug fix. The sibling naukri server was measured on
        2026-08-22 reporting a config it could not parse as ``loaded from
        ...``; instahyre was checked for the same defect and found HONEST on
        every branch. Nothing pinned that, and the healthy-case assertion in
        ``test_the_config_source_still_names_jobhunt_json`` structurally
        cannot: it only ever exercises a file that parsed.

        It passes on the day it is written, which is stated rather than
        hidden. What it CAN catch is a later change that composes
        ``config_status`` from ``source`` without consulting ``config_error``
        -- which is the exact shape the sibling had.
        """
        branches = [
            ("malformed json", BROKEN_JSON),
            ("json but not an object", "[1, 2, 3]"),
            ("not json at all", "weights: skills 0.7"),
            ("undecodable bytes", b'{"config_version": 1, "note": "caf\xe9"}'),
        ]

        for label, content in branches:
            root = tmp_path / label.replace(" ", "-")
            _, config = production_geometry(root, monkeypatch, content="{}")
            if isinstance(content, bytes):
                config.write_bytes(content)
            else:
                config.write_text(content, encoding="utf-8")
            policy.invalidate_cache()

            report = server_module.instahyre_config()
            status = report["config_status"]

            assert report["config_error"], (
                "[%s] parsed cleanly, so this is not a failure branch and the "
                "assertions below would pass vacuously" % (label,)
            )
            assert status.startswith("error:"), (
                "[%s] an unparseable config reports as %r" % (label, status)
            )
            assert "loaded from" not in status, (
                "[%s] an unparseable config reports itself LOADED: %r"
                % (label, status)
            )
