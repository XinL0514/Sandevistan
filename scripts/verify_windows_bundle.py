"""Verify the Windows package's ADB, Qt, fonts and license materials."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path


REQUIRED_LICENSES = (
    "THIRD_PARTY_NOTICES.md", "LGPL-3.0.txt", "GPL-3.0.txt",
    "Python-3.13.0-LICENSE.txt", "OpenSSL-3.0.15-LICENSE.txt",
    "QtAwesome-1.4.2-MIT.txt", "QtPy-2.4.3-MIT.txt",
    "FontAwesome-6-LICENSE.txt", "SOURCES_AND_RELINKING.md",
)
REQUIRED_FONTS = (
    "fontawesome6-solid-webfont-6.7.2.ttf",
    "fontawesome6-solid-webfont-charmap-6.7.2.json",
)


def verify(app: Path) -> None:
    internal = app / "_internal"
    licenses = internal / "licenses"
    adb_dir = internal / "platform-tools"
    fonts = internal / "qtawesome" / "fonts"
    required = [app / "Sandevistan ADB.exe",
                *(licenses / name for name in REQUIRED_LICENSES),
                *(licenses / "platform-tools" / name
                  for name in ("NOTICE.txt", "source.properties")),
                *(adb_dir / name for name in
                  ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll")),
                *(fonts / name for name in REQUIRED_FONTS)]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError("Missing Windows bundle files:\n" + "\n".join(missing))

    font_names = sorted(path.name for path in fonts.iterdir())
    if font_names != sorted(REQUIRED_FONTS):
        raise RuntimeError(f"Unexpected bundled fonts: {font_names}")

    manifest = licenses / "sources" / "SHA256SUMS.txt"
    if not manifest.is_file():
        raise RuntimeError(f"Missing source archive manifest: {manifest}")
    for line in manifest.read_text(encoding="utf-8").splitlines():
        expected, filename = line.split(maxsplit=1)
        archive = manifest.parent / filename
        if not archive.is_file():
            raise RuntimeError(f"Missing LGPL source archive: {filename}")
        with archive.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != expected:
            raise RuntimeError(f"LGPL source checksum mismatch: {filename}")

    names = {path.name.lower() for path in internal.rglob("*")}
    if "qtwidgets.pyd" not in names or "qwindows.dll" not in names:
        raise RuntimeError("Qt Widgets or Windows platform plugin is missing")
    unexpected = sorted(name for name in names if "qtvirtualkeyboard" in name)
    if unexpected:
        raise RuntimeError(f"Unused Qt Virtual Keyboard entered the bundle: "
                           f"{unexpected}")

    version = subprocess.run([str(adb_dir / "adb.exe"), "version"],
                             capture_output=True, text=True, timeout=15,
                             check=True).stdout
    if "Android Debug Bridge version" not in version:
        raise RuntimeError(f"Bundled adb failed version check: {version}")
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"
    startup = subprocess.run([str(app / "Sandevistan ADB.exe"), "--smoke-test"],
                             cwd=app, env=environment, capture_output=True,
                             text=True, timeout=30, check=False)
    if startup.returncode:
        raise RuntimeError(f"Windows EXE startup failed ({startup.returncode}): "
                           f"{startup.stderr.strip()}")
    print("Windows bundle license, Qt and adb checks passed")


if __name__ == "__main__":
    verify(Path(sys.argv[1]))
