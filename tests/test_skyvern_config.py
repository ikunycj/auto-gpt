# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import env_loader
from apps.web import config_editor
from core.skyvern_client import SkyvernClient


class SkyvernConfigTests(unittest.TestCase):
    def test_secret_registry_includes_skyvern_api_key(self):
        self.assertIn("SKYVERN_API_KEY", env_loader.SECRET_ENV_KEYS)

    def test_webui_exposes_skyvern_fields(self):
        fields = {f["key"]: f for f in config_editor.EDITABLE_FIELDS}
        self.assertIn("SKYVERN_API_KEY", fields)
        self.assertTrue(fields["SKYVERN_API_KEY"].get("secret"))
        self.assertEqual(fields["SKYVERN_API_KEY"].get("storage"), "env")
        self.assertIn("SKYVERN_BROWSER_SESSION_TIMEOUT", fields)

    def test_cdp_headers_include_api_key(self):
        headers = SkyvernClient(api_key="key-123").cdp_headers()
        self.assertEqual(headers["x-api-key"], "key-123")
        self.assertEqual(headers["Authorization"], "Bearer key-123")

    def test_skyvern_normalizes_legacy_webui_values(self):
        self.assertEqual(SkyvernClient._normalize_proxy_location("jp"), "RESIDENTIAL_JP")
        self.assertEqual(SkyvernClient._normalize_proxy_location("gb"), "RESIDENTIAL_GB")
        self.assertEqual(SkyvernClient._normalize_browser_type("chromium-headful"), "stealth-chromium")
        self.assertEqual(SkyvernClient._normalize_browser_type("edge"), "msedge")

    @patch("core.skyvern_client.requests.get")
    @patch("core.skyvern_client.requests.post")
    def test_open_session_reads_browser_address_from_get_session(self, post, get):
        post.return_value.status_code = 200
        post.return_value.json.return_value = {"browser_session_id": "bs_123"}
        get.return_value.status_code = 200
        get.return_value.json.return_value = {"browser_session_id": "bs_123", "browser_address": "http://127.0.0.1:9222"}

        client = SkyvernClient(api_key="key-123", api_base="https://api.example.test")
        session = client.open_session()

        self.assertEqual(session.session_id, "bs_123")
        self.assertEqual(session.connect_url, "http://127.0.0.1:9222")
        post.assert_called_once()
        get.assert_called_once()


if __name__ == "__main__":
    unittest.main()
