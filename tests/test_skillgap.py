"""The skill-gap instrument: which ONE skill would widen his inbound queue most.

WHY THIS FILE IS WORTH ITS LENGTH
---------------------------------
Instahyre is a reverse marketplace. He does not apply his way in -- employers
initiate, and the only lever he holds is whether his profile lands inside a
match set. The platform caps that profile at 20 skills (``C.MAX_SKILLS``, read
out of their own bundle as ``CANDIDATE_MAX_SKILLS_COUNT``), so the question is
not "what could I learn" but "which skill, added to my 20, buys the most match
sets I am currently only partially matching" -- and, because the cap binds,
"which of my 20 is currently buying nothing".

That makes this a MEASUREMENT, not a recommendation engine, and every number it
prints has to survive being read as one. Three ways it could lie quietly, each
pinned below:

1. **Aliasing.** A job asking ``"nodejs"`` against a profile holding
   ``"Node.js"`` is NOT a gap. Raw string equality says it is. Every comparison
   here goes through jobcore's taxonomy, and ``test_aliasing_*`` holds it --
   with a CONTROL that shows raw equality really would have reported the gap,
   so the assertion is not vacuously true.
2. **The denominator.** A job that declares no skills cannot demand one. Count
   it in the denominator and every percentage is silently deflated. The
   denominator is ``analysed_jobs``, never ``queue_size``.
3. **The empty state.** "No gap found" and "no job in the queue declared any
   skill" are opposite facts that produce the same empty list. The second is a
   contract change or a shaping bug and must never print as good news. This
   package's governing rule is written in ``instahyre_server/errors.py``: a
   failure must never look like an empty result.

FIXTURES ARE BUILT FROM THE REAL RECORD SHAPE, offline, in this file. The keys
are transcribed from ``tests/fixtures/opportunities_pending.json`` -- the queue
object carries ``job.keywords`` and ``employer.company_name``; after
``shape.shape_opportunity`` the same list surfaces as ``skills``, capped at 8
with the overflow counted in ``skills_more``. Both shapes are exercised,
because the truncation is real: 2 of the 6 records in that live fixture carry
more than 8 keywords.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from instahyre_server import constants as C
from instahyre_server import shape, skillgap
from instahyre_server.skillgap import analyse_gap

MODULE_PATH = Path(skillgap.__file__)


# ---------------------------------------------------------------------------
# Record builders -- the live shapes, nothing invented
# ---------------------------------------------------------------------------


def opp(job_id: int, title: str, company: str, keywords: list, *, score: float = 1.0) -> dict:
    """One raw curated-queue object, keyed as the live API keys it."""
    return {
        "id": str(6_000_000_000 + job_id),
        "score": score,
        "interview_status": 0,
        "is_active": True,
        "employer": {"id": 900 + job_id, "company_name": company},
        "job": {
            "id": job_id,
            "title": title,
            "keywords": list(keywords),
            "locations": ["Bangalore"],
            "hiring_company_name": company,
            "opportunity_url": "/opportunity/%d" % job_id,
        },
    }


def shaped(*records: dict) -> list:
    """The same records after the shaping every tool result goes through."""
    return [shape.shape_opportunity(r) for r in records]


# A queue small enough to do the arithmetic by hand in the assertions.
#   java        -> jobs 1, 2, 3        (3 of the 3 that declare skills)
#   node.js     -> job  1              (his)
#   typescript  -> job  1              (his)
#   kubernetes  -> job  2
#   amazon web services -> job 2       (his)
#   apache kafka -> job 3
#   python      -> job 3
#   job 4 declares nothing at all.
QUEUE = [
    opp(1, "Backend Engineer", "Acme", ["Node.js", "TypeScript", "Java"]),
    opp(2, "Platform Engineer", "Borg", ["Java", "Kubernetes", "AWS"]),
    opp(3, "Data Engineer", "Cyan", ["Java", "Kafka", "Python"]),
    opp(4, "Frontend Engineer", "Delta", []),
]

# Four of his real ones. "React.js" is demanded by nothing in QUEUE, so it is
# this fixture's dead weight.
PROFILE = ["Node.js", "TypeScript", "AWS", "React.js"]


def by_skill(rows: list) -> dict:
    """``[{"skill": "Java", ...}]`` -> ``{"Java": {...}}``, for readable asserts."""
    return {row["skill"]: row for row in rows}


def canonicals(rows: list) -> list:
    return [row["canonical"] for row in rows]


# ---------------------------------------------------------------------------
# 1 -- ranking: the actual question the tool answers
# ---------------------------------------------------------------------------


def test_top_missing_skill_is_the_one_most_of_the_queue_demands():
    out = analyse_gap(QUEUE, PROFILE)
    assert canonicals(out["missing_skills"])[0] == "java"
    assert by_skill(out["missing_skills"])["Java"]["appears_in"] == 3


def test_missing_skills_ranked_by_demand_descending():
    out = analyse_gap(QUEUE, PROFILE)
    counts = [row["appears_in"] for row in out["missing_skills"]]
    assert counts == sorted(counts, reverse=True)


def test_ties_break_on_canonical_name_so_the_order_is_reproducible():
    """A ranking that reshuffles between two identical runs cannot be cited."""
    out = analyse_gap(QUEUE, PROFILE)
    tied = [row["canonical"] for row in out["missing_skills"] if row["appears_in"] == 1]
    assert tied == sorted(tied)
    assert analyse_gap(QUEUE, PROFILE)["missing_skills"] == out["missing_skills"]


def test_a_skill_he_already_has_is_never_in_the_gap():
    out = analyse_gap(QUEUE, PROFILE)
    assert "node.js" not in canonicals(out["missing_skills"])
    assert "typescript" not in canonicals(out["missing_skills"])


def test_top_n_honoured():
    out = analyse_gap(QUEUE, PROFILE, top_n=2)
    assert len(out["missing_skills"]) == 2
    assert canonicals(out["missing_skills"])[0] == "java"


def test_top_n_truncation_reports_the_full_count_it_hid():
    """A truncated list that does not say it is truncated reads as complete."""
    full = analyse_gap(QUEUE, PROFILE)
    short = analyse_gap(QUEUE, PROFILE, top_n=2)
    assert short["missing_skills_total"] == full["missing_skills_total"]
    assert short["missing_skills_total"] > len(short["missing_skills"])


def test_top_n_zero_or_negative_is_refused_not_silently_treated_as_all():
    with pytest.raises(ValueError):
        analyse_gap(QUEUE, PROFILE, top_n=0)


# ---------------------------------------------------------------------------
# 2 -- aliasing. LOAD-BEARING: this is control #1.
# ---------------------------------------------------------------------------


def test_aliasing_a_job_asking_nodejs_against_a_profile_holding_node_dot_js_is_not_a_gap():
    queue = [opp(11, "Integration Developer", "Acme", ["nodejs", "Java"])]
    out = analyse_gap(queue, ["Node.js"])
    assert "node.js" not in canonicals(out["missing_skills"])
    assert "node.js" in canonicals(out["covered_skills"])


def test_aliasing__CONTROL__raw_equality_would_have_reported_that_gap():
    """The assertion above is only meaningful if the spellings really differ.

    No production code is involved here. It exists so a future reader can see
    the test above is not passing because the two strings were equal all along
    -- the failure mode that made six checks in this repo's family certify
    nothing.
    """
    assert "nodejs" != "Node.js"
    assert "nodejs" not in {"Node.js"}


def test_aliasing_holds_in_the_other_direction_too():
    """His spelling is the odd one this time; the job's is the common one."""
    queue = [opp(12, "Backend Engineer", "Acme", ["Node.js"])]
    out = analyse_gap(queue, ["NodeJS"])
    assert out["missing_skills"] == []
    assert by_skill(out["covered_skills"])["NodeJS"]["appears_in"] == 1


