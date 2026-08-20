"""taxonomy.py -- the resolvers that keep a filter from failing on a capital letter.

Instahyre validates ``jobLocations`` server-side and is case-sensitive, so
``bangalore`` is an HTTP 400 and ``Bangalore`` is 7,000+ jobs. Every test here
mocks the three taxonomy endpoints from the golden fixtures; nothing reaches
the network.
"""

from __future__ import annotations

import pytest

from conftest import fixture_json, make_http, taxonomy_routes
from instahyre_server import constants as C
from instahyre_server.cache import Store
from instahyre_server.errors import InvalidFilter
from instahyre_server.taxonomy import Taxonomy, resolve_company_size, resolve_job_type


@pytest.fixture
def taxonomy():
    """A Taxonomy over mocked endpoints, with its transport recorder attached."""
    http = make_http(taxonomy_routes())
    tax = Taxonomy(http, Store(":memory:"))
    tax.routes = http.routes
    return tax


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def test_job_functions_loads_all_58_with_category_and_tech_flag(taxonomy):
    rows = taxonomy.job_functions()
    assert len(rows) == 58
    backend = next(r for r in rows if r["name"] == "Backend Development")
    assert backend["id"] == 10
    assert backend["category"] == "Software Engineering"
    assert backend["is_tech"] is True
    assert backend["slug"] == "backend-development"


def test_industries_loads_all_74(taxonomy):
    rows = taxonomy.industries()
    assert len(rows) == 74
    assert {"id", "name"} == set(rows[0])


def test_locations_loads_the_accepted_tokens(taxonomy):
    rows = taxonomy.locations()
    assert len(rows) == 308
    values = [r["value"] for r in rows]
    assert "Bangalore" in values
    assert "Work From Home" in values
    remote = next(r for r in rows if r["value"] == "Work From Home")
    assert remote["group"] == "remote"


def test_industry_names_is_an_id_to_name_map(taxonomy):
    names = taxonomy.industry_names()
    assert names[13] == "Computer Software / IT / Internet"
    assert len(names) == 74


# ---------------------------------------------------------------------------
# resolve_location -- the trap the resolver exists for
# ---------------------------------------------------------------------------


def test_resolve_location_corrects_the_case(taxonomy):
    """'bangalore' is a hard 400 on the wire; the resolver fixes it first."""
    assert taxonomy.resolve_location("bangalore") == "Bangalore"


def test_resolve_location_corrects_shouting_and_padding(taxonomy):
    assert taxonomy.resolve_location("  BANGALORE  ") == "Bangalore"


def test_resolve_location_passes_an_exact_token_through(taxonomy):
    assert taxonomy.resolve_location("Work From Home") == "Work From Home"


def test_resolve_location_on_a_misspelling_suggests_the_right_one(taxonomy):
    with pytest.raises(InvalidFilter) as excinfo:
        taxonomy.resolve_location("Banglore")

    error = excinfo.value
    assert "Did you mean" in error.message
    assert "Bangalore" in error.message
    assert error.field == "jobLocations"


def test_resolve_location_on_pure_nonsense_still_raises(taxonomy):
    with pytest.raises(InvalidFilter) as excinfo:
        taxonomy.resolve_location("Zzzqqxx")
    assert "not a valid location" in excinfo.value.message


def test_resolve_location_rejects_an_empty_value(taxonomy):
    with pytest.raises(InvalidFilter):
        taxonomy.resolve_location("   ")


def test_resolve_locations_maps_a_whole_list(taxonomy):
    assert taxonomy.resolve_locations(["bangalore", "pune"]) == ["Bangalore", "Pune"]


def test_resolve_locations_accepts_a_bare_string(taxonomy):
    assert taxonomy.resolve_locations("bangalore") == ["Bangalore"]


def test_resolve_locations_on_none_is_none(taxonomy):
    assert taxonomy.resolve_locations(None) is None


# ---------------------------------------------------------------------------
# resolve_job_function
# ---------------------------------------------------------------------------


def test_resolve_job_function_by_exact_name(taxonomy):
    assert taxonomy.resolve_job_function("Backend Development") == 10


def test_resolve_job_function_is_case_insensitive(taxonomy):
    assert taxonomy.resolve_job_function("backend development") == 10


def test_resolve_job_function_accepts_a_known_id(taxonomy):
    assert taxonomy.resolve_job_function(10) == 10
    assert taxonomy.resolve_job_function("10") == 10


def test_resolve_job_function_on_an_unknown_name_raises(taxonomy):
    with pytest.raises(InvalidFilter) as excinfo:
        taxonomy.resolve_job_function("Underwater Basket Weaving")
    assert excinfo.value.field == "job_functions"


def test_resolve_job_function_on_an_unknown_id_raises(taxonomy):
    with pytest.raises(InvalidFilter) as excinfo:
        taxonomy.resolve_job_function(99999)
    assert "99999" in excinfo.value.message
    assert "instahyre_list_job_functions" in excinfo.value.message


