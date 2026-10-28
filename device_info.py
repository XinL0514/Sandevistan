"""Parse hardware and capacity details available to an unprivileged ADB shell."""

from __future__ import annotations

import re
from dataclasses import dataclass


UNKNOWN = "设备未提供"


@dataclass(frozen=True)
class DeviceInfo:
    device: dict[str, str]
    cpu: dict[str, str]
    gpu: dict[str, str]
    storage: dict[str, str]
    ram: dict[str, str]


def _value(value: str | None) -> str:
    value = (value or "").strip()
    return value if value and value.lower() not in {"unknown", "null", "n/a"} else UNKNOWN


def _properties(output: str) -> dict[str, str]:
    return dict(re.findall(r"(?m)^\[([^]]+)\]: \[([^]]*)\]$", output))


def _cpu_fields(output: str) -> tuple[dict[str, str], int | None]:
    fields: dict[str, str] = {}
    cores = 0
    for line in output.splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            continue
        key, value = key.strip(), value.strip()
        if key == "processor" and value.isdigit():
            cores += 1
        elif key and value:
            fields.setdefault(key, value)
    return fields, cores or None


def _memory_fields(output: str) -> dict[str, int]:
    return {name: int(value) for name, value in re.findall(
        r"(?m)^([A-Za-z_]+):\s+(\d+)\s+kB\s*$", output
    )}


def _size(kib: int | None) -> str:
    if kib is None:
        return UNKNOWN
    if kib >= 1024 * 1024:
        return f"{kib / (1024 * 1024):.1f} GiB"
    if kib >= 1024:
        return f"{kib / 1024:.1f} MiB"
    return f"{kib} KiB"


def _filesystem(output: str) -> dict[str, str]:
    for line in output.splitlines():
        parts = line.split()
        if (len(parts) >= 6 and all(part.isdigit() for part in parts[1:4])
                and re.fullmatch(r"\d+%", parts[4])):
            return {
                "total": _size(int(parts[1])),
                "used": _size(int(parts[2])),
                "available": _size(int(parts[3])),
                "usage": parts[4],
                "filesystem": parts[0],
            }
    return {}


def _gpu_fields(output: str) -> tuple[str, str, str]:
    for line in output.splitlines():
        if "GLES:" in line:
            parts = [part.strip() for part in line.split("GLES:", 1)[1].split(",", 2)]
            if len(parts) == 3:
                return tuple(parts)
        match = re.search(r"\bGL_RENDERER\s*[:=]\s*(.+)", line)
        if match:
            return "", match.group(1).strip(), ""
    return "", "", ""


def _max_frequency(output: str) -> str:
    frequencies = [int(line) for line in output.splitlines() if line.strip().isdigit()]
    return f"{max(frequencies) / 1_000_000:.2f} GHz" if frequencies else UNKNOWN


def _gles_version(value: str | None) -> str:
    if value and value.isdigit():
        encoded = int(value)
        major, minor = encoded >> 16, encoded & 0xffff
        if 1 <= major <= 9:
            return f"OpenGL ES {major}.{minor}"
    return UNKNOWN


def parse_device_info(properties: str, cpuinfo: str, gpuinfo: str,
                      meminfo: str, data_df: str, system_df: str,
                      frequencies: str = "") -> DeviceInfo:
    props = _properties(properties)
    cpu_fields, cores = _cpu_fields(cpuinfo)
    memory = _memory_fields(meminfo)
    data = _filesystem(data_df)
    system = _filesystem(system_df)
    gpu_vendor, gpu_renderer, gpu_version = _gpu_fields(gpuinfo)

    def first(*values: str | None) -> str:
        return next((value for item in values
                     if (value := _value(item)) != UNKNOWN), UNKNOWN)

    def prop(*names: str) -> str:
        return first(*(props.get(name) for name in names))

    return DeviceInfo(
        device={
            "型号": prop("ro.product.marketname", "ro.product.model"),
            "制造商": prop("ro.product.manufacturer"),
            "Android 版本": prop("ro.build.version.release"),
        },
        cpu={
            "SoC 型号": first(props.get("ro.soc.model"), cpu_fields.get("Hardware"),
                              cpu_fields.get("model name"), cpu_fields.get("Processor")),
            "SoC 厂商": prop("ro.soc.manufacturer"),
            "逻辑核心": str(cores) if cores is not None else UNKNOWN,
            "最高标称频率": _max_frequency(frequencies),
            "平台": prop("ro.board.platform", "ro.hardware"),
            "CPU ABI": prop("ro.product.cpu.abilist", "ro.product.cpu.abi"),
        },
        gpu={
            "GPU / 渲染器": _value(gpu_renderer),
            "厂商": _value(gpu_vendor),
            "图形 API": first(gpu_version, _gles_version(props.get("ro.opengles.version"))),
            "EGL 驱动": prop("ro.hardware.egl"),
        },
        storage={
            "内部存储总量（/data）": data.get("total", UNKNOWN),
            "已使用": data.get("used", UNKNOWN),
            "可用": data.get("available", UNKNOWN),
            "使用率": data.get("usage", UNKNOWN),
            "文件系统": data.get("filesystem", UNKNOWN),
            "系统分区总量（/system）": system.get("total", UNKNOWN),
            "系统分区可用": system.get("available", UNKNOWN),
        },
        ram={
            "物理内存总量": _size(memory.get("MemTotal")),
            "当前可用": _size(memory.get("MemAvailable")),
            "空闲": _size(memory.get("MemFree")),
            "缓存": _size(memory.get("Cached")),
            "Swap 总量": _size(memory.get("SwapTotal")),
            "Swap 可用": _size(memory.get("SwapFree")),
        },
    )
