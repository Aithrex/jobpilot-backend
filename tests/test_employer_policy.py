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


def STRICT():
    return ep.normalize_prefs({"preset": "strict_direct"})


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
def test_default_is_the_owner_default_not_strict():
    """Owner decision 2026-10-23: staffing AND consultancies are hidden by
    default, recognized firms are not; unknown employers are shown."""
    p = ep.get_prefs(USER)
    assert p["preset"] == "custom" and p["set_by"] == "default"
    assert p["exclude"] == ep.DEFAULT_EXCLUDE
    assert p["exclude"]["unknown_business_model"] is False
    for k in ("staffing", "consultancy", "client_placement", "talent_marketplace",
              "task_gig", "talent_community"):
        assert p["exclude"][k] is True
    assert current_db().user_preferences.count_documents({}) == 0   # nothing written
    # strict_direct is unchanged and one PUT away.
    strict = ep.normalize_prefs({"preset": "strict_direct"})
    assert strict["preset"] == "strict_direct" and strict["exclude"] == ep.STRICT_EXCLUDE


def test_default_hides_body_shops_and_shows_recognized_firms():
    rows = [_fresh(job_id="c", employer_name="Capgemini", title_raw="Java Developer",
                   canonical_url="https://careers.capgemini.com/job/1"),
            _fresh(job_id="m", employer_name="IBM", canonical_url="https://careers.ibm.com/job/1"),
            _fresh(job_id="h", employer_name="Hays"),
            _fresh(job_id="i", employer_name="Insight Global"),
            _fresh(job_id="b", employer_name="PIT SOLUTIONS LLC", title_raw="Java Developer"),
            _fresh(job_id="s", employer_name="Acme Staffing LLC")]
    kept, block = ep.apply(rows, ep.default_prefs(), now=NOW, resolve=False)
    assert [r["job_id"] for r in kept] == ["c", "m", "h", "i"]
    assert kept[0]["employer_profile"]["kind"] == "consultancy"
    assert kept[1]["employer_profile"]["kind"] == "mixed"
    assert kept[2]["employer_profile"]["kind"] == "staffing"
    assert block["excluded_counts"]["consultancy"] == 1      # PIT SOLUTIONS
    assert block["excluded_counts"]["staffing"] == 1         # Acme Staffing


def test_strict_still_hides_recognized_firms_only_by_their_own_toggle():
    rows = [_fresh(job_id="i", employer_name="Insight Global")]
    off = ep.normalize_prefs({"preset": "custom", "exclude": {**ep.DEFAULT_EXCLUDE}})
    kept, _ = ep.apply(rows, off, now=NOW, resolve=False)
    assert [r["job_id"] for r in kept] == ["i"]


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
    kept, block = ep.apply([row], STRICT(), now=NOW, resolve=False)
    assert row["employer_profile"]["kind"] == "consultancy"
    assert row["posting_profile"]["relationship"] == "own_role"
    assert kept == [row]


def test_consultancy_delivery_role_is_excluded():
    row = _fresh(employer_name="Capgemini", title_raw="Java Developer",
                 canonical_url="https://careers.capgemini.com/job/1")
    kept, block = ep.apply([row], STRICT(), now=NOW, resolve=False)
    assert kept == [] and block["excluded_counts"]["consultancy"] == 1


def test_mixed_business_hidden_in_strict_and_itemised():
    row = _fresh(employer_name="IBM", title_raw="AI Back-End Developer",
                 canonical_url="https://careers.ibm.com/job/1")
    kept, block = ep.apply([row], STRICT(), now=NOW, resolve=False)
    assert kept == [] and block["excluded_counts"]["consultancy"] == 1
    assert block["reason_counts"]["mixed_unresolved"] == 1


def test_anonymous_client_placement_even_at_unknown_employer():
    row = _fresh(employer_name="Blueera Technologies", title_raw="Java Engineer",
                 description="Our client is seeking a Java engineer. C2C or W2 through our vendor.")
    kept, block = ep.apply([row], ep.default_prefs(), now=NOW, resolve=False)
    assert row["posting_profile"]["relationship"] == "undisclosed_client"
    assert kept == [] and block["excluded_counts"]["client_placement"] == 1


def test_unanticipated_client_sites_is_a_placement():
    # AGAP Technologies, served at 88 on 2026-10-22.
    row = _fresh(employer_name="AGAP TECHNOLOGIES INC", title_raw="SOFTWARE DEVELOPER",
                 description="Work under supervision. Travel and/or relocation to "
                             "unanticipated client sites throughout USA is required.")
    kept, block = ep.apply([row], ep.default_prefs(), now=NOW, resolve=False)
    assert row["posting_profile"]["relationship"] == "undisclosed_client"
    # AGAP is also a verified consultancy now, which is the reason charged.
    assert kept == [] and "client_placement" in row["policy"]["reasons"]
    assert block["excluded_counts"]["consultancy"] == 1


