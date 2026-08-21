"""The config seam: jobcore is a hard dependency, and its policy reaches this server.

WHAT WAS BROKEN
---------------
Until this commit, ``instahyre_server/scoring.py`` imported the FLAT jobcore
function ``compute_fit_score``, which delegates to ``jobcore.scoring.
DEFAULT_ENGINE`` -- a module-level singleton built at import time with default
everything, for the life of the process. This server never constructed an
engine and had nowhere to put a policy, so a weight edited in ``jobhunt.json``
could move a Naukri score and a Uplers score and could NOT move an Instahyre
one. It also reached jobcore through a ``sys.path`` hack at a sibling checkout
rather than an installed dependency, and carried a second scorer with its own
hardcoded 60/40 that read nothing.

WHAT THESE TESTS HOLD
---------------------
1. With no config file, every number is byte-for-byte what it was before any of
   this existed -- 15 golden cases captured from the pre-change module.
2. A non-default weight MOVES an Instahyre score, at the function and through
   the whole ``instahyre_rank_jobs`` tool.
3. A file edited on disk moves it again with no restart, including the
   constant-length ``0.6`` -> ``0.8`` edit that an mtime trigger misses.
4. The scoring path still never reads a file -- the policy is injected.
5. jobcore missing is a loud ImportError, not a quiet second opinion.

EVERY GUARD HERE HAS A CONTROL. A test named ``*__CONTROL`` exists to show the
guard above it is capable of failing: the weight assertions are re-run against
the OLD call (the flat function with no policy) and asserted NOT to move, and
each source-scanning instrument is pointed at input it must trip on. Six bugs
in this family this week were checks that could not fail; an uncontrolled guard
is a claim, not a measurement.

And the whole file has been RUN against a permissive build -- one that accepts
the policy and discards it, which is precisely the bug it exists to catch::

    PYTHONPATH=scripts pytest tests/test_scoring_policy.py -p permissive_scorer_control
    # 6 failed, 40 passed

``scripts/permissive_scorer_control.py`` ships that plugin and lists which six,
and why the other forty are supposed to survive it.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from conftest import make_client
from fastmcp.exceptions import ToolError

from instahyre_server import constants as C
from instahyre_server import policy, scoring
from instahyre_server import server as server_module

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SEARCH = C.EP_JOB_SEARCH

# The exact call signatures the baseline was captured with. Kept beside the
# expected values on purpose: a golden file whose inputs live somewhere else is
# a golden file that quietly stops testing what it says it tests.
BASELINE_CASES = {
    "plain_overlap": dict(
        job_skills=["Node.js", "React", "AWS", "MongoDB"],
        profile_skills=["nodejs", "reactjs", "typescript"],
        experience_min=3, experience_max=6, profile_years=5.0,
    ),
    "no_job_skills": dict(
        job_skills=[], profile_skills=["nodejs"], profile_years=5.0,
    ),
    "no_profile_skills": dict(
        job_skills=["Java", "Spring"], profile_skills=[], profile_years=5.0,
    ),
    "exact_band": dict(
        job_skills=["Python", "Django"], profile_skills=["python", "django"],
        experience_min=4, experience_max=8, profile_years=6.0,
    ),
    "under_band": dict(
        job_skills=["Python", "Django"], profile_skills=["python"],
        experience_min=8, experience_max=12, profile_years=2.0,
    ),
    "over_band": dict(
        job_skills=["Go", "Kubernetes"], profile_skills=["golang", "k8s"],
        experience_min=1, experience_max=2, profile_years=9.0,
    ),
    "open_ended_low": dict(
        job_skills=["Node.js"], profile_skills=["node"],
        experience_min=5, experience_max=None, profile_years=7.0,
    ),
    "no_experience_at_all": dict(
        job_skills=["Node.js", "GraphQL"], profile_skills=["nodejs"],
        profile_years=None,
    ),
    "location_spelling_variant": dict(
        job_skills=["Node.js", "React"], profile_skills=["nodejs", "react"],
        experience_min=3, experience_max=7, profile_years=5.0,
        job_location="Bangalore", profile_location="Bengaluru",
    ),
    "location_exact": dict(
        job_skills=["Node.js", "React", "AWS", "Docker"],
        profile_skills=["nodejs", "react"],
        experience_min=3, experience_max=7, profile_years=5.0,
        job_location="Bengaluru", profile_location="Bengaluru",
    ),
    "location_miss": dict(
        job_skills=["Node.js", "React"], profile_skills=["nodejs", "react"],
        experience_min=3, experience_max=7, profile_years=5.0,
        job_location="Chennai", profile_location="Bengaluru",
    ),
    "remote_job": dict(
        job_skills=["Node.js", "React"], profile_skills=["nodejs", "react"],
        experience_min=3, experience_max=7, profile_years=5.0,
        job_location="Remote", profile_location="Bengaluru",
    ),
    "digest_shape_skills_only": dict(
        job_skills=["Node.js", "TypeScript", "AWS", "Docker", "Kubernetes"],
        profile_skills=["nodejs", "typescript", "aws"],
        profile_years=None,
    ),
    "single_year_band": dict(
        job_skills=["React", "Redux"], profile_skills=["reactjs"],
        experience_min=5, experience_max=5, profile_years=5.0,
    ),
    "everything_matches": dict(
        job_skills=["Node.js"], profile_skills=["nodejs"],
        experience_min=1, experience_max=20, profile_years=5.0,
        job_location="Remote", profile_location="Bengaluru",
    ),
}

BASELINE = json.loads(
    (FIXTURES / "scoring_baseline_pre_config.json").read_text(encoding="utf-8")
)

# One case that exercises both halves of the base score, so a weight change on
# either side has to show up.
MOVER = dict(
    job_skills=["Node.js", "React", "AWS", "MongoDB"],
    profile_skills=["nodejs", "reactjs", "typescript"],
    experience_min=3, experience_max=6, profile_years=5.0,
)


def write_config(path: Path, document: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def point_at(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Aim the loader at *path* and drop any snapshot cached from before."""
    monkeypatch.setenv("JOBHUNT_CONFIG", str(path))
    policy.invalidate_cache()


