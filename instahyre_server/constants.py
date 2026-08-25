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

# --- Saved searches, and what a "job alert" actually is ---------------------
#
# READ out of the frontend bundle, not inferred from the name: searchjobs.html
# binds toggleSavedJobSearchAlerts($event) on a saved-search row, and the rows
# carry job_alert_enabled_at. Three consequences, each of which contradicts a
# reasonable-sounding assumption about how alerts usually work:
#
#   AN ALERT IS NOT AN OBJECT. It is a boolean toggle on a saved search. There
#   is no alerts resource to list, no alert id to hold, and nothing to create
#   or delete -- only a search whose flag is on or off.
#
#   THE TOGGLE IS GATED ON FILTER COUNT. A search carrying fewer than three
#   filters cannot have alerts switched on at all.
#
#   THERE IS NO FREQUENCY ANYWHERE. No daily, no weekly, no digest, no field
#   for one in any of the bundles. A tool that offered a frequency would be
#   describing a different product.
#
# The account this server runs against has ZERO saved searches (VERIFIED live
# 2026-08-21: {"meta": ..., "objects": []}), so the populated shape has never
# been seen on the wire and no record field beyond job_alert_enabled_at has any
# evidence behind it. That is why saved_searches() forwards each row whole
# instead of renaming it into a shape nobody has measured.
MAX_SAVED_SEARCHES = 5
SAVED_SEARCH_ALERT_MIN_FILTERS = 3
SAVED_SEARCH_ALERT_FIELD = "job_alert_enabled_at"
SAVED_SEARCH_HAS_FREQUENCY = False

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

# The uploaded resume. BOTH halves measured live on 2026-08-21, in one sitting,
# because only the pair settles where the id may come from:
#
#   GET /candidate_misc/profile/resume/7770003  -> HTTP 200, the full record
#                                                  (title, uploaded_on,
#                                                  is_fresh, conversion_status,
#                                                  and four file URLs)
#   GET /candidate_misc/profile/resume/         -> HTTP 405
#
# The 405 is the detail-only pattern documented above, and it is the reason
# this id is never guessed and never enumerated: the ONLY published source for
# it is the profile payload's own ``resume`` object, which names it twice (as
# ``id`` and inside ``resource_uri``). An account with no resume simply has no
# ``resume`` key -- absent, not null -- so there is no id to look up, and the
# 405 means there is no listing to fall back on either. Both facts point the
# same way: no resume on the profile is an answer, not a lookup to attempt.
EP_RESUME = "/candidate_misc/profile/resume/{resume_id}"

# ``is_fresh`` is Instahyre's own verdict on the uploaded file and it is not
# cosmetic -- the platform surfaces resume staleness to recruiters. The CUTOFF
# that flips it is not published anywhere: no bundle constant, no help page, no
# API field names the number of days. So the tool reports the platform's flag
# beside a derived age and infers nothing from the pair. Do not "fix" this by
# picking a plausible number like 30 or 90; a printed threshold reads as
# measured, and this one would not be.
RESUME_FRESHNESS_CUTOFF_PUBLISHED = False

# Settings GET echoes the account's password fields back in the payload. They
# are stripped before anything is returned, logged or cached. Never widen this.
SETTINGS_NEVER_EMIT = frozenset({"password", "current_password", "confirm_password"})

# Personal contact details. Real, his, and of no use to an agent choosing a
# job -- so they are summarised as present/absent rather than echoed.
CONTACT_FIELDS = frozenset({"phone", "alternate_phone", "email", "name"})

# SUPERSEDED 2026-08-24 by JOB_SEARCH_STATUS, which carries all three codes off
# constant("JOB_SEARCH_STATUS",{ACTIVE:0,PASSIVE:1,NOT_LOOKING:2}) in the shipped
# bundle. This constant is kept, not deleted: it is the honest record of what was
# known when only this account's own value had been seen, and the settings
# endpoint still returns a bare integer that nothing on ITS page decodes. Read
# JOB_SEARCH_STATUS for the profile's `jsp.status`; this one says only what one
# live read confirmed.
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

# How the not-found cross-check walks his conversation list. VERIFIED live on
# 2026-08-22: the message endpoint has NO not-found signal -- conv_id=1 and
# conv_id=999999999 both answered 200 with {"objects": [], "meta": {...}} and
# nothing else -- so an empty thread is resolved against the LIST, the way
# find_opportunity resolves an opportunity id against the queue. The cap is
# there so one read can never become hundreds of requests; hitting it reports
# "do not know", never "not his".
CONV_ID_CHECK_PAGE = 100
CONV_ID_CHECK_MAX_PAGES = 20

# The keys that make up a thread FRAME on a message payload. All three are
# ABSENT from the not-found capture above -- not null, absent. Corroboration
# for the cross-check's verdict; never the verdict itself, because no real
# thread has ever been captured on this account to compare against.
MSG_THREAD_FRAME = ("recipients", "starred", "unsent_messages")

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

#: Substrings that may never appear in any path the READ side requests. Checked
#: as a substring test, not equality, so a query string or a trailing slash
#: cannot smuggle one past.
#:
#: THIS LIST DID NOT SHRINK WHEN REPLY WAS BUILT, and that is deliberate. The
#: read tier still refuses all five, including send_message: a reader that could
#: reach the send path by editing a constant is exactly what this guard exists
#: to stop. Sending goes out through a DIFFERENT door -- see
#: SENDABLE_INBOX_PATHS below -- which is an allowlist of one rather than a hole
#: in this list.
MUTATING_PATH_MARKERS = (
    "mark_all_read",
    "send_message",
    "star_conversation",
    "toggle_message_read",
    "apply_bulk",
)

