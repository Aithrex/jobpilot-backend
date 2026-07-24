"""Cross-user ATS keyword signals (Craft-overhaul Part B3).

The firewall is the point of these tests: aggregates only — no verbatim bullet,
company, metric, or user identity may reach the stats collection. Also covers
k-anonymity floors, lift computation, and the consumer hint. mongomock only.
"""
import pytest

from services import ats_signal_service as ats
from services.ats_signal_service import (
    recompute_ats_signals,
    high_signal_keywords,
    STATS_COLLECTION,
    ROLE_TOTAL_SENTINEL,
)

# Distinctive strings that must NEVER appear in the aggregate output.
_SECRET_COMPANY = "HooliCorpSecret"
_SECRET_BULLET = "Reduced fraud losses by 37 percent at HooliCorpSecret."
_SECRET_USER = "victim@private.com"


@pytest.fixture(autouse=True)
def _clean(mock_db):
    mock_db.tailoring_records.delete_many({})
    mock_db[STATS_COLLECTION].delete_many({})
    yield


def _record(mock_db, user, title, keywords, overall, company=_SECRET_COMPANY):
    mock_db.tailoring_records.insert_one({
        "user_email": user,
        "deleted": False,
        # jd_analysis is the only projected field; include company to prove it
        # is NEVER read into the aggregate.
        "jd_analysis": {"job_title": title, "company": company,
                        "required_skills": keywords, "keywords": []},
        "ats_scores": {"schema_version": 2, "overall": overall},
        # Verbatim resume text that must never leak into the aggregate:
        "tailored_resume": {"experience": [{"company": company,
                                            "bullets": [_SECRET_BULLET]}]},
    })


def _seed_backend_corpus(mock_db):
    # 6 backend JDs; "kubernetes" appears in the 4 HIGH-scoring ones, so it
    # should show positive lift. Distinct users → cross-user aggregate.
    _record(mock_db, "u1@x.com", "Senior Backend Engineer", ["Kubernetes", "Python"], 90)
    _record(mock_db, "u2@x.com", "Backend Engineer", ["Kubernetes", "Go"], 88)
    _record(mock_db, "u3@x.com", "Backend Developer", ["Kubernetes", "Java"], 86)
    _record(mock_db, "u4@x.com", "Backend Engineer", ["Kubernetes", "Rust"], 84)
    _record(mock_db, "u5@x.com", "Backend Engineer", ["PHP"], 60)
    _record(mock_db, "u6@x.com", "Backend Engineer", ["PHP"], 58)


def test_recompute_produces_role_and_keyword_rows(mock_db):
    _seed_backend_corpus(mock_db)
    summary = recompute_ats_signals()
    assert summary["roles"] >= 1
    assert summary["keyword_rows"] >= 1

    rows = list(mock_db[STATS_COLLECTION].find({}))
    # A role baseline sentinel exists.
    assert any(r["keyword"] == ROLE_TOTAL_SENTINEL for r in rows)
    # kubernetes shows positive lift (high-scoring JDs carry it).
    kube = [r for r in rows if r["keyword"] == "Kubernetes"]
    assert kube and kube[0]["lift"] > 0


def test_firewall_no_verbatim_text_or_identity_in_output(mock_db):
    _seed_backend_corpus(mock_db)
    recompute_ats_signals()

    rows = list(mock_db[STATS_COLLECTION].find({}))
    # Serialize every stored doc and assert NONE of the sensitive strings leak.
    blob = repr(rows)
    assert _SECRET_COMPANY not in blob
    assert _SECRET_BULLET not in blob
    assert "37" not in blob or "percent" not in blob  # no metric text
    assert "@" not in blob  # no user_email
    # Only the allowed aggregate keys are present.
    allowed = {"role", "keyword", "jd_count", "mean_ats", "lift", "updated_at", "_id"}
    for r in rows:
        assert set(r.keys()) <= allowed


def test_k_anonymity_floor_hides_rare_keywords(mock_db):
    # A keyword appearing in only 1-2 JDs must not be published (MIN_KEYWORD_JDS=3).
    _seed_backend_corpus(mock_db)  # "go"/"java"/"rust" each appear once
    recompute_ats_signals()
    rows = list(mock_db[STATS_COLLECTION].find({}))
    kws = {r["keyword"] for r in rows}
    assert "Go" not in kws and "Java" not in kws and "Rust" not in kws
    assert "Kubernetes" in kws  # appears in 4 JDs → published


def test_role_floor_hides_thin_roles(mock_db):
    # Only 2 mobile JDs → below MIN_ROLE_JDS=5 → role not published.
    _record(mock_db, "m1@x.com", "iOS Mobile Engineer", ["Swift"], 80)
    _record(mock_db, "m2@x.com", "Android Mobile Engineer", ["Kotlin"], 82)
    recompute_ats_signals()
    rows = list(mock_db[STATS_COLLECTION].find({"role": "mobile"}))
    assert rows == []


def test_unscored_records_are_skipped(mock_db):
    mock_db.tailoring_records.insert_one({
        "user_email": "u@x.com", "deleted": False,
        "jd_analysis": {"job_title": "Backend Engineer", "required_skills": ["Kubernetes"]},
        "ats_scores": None,  # unscored
    })
    summary = recompute_ats_signals()
    assert summary["scanned"] == 0


def test_soft_deleted_records_are_skipped(mock_db):
    _record(mock_db, "u@x.com", "Backend Engineer", ["Kubernetes"], 90)
    mock_db.tailoring_records.update_many({}, {"$set": {"deleted": True}})
    summary = recompute_ats_signals()
    assert summary["scanned"] == 0


def test_high_signal_keywords_consumer(mock_db):
    _seed_backend_corpus(mock_db)
    recompute_ats_signals()
    hints = high_signal_keywords("Senior Backend Engineer")
    assert "Kubernetes" in hints
    # Unknown role / no data → empty (fail open).
    assert high_signal_keywords("Underwater Basket Weaver") == []


def test_high_signal_keywords_fails_open_on_error(monkeypatch):
    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(ats, "_db", _boom)
    assert high_signal_keywords("Backend Engineer") == []
