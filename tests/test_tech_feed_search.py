"""The unified tech-feed contract: filters, cursors, facets, and the invariants.

These are the acceptance criteria stated as tests. In particular:

* an excluded term can never add a job (the P0 inversion, again, on this path);
* unknown salary / unknown posting date / unknown seniority never exclude a
  posting and are never rendered as a value;
* cursor pages contain no duplicates and no omissions under a stable
  watermark, and paging does no ingestion and no ranking;
* `match_score` is `fit_score` and nothing else; `rank_score` is separate;
* a nontechnical role at a technology employer is not core-tech, and a
  software role at a nontechnical employer is.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services import fit_score
from services import job_content_store as store
from services import job_inventory_service as inv
from services import tech_feed_service as tfs

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
COLLECTIONS = ("job_postings", "job_source_state", "job_ingestion_runs",
               "tech_feed_searches", "ingest_runner_heartbeat", "companies",
               "company_ats_endpoints")

SWE_JD = ("Senior Backend Engineer. Requirements: 5+ years of experience with "
          "Python, AWS, Kubernetes and PostgreSQL. Preferred: Terraform, Go.")


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.delenv("RESUME_S3_BUCKET", raising=False)
    store.reset_caches()
    d = inv._db()
    for c in COLLECTIONS:
        d[c].delete_many({})
    yield d
    for c in COLLECTIONS:
        d[c].delete_many({})


def seed(*, source="career-pages", sjid, title, company="Acme Inc",
         description=SWE_JD, location="Austin, TX, USA", days=2,
         ats="greenhouse", tenant="acme", comp=None, work_mode=None,
         url=None, parse=True, employment_type="Full time"):
    raw = {
        "source": source, "source_job_id": sjid,
        "url": url or f"https://boards.greenhouse.io/{tenant}/jobs/{sjid}",
        "company": company, "title": title, "location": [location],
        "description": description, "employment_type": employment_type,
        "cost_class": "free", "source_quality": "direct", "industry": "tech",
        "ats": ats, "tenant": tenant, "posted_at": NOW - timedelta(days=days),
    }
    if comp is not None:
        raw["compensation"] = comp
    if work_mode:
        raw["work_mode"] = work_mode
    inv.upsert_posting(raw, now=NOW)
    if parse:
        doc = inv.get_posting(source, sjid)
        inv.mark_requirements_parsed(
            source, sjid, doc["content_hash"],
            fit_score.extract_requirements(description, title), now=NOW)
    return sjid


def search(**over):
    body = {"tech_scope": ["core"], "page_size": 25, "personalize": False,
            "posted_within_days": 30}
    body.update(over)
    return tfs.search(body)


def titles(result):
    return [j["title_raw"] for j in result["jobs"]]


# ---------------------------------------------------------------------------
# Request normalization - nothing unvalidated reaches Mongo
# ---------------------------------------------------------------------------

class TestNormalizeRequest:
    def test_unknown_families_and_specialties_are_dropped(self):
        r = tfs.normalize_request({"role_families": ["software_engineering",
                                                     "'; drop --", "bogus"],
                                   "role_specialties": ["backend", "nope"]})
        assert r["role_families"] == ["software_engineering", "cloud_platform"] or \
               "bogus" not in r["role_families"]
        assert "nope" not in r["role_specialties"]

    def test_a_specialty_implies_its_family(self):
        r = tfs.normalize_request({"role_specialties": ["sre"]})
        assert "cloud_platform" in r["role_families"]

    def test_page_size_is_clamped(self):
        assert tfs.normalize_request({"page_size": 10 ** 6})["page_size"] == tfs.MAX_PAGE_SIZE
        assert tfs.normalize_request({"page_size": 0})["page_size"] == tfs.DEFAULT_PAGE_SIZE
        assert tfs.normalize_request({"page_size": "x"})["page_size"] == tfs.DEFAULT_PAGE_SIZE

    def test_an_unknown_sort_falls_back_to_relevance(self):
        assert tfs.normalize_request({"sort": "; drop"})["sort"] == tfs.SORT_RELEVANCE

    def test_default_scope_is_core(self):
        assert tfs.normalize_request({})["tech_scope"] == ["core"]

    def test_include_unknown_salary_defaults_true(self):
        assert tfs.normalize_request({})["include_unknown_salary"] is True

    def test_query_operators_cannot_reach_mongo(self):
        """A dict in a list position must not survive into the filter."""
        r = tfs.normalize_request({"company_ids": [{"$ne": None}],
                                   "titles": [{"$gt": ""}],
                                   "exclude_companies": [{"$where": "1"}]})
        q = tfs.build_query(r)

        def user_values(node):
            """Every leaf value that could have come from the request body."""
            if isinstance(node, dict):
                for k, v in node.items():
                    yield from user_values(v)
            elif isinstance(node, (list, tuple)):
                for v in node:
                    yield from user_values(v)
            else:
                yield node

        # The dicts survive only as STRINGS - a value position, never an
        # operator position. That is the whole invariant: a user cannot
        # introduce a `$` key into the filter.
        for v in user_values(q):
            assert not isinstance(v, dict)
        assert all(not str(v).startswith("$") for v in user_values(q))

    def test_max_years_below_min_is_raised_not_inverted(self):
        r = tfs.normalize_request({"min_years": 8, "max_years": 2})
        assert r["max_years"] == 8


# ---------------------------------------------------------------------------
# Cursors
# ---------------------------------------------------------------------------

class TestCursor:
    def test_offset_round_trip(self):
        assert tfs.decode_cursor(tfs.encode_cursor("abc123", 75)) == ("abc123", 75, None)

    def test_keyset_round_trip(self):
        """The second cursor shape: a position in index order rather than an
        offset into a frozen list. This is what pages past the 2,000-row
        ranked window."""
        from datetime import datetime, timezone
        posted = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
        raw = tfs.encode_cursor("abc123", keyset={"p": posted, "j": "job-9"})
        sid, off, keyset = tfs.decode_cursor(raw)
        assert sid == "abc123" and off == 0
        assert keyset["j"] == "job-9"
        assert keyset["p"] == posted

    def test_an_undated_keyset_round_trips_as_none(self):
        """18,222 live postings carry no date. The undated tail is a real
        position in the traversal, not a missing one."""
        raw = tfs.encode_cursor("abc123", keyset={"p": None, "j": "job-1"})
        _sid, _off, keyset = tfs.decode_cursor(raw)
        assert keyset == {"p": None, "j": "job-1"}

    @pytest.mark.parametrize("bad", [
        None, "", "not-base64!!", "e30", "!!!!",
        "eyJzIjoiYSIsIm8iOi0xfQ",          # negative offset
        "x" * 600,                          # absurdly long
    ])
    def test_malformed_cursors_return_none_and_never_raise(self, bad):
        assert tfs.decode_cursor(bad) in (None, ("a", 0, None))

    def test_a_keyset_with_an_unparseable_date_is_refused(self):
        import base64 as _b64
        import json as _json
        raw = _b64.urlsafe_b64encode(_json.dumps(
            {"s": "a", "k": {"p": "not-a-date", "j": "x"}}).encode()).decode().rstrip("=")
        assert tfs.decode_cursor(raw) is None


class TestKeysetPredicate:
    """`(posted_at desc, job_id asc)` with an undated tail.

    BSON orders `Null < Date`, so descending puts undated postings LAST and
    `$lt: <date>` already includes them. Getting this wrong either skips
    18,222 postings or serves them on every page.
    """

    def test_dated_position_advances_on_both_keys(self):
        from datetime import datetime, timezone
        posted = datetime(2026, 8, 30, tzinfo=timezone.utc)
        pred = tfs.keyset_predicate({"p": posted, "j": "j5"})
        bound, tie = pred["$and"]
        assert tie["$or"][0] == {"posted_at": {"$lt": posted}}
        assert tie["$or"][1] == {"posted_at": posted, "job_id": {"$gt": "j5"}}
        # The upper bound is not redundant with the `$or` - it is what lets
        # Mongo bound the index range. Measured on live `job_postings`:
        # 71,333 keys examined and 13.6s WITHOUT it, 250 keys and 24ms with.
        assert bound == {"posted_at": {"$lte": posted}}

    def test_the_range_bound_shrinks_as_paging_advances(self):
        """Later pages get CHEAPER, not more expensive: each page lowers the
        upper bound, so the scanned range closes toward the freshness cutoff."""
        from datetime import datetime, timedelta, timezone
        first = datetime(2026, 8, 30, tzinfo=timezone.utc)
        later = first - timedelta(days=5)
        a = tfs.keyset_predicate({"p": first, "j": "j1"})["$and"][0]
        b = tfs.keyset_predicate({"p": later, "j": "j9"})["$and"][0]
        assert b["posted_at"]["$lte"] < a["posted_at"]["$lte"]

    def test_the_undated_tail_orders_by_job_id_alone(self):
        assert tfs.keyset_predicate({"p": None, "j": "j5"}) == {
            "posted_at": None, "job_id": {"$gt": "j5"}}


# ---------------------------------------------------------------------------
# Occupation vs employer - the two failures the product must not have
# ---------------------------------------------------------------------------

class TestScopeSeparation:
    def test_a_nontechnical_role_at_a_tech_employer_is_not_core(self, db):
        seed(sjid="s1", title="Senior Backend Engineer", company="Stripe")
        seed(sjid="s2", title="Retail Store Manager", company="Apple",
             description="Run the store team and drive sales targets.")
        got = titles(search())
        assert "Senior Backend Engineer" in got
        assert "Retail Store Manager" not in got

    def test_a_software_role_at_a_nontech_employer_is_core(self, db):
        seed(sjid="h1", title="Senior Software Engineer",
             company="Cleveland Clinic",
             description="Java and Spring Boot for our clinical platform. "
                         "5+ years of experience.")
        assert "Senior Software Engineer" in titles(search())

    def test_adjacent_is_selectable_and_separate_from_core(self, db):
        seed(sjid="a1", title="Technical Program Manager", company="Acme",
             description="Drive multi-team platform migrations.")
        seed(sjid="a2", title="Backend Engineer", company="Acme")
        assert titles(search(tech_scope=["core"])) == ["Backend Engineer"]
        assert titles(search(tech_scope=["adjacent"])) == ["Technical Program Manager"]
        both = set(titles(search(tech_scope=["core", "adjacent"])))
        assert both == {"Backend Engineer", "Technical Program Manager"}

    def test_a_physical_engineering_role_is_not_core(self, db):
        seed(sjid="p1", title="Corrosion Reliability Engineer II", company="Acme",
             description="Cathodic protection surveys and pipeline inspection.")
        seed(sjid="p2", title="Site Reliability Engineer", company="Acme")
        assert titles(search()) == ["Site Reliability Engineer"]


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

class TestFilters:
    def test_an_excluded_title_term_removes_and_never_adds(self, db):
        seed(sjid="e1", title="Backend Engineer", company="Acme")
        seed(sjid="e2", title="Frontend Engineer", company="Acme")
        got = titles(search(exclude_title_terms=["frontend"]))
        assert got == ["Backend Engineer"]

    def test_an_exclusion_beats_every_positive_signal(self, db):
        seed(sjid="e1", title="Backend Engineer", company="Acme")
        got = titles(search(titles=["Backend Engineer"],
                            role_specialties=["backend"],
                            skills_required=["python"],
                            exclude_title_terms=["backend"]))
        assert got == []

    def test_an_excluded_company_is_removed(self, db):
        seed(sjid="c1", title="Backend Engineer", company="Acme")
        seed(sjid="c2", title="Backend Engineer", company="Beta", tenant="beta")
        got = [j["employer_name"] for j in search(exclude_companies=["Acme"])["jobs"]]
        assert "Acme" not in " ".join(got)

    def test_specialty_filter(self, db):
        seed(sjid="f1", title="Site Reliability Engineer", company="Acme")
        seed(sjid="f2", title="iOS Engineer", company="Acme")
        assert titles(search(role_specialties=["sre"])) == ["Site Reliability Engineer"]

    def test_salary_floor_drops_a_known_low_band(self, db):
        seed(sjid="s1", title="Backend Engineer", company="Acme",
             comp={"min": 80000, "max": 95000, "currency": "USD",
                   "interval": "year"})
        seed(sjid="s2", title="Backend Engineer", company="Beta", tenant="beta",
             comp={"min": 180000, "max": 220000, "currency": "USD",
                   "interval": "year"})
        got = [j["employer_name"] for j in search(min_salary=150000)["jobs"]]
        assert got == ["Beta"]

    def test_unknown_salary_is_kept_by_default(self, db):
        seed(sjid="s1", title="Backend Engineer", company="Acme")   # no comp
        assert len(search(min_salary=150000)["jobs"]) == 1

    def test_unknown_salary_is_dropped_only_on_explicit_opt_out(self, db):
        seed(sjid="s1", title="Backend Engineer", company="Acme")
        assert search(min_salary=150000, include_unknown_salary=False)["jobs"] == []

    def test_unknown_salary_renders_as_not_published_never_as_zero(self, db):
        seed(sjid="s1", title="Backend Engineer", company="Acme")
        job = search()["jobs"][0]
        assert job["salary"]["published"] is False
        assert job["salary"]["min_annual"] is None
        assert job["salary"]["max_annual"] is None

    def test_remote_filter(self, db):
        seed(sjid="r1", title="Backend Engineer", company="Acme",
             work_mode="remote", location="Remote, USA")
        seed(sjid="r2", title="Backend Engineer", company="Beta", tenant="beta")
        got = [j["work_mode"] for j in search(work_modes=["remote"])["jobs"]]
        assert got == ["remote"]

    def test_unknown_seniority_is_not_excluded_by_a_seniority_filter(self, db):
        seed(sjid="u1", title="Software Engineer", company="Acme",
             description="Build things with Python.")
        assert len(search(seniority=["senior"])["jobs"]) == 1

    def test_an_unknown_posting_date_is_labelled_not_invented(self, db):
        inv.upsert_posting({
            "source": "icims", "source_job_id": "u1",
            "url": "https://careers-acme.icims.com/jobs/1",
            "company": "Acme", "title": "Backend Engineer",
            "location": ["Austin, TX"], "description": SWE_JD,
            "cost_class": "free", "ats": "icims", "tenant": "acme",
            "posted_at": None,
        }, now=NOW)
        job = search(include_undated=True)["jobs"][0]
        assert job["posted_at"] is None
        assert job["posted_at_confidence"] == "unknown"
        assert job["first_seen_at"] is not None


# ---------------------------------------------------------------------------
# Scores
# ---------------------------------------------------------------------------

class TestEmployerScope:
    """`employer_scope=tech_only` must actually filter.

    An earlier implementation classified employers from the posting mix over
    the CANDIDATE SET alone. Measured on live data, a 2,000-row candidate set
    held 191 distinct employers of which 4 cleared the mix floor (median 1 row
    each) - so the filter rendered in the UI, appeared in `applied_filters`,
    and dropped nothing. A filter that does nothing is worse than no filter,
    because the user believes it worked.
    """

    def _seed_two_employers(self, db):
        db.companies.insert_many([
            {"company_id": "c-soft", "name": "Cadence Design Systems",
             "name_normalized": "CADENCE DESIGN SYSTEMS", "sic": "7372",
             "sic_description": "Services-Prepackaged Software"},
            {"company_id": "c-bank", "name": "Big Retail Bank",
             "name_normalized": "BIG RETAIL BANK", "sic": "6022",
             "sic_description": "State Commercial Banks"},
        ])
        seed(sjid="t1", title="Backend Engineer",
             company="Cadence Design Systems", tenant="cadence")
        seed(sjid="t2", title="Backend Engineer",
             company="Big Retail Bank", tenant="bigbank")

    def test_tech_only_drops_a_non_tech_employer(self, db):
        self._seed_two_employers(db)
        res = search(employer_scope="tech_only")
        names = [j["employer_name"] for j in res["jobs"]]
        assert "Cadence Design Systems" in names
        assert "Big Retail Bank" not in names
        assert res["diagnostics"]["employer_scope_dropped"] == 1

    def test_the_default_scope_keeps_both(self, db):
        self._seed_two_employers(db)
        names = {j["employer_name"] for j in search()["jobs"]}
        assert names == {"Cadence Design Systems", "Big Retail Bank"}

    def test_an_unclassifiable_employer_is_kept_not_dropped(self, db):
        """Unknown must never silently narrow the market."""
        seed(sjid="u1", title="Backend Engineer", company="Nobody Ever Heard Of",
             tenant="nobody")
        res = search(employer_scope="tech_only")
        assert len(res["jobs"]) == 1
        assert res["jobs"][0]["employer_tech_classification"] == "unknown"

    def test_the_card_carries_the_employer_evidence(self, db):
        self._seed_two_employers(db)
        card = [j for j in search(employer_scope="tech_only")["jobs"]
                if j["employer_name"] == "Cadence Design Systems"][0]
        assert card["employer_tech_classification"] == "software"
        assert card["employer_tech_evidence"]
        assert "7372" in str(card["employer_tech_evidence"])

    def test_a_software_role_at_a_nontech_employer_survives_the_DEFAULT_scope(self, db):
        """The product's core promise: tech roles at ANY employer, by default."""
        db.companies.insert_one(
            {"company_id": "c-hosp", "name": "Cleveland Clinic",
             "name_normalized": "CLEVELAND CLINIC", "sic": "8062",
             "sic_description": "Services-General Medical & Surgical Hospitals"})
        seed(sjid="h1", title="Senior Software Engineer",
             company="Cleveland Clinic", tenant="clinic")
        assert len(search()["jobs"]) == 1


