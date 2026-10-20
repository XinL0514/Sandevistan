import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adb_client import (
    AdbClient, AdbError, Device, RemoteEntry, parse_detailed_directory, parse_devices,
    parse_directory, parse_package_list, validate_endpoint,
)


class DeviceParsingTests(unittest.TestCase):
    def test_user_packages_need_only_one_package_manager_call(self):
        output = ("package:com.example.two\n"
                  "package:com.example.one\n"
                  "package:com.example.one\n"
                  "unexpected output\n")
        self.assertEqual(parse_package_list(output), [
            "com.example.one", "com.example.two"
        ])
        client = AdbClient("/usr/bin/true")
        with patch.object(client, "run", return_value=output) as run:
            packages = client.list_user_packages("ABC123")
        self.assertEqual(packages, ["com.example.one", "com.example.two"])
        run.assert_called_once_with(
            "ABC123", "shell", "pm", "list", "packages", "-3", timeout=30
        )

    def test_parse_devices_and_states(self):
        output = """List of devices attached
ABC123 device product:x model:Pixel_9 device:y transport_id:1
XYZ789 unauthorized usb:1-2 transport_id:2
"""
        devices = parse_devices(output)
        self.assertEqual([(d.serial, d.state, d.model) for d in devices], [
            ("ABC123", "device", "Pixel_9"),
            ("XYZ789", "unauthorized", ""),
        ])

    def test_device_label_prefers_android_name_and_shows_connection(self):
        usb = Device("ABC123", "device", "25099RP13C", "REDMI Pad 2 Pro")
        wifi = Device("192.168.1.20:37123", "device", "Pixel_9", "客厅手机")
        self.assertEqual(usb.label, "REDMI Pad 2 Pro · USB · ABC123")
        self.assertEqual(wifi.label, "客厅手机 · Wi-Fi · 192.168.1.20:37123")

    def test_device_name_comes_from_android_settings(self):
        client = AdbClient("/usr/bin/true")
        with patch.object(client, "_execute", return_value=(
            "List of devices attached\nABC123 device model:25099RP13C\n"
            "XYZ789 unauthorized\n"
        )), patch.object(client, "run", return_value="REDMI Pad 2 Pro") as run:
            devices = client.list_devices()
        self.assertEqual(devices[0].display_name, "REDMI Pad 2 Pro")
        self.assertEqual(devices[1].name, "")
        run.assert_called_once_with("ABC123", "shell", "settings", "get",
                                    "global", "device_name", timeout=5)

    def test_wifi_address_validation_and_commands(self):
        self.assertEqual(validate_endpoint(" 192.168.1.20:37123 "),
                         "192.168.1.20:37123")
        for address in ("192.168.1.20", "192.168.1.20:0", "bad address:123",
                        "192.168.1.20:65536"):
            with self.assertRaises(AdbError):
                validate_endpoint(address)
        client = AdbClient("/usr/bin/true")
        with patch.object(client, "_execute", side_effect=[
            "Successfully paired to 192.168.1.20:37123",
            "connected to 192.168.1.20:38333",
        ]) as execute:
            client.pair_wifi("192.168.1.20:37123", "123456")
            client.connect_wifi("192.168.1.20:38333")
        self.assertEqual(execute.call_args_list[0].args[0],
                         ["pair", "192.168.1.20:37123", "123456"])
        self.assertEqual(execute.call_args_list[1].args[0],
                         ["connect", "192.168.1.20:38333"])

    def test_batch_install_continues_after_a_device_fails(self):
        client = AdbClient("/usr/bin/true")
        apk = Path("/tmp/example.apk")
        with patch.object(client, "run", side_effect=[
            "Success", AdbError("设备离线"), "Success",
        ]) as run:
            results = client.install_many(["A", "B", "C"], apk)
        self.assertEqual([(item.serial, item.success) for item in results],
                         [("A", True), ("B", False), ("C", True)])
        self.assertEqual(results[1].detail, "设备离线")
        self.assertEqual([call.args[0] for call in run.call_args_list],
                         ["A", "B", "C"])

    def test_screenshot_rejects_invalid_output(self):
        with tempfile.TemporaryDirectory() as folder:
            fake_adb = Path(folder) / "adb"
            fake_adb.write_text("#!/bin/sh\nprintf 'not a png'\n")
            fake_adb.chmod(0o755)
            destination = Path(folder) / "screenshot.png"
            with self.assertRaises(AdbError):
                AdbClient(str(fake_adb)).screenshot("ABC123", destination)
            self.assertFalse(destination.exists())

    def test_device_serial_and_file_path_are_separate_arguments(self):
        with tempfile.TemporaryDirectory() as folder:
            fake_adb = Path(folder) / "adb"
            fake_adb.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
            fake_adb.chmod(0o755)
            output = AdbClient(str(fake_adb)).run(
                "ABC123", "push", "/tmp/a file.txt", "/sdcard/Download/"
            )
            self.assertEqual(output.splitlines(), [
                "-s", "ABC123", "push", "/tmp/a file.txt", "/sdcard/Download/"
            ])

    def test_remote_directory_lists_folders_first_and_keeps_spaces(self):
        entries = parse_directory(
            "report 1.txt\n.hidden/\nPhotos/\n", "/sdcard/Download"
        )
        self.assertEqual([(item.name, item.is_dir, item.path) for item in entries], [
            (".hidden", True, "/sdcard/Download/.hidden"),
            ("Photos", True, "/sdcard/Download/Photos"),
            ("report 1.txt", False, "/sdcard/Download/report 1.txt"),
        ])

    def test_remote_directory_path_is_quoted_for_device_shell(self):
        with tempfile.TemporaryDirectory() as folder:
            fake_adb = Path(folder) / "adb"
            arguments = Path(folder) / "arguments"
            fake_adb.write_text(
                f"#!/bin/sh\nprintf '%s\\n' \"$4\" > '{arguments}'\n"
                "printf 'total 1\\n"
                "drwxr-xr-x 2 root root 4096 2026-10-19 15:00 My folder\\n"
                "-rw-r--r-- 1 root root 99 2026-10-19 15:01 report 1.txt\\n'\n"
            )
            fake_adb.chmod(0o755)
            entries = AdbClient(str(fake_adb)).list_directory(
                "ABC123", "/sdcard/My Folder"
            )
            self.assertEqual(arguments.read_text().strip(),
                             "ls -la -- '/sdcard/My Folder/'")
            self.assertEqual([item.name for item in entries],
                             ["My folder", "report 1.txt"])

    def test_remove_remote_file_and_folder_quotes_device_paths(self):
        client = AdbClient("/usr/bin/true")
        with patch.object(client, "run", return_value="") as run:
            client.remove_remote_entry(
                "ABC123", "/sdcard/Download",
                RemoteEntry("Bob's file.txt", "/sdcard/Download/Bob's file.txt", False))
            client.remove_remote_entry(
                "ABC123", "/sdcard/Download",
                RemoteEntry("My folder", "/sdcard/Download/My folder", True))
        self.assertEqual(run.call_args_list[0].args[:2], ("ABC123", "shell"))
        self.assertEqual(run.call_args_list[0].args[2],
                         "rm -- '/sdcard/Download/Bob'\"'\"'s file.txt'")
        self.assertEqual(run.call_args_list[1].args[2],
                         "rm -r -- '/sdcard/Download/My folder'")

    def test_remove_remote_entry_rejects_root_and_non_child_paths(self):
        client = AdbClient("/usr/bin/true")
        cases = [
            ("/", RemoteEntry("sdcard", "/sdcard", True)),
            ("/sdcard", RemoteEntry("..", "/sdcard/..", True)),
            ("/sdcard", RemoteEntry("secret", "/data/secret", False)),
            ("/sdcard", RemoteEntry("other/file", "/sdcard/other/file", False)),
        ]
        with patch.object(client, "run") as run:
            for directory, entry in cases:
                with self.subTest(directory=directory, name=entry.name):
                    with self.assertRaises(AdbError):
                        client.remove_remote_entry("ABC123", directory, entry)
        run.assert_not_called()

    def test_detailed_listing_reads_size_date_and_spaced_name(self):
        entries = parse_detailed_directory(
            "total 1\n-rw-rw---- 1 root media_rw 1024 2026-10-19 15:20 my file.txt\n",
            "/sdcard/Download",
        )
        self.assertEqual((entries[0].name, entries[0].size, entries[0].modified),
                         ("my file.txt", 1024, "2026-10-19 15:20"))


if __name__ == "__main__":
    unittest.main()
