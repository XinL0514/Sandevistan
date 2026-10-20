#!/bin/zsh
# Build "Sandevistan ADB.app" with PyInstaller.
#
# The bundle carries its own Python, PySide6 and Qt frameworks, so the result
# runs on a Mac without Python installed. Output: dist/Sandevistan ADB.app
# Bundle metadata (version, identifier) lives in "Sandevistan ADB.spec".
set -eu

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_dir"
export PYINSTALLER_CONFIG_DIR="${PYINSTALLER_CONFIG_DIR:-$project_dir/.pyinstaller}"

venv="${SANDEVISTAN_VENV:-$project_dir/.venv}"
python="$venv/bin/python"
if [[ ! -x "$python" ]]; then
    print -u2 "缺少虚拟环境：$venv"
    print -u2 "先运行：python3 -m venv .venv && .venv/bin/pip install -r requirements-qt.txt pyinstaller"
    exit 1
fi

if ! "$python" -c 'import PyInstaller' 2>/dev/null; then
    print "安装 PyInstaller…"
    "$python" -m pip install --quiet pyinstaller
fi

"$project_dir/scripts/fetch_lgpl_sources.sh"

rm -rf build dist/"Sandevistan ADB.app" dist/"Sandevistan ADB"
rm -f dist/Sandevistan-ADB-macos-arm64-dev.zip

"$python" -m PyInstaller --noconfirm --clean "Sandevistan ADB.spec"

app="dist/Sandevistan ADB.app"
"$python" scripts/verify_bundle.py "$app"

# Strip Finder metadata that breaks codesign, then ad-hoc sign for local
# testing. FileProvider (iCloud/Documents sync) can re-add extended
# attributes during signing, so retry until the signature verifies.
for attempt in 1 2 3; do
    xattr -cr "$app"
    codesign --force --deep --sign - "$app" >/dev/null 2>&1 || true
    if codesign --verify --deep "$app" >/dev/null 2>&1; then
        break
    fi
    print "签名校验失败，重试（$attempt/3）…"
    sleep 1
done
codesign --verify --deep "$app"

# Desktop folders managed by FileProvider may add Finder metadata back to the
# live .app. The ZIP strips that metadata; verify the extracted artifact too.
archive="dist/Sandevistan-ADB-macos-arm64-dev.zip"
ditto --norsrc -c -k --keepParent "$app" "$archive"
verify_dir="$(mktemp -d "${TMPDIR:-/private/tmp}/sandevistan-verify.XXXXXX")"
trap 'rm -rf "$verify_dir"' EXIT
ditto -x -k "$archive" "$verify_dir"
codesign --verify --deep --strict "$verify_dir/Sandevistan ADB.app"

print "Created $project_dir/$app"
print "Verified ZIP: $project_dir/$archive"
