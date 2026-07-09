"""§6.12.9 eval — the entailment gate (authored.py Step 5) never lets a
changed fact through, and never invents to fill a gap.

Contract under test (real gate code, fake judge verdicts only):
  * A candidate the judge does not certify (``same_facts != "yes"``) is
    dropped — A⊨B direction (a dropped/weakened fact fails).
  * A candidate certified ``same_facts: "yes"`` but with a non-empty
    ``added_fact`` is STILL dropped — B⊨A direction (an added fact fails).
    Both directions are required.
  * A candidate with no verdict at all defaults to DENY.
  * A judge failure (exception) discards ALL candidates.
  * Zero survivors → the Pass A bullet passes through VERBATIM
    (mode ``passthrough``). The pipeline never fabricates a replacement.
  * End-to-end (build_authored_resume): fabricated details (a new volume
    figure, a new employer) produced by the generator are blocked by the
    gate; immutable scaffold fields (employer/title/dates/education/contact/
    skills) are copied through untouched.

Hard-fail criteria: any non-entailed candidate surviving the gate, any
fabricated token reaching the persisted resume, any immutable field changed,
or a zero-survivor bullet that is not the verbatim Pass A text.
"""
import json
import re

import pytest

from services.voicegraph import authored, samples, style

USER = "entailment-eval@example.com"

SRC = ("Rewrote the nightly ingestion consumers in Kafka and cut end-to-end "
       "lag from nine minutes to forty seconds.")

# Reflective, deliberately NON-concrete prose (no digits, no mid-sentence
# proper nouns, no strong verbs) so it feeds the voice profile without ever
# qualifying as a Pass A fact or a verbatim-promotion source.
FILLER = (
    "I think the way we talk about our work matters more than we usually "
    "admit. When something goes wrong, I try to say so plainly, because "
    "hiding it never helps anyone. I like short sentences. I like plain "
    "words. Most of the time the honest version of a story is also the "
    "shortest one, and when it is not, that usually means I have not yet "
    "understood the problem well enough to describe it. I do not enjoy "
    "meetings where nobody says what they mean. If a plan seems shaky, I "
    "would rather hear that early, while there is still time to change "
    "course without drama. My notes are full of questions I never asked out "
    "loud, and every month I try to ask a few more of them anyway. Writing "
    "things down keeps me honest. Reading those notes later keeps me humble "
    "about how much I actually knew at the time. That is the whole method, "
    "and so far it has never once let me down."
)


def _profile():
    """A minimal but real-shaped voice profile for the unit-level tests."""
    return {
        "features": style.compute_features(FILLER),
        "verb_inventory": [],
        "anti_patterns": [],
        "description": [],
    }


def _judge_by_rule(fake_llm, rule):
    """Judge handler: parses '[i] candidate' lines out of the real judge
    prompt and answers with ``rule(candidate_text) -> (same_facts, added)``."""
    def handler(call):
        if isinstance(call["schema"], dict) and "results" in call["schema"]:
            results = []
            for m in re.finditer(r"^\[(\d+)\] (.*)$", call["prompt"], re.M):
                same, added = rule(m.group(2))
                results.append({"index": int(m.group(1)),
                                "same_facts": same, "added_fact": added})
            return {"results": results}
        raise AssertionError(f"unexpected non-judge call: {call['schema']}")
    fake_llm.set_handler(handler)


# ---------------------------------------------------------------------------
# Unit level — the real gate function, canned verdicts
# ---------------------------------------------------------------------------
def test_non_entailed_and_added_fact_candidates_are_dropped(fake_llm):
    candidates = [
        "After the pipeline kept dying, rewrote the Kafka consumers and cut "
        "lag from nine minutes to forty seconds.",           # faithful
        "Rewrote the Kafka consumers, cutting lag to forty seconds for over "
        "ten million daily events.",                          # ADDED fact
        "Reduced pipeline lag.",                              # DROPPED facts
    ]
    fake_llm.queue({"results": [
        {"index": 0, "same_facts": "yes", "added_fact": ""},
        # judge says "same facts" but names an addition → B⊨A fails → drop
        {"index": 1, "same_facts": "yes",
         "added_fact": "ten million daily events"},
        # judge says not equivalent (facts dropped) → A⊨B fails → drop
        {"index": 2, "same_facts": "no", "added_fact": ""},
    ]})
    survivors = authored._judge_entailment(SRC, candidates)
    assert survivors == [candidates[0]]