def test_aliasing_collapses_three_spellings_inside_one_job_to_one_demand():
    """Live record 439233 asks for 'Node.js', 'Node' AND 'NodeJS' in one list.

    Counted naively that one job votes three times for one skill. Measured from
    ``opportunities_pending.json``; the queue really does this.
    """
    queue = [
        opp(13, "System Engineer", "Acme", ["Node.js", "Node", "NodeJS", "jQuery"]),
        opp(14, "Backend Engineer", "Borg", ["jQuery"]),
    ]
    out = analyse_gap(queue, ["Python"])
    rows = {row["canonical"]: row for row in out["missing_skills"]}
    assert rows["node.js"]["appears_in"] == 1
    assert rows["jquery"]["appears_in"] == 2
    assert canonicals(out["missing_skills"])[0] == "jquery"


def test_the_displayed_spelling_is_a_real_surface_form_not_the_canonical_key():
    """'amazon web services' is the canonical for AWS. Nobody writes that."""
    queue = [opp(15, "Platform Engineer", "Acme", ["AWS"])]
    out = analyse_gap(queue, ["Python"])
    row = out["missing_skills"][0]
    assert row["skill"] == "AWS"
    assert row["canonical"] == "amazon web services"


def test_the_displayed_spelling_is_the_most_common_one_in_the_queue():
    queue = [
        opp(16, "A", "Acme", ["AWS"]),
        opp(17, "B", "Borg", ["AWS"]),
        opp(18, "C", "Cyan", ["Amazon Web Services"]),
    ]
    out = analyse_gap(queue, ["Python"])
    assert out["missing_skills"][0]["skill"] == "AWS"
    assert out["missing_skills"][0]["appears_in"] == 3


