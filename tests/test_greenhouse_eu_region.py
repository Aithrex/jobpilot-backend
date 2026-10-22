"""Greenhouse EU region: identity, listing, pagination, detail — from fixtures.

The fixtures under `tests/fixtures/greenhouse_eu/` are TRIMMED captures of
Cognite's public EU board taken 2026-10-21 (`job-boards.eu.greenhouse.io/
cognite`, `?page=2`, `?page=3`, `/jobs/4969944101`). Real field names and real
values; the row lists were cut to 3 + 1 rows and the counts edited to agree
(total 4, two pages). What the capture established about the region:

  * `boards-api.eu.greenhouse.io` does not resolve, and `api.eu.greenhouse.io`
    is the recruiter app (302 to sign-in) — there is no public EU JSON API;
  * the public board page carries the listing in `window.__remixContext` as
    `jobPosts {count, page, total, total_pages, data[]}`, paged with `?page=N`;
  * a past-the-end page answers 200 with `data: []`.
"""
from pathlib import Path

import pytest

from services.ats_providers import registry
from services.ats_providers.base import ChallengeError, Endpoint

FIX = Path(__file__).resolve().parent / "fixtures" / "greenhouse_eu"
EU = "https://job-boards.eu.greenhouse.io"


def _fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class _Ctx:
    def __init__(self, pages=None, fail=None):
        self.pages = pages or {}
        self.fail = fail or {}
        self.calls = []
        self.cutoff_iso = None
        self.listing_truncated = False
        self.options = {}

    def should_stop(self):
        return False

    def log(self, *a, **k):
        pass

    def _key(self, url, params):
        page = (params or {}).get("page")
        return f"{url}?page={page}" if page else url

    def get_text(self, url, params=None, timeout=None, vendor="", **kw):
        key = self._key(url, params)
        self.calls.append(key)
        if key in self.fail:
            return self.fail[key], ""
        if key in self.pages:
            return 200, self.pages[key]
        return 404, "<html>not found</html>"

    def get_json(self, url, params=None, timeout=None, vendor="", **kw):
        self.calls.append(url)
        raise AssertionError(f"EU endpoint must not call a JSON API: {url}")


@pytest.fixture
def gh():
    return registry.get("greenhouse")


@pytest.fixture
def eu_ep():
    return Endpoint(vendor="greenhouse", tenant="cognite", site="eu")


def _board_pages():
    return {f"{EU}/cognite": _fx("cognite_board_page1.html"),
            f"{EU}/cognite?page=2": _fx("cognite_board_page2.html"),
            f"{EU}/cognite?page=3": _fx("cognite_board_page3_empty.html")}


# -- identity ---------------------------------------------------------------

def test_eu_board_url_fingerprints_to_the_eu_region(gh):
    for url in ("https://job-boards.eu.greenhouse.io/cognite",
                "https://job-boards.eu.greenhouse.io/cognite/jobs/4969944101",
                "https://boards.eu.greenhouse.io/embed/job_board?for=cognite"):
        ep = None
        for f in gh.fingerprints:
            fields = f.match(url)
            if fields:
                ep = gh.endpoint_from_match(fields, url)
                break
        assert ep is not None, url
        assert (ep.tenant, ep.site) == ("cognite", "eu"), url
        assert ep.member_key == "greenhouse-cognite-eu"


def test_us_board_is_unchanged(gh):
    url = "https://job-boards.greenhouse.io/ascentds"
    fields = next(f.match(url) for f in gh.fingerprints if f.match(url))
    ep = gh.endpoint_from_match(fields, url)
    assert (ep.tenant, ep.site) == ("ascentds", "")
    assert ep.member_key == "greenhouse-ascentds"


def test_board_url_is_region_specific(gh, eu_ep):
    assert gh.board_url(eu_ep) == f"{EU}/cognite"
    assert gh.board_url(Endpoint(vendor="greenhouse", tenant="hover")) == \
        "https://job-boards.greenhouse.io/hover"
    assert gh.board_url(Endpoint(vendor="greenhouse", tenant="x", site="apac")) == ""


def test_eu_hosts_are_allowlisted_and_bounded(gh):
    assert "job-boards.eu.greenhouse.io" in gh.api_hosts
    # Named hosts only — no suffix wildcard for the region.
    assert not any(h.startswith(".") for h in gh.api_hosts)


# -- listing ----------------------------------------------------------------

def test_eu_list_pages_to_the_boards_own_end(gh, eu_ep):
    ctx = _Ctx(_board_pages())
    rows = list(gh.list_jobs(eu_ep, ctx))
    assert [r.source_job_id for r in rows] == [
        "4908731101", "4960573101", "4920585101", "4986174101"]
    assert ctx.calls == [f"{EU}/cognite", f"{EU}/cognite?page=2"]
    assert not ctx.listing_truncated
    first = rows[0]
    assert first.apply_url == f"{EU}/cognite/jobs/4908731101"
    assert first.location == "USA (remote)"
    assert first.posted_at.startswith("2026-08-20")
    assert first.date_confidence == "exact"
    assert first.detail_url == f"{EU}/cognite/jobs/4908731101"
    assert all("boards-api" not in c for c in ctx.calls)


def test_eu_list_never_queries_the_us_region(gh, eu_ep):
    ctx = _Ctx(_board_pages())
    list(gh.list_jobs(eu_ep, ctx))
    assert all(c.startswith(EU) for c in ctx.calls)


def test_eu_duplicate_across_pages_is_yielded_once(gh, eu_ep):
    pages = _board_pages()
    # Page 2 repeats a page-1 row (a board that shifted while we paged).
    pages[f"{EU}/cognite?page=2"] = pages[f"{EU}/cognite?page=2"].replace(
        "4986174101", "4908731101")
    rows = list(gh.list_jobs(eu_ep, _Ctx(pages)))
    ids = [r.source_job_id for r in rows]
    assert len(ids) == len(set(ids)) == 3


