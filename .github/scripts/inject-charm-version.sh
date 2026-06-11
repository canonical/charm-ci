#!/usr/bin/env bash
# Inject a root-level version file into charm archives before publishing.

set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "::error::usage: inject-charm-version.sh <commit-sha> <charm-file>..." >&2
  exit 1
fi

commit_sha="$1"
shift

if [ -z "${commit_sha}" ]; then
  echo "::error::commit SHA must not be empty" >&2
  exit 1
fi

version="${commit_sha:0:8}"

for charm_file in "$@"; do
  python3 - "${charm_file}" "${version}" <<'PY'
import sys
import zipfile
from pathlib import Path

charm_path = Path(sys.argv[1])
version = sys.argv[2]

if not charm_path.is_file():
    print(f"::error::charm file not found: {charm_path}", file=sys.stderr)
    sys.exit(1)

try:
    with zipfile.ZipFile(charm_path, "r") as charm:
        if "version" in charm.namelist():
            print(f"Preserving existing version file in {charm_path}")
            sys.exit(0)
    with zipfile.ZipFile(charm_path, "a") as charm:
        charm.writestr("version", f"{version}\n")
except zipfile.BadZipFile:
    print(f"::error::not a valid charm archive: {charm_path}", file=sys.stderr)
    sys.exit(1)

print(f"Injected version {version} into {charm_path}")
PY
done
