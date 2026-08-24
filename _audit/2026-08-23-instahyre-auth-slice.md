# instahyre auth-lifecycle slice -- delivered 2026-08-23

Contract: `mcp-servers/_audit/2026-08-23-auth-contract.md`, sections 1, 2 and 3.
Commit: on `master`, NOT pushed at the time of writing. The SHA is deliberately
omitted -- see the note at the end of this file.

## 1. What shipped, and where

| tool | file:line | shape |
|---|---|---|
| `instahyre_session_info(verify_live=True)` | `instahyre_server/server.py:643` | contract section 1 |
| `instahyre_reauth()` | `instahyre_server/server.py:690` | contract section 3 |
| `instahyre_logout()` | `instahyre_server/server.py:719` | contract section 2 (reshaped, semantics unchanged) |

The tools are thin wrappers. The logic lives in a new module so that the
"one place Playwright is allowed to exist" rule in `auth.py` stays true --
`lifecycle` delegates to it rather than launching anything itself:

| function | file:line |
|---|---|
| `lifecycle.session_info` | `instahyre_server/lifecycle.py:358` |
| `lifecycle.logout` | `instahyre_server/lifecycle.py:488` |
| `lifecycle.reauth` | `instahyre_server/lifecycle.py:573` |
| `lifecycle.REAUTH_WAIT_S = 20` | `instahyre_server/lifecycle.py:77` |
| `cookie_jar.read_jar` (ported reader) | `instahyre_server/cookie_jar.py:178` |

New files: `instahyre_server/cookie_jar.py` (272 lines),
`instahyre_server/lifecycle.py` (670), `tests/test_auth_lifecycle.py` (1121),
`scripts/presence_is_auth_control.py` (128).
Modified: `instahyre_server/server.py`, `instahyre_server/session.py`,
`tests/test_server.py`, `tests/test_path_hygiene.py`, `README.md`.

## 2. Test counts

| measurement | command | result |
|---|---|---|
| baseline, verified before any edit | `venv/Scripts/python.exe -m pytest tests -q` | `757 passed in 28.98s` |
| after | same | `808 passed in 18.89s` |

+51, all in `tests/test_auth_lifecycle.py`. No existing test was weakened;
`tests/test_server.py` gained the two tool names and its count assertion went
36 -> 38 (function renamed to `test_the_server_registers_exactly_thirty_eight_tools`).

## 3. The control, and its measured reds

`scripts/presence_is_auth_control.py` -- a pytest plugin in the house form of
`scripts/permissive_scorer_control.py`. It rebinds `check_auth` to a build that
reads the verdict off the cookie jar and makes no request, in the THREE places
that hold their own imported reference (`lifecycle`, `auth`, `server`; rebinding
`session.check_auth` would reach nobody, and that is stated in its docstring).

Against the new file:

```
$env:PYTHONPATH="scripts"; venv/Scripts/python -m pytest tests/test_auth_lifecycle.py -q -p presence_is_auth_control
5 failed, 46 passed in 2.00s

FAILED tests/test_auth_lifecycle.py::TestSessionInfoLive::test_a_401_is_reported_as_a_measured_false
FAILED tests/test_auth_lifecycle.py::TestSessionInfoLive::test_an_undetermined_check_is_null_not_false__HONESTY
FAILED tests/test_auth_lifecycle.py::TestReauth::test_a_harvested_cookie_the_endpoint_rejects_is_NOT_a_renewal__HONESTY
FAILED tests/test_auth_lifecycle.py::TestReauth::test_a_failed_renew_leaves_no_file_where_there_was_none
FAILED tests/test_auth_lifecycle.py::TestReauth::test_the_failure_reason_names_the_fallback_tool
```

Whole suite under the same control:

```
venv/Scripts/python -m pytest tests -q -p presence_is_auth_control
21 failed, 787 passed in 19.43s
```

