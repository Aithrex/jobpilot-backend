#!/usr/bin/env bash
# Protect `main` in both repos so a change is tested once, before it merges.
#
#   bash scripts/apply_main_ruleset.sh            # apply
#   bash scripts/apply_main_ruleset.sh --remove   # undo (deletes the rulesets)
#
# Each repo gets one ruleset on its default branch:
#   - a pull request is required (no approval count: solo owner),
#   - the repo's test check must pass, run by GitHub Actions (integration 15368,
#     so a hand-written commit status cannot satisfy it),
#   - the branch must be up to date with main (so the tested tree IS the merged
#     tree and the post-merge run reuses it instead of re-running),
#   - no force-push, no deletion.
# It also turns on auto-merge (`gh pr merge N --merge --auto`) and the
# "Update branch" button.
set -euo pipefail

NAME="main: tested before merge"
REPOS=("jobpilot-backend|Offline tests + evals" "jobpilot-frontend|Test and production build")

for pair in "${REPOS[@]}"; do
  repo="Aithrex/${pair%%|*}"
  check="${pair#*|}"
  existing=$(gh api "repos/${repo}/rulesets" --jq ".[] | select(.name == \"${NAME}\") | .id")

  if [ "${1:-}" = "--remove" ]; then
    for id in $existing; do
      gh api -X DELETE "repos/${repo}/rulesets/${id}" && echo "${repo}: removed ruleset ${id}"
    done
    continue
  fi

  body=$(cat <<JSON
{
  "name": "${NAME}",
  "target": "branch",
  "enforcement": "active",
  "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
  "bypass_actors": [],
  "rules": [
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {"type": "pull_request", "parameters": {
      "required_approving_review_count": 0,
      "dismiss_stale_reviews_on_push": false,
      "require_code_owner_review": false,
      "require_last_push_approval": false,
      "required_review_thread_resolution": false}},
    {"type": "required_status_checks", "parameters": {
      "strict_required_status_checks_policy": true,
      "required_status_checks": [{"context": "${check}", "integration_id": 15368}]}}
  ]
}
JSON
)
  if [ -n "$existing" ]; then
    id=$(echo "$existing" | head -1)
    echo "$body" | gh api -X PUT "repos/${repo}/rulesets/${id}" --input - --jq '"\(.name): updated (\(.enforcement))"'
  else
    echo "$body" | gh api -X POST "repos/${repo}/rulesets" --input - --jq '"\(.name): created (\(.enforcement))"'
  fi
  gh api -X PATCH "repos/${repo}" -F allow_auto_merge=true -F allow_update_branch=true \
    --jq '"'"${repo}"': auto-merge=\(.allow_auto_merge) update-branch=\(.allow_update_branch)"'
done
