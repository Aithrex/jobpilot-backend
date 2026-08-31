"""The civil-day label on an application: ``application.applied_date``.

``applied_at`` is a UTC instant and answers *when*. ``applied_date`` is a
"YYYY-MM-DD" civil day and answers *which day it counts for* — a label that only
means something relative to a timezone. Deriving the second from the first at
read time is what made an 8pm-in-Phoenix application show up under the next day,
so the day is frozen once, in the user's zone, at write time.

Covered here:
  * the motivating case — an evening application must NOT roll into tomorrow;
  * back-dating: an explicit ``applied_at`` wins over the auto-stamp AND drives
    the day label, rather than the label silently reading "today";
  * the ``confirmation_pending`` generalization, including the legacy
    ``batch_confirmation_pending`` alias and the streak gate that reads it;
  * the backfill's timezone fallback chain and its idempotency.

Runs fully offline (mongomock via conftest/_vg_support).
"""
from datetime import datetime, timezone

import pytest

from _vg_support import current_db, load_service_app

USER = "applied-date@aspirely.me"
LA = "America/Los_Angeles"


@pytest.fixture()
def client():
    return load_service_app("resume")


@pytest.fixture()
def db():
    d = current_db()
    for c in ("tailoring_records", "applications", "notification_prefs",
              "parse_streaks", "saved_jobs"):
        d[c].delete_many({})
    return d


def _token(flask_app, identity=USER):
    from flask_jwt_extended import create_access_token
    with flask_app.app_context():
        return create_access_token(identity=identity)


def _seed(db, record_id="rec-1", *, user=USER, status="draft", application=None):
    db.tailoring_records.insert_one({
        "record_id": record_id,
        "user_email": user,
        "jd_analysis": {"company": "Acme", "job_title": "Engineer"},
        "application": application if application is not None else {"status": status},
        "created_at": datetime(2026, 1, 1),
    })


def _patch(client, token, body, record_id="rec-1"):
    return client.test_client().patch(
        f"/api/resume/tailoring-records/{record_id}/application",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )


def _app(db, record_id="rec-1", user=USER):
    doc = db.tailoring_records.find_one({"record_id": record_id, "user_email": user})
    return (doc or {}).get("application") or {}


# ---------------------------------------------------------------------------
# 1. The bug this exists to prevent: an evening application rolling to tomorrow
# ---------------------------------------------------------------------------
class TestCivilDayFromTimezone:
    def test_evening_local_application_keeps_its_own_day(self, client, db):
        """8pm Aug 29 in Los Angeles is Aug 30 03:00 UTC — the day must read 29."""
        _seed(db)
        token = _token(client)
        resp = _patch(client, token, {
            "status": "applied",
            "applied_at": "2026-08-30T03:00:00Z",
            "tz": LA,
        })
        assert resp.status_code == 200, resp.get_data(as_text=True)

        app = _app(db)
        assert app["applied_date"] == "2026-08-29"
        assert app["applied_tz"] == LA
        # The instant itself stays UTC and is untouched by the labelling.
        assert app["applied_at"].replace(tzinfo=timezone.utc) == datetime(
            2026, 8, 30, 3, 0, tzinfo=timezone.utc)

    def test_same_instant_labels_differently_per_zone(self, client, db):
        _seed(db, "rec-la")
        _seed(db, "rec-utc")
        token = _token(client)
        instant = "2026-08-30T03:00:00Z"

        _patch(client, token, {"status": "applied", "applied_at": instant, "tz": LA},
               record_id="rec-la")
        _patch(client, token, {"status": "applied", "applied_at": instant, "tz": "UTC"},
               record_id="rec-utc")

        assert _app(db, "rec-la")["applied_date"] == "2026-08-29"
        assert _app(db, "rec-utc")["applied_date"] == "2026-08-30"

    def test_missing_tz_falls_back_to_stored_user_zone(self, client, db):
        """No browser zone on the request -> the user's declared zone is used."""
        db.notification_prefs.insert_one(
            {"user_email": USER, "quiet_hours": {"tz": LA}})
        _seed(db)
        token = _token(client)
        _patch(client, token, {"status": "applied", "applied_at": "2026-08-30T03:00:00Z"})

        app = _app(db)
        assert app["applied_date"] == "2026-08-29"
        assert app["applied_tz"] == LA

    def test_auto_stamped_applied_at_is_also_labelled(self, client, db):
        """A bare status=applied stamps the instant, so it must stamp the day."""
        _seed(db)
        token = _token(client)
        _patch(client, token, {"status": "applied", "tz": LA})

        app = _app(db)
        assert app["applied_date"]
        assert len(app["applied_date"]) == 10
        assert app["applied_tz"] == LA

    def test_non_applied_status_writes_no_day(self, client, db):
        _seed(db)
        token = _token(client)
        _patch(client, token, {"status": "interviewing", "tz": LA})
        assert "applied_date" not in _app(db)

    def test_clearing_applied_at_clears_the_day(self, client, db):
        _seed(db, application={
            "status": "applied",
            "applied_at": datetime(2026, 8, 30, 3, 0),
            "applied_date": "2026-08-29",
            "applied_tz": LA,
        })
        token = _token(client)
        _patch(client, token, {"applied_at": None})

        app = _app(db)
        assert app["applied_at"] is None
        assert app["applied_date"] is None
        assert app["applied_tz"] is None


