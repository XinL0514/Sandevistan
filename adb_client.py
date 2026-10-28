"""Small, argument-safe wrapper around the official adb executable."""

from __future__ import annotations

import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from device_info import DeviceInfo, parse_device_info
from performance_tools import (
    PerformanceSample, parse_device_network, parse_gfxinfo_frames,
    parse_proc_stats, parse_surface_latency, parse_surface_layers,
    parse_uid_network,
)


class AdbError(Exception):
    """An adb command failed or adb could not be found."""


@dataclass(frozen=True)
class Device:
    serial: str
    state: str
    model: str = ""
    name: str = ""

    @property
    def display_name(self) -> str:
        return self.name or self.model.replace("_", " ") or self.serial

    @property
    def is_wifi(self) -> bool:
        return (
            (":" in self.serial or "._adb-tls-connect._tcp" in self.serial)
            and not self.serial.startswith("emulator-")
        )

    @property
    def label(self) -> str:
        connection = "Wi-Fi" if self.is_wifi else "USB"
        state = "" if self.state == "device" else f" · {self.state}"
        return f"{self.display_name} · {connection} · {self.serial}{state}"


@dataclass(frozen=True)
class RemoteEntry:
    name: str
    path: str
    is_dir: bool
    size: int | None = None
    modified: str = ""


@dataclass(frozen=True)
class InstallResult:
    serial: str
    success: bool
    detail: str


@dataclass(frozen=True)
class AppPermission:
    name: str
    granted: bool | None
    runtime: bool


@dataclass(frozen=True)
class AppProcess:
    pid: int
    name: str
    rss_kb: int | None


@dataclass(frozen=True)
class AppProcessReport:
    processes: list[AppProcess]
    total_pss_kb: int | None
    total_rss_kb: int | None
    meminfo: str


@dataclass(frozen=True)
class CrashPullResult:
    folder: Path
    anr_count: int
    tombstone_count: int
    bugreport: Path | None = None
    bugreport_text: Path | None = None


PACKAGE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
ACTIVITY_NAME = re.compile(r"\.?[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*")


def validate_package_name(package: str) -> str:
    package = package.strip()
    if not PACKAGE_NAME.fullmatch(package):
        raise AdbError("请输入有效的应用包名。")
    return package


def monkey_arguments(serial: str, package: str, events: int,
                     throttle_ms: int) -> list[str]:
    """Build an argument-safe, package-scoped Android Monkey command."""
    package = validate_package_name(package)
    if not isinstance(events, int) or not 1 <= events <= 100000:
        raise AdbError("Monkey 事件数须在 1 到 100000 之间。")
    if not isinstance(throttle_ms, int) or not 0 <= throttle_ms <= 10000:
        raise AdbError("Monkey 事件间隔须在 0 到 10000 毫秒之间。")
    return ["-s", serial, "shell", "monkey", "-p", package,
            "--throttle", str(throttle_ms), "-v", str(events)]


def monkey_result(output: str, exit_code: int, requested: int) -> tuple[bool, str]:
    """Treat crashes, aborts and partial runs as failures even if adb exits 0."""
    injected = re.search(r"(?im)^Events injected:\s*(\d+)", output)
    count = int(injected.group(1)) if injected else 0
    if re.search(r"(?im)(?:Monkey aborted|\*\* No activities found|"
                 r"//\s*(?:CRASH|NOT RESPONDING))", output):
        return False, f"Monkey 中止或应用崩溃 · 已注入 {count}/{requested} 个事件"
    if exit_code != 0:
        return False, f"ADB 退出码 {exit_code} · 已注入 {count}/{requested} 个事件"
    if count < requested:
        return False, f"Monkey 未完成 · 已注入 {count}/{requested} 个事件"
    return True, f"Monkey 测试完成 · 已注入 {count}/{requested} 个事件"


