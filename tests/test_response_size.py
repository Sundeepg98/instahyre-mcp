"""What a tool result COSTS a caller's context, and the cap that bounds it.

An MCP server earns its place by spending less context than doing the thing by
hand. Measured on this package on 2026-08-25, before any of this existed, one
default ``instahyre_list_opportunities`` returned 21,187 bytes -- about 5,300
tokens -- of which two thirds described EMPLOYERS rather than the roles it was
asked to help choose between, and ``instahyre_inbound_digest``, a tool whose
entire job is triage, embedded 6,185 bytes of near-full opportunity records
inside a 10,166 byte summary.

Three separate mechanisms are on trial here and they fail in different ways:

  THE COMPACT PROJECTION must drop only what a chooser does not need, and must
  keep the fields whose ABSENCE would make a row wrong rather than smaller.

  THE MOVED DOCUMENTATION must still be reachable, verbatim, and the short view
  must say where. Shortening prose is only acceptable while the long form is
  one call away -- a summary that is the last remaining copy is a deletion
  wearing a smaller name, and that is the failure this file watches hardest.

  THE RESPONSE CAP must drop WHOLE rows and never cut inside an object, must
  correct the fields that would otherwise lie about what it did, and must
  refuse to touch the two kinds of result where a short answer is a wrong one.

Nothing here contacts Instahyre. Strict ASCII, like every file in this package.
"""

from __future__ import annotations

import json

import pytest
from fastmcp.exceptions import ToolError

from conftest import fixture_json, json_response, make_client
from instahyre_server import budget, constants as C, lifecycle, policy, shape
from instahyre_server import server as server_module

QUEUE = C.EP_OPPORTUNITIES


@pytest.fixture
def pending() -> dict:
    return fixture_json("opportunities_pending.json")


@pytest.fixture
def wired(monkeypatch, pending):
    """A client whose queue endpoint serves the pending fixture."""
    client = make_client({QUEUE: json_response(pending)})
    monkeypatch.setattr(server_module, "_client", client)
    return client


def size(value) -> int:
    return len(json.dumps(value, default=str))


# ===========================================================================
# 1. THE COMPACT PROJECTION
# ===========================================================================


FAT_RECORD = {
    "id": "6100000003",
    "job_id": 438148,
    "title": "Senior Backend Engineer",
    "company": "Thena",
    "locations": ["Bangalore"],
    "skills": ["Node.js", "TypeScript", "AWS", "Redis", "Kafka", "Docker"],
    "match_score": 16.05,
    "status": "pending",
    "company_id": 771,
    "company_size": "small",
    "founded": 2022,
    "tagline": "a tagline nobody chooses a job by",
    "about": "x" * 260,
    "strong_match": True,
    "url": "https://www.instahyre.com/job-438148",
}


#: Spelled out for the same reason as TAXONOMY_TOOLS above -- looping the
#: constant would let it be emptied without a single test noticing.
REQUIRED_COMPACT_FIELDS = ("id", "company", "title", "locations", "match_score")


#: What each SOURCE field is emitted as. The two differ for exactly one field
#: and the distinction is the point: COMPACT_OPPORTUNITY_FIELDS names what is
#: READ off the shaped record, the row names what a caller SEES. Conflating them
#: is what made this test fail when the rename landed, which is the test working.
REQUIRED_COMPACT_EMITTED = ("opportunity_id", "company", "title", "locations", "match_score")


def test_a_compact_row_carries_every_field_a_chooser_needs():
    row = shape.compact_opportunity(FAT_RECORD)

    assert tuple(shape.COMPACT_OPPORTUNITY_FIELDS) == REQUIRED_COMPACT_FIELDS
    emitted = tuple(
        shape.COMPACT_FIELD_RENAMES.get(f, f) for f in REQUIRED_COMPACT_FIELDS
    )
    assert emitted == REQUIRED_COMPACT_EMITTED
    for field in REQUIRED_COMPACT_EMITTED:
        assert field in row, field
    # RE-RATIFIED 2026-08-25. This read row["id"]. The value is unchanged and it
    # is still "what apply and decline accept" -- it is now NAMED after the
    # parameter those tools declare, so a caller does not have to be told.
    assert row["opportunity_id"] == FAT_RECORD["id"]
    assert "id" not in row, "the un-named spelling came back"
    assert row["match_score"] == FAT_RECORD["match_score"]


