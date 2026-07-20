"""One-shot Guest Onboarding v1 → v2 identity/quota backfill.

Per docs/specs/guest-onboarding.md v2 §Migration/backfill:

  1. CLUSTER-PRESERVING: existing `merged_into` clusters are kept EXACTLY as
     they are — linkage is NEVER re-run on plaintext data (that would apply
     the old, weaker rules to data we're about to destroy).
  2. One `quota_subject_id` is minted per cluster (the canonical/root session
     keeps an existing subject id if it already has one) and stamped on every
     member session.
  3. Existing per-session `tailors_used` values SUM onto the subject's
     counter (`$max`-guarded so re-runs never inflate it; counters can never
     go negative — negatives clamp to 0). Sessions already at/over the new
     limit (used >= 1) simply have no remaining trial; the API renders the
     exhausted state gracefully.
  4. Plaintext identity arrays (`identity.emails/phones/resume_hashes`) are
     RE-KEYED into the v2 versioned HMAC fields ("v1:" + hex hmac_sha256):
       * emails/phones — HMAC of the stored normalized value;
       * resume_hashes — HMAC of the stored sha256 (the v2 live path signs
         `sha256(canonical_text)` too, so live and backfilled signals match);
     then `$unset` — no plaintext identity lookup data survives.

Fail-closed: GUEST_SIGNAL_HMAC_KEY MUST be configured, otherwise the script
refuses to run (we never destroy plaintext before it has been re-keyed, and
we never store unkeyed signals).

Idempotent + safe to re-run: already-migrated sessions (no plaintext arrays,
subject stamped) are no-ops.

Usage (from the repo root, with Mongo + the secret configured):
    python scripts/backfill_guest_identity_v2.py            # apply
    python scripts/backfill_guest_identity_v2.py --dry-run  # preview only
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

# Mirror local.py path wiring so `utils.*`/`services.*` resolve from the layer.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

import uuid  # noqa: E402

from utils.db_connect import DBConnect  # noqa: E402
from services import guest_identity_service as identity  # noqa: E402

_MAX_HOPS = 10


def _now():
    return datetime.now(timezone.utc)


def _root_id(doc, by_id):
    """Follow merged_into to the cluster root using the in-memory map only
    (cluster-preserving; NO re-resolution)."""
    current = doc
    for _ in range(_MAX_HOPS):
        target = current.get("merged_into")
        if not target or str(target) not in by_id:
            return str(current["guest_id"])
        nxt = by_id[str(target)]
        if nxt["guest_id"] == current["guest_id"]:
            return str(current["guest_id"])
        current = nxt
    return str(current["guest_id"])


def main():
    dry_run = "--dry-run" in sys.argv

    if identity.signal_key() is None:
        print(
            "REFUSING TO RUN: GUEST_SIGNAL_HMAC_KEY is not configured.\n"
            "The backfill re-keys plaintext identity arrays into keyed HMACs "
            "and then deletes the plaintext — without the key that would "
            "either store unkeyed signals or destroy data before re-keying.\n"
            "Set the secret (SSM SSM_GUEST_SIGNAL_HMAC_KEY / env "
            "GUEST_SIGNAL_HMAC_KEY) and re-run."
        )
        sys.exit(2)

    db = DBConnect().get_db()
    sessions = list(db.guest_sessions.find({}))
    by_id = {str(d["guest_id"]): d for d in sessions}
    print(f"== backfill_guest_identity_v2 ({'DRY RUN' if dry_run else 'APPLY'}) "
          f"on db '{db.name}': {len(sessions)} session(s) ==")

    # Group into clusters by (preserved) merged_into root.
    clusters = {}
    for doc in sessions:
        clusters.setdefault(_root_id(doc, by_id), []).append(doc)

    migrated_sessions = subjects_touched = 0
    for root_id, members in clusters.items():
        root = by_id[root_id]
        subject_id = str(root.get("quota_subject_id") or uuid.uuid4())
        total_tailors = 0
        last_seen = None
        for m in members:
            try:
                total_tailors += max(0, int(m.get("tailors_used") or 0))
            except (TypeError, ValueError):
                pass
            ls = m.get("last_seen_at")
            if ls is not None and (last_seen is None or ls > last_seen):
                last_seen = ls

        if dry_run:
            print(f"  [cluster] root={root_id} members={len(members)} "
                  f"subject={subject_id} tailors_used={total_tailors}")
        else:
            # $max keeps re-runs from inflating the counter; never negative.
            db.guest_quota_subjects.update_one(
                {"subject_id": subject_id},
                {
                    "$setOnInsert": {
                        "created_at": root.get("created_at") or _now(),
                        "reservation": None,
                        "matched_jobs": None,
                        "linked_accounts": [],
                    },
                    "$set": {"last_seen_at": last_seen or _now()},
                    "$max": {"tailors_used": total_tailors},
                },
                upsert=True,
            )
        subjects_touched += 1

        for m in members:
            ident = m.get("identity") or {}
            email_hmacs = sorted({
                h for h in (
                    [identity.signal_hmac(e) for e in (ident.get("emails") or []) if e]
                    + list(ident.get("email_hmacs") or [])
                ) if h
            })
            phone_hmacs = sorted({
                h for h in (
                    [identity.signal_hmac(p) for p in (ident.get("phones") or []) if p]
                    + list(ident.get("phone_hmacs") or [])
                ) if h
            })
            # v1 stored sha256(canonical text); v2 signs that same digest —
            # re-key the stored hash directly (no original text needed).
            sig_hmacs = sorted({
                h for h in (
                    [identity.signal_hmac(rh) for rh in (ident.get("resume_hashes") or []) if rh]
                    + list(ident.get("resume_sig_hmacs") or [])
                ) if h
            })

            if dry_run:
                migrated_sessions += 1
                continue
            db.guest_sessions.update_one(
                {"guest_id": m["guest_id"]},
                {
                    "$set": {
                        "quota_subject_id": subject_id,
                        "identity.email_hmacs": email_hmacs,
                        "identity.phone_hmacs": phone_hmacs,
                        "identity.resume_sig_hmacs": sig_hmacs,
                        "identity.sig_v": identity.SIGNAL_VERSION,
                    },
                    # Drop the plaintext arrays AND the per-session counter
                    # (quota now lives on the subject).
                    "$unset": {
                        "identity.emails": "",
                        "identity.phones": "",
                        "identity.resume_hashes": "",
                        "tailors_used": "",
                    },
                },
            )
            migrated_sessions += 1

    print(f"\nDone. clusters={len(clusters)} subjects={subjects_touched} "
          f"sessions={'would migrate ' if dry_run else 'migrated '}{migrated_sessions}"
          + ("  (dry run — nothing written)" if dry_run else ""))


if __name__ == "__main__":
    main()
