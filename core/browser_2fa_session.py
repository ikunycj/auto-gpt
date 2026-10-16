# -*- coding: utf-8 -*-
"""Same-profile browser transport for the existing ChatGPT TOTP enrollment flow.

The registration browser, rather than a second HTTP/TLS client, owns all auth
and MFA requests.  Fetches run on the target origin; OAuth URLs are navigated
in the same tab so the browser maintains its own cookies and proxy route.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


_ALLOWED_HOSTS = frozenset({"auth.openai.com", "chatgpt.com"})
_ALLOWED_HEADERS = frozenset({"accept", "authorization", "content-type", "oai-device-id", "oai-language"})


@dataclass
class BrowserResponse:
    status_code: int
    url: str
    payload: Any = None

    def json(self) -> Any:
        if self.payload is None:
            raise RuntimeError("浏览器请求未返回 JSON")
        return self.payload

    def raise_for_status(self) -> None:
        if not 200 <= self.status_code < 400:
            raise RuntimeError(
                f"浏览器请求失败：HTTP {self.status_code}; url={self.url}; payload={self.payload!r}"
            )


class BrowserBacked2FASession:
    """Provide the methods used by ``setup_2fa`` through one live browser tab.

    All API calls must be same-origin, and only the two approved auth hosts may
    be visited. A browser navigation is used for OAuth redirects because fetch
    would follow the redirect cross-origin and lose the current page context.
    """

    def __init__(self, driver):
        from core.chatgpt_browser_login import _browser_cookies

        self.driver = driver
        did = next(
            (str(row.get("value") or "") for row in _browser_cookies(driver)
             if row.get("name") == "oai-did" and row.get("value")),
            "",
        )
        if not did:
            raise RuntimeError("无法读取注册浏览器的 oai-did，已停止 2FA 设置")
        self.device_id = did

    def navigator_language(self) -> str:
        try:
            return str(self.driver.execute_script("return navigator.language || ''") or "") or "en-US"
        except Exception:
            return "en-US"

    def get_nextauth_headers(self, **_kwargs) -> dict[str, str]:
        return {"accept": "application/json"}

    def get_chatgpt_headers(self, **_kwargs) -> dict[str, str]:
        return {"accept": "application/json", "content-type": "application/json"}

    def get_auth_headers(self, **_kwargs) -> dict[str, str]:
        return {"accept": "application/json", "content-type": "application/json"}

    def get_auth_navigate_headers(self, **_kwargs) -> dict[str, str]:
        return {}

    def get_chatgpt_navigate_headers(self, **_kwargs) -> dict[str, str]:
        return {}

    @staticmethod
    def _host(url: str) -> str:
        try:
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_HOSTS:
                raise ValueError("invalid host")
            if parsed.username or parsed.password or parsed.port:
                raise ValueError("invalid authority")
            return parsed.hostname
        except (TypeError, ValueError):
            raise RuntimeError("2FA 请求目标不是允许的 HTTPS 认证域名") from None

    def get(self, url: str, *, headers=None, allow_redirects=False) -> BrowserResponse:
        if allow_redirects:
            return self._navigate(url)
        return self._fetch("GET", url, headers=headers)

    def post(self, url: str, *, headers=None, data=None) -> BrowserResponse:
        return self._fetch("POST", url, headers=headers, data=data)

    def _navigate(self, url: str) -> BrowserResponse:
        self._host(url)
        try:
            self.driver.get(url)
            landed = str(self.driver.current_url or "")
            self._host(landed)
        except Exception:
            # Selenium errors can include sensitive OAuth URLs and codes.
            raise RuntimeError("2FA 浏览器认证跳转失败") from None
        return BrowserResponse(200, landed)

    def _fetch(self, method: str, url: str, *, headers=None, data=None) -> BrowserResponse:
        target_host = self._host(url)
        try:
            current_host = self._host(str(self.driver.current_url or ""))
        except RuntimeError:
            raise RuntimeError("2FA 请求时浏览器不在认证域名") from None
        if target_host != current_host:
            raise RuntimeError("2FA API 请求与当前浏览器页面不同源")
        safe_headers = {
            str(key).lower(): str(value)
            for key, value in (headers or {}).items()
            if str(key).lower() in _ALLOWED_HEADERS
        }
        try:
            result = self.driver.execute_async_script(
                """
                const [method, url, headers, body] = arguments;
                const done = arguments[arguments.length - 1];
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), 25000);
                fetch(url, {
                  method, headers, body: method === 'POST' ? body : undefined,
                  credentials: 'include', redirect: 'follow',
                  signal: controller.signal,
                }).then(async response => {
                  const contentType = response.headers.get('content-type') || '';
                  let payload = null;
                  if (contentType.includes('application/json')) {
                    try { payload = await response.json(); } catch (_) { /* invalid JSON */ }
                  }
                  done({ok: true, status: response.status, payload});
                }).catch(() => done({ok: false})).finally(() => clearTimeout(timer));
                """,
                method, url, safe_headers, data,
            )
        except Exception:
            raise RuntimeError("2FA 浏览器请求未完成") from None
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError("2FA 浏览器请求未完成")
        status = result.get("status")
        if not isinstance(status, int) or isinstance(status, bool):
            raise RuntimeError("2FA 浏览器请求未返回有效状态码")
        return BrowserResponse(status, url, result.get("payload"))
