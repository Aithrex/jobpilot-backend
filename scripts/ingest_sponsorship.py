"""Sponsorship-intel ingest — build the ``sponsorship_signals`` collection.

  *** HUMAN-RUN SCRIPT — REQUIRES OUT-OF-BAND DATA FILES. NOT run by Lambda. ***

This aggregates two public federal datasets, per employer over the last N fiscal
years, and UPSERTS one row per employer into ``sponsorship_signals`` for the
read-only ``services.sponsorship_service`` to score. It downloads NOTHING: the
gov files are multi-hundred-MB and must be fetched by a human first, then passed
by path.

Data sources (download manually, then pass with --lca / --uscis):
  1. DOL OFLC LCA Disclosure Data (H-1B/H-1B1/E-3), quarterly XLSX/CSV:
       https://www.dol.gov/agencies/eta/foreign-labor/performance
     -> lca_filings_5y, median_wage, top_titles, last_filed_at
     Columns vary by year; we resolve them flexibly (EMPLOYER_NAME /
     LCA_CASE_EMPLOYER_NAME, CASE_STATUS / STATUS, JOB_TITLE / SOC_TITLE,
     WAGE_RATE_OF_PAY_FROM / LCA_CASE_WAGE_RATE_FROM, DECISION_DATE / CASE_SUBMITTED).
  2. USCIS H-1B Employer Data Hub, per-FY CSV:
       https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub
     -> approvals_5y, denials_5y  (Initial + Continuing Approval/Denial),
        keyed on "Employer (Petitioner) Name" and "Fiscal Year".

The two feeds are joined per employer by ``normalize_employer`` (shared with the
runtime service, so matching is identical on both sides).

Defensive by design: unknown encodings (utf-8 -> utf-8-sig -> cp1252 -> latin-1),
missing/renamed columns (resolved by candidate lists; a missing optional column
is skipped, a missing REQUIRED column aborts that file with a clear message),
blank/garbage numeric + date cells, and huge files (rows are streamed, never
loaded whole; per-employer wage samples are capped for a bounded-memory median).

Usage (run from the repo root, needs a LIVE MongoDB connection):
    python scripts/ingest_sponsorship.py --lca LCA_Disclosure_FY2024.xlsx \
        --uscis h1b_datahub_export_fy2024.csv --uscis h1b_datahub_export_fy2023.csv
    python scripts/ingest_sponsorship.py --uscis <csv> --years 5 --dry-run
    python scripts/ingest_sponsorship.py --lca <xlsx> --limit 5000   # quick test

At least one of --lca / --uscis is required. --uscis may be repeated (one CSV per
fiscal year). --dry-run aggregates + prints a sample but writes nothing.
"""
import argparse
import csv
import logging
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Mirror migrate_indexes.py path wiring so `utils.*` / `services.*` resolve from
# the shared layer (PEP 420 namespace packages; no __init__.py).
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from services.sponsorship_service import normalize_employer  # noqa: E402
from utils.db_connect import DBConnect  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("ingest_sponsorship")

# Cap wage samples kept per employer so a national file can't blow up memory;
# the median of a few thousand samples is an honest estimate.
_MAX_WAGE_SAMPLES = 4096
_TOP_TITLES = 5
_UPSERT_BATCH = 1000

# --- Flexible column resolution (year-to-year the gov schemas drift) ---------
_LCA_EMPLOYER_COLS = ("EMPLOYER_NAME", "LCA_CASE_EMPLOYER_NAME", "EMPLOYER_BUSINESS_DBA")
_LCA_STATUS_COLS = ("CASE_STATUS", "STATUS", "LCA_CASE_STATUS")
_LCA_TITLE_COLS = ("JOB_TITLE", "SOC_TITLE", "LCA_CASE_JOB_TITLE", "PW_SOC_TITLE")
_LCA_WAGE_COLS = ("WAGE_RATE_OF_PAY_FROM", "LCA_CASE_WAGE_RATE_FROM",
                  "WAGE_RATE_OF_PAY_FROM_1", "PREVAILING_WAGE")
_LCA_WAGE_UNIT_COLS = ("WAGE_UNIT_OF_PAY", "LCA_CASE_WAGE_RATE_UNIT",
                       "PW_UNIT_OF_PAY", "WAGE_UNIT_OF_PAY_1")
