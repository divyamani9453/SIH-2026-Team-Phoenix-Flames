import time
from playwright.sync_api import sync_playwright
import subprocess

proc = subprocess.Popen(["/home/jules/.pyenv/shims/python3", "app.py"], env={"PYTHONPATH": ".", "PATH": "/home/jules/.pyenv/shims:/usr/bin:/bin", "PORT": "8059"})
time.sleep(8)

try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("http://127.0.0.1:8059")
        page.wait_for_timeout(3000)

        # Select Municipal Wards: Bengaluru in state-filter
        page.click("#state-filter")
        page.wait_for_timeout(500)
        page.click("text=Municipal Wards: Bengaluru (243 Wards)")
        page.wait_for_timeout(3000)

        page.locator("#district-map").screenshot(path="/tmp/thermal_map_bengaluru.png")
        print("Bengaluru screenshot taken successfully!")
        browser.close()
finally:
    proc.terminate()
