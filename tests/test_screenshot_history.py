import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from screenshot_history import ScreenshotHistory, reveal_in_finder


class ScreenshotHistoryTests(unittest.TestCase):
    def test_records_survive_restart_and_same_path_is_updated(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Path(folder) / "app" / "screenshot_history.json"
            first = Path(folder) / "第一张.png"
            second = Path(folder) / "second.png"
            history = ScreenshotHistory(storage)
            history.add(first, "平板")
            history.add(second, "手机")
            history.add(first, "新设备")

            reloaded = ScreenshotHistory(storage)
            self.assertEqual(
                [(item.path, item.device_name) for item in reloaded.entries],
                [(first.resolve(), "新设备"), (second.resolve(), "手机")],
            )
            self.assertTrue(reloaded.entries[0].display_time)

    def test_missing_or_invalid_history_starts_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Path(folder) / "history.json"
            self.assertEqual(ScreenshotHistory(storage).entries, [])
            storage.write_text("broken JSON", encoding="utf-8")
            self.assertEqual(ScreenshotHistory(storage).entries, [])

    def test_reveal_uses_finder_and_rejects_missing_file(self):
        with tempfile.TemporaryDirectory() as folder:
            screenshot = Path(folder) / "截图 1.png"
            screenshot.write_bytes(b"png")
            with patch("screenshot_history.subprocess.run") as run:
                run.return_value.returncode = 0
                self.assertEqual(reveal_in_finder(screenshot), str(screenshot))
            self.assertEqual(run.call_args.args[0],
                             ["open", "-R", str(screenshot)])
            screenshot.unlink()
            with self.assertRaises(FileNotFoundError):
                reveal_in_finder(screenshot)


if __name__ == "__main__":
    unittest.main()
