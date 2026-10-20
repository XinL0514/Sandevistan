import unittest
from unittest.mock import patch

from adb_client import (
    AdbClient, AdbError, activity_component, parse_app_permissions,
    parse_app_processes, parse_foreground_activity, parse_meminfo_total,
)


class AppToolsTests(unittest.TestCase):
    def setUp(self):
        self.client = AdbClient("/usr/bin/true")

    def test_activity_component_rejects_other_packages_and_shell_text(self):
        self.assertEqual(activity_component("com.example.app", ".MainActivity"),
                         "com.example.app/.MainActivity")
        self.assertEqual(activity_component(
            "com.example.app", "com.example.app/com.example.app.MainActivity"),
            "com.example.app/com.example.app.MainActivity")
        for activity in ("com.other.app/.Main", ".Main; reboot", ""):
            with self.assertRaises(AdbError):
                activity_component("com.example.app", activity)

    def test_start_stop_and_clear_commands(self):
        with patch.object(self.client, "run", return_value="Status: ok") as run:
            self.client.start_app("ABC", "com.example.app", ".MainActivity")
        self.assertEqual(run.call_args.args,
                         ("ABC", "shell", "am start -W -n com.example.app/.MainActivity"))
        with patch.object(self.client, "run", return_value="Status: ok") as run:
            self.client.start_app("ABC", "com.example.app")
        self.assertIn("android.intent.category.LAUNCHER", run.call_args.args[2])
        for output in (
            "Error type 3\nError: Activity class does not exist.",
            "Status: timeout\nActivity: com.example.app/.MainActivity",
        ):
            with patch.object(self.client, "run", return_value=output):
                with self.assertRaises(AdbError):
                    self.client.start_app("ABC", "com.example.app")
        with patch.object(self.client, "run", return_value="") as run:
            self.client.stop_app("ABC", "com.example.app")
        run.assert_called_once_with("ABC", "shell", "am", "force-stop",
                                    "com.example.app", timeout=15)
        with patch.object(self.client, "run", return_value="Success") as run:
            self.client.clear_app("ABC", "com.example.app", cache_only=True)
        run.assert_called_once_with("ABC", "shell", "pm", "clear",
                                    "--cache-only", "com.example.app", timeout=60)
        with patch.object(self.client, "run", return_value="Failed"):
            with self.assertRaises(AdbError):
                self.client.clear_app("ABC", "com.example.app", cache_only=False)

    def test_foreground_activity_formats(self):
        self.assertEqual(parse_foreground_activity(
            "topResumedActivity=ActivityRecord{123 u0 com.example.app/.Main t1}"),
            "com.example.app/.Main")
        self.assertEqual(parse_foreground_activity(
            "mResumedActivity: ActivityRecord{456 u0 com.other.app/com.other.Home t2}"),
            "com.other.app/com.other.Home")
        self.assertEqual(parse_foreground_activity(
            "  ResumedActivity: ActivityRecord{789 u0 com.example.app/.Page t3}"),
            "com.example.app/.Page")
        self.assertIsNone(parse_foreground_activity(
            "mLastResumedActivity=ActivityRecord{789 u0 com.example.app/.Old t3}"))
        self.assertIsNone(parse_foreground_activity("no activity"))

    def test_permissions_include_runtime_and_read_only_entries(self):
        output = """Package [com.example.app]:
    requested permissions:
      android.permission.CAMERA
      android.permission.INTERNET
      android.permission.POST_NOTIFICATIONS
    install permissions:
      android.permission.INTERNET: granted=true
    User 0: installed=true
      runtime permissions:
        android.permission.CAMERA: granted=false, flags=[ USER_SET]
        android.permission.POST_NOTIFICATIONS: granted=true
    User 10: installed=true
      runtime permissions:
        android.permission.CAMERA: granted=true
"""
        permissions = {item.name: item for item in parse_app_permissions(output)}
        self.assertEqual((permissions["android.permission.CAMERA"].granted,
                          permissions["android.permission.CAMERA"].runtime),
                         (False, True))
        self.assertEqual((permissions["android.permission.INTERNET"].granted,
                          permissions["android.permission.INTERNET"].runtime),
                         (True, False))
        self.assertTrue(permissions["android.permission.POST_NOTIFICATIONS"].granted)

    def test_process_and_memory_parsing(self):
        output = """USER PID PPID VSZ RSS WCHAN ADDR S NAME
u0_a1 123 1 100 2048 0 0 S com.example.app
u0_a1 124 1 100 1024 0 0 S com.example.app:remote
u0_a2 125 1 100 9999 0 0 S com.example.application
"""
        processes = parse_app_processes(output, "com.example.app")
        self.assertEqual([(item.pid, item.name, item.rss_kb)
                          for item in processes], [
                              (123, "com.example.app", 2048),
                              (124, "com.example.app:remote", 1024),
                          ])
        self.assertEqual(parse_meminfo_total(
            "TOTAL PSS: 1,234\nTOTAL RSS: 5,678\n", "PSS"), 1234)
        self.assertEqual(parse_meminfo_total(
            "TOTAL PSS: 148105    TOTAL RSS: 287252    TOTAL SWAP PSS: 287",
            "RSS"), 287252)
        self.assertEqual(parse_app_processes(
            "  PID    RSS NAME\n 1234  2048 com.example.app\n",
            "com.example.app")[0].rss_kb, 2048)
        with patch.object(self.client, "run", side_effect=[
            output, "TOTAL PSS: 1,234\nTOTAL RSS: 5,678",
        ]):
            report = self.client.app_process_report("ABC", "com.example.app")
        self.assertEqual((len(report.processes), report.total_pss_kb,
                          report.total_rss_kb), (2, 1234, 5678))

    def test_permission_command_validates_name(self):
        with patch.object(self.client, "run", return_value="") as run:
            self.client.set_app_permission(
                "ABC", "com.example.app", "android.permission.CAMERA", grant=True)
        run.assert_called_once_with("ABC", "shell", "pm", "grant",
                                    "com.example.app", "android.permission.CAMERA",
                                    timeout=30)
        with self.assertRaises(AdbError):
            self.client.set_app_permission(
                "ABC", "com.example.app", "android.permission.CAMERA;reboot",
                grant=False)

    def test_check_permission_reads_one_effective_state(self):
        with patch.object(self.client, "run", return_value="0") as run:
            self.assertTrue(self.client.check_app_permission(
                "ABC", "com.example.app", "android.permission.CAMERA"))
        run.assert_called_once_with(
            "ABC", "shell", "dumpsys", "package", "check-permission",
            "android.permission.CAMERA", "com.example.app", "0", timeout=15)
        with patch.object(self.client, "run", return_value="-1"):
            self.assertFalse(self.client.check_app_permission(
                "ABC", "com.example.app", "android.permission.CAMERA"))
        with patch.object(self.client, "run", return_value="unknown command"):
            with self.assertRaises(AdbError):
                self.client.check_app_permission(
                    "ABC", "com.example.app", "android.permission.CAMERA")

    def test_batch_permission_refresh_rejects_incomplete_results(self):
        names = ["android.permission.CAMERA", "android.permission.RECORD_AUDIO"]
        with patch.object(self.client, "run", return_value=(
            "android.permission.CAMERA 0\n"
            "android.permission.RECORD_AUDIO -1\n")) as run:
            self.assertEqual(self.client.app_permission_states(
                "ABC", "com.example.app", names), {
                    names[0]: True, names[1]: False,
                })
        self.assertIn("check-permission", run.call_args.args[2])
        with patch.object(self.client, "run", return_value=
                          "android.permission.CAMERA 0"):
            with self.assertRaises(AdbError):
                self.client.app_permission_states("ABC", "com.example.app", names)


if __name__ == "__main__":
    unittest.main()
