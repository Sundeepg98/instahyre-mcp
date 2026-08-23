# Instahyre MCP

Instahyre as MCP tools: public job search, the authenticated inbound side that
is the actual point of this platform, the message inbox, and guarded profile
writes.

Instahyre is a **reverse marketplace** -- employers put you in a curated queue
and recruiters open your resume. Outbound search is the commodity half; the
scarce signal is who engaged, and how fast you notice. `instahyre_inbound_digest`
is the tool that answers that in one call.

## The architecture: httpx by default, browser where the API cannot reach

**43 of the 46 tools are plain `httpx`. Three use a browser, and all three say so.**

Instahyre's `/api/v1/*` is exempt from Cloudflare bot management. It answers a
cold, unauthenticated, honestly-identified HTTP client on the first request --
no cookie, no token, no JS challenge. Measured across ~300 live requests: zero
throttling, zero challenges, on the default `python-httpx` User-Agent. So the
API is the data path, and it stays the data path.

Its HTML pages are a different story: those **are** Cloudflare-gated (403 to
`httpx`, `"Just a moment..."` to headless Chromium -- both measured). When
something exists only there, a real browser is the honest way to read it, and
this server uses one deliberately rather than pretending the thing is
unreachable.

That distinction was learned the hard way. An earlier build stated the rule as
"no browser, ever" and, on the strength of it, reported message bodies as **not
possible**. They were always possible. The conversation list simply lives in a
namespace nobody had looked in -- `/inbox_page/candidate_conversation`, not the
`/resume_modal/emails/` namespace where the message resource sits. A browser
found it in one page load, by recording what the inbox fetched. The endpoint
then turned out to be ordinary `/api/v1/*`, so the tools that use it are pure
`httpx`. **The browser answered a question the API could not be asked; it did
not join the data path.**

| Tool | Browser? | Why |
|---|---|---|
| `instahyre_login_browser` | yes, visible window | Google OAuth is a redirect dance no HTTP client can complete. |
| `instahyre_verify_apply_target` | yes, visible window | Reads a server-injected page flag that decides **which endpoint an application posts to**. No API exposes it, and the page is Cloudflare-gated. Applications cannot be withdrawn, so this is worth a browser rather than an assumption. |
| `instahyre_reauth` | yes, **headless**, never visible | Re-harvests the persistent profile's own long-lived `sessionid`, which outlives the copy saved on disk. It loads `/candidate/opportunities/` -- **never** the login page: a tool whose claim is "this is not a login" should not fetch the login URL, and sending a browser carrying a live session to a sign-in page is a needless risk. Headless is the guarantee, not an optimisation: no window means no human can be waited for. |
| everything else (43 tools) | no | Plain `httpx`. |

The two *visible-window* browser tools abort **every** non-GET request at the router, except
Cloudflare's own `/cdn-cgi/` challenge handshake -- which mutates nothing in the
account, and without which verification can never complete. Neither clicks
anything.

This is still the deliberate difference from the sibling Naukri server, which
needs a persistent Chrome profile, a CDP bridge and ~1,300 lines of anti-bot
plumbing in its data path. Here the browser is occasional and opt-in.

**We do not spoof a browser User-Agent.** It buys nothing (the API accepts the
honest one), and Instahyre's terms prohibit forging headers. If the API
exemption is ever removed, the client raises a typed `ChallengeDetected` -- that
is a signal to stop and reassess, not to route around.

## Install

```bash
python -m venv venv
venv/Scripts/python -m pip install -r requirements.txt

# ...then jobcore, the shared scoring engine. It is REQUIRED, and it is not on
# PyPI. Pick the line that matches your checkout:
venv/Scripts/python -m pip install -e ../jobcore          # you have the sibling
venv/Scripts/python -m pip install -r requirements-ci.txt # you do not (pinned, from git)

venv/Scripts/python -m pytest tests/ -q
```

The two jobcore lines are alternatives, not steps. Run the editable one if
`../jobcore` is checked out beside this repo -- it is the only way to iterate on
the scorer -- and the `requirements-ci.txt` one otherwise. Do **not** run the
second after the first: a direct-URL requirement silently uninstalls an editable
install, with no "already satisfied" line to warn you.

