"""Unit tests for ResumeTailor._polish_skills — the deterministic,
industry-agnostic skills post-pass (cross-category dedup, umbrella collapse,
JD-relevance ordering, title normalization).

Pure dicts, no LLM, no DB.
"""
import copy

from services.resume_tailor import ResumeTailor


def _polish(skills, jd=None, title=None):
    tailored = {"skills": copy.deepcopy(skills)}
    if title is not None:
        tailored["skills_section_title"] = title
    return ResumeTailor._polish_skills(tailored, jd or {})


# ---------------------------------------------------------------------------
# Umbrella collapse
# ---------------------------------------------------------------------------

def test_aws_umbrella_plus_services_collapse_to_one_entry():
    out = _polish({"Cloud": ["AWS", "Amazon EC2", "Amazon S3", "Docker"]})
    assert out["skills"]["Cloud"] == ["AWS (EC2, S3)", "Docker"]


def test_umbrella_prefix_preserves_model_acronym_pair():
    """A model-emitted 'Amazon Web Services (AWS)' keeps its full-form prefix
    (and the scorer's acronym-pair bonus) after collapse."""
    out = _polish({"Cloud": ["Amazon Web Services (AWS)", "Amazon EC2", "Amazon S3"]})
    assert out["skills"]["Cloud"] == ["Amazon Web Services (AWS, EC2, S3)"]


def test_two_members_collapse_without_umbrella():
    out = _polish({"Cloud": ["Amazon EC2", "Amazon S3", "Kubernetes"]})
    assert out["skills"]["Cloud"][0] == "AWS (EC2, S3)"
    assert "Kubernetes" in out["skills"]["Cloud"]


def test_single_member_without_umbrella_stays():
    out = _polish({"Cloud": ["Amazon EC2", "Kubernetes"]})
    assert out["skills"]["Cloud"] == ["Amazon EC2", "Kubernetes"]


def test_alias_forms_resolve_via_normalizer():
    """Short forms (EC2, S3, EKS) resolve to canonical members through
    keyword_normalizer aliases."""
    out = _polish({"Cloud": ["AWS", "EC2", "S3", "EKS"]})
    assert out["skills"]["Cloud"] == ["AWS (EC2, S3, EKS)"]


def test_adobe_literal_members_collapse():
    out = _polish({"Design Tools": ["Photoshop", "Illustrator", "Canva"]})
    assert out["skills"]["Design Tools"] == [
        "Adobe Creative Suite (Photoshop, Illustrator)", "Canva",
    ]


def test_office_collapse_caps_subs_at_four():
    out = _polish({
        "Software": ["Microsoft Office", "Excel", "PowerPoint", "Word",
                     "Outlook", "Visio", "QuickBooks"],
    })
    entry = out["skills"]["Software"][0]
    assert entry.startswith("Microsoft Office (")
    subs = entry[entry.index("(") + 1:-1].split(", ")
    assert len(subs) == ResumeTailor._MAX_UMBRELLA_SUBS
    assert "QuickBooks" in out["skills"]["Software"]


def test_jd_matched_members_survive_sub_cap():
    """JD-matched sub-services are never dropped from the parenthetical."""
    jd = {"required_skills": ["Amazon RDS", "Amazon SQS"]}
    out = _polish({
        "Cloud": ["AWS", "Amazon EC2", "Amazon S3", "AWS Lambda",
                  "Amazon ECS", "Amazon RDS", "Amazon SQS"],
    }, jd)
    entry = out["skills"]["Cloud"][0]
    assert "RDS" in entry and "SQS" in entry


def test_platform_agnostic_tools_never_absorbed():
    out = _polish({"Cloud": ["AWS", "Amazon EC2", "Amazon S3",
                             "Terraform", "Kubernetes"]})
    cloud = out["skills"]["Cloud"]
    assert "Terraform" in cloud
    assert "Kubernetes" in cloud


