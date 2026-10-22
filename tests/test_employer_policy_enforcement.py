"""The SAME employer policy on every serving surface.

One seeded inventory, one user with the default (strict) policy, and every
surface asked the same question: the tech feed's ranked window, its keyset
phase, its facets, the inventory tabs, saved-search alerts, the legacy
payload path, the match sweep's selection and the submission gate. An
excluded posting must not appear anywhere, and every list must carry a
`policy` block whose counts are honest.
"""
from datetime import datetime, timedelta, timezone

import pytest

from _vg_support import current_db
from services import employer_policy as ep
from services import fit_score
from services import inventory_feed as feed
from services import job_content_store as store
from services import job_inventory_service as inv
from services import tech_feed_service as tfs
from services.employer_registry import employer_id_for

USER = "strict.seeker@example.com"
NOW = datetime.now(timezone.utc)
JD = ("Backend Engineer. Build services with Python, AWS and PostgreSQL. "
      "3+ years of experience.")
COLLECTIONS = ("job_postings", "job_source_state", "job_ingestion_runs",
               "tech_feed_searches", "user_preferences", "employers",
               "tech_feed_saved_searches", "companies", "company_ats_endpoints")

#: (key, company, url host, title, extra) -> expected to be SHOWN under strict?
SHAPES = [
    ("etsy", "Etsy", "https://boards.greenhouse.io/etsy/jobs/{i}", "Backend Engineer", True),
    ("capg", "Capgemini", "https://careers.capgemini.com/job/{i}", "Backend Engineer", False),
    ("hays", "Hays", "https://boards.greenhouse.io/hays/jobs/{i}", "Backend Engineer", False),
    ("ibm", "IBM", "https://careers.ibm.com/job/{i}", "Backend Engineer", False),
    ("unk", "Zyxwv Labs", "https://boards.greenhouse.io/zyxwv/jobs/{i}", "Backend Engineer", True),
    ("gen", "Etsy", "https://boards.greenhouse.io/etsy/jobs/9{i}", "General Application - Engineering", False),
]


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.delenv("RESUME_S3_BUCKET", raising=False)
    store.reset_caches()
    d = current_db()
    for c in COLLECTIONS:
        d[c].delete_many({})
    yield d
    for c in COLLECTIONS:
        d[c].delete_many({})


def _seed(per_shape=4):
    expected = {}
    for key, company, url, title, shown in SHAPES:
        for i in range(per_shape):
            sjid = f"{key}-{i}"
            inv.upsert_posting({
                "source": "career-pages", "source_job_id": sjid,
                "url": url.format(i=i), "company": company if key != "unk" else f"{company} {i}",
                "title": title, "location": ["Austin, TX, USA"], "description": JD,
                "employment_type": "Full time", "cost_class": "free",
                "source_quality": "direct", "industry": "tech", "ats": "greenhouse",
                "tenant": f"{key}{i}", "posted_at": NOW - timedelta(days=1 + i),
            }, now=NOW)
            doc = inv.get_posting("career-pages", sjid)
            if company == "Etsy":
                inv._postings().update_one(
                    {"source_job_id": sjid},
                    {"$set": {"canonical_employer_id": employer_id_for(domain="etsy.com")}})
            inv.mark_requirements_parsed("career-pages", sjid, doc["content_hash"],
                                         fit_score.extract_requirements(JD, title), now=NOW)
            expected[doc["job_id"]] = shown
    # One stale posting from an operating employer: hidden in strict mode.
    inv.upsert_posting({
        "source": "career-pages", "source_job_id": "stale-0",
        "url": "https://boards.greenhouse.io/etsy/jobs/777", "company": "Etsy",
        "title": "Backend Engineer", "location": ["Austin, TX, USA"], "description": JD,
        "employment_type": "Full time", "cost_class": "free", "source_quality": "direct",
        "industry": "tech", "ats": "greenhouse", "tenant": "etsystale",
        "posted_at": NOW - timedelta(days=2)}, now=NOW - timedelta(hours=100))
    stale = inv.get_posting("career-pages", "stale-0")
    inv._postings().update_one({"source_job_id": "stale-0"}, {"$set": {
        "canonical_employer_id": employer_id_for(domain="etsy.com"),
        "last_verified_at": NOW - timedelta(hours=100),
        "last_seen_at": NOW - timedelta(hours=100)}})
    expected[stale["job_id"]] = False
    return expected


