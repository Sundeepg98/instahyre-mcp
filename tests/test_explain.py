"""``explain=True``: the working behind a score, and the two hashes that say what it is.

WHAT WAS BROKEN
---------------
``jobcore``'s scorer has always been able to hand back the arithmetic it used
-- ``compute_fit_score(..., explain=True)`` returns the weights, the base
skills/experience split, every bonus and the cap, the verdict band, and the
hash of the arithmetic. No tool on this server could ask for it. A fit score
arrived as a bare integer with no way to find out how it was reached, so "why
is this 56" was answerable only by reading jobcore's source and re-deriving it
by hand.

THE SECOND HALF: THE RENAME
---------------------------
A result's stamp is ``scoring_hash``, not ``policy_hash``, and the distinction
is the reason the rename happened. ``Policy.policy_hash`` covers ``scoring``
AND ``candidate``; the stamp covers ``scoring`` alone. Both were called
``policy_hash`` until 2026-08-21, so one identical policy produced two
different values under one field name -- on the single field whose entire job
is to answer "are these two scores comparable". Comparing a stored score's
stamp against a config readout's fingerprint reported a difference that did not
exist.

WHAT THESE TESTS HOLD
---------------------
1. ``explain`` is OFF by default and adds NOTHING when off -- no key, not
   ``None``, not ``{}``, at any depth of either tool's response.
2. When on, the block lands on each scored ROW, and its arithmetic actually
   REPRODUCES the score: ``base.combined + bonuses.total``, rounded, capped at
   100, IS ``overall_score``. A block that only echoed constants would pass a
   key-presence check and fail this one.
3. The block carries ``scoring_hash`` and never ``policy_hash``.
4. ``instahyre_inbound_digest(rank_against_my_profile=False, explain=True)``
   produces no block and does not raise -- with no jobcore score on the rows
   there is nothing to explain.
5. The bridge the rename exists to build: a config readout prints BOTH hashes,
   they differ under a policy whose candidate and scoring are both non-default,
   and the readout's ``scoring_hash`` EQUALS the stamp on a result scored under
   that same policy.

The claim in (5) is proved NON-TAUTOLOGICALLY. Two configs that share their
``scoring`` block but differ in ``candidate`` are shown to produce the same
``scoring_hash`` and DIFFERENT ``policy_hash`` values -- which is precisely the
false difference the old single name reported -- and a third config that
changes a weight is shown to move ``scoring_hash``, so the equality assertion
is capable of failing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import fixture_json, make_client

from instahyre_server import constants as C
from instahyre_server import policy, scoring
from instahyre_server import server as server_module

SEARCH = C.EP_JOB_SEARCH

#: The candidate id every profile fixture belongs to, as in test_inbound.
CANDIDATE_ID = 9999999


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_config(path: Path, document: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def point_at(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Aim the loader at *path* and drop any snapshot cached from before."""
    monkeypatch.setenv("JOBHUNT_CONFIG", str(path))
    policy.invalidate_cache()


def policy_doc(
    skills: float,
    experience: float,
    candidate_skills: list,
    years: float = 7.5,
    revision: int = 1,
) -> dict:
    """A config whose scoring AND candidate blocks are both non-default.

    Both halves matter here: with a default candidate the two fingerprints
    could coincide for an uninteresting reason, and an equality test that holds
    only because both sides are empty proves nothing.
    """
    return {
        "config_version": 1,
        "revision": revision,
        "scoring": {"weights": {"skills": skills, "experience": experience}},
        "candidate": {"skills": list(candidate_skills), "years_experience": years},
    }


def find_key(node, key: str) -> list:
    """Every value stored under *key*, at any depth of a nested dict/list."""
    found = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                found.append(v)
            found.extend(find_key(v, key))
    elif isinstance(node, list):
        for item in node:
            found.extend(find_key(item, key))
    return found


def digest_routes() -> dict:
    """Every endpoint one ``instahyre_inbound_digest`` call touches."""
    return {
        C.EP_OPP_NAVBAR_COUNT: fixture_json("navbar_count.json"),
        C.EP_ACTIVITY_COUNTS: fixture_json("activity_counts.json"),
        C.EP_ACTIVITY: fixture_json("activity_viewed.json"),
        C.EP_MESSAGE_COUNT: fixture_json("message_count.json"),
        C.EP_OPPORTUNITIES: fixture_json("opportunities_pending.json"),
        C.EP_EDUCATION: fixture_json("education.json"),
        C.EP_PROFILE.format(candidate_id=CANDIDATE_ID): fixture_json(
            "candidate_profile.json"
        ),
    }


