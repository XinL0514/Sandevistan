"""Qt interface for the Sandevistan ADB tool.

The visual language is defined by theme.py (tokens) and style.qss (rules).
This module assembles widgets and wires them to AdbClient.

Threading model: adb calls run on a Python thread and publish results to a
queue; a QTimer on the GUI thread drains that queue every 100 ms. Widgets are
only ever touched from the GUI thread.
"""

from __future__ import annotations

import os
import codecs
import json
import posixpath
import queue
import re
import shutil
import sys
import threading
import time
from urllib.parse import urlsplit
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import qtawesome as qta
from PySide6.QtCore import QProcess, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QSyntaxHighlighter, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox,
    QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QLayout, QMainWindow,
    QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSplitter, QStackedWidget,
    QTabWidget,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

import theme
from adb_client import (
    AdbClient, AdbError, AppPermission, AppProcessReport, Device,
    apk_package_name, install_flags, validate_package_name,
)
from permission_labels import permission_label
from dart_network import DartNetworkError, DartNetworkSession, request_key
from logcat_tools import LOGCAT_BUFFERS, logcat_matches, parse_logcat_line
from performance_tools import PerformanceSample, cpu_percent, fps_from_frames
from screenshot_history import ScreenshotHistory, reveal_in_finder


# --- Qt plugin bootstrap -------------------------------------------------

def prepare_qt_plugins() -> None:
    """Make the Qt platform plugins loadable before QApplication starts.

    On macOS, files unpacked into .venv can carry the BSD "hidden" flag, and
    Qt's plugin scanner silently skips hidden files, which makes startup fail
    with "Could not find the Qt platform plugin". Copy the plugins to a
    private cache directory with plain content copies and clear the flag, then
    point Qt at that directory. Must run before QApplication is constructed.
    """
    if sys.platform != "darwin":
        return
    if getattr(sys, "frozen", False):
        return
    import PySide6

    source = Path(PySide6.__file__).parent / "Qt" / "plugins" / "platforms"
    # In a frozen bundle PyInstaller supplies its own plugin path, and there is
    # no source directory to mirror. Leave Qt's default lookup untouched.
    if not source.is_dir():
        return
    dylibs = sorted(source.glob("libq*.dylib"))
    if not dylibs:
        return

    target = Path.home() / ".cache" / "sandevistan-qt-plugins"
    target.mkdir(parents=True, exist_ok=True)
    os.chflags(target, 0)
    for dylib in dylibs:
        destination = target / dylib.name
        shutil.copyfile(dylib, destination)
        os.chflags(destination, 0)
    os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(target)


# --- Stylesheet loading --------------------------------------------------

def _checkmark_url() -> str:
    """Render a white check mark PNG once and return a QSS url() for it."""
    cache = Path.home() / ".cache" / "sandevistan-qt-plugins"
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / "checkbox-check.png"
    if not path.exists():
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QColor, QPainter, QPen, QPixmap

        pixmap = QPixmap(16, 16)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        try:
            pen = QPen(QColor("#FFFFFF"))
            pen.setWidthF(2.0)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(pen)
            painter.drawLine(QPointF(4, 8.5), QPointF(7, 11.5))
            painter.drawLine(QPointF(7, 11.5), QPointF(12, 5))
        finally:
            painter.end()
        pixmap.save(str(path))
    return f'url("{path}")'


def load_stylesheet() -> str:
    """Read style.qss and substitute every @TOKEN with its theme value."""
    raw = (Path(__file__).parent / "style.qss").read_text(encoding="utf-8")

    def replace(match):
        name = match.group(1)
        if name == "CHECK_ICON_URL":
            return _checkmark_url()
        value = getattr(theme, name, None)
        if value is None:
            raise KeyError(f"style.qss references unknown token @{name}")
        return str(value)

    return re.sub(r"@([A-Z][A-Z0-9_]*)", replace, raw)


# --- Data ----------------------------------------------------------------

@dataclass(frozen=True)
class LocalEntry:
    name: str
    path: Path
    is_dir: bool
    size: int | None
    modified: str


def format_size(size: int | None, is_dir: bool) -> str:
    if is_dir or size is None:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def file_kind(name: str, is_dir: bool) -> str:
    if is_dir:
        return "文件夹"
    suffix = Path(name).suffix
    return f"{suffix[1:].upper()} 文件" if suffix else "文件"


def read_local_directory(path: Path) -> list[LocalEntry]:
    entries = []
    for child in path.iterdir():
        try:
            info = child.stat()
            entries.append(LocalEntry(
                child.name, child, child.is_dir(), info.st_size,
                datetime.fromtimestamp(info.st_mtime).strftime("%Y-%m-%d %H:%M"),
            ))
        except OSError:
            continue
    return sorted(entries, key=lambda item: (not item.is_dir, item.name.casefold()))


# --- Small builders ------------------------------------------------------

def label(text: str, name: str | None = None) -> QLabel:
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    return widget


def icon_button(text: str, icon_name: str | None = None, *,
                variant: str = "", color: str | None = None) -> QPushButton:
    button = QPushButton(text)
    button.setCursor(Qt.PointingHandCursor)
    if icon_name:
        button.setIcon(qta.icon(icon_name, color=color or theme.TEXT_MUTED))
        button.setIconSize(QSize(14, 14))
    if variant:
        button.setProperty("variant", variant)
    return button


def divider() -> QFrame:
    line = QFrame()
    line.setObjectName("divider")
    line.setFrameShape(QFrame.HLine)
    return line


def table(columns: list[tuple[str, int]], *,
          stretch_column: int = 0) -> QTreeWidget:
    """A flat, header-only tree used for every list in the app."""
    tree = QTreeWidget()
    tree.setColumnCount(len(columns))
    tree.setHeaderLabels([name for name, _ in columns])
    tree.setRootIsDecorated(False)
    tree.setIndentation(0)
    tree.setUniformRowHeights(True)
    tree.setAlternatingRowColors(False)
    tree.setSelectionBehavior(QAbstractItemView.SelectRows)
    tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
    tree.setFrameShape(QFrame.NoFrame)
    tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    header = tree.header()
    header.setStretchLastSection(False)
    header.setHighlightSections(False)
    for index, (_, width) in enumerate(columns):
        mode = (QHeaderView.Stretch if index == stretch_column
                else QHeaderView.Interactive)
        header.setSectionResizeMode(index, mode)
        tree.setColumnWidth(index, width)
    return tree


def scrollable(page: QWidget, minimum_height: int) -> QScrollArea:
    """Wrap a page so a short window scrolls instead of squeezing widgets."""
    page.setMinimumHeight(minimum_height)
    page.layout().setSizeConstraint(QLayout.SetMinimumSize)
    view = QScrollArea()
    view.setWidgetResizable(True)
    view.setFrameShape(QFrame.NoFrame)
    view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    view.setWidget(page)
    return view


