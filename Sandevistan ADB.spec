# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Qt build of Sandevistan ADB."""

from PyInstaller.utils.hooks import collect_all
from pathlib import Path
import os
import sys

VERSION = "0.11.0"

adb_binary = Path(os.environ["SANDEVISTAN_ADB_BINARY"])
adb_notice = adb_binary.parent / "NOTICE.txt"
adb_properties = adb_binary.parent / "source.properties"
adb_support = []
if sys.platform == "win32":
    if adb_binary.name.lower() != "adb.exe":
        raise ValueError("Windows builds require platform-tools/adb.exe")
    for name in ("AdbWinApi.dll", "AdbWinUsbApi.dll"):
        if not (adb_binary.parent / name).is_file():
            raise FileNotFoundError(adb_binary.parent / name)
    adb_support = [
        (str(path), "platform-tools") for path in adb_binary.parent.glob("*.dll")
    ]

qtawesome_datas, qtawesome_binaries, qtawesome_hiddenimports = collect_all("qtawesome")

# QtAwesome loads every font in its registry, even though the UI uses fa6s
# exclusively. The runtime hook narrows that registry; carry only its assets.
SOLID_FONT_FILES = {
    "fontawesome6-solid-webfont-6.7.2.ttf",
    "fontawesome6-solid-webfont-charmap-6.7.2.json",
}
UNUSED_QT_FRAMEWORKS = {
    "QtPdf", "QtQml", "QtQmlMeta", "QtQmlModels", "QtQmlWorkerScript",
    "QtQuick", "QtVirtualKeyboard", "QtVirtualKeyboardQml",
}
UNUSED_QT_PLUGINS = {
    "libqpdf.dylib", "libqtvirtualkeyboardplugin.dylib",
}


def keep_bundle_entry(entry):
    target = entry[0].replace("\\", "/")
    source = str(entry[1]).replace("\\", "/")
    if "qtvirtualkeyboard" in target.casefold() or \
            "qtvirtualkeyboard" in source.casefold():
        return False
    if target.startswith("qtawesome/fonts/"):
        return target.rsplit("/", 1)[-1] in SOLID_FONT_FILES
    # This app uses Qt Widgets; PyInstaller's broad Qt plugin discovery would
    # otherwise pull in QML, PDF and the GPL-only virtual keyboard module.
    if any(
        target == name or source == name
        or f"/{name}.framework/" in target
        or f"/{name}.framework/" in source
        for name in UNUSED_QT_FRAMEWORKS
    ):
        return False
    if target.rsplit("/", 1)[-1] in UNUSED_QT_PLUGINS:
        return False
    return True

a = Analysis(
    ["main_qt.py"],
    pathex=[],
    binaries=[*qtawesome_binaries, (str(adb_binary), "platform-tools"),
              *adb_support],
    datas=[
        ("style.qss", "."),
        ("licenses", "licenses"),
        ("distribution/sources", "licenses/sources"),
        (str(adb_notice), "licenses/platform-tools"),
        (str(adb_properties), "licenses/platform-tools"),
        *qtawesome_datas,
    ],
    hiddenimports=["PySide6.QtSvg", *qtawesome_hiddenimports],
    hookspath=[],
    runtime_hooks=["scripts/qtawesome_solid_only.py"],
    excludes=[
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.QtQuickWidgets",
        "PySide6.QtQuickControls2",
        "PySide6.QtDataVisualization",
        "PySide6.QtVirtualKeyboard",
        "PySide6.QtPdf",
        "PySide6.QtPdfWidgets",
    ],
)
a.binaries = [entry for entry in a.binaries if keep_bundle_entry(entry)]
a.datas = [entry for entry in a.datas if keep_bundle_entry(entry)]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Sandevistan ADB",
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="Sandevistan ADB",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Sandevistan ADB.app",
        icon=None,
        bundle_identifier="local.sandevistan.adb",
        info_plist={
            "CFBundleName": "Sandevistan ADB",
            "CFBundleDisplayName": "Sandevistan ADB",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": "20",
            "CFBundleDevelopmentRegion": "zh_CN",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "12.0",
            "NSHumanReadableCopyright": "",
        },
    )
