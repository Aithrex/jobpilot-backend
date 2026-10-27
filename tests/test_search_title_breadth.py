"""The 40-title search cap is shared fairly across a candidate's specialties.

Measured on the 2026-10-22 feed audit: the expansion walked specialties in
taxonomy order, so frontend's fourteen spellings took the cap before the
specialties later in the taxonomy were reached. A "Software Engineer II -
Platform Engineering" resume never searched "Platform Engineer", and a DevOps
resume never searched "Site Reliability Engineer".
"""
from services import fit_score
from services.job_title_seed import TITLE_CAP, canonical_search_titles

_PLATFORM_SPECS = ["general_software", "backend", "frontend", "full_stack",
                   "data_engineering", "devops", "platform_engineering",
                   "cloud_engineering"]


def _lower(titles):
    return [t.lower() for t in titles]


def test_a_platform_engineer_searches_platform_engineer():
    out = canonical_search_titles(
        ["Software Engineer II – Platform Engineering",
         "Full-Stack Software Engineer – EHR Platform"],
        _PLATFORM_SPECS, level="mid")
    assert "platform engineer" in _lower(out)
    # The specialty the resume NAMES comes before the inferred long tail.
    assert _lower(out).index("platform engineer") < _lower(out).index("frontend engineer")


def test_a_devops_engineer_searches_sre_and_platform():
    out = canonical_search_titles(
        ["Cloud and DevOps Engineer", "Software Engineer, Contract"],
        ["general_software", "backend", "frontend", "devops", "cloud_engineering"],
        level="entry")
    low = _lower(out)
    assert "devops engineer" in low
    assert "site reliability engineer" in low
    assert "platform engineer" in low


def test_every_specialty_gets_its_head_title_inside_the_cap():
    out = canonical_search_titles(["Software Engineer"],
                                  ["general_software", "backend", "frontend",
                                   "full_stack", "data_engineering"], level="mid")
    low = _lower(out)
    assert len(out) <= TITLE_CAP
    for head in ("backend engineer", "frontend engineer", "full stack engineer",
                 "etl engineer"):
        assert head in low, head


def test_a_stated_backend_does_not_readmit_other_families():
    # The family gate still holds: naming "backend" adds only the family of
    # backend's head phrase, never every family its long tail classifies as.
    out = canonical_search_titles(["Backend Engineer"], ["general_software", "backend"],
                                  level="mid")
    fams = {fit_score.classify_role(t) for t in out} - {None}
    assert fams == {"software_engineering"}


def test_the_software_floor_still_leads():
    out = canonical_search_titles(["Software Engineer"], _PLATFORM_SPECS, level="mid")
    assert out[:3] == ["Software Engineer", "Software Developer",
                       "Software Development Engineer"]


def test_a_plain_software_resume_searches_what_it_did_before_the_reorder():
    """2026-10-24: a round-robin over every specialty put "Postgres Engineer",
    "Mysql Engineer" and "ETL Engineer" in a general software engineer's top
    ten. Only specialties the resume NAMES are reordered; this is the list a
    plain software resume searched before any of that (115f538)."""
    out = canonical_search_titles(
        ["Software Engineer", "Server Debug Technician", "Network Analyst"],
        ["general_software", "backend", "frontend", "full_stack", "data_engineering",
         "database", "devops", "platform_engineering", "cloud_engineering"], level="mid")
    assert out[:6] == ["Software Engineer", "Software Developer", "Software Development Engineer",
                       "Backend Engineer", "Back End Engineer", "Back-End Engineer"]
    low = _lower(out[:15])
    for odd in ("postgres engineer", "mysql engineer", "etl engineer", "data pipeline engineer"):
        assert odd not in low, odd


# --- 2026-10-27: no evidenced specialty loses every title; no junk chips -----

def test_every_evidenced_specialty_keeps_its_head_without_moving_the_top():
    """A "Software Engineer" resume with DevOps, platform and cloud evidence
    searched none of the three: general-software filler held the slots."""
    specs = ["general_software", "backend", "frontend", "full_stack", "data_engineering",
             "database", "devops", "platform_engineering", "cloud_engineering"]
    out = canonical_search_titles(
        ["Software Engineer", "Server Debug Technician", "Network Analyst"], specs, level="mid")
    low = _lower(out)
    assert len(out) == TITLE_CAP
    for head in ("devops engineer", "platform engineer", "cloud engineer", "etl engineer"):
        assert head in low, head
    assert out[:6] == ["Software Engineer", "Software Developer", "Software Development Engineer",
                       "Backend Engineer", "Back End Engineer", "Back-End Engineer"]


def test_the_expansion_never_offers_junk_spellings():
    out = canonical_search_titles(["Software Engineer"], ["general_software", "backend"],
                                  level="mid")
    for junk in ("polyglot engineer", "software craftsperson", "mts software",
                 "software engr", "development engineer software", "product engineer"):
        assert junk not in _lower(out), junk


def test_a_title_the_seeker_held_is_still_searched():
    out = canonical_search_titles(["Product Engineer"], ["general_software"], level="mid")
    assert "product engineer" in _lower(out)


def test_unhelpful_titles_with_tool_evidence_search_the_evidenced_work():
    """"Technical Associate" running Terraform and Kubernetes searched only
    "Technical Associate" and "Analyst"."""
    out = canonical_search_titles(["Technical Associate", "Analyst"],
                                  ["devops", "kubernetes_containers"], level="entry")
    low = _lower(out)
    assert low[:2] == ["technical associate", "analyst"]
    assert "devops engineer" in low


def test_another_occupation_still_keeps_only_its_own_titles():
    """The nurse-plus-one-stray-specialty guard holds."""
    out = canonical_search_titles(["Registered Nurse"], ["general_software"],
                                  degrees=["BSN Nursing"])
    assert out == ["Registered Nurse"]
