"""Paging to the end shows each job once - the same cards an all-at-once pass shows.

Measured on a frozen copy of live inventory (2026-10-06): a full walk of the
all-levels feed returned 23,753 cards where the all-at-once pipeline keeps
22,651. Every one of the 1,152 extras was a copy of a job already served -
255 the same opening reached through another source, 897 the same role in
another city - because a deep page collapsed duplicates only against ITSELF.
The accuracy harness read the mirror image of this as "silent drops".

These tests seed exactly those shapes across the ranked-window boundary and
walk the REAL `search()` to exhaustion.
"""
import random
from datetime import timedelta

import pytest

from services import inventory_feed
from services import job_inventory_service as inv
from services import tech_feed_service as tfs

COLLECTIONS = ("job_postings", "job_source_state", "job_ingestion_runs",
               "tech_feed_searches", "ingest_runner_heartbeat", "companies",
               "company_ats_endpoints")
JD = ("Backend Engineer. Build services with Python, AWS and PostgreSQL. "
      "3+ years of experience.")
CITIES = ["Austin, TX, USA", "Seattle, WA, USA", "New York, NY, USA",
          "Denver, CO, USA", "Chicago, IL, USA"]
PAYLOAD = {"posted_within_days": 30, "employer_scope": "all"}


@pytest.fixture()
def db():
    from _vg_support import current_db
    d = current_db()
    for c in COLLECTIONS:
        d[c].delete_many({})
    yield d
    for c in COLLECTIONS:
        d[c].delete_many({})


def _seed(d, rng, now, *, jobs):
    """`jobs` roles; each open in 1-4 cities, some cities twice, and some
    openings also listed by a second source."""
    n = 0
    for j in range(jobs):
        company, title = f"Acme {j}", rng.choice(["Backend Engineer", "Platform Engineer"])
        dated = rng.random() < 0.75
        for city in [rng.choice(CITIES) for _ in range(rng.randint(1, 4))]:
            n += 1
            sjid = f"p{n:04d}"
            posted = (now - timedelta(days=rng.randint(1, 9), minutes=rng.randrange(900))
                      if dated and rng.random() < 0.9 else None)
            raw = {"source": "career-pages", "source_job_id": sjid,
                   "url": f"https://boards.greenhouse.io/acme{j}/jobs/{n}",
                   "company": company, "title": title, "location": [city],
                   "description": JD, "employment_type": "Full time",
                   "cost_class": "free", "source_quality": "direct",
                   "industry": "tech", "ats": "greenhouse", "tenant": f"acme{j}",
                   "posted_at": posted}
            inv.upsert_posting(raw, now=now - timedelta(hours=rng.randrange(1, 200)))
            if rng.random() < 0.25:
                # the SAME opening, reached through a second source
                n += 1
                twin = dict(raw, source="workday", source_job_id=f"p{n:04d}",
                            url=f"https://acme{j}.wd1.myworkdayjobs.com/x/job/{n}",
                            posted_at=(posted - timedelta(days=rng.randint(0, 3))
                                       if posted else None))
                inv.upsert_posting(twin, now=now - timedelta(hours=rng.randrange(1, 200)))
                gid = d.job_postings.find_one({"source_job_id": sjid})["dedupe_group_id"]
                d.job_postings.update_one({"source_job_id": f"p{n:04d}"},
                                          {"$set": {"dedupe_group_id": gid}})
    return n


def _walk(page_size):
    jobs, cursor, pages = [], None, 0
    while True:
        res = tfs.search(dict(PAYLOAD, page_size=page_size,
                              **({"cursor": cursor} if cursor else {})))
        jobs += res.get("jobs") or []
        cursor = res.get("next_cursor")
        pages += 1
        if not cursor or pages > 400:
            break
    assert pages <= 400, "pagination did not terminate"
    return jobs


def _docs(d, job_ids):
    return list(d.job_postings.find({"job_id": {"$in": list(job_ids)}}, tfs._PROJECTION))


@pytest.mark.parametrize("seed,jobs,limit,page_size", [
    (1, 25, 10, 5), (2, 25, 10, 25), (3, 40, 7, 4), (4, 40, 30, 10),
    (5, 60, 12, 8), (6, 12, 5, 3), (7, 60, 50, 25), (8, 30, 3, 6),
])
def test_a_deep_walk_shows_each_job_once(db, monkeypatch, seed, jobs, limit, page_size):
    rng = random.Random(seed)
    now = tfs._now()
    seeded = _seed(db, rng, now, jobs=jobs)

    # The reference: everything inside ONE ranked window, collapsed all at once.
    monkeypatch.setattr(tfs, "CANDIDATE_LIMIT", 10_000)
    reference = [str(j["job_id"]) for j in _walk(100)]
    assert 0 < len(reference) <= seeded

    monkeypatch.setattr(tfs, "CANDIDATE_LIMIT", limit)
    walked = [str(j["job_id"]) for j in _walk(page_size)]

    assert len(walked) == len(set(walked)), "the same posting was served twice"
    openings = [inventory_feed.dedupe_key_of(d) for d in _docs(db, walked)]
    assert len(openings) == len(set(openings)), "the same opening was served through two copies"
    # The same ROLES and the same number of cards as the all-at-once pass. Which
    # city's copy represents a multi-city role can differ - undated rows are
    # ordered by job_id inside the window and by first_seen_at past it - but a
    # role never gains or loses a card: a group of n postings over c known
    # cities is always 1 + (n - c) cards, whatever order it is read in.
    roles = lambda ids: sorted(tfs._variant_key_of(d) for d in _docs(db, ids))  # noqa: E731
    assert set(roles(walked)) == set(roles(reference))
    assert roles(walked) == roles(reference), (len(walked), len(reference))


