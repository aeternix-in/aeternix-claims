"""M5 Query desk: capture, classify, time, draft, sign off, reply, learn."""
from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy.orm import Session

from . import audit, llm
from .documents import DOC_TYPES
from .models import Case, Document, Query, QueryLibraryEntry
from .money import add, now_utc
from .rulepacks import get_pack
from .scrubber import _val
from .statemachine import apply

CATEGORIES = {
    "missing_document": "Missing document",
    "clarification": "Clarification",
    "rate": "Rate or tariff",
    "medical_necessity": "Medical necessity",
    "duplicate": "Duplicate",
}

_CAT_RULES = [
    ("duplicate", r"duplicate|already\s+(claimed|submitted|availed)|same\s+(patient|beneficiary)"),
    ("medical_necessity", r"justif|indication|necessity|why\s+(was|is)|clinical\s+(notes|details|reason)|"
                          r"line\s+of\s+treatment|prolonged\s+stay|length\s+of\s+stay"),
    ("rate", r"\brate\b|tariff|package\s+(rate|amount)|excess|higher\s+than|beyond\s+(the\s+)?(rate|package)|"
             r"non[-\s]?payable|deduct"),
    ("missing_document", r"submit|upload|provide|attach|copy\s+of|not\s+(attached|enclosed|legible|clear)|missing|"
                         r"resubmit|send\s+the"),
    ("clarification", r"clarif|explain|confirm|mismatch|discrepan|correct"),
]

_DOC_HINTS = {
    "discharge_summary": r"discharge\s+summary",
    "final_bill": r"(final|detailed|itemi[sz]ed)\s+bill|bill\s+break",
    "investigation_report": r"investigation|report|ct\b|usg|ultrasound|x-?ray|lab|creatinine|histopath",
    "ot_notes": r"\bot\s+notes|operation\s+notes|operative\s+notes|surgery\s+notes",
    "implant_invoice": r"implant\s+invoice|invoice\s+of\s+(the\s+)?(implant|stent)|stent\s+invoice",
    "implant_sticker": r"sticker|barcode",
    "referral_letter": r"referral|permission\s+letter",
    "emergency_certificate": r"emergency\s+certificate",
    "beneficiary_card": r"card\s+copy|beneficiary\s+card|id\s+proof",
    "consent_form": r"consent",
}


def classify(text: str) -> tuple[str, list[str]]:
    t = (text or "").lower()
    category = next((c for c, p in _CAT_RULES if re.search(p, t)), "clarification")
    docs = [d for d, p in _DOC_HINTS.items() if re.search(p, t)]
    if category == "clarification" and docs and re.search(r"submit|upload|provide|attach|copy", t):
        category = "missing_document"
    return category, docs


def raise_query(db: Session, case: Case, text: str, actor, *, stage: str | None = None,
                raised_at: datetime | None = None, source: str = "manual") -> Query:
    if stage is None:
        stage = "preauth" if case.state in {"PREAUTH_SUBMITTED", "PREAUTH_QUERY"} else "claim"
    category, docs = classify(text)
    pack = get_pack(case.payer.scheme)
    raised = raised_at or now_utc()
    reply_d = pack.deadline("preauth_query_reply" if stage == "preauth" else "claim_query_reply")
    cutoff_d = pack.deadline("claim_query_cutoff") if stage == "claim" else None
    q = Query(hospital_id=case.hospital_id, case_id=case.id, stage=stage, raised_at=raised, text=text.strip(),
              category=category, requested_doc_types=docs, source=source,
              due_at=add(raised, reply_d.value, reply_d.unit) if reply_d else None,
              cutoff_at=add(raised, cutoff_d.value, cutoff_d.unit) if cutoff_d else None)
    db.add(q)
    db.flush()
    event = "raise_preauth_query" if stage == "preauth" else "raise_claim_query"
    if case.state in {"PREAUTH_SUBMITTED", "CLAIM_SUBMITTED"}:
        apply(db, case, event, actor, {"query_id": q.id, "raised_at": raised, "text": text[:300]})
    else:  # a second query on a case already in query: give it its own clocks
        from . import clocks
        clocks.on_event(db, case, pack, event, raised, {"query_id": q.id, "raised_at": raised})
    audit.record(db, hospital_id=case.hospital_id, actor=actor, action="query.raised", entity="query",
                 entity_id=q.id, case_id=case.id, after={"category": category, "docs": docs, "source": source})
    draft(db, q, actor)
    return q


def _library_suggestion(db: Session, q: Query, scheme: str) -> QueryLibraryEntry | None:
    words = set(re.findall(r"[a-z]{4,}", q.text.lower()))
    best, best_score = None, 0.0
    rows = (db.query(QueryLibraryEntry).filter(QueryLibraryEntry.category == q.category,
                                               QueryLibraryEntry.scheme == scheme,
                                               QueryLibraryEntry.outcome == "cleared")
            .filter((QueryLibraryEntry.hospital_id == q.hospital_id) | (QueryLibraryEntry.hospital_id.is_(None)))
            .limit(500).all())
    for r in rows:
        other = set(re.findall(r"[a-z]{4,}", r.query_text.lower()))
        if not other:
            continue
        score = len(words & other) / len(words | other)
        if score > best_score:
            best, best_score = r, score
    return best if best_score >= 0.35 else None


