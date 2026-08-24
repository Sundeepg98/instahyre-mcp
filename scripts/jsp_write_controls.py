"""Prove the job-search-profile write's tests can actually fail, by breaking it.

WHY THIS EXISTS
---------------
``update_job_search_profile`` PUTs the WHOLE object to a full-replacement
endpoint. A PATCH that forgets a field changes nothing; this PUT DELETES it --
with a 200, with no warning, and with no withdraw anywhere in the product. The
profile does not raise; it simply stops appearing in the filtered result sets
it used to appear in, and the only surviving record of what it held is a
snapshot file on this machine.

That is a failure nobody sees, on the live account of somebody looking for
work, which makes the tests guarding it exactly the kind that can pass without
testing anything. Four claims are being defended and they fail in different
directions:

  THE BODY IS THE OBJECT. Every key the read returned rides the write, no key
  the read did not return joins it, no server-owned key moves, and an untouched
  field goes back in the server's own type. A break here is silent by
  construction -- the request succeeds and something is simply gone.

  THE GATE HOLDS. confirm=False sends nothing, an unsigned write is refused
  before the wire, a notice period outside the platform's own bands is refused
  rather than clamped, an empty location list is refused outright, and a field
  this server does not write is refused by name.

  THE WRITE ADDRESSES THE RIGHT ROW AT THE RIGHT INSTANT. The URL binds the
  JSP's own id, which is a different number from the candidate's; the body is
  built from the read the SNAPSHOT took, not an earlier one; and the verifying
  read cannot be served out of the 15-minute profile cache. All three fail by
  reporting success.

  THE ROLLBACK REFUSES WHAT IT CANNOT UNDO. A snapshot taken before the jsp was
  captured holds no jsp, and restoring from it would delete every key it could
  not supply.

Each plant below breaks ONE of those and requires the specific test that claims
to cover it to go red. Then the file is restored byte-for-byte.

Sibling controls: ``reply_guard_controls.py`` (the one write that reaches
another person), ``skill_removal_controls.py`` (removal by omission),
``permissive_scorer_control.py``, ``presence_is_auth_control.py``.

    venv/Scripts/python.exe scripts/jsp_write_controls.py

Exit 0 only when every plant went red AND the module is green afterwards.
Nothing here contacts Instahyre. Strict ASCII, like every file in this package.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = REPO / "venv" / "Scripts" / "python.exe"
PW = REPO / "instahyre_server" / "profile_write.py"
CONSTANTS = REPO / "instahyre_server" / "constants.py"
MODULE = "tests/test_jsp_write.py"


def case(name, path, old, new, test):
    return {"name": name, "path": path, "old": old, "new": new, "test": MODULE + "::" + test}


PLANTS = [
    # -- the body is the object ---------------------------------------------
    case(
        "the omission guard neutered",
        PW,
        "        if missing:",
        "        if False:",
        "test_a_body_that_omits_a_key_is_refused__CONTROL",
    ),
    case(
        "the extra-key half of the omission guard neutered",
        PW,
        "        if added:",
        "        if False:",
        "test_a_body_with_an_extra_key_is_refused",
    ),
    case(
        "the server-owned guard neutered",
        PW,
        "        if moved:",
        "        if False:",
        "test_a_changed_server_owned_key_is_refused",
    ),
    # -- the gate -----------------------------------------------------------
    case(
        "the confirm gate falls through to the write",
        PW,
        "        plan = self.plan_job_search_profile(**changes)\n        if not confirm:",
        "        plan = self.plan_job_search_profile(**changes)\n        if False:",
        "test_confirm_false_sends_nothing",
    ),
    case(
        "the CSRF refusal removed",
        PW,
        '                "still replace the whole object to achieve it."\n'
        "            )\n"
        "\n"
        '        if not self.http.cookies.get("csrftoken"):',
        '                "still replace the whole object to achieve it."\n'
        "            )\n"
        "\n"
        "        if False:",
        "test_the_write_refuses_without_a_csrf_token",
    ),
    # -- the address, the instant, and the read-back ------------------------
    case(
        "the body built from the plan's read rather than the snapshot's",
        PW,
        "        final = self._plan_from(before, supplied)",
        "        final = self._plan_from(self.read_jsp(), supplied)",
        "test_the_body_is_built_from_the_snapshots_read_not_an_earlier_one",
    ),
    case(
        "the URL addresses the candidate instead of the JSP",
        PW,
        '        self.http.put(C.EP_JSP.format(jsp_id=before["id"]), json_body=body)',
        "        self.http.put(C.EP_JSP.format(jsp_id=cid), json_body=body)",
        "test_the_put_url_uses_the_jsp_id_not_the_candidate_id",
    ),
    case(
        "the verifying read allowed to come from the cache",
        PW,
        '        self.store.put("profile", str(cid), None, -1)\n'
        "        after = self.read_jsp()",
        "        after = self.read_jsp()",
        "test_the_profile_cache_is_expired_before_the_verifying_read",
    ),
    # -- what rides the write, and what may not -----------------------------
    case(
        "the browser's career-break NULLing reproduced",
        PW,
        "        body = dict(jsp)\n        body.update(validated)",
        "        body = dict(jsp)\n"
        "        body.update(validated)\n"
        '        body["career_break_reason"] = None',
        "test_the_career_break_nulling_the_browser_does_is_not_reproduced",
    ),
    case(
        "notice_period accepts any integer, not just a published band",
        PW,
        "            if value not in C.NOTICE_PERIOD_RANGES:",
        "            if False:",
        "test_notice_period_rejects_a_day_count",
    ),
    case(
        "an untouched salary retyped to float, as the browser does",
        PW,
        "        self._guard_no_key_dropped(jsp, body)",
        '        body["current_salary"] = float(body["current_salary"])\n'
        "        self._guard_no_key_dropped(jsp, body)",
        "test_untouched_keys_go_back_in_the_servers_own_types",
    ),
    case(
        "the writable-field list admits a server-owned key",
        CONSTANTS,
        'JSP_WRITABLE_FIELDS = (\n    "notice_period",',
        'JSP_WRITABLE_FIELDS = (\n    "is_immediate_joinee",\n    "notice_period",',
        "test_no_server_owned_key_is_also_listed_writable",
    ),
    case(
        "the empty-location refusal removed",
        PW,
        "            if not value:\n                # The empty-list refusal",
        "            if False:\n                # The empty-list refusal",
        "test_an_empty_location_list_is_refused",
    ),
    # -- the rollback -------------------------------------------------------
    case(
        "a snapshot with no jsp restored anyway",
        PW,
        "        if not isinstance(target, dict) or not target:",
        "        if False:",
        "test_a_snapshot_without_a_jsp_cannot_restore_one",
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
        results.append(
            (plant["name"], "RED" if code != 0 else "GREEN -- THIS CHECK CANNOT FAIL", tail)
        )

    print("\n=== job-search profile write: red controls ===")
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