def weights_doc(skills: float, experience: float, revision: int = 1) -> dict:
    return {
        "config_version": 1,
        "revision": revision,
        "scoring": {"weights": {"skills": skills, "experience": experience}},
    }


# ---------------------------------------------------------------------------
# 1. Nothing changed until he edits the file
# ---------------------------------------------------------------------------


class TestDefaultsAreTodaysLiterals:
    """Captured from the module as it stood BEFORE the config seam existed.

    These are not hand-written expectations. ``tests/fixtures/
    scoring_baseline_pre_config.json`` was produced by running the pre-change
    ``score_job`` over the cases above, so a difference of one point anywhere
    means the migration changed a number it promised not to change.
    """

    @pytest.mark.parametrize("case", sorted(BASELINE_CASES))
    def test_the_whole_result_dict_is_unchanged(self, case):
        assert scoring.score_job(**BASELINE_CASES[case]) == BASELINE[case]

    def test_the_baseline_covers_every_case_and_nothing_extra(self):
        assert set(BASELINE) == set(BASELINE_CASES)

    def test_a_default_scored_result_carries_no_policy_stamp(self):
        """A stamp on every result would change the shape for every caller.

        jobcore stamps exactly when the policy is NOT the shipped default, so
        today's output stays byte-identical and a non-default one announces
        itself. The next test proves the other half.
        """
        result = scoring.score_job(**MOVER)
        assert "policy_hash" not in result
        assert "policy_rev" not in result

    def test_a_non_default_scored_result_says_so(self, tmp_path, monkeypatch):
        point_at(monkeypatch, write_config(tmp_path / "jobhunt.json",
                                           weights_doc(0.8, 0.2)))
        result = scoring.score_job(**MOVER, **policy.scoring_args())
        assert result["policy_hash"], "a non-default policy must stamp its hash"
        assert result["policy_rev"] >= 1


# ---------------------------------------------------------------------------
# 2. A configured weight moves an Instahyre score -- the thing that was broken
# ---------------------------------------------------------------------------


