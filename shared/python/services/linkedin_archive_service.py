"""LinkedIn archive importer (Compass §2.1 + §6.11.3).

LinkedIn's API cannot read a member's profile or connection graph. But every
member can export their OWN data (Settings -> "Get a copy of your data") as a
ZIP of CSVs. That export is user-initiated and ToS-clean, and it is the only
sanctioned way to enrich a JobPilot profile from LinkedIn and to power the
warm-referral finder.

This module does three things and nothing else:
  1. `parse_archive`  -- defensively parse the known CSVs out of the uploaded ZIP.
  2. `save_archive`   -- upsert the normalized result into `linkedin_archives`,
                         one doc per user.
  3. `diff_against_resume` -- surface differences between the archive and the
                         user's already-parsed resume for the user to RESOLVE
                         (never auto-overwrite).

PII boundary (enforced here, not in a prompt): `Connections.csv` carries
THIRD-PARTY names/companies/roles. It is the user's own export, so we store it,
but it is *never* exposed cross-user and is *never* sent to an LLM. Only the
uploading user reads their own connections (referral_finder_service), and even
the draft-generator sends a role string, never a connection's identity.

Parsing uses only the stdlib (`zipfile`, `io`, `csv`) so there is no import-time
cost and no new dependency. There is NO LLM call in this file — importing an
archive is pure CSV parsing and stays synchronous in the route.
"""
from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from utils.db_connect import DBConnect
from utils.security import InputSanitizer

logger = logging.getLogger(__name__)

ARCHIVE_COLLECTION = "linkedin_archives"

# Defensive caps — a hostile or corrupt ZIP must not exhaust memory.
_MAX_ROWS = 60_000          # LinkedIn caps connections ~30k; leave headroom.
_MAX_FIELD = 500            # per-cell length after sanitize.
_MIN_HEADER_HITS = 2        # cells that must match known columns to be "the header"
                            # row (skips the Connections.csv preamble/notes line).

# ---------------------------------------------------------------------------
# Column maps: canonical_field -> accepted (normalized) header names.
# LinkedIn has renamed columns over the years; we accept the known variants.
# ---------------------------------------------------------------------------
_POSITIONS_MAP = {
    "company": ["company name", "company"],
    "title": ["title", "position title", "position"],
    "description": ["description"],
    "location": ["location"],
    "started_on": ["started on", "start date"],
    "finished_on": ["finished on", "end date"],
}
_EDUCATION_MAP = {
    "school": ["school name", "school"],
    "degree": ["degree name", "degree"],
    "field": ["field of study", "field"],
    "start": ["start date", "started on"],
    "end": ["end date", "finished on"],
    "notes": ["notes"],
    "activities": ["activities"],
}
_SKILLS_MAP = {
    "name": ["name", "skill", "skill name"],
}
_PROJECTS_MAP = {
    "name": ["title", "name", "project name"],
    "description": ["description"],
    "url": ["url"],
    "started_on": ["started on", "start date"],
    "finished_on": ["finished on", "end date"],
}
_CERTS_MAP = {
    "name": ["name"],
    "authority": ["authority"],
    "url": ["url"],
    "started_on": ["started on", "start date"],
    "finished_on": ["finished on", "end date"],
    "license_number": ["license number"],
}
_CONNECTIONS_MAP = {
    "first_name": ["first name"],
    "last_name": ["last name"],
    "company": ["company"],
    "position": ["position"],
    "connected_on": ["connected on"],
    # url/email intentionally NOT normalized into the stored connection — we
    # never need the third party's contact channel and won't store it.
}


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------
def _norm_header(h: str) -> str:
    """Normalize a header cell: lowercase, collapse whitespace, strip BOM."""
    return re.sub(r"\s+", " ", (h or "").replace("﻿", "").strip()).lower()


def _clean_cell(v: Any) -> str:
    """Bound + strip a data cell (defense in depth before it ever hits Mongo)."""
    return InputSanitizer.sanitize_string(v or "", max_length=_MAX_FIELD)


def _find_member(zf: zipfile.ZipFile, target: str) -> Optional[str]:
    """Case-insensitive basename match, tolerating a folder prefix in the ZIP."""
    tl = target.lower()
    for name in zf.namelist():
        if name.endswith("/"):
            continue
        if name.split("/")[-1].lower() == tl:
            return name
    return None


def _decode(raw: bytes) -> str:
    """Decode bytes tolerating BOM and the occasional latin-1 export."""
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _match_field(cell_norm: str, field_map: Dict[str, List[str]]) -> Optional[str]:
    for field, accepted in field_map.items():
        if cell_norm in accepted:
            return field
    return None