# --- The one inbox write that IS reachable ---------------------------------
#
# Replying was refused by this server until 2026-08-23 on the honest grounds
# that nobody had measured the request. That is no longer true: the caller was
# read whole out of Instahyre's own inbox controller bundle (see
# CAPTURED_WRITE_CONTRACTS["inbox_reply"]), so the refusal would now be caution
# without a reason.
#
# THE CARVE-OUT IS AN ALLOWLIST, NOT AN EXEMPTION. The write side does not ask
# "is this path forbidden?" -- it asks "is this path ONE OF THE PATHS I am
# allowed to send to?", and the set is short, named and enumerated. Bulk apply
# is not merely still-blocked; it is unreachable from the write channel because
# there is no branch that could reach it.
#
# THE SET WAS ONE ENTRY WIDE UNTIL 2026-08-25. It is now four, and the way it
# grew matters more than the fact that it did: by NAMED ENTRY, one constant at
# a time, never by relaxing the rule into a prefix, a regex or a "starts with
# /resume_modal" test. A rule that admits a family admits members nobody has
# read; a named entry cannot.
#
# The trailing slash here is MEASURED and is not decoration: the factory
# declares `send_message:{method:'POST',url:url+"send_message/"}` WITH it, while
# its siblings star_conversation and toggle_message_read are declared without.
# Django's APPEND_SLASH answers a slashless POST with a 301 that drops the body,
# and this client does not follow redirects -- so the wrong spelling fails
# loudly rather than sending a truncated message.
EP_SEND_MESSAGE = "/resume_modal/emails/message/send_message/"

# --- The three inbox writes admitted on 2026-08-25 -------------------------
#
# THE REFUSAL THESE RETIRE, quoted so the change is legible as a change: "there
# is no branch here that could construct them", and, on the value argument,
# "building them would add two write paths that cannot currently do anything".
# The first half was true and is now deliberately no longer true. The second
# half is STILL TRUE and is not being denied: his inbox holds ZERO
# conversations (measured 2026-08-23, authenticated, 200), so none of the three
# has ever been exercised against live data and none can be until a recruiter
# opens a thread. What changed is the ruling, not the inbox -- whatever is
# technically possible gets built, and the contract for all three ships in
# Instahyre's own JavaScript, so all three are possible.
#
# THE GATES DID NOT RELAX. Each one is confirm=False by default and sends
# nothing without confirm=True; each is reached through the allowlist below
# rather than through a hole in a blocklist; and the READ tier is byte-for-byte
# unchanged -- MUTATING_PATH_MARKERS still holds all five markers and
# guard_read_only still refuses every one of these paths, so a reader cannot
# arrive at a write by editing a constant. FORBIDDEN_ENDPOINTS is untouched:
# both apply_bulk spellings stay permanently banned at any evidence level.
#
# NO TRAILING SLASH ON THE TWO MESSAGE ACTIONS. The factory declares them
# without one -- `star_conversation:{method:'POST',url:url+"star_conversation"}`
# and `toggle_msg_read:{method:'POST',url:url+"toggle_message_read"}` -- next to
# a send_message that IS declared with one. The three spellings are copied, not
# regularised.
EP_STAR_CONVERSATION = "/resume_modal/emails/message/star_conversation"
EP_TOGGLE_MESSAGE_READ = "/resume_modal/emails/message/toggle_message_read"

#: The GET THAT MUTATES, and the single most dangerous shape in this package.
#: `mark_all_read:{method:"GET",url:url+"mark_all_read",ignoreLoadingBar:true}`
#: on the candidateConversationService factory -- the same resource prefix the
#: conversation LIST is served from. Every "just GET it and see" instinct is
#: wrong here, which is why both guards in this package key on the PATH and
#: never on the verb, and why this one is gated exactly as hard as a POST.
EP_MARK_ALL_READ = "/inbox_page/candidate_conversation/mark_all_read"

#: The complete set of paths this package may send a MUTATING request to on the
#: inbox resource. FOUR NAMED ENTRIES, each admitted on its own captured
#: contract; a test pins the size and the literal spellings, so a fifth is a
#: visible edit rather than a quiet capability.
SENDABLE_INBOX_PATHS = frozenset(
    {
        EP_SEND_MESSAGE,
        EP_STAR_CONVERSATION,
        EP_TOGGLE_MESSAGE_READ,
        EP_MARK_ALL_READ,
    }
)

#: The star body, exactly, on the CANDIDATE branch -- which is the branch this
#: account is on.
#:
#: BOTH SHIPPED CALLERS AGREE ON IT, and reading only one of them is how the
#: shape gets got wrong. `inboxService.markUnstarred` builds
#: `{star_conv:false, job_id:conv.job_id}` and adds `can_user` ONLY under
#: `if(profileType!=="candidate")`. `inboxService.toggleStarConversation`
#: branches the same way: `if(profileType=='candidate')` it builds
#: `{star_conv:!Boolean(selectedConv.starred), job_id:selectedConv.job_id}`,
#: else it adds `can_user`. So on a candidate session the two functions emit
#: the IDENTICAL two-key shape and differ only in which boolean they compute.
#:
#: THE KEY IS `star_conv`, NOT `starred`. `starred` appears in that function
#: only as `response.starred` -- the field read back OFF the response -- and
#: mistaking a response field for a request field is the exact error this
#: comment exists to stop. There is no shipped caller anywhere that sends a
#: `starred` key.
#:
#: `can_user` is a limited_candidate resource URI naming the OTHER party, and
#: it is recruiter-side only. Sending it from a candidate session would be
#: sending a field his own browser never sends.
STAR_CONVERSATION_BODY_KEYS = ("job_id", "star_conv")
STAR_CONVERSATION_CANDIDATE_OMITS_CAN_USER = True
#: The response carries the new state: `inboxService.scope.selectedConv.starred
#: = response.starred`. So this one write verifies itself out of its own reply,
#: which none of the other inbox writes can do.
STAR_CONVERSATION_RESPONSE_FIELD = "starred"