class TestAConfiguredWeightMovesAnInstahyreScore:

    def test_the_function_moves(self):
        from jobcore import DEFAULT_SCORING_POLICY
        import dataclasses
        from jobcore.policy import Weights

        tilted = dataclasses.replace(
            DEFAULT_SCORING_POLICY, weights=Weights(skills=0.8, experience=0.2)
        )
        default_score = scoring.score_job(**MOVER)["overall_score"]
        tilted_score = scoring.score_job(**MOVER, policy=tilted)["overall_score"]

        # skills 50, experience 100. 0.6/0.4 -> 70; 0.8/0.2 -> 60.
        assert default_score == 70
        assert tilted_score == 60

    def test_the_function_could_not_move_before_this_change__CONTROL(self):
        """The OLD call, re-created verbatim: the flat jobcore function.

        This is what ``score_job`` used to do. It resolves to ``DEFAULT_ENGINE``
        -- built at import with default everything -- so no policy can reach it
        and the number above is unreachable. If this control ever starts
        moving, the test above has stopped being evidence of anything.
        """
        import dataclasses

        from jobcore import DEFAULT_SCORING_POLICY, compute_fit_score, parse_skills
        from jobcore.policy import Weights

        tilted = dataclasses.replace(
            DEFAULT_SCORING_POLICY, weights=Weights(skills=0.8, experience=0.2)
        )

        def score_the_old_way():
            return compute_fit_score(
                job_skills=parse_skills(list(MOVER["job_skills"])),
                profile_skills=parse_skills(list(MOVER["profile_skills"])),
                job_exp_str="3-6 years",
                profile_exp=MOVER["profile_years"],
                experience_min=MOVER["experience_min"],
                experience_max=MOVER["experience_max"],
            )["overall_score"]

        assert score_the_old_way() == 70
        # The policy exists, is different, and the old path cannot see it.
        assert tilted != DEFAULT_SCORING_POLICY
        assert score_the_old_way() == 70

    def test_the_whole_tool_moves(self, tmp_path, monkeypatch, search_payload):
        """End to end: a file on disk changes what ``instahyre_rank_jobs`` returns."""
        from conftest import json_response

        def run() -> dict:
            client = make_client({SEARCH: json_response(search_payload)})
            monkeypatch.setattr(server_module, "_client", client)
            return server_module.instahyre_rank_jobs(
                my_skills=["Java", "Spring Boot", "Node.js"],
                my_experience_years=5.0,
                top_n=10,
            )

        monkeypatch.setenv("JOBHUNT_CONFIG", ":none:")
        policy.invalidate_cache()
        before = run()

        point_at(monkeypatch, write_config(tmp_path / "jobhunt.json",
                                           weights_doc(0.9, 0.1)))
        after = run()

        assert before["scored"] == after["scored"] > 0
        before_scores = [j["fit_score"] for j in before["ranked_jobs"]]
        after_scores = [j["fit_score"] for j in after["ranked_jobs"]]
        assert before_scores != after_scores, (
            "a weight of 0.9/0.1 in the config file did not move a single "
            "Instahyre score -- the policy is not reaching the engine"
        )
        assert before.get("policy_hash") != after.get("policy_hash")
        assert after["policy_rev"] >= 1

    def test_the_tool_reports_which_policy_produced_the_numbers(
        self, tmp_path, monkeypatch, search_payload
    ):
        from conftest import json_response

        point_at(monkeypatch, write_config(tmp_path / "jobhunt.json",
                                           weights_doc(0.7, 0.3)))
        client = make_client({SEARCH: json_response(search_payload)})
        monkeypatch.setattr(server_module, "_client", client)
        out = server_module.instahyre_rank_jobs(my_skills=["Java"], top_n=3)

        assert out["scoring_engine"] == "jobcore"
        assert out["policy_hash"]
        assert out["config_source"] == str(tmp_path / "jobhunt.json")


# ---------------------------------------------------------------------------
# 3. The file, re-read without a restart
# ---------------------------------------------------------------------------


class TestTheFileIsRereadWithoutARestart:

    def test_a_constant_length_edit_is_seen(self, tmp_path, monkeypatch):
        """``0.6`` -> ``0.8`` changes neither the file's length nor, reliably, its mtime.

        Measured on this filesystem when the loader was designed: 12 atomic
        replaces produced only 8 distinct ``(mtime_ns, size)`` pairs. jobcore
        hashes the bytes instead, so this edit is visible. No cache is dropped
        between the two reads -- that is the point of the test.
        """
        path = tmp_path / "jobhunt.json"
        write_config(path, weights_doc(0.6, 0.4))
        point_at(monkeypatch, path)

        first = scoring.score_job(**MOVER, **policy.scoring_args())["overall_score"]

        raw = path.read_text(encoding="utf-8")
        edited = raw.replace('"skills": 0.6', '"skills": 0.8').replace(
            '"experience": 0.4', '"experience": 0.2'
        )
        assert len(edited) == len(raw), "the edit must be constant-length to be a test"
        path.write_text(edited, encoding="utf-8")
        os.utime(path, (0, 0))  # and force the mtime backwards, for good measure

        second = scoring.score_job(**MOVER, **policy.scoring_args())["overall_score"]
        assert (first, second) == (70, 60)

    def test_an_unreadable_file_falls_back_loudly_not_silently(self, tmp_path, monkeypatch):
        path = tmp_path / "jobhunt.json"
        path.write_text("{ this is not json", encoding="utf-8")
        point_at(monkeypatch, path)

        snapshot = policy.current()
        assert snapshot.config_error, "a malformed config must say so"
        # ... and still score, on defaults, rather than taking the server down.
        assert scoring.score_job(**MOVER, **policy.scoring_args(snapshot))[
            "overall_score"
        ] == 70
        assert "config_error" in policy.summary(snapshot)