Playwright's browser binary is only needed by the three browser tools
(`instahyre_login_browser`, `instahyre_verify_apply_target`,
`instahyre_reauth`). The other 43 work without it -- and `instahyre_reauth`
reports "no silent renew was possible" and names the fallback rather than
raising, so a checkout with no chromium is degraded, not broken:

```bash
venv/Scripts/python -m playwright install chromium
```

## Tools

### Public -- no login

| Tool | What it does | Requests |
|---|---|---|
| `instahyre_search_jobs` | Search live jobs. Filters: skills, job functions, locations, company, industry, size, experience, job type. | 1 |
| `instahyre_get_job` | One job's description, experience band, recruiter, agency verdict. | 1 (cached 6h) |
| `instahyre_get_company` | An employer's profile plus every live job they have open. Doubles as a membership oracle. | 1 |
| `instahyre_market_stats` | Faceted market aggregates for a slice, with no job records at all. | 1 |
| `instahyre_rank_jobs` | Search, then rank one page by fit against your own skills. Skills come from `my_skills`, else the shared config, else -- when signed in -- his own Instahyre profile; `skills_source` names the winner. | 1+ (+2 for the profile fallback) |
| `instahyre_sync_index` | Page a slice into the local index and report what is NEW since last run. | 1/page |
| `instahyre_list_job_functions` | The 58 job functions with ids. | 1 (cached 30d) |
| `instahyre_list_locations` | The accepted location tokens, grouped. | 1 (cached 30d) |
| `instahyre_list_industries` | The 74 industry types with ids. | 1 (cached 30d) |
| `instahyre_server_info` | Cache state, request count, and what this platform cannot provide. | 0 |
| `instahyre_config` | The scoring policy in force, its hash, and which file it came from. | 0 |

### Authenticated -- inbound triage

The half that matters. Every one of these needs a live session.

| Tool | What it does | Requests |
|---|---|---|
| `instahyre_inbound_digest` | **Start here.** Queue badge, who viewed your resume, unread messages, top-scoring untouched matches, and what appeared since last run. | ~5 |
| `instahyre_list_opportunities` | The curated queue: roles employers matched to you, with real match scores. Ranked across the whole queue. | 1 |
| `instahyre_get_opportunity` | One match, composed from the queue record + public job detail + sibling roles at that employer. | 2-3 |
| `instahyre_opportunity_counts` | Queue facets: status, location, industry, employer, size. No records. | 1 |
| `instahyre_recruiter_activity` | Who viewed / contacted / did not shortlist you. The most perishable signal here. | 2 |
| `instahyre_list_applications` | Every application and decline, with state. | 2 |
| `instahyre_get_profile` | Your profile, plus completeness gaps ranked by what each costs you. | 1-2 |
| `instahyre_account_settings` | Visibility, notifications, blocked employers. | 1-2 |
| `instahyre_apply` | Apply to **one** opportunity. **Irreversible.** Preview-only unless `confirm=True`. | 0-1 |
| `instahyre_decline_opportunity` | Mark one "not interested". Also irreversible, same gate. | 0-1 |

### The inbox -- read-only, no browser

Recruiter conversations and message bodies. Every request is checked against a
list of mutating path fragments before it goes out, so this tier **cannot**
send, reply, star, or mark read.

| Tool | What it does | Requests |
|---|---|---|
| `instahyre_list_conversations` | Threads, with company and role joined in from the job. Filters: status, unread, starred, free text. | 1 (+1 per job if `include_job`) |
| `instahyre_read_conversation` | Every message in one thread as text, oldest first. A `conv_id` that is not his raises `not_found` rather than returning an empty thread. | 1 (+1 when the thread comes back empty) |
| `instahyre_inbox_counts` | Unread / starred / starred-unread totals. | 1 |

One honest caveat, stated in the tool's own docstring: **reading a thread may
mark it read on Instahyre's side.** The site sends no mark-read request -- it
decrements the badge locally -- which is only coherent if the server does it
when the messages are fetched. Listing conversations and counts is provably
non-mutating; fetching a thread is not provably non-mutating. It could not be
tested, because the inbox currently holds zero conversations.