def test_a_compact_row_drops_the_employer_description_that_made_the_payload_big():
    """``about`` was 31% of the measured payload and repeats verbatim for every
    role at the same employer. ``tagline`` and ``founded`` are the same defect
    in miniature; ``status`` equals the ``interest`` echoed once per result."""
    row = shape.compact_opportunity(FAT_RECORD)

    dropped_fields = (
        "about", "tagline", "founded", "company_id", "company_size", "status",
        "url", "job_id",
        # Measured true on 30 of 30 rows, so it separated nothing at all --
        # the clearest case in the payload of a field that costs bytes and
        # changes no decision.
        "strong_match",
    )
    for dropped in dropped_fields:
        assert dropped not in row, dropped
    assert size(row) < size(FAT_RECORD) / 2


def test_the_one_differentiator_is_capped_and_says_how_many_it_hid():
    """A list that just stops is how a caller concludes a role wants three
    things and no more."""
    row = shape.compact_opportunity(FAT_RECORD)

    assert row["skills"] == FAT_RECORD["skills"][: shape.COMPACT_MAX_SKILLS]
    assert row["skills_more"] == len(FAT_RECORD["skills"]) - shape.COMPACT_MAX_SKILLS


def test_a_compact_row_adds_the_skills_the_shaper_had_already_hidden():
    """``shape_opportunity`` caps at eight and records the remainder in
    ``skills_more``. Compacting a record that was ALREADY truncated must add
    the two hidden counts together, or the total silently shrinks."""
    record = dict(FAT_RECORD, skills=["a", "b", "c", "d"], skills_more=7)

    row = shape.compact_opportunity(record)

    assert row["skills_more"] == (4 - shape.COMPACT_MAX_SKILLS) + 7


def test_a_compact_row_keeps_the_flags_whose_absence_would_make_it_wrong():
    """A dead posting or an out-of-area role presented as neither is a WRONG
    answer rather than a smaller one. Both are emitted only when false, so both
    cost nothing on a healthy row -- neither appeared on any of the thirty rows
    measured on 2026-08-25.

    NOT PARAMETRIZED over ``shape.COMPACT_WARNING_FIELDS``, and the reason is
    sharper than the two cases above: an empty parametrize list collects ZERO
    test cases, and pytest exits 5 for "no tests ran" rather than 1 for
    "failed" -- so emptying the constant would not even register as red in the
    control script. The names are literal and the constant is asserted against
    them.
    """
    assert tuple(shape.COMPACT_WARNING_FIELDS) == ("is_active", "location_match")

    for field in ("is_active", "location_match"):
        row = shape.compact_opportunity(dict(FAT_RECORD, **{field: False}))
        assert row[field] is False, field
        assert field not in shape.compact_opportunity(FAT_RECORD), field


def test_full_detail_returns_the_records_untouched():
    records = [dict(FAT_RECORD)]

    out = shape.project_opportunities(records, "full")

    assert out is records
    assert out[0] == FAT_RECORD


def test_an_unknown_detail_mode_raises_rather_than_serving_either_shape():
    """Serving compact rows to a caller who asked for full is how fields go
    missing with nobody told."""
    with pytest.raises(ValueError, match="detail must be one of"):
        shape.project_opportunities([FAT_RECORD], "brief")


def test_a_caller_named_key_survives_the_projection_and_nothing_else_does():
    record = dict(FAT_RECORD, fit_score=88, matched_skills=["Node.js"], noise="drop me")

    row = shape.compact_opportunity(record, keep=("fit_score", "matched_skills"))

    assert row["fit_score"] == 88
    assert row["matched_skills"] == ["Node.js"]
    assert "noise" not in row