def apk_package_name(apk: Path) -> str:
    """Read the package id before a destructive reinstall without guessing from its filename."""
    candidates = []
    found = shutil.which("aapt")
    if found is None and sys.platform == "win32":
        found = shutil.which("aapt.exe")
    if found:
        candidates.append(Path(found))
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        sdk_default = (Path(local_app_data) if local_app_data else
                       Path.home() / "AppData" / "Local") / "Android" / "Sdk"
    else:
        sdk_default = Path.home() / "Library" / "Android" / "sdk"
    sdk_roots = [os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"),
                 str(sdk_default)]
    aapt_name = "aapt.exe" if sys.platform == "win32" else "aapt"
    for root in filter(None, sdk_roots):
        candidates.extend(sorted(
            (Path(root) / "build-tools").glob(f"*/{aapt_name}"), reverse=True))
    executable = next((path for path in candidates if path.is_file()), None)
    if executable is None:
        raise AdbError(
            "不保留数据需要先识别 APK 包名。未找到 Android SDK Build-Tools 的 aapt；"
            "请安装 Build-Tools，或先在应用列表中手动卸载旧版。")
    try:
        result = subprocess.run(
            [str(executable), "dump", "badging", str(apk)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AdbError(f"无法读取 APK 包名：{exc}") from exc
    match = re.search(rb"(?m)^package: name='([^']+)'", result.stdout)
    if result.returncode != 0 or match is None:
        raise AdbError("无法从 APK 中识别包名，已取消卸载旧版。")
    return validate_package_name(match.group(1).decode("utf-8", "replace"))


def install_flags(*, allow_downgrade: bool, keep_data: bool) -> list[str]:
    """Keep the downgrade flag independent of the reinstall/data option."""
    return (["-r"] if keep_data else []) + (["-d"] if allow_downgrade else [])


def activity_component(package: str, activity: str) -> str:
    package = validate_package_name(package)
    activity = activity.strip()
    if "/" in activity:
        owner, activity = activity.split("/", 1)
        if owner != package:
            raise AdbError("Activity 的包名必须与目标应用一致。")
    if not ACTIVITY_NAME.fullmatch(activity):
        raise AdbError("请输入有效的 Activity，例如 .MainActivity。")
    return f"{package}/{activity}"


def parse_foreground_activity(output: str) -> str | None:
    component = r"([A-Za-z_][A-Za-z0-9_.]*/[A-Za-z_.$][A-Za-z0-9_.$]*)"
    for marker in ("topResumedActivity", "mResumedActivity", "ResumedActivity"):
        for line in output.splitlines():
            if re.search(rf"(?<![A-Za-z]){marker}\s*[:=]", line):
                match = re.search(component, line)
                if match:
                    return match.group(1)
    return None


def parse_app_permissions(output: str, user_id: int = 0) -> list[AppPermission]:
    requested: set[str] = set()
    installed: dict[str, bool] = {}
    runtime: dict[str, bool] = {}
    section = ""
    section_indent = 0
    current_user: int | None = None
    permission = r"([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)"
    for line in output.splitlines():
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        user_match = re.match(r"User (\d+):", stripped)
        if user_match:
            current_user = int(user_match.group(1))
            section = ""
        header = re.match(r"(requested|install|runtime) permissions:", stripped)
        if header:
            section = header.group(1)
            section_indent = indent
            continue
        if not section or not stripped:
            continue
        if indent <= section_indent:
            section = ""
            continue
        if section == "requested":
            match = re.fullmatch(permission, stripped)
            if match:
                requested.add(match.group(1))
        else:
            match = re.match(permission + r": granted=(true|false)\b", stripped)
            if not match:
                continue
            if section == "install":
                installed[match.group(1)] = match.group(2) == "true"
            elif current_user == user_id:
                runtime[match.group(1)] = match.group(2) == "true"
    names = requested | installed.keys() | runtime.keys()
    return [AppPermission(name, runtime.get(name, installed.get(name)),
                          name in runtime)
            for name in sorted(names, key=lambda name: (name not in runtime, name))]


def parse_app_processes(output: str, package: str) -> list[AppProcess]:
    lines = [line.split() for line in output.splitlines() if line.strip()]
    if not lines:
        return []
    header = [column.upper() for column in lines[0]]
    if "PID" not in header or not ({"NAME", "CMDLINE"} & set(header)):
        raise AdbError("设备返回的进程列表格式无法识别。")
    pid_index = header.index("PID")
    name_index = header.index("NAME" if "NAME" in header else "CMDLINE")
    rss_index = header.index("RSS") if "RSS" in header else None
    processes = []
    for fields in lines[1:]:
        if len(fields) <= max(pid_index, name_index):
            continue
        name = fields[name_index]
        if name != package and not name.startswith(package + ":"):
            continue
        if not fields[pid_index].isdigit():
            continue
        rss = (int(fields[rss_index]) if rss_index is not None
               and len(fields) > rss_index and fields[rss_index].isdigit() else None)
        processes.append(AppProcess(int(fields[pid_index]), name, rss))
    return processes


def parse_meminfo_total(output: str, name: str) -> int | None:
    match = re.search(rf"(?<!\S)TOTAL {re.escape(name)}:\s*([\d,]+)(?=\s|$)", output)
    return int(match.group(1).replace(",", "")) if match else None


def parse_package_list(output: str) -> list[str]:
    """Read package identifiers from `pm list packages` output."""
    return sorted({
        line.removeprefix("package:").strip()
        for line in output.splitlines()
        if line.startswith("package:") and PACKAGE_NAME.fullmatch(
            line.removeprefix("package:").strip(),
        )
    })


def parse_devices(output: str) -> list[Device]:
    devices = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] == "List" or parts[0].startswith("*"):
            continue
        details = dict(part.split(":", 1) for part in parts[2:] if ":" in part)
        devices.append(Device(parts[0], parts[1], details.get("model", "")))
    return devices


def validate_endpoint(value: str) -> str:
    """Require the address and port shown by Android wireless debugging."""
    endpoint = value.strip()
    match = re.fullmatch(r"(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+):(\d{1,5})", endpoint)
    if not match or not 1 <= int(match.group(2)) <= 65535:
        raise AdbError("请输入 IP:端口，例如 192.168.1.20:37123。")
    return endpoint


def parse_directory(output: str, directory: str) -> list[RemoteEntry]:
    entries = []
    for line in output.splitlines():
        if not line or line in (".", ".."):
            continue
        is_dir = line.endswith("/")
        name = line[:-1] if is_dir else line
        entries.append(RemoteEntry(name, posixpath.join(directory, name), is_dir))
    return sorted(entries, key=lambda item: (not item.is_dir, item.name.casefold()))


def parse_detailed_directory(output: str, directory: str) -> list[RemoteEntry] | None:
    """Parse Android toybox `ls -la`; return None for unfamiliar formats."""
    entries = []
    for line in output.splitlines():
        if not line or line.startswith("total "):
            continue
        parts = line.split(maxsplit=7)
        if (len(parts) != 8 or parts[0][:1] not in "-dlcbps"
                or not parts[4].isdigit()
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[5])):
            return None
        name = parts[7].split(" -> ", 1)[0] if parts[0].startswith("l") else parts[7]
        if name in (".", ".."):
            continue
        entries.append(RemoteEntry(
            name=name,
            path=posixpath.join(directory, name),
            is_dir=parts[0].startswith("d"),
            size=int(parts[4]),
            modified=f"{parts[5]} {parts[6]}",
        ))
    return sorted(entries, key=lambda item: (not item.is_dir, item.name.casefold()))


