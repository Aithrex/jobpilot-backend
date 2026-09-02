"""
Resume document renderer — PDF (fpdf2) and DOCX (python-docx).

One LayoutSpec is the source of truth for page size, type scale, gaps,
section order, and headings. Both renderers consume it. The layout solver
always terminates in an explicit PageResolution — never silent clipping.
"""
from __future__ import annotations

import copy
import io
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple

logger = logging.getLogger(__name__)


def _two_page_enabled() -> bool:
    """Legacy env flag. The solver may still emit two pages when content
    cannot fit one page without clipping — clipping is never allowed.
    This flag only changes the *allocator* budget (see bullet_allocator)."""
    default = "true" if os.environ.get("ENVIRONMENT", "development") == "development" else "false"
    return os.environ.get("ALLOW_TWO_PAGE", default).strip().lower() in ("1", "true", "yes", "on")


ALLOW_TWO_PAGE = _two_page_enabled()

Outcome = Literal["fit_one", "fit_two", "trimmed_to_one", "trimmed_to_two"]


# ---------------------------------------------------------------------------
# Layout spec — the single frozen definition of the page
# ---------------------------------------------------------------------------

PAGE_SIZES = {
    "letter": (215.9, 279.4),
    "a4": (210.0, 297.0),
}


_IN = 25.4  # mm per inch


@dataclass(frozen=True)
class LayoutSpec:
    """Every literal both renderers (and the preview) must share."""

    page: str = "letter"  # "letter" | "a4"

    # Dense-end values transcribed from the reference (0.42 / 0.30 / 0.24 in).
    # Sparse pages interpolate toward the wide end; sides never go below dense.
    side_margin_dense_in: float = 0.42
    side_margin_sparse_in: float = 0.70
    top_margin_dense_in: float = 0.30
    top_margin_sparse_in: float = 0.45
    bottom_margin_dense_in: float = 0.24
    bottom_margin_sparse_in: float = 0.40

    # PDF embeds Charis SIL. DOCX names Georgia: Charis is not installed on
    # most machines, and Word's silent substitute is worse than a deliberate
    # near-match (Georgia, similar x-height). Intentional, not drift.
    pdf_font_family: str = "CharisSIL"
    pdf_font_fallback_family: str = "Tinos"
    docx_font_family: str = "Georgia"

    name_pt: float = 19.0
    section_pt: float = 11.5
    entry_pt: float = 10.2
    contact_pt: float = 9.4
    body_pt: float = 9.4
    min_heading_body_gap_pt: float = 1.5
    # 3.77 mm at 9.4 pt → 1.137 ratio. Other tiers scale from this.
    body_lh_mm_at_ref: float = 3.77
    text_align: str = "L"  # never "J" — short resume lines river when justified

    section_order: Tuple[str, ...] = (
        "summary", "experience", "projects", "skills", "education", "certifications",
    )
    headings: Tuple[Tuple[str, str], ...] = (
        ("summary", "SUMMARY"),
        ("experience", "EXPERIENCE"),
        ("projects", "PROJECTS"),
        ("skills", "TECHNICAL SKILLS"),
        ("education", "EDUCATION"),
        ("certifications", "CERTIFICATIONS"),
    )

    # Tight inside a section, wider between sections. Dense = reference.
    # Heading chrome (cell, rule gap, post_header) is FROZEN — the density
    # ramp may open space between sections and between jobs, never under a
    # heading. Otherwise SUMMARY looks glued to its rule on a dense page
    # and EXPERIENCE looks loose on a sparse one.
    min_section_gap: float = 1.82   # before heading
    max_section_gap: float = 3.20
    min_entry_gap: float = 1.13
    max_entry_gap: float = 2.00
    min_post_header: float = 0.80   # after rule → first content (frozen)
    max_post_header: float = 0.80
    min_header_gap: float = 0.33    # company → role title
    max_header_gap: float = 0.50
    min_skill_gap: float = 0.0
    max_skill_gap: float = 0.0
    min_bullet_gap: float = 0.30
    max_bullet_gap: float = 0.45
    min_pre_bullet: float = 0.46
    max_pre_bullet: float = 0.60
    edu_entry_gap: float = 1.13
    name_contact_gap_mm: float = 1.20
    contact_line_gap_mm: float = 0.42
    contact_section_gap_mm: float = 1.82  # same as min_section_gap
    heading_rule_gap_mm: float = 1.10
    bullet_indent_mm: float = 3.48   # text indent from left margin
    bullet_text_gap_mm: float = 1.39  # marker → text
    bullet_glyph: str = "\u2022"
    bullet_glyph_fallback: str = "-"
    split_row_gap_mm: float = 2.0
    section_hr_width: float = 0.25
    heading_cell_h: float = 4.70     # 11.5 pt + Charis descender (SUMMARY Y)
    name_cell_h: float = 6.70        # 19.0 pt
    job_title_cell_h: float = 3.77
    entry_row_h: float = 4.50        # company / project / school first line
    max_skill_leading: float = 3.77
    docx_max_skill_spacing: float = 1.137
    # Fallback if Charis cannot be measured. Re-derived at 10pt / 0.42in sides.
    docx_chars_per_line_at_10pt: float = 110.0

    target_fill: float = 0.95
    max_fill: float = 0.97
    min_second_page_fill: float = 0.55
    sparse_fill_floor: float = 0.62
    fill_ramp_lo: float = 0.55
    fill_ramp_hi: float = 0.68
    max_leading_mm: float = 6.0
    stretch_cap: float = 1.40

    def heading(self, key: str) -> str:
        return dict(self.headings).get(key, key.upper())

    @property
    def page_width_mm(self) -> float:
        return PAGE_SIZES[self.page][0]

    @property
    def page_height_mm(self) -> float:
        return PAGE_SIZES[self.page][1]

    @property
    def margin_lr_mm(self) -> float:
        return self.side_margin_dense_in * _IN

    @property
    def margin_lr_wide_mm(self) -> float:
        return self.side_margin_sparse_in * _IN

    @property
    def margin_top_mm(self) -> float:
        return self.top_margin_dense_in * _IN

    @property
    def margin_bottom_mm(self) -> float:
        return self.bottom_margin_dense_in * _IN

    @property
    def margin_top_sparse_mm(self) -> float:
        return self.top_margin_sparse_in * _IN

    @property
    def margin_bottom_sparse_mm(self) -> float:
        return self.bottom_margin_sparse_in * _IN

    # Compat aliases used as stretch bases.
    @property
    def bullet_gap(self) -> float:
        return self.min_bullet_gap

    @property
    def pre_bullet_gap(self) -> float:
        return self.min_pre_bullet

    def usable_height(self, top: Optional[float] = None, bottom: Optional[float] = None) -> float:
        t = self.margin_top_mm if top is None else top
        b = self.margin_bottom_mm if bottom is None else bottom
        return self.page_height_mm - t - b

    def usable_width(self, margin_lr: float) -> float:
        return self.page_width_mm - 2 * margin_lr

    def to_public_dict(self) -> Dict[str, Any]:
        """JSON the frontend preview consumes — headings, order, type scale."""
        return {
            "page": self.page,
            "page_width_mm": self.page_width_mm,
            "page_height_mm": self.page_height_mm,
            "margin_top_mm": self.margin_top_mm,
            "margin_bottom_mm": self.margin_bottom_mm,
            "margin_lr_mm": self.margin_lr_mm,
            "name_pt": self.name_pt,
            "section_pt": self.section_pt,
            "entry_pt": self.entry_pt,
            "contact_pt": self.contact_pt,
            "body_pt": self.body_pt,
            "text_align": self.text_align,
            "pdf_font_family": self.pdf_font_family,
            "docx_font_family": self.docx_font_family,
            "section_order": list(self.section_order),
            "headings": {k: v for k, v in self.headings},
        }


DEFAULT_SPEC = LayoutSpec()


@dataclass(frozen=True)
class FontTier:
    body_pt: float
    lh: float
    lh_s: float


@dataclass(frozen=True)
class GapPlan:
    section_gap: float
    entry_gap: float
    post_header: float
    header_gap: float
    skill_gap: float
    margin_lr: float
    margin_top: float = 7.62    # 0.30in dense default
    margin_bottom: float = 6.10  # 0.24in dense default
    pre_bullet: float = 0.46
    bullet_gap: float = 0.30


@dataclass(frozen=True)
class DroppedItem:
    kind: str          # "project_bullet" | "project" | "experience_bullet" | "experience"
    index: int
    preview: str

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "index": self.index, "preview": self.preview}


@dataclass
class PageResolution:
    tier: FontTier
    gaps: GapPlan
    stretch: float
    pages: int
    predicted_height_mm: float
    fill_ratio: float
    dropped: List[DroppedItem] = field(default_factory=list)
    outcome: Outcome = "fit_one"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pages": self.pages,
            "outcome": self.outcome,
            "fill_ratio": round(self.fill_ratio, 4),
            "dropped": [d.to_dict() for d in self.dropped],
            "body_pt": self.tier.body_pt,
            "predicted_height_mm": round(self.predicted_height_mm, 2),
        }


class UnrenderableTextError(ValueError):
    """Raised when the resume contains codepoints no loaded font can draw."""


