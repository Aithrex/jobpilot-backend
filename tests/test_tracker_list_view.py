"""The tracker's slim list payload: ``GET /tailoring-records?view=list``.

The tracker renders about eight fields per row, but the default response hands
back whole documents — job description text, every stored version — which the
serializer then partly throws away. At twenty records that is invisible; at the
five hundred a fortnight of thirty-a-day applying produces, it is the page load.

``view=list`` projects in Mongo instead, so the heavy fields are never read.
Covered here:
  * the slim row carries what a row draws and nothing else;
  * ``version_count`` and ``has_tailored_resume`` are computed server-side, so
    the client can still tell a base-resume application from a tailored one
    without the arrays behind them;
  * datetimes still arrive as ISO strings with an explicit UTC offset;
  * the default (non-``view``) response retains version and job metadata for
    interview prep and tailored resumes, with assessment details on demand;
  * the stale-application scan still finds and still skips the right records now
    that it runs against a projection and a status filter.

Runs fully offline (mongomock via conftest/_vg_support).
"""
from datetime import datetime, timedelta, timezone

import pytest

from _vg_support import current_db, load_service_app

USER = "tracker-list@aspirely.me"


@pytest.fixture()
def client():
    return load_service_app("resume")


@pytest.fixture()
def db():
    d = current_db()
    d.tailoring_records.delete_many({})
    return d


def _token(flask_app, identity=USER):
    from flask_jwt_extended import create_access_token
    with flask_app.app_context():
        return create_access_token(identity=identity)


def _seed(db, record_id, *, user=USER, application=None, versions=None, tailored=None,
          created_at=None, ats_overall=88):
    db.tailoring_records.insert_one({
        "record_id": record_id,
        "user_email": user,
        "source": "tailor",
        "source_job_id": "job-1",
        "current_version_id": "v-1",
        "base_resume_filename": "base.pdf",
        "base_resume_s3_key": "s3://bucket/base.pdf",
        # The two fields this whole change exists to stop reading.
        "jd_text": "A very long job description. " * 200,
        "jd_analysis": {
            "company": "Acme",
            "job_title": "Engineer",
            "required_skills": ["python"] * 40,
            "keywords": ["kubernetes"] * 40,
        },
        "ats_scores": {
            "overall": ats_overall,
            "missing_keywords": ["terraform"] * 30,
            "suggestions": ["Add more numbers."] * 10,
        },
        "versions": versions if versions is not None else [],
        "tailored_resume": tailored,
        "application": application if application is not None else {"status": "applied"},
        "created_at": created_at or datetime(2026, 1, 1),
        "updated_at": datetime(2026, 1, 2),
    })


def _list(client, token, query=""):
    return client.test_client().get(
        f"/api/resume/tailoring-records{query}",
        headers={"Authorization": f"Bearer {token}"},
    )


class TestSlimRows:
    def test_slim_rows_page_without_losing_equal_timestamp_records(self, client, db):
        for i in range(5):
            _seed(db, f"rec-{i}")
        _seed(db, "not-mine", user="another@example.test")
        token = _token(client)
        seen = []
        offset = 0
        while offset is not None:
            response = _list(client, token, f"?view=list&page_size=2&offset={offset}")
            assert response.status_code == 200
            data = response.get_json()
            assert data["total"] == 5
            assert len(data["records"]) <= 2
            seen.extend(row["record_id"] for row in data["records"])
            offset = data["next_offset"]
        assert seen == [f"rec-{i}" for i in range(4, -1, -1)]

    def test_invalid_slim_pagination_returns_400(self, client, db):
        response = _list(client, _token(client), "?view=list&page_size=bad")
        assert response.status_code == 400

    def test_row_carries_what_a_row_draws_and_drops_the_rest(self, client, db):
        _seed(db, "rec-1")
        resp = _list(client, _token(client), "?view=list")
        assert resp.status_code == 200

        row = resp.get_json()["records"][0]
        assert row["record_id"] == "rec-1"
        assert row["jd_analysis"]["job_title"] == "Engineer"
        assert row["jd_analysis"]["company"] == "Acme"
        assert row["ats_scores"]["overall"] == 88
        assert row["application"]["status"] == "applied"

        # The payload this change is about.
        assert "jd_text" not in row
        assert "versions" not in row
        assert "tailored_resume" not in row
        # And the parts of jd_analysis/ats_scores a row never shows.
        assert "required_skills" not in row["jd_analysis"]
        assert "missing_keywords" not in row["ats_scores"]

    def test_counts_replace_the_arrays_they_came_from(self, client, db):
        _seed(db, "rec-tailored",
              versions=[{"version_id": "v-1"}, {"version_id": "v-2"}],
              tailored={"summary": "..."})
        _seed(db, "rec-base", application={"status": "applied", "resume_used": "base"})

        rows = {r["record_id"]: r for r in _list(client, _token(client), "?view=list").get_json()["records"]}

        assert rows["rec-tailored"]["version_count"] == 2
        assert rows["rec-tailored"]["has_tailored_resume"] is True
        # A base-resume application is exactly the absence of both, and the
        # tracker's "Applied with base resume" marker reads these two fields.
        assert rows["rec-base"]["version_count"] == 0
        assert rows["rec-base"]["has_tailored_resume"] is False

    def test_datetimes_are_iso_with_an_explicit_offset(self, client, db):
        # Naive timestamps are stored throughout; without the +00:00 suffix a
        # browser parses them as local time and the civil day can shift.
        _seed(db, "rec-1", application={
            "status": "applied",
            "applied_at": datetime(2026, 3, 4, 19, 30),
            "next_action_date": datetime(2026, 3, 11, 9, 0),
        })

        row = _list(client, _token(client), "?view=list").get_json()["records"][0]

        assert row["created_at"].endswith("+00:00")
        assert row["application"]["applied_at"] == "2026-03-04T19:30:00+00:00"
        assert row["application"]["next_action_date"] == "2026-03-11T09:00:00+00:00"

    def test_other_users_records_are_not_listed(self, client, db):
        _seed(db, "mine")
        _seed(db, "theirs", user="someone-else@aspirely.me")

        rows = _list(client, _token(client), "?view=list").get_json()["records"]

        assert [r["record_id"] for r in rows] == ["mine"]

    def test_default_response_retains_version_and_job_metadata(self, client, db):
        # Interview prep and tailored resumes need version and job metadata,
        # so the tracker's version-free slim path stays strictly opt-in.
        _seed(db, "rec-1", versions=[{"version_id": "v-1"}])

        row = _list(client, _token(client)).get_json()["records"][0]

        assert "versions" in row
        assert row["jd_analysis"]["required_skills"]
        assert "version_count" not in row


