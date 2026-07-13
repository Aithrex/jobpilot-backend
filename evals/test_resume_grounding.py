"""§6.12.9 eval — resume grounding / graph span fidelity.

Contract under test:
  * store.py hard rule: a graph node/edge CANNOT exist without at least one
    verified evidence span (non-empty source + quote + sane offsets).
    ``add_node`` / ``add_edge`` must raise ValueError otherwise, and must
    persist NOTHING on rejection.
  * extract.py span fidelity: every quote the model returns must be an exact
    (or whitespace-tolerant) substring of the source sample. A quote that is
    not locatable is a hallucinated span → the WHOLE extraction is rejected
    (one retry, then a loud ValueError) and the graph is left untouched.
    Locatable quotes with wrong offsets are self-healed to the real position.
  * Unknown node types / edge rels returned by the model are dropped, never
    trusted.

Hard-fail criteria: any persisted node/edge without a verified evidence row,
any evidence quote that is not a real substring of its source, or a
hallucinated span that survives extraction.
"""
import pytest

from services.claude_client import CLAUDE_PRO
from services.voicegraph import extract, resolution, samples, store

USER = "grounding-eval@example.com"

SAMPLE_TEXT = (
    "Last spring our ingestion pipeline kept dying under load, so I rewrote "
    "the consumer group logic in Kafka and cut end-to-end lag from nine "
    "minutes to forty seconds. I also moved the checkpoint store to "
    "PostgreSQL so replays became deterministic and boring."
)


def _mk_sample(text=SAMPLE_TEXT):
    doc = samples.save_sample(USER, text, finalize=True)
    return doc["sample_id"]


def _isolate_resolution(monkeypatch):
    """extract_graph_from_sample ends by running entity resolution against the
    parsed resume. Pin that boundary to 'no resume records' so this eval is
    deterministic regardless of what other test modules seeded."""
    monkeypatch.setattr(
        resolution, "_resume_records",
        lambda user_email: {"skills": [], "orgs": [], "roles": [], "projects": []},
    )


