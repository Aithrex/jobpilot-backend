"""Perpetual-missing detector — mongomock only."""


def test_perpetual_flag_after_enough_samples(mock_db, monkeypatch):
    import services.keyword_gap_stats as st

    monkeypatch.setattr(st, "MIN_SEEN", 2)
    monkeypatch.setattr(st, "PERPETUAL_RATE", 0.01)
    mock_db[st.COLLECTION].delete_many({})

    first = st.record_observation(["Python", "PhantomTerm"], ["PhantomTerm"])
    assert first == []
    flagged = st.record_observation(["Python", "PhantomTerm"], ["PhantomTerm"])
    assert "phantomterm" in flagged


def test_as_audit_is_the_sidecar_shape():
    from services.keyword_gap_engine import as_audit

    audit = as_audit({
        "matched_keywords": ["Python"],
        "missing_keywords": ["Kafka"],
        "partial_matches": [],
        "coverage_percentage": 50,
        "required_missing": ["Kafka"],
        "required_coverage": 0,
    })
    assert audit["matched"] == ["Python"]
    assert audit["missing"] == ["Kafka"]
    assert audit["required_missing"] == ["Kafka"]
