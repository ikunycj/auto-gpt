from core.cloakbrowser_driver import CloakElement, CloakSeleniumDriver


class _Keyboard:
    def __init__(self):
        self.typed = []
        self.pressed = []

    def type(self, text, delay=0):
        self.typed.append((text, delay))

    def press(self, key):
        self.pressed.append(key)


class _Locator:
    def __init__(self):
        self.clicks = 0

    def click(self, timeout=0):
        self.clicks += 1


class _Page:
    def __init__(self):
        self.keyboard = _Keyboard()
        self.front_calls = 0

    def bring_to_front(self):
        self.front_calls += 1


def test_send_keys_appends_chunks_instead_of_replacing_input():
    page = _Page()
    locator = _Locator()
    element = CloakElement(page, locator=locator)

    element.send_keys("12")
    element.send_keys("34")
    element.send_keys("56")

    assert locator.clicks == 3
    assert page.keyboard.typed == [("12", 35), ("34", 35), ("56", 35)]


def test_send_keys_maps_selenium_backspace_to_keyboard_key():
    page = _Page()
    element = CloakElement(page, locator=_Locator())

    element.send_keys("\ue003")

    assert page.keyboard.pressed == ["Backspace"]
    assert page.keyboard.typed == []


def test_window_controls_minimize_and_restore_the_current_cloak_window():
    page = _Page()
    driver = CloakSeleniumDriver(browser=object(), context=None, page=page)
    calls = []

    def cdp(command, params=None):
        calls.append((command, params or {}))
        if command == "Browser.getWindowForTarget":
            return {"windowId": 42}
        return {}

    driver.execute_cdp_cmd = cdp
    driver.minimize_window()
    driver.focus_window()

    states = [params["bounds"]["windowState"] for command, params in calls if command == "Browser.setWindowBounds"]
    assert states == ["minimized", "normal"]
    assert page.front_calls == 1