class TestHistoryPagination:
    def test_existing_client_gets_small_summaries_and_details_remain_available(self, client, db):
        scores = {"overall": 88, "schema_version": 2, "details": "x" * 1_100_000}
        for i in range(7):
            _seed(db, f"rec-{i}", versions=[{
                "version_id": f"v-{i}", "version_number": 1, "source": "initial",
                "tailored_resume": {"summary": "Full resume"},
                "ats_scores": scores,
                "files": {"pdf": {"filename": "resume.pdf", "s3_key": "saved.pdf"}},
            }])
            db.tailoring_records.update_one({"record_id": f"rec-{i}"}, {
                "$set": {"ats_scores": scores},
            })
        token = _token(client)
        # The already-deployed browser sends no pagination parameters.
        resp = _list(client, token)
        assert resp.status_code == 200
        assert len(resp.data) < 100_000
        rows = resp.get_json()["records"]
        assert len(rows) == 7
        for row in rows:
            assert row["ats_scores"] == {"overall": 88, "schema_version": 2}
            assert row["versions"][0]["ats_scores"] == row["ats_scores"]
            assert row["versions"][0]["files"]["pdf"]["s3_key"] == "saved.pdf"
            assert row["jd_analysis"]["required_skills"]
        detail = client.test_client().get("/api/resume/tailoring-records/rec-0",
            headers={"Authorization": f"Bearer {token}"}).get_json()["record"]
        assert detail["ats_scores"] == scores
        assert detail["versions"][0]["ats_scores"] == scores
        assert detail["versions"][0]["tailored_resume"]["summary"] == "Full resume"
        assert db.tailoring_records.find_one({"record_id": "rec-0"})["ats_scores"] == scores

    @pytest.mark.parametrize("scores", [None, {}, {"schema_version": 4, "overall": None}])
    def test_summary_preserves_unknown_and_unscored_results(self, client, db, scores):
        _seed(db, "unscored", versions=[{"version_id": "v-1", "ats_scores": scores}])
        db.tailoring_records.update_one({"record_id": "unscored"}, {"$set": {"ats_scores": scores}})
        row = _list(client, _token(client)).get_json()["records"][0]
        assert row["ats_scores"] == scores
        assert row["versions"][0]["ats_scores"] == scores

    def test_all_pages_preserve_versions_and_owner_scope(self, client, db):
        for i in range(5):
            _seed(db, f"rec-{i}", versions=[{
                "version_id": f"v-{i}", "version_number": 1,
                "tailored_resume": {"summary": "Full resume"},
                "files": {"pdf": {"filename": "resume.pdf"}},
            }])
        _seed(db, "other", user="other@example.com")
        token = _token(client)
        offset = 0
        rows = []
        while offset is not None:
            resp = _list(client, token, f"?page_size=2&offset={offset}")
            assert resp.status_code == 200
            page = resp.get_json()
            rows.extend(page["records"])
            offset = page["next_offset"]
        assert [r["record_id"] for r in rows] == [f"rec-{i}" for i in range(4, -1, -1)]
        for row in rows:
            assert row["versions"][0]["files"]["pdf"]["filename"] == "resume.pdf"
            assert "tailored_resume" not in row["versions"][0]
        # Projection must not discard stored resume content.
        assert db.tailoring_records.find_one({"record_id": "rec-0"})["versions"][0]["tailored_resume"]

    def test_large_history_is_split_by_bytes_without_losing_records(self, client, db):
        for i in range(7):
            _seed(db, f"rec-{i}")
            db.tailoring_records.update_one({"record_id": f"rec-{i}"}, {
                "$set": {"ats_scores.details": "x" * 1_100_000},
            })
        token = _token(client)
        # Explicit full exports still need the byte limit: assessment reports
        # are intentionally preserved when the caller asks for full records.
        assert len(_list(client, token, "?include=full").data) > 6 * 1024 * 1024
        offset = 0
        seen = []
        while offset is not None:
            resp = _list(client, token, f"?include=full&page_size=50&offset={offset}")
            assert resp.status_code == 200
            assert len(resp.data) < 2 * 1024 * 1024
            page = resp.get_json()
            seen.extend(r["record_id"] for r in page["records"])
            offset = page["next_offset"]
        assert len(seen) == len(set(seen)) == 7

    def test_empty_history_and_offset_past_end_terminate(self, client, db):
        token = _token(client)
        for query in ("?page_size=50", "?page_size=50&offset=100"):
            resp = _list(client, token, query)
            assert resp.status_code == 200
            assert resp.get_json() == {"records": [], "next_offset": None}

    def test_single_oversized_record_returns_explicit_error(self, client, db):
        _seed(db, "oversized")
        db.tailoring_records.update_one({"record_id": "oversized"}, {
            "$set": {"ats_scores.details": "x" * (2 * 1024 * 1024)},
        })
        resp = _list(client, _token(client), "?include=full&page_size=50")
        assert resp.status_code == 413
        assert "too large" in resp.get_json()["error"]

    def test_full_page_preserves_resume_bodies_and_job_description(self, client, db):
        _seed(db, "full", versions=[{
            "version_id": "v-1", "tailored_resume": {"summary": "Full resume"},
        }])
        resp = _list(client, _token(client), "?page_size=50&include=full")
        assert resp.status_code == 200
        row = resp.get_json()["records"][0]
        assert row["versions"][0]["tailored_resume"]["summary"] == "Full resume"
        assert row["jd_text"]

    def test_legacy_versions_still_migrate_and_detail_keeps_content(self, client, db):
        _seed(db, "legacy", tailored={"summary": "Legacy resume"})
        db.tailoring_records.update_one({"record_id": "legacy"}, {"$unset": {"current_version_id": ""}})
        token = _token(client)
        row = _list(client, token, "?page_size=50").get_json()["records"][0]
        assert row["versions"][0]["version_id"] == row["current_version_id"]
        resp = client.test_client().get("/api/resume/tailoring-records/legacy",
            headers={"Authorization": f"Bearer {token}"})
        assert resp.get_json()["record"]["versions"][0]["tailored_resume"]["summary"] == "Legacy resume"

    @pytest.mark.parametrize("query", ["?page_size=bad", "?page_size=50&offset=bad"])
    def test_invalid_pagination(self, client, db, query):
        assert _list(client, _token(client), query).status_code == 400


