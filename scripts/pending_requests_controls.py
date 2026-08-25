"""Prove every leaderboard-cluster gate can actually fail, by breaking each first.

WHY THIS EXISTS
---------------
The leaderboard cluster is the only channel on this server where INSTAHYRE ASKS
HIM something, and one of its two questions is terminal: were you hired at this
company. Every test covering it is green, and green is exactly what a check
that CANNOT fail looks like.

The risk here has a shape nothing else in this package has. All three routes in
the cluster answered EMPTY on 2026-08-25 -- {"data": []}, {"objects": [], ...}
and {"show_modal": false}, all 200, from his own signed-in session -- so no
assertion anywhere has ever met a populated payload, and every fixture is
hand-built. Worse, the emptiness is load-bearing: because both writes validate
their id against a LIVE re-read, an empty channel is what makes them refuse. A
gate that quietly stopped revalidating would go on passing every "it refuses
today" test for the wrong reason -- refusing because the fixture is empty rather
than because the gate fired -- right up until the day Instahyre offers a real
hire check and the tool answers it unprompted.

So each gate is broken here and required to be noticed. The claims defended,
one plant each:

  NOTHING PENDING IS A POSITIVE RESULT, never an error and never an empty dict.
  A FAILED READ STILL FAILS -- the mirror, and the more dangerous direction: a
  lapsed session must never arrive dressed as "nothing pending".
  THE READ COVERS ALL THREE ROUTES, and carries the candidate id the site's own
  reader carries.
  IT NAMES THE COMPANY rather than counting checks, and does not echo his own
  photograph back at him.
  THE CONFIRM GATE HOLDS on both writes. Nothing is requested without it.
  THE ID COMES FROM A LIVE RE-READ, not from a remembered list, and a fabricated
  one cannot be submitted.
  BOTH HALVES OF THE REQUEST GO. This is the one that must not regress quietly:
  submit_rating declares Angular action params, so its three fields ride the
  QUERY STRING as well as the JSON body, and a reproduction of either half alone
  is a request the browser never makes. Two plants, one per half.
  THE SITE'S OWN RATING RULES ARE REPRODUCED -- no rating means no submission,
  and no second ask-later -- and the scale is 1 to 5.
  CHOICE IS NOT GIVEN A MEANING NOBODY MEASURED, and a boolean is not widened
  into one.
  CSRF IS REQUIRED, on both writes.
  THE OUTCOME IS READ BACK FROM STATE, not from a response shape nobody has seen.
  THE DOOR IS A NAMED ALLOWLIST OF TWO and the collection url is NOT on it --
  which is the only thing keeping add_joining_date, an action with no caller in
  any captured bundle, unreachable. Two plants: one adds the url, one softens
  the guard into a prefix rule that would admit it.
  THE READ TOOL STAYS A READ, and the write tools default confirm to False.
  THE EVIDENCE CLASS IS NOT INFLATED. SHIPPED is not WIRE.

A NOTE ON WHAT COUNTS AS RED, because it bit a sibling. pytest exits 1 when a
test fails and 4 when a node id does not exist, and a harness that treats any
non-zero exit as RED reports a plant whose test was RENAMED as a passing control
while running nothing. Only exit 1 is RED here.

A SECOND NOTE, specific to this file. Several plants let a write escape into a
``NoWriteHTTP`` double, which raises a ``BaseException`` on purpose so that no
``except Exception`` can mute it. That was measured rather than assumed before
these plants were written: pytest reports such an escape as a FAILED test and
exits 1, so those plants read RED like any other.

Sibling controls: ``bulk_apply_controls.py``, ``inbox_write_controls.py``,
``reply_guard_controls.py``, ``jsp_write_controls.py``,
``skill_removal_controls.py``, ``permissive_scorer_control.py``,
``presence_is_auth_control.py``.

    venv/Scripts/python.exe scripts/pending_requests_controls.py

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

PENDING = "tests/test_pending_requests.py"
MODULES = (
    PENDING,
    "tests/test_inbound_safety.py",
    "tests/test_unverified_writes.py",
    "tests/test_writes.py",
    "tests/test_server.py",
)


def case(name, path, old, new, test, module=PENDING):
    return {
        "name": name,
        "path": path,
        "old": old,
        "new": new,
        "test": module + "::" + test,
    }


PLANTS = [
    # -- THE READ: an empty channel is a RESULT ---------------------------
    case(
        "read 1: an empty channel raises instead of answering",
        WRITES,
        "        checks = self._live_hire_checks()\n"
        "        queue = self._live_hire_queue()",
        '        checks = self._live_hire_checks()\n'
        '        if not checks["records"]:\n'
        '            raise NothingToDo("nothing pending")\n'
        "        queue = self._live_hire_queue()",
        "test_the_empty_read_raises_nothing",
    ),
    case(
        "read 2: an empty channel answers with a bare empty dict",
        WRITES,
        "        if pending_count:\n"
        '            result["summary"] = (',
        "        if not pending_count:\n"
        "            return {}\n"
        "        if pending_count:\n"
        '            result["summary"] = (',
        "test_the_empty_result_is_not_an_empty_dict",
    ),
    case(
        "read 3: THE MIRROR -- a failed read swallowed into 'nothing pending'",
        WRITES,
        "        payload = self.http.get(C.EP_CANDIDATE_RATING_INFO)",
        "        try:\n"
        "            payload = self.http.get(C.EP_CANDIDATE_RATING_INFO)\n"
        "        except Exception:\n"
        "            payload = {}",
        "test_a_read_that_fails_raises_instead_of_reporting_nothing_pending"
        "[/leaderboard/candidate_rating/get_opportunity_info]",
    ),
    case(
        "read 4: one of the three routes silently dropped",
        WRITES,
        "        queue = self._live_hire_queue()\n"
        "        offer = self._live_rating_offer()",
        "        queue = {\n"
        '            "records": [],\n'
        '            "records_read": 0,\n'
        '            "total_reported": None,\n'
        '            "complete": True,\n'
        "        }\n"
        "        offer = self._live_rating_offer()",
        "test_the_read_covers_all_three_routes_in_one_call",
    ),
    case(
        "read 5: the candidate id dropped from the modal query",
        WRITES,
        '            params={"id": self.inbound.candidate_id()},',
        "            params=None,",
        "test_the_modal_read_carries_the_candidate_id_in_the_query_string",
    ),
    case(
        "read 6: the company name collapses to a count",
        WRITES,
        '        "company": row.get("company_name"),',
        '        "company": None,',
        "test_a_populated_channel_names_the_hire_check_rather_than_counting_it",
    ),
    case(
        "read 7: his own photograph echoed back into the result",
        WRITES,
        '        "has_candidate_image": bool(row.get("can_image")),',
        '        "has_candidate_image": row.get("can_image"),',
        "test_the_read_does_not_echo_the_two_image_urls",
    ),
    # -- WRITE 1: answering a hire check ----------------------------------
    case(
        "hire 1: confirm ignored -- the preview branch removed",
        WRITES,
        "        if not confirm:\n"
        '            preview["confirmed"] = False\n'
        '            preview["next"] = (\n'
        '                "NOTHING HAS BEEN SENT. Re-run with confirm=True to answer hire check "',
        "        if False:\n"
        '            preview["confirmed"] = False\n'
        '            preview["next"] = (\n'
        '                "NOTHING HAS BEEN SENT. Re-run with confirm=True to answer hire check "',
        "test_answering_without_confirm_issues_no_write_at_all",
    ),
    case(
        "hire 2: THE LIVE-READ GATE REMOVED -- a fabricated id becomes sendable",
        WRITES,
        "        if wanted not in offered:",
        '        offered.setdefault(wanted, {"hired_id": wanted})\n'
        "        if False:",
        "test_the_refusal_says_the_channel_is_empty_when_it_is",
    ),
    case(
        "hire 3: the modal remembered instead of re-read",
        WRITES,
        "        checks = self._live_hire_checks()\n"
        "        offered = {",
        '        if not hasattr(self, "_planted_cache"):\n'
        "            self._planted_cache = self._live_hire_checks()\n"
        "        checks = self._planted_cache\n"
        "        offered = {",
        "test_the_modal_is_re_read_for_the_check_rather_than_remembered",
    ),
    case(
        "hire 4: the id dropped from the query string",
        WRITES,
        "            C.EP_VERIFY_HIRED_SUBMIT_RESPONSE,\n"
        "            params=query,\n"
        "            json_body=body,",
        "            C.EP_VERIFY_HIRED_SUBMIT_RESPONSE,\n"
        "            json_body=body,",
        "test_a_confirmed_answer_sends_the_captured_body_and_the_captured_query",
    ),
    case(
        "hire 5: CSRF no longer required",
        WRITES,
        '        self._require_csrf("answer a hire check")',
        "        pass",
        "test_answering_without_a_csrf_token_refuses",
    ),
    case(
        "hire 6: choice given a meaning nobody measured",
        WRITES,
        '            "choice_meaning": C.HIRE_CHOICE_MEANINGS_ARE_UNMEASURED,',
        '            "choice_meaning": "1 means hired and 2 means not hired.",',
        "test_the_preview_refuses_to_give_choice_a_meaning_it_has_not_measured",
    ),
    case(
        "hire 7: a boolean choice widened into choice=1",
        WRITES,
        "    if isinstance(choice, bool) or not isinstance(choice, int):",
        "    if not isinstance(choice, int):",
        "test_a_boolean_choice_is_refused_rather_than_widened_to_one",
    ),
    case(
        "hire 8: the outcome trusted from the response instead of read from state",
        WRITES,
        '            "verification": self._verify_hire_check(wanted),',
        '            "verification": {"ok": True, "how": "the response said so",\n'
        '                             "still_offered": False},',
        "test_a_check_still_offered_after_the_answer_warns_and_does_not_advise_resending",
    ),
    # -- WRITE 2: rating an opportunity -----------------------------------
    case(
        "rating 1: confirm ignored -- the preview branch removed",
        WRITES,
        "        if not confirm:\n"
        '            preview["confirmed"] = False\n'
        '            preview["next"] = (\n'
        '                "NOTHING HAS BEEN SENT. Re-run with confirm=True to %s."',
        "        if False:\n"
        '            preview["confirmed"] = False\n'
        '            preview["next"] = (\n'
        '                "NOTHING HAS BEEN SENT. Re-run with confirm=True to %s."',
        "test_rating_without_confirm_issues_no_write_at_all",
    ),
    case(
        "rating 2: THE QUERY-STRING HALF DROPPED -- half the captured request",
        WRITES,
        "            C.EP_CANDIDATE_RATING_SUBMIT,\n"
        "            params=query,\n"
        "            json_body=body,",
        "            C.EP_CANDIDATE_RATING_SUBMIT,\n"
        "            json_body=body,",
        "test_the_three_fields_ride_the_query_string_AND_the_body",
    ),
    case(
        "rating 3: THE BODY HALF DROPPED -- the same mistake, mirrored",
        WRITES,
        "        response = self.http.post(\n"
        "            C.EP_CANDIDATE_RATING_SUBMIT,\n"
        "            params=query,\n"
        "            json_body=body,",
        "        response = self.http.post(\n"
        "            C.EP_CANDIDATE_RATING_SUBMIT,\n"
        "            params=query,\n"
        "            json_body=None,",
        "test_the_ask_later_branch_sends_a_null_rating_in_the_body_and_drops_it_from_the_query",
    ),
    case(
        "rating 4: THE LIVE-READ GATE REMOVED -- any uri becomes ratable",
        WRITES,
        '        if wanted != offer["resource_uri"]:',
        "        if False:",
        "test_a_fabricated_rating_uri_is_refused_by_name",
    ),
    case(
        "rating 5: the nothing-offered refusal loses its reason",
        WRITES,
        '        if not offer["show_modal"] or not offer["resource_uri"]:',
        "        if False:",
        "test_a_rating_is_refused_outright_when_nothing_is_offered",
    ),
    case(
        "rating 6: THEIR rule dropped -- a rating submitted with no rating",
        WRITES,
        "    if rating is None:\n"
        "        if not ask_later:",
        "    if rating is None:\n"
        "        if False:",
        "test_a_rating_submission_with_no_rating_is_refused__THEIR_RULE",
    ),
    case(
        "rating 7: THEIR rule dropped -- a second ask-later allowed",
        WRITES,
        '        if defer and offer["asked_before"]:',
        "        if False:",
        "test_a_second_ask_later_is_refused__THEIR_RULE",
    ),
    case(
        "rating 8: the 1-to-5 scale stops being enforced",
        WRITES,
        "    if not C.RATING_SCALE_MIN <= rating <= C.RATING_SCALE_MAX:",
        "    if False:",
        "test_a_rating_outside_one_to_five_is_refused[6]",
    ),
    case(
        "rating 9: CSRF no longer required",
        WRITES,
        '        self._require_csrf("submit an opportunity rating")',
        "        pass",
        "test_rating_without_a_csrf_token_refuses",
    ),
    # -- THE DOOR ----------------------------------------------------------
    case(
        "door 1: the collection url added to the allowlist, unblocking add_joining_date",
        CONSTANTS,
        "SENDABLE_LEADERBOARD_PATHS = frozenset(\n"
        "    {EP_VERIFY_HIRED_SUBMIT_RESPONSE, EP_CANDIDATE_RATING_SUBMIT}\n"
        ")",
        "SENDABLE_LEADERBOARD_PATHS = frozenset(\n"
        "    {EP_VERIFY_HIRED_SUBMIT_RESPONSE, EP_CANDIDATE_RATING_SUBMIT, EP_VERIFY_HIRED}\n"
        ")",
        "test_the_allowlist_is_exactly_the_two_named_paths",
    ),
    case(
        "door 2: the allowlist softened into a prefix rule that admits a family",
        WRITES,
        "    if path not in C.SENDABLE_LEADERBOARD_PATHS:",
        '    if not path.startswith("/leaderboard/"):',
        "test_the_collection_url_is_not_sendable_which_is_what_blocks_add_joining_date",
    ),
    # -- THE MCP SURFACE ---------------------------------------------------
    case(
        "surface 1: the read tool grows a confirm, teaching that reading is a write",
        SERVER,
        "def instahyre_pending_requests() -> dict:",
        "def instahyre_pending_requests(confirm: bool = False) -> dict:",
        "test_the_read_tool_has_no_confirm_and_takes_no_arguments",
    ),
    case(
        "surface 2: the hire-check tool defaults confirm to True",
        SERVER,
        "def instahyre_answer_hire_check(\n"
        "    hired_id: str,\n"
        "    choice: int,\n"
        "    confirm: bool = False,\n"
        ") -> dict:",
        "def instahyre_answer_hire_check(\n"
        "    hired_id: str,\n"
        "    choice: int,\n"
        "    confirm: bool = True,\n"
        ") -> dict:",
        "test_both_write_tools_default_confirm_to_false[instahyre_answer_hire_check]",
    ),
    case(
        "surface 3: SHIPPED evidence inflated to WIRE",
        CONSTANTS,
        '    "hire_check": {\n        "evidence": CONTRACT_SHIPPED,',
        '    "hire_check": {\n        "evidence": CONTRACT_WIRE,',
        "test_the_write_tools_carry_their_evidence_class_into_the_preview",
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

    print("\n=== leaderboard cluster: red controls ===")
    bad = 0
    for name, result, tail in results:
        ok = result == "RED"
        bad += 0 if ok else 1
        print("[%s] %-76s %s" % ("ok " if ok else "BAD", name[:76], result))
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
