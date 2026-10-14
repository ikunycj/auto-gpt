# -*- coding: utf-8 -*-
import unittest
import json
import zipfile
from datetime import datetime
from io import BytesIO
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import Mock, patch

from core import account_store
from core import codex_relay_service as relay
from core import db, sqlite_store
from core import gpt_account_service
from apps.web.app import create_app


class CodexRelayWebUiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.patchers = [
            patch.dict("os.environ", {"TURB_SQLITE_PATH": str(root / "accounts.sqlite3")}),
            patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
            patch.object(relay, "_ACCOUNTS_PATH", root / "accounts.json"),
            patch.object(relay, "_PHONES_PATH", root / "phones.json"),
            patch.object(relay, "_JOBS_PATH", root / "jobs.json"),
            patch.object(relay, "_LOG_DIR", root / "logs"),
            patch.object(relay, "_CREDENTIAL_DIR", root / "codex_accounts"),
            patch.object(relay, "_SUB2_SERVICES_PATH", root / "sub2-services.json"),
        ]
        for patcher in self.patchers:
            patcher.start()
        relay._browser_controls.clear()
        relay._phone_locks.clear()
        self.client = create_app().test_client()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tempdir.cleanup()

    def seed_gpt_accounts(self, *accounts):
        account_store.write_gpt_accounts(db._ACCOUNTS_JSON, [dict(row) for row in accounts])

    def stored_gpt_accounts(self):
        return account_store.read_gpt_accounts(db._ACCOUNTS_JSON)

    def test_summary_and_list_use_one_gpt_account_id_per_row(self):
        self.seed_gpt_accounts(
            {"id": "gpt-one", "email": "one@example.com", "chatgpt_password": "one-password"},
            {"id": "gpt-two", "email": "two@example.com", "chatgpt_password": "two-password"},
        )

        response = self.client.get("/api/gpt-accounts")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["total"], 2)
        items = response.get_json()["items"]
        self.assertEqual({item["id"] for item in items}, {"gpt-one", "gpt-two"})
        for item in items:
            self.assertNotIn("registered", item)
            self.assertNotIn("relay", item)
            self.assertNotIn("registered_account_id", item)
            self.assertNotIn("relay_account_id", item)
        self.assertEqual(self.client.get("/api/summary").get_json()["accounts"], 2)

    def test_gpt_accounts_exposes_and_counts_deleted_or_deactivated_accounts(self):
        self.seed_gpt_accounts(
            {"id": "terminal", "email": "terminal@example.com", "codex_status": "deactivated", "liveness_status": "dead"},
            {"id": "retryable", "email": "retryable@example.com", "codex_status": "failed"},
        )
        relay._write(relay._JOBS_PATH, [{
            "id": "mfa-failure", "account_id": "terminal", "status": "failed",
            "error": "账号已删除或已停用（account_deactivated）",
        }])

        response = self.client.get("/api/gpt-accounts")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status_counts"]["codex"]["deactivated"], 1)
        self.assertEqual(payload["status_counts"]["codex"]["failed"], 1)

        filtered = self.client.get("/api/gpt-accounts?codex_status=deactivated").get_json()
        self.assertEqual(filtered["total"], 1)
        row = filtered["items"][0]
        self.assertEqual(row["id"], "terminal")
        self.assertEqual(row["codex_status"], "deactivated")
        self.assertEqual(row["gpt_status"], "dead")
        self.assertIn("账号已删除或已停用", row["codex_job"]["error"])

    def test_delete_removes_selected_gpt_rows_from_database(self):
        self.seed_gpt_accounts(
            {"id": "gpt-one", "email": "one@example.com", "chatgpt_password": "one-password"},
            {"id": "gpt-two", "email": "two@example.com", "chatgpt_password": "two-password"},
        )

        response = self.client.delete("/api/gpt-accounts", json={"account_ids": ["gpt-one"]})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["deleted_count"], 1)
        self.assertEqual([item["id"] for item in self.stored_gpt_accounts()], ["gpt-two"])
        self.assertEqual(self.client.get("/api/gpt-accounts").get_json()["total"], 1)
        self.assertIsNone(gpt_account_service.get_account("gpt-one"))

    def test_delete_rejects_empty_and_busy_requests_without_removing_rows(self):
        self.seed_gpt_accounts({"id": "gpt-busy", "email": "busy@example.com"})
        response = self.client.delete("/api/gpt-accounts", json={"account_ids": []})
        self.assertEqual(response.status_code, 400)

        relay._write(relay._JOBS_PATH, [{
            "id": "running-job", "account_id": "gpt-busy", "email": "busy@example.com",
            "status": "running", "created_at": datetime.now().isoformat(timespec="seconds"),
        }])
        response = self.client.delete("/api/gpt-accounts", json={"account_ids": ["gpt-busy"]})

        self.assertEqual(response.status_code, 409)
        self.assertEqual([item["id"] for item in self.stored_gpt_accounts()], ["gpt-busy"])

    def test_import_plaintext_materials_stays_in_one_gpt_row(self):
        response = self.client.post("/api/gpt-accounts/import", json={
            "text": "user@example.com----chatgpt-password----JBSWY3DPEHPK3PXP----+14155550123----https://sms.example/code"
        })
        self.assertEqual(response.status_code, 200)

        response = self.client.get("/api/gpt-accounts")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["total"], 1)
        account = response.get_json()["items"][0]
        self.assertEqual(account["chatgpt_password"], "chatgpt-password")
        self.assertEqual(account["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertEqual(account["phone"], "")
        self.assertEqual(account["sms_code_url"], "")
        self.assertEqual([item["id"] for item in self.stored_gpt_accounts()], [account["id"]])

        phone_response = self.client.get("/api/phones")
        self.assertEqual(phone_response.status_code, 200)
        phone = phone_response.get_json()["items"][0]
        self.assertEqual(phone["phone"], "+14155550123")
        self.assertEqual(phone["sms_code_url"], "https://sms.example/code")
        self.assertFalse(phone["assigned"])

    def test_import_after_delete_creates_new_row_with_new_id(self):
        material = "again@example.com----chatgpt-password----JBSWY3DPEHPK3PXP"
        self.assertEqual(self.client.post("/api/gpt-accounts/import", json={"text": material}).status_code, 200)
        old_id = self.stored_gpt_accounts()[0]["id"]
        self.assertEqual(self.client.delete("/api/gpt-accounts", json={"account_ids": [old_id]}).status_code, 200)
        self.assertEqual(self.stored_gpt_accounts(), [])

        response = self.client.post("/api/gpt-accounts/import", json={"text": material})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["inserted"], 1)
        rows = self.stored_gpt_accounts()
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0]["id"], old_id)
        self.assertEqual(rows[0]["email"], "again@example.com")

    def test_edit_preserves_single_id_and_existing_login_material(self):
        self.seed_gpt_accounts({
            "id": "gpt-edit", "email": "editable@example.com",
            "chatgpt_password": "old-password", "mailbox_password": "mail-password",
            "email_provider": "mailnest", "email_provider_context": {"project_code": "chatgpt001"},
            "access_token": "existing-access-token", "copy_line": "editable@example.com----existing-access-token----totp",
            "note": "before",
        })

        response = self.client.put("/api/gpt-accounts/gpt-edit", json={
            "chatgpt_password": "new-password", "totp_secret": "JBSWY3DPEHPK3PXP", "note": "edited",
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["account"]["id"], "gpt-edit")
        response_text = response.get_data(as_text=True)
        self.assertNotIn("access_token", response_text)
        self.assertNotIn("copy_line", response_text)
        self.assertNotIn("existing-access-token", response_text)
        rows = self.stored_gpt_accounts()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "gpt-edit")
        self.assertEqual(rows[0]["chatgpt_password"], "new-password")
        self.assertEqual(rows[0]["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertEqual(rows[0]["note"], "edited")
        self.assertEqual(rows[0]["mailbox_password"], "mail-password")
        self.assertEqual(rows[0]["access_token"], "existing-access-token")
        self.assertEqual(rows[0]["copy_line"], "editable@example.com----existing-access-token----totp")
        self.assertEqual(rows[0]["email_provider_context"], {"project_code": "chatgpt001"})

    def test_gpt_account_list_does_not_expose_token_or_copy_line(self):
        token = "eyJ0eXAiOiJKV1QifQ.eyJzdWIiOiJ0ZXN0In0.synthetic-signature"
        self.seed_gpt_accounts({
            "id": "gpt-secret", "email": "secret@example.com",
            "chatgpt_password": "account-password",
            "access_token": token,
            "gpt_access_token": token,
            "id_token": token,
            "refresh_token": "synthetic-refresh-token",
            "copy_line": f"secret@example.com----{token}----totp",
            "unrecognized_secret_material": "unrecognized-secret-sentinel",
        })

        for path in ("/api/gpt-accounts", "/api/gpt-accounts?paged=1&page=1&page_size=1"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            item = response.get_json()["items"][0]
            for field in ("access_token", "gpt_access_token", "id_token", "refresh_token", "copy_line", "unrecognized_secret_material"):
                self.assertNotIn(field, item)
            response_text = response.get_data(as_text=True)
            for secret in (token, "synthetic-refresh-token", "unrecognized-secret-sentinel"):
                self.assertNotIn(secret, response_text)
        stored = self.stored_gpt_accounts()[0]
        self.assertEqual(stored["access_token"], token)
        self.assertEqual(stored["copy_line"], f"secret@example.com----{token}----totp")

    def test_edit_can_clear_fields_without_creating_another_row(self):
        self.seed_gpt_accounts({
            "id": "gpt-clear", "email": "clear@example.com", "chatgpt_password": "old-password",
            "totp_secret": "JBSWY3DPEHPK3PXP", "note": "old note",
        })

        response = self.client.put("/api/gpt-accounts/gpt-clear", json={
            "chatgpt_password": "", "totp_secret": "", "note": "",
        })

        self.assertEqual(response.status_code, 200)
        rows = self.stored_gpt_accounts()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "gpt-clear")
        self.assertEqual(rows[0]["chatgpt_password"], "")
        self.assertEqual(rows[0]["totp_secret"], "")
        self.assertEqual(rows[0]["note"], "")

    def test_edit_refuses_active_account_and_invalid_url_without_mutation(self):
        initial = {"id": "gpt-guarded", "email": "guarded@example.com", "note": "before"}
        self.seed_gpt_accounts(initial)
        relay._write(relay._JOBS_PATH, [{
            "id": "running-job", "account_id": "gpt-guarded", "email": "guarded@example.com",
            "status": "running", "created_at": datetime.now().isoformat(timespec="seconds"),
        }])
        response = self.client.put("/api/gpt-accounts/gpt-guarded", json={"note": "after"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.stored_gpt_accounts(), [initial])

        relay._write(relay._JOBS_PATH, [])
        response = self.client.put("/api/gpt-accounts/gpt-guarded", json={
            "email_code_url": "file:///local/path", "note": "after",
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.stored_gpt_accounts(), [initial])

    def test_authorize_uses_selected_gpt_id_and_needs_no_phone_pool(self):
        self.seed_gpt_accounts({
            "id": "gpt-auth", "email": "auth@example.com", "chatgpt_password": "gpt-password",
            "email_code_url": "https://mail.example/code", "codex_status": "not_authorized",
        })
        with patch.object(relay, "start_jobs", return_value={"ok": True, "submitted": 1, "jobs": []}) as start:
            response = self.client.post("/api/gpt-accounts/authorize", json={
                "account_ids": ["gpt-auth"], "workers": 2,
            })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["emails"], ["auth@example.com"])
        start.assert_called_once_with(["gpt-auth"], workers=2, phone_ids=[])
        self.assertEqual([item["id"] for item in self.stored_gpt_accounts()], ["gpt-auth"])
        self.assertEqual(relay._read(relay._PHONES_PATH), [])

    def test_authorize_accepts_mailnest_otp_login_without_password(self):
        self.seed_gpt_accounts({
            "id": "gpt-otp", "email": "otp@example.com", "login_method": "email_otp",
            "email_provider": "mailnest", "email_provider_context": {"project_code": "chatgpt001"},
            "codex_status": "not_authorized",
        })
        with patch.object(relay, "start_jobs", return_value={"ok": True, "submitted": 1, "jobs": []}) as start:
            response = self.client.post("/api/gpt-accounts/authorize", json={"account_ids": ["gpt-otp"]})

        self.assertEqual(response.status_code, 200)
        start.assert_called_once_with(["gpt-otp"], workers=1, phone_ids=[])
        self.assertEqual(len(self.stored_gpt_accounts()), 1)

    def test_enable_2fa_uses_selected_gpt_id_without_creating_another_row(self):
        self.seed_gpt_accounts({
            "id": "gpt-2fa", "email": "twofa@example.com", "chatgpt_password": "gpt-password",
            "totp_secret": "", "codex_status": "not_authorized",
        })
        with patch.object(relay, "start_account_actions", return_value={"ok": True, "submitted": 1, "jobs": []}) as start:
            response = self.client.post("/api/gpt-accounts/actions", json={
                "account_ids": ["gpt-2fa"], "action": "enable_2fa",
            })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["emails"], ["twofa@example.com"])
        start.assert_called_once_with(["gpt-2fa"], "enable_2fa", workers=1)
        self.assertEqual([item["id"] for item in self.stored_gpt_accounts()], ["gpt-2fa"])

    def test_actions_reject_unknown_gpt_account_id(self):
        self.seed_gpt_accounts({"id": "gpt-known", "email": "known@example.com"})
        response = self.client.post("/api/gpt-accounts/actions", json={
            "account_ids": ["missing-id"], "action": "enable_2fa",
        })
        self.assertEqual(response.status_code, 404)
        self.assertEqual([item["id"] for item in self.stored_gpt_accounts()], ["gpt-known"])

    def test_verification_requires_matching_wait_state(self):
        relay._write(relay._JOBS_PATH, [{
            "id": "job-1", "email": "user@example.com", "status": "waiting_sms",
        }])
        response = self.client.post("/api/codex-jobs/job-1/verification", json={
            "stage": "email", "code": "123456",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("未等待", response.get_json()["error"])

    def test_verification_submission_reaches_waiting_job(self):
        relay._write(relay._JOBS_PATH, [{
            "id": "job-2", "email": "user@example.com", "status": "waiting_totp",
        }])
        response = self.client.post("/api/codex-jobs/job-2/verification", json={
            "stage": "totp", "code": "123456",
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            "ok": True, "job_id": "job-2", "stage": "totp",
        })
        self.assertEqual(relay._pop_code("job-2", "totp"), "123456")

    def test_browser_assist_submission_reaches_waiting_job(self):
        relay._write(relay._JOBS_PATH, [{
            "id": "job-browser", "email": "user@example.com", "status": "waiting_browser",
        }])
        response = self.client.post("/api/codex-jobs/job-browser/browser-assist", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True, "job_id": "job-browser", "status": "running"})
        self.assertTrue(relay._verification_events[("job-browser", "browser")].is_set())

    def test_browser_focus_route_opens_exact_waiting_session(self):
        relay._write(relay._JOBS_PATH, [{
            "id": "job-browser", "email": "user@example.com", "status": "waiting_browser",
        }])
        focused = []
        relay._browser_controls["job-browser"] = {"focus": lambda: focused.append(True)}

        response = self.client.post("/api/codex-jobs/job-browser/browser-focus", json={})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "waiting_browser")
        self.assertEqual(focused, [True])

    def test_batch_delete_jobs_endpoint_skips_active_tasks(self):
        relay._write(relay._JOBS_PATH, [
            {"id": "job-success", "status": "success"},
            {"id": "job-active", "status": "running"},
        ])

        response = self.client.delete("/api/codex-jobs", json={
            "job_ids": ["job-success", "job-active", "job-missing"],
        })

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["deleted"], 1)
        self.assertEqual(payload["deleted_ids"], ["job-success"])
        self.assertEqual([item["job_id"] for item in payload["skipped"]], ["job-active", "job-missing"])
        self.assertEqual([row["id"] for row in relay._read(relay._JOBS_PATH)], ["job-active"])

    def test_browser_assist_job_does_not_accept_numeric_verification(self):
        relay._write(relay._JOBS_PATH, [{
            "id": "job-browser", "email": "user@example.com", "status": "waiting_browser",
        }])
        response = self.client.post("/api/codex-jobs/job-browser/verification", json={
            "stage": "browser", "code": "123456",
        })
        self.assertEqual(response.status_code, 400)

    def test_separate_import_endpoints_auto_assign_with_plaintext_materials(self):
        account_response = self.client.post("/api/gpt-accounts/import", json={
            "text": "user@example.com----chatgpt-password----JBSWY3DPEHPK3PXP",
        })
        self.assertEqual(account_response.status_code, 200)
        self.assertEqual(account_response.get_json()["unassigned_accounts"], 1)

        phone_response = self.client.post("/api/phones/import", json={
            "text": "+14155550123----https://sms.example/private-code",
        })
        self.assertEqual(phone_response.status_code, 200)
        self.assertEqual(phone_response.get_json()["assigned"], 0)

        phones_response = self.client.get("/api/phones")
        self.assertEqual(phones_response.status_code, 200)
        payload = phones_response.get_json()
        self.assertEqual(payload["total"], 1)
        self.assertFalse(payload["items"][0]["assigned"])
        self.assertTrue(payload["items"][0]["candidate"])
        serialized = phones_response.get_data(as_text=True)
        self.assertIn("https://sms.example/private-code", serialized)
        self.assertIn("+14155550123", serialized)

    def test_phone_available_uses_can_be_adjusted_single_or_batch(self):
        self.client.post("/api/phones/import", json={
            "text": (
                "+14155550123----https://sms.example/one\n"
                "+14155550124----https://sms.example/two"
            ),
        })
        phones = self.client.get("/api/phones").get_json()["items"]
        ids = [phone["id"] for phone in phones]
        self.assertEqual([phone["available_uses"] for phone in phones], [1, 1])

        response = self.client.patch("/api/phones/available-uses", json={
            "phone_ids": ids,
            "delta": 1,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["updated"], 2)
        phones = self.client.get("/api/phones").get_json()["items"]
        self.assertEqual([phone["available_uses"] for phone in phones], [2, 2])

        response = self.client.patch("/api/phones/available-uses", json={
            "phone_ids": [ids[0]],
            "delta": -1,
        })
        self.assertEqual(response.status_code, 200)
        phones = self.client.get("/api/phones").get_json()["items"]
        self.assertEqual([phone["available_uses"] for phone in phones], [1, 2])

    def test_accounts_phones_and_jobs_support_independent_pagination(self):
        self.client.post("/api/gpt-accounts/import", json={
            "text": "\n".join([
                "a@example.com----https://mail.example/a",
                "b@example.com----https://mail.example/b",
                "c@example.com----https://mail.example/c",
            ])
        })
        self.client.post("/api/phones/import", json={
            "text": "\n".join([
                "+14155550101----https://sms.example/1",
                "+14155550102----https://sms.example/2",
                "+14155550103----https://sms.example/3",
            ])
        })
        relay._write(relay._JOBS_PATH, [
            {"id": "job-1", "email": "a@example.com", "status": "success", "created_at": "2026-01-01T00:00:01"},
            {"id": "job-2", "email": "b@example.com", "status": "success", "created_at": "2026-01-01T00:00:02"},
            {"id": "job-3", "email": "c@example.com", "status": "success", "created_at": "2026-01-01T00:00:03"},
        ])

        for path in ("gpt-accounts", "phones", "codex-jobs"):
            response = self.client.get(f"/api/{path}?paged=1&page=2&page_size=2")
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload["page"], 2)
            self.assertEqual(payload["page_size"], 2)
            self.assertEqual(payload["total"], 3)
            self.assertEqual(len(payload["items"]), 1)

    def test_accounts_report_active_codex_authorization_independently_of_saved_status(self):
        self.client.post("/api/gpt-accounts/import", json={
            "text": "\n".join((
                "authorizing@example.com----chatgpt-password----JBSWY3DPEHPK3PXP",
                "maintenance@example.com----chatgpt-password----JBSWY3DPEHPK3PXP",
            )),
        })
        accounts = {
            item["email"]: item
            for item in self.client.get("/api/gpt-accounts").get_json()["items"]
        }
        relay._write(relay._JOBS_PATH, [
            {
                "id": "job-codex",
                "account_id": accounts["authorizing@example.com"]["id"],
                "email": "authorizing@example.com",
                "status": "pending",
            },
            {
                "id": "job-maintenance",
                "account_id": accounts["maintenance@example.com"]["id"],
                "email": "maintenance@example.com",
                "action": "check_quota",
                "status": "running",
            },
        ])

        listed = {
            item["email"]: item
            for item in self.client.get("/api/gpt-accounts").get_json()["items"]
        }

        self.assertEqual(listed["authorizing@example.com"]["codex_status"], "authorizing")
        self.assertEqual(listed["authorizing@example.com"]["active_operation"], "authorizing")
        self.assertNotEqual(listed["maintenance@example.com"].get("active_operation"), "authorizing")

        relay._update_job("job-codex", status="success")
        completed = {
            item["email"]: item
            for item in self.client.get("/api/gpt-accounts").get_json()["items"]
        }
        self.assertNotEqual(completed["authorizing@example.com"].get("active_operation"), "authorizing")

    def test_outlook_import_and_account_edit_keep_credentials_out_of_response(self):
        response = self.client.post("/api/gpt-accounts/import", json={
            "text": "outlook@example.com----mailbox-password----client-id----refresh-token",
        })
        self.assertEqual(response.status_code, 200)

        account = self.client.get("/api/gpt-accounts").get_json()["items"][0]
        self.assertEqual(account["email_provider_label"], "Outlook")
        self.assertNotIn("mailbox_password", account)
        self.assertNotIn("outlook_client_id", account)
        self.assertNotIn("outlook_refresh_token", account)
        self.assertNotIn("mailbox-password", str(account))
        self.assertNotIn("refresh-token", str(account))
        stored = self.stored_gpt_accounts()[0]
        self.assertEqual(stored["mailbox_password"], "mailbox-password")
        self.assertEqual(stored["outlook_refresh_token"], "refresh-token")

        response = self.client.put(f"/api/gpt-accounts/{account['id']}", json={
            "chatgpt_password": "new-chatgpt-password",
            "note": "Plus account",
        })
        self.assertEqual(response.status_code, 200)
        updated = response.get_json()["account"]
        self.assertEqual(updated["note"], "Plus account")
        self.assertTrue(updated["password_changed_at"])
        self.assertIn("new-chatgpt-password", response.get_data(as_text=True))

    def test_explicit_account_import_format_is_enforced(self):
        response = self.client.post("/api/gpt-accounts/import", json={
            "format": "chatgpt_email_url",
            "text": "mail-code@example.com----chatgpt-password----https://mail.example/latest",
        })
        self.assertEqual(response.status_code, 200)
        account = self.client.get("/api/gpt-accounts").get_json()["items"][0]
        self.assertEqual(account["chatgpt_password"], "chatgpt-password")
        self.assertEqual(account["email_code_url"], "https://mail.example/latest")
        self.assertFalse(account["has_totp"])

        response = self.client.post("/api/gpt-accounts/import", json={
            "format": "chatgpt_totp",
            "text": "user@example.com----chatgpt-password----JBSWY3DPEHPK3PXP",
        })
        self.assertEqual(response.status_code, 200)

        response = self.client.post("/api/gpt-accounts/import", json={
            "format": "outlook",
            "text": "bad@example.com----password----JBSWY3DPEHPK3PXP",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("微软 Outlook 格式", response.get_json()["error"])

    def test_explicit_rt_sub2_and_cpa_exports(self):
        response = self.client.post("/api/gpt-accounts/import", json={
            "text": "export@example.com----chatgpt-password----JBSWY3DPEHPK3PXP",
        })
        self.assertEqual(response.status_code, 200)
        account = self.client.get("/api/gpt-accounts").get_json()["items"][0]
        relay._CREDENTIAL_DIR.mkdir(parents=True, exist_ok=True)
        credential = {
            "email": "export@example.com",
            "access_token": "test-access-token",
            "refresh_token": "test-refresh-token",
            "id_token": "test-id-token",
            "type": "codex",
        }
        sqlite_store.write_file(relay._CREDENTIAL_DIR / "codex-export@example.com-plus.json",
            json.dumps(credential), category="codex_credentials"
        )

        response = self.client.post("/api/gpt-accounts/export/copy", json={
            "account_ids": [account["id"]], "format": "rt",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["content"], "export@example.com----test-refresh-token\n")

        response = self.client.post("/api/gpt-accounts/export/copy", json={
            "account_ids": [account["id"]], "format": "sub2",
        })
        self.assertEqual(response.status_code, 200)
        sub2 = json.loads(response.get_json()["content"])
        self.assertEqual(sub2["type"], "sub2api-data")
        self.assertEqual(sub2["version"], 1)
        self.assertTrue(sub2["exported_at"].endswith("Z"))
        self.assertEqual(sub2["proxies"], [])
        self.assertEqual(len(sub2["accounts"]), 1)
        exported_account = sub2["accounts"][0]
        self.assertEqual(exported_account["name"], "export@example.com")
        self.assertEqual(exported_account["platform"], "openai")
        self.assertEqual(exported_account["type"], "oauth")
        self.assertEqual(exported_account["credentials"]["refresh_token"], "test-refresh-token")
        self.assertEqual(exported_account["credentials"]["client_id"], "app_EMoamEEZ73f0CkXaXp7hrann")
        self.assertEqual(exported_account["concurrency"], 10)
        self.assertEqual(exported_account["priority"], 1)
        self.assertEqual(exported_account["notes"], "\n".join((
            "自动导入注册机",
            "export@example.com----chatgpt-password----JBSWY3DPEHPK3PXP",
            "",
        )))

        response = self.client.post("/api/gpt-accounts/export/download", json={
            "account_ids": [account["id"]], "format": "sub2",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("codex-sub2api-relay-", response.headers["Content-Disposition"])
        self.assertEqual(json.loads(response.data)["type"], "sub2api-data")

        response = self.client.post("/api/gpt-accounts/export/download", json={
            "account_ids": [account["id"]], "format": "cpa",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/zip")
        with zipfile.ZipFile(BytesIO(response.data)) as archive:
            self.assertIn("codex-export@example.com-plus.json", archive.namelist())
            self.assertIn("manifest.json", archive.namelist())

    def test_sub2_services_can_be_saved_updated_listed_and_deleted(self):
        response = self.client.post("/api/sub2-services", json={
            "name": "主服务",
            "homepage": "https://console.example.com/",
            "api_base": "https://api.example.com/",
            "admin_key": "test-admin-key",
        })
        self.assertEqual(response.status_code, 200)
        service = response.get_json()["service"]
        self.assertEqual(service["homepage"], "https://console.example.com")
        self.assertEqual(service["api_base"], "https://api.example.com")
        self.assertEqual(service["admin_key"], "test-admin-key")
        db_path = sqlite_store.database_path(relay._SUB2_SERVICES_PATH)
        self.assertEqual(db_path.stat().st_mode & 0o777, 0o600)
        self.assertIn("test-admin-key", json.dumps(relay._read(relay._SUB2_SERVICES_PATH)))
        self.assertFalse(relay._SUB2_SERVICES_PATH.exists())

        response = self.client.post("/api/sub2-services", json={
            **service,
            "name": "备用服务",
            "admin_key": "updated-admin-key",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["service"]["name"], "备用服务")
        listing = self.client.get("/api/sub2-services").get_json()
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["items"][0]["admin_key"], "updated-admin-key")

        response = self.client.delete(f"/api/sub2-services/{service['id']}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/sub2-services").get_json()["total"], 0)

    def test_selected_accounts_are_imported_to_saved_sub2_service(self):
        self.client.post("/api/gpt-accounts/import", json={
            "text": "sub2@example.com----chatgpt-password----JBSWY3DPEHPK3PXP----+14155550123----https://sms.example/code",
        })
        account = self.client.get("/api/gpt-accounts").get_json()["items"][0]
        relay._CREDENTIAL_DIR.mkdir(parents=True, exist_ok=True)
        sqlite_store.write_file(relay._CREDENTIAL_DIR / "codex-sub2@example.com.json", json.dumps({
            "email": "sub2@example.com",
            "access_token": "test-access-token",
            "refresh_token": "test-refresh-token",
            "id_token": "test-id-token",
        }), category="codex_credentials")
        service = self.client.post("/api/sub2-services", json={
            "name": "主服务",
            "homepage": "https://console.example.com",
            "api_base": "https://api.example.com/api/v1",
            "admin_key": "test-admin-key",
        }).get_json()["service"]
        remote = Mock(status_code=200, text="")
        remote.json.return_value = {
            "code": 0, "message": "success", "data": {
                "total": 1, "created": 1, "updated": 0, "skipped": 0, "failed": 0,
                "items": [{"index": 1, "action": "created", "account_id": 42}],
            },
        }
        note_response = Mock(status_code=200, text="")
        note_response.json.return_value = {"code": 0, "message": "success"}

        with patch("requests.post", return_value=remote) as post, patch("requests.put", return_value=note_response) as put:
            response = self.client.post("/api/gpt-accounts/sub2/import", json={
                "account_ids": [account["id"]], "service_id": service["id"],
            })

        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(result["submitted"], 1)
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["note_updated"], 1)
        self.assertEqual(result["note_failed"], 0)
        self.assertNotIn("admin_key", result["service"])
        _args, kwargs = post.call_args
        self.assertEqual(_args[0], "https://api.example.com/api/v1/admin/accounts/import/codex-session")
        self.assertEqual(kwargs["headers"]["x-api-key"], "test-admin-key")
        self.assertTrue(kwargs["json"]["update_existing"])
        self.assertTrue(kwargs["json"]["confirm_mixed_channel_risk"])
        self.assertEqual(kwargs["json"]["concurrency"], 10)
        self.assertEqual(kwargs["json"]["priority"], 1)
        uploaded = json.loads(kwargs["json"]["contents"][0])
        self.assertEqual(uploaded["email"], "sub2@example.com")
        self.assertEqual(uploaded["refresh_token"], "test-refresh-token")
        put_args, put_kwargs = put.call_args
        self.assertEqual(put_args[0], "https://api.example.com/api/v1/admin/accounts/42")
        self.assertEqual(put_kwargs["json"], {
            "notes": "\n".join((
                "自动导入注册机",
                "sub2@example.com----chatgpt-password----JBSWY3DPEHPK3PXP",
                "",
            )),
            "concurrency": 10,
            "priority": 1,
        })
        synced = self.client.get("/api/gpt-accounts").get_json()["items"][0]
        self.assertEqual(synced["sub2_service_id"], service["id"])
        self.assertEqual(synced["sub2_account_id"], "42")
        self.assertEqual(synced["sub2_status"], "synced")

    def test_terminal_account_sync_deletes_linked_sub2_account(self):
        self.client.post("/api/gpt-accounts/import", json={
            "text": "disabled@example.com----chatgpt-password----JBSWY3DPEHPK3PXP",
        })
        account = self.client.get("/api/gpt-accounts").get_json()["items"][0]
        service = self.client.post("/api/sub2-services", json={
            "name": "主服务", "api_base": "https://api.example.com/api/v1",
            "admin_key": "test-admin-key",
        }).get_json()["service"]
        relay._update_account(
            account["id"],
            codex_status="deactivated",
            sub2_service_id=service["id"],
            sub2_account_id="42",
        )
        listing = Mock(status_code=200, text="")
        listing.json.return_value = {"code": 0, "data": {
            "total": 1, "pages": 1, "items": [{
                "id": 42, "name": "disabled@example.com", "platform": "openai",
                "type": "oauth", "parent_account_id": None,
            }],
        }}
        deleted = Mock(status_code=200, text="")
        deleted.json.return_value = {"code": 0, "data": {"message": "deleted"}}

        with (
            patch("requests.get", return_value=listing),
            patch("requests.delete", return_value=deleted) as delete,
            patch("requests.post") as post,
        ):
            response = self.client.post("/api/gpt-accounts/sub2/import", json={
                "account_ids": [account["id"]],
                "service_id": service["id"],
                "delete_terminal": True,
            })

        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(result["submitted"], 0)
        self.assertEqual(result["terminated_selected"], 1)
        self.assertEqual(result["delete_attempted"], 1)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["delete_not_found"], 0)
        self.assertEqual(result["delete_failed"], 0)
        post.assert_not_called()
        self.assertEqual(delete.call_args.args[0], "https://api.example.com/api/v1/admin/accounts/42")
        self.assertEqual(delete.call_args.kwargs["headers"]["x-api-key"], "test-admin-key")
        stored = relay._read(relay._ACCOUNTS_PATH)[0]
        self.assertEqual(stored["sub2_status"], "deleted")
        self.assertTrue(stored["sub2_deleted_at"])
        self.assertEqual(stored["sub2_links"][service["id"]]["status"], "deleted")

    def test_terminal_account_delete_requires_explicit_confirmation_flag(self):
        self.client.post("/api/gpt-accounts/import", json={
            "text": "no-confirm@example.com----chatgpt-password----JBSWY3DPEHPK3PXP",
        })
        account = self.client.get("/api/gpt-accounts").get_json()["items"][0]
        service = self.client.post("/api/sub2-services", json={
            "name": "主服务", "api_base": "https://api.example.com", "admin_key": "test-admin-key",
        }).get_json()["service"]
        relay._update_account(account["id"], codex_status="deactivated")

        with patch("requests.delete") as delete:
            response = self.client.post("/api/gpt-accounts/sub2/import", json={
                "account_ids": [account["id"]], "service_id": service["id"],
            })

        self.assertEqual(response.status_code, 400)
        self.assertIn("明确确认", response.get_json()["error"])
        delete.assert_not_called()

    def test_terminal_account_delete_is_idempotent_when_remote_is_absent(self):
        self.client.post("/api/gpt-accounts/import", json={
            "text": "already-gone@example.com----chatgpt-password----JBSWY3DPEHPK3PXP",
        })
        account = self.client.get("/api/gpt-accounts").get_json()["items"][0]
        service = self.client.post("/api/sub2-services", json={
            "name": "主服务", "api_base": "https://api.example.com", "admin_key": "test-admin-key",
        }).get_json()["service"]
        relay._update_account(
            account["id"], codex_status="deactivated",
            sub2_service_id=service["id"], sub2_account_id="77",
        )
        listing = Mock(status_code=200, text="")
        listing.json.return_value = {"code": 0, "data": {"total": 0, "pages": 1, "items": []}}

        with patch("requests.get", return_value=listing), patch("requests.delete") as delete:
            response = self.client.post("/api/gpt-accounts/sub2/import", json={
                "account_ids": [account["id"]],
                "service_id": service["id"],
                "delete_terminal": True,
            })

        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(result["delete_not_found"], 1)
        self.assertEqual(result["delete_failed"], 0)
        delete.assert_not_called()

    def test_sub2_sync_uploads_active_and_deletes_terminal_accounts_together(self):
        self.client.post("/api/gpt-accounts/import", json={
            "text": "\n".join((
                "active@example.com----active-password----JBSWY3DPEHPK3PXP",
                "terminal@example.com----terminal-password----JBSWY3DPEHPK3PXP",
            )),
        })
        accounts = {
            item["email"]: item
            for item in self.client.get("/api/gpt-accounts").get_json()["items"]
        }
        service = self.client.post("/api/sub2-services", json={
            "name": "主服务", "api_base": "https://api.example.com/api/v1",
            "admin_key": "test-admin-key",
        }).get_json()["service"]
        relay._CREDENTIAL_DIR.mkdir(parents=True, exist_ok=True)
        sqlite_store.write_file(relay._CREDENTIAL_DIR / "codex-active@example.com.json", json.dumps({
            "email": "active@example.com", "refresh_token": "active-refresh-token",
        }), category="codex_credentials")
        relay._update_account(
            accounts["terminal@example.com"]["id"],
            codex_status="deactivated",
            sub2_service_id=service["id"],
            sub2_account_id="84",
        )
        imported = Mock(status_code=200, text="")
        imported.json.return_value = {"code": 0, "data": {
            "total": 1, "created": 1, "updated": 0, "failed": 0,
            "items": [{"index": 1, "action": "created", "account_id": 83}],
        }}
        note_updated = Mock(status_code=200, text="")
        note_updated.json.return_value = {"code": 0, "data": {}}
        listing = Mock(status_code=200, text="")
        listing.json.return_value = {"code": 0, "data": {
            "total": 1, "pages": 1, "items": [{
                "id": 84, "name": "terminal@example.com", "platform": "openai",
                "type": "oauth", "parent_account_id": None,
            }],
        }}
        deleted = Mock(status_code=200, text="")
        deleted.json.return_value = {"code": 0, "data": {}}

        with (
            patch("requests.post", return_value=imported),
            patch("requests.put", return_value=note_updated),
            patch("requests.get", return_value=listing),
            patch("requests.delete", return_value=deleted),
        ):
            response = self.client.post("/api/gpt-accounts/sub2/import", json={
                "account_ids": [
                    accounts["active@example.com"]["id"],
                    accounts["terminal@example.com"]["id"],
                ],
                "service_id": service["id"],
                "delete_terminal": True,
            })

        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(result["submitted"], 1)
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["delete_failed"], 0)
        stored = {row["email"]: row for row in relay._read(relay._ACCOUNTS_PATH)}
        self.assertEqual(stored["active@example.com"]["sub2_account_id"], "83")
        self.assertEqual(stored["terminal@example.com"]["sub2_status"], "deleted")

    def test_terminal_account_delete_rejects_ambiguous_remote_email(self):
        self.client.post("/api/gpt-accounts/import", json={
            "text": "duplicate@example.com----chatgpt-password----JBSWY3DPEHPK3PXP",
        })
        account = self.client.get("/api/gpt-accounts").get_json()["items"][0]
        service = self.client.post("/api/sub2-services", json={
            "name": "主服务", "api_base": "https://api.example.com", "admin_key": "test-admin-key",
        }).get_json()["service"]
        relay._update_account(account["id"], codex_status="deactivated")
        listing = Mock(status_code=200, text="")
        listing.json.return_value = {"code": 0, "data": {
            "total": 2, "pages": 1, "items": [
                {"id": 91, "name": "duplicate@example.com", "platform": "openai", "type": "oauth"},
                {"id": 92, "name": "duplicate@example.com", "platform": "openai", "type": "oauth"},
            ],
        }}

        with patch("requests.get", return_value=listing), patch("requests.delete") as delete:
            response = self.client.post("/api/gpt-accounts/sub2/import", json={
                "account_ids": [account["id"]],
                "service_id": service["id"],
                "delete_terminal": True,
            })

        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertFalse(result["ok"])
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(result["delete_failed"], 1)
        self.assertIn("同一邮箱存在多个账号", result["errors"][0]["error"])
        delete.assert_not_called()

    def test_accounts_sync_from_sub2_uses_strict_notes_and_pagination(self):
        self.client.post("/api/gpt-accounts/import", json={
            "text": "existing@example.com----https://mail.example/latest",
        })
        service = self.client.post("/api/sub2-services", json={
            "name": "主服务", "api_base": "https://api.example.com/api/v1",
            "admin_key": "test-admin-key",
        }).get_json()["service"]
        first_page = Mock(status_code=200, text="")
        first_page.json.return_value = {
            "code": 0, "message": "success", "data": {
                "total": 4, "page": 1, "page_size": 100, "pages": 2,
                "items": [
                    {
                        "id": 11, "name": "remote@example.com", "platform": "openai", "type": "oauth", "status": "active",
                        "notes": "\n".join((
                            "自动导入注册机",
                            "remote@example.com----remote-password----JBSWY3DPEHPK3PXP",
                            "+14155550123----https://sms.example/remote",
                        )),
                    },
                    {
                        "id": 12, "name": "old@example.com", "platform": "openai", "type": "oauth", "status": "active",
                        "notes": "\n".join((
                            "注册机自动导入",
                            "GPT邮箱信息：old@example.com----old-password",
                            "手机号+接码信息：未配置",
                        )),
                    },
                ],
            },
        }
        second_page = Mock(status_code=200, text="")
        second_page.json.return_value = {
            "code": 0, "message": "success", "data": {
                "total": 4, "page": 2, "page_size": 100, "pages": 2,
                "items": [
                    {
                        "id": 13, "name": "existing@example.com", "platform": "openai", "type": "oauth", "status": "active",
                        "notes": "\n".join((
                            "自动导入注册机",
                            "existing@example.com----https://mail.example/latest",
                            "",
                        )),
                    },
                    {
                        "id": 14, "name": "different@example.com", "platform": "openai", "type": "oauth", "status": "active",
                        "notes": "\n".join((
                            "自动导入注册机",
                            "mismatch@example.com----https://mail.example/latest",
                            "",
                        )),
                    },
                ],
            },
        }

        with patch("requests.get", side_effect=[first_page, second_page]) as get:
            response = self.client.post("/api/gpt-accounts/sub2/sync", json={
                "service_id": service["id"],
            })

        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(result["remote_total"], 4)
        self.assertEqual(result["marked"], 3)
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["skipped_unmarked"], 1)
        self.assertEqual(result["skipped_invalid"], 0)
        self.assertEqual(result["conflicts"], 1)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args_list[0].kwargs["params"]["page"], 1)
        self.assertEqual(get.call_args_list[1].kwargs["params"]["page"], 2)
        self.assertEqual(get.call_args_list[0].kwargs["params"]["type"], "oauth")

        accounts = {item["email"]: item for item in self.client.get("/api/gpt-accounts").get_json()["items"]}
        remote = accounts["remote@example.com"]
        self.assertEqual(remote["chatgpt_password"], "remote-password")
        self.assertEqual(remote["phone"], "+14155550123----https://sms.example/remote")
        self.assertEqual(remote["sms_code_url"], "https://sms.example/remote")
        self.assertEqual(remote["codex_status"], "unauthorized")
        self.assertEqual(remote["sub2_account_id"], "11")
        self.assertFalse(relay._CREDENTIAL_DIR.exists())
        phone = self.client.get("/api/phones").get_json()["items"][0]
        self.assertTrue(phone["candidate"])
        self.assertTrue(phone["assigned"])
        self.assertEqual(phone["assigned_account_email"], "remote@example.com")

    def test_sub2_reverse_sync_rejects_invalid_and_conflicting_notes(self):
        self.client.post("/api/gpt-accounts/import", json={
            "text": "conflict@example.com----local-password----JBSWY3DPEHPK3PXP",
        })
        service = self.client.post("/api/sub2-services", json={
            "name": "主服务", "api_base": "https://api.example.com",
            "admin_key": "test-admin-key",
        }).get_json()["service"]
        remote = Mock(status_code=200, text="")
        remote.json.return_value = {
            "code": 0, "message": "success", "data": {
                "total": 3, "page": 1, "page_size": 100, "pages": 1,
                "items": [
                    {
                        "id": 21, "name": "conflict@example.com", "platform": "openai", "type": "oauth",
                        "notes": "\n".join((
                            "自动导入注册机",
                            "conflict@example.com----remote-password----JBSWY3DPEHPK3PXP",
                            "",
                        )),
                    },
                    {
                        "id": 22, "name": "invalid@example.com", "platform": "openai", "type": "oauth",
                        "notes": "\n".join((
                            "自动导入注册机",
                            "invalid@example.com----https://mail.example/latest",
                            "not-a-phone-record",
                        )),
                    },
                    {
                        "id": 23, "name": "old@example.com", "platform": "openai", "type": "oauth",
                        "notes": "old@example.com----old-password",
                    },
                ],
            },
        }

        with patch("requests.get", return_value=remote):
            response = self.client.post("/api/gpt-accounts/sub2/sync", json={
                "service_id": service["id"],
            })

        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(result["inserted"], 0)
        self.assertEqual(result["conflicts"], 1)
        self.assertEqual(result["skipped_invalid"], 1)
        self.assertEqual(result["skipped_unmarked"], 1)
        account = self.client.get("/api/gpt-accounts").get_json()["items"][0]
        self.assertEqual(account["chatgpt_password"], "local-password")
        self.assertEqual(account.get("sub2_account_id", ""), "")

    def test_sub2_reverse_sync_uses_three_way_merge(self):
        service = self.client.post("/api/sub2-services", json={
            "name": "主服务", "api_base": "https://api.example.com",
            "admin_key": "test-admin-key",
        }).get_json()["service"]

        def remote_response(password, phone_line=""):
            response = Mock(status_code=200, text="")
            response.json.return_value = {
                "code": 0, "message": "success", "data": {
                    "total": 1, "page": 1, "page_size": 100, "pages": 1,
                    "items": [{
                        "id": 31, "name": "merge@example.com", "platform": "openai", "type": "oauth", "status": "active",
                        "notes": "\n".join((
                            "自动导入注册机",
                            f"merge@example.com----{password}----JBSWY3DPEHPK3PXP",
                            phone_line,
                        )),
                    }],
                },
            }
            return response

        with patch("requests.get", return_value=remote_response(
            "remote-v1", "+14155550124----https://sms.example/merge",
        )):
            first = self.client.post("/api/gpt-accounts/sub2/sync", json={"service_id": service["id"]}).get_json()
        self.assertEqual(first["inserted"], 1)

        with patch("requests.get", return_value=remote_response("remote-v2")):
            remote_changed = self.client.post("/api/gpt-accounts/sub2/sync", json={"service_id": service["id"]}).get_json()
        self.assertEqual(remote_changed["updated"], 1)
        account = self.client.get("/api/gpt-accounts").get_json()["items"][0]
        self.assertEqual(account["chatgpt_password"], "remote-v2")
        self.assertEqual(account["phone"], "")
        self.assertFalse(self.client.get("/api/phones").get_json()["items"][0]["assigned"])

        with patch("requests.get", return_value=remote_response("remote-v2")):
            repeated = self.client.post("/api/gpt-accounts/sub2/sync", json={"service_id": service["id"]}).get_json()
        self.assertEqual(repeated["unchanged"], 1)

        self.client.put(f"/api/gpt-accounts/{account['id']}", json={"chatgpt_password": "local-v3"})
        with patch("requests.get", return_value=remote_response("remote-v2")):
            local_changed = self.client.post("/api/gpt-accounts/sub2/sync", json={"service_id": service["id"]}).get_json()
        self.assertEqual(local_changed["unchanged"], 1)
        account = self.client.get("/api/gpt-accounts").get_json()["items"][0]
        self.assertEqual(account["chatgpt_password"], "local-v3")

        with patch("requests.get", return_value=remote_response("remote-v3")):
            both_changed = self.client.post("/api/gpt-accounts/sub2/sync", json={"service_id": service["id"]}).get_json()
        self.assertEqual(both_changed["conflicts"], 1)
        account = self.client.get("/api/gpt-accounts").get_json()["items"][0]
        self.assertEqual(account["chatgpt_password"], "local-v3")

    def test_sub2_notes_accept_trimmed_empty_phone_line(self):
        parsed = relay._parse_sub2_notes({
            "notes": "自动导入注册机\ntrimmed@example.com----https://mail.example/code",
        })

        self.assertEqual(parsed["account"]["email"], "trimmed@example.com")
        self.assertEqual(parsed["phone"], {})
        self.assertEqual(parsed["notes"], "自动导入注册机\ntrimmed@example.com----https://mail.example/code\n")

    def test_sub2_notes_accept_three_or_four_dash_phone_separator(self):
        for separator in ("---", "----"):
            with self.subTest(separator=separator):
                parsed = relay._parse_sub2_notes({
                    "notes": "\n".join((
                        "自动导入注册机",
                        "phone-separator@example.com----https://mail.example/code",
                        f"+14155550123{separator}https://sms.example/code{separator}2",
                    )),
                })

                self.assertEqual(parsed["phone"], {
                    "phone": "+14155550123",
                    "sms_code_url": "https://sms.example/code",
                    "available_uses": 2,
                })

    def test_sub2_notes_reject_two_dash_phone_separator(self):
        with self.assertRaisesRegex(ValueError, "手机号格式"):
            relay._parse_sub2_notes({
                "notes": "\n".join((
                    "自动导入注册机",
                    "bad-phone-separator@example.com----https://mail.example/code",
                    "+14155550123--https://sms.example/code",
                )),
            })

    def test_sub2_notes_reject_unknown_fourth_line(self):
        with self.assertRaisesRegex(ValueError, "两或三行"):
            relay._parse_sub2_notes({
                "notes": "\n".join((
                    "自动导入注册机",
                    "unknown-footer@example.com----https://mail.example/code",
                    "",
                    "未知附加内容",
                )),
            })

    def test_sub2_business_failure_is_returned_as_error(self):
        self.client.post("/api/gpt-accounts/import", json={
            "text": "sub2-error@example.com----chatgpt-password----JBSWY3DPEHPK3PXP",
        })
        account = self.client.get("/api/gpt-accounts").get_json()["items"][0]
        relay._CREDENTIAL_DIR.mkdir(parents=True, exist_ok=True)
        sqlite_store.write_file(relay._CREDENTIAL_DIR / "codex-sub2-error@example.com.json", json.dumps({
            "email": "sub2-error@example.com", "refresh_token": "test-refresh-token",
        }), category="codex_credentials")
        service = self.client.post("/api/sub2-services", json={
            "name": "错误服务", "api_base": "https://api.example.com",
            "admin_key": "test-admin-key",
        }).get_json()["service"]
        remote = Mock(status_code=200, text="")
        remote.json.return_value = {"ok": False, "error": "管理员密钥无效"}

        with patch("requests.post", return_value=remote):
            response = self.client.post("/api/gpt-accounts/sub2/import", json={
                "account_ids": [account["id"]], "service_id": service["id"],
            })

        self.assertEqual(response.status_code, 400)
        self.assertIn("管理员密钥无效", response.get_json()["error"])

    def test_sub2_status_query_maps_scheduler_quota_and_usage_windows(self):
        self.client.post("/api/gpt-accounts/import", json={
            "text": "status@example.com----chatgpt-password----JBSWY3DPEHPK3PXP",
        })
        account = self.client.get("/api/gpt-accounts").get_json()["items"][0]
        service = self.client.post("/api/sub2-services", json={
            "name": "状态服务", "api_base": "https://api.example.com", "admin_key": "test-admin-key",
        }).get_json()["service"]

        listing = Mock(status_code=200, text="")
        listing.json.return_value = {"code": 0, "data": {
            "total": 1, "page": 1, "page_size": 100, "pages": 1,
            "items": [{
                "id": 1531, "name": "status@example.com", "platform": "openai", "type": "oauth",
                "status": "active", "schedulable": False,
                "temp_unschedulable_reason": "rate_limited",
                "session_window_status": "active", "credentials_status": "active",
                "session_window_start": "2026-08-11T10:00:00Z",
                "session_window_end": "2026-08-11T15:00:00Z",
                "rate_limit_reset_at": 1787021103, "last_used_at": "2026-08-11T14:00:00Z",
            }],
        }}
        quota = Mock(status_code=200, text="")
        quota.json.return_value = {"code": 0, "data": {
            "plan_type": "plus", "fetched_at": 1786428085,
            "rate_limit": {"allowed": False, "limit_reached": True, "primary_window": {
                "used_percent": 100, "limit_window_seconds": 604800,
                "reset_after_seconds": 593018, "reset_at": 1787021103,
            }},
        }}
        usage = Mock(status_code=200, text="")
        usage.json.return_value = {"code": 0, "data": {
            "updated_at": "2026-08-11T14:01:28+08:00",
            "five_hour": {"utilization": 12, "resets_at": "2026-08-11T15:00:00+08:00", "remaining_seconds": 3600,
                           "window_stats": {"requests": 3, "tokens": 1200, "cost": 0.2}},
            "seven_day": {"utilization": 100, "resets_at": "2026-08-18T10:45:02+08:00", "remaining_seconds": 593013,
                           "window_stats": {"requests": 10, "tokens": 5000, "cost": 1.2}},
        }}

        with patch("requests.get", side_effect=[listing, quota, usage]) as get:
            result = relay.sync_sub2_account_status(account["id"])

        self.assertTrue(result["ok"])
        self.assertFalse(result["partial"])
        self.assertEqual(get.call_count, 3)
        self.assertEqual(get.call_args_list[1].args[0], "https://api.example.com/api/v1/admin/openai/accounts/1531/quota")
        self.assertEqual(get.call_args_list[2].args[0], "https://api.example.com/api/v1/admin/accounts/1531/usage")
        self.assertFalse(get.call_args_list[0].kwargs["verify"])
        stored = self.client.get("/api/gpt-accounts").get_json()["items"][0]
        self.assertEqual(stored["sub2_service_id"], service["id"])
        self.assertEqual(stored["sub2_account_id"], "1531")
        self.assertEqual(stored["sub2_liveness_status"], "blocked")
        self.assertEqual(stored["sub2_account_status"], "active")
        self.assertFalse(stored["sub2_schedulable"])
        self.assertEqual(stored["sub2_quota_plan"], "plus")
        self.assertEqual(stored["sub2_quota_primary_used_percent"], 100)
        self.assertEqual(stored["sub2_five_hour_utilization"], 12)
        self.assertEqual(stored["sub2_seven_day_utilization"], 100)
        self.assertEqual(stored["sub2_five_hour_stats"]["tokens"], 1200)
        self.assertEqual(stored["sub2_seven_day_stats"]["requests"], 10)

    def test_sub2_status_action_writes_task_result(self):
        self.client.post("/api/gpt-accounts/import", json={
            "text": "task-status@example.com----https://mail.example/code",
        })
        account = self.client.get("/api/gpt-accounts").get_json()["items"][0]
        service = self.client.post("/api/sub2-services", json={
            "name": "任务服务", "api_base": "https://api.example.com", "admin_key": "test-admin-key",
        }).get_json()["service"]
        relay._update_account(account["id"], sub2_service_id=service["id"], sub2_account_id="9")
        listing = Mock(status_code=200, text="")
        listing.json.return_value = {"code": 0, "data": {"total": 1, "page": 1, "page_size": 100, "pages": 1,
            "items": [{"id": 9, "name": "task-status@example.com", "platform": "openai", "type": "oauth",
                       "status": "active", "schedulable": True, "credentials_status": "active"}]}}
        quota = Mock(status_code=200, text="")
        quota.json.return_value = {"code": 0, "data": {"plan_type": "free", "rate_limit": {"primary_window": {"used_percent": 0}}}}
        usage = Mock(status_code=200, text="")
        usage.json.return_value = {"code": 0, "data": {"five_hour": {"utilization": 0}, "seven_day": {"utilization": 5}}}
        job = {"id": "job-sub2-status", "account_id": account["id"], "email": account["email"],
               "action": "check_sub2_status", "status": "queued", "stage": "queued"}
        relay._write(relay._JOBS_PATH, [job])
        with patch("requests.get", side_effect=[listing, quota, usage]):
            relay._run_maintenance_job(job["id"], account["id"], "check_sub2_status")
        stored_job = relay._read(relay._JOBS_PATH)[0]
        self.assertEqual(stored_job["status"], "success")
        self.assertIn("sub2 状态已同步", stored_job["message"])

    def test_refresh_sub2_updates_existing_oauth_and_preserves_remote_credentials(self):
        self.client.post("/api/gpt-accounts/import", json={
            "text": "refresh@example.com----chatgpt-password----JBSWY3DPEHPK3PXP",
        })
        account = self.client.get("/api/gpt-accounts").get_json()["items"][0]
        service = self.client.post("/api/sub2-services", json={
            "name": "刷新服务", "api_base": "https://api.example.com", "admin_key": "test-admin-key",
        }).get_json()["service"]
        relay._CREDENTIAL_DIR.mkdir(parents=True, exist_ok=True)
        credential_path = relay._CREDENTIAL_DIR / "codex-refresh@example.com.json"
        sqlite_store.write_file(credential_path, json.dumps({"email": account["email"], "refresh_token": "old-local-rt"}), category="codex_credentials")

        remote = {
            "id": 77, "name": account["email"], "platform": "openai", "type": "oauth",
            "status": "error", "schedulable": False, "credentials_status": "invalid",
            "credentials": {
                "model_mapping": {"gpt-5": "gpt-5-codex"},
                "header_overrides": {"x-upstream": "keep"},
                "openai_capabilities": ["responses"],
                "auth_mode": "personalAccessToken",
                "openai_auth_mode": "personal_access_token",
                "token_type": "Bearer",
                "chatgpt_account_is_fedramp": True,
            },
        }
        listing = Mock(status_code=200, text="")
        listing.json.return_value = {"code": 0, "data": {"total": 1, "page": 1, "page_size": 100, "pages": 1, "items": [remote]}}
        status_listing = Mock(status_code=200, text="")
        status_listing.json.return_value = {"code": 0, "data": {"total": 1, "page": 1, "page_size": 100, "pages": 1, "items": [remote]}}
        quota = Mock(status_code=200, text="")
        quota.json.return_value = {"code": 0, "data": {"plan_type": "plus", "rate_limit": {"primary_window": {"used_percent": 12}}}}
        usage = Mock(status_code=200, text="")
        usage.json.return_value = {"code": 0, "data": {"five_hour": {"utilization": 9}, "seven_day": {"utilization": 31}}}
        apply_response = Mock(status_code=200, text="")
        apply_response.json.return_value = {"code": 0, "data": {}}
        refresh_response = Mock(status_code=200, text="")
        refresh_response.json.return_value = {"code": 0, "data": {}}
        refreshed_local = {
            "email": account["email"], "access_token": "new-local-at", "refresh_token": "new-local-rt",
            "id_token": "new-local-id", "expired": "2026-08-11T18:00:00Z", "account_id": "chatgpt-account-77",
            "client_id": "local-client-id",
        }

        with patch("core.codex_oauth.refresh_codex_credential", return_value=refreshed_local), \
             patch("requests.get", side_effect=[listing, status_listing, quota, usage]), \
             patch("requests.post", side_effect=[apply_response, refresh_response]) as post:
            result = relay.refresh_sub2_account(account["id"])

        self.assertTrue(result["ok"])
        self.assertFalse(result["partial"])
        self.assertTrue(result["remote_refreshed"])
        self.assertEqual(post.call_count, 2)
        apply_call, refresh_call = post.call_args_list
        self.assertEqual(apply_call.args[0], "https://api.example.com/api/v1/admin/accounts/77/apply-oauth-credentials")
        apply_body = apply_call.kwargs["json"]
        self.assertEqual(apply_body["type"], "oauth")
        self.assertEqual(apply_body["credentials"]["access_token"], "new-local-at")
        self.assertEqual(apply_body["credentials"]["refresh_token"], "new-local-rt")
        self.assertEqual(apply_body["credentials"]["chatgpt_account_id"], "chatgpt-account-77")
        self.assertEqual(apply_body["credentials"]["model_mapping"], {"gpt-5": "gpt-5-codex"})
        self.assertEqual(apply_body["credentials"]["header_overrides"], {"x-upstream": "keep"})
        self.assertEqual(apply_body["credentials"]["openai_capabilities"], ["responses"])
        self.assertNotIn("auth_mode", apply_body["credentials"])
        self.assertNotIn("openai_auth_mode", apply_body["credentials"])
        self.assertNotIn("concurrency", apply_body)
        self.assertNotIn("priority", apply_body)
        self.assertNotIn("proxy_id", apply_body)
        self.assertNotIn("group_ids", apply_body)
        self.assertIn("Idempotency-Key", apply_call.kwargs["headers"])
        self.assertFalse(apply_call.kwargs["verify"])
        self.assertEqual(refresh_call.args[0], "https://api.example.com/api/v1/admin/accounts/77/refresh")
        self.assertEqual(refresh_call.kwargs["json"], {})

        stored = self.client.get("/api/gpt-accounts").get_json()["items"][0]
        self.assertEqual(stored["sub2_account_id"], "77")
        self.assertEqual(stored["sub2_credentials_update_status"], "updated")
        self.assertEqual(stored["sub2_refresh_status"], "refreshed")
        self.assertTrue(stored["sub2_credentials_updated_at"])
        self.assertTrue(stored["sub2_refresh_at"])
        self.assertEqual(stored["sub2_refresh_error"], "")

    def test_refresh_sub2_keeps_credential_update_when_remote_refresh_fails(self):
        self.client.post("/api/gpt-accounts/import", json={
            "text": "partial@example.com----chatgpt-password----JBSWY3DPEHPK3PXP",
        })
        account = self.client.get("/api/gpt-accounts").get_json()["items"][0]
        self.client.post("/api/sub2-services", json={
            "name": "部分成功服务", "api_base": "https://api.example.com", "admin_key": "test-admin-key",
        })
        relay._CREDENTIAL_DIR.mkdir(parents=True, exist_ok=True)
        sqlite_store.write_file(relay._CREDENTIAL_DIR / "codex-partial@example.com.json",
            json.dumps({"email": account["email"], "refresh_token": "old-local-rt"}), category="codex_credentials"
        )
        remote = {
            "id": 88, "name": account["email"], "platform": "openai", "type": "oauth",
            "status": "active", "schedulable": True, "credentials_status": "active",
            "credentials": {"model_mapping": {"gpt-5": "gpt-5-codex"}},
        }
        listing = Mock(status_code=200, text="")
        listing.json.return_value = {"code": 0, "data": {"total": 1, "page": 1, "page_size": 100, "pages": 1, "items": [remote]}}
        status_listing = Mock(status_code=200, text="")
        status_listing.json.return_value = {"code": 0, "data": {"total": 1, "page": 1, "page_size": 100, "pages": 1, "items": [remote]}}
        quota = Mock(status_code=200, text="")
        quota.json.return_value = {"code": 0, "data": {}}
        usage = Mock(status_code=200, text="")
        usage.json.return_value = {"code": 0, "data": {}}
        apply_response = Mock(status_code=200, text="")
        apply_response.json.return_value = {"code": 0, "data": {}}
        refresh_failed = Mock(status_code=502, text="upstream refresh failed")
        refresh_failed.json.return_value = {"error": "upstream refresh failed"}

        with patch("core.codex_oauth.refresh_codex_credential", return_value={
            "email": account["email"], "access_token": "new-local-at", "refresh_token": "new-local-rt",
        }), patch("requests.get", side_effect=[listing, status_listing, quota, usage]), \
             patch("requests.post", side_effect=[apply_response, refresh_failed]):
            result = relay.refresh_sub2_account(account["id"])

        self.assertTrue(result["ok"])
        self.assertTrue(result["partial"])
        self.assertFalse(result["remote_refreshed"])
        stored = self.client.get("/api/gpt-accounts").get_json()["items"][0]
        self.assertEqual(stored["sub2_credentials_update_status"], "updated")
        self.assertEqual(stored["sub2_refresh_status"], "failed")
        self.assertIn("upstream refresh failed", stored["sub2_refresh_error"])

    def test_refresh_sub2_reads_redacted_credentials_from_detail_when_list_omits_them(self):
        self.client.post("/api/gpt-accounts/import", json={
            "text": "detail-fallback@example.com----chatgpt-password----JBSWY3DPEHPK3PXP",
        })
        account = self.client.get("/api/gpt-accounts").get_json()["items"][0]
        self.client.post("/api/sub2-services", json={
            "name": "详情回退服务", "api_base": "https://api.example.com", "admin_key": "test-admin-key",
        })
        relay._CREDENTIAL_DIR.mkdir(parents=True, exist_ok=True)
        sqlite_store.write_file(relay._CREDENTIAL_DIR / "codex-detail-fallback@example.com.json",
            json.dumps({"email": account["email"], "refresh_token": "old-local-rt"}), category="codex_credentials"
        )
        remote = {
            "id": 97, "name": account["email"], "platform": "openai", "type": "oauth",
            "status": "active", "schedulable": True, "credentials_status": "active",
        }
        remote_detail = {
            **remote,
            "credentials": {"model_mapping": {"gpt-5": "gpt-5-codex"}, "header_overrides": {"x-route": "keep"}},
        }
        listing = Mock(status_code=200, text="")
        listing.json.return_value = {"code": 0, "data": {"total": 1, "page": 1, "page_size": 100, "pages": 1, "items": [remote]}}
        detail = Mock(status_code=200, text="")
        detail.json.return_value = {"code": 0, "data": remote_detail}
        status_listing = Mock(status_code=200, text="")
        status_listing.json.return_value = {"code": 0, "data": {"total": 1, "page": 1, "page_size": 100, "pages": 1, "items": [remote]}}
        quota = Mock(status_code=200, text="")
        quota.json.return_value = {"code": 0, "data": {}}
        usage = Mock(status_code=200, text="")
        usage.json.return_value = {"code": 0, "data": {}}
        apply_response = Mock(status_code=200, text="")
        apply_response.json.return_value = {"code": 0, "data": {}}
        refresh_response = Mock(status_code=200, text="")
        refresh_response.json.return_value = {"code": 0, "data": {}}

        with patch("core.codex_oauth.refresh_codex_credential", return_value={
            "email": account["email"], "access_token": "new-local-at", "refresh_token": "new-local-rt",
        }), patch("requests.get", side_effect=[listing, detail, status_listing, quota, usage]) as get, \
             patch("requests.post", side_effect=[apply_response, refresh_response]) as post:
            result = relay.refresh_sub2_account(account["id"])

        self.assertTrue(result["ok"])
        self.assertFalse(result["partial"])
        self.assertEqual(get.call_args_list[1].args[0], "https://api.example.com/api/v1/admin/accounts/97")
        apply_body = post.call_args_list[0].kwargs["json"]
        self.assertEqual(apply_body["credentials"]["model_mapping"], {"gpt-5": "gpt-5-codex"})
        self.assertEqual(apply_body["credentials"]["header_overrides"], {"x-route": "keep"})
        self.assertEqual(apply_body["credentials"]["refresh_token"], "new-local-rt")

    def test_refresh_sub2_rejects_non_openai_oauth_remote(self):
        self.client.post("/api/gpt-accounts/import", json={
            "text": "wrong-remote@example.com----chatgpt-password----JBSWY3DPEHPK3PXP",
        })
        account = self.client.get("/api/gpt-accounts").get_json()["items"][0]
        service = self.client.post("/api/sub2-services", json={
            "name": "错误类型服务", "api_base": "https://api.example.com", "admin_key": "test-admin-key",
        }).get_json()["service"]
        relay._update_account(account["id"], sub2_service_id=service["id"], sub2_account_id="99")
        listing = Mock(status_code=200, text="")
        listing.json.return_value = {"code": 0, "data": {"total": 1, "page": 1, "page_size": 100, "pages": 1, "items": [{
            "id": 99, "name": account["email"], "platform": "gemini", "type": "oauth", "status": "active",
        }]}}

        with patch("requests.get", return_value=listing), patch("requests.post") as post:
            with self.assertRaisesRegex(RuntimeError, "不是 OpenAI OAuth"):
                relay.refresh_sub2_account(account["id"])

        post.assert_not_called()

    def test_refresh_sub2_action_reports_partial_completion(self):
        self.client.post("/api/gpt-accounts/import", json={
            "text": "refresh-task@example.com----https://mail.example/code",
        })
        account = self.client.get("/api/gpt-accounts").get_json()["items"][0]
        job = {"id": "job-sub2-refresh", "account_id": account["id"], "email": account["email"],
               "action": "refresh_sub2", "status": "queued", "stage": "queued"}
        relay._write(relay._JOBS_PATH, [job])

        with patch.object(relay, "refresh_sub2_account", return_value={
            "ok": True, "partial": True, "remote_refreshed": False, "errors": ["远端刷新失败"],
        }):
            relay._run_maintenance_job(job["id"], account["id"], "refresh_sub2")

        stored_job = relay._read(relay._JOBS_PATH)[0]
        self.assertEqual(stored_job["status"], "success")
        self.assertIn("凭证已更新", stored_job["message"])
        self.assertIn("远端账号刷新失败", stored_job["message"])
        stored_account = relay._read(relay._ACCOUNTS_PATH)[0]
        self.assertEqual(stored_account["maintenance_status"], "partial")
        self.assertEqual(stored_account["maintenance_action"], "refresh_sub2")

    def test_new_account_actions_are_accepted_by_api(self):
        self.client.post("/api/gpt-accounts/import", json={
            "text": "account-1@example.com----chatgpt-password----JBSWY3DPEHPK3PXP",
        })
        account_id = self.client.get("/api/gpt-accounts").get_json()["items"][0]["id"]
        with patch.object(relay, "start_account_actions", return_value={"ok": True, "submitted": 1}) as start:
            for action in ("check_email_liveness", "check_gpt_liveness", "check_quota", "check_sub2_status", "refresh_sub2"):
                response = self.client.post("/api/gpt-accounts/actions", json={
                    "account_ids": [account_id], "action": action, "workers": 1,
                })
                self.assertEqual(response.status_code, 200)
            self.assertEqual([call.args[1] for call in start.call_args_list], [
                "check_email_liveness", "check_gpt_liveness", "check_quota", "check_sub2_status", "refresh_sub2",
            ])


if __name__ == "__main__":
    unittest.main()
