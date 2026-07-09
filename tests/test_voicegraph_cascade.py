"""End-to-end test of the Voice Graph evidence invariant + revocation cascade.

Runs against mongomock (no live Mongo). Covers the two properties that must never
regress (§6.12.3 / §6.12.8):
  1. A node/edge cannot be inserted without a verified evidence span.
  2. Revoking consent deletes samples + voice profile, drops graph rows whose ONLY
     evidence is a writing sample, KEEPS co-evidenced nodes, and FREEZES (never
     deletes) already-generated resumes.

Run: PYTHONPATH=shared/python python tests/test_voicegraph_cascade.py
"""
import sys

import mongomock

from utils import db_connect

# Patch the Mongo singleton to an in-memory mock before importing the services.
_MOCK_DB = mongomock.MongoClient()["portfolio_db"]
db_connect.DBConnect.get_db = lambda self=None: _MOCK_DB

from services.voicegraph import store, privacy, consent  # noqa: E402

USER = "priya@example.com"


def _reset():
    for c in ("graph_nodes", "graph_edges", "graph_evidence", "writing_samples",
              "style_profiles", "authored_resumes", "consents"):
        _MOCK_DB[c].delete_many({})


def test_evidence_required():
    _reset()
    try:
        store.add_node(USER, "Skill", "kafka", evidence=[])
        assert False, "add_node must reject empty evidence"
    except ValueError:
        pass
    # a span with no quote is not valid evidence either
    try:
        store.add_node(USER, "Skill", "kafka",
                       evidence=[{"source": "writing_sample", "char_start": 0, "char_end": 5}])
        assert False, "add_node must reject evidence with no quote"
    except ValueError:
        pass
    print("evidence-required invariant: PASS")


def test_cascade_keeps_co_evidenced():
    _reset()
    # node A: only writing-sample evidence  -> must die on revoke
    a = store.add_node(USER, "Problem", "ingestion dying",
                       evidence=[{"source": "writing_sample", "source_id": "s1",
                                  "char_start": 0, "char_end": 10, "quote": "ingestion"}])
    # node B: co-evidenced by a writing sample AND the resume -> must survive
    b = store.add_node(USER, "Skill", "kafka",
                       evidence=[
                           {"source": "writing_sample", "source_id": "s1",
                            "char_start": 11, "char_end": 16, "quote": "kafka"},
                           {"source": "resume", "source_id": "r1",
                            "char_start": 0, "char_end": 5, "quote": "Kafka"},
                       ])
    store.add_edge(USER, b, a, "FACED",
                   evidence=[{"source": "writing_sample", "source_id": "s1",
                              "char_start": 0, "char_end": 16, "quote": "ingestion kafka"}])

    # seed the rest of the user's Voice Graph state
    _MOCK_DB.writing_samples.insert_many([
        {"sample_id": "s1", "user_email": USER, "text_enc": "x", "status": "ready"},
        {"sample_id": "s2", "user_email": USER, "text_enc": "y", "status": "draft"},
    ])
    _MOCK_DB.style_profiles.insert_one({"user_email": USER, "version": 1})
    _MOCK_DB.authored_resumes.insert_one({"resume_id": "res1", "user_email": USER,
                                          "provenance_mode": "authored"})
    consent.grant(USER, ip="127.0.0.1")

    summary = privacy.revoke_cascade(USER)

    # node A gone, node B survives (still resume-evidenced)
    assert _MOCK_DB.graph_nodes.find_one({"node_id": a}) is None, "sample-only node must be deleted"
    assert _MOCK_DB.graph_nodes.find_one({"node_id": b}) is not None, "co-evidenced node must survive"
    # B's writing-sample evidence is gone, resume evidence remains
    ev_sources = {e["source"] for e in _MOCK_DB.graph_evidence.find({"target_id": b})}
    assert ev_sources == {"resume"}, f"expected only resume evidence on B, got {ev_sources}"
    # samples + style profile deleted
    assert _MOCK_DB.writing_samples.count_documents({"user_email": USER}) == 0
    assert _MOCK_DB.style_profiles.count_documents({"user_email": USER}) == 0
    # generated resume FROZEN, not deleted
    r = _MOCK_DB.authored_resumes.find_one({"resume_id": "res1"})
    assert r is not None and r.get("frozen") is True, "generated resume must be frozen, not deleted"
    # consent revoked
    assert consent.is_granted(USER) is False
    print("revocation cascade (co-evidenced survival + freeze): PASS", summary)


def test_entity_resolution():
    _reset()
    from services.voicegraph import resolution
    # isolate from the resume parser: feed fixed resume records
    resolution._resume_records = lambda user_email: {
        "skills": ["Kafka", "PostgreSQL"], "orgs": [], "roles": [], "projects": [],
    }
    ev = lambda q: [{"source": "writing_sample", "source_id": "s1",
                     "char_start": 0, "char_end": len(q), "quote": q}]
    n_kafka = store.add_node(USER, "Skill", "kafka", evidence=ev("kafka"))
    n_pg = store.add_node(USER, "Skill", "postgres", evidence=ev("postgres"))
    n_redis = store.add_node(USER, "Skill", "redis", evidence=ev("redis"))

    summary = resolution.resolve_entities(USER)

    # exact (kafka == Kafka) auto-merges: confirmed + resume co-evidence
    k = store.get_node(USER, n_kafka)
    assert k["user_confirmed"] is True, "exact match must auto-confirm"
    k_sources = {e["source"] for e in _MOCK_DB.graph_evidence.find({"target_id": n_kafka})}
    assert "resume" in k_sources, "auto-merge must add resume co-evidence"
    # postgres ~ PostgreSQL is ambiguous -> pending candidate, NOT auto-merged
    assert store.get_node(USER, n_pg)["user_confirmed"] is False
    cands = resolution.list_candidates(USER)
    assert any(c["node_id"] == n_pg for c in cands), "ambiguous match must be surfaced, not guessed"
    # redis has no resume match -> nothing
    assert store.get_node(USER, n_redis)["user_confirmed"] is False
    assert not any(c["node_id"] == n_redis for c in cands)
    print("entity resolution (auto-merge / surface-ambiguous / ignore): PASS", summary)


if __name__ == "__main__":
    test_evidence_required()
    test_cascade_keeps_co_evidenced()
    test_entity_resolution()
    print("ALL VOICE GRAPH TESTS PASSED")
    sys.exit(0)
