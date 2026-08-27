# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import sms_provider
from config import codex as codex_config
from config import env_loader
from apps.web import config_editor


class _Resp:
    status_code = 200
    text = "{}"

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _Http:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def post(self, url, headers=None, data=None):
        self.calls.append({"url": url, "headers": headers or {}, "data": data})
        return _Resp(self.responses.pop(0))

    def close(self):
        self.closed = True


class HSmsProviderTests(unittest.TestCase):
    def test_secret_registry_and_webui_fields_include_h(self):
        self.assertIn("H_ADMIN_AUTH_CODE", env_loader.SECRET_ENV_KEYS)
        fields = {f["key"]: f for f in config_editor.EDITABLE_FIELDS}
        self.assertIn("H_API_BASE", fields)
        self.assertIn("H_PHONE_ACQUIRE_MODE", fields)
        self.assertTrue(fields["H_ADMIN_AUTH_CODE"].get("secret"))

    def test_acquire_number_uses_h_take_reusable_phone(self):
        http = _Http([{"item": {"id": "hid-1", "phone": "2025550123"}, "reused": True, "duplicate": False}])
        with patch.object(codex_config, "SMS_PROVIDER", "h"), patch.object(codex_config, "H_API_BASE", "http://localhost:8788"), patch.object(codex_config, "H_ADMIN_AUTH_CODE", "adm"), patch.object(codex_config, "SMS_SERVICE", "12345"), patch.object(codex_config, "SMS_COUNTRY", "us"), patch.object(codex_config, "H_PHONE_PREFIX", "1"), patch.object(codex_config, "H_PHONE_ACQUIRE_MODE", "reusable"):
            activation_id, phone = sms_provider.acquire_number(http=http)

        self.assertEqual(activation_id, "hid-1")
        self.assertEqual(phone, "12025550123")
        self.assertTrue(http.calls[0]["url"].endswith("/api/admin/h/take-reusable-phone"))
        self.assertIn('"projectId": "12345"', http.calls[0]["data"])
        self.assertIn('"country": "us"', http.calls[0]["data"])
        self.assertEqual(http.calls[0]["headers"]["Authorization"], "Bearer adm")

    def test_acquire_number_uses_h_take_phone_when_mode_new(self):
        http = _Http([{"item": {"id": "hid-2", "phone": "2025550456"}, "reused": False, "duplicate": False}])
        with patch.object(codex_config, "SMS_PROVIDER", "h"), patch.object(codex_config, "H_API_BASE", "http://localhost:8788"), patch.object(codex_config, "H_ADMIN_AUTH_CODE", "adm"), patch.object(codex_config, "SMS_SERVICE", "12345"), patch.object(codex_config, "SMS_COUNTRY", "us"), patch.object(codex_config, "H_PHONE_PREFIX", "1"), patch.object(codex_config, "H_PHONE_ACQUIRE_MODE", "new"):
            activation_id, phone = sms_provider.acquire_number(http=http)

        self.assertEqual(activation_id, "hid-2")
        self.assertEqual(phone, "12025550456")
        self.assertTrue(http.calls[0]["url"].endswith("/api/admin/h/take-phone"))
        self.assertIn('"projectId": "12345"', http.calls[0]["data"])
        self.assertIn('"country": "us"', http.calls[0]["data"])

    def test_wait_for_sms_code_uses_h_fetch_code(self):
        http = _Http([{"item": {"id": "hid-1", "status": "code_received"}, "code": "123456"}])
        with patch.object(codex_config, "SMS_PROVIDER", "h"), patch.object(codex_config, "H_API_BASE", "http://localhost:8788"), patch.object(codex_config, "H_ADMIN_AUTH_CODE", "adm"):
            code = sms_provider.wait_for_sms_code("hid-1", http=http, max_wait=1, poll_interval=0)

        self.assertEqual(code, "123456")
        self.assertTrue(http.calls[0]["url"].endswith("/api/admin/h/fetch-code"))
        self.assertIn('"id": "hid-1"', http.calls[0]["data"])

    def test_set_status_does_not_call_grizzly_for_h(self):
        http = _Http([])
        with patch.object(codex_config, "SMS_PROVIDER", "h"), patch.object(codex_config, "H_API_BASE", "http://localhost:8788"), patch.object(codex_config, "H_ADMIN_AUTH_CODE", "adm"), patch.object(codex_config, "SMS_API_BASE", "https://grizzly.example/handler"):
            result = sms_provider.set_status("hid-1", 1, http=http)

        self.assertEqual(result, "OK")
        self.assertEqual(http.calls, [])

    def test_cancel_uses_h_release(self):
        http = _Http([{"released": 1, "failed": []}])
        with patch.object(codex_config, "SMS_PROVIDER", "h"), patch.object(codex_config, "H_API_BASE", "http://localhost:8788"), patch.object(codex_config, "H_ADMIN_AUTH_CODE", "adm"):
            sms_provider.cancel("hid-1", http=http)

        self.assertTrue(http.calls[0]["url"].endswith("/api/admin/h/release"))
        self.assertIn('"id": "hid-1"', http.calls[0]["data"])


if __name__ == "__main__":
    unittest.main()
