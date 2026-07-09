"""Voice profile — measured stylometry, not vibes (§6.12.4).

Separate from the knowledge graph: the graph says *what happened*, this says *how
the person talks*. Computed over the user's own writing samples against a small
baseline of general professional English. Everything here is deterministic and
zero-model — importantly, NOTHING optimizes for "sounds human / evades detector"
(see docs/product/authentic-not-undetectable.md). We measure grounding-adjacent,
specificity, and voice; that is all.

`distance(text, profile)` is a real weighted distance over the feature vector,
calibrated so a held-out sample from the SAME user scores lower than a sample from
a DIFFERENT user in the same role family (proven by evals/voice_identity).
"""
import logging
import math
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from utils.db_connect import DBConnect

from services.voicegraph import samples as sample_svc

logger = logging.getLogger(__name__)

MIN_PROFILE_WORDS = 150  # below this we refuse to build a profile (§6.12.2)

# --- lexicons -------------------------------------------------------------
_HEDGES = {
    "kind", "sort", "basically", "maybe", "probably", "somewhat", "roughly",
    "guess", "think", "believe", "perhaps", "possibly", "arguably", "fairly",
    "quite", "rather", "actually", "essentially", "generally", "usually",
}
_SUBORDINATORS = {
    "because", "although", "though", "while", "whereas", "since", "unless",
    "until", "whenever", "wherever", "if", "when", "after", "before", "as",
    "that", "which", "who", "whom", "whose",
}
_CONJUNCTIONS_INITIAL = {"and", "but", "so", "or", "yet", "because"}
# Résumé/LinkedIn clichés. Any of these the user NEVER uses becomes a hard ban
# list at generation time (§6.12.4 anti-patterns).
_CLICHES = {
    "leveraged", "leverage", "spearheaded", "utilized", "utilize", "synergy",
    "passionate", "results-driven", "detail-oriented", "team-player",
    "go-getter", "self-starter", "thought-leader", "impactful", "seamless",
    "cutting-edge", "world-class", "best-in-class", "robust", "holistic",
    "dynamic", "proactively", "spearhead", "orchestrated", "pioneered",
    "revolutionized", "transformative", "storied", "humbled", "excited",
    "delighted", "thrilled", "unlock", "unlocking", "empower", "empowered",
}
# A tiny baseline of common professional-English verbs so the user's verb
# inventory can be ranked by distinctiveness (tf over baseline).
_BASELINE_VERBS = {
    "work", "make", "use", "build", "help", "manage", "create", "develop",
    "lead", "run", "do", "get", "take", "give", "find", "keep", "start",
    "improve", "support", "provide", "handle", "add", "change", "move",
}

_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")
_NUMBER = re.compile(r"\b\d[\d,.%kKmMbB]*\b")
_SENT_SPLIT = re.compile(r"[.!?]+(?:\s+|$)")
_PASSIVE = re.compile(r"\b(was|were|is|are|been|being|be)\b\s+\w+(ed|en)\b", re.IGNORECASE)
_CONTRACTION = re.compile(r"\b\w+'(t|s|re|ve|ll|d|m)\b", re.IGNORECASE)


def _sentences(text: str) -> List[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text) if s.strip()]


def _words(text: str) -> List[str]:
    return _WORD.findall(text)


def compute_features(text: str) -> Dict[str, Any]:
    """Return the measured stylometric feature vector for a body of text."""
    sents = _sentences(text)
    words = _words(text)
    lower = [w.lower() for w in words]
    n_words = max(1, len(words))
    n_sents = max(1, len(sents))

    sent_lens = [len(_words(s)) for s in sents] or [0]
    mean_len = sum(sent_lens) / len(sent_lens)
    var = sum((l - mean_len) ** 2 for l in sent_lens) / len(sent_lens)
    std_len = math.sqrt(var)

    subord = sum(1 for w in lower if w in _SUBORDINATORS)
    hedges = sum(1 for w in lower if w in _HEDGES)
    passives = len(_PASSIVE.findall(text))
    contractions = len(_CONTRACTION.findall(text))
    numbers = len(_NUMBER.findall(text))
    initial_conj = sum(1 for s in sents if (_words(s) or [""])[0].lower() in _CONJUNCTIONS_INITIAL)

    types = set(lower)
    ttr = len(types) / n_words
    # concreteness ~ numbers + mid-sentence capitalized tokens (proper nouns) per 100 words
    proper = 0
    for s in sents:
        sw = _WORD.findall(s)
        for i, w in enumerate(sw):
            if i > 0 and w[0].isupper() and not w.isupper():
                proper += 1
    concreteness = (numbers + proper) * 100.0 / n_words

    features = {
        "sent_len_mean": round(mean_len, 3),
        "sent_len_std": round(std_len, 3),
        "subordination_rate": round(subord / n_sents, 4),
        "passive_rate": round(passives / n_sents, 4),
        "hedge_rate": round(hedges / n_words, 5),
        "contraction_rate": round(contractions / n_words, 5),
        "type_token_ratio": round(ttr, 4),
        "concreteness_index": round(concreteness, 3),
        "initial_conjunction_rate": round(initial_conj / n_sents, 4),
        "numbers_per_100w": round(numbers * 100.0 / n_words, 3),
        "word_count": len(words),
    }
    return features


def _verb_inventory(lower: List[str]) -> List[Dict[str, Any]]:
    """Distinctive verbs: the user's frequent words that are NOT baseline-common,
    ranked by frequency. A pragmatic tf-over-baseline stand-in for tf-idf."""
    counts = Counter(w for w in lower if w.endswith(("ed", "t", "e", "d", "n", "s")) and len(w) > 3)
    ranked = []
    for w, c in counts.most_common(60):
        if w in _BASELINE_VERBS or w in _HEDGES or w in _SUBORDINATORS:
            continue
        ranked.append({"verb": w, "count": c})
        if len(ranked) >= 15:
            break
    return ranked