#: The toggle-read body, exactly. `inboxService.markUnread(conv)` calls
#: `messageService.toggle_msg_read({conversation:conv.resource_uri,
#: mark_unread:true})`.
#:
#: `conversation` IS A RESOURCE URI, NOT AN ID. It is `conv.resource_uri`,
#: a server-supplied string off the conversation record -- the same tastypie
#: convention the support ticket names its candidate by. A bare integer is a
#: different request, and this server never assembles the URI itself: it reads
#: the record and copies the value the server returned.
TOGGLE_MESSAGE_READ_BODY_KEYS = ("conversation", "mark_unread")
#: ONLY `true` HAS A SHIPPED CALLER. Across every bundle the one caller of this
#: action is markUnread, and it sends the literal true. The site has no
#: "mark read" button at all -- it marks read implicitly, by fetching the
#: thread -- so `mark_unread:false` is a value nobody has been observed
#: sending. It is not forbidden here, but every preview says so out loud
#: rather than letting the two values borrow each other's evidence.
TOGGLE_MESSAGE_READ_TRUE_IS_THE_ONLY_OBSERVED_VALUE = True

#: mark_all_read is the one entry in the captured register whose METHOD is GET.
MARK_ALL_READ_METHOD = "GET"
#: Its args ride the QUERY STRING: `data=buildFilters(); data.page_loaded_at=
#: $scope.pageLoadTimestamp;` and the $resource action is a GET, so the dict
#: becomes query parameters and there is no body at all.
#:
#: buildFilters() IS MEASURED, not assumed -- read whole out of the inbox
#: controller bundle:
#:
#:     var buildFilters=function(){var filterDict={};
#:       if($scope.isOpenedInMobile){...}
#:       else{if($scope.filters.selectedStatus){filterDict.status=...;}
#:         if($scope.getConvType($scope.convTypes.UNREAD)){filterDict.unread=true;}
#:         else if($scope.getConvType($scope.convTypes.STARRED)){filterDict.starred=true;}}
#:       if($scope.filters.query){filterDict.query=$scope.filters.query;}
#:       return filterDict;}
#:
#: On the default desktop view -- "All conversations", no search box -- every
#: branch is falsy and it returns an EMPTY dict. That is why the tool built on
#: this takes no filter arguments: the widest call is the one the name
#: promises, and it is also the only one whose filter dict needs no choosing.
MARK_ALL_READ_QUERY_KEYS = ("page_loaded_at",)
#: `$scope.pageLoadTimestamp=new Date().toISOString()`, assigned when the
#: conversation LIST is fetched (`if(!triggeredOnScroll||!$scope
#: .pageLoadTimestamp)`), not at page load despite the name. So the value is
#: JavaScript's ISO-8601-with-milliseconds spelling, and its MEANING is "the
#: moment the list I am looking at was read" -- a race guard, so a message that
#: arrived after that read is not swept up. This server reproduces both halves:
#: it reads the list, stamps that read, and sends that stamp.
MARK_ALL_READ_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
#: `$scope.markAllRead` opens with `if(inboxService.getMarkAllAsReadCount())`
#: and does NOTHING when it is zero. That count is
#: `conv_count.unread || 0` (or `conv_count.starred_unread` when the starred
#: filter is on). A platform-side gate, reproduced rather than invented.
MARK_ALL_READ_GATE_COUNT_FIELD = "unread"
#: `markAllReadCallback(response)` reads `response.conv_count.unread`. So the
#: response itself reports the new unread total, which is what a re-read has to
#: agree with.
MARK_ALL_READ_RESPONSE_COUNT_KEY = "conv_count"

#: The body, exactly. `content` is the Quill editor's HTML; `attachments` is
#: initialised to [] by the page and populated by an uploader that ships in no
#: bundle, so its ELEMENT SHAPE IS UNMEASURED and this server always sends the
#: empty list rather than inventing one.
SEND_MESSAGE_BODY_KEYS = ("attachments", "content", "conv_id")

#: What the page validates before sending: nothing. The only guard on
#: `addMessage` is a double-click latch (`buttonParams.disabled`), there is no
#: empty-content check, no length check and no closed-thread rule -- an empty
#: editor POSTs `content: ""`. Every rail on the reply tool is therefore this
#: server's own, and is recorded as such rather than dressed up as the
#: platform's.
SEND_MESSAGE_CLIENT_VALIDATES_NOTHING = True

#: The candidate compose form has NO subject field: the literal `subject` has
#: zero hits in the inbox controller bundle. EMAIL_SUBJECT_MAX_LENGTH is
#: employer-side. A reply that offered a subject would be offering a field the
#: endpoint has never been sent.
SEND_MESSAGE_HAS_NO_SUBJECT = True

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

