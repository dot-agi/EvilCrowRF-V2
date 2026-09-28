#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."

icon_source="$PWD/macos/Runner/Resources/EvilCrow.icns"
asset_dir="$PWD/macos/Runner/Assets.xcassets/AppIcon.appiconset"
icon_tmp="$(mktemp -d "${TMPDIR:-/tmp}/evilcrow-icons.XXXXXX")"
trap 'rm -rf "$icon_tmp"' EXIT

iconutil --convert iconset --output "$icon_tmp/EvilCrow.iconset" "$icon_source"
for icon in "$icon_tmp/EvilCrow.iconset/"*.png; do
  cp "$icon" "$asset_dir/$(basename "$icon")"
done
echo "Updated macOS AppIcon PNGs from $icon_source"
