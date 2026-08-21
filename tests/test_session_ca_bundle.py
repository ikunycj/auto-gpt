# -*- coding: utf-8 -*-
import os
import unittest
from unittest.mock import patch

from core.session import _apply_host_ca_bundle


class SessionCaBundleTests(unittest.TestCase):
    def test_chatgpt_uses_configured_bundle(self):
        with patch.dict(os.environ, {"CHATGPT_CA_BUNDLE": "/tmp/system-certs.pem"}):
            kwargs = _apply_host_ca_bundle("https://chatgpt.com/login", {})
        self.assertEqual(kwargs["verify"], "/tmp/system-certs.pem")

    def test_other_hosts_keep_default_verification(self):
        with patch.dict(os.environ, {"CHATGPT_CA_BUNDLE": "/tmp/system-certs.pem"}):
            kwargs = _apply_host_ca_bundle("https://auth.openai.com/log-in", {})
        self.assertNotIn("verify", kwargs)

    def test_explicit_verify_is_not_overridden(self):
        with patch.dict(os.environ, {"CHATGPT_CA_BUNDLE": "/tmp/system-certs.pem"}):
            kwargs = _apply_host_ca_bundle("https://chatgpt.com/login", {"verify": False})
        self.assertFalse(kwargs["verify"])


if __name__ == "__main__":
    unittest.main()