# --- Writes that CANNOT be built on the evidence this tree holds -------------
#
# Six write surfaces were commissioned on 2026-08-23 and none was built. This
# is the measurement that stopped them, recorded here rather than in a report,
# so the next session finds the finding instead of re-deriving it -- or worse,
# guessing a body and shipping it.
#
# WHAT THE EVIDENCE ACTUALLY IS. Five of the six trace to ONE table in
# `_audit/2026-08-20-instahyre-exploration.md`, whose own heading reads
# "auth INFERRED from shipped code (NOT probed, per the read-only constraint)".
# It was built by resolving an Angular API_PATHS map against $resource action
# maps. That technique yields a path string and a set of action NAMES. It
# cannot yield a request body, and it never did.
#
#   A JS FUNCTION NAME IS NOT A CONTRACT. `send_invites` is an entry in an
#   action map. It says a route probably exists. It does not say what method
#   reaches it, what fields it takes, what it returns, or what it does on
#   partial input -- and those are the whole content of a write.
#
# NO POST HAS EVER BEEN SENT TO INSTAHYRE FROM THIS CODEBASE, so there is no
# measured status code for any of the six either. Every live call in the
# exploration, the parity pass and the auth tier was a GET.
#
# THE RULE THIS ENCODES: a write with a guessed body shape is worse than no
# tool. It fails in the direction that cannot be undone -- a guessed body that
# is WRONG usually 400s harmlessly, and a guessed body that is half-right
# succeeds and does something nobody chose. On this platform the second case is
# permanent.
#
# Deliberately NOT merged into FORBIDDEN_ENDPOINTS, which means something
# stronger: those two bulk paths must never be built, at any evidence level.
# These six are UNBUILT PENDING MEASUREMENT. Capture the real request in a
# signed-in browser, record the body here, and they become ordinary work.
UNVERIFIED_WRITE_SURFACES = {
    "screening_questionnaires": (
        "POST /questionnaires/answer is a name with an explicitly UNVERIFIED body, "
        "and there is no READ route for the questions at all -- so there is no way "
        "to know what is being answered. It rides an application that cannot be "
        "withdrawn. The 2026-08-23 capture pass did NOT advance it and could not: "
        "across all ten shipped bundles there is no $resource factory and no "
        "caller, only an HTTP error interceptor that matches the URL as a "
        "substring to raise its upload size limit from 8MB to 100MB. That "
        "establishes the route exists and takes files. It yields no method and no "
        "field. Reaching the questionnaire in a browser means pressing Apply on a "
        "real opportunity, and that is the one action this server must never take, "
        "so the capture technique that unblocked the others cannot be pointed at "
        "this one."
    ),
    "workex_put": (
        "THE 2026-08-23 ENTRY WAS WRONG ON ITS CENTRAL CLAIM and is corrected here "
        "rather than quietly replaced: it said NO CALLER exists in any of the ten "
        "shipped bundles. One does. candidateService declares "
        "save_onboarding_workex:{url:...onboarding_workex/:id,method:'PUT'} and "
        "$scope.onBoardingProfileSave calls it -- "
        "candidateService.save_onboarding_workex({id:$scope.candidate.id},"
        "$scope.candidate). The earlier pass searched the CANDIDATE PROFILE page and "
        "found nothing because the control is not there; it is on the ONBOARDING "
        "page, which is a different controller.\n\n"
        "Reading the caller settles the surface and closes it, in the opposite "
        "direction from the one the register expected. The body is not a "
        "work-experience record. It is $scope.candidate -- THE ENTIRE CANDIDATE "
        "OBJECT, jsp and education and skills included -- and the response is read "
        "back for current_company, current_company_nopunc and companies_to_block. So "
        "the route named 'workex' writes the CANDIDATE, and 'edit work experience' is "
        "not a capability this platform has: there is no work-experience record to "
        "edit, which is why the profile page renders no control for one and why the "
        "42-key profile payload contains no such block. That was the true finding "
        "behind the earlier entry's wrong reason.\n\n"
        "It stays unbuilt, and now on the merits rather than for want of evidence. "
        "The fields this route reaches -- current_company, current_designation, "
        "total_experience -- are ALREADY writable through EP_PROFILE_PATCH, which is "
        "a sparse PATCH: it touches the keys it names and nothing else. Building this "
        "PUT would replace a whole-object write for a same-effect sparse one, which "
        "is strictly more blast radius for zero new capability. What remains "
        "genuinely unmeasured is the exact body shape -- $scope.candidate as the "
        "onboarding controller holds it, which is not byte-identical to the profile "
        "GET -- and that is the measurement anyone reversing this decision must take "
        "first."
    ),
}

# --- Writes whose contract WAS captured, 2026-08-23 -------------------------
#
# Four of the six above came off this list on 2026-08-23, by the route the
# register itself named: "capture the real request in a signed-in browser,
# record the body here, and they become ordinary work."
#
# TWO EVIDENCE CLASSES, and they are not the same thing. Every entry says which
# it is, because the difference decides how much a caller may lean on it.
#
#   WIRE       -- the serialized request the page itself built, recorded by
#                 scripts/capture_write_contracts.py and ABORTED at the router
#                 before it left the machine. Nothing was sent. This is the
#                 strongest evidence short of a response.
#   SHIPPED    -- read out of Instahyre's own JavaScript: the whole $resource
#                 factory plus the whole calling function, quoted verbatim. It
#                 is what the browser WOULD build. It is the same class of
#                 evidence that corrected the apply contract twice, and it is
#                 strictly weaker than WIRE because it has never been
#                 serialized.
#
# ONE MEASUREMENT THAT APPLIES TO ALL OF THEM. AngularJS 1.x $resource defaults
# to stripTrailingSlashes:true, and this was MEASURED here rather than
# remembered: the candidate_query factory declares the URL with a trailing
# slash, and the wire capture shows the request going to
# .../candidate_query with NO trailing slash. So every URL below is the
# stripped spelling. This client does not follow redirects, so guessing wrong
# in this direction surfaces as a 301, not as a silent mutation -- the safe
# failure.
CONTRACT_WIRE = "WIRE"
CONTRACT_SHIPPED = "SHIPPED"

