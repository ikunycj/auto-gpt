# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import codex as codex_config
from config import env_loader
from core import sms_provider
from apps.web import config_editor


class _Response:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


class _Http:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        return self.responses.pop(0)


class FixedUrlSmsProviderTests(unittest.TestCase):
    def test_config_is_registered_as_secret_webui_fields(self):
        self.assertIn("FIXED_SMS_CODE_URL", env_loader.SECRET_ENV_KEYS)
        fields = {field["key"]: field for field in config_editor.EDITABLE_FIELDS}
        self.assertTrue(fields["FIXED_SMS_PHONE"].get("secret"))
        self.assertTrue(fields["FIXED_SMS_CODE_URL"].get("secret"))

    def test_acquire_and_poll_fixed_url(self):
        http = _Http([_Response("OpenAI 验证码：123456")])
        with patch.object(codex_config, "SMS_PROVIDER", "fixed_url"), \
             patch.object(codex_config, "FIXED_SMS_PHONE", "+14155550123"), \
             patch.object(codex_config, "FIXED_SMS_CODE_URL", "https://sms.example/code"):
            activation_id, phone = sms_provider.acquire_number(http=http)
            self.assertEqual(sms_provider.set_status(activation_id, 1, http=http), "OK")
            code = sms_provider.wait_for_sms_code(activation_id, http=http, max_wait=1, poll_interval=0)
            sms_provider.complete(activation_id, http=http)

        self.assertTrue(activation_id.startswith("fixed-"))
        self.assertEqual(phone, "14155550123")
        self.assertEqual(code, "123456")
        self.assertEqual(http.calls, ["https://sms.example/code"])

    def test_fixed_url_requires_http_url(self):
        with patch.object(codex_config, "SMS_PROVIDER", "fixed_url"), \
             patch.object(codex_config, "FIXED_SMS_PHONE", "+14155550123"), \
             patch.object(codex_config, "FIXED_SMS_CODE_URL", "not-a-url"):
            with self.assertRaisesRegex(sms_provider.SmsProviderError, "http"):
                sms_provider.acquire_number(http=_Http([]))

    def test_fixed_url_can_acquire_material_lazily(self):
        http = _Http([_Response("验证码 654321")])
        calls = []
        with patch.object(codex_config, "SMS_PROVIDER", "fixed_url"), \
             sms_provider.fixed_sms_context(
                 acquire_provider=lambda: calls.append("acquire") or {
                     "activation_id": "job-phone",
                     "phone": "+14155550123",
                     "code_url": "https://sms.example/lazy",
                 },
             ):
            activation_id, phone = sms_provider.acquire_number(http=http)
            code = sms_provider.wait_for_sms_code(activation_id, http=http, max_wait=1, poll_interval=0)

        self.assertEqual(calls, ["acquire"])
        self.assertEqual(activation_id, "job-phone")
        self.assertEqual(phone, "14155550123")
        self.assertEqual(code, "654321")

    def test_fixed_url_calls_success_and_failure_hooks(self):
        successes = []
        failures = []
        with patch.object(codex_config, "SMS_PROVIDER", "fixed_url"), \
             sms_provider.fixed_sms_context(
                 phone="+14155550123",
                 code_url="https://sms.example/code",
                 success_provider=lambda: successes.append(True),
                 failure_provider=lambda activation_id: failures.append(activation_id),
             ):
            activation_id, _phone = sms_provider.acquire_number(http=_Http([]))
            sms_provider.complete(activation_id, http=_Http([]))
            failed_id, _phone = sms_provider.acquire_number(http=_Http([]))
            sms_provider.cancel(failed_id, http=_Http([]), background=False)

        self.assertEqual(successes, [True])
        self.assertEqual(failures, [failed_id])


if __name__ == "__main__":
    unittest.main()
