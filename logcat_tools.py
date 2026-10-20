"""Parsing and filtering for Android logcat's threadtime output."""

from __future__ import annotations

import re
from dataclasses import dataclass


LOGCAT_BUFFERS = ("main", "system", "events", "crash")
LOG_LEVELS = {"V": 0, "D": 1, "I": 2, "W": 3, "E": 4, "F": 5, "A": 5}
THREADTIME = re.compile(
    r"^(?:\d{2}-\d{2}|\d{4}-\d{2}-\d{2})\s+"
    r"\d{2}:\d{2}:\d{2}\.\d+\s+"
    r"(?P<pid>\d+)\s+\d+\s+(?P<level>[VDIWEFA])\s+[^:]+:"
)


@dataclass(frozen=True)
class LogcatEntry:
    raw: str
    pid: int | None
    level: str | None


def parse_logcat_line(raw: str) -> LogcatEntry:
    match = THREADTIME.match(raw)
    if match is None:
        return LogcatEntry(raw, None, None)
    return LogcatEntry(raw, int(match.group("pid")), match.group("level"))


def logcat_matches(entry: LogcatEntry, *, package: str = "",
                   package_pids: set[int] | None = None,
                   minimum_level: str = "V", keyword: str = "") -> bool:
    if package and not (
        package.lower() in entry.raw.lower()
        or entry.pid in (package_pids or set())
    ):
        return False
    if entry.level is None:
        if minimum_level != "V":
            return False
    elif LOG_LEVELS[entry.level] < LOG_LEVELS[minimum_level]:
        return False
    return not keyword or keyword.lower() in entry.raw.lower()
