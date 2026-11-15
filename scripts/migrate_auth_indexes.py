"""Sign-in indexes: make each provider subject UNIQUE, and index the codes.

    python scripts/migrate_auth_indexes.py            # dry run: prints the plan
    python scripts/migrate_auth_indexes.py --apply    # owner only, on production

Why: `users.oauth_identities.<provider>.subject` was indexed sparse but not
unique, so nothing in the database stopped two accounts from holding the same
Google identity (a double-clicked first sign-in could create both, and which
one later sign-ins reached was arbitrary).

What --apply does, per provider:
  1. looks for subjects held by more than one account and STOPS that provider
     if it finds any (they are printed; merging accounts is a human decision);
  2. drops the old non-unique index `oauth_<provider>_subject`;
  3. creates `oauth_<provider>_subject_unique` (unique + sparse).
Between 2 and 3 the lookup is unindexed for a moment; `users` is small.

It also ensures the two `auth_email_codes` indexes. The app creates those
itself on first use, so this is belt and braces.

Reads and writes the database named by `.env`. On this machine that is
PRODUCTION: the dry run only reads; --apply is for the owner to run.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

PROVIDERS = ("google", "github", "linkedin", "microsoft", "apple")


def duplicates(users, field: str) -> list:
    return list(users.aggregate([
        {"$match": {field: {"$type": "string"}}},
        {"$group": {"_id": f"${field}", "n": {"$sum": 1}, "emails": {"$push": "$email"}}},
        {"$match": {"n": {"$gt": 1}}},
    ]))


def main() -> int:
    apply = "--apply" in sys.argv[1:]
    from utils.db_connect import DBConnect
    db = DBConnect().get_db()
    users = db.users
    print(f"database: {db.name}   mode: {'APPLY' if apply else 'dry run'}")

    existing = {ix["name"]: ix for ix in users.list_indexes()}
    blocked = 0
    for provider in PROVIDERS:
        field = f"oauth_identities.{provider}.subject"
        old, new = f"oauth_{provider}_subject", f"oauth_{provider}_subject_unique"
        linked = users.count_documents({field: {"$type": "string"}})
        dups = duplicates(users, field)
        if dups:
            blocked += 1
            print(f"  {provider}: {linked} linked, {len(dups)} subject(s) on more than one account - NOT changed")
            for d in dups:
                print(f"      subject ...{str(d['_id'])[-6:]} -> {', '.join(str(e) for e in d['emails'])}")
            continue
        if new in existing:
            print(f"  {provider}: {linked} linked, unique index already present")
            continue
        print(f"  {provider}: {linked} linked, no duplicates -> "
              f"{'drop ' + old + ', ' if old in existing else ''}create {new}")
        if apply:
            if old in existing:
                users.drop_index(old)
            users.create_index([(field, 1)], name=new, unique=True, sparse=True)

    codes = db.auth_email_codes
    print("  auth_email_codes: ttl_expires (expires_at), email_created (email, created_at desc)")
    if apply:
        codes.create_index("expires_at", expireAfterSeconds=0, name="ttl_expires")
        codes.create_index([("email", 1), ("created_at", -1)], name="email_created")

    if not apply:
        print("dry run: nothing was changed. Re-run with --apply.")
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
