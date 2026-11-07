import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from controller import ControlError, EventLog
from myuplink import MyUplink, NoRedirects


SYSTEM = "a2f23e7c-839e-4f16-85f6-55a0e6555906"


class MyUplinkTests(unittest.TestCase):
    def setUp(self):
        self.client = MyUplink({"myuplink_client_id": "test-client", "myuplink_client_secret": "test-secret",
                               "myuplink_system_id": SYSTEM, "myuplink_device_id": "selected-device"})
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.events = EventLog(Path(self.temp.name) / "events.sqlite")
        self.alarm = {"id": "12345", "alarmNumber": 229, "deviceId": "selected-device",
                      "status": "Active", "header": "Korta drifttider"}
        self.client.request = Mock(side_effect=self.response)
        self.sent = []

    def response(self, path, data=None, token=None):
        self.sent.append((path, data, token))
        if path == "/oauth/token":
            return {"access_token": "test-token", "expires_in": 3600}
        if path.endswith("/reset"):
            return {"status": 200, "payload": {}}
        return {"notifications": [self.alarm], "numItems": 1}

    def test_optional_configuration_and_invalid_identifiers_fail_explicitly(self):
        self.assertFalse(MyUplink({}).report()["configured"])
        self.client.client_secret = ""
        with self.assertRaises(ControlError):
            self.client.report()
        self.client.client_secret = "test-secret"
        self.client.system_id = "../foreign"
        with self.assertRaises(ControlError):
            self.client.report()
        self.client.request.assert_not_called()

    def test_uuid_is_readable_but_never_substituted_with_alarm_number(self):
        self.alarm["id"] = SYSTEM
        report = self.client.report()
        self.assertFalse(report["alarms"][0]["can_reset"])
        self.assertIn("UUID", report["alarms"][0]["blocked_reason"])
        with self.assertRaises(ControlError):
            self.client.reset(SYSTEM, self.events)
        with self.assertRaises(ControlError):
            self.client.reset("229", self.events)
        self.assertFalse(any(p.endswith("/reset") for p, _, _ in self.sent))
        self.assertNotIn("test-secret", json.dumps(report))
        self.assertNotIn("test-token", json.dumps(report))

    def test_numeric_id_reset_is_single_request_not_claimed_cleared_and_logged(self):
        self.client.report()
        result = self.client.reset("12345", self.events)
        self.assertFalse(result["confirmed_cleared"])
        posts = [(p, body) for p, body, _ in self.sent if p.endswith("/reset")]
        self.assertEqual(posts, [(f"/v2/systems/{SYSTEM}/notifications/12345/reset", b"{}")])
        with self.assertRaisesRegex(ControlError, "två minuter"):
            self.client.reset("12345", self.events)
        kinds = [e["kind"] for e in self.events.recent()]
        self.assertIn("alarm_reset_attempt", kinds)
        self.assertIn("alarm_reset_sent", kinds)
        self.assertEqual(len([p for p, _, _ in self.sent if p == "/oauth/token"]), 1)

    def test_persistent_cooldown_survives_client_restart(self):
        self.client.reset("12345", self.events)
        other = MyUplink({"myuplink_client_id": "client", "myuplink_client_secret": "secret",
                         "myuplink_system_id": SYSTEM, "myuplink_device_id": "selected-device"})
        other.request = Mock()
        with self.assertRaises(ControlError):
            other.reset("12345", self.events)
        other.request.assert_not_called()

    def test_changed_alarm_foreign_device_status_or_code_cannot_reset(self):
        for key, value in (("deviceId", "foreign-device"), ("status", "ResetByUserFromCloud"),
                           ("alarmNumber", 999), ("id", "2147483648")):
            with self.subTest(key=key):
                self.alarm = {**self.alarm, key: value}
                with self.assertRaises(ControlError):
                    self.client.reset("12345", self.events)
                self.alarm = {"id": "12345", "alarmNumber": 229, "deviceId": "selected-device", "status": "Active"}
        self.assertFalse(any(p.endswith("/reset") for p, _, _ in self.sent))

    def test_pending_concurrent_operation_does_not_send_second_reset(self):
        self.client.lock.acquire()
        try:
            with self.assertRaisesRegex(ControlError, "pågår"):
                self.client.reset("12345", self.events)
        finally:
            self.client.lock.release()
        self.client.request.assert_not_called()

    def test_timeout_does_not_retry_or_claim_success(self):
        def response(path, data=None, token=None):
            if path.endswith("/reset"):
                raise ControlError("timeout; kan ha skickats")
            return self.response(path, data, token)
        self.client.request.side_effect = response
        with self.assertRaisesRegex(ControlError, "timeout"):
            self.client.reset("12345", self.events)
        self.assertEqual(len([c for c in self.client.request.call_args_list if c.args[0].endswith("/reset")]), 1)
        with self.assertRaisesRegex(ControlError, "två minuter"):
            self.client.reset("12345", self.events)

    def test_failed_device_result_and_log_failure_are_not_success(self):
        def response(path, data=None, token=None):
            return {"status": 500} if path.endswith("/reset") else self.response(path, data, token)
        self.client.request.side_effect = response
        with self.assertRaisesRegex(ControlError, "inte en lyckad"):
            self.client.reset("12345", self.events)
        self.assertNotIn("alarm_reset_sent", [e["kind"] for e in self.events.recent()])
        self.client.request.reset_mock()
        log = Mock()
        log.history.return_value = []
        log.add.side_effect = OSError("disk full")
        with self.assertRaises(OSError):
            self.client.reset("12345", log)
        self.assertFalse(any(c.args[0].endswith("/reset") for c in self.client.request.call_args_list))

    def test_cache_is_cleared_before_revalidation_and_invalid_data_is_not_empty_success(self):
        self.client.report()
        self.client.report()
        self.assertEqual(self.client.request.call_count, 2)
        self.client.cached["checked_at"] = 0
        self.client.request.return_value = None
        self.client.request.side_effect = None
        with self.assertRaises(ControlError):
            self.client.report()
        self.assertIsNone(self.client.cached)

    def test_bounded_transport_never_exposes_credentials_or_follows_redirects(self):
        client = MyUplink({})
        client.opener = Mock()
        for error in (URLError("secret detail"), TimeoutError("secret detail"),
                      HTTPError("url", 403, "secret detail", {}, None)):
            client.opener.open.side_effect = error
            with self.assertRaises(ControlError) as caught:
                client.request("/oauth/token", b"secret")
            self.assertNotIn("secret", str(caught.exception))
        self.assertIsNone(NoRedirects().redirect_request(None, None, 302, "", {}, "https://elsewhere"))

    def test_duplicate_or_malformed_rows_rejected(self):
        self.client.request.side_effect = lambda *args, **kwargs: (
            {"access_token": "token", "expires_in": 3600} if args[0] == "/oauth/token"
            else {"notifications": [self.alarm, self.alarm], "numItems": 2})
        with self.assertRaisesRegex(ControlError, "dubbla"):
            self.client.report()

    def test_nullable_empty_list_and_invalid_token_expiry(self):
        self.client.request.side_effect = lambda *args, **kwargs: (
            {"access_token": "token", "expires_in": 3600} if args[0] == "/oauth/token"
            else {"notifications": None, "numItems": 0})
        self.assertEqual(self.client.report()["alarms"], [])
        self.client.token = None
        self.client.request.side_effect = None
        self.client.request.return_value = {"access_token": "token", "expires_in": float("nan")}
        with self.assertRaisesRegex(ControlError, "tokensvar"):
            self.client.access_token()

    def test_excessive_list_is_rejected_before_reset(self):
        self.client.request.side_effect = lambda *args, **kwargs: (
            {"access_token": "token", "expires_in": 3600} if args[0] == "/oauth/token"
            else {"notifications": [self.alarm], "numItems": 1001})
        with self.assertRaises(ControlError):
            self.client.reset("12345", self.events)
        self.assertFalse(any(c.args[0].endswith("/reset") for c in self.client.request.call_args_list))
