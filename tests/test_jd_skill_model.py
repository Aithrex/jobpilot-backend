"""The skill model: what it reads, what it refuses, and that production can run it."""

import gzip
import json
import os
import time

import pytest

from services import fit_score, jd_profile, jd_skill_model as model

JD = """
<h2>About Acme</h2><p>Acme builds logistics software. We love our team.</p>
<h2>Requirements</h2>
<ul>
<li>3+ years of experience with Python, Kubernetes and PostgreSQL</li>
<li>Strong knowledge of Terraform</li>
<li>Excellent communication skills</li>
</ul>
<h2>Benefits</h2>
<ul><li>Medical, dental and vision insurance</li><li>401(k) matching</li></ul>
<h2>Equal Opportunity</h2><p>We consider all applicants regardless of protected veteran status.</p>
"""


def test_weights_ship_with_the_code():
    assert os.path.getsize(model.WEIGHTS_FILE) < 5_000_000
    with gzip.open(model.WEIGHTS_FILE, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    assert {"threshold", "bias", "weights", "trees", "stats", "keep_share",
            "baseline", "negative_rate", "version"} <= set(payload)
    assert 0 < payload["threshold"] < 1
    assert payload["trained_on"] >= 900


def test_clean_keeps_case_and_drops_markup():
    text = model.clean("<li>C&#43;&#43; and <b>SQL</b></li>")
    assert "C++" in text and "SQL" in text and "<" not in text


def test_a_skill_is_never_wrapped_in_filler():
    phrases = {p for p, _ in model.mentions("Requirements:\nStrong customer service skills required")}
    assert "customer service" in phrases
    assert not {"strong customer service", "customer service skills"} & phrases


def test_a_phrase_does_not_cross_a_list_comma():
    phrases = {p for p, _ in model.mentions("Experience with Python, Java and Go")}
    assert "python" in phrases and "java" in phrases
    assert "python java" not in phrases


def test_section_is_read_from_the_heading():
    sections = {f for _, fs in model.mentions("Benefits\nMedical insurance and dental") for f in fs
                if f.startswith("sec=")}
    assert sections == {"sec=benefits"}


def test_reads_skills_and_leaves_benefits_and_legal_out():
    found = model.read_skills(JD)
    assert {"python", "kubernetes"} <= set(found)
    assert not {"medical", "dental", "vision insurance", "protected veteran status"} & set(found)


def test_scores_are_probabilities():
    scores = model.score(JD)
    assert scores and all(0.0 <= v <= 1.0 for v in scores.values())


def test_pick_skips_a_phrase_a_better_one_covers():
    assert model.pick({"data analysis": 0.9, "data": 0.8, "sql": 0.7}, 0.5) == ["data analysis", "sql"]


def test_pick_honours_threshold_and_limit():
    assert model.pick({"a": 0.2}, 0.5) == []
    assert len(model.pick({str(i): 0.9 for i in range(50)}, 0.5, limit=3)) == 3


def test_nothing_to_read_is_nothing_found():
    assert model.read_skills("") == []
    assert model.read_skills(None) == []
    assert model.score("   ") == {}


def test_a_missing_weights_file_costs_nothing(monkeypatch):
    monkeypatch.setattr(model, "_model", None)
    monkeypatch.setattr(model, "WEIGHTS_FILE", "/nonexistent/weights.json.gz")
    assert model.read_skills(JD) == []


def test_without_the_model_the_vocabulary_answers(monkeypatch):
    monkeypatch.setattr(jd_profile.jd_skill_model, "read_skills", lambda *a, **k: [])
    blob = "requirements: experience with inventory management"
    from services.jd_skill_vocabulary import JD_SKILL_VOCABULARY
    term = next(v for v in JD_SKILL_VOCABULARY if " " in v)
    assert term in jd_profile.read_additional_skills(f"{blob} and {term}", [], "text")


def test_additional_skills_leave_out_what_the_lexicon_already_named():
    out = jd_profile.read_additional_skills("", ["python"], JD)
    assert "python" not in out


def test_the_dictionary_carries_model_skills():
    profile = jd_profile.build(JD, "Backend Engineer")
    assert profile["version"] == jd_profile.JD_PROFILE_VERSION
    assert {"python", "kubernetes", "postgresql"} <= set(profile["skills"]["technical"])


def test_match_score_inputs_do_not_depend_on_the_model(monkeypatch):
    """`required_skills` feed `fit_score`, which must stay on the lexicon alone."""
    before = fit_score.extract_requirements(JD, "Backend Engineer")
    fit_score._extract_requirements_cached.cache_clear()
    monkeypatch.setattr(jd_profile.jd_skill_model, "read_skills", lambda *a, **k: ["made up skill"])
    after = fit_score.extract_requirements(JD, "Backend Engineer")
    fit_score._extract_requirements_cached.cache_clear()
    assert before["required_skills"] == after["required_skills"]
    assert before["preferred_skills"] == after["preferred_skills"]


def test_fast_enough_for_the_crawl():
    model._load()
    started = time.perf_counter()
    for _ in range(5):
        model.read_skills(JD * 6)
    assert (time.perf_counter() - started) / 5 < 1.0


@pytest.mark.parametrize("hostile", ["<" * 5000, "a," * 20000, "\n" * 20000, "é" * 30000])
def test_odd_input_does_not_break_it(hostile):
    assert isinstance(model.read_skills(hostile), list)
