"""Guest Onboarding v2 — offline test suite (docs/specs/guest-onboarding.md v2 §QA).

Covers, per the v2 spec:
  * identity resolution matrix — keyed-HMAC signals, >=2-types-with-1-strong
    merge, namesake / name+device / device-only never merge, fail-closed when
    GUEST_SIGNAL_HMAC_KEY is unset (no plaintext storage, no matching);
  * quota subjects — the atomic single-reservation slot (one reservation under
    contention, idempotent same-match retry, failed-tailor release), subject
    counters survive re-uploads under fresh guest_ids;
  * subject-level same-day match cache — re-upload never re-spends the global
    matches breaker; server never returns more than guest_jobs_max rows;
  * tailor accepts ONLY served match_ids (v1 job payloads → 400);
  * download capability tokens — mint/verify, forged → 404, expired → 410;
  * claim v2 — exact/atomic/idempotent, day-1 free carry-in seeded once per
    (subject, day), premium untouched, linked free accounts share the pool;
  * retention cleanup deletes only expired unclaimed data;
  * global breakers, ip_hash fail-closed, challenge seam, no guest 401s.

Fully offline: mongomock via tests/_vg_support (patched before any service
import), no Bedrock (async dispatch stubbed), no network (free-source fetch
patched with canned rows), renderer patched where a route would render.
"""
import io
import json
import sys
import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest

from _vg_support import load_service_app

from services import guest_challenge_service
from services import guest_identity_service as identity
from services import guest_service
from services import platform_config_service as pconfig
from utils import db_connect

GUEST_COLLECTIONS = (
    "guest_sessions",
    "guest_quota_subjects",
    "guest_claim_carryins",
    "guest_global_counters",
    "guest_challenge_velocity",
    "guest_challenges",
    "rate_limit_buckets",
    "job_match_snapshots",
    "platform_config",
    "tailoring_records",
    "tailor_usage",
    "user_resumes",
    "resume_assets",
    "resume_jobs",
    "users",
    "feature_usage",
    "sponsorship_signals",
    "quota_counters",
    "quota_ledger",
)

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


def _structured(name="Jordan Alvarez", email="jordan.alvarez@example.com",
                phone="(555) 010-7788", skills=None):
    return {
        "contact": {"name": name, "email": email, "phone": phone,
                    "linkedin": "", "github": ""},
        "summary": "Backend engineer building APIs.",
        "skills": {"languages": skills or ["Python", "Go", "SQL"]},
        "experience": [{
            "title": "Backend Engineer", "company": "Acme",
            "dates": "2020 – 2024", "bullets": ["Built APIs in Python"],
        }],
        "education": [],
        "projects": [],
        "certifications": [],
    }


@pytest.fixture(autouse=True)
def _guest_secrets(monkeypatch):
    """v2 secrets present by default (individual tests delenv to prove the
    fail-closed paths). Challenge provider defaults to the no-op."""
    monkeypatch.setenv("GUEST_SIGNAL_HMAC_KEY", "unit-test-signal-key")
    monkeypatch.setenv("GUEST_DOWNLOAD_TOKEN_SECRET", "unit-test-download-secret")
    monkeypatch.delenv("GUEST_CHALLENGE_PROVIDER", raising=False)
    monkeypatch.delenv("GUEST_CHALLENGE_SECRET", raising=False)
    monkeypatch.delenv("GUEST_IP_HASH_SALT", raising=False)


@pytest.fixture()
def guest_db():
    """Clean guest-related collections + drop the config TTL cache + reset the
    process-global in-memory IP rate limiter (route tests share one process)."""
    from utils.security import get_rate_limiter
    db = db_connect.DBConnect().get_db()
    for c in GUEST_COLLECTIONS:
        db[c].delete_many({})
    pconfig.invalidate_cache()
    get_rate_limiter()._requests.clear()
    yield db
    pconfig.invalidate_cache()
    get_rate_limiter()._requests.clear()


def _new_session(fp=None, device_token_hash=None):
    return guest_service.create_or_get_session(
        None, fp, "iphash", device_token_hash=device_token_hash
    )


def _attach(guest_id, structured, raw_text, fp=None):
    return guest_service.attach_parsed_resume(
        guest_id, structured, raw_text, "resume.pdf", fingerprint_hash=fp
    )


def _subject_doc(guest_db, guest_id):
    sid = guest_service.subject_for_guest(guest_id)
    return guest_db.guest_quota_subjects.find_one({"subject_id": sid})


def _serve_matches(guest_id, n=2):
    """Store a served match cache (cards + server-side JD sources) and return
    the minted match_ids — mirrors what compute_guest_matches persists."""
    jobs, sources = [], {}
    for i in range(n):
        mid = _uuid.uuid4().hex
        jobs.append({
            "match_id": mid, "title": f"Backend Role {i}", "company": "Acme",
            "url": f"https://jobs.example/{i}", "source": "Greenhouse",
            "days_ago": 1,   # guest freshness contract: cards carry a known age
            "match_score": 80 - i, "matched_skills": ["python"],
            "why": "Estimated match because this posting mentions python from your resume.",
            "sponsorship": None,
        })
        sources[mid] = {
            "title": f"Backend Role {i}", "company": "Acme",
            "jd_text": "Python APIs backend services " * 20,
            "url": f"https://jobs.example/{i}",
        }
    guest_service._store_matches(guest_id, jobs, 20, jd_sources=sources)
    return [j["match_id"] for j in jobs]


# ===========================================================================
# Identity resolution matrix (v2: keyed HMACs, >=2 types with >=1 strong)
# ===========================================================================

class TestIdentityResolution:
    def test_extract_signals_are_versioned_hmacs(self, guest_db):
        sig = identity.extract_signals(
            _structured(name="  JOrdan   Álvarez ", phone="+1 (555) 010-7788"),
            fingerprint_hash="fp_ABC-123",
            raw_text="Jordan Alvarez resume text " * 5,
        )
        assert sig["name_normalized"] == "jordan alvarez"
        assert len(sig["email_hmacs"]) == 1
        assert sig["email_hmacs"][0].startswith("v1:")
        assert len(sig["email_hmacs"][0]) == len("v1:") + 64
        # No plaintext PII survives in the stored/matched signal values.
        assert "jordan.alvarez" not in sig["email_hmacs"][0]
        assert len(sig["phone_hmacs"]) == 1 and sig["phone_hmacs"][0].startswith("v1:")
        assert "5550107788" not in sig["phone_hmacs"][0]
        assert sig["resume_sig_hmac"].startswith("v1:")
        assert sig["fingerprint"] == "fp_ABC-123"

    def test_fail_closed_without_hmac_key(self, guest_db, monkeypatch):
        monkeypatch.delenv("GUEST_SIGNAL_HMAC_KEY", raising=False)
        sig = identity.extract_signals(
            _structured(), fingerprint_hash="fp_x", raw_text="text " * 10
        )
        # No key → no signals stored or matched; NO plaintext fallback.
        assert sig["email_hmacs"] == [] and sig["phone_hmacs"] == []
        assert sig["resume_sig_hmac"] == ""

        # Identical resume + identical fingerprint → subjects stay STANDALONE.
        a = _new_session(fp="fp_shared")
        _attach(a["guest_id"], _structured(), "same resume text " * 5, fp="fp_shared")
        b = _new_session(fp="fp_shared")
        rec = _attach(b["guest_id"], _structured(), "same resume text " * 5, fp="fp_shared")
        assert rec["recognized"] is False
        b_doc = guest_db.guest_sessions.find_one({"guest_id": b["guest_id"]})
        assert b_doc["merged_into"] is None
        assert b_doc["identity"]["email_hmacs"] == []
        assert "emails" not in b_doc["identity"]  # v1 plaintext fields never written

    def test_two_strong_signals_merge_and_unify_subjects(self, guest_db):
        raw_a = "Jordan Alvarez jordan.alvarez@example.com backend python " * 4
        a = _new_session()
        _attach(a["guest_id"], _structured(), raw_a)

        # Second device, DIFFERENT resume text/fingerprint but same email+phone
        # (2 distinct strong types) -> same person, soft merge.
        b = _new_session(fp="fp_other_device")
        rec = _attach(
            b["guest_id"],
            _structured(),
            "totally different canonical text for hashing " * 4,
            fp="fp_other_device",
        )
        assert rec["recognized"] is True
        # v2: the returned name is the session's OWN parsed name (never a
        # cross-session disclosure).
        assert rec["recognized_name"] == "jordan alvarez"

        b_doc = guest_db.guest_sessions.find_one({"guest_id": b["guest_id"]})
        a_doc = guest_db.guest_sessions.find_one({"guest_id": a["guest_id"]})
        assert b_doc["merged_into"] == a["guest_id"]
        assert set(identity.cluster_ids(b["guest_id"])) == {a["guest_id"], b["guest_id"]}
        # One quota subject for the whole cluster.
        assert b_doc["quota_subject_id"] == a_doc["quota_subject_id"]

    def test_single_strong_signal_does_not_merge(self, guest_db):
        a = _new_session()
        _attach(a["guest_id"], _structured(), "resume text one " * 5)
        # Same EMAIL only — different phone, text, no shared device: 1 type.
        b = _new_session()
        rec = _attach(
            b["guest_id"],
            _structured(name="Sam Lee", phone="(444) 222-9999"),
            "a wholly different resume " * 5,
        )
        assert rec["recognized"] is False
        assert guest_db.guest_sessions.find_one(
            {"guest_id": b["guest_id"]})["merged_into"] is None

    def test_namesake_name_only_does_not_merge(self, guest_db):
        a = _new_session()
        _attach(a["guest_id"], _structured(), "resume text one " * 5)
        namesake = _structured(email="other.jordan@elsewhere.org", phone="(444) 222-9999")
        b = _new_session()
        rec = _attach(b["guest_id"], namesake, "a wholly different resume " * 5)
        assert rec["recognized"] is False
        b_doc = guest_db.guest_sessions.find_one({"guest_id": b["guest_id"]})
        assert b_doc["merged_into"] is None
        assert identity.cluster_ids(b["guest_id"]) == [b["guest_id"]]

    def test_name_plus_device_never_merges(self, guest_db):
        # Shared computer, two people with the same name: name (weak) +
        # fingerprint/device (weak) = 2 types but ZERO strong → no merge.
        a = _new_session(fp="fp_family_pc")
        _attach(a["guest_id"], _structured(), "first person resume " * 5, fp="fp_family_pc")
        b = _new_session(fp="fp_family_pc")
        rec = _attach(
            b["guest_id"],
            _structured(email="other.jordan@elsewhere.org", phone="(444) 222-9999"),
            "second person resume entirely different " * 5,
            fp="fp_family_pc",
        )
        assert rec["recognized"] is False
        b_doc = guest_db.guest_sessions.find_one({"guest_id": b["guest_id"]})
        assert b_doc["merged_into"] is None
        # Shared-device users stay separate subjects (separate trials).
        a_doc = guest_db.guest_sessions.find_one({"guest_id": a["guest_id"]})
        assert b_doc["quota_subject_id"] != a_doc["quota_subject_id"]

    def test_strong_plus_device_merges(self, guest_db):
        # resume-signature (strong) + fingerprint (device) = 2 types, 1 strong.
        raw = "identical canonical resume text " * 4
        a = _new_session(fp="fp_same")
        _attach(a["guest_id"], _structured(email="", phone=""), raw, fp="fp_same")
        b = _new_session(fp="fp_same")
        rec = _attach(b["guest_id"], _structured(email="", phone=""), raw, fp="fp_same")
        assert rec["recognized"] is True
        assert guest_db.guest_sessions.find_one(
            {"guest_id": b["guest_id"]})["merged_into"] == a["guest_id"]


# ===========================================================================
# Quota subjects — atomic single-reservation slot
# ===========================================================================

