"""§6.12.9 eval — anti-pattern leak: banned words never reach the output.

Contract under test:
  * style.py inventory: a cliché the user NEVER uses lands in the profile's
    ``anti_patterns``; a cliché the user actually writes is NOT banned (it is
    their voice).
  * authored.py Step 4 is a HARD post-filter in code, not a prompt request:
    ``_anti_pattern_re`` + the list comprehension in ``_process_bullet`` drop
    every candidate containing a banned token — any casing, hyphenated forms
    included — BEFORE the entailment judge ever sees it.
  * Word-boundary semantics: banned tokens are matched as whole words
    ("robust" is banned; "robustness" is a different word and survives).

Hard-fail criteria: any banned token, in any casing, present in a surviving
candidate or in the final chosen bullet; or a banned candidate reaching the
judge prompt.
"""
import re

from services.voicegraph import authored, samples, style

USER = "antipattern-eval@example.com"

# Clean corpus — long enough for a profile, zero clichés.
CLEAN_CORPUS = (
    "I keep a plain list of what broke this week and what I did about it. "
    "When the queue backed up on Tuesday, I read the consumer logs line by "
    "line until the pattern was obvious, then I rewrote the retry loop and "
    "watched the graphs settle. I like numbers in a sentence because they "
    "keep me honest: two retries, forty seconds, one bad index. If I cannot "
    "say what changed in one line, I do not understand it yet. My reviews "
    "are short and specific. I ask for the failing case first, then the fix, "
    "then the test that proves it. Most of my writing is like this — small "
    "claims, checked twice, said once. When a plan is shaky I say so early. "
    "When I am wrong I write that down too, so the next decision starts from "
    "what actually happened rather than from what I wish had happened."
)

BANNED_SEEDS = [
    "leveraged", "spearheaded", "utilized", "synergy", "results-driven",
    "cutting-edge", "robust", "dynamic", "passionate", "empowered",
]

ADVERSARIAL = [
    "Leveraged Kafka to modernize the ingestion path.",           # cased
    "SPEARHEADED the migration across three teams.",              # upper
    "We utilized a results-driven approach to planning.",         # two hits
    "Delivered a cutting-edge, world-class synergy platform.",    # stacked
    "I am passionate about dynamic, robust systems.",             # stacked
    "Empowered stakeholders through seamless dashboards.",        # stacked
]

CLEAN_CANDIDATES = [
    "Rewrote the ingestion consumers and cut lag to forty seconds.",
    "The robustness tests passed on the first run.",   # 'robust' as substring only
    "Documented the utilization curve for capacity planning.",  # not 'utilize'
]


def _banned_hits(text, banned):
    """Independent word-boundary scan (not the code under test) for leaks."""
    hits = []
    for b in banned:
        if re.search(r"(?<![A-Za-z])" + re.escape(b) + r"(?![A-Za-z])",
                     text, re.IGNORECASE):
            hits.append(b)
    return hits


# ---------------------------------------------------------------------------
# Inventory — what gets banned, and what must not
# ---------------------------------------------------------------------------
def test_unused_cliches_are_banned_and_used_ones_are_not(mock_db):
    samples.save_sample(USER, CLEAN_CORPUS, finalize=True)
    profile = style.build_profile(USER)
    assert not profile.get("insufficient")
    banned = profile["anti_patterns"]
    for seed in BANNED_SEEDS:
        assert seed in banned, f"unused cliché '{seed}' must be banned"

    # a user who genuinely writes "leveraged" keeps it — it IS their voice
    user2 = "cliche-user@example.com"
    samples.save_sample(
        user2,
        CLEAN_CORPUS + " I leveraged the cache layer to absorb the spike, "
        "and honestly I would say leveraged again — it is the word I use.",
        finalize=True)
    p2 = style.build_profile(user2)
    assert "leveraged" not in p2["anti_patterns"]
    assert "spearheaded" in p2["anti_patterns"]


# ---------------------------------------------------------------------------
# The Step 4 hard filter over adversarial texts
# ---------------------------------------------------------------------------
def test_step4_filter_removes_every_banned_token(mock_db):
    samples.save_sample(USER, CLEAN_CORPUS, finalize=True)
    banned = style.build_profile(USER)["anti_patterns"]
    anti_re = authored._anti_pattern_re(banned)
    assert anti_re is not None

    candidates = ADVERSARIAL + CLEAN_CANDIDATES
    survivors = [c for c in candidates if not anti_re.search(c)]

    # zero banned tokens in the surviving set = pass
    for s in survivors:
        assert _banned_hits(s, banned) == [], f"banned token leaked: {s!r}"
    # every adversarial candidate died; every clean one survived
    for adv in ADVERSARIAL:
        assert adv not in survivors
    for clean in CLEAN_CANDIDATES:
        assert clean in survivors, \
            f"word-boundary matching must not kill: {clean!r}"


# ---------------------------------------------------------------------------
# End-to-end per-bullet: banned candidates never even reach the judge
# ---------------------------------------------------------------------------
def test_banned_candidates_are_filtered_before_the_judge(mock_db, fake_llm):
    samples.save_sample(USER, CLEAN_CORPUS, finalize=True)
    profile = style.build_profile(USER)
    banned = profile["anti_patterns"]
    anti_re = authored._anti_pattern_re(banned)

    src = ("Rewrote the ingestion consumers in Kafka and cut lag from nine "
           "minutes to forty seconds.")
    clean = ("After the consumers kept lagging, rewrote them in Kafka and "
             "cut the delay from nine minutes to forty seconds.")

    def handler(call):
        if "candidates" in call["schema"]:
            return {"candidates": [
                "Leveraged Kafka to spearhead the ingestion rework, cutting "
                "lag from nine minutes to forty seconds.",
                "Utilized a dynamic approach to cut lag from nine minutes "
                "to forty seconds.",
                clean,
            ]}
        # judge approves everything it is shown — the FILTER must be the gate
        results = []
        for m in re.finditer(r"^\[(\d+)\] (.*)$", call["prompt"], re.M):
            results.append({"index": int(m.group(1)), "same_facts": "yes",
                            "added_fact": ""})
        return {"results": results}
    fake_llm.set_handler(handler)

    item = {"section": "experience", "exp_index": 0, "bullet_index": 0,
            "text": src}
    out = authored._process_bullet(item, profile, [], "", banned, anti_re, 1.0)

    assert out["text"] == clean
    assert _banned_hits(out["text"], banned) == []
    # the judge call must never have seen the banned candidates
    judge_calls = [c for c in fake_llm.calls
                   if isinstance(c["schema"], dict) and "results" in c["schema"]]
    assert len(judge_calls) == 1
    assert "Leveraged" not in judge_calls[0]["prompt"]
    assert "Utilized" not in judge_calls[0]["prompt"]
    assert clean in judge_calls[0]["prompt"]