# ---------------------------------------------------------------------------
# 2. Back-dating — "I applied yesterday" from the post-download prompt
# ---------------------------------------------------------------------------
class TestBackDating:
    def test_explicit_applied_at_overrides_the_auto_stamp(self, client, db):
        _seed(db)
        token = _token(client)
        # Noon-local anchor, as the dialog sends it.
        _patch(client, token, {
            "status": "applied",
            "applied_at": "2026-08-20T19:00:00Z",   # noon in LA
            "tz": LA,
        })

        app = _app(db)
        assert app["applied_date"] == "2026-08-20"
        assert app["applied_at"].year == 2026
        assert app["applied_at"].month == 8
        assert app["applied_at"].day == 20

    def test_noon_anchor_survives_a_westward_reinterpretation(self, client, db):
        """The reason the dialog anchors at noon rather than midnight.

        Noon in LA is 19:00Z. Re-read in any zone from UTC-12 to UTC+11 that
        instant still falls on the same calendar day; a midnight anchor (07:00Z)
        would slide backwards for anyone west of UTC-7.
        """
        _seed(db, "rec-noon")
        _seed(db, "rec-midnight")
        token = _token(client)
        _patch(client, token,
               {"status": "applied", "applied_at": "2026-08-20T19:00:00Z", "tz": "UTC"},
               record_id="rec-noon")
        _patch(client, token,
               {"status": "applied", "applied_at": "2026-08-20T07:00:00Z", "tz": "Pacific/Midway"},
               record_id="rec-midnight")

        assert _app(db, "rec-noon")["applied_date"] == "2026-08-20"
        # Midnight-anchored slides to the previous day out west — what we avoid.
        assert _app(db, "rec-midnight")["applied_date"] == "2026-08-19"


# ---------------------------------------------------------------------------
# 3. confirmation_pending — the flow-agnostic rename, with the legacy alias
# ---------------------------------------------------------------------------
class TestConfirmationPending:
    def test_canonical_key_mirrors_to_the_legacy_name(self, client, db):
        _seed(db)
        token = _token(client)
        _patch(client, token, {"confirmation_pending": True})

        app = _app(db)
        assert app["confirmation_pending"] is True
        assert app["batch_confirmation_pending"] is True

    def test_legacy_key_still_accepted_and_mirrors_forward(self, client, db):
        _seed(db)
        token = _token(client)
        _patch(client, token, {"batch_confirmation_pending": True})

        app = _app(db)
        assert app["confirmation_pending"] is True
        assert app["batch_confirmation_pending"] is True

    def test_streak_fires_on_canonical_pending_confirmation(self, client, db, monkeypatch):
        _seed(db, application={"status": "draft", "confirmation_pending": True})
        calls = []
        import services.streak_service as ss
        monkeypatch.setattr(ss, "record_application",
                            lambda email, tz=None: calls.append(email))
        token = _token(client)
        resp = _patch(client, token,
                      {"status": "applied", "confirmation_pending": False})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert calls == [USER]

    def test_answering_the_prompt_clears_it(self, client, db):
        _seed(db, application={"status": "draft", "confirmation_pending": True})
        token = _token(client)
        _patch(client, token, {"status": "saved", "confirmation_pending": False})

        app = _app(db)
        assert app["confirmation_pending"] is False
        assert app["batch_confirmation_pending"] is False
        assert app["status"] == "saved"


# ---------------------------------------------------------------------------
# 4. The backfill — zone fallback chain and idempotency
# ---------------------------------------------------------------------------
class TestBackfill:
    def _run(self, db, dry_run=False):
        import importlib.util
        from pathlib import Path
        from services import user_time

        path = (Path(__file__).resolve().parent.parent
                / "scripts" / "backfill_applied_date.py")
        spec = importlib.util.spec_from_file_location("_backfill_applied_date", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.backfill_applied_dates(db, user_time, dry_run)

    def test_uses_declared_notification_zone_first(self, db):
        db.notification_prefs.insert_one(
            {"user_email": USER, "quiet_hours": {"tz": LA}})
        db.parse_streaks.insert_one({"user_email": USER, "tz": "Asia/Tokyo"})
        _seed(db, application={"status": "applied",
                               "applied_at": datetime(2026, 8, 30, 3, 0)})

        labelled, _, _ = self._run(db)
        assert labelled == 1
        assert _app(db)["applied_date"] == "2026-08-29"
        assert _app(db)["applied_tz"] == LA

    def test_falls_back_to_streak_zone(self, db):
        db.parse_streaks.insert_one({"user_email": USER, "tz": LA})
        _seed(db, application={"status": "applied",
                               "applied_at": datetime(2026, 8, 30, 3, 0)})

        self._run(db)
        assert _app(db)["applied_date"] == "2026-08-29"

    def test_falls_back_to_default_zone_when_nothing_is_known(self, db):
        _seed(db, application={"status": "applied",
                               "applied_at": datetime(2026, 8, 30, 3, 0)})

        self._run(db)
        # America/New_York is UTC-4 in August, so 03:00Z is still Aug 29 there.
        assert _app(db)["applied_date"] == "2026-08-29"
        assert _app(db)["applied_tz"] == "America/New_York"

    def test_skips_records_that_were_never_applied_to(self, db):
        _seed(db, application={"status": "draft"})
        labelled, _, _ = self._run(db)
        assert labelled == 0
        assert "applied_date" not in _app(db)

    def test_dry_run_writes_nothing(self, db):
        _seed(db, application={"status": "applied",
                               "applied_at": datetime(2026, 8, 30, 3, 0)})
        labelled, _, _ = self._run(db, dry_run=True)
        assert labelled == 1
        assert "applied_date" not in _app(db)

    def test_rerunning_is_a_noop(self, db):
        _seed(db, application={"status": "applied",
                               "applied_at": datetime(2026, 8, 30, 3, 0)})
        assert self._run(db)[0] == 1
        # Second pass finds nothing left to label.
        assert self._run(db)[0] == 0

    def test_clean_database_is_a_noop(self, db):
        assert self._run(db) == (0, 0, 0)
