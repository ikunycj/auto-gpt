# -*- coding: utf-8 -*-
import os
import unittest
from unittest.mock import patch

from config import env_loader
from apps.web import config_editor


class ConfigDefaultFallbackTests(unittest.TestCase):
    def test_blank_env_value_uses_default_for_all_supported_types(self):
        old_loaded = env_loader._LOADED
        env_loader._LOADED = True
        try:
            with patch.dict(os.environ, {
                "BOOL_KEY": "",
                "INT_KEY": "",
                "FLOAT_KEY": "",
                "STR_KEY": "",
                "LIST_KEY": "",
            }, clear=True):
                self.assertTrue(env_loader.env_bool("BOOL_KEY", True))
                self.assertEqual(env_loader.env_int("INT_KEY", 90), 90)
                self.assertEqual(env_loader.env_float("FLOAT_KEY", 1.5), 1.5)
                self.assertEqual(env_loader.env_str("STR_KEY", "default"), "default")
                self.assertEqual(env_loader.env_list("LIST_KEY", ["a"]), ["a"])
        finally:
            env_loader._LOADED = old_loaded

    def test_proxy_pool_blank_env_value_means_empty_list(self):
        old_loaded = env_loader._LOADED
        env_loader._LOADED = True
        namespace = {"PROXY_POOL": ["socks5://127.0.0.1:7897"]}
        try:
            with patch.dict(os.environ, {"PROXY_POOL": ""}, clear=True):
                env_loader.apply_env_overrides(namespace, {"PROXY_POOL": "list_str_multiline"})
        finally:
            env_loader._LOADED = old_loaded

        self.assertEqual(namespace["PROXY_POOL"], [])

    def test_config_editor_formats_empty_list_as_literal_empty_list(self):
        self.assertEqual(config_editor._format_env_value([], "list_str_multiline"), "[]")

    def test_apply_env_overrides_does_not_let_blank_values_mask_defaults(self):
        old_loaded = env_loader._LOADED
        env_loader._LOADED = True
        namespace = {"FEATURE_ENABLED": True, "BASE_URL": "https://example.test"}
        try:
            with patch.dict(os.environ, {"FEATURE_ENABLED": "", "BASE_URL": ""}, clear=True):
                env_loader.apply_env_overrides(namespace, {"FEATURE_ENABLED": "bool", "BASE_URL": "str"})
        finally:
            env_loader._LOADED = old_loaded

        self.assertTrue(namespace["FEATURE_ENABLED"])
        self.assertEqual(namespace["BASE_URL"], "https://example.test")

    def test_config_editor_parses_env_str_default_from_source(self):
        source = 'API_KEY: str = env_str("API_KEY", "fallback-key")\n'
        self.assertEqual(
            config_editor._parse_value_from_source(source, "API_KEY", "str"),
            "fallback-key",
        )

    def test_config_editor_blank_env_value_falls_back_to_source_default(self):
        self.assertEqual(
            config_editor._coerce_raw_value("", "wss://connect.browser-use.com", "str"),
            "wss://connect.browser-use.com",
        )
        self.assertTrue(config_editor._coerce_raw_value("", True, "bool"))

    def test_every_browser_field_is_assigned_to_one_webui_module(self):
        browser_keys = {
            field["key"]
            for field in config_editor.EDITABLE_FIELDS
            if field["group"] == "代理浏览器"
        }
        self.assertEqual(browser_keys, set(config_editor._BROWSER_FIELD_MODULE))
        counts = {
            module: list(config_editor._BROWSER_FIELD_MODULE.values()).count(module)
            for module in set(config_editor._BROWSER_FIELD_MODULE.values())
        }
        self.assertEqual(
            counts,
            {
                "general": 1,
                "roxy": 31,
                "cloak": 12,
                "browser_use": 14,
                "skyvern": 10,
                "system_chrome": 4,
                "locale": 4,
            },
        )

    def test_sms_fields_are_assigned_to_platform_sections(self):
        fields = {field["key"]: field for field in config_editor.get_config()}
        self.assertEqual(
            {
                key: (fields[key].get("sms_section"), fields[key].get("sms_channel"))
                for key in (
                    "SMS_POOL_PLATFORM_ENABLED",
                    "SMS_PROVIDER",
                    "SMS_API_BASE",
                    "SMS_API_KEY",
                    "L_API_BASE",
                    "L_ADMIN_AUTH_CODE",
                    "H_API_BASE",
                    "H_ADMIN_AUTH_CODE",
                )
            },
            {
                "SMS_POOL_PLATFORM_ENABLED": ("sms", "general"),
                "SMS_PROVIDER": ("sms", "general"),
                "SMS_API_BASE": ("sms", "grizzly"),
                "SMS_API_KEY": ("sms", "grizzly"),
                "L_API_BASE": ("sms", "l"),
                "L_ADMIN_AUTH_CODE": ("sms", "l"),
                "H_API_BASE": ("sms", "h"),
                "H_ADMIN_AUTH_CODE": ("sms", "h"),
            },
        )


if __name__ == "__main__":
    unittest.main()
