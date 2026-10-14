"""Authentication errors take precedence without keeping sensitive responses."""
import base64
import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from core import browser_auth_errors as errors
from core import roxy_codex_oauth as oauth
from registration.drivers.roxy import implementation as registration


@pytest.mark.parametrize("state,url,expected", [
    ({"title": "Your account has been deleted."}, "https://auth.openai.com/mfa", "account_deleted"),
    ({"errors": "Your account has been deactivated."}, "https://auth.openai.com/mfa", "account_deactivated"),
    ({"error_code": "account_banned"}, "https://auth.openai.com/mfa", "account_banned"),
    ({}, "https://auth.openai.com/error?error=account_deleted", "account_deleted"),
    ({}, "https://auth.openai.com/error?error=Your+account+has+been+deactivated.", "account_deactivated"),
    ({"errors": "Invalid code. Enter the code from your authenticator."}, "https://auth.openai.com/mfa", ""),
])
def test_reads_terminal_title_alert_observer_and_url(state, url, expected):
    driver = SimpleNamespace(current_url=url, execute_script=lambda _s: state)
    assert errors.read_account_error(driver) == expected


def test_observer_is_optional_and_cdp_installation_is_idempotent():
    errors.install_auth_error_observer(object())
    assert errors.read_account_error(object()) == ""
    calls = []

    def cdp(command, params):
        calls.append(command)
        return {"identifier": "test-id"}

    driver = SimpleNamespace(execute_cdp_cmd=cdp, execute_script=lambda _s: None)
    errors.install_auth_error_observer(driver)
    errors.install_auth_error_observer(driver)
    assert calls == ["Network.enable", "Page.addScriptToEvaluateOnNewDocument"]


class NetworkDriver:
    current_url = "https://auth.openai.com/mfa"

    def __init__(self, body, *, url="https://auth.openai.com/api/accounts/mfa/totp/validate", status=403, base64_encoded=False, retries=0):
        self.body = body
        self.base64_encoded = base64_encoded
        self.retries = retries
        self.calls = []
        self.rows = [{"message": json.dumps({"message": {
            "method": "Network.responseReceived",
            "params": {"requestId": "request-1", "response": {"url": url, "status": status}},
        }})}]

    def execute_script(self, _script):
        return {}

    def get_log(self, kind):
        assert kind == "performance"
        rows, self.rows = self.rows, []
        return rows

    def execute_cdp_cmd(self, command, params):
        assert command == "Network.getResponseBody"
        self.calls.append(params["requestId"])
        if self.retries:
            self.retries -= 1
            raise RuntimeError("body not available yet")
        return {"body": self.body, "base64Encoded": self.base64_encoded}


def test_cdp_body_retried_then_decoded_without_retaining_response():
    body = json.dumps({"error": {"code": "account_deleted"}, "access_token": "private-token"})
    driver = NetworkDriver(base64.b64encode(body.encode()).decode(), base64_encoded=True, retries=1)
    assert errors.read_account_error(driver) == ""
    assert driver._account_error_requests == {"request-1": 1}
    assert errors.read_account_error(driver) == "account_deleted"
    assert driver._account_error_requests == {}
    assert driver.calls == ["request-1", "request-1"]


@pytest.mark.parametrize("url,status,body", [
    ("https://evil.example/api/accounts/mfa", 403, '{"error":{"code":"account_deleted"}}'),
    ("https://auth.openai.com.evil.example/api/accounts/mfa", 403, '{"code":"account_deleted"}'),
    ("https://auth.openai.com/api/accounts/mfa", 200, '{"code":"account_deleted"}'),
    ("https://auth.openai.com/api/accounts/mfa", 400, '{"error":{"code":"invalid_code"}}'),
    ("https://auth.openai.com/api/accounts/mfa", 403, 'Verify you are human'),
])
def test_cdp_does_not_mislabel_other_errors(url, status, body):
    driver = NetworkDriver(body, url=url, status=status)
    assert errors.read_account_error(driver) == ""
    assert driver._account_error_requests == {}
    if status < 400 or not errors._auth_response_url(url):
        assert driver.calls == []