def test_missing_verdict_defaults_to_deny(fake_llm):
    candidates = ["Faithful rephrase one.", "Unjudged candidate two."]
    fake_llm.queue({"results": [
        {"index": 0, "same_facts": "yes", "added_fact": ""},
        # index 1 intentionally absent from the verdict list
    ]})
    survivors = authored._judge_entailment(SRC, candidates)
    assert survivors == [candidates[0]], \
        "a candidate without a verdict must be denied, not waved through"


def test_judge_failure_discards_all_candidates(fake_llm):
    fake_llm.fail_with(RuntimeError("bedrock unavailable"))
    assert authored._judge_entailment(SRC, ["anything"]) == []


def test_zero_survivors_falls_back_to_verbatim_passthrough(fake_llm):
    """The full per-bullet path: generation succeeds, the judge rejects
    everything → the output is the Pass A bullet, byte-for-byte."""
    def handler(call):
        if "candidates" in call["schema"]:      # Step 3 generation
            return {"candidates": [
                "Overhauled ingestion, cutting lag by ninety-five percent "
                "across four regions."]}         # fabricated detail
        return {"results": [                     # Step 5 judge: reject
            {"index": 0, "same_facts": "no",
             "added_fact": "four regions"}]}
    fake_llm.set_handler(handler)

    item = {"section": "experience", "exp_index": 0, "bullet_index": 0,
            "text": SRC}
    out = authored._process_bullet(item, _profile(), [], "", [], None, 1.0)
    assert out["text"] == SRC, "zero survivors must yield the EXACT source"
    assert out["evidence"]["mode"] == "passthrough"


def test_generation_failure_falls_back_to_passthrough(fake_llm):
    fake_llm.fail_with(RuntimeError("bedrock unavailable"))
    item = {"section": "experience", "exp_index": 0, "bullet_index": 0,
            "text": SRC}
    out = authored._process_bullet(item, _profile(), [], "", [], None, 1.0)
    assert out["text"] == SRC
    assert out["evidence"]["mode"] == "passthrough"


def test_certified_survivor_becomes_voice_matched(fake_llm):
    faithful = ("After the pipeline kept dying, I rewrote the nightly "
                "ingestion consumers in Kafka and end-to-end lag went from "
                "nine minutes to about forty seconds.")
    def handler(call):
        if "candidates" in call["schema"]:
            return {"candidates": [faithful]}
        return {"results": [{"index": 0, "same_facts": "yes",
                             "added_fact": ""}]}
    fake_llm.set_handler(handler)

    item = {"section": "experience", "exp_index": 0, "bullet_index": 0,
            "text": SRC}
    out = authored._process_bullet(item, _profile(), [], "", [], None, 1.0)
    assert out["text"] == faithful
    assert out["evidence"]["mode"] == "voice-matched"


# ---------------------------------------------------------------------------
# End-to-end — build_authored_resume with a stubbed parsed resume.
#
# NOTE: authored._pass_a calls ResumeParser() with no arguments while the
# real class requires a db handle (ResumeParser.__init__(self, db)), so the
# parsed-resume path currently raises inside its try/except and falls back —
# a production bug reported to backend-engineer. The stub below fakes the
# parser at its import boundary so the REAL assemble-in-place, gate and
# immutable-field logic can still be exercised offline.
# ---------------------------------------------------------------------------
STRUCTURED = {
    "contact": {"name": "Priya Raman", "email": "priya@example.com"},
    "summary": "Backend engineer.",
    "skills": {"Data": ["Kafka", "PostgreSQL"]},
    "experience": [{
        "company": "Vantage Analytics", "title": "Backend Engineer",
        "location": "Tempe, AZ", "dates": "May 2022 - Present", "type": "",
        "bullets": [SRC],
    }],
    "education": [{"institution": "Arizona State University",
                   "degree": "M.S. Computer Science", "dates": "2019 - 2021"}],
    "projects": [],
    "certifications": [],
}