### Profile writes -- these change your account

| Tool | What it does | Requests |
|---|---|---|
| `instahyre_update_skills` | Add skills. **Add-only**, snapshots first, verifies after. Preview unless `confirm=True`. | 2-4 |
| `instahyre_update_profile` | Set designation, company, or years of experience. Same gate. | 2-4 |
| `instahyre_restore_profile` | Put the skill list back to a snapshot. | 2-3 |
| `instahyre_list_profile_snapshots` | Restore points on disk. | 0 |
| `instahyre_verify_apply_target` | **Opens a browser.** Re-measures which endpoint an application would post to. | 0 (browser) |

### The captured write tier -- measured before it was built

Every request in this tier was recorded before a line of the tool was written.
Five other write surfaces were commissioned on 2026-08-23 and refused on the
spot, because not one of them had a recorded request body -- and a write with a
guessed body is worse than no tool. A wrong guess usually 400s harmlessly; a
half-right guess succeeds and does something nobody chose, and on this platform
the second case is permanent.

`scripts/capture_write_contracts.py` is how they were unblocked. It opens the
real signed-in browser, aborts every non-GET at the router, and **records what
it aborted** -- method, URL, body, headers. It refuses to drive anything until
it has fired a POST from inside the page and watched the router block it, so
"nothing was sent" is a measurement rather than an assumption.

| Tool | What it does | Contract measured |
|---|---|---|
| `instahyre_support_ticket` | Raise a ticket. A person reads it; there is no delete. Preview unless `confirm=True`. | **Wire** -- recorded and aborted |
| `instahyre_toggle_job_alert` | Alerts on/off for one saved search. Reversible. Sends the query string alongside the flag, like the site does. | Shipped source, whole functions |
| `instahyre_referral_link` | Ask for his own referral link. Contacts nobody. | Shipped source |
| `instahyre_referral_contacts` | Who Instahyre would offer as invitees. A **read** -- it is a GET in their client too. | Shipped source |
| `instahyre_send_referral_invites` | Invite people. **IRREVERSIBLE.** Preview names every recipient. | Shipped source |

`instahyre_send_referral_invites` is the one with real consequences: the mail
carries his name, it reaches people who know him, and Instahyre has no unsend
anywhere in its product. So `confirm=False` prints the full recipient list,
malformed addresses are refused rather than attempted, duplicates are removed
before the count, and one call will not send more than ten.

**Two surfaces are still not built, and the reason is in
`constants.UNVERIFIED_WRITE_SURFACES`.** A screening questionnaire can only be
opened by pressing Apply on a real opportunity -- the one action this server
must never take -- so the capture technique is closed off by the rule it exists
to serve. The workex PUT has no caller in any shipped bundle and no control on
the signed-in profile page; it appears to be onboarding-only, so there is
nothing to intercept. The profile-image contract WAS captured (it is JSON, not
multipart) but no tool is built on it: reproducing the browser's body needs a
WebP encoder at width<=800 that this package has no dependency for.

### Session

| Tool | What it does |
|---|---|
| `instahyre_login` | Email + password, over plain HTTP. No browser. |
| `instahyre_login_browser` | Opens a window for Google sign-in. One of three tools that start a browser. |
| `instahyre_auth_status` | Asks the server whether the session is live. Can honestly return `false`. |
| `instahyre_session_info` | What the credential is, when it expires, when the session lapses for good, and how to renew it -- including that a renew launches a headless browser and what that costs. `verify_live=False` costs no network and no browser. |
| `instahyre_reauth` | Silent renew from the browser profile -- headless, no password, no window, and it never visits the login page. Try this first when a tool says `auth_required`. Reports which of seven things went wrong when it cannot renew, and puts the previous session back byte for byte. |
| `instahyre_logout` | Clears the locally saved cookies. Leaves the browser profile alone, so `instahyre_reauth` usually gets straight back in. |

## What this platform does not have