# ---------------------------------------------------------------------------
# 3 -- the denominator
# ---------------------------------------------------------------------------


def test_share_of_queue_excludes_jobs_that_declared_no_skills():
    """QUEUE has 4 records, 3 with keywords, and java is in all 3.

    100.0, not 75.0. A keyword-less job cannot demand anything, so counting it
    in the denominator would deflate every share on the page.
    """
    out = analyse_gap(QUEUE, PROFILE)
    assert out["queue_size"] == 4
    assert out["analysed_jobs"] == 3
    assert out["jobs_with_no_keywords"] == 1
    assert by_skill(out["missing_skills"])["Java"]["share_of_queue"] == 100.0


def test_share_of_queue_arithmetic_on_a_partial_demand():
    out = analyse_gap(QUEUE, PROFILE)
    # kubernetes: 1 of the 3 analysed jobs -> 33.3
    assert by_skill(out["missing_skills"])["Kubernetes"]["share_of_queue"] == 33.3


def test_covered_skills_share_uses_the_same_denominator():
    out = analyse_gap(QUEUE, PROFILE)
    # node.js: 1 of 3 analysed -> 33.3, the same denominator as the gap side.
    assert by_skill(out["covered_skills"])["Node.js"]["share_of_queue"] == 33.3


def test_a_job_whose_keywords_are_all_blank_strings_counts_as_keywordless():
    queue = [opp(19, "A", "Acme", ["", "   "]), opp(20, "B", "Borg", ["Java"])]
    out = analyse_gap(queue, ["Python"])
    assert out["jobs_with_no_keywords"] == 1
    assert out["analysed_jobs"] == 1
    assert out["missing_skills"][0]["share_of_queue"] == 100.0


# ---------------------------------------------------------------------------
# 4 -- the other half: which of the 20 slots is dead weight
# ---------------------------------------------------------------------------


def test_covered_and_dead_weight_partition_his_profile():
    out = analyse_gap(QUEUE, PROFILE)
    seen = [row["skill"] for row in out["covered_skills"]]
    seen += [row["skill"] for row in out["dead_weight_skills"]]
    assert sorted(seen) == sorted(PROFILE)


def test_dead_weight_is_the_skill_nothing_in_the_queue_asks_for():
    out = analyse_gap(QUEUE, PROFILE)
    assert [row["skill"] for row in out["dead_weight_skills"]] == ["React.js"]
    assert out["dead_weight_skills"][0]["appears_in"] == 0


def test_dead_weight_is_empty_when_every_slot_earns_its_place():
    queue = [opp(21, "A", "Acme", ["Node.js", "TypeScript"])]
    out = analyse_gap(queue, ["Node.js", "TypeScript"])
    assert out["dead_weight_skills"] == []


def test_covered_skills_ranked_by_demand():
    queue = [
        opp(22, "A", "Acme", ["Node.js", "AWS"]),
        opp(23, "B", "Borg", ["AWS"]),
    ]
    out = analyse_gap(queue, ["Node.js", "AWS"])
    assert [row["skill"] for row in out["covered_skills"]] == ["AWS", "Node.js"]


