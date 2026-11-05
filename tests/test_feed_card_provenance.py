"""Cards say when the crawler first saw them and whether their score is
provisional (2026-11-05): "New since your last visit" compares `first_seen_at`
with the snapshot's `seen_through`, and a card whose description is not read
yet says its score leans on the title."""
from datetime import datetime, timezone

from services import inventory_feed as feed

SEEN = datetime(2026, 11, 4, 15, 30)  # naive, as Mongo hands it back


def _doc(**over):
    base = {"job_id": "j1", "title_raw": "Backend Engineer", "employer_name": "Acme",
            "tenant": "acme", "source_job_id": "workday-acme-ext:JR1", "ats": "workday",
            "source": "workday", "locations": [{"raw": "Austin, TX", "key": "austin-tx",
                                                "country_code": "US"}],
            "posted_at": datetime(2026, 11, 4, tzinfo=timezone.utc), "first_seen_at": SEEN,
            "canonical_url": "https://acme.example/job/1",
            "requirements": {"required_skills": ["python"], "extractor_version": 14},
            "body_state": "full", "description_ref": "s3://bodies/abc", "description_chars": 5200}
    base.update(over)
    return base


def _card(doc):
    return feed._row_from_posting(doc, tz_offset=-420)


def test_first_seen_is_an_aware_iso_timestamp():
    assert _card(_doc())["first_seen_at"] == SEEN.replace(tzinfo=timezone.utc).isoformat()


def test_a_read_description_is_not_provisional():
    assert "score_basis" not in _card(_doc())


def test_a_teaser_a_missing_body_or_no_profile_is_title_only():
    assert _card(_doc(body_state="teaser"))["score_basis"] == "title_only"
    assert _card(_doc(description_ref="", description_chars=130))["score_basis"] == "title_only"
    assert _card(_doc(requirements=None))["score_basis"] == "title_only"


def test_an_inline_body_without_an_offload_ref_still_counts():
    assert "score_basis" not in _card(_doc(description_ref="", description_chars=900))


def test_the_card_projection_carries_body_state():
    from services import job_inventory_service as inv
    assert inv._CARD_PROJECTION.get("body_state") == 1
