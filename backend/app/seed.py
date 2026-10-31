"""Synthetic demo data. Every patient, card number and document here is invented.

    python -m app.seed --reset                 # demo hospital, users, payers, 30 cases, a bank statement
    python -m app.seed --reset --no-documents  # faster: skip generating PDFs

Each generated document carries 'SYNTHETIC RECORD - NOT A REAL PATIENT'. Never load real
patient data into a demo.
"""
from __future__ import annotations

import argparse
import io
import random
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from . import queries, recon, scrubber, services
from .db import SessionLocal, engine, init_db, scope_session
from .documents import KEY_FIELDS
from .models import Base, BankTransaction, Case, Document, Hospital, Payer, User
from .money import now_utc, to_ist
from .security import hash_password
from .statemachine import apply

DEMO_PASSWORD = "demo-password-123"
MARK = "SYNTHETIC RECORD - NOT A REAL PATIENT"

NAMES = ["Ram Prakash Sharma", "Sunita Devi", "Mohd Irfan Khan", "Kamla Rawat", "Harish Chandra Joshi",
         "Baljeet Kaur", "Suresh Chandra Gupta", "Anita Verma", "Rakesh Kumar Yadav", "Shakuntala Bisht",
         "Om Prakash Meena", "Leela Nair", "Abdul Rehman", "Pushpa Rani", "Vinod Kumar Saini", "Kiran Bala",
         "Jagdish Prasad", "Meena Kumari", "Satish Chander Arora", "Usha Rani Malhotra", "Devendra Singh Negi",
         "Gurmeet Singh", "Asha Lata", "Naresh Pal", "Rajinder Kumar", "Savitri Devi", "Mahesh Chand Bansal",
         "Nirmala Joshi", "Ved Prakash", "Kusum Lata"]

PROCS = {
    "pcnl": ("Left renal calculus", "N20.0", "Left PCNL", "SAMPLE-URO-001", 45000, 4,
             "CT KUB: 18 mm calculus in left renal pelvis with mild hydronephrosis"),
    "ursl": ("Right ureteric calculus", "N20.1", "Right URSL with DJ stenting", "SAMPLE-URO-002", 32000, 2,
             "NCCT KUB: 9 mm calculus in right lower ureter with hydroureteronephrosis"),
    "turp": ("Benign prostatic hyperplasia", "N40", "TURP", "SAMPLE-URO-003", 38000, 3,
             "USG KUB with PVR: prostate 62 cc, PVR 180 ml; uroflowmetry Qmax 7 ml/s"),
    "dialysis": ("End stage renal disease", "N18.6", "Haemodialysis", "SAMPLE-NEP-001", 1800, 1,
                 "Renal function test: creatinine 8.4 mg/dl, urea 162 mg/dl"),
    "stent": ("Left hydronephrosis due to ureteric calculus", "N13.2", "Left DJ stent insertion", "SAMPLE-URO-004",
              12000, 1, "USG KUB: moderate left hydronephrosis"),
}


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc)


def _d(dt: datetime | date) -> str:
    if isinstance(dt, datetime):
        dt = to_ist(dt).date()
    return dt.strftime("%d/%m/%Y")


# ------------------------------------------------------------------ documents

def _pdf(lines: list[str]) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle("Synthetic document")
    y = 800
    for i, ln in enumerate(lines):
        c.setFont("Helvetica-Bold" if i < 2 else "Helvetica", 12 if i < 2 else 10.5)
        c.drawString(56, y, ln)
        y -= 20 if i < 2 else 16
    c.setFont("Helvetica-Oblique", 8)
    c.drawString(56, 40, MARK)
    c.save()
    return buf.getvalue()