def test_members_spread_across_categories_collapse_once():
    out = _polish({
        "Cloud": ["AWS", "Amazon EC2"],
        "Storage": ["Amazon S3", "PostgreSQL"],
    })
    assert out["skills"]["Cloud"] == ["AWS (EC2, S3)"]
    assert out["skills"]["Storage"] == ["PostgreSQL"]


# ---------------------------------------------------------------------------
# Dedup / ordering / cap
# ---------------------------------------------------------------------------

def test_cross_category_dedup_keeps_most_relevant_category():
    jd = {"required_skills": ["Python", "Django"]}
    out = _polish({
        "Tools": ["Git", "Python"],
        "Backend": ["Python", "Django"],
    }, jd)
    # Programming languages are filed under Languages, not Backend/Tools.
    assert "Python" in out["skills"]["Languages"]
    assert "Python" not in out["skills"].get("Tools", [])
    assert "Django" in out["skills"]["Backend"]


def test_empty_categories_pruned():
    out = _polish({"Cloud": [], "Backend": ["Python"]})
    assert "Cloud" not in out["skills"]
    assert out["skills"]["Languages"] == ["Python"]
    assert "Backend" not in out["skills"]


def test_alias_dedup_across_categories():
    """'Postgres' and 'PostgreSQL' are the same canonical skill."""
    out = _polish({
        "Databases": ["PostgreSQL"],
        "Backend": ["Postgres", "FastAPI"],
    })
    all_items = [s for items in out["skills"].values() for s in items]
    assert len([s for s in all_items if "ostgre" in s]) == 1


def test_jd_aligned_first_keeps_all_items():
    """JD-aligned skills lead the category; extra original items are kept."""
    jd = {"required_skills": ["Go", "Rust"]}
    items = ["Skill%02d" % i for i in range(10)] + ["Go", "Rust"]
    out = _polish({"Languages": items}, jd)
    kept = out["skills"]["Languages"]
    assert len(kept) == 12
    assert kept[0] == "Go" and kept[1] == "Rust"
    assert "Skill09" in kept


def test_categories_reordered_by_jd_relevance():
    jd = {"required_skills": ["Excel", "financial modeling"],
          "keywords": ["variance analysis"]}
    out = _polish({
        "Languages": ["French", "Spanish"],
        "Financial Analysis": ["financial modeling", "variance analysis",
                               "Microsoft Excel"],
    }, jd)
    assert list(out["skills"]) == ["Financial Analysis", "Languages"]


# ---------------------------------------------------------------------------
# Section title normalization
# ---------------------------------------------------------------------------

def test_title_normalized_case_insensitively():
    out = _polish({"A": ["Python"]}, title="core skills")
    assert out["skills_section_title"] == "Core Skills"


def test_title_falls_back_to_default():
    assert _polish({"A": ["Python"]})["skills_section_title"] == "Technical Skills"
    assert _polish({"A": ["Python"]}, title="Core Competencies")[
        "skills_section_title"] == "Technical Skills"


# ---------------------------------------------------------------------------
# Downstream regressions: collapsed entries still keyword-match and survive
# PDF extraction
# ---------------------------------------------------------------------------

def _mini_resume(skills):
    return {
        "contact": {"name": "Test User", "email": "t@example.com",
                    "phone": "480-555-0100", "linkedin": "", "github": "",
                    "location": "Tempe, AZ", "portfolio": ""},
        "summary": "Backend engineer building services on AWS.",
        "skills": skills,
        "experience": [{"title": "Engineer", "company": "Acme",
                        "location": "", "dates": "2022 - Present",
                        "type": "Full-time",
                        "bullets": ["Built APIs handling 1M requests daily."]}],
        "education": [], "projects": [], "certifications": [],
    }


def test_gap_engine_matches_members_inside_collapsed_entry():
    from services.keyword_gap_engine import KeywordGapEngine

    resume = _mini_resume({"Cloud": ["AWS (EC2, S3, EKS, RDS)", "Docker"]})
    jd = {"required_skills": ["EC2", "S3", "EKS", "RDS", "AWS"], "keywords": []}
    gap = KeywordGapEngine().analyze(resume, jd)
    missing = {m.lower() for m in (gap.get("required_missing") or [])}
    for kw in ("ec2", "s3", "eks", "rds", "aws"):
        assert not any(kw in m for m in missing), f"{kw} reported missing"


