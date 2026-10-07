"""Early-career postings stay 30 days; everything else stays 7.

Product decision 2026-10-02. Cross-matching one early-career candidate against
100 entry-level roles pulled straight from employers' own boards, 93 were
missing from the inventory - every one of them still open, but published more
than 7 days earlier, and the crawler stores only the last 7 days. New-grad and
entry-level roles stay open for weeks; senior roles fill and churn. So intern
and entry postings now live 30 days; the 7-day window is unchanged for the
rest, and the guest flow keeps its strict 7-day promise.
"""
from datetime import datetime, timedelta, timezone

import pytest

from services import job_inventory_service as inv
from services import job_ingest_worker as worker

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


def naive(dt):
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


@pytest.fixture()
def db():
    d = inv._db()
    d.job_postings.delete_many({})
    return d


def _raw(sjid, title, age_days, body=""):
    return {"source": "workday", "source_job_id": sjid, "company": "Acme",
            "url": f"https://acme.wd1.myworkdayjobs.com/x/job/{sjid}",
            "title": title, "location": ["Austin, TX, USA"], "description": body,
            "employment_type": "Full time", "posted_at": NOW - timedelta(days=age_days),
            "cost_class": "free", "source_quality": "direct"}


class TestTheWindowFollowsTheLevel:
    def test_max_age_by_level(self):
        assert inv.max_age_days_for("entry") == 30
        assert inv.max_age_days_for("intern") == 30
        for level in ("mid", "senior", "unknown", None):
            assert inv.max_age_days_for(level) == 7

    def test_expiry_on_insert(self, db):
        inv.upsert_posting(_raw("E1", "Software Engineer, New Grad", 2), now=NOW)
        inv.upsert_posting(_raw("S1", "Senior Software Engineer", 2), now=NOW)
        e = db.job_postings.find_one({"source_job_id": "E1"})
        s = db.job_postings.find_one({"source_job_id": "S1"})
        assert e["seniority"] == "entry"
        assert e["expire_at"] == naive(NOW - timedelta(days=2) + timedelta(days=30))
        assert s["expire_at"] == naive(NOW - timedelta(days=2) + timedelta(days=7))

    def test_the_aged_sweep_keeps_entry_to_30_days(self, db):
        inv.upsert_posting(_raw("E10", "Software Engineer I", 10), now=NOW - timedelta(days=9))
        inv.upsert_posting(_raw("S10", "Senior Software Engineer", 10), now=NOW - timedelta(days=9))
        inv.upsert_posting(_raw("E31", "Software Engineer I", 31), now=NOW - timedelta(days=30))
        inv.expire_aged_postings(now=NOW)
        status = {d["source_job_id"]: d["status"] for d in db.job_postings.find({})}
        assert status == {"E10": "active", "S10": "closed", "E31": "closed"}

    def test_an_explicit_window_still_overrides(self, db):
        inv.upsert_posting(_raw("E10", "Software Engineer I", 10), now=NOW - timedelta(days=9))
        inv.expire_aged_postings(max_age_days=7, now=NOW)
        assert db.job_postings.find_one({"source_job_id": "E10"})["status"] == "closed"

    def test_a_level_change_moves_the_expiry(self, db):
        from services import fit_score
        inv.upsert_posting(_raw("M1", "Software Engineer", 3,
                                body="We are hiring."), now=NOW)
        doc = db.job_postings.find_one({"source_job_id": "M1"})
        assert doc["expire_at"] == naive(NOW - timedelta(days=3) + timedelta(days=7))
        req = fit_score.extract_requirements("This role is for new grads.", "Software Engineer")
        assert inv.mark_requirements_parsed("workday", "M1", doc["content_hash"], req, now=NOW)
        after = db.job_postings.find_one({"source_job_id": "M1"})
        assert after["seniority"] == "entry"
        assert after["expire_at"] == naive(NOW - timedelta(days=3) + timedelta(days=30))

    def test_a_pinned_posting_is_never_given_an_expiry(self, db):
        from services import fit_score
        inv.upsert_posting(_raw("P1", "Software Engineer", 3, body="Hiring."), now=NOW)
        db.job_postings.update_one({"source_job_id": "P1"},
                                   {"$set": {"pinned": True}, "$unset": {"expire_at": ""}})
        doc = db.job_postings.find_one({"source_job_id": "P1"})
        req = fit_score.extract_requirements("For new grads.", "Software Engineer")
        inv.mark_requirements_parsed("workday", "P1", doc["content_hash"], req, now=NOW)
        assert "expire_at" not in db.job_postings.find_one({"source_job_id": "P1"})


