"""One-time, idempotent retrofit migration for in-house auth + freemium.

Brings the existing `users` collection (inherited from the portfolio monorepo)
up to the schema the new /api/auth + entitlements code expects, and creates the
foundation indexes — WITHOUT breaking any current login.

Safety guarantees:
  * Additive only. Every write is `$exists:false` guarded or `$addToSet` /
    `$setOnInsert`, so re-running is a no-op.
  * NEVER unsets password_hash and NEVER rewrites an email. Existing bcrypt
    hashes keep verifying; with the same JWT_SECRET_KEY, existing sessions stay
    valid → zero forced re-logins.
  * Pre-flight aborts on case-duplicate emails (e.g. Foo@x.com vs foo@x.com)
    BEFORE creating the unique email index — those must be resolved by hand,
    never auto-merged.

Usage:
    python scripts/migrate_users_v2.py            # audit + apply (idempotent)
    python scripts/migrate_users_v2.py --dry-run  # audit + show plan, no writes
"""
import sys
from pathlib import Path

# Mirror local.py path wiring so `utils.*` / `models.*` resolve.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from datetime import datetime, timezone  # noqa: E402
from pymongo import ASCENDING  # noqa: E402
from utils.db_connect import DBConnect  # noqa: E402

ADMIN_EMAILS = ["harshith@aithrex.tech", "mannesiddardha@gmail.com"]
OAUTH_PROVIDERS = ["google", "github", "linkedin", "microsoft", "apple"]


def _now():
    return datetime.now(timezone.utc)


def audit_case_duplicate_emails(db) -> list:
    """Return groups of emails that collide case-insensitively."""
    pipeline = [
        {"$match": {"email": {"$type": "string"}}},
        {"$group": {"_id": {"$toLower": "$email"}, "emails": {"$addToSet": "$email"}, "n": {"$sum": 1}}},
        {"$match": {"n": {"$gt": 1}}},
    ]
    return [row for row in db.users.aggregate(pipeline)]


def backfill(db, dry_run: bool):
    actions = []

    # 1. plan + plan_since
    flt = {"plan": {"$exists": False}}
    n = db.users.count_documents(flt)
    actions.append(f"set plan='free'+plan_since on {n} users")
    if not dry_run and n:
        db.users.update_many(flt, {"$set": {"plan": "free", "plan_since": _now()}})

    # 2. status
    flt = {"status": {"$exists": False}}
    n = db.users.count_documents(flt)
    actions.append(f"set status='active' on {n} users")
    if not dry_run and n:
        db.users.update_many(flt, {"$set": {"status": "active"}})

    # 3. auth_providers + email_verified for existing password users
    flt = {"password_hash": {"$exists": True}, "auth_providers": {"$exists": False}}
    n = db.users.count_documents(flt)
    actions.append(f"set auth_providers=['password']+email_verified=true on {n} password users")
    if not dry_run and n:
        db.users.update_many(flt, {"$set": {"auth_providers": ["password"], "email_verified": True}})

    # 4. created_at where missing
    flt = {"created_at": {"$exists": False}}
    n = db.users.count_documents(flt)
    actions.append(f"set created_at (now) on {n} users")
    if not dry_run and n:
        db.users.update_many(flt, {"$currentDate": {"created_at": True}})

    # 5. is_admin for seed admins (only affects existing accounts)
    flt = {"email": {"$in": ADMIN_EMAILS}}
    n = db.users.count_documents(flt)
    actions.append(f"set is_admin=true on {n} existing admin account(s)")
    if not dry_run and n:
        db.users.update_many(flt, {"$set": {"is_admin": True}})

    # 6. user_preferences upsert per user (onboarding_complete=false)
    created_prefs = 0
    cursor = db.users.find({}, {"email": 1})
    for u in cursor:
        email = u.get("email")
        if not email:
            continue
        if db.user_preferences.count_documents({"user_email": email}, limit=1) == 0:
            created_prefs += 1
            if not dry_run:
                db.user_preferences.update_one(
                    {"user_email": email},
                    {"$setOnInsert": {"user_email": email, "onboarding_complete": False, "created_at": _now()}},
                    upsert=True,
                )
    actions.append(f"create user_preferences for {created_prefs} users without one")

    return actions


def create_indexes(db, dry_run: bool):
    specs = [
        ("users", [("email", ASCENDING)], {"unique": True, "name": "email_unique"}),
        ("users", [("plan", ASCENDING)], {"name": "plan_idx"}),
        ("users", [("status", ASCENDING)], {"name": "status_idx"}),
        ("user_preferences", [("user_email", ASCENDING)], {"unique": True, "name": "user_email_unique"}),
        ("password_reset_tokens", [("token_hash", ASCENDING)], {"unique": True, "name": "token_hash_unique"}),
        ("password_reset_tokens", [("expires_at", ASCENDING)], {"expireAfterSeconds": 0, "name": "ttl_expires"}),
        ("oauth_login_states", [("state", ASCENDING)], {"unique": True, "name": "state_unique"}),
        ("oauth_login_states", [("expires_at", ASCENDING)], {"expireAfterSeconds": 0, "name": "ttl_expires"}),
        ("feature_usage", [("user_email", ASCENDING), ("date", ASCENDING), ("feature", ASCENDING)],
         {"unique": True, "name": "user_date_feature_unique"}),
    ]
    # Sparse per-provider OAuth subject indexes.
    for p in OAUTH_PROVIDERS:
        specs.append(("users", [(f"oauth_identities.{p}.subject", ASCENDING)],
                      {"sparse": True, "name": f"oauth_{p}_subject"}))

    for coll, keys, opts in specs:
        label = f"{coll}: {opts.get('name')}"
        if dry_run:
            print(f"  [index] would ensure {label}")
            continue
        try:
            db[coll].create_index(keys, **opts)
            print(f"  [index] ensured {label}")
        except Exception as e:
            print(f"  [index] FAILED {label}: {e}")


def main():
    dry_run = "--dry-run" in sys.argv
    db = DBConnect().get_db()
    print(f"== migrate_users_v2 ({'DRY RUN' if dry_run else 'APPLY'}) on db '{db.name}' ==")

    dups = audit_case_duplicate_emails(db)
    if dups:
        print("\n!! ABORT: case-duplicate emails found. Resolve these by hand before migrating:")
        for d in dups:
            print(f"   {d['_id']}: {d['emails']}")
        sys.exit(1)
    print("Pre-flight OK: no case-duplicate emails.")

    print("\nBackfill:")
    for a in backfill(db, dry_run):
        print(f"  - {a}")

    print("\nIndexes:")
    create_indexes(db, dry_run)

    print("\nDone." + ("  (dry run — no writes performed)" if dry_run else ""))


if __name__ == "__main__":
    main()