def test_cdp_unavailable_body_has_bounded_retries():
    driver = NetworkDriver("", retries=100)
    for _ in range(10):
        assert errors.read_account_error(driver) == ""
    assert len(driver.calls) == 3
    assert driver._account_error_requests == {}


@pytest.mark.parametrize("code", [
    "ERR_TUNNEL_CONNECTION_FAILED", "ERR_PROXY_CONNECTION_FAILED",
    "ERR_SOCKS_CONNECTION_FAILED", "ERR_SSL_PROTOCOL_ERROR",
])
@pytest.mark.parametrize("formatter", [oauth._redact_diagnostic_text, registration._safe_exception_text])
def test_network_error_keeps_actionable_code_without_secrets(code, formatter):
    message = formatter(f"Message: net::{code} https://user:proxy-secret@proxy.example/?token=oauth-secret Stacktrace: 0 chromedriver")
    assert code in message
    assert "未自动切换为直连" in message
    assert "不能用于判断账号是否停用" in message
    for secret in ("proxy-secret", "oauth-secret", "proxy.example", "Stacktrace", "chromedriver"):
        assert secret not in message


@pytest.mark.parametrize("formatter", [oauth._redact_diagnostic_text, registration._safe_exception_text])
def test_phone_ip_and_browser_version_are_not_masked(formatter):
    message = formatter("phone=+8613812345678 debugger=127.0.0.1:51471 chrome=154.0.8037.44 token=private-value")
    assert "+8613812345678" in message
    assert "127.0.0.1:51471" in message
    assert "154.0.8037.44" in message
    assert "private-value" not in message


def test_javascript_observer_preserves_fetch_xhr_and_only_saves_whitelisted_code():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to execute the browser observer in an isolated VM")
    script = r'''
const vm = require('node:vm');
const assert = require('node:assert/strict');
const observer = OBSERVER;
const AUTH = 'https://auth.openai.com/api/accounts/mfa/validate';
let response;
class XHR {
  open(method, url) { this.opened = url; }
  addEventListener(event, callback) { this.callback = callback; }
  send() { this.callback(); }
}
const context = {URL, XMLHttpRequest: XHR, location: {href: AUTH}};
context.window = context;
const rejection = new Error('fetch failed');
context.fetch = async () => { if (response === rejection) throw rejection; return response; };
vm.createContext(context);
vm.runInContext(observer, context);
const wrappedFetch = context.fetch;
vm.runInContext(observer, context);
assert.equal(context.fetch, wrappedFetch);
async function check(url, status, body, expected, brokenClone = false) {
  context.__accountAuthError = '';
  response = {status, clone() { if (brokenClone) throw new Error('opaque'); return {text: async () => body}; }};
  assert.equal(await context.fetch(url), response);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(context.__accountAuthError, expected);
}
(async () => {
  await check(AUTH, 403, '{"error":{"code":"account_deleted"},"token":"private-token"}', 'account_deleted');
  await check({url: AUTH}, 403, '{"code":"account_banned"}', 'account_banned');
  await check(new URL(AUTH), 403, 'You do not have an account because it has been deleted or deactivated.', 'account_deactivated');
  await check(AUTH, 200, '{"access_token":"private-token","code":"account_deleted"}', '');
  await check(AUTH, 400, '{"error":{"code":"invalid_code"}}', '');
  await check('https://evil.example/api/accounts/mfa', 403, 'account_deleted', '');
  await check(AUTH, 403, 'account_deleted', '', true);
  const xhr = new context.XMLHttpRequest();
  xhr.open('POST', AUTH); xhr.status = 403; xhr.responseText = '{"code":"account_banned","token":"private-token"}';
  xhr.send();
  assert.equal(context.__accountAuthError, 'account_banned');
  assert.deepEqual(Object.keys(context).filter(k => k.startsWith('__account')).sort(), ['__accountAuthError', '__accountErrorObserverInstalled']);
  response = rejection;
  await assert.rejects(context.fetch(AUTH), error => error === rejection);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''.replace("OBSERVER", json.dumps(errors._AUTH_ERROR_SCRIPT), 1)
    completed = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