def _read_csv(
    zf: zipfile.ZipFile, filename: str, field_map: Dict[str, List[str]]
) -> List[Dict[str, str]]:
    """Read one CSV member into a list of normalized dicts.

    Robust to: missing file, BOM, encoding, header renames, and a leading
    preamble/notes block before the real header (as in `Connections.csv`).
    """
    member = _find_member(zf, filename)
    if not member:
        return []
    try:
        text = _decode(zf.read(member))
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("linkedin_archive: could not read %s: %s", filename, e)
        return []

    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []

    # Locate the header row: the first row whose cells match >= _MIN_HEADER_HITS
    # known columns. This transparently skips the Connections.csv preamble.
    header_idx = -1
    header_fields: List[Optional[str]] = []
    need = min(_MIN_HEADER_HITS, len(field_map))
    for i, row in enumerate(rows):
        fields = [_match_field(_norm_header(c), field_map) for c in row]
        if sum(1 for f in fields if f) >= need:
            header_idx = i
            header_fields = fields
            break
    if header_idx == -1:
        logger.info("linkedin_archive: no recognizable header in %s", filename)
        return []

    out: List[Dict[str, str]] = []
    for row in rows[header_idx + 1:]:
        if not any((c or "").strip() for c in row):
            continue  # blank line
        rec: Dict[str, str] = {}
        for idx, field in enumerate(header_fields):
            if field and idx < len(row):
                rec[field] = _clean_cell(row[idx])
        if any(rec.values()):
            out.append(rec)
        if len(out) >= _MAX_ROWS:
            logger.warning("linkedin_archive: %s truncated at %d rows", filename, _MAX_ROWS)
            break
    return out


# ---------------------------------------------------------------------------
# Public: parse
# ---------------------------------------------------------------------------
def parse_archive(zip_bytes: bytes) -> Dict[str, Any]:
    """Parse a LinkedIn data-export ZIP into a normalized dict.

    Returns:
        {
          "positions":      [{company,title,description,location,started_on,finished_on}],
          "educations":     [{school,degree,field,start,end,...}],
          "skills":         ["Python", ...],
          "projects":       [{name,description,url,started_on,finished_on}],
          "certifications": [{name,authority,url,started_on,finished_on}],
          "connections":    [{first_name,last_name,company,position,connected_on}],
        }

    Raises:
        ValueError("invalid_zip") if the bytes are not a readable ZIP.
    """
    if not zip_bytes:
        raise ValueError("empty_upload")
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise ValueError("invalid_zip")

    with zf:
        positions = _read_csv(zf, "Positions.csv", _POSITIONS_MAP)
        educations = _read_csv(zf, "Education.csv", _EDUCATION_MAP)
        skill_rows = _read_csv(zf, "Skills.csv", _SKILLS_MAP)
        projects = _read_csv(zf, "Projects.csv", _PROJECTS_MAP)
        certifications = _read_csv(zf, "Certifications.csv", _CERTS_MAP)
        connection_rows = _read_csv(zf, "Connections.csv", _CONNECTIONS_MAP)

    # Skills.csv is a single-column file -> flatten to a de-duped string list.
    skills: List[str] = []
    seen_skills = set()
    for r in skill_rows:
        s = (r.get("name") or "").strip()
        k = s.lower()
        if s and k not in seen_skills:
            seen_skills.add(k)
            skills.append(s)

    # Normalize connections to EXACTLY the 5 fields the finder needs. A row with
    # neither a name nor a company is noise (LinkedIn emits these) -> drop it.
    connections: List[Dict[str, str]] = []
    for r in connection_rows:
        first = (r.get("first_name") or "").strip()
        last = (r.get("last_name") or "").strip()
        company = (r.get("company") or "").strip()
        if not (first or last) and not company:
            continue
        connections.append({
            "first_name": first,
            "last_name": last,
            "company": company,
            "position": (r.get("position") or "").strip(),
            "connected_on": (r.get("connected_on") or "").strip(),
        })

    logger.info(
        "linkedin_archive: parsed positions=%d educations=%d skills=%d "
        "projects=%d certs=%d connections=%d",
        len(positions), len(educations), len(skills),
        len(projects), len(certifications), len(connections),
    )
    return {
        "positions": positions,
        "educations": educations,
        "skills": skills,
        "projects": projects,
        "certifications": certifications,
        "connections": connections,
    }


# ---------------------------------------------------------------------------
# Public: persist
# ---------------------------------------------------------------------------
def _archives():
    return DBConnect().get_db()[ARCHIVE_COLLECTION]