class TestScores:
    def test_no_resume_means_no_score_and_no_version_marker(self, db):
        seed(sjid="n1", title="Backend Engineer", company="Acme")
        job = search(personalize=True, user_email="nobody@example.com")["jobs"][0]
        assert job["match_score"] == 0
        assert job["fit_score_version"] is None

    def test_rank_score_is_separate_from_match_score(self, db):
        seed(sjid="n1", title="Backend Engineer", company="Acme")
        job = search()["jobs"][0]
        assert job["rank_score"] is not None
        assert job["rank_score"] != job["match_score"]

    def test_the_envelope_states_both_versions(self, db):
        seed(sjid="n1", title="Backend Engineer", company="Acme")
        res = search()
        assert res["taxonomy_version"]
        assert res["fit_score_version"]

    def test_every_card_carries_its_classification_evidence(self, db):
        seed(sjid="n1", title="Backend Engineer", company="Acme")
        job = search()["jobs"][0]
        assert job["taxonomy_version"]
        assert job["classification_evidence"]
        assert job["classification_confidence"] > 0


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

class TestSkillFilters:
    """The skill filters read `requirements.required_skills`, not `.skills`.

    An earlier version read `requirements["skills"]`, a key
    `fit_score.extract_requirements` does not produce. With `tech.*` also
    unbackfilled the skill set was empty for every row, so BOTH skill filters
    matched everything: `skills_required=["kubernetes"]` returned 435 results
    on live data, identical to no filter at all.
    """

    def _seed_pair(self):
        seed(sjid="k1", title="Platform Engineer", company="Acme",
             description="Requirements: 5+ years with Python, Kubernetes and "
                         "Terraform.")
        seed(sjid="k2", title="Frontend Engineer", company="Beta", tenant="beta",
             description="Requirements: React, TypeScript and CSS.")

    def test_a_required_skill_narrows(self, db):
        self._seed_pair()
        assert len(search()["jobs"]) == 2
        got = search(skills_required=["kubernetes"])
        assert [j["title_raw"] for j in got["jobs"]] == ["Platform Engineer"]

    def test_an_excluded_skill_removes(self, db):
        self._seed_pair()
        got = search(exclude_skills=["react"])
        assert [j["title_raw"] for j in got["jobs"]] == ["Platform Engineer"]

    def test_a_posting_whose_skills_we_could_not_parse_is_kept(self, db):
        """Unknown is not a no. A row we never parsed must not fail a test it
        was never given - and the count is reported so a filter that is
        silently doing nothing stays visible."""
        seed(sjid="n1", title="Backend Engineer", company="Acme",
             description="", parse=False)
        got = search(skills_required=["kubernetes"])
        assert len(got["jobs"]) == 1
        assert got["diagnostics"]["filtered_out"].get("_skills_unknown_kept") == 1


