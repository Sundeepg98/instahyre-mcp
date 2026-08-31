# 2026-08-31 - identity paths (instahyre slice)

The full write-up lives in the Naukri repository, because the pass covered three repos at once
and the finding is the same class in all three:

    naukri-mcp  _audit/2026-08-31-path-sweep.md

This file records what happened HERE, so the numbers are findable from this repo alone. It names
no real value: `<given>` is the operator's given name, `<account>` his Windows account name.

## What was found

9 hits across 3 tracked files carried an absolute path whose first segment is `<given>`, and 6
more carried `<account>`. **Six of those are PRODUCTION SOURCE, not test data:**

    instahyre_server/paths.py     3 x <given> in the module docstring, 2 x <account>
    instahyre_server/policy.py    1 x <account>

The remaining hits sit in `tests/test_path_hygiene.py` and `tests/test_scoring_policy.py`.

The irony is exact and worth recording: the leaking docstrings are the ones explaining how a path
gets relativised, and how `repr()` doubles a separator so an exact-substring scrubber walks
straight past it. They were correct about the mechanism and demonstrated it with the real value.

## Why nothing caught it

**There was no rule.** `tests/test_path_hygiene.py` is thorough, but its subject is a TOOL RESULT
at runtime: it walks payloads and has never read a tracked file. `tests/test_pii_hygiene.py` does
walk `git ls-files` and hunts twenty shapes - but the only two whose names contain PATH,
`ACCOUNT_SCOPED_ID_IN_PATH` and `PUBLIC_AVATAR_PATH`, are URL-route rules. Neither is a
filesystem path.

So committed files were checked for email, phone, handles, credentials and account ids, and never
once for this machine's layout. The class was UNGUARDED, not under-guarded.

## What changed

`4e944ab`. 9 given-name and 6 account substitutions. Every separator run was captured and
re-emitted byte for byte, so only the segment changed and the escaping did not - which matters
more here than anywhere, because two of these docstrings exist specifically to show two spellings
of one path differing.

325 lines appended to `tests/test_pii_hygiene.py`: three rules (Windows user path, drive root,
POSIX home) with the separator run written `+` rather than one character, two measured
allowlists, and five tests - including the narrow rule DERIVED from the shipped one and shown
failing on the doubled spelling. Every separator is built from `chr(92)`.

Driven over this repo's own pre-fix content at HEAD, the new check reports **15 findings**.

Suite **1541 -> 1546**, the +5 being exactly the five new tests.

## Not covered

Read section 8 of the Naukri write-up before treating this repo as clean. In particular: the
redaction cleans the TREE, and does not remove anything from published history; and the new rules
hunt three path shapes, so a green run means no paths of those shapes, not a clean repository.
