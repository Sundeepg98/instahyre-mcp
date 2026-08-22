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

EVERY SCANNER HERE HAS A CONTROL
--------------------------------
This file follows the rule ``test_scoring_policy.py`` is built on: a guard that
has never been shown failing is a claim, not a measurement, and six bugs in
this family last week were checks that could not fail. The payload walker is
therefore pointed at a payload that DOES carry a drive letter, in
``test_the_scanner_trips_on_an_absolute_path__CONTROL``, and asserted to trip.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from conftest import fixture_json, make_client
from instahyre_server import constants as C
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


#: ``instahyre_server_info()`` builds the process-wide client as a side effect
#: of being called, so every test here would otherwise leave one behind for a
#: later file to trip over. See the fixture's own docstring in conftest.
pytestmark = pytest.mark.usefixtures("restore_server_globals")


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
    """
    payloads = {
        "instahyre_config()": server_module.instahyre_config(),
        "instahyre_server_info()": server_module.instahyre_server_info(),
    }
    for section in policy.SECTIONS:
        payloads["instahyre_config(section=%r)" % section] = (
            server_module.instahyre_config(section=section)
        )

    client = writer_client()
    monkeypatch.setattr(server_module, "_client", client)
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

        The config is pointed at a real file first, because the interesting
        branch is the one that FOUND something: with no file, ``source`` is
        ``None`` and the leak has nothing to leak.
        """
        config = tmp_path / "config" / "jobhunt.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            json.dumps({"config_version": 1, "revision": 1,
                        "scoring": {"weights": {"skills": 0.7, "experience": 0.3}}}),
            encoding="utf-8",
        )
        monkeypatch.setenv("JOBHUNT_CONFIG", str(config))
        policy.invalidate_cache()

        leaks: list[str] = []
        for call, payload in offline_tool_payloads(monkeypatch).items():
            for hit in absolute_paths_in(payload):
                leaks.append("%s -> %s" % (call, hit))

        assert not leaks, (
            "%d absolute local path(s) reached a tool result:\n  %s"
            % (len(leaks), "\n  ".join(leaks))
        )

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
        """
        config = tmp_path / "config" / "jobhunt.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            json.dumps({"config_version": 1, "revision": 1}), encoding="utf-8"
        )
        monkeypatch.setenv("JOBHUNT_CONFIG", str(config))
        policy.invalidate_cache()

        report = server_module.instahyre_config()

        assert report["source"], "the source was emptied rather than relativised"
        assert report["source"].endswith("jobhunt.json")
        assert not ABSOLUTE_LOCAL.search(report["source"])
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
