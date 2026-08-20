"""Taxonomies, and the resolvers that keep a filter from failing on a capital letter.

Instahyre validates ``jobLocations`` server-side and is **case-sensitive**:
``Bangalore`` is 7,161 jobs and ``bangalore`` is HTTP 400 "Invalid location".
An agent typing a city name naturally will hit that constantly, so every
user-supplied filter value goes through a resolver here first, and a miss comes
back as a suggestion rather than a wire error.
"""

from __future__ import annotations

import difflib
from typing import Any, Iterable, Optional

from . import constants as C
from .cache import Store
from .errors import InvalidFilter
from .http import InstahyreHTTP

_NS = "taxonomy"


class Taxonomy:
    """Lazily-loaded, 30-day-cached reference data plus fuzzy resolvers."""

    def __init__(self, http: InstahyreHTTP, store: Store) -> None:
        self._http = http
        self._store = store

    # -- loaders -----------------------------------------------------------

    def _cached(self, key: str, fetch) -> Any:
        hit = self._store.get(_NS, key)
        if hit is not None:
            return hit
        value = fetch()
        self._store.put(_NS, key, value, C.TTL_TAXONOMY)
        return value

    def job_functions(self) -> list[dict]:
        """58 job functions, each with its parent category and an ``is_tech`` flag."""

        def fetch() -> list[dict]:
            payload = self._http.get(C.EP_JOB_FUNCTION)
            out = []
            for obj in payload.get("objects", []):
                category = obj.get("job_category") or {}
                out.append(
                    {
                        "id": obj.get("id"),
                        "name": obj.get("name"),
                        "slug": obj.get("slug"),
                        "category": category.get("name"),
                        "is_tech": category.get("is_tech"),
                        "is_live": obj.get("is_live"),
                    }
                )
            return out

        return self._cached("job_functions", fetch)

    def industries(self) -> list[dict]:
        """74 industry types."""

        def fetch() -> list[dict]:
            payload = self._http.get(C.EP_INDUSTRY_TYPE)
            return [
                {"id": o.get("id"), "name": o.get("name")} for o in payload.get("objects", [])
            ]

        return self._cached("industries", fetch)

    def locations(self) -> list[dict]:
        """308 accepted location tokens, grouped by state / region / remote.

        Served from a candidate-profile endpoint that happens to be public --
        it is the only enumeration of valid ``jobLocations`` values that exists.
        """

        def fetch() -> list[dict]:
            payload = self._http.get(C.EP_LOCATION_DATA)
            data = (payload or {}).get("data") or {}
            out = []
            for entry in data.get("cities_preferred", []):
                value = entry.get("value")
                if not value:
                    continue
                out.append(
                    {"value": value, "name": entry.get("name") or value, "group": entry.get("type")}
                )
            return out

        return self._cached("locations", fetch)

    def industry_names(self) -> dict[int, str]:
        """id -> name, for resolving the industry facet block (which returns ids)."""
        return {row["id"]: row["name"] for row in self.industries() if row.get("id") is not None}

    # -- resolvers ---------------------------------------------------------

    def resolve_location(self, value: str) -> str:
        """User text -> the exact token the API accepts, or a helpful 400."""
        return _resolve(
            value,
            [row["value"] for row in self.locations()],
            field="jobLocations",
            what="location",
        )

    def resolve_locations(self, values: Optional[Iterable[str]]) -> Optional[list[str]]:
        if values is None:
            return None
        if isinstance(values, str):
            values = [values]
        return [self.resolve_location(v) for v in values]

    def resolve_job_function(self, value: Any) -> int:
        """Accept an id, an exact name, or something close to one."""
        rows = self.job_functions()
        if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
            wanted = int(value)
            if any(row["id"] == wanted for row in rows):
                return wanted
            raise InvalidFilter(
                f"No job function with id {wanted}. Call instahyre_list_job_functions to see all 58.",
                field="job_functions",
            )
        name = _resolve(
            str(value), [row["name"] for row in rows], field="job_functions", what="job function"
        )
        return next(row["id"] for row in rows if row["name"] == name)

    def resolve_job_functions(self, values: Optional[Iterable[Any]]) -> Optional[list[int]]:
        if values is None:
            return None
        if isinstance(values, (str, int)):
            values = [values]
        return [self.resolve_job_function(v) for v in values]

    def resolve_industry(self, value: Any) -> int:
        rows = self.industries()
        if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
            wanted = int(value)
            if any(row["id"] == wanted for row in rows):
                return wanted
            raise InvalidFilter(f"No industry with id {wanted}.", field="industry_types")
        name = _resolve(
            str(value), [row["name"] for row in rows], field="industry_types", what="industry"
        )
        return next(row["id"] for row in rows if row["name"] == name)

    def resolve_industries(self, values: Optional[Iterable[Any]]) -> Optional[list[int]]:
        if values is None:
            return None
        if isinstance(values, (str, int)):
            values = [values]
        return [self.resolve_industry(v) for v in values]


def _resolve(value: str, candidates: list[str], *, field: str, what: str) -> str:
    """Exact, then case-insensitive, then a near-miss suggestion."""
    value = (value or "").strip()
    if not value:
        raise InvalidFilter(f"Empty {what}.", field=field)
    if value in candidates:
        return value
    folded = {c.casefold(): c for c in candidates}
    if value.casefold() in folded:
        return folded[value.casefold()]
    near = difflib.get_close_matches(value, candidates, n=4, cutoff=0.6)
    if not near:
        near = [c for c in candidates if value.casefold() in c.casefold()][:4]
    hint = f" Did you mean: {', '.join(near)}?" if near else ""
    raise InvalidFilter(f"'{value}' is not a valid {what}.{hint}", field=field)


def resolve_company_size(value: Optional[Any]) -> Optional[int]:
    """Accept ``small`` / ``medium`` / ``large`` (or the raw code) -> band code.

    The codes are NOT ordinal: 1 is small, 2 is **large**, 3 is **medium**.
    """
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        if value in C.COMPANY_SIZE_NAMES:
            return value
        raise InvalidFilter(
            f"company_size code {value} is not valid. Use small / medium / large.",
            field="company_size",
        )
    key = str(value).strip().casefold()
    if key in C.COMPANY_SIZE:
        return C.COMPANY_SIZE[key]
    raise InvalidFilter(
        f"'{value}' is not a company size. Use one of: small, medium, large, any.",
        field="company_size",
    )


def resolve_job_type(value: Optional[Any]) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        if value in C.JOB_TYPE_NAMES:
            return value
        raise InvalidFilter(f"job_type code {value} is not valid.", field="job_type")
    key = str(value).strip().casefold().replace("-", "_").replace(" ", "_")
    if key in C.JOB_TYPE:
        return C.JOB_TYPE[key]
    raise InvalidFilter(
        f"'{value}' is not a job type. Use one of: full_time, internship, any.", field="job_type"
    )