_LCA_DATE_COLS = ("DECISION_DATE", "CASE_SUBMITTED", "RECEIVED_DATE",
                  "LCA_CASE_SUBMIT")

_USCIS_EMPLOYER_COLS = ("Employer (Petitioner) Name", "Employer", "Petitioner",
                        "EMPLOYER_PETITIONER_NAME")
_USCIS_FY_COLS = ("Fiscal Year", "FiscalYear", "FISCAL_YEAR")
_USCIS_APPROVAL_COLS = ("Initial Approval", "Initial Approvals",
                        "Continuing Approval", "Continuing Approvals")
_USCIS_DENIAL_COLS = ("Initial Denial", "Initial Denials",
                      "Continuing Denial", "Continuing Denials")

# Annualize DOL wages to a comparable yearly figure for the median.
_WAGE_ANNUAL_FACTOR = {
    "year": 1.0, "yr": 1.0, "annual": 1.0, "annually": 1.0,
    "month": 12.0, "mth": 12.0, "monthly": 12.0,
    "bi-weekly": 26.0, "biweekly": 26.0,
    "week": 52.0, "weekly": 52.0,
    "hour": 2080.0, "hr": 2080.0, "hourly": 2080.0,
}


# ---------------------------------------------------------------------------
# Defensive IO helpers.
# ---------------------------------------------------------------------------
def _open_text(path: Path):
    """Open a text file, trying a sequence of common government-file encodings."""
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            fh = open(path, "r", encoding=enc, newline="")
            fh.read(4096)
            fh.seek(0)
            return fh, enc
        except (UnicodeDecodeError, UnicodeError):
            continue
    # Last resort: latin-1 decodes any byte.
    return open(path, "r", encoding="latin-1", newline=""), "latin-1"


def _iter_rows(path: Path):
    """Yield dict rows from a CSV or XLSX file, streaming (bounded memory).

    XLSX requires openpyxl; if it's missing we raise a clear, actionable error.
    """
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        try:
            from openpyxl import load_workbook
        except ImportError:
            raise SystemExit(
                f"'{path.name}' is XLSX but openpyxl is not installed. Either "
                f"`pip install openpyxl` or convert the file to CSV and pass that."
            )
        wb = load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        rows = ws.iter_rows(values_only=True)
        try:
            header = [str(c).strip() if c is not None else "" for c in next(rows)]
        except StopIteration:
            wb.close()
            return
        for raw in rows:
            yield {header[i]: raw[i] for i in range(min(len(header), len(raw)))}
        wb.close()
        return

    # CSV / TSV / txt.
    fh, enc = _open_text(path)
    logger.info("Reading %s as CSV (encoding=%s)", path.name, enc)
    try:
        reader = csv.DictReader(fh)
        for row in reader:
            yield row
    finally:
        fh.close()


def _resolve(header_keys, candidates):
    """Return the first candidate present in the header (case-insensitive), else None."""
    lower = {str(k).strip().lower(): k for k in header_keys if k is not None}
    for cand in candidates:
        hit = lower.get(cand.lower())
        if hit is not None:
            return hit
    return None


def _num(value):
    if value is None:
        return None
    try:
        cleaned = str(value).replace(",", "").replace("$", "").strip()
        return float(cleaned) if cleaned else None
    except (TypeError, ValueError):
        return None


