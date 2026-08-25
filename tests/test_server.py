"""server.py -- the MCP surface contract.

Nothing here starts a client or makes a request; ``get_client()`` is lazy, so
listing tools and exercising the error decorator touch neither the network nor
the state dir.
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp.exceptions import ToolError

from instahyre_server.errors import (
    ApiError,
    AuthRequired,
    ChallengeDetected,
    InvalidFilter,
    NotFound,
    RateLimited,
    TransportError,
)
from instahyre_server.server import handled, mcp

EXPECTED_TOOLS = {
    # The 2026-08-23 write tier: five tools, each on a surface whose
    # request body was captured before the tool was written.
    "instahyre_support_ticket",
    "instahyre_toggle_job_alert",
    "instahyre_referral_link",
    "instahyre_referral_contacts",
    "instahyre_send_referral_invites",
    "instahyre_search_jobs",
    "instahyre_get_job",
    "instahyre_get_company",
    "instahyre_list_job_functions",
    "instahyre_list_locations",
    "instahyre_list_industries",
    "instahyre_market_stats",
    "instahyre_sync_index",
    "instahyre_rank_jobs",
    "instahyre_auth_status",
    "instahyre_login",
    "instahyre_login_browser",
    "instahyre_logout",
    # The auth lifecycle, added 2026-08-23 to the four-server contract in
    # _audit/2026-08-23-auth-contract.md. session_info reports the credential
    # and its expiry; reauth renews it silently from the browser profile.
    "instahyre_session_info",
    "instahyre_reauth",
    "instahyre_server_info",
    "instahyre_config",
    # Tier 2 -- authenticated.
    "instahyre_inbound_digest",
    "instahyre_list_opportunities",
    "instahyre_get_opportunity",
    "instahyre_opportunity_counts",
    "instahyre_recruiter_activity",
    "instahyre_list_applications",
    "instahyre_get_profile",
    "instahyre_account_settings",
    "instahyre_apply",
    "instahyre_decline_opportunity",
    # Tier 3 -- the inbox. Reads are plain HTTP with no browser; the four
    # writes below run on a NAMED allowlist of four URLs, one per captured
    # contract.
    "instahyre_list_conversations",
    "instahyre_read_conversation",
    "instahyre_inbox_counts",
    "instahyre_reply_to_conversation",
    # Added 2026-08-25. All three retire refusals that had stopped resting on
    # evidence -- their contracts were captured on 2026-08-23 and the tools
    # were withheld on value until the ruling changed. mark_all_conversations_
    # read is the one to read twice: it is a GET that bulk-mutates, and it
    # carries the same confirm gate as any POST here.
    "instahyre_star_conversation",
    "instahyre_mark_conversation_read",
    "instahyre_mark_all_conversations_read",
    # Read-only analysis over what the account already holds. No new endpoint
    # between them: skill_gap re-reads the queue, resume_info follows an id the
    # profile already publishes, saved_searches exposes a route the client had
    # implemented and never surfaced.
    "instahyre_skill_gap",
    "instahyre_resume_info",
    "instahyre_saved_searches",
    # Tier 4 -- profile writes. These change his account.
    "instahyre_update_skills",
    "instahyre_update_profile",
    # Added 2026-08-24. It retires a refusal rather than filling a gap: the
    # job-search-profile fields were named as NOT writable, on the honest
    # ground that the whole-object PUT was unverified. It was verified.
    "instahyre_update_job_search_profile",
    "instahyre_restore_profile",
    "instahyre_list_profile_snapshots",
    "instahyre_verify_apply_target",
    # The inbound watch. Three tools, none of which runs unattended -- see
    # instahyre_server/inbound_watch.py for why that is a design decision and
    # not an omission.
    "instahyre_whats_new",
    "instahyre_watch_status",
    "instahyre_watch_forget",
}


@pytest.fixture(scope="module")
def tools():
    return asyncio.run(mcp.list_tools())


# ---------------------------------------------------------------------------
# The handled decorator
# ---------------------------------------------------------------------------


def test_handled_turns_a_typed_error_into_a_tool_error_carrying_the_kind():
    @handled
    def boom():
        raise NotFound("No such resource: /employer_public_jobs/999999/", path="/x", status=404)

    with pytest.raises(ToolError) as excinfo:
        boom()

    message = str(excinfo.value)
    assert message.startswith("[not_found] ")
    assert "No such resource" in message
    assert "status=404" in message


def test_handled_carries_the_field_of_an_invalid_filter():
    @handled
    def boom():
        raise InvalidFilter("'bangalore' is not a valid location.", field="jobLocations")

    with pytest.raises(ToolError) as excinfo:
        boom()

    assert "[invalid_filter]" in str(excinfo.value)
    assert "field=jobLocations" in str(excinfo.value)


@pytest.mark.parametrize(
    "error, kind",
    [
        (NotFound("x"), "not_found"),
        (InvalidFilter("x"), "invalid_filter"),
        (AuthRequired("x"), "auth_required"),
        (ChallengeDetected("x"), "challenge_detected"),
        (RateLimited("x"), "rate_limited"),
        (ApiError("x"), "api_error"),
        (TransportError("x"), "transport_error"),
    ],
)
def test_handled_labels_every_error_kind(error, kind):
    @handled
    def boom():
        raise error

    with pytest.raises(ToolError) as excinfo:
        boom()
    assert str(excinfo.value).startswith("[%s]" % kind)


def test_handled_never_swallows_the_error_into_a_return_value():
    @handled
    def boom():
        raise NotFound("gone")

    result = "sentinel"
    with pytest.raises(ToolError):
        result = boom()
    assert result == "sentinel"


def test_handled_leaves_a_successful_return_untouched():
    @handled
    def fine(a, b=2):
        """A docstring the decorator must preserve."""
        return {"a": a, "b": b}

    assert fine(1, b=3) == {"a": 1, "b": 3}
    assert fine.__name__ == "fine"
    assert fine.__doc__ == "A docstring the decorator must preserve."


def test_handled_does_not_catch_unrelated_exceptions():
    """Only InstahyreError is translated; a real bug must stay a real bug."""

    @handled
    def boom():
        raise ValueError("programmer error")

    with pytest.raises(ValueError):
        boom()


# ---------------------------------------------------------------------------
# The tool registry
# ---------------------------------------------------------------------------


def test_the_server_registers_exactly_fifty_one_tools(tools):
    """48 until 2026-08-25, when the three remaining measured inbox writes were
    built. The number is pinned rather than derived so that a tool appearing on
    the MCP surface is an edit somebody made here on purpose."""
    assert len(tools) == len(EXPECTED_TOOLS) == 51


def test_every_tool_name_is_namespaced(tools):
    for tool in tools:
        assert tool.name.startswith("instahyre_"), tool.name


def test_the_registered_tool_names_are_the_expected_set(tools):
    assert {tool.name for tool in tools} == EXPECTED_TOOLS


def test_every_tool_has_a_non_empty_description(tools):
    for tool in tools:
        assert tool.description, "%s has no docstring" % tool.name
        assert tool.description.strip(), "%s has a blank docstring" % tool.name


def test_every_tool_description_is_a_real_sentence_not_a_placeholder(tools):
    for tool in tools:
        assert len(tool.description.strip()) > 40, "%s is barely documented" % tool.name


def test_there_is_no_bulk_apply_tool(tools):
    """Bulk apply must never exist on this server: Instahyre applications
    cannot be withdrawn once sent."""
    offenders = [tool.name for tool in tools if "bulk" in tool.name.lower()]
    assert offenders == []


def test_no_tool_promises_a_sort_argument(tools):
    """The API accepts a sort parameter and demonstrably ignores it."""
    for tool in tools:
        assert "sort" not in tool.parameters.get("properties", {}), tool.name


def test_the_server_instructions_state_the_two_absent_fields():
    instructions = mcp.instructions or ""
    assert "NO salary data" in instructions
    assert "NO posting dates" in instructions
    assert "cannot be withdrawn" in instructions.lower()


def test_search_tool_exposes_the_agency_filters(tools):
    tool = next(t for t in tools if t.name == "instahyre_search_jobs")
    properties = tool.parameters["properties"]
    assert "exclude_agencies" in properties
    assert "show_agency_flag" in properties
    assert "offset" in properties


def test_listing_tools_makes_no_request_and_builds_no_client(tools):
    """The client is lazy on purpose, so importing the server is inert."""
    from instahyre_server import server

    assert server._client is None
    assert server._sessions is None