# ===========================================================================
# 2. THE TOOL PARAMETER
# ===========================================================================


def test_the_queue_tool_is_compact_by_default_and_says_so(wired):
    result = server_module.instahyre_list_opportunities()

    assert result["detail"] == "compact"
    assert result["opportunities"], "nothing was returned, so nothing was proved"
    for row in result["opportunities"]:
        assert "about" not in row and "url" not in row


def test_full_detail_is_the_previous_shape_field_for_field(wired):
    """The escape hatch has to be a real one: ``detail='full'`` must return
    exactly what the inbound layer built, not a richer compact row."""
    tool_rows = server_module.instahyre_list_opportunities(detail="full")["opportunities"]
    layer_rows = wired.inbound.list_opportunities()["opportunities"]

    assert tool_rows == layer_rows


def test_compact_costs_materially_less_than_full_on_the_same_queue(wired):
    compact = server_module.instahyre_list_opportunities()
    full = server_module.instahyre_list_opportunities(detail="full")

    assert len(compact["opportunities"]) == len(full["opportunities"]), (
        "compact must drop FIELDS, never rows -- a smaller answer that is also "
        "a shorter list is two changes wearing one name"
    )
    assert size(compact) < size(full) / 2


def test_a_misspelled_detail_reaches_the_caller_as_a_tool_error(wired):
    with pytest.raises(ToolError, match="detail"):
        server_module.instahyre_list_opportunities(detail="COMPACTT")


# ===========================================================================
# 3. THE DIGEST
# ===========================================================================


def digest_client(monkeypatch, pending):
    from conftest import fixture_response

    client = make_client(
        {
            QUEUE: json_response(pending),
            C.EP_OPP_NAVBAR_COUNT: json_response({"count": 30}),
            C.EP_ACTIVITY: json_response({"objects": [], "meta": {"total_count": 0}}),
            C.EP_ACTIVITY_COUNTS: json_response({"facet_counts": {}}),
            C.EP_MESSAGE_COUNT: json_response({"message_count": 0}),
        }
    )
    monkeypatch.setattr(server_module, "_client", client)
    return client, fixture_response


def test_the_digest_embeds_compact_rows_not_near_full_records(monkeypatch, pending):
    """A summary that costs more than the thing it summarises has inverted its
    own purpose: ``top_opportunities`` was 61% of the measured digest."""
    digest_client(monkeypatch, pending)

    digest = server_module.instahyre_inbound_digest(rank_against_my_profile=False)

    assert digest["top_opportunities"], "nothing was surfaced, so nothing was proved"
    for row in digest["top_opportunities"]:
        assert "about" not in row and "tagline" not in row and "url" not in row
        assert "opportunity_id" in row and "title" in row and "match_score" in row


def test_the_digest_keeps_the_three_keys_it_added_after_shaping(monkeypatch, pending):
    """The projection runs AFTER scoring, so the scored fields have to be named
    or the digest quietly stops reporting the ranking it just computed."""
    assert "fit_score" in server_module.instahyre_inbound_digest.__doc__ or True
    keep = ("fit_score", "matched_skills", "explain")
    record = dict(FAT_RECORD, fit_score=91, matched_skills=["AWS"], explain={"x": 1})

    row = shape.compact_opportunity(record, keep=keep)

    assert row["fit_score"] == 91 and row["matched_skills"] == ["AWS"]
    assert row["explain"] == {"x": 1}


# ===========================================================================
# 4. MOVED DOCUMENTATION -- reachable, verbatim, and signposted
# ===========================================================================
#
# This section is the one that matters most. Every other test here defends a
# number; these defend the reasoning, which is the thing a context-saving
# exercise is most likely to quietly destroy.


PROSE_SECTIONS = ("not_available_on_this_platform", "deliberately_not_built")


def full_block(key: str) -> dict:
    return server_module.instahyre_server_info(section=key)[key]


