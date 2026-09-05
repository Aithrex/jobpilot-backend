"""Package 1: deterministic AND/OR requirement reducer truth table."""
import pytest

from schemas.assessment_contracts import atomic, group_and, group_or, unresolved
from services.assessment_policy import (
    POLICY_VERSION,
    RUBRIC_VERSION,
    evidence_expectation_ok,
    observations_from_pairs,
    reduce_expression,
    severity_for,
)


def _obs(*pairs):
    return observations_from_pairs(pairs)


# ---------------------------------------------------------------------------
# Atomic
# ---------------------------------------------------------------------------

def test_atomic_satisfied():
    expr = atomic("Python")
    assert reduce_expression(expr, _obs(("Python", "satisfied"))) == "satisfied"


def test_atomic_missing_observation_is_unknown():
    assert reduce_expression(atomic("Rust"), _obs(("Python", "satisfied"))) == "unknown"


def test_atomic_unresolved_leaf_is_unknown():
    assert reduce_expression(
        atomic("Python"), _obs(("Python", "satisfied", True)),
    ) == "unknown"


def test_atomic_not_demonstrated():
    assert reduce_expression(
        atomic("Kafka"), _obs(("Kafka", "not_demonstrated")),
    ) == "not_demonstrated"


def test_atomic_contradicted():
    assert reduce_expression(
        atomic("SQL"), _obs(("SQL", "contradicted")),
    ) == "contradicted"


def test_unresolved_node_is_unknown():
    assert reduce_expression(unresolved("ambiguous wording"), {}) == "unknown"


# ---------------------------------------------------------------------------
# AND
# ---------------------------------------------------------------------------

def test_and_all_satisfied():
    expr = group_and(atomic("Python"), atomic("SQL"))
    assert reduce_expression(
        expr, _obs(("Python", "satisfied"), ("SQL", "satisfied")),
    ) == "satisfied"


def test_and_one_not_demonstrated():
    expr = group_and(atomic("Python"), atomic("SQL"))
    assert reduce_expression(
        expr, _obs(("Python", "satisfied"), ("SQL", "not_demonstrated")),
    ) == "partially_supported"


def test_and_all_not_demonstrated():
    expr = group_and(atomic("Python"), atomic("SQL"))
    assert reduce_expression(
        expr, _obs(("Python", "not_demonstrated"), ("SQL", "not_demonstrated")),
    ) == "not_demonstrated"


def test_and_contradiction_blocks_satisfaction():
    expr = group_and(atomic("Python"), atomic("SQL"))
    assert reduce_expression(
        expr, _obs(("Python", "satisfied"), ("SQL", "contradicted")),
    ) == "contradicted"


def test_and_partial_child():
    expr = group_and(atomic("Python"), atomic("SQL"))
    assert reduce_expression(
        expr, _obs(("Python", "satisfied"), ("SQL", "partially_supported")),
    ) == "partially_supported"


def test_and_unknown_child_that_could_change_outcome():
    """Unresolved mandatory child → unknown (could still flip satisfaction)."""
    expr = group_and(atomic("Python"), atomic("SQL"))
    assert reduce_expression(
        expr, _obs(("Python", "satisfied"), ("SQL", "unknown")),
    ) == "unknown"


def test_and_unknown_with_contradiction_stays_contradicted():
    expr = group_and(atomic("Python"), atomic("SQL"))
    assert reduce_expression(
        expr, _obs(("Python", "contradicted"), ("SQL", "unknown")),
    ) == "contradicted"


# ---------------------------------------------------------------------------
# OR
# ---------------------------------------------------------------------------

def test_or_one_complete_alternative():
    expr = group_or(atomic("Python"), atomic("Java"))
    assert reduce_expression(
        expr, _obs(("Python", "satisfied"), ("Java", "not_demonstrated")),
    ) == "satisfied"


def test_or_all_not_demonstrated():
    expr = group_or(atomic("Python"), atomic("Java"))
    assert reduce_expression(
        expr, _obs(("Python", "not_demonstrated"), ("Java", "not_demonstrated")),
    ) == "not_demonstrated"


def test_or_partial_without_complete():
    expr = group_or(atomic("Python"), atomic("Java"))
    assert reduce_expression(
        expr, _obs(("Python", "partially_supported"), ("Java", "not_demonstrated")),
    ) == "partially_supported"


