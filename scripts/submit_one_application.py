"""Enqueue + dispatch the auto-apply SUBMIT job for ONE application.

This is the step that files a REAL application with a REAL employer. It is
deliberately a separate, manually-run script rather than anything automatic.

Preconditions (all verified for the Expedia run on 2026-08-22):
  * the ledger row is in `approved`
  * the approval snapshot is frozen (application_hash / answers_fingerprint)
  * `_resume_ref` resolves to a real résumé asset
  * the work-auth answers resolve from DECLARED profile data, not inference

Usage:
    python3 scripts/submit_one_application.py <application_id> <user_email>
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
    application_id, user_email = sys.argv[1], sys.argv[2]

    from services import application_ledger_service as ledger
    app = ledger.get_application(user_email, application_id)
    if not app:
        print(f"ERROR: no such application {application_id}")
        return 1
    status = app.get("status")
    print(f"application : {application_id}")
    print(f"company     : {app.get('company')}")
    print(f"title       : {app.get('title')}")
    print(f"status      : {status}")
    if status != "approved":
        print(f"REFUSING: status must be 'approved' to submit, not {status!r}")
        return 1

    from services.resume_service import get_resume_service
    payload = {"application_id": application_id, "user_email": user_email}
    job_id = get_resume_service().create_job("auto_apply_submit", payload, user_email)
    event = {"async_job": True, "job_id": job_id,
             "job_type": "auto_apply_submit", "payload": payload}
    print(f"\nsubmit job  : {job_id}")

    import boto3
    client = boto3.client("lambda", region_name="us-east-1")
    resp = client.invoke(
        FunctionName="jobpilot-resume-prod",
        InvocationType="RequestResponse",
        Payload=json.dumps(event).encode(),
    )
    body = resp["Payload"].read().decode()
    print(f"lambda      : {resp['StatusCode']}  {body[:400]}")

    app = ledger.get_application(user_email, application_id)
    print(f"\nfinal status: {app.get('status')}")
    if app.get("error"):
        print(f"error       : {app.get('error')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