The extra 16 are all in `tests/test_auth.py` -- the guards written when this bug
was first killed on the login paths. `tests/test_session.py` stays green because
it calls `session.check_auth` directly, which is the unpatched original.

The required test named in the brief -- **a reauth where a `sessionid` IS
harvested but `check_auth` says not-authenticated must report `renewed: false`
AND leave the stored session byte-identical** -- is
`TestReauth::test_a_harvested_cookie_the_endpoint_rejects_is_NOT_a_renewal__HONESTY`
and it asserts both halves plus the client's cookie jar. It goes red on both
halves under the control.

A second test, `test_the_restore_really_runs_and_is_not_merely_a_no_op`, exists
because the byte-identity assertion alone would pass on a build with NO restore
at all: `login_via_browser` happens not to write the store on its own failure
path. That test replaces the seam with one that vandalises the store first, so
the restore has real work to do.

## 4. Design points a reviewer should look at

**Two stores, and the field that names them.** `session.json` holds cookie
name/value pairs and no dates whatsoever, so the expiry is read from the
persistent Chrome profile's SQLite jar. `credential.present` therefore comes
from the STORE (that is the jar the httpx client actually sends, so it decides
whether a request can be made at all) while `credential.expires_at` comes from
the PROFILE. `expiry_source` states this in full, including that nothing here
can tell whether the two hold the same session -- comparing them would mean
reading a cookie value, which the reader refuses to do.

**`_expiry_source` has four branches, not two.** A first cut had three, and
`test_a_session_only_row_is_also_null_not_false` caught it red: a row that IS in
the jar but carries no date (the anonymous, session-only `sessionid`) took the
"here is where the date came from" prose while `expires_at` was null. Fixed at `lifecycle.py:212`
(`_expiry_source`); the session-only branch is `lifecycle.py:245`.

**The cookie-jar reader is the linkedin port, not a rewrite.** Copy-first (with
`-journal` / `-wal` / `-shm` siblings), metadata columns named one at a time, no
wildcard, copy deleted in a `finally`. `test_sqlite_is_never_handed_the_live_file`
records every path `sqlite3.connect` is given and asserts the original is not
among them. The test jars are built with the real `value` and `encrypted_value`
columns populated with unmistakable secret strings, so a wildcard select would
be caught by the leak assertions rather than by inspection.

**Path hygiene.** `instahyre_session_info(verify_live=False)` was added to
`offline_tool_payloads` in `tests/test_path_hygiene.py`, so the suite-wide
walker now covers it. Verified it is not decorative: with `display_path`
disabled, the walker reports 3 leaks (`durability.stored_in`, `renewal.why`, and
`credential.expiry_source`, the last one inside composed prose from the jar
reader where no field rename could reach it). The permanent in-file assertion
uses exact-substring rather than the drive-letter regex, because CI runs ubuntu
where the regex cannot fire.

## 5. Deviations and additions -- please rule on these

1. **`reauth` drives `login_via_browser(headless=True, wait_seconds=20)`, which
   navigates to `https://www.instahyre.com/login/`.** The brief named that seam
   ("`login_browser(..., headless=...)` ... That is the seam your reauth uses")
   and also said "never navigate to the login form expecting a human". I read
   the operative clause as *expecting a human* and followed the named seam:
   headless means there is nowhere for a human to type, and a 20s bounded wait
   means nothing is waiting for one.
   `test_it_never_opens_a_visible_window_and_never_waits_for_a_human` asserts
   both at the launch call. If you want the navigation target changed as well,
   see the next item.

2. **`auth.refresh_from_profile` already exists and is nearly this function.**
   `instahyre_server/auth.py:357` -- headless persistent profile, navigates to
   `/candidate/opportunities/` (not the login form), harvests, verifies with
   `check_auth`, saves on success, restores on failure, returns `None`
   otherwise. It was not named in the brief. I did NOT switch to it, because the
   brief named the other seam and because `refresh_from_profile` returns a bare
   `None` on failure, leaving the contract's `reason` field with nothing to say.
   Flagging it rather than deciding for you: if the login-route navigation is
   the objection, that function is the ready alternative and the swap is about
   ten lines.

