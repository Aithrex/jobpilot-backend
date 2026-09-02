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
# Only what the runner imports: the shared layer and its own package. NOT the
# other services — a crawler box has no business holding the auth blueprints.
tar -czf "$TMP/app.tar.gz" \
  -C "$HERE" \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='.git' \
  shared/python services/ingest_runner

SIZE=$(du -h "$TMP/app.tar.gz" | cut -f1)
echo "==> uploading $SIZE to s3://$BUCKET/$KEY"
aws s3 cp "$TMP/app.tar.gz" "s3://$BUCKET/$KEY" \
  --profile "$PROFILE" --region "$REGION" --only-show-errors

cat > "$TMP/install.sh" <<'REMOTE'
#!/usr/bin/env bash
# Runs on the box. A FILE executed by bash, never inlined into SSM's command
# list: AWS-RunShellScript executes under dash, where `set -o pipefail` and
# heredocs-inside-quoting are syntax errors (the executor userdata carries the
# same warning). Uploading the script and running `bash install.sh` sidesteps
# the whole dialect problem.
set -xeuo pipefail
install -d -o jobpilot -g jobpilot /opt/jobpilot/app /var/log/jobpilot

# boto3 resolves credentials from the instance profile; no keys on disk.
sudo -u jobpilot /opt/jobpilot/venv/bin/python - <<'PY'
import boto3
boto3.client("s3").download_file(
    "jobpilot-resumes-prod-032398581420",
    "deploy/ingest-runner/app.tar.gz",
    "/tmp/app.tar.gz")
PY
sudo -u jobpilot tar -xzf /tmp/app.tar.gz -C /opt/jobpilot/app
rm -f /tmp/app.tar.gz

# Install the SHARED requirements, not a hand-listed subset: the runner imports
# the same tree the Lambda layer ships, so guessing the subset is how you find
# a missing transitive dep at 3am on a box with no rollback.
sudo -u jobpilot /opt/jobpilot/venv/bin/pip install -q --upgrade \
  -r /opt/jobpilot/app/shared/python/requirements.txt

# Import check BEFORE systemd ever starts: a missing dep fails here, loudly,
# rather than as a restart loop.
cd /opt/jobpilot/app
sudo -u jobpilot \
  env PYTHONPATH=/opt/jobpilot/app/shared/python:/opt/jobpilot/app \
  /opt/jobpilot/venv/bin/python -c \
  'import services.ingest_runner.runner as r, services.job_ingest_worker as w; print("import OK; sources:", w.SOURCES)'
REMOTE

if [ "$RESTART" = "1" ]; then
cat >> "$TMP/install.sh" <<'REMOTE'
systemctl daemon-reload
systemctl restart ingest-runner
sleep 5
systemctl is-active ingest-runner
REMOTE
fi

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
