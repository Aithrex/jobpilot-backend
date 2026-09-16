"""Sponsorship-intel ingest — build the ``sponsorship_signals`` collection.

  *** HUMAN-RUN SCRIPT — REQUIRES OUT-OF-BAND DATA FILES. NOT run by Lambda. ***

One row per EMPLOYER (not per legal entity, not per fiscal year) for the
read-only ``services.sponsorship_service`` to score. It downloads NOTHING: the
government files are large and must be fetched by a human first, then passed by
path.

Data sources (download manually, then pass with --uscis / --lca):

  1. **USCIS H-1B Employer Data Hub** — one row per employer x fiscal year x
     NAICS x city, XLSX or CSV::

         https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub

     -> ``approvals_5y``, ``denials_5y``, plus NAICS / city / state / Tax-ID
     last-4, which this script uses for entity rollup and for the gap report.
     Every column whose header ends in "Approval(s)"/"Denial(s)" is summed, so
     the FY2026 layout (New Employment / Continuation / Change with Same
     Employer / New Concurrent / Change of Employer / Amended) and the older
     two-column layout (Initial / Continuing) both parse with no code change.

  2. **DOL OFLC LCA Disclosure Data** (H-1B/H-1B1/E-3), quarterly XLSX/CSV::

         https://www.dol.gov/agencies/eta/foreign-labor/performance

     -> ``lca_filings_5y``, ``median_wage``, ``top_titles``, ``last_filed_at``.

**THE WINDOW IS WHATEVER THE FILES SAY, AND THE DOCUMENT RECORDS IT.** The
fiscal years are read from the data, not assumed: ``data_window.fiscal_years``
and ``data_window.years`` carry what was actually ingested and
``data_window.label`` is the sentence a human should read. A single-FY file
produces ``years: 1``, and when that disagrees with
``sponsorship_service.DATA_WINDOW_YEARS`` the run says so — loudly, on stderr —
and flags every document with ``data_window.service_claim_mismatch``. The
contract keys are still named ``approvals_5y`` / ``denials_5y`` because the
consuming service reads those names; the honest window lives beside them and is
never inferred from the key.

**Fields with no source are ABSENT, not zero.** With no --lca file there is no
``lca_filings_5y``, ``median_wage``, ``top_titles`` or ``last_filed_at`` on the
document at all. ``sponsorship_service`` defaults each of them on read (0, None,
[], None), so a USCIS-only row scores on approvals alone — which is the true
state of our knowledge. Writing ``median_wage: 0`` would be a claim.

Entity rollup (``scripts/data/sponsorship_rollups.py``) merges subsidiary legal
entities onto one employer on one of three bases, and NEVER on a name
resemblance — the same rule ``employer_registry.merge_decision`` enforces:

  * ``curated_alias``     — an exact-name whitelist, human-curated, with the
                            refusals recorded next to the merges;
  * ``employer_registry`` — both names resolve to ONE ``employers`` row whose
                            identity is domain- or curated-based (>= 0.80);
  * ``tax_id_and_brand``  — identical Tax-ID last-4 AND first name token AND
                            petitioner state (the last-4 is never probative
                            alone; USCIS publishes only four digits).

Everything else that merely shares a leading token is left UNMERGED and written
to the run's ``--candidates`` JSON for review.

Safety:
  * ``--dry-run`` is the DEFAULT. Writing requires an explicit ``--apply``.
  * Writes touch exactly one collection, ``sponsorship_signals``, via keyed
    ``ReplaceOne(upsert=True)`` — idempotent, and an interrupted run is resumed
    by simply re-running it (there is no partial state to clean up).
  * ``--prune`` removes documents left by an EARLIER run of this script (by
    ``ingest_run_id``) and refuses to run when ``--limit`` made the pass partial.

Usage (run from the repo root; --apply needs a LIVE MongoDB connection)::

    python scripts/ingest_sponsorship.py --uscis "Employer Information.xlsx"
    python scripts/ingest_sponsorship.py --uscis <file> --apply --prune
    python scripts/ingest_sponsorship.py --uscis <file> --limit 5000   # quick test
    python scripts/ingest_sponsorship.py --lca <xlsx> --uscis <csv> --apply
"""
import argparse
import csv
import hashlib
import json
import logging
import sys
import uuid
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

from services.sponsorship_service import (  # noqa: E402
    DATA_WINDOW_YEARS, SOURCE_LABEL, normalize_employer,
)
from scripts.data import sponsorship_rollups as rollups  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("ingest_sponsorship")

INGEST_VERSION = "sponsorship-ingest-2.0.0"
COLLECTION = "sponsorship_signals"

USCIS_DATASET = "uscis_h1b_employer_data_hub"
USCIS_LABEL = "USCIS H-1B Employer Data Hub"
LCA_DATASET = "dol_oflc_lca_disclosure"
LCA_LABEL = "DOL OFLC LCA Disclosure Data"

# Cap wage samples kept per employer so a national file can't blow up memory;
# the median of a few thousand samples is an honest estimate.
_MAX_WAGE_SAMPLES = 4096
_TOP_TITLES = 5
_UPSERT_BATCH = 1000
# Bounded arrays on the document (no unbounded array growth).
_MAX_ENTITIES_ON_DOC = 40
_MAX_REFUSED_ON_DOC = 20
_MAX_NAICS_ON_DOC = 8

