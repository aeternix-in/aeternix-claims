"""Case and document operations shared by the API, the importer and the seed script."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import audit, clocks, documents
from .models import Case, Document, Hospital, Payer
from .money import now_utc, parse_date, parse_datetime_ist, to_ist, to_paise
from .rulepacks import get_pack

CASE_FIELDS = ["beneficiary_name", "beneficiary_ref", "referral_no", "age", "sex", "ward", "treating_doctor",
               "provisional_diagnosis", "icd10", "planned_procedure", "package_code", "emergency", "portability",
               "implant_used", "surgical", "notes", "bill_no"]


def next_case_no(db: Session, hospital_id: str, at: datetime | None = None) -> str:
    h = db.get(Hospital, hospital_id)
    local = to_ist(at or now_utc())
    prefix = f"{h.code}-{local:%y%m}-"
    n = db.query(func.count(Case.id)).filter(Case.hospital_id == hospital_id, Case.case_no.like(prefix + "%")).scalar()
    pending = sum(1 for o in db.new if isinstance(o, Case) and o.case_no and o.case_no.startswith(prefix))
    return f"{prefix}{(n or 0) + pending + 1:04d}"


def create_case(db: Session, hospital_id: str, data: dict, actor) -> Case:
    payer = db.query(Payer).filter(Payer.hospital_id == hospital_id, Payer.id == data.get("payer_id")).first()
    if not payer:
        raise ValueError("Choose the payer for this case.")
    if not (data.get("beneficiary_name") or "").strip():
        raise ValueError("Enter the patient's name as it appears on the card.")
    admitted = parse_datetime_ist(data.get("admitted_at")) or now_utc()
    pack = get_pack(payer.scheme)
    case = Case(hospital_id=hospital_id, payer_id=payer.id, case_no=next_case_no(db, hospital_id, admitted),
                rule_pack_version=pack.version, admitted_at=admitted, created_by=getattr(actor, "id", None),
                beneficiary_name=data["beneficiary_name"].strip())
    case.payer = payer
    for f in CASE_FIELDS:
        if f in data and f != "beneficiary_name":
            setattr(case, f, data[f])
    if data.get("icd10"):
        case.icd10 = str(data["icd10"]).strip().upper()
    db.add(case)
    db.flush()
    clocks.on_case_created(db, case, pack)
    audit.record(db, hospital_id=hospital_id, actor=actor, action="case.created", entity="case", entity_id=case.id,
                 case_id=case.id, after={"case_no": case.case_no, "payer": payer.code})
    db.flush()
    return case


def update_case(db: Session, case: Case, data: dict, actor) -> Case:
    before = audit.snapshot(case, CASE_FIELDS + ["admitted_at", "procedure_date", "billed_paise"])
    for f in CASE_FIELDS:
        if f in data:
            setattr(case, f, data[f])
    if "icd10" in data and data["icd10"]:
        case.icd10 = str(data["icd10"]).strip().upper()
    if "admitted_at" in data and data["admitted_at"]:
        case.admitted_at = parse_datetime_ist(data["admitted_at"])
    if "procedure_date" in data:
        case.procedure_date = parse_date(data["procedure_date"])
    if "billed_amount" in data:
        case.billed_paise = to_paise(data["billed_amount"])
    audit.record(db, hospital_id=case.hospital_id, actor=actor, action="case.updated", entity="case",
                 entity_id=case.id, case_id=case.id, before=before,
                 after=audit.snapshot(case, CASE_FIELDS + ["admitted_at", "procedure_date", "billed_paise"]))
    db.flush()
    return case


def ingest_document(db: Session, case: Case, filename: str, data: bytes, actor, source: str = "upload",
                    doc_type: str | None = None) -> Document:
    if not data:
        raise ValueError(f"{filename} is empty.")
    if len(data) > 25 * 1024 * 1024:
        raise ValueError(f"{filename} is larger than 25 MB. Scan at a lower resolution.")
    sha, key = documents.store_file(case.hospital_id, data)
    dup = db.query(Document).filter(Document.case_id == case.id, Document.sha256 == sha,
                                    Document.voided.is_(False)).first()
    if dup:
        return dup
    mime = documents.guess_mime(filename, data)
    text, method = documents.extract_text(data, mime)
    dtype, conf = (doc_type, 1.0) if doc_type in documents.DOC_TYPES else documents.classify(text, filename)
    extracted = documents.extract_fields(dtype, text)
    for k, v in documents.llm_extract(dtype, text, [case.beneficiary_name]).items():
        extracted.setdefault(k, v)
    doc = Document(hospital_id=case.hospital_id, case_id=case.id, doc_type=dtype, source=source,
                   filename=filename[:300], mime=mime, size=len(data), sha256=sha, storage_key=key, text=text,
                   ocr_method=method, classification_confidence=conf, extracted=extracted)
    db.add(doc)
    db.flush()
    audit.record(db, hospital_id=case.hospital_id, actor=actor, action="document.added", entity="document",
                 entity_id=doc.id, case_id=case.id, after={"type": dtype, "confidence": conf, "ocr": method,
                                                           "fields": list(extracted)})
    return doc


def reclassify(db: Session, doc: Document, doc_type: str, actor) -> Document:
    if doc_type not in documents.DOC_TYPES:
        raise ValueError("Unknown document type.")
    before = doc.doc_type
    doc.doc_type = doc_type
    doc.classification_confidence = 1.0
    doc.extracted = documents.extract_fields(doc_type, doc.text or "")
    doc.verified_fields, doc.verified_at, doc.verified_by = {}, None, None
    audit.record(db, hospital_id=doc.hospital_id, actor=actor, action="document.reclassified", entity="document",
                 entity_id=doc.id, case_id=doc.case_id, before={"type": before}, after={"type": doc_type})
    db.flush()
    return doc


def verify_document(db: Session, doc: Document, fields: dict, actor) -> Document:
    """A person confirms (or corrects) the extracted fields. Confirmed bill values become the claim's."""
    clean = {}
    for k, v in fields.items():
        if k in {"total_amount"}:
            v = to_paise(v) if not isinstance(v, int) else v
        if k == "bill_lines" and isinstance(v, list):
            v = [{"code": str(x.get("code", "")).upper(), "description": x.get("description", ""),
                  "qty": int(x.get("qty") or 1),
                  "amount_paise": x["amount_paise"] if isinstance(x.get("amount_paise"), int) else to_paise(x.get("amount"))}
                 for x in v]
        if k == "signed":
            v = v in (True, "true", "yes", "Yes", 1, "1")
        clean[k] = v
    doc.verified_fields = {**(doc.verified_fields or {}), **clean}
    doc.verified_by = actor.id
    doc.verified_at = now_utc()
    case = db.get(Case, doc.case_id)
    if doc.doc_type == "final_bill":
        if clean.get("total_amount"):
            case.billed_paise = clean["total_amount"]
        if clean.get("bill_lines"):
            case.bill_lines = clean["bill_lines"]
        if clean.get("bill_no"):
            case.bill_no = clean["bill_no"]
        if clean.get("bill_date"):
            case.bill_date = parse_date(clean["bill_date"])
    if doc.doc_type in {"discharge_summary", "ot_notes"}:
        if clean.get("procedure_date") and not case.procedure_date:
            case.procedure_date = parse_date(clean["procedure_date"])
        if clean.get("icd10") and not case.icd10:
            case.icd10 = str(clean["icd10"]).upper()
        if clean.get("diagnosis") and not case.provisional_diagnosis:
            case.provisional_diagnosis = clean["diagnosis"]
    audit.record(db, hospital_id=doc.hospital_id, actor=actor, action="document.verified", entity="document",
                 entity_id=doc.id, case_id=doc.case_id, after={"fields": list(clean)})
    db.flush()
    return doc
