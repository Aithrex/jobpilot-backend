"""Project links: attach the repository or demo URL a résumé already shows.

Screeners read project links. HackerRank's published intern rubric
(interviewstreet/hiring-agent) takes 3-5 points off for every project without
a GitHub link or live demo, and it follows the link to the repository. Our
project schema had no URL field, so every project lost those points however
real it was.

This module is deterministic and spends no model call. A project gets a URL
only when the uploaded résumé itself contains that URL:

* an explicit ``url`` the parser emitted is kept only if the same link is in
  the document (its text or its hyperlink targets) — never a constructed one;
* otherwise a link from the document is attached when its repository or demo
  slug names the project ("notes-qa-bot" for "Notes Q&A Bot"), or, failing
  that, when it is printed on the project's own line or the line under it.

Contact links (LinkedIn, the GitHub profile, the portfolio) are never used as a
project link, and one link is never given to two projects.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

_URL_RE = re.compile(
    r"(?:https?://[^\s<>\"')\]]+|(?:www\.)?(?:github\.com|gitlab\.com|bitbucket\.org|huggingface\.co|"
    r"kaggle\.com|[a-z0-9-]+\.(?:vercel\.app|netlify\.app|herokuapp\.com|github\.io|streamlit\.app|"
    r"onrender\.com|pages\.dev|fly\.dev))/?[^\s<>\"')\]]*)",
    re.I,
)
_MARKER_RE = re.compile(r"\[Extracted Link:\s*([^\]\s]+)\s*\]")
_STOP = {"the", "and", "for", "with", "app", "project", "system", "using", "based", "tool", "web", "api"}
_PROFILE_HOSTS = ("linkedin.com",)


def normalize(url: str) -> str:
    """Comparable form: lower-case, no scheme, no www, no trailing slash/punctuation."""
    u = str(url or "").strip().strip(".,;:)").lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    return u.rstrip("/")


def display(url: str) -> str:
    u = str(url or "").strip().strip(".,;:)")
    u = re.sub(r"^https?://", "", u, flags=re.I)
    u = re.sub(r"^www\.", "", u, flags=re.I)
    return u.rstrip("/")


def href(url: str) -> str:
    u = str(url or "").strip().strip(".,;:)")
    return u if re.match(r"^https?://", u, re.I) else f"https://{u}"


def is_link(value: str) -> bool:
    v = str(value or "").strip()
    if not v or " " in v or "@" in v.split("/")[0]:
        return False
    return bool(_URL_RE.fullmatch(v) or re.match(r"^https?://\S+\.\S+", v, re.I))


def document_links(raw_text: str, extracted_urls: Sequence[str] = ()) -> List[str]:
    """Every link the document holds: hyperlink targets first, then printed URLs."""
    found: List[str] = []
    seen = set()
    for u in list(extracted_urls or []) + _MARKER_RE.findall(raw_text or "") + _URL_RE.findall(raw_text or ""):
        n = normalize(u)
        if not n or n in seen or "." not in n.split("/")[0]:
            continue
        seen.add(n)
        found.append(str(u).strip().strip(".,;:)"))
    return found


def _contact_keys(structured: Dict[str, Any]) -> set:
    contact = (structured or {}).get("contact") or {}
    keys = {normalize(contact.get(k) or "") for k in ("linkedin", "github", "portfolio")}
    return {k for k in keys if k}


def _name_tokens(name: str) -> List[str]:
    toks = re.findall(r"[a-z0-9]+", str(name or "").lower())
    return [t for t in toks if len(t) > 1 and t not in _STOP]


def _slug_matches(url: str, name: str) -> bool:
    """The repository/demo slug names the project ("notes-qa-bot" ~ "Notes Q&A Bot")."""
    parts = [p for p in normalize(url).split("/") if p]
    if len(parts) < 2 and not re.search(r"\.(vercel|netlify|herokuapp|github|streamlit|onrender|pages|fly)\.", parts[0] if parts else ""):
        return False
    slug = parts[-1] if len(parts) >= 2 else parts[0].split(".")[0]
    slug_tokens = set(re.findall(r"[a-z0-9]+", slug))
    name_tokens = _name_tokens(name)
    if not slug_tokens or not name_tokens:
        return False
    if "".join(name_tokens) == "".join(re.findall(r"[a-z0-9]+", slug)):
        return True
    hit = sum(1 for t in name_tokens if t in slug_tokens)
    return hit >= 1 and hit * 2 >= len(name_tokens)


def _project_block(raw_text: str, name: str, next_names: Iterable[str]) -> str:
    """The project's own line and the line after it.

    Résumés print a project link on the name line ("Notes Bot | github.com/...")
    or directly under it. Reading further runs into the next entry, whose name
    the parser may not have captured. Hyperlink markers are appended at the END
    of the document text, so they are removed first.
    """
    text = _MARKER_RE.sub("", raw_text or "")
    low = text.lower()
    key = str(name or "").lower().strip()
    start = low.find(key) if key else -1
    if start < 0:
        return ""
    line_start = low.rfind("\n", 0, start) + 1
    end = start
    for _ in range(2):
        nxt = low.find("\n", end + 1)
        end = len(text) if nxt < 0 else nxt
        if nxt < 0:
            break
    for other in next_names:
        o = str(other or "").lower().strip()
        if not o:
            continue
        pos = low.find(o, start + len(key))
        if 0 <= pos < end:
            end = pos
    return text[line_start:end]


def attach_project_links(
    structured: Dict[str, Any],
    raw_text: str = "",
    extracted_urls: Sequence[str] = (),
    *,
    validate_existing: bool = True,
) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    """Give each project the link the document shows for it. Returns (resume, attached).

    ``validate_existing=False`` is the backfill mode for résumés parsed before
    this field existed: a URL already stored is kept as it is (its document may
    no longer carry the hyperlink markers) and only empty ones are filled.
    """
    projects = [p for p in ((structured or {}).get("projects") or []) if isinstance(p, dict)]
    if not projects:
        return structured, []
    links = document_links(raw_text, extracted_urls)
    known = {normalize(u) for u in links}
    blocked = _contact_keys(structured)
    used = set()
    attached: List[Dict[str, str]] = []

    # 1. A URL the parser emitted survives only if the document holds it.
    for p in projects:
        given = str(p.get("url") or "").strip()
        if not given:
            p["url"] = ""
            continue
        n = normalize(given)
        if not validate_existing:
            used.add(n)
            continue
        if not is_link(given) or n not in known or n in blocked or n in used:
            p["url"] = ""
            continue
        used.add(n)

    names = [str(p.get("name") or "") for p in projects]
    candidates = [u for u in links
                  if normalize(u) not in blocked
                  and not any(h in normalize(u) for h in _PROFILE_HOSTS)
                  and len([s for s in normalize(u).split("/") if s]) >= 1]

    # 2. A hyperlink whose slug names the project.
    for i, p in enumerate(projects):
        if p.get("url"):
            continue
        for u in candidates:
            n = normalize(u)
            if n in used or not _slug_matches(u, names[i]):
                continue
            # A bare profile (github.com/<user>) is never a project link.
            if n.startswith(("github.com/", "gitlab.com/")) and len(n.split("/")) < 3:
                continue
            p["url"] = u
            used.add(n)
            attached.append({"project": names[i], "url": u, "basis": "slug_names_project"})
            break
    # 3. A link printed inside the project's own block.
    for i, p in enumerate(projects):
        if p.get("url"):
            continue
        block = _project_block(raw_text, names[i], names[:i] + names[i + 1:])
        if not block:
            continue
        block_links = {normalize(u) for u in _URL_RE.findall(block)}
        for u in candidates:
            n = normalize(u)
            if n in used or n not in block_links:
                continue
            p["url"] = u
            used.add(n)
            attached.append({"project": names[i], "url": u, "basis": "in_project_block"})
            break

    return structured, attached


def restore_from_original(original: Dict[str, Any], tailored: Dict[str, Any]) -> List[str]:
    """A tailored project's URL is the ORIGINAL project's URL, or empty.

    Matched by project name, like IntegrityGuard matches projects. Returns the
    names whose URL a model changed or invented.
    """
    orig = {str(p.get("name") or "").lower().strip(): str(p.get("url") or "")
            for p in ((original or {}).get("projects") or []) if isinstance(p, dict)}
    changed: List[str] = []
    for p in (tailored or {}).get("projects") or []:
        if not isinstance(p, dict):
            continue
        key = str(p.get("name") or "").lower().strip()
        want = orig.get(key, "")
        have = str(p.get("url") or "")
        # Omitted by the writer = silently restored. A DIFFERENT link = an overwrite.
        if have and have != want:
            changed.append(str(p.get("name") or ""))
        p["url"] = want
    return changed


def link_of(project: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """(display text, href) for rendering, or None."""
    url = str((project or {}).get("url") or "").strip()
    if not url or not is_link(url):
        return None
    return display(url), href(url)


def carry_current_links(current: Dict[str, Any], target: Dict[str, Any]) -> None:
    """On regenerate, a project keeps the link of the version the user is editing.

    ``current`` is the tailored résumé the user had in front of them; its links
    came either from the parsed original or from the user typing one in the
    editor, never from a model (the guard resets those). A user who deleted a
    link keeps it deleted. Projects new in ``target`` keep the guard's value.
    """
    links = {str(p.get("name") or "").lower().strip(): str(p.get("url") or "")
             for p in ((current or {}).get("projects") or []) if isinstance(p, dict)}
    for p in (target or {}).get("projects") or []:
        if not isinstance(p, dict):
            continue
        key = str(p.get("name") or "").lower().strip()
        if key in links:
            value = links[key]
            p["url"] = value if (not value or is_link(value)) else ""
