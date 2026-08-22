"""Which ONE skill, added to his 20, would put him inside the most match sets.

WHY THIS INSTRUMENT AND NOT A "MISSING SKILLS" LIST
---------------------------------------------------
Instahyre is a REVERSE marketplace: employers initiate, and he cannot apply his
way into a queue he is not in. The scarce resource is therefore membership of
the match set, and the profile that decides it is capped at 20 skills --
``C.MAX_SKILLS``, read out of Instahyre's own bundle as
``constant("CANDIDATE_MAX_SKILLS_COUNT",20)`` and enforced client-side with the
message "You cannot add more than 20 skills".

A cap turns the question from "what should I learn" into an ALLOCATION problem
with two halves, and this module answers both against the LIVE queue rather
than against a wishlist:

* ``missing_skills`` -- ranked by how many queued jobs demand a skill he does
  not have. The top row is the highest-leverage single addition.
* ``covered_skills`` / ``dead_weight_skills`` -- his own 20, with the demand
  each one is actually earning. A skill zero queued jobs ask for is buying
  nothing, and under a hard cap that is not a neutral fact: it is the row to
  spend on something from the first list.

NO NETWORK, NO FILE, NO SECOND SCORER
-------------------------------------
It takes ALREADY-FETCHED records and returns a dict. Nothing here opens a
socket or a file, which is what lets the whole thing be tested offline, the way
every test in this repo runs.

Skill comparison goes through ``jobcore`` -- the same engine
:mod:`instahyre_server.scoring` binds, taxonomy extension and all. That is not
a preference. A local comparison would be the fallback scorer this package
deliberately deleted, wearing a different name: raw string equality says a job
asking ``"nodejs"`` is a gap against a profile holding ``"Node.js"``, and one
live record on this account (job 439233) asks for ``"Node.js"``, ``"Node"``
AND ``"NodeJS"`` at once, which un-normalised counts as three votes for one
skill. The gap for one job is exactly ``SkillMatch.compute(...).missing``;
this module's whole job is to aggregate that across the queue and say what the
aggregate does and does not measure.

Policy is INJECTED, never read here, exactly as ``scoring.score_job`` takes it:
pass ``**instahyre_server.policy.scoring_args(snapshot)``. One snapshot bound
at tool entry means the whole ranking is computed under one policy even if
``jobhunt.json`` changes mid-call.

WHAT THE NUMBERS DO NOT SAY
---------------------------
Two undercounts are possible and both are reported rather than absorbed:

1. **Keyword-less jobs.** A job declaring no skills cannot demand one, so it is
   excluded from the denominator (``analysed_jobs``, never ``queue_size``).
   Leaving it in would deflate every share on the page by a quiet fraction.
2. **Shaped records.** ``shape.shape_opportunity`` caps its ``skills`` list at
   8 and counts the overflow in ``skills_more``; 2 of the 6 records in
   ``tests/fixtures/opportunities_pending.json`` exceed that. Counts computed
   off shaped records are LOWER BOUNDS, and a result built from any of them
   says so in ``truncation_warning``. Pass raw queue objects -- the ones still
   carrying ``job.keywords`` -- for exact numbers.

And every zero carries a ``diagnosis``. "He covers everything the queue asks
for", "the queue was empty", "the records carried no keywords at all" and "no
profile skills to measure against" are four different facts that all produce
the same empty ``missing_skills``. The third is a contract change or a shaping
bug and must never print as good news -- ``instahyre_server/errors.py`` states
the rule this file is holding up: a failure must never look like an empty
result.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Optional, Sequence

# LOCAL IMPORT FIRST, and that is not a style slip. ``.scoring`` owns the one
# ImportError that names how to install jobcore; importing it before any
# ``from jobcore import ...`` here means a missing dependency still produces
# that sentence rather than a bare "No module named 'jobcore'".
from . import constants as C
from .scoring import ENGINE, ENGINE_VERSION, engine_for
from jobcore import CandidatePolicy, ScoringPolicy, SkillMatch

__all__ = ["analyse_gap"]

#: How many gap rows a caller gets unless it asks for more. 15 is a readable
#: page and comfortably more than the number of skills that ever clear a
#: single-digit ``appears_in`` on a queue this size.
DEFAULT_TOP_N = 15

#: Named jobs per gap row. Enough to make a count checkable by hand, few enough
#: that a skill demanded by 200 jobs does not dump the queue into the result.
DEFAULT_EXAMPLES = 3


def analyse_gap(
    records: Sequence[dict],
    profile_skills: Iterable[str],
    *,
    top_n: int = DEFAULT_TOP_N,
    examples_per_skill: int = DEFAULT_EXAMPLES,
    policy: Optional[ScoringPolicy] = None,
    candidate: Optional[CandidatePolicy] = None,
    policy_rev: Optional[int] = None,
) -> dict:
    """Aggregate the per-job skill gap across an already-fetched queue.

    Args:
        records: queue records, RAW (carrying ``job.keywords``) or shaped by
            :func:`instahyre_server.shape.shape_opportunity` (carrying
            ``skills``). Raw wins when a record somehow carries both, because
            the shaped list is capped at 8. Nothing is fetched -- hand in the
            whole queue, which fits in one request at
            ``limit=C.OPP_MAX_LIMIT``.
        profile_skills: his own skills, as
            ``Inbound.profile_for_scoring()["skills"]`` returns them.
        top_n: how many gap rows to return. ``missing_skills_total`` always
            reports the full count, so a truncated list never reads as complete.
        examples_per_skill: named jobs per gap row.
        policy: the configured :class:`~jobcore.policy.ScoringPolicy`. Pass
            ``**instahyre_server.policy.scoring_args(snapshot)`` rather than
            building this by hand -- that spread fills ``policy``,
            ``candidate`` and ``policy_rev`` in one go, and a forgotten policy
            then shows up as a missing spread instead of a silently
            default-scored result.
        candidate: his canonical self-description. Accepted so the same spread
            works here as at every other scoring call site; the skill taxonomy
            does not read it today.
        policy_rev: stamped into the result when the policy is not the shipped
            default, so a number produced under a non-default policy says so.

    Returns:
        A dict. ``missing_skills`` and ``covered_skills`` are the two halves of
        the allocation question; ``queue_size`` / ``analysed_jobs`` /
        ``jobs_with_no_keywords`` are what the percentages were computed over;
        ``skill_slots`` says whether adding anything is even possible without a
        swap. Any zero brings a ``diagnosis``.

    Raises:
        ValueError: ``top_n`` or ``examples_per_skill`` below 1. Silently
            treating 0 as "all" would make a caller's typo look like a result.
    """
    top_n = int(top_n)
    examples_per_skill = int(examples_per_skill)
    if top_n < 1:
        raise ValueError("top_n must be at least 1, not %r" % (top_n,))
    if examples_per_skill < 1:
        raise ValueError(
            "examples_per_skill must be at least 1, not %r" % (examples_per_skill,)
        )

    # Materialised once: ``records`` is measured (``queue_size``) and walked,
    # and a caller handing in a generator should not get a queue size of zero.
    queue = list(records)

    engine = engine_for(policy, candidate)
    # ``SkillMatch``'s policy argument feeds only its ``.score`` property today,
    # not ``.missing`` -- which is the half this module reads. Passed anyway:
    # calling the shared value object the way the shared scorer calls it is how
    # this stays one engine rather than two that agree by coincidence.
    skills_policy = policy.skills if policy is not None else None

    # -- his side ----------------------------------------------------------
    profile_surfaces = _surfaces(profile_skills)
    # canonical -> the spellings HE wrote, in the order he wrote them. Two of
    # his rows can normalise together ("Node" and "Node.js"), which costs two
    # of twenty slots and buys one; that is what ``duplicate_slots`` reports.
    his_spellings: dict[str, list[str]] = {}
    for surface in profile_surfaces:
        his_spellings.setdefault(engine.normalize_skill(surface), []).append(surface)
    # Built through the scorer's own entry point rather than from the keys
    # above, so the set compared against every job is literally the set the
    # scorer would compare. ``parse_set`` is ``normalize`` applied elementwise,
    # so the two agree by construction -- this just removes the "by
    # construction" from the load-bearing half.
    profile_set = engine.parse_skills(profile_surfaces)

    # -- the queue ---------------------------------------------------------
    missing_counts: Counter = Counter()
    covered_counts: Counter = Counter()
    # canonical -> how often each SURFACE spelling of it appeared. The
    # canonical is a lookup key ("amazon web services"); nobody writes it, so
    # what gets displayed is the spelling the queue itself uses most.
    surface_counts: dict[str, Counter] = {}
    examples: dict[str, list[dict]] = {}
    analysed = 0
    keywordless = 0
    truncated = 0

    for record in queue:
        surfaces, was_truncated = _record_skills(record)
        if not surfaces:
            # Cannot contribute a demand, so it must not sit in the
            # denominator either. Counted out loud instead of dropped.
            keywordless += 1
            continue
        analysed += 1
        if was_truncated:
            truncated += 1

        job_set = engine.parse_skills(surfaces)
        for surface in surfaces:
            canonical = engine.normalize_skill(surface)
            surface_counts.setdefault(canonical, Counter())[surface] += 1

        match = SkillMatch.compute(job_set, profile_set, skills_policy)
        for canonical in match.missing:
            missing_counts[canonical] += 1
            bucket = examples.setdefault(canonical, [])
            if len(bucket) < examples_per_skill:
                bucket.append(_identity(record))
        for canonical in match.matched:
            covered_counts[canonical] += 1

    # -- assembly ----------------------------------------------------------
    # Ranked by demand, ties broken on the canonical name. A ranking that
    # reshuffles between two identical runs cannot be quoted at anyone.
    ranked = sorted(missing_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    missing_rows = [
        {
            "skill": _display_spelling(surface_counts.get(canonical)) or canonical,
            "canonical": canonical,
            "appears_in": count,
            "share_of_queue": _share(count, analysed),
            "example_jobs": examples.get(canonical, []),
        }
        for canonical, count in ranked[:top_n]
    ]

    covered_rows: list[dict] = []
    dead_rows: list[dict] = []
    for canonical, spellings in his_spellings.items():
        demand = covered_counts.get(canonical, 0)
        if demand:
            covered_rows.append(
                {
                    "skill": spellings[0],
                    "canonical": canonical,
                    "appears_in": demand,
                    "share_of_queue": _share(demand, analysed),
                }
            )
        else:
            dead_rows.append(
                {"skill": spellings[0], "canonical": canonical, "appears_in": 0}
            )
    covered_rows.sort(key=lambda row: (-row["appears_in"], row["canonical"]))
    dead_rows.sort(key=lambda row: row["canonical"])

    duplicate_slots = sorted(
        (
            {
                "canonical": canonical,
                "spellings": sorted(spellings),
                "wasted_slots": len(spellings) - 1,
            }
            for canonical, spellings in his_spellings.items()
            if len(spellings) > 1
        ),
        key=lambda row: row["canonical"],
    )

    used = len(profile_surfaces)
    result: dict[str, Any] = {
        "queue_size": len(queue),
        "analysed_jobs": analysed,
        "jobs_with_no_keywords": keywordless,
        "skills_truncated_jobs": truncated,
        # The cap is a PLATFORM constraint, not a preference, so the result
        # says up front whether "add this skill" is even an available move.
        "skill_slots": {
            "used": used,
            "cap": C.MAX_SKILLS,
            "free": max(0, C.MAX_SKILLS - used),
        },
        "missing_skills": missing_rows,
        "missing_skills_total": len(missing_counts),
        "covered_skills": covered_rows,
        "dead_weight_skills": dead_rows,
        "duplicate_slots": duplicate_slots,
        "denominator": (
            "share_of_queue is a percentage of analysed_jobs -- the records that "
            "declared at least one skill -- never of queue_size."
        ),
        "engine": ENGINE,
        "engine_version": ENGINE_VERSION,
    }
    if policy_rev is not None:
        result["policy_rev"] = policy_rev
    if truncated:
        result["truncation_warning"] = (
            "%d of the analysed records arrived already shaped, with the skill list "
            "capped at 8 (shape_opportunity keeps the overflow in skills_more). Every "
            "count and share here is therefore a LOWER BOUND. Pass raw queue objects, "
            "the ones still carrying job.keywords, for exact numbers." % truncated
        )

    # Only present when there is a zero to explain. A field that is always
    # there and usually null trains a reader to stop looking at it.
    diagnosis = _diagnose(
        queue_size=len(queue),
        analysed=analysed,
        profile_count=used,
        missing_total=len(missing_counts),
    )
    if diagnosis is not None:
        result["diagnosis_reason"], result["diagnosis"] = diagnosis
    return result


# ---------------------------------------------------------------------------
# Reading one record
# ---------------------------------------------------------------------------


def _record_skills(record: dict) -> tuple[list[str], bool]:
    """``(surface skills, was the list truncated)`` for one record.

    RAW WINS. A raw queue object carries the complete ``job.keywords``; a
    shaped one carries ``skills`` capped at 8 with the remainder counted in
    ``skills_more``. A record holding both is read from the complete half, and
    only the shaped path can report truncation because only it can suffer it.
    """
    job = record.get("job")
    if isinstance(job, dict) and "keywords" in job:
        return _surfaces(job.get("keywords")), False
    return _surfaces(record.get("skills")), bool(record.get("skills_more"))


def _identity(record: dict) -> dict:
    """The ``{job_id, title, company}`` triple, from either record shape.

    ``job.id`` is the ordinary job id, NOT the opportunity id an apply needs --
    same distinction ``shape_opportunity`` is careful about. This is a gap
    report, so it names the job.
    """
    job = record.get("job")
    if isinstance(job, dict):
        employer = record.get("employer") or {}
        return {
            "job_id": job.get("id"),
            "title": job.get("title") or job.get("candidate_title"),
            "company": employer.get("company_name") or job.get("hiring_company_name"),
        }
    return {
        "job_id": record.get("job_id"),
        "title": record.get("title"),
        "company": record.get("company"),
    }


def _surfaces(value: Any) -> list[str]:
    """Skill strings as WRITTEN, before any normalisation.

    Mirrors ``shape._as_list``'s contract for the two shapes this API really
    uses -- a list, or the comma-joined string it sometimes sends instead --
    with two deliberate differences. A set is sorted first, because an
    unordered input must not make the output reorder between runs. And a scalar
    of some other type becomes NOTHING rather than a one-element list: a
    keywords field holding an int is a contract change, and inventing a skill
    named "42" out of it would launder that into a data point.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (set, frozenset)):
        value = sorted(value)
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def _display_spelling(counter: Optional[Counter]) -> Optional[str]:
    """The most common surface spelling, ties broken alphabetically."""
    if not counter:
        return None
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _share(count: int, denominator: int) -> float:
    """Percentage of the jobs that could have demanded it. Never of the queue."""
    if denominator <= 0:
        return 0.0
    return round(count / denominator * 100, 1)


