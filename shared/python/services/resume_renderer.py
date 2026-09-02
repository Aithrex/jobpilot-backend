"""
Resume document renderer — PDF (fpdf2) and DOCX (python-docx) generation.

Template-based rendering with zero AI involvement.
Format: A4, 0.6in left/right and 0.4in top/bottom, Tinos/Times, adaptive density.
Section order: Summary → Experience → Projects → Technical Skills → Education.
"""
import io
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


def _two_page_enabled() -> bool:
    """Opt-in two-page rendering, for a senior resume that genuinely overflows
    one page at the minimum body size. Default comes from the shared flag
    registry and is identical in every environment."""
    from utils.feature_flags import enabled
    return enabled("ALLOW_TWO_PAGE")


ALLOW_TWO_PAGE = _two_page_enabled()


class ResumeRenderer:
    """Generates PDF and DOCX documents from a structured (tailored) resume dict."""

    # Bump this whenever the rendered OUTPUT changes (margins, fonts, name
    # casing, section layout, bullet rules, etc.). The download endpoint caches
    # rendered files in S3 keyed on the resume *content* hash — but format
    # changes live here in the renderer, not the content, so without a version
    # tag a cache "hit" would keep serving a PDF rendered by the OLD code.
    # Including this in the cache key forces a re-render after any format fix.
    RENDER_VERSION = "2026-09-01.3"  # shared LayoutPlan for PDF and DOCX

    # Allowed headings for the skills section. The tailor may pick one per
    # industry via `skills_section_title` (ResumeTailor normalizes it); this
    # is defense-in-depth so an unexpected value can never reach the page.
    # Every entry must contain the word "Skills" — pdf_ats_check's
    # section-presence check greps the extracted text for "SKILLS".
    SKILLS_TITLES = (
        "Technical Skills",
        "Skills",
        "Core Skills",
        "Professional Skills",
        "Key Skills",
    )

    @classmethod
    def _skills_title(cls, tailored: Dict[str, Any]) -> str:
        title = str(tailored.get("skills_section_title") or "").strip()
        return title if title in cls.SKILLS_TITLES else "Technical Skills"

    @staticmethod
    def _contact_display(key: str, value: str) -> tuple:
        """Return (display_text, href_url_or_None) for a contact field.

        The href element is currently unused by both renderers (contacts
        render as plain text — product decision) but is kept for the display
        normalization callers rely on and a possible future link toggle.
        """
        if not value:
            return ("", None)
        if key == "linkedin":
            display = re.sub(r'^https?://', '', value)  # strip protocol
            href = value if value.startswith("http") else f"https://{value}"
            return (display, href)
        if key == "github":
            display = re.sub(r'^https?://', '', value)
            href = value if value.startswith("http") else f"https://{value}"
            return (display, href)
        if key == "portfolio":
            # Strip protocol for display, link to full URL
            display = re.sub(r'^https?://', '', value).rstrip('/')
            href = value if value.startswith("http") else f"https://{value}"
            return (display, href)
        if key == "email":
            return (value, f"mailto:{value}")
        if key == "location":
            return (value, None)  # plain text, no link
        return (value, None)  # phone — no href

    @staticmethod
    def _title_case_name(name: str) -> str:
        """Normalize candidate name to Title Case.

        If a token is entirely upper- or lower-case, capitalize only the first
        letter ("HARSHITH" -> "Harshith"). If it already has mixed case
        (e.g. "McDonald", "deShawn"), leave it untouched so we don't damage
        legitimately styled surnames.
        """
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
        """Replace Unicode chars unsupported by built-in PDF fonts with latin-1 equivalents."""
        if not text:
            return ""
        text = str(text)
        replacements = {
            "\u2022": "\xb7",  # bullet • → middle dot · (latin-1 compatible)
            "\u2013": " - ",   # en-dash – → spaced hyphen
            "\u2014": " - ",   # em-dash — → spaced hyphen
            "\u2015": " - ",   # horizontal bar ―
            "\u2212": "-",     # minus sign − (Latin-1 safe single hyphen)
            "\u2018": "'",     # left single quote
            "\u2019": "'",     # right single quote / apostrophe
            "\u201c": '"',     # left double quote
            "\u201d": '"',     # right double quote
            "\u2026": "...",   # ellipsis …
            "\u00a0": " ",     # non-breaking space
            "\u200b": "",      # zero-width space
        }
        for char, repl in replacements.items():
            text = text.replace(char, repl)
        # Collapse duplicate spaces that the spaced-hyphen replacement can
        # introduce when the original already had a space (e.g. "x — y" →
        # "x  -  y" → "x - y").
        text = re.sub(r"  +", " ", text)
        # Fallback: strip any remaining non-latin-1 characters
        return text.encode("latin-1", errors="replace").decode("latin-1")

    # ------------------------------------------------------------------
    # PDF generation (fpdf2) — matches LaTeX resume.cls template
    # ------------------------------------------------------------------

    # Layout constants — loosened from a previous aggressive-density pass.
    # The earlier ultra-tight values produced a "packed" look where bullets
    # crashed into role titles and section headers crowded their content.
    # These values give a clean professional appearance while staying within
    # one page for typical 2-experience + up-to-3-project resumes. The Pass 2
    # densest layouts go to a second page at 10pt rather than shrinking below.
    _MARGIN_LR = 15.24         # 0.6 inch left/right
    _MARGIN_TOP = 10.16        # 0.4 inch
    _MARGIN_BOTTOM = 10.16     # 0.4 inch
    _PAGE_H = 297.0            # A4 height mm (Letter is 279.4)
    _AVAIL_H = _PAGE_H - _MARGIN_TOP - _MARGIN_BOTTOM  # ~276.7mm usable

    def _page_spec(self, tailored: Dict[str, Any]) -> Tuple[str, float, float]:
        """Return (fpdf_format, height_mm, width_mm)."""
        fmt = str((tailored or {}).get("_page_format") or "A4").strip()
        if fmt.lower() in ("letter", "us-letter", "usletter"):
            return "Letter", 279.4, 215.9
        return "A4", 297.0, 210.0

    def _activate_page(self, tailored: Dict[str, Any]) -> None:
        fmt, page_h, page_w = self._page_spec(tailored)
        self._layout_format = fmt
        self._layout_page_h = page_h
        self._layout_page_w = page_w
        self._layout_avail_h = page_h - self._MARGIN_TOP - self._MARGIN_BOTTOM

    @property
    def _active_avail_h(self) -> float:
        return getattr(self, "_layout_avail_h", None) or self._active_avail_h

    # Font family: Tinos — the SIL-OFL, metric-identical twin of Times New
    # Roman, matching the user's own Overleaf template (resume.cls compiles
    # with newtxtext, a Times clone). Embedded from
    # shared/python/services/fonts/ at render time; the DOCX renderer names
    # "Times New Roman" directly (on every recipient's machine, same
    # metrics). Falls back to core Times if the TTFs are missing.
    _FONT_FAMILY = "Tinos"

    # Font sizes. Section/company/role-title sizes are DERIVED per render as
    # body_size + 1pt so the hierarchy scales with the adaptive density tier
    # (11pt body → 12pt headers; 9pt body → 10pt headers) instead of headers
    # staying fixed while the body moves.
    _NAME_SIZE = 20.0          # large centered name (recruiter guidance: 20-22)
    _CONTACT_SIZE = 10.0       # contact line
    _BODY_SIZE = 11.0          # body base (overridden by adaptive tier)
    _HEADING_DELTA = 1.0       # section/company/title = body + 1 (template: 12pt at 11 body)
    _BULLET_INDENT = 5.05      # ~0.199in

    # Line heights
    _LH_BODY = 4.2             # body line height
    _LH_BULLET = 4.0           # bullet line height
    _LH_SKILL = 4.0            # skill row line height
    _LH_CONTACT = 4.5          # contact line height

    _SECTION_HR_WIDTH = 0.18   # rule under section header (template: 0.5pt)

    # Spacing floors — minimum visual gaps. Each constant fires multiple times
    # on a project-heavy resume, so values are kept conservative to fit one page:
    #   section_gap fires 5× (Summary, Experience, Projects, Skills, Education)
    #   entry_gap fires up to 4× (between experience entries + between projects)
    #   post_header fires 5× (after each section's HR rule)
    _MIN_SECTION_GAP = 1.2     # before section header (not the first).
                               # 0.3mm tighter than 1.5 so the 1.0mm post-header
                               # floor does not steal experience-bullet budget.
    _MIN_ENTRY_GAP = 0.9       # between experiences / between projects
    _MIN_POST_HEADER = 1.0     # after section HR, before first content
                              # (0.4mm read as touching the rule on dense pages)
    _MIN_HEADER_GAP = 0.5      # after name+contact block
    _MIN_SKILL_GAP = 0.4       # compact separation between skill categories.
                               # Reference LaTeX uses \vspace{-2pt} (tighter
                               # than touching); we keep ours visible but
                               # snug — the previous 0.8mm made skills look
                               # airier than the rest of the resume.
    _BULLET_GAP = 0.6          # gap between bullets within a list — breathing
                               # room so consecutive bullets don't visually merge
    _PRE_BULLET_GAP = 1.2      # gap below italic role title before first bullet
    _EDU_ENTRY_GAP = 1.2       # gap between two education entries
    _JOB_TITLE_CELL_H = 4.0    # italic role-title line height (was 4.5 — the
                               # extra 0.5mm read as dead space under the title)
    _NAME_CELL_H = 6.2         # name line (was 6.5 — offset for post_header floor)
    _HEADING_CELL_H = 4.2      # section title cell (was 4.5 — same offset)

    # Spacing ceilings — how much Pass 2 may inflate on sparse pages.
    # Heading→content, job→job, and between-section gaps stay tight.
    # Leftover whitespace is absorbed by Pass-3 leading, not by holes
    # between SUMMARY / EXPERIENCE / later sections.
    _MAX_SECTION_GAP = 2.8     # before a section header (not the first)
    _MAX_ENTRY_GAP = 2.2
    _MAX_POST_HEADER = 1.6
    _MAX_HEADER_GAP = 2.0
    _MAX_SKILL_GAP = 1.4       # fires once PER CATEGORY, so its ceiling costs
                               # n_categories × the gap. At 2.2 a six-category
                               # block spent 13mm on inter-row air alone and
                               # read far airier than the bullets above it.

    # Skills rows are dense comma lists, not prose — they don't need the body's
    # Pass-3 leading. Without this cap an 11pt sparse page gave them 5.04mm
    # per line (3.60 × 1.40), which is what made the block look padded.
    _MAX_SKILL_LEADING = 4.2
    _DOCX_MAX_SKILL_SPACING = 1.15  # same cap expressed as a DOCX line-spacing

    # Sparse-page guards. A resume with genuinely half a page of content used
    # to saturate everything at once — 11pt type, all five gaps pinned to their
    # ceilings, leading stretched the full 1.40× — and STILL left a blank band
    # at the bottom, so it read as padded and unfinished at the same time.
    # These make the fill effort proportional to how fillable the page is.
    #
    # Measured at minimum gaps: a 2-role / 4-bullet resume is ~55% full at
    # 11pt, the dead-band shape Pass 3 exists for is ~74%, a 4-role senior
    # ~93%. The thresholds sit in the gap between the first two.
    _SPARSE_FILL_FLOOR = 0.62  # refuse an up-tier that still leaves this empty
    _FILL_RAMP_LO = 0.55       # at or below: minimum gaps, no stretch
    _FILL_RAMP_HI = 0.68       # at or above: full Pass-2/3 effort, as before

    # Pass-3 leading ceiling: body line height may stretch up to this many mm.
    # Paired with a 1.40 stretch cap so 11pt body (lh=4.0) can actually reach
    # it — leftover on short pages fills via taller lines, not section gaps.
    _MAX_LEADING_MM = 6.0
    _STRETCH_CAP = 1.40

    # Up-tier ladder: (body_size, lh, lh_s, max_fill). A bigger font must be
    # JUSTIFIED by emptiness — each rung is taken only when the content at
    # that size still leaves max_fill headroom for Pass-2 gap distribution.
    # Without the threshold, a borderline page picked the largest size that
    # barely fit, rendering oversized text with zero breathing room (and
    # Helvetica runs optically larger than Times at equal pt, so 11pt is
    # reserved for genuinely sparse pages). Content-rich resumes stay at the
    # professional 10pt base; denser still → generate_pdf's shrink tiers.
    _UP_TIERS = (
        (11.0, 4.00, 3.60, 0.78),
        (10.5, 3.70, 3.35, 0.88),
    )
    _BASE_TIER = (10.0, 3.40, 3.08)

    def _measure_min(self, tailored: Dict[str, Any], body_size: float,
                     lh: float, lh_s: float) -> float:
        """Content height (mm) at minimum gaps for the given density tier."""
        _, height = self._render_pdf(
            tailored,
            section_gap=self._MIN_SECTION_GAP,
            entry_gap=self._MIN_ENTRY_GAP,
            post_header=self._MIN_POST_HEADER,
            header_gap=self._MIN_HEADER_GAP,
            skill_gap=self._MIN_SKILL_GAP,
            body_size=body_size, lh=lh, lh_s=lh_s,
            measure_only=True,
        )
        return height

    def _choose_up_tier(
        self, tailored: Dict[str, Any]
    ) -> "tuple[float, float, float, float]":
        """Pick the largest up-tier whose min-gap fill stays UNDER that
        rung's max_fill threshold (bigger font ⇒ more emptiness required).

        Returns (body_size, lh, lh_s, min_height). Falls through to the 10pt
        base tier (possibly overflowing — generate_pdf's shrink tiers take it
        from there).
        """
        for body_size, lh, lh_s, max_fill in self._UP_TIERS:
            height = self._measure_min(tailored, body_size, lh, lh_s)
            # Sparse floor: on a page that will still be mostly empty, a bigger
            # font only makes the emptiness louder. Skipping the rung drops the
            # resume to the professional 10pt base instead.
            if height < self._active_avail_h * self._SPARSE_FILL_FLOOR:
                continue
            if height <= self._active_avail_h * max_fill:
                return body_size, lh, lh_s, height
        body_size, lh, lh_s = self._BASE_TIER
        return body_size, lh, lh_s, self._measure_min(tailored, body_size, lh, lh_s)

    def _count_spacing_slots(self, tailored: Dict[str, Any]) -> Dict[str, int]:
        """Count the number of each spacing slot type in the resume."""
        section_count = 0
        entry_gaps = 0
        skill_rows = 0

        if tailored.get("summary"):
            section_count += 1
        experience = tailored.get("experience", [])
        if experience:
            section_count += 1
            entry_gaps += max(0, len(experience) - 1)
        projects = tailored.get("projects", [])
        if projects:
            section_count += 1
            entry_gaps += max(0, len(projects) - 1)
        skills = tailored.get("skills", {})
        if skills:
            section_count += 1
            skill_rows = sum(1 for v in skills.values() if v)
        if tailored.get("education"):
            section_count += 1
        if tailored.get("licenses"):
            section_count += 1
        if tailored.get("certifications"):
            section_count += 1
        for key in (
            "languages", "awards", "publications", "patents",
            "leadership", "volunteering", "associations", "clearance",
        ):
            if tailored.get(key):
                section_count += 1

        return {
            "sections": section_count,
            "entry_gaps": entry_gaps,
            "skill_rows": skill_rows,
        }

    def _register_fonts(self, pdf) -> None:
        """Embed the Tinos family (Times metrics, SIL OFL) into the PDF.

        Falls back to core Times when the TTFs are unavailable so a
        missing font file degrades the look, never the render.
        """
        import os

        fonts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
        try:
            pdf.add_font(self._FONT_FAMILY, "", os.path.join(fonts_dir, "Tinos-Regular.ttf"))
            pdf.add_font(self._FONT_FAMILY, "B", os.path.join(fonts_dir, "Tinos-Bold.ttf"))
            pdf.add_font(self._FONT_FAMILY, "I", os.path.join(fonts_dir, "Tinos-Italic.ttf"))
            pdf.add_font(self._FONT_FAMILY, "BI", os.path.join(fonts_dir, "Tinos-BoldItalic.ttf"))
        except Exception:
            # Instance attribute shadows the class constant for this renderer.
            self._FONT_FAMILY = "Times"

    def _render_pdf(
        self,
        tailored: Dict[str, Any],
        *,
        section_gap: float,
        entry_gap: float,
        post_header: float,
        header_gap: float,
        skill_gap: float,
        body_size: float = 11.0,
        lh: float = 4.2,
        lh_s: float = 4.0,
        stretch: float = 1.0,
        measure_only: bool = False,
        max_pages: int = 1,
    ) -> "tuple[bytes | None, float]":
        """Core rendering logic matching the LaTeX resume.cls template.

        Returns (pdf_bytes_or_None, content_height_mm). When `max_pages > 1`
        (Phase 5), fpdf auto-page-break is enabled so content flows onto a
        second page instead of overflowing; the returned height then spans all
        pages used (page-count * usable-height + last-page content) so the
        layout solver measures true total content.
        If measure_only is True, pdf_bytes is None (saves memory).

        `stretch` scales body leading and intra-entry gaps continuously
        (Pass 3): line height doesn't change wrapping, so content height is
        linear in it. Header/name/contact cell heights stay fixed on purpose —
        headers hug their content; only body text breathes.
        """
        from fpdf import FPDF
        sanitize = self._sanitize_text

        # Pass-3 leading stretch — body leading + the gaps that read as part
        # of the body rhythm. Section/entry gaps are Pass-2 knobs, not scaled.
        lh = lh * stretch
        lh_s = lh_s * stretch
        bullet_gap = self._BULLET_GAP * stretch
        pre_bullet_gap = self._PRE_BULLET_GAP * stretch
        edu_entry_gap = self._EDU_ENTRY_GAP * stretch
        job_title_h = self._JOB_TITLE_CELL_H * stretch

        contact = tailored.get("contact", {})
        name = sanitize(contact.get("name", "")).strip() or "Resume"
        name = self._title_case_name(name)
        ml = self._MARGIN_LR
        mt = self._MARGIN_TOP
        # Section/company/role-title size scales with the density tier so the
        # visual hierarchy holds at every body size (11 → 12pt headers, etc).
        heading_size = body_size + self._HEADING_DELTA

        self._activate_page(tailored)
        pdf = FPDF(format=getattr(self, "_layout_format", "A4"))
        # Single-page (default): no auto break — content that overflows is
        # caught by the layout solver's shrink cascade. Multi-page (Phase 5):
        # enable auto break at the bottom margin so overflow flows to page 2+
        # instead of being clipped.
        if max_pages > 1:
            pdf.set_auto_page_break(auto=True, margin=self._MARGIN_BOTTOM)
        else:
            pdf.set_auto_page_break(auto=False)
        self._register_fonts(pdf)
        # ATS hint: enterprise screeners (Workday, iCIMS) sometimes flag
        # PDFs with empty Document Information as low-confidence — populating
        # Title/Author/Subject also makes the file readable when recruiters
        # save it locally and sort by metadata.
        target_role = sanitize(str(tailored.get("target_role") or "")).strip()
        pdf.set_title(f"Resume - {name}")
        pdf.set_author(name)
        pdf.set_subject(target_role or "Resume")
        pdf.set_creator("Portfolio Resume Builder")
        pdf.add_page()
        pdf.set_margins(ml, mt, ml)
        pdf.set_y(mt)
        w = pdf.w - 2 * ml  # usable width ≈ 184.6mm

        # Name in Title Case (centered, bold) — e.g. "Harshith Siddardha
        # Manne" rather than "HARSHITH SIDDARDHA MANNE". `name` is already
        # normalized to Title Case above via _title_case_name.
        pdf.set_font(self._FONT_FAMILY, "B", self._NAME_SIZE)
        pdf.cell(w, self._NAME_CELL_H, name, align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(0.5)  # \nameskip = \smallskip

        # ── Contact line (centered, pipe-separated, plain black text) ──
        # LaTeX: \fontsize{11}{13}\selectfont, pipe separator
        # Product decision: contact fields render as PLAIN TEXT — no
        # hyperlinks, no link color. _contact_display() still normalizes the
        # display form (protocol stripping); its href is intentionally unused.
        # Order matches the LaTeX template's reference resume:
        #   location | phone | email | portfolio | linkedin | github
        contact_fields = []
        for key in ("location", "phone", "email", "portfolio", "linkedin", "github"):
            val = contact.get(key, "")
            if val:
                display, href = self._contact_display(key, val)
                contact_fields.append((sanitize(display), href))

        if contact_fields:
            pdf.set_font(self._FONT_FAMILY, "", self._CONTACT_SIZE)
            pdf.set_text_color(0, 0, 0)
            sep = " | "
            sep_w = pdf.get_string_width(sep)

            # Wrap-aware layout: distribute contact fields across multiple
            # lines so the line never exceeds page width. Previously a single
            # too-wide line produced a negative x_start, pushing most of the
            # text off the page (only the right-most fragment showed up).
            # Matches the reference template which wraps when total width
            # exceeds the page (Tempe, AZ | phone | email | portfolio |
            # linkedin |\n github).
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

            for line in lines:
                line_w = sum(pdf.get_string_width(d) for d, _ in line)
                line_w += sep_w * max(0, len(line) - 1)
                x_start = ml + max(0.0, (w - line_w) / 2)
                pdf.set_x(x_start)
                for i, (display, _href) in enumerate(line):
                    cell_w = pdf.get_string_width(display)
                    pdf.cell(cell_w, self._LH_CONTACT, display)
                    if i < len(line) - 1:
                        pdf.cell(sep_w, self._LH_CONTACT, sep)
                pdf.ln(self._LH_CONTACT)

        pdf.ln(header_gap)

        # ── Helper: section header — UPPERCASE title + hairline ──
        # First section sits directly under the contact block (header_gap
        # already applied). Later sections get section_gap so EXPERIENCE /
        # PROJECTS still separate from the previous block, without stacking
        # both gaps above SUMMARY.
        first_section = True

        def section_header(title: str):
            nonlocal first_section
            if first_section:
                first_section = False
            else:
                pdf.ln(section_gap)
            pdf.set_font(self._FONT_FAMILY, "B", heading_size)
            pdf.cell(w, self._HEADING_CELL_H, title.upper(), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(0.15)
            ry = pdf.get_y()
            pdf.set_draw_color(0, 0, 0)
            pdf.set_line_width(self._SECTION_HR_WIDTH)
            pdf.line(ml, ry, ml + w, ry)
            pdf.ln(post_header)

        # ── Helper: bullet point — constant spacing between bullets ──
        def bullet(text: str, is_first: bool = False):
            if is_first:
                pdf.ln(pre_bullet_gap)  # gap before first bullet
            else:
                pdf.ln(bullet_gap)      # constant gap between bullets
            indent = self._BULLET_INDENT
            pdf.set_font(self._FONT_FAMILY, "", body_size)
            x_start = pdf.l_margin + indent
            y_center = pdf.get_y() + lh / 2

            # Small filled circle bullet (Latin-1 safe)
            pdf.set_fill_color(0, 0, 0)
            pdf.circle(x_start + 0.8, y_center, 0.6, style="F")

            pdf.set_x(x_start + 2.5)
            pdf.multi_cell(w - indent - 2.5, lh, sanitize(text),
                           new_x="LMARGIN", new_y="NEXT")

        # ── SUMMARY ── (LaTeX: rSection{SUMMARY})
        # Reference template uses the short "SUMMARY" header. First
        # descriptor phrase rendered bold via multi_cell markdown — safer
        # than pdf.write() which doesn't reset X reliably between
        # font-weight switches.
        summary = tailored.get("summary", "")
        if summary:
            section_header("Summary")
            pdf.set_font(self._FONT_FAMILY, "", body_size)
            safe_summary = sanitize(summary)
            # Detect the lead descriptor phrase: everything up to the first
            # connective ("with", "who", "experienced", "skilled",
            # "specializing", "focused", "that", "building", "designing",
            # comma, en/em-dash). Bold only if ≤ 60 chars and ≥ 4 chars.
            lead_match = re.match(
                r'^([^,—\-]+?)(?=\s+(?:with|who|experienced|skilled|specializing|focused|that|building|designing)\b|\s*[—,]\s*|\s+-\s+)',
                safe_summary,
                flags=re.IGNORECASE,
            )
            lead = lead_match.group(1).strip() if lead_match else ""
            if lead and 3 < len(lead) <= 60 and "**" not in safe_summary:
                # Wrap lead in markdown bold; multi_cell handles wrapping
                # within `w` correctly, unlike pdf.write().
                rest = safe_summary[len(lead):]
                rendered = f"**{lead}**{rest}"
                pdf.multi_cell(w, lh, rendered, markdown=True,
                               new_x="LMARGIN", new_y="NEXT")
            else:
                pdf.multi_cell(w, lh, safe_summary,
                               new_x="LMARGIN", new_y="NEXT")

        creds_early = bool((tailored.get("content_plan") or {}).get("credentials_first"))

        def emit_credentials():
            licenses = [
                sanitize(c) for c in (tailored.get("licenses") or [])
                if isinstance(c, str) and c.strip()
            ]
            if licenses:
                section_header("Licenses")
                pdf.set_font(self._FONT_FAMILY, "", body_size)
                pdf.multi_cell(w, lh_s, "  ·  ".join(licenses),
                               new_x="LMARGIN", new_y="NEXT")
            certs = [
                sanitize(c) for c in (tailored.get("certifications") or [])
                if isinstance(c, str) and c.strip()
            ]
            if certs:
                section_header("Certifications")
                pdf.set_font(self._FONT_FAMILY, "", body_size)
                pdf.multi_cell(w, lh_s, "  ·  ".join(certs),
                               new_x="LMARGIN", new_y="NEXT")

        if creds_early:
            emit_credentials()

        # ── EXPERIENCE ── (LaTeX: rSection{EXPERIENCE})
        experience = tailored.get("experience", [])
        if experience:
            section_header("Experience")
            for i, exp in enumerate(experience):
                company = sanitize(exp.get("company", ""))
                location = sanitize(exp.get("location", ""))
                # Dates are already normalized by services.date_normalizer
                # before reaching the renderer (e.g. "August 2025 - Present"
                # with ASCII hyphen). Pass through unchanged so the rendered
                # PDF text matches the JSON exactly — the ATS parseability
                # check tokenizes the JSON and requires those tokens to
                # appear verbatim in the extracted PDF text.
                dates = sanitize(exp.get("dates", ""))
                company_line = f"{company}, {location}" if location else company

                # Company bold + dates right — LaTeX: \textbf{COMPANY} \hfill {dates}
                pdf.set_font(self._FONT_FAMILY, "B", heading_size)
                if dates:
                    pdf.set_font(self._FONT_FAMILY, "", body_size)
                    date_w = pdf.get_string_width(dates) + 2
                    pdf.set_font(self._FONT_FAMILY, "B", heading_size)
                    pdf.cell(w - date_w, 4.5, company_line)
                    pdf.set_font(self._FONT_FAMILY, "", body_size)
                    pdf.cell(date_w, 4.5, dates, align="R",
                             new_x="LMARGIN", new_y="NEXT")
                else:
                    pdf.cell(w, 4.5, company_line,
                             new_x="LMARGIN", new_y="NEXT")

                # Job title italic — LaTeX: \fontsize{12}{14}\selectfont\textit{title}
                title = sanitize(exp.get("title", ""))
                emp_type = sanitize(exp.get("type", ""))
                if title and emp_type:
                    title = f"{title} ({emp_type})"
                if title:
                    pdf.set_font(self._FONT_FAMILY, "I", heading_size)
                    pdf.cell(w, job_title_h, title,
                             new_x="LMARGIN", new_y="NEXT")

                exp_bullets = exp.get("bullets", [])
                for bi, b in enumerate(exp_bullets):
                    bullet(b, is_first=(bi == 0))
                if i < len(experience) - 1:
                    pdf.ln(entry_gap)

        # ── PROJECTS ── (LaTeX: rSection{PROJECTS})
        projects = tailored.get("projects", [])
        if projects:
            section_header("Projects")
            for i, proj in enumerate(projects):
                # Project name bold — LaTeX: \textbf{Project Name}
                pdf.set_font(self._FONT_FAMILY, "B", heading_size)
                proj_name = sanitize(proj.get("name", ""))
                proj_dates = sanitize(proj.get("dates", ""))
                if proj_dates:
                    pdf.set_font(self._FONT_FAMILY, "", body_size)
                    date_w = pdf.get_string_width(proj_dates) + 2
                    pdf.set_font(self._FONT_FAMILY, "B", heading_size)
                    pdf.cell(w - date_w, 4.5, proj_name)
                    pdf.set_font(self._FONT_FAMILY, "", body_size)
                    pdf.cell(date_w, 4.5, proj_dates, align="R",
                             new_x="LMARGIN", new_y="NEXT")
                else:
                    pdf.cell(w, 4.5, proj_name,
                             new_x="LMARGIN", new_y="NEXT")

                tech = sanitize(proj.get("tech", ""))
                if tech:
                    pdf.set_font(self._FONT_FAMILY, "I", body_size)
                    pdf.set_text_color(85, 85, 85)
                    pdf.multi_cell(w, lh_s, tech,
                                   new_x="LMARGIN", new_y="NEXT")
                    pdf.set_text_color(0, 0, 0)

                proj_bullets = proj.get("bullets", [])
                for bi, b in enumerate(proj_bullets):
                    bullet(b, is_first=(bi == 0))
                if i < len(projects) - 1:
                    pdf.ln(entry_gap)

        # ── TECHNICAL SKILLS ── (LaTeX: rSection{TECHNICAL SKILLS})
        skills = tailored.get("skills", {})
        if skills:
            section_header(self._skills_title(tailored))
            # Use a slightly looser line-height than the bullet tight value
            # so a category that wraps onto a second line still reads as one
            # cohesive block. Without this, wrapped categories collapse onto
            # the following category and the boundary disappears.
            lh_skill = max(min(lh_s, self._MAX_SKILL_LEADING), 3.45)
            rows = [(cat, items) for cat, items in skills.items() if items]
            for i, (category, skill_list) in enumerate(rows):
                # Bold category + regular skills in one multi_cell via markdown.
                # multi_cell wraps all lines to the left margin.
                pdf.set_font(self._FONT_FAMILY, "", body_size)
                sk = ", ".join(skill_list) if isinstance(skill_list, list) else str(skill_list)
                line = f"**{sanitize(category)}:** {sanitize(sk)}"
                pdf.multi_cell(w, lh_skill, line, markdown=True,
                               new_x="LMARGIN", new_y="NEXT")
                # The last category gets no trailing gap — it would stack on
                # top of the following section_gap and read as a double break.
                if i < len(rows) - 1:
                    pdf.ln(skill_gap)

        # ── EDUCATION ── (LaTeX: rSection{Education})
        education = tailored.get("education", [])
        if education:
            seen_edu = set()
            deduped_education = []
            for edu in education:
                inst = sanitize(edu.get("institution", "")).strip()
                degree = sanitize(edu.get("degree", "")).strip()
                if not inst and not degree:
                    continue
                key = (inst.lower(), degree.lower())
                if key in seen_edu:
                    continue
                seen_edu.add(key)
                deduped_education.append(edu)

            if deduped_education:
                section_header("Education")
                for ei, edu in enumerate(deduped_education):
                    if ei > 0:
                        pdf.ln(edu_entry_gap)  # gap between education entries
                    inst = sanitize(edu.get("institution", ""))
                    location = sanitize(edu.get("location", ""))
                    degree = sanitize(edu.get("degree", ""))
                    dates = sanitize(edu.get("dates", ""))
                    gpa = sanitize(edu.get("gpa", ""))

                    # Strip date patterns baked into degree by AI
                    if degree and re.search(
                        r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4}\b',
                        degree, re.IGNORECASE,
                    ):
                        cleaned = re.sub(
                            r'\s*\|?\s*\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*'
                            r'\s+\d{4}\b',
                            '', degree, flags=re.IGNORECASE,
                        ).strip().rstrip('|').strip()
                        if cleaned:
                            degree = cleaned

                    # Reference layout:
                    #   Institution[, Location] | Degree[, GPA: X.XX]   <date right-aligned>
                    # Skip location if it's already substring of institution
                    # (e.g. "JNTU Kakinada" + location "Kakinada" → don't
                    # render "JNTU Kakinada, Kakinada").
                    if inst and location and location.lower() in inst.lower():
                        inst_part = inst
                    elif inst and location:
                        inst_part = f"{inst}, {location}"
                    else:
                        inst_part = inst
                    left_parts = [p for p in [inst_part, degree] if p]
                    if gpa:
                        left_parts.append(f"GPA: {gpa}")
                    left_text = " | ".join(left_parts)

                    pdf.set_font(self._FONT_FAMILY, "", body_size)
                    if dates:
                        date_w = pdf.get_string_width(dates) + 2
                        pdf.cell(w - date_w, lh_s, left_text)
                        pdf.cell(date_w, lh_s, dates, align="R",
                                 new_x="LMARGIN", new_y="NEXT")
                    else:
                        pdf.multi_cell(w, lh_s, left_text,
                                       new_x="LMARGIN", new_y="NEXT")
                    coursework = sanitize(edu.get("coursework", ""))
                    if coursework:
                        pdf.set_font(self._FONT_FAMILY, "I", body_size)
                        pdf.multi_cell(w, lh_s, coursework,
                                       new_x="LMARGIN", new_y="NEXT")

        if not creds_early:
            emit_credentials()

        extra_titles = (
            ("languages", "Languages"),
            ("awards", "Awards"),
            ("publications", "Publications"),
            ("patents", "Patents"),
            ("leadership", "Leadership"),
            ("volunteering", "Volunteering"),
            ("associations", "Associations"),
            ("clearance", "Clearance"),
        )
        for key, title in extra_titles:
            items = [
                sanitize(c) for c in (tailored.get(key) or [])
                if isinstance(c, str) and c.strip()
            ]
            if items:
                section_header(title)
                pdf.set_font(self._FONT_FAMILY, "", body_size)
                pdf.multi_cell(w, lh_s, "  ·  ".join(items),
                               new_x="LMARGIN", new_y="NEXT")

        # Total content height. Single page: current Y minus top margin.
        # Multi-page: each completed page contributed a full usable height, plus
        # the content on the final page. `pdf.page` is the current (1-based) page
        # number after all content is drawn.
        if max_pages > 1 and getattr(pdf, "page", 1) > 1:
            content_h = (pdf.page - 1) * self._active_avail_h + (pdf.get_y() - mt)
        else:
            content_h = pdf.get_y() - mt
        if measure_only:
            return None, content_h
        return bytes(pdf.output()), content_h

    def _compute_pdf_layout(self, tailored: Dict[str, Any]) -> Dict[str, float]:
        """Solve the page layout in three passes and return the render knobs.

        Pass 1: choose the largest font tier that fits (or two pages on overflow).
        Pass 2: distribute leftover whitespace into gaps, capped at ceilings.
        Pass 3: continuous leading stretch.
        """
        self._activate_page(tailored)
        avail = self._active_avail_h
        slots = self._count_spacing_slots(tailored)

        # --- Phase 5: two-page decision (opt-in) ---
        # Before shrinking a dense senior resume down to 9pt to force one page,
        # check whether it genuinely overflows the densest 10pt single page. If
        # so AND two-page is allowed, render on two pages at a readable 10pt
        # instead of an unreadable 9pt single page. When ALLOW_TWO_PAGE is off
        # (default in prod), max_pages stays 1 and behavior is byte-identical.
        max_pages = 1
        if ALLOW_TWO_PAGE:
            _, densest_1page = self._render_pdf(
                tailored,
                section_gap=self._MIN_SECTION_GAP, entry_gap=self._MIN_ENTRY_GAP,
                post_header=self._MIN_POST_HEADER, header_gap=self._MIN_HEADER_GAP,
                skill_gap=self._MIN_SKILL_GAP,
                body_size=10.0, lh=3.10, lh_s=3.08, measure_only=True,
            )
            if densest_1page > self._active_avail_h:
                max_pages = 2
                avail = self._active_avail_h * 2  # two-page budget

        # --- Pass 1: choose the LARGEST presentation that fits (up-tiers) ---
        # Bidirectional density: sparse resumes (e.g. an experienced 2-role
        # resume with no generated projects) used to render at a fixed 10pt
        # and leave a ~40mm dead band at the bottom of the page. The up-tier
        # ladder scales the body font and leading UP first (11pt airy →
        # 10.5pt) and only then falls back to the 10pt base + the shrink
        # tiers below. 11pt is the ceiling — 12pt body reads as padding.
        body_size, lh, lh_s, min_height = self._choose_up_tier(tailored)

        # --- Overflow protection: tiered fallback (10pt-tight → 10pt-ultra → 9.5pt → 9pt) ---
        # Body font size is what recruiters notice most — keep it at 10pt
        # whenever possible. Two tight-lh tiers stack before any font drop:
        #   Tier 1: lh=3.35 / lh_s=3.08 — keeps per-bullet-line breathing room
        #   Tier 2: lh=3.22 / lh_s=3.08
        #   Tier 3: lh=3.10 / lh_s=3.08 — densest 10pt tier before a font drop
        # lh_s (skill/edu rows) is held at 3.08 across all 10pt tiers so those
        # rows keep a consistent line height regardless of which tier fits.
        # Only past Tier 3 do we drop to 9.5pt.
        if min_height > avail:
            for tier_lh, tier_lhs in ((3.35, 3.08), (3.22, 3.08), (3.10, 3.08)):
                _, retry_height = self._render_pdf(
                    tailored,
                    section_gap=self._MIN_SECTION_GAP,
                    entry_gap=self._MIN_ENTRY_GAP,
                    post_header=self._MIN_POST_HEADER,
                    header_gap=self._MIN_HEADER_GAP,
                    skill_gap=self._MIN_SKILL_GAP,
                    body_size=10.0, lh=tier_lh, lh_s=tier_lhs,
                    measure_only=True, max_pages=max_pages,
                )
                if retry_height <= avail:
                    lh = tier_lh
                    lh_s = tier_lhs
                    min_height = retry_height
                    break

        if min_height > avail and max_pages == 1 and ALLOW_TWO_PAGE:
            # Never drop below 10pt. Overflow at the densest 10pt tier
            # becomes a second page instead of an unreadable shrink.
            max_pages = 2
            avail = self._active_avail_h * 2
            body_size = 10.0
            lh = 3.10
            lh_s = 3.08
            _, min_height = self._render_pdf(
                tailored,
                section_gap=self._MIN_SECTION_GAP,
                entry_gap=self._MIN_ENTRY_GAP,
                post_header=self._MIN_POST_HEADER,
                header_gap=self._MIN_HEADER_GAP,
                skill_gap=self._MIN_SKILL_GAP,
                body_size=body_size, lh=lh, lh_s=lh_s,
                measure_only=True, max_pages=max_pages,
            )

        # --- Pass 2: distribute remaining whitespace ---
        remaining = max(0, avail - min_height)

        # Fill-aware ramp: how hard Passes 2 and 3 should try to reach the
        # bottom. A page that CAN be filled honestly keeps the full effort; one
        # that can't gets minimum gaps and no stretch, so it reads as a short
        # resume rather than a padded one. Held at 1.0 on the opt-in two-page
        # path, where `avail` is a two-page budget and the ratio is meaningless.
        if max_pages > 1:
            ramp = 1.0
        else:
            fill = min_height / avail if avail > 0 else 1.0
            span = self._FILL_RAMP_HI - self._FILL_RAMP_LO
            ramp = (fill - self._FILL_RAMP_LO) / span if span > 0 else 1.0
            ramp = max(0.0, min(1.0, ramp))

        # Weight distribution: leftover must not open holes between sections,
        # under headings, or between roles. Pass-3 leading absorbs the rest.
        n_sections = max(slots["sections"], 1)
        n_entry_gaps = max(slots["entry_gaps"], 1)
        # skill_gap renders BETWEEN categories, so a 6-row block has 5 gaps.
        n_skill_gaps = max(slots["skill_rows"] - 1, 1)

        total_slots = (
            n_sections * 0.5    # section_gap — keep SUMMARY→EXPERIENCE tight
            + n_entry_gaps * 0.6  # entry_gap — keep jobs tight
            + n_sections * 0.3  # post_header — keep heading→content tight
            + 1 * 1             # header_gap weight
            + n_skill_gaps * 0.3  # skill_gap — the airiest knob per mm, since
                                  # it repeats once per category
        )

        if total_slots > 0 and remaining > 0:
            unit = (remaining / total_slots) * ramp
        else:
            unit = 0

        section_gap = min(self._MIN_SECTION_GAP + unit * 0.5, self._MAX_SECTION_GAP)
        entry_gap = min(self._MIN_ENTRY_GAP + unit * 0.6, self._MAX_ENTRY_GAP)
        post_header = min(self._MIN_POST_HEADER + unit * 0.3, self._MAX_POST_HEADER)
        header_gap = min(self._MIN_HEADER_GAP + unit * 1, self._MAX_HEADER_GAP)
        skill_gap = min(self._MIN_SKILL_GAP + unit * 0.3, self._MAX_SKILL_GAP)

        # --- Pass 3: continuous leading stretch absorbs what the gap ---
        # --- ceilings couldn't ---
        knobs = dict(
            section_gap=section_gap,
            entry_gap=entry_gap,
            post_header=post_header,
            header_gap=header_gap,
            skill_gap=skill_gap,
            body_size=body_size, lh=lh, lh_s=lh_s,
            max_pages=max_pages,
        )
        stretch = 1.0
        _, h1 = self._render_pdf(tailored, **knobs, stretch=1.0,
                                 measure_only=True)
        predicted = h1
        # Stretch cap held at _STRETCH_CAP — beyond this, leading reads as
        # padding. The ramp then pulls it back toward 1.0 on pages too sparse
        # to fill honestly, so a half-page resume keeps normal leading instead
        # of stretching into a full page of air.
        s_max = min(self._STRETCH_CAP, self._MAX_LEADING_MM / lh)
        s_max = 1.0 + (s_max - 1.0) * ramp
        # Leading stretch fills a single page's dead band. On a 2-page render,
        # a partially-filled last page is expected — don't stretch content
        # across the page break.
        if max_pages == 1 and s_max > 1.01 and (avail - h1) > 1.0:
            _, h2 = self._render_pdf(tailored, **knobs, stretch=s_max,
                                     measure_only=True)
            if h2 <= avail:
                stretch = s_max
                predicted = h2
            elif h2 > h1:
                # Height is linear in stretch: interpolate the exact factor
                # that lands on the page bottom, then verify (rounding guard).
                stretch = 1.0 + (avail - h1) * (s_max - 1.0) / (h2 - h1)
                stretch = max(1.0, min(stretch, s_max))
                _, hv = self._render_pdf(tailored, **knobs, stretch=stretch,
                                         measure_only=True)
                if hv > avail:
                    stretch = max(1.0, stretch - 0.02)
                    _, hv = self._render_pdf(tailored, **knobs,
                                             stretch=stretch,
                                             measure_only=True)
                predicted = hv

        knobs["stretch"] = stretch
        knobs["predicted_height"] = predicted
        return knobs

    _RENDER_KNOBS = (
        "section_gap", "entry_gap", "post_header", "header_gap", "skill_gap",
        "body_size", "lh", "lh_s", "stretch", "max_pages",
    )

    def layout_plan(self, tailored: Dict[str, Any]) -> Dict[str, Any]:
        """One layout for PDF, DOCX, and the HTML preview.

        Page size, font tier, gaps, stretch, and page count are solved once.
        Emitters only draw.
        """
        knobs = self._compute_pdf_layout(tailored)
        plan = tailored.get("content_plan") or {}
        from schemas.tailoring_contracts import DEFAULT_SECTION_ORDER
        return {
            "body_size": knobs["body_size"],
            "lh": knobs["lh"],
            "lh_s": knobs["lh_s"],
            "stretch": knobs.get("stretch", 1.0),
            "section_gap": knobs["section_gap"],
            "entry_gap": knobs["entry_gap"],
            "post_header": knobs["post_header"],
            "header_gap": knobs["header_gap"],
            "skill_gap": knobs["skill_gap"],
            "max_pages": knobs.get("max_pages", 1),
            "predicted_height": knobs.get("predicted_height"),
            "page_format": tailored.get("_page_format") or getattr(self, "_page_format", "A4") or "A4",
            "section_order": list(plan.get("section_order") or DEFAULT_SECTION_ORDER),
            "credentials_first": bool(plan.get("credentials_first")),
        }

    def generate_pdf(self, tailored: Dict[str, Any]) -> bytes:
        """Generate a PDF from the shared layout plan."""
        plan = self.layout_plan(tailored)
        knobs = {k: plan[k] for k in self._RENDER_KNOBS}
        pdf_bytes, _ = self._render_pdf(tailored, **knobs)
        return pdf_bytes

    # ------------------------------------------------------------------
    # DOCX generation (python-docx) — mirrors the PDF renderer's layout
    # ------------------------------------------------------------------

    # Usable page height in pt (A4 297mm − 0.4in top/bottom ≈ 276.7mm ≈ 784pt).
    _DOCX_AVAIL_PT = 780.0

    def _estimate_docx_height_pt(
        self,
        tailored: Dict[str, Any],
        *,
        body_size: float,
        line_spacing: float,
        section_gap: float,
        entry_gap: float,
        skill_gap: float,
        header_gap: float,
    ) -> float:
        """Heuristic height estimator in points.

        python-docx can't render, so we estimate: each paragraph contributes
        (line_count * line_spacing) + paragraph_before/after spacing. Line count
        is derived by dividing text length by a characters-per-line constant
        based on body_size (smaller font ⇒ more chars/line).
        """
        # Usable width in mm * (chars per mm at given pt). Arial at 10pt
        # averages ~2.1 chars/mm in our 197mm-wide text box → ~414 chars/line.
        # Scale inversely with font size (Times/Tinos metrics).
        scale = 10.0 / max(body_size, 6.0)
        chars_per_line = 95 * scale  # rough but stable
        h = 0.0

        def para(text: str, size: float = None, bullet: bool = False,
                 spacing: float = None) -> float:
            s = size or body_size
            # Body paragraphs honor the line_spacing knob (Pass-3 fill);
            # name/contact/section headers below stay at the fixed 1.15.
            line_h = s * (line_spacing if spacing is None else spacing)
            if not text:
                return line_h + 2
            length = len(text) + (3 if bullet else 0)
            lines = max(1, int(length / chars_per_line) + (1 if length % chars_per_line else 0))
            return lines * line_h + 1.5

        # Name + contact + HR
        h += 17 * 1.15 + 2        # name
        contact = tailored.get("contact", {})
        if any(contact.get(k) for k in ("location", "phone", "email", "portfolio", "linkedin", "github")):
            h += 10.5 * 1.15 + 1  # contact line
        h += 4                     # HR line
        h += header_gap

        def section(title: str) -> float:
            return 11 * 1.15 + 2 + section_gap

        def header_block(left: str, dates: str) -> float:
            return max(11 * 1.15, body_size * 1.15) + 2

        if tailored.get("summary"):
            h += section("SUMMARY")
            h += para(tailored["summary"])

        experience = tailored.get("experience", []) or []
        if experience:
            h += section("EXPERIENCE")
            for i, exp in enumerate(experience):
                h += header_block(exp.get("company", ""), exp.get("dates", ""))
                if exp.get("title"):
                    h += 10.5 * line_spacing + 1
                for b in exp.get("bullets", []) or []:
                    h += para(b, bullet=True)
                if i < len(experience) - 1:
                    h += entry_gap

        projects = tailored.get("projects", []) or []
        if projects:
            h += section("PROJECTS")
            for i, proj in enumerate(projects):
                h += header_block(proj.get("name", ""), proj.get("dates", ""))
                for b in proj.get("bullets", []) or []:
                    h += para(b, bullet=True)
                if i < len(projects) - 1:
                    h += entry_gap

        skills = tailored.get("skills", {}) or {}
        skill_rows = [(cat, v) for cat, v in skills.items() if v]
        if skill_rows:
            h += section("TECHNICAL SKILLS")
            skill_spacing = min(line_spacing, self._DOCX_MAX_SKILL_SPACING)
            for cat, sk in skill_rows:
                items = ", ".join(sk) if isinstance(sk, list) else str(sk)
                # The bold "Category: " prefix is on the same line, so it has
                # to count toward the wrap estimate.
                h += para(f"{cat}: {items}", spacing=skill_spacing) + skill_gap

        education = tailored.get("education", []) or []
        if education:
            h += section("EDUCATION")
            for edu in education:
                inst = edu.get("institution", "")
                degree = edu.get("degree", "")
                if inst or degree:
                    h += para(f"{inst} | {degree}")

        return h

    def _render_docx(
        self,
        tailored: Dict[str, Any],
        *,
        body_size: float,
        title_size: float,
        header_size: float,
        section_gap: float,
        entry_gap: float,
        skill_gap: float,
        header_gap: float,
        line_spacing: float = 1.1,
    ) -> bytes:
        """Render a DOCX mirroring the PDF layout with the given spacing knobs."""
        from docx import Document
        from docx.shared import Pt, Inches, Mm, Emu
        from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
        from docx.oxml.ns import qn
        from docx.oxml import OxmlElement

        FONT = "Times New Roman"  # matches the Overleaf template; Tinos-identical metrics
        doc = Document()

        self._activate_page(tailored)
        page_w = getattr(self, "_layout_page_w", 210.0)
        page_h = getattr(self, "_layout_page_h", 297.0)
        # Page size mirrors the PDF (A4 or US Letter).
        for section in doc.sections:
            section.page_width = Mm(page_w)
            section.page_height = Mm(page_h)
            section.top_margin = Inches(0.4)
            section.bottom_margin = Inches(0.4)
            section.left_margin = Inches(0.6)
            section.right_margin = Inches(0.6)

        # Normal style
        style = doc.styles["Normal"]
        style.font.name = FONT
        style.font.size = Pt(body_size)
        style.paragraph_format.space_after = Pt(0)
        style.paragraph_format.space_before = Pt(0)
        style.paragraph_format.line_spacing = line_spacing

        # Usable width in EMU for right-aligned tab stops (page_width - L - R)
        usable_w_emu = section.page_width - section.left_margin - section.right_margin

        # ------------------- helpers -------------------
        def set_run_font(run, size_pt: float, bold=False, italic=False, color="000000"):
            run.font.name = FONT
            run.font.size = Pt(size_pt)
            run.bold = bold
            run.italic = italic
            # Pin the font for every script class (prevents font swap in Word)
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

        def add_bottom_border(p, size="6"):
            pPr = p._p.get_or_add_pPr()
            pBdr = OxmlElement("w:pBdr")
            bottom = OxmlElement("w:bottom")
            bottom.set(qn("w:val"), "single")
            bottom.set(qn("w:sz"), size)
            bottom.set(qn("w:space"), "0")
            bottom.set(qn("w:color"), "000000")
            pBdr.append(bottom)
            pPr.append(pBdr)

        def add_section_header(title: str):
            p = doc.add_paragraph()
            run = p.add_run(title.upper())
            set_run_font(run, header_size, bold=True)
            tight(p, before=section_gap, after=2.8)
            add_bottom_border(p, size="4")

        def add_bullet(text: str, indent_emu=None):
            p = doc.add_paragraph()
            # Manual bullet: filled circle + space, with hanging indent. Using a
            # raw paragraph (not List Bullet style) avoids Word's auto-numbering
            # XML which can drift across versions.
            r_dot = p.add_run("\u2022  ")
            set_run_font(r_dot, body_size, bold=True)
            r = p.add_run(text)
            set_run_font(r, body_size)
            # Indent bullet glyph + hang text under it.
            p.paragraph_format.left_indent = Inches(0.32)
            p.paragraph_format.first_line_indent = Inches(-0.18)
            tight(p, before=0.5, after=0.3)

        # ------------------- HEADER -------------------
        contact = tailored.get("contact", {})
        name = (contact.get("name", "") or "Resume").strip()
        name = self._title_case_name(name)
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        # Title Case name — e.g. "Harshith Siddardha Manne" (name is already
        # normalized to Title Case above via _title_case_name).
        run = p.add_run(name)
        set_run_font(run, title_size, bold=True)
        tight(p, before=0, after=1.0)

        contact_fields = []
        for key in ("location", "phone", "email", "portfolio", "linkedin", "github"):
            val = contact.get(key, "")
            if val:
                display, href = self._contact_display(key, val)
                contact_fields.append((display, href))
        if contact_fields:
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            # Plain black text — no hyperlinks (mirrors the PDF contact line).
            for i, (display, _href) in enumerate(contact_fields):
                run = p.add_run(display)
                set_run_font(run, body_size + 0.5)
                if i < len(contact_fields) - 1:
                    sep = p.add_run(" | ")
                    set_run_font(sep, body_size + 0.5)
            tight(p, before=0, after=header_gap)

        # ------------------- SUMMARY -------------------
        summary = tailored.get("summary", "")
        if summary:
            add_section_header("Summary")
            p = doc.add_paragraph()
            run = p.add_run(summary)
            set_run_font(run, body_size)
            tight(p, before=0, after=0.5)

        creds_early = bool((tailored.get("content_plan") or {}).get("credentials_first"))

        def emit_credentials_docx():
            licenses = [c for c in (tailored.get("licenses") or []) if isinstance(c, str) and c.strip()]
            if licenses:
                add_section_header("Licenses")
                p_l = doc.add_paragraph()
                r_l = p_l.add_run("  ·  ".join(licenses))
                set_run_font(r_l, body_size)
                tight(p_l, before=0, after=0)
            certs = [c for c in (tailored.get("certifications") or []) if isinstance(c, str) and c.strip()]
            if certs:
                add_section_header("Certifications")
                p_c = doc.add_paragraph()
                r_c = p_c.add_run("  ·  ".join(certs))
                set_run_font(r_c, body_size)
                tight(p_c, before=0, after=0)

        if creds_early:
            emit_credentials_docx()

        # ------------------- helper for a company/dates header line -------------------
        def header_line(left_bold: str, dates: str, bold_size: float):
            p = doc.add_paragraph()
            if dates:
                p.paragraph_format.tab_stops.add_tab_stop(
                    usable_w_emu, WD_TAB_ALIGNMENT.RIGHT)
            r1 = p.add_run(left_bold)
            set_run_font(r1, bold_size, bold=True)
            if dates:
                r2 = p.add_run(f"\t{dates}")
                set_run_font(r2, body_size)
            tight(p, before=0.6, after=0.0)
            return p

        # ------------------- EXPERIENCE -------------------
        experience = tailored.get("experience", []) or []
        if experience:
            add_section_header("Experience")
            for i, exp in enumerate(experience):
                company = exp.get("company", "")
                location = exp.get("location", "")
                dates = exp.get("dates", "")
                company_line = f"{company}, {location}" if location else company
                header_line(company_line, dates, bold_size=body_size + 1)

                title = exp.get("title", "")
                emp_type = (exp.get("type") or "").strip()
                if title and emp_type:
                    title = f"{title} ({emp_type})"
                if title:
                    p2 = doc.add_paragraph()
                    r = p2.add_run(title)
                    set_run_font(r, body_size + 0.5, italic=True)
                    tight(p2, before=0, after=0.5)

                for b in exp.get("bullets", []) or []:
                    add_bullet(b)
                if i < len(experience) - 1:
                    # Gap between experience entries via an empty spacer para
                    spacer = doc.add_paragraph()
                    tight(spacer, before=entry_gap, after=0)

        # ------------------- PROJECTS -------------------
        projects = tailored.get("projects", []) or []
        if projects:
            add_section_header("Projects")
            for i, proj in enumerate(projects):
                name = proj.get("name", "")
                dates = proj.get("dates", "")
                header_line(name, dates, bold_size=body_size + 1)

                tech = proj.get("tech", "")
                if tech:
                    p2 = doc.add_paragraph()
                    r = p2.add_run(tech)
                    set_run_font(r, body_size, italic=True, color="555555")
                    tight(p2, before=0, after=0.5)

                for b in proj.get("bullets", []) or []:
                    add_bullet(b)
                if i < len(projects) - 1:
                    spacer = doc.add_paragraph()
                    tight(spacer, before=entry_gap, after=0)

        # ------------------- TECHNICAL SKILLS -------------------
        skills = tailored.get("skills", {}) or {}
        if any(v for v in skills.values()):
            add_section_header(self._skills_title(tailored))
            for category, skill_list in skills.items():
                if not skill_list:
                    continue
                p = doc.add_paragraph()
                r_cat = p.add_run(f"{category}: ")
                set_run_font(r_cat, body_size, bold=True)
                sk = ", ".join(skill_list) if isinstance(skill_list, list) else str(skill_list)
                r_sk = p.add_run(sk)
                set_run_font(r_sk, body_size)
                tight(p, before=skill_gap, after=0,
                      spacing=min(line_spacing, self._DOCX_MAX_SKILL_SPACING))

        # ------------------- EDUCATION -------------------
        education = tailored.get("education", []) or []
        if education:
            seen_edu = set()
            deduped_education = []
            for edu in education:
                inst = (edu.get("institution", "") or "").strip()
                degree = (edu.get("degree", "") or "").strip()
                if not inst and not degree:
                    continue
                key = (inst.lower(), degree.lower())
                if key in seen_edu:
                    continue
                seen_edu.add(key)
                deduped_education.append(edu)

            if deduped_education:
                add_section_header("Education")
                for edu in deduped_education:
                    inst = edu.get("institution", "") or ""
                    location = edu.get("location", "") or ""
                    degree = edu.get("degree", "") or ""
                    dates = edu.get("dates", "") or ""
                    gpa = edu.get("gpa", "") or ""

                    if degree and re.search(
                        r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4}\b',
                        degree, re.IGNORECASE,
                    ):
                        cleaned = re.sub(
                            r'\s*\|?\s*\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*'
                            r'\s+\d{4}\b',
                            '', degree, flags=re.IGNORECASE,
                        ).strip().rstrip('|').strip()
                        if cleaned:
                            degree = cleaned

                    if inst and location and location.lower() in inst.lower():
                        inst_part = inst
                    elif inst and location:
                        inst_part = f"{inst}, {location}"
                    else:
                        inst_part = inst
                    parts = [x for x in [inst_part, degree] if x]
                    if gpa:
                        parts.append(f"GPA: {gpa}")
                    left_text = " | ".join(parts)

                    p = doc.add_paragraph()
                    if dates:
                        p.paragraph_format.tab_stops.add_tab_stop(
                            usable_w_emu, WD_TAB_ALIGNMENT.RIGHT)
                    r = p.add_run(left_text)
                    set_run_font(r, body_size)
                    if dates:
                        r2 = p.add_run(f"\t{dates}")
                        set_run_font(r2, body_size)
                    tight(p, before=1.0, after=0)
                    coursework = (edu.get("coursework") or "").strip()
                    if coursework:
                        p_c = doc.add_paragraph()
                        r_c = p_c.add_run(coursework)
                        set_run_font(r_c, body_size, italic=True)
                        tight(p_c, before=0, after=0)

        if not creds_early:
            emit_credentials_docx()

        extra_titles = (
            ("languages", "Languages"),
            ("awards", "Awards"),
            ("publications", "Publications"),
            ("patents", "Patents"),
            ("leadership", "Leadership"),
            ("volunteering", "Volunteering"),
            ("associations", "Associations"),
            ("clearance", "Clearance"),
        )
        for key, title in extra_titles:
            items = [c for c in (tailored.get(key) or []) if isinstance(c, str) and c.strip()]
            if items:
                add_section_header(title)
                p_x = doc.add_paragraph()
                r_x = p_x.add_run("  ·  ".join(items))
                set_run_font(r_x, body_size)
                tight(p_x, before=0, after=0)

        buf = io.BytesIO()
        doc.save(buf)
        return buf.getvalue()

    def generate_docx(self, tailored: Dict[str, Any]) -> bytes:
        """Generate a DOCX from the same LayoutPlan the PDF uses.

        Gaps are solved in millimetres by the PDF solver and converted to
        points so Word does not run a second shrink ladder.
        """
        plan = self.layout_plan(tailored)
        mm_to_pt = 2.83465
        body = float(plan["body_size"])
        title_size = 20.0 if body >= 10.8 else (19.5 if body >= 10.3 else 19.0)
        header_size = 12.0 if body >= 10.8 else (11.5 if body >= 10.3 else 11.0)
        return self._render_docx(
            tailored,
            body_size=body,
            title_size=title_size,
            header_size=header_size,
            section_gap=float(plan["section_gap"]) * mm_to_pt,
            entry_gap=float(plan["entry_gap"]) * mm_to_pt,
            skill_gap=float(plan["skill_gap"]) * mm_to_pt,
            header_gap=float(plan["header_gap"]) * mm_to_pt,
            line_spacing=max(1.1, min(1.40, 1.15 * float(plan.get("stretch") or 1.0))),
        )

    # ------------------------------------------------------------------
    # Filename helper
    # ------------------------------------------------------------------

    @staticmethod
    def build_filename(tailored: Dict[str, Any], jd_analysis: Dict[str, Any], ext: str) -> str:
        """Build ATS-friendly filename: Firstname_Lastname_JobTitle.ext"""
        contact = tailored.get("contact", {})
        name = contact.get("name", "").strip()
        name = ResumeRenderer._title_case_name(name)
        parts = name.split()
        first = parts[0] if parts else "Resume"
        last = parts[-1] if len(parts) > 1 else ""

        job_title = jd_analysis.get("job_title", "")

        def clean(s: str) -> str:
            s = re.sub(r"[^a-zA-Z0-9 ]+", " ", s).strip()
            s = re.sub(r"\s+", "_", s).strip("_")
            return s

        segments = [clean(first), clean(last), clean(job_title)]
        segments = [s for s in segments if s]
        return "_".join(segments) + f".{ext}"

    @staticmethod
    def build_cover_letter_filename(
        candidate_name: str, job_title: str, company: str, ext: str = "pdf",
    ) -> str:
        """Build ATS-friendly cover letter filename:
            Firstname_Lastname_JobTitle_Cover_Letter.ext

        Mirrors the resume filename convention so recruiters see paired
        documents in a single sorted view. Company is intentionally NOT
        included — many ATS portals reject filenames containing punctuation
        from real company names (e.g. "Two Sigma, LLC"), and the job_title
        already disambiguates the role.
        """
        normalized = ResumeRenderer._title_case_name((candidate_name or "").strip())
        parts = normalized.split()
        first = parts[0] if parts else "Applicant"
        last = parts[-1] if len(parts) > 1 else ""

        def clean(s: str) -> str:
            s = re.sub(r"[^a-zA-Z0-9 ]+", " ", s).strip()
            s = re.sub(r"\s+", "_", s).strip("_")
            return s

        segments = [clean(first), clean(last), clean(job_title), "Cover_Letter"]
        segments = [s for s in segments if s]
        return "_".join(segments) + f".{ext}"
