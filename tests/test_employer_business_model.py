"""Employer business model (ebm-1.0.0) + the reproduced gap 1.

Gap 1 (docs/audit/jobright-2026-10-21/CLAUDE-PROMPT.txt): `tech_feed_service`
had no consultancy/unknown kind and `_employer_kind_of` FELL BACK to
`direct_employer` after a short name regex, so Accenture, Deloitte,
Cognizant, Hays and an unknown name all read "Direct employer posting".
"""
from services import employer_business_model as ebm
from services import tech_feed_service as tfs
from services.employer_registry import employer_id_for


def _row(name, **kw):
    row = {"employer_name": name, "title_raw": "Software Engineer"}
    row.update(kw)
    return row


# --- gap 1 -----------------------------------------------------------------
def test_gap1_named_consultancies_and_staffing_are_not_direct():
    for name in ("Accenture", "Deloitte", "Cognizant", "Hays"):
        kind = tfs._employer_kind_of(_row(name))
        assert kind != tfs.EMPLOYER_KIND_DIRECT, name
    assert tfs._employer_kind_of(_row("Accenture")) == tfs.EMPLOYER_KIND_CONSULTANCY
    assert tfs._employer_kind_of(_row("Hays")) == tfs.EMPLOYER_KIND_STAFFING


def test_gap1_unknown_name_is_unknown_never_direct():
    assert tfs._employer_kind_of(_row("Zyxwv Holdings")) == tfs.EMPLOYER_KIND_UNKNOWN


def test_employer_kinds_include_consultancy_mixed_unknown():
    for k in ("consultancy", "mixed", "unknown"):
        assert k in tfs.EMPLOYER_KINDS
    req = tfs.normalize_request({"employer_kind": "consultancy"})
    assert req["employer_kind"] == "consultancy"


def test_marketplace_board_stays_marketplace():
    assert tfs._employer_kind_of(_row("Acme", board_kind="marketplace")) == \
        tfs.EMPLOYER_KIND_MARKETPLACE


# --- the name rule is the weakest basis and never yields operating --------
def test_name_rule_never_produces_operating():
    for name in ("Acme Software", "Stripe", "Etsy", "Mozilla", "Juicebox",
                 "Talent Software Inc", "Recruiting Tools Co"):
        model = ebm.classify_employer(None, name=name)
        assert model["kind"] != ebm.KIND_OPERATING
        if model["basis"] == ebm.BASIS_NAME:
            assert model["confidence"] <= ebm.NAME_RULE_CONFIDENCE


def test_unknown_is_a_real_answer():
    model = ebm.classify_employer(None, name="Some Unheard Of Company")
    assert model["kind"] == "unknown" and model["basis"] == "none"
    assert model["version"] == ebm.EBM_VERSION


# --- research register, by DOMAIN only ------------------------------------
def test_register_applies_by_domain_not_by_name():
    # A company merely NAMED like a register staffing firm, with no domain,
    # does not inherit the register row.
    model = ebm.classify_employer(None, name="Brooksource Bakery LLC")
    assert model["basis"] != ebm.BASIS_REGISTER
    by_domain = ebm.classify_employer(None, domains=["brooksource.com"], name="X")
    assert by_domain["kind"] == "staffing" and by_domain["basis"] == "research_register"


def test_register_resolves_by_canonical_employer_id_hash():
    row = _row("Capgemini Engineering",
               canonical_employer_id=employer_id_for(domain="capgemini.com"))
    model = ebm.profile_for_row(row, employers=None)
    assert model["kind"] == "consultancy" and model["basis"] == "research_register"


def test_register_resolves_by_posting_url_host_but_never_a_vendor_host():
    row = _row("Capgemini", canonical_url="https://careers.capgemini.com/job/1")
    assert ebm.profile_for_row(row)["basis"] == "research_register"
    # A Greenhouse host is the VENDOR's domain: it identifies nobody.
    gh = _row("Unknown Co", canonical_url="https://job-boards.greenhouse.io/x/jobs/1")
    assert ebm.profile_for_row(gh)["kind"] == "unknown"


def test_name_collisions_resolve_by_domain():
    assert ebm.classify_employer(None, domains=["suroninc.com"])["kind"] == "staffing"
    assert ebm.classify_employer(None, domains=["suron.com"])["kind"] == "operating"
    assert ebm.classify_employer(None, domains=["crosstalent.com"])["kind"] == "operating"
    assert ebm.classify_employer(None, domains=["crosstalent.io"])["kind"] == "talent_marketplace"


