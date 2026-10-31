"""Desk routes: cases, documents, claim check, claim pack, portal actions, queries."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query as Q, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from .. import audit, clocks, packs, queries, scrubber, services
from ..db import get_db
from ..fhir import bundle
from ..models import Case, CheckResult, DeskActivity, Document, Query
from ..money import now_utc, parse_datetime_ist
from ..rulepacks import get_pack
from ..security import CLINICAL, DESK_WRITE, Ctx, get_ctx, require
from ..statemachine import TransitionError, apply
from . import serial

router = APIRouter(prefix="/api", tags=["desk"])


def _case(db: Session, ctx: Ctx, case_id: str) -> Case:
    c = db.query(Case).filter(Case.hospital_id == ctx.hospital_id, Case.id == case_id).first()
    if not c:
        raise HTTPException(404, "Case not found.")
    return c


def _doc(db: Session, ctx: Ctx, doc_id: str) -> Document:
    d = db.query(Document).filter(Document.hospital_id == ctx.hospital_id, Document.id == doc_id).first()
    if not d:
        raise HTTPException(404, "Document not found.")
    return d


def _query(db: Session, ctx: Ctx, qid: str) -> Query:
    q = db.query(Query).filter(Query.hospital_id == ctx.hospital_id, Query.id == qid).first()
    if not q:
        raise HTTPException(404, "Query not found.")
    return q


def _done(db: Session, case: Case) -> dict:
    db.commit()
    db.refresh(case)
    return serial.case_detail(db, case)


# ----------------------------------------------------------------------- cases

@router.get("/cases")
def list_cases(state: str | None = None, payer: str | None = None, q: str | None = None, bucket: str | None = None,
               open_only: bool = True, limit: int = Q(200, le=1000), ctx: Ctx = Depends(get_ctx),
               db: Session = Depends(get_db)):
    query = db.query(Case).filter(Case.hospital_id == ctx.hospital_id)
    if state:
        query = query.filter(Case.state.in_(state.split(",")))
    elif open_only:
        query = query.filter(Case.state != "CLOSED")
    if payer:
        query = query.filter(Case.payer_id == payer)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter((Case.case_no.ilike(like)) | (Case.beneficiary_name.ilike(like)) |
                             (Case.portal_case_id.ilike(like)) | (Case.beneficiary_ref.ilike(like)))
    rows = [serial.case_summary(db, c) for c in query.order_by(Case.admitted_at.desc()).limit(limit).all()]
    if bucket:
        rows = [r for r in rows if bucket in r["buckets"]]
    return rows


@router.post("/cases")
def create_case(body: dict = Body(...), ctx: Ctx = Depends(require(DESK_WRITE)), db: Session = Depends(get_db)):
    try:
        case = services.create_case(db, ctx.hospital_id, body, ctx.user)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return _done(db, case)


@router.get("/cases/{case_id}")
def get_case(case_id: str, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    return serial.case_detail(db, _case(db, ctx, case_id))


@router.patch("/cases/{case_id}")
def patch_case(case_id: str, body: dict = Body(...), ctx: Ctx = Depends(require(DESK_WRITE)),
               db: Session = Depends(get_db)):
    case = _case(db, ctx, case_id)
    services.update_case(db, case, body, ctx.user)
    return _done(db, case)


@router.post("/cases/{case_id}/portal-registered")
def portal_registered(case_id: str, body: dict = Body(default={}), ctx: Ctx = Depends(require(DESK_WRITE)),
                      db: Session = Depends(get_db)):
    case = _case(db, ctx, case_id)
    at = parse_datetime_ist(body.get("at")) or now_utc()
    case.portal_registered_at = at
    if body.get("portal_case_id"):
        case.portal_case_id = body["portal_case_id"]
    clocks.on_portal_registered(db, case, get_pack(case.payer.scheme), at)
    audit.record(db, hospital_id=case.hospital_id, actor=ctx.user, action="case.portal_registered", entity="case",
                 entity_id=case.id, case_id=case.id, after={"at": at, "portal_case_id": case.portal_case_id})
    return _done(db, case)


@router.post("/cases/{case_id}/counter-checks")
def counter_check(case_id: str, body: dict = Body(...), ctx: Ctx = Depends(require(DESK_WRITE)),
                  db: Session = Depends(get_db)):
    case = _case(db, ctx, case_id)
    keys = {cc.key for cc in get_pack(case.payer.scheme).counter_checks}
    key = body.get("key")
    if key not in keys:
        raise HTTPException(422, "This scheme has no such counter check.")
    case.counter_checks = {**(case.counter_checks or {}), key: now_utc().isoformat()}
    audit.record(db, hospital_id=case.hospital_id, actor=ctx.user, action="case.counter_check", entity="case",
                 entity_id=case.id, case_id=case.id, after={"check": key})
    return _done(db, case)


@router.post("/cases/{case_id}/actions/{event}")
def case_action(case_id: str, event: str, body: dict = Body(default={}), ctx: Ctx = Depends(require(DESK_WRITE)),
                db: Session = Depends(get_db)):
    case = _case(db, ctx, case_id)
    try:
        apply(db, case, event, ctx.user, body)
    except TransitionError as e:
        db.rollback()
        raise HTTPException(409, str(e))
    return _done(db, case)


@router.post("/cases/{case_id}/activity")
def activity(case_id: str, body: dict = Body(...), ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    case = _case(db, ctx, case_id)
    secs = int(body.get("seconds") or 0)
    if 0 < secs <= 300 and ctx.role in {"desk", "owner", "hospital_admin"}:
        db.add(DeskActivity(hospital_id=case.hospital_id, case_id=case.id, user_id=ctx.user_id, seconds=secs))
        db.commit()
    return {"ok": True}


@router.get("/cases/{case_id}/fhir")
def case_fhir(case_id: str, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    return bundle(db, _case(db, ctx, case_id))


@router.get("/cases/{case_id}/pack")
def case_pack(case_id: str, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    case = _case(db, ctx, case_id)
    data = packs.build_zip(db, case)
    audit.record(db, hospital_id=case.hospital_id, actor=ctx.user, action="pack.downloaded", entity="case",
                 entity_id=case.id, case_id=case.id)
    db.commit()
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{case.case_no}_claim_pack.zip"'})


@router.get("/cases/{case_id}/prefill")
def case_prefill(case_id: str, portal: str | None = None, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    return packs.prefill(db, _case(db, ctx, case_id), portal)


@router.post("/cases/{case_id}/check")
def run_check(case_id: str, ctx: Ctx = Depends(require(DESK_WRITE | CLINICAL)), db: Session = Depends(get_db)):
    case = _case(db, ctx, case_id)
    scrubber.run(db, case, ctx.user)
    return _done(db, case)


@router.post("/checks/{check_id}/clear")
def clear_check(check_id: str, body: dict = Body(...), ctx: Ctx = Depends(require(DESK_WRITE | CLINICAL)),
                db: Session = Depends(get_db)):
    r = db.query(CheckResult).filter(CheckResult.hospital_id == ctx.hospital_id, CheckResult.id == check_id).first()
    if not r:
        raise HTTPException(404, "Check not found.")
    if r.result != "amber":
        raise HTTPException(409, "Only amber flags can be cleared. Fix red flags and run the check again.")
    if r.needs_clinician and ctx.role not in CLINICAL:
        raise HTTPException(403, "A clinician must clear this flag.")
    note = (body.get("note") or "").strip()
    if len(note) < 5:
        raise HTTPException(422, "Write a short note saying why this is acceptable.")
    r.cleared_by, r.cleared_at, r.clear_note = ctx.user_id, now_utc(), note
    audit.record(db, hospital_id=r.hospital_id, actor=ctx.user, action="check.cleared", entity="check",
                 entity_id=r.id, case_id=r.case_id, after={"rule": r.rule, "note": note})
    return _done(db, _case(db, ctx, r.case_id))


# ------------------------------------------------------------------- documents

@router.post("/cases/{case_id}/documents")
async def upload_documents(case_id: str, files: list[UploadFile] = File(...), source: str = Form("upload"),
                           doc_type: str | None = Form(None), ctx: Ctx = Depends(require(DESK_WRITE)),
                           db: Session = Depends(get_db)):
    case = _case(db, ctx, case_id)
    added = []
    for f in files:
        data = await f.read()
        try:
            added.append(services.ingest_document(db, case, f.filename or "upload", data, ctx.user, source, doc_type))
        except ValueError as e:
            raise HTTPException(422, str(e))
    db.commit()
    return {"documents": [serial.document(d) for d in added], "case": serial.case_detail(db, case)}


@router.get("/documents/{doc_id}")
def get_document(doc_id: str, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    return serial.document(_doc(db, ctx, doc_id), with_text=True)


@router.get("/documents/{doc_id}/file")
def document_file(doc_id: str, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    from ..documents import load_file
    d = _doc(db, ctx, doc_id)
    return Response(load_file(d.storage_key), media_type=d.mime,
                    headers={"Content-Disposition": f'inline; filename="{d.filename}"'})


@router.post("/documents/{doc_id}/verify")
def verify_document(doc_id: str, body: dict = Body(...), ctx: Ctx = Depends(require(DESK_WRITE | CLINICAL)),
                    db: Session = Depends(get_db)):
    d = _doc(db, ctx, doc_id)
    services.verify_document(db, d, body.get("fields") or {}, ctx.user)
    return _done(db, _case(db, ctx, d.case_id))


@router.patch("/documents/{doc_id}")
def patch_document(doc_id: str, body: dict = Body(...), ctx: Ctx = Depends(require(DESK_WRITE)),
                   db: Session = Depends(get_db)):
    d = _doc(db, ctx, doc_id)
    try:
        services.reclassify(db, d, body.get("doc_type", ""), ctx.user)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return _done(db, _case(db, ctx, d.case_id))


@router.post("/documents/{doc_id}/void")
def void_document(doc_id: str, body: dict = Body(default={}), ctx: Ctx = Depends(require(DESK_WRITE)),
                  db: Session = Depends(get_db)):
    d = _doc(db, ctx, doc_id)
    d.voided = True  # the original stays in storage and in the audit trail
    audit.record(db, hospital_id=d.hospital_id, actor=ctx.user, action="document.voided", entity="document",
                 entity_id=d.id, case_id=d.case_id, after={"reason": body.get("reason")})
    return _done(db, _case(db, ctx, d.case_id))


# --------------------------------------------------------------------- queries

@router.post("/cases/{case_id}/queries")
def add_query(case_id: str, body: dict = Body(...), ctx: Ctx = Depends(require(DESK_WRITE)),
              db: Session = Depends(get_db)):
    case = _case(db, ctx, case_id)
    text = (body.get("text") or "").strip()
    if len(text) < 5:
        raise HTTPException(422, "Paste the query text exactly as the payer wrote it.")
    try:
        queries.raise_query(db, case, text, ctx.user, stage=body.get("stage"),
                            raised_at=parse_datetime_ist(body.get("raised_at")), source=body.get("source", "manual"))
    except TransitionError as e:
        db.rollback()
        raise HTTPException(409, str(e))
    return _done(db, case)


@router.post("/queries/{qid}/redraft")
def redraft(qid: str, ctx: Ctx = Depends(require(DESK_WRITE | CLINICAL)), db: Session = Depends(get_db)):
    q = _query(db, ctx, qid)
    queries.draft(db, q, ctx.user)
    return _done(db, _case(db, ctx, q.case_id))


@router.patch("/queries/{qid}")
def edit_query(qid: str, body: dict = Body(...), ctx: Ctx = Depends(require(DESK_WRITE | CLINICAL)),
               db: Session = Depends(get_db)):
    q = _query(db, ctx, qid)
    if "draft_reply" in body:
        q.draft_reply = body["draft_reply"]
    if "draft_documents" in body:
        q.draft_documents = body["draft_documents"]
    if "category" in body and body["category"] in queries.CATEGORIES:
        q.category = body["category"]
        if q.category == "medical_necessity" and not q.clinician_signoff_at:
            q.status = "awaiting_clinician"
    audit.record(db, hospital_id=q.hospital_id, actor=ctx.user, action="query.edited", entity="query",
                 entity_id=q.id, case_id=q.case_id)
    return _done(db, _case(db, ctx, q.case_id))


@router.post("/queries/{qid}/signoff")
def signoff(qid: str, body: dict = Body(default={}), ctx: Ctx = Depends(require(CLINICAL)),
            db: Session = Depends(get_db)):
    q = _query(db, ctx, qid)
    queries.signoff(db, q, ctx.user, body.get("text"))
    return _done(db, _case(db, ctx, q.case_id))


@router.post("/queries/{qid}/reply")
def reply(qid: str, body: dict = Body(...), ctx: Ctx = Depends(require(DESK_WRITE)), db: Session = Depends(get_db)):
    q = _query(db, ctx, qid)
    try:
        queries.reply(db, q, ctx.user, body.get("reply_text") or q.draft_reply or "",
                      body.get("document_ids", q.draft_documents or []))
    except (ValueError, TransitionError) as e:
        db.rollback()
        raise HTTPException(409, str(e))
    return _done(db, _case(db, ctx, q.case_id))


@router.post("/queries/{qid}/outcome")
def outcome(qid: str, body: dict = Body(...), ctx: Ctx = Depends(require(DESK_WRITE)), db: Session = Depends(get_db)):
    q = _query(db, ctx, qid)
    if body.get("outcome") not in {"cleared", "repeat_query", "rejected"}:
        raise HTTPException(422, "Outcome must be cleared, repeat_query or rejected.")
    queries.record_outcome(db, q, ctx.user, body["outcome"])
    return _done(db, _case(db, ctx, q.case_id))
