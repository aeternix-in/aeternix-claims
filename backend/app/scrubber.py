"""M4 Claim scrubber: catch what gets a claim queried or cut, before it is submitted.

Three layers, cheapest first:
  1. hard rules    — documents, dates, identity, rates, totals, signatures, windows
  2. model checks  — does the diagnosis support the procedure and the package, do the
                     investigations support the indication, does the summary agree with the bill
  3. clinician     — high-value or flagged cases go to an Aeternix clinician the same day

A red result blocks submission until fixed. An amber result blocks until a person clears it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from . import audit, llm
from .config import settings
from .documents import DOC_TYPES, needs_verification
from .models import Case, CheckResult, Document, new_id
from .money import days_between, now_utc, parse_date, rupees
from .rulepacks import clinical_rules, get_pack, rate_card_for
from .statemachine import OPEN_STATES


@dataclass
class Finding:
    rule: str
    layer: str
    result: str
    message: str
    details: dict = field(default_factory=dict)
    needs_clinician: bool = False


def _val(doc: Document, key: str):
    """Verified value first; else the extracted suggestion. Returns (value, verified)."""
    if key in (doc.verified_fields or {}):
        return doc.verified_fields[key], True
    ex = (doc.extracted or {}).get(key)
    return (ex.get("value") if isinstance(ex, dict) else ex), False


def _norm_name(s: str) -> list[str]:
    s = re.sub(r"(?i)\b(mr|mrs|ms|shri|smt|kumari|dr|late)\.?\s+", "", s or "")
    return [t for t in re.sub(r"[^a-z ]", " ", s.lower()).split() if t]


def names_match(a: str, b: str) -> bool:
    ta, tb = _norm_name(a), _norm_name(b)
    if not ta or not tb:
        return True
    if ta == tb:
        return True
    # allow initials and a missing middle name: every token of the shorter must match a token or initial
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    def ok(tok):
        return any(tok == x or (len(tok) == 1 and x.startswith(tok)) or (len(x) == 1 and tok.startswith(x)) for x in long_)
    return all(ok(t) for t in short) and ta[-1][0] == tb[-1][0]


def _procedure_text(case: Case, docs: list[Document]) -> str:
    parts = [case.planned_procedure or ""]
    for d in docs:
        if d.doc_type in {"discharge_summary", "ot_notes"}:
            v, _ = _val(d, "procedure")
            parts.append(str(v or ""))
    for line in case.bill_lines or []:
        parts.append(line.get("description", ""))
    return " ".join(parts).lower()


def check(db: Session, case: Case) -> list[Finding]:
    pack = get_pack(case.payer.scheme)
    docs = [d for d in db.query(Document).filter(Document.case_id == case.id, Document.voided.is_(False)).all()]
    by_type: dict[str, list[Document]] = {}
    for d in docs:
        by_type.setdefault(d.doc_type, []).append(d)
    f: list[Finding] = []
    stage = "final" if case.discharged_at else "preauth"
    flags = {"emergency": case.emergency, "surgical": case.surgical, "implant_used": case.implant_used}

    # 1. required documents
    missing = [t for t in pack.docs_for(stage, flags) if t not in by_type]
    for t in missing:
        f.append(Finding("required_documents", "hard", "red", f"{DOC_TYPES.get(t, t)} is missing.",
                         {"doc_type": t, "stage": stage}))
    if not missing:
        f.append(Finding("required_documents", "hard", "green",
                         f"All {len(pack.docs_for(stage, flags))} required documents are present.", {"stage": stage}))

    # 2. every key field on every document confirmed by a person
    for d in docs:
        pending = needs_verification(d.doc_type, d.verified_fields)
        if pending:
            f.append(Finding("fields_verified", "hard", "red",
                             f"Confirm the fields on the {DOC_TYPES.get(d.doc_type, d.doc_type).lower()} "
                             f"({', '.join(pending)}).", {"document_id": d.id, "fields": pending}))

    ds = (by_type.get("discharge_summary") or [None])[0]
    bill = (by_type.get("final_bill") or [None])[0]

    # 3. date order and dates agreeing across documents
    adm = case.admitted_at.date() if case.admitted_at else None
    dis = case.discharged_at.date() if case.discharged_at else None
    if adm and dis and dis < adm:
        f.append(Finding("date_order", "hard", "red", "Discharge date is before admission date."))
    proc_date = case.procedure_date
    if ds:
        v, _ = _val(ds, "procedure_date")
        proc_date = proc_date or parse_date(v)
    if proc_date and adm and proc_date < adm:
        f.append(Finding("date_order", "hard", "red",
                         f"Procedure date {proc_date:%d/%m/%Y} is before admission {adm:%d/%m/%Y}."))
    if proc_date and dis and proc_date > dis:
        f.append(Finding("date_order", "hard", "red",
                         f"Procedure date {proc_date:%d/%m/%Y} is after discharge {dis:%d/%m/%Y}."))
    for d in [x for x in (ds, bill) if x]:
        for key, case_date, label in (("admission_date", adm, "admission"), ("discharge_date", dis, "discharge")):
            v, _ = _val(d, key)
            dv = parse_date(v)
            if dv and case_date and dv != case_date:
                f.append(Finding("date_consistency", "hard", "red",
                                 f"The {DOC_TYPES[d.doc_type].lower()} says {label} {dv:%d/%m/%Y}; "
                                 f"the case says {case_date:%d/%m/%Y}.", {"document_id": d.id}))

    # 4. identity across documents
    for d in docs:
        v, _ = _val(d, "patient_name")
        if v and not names_match(str(v), case.beneficiary_name):
            f.append(Finding("identity_match", "hard", "red",
                             f"Name on the {DOC_TYPES.get(d.doc_type, d.doc_type).lower()} ('{v}') does not match "
                             f"the case ('{case.beneficiary_name}').", {"document_id": d.id}))
        ref, _ = _val(d, "beneficiary_ref")
        if ref and case.beneficiary_ref and re.sub(r"\W", "", str(ref)).upper() != re.sub(r"\W", "", case.beneficiary_ref).upper():
            f.append(Finding("identity_match", "hard", "red",
                             f"Card number on the {DOC_TYPES.get(d.doc_type, d.doc_type).lower()} ({ref}) does not "
                             f"match the case ({case.beneficiary_ref}).", {"document_id": d.id}))

    # 5–7. rate card, length of stay, bill totals
    rates, meta = rate_card_for(pack.key, case.hospital_id, adm)
    lines = case.bill_lines or []
    if bill and not lines:
        v, _ = _val(bill, "bill_lines")
        lines = v or []
    if not meta.get("loaded"):
        f.append(Finding("rate_card", "hard", "amber", "No rate card is loaded for this scheme, so bill lines "
                         "were not checked against rates.", meta))
    elif lines:
        over, unknown = [], []
        for ln in lines:
            code = str(ln.get("code", "")).upper()
            entry = rates.get(code)
            if not entry:
                unknown.append(code)
                continue
            allowed = entry.rate_paise * int(ln.get("qty") or 1)
            if (ln.get("amount_paise") or 0) > allowed:
                over.append({"code": code, "billed": ln["amount_paise"], "allowed": allowed})
        for o in over:
            f.append(Finding("bill_vs_rate_card", "hard", "red",
                             f"{o['code']} is billed at {rupees(o['billed'])}; the rate card allows "
                             f"{rupees(o['allowed'])}.", {**o, **meta}))
        if unknown:
            f.append(Finding("bill_vs_rate_card", "hard", "amber",
                             f"Not in the rate card: {', '.join(unknown)}. Check the code or justify the line.",
                             {"codes": unknown, **meta}))
        if not over and not unknown:
            note = " (SAMPLE rates — load the official list)" if meta.get("sample") else ""
            f.append(Finding("bill_vs_rate_card", "hard", "green", f"Every bill line is within the rate card{note}.",
                             meta))
        if adm and dis:
            los = (dis - adm).days
            for ln in lines:
                entry = rates.get(str(ln.get("code", "")).upper())
                if entry and entry.los_days and entry.category == "package" and los > entry.los_days + 1:
                    f.append(Finding("length_of_stay", "hard", "amber",
                                     f"Stay of {los} days is longer than the {entry.los_days}-day package "
                                     f"{entry.code}. Add a clinical note on why.",
                                     {"los": los, "package_los": entry.los_days}, needs_clinician=True))
    if bill:
        total, _ = _val(bill, "total_amount")
        if lines and total:
            s = sum(int(ln.get("amount_paise") or 0) for ln in lines)
            if s != int(total):
                f.append(Finding("bill_total", "hard", "red",
                                 f"Bill lines add up to {rupees(s)} but the bill total is {rupees(int(total))}."))
        if total and case.billed_paise and int(total) != case.billed_paise:
            f.append(Finding("bill_total", "hard", "red",
                             f"Claim amount {rupees(case.billed_paise)} differs from the bill total "
                             f"{rupees(int(total))}."))
    if stage == "final" and not case.billed_paise and not (bill and _val(bill, "total_amount")[0]):
        f.append(Finding("bill_total", "hard", "red", "The claim amount is missing. Add the final bill."))

    # 8. signatures
    if ds:
        signed, verified = _val(ds, "signed")
        if signed is False or (isinstance(signed, str) and signed.lower() in {"false", "no", "0"}):
            f.append(Finding("signatures", "hard", "red", "The discharge summary is not signed by the treating doctor."))

    # 9. claim window
    window = pack.deadline("claim_submission")
    if window and case.discharged_at and case.state in {"DISCHARGED", "READY_TO_SUBMIT", "LATE_CONDONATION"}:
        used = days_between(case.discharged_at, now_utc())
        left = int(window.value) - used
        if left < 0:
            slab = pack.condonation_slab(used)
            who = f" Condonation is decided by the {slab.authority}." if slab else ""
            f.append(Finding("claim_window", "hard", "red" if not case.condonation_granted else "green",
                             (f"The {int(window.value)}-day window passed {-left} days ago.{who}"
                              if not case.condonation_granted else "Window passed; condonation recorded."),
                             {"days_since_discharge": used, "authority": slab.authority if slab else None}))
        else:
            f.append(Finding("claim_window", "hard", "green", f"{left} days left in the {int(window.value)}-day window."))

    # 10. counter checks (PM-JAY biometrics)
    for cc in pack.counter_checks:
        due = (cc.key != "biometric_discharge") or bool(case.discharged_at)
        if due and not (case.counter_checks or {}).get(cc.key):
            f.append(Finding("counter_checks", "hard", "red", f"{cc.label} is not recorded.", {"check": cc.key}))

    # 11. duplicate case
    if case.beneficiary_ref:
        dup = (db.query(Case).filter(Case.hospital_id == case.hospital_id, Case.id != case.id,
                                     Case.beneficiary_ref == case.beneficiary_ref, Case.state.in_(OPEN_STATES)).all())
        for o in dup:
            o_end = o.discharged_at or now_utc()
            c_end = case.discharged_at or now_utc()
            if o.admitted_at <= c_end and case.admitted_at <= o_end:
                f.append(Finding("duplicate_case", "hard", "amber",
                                 f"Case {o.case_no} has the same card number and overlapping dates.", {"other": o.case_no}))

    # 12. diagnosis code
    icd = case.icd10 or (ds and _val(ds, "icd10")[0])
    if not icd or not re.fullmatch(r"[A-TV-Z]\d{2}(\.\d{1,2})?", str(icd).upper()):
        f.append(Finding("diagnosis_code", "hard", "red", "Add a valid ICD-10 code for the final diagnosis."))

    # 13. implant mentioned but not flagged
    if ds and not case.implant_used:
        text = (ds.text or "").lower() + " " + str(_val(ds, "implant")[0] or "").lower()
        if re.search(r"\b(dj|double\s*j|ureteric)\s+stent|\bimplant\b(?!\s*:\s*(nil|none|na))", text):
            f.append(Finding("implant_flag", "hard", "amber",
                             "The discharge summary mentions an implant or stent. Mark 'implant used' so the "
                             "invoice and sticker are required."))

    # ---------------------------------------------------------------- model layer
    f += _model_checks(case, docs, by_type, icd, rates)

    # ---------------------------------------------------------------- clinician layer
    amount = case.billed_paise or 0
    if amount >= settings.high_value_threshold_paise:
        f.append(Finding("high_value_review", "clinician", "amber",
                         f"High-value claim ({rupees(amount)}): clinician review before submission.",
                         {"threshold": settings.high_value_threshold_paise}, needs_clinician=True))
    return f


def _model_checks(case: Case, docs, by_type, icd, rates) -> list[Finding]:
    out: list[Finding] = []
    rules = clinical_rules()["procedures"]
    ptext = _procedure_text(case, docs)
    matched = [r for r in rules if any(k in ptext for k in r["keywords"])]
    icd_s = str(icd or "").upper()
    for r in matched:
        if icd_s and not any(icd_s.startswith(p) for p in r["expect_icd10_prefixes"]):
            out.append(Finding("diagnosis_supports_procedure", "model", "amber",
                               f"{r['name']} usually needs a diagnosis coded {', '.join(r['expect_icd10_prefixes'])}; "
                               f"this case is coded {icd_s}.", {"procedure": r["name"]}, needs_clinician=True))
        inv_text = " ".join((d.text or "").lower() for d in by_type.get("investigation_report", []))
        ds = (by_type.get("discharge_summary") or [None])[0]
        if ds:
            inv_text += " " + str(_val(ds, "investigations")[0] or "").lower()
        if not any(k in inv_text for k in r["supporting_investigations"]):
            out.append(Finding("investigations_support", "model", "amber",
                               f"No investigation found that supports {r['name']} "
                               f"(expected one of: {', '.join(r['supporting_investigations'][:4])}).",
                               {"procedure": r["name"]}, needs_clinician=True))
    ds = (by_type.get("discharge_summary") or [None])[0]
    if ds:
        dstext = ((ds.text or "") + " " + str(_val(ds, "procedure")[0] or "")).lower()
        for ln in case.bill_lines or []:
            entry = rates.get(str(ln.get("code", "")).upper())
            desc = (ln.get("description") or "").lower()
            words = [r for r in rules if any(k in desc for k in r["keywords"])]
            if entry and entry.category == "package" and words and not any(
                    k in dstext for r in words for k in r["keywords"]):
                out.append(Finding("summary_vs_bill", "model", "amber",
                                   f"The bill charges '{ln.get('description')}' but the discharge summary does not "
                                   f"mention it.", {"code": ln.get("code")}, needs_clinician=True))
            if (entry and entry.category == "implant") or "stent (implant)" in desc:
                if not re.search(r"stent|implant", dstext):
                    out.append(Finding("summary_vs_bill", "model", "amber",
                                       "An implant is billed but the discharge summary does not mention one.",
                                       {"code": ln.get("code")}, needs_clinician=True))
    if not matched and (case.planned_procedure or case.bill_lines):
        out.append(Finding("diagnosis_supports_procedure", "model", "green",
                           "No clinical rule covers this procedure yet; nothing to compare."))
    elif matched and not any(x.rule == "diagnosis_supports_procedure" for x in out):
        out.append(Finding("diagnosis_supports_procedure", "model", "green",
                           f"Diagnosis {icd_s or '—'} fits {', '.join(r['name'] for r in matched)}."))

    if llm.enabled() and ds:
        try:
            names = [case.beneficiary_name]
            res = llm.complete_json(
                "You review Indian hospital insurance claims before submission. List only concrete "
                "inconsistencies a payer would query. JSON: {\"issues\": [\"...\"]}. Empty list if none.",
                llm.redact(f"Diagnosis: {case.provisional_diagnosis} ({icd})\nProcedure: {case.planned_procedure}\n"
                           f"Bill lines: {case.bill_lines}\n\nDischarge summary:\n{(ds.text or '')[:8000]}", names))
            for issue in (res.get("issues") or [])[:5]:
                out.append(Finding("model_review", "model", "amber", str(issue)[:480], needs_clinician=True))
        except Exception:
            pass
    return out


def run(db: Session, case: Case, actor) -> tuple[str, list[CheckResult]]:
    findings = check(db, case)
    run_id = new_id()
    prior = {(r.rule, r.message): r for r in db.query(CheckResult).filter(
        CheckResult.case_id == case.id, CheckResult.cleared_at.isnot(None)).all()}
    rows = []
    for fd in findings:
        row = CheckResult(hospital_id=case.hospital_id, case_id=case.id, run_id=run_id, rule=fd.rule, layer=fd.layer,
                          result=fd.result, message=fd.message, details=fd.details, needs_clinician=fd.needs_clinician)
        p = prior.get((fd.rule, fd.message))
        if p and fd.result == "amber":  # a cleared amber stays cleared across re-runs
            row.cleared_by, row.cleared_at, row.clear_note = p.cleared_by, p.cleared_at, p.clear_note
        db.add(row)
        rows.append(row)
    status = "red" if any(r.result == "red" for r in rows) else (
        "amber" if any(r.result == "amber" and not r.cleared_at for r in rows) else "green")
    audit.record(db, hospital_id=case.hospital_id, actor=actor, action="scrub.run", entity="case", entity_id=case.id,
                 case_id=case.id, after={"run_id": run_id, "status": status,
                                         "red": sum(r.result == "red" for r in rows),
                                         "amber": sum(r.result == "amber" for r in rows)})
    if case.state == "READY_TO_SUBMIT" and status != "green":
        from .statemachine import apply
        apply(db, case, "reopen_pack", actor, {})
    db.flush()
    return status, rows
