"""Parse Android performance counters without depending on Qt or adb."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PerformanceSample:
    sampled_at: float
    process_count: int
    cpu_ticks: dict[int, int]
    system_ticks: int | None
    cpu_cores: int
    pss_kb: int | None
    rss_kb: int | None
    device_rx_bytes: int | None
    device_tx_bytes: int | None
    layer: str | None
    frame_times_ns: tuple[int, ...]
    fps_source: str
    issues: tuple[str, ...] = ()


def parse_proc_stats(output: str) -> tuple[dict[int, int], int | None, int]:
    """Return process CPU ticks, total device ticks, and logical CPU count."""
    processes: dict[int, int] = {}
    total = None
    cores = 0
    for line in output.splitlines():
        if line.startswith("cpu "):
            values = line.split()[1:9]
            if len(values) == 8 and all(value.isdigit() for value in values):
                total = sum(map(int, values))
        elif re.match(r"cpu\d+\s", line):
            cores += 1
        elif re.match(r"\d+ \(", line):
            closing = line.rfind(")")
            if closing < 0:
                continue
            fields = line[closing + 1:].split()
            if len(fields) > 12 and fields[11].isdigit() and fields[12].isdigit():
                processes[int(line.split(" ", 1)[0])] = (
                    int(fields[11]) + int(fields[12]))
    return processes, total, cores


def cpu_percent(previous: PerformanceSample | None,
                current: PerformanceSample) -> float | None:
    if previous is None or previous.system_ticks is None \
            or current.system_ticks is None or not current.cpu_cores:
        return None
    total_delta = current.system_ticks - previous.system_ticks
    if total_delta <= 0:
        return None
    common = current.cpu_ticks.keys() & previous.cpu_ticks.keys()
    if not common:
        return None
    used = sum(max(0, current.cpu_ticks[pid] - previous.cpu_ticks[pid])
               for pid in common)
    return max(0.0, used / total_delta * current.cpu_cores * 100)


def parse_device_network(output: str) -> tuple[int, int] | None:
    """Sum physical network interfaces; exclude loopback and VPN duplication."""
    physical = []
    other = []
    for line in output.splitlines():
        if ":" not in line:
            continue
        name, counters = line.split(":", 1)
        name = name.strip()
        numbers = counters.split()
        if len(numbers) < 16 or not numbers[0].isdigit() or not numbers[8].isdigit():
            continue
        if name == "lo":
            continue
        pair = (int(numbers[0]), int(numbers[8]))
        if re.match(r"^(?:wlan|rmnet|eth|ccmni|pdp|wwan|usb)\w*\d*$", name):
            physical.append(pair)
        elif not name.startswith(("dummy", "tun", "veth", "bridge", "br-")):
            other.append(pair)
    chosen = physical or other
    if not chosen:
        return None
    return sum(pair[0] for pair in chosen), sum(pair[1] for pair in chosen)


def parse_uid_network(output: str, uid: int) -> tuple[int, int] | None:
    """Sum untagged UID history buckets from `dumpsys netstats --uid`."""
    in_uid_stats = False
    wanted = False
    found = False
    rx = tx = 0
    for line in output.splitlines():
        if line == "UID stats:":
            in_uid_stats = True
            continue
        if in_uid_stats and line and not line[0].isspace():
            break
        if not in_uid_stats:
            continue
        if line.lstrip().startswith("ident="):
            wanted = (re.search(rf"\buid={uid}\b", line) is not None
                      and "tag=0x0" in line)
        elif wanted:
            match = re.search(r"\brb=(\d+)\b.*?\btb=(\d+)\b", line)
            if match:
                rx += int(match.group(1))
                tx += int(match.group(2))
                found = True
    return (rx, tx) if found else None


def parse_surface_layers(output: str, package: str) -> list[str]:
    pattern = re.compile(re.escape(package) + r"(?=$|/|\]|#|\(|\s)")
    layers = []
    for line in output.splitlines():
        name = line.strip()
        if name.startswith("RequestedLayerState{") and name.endswith("}"):
            # Recent Android versions wrap names with state metadata. The
            # layer ID is part of the name accepted by --latency; parentId is not.
            name = re.sub(r"\s+parentId=\d+.*$", "", name[20:-1])
        if (pattern.search(name) and "ActivityRecord" not in name
                and "InputSink" not in name):
            layers.append(name)
    return sorted(set(layers), key=lambda name: (
        "SurfaceView" not in name, "(BLAST)" not in name, name))


def parse_surface_latency(output: str) -> tuple[int, ...]:
    times = set()
    for line in output.splitlines()[1:]:
        fields = line.split()
        if len(fields) != 3 or not all(field.isdigit() for field in fields):
            continue
        presented = int(fields[1])
        if 0 < presented < (1 << 63) - 1:
            times.add(presented)
    return tuple(sorted(times))


def parse_gfxinfo_frames(output: str) -> tuple[int, ...]:
    in_frames = False
    index = None
    times = set()
    for line in output.splitlines():
        if line.startswith("---PROFILEDATA---"):
            in_frames = not in_frames
            index = None
            continue
        if not in_frames:
            continue
        fields = line.rstrip(",").split(",")
        if fields and fields[0] == "Flags":
            index = fields.index("FrameCompleted") if "FrameCompleted" in fields else None
        elif (index is not None and len(fields) > index
              and fields[0] == "0" and fields[index].isdigit()):
            stamp = int(fields[index])
            if stamp > 0:
                times.add(stamp)
    return tuple(sorted(times))


def fps_from_frames(previous_latest: int | None,
                    current: tuple[int, ...]) -> float | None:
    if previous_latest is None or not current:
        return None
    newer = [stamp for stamp in current if stamp > previous_latest]
    if not newer:
        return 0.0
    elapsed = (newer[-1] - previous_latest) / 1_000_000_000
    return min(240.0, len(newer) / elapsed) if elapsed > 0 else None
