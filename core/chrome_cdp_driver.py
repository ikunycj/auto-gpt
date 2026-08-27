# -*- coding: utf-8 -*-
"""Launch a stock local Chrome process and attach through CDP.

Chrome is started as a normal process instead of through Playwright's launch
API. This keeps ``navigator.webdriver`` false while retaining the Playwright
page API used by the existing browser OAuth flow.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import urlopen

from config import codex as _cfg
from core.cloakbrowser_driver import CloakSeleniumDriver


logger = logging.getLogger(__name__)


@dataclass
class ChromeCDPOpenResult:
    profile_id: str = "chrome-cdp"
    raw: dict | None = None


def _find_chrome_executable(explicit: str = "") -> str:
    configured = str(explicit or getattr(_cfg, "CHROME_CDP_EXECUTABLE_PATH", "") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        raise RuntimeError(f"Chrome 可执行文件不存在或不可执行：{path}")

    system = platform.system().lower()
    candidates: list[str] = []
    if system == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
    elif system == "windows":
        for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
            if base:
                candidates.append(str(Path(base) / "Google/Chrome/Application/chrome.exe"))
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            found = shutil.which(name)
            if found:
                candidates.append(found)

    for candidate in candidates:
        path = Path(candidate)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    raise RuntimeError("未找到系统 Google Chrome，请在配置中填写 CHROME_CDP_EXECUTABLE_PATH")


def _reserve_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _proxy_server_arg(proxy: str | None) -> str:
    value = str(proxy or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = f"http://{value}"
    parsed = urlsplit(value.replace("socks5h://", "socks5://", 1))
    if parsed.username or parsed.password:
        raise RuntimeError("Chrome CDP 暂不支持带账号密码的代理，避免凭据暴露在进程参数中")
    if not parsed.hostname:
        raise RuntimeError("Chrome CDP 代理地址无效")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme or 'http'}://{host}{port}"


def _build_chrome_command(executable: str, port: int, profile_dir: str, proxy: str | None, background: bool) -> list[str]:
    command = [
        executable,
        f"--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--incognito",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    proxy_server = _proxy_server_arg(proxy)
    if proxy_server:
        command.append(f"--proxy-server={proxy_server}")
    if background:
        command.append("--start-minimized")
    command.append("about:blank")
    return command


def _wait_for_cdp(port: int, process: subprocess.Popen, timeout: float) -> dict:
    endpoint = f"http://127.0.0.1:{port}/json/version"
    deadline = time.monotonic() + max(1.0, float(timeout))
    last_error = ""
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(f"系统 Chrome 在 CDP 就绪前退出：exit={return_code}")
        try:
            with urlopen(endpoint, timeout=1.0) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
            if payload.get("webSocketDebuggerUrl"):
                return payload
            last_error = "响应缺少 webSocketDebuggerUrl"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.25)
    raise RuntimeError(f"等待系统 Chrome CDP 就绪超时：{last_error[:160]}")


class ChromeCDPSeleniumDriver(CloakSeleniumDriver):
    def __init__(
        self,
        browser: Any,
        context: Any,
        page: Any,
        *,
        playwright: Any,
        process: subprocess.Popen,
        profile_dir: str,
        keep_open: bool = False,
    ):
        super().__init__(browser=browser, context=context, page=page)
        self._playwright = playwright
        self._chrome_process = process
        self._chrome_profile_dir = profile_dir
        self._keep_open = bool(keep_open)
        self._closed = False
        self._registration_log_prefix = "[Chrome注册]"

    def quit(self) -> None:
        if self._closed:
            return
        self._closed = True
        process = self._chrome_process
        profile_dir = self._chrome_profile_dir

        if self._keep_open:
            try:
                self._playwright.stop()
            except Exception:
                pass
            logger.info("[ChromeCDP] 按配置保留浏览器进程 pid=%s profile=%s", getattr(process, "pid", "-"), profile_dir)
            return

        try:
            if self.context is not None:
                self.context.close()
        except Exception:
            pass
        try:
            self.browser.close()
        except Exception:
            pass
        try:
            self._playwright.stop()
        except Exception:
            pass

        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        finally:
            shutil.rmtree(profile_dir, ignore_errors=True)


def build_chrome_cdp_driver(
    proxy: str | None = None,
    *,
    background: bool = False,
    keep_open: bool | None = None,
) -> tuple[ChromeCDPSeleniumDriver, ChromeCDPOpenResult]:
    selected_proxy = proxy
    if selected_proxy is None:
        try:
            from config.proxy import pick_proxy

            selected_proxy = pick_proxy()
        except Exception:
            selected_proxy = None

    executable = _find_chrome_executable()
    port = _reserve_local_port()
    profile_dir = tempfile.mkdtemp(prefix="codex-chrome-cdp-")
    command = _build_chrome_command(executable, port, profile_dir, selected_proxy, background)
    logger.info(
        "[ChromeCDP] 启动系统 Chrome：executable=%s port=%s proxy=%s profile=临时目录 background=%s",
        executable,
        port,
        "已配置" if selected_proxy else "无",
        background,
    )

    process = None
    playwright = None
    browser = None
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        timeout = float(getattr(_cfg, "CHROME_CDP_START_TIMEOUT", 20) or 20)
        version_info = _wait_for_cdp(port, process, timeout)

        from playwright.sync_api import sync_playwright

        playwright = sync_playwright().start()
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        contexts = list(browser.contexts)
        context = contexts[0] if contexts else browser.new_context()
        pages = list(context.pages)
        page = pages[0] if pages else context.new_page()
        driver = ChromeCDPSeleniumDriver(
            browser,
            context,
            page,
            playwright=playwright,
            process=process,
            profile_dir=profile_dir,
            keep_open=(
                bool(getattr(_cfg, "CHROME_CDP_KEEP_BROWSER_OPEN", False))
                if keep_open is None
                else bool(keep_open)
            ),
        )
        driver.set_page_load_timeout(int(getattr(_cfg, "CHROME_CDP_PAGE_TIMEOUT", 90) or 90))

        environment = page.evaluate("""() => ({
          webdriver: navigator.webdriver,
          language: navigator.language,
          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
          userAgent: navigator.userAgent
        })""") or {}
        if environment.get("webdriver") is True:
            raise RuntimeError("系统 Chrome 暴露 navigator.webdriver=true，已停止授权以避免触发风控")
        logger.info(
            "[ChromeCDP] CDP 已连接：browser=%s webdriver=%s language=%s timezone=%s",
            str(version_info.get("Browser") or getattr(browser, "version", ""))[:80],
            environment.get("webdriver"),
            environment.get("language"),
            environment.get("timezone"),
        )
        return driver, ChromeCDPOpenResult(
            raw={
                "driver": "chrome_cdp",
                "proxy": str(selected_proxy or ""),
                "port": port,
                "pid": process.pid,
                "browser": str(version_info.get("Browser") or ""),
            }
        )
    except Exception:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        shutil.rmtree(profile_dir, ignore_errors=True)
        raise
