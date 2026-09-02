"""`/api/jobs/tech-feed/companies` — the contract, the auth and the leak surface.

Mounted under the existing `/api/jobs/tech-feed` subpath on purpose: a new
top-level `/api/<prefix>` needs a matching entry in `var.service_config` and
404s in prod until it exists (CLAUDE.md invariant 6).
"""
from __future__ import annotations

import pytest

from _vg_support import current_db, load_service_app
from services import career_source_service as css
from services import employer_registry as er
from services.career_source_service import evidence

USER = "seeker@example.com"


@pytest.fixture()
def app():
    return load_service_app("jobs")


@pytest.fixture()
def client(app):
    from flask_jwt_extended import create_access_token
    with app.app_context():
        tok = create_access_token(identity=USER)
    c = app.test_client()

    def call(path):
        return c.get(path, headers={"Authorization": f"Bearer {tok}"})
    return call


@pytest.fixture(autouse=True)
def seeded():
    db = current_db()
    db["employers"].delete_many({})
    db[css.CAREER_SOURCES].delete_many({})
    db["job_postings"].delete_many({})
    db["employers"].insert_one({
        "employer_id": "emp_000000000001", "display_name": "Stripe",
        "name_key": "stripe", "aliases": ["Stripe"], "brands": [],
        "operating_status": er.STATUS_ACTIVE, "identity_confidence": 0.95,
        "id_basis": "domain", "canonical_domain": "stripe.com",
        "employer_categories": ["fintech", "other"], "cohorts": ["private_growth"],
        "evidence": [{"kind": "curated_seed", "source": "tech_companies.py"}],
    })
    db[css.CAREER_SOURCES].insert_one(dict(css.build_source(
        employer_id="emp_000000000001", ats_vendor="greenhouse", tenant="stripe",
        job_count_last=12,
        ownership_evidence=[evidence(css.SIGNAL_DOMAIN_LINK,
                                     source="https://stripe.com/jobs",
                                     value="stripe.com -> greenhouse:stripe",
                                     confirmed=True)],
        content_validation_evidence=[evidence("content_fetch", source="greenhouse",
                                              value="12 postings", confirmed=True)])))
    return db


class TestContract:

    def test_a_name_returns_the_canonical_id(self, client):
        r = client("/api/jobs/tech-feed/companies?q=stri")
        assert r.status_code == 200
        body = r.get_json()
        assert body["companies"][0]["employer_id"] == "emp_000000000001"
        assert body["companies"][0]["display_name"] == "Stripe"
        assert body["companies"][0]["career_source_verified"] is True

    def test_other_is_dropped_when_a_specific_category_is_present(self, client):
        cats = client("/api/jobs/tech-feed/companies?q=stripe"
                      ).get_json()["companies"][0]["categories"]
        assert cats == ["fintech"]

    def test_ids_resolve_back_to_names(self, client):
        body = client("/api/jobs/tech-feed/companies?ids=emp_000000000001").get_json()
        assert body["companies"][0]["display_name"] == "Stripe"

    def test_the_category_facet_is_canonical_and_counted(self, client):
        r = client("/api/jobs/tech-feed/company-categories")
        assert r.status_code == 200
        values = {c["value"]: c["employers"] for c in r.get_json()["categories"]}
        assert values.get("fintech") == 1


class TestAuthAndSafety:

    def test_the_endpoint_requires_a_token(self, app):
        r = app.test_client().get("/api/jobs/tech-feed/companies?q=stripe")
        assert r.status_code == 401

    def test_no_ownership_evidence_reaches_the_wire(self, client):
        raw = client("/api/jobs/tech-feed/companies?q=stripe").get_data(as_text=True)
        for leak in ("ownership_evidence", "greenhouse:stripe", "canonical_endpoint",
                     "member_key", "redirect_chain", "verified_domain",
                     "https://stripe.com/jobs"):
            assert leak not in raw, f"{leak!r} leaked to a public payload"

    def test_a_hostile_query_is_data_not_a_regex(self, client):
        r = client("/api/jobs/tech-feed/companies?q=.%2A")
        assert r.status_code == 200
        assert r.get_json()["companies"] == []
