"""shape.py -- the pure functions that throw bytes away without losing meaning.

No transport is involved anywhere in this file; every input is either a golden
fixture or a small literal.
"""

from __future__ import annotations

from conftest import fixture_json
from instahyre_server import shape
from instahyre_server.shape import (
    collapse_duplicates,
    dedupe,
    shape_detail,
    shape_meta,
    shape_search_record,
    size_band,
    strip_html,
    truncate,
)


# ---------------------------------------------------------------------------
# strip_html
# ---------------------------------------------------------------------------


def test_strip_html_on_a_real_description_leaves_no_markup(detail_direct):
    raw = detail_direct["description"]
    assert raw.startswith("<html><body>"), "the fixture must really be an HTML document"

    text = strip_html(raw)

    assert "<" not in text and ">" not in text, "no tag survived"
    assert "<p>" not in text and "<li>" not in text
    assert "html" not in text.split("\n")[0].lower()
    assert text.startswith("We are seeking a Java Engineer")


def test_strip_html_preserves_bullet_structure(detail_direct):
    text = strip_html(detail_direct["description"])
    bullets = [line for line in text.split("\n") if line.startswith("- ")]
    assert len(bullets) == 14, "every <li> became a bullet line"
    assert any("Spring Boot" in line for line in bullets)
    assert "Responsibilities:" in text, "headings survive as their own lines"


def test_strip_html_unescapes_entities():
    """The fixture descriptions happen to carry no entities, so this branch is
    exercised directly rather than pretending the fixture proves it."""
    assert strip_html("<p>R&amp;D &lt;team&gt; &quot;core&quot; &#39;now&#39;</p>") == (
        "R&D <team> \"core\" 'now'"
    )
    assert strip_html("<p>a&nbsp;b</p>") == "a b", "nbsp becomes a plain space"


def test_strip_html_collapses_whitespace_but_keeps_paragraphs():
    text = strip_html("<p>one</p><p><br /></p><p><br /></p><p>two   three</p>")
    assert text == "one\n\ntwo three"


def test_strip_html_on_empty_input():
    assert strip_html(None) == ""
    assert strip_html("") == ""


def test_strip_html_turns_breaks_into_newlines():
    assert strip_html("a<br>b<br/>c") == "a\nb\nc"


# ---------------------------------------------------------------------------
# truncate
# ---------------------------------------------------------------------------


def test_truncate_cuts_at_a_word_boundary_and_reports_it():
    text = "alpha beta gamma delta epsilon zeta eta theta"
    cut, was_truncated = truncate(text, 20)

    assert was_truncated is True
    assert cut.endswith(" ...")
    body = cut[: -len(" ...")]
    assert text.startswith(body), "the kept prefix is verbatim"
    assert not body.endswith(" ")
    assert body.split(" ")[-1] in text.split(" "), "no word was cut in half"
    assert body == "alpha beta gamma"


def test_truncate_leaves_short_text_alone():
    assert truncate("short", 100) == ("short", False)


def test_truncate_with_no_limit_is_a_passthrough():
    text = "x" * 5000
    assert truncate(text, None) == (text, False)
    assert truncate(text, 0) == (text, False)


def test_truncate_falls_back_to_a_hard_cut_when_there_is_no_late_space():
    """A single long token has no word boundary to respect."""
    cut, was_truncated = truncate("a" * 100, 10)
    assert was_truncated is True
    assert cut == "a" * 10 + " ..."


def test_truncate_on_a_real_description_is_shorter_than_the_source(detail_direct):
    full = strip_html(detail_direct["description"])
    cut, was_truncated = truncate(full, shape.DEFAULT_DESCRIPTION_CHARS)
    assert was_truncated is True
    assert len(cut) < len(full)


# ---------------------------------------------------------------------------
# size_band -- the non-ordinal bucket map
# ---------------------------------------------------------------------------


def test_size_band_maps_every_observed_bucket():
    assert size_band(1) == "small"
    assert size_band(10) == "small"
    assert size_band(50) == "medium"
    assert size_band(200) == "large"
    assert size_band(500) == "large"
    assert size_band(1000) == "large"


def test_size_band_on_none_is_none():
    assert size_band(None) is None


def test_size_band_on_an_unknown_bucket_is_none_not_a_guess():
    assert size_band(7) is None


def test_size_band_covers_every_bucket_the_constants_declare():
    from instahyre_server import constants as C

    for bucket in C.EMPLOYEE_COUNT_BUCKETS:
        assert size_band(bucket) in {"small", "medium", "large"}


# ---------------------------------------------------------------------------
# shape_search_record
# ---------------------------------------------------------------------------


def _object_by_id(payload: dict, job_id: int) -> dict:
    for obj in payload["objects"]:
        if obj["id"] == job_id:
            return obj
    raise AssertionError("fixture no longer contains job id %s" % job_id)


