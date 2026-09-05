"""Unit tests for the skill -> heading map (ResumeTailor._build_skill_headings).

The map decides where a skill belongs, once per tailoring run, in three tiers:
base placements are frozen, unplaced JD terms go to a FLASH pass, and anything
still unplaced is simply absent so the caller's taxonomy chain runs as before.

The LLM is stubbed throughout — these assert the tiering and the validator, not
model quality.
"""
import pytest

from services import resume_tailor as rt
from services.resume_tailor import ResumeTailor, SkillHeadings


BASE = {
    "skills": {
        "Languages": ["Python", "TypeScript"],
        "Frameworks & Libraries": ["React", "Flask"],
        "Tools & Platforms": ["Docker", "Git"],
    }
}

JD = {
    "job_title": "AI Engineer",
    "required_skills": ["Claude API", "Gemini API", "LangChain"],
    "keywords": ["Kafka"],
}


@pytest.fixture()
def flag_on(monkeypatch):
    monkeypatch.setattr(rt, "SKILLS_CATEGORIZER", True, raising=False)


def stub_placements(monkeypatch, assignments, raises=None):
    """Stub the FLASH call with a canned assignments payload."""
    def _fake(**kwargs):
        if raises:
            raise raises
        return {"assignments": assignments}

    import services.claude_client as cc
    monkeypatch.setattr(cc, "claude_json", _fake)


# ---------------------------------------------------------------------------
# Tier 1 — base placements are frozen
# ---------------------------------------------------------------------------

def test_base_skill_keeps_its_own_heading(flag_on, monkeypatch):
    """Even when the model says otherwise, what the user wrote wins."""
    stub_placements(monkeypatch, [
        {"skill": "Python", "heading": "Tools & Platforms"},
        {"skill": "Claude API", "heading": "Tools & Platforms"},
    ])
    headings = ResumeTailor._build_skill_headings(BASE, JD)
    assert headings.mapping["python"] == "Languages"
    assert headings.mapping["react"] == "Frameworks & Libraries"


def test_base_skills_are_never_sent_to_the_model(flag_on, monkeypatch):
    seen = {}

    def _fake(**kwargs):
        seen["prompt"] = kwargs.get("prompt", "")
        return {"assignments": []}

    import services.claude_client as cc
    monkeypatch.setattr(cc, "claude_json", _fake)
    ResumeTailor._build_skill_headings(BASE, JD)

    terms = seen["prompt"].split("terms_to_place")[1]
    assert "Claude API" in terms and "Kafka" in terms
    # Python is base-placed, so it is context, never something to re-file.
    assert "- Python\n" not in terms


def test_no_base_skills_yields_an_empty_map():
    assert ResumeTailor._build_skill_headings({}, JD) == SkillHeadings({}, [], None)


# ---------------------------------------------------------------------------
# Tier 2 — the placement pass
# ---------------------------------------------------------------------------

def test_unplaced_jd_term_lands_in_a_base_heading(flag_on, monkeypatch):
    stub_placements(monkeypatch, [
        {"skill": "Claude API", "heading": "Tools & Platforms"},
        {"skill": "Kafka", "heading": "Tools & Platforms"},
    ])
    headings = ResumeTailor._build_skill_headings(BASE, JD)
    assert headings.mapping["claude api"] == "Tools & Platforms"
    assert headings.mapping["kafka"] == "Tools & Platforms"
    assert headings.admitted is None


def test_flag_off_skips_the_pass_entirely(monkeypatch):
    monkeypatch.setattr(rt, "SKILLS_CATEGORIZER", False, raising=False)

    def _explode(**kwargs):
        raise AssertionError("the placement pass must not run with the flag off")

    import services.claude_client as cc
    monkeypatch.setattr(cc, "claude_json", _explode)

    headings = ResumeTailor._build_skill_headings(BASE, JD)
    assert "claude api" not in headings.mapping
    assert headings.mapping["python"] == "Languages"


def test_map_is_findable_by_the_literal_token_too(flag_on, monkeypatch):
    """The frontend has no alias table, so both key forms must be present."""
    base = {"skills": {"Databases": ["Postgres"], "Tools": ["Git"]}}
    stub_placements(monkeypatch, [])
    headings = ResumeTailor._build_skill_headings(base, {"required_skills": []})
    assert headings.mapping["postgres"] == "Databases"
    assert headings.mapping[ResumeTailor._skill_key("Postgres")] == "Databases"


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

