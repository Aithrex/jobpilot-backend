"""Move the ATS credential vault off AWS KMS, BEFORE the AWS account goes.

Reads each `ats_credentials` row still holding a v1 (KMS) blob, opens it with
KMS, re-seals it with CREDENTIALS_MASTER_KEY, proves the new blob opens to the
same password, then writes it. Prints counts only: never a password, never an
email. Safe to re-run: rows already at v2 are skipped.

    python scripts/rewrap_ats_credentials.py            # dry run, writes nothing
    python scripts/rewrap_ats_credentials.py --apply

Needs: MONGODB_URI, CREDENTIALS_KMS_KEY_ID, CREDENTIALS_MASTER_KEY, and AWS
credentials allowed kms:Decrypt on the key.
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared" / "python"))

from pymongo import MongoClient

from services.ats_credential_service import _context
from services.kms_cipher import KmsCipherError, KmsEnvelopeCipher, LocalEnvelopeCipher


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    coll = MongoClient(os.environ["MONGODB_URI"])[os.getenv("DB_NAME", "aspirely_db")]["ats_credentials"]
    kms, local = KmsEnvelopeCipher(), LocalEnvelopeCipher()
    query = {"password_blob.v": 1}
    print(f"rows={coll.estimated_document_count()} on_kms={coll.count_documents(query)}")

    done = failed = 0
    for doc in coll.find(query):
        ctx = _context(doc["user_email"], doc["ats_family"], doc.get("tenant", ""))
        try:
            password = kms.decrypt(doc["password_blob"], context=ctx)
            blob = local.encrypt(password, context=ctx)
            if local.decrypt(blob, context=ctx) != password:
                raise KmsCipherError("round trip mismatch")
        except KmsCipherError:
            failed += 1
            print(f"FAILED row {doc['_id']}")
            continue
        finally:
            password = None
        if args.apply:
            coll.update_one({"_id": doc["_id"], "password_blob.v": 1}, {"$set": {"password_blob": blob}})
        done += 1

    print(f"{'rewrapped' if args.apply else 'would rewrap'}={done} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
