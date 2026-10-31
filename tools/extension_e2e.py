"""End-to-end check of the Chrome extension against the mock portal, with the real extension loaded.

Needs a freshly seeded server in DEMO_MODE:
    python tools/extension_e2e.py http://127.0.0.1:8000 out/
It checks: sign-in from the options page, reading the claim list into Aeternix, a payer query and a payer
payment flowing into case states, a red case being refused, a green case being filled with documents
attached, the extension NOT submitting, and the confirmation reference being recorded.
"""
import json
import sys
import tempfile
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "screens")
OUT.mkdir(parents=True, exist_ok=True)
EXT = str(Path(__file__).resolve().parent.parent / "extension")
PW = "demo-password-123"


def call(method, path, token=None, body=None):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


token = call("POST", "/api/auth/login", body={"email": "desk@demo.local", "password": PW})["token"]
by_no = {c["case_no"]: c for c in call("GET", "/api/cases?limit=500", token)}
green, red = by_no["DEMO-2610-0004"], by_no["DEMO-2610-0005"]
if green["state"] == "DISCHARGED":
    call("POST", f"/api/cases/{green['id']}/actions/mark_ready", token, {})
submitted = [c for c in by_no.values() if c["state"] == "CLAIM_SUBMITTED"][0]
approved = [c for c in by_no.values() if c["state"] == "APPROVED"][0]


def ref_of(case):
    d = call("GET", f"/api/cases/{case['id']}", token)
    claims = [x for x in d["claims"] if x["stage"] == "final" and x["portal_ref"]]
    return claims[-1]["portal_ref"] if claims else d["portal_case_id"]


checks = []


def ok(name, cond, detail=""):
    checks.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail else ""))


