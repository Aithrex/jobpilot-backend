"""Marketplace boards (Jobgether) pin below first-party employers."""
from __future__ import annotations

from services.career_pages_catalog import (
    is_marketplace_board,
    marketplace_sort_key,
)


def test_jobgether_is_marketplace():
    assert is_marketplace_board({"slug": "jobgether", "board_kind": "marketplace"})
    assert is_marketplace_board({"tenant": "jobgether", "company": "Acme"})
    assert is_marketplace_board("jobgether")
    assert not is_marketplace_board({"slug": "spotify", "ats": "lever"})


def test_marketplace_sort_key_pins_last():
    employer = {"tenant": "spotify", "match_score": 40}
    market = {"tenant": "jobgether", "board_kind": "marketplace", "match_score": 99}
    rows = [market, employer]
    rows.sort(key=lambda j: (
        marketplace_sort_key(j),
        -(j.get("match_score") or 0),
    ))
    assert rows[0]["tenant"] == "spotify"
    assert rows[1]["tenant"] == "jobgether"
