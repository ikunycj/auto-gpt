# -*- coding: utf-8 -*-
"""通过 RoxyBrowser 指纹浏览器 + Selenium 执行 ChatGPT 注册。"""
from __future__ import annotations

import logging
import json
import random
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from config import roxybrowser as _cfg
from config import twofa as _twofa_cfg
from core.account_export import save_account_data, setup_2fa
from core.chatgpt_browser_login import verify_password_totp_login
from core.browser_auth_errors import describe_browser_network_error
from core.email_provider import wait_for_otp, resolve_email_source
from core.humanize import delay as human_delay
from core.roxybrowser_client import RoxyBrowserClient, RoxyOpenResult
from registration.application.passwords import generate_registration_password, registration_password
from registration.ports.stop_signal import check_stop_requested

logger = logging.getLogger(__name__)


class RegistrationPasswordSubmissionError(RuntimeError):
    """Password submission reached the browser; remote creation is uncertain."""


class _RegistrationRequestMonitor:
    """Observe only the registration response metadata in Chrome performance logs.

    Selenium's performance log gives us CDP Network events without inspecting
    request bodies, response bodies, cookies or authorization headers.  Some
    remote WebDriver implementations do not expose this log; then page state
    still governs the normal submission timeout.
    """

    _REGISTER_PATH = "/api/accounts/user/register"
    _HOSTS = {"auth.openai.com", "chatgpt.com"}

    def __init__(self, driver):
        self.driver = driver
        self.available = callable(getattr(driver, "get_log", None))
        self._requests: dict[str, str] = {}
        self.last_response: dict | None = None
        self._last_resource_id: str | None = None
        self._resource_baseline: str | None = None
        self._page = getattr(driver, "page", None)
        if self._page is not None and callable(getattr(self._page, "on", None)):
            try:
                self._page.on("response", self._on_page_response)
            except Exception:
                self._page = None
        if self.available:
            self.poll()  # Discard events from earlier page loads.
            self._requests.clear()
        else:
            self._resource_timing_response()
        # Resource Timing retains prior submissions on the current document.
        # A new attempt must not read the previous 400 as its own response.
        self._resource_baseline = self._last_resource_id
        self.last_response = None

    @classmethod
    def _is_register_url(cls, url: object) -> bool:
        try:
            parsed = urlsplit(str(url or ""))
            return parsed.hostname in cls._HOSTS and parsed.path == cls._REGISTER_PATH
        except (TypeError, ValueError):
            return False

    def _on_page_response(self, response) -> None:
        try:
            if not self._is_register_url(response.url):
                return
            headers = response.headers
            content_type = str(headers.get("content-type") or "").split(";", 1)[0].strip().lower()
            if not re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", content_type):
                content_type = "unknown"
            method = str(response.request.method or "").upper()
            self.last_response = {
                "method": method if method in {"GET", "POST"} else "unknown",
                "status": int(response.status),
                "content_type": content_type,
                "challenge_detected": "chlray" in str(headers.get("server-timing") or ""),
            }
        except Exception:
            pass

    def _resource_timing_response(self) -> dict | None:
        """Chrome fallback when Selenium does not expose performance events."""
        try:
            result = self.driver.execute_script(r"""
            const entry = performance.getEntriesByType('resource').filter(item => {
              try {
                const url = new URL(item.name);
                return (url.hostname === 'auth.openai.com' || url.hostname === 'chatgpt.com')
                  && url.pathname === '/api/accounts/user/register';
              } catch (_) { return false; }
            }).at(-1);
            if (!entry) return null;
            return {
              resource_id: `${performance.timeOrigin}:${entry.startTime}`,
              status: entry.responseStatus || 0,
              content_type: entry.contentType || '',
              challenge_detected: [...(entry.serverTiming || [])].some(item => item.name === 'chlray')
            };
            """)
        except Exception:
            return None
        if not isinstance(result, dict):
            return None
        resource_id = str(result.get("resource_id") or "")
        if resource_id:
            self._last_resource_id = resource_id
            if resource_id == self._resource_baseline:
                return None
        try:
            status = int(result.get("status") or 0)
        except (TypeError, ValueError):
            return None
        if status < 100:
            return None
        content_type = str(result.get("content_type") or "").split(";", 1)[0].strip().lower()
        if not re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", content_type):
            content_type = "unknown"
        return {
            "method": "unknown", "status": status,
            "content_type": content_type,
            "challenge_detected": result.get("challenge_detected") is True,
        }

    def poll(self) -> dict | None:
        if self.available:
            try:
                rows = self.driver.get_log("performance")
            except Exception:
                self.available = False
                rows = []
        else:
            rows = []
        for row in rows:
            try:
                message = json.loads(row.get("message", "")).get("message", {})
                event, params = message.get("method"), message.get("params", {})
                request_id = str(params.get("requestId") or "")
                if event == "Network.requestWillBeSent":
                    request = params.get("request") or {}
                    if self._is_register_url(request.get("url")) and request_id:
                        method = str(request.get("method") or "").upper()
                        self._requests[request_id] = method if method in {"GET", "POST"} else "unknown"
                elif event == "Network.responseReceived":
                    response = params.get("response") or {}
                    if not self._is_register_url(response.get("url")):
                        continue
                    status = int(response.get("status") or 0)
                    headers = response.get("headers") or {}
                    challenge_detected = any(
                        str(key).lower() == "server-timing" and "chlray" in str(value)
                        for key, value in headers.items()
                    )
                    content_type = next(
                        (str(value).split(";", 1)[0].strip().lower()
                         for key, value in headers.items() if str(key).lower() == "content-type"),
                        str(response.get("mimeType") or "").strip().lower(),
                    )
                    # MIME only: no untrusted header values, URLs, bodies or secrets.
                    if not re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", content_type):
                        content_type = "unknown"
                    self.last_response = {
                        "method": self._requests.get(request_id, "unknown"),
                        "status": status,
                        "content_type": content_type,
                        "challenge_detected": challenge_detected,
                    }
            except (AttributeError, TypeError, ValueError):
                continue
        if self.last_response is None:
            self.last_response = self._resource_timing_response()
        return self.last_response

    def close(self) -> None:
        if self._page is not None:
            try:
                self._page.remove_listener("response", self._on_page_response)
            except Exception:
                pass


def _password_submission_route_error(driver) -> bool:
    """Detect the known client-side route error without reading the page text."""
    try:
        return bool(driver.execute_script("""
        const text = document.body ? document.body.innerText : '';
        return text.includes('Route Error') && text.includes('Invalid content type');
        """))
    except Exception:
        return False