Worth knowing before you go looking. Each of these is a measured absence, not an
unimplemented feature, and `instahyre_server_info` repeats them at runtime.

- **Salary: none.** Zero of 1,235 sampled records carried any pay field, and
  zero of 45 descriptions contained a pay figure (verified with positive
  controls). There is no pay channel on this API at all.
- **Posting dates: none.** No `posted_at`/`created_at` on any endpoint. Job ids
  are sequential, so `instahyre_sync_index` writing `first_seen` locally is the
  only freshness signal that will ever exist.
- **Sorting: inert.** The API accepts a `sort` parameter and demonstrably
  ignores it -- `sort=relevance`, `date`, `-id` all return an identical first
  page. No tool here offers a sort argument; `instahyre_rank_jobs` orders
  locally instead.
- **Applicant counts, company ratings: none.** No competition signal.
- **Hybrid vs onsite: not modelled.** `Work From Home` is the only remote token
  (~8.6% of the corpus); the arrangement of the rest is simply not in the data.
- **Saved or bookmarked jobs: none.** There is no bookmark feature. Saved
  *searches* exist (`/saved_job_searches`) and have no job-side equivalent.
- ~~**Message bodies: unreachable.**~~ **This entry was wrong and is kept as a
  correction.** It read: *"the message list demands a `conv_id` and no endpoint
  anywhere enumerates conversations, so threads must be read on the website."*
  The endpoint exists -- `/inbox_page/candidate_conversation` -- and answers
  plain `httpx`. The original search looked for a conversation resource beside
  the message resource in `/resume_modal/emails/`, found two 404s, and
  generalised from them. See `instahyre_list_conversations`. The lesson worth
  keeping: *"no endpoint does X"* is a claim about where you looked, and this
  file should say which namespaces were searched before it says a platform
  cannot do something.
- **Machine timestamps on recruiter activity: none.** `action_date` arrives
  pre-formatted for a human -- `"13 hours ago"`, `"Aug 17 at 3:47 PM"`. Read
  them; do not compute on them.
- **A per-opportunity detail route: none.** `candidate_matching/<id>` is a 400,
  not a 404. `instahyre_get_opportunity` finds a record by scanning the queue,
  which is why it composes rather than fetches.
- **Recruiter contact details: none.** Activity names the recruiter and their
  firm; there is no email or phone anywhere on the candidate API.

## Traps this client handles for you

Each of these was measured live, and each would otherwise be a silent wrong
answer.

- **`company_size` codes are not ordinal.** 1 is small, **2 is large, 3 is
  medium**. Proven by exact partition arithmetic (4022 + 2286 + 7147 = 13455 =
  the unfiltered total). Assuming 1/2/3 = small/medium/large mislabels every
  medium and large company. Tools take the words, never the codes.
- **Locations are case-sensitive.** `Bangalore` is 7,000+ jobs; `bangalore` is
  HTTP 400 "Invalid location". Every location goes through a resolver that
  corrects case and suggests near-misses.
- **`limit` has a floor as well as a ceiling.** `limit=1` returns 35 objects.
  There is no cheap count-only call; always read `meta.limit`.
- **Skills fail silently.** Locations, companies, industries, sizes and years
  are all validated server-side and 400 on a bad value. `skills` is not -- an
  unrecognised skill returns HTTP 200 with zero results, indistinguishable from
  a genuinely empty market. When a search comes back empty, the result carries a
  `diagnosis` naming which skill matched nothing.
- **A missing job id returns a 48 KB HTML page with a 404.** Never parsed;
  raised as a typed `not_found`.
- **The same role appears under several ids.** One 841-job sample held 41
  `(company, title)` pairs under multiple ids, one under seven. Results dedupe
  on id and annotate `duplicate_ids`.
- **`candidate_opportunity_employer/:id` is advertised on every search result
  and returns 404.** A dead reference; ignored.

### Traps on the authenticated tier

- **`status` on the queue is accepted and ignored.** It looks like the obvious
  filter name. `status=1` and `status=2` both returned the full unfiltered 228.
  The one that works is **`interest_facet`**, and it is the only one this client
  sends. A filter that lies is worse than one that errors.
