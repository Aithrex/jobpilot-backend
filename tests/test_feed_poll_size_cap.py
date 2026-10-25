"""A feed result always fits the poll response, and keeps its best cards.

2026-10-22: the per-card policy blocks took a 1,500-card Workday result from
5.66 MB to 6.63 MB. Lambda refuses a response over 6 MB, so the poll failed,
the client silently kept only the small MNC list, and every 80+ inventory
match disappeared from For-you.
"""
import json

from services.resume_service import fit_feed_result


def _card(i, score, days=1, pad=900):
    return {"job_id": f"j{i}", "title": f"Engineer {i}", "match_score": score,
            "days_ago": days, "employer_profile": {"kind": "operating"},
            "availability": {"state": "verified_open"}, "blurb": "x" * pad}


def _size(obj):
    return len(json.dumps(obj, default=str))


def test_a_result_under_the_budget_is_untouched():
    result = {"jobs": [_card(i, 50) for i in range(5)], "policy": {"kept": 5}}
    assert fit_feed_result(result, max_bytes=10_000_000) is result


def test_an_oversized_result_fits_and_keeps_every_strong_match():
    jobs = [_card(i, 90 if i % 10 == 0 else 40 + i % 30) for i in range(1500)]
    result = {"jobs": jobs, "policy": {"kept": 1500}}
    budget = _size(result) // 2
    out = fit_feed_result(result, max_bytes=budget)
    assert _size(out) <= budget
    kept_scores = [j["match_score"] for j in out["jobs"]]
    assert kept_scores.count(90) == 150          # every strong match survives
    assert min(kept_scores) >= min(j["match_score"] for j in jobs)
    assert out["trimmed_for_size"]["dropped"] == 1500 - len(out["jobs"])
    assert out["policy"] == {"kept": 1500}       # other keys ride along


def test_survivors_keep_their_original_order():
    jobs = [_card(i, s) for i, s in enumerate([70, 10, 80, 20, 90, 30])]
    out = fit_feed_result({"jobs": jobs}, max_bytes=_size({"jobs": jobs[:4]}))
    ids = [j["job_id"] for j in out["jobs"]]
    assert ids == sorted(ids, key=lambda x: int(x[1:]))
    assert "j1" not in ids  # the 10 goes first


def test_on_a_tie_the_older_card_goes_first():
    jobs = [_card(0, 50, days=1), _card(1, 50, days=20)]
    out = fit_feed_result({"jobs": jobs}, max_bytes=_size({"jobs": jobs[:1]}) + 5)
    assert [j["job_id"] for j in out["jobs"]] == ["j0"]


def test_non_feed_shapes_pass_through():
    assert fit_feed_result(None, max_bytes=1) is None
    assert fit_feed_result({"text": "x" * 100}, max_bytes=1) == {"text": "x" * 100}


def test_the_poll_route_trims_feed_jobs_but_not_user_work(mock_db):
    from services.resume_service import get_resume_service
    svc = get_resume_service()
    big = {"jobs": [_card(i, i % 100, pad=4000) for i in range(1600)]}
    assert _size(big) > 6_000_000

    feed_id = svc.create_job("workday_jobs", {}, "a@b.com")
    svc.complete_job(feed_id, big)
    served = svc.get_job(feed_id, "a@b.com")
    assert _size(served["result"]) <= 4_500_000
    assert served["result"]["trimmed_for_size"]["dropped"] > 0

    own_id = svc.create_job("tailor", {}, "a@b.com")
    svc.complete_job(own_id, {"jobs": big["jobs"][:3]})
    assert "trimmed_for_size" not in svc.get_job(own_id, "a@b.com")["result"]


def test_feed_cards_do_not_carry_the_taxonomy_verdict():
    """The stored `tech` block scores the row and is read by no card; at ~680
    bytes it was a fifth of a 1,500-card tab (2026-10-24)."""
    import inspect
    from services import inventory_feed
    src = inspect.getsource(inventory_feed.build_feed)
    assert 'row.pop("tech", None)' in src