def _click_route_error_try_again(driver) -> bool:
    """Retry only the visible recovery action on the known Route Error page."""
    try:
        result = driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
        const retry = [...document.querySelectorAll('button, a, input[type="button"], input[type="submit"]')]
          .find(el => {
            const label = String(el.textContent || el.value || el.getAttribute('aria-label') || '').trim().toLowerCase();
            return visible(el) && /^(try again|retry|重试|再试一次)$/.test(label);
          });
        if (!retry) return {ok:false};
        retry.scrollIntoView({block:'center'});
        return {ok:true, button:retry};
        """) or {}
        if not result.get("ok") or not result.get("button"):
            return False
        _human_click(driver, result["button"], label="route_error_retry")
        return True
    except Exception:
        return False


def _log_prefix(driver=None) -> str:
    """按当前浏览器实现返回注册日志前缀。

    CloakBrowser 复用 Roxy 的页面操作函数；这些共享函数必须跟随实际 driver
    输出 `[Cloak注册]`，避免 Cloak 流程里混入 `[Roxy注册]` 日志。
    """
    try:
        explicit = str(getattr(driver, "_registration_log_prefix", "") or "").strip()
        if explicit:
            return explicit
        if driver is not None and driver.__class__.__name__ == "CloakSeleniumDriver":
            return "[Cloak注册]"
    except Exception:
        pass
    return "[Roxy注册]"


def _diagnostic_route(url: object) -> str:
    """Give logs a route label without query strings, fragments or opaque path tokens."""
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(str(url or ""))
        path = (parsed.path or "").lower()
        for route in (
            "/create-account/password", "/log-in/password", "/email-verification",
            "/signup/profile", "/create-account/profile", "/about-you", "/profile",
            "/add-phone", "/phone-verification", "/authorize", "/consent",
            "/workspace", "/auth/callback", "/auth/login",
        ):
            if path == route or path.startswith(route + "/"):
                return route
        if path == "/":
            return "/"
    except Exception:
        pass
    return "[其他页面]"


_DIAGNOSTIC_URL_RE = re.compile(r"(?i)\b(?:https?|chrome-error)://[^\s<>\"']+")
_DIAGNOSTIC_EMAIL_RE = re.compile(r"(?i)\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_DIAGNOSTIC_SECRET_RE = re.compile(
    r"(?i)(\b(?:password|passwd|token|secret|authorization|cookie|otp|code|access_token|refresh_token)\b\s*[:=]\s*)([^\s,;]+)"
)
_DIAGNOSTIC_LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_-]{32,}\b")


def _safe_exception_text(exc: object, limit: int = 240) -> str:
    """Keep ordinary error labels while removing values from browser/provider errors."""
    network_error = describe_browser_network_error(exc)
    if network_error:
        return network_error[:limit]
    text = " ".join(str(exc or "").split())
    if not text:
        return "-"

    def redact_url(match: re.Match) -> str:
        return f"route={_diagnostic_route(match.group(0))}"

    text = _DIAGNOSTIC_URL_RE.sub(redact_url, text)
    text = _DIAGNOSTIC_EMAIL_RE.sub("[邮箱]", text)
    text = _DIAGNOSTIC_SECRET_RE.sub(lambda match: f"{match.group(1)}[已隐藏]", text)
    text = _DIAGNOSTIC_LONG_TOKEN_RE.sub("[长令牌已隐藏]", text)
    text = text.replace("\x00", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


def _diagnostic_state(state: object) -> dict:
    """Summarize untrusted DOM/JS results without any input, body or URL values."""
    if not isinstance(state, dict):
        return {"state": "unavailable"}
    summary = {"route": _diagnostic_route(state.get("url"))}
    for key in ("inputs", "buttons", "widgets", "forms", "actions", "errors"):
        if isinstance(state.get(key), list):
            summary[f"{key}_count"] = len(state[key])
    invalid = sum(
        str(item.get("ariaInvalid") or "").lower() == "true"
        for item in (state.get("inputs") or []) if isinstance(item, dict)
    )
    if invalid:
        summary["invalid_inputs"] = invalid
    for key in ("ok", "hasForm", "hasSubmit"):
        if isinstance(state.get(key), bool):
            summary[key] = state[key]
    if type(state.get("status")) is int:
        summary["status"] = state["status"]
    # Only page-script labels may be included. An arbitrary DOM/exception reason
    # could itself contain an OTP, password or redirect query parameter.
    reason = state.get("reason")
    if isinstance(reason, str) and reason in {
        "missing_email_input", "email_value_not_ready", "no_safe_submit",
        "no_safe_global_action", "ambiguous_submit", "primary_submit",
        "safe_submit", "empty_email", "missing_form", "missing_safe_submit",
        "stable_async_enter_click", "resubmitted_email_form",
        "missing_continue_with_password", "clicked_continue_with_password",
        "continue_with_password_did_not_open_password_page",
        "missing_password_input", "missing_submit", "password_targets",
        "missing_passwordless_button", "passwordless_send_otp_target",
        "clicked_passwordless_send_otp", "empty_result",
    }:
        summary["reason"] = reason
    if state.get("error"):
        summary["error"] = "页面检查失败"
    return summary


def _webdriver_rejected_performance_logging(exc: object) -> bool:
    """识别仅由 ``goog:loggingPrefs`` 能力不兼容导致的建连失败。"""
    text = str(exc or "").lower()
    markers = (
        "goog:loggingprefs",
        "loggingprefs",
        "unknown capability",
        "unrecognized capability",
        "unexpected capability",
        "invalid capability",
    )
    return any(marker in text for marker in markers)


def _roxy_options(Options, debugger_address: str | None = None, *, performance_logs: bool = True):
    options = Options()
    # 页面里长轮询/风控脚本偶尔会让 driver.get 等到超时；eager 只等 DOMContentLoaded。
    options.page_load_strategy = "eager"
    if performance_logs:
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    if debugger_address:
        options.add_experimental_option("debuggerAddress", debugger_address)
    return options


def _connect_roxy_webdriver(connect, options_factory):
    """连接 Roxy WebDriver；性能日志不是 Roxy 的必需能力。"""
    try:
        return connect(options_factory(True))
    except Exception as exc:
        if not _webdriver_rejected_performance_logging(exc):
            raise
        logger.warning(
            "[Roxy] WebDriver 不支持性能日志能力，改用页面性能计时回退：%s",
            type(exc).__name__,
        )
        return connect(options_factory(False))


def _build_driver(opened: RoxyOpenResult):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.remote.webdriver import WebDriver as RemoteWebDriver

    if opened.debugger_address:
        logger.info("[Roxy] Selenium 连接 debuggerAddress=%s", opened.debugger_address)
        driver_path = ""
        try:
            raw_data = opened.raw.get("data") if isinstance(opened.raw, dict) else {}
            if isinstance(raw_data, dict):
                driver_path = str(raw_data.get("driver") or raw_data.get("driverPath") or raw_data.get("driver_path") or "").strip()
        except Exception:
            driver_path = ""

        def connect(options):
            if driver_path:
                logger.info("[Roxy] 使用 Roxy chromedriver=%s", driver_path)
                return webdriver.Chrome(service=Service(executable_path=driver_path), options=options)
            return webdriver.Chrome(options=options)

        driver = _connect_roxy_webdriver(
            connect,
            lambda performance_logs: _roxy_options(
                Options,
                opened.debugger_address,
                performance_logs=performance_logs,
            ),
        )
        _apply_browser_automation_mask(driver)
        return driver

    if opened.webdriver_url:
        logger.info("[Roxy] Selenium 已连接远程 WebDriver")
        driver = _connect_roxy_webdriver(
            lambda options: RemoteWebDriver(command_executor=opened.webdriver_url, options=options),
            lambda performance_logs: _roxy_options(Options, performance_logs=performance_logs),
        )
        _apply_browser_automation_mask(driver)
        return driver

    raise RuntimeError("Roxy 未返回可连接的 Selenium 地址")


def _clear_chatgpt_auth_state(driver) -> None:
    """清理 Roxy 注册 Profile 的旧 ChatGPT 登录态。

    固定 Roxy Profile 会在 ``ROXY_KEEP_BROWSER_OPEN=True`` 时复用同一个
    Chrome 用户目录；如果上一次任务留下了登录 cookie，打开
    ``/auth/login`` 会被重定向到 ``chatgpt.com/``，注册流程看不到邮箱输入框，
    也就无法进入密码和 2FA 阶段。注册任务必须从未登录状态开始，因此这里同时
    清理浏览器 cookie 和 ChatGPT/Auth 两个站点的存储。
    """
    cdp = getattr(driver, "execute_cdp_cmd", None)
    if callable(cdp):
        try:
            cdp("Network.clearBrowserCookies", {})
        except Exception as exc:
            logger.debug("[Roxy注册] 清理浏览器 cookie 失败，继续使用 Selenium 清理：%s", type(exc).__name__)
        for origin in ("https://chatgpt.com", "https://auth.openai.com"):
            try:
                cdp(
                    "Storage.clearDataForOrigin",
                    {"origin": origin, "storageTypes": "all"},
                )
            except Exception as exc:
                logger.debug(
                    "[Roxy注册] 清理站点存储失败：origin=%s error=%s",
                    origin,
                    type(exc).__name__,
                )

    delete_cookies = getattr(driver, "delete_all_cookies", None)
    if callable(delete_cookies):
        try:
            delete_cookies()
        except Exception as exc:
            logger.debug("[Roxy注册] Selenium cookie 清理失败：%s", type(exc).__name__)

    # CDP Storage API 在旧版 Roxy/Chrome 上可能不可用；对当前页面再做一次
    # best-effort 的 Web Storage 清理，不能让兼容性问题阻断注册。
    execute_script = getattr(driver, "execute_script", None)
    if callable(execute_script):
        try:
            execute_script(
                "try { localStorage.clear(); } catch (_) {}"
                "try { sessionStorage.clear(); } catch (_) {}"
            )
        except Exception as exc:
            logger.debug("[Roxy注册] Web Storage 清理失败：%s", type(exc).__name__)
    logger.info("[Roxy注册] 已清理 Profile 的 ChatGPT 登录态，开始全新注册")


def _center_browser_window(driver) -> None:
    """把可见的 Roxy 窗口移动到 Windows 主屏工作区中央。"""
    if bool(getattr(_cfg, "ROXY_OPEN_HEADLESS", False)):
        return
    try:
        import platform
        if platform.system().lower() != "windows":
            return
        import ctypes

        class _Rect(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        work_area = _Rect()
        if not ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work_area), 0):
            raise OSError("无法读取 Windows 工作区")
        size = driver.get_window_size()
        width = max(1, int(size.get("width") or 1))
        height = max(1, int(size.get("height") or 1))
        x = int(work_area.left + max(0, (work_area.right - work_area.left - width) // 2))
        y = int(work_area.top + max(0, (work_area.bottom - work_area.top - height) // 2))
        driver.set_window_position(x, y)
        logger.info("[Roxy] 浏览器窗口已居中：x=%s y=%s width=%s height=%s", x, y, width, height)
    except Exception as exc:
        logger.warning("[Roxy] 浏览器窗口居中失败，继续执行：%s", _safe_exception_text(exc))


def _wait(driver, timeout: int | None = None):
    from selenium.webdriver.support.ui import WebDriverWait
    return WebDriverWait(driver, timeout or int(_cfg.ROXY_SELENIUM_TIMEOUT))


def _safe_get(driver, url: str, *, timeout: int = 45, attempts: int = 2, accept_hosts: tuple[str, ...] = ()) -> None:
    """带容错的页面跳转。

    Roxy/Chrome 150 偶发 `Timed out receiving message from renderer`，实际页面可能已经可用。
    这里超时后先 `window.stop()`，只要当前 URL/DOM 已进入目标页就继续；否则重试一次。
    """
    from selenium.common.exceptions import TimeoutException, WebDriverException

    last_exc: Exception | None = None
    old_timeout = int(getattr(_cfg, "ROXY_SELENIUM_TIMEOUT", 90) or 90)
    hosts = tuple(h.lower() for h in (accept_hosts or ()))
    for attempt in range(1, max(1, attempts) + 1):
        try:
            try:
                driver.set_page_load_timeout(max(10, int(timeout)))
                driver.set_script_timeout(8)
            except Exception:
                pass
            driver.get(url)
            return
        except TimeoutException as exc:
            last_exc = exc
            logger.warning(
                "%s 页面加载超时，尝试停止加载后检查 DOM：route=%s attempt=%s/%s error=%s",
                _log_prefix(driver), _diagnostic_route(url), attempt, attempts, type(exc).__name__,
            )
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
            time.sleep(1.0)
            try:
                current = str(driver.current_url or "").lower()
            except Exception:
                current = ""
            try:
                ready = str(driver.execute_script("return document.readyState || ''") or "")
                has_body = bool(driver.execute_script("return !!document.body"))
            except Exception:
                ready = ""
                has_body = False
            target_ok = any(h in current for h in hosts) if hosts else (url.split("/", 3)[2].lower() in current)
            if target_ok and has_body:
                logger.info(
                    "%s 页面加载虽超时但 DOM 可用，继续流程：route=%s readyState=%s",
                    _log_prefix(driver), _diagnostic_route(current), ready or "-",
                )
                return
            if attempt < attempts:
                try:
                    driver.get("about:blank")
                except Exception:
                    pass
                time.sleep(1.5 * attempt)
                continue
        except WebDriverException as exc:
            last_exc = exc
            if attempt < attempts:
                logger.warning(
                    "%s 页面跳转失败，准备重试：route=%s attempt=%s/%s error=%s",
                    _log_prefix(driver), _diagnostic_route(url), attempt, attempts, type(exc).__name__,
                )
                time.sleep(1.5 * attempt)
                continue
            raise RuntimeError(
                f"页面跳转失败: route={_diagnostic_route(url)} error={type(exc).__name__}"
            ) from exc
        finally:
            try:
                driver.set_page_load_timeout(old_timeout)
            except Exception:
                pass
    if last_exc is not None:
        raise RuntimeError(
            f"页面跳转失败: route={_diagnostic_route(url)} error={type(last_exc).__name__}"
        ) from last_exc
    raise RuntimeError(f"页面跳转失败: route={_diagnostic_route(url)}")


def _visible(el) -> bool:
    try:
        return el.is_displayed() and el.is_enabled()
    except Exception:
        return False


def _browser_actions_enabled() -> bool:
    try:
        from config import humanize as _hcfg
        return bool(getattr(_hcfg, "ENABLE_HUMANIZE_BROWSER_ACTIONS", True))
    except Exception:
        return True


def _apply_browser_automation_mask(driver) -> None:
    """连接 Selenium 后尽量降低明显自动化特征；失败不影响主流程。"""
    if not _browser_actions_enabled():
        return
    try:
        script = r"""
        (() => {
          try {
            Object.defineProperty(Navigator.prototype, 'webdriver', {
              configurable: true, get: () => undefined
            });
          } catch (_) {}
          if (!window.chrome) window.chrome = {};
          if (!window.chrome.runtime) window.chrome.runtime = {};
          const permissions = window.navigator.permissions;
          if (permissions && permissions.query && !permissions.query.__autoGptWrapped) {
            const originalQuery = permissions.query.bind(permissions);
            const wrappedQuery = parameters => (
              parameters && parameters.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : originalQuery(parameters)
            );
            Object.defineProperty(wrappedQuery, '__autoGptWrapped', {value: true});
            permissions.query = wrappedQuery;
          }
        })();
        """
        if hasattr(driver, "execute_cdp_cmd"):
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": script})
        try:
            driver.execute_script(script)
        except Exception:
            pass
        logger.info("%s 已注入浏览器自动化特征弱化脚本", _log_prefix(driver))
    except Exception as exc:
        logger.debug("%s 注入自动化特征弱化脚本失败：%s", _log_prefix(driver), _safe_exception_text(exc))


def _human_scroll_to(driver, el) -> None:
    try:
        block = random.choice(["center", "nearest", "center"])
        driver.execute_script("arguments[0].scrollIntoView({block: arguments[1], inline:'nearest'});", el, block)
        if _browser_actions_enabled():
            time.sleep(random.uniform(0.08, 0.35))
            # 轻微滚动抖动，避免每次都精准居中。
            driver.execute_script("window.scrollBy(0, arguments[0]);", random.randint(-90, 90))
            time.sleep(random.uniform(0.05, 0.22))
            driver.execute_script("arguments[0].scrollIntoView({block:'center', inline:'nearest'});", el)
    except Exception:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        except Exception:
            pass


def _human_click(driver, el, *, label: str = "") -> None:
    """快速人工化点击。

    之前用 ActionChains 在 Roxy/Chrome 150 上偶发卡住 1-2 分钟，导致邮箱提交很慢。
    这里改为 CDP 派发鼠标事件；没有 CDP 时再用 JS/原生 click 兜底。
    """
    _human_scroll_to(driver, el)
    if not _browser_actions_enabled():
        time.sleep(0.2)
        el.click()
        return
    try:
        human_delay("click")
        point = driver.execute_script(r"""
        const el = arguments[0];
        const r = el.getBoundingClientRect();
        const x = r.left + r.width * (0.30 + Math.random() * 0.40);
        const y = r.top + r.height * (0.35 + Math.random() * 0.30);
        return {x, y, w:r.width, h:r.height};
        """, el) or {}
        x = float(point.get("x") or 0)
        y = float(point.get("y") or 0)
        if hasattr(driver, "execute_cdp_cmd") and x > 0 and y > 0:
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
            time.sleep(random.uniform(0.05, 0.22))
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
            time.sleep(random.uniform(0.035, 0.13))
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
        else:
            driver.execute_script(r"""
            const el = arguments[0];
            el.dispatchEvent(new PointerEvent('pointerdown', {bubbles:true, cancelable:true, pointerType:'mouse'}));
            el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
            el.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true, view:window}));
            el.click();
            """, el)
    except Exception as exc:
        logger.debug("%s 人工化点击失败，回退 el.click label=%s err=%s", _log_prefix(driver), label, _safe_exception_text(exc))
        time.sleep(random.uniform(0.12, 0.45))
        try:
            driver.execute_script("arguments[0].click();", el)
        except Exception:
            el.click()


def _human_type_text(driver, el, value: str, *, clear: bool = True) -> None:
    """按字符/小段输入，触发真实 key events；失败时回退 JS setter。"""
    if not _browser_actions_enabled():
        if clear:
            try:
                el.clear()
            except Exception:
                pass
        el.send_keys(value)
        return
    try:
        _human_scroll_to(driver, el)
        try:
            _human_click(driver, el, label="input_focus")
        except Exception:
            driver.execute_script("arguments[0].focus();", el)
        if clear:
            from selenium.webdriver.common.keys import Keys
            mod = Keys.COMMAND
            try:
                import platform
                if platform.system().lower() != "darwin":
                    mod = Keys.CONTROL
            except Exception:
                pass
            try:
                el.send_keys(mod, "a")
                time.sleep(random.uniform(0.04, 0.16))
                el.send_keys(Keys.BACKSPACE)
            except Exception:
                try:
                    el.clear()
                except Exception:
                    pass
        text = str(value)
        i = 0
        while i < len(text):
            # 邮箱/密码整体仍逐字符，但偶尔 2 字符一组，节奏更自然。
            step = 2 if random.random() < 0.12 and i + 1 < len(text) else 1
            el.send_keys(text[i:i + step])
            i += step
            human_delay("keystroke")
            if i < len(text) and random.random() < 0.08:
                human_delay("typing_pause")
        driver.execute_script(
            "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));"
            "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
            el,
        )
    except Exception as exc:
        logger.debug("%s 人工化输入失败，回退 JS setter err=%s", _log_prefix(driver), _safe_exception_text(exc))
        _set_element_value(driver, el, value)


def _page_warmup(driver, *, reason: str = "") -> None:
    if not _browser_actions_enabled():
        return
    try:
        human_delay("page_warmup")
        if hasattr(driver, "execute_cdp_cmd"):
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": random.randint(80, 360),
                "y": random.randint(80, 260),
            })
    except Exception:
        pass


def _find_any(driver, selectors: list[str], timeout: int | None = None):
    from selenium.webdriver.common.by import By

    end = time.time() + (timeout or int(_cfg.ROXY_SELENIUM_TIMEOUT))
    last = None
    while time.time() < end:
        for selector in selectors:
            try:
                by = By.XPATH if selector.startswith("//") else By.CSS_SELECTOR
                items = driver.find_elements(by, selector)
                for item in items:
                    if _visible(item):
                        return item
            except Exception as exc:
                last = exc
        time.sleep(0.4)
    raise RuntimeError(f"找不到页面元素: {selectors}; error={type(last).__name__ if last else 'None'}")


def _click_any(driver, selectors: list[str], timeout: int | None = None) -> None:
    el = _find_any(driver, selectors, timeout)
    _human_click(driver, el, label="click_any")


def _type_any(driver, selectors: list[str], value: str, timeout: int | None = None, clear: bool = True) -> None:
    el = _find_any(driver, selectors, timeout)
    _human_type_text(driver, el, value, clear=clear)


_EMAIL_INPUT_SELECTORS = [
    "input[type='email']",
    "input[name='email']",
    "input[name='username']",
    "input#email-input",
    "input[autocomplete='email']",
]


def _email_entry_state(driver) -> dict:
    try:
        return driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled;
        const attrText = el => [
          el.id, el.getAttribute('name'), el.getAttribute('type'), el.getAttribute('autocomplete'),
          el.getAttribute('data-testid'), el.getAttribute('data-test-id'), el.getAttribute('data-provider'),
          el.getAttribute('data-auth-provider'), el.getAttribute('href'), el.getAttribute('action'),
          el.getAttribute('formaction'), el.getAttribute('value')
        ].filter(Boolean).join(' ').toLowerCase();
        const inputs = [...document.querySelectorAll('input')].filter(visible).map(el => ({
          type: el.getAttribute('type') || '', name: el.getAttribute('name') || '', id: el.id || '',
          autocomplete: el.getAttribute('autocomplete') || '', value: el.value || ''
        })).slice(0, 30);
        const actions = [...document.querySelectorAll('button,a,[role=button],input[type=button],input[type=submit]')]
          .filter(visible).map(el => ({tag: el.tagName, type: el.getAttribute('type') || '', attrs: attrText(el)})).slice(0, 40);
        return {url: location.href, title: document.title, inputs, actions};
        """) or {}
    except Exception as exc:
        return {"url": getattr(driver, "current_url", ""), "error": f"{type(exc).__name__}: {exc}"}


def _find_visible_email_input_js(driver):
    return driver.execute_script(r"""
    const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
      && !el.disabled && !el.readOnly;
    const selectors = [
      'input[type="email"]',
      'input[name="email"]',
      'input[name="username"]',
      'input#email-input',
      'input[autocomplete="email"]'
    ];
    for (const sel of selectors) {
      const el = [...document.querySelectorAll(sel)].find(visible);
      if (el) return el;
    }
    return null;
    """)


