"""Render a real resume to PDF and dump the layout solver's decisions.

Spacing bugs on sparse resumes are invisible in unit fixtures — they only show
up on a specific person's content. This renders the BASE (parsed upload) and a
TAILORED record for one user, writes both PDFs, and prints the knobs
``_compute_pdf_layout`` chose for each so a before/after comparison is numeric
as well as visual.

Read-only: nothing is written back to Mongo.

Usage:
    python scripts/render_resume_probe.py --email me@example.com
    python scripts/render_resume_probe.py --record-id <uuid>
    python scripts/render_resume_probe.py --email me@example.com --out build/probe
"""
import argparse
import json
import sys
from pathlib import Path

# Mirror local.py path wiring so `utils.*` / `services.*` resolve from the
# shared layer (kept identical to scripts/backfill_applied_date.py).
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass


def _fmt_knobs(renderer, label: str, knobs: dict) -> str:
    """One-line-per-knob dump plus the derived page fill."""
    avail = renderer._AVAIL_H
    predicted = knobs.get("predicted_height", 0.0)
    lines = [
        f"--- {label} ---",
        f"  body_size      {knobs['body_size']:.2f} pt",
        f"  lh / lh_s      {knobs['lh']:.3f} / {knobs['lh_s']:.3f} mm",
        f"  stretch        {knobs['stretch']:.3f}",
        f"  section_gap    {knobs['section_gap']:.3f} mm"
        f"  (min {renderer._MIN_SECTION_GAP} / max {renderer._MAX_SECTION_GAP})",
        f"  entry_gap      {knobs['entry_gap']:.3f} mm"
        f"  (min {renderer._MIN_ENTRY_GAP} / max {renderer._MAX_ENTRY_GAP})",
        f"  post_header    {knobs['post_header']:.3f} mm"
        f"  (min {renderer._MIN_POST_HEADER} / max {renderer._MAX_POST_HEADER})",
        f"  header_gap     {knobs['header_gap']:.3f} mm"
        f"  (min {renderer._MIN_HEADER_GAP} / max {renderer._MAX_HEADER_GAP})",
        f"  skill_gap      {knobs['skill_gap']:.3f} mm"
        f"  (min {renderer._MIN_SKILL_GAP} / max {renderer._MAX_SKILL_GAP})",
        f"  predicted      {predicted:.1f} / {avail:.1f} mm"
        f"  = {100 * predicted / avail:.1f}% fill",
    ]
    return "\n".join(lines)


def _dump_skills(label: str, resume: dict) -> str:
    skills = resume.get("skills") or {}
    out = [f"--- {label} skills ({len(skills)} categories) ---"]
    for cat, items in skills.items():
        joined = ", ".join(items) if isinstance(items, list) else str(items)
        out.append(f"  {cat}: {joined}")
    return "\n".join(out)


def _render(renderer, resume: dict, label: str, out_dir: Path) -> None:
    _working, resolution = renderer._compute_pdf_layout(resume)
    knobs = {
        "body_size": resolution.tier.body_pt,
        "lh": resolution.tier.lh,
        "lh_s": resolution.tier.lh_s,
        "stretch": resolution.stretch,
        "section_gap": resolution.gaps.section_gap,
        "entry_gap": resolution.gaps.entry_gap,
        "post_header": resolution.gaps.post_header,
        "header_gap": resolution.gaps.header_gap,
        "skill_gap": resolution.gaps.skill_gap,
        "predicted_height": resolution.predicted_height_mm,
    }
    print(_fmt_knobs(renderer, label, knobs))
    print(_dump_skills(label, resume))

    slug = label.lower().replace(" ", "_")
    pdf_path = out_dir / f"{slug}.pdf"
    pdf_bytes, _res = renderer.generate_pdf(resume)
    pdf_path.write_bytes(pdf_bytes)
    json_path = out_dir / f"{slug}.json"
    json_path.write_text(json.dumps(resume, indent=2, default=str), encoding="utf-8")
    print(f"  wrote {pdf_path}")
    print(f"  wrote {json_path}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--email", default="", help="render this user's base resume")
    ap.add_argument(
        "--record-id",
        default="",
        help="render this tailoring record (defaults to the user's most recent)",
    )
    ap.add_argument("--out", default="build/render-probe", help="output directory")
    args = ap.parse_args()

    if not args.email and not args.record_id:
        ap.error("pass --email and/or --record-id")

    from services.resume_renderer import ResumeRenderer
    from utils.db_connect import DBConnect

    db = DBConnect().get_db()
    renderer = ResumeRenderer()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.email:
        from services.resume_parser import ResumeParser

        parsed = ResumeParser(db).get_structured_resume(user_email=args.email)
        structured = (parsed or {}).get("structured")
        if structured:
            _render(renderer, structured, "base", out_dir)
        else:
            print(f"no parsed base resume for {args.email}\n")

    query = {"record_id": args.record_id} if args.record_id else {"user_email": args.email}
    record = db.tailoring_records.find_one(
        query, {"_id": 0, "record_id": 1, "tailored_resume": 1, "jd_analysis": 1},
        sort=[("created_at", -1)],
    )
    if not record or not record.get("tailored_resume"):
        print(f"no tailoring record with a tailored_resume for {query}")
        return 1

    jd = record.get("jd_analysis") or {}
    print(
        f"tailoring record {record.get('record_id')} — "
        f"{jd.get('job_title') or '?'} at {jd.get('company') or '?'}"
    )
    _render(renderer, record["tailored_resume"], "tailored", out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
