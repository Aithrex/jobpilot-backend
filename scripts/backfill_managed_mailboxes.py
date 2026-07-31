"""Backfill the Managed Application Mailbox for EXISTING users (spec §6).

New signups now get a mailbox automatically (`auth.register` /
`oauth_service.upsert_user` call `managed_mailbox_service.provision`), and that
mailbox forwards to the user's OWN registered address, enabled + verified — the
user is never asked to create an address or pick a destination.

This script brings accounts created BEFORE that landed onto the same footing:

  1. PROVISION every active user who has no `managed_mailboxes` doc.
     `provision()` is idempotent per user (unique index on `user_email`) and is a
     pure DB operation, so re-running is a no-op. The readable local part is
     seeded server-side from `users.name`, falling back to the parsed resume's
     `contact.name` (the SAME resolution order the mailbox route uses — D7: never
     a name from a request body). With neither, an opaque `career.<hex>` stopgap
     is minted and flagged `name_pending`, which the mailbox route self-repairs
     once a name exists.
  2. FORWARDING DEFAULTS for mailboxes that already exist but were never
     configured (`forward_to` null/absent) -> forward to the owner's own
     registered address, enabled + verified. A user who DELIBERATELY chose a
     different destination is left completely alone (address AND verified state).

Deleted / soft-deleted accounts are skipped. Every step is wrapped so one bad
row never aborts the run. Nothing here sends email.

Requires a LIVE MongoDB connection (singleton `utils.db_connect.DBConnect`).

Usage (from the repo root):
    python3 scripts/backfill_managed_mailboxes.py            # apply
    python3 scripts/backfill_managed_mailboxes.py --dry-run  # preview only
"""
import sys
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

from utils.db_connect import DBConnect  # noqa: E402
from utils.security import InputSanitizer  # noqa: E402
from services import managed_mailbox_service as mailbox  # noqa: E402


def _display_name(db, email):
    """Server-side name resolution — same order as
    `resume/blueprints/mailbox.py::_resolve_display_name`: the users doc, then
    the active parsed resume's contact.name. None -> opaque stopgap alias."""
    user = db.users.find_one({"email": email}, {"name": 1, "_id": 0}) or {}
    name = str(user.get("name") or "").strip()
    if name:
        return name
    try:
        from services import resume_access
        structured = (resume_access.get_active_structured(email) or {}).get("structured") or {}
        name = str((structured.get("contact") or {}).get("name") or "").strip()
        if name:
            return name
    except Exception:
        pass
    return None


def main():
    dry_run = "--dry-run" in sys.argv
    db = DBConnect().get_db()

    if not mailbox.apply_mail_domain():
        print("WARNING: APPLY_MAIL_DOMAIN is unset — minted addresses will have "
              "no host. Set it before running against prod.\n")

    # --- 1. Provision the missing mailboxes. --------------------------------
    have = {d.get("user_email") for d in db.managed_mailboxes.find({}, {"user_email": 1, "_id": 0})}
    scanned = provisioned = skipped = failed = 0

    for u in db.users.find({}, {"email": 1, "is_deleted": 1, "status": 1, "_id": 0}):
        email = InputSanitizer.sanitize_email(u.get("email"))
        if not email:
            continue
        scanned += 1
        if u.get("is_deleted") or str(u.get("status") or "active").lower() == "deleted":
            skipped += 1
            continue
        if email in have:
            skipped += 1
            continue
        if dry_run:
            provisioned += 1
            continue
        try:
            mailbox.provision(email, _display_name(db, email))
            provisioned += 1
        except Exception as e:
            failed += 1
            print(f"  FAILED provision for a user: {type(e).__name__}: {e}")

    print(f"provision: users={scanned} "
          f"{'would provision ' if dry_run else 'provisioned '}{provisioned} "
          f"skipped={skipped} failed={failed}")

    # --- 2. Forwarding defaults on pre-existing, never-configured mailboxes. --
    counts = mailbox.backfill_forwarding_defaults(dry_run=dry_run)
    print(f"forwarding defaults: scanned={counts['scanned']} "
          f"{'would update ' if dry_run else 'updated '}{counts['updated']}")

    print("\nDone." + ("  (dry run — nothing written)" if dry_run else ""))


if __name__ == "__main__":
    main()
