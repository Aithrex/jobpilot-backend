"""`classify_role` recall and precision, pinned against REAL inventory titles.

`role_family` is the retrieval key for both the guest path and the feed tabs, so
a title the classifier cannot read is a job nobody can reach however well they
match it. Live inventory started at 34.4% of distinct titles classified; the
cases below are drawn from the titles that were failing.

Recall alone is the wrong target - "classify everything" is trivial and
actively harmful, because a posting filed under the wrong family surfaces to
the wrong person. So this file pins BOTH directions: titles that must now
classify, and titles that must NOT be dragged into a neighbouring family.
"""
import pytest

from services import fit_score as fs


# ---------------------------------------------------------------------------
# Recall: real titles from the inventory that used to fall to "other"
# ---------------------------------------------------------------------------

RECALL_CASES = [
    # --- engineering leadership + customer-facing engineering --------------
    ("Engineering Manager, Model Infrastructure", "software_engineering"),
    ("Director of Engineering, Payments", "software_engineering"),
    ("Sr. Forward Deployed Engineer-Retail", "software_engineering"),
    ("Partner Engineer: Partner Intelligence, AI & Apps", "software_engineering"),
    ("Sr. Solutions Architect - Financial Services", "software_engineering"),
    ("Enterprise Solutions Architect", "software_engineering"),
    ("Staff Engineer, Developer Experience", "software_engineering"),
    ("Performance Engineer", "software_engineering"),

    # --- physical / industrial engineering: previously homeless ------------
    ("Mechanical Engineer II", "hardware_engineering"),
    ("Senior Electrical Engineer - Healthcare Buildings", "hardware_engineering"),
    ("Manufacturing Engineer", "hardware_engineering"),
    ("Fire Safety Engineer", "hardware_engineering"),
    ("Process Engineer, Fill Finish", "hardware_engineering"),
    ("Controls Engineer", "hardware_engineering"),

    # --- quality -----------------------------------------------------------
    ("Senior Tester", "qa"),
    ("QA Shop Floor Specialist II", "qa"),
    ("Quality Control Analyst", "qa"),

    # --- everything else that was leaking to "other" -----------------------
    ("Technical Account Manager, Weekend Coverage", "customer_support"),
    ("Implementation Manager - Bilingual", "customer_support"),
    ("Client Services Specialist", "customer_support"),
    ("Sales Enablement Manager, Field Readiness", "sales"),
    ("Senior GTM Analyst", "sales"),
    ("Director, Client Partnerships (New & Growth)", "sales"),
    ("Senior Director, Commercial Strategic Alliances", "sales"),
    ("Procurement Specialist", "operations"),
    ("Manager, Distribution Operations", "operations"),
    ("Sr Manager, Continuous Improvement", "operations"),
    ("Head of Security GRC", "security"),
    ("Lead Detection Engineer", "security"),
    ("Incident Response Lead", "security"),
    ("IT Service Desk Engineer", "it_support"),
    ("Staff Workday HRIS Technical Analyst", "hr"),
    ("Director, Product Management (Player Identity)", "product"),
    ("Senior/Staff User Researcher", "design"),
    ("Director, Paid Media", "marketing"),
    ("Director, International Communications", "marketing"),
    ("Immigration Specialist", "legal"),
    ("Compliance Senior Specialist", "legal"),
    ("Sr. Director of Vendor Compliance", "legal"),
    ("Patient Care Technician", "allied_health"),
    ("Care Coordinator, Home Health", "allied_health"),
    ("Personal Banker", "finance"),
    ("FinOps Analyst", "finance"),
    ("Senior Order to Cash Analyst", "finance"),
    ("Payroll Specialist", "accounting"),
    ("Keyholder", "retail"),
    ("Maintenance Specialist I/II (Contractor)", "skilled_trades"),
    ("Upstream Principal Scientist, Vector Biology", "science_lab"),
    ("Strategy & Analytics Associate", "data_analysis"),
]


@pytest.mark.parametrize("title,expected", RECALL_CASES)
def test_real_titles_classify(title, expected):
    assert fs.classify_role(title) == expected, title


# ---------------------------------------------------------------------------
# Precision: the additions must not drag neighbours in
# ---------------------------------------------------------------------------

PRECISION_CASES = [
    # Matching is on literal phrases, so a seniority-prefixed bare "engineer"
    # cannot swallow an unrelated discipline.
    ("Staff Civil Engineer", "construction"),
    ("Site/Civil Engineer", "construction"),
    # Longest-phrase-wins at a given position.
    ("Technical Account Manager", "customer_support"),
    ("Account Manager, Enterprise", "sales"),
    # "equity research" must beat science_lab's "research associate", which
    # previously filed a finance role under laboratory science.
    ("Equity Research Associate - FinTech", "finance"),
    ("Clinical Research Associate", "science_lab"),
    # Qualified communications only: bare "communications" would swallow this.
    ("Communications Compliance Senior Specialist", "legal"),
    ("Corporate Communications Manager", "marketing"),
    # Long-standing families must not move.
    ("Registered Nurse, ICU", "nursing"),
    ("High School Math Teacher", "teaching"),
    ("Senior Software Engineer", "software_engineering"),
    ("Site Reliability Engineer", "devops"),
    ("Data Scientist, Forecasting", "data_science"),
    ("Journeyman Electrician", "skilled_trades"),
    ("Paralegal, Litigation", "legal"),
    ("Line Cook", "hospitality"),
    ("CDL Truck Driver", "driving"),
]


@pytest.mark.parametrize("title,expected", PRECISION_CASES)
def test_neighbouring_families_are_not_captured(title, expected):
    assert fs.classify_role(title) == expected, title


def test_an_unreadable_title_is_still_refused():
    """The classifier must keep returning None rather than guessing.

    "Classify everything" is trivially achievable and worse than useless: a
    posting filed under the wrong family is shown to the wrong person, which is
    a trust failure, while an unclassified one is merely unreachable.
    """
    for title in ("Grand Vizier of Vibes", "Expression of Interest: 2027 Sneak Peek",
                  "Evergreen - FDE Brazil", "", "   "):
        assert fs.classify_role(title) is None, title


def test_writer_and_reader_share_the_taxonomy():
    """The inventory stamps `role_family` with the same function the resume side
    derives it from. If these ever diverge, retrieval silently matches nothing.
    """
    from services import job_inventory_service as inv

    for title, expected in RECALL_CASES[:12] + PRECISION_CASES[:6]:
        assert inv.role_family_of(title) == expected, title


def test_hardware_engineering_is_a_real_family():
    assert "hardware_engineering" in fs.ROLE_FAMILIES
    assert len(fs.ROLE_FAMILIES) == 32


def test_body_fallback_window_covers_the_requirements_section():
    """The first 800 characters of a JD are almost always company boilerplate.

    Measured on live inventory, an 800-char window recovered a family for 9% of
    title-unclassifiable postings against 50% at 3000.
    """
    boilerplate = ("About us. " + "We are a mission driven company. " * 60)
    body = boilerplate + " We are hiring a Registered Nurse for our ICU unit."
    assert len(boilerplate) > 800
    profile = fs.extract_requirements(body, "Sr. Forward Deployed Specialist")
    assert profile["role_family"] == "nursing"


def test_the_title_always_beats_the_body():
    """A body can mention several professions; the title is the employer's own
    summary of the role."""
    body = "You will partner with our nursing staff and support patient care."
    profile = fs.extract_requirements(body, "Senior Software Engineer")
    assert profile["role_family"] == "software_engineering"