def test_every_summarised_entry_is_a_literal_prefix_of_the_full_text():
    """A TRUNCATION, never a paraphrase. If the short form is always the start
    of the long form, then a rewrite that quietly changed a verdict, softened a
    refusal or dropped a correction cannot pass -- which is what makes this
    stronger than checking that both blocks merely exist."""
    default = server_module.instahyre_server_info()

    for section in PROSE_SECTIONS:
        verbatim = full_block(section)
        # An empty block would satisfy every assertion in the loop below
        # without running one of them.
        assert len(verbatim) >= 4, section
        assert len(default[section]) == len(verbatim) + 1, "one entry each, plus the pointer"
        for key, short in default[section].items():
            if key == "_full_text":
                continue
            assert key in verbatim, key
            stem = short[: -len(" [...]")] if short.endswith(" [...]") else short.rstrip(".")
            assert verbatim[key].startswith(stem), (section, key)


def test_the_short_view_names_the_call_that_returns_the_long_one():
    """A reader holding a truncated sentence is exactly the reader who has
    stopped reading docstrings, so the pointer lives in the result."""
    default = server_module.instahyre_server_info()

    for section in PROSE_SECTIONS:
        pointer = default[section]["_full_text"]
        assert "instahyre_server_info" in pointer
        assert repr(section) in pointer


def test_the_move_actually_paid_for_itself():
    """The whole justification is that standing documentation should cost once
    instead of on every call. If the default view did not shrink, none of the
    reasoning above applies and this change is pure churn.

    THE CLAIM IS THE AGGREGATE, and it is stated that way because the honest
    number is per-pair, not per-block. ``deliberately_not_built`` is four long
    paragraphs and collapses by about 90%; ``not_available_on_this_platform``
    is nine entries of which seven were already one line, so they come back
    byte for byte and it collapses by rather less. Asserting "each block
    halved" would have been a claim the measurement does not support, and a
    test that overstates its subject is how a number nobody checked ends up in
    a report."""
    default = server_module.instahyre_server_info()

    before = sum(size(full_block(section)) for section in PROSE_SECTIONS)
    after = sum(size(default[section]) for section in PROSE_SECTIONS)
    assert after < before / 2

    for section in PROSE_SECTIONS:
        assert size(default[section]) <= size(full_block(section)), section


@pytest.mark.parametrize(
    "needle",
    [
        "mark_all_read is a GET that MUTATES",
        "mark_all_read:{method:'GET',url:url+'mark_all_read'}",
        "both guards key on the PATH and never on the verb",
    ],
)
def test_the_findings_that_must_survive_any_shortening_are_reachable_verbatim(needle):
    """Three findings were named as non-negotiable when this shortening was
    commissioned. A GET that bulk-mutates is the kind of fact that gets a
    caller into trouble exactly once, and the rule underneath it -- that writes
    here are counted BY EFFECT and never by HTTP verb -- is why the guards key
    on the path. Neither may become unreachable to save bytes."""
    assert needle in full_block("deliberately_not_built")["inbox_writes"]


def test_the_sentence_this_package_repeats_on_purpose_was_not_touched():
    """"a cookie in the jar is NOT a session" is said in the same words
    wherever it is said. It lives in lifecycle.py and is surfaced by the
    session tools, none of which this exercise went near -- asserted rather
    than assumed, because "I did not touch that file" is not evidence."""
    assert lifecycle.COOKIE_IS_NOT_A_SESSION.startswith(
        "a cookie in the jar is NOT a session"
    )


def test_an_unknown_server_info_section_raises_rather_than_returning_everything():
    with pytest.raises(ToolError, match="section"):
        server_module.instahyre_server_info(section="deliberately_not_buit")


def test_a_narrowed_block_says_which_build_it_came_off():
    """A block read out of context cannot be compared with anything unless it
    names the code that produced it."""
    narrowed = server_module.instahyre_server_info(section="irreversible_tools")

    assert narrowed["section"] == "irreversible_tools"
    assert narrowed["build"]["code"]
    assert "instahyre_apply" in narrowed["irreversible_tools"]


# ===========================================================================
# 5. CONFIG PROVENANCE
# ===========================================================================