def test_resolve_job_functions_maps_a_list(taxonomy):
    assert taxonomy.resolve_job_functions(["Backend Development", 10]) == [10, 10]


def test_resolve_industry_by_name_and_id(taxonomy):
    assert taxonomy.resolve_industry("Computer Software / IT / Internet") == 13
    assert taxonomy.resolve_industry(13) == 13
    with pytest.raises(InvalidFilter) as excinfo:
        taxonomy.resolve_industry("Nonexistent Industry Sector")
    assert excinfo.value.field == "industry_types"


# ---------------------------------------------------------------------------
# The non-ordinal company_size codes
# ---------------------------------------------------------------------------


def test_resolve_company_size_uses_the_non_ordinal_codes():
    """2 is LARGE and 3 is MEDIUM. Asserting 2 == medium would assert the bug.

    Verified live by exact integer identity: each band's total_count matches
    exactly one value in meta.company_size_count, and the three sum to the
    unfiltered corpus total.
    """
    assert resolve_company_size("small") == 1
    assert resolve_company_size("large") == 2
    assert resolve_company_size("medium") == 3
    assert resolve_company_size("any") == 0


def test_company_size_names_round_trip_the_codes():
    for name, code in C.COMPANY_SIZE.items():
        assert C.COMPANY_SIZE_NAMES[code] == name


def test_resolve_company_size_is_case_insensitive():
    assert resolve_company_size("LARGE") == 2
    assert resolve_company_size(" Medium ") == 3


def test_resolve_company_size_accepts_a_raw_code():
    assert resolve_company_size(2) == 2


def test_resolve_company_size_on_none_is_none():
    assert resolve_company_size(None) is None


def test_resolve_company_size_rejects_an_unknown_name():
    with pytest.raises(InvalidFilter) as excinfo:
        resolve_company_size("enormous")
    assert excinfo.value.field == "company_size"


def test_resolve_company_size_rejects_an_out_of_range_code():
    with pytest.raises(InvalidFilter):
        resolve_company_size(9)


# ---------------------------------------------------------------------------
# resolve_job_type
# ---------------------------------------------------------------------------


def test_resolve_job_type_codes():
    assert resolve_job_type("full_time") == 1
    assert resolve_job_type("internship") == 2
    assert resolve_job_type("any") == 0


def test_resolve_job_type_normalises_separators():
    assert resolve_job_type("full-time") == 1
    assert resolve_job_type("Full Time") == 1


def test_resolve_job_type_on_none_is_none():
    assert resolve_job_type(None) is None


def test_resolve_job_type_rejects_nonsense():
    with pytest.raises(InvalidFilter) as excinfo:
        resolve_job_type("gig")
    assert excinfo.value.field == "job_type"


def test_resolve_job_type_rejects_an_out_of_range_code():
    with pytest.raises(InvalidFilter):
        resolve_job_type(7)


# ---------------------------------------------------------------------------
# Caching -- taxonomies are 30-day cached, so a second call is free
# ---------------------------------------------------------------------------


def test_locations_are_fetched_once_and_then_cached(taxonomy):
    taxonomy.locations()
    taxonomy.locations()
    taxonomy.locations()
    assert taxonomy.routes.count(C.EP_LOCATION_DATA) == 1


def test_job_functions_are_fetched_once_and_then_cached(taxonomy):
    first = taxonomy.job_functions()
    second = taxonomy.job_functions()
    assert first == second
    assert taxonomy.routes.count(C.EP_JOB_FUNCTION) == 1


def test_industries_are_fetched_once_and_then_cached(taxonomy):
    taxonomy.industries()
    taxonomy.industry_names()
    assert taxonomy.routes.count(C.EP_INDUSTRY_TYPE) == 1


def test_repeated_resolution_costs_no_extra_requests(taxonomy):
    for _ in range(5):
        taxonomy.resolve_location("bangalore")
        taxonomy.resolve_job_function("Backend Development")
    assert taxonomy.routes.count() == 2, "one location fetch, one job-function fetch"


def test_a_cache_hit_survives_a_new_taxonomy_over_the_same_store():
    """The cache lives in the Store, not in the instance."""
    store = Store(":memory:")
    http = make_http(taxonomy_routes())
    Taxonomy(http, store).locations()
    Taxonomy(http, store).locations()
    assert http.routes.count(C.EP_LOCATION_DATA) == 1


def test_taxonomy_cache_uses_the_thirty_day_ttl(taxonomy):
    taxonomy.locations()
    assert C.TTL_TAXONOMY == 30 * 24 * 3600
    assert taxonomy._store.get("taxonomy", "locations") is not None


def test_fixture_endpoints_are_the_ones_the_constants_name():
    """If an endpoint constant moves, these tests must stop passing quietly."""
    routes = taxonomy_routes()
    assert set(routes) == {C.EP_JOB_FUNCTION, C.EP_INDUSTRY_TYPE, C.EP_LOCATION_DATA}
    assert fixture_json("job_functions.json")["meta"]["total_count"] == 58
