"""One-shot before/after Education orphan diagnostic (not a unit test)."""
from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader

from services.resume_renderer import ResumeRenderer
from tests.test_pagination_education_orphan import _long_resume_almost_full, _page_texts


def _summarize(label: str, pages: list[str]) -> None:
    print(f"{label}: pages={len(pages)}")
    for i, t in enumerate(pages):
        edu = "EDUCATION" in t.upper()
        asu = "Arizona State" in t
        orphan = edu and not asu and "Master of Science" not in t
        print(
            f"  page {i + 1}: chars={len(t)} education={edu} "
            f"asu={asu} orphan_heading={orphan}"
        )


def main() -> None:
    before_path = Path(
        r"C:\Users\kommu\Downloads\Surya_Kommuguri_Software_Engineer_II (2).pdf"
    )
    if before_path.exists():
        reader = PdfReader(str(before_path))
        before_pages = [(p.extract_text() or "") for p in reader.pages]
        _summarize("BEFORE (reported PDF)", before_pages)
    else:
        print("BEFORE: reported PDF not found")

    resume = _long_resume_almost_full(push_edu=True)
    pdf, res = ResumeRenderer().generate_pdf(resume)
    pages = _page_texts(pdf)
    out = Path("docs/plans/education-orphan-after.pdf")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(pdf)
    _summarize("AFTER (fixture re-render)", pages)
    print(
        f"outcome={res.outcome} body_pt={res.tier.body_pt} "
        f"warnings={res.layout_warnings} wrote={out}"
    )


if __name__ == "__main__":
    main()