#: Cases chosen for what they exercise, not for coverage of the input space:
#: a plain result, one whose combined base is NOT an integer, one where the
#: bonus pushes the total over 100 so the ceiling has to apply, one with a
#: bonus that does not hit the ceiling, and one with no skill overlap at all.
EXPLAIN_CASES = {
    "plain_overlap": dict(
        job_skills=["Node.js", "React", "AWS", "MongoDB"],
        profile_skills=["nodejs", "reactjs", "typescript"],
        experience_min=3, experience_max=6, profile_years=5.0,
    ),
    "non_integer_combined": dict(
        job_skills=["Go", "Kubernetes"], profile_skills=["golang", "k8s"],
        experience_min=1, experience_max=2, profile_years=9.0,
    ),
    "ceiling_applied": dict(
        job_skills=["Node.js"], profile_skills=["nodejs"],
        experience_min=1, experience_max=20, profile_years=5.0,
        job_location="Remote", profile_location="Bengaluru",
    ),
    "bonus_below_ceiling": dict(
        job_skills=["Node.js", "React", "AWS", "Docker"],
        profile_skills=["nodejs", "react"],
        experience_min=3, experience_max=7, profile_years=5.0,
        job_location="Bengaluru", profile_location="Bengaluru",
    ),
    "no_profile_skills": dict(
        job_skills=["Java", "Spring"], profile_skills=[], profile_years=5.0,
    ),
}


# ---------------------------------------------------------------------------
# 1. The adapter: off by default, and off means absent
# ---------------------------------------------------------------------------


class TestTheAdapterThreadsExplainThrough:

    def test_the_default_result_has_no_explain_key_at_all(self):
        """Not ``None``, not ``{}`` -- absent. Anything else changes the shape
        of every result for every caller who did not ask for it."""
        result = scoring.score_job(**EXPLAIN_CASES["plain_overlap"])

        assert "explain" not in result
        assert find_key(result, "explain") == []

    def test_explain_true_adds_the_block(self):
        result = scoring.score_job(**EXPLAIN_CASES["plain_overlap"], explain=True)

        assert isinstance(result["explain"], dict)
        assert result["explain"], "an empty block is not an explanation"

    def test_explain_changes_nothing_else_about_the_result(self):
        """The block is additive. If asking for the working moved the number,
        the working would not be a description of anything."""
        plain = scoring.score_job(**EXPLAIN_CASES["plain_overlap"])
        explained = scoring.score_job(
            **EXPLAIN_CASES["plain_overlap"], explain=True
        )

        assert explained.pop("explain")
        assert explained == plain

    def test_explain_is_not_smuggled_in_through_scoring_args(self):
        """``scoring_args`` carries POLICY. ``explain`` is a per-call display
        choice, and a policy function that quietly decided display would be the
        wrong seam -- it would make the block un-turn-off-able per call."""
        assert "explain" not in policy.scoring_args()


# ---------------------------------------------------------------------------
# 2. The arithmetic reproduces the score -- the test that makes the rest real
# ---------------------------------------------------------------------------


class TestTheArithmeticReproducesTheScore:
    """A block that only echoed constants would pass every key-presence check
    above and fail every assertion here."""

    @pytest.mark.parametrize("case", sorted(EXPLAIN_CASES))
    def test_base_plus_bonuses_rounded_and_capped_is_the_overall_score(self, case):
        result = scoring.score_job(**EXPLAIN_CASES[case], explain=True)
        block = result["explain"]

        reconstructed = min(
            100, round(block["base"]["combined"] + block["bonuses"]["total"])
        )

        assert reconstructed == result["overall_score"], (
            "the explain block does not add up to the score it claims to "
            "explain: %.1f + %s -> %d, but the score is %d"
            % (
                block["base"]["combined"],
                block["bonuses"]["total"],
                reconstructed,
                result["overall_score"],
            )
        )
        assert block["overall_score"] == result["overall_score"]

    @pytest.mark.parametrize("case", sorted(EXPLAIN_CASES))
    def test_the_weighted_split_reproduces_the_combined_base(self, case):
        result = scoring.score_job(**EXPLAIN_CASES[case], explain=True)
        block = result["explain"]
        weights = block["weights"]
        base = block["base"]

        expected = base["skills"] * weights["skills"] + base["experience"] * weights["experience"]

        assert abs(expected - base["combined"]) < 0.15, (
            "base.skills and base.experience under these weights do not "
            "produce base.combined"
        )

    @pytest.mark.parametrize("case", sorted(EXPLAIN_CASES))
    def test_the_flags_agree_with_the_numbers_beside_them(self, case):
        """``score_ceiling_applied`` and ``bonus_cap_applied`` are derived, so
        they must track the arithmetic rather than being independently set."""
        block = scoring.score_job(**EXPLAIN_CASES[case], explain=True)["explain"]
        bonuses = block["bonuses"]

        uncapped = round(block["base"]["combined"] + bonuses["total"])

        assert block["score_ceiling_applied"] == (uncapped > 100)
        assert block["bonus_cap_applied"] == (bonuses["raw_total"] > bonuses["cap"])
        assert bonuses["total"] == min(bonuses["raw_total"], bonuses["cap"])

    def test_the_ceiling_case_really_does_exceed_one_hundred__CONTROL(self):
        """Shows the ceiling assertion above is capable of failing.

        If no case in the set ever went over 100, ``score_ceiling_applied``
        would be False everywhere and the flag test would be an assertion that
        False equals False.
        """
        block = scoring.score_job(
            **EXPLAIN_CASES["ceiling_applied"], explain=True
        )["explain"]

        assert block["base"]["combined"] + block["bonuses"]["total"] > 100
        assert block["score_ceiling_applied"] is True

    def test_the_verdict_band_is_the_one_the_score_falls_in(self):
        result = scoring.score_job(**EXPLAIN_CASES["plain_overlap"], explain=True)
        band = result["explain"]["verdict_band"]

        assert band["label"] == result["recommendation"]
        assert result["overall_score"] >= band["min"]