class TestTheCrawlAdmitsAgedEarlyCareerOnly:
    FRESH = NOW - timedelta(days=7)

    def _row(self, sjid, title, age):
        return ({"member_key": "m"}, {"source_job_id": sjid, "title": title,
                                      "posted_at": NOW - timedelta(days=age)})

    def test_aged_rows_need_an_early_career_title_or_stored_level(self):
        rows = [self._row("fresh", "Senior Engineer", 2),
                self._row("aged-senior", "Senior Engineer", 12),
                self._row("aged-newgrad", "Software Engineer, New Grad", 12),
                self._row("aged-unmarked-known-entry", "Software Engineer", 12),
                self._row("aged-unmarked", "Software Engineer", 12),
                ({"member_key": "m"}, {"source_job_id": "undated", "title": "X", "posted_at": None})]
        existing = {"aged-unmarked-known-entry": {"seniority": "entry"},
                    "aged-unmarked": {"seniority": "mid"}}
        kept, dropped = worker._admit_by_age(rows, existing, fresh_cutoff=self.FRESH)
        assert {r["source_job_id"] for _m, r in kept} == {
            "fresh", "aged-newgrad", "aged-unmarked-known-entry", "undated"}
        assert dropped == 2

    def test_the_stale_close_guard_follows_the_level(self):
        early_cut = NOW - timedelta(days=30)
        aged = NOW - timedelta(days=12)
        assert worker._inside_listing_window(aged, "entry", self.FRESH, early_cut) is True
        assert worker._inside_listing_window(aged, "senior", self.FRESH, early_cut) is False
        assert worker._inside_listing_window(NOW, "senior", self.FRESH, early_cut) is True


class TestDeepListingOncePerDay:
    """Listing every shard back 30 days on every tick (f7f0175) halved Workday
    throughput: per hour, postings written fell 64,970 -> 25,016 and rows
    skipped for budget rose 2,252 -> 58,727, because paged sources read ~4x
    the pages to find a few older early-career roles. Now each shard lists
    back 30 days ONCE A DAY (a deep run) and 7 days otherwise; its first deep
    run lands at a per-shard hour, so the fleet does not go deep at once."""

    def test_a_shard_deep_lists_once_a_day(self):
        now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
        assert inv.deep_listing_due({"deep_listed_at": now - timedelta(hours=25)},
                                    now, shard="s1") is True
        assert inv.deep_listing_due({"deep_listed_at": now - timedelta(hours=2)},
                                    now, shard="s1") is False

    def test_first_deep_runs_are_spread_across_the_day(self):
        hours = {inv.deep_listing_first_hour("workday", f"s{i:03d}") for i in range(48)}
        assert len(hours) >= 12
        h = inv.deep_listing_first_hour("workday", "s007")
        at = datetime(2026, 10, 3, h, 5, tzinfo=timezone.utc)
        assert inv.deep_listing_due({}, at, source="workday", shard="s007") is True
        other = at.replace(hour=(h + 1) % 24)
        assert inv.deep_listing_due({}, other, source="workday", shard="s007") is False

    def test_mark_deep_listed(self):
        now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
        inv.ensure_source_state("workday", "s009", now=now)
        inv.mark_deep_listed("workday", "s009", now=now)
        doc = inv._source_state().find_one({"source": "workday", "shard": "s009"})
        assert doc["deep_listed_at"] == naive(now)


class TestEmployerLaddersReachTheStoredLevel:
    def test_insert_and_reparse_use_the_employer_ladder(self, db):
        from services import fit_score
        raw = _raw("NF1", "Software Engineer (L5) - JavaScript Platform", 1)
        raw["company"] = "Netflix"
        inv.upsert_posting(raw, now=NOW)
        doc = db.job_postings.find_one({"source_job_id": "NF1"})
        assert doc["seniority"] == "senior"
        assert doc["tech"]["seniority"] == "senior"
        req = fit_score.extract_requirements("Build our JavaScript platform.", doc["title_raw"])
        assert inv.mark_requirements_parsed("workday", "NF1", doc["content_hash"], req, now=NOW)
        after = db.job_postings.find_one({"source_job_id": "NF1"})
        assert after["seniority"] == "senior"
        assert after["requirements"]["job_level"] == "senior"