# --- Flexible column resolution (year-to-year the gov schemas drift) ---------
_LCA_EMPLOYER_COLS = ("EMPLOYER_NAME", "LCA_CASE_EMPLOYER_NAME", "EMPLOYER_BUSINESS_DBA")
_LCA_STATUS_COLS = ("CASE_STATUS", "STATUS", "LCA_CASE_STATUS")
_LCA_TITLE_COLS = ("JOB_TITLE", "SOC_TITLE", "LCA_CASE_JOB_TITLE", "PW_SOC_TITLE")
_LCA_WAGE_COLS = ("WAGE_RATE_OF_PAY_FROM", "LCA_CASE_WAGE_RATE_FROM",
                  "WAGE_RATE_OF_PAY_FROM_1", "PREVAILING_WAGE")
_LCA_WAGE_UNIT_COLS = ("WAGE_UNIT_OF_PAY", "LCA_CASE_WAGE_RATE_UNIT",
                       "PW_UNIT_OF_PAY", "WAGE_UNIT_OF_PAY_1")
_LCA_DATE_COLS = ("DECISION_DATE", "CASE_SUBMITTED", "RECEIVED_DATE", "LCA_CASE_SUBMIT")

_USCIS_EMPLOYER_COLS = ("Employer (Petitioner) Name", "Employer Petitioner Name",
                        "Employer", "Petitioner", "EMPLOYER_PETITIONER_NAME")
_USCIS_FY_COLS = ("Fiscal Year", "FiscalYear", "FISCAL_YEAR")
_USCIS_TAX_COLS = ("Tax ID", "TaxID", "Tax Id")
_USCIS_NAICS_COLS = ("Industry (NAICS) Code", "NAICS", "Industry NAICS Code", "NAICS Code")
_USCIS_CITY_COLS = ("Petitioner City", "City")
_USCIS_STATE_COLS = ("Petitioner State", "State")

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


def iter_rows(path: Path):
    """Yield dict rows from a CSV or XLSX file, STREAMING (bounded memory).

    The USCIS export is 4.5 MB / 49k rows of XLSX; ``read_only=True`` plus
    ``iter_rows`` keeps one row in memory at a time rather than materializing
    the sheet. Duplicate/blank header cells are disambiguated so a row dict
    never silently loses a column.
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
        try:
            ws = wb.active
            rows = ws.iter_rows(values_only=True)
            try:
                raw_header = next(rows)
            except StopIteration:
                return
            header = _dedupe_header(raw_header)
            for raw in rows:
                yield {header[i]: raw[i] for i in range(min(len(header), len(raw)))}
        finally:
            wb.close()
        return

    # CSV / TSV / txt.
    fh, enc = _open_text(path)
    logger.info("Reading %s as CSV (encoding=%s)", path.name, enc)
    try:
        for row in csv.DictReader(fh):
            yield row
    finally:
        fh.close()


def _dedupe_header(raw_header) -> list:
    """['A', None, 'A'] -> ['A', 'column_2', 'A__2'] (never collapse two columns)."""
    out, seen = [], Counter()
    for i, cell in enumerate(raw_header):
        name = "" if cell is None else str(cell).strip()
        if not name:
            name = f"column_{i + 1}"
        seen[name] += 1
        out.append(name if seen[name] == 1 else f"{name}__{seen[name]}")
    return out


def _resolve(header_keys, candidates):
    """Return the first candidate present in the header (case-insensitive), else None."""
    lower = {str(k).strip().lower(): k for k in header_keys if k is not None}
    for cand in candidates:
        hit = lower.get(cand.lower())
        if hit is not None:
            return hit
    return None


def resolve_outcome_columns(header_keys):
    """(approval_cols, denial_cols) by header SUFFIX, not by an enumerated list.

    USCIS has changed this column set at least twice (Initial/Continuing ->
    New Employment/Continuation/Change with Same Employer/New Concurrent/Change
    of Employer/Amended). Every one of them ends in "Approval(s)"/"Denial(s)",
    so matching the suffix parses both layouts and any future one, instead of
    silently summing a subset.
    """
    approvals, denials = [], []
    for key in header_keys:
        low = str(key or "").strip().lower()
        if low.endswith(("approval", "approvals")):
            approvals.append(key)
        elif low.endswith(("denial", "denials")):
            denials.append(key)
    return approvals, denials


def _num(value):
    if value is None:
        return None
    try:
        cleaned = str(value).replace(",", "").replace("$", "").strip()
        return float(cleaned) if cleaned else None
    except (TypeError, ValueError):
        return None


def _int(value) -> int:
    n = _num(value)
    try:
        return int(n) if n is not None else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def _text(value) -> str:
    return str(value).strip() if value is not None else ""


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


def file_digest(path: Path) -> str:
    """SHA-256 of the source file — the vintage claim has to be checkable."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def naics_sector(value) -> tuple:
    """'54 - Professional, Scientific...' -> ('54', 'Professional, Scientific...')."""
    text = _text(value)
    if not text:
        return "", ""
    if " - " in text:
        code, _, label = text.partition(" - ")
        return code.strip(), label.strip()
    return text.strip(), ""


# ---------------------------------------------------------------------------
# Per-legal-entity accumulator (one per EXACT normalized petitioner name).
# ---------------------------------------------------------------------------
class Entity:
    __slots__ = ("norm", "display", "approvals", "denials", "rows", "fiscal_years",
                 "tax_ids", "naics", "places", "lca_filings", "wages", "titles",
                 "last_filed_at")

    def __init__(self, norm=""):
        self.norm = norm
        self.display = ""
        self.approvals = 0
        self.denials = 0
        self.rows = 0
        self.fiscal_years = set()
        self.tax_ids = Counter()
        self.naics = Counter()
        self.places = Counter()
        self.lca_filings = 0
        self.wages = []
        self.titles = Counter()
        self.last_filed_at = None

    # The longest raw spelling is the most informative one for a human badge
    # ("APPLE INC" over "APPLE"), and picking deterministically (length, then
    # lexical) keeps two runs over the same file byte-identical.
    def note_display(self, raw_name):
        name = _text(raw_name)
        if not name:
            return
        if not self.display or (len(name), name) > (len(self.display), self.display):
            self.display = name

    def note_date(self, dt):
        if dt and (self.last_filed_at is None or dt > self.last_filed_at):
            self.last_filed_at = dt

    @property
    def top_tax_id(self) -> str:
        return self.tax_ids.most_common(1)[0][0] if self.tax_ids else ""

    @property
    def top_place(self) -> tuple:
        if not self.places:
            return ("", "")
        return self.places.most_common(1)[0][0]

    @property
    def top_naics(self) -> tuple:
        if not self.naics:
            return ("", "")
        return self.naics.most_common(1)[0][0]


