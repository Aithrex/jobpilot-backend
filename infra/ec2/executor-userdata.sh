#!/bin/bash
# JobPilot browser-executor bootstrap (Ubuntu 24.04, t3.small).
# Installs Playwright + Chromium and prepares the PERSISTENT profile directory
# that /var/lib/jobpilot/profiles holds — the thing Lambda structurally could
# not give us, and the main reason this box exists.
set -xeuo pipefail
# Plain redirection, NOT `exec > >(tee ...)`: process substitution is a
# bashism and SSM AWS-RunShellScript executes under dash, where it is a
# syntax error before the first real line runs.
exec >> /var/log/jobpilot-bootstrap.log 2>&1

# 2 GB swap — insurance so a heavy Workday SPA cannot OOM a 2 GB box.
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
# NOTE: no `awscli` here. Ubuntu 24.04 dropped it from the default apt
# sources, and with `set -e` that one missing package aborted the whole
# bootstrap. The executor talks to AWS through boto3 (pip), not the CLI.
apt-get install -y python3-pip python3-venv git jq unzip fonts-liberation

# Dedicated unprivileged user; browser profiles persist on disk under /var/lib.
id -u jobpilot &>/dev/null || useradd -m -s /bin/bash jobpilot
mkdir -p /opt/jobpilot /var/lib/jobpilot/profiles
chown -R jobpilot:jobpilot /opt/jobpilot /var/lib/jobpilot

sudo -u jobpilot python3 -m venv /opt/jobpilot/venv
sudo -u jobpilot /opt/jobpilot/venv/bin/pip install --upgrade pip
sudo -u jobpilot /opt/jobpilot/venv/bin/pip install playwright pymongo boto3 requests
# Chromium OS deps install apt packages, so this half runs as root.
/opt/jobpilot/venv/bin/playwright install-deps chromium
sudo -u jobpilot /opt/jobpilot/venv/bin/playwright install chromium

touch /var/lib/jobpilot/BOOTSTRAP_OK
echo "bootstrap complete: $(date -u +%FT%TZ)"
