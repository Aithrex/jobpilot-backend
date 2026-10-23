"""The employer policy on the APPLICATION paths and the notification paths:
match-sweep selection, the hourly saved-job scheduler, the final submission
gate (in-Lambda `submit` and the worker's `presubmit_recheck`), saved-search
alerts and the morning digest."""
from datetime import datetime, timedelta, timezone

import pytest

from _vg_support import current_db
from services import apply_match_sweep as sweep
from services import employer_policy as ep
from services import job_inventory_service as inv
from services.employer_registry import employer_id_for

from test_apply_match_sweep import (  # noqa: F401  (pytest fixture import)
    USER, sw, _seed_posting, _rewind_watermark)

NOW = datetime.now(timezone.utc)


# --- auto-apply selection ----------------------------------------------------
def test_match_sweep_never_selects_what_the_feed_hides(sw):
    _rewind_watermark(sw, hours=2)
    ep.put_prefs(USER, {"preset": "strict_direct"})
    _seed_posting(sjid="cap-1", company="Capgemini", parse=True,
                  first_seen=NOW - timedelta(minutes=50))
    _seed_posting(sjid="acme-1", company="Acme Corp", parse=True,
                  first_seen=NOW - timedelta(minutes=40))
    out = sweep.run_user_sweep(USER)
    assert out["policy_excluded"] == 1
    assert out["policy_excluded_counts"] == {"consultancy": 1}
    companies = {a["company"] for a in sw.applications.find({}, {"company": 1})}
    assert "Capgemini" not in companies
    # The excluded posting was EXAMINED: the cursor moved past it.
    wm = sw.auto_submit_settings.find_one({"user_email": USER})[sweep.WATERMARK_FIELD]
    wm = wm if wm.tzinfo else wm.replace(tzinfo=timezone.utc)
    assert wm >= NOW - timedelta(minutes=41)
    current_db().user_preferences.delete_many({})


def test_match_sweep_default_does_not_hide_consultancies(sw):
    _rewind_watermark(sw, hours=2)
    current_db().user_preferences.delete_many({})
    _seed_posting(sjid="cap-d", company="Capgemini", parse=True,
                  first_seen=NOW - timedelta(minutes=50))
    out = sweep.run_user_sweep(USER)
    assert out["policy_excluded"] == 0


def test_match_sweep_honours_a_stored_off_choice(sw):
    _rewind_watermark(sw, hours=2)
    ep.put_prefs(USER, {"preset": "off"})
    _seed_posting(sjid="cap-2", company="Capgemini", parse=True,
                  first_seen=NOW - timedelta(minutes=30))
    out = sweep.run_user_sweep(USER)
    assert out["policy_excluded"] == 0
    current_db().user_preferences.delete_many({})


def test_hourly_scheduler_skips_saved_jobs_the_policy_excludes(monkeypatch):
    from services import apply_scheduler as sched
    created = []
    monkeypatch.setattr(sched, "_saved_jobs", lambda email: [
        {"status": "saved", "job_data": {"company": "Acme Staffing Group", "title": "Java Dev",
                                         "apply_link": "https://roberthalf.com/job/1"}},
        {"status": "saved", "job_data": {"company": "Zyxwv Labs", "title": "Java Dev",
                                         "apply_link": "https://boards.greenhouse.io/z/1"}},
    ])
    from services import application_ledger_service as als
    monkeypatch.setattr(als, "dedup_gate", lambda e, i: (False, None))

    def _create(email, **kw):
        created.append(kw["company"])
        return {"application_id": None, "status": "queued"}, "created"
    monkeypatch.setattr(als, "create_application", _create)
    monkeypatch.setattr(sched, "_in_quiet_hours", lambda e: True)
    res = sched._run_user_hourly(USER)
    assert created == ["Zyxwv Labs"]
    assert res["skipped_policy"] == 1


# --- the final submission gate ---------------------------------------------
def _posting(job_id, **kw):
    doc = {"job_id": job_id, "source": "career-pages", "source_job_id": job_id,
           "title_raw": "Backend Engineer", "status": "active",
           "last_verified_at": NOW - timedelta(hours=1), "employer_name": "Etsy",
           "canonical_employer_id": employer_id_for(domain="etsy.com")}
    doc.update(kw)
    current_db().job_postings.insert_one(doc)


@pytest.fixture()
def clean_inv():
    db = current_db()
    db.job_postings.delete_many({})
    db.user_preferences.delete_many({})
    yield db
    db.job_postings.delete_many({})
    db.user_preferences.delete_many({})


def test_gate_passes_a_fresh_operating_posting(clean_inv):
    _posting("ok1")
    g = ep.submission_gate(USER, {"posting_job_id": "ok1", "company": "Etsy"})
    assert g["ok"], g


def test_gate_default_lets_a_consultancy_through_strict_does_not(clean_inv):
    _posting("capd", employer_name="Capgemini", canonical_employer_id=None,
             canonical_url="https://careers.capgemini.com/job/9")
    assert ep.submission_gate(USER, {"posting_job_id": "capd"})["ok"]
    ep.put_prefs(USER, {"preset": "strict_direct"})
    assert ep.submission_gate(USER, {"posting_job_id": "capd"})["reasons"] == \
        ["employer_policy:consultancy"]


