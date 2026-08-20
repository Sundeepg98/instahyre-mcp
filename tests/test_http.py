"""http.py -- error mapping, retries, and parameter flattening.

This is the file that decides whether a failure arrives as a type or as an
innocent-looking empty dict, so every branch of ``_interpret`` gets its own
test asserting the EXACT exception class.
"""

from __future__ import annotations

import httpx
import pytest

from conftest import (
    HTML_404_BODY,
    HTML_CHALLENGE_BODY,
    fixture_json,
    html_response,
    json_response,
    make_http,
)
from instahyre_server import constants as C
from instahyre_server.errors import (
    ApiError,
    AuthRequired,
    ChallengeDetected,
    InstahyreError,
    InvalidFilter,
    NotFound,
    RateLimited,
    TransportError,
)
from instahyre_server.http import _flatten_params

JOB_DETAIL_PATH = C.EP_JOB_DETAIL.format(job_id=999999)
SEARCH = C.EP_JOB_SEARCH


# ---------------------------------------------------------------------------
# 404 -- the headline case
# ---------------------------------------------------------------------------


def test_404_with_48kb_html_body_raises_not_found():
    """Instahyre serves a full HTML page for a missing job id.

    The status alone must be the signal: no JSON parse is attempted, and the
    caller gets NotFound rather than an empty dict.
    """
    assert len(HTML_404_BODY) > 40_000, "the fixture body must be realistically large"
    http = make_http({JOB_DETAIL_PATH: html_response(HTML_404_BODY, status=404)})

    with pytest.raises(NotFound) as excinfo:
        http.get(JOB_DETAIL_PATH)

    error = excinfo.value
    assert error.kind == "not_found"
    assert error.context["status"] == 404
    assert error.context["path"] == JOB_DETAIL_PATH
    # The body is never echoed -- 48 KB of markup has no business in an error.
    assert "<html" not in error.message
    assert http.routes.count() == 1, "a 404 is a verdict, never retried"


def test_404_does_not_return_an_empty_dict():
    """The regression guard: NotFound must not degrade into a falsy result."""
    http = make_http({JOB_DETAIL_PATH: html_response(HTML_404_BODY, status=404)})
    result = None
    try:
        result = http.get(JOB_DETAIL_PATH)
    except NotFound:
        pass
    assert result is None, "a 404 returned a value instead of raising"


# ---------------------------------------------------------------------------
# 401 / logged_out
# ---------------------------------------------------------------------------


def test_401_logged_out_body_raises_auth_required():
    payload = fixture_json("error_401.json")
    assert payload == {"logged_out": True}
    http = make_http({C.EP_JOB_CATEGORY: json_response(payload, status=401)})

    with pytest.raises(AuthRequired) as excinfo:
        http.get(C.EP_JOB_CATEGORY)

    assert excinfo.value.kind == "auth_required"
    assert excinfo.value.context["status"] == 401
    assert "instahyre_login" in excinfo.value.message


def test_logged_out_body_on_a_200_also_raises_auth_required():
    """The body is a second, independent signal -- a 200 must not hide it."""
    http = make_http({C.EP_JOB_CATEGORY: json_response(fixture_json("error_401.json"), status=200)})
    with pytest.raises(AuthRequired):
        http.get(C.EP_JOB_CATEGORY)


def test_401_is_not_retried():
    http = make_http({C.EP_JOB_CATEGORY: json_response({"logged_out": True}, status=401)})
    with pytest.raises(AuthRequired):
        http.get(C.EP_JOB_CATEGORY)
    assert http.routes.count() == 1


# ---------------------------------------------------------------------------
# 400 -- tastypie filter validation
# ---------------------------------------------------------------------------


def test_400_invalid_location_names_the_field():
    payload = fixture_json("error_400_location.json")
    assert payload == {"job_locations": ["Invalid location"]}
    http = make_http({SEARCH: json_response(payload, status=400)})

    with pytest.raises(InvalidFilter) as excinfo:
        http.get(SEARCH, params={"jobLocations": "bangalore"})

    error = excinfo.value
    assert error.field == "job_locations", "the field is reported as the server spelled it"
    assert error.kind == "invalid_filter"
    assert "Invalid location" in error.message
    assert error.context["status"] == 400


def test_400_invalid_company_names_the_field():
    payload = fixture_json("error_400_company.json")
    assert payload == {"companies": ["Invalid company"]}
    http = make_http({SEARCH: json_response(payload, status=400)})

    with pytest.raises(InvalidFilter) as excinfo:
        http.get(SEARCH, params={"companies": "Definitely Not A Company"})

    assert excinfo.value.field == "companies"
    assert "Invalid company" in excinfo.value.message


def test_400_is_not_retried():
    """A 400 is a verdict about the request, so retrying is pure waste."""
    http = make_http({SEARCH: json_response({"companies": ["Invalid company"]}, status=400)})

    with pytest.raises(InvalidFilter):
        http.get(SEARCH)

    assert http.routes.count() == 1, "400 must be attempted exactly once"