# ---------------------------------------------------------------------------
# 4. The scoring path still reads no files
# ---------------------------------------------------------------------------


def _module_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = ("." * node.level) + (node.module or "")
            names.add(base)
            names.update("%s.%s" % (base, alias.name) for alias in node.names)
    return names


FILE_READING = ("jobcore.config", ".config", ".policy", "instahyre_server.policy")


def _file_reading_imports(path: Path) -> set[str]:
    return {n for n in _module_imports(path) if any(n.startswith(f) for f in FILE_READING)}


class TestTheScoringPathNeverReadsAFile:

    def test_scoring_imports_nothing_that_opens_a_file(self):
        assert _file_reading_imports(REPO / "instahyre_server" / "scoring.py") == set()

    def test_the_scan_trips_on_a_module_that_does__CONTROL(self):
        """Pointed at ``policy.py``, which imports ``jobcore.config`` on purpose.

        Without this, the assertion above would pass just as happily against a
        checker that never finds anything.
        """
        found = _file_reading_imports(REPO / "instahyre_server" / "policy.py")
        assert found, "the import scanner found nothing in the one module that must trip it"
        assert any("jobcore.config" in name for name in found)

    def test_scoring_never_calls_open(self):
        tree = ast.parse(
            (REPO / "instahyre_server" / "scoring.py").read_text(encoding="utf-8")
        )
        calls = [
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        ]
        assert "open" not in calls


# ---------------------------------------------------------------------------
# 5. jobcore is a hard dependency, and there is exactly one engine
# ---------------------------------------------------------------------------


BLOCK_JOBCORE = textwrap.dedent(
    """
    import sys

    class Blocker:
        def find_spec(self, name, path=None, target=None):
            if name == "jobcore" or name.startswith("jobcore."):
                raise ImportError("jobcore is hidden for this test")
            return None

    if %(block)s:
        sys.meta_path.insert(0, Blocker())

    try:
        import instahyre_server.scoring
    except ImportError as exc:
        print("IMPORTERROR::" + " | ".join(str(exc).splitlines()))
        raise SystemExit(0)
    print("IMPORTED::" + instahyre_server.scoring.ENGINE)
    raise SystemExit(0)
    """
)


def _probe_import(block: bool) -> str:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO)
    env["JOBHUNT_CONFIG"] = ":none:"
    proc = subprocess.run(
        [sys.executable, "-c", BLOCK_JOBCORE % {"block": block}],
        cwd=str(REPO), capture_output=True, text=True, timeout=120, env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout.strip()


class TestJobcoreIsAHardDependency:

    def test_a_missing_jobcore_is_a_loud_import_error_naming_the_fix(self):
        out = _probe_import(block=True)
        assert out.startswith("IMPORTERROR::"), out
        assert "pip install -e ../jobcore" in out
        assert "requirements-ci.txt" in out
        assert "no local fallback" in out

    def test_the_probe_imports_fine_when_jobcore_is_present__CONTROL(self):
        """Otherwise the test above would pass on any broken subprocess."""
        out = _probe_import(block=False)
        assert out == "IMPORTED::jobcore", out

    def test_the_installed_jobcore_is_a_real_distribution_not_a_path_hack(self):
        """``importlib.metadata`` only knows about something that was INSTALLED.

        The sibling ``sys.path`` insert this module used to do produces a
        working import and no distribution at all, so this is the assertion
        that separates the two.
        """
        import importlib.metadata as md

        assert md.version("jobcore")
        assert scoring.ENGINE_VERSION == md.version("jobcore")


def _engine_assignments(path: Path) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "ENGINE" for t in node.targets)
    ]