def _is_oauth_consent_like(driver) -> bool:
    """检测是否已到 OAuth 授权/consent 页。这里不能再点任何邮箱分支或全局提交按钮。"""
    try:
        return bool(driver.execute_script(r"""
        const url = String(location.href || '').toLowerCase();
        if (/oauth|authorize|consent/.test(url) && !/login|signup|identifier|email-verification/.test(url)) return true;
        const formsWithEmail = [...document.querySelectorAll('form')]
          .some(form => form.querySelector('input[type="email"],input[name="email"],input[name="username"],input[autocomplete="email"]'));
        if (formsWithEmail) return false;
        const actions = [...document.querySelectorAll('button,a,[role="button"],input[type="submit"],input[type="button"]')]
          .map(el => [el.id, el.name, el.type, el.getAttribute('data-testid'), el.getAttribute('data-test-id'),
            el.getAttribute('data-provider'), el.getAttribute('data-auth-provider'), el.getAttribute('href'),
            el.getAttribute('formaction'), el.value, el.className].filter(Boolean).join(' ').toLowerCase())
          .join(' ');
        return /oauth|authorize|consent|grant|allow/.test(actions) && !/email|username/.test(actions);
        """))
    except Exception:
        return False


def _is_external_idp_url(url: str) -> bool:
    u = str(url or '').lower()
    return any(x in u for x in (
        'accounts.google.', 'google.com/o/oauth', 'appleid.apple.', 'login.microsoftonline.',
        'login.live.', 'github.com/login/oauth', 'facebook.com/', 'saml', 'sso'
    ))


def _assert_not_external_idp(driver, label: str = '') -> None:
    try:
        current = str(driver.current_url or '')
    except Exception:
        current = ''
    if _is_external_idp_url(current):
        raise RuntimeError(f"误入第三方账号授权页（{label}）：route={_diagnostic_route(current)}")


def _click_email_entry_option(driver) -> bool:
    """点击“邮箱方式”入口；只看 DOM 技术属性，不看按钮可见文案，并显式排除 Google 等第三方。"""
    if _is_oauth_consent_like(driver):
        logger.info("%s 当前疑似 OAuth 授权页，跳过邮箱入口兜底点击", _log_prefix(driver))
        return False
    target = driver.execute_script(r"""
    const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
      && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
    const attrText = el => {
      const own = [
        el.id, el.getAttribute('name'), el.getAttribute('type'), el.getAttribute('autocomplete'),
        el.getAttribute('data-testid'), el.getAttribute('data-test-id'), el.getAttribute('data-provider'),
        el.getAttribute('data-auth-provider'), el.getAttribute('data-idp'), el.getAttribute('href'), el.getAttribute('action'),
        el.getAttribute('formaction'), el.getAttribute('value'), el.getAttribute('aria-label'), el.className
      ].filter(Boolean).join(' ');
      const desc = [...el.querySelectorAll('img,svg,use,[aria-label],[data-provider],[data-testid],[data-test-id]')]
        .map(x => [x.getAttribute('alt'), x.getAttribute('src'), x.getAttribute('href'), x.getAttribute('xlink:href'),
          x.getAttribute('aria-label'), x.getAttribute('data-provider'), x.getAttribute('data-testid'), x.getAttribute('data-test-id'), x.className]
          .filter(Boolean).join(' ')).join(' ');
      return `${own} ${desc}`.toLowerCase();
    };
    const bad = /google|apple|microsoft|github|facebook|saml|sso|oauth|social|oidc|idp|provider|authorize|consent|grant|allow/;
    const good = /(^|[^a-z])(email|mail|username|passwordless|otp|magic)([^a-z]|$)/;
    const candidates = [...document.querySelectorAll('button,a,[role="button"],input[type="button"],input[type="submit"]')]
      .filter(visible)
      .map(el => ({el, attrs: attrText(el), hasLogo: !!el.querySelector('img,svg,use')}))
      .filter(x => good.test(x.attrs) && !bad.test(x.attrs) && !x.hasLogo);
    if (candidates.length !== 1) return null;
    candidates[0].el.scrollIntoView({block:'center'});
    return candidates[0].el;
    """)
    if target:
        _human_click(driver, target, label="email_entry")
        return True
    return False


def _type_email_address(driver, email: str, timeout: int | None = None) -> None:
    """进入邮箱登录/注册方式并填写邮箱。全程不依赖页面可见文字，避免非日本出口本地化后误点 Google。"""
    end = time.time() + (timeout or int(_cfg.ROXY_SELENIUM_TIMEOUT))
    last_state = None
    clicked_email_option = False
    while time.time() < end:
        el = _find_visible_email_input_js(driver)
        if el:
            _human_type_text(driver, el, email, clear=True)
            return
        last_state = _email_entry_state(driver)
        if not clicked_email_option and _click_email_entry_option(driver):
            clicked_email_option = True
            time.sleep(1.0)
            _assert_not_external_idp(driver, "点击邮箱入口后")
            continue
        time.sleep(0.4)
    raise RuntimeError(f"找不到邮箱输入框/邮箱入口（未使用文字识别），state={_diagnostic_state(last_state)}")


def _submit_nearest_form_for_active_input(driver) -> bool:
    if _is_oauth_consent_like(driver):
        logger.info("%s 当前疑似 OAuth 授权页，禁止执行邮箱提交", _log_prefix(driver))
        return False
    result = driver.execute_script(r"""
    const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
      && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
    const input = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete="email"]')]
      .find(visible);
    if (!input) return {ok:false, reason:'missing_email_input'};
    const value = String(input.value || '').trim();
    if (!value || !value.includes('@')) return {ok:false, reason:'email_value_not_ready', value};
    const form = input.closest('form');

    const bad = /google|apple|microsoft|github|facebook|saml|sso|oauth|social|oidc|sso|saml|idp|provider|authorize|consent|grant|allow/;
    const attrText = el => {
      const own = [el.id, el.name, el.type, el.getAttribute('data-testid'), el.getAttribute('data-test-id'),
        el.getAttribute('data-provider'), el.getAttribute('data-auth-provider'), el.getAttribute('data-idp'),
          el.getAttribute('aria-label'), el.getAttribute('href'), el.getAttribute('formaction'), el.value, el.className,
          el.innerText]
        .filter(Boolean).join(' ');
      const desc = [...el.querySelectorAll('img,svg,use,[aria-label],[data-provider],[data-testid],[data-test-id]')]
        .map(x => [x.getAttribute('alt'), x.getAttribute('src'), x.getAttribute('href'), x.getAttribute('xlink:href'),
          x.getAttribute('aria-label'), x.getAttribute('data-provider'), x.getAttribute('data-testid'), x.getAttribute('data-test-id'), x.className]
          .filter(Boolean).join(' '))
        .join(' ');
      return `${own} ${desc}`.toLowerCase();
    };
    const inputRect = input.getBoundingClientRect();
    const formId = form?.getAttribute('id') || '';
    const exactAction = el => /^(continue|next|log in|login|submit|继续|登录|続行|次へ)$/i.test(String(el.innerText || el.value || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim());
    // OpenAI's current login page can render the email input and Continue button
    // in a div tree without a native <form>. In that case only accept an exact,
    // non-provider action or a clearly primary submit button from the page.
    const scopedButtons = form ? [
      ...form.querySelectorAll('button,input[type="submit"]'),
      ...(formId ? [...document.querySelectorAll(`button[form="${CSS.escape(formId)}"],input[type="submit"][form="${CSS.escape(formId)}"]`)] : [])
    ] : [...document.querySelectorAll('button,input[type="submit"],input[type="button"]')];
    if (form) {
      scopedButtons.push(...[...document.querySelectorAll('button,input[type="submit"],input[type="button"]')]
        .filter(el => exactAction(el) && !form.contains(el)));
    }
    const rawButtons = scopedButtons
      .filter(visible)
      .map((el, idx) => {
        const r = el.getBoundingClientRect();
        const attrs = attrText(el);
        const hasLogo = !!el.querySelector('img,svg,use');
        const isBad = bad.test(attrs) || hasLogo;
        const belowInput = r.top >= inputRect.bottom - 10;
        const distance = Math.max(0, r.top - inputRect.bottom) + Math.abs((r.left + r.right) / 2 - (inputRect.left + inputRect.right) / 2) / 10;
        const cls = String(el.className || '').toLowerCase();
        const type = String(el.getAttribute('type') || '').toLowerCase();
        // ChatGPT 新版邮箱页的主按钮形如：
        // <button class="... btn-primary ... w-full ..." type="submit"><div>続行</div></button>
        // 优先选择同 form 下的 primary submit，而不是因为多个按钮距离接近误判歧义。
        const isPrimarySubmit = (el.tagName === 'BUTTON' || el.tagName === 'INPUT') && type === 'submit'
          && (/\bbtn-primary\b/.test(cls) || /\b_primary_/.test(cls) || /\bw-full\b/.test(cls));
        const exact = exactAction(el);
        const score = (isPrimarySubmit ? 1000 : 0) + (exact ? 500 : 0) + (type === 'submit' ? 100 : 0) - distance;
        return {el, idx, attrs, isBad, hasLogo, belowInput, distance, score, isPrimarySubmit, exact, tag: el.tagName, type};
      });
    const safe = rawButtons.filter(x => !x.isBad && x.belowInput && (form || x.exact || x.isPrimarySubmit))
      .sort((a,b) => b.score - a.score || a.distance - b.distance || a.idx - b.idx);
    if (!safe.length) {
      return {ok:false, reason:form ? 'no_safe_submit' : 'no_safe_global_action', buttons: rawButtons.map(x => ({idx:x.idx, isBad:x.isBad, hasLogo:x.hasLogo, belowInput:x.belowInput, primary:x.isPrimarySubmit, exact:x.exact, attrs:x.attrs.slice(0,160), type:x.type}))};
    }
    // 多个安全按钮时，若没有明确 primary submit，且距离接近，才认为页面歧义。
    if (!safe[0].isPrimarySubmit && safe.length > 1 && Math.abs(safe[0].distance - safe[1].distance) < 8) {
      return {ok:false, reason:'ambiguous_submit', buttons: safe.slice(0,3).map(x => ({idx:x.idx, distance:x.distance, score:x.score, primary:x.isPrimarySubmit, attrs:x.attrs.slice(0,160), type:x.type}))};
    }
    const target = safe[0].el;
    target.scrollIntoView({block:'center'});
    window.__roxy_email_submit_debug = {at: Date.now(), targetAttrs: safe[0].attrs.slice(0,240), buttonCount: rawButtons.length, primary:safe[0].isPrimarySubmit};
    return {ok:true, reason:safe[0].isPrimarySubmit ? 'primary_submit' : 'safe_submit', target, targetAttrs:safe[0].attrs.slice(0,160), primary:safe[0].isPrimarySubmit};
    """) or {}
    if result.get("ok"):
        target = result.get("target")
        if target:
            _human_click(driver, target, label="email_submit")
        else:
            logger.warning("%s 邮箱提交未返回目标元素，回退 requestSubmit", _log_prefix(driver))
            driver.execute_script("document.querySelector('form')?.requestSubmit?.();")
        logger.info("%s 邮箱表单安全提交：%s", _log_prefix(driver), _diagnostic_state(result))
        time.sleep(0.8)
        _assert_not_external_idp(driver, "提交邮箱后")
        return True
    logger.warning("%s 未执行邮箱提交：%s", _log_prefix(driver), _diagnostic_state(result))
    return False


def _current_email_input_value(driver) -> str:
    try:
        state = _email_input_value_state(driver)
        for item in state.get("inputs") or []:
            value = str(item.get("value") or "").strip()
            if "@" in value:
                return value
    except Exception:
        pass
    return ""


