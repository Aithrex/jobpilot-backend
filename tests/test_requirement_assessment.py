"""Package 4: canonical AND/OR requirements, observation, source reconciliation."""
from __future__ import annotations

from services.requirement_assessment import (
    attach_expressions,
    canonicalize_requirements,
    evaluate_requirements,
    observe_document,
    parse_expression_from_text,
    reconcile_source,
)
from services.assessment_policy import reduce_expression, observations_from_pairs
from fixtures.assessment.cases import (
    AND_OR_JD,
    SQL_NOSQL_JD,
    SQL_NOSQL_RESUME,
)


def test_parse_python_or_java_plus_sql():
    expr = parse_expression_from_text("Python or Java, plus SQL", ["Python", "Java"])
    assert expr.kind == "and"
    assert expr.children[0].kind == "or"
    assert expr.children[1].kind == "atomic"
    assert expr.children[1].predicate.capability == "SQL"


def test_ambiguous_and_or_unresolved():
    expr = parse_expression_from_text("Python or Java and SQL")
    assert expr.kind == "unresolved"


def test_sql_nosql_observation():
    reqs = canonicalize_requirements(SQL_NOSQL_JD)
    text = "Operated MongoDB NoSQL clusters for product analytics."
    obs = observe_document(text, reqs)
    # SQL required — must not be satisfied by NoSQL alone
    sql_keys = [k for k in obs if k.lower() == "sql"]
    assert sql_keys
    assert obs[sql_keys[0]].document_state == "not_demonstrated"


def test_and_or_reducer_on_document():
    jd = attach_expressions(AND_OR_JD)
    # Ensure expression attached
    assert jd["requirements"][0].get("expression", {}).get("kind") == "and"
    reqs = canonicalize_requirements(jd)
    text = "Built services in Python. Wrote SQL against the warehouse."
    obs = observe_document(text, reqs)
    state = reduce_expression(reqs[0].expression, obs)
    assert state == "satisfied"


def test_wrong_employer_contradicted():
    facts = [
        {"id": "f1", "entity": "experience.0", "text": "Built billing APIs in Python.", "kind": "accomplishment"},
        {"id": "f2", "entity": "experience.1", "text": "Built billing APIs in Python.", "kind": "accomplishment"},
    ]
    # Claim attributed to Globex (experience.1) but we require Acme (experience.0)
    ss, cs, refs, _ = reconcile_source(
        document_text="Built billing APIs in Python.",
        document_refs=[],
        source_facts=facts,
        claim_text="Built billing APIs in Python.",
        expected_entity="experience.0",
    )
    # Both entities match the text; with expected_entity experience.0 there is a match.
    assert ss == "supported"
    # Force wrong employer only:
    ss2, cs2, _, _ = reconcile_source(
        document_text="Built billing APIs in Python.",
        document_refs=[],
        source_facts=[facts[1]],  # only Globex fact
        claim_text="Built billing APIs in Python.",
        expected_entity="experience.0",
    )
    assert ss2 == "contradicted"
    assert cs2 == "contradicted"


def test_evaluate_counts_no_overall():
    result = evaluate_requirements(
        document_text="Python and SQL engineer. Wrote SQL queries.",
        jd=attach_expressions(AND_OR_JD),
        resume=SQL_NOSQL_RESUME,
    )
    assert "counts" in result
    assert "evaluations" in result
    assert all("document_state" in e for e in result["evaluations"])


def test_keyword_repetition_does_not_change_state():
    reqs = canonicalize_requirements({"required_skills": ["Python"], "requirements": []})
    once = observe_document("Used Python once.", reqs)
    many = observe_document("Python Python Python engineer.", reqs)
    assert once["python"].document_state == many["python"].document_state == "satisfied"