class TestSalaryPlausibility:
    """`salary_normalizer` misreads a small, highly visible slice of pay text.

    Live inventory: 2.4% of priced postings claim over $1M/year, and the raw
    text shows why - a page fragment read as "$5 - $630 an hour", a number
    split mid-digit into "$123,500 a month". Under a salary sort every one of
    them landed on the first page.
    """

    @pytest.mark.parametrize("comp, why", [
        ({"min": 5, "max": 630, "period": "hour", "currency": "USD"},
         "$630/hr is a parse error, $250/hr is a real contract rate"),
        ({"min": 90000, "max": 110000, "period": "month", "currency": "USD"},
         "a monthly figure above $60k is an annual figure mislabelled"),
        ({"min": 500, "max": 215000, "period": "month", "currency": "USD"},
         "a 430x spread is two unrelated numbers, not a range"),
        ({"min": 2000000, "max": 9000000, "period": "year", "currency": "USD"},
         "above the annual ceiling"),
    ])
    def test_an_implausible_band_is_treated_as_unknown(self, db, comp, why):
        seed(sjid="p1", title="Backend Engineer", company="Acme", comp=comp)
        job = search()["jobs"][0]
        assert job["salary"]["plausible"] is False, why

    def test_a_real_band_is_plausible(self, db):
        seed(sjid="p1", title="Backend Engineer", company="Acme",
             comp={"min": 180000, "max": 240000, "period": "year",
                   "currency": "USD"})
        job = search()["jobs"][0]
        assert job["salary"]["plausible"] is True
        assert job["salary"]["published"] is True

    def test_the_employers_own_text_is_still_shown(self, db):
        """We distrust the NUMBER, we do not hide what the employer wrote."""
        seed(sjid="p1", title="Backend Engineer", company="Acme",
             comp={"min": 90000, "max": 110000, "period": "month",
                   "currency": "USD"})
        job = search()["jobs"][0]
        assert job["salary"]["display"]
        assert job["salary"]["plausible"] is False

    def test_an_implausible_band_does_not_win_a_salary_sort(self, db):
        seed(sjid="good", title="Backend Engineer", company="Real",
             tenant="real",
             comp={"min": 300000, "max": 400000, "period": "year",
                   "currency": "USD"})
        seed(sjid="junk", title="Backend Engineer", company="Junk",
             tenant="junk",
             comp={"min": 90000, "max": 110000, "period": "month",
                   "currency": "USD"})
        got = search(sort="salary")
        assert got["jobs"][0]["employer_name"] == "Real"

    def test_an_implausible_band_does_not_pass_a_salary_floor(self, db):
        seed(sjid="junk", title="Backend Engineer", company="Junk",
             comp={"min": 90000, "max": 110000, "period": "month",
                   "currency": "USD"})
        assert search(min_salary=500000, include_unknown_salary=False)["jobs"] == []

    def test_the_interval_is_read_from_the_stored_field_name(self, db):
        """Stored as `period`; the contract calls it `interval`. Reading
        `interval` off the document returned null on every card."""
        seed(sjid="p1", title="Backend Engineer", company="Acme",
             comp={"min": 180000, "max": 240000, "period": "year",
                   "currency": "USD"})
        assert search()["jobs"][0]["salary"]["interval"] == "year"