def test_two_of_his_skills_that_normalise_together_are_flagged_as_one_wasted_slot():
    """Under a hard cap of 20, 'Node' and 'Node.js' cost two rows and buy one."""
    out = analyse_gap(QUEUE, ["Node.js", "Node", "AWS"])
    assert out["duplicate_slots"] == [
        {"canonical": "node.js", "spellings": ["Node", "Node.js"], "wasted_slots": 1}
    ]


def test_no_duplicate_slots_when_every_spelling_is_distinct():
    assert analyse_gap(QUEUE, PROFILE)["duplicate_slots"] == []


# ---------------------------------------------------------------------------
# 5 -- the cap. Adding a skill may not even be possible.
# ---------------------------------------------------------------------------


def test_skill_slots_report_room_against_the_platform_cap():
    out = analyse_gap(QUEUE, PROFILE)
    assert out["skill_slots"] == {"used": 4, "cap": C.MAX_SKILLS, "free": 16}


def test_skill_slots_at_the_cap_report_zero_free():
    full = ["skill%02d" % i for i in range(C.MAX_SKILLS)]
    out = analyse_gap(QUEUE, full)
    assert out["skill_slots"]["used"] == C.MAX_SKILLS
    assert out["skill_slots"]["free"] == 0


def test_a_profile_over_the_cap_never_reports_negative_free_room():
    over = ["skill%02d" % i for i in range(C.MAX_SKILLS + 5)]
    out = analyse_gap(QUEUE, over)
    assert out["skill_slots"]["free"] == 0
    assert out["skill_slots"]["used"] == C.MAX_SKILLS + 5


# ---------------------------------------------------------------------------
# 6 -- the empty state. LOAD-BEARING: the keywords branch is control #2.
# ---------------------------------------------------------------------------


def test_diagnosis_queue_was_empty():
    out = analyse_gap([], PROFILE)
    assert out["queue_size"] == 0
    assert out["diagnosis_reason"] == "queue_empty"
    assert isinstance(out["diagnosis"], str) and out["diagnosis"]


def test_diagnosis_records_existed_but_none_carried_keywords():
    """The branch that must never read as 'no gap'.

    Records present, zero keywords anywhere. That is a contract change on
    ``job.keywords`` or a shaping bug, and it produces exactly the same empty
    ``missing_skills`` as perfect coverage does.
    """
    queue = [opp(31, "A", "Acme", []), opp(32, "B", "Borg", [])]
    out = analyse_gap(queue, PROFILE)
    assert out["queue_size"] == 2
    assert out["analysed_jobs"] == 0
    assert out["diagnosis_reason"] == "no_keywords"
    text = out["diagnosis"].lower()
    assert "keyword" in text
    assert "no gap" not in text


def test_diagnosis_genuinely_no_gap():
    queue = [opp(33, "A", "Acme", ["Node.js", "TypeScript"])]
    out = analyse_gap(queue, PROFILE)
    assert out["missing_skills"] == []
    assert out["diagnosis_reason"] == "full_coverage"
    assert "cover" in out["diagnosis"].lower()


def test_the_three_empty_readings_are_three_different_readings():
    """Three distinct facts. One shrug for all three would be the bug.

    Asserting distinct REASONS, not merely distinct sentences. Measured while
    the ``no_keywords`` branch was deliberately removed: the two collapsed
    onto ``full_coverage`` and still produced different strings, because that
    sentence interpolates ``analysed_jobs`` and the counts differed. A check
    that a defect walks straight through is not a check.
    """
    empty = analyse_gap([], PROFILE)
    bare = analyse_gap([opp(34, "A", "Acme", [])], PROFILE)
    covered = analyse_gap([opp(35, "A", "Acme", ["Node.js"])], PROFILE)
    assert len({r["diagnosis_reason"] for r in (empty, bare, covered)}) == 3
    assert len({r["diagnosis"] for r in (empty, bare, covered)}) == 3


def test_diagnosis_no_profile_skills_to_measure_against():
    """A fourth zero: every job skill is 'missing' and nothing is covered.

    Arithmetically true and useless. ``profile_for_scoring`` returns
    ``{"skills": []}`` when the session is gone, so this is reachable in
    production, and it must not read as "every slot is dead weight".
    """
    out = analyse_gap(QUEUE, [])
    assert out["covered_skills"] == []
    assert out["dead_weight_skills"] == []
    assert out["diagnosis_reason"] == "no_profile_skills"


