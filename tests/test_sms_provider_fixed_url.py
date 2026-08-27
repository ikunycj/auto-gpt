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
    def test_legacy_fixed_config_is_redacted_but_not_exposed_in_webui(self):
        self.assertNotIn("FIXED_SMS_PHONE", env_loader.SECRET_ENV_KEYS)
        self.assertNotIn("FIXED_SMS_CODE_URL", env_loader.SECRET_ENV_KEYS)
        fields = {field["key"]: field for field in config_editor.EDITABLE_FIELDS}
        self.assertNotIn("FIXED_SMS_PHONE", fields)
        self.assertNotIn("FIXED_SMS_CODE_URL", fields)

    def test_global_fixed_url_config_is_rejected_even_when_legacy_values_exist(self):
        http = _Http([])
        with patch.object(codex_config, "SMS_PROVIDER", "fixed_url"), \
             patch.object(codex_config, "FIXED_SMS_PHONE", "+14155550123", create=True), \
             patch.object(codex_config, "FIXED_SMS_CODE_URL", "https://sms.example/code", create=True):
            with self.assertRaisesRegex(sms_provider.SmsProviderError, "手机号池"):
                sms_provider.acquire_number(http=http)

        self.assertEqual(http.calls, [])

    def test_phone_pool_context_requires_http_url(self):
        with patch.object(codex_config, "SMS_PROVIDER", "fixed_url"), \
             self.assertRaisesRegex(sms_provider.SmsProviderError, "http"):
            with sms_provider.fixed_sms_context(phone="+14155550123", code_url="not-a-url"):
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