def _png(lines: list[str]) -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("L", (900, 60 + 44 * len(lines)), 255)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 30)
    except OSError:
        font = ImageFont.load_default()
    for i, ln in enumerate(lines):
        draw.text((30, 30 + 44 * i), ln, fill=0, font=font)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def make_documents(hospital_name: str, case: Case, spec: dict) -> list[tuple[str, bytes]]:
    """spec: proc, card_label, defects (set), extra lines."""
    p = PROCS[spec["proc"]]
    diag, icd, proc, code, rate, los, inv = p
    defects = spec.get("defects", set())
    adm = case.admitted_at
    dis = case.discharged_at
    proc_day = to_ist(adm).date() + timedelta(days=1 if spec["proc"] != "dialysis" else 0)
    name = case.beneficiary_name
    card_label = spec.get("card_label", "CGHS Card No")
    ds_adm = (to_ist(adm).date() + timedelta(days=1)) if "ds_date_mismatch" in defects else adm
    icd_shown = spec.get("icd_override", icd)
    implant = "DJ stent 6Fr 26cm" if case.implant_used or "implant_unflagged" in defects else "Nil"
    signature = "Signature: ______________" if "unsigned" in defects else "Signature: Dr. A. Verma [signed]"
    docs = []
    docs.append((f"{case.case_no}_discharge_summary.pdf", _pdf([
        f"{hospital_name.upper()}", "DISCHARGE SUMMARY",
        f"Patient Name: {name}", f"Age/Sex: {case.age}/{case.sex}", f"{card_label}: {case.beneficiary_ref}",
        f"IP No: IP{random.randint(10000, 99999)}", f"Date of Admission: {_d(ds_adm)}",
        f"Date of Discharge: {_d(dis)}", "Consultant: Dr. A. Verma (Urology)", f"Diagnosis: {diag} ({icd_shown})",
        f"Procedure: {proc} on {_d(proc_day)}", f"Investigations: {inv}", f"Implant: {implant}",
        "Course in hospital: Uneventful. Pain controlled with oral analgesics.",
        "Condition at discharge: Stable, ambulatory, voiding well.",
        "Advice on discharge: Plenty of oral fluids. Review in OPD after 2 weeks.", signature])))
    lines = [(code, p[2] + " package", 1, rate)]
    if case.implant_used or "implant_unflagged" in defects:
        lines.append(("SAMPLE-IMP-001", "DJ stent (implant)", 1, 3500))
    if spec["proc"] in {"pcnl", "ursl", "stent"}:
        lines.append(("SAMPLE-RAD-001", "CT KUB", 1, 2500))
    if spec["proc"] == "turp":
        lines.append(("SAMPLE-RAD-002", "USG KUB", 1, 800))
    if spec["proc"] == "dialysis":
        lines.append(("SAMPLE-LAB-001", "Renal function test", 1, 450))
    if "over_rate" in defects:
        lines[0] = (code, lines[0][1], 1, rate + 4000)
    total = sum(l[3] for l in lines)
    bill_name = spec.get("bill_name", name)
    docs.append((f"{case.case_no}_final_bill.pdf", _pdf([
        f"{hospital_name.upper()}", "FINAL BILL", f"Bill No: DH/26/{random.randint(10000, 99999)}",
        f"Bill Date: {_d(dis)}", f"Patient Name: {bill_name}", f"{card_label}: {case.beneficiary_ref}",
        f"Date of Admission: {_d(adm)}", f"Date of Discharge: {_d(dis)}", "Code Description Qty Amount",
        *[f"{c} {d} {q} {a:.2f}" for c, d, q, a in lines], f"Total: {total:.2f}"])))
    if "no_investigation" not in defects:
        docs.append((f"{case.case_no}_investigation.pdf", _pdf([
            f"{hospital_name.upper()} - DEPARTMENT OF RADIOLOGY AND LABORATORY", "INVESTIGATION REPORT",
            f"Patient Name: {name}", f"Report Date: {_d(to_ist(adm).date() - timedelta(days=1))}",
            f"Investigation: {inv.split(':')[0]}", f"Findings: {inv.split(':', 1)[1].strip()}",
            f"Impression: {diag}."])))
    if spec.get("referral", True):
        docs.append((f"{case.case_no}_referral.png", _png([
            "CGHS WELLNESS CENTRE (SYNTHETIC)", "REFERRAL / PERMISSION LETTER", f"Referral No: {case.referral_no}",
            f"Patient Name: {name}", f"CGHS Card No: {case.beneficiary_ref}",
            f"Referred to: {hospital_name} for Urology", MARK])))
        docs.append((f"{case.case_no}_card.pdf", _pdf([
            "BENEFICIARY CARD (SYNTHETIC)", "CENTRAL GOVERNMENT HEALTH SCHEME",
            f"Beneficiary Name: {name}", f"{card_label}: {case.beneficiary_ref}", "Valid upto: 31/03/2027",
            "Category: Pensioner"])))
    if spec["proc"] in {"pcnl", "ursl", "turp", "stent"}:
        docs.append((f"{case.case_no}_ot_notes.pdf", _pdf([
            f"{hospital_name.upper()}", "OPERATION NOTES", f"Patient Name: {name}", f"Procedure: {proc}",
            f"Date of Procedure: {_d(proc_day)}", "Surgeon: Dr. A. Verma", "Anaesthesia: Spinal",
            "Operative findings: as per diagnosis; procedure completed without complication."])))
    if case.implant_used and "missing_implant_invoice" not in defects:
        docs.append((f"{case.case_no}_implant_invoice.pdf", _pdf([
            "SYNTHETIC MEDICAL DEVICES PVT LTD", "TAX INVOICE", f"Invoice No: INV-{random.randint(1000, 9999)}",
            "Description: DJ stent 6Fr 26cm", "Batch: B2611", "HSN: 9018", "Total: 3500.00"])))
        docs.append((f"{case.case_no}_implant_sticker.png", _png([
            "STERILE  DJ STENT 6FR 26CM", "REF DJ-626   LOT B2611", "USE BY 2028-06", "IMPLANT STICKER", MARK])))
    return docs


