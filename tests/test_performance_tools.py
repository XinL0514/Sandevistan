import unittest
from unittest.mock import patch

from adb_client import AdbClient, AdbError
from performance_tools import (
    PerformanceSample, cpu_percent, fps_from_frames, parse_device_network,
    parse_gfxinfo_frames, parse_proc_stats, parse_surface_latency,
    parse_surface_layers, parse_uid_network,
)


class PerformanceToolsTests(unittest.TestCase):
    def test_proc_cpu_uses_matching_pids_and_one_core_percentage(self):
        def stat(pid, user, system):
            fields = ["S", *(["0"] * 10), str(user), str(system)]
            return f"{pid} (app process) " + " ".join(fields)

        before = parse_proc_stats(
            stat(123, 10, 5) + "\ncpu 100 0 100 100 0 0 0 0 0 0\n"
            "cpu0 1\ncpu1 1\ncpu2 1\ncpu3 1\n")
        after = parse_proc_stats(
            stat(123, 45, 30) + "\ncpu 200 0 200 200 0 0 0 0 0 0\n"
            "cpu0 1\ncpu1 1\ncpu2 1\ncpu3 1\n")
        self.assertEqual(before[0], {123: 15})
        make = lambda values: PerformanceSample(
            0, 1, values[0], values[1], values[2], None, None,
            None, None, None, (), "")
        self.assertAlmostEqual(cpu_percent(make(before), make(after)), 80.0)

    def test_device_and_uid_network_counters_have_explicit_scopes(self):
        output = (
            "Inter-| Receive | Transmit\n"
            "lo: 999 0 0 0 0 0 0 0 999 0 0 0 0 0 0 0\n"
            "wlan0: 1200 0 0 0 0 0 0 0 500 0 0 0 0 0 0 0\n"
            "tun0: 800 0 0 0 0 0 0 0 200 0 0 0 0 0 0 0\n"
        )
        self.assertEqual(parse_device_network(output), (1200, 500))
        history = (
            "Dev stats:\n  ident=[] uid=-1\nUID stats:\n"
            "  ident=[] uid=10241 set=DEFAULT tag=0x0\n"
            "    st=1 rb=12 rp=0 tb=7 tp=0\n"
            "  ident=[] uid=10241 set=FOREGROUND tag=0x0\n"
            "    st=2 rb=30 rp=0 tb=4 tp=0\n"
            "  ident=[] uid=10242 set=DEFAULT tag=0x0\n"
            "    st=2 rb=900 rp=0 tb=900 tp=0\n"
            "UID tag stats:\n"
            "  ident=[] uid=10241 set=DEFAULT tag=0x123\n"
            "    st=2 rb=400 rp=0 tb=400 tp=0\n"
        )
        self.assertEqual(parse_uid_network(history, 10241), (42, 11))
        self.assertIsNone(parse_uid_network(history, 99999))

    def test_surfaceview_and_gfxinfo_frame_sources(self):
        layers = (
            "SurfaceView[com.example.app/.Main](BLAST)#7\n"
            "com.example.app/.Main#5\n"
            "SurfaceView[com.example.application/.Main](BLAST)#8\n"
        )
        self.assertEqual(parse_surface_layers(layers, "com.example.app"), [
            "SurfaceView[com.example.app/.Main](BLAST)#7",
            "com.example.app/.Main#5",
        ])
        latency = (
            "16666666\n1 1000000000 3\n1 1016666667 3\n"
            "1 9223372036854775807 3\n")
        self.assertEqual(parse_surface_latency(latency),
                         (1000000000, 1016666667))
        self.assertAlmostEqual(
            fps_from_frames(1000000000, (1016666667, 1033333334)), 60, delta=0.01)
        self.assertEqual(fps_from_frames(1033333334, (1016666667, 1033333334)), 0)
        gfx = (
            "---PROFILEDATA---\nFlags,FrameCompleted,Other,\n"
            "0,123456,0,\n0,123999,0,\n---PROFILEDATA---\n")
        self.assertEqual(parse_gfxinfo_frames(gfx), (123456, 123999))

    def test_surface_layers_unwrap_state_and_skip_activity_records(self):
        layers = (
            "RequestedLayerState{ActivityRecord{42 u0 com.example.app/.Main t1}#1 parentId=9}\n"
            "RequestedLayerState{a1b2c3 SurfaceView[com.example.app/.Main](BLAST)#3 parentId=2}\n"
            "RequestedLayerState{d4e5f6 com.example.app/.Main#2 parentId=1}\n"
        )
        self.assertEqual(parse_surface_layers(layers, "com.example.app"), [
            "a1b2c3 SurfaceView[com.example.app/.Main](BLAST)#3",
            "d4e5f6 com.example.app/.Main#2",
        ])

    def test_gfxinfo_ignores_flagged_frames(self):
        gfx = (
            "---PROFILEDATA---\nFlags,FrameCompleted,Other,\n"
            "0,123456,0,\n32,123999,0,\n"
            "---PROFILEDATA---\n")
        self.assertEqual(parse_gfxinfo_frames(gfx), (123456,))

    def test_client_samples_without_affecting_unavailable_metrics(self):
        client = AdbClient("/usr/bin/true")
        responses = {
            "ps": "PID NAME\n123 com.example.app\n",
            "cat": "123 (app) S " + " ".join(["0"] * 10 + ["12", "8"]) +
                   "\ncpu 100 0 100 100 0 0 0 0\ncpu0 1\n",
            "dumpsys": "",
        }
        def run(_serial, *args, **_kwargs):
            if args[:2] == ("shell", "ps"):
                return responses["ps"]
            if args[:3] == ("shell", "cat", "/proc/net/dev"):
                return "wlan0: 100 0 0 0 0 0 0 0 200 0 0 0 0 0 0 0"
            if args[:2] == ("shell", "cat"):
                return responses["cat"]
            if args[:3] == ("shell", "dumpsys", "meminfo"):
                return "TOTAL PSS: 2048\nTOTAL RSS: 4096"
            if args[:3] == ("shell", "dumpsys", "SurfaceFlinger"):
                return "SurfaceView[com.example.app/.Main](BLAST)#7" \
                    if args[3] == "--list" else ""
            if args[:2] == ("shell", "dumpsys SurfaceFlinger --latency 'SurfaceView[com.example.app/.Main](BLAST)#7'"):
                return "16666666\n1 1000000000 3"
            return ""
        with patch.object(client, "run", side_effect=run):
            sample = client.performance_sample("ABC", "com.example.app")
        self.assertEqual(sample.process_count, 1)
        self.assertEqual((sample.pss_kb, sample.rss_kb), (2048, 4096))
        self.assertEqual((sample.device_rx_bytes, sample.device_tx_bytes), (100, 200))

    def test_client_falls_back_when_extended_ps_is_unavailable(self):
        client = AdbClient("/usr/bin/true")
        def run(_serial, *args, **_kwargs):
            if args == ("shell", "ps", "-A", "-o", "PID,NAME"):
                raise AdbError("unknown ps option")
            if args == ("shell", "ps", "-A"):
                return "USER PID PPID VSZ RSS WCHAN ADDR S NAME\nu0_a1 123 1 0 100 0 0 S com.example.app"
            return ""
        with patch.object(client, "run", side_effect=run):
            sample = client.performance_sample("ABC", "com.example.app")
        self.assertEqual(sample.process_count, 1)


if __name__ == "__main__":
    unittest.main()
