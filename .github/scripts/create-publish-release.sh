#!/usr/bin/env bash
# Tag published charm revisions and, optionally, create a single combined
# GitHub Release summarizing everything published in this workflow run.
#
# Per-revision git tag pattern: {charm-name}-rev{revision}  (e.g. haproxy-rev418)
#   Created for every entry in .releases[] of each published charm, when
#   CREATE_TAGS=true. Each tag is independently useful for traceability and
#   rollback even without a visible Release.
#
# Combined release tag: publish-${GITHUB_RUN_ID}
#   At most one GitHub Release is created per workflow run (idempotent across
#   reruns of the same run), when CREATE_RELEASE=true. Its body lists every
#   (charm, revision, base, arch) published, linking to the matching
#   per-revision tag when CREATE_TAGS=true (plain text otherwise).
#
# Expected environment:
#   GH_TOKEN           — GitHub token with contents:write
#   GITHUB_SHA         — commit SHA being published
#   GITHUB_REPOSITORY  — owner/repo (used to build tag tree links)
#   GITHUB_SERVER_URL  — e.g. https://github.com (used to build tag tree links)
#   GITHUB_RUN_ID      — used to build the idempotent combined-release tag
#   CREATE_TAGS        — "true"/"false" — create per-revision git tags (default true)
#   CREATE_RELEASE     — "true"/"false" — create the combined release (default true)
#
# Expected file:
#   publish-results.json — JSON output from `opcli artifacts publish --json`

set -euo pipefail

CREATE_TAGS="${CREATE_TAGS:-true}"
CREATE_RELEASE="${CREATE_RELEASE:-true}"

find_previous_combined_release_tag() {
  local tag_prefix="publish-"
  local previous_tag

  while IFS= read -r previous_tag; do
    if gh release view "${previous_tag}" > /dev/null 2>&1; then
      echo "${previous_tag}"
      return 0
    fi
  done < <(
    git ls-remote --tags --refs origin "${tag_prefix}*" \
      | awk -v prefix="refs/tags/${tag_prefix}" -v current="${GITHUB_RUN_ID:-0}" '
      {
        ref = $2
        if (index(ref, prefix) == 1) {
          run_id = substr(ref, length(prefix) + 1)
          if (run_id ~ /^[0-9]+$/ && run_id + 0 < current + 0) {
            print run_id
          }
        }
      }
    ' \
      | sort -nr \
      | awk -v prefix="${tag_prefix}" '{ print prefix $1 }'
  )
}

tag_exists_remotely() {
  local tag="$1"
  git ls-remote --tags --refs origin "${tag}" 2>/dev/null | grep -q "refs/tags/${tag}$"
}

if [ ! -f publish-results.json ]; then
  echo "::error::publish-results.json not found"
  exit 1
fi

# Validate JSON integrity (guards against stdout contamination)
if ! jq empty publish-results.json 2>/dev/null; then
  echo "::error::publish-results.json is not valid JSON"
  exit 1
fi

: "${GH_TOKEN:?GH_TOKEN environment variable is required}"

# Skip if no charms were published
if [ "$(jq 'length' publish-results.json)" -eq 0 ]; then
  echo "No charms published — skipping tag/release creation."
  exit 0
fi

if [ "${CREATE_TAGS}" != "true" ] && [ "${CREATE_RELEASE}" != "true" ]; then
  echo "create-tags and create-release are both false — nothing to do."
  exit 0
fi

REPO_URL="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-}"
# NOTE: this script is intended to run inside GitHub Actions, where
# GITHUB_RUN_ID is always set and unique per workflow run. The "manual"
# fallback exists only so the script doesn't hard-fail outside that context
# (e.g. local testing); two genuinely separate ad hoc invocations without
# GITHUB_RUN_ID set would collide on the same "publish-manual" tag and the
# second would be (incorrectly) treated as an idempotent rerun of the first.
COMBINED_TAG="publish-${GITHUB_RUN_ID:-manual}"
# Left on disk after this script exits (ephemeral CI runner) so it can be
# inspected for debugging; not cleaned up to keep this script simple.
BODY_FILE="$(mktemp)"

echo "## Published charms" >> "${BODY_FILE}"

while IFS= read -r charm_entry; do
  charm_name=$(echo "$charm_entry" | jq -r '.charm_name')
  channel=$(echo "$charm_entry" | jq -r '.channel')
  resources=$(echo "$charm_entry" | jq -r '
    if (.resources | length) > 0
    then " — resources: " + ([.resources | to_entries[] | "\(.key) rev\(.value)"] | join(", "))
    else "" end')

  {
    echo ""
    echo "### ${charm_name} → \`${channel}\`"
  } >> "${BODY_FILE}"

  while IFS= read -r release_entry; do
    revision=$(echo "$release_entry" | jq -r '.revision')
    base=$(echo "$release_entry" | jq -r '.base // "unknown"')
    arch=$(echo "$release_entry" | jq -r '.arch')

    tag="${charm_name}-rev${revision}"
    label="${tag} (${base}, ${arch})${resources}"

    if [ "${CREATE_TAGS}" = "true" ]; then
      if tag_exists_remotely "${tag}"; then
        echo "Tag ${tag} already exists — skipping."
      else
        git tag "${tag}" "${GITHUB_SHA:-HEAD}"
        git push origin "refs/tags/${tag}"
      fi
      echo "- [${label}](${REPO_URL}/tree/${tag})" >> "${BODY_FILE}"
    else
      echo "- ${label}" >> "${BODY_FILE}"
    fi
  done < <(echo "$charm_entry" | jq -c '.releases[]')
done < <(jq -c '.[]' publish-results.json)

if [ "${CREATE_RELEASE}" != "true" ]; then
  echo "create-release is false — skipping combined release creation."
  exit 0
fi

if gh release view "${COMBINED_TAG}" > /dev/null 2>&1; then
  echo "Release ${COMBINED_TAG} already exists — skipping (idempotent rerun)."
  exit 0
fi

release_args=(
  release create "${COMBINED_TAG}"
  --target "${GITHUB_SHA:-HEAD}"
  --title "Publish ${COMBINED_TAG#publish-}"
  --notes-file "${BODY_FILE}"
  --generate-notes
)
previous_tag="$(find_previous_combined_release_tag)"
if [ -n "${previous_tag}" ]; then
  release_args+=(--notes-start-tag "${previous_tag}")
fi

gh "${release_args[@]}"
