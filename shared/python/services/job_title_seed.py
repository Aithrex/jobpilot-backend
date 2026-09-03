"""Job-search title seed across ALL parsed resumes, cached by resume-set fingerprint.

Union of ``job_titles`` on each parsed ``resume_assets`` row is free (already
extracted at parse time). The LLM title synthesizer (``suggest_filters``)
runs ONLY when the resume set fingerprint changes AND we have no stored
synth for that fingerprint. Upload / delete / reparse changes the
fingerprint; browsing Jobs does not.
"""
from __future__ import annotations

import functools
import hashlib
import re
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from services import resume_access
from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)

TITLE_CAP = 40
_COL = "job_filter_preferences"

# Same-container lock so Jobs hydrate + Daily Pipeline mounting together
# cannot fire two PRO synths for one user. Cross-Lambda races are still
# possible; the fingerprint re-check immediately before the LLM call
# collapses most of those.
_synth_locks: Dict[str, threading.Lock] = {}
_synth_locks_guard = threading.Lock()


def _lock_for(user_email: str) -> threading.Lock:
    with _synth_locks_guard:
        return _synth_locks.setdefault(user_email, threading.Lock())


def _db():
    return DBConnect().get_db()


def _iso(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value or "")


def fingerprint_assets(assets: List[Dict[str, Any]]) -> str:
    """Stable id of the parsed-resume set. Changes iff an asset is added,
    removed, or re-parsed."""
    parts = []
    for asset in sorted(assets, key=lambda a: str(a.get("resume_id") or "")):
        rid = str(asset.get("resume_id") or "")
        if not rid:
            continue
        parts.append(f"{rid}:{_iso(asset.get('parsed_at'))}")
    if not parts:
        return ""
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]


def union_titles_from_assets(assets: List[Dict[str, Any]]) -> List[str]:
    """Case-insensitive merge of stored job_titles, newest assets first."""
    seen: set = set()
    out: List[str] = []
    for asset in assets:
        raw = asset.get("job_titles") or []
        if not raw:
            structured = asset.get("structured") or {}
            if isinstance(structured, dict):
                from schemas.resume_schemas import extract_job_titles

                raw = extract_job_titles(structured)
        if not isinstance(raw, list):
            continue
        for title in raw:
            text = str(title or "").strip()
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(text)
            if len(out) >= TITLE_CAP:
                return out
    return out


#: Titles that carry the software market and must never be cut by `TITLE_CAP`.
#: Every one is a canonical phrase in `tech_taxonomy` and every one matches
#: live inventory: "Software Engineer" alone matches 4,412 of 30,335 active
#: core-tech postings, "Software Development Engineer" 445 (it is what Amazon,
#: and only Amazon at this volume, posts under).
_ALWAYS_MATCHABLE = (
    "Software Engineer", "Software Developer", "Software Development Engineer",
)


#: Words whose natural casing `str.title()` gets wrong. The vocabulary is
#: stored lower-case and matching is case-insensitive, so this is purely how
#: the chip reads to the user - but a feed that offers "Devops Engineer" and
#: "Sre" looks broken, and a user who does not trust the chips edits them.
_TITLE_CASE_FIXES = {
    "Devops": "DevOps", "Devsecops": "DevSecOps", "Sre": "SRE", "Ai": "AI",
    "Ml": "ML", "Mlops": "MLOps", "Ui": "UI", "Ux": "UX", "Qa": "QA",
    "Ios": "iOS", "Sde": "SDE", "Swe": "SWE", "Api": "API", "Sdet": "SDET",
    "Nlp": "NLP", "Llm": "LLM", "Etl": "ETL", "Bi": "BI", "It": "IT",
    "Cicd": "CI/CD", "Genai": "GenAI", "Gitops": "GitOps", "Mts": "MTS",
}

#: Phrases that are a DIFFERENT JOB, not a level variant of the same one. A
#: search-title vocabulary for an individual contributor must never contain
#: them: "software engineering manager" matches with the same word boundaries
#: as "software engineer" and would put people-management reqs in an IC feed.
_NEVER_SEARCH = ("manager", "director", "head of", "vp ", "vice president")

