#!/usr/bin/env bash
# Runs ON the ingest box (Oracle Cloud, via infra/oracle/pull-deploy.sh).
# Packages the four trees from a git checkout, installs deps, IMPORT-CHECKS,
# and only then restarts the unit.
#
# It lived under infra/ec2/ when the crawl ran on EC2 and was deployed over
# SSM; that box was terminated 2026-10-02, and this is the one installer that
# survived it. Kept as one file so the path an operator tests is the path the
# deploy timer runs.
set -xeuo pipefail

: "${APP_SRC:?APP_SRC must point at a git checkout (see infra/oracle/pull-deploy.sh)}"

install -d -o jobpilot -g jobpilot /opt/jobpilot/app /var/log/jobpilot

# SAME four trees the unit and the import checks below expect.
tar -czf /tmp/app.tar.gz -C "$APP_SRC" \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='.git' \
  shared/python services/ingest_runner infra/ec2 scripts
chown jobpilot /tmp/app.tar.gz
sudo -u jobpilot tar -xzf /tmp/app.tar.gz -C /opt/jobpilot/app
rm -f /tmp/app.tar.gz
# Read by the runner into its heartbeat, which is how a deploy is confirmed.
if [ -n "${REVISION:-}" ]; then
  echo "$REVISION" | sudo -u jobpilot tee /opt/jobpilot/app/REVISION >/dev/null
fi

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

# The 05:45 coverage job imports this from scripts/. Checked here because the
# job catches its own exceptions: a missing module failed it every morning for
# three weeks without once failing a deploy.
sudo -u jobpilot \
  env PYTHONPATH=/opt/jobpilot/app/shared/python:/opt/jobpilot/app:/opt/jobpilot/app/scripts \
  /opt/jobpilot/venv/bin/python -c 'import employer_coverage_report; print("coverage report import OK")'

if [ "${NO_RESTART:-0}" != "1" ]; then
  # The unit file itself can change (new env, new dials), so reload before
  # restarting rather than assuming only code moved.
  #
  # REQUIRED, not `if [ -f ]`. The conditional used to skip silently because
  # the tarball never contained infra/ec2, so the box ran the cutover unit -
  # with `--no-maintenance` - from 2026-09-02 until 2026-09-24 while every
  # deploy reported success. A missing unit is now a failed deploy.
  UNIT=/opt/jobpilot/app/infra/ec2/ingest-runner.service
  if [ ! -f "$UNIT" ]; then
    echo "FATAL: $UNIT missing from the package - refusing to restart on a stale unit" >&2
    exit 1
  fi
  install -m 0644 "$UNIT" /etc/systemd/system/ingest-runner.service
  systemctl daemon-reload
  systemctl restart ingest-runner
  sleep 5
  systemctl is-active ingest-runner
fi
