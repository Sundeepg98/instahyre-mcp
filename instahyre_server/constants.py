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
EP_MESSAGE_COUNT = "/resume_modal/emails/message/message_count"
# VERIFIED: this path answers only WITH the unread flag. Without it, Cloudflare
# returns an HTML 403 -- which the client correctly raises as ChallengeDetected.
MESSAGE_COUNT_PARAMS = {"unread": 1}

# THE CONVERSATION LIST EXISTS. An earlier build of this server concluded that
# "no endpoint enumerates conversations" and shipped the unread count as the
# only inbox signal. That conclusion was WRONG, and the way it was reached is
# worth recording: it searched the /resume_modal/emails/ namespace (where the
# message resource lives) for a sibling conversation resource, found
# .../conversation and .../conversations both 404, and generalised from two
# misses to "the platform cannot do this".
#
# The resource is in a DIFFERENT namespace entirely. Found 2026-08-21 by
# opening the inbox in the authenticated browser and recording its XHRs -- the
# page cannot render without it, so it had to exist somewhere.
#
# VERIFIED live over plain httpx on the same day: 200, tastypie {meta, objects}.
# It is an /api/v1/* path, so it is Cloudflare-exempt like the rest of the API
# and needs NO browser. The browser found it; the browser is not needed to use
# it.
EP_CONVERSATIONS = "/inbox_page/candidate_conversation"
EP_CONVERSATION_COUNT = "/inbox_page/candidate_conversation/count"

# The thread body endpoint. VERIFIED live: a bare GET returns HTTP 400
# {"conv_id": ["This field is required."]} -- the endpoint names its own
# contract. conv_id is the ONLY parameter the frontend ever sends; the whole
# thread comes back in one call, unpaginated.
EP_MESSAGES = "/resume_modal/emails/message"

# Transcribed from the inbox controller's buildFilters(). Four filters exist
# besides limit/offset, and the EMISSION RULES matter as much as the names:
#
#   status   -- 1 or 2 ONLY. "All conversations" is 0, which is falsy, so the
#               app never sends status=0; it omits the key. We do the same,
#               because a value the frontend never emits is a value nobody has
#               ever seen the server handle.
#   unread   -- literal true ONLY. Never sent as false; the key is omitted.
#   starred  -- literal true ONLY. Same rule. Mutually exclusive with unread
#               in the UI, though the desktop branch shows the server accepts
#               either alongside status.
#   query    -- free text search.
CONV_STATUS = {"in_process": 1, "closed_by_recruiter": 2}
CONV_STATUS_NAMES = {1: "in process", 2: "closed by recruiter"}
CONV_PARAMS = frozenset({"status", "unread", "starred", "query", "limit", "offset"})
CONV_DEFAULT_LIMIT = 10

# A conversation record carries NO company, recruiter or subject field -- the
# site joins those in from GET /employer_public_jobs/{job_id}. Verified by
# enumerating every property access on conv/selectedConv across all eight
# frontend bundles: 11 distinct names, none of them a company or a person.
CONV_FIELDS = frozenset(
    {
        "id",
        "resource_uri",
        "job_id",
        "opportunity_id",
        "is_latest_msg_read",
        "is_starred",
        "latest_message",
        "latest_msg_at",
    }
)

# The message record's direction flag is `is_owner`, NOT `is_from_candidate`.
# `content_html` is the body. `show_message` is a HARD GATE: the site's render
# loop `break`s on the first falsy one and discards it and everything after,
# so the API can return more than the UI shows.
MSG_BODY_FIELD = "content_html"
MSG_DIRECTION_FIELD = "is_owner"

