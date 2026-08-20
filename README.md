# Instahyre MCP

Job search over Instahyre's public API, exposed as MCP tools. No browser in the
data path.

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

- **There is no bulk-apply tool and there will not be one.** Instahyre's FAQ is
  explicit that an application **cannot be withdrawn** once sent. Automating the
  one irreversible, reputation-bearing action is the wrong optimisation.
- Any destructive or irreversible action requires an explicit confirmation
  argument that defaults to not acting.
- Requests are paced ~1.2s apart with jitter, retried with exponential backoff,
  and honour `Retry-After`. Personal volume only.
- Passwords are used for a single request and never logged, cached or written to
  disk. `_state/` (session cookies, index, browser profile) is gitignored.

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

249 tests, entirely offline -- every HTTP call goes through
`httpx.MockTransport` over golden fixtures captured from the live API, and an
unmocked path fails loudly rather than returning empty.

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
