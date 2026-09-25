#!/usr/bin/env bash
# Ship the ingest runner to the EC2 box and restart it.
#
# Uses only permissions the instance profile ALREADY has (see
# `jobpilot-executor-access`): s3:GetObject on the resumes bucket, SSM for the
# remote command, SSM parameters + KMS for secrets. Nothing new to provision.
#
#   ./deploy-ingest-runner.sh                 # deploy + restart
#   ./deploy-ingest-runner.sh --no-restart    # stage only
set -euo pipefail

PROFILE="${AWS_PROFILE:-aithrex}"
REGION="${AWS_REGION:-us-east-1}"
INSTANCE="${INGEST_INSTANCE_ID:-i-06bf0e03e60d9d409}"
BUCKET="${DEPLOY_BUCKET:-jobpilot-resumes-prod-032398581420}"
KEY="deploy/ingest-runner/app.tar.gz"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RESTART=1
[ "${1:-}" = "--no-restart" ] && RESTART=0

echo "==> packaging from $HERE"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
# What the runner imports - the shared layer, its own package, and the scripts
# its daily coverage job imports - plus its own systemd unit. NOT the other
# services: a crawler box has no business holding the auth blueprints. Keep in
# step with the `deploy-ingest-runner` job in .github/workflows/deploy.yml.
tar -czf "$TMP/app.tar.gz" \
  -C "$HERE" \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='.git' \
  shared/python services/ingest_runner infra/ec2 scripts

SIZE=$(du -h "$TMP/app.tar.gz" | cut -f1)
echo "==> uploading $SIZE to s3://$BUCKET/$KEY"
aws s3 cp "$TMP/app.tar.gz" "s3://$BUCKET/$KEY" \
  --profile "$PROFILE" --region "$REGION" --only-show-errors

# ONE installer, shared with the `deploy-ingest-runner` job in
# .github/workflows/deploy.yml. It used to be inlined here and nowhere else,
# so CI had to reimplement it -- which is how the path an operator tests and
# the path that runs on every push quietly stop being the same thing.
cp "$HERE/infra/ec2/install-ingest-runner.sh" "$TMP/install.sh"
[ "$RESTART" = "1" ] || sed -i '1a export NO_RESTART=1' "$TMP/install.sh"

echo "==> uploading installer"
aws s3 cp "$TMP/install.sh" "s3://$BUCKET/deploy/ingest-runner/install.sh" \
  --profile "$PROFILE" --region "$REGION" --only-show-errors

echo "==> installing on $INSTANCE via SSM"
cat > "$TMP/params.json" <<JSON
{"commands":[
  "set -e",
  "/opt/jobpilot/venv/bin/python -c \"import boto3;boto3.client('s3').download_file('$BUCKET','deploy/ingest-runner/install.sh','/tmp/install.sh')\"",
  "bash /tmp/install.sh"
]}
JSON

ID=$(aws ssm send-command --profile "$PROFILE" --region "$REGION" \
  --instance-ids "$INSTANCE" \
  --document-name AWS-RunShellScript \
  --comment "deploy jobpilot ingest runner" \
  --parameters "file://$TMP/params.json" \
  --query 'Command.CommandId' --output text)
echo "==> SSM command $ID"

for _ in $(seq 1 90); do
  STATUS=$(aws ssm get-command-invocation --profile "$PROFILE" --region "$REGION" \
    --command-id "$ID" --instance-id "$INSTANCE" --query Status --output text 2>/dev/null || echo Pending)
  case "$STATUS" in
    Success) echo "==> Success"; break ;;
    Failed|Cancelled|TimedOut) echo "==> $STATUS"; break ;;
  esac
  sleep 5
done
aws ssm get-command-invocation --profile "$PROFILE" --region "$REGION" \
  --command-id "$ID" --instance-id "$INSTANCE" \
  --query '{Status:Status,Out:StandardOutputContent,Err:StandardErrorContent}' --output json | tail -60