class TestReservation:
    def test_default_limit_is_one(self, guest_db):
        assert pconfig.DEFAULTS["guest_tailor_limit"] == 1
        assert pconfig.DEFAULTS["guest_jobs_max"] == 5
        assert pconfig.DEFAULTS["retention_days"] == 30

    def test_single_reservation_then_exhausted(self, guest_db):
        s = _new_session()
        m1, m2 = _uuid.uuid4().hex, _uuid.uuid4().hex
        first = guest_service.try_reserve_tailor(s["guest_id"], m1)
        assert first.get("reserved") is True
        assert first["state"]["tailors_used"] == 1 and first["state"]["tailors_limit"] == 1
        # A DIFFERENT match after the reservation → quota exhausted.
        second = guest_service.try_reserve_tailor(s["guest_id"], m2)
        assert second.get("exhausted") is True
        doc = _subject_doc(guest_db, s["guest_id"])
        assert doc["tailors_used"] == 1
        assert doc["reservation"]["match_id"] == m1

    def test_same_match_retry_is_idempotent(self, guest_db):
        s = _new_session()
        mid = _uuid.uuid4().hex
        assert guest_service.try_reserve_tailor(s["guest_id"], mid)["reserved"]
        job_id = str(_uuid.uuid4())
        guest_service.attach_reservation_job(s["guest_id"], mid, job_id)
        retry = guest_service.try_reserve_tailor(s["guest_id"], mid)
        assert retry.get("existing") is True and retry["job_id"] == job_id
        # Counter did NOT double-charge.
        assert _subject_doc(guest_db, s["guest_id"])["tailors_used"] == 1

    def test_failed_tailor_releases_the_slot(self, guest_db):
        s = _new_session()
        mid = _uuid.uuid4().hex
        assert guest_service.try_reserve_tailor(s["guest_id"], mid)["reserved"]
        job_id = str(_uuid.uuid4())
        guest_service.attach_reservation_job(s["guest_id"], mid, job_id)
        guest_service.fail_reservation(s["guest_id"], job_id)
        doc = _subject_doc(guest_db, s["guest_id"])
        assert doc["tailors_used"] == 0                       # refunded, never negative
        assert doc["reservation"]["status"] == "failed"
        # Double-fail is idempotent (status guard).
        guest_service.fail_reservation(s["guest_id"], job_id)
        assert _subject_doc(guest_db, s["guest_id"])["tailors_used"] == 0
        # The guest is not cheated: a fresh reservation succeeds.
        again = guest_service.try_reserve_tailor(s["guest_id"], mid)
        assert again.get("reserved") is True

    def test_completed_reservation_is_terminal(self, guest_db):
        s = _new_session()
        mid = _uuid.uuid4().hex
        guest_service.try_reserve_tailor(s["guest_id"], mid)
        job_id = str(_uuid.uuid4())
        guest_service.attach_reservation_job(s["guest_id"], mid, job_id)
        guest_service.complete_reservation(s["guest_id"], job_id, "rec-1")
        doc = _subject_doc(guest_db, s["guest_id"])
        assert doc["reservation"]["status"] == "completed"
        assert doc["reservation"]["record_id"] == "rec-1"
        # fail after complete: no-op (no refund of a delivered tailor).
        guest_service.fail_reservation(s["guest_id"], job_id)
        assert _subject_doc(guest_db, s["guest_id"])["tailors_used"] == 1
        # Same-match retry after completion is still idempotent.
        retry = guest_service.try_reserve_tailor(s["guest_id"], mid)
        assert retry.get("existing") is True and retry["job_id"] == job_id

    def test_pre_dispatch_release_restores_pristine_slot(self, guest_db):
        s = _new_session()
        guest_service.try_reserve_tailor(s["guest_id"], _uuid.uuid4().hex)
        guest_service.release_reservation(s["guest_id"])
        doc = _subject_doc(guest_db, s["guest_id"])
        assert doc["tailors_used"] == 0 and doc["reservation"] is None
        # Status-guarded: a second release cannot go negative.
        guest_service.release_reservation(s["guest_id"])
        assert _subject_doc(guest_db, s["guest_id"])["tailors_used"] == 0

    def test_reupload_fresh_guest_id_does_not_reset_trial(self, guest_db):
        # Session A uses the single trial; the same person re-uploads the same
        # resume under a FRESH guest_id → merged subject → still exhausted.
        raw = "one resume to rule them all " * 4
        a = _new_session()
        _attach(a["guest_id"], _structured(), raw)
        assert guest_service.try_reserve_tailor(a["guest_id"], _uuid.uuid4().hex)["reserved"]

        b = _new_session(fp="fp_fresh_browser")
        _attach(b["guest_id"], _structured(), raw, fp="fp_fresh_browser")
        outcome = guest_service.try_reserve_tailor(b["guest_id"], _uuid.uuid4().hex)
        assert outcome.get("exhausted") is True
        assert guest_service.tailor_quota_state(b["guest_id"])["tailors_used"] == 1

    def test_unknown_session_reserve_fails_cleanly(self, guest_db):
        outcome = guest_service.try_reserve_tailor(str(_uuid.uuid4()), _uuid.uuid4().hex)
        assert outcome.get("exhausted") is True
        assert guest_db.guest_sessions.count_documents({}) == 0


# ===========================================================================
# Global daily circuit breakers + entitlements passthrough
# ===========================================================================

class TestGuestQuotas:
    def test_global_daily_circuit_breaker(self, guest_db):
        pconfig.update_guest_config({"global_daily_guest_tailors": 2}, "admin@test")
        assert guest_service.try_reserve_global_tailor() is True
        assert guest_service.try_reserve_global_tailor() is True
        assert guest_service.try_reserve_global_tailor() is False
        # Over-limit attempt rolled its $inc back — counter stays at the cap.
        doc = guest_db.guest_global_counters.find_one(
            {"_id": {"$regex": "^guest_tailors:"}})
        assert doc["count"] == 2
        # release (failed dispatch) frees a slot again
        guest_service.release_global_tailor()
        assert guest_service.try_reserve_global_tailor() is True

    def test_free_daily_tailors_flows_into_entitlements(self, guest_db):
        from services import entitlements_service
        assert entitlements_service.quota("free", "tailor") == 5  # default
        pconfig.update_guest_config({"free_daily_tailors": 7}, "admin@test")
        assert entitlements_service.quota("free", "tailor") == 7
        # premium plans unaffected by the guest knob
        assert entitlements_service.quota("premium", "tailor") == 30

    def test_quota_identical_when_platform_config_doc_absent(self, guest_db):
        # Prod state until an admin first writes the doc: existing users must
        # get byte-identical behavior to before Guest Onboarding existed.
        from services import entitlements_service
        assert guest_db.platform_config.find_one({"_id": "guest_onboarding"}) is None
        assert entitlements_service.quota("free", "tailor") == 5
        assert entitlements_service.quota("premium", "tailor") == 30
        assert entitlements_service.quota("free", "tailor") == \
            entitlements_service.PLAN_LIMITS["free"]["tailor"]


# ===========================================================================
# Claim v2 — atomic/idempotent, carry-in once per (subject, day), free only
# ===========================================================================

class TestClaim:
    def _seed_guest(self, guest_db, tailors_used=1):
        s = _new_session()
        _attach(s["guest_id"], _structured(), "resume content for claim " * 4)
        sid = guest_service.subject_for_guest(s["guest_id"])
        guest_db.guest_quota_subjects.update_one(
            {"subject_id": sid}, {"$set": {"tailors_used": tailors_used}})
        guest_db.tailoring_records.insert_one({
            "record_id": str(_uuid.uuid4()), "user_email": None,
            "guest_id": s["guest_id"],
            "tailored_resume": {"summary": "x"}, "source": "guest_tailor",
        })
        return s["guest_id"]

    def test_claim_v2_shape_and_single_seed(self, guest_db):
        gid = self._seed_guest(guest_db, tailors_used=1)
        email = "newuser@example.com"

        first = guest_service.claim_session(gid, email)
        assert first["claimed"] is True and first["already_claimed"] is False
        assert first["records_transferred"] == 1
        assert first["resume_imported"] is True
        assert first["tailors_carried"] == 1
        assert first["daily_tailors_used"] == 1      # "1 of 5 used today"
        assert first["daily_tailors_limit"] == 5

        # REPEAT by the same account: already_claimed:true, nothing re-runs.
        second = guest_service.claim_session(gid, email)
        assert second["claimed"] is True and second["already_claimed"] is True
        assert second["records_transferred"] == 0
        assert second["tailors_carried"] == 0
        assert second["daily_tailors_used"] == 1     # unchanged — no double seed

        from services import usage_service
        assert usage_service.get_usage(email)["used"] == 1  # seeded exactly ONCE
        assert guest_db.guest_claim_carryins.count_documents({}) == 1

        rec = guest_db.tailoring_records.find_one({"guest_id": gid})
        assert rec["user_email"] == email
        session = guest_db.guest_sessions.find_one({"guest_id": gid})
        assert session["claimed_by"] == email and session["claimed_at"]
        # The account email is linked on the quota subject.
        assert email in _subject_doc(guest_db, gid)["linked_accounts"]

    def test_claim_by_second_account_is_rejected(self, guest_db):
        gid = self._seed_guest(guest_db)
        assert guest_service.claim_session(gid, "first@example.com")["claimed"]
        res = guest_service.claim_session(gid, "second@example.com")
        assert res.get("error") == "already_claimed"

    def test_claim_does_not_overwrite_existing_resume(self, guest_db):
        gid = self._seed_guest(guest_db)
        email = "hasresume@example.com"
        # v3: the user's resume lives on a resume_assets doc.
        guest_db.resume_assets.insert_one({
            "resume_id": "existing-asset", "owner_type": "user",
            "user_email": email, "structured": {"summary": "existing"},
            "parse_status": "parsed",
            "parsed_at": datetime.now(timezone.utc),
        })
        res = guest_service.claim_session(gid, email)
        assert res["resume_imported"] is False
        # The user's pre-existing asset is untouched (v3: the claim ATTACHES
        # the guest asset as an additional owned asset, never overwrites).
        doc = guest_db.resume_assets.find_one({"resume_id": "existing-asset"})
        assert doc["structured"]["summary"] == "existing"
        # And the active pointer was not stolen by the claimed guest asset.
        user = guest_db.users.find_one({"email": email}) or {}
        assert user.get("active_resume_id") in (None, "existing-asset")

    def test_premium_claim_never_seeds_usage(self, guest_db):
        gid = self._seed_guest(guest_db, tailors_used=1)
        email = "prem@example.com"
        guest_db.users.insert_one({"email": email, "plan": "premium"})
        res = guest_service.claim_session(gid, email)
        assert res["claimed"] is True
        assert res["tailors_carried"] == 0            # premium untouched
        from services import usage_service
        assert guest_db.tailor_usage.count_documents({"email": email}) == 0
        assert guest_db.guest_claim_carryins.count_documents({}) == 0
        # Artifacts still transfer (claim is not a metering feature).
        assert res["records_transferred"] == 1

    def test_linked_free_accounts_share_daily_pool(self, guest_db):
        # Two sessions of the SAME quota subject, claimed by two FREE accounts
        # → both linked → shared daily pool, carry-in seeded ONCE.
        raw = "shared subject resume " * 4
        a = _new_session()
        _attach(a["guest_id"], _structured(), raw)
        sid = guest_service.subject_for_guest(a["guest_id"])
        guest_db.guest_quota_subjects.update_one(
            {"subject_id": sid}, {"$set": {"tailors_used": 1}})
        b = _new_session(fp="fp_b")
        _attach(b["guest_id"], _structured(), raw, fp="fp_b")  # strong merge
        assert guest_service.subject_for_guest(b["guest_id"]) == sid

        first = guest_service.claim_session(a["guest_id"], "alpha@example.com")
        assert first["tailors_carried"] == 1
        second = guest_service.claim_session(b["guest_id"], "beta@example.com")
        assert second["claimed"] is True
        assert second["tailors_carried"] == 0          # carry-in once per (subject, day)
        assert second["daily_tailors_used"] == 1       # sees the shared pool

        from services import usage_service
        assert usage_service.get_usage("alpha@example.com")["used"] == 1
        assert usage_service.get_usage("beta@example.com")["used"] == 1  # shared
        usage_service.increment_usage("beta@example.com", 1)
        assert usage_service.get_usage("alpha@example.com")["used"] == 2  # no double count
        # Each event recorded exactly once under its actor.
        assert guest_db.tailor_usage.count_documents({}) == 2

    def test_premium_user_pool_untouched(self, guest_db):
        # A premium account linked to a subject still meters SOLO.
        guest_db.guest_quota_subjects.insert_one({
            "subject_id": str(_uuid.uuid4()), "tailors_used": 1,
            "reservation": None, "matched_jobs": None,
            "linked_accounts": ["prem2@example.com", "free2@example.com"],
        })
        guest_db.users.insert_one({"email": "prem2@example.com", "plan": "premium"})
        from services import usage_service
        usage_service.increment_usage("free2@example.com", 3)
        assert usage_service.get_usage("prem2@example.com")["used"] == 0


# ===========================================================================
# Admin config validation
# ===========================================================================