# ===========================================================================
# THE READ-ONLY INBOX CONTRACT
# ===========================================================================
#
# Four inbox endpoints MUTATE. None of them is ever constructed by this
# package, and _guard_read_only() below refuses any path that names one.
#
# mark_all_read is the trap, and it is worth spelling out: it is a **GET** that
# bulk-clears unread state, and it sits on the SAME resource prefix as the list
# call. A "just GET everything under this resource and see what it does" probe
# -- an entirely reasonable-looking way to explore an API -- would silently
# wipe his unread flags with no request body and no warning.
MUTATING_INBOX_PATHS = frozenset(
    {
        "/inbox_page/candidate_conversation/mark_all_read",
        "/resume_modal/emails/message/send_message/",
        "/resume_modal/emails/message/star_conversation",
        "/resume_modal/emails/message/toggle_message_read",
    }
)

#: Substrings that may never appear in any path this package requests. Checked
#: as a substring test, not equality, so a query string or a trailing slash
#: cannot smuggle one past.
MUTATING_PATH_MARKERS = (
    "mark_all_read",
    "send_message",
    "star_conversation",
    "toggle_message_read",
    "apply_bulk",
)

# --- The one-way door ------------------------------------------------------
#
# Instahyre's own FAQ: an application "is sent automatically by the system, so
# it cannot be withdrawn". Declining is equally final. Both actions POST here.
EP_APPLY = "/candidate_opportunities/candidate_opportunity/apply/"

# A SECOND, INDEPENDENT READING OF THE DISPATCHER CORRECTED THIS. 2026-08-21.
#
# The previous transcription was:
#
#   var data={"is_interested":choice}
#   if($scope.enableCandidateESOpps){data.job_id=opp.job.id;}
#   else{data.id=opp.id||null; if($scope.showSearchedJobs||!opp.id){data.job_id=opp.job.id;}}
#
# posted to /candidate_opportunities/candidate_opportunity/apply/. Two errors,
# both of which would have gone out on the first real application:
#
# 1. THE URL IS NOT CONSTANT. enableCandidateESOpps switches the SERVICE, not
#    just the body. The frontend has two $resource factories and the flag picks
#    between them, so the ES body (job_id) is only ever posted to the ES URL:
#
#      flag false -> candidateOpportunitiesService -> .../candidate_opportunity/apply/
#      flag true  -> candidateMatchingService      -> .../candidate_matching/apply/
#
#    The old constant pinned the non-ES URL while the old body builder emitted
#    the ES shape. That pairing is one the frontend never produces.
#
# 2. A BODY KEY WAS MISSING. data.is_activity_page_job is set unconditionally
#    on every call, on both branches, immediately before the POST. It is true
#    only when the page was opened deep-linked from the activity page with
#    matching ?opp_id and ?job_id, so for every apply this server would make it
#    is false.
#
# WHICH BRANCH IS HIS ACCOUNT ON? ES. That is measured, not assumed: the
# opportunities page for this account fetches /candidate_matching and
# /candidate_matching/fetch_filter_counts, and those two URLs are built only by
# candidateMatchingService -- candidateOpportunitiesService spells the second
# one /candidate_opportunity/{id}/fetch_filter_counts, which is not what the
# wire showed. The queue this server reads (EP_OPPORTUNITIES) is the same
# candidate_matching resource, which is the same fact from the other side.
EP_APPLY_ES = "/candidate_opportunities/candidate_matching/apply/"
EP_APPLY_LEGACY = "/candidate_opportunities/candidate_opportunity/apply/"

#: The branch this account is on. instahyre_verify_apply_target re-measures it
#: from the live page and says so if it ever disagrees with this default.
APPLY_BRANCH_ES = True

#: Kept as the legacy alias so nothing that imported it breaks, and pointed at
#: the branch actually in force.
EP_APPLY = EP_APPLY_ES

APPLY_IS_INTERESTED_APPLY = True
APPLY_IS_INTERESTED_DECLINE = False

# VERIFIED from the frontend's global $http config, not guessed:
#   $httpProvider.defaults.xsrfHeaderName="X-CSRFToken"
#   $httpProvider.defaults.xsrfCookieName='csrftoken'
#   defaults.headers.post["Content-Type"]="application/json;"
# Note the literal trailing semicolon on the content type. We send the ordinary
# "application/json" instead -- deviating here is safe (the semicolon is an
# empty parameter list, which is what Angular emits by accident) and matching
# it exactly would be cargo-culting a bug.
APPLY_CSRF_HEADER = "X-CSRFToken"

