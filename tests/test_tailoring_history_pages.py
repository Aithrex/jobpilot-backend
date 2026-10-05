from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import pytest

from test_tracker_list_view import USER, _seed, _token, client, db  # noqa: F401


def page(client, **params):
    return client.test_client().get("/api/resume/tailoring-history?" + urlencode(params),
        headers={"Authorization": "Bearer " + _token(client)})


def all_pages(client, **params):
    seen = []
    for _ in range(20):
        response = page(client, **params)
        assert response.status_code == 200, response.get_json()
        data = response.get_json()
        seen.extend(row["record_id"] for row in data["records"])
        if not data["next_cursor"]:
            return seen
        params["cursor"] = data["next_cursor"]
    pytest.fail("Pagination did not terminate")


def test_bounded_page_and_summary_are_owner_scoped(client, db):
    for i in range(6):
        _seed(db, f"r{i}", versions=[{"version_id": "v", "ats_scores": {"overall": 80, "report": "x" * 10000},
                                    "tailored_resume": {"summary": "private body"},
                                    "files": {"pdf": {"s3_key": "mine.pdf", "layout": "x" * 10000}}}])
    _seed(db, "other", user="other@example.com")
    data = page(client, page_size=2).get_json()
    assert len(data["records"]) == 2
    assert data["total"] == data["summary"]["total"] == 6
    assert data["next_cursor"]
    row = data["records"][0]
    assert "tailored_resume" not in row and "jd_text" not in row
    assert row["versions"][0]["ats_scores"] == {"overall": 80}
    assert row["versions"][0]["files"]["pdf"] == {"s3_key": "mine.pdf"}
    assert all_pages(client, page_size=2) == ["r5", "r4", "r3", "r2", "r1", "r0"]


def test_search_finds_records_beyond_first_page_and_treats_regex_literally(client, db):
    for i in range(5):
        _seed(db, f"r{i}")
    db.tailoring_records.update_one({"record_id": "r0"}, {"$set": {"jd_analysis.company": "A+B", "jd_analysis.required_skills": ["Rust"]}})
    assert all_pages(client, page_size=1, q="rust") == ["r0"]
    assert all_pages(client, q="A+B") == ["r0"]
    assert all_pages(client, q=".*") == []
    assert all_pages(client, company="a+b", role="engine") == ["r0"]


def test_combined_filters_dates_and_status(client, db):
    _seed(db, "match", created_at=datetime(2026, 5, 2, 23, 59), ats_overall=90,
          application={"status": "offer"}, versions=[{"version_id": "1"}, {"version_id": "2"}])
    _seed(db, "wrong-status", created_at=datetime(2026, 5, 2), ats_overall=99)
    _seed(db, "next-day", created_at=datetime(2026, 5, 3), application={"status": "offer"})
    assert all_pages(client, date_from="2026-05-02", date_to="2026-05-02", status="offer",
                     ats_min=85, multi_version="true") == ["match"]
    _seed(db, "draft", application={})
    assert all_pages(client, status="draft") == ["draft"]


@pytest.mark.parametrize("sort,expected", [
    ("newest", ["high2", "high1", "zero", "null", "missing"]),
    ("oldest", ["missing", "null", "zero", "high1", "high2"]),
    ("ats", ["high2", "high1", "zero", "null", "missing"]),
])
def test_stable_cursor_sort_with_ties_and_unscored_rows(client, db, sort, expected):
    for i, (name, score) in enumerate((("missing", None), ("null", None), ("zero", 0), ("high1", 90), ("high2", 90))):
        _seed(db, name, ats_overall=score, created_at=datetime(2026, 1, 1) + timedelta(days=min(i, 3)))
    db.tailoring_records.update_one({"record_id": "missing"}, {"$unset": {"ats_scores": "", "created_at": ""}})
    assert all_pages(client, sort=sort, page_size=1) == expected


def test_cursor_survives_newer_insert_without_duplicates(client, db):
    for i in range(4):
        _seed(db, f"r{i}")
    first = page(client, page_size=2).get_json()
    _seed(db, "new", created_at=datetime(2026, 2, 1))
    assert all_pages(client, page_size=2, cursor=first["next_cursor"]) == ["r1", "r0"]


def test_cursors_reject_tampering_and_changes_in_owner_or_filters(client, db):
    _seed(db, "r1")
    _seed(db, "r2")
    cursor = page(client, page_size=1).get_json()["next_cursor"]
    assert page(client, cursor=cursor + "bad").status_code == 400
    assert page(client, cursor=cursor, q="changed").status_code == 400
    response = client.test_client().get("/api/resume/tailoring-history?" + urlencode({"cursor": cursor}),
        headers={"Authorization": "Bearer " + _token(client, "other@example.com")})
    assert response.status_code == 400


def test_presets_use_entire_history_and_correct_score_and_due_filters(client, db):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    _seed(db, "low", ats_overall=0, created_at=now, application={"next_action_date": "2020-01-01"})
    _seed(db, "unscored", ats_overall=None, created_at=now)
    _seed(db, "high", ats_overall=99, created_at=now)
    _seed(db, "future", created_at=datetime(2026, 1, 1), application={"next_action_date": now + timedelta(days=10)})
    data = page(client, page_size=1).get_json()
    assert data["summary"]["presets"]["needsWork"] == 1
    assert data["summary"]["presets"]["pending"] == 1
    assert all_pages(client, preset="needsWork") == ["low"]
    assert all_pages(client, preset="pending") == ["low"]


def test_byte_limited_cursor_continues_from_last_returned_record(client, db):
    for i in range(3):
        _seed(db, f"r{i}", versions=[{"version_id": "v", "user_feedback": "x" * 1_100_000}])
    first = page(client).get_json()
    assert len(first["records"]) == 1
    assert all_pages(client) == ["r2", "r1", "r0"]


@pytest.mark.parametrize("params", [
    {"page_size": 0}, {"page_size": 101}, {"page_size": "bad"}, {"cursor": "bad"},
    {"sort": "bad"}, {"status": "bad"}, {"preset": "bad"}, {"multi_version": "bad"},
    {"ats_min": -1}, {"ats_min": "nan"}, {"date_to": "2026-02-30"},
    {"date_from": "2026-02-01", "date_to": "2026-01-01"}, {"q": "x" * 201},
])
def test_invalid_parameters(client, db, params):
    assert page(client, **params).status_code == 400
