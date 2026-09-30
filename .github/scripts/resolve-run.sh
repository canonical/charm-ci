#!/usr/bin/env bash
# resolve-run.sh — Find the last successful integration-test run matching
# the current commit's tree SHA.
#
# Required environment variables:
#   GH_TOKEN              — GitHub token with actions:read scope
#   GITHUB_REPOSITORY     — owner/repo
#   GITHUB_SHA            — commit SHA to resolve
#   GITHUB_OUTPUT         — path to GitHub Actions output file
#   INPUT_WORKFLOW_FILE   — filename of the integration-test workflow
set -euo pipefail

: "${GH_TOKEN:?GH_TOKEN must be set}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY must be set}"
: "${GITHUB_SHA:?GITHUB_SHA must be set}"
: "${GITHUB_OUTPUT:?GITHUB_OUTPUT must be set}"
: "${INPUT_WORKFLOW_FILE:?INPUT_WORKFLOW_FILE must be set}"

REPO="${GITHUB_REPOSITORY}"
COMMIT_SHA="${GITHUB_SHA}"

# 1. Get the tree SHA of the current commit.
TREE_SHA=$(gh api "repos/${REPO}/git/commits/${COMMIT_SHA}" --jq '.tree.sha')
echo "Current commit: ${COMMIT_SHA}"
echo "Tree SHA: ${TREE_SHA}"

# 2. Find the integration-test workflow ID.
WORKFLOW_ID=$(gh api "repos/${REPO}/actions/workflows" \
  --jq ".workflows[] | select(.path | endswith(\"${INPUT_WORKFLOW_FILE}\")) | .id")

if [ -z "${WORKFLOW_ID}" ] || [ "${WORKFLOW_ID}" = "null" ]; then
  echo "::error::Could not find workflow '${INPUT_WORKFLOW_FILE}' in ${REPO}"
  exit 1
fi

# Guard against ambiguous matches (multiple workflows with same filename).
workflow_count=$(echo "${WORKFLOW_ID}" | wc -l)
if [ "${workflow_count}" -gt 1 ]; then
  echo "::error::Multiple workflows found matching '${INPUT_WORKFLOW_FILE}':"
  echo "${WORKFLOW_ID}"
  exit 1
fi

echo "Integration workflow ID: ${WORKFLOW_ID} (${INPUT_WORKFLOW_FILE})"

# 3. Find the most recent successful run whose *tested code* matches our tree.
# Checks up to 100 runs (covers ~14 days for active repos) and stops at the first
# (most recent) match, so the common case costs one extra API call.
#
# Two deliberate robustness choices, each guarding against an eventually-consistent
# field in the list-workflow-runs API that previously made this gate spuriously fail
# right after a merge even though the integration tests had passed:
#   - We list runs unfiltered and select `.conclusion == "success"` client-side,
#     rather than using the `?status=success` API filter. That filtered endpoint is
#     served from a cache that can lag by hours and omit a just-finished run.
#   - We match on each run's authoritative `.head_sha`, resolved to its tree via the
#     git API, rather than on `.head_commit.tree_id`. For pull_request runs the list
#     API populates `head_commit` lazily and intermittently returns a stale commit.
#
# Matching by tree (rather than by commit SHA) is intentional: multiple commits can
# share a tree (e.g. a rebase, or a merge commit that preserves its PR's tree), and
# the tree guarantees the *code* was tested regardless of which commit triggered the
# run.
mapfile -t RUNS < <(gh api "repos/${REPO}/actions/workflows/${WORKFLOW_ID}/runs?per_page=100" \
  --jq '.workflow_runs[] | select(.conclusion == "success") | "\(.id) \(.head_sha)"')

RUN_ID=""
declare -A TREE_CACHE=()
for run in "${RUNS[@]}"; do
  run_id="${run%% *}"
  head_sha="${run##* }"
  [ -n "${head_sha}" ] || continue
  if [ "${head_sha}" = "${COMMIT_SHA}" ]; then
    run_tree="${TREE_SHA}"
  elif [ -n "${TREE_CACHE[${head_sha}]:-}" ]; then
    run_tree="${TREE_CACHE[${head_sha}]}"
  else
    run_tree=$(gh api "repos/${REPO}/git/commits/${head_sha}" --jq '.tree.sha' 2>/dev/null || true)
    TREE_CACHE[${head_sha}]="${run_tree}"
  fi
  if [ "${run_tree}" = "${TREE_SHA}" ]; then
    RUN_ID="${run_id}"
    break
  fi
done

if [ -z "${RUN_ID}" ] || [ "${RUN_ID}" = "null" ]; then
  echo "::error::No successful integration-test run found for tree SHA ${TREE_SHA}."
  echo "::error::Ensure integration tests have passed for this exact code before publishing."
  exit 1
fi

echo "run-id=${RUN_ID}" >> "${GITHUB_OUTPUT}"
echo "Found integration-test run: ${RUN_ID}"
