import time
import json
from playwright.sync_api import sync_playwright
import subprocess

proc = subprocess.Popen(["/home/jules/.pyenv/shims/python3", "app.py"], env={"PYTHONPATH": ".", "PATH": "/home/jules/.pyenv/shims:/usr/bin:/bin", "PORT": "8060"})
time.sleep(8)

try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # Listen to console messages and errors
        page.on("console", lambda msg: print(f"[Browser Console] {msg.type}: {msg.text}"))
        page.on("pageerror", lambda err: print(f"[Browser PageError] {err}"))

        page.goto("http://127.0.0.1:8060")
        page.wait_for_timeout(4000)

        print("=== Initial Load ===")
        # Check initial figures in page JS or Plotly graph elements
        map1_exists = page.locator("#district-map .js-plotly-plot").count()
        map2_exists = page.locator("#mortality-map .js-plotly-plot").count()
        print(f"district-map element count: {map1_exists}, mortality-map element count: {map2_exists}")

        # Select Ahmedabad Wards
        print("=== Selecting Ahmedabad Wards ===")
        page.click("#state-filter")
        page.wait_for_timeout(500)
        page.click("text=Municipal Wards: Ahmedabad (48 Wards)")
        page.wait_for_timeout(4000)

        page.screenshot(path="/tmp/full_page_ahmedabad.png", full_page=True)

        # Inspect Plotly graph data on client side
        map1_data = page.evaluate("() => document.getElementById('district-map').data")
        map2_data = page.evaluate("() => document.getElementById('mortality-map').data")

        print(f"Map 1 (thermal) client trace count: {len(map1_data) if map1_data else 'None'}")
        if map1_data:
            print("Map 1 trace 0 keys:", map1_data[0].keys() if isinstance(map1_data[0], dict) else type(map1_data[0]))
            print("Map 1 trace 0 z length:", len(map1_data[0].get('z', [])))
            print("Map 1 trace 0 locations length:", len(map1_data[0].get('locations', [])))

        print(f"Map 2 (mortality) client trace count: {len(map2_data) if map2_data else 'None'}")
        if map2_data:
            print("Map 2 trace 0 z length:", len(map2_data[0].get('z', [])))
            print("Map 2 trace 0 locations length:", len(map2_data[0].get('locations', [])))

        # Select Bengaluru Wards
        print("=== Selecting Bengaluru Wards ===")
        page.click("#state-filter")
        page.wait_for_timeout(500)
        page.click("text=Municipal Wards: Bengaluru (243 Wards)")
        page.wait_for_timeout(4000)

        map1_blr = page.evaluate("() => document.getElementById('district-map').data")
        map2_blr = page.evaluate("() => document.getElementById('mortality-map').data")
        print(f"Map 1 (thermal) Blr trace 0 z length: {len(map1_blr[0].get('z', [])) if map1_blr else 'None'}")
        print(f"Map 2 (mortality) Blr trace 0 z length: {len(map2_blr[0].get('z', [])) if map2_blr else 'None'}")

        browser.close()
finally:
    proc.terminate()