def test_no_diagnosis_when_there_is_something_to_report():
    """A quiet field that is always present trains a reader to ignore it."""
    assert "diagnosis" not in analyse_gap(QUEUE, PROFILE)
    assert "diagnosis_reason" not in analyse_gap(QUEUE, PROFILE)


# ---------------------------------------------------------------------------
# 7 -- example jobs
# ---------------------------------------------------------------------------


def test_example_jobs_name_the_jobs_that_demand_the_skill():
    out = analyse_gap(QUEUE, PROFILE)
    examples = by_skill(out["missing_skills"])["Java"]["example_jobs"]
    assert [e["job_id"] for e in examples] == [1, 2, 3]
    assert examples[0] == {"job_id": 1, "title": "Backend Engineer", "company": "Acme"}


def test_example_jobs_are_capped_so_a_wide_demand_does_not_dump_the_queue():
    queue = [opp(40 + i, "Role %d" % i, "Co %d" % i, ["Java"]) for i in range(9)]
    out = analyse_gap(queue, ["Python"])
    row = out["missing_skills"][0]
    assert row["appears_in"] == 9
    assert len(row["example_jobs"]) == 3


def test_example_jobs_cap_is_tunable():
    queue = [opp(50 + i, "Role %d" % i, "Co %d" % i, ["Java"]) for i in range(5)]
    out = analyse_gap(queue, ["Python"], examples_per_skill=2)
    assert len(out["missing_skills"][0]["example_jobs"]) == 2


# ---------------------------------------------------------------------------
# 8 -- both record shapes, and the truncation that lives in one of them
# ---------------------------------------------------------------------------


def test_shaped_records_are_accepted_not_just_raw_queue_objects():
    out = analyse_gap(shaped(*QUEUE), PROFILE)
    assert by_skill(out["missing_skills"])["Java"]["appears_in"] == 3
    assert out["jobs_with_no_keywords"] == 1


def test_shaped_and_raw_records_produce_the_same_ranking_when_nothing_is_truncated():
    assert (
        analyse_gap(shaped(*QUEUE), PROFILE)["missing_skills"]
        == analyse_gap(QUEUE, PROFILE)["missing_skills"]
    )


def test_shaped_records_over_eight_keywords_are_reported_as_truncated():
    """``shape_opportunity`` caps ``skills`` at 8 and counts the rest.

    2 of the 6 records in ``opportunities_pending.json`` exceed that, so counts
    computed off shaped records are LOWER BOUNDS and the result has to say so
    rather than print them as measurements.
    """
    wide = opp(60, "Engineering Team Lead", "Acme", [
        "React.js", "TypeScript", "Python", "Algorithms", "Architecture",
        "Data Structures", "JavaScript", "LangChain", "NestJS", "Temporal",
    ])
    out = analyse_gap(shaped(wide), PROFILE)
    assert out["skills_truncated_jobs"] == 1
    assert "truncation_warning" in out
    assert "lower bound" in out["truncation_warning"].lower()


def test_raw_records_are_never_truncated_and_say_nothing_about_it():
    wide = opp(61, "Engineering Team Lead", "Acme", [
        "React.js", "TypeScript", "Python", "Algorithms", "Architecture",
        "Data Structures", "JavaScript", "LangChain", "NestJS", "Temporal",
    ])
    out = analyse_gap([wide], PROFILE)
    assert out["skills_truncated_jobs"] == 0
    assert "truncation_warning" not in out
    # 10 keywords, two of which (React.js, TypeScript) are his.
    assert len(out["missing_skills"]) == 8


def test_raw_keywords_win_over_a_shaped_skills_list_on_the_same_record():
    """A record carrying both is read from the complete half, not the capped one."""
    record = opp(62, "A", "Acme", ["Java", "Kafka", "Kubernetes"])
    record["skills"] = ["Java"]
    record["skills_more"] = 2
    out = analyse_gap([record], ["Python"])
    assert len(out["missing_skills"]) == 3


def test_keywords_given_as_a_comma_joined_string_are_split():
    """``locations`` arrives both ways on this API; keywords are read the same."""
    record = opp(63, "A", "Acme", [])
    record["job"]["keywords"] = "Java, Kafka"
    out = analyse_gap([record], ["Python"])
    assert sorted(canonicals(out["missing_skills"])) == ["apache kafka", "java"]