def test_shape_search_record_emits_the_compact_keys(search_payload):
    obj = _object_by_id(search_payload, 432558)
    record = shape_search_record(obj)

    assert record["id"] == 432558
    assert record["title"] == "Java Engineer"
    assert record["company"] == "Ridgeline Analytics"
    assert record["locations"] == ["Bangalore"]
    assert record["skills"] == ["Data Structures", "Java", "Spring"]
    assert record["company_size"] == "large"
    assert record["founded"] == 2015
    assert record["company_id"] == 20108
    assert record["url"].startswith("https://www.instahyre.com/job-432558")


def test_shape_search_record_is_much_smaller_than_the_source(search_payload):
    import json

    obj = _object_by_id(search_payload, 432558)
    assert len(json.dumps(shape_search_record(obj))) < len(json.dumps(obj)) / 2


def test_shape_search_record_splits_a_comma_joined_location_string(search_payload):
    obj = _object_by_id(search_payload, 423245)
    assert obj["locations"] == "Bangalore,Chennai,Hyderabad", "fixture drift"
    assert shape_search_record(obj)["locations"] == ["Bangalore", "Chennai", "Hyderabad"]


def test_shape_search_record_caps_skills_at_eight_and_counts_the_rest(search_payload):
    obj = _object_by_id(search_payload, 438765)
    assert len(obj["keywords"]) == 17, "fixture drift"

    record = shape_search_record(obj)

    assert len(record["skills"]) == shape.MAX_SKILLS_IN_LIST == 8
    assert record["skills"] == obj["keywords"][:8]
    assert record["skills_more"] == 9


def test_shape_search_record_omits_skills_more_when_under_the_cap(search_payload):
    record = shape_search_record(_object_by_id(search_payload, 432558))
    assert "skills_more" not in record


def test_shape_search_record_respects_a_custom_cap(search_payload):
    record = shape_search_record(_object_by_id(search_payload, 438765), max_skills=50)
    assert len(record["skills"]) == 17
    assert "skills_more" not in record


def test_authenticated_only_fields_are_absent_when_null(search_payload):
    """Anonymous callers get nulls; a null must not become a key."""
    obj = _object_by_id(search_payload, 432558)
    assert obj["score"] is None
    assert obj["is_strong_match"] is None
    assert obj["reviewed_at"] is None
    assert obj["interview_status"] is None

    record = shape_search_record(obj)

    for key in ("match_score", "strong_match", "reviewed_at", "interview_status"):
        assert key not in record, "%s leaked into an anonymous record" % key


def test_authenticated_only_fields_are_present_when_populated(search_payload):
    """Present == the session is live, which is the whole signal."""
    obj = dict(_object_by_id(search_payload, 432558))
    obj.update(
        {
            "score": 88,
            "is_strong_match": True,
            "reviewed_at": "2026-08-19T04:11:02",
            "interview_status": 2,
        }
    )

    record = shape_search_record(obj)

    assert record["match_score"] == 88
    assert record["strong_match"] is True
    assert record["reviewed_at"] == "2026-08-19T04:11:02"
    assert record["interview_status"] == 2


def test_every_object_in_the_fixture_shapes_without_error(search_payload):
    records = [shape_search_record(o) for o in search_payload["objects"]]
    assert len(records) == 35
    assert all(r["id"] for r in records)
    assert all(r["company"] for r in records)


# ---------------------------------------------------------------------------
# shape_detail
# ---------------------------------------------------------------------------


def test_shape_detail_flags_an_agency_posting(detail_agency):
    assert "agency_function_names" in detail_agency, "fixture drift"

    record = shape_detail(detail_agency)

    assert record["posted_by_agency"] is True
    assert record["agency_name"] == "Recro"
    assert record["company"] == "Thena", "company is the hiring company, not the agency"


def test_shape_detail_flags_a_direct_posting(detail_direct):
    assert "agency_function_names" not in detail_direct, "fixture drift"

    record = shape_detail(detail_direct)

    assert record["posted_by_agency"] is False
    assert "agency_name" not in record
    assert record["company"] == "Ridgeline Analytics"


def test_shape_detail_always_reports_salary_as_none_with_a_note(detail_direct, detail_agency):
    for raw in (detail_direct, detail_agency):
        record = shape_detail(raw)
        assert record["salary"] is None
        assert "salary" in record, "the key must exist so nobody goes hunting for it"
        assert "no salary data" in record["salary_note"]


def test_shape_detail_derives_the_experience_band(detail_direct):
    assert shape_detail(detail_direct)["experience_years"] == "3-6"


def test_shape_detail_carries_the_recruiter_block(detail_agency):
    assert shape_detail(detail_agency)["recruiter"] == {
        "name": "Neha Gupta",
        "designation": "TA Executive",
        "company": "Recro",
    }


def test_shape_detail_truncates_the_description_and_says_so(detail_direct):
    record = shape_detail(detail_direct, description_chars=200)
    assert record["description_truncated"] is True
    assert record["description_full_chars"] > 200
    assert len(record["description"]) <= 210


def test_shape_detail_with_no_limit_keeps_the_whole_description(detail_direct):
    record = shape_detail(detail_direct, description_chars=None)
    assert "description_truncated" not in record
    assert record["description"] == strip_html(detail_direct["description"])