class TestAgedRowsAreJudgedByTheirYearsToo:
    """In a ground truth of 100 real entry-level roles, 70 were entry-level
    only by their STATED YEARS ("Forward Deployed Software Engineer", 1 year).
    Lever and Ashby listings carry the body, and a deep run asks Greenhouse
    for it, so an aged row can be judged on its years with no extra request."""

    FRESH = NOW - timedelta(days=7)

    def test_an_aged_row_with_entry_years_in_its_body_is_admitted(self):
        rows = [({"member_key": "m"}, {"source_job_id": "a", "title": "Software Engineer",
                                        "posted_at": NOW - timedelta(days=12),
                                        "description": "Qualifications: 0-2 years of experience."}),
                ({"member_key": "m"}, {"source_job_id": "b", "title": "Software Engineer",
                                        "posted_at": NOW - timedelta(days=12),
                                        "description": "Qualifications: 6+ years of experience."})]
        kept, dropped = worker._admit_by_age(rows, {}, fresh_cutoff=self.FRESH)
        assert [r["source_job_id"] for _m, r in kept] == ["a"]
        assert dropped == 1


class TestEarlyCareerRolesOlderThanTheCandidateWindow:
    """Retrieval is newest-first and capped. Early-career postings live 30 days
    and the rest 7, so most of what an early-career user came for is OLDER than
    the newest N candidates and never reached scoring. Cross-checked against
    employers' own boards for every real user (2026-10-06): 51 of 128
    in-inventory roles missing from their boards were lost exactly here."""

    def _seed(self, db):
        # 6 fresh senior roles fill the window; the entry and intern roles are older.
        for i in range(6):
            inv.upsert_posting(_raw(f"S{i}", "Senior Software Engineer", 1), now=NOW)
        inv.upsert_posting(_raw("E18", "Software Engineer, New Grad", 18), now=NOW - timedelta(days=17))
        inv.upsert_posting(_raw("I22", "Software Engineer Intern", 22), now=NOW - timedelta(days=21))
        inv.upsert_posting(_raw("M20", "Software Engineer II", 20), now=NOW - timedelta(days=19))
        db.job_postings.update_many({}, {"$set": {"status": "active"}})

    def _window(self, monkeypatch, db, levels):
        from services import experience_view, inventory_feed
        monkeypatch.setattr(inventory_feed, "CANDIDATE_LIMIT", 6)
        monkeypatch.setattr(experience_view, "for_user", lambda email: {"levels": levels})
        docs = inv.find_active_fresh(source="workday", max_age_days=30, limit=6, now=NOW)
        assert {d["source_job_id"] for d in docs} == {f"S{i}" for i in range(6)}
        return inventory_feed, docs

    def test_an_early_career_user_gets_the_older_roles_at_their_level(self, db, monkeypatch):
        self._seed(db)
        feed, docs = self._window(monkeypatch, db, ["intern", "entry"])
        extra = feed._own_level_slice(docs, "a@b.co", source="workday")
        assert {d["source_job_id"] for d in extra} == {"E18", "I22"}

    def test_only_the_levels_in_their_view(self, db, monkeypatch):
        self._seed(db)
        feed, docs = self._window(monkeypatch, db, ["entry", "mid"])
        extra = feed._own_level_slice(docs, "a@b.co", source="workday")
        assert {d["source_job_id"] for d in extra} == {"E18"}     # mid is not early-career

    def test_other_users_and_unfilled_windows_are_untouched(self, db, monkeypatch):
        self._seed(db)
        feed, docs = self._window(monkeypatch, db, ["senior"])
        assert feed._own_level_slice(docs, "a@b.co", source="workday") == []
        feed, docs = self._window(monkeypatch, db, ["entry"])
        assert feed._own_level_slice(docs[:3], "a@b.co", source="workday") == []   # window not full
        assert feed._own_level_slice(docs, "", source="workday") == []             # guest


