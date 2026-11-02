"""The second opinion on years and work mode. Offline."""

import gzip

from services import fit_score, jd_field_model as M, jd_profile


def _reqs(text):
    fit_score._extract_requirements_cached.cache_clear()
    return fit_score._extract_requirements_cached(text, "")


def test_reads_a_plain_onsite_statement_the_rules_miss():
    text = "About the role\nYou will build things.\nFull-time onsite required.\nWe are hiring now."
    assert jd_profile.read_work_mode(fit_score._norm(text)) == "unknown"
    assert _reqs(text)["work_mode"] == "onsite"


def test_years_without_the_word_experience():
    text = "Qualifications\n- 5+ Years of Commercial Banking\n- Strong credit analysis"
    assert fit_score._required_years(fit_score._norm(text)) is None
    assert _reqs(text)["required_years"] == 5


def test_a_person_age_or_company_age_is_not_experience():
    text = ("Founded over 50 years ago, we serve millions.\nRequirements\n"
            "Must be at least 18 years of age.\nValid driver's license.")
    assert _reqs(text)["required_years"] is None


def test_preferred_only_stays_preferred():
    text = "Qualifications\n2 years of relevant experience preferred.\nHigh school diploma."
    reqs = _reqs(text)
    assert reqs["required_years"] == 2
    experience = jd_profile.build(text, "")["experience"]
    assert experience["min_years"] is None and experience["preferred_years"] == 2


def test_no_mentions_is_no_years():
    assert M.check_years("We make great software.", None) is None


def test_rule_answer_survives_unreadable_weights(monkeypatch, tmp_path):
    bad = tmp_path / "weights.json.gz"
    bad.write_bytes(gzip.compress(b"{not json"))
    monkeypatch.setattr(M, "WEIGHTS_FILE", str(bad))
    monkeypatch.setattr(M, "_model", None)
    assert M.check_work_mode("Hybrid role in Austin", "hybrid") == "hybrid"
    assert M.check_years("5 years of experience", 5) == 5


def test_rule_answer_survives_a_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(M, "WEIGHTS_FILE", str(tmp_path / "gone.json.gz"))
    monkeypatch.setattr(M, "_model", None)
    assert M.check_work_mode("anything", "remote") == "remote"


def test_empty_posting_keeps_the_title_reading():
    fit_score._extract_requirements_cached.cache_clear()
    assert fit_score._extract_requirements_cached("", "Data Engineer - Remote")["work_mode"] == "remote"


def test_extraction_stays_fast():
    import time
    text = ("Requirements\n- 5+ years of Python\n- Hybrid, 3 days a week in office\n" * 20)
    M.check_work_mode(text, "hybrid")
    start = time.perf_counter()
    for _ in range(20):
        M.check_years(text, 5)
        M.check_work_mode(text, "hybrid")
    assert (time.perf_counter() - start) / 20 < 0.05
