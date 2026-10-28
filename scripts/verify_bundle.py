"""Fail a build if unused GPL-only Qt modules or license assets reappear."""

from pathlib import Path
import hashlib
import os
import subprocess
import sys


app = Path(sys.argv[1])
contents = app / "Contents"
resources = contents / "Resources"
frameworks = contents / "Frameworks" / "PySide6" / "Qt" / "lib"

required = [
    resources / "licenses" / "THIRD_PARTY_NOTICES.md",
    resources / "licenses" / "LGPL-3.0.txt",
    resources / "licenses" / "GPL-3.0.txt",
    resources / "licenses" / "Python-3.13.0-LICENSE.txt",
    resources / "licenses" / "OpenSSL-3.0.15-LICENSE.txt",
    resources / "licenses" / "QtAwesome-1.4.2-MIT.txt",
    resources / "licenses" / "QtPy-2.4.3-MIT.txt",
    resources / "licenses" / "FontAwesome-6-LICENSE.txt",
    resources / "licenses" / "SOURCES_AND_RELINKING.md",
    resources / "licenses" / "platform-tools" / "NOTICE.txt",
    resources / "licenses" / "platform-tools" / "source.properties",
    resources / "qtawesome" / "fonts" / "fontawesome6-solid-webfont-6.7.2.ttf",
    resources / "qtawesome" / "fonts" / "fontawesome6-solid-webfont-charmap-6.7.2.json",
    frameworks / "QtWidgets.framework",
]
missing = [str(path) for path in required if not path.exists()]
if missing:
    raise SystemExit("Missing required bundle files:\n" + "\n".join(missing))

font_names = sorted(path.name for path in (resources / "qtawesome" / "fonts").iterdir())
expected_fonts = sorted([
    "fontawesome6-solid-webfont-6.7.2.ttf",
    "fontawesome6-solid-webfont-charmap-6.7.2.json",
])
if font_names != expected_fonts:
    raise SystemExit(f"Unexpected QtAwesome fonts: {font_names}")

source_dir = resources / "licenses" / "sources"
manifest = source_dir / "SHA256SUMS.txt"
if not manifest.is_file():
    raise SystemExit("Missing source archive checksum manifest")
for line in manifest.read_text(encoding="utf-8").splitlines():
    expected, filename = line.split(maxsplit=1)
    archive = source_dir / filename
    if not archive.is_file():
        raise SystemExit(f"Missing LGPL source archive: {filename}")
    with archive.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != expected:
        raise SystemExit(f"LGPL source checksum mismatch: {filename}")

for path in contents.rglob("*"):
    name = path.name.lower()
    if "qtvirtualkeyboard" in name:
        raise SystemExit(f"GPL-only Qt Virtual Keyboard is present: {path}")

adb = contents / "Frameworks" / "platform-tools" / "adb"
if not adb.is_file() or not os.access(adb, os.X_OK):
    raise SystemExit(f"Missing executable bundled adb: {adb}")
version = subprocess.run([str(adb), "version"], capture_output=True,
                         text=True, timeout=10, check=True).stdout
if "Android Debug Bridge version" not in version:
    raise SystemExit(f"Bundled adb failed version check: {version}")

print("Bundle license, dependency and adb checks passed")