def _stabilize_email_input_before_submit(driver, email: str) -> dict:
    """提交前把 DOM value / React 受控状态 / blur-change 状态统一稳定下来。"""
    try:
        return driver.execute_script(r"""
        const email = String(arguments[0] || '').trim();
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const input = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete*="email"]')]
          .find(visible);
        if (!input) return {ok:false, reason:'missing_email_input'};

        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        input.scrollIntoView({block:'center', inline:'nearest'});
        input.focus();
        if (setter) setter.call(input, email); else input.value = email;

        // 让 React/表单校验尽量收到完整输入链路。
        try { input.dispatchEvent(new InputEvent('beforeinput', {bubbles:true, cancelable:true, inputType:'insertText', data:email})); } catch (_) {}
        try { input.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:email})); } catch (_) {
          input.dispatchEvent(new Event('input', {bubbles:true}));
        }
        input.dispatchEvent(new Event('change', {bubbles:true}));
        input.dispatchEvent(new FocusEvent('blur', {bubbles:true}));
        input.blur();
        input.focus();

        const form = input.closest('form');
        const submit = form?.querySelector('button[type="submit"],input[type="submit"]');
        return {
          ok:true,
          value: input.value,
          active: document.activeElement === input,
          hasForm: !!form,
          hasSubmit: !!submit,
          submitDisabled: submit ? (!!submit.disabled || String(submit.getAttribute('aria-disabled') || '').toLowerCase() === 'true') : null,
          url: location.href
        };
        """, email) or {}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _submit_email_form_stable(driver, email: str) -> dict:
    """第一次提交就按“补交成功”的方式执行：稳定 value 后 Enter + DOM click。"""
    try:
        return driver.execute_script(r"""
        const email = String(arguments[0] || '').trim();
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
        const editable = el => visible(el) && !el.readOnly;
        const input = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete*="email"]')]
          .find(editable);
        if (!input) return {ok:false, reason:'missing_email_input'};
        if (!email || !email.includes('@')) return {ok:false, reason:'empty_email', value: email};

        const form = input.closest('form');
        if (!form) return {ok:false, reason:'missing_form'};

        const bad = /google|apple|microsoft|github|facebook|saml|sso|oauth|social|oidc|idp|provider|authorize|consent|grant|allow/;
        const attrText = el => {
          const own = [el.id, el.name, el.type, el.getAttribute('data-testid'), el.getAttribute('data-test-id'),
            el.getAttribute('data-provider'), el.getAttribute('data-auth-provider'), el.getAttribute('data-idp'),
            el.getAttribute('aria-label'), el.getAttribute('href'), el.getAttribute('formaction'), el.value, el.className]
            .filter(Boolean).join(' ');
          const desc = [...el.querySelectorAll('img,svg,use,[aria-label],[data-provider],[data-testid],[data-test-id]')]
            .map(x => [x.getAttribute('alt'), x.getAttribute('src'), x.getAttribute('href'), x.getAttribute('xlink:href'),
              x.getAttribute('aria-label'), x.getAttribute('data-provider'), x.getAttribute('data-testid'), x.getAttribute('data-test-id'), x.className]
              .filter(Boolean).join(' '))
            .join(' ');
          return `${own} ${desc}`.toLowerCase();
        };

        const formId = form.getAttribute('id') || '';
        const buttons = [
          ...form.querySelectorAll('button,input[type="submit"]'),
          ...(formId ? [...document.querySelectorAll(`button[form="${CSS.escape(formId)}"],input[type="submit"][form="${CSS.escape(formId)}"]`)] : [])
        ].filter((el, idx, arr) => arr.indexOf(el) === idx)
          .filter(el => visible(el) && !bad.test(attrText(el)) && !el.querySelector('img,svg,use'));
        const submit = buttons.find(el => (el.getAttribute('type') || '').toLowerCase() === 'submit') || buttons[0] || null;
        if (!submit) return {ok:false, reason:'missing_safe_submit'};

        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        input.scrollIntoView({block:'center', inline:'nearest'});
        input.focus();
        if (setter) setter.call(input, email); else input.value = email;
        try { input.dispatchEvent(new InputEvent('beforeinput', {bubbles:true, cancelable:true, inputType:'insertText', data:email})); } catch (_) {}
        try { input.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:email})); } catch (_) {
          input.dispatchEvent(new Event('input', {bubbles:true}));
        }
        input.dispatchEvent(new Event('change', {bubbles:true}));
        input.dispatchEvent(new FocusEvent('blur', {bubbles:true}));
        input.blur();
        input.focus();

        submit.scrollIntoView({block:'center', inline:'nearest'});

        // 不要在 execute_script 同步执行 submit.click()：
        // ChromeDriver 会等前端 submit/navigation，Roxy/Chrome 150 上可能卡到 page/script timeout。
        // setTimeout 让 Selenium 先返回，点击在页面事件循环里异步发生，和补交逻辑一致。
        setTimeout(() => {
          try {
            input.focus();
            input.dispatchEvent(new KeyboardEvent('keydown', {bubbles:true, cancelable:true, key:'Enter', code:'Enter'}));
            input.dispatchEvent(new KeyboardEvent('keypress', {bubbles:true, cancelable:true, key:'Enter', code:'Enter'}));
            input.dispatchEvent(new KeyboardEvent('keyup', {bubbles:true, cancelable:true, key:'Enter', code:'Enter'}));
            if (submit && !submit.disabled) submit.click();
            else if (form && typeof form.requestSubmit === 'function') form.requestSubmit();
          } catch (_) {}
        }, 80);

        window.__roxy_email_submit_debug = {
          at: Date.now(),
          mode: 'stable_async_enter_click',
          value: input.value,
          submitAttrs: attrText(submit).slice(0, 240)
        };
        return {
          ok:true,
          reason:'stable_async_enter_click',
          value: input.value,
          submitDisabled: !!submit.disabled || String(submit.getAttribute('aria-disabled') || '').toLowerCase() === 'true',
          submitAttrs: attrText(submit).slice(0, 180),
          url: location.href
        };
        """, email) or {}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _submit_email_step(driver, email: str | None = None) -> None:
    # 不再优先走浏览器内 NextAuth fetch：
    # Roxy/Chrome 150 下 execute_async_script + fetch 偶发卡到 script timeout；
    # 实测 UI 首次提交后若停在 /auth/login?email=...，由 _recover_email_submit_if_stuck 补交表单更稳定。
    email_value = str(email or _current_email_input_value(driver) or "").strip()
    stable = _stabilize_email_input_before_submit(driver, email_value)
    logger.info("%s 邮箱提交前状态稳定：%s", _log_prefix(driver), _diagnostic_state(stable))
    time.sleep(random.uniform(0.8, 1.8) if _browser_actions_enabled() else 0.4)

    stable_submit = _submit_email_form_stable(driver, email_value)
    if stable_submit.get("ok"):
        logger.info("%s 邮箱稳定表单提交：%s", _log_prefix(driver), _diagnostic_state(stable_submit))
        time.sleep(1.0)
        _assert_not_external_idp(driver, "稳定表单提交邮箱后")
        return
    logger.warning("%s 邮箱稳定表单提交失败，回退 UI 点击提交：%s", _log_prefix(driver), _diagnostic_state(stable_submit))
    if _submit_nearest_form_for_active_input(driver):
        return
    raise RuntimeError(f"无法提交邮箱步骤（拒绝按页面文字或首个 submit 兜底，避免误点第三方登录），state={_diagnostic_state(_email_entry_state(driver))}")


def _recover_email_submit_if_stuck(driver, email: str) -> dict:
    """邮箱提交后停在 /auth/login?email= 且输入框被清空时，补一次原生表单提交。"""
    try:
        return driver.execute_script(r"""
        const email = String(arguments[0] || '').trim();
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const input = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete*="email"]')]
          .find(visible);
        if (!input) return {ok:false, reason:'missing_email_input'};
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        input.focus();
        if (setter) setter.call(input, email); else input.value = email;
        input.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:email}));
        input.dispatchEvent(new Event('change', {bubbles:true}));
        const form = input.closest('form');
        const submit = form?.querySelector('button[type="submit"],input[type="submit"]');
        setTimeout(() => {
          try {
            input.dispatchEvent(new KeyboardEvent('keydown', {bubbles:true, cancelable:true, key:'Enter', code:'Enter'}));
            input.dispatchEvent(new KeyboardEvent('keyup', {bubbles:true, cancelable:true, key:'Enter', code:'Enter'}));
            if (submit && !submit.disabled) submit.click();
            else if (form && typeof form.requestSubmit === 'function') form.requestSubmit();
          } catch (_) {}
        }, 80);
        return {ok:true, reason:'resubmitted_email_form', value: input.value, hasForm: !!form, hasSubmit: !!submit};
        """, email) or {}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _submit_email_via_browser_nextauth(driver, email: str) -> dict:
    """在 Roxy 浏览器上下文里调用 ChatGPT NextAuth signin。

    UI submit 在 Roxy/Chrome 150 上会偶发只跳到 `/auth/login?email=...` 后停住。
    这里改走浏览器页面内 fetch，仍使用当前 Roxy 浏览器的 cookie / 指纹环境，
    拿到 auth.openai.com authorize URL 后让浏览器跳转。
    """
    try:
        current = str(getattr(driver, "current_url", "") or "")
        if "chatgpt.com" not in current:
            return {"ok": False, "reason": "not_on_chatgpt", "route": _diagnostic_route(current)}
    except Exception:
        current = ""

    did = str(uuid.uuid4())
    auth_log_id = str(uuid.uuid4())
    old_script_timeout = int(getattr(_cfg, "ROXY_SELENIUM_TIMEOUT", 90) or 90)
    try:
        try:
            driver.set_script_timeout(25)
        except Exception:
            pass
        result = driver.execute_async_script(r"""
        const email = String(arguments[0] || '').trim();
        const did = String(arguments[1] || '');
        const authLogId = String(arguments[2] || '');
        const done = arguments[arguments.length - 1];
        (async () => {
          try {
            const csrfResp = await fetch('/api/auth/csrf', {
              method: 'GET',
              credentials: 'include',
              headers: {
                'accept': 'application/json',
                'cache-control': 'no-cache',
                'pragma': 'no-cache'
              }
            });
            const csrfText = await csrfResp.text();
            let csrfData = {};
            try { csrfData = JSON.parse(csrfText); } catch (_) {}
            const csrfToken = csrfData.csrfToken || '';
            if (!csrfResp.ok || !csrfToken) {
              done({ok:false, stage:'csrf', status:csrfResp.status, reason:'csrf_unavailable'});
              return;
            }

            const q = new URLSearchParams({
              prompt: 'login',
              'ext-oai-did': did,
              auth_session_logging_id: authLogId,
              'ext-passkey-client-capabilities': '11111',
              screen_hint: 'login_or_signup',
              login_hint: email
            });
            const body = new URLSearchParams({
              callbackUrl: 'https://chatgpt.com/',
              csrfToken,
              json: 'true'
            });
            const resp = await fetch('/api/auth/signin/openai?' + q.toString(), {
              method: 'POST',
              credentials: 'include',
              headers: {
                'accept': 'application/json',
                'content-type': 'application/x-www-form-urlencoded',
                'cache-control': 'no-cache',
                'pragma': 'no-cache'
              },
              body: body.toString()
            });
            const text = await resp.text();
            let data = {};
            try { data = JSON.parse(text); } catch (_) {}
            let url = data.url || '';
            if (!resp.ok || !url) {
              done({ok:false, stage:'signin', status:resp.status, reason:'signin_redirect_unavailable'});
              return;
            }

            try {
              const u = new URL(url, location.href);
              if (!u.searchParams.get('screen_hint')) u.searchParams.set('screen_hint', 'login_or_signup');
              if (!u.searchParams.get('login_hint')) u.searchParams.set('login_hint', email);
              if (!u.searchParams.get('ext-oai-did')) u.searchParams.set('ext-oai-did', did);
              if (!u.searchParams.get('auth_session_logging_id')) u.searchParams.set('auth_session_logging_id', authLogId);
              url = u.toString();
            } catch (_) {}
            window.location.assign(url);
            done({ok:true, stage:'redirect'});
          } catch (e) {
            done({ok:false, stage:'exception', reason:'browser_fetch_failed'});
          }
        })();
        """, email, did, auth_log_id) or {}
        if not isinstance(result, dict):
            return {"ok": False, "reason": "invalid_result"}
        stage = result.get("stage")
        reason = result.get("reason")
        return {
            "ok": result.get("ok") is True,
            "stage": stage if stage in ("csrf", "signin", "redirect", "exception") else "unknown",
            "reason": reason if reason in (
                "csrf_unavailable", "signin_redirect_unavailable", "browser_fetch_failed",
            ) else "unknown",
            "status": result.get("status") if type(result.get("status")) is int else None,
        }
    except Exception as exc:
        return {"ok": False, "reason": "browser_fetch_failed", "error_type": type(exc).__name__}
    finally:
        try:
            driver.set_script_timeout(old_script_timeout)
        except Exception:
            pass