class TestStaleScanProjection:
    """The nudge scan now runs on a filter plus a projection instead of every
    whole document. Its answers must not have moved."""

    def _stale(self):
        from services import job_intelligence_service as jis
        return jis.get_stale_applications(USER, stale_days=5)

    def test_an_old_applied_record_is_still_found(self, client, db):
        long_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=20)
        _seed(db, "rec-stale", application={"status": "applied", "updated_at": long_ago})

        result = self._stale()

        assert result["count"] == 1
        row = result["stale_applications"][0]
        assert row["record_id"] == "rec-stale"
        assert row["job_title"] == "Engineer"
        assert row["company"] == "Acme"
        assert row["days_stale"] >= 19

    def test_a_dismissed_nudge_stays_dismissed(self, client, db):
        long_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=20)
        _seed(db, "rec-dismissed", application={
            "status": "applied",
            "updated_at": long_ago,
            "followup_dismissed_at": long_ago + timedelta(days=1),
        })

        assert self._stale()["count"] == 0

    def test_recent_and_non_applied_records_are_skipped(self, client, db):
        recent = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
        long_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=20)
        _seed(db, "rec-recent", application={"status": "applied", "updated_at": recent})
        _seed(db, "rec-draft", application={"status": "draft", "updated_at": long_ago})
        _seed(db, "rec-no-app", application={})

        assert self._stale()["count"] == 0
