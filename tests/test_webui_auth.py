# -*- coding: utf-8 -*-
import unittest
from pathlib import Path

from apps.web.app import create_app


class WebUiAuthTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(auth_code="test-auth")
        self.client = self.app.test_client()

    def test_api_requires_auth_code(self):
        r = self.client.get("/api/summary")
        self.assertEqual(r.status_code, 401)
        self.assertIn("未授权", r.get_json()["error"])

    def test_api_accepts_auth_header(self):
        r = self.client.get("/api/summary", headers={"X-Auth-Code": "test-auth"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("accounts", r.get_json())


    def test_query_auth_code_is_not_accepted(self):
        r = self.client.get("/api/summary?auth_code=test-auth")
        self.assertEqual(r.status_code, 401)

    def test_json_body_auth_code_is_not_accepted(self):
        r = self.client.post("/api/jobs/cancel-pending", json={"auth_code": "test-auth"})
        self.assertEqual(r.status_code, 401)

    def test_login_remember_sets_persistent_session(self):
        r = self.client.post("/login", data={"auth_code": "test-auth", "next": "/", "remember": "1"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("Expires=", r.headers.get("Set-Cookie") or "")

    def test_login_sets_session_cookie(self):
        r = self.client.post("/login", data={"auth_code": "test-auth", "next": "/"})
        self.assertEqual(r.status_code, 302)
        r = self.client.get("/api/summary")
        self.assertEqual(r.status_code, 200)

    def test_root_serves_react_bundle(self):
        r = self.client.get("/", headers={"X-Auth-Code": "test-auth"})
        dist_index = Path(__file__).resolve().parents[1] / "web" / "dist" / "index.html"
        if dist_index.is_file():
            self.assertEqual(r.status_code, 200)
            self.assertIn("ChatGPT 注册机", r.get_data(as_text=True))
            self.assertIn("/assets/index-", r.get_data(as_text=True))

    def test_login_serves_react_entry_when_bundle_exists(self):
        dist_index = Path(__file__).resolve().parents[1] / "web" / "dist" / "index.html"
        if dist_index.is_file():
            r = self.client.get("/login")
            self.assertEqual(r.status_code, 200)
            self.assertIn("ChatGPT 注册机", r.get_data(as_text=True))
            bad = self.client.post("/login?next=%2F", data={"auth_code": "wrong"})
            self.assertEqual(bad.status_code, 302)
            self.assertIn("error=invalid", bad.headers.get("Location", ""))


if __name__ == "__main__":
    unittest.main()
