"""Load a hospital's existing outstanding-claims list (any spreadsheet) as the week-one baseline.

Columns are matched by name, loosely. Each row becomes a case placed directly in the
state its status column describes, marked ``imported`` so pilot metrics can exclude it.
"""
from __future__ import annotations

import csv
import io
import re

from sqlalchemy.orm import Session

from . import audit, clocks
from .adapters import get_adapter
from .models import Case, Claim, Decision, Payer, Query
from .money import parse_date, parse_datetime_ist, to_paise
from .rulepacks import get_pack

COLS = {
    "ref": ["claim no", "claim number", "claim id", "case id", "tms id", "reference", "ref no", "claim ref", "ip no"],
    "patient": ["patient", "patient name", "beneficiary", "beneficiary name", "name"],
    "card": ["card no", "beneficiary id", "card", "member id", "cghs id"],
    "payer": ["payer", "scheme", "insurer", "tpa", "panel"],
    "admission": ["admission date", "doa", "date of admission", "admitted on"],
    "discharge": ["discharge date", "dod", "date of discharge", "discharged on"],
    "claimed": ["claimed", "claim amount", "billed", "bill amount", "amount claimed", "amount"],
    "approved": ["approved", "approved amount", "sanctioned", "passed amount"],
    "status": ["status", "claim status", "current status", "stage"],
    "submitted": ["submitted on", "submission date", "date of submission", "submitted"],
    "query": ["query", "query text", "remarks", "deficiency"],
}


def _read_rows(filename: str, data: bytes) -> list[dict]:
    if filename.lower().endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        rows = [[("" if v is None else (v.strftime("%d/%m/%Y") if hasattr(v, "strftime") else str(v))) for v in r]
                for r in wb.active.iter_rows(values_only=True)]
    else:
        text = data.decode("utf-8-sig", errors="replace")
        rows = list(csv.reader(io.StringIO(text)))
    rows = [r for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        return []
    header = [re.sub(r"\s+", " ", (h or "").strip().lower()) for h in rows[0]]
    idx: dict[str, int] = {}
    for key, names in COLS.items():
        for j, h in enumerate(header):
            if h in names:
                idx.setdefault(key, j)
    return [{k: (r[j] if j < len(r) else "") for k, j in idx.items()} for r in rows[1:]]


def _state_for(status: str, has_approved: bool) -> str:
    s = (status or "").lower()
    if re.search(r"not\s+submitted|pending\s+submission|to\s+be\s+submitted|draft", s):
        return "DISCHARGED"
    semantic = get_adapter("nha_tms").semantic_status(s)
    return {"paid": "PAID_BY_PAYER", "rejected": "REJECTED", "part_approved": "PART_APPROVED",
            "query": "CLAIM_QUERY", "approved": "APPROVED", "submitted": "CLAIM_SUBMITTED"}.get(
        semantic, "APPROVED" if has_approved else "CLAIM_SUBMITTED")


def import_outstanding(db: Session, hospital_id: str, filename: str, data: bytes, actor, default_payer: Payer | None,
                       next_case_no) -> dict:
    payers = {p.code.lower(): p for p in db.query(Payer).filter(Payer.hospital_id == hospital_id).all()}
    by_name = {p.name.lower(): p for p in payers.values()}
    created, skipped, errors = 0, 0, []
    for i, row in enumerate(_read_rows(filename, data), start=2):
        ptxt = (row.get("payer") or "").strip().lower()
        payer = payers.get(ptxt) or by_name.get(ptxt) or next(
            (p for k, p in payers.items() if k and k in ptxt), None) or default_payer
        if not payer:
            errors.append(f"Row {i}: payer '{row.get('payer')}' not recognised.")
            continue
        ref = (row.get("ref") or "").strip()
        if ref and db.query(Case).filter(Case.hospital_id == hospital_id, Case.portal_case_id == ref).first():
            skipped += 1
            continue
        admitted = parse_datetime_ist(row.get("admission")) or parse_datetime_ist(row.get("discharge"))
        if not admitted:
            errors.append(f"Row {i}: no admission or discharge date.")
            continue
        claimed = to_paise(row.get("claimed"))
        approved = to_paise(row.get("approved"))
        state = _state_for(row.get("status", ""), bool(approved))
        pack = get_pack(payer.scheme)
        case = Case(hospital_id=hospital_id, payer_id=payer.id, case_no=next_case_no(db, hospital_id, admitted),
                    rule_pack_version=pack.version, beneficiary_name=(row.get("patient") or "Unknown").strip(),
                    beneficiary_ref=(row.get("card") or "").strip() or None, admitted_at=admitted,
                    discharged_at=parse_datetime_ist(row.get("discharge")) or admitted, billed_paise=claimed,
                    state=state, portal_case_id=ref or None, imported=True)
        case.payer = payer
        db.add(case)
        db.flush()
        submitted = parse_datetime_ist(row.get("submitted"))
        if state not in {"DISCHARGED"} and claimed:
            db.add(Claim(hospital_id=hospital_id, case_id=case.id, stage="final", version=1, amount_paise=claimed,
                         portal_ref=ref or None, submitted_at=submitted or case.discharged_at, status="imported"))
        if state in {"APPROVED", "PART_APPROVED", "PAID_BY_PAYER"}:
            appr = approved if approved is not None else claimed or 0
            ded = max((claimed or 0) - appr, 0)
            db.add(Decision(hospital_id=hospital_id, case_id=case.id, decision="part_approved" if ded else "approved",
                            approved_paise=appr, deductions=[{"reason": "From imported list", "amount_paise": ded}] if ded else [],
                            source="import"))
            if ded and state == "APPROVED":
                case.state = "PART_APPROVED"
        if state == "REJECTED":
            db.add(Decision(hospital_id=hospital_id, case_id=case.id, decision="rejected", approved_paise=0,
                            rejection_reason=row.get("query") or row.get("status") or "From imported list",
                            source="import"))
        if state == "CLAIM_QUERY":
            db.add(Query(hospital_id=hospital_id, case_id=case.id, stage="claim", raised_at=submitted or case.discharged_at,
                         text=row.get("query") or "Query shown in the imported list (text not provided).",
                         category="clarification", source="import"))
        if state == "DISCHARGED":
            clocks.start(db, case, "claim_submission", case.discharged_at, pack.deadline("claim_submission"))
        audit.record(db, hospital_id=hospital_id, actor=actor, action="case.imported", entity="case", entity_id=case.id,
                     case_id=case.id, after={"row": i, "state": case.state})
        created += 1
    db.flush()
    return {"created": created, "skipped_existing": skipped, "errors": errors}


__all__ = ["import_outstanding", "parse_date"]