def test_400_with_an_unparseable_body_still_raises_invalid_filter():
    http = make_http({SEARCH: html_response(b"<html>nope</html>", status=400)})
    with pytest.raises(InvalidFilter) as excinfo:
        http.get(SEARCH)
    assert excinfo.value.field is None


# ---------------------------------------------------------------------------
# Cloudflare
# ---------------------------------------------------------------------------


def test_cf_mitigated_header_wins_over_a_200():
    """The tripwire: when bot management is on, the status code is a lie."""
    http = make_http(
        {
            SEARCH: json_response(
                fixture_json("search_backend_blr.json"),
                status=200,
                headers={"cf-mitigated": "challenge"},
            )
        }
    )

    with pytest.raises(ChallengeDetected) as excinfo:
        http.get(SEARCH)

    assert excinfo.value.kind == "challenge_detected"
    assert excinfo.value.context["status"] == 200
    assert "challenge" in excinfo.value.message


def test_cf_mitigated_header_wins_over_a_404():
    """Checked before every status branch, not just the happy one."""
    http = make_http(
        {
            JOB_DETAIL_PATH: html_response(
                HTML_404_BODY, status=404, headers={"cf-mitigated": "challenge"}
            )
        }
    )
    with pytest.raises(ChallengeDetected):
        http.get(JOB_DETAIL_PATH)


def test_403_with_html_body_raises_challenge_detected():
    http = make_http({SEARCH: html_response(HTML_CHALLENGE_BODY, status=403)})

    with pytest.raises(ChallengeDetected) as excinfo:
        http.get(SEARCH)

    assert excinfo.value.context["status"] == 403


def test_403_with_json_body_is_a_plain_api_error():
    """Only an HTML 403 means Cloudflare; a JSON 403 is the app saying no."""
    http = make_http({SEARCH: json_response({"detail": "forbidden"}, status=403)})
    with pytest.raises(ApiError) as excinfo:
        http.get(SEARCH)
    assert not isinstance(excinfo.value, ChallengeDetected)


def test_200_html_on_a_json_endpoint_raises_challenge_not_a_parse_error():
    http = make_http({SEARCH: html_response(HTML_CHALLENGE_BODY, status=200)})

    with pytest.raises(ChallengeDetected) as excinfo:
        http.get(SEARCH)

    assert "HTML where JSON was expected" in excinfo.value.message


def test_html_is_detected_by_body_sniff_even_without_a_content_type():
    """Content-Type is not trusted on its own."""
    http = make_http(
        {SEARCH: httpx.Response(200, content=b"<!DOCTYPE html><html></html>", headers={})}
    )
    with pytest.raises(ChallengeDetected):
        http.get(SEARCH)


# ---------------------------------------------------------------------------
# 429
# ---------------------------------------------------------------------------


def test_429_raises_rate_limited_carrying_retry_after():
    http = make_http(
        {SEARCH: json_response({"detail": "slow down"}, status=429, headers={"retry-after": "30"})},
        max_retries=1,
    )

    with pytest.raises(RateLimited) as excinfo:
        http.get(SEARCH)

    assert excinfo.value.kind == "rate_limited"
    assert excinfo.value.context["retry_after"] == 30.0


def test_429_without_a_retry_after_header_reports_none():
    http = make_http({SEARCH: json_response({"detail": "slow"}, status=429)}, max_retries=1)
    with pytest.raises(RateLimited) as excinfo:
        http.get(SEARCH)
    assert excinfo.value.context["retry_after"] is None


def test_429_is_retried_and_honours_retry_after_as_the_backoff(no_real_sleep):
    http = make_http(
        {SEARCH: json_response({"detail": "slow"}, status=429, headers={"retry-after": "5"})},
        max_retries=3,
    )
    with pytest.raises(RateLimited):
        http.get(SEARCH)
    assert http.routes.count() == 3
    assert no_real_sleep.sleeps == [5.0, 5.0]


# ---------------------------------------------------------------------------
# 5xx and retries
# ---------------------------------------------------------------------------


def test_500_is_retried_up_to_max_retries_then_raises_api_error(no_real_sleep):
    http = make_http({SEARCH: json_response({"error": "boom"}, status=500)}, max_retries=3)

    with pytest.raises(ApiError) as excinfo:
        http.get(SEARCH)

    assert excinfo.value.kind == "api_error"
    assert excinfo.value.context["status"] == 500
    assert http.routes.count() > 1, "a 500 must be retried, not given up on"
    assert http.routes.count() == 3, "exactly max_retries attempts"
    assert len(no_real_sleep.sleeps) == 2, "backoff between the three attempts"
    assert no_real_sleep.sleeps[1] > no_real_sleep.sleeps[0], "backoff must grow"


def test_a_500_that_recovers_returns_the_second_answer():
    """Retry exists to succeed, not merely to postpone the same failure."""
    payload = fixture_json("search_backend_blr.json")
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return json_response({"error": "boom"}, status=500)
        return json_response(payload)

    http = make_http({SEARCH: flaky})
    result = http.get(SEARCH)
    assert calls["n"] == 2
    assert len(result["objects"]) == 35