def test_model_reword_never_reaches_the_map(flag_on, monkeypatch):
    """The model's text is a lookup key only; our string is what survives."""
    stub_placements(monkeypatch, [
        {"skill": "claude api", "heading": "Tools & Platforms"},
    ])
    headings = ResumeTailor._build_skill_headings(BASE, JD)
    # Keyed off OUR term, and the model's casing did not create a second entry.
    assert headings.mapping["claude api"] == "Tools & Platforms"
    assert all(k == k.lower() for k in headings.mapping)


def test_unknown_term_is_dropped_not_fatal(flag_on, monkeypatch):
    stub_placements(monkeypatch, [
        {"skill": "Rust", "heading": "Languages"},
        {"skill": "Kafka", "heading": "Tools & Platforms"},
    ])
    headings = ResumeTailor._build_skill_headings(BASE, JD)
    assert "rust" not in headings.mapping
    assert headings.mapping["kafka"] == "Tools & Platforms"


def test_malformed_response_falls_back_cleanly(flag_on, monkeypatch):
    stub_placements(monkeypatch, ["not-a-dict", {"heading": "Languages"}])
    headings = ResumeTailor._build_skill_headings(BASE, JD)
    assert "claude api" not in headings.mapping
    assert headings.mapping["python"] == "Languages"


def test_exception_leaves_the_taxonomy_chain_in_charge(flag_on, monkeypatch):
    stub_placements(monkeypatch, [], raises=RuntimeError("bedrock down"))
    headings = ResumeTailor._build_skill_headings(BASE, JD)
    assert headings.admitted is None
    assert "claude api" not in headings.mapping
    # Tier 1 still stands — it never needed the model.
    assert headings.mapping["python"] == "Languages"


# ---------------------------------------------------------------------------
# The one novel heading
# ---------------------------------------------------------------------------

def test_novel_heading_needs_three_terms(flag_on, monkeypatch):
    stub_placements(monkeypatch, [
        {"skill": "Claude API", "heading": "AI & ML"},
        {"skill": "Gemini API", "heading": "AI & ML"},
    ])
    headings = ResumeTailor._build_skill_headings(BASE, JD)
    assert headings.admitted is None
    assert "claude api" not in headings.mapping


def test_novel_heading_admitted_with_three(flag_on, monkeypatch):
    stub_placements(monkeypatch, [
        {"skill": "Claude API", "heading": "AI & ML"},
        {"skill": "Gemini API", "heading": "AI & ML"},
        {"skill": "LangChain", "heading": "AI & ML"},
    ])
    headings = ResumeTailor._build_skill_headings(BASE, JD)
    assert headings.admitted == "AI & ML"
    assert headings.mapping["claude api"] == "AI & ML"
    assert headings.order[-1] == "AI & ML"


def test_only_one_novel_heading_survives(flag_on, monkeypatch):
    jd = {"required_skills": ["Claude API", "Gemini API", "LangChain", "Kafka",
                              "Kinesis", "Pulsar"]}
    stub_placements(monkeypatch, [
        {"skill": "Claude API", "heading": "AI & ML"},
        {"skill": "Gemini API", "heading": "AI & ML"},
        {"skill": "LangChain", "heading": "AI & ML"},
        {"skill": "Kafka", "heading": "Streaming"},
        {"skill": "Kinesis", "heading": "Streaming"},
        {"skill": "Pulsar", "heading": "Streaming"},
    ])
    headings = ResumeTailor._build_skill_headings(BASE, jd)
    assert headings.admitted in {"AI & ML", "Streaming"}
    minted = {v for v in headings.mapping.values()} - set(BASE["skills"])
    assert minted == {headings.admitted}


def test_admitted_heading_is_allowed_through_polish():
    """_labels_with_admitted must widen the pin, or _recategorize_skills
    evicts the new heading's members straight back out."""
    headings = SkillHeadings({"claude api": "AI & ML"},
                             ["Languages", "AI & ML"], "AI & ML")
    labels = ResumeTailor._labels_with_admitted(BASE, headings)
    assert "AI & ML" in labels
    assert "Languages" in labels


# ---------------------------------------------------------------------------
# Consumption
# ---------------------------------------------------------------------------

def test_category_for_new_skill_prefers_the_map():
    """Without the map, taxonomy_group('Claude API') -> ai_ml -> 'concept',
    which finds no concept heading here and dumps it in the tail."""
    merged = {k: list(v) for k, v in BASE["skills"].items()}
    allowed = set(merged)

    plain = ResumeTailor._category_for_new_skill(
        "Claude API", merged, {}, allowed=allowed)
    mapped = ResumeTailor._category_for_new_skill(
        "Claude API", merged, {}, allowed=allowed,
        headings=SkillHeadings({"claude api": "Frameworks & Libraries"}, [], None),
    )
    assert mapped == "Frameworks & Libraries"
    assert plain == "Tools & Platforms"