def test_a_record_with_no_job_object_is_keywordless_not_a_crash():
    out = analyse_gap([{"id": "1"}, opp(64, "A", "Acme", ["Java"])], ["Python"])
    assert out["jobs_with_no_keywords"] == 1
    assert out["analysed_jobs"] == 1


# ---------------------------------------------------------------------------
# 9 -- policy is injected, and it reaches the taxonomy
# ---------------------------------------------------------------------------


def taught_policy(alias: str):
    """A ScoringPolicy teaching the taxonomy that *alias* means Node.js."""
    from jobcore import ScoringPolicy
    from jobcore.policy import FrozenMap, SkillsPolicy

    return ScoringPolicy(skills=SkillsPolicy(extra_skills=FrozenMap({"node.js": (alias,)})))


def test_extra_skills_from_the_injected_policy_close_a_gap():
    """A vocabulary addition in ``jobhunt.json`` has to reach the TAXONOMY.

    The same requirement ``scoring.engine_for`` carries: a skill he taught the
    system must stop the job that asks for it from reading as a gap. If this
    ever fails while ``instahyre_rank_jobs`` still honours the same config, the
    gap report and the fit score are running on two different vocabularies.
    """
    queue = [opp(70, "A", "Acme", ["sundeepscript"])]
    plain = analyse_gap(queue, ["Node.js"])
    assert canonicals(plain["missing_skills"]) == ["sundeepscript"]

    out = analyse_gap(queue, ["Node.js"], policy=taught_policy("sundeepscript"))
    assert out["missing_skills"] == []


def test_extra_skills_aliases_must_be_written_lowercase_to_resolve_at_all():
    """A jobcore gotcha, pinned here because it fails SILENTLY.

    ``SkillTaxonomy.extended()`` inserts each alias into its lookup VERBATIM,
    while ``normalize()`` lowercases every input before looking it up. So an
    ``extra_skills`` alias carrying any capital letter is never reachable: the
    config edit lands, jobcore raises nothing, and the skill simply keeps
    reading as a gap.

    Measured 2026-08-22 against jobcore 0.2.0. This test does not assert the
    behaviour is RIGHT -- it lives in a different repository -- only that the
    empty result above is the alias table's doing and not this module's.
    """
    queue = [opp(71, "A", "Acme", ["Sundeepscript"])]
    lower = analyse_gap(queue, ["Node.js"], policy=taught_policy("sundeepscript"))
    upper = analyse_gap(queue, ["Node.js"], policy=taught_policy("Sundeepscript"))
    assert lower["missing_skills"] == []
    assert canonicals(upper["missing_skills"]) == ["sundeepscript"]


def test_the_policy_revision_is_stamped_when_it_is_not_the_shipped_default():
    out = analyse_gap(QUEUE, PROFILE, policy_rev=7)
    assert out["policy_rev"] == 7
    assert "policy_rev" not in analyse_gap(QUEUE, PROFILE)


def test_the_result_names_the_engine_that_produced_it():
    out = analyse_gap(QUEUE, PROFILE)
    assert out["engine"] == "jobcore"
    assert out["engine_version"]


# ---------------------------------------------------------------------------
# 10 -- the module's own constraints
# ---------------------------------------------------------------------------


def test_the_input_records_are_not_mutated():
    """DEEP copy on purpose: a shallow one shares the nested ``job`` dict, so
    it would compare equal even if the keywords list had been edited in place.
    """
    before = copy.deepcopy(QUEUE)
    analyse_gap(QUEUE, PROFILE)
    assert QUEUE == before


def test_the_module_opens_no_socket_and_imports_no_http_client():
    """It takes already-fetched records. That is what makes it unit-testable."""
    source = MODULE_PATH.read_text(encoding="utf-8")
    for banned in ("import httpx", "from .http", "from .client", "import requests"):
        assert banned not in source, "skillgap.py must not reach the network: %s" % banned


def test_the_module_reads_no_file():
    """Policy is injected. A module that opened a config would be a second one."""
    source = MODULE_PATH.read_text(encoding="utf-8")
    for banned in ("open(", "read_text(", "json.load"):
        assert banned not in source, "skillgap.py must not read a file: %s" % banned


def test_the_module_source_is_strict_ascii():
    raw = MODULE_PATH.read_bytes()
    assert all(b < 128 for b in raw), "non-ASCII byte in skillgap.py"