class TestBroadeningSuggestions:
    def test_a_zero_result_search_says_what_to_relax(self, db):
        seed(sjid="b1", title="Backend Engineer", company="Acme")
        got = search(role_specialties=["browsers"])
        assert got["total"] == 0
        assert got["broadening_suggestions"]
        assert any("specialty" in s["label"] for s in got["broadening_suggestions"])

    def test_each_suggestion_carries_the_exact_patch_to_apply(self, db):
        seed(sjid="b1", title="Backend Engineer", company="Acme")
        got = search(role_specialties=["browsers"], min_salary=900000,
                     include_unknown_salary=False)
        for s in got["broadening_suggestions"]:
            assert isinstance(s["patch"], dict) and s["patch"]
            assert s["label"]

    def test_suggestions_are_ranked_by_what_actually_cut(self, db):
        for i in range(12):
            seed(sjid=f"b{i}", title=f"Backend Engineer {i}",
                 company=f"Co {i}", tenant=f"co{i}")
        got = search(role_specialties=["browsers"])
        removed = [s["removed"] for s in got["broadening_suggestions"]]
        assert removed == sorted(removed, reverse=True)

    def test_a_search_with_results_gets_no_suggestions(self, db):
        seed(sjid="b1", title="Backend Engineer", company="Acme")
        got = search()
        assert got["total"] == 1
        assert got["broadening_suggestions"] == []

    def test_a_zero_result_search_always_says_something(self, db):
        """Even with no filter obviously responsible, silence is not an answer."""
        got = search()
        assert got["total"] == 0
        assert got["warnings"]