class Card(QFrame):
    """A rounded white surface with consistent inner padding."""

    def __init__(self, *, padding: int = theme.SPACE_6,
                 spacing: int = theme.SPACE_3) -> None:
        super().__init__()
        self.setObjectName("card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(padding, padding, padding, padding)
        self.body.setSpacing(spacing)


class AppMessageBox(QDialog):
    """Small message and confirmation dialog using the app's visual style."""

    Yes = QDialog.Accepted
    No = QDialog.Rejected

    def __init__(self, parent: QWidget, title: str, message: str,
                 kind: str, confirm_text: str | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setObjectName("appMessageDialog")
        self.setMinimumWidth(440)
        self.setMaximumWidth(620)

        body = QVBoxLayout(self)
        body.setContentsMargins(theme.SPACE_6, theme.SPACE_6,
                                theme.SPACE_6, theme.SPACE_6)
        body.setSpacing(theme.SPACE_4)

        icon_name, icon_color = {
            "information": ("fa6s.circle-info", theme.PRIMARY_TEXT),
            "warning": ("fa6s.triangle-exclamation", theme.WARNING),
            "critical": ("fa6s.circle-exclamation", theme.DANGER),
            "question": ("fa6s.circle-question", theme.PRIMARY_TEXT),
        }[kind]
        heading = QHBoxLayout()
        heading.setSpacing(theme.SPACE_3)
        icon = QLabel()
        icon.setPixmap(qta.icon(icon_name, color=icon_color).pixmap(20, 20))
        heading.addWidget(icon)
        heading.addWidget(label(title, "cardTitle"), 1)
        body.addLayout(heading)

        card = Card(padding=theme.SPACE_4)
        content = label(message, "messageText")
        content.setTextFormat(Qt.PlainText)
        content.setWordWrap(True)
        content.setTextInteractionFlags(Qt.TextSelectableByMouse)
        card.body.addWidget(content)
        body.addWidget(card)

        body.addWidget(divider())
        actions = QHBoxLayout()
        actions.addStretch(1)
        if kind == "question":
            cancel = icon_button("取消", None)
            cancel.clicked.connect(self.reject)
            actions.addWidget(cancel)
        confirm = icon_button(confirm_text or "确定", None,
                              variant="primary", color="#FFFFFF")
        confirm.clicked.connect(self.accept)
        actions.addWidget(confirm)
        body.addLayout(actions)

    @classmethod
    def information(cls, parent: QWidget, title: str, message: str) -> int:
        return cls(parent, title, message, "information").exec()

    @classmethod
    def warning(cls, parent: QWidget, title: str, message: str) -> int:
        return cls(parent, title, message, "warning").exec()

    @classmethod
    def critical(cls, parent: QWidget, title: str, message: str) -> int:
        return cls(parent, title, message, "critical").exec()

    @classmethod
    def question(cls, parent: QWidget, title: str, message: str,
                 confirm_text: str = "确认") -> int:
        return cls(parent, title, message, "question", confirm_text).exec()


# --- Pages ---------------------------------------------------------------

class AppsPage(QWidget):
    def __init__(self, window: "MainWindow") -> None:
        super().__init__()
        self.window = window
        self._package_device_serial: str | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SPACE_6)

        columns = QHBoxLayout()
        columns.setSpacing(theme.SPACE_4)
        columns.addWidget(self._install_card(), 3)
        columns.addWidget(self._packages_card(), 2)
        layout.addLayout(columns, 1)

        self.tools_card = self._tools_card()
        layout.addWidget(self.tools_card)
        layout.addWidget(self._log_card(), 0)

    # -- install ---------------------------------------------------------

    def _install_card(self) -> Card:
        card = Card(spacing=theme.SPACE_2)
        card.body.addWidget(label("安装应用", "cardTitle"))
        card.body.addWidget(label(
            "选择本地 APK 并安装到目标设备。", "cardDescription"))
        card.body.addSpacing(theme.SPACE_2)

        card.body.addWidget(label("APK 路径", "fieldLabel"))
        row = QHBoxLayout()
        row.setSpacing(theme.SPACE_2)
        self.apk_path = QLineEdit()
        self.apk_path.setPlaceholderText("尚未选择文件")
        row.addWidget(self.apk_path, 1)
        choose = icon_button("选择…", theme.ICON_FOLDER)
        choose.clicked.connect(self._choose_apk)
        row.addWidget(choose)
        card.body.addLayout(row)

        card.body.addWidget(label("目标设备", "fieldLabel"))
        self.target_mode = QComboBox()
        self.target_mode.addItems(["当前设备", "所有已连接设备", "自定义设备…"])
        self._previous_target_mode = 0
        self.target_mode.currentIndexChanged.connect(self._target_mode_changed)
        card.body.addWidget(self.target_mode)

        card.body.addWidget(label("安装选项", "fieldLabel"))
        options = QHBoxLayout()
        options.setSpacing(theme.SPACE_4)
        self.downgrade_box = QCheckBox("允许降级安装")
        self.keep_data_box = QCheckBox("保留数据")
        self.keep_data_box.setChecked(True)
        options.addWidget(self.downgrade_box)
        options.addWidget(self.keep_data_box)
        options.addStretch(1)
        card.body.addLayout(options)

        card.body.addSpacing(theme.SPACE_2)
        card.body.addWidget(label("将安装到", "fieldLabel"))
        self.target_summary = QFrame()
        self.target_summary.setObjectName("summary")
        summary_row = QHBoxLayout(self.target_summary)
        summary_row.setContentsMargins(
            theme.SPACE_3, theme.SPACE_2, theme.SPACE_3, theme.SPACE_2)
        summary_row.setSpacing(theme.SPACE_3)
        self.summary_icon = QLabel()
        self.summary_icon.setPixmap(qta.icon(
            theme.ICON_DEVICE, color=theme.PRIMARY_TEXT).pixmap(16, 16))
        summary_row.addWidget(self.summary_icon)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        self.summary_title = label("未选择设备", "summaryTitle")
        self.summary_detail = label("连接设备后点击「刷新设备」", "hint")
        self.summary_detail.setWordWrap(True)
        texts.addWidget(self.summary_title)
        texts.addWidget(self.summary_detail)
        summary_row.addLayout(texts, 1)
        card.body.addWidget(self.target_summary)

        card.body.addStretch(1)
        card.body.addWidget(divider())
        actions = QHBoxLayout()
        actions.setSpacing(theme.SPACE_2)
        self.install_status = label("", "hint")
        actions.addWidget(self.install_status)
        actions.addStretch(1)
        install = icon_button("开始安装", theme.ICON_INBOX,
                              variant="primary", color="#FFFFFF")
        install.clicked.connect(self._install_apk)
        actions.addWidget(install)
        self.install_button = install
        card.body.addLayout(actions)
        return card

    def _choose_apk(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 APK", str(Path.home()), "Android APK (*.apk)")
        if path:
            self.apk_path.setText(path)
            self._update_target_summary()

    def _target_mode_changed(self, index: int) -> None:
        if index == 2:
            if self.window.install_targets:
                initial = self.window.install_targets
            elif self._previous_target_mode == 1:
                initial = {device.serial for device in self.window.ready_devices()}
            else:
                current = self.window.current_device()
                initial = {current.serial} if current else set()
            if not self.window.choose_install_targets(initial):
                self.target_mode.blockSignals(True)
                self.target_mode.setCurrentIndex(self._previous_target_mode)
                self.target_mode.blockSignals(False)
                index = self._previous_target_mode
        self._previous_target_mode = index
        self._update_target_summary()

    def _update_target_summary(self) -> None:
        targets = self.window.install_target_devices()
        if not targets:
            self.summary_title.setText("未选择设备")
            self.summary_detail.setText("连接设备后点击「刷新设备」")
            self.install_status.setText("")
            return
        if len(targets) == 1:
            device = targets[0]
            self.summary_title.setText(device.display_name)
            self.summary_detail.setText(f"{device.serial} · 已连接")
        else:
            self.summary_title.setText(f"{len(targets)} 台设备")
            self.summary_detail.setText("、".join(device.label for device in targets))
        self.install_status.setText(f"{len(targets)} 台在线")

    def _install_apk(self) -> None:
        window = self.window
        targets = window.install_target_devices()
        if not targets:
            AppMessageBox.information(self, "安装设备", "请先连接并选择至少一台设备。")
            return
        path = Path(self.apk_path.text().strip()).expanduser()
        if not path.is_file() or path.suffix.lower() != ".apk":
            AppMessageBox.warning(self, "APK 文件", "请选择有效的 .apk 文件。")
            return
        keep_data = self.keep_data_box.isChecked()
        allow_downgrade = self.downgrade_box.isChecked()
        flags = install_flags(
            allow_downgrade=allow_downgrade, keep_data=keep_data)
        package = ""
        if not keep_data:
            try:
                package = apk_package_name(path)
            except AdbError as exc:
                AppMessageBox.warning(self, "APK 文件", str(exc))
                return
            message = (
                f"将从 {len(targets)} 台目标设备卸载 {package}，清除应用数据，"
                "然后安装所选 APK。\n\n"
                "如果新 APK 安装失败，旧版应用和数据不会自动恢复。")
            if AppMessageBox.question(
                    self, "不保留数据安装", message, "卸载后安装") \
                    != AppMessageBox.Yes:
                return

        def work() -> list:
            results = []
            for device in targets:
                try:
                    if not keep_data:
                        installed = window.client.run(
                            device.serial, "shell", "pm", "path", package,
                            timeout=15)
                        if installed and not all(
                                line.startswith("package:")
                                for line in installed.splitlines()):
                            raise AdbError(f"无法确认旧版应用状态：{installed}")
                        if installed:
                            removed = window.client.run(
                                device.serial, "uninstall", package, timeout=60)
                            if "Success" not in removed.splitlines():
                                raise AdbError(
                                    f"卸载旧版未成功：{removed or '设备未返回成功结果'}")
                    output = window.client.run(
                        device.serial, "install", *flags, str(path))
                    ok = "Success" in output and "Failure" not in output
                    if not ok:
                        raise AdbError(output or "设备未返回安装成功结果。")
                    results.append((device, ok, output))
                except AdbError as exc:
                    detail = str(exc)
                    if "INSTALL_FAILED_VERSION_DOWNGRADE" in detail:
                        detail += (
                            "\n已使用 -d，但设备仍拒绝覆盖降级。可取消“保留数据”，"
                            "确认先卸载旧版再安装；这会清除应用数据。"
                            if allow_downgrade else
                            "\n如需覆盖安装低版本，请勾选“允许降级安装”。")
                    results.append((device, False, detail))
            return results

        def done(results: list) -> None:
            failures = []
            for device, ok, detail in results:
                window.log(f"{'✓' if ok else '✗'} {device.display_name} "
                           f"({device.serial})：{'安装成功' if ok else detail}")
                if not ok:
                    failures.append(f"{device.display_name}：{detail}")
            succeeded = len(results) - len(failures)
            window.set_status(f"APK 安装：成功 {succeeded} 台，失败 {len(failures)} 台")
            if failures:
                AppMessageBox.warning(self, "APK 安装结果", "\n".join(failures))

        window.submit("安装 APK", work, done)

    # -- packages --------------------------------------------------------

    def _packages_card(self) -> Card:
        card = Card()
        self.package_count = label("0 个", "hint")
        heading = QHBoxLayout()
        heading.addWidget(label("用户安装的应用", "cardTitle"))
        heading.addStretch(1)
        heading.addWidget(self.package_count)
        card.body.addLayout(heading)
        card.body.addWidget(label(
            "列出设备上已安装的第三方应用。", "cardDescription"))

        search = QHBoxLayout()
        search.setSpacing(theme.SPACE_2)
        self.package_search = QLineEdit()
        self.package_search.setPlaceholderText("搜索包名")
        self.package_search.textChanged.connect(self._filter_packages)
        search.addWidget(self.package_search, 1)
        load = icon_button("加载", theme.ICON_REFRESH)
        load.clicked.connect(self._load_packages)
        search.addWidget(load)
        card.body.addLayout(search)

        self.package_tree = table([("包名", 300)], stretch_column=0)
        self.package_tree.itemSelectionChanged.connect(self._package_selected)
        card.body.addWidget(self.package_tree, 1)

        footer = QHBoxLayout()
        self.package_hint = label("选择一项后卸载", "hint")
        footer.addWidget(self.package_hint)
        footer.addStretch(1)
        tools = icon_button("应用工具", "fa6s.wrench")
        tools.clicked.connect(self._focus_tools)
        footer.addWidget(tools)
        uninstall = icon_button("卸载选中", "fa6s.trash-can",
                                variant="danger", color=theme.DANGER)
        uninstall.clicked.connect(self._uninstall)
        footer.addWidget(uninstall)
        card.body.addLayout(footer)
        return card

    def _load_packages(self) -> None:
        window = self.window
        serial = window.selected_serial()
        if serial is None:
            return
        started = time.monotonic()

        def work() -> list:
            return window.client.list_user_packages(serial)

        def done(packages: list) -> None:
            current = window.current_device()
            if current is None or current.serial != serial:
                return
            self.package_tree.clear()
            for package in packages:
                item = QTreeWidgetItem([package])
                item.setIcon(0, qta.icon(theme.ICON_APPS,
                                         color=theme.TEXT_MUTED))
                self.package_tree.addTopLevelItem(item)
            self.package_count.setText(f"{len(packages)} 个")
            self.package_hint.setText("选择一项后卸载")
            self._filter_packages(self.package_search.text())
            elapsed = time.monotonic() - started
            window.log(f"读取应用列表耗时 {elapsed:.1f} 秒")
            window.set_status(
                f"找到 {len(packages)} 个用户安装的应用 · {elapsed:.1f} 秒")

        window.submit("读取应用列表", work, done)

    def _filter_packages(self, text: str) -> None:
        needle = text.strip().lower()
        visible = 0
        for index in range(self.package_tree.topLevelItemCount()):
            item = self.package_tree.topLevelItem(index)
            match = needle in item.text(0).lower()
            item.setHidden(not match)
            visible += match
        self.package_hint.setText(
            f"匹配 {visible} 项" if needle else "选择一项后卸载")

    def _package_selected(self) -> None:
        items = self.package_tree.selectedItems()
        self.package_hint.setText(
            items[0].text(0) if items else "选择一项后卸载")
        if items:
            self.tool_package.setText(items[0].text(0))

    def _uninstall(self) -> None:
        window = self.window
        serial = window.selected_serial()
        if serial is None:
            return
        items = self.package_tree.selectedItems()
        if not items:
            AppMessageBox.information(self, "卸载应用", "请先选择一个应用。")
            return
        try:
            package = validate_package_name(items[0].text(0))
        except AdbError as exc:
            AppMessageBox.warning(self, "应用包名", str(exc))
            return
        if AppMessageBox.question(
                self, "确认卸载", f"从设备上卸载 {package}？", "卸载") \
                != AppMessageBox.Yes:
            return

        def done(_result: str) -> None:
            row = self.package_tree.indexOfTopLevelItem(items[0])
            if row >= 0:
                self.package_tree.takeTopLevelItem(row)
            self.package_count.setText(
                f"{self.package_tree.topLevelItemCount()} 个")
            if self.tool_package.text() == package:
                self.tool_package.clear()

        window.submit("卸载应用", lambda: window.client.run(
            serial, "uninstall", package), done)

    # -- app tools -------------------------------------------------------

    def _focus_tools(self) -> None:
        self.tool_package.setFocus()
        view = self.window.pages.widget(0)
        if isinstance(view, QScrollArea):
            view.ensureWidgetVisible(self.tools_card, 0, theme.SPACE_4)

    def _tools_card(self) -> Card:
        card = Card(padding=theme.SPACE_4)
        card.body.addWidget(label("应用工具", "cardTitle"))
        card.body.addWidget(label(
            "针对顶部当前设备执行。可从右侧列表选包名，也可手动输入。",
            "cardDescription"))

        fields = QHBoxLayout()
        fields.setSpacing(theme.SPACE_3)
        package_fields = QVBoxLayout()
        package_fields.addWidget(label("应用包名", "fieldLabel"))
        self.tool_package = QLineEdit()
        self.tool_package.setPlaceholderText("com.example.app")
        package_fields.addWidget(self.tool_package)
        fields.addLayout(package_fields, 1)
        activity_fields = QVBoxLayout()
        activity_fields.addWidget(label("Activity（可选）", "fieldLabel"))
        self.tool_activity = QLineEdit()
        self.tool_activity.setPlaceholderText(".MainActivity；留空启动默认入口")
        activity_fields.addWidget(self.tool_activity)
        fields.addLayout(activity_fields, 1)
        card.body.addLayout(fields)

        actions = QHBoxLayout()
        actions.setSpacing(theme.SPACE_2)
        for title, icon, handler, variant in (
            ("启动 App", "fa6s.play", self._start_app, "primary"),
            ("强制停止", "fa6s.stop", self._stop_app, ""),
            ("清除缓存", "fa6s.broom", lambda: self._clear_app(True), ""),
            ("清除数据", "fa6s.trash-can", lambda: self._clear_app(False), "danger"),
        ):
            button = icon_button(title, icon, variant=variant,
                                 color="#FFFFFF" if variant == "primary" else None)
            button.clicked.connect(handler)
            actions.addWidget(button)
        actions.addStretch(1)
        card.body.addLayout(actions)
        self.tool_feedback = label("", "hint")
        self.tool_feedback.setWordWrap(True)
        card.body.addWidget(self.tool_feedback)

        inspect = QHBoxLayout()
        inspect.setSpacing(theme.SPACE_2)
        foreground = icon_button("读取前台 Activity", "fa6s.crosshairs")
        foreground.clicked.connect(self._foreground_activity)
        inspect.addWidget(foreground)
        self.foreground_value = QLineEdit()
        self.foreground_value.setReadOnly(True)
        self.foreground_value.setPlaceholderText("点击左侧按钮查看当前页面")
        inspect.addWidget(self.foreground_value, 1)
        permissions = icon_button("应用权限", "fa6s.shield-halved")
        permissions.clicked.connect(self._show_permissions)
        inspect.addWidget(permissions)
        processes = icon_button("进程与内存", "fa6s.chart-simple")
        processes.clicked.connect(self._show_processes)
        inspect.addWidget(processes)
        card.body.addLayout(inspect)
        return card

    def _tool_target(self) -> tuple[str, str] | None:
        serial = self.window.selected_serial()
        if serial is None:
            return None
        try:
            package = validate_package_name(self.tool_package.text())
        except AdbError as exc:
            AppMessageBox.warning(self, "应用包名", str(exc))
            return None
        return serial, package

    def _start_app(self) -> None:
        target = self._tool_target()
        if target is None:
            return
        serial, package = target
        activity = self.tool_activity.text().strip()
        self.tool_feedback.setText(f"正在设备 {serial} 上启动 {package}…")

        def work() -> tuple[str, str | None]:
            output = self.window.client.start_app(serial, package, activity)
            try:
                foreground = self.window.client.foreground_activity(serial)
            except AdbError:
                foreground = None
            return output, foreground

        def done(result: tuple[str, str | None]) -> None:
            output, foreground = result
            self.window.log(output)
            if foreground and foreground.startswith(package + "/"):
                already_open = (
                    "intent has been delivered to currently running top-most instance"
                    in output
                )
                action = "已在设备前台" if already_open else "已打开"
                self.tool_feedback.setText(
                    f"{package} {action} · {serial} · {foreground}")
            elif not foreground or foreground == "未找到前台 Activity":
                self.tool_feedback.setText(
                    f"启动命令已发送到 {serial}，但未能确认前台页面；"
                    "请检查设备是否亮屏并已解锁。")
            else:
                self.tool_feedback.setText(
                    f"启动命令已发送到 {serial}，但当前前台是 {foreground}；"
                    "请检查顶部选择的设备。")

        started = self.window.submit(
            f"启动 {package}",
            work,
            done,
            finished=lambda: self.tool_feedback.setText("启动失败，请查看错误提示。"),
            log_result=False,
        )
        if not started:
            self.tool_feedback.setText("已有任务正在执行，请稍候再启动。")

    def _stop_app(self) -> None:
        target = self._tool_target()
        if target is None:
            return
        serial, package = target
        self.window.submit(f"强制停止 {package}", lambda: self.window.client.stop_app(
            serial, package), log_result=False)

    def _clear_app(self, cache_only: bool) -> None:
        target = self._tool_target()
        if target is None:
            return
        serial, package = target
        if cache_only:
            title = "清除应用缓存"
            message = f"清除 {package} 的缓存？应用数据会保留。"
            confirm = "清除缓存"
        else:
            title = "清除应用数据"
            message = f"清除 {package} 的全部应用数据？登录状态和本地设置也会删除。"
            confirm = "清除数据"
        if AppMessageBox.question(self, title, message, confirm) != AppMessageBox.Yes:
            return
        self.window.submit(f"{title} · {package}", lambda: self.window.client.clear_app(
            serial, package, cache_only=cache_only), log_result=False)

    def _foreground_activity(self) -> None:
        serial = self.window.selected_serial()
        if serial is None:
            return
        self.window.submit("读取前台 Activity",
                           lambda: self.window.client.foreground_activity(serial),
                           lambda result: self.foreground_value.setText(result))

    def _show_permissions(self) -> None:
        target = self._tool_target()
        if target is None:
            return
        serial, package = target
        self.window.submit("读取应用权限",
                           lambda: self.window.client.app_permissions(serial, package),
                           lambda permissions: AppPermissionsDialog(
                               self.window, serial, package, permissions).exec(),
                           log_result=False)

    def _show_processes(self) -> None:
        target = self._tool_target()
        if target is None:
            return
        serial, package = target
        self.window.submit("读取进程与内存",
                           lambda: self.window.client.app_process_report(serial, package),
                           lambda report: AppProcessDialog(
                               self.window, serial, package, report).exec(),
                           log_result=False)

    def device_changed(self, serial: str | None) -> None:
        if serial == self._package_device_serial:
            return
        self._package_device_serial = serial
        self.package_tree.clear()
        self.package_count.setText("0 个")
        self.package_hint.setText("选择一项后卸载")
        self.tool_package.clear()
        self.foreground_value.clear()

    # -- log -------------------------------------------------------------

    def _log_card(self) -> Card:
        card = Card(padding=theme.SPACE_4)
        head = QHBoxLayout()
        head.addWidget(label("活动记录", "cardTitle"))
        head.addStretch(1)
        self.log_status = label("就绪", "hint")
        head.addWidget(self.log_status)
        card.body.addLayout(head)

        log = QPlainTextEdit()
        log.setObjectName("logView")
        log.setReadOnly(True)
        log.setMinimumHeight(64)
        log.setMaximumHeight(132)
        log.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        card.body.addWidget(log)
        self.window.log_view = log
        return card


class ShotPage(QWidget):
    def __init__(self, window: "MainWindow") -> None:
        super().__init__()
        self.window = window
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SPACE_4)

        layout.addWidget(self._capture_card())
        layout.addWidget(self._history_card(), 1)

    def _capture_card(self) -> Card:
        card = Card()
        row = QHBoxLayout()
        row.setSpacing(theme.SPACE_6)
        texts = QVBoxLayout()
        texts.setSpacing(theme.SPACE_2)
        texts.addWidget(label("把当前屏幕保存到 Mac", "cardTitle"))
        texts.addWidget(label(
            "选择保存位置，完成后即可在下方历史记录中找到截图。",
            "cardDescription"))
        texts.addSpacing(theme.SPACE_2)
        button_row = QHBoxLayout()
        capture = icon_button("截取当前屏幕", theme.ICON_SHOT,
                              variant="primary", color="#FFFFFF")
        capture.setMinimumWidth(148)
        capture.clicked.connect(self._take_screenshot)
        button_row.addWidget(capture)
        button_row.addStretch(1)
        texts.addLayout(button_row)
        row.addLayout(texts, 1)
        row.addWidget(self._illustration())
        card.body.addLayout(row)
        return card

    def _illustration(self) -> QWidget:
        preview = QWidget()
        preview.setFixedSize(104, 104)
        preview.setStyleSheet(
            f"background: {theme.PRIMARY_SOFT};"
            f"border: 1px solid {theme.BORDER};"
            f"border-radius: {theme.RADIUS_CARD}px;"
        )
        inner = QVBoxLayout(preview)
        inner.setContentsMargins(0, 0, 0, 0)
        glyph = QLabel()
        glyph.setAlignment(Qt.AlignCenter)
        glyph.setPixmap(qta.icon(
            theme.ICON_SHOT, color=theme.PRIMARY).pixmap(36, 36))
        inner.addWidget(glyph)
        return preview

    def _history_card(self) -> Card:
        card = Card()
        self.record_count = label("", "hint")
        head = QHBoxLayout()
        head.addWidget(label("截图历史", "cardTitle"))
        head.addWidget(self.record_count)
        head.addStretch(1)
        reveal = icon_button("在 Finder 中显示", theme.ICON_FILES,
                             variant="ghost")
        reveal.clicked.connect(self._reveal)
        head.addWidget(reveal)
        card.body.addLayout(head)

        self.history_tree = table([
            ("文件名", 220), ("保存时间", 150), ("设备", 140),
            ("保存文件夹", 260)])
        self.history_tree.itemDoubleClicked.connect(lambda _i, _c: self._reveal())
        self.history_tree.itemSelectionChanged.connect(self._selection_changed)
        card.body.addWidget(self.history_tree, 1)
        self.path_hint = label("选择一条记录查看完整保存路径", "hint")
        card.body.addWidget(self.path_hint)

        self._reload_history()
        return card

    def _reload_history(self) -> None:
        records = self.window.screenshot_history.entries
        self.history_tree.clear()
        for record in records:
            item = QTreeWidgetItem([
                record.path.name, record.display_time, record.device_name,
                str(record.path.parent)])
            item.setIcon(0, qta.icon("fa6s.image", color=theme.TEXT_MUTED))
            item.setData(0, Qt.UserRole, record)
            self.history_tree.addTopLevelItem(item)
        if not records:
            self.history_tree.addTopLevelItem(QTreeWidgetItem(
                ["还没有截图记录", "", "", "点击上方按钮截取当前屏幕"]))
        self.record_count.setText(f"{len(records)} 张截图")

    def _take_screenshot(self) -> None:
        window = self.window
        serial = window.selected_serial()
        if serial is None:
            return
        device = window.current_device()
        downloads = Path.home() / "Downloads"
        start = downloads if downloads.is_dir() else Path.home()
        suggested = start / f"截图_{datetime.now():%Y%m%d_%H%M%S}.png"
        path, _ = QFileDialog.getSaveFileName(
            self, "保存截图", str(suggested), "PNG 图片 (*.png)")
        if not path:
            return
        destination = Path(path).expanduser().resolve()

        def work() -> str:
            window.client.screenshot(serial, destination)
            return str(destination)

        def done(result: str) -> None:
            try:
                window.screenshot_history.add(destination, device.display_name)
            except OSError as exc:
                AppMessageBox.warning(
                    self, "截图历史",
                    f"截图已保存，但历史记录写入失败：{exc}")
            self._reload_history()
            window.log(f"✓ 截图已保存：{result}")
            window.set_status("截图已保存")

        window.submit("保存截图", work, done)

    def _selection_changed(self) -> None:
        items = self.history_tree.selectedItems()
        record = items[0].data(0, Qt.UserRole) if items else None
        self.path_hint.setText(
            str(record.path) if record
            else "选择一条记录查看完整保存路径")

    def _reveal(self) -> None:
        items = self.history_tree.selectedItems()
        record = items[0].data(0, Qt.UserRole) if items else None
        if record is None:
            AppMessageBox.information(self, "截图历史", "请先选择一条截图记录。")
            return
        window = self.window

        def done(result: str) -> None:
            window.log(f"已在 Finder 中显示：{result}")

        window.submit("在 Finder 中显示截图",
                      lambda: reveal_in_finder(record.path), done)


class FilePane:
    """One side of the file manager: local disk or the connected device."""

    def __init__(self, page: "FilesPage", *, local: bool) -> None:
        self.page = page
        self.window = page.window
        self.local = local
        self.directory = Path.home() if local else Path("/sdcard/")
        self.history: list = []
        self.all_entries: list = []
        self.is_busy = False

    def build(self) -> QWidget:
        card = Card(padding=theme.SPACE_4)
        head = QHBoxLayout()
        self.title_label = label("本机 · Mac" if self.local else "设备文件",
                                 "cardTitle")
        head.addWidget(self.title_label)
        head.addStretch(1)
        self.count_label = label("0 项", "hint")
        head.addWidget(self.count_label)
        card.body.addLayout(head)

        path_row = QHBoxLayout()
        path_row.setSpacing(theme.SPACE_2)
        self.path_edit = QLineEdit(str(self.directory))
        self.path_edit.setStyleSheet(
            f"font-family: {theme.FONT_MONO}; font-size: 10pt;")
        self.path_edit.returnPressed.connect(self.go)
        path_row.addWidget(self.path_edit, 1)
        go = icon_button("前往", None)
        go.setFixedWidth(58)
        go.clicked.connect(self.go)
        path_row.addWidget(go)
        card.body.addLayout(path_row)

        nav = QHBoxLayout()
        nav.setSpacing(theme.SPACE_2)
        for text, icon_name, handler in (
            ("主页", "fa6s.house", self.home),
            ("返回", "fa6s.arrow-left", self.back),
            ("上级", "fa6s.arrow-up", self.parent),
            ("刷新", theme.ICON_REFRESH, self.refresh),
        ):
            button = icon_button(text, icon_name)
            button.setFixedHeight(theme.CONTROL_HEIGHT)
            button.setStyleSheet("padding: 0 10px; font-size: 10pt;")
            button.clicked.connect(handler)
            nav.addWidget(button)
        nav.addStretch(1)
        card.body.addLayout(nav)

        self.tree = table([("名称", 118), ("大小", 56), ("修改时间", 136)])
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.tree.itemSelectionChanged.connect(self._selection_changed)
        card.body.addWidget(self.tree, 1)

        footer = QHBoxLayout()
        self.info_label = label("", "hint")
        footer.addWidget(self.info_label)
        footer.addStretch(1)
        if not self.local:
            self.delete_button = icon_button(
                "删除选中", "fa6s.trash-can", variant="danger", color=theme.DANGER)
            self.delete_button.setEnabled(False)
            self.delete_button.clicked.connect(self.page.delete_remote_selected)
            footer.addWidget(self.delete_button)
        self.hidden_box = QCheckBox("显示隐藏文件")
        self.hidden_box.toggled.connect(lambda _state: self.render())
        footer.addWidget(self.hidden_box)
        card.body.addLayout(footer)
        return card

    # -- rendering -------------------------------------------------------

    def _visible(self, entries: list) -> list:
        if self.hidden_box.isChecked():
            return entries
        return [entry for entry in entries if not entry.name.startswith(".")]

    def render(self) -> None:
        self.tree.clear()
        entries = self._visible(self.all_entries)
        for entry in entries:
            item = QTreeWidgetItem([
                entry.name + ("/" if entry.is_dir else ""),
                format_size(entry.size, entry.is_dir),
                entry.modified,
            ])
            item.setIcon(0, qta.icon(
                theme.ICON_FOLDER if entry.is_dir else "fa6s.file",
                color=theme.TEXT_MUTED))
            item.setData(0, Qt.UserRole, entry)
            self.tree.addTopLevelItem(item)
        self.count_label.setText(f"{len(self.all_entries)} 项")
        self.info_label.setText(
            "连接设备后即可浏览文件" if not self.local
            else "双击文件夹进入")
        self.path_edit.setText(str(self.directory))

    def _selection_changed(self) -> None:
        count = len(self.tree.selectedItems())
        if not self.local:
            self.delete_button.setEnabled(bool(count) and not self.window.busy)
        if count:
            self.info_label.setText(f"已选 {count} 项 · 共 {len(self.all_entries)} 项")
        else:
            self.info_label.setText(
                "双击文件夹进入" if self.local else "连接设备后即可浏览文件")

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        entry = item.data(0, Qt.UserRole)
        if entry is not None and entry.is_dir:
            self.open_path(entry.path if self.local else entry.path)

    def selected_entries(self) -> list:
        return [item.data(0, Qt.UserRole)
                for item in self.tree.selectedItems()
                if item.data(0, Qt.UserRole) is not None]

    # -- navigation ------------------------------------------------------

    def go(self) -> None:
        self.open_path(self.path_edit.text())

    def home(self) -> None:
        self.open_path(Path.home() if self.local else "/sdcard/")

    def parent(self) -> None:
        if self.local:
            self.open_path(self.directory.parent)
        else:
            self.open_path(posixpath.dirname(
                str(self.directory).rstrip("/")) or "/")

    def back(self) -> None:
        if self.history and not self.is_busy:
            self.open_path(self.history.pop(), remember=False)

    def refresh(self) -> None:
        self.open_path(self.directory, remember=False)

    def open_path(self, raw, *, remember: bool = True) -> None:
        if self.local:
            self._open_local(raw, remember=remember)
        else:
            self._open_remote(raw, remember=remember)

    def _open_local(self, raw, *, remember: bool) -> None:
        path = Path(str(raw).strip()).expanduser()
        if not path.is_absolute():
            path = self.directory / path
        try:
            path = path.resolve()
            entries = read_local_directory(path)
        except OSError:
            AppMessageBox.warning(
                self.page, "本机路径", f"无法打开文件夹：{path}")
            return
        if remember and path != self.directory:
            self.history.append(self.directory)
        self.directory = path
        self.all_entries = entries
        self.render()

    def _open_remote(self, raw, *, remember: bool) -> None:
        window = self.window
        if window.client is None:
            return
        device = window.current_device()
        if device is None:
            # No device selected yet (for example while the first refresh is
            # still running). Leave the pane empty instead of interrupting.
            self.all_entries = []
            self.render()
            self.info_label.setText("连接设备后即可浏览文件")
            return
        if device.state != "device":
            AppMessageBox.warning(
                self.page, "设备尚未就绪",
                f"当前状态：{device.state}。请在手机上允许 USB 调试。")
            return
        serial = device.serial
        raw = str(raw).strip()
        if not raw:
            AppMessageBox.warning(self.page, "设备路径", "请输入要打开的设备文件夹路径。")
            return
        directory = posixpath.normpath(
            raw if raw.startswith("/")
            else posixpath.join(str(self.directory), raw))
        if self.is_busy:
            return

        def work() -> list:
            return window.client.list_directory(serial, directory)

        def done(entries: list) -> None:
            if remember and directory != self.directory:
                self.history.append(self.directory)
            self.directory = Path(directory)
            self.all_entries = sorted(
                entries, key=lambda item: (not item.is_dir, item.name.casefold()))
            self.render()

        self.is_busy = True
        window.submit("读取设备目录", work, done,
                      finished=lambda: setattr(self, "is_busy", False))


class FilesPage(QWidget):
    def __init__(self, window: "MainWindow") -> None:
        super().__init__()
        self.window = window
        self.local = FilePane(self, local=True)
        self.remote = FilePane(self, local=False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SPACE_4)
        layout.addWidget(self.local.build(), 1)
        layout.addLayout(self._transfer_column(), 0)
        layout.addWidget(self.remote.build(), 1)

        # The local pane has no device dependency, so load it immediately.
        self.local.refresh()

    def _transfer_column(self) -> QVBoxLayout:
        column = QVBoxLayout()
        column.setSpacing(theme.SPACE_2)
        column.addStretch(1)

        upload = icon_button("上传", "fa6s.arrow-right",
                             variant="primary", color="#FFFFFF")
        upload.setFixedWidth(76)
        upload.clicked.connect(self.upload_selected)
        column.addWidget(upload)
        column.addSpacing(theme.SPACE_1)

        download = icon_button("下载", "fa6s.arrow-left",
                               color=theme.TEXT_MUTED)
        download.setFixedWidth(76)
        download.clicked.connect(self.download_selected)
        column.addWidget(download)

        column.addStretch(1)
        return column

    def upload_selected(self) -> None:
        window = self.window
        serial = window.selected_serial()
        if serial is None:
            return
        entries = self.local.selected_entries()
        if not entries:
            AppMessageBox.information(
                self, "选择文件", "请先在左侧选中要上传的文件或文件夹。")
            return
        destination = str(self.remote.directory)
        existing = {entry.name for entry in self.remote.all_entries}
        conflicts = [entry.name for entry in entries if entry.name in existing]
        if conflicts and AppMessageBox.question(
            self, "目标已存在",
            f"设备上已有 {len(conflicts)} 个同名项。继续上传可能覆盖文件，是否继续？",
            "继续",
        ) != AppMessageBox.Yes:
            return

        def work() -> str:
            for entry in entries:
                window.client.run(serial, "push", str(entry.path),
                                  destination, timeout=None)
            return f"{len(entries)} 项已上传"

        def done(result: str) -> None:
            window.log(f"✓ {result} → {destination}")
            self.remote.refresh()

        window.submit("上传文件", work, done)

    def download_selected(self) -> None:
        window = self.window
        serial = window.selected_serial()
        if serial is None:
            return
        entries = self.remote.selected_entries()
        if not entries:
            AppMessageBox.information(
                self, "选择文件", "请先在右侧选中要下载的文件或文件夹。")
            return
        destination = self.local.directory
        conflicts = [entry.name for entry in entries
                     if (destination / entry.name).exists()]
        if conflicts and AppMessageBox.question(
            self, "目标已存在",
            f"Mac 上已有 {len(conflicts)} 个同名项。继续下载可能覆盖文件，是否继续？",
            "继续",
        ) != AppMessageBox.Yes:
            return

        def work() -> str:
            for entry in entries:
                window.client.run(serial, "pull", entry.path,
                                  str(destination), timeout=None)
            return f"{len(entries)} 项已下载"

        def done(result: str) -> None:
            window.log(f"✓ {result} → {destination}")
            self.local.refresh()

        window.submit("下载文件", work, done)

    def delete_remote_selected(self) -> None:
        window = self.window
        if window.busy:
            return
        serial = window.selected_serial()
        if serial is None:
            return
        entries = self.remote.selected_entries()
        if not entries:
            AppMessageBox.information(
                self, "选择文件", "请先在右侧选中要删除的设备文件或文件夹。")
            return
        directory = str(self.remote.directory)
        names = "\n".join(f"• {entry.name}/" if entry.is_dir else f"• {entry.name}"
                          for entry in entries[:8])
        if len(entries) > 8:
            names += f"\n…另有 {len(entries) - 8} 项"
        if AppMessageBox.question(
            self, "删除设备文件",
            f"将从设备 {serial} 永久删除以下 {len(entries)} 项：\n{names}\n"
            "文件夹及其中内容也会删除，此操作无法撤销。",
            "删除",
        ) != AppMessageBox.Yes:
            return

        def work() -> tuple[list[str], list[tuple[str, str]]]:
            deleted = []
            failed = []
            for entry in entries:
                try:
                    window.client.remove_remote_entry(serial, directory, entry)
                    deleted.append(entry.name)
                except AdbError as exc:
                    failed.append((entry.name, str(exc)))
            return deleted, failed

        def done(result: tuple[list[str], list[tuple[str, str]]]) -> None:
            deleted, failed = result
            window.log(f"删除设备文件：成功 {len(deleted)} 项，失败 {len(failed)} 项"
                       f" · {serial} · {directory}")
            for name, detail in failed:
                window.log(f"✗ {name}：{detail}")
            self.remote.refresh()
            if failed:
                details = "\n".join(f"{name}：{detail}" for name, detail in failed[:3])
                if len(failed) > 3:
                    details += f"\n…另有 {len(failed) - 3} 项失败"
                AppMessageBox.warning(
                    self, "删除设备文件", f"已删除 {len(deleted)} 项，失败 {len(failed)} 项。\n{details}")

        if window.submit("删除设备文件", work, done, log_result=False):
            self.remote.delete_button.setEnabled(False)


# --- Dialogs -------------------------------------------------------------

class WifiDialog(QDialog):
    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self.window = window
        self.setWindowTitle("Wi-Fi 连接设备")
        self.setObjectName("wifiDialog")
        self.setMinimumWidth(460)

        body = QVBoxLayout(self)
        body.setContentsMargins(theme.SPACE_6, theme.SPACE_6,
                                theme.SPACE_6, theme.SPACE_6)
        body.setSpacing(theme.SPACE_3)
        body.addWidget(label("Wi-Fi 连接", "pageTitle"))
        body.addWidget(label("Mac 和 Android 设备需连接同一 Wi-Fi 网络。",
                             "pageSubtitle"))

        pair = Card(padding=theme.SPACE_4)
        pair.body.addWidget(label("Android 11 及以上 · 无线调试配对", "cardTitle"))
        pair.body.addWidget(label(
            "在手机「无线调试」中点「使用配对码配对设备」，填写弹出的 IP、端口和配对码。",
            "cardDescription"))
        self.pair_endpoint = self._field(pair, "配对地址（IP:配对端口）")
        self.pair_code = self._field(pair, "6 位配对码")
        pair_button = icon_button("配对", None, variant="primary",
                                  color="#FFFFFF")
        pair_button.clicked.connect(self._pair)
        pair.body.addWidget(pair_button)
        body.addWidget(pair)

        connect = Card(padding=theme.SPACE_4)
        connect.body.addWidget(label("连接设备", "cardTitle"))
        connect.body.addWidget(label(
            "填写无线调试主页显示的 IP:端口（与配对端口不同）。旧版设备可先通过 USB 开启 5555 端口。",
            "cardDescription"))
        self.connect_endpoint = self._field(connect, "连接地址（IP:连接端口）")
        actions = QHBoxLayout()
        actions.setSpacing(theme.SPACE_2)
        connect_button = icon_button("连接", None, variant="primary",
                                     color="#FFFFFF")
        connect_button.clicked.connect(self._connect)
        actions.addWidget(connect_button)
        tcpip = icon_button("USB 开启 5555", None)
        tcpip.clicked.connect(self._enable_tcpip)
        actions.addWidget(tcpip)
        actions.addStretch(1)
        disconnect = icon_button("断开当前 Wi-Fi", None, variant="ghost")
        disconnect.clicked.connect(self._disconnect)
        actions.addWidget(disconnect)
        connect.body.addLayout(actions)
        body.addWidget(connect)

        close = icon_button("关闭", None)
        close.clicked.connect(self.accept)
        footer = QHBoxLayout()
        footer.addStretch(1)
        footer.addWidget(close)
        body.addLayout(footer)

    def _field(self, card: Card, text: str) -> QLineEdit:
        card.body.addWidget(label(text, "fieldLabel"))
        field = QLineEdit()
        card.body.addWidget(field)
        return field

    def _pair(self) -> None:
        window = self.window

        def done(result: str) -> None:
            window.log(f"✓ Wi-Fi 配对成功：{result}")
            window.set_status("配对成功，请在下方填写连接地址")

        window.submit("Wi-Fi 配对", lambda: window.client.pair_wifi(
            self.pair_endpoint.text(), self.pair_code.text()), done)

    def _connect(self) -> None:
        window = self.window
        endpoint = self.connect_endpoint.text().strip()

        def done(result: str) -> None:
            window.log(f"✓ Wi-Fi 连接：{result}")
            window.refresh_devices()

        window.submit("Wi-Fi 连接",
                      lambda: window.client.connect_wifi(endpoint), done)

    def _enable_tcpip(self) -> None:
        window = self.window
        serial = window.selected_serial()
        if serial is None:
            return
        device = window.current_device()
        if device is not None and device.is_wifi:
            AppMessageBox.information(self, "USB 连接", "请先在顶部选择 USB 连接的设备。")
            return
        window.submit("开启 USB 设备的 5555 端口",
                      lambda: window.client.enable_tcpip(serial))

    def _disconnect(self) -> None:
        window = self.window
        device = window.current_device()
        if device is None or not device.is_wifi:
            AppMessageBox.information(self, "Wi-Fi 断开", "请先在顶部选择一台 Wi-Fi 设备。")
            return

        def done(result: str) -> None:
            window.log(f"✓ 已断开：{result}")
            window.refresh_devices()

        window.submit("Wi-Fi 断开",
                      lambda: window.client.disconnect_wifi(device.serial), done)


class TargetsDialog(QDialog):
    def __init__(self, window: "MainWindow", initial: set[str]) -> None:
        super().__init__(window)
        self.setWindowTitle("选择安装设备")
        self.setObjectName("targetsDialog")
        self.setMinimumWidth(560)
        self.result_serials: set[str] | None = None
        self.device_checks: dict[str, QCheckBox] = {}
        devices = window.ready_devices()

        body = QVBoxLayout(self)
        body.setContentsMargins(theme.SPACE_6, theme.SPACE_6,
                                theme.SPACE_6, theme.SPACE_6)
        body.setSpacing(theme.SPACE_4)
        body.addWidget(label("选择安装设备", "pageTitle"))
        body.addWidget(label("所选设备都会收到同一个 APK。", "pageSubtitle"))

        card = Card(padding=theme.SPACE_4)
        heading = QHBoxLayout()
        heading.addWidget(label("已连接设备", "cardTitle"))
        heading.addWidget(label(f"{len(devices)} 台在线", "accent"))
        heading.addStretch(1)
        select_all = icon_button("全选", None)
        select_all.clicked.connect(lambda: self._set_all(Qt.Checked))
        clear = icon_button("清空", None)
        clear.clicked.connect(lambda: self._set_all(Qt.Unchecked))
        heading.addWidget(select_all)
        heading.addWidget(clear)
        card.body.addLayout(heading)

        device_area = QScrollArea()
        device_area.setWidgetResizable(True)
        device_area.setFrameShape(QFrame.NoFrame)
        device_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        device_area.setFixedHeight(
            min(len(devices), 6) * (theme.ROW_HEIGHT + theme.SPACE_8))
        rows = QWidget()
        rows_layout = QVBoxLayout(rows)
        rows_layout.setContentsMargins(0, 0, 0, 0)
        rows_layout.setSpacing(theme.SPACE_2)
        for device in devices:
            row = QFrame()
            row.setObjectName("targetDeviceRow")
            row_layout = QVBoxLayout(row)
            row_layout.setContentsMargins(theme.SPACE_3, theme.SPACE_2,
                                          theme.SPACE_3, theme.SPACE_2)
            row_layout.setSpacing(theme.SPACE_1)
            check = QCheckBox(device.display_name)
            check.setChecked(device.serial in initial)
            check.toggled.connect(self._update_selected_count)
            row_layout.addWidget(check)
            detail = label(
                f"{'Wi-Fi' if device.is_wifi else 'USB'} · {device.serial}",
                "hint")
            detail.setContentsMargins(theme.SPACE_6, 0, 0, 0)
            row_layout.addWidget(detail)
            rows_layout.addWidget(row)
            self.device_checks[device.serial] = check
        rows_layout.addStretch(1)
        device_area.setWidget(rows)
        card.body.addWidget(device_area)
        body.addWidget(card)

        body.addWidget(divider())
        actions = QHBoxLayout()
        actions.setSpacing(theme.SPACE_2)
        self.selected_count = label("", "hint")
        self._update_selected_count()
        actions.addWidget(self.selected_count)
        actions.addStretch(1)
        cancel = icon_button("取消", None)
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        confirm = icon_button("确定", None, variant="primary", color="#FFFFFF")
        confirm.clicked.connect(self._save)
        actions.addWidget(confirm)
        body.addLayout(actions)

    def _set_all(self, state) -> None:
        for check in self.device_checks.values():
            check.setCheckState(state)

    def _update_selected_count(self) -> None:
        count = sum(check.isChecked() for check in self.device_checks.values())
        self.selected_count.setText(f"已选择 {count} 台设备")

    def _save(self) -> None:
        chosen = {serial for serial, check in self.device_checks.items()
                  if check.isChecked()}
        if not chosen:
            AppMessageBox.warning(self, "安装设备", "请至少选择一台设备。")
            return
        self.result_serials = chosen
        self.accept()


class AppPermissionsDialog(QDialog):
    def __init__(self, window: "MainWindow", serial: str, package: str,
                 permissions: list[AppPermission]) -> None:
        super().__init__(window)
        self.window = window
        self.serial = serial
        self.package = package
        self.setWindowTitle("应用权限")
        self.setObjectName("appToolDialog")
        self.setMinimumSize(900, 500)
        self._busy = False
        self._task_events: queue.Queue = queue.Queue()
        self._task_timer = QTimer(self)
        self._task_timer.timeout.connect(self._drain_task_events)

        body = QVBoxLayout(self)
        body.setContentsMargins(theme.SPACE_6, theme.SPACE_6,
                                theme.SPACE_6, theme.SPACE_6)
        body.setSpacing(theme.SPACE_4)
        body.addWidget(label("应用权限", "pageTitle"))
        subtitle = label(f"{package} · {serial}", "pageSubtitle")
        subtitle.setWordWrap(True)
        body.addWidget(subtitle)

        card = Card(padding=theme.SPACE_4)
        search = QHBoxLayout()
        search.addWidget(label("已声明的权限", "cardTitle"))
        search.addStretch(1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索名称或标识")
        self.search.setMaximumWidth(240)
        self.search.textChanged.connect(self._filter)
        search.addWidget(self.search)
        card.body.addLayout(search)
        self.tree = table([
            ("中文名称", 210), ("权限标识", 390),
            ("状态", 92), ("类型", 90),
        ], stretch_column=1)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.itemSelectionChanged.connect(self._selection_changed)
        card.body.addWidget(self.tree, 1)
        body.addWidget(card, 1)

        note = label("仅运行时权限可授予或撤销；设备策略可能限制部分权限。", "hint")
        body.addWidget(note)
        self.permission_status = label("", "hint")
        self.permission_status.setWordWrap(True)
        body.addWidget(self.permission_status)
        body.addWidget(divider())
        actions = QHBoxLayout()
        actions.setSpacing(theme.SPACE_2)
        self.refresh_button = icon_button("刷新", theme.ICON_REFRESH)
        self.refresh_button.clicked.connect(self._refresh)
        actions.addWidget(self.refresh_button)
        actions.addStretch(1)
        self.grant_button = icon_button("授予", "fa6s.check",
                                        variant="primary", color="#FFFFFF")
        self.grant_button.clicked.connect(lambda: self._apply(True))
        actions.addWidget(self.grant_button)
        self.revoke_button = icon_button("撤销", "fa6s.ban",
                                         variant="danger", color=theme.DANGER)
        self.revoke_button.clicked.connect(lambda: self._apply(False))
        actions.addWidget(self.revoke_button)
        close = icon_button("关闭", None)
        close.clicked.connect(self.accept)
        actions.addWidget(close)
        body.addLayout(actions)
        self._populate(permissions)

    def _populate(self, permissions: list[AppPermission]) -> None:
        selected = self.tree.selectedItems()
        selected_name = selected[0].data(0, Qt.UserRole).name if selected else None
        self.tree.clear()
        selected_item = None
        for permission in permissions:
            if permission.granted is True:
                status = "已授予"
            elif permission.granted is False:
                status = "未授予"
            else:
                status = "状态未知"
            item = QTreeWidgetItem([
                permission_label(permission.name) or "—", permission.name, status,
                "运行时" if permission.runtime else "非运行时",
            ])
            item.setData(0, Qt.UserRole, permission)
            self.tree.addTopLevelItem(item)
            if permission.name == selected_name:
                selected_item = item
        self._filter(self.search.text())
        if selected_item is not None and not selected_item.isHidden():
            self.tree.setCurrentItem(selected_item)
        self.permission_status.setText(f"已从设备读取 {len(permissions)} 项权限。")
        self._selection_changed()

    def _filter(self, text: str) -> None:
        needle = text.strip().lower()
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            hidden = (needle not in item.text(0).lower()
                      and needle not in item.text(1).lower())
            if hidden and item.isSelected():
                item.setSelected(False)
            item.setHidden(hidden)

    def _selection_changed(self) -> None:
        items = self.tree.selectedItems()
        permission = items[0].data(0, Qt.UserRole) if items else None
        can_change = (not self._busy and permission is not None
                      and permission.runtime)
        self.grant_button.setEnabled(can_change and permission.granted is False)
        self.revoke_button.setEnabled(can_change and permission.granted is True)

    def _run_task(self, title: str, work: Callable[[], object],
                  done: Callable[[object], None]) -> None:
        if self._busy:
            self.permission_status.setText("当前权限操作尚未完成，请稍候。")
            return
        self._busy = True
        self.refresh_button.setEnabled(False)
        self._selection_changed()
        self.window.log(f"▶ {title} · {self.package} · {self.serial}")
        self._task_done = (title, done)
        self._task_timer.start(100)

        def runner() -> None:
            try:
                self._task_events.put((True, work()))
            except (AdbError, OSError) as exc:
                self._task_events.put((False, str(exc)))

        threading.Thread(target=runner, daemon=True).start()

    def _drain_task_events(self) -> None:
        try:
            ok, result = self._task_events.get_nowait()
        except queue.Empty:
            return
        self._task_timer.stop()
        self._busy = False
        self.refresh_button.setEnabled(True)
        self._selection_changed()
        title, done = self._task_done
        if ok:
            self.window.log(f"✓ {title}完成")
            if self.isVisible():
                done(result)
        else:
            self.window.log(f"✗ {title}失败：{result}")
            if self.isVisible():
                self.permission_status.setText("操作失败，请查看错误提示。")
                AppMessageBox.warning(self, title, str(result))

    def _apply(self, grant: bool) -> None:
        items = self.tree.selectedItems()
        if not items:
            return
        item = items[0]
        permission = item.data(0, Qt.UserRole)
        if not permission.runtime:
            return

        title = "授予权限" if grant else "撤销权限"
        self.permission_status.setText(f"正在{title}并核对这项权限…")

        def work() -> bool | None:
            self.window.client.set_app_permission(
                self.serial, self.package, permission.name, grant=grant)
            try:
                return self.window.client.check_app_permission(
                    self.serial, self.package, permission.name)
            except AdbError:
                permissions = self.window.client.app_permissions(
                    self.serial, self.package)
                current = next((entry for entry in permissions
                                if entry.name == permission.name), None)
                return current.granted if current else None

        def done(granted: bool | None) -> None:
            updated = AppPermission(permission.name, granted, permission.runtime)
            item.setData(0, Qt.UserRole, updated)
            item.setText(2, "状态未知" if granted is None else
                         "已授予" if granted else "未授予")
            self._selection_changed()
            if granted is grant:
                self.permission_status.setText(f"设备已确认：{permission.name}"
                                               f"{'已授予' if grant else '已撤销'}。")
            else:
                state = ("状态未知" if granted is None else
                         "已授予" if granted else "未授予")
                self.permission_status.setText(
                    f"命令已发送，但设备仍报告 {permission.name}：{state}；"
                    "请检查设备策略或系统设置。")

        self._run_task(title, work, done)

    def _refresh(self) -> None:
        names = [self.tree.topLevelItem(index).data(0, Qt.UserRole).name
                 for index in range(self.tree.topLevelItemCount())]
        self.permission_status.setText("正在核对设备上的权限状态…")

        def work() -> dict[str, bool] | list[AppPermission]:
            try:
                return self.window.client.app_permission_states(
                    self.serial, self.package, names)
            except AdbError:
                return self.window.client.app_permissions(self.serial, self.package)

        def done(result: dict[str, bool] | list[AppPermission]) -> None:
            if isinstance(result, list):
                self._populate(result)
                return
            for index in range(self.tree.topLevelItemCount()):
                item = self.tree.topLevelItem(index)
                previous = item.data(0, Qt.UserRole)
                granted = result[previous.name]
                item.setData(0, Qt.UserRole, AppPermission(
                    previous.name, granted, previous.runtime))
                item.setText(2, "已授予" if granted else "未授予")
            self._selection_changed()
            self.permission_status.setText(f"已核对 {len(result)} 项权限状态。")

        self._run_task("刷新应用权限", work, done)


class AppProcessDialog(QDialog):
    def __init__(self, window: "MainWindow", serial: str, package: str,
                 report: AppProcessReport) -> None:
        super().__init__(window)
        self.window = window
        self.serial = serial
        self.package = package
        self.setWindowTitle("进程与内存")
        self.setObjectName("appToolDialog")
        self.setMinimumSize(760, 700)

        body = QVBoxLayout(self)
        body.setContentsMargins(theme.SPACE_6, theme.SPACE_6,
                                theme.SPACE_6, theme.SPACE_6)
        body.setSpacing(theme.SPACE_4)
        body.addWidget(label("进程与内存", "pageTitle"))
        subtitle = label(f"{package} · {serial}", "pageSubtitle")
        subtitle.setWordWrap(True)
        body.addWidget(subtitle)

        self.summary = QFrame()
        self.summary.setObjectName("summary")
        summary_layout = QHBoxLayout(self.summary)
        summary_layout.setContentsMargins(theme.SPACE_4, theme.SPACE_3,
                                          theme.SPACE_4, theme.SPACE_3)
        self.summary_text = label("", "summaryTitle")
        summary_layout.addWidget(self.summary_text)
        body.addWidget(self.summary)

        card = Card(padding=theme.SPACE_4)
        card.body.addWidget(label("运行中的进程", "cardTitle"))
        self.process_tree = table([("PID", 90), ("进程名", 430), ("RSS", 110)],
                                  stretch_column=1)
        self.process_tree.setFixedHeight(140)
        card.body.addWidget(self.process_tree)
        card.body.addWidget(label("内存详情（dumpsys meminfo）", "cardTitle"))
        self.meminfo_text = QPlainTextEdit()
        self.meminfo_text.setObjectName("reportText")
        self.meminfo_text.setReadOnly(True)
        self.meminfo_text.setMinimumHeight(180)
        card.body.addWidget(self.meminfo_text, 1)
        body.addWidget(card, 1)

        actions = QHBoxLayout()
        refresh = icon_button("刷新", theme.ICON_REFRESH)
        refresh.clicked.connect(self._refresh)
        actions.addWidget(refresh)
        actions.addStretch(1)
        close = icon_button("关闭", None)
        close.clicked.connect(self.accept)
        actions.addWidget(close)
        body.addLayout(actions)
        self._render(report)

    def _render(self, report: AppProcessReport) -> None:
        def size(kb: int | None) -> str:
            return f"{kb / 1024:.1f} MB" if kb is not None else "—"

        self.summary_text.setText(
            f"{len(report.processes)} 个进程 · 总 PSS {size(report.total_pss_kb)}"
            f" · 总 RSS {size(report.total_rss_kb)}")
        self.process_tree.clear()
        for process in report.processes:
            self.process_tree.addTopLevelItem(QTreeWidgetItem([
                str(process.pid), process.name, size(process.rss_kb)]))
        if not report.processes:
            self.process_tree.addTopLevelItem(QTreeWidgetItem(
                ["—", "应用未运行", "—"]))
        self.meminfo_text.setPlainText(report.meminfo or "设备未返回内存详情。")

    def _refresh(self) -> None:
        self.window.submit(
            "刷新进程与内存",
            lambda: self.window.client.app_process_report(self.serial, self.package),
            self._render, log_result=False)


# --- Device logs ---------------------------------------------------------

class LogcatHighlighter(QSyntaxHighlighter):
    def __init__(self, document) -> None:
        super().__init__(document)
        self.error_format = QTextCharFormat()
        self.error_format.setForeground(QColor(theme.DANGER))
        self.warning_format = QTextCharFormat()
        self.warning_format.setForeground(QColor(theme.WARNING_TEXT))

    def highlightBlock(self, text: str) -> None:
        level = parse_logcat_line(text).level
        if level in ("E", "F", "A"):
            self.setFormat(0, len(text), self.error_format)
        elif level == "W":
            self.setFormat(0, len(text), self.warning_format)


class LogcatPage(QWidget):
    MAX_LINES = 10000

    def __init__(self, window: "MainWindow") -> None:
        super().__init__()
        self.window = window
        self.process: QProcess | None = None
        self.stream_serial: str | None = None
        self.device_serial: str | None = None
        self.source_manual = False
        self.source_results: queue.Queue = queue.Queue()
        self.package_results: queue.Queue = queue.Queue()
        self.package_cache: dict[str, list[str]] = {}
        self.package_loading: set[str] = set()
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.partial_line = ""
        self.entries = deque(maxlen=self.MAX_LINES)
        self.pending = deque(maxlen=self.MAX_LINES)
        self.visible_count = 0
        self.package_pids: set[int] = set()
        self.pid_results: queue.Queue = queue.Queue()
        self.pid_request: tuple[str, str] | None = None
        self.pid_generation = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SPACE_4)
        card = Card(padding=theme.SPACE_4)
        layout.addWidget(card)

        row = QHBoxLayout()
        row.setSpacing(theme.SPACE_2)
        row.addWidget(label("来源"))
        self.source_box = QComboBox()
        self.source_box.addItem("Logcat", "logcat")
        self.source_box.addItem("HiLog（华为/鸿蒙）", "hilog")
        row.addWidget(self.source_box)
        row.addWidget(label("缓冲区"))
        self.buffer_box = QComboBox()
        self.buffer_box.addItem("全部", "all")
        for name in LOGCAT_BUFFERS:
            self.buffer_box.addItem(name, name)
        self.buffer_box.currentIndexChanged.connect(self._buffer_changed)
        row.addWidget(self.buffer_box)
        row.addWidget(label("最低级别"))
        self.level_box = QComboBox()
        for level in ("V", "D", "I", "W", "E"):
            self.level_box.addItem(level, level)
        row.addWidget(self.level_box)
        row.addStretch()
        self.start_button = icon_button("启动", "fa6s.play", variant="primary")
        self.start_button.clicked.connect(self.start)
        row.addWidget(self.start_button)
        self.stop_button = icon_button("停止", "fa6s.stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        row.addWidget(self.stop_button)
        card.body.addLayout(row)

        filters = QHBoxLayout()
        filters.setSpacing(theme.SPACE_2)
        filters.addWidget(label("包名"))
        self.package_edit = QLineEdit()
        self.package_edit.setPlaceholderText("可选：com.example.app")
        self.package_edit.textChanged.connect(self._package_changed)
        filters.addWidget(self.package_edit, 1)
        self.package_box = QComboBox()
        self.package_box.setFixedWidth(theme.LOGCAT_APP_SELECT_WIDTH)
        self.package_box.addItem("选择设备应用…")
        self.package_box.setEnabled(False)
        self.package_box.currentIndexChanged.connect(self._package_selected)
        filters.addWidget(self.package_box)
        refresh_packages = icon_button("刷新应用", theme.ICON_REFRESH)
        refresh_packages.clicked.connect(lambda: self.refresh_packages(force=True))
        filters.addWidget(refresh_packages)
        filters.addWidget(label("关键词"))
        self.keyword_edit = QLineEdit()
        self.keyword_edit.setPlaceholderText("搜索日志内容")
        self.keyword_edit.textChanged.connect(self._render)
        filters.addWidget(self.keyword_edit, 1)
        card.body.addLayout(filters)

        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("logcatView")
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.log_view.setPlaceholderText(
            "暂无日志。启动后会显示符合筛选条件的日志。")
        self.log_view.setMaximumBlockCount(self.MAX_LINES)
        self.log_view.setMinimumHeight(theme.LOGCAT_VIEW_MIN_HEIGHT)
        self.highlighter = LogcatHighlighter(self.log_view.document())
        card.body.addWidget(self.log_view)
        self.level_box.currentIndexChanged.connect(self._render)

        actions = QHBoxLayout()
        actions.setSpacing(theme.SPACE_2)
        self.stream_status = label("未启动 · 最多保留 10000 行", "muted")
        actions.addWidget(self.stream_status, 1)
        clear_display = icon_button("清空显示", "fa6s.eraser")
        clear_display.clicked.connect(self.clear_display)
        actions.addWidget(clear_display)
        self.clear_device_button = icon_button("清空设备缓冲区", "fa6s.trash-can")
        self.clear_device_button.clicked.connect(self.clear_device_buffer)
        actions.addWidget(self.clear_device_button)
        export = icon_button("导出 TXT", "fa6s.file-arrow-down")
        export.clicked.connect(self.export_txt)
        actions.addWidget(export)
        card.body.addLayout(actions)

        crash = Card(padding=theme.SPACE_4)
        crash.body.addWidget(label("崩溃日志抓取", "cardTitle"))
        crash.body.addWidget(label(
            "抓取设备上的 ANR 与 tombstone 文件。受设备权限限制时会尝试生成 bugreport。", "muted"))
        crash_actions = QHBoxLayout()
        self.crash_status = label("选择保存位置后开始抓取", "muted")
        self.crash_status.setWordWrap(True)
        crash_actions.addWidget(self.crash_status, 1)
        self.crash_button = icon_button("抓取 ANR / tombstone", "fa6s.download",
                                        variant="primary")
        self.crash_button.clicked.connect(self.pull_crashes)
        crash_actions.addWidget(self.crash_button)
        crash.body.addLayout(crash_actions)
        layout.addWidget(crash)
        layout.addStretch()

        self.flush_timer = QTimer(self)
        self.flush_timer.timeout.connect(self._flush)
        self.flush_timer.start(100)
        self.pid_timer = QTimer(self)
        self.pid_timer.timeout.connect(self._request_pids)
        self.pid_timer.start(4000)
        self.source_box.currentIndexChanged.connect(self._source_changed)
        self.source_box.activated.connect(self._source_user_selected)

    def device_changed(self, serial: str | None) -> None:
        if serial == self.device_serial:
            return
        self.stop()
        self.device_serial = serial
        self.source_manual = False
        self.source_box.setCurrentIndex(0)
        self.package_pids.clear()
        self.pid_generation += 1
        self.pid_request = None
        self._populate_packages(
            self.package_cache.get(serial, []) if serial else [],
            "选择设备应用…" if serial else "未选择设备")
        client = self.window.client
        if serial is None or client is None:
            return

        def probe() -> None:
            try:
                manufacturer = client.run(
                    serial, "shell", "getprop", "ro.product.manufacturer",
                    timeout=5).strip().upper()
            except AdbError:
                manufacturer = ""
            self.source_results.put((serial, manufacturer))

        threading.Thread(target=probe, daemon=True).start()
        if hasattr(self.window, "pages") and self.window.pages.currentIndex() == 3:
            self.refresh_packages()

    def refresh_packages(self, *, force: bool = False) -> None:
        serial = self.device_serial
        client = self.window.client
        if serial is None or client is None or serial in self.package_loading:
            return
        if not force and serial in self.package_cache:
            self._populate_packages(self.package_cache[serial])
            return
        self.package_loading.add(serial)
        self._populate_packages([], "正在加载设备应用…")

        def worker() -> None:
            try:
                packages = client.list_user_packages(serial)
                error = ""
            except AdbError as exc:
                packages, error = [], str(exc)
            self.package_results.put((serial, packages, error))

        threading.Thread(target=worker, daemon=True).start()

    def _populate_packages(self, packages: list[str],
                           placeholder: str = "选择设备应用…") -> None:
        current = self.package_edit.text().strip()
        self.package_box.blockSignals(True)
        self.package_box.clear()
        self.package_box.addItem(placeholder)
        for package in packages:
            self.package_box.addItem(package, package)
        index = self.package_box.findData(current)
        self.package_box.setCurrentIndex(max(0, index))
        self.package_box.setEnabled(bool(packages))
        self.package_box.blockSignals(False)

    def _package_selected(self, _index: int) -> None:
        package = self.package_box.currentData()
        if package:
            self.package_edit.setText(package)

    def _sync_package_selection(self) -> None:
        package = self.package_edit.text().strip()
        index = self.package_box.findData(package)
        if index != self.package_box.currentIndex():
            self.package_box.blockSignals(True)
            self.package_box.setCurrentIndex(max(0, index))
            self.package_box.blockSignals(False)

    def _source_user_selected(self, _index: int) -> None:
        self.source_manual = True

    def _buffer_changed(self) -> None:
        if self.process is not None and self.source_box.currentData() == "logcat":
            self.stop()
            self.start()

    def _source_changed(self) -> None:
        is_logcat = self.source_box.currentData() == "logcat"
        self.buffer_box.setEnabled(is_logcat)
        self.clear_device_button.setText(
            "清空设备缓冲区" if is_logcat else "清空设备 HiLog")
        if self.process is not None:
            self.stop()
            self.start()

    def _package_changed(self) -> None:
        self._sync_package_selection()
        self.package_pids.clear()
        self.pid_generation += 1
        self.pid_request = None
        self._render()
        self._request_pids()

    def _buffer_args(self) -> list[str]:
        selected = self.buffer_box.currentData()
        names = LOGCAT_BUFFERS if selected == "all" else (selected,)
        return [arg for name in names for arg in ("-b", name)]

    def _request_pids(self) -> None:
        package = self.package_edit.text().strip()
        serial = self.stream_serial
        client = self.window.client
        if not package or not serial or client is None or self.pid_request:
            return
        request = (serial, package)
        self.pid_request = request
        generation = self.pid_generation

        def worker() -> None:
            try:
                from adb_client import parse_app_processes
                try:
                    output = client.run(serial, "shell", "ps", "-A", "-o", "PID,NAME",
                                        timeout=10)
                except AdbError:
                    output = client.run(serial, "shell", "ps", "-A", timeout=10)
                pids = {item.pid for item in parse_app_processes(output, package)}
            except AdbError:
                pids = set()
            self.pid_results.put((generation, request, pids))

        threading.Thread(target=worker, daemon=True).start()

    def start(self) -> None:
        if self.process is not None:
            return
        serial = self.window.selected_serial()
        if serial is None or self.window.client is None:
            return
        self.clear_display()
        self.stream_serial = serial
        self.package_pids.clear()
        self.pid_generation += 1
        self.pid_request = None
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.partial_line = ""
        process = QProcess(self)
        process.setProgram(self.window.client.executable)
        if self.source_box.currentData() == "hilog":
            arguments = ["-s", serial, "shell", "hilog"]
        else:
            arguments = ["-s", serial, "logcat", *self._buffer_args(),
                         "-v", "threadtime"]
        process.setArguments(arguments)
        process.readyReadStandardOutput.connect(lambda: self._read_stdout(process))
        process.readyReadStandardError.connect(lambda: self._read_stderr(process))
        process.finished.connect(lambda _code, _status: self._finished(process))
        process.errorOccurred.connect(lambda _error: self._process_error(process))
        self.process = process
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self._update_stream_status()
        process.start()
        self._request_pids()

    def stop(self) -> None:
        process = self.process
        if process is None:
            return
        self.process = None
        self.stream_serial = None
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.stream_status.setText(
            f"已停止 · 收到 {len(self.entries)} 行 · 匹配 {self.visible_count} 行")
        if process.state() != QProcess.NotRunning:
            process.kill()

    def _read_stdout(self, process: QProcess) -> None:
        if process is not self.process:
            return
        chunk = self.decoder.decode(bytes(process.readAllStandardOutput()))
        self.partial_line += chunk
        *lines, self.partial_line = self.partial_line.split("\n")
        for line in lines:
            self.pending.append(parse_logcat_line(line.rstrip("\r")))

    def _read_stderr(self, process: QProcess) -> None:
        if process is self.process:
            message = bytes(process.readAllStandardError()).decode("utf-8", "replace").strip()
            if message:
                self.stream_status.setText(message.splitlines()[-1])
                self.window.log(f"logcat：{message}")

    def _process_error(self, process: QProcess) -> None:
        if process is self.process and process.error() == QProcess.FailedToStart:
            error = process.errorString()
            self.stop()
            self.stream_status.setText(f"无法启动 logcat：{error}")

    def _finished(self, process: QProcess) -> None:
        if process is self.process:
            self._read_stdout(process)
            if self.partial_line:
                self.pending.append(parse_logcat_line(self.partial_line))
                self.partial_line = ""
            self._read_stderr(process)
            code = process.exitCode()
            self.stop()
            self.stream_status.setText(
                f"logcat 已退出（代码 {code}）· 已保留 {len(self.entries)} 行")
        process.deleteLater()

    def _matches(self, entry) -> bool:
        return logcat_matches(
            entry, package=self.package_edit.text().strip(),
            package_pids=self.package_pids,
            minimum_level=self.level_box.currentData(),
            keyword=self.keyword_edit.text().strip())

    def _update_stream_status(self) -> None:
        if self.process is None:
            return
        self.stream_status.setText(
            f"正在读取 {self.stream_serial} · "
            f"{'HiLog' if self.source_box.currentData() == 'hilog' else self.buffer_box.currentText()}"
            f" · 收到 {len(self.entries)} 行 · 匹配 {self.visible_count} 行")
        if self.visible_count:
            self.log_view.setPlaceholderText("")
        elif self.package_edit.text().strip():
            if self.source_box.currentData() == "logcat":
                self.log_view.setPlaceholderText(
                    "暂无符合包名的 Logcat。华为/鸿蒙设备可把「来源」切到 HiLog；"
                    "也可清空包名查看原始日志。")
            else:
                self.log_view.setPlaceholderText(
                    "暂无符合包名的 HiLog。可清空包名查看原始日志，"
                    "或确认应用是否正在运行并输出日志。")
        else:
            self.log_view.setPlaceholderText("正在等待设备日志…")

    def _flush(self) -> None:
        while True:
            try:
                serial, packages, error = self.package_results.get_nowait()
            except queue.Empty:
                break
            self.package_loading.discard(serial)
            if not error:
                self.package_cache[serial] = packages
            if serial == self.device_serial:
                self._populate_packages(
                    packages, "加载失败，点刷新重试" if error
                    else "无用户安装应用" if not packages else "选择设备应用…")
                if error:
                    self.window.log(f"读取设备应用失败：{error}")
        while True:
            try:
                serial, manufacturer = self.source_results.get_nowait()
            except queue.Empty:
                break
            if serial == self.device_serial and not self.source_manual:
                self.source_box.setCurrentIndex(
                    1 if manufacturer == "HUAWEI" else 0)
        changed = False
        while True:
            try:
                generation, request, pids = self.pid_results.get_nowait()
            except queue.Empty:
                break
            if generation == self.pid_generation and request == self.pid_request:
                self.pid_request = None
                new_pids = pids - self.package_pids
                if new_pids:
                    self.package_pids.update(new_pids)
                    changed = True
        if changed:
            self._render()
        if not self.pending:
            return
        lines = []
        while self.pending:
            entry = self.pending.popleft()
            self.entries.append(entry)
            if self._matches(entry):
                lines.append(entry.raw)
        if lines:
            bar = self.log_view.verticalScrollBar()
            at_bottom = bar.value() >= bar.maximum() - 2
            self.log_view.appendPlainText("\n".join(lines))
            self.visible_count = min(self.MAX_LINES, self.visible_count + len(lines))
            if at_bottom:
                bar.setValue(bar.maximum())
        self._update_stream_status()

    def _render(self) -> None:
        self._flush_pending_only()
        lines = [entry.raw for entry in self.entries if self._matches(entry)]
        self.log_view.setPlainText("\n".join(lines))
        self.visible_count = len(lines)
        self.log_view.moveCursor(QTextCursor.End)
        self._update_stream_status()

    def _flush_pending_only(self) -> None:
        while self.pending:
            self.entries.append(self.pending.popleft())

    def clear_display(self) -> None:
        self.entries.clear()
        self.pending.clear()
        self.visible_count = 0
        self.log_view.clear()
        self._update_stream_status()

    def clear_device_buffer(self) -> None:
        serial = self.window.selected_serial()
        if serial is None or self.window.client is None:
            return
        source = self.source_box.currentData()
        buffer = self.buffer_box.currentData()
        label_text = (self.buffer_box.currentText() if source == "logcat"
                      else "HiLog 全部")
        if AppMessageBox.question(
            self.window, "清空设备日志",
            f"确定清空 {serial} 的 {label_text} 日志缓冲区吗？"
        ) != AppMessageBox.Yes:
            return
        clear = (lambda: self.window.client.clear_logcat(serial, buffer)
                 if source == "logcat" else self.window.client.clear_hilog(serial))
        self.window.submit(
            "清空设备日志", clear,
            lambda _result: self.clear_display(), log_result=False)

    def export_txt(self) -> None:
        self._flush_pending_only()
        lines = [entry.raw for entry in self.entries if self._matches(entry)]
        if not lines:
            AppMessageBox.information(self.window, "导出日志", "当前没有可导出的日志。")
            return
        name = f"logcat_{datetime.now():%Y%m%d_%H%M%S}.txt"
        path, _ = QFileDialog.getSaveFileName(
            self.window, "导出 Logcat", str(Path.home() / "Downloads" / name),
            "文本文件 (*.txt)")
        if path:
            try:
                Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
            except OSError as exc:
                AppMessageBox.warning(self.window, "导出日志", str(exc))
                return
            self.window.set_status(f"已导出 {len(lines)} 行日志")
            self.window.log(f"已导出 Logcat：{path}")

    def pull_crashes(self) -> None:
        serial = self.window.selected_serial()
        if serial is None or self.window.client is None:
            return
        destination = QFileDialog.getExistingDirectory(
            self.window, "选择崩溃日志保存位置", str(Path.home() / "Downloads"))
        if not destination:
            return
        self.crash_button.setEnabled(False)
        self.crash_status.setText("正在抓取；若需要生成 bugreport，可能耗时数分钟…")

        def done(result) -> None:
            summary = (f"ANR {result.anr_count} 个，tombstone "
                       f"{result.tombstone_count} 个 · {result.folder}")
            if result.bugreport:
                summary += f" · 原始 bugreport：{result.bugreport.name}"
            if result.bugreport_text:
                summary += f" · 文本报告：{result.bugreport_text.name}"
            self.crash_status.setText(summary)
            self.window.log(summary)
            if not result.anr_count and not result.tombstone_count:
                AppMessageBox.information(
                    self.window, "未找到崩溃文件",
                    "设备未提供可读取的 ANR 或 tombstone 文件。"
                    "已尝试直接读取和生成 bugreport。")

        def finished() -> None:
            self.crash_button.setEnabled(True)
            self.crash_status.setText("抓取结束；失败详情请查看活动记录")

        if not self.window.submit(
            "抓取崩溃日志",
            lambda: self.window.client.pull_crash_reports(serial, Path(destination)),
            done, finished=finished,
            log_result=False,
        ):
            self.crash_button.setEnabled(True)
            self.crash_status.setText("已有任务进行中，请稍候重试")


# --- Performance monitoring ---------------------------------------------

class PerformancePage(QWidget):
    SAMPLE_MS = 2500
    NETWORK_MS = 12000
    MAX_SAMPLES = 60

    def __init__(self, window: "MainWindow") -> None:
        super().__init__()
        self.window = window
        self.device_serial: str | None = None
        self.monitor_serial: str | None = None
        self.monitor_package = ""
        self.generation = 0
        self.sample_in_flight = False
        self.network_in_flight = False
        self.uid: int | None = None
        self.previous: PerformanceSample | None = None
        self.package_cache: dict[str, list[str]] = {}
        self.package_loading: set[str] = set()
        self.package_results: queue.Queue = queue.Queue()
        self.sample_results: queue.Queue = queue.Queue()
        self.network_results: queue.Queue = queue.Queue()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SPACE_4)

        controls = Card(padding=theme.SPACE_4)
        row = QHBoxLayout()
        row.setSpacing(theme.SPACE_2)
        row.addWidget(label("应用包名"))
        self.package_edit = QLineEdit()
        self.package_edit.setPlaceholderText("选择当前设备上的应用，或输入包名")
        self.package_edit.textChanged.connect(self._package_changed)
        row.addWidget(self.package_edit, 1)
        self.package_box = QComboBox()
        self.package_box.setFixedWidth(theme.LOGCAT_APP_SELECT_WIDTH)
        self.package_box.addItem("选择设备应用…")
        self.package_box.setEnabled(False)
        self.package_box.currentIndexChanged.connect(self._package_selected)
        row.addWidget(self.package_box)
        refresh = icon_button("刷新应用", theme.ICON_REFRESH)
        refresh.clicked.connect(lambda: self.refresh_packages(force=True))
        row.addWidget(refresh)
        controls.body.addLayout(row)

        actions = QHBoxLayout()
        self.status = label("选择应用后开始监控", "muted")
        self.status.setWordWrap(True)
        actions.addWidget(self.status, 1)
        self.start_button = icon_button("开始监控", "fa6s.play", variant="primary")
        self.start_button.clicked.connect(self.start)
        actions.addWidget(self.start_button)
        self.stop_button = icon_button("停止", "fa6s.stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        actions.addWidget(self.stop_button)
        controls.body.addLayout(actions)
        layout.addWidget(controls)

        metrics = QGridLayout()
        metrics.setSpacing(theme.SPACE_4)
        cpu_card, self.cpu_value, self.cpu_detail = self._metric_card(
            "CPU", "—", "应用进程 · 单核 100%")
        memory_card, self.memory_value, self.memory_detail = self._metric_card(
            "内存", "—", "应用 PSS / RSS")
        network_card, self.network_value, self.network_detail = self._metric_card(
            "网络 RX / TX", "—", "设备接口实时速率")
        self.network_value.setWordWrap(True)
        self.app_network_detail = label("应用累计：等待 UID 统计", "muted")
        self.app_network_detail.setWordWrap(True)
        network_card.body.addWidget(self.app_network_detail)
        fps_card, self.fps_value, self.fps_detail = self._metric_card(
            "帧率", "—", "优先读取画面图层")
        self.layer_detail = label("图层：—", "muted")
        self.layer_detail.setWordWrap(True)
        fps_card.body.addWidget(self.layer_detail)
        metrics.addWidget(cpu_card, 0, 0)
        metrics.addWidget(memory_card, 0, 1)
        metrics.addWidget(network_card, 1, 0)
        metrics.addWidget(fps_card, 1, 1)
        metrics.setColumnStretch(0, 1)
        metrics.setColumnStretch(1, 1)
        layout.addLayout(metrics)

        history = Card(padding=theme.SPACE_4)
        history.body.addWidget(label("采样记录", "cardTitle"))
        history.body.addWidget(label(
            "约每 2.5 秒采样一次；CPU 为应用进程之和，网络速率为设备接口合计。",
            "cardDescription"))
        self.history_tree = table([
            ("时间", 85), ("CPU", 75), ("PSS", 85),
            ("设备 RX/s", 105), ("设备 TX/s", 105),
            ("FPS", 75), ("帧率来源", 130),
        ])
        self.history_tree.setMinimumHeight(200)
        history.body.addWidget(self.history_tree)
        layout.addWidget(history)
        layout.addStretch(1)

        self.sample_timer = QTimer(self)
        self.sample_timer.setInterval(self.SAMPLE_MS)
        self.sample_timer.timeout.connect(self._request_sample)
        self.network_timer = QTimer(self)
        self.network_timer.setInterval(self.NETWORK_MS)
        self.network_timer.timeout.connect(self._request_network)
        self.result_timer = QTimer(self)
        self.result_timer.timeout.connect(self._flush_results)
        self.result_timer.start(100)

    @staticmethod
    def _metric_card(title: str, value: str,
                     detail: str) -> tuple[Card, QLabel, QLabel]:
        card = Card(padding=theme.SPACE_4)
        card.body.addWidget(label(title, "cardTitle"))
        value_label = label(value, "metricValue")
        card.body.addWidget(value_label)
        detail_label = label(detail, "muted")
        detail_label.setWordWrap(True)
        card.body.addWidget(detail_label)
        return card, value_label, detail_label

    @staticmethod
    def _bytes(value: float | int | None) -> str:
        if value is None:
            return "—"
        amount = float(value)
        for unit in ("B", "KB", "MB", "GB"):
            if abs(amount) < 1024 or unit == "GB":
                return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
            amount /= 1024
        return "—"

    def device_changed(self, serial: str | None) -> None:
        if serial == self.device_serial:
            return
        self.stop()
        self.device_serial = serial
        self.package_edit.clear()
        self._populate_packages(
            self.package_cache.get(serial, []) if serial else [],
            "选择设备应用…" if serial else "未选择设备")
        if serial and self.window.pages.currentIndex() == 4:
            self.refresh_packages()

    def refresh_packages(self, *, force: bool = False) -> None:
        serial = self.device_serial
        client = self.window.client
        if serial is None or client is None or serial in self.package_loading:
            return
        if not force and serial in self.package_cache:
            self._populate_packages(self.package_cache[serial])
            return
        self.package_loading.add(serial)
        self._populate_packages([], "正在加载设备应用…")

        def worker() -> None:
            try:
                packages = client.list_user_packages(serial)
                error = ""
            except AdbError as exc:
                packages, error = [], str(exc)
            self.package_results.put((serial, packages, error))

        threading.Thread(target=worker, daemon=True).start()

    def _populate_packages(self, packages: list[str],
                           placeholder: str = "选择设备应用…") -> None:
        current = self.package_edit.text().strip()
        self.package_box.blockSignals(True)
        self.package_box.clear()
        self.package_box.addItem(placeholder)
        for package in packages:
            self.package_box.addItem(package, package)
        self.package_box.setCurrentIndex(max(0, self.package_box.findData(current)))
        self.package_box.setEnabled(bool(packages))
        self.package_box.blockSignals(False)

    def _package_selected(self, _index: int) -> None:
        package = self.package_box.currentData()
        if package:
            self.package_edit.setText(package)

    def _package_changed(self) -> None:
        self.stop()
        package = self.package_edit.text().strip()
        index = self.package_box.findData(package)
        if index != self.package_box.currentIndex():
            self.package_box.blockSignals(True)
            self.package_box.setCurrentIndex(max(0, index))
            self.package_box.blockSignals(False)

    def start(self) -> None:
        serial = self.window.selected_serial()
        if serial is None or self.window.client is None:
            return
        try:
            package = validate_package_name(self.package_edit.text())
        except AdbError as exc:
            AppMessageBox.warning(self, "应用包名", str(exc))
            return
        self.stop()
        self.monitor_serial = serial
        self.monitor_package = package
        self.uid = None
        self.previous = None
        self.history_tree.clear()
        self.cpu_value.setText("采样中")
        self.memory_value.setText("采样中")
        self.network_value.setText("采样中")
        self.fps_value.setText("采样中")
        self.app_network_detail.setText("应用累计：等待 UID 统计")
        self.status.setText(f"正在监控 {package} · {serial}")
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.sample_timer.start()
        self.network_timer.start()
        self._request_sample()
        self._request_network()

    def stop(self) -> None:
        was_running = self.monitor_serial is not None
        self.generation += 1
        self.monitor_serial = None
        self.monitor_package = ""
        self.sample_timer.stop()
        self.network_timer.stop()
        self.sample_in_flight = False
        self.network_in_flight = False
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        if was_running:
            self.status.setText("已停止监控")

    def _request_sample(self) -> None:
        serial, package = self.monitor_serial, self.monitor_package
        client = self.window.client
        if not serial or not package or client is None or self.sample_in_flight:
            return
        self.sample_in_flight = True
        generation = self.generation

        def worker() -> None:
            try:
                sample = client.performance_sample(serial, package)
                error = ""
            except (AdbError, OSError) as exc:
                sample, error = None, str(exc)
            self.sample_results.put((generation, sample, error))

        threading.Thread(target=worker, daemon=True).start()

    def _request_network(self) -> None:
        serial, package = self.monitor_serial, self.monitor_package
        client = self.window.client
        if not serial or not package or client is None or self.network_in_flight:
            return
        self.network_in_flight = True
        generation = self.generation
        uid = self.uid

        def worker() -> None:
            try:
                if uid is None:
                    resolved_uid = client.performance_uid(serial, package)
                else:
                    resolved_uid = uid
                usage = client.performance_network_usage(serial, resolved_uid)
                error = "" if usage is not None else "设备未提供该应用的 UID 流量"
            except (AdbError, OSError) as exc:
                resolved_uid, usage, error = None, None, str(exc)
            self.network_results.put((generation, resolved_uid, usage, error))

        threading.Thread(target=worker, daemon=True).start()

    def _flush_results(self) -> None:
        while not self.package_results.empty():
            serial, packages, error = self.package_results.get_nowait()
            self.package_loading.discard(serial)
            if not error:
                self.package_cache[serial] = packages
            if serial == self.device_serial:
                self._populate_packages(
                    packages, "加载失败，点刷新重试" if error else
                    "无用户安装应用" if not packages else "选择设备应用…")
                if error:
                    self.window.log(f"读取性能监控应用列表失败：{error}")

        while not self.network_results.empty():
            generation, uid, usage, error = self.network_results.get_nowait()
            if generation != self.generation:
                continue
            self.network_in_flight = False
            if uid is not None:
                self.uid = uid
            if usage is not None:
                self.app_network_detail.setText(
                    f"应用 UID {uid} 累计：RX {self._bytes(usage[0])}"
                    f" · TX {self._bytes(usage[1])}（更新可能延迟）")
            else:
                self.app_network_detail.setText(f"应用累计：{error}")

        while not self.sample_results.empty():
            generation, sample, error = self.sample_results.get_nowait()
            if generation != self.generation:
                continue
            self.sample_in_flight = False
            if sample is None:
                self.status.setText(f"采样失败：{error}")
                continue
            self._show_sample(sample)

    def _show_sample(self, sample: PerformanceSample) -> None:
        previous = self.previous
        if sample.process_count:
            cpu = cpu_percent(previous, sample)
            cpu_text = f"{cpu:.1f}%" if cpu is not None else "采样中"
            self.cpu_value.setText(cpu_text)
            self.cpu_detail.setText(f"{sample.process_count} 个进程 · 单核 100%")
            pss = self._bytes(sample.pss_kb * 1024) if sample.pss_kb is not None else "—"
            rss = self._bytes(sample.rss_kb * 1024) if sample.rss_kb is not None else "—"
            self.memory_value.setText(pss)
            self.memory_detail.setText(f"PSS {pss} · RSS {rss}")
        else:
            cpu_text = "—"
            pss = "—"
            self.cpu_value.setText("—")
            self.cpu_detail.setText("应用未运行")
            self.memory_value.setText("—")
            self.memory_detail.setText("应用未运行")

        rate_rx = rate_tx = None
        if (previous is not None and sample.device_rx_bytes is not None
                and sample.device_tx_bytes is not None
                and previous.device_rx_bytes is not None
                and previous.device_tx_bytes is not None):
            seconds = sample.sampled_at - previous.sampled_at
            if seconds > 0:
                rate_rx = max(0, sample.device_rx_bytes - previous.device_rx_bytes) / seconds
                rate_tx = max(0, sample.device_tx_bytes - previous.device_tx_bytes) / seconds
        rx_text = self._bytes(rate_rx) + "/s" if rate_rx is not None else "—"
        tx_text = self._bytes(rate_tx) + "/s" if rate_tx is not None else "—"
        self.network_value.setText(f"RX {rx_text} · TX {tx_text}")
        self.network_detail.setText(
            "设备接口合计 · 实时速率（不含 loopback / VPN）")

        same_source = (previous is not None and previous.layer == sample.layer
                       and previous.fps_source == sample.fps_source)
        latest = (previous.frame_times_ns[-1] if same_source
                  and previous.frame_times_ns else None)
        fps = fps_from_frames(latest, sample.frame_times_ns)
        fps_text = f"{fps:.1f}" if fps is not None else "—"
        if fps is None:
            self.fps_value.setText("—")
        elif sample.fps_source == "gfxinfo":
            self.fps_value.setText(f"{fps_text} FPS · HWUI")
        else:
            self.fps_value.setText(f"{fps_text} FPS")
        if sample.fps_source == "gfxinfo":
            self.fps_detail.setText(
                "来源：gfxinfo · Android View 帧率估算；Flutter / SurfaceView "
                "画面可能未计入")
        else:
            self.fps_detail.setText(
                f"来源：{sample.fps_source or '暂无可用帧数据'}")
        self.layer_detail.setText(f"图层：{sample.layer or '—'}")
        self.status.setText(
            f"正在监控 {self.monitor_package} · {self.monitor_serial}"
            + (" · 应用未运行" if not sample.process_count else "")
            + (f" · {sample.issues[0]}" if sample.issues else ""))

        self.history_tree.insertTopLevelItem(0, QTreeWidgetItem([
            datetime.now().strftime("%H:%M:%S"), cpu_text, pss,
            rx_text, tx_text, fps_text, sample.fps_source or "—",
        ]))
        while self.history_tree.topLevelItemCount() > self.MAX_SAMPLES:
            self.history_tree.takeTopLevelItem(self.history_tree.topLevelItemCount() - 1)
        self.previous = sample


# --- Dart HTTP requests --------------------------------------------------

class DartNetworkPage(QWidget):
    MAX_REQUESTS = 3000

    def __init__(self, window: "MainWindow") -> None:
        super().__init__()
        self.window = window
        self.device_serial: str | None = None
        self.package_cache: dict[str, list[str]] = {}
        self.package_loading: set[str] = set()
        self.events: queue.Queue = queue.Queue()
        self.commands: queue.Queue = queue.Queue()
        self.stop_event: threading.Event | None = None
        self.worker_thread: threading.Thread | None = None
        self.generation = 0
        self.connection_error = False
        self.requests: dict[tuple[str, str], dict] = {}
        self.details: dict[tuple[str, str], dict] = {}
        self.items: dict[tuple[str, str], QTreeWidgetItem] = {}
        self.pending_details: set[tuple[str, str]] = set()
        self.ignored_keys: set[tuple[str, str]] = set()
        self.ignored_order: deque[tuple[str, str]] = deque()
        self.capture_serial = ""
        self.capture_package = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SPACE_4)
        controls = Card(padding=theme.SPACE_4)
        layout.addWidget(controls)

        package_row = QHBoxLayout()
        package_row.setSpacing(theme.SPACE_2)
        package_row.addWidget(label("应用包名"))
        self.package_edit = QLineEdit()
        self.package_edit.setPlaceholderText("com.example.app")
        self.package_edit.textChanged.connect(self._package_changed)
        package_row.addWidget(self.package_edit, 1)
        self.package_box = QComboBox()
        self.package_box.setFixedWidth(theme.LOGCAT_APP_SELECT_WIDTH)
        self.package_box.addItem("选择设备应用…")
        self.package_box.setEnabled(False)
        self.package_box.currentIndexChanged.connect(self._package_selected)
        package_row.addWidget(self.package_box)
        refresh = icon_button("刷新应用", theme.ICON_REFRESH)
        refresh.clicked.connect(lambda: self.refresh_packages(force=True))
        package_row.addWidget(refresh)
        controls.body.addLayout(package_row)

        action_row = QHBoxLayout()
        action_row.setSpacing(theme.SPACE_2)
        self.status = label("请先在设备上打开 Flutter debug/profile 应用", "muted")
        self.status.setWordWrap(True)
        action_row.addWidget(self.status, 1)
        self.start_button = icon_button("开始抓取", "fa6s.play", variant="primary")
        self.start_button.clicked.connect(self.start)
        action_row.addWidget(self.start_button)
        self.stop_button = icon_button("停止", "fa6s.stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        action_row.addWidget(self.stop_button)
        controls.body.addLayout(action_row)
        note = label(
            "读取 Dart VM Service 的 dart:io 请求（包含 Dio）；原生插件请求可能不在列表中。"
            "连接后只显示新请求。", "muted")
        note.setWordWrap(True)
        controls.body.addWidget(note)

        results = Card(padding=theme.SPACE_4)
        toolbar = QHBoxLayout()
        toolbar.addWidget(label("网络请求", "cardTitle"))
        self.count_label = label("0 条请求", "muted")
        toolbar.addWidget(self.count_label)
        toolbar.addStretch(1)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("筛选 URL、方法或状态")
        self.search_edit.setFixedWidth(260)
        self.search_edit.textChanged.connect(self._filter_requests)
        toolbar.addWidget(self.search_edit)
        clear = icon_button("清空", "fa6s.eraser")
        clear.clicked.connect(self.clear_display)
        toolbar.addWidget(clear)
        export = icon_button("导出 JSON", "fa6s.file-arrow-down")
        export.clicked.connect(self.export_json)
        toolbar.addWidget(export)
        results.body.addLayout(toolbar)

        self.request_tree = table([
            ("状态", 62), ("方法", 68), ("请求", 220), ("耗时", 78),
        ], stretch_column=2)
        self.request_tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.request_tree.itemSelectionChanged.connect(self._selected)
        self.request_tree.setMinimumWidth(320)

        inspector = QFrame()
        inspector.setObjectName("networkInspector")
        inspector_layout = QVBoxLayout(inspector)
        inspector_layout.setContentsMargins(theme.SPACE_3, 0, 0, 0)
        inspector_layout.setSpacing(theme.SPACE_2)
        self.detail_title = label("选择一条请求", "cardTitle")
        self.detail_title.setWordWrap(True)
        inspector_layout.addWidget(self.detail_title)
        self.detail_subtitle = label("查看请求和响应的详细内容", "muted")
        self.detail_subtitle.setWordWrap(True)
        inspector_layout.addWidget(self.detail_subtitle)

        self.detail_tabs = QTabWidget()
        self.detail_tabs.setObjectName("networkTabs")
        self.overview_tree = table([("字段", 110), ("值", 240)], stretch_column=1)
        self.overview_tree.setSelectionMode(QAbstractItemView.NoSelection)
        self.detail_tabs.addTab(self.overview_tree, "概览")

        self.request_headers_view = self._detail_text("请求头暂不可用")
        self.request_body_view = self._detail_text("请求正文暂不可用")
        request_tabs = QTabWidget()
        request_tabs.setObjectName("networkTabs")
        request_tabs.addTab(self.request_headers_view, "Headers")
        request_tabs.addTab(self.request_body_view, "Body")
        self.detail_tabs.addTab(request_tabs, "请求")

        self.response_headers_view = self._detail_text("响应头暂不可用")
        self.response_body_view = self._detail_text("响应正文暂不可用")
        response_tabs = QTabWidget()
        response_tabs.setObjectName("networkTabs")
        response_tabs.addTab(self.response_headers_view, "Headers")
        response_tabs.addTab(self.response_body_view, "Body")
        self.detail_tabs.addTab(response_tabs, "响应")

        self.raw_view = self._detail_text("选择一条请求查看原始数据")
        self.detail_tabs.addTab(self.raw_view, "原始数据")
        inspector_layout.addWidget(self.detail_tabs, 1)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setObjectName("networkSplitter")
        splitter.addWidget(self.request_tree)
        splitter.addWidget(inspector)
        splitter.setChildrenCollapsible(False)
        splitter.setSizes([430, 510])
        results.body.addWidget(splitter, 1)
        layout.addWidget(results, 1)

        self.result_timer = QTimer(self)
        self.result_timer.timeout.connect(self._flush)
        self.result_timer.start(100)

    @staticmethod
    def _detail_text(placeholder: str) -> QPlainTextEdit:
        view = QPlainTextEdit()
        view.setObjectName("networkDetailText")
        view.setReadOnly(True)
        view.setLineWrapMode(QPlainTextEdit.NoWrap)
        view.setPlaceholderText(placeholder)
        return view

    def device_changed(self, serial: str | None) -> None:
        if serial == self.device_serial:
            return
        self.stop()
        self.generation += 1
        self.device_serial = serial
        self.package_edit.clear()
        self.clear_display()
        self.ignored_keys.clear()
        self.ignored_order.clear()
        self.status.setText("请先在设备上打开 Flutter debug/profile 应用")
        self._populate_packages(
            self.package_cache.get(serial, []) if serial else [],
            "选择设备应用…" if serial else "未选择设备")
        if serial and self.window.pages.currentIndex() == 5:
            self.refresh_packages()

    def refresh_packages(self, *, force: bool = False) -> None:
        serial, client = self.device_serial, self.window.client
        if serial is None or client is None or serial in self.package_loading:
            return
        if not force and serial in self.package_cache:
            self._populate_packages(self.package_cache[serial])
            return
        self.package_loading.add(serial)
        self._populate_packages([], "正在加载设备应用…")

        def worker() -> None:
            try:
                packages, error = client.list_user_packages(serial), ""
            except AdbError as exc:
                packages, error = [], str(exc)
            self.events.put(("packages", serial, packages, error))

        threading.Thread(target=worker, daemon=True).start()

    def _populate_packages(self, packages: list[str],
                           placeholder: str = "选择设备应用…") -> None:
        current = self.package_edit.text().strip()
        self.package_box.blockSignals(True)
        self.package_box.clear()
        self.package_box.addItem(placeholder)
        for package in packages:
            self.package_box.addItem(package, package)
        self.package_box.setCurrentIndex(max(0, self.package_box.findData(current)))
        self.package_box.setEnabled(bool(packages))
        self.package_box.blockSignals(False)

    def _package_selected(self, _index: int) -> None:
        package = self.package_box.currentData()
        if package:
            self.package_edit.setText(package)

    def _package_changed(self) -> None:
        current = self.package_edit.text().strip()
        index = self.package_box.findData(current)
        if index != self.package_box.currentIndex():
            self.package_box.blockSignals(True)
            self.package_box.setCurrentIndex(max(0, index))
            self.package_box.blockSignals(False)
        if self.worker_thread is not None:
            self.stop()

    def start(self) -> None:
        if self.worker_thread is not None:
            return
        serial = self.window.selected_serial()
        client = self.window.client
        if serial is None or client is None:
            return
        try:
            package = validate_package_name(self.package_edit.text())
        except AdbError as exc:
            AppMessageBox.warning(self, "应用包名", str(exc))
            return
        self.clear_display()
        self.ignored_keys.clear()
        self.ignored_order.clear()
        self.generation += 1
        generation = self.generation
        self.capture_serial, self.capture_package = serial, package
        self.connection_error = False
        self.stop_event = threading.Event()
        self.commands = queue.Queue()
        stop_event, commands = self.stop_event, self.commands
        self.status.setText(f"正在连接 {package} 的 Dart VM Service…")
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)

        def worker() -> None:
            session = DartNetworkSession(client, serial, package)
            try:
                session.connect()
                self.events.put(("connected", generation, len(session.isolates)))
                while not stop_event.is_set():
                    while not commands.empty() and not stop_event.is_set():
                        key = commands.get_nowait()
                        try:
                            detail = session.request_detail(*key)
                            self.events.put(("detail", generation, key, detail))
                        except (AdbError, DartNetworkError) as exc:
                            self.events.put(("detail_error", generation, key, str(exc)))
                    if stop_event.is_set():
                        break
                    requests = session.poll()
                    if requests:
                        self.events.put(("requests", generation, requests))
                    stop_event.wait(1.5)
            except (AdbError, DartNetworkError, OSError) as exc:
                self.events.put(("error", generation, str(exc)))
            except Exception as exc:
                self.events.put(("error", generation,
                                 f"处理 Dart 请求时出错：{type(exc).__name__}"))
            finally:
                session.close()
                self.events.put(("closed", generation))

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()

    def stop(self, *, wait: bool = False) -> None:
        if self.stop_event is not None:
            self.stop_event.set()
            self.stop_button.setEnabled(False)
            self.status.setText("正在停止抓取并清理 ADB 端口转发…")
        if wait and self.worker_thread is not None:
            self.worker_thread.join(timeout=20)

    def _flush(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                return
            kind = event[0]
            if kind == "packages":
                _, serial, packages, error = event
                self.package_loading.discard(serial)
                if not error:
                    self.package_cache[serial] = packages
                if serial == self.device_serial:
                    self._populate_packages(
                        packages, "加载失败，点刷新重试" if error else
                        "无用户安装应用" if not packages else "选择设备应用…")
                continue
            generation = event[1]
            if kind == "closed":
                self.worker_thread = None
                self.stop_event = None
                self.start_button.setEnabled(True)
                self.stop_button.setEnabled(False)
                if generation == self.generation and not self.connection_error:
                    self.status.setText(f"已停止 · 保留 {len(self.requests)} 条请求")
                continue
            if generation != self.generation:
                continue
            if kind == "connected":
                if self.stop_event is not None and not self.stop_event.is_set():
                    self.status.setText(
                        f"正在抓取 {self.capture_package} · {self.capture_serial}"
                        f" · {event[2]} 个 Dart isolate")
            elif kind == "requests":
                self._show_requests(event[2])
            elif kind == "detail":
                _, _, key, detail = event
                self.pending_details.discard(key)
                if key not in self.requests:
                    continue
                self.details[key] = detail
                if self._selected_key() == key:
                    self._render_detail(key, detail)
            elif kind == "detail_error":
                _, _, key, error = event
                self.pending_details.discard(key)
                if self._selected_key() == key:
                    self._detail_state(f"无法读取请求详情：{error}")
            elif kind == "error":
                self.connection_error = True
                self.status.setText(f"抓取失败：{event[2]}")

    def _show_requests(self, requests: list[dict]) -> None:
        for request in requests:
            key = request_key(request)
            if key in self.ignored_keys:
                continue
            old = self.requests.get(key)
            if old == request:
                continue
            self.requests[key] = request
            self.details.pop(key, None)
            response = request.get("response") or {}
            status = str(response.get("statusCode") or
                         ("失败" if (request.get("request") or {}).get("error")
                          else "进行中"))
            started, ended = request.get("startTime"), request.get("endTime")
            duration = (f"{(ended - started) / 1000:.0f} ms"
                        if isinstance(started, int) and isinstance(ended, int)
                        else "—")
            uri = str(request.get("uri", "—"))
            parsed = urlsplit(uri)
            request_name = (parsed.netloc + parsed.path +
                            (f"?{parsed.query}" if parsed.query else "")) or uri
            values = [status, str(request.get("method", "—")),
                      request_name, duration]
            item = self.items.get(key)
            if item is None:
                item = QTreeWidgetItem(values)
                item.setData(0, Qt.UserRole, key)
                self.items[key] = item
                self.request_tree.insertTopLevelItem(0, item)
            else:
                for index, value in enumerate(values):
                    item.setText(index, value)
            item.setToolTip(2, uri)
            if status.startswith(("4", "5")) or status == "失败":
                for index in range(len(values)):
                    item.setForeground(index, QColor(theme.DANGER))
            else:
                for index in range(len(values)):
                    item.setForeground(index, QColor(theme.TEXT))
            if self._selected_key() == key:
                self._selected()
        while self.request_tree.topLevelItemCount() > self.MAX_REQUESTS:
            item = self.request_tree.takeTopLevelItem(
                self.request_tree.topLevelItemCount() - 1)
            key = item.data(0, Qt.UserRole)
            self.items.pop(key, None)
            self.requests.pop(key, None)
            self.details.pop(key, None)
            self._ignore_keys((key,))
        self._filter_requests()

    def _selected_key(self) -> tuple[str, str] | None:
        selected = self.request_tree.selectedItems()
        return selected[0].data(0, Qt.UserRole) if selected else None

    def _selected(self) -> None:
        key = self._selected_key()
        if key is None:
            self.detail_title.setText("选择一条请求")
            self.detail_subtitle.setText("查看请求和响应的详细内容")
            self._detail_state("")
        elif key in self.details:
            self._render_detail(key, self.details[key])
        elif key in self.pending_details:
            self._detail_state("正在读取请求详情…")
        elif self.worker_thread is not None:
            self.pending_details.add(key)
            self.commands.put(key)
            self._detail_state("正在读取请求详情…")
        else:
            self._detail_state("抓取已停止，无法读取这条请求的详情。")

        if key is not None:
            request = self.requests.get(key, {})
            self.detail_title.setText(
                f"{request.get('method', '—')}  {request.get('uri', '—')}")
            self.detail_subtitle.setText(self.capture_package)

    def _detail_state(self, message: str) -> None:
        self.overview_tree.clear()
        for view in (self.request_headers_view, self.request_body_view,
                     self.response_headers_view, self.response_body_view):
            view.setPlainText(message)
        self.raw_view.setPlainText(message)

    def _render_detail(self, key: tuple[str, str], detail: dict) -> None:
        summary = self.requests.get(key, {})
        request = detail.get("request") or summary.get("request") or {}
        response = detail.get("response") or summary.get("response") or {}
        if not isinstance(request, dict):
            request = {}
        if not isinstance(response, dict):
            response = {}
        uri = str(detail.get("uri") or summary.get("uri") or "—")
        method = str(detail.get("method") or summary.get("method") or "—")
        parsed = urlsplit(uri)
        started = detail.get("startTime", summary.get("startTime"))
        ended = detail.get("endTime", summary.get("endTime"))
        duration = (f"{(ended - started) / 1000:.0f} ms"
                    if isinstance(started, int) and isinstance(ended, int)
                    else "—")
        try:
            when = (datetime.fromtimestamp(started / 1_000_000)
                    .strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                    if isinstance(started, int) else "—")
        except (ValueError, OverflowError, OSError):
            when = "—"
        status = response.get("statusCode") or "进行中"
        rows = [
            ("URL", uri), ("方法", method), ("状态", str(status)),
            ("主机", parsed.netloc or "—"),
            ("路径", parsed.path or "/"), ("开始时间", when),
            ("耗时", duration), ("Dart isolate", key[0]),
        ]
        self.overview_tree.clear()
        for name, value in rows:
            row = QTreeWidgetItem([name, value])
            row.setToolTip(1, value)
            self.overview_tree.addTopLevelItem(row)

        self.request_headers_view.setPlainText(self._format_headers(
            request.get("headers") or detail.get("requestHeaders")))
        self.response_headers_view.setPlainText(self._format_headers(
            response.get("headers") or detail.get("responseHeaders")))
        self.request_body_view.setPlainText(self._format_body(
            detail.get("requestBody", request.get("body"))))
        self.response_body_view.setPlainText(self._format_body(
            detail.get("responseBody", response.get("body"))))
        self.raw_view.setPlainText(self._format_detail(detail))

    @staticmethod
    def _format_headers(headers: object) -> str:
        if isinstance(headers, dict):
            return "\n".join(
                f"{name}: {', '.join(map(str, value)) if isinstance(value, list) else value}"
                for name, value in headers.items()) or "无请求头或响应头数据"
        if isinstance(headers, list):
            return "\n".join(map(str, headers)) or "无请求头或响应头数据"
        return str(headers) if headers else "无请求头或响应头数据"

    @staticmethod
    def _format_body(body: object) -> str:
        if body is None:
            return "未记录正文"
        if isinstance(body, list):
            try:
                body = bytes(body)
            except (ValueError, TypeError):
                return json.dumps(body, ensure_ascii=False, indent=2)
        if isinstance(body, bytes):
            try:
                content = body[:65536].decode("utf-8")
            except UnicodeDecodeError:
                return f"二进制正文 · {len(body)} 字节\n\n{body[:256].hex(' ')}"
            if len(body) > 65536:
                content += f"\n…已截断，完整正文共 {len(body)} 字节"
        elif isinstance(body, (dict, list)):
            return json.dumps(body, ensure_ascii=False, indent=2)
        else:
            content = str(body)
        if len(content) <= 200000:
            try:
                return json.dumps(json.loads(content), ensure_ascii=False, indent=2)
            except (ValueError, TypeError):
                pass
        return content[:200000] + ("\n…正文已截断" if len(content) > 200000 else "")

    @staticmethod
    def _format_detail(detail: dict) -> str:
        display = dict(detail)
        for field in ("requestBody", "responseBody"):
            body = display.get(field)
            if isinstance(body, list):
                try:
                    data = bytes(body)
                except (ValueError, TypeError):
                    continue
                preview = data[:65536].decode("utf-8", "replace")
                display[field] = preview + (
                    f"\n…已截断，完整正文共 {len(data)} 字节"
                    if len(data) > 65536 else "")
        text = json.dumps(display, ensure_ascii=False, indent=2)
        return text[:200000] + ("\n…详情已截断" if len(text) > 200000 else "")

    def _filter_requests(self) -> None:
        needle = self.search_edit.text().strip().casefold()
        visible = 0
        for key, item in self.items.items():
            haystack = " ".join(item.text(index) for index in (0, 1))
            haystack += " " + str(self.requests.get(key, {}).get("uri", ""))
            matched = needle in haystack.casefold()
            item.setHidden(not matched)
            visible += matched
        total = len(self.requests)
        self.count_label.setText(
            f"{visible} / {total} 条请求" if needle else f"{total} 条请求")

    def clear_display(self) -> None:
        self._ignore_keys(self.requests)
        self.requests.clear()
        self.details.clear()
        self.items.clear()
        self.pending_details.clear()
        self.request_tree.clear()
        self.detail_title.setText("选择一条请求")
        self.detail_subtitle.setText("查看请求和响应的详细内容")
        self._detail_state("")
        self.count_label.setText("0 条请求")

    def _ignore_keys(self, keys) -> None:
        for key in keys:
            if key in self.ignored_keys:
                continue
            if len(self.ignored_order) >= self.MAX_REQUESTS * 4:
                self.ignored_keys.discard(self.ignored_order.popleft())
            self.ignored_keys.add(key)
            self.ignored_order.append(key)

    def export_json(self) -> None:
        if not self.requests:
            AppMessageBox.information(self, "导出请求", "当前没有可导出的请求。")
            return
        name = f"dart_network_{datetime.now():%Y%m%d_%H%M%S}.json"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 Dart 请求列表", str(Path.home() / "Downloads" / name),
            "JSON 文件 (*.json)")
        if not path:
            return
        payload = {
            "device": self.capture_serial,
            "package": self.capture_package,
            "exportedAt": datetime.now().isoformat(timespec="seconds"),
            "requests": [self.details.get(key, request)
                         for key, request in self.requests.items()],
        }
        try:
            Path(path).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            AppMessageBox.warning(self, "导出请求", str(exc))
            return
        self.window.set_status(f"已导出 {len(self.requests)} 条 Dart 请求")


# --- Shell ---------------------------------------------------------------

class MainWindow(QMainWindow):
    PAGES = (
        ("apps", "应用管理", "安装与卸载设备上的应用", theme.ICON_APPS),
        ("shot", "屏幕截图", "把当前屏幕保存到本机", theme.ICON_SHOT),
        ("files", "文件管理", "双击文件夹进入 · 可多选传输", theme.ICON_FILES),
        ("logcat", "实时日志", "查看 Logcat / HiLog 与抓取崩溃文件", theme.ICON_LOG),
        ("performance", "性能监控", "查看应用 CPU、内存、流量与帧率", theme.ICON_PERFORMANCE),
        ("dart_network", "Dart 网络", "抓取 Flutter debug/profile 应用的网络请求", theme.ICON_DART_NETWORK),
    )

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Sandevistan ADB")
        self.resize(theme.WINDOW_WIDTH, theme.WINDOW_HEIGHT)
        self.setMinimumSize(960, 680)

        self.client: AdbClient | None = None
        self.devices: dict[str, Device] = {}
        self.install_targets: set[str] = set()
        self.busy = False
        self.events: queue.Queue = queue.Queue()
        self.screenshot_history = ScreenshotHistory()
        self.log_view: QPlainTextEdit | None = None
        self.status_text = "就绪"

        root = QWidget()
        root.setObjectName("workspace")
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._sidebar())

        workspace = QWidget()
        workspace.setObjectName("workspace")
        page_layout = QVBoxLayout(workspace)
        page_layout.setContentsMargins(
            theme.PAGE_PADDING, theme.PAGE_PADDING,
            theme.PAGE_PADDING, theme.PAGE_PADDING)
        page_layout.setSpacing(theme.SPACE_4)

        self.title_label = label("", "pageTitle")
        self.subtitle_label = label("", "pageSubtitle")
        header = QVBoxLayout()
        header.setSpacing(theme.SPACE_1)
        header.addWidget(self.title_label)
        header.addWidget(self.subtitle_label)
        page_layout.addLayout(header)

        page_layout.addWidget(self._device_card())

        self.apps_page = AppsPage(self)
        self.shot_page = ShotPage(self)
        self.files_page = FilesPage(self)
        self.logcat_page = LogcatPage(self)
        self.performance_page = PerformancePage(self)
        self.dart_network_page = DartNetworkPage(self)
        self.pages = QStackedWidget()
        self.pages.addWidget(scrollable(self.apps_page, 620))
        self.pages.addWidget(scrollable(self.shot_page, 500))
        self.pages.addWidget(scrollable(self.files_page, 460))
        self.pages.addWidget(scrollable(self.logcat_page, 560))
        self.pages.addWidget(scrollable(self.performance_page, 620))
        self.pages.addWidget(scrollable(self.dart_network_page, 620))
        page_layout.addWidget(self.pages, 1)

        layout.addWidget(workspace, 1)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._drain_events)
        self.timer.start(100)

        self._show_page(0)

        try:
            self.client = AdbClient()
        except AdbError as exc:
            self.set_status("ADB 不可用")
            self.adb_health.setText("ADB 不可用")
            self.log(str(exc))
            AppMessageBox.critical(self, "缺少 ADB", str(exc))
        else:
            self.set_status("ADB 已就绪，正在查找设备")
            self.adb_health.setText("ADB 已就绪")
            self.refresh_devices()

    # -- status and log --------------------------------------------------

    def set_status(self, text: str) -> None:
        self.status_text = text
        if hasattr(self, "log_status"):
            self.log_status.setText(text)

    def log(self, line: str) -> None:
        if self.log_view is not None:
            self.log_view.appendPlainText(line)

    # -- background work -------------------------------------------------

    def submit(self, title: str, work: Callable[[], object],
               done: Callable[[object], None] | None = None,
               finished: Callable[[], None] | None = None,
               log_result: bool = True) -> bool:
        if self.busy:
            self.log(f"… {title}：已有任务进行中，请稍候")
            return False
        self.busy = True
        self.setCursor(Qt.WaitCursor)
        self.log(f"▶ {title}")
        self.set_status(f"{title}中…")

        def runner() -> None:
            try:
                result = work()
                self.events.put((True, title, result, done, finished, log_result))
            except (AdbError, OSError) as exc:
                self.events.put((False, title, str(exc), None, finished, False))

        threading.Thread(target=runner, daemon=True).start()
        return True

    def _drain_events(self) -> None:
        while True:
            try:
                ok, title, result, done, finished, log_result = self.events.get_nowait()
            except queue.Empty:
                return
            self.busy = False
            self.unsetCursor()
            if finished is not None:
                finished()
            if ok:
                detail = ((f"{len(result)} 项" if isinstance(result, list)
                           else str(result)) if log_result else "")
                self.log(f"✓ {title}完成" + (f"：{detail}" if detail else ""))
                self.set_status(f"{title}完成")
                if done is not None:
                    done(result)
            else:
                self.log(f"✗ {title}失败：{result}")
                self.set_status(f"{title}失败")
                AppMessageBox.warning(self, title, str(result))

    # -- devices ---------------------------------------------------------

    def _device_card(self) -> Card:
        card = Card(padding=theme.SPACE_4)
        card.body.setSpacing(theme.SPACE_2)
        card.setObjectName("cardPlain")

        row = QHBoxLayout()
        row.setSpacing(theme.SPACE_2)
        row.addWidget(label("当前设备", "cardTitle"))
        self.device_box = QComboBox()
        self.device_box.setMinimumWidth(180)
        self.device_box.currentIndexChanged.connect(self._device_changed)
        row.addWidget(self.device_box, 1)
        self.device_count = label("未发现设备", "accent")
        row.addWidget(self.device_count)
        wifi = icon_button("Wi-Fi 连接", theme.ICON_WIFI)
        wifi.clicked.connect(lambda: WifiDialog(self).exec())
        row.addWidget(wifi)
        refresh = icon_button("刷新设备", theme.ICON_REFRESH)
        refresh.clicked.connect(lambda: self.refresh_devices())
        row.addWidget(refresh)
        card.body.addLayout(row)
        return card

    def ready_devices(self) -> list[Device]:
        return [device for device in self.devices.values()
                if device.state == "device"]

    def current_device(self) -> Device | None:
        device = self.device_box.currentData()
        return device if isinstance(device, Device) else None

    def selected_serial(self) -> str | None:
        if self.client is None:
            AppMessageBox.critical(self, "缺少 ADB", "请先安装 Android SDK Platform-Tools。")
            return None
        device = self.current_device()
        if device is None:
            AppMessageBox.information(self, "没有设备", "请连接手机并点击「刷新设备」。")
            return None
        if device.state != "device":
            AppMessageBox.warning(
                self, "设备尚未就绪",
                f"当前状态：{device.state}。请在手机上允许 USB 调试。")
            return None
        return device.serial

    def refresh_devices(self, preferred_serial: str | None = None) -> None:
        if self.client is None:
            return
        previous = self.current_device()

        def done(devices: list[Device]) -> None:
            self.devices = {device.label: device for device in devices}
            self.device_box.blockSignals(True)
            self.device_box.clear()
            for label_text, device in self.devices.items():
                self.device_box.addItem(label_text, device)
            ready = sum(device.state == "device" for device in devices)
            self.device_count.setText(
                f"{ready} 台在线" if ready else "未发现设备")
            wanted = preferred_serial or (previous.serial if previous else None)
            index = next(
                (i for i in range(self.device_box.count())
                 if self.device_box.itemData(i).serial == wanted), -1)
            if index < 0:
                index = next(
                    (i for i in range(self.device_box.count())
                     if self.device_box.itemData(i).state == "device"), 0)
            if self.device_box.count():
                self.device_box.setCurrentIndex(index)
            self.device_box.blockSignals(False)
            self._device_changed()
            if not devices:
                self.set_status("未发现设备。请检查 USB 连接和调试授权。")
                self.log("未发现设备")

        self.submit("刷新设备", self.client.list_devices, done)

    def _device_changed(self, _index: int = 0) -> None:
        device = self.current_device()
        self.files_page.remote.title_label.setText(
            f"设备 · {device.display_name}" if device else "设备文件")
        self.apps_page.device_changed(device.serial if device else None)
        self.logcat_page.device_changed(device.serial if device else None)
        self.performance_page.device_changed(
            device.serial if device and device.state == "device" else None)
        self.dart_network_page.device_changed(
            device.serial if device and device.state == "device" else None)
        self.apps_page._update_target_summary()
        if device is not None and device.state == "device" \
                and self.pages.currentIndex() == 2:
            self.files_page.remote.refresh()
        self.set_status(
            f"当前设备：{device.display_name}" if device else "未选择设备")

    def install_target_devices(self) -> list[Device]:
        ready = self.ready_devices()
        mode = self.apps_page.target_mode.currentIndex()
        if mode == 1:
            return ready
        if mode == 2:
            return [device for device in ready
                    if device.serial in self.install_targets]
        current = self.current_device()
        return [current] if current and current.state == "device" else []

    def choose_install_targets(self, initial: set[str]) -> bool:
        if not self.ready_devices():
            AppMessageBox.information(self, "安装设备", "请先连接设备并点击「刷新设备」。")
            return False
        dialog = TargetsDialog(self, initial)
        if dialog.exec() != QDialog.Accepted or dialog.result_serials is None:
            return False
        self.install_targets = dialog.result_serials
        return True

    # -- pages -----------------------------------------------------------

    def _show_page(self, index: int) -> None:
        if self.pages.currentIndex() == 4 and index != 4:
            self.performance_page.stop()
        self.pages.setCurrentIndex(index)
        _, title, subtitle, _ = self.PAGES[index]
        self.title_label.setText(title)
        self.subtitle_label.setText(subtitle)
        if index == 2:
            self.files_page.remote.refresh()
        elif index == 3:
            self.logcat_page.refresh_packages()
        elif index == 4:
            self.performance_page.refresh_packages()
        elif index == 5:
            self.dart_network_page.refresh_packages()

    def _sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(theme.SIDEBAR_WIDTH)
        column = QVBoxLayout(sidebar)
        column.setContentsMargins(
            theme.SPACE_4, theme.SPACE_6, theme.SPACE_4, theme.SPACE_6)
        column.setSpacing(theme.SPACE_2)

        brand = QHBoxLayout()
        brand.setSpacing(theme.SPACE_3)
        mark = QLabel()
        mark.setPixmap(qta.icon(theme.ICON_DEVICE, color="#FFFFFF").pixmap(20, 20))
        brand.addWidget(mark)
        names = QVBoxLayout()
        names.setSpacing(0)
        names.addWidget(label("SANDEVISTAN", "brandName"))
        names.addWidget(label("ADB 控制台", "brandSub"))
        brand.addLayout(names)
        brand.addStretch(1)
        column.addLayout(brand)
        column.addSpacing(theme.SPACE_8)

        column.addWidget(label("工作台", "sidebarSection"))
        column.addSpacing(theme.SPACE_1)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        for index, (_, title, _, icon_name) in enumerate(self.PAGES):
            button = QPushButton(title)
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setIcon(qta.icon(icon_name, color=theme.SIDEBAR_TEXT))
            button.setIconSize(QSize(15, 15))
            if index == 0:
                button.setChecked(True)
            self.nav_group.addButton(button, index)
            column.addWidget(button)
        self.nav_group.idClicked.connect(self._show_page)

        column.addStretch(1)

        licenses = QPushButton("Qt LGPLv3 · 第三方许可")
        licenses.setObjectName("navButton")
        licenses.setCursor(Qt.PointingHandCursor)
        licenses.clicked.connect(self._show_licenses)
        column.addWidget(licenses)

        status = QHBoxLayout()
        status.setSpacing(theme.SPACE_2)
        dot = QLabel("●")
        dot.setStyleSheet(f"color: {theme.SUCCESS}; font-size: 10pt;")
        status.addWidget(dot)
        self.adb_health = label("检查 ADB 中", "brandSub")
        status.addWidget(self.adb_health)
        status.addStretch(1)
        column.addLayout(status)
        return sidebar

    def _show_licenses(self) -> None:
        notice = Path(__file__).parent / "licenses" / "THIRD_PARTY_NOTICES.md"
        dialog = QDialog(self)
        dialog.setWindowTitle("第三方许可")
        dialog.resize(760, 540)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label("本应用使用 Qt（LGPLv3）及其他开源组件。", "cardTitle"))
        viewer = QPlainTextEdit()
        viewer.setReadOnly(True)
        try:
            viewer.setPlainText(notice.read_text(encoding="utf-8"))
        except OSError as exc:
            viewer.setPlainText(f"无法读取许可说明：{exc}")
        layout.addWidget(viewer, 1)
        close = QPushButton("关闭")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close, alignment=Qt.AlignRight)
        dialog.exec()

    def closeEvent(self, event) -> None:
        self.logcat_page.stop()
        self.performance_page.stop()
        self.dart_network_page.stop(wait=True)
        super().closeEvent(event)


def main() -> int:
    prepare_qt_plugins()
    app = QApplication(sys.argv)
    app.setApplicationName("Sandevistan ADB")
    app.setStyle("Fusion")
    app.setStyleSheet(load_stylesheet())
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
