"""Prove every bulk-apply gate can actually fail, by breaking each one first.

WHY THIS EXISTS
---------------
Bulk apply is the most destructive tool in this package, and the ONLY one whose
endpoints were permanently banned before it was built -- both apply_bulk
spellings sat in ``constants.FORBIDDEN_ENDPOINTS`` under the words "at any
evidence level", until the 2026-08-25 ruling that whatever is technically
possible gets built. When a ban is lifted, the thing that replaces it is the
gate. Every test covering that gate is green, and green is exactly what a check
that CANNOT fail looks like.

The risk is unusually sharp here for two reasons nothing else in this package
has at once. First, NO BULK APPLY HAS EVER BEEN SENT, so every assertion runs
on hand-built fixtures and none of it has met Instahyre. Second, the failure
this gate exists to prevent is SILENT BY NATURE: a truncated list, a deduped
list, a skipped blank, a stale id riding along -- each of those sends a
different number of irreversible applications than the caller confirmed, and
every one of them looks like success from the outside. A guard that quietly
stopped working would not announce itself; it would just start applying.

So each gate is broken here and required to be noticed. The claims defended,
one plant each, plus the contract properties underneath them:

  THE CONFIRM GATE HOLDS. Nothing is requested without confirm=True.
  THE COUNT IS A SECOND CONFIRMATION. A list that changed length fails loudly.
  THE CALLER SUPPLIES THE LIST. No selection knob exists to be reached for.
  THE CAP REFUSES, NEVER TRUNCATES. This is the one that must not regress
  quietly: applying to the first ten of fifteen is indistinguishable from
  working, and it is the reason a plant that TRUNCATES is planted here rather
  than one that merely raises the cap.
  EVERY ID IS CHECKED AGAINST THE LIVE QUEUE, re-read rather than cached.
  THE PREVIEW NAMES EVERY OPPORTUNITY. A count alone is the failure mode.
  EMPTY AND DUPLICATE ARE REFUSED, not repaired.
  CSRF IS REQUIRED, as on every other write.
  THE OUTCOME IS READ BACK FROM STATE, not from a status code.
  THE BODY IS THE CAPTURED ONE: one key, the branch's key, no is_interested.
  THE DOOR IS A NAMED ALLOWLIST OF TWO, and the READ TIER NEVER MOVED.

A NOTE ON WHAT COUNTS AS RED, because it bit a sibling. pytest exits 1 when a
test fails and 4 when a node id does not exist, and a harness that treats any
non-zero exit as RED reports a plant whose test was RENAMED as a passing
control while running nothing. Only exit 1 is RED here. That defect was found
and swept on 2026-08-25; this file inherits the fix rather than the bug.

Sibling controls: ``inbox_write_controls.py``, ``reply_guard_controls.py``,
``jsp_write_controls.py``, ``skill_removal_controls.py``,
``permissive_scorer_control.py``, ``presence_is_auth_control.py``.

    venv/Scripts/python.exe scripts/bulk_apply_controls.py

Exit 0 only when every plant went red AND every module is green afterwards.
Nothing here contacts Instahyre. Strict ASCII, like every file in this package.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = REPO / "venv" / "Scripts" / "python.exe"
WRITES = REPO / "instahyre_server" / "writes.py"
CONSTANTS = REPO / "instahyre_server" / "constants.py"
SERVER = REPO / "instahyre_server" / "server.py"

BULK = "tests/test_bulk_apply.py"
SAFETY = "tests/test_inbound_safety.py"
MODULES = (BULK, SAFETY, "tests/test_writes.py", "tests/test_server.py")


def case(name, path, old, new, test, module=BULK):
    return {
        "name": name,
        "path": path,
        "old": old,
        "new": new,
        "test": module + "::" + test,
    }


PLANTS = [
    # -- GATE 1: confirm ---------------------------------------------------
    case(
        "gate 1: confirm ignored -- the preview branch removed",
        WRITES,
        '        if not confirm:\n'
        '            preview["confirmed"] = False\n'
        '            preview["next"] = (\n'
        '                "NOTHING HAS BEEN SENT. Re-run with confirm=True and "',
        '        if False:\n'
        '            preview["confirmed"] = False\n'
        '            preview["next"] = (\n'
        '                "NOTHING HAS BEEN SENT. Re-run with confirm=True and "',
        "test_a_bulk_apply_without_confirm_issues_no_write_at_all",
    ),
    # -- GATE 2: expected_count -------------------------------------------
    case(
        "gate 2: the expected_count check removed -- one confirmation, not two",
        WRITES,
        "        if expected_count is None or int(expected_count) != resolved:",
        "        if False:",
        "test_a_wrong_expected_count_refuses_and_sends_nothing",
    ),
    case(
        "gate 2: expected_count given a default, so it can be omitted entirely",
        WRITES,
        "        opportunity_ids: Optional[list],\n        expected_count: Optional[int],",
        "        opportunity_ids: Optional[list],\n        expected_count: Optional[int] = None,",
        "test_expected_count_has_no_default_so_it_cannot_be_omitted",
    ),
    # -- GATE 3: the caller supplies the list ------------------------------
    case(
        "gate 3: an apply-to-all switch added to the tool",
        SERVER,
        "def instahyre_apply_bulk(\n"
        "    opportunity_ids: list,\n"
        "    expected_count: int,\n"
        "    confirm: bool = False,\n"
        ") -> dict:",
        "def instahyre_apply_bulk(\n"
        "    opportunity_ids: list,\n"
        "    expected_count: int,\n"
        "    apply_to_all: bool = False,\n"
        "    confirm: bool = False,\n"
        ") -> dict:",
        "test_the_tool_offers_no_way_to_select_a_list_instead_of_passing_one",
    ),
    # -- GATE 4: the cap REFUSES, never truncates --------------------------
    case(
        "gate 4: THE CAP TRUNCATES INSTEAD OF REFUSING -- ten of fifteen, silently",
        WRITES,
        "        if len(wanted) > MAX_BULK_APPLY:",
        "        wanted = wanted[:MAX_BULK_APPLY]\n        if False:",
        "test_over_the_cap_refuses_rather_than_truncating",
    ),
    case(
        "gate 4: the cap raised out of range of his queue",
        WRITES,
        "MAX_BULK_APPLY = 10",
        "MAX_BULK_APPLY = 50",
        "test_the_cap_is_a_named_constant_and_is_well_under_his_queue",
    ),
    case(
        "gate 4: the cap checked AFTER the queue read, so its refusal is ambiguous",
        WRITES,
        "        wanted = _normalise_bulk_ids(opportunity_ids)\n\n"
        "        # THE CAP IS CHECKED BEFORE THE QUEUE IS READ",
        "        wanted = _normalise_bulk_ids(opportunity_ids)\n"
        "        self._live_pending_index()\n\n"
        "        # THE CAP IS CHECKED BEFORE THE QUEUE IS READ",
        "test_the_cap_is_checked_before_the_queue_is_read",
    ),
    # -- GATE 5: every id validated against the LIVE pending queue ---------
    case(
        "gate 5: the pending-queue check removed -- a stale id rides along",
        WRITES,
        "        missing = [opp_id for opp_id in wanted if opp_id not in pending]",
        "        missing = []",
        "test_an_id_that_is_not_in_the_pending_queue_is_refused_by_name",
    ),
    case(
        "gate 5: the queue served from cache, so a just-actioned id still validates",
        WRITES,
        '        payload = self.inbound.raw_queue(interest="pending", use_cache=False)',
        '        payload = self.inbound.raw_queue(interest="pending", use_cache=True)',
        "test_the_pending_queue_is_re_read_rather_than_served_from_cache",
    ),
    case(
        "gate 5: a truncated read reported as a complete one",
        WRITES,
        '            "complete": (\n'
        '                meta.get("total_count") is None\n'
        '                or len(objects) >= meta.get("total_count")\n'
        "            ),",
        '            "complete": True,',
        "test_a_truncated_queue_read_says_so_in_the_refusal",
    ),
    # -- GATE 6: the preview NAMES every opportunity -----------------------
    case(
        "gate 6: the opportunities counted instead of named",
        WRITES,
        '                    record.get("company") or "<unnamed company>",',
        '                    "<%d selected>" % resolved,',
        "test_the_preview_names_every_opportunity_one_line_each",
    ),
    case(
        "gate 6: the named list truncated while the count stays whole",
        WRITES,
        "            ],\n"
        '            "would_apply_to_lines": [',
        "            ][:3],\n"
        '            "would_apply_to_lines": [',
        "test_the_preview_never_reports_a_count_without_the_names",
    ),
    # -- GATE 7: empty and duplicate are refused, not repaired -------------
    case(
        "gate 7: an empty list allowed through -- the site's 'means everything' default",
        WRITES,
        "    if not cleaned:\n"
        "        raise NothingToDo(\n"
        '            "Refusing a bulk apply with an empty list.',
        "    if False:\n"
        "        raise NothingToDo(\n"
        '            "Refusing a bulk apply with an empty list.',
        "test_an_empty_list_is_refused_and_never_read_as_everything",
    ),
    case(
        "gate 7: duplicates silently deduplicated instead of refused",
        WRITES,
        "    duplicated = sorted({item for item in cleaned if cleaned.count(item) > 1})",
        "    cleaned = list(dict.fromkeys(cleaned))\n    duplicated = []",
        "test_a_duplicate_id_is_refused_rather_than_deduplicated",
    ),
    case(
        "gate 7: a blank entry skipped rather than refused",
        WRITES,
        "        text = str(raw).strip()\n        if not text:",
        "        text = str(raw).strip()\n        if not text:\n            continue\n        if False:",
        "test_a_blank_entry_is_refused_rather_than_skipped",
    ),
    # -- GATE 8: CSRF ------------------------------------------------------
    case(
        "gate 8: the CSRF refusal removed",
        WRITES,
        '        self._require_csrf("send a bulk apply")',
        "        pass",
        "test_a_confirmed_bulk_apply_without_a_csrf_token_refuses_before_sending",
    ),
    # -- GATE 9: read the outcome back from STATE --------------------------
    case(
        "gate 9: a 200 treated as the outcome -- no re-read of the queue",
        WRITES,
        '            "verification": self._verify_bulk_apply(wanted, shaped),',
        '            "verification": {"ok": True, "how": "the server answered 200",\n'
        '                             "applied": [], "still_pending": []},',
        "test_an_application_that_did_not_take_is_reported_still_pending",
    ),
    # -- the captured contract --------------------------------------------
    case(
        "contract: the legacy key sent on the ES branch",
        WRITES,
        "        return path, {C.BULK_APPLY_BODY_KEY_ES: values}",
        "        return path, {C.BULK_APPLY_BODY_KEY_LEGACY: values}",
        "test_the_body_carries_exactly_one_key_and_it_is_the_branch_key",
    ),
    case(
        "contract: BOTH id keys sent, the way a hedge against the branch would",
        WRITES,
        "        return path, {C.BULK_APPLY_BODY_KEY_ES: values}",
        "        return path, {C.BULK_APPLY_BODY_KEY_ES: values,\n"
        "                      C.BULK_APPLY_BODY_KEY_LEGACY: values}",
        "test_the_body_carries_exactly_one_key_and_it_is_the_branch_key",
    ),
    case(
        "contract: is_interested smuggled in -- the shape a bulk DECLINE would need",
        WRITES,
        "        return path, {C.BULK_APPLY_BODY_KEY_ES: values}",
        '        return path, {C.BULK_APPLY_BODY_KEY_ES: values, "is_interested": True}',
        "test_the_body_has_no_is_interested_key_because_there_is_no_bulk_decline",
    ),
    case(
        "contract: the ES body posted to the legacy URL, the old pairing bug",
        WRITES,
        "        path = C.EP_APPLY_BULK_ES\n        values = []",
        "        path = C.EP_APPLY_BULK_LEGACY\n        values = []",
        "test_the_preview_shows_the_exact_request_that_would_be_sent",
    ),
    # -- the doors ---------------------------------------------------------
    case(
        "door: the two-entry allowlist replaced by a prefix rule",
        WRITES,
        "    if path not in C.SENDABLE_BULK_APPLY_PATHS:",
        "    if not path.startswith('/candidate_opportunities/'):",
        "test_the_bulk_door_refuses_anything_that_is_not_one_of_the_two_named_paths",
    ),
    case(
        "door: the read tier quietly stops refusing what the write tier now sends",
        CONSTANTS,
        '    "toggle_message_read",\n    "apply_bulk",\n)',
        '    "toggle_message_read",\n)',
        "test_the_read_tier_still_refuses_both_bulk_paths",
    ),
    case(
        "door: the inbox allowlist widened to swallow the bulk paths too",
        CONSTANTS,
        "SENDABLE_BULK_APPLY_PATHS = frozenset({EP_APPLY_BULK_ES, EP_APPLY_BULK_LEGACY})",
        "SENDABLE_BULK_APPLY_PATHS = frozenset({EP_APPLY_BULK_ES, EP_APPLY_BULK_LEGACY,\n"
        '                                       "/resume_modal/emails/message/send_message/"})',
        "test_single_apply_still_cannot_reach_a_bulk_path",
    ),
    # -- the lifted ban, re-ratified --------------------------------------
    case(
        "ruling: the ban quietly regrown, so two doors disagree about the same path",
        CONSTANTS,
        "FORBIDDEN_ENDPOINTS: frozenset = frozenset()",
        # THE LITERAL, NOT THE CONSTANT, and the first attempt got this wrong in
        # a way worth recording: EP_APPLY_BULK_ES is defined BELOW this line, so
        # a plant naming it raises NameError at import, pytest exits 4, and the
        # verdict function correctly reported "no test ran" rather than RED.
        # That is the exit-4 classifier earning its place on its first outing.
        "FORBIDDEN_ENDPOINTS: frozenset = frozenset(\n"
        '    {"/candidate_opportunities/candidate_matching/apply_bulk/"}\n)',
        "test_the_permanent_ban_was_lifted_by_ruling_and_left_an_empty_set",
        module=SAFETY,
    ),
    case(
        "census: a bulk POST admitted without a captured contract behind it",
        CONSTANTS,
        '    "apply_bulk": {\n        "evidence": CONTRACT_SHIPPED,',
        '    "apply_bulk_renamed": {\n        "evidence": CONTRACT_SHIPPED,',
        "test_every_post_call_site_in_the_package_targets_a_measured_endpoint",
        module=SAFETY,
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


def verdict_for(code):
    """Only pytest's exit 1 means a test FAILED.

    Exit 4 is "ERROR: not found: <nodeid>" -- no test ran at all -- and a
    harness that read any non-zero exit as RED would report a plant whose
    target had been renamed as a passing control. That is not hypothetical: it
    happened in ``reply_guard_controls.py`` on 2026-08-25, where two plants
    read RED while running nothing.
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
        found = original.count(plant["old"])
        if found != 1:
            results.append((plant["name"], "ANCHOR-MISSING (%d hits)" % found, ""))
            continue
        try:
            io.open(target, "w", encoding="utf-8", newline="").write(
                original.replace(plant["old"], plant["new"], 1)
            )
            code, tail = pytest_run(plant["test"])
        finally:
            io.open(target, "w", encoding="utf-8", newline="").write(original)
        results.append((plant["name"], verdict_for(code), tail))

    print("\n=== bulk apply: red controls ===")
    bad = 0
    for name, result, tail in results:
        ok = result == "RED"
        bad += 0 if ok else 1
        print("[%s] %-72s %s" % ("ok " if ok else "BAD", name[:72], result))
        if tail:
            print("       %s" % tail)

    failures = 0
    for module in MODULES:
        code, tail = pytest_run(module)
        failures += 0 if code == 0 else 1
        print("\nafter every plant reverted -- %s: %s" % (module, tail))
    print("%d plants, %d did not go red" % (len(results), bad))
    return 1 if bad or failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
