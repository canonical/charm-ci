#!/usr/bin/env bash
# Create one GitHub Release per charm per revision from opcli publish results.
#
# Tags follow the pattern: {charm-name}-rev{revision}  (e.g. haproxy-rev418)
# One release is created for every entry in .releases[] of each published charm.
#
# Expected environment:
#   GH_TOKEN        — GitHub token with contents:write
#
# Expected file:
#   publish-results.json — JSON output from `opcli artifacts publish --json`

set -euo pipefail

find_previous_release_tag() {
  local charm_name="$1"
  local revision="$2"
  local tag_prefix="${charm_name}-rev"
  local previous_tag

  while IFS= read -r previous_tag; do
    if gh release view "${previous_tag}" > /dev/null 2>&1; then
      echo "${previous_tag}"
      return 0
    fi
  done < <(
    git ls-remote --tags --refs origin "${tag_prefix}*" \
      | awk -v prefix="refs/tags/${tag_prefix}" -v current="${revision}" '
      {
        ref = $2
        if (index(ref, prefix) == 1) {
          rev = substr(ref, length(prefix) + 1)
          if (rev ~ /^[0-9]+$/ && rev + 0 < current + 0) {
            print rev
          }
        }
      }
    ' \
      | sort -nr \
      | awk -v prefix="${tag_prefix}" '{ print prefix $1 }'
  )
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
  echo "No charms published — skipping release creation."
  exit 0
fi

while IFS= read -r charm_entry; do
  charm_name=$(echo "$charm_entry" | jq -r '.charm_name')
  channel=$(echo "$charm_entry" | jq -r '.channel')
  resources=$(echo "$charm_entry" | jq -r '
    if (.resources | length) > 0
    then "**Resources:** " + ([.resources | to_entries[] | "\(.key): rev \(.value)"] | join(", "))
    else "" end')

  while IFS= read -r release_entry; do
    revision=$(echo "$release_entry" | jq -r '.revision')
    base=$(echo "$release_entry" | jq -r '.base // "unknown"')
    arch=$(echo "$release_entry" | jq -r '.arch')

    TAG="${charm_name}-rev${revision}"

    # Skip releases that already exist (safe to rerun after partial failures)
    if gh release view "${TAG}" > /dev/null 2>&1; then
      echo "Release ${TAG} already exists — skipping."
      continue
    fi

    TITLE="${charm_name} rev${revision}"
    BODY="## ${charm_name} rev${revision} — published to \`${channel}\`"$'\n\n'
    BODY+="**Base:** ${base}"$'\n'
    BODY+="**Arch:** ${arch}"$'\n'
    if [ -n "${resources}" ]; then
      BODY+="${resources}"$'\n'
    fi

    notes_file="$(mktemp)"
    printf "%s" "${BODY}" > "${notes_file}"

    release_args=(
      release create "${TAG}"
      --title "${TITLE}"
      --notes-file "${notes_file}"
      --generate-notes
    )
    previous_tag="$(find_previous_release_tag "${charm_name}" "${revision}")"
    if [ -n "${previous_tag}" ]; then
      release_args+=(--notes-start-tag "${previous_tag}")
    fi

    git tag "${TAG}" "${GITHUB_SHA:-HEAD}"
    git push origin "refs/tags/${TAG}"
    gh "${release_args[@]}"
    rm -f "${notes_file}"
  done < <(echo "$charm_entry" | jq -c '.releases[]')
done < <(jq -c '.[]' publish-results.json)
