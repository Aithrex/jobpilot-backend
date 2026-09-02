#!/usr/bin/env python3
"""Import the official O*NET title and software-skill datasets, versioned.

Why an IMPORT and not a hand-written list
------------------------------------------
`tech_taxonomy` recognises the titles and skills somebody thought to write
down. O*NET publishes what the US Department of Labor's own occupational
survey recorded — 1,480 alternate titles and 5,848 software/technology skills
across the 33 SOC codes this taxonomy maps to. Importing it means the
vocabulary is sourced rather than remembered, and the release it came from is
recorded so a change to the numbers can be attributed.

What it deliberately does NOT do
--------------------------------
Replace the taxonomy. O*NET is a 2019 SOC crosswalk refreshed on a survey
cycle: it has no "GenAI Engineer", no "MLOps", no "Platform Engineer", and its
software list contains "3M Post-it App" alongside Kubernetes. So it is one
INPUT, filtered to the occupations we classify, and it is paired with
`services/onet_overlay.py` — a hand-maintained overlay of the modern tools and
role aliases the survey has not caught up with. The overlay is small and
explicitly dated; the import is large and explicitly sourced. Neither pretends
to be the other.

Usage
-----
    python scripts/import_onet.py --release 30_0            # fetch + generate
    python scripts/import_onet.py --release 30_0 \
        --alt-file /tmp/alt.txt --tech-file /tmp/tech.txt   # offline

Writes `shared/python/services/onet_data.py` (GENERATED — do not hand-edit).
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))

BASE = "https://www.onetcenter.org/dl_files/database/db_{release}_text/{name}"
ALT_FILE = "Alternate Titles.txt"
TECH_FILE = "Technology Skills.txt"

#: SOC codes outside the taxonomy's own crosswalk that still describe the
#: occupations we classify. Kept explicit so nobody has to guess why a code is
#: here: each is a computer, computer-hardware or engineering-management
#: occupation that our specialties map onto in practice.
EXTRA_SOCS: Tuple[str, ...] = (
    "15-1211.00",  # Computer Systems Analysts
    "15-1211.01",  # Health Informatics Specialists
    "15-1221.00",  # Computer and Information Research Scientists
    "15-1242.00",  # Database Administrators
    "15-1243.00",  # Database Architects
    "15-1244.00",  # Network and Computer Systems Administrators
    "15-1252.00",  # Software Developers
    "15-1253.00",  # Software QA Analysts and Testers
    "15-1254.00",  # Web Developers
    "15-1255.00",  # Web and Digital Interface Designers
    "15-1299.08",  # Computer Systems Engineers/Architects
    "15-1299.09",  # Information Technology Project Managers
    "15-2051.00",  # Data Scientists
    "17-2061.00",  # Computer Hardware Engineers
    "17-2071.00",  # Electrical Engineers
    "17-2072.00",  # Electronics Engineers, Except Computer
    "11-3021.00",  # Computer and Information Systems Managers
)

#: Vendor products that O*NET lists as "software" and that name no
#: engineering skill. Excluding them is a judgement, so it is written down.
NOISE_PREFIXES: Tuple[str, ...] = (
    "3M ", "Adobe Acrobat", "Microsoft Office", "Microsoft Outlook",
    "Microsoft PowerPoint", "Microsoft Word", "Microsoft Excel",
    "Google Docs", "Google Drive", "Dropbox", "Slack ", "Zoom ",
    "Apple Safari", "Google Chrome", "Mozilla Firefox", "Microsoft Edge",
    "Email software", "Web browser software", "Spreadsheet software",
    "Word processing software", "Presentation software", "Calendar and",
)


def _fetch(url: str) -> str:
    import requests
    resp = requests.get(url, timeout=120,
                        headers={"User-Agent": "JobPilotIngest/1.0"})
    resp.raise_for_status()
    return resp.text


def _read(text: str) -> Iterable[Dict[str, str]]:
    return csv.DictReader(io.StringIO(text), delimiter="\t")


def target_socs() -> Set[str]:
    from services import tech_taxonomy as tx
    socs = {c for codes in tx.SPECIALTY_OCCUPATION_CODES.values() for c in codes}
    return socs | set(EXTRA_SOCS)


def _is_noise(name: str) -> bool:
    return any(name.startswith(p) for p in NOISE_PREFIXES)


def build(alt_text: str, tech_text: str, *, release: str) -> str:
    socs = target_socs()
    alt: Dict[str, Set[str]] = defaultdict(set)
    tech: Dict[str, Set[str]] = defaultdict(set)
    hot: Set[str] = set()

    for row in _read(alt_text):
        soc = (row.get("O*NET-SOC Code") or "").strip()
        title = (row.get("Alternate Title") or "").strip()
        if soc in socs and title:
            alt[soc].add(title)
    for row in _read(tech_text):
        soc = (row.get("O*NET-SOC Code") or "").strip()
        name = (row.get("Example") or "").strip()
        if not name or _is_noise(name):
            continue
        if soc in socs:
            tech[soc].add(name)
        if (row.get("Hot Technology") or "").strip().upper() == "Y":
            hot.add(name)

    lines: List[str] = []
    a = lines.append
    a('"""O*NET alternate titles and technology skills for the occupations we classify.')
    a("")
    a("GENERATED FILE - do not edit by hand. Regenerate with:")
    a("")
    a("    python scripts/import_onet.py --release " + release)
    a("")
    a("Source: O*NET(R) Database, US Department of Labor, Employment and Training")
    a("Administration. Used under the CC BY 4.0 licence O*NET publishes it under.")
    a("O*NET(R) is a trademark of USDOL/ETA.")
    a("")
    a("Filtered to the SOC codes `tech_taxonomy.SPECIALTY_OCCUPATION_CODES` maps")
    a("to, plus `scripts/import_onet.EXTRA_SOCS`. Office and browser products are")
    a("dropped (`NOISE_PREFIXES`) because they name no engineering skill.")
    a("")
    a("This is ONE INPUT, not the taxonomy. O*NET is a 2019 SOC crosswalk on a")
    a("survey cycle: it has no GenAI, MLOps or Platform Engineering titles. See")
    a("`services/onet_overlay.py` for the modern tools and aliases it lacks.")
    a('"""')
    a("from __future__ import annotations")
    a("")
    a("from typing import Dict, FrozenSet, Tuple")
    a("")
    a(f'ONET_RELEASE = "{release.replace("_", ".")}"')
    a(f'ONET_IMPORTED_AT = "{datetime.now(timezone.utc).date().isoformat()}"')
    a(f'ONET_VERSION = "onet-{release.replace("_", ".")}"')
    a("")
    a(f"#: {sum(len(v) for v in alt.values()):,} alternate titles across "
      f"{len(alt)} SOC codes.")
    a("ALTERNATE_TITLES: Dict[str, Tuple[str, ...]] = {")
    for soc in sorted(alt):
        a(f"    {soc!r}: (")
        for title in sorted(alt[soc]):
            a(f"        {title!r},")
        a("    ),")
    a("}")
    a("")
    a(f"#: {sum(len(v) for v in tech.values()):,} technology skills across "
      f"{len(tech)} SOC codes.")
    a("TECHNOLOGY_SKILLS: Dict[str, Tuple[str, ...]] = {")
    for soc in sorted(tech):
        a(f"    {soc!r}: (")
        for name in sorted(tech[soc]):
            a(f"        {name!r},")
        a("    ),")
    a("}")
    a("")
    a("#: O*NET's own 'Hot Technology' flag - what employers were observed")
    a("#: asking for most often. Kept across ALL occupations, not only ours,")
    a("#: because a hot technology named in a non-tech occupation is still a")
    a("#: real technology.")
    a("HOT_TECHNOLOGIES: FrozenSet[str] = frozenset({")
    for name in sorted(hot):
        a(f"    {name!r},")
    a("})")
    a("")
    a("ALL_TITLES: FrozenSet[str] = frozenset(")
    a("    t for titles in ALTERNATE_TITLES.values() for t in titles)")
    a("ALL_SKILLS: FrozenSet[str] = frozenset(")
    a("    s for skills in TECHNOLOGY_SKILLS.values() for s in skills)")
    a("")
    a("")
    a("def titles_for(soc: str) -> Tuple[str, ...]:")
    a('    """Alternate titles O*NET records for one SOC code."""')
    a("    return ALTERNATE_TITLES.get(str(soc or ''), ())")
    a("")
    a("")
    a("def skills_for(soc: str) -> Tuple[str, ...]:")
    a('    """Technology skills O*NET records for one SOC code."""')
    a("    return TECHNOLOGY_SKILLS.get(str(soc or ''), ())")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", default="30_0")
    ap.add_argument("--alt-file", default=None)
    ap.add_argument("--tech-file", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    if args.alt_file:
        alt_text = Path(args.alt_file).read_text(encoding="utf-8", errors="ignore")
    else:
        alt_text = _fetch(BASE.format(release=args.release,
                                      name=ALT_FILE.replace(" ", "%20")))
    if args.tech_file:
        tech_text = Path(args.tech_file).read_text(encoding="utf-8", errors="ignore")
    else:
        tech_text = _fetch(BASE.format(release=args.release,
                                       name=TECH_FILE.replace(" ", "%20")))

    body = build(alt_text, tech_text, release=args.release)
    out = Path(args.out) if args.out else (
        ROOT / "shared" / "python" / "services" / "onet_data.py")
    out.write_text(body, encoding="utf-8")
    print(f"wrote {out} ({len(body):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