class TestPagination:
    def _seed_many(self, n=30):
        for i in range(n):
            seed(sjid=f"p{i:03d}", title=f"Backend Engineer {i}",
                 company=f"Co {i}", tenant=f"co{i}", days=(i % 20) + 1)

    def test_pages_have_no_duplicates_and_no_omissions(self, db):
        self._seed_many(30)
        seen, cursor, pages = [], None, 0
        while True:
            res = search(page_size=7, **({"cursor": cursor} if cursor else {}))
            seen.extend(j["job_id"] for j in res["jobs"])
            pages += 1
            cursor = res["next_cursor"]
            if not cursor or pages > 12:
                break
        assert len(seen) == len(set(seen)), "a job appeared on two pages"
        assert len(seen) == 30, f"expected all 30, walked {len(seen)}"

    def test_the_first_page_reports_the_full_total(self, db):
        self._seed_many(30)
        assert search(page_size=7)["total"] == 30

    def test_ordering_is_stable_across_pages(self, db):
        self._seed_many(30)
        first = search(page_size=7)
        again = search(page_size=7, cursor=first["next_cursor"])
        assert again["jobs"], "page 2 was empty"
        assert not (set(j["job_id"] for j in first["jobs"]) &
                    set(j["job_id"] for j in again["jobs"]))

    def test_paging_does_no_retrieval_and_no_re_ranking(self, db):
        self._seed_many(30)
        first = search(page_size=7)
        second = search(page_size=7, cursor=first["next_cursor"])
        assert second["diagnostics"]["served_from"] == "session"
        assert "retrieval_ms" not in second["diagnostics"]

    def test_a_page_echoes_the_watermark_the_search_recorded(self, db):
        self._seed_many(10)
        first = search(page_size=4)
        second = search(page_size=4, cursor=first["next_cursor"])
        assert second["inventory_watermark"] == first["inventory_watermark"]

    def test_an_unknown_cursor_is_a_clean_expiry_not_a_crash(self, db):
        res = search(cursor=tfs.encode_cursor("does-not-exist", 25))
        assert res["error"] == "cursor_expired"
        assert res["jobs"] == []

    def test_a_cursor_from_another_user_is_refused(self, db):
        self._seed_many(10)
        first = tfs.search({"tech_scope": ["core"], "page_size": 4,
                            "personalize": False, "user_email": "a@x.com"})
        res = tfs.search({"cursor": first["next_cursor"], "page_size": 4,
                          "personalize": False, "user_email": "b@x.com"})
        assert res["error"] == "cursor_expired"

    def test_has_more_is_false_on_the_last_page(self, db):
        self._seed_many(10)
        res = search(page_size=100)
        assert res["has_more"] is False
        assert res["next_cursor"] is None


# ---------------------------------------------------------------------------
# Facets, dedupe, health
# ---------------------------------------------------------------------------

class TestFacetsAndHealth:
    def test_facet_counts_describe_the_filtered_set(self, db):
        seed(sjid="f1", title="Site Reliability Engineer", company="Acme")
        seed(sjid="f2", title="Site Reliability Engineer", company="Beta",
             tenant="beta")
        seed(sjid="f3", title="iOS Engineer", company="Acme", tenant="acme2")
        res = search()
        fams = {f["value"]: f["count"] for f in res["facets"]["role_family"]}
        assert fams.get("cloud_platform") == 2
        assert fams.get("software_engineering") == 1

    def test_a_facet_count_matches_what_clicking_it_returns(self, db):
        seed(sjid="f1", title="Site Reliability Engineer", company="Acme")
        seed(sjid="f2", title="iOS Engineer", company="Acme", tenant="acme2")
        res = search()
        want = {f["value"]: f["count"] for f in res["facets"]["role_specialty"]}
        got = search(role_specialties=["sre"])
        assert len(got["jobs"]) == want.get("sre")

    def test_the_same_requisition_from_two_sources_is_one_card(self, db):
        url = "https://boards.greenhouse.io/acme/jobs/999"
        seed(sjid="d1", title="Backend Engineer", company="Acme", url=url)
        seed(sjid="d2", title="Backend Engineer", company="Acme Inc.",
             url=url + "?utm_source=linkedin", source="workday", ats="workday")
        res = search()
        assert len(res["jobs"]) == 1
        assert res["jobs"][0]["duplicate_count"] >= 1
        assert res["jobs"][0]["source_variants"]

    def test_source_health_is_reported_even_when_empty(self, db):
        seed(sjid="h1", title="Backend Engineer", company="Acme")
        res = search()
        assert "source_health" in res
        assert "ingest_runner" in res["source_health"]

    def test_a_dead_ingest_heartbeat_becomes_a_visible_warning(self, db):
        seed(sjid="h1", title="Backend Engineer", company="Acme")
        db.ingest_runner_heartbeat.insert_one({
            "runner_id": "test", "updated_at": NOW - timedelta(days=3)})
        res = search()
        assert any("heartbeat" in w for w in res["warnings"])

    def test_coverage_is_reported(self, db):
        seed(sjid="h1", title="Backend Engineer", company="Acme")
        cov = search()["coverage"]
        assert cov["active_postings"] >= 1
        assert "known_employers" in cov


class TestVerdictReachesTheCard:
    """fit-v2.1 fields the card renders.

    The frontend cannot honestly distinguish "not reachable" from "low score"
    unless the verdict travels with the row. These were computed by
    `fit_score.score()` and dropped on the floor by `_card`, so a card had a
    number and no way to say what it meant.
    """

    def test_eligibility_and_alignment_are_on_the_card(self, db, monkeypatch):
        seed(sjid="v1", title="Senior Backend Engineer")
        resume = {
            "contact": {"name": "A", "location": "Austin, TX"},
            "summary": "Backend engineer.",
            "skills": {"core": ["Python", "AWS", "Kubernetes", "PostgreSQL"]},
            "experience": [{"title": "Senior Backend Engineer", "company": "X",
                            "dates": "Jan 2019 - Jan 2025",
                            "bullets": ["Python services on AWS with Kubernetes "
                                        "and PostgreSQL."]}],
            "education": [{"degree": "BS Computer Science"}],
        }
        # `score_one_best` reads `entry["profile"]`, so a bare profile in the
        # list scores against an EMPTY resume and every verdict comes back
        # `not_scored`. The entry shape is the contract.
        monkeypatch.setattr(
            fit_score, "load_profiles_for_user",
            lambda email: [{"resume_id": "r1", "filename": "r.pdf",
                            "profile": fit_score.build_resume_profile(resume)}])
        got = search(personalize=True, user_email="a@example.com")
        card = got["jobs"][0]
        assert card["eligibility"] in ("eligible", "ineligible", "not_scored")
        assert isinstance(card["role_alignment"], dict)
        assert card["role_alignment"].get("tier")
        assert card["role_alignment"].get("detail")
        assert card["evidence_breadth"] is not None
        assert isinstance(card["hard_constraints"], list)
        assert isinstance(card["ineligible_reasons"], list)

    def test_an_unscored_card_carries_no_verdict_and_no_version(self, db):
        """CLAUDE.md invariant: no parsed resume => NO score, and no marker
        that implies one was computed."""
        seed(sjid="v2", title="Senior Backend Engineer")
        card = search(personalize=False)["jobs"][0]
        assert card["match_score"] == 0
        assert card["fit_score_version"] is None
        assert card["eligibility"] is None
        assert card["role_alignment"] == {}

    def test_a_hard_requirement_is_reported_only_when_unknowable(
            self, db, monkeypatch):
        """A clearance the résumé cannot speak to is a requirement to check.
        A requirement the résumé MEETS is not, and one it definitely fails is
        already in `missing_requirements`."""
        seed(sjid="v3", title="Software Engineer",
             description=("Software Engineer. An active TS/SCI security clearance "
                          "is required. Requirements: 5+ years of Python, AWS, "
                          "Kubernetes and PostgreSQL."))
        resume = {
            "contact": {"name": "A", "location": "Austin, TX"},
            "skills": {"core": ["Python", "AWS", "Kubernetes", "PostgreSQL"]},
            "experience": [{"title": "Software Engineer", "company": "X",
                            "dates": "Jan 2019 - Jan 2025",
                            "bullets": ["Python on AWS with Kubernetes."]}],
        }
        # `score_one_best` reads `entry["profile"]`, so a bare profile in the
        # list scores against an EMPTY resume and every verdict comes back
        # `not_scored`. The entry shape is the contract.
        monkeypatch.setattr(
            fit_score, "load_profiles_for_user",
            lambda email: [{"resume_id": "r1", "filename": "r.pdf",
                            "profile": fit_score.build_resume_profile(resume)}])
        card = search(personalize=True, user_email="a@example.com")["jobs"][0]
        # The FULL list travels, with `status` and `hard_requirement` intact:
        # a requirement to CHECK is `hard_requirement` + `status: unknown`, an
        # ineligibility reason is `status: missing`, and a `met` one is worth
        # showing as reassurance. Pre-filtering here would decide that for
        # every surface at once. The frontend reads the same field name.
        clearance = [c for c in card["hard_constraints"] if c["type"] == "clearance"]
        assert clearance, card["hard_constraints"]
        assert clearance[0]["status"] == "unknown"
        assert clearance[0]["hard_requirement"] is True
        assert card["eligibility"] == "eligible"

    def test_the_card_field_name_matches_what_the_frontend_reads(self, db):
        """`techFeedEligibility.hardConstraintsOf` reads `hard_constraints`,
        top level or inside `fit`. A backend that sends `hard_requirements`
        renders nothing, silently, in both places."""
        seed(sjid="v4", title="Backend Engineer")
        card = search(personalize=False)["jobs"][0]
        assert "hard_constraints" in card
        assert "hard_requirements" not in card


