"""Prove the auth self-description guards can fail, by breaking each one first.

WHY THIS EXISTS
---------------
On 2026-08-25 this server's ``session_info`` was audited for one thing only:
does it describe its own auth honestly. Three findings, and every test written
against them was green from the moment it was written. Green is exactly what a
check that CANNOT fail looks like, and this package has already been bitten by
a family of those. So each new guard is broken here and required to notice.

WHAT WAS WRONG, AND WHY THESE PARTICULAR PLANTS
-----------------------------------------------
1. ``credential.expiry_is_authoritative`` was a HARDCODED ``True``. It sits in
   the block describing the cookie the client SENDS -- read from session.json
   -- while the date beside it is read from the browser profile's SQLite jar.
   Two stores. An unqualified true there tells a consumer to plan a re-login
   off a date that describes a different session.

2. ``expiry_source`` said the two stores could not be told apart "because
   comparing them would mean reading a cookie value", and that read like an
   unexamined limit. It was mistaken for one: a divergence check was specified
   -- hash both values, emit a boolean, never emit the digest. MEASURED
   against the operator's live profile jar before writing any of it: 57 rows,
   0 bytes of plaintext ``value``, every value in ``encrypted_value``, scheme
   tag ``v10``. v10 is AES-256-GCM with a fresh random nonce per write, so the
   SAME sessionid seals to different bytes -- a digest of the sealed blob
   reports two identical sessions as DIFFERENT. The check was refused: its
   failures would have been indistinguishable from its successes. The plants
   below pin both halves of that ruling -- the honest prose, and the banned
   field.

3. The ``renewal`` block carried a bare ``uses_browser: true``. The key is
   spelled the same way on four servers, and on the ones that open a real
   sign-in window a true correctly means "hand this to the human". Here nothing
   opens: reauth is headless, takes no credential parameter and never loads a
   sign-in page, so a client branching on the bare boolean would wait for an
   event that cannot happen.

A NOTE ON WHAT COUNTS AS RED, inherited rather than rediscovered. pytest exits
1 when a test FAILS and 4 when a node id does not exist. A harness reading any
non-zero exit as RED reports a plant whose test was RENAMED as a passing
control while running nothing -- measured in ``reply_guard_controls.py`` on
2026-08-25. Only exit 1 is RED here.

A NOTE ON LINE ENDINGS. ``lifecycle.py`` is CRLF and the test files are LF.
Anchors are written LF and matched against a normalised copy; the write puts
each file back in its own convention, so a plant never leaves a whole-file
diff behind when it reverts.

Sibling controls: ``bulk_apply_controls.py``, ``inbox_write_controls.py``,
``reply_guard_controls.py``, ``jsp_write_controls.py``,
``skill_removal_controls.py``, ``permissive_scorer_control.py``,
``presence_is_auth_control.py``.

    venv/Scripts/python.exe scripts/auth_contract_controls.py

Exit 0 only when every plant went red AND every module is green afterwards.
Nothing here contacts Instahyre. Strict ASCII, like every file in this package.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

NL = chr(10)

REPO = Path(__file__).resolve().parent.parent
PY = REPO / "venv" / "Scripts" / "python.exe"
LIFECYCLE = REPO / "instahyre_server" / "lifecycle.py"
README = REPO / "README.md"

LIFE = "tests/test_auth_lifecycle.py"
MODULES = (LIFE, "tests/test_credential_leak.py", "tests/test_path_hygiene.py")

AUTHORITY = "TestTheExpiryIsNotAuthoritativeForTheSentCredential"
DIGEST = "TestNoDigestOfACookieEverReachesTheOutput"
WINDOW = "TestUsesBrowserCannotSilentlyBecomeAWindowOpeningClaim"
WHY200 = "TestTheRenewalBlockSaysWhyA200IsTheWholeTest"
README_RULE = "TestTheReadmeSaysHowTheWriteSurfaceIsCounted"


def read_text(path):
    """(normalised LF text, original newline convention)."""
    raw = path.read_bytes()
    crlf = raw.count(b"\r\n")
    text = raw.decode("utf-8").replace("\r\n", "\n")
    return text, ("\r\n" if crlf else "\n")


def write_text(path, text, newline):
    io.open(path, "w", encoding="utf-8", newline=newline).write(text)


def case(name, path, old, new, test, module=LIFE):
    return {
        "name": name,
        "path": path,
        "old": old,
        "new": new,
        "test": module + "::" + test,
    }


PLANTS = [
    # -- FINDING 1: the authority field -----------------------------------
    case(
        "authority: the hardcoded True is back, for the credential being SENT",
        LIFECYCLE,
        '        "expiry_is_authoritative": False,\n'
        '        "expiry_authoritative_for": (',
        '        "expiry_is_authoritative": True,\n'
        '        "expiry_authoritative_for": (',
        AUTHORITY + "::test_a_date_that_exists_is_not_authoritative_for_the_cookie_in_use",
    ),
    case(
        "authority: no date collapses to false instead of null -- a claim from nothing",
        LIFECYCLE,
        '            "expiry_is_authoritative": None,\n'
        '            "expiry_authoritative_for": None,',
        '            "expiry_is_authoritative": False,\n'
        '            "expiry_authoritative_for": None,',
        AUTHORITY + "::test_no_date_means_NULL_not_false__HONESTY",
    ),
    case(
        "authority: the false stops naming what to trust instead -- a dead end",
        LIFECYCLE,
        '            "(instahyre_auth_status), not off this date."',
        '            "(the live check), not off this date."',
        AUTHORITY + "::test_the_false_names_what_the_date_IS_authoritative_for",
    ),
    # -- FINDING 2: the prose, and the check that cannot exist -------------
    case(
        # The FIRST version of this plant swapped only the OPENING line and
        # the guard stayed GREEN: v10, AES-256-GCM, nonce and HASH all still
        # sat in the lines below it, so the test still found every substring
        # it looks for. A plant that leaves the evidence in place measures
        # nothing, and it was the PLANT that was weak here, not the guard.
        # This one reverts the whole passage to the exact pre-2026-08-25
        # sentence, which is the regression that can really happen: someone
        # decides the prose is too long and puts the short one back.
        "prose: the measured reason removed -- back to the shrug that misled a lead",
        LIFECYCLE,
        NL.join([
            '        "profile while the saved jar sits still. NOTHING HERE CAN TELL THEM "',
            '        "APART, and the reason is measured rather than assumed: Chrome seals "',
            '        "every value in that jar under its v10 scheme -- AES-256-GCM with a "',
            '        "fresh random nonce per write -- so the SAME sessionid written twice "',
            '        "seals to different bytes. A HASH of the sealed blobs therefore "',
            '        "settles nothing: it would report two identical sessions as "',
            '        "different, which is a wrong answer wearing the clothes of a "',
            '        "measurement. A comparable digest would mean decrypting to plaintext, "',
            '        "and the plaintext IS the cookie value this reader exists never to "',
            '        "fetch. So sameness here is not merely unmeasured, it is unmeasurable "',
            '        "without reading the secret -- which is why expiry_is_authoritative "',
            '        "below is false whenever a date exists."',
        ]),
        NL.join([
            '        "profile while the saved jar sits still. Nothing here can tell them "',
            '        "apart, because comparing them would mean reading a cookie value."',
        ]),
        AUTHORITY + "::test_the_prose_carries_the_MEASURED_reason",
    ),
    case(
        "divergence: the refused boolean is invented anyway",
        LIFECYCLE,
        '        **_expiry_authority(session_expiry["expires_at"], store.path),',
        '        **_expiry_authority(session_expiry["expires_at"], store.path),\n'
        '        "same_session_in_both_stores": True,',
        AUTHORITY + "::test_no_divergence_BOOLEAN_was_invented",
    ),
    case(
        "leak: a digest of the SENT cookie reaches the payload",
        LIFECYCLE,
        '        **_expiry_authority(session_expiry["expires_at"], store.path),',
        '        **_expiry_authority(session_expiry["expires_at"], store.path),\n'
        '        "divergence_probe": __import__("hashlib").sha256(\n'
        '            (saved_cookies.get(SESSION_COOKIE) or "").encode("utf-8")\n'
        '        ).hexdigest()[:16],',
        DIGEST + "::test_no_digest_of_either_cookie_appears_anywhere",
    ),
    # -- FINDING 3: the bare uses_browser ---------------------------------
    case(
        "window: the qualifiers deleted -- the bare boolean returns to renewal",
        LIFECYCLE,
        '        "opens_a_window": False,\n'
        '        "waits_for_a_human": False,\n'
        "        \"mechanism\": _renew_mechanism(profile_dir),\n"
        "        # About session_lapses_at",
        "        \"mechanism\": _renew_mechanism(profile_dir),\n"
        "        # About session_lapses_at",
        WINDOW + "::test_a_true_uses_browser_is_never_alone_in_any_payload",
    ),
    case(
        "window: renewal claims a window opens and a human is awaited",
        LIFECYCLE,
        '        "opens_a_window": False,\n'
        '        "waits_for_a_human": False,\n'
        "        \"mechanism\": _renew_mechanism(profile_dir),\n"
        "        # About session_lapses_at",
        '        "opens_a_window": True,\n'
        '        "waits_for_a_human": True,\n'
        "        \"mechanism\": _renew_mechanism(profile_dir),\n"
        "        # About session_lapses_at",
        WINDOW + "::test_the_qualifiers_sit_beside_the_boolean",
    ),
    case(
        "window: reauth drops the qualifiers, so the two surfaces disagree",
        LIFECYCLE,
        "        # Spelled here too, and for the same reason as in session_info's\n"
        "        # renewal block: one mechanism described in two places must not say\n"
        "        # less in one of them. Nothing opened, and nothing could have.\n"
        '        "opens_a_window": False,\n'
        '        "waits_for_a_human": False,\n',
        "",
        WINDOW + "::test_reauth_carries_them_too",
    ),
    # -- the renewal block's most useful sentences ------------------------
    case(
        "renewal: the signed-out-visitor fact paraphrased away",
        LIFECYCLE,
        "            COOKIE_IS_NOT_A_SESSION,",
        '            "a cookie in the jar is not proof of a session.",',
        WHY200 + "::test_the_signed_out_visitor_fact_is_in_the_block",
    ),
    case(
        "renewal: the byte-for-byte restore guarantee removed",
        LIFECYCLE,
        '        "file is snapshotted as BYTES before the browser starts, and on any "',
        '        "file is left where it is before the browser starts, and on any "',
        WHY200 + "::test_the_byte_for_byte_restore_is_surfaced_not_just_implemented",
    ),
    case(
        "renewal: the lapse date asserts authority it cannot have",
        LIFECYCLE,
        '        "expiry_is_authoritative": (\n'
        '            None if credential["expires_at"] is None else False\n'
        "        ),",
        '        "expiry_is_authoritative": True,',
        WHY200 + "::test_the_lapse_date_is_not_authoritative_either_and_says_why",
    ),
    # -- the README sentence ----------------------------------------------
    case(
        "readme: the by-effect counting rule deleted",
        README,
        "**This package counts its write surface by effect, not by HTTP verb.**",
        "**This package is careful about writes.**",
        README_RULE + "::test_the_counting_rule_is_stated_in_the_safety_section",
    ),
    case(
        "readme: the rule keeps the slogan but loses mark_all_read, its only proof",
        README,
        "`mark_all_read` is a **GET that mutates in bulk**. One request marks every\n"
        "conversation in the inbox as read, and it is Instahyre's own dispatcher that\n"
        "sends it that way (`{method:'GET', url: url+'mark_all_read'}`). Anyone who reads\n"
        "the verb and assumes GET means safe will get this one wrong.",
        "some endpoints do not behave the way their method suggests.",
        README_RULE + "::test_it_names_the_trap_that_makes_it_concrete",
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
    """Only pytest's exit 1 means a test FAILED. See the module docstring."""
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
        original, newline = read_text(target)
        found = original.count(plant["old"])
        if found != 1:
            results.append((plant["name"], "ANCHOR-MISSING (%d hits)" % found, ""))
            continue
        try:
            write_text(
                target, original.replace(plant["old"], plant["new"], 1), newline
            )
            code, tail = pytest_run(plant["test"])
        finally:
            write_text(target, original, newline)
        results.append((plant["name"], verdict_for(code), tail))

    print("\n=== auth contract: red controls ===")
    bad = 0
    for name, result, tail in results:
        ok = result == "RED"
        bad += 0 if ok else 1
        print("[%s] %-74s %s" % ("ok " if ok else "BAD", name[:74], result))
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