def test_shape_detail_uses_the_agency_function_names_when_present(detail_agency):
    assert shape_detail(detail_agency)["job_functions"] == [
        "Backend Development",
        "Frontend Development",
        "Full-Stack Development",
    ]


def test_shape_detail_makes_the_url_absolute(detail_direct):
    assert detail_direct["opportunity_url"].startswith("/"), "fixture drift"
    assert shape_detail(detail_direct)["url"].startswith("https://www.instahyre.com/job-")


# ---------------------------------------------------------------------------
# dedupe / collapse_duplicates
# ---------------------------------------------------------------------------


def test_dedupe_drops_repeat_ids_and_reports_the_count():
    records = [{"id": 1}, {"id": 2}, {"id": 1}, {"id": 3}, {"id": 2}]
    out, dropped = dedupe(records)
    assert [r["id"] for r in out] == [1, 2, 3]
    assert dropped == 2


def test_dedupe_preserves_first_occurrence_order():
    out, dropped = dedupe([{"id": 9, "t": "first"}, {"id": 9, "t": "second"}])
    assert out == [{"id": 9, "t": "first"}]
    assert dropped == 1


def test_dedupe_on_a_clean_page_drops_nothing(search_payload):
    records = [shape_search_record(o) for o in search_payload["objects"]]
    out, dropped = dedupe(records)
    assert dropped == 0
    assert len(out) == 35


def test_collapse_duplicates_flags_one_role_listed_under_several_ids(search_payload):
    """Real drift: the golden page carries one company/title under two ids."""
    records = [shape_search_record(o) for o in search_payload["objects"]]
    collapse_duplicates(records)

    flagged = [r for r in records if "duplicate_ids" in r]
    assert len(flagged) == 2
    assert {r["company"] for r in flagged} == {"Amazon"}
    assert {r["title"] for r in flagged} == {"Lead Engineer"}
    ids = sorted(r["id"] for r in flagged)
    assert flagged[0]["duplicate_ids"] == [r["id"] for r in flagged if r["id"] != flagged[0]["id"]]
    assert ids == [439584, 439675]


def test_collapse_duplicates_leaves_unique_records_untouched():
    records = [
        {"id": 1, "company": "A", "title": "X"},
        {"id": 2, "company": "B", "title": "X"},
    ]
    collapse_duplicates(records)
    assert all("duplicate_ids" not in r for r in records)


def test_collapse_duplicates_never_lists_a_record_as_its_own_duplicate():
    records = [
        {"id": 1, "company": "A", "title": "X"},
        {"id": 2, "company": "A", "title": "X"},
        {"id": 3, "company": "A", "title": "X"},
    ]
    collapse_duplicates(records)
    assert records[0]["duplicate_ids"] == [2, 3]
    assert records[1]["duplicate_ids"] == [1, 3]
    assert records[2]["duplicate_ids"] == [1, 2]


# ---------------------------------------------------------------------------
# shape_meta
# ---------------------------------------------------------------------------


def test_shape_meta_carries_the_headline_counts(search_payload):
    stats = shape_meta(search_payload["meta"])
    assert stats["total_count"] == 1086
    assert stats["returned"] == 35
    assert stats["offset"] == 0
    assert stats["by_company_size"] == {"small": 360, "medium": 197, "large": 529}
    assert stats["by_job_type"] == {"full_time": 1086, "internship": 0}


def test_shape_meta_resolves_industry_facet_ids_to_names(search_payload):
    names = {row["id"]: row["name"] for row in fixture_json("industry_types.json")["objects"]}
    stats = shape_meta(search_payload["meta"], industry_names=names)

    top = stats["top_industries"]
    assert top[0]["id"] == 13
    assert top[0]["name"] == "Computer Software / IT / Internet"
    assert top[0]["count"] == 735
    assert all(item["name"] is not None for item in top)


def test_shape_meta_says_none_rather_than_guessing_an_industry_name(search_payload):
    stats = shape_meta(search_payload["meta"])
    assert [item["name"] for item in stats["top_industries"]] == [None, None, None, None]
    assert [item["id"] for item in stats["top_industries"]] == [13, 9, 29, 41]


def test_shape_meta_renames_the_location_facet_key(search_payload):
    stats = shape_meta(search_payload["meta"])
    assert stats["top_locations"][0] == {"name": "Bangalore", "count": 1086}


def test_shape_meta_warns_that_experience_bands_overlap(search_payload):
    stats = shape_meta(search_payload["meta"])
    assert "by_experience_level" in stats
    assert "overlap" in stats["experience_levels_note"]
    assert sum(stats["by_experience_level"].values()) > stats["total_count"]


def test_shape_meta_always_warns_about_truncated_facets(search_payload, empty_payload):
    for meta in (search_payload["meta"], empty_payload["meta"]):
        assert "4 entries" in shape_meta(meta)["facets_note"]


def test_shape_meta_on_an_empty_result_omits_empty_facets(empty_payload):
    stats = shape_meta(empty_payload["meta"])
    assert stats["total_count"] == 0
    assert "top_companies" not in stats
    assert "top_locations" not in stats
