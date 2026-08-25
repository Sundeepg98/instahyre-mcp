"""The one thing that bounds what a single tool result costs a caller's context.

WHY THIS MODULE EXISTS, AND WHAT WAS ACTUALLY MISSING
-----------------------------------------------------
Before 2026-08-25 nothing in this package bounded the SIZE of a response. That
is easy to miss, because two things that look like the same guarantee were
already in place and are still in place:

  ROW COUNT is bounded.  ``limit`` exists on every list tool and
  ``C.OPP_MAX_LIMIT`` caps it at 1000.

  PER-FIELD CONTENT is bounded.  ``shape.MAX_SKILLS_IN_LIST`` caps keywords at
  eight, ``shape.DEFAULT_DESCRIPTION_CHARS`` caps a description at 1200,
  ``shape.truncate`` caps an employer blurb at 260.

Neither bounds their PRODUCT, and the product is what a caller pays. A
``limit`` of 1000 against the measured 696-byte full-detail queue row is about
696,000 bytes -- roughly 174,000 tokens, more than a whole context window, from
one call that every gate above passed cleanly. A grep for ``MAX_RESPONSE``,
``max_bytes``, ``byte_cap`` or ``MAX_PAYLOAD`` across this package on
2026-08-25 returned nothing at all. This is that grep's answer.

WHAT IT DOES, AND THE ONE PROPERTY THAT MATTERS
-----------------------------------------------
IT DROPS WHOLE ROWS AND NEVER CUTS INSIDE ONE. A truncated JSON object, a
half-written id, a string sheared at byte 40,000 -- those turn an oversized
answer into an unparseable one, which is strictly worse: the caller loses the
rows AND the rows it kept. So the unit of removal is one complete element of
one list, the result always parses, and what went missing is stated in numbers
rather than implied by a short list.

It is mounted in :func:`instahyre_server.server.handled`, which wraps all 57
tools, so a tool added tomorrow is covered without anyone remembering to cover
it. That placement is the whole point of the word "package-wide": a cap that
each tool opts into is a cap the next tool forgets.

WHAT IT DELIBERATELY WILL NOT TOUCH
-----------------------------------
Two carve-outs, both NAMED rather than inferred, because a rule admits members
nobody has read.

  WHOLE-ANSWER TOOLS.  The three taxonomy tools are reference data, asked for
  explicitly by name, cached, and served whole or not at all. Half a taxonomy
  is a WRONG answer rather than a smaller one: a caller who filters against a
  truncated job-function list concludes a function does not exist. They are
  listed by name in :data:`WHOLE_ANSWER_TOOLS`.

  CONFIRMATION-GATED RESULTS.  Any result carrying a top-level ``confirmed``
  key is a write preview or a write receipt. ``instahyre_apply_bulk``'s preview
  must name EVERY opportunity it is about to apply to -- that is a safety
  property with its own control script -- and a preview quietly one row shorter
  than the list it is confirming is exactly the silent failure that gate
  exists to prevent. Size is never a reason to shorten a confirmation.

THE NUMBER
----------
:data:`MAX_RESPONSE_BYTES` is 40,000 bytes, about 10,000 tokens. Derived, not
picked: it is 4.7x the largest legitimate whole-answer payload this server
produces (``instahyre_list_job_functions``, 8,412 bytes measured), and about 57
full-detail queue rows at the measured 696-byte average -- so no shaped result
this server returns today comes near it, while the 696,000-byte call described
above is stopped. Above that it is one third of the ENTIRE 23-tool read
surface (113,100 bytes measured 2026-08-25), and no single call should be able
to spend more than a third of what reading everything costs.

Strict ASCII, like every file in this package.
"""

from __future__ import annotations

import json
from typing import Any, Optional

__all__ = [
    "MAX_RESPONSE_BYTES",
    "WHOLE_ANSWER_TOOLS",
    "CONFIRMATION_KEY",
    "REPORT_KEY",
    "OMITTED_KEY",
    "measure",
    "enforce",
]

#: The cap, in bytes of the JSON a caller receives. See THE NUMBER above.
MAX_RESPONSE_BYTES = 40000

#: Tools whose answer is only correct when it is complete. Named, never matched
#: by prefix or pattern -- see WHAT IT DELIBERATELY WILL NOT TOUCH.
WHOLE_ANSWER_TOOLS = frozenset(
    {
        "instahyre_list_job_functions",
        "instahyre_list_locations",
        "instahyre_list_industries",
    }
)

#: A result carrying this key at top level is a write preview or receipt.
CONFIRMATION_KEY = "confirmed"

#: Where the cap explains itself when it fired.
REPORT_KEY = "response_cap"

#: Mirrored to the top level so a caller cannot miss it. Computed once, at the
#: same moment as the block, so the two cannot disagree.
OMITTED_KEY = "rows_omitted"

#: Keys that count or index THIS payload's rows and would therefore lie after a
#: trim. ``count_returned`` says how many rows are in this result;
#: ``next_offset`` says where the caller resumes. Deliberately NOT here:
#: ``total_matching``, ``queue_size_after_filters``, ``applied_count`` and
#: ``declined_count``, which describe the QUEUE rather than this payload and
#: are still true afterwards -- rewriting them would destroy the only evidence
#: that rows are missing.
_RETURNED_COUNT_KEY = "count_returned"
_OFFSET_KEY = "offset"
_NEXT_OFFSET_KEY = "next_offset"


