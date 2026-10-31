"""M3 Claim pack builder: assemble the documents the portal expects, in its order,
plus the field values the extension uses to pre-fill the portal form."""
from __future__ import annotations

import io
import json
import zipfile

from sqlalchemy.orm import Session

from .adapters import get_adapter, selector_config
from .documents import DOC_TYPES, load_file
from .models import Case, Document
from .rulepacks import get_pack
from .statemachine import LABELS, current_claim_amount, latest_run_status

_EXT = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png", "text/plain": "txt"}


def ordered_documents(db: Session, case: Case) -> list[Document]:
    pack = get_pack(case.payer.scheme)
    flags = {"emergency": case.emergency, "surgical": case.surgical, "implant_used": case.implant_used}
    order = pack.docs_for("final" if case.discharged_at else "preauth", flags)
    docs = db.query(Document).filter(Document.case_id == case.id, Document.voided.is_(False)).all()
    rank = {t: i for i, t in enumerate(order)}
    return sorted(docs, key=lambda d: (rank.get(d.doc_type, 999), d.created_at))


def build_zip(db: Session, case: Case) -> bytes:
    docs = ordered_documents(db, case)
    buf = io.BytesIO()
    adapter = get_adapter(case.payer.portal)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, d in enumerate(docs, 1):
            ext = _EXT.get(d.mime, d.filename.rsplit(".", 1)[-1] if "." in d.filename else "bin")
            z.writestr(f"{case.case_no}_{i:02d}_{d.doc_type}.{ext}", load_file(d.storage_key))
        fields = adapter.prefill_values(case, current_claim_amount(db, case))
        z.writestr(f"{case.case_no}_fields.json", json.dumps(fields, indent=2, ensure_ascii=False))
        index = [f"Claim pack {case.case_no} for {case.payer.name}", ""]
        index += [f"{i:02d}  {DOC_TYPES.get(d.doc_type, d.doc_type)}  ({d.filename})" for i, d in enumerate(docs, 1)]
        z.writestr(f"{case.case_no}_index.txt", "\n".join(index) + "\n")
    return buf.getvalue()


def prefill(db: Session, case: Case, portal: str | None = None) -> dict:
    portal = portal or case.payer.portal
    adapter = get_adapter(portal)
    cfg = selector_config(portal)
    values = adapter.prefill_values(case, current_claim_amount(db, case))
    form = (cfg.get("pages") or {}).get("claim_form") or {}
    docs = ordered_documents(db, case)
    uploads = {}
    for slot, spec in (form.get("uploads") or {}).items():
        types = spec.get("doc_types", [])
        chosen = [d for d in docs if d.doc_type in types and d.verified_at]
        if not spec.get("multiple"):
            chosen = chosen[:1]
        uploads[slot] = [{"document_id": d.id, "filename": d.filename, "mime": d.mime} for d in chosen]
    status, _ = latest_run_status(db, case)
    if case.state == "READY_TO_SUBMIT":
        blocked = None
    elif case.state == "LATE_CONDONATION":
        blocked = None if case.condonation_granted else "The claim window has passed. Record the condonation first."
    elif case.state == "DISCHARGED":
        blocked = ("Mark the case ready to submit first; the claim check is green." if status == "green"
                   else "The claim check is not green yet. Fix it in Aeternix first.")
    else:
        blocked = f"This case is '{LABELS[case.state]}', so there is no claim to file."
    return {"portal": portal, "portal_status": cfg.get("status"), "case_id": case.id, "case_no": case.case_no,
            "state": case.state, "state_label": LABELS[case.state], "check_status": status,
            "ready": blocked is None, "blocked_reason": blocked,
            "fields": {k: values.get(k, "") for k in (form.get("fields") or {}).keys()} or values,
            "uploads": uploads}
