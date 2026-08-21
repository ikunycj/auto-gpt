# -*- coding: utf-8 -*-
import unittest

from core.openai_auth import detect_account_unusable_response_body
from core.browser_use_codex_oauth import _wait_after_email_submit


class CodexDeadAccountDetectionTests(unittest.TestCase):
    def test_detect_account_unusable_response_body_uses_error_code(self):
        self.assertEqual(
            detect_account_unusable_response_body('{"error":{"code":"account_deactivated"}}'),
            "account_deactivated",
        )
        self.assertEqual(
            detect_account_unusable_response_body('{"error":{"code":"account_deleted"}}'),
            "account_deleted",
        )
        self.assertEqual(detect_account_unusable_response_body('Your account has been deactivated.'), "")

    def test_browser_use_email_submit_returns_deactivated_from_response_tracker(self):
        class Body:
            def inner_text(self, timeout=1000):
                return "Your account has been deactivated."

        class Page:
            url = "https://auth.openai.com/email-verification"
            def locator(self, selector):
                return Body()

        tracker = {"code": "account_deactivated"}
        self.assertEqual(_wait_after_email_submit(Page(), timeout=1, dead_tracker=tracker), "deactivated:account_deactivated")
        self.assertNotEqual(_wait_after_email_submit(Page(), timeout=1, dead_tracker={}), "deactivated:account_deactivated")


if __name__ == "__main__":
    unittest.main()