- **The queue's filter spelling is not the search's.** Singular `location` and
  `industry_type` here; plural `jobLocations` and `industry_types` on
  `job_search`. Passing the search spelling filters *nothing* and looks like a
  wide result, so the two builders are deliberately not shared.
- **A bare queue request is HTTP 400 with an empty body** -- no field, no
  message. It needs an explicit `limit`. Every queue call sends one.
- **Two queue resources disagree.** `candidate_matching` returns 228,
  `candidate_opportunity` returns 238 (a strict superset), and
  `fetch_filter_counts` totals 238. The default matches the count the website
  itself shows; `include_unindexed=True` reaches the wider one.
- **Page-then-sort would lie.** The server returns the queue in its own order,
  so ranking a page and calling its top "the best match" reports the best of an
  arbitrary N. Measured: `limit=5` surfaced a 4.50 while a 16.05 sat further
  down the same queue. `instahyre_list_opportunities` fetches the whole queue,
  ranks it, then slices -- one request either way.
- **`is_strong_match` cannot be filtered on**, and says so loudly:
  `{"error": "The 'is_strong_match' field does not allow filtering."}`
- **`has_valid_number` is not `number_verified_at`.** One means the number
  passes format validation, the other means OTP verification actually happened.
  They disagree on this account, so the tools name them differently
  (`phone_format_valid` vs `phone_verified`) rather than letting two tools
  appear to contradict each other.
- **The activity feed's `employer` is the recruiting firm, not the hiring one.**
  Usually a staffing agency. `job.hiring_company_name` is who the role is for.
  Collapsing the two misattributes every event, so both are kept.

## The candidate id, recovered without a browser

Every profile and settings route is detail-only -- a GET on the collection is
HTTP 405 -- so none of them work without the numeric candidate id. And that id
is only ever server-injected into an authenticated HTML page, which is exactly
what a plain client cannot read: the HTML paths are Cloudflare-gated (403 to
`httpx`, a `"Just a moment..."` interstitial to headless Chromium) while
`/api/v1/*` is exempt.

That looked like it forced a browser into the data path.

It does not. `/candidate_misc/profile/education` **is** a collection, it **does**
answer GET, and every row carries its owner's `resource_uri`. One cheap request
recovers the id; it is then cached for 30 days. If a profile has no education
entry, `instahyre_get_profile` raises `candidate_id_unavailable` and says how to
fix it -- rather than returning an empty profile that would read as "you have
not filled anything in".

The endpoint map itself was recovered honestly: a browser was pointed at the
signed-in pages **once**, with every non-GET request aborted at the router, and
the XHRs it issued were recorded. Those paths are transcribed exactly as the
site's own app issues them -- which is why none of them carry a trailing slash.

## The agency filter

About 84% of Instahyre postings come from third-party staffing agencies rather
than the hiring company. The flag is free and exact -- a job detail carries
either `agency_function_names` or `job_function_names`, never both, and that key
choice agreed with `recruiter_company_name != hiring_company_name` in 45 of 45
sampled records.

The catch: it lives on the **detail** object, not the search result. So
`exclude_agencies=True` costs up to one extra request per job on the page.
Verdicts cache for 6 hours, and `instahyre_sync_index` pre-warms them.

## Safety

**Instahyre applications cannot be withdrawn.** Their FAQ says the application
is sent automatically by the system: there is no undo, no support path, and the
employer sees it immediately. Everything below follows from that one fact.

- **Apply is single-only, and irreversible.** `instahyre_apply` takes exactly
  one `opportunity_id`. There is no bulk-apply tool and there will not be one --
  Instahyre's API has `apply_bulk/`, and exposing it would make a single call
  irreversible across an entire queue. The forbidden paths are pinned in
  `constants.FORBIDDEN_ENDPOINTS`, and a test walks the package AST to prove no
  call site can construct one.

  **Both bulk URLs are blocked now, not one.** Instahyre has an ES and a legacy
  variant of every opportunity endpoint, and the forbidden list previously held
  only the legacy spelling -- while this account resolves to ES. The blocked
  path was the one that could never have been reached, and the reachable one was
  not blocked.

