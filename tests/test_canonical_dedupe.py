"""P0: canonical job identity, and dedupe that preserves provenance.

The feed deduped CLIENT-SIDE on `(employer_id_of(company), title_key(title),
location_key)` recomputed from DISPLAY strings, and it deleted the losers. Two
consequences, both measurable on live inventory:

* the same requisition reached through two sources was not merged when the
  employer string differed, and WAS merged when two genuinely different reqs
  shortened to the same title;
* whichever copy sorted second vanished, taking its source, its ATS and its
  city with it - so a role open in Austin and Denver showed one city and no
  indication there was another.

Identity is now computed ONCE at ingest (`compute_dedupe_identity`) from the
strongest signal available, and the collapsed rows survive as
`source_variants`.
"""
from __future__ import annotations

import pytest

from services import inventory_feed as feed
from services import job_inventory_service as inv


# ---------------------------------------------------------------------------
# canonical_url_key
# ---------------------------------------------------------------------------

class TestCanonicalUrlKey:
    @pytest.mark.parametrize("a, b, why", [
        ("https://boards.greenhouse.io/acme/jobs/42",
         "https://boards.greenhouse.io/acme/jobs/42/", "trailing slash"),
        ("https://boards.greenhouse.io/acme/jobs/42",
         "http://boards.greenhouse.io/acme/jobs/42", "scheme"),
        ("https://boards.greenhouse.io/acme/jobs/42",
         "https://WWW.Boards.Greenhouse.IO/acme/jobs/42", "host case + www"),
        ("https://boards.greenhouse.io/acme/jobs/42",
         "https://boards.greenhouse.io/acme/jobs/42?utm_source=linkedin&gh_src=x",
         "tracking parameters"),
        ("https://x.com/j?b=2&a=1", "https://x.com/j?a=1&b=2", "param order"),
    ])
    def test_equivalent_urls_share_a_key(self, a, b, why):
        assert inv.canonical_url_key(a) == inv.canonical_url_key(b), why

    def test_meaningful_params_are_kept(self):
        assert inv.canonical_url_key("https://x.com/j?id=1") != \
               inv.canonical_url_key("https://x.com/j?id=2")

    @pytest.mark.parametrize("bad", ["", None, "not a url", "/relative/path"])
    def test_unusable_input_returns_empty_never_a_partial_key(self, bad):
        assert inv.canonical_url_key(bad) == ""


# ---------------------------------------------------------------------------
# compute_dedupe_identity
# ---------------------------------------------------------------------------

def _identity(**kw):
    base = dict(employer_id="acme", ats="greenhouse", source="career-pages",
                source_job_id="42",
                canonical_url="https://boards.greenhouse.io/acme/jobs/42",
                content_hash="c0ffee", title_normalized="Backend Engineer")
    base.update(kw)
    return inv.compute_dedupe_identity(**base)


class TestDedupeIdentity:
    def test_url_is_the_strongest_signal(self):
        """Same requisition URL through two SOURCES is one posting."""
        a = _identity(source="career-pages", ats="greenhouse", source_job_id="42")
        b = _identity(source="jsearch", ats="", source_job_id="zz-999")
        assert a["dedupe_group_id"] == b["dedupe_group_id"]

    def test_requisition_identity_is_used_when_there_is_no_url(self):
        a = _identity(canonical_url="")
        b = _identity(canonical_url="", content_hash="different")
        assert a["dedupe_group_id"] == b["dedupe_group_id"] != ""

    def test_content_hash_is_a_review_hint_never_a_merge(self):
        """identity-1.0.0: content similarity is supporting evidence only."""
        a = _identity(canonical_url="", source_job_id="")
        b = _identity(canonical_url="", source_job_id="", ats="lever")
        assert a["dedupe_group_id"] == b["dedupe_group_id"] == ""
        assert a["possible_duplicate_group_id"] == b["possible_duplicate_group_id"] != ""

    def test_no_identity_at_all_yields_no_group(self):
        got = _identity(canonical_url="", source_job_id="", content_hash="")
        assert got["dedupe_group_id"] == ""

    def test_different_requisitions_do_not_collide(self):
        assert _identity(source_job_id="42", canonical_url="")["dedupe_group_id"] \
            != _identity(source_job_id="43", canonical_url="")["dedupe_group_id"]

    def test_same_employer_different_employer_id_does_not_collide(self):
        assert _identity(employer_id="acme", canonical_url="")["dedupe_group_id"] \
            != _identity(employer_id="other", canonical_url="")["dedupe_group_id"]

    @pytest.mark.parametrize("a, b", [
        ("Backend Engineer", "Backend Engineer (Remote)"),
        ("Backend Engineer", "Backend Engineer - Req 12345"),
        ("Backend Engineer", "Backend Engineer [R-99881]"),
    ])
    def test_variant_group_ignores_title_decoration(self, a, b):
        assert _identity(title_normalized=a)["variant_group_id"] == \
               _identity(title_normalized=b)["variant_group_id"]

    def test_variant_group_separates_real_roles(self):
        assert _identity(title_normalized="Backend Engineer")["variant_group_id"] != \
               _identity(title_normalized="Frontend Engineer")["variant_group_id"]


