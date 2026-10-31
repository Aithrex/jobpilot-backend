#!/usr/bin/env bash
# Deploy one commit with no downtime. Run as root by pull-deploy.
#
#   1. Build the image; the live site keeps serving meanwhile.
#   2. Start the idle API colour on the new image and wait for its health check.
#   3. Point nginx at it (a reload: open connections finish on the old one).
#   4. Confirm the new revision through nginx; if not, point back and stop.
#   5. Replace the worker, stop the old colour. Their running jobs finish first.
#
# A failure at 1, 2 or 4 leaves the old version serving and exits non-zero.
set -euo pipefail

SHA="${1:?usage: deploy.sh <commit sha>}"
SRC="${SRC:-/opt/jobpilot/src}"
ENV_FILE=/etc/jobpilot/compose.env
UPSTREAM=/etc/jobpilot/nginx/upstream.conf
ACTIVE_FILE=/var/lib/jobpilot/active-color
TAG="${SHA:0:12}"

compose() { docker compose -f "$SRC/infra/compose/docker-compose.yml" --env-file "$ENV_FILE" "$@"; }

write_env() {
  local blue="$1" green="$2" worker="$3" tmp
  tmp=$(mktemp "$ENV_FILE.XXXXXX")
  printf 'BLUE_TAG=%s\nGREEN_TAG=%s\nWORKER_TAG=%s\n' "$blue" "$green" "$worker" > "$tmp"
  chmod 644 "$tmp"
  mv "$tmp" "$ENV_FILE"
}

point_nginx_at() {
  local tmp
  tmp=$(mktemp "$UPSTREAM.XXXXXX")
  printf 'upstream api_active {\n    server api_%s:8000;\n    keepalive 32;\n}\n' "$1" > "$tmp"
  chmod 644 "$tmp"
  mv "$tmp" "$UPSTREAM"
  compose exec -T web nginx -t -q
  compose exec -T web nginx -s reload
}

wait_healthy() {
  local cid state
  cid=$(compose ps -q "$1")
  for _ in $(seq 1 60); do
    state=$(docker inspect -f '{{.State.Health.Status}}' "$cid" 2>/dev/null || echo missing)
    [ "$state" = healthy ] && return 0
    [ "$state" = unhealthy ] && return 1
    sleep 2
  done
  return 1
}

touch "$ENV_FILE"
BLUE_TAG=none GREEN_TAG=none WORKER_TAG=none
# shellcheck disable=SC1090
. "$ENV_FILE"
PREV_BLUE="$BLUE_TAG" PREV_GREEN="$GREEN_TAG" PREV_WORKER="$WORKER_TAG"
ACTIVE=$(cat "$ACTIVE_FILE" 2>/dev/null || echo none)
[ "$ACTIVE" = blue ] && NEXT=green || NEXT=blue

echo "building $TAG"
docker build -q -t "jobpilot-app:$TAG" -f "$SRC/infra/compose/Dockerfile" "$SRC" >/dev/null

if [ "$NEXT" = blue ]; then write_env "$TAG" "$GREEN_TAG" "$WORKER_TAG"
else write_env "$BLUE_TAG" "$TAG" "$WORKER_TAG"; fi

compose up -d --no-deps s3
compose up -d --no-deps "api_$NEXT"
if ! wait_healthy "api_$NEXT"; then
  echo "api_$NEXT failed its health check; $ACTIVE keeps serving" >&2
  compose logs --tail 40 "api_$NEXT" >&2 || true
  compose stop -t 5 "api_$NEXT" || true
  write_env "$PREV_BLUE" "$PREV_GREEN" "$PREV_WORKER"
  exit 1
fi

if [ ! -f "$UPSTREAM" ]; then
  printf 'upstream api_active {\n    server api_%s:8000;\n    keepalive 32;\n}\n' "$NEXT" > "$UPSTREAM"
fi
compose up -d --no-deps web cloudflared
point_nginx_at "$NEXT"
sleep 2

SERVED=$(compose exec -T web wget -qO- -T 5 --header 'Host: aspirely.me' http://127.0.0.1:8080/api/health || true)
if ! grep -q "\"revision\":\"$TAG\"" <<<"${SERVED// /}"; then
  echo "new revision not served through nginx; pointing back at $ACTIVE" >&2
  [ "$ACTIVE" = none ] || point_nginx_at "$ACTIVE"
  compose stop -t 30 "api_$NEXT" || true
  write_env "$PREV_BLUE" "$PREV_GREEN" "$PREV_WORKER"
  exit 1
fi
echo "$NEXT" > "$ACTIVE_FILE"

# shellcheck disable=SC1090
. "$ENV_FILE"
write_env "$BLUE_TAG" "$GREEN_TAG" "$TAG"
# While the old host still owns the schedules, this one must not run them.
[ -f /etc/jobpilot/hold-worker ] || compose up -d --no-deps worker
[ "$ACTIVE" = none ] || compose stop "api_$ACTIVE"

# Keep this image and the previous one for a hand rollback.
docker images --format '{{.Tag}}' jobpilot-app \
  | grep -vxE "$TAG|${PREV_BLUE}|${PREV_GREEN}|${PREV_WORKER}" \
  | xargs -r -I{} docker rmi -f "jobpilot-app:{}" >/dev/null 2>&1 || true
docker image prune -f >/dev/null 2>&1 || true
echo "live: $TAG on api_$NEXT"