- **The apply request itself was wrong, and no application was ever sent with
  it.** The body was transcribed from Instahyre's shipped dispatcher and never
  executed. Re-reading that dispatcher independently found two errors:

  1. `enableCandidateESOpps` switches the `$resource` **service**, not just the
     body -- so the URL and the body's id key move together. The old code paired
     the ES body (`job_id`) with the legacy URL (`candidate_opportunity/apply/`),
     a combination the frontend never produces.
  2. `is_activity_page_job` is set on **every** call, on both branches, and was
     missing entirely.

  The branch in force is confirmed three independent ways: the dispatcher
  source, the `enableCandidateESOpps` flag read off the live page, and which
  service the page's own XHRs used. `instahyre_verify_apply_target` re-measures
  it and reports a MISMATCH rather than silently switching.

  This is the case for not testing an irreversible action by performing it: the
  contract was wrong for weeks, and finding that out cost nothing because
  nothing was ever sent.

- **Instahyre's own UI has no confirmation dialog on apply.** Every modal in
  that flow fires *after* the POST is accepted. The `confirm=True` gate here is
  a stricter guard than the website's.

- **The inbox tier cannot mutate.** Four inbox endpoints do -- send, star,
  toggle-read, and `mark_all_read` -- and every request is checked against them
  by substring first. `mark_all_read` deserves its own line: it is a **GET**
  that bulk-clears unread state, sharing a prefix with the list endpoint, so an
  ordinary "walk the resource and see what is under it" probe would wipe your
  unread flags with no request body and no warning.

- **Profile writes snapshot first and verify after.** A snapshot lands on disk
  *before* the request goes out, so a restore point survives the process dying
  mid-write. A 200 is never treated as success -- every write re-reads and
  compares, and reports `verified: false` with the difference if it does not
  match.

- **Skill writes echo every surviving row back byte-for-byte.** The skills
  resource is a **full replacement set** -- measured, by adding one canary skill
  and then sending a payload that omitted it, which deleted it. So any partial
  list is a deletion instruction, and the echo is what makes the payload's
  implicit claim ("these are all of them") true. `DELETE` on that resource
  answers **405, Allow: GET,PATCH**; a restore path that deleted rows
  individually was removed once that was known, because it could never have
  worked.

- **Removal is that same mechanism, deliberately.** `instahyre_update_skills`
  takes `remove=` as well as `add=`, and does both in ONE PATCH -- a skill
  leaves by not being copied into the payload. This exists because the platform
  caps the list at 20 and the account is AT the cap, so every addition is a
  swap, and `instahyre_skill_gap`'s `dead_weight_skills` names rows appearing in
  **zero** matched jobs while high-demand skills sit outside the list. The rails
  are in the code: names match exactly and case-insensitively (never as a
  substring, so removing "System Design" cannot take "System Design Patterns"),
  a name that is not on the profile is reported rather than silently ignored,
  adding and removing the same skill in one call is refused, and emptying the
  list is refused outright -- on a reverse marketplace a profile with no skills
  is not a short profile, it is an unfindable one. A removal that the server
  ignores is reported as `removal_did_not_take` rather than counted as success.
  Restoring a removed skill brings the NAME back under a NEW id: its original
  row is gone server-side, so it is re-sent in the new-skill shape rather than
  betting that the server tolerates a dead id.

- **`restore_profile` validates its own argument.** `snapshot_id` arrives from
  an agent-callable tool, so it is untrusted input naming a file. A probe with
  `"../not-a-snapshot"` read a file outside the snapshots directory, found no
  skills in it, and deleted all four. It now requires an id matching
  `[0-9]+-[a-z0-9-]+`, resolves inside the snapshots directory, and refuses a
  snapshot holding zero skills -- because restoring from an empty one is not a
  no-op, it is an instruction to delete everything.
- **Declining is equally final.** `instahyre_decline_opportunity` is the same
  endpoint with one boolean flipped, it is permanent, and it feeds Instahyre's
  matching algorithm. Same gate, same warnings.
