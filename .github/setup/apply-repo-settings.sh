#!/usr/bin/env bash
# Applies this repository's settings. Run once, with an admin `gh` login, after
# the "OFF seed tests" workflow has run on main.
set -euo pipefail
R=The-Dev-Spot/basketvitality-seed
cd "$(dirname "$0")"

gh api -X PATCH "repos/$R" --input - >/dev/null <<'JSON'
{
  "has_wiki": false,
  "has_projects": false,
  "delete_branch_on_merge": true,
  "allow_merge_commit": false,
  "allow_rebase_merge": false,
  "allow_squash_merge": true,
  "security_and_analysis": {
    "secret_scanning": { "status": "enabled" },
    "secret_scanning_push_protection": { "status": "enabled" },
    "dependabot_security_updates": { "status": "enabled" }
  }
}
JSON
gh api -X PUT "repos/$R/vulnerability-alerts" --silent
gh api -X PUT "repos/$R/private-vulnerability-reporting" --silent

# Workflows start read-only, can't approve pull requests, and may only use
# GitHub's own actions, pinned to a commit.
gh api -X PUT "repos/$R/actions/permissions/workflow" \
  -f default_workflow_permissions=read -F can_approve_pull_request_reviews=false --silent
gh api -X PUT "repos/$R/actions/permissions" \
  -F enabled=true -f allowed_actions=selected -F sha_pinning_required=true --silent
gh api -X PUT "repos/$R/actions/permissions/selected-actions" \
  -F github_owned_allowed=true -F verified_allowed=false --silent
gh api -X PUT "repos/$R/actions/permissions/fork-pr-contributor-approval" \
  -f approval_policy=all_external_contributors --silent

gh api -X POST "repos/$R/rulesets" --input ruleset-main.json --silent
echo "Done. Check: a direct push to main should now be rejected."
