"""FHIR R4 view of the ledger: Coverage, Claim, ClaimResponse, PaymentNotice, PaymentReconciliation.

This is the 'infrastructure underneath': when NHCX is switched on, submitting becomes a
translation of these resources into NHCX's profiles, not a rebuild. These resources follow
base FHIR R4; they are NOT validated against the NRCeS/NHCX profiles yet.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from .models import Allocation, BankTransaction, Case, Claim, Decision, Hospital, PaymentAdvice
from .money import to_ist


def _money(paise: int | None) -> dict:
    return {"value": round((paise or 0) / 100, 2), "currency": "INR"}


def _dt(d) -> str | None:
    if d is None:
        return None
    return to_ist(d).isoformat() if hasattr(d, "hour") else d.isoformat()


def bundle(db: Session, case: Case) -> dict:
    hospital = db.get(Hospital, case.hospital_id)
    org_ref = {"reference": f"Organization/{hospital.id}", "display": hospital.name}
    payer_ref = {"reference": f"Organization/payer-{case.payer.id}", "display": case.payer.name}
    patient_ref = {"reference": f"Patient/{case.id}", "display": case.case_no}  # no name in the reference
    entries: list[dict] = []
    coverage = {"resourceType": "Coverage", "id": f"cov-{case.id}", "status": "active",
                "subscriberId": case.beneficiary_ref, "beneficiary": patient_ref, "payor": [payer_ref],
                "class": [{"type": {"text": "scheme"}, "value": case.payer.scheme, "name": case.payer.name}]}
    entries.append(coverage)
    claims = db.query(Claim).filter(Claim.case_id == case.id).order_by(Claim.stage, Claim.version).all()
    for c in claims:
        items = []
        lines = case.bill_lines if c.stage == "final" else []
        for i, ln in enumerate(lines or [], 1):
            items.append({"sequence": i, "productOrService": {"coding": [{"code": ln.get("code"),
                                                                          "display": ln.get("description")}]},
                          "quantity": {"value": ln.get("qty", 1)}, "net": _money(ln.get("amount_paise"))})
        entries.append({
            "resourceType": "Claim", "id": f"claim-{c.id}", "status": "active",
            "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/claim-type", "code": "institutional"}]},
            "use": "preauthorization" if c.stage in {"preauth", "enhancement"} else "claim",
            "patient": patient_ref, "created": _dt(c.submitted_at or c.created_at), "provider": org_ref,
            "insurer": payer_ref, "priority": {"coding": [{"code": "stat" if case.emergency else "normal"}]},
            "identifier": [{"system": "urn:portal-ref", "value": c.portal_ref}] if c.portal_ref else [],
            "diagnosis": [{"sequence": 1, "diagnosisCodeableConcept": {"coding": [
                {"system": "http://hl7.org/fhir/sid/icd-10", "code": case.icd10}], "text": case.provisional_diagnosis}}]
            if case.icd10 else [],
            "insurance": [{"sequence": 1, "focal": True, "coverage": {"reference": f"Coverage/cov-{case.id}"}}],
            "billablePeriod": {"start": _dt(case.admitted_at), "end": _dt(case.discharged_at)},
            "item": items, "total": _money(c.amount_paise)})
    for d in db.query(Decision).filter(Decision.case_id == case.id).order_by(Decision.decided_at).all():
        entries.append({
            "resourceType": "ClaimResponse", "id": f"cr-{d.id}", "status": "active", "use": "claim",
            "patient": patient_ref, "created": _dt(d.decided_at), "insurer": payer_ref,
            "outcome": "error" if d.decision == "rejected" else ("partial" if d.decision == "part_approved" else "complete"),
            "disposition": d.rejection_reason or d.decision,
            "total": [{"category": {"coding": [{"code": "benefit"}]}, "amount": _money(d.approved_paise)}],
            "adjudication": [{"category": {"text": x.get("reason")}, "amount": _money(x.get("amount_paise"))}
                             for x in d.deductions or []]})
    for a in db.query(PaymentAdvice).filter(PaymentAdvice.hospital_id == case.hospital_id).all():
        lines = [l for l in a.lines or [] if l.get("case_id") == case.id]
        if not lines:
            continue
        entries.append({"resourceType": "PaymentNotice", "id": f"pn-{a.id}", "status": "active",
                        "created": a.paid_on.isoformat(), "payment": {"display": a.reference_no},
                        "paymentDate": a.paid_on.isoformat(), "recipient": org_ref,
                        "amount": _money(sum(int(l.get("amount_paise") or 0) for l in lines))})
    for al in db.query(Allocation).filter(Allocation.case_id == case.id, Allocation.status == "confirmed").all():
        t = db.get(BankTransaction, al.bank_txn_id)
        entries.append({"resourceType": "PaymentReconciliation", "id": f"pr-{al.id}", "status": "active",
                        "created": _dt(al.confirmed_at), "paymentDate": t.txn_date.isoformat(),
                        "paymentAmount": _money(al.amount_paise), "paymentIdentifier": {"value": t.reference_no or None},
                        "outcome": "complete", "disposition": f"Matched by {al.method}",
                        "detail": [{"type": {"text": "payment"}, "amount": _money(al.amount_paise)},
                                   {"type": {"text": "tax-deducted"}, "amount": _money(al.tax_paise)}]})
    return {"resourceType": "Bundle", "type": "collection", "id": f"case-{case.id}",
            "entry": [{"fullUrl": f"urn:uuid:{e['id']}", "resource": e} for e in entries]}