# ---------------------------------------------------------------------------
# USCIS Employer Data Hub ingestion.
# ---------------------------------------------------------------------------
def ingest_uscis(path: Path, entities: dict, min_year: int, limit: int = 0) -> dict:
    """Stream a Data Hub export into ``entities``. Returns per-file stats."""
    rows = iter_rows(path)
    try:
        first = next(rows)
    except StopIteration:
        logger.warning("USCIS file %s is empty", path.name)
        return {"rows": 0, "fiscal_years": [], "skipped_blank_name": 0}
    keys = list(first.keys())
    emp_col = _resolve(keys, _USCIS_EMPLOYER_COLS)
    if not emp_col:
        raise SystemExit(
            f"USCIS file '{path.name}' has no recognizable employer column "
            f"(looked for {_USCIS_EMPLOYER_COLS}). Columns present: {keys[:12]}..."
        )
    approval_cols, denial_cols = resolve_outcome_columns(keys)
    if not approval_cols and not denial_cols:
        raise SystemExit(
            f"USCIS file '{path.name}' has no Approval/Denial columns "
            f"(no header ends in 'Approval(s)'/'Denial(s)'). Columns: {keys[:20]}"
        )
    fy_col = _resolve(keys, _USCIS_FY_COLS)
    tax_col = _resolve(keys, _USCIS_TAX_COLS)
    naics_col = _resolve(keys, _USCIS_NAICS_COLS)
    city_col = _resolve(keys, _USCIS_CITY_COLS)
    state_col = _resolve(keys, _USCIS_STATE_COLS)
    logger.info("USCIS columns -> employer=%r fy=%r tax=%r naics=%r city=%r",
                emp_col, fy_col, tax_col, naics_col, city_col)
    logger.info("USCIS approvals=%s", approval_cols)
    logger.info("USCIS denials=%s", denial_cols)

    stats = {"rows": 0, "kept": 0, "skipped_blank_name": 0,
             "skipped_unnormalizable_name": 0, "skipped_out_of_window": 0,
             "approvals": 0, "denials": 0, "fiscal_years": set()}

    def _handle(row):
        stats["rows"] += 1
        raw_name = _text(row.get(emp_col))
        if not raw_name:
            # USCIS publishes a handful of rows with the petitioner name
            # suppressed. They carry real counts but name nobody, so they can
            # neither be attributed nor rolled up: counted, then dropped.
            stats["skipped_blank_name"] += 1
            return
        norm = normalize_employer(raw_name)
        if not norm:
            # A name that is entirely punctuation/suffix tokens ("LLC").
            stats["skipped_unnormalizable_name"] += 1
            return
        fy = None
        if fy_col:
            fy_val = _num(row.get(fy_col))
            if fy_val is not None:
                fy = int(fy_val)
                if min_year and fy < min_year:
                    stats["skipped_out_of_window"] += 1
                    return
        approvals = sum(_int(row.get(c)) for c in approval_cols)
        denials = sum(_int(row.get(c)) for c in denial_cols)

        ent = entities.get(norm)
        if ent is None:
            ent = entities[norm] = Entity(norm)
        ent.note_display(raw_name)
        ent.rows += 1
        ent.approvals += approvals
        ent.denials += denials
        if fy is not None:
            ent.fiscal_years.add(fy)
            stats["fiscal_years"].add(fy)
        if tax_col:
            tax = _text(row.get(tax_col))
            if tax:
                ent.tax_ids[tax] += approvals + denials + 1
        if naics_col:
            code, label = naics_sector(row.get(naics_col))
            if code:
                ent.naics[(code, label)] += approvals or 1
        if city_col or state_col:
            place = (_text(row.get(city_col)).upper() if city_col else "",
                     _text(row.get(state_col)).upper() if state_col else "")
            if any(place):
                ent.places[place] += approvals or 1
        stats["kept"] += 1
        stats["approvals"] += approvals
        stats["denials"] += denials

    _handle(first)
    for row in rows:
        _handle(row)
        if limit and stats["rows"] >= limit:
            logger.info("USCIS --limit %d reached", limit)
            break
    stats["fiscal_years"] = sorted(stats["fiscal_years"])
    logger.info("USCIS %s: %d rows, %d kept, %d approvals, %d denials, FY %s",
                path.name, stats["rows"], stats["kept"], stats["approvals"],
                stats["denials"], stats["fiscal_years"])
    if stats["skipped_blank_name"] or stats["skipped_unnormalizable_name"]:
        logger.warning("USCIS %s: dropped %d blank-name and %d unnormalizable-name rows",
                       path.name, stats["skipped_blank_name"],
                       stats["skipped_unnormalizable_name"])
    return stats


