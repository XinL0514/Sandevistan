"""Persistent screenshot records and Finder integration for macOS."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


def default_history_path() -> Path:
    if sys.platform == "win32":
        app_data = Path(os.environ.get(
            "APPDATA", Path.home() / "AppData" / "Roaming"))
        return app_data / "Sandevistan ADB" / "screenshot_history.json"
    return (Path.home() / "Library" / "Application Support" /
            "Sandevistan ADB" / "screenshot_history.json")


DEFAULT_HISTORY_PATH = default_history_path()
MAX_HISTORY_ITEMS = 200


@dataclass(frozen=True)
class ScreenshotRecord:
    path: Path
    captured_at: str
    device_name: str

    @property
    def display_time(self) -> str:
        try:
            return datetime.fromisoformat(self.captured_at).astimezone().strftime(
                "%Y-%m-%d %H:%M"
            )
        except ValueError:
            return self.captured_at


class ScreenshotHistory:
    def __init__(self, storage_path: Path = DEFAULT_HISTORY_PATH) -> None:
        self.storage_path = storage_path
        self.entries = self._load()

    def _load(self) -> list[ScreenshotRecord]:
        try:
            data = json.loads(self.storage_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        entries = []
        for item in data:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            captured_at = item.get("captured_at")
            device_name = item.get("device_name")
            if (isinstance(path, str) and path
                    and isinstance(captured_at, str)
                    and isinstance(device_name, str)):
                entries.append(ScreenshotRecord(Path(path), captured_at, device_name))
        return entries[:MAX_HISTORY_ITEMS]

    def add(self, path: Path, device_name: str) -> ScreenshotRecord:
        path = path.expanduser().resolve()
        entry = ScreenshotRecord(path, datetime.now().astimezone().isoformat(
            timespec="seconds"), device_name)
        self.entries = [entry, *(
            old for old in self.entries if old.path != path
        )][:MAX_HISTORY_ITEMS]
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.storage_path.with_suffix(".json.tmp")
        try:
            temporary.write_text(json.dumps([
                {"path": str(item.path), "captured_at": item.captured_at,
                 "device_name": item.device_name}
                for item in self.entries
            ], ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.storage_path)
        finally:
            temporary.unlink(missing_ok=True)
        return entry


def reveal_in_file_manager(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"文件不存在，可能已移动或删除：{path}")
    if sys.platform == "win32":
        try:
            subprocess.Popen(["explorer.exe", f"/select,{path}"])
        except OSError as exc:
            raise OSError(f"无法打开资源管理器：{exc}") from exc
        return str(path)
    try:
        result = subprocess.run(
            ["open", "-R", str(path)], capture_output=True, text=True,
            timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OSError(f"无法打开 Finder：{exc}") from exc
    if result.returncode:
        raise OSError(result.stderr.strip() or "Finder 未能显示该文件。")
    return str(path)
