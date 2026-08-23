"""Re-arm a row parked on an ACCOUNT wall, then re-run submit.

Context (Expedia, 2026-08-23): the first submit reached Workday's candidate
account gate and parked at `waiting_verify / account_required` WITHOUT
attempting signup. Cause: `browser_executor._workday_clear_account_gate` bails
on its first line when `managed_mailbox_service.can_receive_mail()` is False,
and APPLY_MAIL_DOMAIN / APPLY_MAILBOX_ENABLED were missing from the
jobpilot-apply-executor-prod container (they were only on the resume Lambda).
Fixed 03:37 UTC via scripts/fix_executor_mail_env.sh.

`resume_after_wall` does NOT help here: recheck_verify only OBSERVES whether a
wall cleared ("a single page load - no LLM, no submit"). Nobody cleared this
one; the executor must clear it itself, and that code only runs inside submit.

So: clear the needs_input queue (the wall is not a question the user can
answer), re-land ready_for_review, approve, and re-run submit.

This files a REAL application. Run deliberately.

Usage:
    python3 scripts/rearm_and_submit.py <application_id> <user_email>
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
    from services.auto_apply_service import build_approval_snapshot

    app = ledger.get_application(user_email, aid)
    if not app:
        print(f"ERROR: no such application {aid}")
        return 1
    print(f"application : {aid}")
    print(f"company     : {app.get('company')}  |  {app.get('title')}")
    print(f"status      : {app.get('status')}")

    status = app.get("status")

    # Only a row still parked in `needs_input` needs re-arming. After
    # `resume_after_answers` clears the queue the row is ALREADY in
    # `ready_for_review`, and `update_needs_input` rejects that state
    # ("cannot update needs_input from 'ready_for_review'").
    if status == "needs_input":
        ledger.update_needs_input(user_email, aid, [])
        ledger.mark_ready_for_review(user_email, aid, autonomy="hold",
                                     confidence_score=0.88,
                                     tailoring_record_id=None)
        status = ledger.get_application(user_email, aid).get("status")
        print(f"re-armed    : {status}")

    if status != "approved":
        res = ledger.approve_with_result(
            user_email, [aid], approved_by="owner-authorized-test",
            snapshot_provider=lambda x: build_approval_snapshot(user_email, x))
        print(f"approve     : {json.dumps(res, default=str)[:200]}")

    app = ledger.get_application(user_email, aid)
    if app.get("status") != "approved":
        print(f"REFUSING: status is {app.get('status')!r}, not 'approved'")
        return 1

    from services.resume_service import get_resume_service
    payload = {"application_id": aid, "user_email": user_email}
    job_id = get_resume_service().create_job("auto_apply_submit", payload, user_email)
    event = {"async_job": True, "job_id": job_id,
             "job_type": "auto_apply_submit", "payload": payload}
    print(f"\nsubmit job  : {job_id}")
    print("invoking (Workday signup + verify can take several minutes)...")

    import boto3
    from botocore.config import Config
    client = boto3.client("lambda", region_name="us-east-1",
                          config=Config(read_timeout=600, retries={"max_attempts": 0}))
    resp = client.invoke(FunctionName="jobpilot-resume-prod",
                         InvocationType="RequestResponse",
                         Payload=json.dumps(event).encode())
    print(f"lambda      : {resp['StatusCode']}  {resp['Payload'].read().decode()[:300]}")

    app = ledger.get_application(user_email, aid)
    print(f"\nfinal status: {app.get('status')}")
    for x in (app.get("needs_input") or []):
        print(f"  needs: {str(x)[:160]}")
    if app.get("error"):
        print(f"error       : {app.get('error')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