PAYLOAD = {"user_email": USER, "posted_within_days": 30, "employer_scope": "all",
           "personalize": False}


def _walk(page_size, **extra):
    pages, cursor = [], None
    while True:
        res = tfs.search(dict(PAYLOAD, page_size=page_size, **extra,
                              **({"cursor": cursor} if cursor else {})))
        pages.append(res)
        cursor = res.get("next_cursor")
        if not cursor or len(pages) > 200:
            return pages


def _ids(pages):
    return [j["job_id"] for p in pages for j in p.get("jobs") or []]


def test_tech_feed_ranked_and_keyset_enforce_the_same_policy(db, monkeypatch):
    expected = _seed()
    hidden = {j for j, shown in expected.items() if not shown}
    # Whole result inside the ranked window...
    ranked = _walk(5)
    # ...and a window of 3, so most pages come from the keyset phase.
    monkeypatch.setattr(tfs, "CANDIDATE_LIMIT", 3)
    keyset = _walk(4)
    assert any((p.get("diagnostics") or {}).get("served_from") == "keyset" for p in keyset)
    for pages in (ranked, keyset):
        served = _ids(pages)
        assert not (set(served) & hidden), "an excluded posting was served"
        for p in pages:
            assert p["policy"]["preset"] == "strict_direct"
            assert p["policy"]["version"] == ep.POLICY_VERSION
        for job in (j for p in pages for j in p["jobs"]):
            assert job["employer_profile"]["kind"] in ("operating", "unknown")
            assert job["availability"]["state"] in ("verified_open", "unknown")
            assert job["employer_kind"] != "direct_employer" or \
                job["employer_profile"]["kind"] == "operating"
    # The allowed set is the same whichever phase served it.
    assert set(_ids(ranked)) == set(_ids(keyset))
    first = ranked[0]["policy"]["excluded_counts"]
    assert first["consultancy"] >= 8          # Capgemini + IBM (mixed)
    assert first["staffing"] == 4 and first["talent_community"] == 4
    assert first["stale_verification"] == 1
    assert ranked[0]["policy"]["unverified_shown"] == 4
    # Facets are post-policy for the windowed keys, and say which are not.
    kinds = {b.get("value") for b in ranked[0]["facets"].get("employer_kind") or []}
    assert "consultancy" not in kinds and "staffing" not in kinds
    assert "seniority" in ranked[0]["facets"]["_policy"]["not_applied"]


def test_stored_off_preference_shows_everything_and_survives(db):
    expected = _seed()
    ep.put_prefs(USER, {"preset": "off"})
    pages = _walk(50)
    served = set(_ids(pages))
    # Off never filters by policy: every seeded posting is served, the stale
    # one included, and each still carries its profile blocks.
    assert served == set(expected)
    assert pages[0]["policy"]["preset"] == "off"
    assert sum(pages[0]["policy"]["excluded_counts"].values()) == 0
    kinds = {j["employer_profile"]["kind"] for p in pages for j in p["jobs"]}
    assert {"consultancy", "staffing", "mixed"} <= kinds
    assert ep.get_prefs(USER)["set_by"] == "user"


def test_saved_search_employer_kind_is_honoured_as_intent(db):
    _seed()
    pages = _walk(50, employer_kind="staffing")
    jobs = [j for p in pages for j in p["jobs"]]
    assert jobs and all(j["employer_profile"]["kind"] == "staffing" for j in jobs)
    assert pages[0]["policy"]["overridden_by_request"]["lifted"] == ["staffing"]


