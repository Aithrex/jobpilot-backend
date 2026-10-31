#!/usr/bin/env bash
# Publish the frontend build from the `deploy/web` branch, which CI writes only
# after the suite passes. New hashed assets land first and index.html is
# swapped last, so a visitor never gets a page whose files are missing, and
# assets from recent builds stay for tabs that are already open.
set -euo pipefail

REPO=/opt/jobpilot/web-src
WEB=/srv/jobpilot/web
STATE=/var/lib/jobpilot/web-sha
export GIT_SSH_COMMAND="ssh -i /root/.ssh/jobpilot_web_deploy -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

[ -d "$REPO/.git" ] || git clone -q --depth 1 --branch deploy/web \
  git@github.com:Aithrex/jobpilot-frontend.git "$REPO"
git -C "$REPO" fetch -q --depth 1 origin deploy/web
NEW=$(git -C "$REPO" rev-parse FETCH_HEAD)
[ "$NEW" = "$(cat "$STATE" 2>/dev/null || true)" ] && exit 0

git -C "$REPO" checkout -q --force --detach FETCH_HEAD
rsync -rlt --chmod=D755,F644 --exclude .git --exclude index.html "$REPO/" "$WEB/"
install -m 0644 "$REPO/index.html" "$WEB/.index.html.new"
mv "$WEB/.index.html.new" "$WEB/index.html"
find "$WEB/assets" -type f -mtime +30 -delete 2>/dev/null || true
echo "$NEW" > "$STATE"
echo "web deployed ${NEW:0:12}"
