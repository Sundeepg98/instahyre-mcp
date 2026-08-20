"""The seam to the shared scoring engine.

``jobcore`` is the platform-agnostic scorer extracted from the Naukri server.
If it is importable we use it; if it is not, a deliberately small local
fallback keeps the tool working and says plainly which engine produced the
number. Nothing here re-implements skill aliasing -- that is jobcore's job.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Iterable, Optional

_JOBCORE_SRC = Path(__file__).resolve().parent.parent.parent / "jobcore" / "src"

try:  # installed, or importable from the sibling checkout
    from jobcore import compute_fit_score, parse_skills  # type: ignore

    ENGINE = "jobcore"
except ImportError:  # pragma: no cover - exercised only when jobcore is absent
    if _JOBCORE_SRC.is_dir() and str(_JOBCORE_SRC) not in sys.path:
        sys.path.insert(0, str(_JOBCORE_SRC))
    try:
        from jobcore import compute_fit_score, parse_skills  # type: ignore

        ENGINE = "jobcore"
    except ImportError:
        compute_fit_score = None  # type: ignore
        parse_skills = None  # type: ignore
        ENGINE = "local-fallback"


def _fallback_parse(raw: Any) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, str):
        raw = [part for part in raw.replace(";", ",").split(",")]
    return {str(item).strip().casefold() for item in raw if str(item).strip()}


def _fallback_score(
    job_skills: set[str],
    profile_skills: set[str],
    experience_min: Optional[int],
    experience_max: Optional[int],
    profile_years: Optional[float],
) -> dict:
    """Jaccard-ish skill overlap plus a band check. Honest, and clearly labelled."""
    matched = sorted(job_skills & profile_skills)
    skill_pct = round(100 * len(matched) / len(job_skills)) if job_skills else 0
    if profile_years is None or (experience_min is None and experience_max is None):
        exp_pct = 50
    elif (experience_min is None or profile_years >= experience_min) and (
        experience_max is None or profile_years <= experience_max
    ):
        exp_pct = 100
    else:
        gap = min(
            abs(profile_years - (experience_min or profile_years)),
            abs(profile_years - (experience_max or profile_years)),
        )
        exp_pct = max(0, 100 - int(gap * 20))
    overall = round(0.6 * skill_pct + 0.4 * exp_pct)
    return {
        "overall_score": overall,
        "skill_match": {"matched": matched, "percent": skill_pct},
        "experience_match": exp_pct,
        "recommendation": "strong" if overall >= 70 else "possible" if overall >= 45 else "weak",
    }


def score_job(
    *,
    job_skills: Iterable[str],
    profile_skills: Iterable[str],
    experience_min: Optional[int] = None,
    experience_max: Optional[int] = None,
    profile_years: Optional[float] = None,
    job_location: Optional[str] = None,
    profile_location: Optional[str] = None,
) -> dict:
    """Score one job against a profile. Returns a flat dict plus ``engine``."""
    if ENGINE == "jobcore":
        job_set = parse_skills(list(job_skills))
        profile_set = parse_skills(list(profile_skills))
        exp_str = _range_str(experience_min, experience_max) or ""
        result = compute_fit_score(
            job_skills=job_set,
            profile_skills=profile_set,
            job_exp_str=exp_str,
            profile_exp=profile_years,
            job_location=job_location,
            profile_location=profile_location,
            experience_min=experience_min,
            experience_max=experience_max,
        )
    else:
        result = _fallback_score(
            _fallback_parse(list(job_skills)),
            _fallback_parse(list(profile_skills)),
            experience_min,
            experience_max,
            profile_years,
        )
    result = dict(result)
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
