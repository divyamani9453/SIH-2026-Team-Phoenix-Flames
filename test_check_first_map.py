import time
from playwright.sync_api import sync_playwright
import subprocess

proc = subprocess.Popen(["/home/jules/.pyenv/shims/python3", "app.py"], env={"PYTHONPATH": ".", "PATH": "/home/jules/.pyenv/shims:/usr/bin:/bin", "PORT": "8062"})
time.sleep(8)

try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        page.on("console", lambda msg: print(f"[Console {msg.type}] {msg.text}"))
        page.on("pageerror", lambda err: print(f"[PageError] {err}"))

        page.goto("http://127.0.0.1:8062")
        page.wait_for_timeout(3000)

        print("--- Selecting Ahmedabad Wards ---")
        page.click("#state-filter")
        page.wait_for_timeout(300)
        page.click("text=Municipal Wards: Ahmedabad (48 Wards)")
        page.wait_for_timeout(3000)

        page.locator("#district-map").screenshot(path="/tmp/map1_ahmedabad.png")
        page.locator("#mortality-map").screenshot(path="/tmp/map2_ahmedabad.png")

        print("--- Selecting Bengaluru Wards ---")
        page.click("#state-filter")
        page.wait_for_timeout(300)
        page.click("text=Municipal Wards: Bengaluru (243 Wards)")
        page.wait_for_timeout(3000)

        page.locator("#district-map").screenshot(path="/tmp/map1_bengaluru.png")
        page.locator("#mortality-map").screenshot(path="/tmp/map2_bengaluru.png")

        browser.close()
finally:
    proc.terminate()
