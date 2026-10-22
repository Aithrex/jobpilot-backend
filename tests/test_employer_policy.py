"""Direct-employer policy (employer-policy-1.0.0): preferences, posting
relationship/purpose, availability, and the one shared `apply`."""
from datetime import datetime, timedelta, timezone

import pytest

from _vg_support import current_db, load_service_app
from services import employer_policy as ep
from services import posting_profile as pp
from services.employer_registry import employer_id_for

USER = "policy.user@example.com"
NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)


def _fresh(**kw):
    row = {"job_id": kw.pop("job_id", "j1"), "source_job_id": "s1",
           "title_raw": "Software Engineer", "status": "active",
           "last_verified_at": NOW - timedelta(hours=2)}
    row.update(kw)
    return row


def _op(**kw):
    """A posting on an owner-verified board of an OPERATING employer."""
    return _fresh(employer_name="Etsy", canonical_employer_id=employer_id_for(domain="etsy.com"), **kw)


@pytest.fixture(autouse=True)
def _clean():
    db = current_db()
    db.user_preferences.delete_many({})
    db.employers.delete_many({})
    db.job_postings.delete_many({})
    yield
    db.user_preferences.delete_many({})


# --- preferences -----------------------------------------------------------
def test_default_is_strict_with_unknown_shown():
    p = ep.get_prefs(USER)
    assert p["preset"] == "strict_direct" and p["set_by"] == "default"
    assert p["exclude"]["unknown_business_model"] is False
    assert all(p["exclude"][k] for k in ep.EXCLUDE_KEYS if k != "unknown_business_model")
    assert current_db().user_preferences.count_documents({}) == 0   # nothing written


def test_put_persists_and_is_never_overwritten_by_default():
    current_db().user_preferences.insert_one({"user_email": USER, "experience_level": "entry"})
    stored = ep.put_prefs(USER, {"preset": "off"})
    assert stored["preset"] == "off" and stored["set_by"] == "user"
    again = ep.get_prefs(USER)
    assert again["preset"] == "off" and again["set_by"] == "user"
    doc = current_db().user_preferences.find_one({"user_email": USER})
    assert doc["experience_level"] == "entry"        # other prefs untouched


def test_strict_with_a_changed_toggle_becomes_custom():
    p = ep.normalize_prefs({"preset": "strict_direct", "exclude": {"consultancy": False}})
    assert p["preset"] == "custom" and p["exclude"]["consultancy"] is False
    p = ep.normalize_prefs({"preset": "strict_direct",
                            "exclude": {"unknown_business_model": True}})
    assert p["preset"] == "strict_direct"
    assert ep.normalize_prefs({"preset": "$where"}) is None
    assert ep.normalize_prefs("strict") is None


def test_request_employer_kind_is_an_intentional_choice():
    eff = ep.effective_prefs(ep.default_prefs(), employer_kind="staffing")
    assert eff["exclude"]["staffing"] is False
    assert eff["overridden_by_request"]["lifted"] == ["staffing"]
    assert ep.effective_prefs(ep.default_prefs(), employer_kind="any")["exclude"]["staffing"]


# --- routes ------------------------------------------------------------------
def _client(identity):
    app = load_service_app("jobs")
    from flask_jwt_extended import create_access_token
    with app.app_context():
        tok = create_access_token(identity=identity)
    c = app.test_client()
    h = {"Authorization": f"Bearer {tok}"}
    return (lambda path: c.get(path, headers=h)), \
        (lambda path, body: c.put(path, headers=h, json=body)), c


def test_routes_round_trip_and_never_401_on_bad_body():
    get, put, raw = _client(USER)
    path = "/api/jobs/tech-feed/employer-preferences"
    assert raw.get(path).status_code == 401              # missing token only
    r = get(path)
    assert r.status_code == 200 and r.get_json()["set_by"] == "default"
    r = put(path, {"preset": "custom", "exclude": {"staffing": True, "consultancy": False}})
    assert r.status_code == 200
    body = r.get_json()
    assert body["preset"] == "custom" and body["exclude"]["consultancy"] is False
    assert get(path).get_json()["exclude"]["consultancy"] is False
    bad = put(path, {"preset": {"$ne": 1}})
    assert bad.status_code == 400
    assert put(path, ["x"]).status_code == 400