def test_policy_change_mid_scroll_applies_to_the_next_page(db, monkeypatch):
    _seed()
    ep.put_prefs(USER, {"preset": "off"})
    first = tfs.search(dict(PAYLOAD, page_size=5))
    ep.put_prefs(USER, {"preset": "strict_direct"})
    pages = [first]
    cursor = first.get("next_cursor")
    while cursor:
        res = tfs.search(dict(PAYLOAD, page_size=5, cursor=cursor))
        pages.append(res)
        cursor = res.get("next_cursor")
    for p in pages[1:]:
        for j in p["jobs"]:
            assert j["employer_profile"]["kind"] in ("operating", "unknown")
            assert j["posting_profile"]["purpose"] != "resume_collection"


def test_inventory_tabs_enforce_the_same_policy(db, monkeypatch):
    expected = _seed()
    monkeypatch.setattr(feed, "MIN_ROWS", 1)
    res = feed.build_feed("career-pages", {
        "titles": ["Backend Engineer"], "us_only": True, "user_email": USER,
        "resume_skills": ["Python"], "resume_years": 3})
    assert res is not None
    served = {r["job_id"] for r in res["jobs"]}
    assert not (served & {j for j, shown in expected.items() if not shown})
    assert res["policy"]["preset"] == "strict_direct"
    assert res["policy"]["excluded_counts"]["staffing"] == 4
    for r in res["jobs"]:
        assert "employer_profile" in r and "_employer_profile" not in r


def test_legacy_payload_path_filters_and_counts():
    payload = {"jobs": [
        {"company": "Capgemini", "title": "Java Dev", "apply_link": "https://careers.capgemini.com/1"},
        {"company": "Robert Half", "title": "Java Dev", "apply_link": "https://linkedin.com/jobs/1"},
        {"company": "Zyxwv Labs", "title": "Java Dev", "apply_link": "https://linkedin.com/jobs/2",
         "description": "Our client is seeking a Java developer."},
        {"company": "Zyxwv Labs", "title": "Python Dev", "apply_link": "https://linkedin.com/jobs/3"},
    ], "total": 4}
    out = ep.apply_to_payload(payload, ep.default_prefs())
    assert [j["title"] for j in out["jobs"]] == ["Python Dev"]
    assert out["total"] == 1
    c = out["policy"]["excluded_counts"]
    assert c["consultancy"] == 1 and c["staffing"] == 1 and c["client_placement"] == 1
    assert "_employer_profile" not in out["jobs"][0]


def test_legacy_async_results_and_raw_cache_are_filtered(db):
    """`/api/jobs/search`, the crawl fallbacks and the pipeline complete through
    `resume_service._apply_employer_policy`; `_raw_jobs` must not leak."""
    from services.resume_service import _apply_employer_policy
    rows = lambda: [  # noqa: E731
        {"company": "Capgemini", "title": "Dev", "apply_link": "https://careers.capgemini.com/1"},
        {"company": "Zyxwv Labs", "title": "Dev", "apply_link": "https://linkedin.com/jobs/9"}]
    res = _apply_employer_policy({"jobs": rows(), "_raw_jobs": rows(), "total": 2}, USER)
    assert [j["company"] for j in res["jobs"]] == ["Zyxwv Labs"]
    assert [j["company"] for j in res["_raw_jobs"]] == ["Zyxwv Labs"]
    assert res["policy"]["excluded_counts"]["consultancy"] == 1
    pipe = _apply_employer_policy({"apply_now": rows(), "verify_dates": rows()}, USER,
                                  ("apply_now", "verify_dates"))
    assert len(pipe["apply_now"]) == 1 and len(pipe["verify_dates"]) == 1
    # An inventory payload that already carries a policy block is untouched.
    pre = {"jobs": rows(), "policy": {"preset": "off"}}
    assert len(_apply_employer_policy(pre, USER)["jobs"]) == 2
