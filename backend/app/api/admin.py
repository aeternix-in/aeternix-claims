"""Sign-in, setup (hospital, payers, users, rate cards), reference data, reports, review queue."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from .. import audit, reporting, services
from ..adapters import ADAPTERS, all_selector_configs, selector_config
from ..config import settings
from ..db import get_db, scope_session
from ..documents import DOC_TYPES
from ..escalation import KIND_LABEL
from ..importer import import_outstanding
from ..models import AuditLog, Case, CheckResult, Hospital, Payer, Query, User
from ..money import parse_date
from ..notify import send
from ..queries import CATEGORIES
from ..rulepacks import all_packs, get_pack, rate_card_for, save_hospital_rate_card
from ..security import (ADMIN, ALL_ROLES, AETERNIX_ROLES, CLINICAL, Ctx, current_user, get_ctx, hash_password,
                        issue_token, require, verify_password)
from ..statemachine import LABELS, STATES, STATION, TRANSITIONS
from . import serial

router = APIRouter(prefix="/api", tags=["admin"])


# ------------------------------------------------------------------ auth

@router.post("/auth/login")
def login(body: dict = Body(...), db: Session = Depends(get_db)):
    u = db.query(User).filter(User.email == (body.get("email") or "").strip().lower()).first()
    if not u or not u.active or not verify_password(body.get("password") or "", u.password_hash):
        raise HTTPException(401, "Email or password is wrong.")
    scope_session(db, u.hospital_id)
    audit.record(db, hospital_id=u.hospital_id, actor=u, action="auth.login", entity="user", entity_id=u.id)
    db.commit()
    return {"token": issue_token(u.id), "user": serial.user(u)}


@router.get("/auth/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    ids = [user.hospital_id] if user.hospital_id else (user.hospital_ids or [])
    if user.role == "aeternix_admin" and not ids:
        ids = [h.id for h in db.query(Hospital).all()]
    hospitals = [{"id": h.id, "name": h.name, "code": h.code} for h in db.query(Hospital).filter(Hospital.id.in_(ids)).all()]
    return {"user": serial.user(user), "hospitals": hospitals, "demo_mode": settings.demo_mode}


# ------------------------------------------------------------------ reference data

@router.get("/meta")
def meta(_: User = Depends(current_user)):
    return {"states": [{"key": s, "label": LABELS[s], "station": STATION[s]} for s in STATES],
            "stations": ["Case", "Claim pack", "Query", "Payer", "Bank"],
            "transitions": [{"event": t.event, "label": t.label, "sources": sorted(t.sources), "target": t.target,
                             "system_only": t.system_only} for t in TRANSITIONS],
            "doc_types": DOC_TYPES, "query_categories": CATEGORIES, "letter_kinds": KIND_LABEL,
            "buckets": [{"key": k, "label": v[0], "controllable": v[1]} for k, v in reporting.BUCKETS.items()],
            "adapters": {k: a.name for k, a in ADAPTERS.items()}}


@router.get("/rulepacks")
def rulepacks(ctx: Ctx = Depends(get_ctx)):
    out = []
    for p in all_packs().values():
        _, meta = rate_card_for(p.key, ctx.hospital_id, date.today())
        out.append({"key": p.key, "name": p.name, "version": p.version, "portal": p.portal, "kind": p.kind,
                    "rate_card": meta})
    return out


@router.get("/rulepacks/{key}")
def rulepack(key: str, ctx: Ctx = Depends(get_ctx)):
    try:
        p = get_pack(key)
    except KeyError as e:
        raise HTTPException(404, str(e))
    entries, meta = rate_card_for(key, ctx.hospital_id, date.today())
    return {**p.model_dump(by_alias=True), "rate_card": meta,
            "rate_entries": [e.__dict__ for e in list(entries.values())[:500]]}


@router.post("/rulepacks/{key}/rate-card")
async def upload_rate_card(key: str, file: UploadFile = File(...), effective_from: str = Form(...),
                           ctx: Ctx = Depends(require(ADMIN)), db: Session = Depends(get_db)):
    eff = parse_date(effective_from)
    if not eff:
        raise HTTPException(422, "Enter the date the rates take effect.")
    try:
        get_pack(key)
        path = save_hospital_rate_card(ctx.hospital_id, key, eff, file.filename or "rates.csv",
                                       (await file.read()).decode("utf-8-sig", errors="replace"))
    except (KeyError, ValueError) as e:
        raise HTTPException(422, str(e))
    audit.record(db, hospital_id=ctx.hospital_id, actor=ctx.user, action="ratecard.uploaded", entity="rate_card",
                 entity_id=None, after={"scheme": key, "file": path.name})
    db.commit()
    return {"saved": path.name}


@router.get("/adapters")
def adapters(_: User = Depends(current_user)):
    return all_selector_configs()


@router.get("/adapters/{portal}")
def adapter(portal: str, _: User = Depends(current_user)):
    return selector_config(portal)


# ------------------------------------------------------------------ setup

@router.get("/hospital")
def hospital(ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    h = db.get(Hospital, ctx.hospital_id)
    return {"id": h.id, "name": h.name, "code": h.code, "registration_ids": h.registration_ids,
            "empanelments": h.empanelments, "bank_accounts": h.bank_accounts, "digest_recipients": h.digest_recipients}


@router.patch("/hospital")
def patch_hospital(body: dict = Body(...), ctx: Ctx = Depends(require(ADMIN)), db: Session = Depends(get_db)):
    h = db.get(Hospital, ctx.hospital_id)
    for f in ("name", "registration_ids", "empanelments", "bank_accounts", "digest_recipients"):
        if f in body:
            setattr(h, f, body[f])
    audit.record(db, hospital_id=h.id, actor=ctx.user, action="hospital.updated", entity="hospital", entity_id=h.id,
                 after={k: body[k] for k in body if k != "name"})
    db.commit()
    return {"ok": True}


@router.get("/payers")
def payers(ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    return [serial.payer(p) for p in db.query(Payer).filter(Payer.hospital_id == ctx.hospital_id).order_by(Payer.code).all()]


@router.post("/payers")
def add_payer(body: dict = Body(...), ctx: Ctx = Depends(require(ADMIN)), db: Session = Depends(get_db)):
    try:
        pack = get_pack(body.get("scheme", ""))
    except KeyError as e:
        raise HTTPException(422, str(e))
    p = Payer(hospital_id=ctx.hospital_id, code=body["code"].strip().upper(), name=body.get("name") or body["code"],
              scheme=pack.key, kind=pack.kind, portal=body.get("portal") or pack.portal,
              expected_tds_bps=body.get("expected_tds_bps"), narration_keywords=body.get("narration_keywords") or [],
              escalation_contacts=body.get("escalation_contacts") or [])
    db.add(p)
    audit.record(db, hospital_id=ctx.hospital_id, actor=ctx.user, action="payer.added", entity="payer", entity_id=p.id,
                 after={"code": p.code, "scheme": p.scheme})
    db.commit()
    return serial.payer(p)


@router.patch("/payers/{pid}")
def edit_payer(pid: str, body: dict = Body(...), ctx: Ctx = Depends(require(ADMIN)), db: Session = Depends(get_db)):
    p = db.query(Payer).filter(Payer.hospital_id == ctx.hospital_id, Payer.id == pid).first()
    if not p:
        raise HTTPException(404, "Payer not found.")
    for f in ("name", "portal", "expected_tds_bps", "narration_keywords", "escalation_contacts", "active"):
        if f in body:
            setattr(p, f, body[f])
    audit.record(db, hospital_id=ctx.hospital_id, actor=ctx.user, action="payer.updated", entity="payer",
                 entity_id=p.id, after=body)
    db.commit()
    return serial.payer(p)


@router.get("/users")
def users(ctx: Ctx = Depends(require(ADMIN)), db: Session = Depends(get_db)):
    rows = db.query(User).filter(User.hospital_id == ctx.hospital_id).all()
    return [serial.user(u) for u in rows]


@router.post("/users")
def add_user(body: dict = Body(...), ctx: Ctx = Depends(require(ADMIN)), db: Session = Depends(get_db)):
    role = body.get("role")
    if role not in ALL_ROLES or (role in AETERNIX_ROLES and ctx.role != "aeternix_admin"):
        raise HTTPException(422, "Choose a valid role.")
    if len(body.get("password") or "") < 10:
        raise HTTPException(422, "Passwords need at least 10 characters.")
    email = (body.get("email") or "").strip().lower()
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(409, "That email already has an account.")
    u = User(email=email, name=body.get("name") or email, role=role, password_hash=hash_password(body["password"]),
             hospital_id=None if role in AETERNIX_ROLES else ctx.hospital_id,
             hospital_ids=[ctx.hospital_id] if role in AETERNIX_ROLES else [])
    db.add(u)
    audit.record(db, hospital_id=ctx.hospital_id, actor=ctx.user, action="user.added", entity="user", entity_id=u.id,
                 after={"email": email, "role": role})
    db.commit()
    return serial.user(u)


@router.post("/import/outstanding")
async def import_list(file: UploadFile = File(...), default_payer_id: str | None = Form(None),
                      ctx: Ctx = Depends(require(ADMIN | {"desk", "owner", "accountant"})), db: Session = Depends(get_db)):
    default = db.query(Payer).filter(Payer.hospital_id == ctx.hospital_id, Payer.id == default_payer_id).first() \
        if default_payer_id else None
    res = import_outstanding(db, ctx.hospital_id, file.filename or "list.csv", await file.read(), ctx.user, default,
                             services.next_case_no)
    db.commit()
    return res


# ------------------------------------------------------------------ reports

@router.get("/worklist")
def worklist(ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    return reporting.worklist(db, ctx.hospital_id, ctx.role)


@router.get("/dashboard")
def dashboard(ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    return reporting.dashboard(db, ctx.hospital_id)


@router.get("/scorecard")
def scorecard(month: str | None = None, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    return reporting.scorecard(db, ctx.hospital_id, month)


@router.get("/metrics/pilot")
def pilot(since: str | None = None, until: str | None = None, ctx: Ctx = Depends(get_ctx),
          db: Session = Depends(get_db)):
    return reporting.pilot_metrics(db, ctx.hospital_id, parse_date(since), parse_date(until))


@router.get("/digest/preview")
def digest_preview(ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    h = db.get(Hospital, ctx.hospital_id)
    return {"text": reporting.digest_text(db, h), "recipients": h.digest_recipients}


@router.post("/digest/send")
def digest_send(ctx: Ctx = Depends(require(ADMIN | {"owner"})), db: Session = Depends(get_db)):
    h = db.get(Hospital, ctx.hospital_id)
    text = reporting.digest_text(db, h)
    results = [send(r.get("channel", "console"), r.get("to", ""), f"Aeternix digest: {h.name}", text)
               for r in (h.digest_recipients or [{"channel": "console", "to": "console"}])]
    return {"results": results}


@router.get("/review")
def review_queue(ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    """Clinical review: amber flags that need a clinician, and medical-necessity replies awaiting sign-off."""
    from ..statemachine import latest_run_status
    flags = []
    for c in db.query(Case).filter(Case.hospital_id == ctx.hospital_id,
                                   Case.state.in_(["DISCHARGED", "READY_TO_SUBMIT", "LATE_CONDONATION"])).all():
        _, rows = latest_run_status(db, c)
        for r in rows:
            if r.result == "amber" and r.needs_clinician and not r.cleared_at:
                flags.append({**serial.check(r), "case_id": c.id, "case_no": c.case_no, "payer": c.payer.code,
                              "procedure": c.planned_procedure, "icd10": c.icd10})
    qs = db.query(Query).filter(Query.hospital_id == ctx.hospital_id, Query.status == "awaiting_clinician").all()
    replies = []
    for q in qs:
        c = db.get(Case, q.case_id)
        replies.append({**serial.query(q), "case_no": c.case_no, "payer": c.payer.code})
    return {"flags": flags, "replies": replies}


@router.get("/audit")
def audit_log(case_id: str | None = None, limit: int = 200, ctx: Ctx = Depends(require(ADMIN | {"owner"} | CLINICAL)),
              db: Session = Depends(get_db)):
    q = db.query(AuditLog).filter(AuditLog.hospital_id == ctx.hospital_id)
    if case_id:
        q = q.filter(AuditLog.case_id == case_id)
    return [{"at": serial.iso(a.created_at), "actor": a.actor_name, "action": a.action, "entity": a.entity,
             "entity_id": a.entity_id, "case_id": a.case_id, "after": a.after}
            for a in q.order_by(AuditLog.created_at.desc()).limit(min(limit, 1000)).all()]


__all__ = ["router", "CheckResult"]
