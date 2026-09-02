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
         url=None, parse=True):
    raw = {
        "source": source, "source_job_id": sjid,
        "url": url or f"https://boards.greenhouse.io/{tenant}/jobs/{sjid}",
        "company": company, "title": title, "location": [location],
        "description": description, "employment_type": "Full time",
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
        assert pred["$or"][0] == {"posted_at": {"$lt": posted}}
        assert pred["$or"][1] == {"posted_at": posted, "job_id": {"$gt": "j5"}}

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
        assert isinstance(card["hard_requirements"], list)
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
        kinds = {r["type"] for r in card["hard_requirements"]}
        assert "clearance" in kinds
        assert all(r["status"] == "unknown" for r in card["hard_requirements"])
        assert card["eligibility"] == "eligible"