- **`confirm=False` is the default and sends nothing.** It returns the exact
  request that would go out -- method, URL, body, headers -- plus the role,
  employer and match score, so a human can look before anything happens.
- **Four guards stand between `confirm=True` and a POST**, and each one can
  genuinely fail: the confirmation itself; a refusal to spend the same
  irreversible action twice on one opportunity; a live check that the target
  path is not on the forbidden list; and a refusal to send unsigned when the
  session carries no CSRF token. Each is covered by a test that was shown
  failing when the guard is removed.
- **The request shape was never learned by sending one.** It is transcribed
  from Instahyre's own shipped frontend dispatcher. No request of this shape has
  ever been executed by this package, which is stated in the tool docstring and
  in every preview.
- **Profile writes are preview-only, on purpose.** The read shape is verified;
  the write contract is not, and verifying it means writing to the live profile
  that generates every future match. A PATCH with a field shape slightly wrong
  could blank a field or return 200 having changed nothing -- and a silent no-op
  is the failure class this server exists to refuse. So
  `instahyre_preview_profile_update` shows the request and stops.
- **The confirm gate is advisory to the caller, not structural.** Nothing here
  can observe whether a human actually saw the preview; the distance between
  nothing and a permanent action is one boolean. Every docstring on the way in
  says to preview first.
- Requests are paced ~1.2s apart with jitter, retried with exponential backoff,
  and honour `Retry-After`. Personal volume only.
- Passwords are used for a single request and never logged, cached or written to
  disk. **Instahyre echoes password fields back in the settings payload**; they
  are stripped before anything is returned, cached or logged, and a test proves
  it against a fixture that still contains those keys. `_state/` (session
  cookies, index, browser profile) is gitignored, and committed fixtures are
  sanitised of personal data.

## State

`_state/` beside the package, or `$INSTAHYRE_HOME`:

- `instahyre.db` -- TTL cache, the job index (`first_seen`/`last_seen`), and the
  corpus tracker (`total_count`/`max_id` over time).
- `session.json` -- cookie jar. Never contains a password.
- `browser_profile/` -- persistent Chromium profile, Google sign-in only.

## Scoring

