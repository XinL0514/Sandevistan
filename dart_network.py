"""Read dart:io HTTP profiles from a running Flutter VM Service.

The VM Service address is discovered from the selected app process' logcat
output. ADB forwards its loopback port only for the lifetime of this session.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING

from adb_client import AdbError, validate_package_name

if TYPE_CHECKING:
    from adb_client import AdbClient


class DartNetworkError(Exception):
    """The Dart VM Service could not be reached or queried."""


_SERVICE_LINE = re.compile(
    r"The Dart VM service is listening on (https?://[^\s]+)", re.I)
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def find_service_uri(logcat: str) -> urllib.parse.SplitResult | None:
    """Accept only a loopback VM Service address; never expose its token."""
    for candidate in reversed(_SERVICE_LINE.findall(logcat)):
        parsed = urllib.parse.urlsplit(candidate.rstrip(".,"))
        try:
            valid_port = parsed.port is not None and 0 < parsed.port < 65536
        except ValueError:
            continue
        if (parsed.scheme == "http" and parsed.hostname in
                ("127.0.0.1", "localhost", "::1") and valid_port
                and parsed.path.startswith("/") and parsed.path.endswith("/")
                and ".." not in parsed.path):
            return parsed
    return None


def request_key(request: dict) -> tuple[str, str]:
    return str(request.get("isolateId", "")), str(request.get("id", ""))


class DartNetworkSession:
    """Single-worker VM Service connection; close always removes ADB forward."""

    def __init__(self, client: AdbClient, serial: str, package: str) -> None:
        self.client = client
        self.serial = serial
        self.package = validate_package_name(package)
        self.local_port: str | None = None
        self.base_url = ""
        self.isolates: list[str] = []
        self.enabled_by_us: list[str] = []
        self.before_start: set[tuple[str, str]] = set()
        self.last_timestamp: dict[str, int] = {}

    def connect(self) -> None:
        try:
            pids = self.client.run(
                self.serial, "shell", "pidof", self.package, timeout=10).split()
        except AdbError as exc:
            raise DartNetworkError(
                "应用未运行。请先在设备上打开 debug/profile 应用。") from exc
        if not pids or not pids[0].isdigit():
            raise DartNetworkError("应用未运行。请先在设备上打开 debug/profile 应用。")
        output = self.client.run(
            self.serial, "shell", "logcat", "-d", "-t", "12000",
            "-v", "brief", f"--pid={pids[0]}", timeout=30)
        service = find_service_uri(output)
        if service is None:
            raise DartNetworkError(
                "未找到 Dart VM Service。请确认是 Flutter debug/profile 包，"
                "并在设备上重新启动应用后重试。")
        try:
            port = self.client.run(
                self.serial, "forward", "tcp:0", f"tcp:{service.port}",
                timeout=10).strip()
            if not port.isdigit():
                raise DartNetworkError("ADB 未返回有效的本地转发端口。")
            self.local_port = port
            self.base_url = f"http://127.0.0.1:{port}{service.path}"
            vm = self._rpc("getVM")
            for isolate in vm.get("isolates", []):
                isolate_id = isolate.get("id")
                if not isinstance(isolate_id, str):
                    continue
                try:
                    state = self._rpc(
                        "ext.dart.io.httpEnableTimelineLogging",
                        isolateId=isolate_id)
                    baseline = self._rpc(
                        "ext.dart.io.getHttpProfile", isolateId=isolate_id)
                    self.before_start.update(
                        request_key(request)
                        for request in baseline.get("requests", [])
                        if isinstance(request, dict))
                    if not state.get("enabled", False):
                        self._rpc(
                            "ext.dart.io.httpEnableTimelineLogging",
                            isolateId=isolate_id, enabled="true")
                        self.enabled_by_us.append(isolate_id)
                    self.isolates.append(isolate_id)
                except DartNetworkError:
                    continue  # An isolate without dart:io is not a fatal error.
            if not self.isolates:
                raise DartNetworkError("该应用未提供 Dart HTTP 请求记录接口。")
        except Exception:
            self.close()
            raise

    def _rpc(self, method: str, **params: str | int) -> dict:
        if not self.base_url:
            raise DartNetworkError("Dart VM Service 尚未连接。")
        url = self.base_url + method
        if params:
            url += "?" + urllib.parse.urlencode(params)
        try:
            with _OPENER.open(url, timeout=8) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise DartNetworkError("请求详情过大，超过 8 MB 显示上限。")
                payload = json.loads(raw)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            # urllib errors can contain the VM Service auth token in the URL.
            raise DartNetworkError(f"Dart VM Service 请求失败：{method}") from exc
        if not isinstance(payload, dict) or "error" in payload:
            error = payload.get("error") if isinstance(payload, dict) else None
            message = (error.get("message", "未知错误")
                       if isinstance(error, dict) else str(error or "响应格式错误"))
            raise DartNetworkError(f"Dart VM Service {method}：{message}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise DartNetworkError(f"Dart VM Service {method} 返回格式错误。")
        return result

    def poll(self) -> list[dict]:
        requests: list[dict] = []
        for isolate_id in self.isolates:
            params: dict[str, str | int] = {"isolateId": isolate_id}
            if isolate_id in self.last_timestamp:
                params["updatedSince"] = max(
                    0, self.last_timestamp[isolate_id] - 1_000_000)
            profile = self._rpc("ext.dart.io.getHttpProfile", **params)
            timestamp = profile.get("timestamp")
            if isinstance(timestamp, int):
                self.last_timestamp[isolate_id] = timestamp
            for request in profile.get("requests", []):
                if not isinstance(request, dict):
                    continue
                request.setdefault("isolateId", isolate_id)
                key = request_key(request)
                if key[1] and key not in self.before_start:
                    requests.append(request)
        return requests

    def request_detail(self, isolate_id: str, request_id: str) -> dict:
        if isolate_id not in self.isolates:
            raise DartNetworkError("请求所属的 Dart isolate 已断开。")
        return self._rpc(
            "ext.dart.io.getHttpProfileRequest",
            isolateId=isolate_id, id=request_id)

    def close(self) -> None:
        for isolate_id in self.enabled_by_us:
            try:
                self._rpc("ext.dart.io.httpEnableTimelineLogging",
                          isolateId=isolate_id, enabled="false")
            except DartNetworkError:
                pass
        self.enabled_by_us.clear()
        if self.local_port:
            try:
                self.client.run(self.serial, "forward", "--remove",
                                f"tcp:{self.local_port}", timeout=10)
            except AdbError:
                pass
        self.local_port = None
        self.base_url = ""
