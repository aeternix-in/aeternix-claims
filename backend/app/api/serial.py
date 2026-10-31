"""JSON shapes the web app and the extension read. Amounts are integer paise; times are UTC ISO."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy.orm import Session

from ..documents import DOC_TYPES, KEY_FIELDS, needs_verification
from ..models import (Allocation, AuditLog, BankTransaction, Case, CheckResult, Claim, Clock, Decision, Document,
                      Escalation, Payer, Query, User)
from ..money import now_utc
from ..recon import case_money
from ..reporting import buckets_for, case_flags
from ..rulepacks import get_pack
from ..statemachine import BY_EVENT, LABELS, STATION, allowed_events, latest_run_status


def iso(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat() + "Z"
    if isinstance(v, date):
        return v.isoformat()
    return v


AUDIT_LABELS = {
    "case.created": "Case opened", "case.updated": "Case details changed", "case.imported": "Case imported",
    "case.portal_registered": "Registered on the portal", "case.counter_check": "Counter check recorded",
    "pack.downloaded": "Claim pack downloaded", "check.cleared": "Amber flag cleared",
    "scrub.run": "Claim check run", "document.added": "Document added", "document.verified": "Document verified",
    "document.reclassified": "Document type changed", "document.voided": "Document removed",
    "query.raised": "Query recorded", "query.drafted": "Reply drafted", "query.edited": "Reply edited",
    "query.clinician_signoff": "Clinician signed off the reply", "query.replied": "Reply sent",
    "query.outcome": "Query outcome recorded", "bank.allocation_confirmed": "Bank credit confirmed",
    "bank.allocation_rejected": "Bank match rejected", "escalation.drafted": "Letter drafted",
    "escalation.sent": "Letter sent", "escalation.response": "Reply to letter recorded",
    "email.received": "Payer email received",
}


def audit_label(action: str) -> str:
    if action in AUDIT_LABELS:
        return AUDIT_LABELS[action]
    if action.startswith("case.") and action[5:] in BY_EVENT:
        return BY_EVENT[action[5:]].label
    return action.split(".", 1)[-1].replace("_", " ").capitalize()


def user(u: User) -> dict:
    return {"id": u.id, "name": u.name, "email": u.email, "role": u.role, "hospital_id": u.hospital_id,
            "hospital_ids": u.hospital_ids or []}


def payer(p: Payer) -> dict:
    return {"id": p.id, "code": p.code, "name": p.name, "scheme": p.scheme, "kind": p.kind, "portal": p.portal,
            "expected_tds_bps": p.expected_tds_bps, "narration_keywords": p.narration_keywords or [],
            "escalation_contacts": p.escalation_contacts or [], "active": p.active}


def case_summary(db: Session, c: Case) -> dict:
    flags = case_flags(db, c)
    return {"id": c.id, "case_no": c.case_no, "payer": {"id": c.payer.id, "code": c.payer.code, "name": c.payer.name},
            "beneficiary_name": c.beneficiary_name, "beneficiary_ref": c.beneficiary_ref, "state": c.state,
            "state_label": LABELS[c.state], "station": STATION[c.state], "admitted_at": iso(c.admitted_at),
            "discharged_at": iso(c.discharged_at), "billed_paise": c.billed_paise, "portal_case_id": c.portal_case_id,
            "state_changed_at": iso(c.state_changed_at), "check_status": flags["check_status"],
            "imported": c.imported, "buckets": [b for b, a in buckets_for(db, c) if a]}


def clock(c: Clock) -> dict:
    now = now_utc()
    return {"id": c.id, "kind": c.kind, "label": c.label, "started_at": iso(c.started_at), "due_at": iso(c.due_at),
            "stopped_at": iso(c.stopped_at), "status": c.status, "source": c.source, "query_id": c.query_id,
            "overdue": c.status == "running" and c.due_at < now,
            "seconds_left": int((c.due_at - now).total_seconds()) if c.status == "running" else None}


def document(d: Document, with_text: bool = False) -> dict:
    out = {"id": d.id, "doc_type": d.doc_type, "doc_label": DOC_TYPES.get(d.doc_type, d.doc_type), "source": d.source,
           "filename": d.filename, "mime": d.mime, "size": d.size, "sha256": d.sha256, "ocr_method": d.ocr_method,
           "classification_confidence": d.classification_confidence, "extracted": d.extracted or {},
           "verified_fields": d.verified_fields or {}, "verified_at": iso(d.verified_at),
           "key_fields": KEY_FIELDS.get(d.doc_type, []), "needs_verification": needs_verification(d.doc_type, d.verified_fields),
           "voided": d.voided, "created_at": iso(d.created_at)}
    if with_text:
        out["text"] = (d.text or "")[:20000]
    return out


def check(r: CheckResult) -> dict:
    return {"id": r.id, "run_id": r.run_id, "rule": r.rule, "layer": r.layer, "result": r.result, "message": r.message,
            "details": r.details or {}, "needs_clinician": r.needs_clinician, "cleared_by": r.cleared_by,
            "cleared_at": iso(r.cleared_at), "clear_note": r.clear_note, "created_at": iso(r.created_at)}


def query(q: Query) -> dict:
    return {"id": q.id, "case_id": q.case_id, "stage": q.stage, "raised_at": iso(q.raised_at), "text": q.text,
            "category": q.category, "requested_doc_types": q.requested_doc_types or [], "due_at": iso(q.due_at),
            "cutoff_at": iso(q.cutoff_at), "source": q.source, "status": q.status, "draft_reply": q.draft_reply,
            "draft_documents": q.draft_documents or [], "reply_text": q.reply_text,
            "documents_sent": q.documents_sent or [], "replied_at": iso(q.replied_at),
            "clinician_signoff_at": iso(q.clinician_signoff_at), "outcome": q.outcome,
            "overdue": bool(q.due_at and q.due_at < now_utc() and q.status not in {"replied", "closed"})}


def escalation(e: Escalation) -> dict:
    return {"id": e.id, "case_id": e.case_id, "kind": e.kind, "subject": e.subject, "body": e.body,
            "addressed_to": e.addressed_to, "amount_paise": e.amount_paise, "status": e.status,
            "sent_at": iso(e.sent_at), "reference_no": e.reference_no, "response": e.response,
            "created_at": iso(e.created_at)}


def bank_txn(db: Session, t: BankTransaction) -> dict:
    allocs = db.query(Allocation).filter(Allocation.bank_txn_id == t.id, Allocation.status != "rejected").all()
    out = []
    for a in allocs:
        c = db.get(Case, a.case_id)
        out.append({"id": a.id, "case_id": a.case_id, "case_no": c.case_no if c else None,
                    "payer": c.payer.code if c else None, "amount_paise": a.amount_paise, "tax_paise": a.tax_paise,
                    "method": a.method, "confidence": a.confidence, "status": a.status})
    return {"id": t.id, "account": t.account, "txn_date": iso(t.txn_date), "amount_paise": t.amount_paise,
            "narration": t.narration, "reference_no": t.reference_no, "status": t.status, "match_note": t.match_note,
            "allocations": out}


def case_detail(db: Session, c: Case) -> dict:
    pack = get_pack(c.payer.scheme)
    status, rows = latest_run_status(db, c)
    docs = db.query(Document).filter(Document.case_id == c.id).order_by(Document.created_at).all()
    claims = db.query(Claim).filter(Claim.case_id == c.id).order_by(Claim.created_at).all()
    decisions = db.query(Decision).filter(Decision.case_id == c.id).order_by(Decision.decided_at).all()
    queries = db.query(Query).filter(Query.case_id == c.id).order_by(Query.raised_at.desc()).all()
    clocks = db.query(Clock).filter(Clock.case_id == c.id).order_by(Clock.due_at).all()
    escs = db.query(Escalation).filter(Escalation.case_id == c.id).order_by(Escalation.created_at.desc()).all()
    allocs = db.query(Allocation).filter(Allocation.case_id == c.id, Allocation.status != "rejected").all()
    timeline = db.query(AuditLog).filter(AuditLog.case_id == c.id).order_by(AuditLog.created_at.desc()).limit(200).all()
    flags = case_flags(db, c)
    return {
        **case_summary(db, c),
        "referral_no": c.referral_no, "age": c.age, "sex": c.sex, "ward": c.ward, "treating_doctor": c.treating_doctor,
        "provisional_diagnosis": c.provisional_diagnosis, "icd10": c.icd10, "planned_procedure": c.planned_procedure,
        "package_code": c.package_code, "procedure_date": iso(c.procedure_date), "emergency": c.emergency,
        "portability": c.portability, "implant_used": c.implant_used, "surgical": c.surgical, "bill_no": c.bill_no,
        "bill_date": iso(c.bill_date), "bill_lines": c.bill_lines or [], "portal_registered_at": iso(c.portal_registered_at),
        "counter_checks": c.counter_checks or {}, "condonation_granted": c.condonation_granted,
        "close_reason": c.close_reason, "notes": c.notes, "rule_pack": {"key": pack.key, "name": pack.name,
                                                                         "version": c.rule_pack_version,
                                                                         "counter_checks": [cc.model_dump() for cc in pack.counter_checks],
                                                                         "needs_preauth": pack.needs_preauth(c.emergency),
                                                                         "portal": pack.portal},
        "payer_full": payer(c.payer),
        "allowed_actions": allowed_events(c),
        "money": case_money(db, c),
        "interest": flags["interest"],
        "check": {"status": status, "results": [check(r) for r in rows]},
        "documents": [document(d) for d in docs],
        "claims": [{"id": x.id, "stage": x.stage, "version": x.version, "amount_paise": x.amount_paise,
                    "portal_ref": x.portal_ref, "submitted_at": iso(x.submitted_at), "status": x.status} for x in claims],
        "decisions": [{"id": d.id, "stage": d.stage, "decision": d.decision, "decided_at": iso(d.decided_at),
                       "approved_paise": d.approved_paise, "deductions": d.deductions or [],
                       "rejection_reason": d.rejection_reason, "source": d.source} for d in decisions],
        "queries": [query(q) for q in queries],
        "clocks": [clock(k) for k in clocks],
        "escalations": [escalation(e) for e in escs],
        "allocations": [{"id": a.id, "bank_txn_id": a.bank_txn_id, "amount_paise": a.amount_paise,
                         "tax_paise": a.tax_paise, "method": a.method, "status": a.status} for a in allocs],
        "timeline": [{"at": iso(a.created_at), "actor": a.actor_name, "action": a.action,
                      "label": audit_label(a.action), "after": a.after}
                     for a in timeline],
    }