def _node_resp(quote, char_start, char_end, node_type="Skill", label="Kafka",
               temp_id="n1", confidence=0.9):
    return {
        "temp_id": temp_id, "type": node_type, "label": label, "role": "",
        "quote": quote, "char_start": char_start, "char_end": char_end,
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# store.py — the evidence-required invariant, exhaustively
# ---------------------------------------------------------------------------
def test_store_rejects_every_ungrounded_shape(mock_db):
    bad_evidence = [
        [],                                                            # empty
        None,                                                          # missing
        [{"source": "writing_sample", "char_start": 0, "char_end": 5}],  # no quote
        [{"source": "writing_sample", "quote": "kafka"}],              # no offsets
        [{"source": "", "char_start": 0, "char_end": 5, "quote": "kafka"}],  # no source
        [{"source": "writing_sample", "char_start": 5, "char_end": 5,
          "quote": "kafka"}],                                          # empty span
        [{"source": "writing_sample", "char_start": -1, "char_end": 5,
          "quote": "kafka"}],                                          # negative start
    ]
    for ev in bad_evidence:
        with pytest.raises(ValueError):
            store.add_node(USER, "Skill", "kafka", evidence=ev)
    # nothing may have been persisted by the failed inserts
    assert mock_db.graph_nodes.count_documents({"user_email": USER}) == 0
    assert mock_db.graph_evidence.count_documents({"user_email": USER}) == 0

    # edges enforce the same rule
    good = [{"source": "writing_sample", "source_id": "s1",
             "char_start": 0, "char_end": 5, "quote": "kafka"}]
    a = store.add_node(USER, "Skill", "kafka", evidence=good)
    b = store.add_node(USER, "Problem", "lag", evidence=good)
    for ev in bad_evidence:
        with pytest.raises(ValueError):
            store.add_edge(USER, a, b, "FACED", evidence=ev)
    assert mock_db.graph_edges.count_documents({"user_email": USER}) == 0


# ---------------------------------------------------------------------------
# extract.py — hallucinated spans reject the whole extraction
# ---------------------------------------------------------------------------
def test_extract_rejects_hallucinated_span_and_persists_nothing(
        mock_db, fake_llm, monkeypatch):
    _isolate_resolution(monkeypatch)
    sid = _mk_sample()
    fabricated = {
        "nodes": [
            # a real quote — would be fine on its own
            _node_resp("Kafka", 0, 5, temp_id="n1"),
            # a hallucinated span: NOT a substring of the sample
            _node_resp("architected a global multi-region failover",
                       0, 43, node_type="Outcome",
                       label="global failover", temp_id="n2"),
        ],
        "edges": [],
    }
    # both the first attempt and the single retry hallucinate
    fake_llm.queue(fabricated, fabricated)

    with pytest.raises(ValueError):
        extract.extract_graph_from_sample(USER, sid)

    assert len(fake_llm.calls) == 2, "must retry exactly once, then fail loudly"
    # the graph must be COMPLETELY untouched — no partial insert of the
    # 'good' node from a rejected extraction
    assert mock_db.graph_nodes.count_documents({"user_email": USER}) == 0
    assert mock_db.graph_edges.count_documents({"user_email": USER}) == 0
    assert mock_db.graph_evidence.count_documents({"user_email": USER}) == 0


def test_extract_recovers_when_retry_is_grounded(mock_db, fake_llm, monkeypatch):
    _isolate_resolution(monkeypatch)
    sid = _mk_sample()
    bad = {"nodes": [_node_resp("invented quote never written", 0, 10)],
           "edges": []}
    good = {"nodes": [_node_resp("Kafka", 0, 5)], "edges": []}
    fake_llm.queue(bad, good)

    out = extract.extract_graph_from_sample(USER, sid)
    assert out["nodes"] == 1
    assert len(fake_llm.calls) == 2


# ---------------------------------------------------------------------------
# extract.py — offsets self-heal to the located span
# ---------------------------------------------------------------------------
def test_extract_self_heals_wrong_offsets(mock_db, fake_llm, monkeypatch):
    _isolate_resolution(monkeypatch)
    sid = _mk_sample()
    quote = "cut end-to-end lag"
    resp = {
        "nodes": [
            # real quote, absurd offsets → must heal to the true position
            _node_resp(quote, 999, 1017, node_type="Outcome", label="lag cut"),
            # whitespace-mangled quote → whitespace-tolerant locate must heal it
            _node_resp("consumer   group\tlogic", 0, 5, node_type="Skill",
                       label="consumer groups", temp_id="n2"),
        ],
        "edges": [],
    }
    fake_llm.queue(resp)

    out = extract.extract_graph_from_sample(USER, sid)
    assert out["nodes"] == 2

    rows = list(mock_db.graph_evidence.find({"user_email": USER}))
    assert len(rows) == 2
    for ev in rows:
        cs, ce = ev["char_start"], ev["char_end"]
        assert 0 <= cs < ce <= len(SAMPLE_TEXT)
        # the stored quote is EXACTLY the text at the stored offsets
        assert ev["quote"] == SAMPLE_TEXT[cs:ce]
    healed = next(e for e in rows if e["quote"] == quote)
    assert healed["char_start"] == SAMPLE_TEXT.find(quote)


# ---------------------------------------------------------------------------
# extract.py — happy path: zero ungrounded rows, unknown types dropped
# ---------------------------------------------------------------------------
def test_every_persisted_row_is_grounded_and_unknown_types_dropped(
        mock_db, fake_llm, monkeypatch):
    _isolate_resolution(monkeypatch)
    sid = _mk_sample()
    q_problem = "ingestion pipeline kept dying under load"
    resp = {
        "nodes": [
            _node_resp("Kafka", SAMPLE_TEXT.find("Kafka"),
                       SAMPLE_TEXT.find("Kafka") + 5, temp_id="n1"),
            _node_resp(q_problem, SAMPLE_TEXT.find(q_problem),
                       SAMPLE_TEXT.find(q_problem) + len(q_problem),
                       node_type="Problem", label="ingestion dying",
                       temp_id="n2"),
            # unknown node type — must be dropped, never trusted
            _node_resp("PostgreSQL", SAMPLE_TEXT.find("PostgreSQL"),
                       SAMPLE_TEXT.find("PostgreSQL") + 10,
                       node_type="Fabrication", label="made-up type",
                       temp_id="n3"),
        ],
        "edges": [
            {"src": "n1", "dst": "n2", "rel": "FACED",
             "quote": "rewrote the consumer group logic in Kafka",
             "char_start": 0, "char_end": 10, "confidence": 0.8},
            # unknown rel — must be dropped
            {"src": "n1", "dst": "n2", "rel": "INVENTED_REL",
             "quote": "Kafka", "char_start": 0, "char_end": 5,
             "confidence": 0.8},
        ],
    }
    fake_llm.queue(resp)

    out = extract.extract_graph_from_sample(USER, sid)
    assert out["nodes"] == 2, "unknown node type must be dropped"
    assert out["edges"] == 1, "unknown edge rel must be dropped"

    # THE eval assertion: zero ungrounded rows.
    for n in mock_db.graph_nodes.find({"user_email": USER}):
        evs = list(mock_db.graph_evidence.find(
            {"user_email": USER, "target_type": "node",
             "target_id": n["node_id"]}))
        assert evs, f"ungrounded node persisted: {n['label']}"
        for ev in evs:
            assert ev["quote"] and ev["quote"] in SAMPLE_TEXT
            assert ev["quote"] == SAMPLE_TEXT[ev["char_start"]:ev["char_end"]]
    for e in mock_db.graph_edges.find({"user_email": USER}):
        evs = list(mock_db.graph_evidence.find(
            {"user_email": USER, "target_type": "edge",
             "target_id": e["edge_id"]}))
        assert evs, "ungrounded edge persisted"
        for ev in evs:
            assert ev["quote"] and ev["quote"] in SAMPLE_TEXT

    # the fabricated type never landed anywhere
    assert mock_db.graph_nodes.count_documents(
        {"user_email": USER, "type": "Fabrication"}) == 0

    # extraction ran at PRO tier, temperature 0, with the closed schema
    call = fake_llm.calls[0]
    assert call["model"] == CLAUDE_PRO
    assert call["temperature"] == 0.0
    assert call["schema"] is not None
