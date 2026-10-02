#!/usr/bin/env bash
# Pull-based deploy for the Oracle ingest box. Run as root by
# jobpilot-pull-deploy.timer every 2 minutes.
#
# Why pull: the EC2 box is deployed over SSM, which needs no inbound port and
# no key in CI. Off AWS the equivalent is the box FETCHING. CI pushes a
# commit to `deploy/ingest-runner` only after the offline suite is green for
# that SHA, so this branch is the test gate; nothing else ever writes it.
#
# Install goes through infra/ec2/install-ingest-runner.sh, the SAME installer
# the EC2 box runs, so the import checks and the "unit file is required"
# guard apply here too.
set -euo pipefail

SRC=/opt/jobpilot/src
BRANCH="${DEPLOY_BRANCH:-deploy/ingest-runner}"
STATE_DIR=/var/lib/jobpilot
DEPLOYED="$STATE_DIR/deployed-sha"
FAILED="$STATE_DIR/failed-sha"

exec 9>/run/jobpilot-pull-deploy.lock
flock -n 9 || exit 0
install -d "$STATE_DIR"

git -C "$SRC" fetch -q origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH"
NEW=$(git -C "$SRC" rev-parse "refs/remotes/origin/$BRANCH")

[ "$NEW" = "$(cat "$DEPLOYED" 2>/dev/null || true)" ] && exit 0
# A SHA that failed to install is not retried every 2 minutes; the next push
# is. CI's confirm step goes red because the heartbeat never reports it.
[ "$NEW" = "$(cat "$FAILED" 2>/dev/null || true)" ] && exit 0

echo "deploying ${NEW:0:12}"
git -C "$SRC" checkout -q --force --detach "$NEW"

install -D -m 0644 "$SRC/infra/oracle/ingest-runner.oracle.conf" \
  /etc/systemd/system/ingest-runner.service.d/oracle.conf
install -m 0644 "$SRC/infra/oracle/jobpilot-pull-deploy.service" \
  "$SRC/infra/oracle/jobpilot-pull-deploy.timer" /etc/systemd/system/

if APP_SRC="$SRC" REVISION="$NEW" bash "$SRC/infra/ec2/install-ingest-runner.sh"; then
  echo "$NEW" > "$DEPLOYED"
  rm -f "$FAILED"
  # The installer restarts the unit; on the first deploy it is not yet
  # enabled, and a reboot would then come back with nothing crawling.
  systemctl enable -q ingest-runner
  # Self-update last, so a broken deployer can never strand the box mid-install.
  install -m 0755 "$SRC/infra/oracle/pull-deploy.sh" /usr/local/sbin/jobpilot-pull-deploy
  echo "deployed ${NEW:0:12}"
else
  echo "$NEW" > "$FAILED"
  echo "FAILED to deploy ${NEW:0:12}; still running $(cat "$DEPLOYED" 2>/dev/null || echo nothing)" >&2
  exit 1
fi