def _parse_date(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d %H:%M:%S",
                "%m-%d-%Y", "%d-%b-%y", "%d-%b-%Y"):
        try:
            return datetime.strptime(text[:19], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    # ISO fallback.
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _median(samples):
    if not samples:
        return None
    ordered = sorted(samples)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return round(ordered[mid], 2)
    return round((ordered[mid - 1] + ordered[mid]) / 2.0, 2)


# ---------------------------------------------------------------------------
# Per-employer accumulator.
# ---------------------------------------------------------------------------
class _Agg:
    __slots__ = ("display", "lca_filings", "approvals", "denials",
                 "wages", "titles", "last_filed_at")

    def __init__(self):
        self.display = None
        self.lca_filings = 0
        self.approvals = 0
        self.denials = 0
        self.wages = []
        self.titles = Counter()
        self.last_filed_at = None

    def note_display(self, raw_name):
        # Keep the first non-empty human-readable form we see.
        if not self.display and raw_name:
            self.display = str(raw_name).strip()

    def note_date(self, dt):
        if dt and (self.last_filed_at is None or dt > self.last_filed_at):
            self.last_filed_at = dt


# ---------------------------------------------------------------------------
# LCA (DOL) ingestion.
# ---------------------------------------------------------------------------
def _ingest_lca(path: Path, agg, min_year: int, limit: int) -> int:
    rows = _iter_rows(path)
    try:
        first = next(rows)
    except StopIteration:
        logger.warning("LCA file %s is empty", path.name)
        return 0
    keys = list(first.keys())
    emp_col = _resolve(keys, _LCA_EMPLOYER_COLS)
    if not emp_col:
        raise SystemExit(
            f"LCA file '{path.name}' has no recognizable employer column "
            f"(looked for {_LCA_EMPLOYER_COLS}). Columns present: {keys[:12]}..."
        )
    status_col = _resolve(keys, _LCA_STATUS_COLS)
    title_col = _resolve(keys, _LCA_TITLE_COLS)
    wage_col = _resolve(keys, _LCA_WAGE_COLS)
    unit_col = _resolve(keys, _LCA_WAGE_UNIT_COLS)
    date_col = _resolve(keys, _LCA_DATE_COLS)
    logger.info("LCA columns -> employer=%s status=%s title=%s wage=%s date=%s",
                emp_col, status_col, title_col, wage_col, date_col)

    processed = 0

    def _handle(row):
        nonlocal processed
        raw_name = row.get(emp_col)
        norm = normalize_employer(raw_name)
        if not norm:
            return
        # Only count certified/approved filings when a status column exists.
        if status_col:
            status = str(row.get(status_col) or "").strip().upper()
            if status and status not in ("CERTIFIED", "CERTIFIED-WITHDRAWN",
                                         "CERTIFIED - WITHDRAWN"):
                return
        dt = _parse_date(row.get(date_col)) if date_col else None
        if dt and dt.year < min_year:
            return
        a = agg[norm]
        a.note_display(raw_name)
        a.lca_filings += 1
        a.note_date(dt)
        if title_col:
            title = str(row.get(title_col) or "").strip()
            if title:
                a.titles[title[:120]] += 1
        if wage_col and len(a.wages) < _MAX_WAGE_SAMPLES:
            wage = _num(row.get(wage_col))
            if wage and wage > 0:
                factor = 1.0
                if unit_col:
                    unit = str(row.get(unit_col) or "").strip().lower()
                    factor = _WAGE_ANNUAL_FACTOR.get(unit, 1.0)
                annual = wage * factor
                # Guard against obviously broken rows (e.g. hourly not flagged).
                if 5_000 <= annual <= 2_000_000:
                    a.wages.append(annual)
        processed += 1

    _handle(first)
    for row in rows:
        _handle(row)
        if limit and processed >= limit:
            logger.info("LCA --limit %d reached", limit)
            break
    logger.info("LCA: processed %d qualifying filing rows from %s", processed, path.name)
    return processed


# ---------------------------------------------------------------------------
# USCIS Employer Data Hub ingestion.
# ---------------------------------------------------------------------------
def _ingest_uscis(path: Path, agg, min_year: int, limit: int) -> int:
    rows = _iter_rows(path)
    try:
        first = next(rows)
    except StopIteration:
        logger.warning("USCIS file %s is empty", path.name)
        return 0
    keys = list(first.keys())
    emp_col = _resolve(keys, _USCIS_EMPLOYER_COLS)
    if not emp_col:
        raise SystemExit(
            f"USCIS file '{path.name}' has no recognizable employer column "
            f"(looked for {_USCIS_EMPLOYER_COLS}). Columns present: {keys[:12]}..."
        )
    fy_col = _resolve(keys, _USCIS_FY_COLS)
    # Approval/denial columns: resolve every present variant and sum them.
    approval_cols = [_resolve(keys, (c,)) for c in _USCIS_APPROVAL_COLS]
    denial_cols = [_resolve(keys, (c,)) for c in _USCIS_DENIAL_COLS]
    approval_cols = [c for c in approval_cols if c]
    denial_cols = [c for c in denial_cols if c]
    if not approval_cols and not denial_cols:
        raise SystemExit(
            f"USCIS file '{path.name}' has no approval/denial columns "
            f"(looked for {_USCIS_APPROVAL_COLS + _USCIS_DENIAL_COLS})."
        )
    logger.info("USCIS columns -> employer=%s fy=%s approvals=%s denials=%s",
                emp_col, fy_col, approval_cols, denial_cols)

    processed = 0

    def _handle(row):
        nonlocal processed
        raw_name = row.get(emp_col)
        norm = normalize_employer(raw_name)
        if not norm:
            return
        if fy_col:
            fy = _num(row.get(fy_col))
            if fy is not None and int(fy) < min_year:
                return
        a = agg[norm]
        a.note_display(raw_name)
        for c in approval_cols:
            a.approvals += int(_num(row.get(c)) or 0)
        for c in denial_cols:
            a.denials += int(_num(row.get(c)) or 0)
        processed += 1

    _handle(first)
    for row in rows:
        _handle(row)
        if limit and processed >= limit:
            logger.info("USCIS --limit %d reached", limit)
            break
    logger.info("USCIS: processed %d employer-FY rows from %s", processed, path.name)
    return processed


# ---------------------------------------------------------------------------
# Upsert.
# ---------------------------------------------------------------------------
def _upsert(agg, dry_run: bool) -> int:
    from pymongo import UpdateOne

    now = datetime.now(timezone.utc)
    coll = DBConnect().get_db().sponsorship_signals if not dry_run else None
    ops = []
    written = 0

    def _flush():
        nonlocal written, ops
        if ops and coll is not None:
            coll.bulk_write(ops, ordered=False)
            written += len(ops)
        ops = []

    for norm, a in agg.items():
        doc = {
            "employer_norm": norm,
            "employer_display": a.display or norm.title(),
            "lca_filings_5y": int(a.lca_filings),
            "approvals_5y": int(a.approvals),
            "denials_5y": int(a.denials),
            "median_wage": _median(a.wages),
            "top_titles": [t for t, _ in a.titles.most_common(_TOP_TITLES)],
            "last_filed_at": a.last_filed_at,
            "computed_at": now,
        }
        if dry_run:
            continue
        ops.append(UpdateOne({"employer_norm": norm}, {"$set": doc}, upsert=True))
        if len(ops) >= _UPSERT_BATCH:
            _flush()
    _flush()
    return written


def _print_sample(agg, n=10):
    print(f"\n== Sample of {min(n, len(agg))} aggregated employers ==")
    for norm, a in list(agg.items())[:n]:
        print(f"  {a.display or norm!r}: lca={a.lca_filings} approvals={a.approvals} "
              f"denials={a.denials} median_wage={_median(a.wages)} "
              f"titles={[t for t, _ in a.titles.most_common(3)]}")


def main():
    parser = argparse.ArgumentParser(description="Ingest DOL LCA + USCIS H-1B data into sponsorship_signals")
    parser.add_argument("--lca", help="Path to a DOL OFLC LCA disclosure file (CSV or XLSX)")
    parser.add_argument("--uscis", action="append", default=[],
                        help="Path to a USCIS H-1B Employer Data Hub CSV (repeatable, one per FY)")
    parser.add_argument("--years", type=int, default=5,
                        help="Number of most-recent fiscal years to include (default 5)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max rows to process per file (for quick testing; 0 = all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Aggregate + print a sample, but write nothing")
    args = parser.parse_args()

    if not args.lca and not args.uscis:
        parser.error("provide at least one of --lca or --uscis")

    min_year = datetime.now(timezone.utc).year - args.years + 1
    logger.info("Including fiscal years >= %d (last %d years)", min_year, args.years)

    agg = defaultdict(_Agg)

    if args.lca:
        path = Path(args.lca)
        if not path.exists():
            parser.error(f"--lca file not found: {path}")
        _ingest_lca(path, agg, min_year, args.limit)

    for uscis_path in args.uscis:
        path = Path(uscis_path)
        if not path.exists():
            parser.error(f"--uscis file not found: {path}")
        _ingest_uscis(path, agg, min_year, args.limit)

    logger.info("Aggregated %d distinct employers", len(agg))
    if not agg:
        logger.warning("Nothing to write.")
        return

    _print_sample(agg)

    if args.dry_run:
        print("\n(dry run - nothing written to sponsorship_signals)")
        return

    written = _upsert(agg, dry_run=False)
    print(f"\nDone. Upserted {written} employer rows into sponsorship_signals.")
    print("Reminder: ensure the unique index on employer_norm exists "
          "(scripts/migrate_indexes.py).")


if __name__ == "__main__":
    main()