def _email_input_value_state(driver) -> dict:
    """读取当前可见邮箱框状态，用于提交后确认是否真的进入下一步。"""
    try:
        return driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const inputs = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete*="email"]')]
          .filter(visible)
          .map(el => ({type: el.getAttribute('type') || '', name: el.name || '', id: el.id || '', autocomplete: el.getAttribute('autocomplete') || '', value: el.value || ''}));
        return {url: location.href, inputs};
        """) or {}
    except Exception as exc:
        return {"url": getattr(driver, "current_url", ""), "error": f"{type(exc).__name__}: {exc}"}


def _is_email_login_page_still_present(driver) -> bool:
    state = _email_input_value_state(driver)
    return bool(state.get("inputs"))


def _wait_email_submit_next_state(driver, email: str, timeout: int = 18) -> str:
    """邮箱提交后等待进入 password / otp / logged_in；仍停留邮箱页则返回 email_page。

    Cloak/Playwright 路径里，点击 submit 后页面经常先发生一次 SPA 导航：
    `chatgpt.com/auth/login?email=...`，同时 React 会短暂把 email input 清空。
    旧逻辑一看到空 input 就立刻返回 `email_cleared`，导致在真正跳到
    `auth.openai.com/...` 前过早重填，形成“提交 -> 清空 -> 重填”的循环。
    这里对 email_cleared 做去抖：只记录并继续观察几秒；若期间进入
    password/otp/login_password/logged_in 则按真实状态返回，持续清空才让上层重试。
    """
    end = time.time() + timeout
    last = None
    cleared_seen_at: float | None = None
    cleared_last_log_at = 0.0
    cleared_recover_done = False
    expected_email = str(email or "").strip().lower()
    while time.time() < end:
        if _has_access_token(driver):
            return "logged_in"
        if _is_login_password_page(driver):
            return "login_password"
        if _is_email_verification_page(driver):
            return "otp"
        if _is_signup_password_page(driver):
            return "password"
        state = _email_input_value_state(driver)
        last = state
        inputs = state.get("inputs") or []
        if inputs:
            values = [str(i.get("value") or "") for i in inputs]
            url = str(state.get("url") or "")
            has_blank = any(v == "" for v in values)
            has_expected = any(v.strip().lower() == expected_email for v in values)
            if has_blank and not has_expected:
                now = time.time()
                if cleared_seen_at is None:
                    cleared_seen_at = now
                # URL 已带 email 查询参数时更像是提交后的中间态，给它更长观察窗口。
                debounce = 18.0 if ("/auth/login" in url and "email=" in url) else 5.0
                if now - cleared_last_log_at > 2.0:
                    logger.info(
                        "%s 邮箱提交后检测到输入框短暂清空，继续等待跳转：elapsed=%.1fs debounce=%.1fs url=%s",
                        _log_prefix(driver), now - cleared_seen_at, debounce, _diagnostic_route(url),
                    )
                    cleared_last_log_at = now
                if (
                    not cleared_recover_done
                    and "/auth/login" in url
                    and "email=" in url
                    and now - cleared_seen_at >= 2.0
                ):
                    recover = _recover_email_submit_if_stuck(driver, email)
                    cleared_recover_done = True
                    logger.info("%s 邮箱提交后仍停留在 login?email，中途补交一次表单：%s", _log_prefix(driver), _diagnostic_state(recover))
                if now - cleared_seen_at >= debounce:
                    return "email_cleared"
            else:
                cleared_seen_at = None
            # 仍是当前邮箱页，继续短等。
        time.sleep(0.8)
    logger.info("%s 邮箱提交后等待下一步超时，最后邮箱页状态=%s", _log_prefix(driver), _diagnostic_state(last))
    return "email_page" if _is_email_login_page_still_present(driver) else "unknown"


def _submit_email_and_wait_next(driver, email: str, attempts: int = 3) -> str:
    """填写并提交邮箱，必须确认进入 password/otp/logged_in 才返回。"""
    last_state = None
    for attempt in range(1, attempts + 1):
        _type_email_address(driver, email, timeout=20)
        state = _email_input_value_state(driver)
        last_state = state
        values = [str(i.get("value") or "") for i in (state.get("inputs") or [])]
        if not any(v.strip().lower() == email.strip().lower() for v in values):
            logger.warning("%s 邮箱写入校验失败，准备重试：attempt=%s/%s state=%s", _log_prefix(driver), attempt, attempts, _diagnostic_state(state))
            time.sleep(0.8)
            continue
        logger.info("%s 已填写邮箱并校验通过：%s", _log_prefix(driver), email)
        human_delay("form")
        _submit_email_step(driver, email)
        logger.info("%s 已提交邮箱，等待进入密码页或验证码页（%s/%s）", _log_prefix(driver), attempt, attempts)
        state_name = _wait_email_submit_next_state(driver, email, timeout=20)
        if state_name == "login_password":
            raise RuntimeError(f"邮箱提交后进入登录密码页，按已注册/不可用邮箱处理并停用: route={_diagnostic_route(getattr(driver, 'current_url', ''))}")
        if state_name in ("password", "otp", "logged_in"):
            logger.info("%s 邮箱提交后已进入下一步：%s", _log_prefix(driver), state_name)
            return state_name
        logger.warning("%s 邮箱提交后仍未进入下一步：%s，准备重填重试 state=%s", _log_prefix(driver), state_name, _diagnostic_state(_email_input_value_state(driver)))
        time.sleep(1.0)
    raise RuntimeError(f"邮箱提交后未进入密码页/验证码页，最后状态={_diagnostic_state(last_state)}")


def _type_otp(driver, code: str) -> None:
    from selenium.webdriver.common.by import By

    # 单输入框
    for selector in [
        "input[autocomplete='one-time-code']",
        "input[name='code']",
        "input[inputmode='numeric']",
        "input[type='tel']",
    ]:
        els = [e for e in driver.find_elements(By.CSS_SELECTOR, selector) if _visible(e)]
        if len(els) == 1:
            _human_type_text(driver, els[0], code, clear=True)
            return

    # 6 个分格输入框
    boxes = [e for e in driver.find_elements(By.CSS_SELECTOR, "input") if _visible(e)]
    numeric_boxes = []
    for e in boxes:
        attrs = " ".join(str(e.get_attribute(k) or "") for k in ("inputmode", "autocomplete", "aria-label", "name", "id", "type"))
        if any(x in attrs.lower() for x in ("numeric", "one-time", "code", "otp", "tel")):
            numeric_boxes.append(e)
    if len(numeric_boxes) >= len(code):
        for e, ch in zip(numeric_boxes, code):
            if _browser_actions_enabled():
                _human_scroll_to(driver, e)
                time.sleep(random.uniform(0.04, 0.18))
            e.send_keys(ch)
            if _browser_actions_enabled():
                human_delay("keystroke")
        return

    raise RuntimeError("找不到 OTP 输入框")


def _email_otp_page_state(driver) -> dict:
    """Return only structural OTP-page diagnostics.

    The page may contain an email OTP, password, redirect code or auth token.
    Callers only need input attributes and fixed error markers, so values and
    free-form page text are intentionally discarded in the browser process.
    """
    try:
        return driver.execute_script(r"""
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const inputs = [...document.querySelectorAll('input')].filter(visible).map(el => ({
          type: el.getAttribute('type') || '', name: el.getAttribute('name') || '', id: el.id || '',
          autocomplete: el.getAttribute('autocomplete') || '', inputmode: el.getAttribute('inputmode') || '',
          ariaInvalid: el.getAttribute('aria-invalid') || ''
        })).slice(0, 30);
        const buttons = [...document.querySelectorAll('button,a,[role=button],input[type=button],input[type=submit]')]
          .filter(visible).map(el => ({
            tag: el.tagName, type: el.getAttribute('type') || '',
            action: el.getAttribute('data-dd-action-name') || '', aria: el.getAttribute('aria-label') || '',
            disabled: !!el.disabled || String(el.getAttribute('aria-disabled') || '').toLowerCase() === 'true'
          })).slice(0, 30);
        const rawErrors = [...document.querySelectorAll('.react-aria-FieldError,[slot="errorMessage"],[id$="-error"],[aria-invalid="true"] + *,[class*="error"]')]
          .filter(visible).map(el => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
        const body = (document.body?.innerText || '').replace(/\s+/g, ' ').toLowerCase();
        const errorText = (rawErrors.join(' ') + ' ' + body).toLowerCase();
        const errors = [];
        const add = marker => { if (!errors.includes(marker)) errors.push(marker); };
        if (/invalid code|incorrect code|wrong code|验证码错误|验证码无效|コードが正しく|無効/.test(errorText)) add('otp_invalid');
        if (/expired|验证码已过期|期限/.test(errorText)) add('otp_expired');
        if (rawErrors.length && !errors.length) add('page_error');
        return {url: location.pathname || '/', inputs, buttons, errors};
        """) or {}
    except Exception as exc:
        return {
            "url": _diagnostic_route(getattr(driver, 'current_url', '')),
            "error": "页面检查失败",
            "error_type": type(exc).__name__,
        }


def _is_email_verification_page(driver) -> bool:
    try:
        url = str(driver.current_url or '').lower()
    except Exception:
        url = ''
    if '/log-in/password' in url:
        return False
    if 'email-verification' in url:
        return True
    state = _email_otp_page_state(driver)
    attrs = ' '.join(' '.join(str(i.get(k) or '') for k in ('type','name','id','autocomplete','inputmode')) for i in (state.get('inputs') or [])).lower()
    return 'one-time-code' in attrs or 'otp' in attrs or 'code' in attrs


def _clear_otp_inputs(driver) -> None:
    try:
        driver.execute_script(r"""
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const inputs = [...document.querySelectorAll('input')].filter(visible).filter(el => {
          const attrs = [el.type, el.name, el.id, el.autocomplete, el.inputMode, el.getAttribute('aria-label')].join(' ').toLowerCase();
          return /one-time|otp|code|numeric|tel/.test(attrs);
        });
        for (const el of inputs) {
          const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
          if (setter) setter.call(el, ''); else el.value = '';
          el.dispatchEvent(new Event('input', {bubbles:true}));
          el.dispatchEvent(new Event('change', {bubbles:true}));
        }
        """)
    except Exception:
        pass


def _click_resend_email_otp(driver, timeout: int = 20) -> dict:
    """点击重新发送邮箱验证码。优先按 DOM 属性识别，文本仅兜底。"""
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            btn = driver.execute_script(r"""
            const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
            const enabled = el => !el.disabled && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
            const candidates = [...document.querySelectorAll('button,a,[role=button],[role=link],input[type=button],input[type=submit]')].filter(visible);
            const attrHit = candidates.find(el => {
              if (!enabled(el)) return false;
              const attrs = [el.id, el.getAttribute('name'), el.getAttribute('value'), el.getAttribute('data-dd-action-name'), el.getAttribute('aria-label'), el.getAttribute('title'), el.getAttribute('data-testid')]
                .join(' ').toLowerCase();
              const name = String(el.getAttribute('name') || '').toLowerCase();
              const value = String(el.getAttribute('value') || '').toLowerCase();
              if (name === 'intent' && value === 'resend') return true;
              return /resend|send.*new|new.*code|again/.test(attrs);
            });
            if (attrHit) return attrHit;
            // 兜底：多语言文本，避免因页面没有稳定属性时卡死。
            return candidates.find(el => enabled(el) && /resend|send\s+(?:a\s+)?new\s+code|send\s+again|重新发送|重新发送电子邮件|重发|再次发送|再送信|新しい|届かない/.test((el.innerText || el.textContent || '').toLowerCase())) || null;
            """)
            if btn:
                text = str(btn.text or btn.get_attribute('value') or btn.get_attribute('data-dd-action-name') or '').strip()
                _human_click(driver, btn, label="resend_otp")
                logger.info("%s[OTP] 已点击重新发送验证码按钮：%s", _log_prefix(driver), text or '-')
                time.sleep(random.uniform(1.1, 2.4) if _browser_actions_enabled() else 1.5)
                return {"ok": True, "text": text}
        except Exception as exc:
            last = exc
        time.sleep(0.5)
    raise RuntimeError(f"找不到可点击的重新发送验证码按钮: last={type(last).__name__}, state={_diagnostic_state(_email_otp_page_state(driver))}")


def _wait_after_email_otp_submit(driver, timeout: int = 10) -> str:
    """提交 OTP 后等待页面离开验证码页；仍在验证码页且有错误/输入框则认为验证码无效。"""
    end = time.time() + timeout
    last = {}
    while time.time() < end:
        time.sleep(0.5)
        if not _is_email_verification_page(driver):
            return 'accepted'
        last = _email_otp_page_state(driver)
        invalid = any(str(i.get('ariaInvalid') or '').lower() == 'true' for i in (last.get('inputs') or []))
        if invalid or (last.get('errors') or []):
            return 'invalid'
    if _is_email_verification_page(driver):
        logger.warning("%s[OTP] 提交后仍停留验证码页，按验证码无效/过期处理 snapshot=%s", _log_prefix(driver), _diagnostic_state(_email_otp_page_state(driver)))
        return 'invalid'
    return 'accepted'


def _click_continue(driver) -> None:
    _click_any(driver, [
        "button[type='submit']",
        "//button[contains(., 'Continue')]",
        "//button[contains(., '继续')]",
        "//button[contains(., 'Sign up')]",
        "//button[contains(., 'Create')]",
        "//button[contains(., 'Next')]",
    ], timeout=20)


def _maybe_accept(driver) -> None:
    # 只处理明确的 cookie/consent 弹层按钮；不要用 “Continue” 兜底，
    # 非日本出口时 “Continue with Google” 也会命中，导致误点 Google 登录。
    for selectors in ([
        "button#onetrust-accept-btn-handler",
        "button[data-testid='cookie-accept']",
        "button[data-testid='accept-cookies']",
        "//button[contains(., 'Accept')]",
        "//button[contains(., '同意')]",
        "//button[contains(., 'Agree')]",
    ],):
        try:
            _click_any(driver, selectors, timeout=3)
            time.sleep(0.5)
        except Exception:
            pass


def _page_snapshot(driver) -> dict:
    """Return structural profile-page diagnostics without field values or body text."""
    try:
        return driver.execute_script(r"""
        const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
        const inputs = [...document.querySelectorAll('input,select,textarea')].map(el => ({
          tag: el.tagName, type: el.getAttribute('type') || '', name: el.getAttribute('name') || '',
          id: el.id || '', placeholder: el.getAttribute('placeholder') || '',
          autocomplete: el.getAttribute('autocomplete') || '', aria: el.getAttribute('aria-label') || '',
          visible: visible(el)
        })).filter(x => x.visible).slice(0, 30);
        const buttons = [...document.querySelectorAll('button,a[role=button],input[type=submit]')].map(el => ({
          type: el.getAttribute('type') || '', aria: el.getAttribute('aria-label') || '',
          visible: visible(el), disabled: !!el.disabled
        })).filter(x => x.visible).slice(0, 30);
        const widgets = [...document.querySelectorAll('[role=spinbutton], .react-aria-Select, [data-testid="hidden-select-container"] select')].map(el => ({
          tag: el.tagName, role: el.getAttribute('role') || '', dataType: el.getAttribute('data-type') || '',
          aria: el.getAttribute('aria-label') || '', visible: visible(el)
        })).filter(x => x.visible).slice(0, 30);
        return {url: location.pathname || '/', inputs, buttons, widgets};
        """) or {}
    except Exception as exc:
        return {
            "url": _diagnostic_route(getattr(driver, 'current_url', '')),
            "error": "页面检查失败",
            "error_type": type(exc).__name__,
        }


def _has_access_token(driver) -> bool:
    try:
        result = driver.execute_async_script(r"""
        const done = arguments[0];
        fetch('https://chatgpt.com/api/auth/session', {credentials:'include'})
          .then(r => r.json()).then(j => done(Boolean(j && j.accessToken)))
          .catch(() => done(false));
        """)
        return bool(result)
    except Exception:
        return False


def _is_profile_like(snapshot: dict) -> bool:
    """资料页识别：兼容 about-you/profile；年龄/生日控件可能不是 input，而是 React Aria widget。"""
    url = str(snapshot.get('url') or '').lower()
    inputs = snapshot.get('inputs') or []
    widgets = snapshot.get('widgets') or []
    attrs = ' '.join(
        ' '.join(str(i.get(k) or '') for k in ('name', 'id', 'placeholder', 'autocomplete', 'aria', 'type')).lower()
        for i in inputs
    )
    widget_attrs = ' '.join(
        ' '.join(str(i.get(k) or '') for k in ('role', 'dataType', 'aria', 'text', 'tag')).lower()
        for i in widgets
    )
    has_profile_url = any(x in url for x in ('about-you', 'profile', 'signup/profile', 'create-account/profile'))
    has_name_field = (
        'autocomplete name' in attrs
        or ' name ' in f' {attrs} '
        or 'fullname' in attrs
        or 'full_name' in attrs
        or 'firstname' in attrs
        or 'lastname' in attrs
    )
    has_age_or_birth_field = any(x in f' {attrs} {widget_attrs} ' for x in (
        ' age', '-age', '_age', 'birth', 'birthday', 'birthdate',
        ' month', '-month', '_month', 'data-type month',
        ' day', '-day', '_day', 'data-type day',
        ' year', '-year', '_year', 'data-type year',
        'spinbutton', 'react-aria-select', 'type number',
    ))
    # about-you/profile URL 本身已经足够强；部分新版页面会用无 name 的 React Aria 控件。
    return has_profile_url and (has_name_field or has_age_or_birth_field or bool(inputs) or bool(widgets))


def _set_element_value(driver, el, value: str) -> None:
    """兼容 React 受控输入框：用原生 setter 设置值并派发 input/change。"""
    driver.execute_script(r"""
    const el = arguments[0];
    const value = String(arguments[1]);
    const tag = (el.tagName || '').toLowerCase();
    el.scrollIntoView({block:'center'});
    el.focus();
    if (tag === 'select') {
      el.value = value;
    } else {
      const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
      if (setter) setter.call(el, value);
      else el.value = value;
    }
    el.dispatchEvent(new Event('input', {bubbles:true}));
    el.dispatchEvent(new Event('change', {bubbles:true}));
    el.blur();
    """, el, value)


def _select_or_type(driver, selectors: list[str], value: str, timeout: int = 3) -> bool:
    try:
        el = _find_any(driver, selectors, timeout=timeout)
    except Exception:
        return False
    try:
        tag = (el.tag_name or '').lower()
        if tag == 'select':
            if el.__class__.__name__ == 'CloakElement':
                driver.execute_script(r"""
                const el = arguments[0], value = String(arguments[1]);
                const n = parseInt(value, 10);
                const opts = [...el.options];
                const match = opts.find(o => o.value === value)
                  || opts.find(o => (o.textContent || '').trim() === value)
                  || opts[Math.max(0, n - 1)];
                if (match) el.value = match.value; else el.value = value;
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                """, el, str(value))
            else:
                from selenium.webdriver.support.ui import Select
                sel = Select(el)
                try:
                    sel.select_by_value(str(int(value)))
                except Exception:
                    try:
                        sel.select_by_visible_text(str(int(value)))
                    except Exception:
                        # 月份 select 可能是 0-based，也可能是 1-based；先 value/text，不行再 index。
                        sel.select_by_index(max(0, int(value)-1))
                driver.execute_script("arguments[0].dispatchEvent(new Event('change', {bubbles:true}));", el)
        else:
            _human_type_text(driver, el, str(value), clear=True)
        return True
    except Exception as exc:
        logger.debug('%s 填写字段失败 selectors=%s error=%s', _log_prefix(driver), selectors, type(exc).__name__)
        return False


def _fill_birthday_or_age(driver, birthday: str, age: int) -> str | None:
    """填写 about-you 的年龄/生日控件。

    参考 FlowPilot：优先处理直接年龄 input；否则兼容 hidden birthday/date、原生年月日
    select/input、React Aria hidden native select、role=spinbutton[data-type=year/month/day]。
    返回 age / birthday / ymd / react_select / spinbutton / None。
    """
    y, m, d = birthday.split('-')
    result = driver.execute_script(r"""
    const birthday = String(arguments[0]);
    const year = String(arguments[1]);
    const month = String(Number(arguments[2]));
    const month2 = String(arguments[2]).padStart(2, '0');
    const day = String(Number(arguments[3]));
    const day2 = String(arguments[3]).padStart(2, '0');
    const age = String(arguments[4]);
    const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
      && !el.disabled && !el.readOnly;
    const setValue = (el, value) => {
      if (!el) return false;
      el.scrollIntoView?.({block:'center'});
      el.focus?.();
      const tag = (el.tagName || '').toLowerCase();
      const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype
        : tag === 'select' ? HTMLSelectElement.prototype
        : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
      if (setter) setter.call(el, String(value)); else el.value = String(value);
      if (tag === 'select') {
        [...el.options].forEach(opt => { opt.selected = String(opt.value) === String(value); });
      }
      el.dispatchEvent(new Event('input', {bubbles:true}));
      el.dispatchEvent(new Event('change', {bubbles:true}));
      el.blur?.();
      return true;
    };
    const ageInput = [...document.querySelectorAll('input[name="age"], input#age, input[id$="-age"], input[type="number"]')]
      .find(visible);
    if (ageInput && setValue(ageInput, age)) return {ok:true, mode:'age'};

    const dateInput = [...document.querySelectorAll('input[name="birthdate"], input[type="date"], input[name="birthday"]')]
      .find(el => visible(el) || String(el.getAttribute('type') || '').toLowerCase() === 'date');
    if (dateInput && setValue(dateInput, birthday)) return {ok:true, mode:'birthday'};

    const setFirst = (selectors, values) => {
      for (const sel of selectors) {
        for (const el of [...document.querySelectorAll(sel)]) {
          if (!visible(el)) continue;
          for (const val of values) {
            if (el.tagName === 'SELECT') {
              const has = [...el.options].some(o => String(o.value) === String(val) || String(o.textContent || '').trim() === String(val));
              if (!has) continue;
            }
            if (setValue(el, val)) return true;
          }
        }
      }
      return false;
    };
    const yOk = setFirst(['select[name="year"]','input[name="year"]','select[id*="year"]','input[id*="year"]'], [year]);
    const mOk = setFirst(['select[name="month"]','input[name="month"]','select[id*="month"]','input[id*="month"]'], [month, month2]);
    const dOk = setFirst(['select[name="day"]','input[name="day"]','select[id*="day"]','input[id*="day"]'], [day, day2]);
    if (yOk && mOk && dOk) {
      const hidden = document.querySelector('input[name="birthday"]');
      if (hidden) setValue(hidden, birthday);
      return {ok:true, mode:'ymd'};
    }

    // React Aria Select 通常有 hidden native select；不依赖标签文字，按 option 数值范围和 DOM 顺序推断年/月/日。
    const selects = [...document.querySelectorAll('[data-testid="hidden-select-container"] select, .react-aria-Select select, select')]
      .filter(el => !el.disabled);
    const nums = sel => [...sel.options].map(o => Number(o.value)).filter(Number.isFinite);
    const maxNum = sel => Math.max(...nums(sel), -Infinity);
    const minNum = sel => Math.min(...nums(sel), Infinity);
    const hasOption = (sel, val) => [...sel.options].some(o => String(o.value) === String(val));
    const yearSelects = selects.filter(sel => hasOption(sel, year) && maxNum(sel) > 1900);
    const smallSelects = selects.filter(sel => !yearSelects.includes(sel));
    const monthSelects = smallSelects.filter(sel => (hasOption(sel, month) || hasOption(sel, month2)) && minNum(sel) <= 1 && maxNum(sel) <= 12);
    const daySelects = smallSelects.filter(sel => (hasOption(sel, day) || hasOption(sel, day2)) && maxNum(sel) >= 28);
    if (yearSelects.length && monthSelects.length && daySelects.length) {
      const ys = yearSelects[0];
      let ms = monthSelects[0];
      let ds = daySelects.find(x => x !== ms) || daySelects[0];
      setValue(ys, year);
      setValue(ms, hasOption(ms, month) ? month : month2);
      setValue(ds, hasOption(ds, day) ? day : day2);
      const hidden = document.querySelector('input[name="birthday"]');
      if (hidden) setValue(hidden, birthday);
      return {ok:true, mode:'react_select'};
    }

    const spinYear = document.querySelector('[role="spinbutton"][data-type="year"]');
    const spinMonth = document.querySelector('[role="spinbutton"][data-type="month"]');
    const spinDay = document.querySelector('[role="spinbutton"][data-type="day"]');
    if (spinYear && spinMonth && spinDay) return {ok:false, mode:'spinbutton_needed'};
    return {ok:false, mode:'missing'};
    """, birthday, y, m, d, str(age)) or {}
    if result.get('ok'):
        return str(result.get('mode') or 'birthday')
    if result.get('mode') != 'spinbutton_needed':
        return None

    try:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        mod = Keys.COMMAND
        try:
            import platform
            if platform.system().lower() != 'darwin':
                mod = Keys.CONTROL
        except Exception:
            pass
        for selector, value in [
            ('[role="spinbutton"][data-type="year"]', y),
            ('[role="spinbutton"][data-type="month"]', str(m).zfill(2)),
            ('[role="spinbutton"][data-type="day"]', str(d).zfill(2)),
        ]:
            el = driver.find_element(By.CSS_SELECTOR, selector)
            driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].focus();", el)
            time.sleep(0.1)
            el.send_keys(mod, 'a')
            time.sleep(0.05)
            el.send_keys(str(value))
            time.sleep(0.1)
            driver.execute_script("arguments[0].dispatchEvent(new Event('input', {bubbles:true})); arguments[0].dispatchEvent(new Event('change', {bubbles:true})); arguments[0].blur();", el)
        driver.execute_script(r"""
        const hidden = document.querySelector('input[name="birthday"]');
        if (hidden) {
          const value = arguments[0];
          const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
          if (setter) setter.call(hidden, value); else hidden.value = value;
          hidden.dispatchEvent(new Event('input', {bubbles:true}));
          hidden.dispatchEvent(new Event('change', {bubbles:true}));
        }
        """, birthday)
        return 'spinbutton'
    except Exception as exc:
        logger.debug('%s spinbutton 生日填写失败：%s', _log_prefix(driver), _safe_exception_text(exc))
        return None


def _generate_roxy_password() -> str:
    return generate_registration_password()


def _registration_password() -> str:
    return registration_password()


def _password_page_state(driver) -> dict:
    """Return structural password-page diagnostics without password/form values."""
    try:
        return driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const inputs = [...document.querySelectorAll('input')].map(el => ({
          type: el.getAttribute('type') || '', name: el.getAttribute('name') || '', id: el.id || '',
          autocomplete: el.getAttribute('autocomplete') || '', visible: visible(el)
        })).filter(x => x.visible).slice(0, 30);
        const forms = [...document.querySelectorAll('form')].map(() => ({}));
        const buttons = [...document.querySelectorAll('button,input[type="submit"]')].map(el => ({
          type: el.getAttribute('type') || '', name: el.getAttribute('name') || '', id: el.id || '',
          disabled: !!el.disabled, visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
        })).filter(x => x.visible).slice(0, 30);
        return {url: location.pathname || '/', inputs, forms, buttons};
        """) or {}
    except Exception as exc:
        return {
            "url": _diagnostic_route(getattr(driver, "current_url", "")),
            "error": "页面检查失败",
            "error_type": type(exc).__name__,
        }