def test_a_visa_filing_notice_is_a_placement_but_not_at_an_operating_employer():
    body = ("POSITION: SOFTWARE DEVELOPER NUMBER OF OPENINGS: 5 JOB DUTIES: Design and "
            "develop CI/CD pipelines. Work under supervision. Requires a Master's degree in "
            "Computer Science or foreign equivalent.")
    assert "visa_filing_notice" in pp.signals_from_text("SOFTWARE DEVELOPER", body)
    row = _fresh(employer_name="Somefirm LLC", title_raw="SOFTWARE DEVELOPER", description=body)
    assert pp.classify_posting(row, "unknown")["relationship"] == "undisclosed_client"
    assert pp.classify_posting(row, "operating")["relationship"] != "undisclosed_client"
    # One phrase alone is ordinary job-ad wording.
    assert "visa_filing_notice" not in pp.signals_from_text(
        "Engineer", "Bachelor's degree or foreign equivalent required.")


def test_a_consultant_travelling_to_clients_is_not_a_placement():
    for text in ("Travel up to 50% to client sites.", "Travel to customer sites as needed."):
        assert "undisclosed_client" not in pp.signals_from_text("Senior Consultant", text)


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


def test_strict_excludes_closed_but_shows_stale_as_unverified():
    """A lagging crawl must never empty the default feed: stale is SHOWN and
    labelled; only closed / hard-expired is hidden. (Submission still refuses
    stale - see test_employer_policy_application.py.)"""
    rows = [_op(job_id="a", status="closed"),
            _op(job_id="b", last_verified_at=NOW - timedelta(hours=100)),
            _op(job_id="c")]
    kept, block = ep.apply(rows, ep.default_prefs(), now=NOW, resolve=False)
    assert [r["job_id"] for r in kept] == ["b", "c"]
    assert block["excluded_counts"]["closed"] == 1
    assert block["excluded_counts"]["stale_verification"] == 0
    assert block["availability_unverified_shown"] == 1
    assert kept[0]["availability"]["reason"] == "verification_stale"


def test_default_never_hides_unknown_employers():
    rows = [_fresh(job_id=f"u{i}", employer_name=f"Unheard Of Co {i}") for i in range(5)]
    kept, block = ep.apply(rows, ep.default_prefs(), now=NOW, resolve=False)
    assert len(kept) == 5 and block["unverified_shown"] == 5


def test_trust_hook_requires_an_offline_mode(monkeypatch):
    import sys, types
    calls = []
    mod = types.ModuleType("services.job_legitimacy_service")

    def assess_posting_trust(row):            # no offline switch
        calls.append(row)
        return {"state": "warnings"}
    mod.assess_posting_trust = assess_posting_trust
    monkeypatch.setitem(sys.modules, "services.job_legitimacy_service", mod)
    import services
    monkeypatch.setattr(services, "job_legitimacy_service", mod, raising=False)
    monkeypatch.setattr(ep, "_TRUST_RESOLVED", False)
    row = _op()
    ep.apply([row], ep.default_prefs(), now=NOW, resolve=False)
    assert "trust" not in row and calls == []

    # A module that declares the C4 contract is documented pure: used, and
    # the ownership marker is stamped first when a board map is available.
    mod.TRUST_VERSION = "trust-1.0.0"
    mod.board_ownership_conflict = lambda doc, own: own == "contested"
    monkeypatch.setattr(ep, "_TRUST_RESOLVED", False)
    row = _op()
    ep.annotate(row, now=NOW, ownership="contested")
    assert row["trust"] == {"state": "warnings"} and row["ownership_conflict"] is True
    del mod.TRUST_VERSION

    def offline_trust(row, *, allow_network=True):
        assert allow_network is False
        return {"state": "no_warnings_found", "warnings": [], "version": "t"}
    mod.assess_posting_trust = offline_trust
    monkeypatch.setattr(ep, "_TRUST_RESOLVED", False)
    row = _op()
    ep.apply([row], ep.default_prefs(), now=NOW, resolve=False)
    assert row["trust"]["state"] == "no_warnings_found"
    monkeypatch.setattr(ep, "_TRUST_RESOLVED", False)


def test_direct_apply_is_not_proof_of_direct_employer():
    """Gap 5: an own-ATS application path does not make an unknown employer direct."""
    row = _fresh(employer_name="Zyxwv Labs", cost_class="free",
                 canonical_url="https://job-boards.greenhouse.io/zyxwv/jobs/1")
    from services import tech_feed_service as tfs
    assert tfs._is_direct_apply(row) is True
    kept, _ = ep.apply([row], ep.default_prefs(), now=NOW, resolve=False)
    assert row["employer_profile"]["kind"] == "unknown"
    assert tfs._employer_kind_of(row) == "unknown"
