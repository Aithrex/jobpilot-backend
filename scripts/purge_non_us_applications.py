"""Retire non-US rows from the auto-apply ledger.

WHY THIS EXISTS
---------------
`apply_match_sweep` had no geo filter until `us_geo` was wired into it, so the
unattended fan-out enqueued whatever the crawler surfaced. The live ledger
picked up applications to Hyderabad, Bengaluru, Gurgaon, Yokneam and Krakow
postings for a US-based candidate. The sweep no longer creates them, but the
rows already written sit in the user's one-tap queue forever, each one asking
them to clear an employer account wall for a job they cannot take.

The product is US-only by decision (a separate India edition is planned), so
these are not near-misses to be re-scored — they should never have entered.

WHAT IT DOES
------------
Marks each affected row `skipped` with reason `non_us_posting`. It does NOT
delete: the ledger's dedup index is what makes a double-apply structurally
impossible, and removing the row would let the same posting be enqueued again.
`skipped` is documented as the sticky, non-retryable terminal for exactly this
("a deliberate decision"), which is what this is.

Rows that already reached `submitted`/`confirmed` are NEVER touched — an
application that actually went to an employer is a fact, not a queue entry.

USAGE
-----
    python scripts/purge_non_us_applications.py            # dry run, prints
    python scripts/purge_non_us_applications.py --apply    # writes
    python scripts/purge_non_us_applications.py --apply --user someone@x.com
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "shared", "python"))

# Statuses that represent a live queue entry. Anything else is either already
# terminal or already sent.
PURGEABLE = ("queued", "gathering", "needs_input", "ready", "ready_for_review",
             "failed")


def _location_from_url_and_title(row) -> str:
    """Best available location text for a ledger row.

    The ledger stores the posting's identity, not its location, so geo has to
    be recovered from the two fields that do carry it: the canonical inventory
    row when it is still there, and otherwise the apply URL, which every
    Workday tenant builds as `.../job/<Location>/<Title>_<req>`.
    """
    from urllib.parse import unquote

    url = str(row.get("apply_url") or "")
    if "/job/" in url:
        tail = url.split("/job/", 1)[1]
        segment = tail.split("/", 1)[0]
        return unquote(segment).replace("-", " ").strip()
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write the changes (default is a dry run)")
    ap.add_argument("--user", default="", help="limit to one user_email")
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), ".env"))

    from utils.db_connect import DBConnect
    from services import us_geo, job_inventory_service as inv

    db = DBConnect().get_db()
    query = {"status": {"$in": list(PURGEABLE)}}
    if args.user:
        query["user_email"] = args.user

    hits, kept = [], 0
    for row in db.applications.find(query):
        # Prefer the canonical posting's own geo; fall back to the URL.
        location, country = "", None
        job_id = row.get("posting_job_id")
        if job_id:
            doc = db.job_postings.find_one({"job_id": job_id}, {"locations": 1})
            locs = (doc or {}).get("locations") or []
            first = locs[0] if locs else {}
            if isinstance(first, dict):
                location, country = str(first.get("raw") or ""), first.get("country")
        if not location:
            location = _location_from_url_and_title(row)
        if not location:
            kept += 1        # nothing to judge on: leave it alone
            continue
        # Retire only on a POSITIVE foreign signal, not merely on failing the
        # feed's keep-test. The two bars are deliberately different: the feed
        # drops ambiguity because showing a possibly-foreign row is cheap to
        # get wrong, but this script RETIRES a real application, and killing a
        # genuine US application over an unresolvable "2 Locations" rollup is
        # the worse error. Ambiguous rows stay in the queue.
        if us_geo.keep_us(location, country) or not us_geo.has_foreign_marker(location):
            kept += 1
            continue
        hits.append((row, location))

    print(f"{len(hits)} non-US row(s) to retire; {kept} kept\n")
    for row, location in hits:
        print(f"  [{row.get('status'):14s}] {str(row.get('company'))[:24]:26s} "
              f"{str(row.get('title'))[:44]:46s} | {location[:34]}")

    if not args.apply:
        print("\nDRY RUN. Re-run with --apply to write.")
        return 0

    from services import application_ledger_service as als

    done, blocked = 0, 0
    for row, location in hits:
        # `skipped` is only reachable from a LIVE queue state — see
        # `_TRANSITION_GUARDS`. A row that already reached `failed` is terminal
        # and stays there; the scheduler's infra-retry has its own geo guard
        # (`_is_non_us`) so it will not resurrect it.
        if row.get("status") not in als._TRANSITION_GUARDS[als.STATUS_SKIPPED]:
            blocked += 1
            print(f"  - already terminal ({row.get('status')}), left as-is: "
                  f"{row.get('company')}")
            continue
        try:
            als.transition(row["user_email"], row["application_id"],
                           als.STATUS_SKIPPED, error="non_us_posting")
            done += 1
        except Exception as e:
            print(f"  ! {row.get('application_id')}: {type(e).__name__}: {e}")
    print(f"\nretired {done} row(s); {blocked} already terminal")
    _ = inv  # imported for the geo contract it documents; not called here
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
