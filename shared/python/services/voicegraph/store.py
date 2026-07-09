"""Knowledge-graph repository (§6.12.3).

Collections: graph_nodes, graph_edges, graph_evidence. All `user_email`-scoped.

THE HARD RULE, enforced here and not in a prompt: every node and every edge is
inserted together with at least one evidence row (source + verified char span).
`add_node` / `add_edge` raise ValueError if handed empty evidence — an ungrounded
node cannot exist. `purge_by_source` is the inverse and the basis of the Step-8
revocation cascade: drop a source's evidence, then delete any node/edge left with
no evidence at all (co-evidenced ones survive).
"""
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)


def _db():
    return DBConnect().get_db()


def _nodes():
    return _db().graph_nodes


def _edges():
    return _db().graph_edges


def _evidence():
    return _db().graph_evidence


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _valid_evidence(ev: Dict[str, Any]) -> bool:
    try:
        cs, ce = int(ev["char_start"]), int(ev["char_end"])
    except (KeyError, TypeError, ValueError):
        return False
    return bool(ev.get("source")) and cs >= 0 and ce > cs and bool(ev.get("quote"))


def _write_evidence(user_email: str, target_type: str, target_id: str,
                    evidence: List[Dict[str, Any]]) -> None:
    rows = []
    for ev in evidence:
        rows.append({
            "evidence_id": str(uuid4()),
            "user_email": user_email,
            "target_type": target_type,
            "target_id": target_id,
            "source": ev["source"],
            "source_id": ev.get("source_id", ""),
            "char_start": int(ev["char_start"]),
            "char_end": int(ev["char_end"]),
            "quote": ev["quote"],
            "created_at": _now(),
        })
    if rows:
        _evidence().insert_many(rows)


def add_node(user_email: str, node_type: str, label: str,
             evidence: List[Dict[str, Any]], canonical_label: str = "",
             confidence: float = 0.5, user_confirmed: bool = False,
             embedding: Optional[List[float]] = None) -> str:
    ev = [e for e in (evidence or []) if _valid_evidence(e)]
    if not ev:
        raise ValueError("add_node: a node requires at least one verified evidence span")
    node_id = str(uuid4())
    _nodes().insert_one({
        "node_id": node_id,
        "user_email": user_email,
        "type": node_type,
        "label": label,
        "canonical_label": (canonical_label or label or "").strip().lower(),
        "embedding": embedding,
        "confidence": float(confidence),
        "user_confirmed": bool(user_confirmed),
        "first_seen_at": _now(),
    })
    _write_evidence(user_email, "node", node_id, ev)
    return node_id


def add_edge(user_email: str, src_id: str, dst_id: str, rel: str,
             evidence: List[Dict[str, Any]], confidence: float = 0.5,
             user_confirmed: bool = False) -> str:
    ev = [e for e in (evidence or []) if _valid_evidence(e)]
    if not ev:
        raise ValueError("add_edge: an edge requires at least one verified evidence span")
    edge_id = str(uuid4())
    _edges().insert_one({
        "edge_id": edge_id,
        "user_email": user_email,
        "src_id": src_id,
        "dst_id": dst_id,
        "rel": rel,
        "confidence": float(confidence),
        "user_confirmed": bool(user_confirmed),
        "first_seen_at": _now(),
    })
    _write_evidence(user_email, "edge", edge_id, ev)
    return edge_id


def purge_by_source(user_email: str, source: str, source_id: str = "") -> Dict[str, int]:
    """Remove evidence rows from a given source, then delete any node/edge that has
    no remaining evidence (orphans). Nodes/edges still backed by other sources
    survive. Returns counts. This is the core of the revocation cascade (Step 8)
    and of idempotent re-extraction."""
    q = {"user_email": user_email, "source": source}
    if source_id:
        q["source_id"] = source_id

    affected_nodes = set()
    affected_edges = set()
    for ev in _evidence().find(q, {"target_type": 1, "target_id": 1}):
        if ev.get("target_type") == "node":
            affected_nodes.add(ev["target_id"])
        elif ev.get("target_type") == "edge":
            affected_edges.add(ev["target_id"])

    _evidence().delete_many(q)

    removed_nodes = 0
    removed_edges = 0
    for nid in affected_nodes:
        if _evidence().count_documents({"target_type": "node", "target_id": nid}) == 0:
            _nodes().delete_one({"node_id": nid, "user_email": user_email})
            # drop edges dangling off a removed node (and their evidence)
            dangling = list(_edges().find(
                {"user_email": user_email, "$or": [{"src_id": nid}, {"dst_id": nid}]},
                {"edge_id": 1},
            ))
            for e in dangling:
                affected_edges.add(e["edge_id"])
            removed_nodes += 1
    for eid in affected_edges:
        if _evidence().count_documents({"target_type": "edge", "target_id": eid}) == 0:
            res = _edges().delete_one({"edge_id": eid, "user_email": user_email})
            _evidence().delete_many({"target_type": "edge", "target_id": eid})
            removed_edges += res.deleted_count

    return {"nodes_removed": removed_nodes, "edges_removed": removed_edges}


# ---------------------------------------------------------------------------
# Reads (for the FE graph reveal + entity resolution + resume generation)
# ---------------------------------------------------------------------------
def get_graph(user_email: str) -> Dict[str, List[Dict[str, Any]]]:
    nodes = [_public_node(n) for n in _nodes().find({"user_email": user_email})]
    edges = [_public_edge(e) for e in _edges().find({"user_email": user_email})]
    return {"nodes": nodes, "edges": edges}


def evidence_for(target_type: str, target_id: str) -> List[Dict[str, Any]]:
    out = []
    for ev in _evidence().find({"target_type": target_type, "target_id": target_id}):
        out.append({
            "source": ev.get("source"),
            "source_id": ev.get("source_id"),
            "char_start": ev.get("char_start"),
            "char_end": ev.get("char_end"),
            "quote": ev.get("quote"),
        })
    return out


def _public_node(n: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "node_id": n.get("node_id"),
        "type": n.get("type"),
        "label": n.get("label"),
        "canonical_label": n.get("canonical_label"),
        "confidence": n.get("confidence"),
        "user_confirmed": n.get("user_confirmed", False),
    }


def _public_edge(e: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "edge_id": e.get("edge_id"),
        "src_id": e.get("src_id"),
        "dst_id": e.get("dst_id"),
        "rel": e.get("rel"),
        "confidence": e.get("confidence"),
        "user_confirmed": e.get("user_confirmed", False),
    }
