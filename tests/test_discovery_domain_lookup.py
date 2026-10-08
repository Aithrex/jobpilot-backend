"""When a company's name does not spell its domain, discovery asks a public
index for a suggestion - and believes it only if the site names the company.

2026-10-07: of the H-1B sponsors discovery had deferred, 71% had no domain.
"""
import pytest

from services import career_portal_discovery as d


class _Ctx:
    def __init__(self, pages, suggestions=None, lookup_status=200):
        self.pages, self.suggestions, self.lookup_status = pages, suggestions, lookup_status
        self.asked, self.fetched = [], []

    def should_stop(self):
        return False

    def get_json(self, url, **kwargs):
        self.asked.append(kwargs["params"]["query"])
        return self.lookup_status, self.suggestions

    def get_text(self, url, **kwargs):
        self.fetched.append(url)
        return (200, self.pages[url]) if url in self.pages else (404, "")


class _Robots:
    def allows(self, url):
        return True


def _discoverer(ctx, monkeypatch):
    monkeypatch.setattr(d, "_resolves", lambda host: True)
    engine = object.__new__(d.PortalDiscovery) if hasattr(d, "PortalDiscovery") else None
    if engine is None:
        cls = next(v for v in vars(d).values()
                   if isinstance(v, type) and hasattr(v, "_resolve_domain"))
        engine = object.__new__(cls)
    engine.ctx, engine.robots, engine.last_status = ctx, _Robots(), None
    return engine


AA = "<html><head><title>American Airlines - Airline tickets and low fares</title></head><body>Book</body></html>"


def test_a_suggested_domain_is_used_when_its_own_page_names_the_company(monkeypatch):
    ctx = _Ctx({"https://aa.com/": AA},
               suggestions=[{"name": "American Airlines Group", "domain": "aa.com"}])
    res = d.DiscoveryResult()
    assert _discoverer(ctx, monkeypatch)._resolve_domain("AMERICAN AIRLINES INC", "", res) == "aa.com"
    assert ctx.asked == ["american airlines"]


def test_a_fitting_suggestion_is_still_refused_when_its_page_is_someone_else(monkeypatch):
    parked = ("<html><head><title>Domain for sale</title></head><body>"
              "Rockets and other acme products. Summit views.</body></html>")
    ctx = _Ctx({"https://sra.io/": parked},
               suggestions=[{"name": "Summit Rockets", "domain": "sra.io"}])
    res = d.DiscoveryResult()
    assert _discoverer(ctx, monkeypatch)._resolve_domain("SUMMIT ROCKETS INC", "", res) == ""
    assert "https://sra.io/" in ctx.fetched and res.detail == "domain did not confirm"


def test_the_name_spelled_domain_still_wins_and_costs_no_lookup(monkeypatch):
    ctx = _Ctx({"https://cognex.com/": "<html><title>Cognex</title>cognex vision</html>"},
               suggestions=[{"domain": "elsewhere.com"}])
    res = d.DiscoveryResult()
    assert _discoverer(ctx, monkeypatch)._resolve_domain("COGNEX CORP", "", res) == "cognex.com"
    assert ctx.asked == []


@pytest.mark.parametrize("status,payload", [(429, None), (200, {"error": "x"}), (200, [])])
def test_a_lookup_that_fails_is_just_no_suggestion(monkeypatch, status, payload):
    ctx = _Ctx({}, suggestions=payload, lookup_status=status)
    res = d.DiscoveryResult()
    assert _discoverer(ctx, monkeypatch)._resolve_domain("AMERICAN AIRLINES INC", "", res) == ""


def test_it_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(d, "DOMAIN_LOOKUP_URL", "")
    ctx = _Ctx({"https://aa.com/": AA}, suggestions=[{"domain": "aa.com"}])
    res = d.DiscoveryResult()
    assert _discoverer(ctx, monkeypatch)._resolve_domain("AMERICAN AIRLINES INC", "", res) == ""
    assert ctx.asked == []


def test_a_name_with_nothing_to_check_never_confirms_a_suggestion():
    assert not d._page_mentions("<title>TT</title>", "TT LLC", suggested=True)
    assert d._page_mentions("<title>The Jackson Laboratory</title>", "THE JACKSON LABORATORY",
                            suggested=True)
    assert d._page_mentions("<title>UH</title><p>University Hospitals Medical Group, Inc.</p>",
                            "UNIVERSITY HOSPITALS MEDICAL GROUP INC", suggested=True)


@pytest.mark.parametrize("company,suggestion,fits", [
    ("AMERICAN AIRLINES INC", "American Airlines Group", True),
    ("AMERICAN AIRLINES INC", "American Airlines Federal Credit Union", False),
    ("AMERICAN AIRLINES INC", "American Airlines Cargo", False),
    ("CHARLES SCHWAB & CO., INC.", "Charles Schwab", True),
    ("THE JACKSON LABORATORY", "Jackson Laboratory Japan", False),
    ("NEW YORK INDEPENDENT SYSTEM OPERATOR", "New York Post", False),
    ("UNIVERSITY OF ILLINOIS AT URBANA-CHAMPAIGN", "University of Michigan", False),
    ("WHIRLPOOL CORPORATION", "Whirlpool", True),
    ("WHIRLPOOL CORPORATION", "", False),
])
def test_a_namesake_is_not_the_company(company, suggestion, fits):
    assert d._suggestion_fits(company, suggestion) is fits


def test_a_namesake_is_never_fetched(monkeypatch):
    union = "<html><title>American Airlines Federal Credit Union</title></html>"
    ctx = _Ctx({"https://aacreditunion.org/": union},
               suggestions=[{"name": "American Airlines Group", "domain": "aa.com"},
                            {"name": "American Airlines Federal Credit Union",
                             "domain": "aacreditunion.org"}])
    res = d.DiscoveryResult()
    assert _discoverer(ctx, monkeypatch)._resolve_domain("AMERICAN AIRLINES INC", "", res) == ""
    assert "https://aacreditunion.org/" not in ctx.fetched
