#!/bin/zsh
set -euo pipefail

cd "${0:A:h}"

python3 release_check.py
release_version=$(python3 -c 'from app_version import APP_VERSION; print(APP_VERSION)')
python3 -m PyInstaller "APK Tool Standard.spec" \
  --clean --noconfirm --distpath dist/editions --workpath build/editions/standard
python3 -m PyInstaller "APK Tool Internal.spec" \
  --clean --noconfirm --distpath dist/editions --workpath build/editions/internal

ditto -c -k --sequesterRsrc --keepParent \
  "dist/editions/APK Tool Standard.app" \
  "dist/editions/APK-Tool-Standard-v${release_version}.zip"
ditto -c -k --sequesterRsrc --keepParent \
  "dist/editions/APK Tool Internal.app" \
  "dist/editions/APK-Tool-Internal-v${release_version}.zip"

echo "Builds created in dist/editions"
