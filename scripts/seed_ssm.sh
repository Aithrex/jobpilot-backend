#!/usr/bin/env bash
# Seed SSM Parameter Store (SecureString) from the local .env so the prod
# Lambdas (USE_SSM_SECRETS=true) can read secrets. Values are NEVER printed.
#
# Maps .env keys -> /<prefix>/<param> paths that match the SSM_* env vars
# wired in infra/terraform/lambda.tf.
#
# Usage:  AWS_PROFILE=aithrex bash scripts/seed_ssm.sh [prefix]   (default /jobpilot/prod)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.env"
PREFIX="${1:-/jobpilot/prod}"
REGION="${AWS_REGION:-us-east-1}"
PROFILE_ARG=""
[ -n "${AWS_PROFILE:-}" ] && PROFILE_ARG="--profile ${AWS_PROFILE}"

[ -f "$ENV_FILE" ] || { echo "!! $ENV_FILE not found" >&2; exit 1; }

# .env KEY  ->  SSM param path (under PREFIX)
declare -A MAP=(
  [MONGO_URI]="mongodb-uri"
  [JWT_SECRET_KEY]="jwt-secret-key"
  [IPINFO_TOKEN]="ipinfo-token"
  [GEMINI_API_KEY]="gemini-api-key"
  [JSEARCH_API_KEY]="jsearch-api-key"
  [APIFY_API_KEY]="apify-api-key"
  [JOB_SEARCH_PASSWORD_HASH]="job-search-password-hash"
  [GITHUB_PAT]="github-pat"
)

get_val() {
  # First match wins; everything after the first '=' is the value (handles '=' in values).
  local line
  line=$(grep -E "^$1=" "$ENV_FILE" | head -1 || true)
  [ -z "$line" ] && return 1
  local v="${line#*=}"
  # strip optional surrounding quotes
  v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
  printf '%s' "$v"
}

seeded=0; skipped=0
for key in "${!MAP[@]}"; do
  path="${PREFIX}/${MAP[$key]}"
  if val=$(get_val "$key") && [ -n "$val" ]; then
    aws ssm put-parameter $PROFILE_ARG --region "$REGION" \
      --name "$path" --type SecureString --value "$val" --overwrite >/dev/null
    echo "  set  $path"
    seeded=$((seeded+1))
  else
    echo "  skip $path  (no $key in .env)"
    skipped=$((skipped+1))
  fi
done

echo ">> Done: $seeded set, $skipped skipped (region=$REGION, profile=${AWS_PROFILE:-default})"
