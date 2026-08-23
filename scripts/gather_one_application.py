"""Run the auto-apply GATHER for one application and print what it resolved.

Recon + answer resolution only — this NEVER submits. Use it to inspect exactly
what would be sent before approving anything.

Usage:
    python3 scripts/gather_one_application.py <application_id> <user_email>
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "shared", "python"))

for line in open(os.path.join(ROOT, ".env"), encoding="utf-8-sig"):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    aid, user_email = sys.argv[1], sys.argv[2]

    from services import application_ledger_service as ledger
    app = ledger.get_application(user_email, aid)
    if not app:
        print(f"ERROR: no such application {aid}")
        return 1
    print(f"application : {aid}")
    print(f"company     : {app.get('company')}  |  {app.get('title')}")
    print(f"status      : {app.get('status')}")
    print(f"apply_url   : {app.get('apply_url')}")

    from services.resume_service import get_resume_service
    payload = {"application_id": aid, "user_email": user_email}
    job_id = get_resume_service().create_job("auto_apply_gather", payload, user_email)
    event = {"async_job": True, "job_id": job_id,
             "job_type": "auto_apply_gather", "payload": payload}
    print(f"\ngather job  : {job_id}\ninvoking...")

    import boto3
    from botocore.config import Config
    client = boto3.client("lambda", region_name="us-east-1",
                          config=Config(read_timeout=600, retries={"max_attempts": 0}))
    resp = client.invoke(FunctionName="jobpilot-resume-prod",
                         InvocationType="RequestResponse",
                         Payload=json.dumps(event).encode())
    print(f"lambda      : {resp['StatusCode']}  {resp['Payload'].read().decode()[:200]}")

    app = ledger.get_application(user_email, aid)
    print(f"\nstatus      : {app.get('status')}")
    print(f"autonomy    : {app.get('autonomy')}  confidence: {app.get('confidence_score')}")

    from utils.db_connect import DBConnect
    db = DBConnect().get_db()
    sheet = db.auto_apply_answers.find_one(
        {"application_id": aid, "user_email": user_email}, {"_id": 0}) or {}
    print(f"coverage    : {sheet.get('coverage')}")
    print("\n--- ANSWERS THAT WOULD BE SUBMITTED ---")
    for k, v in sorted((sheet.get("answers") or {}).items()):
        print(f"   {k:26} = {str(v)[:52]}")
    ni = app.get("needs_input") or []
    print(f"\n--- STILL ASKS ({len(ni)}) ---")
    for x in ni:
        print(f"   {json.dumps(x, default=str)[:220]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
