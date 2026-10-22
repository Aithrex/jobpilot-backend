"""identity-1.0.0: rows merge on PROVEN identity only.

Regressions taken from the 2026-10-21 Jobright capture
(docs/audit/jobright-2026-10-21/jobright-postings-reviewed.json). The capture
is competitor evidence, not JobPilot inventory; only URL shapes and titles are
reused here.
"""
from __future__ import annotations

import pytest

from services import job_inventory_service as inv
from services import posting_identity as pid

OPENAI_A = ("https://jobs.ashbyhq.com/openai/e6d5ca02-f30b-4ac5-a69d-c947efb430f9/"
            "application?jr_id=6a0b91a04d93203636872688")
OPENAI_B = ("https://jobs.ashbyhq.com/openai/e6d5ca02-f30b-4ac5-a69d-c947efb430f9/"
            "application?jr_id=68d1efd417554c2d9eee164b")
SCALENCE = ("https://www1.jobdiva.com/portal/?a=aljdnw0qolq9pzanc6e7lzw46uuoh200575bah9r6xf"
            "g5grgtpharu4z9rqj9436&jr_id={jr}#/jobs/332")
MS_REDMOND = ("https://apply.careers.microsoft.com/careers/job/1970393557039399"
              "?domain=microsoft.com&src=jobright&jr_id=6ad8ab1867bec1d0d9c7e728")
MS_CAMBRIDGE = "https://apply.careers.microsoft.com/careers/job/1970393557038974/?jr_id=6ad2bc67810010ec72f59b77"


class TestApplyUrlKeyMirrorsFrontend:
    """Same cases as jobpilot-frontend src/lib/__tests__/careerJobs.test.ts."""

    def test_drops_tracking_and_apply_suffix(self):
        assert pid.apply_url_key("https://ats.rippling.com/acme/jobs/5f1c?utm_source=x&src=li") \
            == pid.apply_url_key("https://ats.rippling.com/acme/jobs/5f1c/")
        assert pid.apply_url_key("https://jobs.lever.co/acme/abc/apply?lever-source=LinkedIn") \
            == pid.apply_url_key("https://jobs.lever.co/acme/abc")

    def test_functional_identifiers_are_preserved(self):
        assert pid.apply_url_key("https://acme.com/careers?gh_jid=1") \
            != pid.apply_url_key("https://acme.com/careers?gh_jid=2")
        assert pid.apply_url_key("https://jobs.ashbyhq.com/acme?ashby_jid=a") \
            != pid.apply_url_key("https://jobs.ashbyhq.com/acme")

    def test_generic_portal_does_not_equal_a_specific_job(self):
        assert pid.apply_url_key("https://portal.jobdiva.com/jobs?a=1") \
            != pid.apply_url_key("https://portal.jobdiva.com/jobs?a=1&jobid=99")

    @pytest.mark.parametrize("bad", ["", None, "mailto:jobs@acme.com", "not a url",
                                     "/relative/path"])
    def test_garbage_never_matches(self, bad):
        assert pid.apply_url_key(bad) == ""

    def test_output_format_matches_frontend(self):
        # host (no www, lower) + path (no trailing slash) + sorted kept query
        assert pid.apply_url_key("https://WWW.Acme.com/jobs/42/?b=2&a=1&utm_medium=x") \
            == "acme.com/jobs/42?a=1&b=2"


