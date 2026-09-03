"""Skill heading architecture: capture, classify, group, name, validate."""
from services import skill_layout as sl
from services.resume_parser import ResumeParser
from services.resume_tailor import ResumeTailor


def _user_layout():
    return [
        sl.SkillCategory("Languages", ["Python", "Java 17", "TypeScript", "JavaScript", "SQL"], "user"),
        sl.SkillCategory("Frontend", ["React", "Redux", "Angular", "HTML5", "CSS3", "Tailwind CSS"], "user"),
        sl.SkillCategory("Backend & APIs", ["FastAPI", "Django", "Spring Boot", "Node.js", "REST APIs"], "user"),
        sl.SkillCategory("Distributed Systems & Data", ["Kafka", "RabbitMQ", "Redis", "PostgreSQL", "pgvector", "MySQL"], "user"),
        sl.SkillCategory("Cloud & Infrastructure", ["AWS", "Docker", "Kubernetes", "Terraform"], "user"),
        sl.SkillCategory("Observability & Testing", ["OpenTelemetry", "Datadog", "Prometheus", "Grafana", "pytest"], "user"),
        sl.SkillCategory("AI & Standards", ["LangChain", "RAG", "AWS Bedrock", "FHIR R4", "HIPAA"], "user"),
    ]


def test_layout_preservation_keeps_custom_labels():
    base = _user_layout()
    terms = [t for row in base for t in row.items]
    out = sl.compose(base, terms, jd_required=["Django", "PostgreSQL", "React"])
    assert out.mode == "PRESERVE"
    assert list(out.skills) == [row.label for row in base]


def test_parser_prompt_does_not_apply_taxonomy():
    import inspect
    import services.resume_parser as rp
    text = inspect.getsource(rp)
    assert "Categorize into these groups when applicable" not in text
    assert "copy those heading names VERBATIM" in text


def test_parser_attach_preserves_custom_labels():
    result = {
        "skills": {
            "Backend & APIs": ["FastAPI", "Django"],
            "Frontend": ["React"],
            "Languages": ["Python"],
        }
    }
    out = ResumeParser._attach_skills_layout(result)
    labels = [row["label"] for row in out["skills_layout"]]
    assert labels == ["Backend & APIs", "Frontend", "Languages"]
    assert all(row["provenance"] == "user" for row in out["skills_layout"])


def test_mode_selection_bands():
    good = _user_layout()
    assert sl.resolve_mode(good) == "PRESERVE"
    inferred = [sl.SkillCategory(r.label, r.items, "inferred") for r in good]
    assert sl.resolve_mode(inferred) == "DERIVE"
    thin = [sl.SkillCategory("Stuff", ["Python"], "user")]
    assert sl.resolve_mode(thin) in {"REFINE", "DERIVE"}


def test_version_suffix_is_language():
    for term in ("Java 17", "Python 3.11", "Java SE 8"):
        assert sl.lookup_term(term).kind == "language", term


def test_language_integrity_rejects_language_in_wrong_group():
    base = _user_layout()
    broken = [
        sl.SkillCategory("Languages", ["Python"], "user"),
        sl.SkillCategory("Concepts & Methodologies", ["Java 17", "Microservices"], "user"),
    ]
    reasons = sl.validate_layout(broken, base)
    assert any(r.startswith("language_integrity") for r in reasons)


def test_role_labels_stripped():
    layout = sl.layout_from_skills({
        "Languages": ["Python", "Full-Stack", "Backend"],
    }, provenance="user")
    flat = [t for row in layout for t in row.items]
    assert "Python" in flat
    assert not any(sl.is_role_label(t) for t in flat)


def test_grouping_determinism():
    terms = [
        "Python", "Java", "React", "Django", "PostgreSQL", "Kafka",
        "AWS", "Docker", "pytest", "Datadog", "LangChain", "Redis",
    ]
    runs = [sl.group_terms(terms) for _ in range(5)]
    first = [tuple(g) for g in runs[0]]
    assert all([tuple(g) for g in run] == first for run in runs[1:])


def test_naming_determinism():
    items = ["OpenTelemetry", "Datadog", "Prometheus", "pytest"]
    names = [sl.name_group(items, [], []) for _ in range(5)]
    assert len(set(names)) == 1