class TestAdminConfig:
    def test_validation_bounds(self, guest_db):
        clean, errors = pconfig.validate_guest_config_patch({
            "guest_tailor_limit": 10,
            "guest_jobs_min_score": 101,      # > max 100
            "guest_jobs_max": 0,              # < min 1
            "free_daily_tailors": "5",        # wrong type
            "enabled": "yes",                 # wrong type
            "nonsense_key": 1,                # unknown
            "retention_days": 30,
        })
        assert clean == {"guest_tailor_limit": 10, "retention_days": 30}
        assert set(errors) == {
            "guest_jobs_min_score", "guest_jobs_max", "free_daily_tailors",
            "enabled", "nonsense_key",
        }

    def test_bool_rejected_for_numeric_knob(self, guest_db):
        clean, errors = pconfig.validate_guest_config_patch({"guest_jobs_max": True})
        assert not clean and "guest_jobs_max" in errors

    def test_v2_defaults(self, guest_db):
        # v2 product contract: 1 lifetime guest tailor, exactly 5 matches,
        # 30-day unclaimed retention. Bounds unchanged.
        assert pconfig.DEFAULTS["guest_tailor_limit"] == 1
        assert pconfig.DEFAULTS["guest_jobs_max"] == 5
        assert pconfig.DEFAULTS["retention_days"] == 30
        assert pconfig.BOUNDS["guest_tailor_limit"] == (0, 100)
        assert pconfig.BOUNDS["guest_jobs_max"] == (1, 50)
        assert pconfig.BOUNDS["retention_days"] == (0, 3650)

    def test_min_score_default_matches_scorer_range(self, guest_db):
        # jd_match_scorer.score_one (denominator clamped to [8, 14]) lands
        # 21-43 on genuine fits because real postings name only 3-6 specific
        # technologies. The bar is locked at 25: 40 starved skill-rich
        # resumes to zero results (2026-07-20 incident), and the spec's
        # original 70 would blank the funnel entirely. Don't raise it
        # without re-tuning the scorer.
        assert pconfig.DEFAULTS["guest_jobs_min_score"] == 25

    def test_admin_route_put_and_get(self, guest_db):
        app = load_service_app("auth")
        from flask_jwt_extended import create_access_token
        with app.app_context():
            token = create_access_token(identity="harshith@aithrex.tech")
        client = app.test_client()
        headers = {"Authorization": f"Bearer {token}"}

        bad = client.put("/api/admin/guest-config", headers=headers,
                         json={"guest_jobs_min_score": -1})
        assert bad.status_code == 400
        assert bad.get_json()["error"] == "invalid_config"

        ok = client.put("/api/admin/guest-config", headers=headers,
                        json={"guest_jobs_min_score": 40, "guest_jobs_max": 10})
        assert ok.status_code == 200
        body = ok.get_json()
        assert body["guest_jobs_min_score"] == 40
        assert body["guest_jobs_max"] == 10
        assert body["updated_by"] == "harshith@aithrex.tech"
        assert body["updated_at"]

        got = client.get("/api/admin/guest-config", headers=headers)
        assert got.status_code == 200
        assert got.get_json()["guest_jobs_min_score"] == 40

    def test_admin_route_requires_admin(self, guest_db):
        app = load_service_app("auth")
        from flask_jwt_extended import create_access_token
        with app.app_context():
            token = create_access_token(identity="pleb@example.com")
        resp = app.test_client().get(
            "/api/admin/guest-config",
            headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403  # never 401 for authorization failures


# ===========================================================================
# Route-level guest flow (resume Lambda app)
# ===========================================================================

@pytest.fixture()
def resume_app(guest_db, monkeypatch):
    # Never spawn the async worker thread in route tests (it would hit the
    # LLM boundary). Routes still return {job_id} and the job row exists.
    import services.resume_service as rs
    monkeypatch.setattr(rs.ResumeService, "invoke_async",
                        staticmethod(lambda *a, **k: None))
    return load_service_app("resume")


class TestGuestRoutes:
    def test_disabled_flag_returns_503(self, guest_db, monkeypatch, resume_app):
        monkeypatch.setenv("GUEST_ONBOARDING_ENABLED", "false")
        resp = resume_app.test_client().get("/api/resume/guest/session")
        assert resp.status_code == 503
        assert resp.get_json()["error"] == "guest_onboarding_disabled"

    def test_admin_kill_switch_returns_503(self, guest_db, resume_app):
        pconfig.update_guest_config({"enabled": False}, "admin@test")
        resp = resume_app.test_client().get("/api/resume/guest/session")
        assert resp.status_code == 503

    def test_onboard_returns_ids_and_device_token(self, guest_db, resume_app):
        import hashlib
        client = resume_app.test_client()
        resp = client.post(
            "/api/resume/guest/onboard",
            data={"file": (io.BytesIO(PDF_BYTES), "resume.pdf"),
                  "fingerprint_hash": "fp_route_test"},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 202, resp.get_data(as_text=True)
        body = resp.get_json()
        gid, jid, device_token = body["guest_id"], body["job_id"], body["device_token"]
        assert guest_service.validate_guest_id(gid)
        assert len(device_token) >= 32  # >=32 random bytes urlsafe
        # Server stores ONLY sha256(token).
        doc = guest_db.guest_sessions.find_one({"guest_id": gid})
        assert doc["device_token_hash"] == hashlib.sha256(
            device_token.encode()).hexdigest()
        assert device_token not in str(doc)

        # Echoed device_token on a repeat onboard is kept, not re-minted.
        again = client.post(
            "/api/resume/guest/onboard",
            data={"file": (io.BytesIO(PDF_BYTES), "resume.pdf"),
                  "guest_id": gid, "device_token": device_token},
            content_type="multipart/form-data",
        )
        assert again.status_code == 202
        assert again.get_json()["device_token"] == device_token
        assert again.get_json()["guest_id"] == gid

        # Capability scoping: right guest sees the job, wrong guest gets 404.
        ok = client.get(f"/api/resume/guest/job/{jid}?guest_id={gid}")
        assert ok.status_code == 200
        job = ok.get_json()
        assert {"job_id", "job_type", "status", "result", "error",
                "created_at", "completed_at"} <= set(job)
        assert "payload" not in job
        assert job["status"] == "processing"

        wrong = client.get(f"/api/resume/guest/job/{jid}?guest_id={_uuid.uuid4()}")
        assert wrong.status_code == 404

    def test_onboard_rejects_bad_files(self, guest_db, resume_app):
        client = resume_app.test_client()
        resp = client.post(
            "/api/resume/guest/onboard",
            data={"file": (io.BytesIO(b"MZ not a pdf"), "resume.exe")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400

    def test_tailor_rejects_v1_job_payload(self, guest_db, resume_app):
        s = _new_session()
        _attach(s["guest_id"], _structured(), "resume text for tailor " * 4)
        resp = resume_app.test_client().post(
            "/api/resume/guest/tailor",
            json={"guest_id": s["guest_id"],
                  "job": {"title": "SWE", "jd_text": "Python APIs"}},
        )
        assert resp.status_code == 400
        assert "match_id" in resp.get_json()["error"]

    def test_tailor_reserve_retry_and_exhaust_flow(self, guest_db, resume_app):
        s = _new_session()
        _attach(s["guest_id"], _structured(), "resume text for tailor " * 4)
        mids = _serve_matches(s["guest_id"], n=2)
        client = resume_app.test_client()

        first = client.post("/api/resume/guest/tailor",
                            json={"guest_id": s["guest_id"], "match_id": mids[0]})
        assert first.status_code == 202, first.get_data(as_text=True)
        body = first.get_json()
        assert body["job_id"] and body["tailors_used"] == 1 and body["tailors_limit"] == 1
        counter = guest_db.guest_global_counters.find_one(
            {"_id": {"$regex": "^guest_tailors:"}})
        assert counter["count"] == 1
        # The worker payload carries the SERVER-stored JD, never a client one.
        job_row = guest_db.resume_jobs.find_one({"job_id": body["job_id"]})
        assert job_row["payload"]["job"]["jd_text"].startswith("Python APIs")

        # Same-match retry → idempotent existing job, no re-spend.
        retry = client.post("/api/resume/guest/tailor",
                            json={"guest_id": s["guest_id"], "match_id": mids[0]})
        assert retry.status_code == 200
        assert retry.get_json()["existing"] is True
        assert retry.get_json()["job_id"] == body["job_id"]
        assert guest_db.guest_global_counters.find_one(
            {"_id": {"$regex": "^guest_tailors:"}})["count"] == 1

        # A DIFFERENT match after the reservation → 429 quota_exhausted (the
        # remaining matches stay visible; only the capability locks).
        other = client.post("/api/resume/guest/tailor",
                            json={"guest_id": s["guest_id"], "match_id": mids[1]})
        assert other.status_code == 429
        obody = other.get_json()
        assert obody["quota_exhausted"] is True
        assert obody["tailors_used"] == 1 and obody["tailors_limit"] == 1

    def test_tailor_unknown_match_is_404(self, guest_db, resume_app):
        s = _new_session()
        _attach(s["guest_id"], _structured(), "resume text " * 5)
        _serve_matches(s["guest_id"], n=1)
        resp = resume_app.test_client().post(
            "/api/resume/guest/tailor",
            json={"guest_id": s["guest_id"], "match_id": _uuid.uuid4().hex},
        )
        assert resp.status_code == 404

    def test_tailor_global_breaker_releases_reservation(self, guest_db, resume_app):
        pconfig.update_guest_config({"global_daily_guest_tailors": 0}, "admin@test")
        s = _new_session()
        _attach(s["guest_id"], _structured(), "resume text " * 5)
        mids = _serve_matches(s["guest_id"], n=1)
        resp = resume_app.test_client().post(
            "/api/resume/guest/tailor",
            json={"guest_id": s["guest_id"], "match_id": mids[0]},
        )
        assert resp.status_code == 429
        body = resp.get_json()
        assert body["quota_exhausted"] is True and body.get("global") is True
        # The subject slot was refunded — signup later still carries 0 used.
        doc = _subject_doc(guest_db, s["guest_id"])
        assert doc["tailors_used"] == 0 and doc["reservation"] is None

    def test_session_device_recognition_leaks_nothing(self, guest_db, resume_app):
        s = _new_session(fp="fp_return_visitor")
        _attach(s["guest_id"], _structured(), "text " * 20, fp="fp_return_visitor")
        client = resume_app.test_client()

        known = client.get(f"/api/resume/guest/session?guest_id={s['guest_id']}")
        assert known.status_code == 200
        body = known.get_json()
        assert body["exists"] and body["has_resume"] and body["tailors_limit"] == 1
        # Owning guest_id + parsed resume → the (own) name is returned.
        assert body["recognized_name"] == "jordan alvarez"

        # guest_id lost but fingerprint matches: recognized WITHOUT any name
        # key (v2 omits it entirely — no empty-string tombstone) and with
        # claimed pinned false (no account-state oracle on a guessable signal).
        recog = client.get(
            "/api/resume/guest/session?fingerprint_hash=fp_return_visitor")
        assert recog.status_code == 200
        rbody = recog.get_json()
        assert rbody == {"exists": False, "recognized": True, "claimed": False}
        assert "recognized_name" not in rbody

    def test_session_recognizes_device_token(self, guest_db, resume_app):
        token = guest_service.mint_device_token()
        _new_session(device_token_hash=guest_service.hash_device_token(token))
        resp = resume_app.test_client().get(
            f"/api/resume/guest/session?device_token={token}")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["exists"] is False and body["recognized"] is True
        assert "recognized_name" not in body

        # An unknown token is simply not recognized (no oracle, no error).
        miss = resume_app.test_client().get(
            f"/api/resume/guest/session?device_token={guest_service.mint_device_token()}")
        assert miss.get_json() == {"exists": False, "recognized": False}

    def test_claim_route_requires_jwt(self, guest_db, resume_app):
        resp = resume_app.test_client().post(
            "/api/resume/guest/claim", json={"guest_id": "x"})
        assert resp.status_code == 401  # the ONLY legitimate 401 (missing JWT)

    def test_claim_route_happy_path_v2_shape(self, guest_db, resume_app):
        s = _new_session()
        _attach(s["guest_id"], _structured(), "claimable resume " * 5)
        from flask_jwt_extended import create_access_token
        with resume_app.app_context():
            token = create_access_token(identity="claimer@example.com")
        resp = resume_app.test_client().post(
            "/api/resume/guest/claim",
            headers={"Authorization": f"Bearer {token}"},
            json={"guest_id": s["guest_id"]},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["claimed"] is True and body["already_claimed"] is False
        assert {"resume_imported", "records_transferred", "tailors_carried",
                "daily_tailors_used", "daily_tailors_limit"} <= set(body)


# ===========================================================================
# Download capability tokens + streaming (shared renderer)
# ===========================================================================

class TestGuestDownloads:
    def _record_for(self, guest_db, guest_id):
        record_id = str(_uuid.uuid4())
        guest_db.tailoring_records.insert_one({
            "record_id": record_id,
            "user_email": None,
            "guest_id": guest_id,
            "tailored_resume": {"contact": {"name": "Jordan Alvarez"},
                                "summary": "Backend engineer."},
            "jd_analysis": {"job_title": "Backend Engineer", "company": "Acme"},
            "source": "guest_tailor",
        })
        return record_id

    def test_mint_and_stream_pdf(self, guest_db, resume_app, monkeypatch):
        import services.resume_service as rs
        s = _new_session()
        rid = self._record_for(guest_db, s["guest_id"])
        client = resume_app.test_client()

        resp = client.post("/api/resume/guest/download",
                           json={"guest_id": s["guest_id"], "record_id": rid,
                                 "format": "pdf"})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = resp.get_json()
        assert body["token"].startswith("v1.")
        assert body["expires_at"] and body["formats"] == ["pdf", "docx"]

        # Streaming goes through the EXISTING shared renderer (patched here —
        # its output quality is covered by the renderer suites).
        svc = rs.get_resume_service()
        monkeypatch.setattr(svc.renderer, "generate_pdf",
                            lambda tailored: b"%PDF-1.4 rendered " + b"x" * 200)
        got = client.get(f"/api/resume/guest/download/{body['token']}")
        assert got.status_code == 200
        assert got.mimetype == "application/pdf"
        assert got.data.startswith(b"%PDF-1.4 rendered")

        # Unlimited re-issue, never consumes quota.
        again = client.post("/api/resume/guest/download",
                            json={"guest_id": s["guest_id"], "record_id": rid,
                                  "format": "docx"})
        assert again.status_code == 200
        assert guest_service.tailor_quota_state(s["guest_id"])["tailors_used"] == 0

    def test_other_guests_record_is_generic_404(self, guest_db, resume_app):
        owner = _new_session()
        rid = self._record_for(guest_db, owner["guest_id"])
        attacker = _new_session()
        resp = resume_app.test_client().post(
            "/api/resume/guest/download",
            json={"guest_id": attacker["guest_id"], "record_id": rid,
                  "format": "pdf"})
        assert resp.status_code == 404
        # Identical body to a nonexistent record — no existence oracle.
        missing = resume_app.test_client().post(
            "/api/resume/guest/download",
            json={"guest_id": attacker["guest_id"],
                  "record_id": str(_uuid.uuid4()), "format": "pdf"})
        assert missing.status_code == 404
        assert resp.get_json() == missing.get_json()

    def test_forged_token_404_expired_token_410(self, guest_db, resume_app):
        s = _new_session()
        rid = self._record_for(guest_db, s["guest_id"])
        client = resume_app.test_client()

        token, _exp = guest_service.mint_download_token(rid, s["guest_id"], "pdf")
        head, payload, sig = token.split(".")
        forged = f"{head}.{payload}.{'A' * len(sig)}"
        assert client.get(f"/api/resume/guest/download/{forged}").status_code == 404
        assert client.get("/api/resume/guest/download/garbage").status_code == 404

        expired, _ = guest_service.mint_download_token(
            rid, s["guest_id"], "pdf", ttl_seconds=-5)
        assert client.get(f"/api/resume/guest/download/{expired}").status_code == 410

    def test_mint_fails_closed_without_secret(self, guest_db, resume_app, monkeypatch):
        monkeypatch.delenv("GUEST_DOWNLOAD_TOKEN_SECRET", raising=False)
        monkeypatch.setenv("ENVIRONMENT", "production")  # dev has a fallback
        monkeypatch.setenv("GUEST_ONBOARDING_ENABLED", "true")  # keep the flag on in "prod"
        s = _new_session()
        rid = self._record_for(guest_db, s["guest_id"])
        resp = resume_app.test_client().post(
            "/api/resume/guest/download",
            json={"guest_id": s["guest_id"], "record_id": rid, "format": "pdf"})
        assert resp.status_code == 503        # capacity-style, NEVER 401
        assert resp.get_json()["error"] == "downloads_unavailable"


# ===========================================================================
# Guest parse job (async worker branch) + matches computation
# ===========================================================================

class TestGuestJobs:
    def test_guest_parse_job_updates_session_no_plaintext(self, guest_db, monkeypatch):
        import services.resume_service as rs
        s = _new_session(fp="fp_parse")
        payload = {
            "file_base64": "ignored", "mime_type": "application/pdf",
            "filename": "resume.pdf", "guest_id": s["guest_id"],
            "fingerprint_hash": "fp_parse",
        }
        job_id = guest_service.create_guest_job("guest_parse", payload, s["guest_id"])
        svc = rs.get_resume_service()
        monkeypatch.setattr(
            svc.parser, "parse_to_structured",
            lambda *a, **k: (_structured(), "Jordan Alvarez raw resume text " * 5),
        )
        rs._process_job(job_id, "guest_parse", payload)

        job = guest_service.get_guest_job(job_id, s["guest_id"])
        assert job["status"] == "completed"
        assert job["result"]["parsed_resume"]["structured"]["contact"]["name"] == "Jordan Alvarez"
        guest = job["result"]["guest"]
        assert guest["tailors_limit"] == 1 and guest["tailors_used"] == 0
        # Owning-guest_id + parsed resume → own name may travel.
        assert guest["recognized_name"] == "jordan alvarez"

        # v3: the session stores only the resume_id ref; parsed content lives
        # on the guest-owned resume_assets doc (get_session hydrates the view).
        session = guest_db.guest_sessions.find_one({"guest_id": s["guest_id"]})
        assert session.get("resume") in (None, {})  # no embedded resume blob
        assert session["resume_id"]
        asset = guest_db.resume_assets.find_one({"resume_id": session["resume_id"]})
        assert asset["structured"] and asset["guest_id"] == s["guest_id"]
        assert guest_service.get_session(s["guest_id"])["resume"]["structured"]
        ident = session["identity"]
        assert ident["email_hmacs"] and ident["email_hmacs"][0].startswith("v1:")
        # v1 plaintext identity fields are GONE from new writes.
        assert "emails" not in ident and "phones" not in ident
        assert "resume_hashes" not in ident

    def test_compute_matches_caps_and_v2_card_fields(self, guest_db, monkeypatch):
        pconfig.update_guest_config(
            {"guest_jobs_min_score": 20, "guest_jobs_max": 2}, "admin@test")
        s = _new_session()
        _attach(s["guest_id"],
                _structured(skills=["Python", "Go", "SQL", "AWS"]),
                "match me " * 10)

        rows = [
            {"title": "Python Backend Engineer", "company": "A", "location": "US",
             "apply_link": "https://a.example/1", "source": "Greenhouse",
             "days_ago": 1, "description": "Python Go SQL AWS backend"},
            {"title": "Go Platform Engineer", "company": "B", "location": "US",
             "apply_link": "https://b.example/2", "source": "Workday Direct",
             "days_ago": 2, "description": "Go SQL AWS"},
            {"title": "Python Data Engineer", "company": "C", "location": "US",
             "apply_link": "https://c.example/3", "source": "Lever",
             "days_ago": 3, "description": "Python SQL"},
            {"title": "Marketing Manager", "company": "D", "location": "US",
             "apply_link": "https://d.example/4", "source": "Ashby",
             "days_ago": 4, "description": "brand campaigns"},
        ]
        monkeypatch.setattr(guest_service, "_fetch_free_jobs", lambda structured: rows)

        result = guest_service.compute_guest_matches(s["guest_id"])
        assert result["cached"] is False
        assert len(result["jobs"]) == 2  # capped at guest_jobs_max
        scores = [j["match_score"] for j in result["jobs"]]
        assert scores == sorted(scores, reverse=True)
        for card in result["jobs"]:
            assert card["match_score"] >= 20
            assert "description" not in card              # card whitelist holds
            assert guest_service.validate_match_id(card["match_id"])
            assert card["why"].startswith("Estimated match")
            assert card["sponsorship"] is None            # no filings ingested → null

        cached = guest_service.get_cached_matches(s["guest_id"])
        assert cached and cached["cached"] is True
        assert len(cached["jobs"]) == 2
        # The tailorable JD source is resolvable per served match_id.
        src = guest_service.find_cached_match(
            s["guest_id"], result["jobs"][0]["match_id"])
        assert src and "Python" in src["jd_text"]

    def test_sponsorship_uses_public_filings_language(self, guest_db, monkeypatch):
        pconfig.update_guest_config({"guest_jobs_min_score": 0}, "admin@test")
        guest_db.sponsorship_signals.insert_one({
            "employer_norm": "acme", "employer_display": "Acme",
            "lca_filings_5y": 120, "approvals_5y": 63, "denials_5y": 2,
            "top_titles": ["Backend Engineer"],
        })
        s = _new_session()
        _attach(s["guest_id"], _structured(skills=["Python"]), "sponsor test " * 5)
        rows = [{"title": "Python Engineer", "company": "Acme", "location": "US",
                 "apply_link": "https://a.example/1", "source": "Greenhouse",
                 "days_ago": 1, "description": "Python"}]
        monkeypatch.setattr(guest_service, "_fetch_free_jobs", lambda structured: rows)
        result = guest_service.compute_guest_matches(s["guest_id"])
        sp = result["jobs"][0]["sponsorship"]
        assert sp is not None
        assert sp["signal"] in ("low", "moderate", "strong")
        assert sp["note"].startswith("Signals from public filings:")
        assert "verified" not in sp["note"].lower()
        assert "guarantee" in sp["note"].lower()

    def test_server_never_returns_more_than_guest_jobs_max(self, guest_db):
        s = _new_session()
        _attach(s["guest_id"], _structured(), "cap test resume " * 5)
        _serve_matches(s["guest_id"], n=8)  # over-full cache (e.g. admin lowered the cap)
        cached = guest_service.get_cached_matches(s["guest_id"])
        assert len(cached["jobs"]) == 5     # default guest_jobs_max — hard cap

    def test_subject_cache_survives_fresh_guest_id(self, guest_db, monkeypatch):
        # Same person re-uploads under a NEW guest_id: the subject-level cache
        # serves the same 5 matches and the matches breaker is NOT re-spent.
        import services.resume_service as rs
        monkeypatch.setattr(rs.ResumeService, "invoke_async",
                            staticmethod(lambda *a, **k: None))
        app = load_service_app("jobs")
        client = app.test_client()
        pconfig.update_guest_config({"global_daily_guest_matches": 1}, "admin@test")

        raw = "subject cache resume " * 5
        a = _new_session()
        _attach(a["guest_id"], _structured(), raw)
        rows = [{"title": "Python Engineer", "company": "A", "location": "US",
                 "apply_link": "https://a.example/1", "source": "Greenhouse",
                 "days_ago": 1, "description": "Python Go SQL"}]
        monkeypatch.setattr(guest_service, "_fetch_free_jobs", lambda structured: rows)

        first = client.post("/api/jobs/guest/matches", json={"guest_id": a["guest_id"]})
        assert first.status_code == 202     # miss: spends the single slot
        guest_service.compute_guest_matches(a["guest_id"])  # worker body

        b = _new_session(fp="fp_new_browser")
        _attach(b["guest_id"], _structured(), raw, fp="fp_new_browser")  # merges
        hit = client.post("/api/jobs/guest/matches", json={"guest_id": b["guest_id"]})
        assert hit.status_code == 200
        assert hit.get_json()["cached"] is True
        counter = guest_db.guest_global_counters.find_one(
            {"_id": {"$regex": "^guest_matches:"}})
        assert counter["count"] == 1        # never re-spent

    def test_jobs_route_returns_cache_or_job_id(self, guest_db, monkeypatch):
        import services.resume_service as rs
        monkeypatch.setattr(rs.ResumeService, "invoke_async",
                            staticmethod(lambda *a, **k: None))
        app = load_service_app("jobs")
        client = app.test_client()

        s = _new_session()
        _attach(s["guest_id"], _structured(), "resume for matches " * 5)

        first = client.post("/api/jobs/guest/matches", json={"guest_id": s["guest_id"]})
        assert first.status_code == 202
        assert first.get_json()["job_id"]

        # Same-day cache present -> synchronous 200 with cached rows.
        guest_service._store_matches(
            s["guest_id"],
            [{"title": "T", "match_score": 90, "days_ago": 1}], 20)
        second = client.post("/api/jobs/guest/matches", json={"guest_id": s["guest_id"]})
        assert second.status_code == 200
        body = second.get_json()
        assert body["cached"] is True and body["jobs"][0]["match_score"] == 90

    def test_jobs_route_requires_resume(self, guest_db):
        app = load_service_app("jobs")
        s = _new_session()
        resp = app.test_client().post(
            "/api/jobs/guest/matches", json={"guest_id": s["guest_id"]})
        assert resp.status_code == 400

    def test_jobs_route_validates_guest_id(self, guest_db):
        app = load_service_app("jobs")
        resp = app.test_client().post(
            "/api/jobs/guest/matches",
            json={"guest_id": {"$ne": None}})  # NoSQL-injection shape
        assert resp.status_code == 400


# ===========================================================================
# Progressive challenge seam (no provider ships in v2)
# ===========================================================================

class TestChallengeSeam:
    def test_default_provider_none_is_noop(self, guest_db, monkeypatch):
        monkeypatch.setenv("GUEST_IP_HASH_SALT", "salt")
        ip_hash = guest_service.hash_ip("203.0.113.7")
        for _ in range(50):
            assert guest_challenge_service.maybe_challenge("tailor", ip_hash) is None
        # No-op means no counters either.
        assert guest_db.guest_challenge_velocity.count_documents({}) == 0

    def test_velocity_triggers_after_threshold_never_on_first(self, guest_db, monkeypatch):
        monkeypatch.setenv("GUEST_IP_HASH_SALT", "salt")
        monkeypatch.setenv("GUEST_CHALLENGE_PROVIDER", "turnstile")
        monkeypatch.setenv("GUEST_CHALLENGE_SECRET", "unit-test-challenge-secret")
        monkeypatch.setenv("GUEST_CHALLENGE_THRESHOLD", "2")
        ip_hash = guest_service.hash_ip("203.0.113.7")
        assert guest_challenge_service.maybe_challenge("onboard", ip_hash) is None  # 1st
        assert guest_challenge_service.maybe_challenge("onboard", ip_hash) is None  # 2nd
        third = guest_challenge_service.maybe_challenge("onboard", ip_hash)
        # A challenge is now ISSUED (stored, expiring, one-time, bound) and
        # carries the challenge_id the client redeems at /challenge/verify.
        assert third["provider"] == "turnstile"
        assert len(third["challenge_id"]) == 32

    def test_no_ip_hash_never_blocks(self, guest_db, monkeypatch):
        monkeypatch.setenv("GUEST_CHALLENGE_PROVIDER", "turnstile")
        monkeypatch.setenv("GUEST_CHALLENGE_SECRET", "unit-test-challenge-secret")
        assert guest_challenge_service.maybe_challenge("tailor", None) is None

    def test_matches_route_returns_challenge_before_spend(self, guest_db, monkeypatch):
        import services.resume_service as rs
        monkeypatch.setattr(rs.ResumeService, "invoke_async",
                            staticmethod(lambda *a, **k: None))
        monkeypatch.setenv("GUEST_IP_HASH_SALT", "salt")
        monkeypatch.setenv("GUEST_CHALLENGE_PROVIDER", "turnstile")
        monkeypatch.setenv("GUEST_CHALLENGE_SECRET", "unit-test-challenge-secret")
        monkeypatch.setenv("GUEST_CHALLENGE_THRESHOLD", "1")
        app = load_service_app("jobs")
        client = app.test_client()
        s = _new_session()
        _attach(s["guest_id"], _structured(), "challenge resume " * 5)

        first = client.post("/api/jobs/guest/matches", json={"guest_id": s["guest_id"]})
        assert first.status_code == 202     # normal first attempt: NEVER challenged
        over = client.post("/api/jobs/guest/matches", json={"guest_id": s["guest_id"]})
        assert over.status_code == 429
        body = over.get_json()
        assert body["error"] == "challenge_required" and body["provider"] == "turnstile"
        # Budget was NOT spent for the challenged attempt.
        counter = guest_db.guest_global_counters.find_one(
            {"_id": {"$regex": "^guest_matches:"}})
        assert counter["count"] == 1


# ===========================================================================
# Retention cleanup — deletes ONLY expired unclaimed data
# ===========================================================================

class TestRetentionCleanup:
    def test_deletes_only_expired_unclaimed(self, guest_db):
        old = _new_session()
        fresh = _new_session()
        claimed = _new_session()
        cutoff_past = datetime.now(timezone.utc) - timedelta(days=40)
        guest_db.guest_sessions.update_one(
            {"guest_id": old["guest_id"]}, {"$set": {"last_seen_at": cutoff_past}})
        guest_db.guest_sessions.update_one(
            {"guest_id": claimed["guest_id"]},
            {"$set": {"last_seen_at": cutoff_past, "claimed_by": "kept@example.com"}})
        guest_db.tailoring_records.insert_many([
            {"record_id": "r-old", "guest_id": old["guest_id"], "user_email": None},
            {"record_id": "r-claimed", "guest_id": claimed["guest_id"],
             "user_email": "kept@example.com"},
        ])
        guest_db.resume_jobs.insert_one(
            {"job_id": "j-old", "guest_id": old["guest_id"], "status": "completed"})

        result = guest_service.run_retention_cleanup()
        assert result["skipped"] is False and result["retention_days"] == 30
        assert result["sessions_deleted"] == 1
        assert result["records_deleted"] == 1
        assert result["jobs_deleted"] == 1

        assert guest_db.guest_sessions.find_one({"guest_id": old["guest_id"]}) is None
        assert guest_db.guest_sessions.find_one({"guest_id": fresh["guest_id"]})
        assert guest_db.guest_sessions.find_one({"guest_id": claimed["guest_id"]})
        assert guest_db.tailoring_records.find_one({"record_id": "r-claimed"})
        assert guest_db.tailoring_records.find_one({"record_id": "r-old"}) is None

    def test_retention_zero_keeps_forever(self, guest_db):
        pconfig.update_guest_config({"retention_days": 0}, "admin@test")
        old = _new_session()
        guest_db.guest_sessions.update_one(
            {"guest_id": old["guest_id"]},
            {"$set": {"last_seen_at": datetime.now(timezone.utc) - timedelta(days=999)}})
        result = guest_service.run_retention_cleanup()
        assert result["skipped"] is True
        assert guest_db.guest_sessions.find_one({"guest_id": old["guest_id"]})

    def test_worker_branch_completes_job(self, guest_db):
        import services.resume_service as rs
        svc = rs.get_resume_service()
        job_id = svc.create_job("guest_retention_cleanup", {}, "admin@test")
        rs._process_job(job_id, "guest_retention_cleanup", {})
        job = svc.get_job(job_id, "admin@test")
        assert job["status"] == "completed"
        assert job["result"]["retention_days"] == 30


# ===========================================================================
# get_client_ip — rightmost-XFF / spoof resistance (security review §1)
# ===========================================================================

class _FakeHeaders:
    def __init__(self, xff):
        self._xff = list(xff or [])

    def getlist(self, name):
        return self._xff if name == "X-Forwarded-For" else []


class _FakeRequest:
    """Minimal request stub: headers.getlist + remote_addr + WSGI environ."""

    def __init__(self, xff=None, remote_addr="", source_ip=None, v1_source_ip=None):
        self.headers = _FakeHeaders(xff)
        self.remote_addr = remote_addr
        self.environ = {}
        if source_ip:
            self.environ["apig_wsgi.request_context"] = {"http": {"sourceIp": source_ip}}
        elif v1_source_ip:
            self.environ["apig_wsgi.request_context"] = {"identity": {"sourceIp": v1_source_ip}}


class TestClientIp:
    @pytest.fixture(autouse=True)
    def _default_hops(self, monkeypatch):
        monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)

    def test_normal_single_hop_request_unchanged(self):
        # No X-Forwarded-For (local dev / tests / direct calls): identical to
        # the old behavior — remote_addr. Existing logged-in throttling keys
        # do not move.
        from utils.security import get_client_ip
        assert get_client_ip(_FakeRequest(remote_addr="203.0.113.7")) == "203.0.113.7"

    def test_prod_cloudfront_chain_normal_user(self):
        # client -> CloudFront (appends client) -> API GW (appends CF edge).
        # Same key the old leftmost parse produced for a non-spoofing user.
        from utils.security import get_client_ip
        req = _FakeRequest(
            xff=["203.0.113.7", "130.176.5.10"], source_ip="130.176.5.10"
        )
        assert get_client_ip(req) == "203.0.113.7"

    def test_spoofed_xff_prefix_is_ignored(self):
        # Attacker sends X-Forwarded-For: 9.9.9.9 — old code keyed the rate
        # limiter on the attacker-chosen value; now the real client wins.
        from utils.security import get_client_ip
        req = _FakeRequest(
            xff=["9.9.9.9", "203.0.113.7", "130.176.5.10"],
            source_ip="130.176.5.10",
        )
        assert get_client_ip(req) == "203.0.113.7"

    def test_direct_api_gateway_normal_user(self):
        # No CloudFront: API GW appends its peer (the client) to XFF and the
        # request-context sourceIp IS the client.
        from utils.security import get_client_ip
        req = _FakeRequest(xff=["203.0.113.7"], source_ip="203.0.113.7")
        assert get_client_ip(req) == "203.0.113.7"

    def test_v1_identity_source_ip_supported(self):
        from utils.security import get_client_ip
        req = _FakeRequest(xff=["203.0.113.7"], v1_source_ip="203.0.113.7")
        assert get_client_ip(req) == "203.0.113.7"

    def test_hops_zero_trusts_only_connection_peer(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXY_HOPS", "0")
        from utils.security import get_client_ip
        req = _FakeRequest(xff=["9.9.9.9"], source_ip="203.0.113.7")
        assert get_client_ip(req) == "203.0.113.7"

    def test_rightmost_xff_without_apig_context(self):
        # Local/test-server topology: rightmost XFF entry wins, junk on the
        # left is never honored.
        from utils.security import get_client_ip
        req = _FakeRequest(xff=["9.9.9.9", "198.51.100.4"], remote_addr="127.0.0.1")
        assert get_client_ip(req) == "198.51.100.4"

    def test_port_decorated_token_normalized(self):
        from utils.security import get_client_ip
        req = _FakeRequest(xff=["9.9.9.9", "203.0.113.7:4711"], remote_addr="127.0.0.1")
        assert get_client_ip(req) == "203.0.113.7"

    def test_invalid_trusted_token_falls_back_to_peer_not_left(self):
        # The entry at the trusted position isn't an IP: NEVER walk left into
        # attacker-controlled territory — fall back to the connection peer.
        from utils.security import get_client_ip
        req = _FakeRequest(xff=["9.9.9.9", "not-an-ip"], remote_addr="127.0.0.1")
        assert get_client_ip(req) == "127.0.0.1"

    def test_logged_in_throttling_not_tightened(self):
        # A normal user hammering under the limit stays un-throttled and two
        # distinct clients keep independent buckets (keying shape unchanged).
        from utils.security import RateLimiter, get_client_ip
        limiter = RateLimiter()
        ip_a = get_client_ip(_FakeRequest(remote_addr="203.0.113.7"))
        ip_b = get_client_ip(
            _FakeRequest(xff=["198.51.100.4", "130.176.5.10"], source_ip="130.176.5.10")
        )
        assert ip_a == "203.0.113.7" and ip_b == "198.51.100.4"
        for _ in range(9):
            assert limiter.is_rate_limited(f"auth:login:{ip_a}", max_requests=10) is False
        assert limiter.is_rate_limited(f"auth:login:{ip_b}", max_requests=10) is False


# ===========================================================================
# ip_hash salt (security review §5) — fail closed, never a reversible hash
# ===========================================================================

class TestIpHashSalt:
    def test_hash_ip_is_none_when_salt_unset(self, guest_db, monkeypatch):
        monkeypatch.delenv("GUEST_IP_HASH_SALT", raising=False)
        assert guest_service.hash_ip("203.0.113.7") is None
        s = guest_service.create_or_get_session(
            None, None, guest_service.hash_ip("203.0.113.7"))
        doc = guest_db.guest_sessions.find_one({"guest_id": s["guest_id"]})
        assert doc["ip_hash"] is None  # stored null — nothing brute-forceable

    def test_hash_ip_hashes_with_salt(self, guest_db, monkeypatch):
        monkeypatch.setenv("GUEST_IP_HASH_SALT", "unit-test-salt")
        h = guest_service.hash_ip("203.0.113.7")
        assert h and len(h) == 64 and "203.0.113.7" not in h
        assert h != guest_service.hash_ip("198.51.100.4")


# ===========================================================================
# Global daily caps: parse (onboard) + matches (security review §1/§6)
# ===========================================================================

class TestGlobalCapacityCaps:
    def _post_onboard(self, client):
        return client.post(
            "/api/resume/guest/onboard",
            data={"file": (io.BytesIO(PDF_BYTES), "resume.pdf"),
                  "fingerprint_hash": "fp_cap_test"},
            content_type="multipart/form-data",
        )

    def test_onboard_global_parse_cap(self, guest_db, resume_app):
        pconfig.update_guest_config({"global_daily_guest_parses": 1}, "admin@test")
        client = resume_app.test_client()
        assert self._post_onboard(client).status_code == 202
        over = self._post_onboard(client)
        assert over.status_code == 429
        body = over.get_json()
        assert body["error"] == "capacity" and body["global"] is True
        assert body["message"]
        counter = guest_db.guest_global_counters.find_one(
            {"_id": {"$regex": "^guest_parses:"}})
        assert counter["count"] == 1  # over-limit attempt rolled back

    def test_onboard_dispatch_failure_releases_reservation(
            self, guest_db, resume_app, monkeypatch):
        import services.resume_service as rs

        def boom(*a, **k):
            raise RuntimeError("lambda invoke failed")

        monkeypatch.setattr(rs.ResumeService, "invoke_async", staticmethod(boom))
        resp = self._post_onboard(resume_app.test_client())
        assert resp.status_code == 500
        counter = guest_db.guest_global_counters.find_one(
            {"_id": {"$regex": "^guest_parses:"}})
        assert (counter or {}).get("count", 0) == 0  # slot freed

    def test_matches_global_cap_and_cache_exemption(self, guest_db, monkeypatch):
        import services.resume_service as rs
        monkeypatch.setattr(rs.ResumeService, "invoke_async",
                            staticmethod(lambda *a, **k: None))
        app = load_service_app("jobs")
        client = app.test_client()

        pconfig.update_guest_config({"global_daily_guest_matches": 1}, "admin@test")
        a = _new_session()
        _attach(a["guest_id"], _structured(), "matches cap resume A " * 5)
        b = _new_session(fp="fp_other")
        _attach(b["guest_id"],
                _structured(name="Sam Lee", email="sam.lee@example.com",
                            phone="(777) 555-0000"),
                "matches cap resume B totally different " * 5, fp="fp_other")

        first = client.post("/api/jobs/guest/matches", json={"guest_id": a["guest_id"]})
        assert first.status_code == 202  # cache miss consumes the 1 slot

        over = client.post("/api/jobs/guest/matches", json={"guest_id": b["guest_id"]})
        assert over.status_code == 429
        body = over.get_json()
        assert body["error"] == "capacity" and body["global"] is True

        # Same-day cache HIT must serve even with the budget exhausted —
        # a cache hit never consumes the global scrape budget.
        guest_service._store_matches(
            a["guest_id"], [{"title": "T", "match_score": 90}], 20)
        hit = client.post("/api/jobs/guest/matches", json={"guest_id": a["guest_id"]})
        assert hit.status_code == 200 and hit.get_json()["cached"] is True

        counter = guest_db.guest_global_counters.find_one(
            {"_id": {"$regex": "^guest_matches:"}})
        assert counter["count"] == 1


# ===========================================================================
# Claim race (security review §7) — the loser transfers NOTHING
# ===========================================================================

class TestClaimRace:
    def test_losing_concurrent_claim_transfers_nothing(self, guest_db, monkeypatch):
        s = _new_session()
        _attach(s["guest_id"], _structured(), "raced claim resume " * 5)
        gid = s["guest_id"]
        guest_db.tailoring_records.insert_one({
            "record_id": "rec-race", "user_email": None, "guest_id": gid,
            "tailored_resume": {"summary": "x"}, "source": "guest_tailor",
        })
        winner, loser = "winner@example.com", "loser@example.com"
        # The winner's flip has already landed...
        guest_db.guest_sessions.update_one(
            {"guest_id": gid},
            {"$set": {"claimed_by": winner,
                      "claimed_at": datetime.now(timezone.utc)}})

        # ...but the loser's pre-check read a STALE (unclaimed) doc — the
        # classic interleaving. Its atomic flip must lose and it must not
        # touch records/resume/usage.
        real_coll = guest_db.guest_sessions

        class StaleFirstRead:
            def __init__(self, real):
                self._real = real
                self._first = True

            def find_one(self, *a, **k):
                doc = self._real.find_one(*a, **k)
                if self._first and doc is not None:
                    self._first = False
                    doc = dict(doc)
                    doc["claimed_by"] = None  # stale pre-check view
                return doc

            def __getattr__(self, name):
                return getattr(self._real, name)

        proxy = StaleFirstRead(real_coll)
        monkeypatch.setattr(guest_service, "_sessions", lambda: proxy)

        res = guest_service.claim_session(gid, loser)
        assert res.get("error") == "already_claimed"

        rec = guest_db.tailoring_records.find_one({"record_id": "rec-race"})
        assert rec["user_email"] is None                      # no PII transfer
        assert guest_db.resume_assets.find_one({"user_email": loser}) is None
        from services import usage_service
        assert usage_service.get_usage(loser)["used"] == 0
        session = guest_db.guest_sessions.find_one({"guest_id": gid})
        assert session["claimed_by"] == winner                # winner keeps it

    def test_repeat_claim_same_account_no_recopy(self, guest_db):
        # Idempotency: the same account claiming twice re-copies nothing.
        s = _new_session()
        _attach(s["guest_id"], _structured(), "repeat claim resume " * 5)
        email = "repeat@example.com"
        first = guest_service.claim_session(s["guest_id"], email)
        assert first["already_claimed"] is False and first["resume_imported"] is True
        before = guest_db.resume_assets.count_documents({"user_email": email})

        second = guest_service.claim_session(s["guest_id"], email)
        assert second["claimed"] is True and second["already_claimed"] is True
        assert second["resume_imported"] is False and second["records_transferred"] == 0
        assert guest_db.resume_assets.count_documents({"user_email": email}) == before


# ===========================================================================
# Job-card link scheme guard (security review §8)
# ===========================================================================

class TestJobCardUrlScheme:
    def test_javascript_and_data_urls_never_reach_cards(self, guest_db, monkeypatch):
        pconfig.update_guest_config(
            {"guest_jobs_min_score": 0, "guest_jobs_max": 10}, "admin@test")
        s = _new_session()
        _attach(s["guest_id"],
                _structured(skills=["Python", "Go", "SQL", "AWS"]),
                "scheme guard resume " * 5)

        rows = [
            {"title": "Python Engineer", "company": "A", "location": "US",
             "apply_link": "javascript:alert(document.cookie)",
             "url": "https://ok.example/apply/1", "days_ago": 1,
             "source": "Greenhouse", "description": "Python Go SQL AWS"},
            {"title": "Go Engineer", "company": "B", "location": "US",
             "apply_link": "https://good.example/2", "days_ago": 1,
             "source": "Lever", "description": "Go SQL AWS"},
            {"title": "SQL Engineer", "company": "C", "location": "US",
             "apply_link": "data:text/html,<script>alert(1)</script>",
             "url": "vbscript:msgbox(1)", "days_ago": 1,
             "source": "Ashby", "description": "SQL Python"},
        ]
        monkeypatch.setattr(guest_service, "_fetch_free_jobs", lambda structured: rows)
        result = guest_service.compute_guest_matches(s["guest_id"])
        cards = {c["title"]: c for c in result["jobs"]}

        assert "apply_link" not in cards["Python Engineer"]        # javascript: dropped
        assert cards["Python Engineer"]["url"] == "https://ok.example/apply/1"
        assert cards["Go Engineer"]["apply_link"] == "https://good.example/2"
        assert "apply_link" not in cards["SQL Engineer"]           # data: dropped
        assert "url" not in cards["SQL Engineer"]                  # vbscript: dropped


# ===========================================================================
# v2 acceptance additions — ambiguous identities (acceptance #1, #2, #3)
# ===========================================================================

class TestIdentityAmbiguityV2:
    """Name-only, fingerprint-only, and name+device (token AND fingerprint)
    matches must never merge quota subjects — ambiguity stays separate."""

    def test_name_only_namesakes_keep_separate_quota_subjects_and_trials(self, guest_db):
        # Acceptance #1: same normalized name, nothing else shared. A has
        # already SPENT the single trial — the namesake must keep their own.
        a = _new_session()
        _attach(a["guest_id"], _structured(), "first jordan resume " * 5)
        assert guest_service.try_reserve_tailor(a["guest_id"], _uuid.uuid4().hex)["reserved"]

        namesake = _structured(email="other.jordan@elsewhere.org", phone="(444) 222-9999")
        b = _new_session()
        rec = _attach(b["guest_id"], namesake, "a different jordan entirely " * 5)
        assert rec["recognized"] is False

        a_doc = guest_db.guest_sessions.find_one({"guest_id": a["guest_id"]})
        b_doc = guest_db.guest_sessions.find_one({"guest_id": b["guest_id"]})
        assert b_doc["merged_into"] is None
        assert b_doc["quota_subject_id"] != a_doc["quota_subject_id"]
        # The namesake's own untouched trial still reserves.
        assert guest_service.try_reserve_tailor(b["guest_id"], _uuid.uuid4().hex)["reserved"]
        assert guest_service.tailor_quota_state(a["guest_id"])["tailors_used"] == 1
        assert guest_service.tailor_quota_state(b["guest_id"])["tailors_used"] == 1

    def test_fingerprint_only_match_never_merges_subjects(self, guest_db):
        # Acceptance #2 (merge half): same fingerprint, different person —
        # ONE weak (device) signal type → no merge, separate subjects.
        a = _new_session(fp="fp_lab_pc")
        _attach(a["guest_id"], _structured(), "person one text " * 5, fp="fp_lab_pc")
        b = _new_session(fp="fp_lab_pc")
        rec = _attach(
            b["guest_id"],
            _structured(name="Sam Lee", email="sam.lee@other.org", phone="(444) 111-2222"),
            "person two text wholly different " * 5,
            fp="fp_lab_pc",
        )
        assert rec["recognized"] is False
        a_doc = guest_db.guest_sessions.find_one({"guest_id": a["guest_id"]})
        b_doc = guest_db.guest_sessions.find_one({"guest_id": b["guest_id"]})
        assert b_doc["merged_into"] is None
        assert b_doc["quota_subject_id"] != a_doc["quota_subject_id"]

    def test_name_plus_device_token_and_fingerprint_never_merges(self, guest_db):
        # Acceptance #3: device_token AND fingerprint AND name — the two
        # device signals count as ONE combined type, name is weak → 2 types
        # but ZERO strong → no merge (shared family computer, two Jordans).
        token = guest_service.mint_device_token()
        dth = guest_service.hash_device_token(token)
        a = _new_session(fp="fp_family", device_token_hash=dth)
        _attach(a["guest_id"], _structured(), "first person resume " * 5, fp="fp_family")
        b = _new_session(fp="fp_family", device_token_hash=dth)
        rec = _attach(
            b["guest_id"],
            _structured(email="other.jordan@elsewhere.org", phone="(444) 222-9999"),
            "second person resume entirely different " * 5,
            fp="fp_family",
        )
        assert rec["recognized"] is False
        a_doc = guest_db.guest_sessions.find_one({"guest_id": a["guest_id"]})
        b_doc = guest_db.guest_sessions.find_one({"guest_id": b["guest_id"]})
        assert b_doc["merged_into"] is None
        assert b_doc["quota_subject_id"] != a_doc["quota_subject_id"]


# ===========================================================================
# v2 acceptance — no name leak in the guest_parse job result (acceptance #2)
# ===========================================================================

class TestParseResultNameLeak:
    def test_guest_parse_on_shared_device_reveals_no_foreign_name(
            self, guest_db, monkeypatch):
        # Session A (Jordan) parsed on this device. Session B — same
        # fingerprint, different person, resume WITHOUT an extractable name.
        # The completed guest_parse result must carry NO recognized_name key
        # (no empty-string tombstone) and must never surface A's name.
        import services.resume_service as rs
        a = _new_session(fp="fp_kiosk")
        _attach(a["guest_id"], _structured(), "jordan owner resume " * 5, fp="fp_kiosk")

        b = _new_session(fp="fp_kiosk")
        payload = {
            "file_base64": "ignored", "mime_type": "application/pdf",
            "filename": "resume.pdf", "guest_id": b["guest_id"],
            "fingerprint_hash": "fp_kiosk",
        }
        job_id = guest_service.create_guest_job("guest_parse", payload, b["guest_id"])
        nameless = _structured(name="", email="sam.lee@elsewhere.org",
                               phone="(444) 111-2222")
        svc = rs.get_resume_service()
        monkeypatch.setattr(
            svc.parser, "parse_to_structured",
            lambda *a2, **k: (nameless, "sam lee second person text " * 5),
        )
        rs._process_job(job_id, "guest_parse", payload)

        job = guest_service.get_guest_job(job_id, b["guest_id"])
        assert job["status"] == "completed"
        guest = job["result"]["guest"]
        assert guest["recognized"] is False           # device-only never merges
        assert "recognized_name" not in guest         # key OMITTED entirely
        assert "jordan" not in json.dumps(job["result"], default=str).lower()
        b_doc = guest_db.guest_sessions.find_one({"guest_id": b["guest_id"]})
        assert b_doc["merged_into"] is None


# ===========================================================================
# v2 acceptance — shared-device artifact isolation (acceptance #4)
# ===========================================================================

class TestSharedDeviceArtifactIsolation:
    """Same device_token + fingerprint, two different people: every artifact
    surface (session summary, job polling, download issuance, claim) is keyed
    STRICTLY to the guest_id capability."""

    def _shared_device_pair(self):
        token = guest_service.mint_device_token()
        dth = guest_service.hash_device_token(token)
        a = _new_session(fp="fp_shared_pc", device_token_hash=dth)
        _attach(a["guest_id"], _structured(), "person one resume " * 5, fp="fp_shared_pc")
        b = _new_session(fp="fp_shared_pc", device_token_hash=dth)
        _attach(
            b["guest_id"],
            _structured(name="Sam Lee", email="sam.lee@other.org", phone="(444) 111-2222"),
            "person two resume different " * 5,
            fp="fp_shared_pc",
        )
        return a["guest_id"], b["guest_id"]

    def test_session_job_and_download_are_guest_id_scoped(self, guest_db, resume_app):
        a_gid, b_gid = self._shared_device_pair()
        client = resume_app.test_client()

        # Session summary: B sees only their OWN resume/name/match-count.
        _serve_matches(a_gid, n=2)                 # A has today's matches; B has none
        resp = client.get(f"/api/resume/guest/session?guest_id={b_gid}")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["exists"] is True
        assert body["recognized_name"] == "sam lee"
        assert body["matched_jobs_today"] == 0     # A's cache invisible to B
        assert "jordan" not in resp.get_data(as_text=True).lower()

        # Job polling: A's job is a generic 404 under B's guest_id.
        job_id = guest_service.create_guest_job("guest_parse", {}, a_gid)
        assert client.get(
            f"/api/resume/guest/job/{job_id}?guest_id={b_gid}").status_code == 404
        assert client.get(
            f"/api/resume/guest/job/{job_id}?guest_id={a_gid}").status_code == 200

        # Download issuance: A's record is a generic 404 for B.
        rid = str(_uuid.uuid4())
        guest_db.tailoring_records.insert_one({
            "record_id": rid, "guest_id": a_gid, "user_email": None,
            "tailored_resume": {"summary": "x"}, "source": "guest_tailor",
        })
        denied = client.post(
            "/api/resume/guest/download",
            json={"guest_id": b_gid, "record_id": rid, "format": "pdf"})
        assert denied.status_code == 404
        allowed = client.post(
            "/api/resume/guest/download",
            json={"guest_id": a_gid, "record_id": rid, "format": "pdf"})
        assert allowed.status_code == 200

    def test_claim_transfers_only_the_presented_sessions_records(self, guest_db):
        a_gid, b_gid = self._shared_device_pair()
        for gid in (a_gid, b_gid):
            guest_db.tailoring_records.insert_one({
                "record_id": str(_uuid.uuid4()), "guest_id": gid, "user_email": None,
                "tailored_resume": {"summary": "x"}, "source": "guest_tailor",
            })
        res = guest_service.claim_session(b_gid, "sam.claims@example.com")
        assert res["claimed"] is True
        assert res["records_transferred"] == 1     # ONLY B's record
        # A's artifacts, session and subject are untouched.
        a_rec = guest_db.tailoring_records.find_one({"guest_id": a_gid})
        assert a_rec["user_email"] is None
        a_doc = guest_db.guest_sessions.find_one({"guest_id": a_gid})
        assert a_doc["claimed_by"] is None
        a_subj = _subject_doc(guest_db, a_gid)
        assert "sam.claims@example.com" not in (a_subj.get("linked_accounts") or [])


# ===========================================================================
# v2 acceptance — foreign match_id capability (acceptance #9)
# ===========================================================================

class TestForeignMatchIdCapability:
    def test_match_id_from_another_subjects_cache_is_404(self, guest_db, resume_app):
        a = _new_session()
        _attach(a["guest_id"], _structured(), "subject A resume " * 5)
        a_mids = _serve_matches(a["guest_id"], n=1)

        b = _new_session(fp="fp_b_device")
        _attach(
            b["guest_id"],
            _structured(name="Sam Lee", email="sam.lee@other.org", phone="(444) 111-2222"),
            "subject B resume different " * 5,
            fp="fp_b_device",
        )
        _serve_matches(b["guest_id"], n=1)         # B has a cache of their OWN

        resp = resume_app.test_client().post(
            "/api/resume/guest/tailor",
            json={"guest_id": b["guest_id"], "match_id": a_mids[0]})
        assert resp.status_code == 404
        # No quota burned anywhere by the cross-subject attempt.
        assert _subject_doc(guest_db, a["guest_id"])["tailors_used"] == 0
        assert _subject_doc(guest_db, b["guest_id"])["tailors_used"] == 0


# ===========================================================================
# v2 acceptance — concurrent reservation interleaving (acceptance #10)
# ===========================================================================

class TestConcurrentReservationInterleaving:
    def test_interleaved_concurrent_tailors_yield_exactly_one_reservation(
            self, guest_db, monkeypatch):
        # Two Lambdas race past their pre-checks; the competing call's atomic
        # find_one_and_update lands FIRST, mid-flight inside ours (same
        # stale-read interleaving technique as TestClaimRace).
        s = _new_session()
        gid = s["guest_id"]
        guest_service.subject_for_guest(gid)       # subject doc exists up front
        m1, m2 = _uuid.uuid4().hex, _uuid.uuid4().hex
        real = guest_db.guest_quota_subjects
        competing = {}

        class InterleavedSubjects:
            def __init__(self, real_coll):
                self._real = real_coll
                self._fired = False

            def find_one_and_update(self, *args, **kwargs):
                if not self._fired:
                    self._fired = True
                    competing["outcome"] = guest_service.try_reserve_tailor(gid, m2)
                return self._real.find_one_and_update(*args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._real, name)

        proxy = InterleavedSubjects(real)
        monkeypatch.setattr(guest_service, "_subjects", lambda: proxy)

        outcome = guest_service.try_reserve_tailor(gid, m1)

        assert competing["outcome"].get("reserved") is True   # exactly one winner
        assert outcome.get("reserved") is None                # loser did NOT reserve
        assert outcome.get("exhausted") is True
        doc = real.find_one({"subject_id": guest_service.subject_for_guest(gid)})
        assert doc["tailors_used"] == 1                       # charged exactly once
        assert doc["reservation"]["match_id"] == m2


# ===========================================================================
# v2 acceptance — download token forgery matrix (acceptance #12)
# ===========================================================================

class TestDownloadTokenForgeryMatrix:
    def test_tampered_cross_bound_and_bad_format_tokens_are_generic_404(
            self, guest_db, resume_app):
        s = _new_session()
        other = _new_session()
        rid = str(_uuid.uuid4())
        guest_db.tailoring_records.insert_one({
            "record_id": rid, "guest_id": s["guest_id"], "user_email": None,
            "tailored_resume": {"summary": "x"}, "source": "guest_tailor",
        })
        client = resume_app.test_client()
        far_exp = int((datetime.now(timezone.utc) + timedelta(minutes=10)).timestamp())

        # (a) tampered payload, original signature kept → 404 (HMAC mismatch).
        token, _ = guest_service.mint_download_token(rid, s["guest_id"], "pdf")
        head, _payload, sig = token.split(".")
        evil_payload = guest_service._b64e(
            f"{rid}|{other['guest_id']}|pdf|{far_exp}".encode("utf-8"))
        tampered = client.get(f"/api/resume/guest/download/{head}.{evil_payload}.{sig}")
        assert tampered.status_code == 404

        # (b) legitimately signed token binding the record to the WRONG guest
        #     (record/guest mismatch) → 404.
        cross, _ = guest_service.mint_download_token(rid, other["guest_id"], "pdf")
        cross_resp = client.get(f"/api/resume/guest/download/{cross}")
        assert cross_resp.status_code == 404

        # (c) correctly signed token whose format is outside the whitelist → 404.
        key = b"unit-test-download-secret"          # the autouse fixture's secret
        p = guest_service._b64e(f"{rid}|{s['guest_id']}|exe|{far_exp}".encode("utf-8"))
        sig2 = guest_service._b64e(guest_service._sign_download(p, key))
        badfmt_resp = client.get(f"/api/resume/guest/download/v1.{p}.{sig2}")
        assert badfmt_resp.status_code == 404

        # (d) valid token for a record that does not exist → 404.
        ghost, _ = guest_service.mint_download_token(
            str(_uuid.uuid4()), s["guest_id"], "pdf")
        ghost_resp = client.get(f"/api/resume/guest/download/{ghost}")
        assert ghost_resp.status_code == 404

        # All bodies generic and identical to plain garbage — no oracle.
        garbage = client.get("/api/resume/guest/download/garbage")
        bodies = {
            str(r.get_json())
            for r in (tampered, cross_resp, badfmt_resp, ghost_resp, garbage)
        }
        assert len(bodies) == 1


# ===========================================================================
# v2 acceptance — claim route status contract (acceptance #13)
# ===========================================================================

class TestClaimRouteStatusContract:
    def test_claim_route_second_account_gets_403_and_transfers_nothing(
            self, guest_db, resume_app):
        s = _new_session()
        _attach(s["guest_id"], _structured(), "route claim resume " * 5)
        from flask_jwt_extended import create_access_token
        with resume_app.app_context():
            t_winner = create_access_token(identity="winner.route@example.com")
            t_loser = create_access_token(identity="loser.route@example.com")
        client = resume_app.test_client()

        ok = client.post("/api/resume/guest/claim",
                         headers={"Authorization": f"Bearer {t_winner}"},
                         json={"guest_id": s["guest_id"]})
        assert ok.status_code == 200

        lost = client.post("/api/resume/guest/claim",
                           headers={"Authorization": f"Bearer {t_loser}"},
                           json={"guest_id": s["guest_id"]})
        assert lost.status_code == 403             # forbidden — never 401
        assert lost.get_json()["error"] == "already_claimed"
        from services import usage_service
        assert usage_service.get_usage("loser.route@example.com")["used"] == 0
        session = guest_db.guest_sessions.find_one({"guest_id": s["guest_id"]})
        assert session["claimed_by"] == "winner.route@example.com"


# ===========================================================================
# v2 acceptance — SSRF posture (acceptance #15)
# ===========================================================================

class TestSSRFPosture:
    """Guest tailoring never fetches a client-supplied URL; the JD fetch runs
    only against the server-STORED match URL."""

    def test_tailor_route_rejects_client_urls_and_never_fetches(
            self, guest_db, resume_app, monkeypatch):
        import services.jd_fetcher as jf
        calls = []

        def recording_fetch(url):
            calls.append(url)
            return "JD text", "html"

        monkeypatch.setattr(jf, "fetch_jd", recording_fetch)
        s = _new_session()
        _attach(s["guest_id"], _structured(), "ssrf resume " * 5)
        mids = _serve_matches(s["guest_id"], n=1)
        client = resume_app.test_client()

        for extra in ({"url": "https://attacker.example/jd"},
                      {"jd_text": "attacker-controlled JD"},
                      {"job": {"title": "SWE", "url": "https://attacker.example/jd"}}):
            resp = client.post(
                "/api/resume/guest/tailor",
                json={"guest_id": s["guest_id"], "match_id": mids[0], **extra})
            assert resp.status_code == 400, extra

        # A clean tailor uses the STORED description — still no fetch.
        ok = client.post("/api/resume/guest/tailor",
                         json={"guest_id": s["guest_id"], "match_id": mids[0]})
        assert ok.status_code == 202
        assert calls == []                        # fetcher never saw ANY url

    def test_worker_fetches_jd_only_from_the_stored_match_url(
            self, guest_db, resume_app, monkeypatch):
        import services.jd_fetcher as jf
        import services.resume_service as rs
        s = _new_session()
        _attach(s["guest_id"], _structured(), "stored url resume " * 5)
        mid = _uuid.uuid4().hex
        stored_url = "https://jobs.example/stored-jd"
        guest_service._store_matches(
            s["guest_id"],
            [{"match_id": mid, "title": "Backend Role", "company": "Acme",
              "url": stored_url, "match_score": 80}],
            20,
            jd_sources={mid: {"title": "Backend Role", "company": "Acme",
                              "jd_text": "", "url": stored_url}},
        )
        resp = resume_app.test_client().post(
            "/api/resume/guest/tailor",
            json={"guest_id": s["guest_id"], "match_id": mid})
        assert resp.status_code == 202
        job_id = resp.get_json()["job_id"]
        row = guest_db.resume_jobs.find_one({"job_id": job_id})
        # The dispatched payload carries the SERVER-stored URL, no client input.
        assert row["payload"]["job"]["url"] == stored_url
        assert row["payload"]["job"]["jd_text"] == ""

        calls = []

        def fake_fetch(url):
            calls.append(url)
            return "", "none"                     # unreachable JD → clean failure

        monkeypatch.setattr(jf, "fetch_jd", fake_fetch)
        rs._process_job(job_id, "guest_tailor", row["payload"])

        assert calls == [stored_url]              # ONLY the stored match URL
        job = guest_service.get_guest_job(job_id, s["guest_id"])
        assert job["status"] == "failed"
        # The infra failure refunded the guest's single trial.
        doc = _subject_doc(guest_db, s["guest_id"])
        assert doc["tailors_used"] == 0
        assert doc["reservation"]["status"] == "failed"


# ===========================================================================
# v2 acceptance — dispatch-failure rollbacks for tailor + matches (acceptance #16;
# the parse rollback lives in TestGlobalCapacityCaps)
# ===========================================================================

class TestDispatchFailureRollbacks:
    def test_tailor_dispatch_failure_rolls_back_reservation_and_breaker(
            self, guest_db, resume_app, monkeypatch):
        import services.resume_service as rs

        def boom(*a, **k):
            raise RuntimeError("lambda invoke failed")

        monkeypatch.setattr(rs.ResumeService, "invoke_async", staticmethod(boom))
        s = _new_session()
        _attach(s["guest_id"], _structured(), "dispatch fail resume " * 5)
        mids = _serve_matches(s["guest_id"], n=1)
        resp = resume_app.test_client().post(
            "/api/resume/guest/tailor",
            json={"guest_id": s["guest_id"], "match_id": mids[0]})
        assert resp.status_code == 500
        doc = _subject_doc(guest_db, s["guest_id"])
        assert doc["tailors_used"] == 0 and doc["reservation"] is None
        counter = guest_db.guest_global_counters.find_one(
            {"_id": {"$regex": "^guest_tailors:"}})
        assert (counter or {}).get("count", 0) == 0
        # The guest is not cheated — a retry after the outage still reserves.
        monkeypatch.setattr(rs.ResumeService, "invoke_async",
                            staticmethod(lambda *a, **k: None))
        retry = resume_app.test_client().post(
            "/api/resume/guest/tailor",
            json={"guest_id": s["guest_id"], "match_id": mids[0]})
        assert retry.status_code == 202

    def test_matches_dispatch_failure_releases_global_slot(self, guest_db, monkeypatch):
        import services.resume_service as rs

        def boom(*a, **k):
            raise RuntimeError("lambda invoke failed")

        monkeypatch.setattr(rs.ResumeService, "invoke_async", staticmethod(boom))
        app = load_service_app("jobs")
        s = _new_session()
        _attach(s["guest_id"], _structured(), "matches dispatch resume " * 5)
        resp = app.test_client().post(
            "/api/jobs/guest/matches", json={"guest_id": s["guest_id"]})
        assert resp.status_code == 500
        counter = guest_db.guest_global_counters.find_one(
            {"_id": {"$regex": "^guest_matches:"}})
        assert (counter or {}).get("count", 0) == 0


# ===========================================================================
# v2 acceptance — challenge hook on onboard + tailor routes (acceptance #18;
# the matches route + seam units live in TestChallengeSeam)
# ===========================================================================

class TestChallengeOnWriteEndpoints:
    def _arm(self, monkeypatch):
        monkeypatch.setenv("GUEST_IP_HASH_SALT", "salt")
        monkeypatch.setenv("GUEST_CHALLENGE_PROVIDER", "turnstile")
        monkeypatch.setenv("GUEST_CHALLENGE_SECRET", "unit-test-challenge-secret")
        monkeypatch.setenv("GUEST_CHALLENGE_THRESHOLD", "1")

    def test_onboard_challenge_after_threshold_before_spend(
            self, guest_db, resume_app, monkeypatch):
        self._arm(monkeypatch)
        client = resume_app.test_client()

        def post():
            return client.post(
                "/api/resume/guest/onboard",
                data={"file": (io.BytesIO(PDF_BYTES), "resume.pdf")},
                content_type="multipart/form-data",
            )

        assert post().status_code == 202          # first attempt NEVER challenged
        over = post()
        assert over.status_code == 429
        body = over.get_json()
        assert body["error"] == "challenge_required"
        assert body["provider"] == "turnstile"
        # No budget spent and no session minted for the challenged attempt.
        counter = guest_db.guest_global_counters.find_one(
            {"_id": {"$regex": "^guest_parses:"}})
        assert counter["count"] == 1
        assert guest_db.guest_sessions.count_documents({}) == 1

    def test_tailor_challenge_after_threshold_before_spend(
            self, guest_db, resume_app, monkeypatch):
        self._arm(monkeypatch)
        s = _new_session()
        _attach(s["guest_id"], _structured(), "challenge tailor resume " * 5)
        mids = _serve_matches(s["guest_id"], n=1)
        client = resume_app.test_client()

        first = client.post("/api/resume/guest/tailor",
                            json={"guest_id": s["guest_id"], "match_id": mids[0]})
        assert first.status_code == 202           # never on the first attempt
        over = client.post("/api/resume/guest/tailor",
                           json={"guest_id": s["guest_id"], "match_id": mids[0]})
        assert over.status_code == 429
        body = over.get_json()
        assert body["error"] == "challenge_required"
        assert body["provider"] == "turnstile"
        # The challenged retry consumed nothing further.
        assert _subject_doc(guest_db, s["guest_id"])["tailors_used"] == 1
        counter = guest_db.guest_global_counters.find_one(
            {"_id": {"$regex": "^guest_tailors:"}})
        assert counter["count"] == 1


# ===========================================================================
# v2 acceptance — admin PUT bounds for the v2 core knobs (acceptance #19)
# ===========================================================================

class TestAdminConfigV2BoundsViaPut:
    def test_put_rejects_out_of_bounds_and_accepts_v2_defaults(self, guest_db):
        app = load_service_app("auth")
        from flask_jwt_extended import create_access_token
        with app.app_context():
            token = create_access_token(identity="harshith@aithrex.tech")
        client = app.test_client()
        headers = {"Authorization": f"Bearer {token}"}

        for bad in ({"guest_tailor_limit": 101}, {"guest_tailor_limit": -1},
                    {"guest_jobs_max": 51}, {"guest_jobs_max": 0},
                    {"retention_days": 3651}, {"retention_days": -1}):
            resp = client.put("/api/admin/guest-config", headers=headers, json=bad)
            assert resp.status_code == 400, bad
            assert resp.get_json()["error"] == "invalid_config"

        ok = client.put("/api/admin/guest-config", headers=headers,
                        json={"guest_tailor_limit": 1, "guest_jobs_max": 5,
                              "retention_days": 30})
        assert ok.status_code == 200
        body = ok.get_json()
        assert body["guest_tailor_limit"] == 1
        assert body["guest_jobs_max"] == 5
        assert body["retention_days"] == 30


# ===========================================================================
# v2 acceptance — NO guest failure path emits 401 (acceptance #17)
# ===========================================================================

_NO_401_SWEEP = [
    ("onboard_bad_file_type", 400),
    ("onboard_missing_file", 400),
    ("onboard_global_parse_cap", 429),
    ("job_poll_unknown", 404),
    ("session_rate_limited", 429),
    ("tailor_v1_payload", 400),
    ("tailor_invalid_guest_id", 400),
    ("tailor_unknown_session", 404),
    ("tailor_no_resume", 400),
    ("tailor_unknown_match", 404),
    ("tailor_quota_exhausted", 429),
    ("tailor_global_breaker", 429),
    ("download_unknown_record", 404),
    ("download_bad_format", 400),
    ("download_forged_token", 404),
    ("download_expired_token", 410),
    ("download_secret_unset", 503),
    ("matches_invalid_guest_id", 400),
    ("matches_unknown_session", 404),
    ("matches_no_resume", 400),
    ("matches_global_cap", 429),
    ("matches_challenge_required", 429),
    ("feature_disabled", 503),
    ("claim_invalid_guest_id", 400),
    ("claim_unknown_session", 404),
    ("claim_other_account", 403),
]


class TestNoGuest401Sweep:
    """Parametrized sweep over guest failure modes: the FE force-logs-out on
    any 401, so NO guest failure path may ever emit one. (The single
    legitimate 401 — a missing/bad JWT on /guest/claim — is pinned separately
    by TestGuestRoutes.test_claim_route_requires_jwt.)"""

    def _gid_with_resume(self):
        s = _new_session()
        _attach(s["guest_id"], _structured(), "sweep resume text " * 5)
        return s["guest_id"]

    def _record_for(self, guest_db, gid):
        rid = str(_uuid.uuid4())
        guest_db.tailoring_records.insert_one({
            "record_id": rid, "guest_id": gid, "user_email": None,
            "tailored_resume": {"summary": "x"}, "source": "guest_tailor",
        })
        return rid

    def _jwt(self, resume_app, email):
        from flask_jwt_extended import create_access_token
        with resume_app.app_context():
            return create_access_token(identity=email)

    def _provoke(self, case, client, guest_db, monkeypatch, resume_app):
        if case == "onboard_bad_file_type":
            return client.post(
                "/api/resume/guest/onboard",
                data={"file": (io.BytesIO(b"MZ not a pdf"), "resume.exe")},
                content_type="multipart/form-data")
        if case == "onboard_missing_file":
            return client.post("/api/resume/guest/onboard", data={},
                               content_type="multipart/form-data")
        if case == "onboard_global_parse_cap":
            pconfig.update_guest_config({"global_daily_guest_parses": 0}, "admin@test")
            return client.post(
                "/api/resume/guest/onboard",
                data={"file": (io.BytesIO(PDF_BYTES), "resume.pdf")},
                content_type="multipart/form-data")
        if case == "job_poll_unknown":
            return client.get(
                f"/api/resume/guest/job/{_uuid.uuid4()}?guest_id={_uuid.uuid4()}")
        if case == "session_rate_limited":
            resp = None
            for _ in range(61):                    # limit is 60 / 5 min
                resp = client.get("/api/resume/guest/session")
            return resp
        if case == "tailor_v1_payload":
            gid = self._gid_with_resume()
            return client.post(
                "/api/resume/guest/tailor",
                json={"guest_id": gid, "job": {"title": "SWE", "jd_text": "x"}})
        if case == "tailor_invalid_guest_id":
            return client.post(
                "/api/resume/guest/tailor",
                json={"guest_id": "nope", "match_id": "a" * 32})
        if case == "tailor_unknown_session":
            return client.post(
                "/api/resume/guest/tailor",
                json={"guest_id": str(_uuid.uuid4()), "match_id": _uuid.uuid4().hex})
        if case == "tailor_no_resume":
            s = _new_session()
            return client.post(
                "/api/resume/guest/tailor",
                json={"guest_id": s["guest_id"], "match_id": _uuid.uuid4().hex})
        if case == "tailor_unknown_match":
            gid = self._gid_with_resume()
            return client.post(
                "/api/resume/guest/tailor",
                json={"guest_id": gid, "match_id": _uuid.uuid4().hex})
        if case == "tailor_quota_exhausted":
            gid = self._gid_with_resume()
            mids = _serve_matches(gid, n=2)
            assert client.post(
                "/api/resume/guest/tailor",
                json={"guest_id": gid, "match_id": mids[0]}).status_code == 202
            return client.post(
                "/api/resume/guest/tailor",
                json={"guest_id": gid, "match_id": mids[1]})
        if case == "tailor_global_breaker":
            pconfig.update_guest_config({"global_daily_guest_tailors": 0}, "admin@test")
            gid = self._gid_with_resume()
            mids = _serve_matches(gid, n=1)
            return client.post(
                "/api/resume/guest/tailor",
                json={"guest_id": gid, "match_id": mids[0]})
        if case == "download_unknown_record":
            s = _new_session()
            return client.post(
                "/api/resume/guest/download",
                json={"guest_id": s["guest_id"],
                      "record_id": str(_uuid.uuid4()), "format": "pdf"})
        if case == "download_bad_format":
            s = _new_session()
            rid = self._record_for(guest_db, s["guest_id"])
            return client.post(
                "/api/resume/guest/download",
                json={"guest_id": s["guest_id"], "record_id": rid, "format": "txt"})
        if case == "download_forged_token":
            return client.get("/api/resume/guest/download/garbage")
        if case == "download_expired_token":
            s = _new_session()
            rid = self._record_for(guest_db, s["guest_id"])
            expired, _ = guest_service.mint_download_token(
                rid, s["guest_id"], "pdf", ttl_seconds=-5)
            return client.get(f"/api/resume/guest/download/{expired}")
        if case == "download_secret_unset":
            monkeypatch.delenv("GUEST_DOWNLOAD_TOKEN_SECRET", raising=False)
            monkeypatch.setenv("ENVIRONMENT", "production")
            monkeypatch.setenv("GUEST_ONBOARDING_ENABLED", "true")
            s = _new_session()
            rid = self._record_for(guest_db, s["guest_id"])
            return client.post(
                "/api/resume/guest/download",
                json={"guest_id": s["guest_id"], "record_id": rid, "format": "pdf"})
        if case.startswith("matches_"):
            jobs_client = load_service_app("jobs").test_client()
            if case == "matches_invalid_guest_id":
                return jobs_client.post("/api/jobs/guest/matches",
                                        json={"guest_id": {"$ne": None}})
            if case == "matches_unknown_session":
                return jobs_client.post("/api/jobs/guest/matches",
                                        json={"guest_id": str(_uuid.uuid4())})
            if case == "matches_no_resume":
                s = _new_session()
                return jobs_client.post("/api/jobs/guest/matches",
                                        json={"guest_id": s["guest_id"]})
            if case == "matches_global_cap":
                pconfig.update_guest_config(
                    {"global_daily_guest_matches": 0}, "admin@test")
                gid = self._gid_with_resume()
                return jobs_client.post("/api/jobs/guest/matches",
                                        json={"guest_id": gid})
            if case == "matches_challenge_required":
                monkeypatch.setenv("GUEST_IP_HASH_SALT", "salt")
                monkeypatch.setenv("GUEST_CHALLENGE_PROVIDER", "turnstile")
                monkeypatch.setenv("GUEST_CHALLENGE_SECRET", "unit-test-secret")
                monkeypatch.setenv("GUEST_CHALLENGE_THRESHOLD", "1")
                gid = self._gid_with_resume()
                assert jobs_client.post(
                    "/api/jobs/guest/matches",
                    json={"guest_id": gid}).status_code == 202
                return jobs_client.post("/api/jobs/guest/matches",
                                        json={"guest_id": gid})
        if case == "feature_disabled":
            monkeypatch.setenv("GUEST_ONBOARDING_ENABLED", "false")
            return client.get("/api/resume/guest/session")
        if case == "claim_invalid_guest_id":
            token = self._jwt(resume_app, "sweep@example.com")
            return client.post("/api/resume/guest/claim",
                               headers={"Authorization": f"Bearer {token}"},
                               json={"guest_id": "nope"})
        if case == "claim_unknown_session":
            token = self._jwt(resume_app, "sweep@example.com")
            return client.post("/api/resume/guest/claim",
                               headers={"Authorization": f"Bearer {token}"},
                               json={"guest_id": str(_uuid.uuid4())})
        if case == "claim_other_account":
            s = _new_session()
            _attach(s["guest_id"], _structured(), "claim sweep resume " * 5)
            assert guest_service.claim_session(
                s["guest_id"], "owner.sweep@example.com")["claimed"]
            token = self._jwt(resume_app, "intruder.sweep@example.com")
            return client.post("/api/resume/guest/claim",
                               headers={"Authorization": f"Bearer {token}"},
                               json={"guest_id": s["guest_id"]})
        raise AssertionError(f"unhandled sweep case: {case}")

    @pytest.mark.parametrize("case,expected", _NO_401_SWEEP,
                             ids=[c for c, _ in _NO_401_SWEEP])
    def test_guest_failure_never_emits_401(
            self, case, expected, guest_db, resume_app, monkeypatch):
        client = resume_app.test_client()
        resp = self._provoke(case, client, guest_db, monkeypatch, resume_app)
        assert resp.status_code != 401, (
            f"{case} emitted 401 — the FE would force-logout a guest")
        assert resp.status_code == expected


# ===========================================================================
# Guest file storage: S3-first payloads, payload stripping, retention deletes
# ===========================================================================

class _FakeStorage:
    """Records calls; serves PDF_BYTES back for any key."""

    def __init__(self, fail_upload=False):
        self.fail_upload = fail_upload
        self.uploaded = []   # (guest_id, filename) tuples (legacy scheme)
        self.assets = []     # (resume_id, mime_type) tuples (v3 scheme)
        self.fetched = []    # keys
        self.deleted = []    # keys

    def upload_guest_resume(self, guest_id, file_bytes, filename):
        if self.fail_upload:
            raise ValueError("RESUME_S3_BUCKET is not configured")
        key = f"guest/{guest_id}/20260720000000_{filename}"
        self.uploaded.append((guest_id, filename))
        return key

    def upload_resume_asset(self, resume_id, file_bytes, mime_type):
        if self.fail_upload:
            raise ValueError("RESUME_S3_BUCKET is not configured")
        self.assets.append((resume_id, mime_type))
        return f"resume-assets/{resume_id}/original"

    def get_resume(self, s3_key):
        self.fetched.append(s3_key)
        return PDF_BYTES

    def delete_resume(self, s3_key):
        self.deleted.append(s3_key)
        return True


class TestGuestFileStorage:
    def _patch_storage(self, monkeypatch, storage):
        import services.s3_service as s3s
        monkeypatch.setattr(s3s, "get_storage_service", lambda: storage)
        return storage

    def test_finalizers_strip_payload(self, guest_db):
        import services.resume_service as rs
        svc = rs.get_resume_service()
        s = _new_session()
        big = {"file_base64": "x" * 5000, "guest_id": s["guest_id"]}

        done = guest_service.create_guest_job("guest_parse", dict(big), s["guest_id"])
        svc.complete_job(done, {"ok": True})
        row = guest_db.resume_jobs.find_one({"job_id": done})
        assert row["status"] == "completed" and "payload" not in row

        failed = guest_service.create_guest_job("guest_parse", dict(big), s["guest_id"])
        svc.fail_job(failed, "boom")
        row = guest_db.resume_jobs.find_one({"job_id": failed})
        assert row["status"] == "failed" and "payload" not in row

    def test_onboard_stores_file_in_s3_and_records_key(
            self, guest_db, resume_app, monkeypatch):
        storage = self._patch_storage(monkeypatch, _FakeStorage())
        resp = resume_app.test_client().post(
            "/api/resume/guest/onboard",
            data={"file": (io.BytesIO(PDF_BYTES), "resume.pdf"),
                  "fingerprint_hash": "fp_s3_path"},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 202, resp.get_data(as_text=True)
        body = resp.get_json()
        gid, jid, rid = body["guest_id"], body["job_id"], body["resume_id"]
        # v3: the object lands under the anonymous asset key scheme.
        assert storage.assets == [(rid, "application/pdf")]

        row = guest_db.resume_jobs.find_one({"job_id": jid})
        assert row["payload"]["object_key"] == f"resume-assets/{rid}/original"
        assert "file_base64" not in row["payload"]          # no inline blob
        assert "file_bytes_b64_dev" not in row["payload"]   # S3 path won

        # Asset doc created BEFORE the S3 write, key attached after.
        asset = guest_db.resume_assets.find_one({"resume_id": rid})
        assert asset["owner_type"] == "guest" and asset["guest_id"] == gid
        assert asset["parse_status"] == "pending"
        assert asset["object_key"] == f"resume-assets/{rid}/original"
        assert asset["scan_status"] == "unscanned"
        assert asset["purge_after"] is not None            # guest retention TTL

        # Legacy retention list still tracks the object until Phase C.
        session = guest_db.guest_sessions.find_one({"guest_id": gid})
        assert session["resume_file_s3_keys"] == [row["payload"]["object_key"]]

    def test_onboard_falls_back_to_inline_without_bucket(
            self, guest_db, resume_app, monkeypatch):
        self._patch_storage(monkeypatch, _FakeStorage(fail_upload=True))
        resp = resume_app.test_client().post(
            "/api/resume/guest/onboard",
            data={"file": (io.BytesIO(PDF_BYTES), "resume.pdf")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 202
        row = guest_db.resume_jobs.find_one({"job_id": resp.get_json()["job_id"]})
        assert "object_key" not in row["payload"]
        # Non-Lambda local-dev fallback rides the explicit dev-only key.
        assert row["payload"]["file_bytes_b64_dev"]

    def test_guest_parse_worker_fetches_from_s3(self, guest_db, monkeypatch):
        import services.resume_service as rs
        storage = self._patch_storage(monkeypatch, _FakeStorage())
        s = _new_session(fp="fp_s3_worker")
        key = f"guest/{s['guest_id']}/20260720000000_resume.pdf"
        payload = {
            "file_s3_key": key, "mime_type": "application/pdf",
            "filename": "resume.pdf", "guest_id": s["guest_id"],
            "fingerprint_hash": "fp_s3_worker",
        }
        job_id = guest_service.create_guest_job("guest_parse", payload, s["guest_id"])
        svc = rs.get_resume_service()
        seen = {}
        def fake_parse(raw, file_b64, mime):
            seen["file_b64"] = file_b64
            return _structured(), "Jordan Alvarez raw resume text " * 5
        monkeypatch.setattr(svc.parser, "parse_to_structured", fake_parse)
        rs._process_job(job_id, "guest_parse", payload)

        assert storage.fetched == [key]
        import base64 as b64
        assert seen["file_b64"] == b64.b64encode(PDF_BYTES).decode("utf-8")
        job = guest_service.get_guest_job(job_id, s["guest_id"])
        assert job["status"] == "completed"

    def test_guest_parse_worker_fails_honestly_on_s3_miss(
            self, guest_db, monkeypatch):
        import services.resume_service as rs

        class _Missing(_FakeStorage):
            def get_resume(self, s3_key):
                raise KeyError(s3_key)

        self._patch_storage(monkeypatch, _Missing())
        s = _new_session()
        payload = {"file_s3_key": "guest/x/nope.pdf", "guest_id": s["guest_id"],
                   "mime_type": "application/pdf", "filename": "resume.pdf"}
        job_id = guest_service.create_guest_job("guest_parse", payload, s["guest_id"])
        rs._process_job(job_id, "guest_parse", payload)
        job = guest_service.get_guest_job(job_id, s["guest_id"])
        assert job["status"] == "failed"
        assert "Could not read the uploaded file" in job["error"]

    def test_retention_cleanup_deletes_recorded_s3_files(
            self, guest_db, monkeypatch):
        from datetime import timedelta
        storage = self._patch_storage(monkeypatch, _FakeStorage())
        s = _new_session()
        key = f"guest/{s['guest_id']}/20260720000000_resume.pdf"
        guest_service.record_resume_file_key(s["guest_id"], key)
        # Recording twice stays deduped ($addToSet).
        guest_service.record_resume_file_key(s["guest_id"], key)
        guest_db.guest_sessions.update_one(
            {"guest_id": s["guest_id"]},
            {"$set": {"last_seen_at":
                      guest_service._now() - timedelta(days=45)}},
        )
        out = guest_service.run_retention_cleanup()
        assert out["sessions_deleted"] == 1
        assert out["files_deleted"] == 1
        assert storage.deleted == [key]


if __name__ == "__main__":  # allow `python tests/test_guest_onboarding.py`
    sys.exit(pytest.main([__file__, "-q"]))
