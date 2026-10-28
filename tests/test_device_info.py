"""Device hardware and capacity parsing without a connected handset."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from adb_client import AdbClient, AdbError
from device_info import UNKNOWN, parse_device_info
from main_qt import DeviceInfoPage


class DeviceInfoTests(unittest.TestCase):
    def test_parses_cpu_gpu_storage_and_ram(self):
        properties = (
            "[ro.product.marketname]: [Example Phone]\n"
            "[ro.product.manufacturer]: [Example]\n"
            "[ro.build.version.release]: [15]\n"
            "[ro.soc.model]: [SM8650]\n"
            "[ro.soc.manufacturer]: [Qualcomm]\n"
            "[ro.board.platform]: [pineapple]\n"
            "[ro.product.cpu.abilist]: [arm64-v8a,armeabi-v7a]\n"
            "[ro.hardware.egl]: [adreno]\n"
        )
        cpuinfo = "processor : 0\nHardware : Qualcomm Technologies, Inc\n\nprocessor : 1\n"
        gpu = "GLES: Qualcomm, Adreno (TM) 750, OpenGL ES 3.2 V@1\n"
        memory = (
            "MemTotal:        8192000 kB\nMemAvailable:   4096000 kB\n"
            "MemFree:         1024000 kB\nCached:          2048000 kB\n"
            "SwapTotal:       2097152 kB\nSwapFree:        1048576 kB\n"
        )
        data = ("Filesystem 1K-blocks Used Available Use% Mounted on\n"
                "/dev/block/dm-2 104857600 41943040 62914560 40% /data\n")
        system = "/dev/block/dm-0 8388608 6291456 2097152 75% /system\n"

        info = parse_device_info(
            properties, cpuinfo, gpu, memory, data, system,
            "1800000\n3200000\n",
        )

        self.assertEqual(info.device["型号"], "Example Phone")
        self.assertEqual(info.cpu["SoC 型号"], "SM8650")
        self.assertEqual(info.cpu["逻辑核心"], "2")
        self.assertEqual(info.cpu["最高标称频率"], "3.20 GHz")
        self.assertEqual(info.gpu["GPU / 渲染器"], "Adreno (TM) 750")
        self.assertEqual(info.gpu["图形 API"], "OpenGL ES 3.2 V@1")
        self.assertEqual(info.storage["内部存储总量（/data）"], "100.0 GiB")
        self.assertEqual(info.storage["系统分区可用"], "2.0 GiB")
        self.assertEqual(info.ram["物理内存总量"], "7.8 GiB")
        self.assertEqual(info.ram["Swap 可用"], "1.0 GiB")

    def test_missing_fields_are_explicit_and_optional_probe_can_fail(self):
        client = AdbClient("/usr/bin/true")

        def run(_serial, *args, **_kwargs):
            if args == ("shell", "getprop"):
                return ("[ro.product.marketname]: [null]\n"
                        "[ro.product.model]: [Fallback Phone]\n"
                        "[ro.opengles.version]: [196610]\n")
            if args == ("shell", "cat", "/proc/meminfo"):
                return "MemTotal: 4194304 kB\n"
            raise AdbError("permission denied")

        with patch.object(client, "run", side_effect=run) as command:
            info = client.device_info("ABC123")

        self.assertEqual(info.device["型号"], "Fallback Phone")
        self.assertEqual(info.gpu["GPU / 渲染器"], UNKNOWN)
        self.assertEqual(info.gpu["图形 API"], "OpenGL ES 3.2")
        self.assertEqual(info.storage["可用"], UNKNOWN)
        self.assertEqual(info.ram["物理内存总量"], "4.0 GiB")
        self.assertEqual(command.call_args_list[0].args,
                         ("ABC123", "shell", "getprop"))
        self.assertIn(("ABC123", "shell", "df", "-k", "/data"),
                      [call.args for call in command.call_args_list])

    def test_getprop_failure_is_reported(self):
        client = AdbClient("/usr/bin/true")
        with patch.object(client, "run", side_effect=AdbError("device offline")):
            with self.assertRaisesRegex(AdbError, "device offline"):
                client.device_info("ABC123")

    def test_page_ignores_old_device_result_and_loads_new_device(self):
        selected = {"device": SimpleNamespace(
            serial="OLD", state="device", display_name="旧设备")}
        submitted = []

        def submit(_title, work, done, finished=None, **_kwargs):
            submitted.append((work, done, finished))
            return True

        window = SimpleNamespace(
            client=Mock(),
            current_device=lambda: selected["device"],
            submit=submit,
            pages=SimpleNamespace(currentIndex=lambda: 6),
        )
        model = Mock()
        page = SimpleNamespace(
            window=window, summary=Mock(), refresh_button=Mock(),
            values={"cpu": {"SoC 型号": model}}, loaded_serial=None,
            refresh=Mock(),
        )
        DeviceInfoPage.refresh(page)
        _work, done, finished = submitted.pop()

        selected["device"] = SimpleNamespace(
            serial="NEW", state="device", display_name="新设备")
        finished()
        done(parse_device_info("[ro.soc.model]: [OLD CHIP]", "", "", "", "", ""))

        page.refresh.assert_called_once_with()
        model.setText.assert_not_called()