# ---------------------------------------------------------------------------
# 3. The block names the arithmetic, and only the arithmetic
# ---------------------------------------------------------------------------


class TestTheBlockCarriesTheScoringHash:

    def test_the_block_carries_scoring_hash_and_never_policy_hash(self):
        block = scoring.score_job(
            **EXPLAIN_CASES["plain_overlap"], explain=True
        )["explain"]

        assert block["scoring_hash"]
        assert "policy_hash" not in block, (
            "a result cannot vouch for policy_hash -- that covers the "
            "candidate block too, and the candidate arrives as a call argument"
        )

    def test_the_hash_moves_when_the_arithmetic_moves__CONTROL(self):
        """Otherwise the assertion above is satisfied by any constant string."""
        import dataclasses

        from jobcore import DEFAULT_SCORING_POLICY
        from jobcore.policy import Weights

        tilted = dataclasses.replace(
            DEFAULT_SCORING_POLICY, weights=Weights(skills=0.8, experience=0.2)
        )

        default_hash = scoring.score_job(
            **EXPLAIN_CASES["plain_overlap"], explain=True
        )["explain"]["scoring_hash"]
        tilted_hash = scoring.score_job(
            **EXPLAIN_CASES["plain_overlap"], policy=tilted, explain=True
        )["explain"]["scoring_hash"]

        assert default_hash != tilted_hash


# ---------------------------------------------------------------------------
# 4. instahyre_rank_jobs
# ---------------------------------------------------------------------------


class TestRankJobsSurfacesExplain:

    def _run(self, monkeypatch, search_payload, **kwargs) -> dict:
        from conftest import json_response

        client = make_client({SEARCH: json_response(search_payload)})
        monkeypatch.setattr(server_module, "_client", client)
        return server_module.instahyre_rank_jobs(
            my_skills=["Java", "Spring Boot", "Node.js"],
            my_experience_years=5.0,
            top_n=5,
            **kwargs,
        )

    def test_the_default_response_has_no_explain_key_anywhere(
        self, monkeypatch, search_payload
    ):
        out = self._run(monkeypatch, search_payload)

        assert out["ranked_jobs"], "nothing was scored, so nothing was proved"
        assert find_key(out, "explain") == []

    def test_explain_true_puts_a_block_on_every_scored_row(
        self, monkeypatch, search_payload
    ):
        out = self._run(monkeypatch, search_payload, explain=True)

        assert out["ranked_jobs"]
        for row in out["ranked_jobs"]:
            assert isinstance(row["explain"], dict)
            assert row["explain"]["scoring_hash"]
            assert row["explain"]["overall_score"] == row["fit_score"]

    def test_the_row_block_reproduces_that_rows_own_score(
        self, monkeypatch, search_payload
    ):
        """Per-row, not one block copied across rows: each has to add up to the
        score sitting beside it."""
        out = self._run(monkeypatch, search_payload, explain=True)

        for row in out["ranked_jobs"]:
            block = row["explain"]
            reconstructed = min(
                100, round(block["base"]["combined"] + block["bonuses"]["total"])
            )
            assert reconstructed == row["fit_score"]

    def test_the_rows_are_not_all_the_same_block__CONTROL(
        self, monkeypatch, search_payload
    ):
        """One block reused for every row would satisfy the test above whenever
        the scores happened to tie."""
        out = self._run(monkeypatch, search_payload, explain=True)
        combined = {row["explain"]["base"]["combined"] for row in out["ranked_jobs"]}

        assert len(combined) > 1, (
            "every row explained itself with an identical base -- either the "
            "fixture has no score spread, or one block is being copied"
        )

    def test_turning_explain_on_changes_nothing_but_the_block(
        self, monkeypatch, search_payload
    ):
        plain = self._run(monkeypatch, search_payload)
        explained = self._run(monkeypatch, search_payload, explain=True)

        for row in explained["ranked_jobs"]:
            row.pop("explain")
        assert explained == plain


