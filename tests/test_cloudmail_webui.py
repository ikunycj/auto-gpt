# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from apps.web.app import create_app


class CloudMailWebUiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("config.reload_all")
    @patch("config.env_loader.write_env_values")
    @patch("core.cloudmail_client.requests.post")
    def test_gen_token_saves_current_cloudmail_config(self, post, write_env_values, reload_all):
        response = post.return_value
        response.status_code = 200
        response.json.return_value = {"code": 200, "data": {"token": "token-abc"}}
        write_env_values.return_value = ["CLOUDMAIL_AUTH_TOKEN", "CLOUDMAIL_API_BASE"]

        r = self.client.post("/api/cloudmail/gen-token", json={
            "api_base": "https://mail.example.com",
            "admin_email": "admin@example.com",
            "password": "pass-123",
            "path": "/api/public/genToken",
        })

        self.assertEqual(r.status_code, 200)
        updates = write_env_values.call_args.args[0]
        self.assertEqual(updates["CLOUDMAIL_AUTH_TOKEN"], "token-abc")
        self.assertEqual(updates["CLOUDMAIL_API_BASE"], "https://mail.example.com")
        self.assertEqual(updates["CLOUDMAIL_ADMIN_EMAIL"], "admin@example.com")
        self.assertEqual(updates["CLOUDMAIL_PASSWORD"], "pass-123")
        self.assertEqual(updates["CLOUDMAIL_TOKEN_PATH"], "/api/public/genToken")


if __name__ == "__main__":
    unittest.main()