class AdbClient:
    def __init__(self, executable: str | None = None) -> None:
        bundled = None
        if getattr(sys, "frozen", False):
            bundle_root = getattr(sys, "_MEIPASS", None)
            if bundle_root:
                adb_name = "adb.exe" if sys.platform == "win32" else "adb"
                path = Path(bundle_root) / "platform-tools" / adb_name
                if path.is_file() and (sys.platform == "win32" or
                                       os.access(path, os.X_OK)):
                    bundled = str(path)
        candidate = executable or bundled or os.environ.get("ADB_PATH") or shutil.which("adb")
        if not candidate:
            for path in ("/opt/homebrew/bin/adb", "/usr/local/bin/adb"):
                if Path(path).is_file():
                    candidate = path
                    break
        if not candidate:
            raise AdbError("找不到 adb。请安装 Android SDK Platform-Tools，或设置 ADB_PATH。")
        self.executable = candidate

    def _execute(
        self, args: list[str], *, timeout: int | None = 300, binary: bool = False
    ) -> str | bytes:
        try:
            result = subprocess.run(
                [self.executable, *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AdbError(f"无法执行 adb：{exc}") from exc
        if result.returncode != 0:
            detail = b"\n".join(part for part in (result.stdout, result.stderr)
                                if part).decode("utf-8", "replace").strip()
            raise AdbError(detail or f"adb 退出码：{result.returncode}")
        if binary:
            return result.stdout
        return (result.stdout + result.stderr).decode("utf-8", "replace").strip()

    def list_devices(self) -> list[Device]:
        devices = parse_devices(self._execute(["devices", "-l"], timeout=15))
        named = []
        for device in devices:
            name = ""
            if device.state == "device":
                for command in (
                    ("shell", "settings", "get", "global", "device_name"),
                    ("shell", "getprop", "ro.product.marketname"),
                    ("shell", "getprop", "ro.product.model"),
                ):
                    try:
                        candidate = self.run(device.serial, *command, timeout=5).strip()
                    except AdbError:
                        continue
                    if candidate and candidate.lower() not in ("null", "unknown"):
                        name = " ".join(candidate.split())
                        break
            named.append(Device(device.serial, device.state, device.model, name))
        return named

    def pair_wifi(self, endpoint: str, code: str) -> str:
        if not re.fullmatch(r"\d{6}", code.strip()):
            raise AdbError("请输入手机上显示的 6 位配对码。")
        result = self._execute(["pair", validate_endpoint(endpoint), code.strip()], timeout=30)
        if "successfully paired" not in result.lower():
            raise AdbError(result or "Wi-Fi 配对失败。")
        return result

    def connect_wifi(self, endpoint: str) -> str:
        result = self._execute(["connect", validate_endpoint(endpoint)], timeout=20)
        if not any(phrase in result.lower() for phrase in ("connected to", "already connected")):
            raise AdbError(result or "Wi-Fi 连接失败。")
        return result

    def disconnect_wifi(self, endpoint: str) -> str:
        target = (endpoint if "._adb-tls-connect._tcp" in endpoint
                  else validate_endpoint(endpoint))
        return self._execute(["disconnect", target], timeout=15)

    def enable_tcpip(self, serial: str) -> str:
        return self.run(serial, "tcpip", "5555", timeout=20)

    def install_many(self, serials: list[str], apk: Path) -> list[InstallResult]:
        results = []
        for serial in serials:
            try:
                output = self.install_apk(serial, apk, ["-r"])
                results.append(InstallResult(serial, True, output))
            except AdbError as exc:
                results.append(InstallResult(serial, False, str(exc)))
        return results

    def install_apk(self, serial: str, apk: Path,
                    flags: list[str] | None = None) -> str:
        """Install an APK and give a useful connection hint if adb exits silently."""
        try:
            output = self.run(serial, "install", *(flags if flags is not None else ["-r"]),
                              str(apk))
        except AdbError as exc:
            if not re.fullmatch(r"adb 退出码：\d+", str(exc)):
                raise
            try:
                state = self.run(serial, "get-state", timeout=5).strip()
            except AdbError as state_exc:
                state = f"无法确认连接状态：{state_exc}"
            if state == "device":
                hint = "设备仍在线，但 adb 未返回安装错误信息。请重试；若持续失败，请检查设备上的安装确认提示。"
            else:
                hint = f"设备连接状态：{state}。请重新连接设备并刷新列表后重试。"
            raise AdbError(f"{exc}（安装命令无输出）\n目标设备：{serial}\n{hint}") from exc
        if "Success" not in output or "Failure" in output:
            raise AdbError(output or "安装未返回成功结果。")
        return output

    def run(self, serial: str, *args: str, timeout: int | None = 300) -> str:
        return self._execute(["-s", serial, *args], timeout=timeout)

    def is_package_installed(self, serial: str, package: str) -> bool:
        """Check an exact package name; `pm path` may exit 1 when absent."""
        package = validate_package_name(package)
        output = self.run(
            serial, "shell", "pm", "list", "packages", package, timeout=15)
        if any(not line.startswith("package:") for line in output.splitlines()):
            raise AdbError(f"无法确认旧版应用状态：{output}")
        return package in parse_package_list(output)

    def list_user_packages(self, serial: str) -> list[str]:
        return parse_package_list(self.run(
            serial, "shell", "pm", "list", "packages", "-3", timeout=30
        ))

    def list_system_packages(self, serial: str) -> list[str]:
        return parse_package_list(self.run(
            serial, "shell", "pm", "list", "packages", "-s", timeout=30
        ))

    def device_info(self, serial: str) -> DeviceInfo:
        """Collect device facts without requiring root access."""
        properties = self.run(serial, "shell", "getprop", timeout=15)

        def optional(*args: str, timeout: int = 15) -> str:
            try:
                return self.run(serial, "shell", *args, timeout=timeout)
            except AdbError:
                return ""

        return parse_device_info(
            properties,
            optional("cat", "/proc/cpuinfo"),
            optional("dumpsys", "SurfaceFlinger", timeout=20),
            optional("cat", "/proc/meminfo"),
            optional("df", "-k", "/data"),
            optional("df", "-k", "/system"),
            optional("cat /sys/devices/system/cpu/cpu*/cpufreq/cpuinfo_max_freq"),
        )

    def start_app(self, serial: str, package: str, activity: str = "") -> str:
        package = validate_package_name(package)
        if activity.strip():
            component = activity_component(package, activity)
            command = f"am start -W -n {shlex.quote(component)}"
        else:
            command = ("am start -W -a android.intent.action.MAIN "
                       "-c android.intent.category.LAUNCHER "
                       f"-p {package}")
        output = self.run(serial, "shell", command, timeout=30)
        error = re.search(
            r"(?im)^[ \t]*(?:Error(?:\s+type\s+\d+|:)|Exception\b|"
            r"Permission Denial:)", output,
        )
        status = re.search(r"(?im)^[ \t]*Status:[ \t]*(\S+)", output)
        if error or (status and status.group(1).lower() != "ok"):
            raise AdbError(output)
        return output or f"已启动 {package}"

    def stop_app(self, serial: str, package: str) -> str:
        package = validate_package_name(package)
        output = self.run(serial, "shell", "am", "force-stop", package, timeout=15)
        if output.strip() not in ("", "Success"):
            raise AdbError(output)
        return f"已停止 {package}"

    def clear_app(self, serial: str, package: str, *, cache_only: bool) -> str:
        package = validate_package_name(package)
        command = ["shell", "pm", "clear"]
        if cache_only:
            command.append("--cache-only")
        output = self.run(serial, *command, package, timeout=60)
        if "Success" not in {line.strip() for line in output.splitlines()}:
            raise AdbError(output or "设备未确认清理成功。")
        return f"已清除 {package} 的{'缓存' if cache_only else '应用数据'}"

    def foreground_activity(self, serial: str) -> str:
        output = self.run(serial, "shell", "dumpsys", "activity", "activities",
                          timeout=30)
        return parse_foreground_activity(output) or "未找到前台 Activity"

    def app_permissions(self, serial: str, package: str) -> list[AppPermission]:
        package = validate_package_name(package)
        output = self.run(serial, "shell", "dumpsys", "package", package,
                          timeout=90)
        permissions = parse_app_permissions(output)
        if not permissions:
            raise AdbError("未读到应用权限，请确认包名和设备状态。")
        return permissions

    def check_app_permission(self, serial: str, package: str,
                             permission: str) -> bool:
        package = validate_package_name(package)
        permission = validate_package_name(permission)
        output = self.run(
            serial, "shell", "dumpsys", "package", "check-permission",
            permission, package, "0", timeout=15,
        ).strip()
        if output not in ("0", "-1"):
            raise AdbError(f"设备未返回有效的权限状态：{output or '空结果'}")
        return output == "0"

    def app_permission_states(self, serial: str, package: str,
                              permissions: list[str]) -> dict[str, bool]:
        package = validate_package_name(package)
        names = list(dict.fromkeys(validate_package_name(name)
                                   for name in permissions))
        if not names:
            return {}
        command = (
            "for permission in " + " ".join(shlex.quote(name) for name in names)
            + "; do printf '%s ' \"$permission\"; "
            + "dumpsys package check-permission \"$permission\" "
            + shlex.quote(package) + " 0; done"
        )
        output = self.run(serial, "shell", command,
                          timeout=max(15, len(names) * 2))
        states = {}
        for line in output.splitlines():
            parts = line.split()
            if len(parts) != 2 or parts[0] not in names or parts[1] not in ("0", "-1"):
                raise AdbError(f"设备未返回有效的权限状态：{line}")
            states[parts[0]] = parts[1] == "0"
        if set(states) != set(names):
            raise AdbError("设备返回的权限状态不完整。")
        return states

    def set_app_permission(self, serial: str, package: str, permission: str,
                           *, grant: bool) -> str:
        package = validate_package_name(package)
        permission = validate_package_name(permission)
        command = "grant" if grant else "revoke"
        output = self.run(serial, "shell", "pm", command, package, permission,
                          timeout=30)
        if output.strip() not in ("", "Success"):
            raise AdbError(output)
        return f"已{'授予' if grant else '撤销'} {permission}"

    def app_process_report(self, serial: str, package: str) -> AppProcessReport:
        package = validate_package_name(package)
        try:
            listing = self.run(
                serial, "shell", "ps", "-A", "-o", "PID,RSS,NAME", timeout=20)
            processes = parse_app_processes(listing, package)
        except AdbError:
            listing = self.run(serial, "shell", "ps", "-A", timeout=20)
            processes = parse_app_processes(listing, package)
        meminfo = self.run(serial, "shell", "dumpsys", "meminfo", package,
                           timeout=45)
        return AppProcessReport(processes, parse_meminfo_total(meminfo, "PSS"),
                                parse_meminfo_total(meminfo, "RSS"), meminfo)

    def performance_uid(self, serial: str, package: str) -> int:
        package = validate_package_name(package)
        output = self.run(serial, "shell", "dumpsys", "package", package,
                          timeout=20)
        match = re.search(r"(?m)^\s*userId=(\d+)\b", output)
        if not match:
            raise AdbError("无法读取应用 UID，应用网络累计流量不可用。")
        return int(match.group(1))

    def performance_network_usage(self, serial: str,
                                  uid: int) -> tuple[int, int] | None:
        output = self.run(serial, "shell", "dumpsys", "netstats", "--uid",
                          timeout=45)
        return parse_uid_network(output, uid)

    def performance_sample(self, serial: str,
                           package: str) -> PerformanceSample:
        package = validate_package_name(package)
        issues = []
        processes = []
        try:
            try:
                output = self.run(serial, "shell", "ps", "-A", "-o", "PID,NAME",
                                  timeout=15)
            except AdbError:
                output = self.run(serial, "shell", "ps", "-A", timeout=15)
            processes = parse_app_processes(output, package)
        except AdbError as exc:
            issues.append(f"进程：{exc}")

        cpu_ticks: dict[int, int] = {}
        system_ticks = None
        cpu_cores = 0
        if processes:
            paths = [f"/proc/{process.pid}/stat" for process in processes]
            try:
                output = self.run(serial, "shell", "cat", *paths, "/proc/stat",
                                  timeout=15)
                cpu_ticks, system_ticks, cpu_cores = parse_proc_stats(output)
            except AdbError as exc:
                issues.append(f"CPU：{exc}")

        pss_kb = rss_kb = None
        if processes:
            try:
                output = self.run(serial, "shell", "dumpsys", "meminfo", package,
                                  timeout=20)
                pss_kb = parse_meminfo_total(output, "PSS")
                rss_kb = parse_meminfo_total(output, "RSS")
            except AdbError as exc:
                issues.append(f"内存：{exc}")

        rx_bytes = tx_bytes = None
        try:
            output = self.run(serial, "shell", "cat", "/proc/net/dev", timeout=10)
            counters = parse_device_network(output)
            if counters is not None:
                rx_bytes, tx_bytes = counters
        except AdbError as exc:
            issues.append(f"网络：{exc}")

        layer = None
        frame_times: tuple[int, ...] = ()
        source = ""
        if processes:
            try:
                output = self.run(serial, "shell", "dumpsys", "SurfaceFlinger",
                                  "--list", timeout=15)
                best_layer = None
                for candidate in parse_surface_layers(output, package)[:8]:
                    latency = self.run(
                        serial, "shell",
                        f"dumpsys SurfaceFlinger --latency {shlex.quote(candidate)}",
                        timeout=15)
                    candidate_times = parse_surface_latency(latency)
                    if candidate_times and (not frame_times or
                                            candidate_times[-1] > frame_times[-1]):
                        best_layer, frame_times = candidate, candidate_times
                if best_layer:
                    layer = best_layer
                    source = "SurfaceView" if "SurfaceView" in best_layer else "窗口图层"
            except AdbError as exc:
                issues.append(f"SurfaceFlinger：{exc}")
            if not frame_times:
                try:
                    output = self.run(serial, "shell", "dumpsys", "gfxinfo",
                                      package, "framestats", timeout=20)
                    frame_times = parse_gfxinfo_frames(output)
                    if frame_times:
                        source = "gfxinfo"
                except AdbError as exc:
                    issues.append(f"gfxinfo：{exc}")

        return PerformanceSample(
            time.monotonic(), len(processes), cpu_ticks, system_ticks,
            cpu_cores, pss_kb, rss_kb, rx_bytes, tx_bytes,
            layer, frame_times, source, tuple(issues))

    def list_directory(self, serial: str, directory: str) -> list[RemoteEntry]:
        # adb shell accepts a shell command string; quote the entire remote path.
        listing_path = directory.rstrip("/") + "/"
        command = f"ls -la -- {shlex.quote(listing_path)}"
        output = self.run(serial, "shell", command, timeout=30)
        detailed = parse_detailed_directory(output, directory)
        if detailed is not None:
            return detailed
        fallback = f"ls -1pA -- {shlex.quote(listing_path)}"
        return parse_directory(self.run(serial, "shell", fallback, timeout=30), directory)

    def remove_remote_entry(self, serial: str, directory: str,
                            entry: RemoteEntry) -> None:
        """Remove one listed child of a device directory, including its contents."""
        parent = posixpath.normpath(directory)
        path = posixpath.normpath(entry.path)
        if (not parent.startswith("/") or parent == "/"
                or not entry.name or entry.name in (".", "..")
                or "/" in entry.name or "\x00" in entry.name
                or any(part == ".." for part in entry.path.split("/"))
                or path != posixpath.join(parent, entry.name)):
            raise AdbError("只能删除当前设备目录中选中的文件或文件夹；不允许删除设备根目录内容。")
        command = f"rm {'-r ' if entry.is_dir else ''}-- {shlex.quote(path)}"
        self.run(serial, "shell", command, timeout=None)

    def screenshot(self, serial: str, destination: Path) -> None:
        image = self._execute(
            ["-s", serial, "exec-out", "screencap", "-p"], timeout=30, binary=True
        )
        if not image.startswith(b"\x89PNG\r\n\x1a\n"):
            raise AdbError("设备没有返回有效的 PNG 截图。")
        destination.write_bytes(image)

    def clear_logcat(self, serial: str, buffer: str) -> None:
        buffers = ("main", "system", "events", "crash")
        if buffer not in (*buffers, "all"):
            raise AdbError("未知的 logcat 缓冲区。")
        names = buffers if buffer == "all" else (buffer,)
        arguments = [arg for name in names for arg in ("-b", name)]
        self.run(serial, "logcat", *arguments, "-c", timeout=20)

    def clear_hilog(self, serial: str) -> None:
        self.run(serial, "shell", "hilog", "-r", timeout=20)

    def pull_crash_reports(self, serial: str, destination: Path) -> CrashPullResult:
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        safe_serial = re.sub(r"[^A-Za-z0-9._-]", "_", serial)
        folder = destination / f"崩溃日志_{safe_serial}_{datetime.now():%Y%m%d_%H%M%S_%f}"
        folder.mkdir()
        folders = {"anr": folder / "anr", "tombstones": folder / "tombstones"}
        counts = {}
        for name, local in folders.items():
            try:
                self.run(serial, "pull", f"/data/{name}", str(folder), timeout=120)
            except AdbError:
                pass  # Production devices often deny direct access to /data.
            counts[name] = sum(path.is_file() for path in local.rglob("*")) \
                if local.exists() else 0

        missing = {name for name, count in counts.items() if count == 0}
        saved_bugreport = None
        saved_report_text = None
        if missing:
            with tempfile.TemporaryDirectory(prefix="sandevistan-bugreport-") as temp:
                temp_dir = Path(temp)
                try:
                    self._execute(["-s", serial, "bugreport", str(temp_dir)],
                                  timeout=600)
                except AdbError as exc:
                    if not any(counts.values()):
                        raise AdbError(
                            "无法直接读取 ANR/tombstone，生成 bugreport 也失败："
                            f"{exc}") from exc
                    return CrashPullResult(folder, counts["anr"],
                                           counts["tombstones"])
                archives = sorted(temp_dir.glob("*.zip"))
                if not archives:
                    raise AdbError("设备未生成 ZIP 格式的 bugreport。")
                archive = archives[-1]
                try:
                    with zipfile.ZipFile(archive) as report:
                        for member in report.infolist():
                            for name in missing:
                                prefix = f"FS/data/{name}/"
                                if member.is_dir() or not member.filename.startswith(prefix):
                                    continue
                                basename = Path(member.filename).name
                                if not basename:
                                    continue
                                target_dir = folders[name]
                                target_dir.mkdir(exist_ok=True)
                                target = target_dir / basename
                                if not target.exists():
                                    with report.open(member) as source, target.open("wb") as output:
                                        shutil.copyfileobj(source, output)
                                counts[name] += 1
                except (OSError, zipfile.BadZipFile) as exc:
                    raise AdbError(f"无法解压 bugreport：{exc}") from exc
                if any(counts[name] == 0 for name in missing):
                    saved_bugreport = folder / archive.name
                    shutil.copy2(archive, saved_bugreport)
                    with zipfile.ZipFile(archive) as report:
                        reports = [member for member in report.infolist()
                                   if not member.is_dir()
                                   and "/" not in member.filename
                                   and member.filename.endswith(".txt")]
                        if reports:
                            report_text = max(reports, key=lambda item: item.file_size)
                            saved_report_text = folder / report_text.filename
                            with report.open(report_text) as source, \
                                    saved_report_text.open("wb") as output:
                                shutil.copyfileobj(source, output)
        return CrashPullResult(folder, counts["anr"],
                               counts["tombstones"], saved_bugreport,
                               saved_report_text)
