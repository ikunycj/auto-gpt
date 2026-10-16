"""Observe terminal authentication errors for local diagnosis."""
from __future__ import annotations

import base64
import json
from urllib.parse import unquote_plus, urlsplit

from core.openai_auth import detect_account_unusable_response_body, detect_account_unusable_text


# Install before form submit so the complete failing response can be inspected
# in the local browser session when authentication fails.
_AUTH_ERROR_SCRIPT = r"""
(() => {
  if (window.__accountErrorObserverInstalled) return;
  window.__accountErrorObserverInstalled = true;
  window.__accountAuthError = '';
  window.__accountAuthErrorDetails = null;
  const hit = url => {
    try {
      const u = new URL(url, location.href);
      return ['auth.openai.com', 'auth0.openai.com'].includes(u.hostname)
        && u.pathname.startsWith('/api/accounts/');
    } catch (_) { return false; }
  };
  const save = (url, status, body) => {
    if (!hit(url) || status < 400) return;
    window.__accountAuthErrorDetails = {url: String(url || ''), status, body: String(body || '')};
    const text = String(body || '').slice(0, 8192).toLowerCase().replace(/\s+/g, ' ');
    let code = '';
    try {
      const data = JSON.parse(text);
      code = data?.error?.code || data?.code || data?.error_code || '';
    } catch (_) {}
    const codes = ['account_deleted', 'account_deactivated', 'account_banned'];
    if (!codes.includes(code)) code = codes.find(c => text.includes(c)) || '';
    if (!code && text.includes('you do not have an account because it has been deleted or deactivated')) code = 'account_deactivated';
    if (!code && /account (has been|was) deleted|账号已删除|账户已删除/.test(text)) code = 'account_deleted';
    if (!code && /account (has been|was) deactivated|账号已停用|账户已停用/.test(text)) code = 'account_deactivated';
    if (code) window.__accountAuthError = code;
  };
  const fetch = window.fetch;
  if (fetch) window.fetch = async function(input, init) {
    const response = await fetch.apply(this, arguments);
    const url = typeof input === 'string' || input instanceof URL ? String(input) : input?.url;
    if (hit(url) && response.status >= 400) {
      try {
        response.clone().text().then(body => save(url, response.status, body)).catch(() => {});
      } catch (_) {}
    }
    return response;
  };
  const open = XMLHttpRequest.prototype.open;
  const send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(method, url) {
    this.__accountErrorUrl = url;
    return open.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function() {
    this.addEventListener('loadend', function() {
      try { save(this.__accountErrorUrl, this.status, this.responseText); } catch (_) {}
    }, {once: true});
    return send.apply(this, arguments);
  };
})();
"""


def install_auth_error_observer(driver) -> None:
    """Optional observer: lack of CDP/JS support must not break normal login."""
    cdp = getattr(driver, 'execute_cdp_cmd', None)
    if callable(cdp) and not getattr(driver, '_account_error_script_id', None):
        try:
            cdp('Network.enable', {})
            result = cdp('Page.addScriptToEvaluateOnNewDocument', {'source': _AUTH_ERROR_SCRIPT})
            if isinstance(result, dict):
                driver._account_error_script_id = result.get('identifier')
        except Exception:
            pass
    try:
        driver.execute_script(_AUTH_ERROR_SCRIPT)
    except Exception:
        pass


def _auth_response_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return parsed.hostname in {'auth.openai.com', 'auth0.openai.com'} and parsed.path.startswith('/api/accounts/')
    except (TypeError, ValueError):
        return False


def _network_account_error(driver) -> str:
    """Fallback for full-page navigation, which destroys the page's JS hook."""
    try:
        rows = driver.get_log('performance')
    except Exception:
        return ''
    if not isinstance(rows, list):
        return ''
    pending = getattr(driver, '_account_error_requests', {})
    if not isinstance(pending, dict):
        pending = {}
    for row in rows:
        try:
            message = json.loads(row.get('message', '{}')).get('message', {})
            params = message.get('params', {})
            request_id = params.get('requestId')
            response = params.get('response', {})
            if message.get('method') == 'Network.responseReceived' and request_id:
                if int(response.get('status', 0)) >= 400 and _auth_response_url(response.get('url', '')):
                    pending[request_id] = 0
        except (TypeError, ValueError, AttributeError):
            continue
    # Retry briefly when responseReceived preceded loadingFinished while keeping
    # the complete response body available for local diagnosis.
    pending = dict(list(pending.items())[-16:])
    for request_id, attempts in list(pending.items()):
        try:
            data = driver.execute_cdp_cmd('Network.getResponseBody', {'requestId': request_id})
            body = str(data.get('body') or '')
            if data.get('base64Encoded'):
                body = base64.b64decode(body).decode('utf-8', errors='replace')
            details = getattr(driver, '_account_error_details', [])
            if not isinstance(details, list):
                details = []
            details.append({"request_id": request_id, "body": body})
            driver._account_error_details = details[-16:]
            pending.pop(request_id, None)
            code = detect_account_unusable_response_body(body[:8192]) or detect_account_unusable_text(body[:8192])
            if code:
                driver._account_error_requests = {}
                return code
        except Exception:
            if attempts >= 2:
                pending.pop(request_id, None)
            else:
                pending[request_id] = attempts + 1
    driver._account_error_requests = pending
    return ''


def read_account_error(driver) -> str:
    """Read only terminal error signals; the caller decides how to display them."""
    try:
        state = driver.execute_script(r"""
        return {
          title: document.title || '',
          errors: [...document.querySelectorAll('[role="alert"], [aria-live="assertive"], [data-error]')]
            .slice(0, 8).map(el => (el.textContent || '').slice(0, 1000)).join(' '),
          error_code: window.__accountAuthError || ''
        };
        """)
    except Exception:
        state = {}
    if isinstance(state, dict):
        for key in ('error_code', 'title', 'errors'):
            code = detect_account_unusable_text(state.get(key, ''))
            if code:
                return code
    try:
        code = detect_account_unusable_text(unquote_plus(str(driver.current_url or '')))
        if code:
            return code
    except Exception:
        pass
    return _network_account_error(driver)


def read_account_error_details(driver) -> list[dict]:
    """Return complete captured authentication error responses for local review."""
    details = getattr(driver, "_account_error_details", None)
    if isinstance(details, list):
        return details
    try:
        value = driver.execute_script("return window.__accountAuthErrorDetails || null")
    except Exception:
        value = None
    return [value] if isinstance(value, dict) else []


def describe_browser_network_error(error: object) -> str:
    """Return the complete browser network error for local auditing."""
    return str(error or "")