`instahyre_rank_jobs` and `instahyre_inbound_digest` score with the shared
[`jobcore`](https://github.com/Sundeepg98/jobcore) engine -- the same one the
Naukri and Uplers servers use -- so a fit score means the same thing on all
three boards. It is a hard dependency: there is **no local fallback scorer**,
and a missing jobcore is an `ImportError` naming the fix rather than a quietly
different number.

There used to be a fallback, behind a `sys.path` insert at `../jobcore/src`.
Both are gone, deliberately. The fallback did no skill aliasing at all, so
`"Node.js"` on a job never matched `"nodejs"` on the profile and every score it
produced was systematically low under the same `fit_score` key; it carried its
own `0.6/0.4` split and its own verdict bands, already drifted from jobcore's;
and once the weights became configurable it was a second, *unconfigurable*
engine shadowing the configurable one. The `sys.path` insert had its own
problem: it imports fine and installs nothing, so nothing pins its version and
`importlib.metadata` cannot even see it.

### The policy: `jobhunt.json`

The numbers are values, not literals. Weights, verdict bands, bonuses, the
experience curve and vocabulary additions live in a shared `jobhunt.json` that
all three servers read; edit it and the next call scores differently, with no
restart. `instahyre_config()` reports the effective policy, both of its
fingerprints, and which file it came from -- or, when there is none, every path
that was tried. `source: null` means built-in defaults, which is the shipped
behaviour.

Two scores are directly comparable only when their `scoring_hash` matches, which
is why every scored result carries one as soon as the policy stops being the
default. That hash covers the arithmetic alone -- weights, bonuses, caps, bands.
`policy_hash` is the wider one, covering scoring *and* the candidate block, and
it is what a config readout is identified by; a result cannot vouch for it,
because the candidate half is a call argument rather than a property of the
number. The readout prints both, and that pair is what matches a stored score
back to the config that produced it.

Pass `explain=True` to `instahyre_rank_jobs` or `instahyre_inbound_digest` to
get the working behind each score on its own row -- weights, the base
skills/experience split, every bonus and the cap, and the `scoring_hash` used.
It costs no extra request and is off by default, because the block is several
times the size of the row it explains.

What that file **cannot** do is grant this server any autonomy. `instahyre_apply`
and `instahyre_decline_opportunity` take `confirm=True` from a human every single
time, in source; there is no agent module and no scheduler here; and jobcore
refuses to *load* its Tier C keys (agent enablement, agent mode, apply
thresholds) from the file at all, naming each refusal in `tier_c_refusals`
rather than ignoring it silently.

`scripts/clean_install_check.py` fails outright if an install does not resolve
jobcore.

## Tests

576 tests, entirely offline -- every HTTP call goes through
`httpx.MockTransport` over golden fixtures captured from the live API, and an
unmocked path fails loudly rather than returning empty. Write paths are
exercised in mocked form only; **no test has ever sent a real application.**

`tests/test_inbound_safety.py` guards the irreversible half. It asserts the
absence of a POST, not merely the presence of an exception -- a test that only
checks the raise would pass against an implementation that sends first and
raises afterwards. It also walks the package AST to enumerate the write surface:
every `.post(` names its endpoint as a bare constant, and `.patch(` appears in
exactly one module.

`tests/test_scoring_policy.py` holds the config seam: 15 golden cases captured
from the pre-change scorer prove the defaults did not move, and the rest prove
a weight in `jobhunt.json` does. It has been run against a deliberately
permissive build -- one that accepts the policy and discards it, which is the
exact bug it exists to catch -- and 6 of its assertions go red there while the
parity cases stay green:

```bash
$env:PYTHONPATH="scripts"
venv/Scripts/python -m pytest tests/test_scoring_policy.py -p permissive_scorer_control
# 6 failed, 40 passed
```

`scripts/permissive_scorer_control.py` ships that plugin and explains which six
fail and why the other forty are supposed to survive it.

`tests/test_hardening.py` pins three defects that shipped green and were only
found by mutating the modules afterwards -- a restore that could empty the
profile, a withheld-message counter that could not count past one, and two
paging arguments that escaped the error taxonomy. Each test in it was re-run
against the re-introduced defect to confirm it actually goes red; a test that
has never been shown failing is a claim, not a measurement.

```bash
venv/Scripts/python -m pytest tests/ -q
```

### Checking a CLEAN install

```bash
venv/Scripts/python scripts/clean_install_check.py
```

Clones the committed tree into a throwaway workspace, builds a brand new venv,
runs the Install recipe above from scratch, imports the server and runs the
suite -- then deletes the workspace. Your working tree and your venv are never
touched.

Run it after touching `requirements.txt` or `pyproject.toml`, and before
believing a green local suite. **A local venv is a cache of a resolve that
happened in the past**, and it cannot show you what a resolve today would
produce. On 2026-08-20 the sibling naukri server declared `mcp[cli]>=1.25.0`
unbounded; `mcp 2.0.0` moved `mcp/server/fastmcp` to `mcp/server/mcpserver`, a
clean resolve picked it up, and all 55 of naukri's test modules died at
collection -- *"5 deselected, 55 errors"*, zero tests run -- while every local
run stayed green on a venv holding mcp 1.26.0 from before 2.0.0 shipped.

This server was **not** affected by that particular move, and that was measured
rather than assumed: it depends on the standalone `fastmcp` project, and after
`import instahyre_server.server` the loaded modules include `mcp.server.auth`
and `mcp.server.models` but **not** `mcp.server.fastmcp`. fastmcp additionally
caps its own dependency at `mcp<2.0`. What it did share was the disease --
an unbounded `>=` on its framework package -- so `fastmcp` is now capped at the
next untested major (`<4`; the suite is measured green on 3.4.7), and
`tests/test_requirements_pins.py` holds that in place by reading
`requirements.txt` and `pyproject.toml` as text. An assertion about the
*installed* version would pass happily in the very venv that hides the bug.