#: Bare gerunds. `tech_taxonomy` carries them because they CLASSIFY a posting
#: correctly, but they are a discipline rather than a role: as a search term
#: "software engineering" matches "Software Engineering Manager" (and not
#: "Software Engineer", which is a different token), so it readmits exactly
#: what `_NEVER_SEARCH` removes.
_NOT_A_ROLE = frozenset({
    "software engineering", "software development", "devops", "devsecops",
    "site reliability engineering", "platform engineering", "data engineering",
    "machine learning", "cloud computing", "quality assurance",
})


#: A presentable role name ends in one of these. It is what separates
#: "Backend Engineer" from "MTS Software" inside the same vocabulary.
_ROLE_NOUNS = frozenset({
    "engineer", "developer", "architect", "scientist", "analyst",
    "administrator", "programmer", "sre", "specialist", "lead",
})


#: Whole-phrase spellings `str.title()` cannot reach, because the fix spans
#: more than one word ("ci cd" -> "CI/CD").
_PHRASE_CASE_FIXES = {
    "ci cd engineer": "CI/CD Engineer",
    "ci cd": "CI/CD",
}


def _display_title(phrase: str) -> str:
    fixed = _PHRASE_CASE_FIXES.get(phrase.lower())
    if fixed:
        return fixed
    return " ".join(_TITLE_CASE_FIXES.get(w, w) for w in phrase.title().split())


@functools.lru_cache(maxsize=1)
def _intern_phrases() -> Tuple[Tuple[str, str], ...]:
    """`(intern phrase, its specialty)`, longest phrase first."""
    from services import tech_taxonomy as tx

    out = []
    for spec in tx.ALL_SPECIALTIES:
        for phrase in tx.title_phrases(specialties=[spec]):
            low = phrase.lower()
            if "intern" in low:
                out.append((low, spec))
    out.sort(key=lambda pair: len(pair[0]), reverse=True)
    return tuple(out)


@functools.lru_cache(maxsize=256)
def _adult_phrase_for(specialty: str) -> Optional[str]:
    """The first presentable NON-intern role name in a specialty, or None."""
    from services import tech_taxonomy as tx

    for phrase in tx.title_phrases(specialties=[specialty]):
        low = phrase.lower()
        if "intern" in low or low in _NOT_A_ROLE:
            continue
        if any(bad in low for bad in _NEVER_SEARCH):
            continue
        words = low.split()
        if 2 <= len(words) <= 4 and words[-1] in _ROLE_NOUNS:
            return low
    return None


