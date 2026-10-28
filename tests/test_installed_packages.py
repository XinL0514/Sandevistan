"""Installed-app category selection in the Qt interface."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from main_qt import AppsPage


class InstalledPackagesTests(unittest.TestCase):
    def test_package_type_switch_clears_list_and_loads_selection(self):
        page = SimpleNamespace(
            package_tree=Mock(),
            package_count=Mock(),
            package_hint=Mock(),
            tool_package=Mock(),
            foreground_value=Mock(),
            _load_packages=Mock(),
        )

        AppsPage._package_type_changed(page, 1)

        page.package_tree.clear.assert_called_once_with()
        page.package_count.setText.assert_called_once_with("0 个")
        page.tool_package.clear.assert_called_once_with()
        page.foreground_value.clear.assert_called_once_with()
        page._load_packages.assert_called_once_with()

    def test_package_load_uses_selected_category(self):
        client = SimpleNamespace(
            list_user_packages=Mock(return_value=["com.example.downloaded"]),
            list_system_packages=Mock(return_value=["com.example.preloaded"]),
        )
        selected = {"category": "user"}
        combo = SimpleNamespace(currentData=lambda: selected["category"])
        device = SimpleNamespace(serial="ABC123")
        submitted = []

        def submit(_title, work, done, finished=None):
            submitted.append((work, done, finished))
            return True

        window = SimpleNamespace(
            client=client,
            selected_serial=lambda: device.serial,
            current_device=lambda: device,
            submit=submit,
        )
        page = SimpleNamespace(window=window, package_type=combo)

        AppsPage._load_packages(page)
        user_work, user_done, user_finished = submitted.pop()
        self.assertEqual(user_work(), ["com.example.downloaded"])
        client.list_user_packages.assert_called_once_with(device.serial)

        selected["category"] = "system"
        page._load_packages = Mock()
        user_finished()
        page._load_packages.assert_called_once_with()
        user_done(["com.example.downloaded"])

        AppsPage._load_packages(page)
        self.assertEqual(submitted.pop()[0](), ["com.example.preloaded"])
        client.list_system_packages.assert_called_once_with(device.serial)