# ---------------------------------------------------------------------------
# 5. instahyre_inbound_digest
# ---------------------------------------------------------------------------


class TestInboundDigestSurfacesExplain:

    def _run(self, monkeypatch, **kwargs) -> dict:
        client = make_client(digest_routes())
        monkeypatch.setattr(server_module, "_client", client)
        return server_module.instahyre_inbound_digest(**kwargs)

    def test_the_default_digest_has_no_explain_key_anywhere(self, monkeypatch):
        out = self._run(monkeypatch)

        assert out["top_opportunities"], "nothing was scored, so nothing was proved"
        assert any(o.get("fit_score") is not None for o in out["top_opportunities"])
        assert find_key(out, "explain") == []

    def test_explain_true_puts_a_block_on_every_scored_row(self, monkeypatch):
        out = self._run(monkeypatch, explain=True)

        scored = [o for o in out["top_opportunities"] if o.get("fit_score") is not None]
        assert scored
        for row in scored:
            block = row["explain"]
            assert block["scoring_hash"]
            assert block["overall_score"] == row["fit_score"]
            reconstructed = min(
                100, round(block["base"]["combined"] + block["bonuses"]["total"])
            )
            assert reconstructed == row["fit_score"]

    def test_unranked_and_explain_true_produces_no_block_and_does_not_raise(
        self, monkeypatch
    ):
        """With ``rank_against_my_profile=False`` no jobcore score is computed,
        so there is nothing for a block to explain. Asking for one anyway is
        not an error -- it is a no-op, and it has to stay one."""
        out = self._run(monkeypatch, rank_against_my_profile=False, explain=True)

        assert out["top_opportunities"]
        assert find_key(out, "explain") == []
        assert all("fit_score" not in o for o in out["top_opportunities"])

    def test_the_unranked_digest_is_the_same_with_explain_on_or_off(self, monkeypatch):
        off = self._run(monkeypatch, rank_against_my_profile=False)
        on = self._run(monkeypatch, rank_against_my_profile=False, explain=True)

        assert on == off


# ---------------------------------------------------------------------------
# 6. The bridge: a config readout matches the stamp on a result
# ---------------------------------------------------------------------------


ALPHA = ["Node.js", "TypeScript", "AWS"]
BETA = ["Java", "Spring Boot", "Kafka", "PostgreSQL"]

MOVER = dict(
    job_skills=["Node.js", "React", "AWS", "MongoDB"],
    profile_skills=["nodejs", "reactjs", "typescript"],
    experience_min=3, experience_max=6, profile_years=5.0,
)


