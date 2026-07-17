"""Voice Graph → fact store grounding (Craft-overhaul Part B2).

A user's consented, evidence-backed Voice Graph nodes become additional
grounding facts for tailoring. Gated on the voice_graph consent scope and
per-user. Uses the mongomock `mock_db` fixture — no Bedrock.
"""
import pytest

from services.profile_facts import (
    build_fact_store,
    grounding_texts,
    enrichment_context,
)
from services.voicegraph import store as vg_store
from services.voicegraph import consent as vg_consent

USER = "a@x.com"
OTHER = "b@x.com"


def _ev(quote):
    return [{"source": "writing_sample", "source_id": "s1",
             "char_start": 0, "char_end": len(quote), "quote": quote}]


def _add_confirmed_node(user, label, quote, confidence=0.9):
    return vg_store.add_node(
        user, "Skill", label, evidence=_ev(quote),
        confidence=confidence, user_confirmed=True,
    )


def test_consented_graph_facts_flow_into_grounding(mock_db):
    _add_confirmed_node(USER, "kafka", "I built a Kafka pipeline for telemetry.")
    vg_consent.grant(USER)

    store = build_fact_store(user_email=USER, structured_resume={})
    assert store["sources"]["voicegraph"] is True

    vg_facts = [f for f in store["facts"] if f["source"] == "voicegraph"]
    assert vg_facts, "expected a voicegraph fact"
    assert vg_facts[0]["fact_id"].startswith("vg:node:")

    # The verbatim quote must reach grounding + the prompt block.
    grounds = grounding_texts(store)
    assert any("Kafka pipeline for telemetry" in g for g in grounds)
    ctx = enrichment_context(store)
    assert "Kafka pipeline for telemetry" in ctx


def test_without_consent_no_graph_facts(mock_db):
    _add_confirmed_node(USER, "kafka", "I built a Kafka pipeline for telemetry.")
    # No consent granted.
    store = build_fact_store(user_email=USER, structured_resume={})
    assert store["sources"]["voicegraph"] is False
    assert not [f for f in store["facts"] if f["source"] == "voicegraph"]


def test_include_voicegraph_false_disables(mock_db):
    _add_confirmed_node(USER, "kafka", "I built a Kafka pipeline for telemetry.")
    vg_consent.grant(USER)
    store = build_fact_store(user_email=USER, structured_resume={},
                             include_voicegraph=False)
    assert store["sources"]["voicegraph"] is False


def test_firewall_only_own_graph(mock_db):
    # OTHER has a consented node; USER has none. USER must not see it.
    _add_confirmed_node(OTHER, "kafka", "I built a Kafka pipeline for telemetry.")
    vg_consent.grant(OTHER)
    vg_consent.grant(USER)  # USER consents but has no nodes

    store = build_fact_store(user_email=USER, structured_resume={})
    assert not [f for f in store["facts"] if f["source"] == "voicegraph"]
    assert "Kafka pipeline for telemetry" not in enrichment_context(store)


def test_unconfirmed_or_low_confidence_nodes_excluded(mock_db):
    vg_consent.grant(USER)
    # unconfirmed
    vg_store.add_node(USER, "Skill", "redis", evidence=_ev("used redis"),
                      confidence=0.9, user_confirmed=False)
    # confirmed but low confidence
    vg_store.add_node(USER, "Skill", "go", evidence=_ev("some go"),
                      confidence=0.3, user_confirmed=True)
    store = build_fact_store(user_email=USER, structured_resume={})
    assert not [f for f in store["facts"] if f["source"] == "voicegraph"]


def test_voicegraph_merge_failure_fails_open(mock_db, monkeypatch):
    # If the graph read raises, tailoring must still get a valid resume-only
    # fact store.
    import services.profile_facts as pf

    def _boom(_email):
        raise RuntimeError("graph exploded")

    monkeypatch.setattr(pf, "_voicegraph_facts", _boom)
    store = build_fact_store(user_email=USER, structured_resume={
        "experience": [{"company": "Acme", "title": "SWE", "dates": "2020-2022"}]
    })
    assert store["sources"]["voicegraph"] is False
    assert any(f["source"] == "resume" for f in store["facts"])
