#!/usr/bin/env bash
# Create a GitHub Release from opcli publish results.
#
# Expected environment:
#   GH_TOKEN        — GitHub token with contents:write
#   INPUT_CHANNEL   — CharmHub channel (e.g. latest/edge)
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

CHANNEL_SLUG="${INPUT_CHANNEL//\//-}"
TAG="publish/$(date -u +%Y%m%dT%H%M%S)-${CHANNEL_SLUG}-$(git rev-parse --short HEAD)"

# Skip if no charms were published
if [ "$(jq 'length' publish-results.json)" -eq 0 ]; then
  echo "No charms published — skipping release creation."
  exit 0
fi

BODY="## Published to ${INPUT_CHANNEL}"$'\n\n'
while IFS= read -r line; do
  charm_name=$(echo "$line" | jq -r '.charm_name')
  resources=$(echo "$line" | jq -r '
    if (.resources | length) > 0
    then " — resources: " + ([.resources | to_entries[] | "\(.key):\(.value)"] | join(", "))
    else "" end')
  while IFS= read -r rel; do
    BODY+="**${charm_name}** — ${rel}${resources}"$'\n'
  done < <(echo "$line" | jq -r '.releases[] | "rev \(.revision) (\(.base // "unknown") \(.arch))"')
done < <(jq -c '.[]' publish-results.json)

git tag "$TAG"
git push origin "$TAG"
gh release create "$TAG" --title "$TAG" --notes "$BODY"
