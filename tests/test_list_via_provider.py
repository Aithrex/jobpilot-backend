"""New ATS vendors must list through the provider, not the Greenhouse fallback."""
import time
from datetime import datetime, timezone

from services import job_ingest_worker as w
from services.ats_providers.base import RawJob


class _FakeProvider:
    vendor = "oracle_orc"
    has_detail = True

    def list_jobs(self, endpoint, ctx):
        assert endpoint.host == "jpmc.fa.oraclecloud.com"
        assert endpoint.site == "CX_1"
        yield RawJob(
            source_job_id="12345",
            title="Software Engineer",
            apply_url="https://jpmc.fa.oraclecloud.com/hcmUI/job/12345",
            posted_at="2026-08-16T00:00:00+00:00",
            location="New York, NY",
        )
        yield RawJob(
            source_job_id="old",
            title="Ancient Role",
            apply_url="https://jpmc.fa.oraclecloud.com/hcmUI/job/old",
            posted_at="2020-01-01T00:00:00+00:00",
        )

    def fetch_detail(self, endpoint, job, ctx):
        job.description = "FULL JD"
        return job


class _Reg:
    def get(self, vendor):
        return _FakeProvider() if vendor == "oracle_orc" else None


def _oracle_member():
    return {
        "member_key": "oracle-orc-jpmc-cx-1",
        "source": "oracle",
        "vendor": "oracle_orc",
        "ats": "oracle_orc",
        "tenant": "jpmc",
        "slug": "jpmc",
        "site": "CX_1",
        "host": "jpmc.fa.oraclecloud.com",
        "display_name": "JPMorgan Chase",
        "industry": "banking",
        "extra": {},
    }


def test_oracle_lists_through_the_provider(monkeypatch):
    monkeypatch.setattr(w, "_ats_registry", lambda: _Reg())
    monkeypatch.setattr(w, "USE_PROVIDERS", True)
    cutoff = datetime(2026, 8, 10, tzinfo=timezone.utc)
    rows = w.list_member(
        _oracle_member(), cutoff=cutoff, policy=w.HostPolicy(),
        deadline=time.monotonic() + 30)
    assert [r["title"] for r in rows] == ["Software Engineer"]
    assert rows[0]["source"] == "oracle"
    assert rows[0]["ats"] == "oracle_orc"
    assert "12345" in rows[0]["source_job_id"]


def test_oracle_detail_goes_through_the_provider(monkeypatch):
    monkeypatch.setattr(w, "_ats_registry", lambda: _Reg())
    monkeypatch.setattr(w, "USE_PROVIDERS", True)
    body = w.fetch_detail(
        {"title": "Software Engineer", "url": "https://x",
         "source_job_id": "oracle-orc-jpmc-cx-1:12345",
         "_detail_ref": "12345"},
        _oracle_member(),
        policy=w.HostPolicy(),
        deadline=time.monotonic() + 30)
    assert body == "FULL JD"


def test_workday_does_not_divert_to_the_provider_path(monkeypatch):
    called = []
    monkeypatch.setattr(w, "_list_via_provider",
                        lambda *a, **k: called.append("provider") or [])
    monkeypatch.setattr(w, "_list_workday",
                        lambda *a, **k: called.append("workday") or [{"t": 1}])
    member = {"source": "workday", "vendor": "workday", "ats": "workday",
              "tenant": "acme", "slug": "acme", "site": "External",
              "display_name": "Acme", "industry": "tech", "member_key": "acme",
              "wd_host": "wd1"}
    rows = w.list_member(
        member, cutoff=datetime(2026, 8, 10, tzinfo=timezone.utc),
        policy=w.HostPolicy(), deadline=time.monotonic() + 30)
    assert called == ["workday"]
    assert rows == [{"t": 1}]