class TestGenericPortals:
    @pytest.mark.parametrize("url", [
        SCALENCE.format(jr="x"),
        "https://boards.greenhouse.io/acme",
        "https://jobs.lever.co/acme/",
        "https://jobs.ashbyhq.com/openai",
        "https://acme.wd5.myworkdayjobs.com/en-US/External",
        "https://acme.com/careers",
        "https://www.amazon.jobs/en/",
        "https://recruiting.ultipro.com/ROP1001ROPER/JobBoard/cf088029-453b-46f8-bcab-46ffb10fdf54/",
    ])
    def test_landing_pages_are_not_keys(self, url):
        assert pid.apply_url_key(url) == "", url

    @pytest.mark.parametrize("url", [
        "https://boards.greenhouse.io/acme/jobs/42",
        "https://job-boards.greenhouse.io/embed/job_app?for=canonical&token=2969042",
        "https://acme.wd5.myworkdayjobs.com/en-US/CareerDepot/job/Atlanta-GA/SWE_Req185723",
        "https://recruiting.myapps.paychex.com/appone/MainInfoReq.asp?R_ID=7157769",
        "https://simeio.applytojob.com/apply/LlSUxJBlJq/FullStack-Software-Engineer",
        "https://www.linkedin.com/jobs/view/4481153600",
        MS_REDMOND,
    ])
    def test_job_pages_are_keys(self, url):
        assert pid.apply_url_key(url) != "", url


class TestCanonicalUrlKeyKeepsJobIds:
    def test_gh_jid_is_a_job_id_not_tracking(self):
        """Every job on an employer-hosted Greenhouse board shared one key."""
        assert inv.canonical_url_key("https://stripe.com/jobs/search?gh_jid=111") \
            != inv.canonical_url_key("https://stripe.com/jobs/search?gh_jid=222")

    def test_jobright_click_id_is_tracking(self):
        assert inv.canonical_url_key(OPENAI_A) == inv.canonical_url_key(OPENAI_B) != ""

    def test_dedupe_groups_differ_for_distinct_gh_jids(self):
        a = inv.compute_dedupe_identity(
            employer_id="stripe", ats="greenhouse", source="career-pages",
            source_job_id="gh-stripe:111", content_hash="c1", title_normalized="SWE",
            canonical_url="https://stripe.com/jobs/search?gh_jid=111")
        b = inv.compute_dedupe_identity(
            employer_id="stripe", ats="greenhouse", source="career-pages",
            source_job_id="gh-stripe:222", content_hash="c2", title_normalized="SWE",
            canonical_url="https://stripe.com/jobs/search?gh_jid=222")
        assert a["dedupe_group_id"] != b["dedupe_group_id"]

    def test_generic_portal_falls_back_to_the_requisition(self):
        common = dict(employer_id="scalence", ats="jobdiva", source="career-pages",
                      content_hash="", title_normalized="x",
                      canonical_url=SCALENCE.format(jr="1"))
        a = inv.compute_dedupe_identity(source_job_id="jd:1", **common)
        b = inv.compute_dedupe_identity(source_job_id="jd:2", **common)
        assert a["dedupe_group_id"] and a["dedupe_group_id"] != b["dedupe_group_id"]


def _card(company, title, location, url, **kw):
    row = {"company": company, "title": title, "location": location,
           "original_job_url": url, "job_id": kw.pop("job_id", url)}
    row.update(kw)
    return row


