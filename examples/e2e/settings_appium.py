"""Smoke test for an explicitly selected local Android emulator or iOS simulator."""

import os

import pytest
from appium import webdriver
from appium.options.android import UiAutomator2Options
from appium.options.ios import XCUITestOptions
from appium.webdriver.client_config import AppiumClientConfig


@pytest.fixture(scope="module")
def settings():
    udid = os.environ["DIFFPROWL_SIMULATOR"]
    if os.environ["DIFFPROWL_PLATFORM"] == "android":
        options = UiAutomator2Options()
        options.app_package = "com.android.settings"
        options.app_activity = ".Settings"
        options.app_wait_activity = "*"
        options.no_reset = True
        options.set_capability("appium:forceAppLaunch", True)
        options.set_capability("appium:uiautomator2ServerLaunchTimeout", 120000)
    elif os.environ["DIFFPROWL_PLATFORM"] == "ios":
        options = XCUITestOptions()
        options.bundle_id = "com.apple.Preferences"
        options.set_capability("appium:wdaLaunchTimeout", 180000)
    else:
        raise ValueError("choose android or ios")
    options.udid = udid
    endpoint = "http://127.0.0.1:4789"
    client = AppiumClientConfig(remote_server_addr=endpoint, timeout=360)
    driver = webdriver.Remote(endpoint, options=options, client_config=client)
    try:
        yield driver
    finally:
        driver.quit()


def test_settings_is_foreground(settings):
    app = "com.android.settings" if os.environ["DIFFPROWL_PLATFORM"] == "android" else "com.apple.Preferences"
    assert settings.query_app_state(app) == 4
