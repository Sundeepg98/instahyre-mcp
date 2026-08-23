"""Prove the skill-REMOVAL tests can actually fail, by breaking the code first.

WHY THIS EXISTS
---------------
Removal is the destructive direction on the one resource that decides his
entire inbound queue, and it works by OMISSION: a skill leaves the profile by
not being copied into a payload. That makes every defect here silent by
construction -- an over-broad match, an off-by-one in the cap arithmetic, a
verification that checks the wrong list -- and it makes the tests guarding it
exactly the kind that can pass without testing anything.

So each guard is shown FAILING. This script plants one defect at a time in
``instahyre_server/profile_write.py``, runs ONLY the test that is supposed to
catch it, and requires that test to go red. Then it restores the file
byte-for-byte and re-runs the whole module to prove the tree is clean again.

A green result from a planted defect is a finding, not a nuisance: it means
that check certifies nothing. This package has already paid for a library of
checks that could not fail; this is the tool that keeps the removal tests out
of it.

Sibling controls: ``permissive_scorer_control.py`` (the scorer must reject),
``presence_is_auth_control.py`` (a cookie on disk is not a session).

    venv/Scripts/python.exe scripts/skill_removal_controls.py

Exit 0 only when every plant went red AND the module is green afterwards.
Strict ASCII, like every file in this package.
"""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = REPO / "venv" / "Scripts" / "python.exe"
TARGET = REPO / "instahyre_server" / "profile_write.py"
MODULE = "tests/test_profile_write.py"


def case(name, old, new, test):
    return {"name": name, "old": old, "new": new, "test": MODULE + "::" + test}


#: Each entry is a REAL defect somebody could plausibly write, paired with the
#: single test whose job is to catch it. The pairing is the point: a plant that
#: reddens the whole suite proves much less than one that reddens the specific
#: check that claims to cover it.
PLANTS = [
    case(
        "substring match instead of exact name match",
        "            if key and key in by_key:",
        "            if key and any(key in k or k in key for k in by_key):",
        "test_a_removal_matches_the_name_exactly_and_never_as_a_substring",
    ),
    case(
        "cap computed against the PRE-removal list",
        "        total = len(kept_names) + len(additions)",
        "        total = len(current_names) + len(additions)",
        "test_a_removal_frees_a_slot_under_the_platform_cap",
    ),
    case(
        "payload echoes every CURRENT row, so no row ever leaves",
        '        objects = list(kept) + [{"candidate": uri, "name": n} for n in additions]',
        '        objects = list(current) + [{"candidate": uri, "name": n} for n in additions]',
        "test_only_the_intended_rows_leave",
    ),
    case(
        "verification expects the pre-write list plus additions",
        '            expected=set(n.lower() for n in plan["resulting_skills"])',
        '            expected=set(n.lower() for n in plan["current_skills"] + plan["would_add"])',
        "test_a_removal_that_the_server_ignores_is_reported_not_counted_as_success",
    ),
    case(
        "restore echoes snapshot rows verbatim, dead ids and all",
        '        self.http.patch(C.EP_SKILL_MODEL, json_body={"objects": objects})',
        '        self.http.patch(C.EP_SKILL_MODEL, json_body={"objects": target})',
        "test_a_restore_recreates_a_removed_row_rather_than_naming_its_dead_id",
    ),
    case(
        "the zero-skills rail removed",
        '        if plan["would_remove"] and not plan["resulting_skills"]:',
        "        if False:",
        "test_removing_every_skill_is_refused_and_sends_nothing",
    ),
    case(
        "add and remove of the same name silently allowed",
        "        if clash:",
        "        if False:",
        "test_adding_and_removing_the_same_skill_in_one_call_is_refused",
    ),
    case(
        "a removal not on the profile is silently swallowed",
        '            report["remove_not_on_profile"] = sorted(not_on_profile)',
        '            report["remove_not_on_profile"] = []',
        "test_a_removal_of_a_skill_not_on_the_profile_removes_nothing_and_says_so",
    ),
]


def pytest_run(nodeid):
    proc = subprocess.run(
        [str(PY), "-m", "pytest", nodeid, "-q", "--no-header"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
    )
    lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
    return proc.returncode, lines[-1] if lines else "(no output)"


def main() -> int:
    if not PY.exists():
        print("no interpreter at %s" % PY)
        return 2

    results = []
    for plant in PLANTS:
        original = io.open(TARGET, encoding="utf-8", newline="").read()
        found = original.count(plant["old"])
        if found != 1:
            # NOT skipped quietly. An anchor that stopped matching means the
            # code moved and this control is no longer pointed at anything.
            results.append((plant["name"], "ANCHOR-MISSING (%d hits)" % found, ""))
            continue
        try:
            io.open(TARGET, "w", encoding="utf-8", newline="").write(
                original.replace(plant["old"], plant["new"], 1)
            )
            code, tail = pytest_run(plant["test"])
        finally:
            # Always, even on KeyboardInterrupt or a pytest crash: leaving a
            # planted defect on disk is far worse than a failed control run.
            io.open(TARGET, "w", encoding="utf-8", newline="").write(original)
        results.append(
            (
                plant["name"],
                "RED" if code != 0 else "GREEN -- THIS CHECK CANNOT FAIL",
                tail,
            )
        )

    print("\n=== skill removal: red controls ===")
    bad = 0
    for name, verdict, tail in results:
        ok = verdict == "RED"
        bad += 0 if ok else 1
        print("[%s] %-58s %s" % ("ok " if ok else "BAD", name[:58], verdict))
        if tail:
            print("       %s" % tail)

    code, tail = pytest_run(MODULE)
    print("\nafter every plant reverted: %s" % tail)
    print("%d plants, %d did not go red" % (len(results), bad))
    return 1 if bad or code != 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
