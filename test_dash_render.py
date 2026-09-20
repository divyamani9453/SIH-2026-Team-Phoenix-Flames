import time
from playwright.sync_api import sync_playwright
import subprocess

# Start dash server in background
proc = subprocess.Popen(["/home/jules/.pyenv/shims/python3", "app.py"], env={"PYTHONPATH": ".", "PATH": "/home/jules/.pyenv/shims:/usr/bin:/bin", "PORT": "8058"})
time.sleep(4)

try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("http://127.0.0.1:8058")
        page.wait_for_timeout(3000)

        # Select Municipal Wards: Ahmedabad in state-filter
        page.click("#state-filter")
        page.wait_for_timeout(500)
        page.click("text=Municipal Wards: Ahmedabad (48 Wards)")
        page.wait_for_timeout(3000)

        # Take screenshot of section 1 (district-map) and section 2 (mortality-map)
        page.locator("#district-map").screenshot(path="/tmp/thermal_map_ahmedabad.png")
        page.locator("#mortality-map").screenshot(path="/tmp/mortality_map_ahmedabad.png")
        print("Screenshots taken successfully!")
        browser.close()
finally:
    proc.terminate()
