#!/usr/bin/env bash
# Zips sp-plugin/ into keep-list-sync.zip, ready to upload via
# Super Productivity → Settings → Plugins → Upload.
set -euo pipefail
cd "$(dirname "$0")/sp-plugin"
rm -f ../keep-list-sync.zip
zip -r ../keep-list-sync.zip manifest.json plugin.js index.html icon.svg
echo "Built keep-list-sync.zip"