# ---------------------------------------------------------------------------
# LCA (DOL) ingestion — unchanged contract, same accumulator.
# ---------------------------------------------------------------------------
def ingest_lca(path: Path, entities: dict, min_year: int, limit: int = 0) -> dict:
    rows = iter_rows(path)
    try:
        first = next(rows)
    except StopIteration:
        logger.warning("LCA file %s is empty", path.name)
        return {"rows": 0, "fiscal_years": []}
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
    logger.info("LCA columns -> employer=%r status=%r title=%r wage=%r date=%r",
                emp_col, status_col, title_col, wage_col, date_col)

    stats = {"rows": 0, "kept": 0, "fiscal_years": set()}

    def _handle(row):
        stats["rows"] += 1
        raw_name = row.get(emp_col)
        norm = normalize_employer(raw_name)
        if not norm:
            return
        if status_col:
            status = _text(row.get(status_col)).upper()
            if status and status not in ("CERTIFIED", "CERTIFIED-WITHDRAWN",
                                         "CERTIFIED - WITHDRAWN"):
                return
        dt = _parse_date(row.get(date_col)) if date_col else None
        if dt and min_year and dt.year < min_year:
            return
        ent = entities.get(norm)
        if ent is None:
            ent = entities[norm] = Entity(norm)
        ent.note_display(raw_name)
        ent.lca_filings += 1
        ent.note_date(dt)
        if dt:
            # DOL fiscal years start 1 Oct.
            stats["fiscal_years"].add(dt.year + 1 if dt.month >= 10 else dt.year)
        if title_col:
            title = _text(row.get(title_col))
            if title:
                ent.titles[title[:120]] += 1
        if wage_col and len(ent.wages) < _MAX_WAGE_SAMPLES:
            wage = _num(row.get(wage_col))
            if wage and wage > 0:
                factor = 1.0
                if unit_col:
                    factor = _WAGE_ANNUAL_FACTOR.get(_text(row.get(unit_col)).lower(), 1.0)
                annual = wage * factor
                if 5_000 <= annual <= 2_000_000:
                    ent.wages.append(annual)
        stats["kept"] += 1

    _handle(first)
    for row in rows:
        _handle(row)
        if limit and stats["rows"] >= limit:
            logger.info("LCA --limit %d reached", limit)
            break
    stats["fiscal_years"] = sorted(stats["fiscal_years"])
    logger.info("LCA %s: %d rows, %d qualifying filings", path.name, stats["rows"],
                stats["kept"])
    return stats


# ---------------------------------------------------------------------------
# Entity rollup.
# ---------------------------------------------------------------------------
class Group:
    """One EMPLOYER: a canonical key, a display name, and its legal entities."""

    __slots__ = ("key", "display", "basis", "members", "aliases", "refused", "note")

    def __init__(self, key, display="", basis=rollups.BASIS_SINGLE):
        self.key = key
        self.display = display
        self.basis = basis
        self.members = []       # [Entity]
        self.aliases = []       # extra lookup keys (not present in the source file)
        self.refused = []       # [{name, approvals, reason}]
        self.note = ""

    @property
    def approvals(self):
        return sum(e.approvals for e in self.members)

    @property
    def denials(self):
        return sum(e.denials for e in self.members)

    @property
    def lca_filings(self):
        return sum(e.lca_filings for e in self.members)


def registry_groups(keys, *, quiet=False) -> dict:
    """normalized name -> shared employer_id, for names ONE strong registry row owns.

    "Strong" is ``identity_confidence >= 0.80`` AFTER following
    ``parent_employer_id`` — i.e. the ``employers`` row is domain- or
    curated-keyed, which is exactly what ``merge_decision`` will merge on. A
    name-keyed row (0.45) proves nothing about two different names, so it is
    skipped. Returns {} when the registry is unreachable: the rollup degrades,
    it never guesses.
    """
    try:
        from utils.db_connect import DBConnect
        coll = DBConnect().get_db().employers
        rows = list(coll.find({}, {"employer_id": 1, "name_key": 1, "aliases": 1,
                                   "parent_employer_id": 1, "identity_confidence": 1,
                                   "_id": 0}))
    except Exception as exc:  # pragma: no cover - needs a live DB
        if not quiet:
            logger.warning("employers registry unavailable (%s); rollup falls back "
                           "to curated + tax-id evidence only", exc)
        return {}

    by_id = {r.get("employer_id"): r for r in rows if r.get("employer_id")}

    def _follow(row, depth=0):
        parent = row.get("parent_employer_id")
        if parent and parent in by_id and depth < 5:
            return _follow(by_id[parent], depth + 1)
        return row

    name_to_ids = defaultdict(set)
    for row in rows:
        for raw in [row.get("name_key")] + list(row.get("aliases") or []):
            key = normalize_employer(raw)
            if key:
                name_to_ids[key].add(row.get("employer_id"))

    out = {}
    wanted = set(keys)
    for key in wanted:
        ids = name_to_ids.get(key)
        if not ids or len(ids) != 1:
            continue  # ambiguous -> prove nothing, merge nothing
        row = by_id.get(next(iter(ids)))
        if not row:
            continue
        final = _follow(row)
        if float(final.get("identity_confidence") or 0) < 0.80:
            continue
        out[key] = final.get("employer_id")
    return out


def tax_id_groups(entities: dict) -> dict:
    """normalized name -> a (tax4, first token, state) bucket id shared by >1 name.

    USCIS publishes only the last four digits of the EIN — 9,879 distinct values
    across 44k employers — so the field is NEVER probative alone. Requiring the
    same first name token and the same petitioner state alongside it is two more
    independent signals, and in practice what it finds is one entity spelled two
    ways ("... D B A APTIV" vs "... DBA APTIV"), which is exactly the case a name
    key cannot merge and a human should not have to.
    """
    buckets = defaultdict(list)
    for key, ent in entities.items():
        tax = ent.top_tax_id
        state = ent.top_place[1]
        first = key.split(" ")[0] if key else ""
        if not tax or not state or len(first) < 3:
            continue
        buckets[(tax, first, state)].append(key)
    out = {}
    for bucket, names in buckets.items():
        if len(names) > 1:
            for name in names:
                out[name] = "tax:%s:%s:%s" % bucket
    return out


