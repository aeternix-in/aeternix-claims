"""M6 Payer tracker: what the extension reads on portal pages, and what arrives by email.

The extension sends exactly what it saw. This module decides what it means:
match it to a case, map the portal's words to a state change, and apply it only when
the move is valid from where the case is. Anything it cannot place is kept for a
person to link.
"""
from __future__ import annotations

import email
import re
from email import policy

from sqlalchemy.orm import Session

from . import audit
from .adapters import get_adapter
from .models import Case, Claim, InboundEmail, Payer, StatusSnapshot
from .money import days_between, now_utc, parse_date, to_paise
from .queries import raise_query
from .rulepacks import get_pack
from .statemachine import LABELS, OPEN_STATES, TransitionError, apply


def find_case(db: Session, hospital_id: str, ref: str) -> Case | None:
    ref = (ref or "").strip()
    if not ref:
        return None
    c = db.query(Case).filter(Case.hospital_id == hospital_id,
                              (Case.portal_case_id == ref) | (Case.case_no == ref)).first()
    if c:
        return c
    cl = db.query(Claim).filter(Claim.hospital_id == hospital_id, Claim.portal_ref == ref).first()
    return db.get(Case, cl.case_id) if cl else None


def ingest_snapshot(db: Session, hospital_id: str, portal: str, row: dict, actor) -> StatusSnapshot:
    ref = str(row.get("portal_ref") or "").strip()
    snap = StatusSnapshot(
        hospital_id=hospital_id, portal=portal, portal_ref=ref, status_text=str(row.get("status_text") or "")[:300],
        approved_paise=to_paise(row.get("approved_amount")), deduction_paise=to_paise(row.get("deduction_amount")),
        payment_ref=(str(row.get("payment_ref")).strip() or None) if row.get("payment_ref") else None,
        payment_date=parse_date(row.get("payment_date")), query_text=row.get("query_text") or None,
        raw=row, seen_by=getattr(actor, "id", None))
    case = find_case(db, hospital_id, ref)
    snap.case_id = case.id if case else None
    last = (db.query(StatusSnapshot).filter(StatusSnapshot.hospital_id == hospital_id, StatusSnapshot.portal == portal,
                                            StatusSnapshot.portal_ref == ref)
            .order_by(StatusSnapshot.created_at.desc()).first())
    snap.changed = (last is None or last.status_text != snap.status_text or last.approved_paise != snap.approved_paise
                    or last.payment_ref != snap.payment_ref)
    db.add(snap)
    if case and snap.changed:
        snap.applied = apply_snapshot(db, case, snap, actor)
    db.flush()
    return snap


def apply_snapshot(db: Session, case: Case, snap: StatusSnapshot, actor) -> str:
    adapter = get_adapter(snap.portal)
    semantic, event = adapter.event_for(snap.status_text, case.state)
    snap.mapped_event = event or semantic
    if not event:
        return f"Recorded; no change needed ({semantic or 'unrecognised status'})."
    try:
        if event in {"raise_preauth_query", "raise_claim_query"}:
            raise_query(db, case, snap.query_text or f"Portal shows: {snap.status_text}", actor,
                        stage="preauth" if event == "raise_preauth_query" else "claim", source="portal")
        elif event in {"approve", "part_approve"}:
            data = {"source": "portal"}
            if snap.approved_paise is not None:
                data["approved_amount"] = snap.approved_paise / 100
            if snap.deduction_paise:
                data["deductions"] = [{"reason": "Deduction shown on portal", "amount": snap.deduction_paise / 100}]
            apply(db, case, event, actor, data)
        elif event == "payer_paid":
            if not snap.payment_ref:
                return "Portal shows paid but no payment reference was read; add it by hand."
            data = {"payment_ref": snap.payment_ref, "source": "portal"}
            if snap.payment_date:
                data["paid_on"] = snap.payment_date.isoformat()
            apply(db, case, event, actor, data)
        elif event in {"reject", "reject_preauth"}:
            apply(db, case, event, actor, {"reason": snap.query_text or snap.status_text, "source": "portal"})
        else:
            apply(db, case, event, actor, {"source": "portal"})
    except (TransitionError, ValueError) as e:
        return f"Not applied: {e}"
    return f"Case {case.case_no} is now '{LABELS[case.state]}'."