class TestIneligibleNeverOutranksReachable:
    """The ordering half of the eligibility gate.

    `fit_score` marks a posting `ineligible` and caps it into the bottom band,
    but a cap is not an ordering: a very fresh ineligible posting could still
    have beaten a stale reachable one on `rank_score`. Eligibility is now the
    FIRST sort key on every sort.
    """

    def _rows(self):
        return [
            {"job_id": "reachable", "rank_score": 0.20, "match_score": 55,
             "_days_ago": 25, "_salary_top": 100000,
             "fit": {"eligibility": "eligible"}},
            {"job_id": "unreachable", "rank_score": 0.95, "match_score": 30,
             "_days_ago": 0, "_salary_top": 400000,
             "fit": {"eligibility": "ineligible"}},
            {"job_id": "unscored", "rank_score": 0.50, "match_score": 0,
             "_days_ago": 5, "_salary_top": 150000, "fit": None},
        ]

    @pytest.mark.parametrize("sort", ["relevance", "newest", "salary"])
    def test_on_every_sort(self, sort):
        rows = self._rows()
        tfs.rank(rows, {**tfs.normalize_request({}), "sort": sort},
                 None, [], 0)
        assert rows[-1]["job_id"] == "unreachable", [r["job_id"] for r in rows]

    def test_an_unscored_posting_is_not_demoted(self):
        """`not_scored` is an absence of evidence, and demoting on absence is
        the mistake fit-v2.1 exists to correct."""
        rows = self._rows()
        tfs.rank(rows, {**tfs.normalize_request({}), "sort": "relevance"},
                 None, [], 0)
        order = [r["job_id"] for r in rows]
        assert order.index("unscored") < order.index("unreachable")

    def test_the_helper_reads_only_a_positive_finding(self):
        assert tfs._ineligible({"fit": {"eligibility": "ineligible"}}) == 1
        assert tfs._ineligible({"fit": {"eligibility": "eligible"}}) == 0
        assert tfs._ineligible({"fit": {"eligibility": "not_scored"}}) == 0
        assert tfs._ineligible({"fit": None}) == 0
        assert tfs._ineligible({}) == 0


class TestTheResponseSaysWhichFiltersDidNothing:
    """`applied_filters` echoes the REQUEST. Rendering that echo as "Server
    applied" tells a user their filter narrowed the results when it did not,
    which is worse than not offering the filter: they believe they have
    excluded something and stop looking."""

    def test_an_inert_filter_is_named(self, db):
        seed(sjid="u1", title="Backend Engineer")
        got = search(radius_km=50, timezone_overlap_hours=4)
        named = {u["filter"] for u in got["unapplied_filters"]}
        assert {"radius_km", "timezone_overlap_hours"} <= named
        assert all(u["reason"] for u in got["unapplied_filters"])

    def test_a_request_that_sets_none_of_them_reports_none(self, db):
        seed(sjid="u2", title="Backend Engineer")
        assert search()["unapplied_filters"] == []

    def test_a_partially_applied_filter_says_partial_not_inert(self, db):
        """A partial filter DID narrow. Reporting it as "not applied" would
        replace one lie with the opposite one."""
        seed(sjid="u3", title="Backend Engineer")
        got = search(sponsorship_preference="needs_sponsorship")
        entry = [u for u in got["unapplied_filters"]
                 if u["filter"] == "sponsorship_preference"]
        assert entry and "most do not" in entry[0]["reason"]
        assert entry[0]["status"] == tfs.FILTER_PARTIAL

    def test_an_inert_filter_is_marked_not_applied_not_partial(self, db):
        seed(sjid="u5", title="Backend Engineer")
        got = search(radius_km=50)
        entry = [u for u in got["unapplied_filters"] if u["filter"] == "radius_km"]
        assert entry and entry[0]["status"] == tfs.FILTER_NOT_APPLIED

    def test_every_entry_carries_a_status(self, db):
        seed(sjid="u6", title="Backend Engineer")
        got = search(radius_km=50, query="backend", employment_types=["contract"],
                     min_years=3, skills_required=["python"])
        assert got["unapplied_filters"]
        for u in got["unapplied_filters"]:
            assert u["status"] in (tfs.FILTER_PARTIAL, tfs.FILTER_NOT_APPLIED)

    def test_region_and_pay_interval_are_NOT_in_the_inert_list(self, db):
        """Both were inert and are now implemented; if either regresses, this
        is where it shows."""
        seed(sjid="u4", title="Backend Engineer")
        got = search(regions=["TX"], pay_interval="year")
        named = {u["filter"] for u in got["unapplied_filters"]}
        assert "regions" not in named
        assert "pay_interval" not in named


