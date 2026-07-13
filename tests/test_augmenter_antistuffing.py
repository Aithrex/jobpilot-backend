"""ContentAugmenter keyword-density phase — anti-stuffing alignment tests.

The phase must: skip entirely when required coverage is already >= 80%,
never inject keywords with ZERO occurrences (that's the tailor's
Defensibility call), and cap strengthening at 8 keywords.
"""
import pytest

from services.content_augmenter import ContentAugmenter


def _augmenter() -> ContentAugmenter:
    # renderer/project_generator are unused by the keyword-density phase.
    return ContentAugmenter(renderer=object(), project_generator=object())


def _tailored_with_bullets(bullets):
    return {
        "contact": {}, "summary": "",
        "skills": {},
        "experience": [{
            "title": "Engineer", "company": "Acme", "location": "",
            "dates": "March 2022 - Present", "type": "Full-time",
            "bullets": list(bullets),
        }],
        "education": [], "projects": [], "certifications": [],
    }


@pytest.fixture()
def llm_spy(monkeypatch):
    """Capture claude_json calls; return no modifications by default."""
    calls = []

    def _fake(prompt=None, **kw):
        calls.append({"prompt": prompt, "kw": kw})
        return {"modified_bullets": []}

    import services.claude_client as cc
    monkeypatch.setattr(cc, "claude_json", _fake)
    return calls


def test_skips_when_coverage_in_honest_band(llm_spy):
    # 4 of 5 required present (80%) → phase must not spend an LLM call.
    jd = {"required_skills": ["alphatool", "betatool", "gammatool", "deltatool", "epsilontool"]}
    tailored = _tailored_with_bullets([
        "Used alphatool and betatool for data pipelines",
        "Automated reports with gammatool and deltatool",
    ])
    result = _augmenter()._enforce_keyword_density(tailored, jd)
    assert result is tailored
    assert llm_spy == []


def test_never_injects_absent_keywords(llm_spy):
    # 1 of 3 present (33% < 80%): only the single-occurrence keyword is
    # strengthened; the two absent ones must never reach the prompt.
    jd = {"required_skills": ["alphatool", "betatool", "gammatool"]}
    tailored = _tailored_with_bullets([
        "Built ingestion jobs with alphatool for the analytics team",
    ])
    _augmenter()._enforce_keyword_density(tailored, jd)
    assert len(llm_spy) == 1
    prompt = llm_spy[0]["prompt"]
    assert "alphatool" in prompt
    assert "betatool" not in prompt
    assert "gammatool" not in prompt
    assert "NEVER invent facts" in prompt


def test_caps_strengthening_at_8(llm_spy):
    # 20 required, 10 present exactly once (50% coverage) → at most 8 sent.
    present = [f"presenttool{i}" for i in range(10)]
    absent = [f"absenttool{i}" for i in range(10)]
    jd = {"required_skills": present + absent}
    tailored = _tailored_with_bullets([
        " ".join(f"Shipped features using {t}" for t in present[:5]),
        " ".join(f"Maintained systems on {t}" for t in present[5:]),
    ])
    _augmenter()._enforce_keyword_density(tailored, jd)
    assert len(llm_spy) == 1
    line = next(
        ln for ln in llm_spy[0]["prompt"].splitlines()
        if ln.startswith("Keywords to strengthen:")
    )
    sent = [k.strip() for k in line.split(":", 1)[1].split(",")]
    assert len(sent) <= 8
    assert all(k.startswith("presenttool") for k in sent)


def test_applies_modification_and_preserves_pool(monkeypatch):
    jd = {"required_skills": ["alphatool", "betatool", "gammatool"]}
    tailored = _tailored_with_bullets([
        "Built ingestion jobs with alphatool for the analytics team",
    ])

    def _fake(prompt=None, **kw):
        return {"modified_bullets": [{
            "index": 1,
            "text": "Built ingestion jobs with alphatool, cutting manual load work",
        }]}

    import services.claude_client as cc
    monkeypatch.setattr(cc, "claude_json", _fake)
    result = _augmenter()._enforce_keyword_density(tailored, jd)
    assert result["experience"][0]["bullets"][0].endswith("manual load work")