def build_groups(entities: dict, *, use_registry=True, use_tax_id=True,
                 curated=None, registry_map=None) -> tuple:
    """Roll legal entities up into employers. Returns (groups, rollup_stats).

    Order of authority: curated whitelist > employers registry > tax-id+brand.
    A name claimed by a curated group is never moved by a mechanical rule; a
    mechanical rule that would span two curated groups is refused and recorded.
    """
    curated_groups = rollups.GROUPS if curated is None else curated
    problems = rollups.validate_groups(curated_groups)
    if problems:
        raise SystemExit("rollup table is invalid:\n  " + "\n  ".join(problems))

    member_of = rollups.member_index(curated_groups)
    refused_by = rollups.excluded_index(curated_groups)

    stats = {"curated": 0, "registry": 0, "tax_id": 0, "single": 0,
             "curated_members_absent": [], "refusals": 0}

    groups = {}          # group key -> Group
    assigned = {}        # entity key -> group key

    def _group(key, display, basis):
        grp = groups.get(key)
        if grp is None:
            grp = groups[key] = Group(key, display, basis)
        return grp

    # 1. curated whitelist -------------------------------------------------
    for cfg in curated_groups:
        present = [name for name in cfg["members"] if name in entities]
        absent = [name for name in cfg["members"] if name not in entities]
        stats["curated_members_absent"].extend(f"{cfg['key']}:{n}" for n in absent)
        if not present:
            continue
        grp = _group(cfg["key"], cfg["display"], rollups.BASIS_CURATED)
        grp.note = cfg.get("note", "")
        # A curated member with no row in this file still belongs to this
        # employer, and a POSTING may well use that spelling ("Ernst & Young"
        # normalizes to a name USCIS never printed). It contributes no numbers,
        # so it becomes a lookup alias rather than being dropped.
        grp.aliases = sorted({a for a in list(cfg.get("aliases") or []) + absent
                              if a not in present})
        for name in present:
            grp.members.append(entities[name])
            assigned[name] = cfg["key"]
        for name in (cfg.get("excluded") or []):
            ent = entities.get(name)
            grp.refused.append({
                "employer_norm": name,
                "employer_display": ent.display if ent else "",
                "approvals": ent.approvals if ent else 0,
                "reason": "curated_exclusion",
            })
            stats["refusals"] += 1
        stats["curated"] += 1

    # 2. employers registry -------------------------------------------------
    if use_registry:
        reg = registry_map if registry_map is not None else registry_groups(entities.keys())
        by_employer = defaultdict(list)
        for name, employer_id in reg.items():
            if name in entities:
                by_employer[employer_id].append(name)
        for employer_id, names in by_employer.items():
            if len(names) < 2:
                continue
            owners = {assigned[n] for n in names if n in assigned}
            if len(owners) > 1:
                # The registry says one employer, the curated table says two.
                # Refuse: two curated decisions outrank one registry row.
                stats["refusals"] += 1
                logger.warning("registry group %s spans curated groups %s — refused",
                               employer_id, sorted(owners))
                continue
            free = [n for n in names if n not in assigned]
            if not free:
                continue
            if owners:
                grp = groups[next(iter(owners))]
            else:
                lead = max(free, key=lambda n: (entities[n].approvals, n))
                grp = _group(lead, entities[lead].display, rollups.BASIS_REGISTRY)
                stats["registry"] += 1
            for name in free:
                grp.members.append(entities[name])
                assigned[name] = grp.key
                if grp.basis == rollups.BASIS_CURATED:
                    grp.note = (grp.note + " ").strip() + \
                        f"[+{name} via employers registry {employer_id}]"

    # 3. tax id + brand token + state --------------------------------------
    if use_tax_id:
        for bucket, names in _invert(tax_id_groups(entities)).items():
            free = [n for n in names if n not in assigned]
            owners = {assigned[n] for n in names if n in assigned}
            if len(owners) > 1 or not free:
                continue
            if owners:
                grp = groups[next(iter(owners))]
            else:
                lead = max(free, key=lambda n: (entities[n].approvals, n))
                grp = _group(lead, entities[lead].display, rollups.BASIS_TAX_ID)
                grp.note = f"merged on {bucket} (Tax-ID last-4 + first token + state)"
                stats["tax_id"] += 1
            for name in free:
                grp.members.append(entities[name])
                assigned[name] = grp.key

    # 4. everything else is its own employer --------------------------------
    for name, ent in entities.items():
        if name in assigned:
            continue
        grp = _group(name, ent.display, rollups.BASIS_SINGLE)
        grp.members.append(ent)
        assigned[name] = name
        stats["single"] += 1

    # 4b. An alias must not overwrite a real employer. Alias keys are extra
    #     lookup names invented by the curated table, so one colliding with a
    #     name that actually filed (or with another group's key) would silently
    #     replace that employer's row — the unique index would take the last
    #     writer. Drop it and say so.
    for grp in groups.values():
        keep = []
        for alias in grp.aliases:
            owner = assigned.get(alias)
            if (owner and owner != grp.key) or (alias in groups and alias != grp.key):
                logger.warning("alias %r of group %r collides with employer %r — dropped",
                               alias, grp.key, owner or alias)
                stats["refusals"] += 1
                continue
            keep.append(alias)
        grp.aliases = keep

    # 5. near-miss candidates: shares a leading token with a rolled-up group,
    #    was NOT merged, and is not already an explicit refusal. This is the
    #    review queue, and it is an ARTIFACT, never a write.
    candidates = []
    # A group is "real" if a human curated it or evidence merged it — a single
    # curated employer (Meta files under one LLC) still has lookalikes worth
    # reviewing, so the token index is not limited to multi-entity groups.
    rolled = {k: g for k, g in groups.items()
              if len(g.members) > 1 or g.basis != rollups.BASIS_SINGLE}
    lead_tokens = {}
    for key, grp in rolled.items():
        token = key.split(" ")[0]
        if len(token) >= 4:
            lead_tokens.setdefault(token, []).append(key)
    for name, ent in entities.items():
        owner = assigned.get(name)
        token = name.split(" ")[0]
        targets = [k for k in lead_tokens.get(token, []) if k != owner]
        if not targets:
            continue
        if owner and len(groups[owner].members) > 1:
            continue  # already merged somewhere on real evidence
        candidates.append({
            "employer_norm": name,
            "employer_display": ent.display,
            "approvals": ent.approvals,
            "denials": ent.denials,
            "tax_id_last4": ent.top_tax_id,
            "city": ent.top_place[0],
            "state": ent.top_place[1],
            "naics": ent.top_naics[0],
            "would_join": targets[0],
            "refused_by": refused_by.get(name, ""),
            "reason": ("curated_exclusion" if name in refused_by
                       else "name_prefix_only_no_corroborating_evidence"),
        })
    candidates.sort(key=lambda c: -c["approvals"])

    stats["groups"] = len(groups)
    stats["entities"] = len(entities)
    stats["rolled_up_groups"] = len(rolled)
    stats["candidates"] = len(candidates)
    # A curated member name that is not in the file is not an error (the FY may
    # simply have no filing), but a silent one hides a typo, so it is reported.
    if stats["curated_members_absent"]:
        logger.info("curated members with no row in this file (%d): %s",
                    len(stats["curated_members_absent"]),
                    ", ".join(sorted(stats["curated_members_absent"])[:20]))
    return list(groups.values()), stats, candidates


