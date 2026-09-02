"""Telemetry that cannot carry identity, because there is nowhere to put it.

The privacy claim is structural, not procedural: the account is a salted
one-way pseudonym, the dimension keys are a closed allowlist, filter VALUES
are never accepted, and result counts are bucketed. A client that wanted to
send an email address would have to invent a field the writer discards.
"""
from __future__ import annotations

import pytest

from _vg_support import current_db
from services import tech_feed_telemetry as tft

USER = "seeker@example.com"
OTHER = "other@example.com"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setenv("TELEMETRY_SALT", "test-salt")
    db = current_db()
    db[tft.EVENTS].delete_many({})
    yield
    db[tft.EVENTS].delete_many({})


class TestNoIdentityIsStored:

    def test_the_email_never_reaches_the_collection(self):
        tft.record([{"event": tft.EV_SEARCH}], user_email=USER)
        raw = repr(list(current_db()[tft.EVENTS].find({})))
        assert USER not in raw
        assert "seeker" not in raw
        assert "@" not in raw

    def test_the_pseudonym_is_stable_and_one_way(self):
        a = tft.pseudonym(USER)
        b = tft.pseudonym(USER)
        assert a == b and a and USER not in a
        assert tft.pseudonym(OTHER) != a

    def test_the_salt_changes_the_pseudonym(self, monkeypatch):
        before = tft.pseudonym(USER)
        monkeypatch.setenv("TELEMETRY_SALT", "rotated")
        assert tft.pseudonym(USER) != before

    def test_an_unconfigured_salt_does_not_fall_back_to_a_bare_hash(self, monkeypatch):
        """Failing open here would make the mapping rebuildable from a list of
        addresses. It fails to a per-process random salt instead, which loses
        the metric and keeps the property."""
        import hashlib
        monkeypatch.delenv("TELEMETRY_SALT", raising=False)
        monkeypatch.setattr(tft, "_PROCESS_SALT", None)
        got = tft.pseudonym(USER)
        bare = hashlib.sha256(USER.encode()).hexdigest()[:20]
        assert got != bare

    def test_no_account_yields_an_empty_subject_not_an_error(self):
        assert tft.pseudonym("") == ""
        assert tft.record([{"event": tft.EV_SEARCH}])["accepted"] == 1


class TestTheClosedVocabulary:

    def test_an_unknown_event_is_rejected_not_stored(self):
        got = tft.record([{"event": "exfiltrate_everything"}], user_email=USER)
        assert got == {"accepted": 0, "rejected": 1}
        assert current_db()[tft.EVENTS].count_documents({}) == 0

    def test_a_dimension_outside_the_allowlist_is_dropped(self):
        tft.record([{"event": tft.EV_SEARCH,
                     "dimensions": {"filter_keys": ["company_ids"],
                                    "email": USER,
                                    "resume_text": "my whole cv",
                                    "job_id": "abc123"}}], user_email=USER)
        doc = current_db()[tft.EVENTS].find_one({})
        assert set(doc["dimensions"]) == {"filter_keys"}
        assert doc["dimensions"]["filter_keys"] == ["company_ids"]

    def test_filter_KEYS_are_recorded_and_values_are_not(self):
        """"Somebody filtered by company" is a product fact. "Somebody
        filtered by Stripe" is that person's job hunt."""
        tft.record([{"event": tft.EV_FILTER_APPLIED,
                     "dimensions": {"filter_keys": ["company_ids", "countries"]}}],
                   user_email=USER)
        doc = current_db()[tft.EVENTS].find_one({})
        assert doc["dimensions"]["filter_keys"] == ["company_ids", "countries"]

    def test_a_value_carrying_free_text_is_stripped_to_a_slug(self):
        tft.record([{"event": tft.EV_SOURCE_FAILURE,
                     "dimensions": {"ats": "greenhouse <script>alert(1)</script>"}}],
                   user_email=USER)
        doc = current_db()[tft.EVENTS].find_one({})
        assert doc["dimensions"]["ats"] == "greenhousescriptalert1script"

    def test_result_counts_are_bucketed_never_exact(self):
        assert tft.result_bucket(0) == "0"
        assert tft.result_bucket(4) == "1-9"
        assert tft.result_bucket(12) == "10-49"
        assert tft.result_bucket(4000) == "50+"


class TestItCannotBreakAUserAction:

    def test_rubbish_in_a_batch_is_counted_not_raised(self):
        got = tft.record(["nonsense", None, 42,
                          {"event": tft.EV_CARD_OPEN}], user_email=USER)
        assert got == {"accepted": 1, "rejected": 3}

    def test_a_batch_is_capped(self):
        got = tft.record([{"event": tft.EV_CARD_IMPRESSION}] * 500, user_email=USER)
        assert got["accepted"] == tft.MAX_EVENTS_PER_BATCH

    def test_a_dead_database_returns_a_count_rather_than_raising(self):
        class _Boom:
            def __getitem__(self, name):
                raise RuntimeError("mongo down")
        got = tft.record([{"event": tft.EV_SEARCH}], user_email=USER, db=_Boom())
        assert got["accepted"] == 0


class TestSummary:

    def test_the_funnel_and_the_filter_use_are_aggregate_only(self):
        tft.record([
            {"event": tft.EV_CARD_IMPRESSION, "count": 100},
            {"event": tft.EV_CARD_OPEN, "count": 20},
            {"event": tft.EV_CARD_APPLY_CLICK, "count": 5},
            {"event": tft.EV_SEARCH,
             "dimensions": {"filter_keys": ["countries", "seniority"]}},
            {"event": tft.EV_ZERO_RESULT, "dimensions": {"reason": "role_specialty"}},
            {"event": tft.EV_SOURCE_FAILURE, "dimensions": {"ats": "icims"}},
        ], user_email=USER)
        out = tft.summary(hours=24)
        assert out["by_event"][tft.EV_CARD_IMPRESSION]["events"] == 100
        assert out["card_open_rate"] == 0.2
        assert out["card_apply_click_rate"] == 0.05
        assert out["filter_use"]["countries"] == 1
        assert out["zero_result_reasons"]["role_specialty"] == 1
        assert out["source_failures_by_vendor"]["icims"] == 1

    def test_distinct_accounts_are_countable_and_not_identifiable(self):
        tft.record([{"event": tft.EV_SEARCH}], user_email=USER)
        tft.record([{"event": tft.EV_SEARCH}], user_email=OTHER)
        out = tft.summary(hours=24)
        assert out["by_event"][tft.EV_SEARCH]["accounts"] == 2
        assert USER not in repr(out) and OTHER not in repr(out)