def _is_signup_password_page(driver) -> bool:
    state = _password_page_state(driver)
    url = str(state.get('url') or '').lower()
    if any(x in url for x in ('/create-account/password', '/u/signup/password', '/signup/password')):
        return True
    if '/log-in/password' in url:
        return False
    inputs = state.get('inputs') or []
    return any(
        i.get('visible') and (
            str(i.get('type') or '').lower() == 'password'
            or 'password' in str(i.get('name') or '').lower()
            or str(i.get('autocomplete') or '').lower() == 'new-password'
        )
        for i in inputs
    )


def _is_login_password_page(driver) -> bool:
    try:
        url = str(driver.current_url or '').lower()
    except Exception:
        url = ''
    if '/log-in/password' in url:
        return True
    state = _password_page_state(driver)
    url = str(state.get('url') or '').lower()
    return '/log-in/password' in url


def _click_continue_with_password_if_present(driver, timeout: int = 20) -> dict:
    """在邮箱验证码页切换到密码注册流程。

    ChatGPT 新版注册页会先显示邮箱验证码，并提供
    ``Continue with password`` 链接；只有点击该链接后才会出现
    ``/create-account/password``。启用 2FA 时必须走这条分支，避免把
    一次性邮箱验证码误当成最终登录方式。
    """
    try:
        result = driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
        const norm = value => String(value || '').replace(/\s+/g, '').toLowerCase();
        const candidates = [...document.querySelectorAll('a,button,[role="button"],[role="link"]')]
          .filter(visible);
        const target = candidates.find(el => {
          const href = String(el.getAttribute('href') || '').toLowerCase();
          const text = norm(el.textContent || el.getAttribute('aria-label') || '');
          const attrs = [el.id, el.getAttribute('name'), el.getAttribute('data-testid'),
            el.getAttribute('data-dd-action-name'), el.getAttribute('aria-label'), el.getAttribute('title'),
            el.className, el.textContent].join(' ').toLowerCase();
          if (href.includes('/create-account/password')) return true;
          return text.includes('continuewithpassword')
            || attrs.includes('continue with password')
            || attrs.includes('continuewithpassword');
        });
        if (!target) return {ok:false, reason:'missing_continue_with_password'};
        target.scrollIntoView({block:'center'});
        return {
          ok:true,
          element: target,
          href: String(target.getAttribute('href') || ''),
          text: String(target.textContent || target.getAttribute('aria-label') || '').trim().slice(0, 80)
        };
        """) or {"ok": False, "reason": "empty_result"}
        if not result.get("ok") or not result.get("element"):
            return {"ok": False, "reason": "missing_continue_with_password"}
        _human_click(driver, result["element"], label="continue_with_password")
        end = time.time() + max(3, int(timeout))
        while time.time() < end:
            if _is_signup_password_page(driver) or _is_login_password_page(driver) or _has_access_token(driver):
                return {
                    "ok": True,
                    "reason": "clicked_continue_with_password",
                }
            time.sleep(0.4)
        return {
            "ok": False,
            "reason": "continue_with_password_did_not_open_password_page",
            "route": _diagnostic_route(getattr(driver, "current_url", "")),
        }
    except Exception as exc:
        return {"ok": False, "reason": "continue_with_password_failed", "error_type": type(exc).__name__}


def _click_passwordless_signup_if_present(driver) -> dict:
    """
    新版注册/登录流在 password 页可能默认要求密码。
    如果页面提供“使用一次性验证码”按钮，优先点击进入邮箱 OTP 页面。
    """
    try:
        result = driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
        const enabled = el => !el.disabled && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
        const norm = s => String(s || '').replace(/\s+/g, '').toLowerCase();
        const candidates = [...document.querySelectorAll('button,a,input[type="submit"],[role="button"],[role="link"]')].filter(el => visible(el) && enabled(el));
        const isPasswordlessOtp = el => {
          const name = String(el.getAttribute('name') || '').toLowerCase();
          const value = String(el.getAttribute('value') || '').toLowerCase();
          const attrs = [
            el.id, name, value, el.getAttribute('aria-label'), el.getAttribute('title'),
            el.getAttribute('data-testid'), el.getAttribute('data-dd-action-name'), el.className, el.textContent
          ].join(' ').toLowerCase();
          const text = norm(el.textContent || el.getAttribute('value') || '');
          return (
            (name === 'intent' && value.includes('passwordless') && value.includes('send_otp')) ||
            (name === 'intent' && value.includes('passwordless') && value.includes('otp')) ||
            (name === 'intent' && value === 'passwordless_signup_send_otp') ||
            (name === 'intent' && value === 'passwordless_login_send_otp') ||
            attrs.includes('passwordless_signup_send_otp') ||
            attrs.includes('passwordless_login_send_otp') ||
            /passwordless.*otp|otp.*passwordless|one[-_\s]?time.*code|code.*one[-_\s]?time/.test(attrs) ||
            text.includes('使用一次性验证码注册') ||
            text.includes('使用一次性验证码登录') ||
            text.includes('使用一次性验证码') ||
            text.includes('使用一次性驗證碼註冊') ||
            text.includes('使用一次性驗證碼登入') ||
            text.includes('一次性验证码') ||
            text.includes('一次性驗證碼') ||
            text.includes('メールでコード') ||
            text.includes('ワンタイムコード') ||
            text.includes('認証コード') ||
            text.includes('useonetimeregistrationcode') ||
            text.includes('useaone-timecodetosignup') ||
            text.includes('useaone-timecodetoregister') ||
            text.includes('useaone-timecodetologin') ||
            text.includes('continuewithaone-timecode') ||
            text.includes('loginwithaone-timecode') ||
            text.includes('signupwithaone-timecode') ||
            text.includes('one-timecode')
          );
        };
        const btn = candidates.find(isPasswordlessOtp);
        if (!btn) return {ok:false, reason:'missing_passwordless_button'};
        btn.scrollIntoView({block:'center'});
        return {
          ok:true,
          reason:'passwordless_send_otp_target',
          button: btn,
          name: btn.getAttribute('name') || '',
          value: btn.getAttribute('value') || '',
          text: (btn.textContent || '').trim().slice(0, 80)
        };
        """) or {"ok": False, "reason": "empty_result"}
        if result.get("ok") and result.get("button"):
            _human_click(driver, result.get("button"), label="passwordless_otp")
            return {"ok": True, "reason": "clicked_passwordless_send_otp"}
        return {"ok": False, "reason": "missing_passwordless_button"}
    except Exception as exc:
        return {"ok": False, "reason": "passwordless_click_failed", "error_type": type(exc).__name__}


