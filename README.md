# Instahyre MCP

Instahyre as MCP tools: public job search, plus the authenticated inbound side
that is the actual point of this platform. No browser in the data path, on
either tier.

Instahyre is a **reverse marketplace** -- employers put you in a curated queue
and recruiters open your resume. Outbound search is the commodity half; the
scarce signal is who engaged, and how fast you notice. `instahyre_inbound_digest`
is the tool that answers that in one call.

## Why this one has no browser

Instahyre's `/api/v1/*` is exempt from Cloudflare bot management. It answers a
cold, unauthenticated, honestly-identified HTTP client on the first request --
no cookie, no token, no JS challenge. Measured across ~300 live requests: zero
throttling, zero challenges, on the default `python-httpx` User-Agent.

So this server is `httpx` end to end. Playwright appears in exactly one module
(`auth.py`) and only to let a human complete a Google sign-in. Everything else
-- including email/password login -- is plain HTTP, because Instahyre hands out
a CSRF token on every API response.

That is the deliberate architectural difference from the sibling Naukri server,
which needs a persistent Chrome profile, a CDP bridge and ~1,300 lines of
anti-bot plumbing. None of that is needed here.

**We do not spoof a browser User-Agent.** It buys nothing (the API accepts the
honest one), and Instahyre's terms prohibit forging headers. If the API
exemption is ever removed, the client raises a typed `ChallengeDetected` -- that
is a signal to stop and reassess, not to route around.

## Install

```bash
python -m venv venv
venv/Scripts/python -m pip install -r requirements.txt
venv/Scripts/python -m pytest tests/ -q
```

Playwright's browser binary is only needed for `instahyre_login_browser`:

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
| `instahyre_rank_jobs` | Search, then rank one page by fit against your own skills. | 1+ |
| `instahyre_sync_index` | Page a slice into the local index and report what is NEW since last run. | 1/page |
| `instahyre_list_job_functions` | The 58 job functions with ids. | 1 (cached 30d) |
| `instahyre_list_locations` | The accepted location tokens, grouped. | 1 (cached 30d) |
| `instahyre_list_industries` | The 74 industry types with ids. | 1 (cached 30d) |
| `instahyre_server_info` | Cache state, request count, and what this platform cannot provide. | 0 |

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
| `instahyre_preview_profile_update` | Builds the profile update that would be sent -- and deliberately never sends it. | 1-2 |

### Session

| Tool | What it does |
|---|---|
| `instahyre_login` | Email + password, over plain HTTP. No browser. |
| `instahyre_login_browser` | Opens a window for Google sign-in. The only tool that starts a browser. |
| `instahyre_auth_status` | Asks the server whether the session is live. Can honestly return `false`. |
| `instahyre_logout` | Clears the locally saved cookies. |

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
- **Message bodies: unreachable.** The site has an inbox, and the API publishes
  its unread *count*. The message list demands a `conv_id` and no endpoint
  anywhere enumerates conversations, so threads must be read on the website.
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

## The candidate id, and why there is still no browser

Every profile and settings route is detail-only -- a GET on the collection is
HTTP 405 -- so none of them work without the numeric candidate id. And that id
is only ever server-injected into an authenticated HTML page, which is exactly
what a plain client cannot read: the HTML paths are Cloudflare-gated (403 to
`httpx`, a `"Just a moment..."` interstitial to headless Chromium) while
`/api/v1/*` is exempt.

That looked like it forced Playwright into the data path and would have cost
this server its defining property.

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
  irreversible across an entire queue. The forbidden path is pinned in
  `constants.FORBIDDEN_ENDPOINTS`, and a test walks the package AST to prove no
  call site can construct it.
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

`instahyre_rank_jobs` uses the shared `jobcore` engine (the platform-agnostic
scorer extracted from the Naukri server) when importable, so scores are
comparable across boards. It falls back to a small local scorer otherwise, and
the output always names which engine produced the number.

Two things worth knowing about that fallback. It is chosen by *what is
importable*, so a checkout **without** a `../jobcore` sibling -- a fresh clone,
or a CI runner -- scores with the local fallback, and those numbers are not
comparable with a jobcore-backed board. And `pip install -r requirements.txt`
does not install jobcore (a `jobcore @ git+...` line there would silently
uninstall a local `pip install -e ../jobcore`). To get comparable scores:

```bash
venv/Scripts/python -m pip install -e ../jobcore
```

`scripts/clean_install_check.py` prints which engine an install resolved to.

## Tests

357 tests, entirely offline -- every HTTP call goes through
`httpx.MockTransport` over golden fixtures captured from the live API, and an
unmocked path fails loudly rather than returning empty. Write paths are
exercised in mocked form only; no test has ever sent a real application.

`tests/test_inbound_safety.py` is the file guarding the irreversible half. It
asserts the absence of a POST, not merely the presence of an exception -- a test
that only checks the raise would pass against an implementation that sends first
and raises afterwards.

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