class TestRegionAndPayIntervalActuallyFilter:
    def test_a_region_filter_narrows(self, db):
        seed(sjid="r1", title="Backend Engineer", location="Austin, TX, USA")
        seed(sjid="r2", title="Backend Engineer", location="Seattle, WA, USA")
        got = titles(search(regions=["TX"]))
        assert len(got) == 1

    def test_a_region_filter_is_case_insensitive(self, db):
        seed(sjid="r3", title="Backend Engineer", location="Austin, TX, USA")
        assert len(titles(search(regions=["tx"]))) == 1

    def test_a_posting_with_no_compensation_survives_a_pay_interval_filter(self, db):
        """Not stating a pay interval is not the same as stating a different
        one, and 81.5% of the live inventory states no compensation at all."""
        seed(sjid="p1", title="Backend Engineer")
        assert len(titles(search(pay_interval="year"))) == 1


class TestTheFiltersThatWereSilentlyDroppedByTheQueryBuilder:
    """`build_query` ends with `q["$and"] = _and`, an ASSIGNMENT. Two clauses
    used `q.setdefault("$and", []).append(...)` instead of appending to `_and`,
    and since `_and` always holds the unconditional tech-scope clause, that
    line deleted them. Measured live: `countries: ["india"]` returned the same
    17,683 postings as no country filter at all."""

    def test_a_country_filter_survives_into_the_query(self, db):
        seed(sjid="c1", title="Backend Engineer", location="Bengaluru, KA, India")
        seed(sjid="c2", title="Backend Engineer", location="Austin, TX, USA")
        assert len(titles(search(countries=["india"]))) == 1

    def test_an_employment_type_filter_survives_into_the_query(self, db):
        seed(sjid="e1", title="Backend Engineer", employment_type="Contract")
        seed(sjid="e2", title="Backend Engineer", employment_type="Part time")
        assert len(titles(search(employment_types=["contract"]))) == 1

    def test_both_at_once_still_work(self, db):
        """The failure mode was one clause overwriting another, so the case
        that matters is more than one of them present."""
        seed(sjid="b1", title="Backend Engineer", location="Bengaluru, KA, India",
             employment_type="Contract")
        seed(sjid="b2", title="Backend Engineer", location="Bengaluru, KA, India",
             employment_type="Part time")
        seed(sjid="b3", title="Backend Engineer", location="Austin, TX, USA",
             employment_type="Contract")
        got = titles(search(countries=["india"], employment_types=["contract"]))
        assert len(got) == 1


class TestTheSearchBoxActuallySearches:
    """`query` was compiled and then read in exactly ONE place: as a fallback
    when `titles` was also set. With no title list — the normal case — typing
    "kubernetes" narrowed nothing. Live: 17,683 postings either way."""

    # A description distinct from `SWE_JD`, so a query hit can only have come
    # from the field under test rather than from shared boilerplate.
    PLAIN = ("We are hiring a software engineer. You will build and ship "
             "features and work with the team on our product.")

    def test_a_query_narrows_with_no_title_list(self, db):
        seed(sjid="q1", title="Kubernetes Platform Engineer", description=self.PLAIN)
        seed(sjid="q2", title="Frontend Engineer", description=self.PLAIN)
        assert titles(search(query="kubernetes")) == ["Kubernetes Platform Engineer"]

    def test_every_word_must_match_not_just_one(self, db):
        """Any-of turns "senior kubernetes" into every senior role in the
        index."""
        seed(sjid="q3", title="Senior Kubernetes Engineer", description=self.PLAIN)
        seed(sjid="q4", title="Senior Frontend Engineer", description=self.PLAIN)
        assert titles(search(query="senior kubernetes")) == ["Senior Kubernetes Engineer"]

    def test_the_employer_name_is_searchable(self, db):
        seed(sjid="q5", title="Backend Engineer", company="Stripe",
             description=self.PLAIN)
        seed(sjid="q6", title="Backend Engineer", company="Acme",
             description=self.PLAIN, tenant="acme2")
        assert len(titles(search(query="stripe"))) == 1

    def test_the_extracted_skills_are_searchable(self, db):
        """Title-only matching returned 38 of 17,688 live core-tech postings
        for "kubernetes", which is a shortfall, not a result."""
        seed(sjid="q9", title="Platform Engineer",
             description="Requirements: strong Kubernetes and Terraform experience.")
        seed(sjid="q10", title="Platform Engineer", description=self.PLAIN,
             tenant="acme2")
        assert len(titles(search(query="kubernetes"))) == 1

    def test_a_query_matching_nothing_returns_nothing_rather_than_everything(self, db):
        seed(sjid="q7", title="Backend Engineer", description=self.PLAIN)
        assert titles(search(query="zzzznotathing")) == []

    def test_a_one_or_two_character_token_is_ignored_not_matched(self, db):
        """Short tokens match everywhere and would silently widen the search."""
        seed(sjid="q8", title="Backend Engineer", description=self.PLAIN)
        assert len(titles(search(query="a backend"))) == 1


class TestTheExperienceRangeMatchesWhatThePostingStates:
    """`min_years` compared against `required_years_max`, which is populated
    only when a JD spells out a range and is set on 0 of 224,389 live
    postings."""

    JUNIOR = "Requirements: minimum 2 years of experience building web services."
    SENIOR = "Requirements: minimum 9 years of experience building web services."
    SILENT = "Requirements: you enjoy building web services with a small team."

    def test_min_years_drops_a_posting_asking_for_less(self, db):
        seed(sjid="y1", title="Backend Engineer", description=self.JUNIOR)
        seed(sjid="y2", title="Backend Engineer", description=self.SENIOR,
             tenant="acme2")
        assert len(titles(search(min_years=8))) == 1

    def test_max_years_drops_a_posting_asking_for_more(self, db):
        seed(sjid="y3", title="Backend Engineer", description=self.JUNIOR)
        seed(sjid="y4", title="Backend Engineer", description=self.SENIOR,
             tenant="acme2")
        assert len(titles(search(max_years=3))) == 1

    def test_a_posting_that_states_no_experience_survives_both(self, db):
        """76.4% of the live inventory states none, and not stating a number
        is not the same as failing one."""
        seed(sjid="y5", title="Backend Engineer", description=self.SILENT)
        assert len(titles(search(min_years=8))) == 1
        assert len(titles(search(max_years=1))) == 1