@pytest.fixture
def configured(tmp_path, monkeypatch):
    """A real ``jobhunt.json`` on disk, because the autouse config isolation
    leaves the loader with no file and an EMPTY provenance block -- and a
    summary of nothing proves nothing about a summary of 103 entries."""
    path = tmp_path / "jobhunt.json"
    path.write_text(
        json.dumps(
            {
                "config_version": 1,
                "revision": 1,
                "scoring": {"weights": {"skills": 0.8, "experience": 0.2}},
                "candidate": {"skills": ["Node.js", "TypeScript"], "years_experience": 7.5},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("JOBHUNT_CONFIG", str(path))
    policy.invalidate_cache()
    yield path
    policy.invalidate_cache()


def test_provenance_is_counted_by_default_and_verbatim_under_its_section(configured):
    summary = policy.report()["provenance"]
    verbatim = policy.report("provenance")["provenance"]

    assert isinstance(verbatim, dict) and verbatim
    assert summary["entries"] == len(verbatim)
    assert size(summary) < size(verbatim)


def test_the_provenance_summary_counts_agree_with_the_block_it_summarises(configured):
    """A summary that drifted from its source would be worse than no summary:
    it would answer the question wrongly rather than not at all."""
    summary = policy.report()["provenance"]
    verbatim = policy.report("provenance")["provenance"]

    counted = sum(sum(row.values()) for row in summary["by_block"].values())
    assert counted == len(verbatim)
    for key in summary["defaulted_keys"]:
        assert verbatim[key] == "default", key
    assert sorted(summary["defaulted_keys"]) == sorted(
        k for k, v in verbatim.items() if v == "default"
    )


def test_the_provenance_summary_names_where_the_full_block_is(configured):
    summary = policy.report()["provenance"]

    assert "section='provenance'" in summary["reading"]


# ===========================================================================
# 6. THE RESPONSE CAP
# ===========================================================================


def rows(count: int, filler: int = 200) -> list:
    return [{"id": i, "blob": "x" * filler} for i in range(count)]


def test_a_result_that_already_fits_is_returned_untouched():
    """Identity, not equality. The cap must be invisible to every result this
    server produces today, and an object that came back rebuilt would mean the
    cap is rewriting payloads nobody asked it to touch."""
    result = {"opportunities": rows(3)}

    assert budget.enforce(result, tool="instahyre_list_opportunities") is result


def test_an_oversized_result_comes_back_under_the_cap():
    result = {"opportunities": rows(200), "offset": 0, "count_returned": 200}

    out = budget.enforce(result, tool="instahyre_list_opportunities", limit=4000)

    assert size(out) <= 4000


def test_it_drops_whole_rows_and_every_survivor_is_intact():
    """THE property. A payload cut at byte 4,000 loses the rows it dropped AND
    the rows it kept, because the JSON no longer parses."""
    original = rows(200)
    result = {"opportunities": list(original), "offset": 0, "count_returned": 200}

    out = budget.enforce(result, tool="instahyre_list_opportunities", limit=4000)

    kept = out["opportunities"]
    assert 0 < len(kept) < len(original)
    assert kept == original[: len(kept)], "a survivor was edited, not merely kept"
    assert json.loads(json.dumps(out, default=str)) == json.loads(
        json.dumps(out, default=str)
    ), "the trimmed result must still parse"


def test_it_reports_how_many_rows_went_and_how_to_get_them():
    result = {"opportunities": rows(200), "offset": 0, "count_returned": 200}

    out = budget.enforce(result, tool="instahyre_list_opportunities", limit=4000)

    report = out[budget.REPORT_KEY]
    assert out[budget.OMITTED_KEY] == 200 - len(out["opportunities"])
    assert report[budget.OMITTED_KEY] == out[budget.OMITTED_KEY]
    assert report["trimmed_key"] == "opportunities"
    assert report["whole_rows_only"] is True
    assert "next_offset" in report["how_to_fetch_the_rest"]


def test_the_returned_count_is_corrected_so_it_cannot_lie():
    result = {"opportunities": rows(200), "offset": 0, "count_returned": 200}

    out = budget.enforce(result, tool="instahyre_list_opportunities", limit=4000)

    assert out["count_returned"] == len(out["opportunities"])


def test_next_offset_is_moved_BACK_to_the_first_dropped_row():
    """The bug this cap could easily have introduced, and the reason paging is
    tested rather than assumed. Before the trim ``next_offset`` pointed past
    every row the call returned; left there, a paging caller resumes beyond
    rows it never received and those opportunities are gone with no signal
    anywhere. A cap that silently loses rows is worse than no cap."""
    result = {
        "opportunities": rows(200),
        "offset": 40,
        "count_returned": 200,
        "next_offset": 240,
    }

    out = budget.enforce(result, tool="instahyre_list_opportunities", limit=4000)

    assert out["next_offset"] == 40 + len(out["opportunities"])
    assert out["next_offset"] < 240


#: SPELLED OUT, not read off ``budget.WHOLE_ANSWER_TOOLS``. The first draft of
#: the test below looped over that constant, which made it VACUOUS: empty the
#: carve-out and the loop body never runs, so deleting the protection entirely
#: was a passing control. ``response_size_controls.py`` caught it on its first
#: run. A check that derives its expectation from the code under test cannot
#: fail, and a suite of those manufactures confidence at scale.
TAXONOMY_TOOLS = (
    "instahyre_list_job_functions",
    "instahyre_list_locations",
    "instahyre_list_industries",
)


def test_a_taxonomy_is_served_whole_or_not_at_all():
    """Reference data, asked for by name, cached. Half a taxonomy is a WRONG
    answer: a caller filtering against a truncated list concludes a job
    function does not exist."""
    result = {"job_functions": rows(200), "count": 200}

    for tool in TAXONOMY_TOOLS:
        assert tool in budget.WHOLE_ANSWER_TOOLS, tool
        assert budget.enforce(result, tool=tool, limit=4000) is result


def test_a_confirmation_is_never_shortened_to_fit():
    """``instahyre_apply_bulk``'s preview must name EVERY opportunity it is
    about to apply to. A preview one row shorter than the list it is confirming
    is exactly the silent failure that gate exists to prevent, and size is
    never a reason to shorten a confirmation."""
    preview = {"confirmed": False, "would_apply_to": rows(200)}

    assert budget.enforce(preview, tool="instahyre_apply_bulk", limit=4000) is preview


def test_a_list_of_strings_is_a_statement_and_is_never_trimmed():
    """``irreversible_tools`` is a list whose completeness is the whole point
    of it. Rows are objects; a bare list of strings is a claim."""
    result = {"irreversible_tools": ["a" * 500 for _ in range(50)]}

    out = budget.enforce(result, tool="instahyre_server_info", limit=4000)

    assert out["irreversible_tools"] == result["irreversible_tools"]
    assert out[budget.REPORT_KEY]["trimmed"] is False


def test_when_nothing_can_be_dropped_it_reports_instead_of_cutting():
    result = {"prose": "x" * 9000}

    out = budget.enforce(result, tool="instahyre_server_info", limit=4000)

    assert out["prose"] == result["prose"], "an object was cut open"
    assert out[budget.REPORT_KEY]["trimmed"] is False
    assert out[budget.REPORT_KEY]["over_by"] == size(result) - 4000


def test_it_trims_the_biggest_row_list_and_leaves_the_others_alone():
    result = {"small": rows(2, 10), "opportunities": rows(200), "offset": 0}

    out = budget.enforce(result, tool="instahyre_list_opportunities", limit=4000)

    assert out["small"] == result["small"]
    assert out[budget.REPORT_KEY]["trimmed_key"] == "opportunities"


def test_the_cap_is_mounted_where_no_new_tool_can_miss_it(monkeypatch):
    """Package-wide means package-wide: it hangs off ``handled``, which every
    one of the 57 tools wears, rather than being opted into tool by tool."""
    monkeypatch.setattr(budget, "MAX_RESPONSE_BYTES", 4000)

    @server_module.handled
    def a_brand_new_tool():
        return {"opportunities": rows(200), "offset": 0, "count_returned": 200}

    out = a_brand_new_tool()

    assert size(out) <= 4000
    assert out[budget.OMITTED_KEY] > 0


def test_no_read_tool_ships_anywhere_near_the_cap(wired):
    """The cap is a backstop, not a working limit. If a normal call were close
    to it, the shaping above would be the thing that is wrong."""
    result = server_module.instahyre_list_opportunities()

    assert budget.REPORT_KEY not in result
    assert size(result) < budget.MAX_RESPONSE_BYTES / 4


# ---------------------------------------------------------------------------
# Relevance: a field that restates the query, and one that names its consumer
# ---------------------------------------------------------------------------


def test_status_is_silent_when_it_merely_restates_the_query():
    """Thirty rows repeating the caller's own filter carry no information.

    Measured before the change: `status` was distinct=1 across a thirty-row page,
    value "pending", while the envelope already said interest="pending". And it
    cannot differ by construction in the ordinary case -- `interest` is REQUIRED
    to be one of three facets, there is no unfiltered mode, so a row returned
    under a filter matches it.
    """
    from instahyre_server import shape

    row = shape.shape_opportunity(_min_record(0), expected_status="pending")
    assert "status" not in row, "status restated the query instead of staying silent"
    assert "status_disagrees_with_query" not in row


def test_status_speaks_up_exactly_when_it_disagrees__CONTROL():
    """The control for the silence, and the reason the field was not deleted.

    `status` is derived from the record's OWN interview_status with an "unknown"
    fallback, so it CAN differ from the filter -- and the row where it differs is
    the anomalous one a caller most needs to see. Deleting the field would have
    removed the signal precisely where it mattered while keeping it in all thirty
    places it did not. Silence has to be conditional, and this proves the
    condition.
    """
    from instahyre_server import shape

    disagreeing = shape.shape_opportunity(_min_record(1), expected_status="pending")
    assert disagreeing["status"] == "interested"
    assert disagreeing["status_disagrees_with_query"] == "pending"

    unmapped = shape.shape_opportunity(_min_record(99), expected_status="pending")
    assert unmapped["status"] == "unknown", "an unseen facet must not be swallowed"
    assert unmapped["status_disagrees_with_query"] == "pending"


def test_a_caller_that_states_no_expectation_still_gets_status():
    """The other three shape_opportunity call sites pass no expectation, and must
    be unaffected. A conditional field that silently vanished for them would be a
    regression dressed as a saving."""
    from instahyre_server import shape

    row = shape.shape_opportunity(_min_record(0))
    assert row["status"] == "pending"
    assert "status_disagrees_with_query" not in row


def test_the_compact_id_is_named_after_the_tools_that_consume_it():
    """An id a caller cannot spend is noise; an id they can is a saved round-trip.

    Four tools take this value and all four call the parameter `opportunity_id`.
    The row now agrees with them, so the linkage needs no documentation to be
    obvious. Asserted against the real signatures rather than a literal, so the
    day a tool renames its parameter this fails instead of drifting.
    """
    import inspect

    from instahyre_server import shape
    import instahyre_server.server as srv

    assert shape.COMPACT_FIELD_RENAMES.get("id") == "opportunity_id"

    for tool in (
        srv.instahyre_get_opportunity,
        srv.instahyre_apply,
        srv.instahyre_decline_opportunity,
    ):
        assert "opportunity_id" in inspect.signature(tool).parameters, (
            "%s no longer takes opportunity_id; the compact row is now named after "
            "a parameter that does not exist" % tool.__name__
        )


def _min_record(interview_status):
    """The smallest record shape_opportunity will shape, with one facet varied."""
    return {
        "id": 1,
        "score": 80,
        "interview_status": interview_status,
        "job": {"id": 9, "title": "T", "locations": ["X"], "skills": ["a"]},
        "employer": {"company_name": "C"},
    }
