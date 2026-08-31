# -*- coding: utf-8 -*-
import unittest
from pathlib import Path

from apps.web.app import create_app


class WebUiAccessTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()

    def test_api_is_available_without_authentication(self):
        response = self.client.get("/api/summary")

        self.assertEqual(response.status_code, 200)
        self.assertIn("accounts", response.get_json())

    def test_auth_headers_do_not_gate_api_access(self):
        response = self.client.get(
            "/api/summary",
            headers={"X-Auth-Code": "invalid", "Authorization": "Bearer invalid"},
        )

        self.assertEqual(response.status_code, 200)

    def test_login_and_logout_routes_are_removed(self):
        self.assertEqual(self.client.get("/login").status_code, 404)
        self.assertEqual(self.client.post("/logout").status_code, 404)

    def test_auth_settings_are_not_exposed(self):
        response = self.client.get("/api/config")
        keys = {field.get("key") for field in response.get_json() if isinstance(field, dict)}

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("WEBUI_AUTH_CODE", keys)
        self.assertNotIn("WEBUI_SESSION_SECRET", keys)

    def test_root_serves_react_bundle_without_authentication(self):
        response = self.client.get("/")
        dist_index = Path(__file__).resolve().parents[1] / "web" / "dist" / "index.html"
        if dist_index.is_file():
            self.assertEqual(response.status_code, 200)
            self.assertIn("ChatGPT 注册机", response.get_data(as_text=True))
            self.assertIn("/assets/index-", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
