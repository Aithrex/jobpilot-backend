"""Serving reads the board-ownership map only when it is already warm.

`build_board_ownership` scans every `career_sources` row. At our traffic most
API requests land on a cold Lambda container, so building it inside a feed
request would put a full collection scan in front of most users. The policy's
trust block uses `warm_board_ownership`, which never builds.
"""
from services import canonical_employer as ce
from services import employer_policy as ep


def test_warm_returns_none_until_something_builds_the_map(monkeypatch):
    ce.reset_cache()
    built = []
    monkeypatch.setattr(ce, "build_board_ownership",
                        lambda db=None: built.append(1) or "MAP")
    assert ce.warm_board_ownership() is None
    assert ep._board_ownership() is None
    assert built == [], "a serving path must never build the map"
    assert ce.cached_board_ownership() == "MAP"
    assert ce.warm_board_ownership() == "MAP" and ep._board_ownership() == "MAP"
    ce.reset_cache()


def test_a_stale_map_is_not_warm(monkeypatch):
    ce.reset_cache()
    monkeypatch.setattr(ce, "build_board_ownership", lambda db=None: "MAP")
    ce.cached_board_ownership()
    assert ce.warm_board_ownership(ttl=0) is None
    ce.reset_cache()
