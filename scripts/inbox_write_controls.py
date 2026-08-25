"""Prove the three new inbox-write guards can actually fail, by breaking them first.

WHY THIS EXISTS
---------------
Starring, marking one thread read, and clearing unread across the whole inbox
were built on 2026-08-25. Every test that covers them is green, and green is
exactly what a check that CANNOT fail looks like. That risk is unusually sharp
on this surface for a reason nothing else in the package shares: **his inbox
holds zero conversations**, so none of the three has ever run against live
data, and every assertion about them runs on hand-built fixtures. A guard on a
surface nobody can exercise is the guard most likely to have quietly stopped
working, so each one is broken here and required to be noticed.

Four claims are defended, and they fail in different directions:

  THE ALLOWLIST GREW BY NAMED ENTRY, NOT BY RULE. Four constants, not a prefix
  and not a regex. A relaxation is silent -- nothing breaks, the server just
  gains reach nobody granted it.

  THE CONFIRM GATE HOLDS ON ALL THREE, including on a GET. mark_all_read is
  the one request in this package whose method says nothing about what it does,
  so "no write was issued" has to be measured on the PATH.

  THE BODIES ARE THE CAPTURED ONES. star_conv rather than starred, no can_user
  on a candidate session, and a conversation named by RESOURCE URI rather than
  by id.

  THE PLATFORM'S OWN GATE IS REPRODUCED. Instahyre refuses to issue
  mark_all_read when the unread count is zero; so does this.

Each plant below breaks ONE of those and requires the specific test that claims
to cover it to go red. Then the file is restored byte-for-byte.

A NOTE ON WHAT COUNTS AS RED, because it bit its sibling. pytest exits 1 when a
test fails and 4 when a node id does not exist, and a harness that treats any
non-zero exit as RED reports a plant whose test was RENAMED as a passing
control while running nothing. Only exit 1 is RED here.

Sibling controls: ``reply_guard_controls.py`` (the reply tool),
``skill_removal_controls.py``, ``permissive_scorer_control.py``,
``presence_is_auth_control.py``.

    venv/Scripts/python.exe scripts/inbox_write_controls.py

Exit 0 only when every plant went red AND both modules are green afterwards.
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

INBOX = "tests/test_inbox_writes.py"
CARVE_OUT = "tests/test_writes.py"
MODULES = (INBOX, CARVE_OUT)


def case(name, path, old, new, test, module=INBOX):
    return {
        "name": name,
        "path": path,
        "old": old,
        "new": new,
        "test": module + "::" + test,
    }


PLANTS = [
    # -- the allowlist grew by NAMED ENTRY, never by rule -------------------
    case(
        "the allowlist replaced by a prefix rule over the message resource",
        WRITES,
        "    if path not in C.SENDABLE_INBOX_PATHS:",
        "    if not path.startswith('/resume_modal/emails/message'):",
        "test_the_send_guard_is_an_allowlist_not_a_blocklist",
        module=CARVE_OUT,
    ),
    case(
        "a fifth path admitted without a captured contract",
        CONSTANTS,
        "        EP_MARK_ALL_READ,\n    }\n)",
        "        EP_MARK_ALL_READ,\n"
        "        '/resume_modal/emails/message/get_candidates_star_status',\n"
        "    }\n)",
        "test_the_sendable_allowlist_holds_exactly_the_four_named_paths",
        module=CARVE_OUT,
    ),
    case(
        "the read tier quietly stops refusing what the write tier now sends",
        CONSTANTS,
        '    "star_conversation",\n    "toggle_message_read",',
        '    "toggle_message_read",',
        "test_the_read_guard_still_refuses_every_one_of_these_paths",
    ),
    # -- 1. starring --------------------------------------------------------
    case(
        "star: confirm ignored -- the preview branch removed",
        WRITES,
        '        if not confirm:\n'
        '            preview["confirmed"] = False\n'
        '            preview["next"] = (\n'
        '                "NOTHING HAS BEEN SENT. Re-run with confirm=True to set star_conv=%r on "',
        '        if False:\n'
        '            preview["confirmed"] = False\n'
        '            preview["next"] = (\n'
        '                "NOTHING HAS BEEN SENT. Re-run with confirm=True to set star_conv=%r on "',
        "test_a_star_without_confirm_issues_nothing_at_all",
    ),
    case(
        "star: the body key spelled 'starred', the way a memory of it would be",
        WRITES,
        '        body = {"star_conv": starred, "job_id": job_id}',
        '        body = {"starred": starred, "job_id": job_id}',
        "test_the_star_body_never_carries_a_key_called_starred",
    ),
    case(
        "star: can_user added, as the RECRUITER branch would",
        WRITES,
        '        body = {"star_conv": starred, "job_id": job_id}',
        '        body = {"star_conv": starred, "job_id": job_id,\n'
        '                "can_user": "/api/v1/candidate_misc/profile/limited_candidate/1"}',
        "test_the_star_body_is_the_candidate_branch_key_for_key",
    ),
    case(
        "star: the CSRF refusal removed",
        WRITES,
        '        self._require_csrf("change a star")',
        "        pass",
        "test_a_confirmed_star_without_a_csrf_token_refuses_before_sending",
    ),
    case(
        "star: a 200 treated as the outcome -- no read-back of the response",
        WRITES,
        '            "verified": observed == starred,',
        '            "verified": True,',
        "test_a_star_whose_response_disagrees_is_reported_unverified_not_successful",
    ),
    # -- 2. marking one conversation read -----------------------------------
    case(
        "mark-read: confirm ignored -- the preview branch removed",
        WRITES,
        '        if not confirm:\n'
        '            preview["confirmed"] = False\n'
        '            preview["next"] = (\n'
        '                "NOTHING HAS BEEN SENT. Re-run with confirm=True to send exactly the "',
        '        if False:\n'
        '            preview["confirmed"] = False\n'
        '            preview["next"] = (\n'
        '                "NOTHING HAS BEEN SENT. Re-run with confirm=True to send exactly the "',
        "test_a_mark_read_without_confirm_issues_nothing_at_all",
    ),
    case(
        "mark-read: the conversation named by ID, not by the server's resource URI",
        WRITES,
        '        body = {"conversation": resource_uri, "mark_unread": mark_unread}',
        '        body = {"conversation": int(conv_id), "mark_unread": mark_unread}',
        "test_the_toggle_body_names_the_conversation_by_resource_uri_not_by_id",
    ),
    case(
        "mark-read: a resource URI ASSEMBLED from the id rather than read off the record",
        WRITES,
        '        resource_uri = record.get("resource_uri")',
        '        resource_uri = "/api/v1/inbox_page/candidate_conversation/%s" % conv_id',
        "test_a_record_with_no_resource_uri_is_refused_rather_than_having_one_built",
    ),
    case(
        "mark-read: the two values presented as equally evidenced",
        WRITES,
        '                if mark_unread\n'
        '                else "mark_unread=false has NO shipped caller.',
        '                if True\n'
        '                else "mark_unread=false has NO shipped caller.',
        "test_the_preview_says_which_of_the_two_values_has_a_shipped_caller",
    ),
    case(
        "mark-read: the CSRF refusal removed",
        WRITES,
        '        self._require_csrf("change a read flag")',
        "        pass",
        "test_a_confirmed_toggle_without_a_csrf_token_refuses_before_sending",
    ),
    case(
        "mark-read: the read-back replaced by trust in the status code",
        WRITES,
        "        verification = self._verify_read_flag(conv_id, mark_unread)",
        '        verification = {"ok": True, "how": "the server answered 200"}',
        "test_a_toggle_whose_re_read_still_shows_the_old_state_is_reported_unverified",
    ),
    # -- 3. the GET that mutates --------------------------------------------
    case(
        "sweep: confirm ignored -- the bulk GET fires on a preview call",
        WRITES,
        '        if not confirm:\n'
        '            preview["confirmed"] = False\n'
        '            preview["next"] = (\n'
        '                "NOTHING HAS BEEN SENT. Read \'would_affect\' above',
        '        if False:\n'
        '            preview["confirmed"] = False\n'
        '            preview["next"] = (\n'
        '                "NOTHING HAS BEEN SENT. Read \'would_affect\' above',
        "test_mark_all_read_without_confirm_issues_no_request_to_the_gated_path",
    ),
    case(
        "sweep: page_loaded_at in Python's spelling instead of JavaScript's",
        WRITES,
        '    return now.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (now.microsecond // 1000)',
        "    return now.isoformat()",
        "test_page_loaded_at_is_spelled_the_way_javascript_spells_it",
    ),
    case(
        "sweep: an invented filter added to the query, narrowing what nobody chose",
        WRITES,
        '        params = {"page_loaded_at": page_loaded_at}',
        '        params = {"page_loaded_at": page_loaded_at, "unread": True}',
        "test_the_sweep_sends_page_loaded_at_and_nothing_else",
    ),
    case(
        "sweep: the platform's own zero-unread gate removed",
        WRITES,
        "        if counted == 0:",
        "        if False:",
        "test_the_sweep_refuses_when_the_server_reports_nothing_unread",
    ),
    case(
        "sweep: a contradiction resolved in favour of proceeding",
        WRITES,
        "        counted = unread_total if isinstance(unread_total, int) else None",
        "        counted = unread_total if unread_total else None",
        "test_a_zero_count_beside_an_unread_row_refuses_rather_than_picking_a_side",
    ),
    case(
        "sweep: the affected threads counted instead of named",
        WRITES,
        '                    "conv_id": r.get("id"),',
        '                    "conv_id": None,',
        "test_the_preview_names_every_thread_that_would_lose_its_unread_flag",
    ),
    case(
        "sweep: the CSRF refusal removed",
        WRITES,
        '        self._require_csrf("clear the whole inbox\'s unread state")',
        "        pass",
        "test_a_confirmed_sweep_without_a_csrf_token_refuses_before_requesting",
    ),
    case(
        "sweep: an unconfirmed sweep reported as a clean success",
        WRITES,
        "        verification = self._verify_mark_all_read(response)",
        '        verification = {"ok": True, "how": "the server answered 200",\n'
        '                        "unread_after": 0}',
        "test_a_sweep_that_cannot_be_confirmed_says_so_and_says_do_not_re_run",
    ),
    # -- 4. the server's own account of itself ------------------------------
    case(
        "the server goes on claiming these three are not built",
        SERVER,
        '            "inbox_writes": (\n'
        '                "ALL FOUR MEASURED INBOX WRITES ARE NOW REACHABLE',
        '            "inbox_writes": (\n'
        '                "STARRING AND MARKING READ ARE MEASURED AND STILL NOT BUILT',
        "test_the_reply_tool_is_declared_irreversible_by_the_server_itself",
        module=CARVE_OUT,
    ),
    case(
        "a docstring stops admitting the tool never ran live",
        SERVER,
        "    NEVER RUN AGAINST LIVE DATA. His inbox holds zero conversations (measured\n"
        "    2026-08-23, authenticated, 200), so this has been exercised against\n"
        "    fixtures only.",
        "    This has been exercised end to end.",
        "test_every_new_tool_admits_in_its_docstring_that_it_never_ran_live",
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
    target had been renamed as a passing control. That is not hypothetical:
    it happened in ``reply_guard_controls.py`` on 2026-08-25, where two plants
    read RED while running nothing, and it is the reason this function exists
    instead of an inline ``code != 0``.
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

    print("\n=== inbox writes: red controls ===")
    bad = 0
    for name, result, tail in results:
        ok = result == "RED"
        bad += 0 if ok else 1
        print("[%s] %-70s %s" % ("ok " if ok else "BAD", name[:70], result))
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
