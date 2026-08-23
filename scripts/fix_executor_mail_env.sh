#!/bin/bash
# Add APPLY_MAIL_DOMAIN + APPLY_MAILBOX_ENABLED to the apply-executor Lambda.
#
# WHY: browser_executor._workday_clear_account_gate refuses to register an
# employer account unless managed_mailbox_service.can_receive_mail() is True —
# correctly, since registering an account whose verification mail we cannot read
# strands a half-created account on a real employer's portal.
#
# can_receive_mail() = mailbox_enabled() AND apply_mail_domain(), both read from
# env. jobpilot-resume-prod has them; jobpilot-apply-executor-prod does NOT, and
# they are not in SSM either. So inside the executor container the check is
# always False and the Workday account gate bails at its first line — which is
# exactly why the Expedia submit parked on `waiting_verify / account_required`
# with no signup attempted and no verification mail ever sent.
#
# This MERGES the two keys into the existing environment; nothing is dropped.
# Same class of gap MORNING_HANDOFF.md already flagged for the auth Lambda.
#
# NOTE: CI deploys code via update-function-code / publish-layer-version and
# bypasses Terraform, so Terraform's state does not know about this. Mirror it
# into the lambda.tf `for_each` env block when the Terraform drift is repaired.
set -euo pipefail
export AWS_PROFILE="${AWS_PROFILE:-aithrex}"
REGION=us-east-1
FN=jobpilot-apply-executor-prod
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo "reading current environment..."
aws lambda get-function-configuration --region "$REGION" --function-name "$FN" \
  --query 'Environment.Variables' --output json > "$TMP/cur.json"

python3 - "$TMP" <<'PY'
import json, sys
tmp = sys.argv[1]
d = json.load(open(f"{tmp}/cur.json"))
before = sorted(d)
d["APPLY_MAIL_DOMAIN"] = "my-private-mail.com"
d["APPLY_MAILBOX_ENABLED"] = "true"
json.dump({"Variables": d}, open(f"{tmp}/new.json", "w"))
print("  before:", before)
print("  adding: APPLY_MAIL_DOMAIN, APPLY_MAILBOX_ENABLED")
PY

echo "updating..."
aws lambda update-function-configuration --region "$REGION" --function-name "$FN" \
  --environment "file://$TMP/new.json" \
  --query '[LastModified,LastUpdateStatus]' --output text

echo "waiting for update to settle..."
aws lambda wait function-updated --region "$REGION" --function-name "$FN"

echo "verifying:"
aws lambda get-function-configuration --region "$REGION" --function-name "$FN" \
  --query 'Environment.Variables.[APPLY_MAIL_DOMAIN,APPLY_MAILBOX_ENABLED]' --output text