# --- posting profile ---------------------------------------------------------
def test_consultancy_internal_role_keeps_both_facts():
    row = _fresh(employer_name="Accenture", title_raw="Talent Acquisition Recruiter",
                 canonical_url="https://www.accenture.com/careers/1")
    kept, block = ep.apply([row], ep.default_prefs(), now=NOW, resolve=False)
    assert row["employer_profile"]["kind"] == "consultancy"
    assert row["posting_profile"]["relationship"] == "own_role"
    assert kept == [row]


def test_consultancy_delivery_role_is_excluded():
    row = _fresh(employer_name="Capgemini", title_raw="Java Developer",
                 canonical_url="https://careers.capgemini.com/job/1")
    kept, block = ep.apply([row], ep.default_prefs(), now=NOW, resolve=False)
    assert kept == [] and block["excluded_counts"]["consultancy"] == 1


def test_mixed_business_hidden_in_strict_and_itemised():
    row = _fresh(employer_name="IBM", title_raw="AI Back-End Developer",
                 canonical_url="https://careers.ibm.com/job/1")
    kept, block = ep.apply([row], ep.default_prefs(), now=NOW, resolve=False)
    assert kept == [] and block["excluded_counts"]["consultancy"] == 1
    assert block["reason_counts"]["mixed_unresolved"] == 1


def test_anonymous_client_placement_even_at_unknown_employer():
    row = _fresh(employer_name="Blueera Technologies", title_raw="Java Engineer",
                 description="Our client is seeking a Java engineer. C2C or W2 through our vendor.")
    kept, block = ep.apply([row], ep.default_prefs(), now=NOW, resolve=False)
    assert row["posting_profile"]["relationship"] == "undisclosed_client"
    assert kept == [] and block["excluded_counts"]["client_placement"] == 1


def test_named_client():
    row = _fresh(employer_name="Somefirm", description="End Client: Wells Fargo. Onsite.")
    assert pp.classify_posting(row, "unknown")["relationship"] == "named_client"


def test_w2_only_and_direct_contract_are_not_placement():
    w2 = _fresh(employer_name="CloudIngest", title_raw="Data Engineer (W2 only)",
                description="This is a W2 position. Contract to hire.")
    prof = pp.classify_posting(w2, "unknown")
    assert prof["relationship"] == "unknown"
    assert "w2_only_not_placement_evidence" in prof["evidence"]
    moz = _fresh(employer_name="Mozilla", title_raw="Software Engineer (Contract)",
                 canonical_employer_id=employer_id_for(domain="mozilla.org"))
    kept, _ = ep.apply([moz], ep.default_prefs(), now=NOW, resolve=False)
    assert moz["posting_profile"]["relationship"] == "own_role" and kept == [moz]


def test_task_gig_contract():
    row = _fresh(employer_name="Mercor", title_raw="MacOS Developer - Fully Remote | Up to $60/hr")
    kept, block = ep.apply([row], ep.default_prefs(), now=NOW, resolve=False)
    assert row["posting_profile"]["relationship"] == "freelance_task"
    assert kept == [] and block["excluded_counts"]["talent_marketplace"] == 1
    gig = _fresh(employer_name="Acme", title_raw="AI Trainer - evaluate AI responses")
    kept, block = ep.apply([gig], ep.default_prefs(), now=NOW, resolve=False)
    assert kept == [] and block["excluded_counts"]["task_gig"] == 1


def test_talent_communities_and_general_applications():
    for title, purpose in (("General Application", "resume_collection"),
                           ("Join our Talent Community", "talent_community"),
                           ("Future Opportunities - Engineering", "talent_community")):
        row = _op(title_raw=title)
        kept, block = ep.apply([row], ep.default_prefs(), now=NOW, resolve=False)
        assert row["posting_profile"]["purpose"] == purpose, title
        assert kept == [] and block["excluded_counts"]["talent_community"] == 1
    # "Data Pipeline Engineer" is a requisition, not a talent pipeline.
    row = _op(title_raw="Data Pipeline Engineer")
    assert pp.classify_posting(row, "operating")["purpose"] == "requisition"