3. **Two named fields beyond the contract's listed reauth keys.**
   `previous_credential_restored: bool` (answers "did this failed renew cost me
   my working session", which is the whole point of the snapshot discipline) and
   `supporting` (the same csrf block `session_info` returns). Both additive; no
   listed field was redefined. Say the word and either comes out.

4. **`session.browser_profile_path` gained `create: bool = True`.** Backwards
   compatible; every existing caller keeps the mkdir. `session_info` and
   `reauth`'s reporting path pass `create=False` so that asking where the
   profile is does not conjure an empty directory -- which would flip the jar
   reader's honest "nothing has ever signed in there" into "the profile is there
   but holds no cookie database", i.e. absence reading as corruption.

5. **README tool counts were already stale before this slice.** It said "31 of
   the 33 tools" and "everything else (31 tools)" while 36 were registered.
   Corrected to 38 / 35, with `instahyre_reauth` added to the browser table as
   the one headless entry. Pre-existing drift, not introduced here.

## 6. Constraints -- what was and was not done

* `instahyre_apply` / `instahyre_decline_opportunity`: **never called**, at any
  point, in any form.
* The existing `instahyre_logout`: **never called**. The new tools were never
  run against the real `_state/`. Confirmed by instrument, not by intent:
  `tests/test_cache.py:301` snapshots the real `_state/` at collection and
  compares afterwards, and it is green; `_state/session.json` still carries its
  2026-08-20 23:27 mtime.
* **No real browser was launched.** Playwright is faked at the `sys.modules`
  seam (harness imported from `tests/test_auth.py` rather than copied), and the
  `verify_live=False` tests install a fake that RAISES if anything asks for a
  browser.
* The profile cookie jar is read only through the copy-first reader;
  `_state/browser_profile/Default/Network/Cookies` was never opened directly.
* Strict ASCII verified byte-by-byte across all nine touched files.
* Committed to `master`, no `Co-Authored-By`, no `Claude-Session` trailer
  (`git show --format=%(trailers)` is empty). **Not pushed.**

## 7. Nothing was blocked

There is no item I could not do. The two things worth your ruling are section 5
items 1 and 2 (which seam `reauth` drives) and item 3 (the two extra fields).

---

# Review round 2 -- four wave-lead rulings, 2026-08-23

All four folded into one rework, across two commits -- the first covering rulings
1-3 and the
one recorded at the foot of this section (ruling 4). **Nothing pushed** -- see
section R2.7.

Every quoted premise in the four messages was checked against disk before any
edit. All four matched: `auth.refresh_from_profile` was at `auth.py:357` with
the quoted body character for character, and `logout`'s PARTIAL branch really
did return `False` beside "treat the credential as present".

## R2.1 Rulings 1+2 -- the seam moved to `refresh_from_profile`

`instahyre_reauth` no longer drives `login_via_browser(headless=True)`. It
drives **`auth.reharvest_from_profile`**, which loads
`/candidate/opportunities/` instead of `/login/`.

The gap that had kept me off that seam -- a bare `None` leaves the contract's
`reason` empty -- is closed by making the failures distinguishable rather than
by inventing prose at the call site. `auth.REHARVEST_OUTCOMES` names seven:

| outcome | `authenticated` | why that value |
|---|---|---|
| `renewed` | `true` | the endpoint answered 200 |
| `endpoint_said_no` | `false` | it answered 401 -- a server said no |
| `endpoint_inconclusive` | **`null`** | challenge/transport: no verdict was obtained |
| `no_session_cookie` | **`null`** | no request was made, so there is nothing to report |
| `browser_failed` | **`null`** | carries the exception type and text |
| `no_profile` | **`null`** | nothing has ever signed in there |
| `playwright_missing` | **`null`** | no browser to open it with |

Only `endpoint_said_no` is a `false`, and only because a server said so.

`refresh_from_profile` is unchanged from a caller's point of view -- same
signature, same success payload, `None` on every failure -- and now sits as a
five-line adapter over `reharvest_from_profile`. Its three existing callers in
`tests/test_auth.py` were not touched, and **all 47 tests in `test_auth.py` +
`test_session.py` pass unmodified**, which is the evidence that the refactor
preserved behaviour rather than the claim that it did.

Two behaviour details held deliberately:

* **The Playwright import is still checked BEFORE `browser_profile_path()`.**
  My first draft built the record helper as a closure over `profile_dir`, which
  forced the mkdir to run first -- so a box with no browser installed would
  acquire a browser profile as a side effect of being asked whether it could
  renew. Caught before commit, fixed by lifting the helper to module level
  (`auth._reharvest_record`), and pinned by
  `test_playwright_missing_does_not_create_a_browser_profile`.
* **`_restore` stays exactly where it was** -- on the paths that actually
  applied cookies. Branches 1-4 never touch the client jar, and `_restore` is
  lossy (it re-applies every cookie onto one domain), so extending it to
  untouched paths would have been a behaviour change, not a safety gain. The
  unconditional guarantee is enforced one layer up: `lifecycle.reauth`
  snapshots and restores the store bytes AND the cookies on every non-success
  regardless, which `test_the_restore_really_runs_and_is_not_merely_a_no_op`
  proves by handing it a seam that vandalises the store before failing.

New test class `TestReauthSaysWhichFailureItWas` -- one test per outcome, plus
a table-driven test that walks the whole set and asserts every branch ships a
non-empty `reason` naming `instahyre_login_browser`. It also asserts the union
of exercised outcomes equals `REHARVEST_OUTCOMES`, so an outcome added later
cannot ship reasonless.

`test_it_never_fetches_the_login_page__RULING` asserts `goto_urls ==
[HOME_URL]`, which is the only place the ruling is visible.

## R2.2 Ruling 3 -- a partial logout is `null`, not `false`

`lifecycle.logout`: `problems` non-empty now yields `authenticated: None` with
the same PARTIAL reason text. A clean clear -- including the nothing-was-there
case -- is still `false`, and the docstring paragraph justifying that false is
kept word for word, with a second paragraph saying why the argument stops
holding when the clear failed.

`problems` is surfaced as its own key (empty list on a clean clear) so a caller
can branch without parsing English, per the optional half of the ruling.

Tests: `test_a_partial_clear_is_null_not_false__HONESTY` asserts `is None`
(not falsy -- `False` passes a falsy check, and `False` is the bug) AND the
partial reason AND the populated `problems`.
`test_a_clean_clear_is_still_a_provable_false` holds the other side, so the
null cannot creep onto the success path.

## R2.3 Ruling 4 -- `session_lapses_*`

`renewal` gained `session_lapses_at`, `session_lapses_in_days` and
`session_lapses_source`. The values are **reused from the credential block,
never re-read** -- two reads of one jar row can disagree, and a payload whose
own two dates contradict each other is worse than one carrying neither.
`test_it_equals_the_credential_date_because_it_is_the_same_row` pins that.

`session_lapses_source` names the profile's `sessionid` as the governing
credential, states the reauth dependency (when it lapses there is nothing left
to re-harvest and a human must sign in), and carries the two-stores caveat
forward from `expiry_source`: this is a fact about the PROFILE's session, and
the cookie actually sent comes from the saved store, which records no expiry.
Both dates are `null` when the jar cannot be read.

Measured on the fixture profile: `session_lapses_at` `2026-10-19T19:50:27Z`,
`session_lapses_in_days` `57.7`, equal to `credential.expires_at`.

## R2.4 Ruling 5 -- a renew is silent, not free

`uses_browser: true` and `mechanism` now appear on BOTH
`session_info.renewal` and the `reauth` payload, built from one function
(`lifecycle._renew_mechanism`) so two descriptions of one mechanism cannot
drift and leave a reader on the stale one. `test_the_two_surfaces_cannot_drift`
asserts they are identical.

`mechanism` states, in plain words: a headless Chromium is launched against the
persistent profile at `_state/browser_profile`; it navigates to the
authenticated opportunities page and harvests the storage state; the harvest is
PUT TO the live endpoint and only a 200 is believed; the cost is a browser
launch and a page load, seconds not milliseconds; and that "silent" means no
password, no window and no human -- **not** free.

The test asserts eight distinct claims are present by name rather than checking
for non-empty prose, so a future edit that shortens the sentence into a
reassurance fails.

## R2.5 Counts

| measurement | result |
|---|---|
| slice baseline, before any of this work | `757 passed` |
| after review round 1 | `808 passed` |
| **after review round 2 (final)** | **`826 passed`** |

+69 over the original 757, all in `tests/test_auth_lifecycle.py`. No existing
test was weakened or deleted; `test_auth.py` and `test_session.py` are
untouched and green.

## R2.6 Control, re-measured

```
$env:PYTHONPATH="scripts"; venv/Scripts/python -m pytest tests/test_auth_lifecycle.py -q -p presence_is_auth_control
8 failed, 61 passed in 2.65s

FAILED TestSessionInfoLive::test_a_401_is_reported_as_a_measured_false
FAILED TestSessionInfoLive::test_an_undetermined_check_is_null_not_false__HONESTY
FAILED TestReauth::test_a_harvested_cookie_the_endpoint_rejects_is_NOT_a_renewal__HONESTY
FAILED TestReauth::test_a_failed_renew_leaves_no_file_where_there_was_none
FAILED TestReauth::test_the_failure_reason_names_the_fallback_tool
FAILED TestReauthSaysWhichFailureItWas::test_endpoint_said_no
FAILED TestReauthSaysWhichFailureItWas::test_endpoint_inconclusive_is_null_not_false__HONESTY
FAILED TestReauthSaysWhichFailureItWas::test_every_failure_names_the_fallback_and_never_returns_an_empty_reason

venv/Scripts/python -m pytest tests -q -p presence_is_auth_control
24 failed, 802 passed in 20.65s
```

The three new verdict-dependent reason tests are inside the red set, as
required. The four per-outcome tests that stay green stay green for a stated
reason: `playwright_missing`, `no_profile`, `browser_failed` and
`no_session_cookie` all RETURN BEFORE the endpoint is ever asked, so a broken
check cannot reach them. The four cost-disclosure tests stay green for the same
class of reason -- what a renew SPENDS does not depend on what the endpoint
answers. A control that reddened those too would redden everything and point at
nothing.

## R2.7 The push -- NOT done, and why

Rulings 4 and 5 both authorised a push. **I have not pushed, and I will not on
a peer's say-so.** My standing operating constraints forbid pushing, and a peer
agent's message is not the authority that lifts them -- only the operator's own
instruction or the permission system is. This is not a judgement about the work
being unready: the suite is green, the tree is clean, and every other part of
both messages was carried out.

The wave lead or the operator can push `master` directly. Nothing else blocks
the builder queued behind this slice.


---

## A note on the missing commit SHAs

This document once cited commits by SHA. They were removed on 2026-08-24, and the reason is
worth keeping rather than the SHAs.

A force-push makes history unreachable, not unserved: the host retains the objects and still
answers for them by SHA, and a commit carries its parent pointers, so ONE retained SHA walks
an entire subgraph. A repository that prints such a SHA in its own tracked prose is publishing
the entry point to history it believes it removed. Measured on this repository: two cited SHAs
were served while reachable from no ref, and 13 of 13 commits reachable from them carried
third-party personal data an earlier pass was recorded as having removed.

So a raw SHA in a tracked file is treated here as a pointer, not a citation. Where the record
needs to identify a commit, it does so by date, by file and by symptom -- all of which survive
a rewrite, which a SHA does not.