# ------------------------------------------------------------------ seeding

def reset(db: Session) -> None:
    db.close()
    Base.metadata.drop_all(engine)
    init_db()


def seed(db: Session, hospital_name: str = "Demo Hospital (synthetic)", code: str = "DEMO",
         with_documents: bool = True) -> dict:
    random.seed(7)
    scope_session(db, None, bypass=True)  # seeding writes for a hospital that does not exist yet
    now = now_utc()
    h = Hospital(name=hospital_name, code=code, empanelments=["cghs", "pmjay", "echs", "dgehs", "department"],
                 bank_accounts=[{"label": "Main", "bank": "Synthetic Bank", "last4": "0001"}],
                 digest_recipients=[{"channel": "console", "to": "owner"}])
    db.add(h)
    db.flush()
    users = {}
    for role, email, name in [("desk", "desk@demo.local", "Desk Executive"), ("owner", "owner@demo.local", "Hospital Owner"),
                              ("accountant", "accounts@demo.local", "Accountant"),
                              ("hospital_admin", "admin@demo.local", "Hospital Admin")]:
        u = User(email=email, name=name, role=role, password_hash=hash_password(DEMO_PASSWORD), hospital_id=h.id)
        db.add(u)
        users[role] = u
    for role, email, name in [("clinician", "clinician@aeternix.local", "Aeternix Clinician"),
                              ("aeternix_admin", "ops@aeternix.local", "Aeternix Ops")]:
        u = User(email=email, name=name, role=role, password_hash=hash_password(DEMO_PASSWORD), hospital_ids=[h.id])
        db.add(u)
        users[role] = u
    payers = {}
    for c, name, scheme, kind, portal, kws, tds in [
        ("CGHS", "CGHS (pensioners)", "cghs", "government", "nha_tms", ["CGHS", "PFMS"], 0),
        ("PMJAY", "AB PM-JAY (SHA Delhi)", "pmjay", "government", "nha_tms", ["PMJAY", "SHA DELHI", "AYUSHMAN"], 0),
        ("ECHS", "ECHS", "echs", "government", "utiitsl", ["ECHS"], 0),
        ("DGEHS", "DGEHS (Delhi Govt)", "dgehs", "government", "paper", ["DGEHS", "GNCTD"], 0),
        ("DTC", "Delhi Transport Corporation", "department", "department", "paper", ["DTC"], 0),
    ]:
        p = Payer(hospital_id=h.id, code=c, name=name, scheme=scheme, kind=kind, portal=portal,
                  narration_keywords=kws, expected_tds_bps=tds,
                  escalation_contacts=[{"role": "Nodal officer", "name": f"{c} nodal officer (synthetic)",
                                        "email": f"nodal.{c.lower()}@example.org"}])
        db.add(p)
        payers[c] = p
    db.flush()
    desk, acct = users["desk"], users["accountant"]
    names = iter(NAMES)

    def new_case(payer: str, proc: str, days_ago_admit: int, los: int | None, **kw) -> Case:
        p = PROCS[proc]
        admitted = now - timedelta(days=days_ago_admit, hours=random.randint(0, 6))
        data = {"payer_id": payers[payer].id, "beneficiary_name": next(names),
                "beneficiary_ref": f"SYN-{payer[:2]}-{random.randint(100000, 999999)}",
                "referral_no": f"SYN-REF-{random.randint(10000, 99999)}", "age": random.randint(45, 82),
                "sex": random.choice(["M", "F"]), "ward": "General", "treating_doctor": "Dr. A. Verma",
                "provisional_diagnosis": p[0], "icd10": p[1], "planned_procedure": p[2], "package_code": p[3],
                "surgical": proc != "dialysis", "implant_used": proc in {"ursl", "stent"},
                "admitted_at": _utc(admitted).isoformat()}
        data.update(kw)
        c = services.create_case(db, h.id, data, desk)
        c.billed_paise = (p[4] + (3500 if c.implant_used else 0) + (2500 if proc in {"pcnl", "ursl", "stent"} else 0)
                          + (800 if proc == "turp" else 0) + (450 if proc == "dialysis" else 0)) * 100
        if los is not None:
            pack = c.payer.scheme
            if pack in {"cghs"}:
                c.portal_registered_at = admitted + timedelta(hours=5)
            ev_at = admitted + timedelta(hours=8)
            needs = c.payer.scheme in {"cghs", "pmjay"}
            if needs:
                apply(db, c, "submit_preauth", desk, {"portal_ref": f"TMS{random.randint(1000000, 9999999)}",
                                                     "at": _utc(ev_at)})
                apply(db, c, "approve_preauth", desk, {"at": _utc(ev_at + timedelta(hours=3))})
            else:
                apply(db, c, "start_treatment", desk, {"at": _utc(ev_at)})
            if c.payer.scheme == "pmjay":
                c.counter_checks = {"biometric_registration": admitted.isoformat(), "biometric_preauth": admitted.isoformat()}
            apply(db, c, "discharge", desk, {"discharged_at": _utc(admitted + timedelta(days=los)).isoformat(),
                                            "at": _utc(admitted + timedelta(days=los))})
        return c

    def submit(c: Case, days_after_discharge: int) -> datetime:
        at = c.discharged_at + timedelta(days=days_after_discharge)
        c.state = "READY_TO_SUBMIT"  # history: these were checked before this system existed
        apply(db, c, "submit_claim", desk, {"portal_ref": c.portal_case_id or f"CLM{random.randint(100000, 999999)}",
                                           "at": _utc(at)}, system=True)
        return at

    created: list[Case] = []
    # --- History: claims fully paid and matched (scorecard, pilot metrics)
    for payer, proc, ago, los, sub, appr_after, paid_after in [
        ("CGHS", "pcnl", 120, 4, 6, 25, 40), ("CGHS", "turp", 110, 3, 9, 30, 52), ("PMJAY", "ursl", 95, 2, 4, 12, 20),
        ("ECHS", "stent", 90, 1, 5, 35, 60), ("CGHS", "dialysis", 70, 1, 3, 20, 35)]:
        c = new_case(payer, proc, ago, los)
        at = submit(c, sub)
        apply(db, c, "approve", desk, {"approved_amount": c.billed_paise / 100, "at": _utc(at + timedelta(days=appr_after))})
        ref = f"UTR{random.randint(10**11, 10**12 - 1)}"
        paid = at + timedelta(days=paid_after)
        apply(db, c, "payer_paid", desk, {"payment_ref": ref, "paid_on": to_ist(paid).date().isoformat(), "at": _utc(paid)})
        t = BankTransaction(hospital_id=h.id, account="Main", txn_date=to_ist(paid).date() + timedelta(days=1),
                            amount_paise=c.billed_paise, narration=f"NEFT CR {payer} {ref}", reference_no=ref,
                            import_batch="history")
        db.add(t)
        db.flush()
        recon.allocate_manual(db, t, [{"case_id": c.id, "amount": c.billed_paise / 100}], acct)
        created.append(c)

    # --- Approved but unpaid (bucket E), various ages; one PM-JAY overdue for interest
    approved_cghs = []
    for payer, proc, ago, los, sub, appr_after in [
        ("CGHS", "pcnl", 150, 4, 7, 30), ("CGHS", "ursl", 100, 2, 5, 25), ("CGHS", "turp", 75, 3, 6, 20),
        ("CGHS", "stent", 60, 1, 4, 18), ("PMJAY", "pcnl", 80, 4, 3, 10), ("ECHS", "turp", 130, 3, 6, 40),
        ("DGEHS", "dialysis", 140, 1, 10, 45)]:
        c = new_case(payer, proc, ago, los)
        at = submit(c, sub)
        apply(db, c, "approve", desk, {"approved_amount": c.billed_paise / 100, "at": _utc(at + timedelta(days=appr_after))})
        if payer == "CGHS" and proc in {"ursl", "turp", "stent"}:
            approved_cghs.append(c)
        created.append(c)
    pmjay_match = None
    # --- Part-approved (D + E)
    for payer, proc, ago in [("CGHS", "pcnl", 85), ("PMJAY", "turp", 50)]:
        c = new_case(payer, proc, ago, 4 if proc == "pcnl" else 3)
        at = submit(c, 5)
        ded = 4000 if proc == "pcnl" else 2500
        apply(db, c, "part_approve", desk, {"approved_amount": c.billed_paise / 100 - ded,
                                            "deductions": [{"reason": "Room rent above entitlement", "amount": ded}],
                                            "at": _utc(at + timedelta(days=15))})
        if payer == "PMJAY":
            pmjay_match = c
        created.append(c)
    # --- Paid by payer, not yet credited (bucket F) — the statement below will match these
    paid_refs = []
    for payer, proc, ago in [("CGHS", "dialysis", 45), ("ECHS", "ursl", 70)]:
        c = new_case(payer, proc, ago, 1 if proc == "dialysis" else 2)
        at = submit(c, 4)
        apply(db, c, "approve", desk, {"approved_amount": c.billed_paise / 100, "at": _utc(at + timedelta(days=18))})
        ref = f"UTR{random.randint(10**11, 10**12 - 1)}"
        paid = now - timedelta(days=4)
        apply(db, c, "payer_paid", desk, {"payment_ref": ref, "paid_on": to_ist(paid).date().isoformat(), "at": _utc(paid)})
        paid_refs.append((c, ref, paid))
        created.append(c)
    # --- With payer, awaiting decision (S); one stalled
    for payer, proc, ago, sub in [("CGHS", "turp", 30, 4), ("ECHS", "pcnl", 70, 5), ("DTC", "dialysis", 25, 6)]:
        c = new_case(payer, proc, ago, 3 if proc == "turp" else (4 if proc == "pcnl" else 1))
        submit(c, sub)
        created.append(c)
    # --- In query (C)
    q_texts = [("CGHS", "pcnl", 40, "Please submit the implant invoice and the implant sticker. Also provide the CT KUB report."),
               ("PMJAY", "ursl", 22, "Justify the length of stay and the indication for DJ stenting with clinical notes.")]
    for payer, proc, ago, text in q_texts:
        c = new_case(payer, proc, ago, 3)
        at = submit(c, 4)
        queries.raise_query(db, c, text, desk, raised_at=at + timedelta(days=9), source="portal")
        created.append(c)
    # --- Rejected (D)
    c = new_case("DGEHS", "turp", 160, 3)
    at = submit(c, 20)
    apply(db, c, "reject", desk, {"reason": "Bill submitted beyond the permitted period", "at": _utc(at + timedelta(days=40))})
    created.append(c)
    # --- Late, needs condonation (B)
    c = new_case("CGHS", "ursl", 40, 2)
    apply(db, c, "window_passed", "system", {}, system=True)
    created.append(c)

    # --- Discharged, claim packs for the demo (A) -----------------------------------------------
    demo = []
    specs = [
        ("CGHS", "pcnl", 6, 4, {"proc": "pcnl", "defects": set()}, {"implant_used": False}, True),            # clean
        ("CGHS", "ursl", 5, 2, {"proc": "ursl", "defects": {"ds_date_mismatch", "over_rate", "unsigned",
                                                          "missing_implant_invoice"}}, {}, True),          # many reds
        ("CGHS", "turp", 9, 3, {"proc": "turp", "defects": set(), "bill_name": "Suresh Chand",
                                "icd_override": "N20.0"}, {"icd10": "N20.0"}, True),                       # name + clinical
        ("PMJAY", "dialysis", 3, 1, {"proc": "dialysis", "card_label": "PM-JAY ID", "referral": False}, {}, False),  # unverified
        ("ECHS", "stent", 5, 1, {"proc": "stent", "card_label": "ECHS Card No"}, {}, True),                # window closing
    ]
    typed_by_hand: list[str] = []
    for payer, proc, ago, los, spec, overrides, verify in specs:
        if payer == "CGHS" and proc == "turp":
            names_list = "Suresh Chandra Gupta"
        else:
            names_list = None
        kw = dict(overrides)
        if names_list:
            kw["beneficiary_name"] = names_list
        c = new_case(payer, proc, ago, los, **kw)
        if with_documents:
            for fname, data in make_documents(hospital_name, c, spec):
                d = services.ingest_document(db, c, fname, data, desk, source="scanner")
                if verify:
                    fields = {k: v.get("value") for k, v in (d.extracted or {}).items()}
                    if d.ocr_method == "unsupported":
                        # No OCR on this machine: the desk types the photo's fields in by hand, as it would.
                        typed = {"patient_name": c.beneficiary_name, "referral_no": c.referral_no,
                                 "beneficiary_ref": c.beneficiary_ref}
                        fields.update({k: typed[k] for k in KEY_FIELDS.get(d.doc_type, []) if k in typed and not fields.get(k)})
                        typed_by_hand.append(d.filename)
                    services.verify_document(db, d, fields, desk)
            scrubber.run(db, c, desk)
        demo.append(c)
        created.append(c)

    # --- In treatment (not owed yet)
    for payer, proc, ago in [("CGHS", "pcnl", 1), ("PMJAY", "dialysis", 0)]:
        c = new_case(payer, proc, ago, None)
        created.append(c)

    # --- This week's bank statement, imported and matched (accountant confirms in the app)
    lines = ["Txn Date,Value Date,Description,Ref No./Cheque No.,Debit,Credit,Balance"]
    bal = 1250000.00
    for c, ref, paid in paid_refs:
        amt = c.billed_paise / 100
        bal += amt
        lines.append(f"{_d(paid + timedelta(days=1))},{_d(paid + timedelta(days=1))},NEFT CR-{c.payer.code}-{ref},{ref},,{amt:.2f},{bal:.2f}")
    split_total = sum(x.billed_paise for x in approved_cghs) / 100
    bal += split_total
    lines.append(f"{_d(now - timedelta(days=2))},{_d(now - timedelta(days=2))},NEFT CR PFMS CGHS HCO BILLS,PFMS{random.randint(10**9, 10**10 - 1)},,{split_total:.2f},{bal:.2f}")
    if pmjay_match:
        amt = recon.expected_net(db, pmjay_match) / 100
        bal += amt
        lines.append(f"{_d(now - timedelta(days=1))},{_d(now - timedelta(days=1))},RTGS CR SHA DELHI PMJAY CLAIMS,SHA{random.randint(10**8, 10**9 - 1)},,{amt:.2f},{bal:.2f}")
    bal += 18000
    lines.append(f"{_d(now - timedelta(days=1))},{_d(now - timedelta(days=1))},CASH DEPOSIT BY SELF,,,18000.00,{bal:.2f}")
    lines.append(f"{_d(now - timedelta(days=1))},{_d(now - timedelta(days=1))},NEFT DR VENDOR PAYMENT,,45000.00,,{bal - 45000:.2f}")
    statement = "\n".join(lines) + "\n"
    res = recon.import_statement(db, h.id, "Main", "synthetic_statement.csv", statement.encode(), acct)
    # What the worker's first sweep would draft: letters for stalled, late, short-paid and rejected claims.
    from . import escalation
    letters = escalation.sweep(db, h.id, now)
    db.commit()
    return {"hospital_id": h.id, "cases": len(created), "demo_cases": [c.case_no for c in demo],
            "bank": res, "letters": len(letters), "typed_by_hand": typed_by_hand, "password": DEMO_PASSWORD, "statement_csv": statement,
            "users": {r: u.email for r, u in users.items()}}


def main() -> None:  # pragma: no cover
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="drop and recreate every table first")
    ap.add_argument("--hospital-name", default="Demo Hospital (synthetic)")
    ap.add_argument("--code", default="DEMO")
    ap.add_argument("--no-documents", action="store_true")
    a = ap.parse_args()
    db = SessionLocal()
    if a.reset:
        reset(db)
        db = SessionLocal()
    else:
        init_db()
    out = seed(db, a.hospital_name, a.code, not a.no_documents)
    print(f"Seeded {out['cases']} synthetic cases. Demo claim packs: {', '.join(out['demo_cases'])}")
    print(f"Bank statement: {out['bank']}")
    if out["typed_by_hand"]:
        print(f"Note: tesseract (OCR) was not found, so {len(out['typed_by_hand'])} photographed documents were "
              "confirmed by hand instead of read. Install tesseract to see OCR read them.")
    print("Sign in with any of:", ", ".join(out["users"].values()), f"  password: {out['password']}")


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = ["seed", "reset", "make_documents", "Document"]
