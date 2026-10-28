import unittest

from adb_client import AdbError, monkey_arguments, monkey_result


class MonkeyTests(unittest.TestCase):
    def test_arguments_limit_events_to_one_package_and_device(self):
        self.assertEqual(monkey_arguments("USB123", "com.example.app", 500, 100), [
            "-s", "USB123", "shell", "monkey", "-p", "com.example.app",
            "--throttle", "100", "-v", "500",
        ])
        for package, events, throttle in (
            ("com.example.app; reboot", 500, 100),
            ("com.example.app", 0, 100),
            ("com.example.app", 100001, 100),
            ("com.example.app", 500, -1),
            ("com.example.app", 500, 10001),
        ):
            with self.subTest(package=package, events=events, throttle=throttle):
                with self.assertRaises(AdbError):
                    monkey_arguments("USB123", package, events, throttle)

    def test_result_requires_all_events_and_no_crash(self):
        self.assertEqual(monkey_result("Events injected: 500", 0, 500),
                         (True, "Monkey 测试完成 · 已注入 500/500 个事件"))
        for output, code in (
            ("Events injected: 499", 0),
            ("Events injected: 500\n// CRASH: com.example.app", 0),
            ("** No activities found to run, monkey aborted.", 0),
            ("Events injected: 500", 1),
        ):
            with self.subTest(output=output, code=code):
                self.assertFalse(monkey_result(output, code, 500)[0])


if __name__ == "__main__":
    unittest.main()