#: Support tickets. WIRE-captured whole.
EP_SUPPORT_QUERY = "/candidate_misc/support/candidate_query"
SUPPORT_QUERY_CONTENT_TYPE = "application/json;"
#: The candidate is named by RESOURCE URI, not by integer id. Tastypie style,
#: and getting this wrong is a 400 rather than a wrong write.
EP_LIMITED_CANDIDATE = "/candidate_misc/profile/limited_candidate/"
SUPPORT_QUERY_BODY_KEYS = ("attachments", "candidate", "message")
#: Field errors come back nested under this key, e.g.
#: {"candidate_query": {"attachments": ["..."]}}
SUPPORT_QUERY_ERROR_ENVELOPE = "candidate_query"

#: Saved-search job alerts. SHIPPED, whole functions verbatim.
EP_SAVED_SEARCH_DETAIL = "/candidate_opportunities/saved_job_searches/"
SAVED_SEARCH_TOGGLE_METHOD = "PATCH"
#: toggleAlerts sends the flag ALONGSIDE search_string -- it is not a flag-only
#: PATCH, and a PATCH that omitted search_string is a request the site never
#: makes.
SAVED_SEARCH_TOGGLE_BODY_KEYS = ("id", "job_alert_enabled_at", "search_string")
#: Named like a timestamp column; the client treats it as a strict boolean
#: (it sends `!current`, and the gate assigns the literal false). Which of the
#: two the SERVER stores is not determined by this evidence.
SAVED_SEARCH_ALERT_IS_BOOLEAN_CLIENT_SIDE = True
SAVED_SEARCH_ERROR_ENVELOPE = "saved_job_searches"
#: canEnableJobAlerts, measured: at least three non-empty sidebar filters, with
#: job_categories always excluded from the count and job_type excluded unless
#: the candidate is a fresher. Failing the gate forces the flag false.
SAVED_SEARCH_ALERT_GATE_EXCLUDES = ("job_categories", "job_type")

#: Referrals. SHIPPED, both callers quoted whole.
EP_REFERRAL = "/candidate_misc/refer/referral"
EP_REFERRAL_INVITES = "/candidate_misc/refer/referral/send_invites"
EP_REFERRAL_CONTACTS = "/candidate_misc/refer/referral/import_gmail_contacts"
#: get_link posts the referral object itself and the response returns it
#: populated; referral_url round-trips, empty on the way out.
REFERRAL_LINK_BODY_KEYS = ("email", "name", "referral_url")
#: send_invites carries BOTH the structured list and the raw string. `name` and
#: `email` at the top level are the REFERRER, not an invitee -- reading them as
#: the recipient is the misreading this comment exists to prevent.
REFERRAL_INVITE_BODY_KEYS = ("email", "email_list", "friends", "name")
#: constructInvitationsDict strips EVERY space from an address, not just the
#: ends: `item.replace(/ /g,'')`.
REFERRAL_STRIPS_ALL_SPACES = True
#: import_gmail_contacts is a GET. That matters: the "who would be contacted"
#: list can be read without sending anything, so a confirm gate for invites can
#: name real people from a real source instead of a fabricated one.
REFERRAL_CONTACTS_IS_GET = True

#: Profile image. WIRE-captured on the REPLACE branch only.
EP_PROFILE_IMAGE = "/profiles/profile_image"
#: The service picks its URL by method: POST goes to the collection, PUT goes
#: to `data.resource_uri` -- the existing image's own detail URL. This account
#: already has an image, so the wire capture is the PUT.
PROFILE_IMAGE_PUT_BODY_KEYS = ("file_b64", "profile", "resource_uri", "title")
#: file_b64 is a DATA URL, not bare base64: canvas.toDataURL("image/webp",0.7)
#: after downscaling to width <= 800 (aspect preserved, smaller images left
#: alone). MAX_WIDTH=800 and QUALITY_PARAMETER=0.7 are shipped constants.
PROFILE_IMAGE_ENCODING = "data:image/webp;base64, width<=800, quality 0.7"
PROFILE_IMAGE_MAX_WIDTH = 800
PROFILE_IMAGE_QUALITY = 0.7

