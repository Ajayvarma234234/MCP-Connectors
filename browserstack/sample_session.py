"""OPTIONAL: run one tiny test on BrowserStack so a brand-new trial account has a build and session to inspect.

    pip install selenium
    python sample_session.py

Opens https://example.com on Windows 11 / Chrome in BrowserStack's cloud, checks the page title, and marks the
session passed or failed. It uses ~1 minute of your Automate trial time. Credentials come from
BROWSERSTACK_USERNAME / BROWSERSTACK_ACCESS_KEY (or you are asked). This is the only file that needs selenium;
the connector itself does not.
"""
import getpass
import json
import os

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

user = os.getenv("BROWSERSTACK_USERNAME") or input("BrowserStack username: ").strip()
key = os.getenv("BROWSERSTACK_ACCESS_KEY") or getpass.getpass("BrowserStack access key (hidden): ").strip()

options = Options()
options.set_capability("bstack:options", {
    "os": "Windows", "osVersion": "11",
    "projectName": "MCP connector demo", "buildName": "connector-demo-1",
    "sessionName": "example.com title check",
    "userName": user, "accessKey": key,
})

driver = webdriver.Remote("https://hub.browserstack.com/wd/hub", options=options)
try:
    driver.get("https://example.com")
    ok = "Example Domain" in driver.title
    status, reason = ("passed", "Title matched") if ok else ("failed", f"Unexpected title: {driver.title}")
    driver.execute_script("browserstack_executor: " + json.dumps(
        {"action": "setSessionStatus", "arguments": {"status": status, "reason": reason}}))
    print(f"Session finished: {status} ({reason})")
finally:
    driver.quit()
