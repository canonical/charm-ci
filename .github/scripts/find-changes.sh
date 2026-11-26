#!/usr/bin/env bash
# find-changes.sh — Decide whether a pull request changes any file that does
# not match IGNORED_FILE_PATTERNS, so integration-test.yml can skip the build
# and tests. Run it from the root of a pull_request checkout (a merge commit,
# fetch-depth 2 or more).
#
# Required environment variables:
#   EVENT_NAME             — github.event_name
#   GITHUB_OUTPUT          — path to GitHub Actions output file
# Optional environment variables:
#   IGNORED_FILE_PATTERNS  — extended regex (grep -E) matched against paths
#                            relative to the repository root
#
# Writes has-code-changes=true|false. Without patterns, outside a pull request,
# or without a merge commit to diff, it writes true: nothing is skipped.
set -euo pipefail

: "${EVENT_NAME:?EVENT_NAME must be set}"
: "${GITHUB_OUTPUT:?GITHUB_OUTPUT must be set}"
IGNORED_FILE_PATTERNS="${IGNORED_FILE_PATTERNS:-}"

has_code_changes=true
# The PR checkout is a merge commit: HEAD^1..HEAD is what the PR changes.
if [ "${EVENT_NAME}" = "pull_request" ] && [ -n "${IGNORED_FILE_PATTERNS}" ] \
  && git rev-parse --verify --quiet HEAD^2 > /dev/null; then
  if grep -qE -- "${IGNORED_FILE_PATTERNS}" <<< ""; then
    echo "::error::ignored-file-patterns matches every path; check for a trailing newline or an empty alternative."
    exit 1
  fi
  changed_files=$(git -c core.quotepath=off diff --no-renames --name-only HEAD^1 HEAD)
  # grep -v exits 0 if any file is not ignored, 1 if all are, 2 on an invalid regex.
  status=0
  grep -qvE -- "${IGNORED_FILE_PATTERNS}" <<< "${changed_files}" || status=$?
  case "${status}" in
    0) ;;
    1)
      has_code_changes=false
      echo "Only ignored files changed; skipping build and integration tests."
      ;;
    *)
      echo "::error::ignored-file-patterns is not a valid extended regex."
      exit 1
      ;;
  esac
fi
echo "has-code-changes=${has_code_changes}" >> "${GITHUB_OUTPUT}"
