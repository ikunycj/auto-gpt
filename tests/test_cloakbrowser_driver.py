from core.cloakbrowser_driver import CloakElement, CloakSeleniumDriver


class _Keyboard:
    def __init__(self, page):
        self.page = page
        self.typed = []
        self.pressed = []

    def type(self, text, delay=0):
        self.typed.append((text, delay))
        active = self.page.active_locator
        if active is not None:
            if active.selection_all:
                active.value = text
                active.selection_all = False
            else:
                active.value += text

    def press(self, key):
        self.pressed.append(key)
        active = self.page.active_locator
        if active is None:
            return
        if key in ("Meta+A", "Control+A"):
            active.selection_all = True
        elif key == "Backspace":
            if active.selection_all:
                active.value = ""
                active.selection_all = False
            else:
                active.value = active.value[:-1]


class _Locator:
    def __init__(self, page=None):
        self.page = page
        self.clicks = 0
        self.focuses = 0
        self.selection_all = False
        self.value = ""

    def click(self, timeout=0):
        self.clicks += 1
        if self.page is not None:
            self.page.active_locator = self
        self.selection_all = False

    def focus(self, timeout=0):
        self.focuses += 1
        if self.page is not None:
            self.page.active_locator = self


class _Page:
    def __init__(self):
        self.active_locator = None
        self.keyboard = _Keyboard(self)
        self.front_calls = 0

    def bring_to_front(self):
        self.front_calls += 1


def test_send_keys_appends_chunks_instead_of_replacing_input():
    page = _Page()
    locator = _Locator(page)
    element = CloakElement(page, locator=locator)

    element.send_keys("12")
    element.send_keys("34")
    element.send_keys("56")

    assert locator.clicks == 0
    assert locator.focuses == 3
    assert locator.value == "123456"
    assert page.keyboard.typed == [("12", 35), ("34", 35), ("56", 35)]


def test_send_keys_maps_selenium_backspace_to_keyboard_key():
    page = _Page()
    element = CloakElement(page, locator=_Locator(page))

    element.send_keys("\ue003")

    assert page.keyboard.pressed == ["Backspace"]
    assert page.keyboard.typed == []


def test_send_keys_preserves_select_all_until_clear_and_types_long_email_exactly():
    page = _Page()
    locator = _Locator(page)
    locator.value = "stale@example.com"
    element = CloakElement(page, locator=locator)

    element.send_keys("\ue03d", "a")
    element.send_keys("\ue003")
    for chunk in ("qi5612", "b2f310", "yeiqyy", "@outlook.com"):
        element.send_keys(chunk)

    assert locator.clicks == 0
    assert locator.value == "qi5612b2f310yeiqyy@outlook.com"


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