def _fill_password_page_if_present(
    driver, email: str, timeout: int = 25, *, allow_passwordless_signup: bool = True,
) -> str | None:
    """邮箱提交后兼容 create-account/password。返回本次设置的 OpenAI 账号密码；未遇到密码页返回 None。"""
    end = time.time() + timeout
    last = {}
    while time.time() < end:
        # In the strict password branch the previous page can remain
        # ``/email-verification`` for a few seconds after clicking
        # ``Continue with password``.  Returning here would silently turn
        # the flow back into OTP-only registration.  Keep waiting for the
        # password route; the caller will fail closed after ``timeout`` if
        # the switch never completed.
        if _is_email_verification_page(driver):
            if allow_passwordless_signup:
                return None
            time.sleep(0.4)
            continue
        if _has_access_token(driver):
            if allow_passwordless_signup:
                return None
            raise RuntimeError("启用 2FA 时密码页未完成，页面已出现登录态但未确认新密码")
        last = _password_page_state(driver)
        is_signup_password = _is_signup_password_page(driver)
        is_login_password = _is_login_password_page(driver)
        if not (is_signup_password or is_login_password):
            time.sleep(0.5)
            continue
        passwordless = _click_passwordless_signup_if_present(driver) if allow_passwordless_signup else {"ok": False}
        if passwordless.get('ok'):
            logger.info(
                "%s 检测到密码页，按原注册流程已切换到邮箱一次性验证码：email=%s reason=%s",
                _log_prefix(driver), email, passwordless.get("reason") or "-",
            )
            wait_end = time.time() + 20
            while time.time() < wait_end:
                if _is_email_verification_page(driver):
                    logger.info("%s 一次性验证码入口已进入邮箱验证码页", _log_prefix(driver))
                    return None
                if _has_access_token(driver):
                    logger.info("%s 一次性验证码入口后已检测到登录态", _log_prefix(driver))
                    return None
                time.sleep(0.5)
            logger.info("%s 已点击一次性验证码入口，交给后续 OTP 阶段继续处理", _log_prefix(driver))
            return None
        if is_login_password:
            if not allow_passwordless_signup:
                raise RuntimeError("注册进入已有账号的登录密码页，无法确认本次设置了新密码")
            logger.info("%s 当前是登录密码页但未找到一次性验证码入口，跳过密码填写并交给 OTP 阶段：state=%s", _log_prefix(driver), _diagnostic_state(last))
            return None
        password = _registration_password()
        logger.info("%s 检测到 create-account/password，准备设置密码（%s 位）：email=%s", _log_prefix(driver), len(password), email)
        for submission_attempt in range(1, 4):
            if submission_attempt > 1:
                # The retry action can advance directly to the next step.
                if _is_email_verification_page(driver) or _has_access_token(driver) or _is_profile_like(_page_snapshot(driver)):
                    return password
                if not _is_signup_password_page(driver):
                    raise RegistrationPasswordSubmissionError(
                        "Route Error 重试后没有回到新密码页或下一步；账号状态不确定，请勿重用此邮箱"
                    )
            result = driver.execute_script(r"""
            const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
              && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
              && !el.disabled && !el.readOnly;
            const input = [...document.querySelectorAll('input[type="password"],input[name*="password" i],input[autocomplete="new-password"]')]
              .find(visible);
            if (!input) return {ok:false, reason:'missing_password_input'};
            const form = input.closest('form');
            const scope = form || document;
            const buttons = [...scope.querySelectorAll('button,input[type="submit"]')]
              .filter(el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length) && !el.disabled && el.getAttribute('aria-disabled') !== 'true')
              .map((el, idx) => {
                const r = el.getBoundingClientRect();
                const ir = input.getBoundingClientRect();
                return {el, idx, below: r.top >= ir.bottom - 10, dist: Math.max(0, r.top - ir.bottom) + Math.abs((r.left+r.right-ir.left-ir.right)/2)/10};
              })
              .filter(x => x.below)
              .sort((a,b) => a.dist - b.dist || a.idx - b.idx);
            if (!buttons.length) return {ok:false, reason:'missing_submit'};
            buttons[0].el.scrollIntoView({block:'center'});
            return {ok:true, reason:'password_targets', input, button: buttons[0].el};
            """) or {}
            if not result.get('ok'):
                raise RuntimeError(f"密码页处理失败：{_diagnostic_state(result)} state={_diagnostic_state(last)}")
            password_input = result.get("input")
            _human_type_text(driver, password_input, password, clear=True)
            actual_password = str(password_input.get_attribute("value") or "")
            if actual_password != password:
                logger.warning(
                    "%s 密码写入值不一致，使用 React 原生 setter 修正：expected_length=%s actual_length=%s",
                    _log_prefix(driver),
                    len(password),
                    len(actual_password),
                )
                _set_element_value(driver, password_input, password)
                actual_password = str(password_input.get_attribute("value") or "")
            if actual_password != password:
                raise RuntimeError(
                    f"密码写入校验失败，已停止提交: "
                    f"expected_length={len(password)} actual_length={len(actual_password)}"
                )
            human_delay("form", minimum=0.4, maximum=1.4)
            monitor = _RegistrationRequestMonitor(driver)
            retry_route_error = False
            try:
                try:
                    _human_click(driver, result.get("button"), label="password_submit")
                except Exception as exc:
                    raise RegistrationPasswordSubmissionError(
                        "注册密码提交结果不确定：点击后浏览器报错；请检查账号状态，勿重用此邮箱"
                    ) from exc
                logger.info("%s 已填写并提交密码页（第 %s/3 次）", _log_prefix(driver), submission_attempt)
                wait_end = time.time() + 20
                while time.time() < wait_end:
                    response = monitor.poll()
                    if response and response["status"] >= 400:
                        # The known 400 HTML Route Error has a recovery action;
                        # other 400s, challenges and 403s are terminal.
                        route_error = False
                        if response["status"] == 400 and not response.get("challenge_detected"):
                            route_wait = time.time() + 2
                            while time.time() < route_wait:
                                if _password_submission_route_error(driver):
                                    route_error = True
                                    break
                                time.sleep(0.2)
                        if route_error and submission_attempt < 3:
                            retry_route_error = True
                            break
                        challenge = "Cloudflare challenge；" if response.get("challenge_detected") else ""
                        raise RegistrationPasswordSubmissionError(
                            "注册密码提交结果不确定：注册接口 "
                            f"{response['method']} HTTP {response['status']} "
                            f"Content-Type={response['content_type']}；{challenge}"
                            "远端是否创建账号尚未确认，请勿重用此邮箱"
                        )
                    if _password_submission_route_error(driver):
                        if submission_attempt < 3:
                            retry_route_error = True
                            break
                        detail = (
                            f"注册接口 HTTP {response['status']} Content-Type={response['content_type']}"
                            if response else "注册接口响应状态未捕获"
                        )
                        raise RegistrationPasswordSubmissionError(
                            f"注册密码提交结果不确定：页面出现 Route Error，{detail}；请勿重用此邮箱"
                        )
                    if _is_email_verification_page(driver):
                        logger.info("%s 密码提交后已进入邮箱验证码页", _log_prefix(driver))
                        return password
                    if _has_access_token(driver):
                        logger.info("%s 密码提交后已检测到登录态", _log_prefix(driver))
                        return password
                    if _is_profile_like(_page_snapshot(driver)):
                        logger.info("%s 密码提交后已进入资料页", _log_prefix(driver))
                        return password
                    time.sleep(0.5)
                if not retry_route_error:
                    raise RegistrationPasswordSubmissionError(
                        "注册密码提交结果不确定：页面未进入验证码页、资料页或登录态；"
                        f"state={_diagnostic_state(_password_page_state(driver))}；请勿重用此邮箱"
                    )
            finally:
                monitor.close()
            logger.warning("%s 注册密码页出现 HTTP 400 Route Error，尝试页面恢复（%s/2）", _log_prefix(driver), submission_attempt)
            if not _click_route_error_try_again(driver):
                raise RegistrationPasswordSubmissionError(
                    "Route Error 页面没有可用的 Try again 按钮；账号状态不确定，请勿重用此邮箱"
                )
            recovery_end = time.time() + 8
            while time.time() < recovery_end:
                if not _password_submission_route_error(driver):
                    if (_is_signup_password_page(driver) or _is_email_verification_page(driver)
                            or _has_access_token(driver) or _is_profile_like(_page_snapshot(driver))):
                        break
                time.sleep(0.3)
            else:
                raise RegistrationPasswordSubmissionError(
                    "Route Error 重试后未回到密码页或下一步；账号状态不确定，请勿重用此邮箱"
                )
    if not allow_passwordless_signup:
        raise RuntimeError(
            "已启用 2FA，但点击密码注册入口后未检测到可填写的密码页；"
            f"已停止邮箱 OTP 单独登录的注册: state={_diagnostic_state(last)}"
        )
    logger.info("%s 未检测到密码页，继续后续流程 last=%s", _log_prefix(driver), _diagnostic_state(last))
    return None


