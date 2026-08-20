"""Wire-level constants for the Instahyre API.

Every value here was VERIFIED against the live API on 2026-08-20. Where a
value was inferred rather than measured, the comment says so.
"""

from __future__ import annotations

API_BASE = "https://www.instahyre.com/api/v1"
SITE_BASE = "https://www.instahyre.com"

# The honest User-Agent. Instahyre's Cloudflare rule string-matches UAs on HTML
# paths only; /api/v1/* is exempt entirely and answers with no UA at all.
# Forging a browser UA buys nothing here and their terms prohibit it, so we
# identify ourselves truthfully. Do not "fix" this by pasting a Chrome UA.
USER_AGENT = "instahyre-mcp/1.0 (+personal job-search automation; httpx)"

# --- Endpoints -------------------------------------------------------------

EP_JOB_SEARCH = "/job_search/"
EP_JOB_DETAIL = "/employer_public_jobs/{job_id}/"
EP_JOB_FUNCTION = "/job_function/"
EP_INDUSTRY_TYPE = "/industry_type/"
EP_LOCATION_DATA = "/candidate_misc/profile/candidate_locations/location_data/"
EP_EMPLOYER_ANON = "/employer_misc/employer_profile/anon_employer_limited/{employer_id}/"
EP_JOB_CATEGORY = "/job_category/"  # 401 {"logged_out": true} when anonymous
EP_LOGIN = "/users/user_login"

# --- job_search request contract -------------------------------------------

# VERIFIED: limit has both a FLOOR and a ceiling of 35. limit=1 returns 35
# objects with meta.limit == 35. There is therefore no cheap count-only call --
# every "just give me the total" costs a full page. Always read meta.limit.
PAGE_SIZE = 35

# THE BAND CODES ARE NOT ORDINAL. 2 is *large* and 3 is *medium*. Assuming
# 1/2/3 == small/medium/large silently mislabels every medium and large
# company, which is exactly the kind of bug that never announces itself.
#
# VERIFIED by exact integer identity, not inference: each band's total_count
# (4022 / 7147 / 2286) equals exactly one of the three distinct values in
# meta.company_size_count (small 4022, medium 2286, large 7147), so the
# assignment is unique. Cross-check: 4022 + 2286 + 7147 == 13455 == the
# unfiltered total, so the bands partition the corpus.
# 0 behaves as "no filter". Any other integer -> 400 "Invalid company size".
COMPANY_SIZE = {"any": 0, "small": 1, "large": 2, "medium": 3}
COMPANY_SIZE_NAMES = {0: "any", 1: "small", 2: "large", 3: "medium"}

# employee_count bucket -> size band. DERIVED from 420 sampled records across
# the three bands (140 each) with zero cross-band overlap.
EMPLOYEE_BUCKET_TO_BAND = {1: "small", 10: "small", 50: "medium", 200: "large", 500: "large", 1000: "large"}

# VERIFIED: 0 -> everything, 1 -> full_time, 2 -> internship.
JOB_TYPE = {"any": 0, "full_time": 1, "internship": 2}
JOB_TYPE_NAMES = {0: "any", 1: "full_time", 2: "internship"}

# Multi-value filters are ORed with dedupe (VERIFIED: skills=Node.js -> 836,
# skills=Node.js&skills=TypeScript -> 1349 in the same slice).
MULTI_VALUE_PARAMS = frozenset({"skills", "job_functions", "jobLocations", "industry_types"})

# The full accepted parameter set, taken from the search controller's
# sidebarFilterFields and confirmed live.
SEARCH_PARAMS = frozenset(
    {
        "skills",
        "job_functions",
        "job_type",
        "years",
        "industry_types",
        "companies",
        "jobLocations",
        "company_size",
        "limit",
        "offset",
    }
)

# VERIFIED INERT: sort=relevance|date|-date|id|-id|score all return an
# identical first page against a fixed control query. The API accepts the
# parameter and ignores it. We deliberately do NOT expose a sort argument --
# offering one would be a lie. Ordering is applied locally instead.
SORT_IS_SUPPORTED = False

# --- Which filters fail loudly, and which fail silently --------------------
#
# VERIFIED: bad values on these four come back as HTTP 400 with a tastypie
# error dict, e.g. {"job_locations": ["Invalid location"]}. Note the response
# key does NOT always match the request key -- jobLocations -> job_locations.
VALIDATED_PARAMS = {
    "jobLocations": "job_locations",
    "companies": "companies",
    "industry_types": "industry_types",
    "company_size": "company_size",
    "years": "years",
}

# VERIFIED SILENT: an unknown skill returns HTTP 200 with total_count 0. This
# is the platform's one silent-empty failure mode and the reason
# diagnose_empty_result() exists.
SILENT_PARAMS = ("skills",)

# --- Bands published by the frontend bundle --------------------------------

INSTAMATCH_SCORES = {"high": 100, "medium": 50, "low": 20}
OPP_FETCH_STATUS = {
    0: "FETCHING",
    1: "CALCULATING",
    2: "NO_OPPORTUNITIES",
    3: "NO_MATCHES",
    4: "SHOW_OPPORTUNITIES",
}

# employee_count on a search result's employer object is a bucketed enum, not a
# headcount. These are the observed bucket floors.
EMPLOYEE_COUNT_BUCKETS = (1, 10, 50, 200, 500, 1000)

# --- Pacing ----------------------------------------------------------------
#
# Deliberately conservative: personal volume, indistinguishable from a human on
# a fast connection. No throttling was ever observed but no RateLimit-* headers
# exist either, so there is no signal to ride up against.
DEFAULT_MIN_INTERVAL_S = 1.2
DEFAULT_TIMEOUT_S = 30.0
MAX_RETRIES = 3

# --- Cache TTLs (seconds) --------------------------------------------------

TTL_TAXONOMY = 30 * 24 * 3600
TTL_SEARCH = 15 * 60
TTL_DETAIL = 6 * 3600
TTL_OPPORTUNITIES = 5 * 60
