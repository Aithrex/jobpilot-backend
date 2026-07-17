"""Own-edit learning signals (Craft-overhaul Part B1).

Covers the firewall (user A never sees user B's edits), the diff logic
(reworded bullets surface, verbatim/new bullets don't), user_feedback capture,
and fail-open behavior. Uses the mongomock `mock_db` fixture — no Bedrock.
"""
import pytest

from services import edit_signals
from services.edit_signals import past_edits_context, _match_reworded_pairs


@pytest.fixture(autouse=True)
def _clean_records(mock_db):
    # mock_db only auto-clears the voice-graph collections; this feature uses
    # tailoring_records, so wipe it before each test for isolation.
    mock_db.tailoring_records.delete_many({})
    yield


def _record(email, parent_bullets, edited_bullets, feedback=None, deleted=False):
    """A tailoring_records doc with one 'initial' + one 'edited' version."""
    parent = {
        "version_id": "v1", "version_number": 1, "source": "initial",
        "parent_version_id": None,
        "tailored_resume": {"experience": [{"company": "Acme", "title": "SWE",
                                            "bullets": list(parent_bullets)}]},
    }
    edited = {
        "version_id": "v2", "version_number": 2, "source": "edited",
        "parent_version_id": "v1", "user_feedback": feedback,
        "tailored_resume": {"experience": [{"company": "Acme", "title": "SWE",
                                            "bullets": list(edited_bullets)}]},
    }
    return {
        "user_email": email, "deleted": deleted,
        "versions": [parent, edited], "current_version_id": "v2",
    }


def test_reworded_bullet_surfaces(mock_db):
    mock_db.tailoring_records.insert_one(_record(
        "a@x.com",
        ["Built the payments pipeline in Python to process transactions."],
        ["Engineered a Python payments pipeline processing transactions daily."],
    ))
    block = past_edits_context("a@x.com")
    assert "rephrased" in block
    assert "Engineered a Python payments pipeline" in block


def test_verbatim_and_new_bullets_do_not_surface(mock_db):
    mock_db.tailoring_records.insert_one(_record(
        "a@x.com",
        ["Built the payments pipeline in Python to process transactions."],
        [
            "Built the payments pipeline in Python to process transactions.",  # identical
            "Launched an entirely unrelated data-warehouse migration effort.",  # new
        ],
    ))
    block = past_edits_context("a@x.com")
    # No reword pair → no "rephrased" line (block may still be empty).
    assert "rephrased" not in block


def test_user_feedback_is_captured(mock_db):
    mock_db.tailoring_records.insert_one(_record(
        "a@x.com",
        ["Built the payments pipeline in Python to process transactions."],
        ["Shipped a Python payments pipeline handling transactions at scale."],
        feedback="Make the bullets more concise and outcome-focused.",
    ))
    block = past_edits_context("a@x.com")
    assert "more concise and outcome-focused" in block


def test_firewall_user_a_never_sees_user_b(mock_db):
    # User B has edits; user A has none. A must get an empty block — never B's.
    mock_db.tailoring_records.insert_one(_record(
        "b@x.com",
        ["Built the payments pipeline in Python to process transactions."],
        ["Engineered a Python payments pipeline processing transactions daily."],
    ))
    block_for_a = past_edits_context("a@x.com")
    assert block_for_a == ""
    assert "payments pipeline" not in block_for_a  # none of B's text leaked


def test_soft_deleted_records_are_skipped(mock_db):
    mock_db.tailoring_records.insert_one(_record(
        "a@x.com",
        ["Built the payments pipeline in Python to process transactions."],
        ["Engineered a Python payments pipeline processing transactions daily."],
        deleted=True,
    ))
    assert past_edits_context("a@x.com") == ""


def test_empty_or_bad_email_returns_empty(mock_db):
    assert past_edits_context("") == ""
    assert past_edits_context("not-an-email") == ""


def test_read_failure_fails_open(monkeypatch):
    # If the DB read raises, degrade to "" — never break tailoring.
    def _boom():
        raise RuntimeError("mongo down")
    monkeypatch.setattr(edit_signals, "_db", _boom)
    assert past_edits_context("a@x.com") == ""


# --- Pure diff-logic unit tests (no DB) ---

def test_match_reworded_pairs_bands():
    parent = ["Built the payments pipeline in Python to process transactions."]
    # reworded (same idea, different words) → a pair
    edited_reword = ["Engineered a Python payments pipeline processing transactions."]
    assert _match_reworded_pairs(parent, edited_reword)
    # identical → no pair (above the identical threshold)
    assert not _match_reworded_pairs(parent, list(parent))
    # unrelated → no pair (below the reword floor)
    assert not _match_reworded_pairs(parent, ["Organized the annual company retreat."])