# ---------------------------------------------------------------------------
# The empty state
# ---------------------------------------------------------------------------


def _diagnose(
    *, queue_size: int, analysed: int, profile_count: int, missing_total: int
) -> Optional[tuple[str, str]]:
    """Say WHY something came back zero. Never shrug.

    Four genuinely different situations produce an identical empty
    ``missing_skills`` (or an empty ``covered_skills``), and one of them is a
    bug wearing good news. Ordered so the most upstream fact wins: no records
    at all, then records with nothing to read, then nothing to read them
    against, then a real result of zero.
    """
    if queue_size == 0:
        return (
            "queue_empty",
            "No opportunities were handed in, so there is no demand to measure "
            "against. That is a fact about the queue, not about his skills -- "
            "instahyre_list_opportunities reports why the queue itself is bare, "
            "and it separates 'no employer has matched yet' from 'the filters "
            "emptied this view'.",
        )
    if analysed == 0:
        return (
            "no_keywords",
            "%d records came in and not one of them declared a single skill, so "
            "nothing could be counted. Every queue record ever observed on this "
            "account carries job.keywords; %d carrying none is a contract change "
            "on that field or a bug in shaping, NOT a profile that already "
            "matches everything. Treat this ranking as unmeasured and look at a "
            "raw record before acting on it." % (queue_size, queue_size),
        )
    if profile_count == 0:
        return (
            "no_profile_skills",
            "The queue declares skills but the profile handed in has none, so "
            "every skill in the queue counts as missing and nothing counts as "
            "covered. profile_for_scoring() returns an empty skill list when the "
            "session has lapsed just as it does when the profile is genuinely "
            "bare -- check instahyre_auth_status before reading this as a "
            "measurement of his profile.",
        )
    if missing_total == 0:
        return (
            "full_coverage",
            "Nothing is missing: his skills cover every skill the %d analysed "
            "jobs ask for. Under the %d-slot cap that means the lever is no "
            "longer adding a skill -- read dead_weight_skills for the rows "
            "currently buying no demand at all."
            % (analysed, C.MAX_SKILLS),
        )
    return None
