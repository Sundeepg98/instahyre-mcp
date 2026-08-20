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


# ===========================================================================
# AUTHENTICATED TIER
# ===========================================================================
#
# Everything below was VERIFIED live against the operator's own session on
# 2026-08-20. The endpoint paths were not guessed and were not read out of a
# bundle: a browser was pointed at the signed-in candidate pages once, with
# every non-GET request aborted at the router, and the XHRs it made were
# recorded. That is why these paths carry NO trailing slash -- they are
# transcribed exactly as the site's own app issues them, and the HTTP client
# does not follow redirects, so a slash added "for tidiness" turns a 200 into
# an unexplained 301.

# --- The inbound queue -----------------------------------------------------
#
# TWO resources serve the same queue and they DISAGREE.
#
#   candidate_matching     -> 228 records. This is what the website shows, and
#                             what the navbar badge counts.
#   candidate_opportunity  -> 238 records, a strict superset (the extra 15 are
#                             ordinary strong matches the search index has
#                             dropped), and it is also what fetch_filter_counts
#                             totals.
#
# We default to candidate_matching so a count here always equals the count he
# sees on the site. The extra 15 are reachable, deliberately, behind a flag.
EP_OPPORTUNITIES = "/candidate_opportunities/candidate_matching"
EP_OPPORTUNITIES_FULL = "/candidate_opportunities/candidate_opportunity"
EP_OPP_FILTER_COUNTS = "/candidate_opportunities/candidate_opportunity/fetch_filter_counts"
EP_OPP_NAVBAR_COUNT = "/candidate_opportunities/candidate_matching/fetch_navbar_count"
# The sibling-roles sub-path. VERIFIED: the leading slash is required on BOTH
# resources. The frontend builds one of them without it; that variant 400s.
EP_OPP_SIBLINGS = (
    "/candidate_opportunities/candidate_opportunity/{opportunity_id}/opps_from_this_company"
)
EP_SAVED_SEARCHES = "/candidate_opportunities/saved_job_searches"

# VERIFIED: a bare GET on either queue resource returns HTTP 400 with an EMPTY
# body -- no field name, no message. Sending an explicit limit fixes it.
# Never issue a queue request without one.
OPP_LIMIT_REQUIRED = True
OPP_DEFAULT_LIMIT = 30
OPP_MAX_LIMIT = 1000

# The queue's filter contract is NOT job_search's. Singular "location" and
# "industry_type" here vs plural "jobLocations"/"industry_types" there;
# passing the search spelling silently filters nothing. Do not share a builder.
OPP_PARAMS = frozenset(
    {"interest_facet", "location", "industry_type", "company_size", "job_type", "limit", "offset"}
)

# VERIFIED: "interest_facet" is the queue's status filter and it WORKS.
# "status", which reads like the obvious name, is accepted and IGNORED --
# status=1 and status=2 both returned the unfiltered 228. A filter that lies is
# worse than one that errors, so only interest_facet is ever sent.
INTEREST_FACET = {"pending": 0, "interested": 1, "not_interested": 2}
INTEREST_FACET_NAMES = {0: "pending", 1: "interested", 2: "not_interested"}

# "interview_status" on a queue record. Mirrors interest_facet.
INTERVIEW_STATUS_NAMES = {0: "no action taken", 1: "you expressed interest", 2: "you declined"}

# VERIFIED loud: filtering on is_strong_match returns
# {"error": "The 'is_strong_match' field does not allow filtering."}
OPP_UNFILTERABLE = frozenset({"is_strong_match", "is_location_match", "score", "reviewed_at"})

# --- Recruiter activity ----------------------------------------------------
#
# The single most perishable signal on the platform: who looked at you, when.
EP_ACTIVITY = "/candidate_misc/activity/employer_activity"
EP_ACTIVITY_COUNTS = "/candidate_misc/activity/employer_activity/fetch_facet_counts"