# BOTH bulk endpoints, not one. The ES/non-ES split applies here too, and the
# earlier list held only the non-ES spelling -- so the ES bulk URL, which is
# the one this account's branch would actually resolve, was NOT blocked.
# Bulk has no is_interested key: it is apply-only, and one call is an
# irreversible mass-apply across a whole queue.
FORBIDDEN_ENDPOINTS = frozenset(
    {
        "/candidate_opportunities/candidate_opportunity/apply_bulk/",
        "/candidate_opportunities/candidate_matching/apply_bulk/",
    }
)

# --- Profile writes --------------------------------------------------------
#
# Skills do NOT ride the profile PATCH. They have their own resource, and the
# naming is a trap: `candidate_skills` does NOT carry the skills (it carries
# the job-search-profile object), while `candidate_skill_model` does.
#
# The site's skills editor fires TWO requests -- PATCH candidate_skill_model
# with the whole array, then, in a .finally(), PUT candidate_skills/{jsp_id}
# with the entire jsp object. THIS PACKAGE SENDS ONLY THE FIRST. The second
# carries no skills; it re-PUTs the job-search profile, and on the way its
# saveCareerBreakFields() NULLs career_break_start_date and career_break_reason
# whenever career stage is not CAREER_BREAK. Skipping it is a deviation from
# browser behaviour in the SAFE direction: strictly fewer fields touched.
EP_SKILL_MODEL = "/candidate_misc/profile/candidate_skill_model"

# VERIFIED live 2026-08-21: GET answers 200 both with and without the trailing
# slash, and this client does not follow redirects, so neither spelling is
# hiding a 301.
#
# TWO SEMANTICS MEASURED ON THE SAME DAY, both by adding one canary skill and
# watching what happened to it:
#
#   PATCH {"objects": [...]} IS A FULL REPLACEMENT SET. A row omitted from the
#   list is DELETED. This was previously derived-but-unproven, and it is the
#   reason every write in profile_write.py echoes the existing rows back
#   verbatim: a partial list is a deletion instruction.
#
#   DELETE on a detail route answers 405 with `Allow: GET,PATCH`. The verb does
#   not exist on this resource. A restore path that tried to delete rows
#   individually was removed once this was known -- it could never have worked.
SKILL_MODEL_IS_REPLACEMENT_SET = True
SKILL_MODEL_ALLOWED_METHODS = ("GET", "PATCH")
#
# A server-returned skill element has exactly four keys:
#   {"resource_uri": ".../candidate_skill_model/<id>",
#    "candidate": ".../candidate/<candidate_id>", "id": <int>, "name": "RabbitMQ"}
# A newly-added one, as the frontend builds it, has no id and no resource_uri:
#   {"candidate": ".../candidate/<candidate_id>", "name": "Redis"}
SKILL_ELEMENT_KEYS = frozenset({"resource_uri", "candidate", "id", "name"})

# READ from the bundle: constant("CANDIDATE_MAX_SKILLS_COUNT",20), enforced
# client-side in two places with the message "You cannot add more than 20
# skills", and maxTagLength:50 on the widget. This is a platform ceiling, not
# our choice -- a resume with 32 skills does not fit and must be prioritised.
MAX_SKILLS = 20
MAX_SKILL_NAME_CHARS = 50

# The sparse-PATCH route for scalar profile fields. VERIFIED as genuinely
# sparse: four independent frontend call sites PATCH a single key and read the
# same key back, with no other field disturbed. All four are scalars, so
# "PATCH is sparse" is proven for scalars and merely assumed for collections --
# which is one more reason skills go through their own resource instead.
EP_PROFILE_PATCH = "/candidate_misc/profile/candidate/{candidate_id}"

TTL_CONVERSATIONS = 2 * 60

TTL_ACTIVITY = 5 * 60
TTL_PROFILE = 15 * 60