class TestCaptureRegressions:
    def test_two_openai_cards_sharing_an_ashby_uuid_are_one_group(self):
        rows = [_card("OpenAI", "Software Engineer, Research Developer Productivity",
                      "San Francisco, CA", OPENAI_A, job_id="jr-1"),
                _card("OpenAI", "Software Engineer, Research Developer Productivity",
                      "San Francisco, CA", OPENAI_B, job_id="jr-2")]
        g = pid.group_rows(rows)
        assert g["group_of"][0] == g["group_of"][1]
        assert g["basis"][1] == "url"

    def test_three_scalence_titles_behind_one_jobdiva_portal_are_not_merged(self):
        rows = [_card("Scalence L.L.C.", t, "United States", SCALENCE.format(jr=i),
                      job_id=f"jr-{i}")
                for i, t in enumerate(["Software Engineer I", "Site Reliability Engineer II",
                                       "Python+Data Engineer"])]
        g = pid.group_rows(rows)
        assert len(set(g["group_of"])) == 3

    def test_microsoft_software_engineer_cards_in_different_places_are_not_merged(self):
        rows = [_card("Microsoft", "Software Engineer", "Redmond, WA", MS_REDMOND, job_id="a"),
                _card("Microsoft", "Software Engineer", "Cambridge, MA", MS_CAMBRIDGE,
                      job_id="b")]
        g = pid.group_rows(rows)
        assert g["group_of"][0] != g["group_of"][1]
        assert g["possible_duplicate_of"] == [None, None]

    def test_same_company_title_and_city_is_only_a_possible_duplicate(self):
        rows = [_card("TechSur Solutions", "Full Stack Java Developer", "Reston, VA",
                      "https://jobs.smartrecruiters.com/TechSurSolutions/3743990016311646-fsjd",
                      job_id="a"),
                _card("TechSur Solutions", "Full Stack Java Developer", "Reston, VA",
                      "https://www.linkedin.com/jobs/view/4479432623", job_id="b")]
        g = pid.group_rows(rows)
        assert g["group_of"][0] != g["group_of"][1]
        assert g["possible_duplicate_of"][1] == 0

    def test_same_requisition_on_one_tenant_merges_across_employer_names(self):
        rows = [{"company": "JPMorgan Chase", "title": "SWE", "ats": "oracle",
                 "tenant": "jpmc", "source_job_id": "orc-jpmc-cx1:210786394", "job_id": "a"},
                {"company": "NUTMEG CONTAINER CORPORATION", "title": "SWE", "ats": "oracle",
                 "tenant": "jpmc", "source_job_id": "orc-jpmc-cx2:210786394", "job_id": "b"}]
        g = pid.group_rows(rows)
        assert g["group_of"][0] == g["group_of"][1]
        assert g["basis"][1] == "req"

    def test_a_shared_url_fronting_different_titles_is_vetoed(self):
        url = "https://acme.com/careers/apply-now-123"
        rows = [_card("Acme", "Data Engineer", "NYC", url, job_id="a"),
                _card("Acme", "Payroll Specialist", "NYC", url, job_id="b")]
        g = pid.group_rows(rows)
        assert g["group_of"][0] != g["group_of"][1]


class TestServedDedupe:
    """The real served collapse: `inventory_feed._dedupe` (used by the tech
    feed and the inventory tabs)."""

    def _feed(self):
        from services import inventory_feed
        return inventory_feed

    def test_openai_cards_sharing_an_ashby_uuid_collapse(self):
        rows = [{"job_id": "a", "company": "OpenAI", "title": "SWE, Research Dev Productivity",
                 "location": "San Francisco, CA", "url": OPENAI_A, "days_ago": 1},
                {"job_id": "b", "company": "OpenAI", "title": "SWE, Research Dev Productivity",
                 "location": "San Francisco, CA", "url": OPENAI_B, "days_ago": 0}]
        out = self._feed()._dedupe(rows)
        assert len(out) == 1 and out[0]["duplicate_count"] == 1

    def test_scalence_portal_cards_stay_separate(self):
        rows = [{"job_id": f"s{i}", "company": "Scalence L.L.C.", "title": t,
                 "location": "United States", "url": SCALENCE.format(jr=i)}
                for i, t in enumerate(["Software Engineer I", "Site Reliability Engineer II",
                                       "Python+Data Engineer"])]
        assert len(self._feed()._dedupe(rows)) == 3

    def test_microsoft_cards_stay_separate_and_unflagged(self):
        rows = [{"job_id": "r", "company": "Microsoft", "title": "Software Engineer",
                 "location": "Redmond, WA", "url": MS_REDMOND},
                {"job_id": "c", "company": "Microsoft", "title": "Software Engineer",
                 "location": "Cambridge, MA", "url": MS_CAMBRIDGE}]
        out = self._feed()._dedupe(rows)
        assert len(out) == 2
        assert not any(r.get("possible_duplicate_of") for r in out)
