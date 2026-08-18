"""ADR-004 state-registry adapters: 51 jurisdictions, real harvest paths."""
from datetime import date

from services.state_registries import all_adapters, coverage, enabled_adapters, get
from services.state_registries.base import CompanyRecord, is_probably_operating
from services.state_registries.bizfile import BizfileAdapter
from services.state_registries.federal import FederalProxyAdapter
from services.state_registries.socrata import SocrataAdapter
from services.state_registries.sunbiz import SunbizDailyAdapter, _LAYOUT


def test_every_us_jurisdiction_has_an_enabled_adapter():
    adapters = all_adapters()
    assert len(adapters) == 51
    codes = {a.state for a in adapters}
    assert "DC" in codes and "DE" in codes and "CA" in codes and "TX" in codes
    assert len(enabled_adapters()) == 51
    summary = coverage()
    assert summary["enabled"] == 51
    assert summary["total"] == 51


def test_open_data_states_use_verified_socrata_maps():
    for code in ("NY", "CO", "PA", "OR", "CT"):
        adapter = get(code)
        assert isinstance(adapter, SocrataAdapter), code
        assert adapter.enabled
        assert adapter.source_url.endswith(".json")


def test_florida_is_sunbiz_and_idaho_is_bizfile():
    assert isinstance(get("FL"), SunbizDailyAdapter)
    assert isinstance(get("ID"), BizfileAdapter)
    assert isinstance(get("ND"), BizfileAdapter)


def test_search_only_states_are_federal_proxies_not_stubs():
    for code in ("DE", "CA", "TX", "AL", "WA"):
        adapter = get(code)
        assert isinstance(adapter, FederalProxyAdapter), code
        rows = list(adapter.fetch(limit=2))
        assert len(rows) >= 2, code
        assert all(r.state == code and r.name for r in rows)


def test_company_record_as_upsert_maps_registered_on_and_domain():
    rec = CompanyRecord(
        name="Acme Widgets LLC", state="TX", registered_on="2024-01-15",
        city="Austin", postal="78701", source="federal:TX",
        raw={"domain": "acme.com", "signals": {"h1b": True}})
    doc = rec.as_upsert()
    assert doc["registered_at"] == "2024-01-15"
    assert doc["domain"] == "acme.com"
    assert doc["signals"]["h1b"] is True
    assert doc["signals"]["known_domain"] is True
    assert "registered_on" not in doc


def test_rejected_status_is_not_operating():
    assert not is_probably_operating("Rejected")
    assert is_probably_operating("Good Standing")
    assert is_probably_operating("")


def test_bizfile_parses_title_list_and_strips_record_number():
    class _Resp:
        status_code = 200

        def json(self):
            return {"rows": {"1": {
                "TITLE": ["Acme Handyman (4671892)", "LLC"],
                "STATUS": "Active", "RECORD_NUM": "4671892",
                "FILING_DATE": "01/15/2024",
            }}}

    class _Sess:
        def post(self, url, json=None, headers=None, timeout=None):
            return _Resp()

    adapter = BizfileAdapter(
        state="ID", name="Idaho", host="sosbiz.idaho.gov", notes="t",
        term_seeds=("aa",), session=_Sess())
    recs = list(adapter.fetch(limit=5))
    assert recs[0].name == "Acme Handyman"
    assert recs[0].registry_id == "4671892"
    assert recs[0].registered_on == "2024-01-15"
    assert recs[0].entity_type == "LLC"


def test_sunbiz_parses_fixed_width_active_rows():
    buf = [" "] * 1440

    def put(key, val):
        start, length = _LAYOUT[key]
        for i, ch in enumerate(val[:length]):
            buf[start - 1 + i] = ch

    put("number", "P123456")
    put("name", "PUBLIX SUPER MARKETS INC")
    put("status", "A")
    put("filing_type", "FLAL")
    put("princ_city", "LAKELAND")
    put("princ_zip", "33811")
    put("file_date", "01011930")
    line = "".join(buf)

    class _Resp:
        status_code = 200
        text = line + "\n"

    class _Sess:
        def get(self, url, auth=None, headers=None, timeout=None):
            return _Resp()

    adapter = SunbizDailyAdapter(session=_Sess(), today=date(2026, 8, 14))
    recs = list(adapter.fetch(limit=1))
    assert recs[0].name == "PUBLIX SUPER MARKETS INC"
    assert recs[0].status == "active"
    assert recs[0].city == "LAKELAND"
    assert recs[0].registered_on == "1930-01-01"