def test_collapsed_skills_survive_pdf_extraction():
    from services.pdf_ats_check import check_pdf_parseability
    from services.resume_renderer import ResumeRenderer

    resume = _mini_resume({
        "Cloud & DevOps": ["AWS (EC2, S3, EKS, RDS)", "Kubernetes", "Docker",
                           "Helm", "GitHub Actions", "Terraform"],
        "Languages": ["Python", "Go", "Java"],
    })
    pdf = ResumeRenderer().generate_pdf(resume)
    check = check_pdf_parseability(pdf, resume, {})
    found = check["checks"]["skills_keywords_found"]
    total = check["checks"]["skills_keywords_total"]
    assert total > 0 and found / total >= 0.9


# ---------------------------------------------------------------------------
# Base-skill restore + evidenced JD adds (_ensure_jd_skills_coverage)
# ---------------------------------------------------------------------------

def _merge(original, tailored_skills, jd):
    t = {"skills": copy.deepcopy(tailored_skills)}
    t = ResumeTailor._ensure_jd_skills_coverage(t, original, jd)
    return ResumeTailor._polish_skills(t, jd)


def test_restore_keeps_all_base_skills_when_model_drops_them():
    original = {
        "skills": {
            "Languages": ["Python", "SQL", "Go"],
            "Cloud": ["AWS", "Docker", "Kubernetes"],
        },
        "summary": "",
        "experience": [],
        "projects": [],
    }
    out = _merge(
        original,
        {"Languages": ["Python"]},  # model dropped most of the list
        {"required_skills": ["Python"]},
    )
    flat = [s for items in out["skills"].values() for s in items]
    for skill in ("Python", "SQL", "Go", "AWS", "Docker", "Kubernetes"):
        assert skill in flat, skill


def test_jd_skills_lead_each_restored_category():
    original = {
        "skills": {"Languages": ["Python", "SQL", "Go"]},
        "summary": "",
        "experience": [],
        "projects": [],
    }
    out = _merge(original, {"Languages": ["Python"]}, {"required_skills": ["Go"]})
    assert out["skills"]["Languages"][0] == "Go"
    assert "Python" in out["skills"]["Languages"]
    assert "SQL" in out["skills"]["Languages"]


def test_adds_jd_skill_mentioned_in_a_bullet():
    original = {
        "skills": {"Languages": ["Python"]},
        "summary": "",
        "experience": [{
            "title": "Engineer", "company": "Acme",
            "bullets": ["Deployed services on Kubernetes and Helm."],
        }],
        "projects": [],
    }
    out = _merge(
        original,
        {"Languages": ["Python"]},
        {"required_skills": ["Kubernetes"]},
    )
    flat = [s for items in out["skills"].values() for s in items]
    assert "Kubernetes" in flat
    assert "Python" in flat


def test_adds_implied_jd_skill_next_to_parent():
    original = {
        "skills": {"Frontend": ["React", "TypeScript"]},
        "summary": "",
        "experience": [],
        "projects": [],
    }
    out = _merge(
        original,
        {"Frontend": ["React"]},
        {"required_skills": ["HTML"]},
    )
    assert "HTML" in out["skills"]["Frontend"]
    assert out["skills"]["Frontend"][0] == "HTML"
    assert "React" in out["skills"]["Frontend"]
    # TypeScript is a programming language even when the original listed it
    # under Frontend.
    assert "TypeScript" in out["skills"]["Languages"]