def _invert(mapping: dict) -> dict:
    out = defaultdict(list)
    for key, value in mapping.items():
        out[value].append(key)
    return out


# ---------------------------------------------------------------------------
# Document building.
# ---------------------------------------------------------------------------
def window_block(fiscal_years, has_lca) -> dict:
    """The vintage block. It states what we HAVE, not what the badge says."""
    years = sorted({int(y) for y in fiscal_years if y})
    span = len(years)
    if not years:
        label = "fiscal year not stated in the source file"
    elif span == 1:
        label = f"USCIS fiscal year {years[0]} (single fiscal year)"
    else:
        label = f"fiscal years {years[0]}-{years[-1]} ({span} fiscal years)"
    return {
        "fiscal_years": years,
        "years": span,
        "label": label,
        # The service's public copy says DATA_WINDOW_YEARS everywhere. When the
        # data does not cover that many years, every consumer of this row is
        # over-claiming and the row says so rather than hiding it.
        "service_claim_years": DATA_WINDOW_YEARS,
        "service_claim_mismatch": bool(span and span != DATA_WINDOW_YEARS),
        # Which of the two datasets the service's SOURCE_LABEL names we
        # actually read. False means the label over-states the source.
        "dol_lca_included": bool(has_lca),
        "uscis_included": True,
    }


def build_documents(groups, *, sources, window, run_id, now=None) -> list:
    """One primary document per employer + one alias document per extra key.

    Alias documents duplicate the employer's numbers under the OTHER names a job
    posting might carry ("Amazon Web Services", "AWS"), because
    ``sponsorship_service`` looks up by the posting's company string and
    ``employer_norm`` is unique. They carry the PARENT's display name, so the
    rendered copy reads "Amazon had N approvals", never "AWS had N approvals" —
    the number is the group's and the sentence must be too.
    """
    now = now or datetime.now(timezone.utc)
    provenance = {
        "ingest_version": INGEST_VERSION,
        "rollup_version": rollups.ROLLUP_VERSION,
        "run_id": run_id,
        "sources": sources,
    }
    docs = []
    for grp in groups:
        members = sorted(grp.members, key=lambda e: (-e.approvals, e.norm))
        naics = Counter()
        places = Counter()
        fy = set()
        for ent in members:
            naics.update(ent.naics)
            places.update(ent.places)
            fy |= ent.fiscal_years
        primary_naics, naics_label = (naics.most_common(1)[0][0] if naics else ("", ""))
        city, state = (places.most_common(1)[0][0] if places else ("", ""))

        doc = {
            "employer_norm": grp.key,
            "employer_display": grp.display or grp.key.title(),
            # --- contract keys read by services/sponsorship_service.py -------
            "approvals_5y": grp.approvals,
            "denials_5y": grp.denials,
            "computed_at": now,
            # --- honest vintage ---------------------------------------------
            "data_window": dict(window, fiscal_years=sorted(fy) or window["fiscal_years"]),
            "source_label": " + ".join(s["label"] for s in sources),
            "ingested_at": now,
            "ingest_run_id": run_id,
            "ingest_version": INGEST_VERSION,
            "provenance": provenance,
            # --- identity ----------------------------------------------------
            "rollup": {
                "basis": grp.basis,
                "is_alias": False,
                "entity_count": len(members),
                "entities": [
                    {
                        "employer_norm": e.norm,
                        "name": e.display,
                        "approvals": e.approvals,
                        "denials": e.denials,
                        "tax_id_last4": e.top_tax_id,
                        "city": e.top_place[0],
                        "state": e.top_place[1],
                    } for e in members[:_MAX_ENTITIES_ON_DOC]
                ],
                "entities_truncated": max(0, len(members) - _MAX_ENTITIES_ON_DOC),
                "refused": grp.refused[:_MAX_REFUSED_ON_DOC],
                "note": grp.note,
            },
            "naics": {
                "primary": primary_naics,
                "label": naics_label,
                "mix": [{"code": c, "label": l, "approvals": n}
                        for (c, l), n in naics.most_common(_MAX_NAICS_ON_DOC)],
            },
            "location": {"city": city, "state": state},
        }
        # Fields with NO source are ABSENT, not zero (see module docstring).
        lca = grp.lca_filings
        if lca:
            wages = []
            titles = Counter()
            last_filed = None
            for ent in members:
                wages.extend(ent.wages)
                titles.update(ent.titles)
                if ent.last_filed_at and (last_filed is None or ent.last_filed_at > last_filed):
                    last_filed = ent.last_filed_at
            doc["lca_filings_5y"] = lca
            doc["median_wage"] = _median(wages)
            doc["top_titles"] = [t for t, _ in titles.most_common(_TOP_TITLES)]
            doc["last_filed_at"] = last_filed
        docs.append(doc)

        seen = {grp.key}
        alias_keys = [e.norm for e in members if e.norm != grp.key] + list(grp.aliases)
        for alias in alias_keys:
            if not alias or alias in seen:
                continue
            seen.add(alias)
            alias_doc = dict(doc)
            alias_doc["employer_norm"] = alias
            alias_doc["rollup"] = dict(doc["rollup"], is_alias=True,
                                       canonical_norm=grp.key)
            docs.append(alias_doc)
    return docs