FAITHFUL = ("Rewrote the nightly ingestion consumers in Kafka, and "
            "end-to-end lag fell from nine minutes to forty seconds.")
FABRICATED = ("At Google, rewrote the nightly ingestion consumers in Kafka, "
              "cutting lag from nine minutes to forty seconds.")


def test_build_authored_blocks_fabrication_and_keeps_immutables(
        mock_db, fake_llm, monkeypatch):
    import copy

    import services.resume_parser as resume_parser_mod

    class _StubParser:
        def __init__(self, *a, **k):
            pass
        def get_structured_resume(self, user_email=""):
            return {"structured": copy.deepcopy(STRUCTURED)}

    monkeypatch.setattr(resume_parser_mod, "ResumeParser", _StubParser)

    # voice profile source (>=150 ready words, zero overlap with the bullet)
    samples.save_sample(USER, FILLER, finalize=True)

    def handler(call):
        if "candidates" in call["schema"]:
            return {"candidates": [FABRICATED, FAITHFUL]}
        results = []
        for m in re.finditer(r"^\[(\d+)\] (.*)$", call["prompt"], re.M):
            if "Google" in m.group(2):
                results.append({"index": int(m.group(1)),
                                "same_facts": "yes",
                                "added_fact": "at Google"})
            else:
                results.append({"index": int(m.group(1)),
                                "same_facts": "yes", "added_fact": ""})
        return {"results": results}
    fake_llm.set_handler(handler)

    out = authored.build_authored_resume(USER)

    built = out["structured"]
    # the ONLY mutable thing — the bullet — became the certified survivor
    assert built["experience"][0]["bullets"] == [FAITHFUL]
    assert out["provenance"][0]["mode"] == "voice-matched"
    assert out["provenance"][0]["source_bullet"] == SRC

    # fabricated employer never reached any part of the output
    blob = json.dumps(out, default=str)
    assert "Google" not in blob

    # immutable fields copied through EXACTLY (the IntegrityGuard principle)
    assert built["experience"][0]["company"] == "Vantage Analytics"
    assert built["experience"][0]["title"] == "Backend Engineer"
    assert built["experience"][0]["dates"] == "May 2022 - Present"
    assert built["experience"][0]["location"] == "Tempe, AZ"
    assert built["education"] == STRUCTURED["education"]
    assert built["contact"] == STRUCTURED["contact"]
    assert built["skills"] == STRUCTURED["skills"]

    # and the persisted artifact matches
    doc = mock_db.authored_resumes.find_one({"user_email": USER})
    assert doc and doc["provenance_mode"] == "authored"
    assert "Google" not in json.dumps(doc["structured"], default=str)


def test_build_authored_zero_survivors_ships_verbatim_facts(
        mock_db, fake_llm, monkeypatch):
    """If the judge rejects every candidate for every bullet, the authored
    resume is still produced — from the frozen facts, verbatim. Never empty,
    never invented."""
    import copy

    import services.resume_parser as resume_parser_mod

    class _StubParser:
        def __init__(self, *a, **k):
            pass
        def get_structured_resume(self, user_email=""):
            return {"structured": copy.deepcopy(STRUCTURED)}

    monkeypatch.setattr(resume_parser_mod, "ResumeParser", _StubParser)
    samples.save_sample(USER, FILLER, finalize=True)

    def handler(call):
        if "candidates" in call["schema"]:
            return {"candidates": [FABRICATED]}
        results = []
        for m in re.finditer(r"^\[(\d+)\] (.*)$", call["prompt"], re.M):
            results.append({"index": int(m.group(1)), "same_facts": "no",
                            "added_fact": "at Google"})
        return {"results": results}
    fake_llm.set_handler(handler)

    out = authored.build_authored_resume(USER)
    assert out["structured"]["experience"][0]["bullets"] == [SRC]
    assert out["provenance"][0]["mode"] == "passthrough"
    assert "Google" not in json.dumps(out, default=str)
