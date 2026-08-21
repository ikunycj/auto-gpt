# -*- coding: utf-8 -*-
import unittest
from pathlib import Path

from config import email
from config.env_loader import SECRET_ENV_KEYS
from apps.web.config_editor import EDITABLE_FIELDS


class MailNestConfigTests(unittest.TestCase):
    def test_email_config_declares_mailnest_defaults(self):
        source = Path(email.__file__).read_text(encoding="utf-8")
        self.assertIn('MAIL_NEST_API_KEY = env_str("MAIL_NEST_API_KEY", "")', source)
        self.assertIn('MAIL_NEST_PROJECT_CODE = "chatgpt001"', source)
        self.assertIn('"mailnest"', source)

    def test_secret_registry_includes_mailnest_api_key(self):
        self.assertEqual(SECRET_ENV_KEYS["MAIL_NEST_API_KEY"], "MailNest API Key")

    def test_webui_exposes_mailnest_fields(self):
        key_field = next(item for item in EDITABLE_FIELDS if item["key"] == "MAIL_NEST_API_KEY")
        self.assertEqual(key_field["group"], "邮箱 / OTP")
        self.assertTrue(key_field["secret"])
        self.assertEqual(key_field["storage"], "env")
        project_field = next(item for item in EDITABLE_FIELDS if item["key"] == "MAIL_NEST_PROJECT_CODE")
        self.assertEqual(project_field["type"], "str")


if __name__ == "__main__":
    unittest.main()
