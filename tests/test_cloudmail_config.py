# -*- coding: utf-8 -*-
import unittest
from pathlib import Path

from config import email
from config.env_loader import SECRET_ENV_KEYS
from apps.web.config_editor import EDITABLE_FIELDS


class CloudMailConfigTests(unittest.TestCase):
    def test_email_config_declares_cloudmail_defaults(self):
        source = Path(email.__file__).read_text(encoding="utf-8")
        self.assertIn('CLOUDMAIL_AUTH_TOKEN = env_str("CLOUDMAIL_AUTH_TOKEN", "")', source)
        self.assertIn('CLOUDMAIL_ADMIN_EMAIL = env_str("CLOUDMAIL_ADMIN_EMAIL", "")', source)
        self.assertIn('CLOUDMAIL_PASSWORD = env_str("CLOUDMAIL_PASSWORD", "")', source)
        self.assertIn('CLOUDMAIL_DOMAINS = []', source)
        self.assertIn('"cloudmail"', source)

    def test_secret_registry_includes_cloudmail_token(self):
        self.assertEqual(SECRET_ENV_KEYS["CLOUDMAIL_AUTH_TOKEN"], "CloudMail Authorization Token")
        self.assertEqual(SECRET_ENV_KEYS["CLOUDMAIL_PASSWORD"], "CloudMail 登录密码")

    def test_webui_exposes_cloudmail_fields(self):
        keys = {item["key"]: item for item in EDITABLE_FIELDS}
        self.assertEqual(keys["CLOUDMAIL_ADMIN_EMAIL"]["storage"], "env")
        self.assertTrue(keys["CLOUDMAIL_PASSWORD"]["secret"])
        self.assertEqual(keys["CLOUDMAIL_AUTH_TOKEN"]["storage"], "env")
        self.assertTrue(keys["CLOUDMAIL_AUTH_TOKEN"]["secret"])
        self.assertEqual(keys["CLOUDMAIL_DOMAINS"]["type"], "list_str_multiline")
        self.assertEqual(keys["CLOUDMAIL_AUTO_ADD_USER"]["type"], "bool")


if __name__ == "__main__":
    unittest.main()