with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        tempfile.mkdtemp(), channel="chromium", headless=True, viewport={"width": 1280, "height": 900},
        args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}"])
    sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker")
    ext_id = sw.url.split("/")[2]
    errors = []

    # 1. Sign in from the options page.
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"chrome-extension://{ext_id}/options.html")
    page.fill("#backend", BASE)
    page.fill("#email", "desk@demo.local")
    page.fill("#password", PW)
    page.click("#signinBtn")
    expect(page.locator("#account")).to_be_visible()
    expect(page.locator("#portalRows")).to_contain_text("Mock payer portal")
    page.screenshot(path=OUT / "ext_01_options.png", full_page=True)
    rows = page.locator("#portalRows").inner_text()
    ok("options: mock portal ready and allowed", "Ready" in rows and "Allowed" in rows)
    ok("options: real portals shown as not captured", "Not captured yet" in rows)

    # 2. The app's own sign-in, so the simulator can load this hospital's claims.
    page.goto(BASE + "/")
    page.fill("input[type=email]", "desk@demo.local")
    page.fill("input[type=password]", PW)
    page.click("button.primary")
    page.wait_for_selector("nav.rail")

    # 3. Claim list: load the payer-side fixture, then reload so the extension reads it.
    page.goto(BASE + "/mock-portal/claims.html")
    page.click("#simLoad")
    expect(page.locator("#simErr")).to_contain_text("Loaded")
    page.reload()
    panel = page.locator("#aeternix-desk")
    ptext = lambda: panel.locator(".box").inner_text()  # the panel lives in a shadow root
    expect(panel).to_contain_text("since last read", timeout=15000)
    first = [ln for ln in ptext().splitlines() if "since last read" in ln][0]
    ok("claim list read into Aeternix", "not linked to a case" in first, first)

    # 4. Play the payer: raise a query on a submitted claim, pay an approved one.
    q_ref, p_ref = ref_of(submitted), ref_of(approved)
    page.select_option("#simRef", q_ref)
    page.select_option("#simStatus", "Query Raised")
    page.fill("#simQuery", "Please send the implant invoice and the pre-operative CT report.")
    page.click("#simApply")
    page.select_option("#simRef", p_ref)
    page.select_option("#simStatus", "Payment Done")
    page.fill("#simUtr", "SBINR52026103100042")
    page.click("#simApply")
    page.reload()
    expect(panel).to_contain_text("changed since last read", timeout=15000)
    page.screenshot(path=OUT / "ext_02_claim_list.png", full_page=True)
    s1 = call("GET", f"/api/cases/{submitted['id']}", token)
    s2 = call("GET", f"/api/cases/{approved['id']}", token)
    ok("payer query became a case query", s1["state"] == "CLAIM_QUERY" and s1["queries"]
       and "implant invoice" in s1["queries"][0]["text"], s1["state"])
    ok("payer payment became 'Paid by payer'", s2["state"] == "PAID_BY_PAYER", s2["state"])

    # 5. Claim form: a red case is refused; the green one is filled; nothing is submitted.
    before = page.evaluate("JSON.parse(localStorage.getItem('mockPortal.claims.v1') || '[]').length")
    page.goto(BASE + "/mock-portal/claim-form.html")
    search = panel.locator("input[type=search]")
    search.fill(red["case_no"])
    panel.locator("button.pick", has_text=red["case_no"]).click()
    expect(panel).to_contain_text("not green", timeout=10000)
    page.screenshot(path=OUT / "ext_03_form_refused.png")
    ok("red case refused", "Fill from Aeternix" not in ptext())
    panel.get_by_text("Pick another case").click()
    panel.locator("input[type=search]").fill(green["case_no"])
    panel.locator("button.pick", has_text=green["case_no"]).click()
    panel.get_by_text("Fill from Aeternix").click()
    expect(panel).to_contain_text("Filled", timeout=15000)
    page.wait_for_timeout(600)
    page.screenshot(path=OUT / "ext_04_form_filled.png", full_page=True)
    vals = page.evaluate("""() => ({
        name: document.querySelector('#patientName').value, ben: document.querySelector('#beneficiaryId').value,
        adm: document.querySelector('#admissionDate').value, amt: document.querySelector('#claimAmount').value,
        dis: document.querySelector('#fileDischarge').files.length, bill: document.querySelector('#fileBill').files.length,
        other: document.querySelector('#fileOther').files.length,
        size: document.querySelector('#fileDischarge').files[0] && document.querySelector('#fileDischarge').files[0].size })""")
    ok("fields filled", vals["name"] == green["beneficiary_name"] and vals["ben"] == green["beneficiary_ref"]
       and vals["adm"] and float(vals["amt"]) == green["billed_paise"] / 100, json.dumps(vals))
    ok("documents attached", vals["dis"] == 1 and vals["bill"] == 1 and vals["other"] >= 1 and vals["size"] > 500)
    after = page.evaluate("JSON.parse(localStorage.getItem('mockPortal.claims.v1') || '[]').length")
    ok("extension did not submit", "claim-form" in page.url and after == before, page.url)

    # 6. The person presses Submit; the extension reads the reference and records it on the case.
    page.click("#submitClaim")
    page.wait_for_url("**/submitted.html*")
    ref = page.locator("#claimRef").inner_text().strip()
    expect(panel).to_contain_text(green["case_no"], timeout=10000)
    page.screenshot(path=OUT / "ext_05_confirmation.png")
    panel.get_by_text(f"Record it as submitted for {green['case_no']}").click()
    expect(panel).to_contain_text("Recorded", timeout=10000)
    page.screenshot(path=OUT / "ext_06_recorded.png")
    g = call("GET", f"/api/cases/{green['id']}", token)
    ok("submission recorded with the portal reference", g["state"] == "CLAIM_SUBMITTED"
       and any(c["portal_ref"] == ref for c in g["claims"]), f"{g['state']} {ref}")

    # 7. Popup on a page that is not a portal.
    pop = ctx.new_page()
    pop.set_viewport_size({"width": 352, "height": 260})
    pop.goto(f"chrome-extension://{ext_id}/popup.html")
    expect(pop.locator("#page")).not_to_be_empty()
    pop.screenshot(path=OUT / "ext_07_popup.png")
    ok("popup renders", "Desk Executive" in pop.locator("#who").inner_text())
    ok("no page errors", not errors, "; ".join(errors))
    ctx.close()

failed = [c for c in checks if not c[1]]
print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