def stalled_cases(db: Session, hospital_id: str) -> list[tuple[Case, int, int]]:
    """(case, days in state, limit) for cases sitting in a state longer than the rule pack allows."""
    out = []
    now = now_utc()
    for case in db.query(Case).filter(Case.hospital_id == hospital_id, Case.state.in_(OPEN_STATES)).all():
        limit = get_pack(case.payer.scheme).stall_days.get(case.state)
        if limit is None:
            continue
        days = days_between(case.state_changed_at, now)
        if days > limit:
            out.append((case, days, limit))
    return out


# ------------------------------------------------------------------ email

_UTR = re.compile(r"\b(?:UTR|ref(?:erence)?|txn|transaction)\s*(?:no\.?|number|id)?\s*[:#-]?\s*([A-Z0-9]{10,22})\b", re.I)
_AMOUNT = re.compile(r"(?:rs\.?|inr|₹)\s*([\d,]+(?:\.\d{1,2})?)", re.I)


def parse_email(raw: str | bytes, subject: str = "", sender: str = "") -> dict:
    if isinstance(raw, bytes) or (raw and raw.lstrip().lower().startswith(("from:", "received:", "subject:", "mime"))):
        msg = email.message_from_bytes(raw if isinstance(raw, bytes) else raw.encode(), policy=policy.default)
        subject = subject or str(msg.get("subject", ""))
        sender = sender or str(msg.get("from", ""))
        part = msg.get_body(preferencelist=("plain", "html"))
        body = part.get_content() if part else ""
        body = re.sub(r"<[^>]+>", " ", body)
    else:
        body = raw or ""
    text = f"{subject}\n{body}"
    low = text.lower()
    kind = ("settlement" if re.search(r"settle|payment\s+(advice|released|made)|utr|credited|remittance", low) else
            "query" if re.search(r"\bquer(y|ies)\b|deficien|clarification|additional\s+documents", low) else
            "approval" if re.search(r"approv|authori[sz]ation\s+letter|sanction", low) else
            "rejection" if re.search(r"reject|denial|repudiat", low) else "other")
    utr = _UTR.search(text)
    amounts = [to_paise(a) for a in _AMOUNT.findall(text)]
    return {"subject": subject, "sender": sender, "body": body.strip(), "kind": kind,
            "payment_ref": utr.group(1) if utr else None, "amounts_paise": [a for a in amounts if a],
            "candidate_refs": sorted(set(re.findall(r"\b[A-Z]{2,6}[/-]?\d{2,}[/-]?\d{2,}[A-Z0-9/-]*\b", text)))[:20]}


def ingest_email(db: Session, hospital_id: str, raw: str | bytes, actor, subject: str = "", sender: str = "") -> InboundEmail:
    parsed = parse_email(raw, subject, sender)
    case = None
    for ref in parsed["candidate_refs"]:
        case = find_case(db, hospital_id, ref)
        if case:
            break
    if case is None:
        # fall back to any known portal reference appearing anywhere in the text
        refs = {c.portal_case_id: c for c in db.query(Case).filter(Case.hospital_id == hospital_id,
                                                                   Case.portal_case_id.isnot(None)).all()}
        for ref, c in refs.items():
            if ref and ref in parsed["subject"] + parsed["body"]:
                case = c
                break
    row = InboundEmail(hospital_id=hospital_id, sender=parsed["sender"][:300], subject=parsed["subject"][:500],
                       body=parsed["body"][:20000], parsed={k: v for k, v in parsed.items() if k != "body"},
                       case_id=case.id if case else None)
    db.add(row)
    suggestions = []
    if case:
        if parsed["kind"] == "query":
            suggestions.append({"action": "raise_query", "text": parsed["body"][:1500]})
        elif parsed["kind"] == "approval":
            suggestions.append({"action": "approve", "approved_amount_paise": max(parsed["amounts_paise"] or [0])})
        elif parsed["kind"] == "settlement" and parsed["payment_ref"]:
            suggestions.append({"action": "payer_paid", "payment_ref": parsed["payment_ref"],
                                "amount_paise": max(parsed["amounts_paise"] or [0]) or None})
        elif parsed["kind"] == "rejection":
            suggestions.append({"action": "reject", "reason": parsed["subject"]})
    row.parsed = {**row.parsed, "suggestions": suggestions}
    audit.record(db, hospital_id=hospital_id, actor=actor, action="email.received", entity="inbound_email",
                 entity_id=row.id, case_id=row.case_id, after={"kind": parsed["kind"], "matched": bool(case)})
    db.flush()
    return row


def payer_for_portal(db: Session, hospital_id: str, portal: str) -> list[Payer]:
    return db.query(Payer).filter(Payer.hospital_id == hospital_id, Payer.portal == portal).all()