class TestThereIsExactlyOneEngine:

    def test_engine_is_assigned_once_and_is_the_constant_jobcore(self):
        assigns = _engine_assignments(REPO / "instahyre_server" / "scoring.py")
        assert len(assigns) == 1
        value = assigns[0].value
        assert isinstance(value, ast.Constant) and value.value == "jobcore"

    def test_the_scan_counts_more_than_one_when_there_is_more_than_one__CONTROL(
        self, tmp_path
    ):
        """The pre-change module assigned ENGINE three times, one of them
        ``"local-fallback"``. Fed that shape, the counter must say so."""
        decoy = tmp_path / "decoy.py"
        decoy.write_text(
            "ENGINE = 'jobcore'\n"
            "try:\n    ENGINE = 'jobcore'\nexcept ImportError:\n"
            "    ENGINE = 'local-fallback'\n",
            encoding="utf-8",
        )
        assert len(_engine_assignments(decoy)) == 3

    def test_no_fallback_scorer_survives_anywhere_in_the_package(self):
        offenders = []
        for path in (REPO / "instahyre_server").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for marker in ("local-fallback", "_fallback_score", "_fallback_parse"):
                # The scoring module explains the deletion in prose; a mention
                # inside its own docstring is documentation, not a code path.
                body = text.split('"""', 2)[-1] if path.name == "scoring.py" else text
                if marker in body:
                    offenders.append("%s: %s" % (path.name, marker))
        assert offenders == []


# ---------------------------------------------------------------------------
# 6. The candidate block is read, and the vocabulary reaches the taxonomy
# ---------------------------------------------------------------------------


class TestTheCandidateBlockReachesTheScore:

    def test_candidate_locations_supply_the_location_bonus(self, tmp_path, monkeypatch):
        case = dict(
            job_skills=["Node.js", "React", "AWS", "Docker"],
            profile_skills=["nodejs", "react"],
            experience_min=3, experience_max=7, profile_years=5.0,
            job_location="Bengaluru", profile_location=None,
        )
        assert scoring.score_job(**case)["bonuses"]["location"] == 0

        point_at(monkeypatch, write_config(tmp_path / "jobhunt.json", {
            "config_version": 1, "revision": 1,
            "candidate": {"locations": ["Bengaluru"]},
        }))
        scored = scoring.score_job(**case, **policy.scoring_args())
        assert scored["bonuses"]["location"] == 5

    def test_candidate_skills_fill_in_for_an_omitted_my_skills(
        self, tmp_path, monkeypatch, search_payload
    ):
        from conftest import json_response

        point_at(monkeypatch, write_config(tmp_path / "jobhunt.json", {
            "config_version": 1, "revision": 1,
            "candidate": {"skills": ["Java", "Spring Boot"], "years_experience": 6.0},
        }))
        client = make_client({SEARCH: json_response(search_payload)})
        monkeypatch.setattr(server_module, "_client", client)
        out = server_module.instahyre_rank_jobs(top_n=3)
        assert out["scored_against_skills"] == ["Java", "Spring Boot"]

    def test_no_skills_anywhere_is_refused_by_name_not_scored_as_zero(
        self, monkeypatch, search_payload
    ):
        from conftest import json_response

        monkeypatch.setenv("JOBHUNT_CONFIG", ":none:")
        policy.invalidate_cache()
        client = make_client({SEARCH: json_response(search_payload)})
        monkeypatch.setattr(server_module, "_client", client)
        # ``handled`` translates the typed error into the ToolError an MCP
        # client actually sees; the field name has to survive that translation.
        with pytest.raises(ToolError) as excinfo:
            server_module.instahyre_rank_jobs()
        assert "[invalid_filter]" in str(excinfo.value)
        assert "my_skills" in str(excinfo.value)


class TestVocabularyAdditionsReachTheTaxonomy:

    def test_an_extra_skill_alias_starts_matching(self, tmp_path, monkeypatch):
        """A weight is not enough: a skill he teaches the system has to reach
        the TAXONOMY, or the job asking for it still fails to match."""
        case = dict(
            job_skills=["Temporal.io"], profile_skills=["temporal"], profile_years=5.0,
        )
        assert scoring.score_job(**case)["skill_match"]["score"] == 0

        point_at(monkeypatch, write_config(tmp_path / "jobhunt.json", {
            "config_version": 1, "revision": 1,
            "scoring": {"skills": {"extra_skills": {"temporal": ["temporal.io"]}}},
        }))
        scored = scoring.score_job(**case, **policy.scoring_args())
        assert scored["skill_match"]["score"] == 100
        assert scored["skill_match"]["matched"] == ["temporal"]