class TestAMarketplaceRepostIsNotTheEmployersOwnPosting:
    """A marketplace board lists MANY employers' roles under one board, so its
    owner is not the job's employer. `board_kind` has been in the catalogs
    since Jobgether was added and in `_PROJECTION` since the feed was written,
    but nothing on the write path ever stored it: it was None on all 225,095
    active postings, and `apply_mode: direct_apply` matched every one of them.
    """

    def test_the_ingest_path_now_stores_a_board_kind(self, db):
        seed(sjid="bk1", title="Backend Engineer")
        assert inv.get_posting("career-pages", "bk1")["board_kind"] == "employer"

    def test_a_marketplace_tenant_is_recorded_as_one(self, db):
        seed(sjid="bk2", title="Backend Engineer", company="Jobgether",
             tenant="jobgether")
        assert inv.get_posting("career-pages", "bk2")["board_kind"] == "marketplace"

    def test_direct_apply_excludes_a_marketplace_repost(self, db):
        seed(sjid="bk3", title="Backend Engineer")
        seed(sjid="bk4", title="Backend Engineer", company="Jobgether",
             tenant="jobgether")
        got = search(apply_mode="direct_apply")
        assert [j["employer_name"] for j in got["jobs"]] == ["Acme Inc"]

    def test_a_stored_row_with_no_board_kind_still_reads_as_a_marketplace(self, db):
        """225,095 rows predate the write-path fix, so the catalog lookup is
        the fallback - the same one `inventory_feed` already uses, so the two
        surfaces cannot disagree about which boards are aggregators."""
        assert tfs._is_direct_apply(
            {"tenant": "jobgether", "employer_name": "Jobgether",
             "canonical_url": "https://x/y", "cost_class": "free"}) is False
        assert tfs._is_direct_apply(
            {"tenant": "acme", "employer_name": "Acme Inc",
             "canonical_url": "https://x/y", "cost_class": "free"}) is True


class TestOneEmployerDoesNotOwnThePage:
    """Measured on the live feed for one account: 15 of the first 25 cards
    were JPMorgan Chase. They were not duplicates — Android, iOS, Java/AWS,
    React/Java, all different requisitions — and each deserved its score. A
    page that shows one employer fifteen times has shown the user one
    employer.

    This is ORDERING. Nothing is dropped and no score changes; an employer's
    fourth-best role lands on page two.
    """

    def _rows(self, spec):
        return [{"employer_id": e, "employer_name": e, "job_id": f"{e}{i}"}
                for e, n in spec for i in range(n)]

    def test_no_employer_exceeds_the_cap_while_others_are_available(self):
        rows = self._rows([("big", 40), ("a", 3), ("b", 3), ("c", 3), ("d", 3)])
        out, moved = tfs._diversify_employers(rows, 10)
        first = [r["employer_id"] for r in out[:10]]
        assert first.count("big") <= tfs.EMPLOYER_PAGE_CAP
        assert moved > 0

    def test_the_cap_is_best_effort_and_never_shortens_a_page(self):
        """When the remaining results genuinely ARE one employer there is no
        diversity to be had, and a four-row page would show the user less
        while shifting every later boundary."""
        rows = self._rows([("big", 40), ("a", 1)])
        out, _ = tfs._diversify_employers(rows, 10)
        assert len(out[:10]) == 10
        assert "a" in [r["employer_id"] for r in out[:10]]

    def test_nothing_is_dropped(self):
        rows = self._rows([("big", 40), ("a", 3), ("b", 2)])
        out, _ = tfs._diversify_employers(rows, 10)
        assert sorted(r["job_id"] for r in out) == sorted(r["job_id"] for r in rows)

    def test_relative_order_within_an_employer_is_preserved(self):
        rows = self._rows([("big", 12), ("a", 20)])
        out, _ = tfs._diversify_employers(rows, 5)
        big = [r["job_id"] for r in out if r["employer_id"] == "big"]
        assert big == [f"big{i}" for i in range(12)]

    def test_a_result_set_smaller_than_a_page_is_untouched(self):
        rows = self._rows([("big", 5)])
        out, moved = tfs._diversify_employers(rows, 25)
        assert out == rows and moved == 0

    def test_a_feed_that_is_ALL_one_employer_still_returns_everything(self):
        """The cap must never turn into a filter."""
        rows = self._rows([("only", 30)])
        out, _ = tfs._diversify_employers(rows, 10)
        assert len(out) == 30

    def test_the_top_ranked_row_is_still_first(self):
        """Diversity reorders the tail, not the winner."""
        rows = self._rows([("big", 40), ("a", 5)])
        out, _ = tfs._diversify_employers(rows, 10)
        assert out[0]["job_id"] == "big0"

    def test_the_feed_applies_it(self, db):
        for i in range(8):
            seed(sjid=f"dv{i}", title=f"Backend Engineer {i}", company="BigCo",
                 tenant="bigco")
        seed(sjid="dv9", title="Backend Engineer X", company="SmallCo",
             tenant="smallco")
        got = search(page_size=5)
        names = [j["employer_name"] for j in got["jobs"]]
        # SmallCo ranked ninth by relevance and would never have been seen on a
        # five-row page. Promoting it is the entire point.
        assert "SmallCo" in names
        assert names.index("SmallCo") <= tfs.EMPLOYER_PAGE_CAP
        assert len(names) == 5, "the page must still be full"

    def test_the_key_is_the_ats_tenant_not_the_posting_slug(self):
        """One employer's board prints many legal-entity names. JPMorgan's
        Oracle instance serves three CX sites under tenant `jpmc` and stamps
        its postings `jpmorgan-chase`, `nutmeg-container-corporation` and
        `chase-family-limited-partnership-no-5`. Keyed on the slug those read
        as three companies, and they took nine of the first twenty-five
        cards."""
        rows = [{"ats": "oracle", "tenant": "jpmc", "employer_id": slug,
                 "job_id": f"j{i}"}
                for i, slug in enumerate(
                    ["jpmorgan-chase", "nutmeg-container-corporation",
                     "chase-family-limited-partnership-no-5",
                     "jpmorgan-chase", "jpmorgan-chase", "jpmorgan-chase"])]
        rows += [{"ats": "greenhouse", "tenant": f"co{i}", "employer_id": f"co{i}",
                  "job_id": f"c{i}"} for i in range(6)]
        out, _ = tfs._diversify_employers(rows, 6)
        page = [r for r in out[:6] if r.get("tenant") == "jpmc"]
        assert len(page) <= tfs.EMPLOYER_PAGE_CAP

    def test_a_row_with_no_tenant_falls_back_to_the_employer_name(self):
        rows = [{"employer_name": "Acme", "job_id": f"a{i}"} for i in range(6)]
        rows += [{"employer_name": "Other", "job_id": f"o{i}"} for i in range(6)]
        out, _ = tfs._diversify_employers(rows, 6)
        assert sum(1 for r in out[:6] if r["employer_name"] == "Acme") \
            <= tfs.EMPLOYER_PAGE_CAP
