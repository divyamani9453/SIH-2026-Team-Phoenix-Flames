import time
from playwright.sync_api import sync_playwright
import subprocess

proc = subprocess.Popen(["/home/jules/.pyenv/shims/python3", "app.py"], env={"PYTHONPATH": ".", "PATH": "/home/jules/.pyenv/shims:/usr/bin:/bin", "PORT": "8061"})
time.sleep(8)

try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("http://127.0.0.1:8061")
        page.wait_for_timeout(3000)

        # Select Municipal Wards: Ahmedabad in state-filter
        page.click("#state-filter")
        page.wait_for_timeout(500)
        page.click("text=Municipal Wards: Ahmedabad (48 Wards)")
        page.wait_for_timeout(4000)

        # Evaluate JS on client
        d1 = page.evaluate("() => document.getElementById('district-map').data")
        d2 = page.evaluate("() => document.getElementById('mortality-map').data")

        print("=== Client Data for Ahmedabad ===")
        print("district-map data exists:", d1 is not None)
        if d1:
            print("district-map trace count:", len(d1))
            print("district-map trace 0 locations length:", len(d1[0].get('locations', [])))
            print("district-map trace 0 z length:", len(d1[0].get('z', [])))

        print("mortality-map data exists:", d2 is not None)
        if d2:
            print("mortality-map trace count:", len(d2))
            print("mortality-map trace 0 locations length:", len(d2[0].get('locations', [])))
            print("mortality-map trace 0 z length:", len(d2[0].get('z', [])))

        browser.close()
finally:
    proc.terminate()