def measure(value: Any) -> int:
    """Bytes of JSON, counted exactly as a caller would receive them."""
    try:
        return len(json.dumps(value, default=str))
    except Exception:
        # A result that cannot be serialised is not a result this cap can
        # reason about. Say so by measuring zero rather than raising here and
        # turning a size question into a tool failure.
        return 0


def _row_lists(result: dict) -> list:
    """Top-level keys holding a list of dicts, biggest first.

    A list of DICTS, specifically. ``irreversible_tools`` in
    ``instahyre_server_info`` is a list of strings whose completeness is the
    whole point of it, and ``scored_against_skills`` is a list of skill names
    that a score was computed against -- dropping members of either would make
    the result say something false rather than something shorter. Rows are
    objects; a bare list of strings is a statement.
    """
    out = []
    for key, value in result.items():
        if not isinstance(value, list) or not value:
            continue
        if not all(isinstance(item, dict) for item in value):
            continue
        out.append((measure(value), key))
    out.sort(reverse=True)
    return [key for _, key in out]


def _remedy(tool: str, key: str, kept: int, omitted: int, has_offset: bool) -> str:
    """How to get the dropped rows. Names the mechanism, never just 'try again'."""
    if has_offset:
        return (
            "%d row(s) of '%s' were dropped to fit the response cap. They are NOT lost: "
            "next_offset has been moved back to the first dropped row, so calling %s "
            "again with that offset returns them. Narrowing with the tool's own filters, "
            "or asking for detail='compact' where the tool offers it, returns more rows "
            "per call." % (omitted, key, tool)
        )
    return (
        "%d row(s) of '%s' were dropped to fit the response cap, keeping the first %d. "
        "This tool exposes no offset, so re-run %s with a smaller limit or a narrower "
        "filter to see the rest." % (omitted, key, kept, tool)
    )


def enforce(result: Any, *, tool: str, limit: Optional[int] = None) -> Any:
    """Bring one tool result under the cap by dropping whole rows, or explain why not.

    Returns the result unchanged whenever it already fits, is not a dict, is a
    whole-answer tool, or carries a confirmation gate.

    Args:
        result: whatever the tool returned.
        tool: the tool's function name, for the carve-out check and the remedy.
        limit: override the cap. Tests use it; nothing in the server does.
    """
    cap = MAX_RESPONSE_BYTES if limit is None else int(limit)
    if not isinstance(result, dict):
        # Not a shape rows can be dropped from without inventing a rule for it.
        return result
    before = measure(result)
    if before <= cap:
        return result
    if tool in WHOLE_ANSWER_TOOLS:
        return result
    if CONFIRMATION_KEY in result:
        return result

    candidates = _row_lists(result)
    if not candidates:
        # Nothing droppable. The alternative is cutting inside an object, which
        # is the one thing this module exists not to do, so it reports instead.
        out = dict(result)
        out[REPORT_KEY] = {
            "limit_bytes": cap,
            "bytes": before,
            "over_by": before - cap,
            "trimmed": False,
            "why": (
                "This result is over the response cap and holds no list of rows to drop. "
                "Nothing was removed: cutting inside an object would return JSON that "
                "does not parse, which loses the whole answer instead of part of it."
            ),
        }
        return out

    key = candidates[0]
    rows = result[key]
    has_offset = isinstance(result.get(_OFFSET_KEY), int)

    def trial(keep: int) -> dict:
        out = dict(result)
        out[key] = rows[:keep]
        omitted = len(rows) - keep
        if _RETURNED_COUNT_KEY in out:
            out[_RETURNED_COUNT_KEY] = keep
        if has_offset:
            # THE CORRECTION THAT IS NOT OPTIONAL. Before the trim,
            # ``next_offset`` pointed PAST every row this call returned. Leaving
            # it there after dropping rows tells a paging caller to resume
            # beyond rows it never received, and those rows are then gone with
            # no signal anywhere -- a cap that silently loses opportunities is
            # worse than no cap. It is moved back to the first dropped row.
            out[_NEXT_OFFSET_KEY] = result[_OFFSET_KEY] + keep
        elif _NEXT_OFFSET_KEY in out:
            out.pop(_NEXT_OFFSET_KEY)
        out[OMITTED_KEY] = omitted
        out[REPORT_KEY] = {
            "limit_bytes": cap,
            "bytes_before": before,
            "trimmed_key": key,
            "rows_returned": keep,
            OMITTED_KEY: omitted,
            "whole_rows_only": True,
            "how_to_fetch_the_rest": _remedy(tool, key, keep, omitted, has_offset),
        }
        return out

    # Binary search the largest number of whole rows that fits, measuring the
    # candidate WITH its own report block attached -- the block costs bytes too,
    # and a search that ignored them would hand back a result over the cap.
    #
    # NO ``bytes_after`` FIELD, and that is not an omission. Writing the final
    # size into the result changes the final size, so the field could only ever
    # be off by its own width -- a number that is wrong by construction is
    # worse than no number, and ``limit_bytes`` already states the guarantee.
    low, high, best = 0, len(rows) - 1, 0
    while low <= high:
        mid = (low + high) // 2
        if measure(trial(mid)) <= cap:
            best = mid
            low = mid + 1
        else:
            high = mid - 1

    out = trial(best)
    # Zero rows and still over: everything left is one indivisible payload.
    # Reported, never cut -- the same ruling as the no-candidates branch above.
    if measure(out) > cap:
        out[REPORT_KEY]["under_cap"] = False
        out[REPORT_KEY]["why"] = (
            "Every row was dropped and this result is still over the cap. What remains "
            "is not rows, so nothing further was removed: cutting inside an object would "
            "return JSON that does not parse."
        )
    return out
