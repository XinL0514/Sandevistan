"""Regression tests for the install target mode shown in the Qt interface."""

import unittest
import tempfile
import queue
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from adb_client import Device
from main_qt import AppMessageBox, AppsPage, MainWindow


class FakeCombo:
    def __init__(self):
        self.index = 0

    def currentIndex(self):
        return self.index

    def setCurrentIndex(self, index):
        self.index = index

    def blockSignals(self, blocked):
        pass


class InstallTargetTests(unittest.TestCase):
    def setUp(self):
        self.usb = Device("USB123", "device", "Pad")
        self.wifi = Device("192.168.1.2:5555", "device", "Phone")
        self.combo = FakeCombo()
        self.window = SimpleNamespace(
            apps_page=SimpleNamespace(target_mode=self.combo),
            install_targets={self.wifi.serial},
            ready_devices=lambda: [self.usb, self.wifi],
            current_device=lambda: self.usb,
        )

    def targets(self):
        return MainWindow.install_target_devices(self.window)

    def test_switching_modes_changes_effective_install_targets(self):
        self.assertEqual(self.targets(), [self.usb])
        self.combo.setCurrentIndex(1)
        self.assertEqual(self.targets(), [self.usb, self.wifi])
        self.combo.setCurrentIndex(2)
        self.assertEqual(self.targets(), [self.wifi])
        self.window.install_targets = {self.usb.serial, self.wifi.serial}
        self.assertEqual(self.targets(), [self.usb, self.wifi])
        self.combo.setCurrentIndex(0)
        self.assertEqual(self.targets(), [self.usb])

    def test_cancelling_custom_selection_restores_previous_mode(self):
        self.combo.setCurrentIndex(1)
        page = SimpleNamespace(
            window=self.window,
            target_mode=self.combo,
            edit_targets_button=SimpleNamespace(setVisible=lambda _: None),
            _previous_target_mode=1,
            _update_target_summary=lambda: None,
        )
        self.window.choose_install_targets = lambda initial: False
        self.combo.setCurrentIndex(2)
        AppsPage._target_mode_changed(page, 2)
        self.assertEqual(self.combo.currentIndex(), 1)
        self.assertEqual(self.targets(), [self.usb, self.wifi])

    def test_custom_targets_can_be_edited_without_changing_mode(self):
        selected = []
        self.window.choose_install_targets = lambda initial: selected.append(initial) or True
        page = SimpleNamespace(
            window=self.window,
            _update_target_summary=lambda: selected.append("updated"),
        )
        AppsPage._edit_install_targets(page)
        self.assertEqual(selected, [{self.wifi.serial}, "updated"])

    def test_downgrade_flag_survives_both_data_choices(self):
        for keep_data in (True, False):
            with self.subTest(keep_data=keep_data), tempfile.TemporaryDirectory() as folder:
                apk = Path(folder) / "older.apk"
                apk.write_bytes(b"APK")
                calls = []

                def run(serial, *args, **kwargs):
                    calls.append((serial, args))
                    return "Success"

                def is_package_installed(serial, package):
                    calls.append((serial, ("check", package)))
                    return True

                def install_apk(serial, path, flags):
                    calls.append((serial, ("install", *flags, str(path))))
                    return "Success"

                def submit(_name, work, done, **kwargs):
                    self.assertTrue(kwargs["independent"])
                    done(work())

                window = SimpleNamespace(
                    client=SimpleNamespace(
                        run=run, install_apk=install_apk,
                        is_package_installed=is_package_installed),
                    install_target_devices=lambda: [self.usb],
                    submit=submit, log=lambda _: None,
                    set_status=lambda _: None,
                )
                page = SimpleNamespace(
                    window=window,
                    install_lock=threading.Lock(),
                    apk_path=SimpleNamespace(text=lambda: str(apk)),
                    downgrade_box=SimpleNamespace(isChecked=lambda: True),
                    keep_data_box=SimpleNamespace(isChecked=lambda: keep_data),
                )
                with patch("main_qt.apk_package_name", return_value="com.example.app"), \
                        patch.object(AppMessageBox, "question",
                                     return_value=AppMessageBox.Yes) as question:
                    AppsPage._install_apk(page)
                install = [args for _, args in calls if args[0] == "install"]
                expected_flags = ("-r", "-d") if keep_data else ("-d",)
                self.assertEqual(install, [
                    ("install", *expected_flags, str(apk))
                ])
                uninstall = [args for _, args in calls if args[0] == "uninstall"]
                self.assertEqual(bool(uninstall), not keep_data)
                self.assertEqual(question.called, not keep_data)

    def test_install_without_existing_app_skips_uninstall(self):
        with tempfile.TemporaryDirectory() as folder:
            apk = Path(folder) / "new.apk"
            apk.write_bytes(b"APK")
            calls = []
            client = SimpleNamespace(
                is_package_installed=lambda serial, package: False,
                run=lambda *args, **kwargs: calls.append(args),
                install_apk=lambda serial, path, flags: calls.append(
                    (serial, path, flags)) or "Success",
            )
            window = SimpleNamespace(
                client=client,
                install_target_devices=lambda: [self.usb],
                submit=lambda _name, work, done, **kwargs: done(work()),
                log=lambda _: None, set_status=lambda _: None,
            )
            page = SimpleNamespace(
                window=window,
                install_lock=threading.Lock(),
                apk_path=SimpleNamespace(text=lambda: str(apk)),
                downgrade_box=SimpleNamespace(isChecked=lambda: False),
                keep_data_box=SimpleNamespace(isChecked=lambda: False),
            )
            with patch("main_qt.apk_package_name", return_value="com.example.new"), \
                    patch.object(AppMessageBox, "question", return_value=AppMessageBox.Yes):
                AppsPage._install_apk(page)
            self.assertEqual(calls, [(self.usb.serial, apk, [])])

    def test_install_task_does_not_hold_global_busy_state(self):
        events = queue.Queue()
        logs = []
        window = SimpleNamespace(
            busy=True, events=events,
            log=logs.append, set_status=lambda _: None,
            setCursor=lambda _: None, unsetCursor=lambda: None,
        )
        class ImmediateThread:
            def __init__(self, target, daemon):
                self.target = target

            def start(self):
                self.target()

        with patch("main_qt.threading.Thread", ImmediateThread):
            self.assertTrue(MainWindow.submit(
                window, "安装 APK", lambda: "Success", independent=True))
            self.assertTrue(window.busy)
            MainWindow._drain_events(window)
            self.assertTrue(window.busy)
            self.assertTrue(MainWindow.submit(
                window, "另一个安装任务", lambda: "Success", independent=True))
            self.assertFalse(MainWindow.submit(window, "普通任务", lambda: None))


if __name__ == "__main__":
    unittest.main()