def test_kind_bucket_routes_api_to_tools_without_map():
    merged = {k: list(v) for k, v in BASE["skills"].items()}
    cat = ResumeTailor._category_for_new_skill(
        "Gemini API", merged, {}, allowed=set(merged),
    )
    assert cat == "Tools & Platforms"


def test_jd_skill_display_preserves_api_label():
    assert ResumeTailor._jd_skill_display("Claude API") == "Claude API"
    assert ResumeTailor._jd_skill_display("PostgreSQL") == "PostgreSQL"


def test_map_entry_outside_allowed_is_ignored():
    merged = {k: list(v) for k, v in BASE["skills"].items()}
    cat = ResumeTailor._category_for_new_skill(
        "Claude API", merged, {}, allowed=set(merged),
        headings=SkillHeadings({"claude api": "Nonexistent"}, [], None),
    )
    assert cat in merged


def test_sidecar_carries_map_and_order():
    headings = SkillHeadings({"python": "Languages"}, ["Languages", "Tools"], None)
    out = ResumeTailor._stamp_skill_headings({"skills": {}}, headings)
    assert out["_skill_headings"] == {
        "map": {"python": "Languages"},
        "order": ["Languages", "Tools"],
    }


def test_sidecar_absent_when_there_is_nothing_to_say():
    out = ResumeTailor._stamp_skill_headings({"skills": {}}, SkillHeadings({}, [], None))
    assert "_skill_headings" not in out


def test_sidecar_is_stripped_before_reentering_a_prompt():
    from services.resume_tailor import strip_sidecar_keys
    stripped = strip_sidecar_keys(
        {
            "summary": "x",
            "_skill_headings": {"map": {}, "order": []},
            "summary_write": {"source": "base", "reasons": ["shape"]},
        }
    )
    assert "_skill_headings" not in stripped
    assert "summary_write" not in stripped


# ---------------------------------------------------------------------------
# Through the real coverage pass
# ---------------------------------------------------------------------------

def _resume_mentioning(text):
    return {
        "skills": dict(BASE["skills"]),
        "experience": [{"title": "Engineer", "company": "Acme", "bullets": [text]}],
    }


def test_evidenced_jd_skill_lands_where_the_map_says(flag_on, monkeypatch):
    monkeypatch.setattr(rt, "SKILLS_PRESERVE_LAYOUT", True, raising=False)
    original = _resume_mentioning("Shipped a chat feature on the Claude API.")
    stub_placements(monkeypatch, [
        {"skill": "Claude API", "heading": "Tools & Platforms"},
    ])
    headings = ResumeTailor._build_skill_headings(original, JD)
    out = ResumeTailor._ensure_jd_skills_coverage(
        {"skills": {}}, original, JD, headings=headings,
    )
    added = set(out["skills"]["Tools & Platforms"]) - {"Docker", "Git"}
    assert added == {"Claude API"}, out["skills"]
    assert not (set(out["skills"]["Frameworks & Libraries"]) - {"React", "Flask"})


def test_admitted_heading_reaches_the_document(flag_on, monkeypatch):
    original = _resume_mentioning(
        "Built agents on the Claude API and Gemini API using LangChain."
    )
    stub_placements(monkeypatch, [
        {"skill": "Claude API", "heading": "AI & ML"},
        {"skill": "Gemini API", "heading": "AI & ML"},
        {"skill": "LangChain", "heading": "AI & ML"},
    ])
    headings = ResumeTailor._build_skill_headings(original, JD)
    out = ResumeTailor._ensure_jd_skills_coverage(
        {"skills": {}}, original, JD, headings=headings,
    )
    assert "AI & ML" in out["skills"]

    # And it must survive polish, which is what _labels_with_admitted is for.
    polished = ResumeTailor._polish_skills(
        out, JD, base_labels=ResumeTailor._labels_with_admitted(original, headings),
    )
    assert "AI & ML" in polished["skills"]


# ---------------------------------------------------------------------------
# Non-software resumes
# ---------------------------------------------------------------------------

def test_clinical_resume_headings_are_untouched(flag_on, monkeypatch):
    base = {
        "skills": {
            "Clinical Systems": ["Epic", "Cerner"],
            "Patient Care": ["Triage", "Medication Administration"],
        }
    }
    jd = {"job_title": "ICU Nurse", "required_skills": ["ACLS"], "keywords": []}
    stub_placements(monkeypatch, [{"skill": "ACLS", "heading": "Patient Care"}])

    headings = ResumeTailor._build_skill_headings(base, jd)
    assert headings.order == ["Clinical Systems", "Patient Care"]
    assert headings.mapping["epic"] == "Clinical Systems"
    assert headings.mapping["acls"] == "Patient Care"
    assert headings.admitted is None
