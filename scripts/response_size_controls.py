"""Prove every context-cost guard can actually fail, by breaking each one first.

WHY THIS EXISTS
---------------
On 2026-08-25 this server's 23 read tools cost a caller 113,100 bytes -- about
28,000 tokens -- to call once each. One default ``instahyre_list_opportunities``
was 21,187 of those bytes and two thirds of it described EMPLOYERS rather than
the roles it was asked to help choose between. The fix was a compact
projection, two blocks of standing documentation moved behind a ``section=``
parameter, and the package's first response-size cap.

Every test covering that work is green. Green is exactly what a check that
CANNOT fail looks like, and the failures this particular work invites are all
SILENT ONES -- which is why they get a control script rather than trust:

  A COMPACT MODE THAT QUIETLY SERVES FULL ROWS looks identical to a working
  one from the outside. The caller asked to save context, was told "compact",
  and paid full price. Nothing errors. Nothing looks wrong.

  A CAP THAT CUTS INSIDE AN OBJECT returns JSON that does not parse, and so
  loses the rows it dropped AND the rows it kept. Worse than no cap.

  DOCUMENTATION THAT BECOMES UNREACHABLE RATHER THAN RELOCATED is the failure
  this whole exercise was warned about by name. ``deliberately_not_built``
  records why surfaces were built, retired or refused -- including that
  ``mark_all_read`` is a GET that bulk-mutates, and the rule underneath it that
  writes here are counted BY EFFECT and never by HTTP verb. Shortening it is
  only acceptable while the long form is one call away. A summary that became
  the last remaining copy would pass every size test in the suite while
  destroying the thing the size tests were protecting.

  A CAP THAT LOSES ROWS WHILE PAGING is the subtlest of the four. Trim the tail
  of a page but leave ``next_offset`` pointing past it, and the caller resumes
  beyond rows it never received. Those opportunities are gone, and nothing
  anywhere reports it.

So each guard is broken here and required to be noticed.

A NOTE ON WHAT COUNTS AS RED, inherited from ``bulk_apply_controls.py``.
pytest exits 1 when a test FAILS and 4 when a node id does not exist, so a
harness that treated any non-zero exit as RED would report a plant whose test
had been RENAMED as a passing control while running nothing. Only exit 1 is
RED here.

A NOTE ON LINE ENDINGS, which is this file's own addition to the pattern. This
package is MIXED -- 14 files are CRLF and 60 are LF, and ``policy.py``, which
two plants below target, is one of the CRLF ones. A plant written with ``\\n``
anchors matches nothing in a CRLF file and reports ANCHOR-MISSING, which reads
like a moved target rather than a broken harness. Anchors are therefore
translated into each file's own convention before matching.

    venv/Scripts/python.exe scripts/response_size_controls.py

Exit 0 only when every plant went red AND every module is green afterwards.
Nothing here contacts Instahyre. Strict ASCII, like every file in this package.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = REPO / "venv" / "Scripts" / "python.exe"

SERVER = REPO / "instahyre_server" / "server.py"
BUDGET = REPO / "instahyre_server" / "budget.py"
SHAPE = REPO / "instahyre_server" / "shape.py"
POLICY = REPO / "instahyre_server" / "policy.py"

SIZE = "tests/test_response_size.py"
WRITES = "tests/test_writes.py"
MODULES = (SIZE, WRITES, "tests/test_server.py", "tests/test_inbound.py",
           "tests/test_explain.py", "tests/test_path_hygiene.py")


def case(name, path, old, new, test, module=SIZE):
    return {
        "name": name,
        "path": path,
        "old": old,
        "new": new,
        "test": module + "::" + test,
    }


PLANTS = [
    # -- GUARD 1: compact means compact ------------------------------------
    case(
        "guard 1: COMPACT SILENTLY SERVES FULL ROWS -- the caller pays full price "
        "and is told 'compact'",
        SERVER,
        '            result["opportunities"], detail\n',
        '            result["opportunities"], "full"\n',
        "test_the_queue_tool_is_compact_by_default_and_says_so",
    ),
    case(
        "guard 1: full mode quietly downgraded to compact -- the escape hatch stops "
        "escaping",
        SHAPE,
        '    if mode == "full":\n        return records\n',
        '    if mode == "full":\n        return [compact_opportunity(r) for r in records]\n',
        "test_full_detail_is_the_previous_shape_field_for_field",
    ),
    case(
        "guard 1: a misspelled mode falls back instead of raising",
        SHAPE,
        '        raise ValueError(\n'
        '            "detail must be one of %s, not %r" % (", ".join(DETAIL_MODES), detail)\n'
        '        )\n',
        '        mode = "compact"\n',
        "test_an_unknown_detail_mode_raises_rather_than_serving_either_shape",
    ),
    case(
        "guard 1: the warning flags dropped -- a dead posting presented as live",
        SHAPE,
        "    for field in COMPACT_WARNING_FIELDS:\n"
        "        if field in record:\n"
        "            out[field] = record[field]\n",
        "    for field in ():\n"
        "        if field in record:\n"
        "            out[field] = record[field]\n",
        "test_a_compact_row_keeps_the_flags_whose_absence_would_make_it_wrong",
    ),
    case(
        "guard 1: the hidden-skill count dropped -- a list that just stops",
        SHAPE,
        "        hidden = max(0, len(skills) - max_skills) + int(record.get(\"skills_more\") or 0)\n",
        "        hidden = 0\n",
        "test_the_one_differentiator_is_capped_and_says_how_many_it_hid",
    ),
    # -- GUARD 2: the cap drops WHOLE rows ---------------------------------
    case(
        "guard 2: THE CAP CUTS MID-OBJECT -- a half-written row, and JSON that no "
        "longer parses",
        BUDGET,
        "        out[key] = rows[:keep]\n",
        "        out[key] = list(rows[:keep])\n"
        "        if keep < len(rows):\n"
        "            out[key].append(dict(list(rows[keep].items())[:1]))\n",
        "test_it_drops_whole_rows_and_every_survivor_is_intact",
    ),
    case(
        "guard 2: NEXT_OFFSET LEFT PAST THE DROPPED ROWS -- a paging caller skips "
        "them and nothing says so",
        BUDGET,
        "            out[_NEXT_OFFSET_KEY] = result[_OFFSET_KEY] + keep\n",
        "            pass\n",
        "test_next_offset_is_moved_BACK_to_the_first_dropped_row",
    ),
    case(
        "guard 2: count_returned left at the pre-trim number, so the result lies "
        "about itself",
        BUDGET,
        "        if _RETURNED_COUNT_KEY in out:\n",
        "        if False:\n",
        "test_the_returned_count_is_corrected_so_it_cannot_lie",
    ),
    case(
        "guard 2: the trim reported as nothing at all -- rows_omitted removed",
        BUDGET,
        "        out[OMITTED_KEY] = omitted\n",
        "        out[OMITTED_KEY] = 0\n",
        "test_it_reports_how_many_rows_went_and_how_to_get_them",
    ),
    case(
        "guard 2: a taxonomy served half -- a caller concludes a job function does "
        "not exist",
        BUDGET,
        "WHOLE_ANSWER_TOOLS = frozenset(\n",
        "WHOLE_ANSWER_TOOLS = frozenset()\n_UNUSED_WHOLE_ANSWER = frozenset(\n",
        "test_a_taxonomy_is_served_whole_or_not_at_all",
    ),
    case(
        "guard 2: a bulk-apply PREVIEW shortened to fit -- it stops naming every "
        "opportunity it is about to apply to",
        BUDGET,
        "    if CONFIRMATION_KEY in result:\n",
        "    if False:\n",
        "test_a_confirmation_is_never_shortened_to_fit",
    ),
    case(
        "guard 2: a list of STRINGS treated as rows -- irreversible_tools trimmed",
        BUDGET,
        "        if not all(isinstance(item, dict) for item in value):\n"
        "            continue\n",
        "        if False:\n"
        "            continue\n",
        "test_a_list_of_strings_is_a_statement_and_is_never_trimmed",
    ),
    case(
        "guard 2: the cap unmounted from handled, so any new tool escapes it",
        SERVER,
        "            return budget.enforce(func(*args, **kwargs), tool=func.__name__)\n",
        "            return func(*args, **kwargs)\n",
        "test_the_cap_is_mounted_where_no_new_tool_can_miss_it",
    ),
    # THE CARVE-OUT CONSTANTS EMPTIED, not merely bypassed. This pair exists
    # because the first run of this script found the taxonomy control GREEN:
    # its test looped over ``budget.WHOLE_ANSWER_TOOLS`` itself, so emptying
    # the set deleted the protection AND every assertion guarding it in one
    # edit, and the check passed while checking nothing. Emptying a constant
    # is the cheapest way to find a test whose subject and expectation are
    # the same object. All three tests were rewritten to name their fields
    # literally; these plants are what proves the rewrite worked.
    case(
        "guard 1: the mandated compact fields emptied -- rows with no id to apply with",
        SHAPE,
        'COMPACT_OPPORTUNITY_FIELDS = ("id", "company", "title", "locations", "match_score")',
        'COMPACT_OPPORTUNITY_FIELDS = ()',
        "test_a_compact_row_carries_every_field_a_chooser_needs",
    ),
    case(
        "guard 1: the warning fields emptied at the constant, not at the loop "
        "that reads it -- an empty parametrize collects zero cases and exits 5",
        SHAPE,
        'COMPACT_WARNING_FIELDS = ("is_active", "location_match")',
        'COMPACT_WARNING_FIELDS = ()',
        "test_a_compact_row_keeps_the_flags_whose_absence_would_make_it_wrong",
    ),
    # -- GUARD 3: moved, not deleted ---------------------------------------
    case(
        "guard 3: A MOVED SECTION BECOMES UNREACHABLE -- section= returns the "
        "summary too, so the prose exists nowhere",
        SERVER,
        '    narrowed = {"section": key, key: full.get(key), "server": full["server"]}\n',
        '    narrowed = {\n'
        '        "section": key,\n'
        '        key: shape.summarise_prose(full[key])\n'
        '        if key in SERVER_INFO_PROSE_SECTIONS\n'
        '        else full.get(key),\n'
        '        "server": full["server"],\n'
        '    }\n',
        "test_the_findings_that_must_survive_any_shortening_are_reachable_verbatim",
    ),
    case(
        "guard 3: the same move, caught by the write tier's own test",
        SERVER,
        '    narrowed = {"section": key, key: full.get(key), "server": full["server"]}\n',
        '    narrowed = {\n'
        '        "section": key,\n'
        '        key: shape.summarise_prose(full[key])\n'
        '        if key in SERVER_INFO_PROSE_SECTIONS\n'
        '        else full.get(key),\n'
        '        "server": full["server"],\n'
        '    }\n',
        "test_the_reply_tool_is_declared_irreversible_by_the_server_itself",
        module=WRITES,
    ),
    case(
        "guard 3: the block dropped from the default view entirely",
        SERVER,
        "            out[key] = shape.summarise_prose(full[key])\n",
        "            out.pop(key, None)\n            continue\n",
        "test_the_short_view_names_the_call_that_returns_the_long_one",
    ),
    case(
        "guard 3: the pointer removed -- a reader of the short form is never told "
        "there is a long one",
        SERVER,
        '            out[key]["_full_text"] = (\n',
        '            out[key]["_note"] = (\n',
        "test_the_short_view_names_the_call_that_returns_the_long_one",
    ),
    case(
        "guard 3: THE SUMMARY PARAPHRASES instead of truncating, so a verdict can "
        "be quietly rewritten",
        SHAPE,
        "    if len(text) <= limit:\n        return text\n",
        '    if True:\n        return "see the full text for details"\n',
        "test_every_summarised_entry_is_a_literal_prefix_of_the_full_text",
    ),
    # -- GUARD 4: provenance counted, not guessed --------------------------
    case(
        "guard 4: the provenance summary names the wrong half, so its counts stop "
        "matching the block they summarise",
        POLICY,
        "        if source == _DEFAULTED:\n            defaulted.append(key)\n",
        "        if source != _DEFAULTED:\n            defaulted.append(key)\n",
        "test_the_provenance_summary_counts_agree_with_the_block_it_summarises",
    ),
    case(
        "guard 4: section='provenance' summarised too, so all 103 entries are gone",
        POLICY,
        '        full["provenance"] = _provenance_summary(full["provenance"])\n'
        "        return full\n",
        '        full["provenance"] = _provenance_summary(full["provenance"])\n'
        "        return full\n"
        "    full[\"provenance\"] = _provenance_summary(full[\"provenance\"])\n",
        "test_provenance_is_counted_by_default_and_verbatim_under_its_section",
    ),
]


def newline_of(text: str) -> str:
    """The convention this file already uses. See the note in the module docstring."""
    return "\r\n" if "\r\n" in text else "\n"


def translate(needle: str, eol: str) -> str:
    return needle.replace("\n", eol) if eol != "\n" else needle


def pytest_run(nodeid):
    proc = subprocess.run(
        [str(PY), "-m", "pytest", nodeid, "-q", "--no-header"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
    )
    lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
    return proc.returncode, lines[-1] if lines else "(no output)"


def verdict_for(code):
    """Only pytest's exit 1 means a test FAILED.

    Exit 4 is "ERROR: not found: <nodeid>" -- no test ran at all -- and a
    harness that read any non-zero exit as RED would report a plant whose
    target had been RENAMED as a passing control.
    """
    if code == 1:
        return "RED"
    if code == 0:
        return "GREEN -- THIS CHECK CANNOT FAIL"
    return "HARNESS -- pytest exit %d, no test ran" % code


def main() -> int:
    if not PY.exists():
        print("no interpreter at %s" % PY)
        return 2

    results = []
    for plant in PLANTS:
        target = plant["path"]
        original = io.open(target, encoding="utf-8", newline="").read()
        eol = newline_of(original)
        old = translate(plant["old"], eol)
        new = translate(plant["new"], eol)
        found = original.count(old)
        if found != 1:
            results.append((plant["name"], "ANCHOR-MISSING (%d hits)" % found, ""))
            continue
        try:
            io.open(target, "w", encoding="utf-8", newline="").write(
                original.replace(old, new, 1)
            )
            code, tail = pytest_run(plant["test"])
        finally:
            io.open(target, "w", encoding="utf-8", newline="").write(original)
        results.append((plant["name"], verdict_for(code), tail))

    print("\n=== response size: red controls ===")
    bad = 0
    for name, result, tail in results:
        ok = result == "RED"
        bad += 0 if ok else 1
        print("[%s] %-96s %s" % ("ok " if ok else "BAD", name[:96], result))
        if tail:
            print("       %s" % tail)

    failures = 0
    for module in MODULES:
        code, tail = pytest_run(module)
        failures += 0 if code == 0 else 1
        print("\nafter every plant reverted -- %s: %s" % (module, tail))
    print("\n%d plants, %d did not go red" % (len(results), bad))
    return 1 if bad or failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
