#!/usr/bin/env bash

set -e

# Minimal script: build resources.zip for KiCad PCM
# - Reads identifier from PCM/metadata.template.json
# - Copies icon from PCM/icon.png or PCM/jlcpcb.png
# - Deletes existing ./$identifier directory first
# - Outputs resources.zip into the current working directory

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
meta="$here/metadata.template.json"

# Read identifier from metadata
identifier=""
if [ -f "$meta" ]; then
  # Fallback to grep/sed parsing if Python is unavailable
  if [ -z "$identifier" ]; then
    identifier="$(sed -n -E 's/.*"identifier"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' "$meta" | head -n 1 | tr -d '\r')"
  fi
fi

if [ -z "$identifier" ]; then
  echo "Error: could not read 'identifier' from $meta" >&2
  echo "Tip: ensure metadata.template.json has a line like: \"identifier\": \"com.github....\"" >&2
  exit 1
fi

icon_src=""
if [ -f "$here/icon.png" ]; then
  icon_src="$here/icon.png"
elif [ -f "$here/jlcpcb.png" ]; then
  icon_src="$here/jlcpcb.png"
else
  echo "Error: no icon found. Place icon.png or jlcpcb.png in PCM/." >&2
  exit 1
fi

# Clean previous identifier dir and zip in current working directory
rm -rf "$PWD/$identifier"
rm -f "$PWD/resources.zip"

dest_dir="$PWD/$identifier"
# Ensure we remove the temporary build folder on exit
cleanup() {
  rm -rf "$dest_dir" 2>/dev/null || true
}
trap cleanup EXIT
mkdir -p "$dest_dir"
cp -f "$icon_src" "$dest_dir/icon.png"

( zip -r -q resources.zip "$identifier" )

echo "Created: $PWD/resources.zip"
echo "Contains: $PWD/$identifier/icon.png"
