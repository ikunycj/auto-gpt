import pytest

from core import chrome_cdp_driver as chrome_cdp


class FakeProcess:
    pid = 12345

    def __init__(self):
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class FakePage:
    url = "about:blank"

    def __init__(self, environment=None):
        self.environment = environment or {
            "webdriver": False,
            "language": "zh-CN",
            "timezone": "Asia/Shanghai",
            "userAgent": "Chrome/151",
        }
        self.navigation_timeout = None
        self.timeout = None

    def evaluate(self, _script):
        return self.environment

    def set_default_navigation_timeout(self, value):
        self.navigation_timeout = value

    def set_default_timeout(self, value):
        self.timeout = value


class FakeContext:
    def __init__(self, page):
        self.pages = [page]
        self.closed = False

    def close(self):
        self.closed = True


class FakeBrowser:
    version = "151.0.0.0"

    def __init__(self, context):
        self.contexts = [context]
        self.closed = False

    def close(self):
        self.closed = True


class FakePlaywright:
    def __init__(self, browser):
        self.browser = browser
        self.stopped = False

    def stop(self):
        self.stopped = True


def test_chrome_command_uses_normal_process_and_isolated_profile(tmp_path):
    command = chrome_cdp._build_chrome_command(
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        9333,
        str(tmp_path),
        "",
        True,
    )

    assert "--remote-debugging-address=127.0.0.1" in command
    assert "--remote-debugging-port=9333" in command
    assert f"--user-data-dir={tmp_path}" in command
    assert "--incognito" in command
    assert "--start-minimized" in command
    assert not any(arg in {"--enable-automation", "--disable-blink-features=AutomationControlled"} for arg in command)


def test_chrome_command_rejects_authenticated_proxy(tmp_path):
    with pytest.raises(RuntimeError, match="带账号密码"):
        chrome_cdp._build_chrome_command(
            "/path/to/chrome",
            9333,
            str(tmp_path),
            "http://user:password@proxy.example:8080",
            False,
        )


def test_driver_quit_closes_owned_browser_and_removes_profile(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    page = FakePage()
    context = FakeContext(page)
    browser = FakeBrowser(context)
    playwright = FakePlaywright(browser)
    process = FakeProcess()
    driver = chrome_cdp.ChromeCDPSeleniumDriver(
        browser,
        context,
        page,
        playwright=playwright,
        process=process,
        profile_dir=str(profile),
    )

    driver.quit()
    driver.quit()

    assert context.closed is True
    assert browser.closed is True
    assert playwright.stopped is True
    assert process.terminated is True
    assert not profile.exists()


def test_driver_keep_open_only_disconnects_playwright(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    page = FakePage()
    context = FakeContext(page)
    browser = FakeBrowser(context)
    playwright = FakePlaywright(browser)
    process = FakeProcess()
    driver = chrome_cdp.ChromeCDPSeleniumDriver(
        browser,
        context,
        page,
        playwright=playwright,
        process=process,
        profile_dir=str(profile),
        keep_open=True,
    )

    driver.quit()

    assert context.closed is False
    assert browser.closed is False
    assert playwright.stopped is True
    assert process.terminated is False
    assert profile.exists()


def test_find_chrome_executable_accepts_explicit_executable(tmp_path):
    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)

    assert chrome_cdp._find_chrome_executable(str(executable)) == str(executable)


def test_wait_for_cdp_reports_early_process_exit():
    process = FakeProcess()
    process.returncode = 17

    with pytest.raises(RuntimeError, match="exit=17"):
        chrome_cdp._wait_for_cdp(9333, process, timeout=1)
