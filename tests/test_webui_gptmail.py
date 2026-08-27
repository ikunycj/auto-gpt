# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import email as email_config
from config import register as register_config
from apps.web.app import create_app


class GPTMailWebUiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("apps.web.app.svc.submit_registration")
    def test_jobs_rejects_gptmail_without_api_key_before_creating_tasks(self, submit_registration):
        submit_registration.return_value = []
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "gptmail"
        ), patch.object(email_config, "GPTMAIL_API_KEY", ""):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 400)
        self.assertIn("请填写 GPTMail API Key", response.get_json()["error"])
        submit_registration.assert_not_called()

    @patch("apps.web.app.db.outlook_pool_summary")
    @patch("apps.web.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_jobs_with_gptmail_key_does_not_check_outlook_pool(self, submit_registration, outlook_pool_summary):
        outlook_pool_summary.return_value = {"total": 0, "available": 0, "used": 0, "failed": 0}
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "gptmail"
        ), patch.object(email_config, "GPTMAIL_API_KEY", "key-123"):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["warning"], "")
        outlook_pool_summary.assert_not_called()
        submit_registration.assert_called_once_with(count=1, workers=1)

    @patch("apps.web.app.db.domain_email_pool_summary", return_value={"total": 0, "available": 0, "used": 0, "failed": 0})
    @patch("apps.web.app.db.outlook_pool_summary")
    @patch("apps.web.app.db.count_accounts", return_value=0)
    def test_summary_does_not_count_gptmail_as_outlook_pool(self, count_accounts, outlook_pool_summary, domain_pool_summary):
        outlook_pool_summary.return_value = {"total": 0, "available": 0, "used": 0, "failed": 0}
        with patch.object(email_config, "EMAIL_SOURCE", "gptmail"):
            response = self.client.get("/api/summary")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["outlook_total"], 0)
        self.assertIn("outlook", [item["id"] for item in payload["registration_email"]["all_channels"]])
        outlook_pool_summary.assert_called_once()

    @patch("apps.web.app.db.domain_email_pool_summary", return_value={"total": 0, "available": 0, "used": 0, "failed": 0})
    def test_summary_exposes_safe_mailnest_runtime_status(self, domain_pool_summary):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "mailnest"
        ), patch.object(email_config, "MAIL_NEST_API_KEY", "key-123", create=True), patch.object(
            email_config, "MAIL_NEST_PROJECT_CODE", "chatgpt001", create=True
        ):
            response = self.client.get("/api/summary")

        self.assertEqual(response.status_code, 200)
        runtime = response.get_json()["registration_email"]
        self.assertEqual(runtime["sources"], ["mailnest"])
        self.assertTrue(runtime["automatic"])
        self.assertTrue(runtime["mailnest_configured"])
        self.assertNotIn("key-123", response.get_data(as_text=True))

    @patch("apps.web.app.svc.submit_registration")
    def test_jobs_rejects_mailnest_without_api_key_before_creating_tasks(self, submit_registration):
        submit_registration.return_value = []
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "mailnest"
        ), patch.object(email_config, "MAIL_NEST_API_KEY", "", create=True), patch.object(
            email_config, "MAIL_NEST_PROJECT_CODE", "chatgpt001", create=True
        ):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 400)
        self.assertIn("MailNest API Key", response.get_json()["error"])
        submit_registration.assert_not_called()

    @patch("apps.web.app.db.outlook_pool_summary")
    @patch("apps.web.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_jobs_with_mailnest_key_does_not_check_outlook_pool(self, submit_registration, outlook_pool_summary):
        outlook_pool_summary.return_value = {"total": 0, "available": 0, "used": 0, "failed": 0}
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "mailnest"
        ), patch.object(email_config, "MAIL_NEST_API_KEY", "key-123", create=True), patch.object(
            email_config, "MAIL_NEST_PROJECT_CODE", "chatgpt001", create=True
        ):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["warning"], "")
        outlook_pool_summary.assert_not_called()
        submit_registration.assert_called_once_with(count=1, workers=1)

    @patch("apps.web.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_jobs_skip_unready_source_and_submit_only_ready_fallback(self, submit_registration):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "gptmail,mailnest"
        ), patch.object(email_config, "GPTMAIL_API_KEY", ""), patch.object(
            email_config, "MAIL_NEST_API_KEY", "key-123", create=True
        ), patch.object(email_config, "MAIL_NEST_PROJECT_CODE", "chatgpt001", create=True):
            response = self.client.post("/api/jobs", json={"count": 2, "workers": 1})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["usable_sources"], ["mailnest"])
        self.assertIn("已跳过未就绪渠道", payload["warning"])
        self.assertIn("GPTMail API Key", payload["warning"])
        submit_registration.assert_called_once_with(
            count=2,
            workers=1,
            email_source="mailnest",
        )

    @patch("apps.web.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_jobs_use_task_scoped_channel_order_even_in_global_manual_mode(self, submit_registration):
        with patch.object(email_config, "USE_EMAIL_SERVICE", False), patch.object(
            register_config, "REGISTER_EMAIL", ""
        ), patch.object(email_config, "EMAIL_SOURCE", "outlook"), patch.object(
            email_config, "MAIL_NEST_API_KEY", "key-123", create=True
        ), patch.object(email_config, "MAIL_NEST_PROJECT_CODE", "chatgpt001", create=True), patch.object(
            email_config, "GPTMAIL_API_KEY", "gpt-key", create=True
        ):
            response = self.client.post("/api/jobs", json={
                "count": 2,
                "workers": 1,
                "email_sources": ["mailnest", "gptmail"],
            })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["usable_sources"], ["mailnest", "gptmail"])
        submit_registration.assert_called_once_with(
            count=2,
            workers=1,
            email_source="mailnest,gptmail",
        )

    @patch("apps.web.app.svc.submit_registration")
    def test_jobs_reject_unready_task_scoped_channel_instead_of_silently_skipping(self, submit_registration):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "GPTMAIL_API_KEY", "", create=True
        ), patch.object(email_config, "MAIL_NEST_API_KEY", "key-123", create=True), patch.object(
            email_config, "MAIL_NEST_PROJECT_CODE", "chatgpt001", create=True
        ):
            response = self.client.post("/api/jobs", json={
                "count": 1,
                "workers": 1,
                "email_sources": ["mailnest", "gptmail"],
            })

        self.assertEqual(response.status_code, 400)
        self.assertIn("本次选择中有不可用", response.get_json()["error"])
        self.assertIn("GPTMail API Key", response.get_json()["error"])
        submit_registration.assert_not_called()

    @patch("apps.web.app.svc.submit_registration")
    def test_jobs_reject_unknown_task_scoped_channel(self, submit_registration):
        response = self.client.post("/api/jobs", json={
            "count": 1,
            "workers": 1,
            "email_sources": ["mailnest", "not-a-provider"],
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn("不支持的邮箱渠道", response.get_json()["error"])
        submit_registration.assert_not_called()

    @patch("apps.web.app.db.generic_api_email_pool_summary")
    @patch("apps.web.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_jobs_use_imported_generic_api_pool(self, submit_registration, pool_summary):
        pool_summary.return_value = {"total": 3, "available": 2, "used": 1, "failed": 0}
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "generic_api"
        ):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["warning"], "")
        submit_registration.assert_called_once_with(count=1, workers=1)

    @patch("apps.web.app.db.domain_email_pool_summary", return_value={"total": 0, "available": 0, "used": 0, "failed": 0})
    def test_summary_exposes_status_for_every_selected_source_without_secrets(self, domain_pool_summary):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "gptmail,mailnest"
        ), patch.object(email_config, "GPTMAIL_API_KEY", ""), patch.object(
            email_config, "MAIL_NEST_API_KEY", "private-key", create=True
        ), patch.object(email_config, "MAIL_NEST_PROJECT_CODE", "chatgpt001", create=True):
            response = self.client.get("/api/summary")

        self.assertEqual(response.status_code, 200)
        runtime = response.get_json()["registration_email"]
        self.assertEqual(runtime["usable_sources"], ["mailnest"])
        self.assertFalse(runtime["channels"][0]["ready"])
        self.assertTrue(runtime["channels"][1]["ready"])
        self.assertNotIn("private-key", response.get_data(as_text=True))

    @patch("apps.web.app.db.domain_email_pool_summary", return_value={"total": 0, "available": 0, "used": 0, "failed": 0})
    def test_summary_exposes_manual_registration_readiness_without_email(self, domain_pool_summary):
        with patch.object(email_config, "USE_EMAIL_SERVICE", False), patch.object(
            register_config, "REGISTER_EMAIL", "private@example.com"
        ):
            response = self.client.get("/api/summary")

        self.assertEqual(response.status_code, 200)
        runtime = response.get_json()["registration_email"]
        self.assertFalse(runtime["automatic"])
        self.assertTrue(runtime["manual_configured"])
        self.assertTrue(runtime["ready"])
        self.assertNotIn("private@example.com", response.get_data(as_text=True))

    @patch("core.manual_otp.list_waiting", return_value=[{"email": "wait@example.com", "job_id": 7, "since": 1.0}])
    def test_manual_otp_waiting_is_available_to_gpt_accounts_page(self, list_waiting):
        response = self.client.get("/api/manual-otp/waiting")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["waiting"][0]["job_id"], 7)

    @patch("apps.web.app.svc.submit_registration")
    def test_jobs_allows_cloudmail_without_manual_domains(self, submit_registration):
        submit_registration.return_value = []
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "cloudmail"
        ), patch.object(email_config, "CLOUDMAIL_API_BASE", "https://mail.example.com", create=True), patch.object(
            email_config, "CLOUDMAIL_AUTH_TOKEN", "token", create=True
        ), patch.object(email_config, "CLOUDMAIL_DOMAINS", [], create=True):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["warning"], "")
        submit_registration.assert_called_once_with(count=1, workers=1)

    @patch("apps.web.app.db.outlook_pool_summary")
    @patch("apps.web.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_jobs_with_cloudmail_config_does_not_check_outlook_pool(self, submit_registration, outlook_pool_summary):
        outlook_pool_summary.return_value = {"total": 0, "available": 0, "used": 0, "failed": 0}
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "cloudmail"
        ), patch.object(email_config, "CLOUDMAIL_API_BASE", "https://mail.example.com", create=True), patch.object(
            email_config, "CLOUDMAIL_AUTH_TOKEN", "token", create=True
        ), patch.object(email_config, "CLOUDMAIL_DOMAINS", ["example.com"], create=True):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["warning"], "")
        outlook_pool_summary.assert_not_called()
        submit_registration.assert_called_once_with(count=1, workers=1)
