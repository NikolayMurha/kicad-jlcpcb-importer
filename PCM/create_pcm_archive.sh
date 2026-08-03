#!/bin/sh

set -eu

# heavily inspired by https://github.com/4ms/4ms-kicad-lib/blob/master/PCM/make_archive.sh

VERSION=${1:?usage: create_pcm_archive.sh VERSION}
ARCHIVE_ROOT="PCM/archive"
PLUGIN_ROOT="$ARCHIVE_ROOT/plugins"
RESOURCE_ROOT="$ARCHIVE_ROOT/resources"

if ! printf '%s\n' "$VERSION" | grep -Eq '^[0-9]{4}\.[0-9]{2}\.[0-9]{2}$'; then
  echo "Invalid version '$VERSION': expected CalVer YYYY.MM.DD" >&2
  exit 2
fi

python3 -c 'from datetime import datetime; import sys; datetime.strptime(sys.argv[1], "%Y.%m.%d")' "$VERSION" || {
  echo "Invalid calendar date in version '$VERSION'" >&2
  exit 2
}

SOURCE_VERSION=$(tr -d '[:space:]' < VERSION)
if [ "$SOURCE_VERSION" != "$VERSION" ]; then
  echo "VERSION file contains '$SOURCE_VERSION', expected '$VERSION'" >&2
  exit 2
fi

echo "Clean up old files"
rm -f PCM/*.zip
rm -rf "$ARCHIVE_ROOT"


echo "Create folder structure for ZIP"
mkdir -p "$PLUGIN_ROOT"
mkdir -p "$RESOURCE_ROOT"

echo "Copy files to destination"
cp VERSION "$PLUGIN_ROOT"
cp ./*.py "$PLUGIN_ROOT"
cp plugin.json "$PLUGIN_ROOT/plugin.json"
cp requirements.txt "$PLUGIN_ROOT"
cp settings.default.json "$PLUGIN_ROOT/settings.default.json"
cp -R src icons images "$PLUGIN_ROOT"
find "$PLUGIN_ROOT" -type f \( -name '*.pyc' -o -name '.DS_Store' -o -name 'test_*.py' -o -name 'pytest.ini' \) -delete
find "$PLUGIN_ROOT" -depth -type d -name '__pycache__' -empty -delete
# Include an icon in the PCM archive resources. Prefer PCM/icon.png; fall back to PCM/jlcpcb.png.
if [ -f PCM/icon.png ]; then
  cp PCM/icon.png "$RESOURCE_ROOT/icon.png"
elif [ -f PCM/jlcpcb.png ]; then
  cp PCM/jlcpcb.png "$RESOURCE_ROOT/icon.png"
else
  echo "Warning: no icon found in PCM/ (expected icon.png or jlcpcb.png)" >&2
fi
cp PCM/metadata.template.json "$ARCHIVE_ROOT/metadata.json"

echo "Write version info to file"
printf '%s\n' "$VERSION" > "$PLUGIN_ROOT/VERSION"

echo "Modify archive metadata.json"
metadata_tmp="$ARCHIVE_ROOT/metadata.json.tmp"
sed \
  -e "s|VERSION_HERE|$VERSION|g" \
  -e '/SHA256_HERE/d' \
  -e '/DOWNLOAD_SIZE_HERE/d' \
  -e '/DOWNLOAD_URL_HERE/d' \
  -e '/INSTALL_SIZE_HERE/d' \
  "$ARCHIVE_ROOT/metadata.json" > "$metadata_tmp"
mv "$metadata_tmp" "$ARCHIVE_ROOT/metadata.json"

echo "Zip PCM archive"
cd "$ARCHIVE_ROOT"
zip -r "../KiCAD-PCM-$VERSION.zip" .
cd ../..

echo "Gather data for repo rebuild"
if [ -n "${GITHUB_ENV:-}" ]; then
  echo "VERSION=$VERSION" >> "$GITHUB_ENV"
  echo "DOWNLOAD_SHA256=$(shasum --algorithm 256 "PCM/KiCAD-PCM-$VERSION.zip" | xargs | cut -d' ' -f1)" >> "$GITHUB_ENV"
  echo "DOWNLOAD_SIZE=$(wc -c < "PCM/KiCAD-PCM-$VERSION.zip" | xargs)" >> "$GITHUB_ENV"
  echo "DOWNLOAD_URL=https:\/\/github.com\/NikolayMurha\/kicad-jlcpcb-importer\/releases\/download\/$VERSION\/KiCAD-PCM-$VERSION.zip" >> "$GITHUB_ENV"
  echo "INSTALL_SIZE=$(unzip -l "PCM/KiCAD-PCM-$VERSION.zip" | tail -1 | xargs | cut -d' ' -f1)" >> "$GITHUB_ENV"
fi