def test_eu_failed_later_page_is_truncation_not_census(gh, eu_ep):
    pages = _board_pages()
    del pages[f"{EU}/cognite?page=2"]
    ctx = _Ctx(pages, fail={f"{EU}/cognite?page=2": 500})
    rows = list(gh.list_jobs(eu_ep, ctx))
    assert len(rows) == 3
    assert ctx.listing_truncated is True


def test_eu_page_cap_is_truncation(gh, eu_ep, monkeypatch):
    monkeypatch.setattr(type(gh), "EU_MAX_PAGES", 1)
    ctx = _Ctx(_board_pages())
    rows = list(gh.list_jobs(eu_ep, ctx))
    assert len(rows) == 3 and ctx.listing_truncated is True


def test_eu_schema_change_yields_fewer_rows_not_a_crash(gh, eu_ep):
    page = _fx("cognite_board_page1.html").replace('"jobPosts"', '"jobPostings"')
    ctx = _Ctx({f"{EU}/cognite": page})
    assert list(gh.list_jobs(eu_ep, ctx)) == []
    garbage = _Ctx({f"{EU}/cognite": "<script>window.__remixContext = {not json"})
    assert list(gh.list_jobs(eu_ep, garbage)) == []


def test_eu_row_missing_fields_is_skipped(gh, eu_ep):
    page = _fx("cognite_board_page1.html").replace(
        '"title": "Principal Software Engineer"', '"title": ""')
    pages = _board_pages()
    pages[f"{EU}/cognite"] = page
    rows = list(gh.list_jobs(eu_ep, _Ctx(pages)))
    assert "4960573101" not in [r.source_job_id for r in rows]
    assert len(rows) == 3


def test_eu_missing_board_is_empty_not_error(gh):
    ep = Endpoint(vendor="greenhouse", tenant="no-such-board", site="eu")
    assert list(gh.list_jobs(ep, _Ctx({}))) == []


def test_eu_challenge_raises(gh, eu_ep):
    ctx = _Ctx({}, fail={f"{EU}/cognite": 429})
    with pytest.raises(ChallengeError):
        list(gh.list_jobs(eu_ep, ctx))


def test_unknown_region_is_refused_not_sent_to_us(gh):
    ep = Endpoint(vendor="greenhouse", tenant="cognite", site="apac")
    ctx = _Ctx({})
    assert list(gh.list_jobs(ep, ctx)) == []
    assert ctx.calls == []


def test_eu_board_owner_comes_from_the_board(gh, eu_ep):
    assert gh.board_owner(eu_ep, _Ctx(_board_pages())) == "Cognite - AI for Industry"


# -- detail -----------------------------------------------------------------

def test_eu_detail_reads_the_job_post_content(gh, eu_ep):
    from services.ats_providers.base import RawJob
    job = RawJob(source_job_id="4969944101", title="Academy Engineer",
                 apply_url=f"{EU}/cognite/jobs/4969944101",
                 detail_url=f"{EU}/cognite/jobs/4969944101")
    ctx = _Ctx({f"{EU}/cognite/jobs/4969944101": _fx("cognite_job_4969944101.html")})
    out = gh.fetch_detail(eu_ep, job, ctx)
    assert out is not None and len(out.description) > 500
    assert "Ownership" in out.description
    assert "<" not in out.description[:200]


def test_eu_detail_refuses_a_foreign_detail_ref(gh, eu_ep):
    from services.ats_providers.base import RawJob
    job = RawJob(source_job_id="1", title="x", apply_url="",
                 detail_url="https://boards-api.greenhouse.io/v1/boards/cognite/jobs/1")
    ctx = _Ctx({})
    assert gh.fetch_detail(eu_ep, job, ctx) is None
    assert ctx.calls == []


# -- the crawler routes a regional member through the provider --------------

def test_regional_member_bypasses_the_us_legacy_path(monkeypatch):
    from services import job_ingest_worker as jw

    member = jw._endpoint_member("greenhouse", tenant="cognite", site="eu",
                                 display_name="Cognite")
    us_member = jw._endpoint_member("greenhouse", tenant="hover")
    assert jw._is_regional_member(member)
    assert not jw._uses_legacy_listing(member)
    assert jw._uses_legacy_listing(us_member)

    called = {}

    def _legacy(*a, **k):
        raise AssertionError("regional member reached the US legacy path")

    monkeypatch.setattr(jw, "_list_career", _legacy)
    monkeypatch.setattr(jw, "_list_via_provider",
                        lambda m, **k: called.setdefault("provider", []) or [])
    monkeypatch.setattr(jw, "USE_PROVIDERS", False)  # even with providers off
    from datetime import datetime, timezone
    out = jw.list_member(member, cutoff=datetime(2026, 1, 1, tzinfo=timezone.utc),
                         policy=None, deadline=1e18)
    assert out == [] and "provider" in called

    # A regional member with no provider gets nothing - never the US API.
    monkeypatch.setattr(jw, "_list_via_provider", lambda m, **k: None)
    assert jw.list_member(member, cutoff=datetime(2026, 1, 1, tzinfo=timezone.utc),
                          policy=None, deadline=1e18) == []


def test_cognite_eu_seed_is_a_career_pages_member():
    from services import job_ingest_worker as jw
    keys = {m["member_key"]: m for m in jw._load_seed_members(jw.SOURCE_CAREER)}
    m = keys.get("greenhouse-cognite-eu")
    assert m is not None and m["site"] == "eu" and m["vendor"] == "greenhouse"
    assert jw.host_allowed(jw.SOURCE_CAREER, f"{EU}/cognite")