def test_product_company_with_recruiting_words_is_not_staffing():
    row = _fresh(employer_name="Juicebox", title_raw="Security Engineer",
                 canonical_employer_id=employer_id_for(domain="juicebox.ai"))
    kept, _ = ep.apply([row], ep.default_prefs(), now=NOW, resolve=False)
    assert kept == [row] and row["employer_profile"]["kind"] == "operating"


def test_unknown_is_shown_as_unverified_never_direct():
    row = _fresh(employer_name="Zyxwv Labs")
    kept, block = ep.apply([row], ep.default_prefs(), now=NOW, resolve=False)
    assert kept == [row] and row["employer_profile"]["kind"] == "unknown"
    assert block["unverified_shown"] == 1
    strict_unknown = ep.normalize_prefs({"preset": "strict_direct",
                                         "exclude": {"unknown_business_model": True}})
    kept, block = ep.apply([_fresh(employer_name="Zyxwv Labs")], strict_unknown,
                           now=NOW, resolve=False)
    assert kept == [] and block["excluded_counts"]["unknown_business_model"] == 1


def test_off_preset_annotates_but_never_filters():
    rows = [_fresh(employer_name="Capgemini", canonical_url="https://careers.capgemini.com/1"),
            _fresh(employer_name="Hays", status="closed")]
    kept, block = ep.apply(rows, ep.normalize_prefs({"preset": "off"}), now=NOW, resolve=False)
    assert len(kept) == 2 and all("employer_profile" in r for r in kept)


# --- availability ------------------------------------------------------------
def test_availability_states():
    assert pp.availability(_op(), now=NOW)["state"] == "verified_open"
    closed = pp.availability(_op(status="closed", closed_reason="not-seen",
                                 closed_at=NOW), now=NOW)
    assert closed["state"] == "closed" and closed["method"] == "listing_seen"
    expired = pp.availability(_op(valid_through=NOW - timedelta(days=1)), now=NOW)
    assert expired["state"] == "closed"
    stale = pp.availability(_op(last_verified_at=NOW - timedelta(hours=100)), now=NOW)
    assert stale["state"] == "unknown" and stale["reason"] == "verification_stale"
    legacy = pp.availability({"title": "x"}, now=NOW)
    assert legacy["state"] == "unknown" and legacy["method"] == "none"


def test_blocked_probe_is_unverifiable_never_closed():
    blocked = pp.availability(_op(last_verified_at=NOW - timedelta(hours=60),
                                  liveness_outcome="blocked",
                                  liveness_outcome_at=NOW - timedelta(hours=1)), now=NOW)
    assert blocked["state"] == "unverifiable"
    backstop = pp.availability(_op(status="closed", closed_reason="unverified"), now=NOW)
    assert backstop["state"] == "unverifiable"


def test_undated_posting_rests_on_last_seen_not_posted_at():
    row = _op(posted_at=None, last_verified_at=None, last_seen_at=NOW - timedelta(hours=3))
    av = pp.availability(row, now=NOW)
    assert av["state"] == "verified_open" and av["verified_at"]
    # A recent posted_at with no observation proves nothing about today.
    row = _op(posted_at=NOW, last_verified_at=None)
    assert pp.availability(row, now=NOW)["state"] == "unknown"


def test_strict_excludes_closed_and_stale():
    rows = [_op(job_id="a", status="closed"),
            _op(job_id="b", last_verified_at=NOW - timedelta(hours=100)),
            _op(job_id="c")]
    kept, block = ep.apply(rows, ep.default_prefs(), now=NOW, resolve=False)
    assert [r["job_id"] for r in kept] == ["c"]
    assert block["excluded_counts"]["closed"] == 1
    assert block["excluded_counts"]["stale_verification"] == 1


def test_direct_apply_is_not_proof_of_direct_employer():
    """Gap 5: an own-ATS application path does not make an unknown employer direct."""
    row = _fresh(employer_name="Zyxwv Labs", cost_class="free",
                 canonical_url="https://job-boards.greenhouse.io/zyxwv/jobs/1")
    from services import tech_feed_service as tfs
    assert tfs._is_direct_apply(row) is True
    kept, _ = ep.apply([row], ep.default_prefs(), now=NOW, resolve=False)
    assert row["employer_profile"]["kind"] == "unknown"
    assert tfs._employer_kind_of(row) == "unknown"
