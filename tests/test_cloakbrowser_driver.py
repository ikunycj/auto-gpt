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
    def __init__(self, value=""):
        self.clicks = 0
        self.value = value

    def click(self, timeout=0):
        self.clicks += 1

    def evaluate(self, expression, arg=None, timeout=0):
        assert expression == "(el, propertyName) => el[propertyName]"
        if arg == "value":
            return self.value
        return None


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


def test_get_property_reads_live_dom_value_for_otp_diagnostics():
    locator = _Locator(value="12345")
    element = CloakElement(_Page(), locator=locator)

    assert element.get_property("value") == "12345"
    locator.value = "123456"
    assert element.get_property("value") == "123456"


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