def _anti_patterns(lower_set: set) -> List[str]:
    """Clichés the user NEVER uses → banned at generation time."""
    return sorted(c for c in _CLICHES if c not in lower_set)


def _col():
    return DBConnect().get_db().style_profiles


def _now() -> datetime:
    return datetime.now(timezone.utc)


def build_profile(user_email: str, persist: bool = True) -> Dict[str, Any]:
    """Compute (and optionally cache) the voice profile from the user's ready
    samples. Refuses (returns {insufficient:True}) below the min signal."""
    texts = [s.get("text", "") for s in sample_svc.list_samples(user_email) if s.get("status") == "ready"]
    sample_ids = [s.get("sample_id") for s in sample_svc.list_samples(user_email) if s.get("status") == "ready"]
    corpus = "\n\n".join(t for t in texts if t)
    total_words = len(_words(corpus))
    if total_words < MIN_PROFILE_WORDS:
        return {
            "insufficient": True,
            "word_count": total_words,
            "needed": MIN_PROFILE_WORDS,
            "message": f"Write about {MIN_PROFILE_WORDS - total_words} more words and we can build your voice profile honestly.",
        }

    lower = [w.lower() for w in _words(corpus)]
    features = compute_features(corpus)
    verb_inventory = _verb_inventory(lower)
    anti_patterns = _anti_patterns(set(lower))

    profile = {
        "user_email": user_email,
        "version": 1,
        "features": features,
        "verb_inventory": verb_inventory,
        "anti_patterns": anti_patterns,
        "sample_ids": sample_ids,
        "computed_at": _now(),
    }
    if persist:
        prev = _col().find_one({"user_email": user_email})
        if prev:
            profile["version"] = int(prev.get("version", 1)) + 1
        _col().update_one({"user_email": user_email}, {"$set": profile}, upsert=True)
    return _public(profile)


def get_profile(user_email: str, rebuild_if_stale: bool = True) -> Dict[str, Any]:
    doc = _col().find_one({"user_email": user_email})
    ready_ids = [s.get("sample_id") for s in sample_svc.list_samples(user_email) if s.get("status") == "ready"]
    if doc and rebuild_if_stale and set(doc.get("sample_ids") or []) != set(ready_ids):
        return build_profile(user_email)
    if not doc:
        return build_profile(user_email)
    return _public(doc)


def _public(p: Dict[str, Any]) -> Dict[str, Any]:
    if p.get("insufficient"):
        return p
    computed = p.get("computed_at")
    return {
        "version": p.get("version", 1),
        "features": p.get("features", {}),
        "verb_inventory": p.get("verb_inventory", []),
        "anti_patterns": p.get("anti_patterns", []),
        "computed_at": computed.isoformat() if hasattr(computed, "isoformat") else computed,
        "description": describe(p),
    }


# --- distance function (the core of the voice_identity eval) --------------
# Feature weights + rough scales for z-like normalization. Tuned so identity
# separation holds; refined by evals/voice_identity in VG Step 9.
_DIST_WEIGHTS = {
    "sent_len_mean": (1.0, 8.0),
    "sent_len_std": (0.6, 5.0),
    "subordination_rate": (1.2, 0.5),
    "passive_rate": (1.0, 0.4),
    "hedge_rate": (1.0, 0.02),
    "contraction_rate": (1.0, 0.03),
    "type_token_ratio": (1.2, 0.15),
    "concreteness_index": (1.4, 8.0),
    "initial_conjunction_rate": (0.8, 0.3),
    "numbers_per_100w": (1.0, 4.0),
}


def distance(text: str, profile: Dict[str, Any]) -> float:
    """Weighted, scale-normalized L2 distance between a text and a voice profile.
    Lower == closer to how this person writes."""
    f = profile.get("features", profile) if isinstance(profile, dict) else {}
    tf = compute_features(text)
    total = 0.0
    wsum = 0.0
    for key, (w, scale) in _DIST_WEIGHTS.items():
        a = float(tf.get(key, 0.0))
        b = float(f.get(key, 0.0))
        d = (a - b) / (scale or 1.0)
        total += w * d * d
        wsum += w
    return math.sqrt(total / wsum) if wsum else 0.0


def describe(profile: Dict[str, Any]) -> List[str]:
    """Plain-English 'here is how you write' (§6.12.7 moment 2). Never a radar
    chart — sentences a person recognizes as true about themselves."""
    f = profile.get("features", {})
    if not f:
        return []
    lines: List[str] = []
    ml = f.get("sent_len_mean", 0)
    if ml and ml < 14:
        lines.append("You write short, declarative sentences.")
    elif ml and ml > 24:
        lines.append("You write long, winding sentences that carry a lot at once.")
    if f.get("passive_rate", 0) < 0.12:
        lines.append("You lead with what you did, not what was done to you, almost always active voice.")
    if f.get("hedge_rate", 0) < 0.004:
        lines.append("You don't hedge. You say what you mean.")
    if f.get("contraction_rate", 0) > 0.01:
        lines.append("You write the way you talk — contractions and all.")
    if f.get("concreteness_index", 0) > 12:
        lines.append("You reach for specifics: real names, real numbers, the actual thing.")
    verbs = [v.get("verb") for v in profile.get("verb_inventory", [])[:5] if v.get("verb")]
    if verbs:
        lines.append("Your verbs: " + ", ".join(f"'{v}'" for v in verbs) + ".")
    never = profile.get("anti_patterns", [])
    for tell in ("leveraged", "utilized", "spearheaded", "passionate"):
        if tell in never:
            lines.append(f"You have never once written '{tell}'.")
            break
    return lines