# The tab labels are taken verbatim from the rendered activity page, not
# inferred from the integers. VERIFIED: omitting activity_facet returns
# 400 "Request is missing the activity_facet or its value is invalid."
ACTIVITY_FACET = {"viewed": 0, "contacted": 1, "not_shortlisted": 2}
ACTIVITY_FACET_NAMES = {0: "viewed", 1: "contacted", 2: "not_shortlisted"}
ACTIVITY_FACET_LABELS = {
    0: "viewed your resume",
    1: "contacted you",
    2: "did not shortlist you",
}

# --- Profile, settings, and the candidate id -------------------------------
#
# Every profile route is DETAIL-ONLY: a GET on the collection is HTTP 405, so
# nothing works without the numeric candidate id.
#
# The id is server-injected into an authenticated HTML page, and the HTML paths
# are Cloudflare-gated -- 403 to a plain client, "Just a moment..." to headless
# Chromium. That looked like it forced a browser into the data path.
#
# It does not. "/candidate_misc/profile/education" is a COLLECTION that does
# answer GET, and every row carries the owning candidate's resource_uri. One
# cheap request recovers the id, and it is cached from then on. This is the
# reason the authenticated tier still needs no browser.
EP_PROFILE = "/candidate_misc/profile/candidate/{candidate_id}"
EP_SETTINGS = "/candidate_misc/settings/candidate_settings/{candidate_id}"
EP_EDUCATION = "/candidate_misc/profile/education"
EP_PRIMARY_SKILL = "/candidate_misc/profile/primary_skill"

# Settings GET echoes the account's password fields back in the payload. They
# are stripped before anything is returned, logged or cached. Never widen this.
SETTINGS_NEVER_EMIT = frozenset({"password", "current_password", "confirm_password"})

# Personal contact details. Real, his, and of no use to an agent choosing a
# job -- so they are summarised as present/absent rather than echoed.
CONTACT_FIELDS = frozenset({"phone", "alternate_phone", "email", "name"})

# VERIFIED: 0 is what this account reads today. The remaining codes are NOT
# known -- the labelled dropdown lives on an authenticated page whose bundle is
# not in the captured corpus. The tool reports the integer and says so.
JOB_SEARCH_STATUS_KNOWN = {0: "actively looking (default)"}

# --- Messages --------------------------------------------------------------
#
# The site has an INBOX. Only its unread COUNT is reachable: the message list
# demands a "conv_id" and NO endpoint enumerates conversations (both
# /resume_modal/emails/conversation and .../conversations are 404).
EP_MESSAGE_COUNT = "/resume_modal/emails/message/message_count"
# VERIFIED: this path answers only WITH the unread flag. Without it, Cloudflare
# returns an HTML 403 -- which the client correctly raises as ChallengeDetected.
MESSAGE_COUNT_PARAMS = {"unread": 1}

# --- The one-way door ------------------------------------------------------
#
# Instahyre's own FAQ: an application "is sent automatically by the system, so
# it cannot be withdrawn". Declining is equally final. Both actions POST here.
EP_APPLY = "/candidate_opportunities/candidate_opportunity/apply/"

# Transcribed from the shipped frontend dispatcher, which builds ONE body for
# both actions and switches on a single boolean:
#
#   var data={"is_interested":choice}
#   if($scope.enableCandidateESOpps){data.job_id=opp.job.id;}
#   else{data.id=opp.id||null; if($scope.showSearchedJobs||!opp.id){data.job_id=opp.job.id;}}
#
# Sending both keys is what the legacy branch does when it has both, so that is
# what we would send. NOTHING in this package has ever executed this request:
# the shape comes from reading their code, never from watching a response.
APPLY_IS_INTERESTED_APPLY = True
APPLY_IS_INTERESTED_DECLINE = False

# apply_bulk/ exists on the API. It is permanently out of scope and no code
# path in this package may construct it.
FORBIDDEN_ENDPOINTS = frozenset({"/candidate_opportunities/candidate_opportunity/apply_bulk/"})

TTL_ACTIVITY = 5 * 60
TTL_PROFILE = 15 * 60