def save_archive(user_email: str, parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Upsert the parsed archive for a user (one doc per user).

    Connections are stored (they are the user's own export) but are third-party
    PII: never exposed cross-user, never sent to an LLM. The returned summary
    deliberately omits the connections list — it carries counts only.
    """
    email = InputSanitizer.sanitize_email(user_email)
    if not email:
        raise ValueError("invalid_user_email")
    if not isinstance(parsed, dict):
        raise ValueError("invalid_parsed_payload")

    now = datetime.now(timezone.utc)
    counts = {
        "positions": len(parsed.get("positions") or []),
        "educations": len(parsed.get("educations") or []),
        "skills": len(parsed.get("skills") or []),
        "projects": len(parsed.get("projects") or []),
        "certifications": len(parsed.get("certifications") or []),
        "connections": len(parsed.get("connections") or []),
    }
    doc = {
        "user_email": email,
        "positions": parsed.get("positions") or [],
        "educations": parsed.get("educations") or [],
        "skills": parsed.get("skills") or [],
        "projects": parsed.get("projects") or [],
        "certifications": parsed.get("certifications") or [],
        "connections": parsed.get("connections") or [],
        "counts": counts,
        "imported_at": now,
    }
    _archives().update_one(
        {"user_email": email},
        {"$set": doc, "$setOnInsert": {"first_imported_at": now}},
        upsert=True,
    )
    logger.info("linkedin_archive: saved for %s (%s)", email, counts)
    return {"user_email": email, "counts": counts, "imported_at": now.isoformat()}


def get_archive(user_email: str) -> Optional[Dict[str, Any]]:
    """Fetch the stored archive for a user (or None). `_id` stripped."""
    email = InputSanitizer.sanitize_email(user_email)
    if not email:
        return None
    doc = _archives().find_one({"user_email": email})
    if doc:
        doc.pop("_id", None)
    return doc


# ---------------------------------------------------------------------------
# Public: diff against the parsed resume
# ---------------------------------------------------------------------------
def _norm(s: str) -> str:
    """Aggressive normalization for cross-source matching."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


_COMPANY_STOP = {"inc", "llc", "ltd", "limited", "corp", "corporation", "co", "company", "the", "group"}


def _norm_company(s: str) -> str:
    toks = [t for t in _norm(s).split() if t not in _COMPANY_STOP]
    return " ".join(toks)


def _resume_structured(user_email: str) -> Dict[str, Any]:
    """Load the user's parsed/structured resume (or {} if none)."""
    try:
        from services.resume_parser import ResumeParser
        db = DBConnect().get_db()
        doc = ResumeParser(db).get_structured_resume(user_email=user_email) or {}
        structured = doc.get("structured")
        return structured if isinstance(structured, dict) else {}
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("linkedin_archive: could not load resume for %s: %s", user_email, e)
        return {}


def diff_against_resume(user_email: str, parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Compare a parsed archive against the user's parsed resume.

    Returns `{"conflicts": [...], "additions": [...]}` for the user to resolve.
    We NEVER auto-overwrite the resume — this only surfaces differences:
      * addition = present in LinkedIn, absent from the resume (safe to add).
      * conflict = same anchor (company / school) but a differing detail
                   (title / degree) — the user decides which is correct.
    """
    email = InputSanitizer.sanitize_email(user_email)
    if not email:
        raise ValueError("invalid_user_email")

    structured = _resume_structured(email)
    additions: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []

    # --- positions vs experience -----------------------------------------
    resume_exp = structured.get("experience") or []
    exp_by_company: Dict[str, List[str]] = {}
    for e in resume_exp:
        c = _norm_company(e.get("company", ""))
        if c:
            exp_by_company.setdefault(c, []).append(_norm(e.get("title", "")))
    for p in (parsed.get("positions") or []):
        c = _norm_company(p.get("company", ""))
        t = _norm(p.get("title", ""))
        if not c:
            continue
        if c not in exp_by_company:
            additions.append({"kind": "position", "linkedin": p})
        elif t and t not in exp_by_company[c]:
            conflicts.append({
                "kind": "position", "field": "title",
                "company": p.get("company", ""),
                "linkedin": {"title": p.get("title", ""), "dates": p.get("started_on", "")},
                "resume_titles": [x for x in exp_by_company[c] if x],
            })

    # --- educations vs education -----------------------------------------
    resume_edu = structured.get("education") or []
    edu_by_school: Dict[str, List[str]] = {}
    for e in resume_edu:
        s = _norm(e.get("institution", ""))
        if s:
            edu_by_school.setdefault(s, []).append(_norm(e.get("degree", "")))
    for ed in (parsed.get("educations") or []):
        s = _norm(ed.get("school", ""))
        d = _norm(ed.get("degree", ""))
        if not s:
            continue
        if s not in edu_by_school:
            additions.append({"kind": "education", "linkedin": ed})
        elif d and d not in edu_by_school[s]:
            conflicts.append({
                "kind": "education", "field": "degree",
                "school": ed.get("school", ""),
                "linkedin": {"degree": ed.get("degree", ""), "field": ed.get("field", "")},
                "resume_degrees": [x for x in edu_by_school[s] if x],
            })

    # --- skills (additions only; skills don't "conflict") ----------------
    resume_skills = set()
    for cat_vals in (structured.get("skills") or {}).values():
        for sk in (cat_vals or []):
            resume_skills.add(_norm(sk))
    for sk in (parsed.get("skills") or []):
        if _norm(sk) and _norm(sk) not in resume_skills:
            additions.append({"kind": "skill", "linkedin": {"name": sk}})

    logger.info(
        "linkedin_archive: diff for %s -> additions=%d conflicts=%d",
        email, len(additions), len(conflicts),
    )
    return {"conflicts": conflicts, "additions": additions,
            "has_resume": bool(structured)}