def draft(db: Session, q: Query, actor) -> Query:
    case = db.get(Case, q.case_id)
    docs = db.query(Document).filter(Document.case_id == case.id, Document.voided.is_(False)).all()
    attach = [d for d in docs if d.doc_type in (q.requested_doc_types or [])]
    missing = [t for t in (q.requested_doc_types or []) if t not in {d.doc_type for d in attach}]
    ref = case.portal_case_id or case.case_no
    lines = [f"Ref: {ref} ({case.case_no})", ""]
    if q.category == "missing_document":
        if attach:
            lines.append("As requested, we enclose:")
            lines += [f"  - {DOC_TYPES.get(d.doc_type, d.doc_type)} ({d.filename})" for d in attach]
        if missing:
            lines.append("")
            lines.append("NOT YET IN THE CASE FILE (add before sending): "
                         + ", ".join(DOC_TYPES.get(t, t) for t in missing))
    elif q.category == "medical_necessity":
        ds = next((d for d in docs if d.doc_type == "discharge_summary"), None)
        diag = (ds and _val(ds, "diagnosis")[0]) or case.provisional_diagnosis or "—"
        proc = (ds and _val(ds, "procedure")[0]) or case.planned_procedure or "—"
        inv = (ds and _val(ds, "investigations")[0]) or "see enclosed reports"
        lines += ["Clinical justification:",
                  f"  Diagnosis: {diag}{f' ({case.icd10})' if case.icd10 else ''}",
                  f"  Procedure: {proc}",
                  f"  Supporting investigations: {inv}",
                  "  [Clinician to complete: indication, why this line of treatment, and any reason for the length of stay]"]
        attach = attach or [d for d in docs if d.doc_type in {"discharge_summary", "investigation_report", "ot_notes"}]
    elif q.category == "rate":
        lines += ["The billed amounts follow the applicable rate list for this hospital and date of service.",
                  "Line-wise details are in the enclosed final bill. [Desk: name the rate list and the line in question]"]
        attach = attach or [d for d in docs if d.doc_type == "final_bill"]
    elif q.category == "duplicate":
        lines += [f"This is a single admission from {case.admitted_at:%d/%m/%Y}"
                  f"{f' to {case.discharged_at:%d/%m/%Y}' if case.discharged_at else ''}.",
                  "No other claim has been raised for this admission. [Desk: quote the other reference if the payer named one]"]
    else:
        lines += ["[Desk: answer the point raised in one or two sentences, with the supporting page]"]
    suggestion = _library_suggestion(db, q, case.payer.scheme)
    if suggestion:
        lines += ["", "--- Reply that cleared a similar query before ---", suggestion.reply_text]
    text = "\n".join(lines)
    if llm.enabled():
        try:
            res = llm.complete_json(
                "You polish replies from an Indian hospital insurance desk to a payer query. Keep every fact and "
                "every bracketed instruction. Plain, polite, short. JSON: {\"reply\": \"...\"}",
                llm.redact(f"Query: {q.text}\n\nDraft:\n{text}", [case.beneficiary_name]))
            if res.get("reply"):
                text = res["reply"]
        except Exception:
            pass
    q.draft_reply = text
    q.draft_documents = [d.id for d in attach]
    q.status = "awaiting_clinician" if q.category == "medical_necessity" and not q.clinician_signoff_at else "drafted"
    audit.record(db, hospital_id=q.hospital_id, actor=actor, action="query.drafted", entity="query", entity_id=q.id,
                 case_id=q.case_id, after={"status": q.status, "documents": q.draft_documents})
    db.flush()
    return q


def signoff(db: Session, q: Query, actor, text: str | None = None) -> Query:
    if text:
        q.draft_reply = text
    q.clinician_signoff_by = actor.id
    q.clinician_signoff_at = now_utc()
    q.status = "drafted"
    audit.record(db, hospital_id=q.hospital_id, actor=actor, action="query.clinician_signoff", entity="query",
                 entity_id=q.id, case_id=q.case_id)
    db.flush()
    return q


def reply(db: Session, q: Query, actor, reply_text: str, document_ids: list[str]) -> Query:
    if q.status in {"replied", "closed"}:
        raise ValueError("This query has already been answered.")
    if q.category == "medical_necessity" and not q.clinician_signoff_at:
        raise ValueError("A clinician must sign off a medical-necessity reply before it is sent.")
    if "[" in reply_text and "]" in reply_text and re.search(r"\[(desk|clinician)[^\]]*\]", reply_text, re.I):
        raise ValueError("The reply still has a bracketed instruction. Complete or remove it before sending.")
    q.reply_text = reply_text
    q.documents_sent = document_ids
    q.replied_at = now_utc()
    q.replied_by = actor.id
    q.status = "replied"
    case = db.get(Case, q.case_id)
    event = "reply_preauth_query" if q.stage == "preauth" else "reply_claim_query"
    if case.state in {"PREAUTH_QUERY", "CLAIM_QUERY"}:
        others_open = db.query(Query).filter(Query.case_id == case.id, Query.id != q.id,
                                             Query.status.in_(["open", "drafted", "awaiting_clinician"])).count()
        if not others_open:
            apply(db, case, event, actor, {"query_id": q.id})
    else:
        from . import clocks
        clocks.stop(db, case, ["preauth_query_reply", "claim_query_reply", "claim_query_cutoff"], query_id=q.id)
    audit.record(db, hospital_id=q.hospital_id, actor=actor, action="query.replied", entity="query", entity_id=q.id,
                 case_id=q.case_id, after={"documents": document_ids})
    db.flush()
    return q


def record_outcome(db: Session, q: Query, actor, outcome: str) -> Query:
    q.outcome = outcome
    q.status = "closed"
    case = db.get(Case, q.case_id)
    if outcome == "cleared" and q.reply_text:
        db.add(QueryLibraryEntry(hospital_id=q.hospital_id, scheme=case.payer.scheme, category=q.category,
                                 query_text=q.text, reply_text=q.reply_text, outcome=outcome))
    audit.record(db, hospital_id=q.hospital_id, actor=actor, action="query.outcome", entity="query", entity_id=q.id,
                 case_id=q.case_id, after={"outcome": outcome})
    db.flush()
    return q