def test_gate_blocks_policy_closed_expired_and_stale(clean_inv):
    ep.put_prefs(USER, {"preset": "strict_direct"})
    _posting("cap", employer_name="Capgemini", canonical_employer_id=None,
             canonical_url="https://careers.capgemini.com/job/1")
    _posting("closed", status="closed", closed_reason="not-seen")
    _posting("expired", valid_through=NOW - timedelta(hours=2))
    _posting("stale", last_verified_at=NOW - timedelta(hours=100))
    _posting("blocked", status="closed", closed_reason="unverified")
    assert ep.submission_gate(USER, {"posting_job_id": "cap"})["reasons"] == \
        ["employer_policy:consultancy"]
    assert "posting_closed_or_expired" in ep.submission_gate(
        USER, {"posting_job_id": "closed"})["reasons"]
    assert "posting_closed_or_expired" in ep.submission_gate(
        USER, {"posting_job_id": "expired"})["reasons"]
    stale = ep.submission_gate(USER, {"posting_job_id": "stale"})
    assert not stale["ok"] and "posting_verification_stale" in stale["reasons"]
    blocked = ep.submission_gate(USER, {"posting_job_id": "blocked"})
    assert not blocked["ok"] and "posting_closed_or_expired" not in blocked["reasons"]
    assert blocked["availability"]["state"] == "unverifiable"


def test_gate_without_an_inventory_record_still_applies_policy_and_expiry(clean_inv):
    ok = ep.submission_gate(USER, {"company": "Zyxwv Labs", "title": "Dev"})
    assert ok["ok"]
    assert not ep.submission_gate(USER, {"company": "Acme Staffing Group", "title": "Dev"})["ok"]
    expired = ep.submission_gate(USER, {"company": "Zyxwv Labs",
                                        "posting_closes_at": (NOW - timedelta(days=1)).isoformat()})
    assert "posting_closed_or_expired" in expired["reasons"]


def test_presubmit_recheck_carries_the_policy_gate(clean_inv, monkeypatch):
    ep.put_prefs(USER, {"preset": "strict_direct"})
    from services import auto_apply_service as aas
    _posting("cap2", employer_name="Capgemini", canonical_employer_id=None,
             canonical_url="https://careers.capgemini.com/job/2")
    app = {"application_id": "a1", "status": "running", "posting_job_id": "cap2",
           "company": "Capgemini"}
    monkeypatch.setattr(aas.ledger, "get_application", lambda e, a: dict(app))
    out = aas.presubmit_recheck("a1", USER)
    assert out["ok"] is False
    assert out["checks"]["policy_employer_policy"] is False
    assert "employer_policy:consultancy" in out["reasons"]


def test_submit_cancels_a_policy_excluded_row_before_the_executor(clean_inv, monkeypatch):
    ep.put_prefs(USER, {"preset": "strict_direct"})
    from services import auto_apply_service as aas
    _posting("cap3", employer_name="Capgemini", canonical_employer_id=None,
             canonical_url="https://careers.capgemini.com/job/3")
    app = {"application_id": "a2", "status": aas.ledger.STATUS_APPROVED,
           "posting_job_id": "cap3", "company": "Capgemini"}
    moved = []
    monkeypatch.setattr(aas.ledger, "get_application", lambda e, a: dict(app))
    monkeypatch.setattr(aas.ledger, "get_approval_snapshot", lambda e, a: None)
    monkeypatch.setattr(aas, "_manually_applied", lambda e, a: None)
    monkeypatch.setattr(aas.ledger, "transition", lambda e, a, s: moved.append(s))

    class _NoExec:
        def is_available(self):
            raise AssertionError("the executor must not be reached")
    monkeypatch.setattr(aas, "get_executor", lambda: _NoExec())
    out = aas.submit("a2", USER)
    assert out["status"] == "blocked_by_policy"
    assert moved == [aas.ledger.STATUS_CANCELLED]


# --- notifications ----------------------------------------------------------
def test_saved_search_alert_never_emails_an_excluded_posting(clean_inv, monkeypatch):
    from services import tech_feed_alerts as alerts
    from services import tech_feed_saved_searches as tfss
    from services import tech_feed_service as tfs
    db = current_db()
    db[tfss.SAVED_SEARCHES].delete_many({})
    db[alerts.DELIVERIES].delete_many({})
    tfss.save(USER, client_id="s1", name="Backend", filters={"posted_within_days": 7},
              alerts={"enabled": True})
    captured = {}
    import services.notifications.service as notify
    monkeypatch.setattr(notify, "get_prefs", lambda e: {"channels": {}, "events": {}})
    monkeypatch.setattr(notify, "send", lambda e, ev, payload: captured.update(payload))
    monkeypatch.setattr(tfs, "search", lambda f: {"jobs": [
        {"job_id": "j-ok", "title": "Backend", "employer_name": "Etsy", "status": "active",
         "employer_profile": {"kind": "operating"}},
    ], "policy": ep.empty_block()})
    out = alerts.evaluate_search({"user_email": USER, "client_id": "s1",
                                  "filters": {}, "alerts": {"enabled": True},
                                  "name": "Backend"})
    assert out["status"] == "sent"
    # The alert's only job source is `tech_feed_service.search`, whose policy
    # enforcement is covered in test_employer_policy_enforcement.py.
    assert [j["job_id"] for j in captured["jobs"]] == ["j-ok"]


def test_daily_digest_reapplies_policy_at_send_time(clean_inv):
    ep.put_prefs(USER, {"preset": "strict_direct"})
    from services.notifications import digests
    db = current_db()
    db.resume_jobs.delete_many({"user_email": USER})
    db.resume_jobs.insert_one({
        "user_email": USER, "job_type": "daily_pipeline", "status": "completed",
        "completed_at": NOW, "result": {"apply_now": [
            {"title": "Java Dev", "company": "Capgemini",
             "url": "https://careers.capgemini.com/1"},
            {"title": "Java Dev", "company": "Zyxwv Labs",
             "url": "https://boards.greenhouse.io/zyxwv/1"}]}})
    rows = digests._top_matches(USER, 5)
    assert [r["company"] for r in rows] == ["Zyxwv Labs"]
    db.resume_jobs.delete_many({"user_email": USER})
