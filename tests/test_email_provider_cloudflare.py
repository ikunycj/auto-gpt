# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import cf_temp_mail_client, email_provider


class EmailProviderCloudflareTests(unittest.TestCase):
    def setUp(self):
        cf_temp_mail_client._CONTEXT_CACHE.clear()

    def test_parse_email_sources_includes_cloudflare(self):
        self.assertEqual(
            email_provider.parse_email_sources("cloudflare,gptmail"),
            ["cloudflare", "gptmail"],
        )

    def test_resolve_prefers_cf_context_over_domain_suffix(self):
        cf_temp_mail_client._CONTEXT_CACHE["x@custom.com"] = cf_temp_mail_client.CFTempMailAccount(
            email="x@custom.com",
            jwt="jwt",
            domain="custom.com",
        )
        with patch.object(email_provider, "parse_email_sources", return_value=["cloudflare_domain"]):
            # even if EMAIL_DOMAIN matches, cache wins
            from config import email as email_cfg
            with patch.object(email_cfg, "EMAIL_DOMAIN", "custom.com"):
                self.assertEqual(email_provider.resolve_email_source("x@custom.com"), "cloudflare")

    @patch("core.cf_temp_mail_client.pick_account")
    def test_pick_from_source_cloudflare(self, pick_account):
        pick_account.return_value = cf_temp_mail_client.CFTempMailAccount(
            email="n@mail.test", jwt="j"
        )
        self.assertEqual(email_provider._pick_from_source("cloudflare"), "n@mail.test")
        pick_account.assert_called_once()

    @patch("core.cf_temp_mail_client.release_account")
    def test_release_routes_to_cloudflare(self, release_account):
        cf_temp_mail_client._CONTEXT_CACHE["n@mail.test"] = cf_temp_mail_client.CFTempMailAccount(
            email="n@mail.test", jwt="j"
        )
        source = email_provider.release_email("n@mail.test", status="used", note="ok")
        self.assertEqual(source, "cloudflare")
        release_account.assert_called_once()


if __name__ == "__main__":
    unittest.main()
