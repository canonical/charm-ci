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

if [ ! -f publish-results.json ]; then
  echo "::error::publish-results.json not found"
  exit 1
fi

# Validate JSON integrity (guards against stdout contamination)
if ! jq empty publish-results.json 2>/dev/null; then
  echo "::error::publish-results.json is not valid JSON"
  exit 1
fi

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
    TITLE="${charm_name} rev${revision}"
    BODY="## ${charm_name} rev${revision} — published to \`${channel}\`"$'\n\n'
    BODY+="**Base:** ${base}"$'\n'
    BODY+="**Arch:** ${arch}"$'\n'
    if [ -n "${resources}" ]; then
      BODY+="${resources}"$'\n'
    fi

    git tag "${TAG}"
    git push origin "${TAG}"
    gh release create "${TAG}" --title "${TITLE}" --notes "${BODY}"
  done < <(echo "$charm_entry" | jq -c '.releases[]')
done < <(jq -c '.[]' publish-results.json)