# ---------------------------------------------------------------------------
# Write.
# ---------------------------------------------------------------------------
def ensure_index(db):
    """The one index this collection needs: the unique upsert/lookup key.

    ``sponsorship_service.get_signal`` is ``find_one({'employer_norm': ...})``
    and ``bulk_signals`` is one ``$in`` over the same field; the ingest upserts
    on it. Unique because one employer may hold exactly one row — that is what
    makes the upsert idempotent. Also declared in scripts/migrate_indexes.py.
    """
    return db[COLLECTION].create_index(
        [("employer_norm", 1)], unique=True, background=True,
        name="employer_norm_unique")


def write_documents(docs, *, prune_run_id=None) -> dict:
    from pymongo import ReplaceOne
    from utils.db_connect import DBConnect

    db = DBConnect().get_db()
    logger.info("ensuring index: %s", ensure_index(db))
    coll = db[COLLECTION]
    written = 0
    ops = []
    for doc in docs:
        ops.append(ReplaceOne({"employer_norm": doc["employer_norm"]}, doc, upsert=True))
        if len(ops) >= _UPSERT_BATCH:
            coll.bulk_write(ops, ordered=False)
            written += len(ops)
            ops = []
            logger.info("  ... %d/%d written", written, len(docs))
    if ops:
        coll.bulk_write(ops, ordered=False)
        written += len(ops)
    out = {"written": written, "pruned": 0}
    if prune_run_id:
        res = coll.delete_many({"ingest_version": INGEST_VERSION,
                                "ingest_run_id": {"$ne": prune_run_id}})
        out["pruned"] = res.deleted_count
        logger.info("pruned %d document(s) from earlier runs", res.deleted_count)
    out["total"] = coll.count_documents({})
    return out


# ---------------------------------------------------------------------------
# Reporting.
# ---------------------------------------------------------------------------
def print_sample(docs, n=20):
    primaries = [d for d in docs if not d["rollup"]["is_alias"]]
    primaries.sort(key=lambda d: -d["approvals_5y"])
    print(f"\n== Top {min(n, len(primaries))} employers after rollup ==")
    for doc in primaries[:n]:
        roll = doc["rollup"]
        print(f"  {doc['approvals_5y']:7,d} approvals / {doc['denials_5y']:5,d} denials  "
              f"{doc['employer_norm']:38s} {doc['employer_display'][:34]:34s} "
              f"[{roll['basis']}, {roll['entity_count']} entit{'y' if roll['entity_count'] == 1 else 'ies'}]")