class TestTheRowCapDoesNotCostAUserTheirOwnLevel:
    """The board keeps the top GLOBAL_KEEP rows by score across EVERY level and
    then shows the user their own. Measured for every real user (2026-10-07):
    40 to 900 rows in a user's own view sat past the cap. The total must not
    grow: responses are 5.3-5.6 MB at the cap and Lambda rejects 6 MB."""

    def _rows(self):
        # rank order: 4 senior, 1 unknown, then past a cap of 5: entry, intern, senior, principal
        return ([{"job_id": f"s{i}", "seniority": "senior"} for i in range(4)]
                + [{"job_id": "u1", "seniority": ""},
                   {"job_id": "e1", "seniority": "entry"}, {"job_id": "i1", "seniority": "intern"},
                   {"job_id": "s9", "seniority": "senior"}, {"job_id": "p1", "seniority": "principal"}])

    def _cap(self, monkeypatch, levels, email="a@b.co", keep=600):
        from services import experience_view, inventory_feed
        monkeypatch.setattr(experience_view, "for_user", lambda e: {"levels": levels})
        monkeypatch.setattr(inventory_feed, "GLOBAL_KEEP", 5)
        monkeypatch.setattr(inventory_feed, "OWN_LEVEL_KEEP", keep)
        return [r["job_id"] for r in inventory_feed._cap_keeping_own_level(self._rows(), email)]

    def test_own_level_rows_displace_the_lowest_ranked_filtered_rows(self, monkeypatch):
        got = self._cap(monkeypatch, ["intern", "entry"])
        assert got == ["s0", "s1", "u1", "e1", "i1"]      # s3 and s2 gave way; unknown stayed
        assert len(got) == 5                              # the total never grows

    def test_a_user_whose_level_already_fills_the_cap_is_untouched(self, monkeypatch):
        assert self._cap(monkeypatch, ["senior"]) == ["s0", "s1", "s2", "s3", "u1"]

    def test_staff_view_covers_the_bands_above_it(self, monkeypatch):
        assert self._cap(monkeypatch, ["staff"]) == ["s0", "s1", "s2", "u1", "p1"]

    def test_a_row_with_no_stated_level_is_rescued_too(self, monkeypatch):
        """It is in every view, so the cap costs the user that row just the
        same. Own-level rows still take the first places."""
        from services import experience_view, inventory_feed
        rows = ([{"job_id": f"s{i}", "seniority": "senior"} for i in range(5)]
                + [{"job_id": "u9", "seniority": ""}, {"job_id": "e1", "seniority": "entry"},
                   {"job_id": "u8", "seniority": "unknown"}])
        monkeypatch.setattr(experience_view, "for_user", lambda e: {"levels": ["entry"]})
        monkeypatch.setattr(inventory_feed, "GLOBAL_KEEP", 5)
        got = [r["job_id"] for r in inventory_feed._cap_keeping_own_level(rows, "a@b.co")]
        assert got == ["s0", "s1", "e1", "u9", "u8"] and len(got) == 5
        monkeypatch.setattr(inventory_feed, "OWN_LEVEL_KEEP", 1)
        got = [r["job_id"] for r in inventory_feed._cap_keeping_own_level(rows, "a@b.co")]
        assert got == ["s0", "s1", "s2", "s3", "e1"]

    def test_bounded_and_guests_get_the_plain_cap(self, monkeypatch):
        assert self._cap(monkeypatch, ["intern", "entry"], keep=1) == ["s0", "s1", "s2", "u1", "e1"]
        assert self._cap(monkeypatch, ["intern", "entry"], email="") == ["s0", "s1", "s2", "s3", "u1"]


class TestEqualScoresAreOrderedByRoleAlignment:
    """Capped postings tie at one score by the hundred; recency alone decided."""

    def test_alignment_then_experience(self):
        from services import inventory_feed
        row = lambda ra, ex: {"fit": {"components": {"role_alignment": ra, "experience": ex}}}
        assert inventory_feed._tie_break(row(25, 0)) > inventory_feed._tie_break(row(10, 20))
        assert inventory_feed._tie_break(row(10, 20)) > inventory_feed._tie_break(row(10, 5))

    def test_a_row_without_a_verdict_sorts_last_not_crashes(self):
        from services import inventory_feed
        assert inventory_feed._tie_break({}) == 0.0
        assert inventory_feed._tie_break({"fit": {"components": {"role_alignment": "x"}}}) == 0.0
