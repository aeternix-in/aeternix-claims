"""The whole arc over HTTP: case → documents → check → submit → query → approval → payment → bank → closed."""
import io
import zipfile

from fastapi.testclient import TestClient

from app.main import app
from app.models import Case, Hospital, Payer, User
from app.security import hash_password
from app.seed import make_documents

from .conftest import days_ago, utc_iso

client = TestClient(app)


def login(email):
    r = client.post("/api/auth/login", json={"email": email, "password": "pw-1234567890"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_full_arc_over_http(db, org):
    desk, acct, clin = login("desk@t.local"), login("accountant@t.local"), login("clin@t.local")
    cghs = org["payers"]["CGHS"]
    r = client.post("/api/cases", headers=desk, json={
        "payer_id": cghs.id, "beneficiary_name": "Kamla Rawat", "beneficiary_ref": "SYN-CG-1", "referral_no": "SYN-REF-1",
        "age": 66, "sex": "F", "admitted_at": utc_iso(days_ago(8)), "provisional_diagnosis": "Left renal calculus",
        "icd10": "n20.0", "planned_procedure": "Left PCNL", "package_code": "SAMPLE-URO-001", "surgical": True})
    assert r.status_code == 200, r.text
    case = r.json()
    cid = case["id"]
    assert case["case_no"].startswith("TST-") and case["icd10"] == "N20.0"
    assert any(c["kind"] == "portal_registration" for c in case["clocks"])

    def act(event, body=None, who=desk, ok=200):
        r = client.post(f"/api/cases/{cid}/actions/{event}", headers=who, json=body or {})
        assert r.status_code == ok, r.text
        return r.json()

    client.post(f"/api/cases/{cid}/portal-registered", headers=desk, json={"portal_case_id": "TMS-777"})
    act("submit_preauth", {"portal_ref": "TMS-777"})
    act("approve_preauth")
    case = act("discharge", {"discharged_at": utc_iso(days_ago(4))})
    assert case["state"] == "DISCHARGED"

    # documents: generate a synthetic pack and upload it the way the desk would
    c_obj = db.get(Case, cid)
    db.refresh(c_obj)
    docs = make_documents("Test Hospital", c_obj, {"proc": "pcnl", "defects": set()})
    files = [("files", (n, d, "application/octet-stream")) for n, d in docs]
    r = client.post(f"/api/cases/{cid}/documents", headers=desk, files=files, data={"source": "scanner"})
    assert r.status_code == 200, r.text
    case = r.json()["case"]
    assert {d["doc_type"] for d in case["documents"]} >= {"discharge_summary", "final_bill", "referral_letter",
                                                           "beneficiary_card", "investigation_report", "ot_notes"}
    case = client.post(f"/api/cases/{cid}/check", headers=desk).json()
    assert case["check"]["status"] == "red"
    assert any(r["rule"] == "fields_verified" for r in case["check"]["results"])
    act("mark_ready", ok=409)
    for d in case["documents"]:
        fields = {k: v["value"] for k, v in d["extracted"].items()}
        client.post(f"/api/documents/{d['id']}/verify", headers=desk, json={"fields": fields})
    case = client.post(f"/api/cases/{cid}/check", headers=desk).json()
    assert case["check"]["status"] == "green", [r for r in case["check"]["results"] if r["result"] != "green"]
    assert case["billed_paise"] == 47500_00  # taken from the verified bill
    case = act("mark_ready")

    pre = client.get(f"/api/cases/{cid}/prefill?portal=mock", headers=desk).json()
    assert pre["fields"]["beneficiary_ref"] == "SYN-CG-1" and pre["fields"]["claim_amount"] == "47500.00"
    assert pre["uploads"]["discharge_summary"] and pre["uploads"]["final_bill"]
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/api/cases/{cid}/pack", headers=desk).content))
    assert any(n.endswith("_fields.json") for n in z.namelist()) and len(z.namelist()) >= 8

    act("submit_claim", {"portal_ref": "TMS-777-C"})
    r = client.post(f"/api/cases/{cid}/queries", headers=desk,
                    json={"text": "Please upload the OT notes and CT KUB report."})
    case = r.json()
    q = case["queries"][0]
    assert case["state"] == "CLAIM_QUERY" and q["category"] == "missing_document" and q["draft_documents"]
    case = client.post(f"/api/queries/{q['id']}/reply", headers=desk, json={}).json()
    assert case["state"] == "CLAIM_SUBMITTED"

    # payer portal shows approval with a deduction; the extension reports it
    r = client.post("/api/tracker/snapshots", headers=desk, json={"portal": "nha_tms", "rows": [
        {"portal_ref": "TMS-777", "status_text": "Partially Approved", "approved_amount": "45,500.00",
         "deduction_amount": "2,000.00"}]})
    assert r.json()["changed"] == 1
    case = client.get(f"/api/cases/{cid}", headers=desk).json()
    assert case["state"] == "PART_APPROVED" and case["money"]["approved_paise"] == 45500_00
    act("payer_paid", {"payment_ref": "UTR998877665544", "paid_on": "2026-10-29"})

    stmt = ("Txn Date,Description,Ref No.,Debit,Credit\n"
            "30/10/2026,NEFT CR PFMS UTR998877665544,UTR998877665544,,45500.00\n").encode()
    r = client.post("/api/bank/statements", headers=acct, files={"file": ("s.csv", stmt, "text/csv")},
                    data={"account": "Main"})
    assert r.json()["results"] == {"exact_ref": 1}, r.text
    txns = client.get("/api/bank/transactions?status=proposed", headers=acct).json()
    alloc = txns[0]["allocations"][0]
    r = client.post(f"/api/bank/allocations/{alloc['id']}/confirm", headers=acct)
    assert r.status_code == 200, r.text
    case = client.get(f"/api/cases/{cid}", headers=desk).json()
    assert case["state"] == "CLOSED" and case["money"]["credited_paise"] == 45500_00

    fhir = client.get(f"/api/cases/{cid}/fhir", headers=desk).json()
    kinds = [e["resource"]["resourceType"] for e in fhir["entry"]]
    assert {"Coverage", "Claim", "ClaimResponse", "PaymentNotice", "PaymentReconciliation"} <= set(kinds)
    assert "Kamla" not in str(fhir)  # no name in the FHIR view

    m = client.get("/api/metrics/pilot", headers=desk).json()
    assert m["claims_submitted"] == 1 and m["share_within_window"] == 1.0
    assert client.get("/api/worklist", headers=desk).status_code == 200
    assert client.get("/api/review", headers=clin).status_code == 200


def test_roles_and_tenancy(db, org):
    other = Hospital(name="Other", code="OTH")
    db.add(other)
    db.flush()
    p = Payer(hospital_id=other.id, code="CGHS", name="CGHS", scheme="cghs", kind="government", portal="nha_tms")
    db.add(p)
    db.add(User(email="desk@other.local", name="Other desk", role="desk", password_hash=hash_password("pw-1234567890"),
                hospital_id=other.id))
    db.commit()
    mine = login("desk@t.local")
    theirs = login("desk@other.local")
    cid = client.post("/api/cases", headers=mine, json={"payer_id": org["payers"]["CGHS"].id,
                                                         "beneficiary_name": "X", "admitted_at": utc_iso(days_ago(1))}).json()["id"]
    assert client.get(f"/api/cases/{cid}", headers=theirs).status_code == 404
    # a payer from another hospital cannot be used
    r = client.post("/api/cases", headers=mine, json={"payer_id": p.id, "beneficiary_name": "Y"})
    assert r.status_code == 422
    # accountant cannot change a case; desk cannot touch the bank
    acct = login("accountant@t.local")
    assert client.post(f"/api/cases/{cid}/actions/start_treatment", headers=acct, json={}).status_code == 403
    assert client.post("/api/bank/match", headers=mine).status_code == 403
    # a hospital user cannot point at another hospital
    assert client.get("/api/cases", headers={**mine, "X-Hospital-Id": other.id}).status_code == 403
    assert client.get("/api/cases").status_code == 401
