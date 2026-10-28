"""Build a verified Windows x64 folder package and ZIP with PyInstaller."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
DIST_DIR = PROJECT / "dist"
APP_DIR = DIST_DIR / "Sandevistan ADB"
ARCHIVE = DIST_DIR / "Sandevistan-ADB-windows-x64-dev.zip"
SOURCES = PROJECT / "distribution" / "sources"
PLATFORM_TOOLS_URL = (
    "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
)


def download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    try:
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=60) as response, \
                        temporary.open("wb") as output:
                    shutil.copyfileobj(response, output)
                temporary.replace(destination)
                return
            except (urllib.error.URLError, TimeoutError):
                temporary.unlink(missing_ok=True)
                if attempt == 2:
                    raise
                time.sleep(attempt + 1)
    finally:
        temporary.unlink(missing_ok=True)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def ensure_sources() -> None:
    manifest = SOURCES / "SHA256SUMS.txt"
    for line in manifest.read_text(encoding="utf-8").splitlines():
        expected, filename = line.split(maxsplit=1)
        target = SOURCES / filename
        if target.is_file() and sha256(target) == expected:
            continue
        if filename.startswith("pyside-"):
            url = ("https://download.qt.io/official_releases/QtForPython/"
                   f"pyside6/PySide6-6.11.1-src/{filename}")
        else:
            url = ("https://download.qt.io/official_releases/qt/6.11/"
                   f"6.11.1/submodules/{filename}")
        print(f"Downloading LGPL source: {filename}", flush=True)
        download(url, target)
        if sha256(target) != expected:
            target.unlink()
            raise RuntimeError(f"LGPL source checksum mismatch: {filename}")


def ensure_platform_tools() -> Path:
    required = ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll",
                "NOTICE.txt", "source.properties")
    configured = os.environ.get("SANDEVISTAN_ADB_BINARY")
    if configured:
        adb = Path(configured).expanduser().resolve()
    else:
        cache = PROJECT / "build" / "windows-platform-tools"
        adb = cache / "platform-tools" / "adb.exe"
        if not all((adb.parent / name).is_file() for name in required):
            archive = cache / "platform-tools-latest-windows.zip"
            print("Downloading Android SDK Platform-Tools for Windows", flush=True)
            download(PLATFORM_TOOLS_URL, archive)
            with zipfile.ZipFile(archive) as package:
                for entry in package.infolist():
                    parts = Path(entry.filename).parts
                    if (len(parts) != 2 or parts[0] != "platform-tools"
                            or entry.is_dir()):
                        continue
                    destination = cache / "platform-tools" / parts[1]
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with package.open(entry) as source, destination.open("wb") as output:
                        shutil.copyfileobj(source, output)
    missing = [name for name in required if not (adb.parent / name).is_file()]
    if adb.name.lower() != "adb.exe" or missing:
        raise RuntimeError(f"Incomplete Windows Platform-Tools: {adb.parent}; "
                           f"missing {', '.join(missing)}")
    version = subprocess.run([str(adb), "version"], capture_output=True,
                             text=True, timeout=15, check=True).stdout
    if "Android Debug Bridge version" not in version:
        raise RuntimeError(f"adb version check failed: {version}")
    return adb


def zip_bundle() -> None:
    temporary = ARCHIVE.with_name(ARCHIVE.name + ".part")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED,
                             compresslevel=6) as package:
            for path in sorted(APP_DIR.rglob("*")):
                if path.is_file():
                    package.write(path, path.relative_to(DIST_DIR))
        with zipfile.ZipFile(temporary) as package:
            damaged = package.testzip()
            if damaged:
                raise RuntimeError(f"Damaged ZIP member: {damaged}")
        temporary.replace(ARCHIVE)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    if sys.platform != "win32" or sys.maxsize <= 2**32:
        raise SystemExit("Run this builder with 64-bit Python on Windows")
    if sys.version_info[:3] != (3, 13, 0):
        raise SystemExit("Use Python 3.13.0 to match the bundled license notices")
    os.chdir(PROJECT)
    ensure_sources()
    adb = ensure_platform_tools()
    environment = os.environ.copy()
    environment["SANDEVISTAN_ADB_BINARY"] = str(adb)
    environment.setdefault("PYINSTALLER_CONFIG_DIR", str(PROJECT / ".pyinstaller"))
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm",
                    "--clean", "Sandevistan ADB.spec"], cwd=PROJECT,
                   env=environment, check=True)
    subprocess.run([sys.executable, "scripts/verify_windows_bundle.py",
                    str(APP_DIR)], cwd=PROJECT, check=True)
    zip_bundle()
    print(f"Created and verified {ARCHIVE}")


if __name__ == "__main__":
    main()
