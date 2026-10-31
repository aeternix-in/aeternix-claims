"""Payer tracker, inbound email, bank reconciliation, payment advices, escalations."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from .. import audit, escalation, recon, tracker
from ..db import get_db
from ..models import Allocation, BankTransaction, Case, Escalation, InboundEmail, PaymentAdvice, StatusSnapshot
from ..money import now_utc, parse_date, to_paise
from ..queries import raise_query
from ..security import BANK_WRITE, DESK_WRITE, Ctx, get_ctx, require
from ..statemachine import TransitionError, apply
from . import serial

router = APIRouter(prefix="/api", tags=["money"])


# ------------------------------------------------------------------ tracker

@router.post("/tracker/snapshots")
def snapshots(body: dict = Body(...), ctx: Ctx = Depends(require(DESK_WRITE)), db: Session = Depends(get_db)):
    portal = body.get("portal") or ""
    rows = body.get("rows") or []
    out = []
    for row in rows[:500]:
        if not row.get("portal_ref"):
            continue
        s = tracker.ingest_snapshot(db, ctx.hospital_id, portal, row, ctx.user)
        out.append({"portal_ref": s.portal_ref, "case_id": s.case_id, "changed": s.changed, "applied": s.applied})
    db.commit()
    return {"seen": len(out), "changed": sum(1 for r in out if r["changed"]),
            "unlinked": sum(1 for r in out if not r["case_id"]), "rows": out}


@router.get("/tracker/unlinked")
def unlinked(ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    rows = (db.query(StatusSnapshot).filter(StatusSnapshot.hospital_id == ctx.hospital_id,
                                            StatusSnapshot.case_id.is_(None))
            .order_by(StatusSnapshot.created_at.desc()).limit(200).all())
    seen, out = set(), []
    for r in rows:
        if r.portal_ref in seen:
            continue
        seen.add(r.portal_ref)
        out.append({"id": r.id, "portal": r.portal, "portal_ref": r.portal_ref, "status_text": r.status_text,
                    "seen_at": serial.iso(r.created_at)})
    return out


@router.post("/tracker/snapshots/{sid}/link")
def link_snapshot(sid: str, body: dict = Body(...), ctx: Ctx = Depends(require(DESK_WRITE)),
                  db: Session = Depends(get_db)):
    s = db.query(StatusSnapshot).filter(StatusSnapshot.hospital_id == ctx.hospital_id, StatusSnapshot.id == sid).first()
    case = db.query(Case).filter(Case.hospital_id == ctx.hospital_id, Case.id == body.get("case_id")).first()
    if not s or not case:
        raise HTTPException(404, "Not found.")
    case.portal_case_id = case.portal_case_id or s.portal_ref
    s.case_id = case.id
    s.applied = tracker.apply_snapshot(db, case, s, ctx.user)
    db.commit()
    return {"applied": s.applied}


# ------------------------------------------------------------------ inbound email

@router.post("/inbound/email")
async def inbound_email(raw: str | None = Form(None), subject: str = Form(""), sender: str = Form(""),
                        file: UploadFile | None = File(None), ctx: Ctx = Depends(require(DESK_WRITE)),
                        db: Session = Depends(get_db)):
    data = await file.read() if file else (raw or "")
    if not data:
        raise HTTPException(422, "Paste the email or attach the .eml file.")
    row = tracker.ingest_email(db, ctx.hospital_id, data, ctx.user, subject, sender)
    db.commit()
    return {"id": row.id, "case_id": row.case_id, "parsed": row.parsed}


@router.get("/inbound/emails")
def emails(ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    rows = (db.query(InboundEmail).filter(InboundEmail.hospital_id == ctx.hospital_id)
            .order_by(InboundEmail.created_at.desc()).limit(100).all())
    return [{"id": r.id, "subject": r.subject, "sender": r.sender, "case_id": r.case_id, "status": r.status,
             "parsed": r.parsed, "received_at": serial.iso(r.created_at)} for r in rows]


@router.post("/inbound/emails/{eid}/apply")
def apply_email(eid: str, body: dict = Body(default={}), ctx: Ctx = Depends(require(DESK_WRITE)),
                db: Session = Depends(get_db)):
    r = db.query(InboundEmail).filter(InboundEmail.hospital_id == ctx.hospital_id, InboundEmail.id == eid).first()
    if not r or not r.case_id:
        raise HTTPException(404, "Email not linked to a case.")
    case = db.get(Case, r.case_id)
    sugg = (r.parsed.get("suggestions") or [None])[int(body.get("index", 0))]
    if not sugg:
        raise HTTPException(422, "Nothing to apply.")
    try:
        if sugg["action"] == "raise_query":
            raise_query(db, case, sugg["text"], ctx.user, source="email")
        elif sugg["action"] == "approve":
            apply(db, case, "approve", ctx.user, {"approved_amount": (sugg.get("approved_amount_paise") or 0) / 100,
                                                   "source": "email"})
        elif sugg["action"] == "payer_paid":
            data = {"payment_ref": sugg["payment_ref"], "source": "email"}
            if sugg.get("amount_paise"):
                data["amount"] = sugg["amount_paise"] / 100
            apply(db, case, "payer_paid", ctx.user, data)
        elif sugg["action"] == "reject":
            apply(db, case, "reject", ctx.user, {"reason": sugg.get("reason") or "See email", "source": "email"})
    except (TransitionError, ValueError) as e:
        db.rollback()
        raise HTTPException(409, str(e))
    r.status = "applied"
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ bank

@router.post("/bank/statements")
async def bank_statement(file: UploadFile = File(...), account: str = Form("Main"),
                         ctx: Ctx = Depends(require(BANK_WRITE)), db: Session = Depends(get_db)):
    data = await file.read()
    try:
        res = recon.import_statement(db, ctx.hospital_id, account.strip() or "Main", file.filename or "statement.csv",
                                     data, ctx.user)
    except ValueError as e:
        db.rollback()
        raise HTTPException(422, str(e))
    db.commit()
    return res


@router.get("/bank/transactions")
def bank_txns(status: str | None = None, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    q = db.query(BankTransaction).filter(BankTransaction.hospital_id == ctx.hospital_id)
    if status:
        q = q.filter(BankTransaction.status.in_(status.split(",")))
    return [serial.bank_txn(db, t) for t in q.order_by(BankTransaction.txn_date.desc()).limit(500).all()]


@router.post("/bank/match")
def bank_match(ctx: Ctx = Depends(require(BANK_WRITE)), db: Session = Depends(get_db)):
    res = recon.match_all(db, ctx.hospital_id, ctx.user)
    db.commit()
    return res


@router.post("/bank/allocations/{aid}/{action}")
def allocation_action(aid: str, action: str, ctx: Ctx = Depends(require(BANK_WRITE)), db: Session = Depends(get_db)):
    a = db.query(Allocation).filter(Allocation.hospital_id == ctx.hospital_id, Allocation.id == aid).first()
    if not a:
        raise HTTPException(404, "Match not found.")
    try:
        if action == "confirm":
            recon.confirm(db, a, ctx.user)
        elif action == "reject":
            recon.reject(db, a, ctx.user)
        else:
            raise HTTPException(404, "Unknown action.")
    except ValueError as e:
        raise HTTPException(409, str(e))
    db.commit()
    return serial.bank_txn(db, db.get(BankTransaction, a.bank_txn_id))


@router.post("/bank/transactions/{tid}/allocate")
def allocate(tid: str, body: dict = Body(...), ctx: Ctx = Depends(require(BANK_WRITE)), db: Session = Depends(get_db)):
    t = db.query(BankTransaction).filter(BankTransaction.hospital_id == ctx.hospital_id, BankTransaction.id == tid).first()
    if not t:
        raise HTTPException(404, "Credit not found.")
    try:
        recon.allocate_manual(db, t, body.get("lines") or [], ctx.user)
    except ValueError as e:
        db.rollback()
        raise HTTPException(422, str(e))
    db.commit()
    return serial.bank_txn(db, t)


@router.post("/bank/transactions/{tid}/ignore")
def ignore(tid: str, body: dict = Body(default={}), ctx: Ctx = Depends(require(BANK_WRITE)),
           db: Session = Depends(get_db)):
    t = db.query(BankTransaction).filter(BankTransaction.hospital_id == ctx.hospital_id, BankTransaction.id == tid).first()
    if not t:
        raise HTTPException(404, "Credit not found.")
    t.status, t.match_note = "ignored", body.get("note") or "Not a claim payment"
    audit.record(db, hospital_id=t.hospital_id, actor=ctx.user, action="bank.ignored", entity="bank_txn",
                 entity_id=t.id, after={"note": t.match_note})
    db.commit()
    return serial.bank_txn(db, t)


@router.get("/bank/candidates")
def candidates(ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    out = []
    for c in recon._open_cases(db, ctx.hospital_id):
        out.append({"case_id": c.id, "case_no": c.case_no, "payer": c.payer.code, "state": c.state,
                    "expected_net_paise": recon.expected_net(db, c), "portal_case_id": c.portal_case_id})
    return out


@router.post("/payments/advices")
def payment_advice(body: dict = Body(...), ctx: Ctx = Depends(require(BANK_WRITE | DESK_WRITE)),
                   db: Session = Depends(get_db)):
    lines = []
    for l in body.get("lines") or []:
        c = db.query(Case).filter(Case.hospital_id == ctx.hospital_id, Case.id == l.get("case_id")).first()
        if not c:
            raise HTTPException(422, "A line points to an unknown case.")
        lines.append({"case_id": c.id, "amount_paise": to_paise(l.get("amount")) or 0,
                      "tds_paise": to_paise(l.get("tds")) or 0})
    if not body.get("reference_no") or not lines:
        raise HTTPException(422, "Enter the payment reference and at least one claim.")
    paid_on = parse_date(body.get("paid_on")) or now_utc().date()
    adv = PaymentAdvice(hospital_id=ctx.hospital_id,
                        payer_id=body.get("payer_id") or db.get(Case, lines[0]["case_id"]).payer_id,
                        reference_no=body["reference_no"].strip(), amount_paise=sum(l["amount_paise"] for l in lines),
                        paid_on=paid_on, lines=lines, source="manual")
    db.add(adv)
    moved = []
    for l in lines:
        c = db.get(Case, l["case_id"])
        if c.state in {"APPROVED", "PART_APPROVED", "DISPUTED"}:
            apply(db, c, "payer_paid", ctx.user, {"payment_ref": adv.reference_no, "paid_on": paid_on.isoformat(),
                                                  "amount": l["amount_paise"] / 100, "advice_recorded": True})
            moved.append(c.case_no)
    db.commit()
    return {"id": adv.id, "cases_marked_paid": moved}


# ------------------------------------------------------------------ escalations

@router.get("/escalations")
def list_escalations(status: str | None = None, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    q = db.query(Escalation).filter(Escalation.hospital_id == ctx.hospital_id)
    if status:
        q = q.filter(Escalation.status.in_(status.split(",")))
    out = []
    for e in q.order_by(Escalation.created_at.desc()).limit(300).all():
        c = db.get(Case, e.case_id)
        out.append({**serial.escalation(e), "case_no": c.case_no, "payer": c.payer.code,
                    "kind_label": escalation.KIND_LABEL.get(e.kind, e.kind)})
    return out


@router.post("/escalations/sweep")
def sweep(ctx: Ctx = Depends(require(DESK_WRITE)), db: Session = Depends(get_db)):
    made = escalation.sweep(db, ctx.hospital_id)
    db.commit()
    return {"drafted": len(made)}


def _esc(db, ctx, eid) -> Escalation:
    e = db.query(Escalation).filter(Escalation.hospital_id == ctx.hospital_id, Escalation.id == eid).first()
    if not e:
        raise HTTPException(404, "Letter not found.")
    return e


@router.patch("/escalations/{eid}")
def edit_escalation(eid: str, body: dict = Body(...), ctx: Ctx = Depends(require(DESK_WRITE)),
                    db: Session = Depends(get_db)):
    e = _esc(db, ctx, eid)
    for f in ("subject", "body", "addressed_to"):
        if f in body:
            setattr(e, f, body[f])
    db.commit()
    return serial.escalation(e)


@router.post("/escalations/{eid}/sent")
def sent(eid: str, body: dict = Body(default={}), ctx: Ctx = Depends(require(DESK_WRITE)), db: Session = Depends(get_db)):
    e = _esc(db, ctx, eid)
    try:
        escalation.mark_sent(db, e, ctx.user, body.get("reference_no"), body.get("body"))
    except ValueError as ex:
        raise HTTPException(409, str(ex))
    db.commit()
    return serial.escalation(e)


@router.post("/escalations/{eid}/response")
def response(eid: str, body: dict = Body(...), ctx: Ctx = Depends(require(DESK_WRITE)), db: Session = Depends(get_db)):
    e = _esc(db, ctx, eid)
    e.response = body.get("response")
    e.status = "closed" if body.get("close") else "responded"
    audit.record(db, hospital_id=e.hospital_id, actor=ctx.user, action="escalation.response", entity="escalation",
                 entity_id=e.id, case_id=e.case_id, after={"status": e.status})
    db.commit()
    return serial.escalation(e)
