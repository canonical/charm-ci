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

# 3. List recent successful runs and find one matching our tree SHA.
# Checks up to 100 runs (covers ~14 days for active repos).
# Selects the most recent matching run (API returns newest first).
# NOTE: Multiple commits can share a tree SHA (e.g., after rebase).
# This is by design — tree SHA guarantees the *code* was tested,
# regardless of which specific commit triggered the test run.
RUN_ID=$(gh api "repos/${REPO}/actions/workflows/${WORKFLOW_ID}/runs?status=success&per_page=100" \
  --jq "[.workflow_runs[] | select(.head_commit.tree_id == \"${TREE_SHA}\")] | first | .id // empty")

if [ -z "${RUN_ID}" ] || [ "${RUN_ID}" = "null" ]; then
  echo "::error::No successful integration-test run found for tree SHA ${TREE_SHA}."
  echo "::error::Ensure integration tests have passed for this exact code before publishing."
  exit 1
fi

echo "run-id=${RUN_ID}" >> "${GITHUB_OUTPUT}"
echo "Found integration-test run: ${RUN_ID}"