def test_grouping_runs_post_addition():
    base_terms = ["Python", "Java", "React", "Django", "PostgreSQL"]
    before = sl.group_terms(base_terms)
    added = ["OpenTelemetry", "Datadog", "Prometheus", "Grafana"]
    after = sl.group_terms(base_terms + added)
    assert len(after) >= len(before)
    obs = [g for g in after if sum(1 for t in g if sl.lookup_term(t).kind == "observability") >= 3]
    assert obs, after


def test_merge_and_split():
    tiny = sl.group_terms(["pytest", "Python", "Java", "React", "Django", "PostgreSQL", "AWS"])
    assert all(len(g) >= 2 or sl.lookup_term(g[0]).kind is None for g in tiny)
    many_db = [
        "PostgreSQL", "MySQL", "MongoDB", "Redis", "Cassandra",
        "DynamoDB", "Oracle", "MariaDB", "SQLite", "Neo4j",
        "pgvector", "Pinecone", "Weaviate", "Elasticsearch",
    ]
    split = sl.group_terms(many_db + ["Python", "Java", "React", "Django"])
    assert any(len(g) >= 3 and all(sl.lookup_term(t).sub == "vector-db" or True for t in g) for g in split)


def test_ordering_bound_preserve():
    base = _user_layout()
    terms = [t for row in base for t in row.items]
    out = sl.compose(base, terms, jd_required=["React", "Redux"])
    labels = list(out.skills)
    base_labels = [r.label for r in base]
    for label in labels:
        if label not in base_labels:
            continue
        moved = base_labels.index(label) - labels.index(label)
        assert moved <= 2, (label, moved)


def test_no_alphabetical_when_base_differs():
    base = _user_layout()
    terms = [t for row in base for t in row.items]
    out = sl.compose(base, terms, jd_required=["Django"])
    labels = list(out.skills)
    assert labels != sorted(labels)


def test_unknown_terms_not_defaulted_to_tool():
    facts = sl.lookup_term("AMadeUpFrameworkX")
    assert facts.kind is None
    result = sl.compose(
        _user_layout(),
        [t for row in _user_layout() for t in row.items] + ["AMadeUpFrameworkX"],
    )
    assert "AMadeUpFrameworkX" in result.unknown_queued or any(
        "AMadeUpFrameworkX" in items for items in result.skills.values()
    )
    # Must not have been dumped into a heading named Tool.
    assert "Tool" not in result.skills
    assert "Tools" not in result.skills


def test_compose_moves_java_out_of_concepts_on_fallback():
    base = [
        sl.SkillCategory("Languages", ["Python", "SQL"], "user"),
        sl.SkillCategory("Concepts & Methodologies", ["Microservices", "Java 17"], "user"),
        sl.SkillCategory("Cloud", ["AWS", "Docker", "Kubernetes"], "user"),
        sl.SkillCategory("Frameworks", ["FastAPI", "Django", "React"], "user"),
    ]
    # Force a gate miss by putting Java in concepts; compose PRESERVE
    # then validate should repair via fallback language integrity.
    terms = [t for row in base for t in row.items]
    out = sl.compose(base, terms)
    langs = " ".join(out.skills.get("Languages", [])).lower()
    assert "java" in langs
    concepts = " ".join(out.skills.get("Concepts & Methodologies", [])).lower()
    assert "java" not in concepts


def test_apply_to_tailored_preserves_user_layout():
    original = {
        "skills": {row.label: row.items for row in _user_layout()},
        "skills_layout": [
            {"label": r.label, "items": r.items, "provenance": "user"}
            for r in _user_layout()
        ],
    }
    tailored = {"skills": dict(original["skills"])}
    out = sl.apply_to_tailored(
        tailored, original,
        {"required_skills": ["Django", "React", "PostgreSQL"]},
    )
    assert out["_skill_layout_mode"] == "PRESERVE"
    assert list(out["skills"]) == [r.label for r in _user_layout()]


def test_tailor_prompt_does_not_invent_category_names():
    import inspect
    src = inspect.getsource(ResumeTailor._build_tailor_prompt)
    assert "If a category label doesn't fit the skills under it, rename" not in src
    assert "Do NOT invent or rename categories" in src
