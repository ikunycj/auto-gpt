# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, patch

from core import cloudmail_client


class CloudMailClientTests(unittest.TestCase):
    def setUp(self):
        cloudmail_client._CONTEXT_CACHE.clear()
        cloudmail_client._DOMAIN_CACHE = None

    def test_pick_account_requires_domains(self):
        with patch.object(cloudmail_client._email_cfg, "CLOUDMAIL_API_BASE", "https://mail.example.com", create=True), patch.object(
            cloudmail_client._email_cfg, "CLOUDMAIL_AUTH_TOKEN", "token", create=True
        ), patch.object(cloudmail_client._email_cfg, "CLOUDMAIL_DOMAINS", [], create=True), patch(
            "core.cloudmail_client.fetch_domains",
            side_effect=cloudmail_client.CloudMailError("CloudMail 自动获取域名失败：域名列表为空"),
        ):
            with self.assertRaisesRegex(cloudmail_client.CloudMailError, "自动获取域名失败"):
                cloudmail_client.pick_account()

    @patch("core.cloudmail_client.requests.get")
    def test_fetch_domains_reads_website_config(self, get):
        response = Mock(status_code=200)
        response.json.return_value = {"code": 200, "data": {"domainList": ["@example.com", "@mail.example.net"]}}
        get.return_value = response

        with patch.object(cloudmail_client._email_cfg, "CLOUDMAIL_API_BASE", "https://mail.example.com", create=True):
            domains = cloudmail_client.fetch_domains(force=True)

        self.assertEqual(domains, ["example.com", "mail.example.net"])
        get.assert_called_once()

    @patch("core.cloudmail_client.requests.post")
    @patch("core.cloudmail_client.secrets.choice", side_effect=list("bcdefghijklm"))
    @patch("core.cloudmail_client.random.choice")
    def test_pick_account_generates_random_domain_email_and_adds_user(self, rand_choice, sec_choice, post):
        rand_choice.side_effect = ["example.com", "a"]
        response = Mock(status_code=200)
        response.json.return_value = {"code": 200, "data": None}
        post.return_value = response

        with patch.object(cloudmail_client._email_cfg, "CLOUDMAIL_API_BASE", "https://mail.example.com", create=True), patch.object(
            cloudmail_client._email_cfg, "CLOUDMAIL_AUTH_TOKEN", "token-123", create=True
        ), patch.object(cloudmail_client._email_cfg, "CLOUDMAIL_DOMAINS", ["example.com"], create=True), patch.object(
            cloudmail_client._email_cfg, "CLOUDMAIL_AUTO_ADD_USER", True, create=True
        ), patch.object(cloudmail_client._email_cfg, "CLOUDMAIL_RANDOM_LOCAL_LENGTH", 6, create=True):
            account = cloudmail_client.pick_account()

        self.assertEqual(account.email, "abcdef@example.com")
        self.assertIs(cloudmail_client.get_account_context("abcdef@example.com"), account)
        post.assert_called_once()
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], "token-123")
        self.assertEqual(kwargs["json"]["list"][0]["email"], "abcdef@example.com")

    @patch("core.cloudmail_client.time.sleep")
    @patch("core.cloudmail_client.requests.post")
    def test_fetch_latest_otp_reads_openai_mail(self, post, sleep):
        response = Mock(status_code=200)
        response.json.return_value = {
            "code": 200,
            "data": [{
                "emailId": "1",
                "sendEmail": "noreply@openai.com",
                "subject": "Your code is 654321",
                "content": "Your verification code is 654321",
                "createTime": "2026-07-15 04:00:10",
            }],
        }
        post.return_value = response

        with patch.object(cloudmail_client._email_cfg, "CLOUDMAIL_API_BASE", "https://mail.example.com", create=True), patch.object(
            cloudmail_client._email_cfg, "CLOUDMAIL_AUTH_TOKEN", "token-123", create=True
        ):
            code = cloudmail_client.fetch_latest_otp(
                "abcdef@example.com",
                after_ts=0,
                max_wait=1,
                poll_interval=1,
                settle_seconds=0,
            )

        self.assertEqual(code, "654321")
        self.assertEqual(post.call_args.kwargs["json"]["toEmail"], "abcdef@example.com")

    @patch("core.cloudmail_client.requests.post")
    def test_gen_token_extracts_token(self, post):
        response = Mock(status_code=200)
        response.json.return_value = {"code": 200, "data": {"token": "token-auto"}}
        post.return_value = response

        token = cloudmail_client.gen_token(
            email="user",
            password="pass",
            base_url="https://mail.example.com",
            path="/api/public/genToken",
        )

        self.assertEqual(token, "token-auto")
        post.assert_called_once_with(
            "https://mail.example.com/api/public/genToken",
            json={"email": "user", "password": "pass"},
            headers={"Accept": "application/json"},
            timeout=cloudmail_client.REQUEST_TIMEOUT,
        )


if __name__ == "__main__":
    unittest.main()