def _accept_profile_consents(driver) -> int:
    """about-you/profile 下出现韩国/日本个人信息同意协议时，默认全部勾选。

    不依赖可见文字；优先处理 allCheckboxes，再处理所有必选 consent checkbox。
    """
    try:
        result = driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled;
        const isChecked = el => el.checked === true || String(el.getAttribute('aria-checked') || el.closest('[role="checkbox"]')?.getAttribute('aria-checked') || '').toLowerCase() === 'true';
        const mark = el => {
          if (!el || isChecked(el)) return false;
          const label = el.closest('label');
          try {
            (label && visible(label) ? label : el).scrollIntoView({block:'center'});
            (label && visible(label) ? label : el).click();
          } catch (_) {}
          if (!isChecked(el)) {
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'checked')?.set;
            if (setter) setter.call(el, true); else el.checked = true;
            el.dispatchEvent(new MouseEvent('click', {bubbles:true}));
            el.dispatchEvent(new Event('input', {bubbles:true}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
          }
          return isChecked(el);
        };
        const all = [...document.querySelectorAll('input[type="checkbox"]')]
          .filter(el => visible(el) || visible(el.closest('label')));
        if (!all.length) return {count:0, names:[]};
        const byName = name => all.find(el => String(el.name || '').toLowerCase() === name.toLowerCase());
        const ordered = [];
        const add = el => { if (el && !ordered.includes(el)) ordered.push(el); };
        add(byName('allCheckboxes'));
        for (const name of ['personalInfoConsent', 'thirdPartyConsent', 'overseasTransferConsent']) add(byName(name));
        for (const el of all) {
          const n = String(el.name || '').toLowerCase();
          const id = String(el.id || '').toLowerCase();
          if (/consent|checkbox|agree|required|personal|third|overseas/.test(`${n} ${id}`)) add(el);
        }
        // about-you/profile 页面里的 checkbox 基本都是必选 consent；剩余可见 checkbox 也全部勾选。
        for (const el of all) add(el);
        const clicked = [];
        for (const el of ordered) {
          if (mark(el)) clicked.push(el.name || el.id || 'checkbox');
        }
        return {count: clicked.length, names: clicked};
        """) or {}
        count = int(result.get('count') or 0)
        if count:
            logger.info("%s 已勾选 about-you/profile 同意协议复选框：%s", _log_prefix(driver), result.get('names'))
        return count
    except Exception as exc:
        logger.debug('%s 勾选 profile consent 失败：%s', _log_prefix(driver), _safe_exception_text(exc))
        return 0


def _complete_profile_page(driver, name: str, birthday: str, timeout: int = 45) -> bool:
    """等待并完成姓名/生日页；若已经登录成功则返回 False，不把它当失败。"""
    end = time.time() + timeout
    y, m, d = birthday.split('-')
    from datetime import date
    today = date.today()
    age = today.year - int(y) - ((today.month, today.day) < (int(m), int(d)))
    last_snapshot = {}
    while time.time() < end:
        time.sleep(1)
        if _has_access_token(driver):
            logger.info('%s 已检测到登录态，资料页可能已跳过', _log_prefix(driver))
            return False
        snap = _page_snapshot(driver)
        last_snapshot = snap
        if not _is_profile_like(snap):
            logger.info('%s 等待资料页中：route=%s', _log_prefix(driver), _diagnostic_route(snap.get('url')))
            continue

        logger.info('%s 检测到资料页，开始填写姓名生日：state=%s', _log_prefix(driver), _diagnostic_state(snap))
        name_ok = False
        # 常见单姓名字段
        for selectors in [
            ["input[name='name']", "input[name='fullName']", "input[name='full_name']", "input[autocomplete='name']"],
            ["input[placeholder*='Name']", "input[placeholder*='name']", "input[aria-label*='Name']", "input[aria-label*='name']"],
        ]:
            if _select_or_type(driver, selectors, name, timeout=3):
                logger.info("%s 已填写姓名字段：%s", _log_prefix(driver), name)
                name_ok = True
                break
        # 兼容 first/last 分开
        if not name_ok:
            parts = name.split(' ', 1)
            first = parts[0]
            last = parts[1] if len(parts) > 1 else 'User'
            first_ok = _select_or_type(driver, ["input[name='firstName']", "input[name='first_name']", "input[placeholder*='First']", "input[aria-label*='First']"], first, timeout=2)
            last_ok = _select_or_type(driver, ["input[name='lastName']", "input[name='last_name']", "input[placeholder*='Last']", "input[aria-label*='Last']"], last, timeout=2)
            name_ok = first_ok or last_ok

        birth_mode = _fill_birthday_or_age(driver, birthday, age)
        birth_ok = bool(birth_mode)
        if birth_ok:
            if birth_mode == 'age':
                logger.info("%s 已填写年龄字段：%s", _log_prefix(driver), age)
            else:
                logger.info("%s 已填写生日字段 mode=%s value=%s", _log_prefix(driver), birth_mode, birthday)

        if not name_ok or not birth_ok:
            logger.warning('%s 资料页字段未填完整 name_ok=%s birth_ok=%s snapshot=%s', _log_prefix(driver), name_ok, birth_ok, _diagnostic_state(snap))
            continue

        _accept_profile_consents(driver)
        human_delay('form')
        for _ in range(3):
            if _click_if_enabled_submit(driver):
                logger.info('%s 已点击资料页提交按钮，等待 OAuth 跳转', _log_prefix(driver))
                return True
            time.sleep(1)
        logger.warning('%s 找不到可点击的资料页提交按钮 snapshot=%s', _log_prefix(driver), _diagnostic_state(_page_snapshot(driver)))
    raise RuntimeError(f'等待/填写资料页超时，最后页面：{_diagnostic_state(last_snapshot)}')


def _click_if_enabled_submit(driver) -> bool:
    """提交资料页：优先 form.requestSubmit/button[type=submit]，不依赖按钮文字。"""
    try:
        target = driver.execute_script(r"""
        const visible = (el) => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const forms = [...document.querySelectorAll('form')].filter(visible);
        for (const form of forms) {
          const submit = form.querySelector('button[type="submit"], input[type="submit"]');
          if (submit && visible(submit) && !submit.disabled) {
            submit.scrollIntoView({block:'center'});
            return submit;
          }
          if (typeof form.requestSubmit === 'function') {
            form.requestSubmit();
            return 'submitted_by_requestSubmit';
          }
        }
        const submitters = [...document.querySelectorAll('button[type="submit"], input[type="submit"]')]
          .filter(el => visible(el) && !el.disabled);
        if (submitters.length) {
          submitters[0].scrollIntoView({block:'center'});
          return submitters[0];
        }
        // 兜底：页面只有一个可点击 button 时点击它，但仍不读文字。
        const buttons = [...document.querySelectorAll('button:not([disabled])')].filter(visible);
        if (buttons.length === 1) {
          buttons[0].scrollIntoView({block:'center'});
          return buttons[0];
        }
        return null;
        """)
        if not target:
            return False
        if isinstance(target, str):
            return True
        _human_click(driver, target, label="profile_submit")
        return True
    except Exception:
        return False


def _read_chatgpt_session_once(driver) -> dict | None:
    """当前页面必须在 chatgpt.com；读取 /api/auth/session，拿不到 token 返回 None。"""
    script = r"""
    const done = arguments[0];
    fetch('/api/auth/session', {credentials: 'include'})
      .then(r => r.json())
      .then(j => done({ok: true, data: j}))
      .catch(e => done({ok: false, error: String(e)}));
    """
    result = driver.execute_async_script(script)
    if result and result.get("ok"):
        data = result.get("data") or {}
        if data.get("accessToken"):
            logger.info("%s /api/auth/session 已返回 accessToken", _log_prefix(driver))
            return data
        logger.info("%s 等待 ChatGPT session 写入 accessToken，当前响应 keys=%s", _log_prefix(driver), list(data.keys()))
    return None


def _switch_to_chatgpt_window_if_any(driver) -> bool:
    """有些浏览器/适配层会在新窗口完成 callback；尝试切到已有 chatgpt.com 句柄。"""
    try:
        handles = list(getattr(driver, "window_handles", []) or [])
        current_handle = None
        try:
            current_handle = getattr(driver, "current_window_handle", None)
        except Exception:
            current_handle = None
        for handle in handles:
            try:
                driver.switch_to.window(handle)
                if "chatgpt.com" in str(getattr(driver, "current_url", "") or ""):
                    return True
            except Exception:
                continue
        if current_handle is not None:
            try:
                driver.switch_to.window(current_handle)
            except Exception:
                pass
    except Exception:
        pass
    return False


def _fetch_chatgpt_session(driver, timeout: int = 90, auto_jump_wait: int = 15) -> dict:
    """等待页面完成跳转并从 ChatGPT 页面内读取登录 session/accessToken。

    旧逻辑会在 auth.openai.com 上一直等到总超时，Cloak/部分 Chromium 场景下
    实际账号已创建成功但当前句柄 URL 没及时更新，导致白等 120 秒。现在只给
    自动跳转 `auto_jump_wait` 秒；超过后立即主动打开 chatgpt.com 读 session。
    """
    end = time.time() + timeout
    auto_jump_end = time.time() + max(3, int(auto_jump_wait or 15))
    last_data = None
    forced_chatgpt_open = False

    while time.time() < end:
        try:
            current = str(driver.current_url or '')
        except Exception:
            current = ''

        if 'chatgpt.com' not in current:
            if _switch_to_chatgpt_window_if_any(driver):
                current = str(getattr(driver, "current_url", "") or "")
            elif time.time() >= auto_jump_end and not forced_chatgpt_open:
                try:
                    logger.info("%s 未在 %ss 内观察到当前窗口跳转 chatgpt.com，主动打开 ChatGPT 内读取 session", _log_prefix(driver), int(auto_jump_wait or 15))
                    _safe_get(driver, "https://chatgpt.com/", timeout=35, attempts=2, accept_hosts=("chatgpt.com",))
                    forced_chatgpt_open = True
                    time.sleep(3)
                    current = str(getattr(driver, "current_url", "") or "")
                except Exception as exc:
                    last_data = f"{type(exc).__name__}: {exc}"
            else:
                time.sleep(1)
                continue

        if 'chatgpt.com' in current:
            try:
                data = _read_chatgpt_session_once(driver)
                if data:
                    return data
                last_data = "session 暂无 accessToken"
            except Exception as exc:
                last_data = f"{type(exc).__name__}: {exc}"
        time.sleep(2)

    raise RuntimeError(f"等待 /api/auth/session accessToken 超时，最后响应: {str(last_data)[:800]}")


def _check_manual_stop() -> None:
    check_stop_requested()


def _profile_proxy_for_2fa(opened: RoxyOpenResult, client: RoxyBrowserClient, proxy: str | None) -> str:
    """Read the matching Roxy Profile route before exporting its login cookies."""
    from core.roxy_codex_oauth import _roxy_profile_token_proxy

    return _roxy_profile_token_proxy(opened, proxy, client)


def _setup_2fa_after_registration(driver, email: str, opened: RoxyOpenResult, client: RoxyBrowserClient, proxy: str | None) -> tuple[str, str]:
    """Enroll TOTP in the live Roxy browser profile.

    Roxy's CDP/Selenium browser is the source of truth for cookies, device
    state, proxy routing, and the Cloudflare challenge.  Moving cookies into a
    separate HTTP/TLS client can make the reauthentication or MFA endpoints
    return 403 even when the browser page is valid, so every 2FA request stays
    in this same browser tab.
    """
    # Keep the read-only profile route check as a diagnostic guard. It proves
    # the profile was opened with a known route before starting the irreversible
    # reauthentication/enrollment steps; the actual requests use the browser.
    profile_proxy = _profile_proxy_for_2fa(opened, client, proxy)
    from core.browser_2fa_session import BrowserBacked2FASession

    session = BrowserBacked2FASession(driver)
    secret = setup_2fa(session, email, otp_provider=wait_for_otp)
    return secret, profile_proxy


def run_roxy_registration(email: str, name: str, birthday: str, proxy: str = None, otp_code: str = None, batch_dir: Path | None = None) -> dict:
    """Roxy 指纹浏览器自动化注册入口。"""
    client = RoxyBrowserClient()
    opened = None
    driver = None
    create_acknowledged = False
    openai_password: str | None = None
    require_2fa = bool(_twofa_cfg.ENABLE_2FA)
    # WebUI may reload configuration during a task; keep this requirement
    # fixed until the account has passed login verification and been saved.
    try:
        try:
            opened = client.open_profile(proxy=proxy)
        except TypeError as exc:
            if "unexpected keyword argument 'proxy'" not in str(exc):
                raise
            opened = client.open_profile()
        driver = _build_driver(opened)
        # Fixed/kept-open Roxy Profiles may still contain the previous
        # ChatGPT session. Clear it before opening the registration page so
        # the task cannot silently land on an already logged-in home page.
        _clear_chatgpt_auth_state(driver)
        _center_browser_window(driver)
        driver.set_page_load_timeout(int(_cfg.ROXY_SELENIUM_TIMEOUT))
        try:
            driver.set_script_timeout(12)
        except Exception:
            pass
        logger.info("[Roxy注册] 开始：%s，profile=%s", email, opened.profile_id)

        otp_after_ts = time.time()
        logger.info("[Roxy注册] 打开登录页：https://chatgpt.com/auth/login")
        _safe_get(
            driver,
            "https://chatgpt.com/auth/login",
            timeout=min(45, int(getattr(_cfg, "ROXY_SELENIUM_TIMEOUT", 90) or 90)),
            attempts=2,
            accept_hosts=("chatgpt.com", "auth.openai.com"),
        )
        human_delay("navigate")
        _page_warmup(driver, reason="login_page")
        logger.info("[Roxy注册] 登录页加载完成，准备填写邮箱")
        _maybe_accept(driver)
        _check_manual_stop()

        # 填邮箱。OpenAI UI 会随出口 IP/语言变化；这里只按 DOM 技术属性找邮箱入口，
        # 并排除 Google/Apple/Microsoft 等第三方入口，不依赖按钮可见文字。
        next_state = _submit_email_and_wait_next(driver, email, attempts=3)
        _check_manual_stop()

        # 新版注册流可能先进入 /create-account/password；参考 FlowPilot 的 fill-password 步骤，
        # 先设置密码并提交，然后再等待邮箱验证码页。
        if next_state == "logged_in":
            openai_password = None
            create_acknowledged = True
            needs_email_otp = False
        elif next_state == "password":
            openai_password = _fill_password_page_if_present(
                driver, email, timeout=25, allow_passwordless_signup=not require_2fa,
            )
            if openai_password or _has_access_token(driver):
                create_acknowledged = True
            needs_email_otp = _is_email_verification_page(driver)
        else:
            if require_2fa:
                switch = _click_continue_with_password_if_present(driver)
                if not switch.get("ok"):
                    raise RuntimeError(
                        "已启用 2FA，但仍未设置密码；邮箱验证码页没有打开密码注册入口；"
                        f"已停止邮箱 OTP 单独登录的注册: {switch.get('reason')}"
                    )
                openai_password = _fill_password_page_if_present(
                    driver, email, timeout=25, allow_passwordless_signup=False,
                )
                if openai_password or _has_access_token(driver):
                    create_acknowledged = True
                needs_email_otp = _is_email_verification_page(driver)
            else:
                openai_password = None
                needs_email_otp = True
        if require_2fa and not openai_password:
            raise RuntimeError("已启用 2FA，但注册流程未设置密码；已停止邮箱 OTP 单独登录的注册")
        _check_manual_stop()

        current_otp = otp_code
        max_otp_attempts = 3
        for otp_attempt in (range(1, max_otp_attempts + 1) if needs_email_otp else ()):
            if current_otp is None:
                logger.info("[Roxy注册][OTP] 等待验证码：%s（第 %s/%s 次）", email, otp_attempt, max_otp_attempts)
                try:
                    current_otp = wait_for_otp(email, after_ts=otp_after_ts)
                except Exception as exc:
                    if otp_attempt >= max_otp_attempts:
                        raise
                    logger.warning(
                        "[Roxy注册][OTP] 一直未收到验证码，点击“重新发送电子邮件”后继续等待（下一轮 %s/%s）：%s: %s",
                        otp_attempt + 1,
                        max_otp_attempts,
                        type(exc).__name__,
                        str(exc)[:180],
                    )
                    otp_after_ts = time.time()
                    _click_resend_email_otp(driver, timeout=25)
                    human_delay("api")
                    current_otp = None
                    continue
            logger.info("[Roxy注册][OTP] 已收到邮箱验证码")
            _clear_otp_inputs(driver)
            _type_otp(driver, current_otp)
            logger.info("[Roxy注册][OTP] 已填写邮箱验证码")
            _check_manual_stop()
            human_delay("otp_input")
            try:
                _click_continue(driver)
                logger.info("[Roxy注册][OTP] 已提交邮箱验证码，等待资料页或登录态")
            except Exception as exc:
                logger.info("[Roxy注册][OTP] 未找到显式提交按钮，继续等待页面状态：%s", _safe_exception_text(exc, 120))

            outcome = _wait_after_email_otp_submit(driver, timeout=10)
            if outcome == 'accepted':
                create_acknowledged = True
                break
            if otp_attempt >= max_otp_attempts:
                raise RuntimeError("邮箱验证码连续错误/过期，已达到最大重试次数")
            logger.warning("[Roxy注册][OTP] 验证码错误/过期，准备重新发送并重新获取验证码（%s/%s）", otp_attempt + 1, max_otp_attempts)
            otp_after_ts = time.time()
            _click_resend_email_otp(driver, timeout=25)
            human_delay("api")
            current_otp = None

        # about-you / profile 信息页：必须完成或确认已有登录态，不能静默跳过。
        logger.info("[Roxy注册] 开始等待资料页/登录态")
        _check_manual_stop()
        profile_submitted = _complete_profile_page(driver, name, birthday, timeout=60)
        if profile_submitted:
            create_acknowledged = True
            # 给 OAuth 回调 / session cookie 写入一点时间。
            human_delay("post_auth")

        logger.info("[Roxy注册] 等待 ChatGPT 跳转并写入 session/accessToken")
        _check_manual_stop()
        session_info = _fetch_chatgpt_session(driver, timeout=120)
        access_token = session_info["accessToken"]
        create_acknowledged = True
        logger.info("[Roxy注册] 已拿到 accessToken：%s", email)
        _check_manual_stop()

        totp_secret = None
        effective_proxy = opened.proxy if opened.proxy is not None else proxy

        email_source = resolve_email_source(email)
        codex_result = {
            "status": "not_authorized",
            "ok": False,
            "message": "GPT 注册完成，等待在 GPT账号 页面手动发起 Codex 授权",
        }
        account_fields = {
            "user_id": (session_info.get("user") or {}).get("id"),
            "user_name": (session_info.get("user") or {}).get("name"),
            "plan_type": (session_info.get("account") or {}).get("planType"),
            "expires_at": session_info.get("expires"),
            "registration_driver": "roxy",
            "registration_profile_id": opened.profile_id,
            "codex_status": codex_result["status"],
        }
        if require_2fa:
            logger.info("[Roxy注册] 开始自动设置 2FA：%s", email)
            try:
                totp_secret, effective_proxy = _setup_2fa_after_registration(
                    driver, email, opened, client, proxy,
                )
                if not totp_secret:
                    raise RuntimeError("2FA 激活后未返回 TOTP secret")
            except Exception as exc:
                logger.error("[Roxy注册] 2FA 设置失败：%s: %s", type(exc).__name__, _safe_exception_text(exc))
                raise RuntimeError(f"Roxy 注册已创建密码，但 2FA 设置失败：{type(exc).__name__}: {_safe_exception_text(exc, 180)}") from exc
            logger.info("[Roxy注册] 2FA 设置完成：%s", email)

            # Keep the original single-browser flow. The verifier clears auth
            # state and proves a fresh login without starting stock Chrome or
            # transferring cookies to a different HTTP/TLS implementation.
            logger.info("[Roxy注册] 在当前 Roxy 环境中清理登录态并验证密码 + TOTP：%s", email)
            try:
                verified_info = verify_password_totp_login(
                    email, openai_password, totp_secret, proxy=effective_proxy, driver=driver,
                )
                access_token = verified_info["accessToken"]
            except Exception as exc:
                logger.error("[Roxy注册] 密码 + TOTP 重登录验证失败：%s", type(exc).__name__)
                raise RuntimeError(
                    "Roxy 注册已创建密码和 2FA，但当前环境的密码 + TOTP 登录验证失败："
                    f"{type(exc).__name__}: {_safe_exception_text(exc, 180)}"
                ) from exc
            logger.info("[Roxy注册] 密码 + TOTP 重登录验证成功：%s", email)

        account_id = save_account_data(
            email=email,
            access_token=access_token,
            totp_secret=totp_secret,
            email_source=email_source,
            chatgpt_password=openai_password,
            login_method="password" if openai_password else "email_otp",
            proxy=effective_proxy,
            batch_dir=batch_dir,
            **account_fields,
            archive=True,
            verified_registration=True,
            enqueue_plan=True,
        )
        return {
            "success": True,
            "email": email,
            "account_id": account_id,
            "access_token": access_token,
            "totp_secret": totp_secret,
            "codex": codex_result,
            "error": None,
        }
    except Exception as exc:
        logger.error("[Roxy注册] 失败：%s: %s", type(exc).__name__, _safe_exception_text(exc))
        logger.debug("[Roxy注册] 失败详情已省略敏感堆栈：%s", type(exc).__name__)
        # 未确认创建前回收邮箱；确认后避免重复使用。
        try:
            from core.email_provider import release_email
            release_email(
                email,
                status=("disabled" if isinstance(exc, RegistrationPasswordSubmissionError)
                        else "failed" if create_acknowledged else "available"),
                note=f"Roxy注册失败: {_safe_exception_text(exc, 180)}",
            )
        except Exception:
            pass
        return {"success": False, "email": email, "error": f"{type(exc).__name__}: {_safe_exception_text(exc, 300)}"}
    finally:
        if driver and not bool(_cfg.ROXY_KEEP_BROWSER_OPEN) and not getattr(opened, "was_open", False):
            try:
                driver.quit()
            except Exception:
                pass
        if not bool(_cfg.ROXY_KEEP_BROWSER_OPEN):
            client.cleanup_profile(opened)
