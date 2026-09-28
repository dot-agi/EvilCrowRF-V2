#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
repo_root="$(cd .. && pwd)"
if [ -z "${EVILCROW_USB_HELPER:-}" ]; then
  "$repo_root/SDR/build_macos.sh"
fi
usb_helper="${EVILCROW_USB_HELPER:-$repo_root/SDR/dist/EvilCrow SDR.app}"
if [ ! -x "$usb_helper/Contents/MacOS/EvilCrow SDR" ]; then
  echo 'Build the USB backend first with SDR/build_macos.sh.' >&2
  exit 1
fi
"$usb_helper/Contents/MacOS/EvilCrow SDR" --backend --help >/dev/null
flutter_bin="${FLUTTER:-$repo_root/.tools/flutter/bin/flutter}"
if [ ! -x "$flutter_bin" ]; then
  flutter_bin="$(command -v flutter)"
fi
export PUB_CACHE="${PUB_CACHE:-$repo_root/.tools/pub-cache}"
if [ -d "$repo_root/.tools/gems" ]; then
  export GEM_HOME="$repo_root/.tools/gems"
  export GEM_PATH="$GEM_HOME"
  export PATH="$GEM_HOME/bin:$PATH"
fi
export CP_HOME_DIR="${CP_HOME_DIR:-$repo_root/.tools/cocoapods}"
"$flutter_bin" --suppress-analytics pub get
"$flutter_bin" --suppress-analytics build macos --release "$@"
app_bundle="$PWD/build/macos/Build/Products/Release/EvilCrow RF.app"
helper_bundle="$app_bundle/Contents/Helpers/EvilCrow SDR.app"
mkdir -p "$app_bundle/Contents/Helpers"
rm -rf "$helper_bundle"
ditto "$usb_helper" "$helper_bundle"
codesign --force --sign - --entitlements macos/Runner/UsbHelper.entitlements "$helper_bundle"
# Flutter can replace App.framework after Xcode seals an incremental build.
# Refresh the outer signature for this local, ad-hoc signed app.
codesign --force --sign - --entitlements macos/Runner/Release.entitlements "$app_bundle"
codesign --verify --deep --strict "$app_bundle"
echo "Built: $app_bundle"