# --- false-positive controls named by the brief ---------------------------
def test_false_positive_controls():
    assert ebm.classify_employer(None, domains=["ascentds.com"])["kind"] in ("operating", "unknown")
    assert ebm.classify_employer(None, domains=["juicebox.ai"])["kind"] == "operating"
    assert ebm.classify_employer(None, domains=["etsy.com"])["kind"] == "operating"
    assert ebm.classify_employer(None, domains=["mozilla.org"])["kind"] == "operating"
    assert ebm.classify_employer(None, domains=["orangelogic.com"])["kind"] == "operating"
    assert ebm.classify_employer(None, domains=["ibm.com"])["kind"] == "mixed"
    # By NAME alone none of them is excluded as staffing.
    for name in ("Ascent Developer Solutions", "Juicebox", "Etsy", "Mozilla",
                 "Orange Logic"):
        assert ebm.classify_employer(None, name=name)["kind"] not in (
            "staffing", "talent_marketplace"), name


def test_review_queue_is_never_forced_binary():
    for dom in ("peraton.com", "mantech.com", "hii.com", "amentum.com", "str.us"):
        assert ebm.classify_employer(None, domains=[dom])["kind"] == "mixed", dom
    assert ebm.classify_employer(None, domains=["risebroadband.com"])["kind"] == "unknown"


def test_mercor_is_a_talent_marketplace():
    assert ebm.classify_employer(None, domains=["mercor.com"])["kind"] == "talent_marketplace"


def test_engineering_design_firms_are_consultancy_with_subtype():
    model = ebm.classify_employer(None, domains=["burnsmcd.com"])
    assert model["kind"] == "consultancy"
    assert model["subtype"] == "engineering_design_services"


# --- registry category / industry codes / override ------------------------
def test_registry_category_it_services_is_consultancy_others_operating():
    emp = {"display_name": "Somecorp", "employer_categories": ["it_services"],
           "category_evidence": [{"category": "it_services", "declared_as": "it-services",
                                  "source": "curated_seed"}]}
    assert ebm.classify_employer(emp)["kind"] == "consultancy"
    sic = {"display_name": "Somecorp", "employer_categories": ["it_services"],
           "category_evidence": [{"category": "it_services", "source": "sic:7371"}]}
    m = ebm.classify_employer(sic)
    assert m["kind"] == "consultancy" and m["confidence"] < 0.5
    prod = {"display_name": "Chipco", "employer_categories": ["semiconductor"]}
    assert ebm.classify_employer(prod)["kind"] == "operating"
    assert ebm.classify_employer(prod)["basis"] == "registry_category"


def test_category_operating_contradicted_by_weaker_basis_is_unknown():
    emp = {"display_name": "Acme Staffing", "employer_categories": ["software_saas"]}
    m = ebm.classify_employer(emp)
    assert m["kind"] == "unknown"
    assert any("conflict" in e["value"] for e in m["evidence"])


def test_naics_5613_is_staffing_5415_only_likely_consultancy_two_digit_ignored():
    m = ebm.classify_employer({"display_name": "X"},
                              industry_codes=[{"system": "naics", "code": "561320"}])
    assert m["kind"] == "staffing" and m["basis"] == "naics"
    m = ebm.classify_employer({"display_name": "X"},
                              industry_codes=[{"system": "naics", "code": "541511"}])
    assert m["kind"] == "consultancy" and m["confidence"] <= 0.35
    m = ebm.classify_employer({"display_name": "X"},
                              industry_codes=[{"system": "naics", "code": "54"}])
    assert m["kind"] == "unknown"


def test_owner_override_wins():
    emp = {"display_name": "Accenture", "canonical_domain": "accenture.com",
           "business_model_override": {"kind": "operating", "evidence": "owner said so",
                                       "set_by": "owner"}}
    m = ebm.classify_employer(emp)
    assert m["kind"] == "operating" and m["basis"] == "owner_override"


def test_stored_model_is_preferred_at_serve_time(mock_db):
    cid = "emp_aaaaaaaaaaaa"
    mock_db["employers"].delete_many({})
    mock_db["employers"].insert_one({
        "employer_id": cid, "display_name": "Foo",
        "business_model": {"kind": "staffing", "confidence": 0.9,
                           "basis": "research_register", "evidence": [],
                           "version": ebm.EBM_VERSION, "classified_at": "x"}})
    rows = [_row("Foo", canonical_employer_id=cid)]
    ebm.resolve_rows(rows)
    assert rows[0]["_employer_profile"]["kind"] == "staffing"
    assert tfs._employer_kind_of(rows[0]) == tfs.EMPLOYER_KIND_STAFFING