def test_does_not_add_unevidenced_jd_skill():
    original = {
        "skills": {"Languages": ["Python"]},
        "summary": "",
        "experience": [{"title": "Engineer", "company": "Acme",
                        "bullets": ["Built APIs in Python."]}],
        "projects": [],
    }
    out = _merge(
        original,
        {"Languages": ["Python"]},
        {"required_skills": ["Salesforce", "Qualys"]},
    )
    flat = [s.lower() for items in out["skills"].values() for s in items]
    assert "salesforce" not in flat
    assert "qualys" not in flat
    assert "python" in flat


def test_does_not_inject_jd_soft_traits():
    original = {
        "skills": {"Languages": ["Python"]},
        "summary": "",
        "experience": [],
        "projects": [],
    }
    out = _merge(
        original,
        {"Languages": ["Python", "Comfort with Ambiguity"]},
        {"required_skills": ["Comfort with Ambiguity", "Python"]},
    )
    flat = " ".join(s.lower() for items in out["skills"].values() for s in items)
    assert "ambiguity" not in flat
    assert "python" in flat


def test_caps_new_jd_adds():
    tools = [
        "Kafka", "Redis", "GraphQL", "Terraform", "Ansible",
        "Grafana", "Prometheus", "Snowflake", "Airflow", "Spark",
    ]
    original = {
        "skills": {"Languages": ["Python"]},
        "summary": "",
        "experience": [{
            "title": "Engineer", "company": "Acme",
            "bullets": ["Used " + ", ".join(tools) + " in production."],
        }],
        "projects": [],
    }
    out = _merge(
        original,
        {"Languages": ["Python"]},
        {"required_skills": tools},
    )
    flat = [s for items in out["skills"].values() for s in items]
    added = [s for s in flat if s != "Python"]
    assert len(added) == ResumeTailor._MAX_NEW_JD_SKILLS
    assert "Python" in flat


# ---------------------------------------------------------------------------
# Recategorize: languages vs concepts, heading clones, "X programming"
# ---------------------------------------------------------------------------

def test_non_languages_leave_languages_category():
    out = _polish({
        "Languages": [
            "Java", "High-throughput", "Telemetry",
            "Performance Optimization", "Python", "SQL",
        ],
        "Concepts & Methodologies": ["Microservices"],
    })
    langs = [s.lower() for s in out["skills"]["Languages"]]
    assert "java" in langs and "python" in langs and "sql" in langs
    assert "high-throughput" not in langs
    assert "telemetry" not in langs
    assert "performance optimization" not in langs
    concepts = " ".join(s.lower() for s in out["skills"]["Concepts & Methodologies"])
    assert "high-throughput" in concepts
    assert "telemetry" in concepts
    assert "performance optimization" in concepts


def test_drops_item_that_repeats_category_heading():
    out = _polish({
        "Databases": ["PostgreSQL", "Databases", "MySQL", "MongoDB"],
    })
    items = [s.lower() for s in out["skills"]["Databases"]]
    assert "databases" not in items
    assert "postgresql" in items and "mysql" in items


def test_java_programming_collapses_into_languages():
    out = _polish({
        "Languages": ["Java", "Python"],
        "Frameworks & Libraries": [
            "Java programming", "FastAPI", "Spring Boot", "React",
        ],
    })
    langs = out["skills"]["Languages"]
    assert langs.count("Java") == 1
    assert "Python" in langs
    frameworks = " ".join(out["skills"]["Frameworks & Libraries"])
    assert "Java programming" not in frameworks
    assert "FastAPI" in frameworks and "React" in frameworks


def test_new_jd_skill_does_not_dump_into_languages():
    original = {
        "skills": {"Languages": ["Python"]},
        "summary": "",
        "experience": [{
            "title": "Engineer", "company": "Acme",
            "bullets": ["Shipped high-throughput telemetry on Kafka."],
        }],
        "projects": [],
    }
    out = _merge(
        original,
        {"Languages": ["Python"]},
        {"required_skills": ["Kafka"]},
    )
    langs = [s.lower() for s in out["skills"].get("Languages", [])]
    assert "kafka" not in langs
    assert "python" in langs
    flat = " ".join(s.lower() for items in out["skills"].values() for s in items)
    assert "kafka" in flat

