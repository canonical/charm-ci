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

# 3. Avoid the inconsistent status-filtered listing; select successes locally.
# Keep tree matching across different commit SHAs (e.g. squash/rebase merges).
# This retains the existing head-commit matching semantics, not checkout provenance.
MAX_ATTEMPTS=5
MAX_PAGES=10
PER_PAGE=100
RETRY_DELAYS=(15 30 60 120)

for ((attempt = 1; attempt <= MAX_ATTEMPTS; attempt++)); do
  for ((page = 1; page <= MAX_PAGES; page++)); do
    if ! response=$(gh api \
      "repos/${REPO}/actions/workflows/${WORKFLOW_ID}/runs?per_page=${PER_PAGE}&page=${page}"); then
      echo "::error::Could not list integration-test runs (attempt ${attempt}, page ${page})."
      exit 1
    fi
    if ! jq -e '(.workflow_runs | type) == "array" and (.total_count | type) == "number"' \
      <<< "${response}" > /dev/null; then
      echo "::error::Invalid integration-test run listing (attempt ${attempt}, page ${page})."
      exit 1
    fi

    run_count=$(jq '.workflow_runs | length' <<< "${response}")
    jq -r --arg attempt "${attempt}/${MAX_ATTEMPTS}" --arg page "${page}" '
      "Run lookup: attempt=\($attempt) page=\($page) returned=\(.workflow_runs | length)" +
      " total=\(.total_count) newest=\(.workflow_runs[0].created_at // "none")" +
      " oldest=\(.workflow_runs[-1].created_at // "none")"
    ' <<< "${response}"
    jq -r --arg tree "${TREE_SHA}" '
      .workflow_runs[]
      | select(.conclusion == "success" and .head_commit.tree_id == $tree)
      | "Candidate run: id=\(.id) attempt=\(.run_attempt) conclusion=\(.conclusion)" +
        " head_sha=\(.head_sha) tree=\(.head_commit.tree_id)"
    ' <<< "${response}"
    RUN_ID=$(jq -r --arg tree "${TREE_SHA}" '
      [.workflow_runs[] | select(.conclusion == "success" and .head_commit.tree_id == $tree)]
      | first | .id // empty
    ' <<< "${response}")

    if [ -n "${RUN_ID}" ]; then
      echo "run-id=${RUN_ID}" >> "${GITHUB_OUTPUT}"
      echo "Found integration-test run: ${RUN_ID}"
      exit 0
    fi
    if [ "${run_count}" -lt "${PER_PAGE}" ]; then
      break
    fi
    if [ "${page}" -eq "${MAX_PAGES}" ]; then
      echo "::warning::Search limit reached: $((MAX_PAGES * PER_PAGE)) runs per attempt."
    fi
  done

  if [ "${attempt}" -lt "${MAX_ATTEMPTS}" ]; then
    delay="${RETRY_DELAYS[attempt - 1]}"
    echo "No matching successful run yet; retrying in ${delay}s."
    sleep "${delay}"
  fi
done

echo "::error::No successful integration-test run found for tree SHA ${TREE_SHA} after ${MAX_ATTEMPTS} attempts."
echo "::error::Ensure integration tests have passed for this exact code before publishing."
exit 1
