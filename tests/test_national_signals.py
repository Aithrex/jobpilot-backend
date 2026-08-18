"""National employment signals: LCA column flex, no network required."""
from services import national_signals


def test_lca_resolves_year_specific_column_names(tmp_path):
    path = tmp_path / "lca.csv"
    path.write_text(
        "LCA_CASE_EMPLOYER_NAME,LCA_CASE_EMPLOYER_STATE,EMPLOYER_CITY\n"
        "Acme Cloud Inc,TX,Austin\n"
        "Bad Row,,\n"
        "North Star LLC,AK,Anchorage\n",
        encoding="utf-8")
    rows = list(national_signals.iter_lca_rows(str(path)))
    assert [(r["name"], r["state"]) for r in rows] == [
        ("Acme Cloud Inc", "TX"),
        ("North Star LLC", "AK"),
    ]
    assert rows[0]["signals"]["h1b"] is True


def test_edgar_tickers_map_off_a_fixture_session():
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"0": {"cik_str": 320193, "ticker": "AAPL",
                          "title": "Apple Inc."},
                    "1": {"cik_str": 1018724, "ticker": "AMZN",
                          "title": "AMAZON COM INC"}}

    class _Sess:
        def get(self, url, timeout=None):
            return _Resp()

    rows = national_signals.fetch_edgar_tickers(session=_Sess())
    assert {r["ticker"] for r in rows} == {"AAPL", "AMZN"}
    assert rows[0]["signals"]["edgar"] is True
    assert rows[0]["cik"] == "0000320193"