def main():
    parser = argparse.ArgumentParser(
        description="Ingest USCIS H-1B Data Hub (+ optional DOL LCA) into sponsorship_signals")
    parser.add_argument("--uscis", action="append", default=[],
                        help="USCIS H-1B Employer Data Hub export (XLSX or CSV; repeatable)")
    parser.add_argument("--lca", action="append", default=[],
                        help="DOL OFLC LCA disclosure file (XLSX or CSV; repeatable)")
    parser.add_argument("--years", type=int, default=0,
                        help="Drop fiscal years older than (current year - N + 1). "
                             "0 = keep everything the files contain (default)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max rows per file (quick test; 0 = all)")
    parser.add_argument("--apply", action="store_true",
                        help="WRITE to sponsorship_signals (default is a dry run)")
    parser.add_argument("--prune", action="store_true",
                        help="With --apply: delete documents left by EARLIER runs")
    parser.add_argument("--no-registry", action="store_true",
                        help="Skip the employers-registry rollup basis (no DB read)")
    parser.add_argument("--no-tax-id", action="store_true",
                        help="Skip the Tax-ID+brand+state rollup basis")
    parser.add_argument("--candidates", default="",
                        help="Write the un-merged near-miss review queue to this JSON path")
    parser.add_argument("--sample", type=int, default=20, help="Employers to print (default 20)")
    args = parser.parse_args()

    if not args.uscis and not args.lca:
        parser.error("provide at least one of --uscis or --lca")
    if args.prune and not args.apply:
        parser.error("--prune only means something with --apply")

    min_year = 0
    if args.years:
        min_year = datetime.now(timezone.utc).year - args.years + 1
        logger.info("Including fiscal years >= %d (--years %d)", min_year, args.years)

    run_id = uuid.uuid4().hex
    entities = {}
    sources = []
    fiscal_years = set()
    has_lca = False
    now = datetime.now(timezone.utc)

    for raw in args.uscis:
        path = Path(raw)
        if not path.exists():
            parser.error(f"--uscis file not found: {path}")
        stats = ingest_uscis(path, entities, min_year, args.limit)
        fiscal_years |= set(stats.get("fiscal_years") or [])
        sources.append({
            "dataset": USCIS_DATASET, "label": USCIS_LABEL, "file": path.name,
            "sha256": file_digest(path), "rows": stats["rows"],
            "rows_used": stats.get("kept", 0),
            "fiscal_years": stats.get("fiscal_years") or [],
            "skipped_blank_name": stats.get("skipped_blank_name", 0),
            "skipped_unnormalizable_name": stats.get("skipped_unnormalizable_name", 0),
            "ingested_at": now, "partial": bool(args.limit),
        })
    for raw in args.lca:
        path = Path(raw)
        if not path.exists():
            parser.error(f"--lca file not found: {path}")
        stats = ingest_lca(path, entities, min_year, args.limit)
        has_lca = True
        fiscal_years |= set(stats.get("fiscal_years") or [])
        sources.append({
            "dataset": LCA_DATASET, "label": LCA_LABEL, "file": path.name,
            "sha256": file_digest(path), "rows": stats["rows"],
            "rows_used": stats.get("kept", 0),
            "fiscal_years": stats.get("fiscal_years") or [],
            "ingested_at": now, "partial": bool(args.limit),
        })

    if not entities:
        logger.warning("Nothing parsed — nothing to write.")
        return 1

    groups, rstats, candidates = build_groups(
        entities, use_registry=not args.no_registry, use_tax_id=not args.no_tax_id)
    window = window_block(fiscal_years, has_lca)
    docs = build_documents(groups, sources=sources, window=window, run_id=run_id, now=now)

    print("\n== Ingest summary ==")
    for src in sources:
        print(f"  source   : {src['file']} ({src['label']}) rows={src['rows']:,} "
              f"used={src['rows_used']:,} FY={src['fiscal_years']} sha256={src['sha256'][:12]}…")
        if src.get("skipped_blank_name"):
            print(f"             dropped {src['skipped_blank_name']} blank-name and "
                  f"{src.get('skipped_unnormalizable_name', 0)} unnormalizable-name row(s)")
    print(f"  entities : {rstats['entities']:,} distinct legal entities (exact normalized name)")
    print(f"  employers: {rstats['groups']:,} after rollup "
          f"({rstats['rolled_up_groups']:,} hold more than one entity)")
    print(f"  bases    : curated={rstats['curated']} registry={rstats['registry']} "
          f"tax_id={rstats['tax_id']} single={rstats['single']}")
    print(f"  refused  : {rstats['refusals']} merge(s) explicitly refused; "
          f"{rstats['candidates']} near-miss candidate(s) left unmerged")
    print(f"  documents: {len(docs):,} "
          f"({sum(1 for d in docs if d['rollup']['is_alias']):,} alias rows)")
    print(f"  window   : {window['label']}")
    absent = [d for d in docs
              if not d["rollup"]["is_alias"] and d["approvals_5y"] == 0 and d["denials_5y"] > 0]
    print(f"  denial-only employers: {len(absent):,} (they band 'none' — 0 approvals is "
          f"an answer, not a gap)")

    if window["service_claim_mismatch"]:
        logger.warning(
            "WINDOW MISMATCH: this data covers %s, but sponsorship_service."
            "DATA_WINDOW_YEARS is %d and every headline it renders says 'in the last "
            "%d years'. Every document carries data_window.service_claim_mismatch=true. "
            "Fix the SERVICE copy (it is owned elsewhere) before this is shown to users.",
            window["label"], DATA_WINDOW_YEARS, DATA_WINDOW_YEARS)
    if not has_lca:
        logger.warning(
            "NO DOL LCA FILE: lca_filings_5y / median_wage / top_titles / "
            "last_filed_at are ABSENT from every document (not zero). "
            "sponsorship_service.SOURCE_LABEL still names both datasets (%r) — it "
            "over-states the source for these rows.", SOURCE_LABEL)

    print_sample(docs, args.sample)

    if args.candidates:
        out = Path(args.candidates)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "generated_at": now.isoformat(),
            "ingest_version": INGEST_VERSION,
            "rollup_version": rollups.ROLLUP_VERSION,
            "note": "Names sharing a leading token with a rolled-up employer that "
                    "were NOT merged, because a name resemblance is not evidence. "
                    "Promote one by adding it to scripts/data/sponsorship_rollups.py.",
            "count": len(candidates),
            "candidates": candidates,
        }, indent=2), encoding="utf-8")
        print(f"\nWrote {len(candidates)} rollup candidates -> {out}")

    if not args.apply:
        print("\n(dry run — nothing written. Re-run with --apply to write "
              f"{len(docs):,} documents to {COLLECTION}.)")
        return 0

    if args.prune and args.limit:
        parser.error("--prune with --limit would delete employers this partial run "
                     "never looked at")
    result = write_documents(docs, prune_run_id=run_id if args.prune else None)
    print(f"\nDone. Upserted {result['written']:,} documents into {COLLECTION} "
          f"(pruned {result['pruned']:,}; collection now holds {result['total']:,}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