def test_503_is_retried_too(no_real_sleep):
    http = make_http({SEARCH: json_response({"e": 1}, status=503)}, max_retries=2)
    with pytest.raises(ApiError):
        http.get(SEARCH)
    assert http.routes.count() == 2


# ---------------------------------------------------------------------------
# Transport failures
# ---------------------------------------------------------------------------


def test_timeout_raises_transport_error():
    def timeout(request):
        raise httpx.ReadTimeout("read timed out", request=request)

    http = make_http({SEARCH: timeout})

    with pytest.raises(TransportError) as excinfo:
        http.get(SEARCH)

    assert excinfo.value.kind == "transport_error"
    assert "timed out" in excinfo.value.message
    assert http.routes.count() == 3, "transport failures get the full retry budget"


def test_connect_error_raises_transport_error():
    def refused(request):
        raise httpx.ConnectError("connection refused", request=request)

    http = make_http({SEARCH: refused}, max_retries=1)

    with pytest.raises(TransportError) as excinfo:
        http.get(SEARCH)

    assert "ConnectError" in excinfo.value.message


def test_every_error_derives_from_instahyre_error():
    """One except clause at the tool boundary has to catch all of them."""
    for cls in (
        NotFound,
        InvalidFilter,
        AuthRequired,
        ChallengeDetected,
        RateLimited,
        ApiError,
        TransportError,
    ):
        assert issubclass(cls, InstahyreError)


# ---------------------------------------------------------------------------
# Success paths
# ---------------------------------------------------------------------------


def test_200_json_returns_the_parsed_payload():
    payload = fixture_json("search_backend_blr.json")
    http = make_http({SEARCH: payload})
    result = http.get(SEARCH)
    assert result["meta"]["total_count"] == 1086
    assert http.request_count == 1


def test_204_returns_an_empty_dict():
    http = make_http({SEARCH: httpx.Response(204)})
    assert http.get(SEARCH) == {}


def test_unexpected_status_raises_api_error():
    http = make_http({SEARCH: httpx.Response(302, headers={"location": "/elsewhere"})})
    with pytest.raises(ApiError) as excinfo:
        http.get(SEARCH)
    assert excinfo.value.context["status"] == 302


def test_unparseable_json_raises_api_error_not_a_value_error():
    http = make_http(
        {SEARCH: httpx.Response(200, content=b"{not json", headers={"content-type": "application/json"})}
    )
    with pytest.raises(ApiError) as excinfo:
        http.get(SEARCH)
    assert "not valid JSON" in excinfo.value.message


def test_unmocked_path_fails_loudly():
    """The harness's own guard: a path nobody declared must not answer empty."""
    http = make_http({SEARCH: {"objects": [], "meta": {}}})
    with pytest.raises(AssertionError) as excinfo:
        http.get("/some/other/endpoint/")
    assert "Unmocked request" in str(excinfo.value)


# ---------------------------------------------------------------------------
# _flatten_params
# ---------------------------------------------------------------------------


def test_flatten_params_repeats_keys_and_drops_none():
    flat = _flatten_params({"skills": ["A", "B"], "years": 5, "x": None})
    assert flat == [("skills", "A"), ("skills", "B"), ("years", "5")]


def test_flatten_params_drops_none_inside_a_list():
    assert _flatten_params({"skills": ["A", None, "B"]}) == [("skills", "A"), ("skills", "B")]


def test_flatten_params_renders_booleans_lowercase():
    assert _flatten_params({"flag": True, "other": False}) == [("flag", "true"), ("other", "false")]


def test_flatten_params_on_none_and_empty():
    assert _flatten_params(None) == []
    assert _flatten_params({}) == []


def test_repeated_keys_reach_the_wire():
    """The flattening is only useful if httpx actually emits repeated keys."""
    http = make_http({SEARCH: {"objects": [], "meta": {}}})
    http.get(SEARCH, params={"skills": ["Node.js", "TypeScript"], "years": 5})
    assert http.routes.params_for(SEARCH)[0] == [
        ("skills", "Node.js"),
        ("skills", "TypeScript"),
        ("years", "5"),
    ]


def test_min_interval_zero_means_no_pacing_sleep(no_real_sleep):
    http = make_http({SEARCH: {"objects": [], "meta": {}}})
    http.get(SEARCH)
    http.get(SEARCH)
    assert no_real_sleep.sleeps == [], "tests must never pace"
    assert http.request_count == 2


def test_pacing_sleeps_when_min_interval_is_set(no_real_sleep):
    """The pacing exists; the tests merely switch it off."""
    http = make_http({SEARCH: {"objects": [], "meta": {}}}, min_interval=1.2)
    http.get(SEARCH)
    http.get(SEARCH)
    assert len(no_real_sleep.sleeps) == 1
    assert 1.0 <= no_real_sleep.sleeps[0] <= 1.4