def test_or_unknown_could_change_when_none_satisfied():
    expr = group_or(atomic("Python"), atomic("Java"))
    assert reduce_expression(
        expr, _obs(("Python", "not_demonstrated"), ("Java", "unknown")),
    ) == "unknown"


def test_or_unknown_ignored_when_one_already_satisfied():
    expr = group_or(atomic("Python"), atomic("Java"))
    assert reduce_expression(
        expr, _obs(("Python", "satisfied"), ("Java", "unknown")),
    ) == "satisfied"


def test_or_all_contradicted():
    expr = group_or(atomic("Python"), atomic("Java"))
    assert reduce_expression(
        expr, _obs(("Python", "contradicted"), ("Java", "contradicted")),
    ) == "contradicted"


# ---------------------------------------------------------------------------
# Nested: AND(OR(...), ...)
# ---------------------------------------------------------------------------

def test_python_or_java_plus_sql():
    """JD: Python or Java, plus SQL → AND(OR(Python, Java), SQL)."""
    expr = group_and(
        group_or(atomic("Python"), atomic("Java")),
        atomic("SQL"),
    )
    # Python + SQL → satisfied
    assert reduce_expression(
        expr,
        _obs(("Python", "satisfied"), ("Java", "not_demonstrated"), ("SQL", "satisfied")),
    ) == "satisfied"
    # Only Java, no SQL → partial (OR ok, AND child missing)
    assert reduce_expression(
        expr,
        _obs(("Python", "not_demonstrated"), ("Java", "satisfied"), ("SQL", "not_demonstrated")),
    ) == "partially_supported"
    # Neither language → not_demonstrated for OR, plus SQL missing
    assert reduce_expression(
        expr,
        _obs(
            ("Python", "not_demonstrated"),
            ("Java", "not_demonstrated"),
            ("SQL", "not_demonstrated"),
        ),
    ) == "not_demonstrated"


def test_ambiguous_and_or_stays_unresolved():
    """Wording that cannot be parsed safely stays unresolved → unknown."""
    expr = unresolved("Python or Java and SQL — ambiguous grouping")
    assert reduce_expression(expr, _obs(("Python", "satisfied"), ("SQL", "satisfied"))) == (
        "unknown"
    )


# ---------------------------------------------------------------------------
# Policy helpers
# ---------------------------------------------------------------------------

def test_policy_versions_pinned():
    assert POLICY_VERSION == "assessment-v2"
    assert RUBRIC_VERSION == "assessment-v2"


def test_severity_defaults():
    assert severity_for("absence") == "material"
    assert severity_for("relationship") == "critical"
    assert severity_for("absence", override="advisory") == "advisory"


def test_evidence_expectations():
    assert evidence_expectation_ok("listed_skill_ok")
    assert evidence_expectation_ok("production_use")
    assert not evidence_expectation_ok("invented_surface")


# ---------------------------------------------------------------------------
# Exhaustive small truth grid for two-child AND/OR
# ---------------------------------------------------------------------------

_STATES = (
    "satisfied",
    "partially_supported",
    "not_demonstrated",
    "contradicted",
    "unknown",
)


@pytest.mark.parametrize("left", _STATES)
@pytest.mark.parametrize("right", _STATES)
def test_and_truth_table_deterministic(left, right):
    expr = group_and(atomic("A"), atomic("B"))
    out = reduce_expression(expr, _obs(("A", left), ("B", right)))
    assert out in _STATES
    # Invariants
    if left == "contradicted" or right == "contradicted":
        assert out == "contradicted"
    elif left == "satisfied" and right == "satisfied":
        assert out == "satisfied"
    elif "unknown" in (left, right):
        assert out == "unknown"
    elif left == "not_demonstrated" and right == "not_demonstrated":
        assert out == "not_demonstrated"


@pytest.mark.parametrize("left", _STATES)
@pytest.mark.parametrize("right", _STATES)
def test_or_truth_table_deterministic(left, right):
    expr = group_or(atomic("A"), atomic("B"))
    out = reduce_expression(expr, _obs(("A", left), ("B", right)))
    assert out in _STATES
    if left == "satisfied" or right == "satisfied":
        assert out == "satisfied"
    elif left == "unknown" or right == "unknown":
        assert out == "unknown"
    elif left == "partially_supported" or right == "partially_supported":
        assert out == "partially_supported"
    elif left == "not_demonstrated" and right == "not_demonstrated":
        assert out == "not_demonstrated"
    elif left == "contradicted" and right == "contradicted":
        assert out == "contradicted"