def canonical_search_titles(titles: Sequence[Any],
                            specialties: Sequence[Any] = (),
                            *, level: Optional[str] = None,
                            cap: int = TITLE_CAP) -> List[str]:
    """Rewrite generated titles into ones that can actually MATCH a posting.

    The search side compiles a title list into
    ``(?<![A-Za-z0-9_])(?:escaped|escaped)(?![A-Za-z0-9_])`` and runs it
    against ``job_postings.title_raw`` - a CONTIGUOUS, literal match. The LLM
    synthesizer was prompted to emit keyword-style strings instead ("Backend
    Engineer Python", "Cloud Engineer AWS Terraform"), which are perfectly
    good queries for a Workday ``titleSearch`` box and match nothing at all
    here. Measured live: 34 of one account's 44 titles matched zero postings
    and the whole set reached 0.29% of the eligible pool.

    So this trims each title back to the longest CANONICAL phrase inside it -
    the `tech_taxonomy` vocabulary, whose authoring rule is already "a phrase
    belongs here only if, on its own in a job title, it is unambiguous", which
    is exactly the property a search term needs. "Backend Engineer Python"
    becomes "Backend Engineer"; "Software Engineer II Cloud" becomes
    "Software Engineer". Losing the stack token loses nothing real: `fit_score`
    ranks on skills far better than a title substring ever could, and the
    stack words remain available to the caller as `custom_role_terms`.

    A title with no canonical phrase in it is DROPPED rather than passed
    through, because passing it through is what produced a feed of zero.

    `level` is the candidate's band (`fit_score.resume_level`). Past
    internships dominate an early-career resume's title list - three of the
    five on the account that prompted this - and an intern search term is not
    a level variant, it is a different job: unfiltered it made "Software
    Engineering Intern" one of only two titles that matched anything at all.
    Intern phrases survive only for an actual intern.
    """
    from services import tech_taxonomy as tx

    # The FULL vocabulary, not the candidate's own specialties. Restricting it
    # to the specialties the taxonomy inferred from the resume threw away real
    # targets: the account this was built for classifies as
    # {general_software, devops, backend}, so narrowing dropped the "Platform
    # Engineer" and "Site Reliability Engineer" titles his own Cloud-Platform
    # resume had asked for. The INPUT list is what expresses intent; this
    # vocabulary only decides how a title is spelled so it can match.
    # `specialties`, when given, ADDS its phrases - it never subtracts.
    vocab = list(tx.title_phrases())
    vocab.extend(tx.title_phrases(specialties=specialties) if specialties else [])
    keep_intern = str(level or "").lower() == "intern"
    usable = []
    for phrase in {str(p).lower() for p in vocab}:
        if phrase in _NOT_A_ROLE:
            continue
        if any(bad in phrase for bad in _NEVER_SEARCH):
            continue
        if not keep_intern and "intern" in phrase:
            continue
        usable.append(phrase)
    # Longest first, so "backend software engineer" wins over "software
    # engineer" on a title that contains both.
    usable.sort(key=len, reverse=True)
    patterns = [(p, re.compile(r"(?<![a-z0-9_])" + re.escape(p) + r"(?![a-z0-9_])"))
                for p in usable]

    out: List[str] = []
    seen: set = set()

    def _add(phrase: str) -> None:
        key = phrase.lower()
        if key not in seen:
            seen.add(key)
            out.append(phrase)

    software_vocab = {p.lower() for p in tx.title_phrases(
        families=["software_engineering", "cloud_platform"])}
    matched_software = False

    for raw in titles or []:
        text = re.sub(r"\s+", " ", str(raw or "")).strip().lower()
        if not text:
            continue
        for phrase, pattern in patterns:
            if pattern.search(text):
                _add(_display_title(phrase))
                if phrase in software_vocab:
                    matched_software = True
                break

    # The software floor goes in HERE, before the specialty expansion below,
    # because `cap` truncates and these three are the titles that carry the
    # market. Appended after the expansion they were the first to be cut.
    if matched_software:
        for phrase in _ALWAYS_MATCHABLE:
            _add(phrase)

    if not out and not keep_intern:
        # Every title this resume states is an INTERNSHIP, and the candidate
        # is not an intern. Stripping intern phrases from the vocabulary is
        # what made them match nothing, so the pass-through below would hand
        # the intern titles straight back unfiltered - the intern filter
        # defeating itself, and exactly the "why is an intern title in my
        # list" the change was made to stop. Read the intern phrase, then
        # search for the SAME specialty's ordinary role name instead: a
        # "Data Science Intern" should be searching "Data Scientist".
        for raw in titles or []:
            text = re.sub(r"\s+", " ", str(raw or "")).strip().lower()
            if not text:
                continue
            for phrase, spec in _intern_phrases():
                if re.search(r"(?<![a-z0-9_])" + re.escape(phrase) + r"(?![a-z0-9_])",
                             text):
                    grown = _adult_phrase_for(spec)
                    if grown:
                        _add(_display_title(grown))
                    break

    if not out:
        # Nothing in this resume is a technical role. `tech_taxonomy` is a
        # TECH vocabulary and has no phrase for "Registered Nurse", so
        # canonicalizing here would hand a nurse an empty list. Give the
        # caller their own titles back: worse search terms than a canonical
        # phrase, but the right occupation, which matters far more.
        #
        # This returns BEFORE the specialty expansion below on purpose. A
        # non-tech resume that picks up one stray tech specialty would
        # otherwise be handed a pure software list with its own occupation
        # erased - measured: a nurse plus `general_software` produced eleven
        # software titles and zero nursing ones.
        kept: List[str] = []
        for raw in titles or []:
            text = str(raw or "").strip()
            if not text:
                continue
            if not keep_intern:
                # The vocabulary had no phrase for this title, so the
                # substitution above could not run - but handing a non-intern
                # "Data Science Intern" is still wrong, and that term matches
                # ONLY internships. Dropping the qualifier widens it to the
                # discipline, which is the closest honest reading available.
                text = re.sub(r"[\s,\-/|]*\b(intern|internship)\b[\s.]*$", "",
                              text, flags=re.IGNORECASE).strip(" ,-/|")
            if text:
                kept.append(text)
        return kept[:cap]

    # Breadth from the SPECIALTIES the taxonomy read off the resume, not only
    # from whatever the synthesizer happened to name. A candidate classified
    # {general_software, devops, backend} should be searching every canonical
    # role name in those specialties, and the LLM reliably names only a few of
    # them - the account this was built for got 16 titles and asked, fairly,
    # why so few.
    #
    # Gated on presentability, because these are also the CHIPS a user reads.
    # `_SPEC_RULES` is a classifier vocabulary and carries forms that classify
    # well but read as broken in a filter rail - "MTS Software", "Software
    # Engr", "Development Engineer Software".
    #
    # And gated on FAMILY, which is the expensive lesson. `inventory_feed.
    # _role_families` unions the family of EVERY title it is given, so one
    # expansion title in the wrong family admits that whole family to
    # retrieval. `classify_role` reads "Server Side Engineer" as HOSPITALITY -
    # a restaurant server - and "Product Engineer" as hardware; measured live,
    # those two alone made 33% of one account's career-pages feed off-domain
    # and put "Embedded Firmware and Software Engineer" at rank 2 with a score
    # of 89. An expansion phrase is admitted only when it reads as the same
    # kind of work the candidate's own titles do.
    from services import fit_score

    home = {f for f in (fit_score.classify_role(t) for t in out) if f}
    for phrase in tx.title_phrases(specialties=specialties) if specialties else []:
        low = phrase.lower()
        if low in _NOT_A_ROLE or any(bad in low for bad in _NEVER_SEARCH):
            continue
        if not keep_intern and "intern" in low:
            continue
        words = low.split()
        if not (2 <= len(words) <= 4) or words[-1] not in _ROLE_NOUNS:
            continue
        fam = fit_score.classify_role(phrase)
        if home and fam is not None and fam not in home:
            continue
        _add(_display_title(low))

    return out[:cap]