# ---------------------------------------------------------------------------
# 7. What the config file cannot do here
# ---------------------------------------------------------------------------


class TestTheConfigCannotGrantApplyAuthorityHere:

    def test_a_tier_c_key_in_the_file_is_refused_and_named(self, tmp_path, monkeypatch):
        """Refused at LOAD, not at write: a text editor never goes near a write path."""
        point_at(monkeypatch, write_config(tmp_path / "jobhunt.json", {
            "config_version": 1, "revision": 1,
            "servers": {"instahyre": {"agent": {"enabled": True, "mode": "auto"},
                                      "min_fit_score": 0}},
        }))
        snapshot = policy.current()
        refused = " ".join(snapshot.tier_c_refusals)
        assert "agent.enabled" in refused
        assert "agent.mode" in refused
        assert "min_fit_score" in refused
        assert snapshot.server("instahyre").get("agent", {}).get("enabled") is not True

    def test_the_refusals_are_visible_from_the_tool(self, tmp_path, monkeypatch):
        point_at(monkeypatch, write_config(tmp_path / "jobhunt.json", {
            "config_version": 1, "revision": 1,
            "servers": {"instahyre": {"agent": {"enabled": True}}},
        }))
        report = server_module.instahyre_config()
        assert report["tier_c_refusals"], (
            "a refused key that nothing surfaces is a silently ignored write"
        )

    def test_the_scan_finds_nothing_when_the_file_is_honest__CONTROL(
        self, tmp_path, monkeypatch
    ):
        point_at(monkeypatch, write_config(tmp_path / "jobhunt.json",
                                           weights_doc(0.7, 0.3)))
        assert policy.current().tier_c_refusals == ()

    def test_apply_still_requires_a_human_confirm_whatever_the_file_says(
        self, tmp_path, monkeypatch
    ):
        """No config value reaches this. The gate is a parameter, in source."""
        import inspect

        point_at(monkeypatch, write_config(tmp_path / "jobhunt.json", {
            "config_version": 1, "revision": 1,
            "servers": {"instahyre": {"agent": {"enabled": True, "mode": "auto"},
                                      "confirm": False, "auto_apply": True}},
        }))
        for name in ("instahyre_apply", "instahyre_decline_opportunity"):
            signature = inspect.signature(getattr(server_module, name))
            assert signature.parameters["confirm"].default is False

    def test_this_server_has_no_agent_loop_to_grant_authority_to(self):
        """The 5-call escalation traced on naukri has no landing site here.

        Stated as a test rather than a comment so that adding one is a visible
        red line rather than a quiet afternoon.
        """
        names = {p.stem for p in (REPO / "instahyre_server").glob("*.py")}
        assert "agent" not in names and "scheduler" not in names


# ---------------------------------------------------------------------------
# 8. The read tool
# ---------------------------------------------------------------------------


class TestTheConfigTool:

    def test_it_reports_defaults_and_every_path_it_tried(self, monkeypatch):
        monkeypatch.delenv("JOBHUNT_CONFIG", raising=False)
        monkeypatch.setenv("JOBHUNT_DISABLE", "1")
        policy.invalidate_cache()
        report = server_module.instahyre_config()
        assert report["source"] is None
        assert "built-in defaults" in report["config_status"]
        assert report["scoring"]["weights"] == {"skills": 0.6, "experience": 0.4}

    def test_it_narrows_to_a_section(self, tmp_path, monkeypatch):
        point_at(monkeypatch, write_config(tmp_path / "jobhunt.json",
                                           weights_doc(0.75, 0.25)))
        report = server_module.instahyre_config(section="scoring")
        assert report["section"] == "scoring"
        assert report["scoring"]["weights"]["skills"] == 0.75
        assert "candidate" not in report

    def test_an_unknown_section_is_refused_by_name(self):
        with pytest.raises(ToolError) as excinfo:
            server_module.instahyre_config(section="wieghts")
        assert "wieghts" in str(excinfo.value)
        assert "field=section" in str(excinfo.value)

    def test_it_makes_no_request_and_builds_no_client(self, monkeypatch):
        monkeypatch.setattr(server_module, "_client", None)
        server_module.instahyre_config()
        assert server_module._client is None