# --- The job-search profile (jsp): the row employers filter on --------------
#
# THIS SECTION RETIRES A REFUSAL, so it starts with what the refusal said:
# "those need the whole object PUT back, a contract this server has not
# verified, so it refuses them by name rather than guessing."
#
# The contract is now read, and the refusal's own premise is what made it
# retirable -- it named an UNVERIFIED contract, not an unknowable one.
#
# WHERE THE JSP LIVES ON THE READ SIDE. It is not a second request. The profile
# GET already returns it whole, under the key `jsp`, with 26 keys and every
# related object EXPANDED (job_function is a full object; industry_types and
# languages are lists of full objects). That matters more than it looks: the
# write below is a read-modify-write, and a read-modify-write is only as safe
# as its read is complete. This one is complete by construction -- it is the
# same payload the site's own controller binds its form to.
#
# WHERE THE WRITE GOES, and the trap in the name. The object's own
# `resource_uri` says `candidate_jsp`. The site does not write there. It writes
# through a $resource factory, quoted whole, whose URL is `candidate_skills/:id`
# -- so there are TWO routes to one object and the site uses the one that sounds
# like it carries skills. It does not; skills ride `candidate_skill_model`.
# Guessing from the resource_uri would have picked the wrong door, which is
# precisely why this stayed refused until it was READ rather than inferred.
#
#     candidateServicesModule.factory("candidateSkillsService",
#       function($resource,API_PATHS){
#         var url=`${API_PATHS.CANDIDATE_MISC_PROFILE}/candidate_skills/:id`,
#             candidate_skills=$resource(url,{id:"@id"},{update:{method:"PUT"}});
#         return candidate_skills;})
#
# `:id` binds to `@id` -- the JSP's OWN id, not the candidate's. The two are
# different numbers on this account and swapping them addresses another row.
#
# WHAT THE BODY IS. The whole jsp, passed by reference:
#
#     var _getSkillUpdatePromise=function(jspData,scope){
#       saveCareerBreakFields(jspData,scope);
#       return candidateSkillModelService.multi_save(...)
#         .finally(function(){return candidateSkillsService.update(jspData).$promise;})...}
#
#     $scope.profileSkillSave=function(editor){
#       ...remapLanguages();
#       _getSkillUpdatePromise($scope.candidate.jsp,editorScope.$$childTail)...}
#
# So `jspData` IS `$scope.candidate.jsp`. There is no projection, no field list
# and no allowlist between the form and the wire: whatever the object holds is
# what goes. That is the whole reason a partial body is dangerous here, and the
# whole reason echoing the read back verbatim is sufficient.
#
# THE OTHER SAVE PATH, and why it is not used. The preference editor reaches
# the same object through `saveChanges`, which reads its method and URL off DOM
# attributes (`cscope.editors[attrs.editorModel]=attrs`). Those attributes ship
# in server-rendered HTML, in no bundle, so that path cannot be read from
# source at all -- only from a live browser. The $resource path above needs no
# browser and lands on the same resource, which is why it is the one recorded.
EP_JSP = "/candidate_misc/profile/candidate_skills/{jsp_id}"

#: The route the object NAMES ITSELF, which is NOT the route the site writes
#: to. Kept as a constant so the difference stays visible and cannot be quietly
#: "corrected" into the write path by someone reading a resource_uri.
JSP_SELF_URI_PREFIX = "/api/v1/candidate_misc/profile/candidate_jsp/"

JSP_PUT_METHOD = "PUT"

#: A PUT is a full replacement and an omitted key is a silent deletion. This
#: server never tests that by omitting one. It removes the question instead:
#: every write echoes back every key the read returned, so the payload's
#: implicit claim -- "this is the whole object" -- is true. Same discipline as
#: the skills replacement set, arrived at by a different route.
JSP_PUT_IS_FULL_REPLACEMENT = True

#: Read from the shipped bundle: constant("NOTICE_PERIOD_RANGES",{...}).
#: THE VALUE IS AN INDEX, NOT A NUMBER OF DAYS, and the two readings coincide
#: only at 0. A profile reading `notice_period: 3` means "2 months or less",
#: not three days. An earlier build surfaced this field as `notice_period_days`;
#: that label was wrong, and wrong invisibly, because this account sits at 0
#: where both readings print the same thing.
NOTICE_PERIOD_RANGES = {
    0: "Immediately",
    1: "15 days or less",
    2: "1 month or less",
    3: "2 months or less",
    4: "3 months or less",
}
#: constant("MAX_NOTICE_PERIOD_INDEX",4) -- shipped, not derived from the dict.
MAX_NOTICE_PERIOD_INDEX = 4

#: constant("JOB_SEARCH_STATUS",{ACTIVE:0,PASSIVE:1,NOT_LOOKING:2}). This
#: SUPERSEDES JOB_SEARCH_STATUS_KNOWN, which held only the value this account
#: happens to sit at and said the rest were unmeasured. They ship.
JOB_SEARCH_STATUS = {0: "actively looking", 1: "passively looking", 2: "not looking"}

#: constant("CAREER_STAGE",{FRESHER:0,EXPERIENCED:1,CAREER_BREAK:2}).
CAREER_STAGE = {0: "fresher", 1: "experienced", 2: "career break"}

#: constant("MIN_JOB_SALARY_LIMIT",0) / constant("MAX_JOB_SALARY_LIMIT",250).
#: Units are LAKHS per annum -- the page renders the figure as salary*100000.
MIN_SALARY_LAKHS = 0
MAX_SALARY_LAKHS = 250

#: The jsp fields this server will change. Short on purpose, and every name
#: ABSENT from it is absent for a stated reason rather than for lack of nerve:
#:
#:   is_immediate_joinee -- ZERO write sites across all ten bundles. The page
#:     never assigns it, so it is server-derived (almost certainly from
#:     notice_period). Writing a field the site itself only reads is the
#:     guessed-contract failure this register exists to refuse. It is echoed
#:     back untouched, and if the server recomputes it after a notice_period
#:     change the verify step reports the move instead of hiding it.
#:   is_salary_hidden -- the control exists ($scope.toggleHideSalary) but is
#:     gated on HIDE_SALARY_LIMITS {'salary':50,'experience':8}. This account
#:     reads 0 and 5, so the site does not offer him the toggle at all.
#:     Sending it would be sending a value his own session cannot produce.
#:   career_stage / career_break_* -- changing career stage CASCADES in the
#:     page: it NULLs notice_period, zeroes current_salary, and blanks
#:     current_company and current_designation. One tool call should not move
#:     four fields it did not name.
#:   job_function / industry_types / languages -- related objects, sent
#:     EXPANDED. Setting one means selecting a whole option object out of a
#:     taxonomy the page had already loaded, not writing an id. Buildable, not
#:     built: it is a wider contract than the four scalars and one list that
#:     were asked for, and it has its own read side to get right first.
JSP_WRITABLE_FIELDS = (
    "notice_period",
    "current_salary",
    "location_preferences",
    "status",
    "job_type",
)

