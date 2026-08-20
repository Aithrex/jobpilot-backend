"""Marketplace boards (Jobgether) stay in catalog but rank below employers."""
from services.career_pages_catalog import (
    is_marketplace_board,
    marketplace_sort_key,
)
from services.career_pages_service import _row


def test_jobgether_is_marketplace_board():
    assert is_marketplace_board("jobgether")
    assert is_marketplace_board({"slug": "jobgether", "board_kind": "marketplace"})
    assert not is_marketplace_board("spotify")


def test_career_row_tags_jobgether_as_marketplace():
    company = {
        "display_name": "Jobgether",
        "ats": "lever",
        "slug": "jobgether",
        "industry": "tech",
        "board_kind": "marketplace",
    }
    row = _row(company, "abc", "Software Engineer", "https://jobs.lever.co/jobgether/abc",
               "2026-08-01", "Remote")
    assert row["board_kind"] == "marketplace"
    assert marketplace_sort_key(row) == 1
    assert marketplace_sort_key({"tenant": "spotify", "company": "Spotify"}) == 0