def _body_lh(pt: float, scale: float = 1.0) -> float:
    """Line height at `pt`, scaled from the 9.4pt / 3.77mm reference."""
    ref = LayoutSpec.body_pt
    return pt * (LayoutSpec.body_lh_mm_at_ref / ref) * scale


def _body_tier(pt: float, lh_scale: float = 1.0) -> FontTier:
    lh = _body_lh(pt, lh_scale)
    return FontTier(pt, lh, lh)


# Density ladder (body only — headings stay on LayoutSpec).
# Max body is 10.0 so section_pt (11.5) stays ≥ 1.5pt above the body.
_UP_TIERS = (
    _body_tier(10.0),
    _body_tier(9.7),
)
_BASE_TIER = _body_tier(9.4)
_TIGHT_BASE = (
    _body_tier(9.4, 0.97),
    _body_tier(9.4, 0.93),
    _body_tier(9.4, 0.90),
)
_TIER_92 = _body_tier(9.2)
_TIER_90 = _body_tier(9.0)
_TWO_PAGE_FLOOR = _body_tier(9.4)

_CJK_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]"
)
_FLOOR_RECENT = 3
_FLOOR_OTHER = 2


class ResumeRenderer:
    """Generates PDF and DOCX from a structured (tailored) resume dict."""

    RENDER_VERSION = "2026-09-02.5"  # frozen heading chrome; skills/projects cleanup

    SKILLS_TITLES = (
        "Technical Skills",
        "Skills",
        "Core Skills",
        "Professional Skills",
        "Key Skills",
    )

    def __init__(self, spec: Optional[LayoutSpec] = None):
        self.spec = spec or DEFAULT_SPEC
        self._FONT_FAMILY = self.spec.pdf_font_family
        self._FALLBACK_SANS = "NotoSans"
        self._FALLBACK_CJK = "NotoSansSC"
        self._loaded_fonts: set = set()

    # Compatibility aliases so augmenter / allocator / tests keep working.
    @property
    def _AVAIL_H(self) -> float:
        return self.spec.usable_height()

    @property
    def _MIN_SECTION_GAP(self) -> float:
        return self.spec.min_section_gap

    @property
    def _MAX_SECTION_GAP(self) -> float:
        return self.spec.max_section_gap

    @property
    def _MIN_ENTRY_GAP(self) -> float:
        return self.spec.min_entry_gap

    @property
    def _MAX_ENTRY_GAP(self) -> float:
        return self.spec.max_entry_gap

    @property
    def _MIN_POST_HEADER(self) -> float:
        return self.spec.min_post_header

    @property
    def _MAX_POST_HEADER(self) -> float:
        return self.spec.max_post_header

    @property
    def _MIN_HEADER_GAP(self) -> float:
        return self.spec.min_header_gap

    @property
    def _MAX_HEADER_GAP(self) -> float:
        return self.spec.max_header_gap

    @property
    def _MIN_SKILL_GAP(self) -> float:
        return self.spec.min_skill_gap

    @property
    def _MAX_SKILL_GAP(self) -> float:
        return self.spec.max_skill_gap

    @property
    def _MAX_SKILL_LEADING(self) -> float:
        return self.spec.max_skill_leading

    @property
    def _DOCX_MAX_SKILL_SPACING(self) -> float:
        return self.spec.docx_max_skill_spacing

    @property
    def _STRETCH_CAP(self) -> float:
        return self.spec.stretch_cap

    @property
    def _MAX_LEADING_MM(self) -> float:
        return self.spec.max_leading_mm

    @property
    def _SPARSE_FILL_FLOOR(self) -> float:
        return self.spec.sparse_fill_floor

    @property
    def _FILL_RAMP_LO(self) -> float:
        return self.spec.fill_ramp_lo

    @property
    def _FILL_RAMP_HI(self) -> float:
        return self.spec.fill_ramp_hi

    @classmethod
    def _skills_title(cls, tailored: Dict[str, Any]) -> str:
        title = str(tailored.get("skills_section_title") or "").strip()
        return title if title in cls.SKILLS_TITLES else "Technical Skills"

    _BAD_SKILL_CATEGORIES = frozenset({
        "", "unknown", "< unknown >", "<unknown>", "none", "n/a",
    })
    _ROLE_LABEL_SKILLS = frozenset({
        "backend", "frontend", "fullstack", "full-stack", "full stack",
        "back-end", "front-end", "full-stack development",
        "fullstack development", "full stack development",
        "backend development", "frontend development",
        "software engineering", "software development",
        "full-stack developer", "fullstack developer", "full stack developer",
    })

    @classmethod
    def _language_item_display(cls, term: str) -> Optional[str]:
        """Canonical language if `term` is a language, including 'Java 17'."""
        raw = re.sub(r"\s*\([^)]*\)\s*$", "", str(term or "")).strip()
        if not raw:
            return None
        versionless = re.sub(r"\s+v?\d+(?:\.\d+)*\s*$", "", raw, flags=re.I).strip()
        try:
            from utils.keyword_normalizer import is_programming_language, normalize_single
        except Exception:
            return None
        for cand in (raw, versionless):
            if cand and is_programming_language(cand):
                return normalize_single(versionless or raw) or (versionless or raw)
        return None

    @classmethod
    def _prepare_skill_rows(
        cls, skills: Dict[str, Any],
    ) -> List[Tuple[str, List[str]]]:
        """Drop broken headings / role labels; file versioned languages."""
        rows: List[Tuple[str, List[str]]] = []
        langs_extra: List[str] = []
        orphans: List[str] = []
        seen_lang = set()

        def _keep_lang(name: str) -> None:
            key = name.lower()
            if key in seen_lang:
                return
            seen_lang.add(key)
            langs_extra.append(name)

        for cat, items in (skills or {}).items():
            if not items:
                continue
            name = str(cat or "").strip()
            raw_items = items if isinstance(items, list) else [items]
            cleaned: List[str] = []
            is_lang_cat = "language" in name.lower()
            is_concept = "concept" in name.lower() or "methodolog" in name.lower()
            for raw in raw_items:
                item = str(raw).strip()
                if not item:
                    continue
                low = re.sub(r"\s+", " ", item.lower())
                if low in cls._ROLE_LABEL_SKILLS or low.strip("<> ") == "unknown":
                    continue
                lang = cls._language_item_display(item)
                has_version = bool(re.search(r"\s+v?\d+(?:\.\d+)*\s*$", item, flags=re.I))
                # Only relocate misfiled languages (Java 17 under Concepts).
                # Do not steal "Python" out of a custom heading — that
                # collapsed every category into Languages and zeroed height.
                if lang and not is_lang_cat and (is_concept or has_version):
                    _keep_lang(lang)
                    continue
                if lang and is_lang_cat:
                    item = lang
                cleaned.append(item)
            if not cleaned:
                continue
            if name.lower() in cls._BAD_SKILL_CATEGORIES or name.startswith("<"):
                orphans.extend(cleaned)
                continue
            rows.append((name, cleaned))

        if langs_extra:
            placed = False
            for i, (cat, items) in enumerate(rows):
                if "language" in cat.lower():
                    merged = list(items)
                    have = {s.lower() for s in merged}
                    for lang in langs_extra:
                        if lang.lower() not in have:
                            merged.append(lang)
                            have.add(lang.lower())
                    rows[i] = (cat, merged)
                    placed = True
                    break
            if not placed:
                rows.append(("Languages", list(langs_extra)))
        if orphans:
            if rows:
                last_cat, last_items = rows[-1]
                rows[-1] = (last_cat, list(last_items) + orphans)
            else:
                rows.append(("Skills", orphans))
        return [(c, v) for c, v in rows if v]

    @staticmethod
    def _contact_display(key: str, value: str) -> tuple:
        if not value:
            return ("", None)
        if key in ("linkedin", "github", "portfolio"):
            display = re.sub(r"^https?://", "", value).rstrip("/")
            href = value if value.startswith("http") else f"https://{value}"
            return (display, href)
        if key == "email":
            return (value, f"mailto:{value}")
        return (value, None)

    @staticmethod
    def _title_case_name(name: str) -> str:
        if not name:
            return name
        out = []
        for tok in name.split():
            if tok.isupper() or tok.islower():
                out.append(tok[:1].upper() + tok[1:].lower())
            else:
                out.append(tok)
        return " ".join(out)

    @staticmethod
    def _sanitize_text(text) -> str:
        """Normalize punctuation. Does NOT replace unrenderable glyphs with '?'."""
        if not text:
            return ""
        text = str(text)
        replacements = {
            "\u2022": "\xb7",
            "\u2013": " - ",
            "\u2014": " - ",
            "\u2015": " - ",
            "\u2212": "-",
            "\u2018": "'",
            "\u2019": "'",
            "\u201c": '"',
            "\u201d": '"',
            "\u2026": "...",
            "\u00a0": " ",
            "\u200b": "",
        }
        for char, repl in replacements.items():
            text = text.replace(char, repl)
        return re.sub(r"  +", " ", text)

    def _assert_renderable(self, text: str, context: str = "") -> None:
        """Fail loudly if a codepoint cannot be drawn by any loaded font."""
        if not text:
            return
        if _CJK_RE.search(text) and "cjk" not in self._loaded_fonts:
            sample = "".join(ch for ch in text if _CJK_RE.search(ch))[:8]
            raise UnrenderableTextError(
                f"Resume contains characters this renderer cannot draw "
                f"({context or 'text'}: {sample!r}). "
                "Add NotoSansSC-Regular.otf to services/fonts/."
            )
        # Core Times is latin-1 only. Charis/Tinos cover Latin/Greek/Cyrillic.
        if self._FONT_FAMILY == "Times":
            try:
                text.encode("latin-1")
            except UnicodeEncodeError:
                if "sans" not in self._loaded_fonts and "cjk" not in self._loaded_fonts:
                    raise UnrenderableTextError(
                        f"Resume contains characters the Times fallback cannot "
                        f"draw ({context or 'text'}). Install Charis SIL in services/fonts/."
                    )

    def _pick_face(self, text: str) -> str:
        if _CJK_RE.search(text) and "cjk" in self._loaded_fonts:
            return self._FALLBACK_CJK
        return self._FONT_FAMILY

    def _set_font(self, pdf, style: str, size: float, text: str = "") -> None:
        face = self._pick_face(text) if text else self._FONT_FAMILY
        try:
            pdf.set_font(face, style, size)
        except Exception:
            pdf.set_font(self._FONT_FAMILY, style, size)

    # ------------------------------------------------------------------
    # Fonts
    # ------------------------------------------------------------------

    def _register_fonts(self, pdf, *, need_cjk: bool = False) -> None:
        fonts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
        self._loaded_fonts = set()
        s = self.spec
        charis_ok = False
        try:
            pdf.add_font(s.pdf_font_family, "", os.path.join(fonts_dir, "CharisSIL-Regular.ttf"))
            pdf.add_font(s.pdf_font_family, "B", os.path.join(fonts_dir, "CharisSIL-Bold.ttf"))
            pdf.add_font(s.pdf_font_family, "I", os.path.join(fonts_dir, "CharisSIL-Italic.ttf"))
            pdf.add_font(s.pdf_font_family, "BI", os.path.join(fonts_dir, "CharisSIL-BoldItalic.ttf"))
            self._FONT_FAMILY = s.pdf_font_family
            self._loaded_fonts.add("charis")
            charis_ok = True
        except Exception as e:
            logger.warning("resume_render Charis SIL missing — %s", e)

        if not charis_ok:
            try:
                pdf.add_font(s.pdf_font_fallback_family, "", os.path.join(fonts_dir, "Tinos-Regular.ttf"))
                pdf.add_font(s.pdf_font_fallback_family, "B", os.path.join(fonts_dir, "Tinos-Bold.ttf"))
                pdf.add_font(s.pdf_font_fallback_family, "I", os.path.join(fonts_dir, "Tinos-Italic.ttf"))
                pdf.add_font(s.pdf_font_fallback_family, "BI", os.path.join(fonts_dir, "Tinos-BoldItalic.ttf"))
                self._loaded_fonts.add("tinos")
                self._FONT_FAMILY = s.pdf_font_fallback_family
                logger.warning("resume_render fell back to Tinos")
            except Exception:
                self._FONT_FAMILY = "Times"
                logger.warning("resume_render Tinos missing — fell back to core Times")

        if not charis_ok:
            sans = os.path.join(fonts_dir, "NotoSans-Regular.ttf")
            if os.path.isfile(sans):
                try:
                    pdf.add_font(self._FALLBACK_SANS, "", sans)
                    pdf.add_font(self._FALLBACK_SANS, "B", sans)
                    pdf.add_font(self._FALLBACK_SANS, "I", sans)
                    pdf.add_font(self._FALLBACK_SANS, "BI", sans)
                    self._loaded_fonts.add("sans")
                except Exception as e:
                    logger.warning("resume_render NotoSans load failed: %s", e)

        if need_cjk:
            for name in ("NotoSansSC-Regular.otf", "NotoSansSC-Regular.ttf"):
                cjk = os.path.join(fonts_dir, name)
                if os.path.isfile(cjk):
                    try:
                        pdf.add_font(self._FALLBACK_CJK, "", cjk)
                        pdf.add_font(self._FALLBACK_CJK, "B", cjk)
                        pdf.add_font(self._FALLBACK_CJK, "I", cjk)
                        pdf.add_font(self._FALLBACK_CJK, "BI", cjk)
                        self._loaded_fonts.add("cjk")
                    except Exception as e:
                        logger.warning("resume_render NotoSansSC load failed: %s", e)
                    break

    # ------------------------------------------------------------------
    # Measure / render PDF
    # ------------------------------------------------------------------

    def _min_gaps(self) -> GapPlan:
        s = self.spec
        return GapPlan(
            s.min_section_gap, s.min_entry_gap, s.min_post_header,
            s.min_header_gap, s.min_skill_gap, s.margin_lr_mm,
            s.margin_top_mm, s.margin_bottom_mm,
            s.min_pre_bullet, s.min_bullet_gap,
        )

    def _max_gaps(self, margin_lr: Optional[float] = None) -> GapPlan:
        s = self.spec
        return GapPlan(
            s.max_section_gap, s.max_entry_gap, s.max_post_header,
            s.max_header_gap, s.max_skill_gap,
            margin_lr if margin_lr is not None else s.margin_lr_wide_mm,
            s.margin_top_sparse_mm, s.margin_bottom_sparse_mm,
            s.max_pre_bullet, s.max_bullet_gap,
        )

    def _measure(self, tailored: Dict[str, Any], tier: FontTier,
                 gaps: Optional[GapPlan] = None, stretch: float = 1.0) -> float:
        g = gaps or self._min_gaps()
        _, height = self._render_pdf(
            tailored,
            section_gap=g.section_gap, entry_gap=g.entry_gap,
            post_header=g.post_header, header_gap=g.header_gap,
            skill_gap=g.skill_gap, margin_lr=g.margin_lr,
            margin_top=g.margin_top, margin_bottom=g.margin_bottom,
            pre_bullet=g.pre_bullet, bullet_gap=g.bullet_gap,
            body_size=tier.body_pt, lh=tier.lh, lh_s=tier.lh_s,
            stretch=stretch, measure_only=True,
        )
        return height

    def _measure_min(self, tailored: Dict[str, Any], body_size: float,
                     lh: float, lh_s: float) -> float:
        return self._measure(tailored, FontTier(body_size, lh, lh_s))

    def _count_spacing_slots(self, tailored: Dict[str, Any]) -> Dict[str, int]:
        section_count = 0
        entry_gaps = 0
        skill_rows = 0
        if tailored.get("summary"):
            section_count += 1
        experience = tailored.get("experience") or []
        if experience:
            section_count += 1
            entry_gaps += max(0, len(experience) - 1)
        projects = tailored.get("projects") or []
        if projects:
            section_count += 1
            entry_gaps += max(0, len(projects) - 1)
        skills = tailored.get("skills") or {}
        if skills:
            section_count += 1
            skill_rows = sum(1 for v in skills.values() if v)
        if tailored.get("education"):
            section_count += 1
        certs = [c for c in (tailored.get("certifications") or []) if str(c).strip()]
        if certs:
            section_count += 1
        return {"sections": section_count, "entry_gaps": entry_gaps, "skill_rows": skill_rows}

    @staticmethod
    def _lerp(a: float, b: float, t: float) -> float:
        t = max(0.0, min(1.0, t))
        return a + (b - a) * t

    def _distribute_gaps(self, tailored: Dict[str, Any], remaining: float,
                         ramp: float) -> GapPlan:
        s = self.spec
        slots = self._count_spacing_slots(tailored)
        n_sections = max(slots["sections"], 1)
        n_entry_gaps = max(slots["entry_gaps"], 1)
        n_skill_gaps = max(slots["skill_rows"] - 1, 1)
        total_slots = (
            n_sections * 0.5 + n_entry_gaps * 0.6 + n_sections * 0.3
            + 1 + n_skill_gaps * 0.3
        )
        unit = (remaining / total_slots) * ramp if total_slots > 0 and remaining > 0 else 0
        return GapPlan(
            min(s.min_section_gap + unit * 0.5, s.max_section_gap),
            min(s.min_entry_gap + unit * 0.6, s.max_entry_gap),
            s.min_post_header,  # frozen — same heading → rule → content on every page
            min(s.min_header_gap + unit * 1, s.max_header_gap),
            min(s.min_skill_gap + unit * 0.3, s.max_skill_gap),
            max(self._lerp(s.margin_lr_mm, s.margin_lr_wide_mm, ramp), s.margin_lr_mm),
            self._lerp(s.margin_top_mm, s.margin_top_sparse_mm, ramp),
            self._lerp(s.margin_bottom_mm, s.margin_bottom_sparse_mm, ramp),
            self._lerp(s.min_pre_bullet, s.max_pre_bullet, ramp),
            self._lerp(s.min_bullet_gap, s.max_bullet_gap, ramp),
        )

    def _choose_up_tier(self, tailored: Dict[str, Any]
                        ) -> Tuple[float, float, float, float]:
        """Largest up-tier that fits under its max_fill, or the 9.4pt base.

        Sparse pages may take 10pt (the max that keeps section_pt 1.5pt above
        body). The old sparse-floor skip that forced 10pt on half-empty pages
        is gone; 11pt is gone because it would sit 0.5pt under the heading.
        """
        avail = self._AVAIL_H
        max_fills = {10.0: 0.78, 9.7: 0.88}
        for tier in _UP_TIERS:
            height = self._measure_min(tailored, tier.body_pt, tier.lh, tier.lh_s)
            if height <= avail * max_fills[tier.body_pt]:
                return tier.body_pt, tier.lh, tier.lh_s, height
        t = _BASE_TIER
        return t.body_pt, t.lh, t.lh_s, self._measure_min(tailored, t.body_pt, t.lh, t.lh_s)

    def _try_one_page_cascade(self, tailored: Dict[str, Any]
                              ) -> Optional[Tuple[FontTier, float]]:
        """Return (tier, min_height) if content fits one page at that tier."""
        avail = self._AVAIL_H
        body, lh, lh_s, height = self._choose_up_tier(tailored)
        tier = FontTier(body, lh, lh_s)
        if height <= avail:
            return tier, height
        for t in _TIGHT_BASE:
            h = self._measure_min(tailored, t.body_pt, t.lh, t.lh_s)
            if h <= avail:
                return t, h
        for t in (_TIER_92, _TIER_90):
            h = self._measure_min(tailored, t.body_pt, t.lh, t.lh_s)
            if h <= avail:
                return t, h
        return None

    def _finish_one_page(self, tailored: Dict[str, Any], tier: FontTier,
                         min_height: float, dropped: List[DroppedItem],
                         outcome: Outcome) -> PageResolution:
        s = self.spec
        avail = s.usable_height()
        fill_at_min = min_height / avail if avail else 1.0
        sparse = fill_at_min < s.sparse_fill_floor and tier.body_pt >= _UP_TIERS[0].body_pt
        if sparse:
            gaps = self._max_gaps()
            stretch = 1.0
            predicted = self._measure(tailored, tier, gaps, stretch)
            resolved_avail = s.usable_height(gaps.margin_top, gaps.margin_bottom)
            # Wider margins can wrap more — if that overflows, fall back.
            if predicted > resolved_avail:
                gaps = GapPlan(
                    s.max_section_gap, s.max_entry_gap, s.max_post_header,
                    s.max_header_gap, s.max_skill_gap, s.margin_lr_mm,
                    s.margin_top_mm, s.margin_bottom_mm,
                    s.max_pre_bullet, s.max_bullet_gap,
                )
                predicted = self._measure(tailored, tier, gaps, stretch)
                resolved_avail = avail
                if predicted > avail:
                    gaps = self._min_gaps()
                    predicted = min_height
            last = predicted
            return PageResolution(
                tier=tier, gaps=gaps, stretch=stretch, pages=1,
                predicted_height_mm=predicted,
                fill_ratio=min(last / resolved_avail, s.max_fill) if resolved_avail else 0,
                dropped=list(dropped), outcome=outcome,
            )

        remaining = max(0.0, avail * s.target_fill - min_height)
        if fill_at_min >= s.fill_ramp_hi:
            ramp = 1.0
        elif fill_at_min <= s.fill_ramp_lo:
            ramp = 0.0
        else:
            span = s.fill_ramp_hi - s.fill_ramp_lo
            ramp = (fill_at_min - s.fill_ramp_lo) / span
        gaps = self._distribute_gaps(tailored, remaining, ramp)
        stretch = 1.0
        h1 = self._measure(tailored, tier, gaps, 1.0)
        resolved_avail = s.usable_height(gaps.margin_top, gaps.margin_bottom)
        if h1 > resolved_avail * s.max_fill + 0.2:
            gaps = GapPlan(
                gaps.section_gap, gaps.entry_gap, gaps.post_header,
                gaps.header_gap, gaps.skill_gap, s.margin_lr_mm,
                s.margin_top_mm, s.margin_bottom_mm,
                gaps.pre_bullet, gaps.bullet_gap,
            )
            resolved_avail = avail
            h1 = self._measure(tailored, tier, gaps, 1.0)
        target_h = resolved_avail * s.target_fill
        max_h = resolved_avail * s.max_fill
        s_max = min(s.stretch_cap, s.max_leading_mm / max(tier.lh, 0.1))
        s_max = 1.0 + (s_max - 1.0) * ramp
        predicted = h1
        if s_max > 1.01 and (target_h - h1) > 1.0:
            h2 = self._measure(tailored, tier, gaps, s_max)
            if h2 <= max_h:
                stretch = s_max
                predicted = h2
            elif h2 > h1:
                stretch = 1.0 + (target_h - h1) * (s_max - 1.0) / (h2 - h1)
                stretch = max(1.0, min(stretch, s_max))
                hv = self._measure(tailored, tier, gaps, stretch)
                if hv > max_h:
                    stretch = max(1.0, stretch - 0.02)
                    hv = self._measure(tailored, tier, gaps, stretch)
                predicted = hv
        if predicted > max_h and stretch > 1.0:
            stretch = 1.0
            predicted = h1
        fill = predicted / resolved_avail if resolved_avail else 0
        fill = min(fill, s.max_fill)
        return PageResolution(
            tier=tier, gaps=gaps, stretch=stretch, pages=1,
            predicted_height_mm=predicted, fill_ratio=fill,
            dropped=list(dropped), outcome=outcome,
        )

    def _finish_two_page(self, tailored: Dict[str, Any], dropped: List[DroppedItem],
                         outcome: Outcome) -> PageResolution:
        """Comfortable two-page density: start at 10pt, never below the 9.4 floor."""
        s = self.spec
        avail = s.usable_height()
        budget = avail * 2
        chosen = _body_tier(10.0)
        height = self._measure_min(tailored, chosen.body_pt, chosen.lh, chosen.lh_s)
        if height > budget:
            chosen = _TWO_PAGE_FLOOR
            height = self._measure_min(tailored, chosen.body_pt, chosen.lh, chosen.lh_s)
        gaps = self._min_gaps()
        # No leading stretch across a page break.
        predicted = height
        pages = 2
        if avail and predicted > avail * 2 + 0.5:
            pages = max(2, int((predicted - 1e-6) / avail) + 1)
        last = predicted - avail * (pages - 1)
        fill = max(0.0, last / avail) if avail else 0
        return PageResolution(
            tier=chosen, gaps=gaps, stretch=1.0, pages=pages,
            predicted_height_mm=predicted, fill_ratio=fill,
            dropped=list(dropped), outcome=outcome,
        )

    def _trim_to_fit(self, tailored: Dict[str, Any], max_pages: int = 1
                     ) -> Tuple[Dict[str, Any], List[DroppedItem]]:
        """Deterministic trim. Deep-copies. Never touches summary/skills/edu/certs."""
        working = copy.deepcopy(tailored)
        dropped: List[DroppedItem] = []
        avail = self._AVAIL_H * max(1, max_pages)

        def fits() -> bool:
            t = _TWO_PAGE_FLOOR if max_pages >= 2 else _TIER_90
            return self._measure_min(working, t.body_pt, t.lh, t.lh_s) <= avail

        if fits():
            return working, dropped

        # 1. Project bullets beyond a 2-bullet floor (last project first).
        projects = list(working.get("projects") or [])
        for pi in range(len(projects) - 1, -1, -1):
            bullets = list(projects[pi].get("bullets") or [])
            while len(bullets) > 2 and not fits():
                gone = bullets.pop()
                dropped.append(DroppedItem("project_bullet", pi, str(gone)[:80]))
            projects[pi]["bullets"] = bullets
        working["projects"] = projects
        if fits():
            return working, dropped

        # 2. Whole projects beyond the first (last first).
        while len(working.get("projects") or []) > 1 and not fits():
            gone = working["projects"].pop()
            dropped.append(DroppedItem(
                "project", len(working["projects"]),
                str(gone.get("name") or "project")[:80],
            ))
        if fits():
            return working, dropped

        # 3. Oldest-role bullets down to the floor (never recent role below 3).
        experience = list(working.get("experience") or [])
        for ei in range(len(experience) - 1, -1, -1):
            floor = _FLOOR_RECENT if ei == 0 else _FLOOR_OTHER
            bullets = list(experience[ei].get("bullets") or [])
            while len(bullets) > floor and not fits():
                gone = bullets.pop()
                dropped.append(DroppedItem("experience_bullet", ei, str(gone)[:80]))
            experience[ei]["bullets"] = bullets
        working["experience"] = experience
        if fits():
            return working, dropped

        # 4. Oldest role entirely (keep at least the most recent).
        while len(working.get("experience") or []) > 1 and not fits():
            gone = working["experience"].pop()
            dropped.append(DroppedItem(
                "experience", len(working["experience"]),
                str(gone.get("company") or gone.get("title") or "role")[:80],
            ))
        return working, dropped

    def _compute_pdf_layout(self, tailored: Dict[str, Any]) -> Tuple[Dict[str, Any], PageResolution]:
        """Solve layout. Returns (possibly-trimmed resume, resolution)."""
        working = tailored
        dropped: List[DroppedItem] = []

        found = self._try_one_page_cascade(working)
        if found:
            tier, height = found
            res = self._finish_one_page(working, tier, height, dropped, "fit_one")
            return working, res

        # Overflow at the 9.0 floor.
        h9 = self._measure_min(working, _TIER_90.body_pt, _TIER_90.lh, _TIER_90.lh_s)
        overflow_frac = (h9 - self._AVAIL_H) / self._AVAIL_H if self._AVAIL_H else 1.0

        if overflow_frac >= self.spec.min_second_page_fill:
            res = self._finish_two_page(working, dropped, "fit_two")
            if res.predicted_height_mm <= self._AVAIL_H * 2 + 0.5:
                return working, res
            working, dropped = self._trim_to_fit(working, max_pages=2)
            outcome: Outcome = "trimmed_to_two" if dropped else "fit_two"
            return working, self._finish_two_page(working, dropped, outcome)

        working, dropped = self._trim_to_fit(working, max_pages=1)
        found = self._try_one_page_cascade(working)
        if found:
            tier, height = found
            res = self._finish_one_page(working, tier, height, dropped, "trimmed_to_one")
            return working, res

        # Trim exhausted — two pages, never clip.
        outcome: Outcome = "trimmed_to_two" if dropped else "fit_two"
        return working, self._finish_two_page(working, dropped, outcome)

    # ------------------------------------------------------------------
    # PDF drawing
    # ------------------------------------------------------------------

    def _reset_pdf_paint(self, pdf) -> None:
        """fpdf2 markdown can leave fill/draw/text in a dirty state."""
        pdf.set_fill_color(255, 255, 255)
        pdf.set_draw_color(0, 0, 0)
        pdf.set_text_color(0, 0, 0)
        pdf.set_line_width(self.spec.section_hr_width)

    def _draw_skill_row(
        self, pdf, category: str, items, *,
        lh: float, w: float, ml: float, body_size: float,
    ) -> None:
        """Bold category + regular items. Never markdown — `**cat:**` prints
        `<UNKNOWN>` when fpdf2 cannot resolve the style token.

        Height must stay a multiple of `lh` (the density tests difference
        two category counts). `write()` does not advance Y reliably after
        a section rule, so this uses cell / multi_cell only.
        """
        sanitize = self._sanitize_text
        align = self.spec.text_align
        sk = ", ".join(items) if isinstance(items, list) else str(items)
        label = f"{sanitize(category)}: "
        body = sanitize(sk)
        self._assert_renderable(label + body, "skills")
        self._reset_pdf_paint(pdf)
        pdf.set_x(ml)
        self._set_font(pdf, "B", body_size, label)
        lw = pdf.get_string_width(label)
        self._set_font(pdf, "", body_size, body)
        if lw < w * 0.55 and pdf.get_string_width(body) <= (w - lw):
            self._set_font(pdf, "B", body_size, label)
            pdf.cell(lw, lh, label)
            self._set_font(pdf, "", body_size, body)
            pdf.cell(w - lw, lh, body, new_x="LMARGIN", new_y="NEXT")
            return
        self._set_font(pdf, "", body_size, label + body)
        pdf.multi_cell(w, lh, label + body, align=align,
                       new_x="LMARGIN", new_y="NEXT")

    def _draw_split_row(self, pdf, left: str, right: str, *,
                        left_size: float, right_size: float, left_style: str,
                        row_h: float, w: float, ml: float,
                        right_style: str = "B") -> None:
        """Left + right-aligned dates with a reserved gap. Never overruns."""
        self._reset_pdf_paint(pdf)
        s = self.spec
        gap = s.split_row_gap_mm
        align = s.text_align
        left = self._sanitize_text(left)
        right = self._sanitize_text(right)
        self._assert_renderable(left + right, "header row")
        if not right:
            self._set_font(pdf, left_style, left_size, left)
            pdf.multi_cell(w, row_h, left, align=align, new_x="LMARGIN", new_y="NEXT")
            return

        self._set_font(pdf, right_style, right_size, right)
        date_w = pdf.get_string_width(right) + 0.8
        left_w = w - date_w - gap
        if left_w < 20:
            # Pathological: dates take most of the line — wrap left fully, dates below.
            self._set_font(pdf, left_style, left_size, left)
            pdf.multi_cell(w, row_h, left, align=align, new_x="LMARGIN", new_y="NEXT")
            self._set_font(pdf, right_style, right_size, right)
            pdf.cell(w, row_h, right, align="R", new_x="LMARGIN", new_y="NEXT")
            return

        self._set_font(pdf, left_style, left_size, left)
        if pdf.get_string_width(left) <= left_w:
            pdf.cell(left_w, row_h, left)
            # Explicit space cell so pypdf cannot glue location+date.
            pdf.cell(gap, row_h, " ")
            self._set_font(pdf, right_style, right_size, right)
            pdf.cell(date_w, row_h, right, align="R", new_x="LMARGIN", new_y="NEXT")
            return

        # Wrap left into left_w; dates sit on the first line.
        y0 = pdf.get_y()
        self._set_font(pdf, left_style, left_size, left)
        pdf.multi_cell(left_w, row_h, left, align=align, new_x="RIGHT", new_y="TOP")
        y1 = pdf.get_y()
        pdf.set_xy(ml + left_w, y0)
        pdf.cell(gap, row_h, " ")
        self._set_font(pdf, right_style, right_size, right)
        pdf.cell(date_w, row_h, right, align="R")
        pdf.set_y(max(y1, y0 + row_h))
        pdf.set_x(ml)

    def _render_pdf(
        self,
        tailored: Dict[str, Any],
        *,
        section_gap: float,
        entry_gap: float,
        post_header: float,
        header_gap: float,
        skill_gap: float,
        body_size: float = 10.0,
        lh: float = 3.4,
        lh_s: float = 3.08,
        stretch: float = 1.0,
        measure_only: bool = False,
        max_pages: int = 1,
        margin_lr: Optional[float] = None,
        margin_top: Optional[float] = None,
        margin_bottom: Optional[float] = None,
        pre_bullet: Optional[float] = None,
        bullet_gap: Optional[float] = None,
    ) -> Tuple[Optional[bytes], float]:
        from fpdf import FPDF

        s = self.spec
        sanitize = self._sanitize_text
        lh = lh * stretch
        lh_s = lh_s * stretch
        resolved_bullet_gap = (s.min_bullet_gap if bullet_gap is None else bullet_gap) * stretch
        pre_bullet_gap = (s.min_pre_bullet if pre_bullet is None else pre_bullet) * stretch
        edu_entry_gap = s.edu_entry_gap * stretch
        job_title_h = s.job_title_cell_h * stretch

        contact = tailored.get("contact") or {}
        name = sanitize(contact.get("name", "")).strip() or "Resume"
        name = self._title_case_name(name)

        ml = s.margin_lr_mm if margin_lr is None else margin_lr
        mt = s.margin_top_mm if margin_top is None else margin_top
        mb = s.margin_bottom_mm if margin_bottom is None else margin_bottom
        avail = s.usable_height(mt, mb)

        pdf = FPDF(unit="mm", format=(s.page_width_mm, s.page_height_mm))
        # Never clip. If the solver is wrong, fpdf2 emitting a second page
        # is a visible bug, not silent data loss.
        pdf.set_auto_page_break(auto=True, margin=mb)
        need_cjk = bool(_CJK_RE.search(str(tailored)))
        self._register_fonts(pdf, need_cjk=need_cjk)
        self._assert_renderable(name, "name")
        align = s.text_align
        target_role = sanitize(str(tailored.get("target_role") or "")).strip()
        pdf.set_title(f"Resume - {name}")
        pdf.set_author(name)
        pdf.set_subject(target_role or "Resume")
        pdf.set_creator("Portfolio Resume Builder")
        pdf.add_page()
        pdf.set_margins(ml, mt, ml)
        pdf.set_y(mt)
        w = pdf.w - 2 * ml

        self._set_font(pdf, "B", s.name_pt, name)
        pdf.cell(w, s.name_cell_h, name, align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(s.name_contact_gap_mm)

        contact_fields = []
        for key in ("location", "phone", "email", "portfolio", "linkedin", "github"):
            val = contact.get(key, "")
            if val:
                display, href = self._contact_display(key, val)
                contact_fields.append((sanitize(display), href))
        if contact_fields:
            self._set_font(pdf, "", s.contact_pt)
            pdf.set_text_color(0, 0, 0)
            sep = " | "
            sep_w = pdf.get_string_width(sep)
            lines: List[List[Tuple[str, Optional[str]]]] = []
            current_line: List[Tuple[str, Optional[str]]] = []
            current_width = 0.0
            for display, href in contact_fields:
                field_w = pdf.get_string_width(display)
                added_w = field_w if not current_line else (sep_w + field_w)
                if current_line and (current_width + added_w) > w:
                    lines.append(current_line)
                    current_line = [(display, href)]
                    current_width = field_w
                else:
                    current_line.append((display, href))
                    current_width += added_w
            if current_line:
                lines.append(current_line)
            lh_c = s.contact_pt * 25.4 / 72.0
            for li, line in enumerate(lines):
                if li:
                    pdf.ln(s.contact_line_gap_mm)
                line_w = sum(pdf.get_string_width(d) for d, _ in line)
                line_w += sep_w * max(0, len(line) - 1)
                x_start = ml + max(0.0, (w - line_w) / 2)
                pdf.set_x(x_start)
                for i, (display, _href) in enumerate(line):
                    cell_w = pdf.get_string_width(display)
                    pdf.cell(cell_w, lh_c, display)
                    if i < len(line) - 1:
                        pdf.cell(sep_w, lh_c, sep)
                pdf.ln(lh_c)

        pdf.ln(s.contact_section_gap_mm)
        first_section = True

        def section_header(title: str):
            nonlocal first_section
            self._reset_pdf_paint(pdf)
            if first_section:
                first_section = False
            else:
                pdf.ln(section_gap)
            self._set_font(pdf, "B", s.section_pt, title)
            pdf.cell(w, s.heading_cell_h, title.upper(), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(s.heading_rule_gap_mm)
            ry = pdf.get_y()
            pdf.set_draw_color(0, 0, 0)
            pdf.set_line_width(s.section_hr_width)
            pdf.line(ml, ry, ml + w, ry)
            pdf.ln(post_header)

        def bullet(text: str, is_first: bool = False):
            if is_first:
                pdf.ln(pre_bullet_gap)
            else:
                pdf.ln(resolved_bullet_gap)
            safe = sanitize(text)
            self._assert_renderable(safe, "bullet")
            glyph = s.bullet_glyph
            self._set_font(pdf, "", body_size, glyph)
            try:
                marker_w = pdf.get_string_width(glyph)
                if marker_w <= 0:
                    raise ValueError("empty glyph")
            except Exception:
                glyph = s.bullet_glyph_fallback
                self._set_font(pdf, "", body_size, glyph)
                marker_w = pdf.get_string_width(glyph)
            text_indent = s.bullet_indent_mm
            marker_gap = s.bullet_text_gap_mm
            marker_x = ml + text_indent - marker_gap
            text_x = ml + text_indent
            y0 = pdf.get_y()
            pdf.set_xy(marker_x, y0)
            self._set_font(pdf, "", body_size, glyph)
            pdf.cell(marker_gap, lh, glyph)
            self._set_font(pdf, "", body_size, safe)
            pdf.set_xy(text_x, y0)
            pdf.multi_cell(w - text_indent, lh, safe, align=align,
                           new_x="LMARGIN", new_y="NEXT")

        # SUMMARY
        summary = tailored.get("summary", "")
        if summary:
            section_header(s.heading("summary"))
            safe_summary = sanitize(summary)
            self._assert_renderable(safe_summary, "summary")
            self._set_font(pdf, "", body_size, safe_summary)
            lead_match = re.match(
                r'^([^,—\-]+?)(?=\s+(?:with|who|experienced|skilled|specializing|focused|that|building|designing)\b|\s*[—,]\s*|\s+-\s+)',
                safe_summary, flags=re.IGNORECASE,
            )
            lead = lead_match.group(1).strip() if lead_match else ""
            if lead and 3 < len(lead) <= 60 and "**" not in safe_summary:
                rest = safe_summary[len(lead):]
                pdf.multi_cell(w, lh, f"**{lead}**{rest}", markdown=True,
                               align=align, new_x="LMARGIN", new_y="NEXT")
                self._reset_pdf_paint(pdf)
            else:
                pdf.multi_cell(w, lh, safe_summary, align=align,
                               new_x="LMARGIN", new_y="NEXT")

        # EXPERIENCE
        experience = tailored.get("experience") or []
        if experience:
            section_header(s.heading("experience"))
            for i, exp in enumerate(experience):
                company = sanitize(exp.get("company", ""))
                location = sanitize(exp.get("location", ""))
                dates = sanitize(exp.get("dates", ""))
                company_line = f"{company}, {location}" if location else company
                self._draw_split_row(
                    pdf, company_line, dates,
                    left_size=s.entry_pt, right_size=body_size, left_style="B",
                    row_h=s.entry_row_h, w=w, ml=ml,
                )
                title = sanitize(exp.get("title", ""))
                role_type = sanitize(exp.get("type", "")).strip()
                title_line = f"{title} · {role_type}" if title and role_type else title
                if title_line:
                    pdf.ln(header_gap)
                    self._assert_renderable(title_line, "title")
                    self._set_font(pdf, "I", body_size, title_line)
                    pdf.cell(w, job_title_h, title_line, new_x="LMARGIN", new_y="NEXT")
                exp_bullets = exp.get("bullets") or []
                for bi, b in enumerate(exp_bullets):
                    bullet(b, is_first=(bi == 0))
                if i < len(experience) - 1:
                    pdf.ln(entry_gap)

        # PROJECTS
        projects = tailored.get("projects") or []
        if projects:
            section_header(s.heading("projects"))
            for i, proj in enumerate(projects):
                proj_name = sanitize(proj.get("name", ""))
                proj_dates = sanitize(proj.get("dates", ""))
                self._draw_split_row(
                    pdf, proj_name, proj_dates,
                    left_size=s.entry_pt, right_size=body_size, left_style="B",
                    row_h=s.entry_row_h, w=w, ml=ml,
                )
                proj_bullets = proj.get("bullets") or []
                for bi, b in enumerate(proj_bullets):
                    bullet(b, is_first=(bi == 0))
                if i < len(projects) - 1:
                    pdf.ln(entry_gap)

        # SKILLS
        skill_rows = self._prepare_skill_rows(tailored.get("skills") or {})
        if skill_rows:
            skills_heading = self._skills_title(tailored)
            section_header(skills_heading)
            lh_skill = max(min(lh_s, s.max_skill_leading), 3.45)
            for i, (category, skill_list) in enumerate(skill_rows):
                self._draw_skill_row(
                    pdf, category, skill_list,
                    lh=lh_skill, w=w, ml=ml, body_size=body_size,
                )
                if i < len(skill_rows) - 1:
                    pdf.ln(skill_gap)
            self._reset_pdf_paint(pdf)

        # EDUCATION
        education = tailored.get("education") or []
        if education:
            seen_edu = set()
            deduped = []
            for edu in education:
                inst = sanitize(edu.get("institution", "")).strip()
                degree = sanitize(edu.get("degree", "")).strip()
                if not inst and not degree:
                    continue
                key = (inst.lower(), degree.lower())
                if key in seen_edu:
                    continue
                seen_edu.add(key)
                deduped.append(edu)
            if deduped:
                section_header(s.heading("education"))
                for ei, edu in enumerate(deduped):
                    if ei > 0:
                        pdf.ln(edu_entry_gap)
                    inst = sanitize(edu.get("institution", ""))
                    location = sanitize(edu.get("location", ""))
                    degree = sanitize(edu.get("degree", ""))
                    dates = sanitize(edu.get("dates", ""))
                    gpa = sanitize(edu.get("gpa", ""))
                    coursework = sanitize(edu.get("coursework", "")).strip()
                    if degree and re.search(
                        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4}\b",
                        degree, re.IGNORECASE,
                    ):
                        cleaned = re.sub(
                            r"\s*\|?\s*\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*"
                            r"\s+\d{4}\b",
                            "", degree, flags=re.IGNORECASE,
                        ).strip().rstrip("|").strip()
                        if cleaned:
                            degree = cleaned
                    if inst and location and location.lower() in inst.lower():
                        loc_part = ""
                    else:
                        loc_part = location
                    left_bits = [p for p in [inst, loc_part] if p]
                    if gpa:
                        left_bits.append(f"GPA: {gpa}")
                    left_text = " | ".join(left_bits)
                    need = s.entry_row_h
                    if degree:
                        need += header_gap + lh
                    if coursework:
                        need += lh_s
                    if (
                        not measure_only
                        and (pdf.page_break_trigger - pdf.get_y()) < (need + 1.0)
                    ):
                        pdf.add_page()
                        pdf.set_y(mt)
                    self._draw_split_row(
                        pdf, left_text, dates,
                        left_size=s.entry_pt, right_size=body_size, left_style="B",
                        row_h=s.entry_row_h, w=w, ml=ml,
                    )
                    if degree:
                        pdf.ln(header_gap)
                        self._assert_renderable(degree, "degree")
                        self._set_font(pdf, "I", body_size, degree)
                        pdf.multi_cell(w, lh, degree, align=align,
                                       new_x="LMARGIN", new_y="NEXT")
                    if coursework:
                        self._set_font(pdf, "", body_size, coursework)
                        pdf.multi_cell(w, lh_s, coursework, align=align,
                                       new_x="LMARGIN", new_y="NEXT")

        # CERTIFICATIONS — last, never trimmed
        certs = [sanitize(c).strip() for c in (tailored.get("certifications") or []) if str(c).strip()]
        if certs:
            section_header(s.heading("certifications"))
            joined = ", ".join(certs)
            self._assert_renderable(joined, "certifications")
            self._set_font(pdf, "", body_size, joined)
            if pdf.get_string_width(joined) <= w:
                pdf.multi_cell(w, lh_s, joined, align=align, new_x="LMARGIN", new_y="NEXT")
            else:
                for c in certs:
                    pdf.multi_cell(w, lh_s, c, align=align, new_x="LMARGIN", new_y="NEXT")

        if getattr(pdf, "page", 1) > 1:
            content_h = (pdf.page - 1) * avail + (pdf.get_y() - mt)
        else:
            content_h = pdf.get_y() - mt
        if measure_only:
            return None, content_h
        return bytes(pdf.output()), content_h

    def generate_pdf(self, tailored: Dict[str, Any]) -> Tuple[bytes, PageResolution]:
        """Render a PDF. Never clips. Returns (bytes, PageResolution)."""
        working, resolution = self._compute_pdf_layout(tailored)
        pdf_bytes, height = self._render_pdf(
            working,
            section_gap=resolution.gaps.section_gap,
            entry_gap=resolution.gaps.entry_gap,
            post_header=resolution.gaps.post_header,
            header_gap=resolution.gaps.header_gap,
            skill_gap=resolution.gaps.skill_gap,
            margin_lr=resolution.gaps.margin_lr,
            margin_top=resolution.gaps.margin_top,
            margin_bottom=resolution.gaps.margin_bottom,
            pre_bullet=resolution.gaps.pre_bullet,
            bullet_gap=resolution.gaps.bullet_gap,
            body_size=resolution.tier.body_pt,
            lh=resolution.tier.lh,
            lh_s=resolution.tier.lh_s,
            stretch=resolution.stretch,
            max_pages=resolution.pages,
        )
        # Prefer measured height from the final render.
        resolution.predicted_height_mm = height
        avail = self.spec.usable_height(
            resolution.gaps.margin_top, resolution.gaps.margin_bottom,
        )
        actual_pages = 1
        if avail and height > avail + 0.5:
            actual_pages = max(1, int((height - 1e-6) / avail) + 1)
        if actual_pages != resolution.pages:
            logger.error(
                "resume_render page_mismatch solver=%d actual=%d h=%.1f avail=%.1f",
                resolution.pages, actual_pages, height, avail,
            )
            resolution.pages = actual_pages
        if resolution.pages <= 1:
            resolution.fill_ratio = min(height / avail, self.spec.max_fill) if avail else 0
        else:
            last = height - avail * (resolution.pages - 1)
            resolution.fill_ratio = min(max(last, 0) / avail, 1.0) if avail else 0
        logger.info(
            "resume_render outcome=%s pages=%d fill_ratio=%.3f body_pt=%.1f dropped=%d",
            resolution.outcome, resolution.pages, resolution.fill_ratio,
            resolution.tier.body_pt, len(resolution.dropped),
        )
        return pdf_bytes or b"", resolution

    # ------------------------------------------------------------------
    # DOCX — same LayoutSpec + same PageResolution knobs
    # ------------------------------------------------------------------

    def _chars_per_line(self, body_size: float, margin_lr: Optional[float] = None) -> float:
        """Empirical wrap for the PDF face at `body_size` on the current page."""
        s = self.spec
        ml = s.margin_lr_mm if margin_lr is None else margin_lr
        try:
            from fpdf import FPDF
            pdf = FPDF(unit="mm", format=(s.page_width_mm, s.page_height_mm))
            self._register_fonts(pdf)
            sample = "abcdefghijklmnopqrstuvwxyz 0123456789.,"
            self._set_font(pdf, "", body_size, sample)
            avg = pdf.get_string_width(sample) / max(len(sample), 1)
            if avg <= 0:
                raise ValueError("zero width")
            return s.usable_width(ml) / avg
        except Exception:
            scale = 10.0 / max(body_size, 6.0)
            return s.docx_chars_per_line_at_10pt * scale

    def _estimate_docx_height_pt(self, tailored, *, body_size, line_spacing,
                                 section_gap, entry_gap, skill_gap, header_gap) -> float:
        s = self.spec
        chars_per_line = self._chars_per_line(body_size)
        mm_pt = 72.0 / 25.4
        h = 0.0

        def para(text: str, size: float = None, bullet: bool = False,
                 spacing: float = None) -> float:
            sz = size or body_size
            line_h = sz * (line_spacing if spacing is None else spacing)
            if not text:
                return line_h + 1
            n = len(str(text)) + (2 if bullet else 0)
            cpl = max(int(chars_per_line), 1)
            lines = max(1, -(-n // cpl))
            return lines * line_h + 0.35

        h += s.name_pt * 1.15 + s.name_contact_gap_mm * mm_pt
        contact = tailored.get("contact") or {}
        if any(contact.get(k) for k in ("location", "phone", "email", "portfolio", "linkedin", "github")):
            h += s.contact_pt * 1.15 + s.contact_line_gap_mm * mm_pt
        h += s.contact_section_gap_mm * mm_pt

        def section(_title: str) -> float:
            return (s.section_pt * 1.15 + s.heading_rule_gap_mm * mm_pt
                    + section_gap * mm_pt)

        if tailored.get("summary"):
            h += section("SUMMARY")
            h += para(tailored["summary"])
        experience = tailored.get("experience") or []
        if experience:
            h += section("EXPERIENCE")
            for i, exp in enumerate(experience):
                h += max(s.entry_pt * 1.15, body_size * 1.15)
                if exp.get("title") or exp.get("type"):
                    h += header_gap * mm_pt + body_size * line_spacing
                for b in exp.get("bullets") or []:
                    h += para(b, bullet=True)
                if i < len(experience) - 1:
                    h += entry_gap * mm_pt
        projects = tailored.get("projects") or []
        if projects:
            h += section("PROJECTS")
            for i, proj in enumerate(projects):
                h += max(s.entry_pt * 1.15, body_size * 1.15)
                for b in proj.get("bullets") or []:
                    h += para(b, bullet=True)
                if i < len(projects) - 1:
                    h += entry_gap * mm_pt
        skill_rows = self._prepare_skill_rows(tailored.get("skills") or {})
        if skill_rows:
            h += section("SKILLS")
            skill_spacing = min(line_spacing, s.docx_max_skill_spacing)
            for cat, sk in skill_rows:
                items = ", ".join(sk) if isinstance(sk, list) else str(sk)
                h += para(f"{cat}: {items}", spacing=skill_spacing) + skill_gap * mm_pt
        education = tailored.get("education") or []
        if education:
            h += section("EDUCATION")
            for edu in education:
                if edu.get("institution") or edu.get("location"):
                    h += s.entry_pt * 1.15
                if edu.get("degree"):
                    h += header_gap * mm_pt + body_size * line_spacing
                if edu.get("coursework"):
                    h += para(str(edu.get("coursework")))
        certs = [c for c in (tailored.get("certifications") or []) if str(c).strip()]
        if certs:
            h += section("CERTIFICATIONS")
            h += para(", ".join(str(c) for c in certs))
        return h

    def _render_docx(
        self,
        tailored: Dict[str, Any],
        *,
        body_size: float,
        section_gap: float,
        entry_gap: float,
        skill_gap: float,
        header_gap: float,
        line_spacing: float,
        margin_lr_in: float,
        margin_top_in: Optional[float] = None,
        margin_bottom_in: Optional[float] = None,
        post_header: Optional[float] = None,
    ) -> bytes:
        from docx import Document
        from docx.shared import Pt, Inches, Mm, Emu, Twips
        from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
        from docx.oxml.ns import qn
        from docx.oxml import OxmlElement

        s = self.spec
        FONT = s.docx_font_family
        doc = Document()
        top_in = s.top_margin_dense_in if margin_top_in is None else margin_top_in
        bottom_in = s.bottom_margin_dense_in if margin_bottom_in is None else margin_bottom_in
        for section in doc.sections:
            section.page_width = Mm(s.page_width_mm)
            section.page_height = Mm(s.page_height_mm)
            section.top_margin = Inches(top_in)
            section.bottom_margin = Inches(bottom_in)
            section.left_margin = Inches(margin_lr_in)
            section.right_margin = Inches(margin_lr_in)

        style = doc.styles["Normal"]
        style.font.name = FONT
        style.font.size = Pt(body_size)
        style.paragraph_format.space_after = Pt(0)
        style.paragraph_format.space_before = Pt(0)
        style.paragraph_format.line_spacing = line_spacing

        usable_w_emu = section.page_width - section.left_margin - section.right_margin

        def set_run_font(run, size_pt: float, bold=False, italic=False, color="000000"):
            run.font.name = FONT
            run.font.size = Pt(size_pt)
            run.bold = bold
            run.italic = italic
            rPr = run._element.get_or_add_rPr()
            rFonts = rPr.find(qn("w:rFonts"))
            if rFonts is None:
                rFonts = OxmlElement("w:rFonts")
                rPr.append(rFonts)
            for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
                rFonts.set(qn(attr), FONT)
            c = OxmlElement("w:color")
            c.set(qn("w:val"), color)
            rPr.append(c)

        def tight(p, before=0.0, after=0.0, spacing=None):
            p.paragraph_format.space_before = Pt(before)
            p.paragraph_format.space_after = Pt(after)
            p.paragraph_format.line_spacing = line_spacing if spacing is None else spacing

        def add_bottom_border(p, size="6", space="0"):
            pPr = p._p.get_or_add_pPr()
            pBdr = OxmlElement("w:pBdr")
            bottom = OxmlElement("w:bottom")
            bottom.set(qn("w:val"), "single")
            bottom.set(qn("w:sz"), size)
            bottom.set(qn("w:space"), space)
            bottom.set(qn("w:color"), "000000")
            pBdr.append(bottom)
            pPr.append(pBdr)

        first_section = True

        def add_section_header(title: str):
            nonlocal first_section
            p = doc.add_paragraph()
            run = p.add_run(title.upper())
            set_run_font(run, s.section_pt, bold=True)
            before = 0.0 if first_section else section_gap
            first_section = False
            after_pt = (s.min_post_header if post_header is None else post_header) * 72 / 25.4
            tight(p, before=before * 72 / 25.4, after=after_pt)
            rule_space_pt = str(int(round(s.heading_rule_gap_mm * 72 / 25.4)))
            add_bottom_border(p, size="6", space=rule_space_pt)

        def add_bullet(text: str, is_first: bool = False):
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            r_dot = p.add_run(f"{s.bullet_glyph} ")
            set_run_font(r_dot, body_size)
            r = p.add_run(text)
            set_run_font(r, body_size)
            hang_in = s.bullet_text_gap_mm / 25.4
            p.paragraph_format.left_indent = Inches(s.bullet_indent_mm / 25.4)
            p.paragraph_format.first_line_indent = Inches(-hang_in)
            before_pt = (s.min_pre_bullet if is_first else s.min_bullet_gap) * 72 / 25.4
            tight(p, before=before_pt, after=0)

        def header_line(left_bold: str, dates: str):
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            if dates:
                p.paragraph_format.tab_stops.add_tab_stop(
                    usable_w_emu, WD_TAB_ALIGNMENT.RIGHT)
            r1 = p.add_run(left_bold)
            set_run_font(r1, s.entry_pt, bold=True)
            if dates:
                # Leading space before the tab so extraction cannot glue tokens.
                r2 = p.add_run(f" \t{dates}")
                set_run_font(r2, body_size, bold=True)
            tight(p, before=0, after=0)
            return p

        contact = tailored.get("contact") or {}
        name = (contact.get("name", "") or "Resume").strip()
        name = self._title_case_name(name)
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(name)
        set_run_font(run, s.name_pt, bold=True)
        tight(p, before=0, after=s.name_contact_gap_mm * 72 / 25.4)

        contact_fields = []
        for key in ("location", "phone", "email", "portfolio", "linkedin", "github"):
            val = contact.get(key, "")
            if val:
                display, href = self._contact_display(key, val)
                contact_fields.append(display)
        if contact_fields:
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            for i, display in enumerate(contact_fields):
                run = p.add_run(display)
                set_run_font(run, s.contact_pt)
                if i < len(contact_fields) - 1:
                    sep = p.add_run(" | ")
                    set_run_font(sep, s.contact_pt)
            tight(p, before=0, after=s.contact_section_gap_mm * 72 / 25.4)

        summary = tailored.get("summary", "")
        if summary:
            add_section_header(s.heading("summary"))
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            run = p.add_run(summary)
            set_run_font(run, body_size)
            tight(p, before=0, after=0)

        experience = tailored.get("experience") or []
        if experience:
            add_section_header(s.heading("experience"))
            for i, exp in enumerate(experience):
                company = exp.get("company", "")
                location = exp.get("location", "")
                dates = exp.get("dates", "")
                company_line = f"{company}, {location}" if location else company
                header_line(company_line, dates)
                title = exp.get("title", "")
                role_type = (exp.get("type") or "").strip()
                title_line = f"{title} · {role_type}" if title and role_type else title
                if title_line:
                    p2 = doc.add_paragraph()
                    p2.alignment = WD_ALIGN_PARAGRAPH.LEFT
                    r = p2.add_run(title_line)
                    set_run_font(r, body_size, italic=True)
                    tight(p2, before=header_gap * 72 / 25.4, after=0)
                for bi, b in enumerate(exp.get("bullets") or []):
                    add_bullet(b, is_first=(bi == 0))
                if i < len(experience) - 1:
                    spacer = doc.add_paragraph()
                    tight(spacer, before=entry_gap, after=0)

        projects = tailored.get("projects") or []
        if projects:
            add_section_header(s.heading("projects"))
            for i, proj in enumerate(projects):
                header_line(proj.get("name", ""), proj.get("dates", ""))
                for bi, b in enumerate(proj.get("bullets") or []):
                    add_bullet(b, is_first=(bi == 0))
                if i < len(projects) - 1:
                    spacer = doc.add_paragraph()
                    tight(spacer, before=entry_gap, after=0)

        skill_rows = self._prepare_skill_rows(tailored.get("skills") or {})
        if skill_rows:
            add_section_header(self._skills_title(tailored))
            for category, skill_list in skill_rows:
                p = doc.add_paragraph()
                p.alignment = WD_ALIGN_PARAGRAPH.LEFT
                r_cat = p.add_run(f"{category}: ")
                set_run_font(r_cat, body_size, bold=True)
                sk = ", ".join(skill_list) if isinstance(skill_list, list) else str(skill_list)
                r_sk = p.add_run(sk)
                set_run_font(r_sk, body_size)
                tight(p, before=skill_gap * 72 / 25.4, after=0,
                      spacing=min(line_spacing, s.docx_max_skill_spacing))

        education = tailored.get("education") or []
        if education:
            seen = set()
            deduped = []
            for edu in education:
                inst = (edu.get("institution", "") or "").strip()
                degree = (edu.get("degree", "") or "").strip()
                if not inst and not degree:
                    continue
                key = (inst.lower(), degree.lower())
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(edu)
            if deduped:
                add_section_header(s.heading("education"))
                for edu in deduped:
                    inst = edu.get("institution", "") or ""
                    location = edu.get("location", "") or ""
                    degree = edu.get("degree", "") or ""
                    dates = edu.get("dates", "") or ""
                    gpa = edu.get("gpa", "") or ""
                    if degree and re.search(
                        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4}\b",
                        degree, re.IGNORECASE,
                    ):
                        cleaned = re.sub(
                            r"\s*\|?\s*\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*"
                            r"\s+\d{4}\b", "", degree, flags=re.IGNORECASE,
                        ).strip().rstrip("|").strip()
                        if cleaned:
                            degree = cleaned
                    if inst and location and location.lower() in inst.lower():
                        loc_part = ""
                    else:
                        loc_part = location
                    left_bits = [x for x in [inst, loc_part] if x]
                    if gpa:
                        left_bits.append(f"GPA: {gpa}")
                    header_line(" | ".join(left_bits), dates)
                    if degree:
                        p = doc.add_paragraph()
                        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
                        r = p.add_run(degree)
                        set_run_font(r, body_size, italic=True)
                        tight(p, before=header_gap * 72 / 25.4, after=0)
                    coursework = (edu.get("coursework") or "").strip()
                    if coursework:
                        p = doc.add_paragraph()
                        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
                        r = p.add_run(coursework)
                        set_run_font(r, body_size)
                        tight(p, before=0, after=0)

        certs = [str(c).strip() for c in (tailored.get("certifications") or []) if str(c).strip()]
        if certs:
            add_section_header(s.heading("certifications"))
            p = doc.add_paragraph()
            r = p.add_run(", ".join(certs))
            set_run_font(r, body_size)
            tight(p, before=0, after=0)

        buf = io.BytesIO()
        doc.save(buf)
        return buf.getvalue()

    def generate_docx(self, tailored: Dict[str, Any]) -> bytes:
        """DOCX from the same PageResolution the PDF solver produced."""
        working, resolution = self._compute_pdf_layout(tailored)
        s = self.spec
        margin_in = resolution.gaps.margin_lr / 25.4
        ref_ratio = (s.body_lh_mm_at_ref * 72 / 25.4) / s.body_pt
        line_spacing = ref_ratio * resolution.stretch
        line_spacing = max(ref_ratio, min(line_spacing, 1.40))
        return self._render_docx(
            working,
            body_size=resolution.tier.body_pt,
            section_gap=resolution.gaps.section_gap,
            entry_gap=resolution.gaps.entry_gap,
            skill_gap=resolution.gaps.skill_gap,
            header_gap=resolution.gaps.header_gap,
            line_spacing=line_spacing,
            margin_lr_in=margin_in,
            margin_top_in=resolution.gaps.margin_top / 25.4,
            margin_bottom_in=resolution.gaps.margin_bottom / 25.4,
            post_header=resolution.gaps.post_header,
        )

    # ------------------------------------------------------------------
    # Filename helpers (unchanged contract)
    # ------------------------------------------------------------------

    @staticmethod
    def build_filename(tailored: Dict[str, Any], jd_analysis: Dict[str, Any], ext: str) -> str:
        contact = tailored.get("contact", {})
        name = contact.get("name", "").strip()
        name = ResumeRenderer._title_case_name(name)
        parts = name.split()
        first = parts[0] if parts else "Resume"
        last = parts[-1] if len(parts) > 1 else ""
        job_title = jd_analysis.get("job_title", "")

        def clean(val: str) -> str:
            val = re.sub(r"[^a-zA-Z0-9 ]+", " ", val).strip()
            return re.sub(r"\s+", "_", val).strip("_")

        segments = [clean(first), clean(last), clean(job_title)]
        return "_".join(seg for seg in segments if seg) + f".{ext}"

    @staticmethod
    def build_cover_letter_filename(
        candidate_name: str, job_title: str, company: str, ext: str = "pdf",
    ) -> str:
        normalized = ResumeRenderer._title_case_name((candidate_name or "").strip())
        parts = normalized.split()
        first = parts[0] if parts else "Applicant"
        last = parts[-1] if len(parts) > 1 else ""

        def clean(val: str) -> str:
            val = re.sub(r"[^a-zA-Z0-9 ]+", " ", val).strip()
            return re.sub(r"\s+", "_", val).strip("_")

        segments = [clean(first), clean(last), clean(job_title), "Cover_Letter"]
        return "_".join(seg for seg in segments if seg) + f".{ext}"
