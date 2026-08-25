"""Prove the education write's tests can actually fail, by breaking it first.

WHY THIS EXISTS
---------------
Education was READ-ONLY in this package until 2026-08-25 -- EP_EDUCATION was a
GET used to recover the candidate id and nothing else. What changed is not the
appetite but the EVIDENCE: the PATCH was captured off the wire, from his own
signed-in browser, aborted at the router before it left the machine. When a
refusal is retired, the thing that replaces it is the gate. Every test covering
that gate is green, and green is exactly what a check that CANNOT fail looks
like.

THE RISK HERE HAS A SHAPE THE TWO SIBLING WRITES DO NOT. Skills and the
job-search profile are both made safe by one rule -- echo the read back
verbatim -- and that rule is checkable by inspection. It does not transfer to
this resource, because THE READ AND THE WRITE ARE DIFFERENT SHAPES: the GET
returns ``university`` as an expanded object and the captured request sends a
bare resource URI. So this write performs a transformation, deliberately, and
its safety rests on "exactly ONE transformation, and it is this one". That is a
much easier property to break by accident than "change nothing", and breaking
it is silent: the request succeeds and a field employers filter on quietly
holds something nobody chose.

Three more properties here are unmeasured rather than merely delicate, and each
gets a plant because an unmeasured thing that stops being defended is
indistinguishable from a measured one:

  EVERY ROW RIDES EVERY WRITE. Whether an omitted ROW is deleted by this
  resource is NOT known. The sibling that shares its save action does delete
  omitted rows; education additionally carries its own deleted_objects channel,
  which argues the other way. Sending every row is correct under both readings,
  so a plant that sends only the edited row must be caught.

  THE REMOVAL CHANNEL IS BUILT ON SOURCE, NOT ON THE WIRE. As of 2026-08-25
  it has a door -- instahyre_remove_education -- and the evidence under it is
  one class weaker than the envelope above it. removeEmptyRow settles the
  ELEMENT (a resource URI, pushed, while the row is spliced out of the list
  in the same handler); the capture caught the channel EMPTY, so the
  SERVER'S ANSWER is still unmeasured. Every removal plant below aims at
  that gap: the two halves agreeing, the last row refused, an unanswered
  channel reported rather than assumed. A plant that fills the channel on an
  EDIT must still be caught -- an edit that named a row there would be asking
  to delete the row it is editing.

  gpa AND grading_scale ARE THE SERVER'S TO SEND. They appear in no shipped
  bundle, so the page cannot have invented them -- but a row that arrives
  WITHOUT them is a world nobody measured, and writing into it is refused by
  name rather than by the downstream shape guard. The two refusals give
  different diagnoses for the same input, so the plant checks which one fired.

A NOTE ON WHAT COUNTS AS RED, because it bit a sibling. pytest exits 1 when a
test fails and 4 when a node id does not exist, and a harness that treats any
non-zero exit as RED reports a plant whose test was RENAMED as a passing
control while running nothing. Only exit 1 is RED here. That defect was found
and swept on 2026-08-25; this file inherits the fix rather than the bug.

Sibling controls: ``jsp_write_controls.py`` (the other full-object write),
``skill_removal_controls.py`` (removal by omission), ``bulk_apply_controls.py``,
``inbox_write_controls.py``, ``reply_guard_controls.py``,
``permissive_scorer_control.py``, ``presence_is_auth_control.py``.

    venv/Scripts/python.exe scripts/education_write_controls.py

Exit 0 only when every plant went red AND every module is green afterwards.
Nothing here contacts Instahyre. Strict ASCII, like every file in this package.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = REPO / "venv" / "Scripts" / "python.exe"
PW = REPO / "instahyre_server" / "profile_write.py"
CONSTANTS = REPO / "instahyre_server" / "constants.py"
SERVER = REPO / "instahyre_server" / "server.py"

EDU = "tests/test_education_write.py"
SERVER_TESTS = "tests/test_server.py"
REGISTER = "tests/test_unverified_writes.py"
MODULES = (EDU, SERVER_TESTS, REGISTER, "tests/test_profile_write.py",
           "tests/test_jsp_write.py", "tests/test_inbound_safety.py")


def case(name, path, old, new, test, module=EDU):
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
        PW,
        "        plan = self.plan_education(education_id, remove=remove, **supplied_raw)\n"
        "        if not confirm:",
        "        plan = self.plan_education(education_id, remove=remove, **supplied_raw)\n"
        "        if False:",
        "test_a_preview_sends_nothing_at_all",
    ),
    # -- GATE 2: every row rides the write ---------------------------------
    case(
        "gate 2: ONLY THE EDITED ROW IS SENT -- the others silently dropped",
        PW,
        "            else:\n"
        "                # UNTOUCHED rows still get the university collapse, because the\n"
        "                # transformation is what the resource is sent, not an edit. What\n"
        "                # they never get is a substitution.\n"
        "                objects.append(self._education_body_row(row, {}))",
        "            else:\n"
        "                continue",
        "test_every_row_the_read_returned_rides_the_write",
    ),
    case(
        "gate 2: a row that rides along is quietly edited too",
        PW,
        "                objects.append(self._education_body_row(row, {}))",
        "                objects.append(self._education_body_row(row, supplied))",
        "test_the_row_that_was_not_edited_rides_unchanged_apart_from_the_collapse",
    ),
    # -- GATE 3: the key guards --------------------------------------------
    case(
        "gate 3: the omitted-key guard removed",
        PW,
        "        missing = sorted(set(read_row) - set(body_row))",
        "        missing = []",
        "test_a_row_that_omits_a_key_is_refused__CONTROL",
    ),
    case(
        "gate 3: the added-key guard removed -- the browser's removable key gets in",
        PW,
        "        added = sorted(set(body_row) - set(read_row))",
        "        added = []",
        "test_a_row_that_adds_a_key_is_refused__CONTROL",
    ),
    # -- GATE 4: EXACTLY ONE TRANSFORMATION --------------------------------
    case(
        "gate 4: a SECOND transformation allowed through -- the guard's moved set emptied",
        PW,
        "        moved = sorted(\n"
        "            key\n"
        "            for key in read_row\n"
        "            if key not in named\n"
        '            and key != "university"\n'
        "            and body_row.get(key) != read_row.get(key)\n"
        "        )",
        "        moved = []",
        "test_a_second_transformation_is_refused__CONTROL",
    ),
    case(
        "gate 4: the university collapse itself left unchecked",
        PW,
        '        if "university" in read_row and body_row.get("university") != expected_university:',
        "        if False:",
        "test_a_university_transformed_some_other_way_is_refused__CONTROL",
    ),
    case(
        "gate 4: the collapse not performed at all -- the expanded object sent",
        PW,
        '        if "university" in body_row:\n'
        '            body_row["university"] = self._collapse_university(body_row["university"])',
        "        if False:\n"
        '            body_row["university"] = self._collapse_university(body_row["university"])',
        "test_the_university_is_collapsed_from_the_expanded_object_to_its_uri",
    ),
    case(
        "gate 4: a custom institute with no uri NULLED instead of passed through",
        PW,
        "            return uri if uri else value",
        "            return uri",
        "test_a_custom_university_with_no_uri_is_not_nulled",
    ),
    case(
        "gate 4: current_degree normalised to a uri, so both spellings agree",
        PW,
        "        body_row = dict(read_row)\n"
        '        if "university" in body_row:',
        "        body_row = dict(read_row)\n"
        '        if isinstance(body_row.get("current_degree"), dict):\n'
        '            body_row["current_degree"] = body_row["current_degree"].get(\n'
        '                "resource_uri"\n'
        "            )\n"
        '        if "university" in body_row:',
        "test_current_degree_stays_expanded_while_degree_stays_a_uri",
    ),
    # -- GATE 5: graduation_year is a STRING -------------------------------
    case(
        "gate 5: graduation_year sent as an int, which is the guess the wire refutes",
        PW,
        "            return str(year)",
        "            return year",
        "test_graduation_year_is_sent_as_a_string",
    ),
    case(
        "gate 5: the year range check removed -- 3000 accepted",
        PW,
        "            if not (C.EDUCATION_MIN_GRADUATION_YEAR <= year <= latest):",
        "            if False:",
        "test_a_graduation_year_outside_the_platforms_own_list_is_refused",
    ),
    # -- GATE 6: the removal channel stays empty ---------------------------
    case(
        "gate 6: THE REMOVAL CHANNEL FILLED ON AN EDIT, which asks to delete the "
        "row being edited",
        PW,
        "        deleted = [uri] if remove else []\n",
        '        deleted = [uri] if remove else ["/api/v1/candidate_misc/profile/education/1"]\n',
        "test_the_deleted_objects_channel_is_sent_empty",
    ),
    # -- GATE 7: refusals by name ------------------------------------------
    case(
        "gate 7: the related-field refusal removed, so the reason is lost",
        PW,
        "        related = sorted(k for k in supplied if k in C.EDUCATION_RELATED_FIELDS)",
        "        related = []",
        "test_a_related_field_is_refused_by_name_with_its_reason[university]",
    ),
    # THIS PAIR IS THE ONE THIS FILE LEARNED SOMETHING FROM. The first attempt
    # planted "specialization" into EDUCATION_WRITABLE_FIELDS and expected the
    # related-field refusal to go red. It did not, and the plant was right to
    # say so: plan_education checks the related set BEFORE the writable set, so
    # naming a taxonomy field as writable is INERT rather than dangerous. That
    # is the correct ordering and it is the same lesson JSP_WRITABLE_FIELDS
    # already carries -- but nothing asserted it, so the two registers could
    # disagree in silence and one reordering would have made the edit live.
    # The plant now aims at an assertion on the DISAGREEMENT, and a second
    # plant covers the edit that really does defeat the refusal.
    case(
        "gate 7: a taxonomy field named as writable AND as related -- the registers disagree",
        CONSTANTS,
        'EDUCATION_WRITABLE_FIELDS = ("graduation_year", "gpa", "grading_scale")',
        'EDUCATION_WRITABLE_FIELDS = ("graduation_year", "gpa", "grading_scale",\n'
        '                            "specialization")',
        "test_the_writable_set_and_the_related_set_never_overlap",
    ),
    case(
        "gate 7: a taxonomy field dropped from the related set, so its reason is lost",
        CONSTANTS,
        '    "specialization": "the specialization -- a specializations taxonomy row",\n',
        "",
        "test_a_related_field_is_refused_by_name_with_its_reason[specialization]",
    ),
    case(
        "gate 7: writing into a row the read did not describe -- the 8-key world",
        PW,
        "        absent = sorted(k for k in supplied if k not in target)",
        "        absent = []",
        "test_a_field_the_read_did_not_return_is_refused_rather_than_added",
    ),
    case(
        "gate 7: the no-op refusal removed, so a write that changes nothing goes on",
        PW,
        '                "Nothing to write: every field supplied already holds that value. "\n'
        '                "Refusing to send a request that cannot change anything -- a no-op "\n'
        '                "write is indistinguishable from a broken one, and this one would "\n'
        '                "still send every education row to achieve it."',
        '                "Between the preview and the write the row already moved to the "\n'
        '                "requested values. Nothing was sent."',
        "test_the_same_value_it_already_holds_is_refused_rather_than_written",
    ),
    case(
        "gate 7: the year compared raw, so the year already on file writes anyway",
        PW,
        '            if key == "graduation_year" and self._same_graduation_year(before, after):',
        "            if False:",
        "test_the_year_already_on_the_profile_is_recognised_across_the_type_change",
    ),
    # -- GATE 8: CSRF ------------------------------------------------------
    case(
        "gate 8: the CSRF refusal removed",
        PW,
        '        if not self.http.cookies.get("csrftoken"):\n'
        "            raise WriteRefused(\n"
        '                "Refusing to write without a CSRF token -- Django would reject the "\n'
        '                "request and the result would be ambiguous. Run instahyre_auth_status."\n'
        "            )\n"
        "\n"
        "        # The snapshot's rows ARE the rows the body is built from, so the restore",
        "        if False:\n"
        "            raise WriteRefused(\n"
        '                "Refusing to write without a CSRF token -- Django would reject the "\n'
        '                "request and the result would be ambiguous. Run instahyre_auth_status."\n'
        "            )\n"
        "\n"
        "        # The snapshot's rows ARE the rows the body is built from, so the restore",
        "test_a_confirmed_write_without_a_csrf_token_refuses_before_the_wire",
    ),
    # -- GATE 9: the snapshot ----------------------------------------------
    case(
        "gate 9: the snapshot no longer carries the education rows",
        PW,
        '        record, snap = self.take_snapshot(label="pre-education-write", education=rows)',
        '        record, snap = self.take_snapshot(label="pre-education-write")',
        "test_a_snapshot_carrying_the_education_rows_is_written_before_the_request",
    ),
    case(
        "gate 9: the body built from a FRESH read, not the one the snapshot took",
        PW,
        "        final = self._education_plan_from(\n"
        "            captured, education_id, supplied, remove=remove\n"
        "        )",
        "        final = self._education_plan_from(\n"
        "            self.read_education(), education_id, supplied, remove=remove\n"
        "        )",
        "test_the_body_is_built_from_the_snapshots_read_not_the_previews",
    ),
    case(
        "gate 9: every snapshot fetches education, so the id read is doubled",
        PW,
        "        if education is not None:\n"
        '            record["education"] = education',
        '        record["education"] = education if education is not None else self.read_education()',
        "test_other_write_paths_still_do_not_fetch_the_education_collection",
    ),
    # -- GATE 10: a 200 is not the outcome ---------------------------------
    case(
        "gate 10: the write verified against its own payload instead of a re-read",
        PW,
        "        after_rows = self.read_education()",
        '        after_rows = body["objects"]',
        "test_a_write_that_did_not_take_is_reported_unverified",
    ),
    case(
        "gate 10: the collateral report emptied -- only the named field is checked",
        PW,
        "        also_changed: dict = {}\n"
        "        for row_id, before in before_rows.items():",
        "        also_changed: dict = {}\n"
        "        for row_id, before in []:",
        "test_a_collateral_field_the_server_moved_is_reported",
    ),
    case(
        "gate 10: a row that VANISHED across the write not noticed",
        PW,
        "            now = after.get(row_id)\n"
        "            if now is None:\n"
        '                also_changed[str(row_id)] = {"before": "present", "after": "GONE"}\n'
        "                continue",
        "            now = after.get(row_id)\n"
        "            if now is None:\n"
        "                continue",
        "test_a_row_that_vanished_across_the_write_is_reported_as_gone",
    ),
    case(
        "gate 10: the year compared raw on the way back, so every success reads as failure",
        PW,
        '        if key == "graduation_year":\n'
        "            return self._same_graduation_year(wanted, got)",
        "        if False:\n"
        "            return self._same_graduation_year(wanted, got)",
        "test_a_year_read_back_as_an_integer_still_verifies",
    ),
    case(
        "gate 10: THE UNIVERSITY COMPARISON GOES BLIND -- a swapped institute passes",
        PW,
        '        if key == "university":\n'
        "            return self._collapse_university(wanted) == self._collapse_university(got)",
        '        if key == "university":\n'
        "            return True",
        "test_a_genuinely_different_institute_is_still_reported",
    ),
    # -- GATE 11: the rollback refuses what it cannot undo ------------------
    case(
        "gate 11: a snapshot with no education rows accepted for this scope",
        PW,
        "        if not isinstance(target, list) or not target:\n"
        "            raise WriteRefused(\n"
        '                "That snapshot holds no education rows.',
        "        if False:\n"
        "            raise WriteRefused(\n"
        '                "That snapshot holds no education rows.',
        "test_a_snapshot_without_education_rows_is_refused_for_this_scope",
    ),
    case(
        "gate 11: a row deleted since the snapshot restored anyway, on an unmeasured shape",
        PW,
        "        vanished = sorted(str(i) for i in snapshot_ids if i not in current_by_id)",
        "        vanished = []",
        "test_a_restore_refuses_when_a_row_has_been_deleted_since_the_snapshot",
    ),
    # -- GATE R1..R8: REMOVAL ----------------------------------------------
    #
    # Added 2026-08-25 with instahyre_remove_education. These plants carry more
    # weight than the edit plants above them, and the reason is the evidence
    # class: the envelope is WIRE, the element is SHIPPED SOURCE, and the
    # server's ANSWER to a non-empty deleted_objects has never been observed at
    # all. Nothing downstream can catch a wrong request here, because there is
    # no recorded reply to compare one against -- so the gates ARE the
    # instrument, and a gate that cannot fail is the whole failure mode.
    case(
        "gate R1: confirm ignored ON THE REMOVAL PATH -- the preview branch removed",
        PW,
        "        plan = self.plan_education(education_id, remove=remove, **supplied_raw)\n"
        "        if not confirm:",
        "        plan = self.plan_education(education_id, remove=remove, **supplied_raw)\n"
        "        if False:",
        "test_a_removal_preview_sends_nothing_and_names_the_row_that_would_go",
    ),
    case(
        "gate R2: THE LAST EDUCATION ROW REMOVABLE -- the refusal removed",
        PW,
        "        if remove and len(rows) <= 1:",
        "        if False:",
        "test_removing_the_last_education_row_is_refused_at_every_confirm_value",
    ),
    case(
        "gate R3: the push-without-splice half of the halves guard emptied",
        PW,
        "        both = sorted(\n"
        '            str(r.get("id")) for r in objects if r.get("resource_uri") in gone\n'
        "        )",
        "        both = []",
        "test_a_payload_whose_two_halves_disagree_is_refused__CONTROL",
    ),
    case(
        "gate R3: the splice-without-push half of the halves guard emptied",
        PW,
        "        missing = sorted(\n"
        '            str(r.get("id"))\n'
        "            for r in rows\n"
        '            if r.get("resource_uri") not in gone\n'
        '            and r.get("resource_uri") not in riding\n'
        "        )",
        "        missing = []",
        "test_a_payload_whose_two_halves_disagree_is_refused__CONTROL",
    ),
    case(
        "gate R3: THE SPLICE NOT PERFORMED -- the removed row pushed AND still sent",
        PW,
        "                if remove:\n"
        "                    # THE SPLICE, which is the half of a removal that is easy\n"
        "                    # to forget. removeEmptyRow pushes the uri onto the deleted\n"
        "                    # list AND splices the row out of $scope.educations, in one\n"
        "                    # handler, before one save. A payload that did only the push\n"
        "                    # would send a row the same request asks to delete.\n"
        "                    continue",
        "                if remove:\n"
        "                    pass",
        "test_a_confirmed_removal_sends_both_halves_in_one_patch",
    ),
    case(
        "gate R4: THE SURVIVORS DROPPED from a removal payload, not just the target",
        PW,
        "            else:\n"
        "                # UNTOUCHED rows still get the university collapse, because the\n"
        "                # transformation is what the resource is sent, not an edit. What\n"
        "                # they never get is a substitution.\n"
        "                objects.append(self._education_body_row(row, {}))",
        "            else:\n"
        "                continue",
        "test_every_surviving_row_rides_the_removal_verbatim",
    ),
    case(
        "gate R5: an id that is not on the profile removes the FIRST row instead",
        PW,
        "        for row in rows:\n"
        '            if row.get("id") == education_id:\n'
        "                return row\n"
        "        raise InvalidFilter(",
        "        for row in rows:\n"
        '            if row.get("id") == education_id:\n'
        "                return row\n"
        "        if rows:\n"
        "            return rows[0]\n"
        "        raise InvalidFilter(",
        "test_removing_a_row_that_is_not_on_the_profile_is_refused_and_names_it",
    ),
    case(
        "gate R6: edit-and-remove-the-same-row allowed through",
        PW,
        "        if remove and supplied:",
        "        if False and supplied:",
        "test_a_request_that_both_edits_and_removes_the_same_row_is_refused",
    ),
    case(
        "gate R7: the CSRF refusal removed, on the removal path",
        PW,
        '        if not self.http.cookies.get("csrftoken"):\n'
        "            raise WriteRefused(\n"
        '                "Refusing to write without a CSRF token -- Django would reject the "\n'
        '                "request and the result would be ambiguous. Run instahyre_auth_status."\n'
        "            )\n"
        "\n"
        "        # The snapshot's rows ARE the rows the body is built from, so the restore",
        "        if False:\n"
        "            raise WriteRefused(\n"
        '                "Refusing to write without a CSRF token -- Django would reject the "\n'
        '                "request and the result would be ambiguous. Run instahyre_auth_status."\n'
        "            )\n"
        "\n"
        "        # The snapshot's rows ARE the rows the body is built from, so the restore",
        "test_a_confirmed_removal_without_a_csrf_token_refuses_before_the_wire",
    ),
    case(
        "gate R7: the snapshot no longer carries the row that is about to go",
        PW,
        '        record, snap = self.take_snapshot(label="pre-education-write", education=rows)',
        '        record, snap = self.take_snapshot(label="pre-education-write")',
        "test_a_snapshot_carrying_the_removed_row_is_written_before_the_request",
    ),
    case(
        "gate R7: THE REMOVAL ASSUMED TO HAVE WORKED instead of re-read",
        PW,
        "        row_is_gone = (education_id not in after) if remove else None",
        "        row_is_gone = True if remove else None",
        "test_a_removal_that_did_not_take_is_reported_unverified",
    ),
    case(
        "gate R7: a successful removal reported as its own collateral damage",
        PW,
        "            if remove and row_id == education_id:\n"
        "                # The row this request removed. Its absence is the OUTCOME and\n"
        "                # is checked by name below; reporting it here would make every\n"
        "                # successful removal read as collateral damage.\n"
        "                continue\n",
        "",
        "test_a_removal_that_took_is_verified_by_re_reading",
    ),
    case(
        "gate R8: a removed row restored anyway, on a shape nobody has measured",
        PW,
        "        vanished = sorted(str(i) for i in snapshot_ids if i not in current_by_id)",
        "        vanished = []",
        "test_a_removed_row_cannot_be_restored_and_the_tool_says_so",
    ),
    case(
        "gate R8: the tool CLAIMS the removal is undoable",
        PW,
        '                    "NO, not cleanly, and this is the honest answer rather than "',
        '                    "YES -- restore_education puts it straight back. "',
        "test_a_removed_row_cannot_be_restored_and_the_tool_says_so",
    ),
    # -- the register and the surface --------------------------------------
    case(
        "census: the captured contract renamed out from under the write",
        CONSTANTS,
        '    "education": {\n        "evidence": CONTRACT_WIRE,',
        '    "education_renamed": {\n        "evidence": CONTRACT_WIRE,',
        "TestTheCapturedContracts::test_it_names_the_eleven_that_were_captured",
        module=REGISTER,
    ),
    case(
        "surface: the tool renamed, so the MCP surface changes without an edit here",
        SERVER,
        "def instahyre_update_education(",
        "def instahyre_update_education_v2(",
        "test_the_registered_tool_names_are_the_expected_set",
        module=SERVER_TESTS,
    ),
]


def pytest_run(nodeid):
    """Run one node id in a subprocess that CANNOT reuse another plant's bytecode.

    THE SECOND WAY THIS HARNESS PATTERN LIES, found on 2026-08-25 while building
    this file, and it is subtler than the exit-4 defect the module docstring
    already records.

    CPython decides a cached ``.pyc`` is fresh by comparing the source's SIZE
    and its mtime IN WHOLE SECONDS. Two plants in the same file that happen to
    change it by the SAME NUMBER OF BYTES, run inside the same wall-clock
    second, therefore produce byte-identical (size, mtime) pairs -- and the
    second run silently executes the FIRST plant's bytecode.

    That is not hypothetical here. ``missing = sorted(set(read_row) -
    set(body_row))`` -> ``missing = []`` and ``added = sorted(set(body_row) -
    set(read_row))`` -> ``added = []`` remove exactly 34 bytes each. Reproduced
    deterministically by stamping both writes with one os.utime value: the
    omitted-key plant went RED and the added-key plant reported "1 passed in
    0.08s" -- a GREEN verdict on a guard that is perfectly intact, printed as
    "THIS CHECK CANNOT FAIL". The failure is a RACE on plant ordering and run
    duration, so it appears and disappears between runs, which is worse than a
    deterministic bug.

    The fix is structural rather than a delay: each run gets its OWN bytecode
    cache directory, so there is no shared cache to hit. In-tree ``__pycache__``
    directories are ignored entirely while PYTHONPYCACHEPREFIX is set, and the
    temp dir is thrown away afterwards, so the repo is never touched.

    EVERY SIBLING CONTROL SCRIPT IN THIS DIRECTORY SHARES THE ORIGINAL DEFECT.
    They are not edited from here -- that is not this file's slice -- but the
    mechanism is recorded so the next person to see an inexplicable GREEN has
    somewhere to start.
    """
    cache = tempfile.mkdtemp(prefix="edu-controls-pyc-")
    env = dict(os.environ, PYTHONPYCACHEPREFIX=cache)
    try:
        proc = subprocess.run(
            [str(PY), "-m", "pytest", nodeid, "-q", "--no-header"],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            env=env,
        )
    finally:
        shutil.rmtree(cache, ignore_errors=True)
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

    print("\n=== education write: red controls ===")
    bad = 0
    for name, result, tail in results:
        ok = result == "RED"
        bad += 0 if ok else 1
        print("[%s] %-78s %s" % ("ok " if ok else "BAD", name[:78], result))
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