#: Keys the SERVER owns. They ride every write, because the browser sends the
#: object whole and so does this server -- but a caller may never SET one.
#: Several are derived from the others, and a supplied value would be either
#: ignored or believed, with no way to tell which from the outside.
JSP_SERVER_OWNED_KEYS = frozenset(
    {
        "resource_uri",
        "candidate",
        "id",
        "status_string",
        "career_stage_value",
        "status_last_modified_at",
        "suggested_industry_types",
        "is_immediate_joinee",
    }
)

#: The site converts these two to floats on load
#: (`jsp.current_salary=parseFloat(jsp.current_salary)`) while the server
#: returns them as STRINGS ("0.0"). So the browser's PUT carries numbers where
#: its own GET carried strings. This server does NOT reproduce that: an
#: untouched field goes back EXACTLY as the server returned it. Deviating from
#: the browser in the direction of "change nothing that was not asked for" is
#: the same call this package already made when it skipped the site's second
#: request during a skills write.
JSP_STRING_TYPED_DECIMALS = ("current_salary", "fresher_salary")

CAPTURED_WRITE_CONTRACTS = {
    "job_search_profile": {
        "evidence": CONTRACT_SHIPPED,
        "method": "PUT",
        "path": EP_JSP,
        "body_keys": ("<the whole jsp object -- every key the read returned>",),
        "note": (
            "The $resource factory and both calling functions are quoted verbatim in "
            "the jsp section further down this file. This entry is SHIPPED rather than "
            "WIRE, and unusually that is the stronger position rather than the weaker "
            "one. The other SHIPPED entries describe a body ASSEMBLED in the page, "
            "which is why each carries the caveat that it has never been serialized. "
            "This body is not assembled at all: it is the object the profile GET "
            "returns, handed to the resource by reference. So the payload can be READ "
            "LIVE instead of reconstructed, and the usual gap between shipped source "
            "and the wire closes on the read side. What SHIPPED does leave open is "
            "narrow and named -- whether the server treats an omitted key as a "
            "deletion. This write never omits one, so the question does not arise, and "
            "_guard_no_key_dropped refuses the request if it ever would. A second "
            "route to the same object exists and is deliberately NOT used: the "
            "object's own resource_uri points at candidate_jsp, while the site writes "
            "to candidate_skills."
        ),
    },
    "inbox_reply": {
        "evidence": CONTRACT_SHIPPED,
        "method": "POST",
        "path": EP_SEND_MESSAGE,
        "body_keys": SEND_MESSAGE_BODY_KEYS,
        "note": (
            "The caller is $scope.addMessage in the inbox page controller, which ships "
            "in output.c956ddddd95a.js -- a TENTH bundle that no earlier pass had, "
            "because no earlier pass reconned /candidate/inbox/. Against the nine "
            "bundles previously in hand, send_message had a factory action and NO "
            "caller, which is the same dead end workex_put is still sitting in; the "
            "bundle was found by censusing the inbox page's own script tags. The body "
            "is $scope.newMessage passed whole: attachments initialised to [] at "
            "controller start, conv_id assigned from $scope.selectedConv.id one "
            "statement before the POST, content bound from the compose form. "
            "NOT wire-confirmed, and it CANNOT be on this account: his inbox holds "
            "zero conversations (measured 2026-08-23, authenticated, 200), the compose "
            "form only renders inside a selected thread, and addMessage dereferences "
            "$scope.selectedConv.id -- so with nothing selected it throws before any "
            "request is built. There is no control to intercept until a recruiter "
            "opens a thread."
        ),
    },
    "inbox_star": {
        "evidence": CONTRACT_SHIPPED,
        "method": "POST",
        "path": EP_STAR_CONVERSATION,
        "body_keys": STAR_CONVERSATION_BODY_KEYS,
        "note": (
            "TWO callers, both quoted whole: inboxService.markUnstarred and "
            "inboxService.toggleStarConversation. Reading only one of them is how this "
            "shape gets got wrong, because each branches on profileType and the "
            "recruiter branch carries a third key. On the CANDIDATE branch -- this "
            "account's branch -- the two emit the identical body {star_conv, job_id} "
            "and differ only in which boolean they compute. can_user is added ONLY "
            "under if(profileType!=='candidate') and names the other party by "
            "limited_candidate resource URI, so sending it here would be sending a "
            "field his own session never sends. The request key is star_conv; the "
            "word 'starred' occurs in that function only as response.starred, the "
            "field read BACK off the reply, and no shipped caller sends a starred "
            "key. NOT wire-confirmed and it cannot be on this account: the inbox "
            "holds zero conversations, both callers dereference a conversation, and "
            "the star control only renders on a thread that exists."
        ),
    },
    "inbox_mark_read": {
        "evidence": CONTRACT_SHIPPED,
        "method": "POST",
        "path": EP_TOGGLE_MESSAGE_READ,
        "body_keys": TOGGLE_MESSAGE_READ_BODY_KEYS,
        "note": (
            "One caller, quoted whole: inboxService.markUnread(conv) sends "
            "{conversation: conv.resource_uri, mark_unread: true}. The first field "
            "name is the trap -- 'conversation' takes a tastypie RESOURCE URI, not "
            "the bare integer id every other inbox call uses, so a guess from the id "
            "would be a different request. This server never assembles that URI; it "
            "reads the conversation record and copies the string the server returned. "
            "The second half is an evidence gap that is stated rather than papered "
            "over: only mark_unread TRUE has a caller anywhere. The site has no mark-"
            "read button -- it marks read implicitly by fetching a thread -- so false "
            "is a value nobody has been observed sending, and every preview says so. "
            "NOT wire-confirmed and it cannot be on this account: zero conversations, "
            "so there is no record to take a resource_uri off and no control to "
            "intercept."
        ),
    },
    "inbox_mark_all_read": {
        "evidence": CONTRACT_SHIPPED,
        "method": MARK_ALL_READ_METHOD,
        "path": EP_MARK_ALL_READ,
        "body_keys": (
            "<none -- this is a GET; its arguments ride the query string>",
        ),
        "query_keys": MARK_ALL_READ_QUERY_KEYS,
        "note": (
            "THE ONLY ENTRY IN THIS REGISTER WHOSE METHOD IS GET, and it bulk-mutates: "
            "one call clears the unread flag across the whole inbox. The factory says "
            "so in as many words -- mark_all_read:{method:'GET',url:url+'mark_all_read'} "
            "-- and it sits on the same resource prefix as the conversation list, so an "
            "ordinary-looking walk of that resource would wipe his unread state with no "
            "body and no warning. That is why both guards in this package key on the "
            "PATH and never on the verb, and why this contract is gated exactly as hard "
            "as a POST. The caller is $scope.markAllRead: it refuses outright when "
            "getMarkAllAsReadCount() is zero, then sends buildFilters() plus "
            "page_loaded_at as query parameters. buildFilters() is measured, not "
            "assumed, and returns an EMPTY dict on the default 'All conversations' view "
            "with no search text; page_loaded_at is new Date().toISOString(), stamped "
            "when the LIST was fetched, and its job is to leave anything newer than "
            "that read alone. NOT wire-confirmed and it cannot be on this account: zero "
            "conversations means zero unread, so the site's own gate would refuse to "
            "issue the request at all."
        ),
    },
    "support_tickets": {
        "evidence": CONTRACT_WIRE,
        "method": "POST",
        "path": EP_SUPPORT_QUERY,
        "body_keys": SUPPORT_QUERY_BODY_KEYS,
        "note": (
            "Recorded from the real signed-in browser on 2026-08-23 and aborted at "
            "the router. Content-Type is 'application/json;' with Angular's literal "
            "trailing semicolon, and the request carries X-CSRFToken. The body is "
            "{candidate: <limited_candidate resource uri>, message, attachments: []}. "
            "The wire URL has NO trailing slash even though the factory declares "
            "one."
        ),
    },
    "saved_search_alert_toggle": {
        "evidence": CONTRACT_SHIPPED,
        "method": SAVED_SEARCH_TOGGLE_METHOD,
        "path": EP_SAVED_SEARCH_DETAIL + ":id",
        "body_keys": SAVED_SEARCH_TOGGLE_BODY_KEYS,
        "note": (
            "The callee that the 2026-08-22 parity pass could not find ships in the "
            "AUTHENTICATED-tier bundle, which no earlier pass had downloaded: "
            "toggleSavedJobSearchAlerts validates and opens a modal, and toggleAlerts "
            "performs the PATCH. NOT wire-confirmed, and it cannot be on this "
            "account: it holds ZERO saved searches, and the save-search control "
            "renders hidden on /candidate/opportunities/ and is absent from "
            "/search-jobs, so there is no row to toggle and no control to intercept."
        ),
    },
    "referrals": {
        "evidence": CONTRACT_SHIPPED,
        "method": "POST",
        "path": EP_REFERRAL_INVITES,
        "body_keys": REFERRAL_INVITE_BODY_KEYS,
        "note": (
            "Per-action method and URL from the referralService factory; bodies from "
            "the two callers, both quoted whole. send_invites still mails REAL THIRD "
            "PARTIES and still has no unsend -- capturing the contract changed what "
            "we know, not what it does. What DID change is that the confirm gate can "
            "now be built honestly: import_gmail_contacts is a GET, so the list of "
            "who would be contacted is readable without sending anything, and the "
            "typed path's list is simply what the caller supplied."
        ),
    },
    "profile_image": {
        "evidence": CONTRACT_WIRE,
        "method": "PUT",
        "path": EP_PROFILE_IMAGE + "/:id",
        "body_keys": PROFILE_IMAGE_PUT_BODY_KEYS,
        "note": (
            "Wire-captured 2026-08-23 by handing the page's own #image-input a 1x1 "
            "fake PNG and letting it build the request; the router threw it away. "
            "This settles the question the register raised -- it is JSON, not "
            "multipart, and the otherData keys are {profile, resource_uri}. The "
            "CREATE branch (POST to the collection, no resource_uri) is NOT "
            "measured: this account already has an image, so the page took the PUT "
            "branch. NO TOOL IS BUILT ON THIS. Reproducing the browser's body needs "
            "a WebP encoder at width<=800, which this package has no dependency "
            "for, and sending a differently-encoded payload would be exactly the "
            "guessed variant this whole register exists to refuse."
        ),
    },
}

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
