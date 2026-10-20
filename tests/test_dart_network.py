import unittest
from unittest.mock import patch

from dart_network import (
    DartNetworkError, DartNetworkSession, find_service_uri, request_key,
)


class FakeClient:
    def __init__(self):
        self.calls = []

    def run(self, _serial, *args, **_kwargs):
        self.calls.append(args)
        if args[:3] == ("shell", "pidof", "com.example.app"):
            return "1234"
        if args[:3] == ("shell", "logcat", "-d"):
            return ("I/flutter ( 1234): The Dart VM service is listening on "
                    "http://127.0.0.1:4321/token=/\n")
        if args == ("forward", "tcp:0", "tcp:4321"):
            return "51999"
        if args == ("forward", "--remove", "tcp:51999"):
            return ""
        raise AssertionError(args)


class DartNetworkTests(unittest.TestCase):
    def test_service_uri_requires_loopback_and_token_path(self):
        self.assertIsNone(find_service_uri(
            "The Dart VM service is listening on http://example.com:1234/token=/"))
        self.assertIsNone(find_service_uri(
            "The Dart VM service is listening on http://127.0.0.1:1234/../"))
        uri = find_service_uri(
            "I/flutter: The Dart VM service is listening on "
            "http://127.0.0.1:4321/token=/")
        self.assertEqual((uri.port, uri.path), (4321, "/token=/"))

    def test_connect_poll_detail_and_restore_recording(self):
        client = FakeClient()
        session = DartNetworkSession(client, "ABC", "com.example.app")
        enabled = False
        calls = []
        old = {"id": "old", "isolateId": "isolates/1", "method": "GET"}
        fresh = {"id": "new", "isolateId": "isolates/1", "method": "POST"}

        def rpc(method, **params):
            nonlocal enabled
            calls.append((method, params))
            if method == "getVM":
                return {"isolates": [{"id": "isolates/1"}]}
            if method == "ext.dart.io.httpEnableTimelineLogging":
                if "enabled" in params:
                    enabled = params["enabled"] == "true"
                return {"enabled": enabled}
            if method == "ext.dart.io.getHttpProfile":
                return {"timestamp": 2_000_000,
                        "requests": [old] if not enabled else [old, fresh]}
            if method == "ext.dart.io.getHttpProfileRequest":
                return {"id": params["id"], "requestBody": [65, 66]}
            raise AssertionError(method)

        with patch.object(session, "_rpc", side_effect=rpc):
            session.connect()
            self.assertTrue(enabled)
            self.assertEqual([request_key(item) for item in session.poll()],
                             [("isolates/1", "new")])
            self.assertEqual(session.request_detail("isolates/1", "new")
                             ["requestBody"], [65, 66])
            session.close()
        self.assertFalse(enabled)
        self.assertIn(("forward", "--remove", "tcp:51999"), client.calls)
        self.assertTrue(any(method == "ext.dart.io.httpEnableTimelineLogging"
                            and params.get("enabled") == "false"
                            for method, params in calls))

    def test_failed_handshake_removes_forward(self):
        client = FakeClient()
        session = DartNetworkSession(client, "ABC", "com.example.app")
        with patch.object(session, "_rpc", side_effect=DartNetworkError("failed")):
            with self.assertRaises(DartNetworkError):
                session.connect()
        self.assertIn(("forward", "--remove", "tcp:51999"), client.calls)


if __name__ == "__main__":
    unittest.main()
