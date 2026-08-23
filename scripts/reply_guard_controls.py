"""Prove the reply guards can actually fail, by breaking them first.

WHY THIS EXISTS
---------------
Replying is the only inbox write this server has and the only one that reaches
another person. Instahyre has no unsend, no edit and no delete, so every guard
around it is a guard whose failure is permanent -- and guards around a surface
nobody dares exercise are exactly the guards that quietly stop working.

Two claims are being defended and they fail in different directions:

  THE CARVE-OUT IS ONE PATH WIDE. Reply became reachable; starring, marking
  read and bulk mark-all-read did not. A widening here is silent -- nothing
  breaks, the server simply gains a power nobody granted it.

  THE GATE HOLDS. confirm=False sends nothing, an empty message is refused, the
  body is exactly what the preview showed, and a 200 is not treated as
  delivery.

Each plant below breaks ONE of those and requires the specific test that claims
to cover it to go red. Then the file is restored byte-for-byte.

Sibling controls: ``skill_removal_controls.py`` (removal by omission),
``permissive_scorer_control.py``, ``presence_is_auth_control.py``.

    venv/Scripts/python.exe scripts/reply_guard_controls.py

Exit 0 only when every plant went red AND the module is green afterwards.
Nothing here contacts Instahyre. Strict ASCII, like every file in this package.
"""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = REPO / "venv" / "Scripts" / "python.exe"
WRITES = REPO / "instahyre_server" / "writes.py"
CONSTANTS = REPO / "instahyre_server" / "constants.py"
MODULE = "tests/test_writes.py"


def case(name, path, old, new, test):
    return {"name": name, "path": path, "old": old, "new": new, "test": MODULE + "::" + test}


PLANTS = [
    # -- the carve-out ------------------------------------------------------
    case(
        "the allowlist widened to admit starring too",
        CONSTANTS,
        "SENDABLE_INBOX_PATHS = frozenset({EP_SEND_MESSAGE})",
        "SENDABLE_INBOX_PATHS = frozenset(\n"
        "    {EP_SEND_MESSAGE, '/resume_modal/emails/message/star_conversation'}\n"
        ")",
        "test_the_sendable_allowlist_holds_exactly_one_path",
    ),
    case(
        "the send constant repointed at the mark-all-read trap",
        CONSTANTS,
        'EP_SEND_MESSAGE = "/resume_modal/emails/message/send_message/"',
        'EP_SEND_MESSAGE = "/inbox_page/candidate_conversation/mark_all_read"',
        "test_the_sendable_allowlist_holds_exactly_one_path",
    ),
    case(
        "the guard turned into a blocklist that only names the obvious",
        WRITES,
        "    if path not in C.SENDABLE_INBOX_PATHS:",
        "    if 'apply_bulk' in path:",
        "test_the_send_guard_is_an_allowlist_not_a_blocklist",
    ),
    case(
        "send_message dropped from the READ tier's refusal list",
        CONSTANTS,
        '    "mark_all_read",\n    "send_message",',
        '    "mark_all_read",',
        "test_the_read_tier_still_refuses_send_message_along_with_all_four_others",
    ),
    # -- the gate -----------------------------------------------------------
    case(
        "confirm ignored -- the preview branch removed",
        WRITES,
        "        if not confirm:\n            preview[\"confirmed\"] = False\n"
        "            preview[\"next\"] = (\n"
        "                \"NOTHING HAS BEEN SENT. Read 'recipients' and 'message_as_typed' above, \"",
        "        if False:\n            preview[\"confirmed\"] = False\n"
        "            preview[\"next\"] = (\n"
        "                \"NOTHING HAS BEEN SENT. Read 'recipients' and 'message_as_typed' above, \"",
        "test_a_reply_without_confirm_issues_no_write_at_all",
    ),
    case(
        "an empty message allowed through, as Instahyre's own page allows it",
        WRITES,
        "        if not text:\n            raise NothingToDo(\n"
        "                \"An empty reply would put a blank message in a recruiter's inbox under \"",
        "        if False:\n            raise NothingToDo(\n"
        "                \"An empty reply would put a blank message in a recruiter's inbox under \"",
        "test_an_empty_or_blank_reply_is_refused_and_sends_nothing",
    ),
    case(
        "the length rail removed",
        WRITES,
        "        if len(text) > MAX_REPLY_CHARS:",
        "        if False:",
        "test_a_reply_over_the_length_cap_is_refused_and_one_at_the_cap_is_allowed",
    ),
    case(
        "content sent raw, so markup in his message is interpreted",
        WRITES,
        '            "content": _as_message_html(text),',
        '            "content": text,',
        "test_html_special_characters_are_escaped_rather_than_sent_as_markup",
    ),
    case(
        "lines reflowed into one paragraph",
        WRITES,
        '        "<p>%s</p>" % (line.strip() or "<br>") for line in lines',
        '        "<p>%s</p>" % " ".join(lines)[:0] or "<p>%s</p>" % " ".join(lines)',
        "test_line_breaks_survive_as_paragraphs_rather_than_being_reflowed",
    ),
    case(
        "the preview quotes the browser's Content-Type, not this client's",
        WRITES,
        '                "content_type": "application/json",',
        '                "content_type": "application/json;charset=utf-8",',
        "test_the_preview_states_the_content_type_this_client_actually_sends",
    ),
    case(
        "attachments invented rather than left empty",
        WRITES,
        '            "attachments": [],\n        }\n        # The doorway.',
        '            "attachments": [{"id": 1}],\n        }\n        # The doorway.',
        "test_attachments_are_always_empty_because_the_element_shape_is_unmeasured",
    ),
    case(
        "a 200 treated as delivery -- no read-back at all",
        WRITES,
        "        verification = self._verify_reply(conv_id, text)",
        '        verification = {"ok": True, "how": "the server answered 200"}',
        "test_a_send_that_cannot_be_confirmed_says_so_and_says_do_not_retry",
    ),
    case(
        "the read-back blinded by show_message, as it was before the fix",
        WRITES,
        "            thread = self.inbox.read_conversation(\n"
        "                conv_id, body_chars=None, include_gated=True\n"
        "            )",
        "            thread = self.inbox.read_conversation(conv_id, body_chars=None)",
        "test_a_send_is_verified_by_re_reading_the_thread",
    ),
    case(
        "the CSRF refusal removed",
        WRITES,
        '        if not self.http.cookies.get("csrftoken"):\n'
        "            raise ConfirmationRequired(\n"
        '                "Refusing to send without a CSRF token',
        "        if False:\n"
        "            raise ConfirmationRequired(\n"
        '                "Refusing to send without a CSRF token',
        "test_a_confirmed_reply_without_a_csrf_token_refuses_before_sending",
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

    print("\n=== reply guards: red controls ===")
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