def test_every_matching_posting_is_reachable_as_a_card_or_on_one(db, monkeypatch):
    """Job-level recall: each match is a card, or is listed on a card as a
    source or location variant. Nothing matches and is simply absent."""
    rng = random.Random(11)
    now = tfs._now()
    _seed(db, rng, now, jobs=45)
    monkeypatch.setattr(tfs, "CANDIDATE_LIMIT", 9)
    req = tfs.normalize_request(dict(PAYLOAD))
    truth = _docs(db, [d["job_id"] for d in db.job_postings.find(
        tfs.build_query(req, now=now), {"job_id": 1})])
    walked = _docs(db, [j["job_id"] for j in _walk(7)])
    shown_openings = {inventory_feed.dedupe_key_of(d) for d in walked}
    shown_roles = {tfs._variant_key_of(d) for d in walked}
    missing = [d["title_raw"] for d in truth
               if inventory_feed.dedupe_key_of(d) not in shown_openings
               and tfs._variant_key_of(d) not in shown_roles]
    assert missing == []


def test_a_replayed_deep_page_is_identical(db, monkeypatch):
    """The rule keeps no state, so asking for the same page twice - a retry, a
    back button - returns the same cards."""
    rng = random.Random(21)
    _seed(db, rng, tfs._now(), jobs=40)
    monkeypatch.setattr(tfs, "CANDIDATE_LIMIT", 8)
    first = tfs.search(dict(PAYLOAD, page_size=8))
    cursor, pages = first.get("next_cursor"), []
    while cursor and len(pages) < 4:
        a = tfs.search(dict(PAYLOAD, page_size=8, cursor=cursor))
        b = tfs.search(dict(PAYLOAD, page_size=8, cursor=cursor))
        assert [j["job_id"] for j in a["jobs"]] == [j["job_id"] for j in b["jobs"]]
        pages.append(a)
        cursor = a.get("next_cursor")
    assert any((p.get("diagnostics") or {}).get("served_from") == "keyset" for p in pages)


def test_a_failed_sibling_lookup_falls_back_and_says_so(db, monkeypatch):
    """The lookup is bounded (rows and milliseconds). When it cannot answer,
    the page collapses within itself as it always did, says so, and the walk
    still reaches every job."""
    rng = random.Random(31)
    now = tfs._now()
    _seed(db, rng, now, jobs=30)
    monkeypatch.setattr(tfs, "CANDIDATE_LIMIT", 6)
    monkeypatch.setattr(tfs, "_one_card_per_job_across_pages", lambda *a, **k: None)
    res = tfs.search(dict(PAYLOAD, page_size=6))
    walked, flags = [j["job_id"] for j in res["jobs"]], []
    while res.get("next_cursor"):
        res = tfs.search(dict(PAYLOAD, page_size=6, cursor=res["next_cursor"]))
        walked += [j["job_id"] for j in res["jobs"]]
        if (res.get("diagnostics") or {}).get("served_from") == "keyset":
            flags.append(res["diagnostics"]["collapsed_across_pages"])
    assert flags and not any(flags)
    assert len(walked) == len(set(walked))
    req = tfs.normalize_request(dict(PAYLOAD))
    truth = _docs(db, [d["job_id"] for d in db.job_postings.find(
        tfs.build_query(req, now=now), {"job_id": 1})])
    shown = _docs(db, walked)
    roles = {tfs._variant_key_of(d) for d in shown}
    assert [d["title_raw"] for d in truth if tfs._variant_key_of(d) not in roles] == []


def test_the_lookup_is_bounded(db, monkeypatch):
    """More siblings than the limit means the group is incomplete, and an
    incomplete group cannot say who represents it: decline, do not guess."""
    from collections import Counter
    rng = random.Random(41)
    _seed(db, rng, tfs._now(), jobs=30)
    req = tfs.normalize_request(dict(PAYLOAD))
    base = tfs.build_query(req, now=tfs._now())
    docs = list(db.job_postings.find(base, tfs._PROJECTION))
    sizes = Counter(d["variant_group_id"] for d in docs)
    shared = next(d for d in docs if sizes[d["variant_group_id"]] > 1)

    def decide(row):
        return tfs._one_card_per_job_across_pages(
            [row], pool=[row], base=base, stored_req=req, suppressed=None,
            projection=tfs._PROJECTION, window_end={"p": None, "j": ""},
            wa_ctx=tfs._work_auth_context(req))

    assert decide(shared) is not None            # within the limit: it answers
    monkeypatch.setattr(tfs, "KEYSET_SIBLING_LIMIT", 0)
    assert decide(shared) is None                # a sibling exists and the limit is 0
