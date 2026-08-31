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
  * the default (non-``view``) response is untouched, because interview prep and
    the tailored-resumes page read the full shape off this same endpoint;
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

    def test_the_default_response_is_unchanged(self, client, db):
        # Interview prep and the tailored-resumes page read the full shape off
        # this same endpoint, so the slim path has to stay strictly opt-in.
        _seed(db, "rec-1", versions=[{"version_id": "v-1"}])

        row = _list(client, _token(client)).get_json()["records"][0]

        assert "versions" in row
        assert row["jd_analysis"]["required_skills"]
        assert "version_count" not in row


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
