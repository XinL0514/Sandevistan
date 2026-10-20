import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from adb_client import AdbClient, AdbError
from logcat_tools import logcat_matches, parse_logcat_line


class LogcatTests(unittest.TestCase):
    def test_threadtime_levels_and_filters(self):
        error = parse_logcat_line(
            "10-20 12:30:14.123  1234  5678 E Example: failed")
        warning = parse_logcat_line(
            "2026-10-20 12:30:15.123  1234  5678 W Example: retry")
        self.assertEqual((error.pid, error.level), (1234, "E"))
        self.assertEqual(warning.level, "W")
        self.assertTrue(logcat_matches(
            error, package="com.example.app", package_pids={1234},
            minimum_level="W", keyword="FAILED"))
        self.assertFalse(logcat_matches(
            warning, minimum_level="E"))
        self.assertFalse(logcat_matches(
            error, package="com.other.app", package_pids={9999}))
        self.assertTrue(logcat_matches(
            parse_logcat_line("--------- beginning of main"),
            minimum_level="V"))
        hilog_error = parse_logcat_line(
            "10-20 13:25:04.067 14412       14412 E 02D03/HiTraceC: failed")
        self.assertEqual((hilog_error.pid, hilog_error.level), (14412, "E"))
        self.assertTrue(logcat_matches(
            hilog_error, package="com.msb.elemotion", package_pids={14412},
            minimum_level="E"))

    def test_clear_selected_buffer(self):
        client = AdbClient("/usr/bin/true")
        with patch.object(client, "run", return_value="") as run:
            client.clear_logcat("ABC", "crash")
        run.assert_called_once_with(
            "ABC", "logcat", "-b", "crash", "-c", timeout=20)
        with patch.object(client, "run", return_value="") as run:
            client.clear_logcat("ABC", "all")
        run.assert_called_once_with(
            "ABC", "logcat", "-b", "main", "-b", "system",
            "-b", "events", "-b", "crash", "-c", timeout=20)
        with self.assertRaises(AdbError):
            client.clear_logcat("ABC", "invalid")
        with patch.object(client, "run", return_value="") as run:
            client.clear_hilog("ABC")
        run.assert_called_once_with(
            "ABC", "shell", "hilog", "-r", timeout=20)

    def test_crash_pull_falls_back_to_bugreport_zip(self):
        client = AdbClient("/usr/bin/true")

        def make_bugreport(args, **_kwargs):
            archive = Path(args[-1]) / "bugreport.zip"
            with zipfile.ZipFile(archive, "w") as report:
                report.writestr("FS/data/anr/anr_2026-10-20", "ANR")
                report.writestr("FS/data/tombstones/tombstone_01", "CRASH")
            return ""

        with tempfile.TemporaryDirectory() as temp:
            with patch.object(client, "run", side_effect=AdbError("Permission denied")):
                with patch.object(client, "_execute", side_effect=make_bugreport):
                    result = client.pull_crash_reports("ABC", Path(temp))
            self.assertEqual((result.anr_count, result.tombstone_count), (1, 1))
            self.assertEqual(
                (result.folder / "anr" / "anr_2026-10-20").read_text(), "ANR")
            self.assertEqual(
                (result.folder / "tombstones" / "tombstone_01").read_text(),
                "CRASH")
            self.assertIsNone(result.bugreport)

    def test_crash_pull_uses_direct_files_when_available(self):
        client = AdbClient("/usr/bin/true")

        def pull(_serial, _command, remote, folder, **_kwargs):
            name = remote.rsplit("/", 1)[-1]
            local = Path(folder) / name
            local.mkdir()
            (local / ("anr_01" if name == "anr" else "tombstone_01")).write_text(
                "sample")
            return ""

        with tempfile.TemporaryDirectory() as temp:
            with patch.object(client, "run", side_effect=pull) as run:
                with patch.object(client, "_execute") as execute:
                    result = client.pull_crash_reports("ABC", Path(temp))
            self.assertEqual((result.anr_count, result.tombstone_count), (1, 1))
            self.assertEqual(run.call_count, 2)
            execute.assert_not_called()

    def test_empty_bugreport_is_kept_for_manual_inspection(self):
        client = AdbClient("/usr/bin/true")

        def make_bugreport(args, **_kwargs):
            with zipfile.ZipFile(Path(args[-1]) / "bugreport.zip", "w") as report:
                report.writestr("bugreport.txt", "No crash attachments")
            return ""

        with tempfile.TemporaryDirectory() as temp:
            with patch.object(client, "run", side_effect=AdbError("Permission denied")):
                with patch.object(client, "_execute", side_effect=make_bugreport):
                    result = client.pull_crash_reports("ABC", Path(temp))
            self.assertEqual((result.anr_count, result.tombstone_count), (0, 0))
            self.assertTrue(result.bugreport.is_file())
            self.assertEqual(result.bugreport_text.read_text(),
                             "No crash attachments")


if __name__ == "__main__":
    unittest.main()
