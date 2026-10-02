#!/usr/bin/env bash
# One-time setup of the Oracle Cloud Always Free ingest box.
# Ubuntu 24.04 (aarch64) on VM.Standard.A1.Flex. Run as root; safe to re-run.
#
#   sudo bash bootstrap.sh
#
# First run stops after printing the deploy key: add it to the GitHub repo
# (read-only), fill /etc/jobpilot/ingest-runner.env, then run it again.
# See README.md for the whole cutover.
set -xeuo pipefail

REPO="${REPO:-git@github.com:Aithrex/jobpilot-backend.git}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KEY=/root/.ssh/jobpilot_deploy
ENV_FILE=/etc/jobpilot/ingest-runner.env

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3-pip python3-venv git

# Same user, paths and venv as the EC2 box, so infra/ec2/install-ingest-runner.sh
# and infra/ec2/ingest-runner.service run here unmodified.
id -u jobpilot &>/dev/null || useradd -m -s /bin/bash jobpilot
install -d -o jobpilot -g jobpilot /opt/jobpilot /opt/jobpilot/app /var/log/jobpilot
[ -x /opt/jobpilot/venv/bin/python ] || sudo -u jobpilot python3 -m venv /opt/jobpilot/venv
sudo -u jobpilot /opt/jobpilot/venv/bin/pip install -q --upgrade pip

# Secrets: root-owned, readable by the service group only.
install -d -m 0750 -o root -g jobpilot /etc/jobpilot
[ -f "$ENV_FILE" ] || install -m 0640 -o root -g jobpilot "$HERE/ingest-runner.env.example" "$ENV_FILE"

# Read-only deploy key for the private repo.
if [ ! -f "$KEY" ]; then
  install -d -m 0700 /root/.ssh
  ssh-keygen -t ed25519 -N "" -C "jobpilot-ingest-oracle" -f "$KEY"
fi

export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
if [ ! -d /opt/jobpilot/src/.git ]; then
  if ! git clone -q "$REPO" /opt/jobpilot/src; then
    set +x
    echo
    echo "=== Add this READ-ONLY deploy key to the repo, then re-run: ==="
    cat "$KEY.pub"
    echo "  gh repo deploy-key add $KEY.pub --repo Aithrex/jobpilot-backend --title oracle-ingest"
    exit 2
  fi
fi

if grep -qE '^MONGODB_URI=$' "$ENV_FILE"; then
  set +x
  echo "=== Fill $ENV_FILE (MONGODB_URI and the S3 keys), then re-run. ==="
  exit 3
fi

install -m 0755 "$HERE/pull-deploy.sh" /usr/local/sbin/jobpilot-pull-deploy
install -m 0644 "$HERE/jobpilot-pull-deploy.service" "$HERE/jobpilot-pull-deploy.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now jobpilot-pull-deploy.timer

set +x
echo "Bootstrap done. The first deploy happens once CI pushes deploy/ingest-runner."
echo "Watch it with:  journalctl -fu jobpilot-pull-deploy -u ingest-runner"
