# -*- coding: utf-8 -*-
import unittest

from core.openai_auth import detect_account_unusable_response_body


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


if __name__ == "__main__":
    unittest.main()
