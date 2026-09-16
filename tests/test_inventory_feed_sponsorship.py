"""The inventory path must demote postings that state a US-only requirement.

Regression for a live defect: on an F1-OPT account, RTX held six of the first
twenty-four cards WITH an H-1B chip, while its postings stored
`clearance_required: true` and `work_authorization_restricted: true`.
`_attach_sponsorship` reads `title + description`, and inventory rows carry an
empty description by design (the body lives in S3), so the JD signal was blind
and every row fell through to the tenant-tier table — which resolved RTX to
`likely`. The demotion only fires on `refused`/`rare`, so it fired on nothing.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared", "python"))

from services import inventory_feed as IF
from services.workday_jobs_service import _attach_sponsorship


def _rows():
    return [
        {"company": "Rtx", "tenant": "globalhr", "title": "Radar Systems Engineer I",
         "description": "",
         "_requirements": {"clearance_required": True,
                           "work_authorization_restricted": True}},
        {"company": "Cisco", "tenant": "cisco", "title": "Software Engineer I",
         "description": "",
         "_requirements": {"clearance_required": False,
                           "work_authorization_restricted": False}},
        {"company": "Quiet Co", "tenant": "zzz", "title": "SWE",
         "description": "", "_requirements": {}},
    ]


def test_stored_requirements_outrank_the_tenant_tier_estimate():
    rows = _rows()
    _attach_sponsorship(rows)
    assert rows[0]["sponsorship"] == "likely"   # the defect, on the old path
    IF._sponsorship_from_stored_requirements(rows)
    assert rows[0]["sponsorship"] == "refused"
    assert rows[0]["h1b_sponsor"] is False
    assert rows[1]["sponsorship"] == "likely"   # untouched
    assert rows[2]["sponsorship"] == "unknown"  # silence stays unknown


def test_demotion_now_fires_for_a_sponsorship_needing_user(monkeypatch):
    rows = _rows()
    _attach_sponsorship(rows)
    IF._sponsorship_from_stored_requirements(rows)
    monkeypatch.setattr(IF, "requires_sponsorship", lambda _e: True)
    IF._demote_unreachable_sponsors(rows, "x@y.z")
    assert rows[0].get("sponsorship_demoted") is True
    assert not rows[1].get("sponsorship_demoted")
    assert not rows[2].get("sponsorship_demoted")


def test_nothing_is_demoted_for_a_user_who_does_not_need_sponsorship(monkeypatch):
    rows = _rows()
    _attach_sponsorship(rows)
    IF._sponsorship_from_stored_requirements(rows)
    monkeypatch.setattr(IF, "requires_sponsorship", lambda _e: False)
    IF._demote_unreachable_sponsors(rows, "x@y.z")
    assert not any(r.get("sponsorship_demoted") for r in rows)