class TestTheConfigReadoutBridgesToTheResultStamp:
    """The whole point of the rename, held by the two ends it joins.

    A stored score carries ``scoring_hash``. A config readout carries both
    fingerprints. Matching the score back to the config that produced it means
    comparing the stamp against the readout's ``scoring_hash`` -- and against
    ``policy_hash`` it would report a difference that is not there.
    """

    def test_the_readout_carries_both_hashes_and_they_differ(
        self, tmp_path, monkeypatch
    ):
        point_at(monkeypatch, write_config(
            tmp_path / "jobhunt.json", policy_doc(0.8, 0.2, ALPHA)))
        readout = policy.report()

        assert readout["policy_hash"]
        assert readout["scoring_hash"]
        assert readout["policy_hash"] != readout["scoring_hash"], (
            "one policy must not produce one value under two names -- that "
            "collision is what the rename exists to remove"
        )

    def test_the_readouts_scoring_hash_is_the_stamp_on_a_result(
        self, tmp_path, monkeypatch
    ):
        point_at(monkeypatch, write_config(
            tmp_path / "jobhunt.json", policy_doc(0.8, 0.2, ALPHA)))

        readout = policy.report()
        result = scoring.score_job(**MOVER, **policy.scoring_args())

        assert result["scoring_hash"] == readout["scoring_hash"]
        assert result["scoring_hash"] != readout["policy_hash"], (
            "if these were equal the rename would have been cosmetic"
        )

    def test_the_explain_blocks_hash_is_the_same_stamp(self, tmp_path, monkeypatch):
        point_at(monkeypatch, write_config(
            tmp_path / "jobhunt.json", policy_doc(0.8, 0.2, ALPHA)))

        readout = policy.report()
        result = scoring.score_job(**MOVER, explain=True, **policy.scoring_args())

        assert result["explain"]["scoring_hash"] == readout["scoring_hash"]

    def test_a_different_candidate_does_not_move_the_scoring_hash__CONTROL(
        self, tmp_path, monkeypatch
    ):
        """The non-tautology proof, and the original bug in miniature.

        Two configs with an IDENTICAL ``scoring`` block and DIFFERENT
        ``candidate`` blocks: the arithmetic is the same, so the scores are
        comparable and ``scoring_hash`` must match. ``policy_hash`` must not --
        and under the old single name, that difference was reported as a
        difference in comparability, which it never was.
        """
        point_at(monkeypatch, write_config(
            tmp_path / "alpha" / "jobhunt.json", policy_doc(0.8, 0.2, ALPHA)))
        alpha_readout = policy.report()
        alpha_result = scoring.score_job(**MOVER, **policy.scoring_args())

        point_at(monkeypatch, write_config(
            tmp_path / "beta" / "jobhunt.json", policy_doc(0.8, 0.2, BETA)))
        beta_readout = policy.report()
        beta_result = scoring.score_job(**MOVER, **policy.scoring_args())

        assert alpha_readout["candidate"] != beta_readout["candidate"], (
            "the two configs must really differ, or this proves nothing"
        )
        assert alpha_readout["policy_hash"] != beta_readout["policy_hash"]
        assert alpha_readout["scoring_hash"] == beta_readout["scoring_hash"]
        assert alpha_result["scoring_hash"] == beta_result["scoring_hash"]

    def test_a_different_weight_does_move_the_scoring_hash__CONTROL(
        self, tmp_path, monkeypatch
    ):
        """Shows the equality assertions above are capable of failing: if
        ``scoring_hash`` were constant, every one of them would pass."""
        point_at(monkeypatch, write_config(
            tmp_path / "one" / "jobhunt.json", policy_doc(0.8, 0.2, ALPHA)))
        one_readout = policy.report()
        one_result = scoring.score_job(**MOVER, **policy.scoring_args())

        point_at(monkeypatch, write_config(
            tmp_path / "two" / "jobhunt.json", policy_doc(0.7, 0.3, ALPHA)))
        two_readout = policy.report()
        two_result = scoring.score_job(**MOVER, **policy.scoring_args())

        assert one_readout["scoring_hash"] != two_readout["scoring_hash"]
        assert one_result["scoring_hash"] != two_result["scoring_hash"]
        assert one_result["scoring_hash"] == one_readout["scoring_hash"]
        assert two_result["scoring_hash"] == two_readout["scoring_hash"]

    def test_a_narrowed_section_still_carries_both_hashes(
        self, tmp_path, monkeypatch
    ):
        """``instahyre_config(section=...)`` re-projects the report by hand, so
        it is the branch that can silently drop a key the full readout has."""
        point_at(monkeypatch, write_config(
            tmp_path / "jobhunt.json", policy_doc(0.8, 0.2, ALPHA)))

        full = policy.report()
        narrowed = policy.report("scoring")

        assert narrowed["policy_hash"] == full["policy_hash"]
        assert narrowed["scoring_hash"] == full["scoring_hash"]

    def test_the_tool_provenance_block_carries_both_hashes(
        self, tmp_path, monkeypatch, search_payload
    ):
        """The hashes a ranking is stamped with, at the top level of the tool
        result, are a CONFIG readout -- so they carry both."""
        from conftest import json_response

        point_at(monkeypatch, write_config(
            tmp_path / "jobhunt.json", policy_doc(0.8, 0.2, ALPHA)))
        client = make_client({SEARCH: json_response(search_payload)})
        monkeypatch.setattr(server_module, "_client", client)

        out = server_module.instahyre_rank_jobs(
            my_skills=["Java"], top_n=3, explain=True
        )

        assert out["policy_hash"]
        assert out["scoring_hash"]
        assert out["policy_hash"] != out["scoring_hash"]
        for row in out["ranked_jobs"]:
            assert row["explain"]["scoring_hash"] == out["scoring_hash"], (
                "the rows and the provenance block disagree about which "
                "arithmetic produced these numbers"
            )