def merge_structured_for_titles(assets: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """One structured doc covering every parsed resume — one LLM call, not N."""
    if not assets:
        return None
    summaries: List[str] = []
    experience: List[Dict[str, Any]] = []
    projects: List[Dict[str, Any]] = []
    education: List[Dict[str, Any]] = []
    certifications: List[str] = []
    skills: Dict[str, List[str]] = {}
    seen_exp: set = set()
    seen_proj: set = set()
    seen_edu: set = set()
    seen_cert: set = set()
    seen_skill: Dict[str, set] = {}

    for asset in assets:
        structured = asset.get("structured")
        if not isinstance(structured, dict):
            continue
        fname = (asset.get("filename") or "resume").strip()
        summary = (structured.get("summary") or "").strip()
        if summary:
            summaries.append(f"{fname}: {summary[:400]}")
        for exp in structured.get("experience") or []:
            if not isinstance(exp, dict):
                continue
            key = (
                (exp.get("title") or "").strip().lower(),
                (exp.get("company") or "").strip().lower(),
            )
            if not key[0] or key in seen_exp:
                continue
            seen_exp.add(key)
            experience.append(exp)
        for proj in structured.get("projects") or []:
            if not isinstance(proj, dict):
                continue
            key = (proj.get("name") or "").strip().lower()
            if not key or key in seen_proj:
                continue
            seen_proj.add(key)
            projects.append(proj)
        for edu in structured.get("education") or []:
            if not isinstance(edu, dict):
                continue
            key = (
                (edu.get("degree") or "").strip().lower(),
                (edu.get("institution") or "").strip().lower(),
            )
            if key in seen_edu:
                continue
            seen_edu.add(key)
            education.append(edu)
        for cert in structured.get("certifications") or []:
            text = str(cert).strip()
            if not text:
                continue
            key = text.lower()
            if key in seen_cert:
                continue
            seen_cert.add(key)
            certifications.append(text)
        raw_skills = structured.get("skills") or {}
        if isinstance(raw_skills, dict):
            for cat, items in raw_skills.items():
                if not isinstance(items, list):
                    continue
                bucket = skills.setdefault(str(cat), [])
                seen_cat = seen_skill.setdefault(str(cat), set())
                for item in items:
                    text = str(item).strip()
                    if not text or text.lower() in seen_cat:
                        continue
                    seen_cat.add(text.lower())
                    bucket.append(text)
        elif isinstance(raw_skills, list):
            bucket = skills.setdefault("Skills", [])
            seen_cat = seen_skill.setdefault("Skills", set())
            for item in raw_skills:
                text = str(item).strip()
                if not text or text.lower() in seen_cat:
                    continue
                seen_cat.add(text.lower())
                bucket.append(text)

    if not experience and not summaries:
        return None
    return {
        "summary": " | ".join(summaries)[:1200],
        "experience": experience[:16],
        "projects": projects[:10],
        "education": education[:6],
        "certifications": certifications[:12],
        "skills": skills,
    }


def load_title_seed(user_email: str) -> Dict[str, Any]:
    if not user_email:
        return {}
    doc = _db()[_COL].find_one({"user_email": user_email}, {"title_seed": 1}) or {}
    seed = doc.get("title_seed")
    return dict(seed) if isinstance(seed, dict) else {}


def save_title_seed(user_email: str, seed: Dict[str, Any]) -> None:
    if not user_email:
        return
    payload = dict(seed)
    payload["updated_at"] = datetime.now(timezone.utc)
    _db()[_COL].update_one(
        {"user_email": user_email},
        {"$set": {"title_seed": payload, "user_email": user_email}},
        upsert=True,
    )


def _persist_union(user_email: str, fp: str, resume_titles: List[str], stored: Dict[str, Any]) -> None:
    """Write fingerprint + union only. Never clobber a concurrent synth.

    When the resume set actually changed, drop the stale synth so the next
    ``suggest_filters_cached`` re-runs. A first-visit GET (no prior synth)
    only $sets the union fields so a parallel suggest can still land.
    """
    now = datetime.now(timezone.utc)
    patch: Dict[str, Any] = {
        "user_email": user_email,
        "title_seed.fingerprint": fp,
        "title_seed.resume_titles": resume_titles,
        "title_seed.union_updated_at": now,
    }
    update: Dict[str, Any] = {"$set": patch}
    prior_synth_fp = stored.get("synth_fingerprint")
    if prior_synth_fp and prior_synth_fp != fp:
        update["$unset"] = {
            "title_seed.suggestions": "",
            "title_seed.synth_titles": "",
            "title_seed.synth_fingerprint": "",
        }
    _db()[_COL].update_one({"user_email": user_email}, update, upsert=True)


def public_title_seed(user_email: str) -> Dict[str, Any]:
    """Cheap GET payload. Recomputes the union; never calls the LLM.

    When the fingerprint matches the stored synth, ``synth_titles`` come from
    cache. When resumes changed, synth_titles is empty until the next
    ``suggest_filters_cached`` (or stays empty if the union is already thick).
    """
    assets = resume_access.list_parsed_assets(user_email)
    fp = fingerprint_assets(assets)
    resume_titles = union_titles_from_assets(assets)
    stored = load_title_seed(user_email)
    synth: List[str] = []
    if stored.get("synth_fingerprint") == fp:
        raw = stored.get("synth_titles") or []
        if isinstance(raw, list):
            synth = [str(t).strip() for t in raw if str(t).strip()][:TITLE_CAP]
    if stored.get("fingerprint") != fp or stored.get("resume_titles") != resume_titles:
        _persist_union(user_email, fp, resume_titles, stored)
    return {
        "fingerprint": fp,
        "resume_titles": resume_titles,
        "synth_titles": synth,
        # The list a caller should actually SEARCH with. `resume_titles` are
        # titles the candidate has HELD (internships included) and
        # `synth_titles` are keyword strings; neither matches live postings.
        # See `canonical_search_titles`.
        "match_titles": match_titles_for(user_email, resume_titles + synth),
        "resume_count": len(assets),
        "cached": True,
    }


def match_titles_for(user_email: str, titles: Sequence[Any]) -> List[str]:
    """`canonical_search_titles` with this user's own specialties and level.

    Degrades to the generic vocabulary on any failure rather than raising: a
    taxonomy or profile problem must cost a user precision, never their whole
    feed. The floor is `_ALWAYS_MATCHABLE`, which is never empty - so this
    can return a worse list, but never the empty one that reads to a user as
    "there are no jobs".
    """
    specialties: List[str] = []
    level: Optional[str] = None
    try:
        from services import fit_score

        seen: set = set()
        best = -1
        for entry in fit_score.load_profiles_for_user(user_email) or []:
            profile = entry.get("profile") or {}
            for spec in (profile.get("tech") or {}).get("role_specialties") or []:
                if spec not in seen:
                    seen.add(spec)
                    specialties.append(str(spec))
            rank = fit_score._LEVEL_ORDER.get(str(profile.get("level") or ""), -1)
            if rank > best:
                best, level = rank, profile.get("level")
    except Exception:
        logger.exception("match_titles_for: profile lookup failed for %s", user_email)
    return canonical_search_titles(titles, specialties, level=level)


def _cached_suggestions(stored: Dict[str, Any], fp: str) -> Optional[Dict[str, Any]]:
    suggestions = stored.get("suggestions")
    if (
        stored.get("synth_fingerprint") == fp
        and isinstance(suggestions, dict)
        and (suggestions.get("workday_titles") or suggestions.get("linkedin_keyword_sets"))
    ):
        return suggestions
    return None


def suggest_filters_cached(
    user_email: str,
    synthesizer: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Resume-aware filter suggestions, LLM only on fingerprint miss.

    ``synthesizer`` is injected by the jobs route (``suggest_filters``) so
    this shared module never imports the jobs-Lambda-local suggester.

    Returns ``(suggestions, from_cache)``. No parsed resume → ValueError
    (same contract as the route's 404).
    """
    assets = resume_access.list_parsed_assets(user_email)
    if not assets:
        raise ValueError("No resume found. Upload a resume to enable smart filters.")
    fp = fingerprint_assets(assets)

    with _lock_for(user_email):
        stored = load_title_seed(user_email)
        hit = _cached_suggestions(stored, fp)
        if hit is not None:
            logger.info("title_seed: cache hit fingerprint=%s user=%s", fp, user_email)
            return hit, True

        merged = merge_structured_for_titles(assets)
        if not merged:
            raise ValueError("No resume found. Upload a resume to enable smart filters.")

        if synthesizer is None:
            # Jobs Lambda only. Kept lazy so resume/auth Lambdas can import
            # public_title_seed without the domain-local suggester.
            from services.filter_suggester_service import suggest_filters as synthesizer

        suggestions = synthesizer(merged)
        resume_titles = union_titles_from_assets(assets)
        synth_titles = [
            str(t).strip()
            for t in (suggestions.get("workday_titles") or [])
            if str(t).strip()
        ][:TITLE_CAP]
        save_title_seed(user_email, {
            "fingerprint": fp,
            "resume_titles": resume_titles,
            "synth_titles": synth_titles,
            "synth_fingerprint": fp,
            "suggestions": suggestions,
        })
        logger.info(
            "title_seed: synthesized fingerprint=%s titles=%d user=%s",
            fp, len(synth_titles), user_email,
        )
        return suggestions, False