# ---------------------------------------------------------------------------
# the feed's dedupe
# ---------------------------------------------------------------------------

def _row(company, title, location, *, days_ago=1, group=None, source="Greenhouse",
         ats="greenhouse", board_kind=None, job_id=None):
    r = {"company": company, "title": title, "location": location,
         "days_ago": days_ago, "source": source, "ats": ats,
         "url": f"https://x.example/{(job_id or title)}",
         "job_id": job_id or f"{company}|{title}|{days_ago}"}
    if group:
        r["dedupe_group_id"] = group
    if board_kind:
        r["board_kind"] = board_kind
    return r


class TestFeedDedupe:
    def test_stored_identity_merges_rows_display_strings_would_not(self):
        """The measured miss: same req, two sources, different company string."""
        out = feed._dedupe([
            _row("Google LLC", "Software Engineer", "New York, NY",
                 days_ago=9, group="g1", source="Greenhouse"),
            _row("Google", "SWE II", "Mountain View, CA",
                 days_ago=1, group="g1", source="Lever"),
        ])
        assert len(out) == 1
        assert out[0]["days_ago"] == 1

    def test_the_collapsed_copy_survives_as_provenance(self):
        out = feed._dedupe([
            _row("Acme", "Backend Engineer", "Austin, TX", days_ago=1, group="g1"),
            _row("Acme", "Backend Engineer", "Denver, CO", days_ago=3, group="g1"),
        ])
        assert len(out) == 1
        variants = out[0]["source_variants"]
        assert out[0]["duplicate_count"] == 1
        assert variants[0]["location"] == "Denver, CO"
        assert variants[0]["job_id"]

    def test_a_direct_employer_copy_outranks_a_marketplace_copy(self):
        """Even when the marketplace copy is fresher: source quality is the
        rule the rest of the feed already sorts by."""
        out = feed._dedupe([
            _row("Acme", "Backend Engineer", "Austin, TX", days_ago=5,
                 group="g1", source="Greenhouse"),
            _row("Acme", "Backend Engineer", "Austin, TX", days_ago=0,
                 group="g1", source="Jobgether", board_kind="marketplace"),
        ])
        assert len(out) == 1
        assert out[0]["source"] == "Greenhouse"
        assert out[0]["source_variants"][0]["source"] == "Jobgether"

    def test_distinct_groups_are_never_merged(self):
        out = feed._dedupe([
            _row("Acme", "Backend Engineer", "Austin, TX", group="g1"),
            _row("Acme", "Frontend Engineer", "Austin, TX", group="g2"),
        ])
        assert len(out) == 2

    def test_rows_without_a_stored_id_keep_the_legacy_rule(self):
        """227,914 live rows predate `dedupe_group_id`. Until the backfill
        lands they must still collapse, or shipping this shows every duplicate
        the feed currently hides."""
        out = feed._dedupe([
            _row("Google LLC", "Software Engineer", "New York, NY", days_ago=9),
            _row("Google", "Software Engineer (Remote)", "New York, NY", days_ago=1),
        ])
        assert len(out) == 1
        assert out[0]["days_ago"] == 1

    def test_mixed_stored_and_legacy_rows_coexist(self):
        out = feed._dedupe([
            _row("Acme", "Backend Engineer", "Austin, TX", group="g1"),
            _row("Beta Co", "Data Engineer", "Denver, CO"),
        ])
        assert len(out) == 2

    def test_order_of_first_appearance_is_preserved(self):
        out = feed._dedupe([
            _row("A", "Backend Engineer", "X", group="g1"),
            _row("B", "Frontend Engineer", "Y", group="g2"),
            _row("A", "Backend Engineer", "Z", group="g1", days_ago=0),
        ])
        assert [r["company"] for r in out] == ["A", "B"]

    def test_no_variants_key_when_nothing_collapsed(self):
        out = feed._dedupe([_row("A", "Backend Engineer", "X", group="g1")])
        assert "source_variants" not in out[0]
        assert "duplicate_count" not in out[0]
