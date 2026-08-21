"""The seam to the shared scoring engine.

``jobcore`` is the platform-agnostic scorer this server shares with the Naukri
and Uplers servers. It is a REQUIRED dependency, imported normally -- no
``sys.path`` hack reaching at a sibling checkout, and no local fallback.

WHY THE LOCAL FALLBACK WAS DELETED
----------------------------------
This module used to carry an ~30-line scorer that ran whenever ``import
jobcore`` failed, reporting ``ENGINE = "local-fallback"``. It is gone, and the
reason is not tidiness:

* **Its numbers were never comparable, and looked like they were.** It did no
  skill aliasing at all, so ``"Node.js"`` on a job and ``"nodejs"`` on the
  profile simply did not match; every score it produced was systematically
  lower than jobcore's on the same inputs, under the same ``fit_score`` key.
  It also carried its own ``0.6/0.4`` split and its own ``70/45`` verdict
  bands, which had already drifted from jobcore's four-band table.
* **It was the second engine, and now the numbers are configurable.** With
  weights, bands and vocabulary living in ``jobhunt.json``, a fallback that
  cannot read them is an unconfigurable engine silently shadowing the
  configurable one. Teaching it to read the same policy would not fix it --
  it would still disagree, because the disagreement is the taxonomy.
* **It was reachable only by accident.** Every documented install path now
  puts jobcore in the environment (``pip install -e ../jobcore`` locally,
  ``requirements-ci.txt`` for a sibling-free clone or CI). A branch nothing
  documented, nothing tested and nothing wanted is dead code that produces
  wrong-looking-right numbers on the one day it runs.

A missing jobcore is now an ImportError naming the fix. That is strictly better
than a silent second opinion.

POLICY IS INJECTED, NEVER READ HERE
-----------------------------------
Nothing in this module opens a file. The caller binds one snapshot at tool
entry (see :mod:`instahyre_server.policy`) and passes it down, so every job in
one ranking is scored under the same policy even if the file changes mid-call.
Called with no policy, this scores under jobcore's shipped defaults -- which
are exactly the literals it used before any of this existed.
"""

from __future__ import annotations

from typing import Iterable, Optional

try:
    import jobcore
    from jobcore import DEFAULT_TAXONOMY, CandidatePolicy, ScoringEngine, ScoringPolicy
except ImportError as exc:  # pragma: no cover - exercised by a subprocess test
    raise ImportError(
        "instahyre-mcp requires jobcore, the shared scoring engine. It is not "
        "on PyPI. Install it one of these two ways:\n"
        "    pip install -e ../jobcore          # with the sibling checkout\n"
        "    pip install -r requirements-ci.txt # no sibling: pinned, from git\n"
        "There is deliberately no local fallback scorer: one that cannot read "
        "the shared skill taxonomy or the configured weights would quietly "
        "produce numbers that are not comparable with any other board."
    ) from exc

__all__ = [
    "ENGINE",
    "ENGINE_VERSION",
    "engine_for",
    "score_job",
    "parse_experience_range",
]

#: Which engine produced a number. One value now, and it stays in the tool
#: output: a caller comparing a score across boards should not have to know
#: which repo it came from to know what it means.
ENGINE = "jobcore"

#: The engine's version, reported beside :data:`ENGINE`. A score is only
#: comparable with another score from the same engine AND the same policy;
#: this names the first half, ``policy_hash`` names the second.
ENGINE_VERSION = jobcore.__version__

#: Built engines, keyed by the (policy, candidate) pair they are bound to.
#: Both are frozen, hashable dataclasses. Rebuilding the taxonomy for every job
#: in a 35-job ranking would be wasteful; keeping an unbounded map of them
#: would be a leak, so it is cleared once it grows past a handful -- policies
#: change when a human edits a file, not per request.
_ENGINES: dict[tuple, ScoringEngine] = {}
_ENGINE_CACHE_MAX = 8


def engine_for(
    policy: Optional[ScoringPolicy] = None,
    candidate: Optional[CandidatePolicy] = None,
) -> ScoringEngine:
    """A :class:`~jobcore.scoring.ScoringEngine` bound to *policy*.

    Both arguments may be ``None``, which means jobcore's shipped defaults --
    the same numbers ``DEFAULT_ENGINE`` carries, so behaviour with no config
    file present is unchanged.

    ``scoring.skills.extra_skills`` is applied here: a vocabulary addition in
    the config file has to reach the TAXONOMY, not just the weights, or a skill
    he taught the system would still fail to match the job that asks for it.
    """
    key = (policy, candidate)
    hit = _ENGINES.get(key)
    if hit is not None:
        return hit

    taxonomy = DEFAULT_TAXONOMY
    if policy is not None:
        extension = policy.skills.taxonomy_extension()
        if extension:
            taxonomy = DEFAULT_TAXONOMY.extended(extension)

    engine = ScoringEngine(taxonomy=taxonomy, policy=policy, candidate=candidate)
    if len(_ENGINES) >= _ENGINE_CACHE_MAX:
        _ENGINES.clear()
    _ENGINES[key] = engine
    return engine


def score_job(
    *,
    job_skills: Iterable[str],
    profile_skills: Iterable[str],
    experience_min: Optional[int] = None,
    experience_max: Optional[int] = None,
    profile_years: Optional[float] = None,
    job_location: Optional[str] = None,
    profile_location: Optional[str] = None,
    policy: Optional[ScoringPolicy] = None,
    candidate: Optional[CandidatePolicy] = None,
    policy_rev: Optional[int] = None,
) -> dict:
    """Score one job against a profile. Returns a flat dict plus ``engine``.

    Args:
        policy: the configured :class:`~jobcore.policy.ScoringPolicy`. Pass
            ``**instahyre_server.policy.scoring_args(snapshot)`` rather than
            building this by hand.
        candidate: his canonical self-description; supplies fallback locations
            and the work-mode preference order.
        policy_rev: stamped into the result when the policy is not the shipped
            default, so a number that came out of a non-default policy says so.
    """
    engine = engine_for(policy, candidate)
    result = dict(
        engine.compute_fit_score(
            job_skills=engine.parse_skills(list(job_skills)),
            profile_skills=engine.parse_skills(list(profile_skills)),
            job_exp_str=_range_str(experience_min, experience_max) or "",
            profile_exp=profile_years,
            job_location=job_location,
            profile_location=profile_location,
            experience_min=experience_min,
            experience_max=experience_max,
            policy_rev=policy_rev,
        )
    )
    result["engine"] = ENGINE
    # Instahyre publishes no salary, so no salary component can ever contribute.
    result["salary_component"] = None
    return result


def _range_str(lo: Optional[int], hi: Optional[int]) -> Optional[str]:
    if lo is None and hi is None:
        return None
    if lo is not None and hi is not None:
        return f"{lo}-{hi} years"
    return f"{lo}+ years" if lo is not None else f"up to {hi} years"


def parse_experience_range(value: Optional[str]) -> tuple[Optional[int], Optional[int]]:
    """``"4-8"`` -> ``(4, 8)``; ``"5+"`` -> ``(5, None)``; ``"<=8"`` -> ``(None, 8)``."""
    if not value:
        return None, None
    text = value.strip()
    if text.endswith("+"):
        return _int_or_none(text[:-1]), None
    if text.startswith("<="):
        return None, _int_or_none(text[2:])
    if "-" in text:
        lo, _, hi = text.partition("-")
        return _int_or_none(lo), _int_or_none(hi)
    single = _int_or_none(text)
    return single, single


def _int_or_none(text: str) -> Optional[int]:
    try:
        return int(text.strip())
    except (TypeError, ValueError):
        return None
