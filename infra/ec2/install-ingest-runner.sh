#!/usr/bin/env bash
# Runs ON the ingest box. Downloads the staged tarball, installs deps,
# IMPORT-CHECKS, and only then restarts the unit.
#
# One file, two callers: `deploy-ingest-runner.sh` (operator, from a laptop)
# and the `deploy-ingest-runner` job in `.github/workflows/deploy.yml` (CI, on
# push to main). They used to be the same script inlined in two places, which
# is how a deploy path drifts from the one people actually test.
#
# Executed as `bash install.sh`, never inlined into SSM's command list:
# AWS-RunShellScript runs under dash, where `set -o pipefail` and
# heredocs-inside-quoting are syntax errors.
set -xeuo pipefail

BUCKET="${DEPLOY_BUCKET:-jobpilot-resumes-prod-032398581420}"
KEY="${DEPLOY_KEY:-deploy/ingest-runner/app.tar.gz}"

install -d -o jobpilot -g jobpilot /opt/jobpilot/app /var/log/jobpilot

# boto3 resolves credentials from the instance profile; no keys on disk.
sudo -u jobpilot /opt/jobpilot/venv/bin/python - <<PY
import boto3
boto3.client("s3").download_file("${BUCKET}", "${KEY}", "/tmp/app.tar.gz")
PY
sudo -u jobpilot tar -xzf /tmp/app.tar.gz -C /opt/jobpilot/app
rm -f /tmp/app.tar.gz

# Install the SHARED requirements, not a hand-listed subset: the runner imports
# the same tree the Lambda layer ships, so guessing the subset is how you find
# a missing transitive dep at 3am on a box with no rollback.
sudo -u jobpilot /opt/jobpilot/venv/bin/pip install -q --upgrade \
  -r /opt/jobpilot/app/shared/python/requirements.txt

# Import check BEFORE systemd ever starts: a missing dep fails here, loudly,
# rather than as a restart loop under `Restart=always` while every EventBridge
# rule is already disabled -- i.e. nothing crawling at all.
cd /opt/jobpilot/app
sudo -u jobpilot \
  env PYTHONPATH=/opt/jobpilot/app/shared/python:/opt/jobpilot/app \
  /opt/jobpilot/venv/bin/python -c \
  'import services.ingest_runner.runner as r, services.job_ingest_worker as w; print("import OK; sources:", w.SOURCES)'

if [ "${NO_RESTART:-0}" != "1" ]; then
  # The unit file itself can change (new env, new dials), so reload before
  # restarting rather than assuming only code moved.
  if [ -f /opt/jobpilot/app/infra/ec2/ingest-runner.service ]; then
    install -m 0644 /opt/jobpilot/app/infra/ec2/ingest-runner.service \
      /etc/systemd/system/ingest-runner.service
  fi
  systemctl daemon-reload
  systemctl restart ingest-runner
  sleep 5
  systemctl is-active ingest-runner
fi
