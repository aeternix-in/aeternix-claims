"""Screenshot the main screens against a running server (python tools/screens.py http://127.0.0.1:8000 out/)."""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
out = Path(sys.argv[2] if len(sys.argv) > 2 else "screens")
out.mkdir(parents=True, exist_ok=True)
PW = "demo-password-123"


def login(page, email):
    page.goto(base + "/")
    page.fill("input[type=email]", email)
    page.fill("input[type=password]", PW)
    page.click("button.primary")
    page.wait_for_selector("nav.rail")


with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1360, "height": 900}, device_scale_factor=1)
    page = ctx.new_page()
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    login(page, "owner@demo.local")
    page.wait_for_selector(".work-item")
    page.screenshot(path=out / "01_worklist.png", full_page=True)
    page.click("text=Owner view")
    page.wait_for_selector(".owed .big")
    page.wait_for_timeout(600)
    page.screenshot(path=out / "02_owner.png", full_page=True)
    page.click("nav >> text=Cases")
    page.wait_for_selector("tbody tr")
    page.screenshot(path=out / "03_cases.png", full_page=True)
    for no, name in [("DEMO-2610-0005", "04_case_red"), ("DEMO-2610-0004", "05_case_green"),
                     ("DEMO-2610-0007", "06_case_unverified")]:
        page.fill("input[aria-label='Search cases']", no)
        page.keyboard.press("Enter")
        page.wait_for_timeout(500)
        page.click(f"text={no}")
        page.wait_for_selector(".arc")
        page.wait_for_timeout(400)
        page.screenshot(path=out / f"{name}.png", full_page=True)
        page.goto(base + "/cases")
        page.wait_for_selector("input[aria-label='Search cases']")
    page.click("nav >> text=Letters")
    page.wait_for_timeout(600)
    page.screenshot(path=out / "07_letters.png", full_page=True)
    ctx.clear_cookies()
    page.evaluate("localStorage.clear()")
    login(page, "accounts@demo.local")
    page.wait_for_selector("text=Confirm match")
    page.screenshot(path=out / "08_bank.png", full_page=True)
    page.evaluate("localStorage.clear()")
    login(page, "clinician@aeternix.local")
    page.wait_for_selector("text=Flags")
    page.wait_for_timeout(500)
    page.screenshot(path=out / "09_review.png", full_page=True)
    mob = b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2)
    mp = mob.new_page()
    login(mp, "desk@demo.local")
    mp.wait_for_selector(".work-item")
    mp.screenshot(path=out / "10_mobile_worklist.png", full_page=False)
    b.close()
    print("console errors:", errors or "none")
