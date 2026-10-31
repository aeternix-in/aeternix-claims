"""M2 Document intelligence: store, read, classify, extract, verify.

Originals are stored unchanged (encrypted at rest when FILE_ENCRYPTION_KEY is set),
addressed by their SHA-256, so every claim can be rebuilt and audited. Extracted
fields are suggestions; only fields a person has verified reach a claim.
"""
from __future__ import annotations

import hashlib
import io
import mimetypes
import re
import shutil
from datetime import datetime
from pathlib import Path

from .config import settings
from .money import parse_date, to_paise

DOC_TYPES = {
    "referral_letter": "Referral / permission letter",
    "emergency_certificate": "Emergency certificate",
    "beneficiary_card": "Beneficiary card",
    "preauth_form": "Pre-auth form",
    "discharge_summary": "Discharge summary",
    "final_bill": "Final bill",
    "investigation_report": "Investigation report",
    "ot_notes": "OT / operation notes",
    "implant_invoice": "Implant invoice",
    "implant_sticker": "Implant sticker",
    "consent_form": "Consent form",
    "prescription": "Prescription",
    "submission_acknowledgement": "Submission acknowledgement",
    "photo": "Photo",
    "other": "Other",
}

# Fields a person must confirm before the scrubber trusts a document.
KEY_FIELDS = {
    "discharge_summary": ["patient_name", "admission_date", "discharge_date", "diagnosis", "signed"],
    "final_bill": ["patient_name", "bill_no", "bill_date", "total_amount"],
    "referral_letter": ["patient_name", "referral_no"],
    "beneficiary_card": ["patient_name", "beneficiary_ref"],
    "implant_invoice": ["implant_description", "total_amount"],
    "investigation_report": ["report_date"],
    "ot_notes": ["procedure_date"],
}

_KEYWORDS: dict[str, list[tuple[str, float]]] = {
    "discharge_summary": [("discharge summary", 5), ("date of discharge", 3), ("condition at discharge", 3),
                          ("course in hospital", 2), ("advice on discharge", 2), ("final diagnosis", 1)],
    "final_bill": [("final bill", 5), ("bill no", 2), ("grand total", 3), ("net amount", 2), ("bill date", 2),
                   ("amount payable", 2)],
    "investigation_report": [("impression", 2), ("ct kub", 3), ("ultrasound", 2), ("usg", 2), ("creatinine", 2),
                             ("report date", 2), ("haemoglobin", 2), ("investigation report", 4), ("findings", 1),
                             ("radiology", 2), ("laboratory", 2)],
    "ot_notes": [("operation notes", 5), ("ot notes", 5), ("operative findings", 3), ("anaesthesia", 2),
                 ("surgeon", 1), ("procedure notes", 3)],
    "implant_invoice": [("tax invoice", 3), ("implant", 2), ("stent", 1), ("batch", 2), ("lot no", 2),
                        ("hsn", 2), ("invoice no", 2)],
    "implant_sticker": [("sticker", 4), ("ref no", 1), ("lot", 1), ("use by", 3), ("sterile", 2)],
    "referral_letter": [("referral", 4), ("permission letter", 4), ("referred to", 3), ("e-referral", 4),
                        ("referral no", 3), ("wellness centre", 3), ("polyclinic", 2)],
    "emergency_certificate": [("emergency certificate", 6), ("emergency admission", 3), ("certify", 1)],
    "beneficiary_card": [("beneficiary id", 3), ("card no", 3), ("cghs card", 4), ("echs card", 4),
                         ("ayushman", 3), ("valid upto", 2), ("pensioner", 1)],
    "consent_form": [("consent", 4), ("i hereby", 2), ("risks explained", 2)],
    "prescription": [("rx", 2), ("prescription", 4), ("tab.", 1), ("cap.", 1)],
    "submission_acknowledgement": [("acknowledg", 4), ("received", 2), ("diary no", 4), ("inward no", 4)],
    "preauth_form": [("pre-authorisation", 4), ("pre-authorization", 4), ("preauth", 4),
                     ("request for cashless", 4)],
}


# ------------------------------------------------------------------- storage

def _fernet():
    if not settings.file_encryption_key:
        return None
    from cryptography.fernet import Fernet
    return Fernet(settings.file_encryption_key.encode())


def store_file(hospital_id: str, data: bytes) -> tuple[str, str]:
    """Return (sha256, storage_key). Identical files are stored once per hospital."""
    sha = hashlib.sha256(data).hexdigest()
    key = f"{hospital_id}/{sha[:2]}/{sha}"
    path = settings.files_dir / key
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        f = _fernet()
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(f.encrypt(data) if f else data)
        shutil.move(tmp, path)
    return sha, key


def load_file(storage_key: str) -> bytes:
    raw = (settings.files_dir / storage_key).read_bytes()
    f = _fernet()
    return f.decrypt(raw) if f else raw


def guess_mime(filename: str, data: bytes) -> str:
    if data[:4] == b"%PDF":
        return "application/pdf"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


# --------------------------------------------------------------- text / OCR

def _tesseract_ok() -> bool:
    return shutil.which("tesseract") is not None


def extract_text(data: bytes, mime: str) -> tuple[str, str]:
    """Return (text, method). Digital PDFs are read directly; scans and photos go to OCR."""
    if settings.ocr_provider == "none":
        return "", "none"
    if mime == "application/pdf":
        import pdfplumber
        text = ""
        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                text = "\n".join((p.extract_text() or "") for p in pdf.pages[:30])
        except Exception:  # damaged PDF: fall through to OCR
            text = ""
        if len(text.strip()) >= 40:
            return text, "pdf_text"
        if _tesseract_ok():
            try:
                import pypdfium2 as pdfium
                import pytesseract
                pdf = pdfium.PdfDocument(data)
                pages = []
                for i in range(min(len(pdf), 15)):
                    img = pdf[i].render(scale=2.5).to_pil()
                    pages.append(pytesseract.image_to_string(img, lang="eng"))
                return "\n".join(pages), "ocr"
            except Exception:
                return text, "pdf_text"
        return text, "pdf_text"
    if mime.startswith("image/") and _tesseract_ok():
        try:
            import pytesseract
            from PIL import Image, ImageOps
            img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("L")
            return pytesseract.image_to_string(img, lang="eng"), "ocr"
        except Exception:
            return "", "ocr_failed"
    if mime.startswith("text/"):
        return data.decode("utf-8", errors="replace"), "text"
    return "", "unsupported"


# ------------------------------------------------------------- classification

def classify(text: str, filename: str = "") -> tuple[str, float]:
    hay = f"{filename.lower()} \n {text.lower()}"
    scores: dict[str, float] = {}
    for dtype, kws in _KEYWORDS.items():
        s = sum(w for kw, w in kws if kw in hay)
        # filename hints count double: desks often name files well
        fname = filename.lower().replace("_", " ").replace("-", " ")
        s += sum(w for kw, w in kws if kw in fname)
        if s:
            scores[dtype] = s
    if not scores:
        return "other", 0.0
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top, top_s = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    conf = round(top_s / (top_s + second) if (top_s + second) else 0.0, 2)
    if top_s < 3:
        conf = min(conf, 0.4)
    return top, conf


# ---------------------------------------------------------------- extraction

def _first(pattern: str, text: str, flags=re.I | re.M) -> str | None:
    m = re.search(pattern, text, flags)
    if not m:
        return None
    return re.sub(r"\s{2,}.*$", "", m.group(1).strip()).strip(" :.-") or None


_DATE = r"(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{1,2}[ -][A-Za-z]{3,9}[ -]\d{2,4}|\d{4}-\d{2}-\d{2})"
_ICD = re.compile(r"\b([A-TV-Z]\d{2}(?:\.\d{1,2})?)\b")
_BILL_LINE = re.compile(r"^\s*([A-Z][A-Z0-9-]{3,}\d)\s+(.+?)\s+(\d{1,3})\s+([\d,]+(?:\.\d{2})?)\s*$", re.M)


def _field(value, confidence: float) -> dict:
    return {"value": value, "confidence": confidence}


def extract_fields(doc_type: str, text: str) -> dict:
    """Rule-based extraction for the fields the scrubber needs. Returns {field: {value, confidence}}."""
    t = text or ""
    out: dict = {}
    name = _first(r"(?:patient(?:'s)?\s*name|name\s+of\s+(?:the\s+)?patient|beneficiary\s*name)\s*[:\-]\s*(.+)$", t)
    if name:
        out["patient_name"] = _field(name, 0.8)
    age_sex = re.search(r"age\s*/\s*sex\s*[:\-]\s*(\d{1,3})\s*(?:y(?:rs|ears)?)?\s*/\s*([MFO])", t, re.I)
    if age_sex:
        out["age"] = _field(int(age_sex.group(1)), 0.8)
        out["sex"] = _field(age_sex.group(2).upper(), 0.8)
    card = _first(r"(?:cghs|echs|pm-?jay|ayushman|beneficiary|member|card)\s*(?:card\s*)?(?:no|id|number)\.?\s*[:\-]\s*([A-Z0-9/-]{5,})", t)
    if card:
        out["beneficiary_ref"] = _field(card, 0.7)
    ref = _first(r"(?:referral|permission|e-?referral|eir)\s*(?:letter\s*)?(?:no|number|id)\.?\s*[:\-]\s*([A-Z0-9/-]{4,})", t)
    if ref:
        out["referral_no"] = _field(ref, 0.7)

    if doc_type in {"discharge_summary", "final_bill", "referral_letter", "ot_notes", "emergency_certificate"}:
        doa = _first(r"(?:date\s+of\s+admission|admission\s+date|d\.?o\.?a\.?)\s*[:\-]\s*" + _DATE, t)
        dod = _first(r"(?:date\s+of\s+discharge|discharge\s+date|d\.?o\.?d\.?)\s*[:\-]\s*" + _DATE, t)
        if doa and parse_date(doa):
            out["admission_date"] = _field(parse_date(doa).isoformat(), 0.85)
        if dod and parse_date(dod):
            out["discharge_date"] = _field(parse_date(dod).isoformat(), 0.85)

    if doc_type in {"discharge_summary", "ot_notes"}:
        diag = _first(r"(?:final\s+)?diagnosis\s*[:\-]\s*(.+)$", t)
        if diag:
            out["diagnosis"] = _field(diag, 0.75)
            m = _ICD.search(diag)
            if m:
                out["icd10"] = _field(m.group(1), 0.8)
        proc = _first(r"(?:procedure|operation|surgery)\s*(?:done|performed)?\s*[:\-]\s*(.+)$", t)
        if proc:
            out["procedure"] = _field(proc, 0.75)
            pd = re.search(r"\bon\s+" + _DATE, proc, re.I)
            if pd and parse_date(pd.group(1)):
                out["procedure_date"] = _field(parse_date(pd.group(1)).isoformat(), 0.75)
        pdate = _first(r"(?:date\s+of\s+(?:procedure|surgery|operation))\s*[:\-]\s*" + _DATE, t)
        if pdate and parse_date(pdate):
            out["procedure_date"] = _field(parse_date(pdate).isoformat(), 0.85)
        implant = _first(r"implant(?:s)?\s*(?:used)?\s*[:\-]\s*(.+)$", t)
        if implant:
            out["implant"] = _field(implant, 0.7)
        sig = re.search(r"signature\s*[:\-]?\s*(.*)$", t, re.I | re.M)
        if sig:
            line = sig.group(1)
            signed = bool(re.search(r"dr\.?\s+\w|signed|\[signed\]|sd/-", line, re.I)) and not re.search(r"_{3,}", line)
            out["signed"] = _field(signed, 0.6)
        inv = _first(r"investigations?\s*[:\-]\s*(.+)$", t)
        if inv:
            out["investigations"] = _field(inv, 0.7)
        doctor = _first(r"(?:consultant|treating\s+doctor|surgeon)\s*[:\-]\s*(.+)$", t)
        if doctor:
            out["treating_doctor"] = _field(doctor, 0.7)

    if doc_type == "final_bill":
        bill_no = _first(r"bill\s*(?:no|number)\.?\s*[:\-]\s*([A-Z0-9/-]+)", t)
        if bill_no:
            out["bill_no"] = _field(bill_no, 0.85)
        bill_date = _first(r"bill\s*date\s*[:\-]\s*" + _DATE, t)
        if bill_date and parse_date(bill_date):
            out["bill_date"] = _field(parse_date(bill_date).isoformat(), 0.85)
        total = _first(r"(?:grand\s+total|net\s+amount|total\s+amount|amount\s+payable|total)\s*[:\-]?\s*(?:rs\.?|inr|₹)?\s*([\d,]+(?:\.\d{2})?)\s*$", t)
        if total and to_paise(total):
            out["total_amount"] = _field(to_paise(total), 0.8)
        lines = [{"code": m.group(1).upper(), "description": m.group(2).strip(), "qty": int(m.group(3)),
                  "amount_paise": to_paise(m.group(4))} for m in _BILL_LINE.finditer(t)]
        if lines:
            out["bill_lines"] = _field(lines, 0.75)

    if doc_type == "implant_invoice":
        desc = _first(r"(?:description|item|product)\s*[:\-]\s*(.+)$", t) or _first(r"((?:dj|double\s*j|ureteric)\s+stent[^\n]*)", t)
        if desc:
            out["implant_description"] = _field(desc, 0.7)
        total = _first(r"(?:grand\s+total|invoice\s+total|total)\s*[:\-]?\s*(?:rs\.?|inr|₹)?\s*([\d,]+(?:\.\d{2})?)\s*$", t)
        if total and to_paise(total):
            out["total_amount"] = _field(to_paise(total), 0.75)
        inv_no = _first(r"invoice\s*(?:no|number)\.?\s*[:\-]\s*([A-Z0-9/-]+)", t)
        if inv_no:
            out["invoice_no"] = _field(inv_no, 0.8)

    if doc_type == "investigation_report":
        rdate = _first(r"(?:report\s+date|date\s+of\s+report|date)\s*[:\-]\s*" + _DATE, t)
        if rdate and parse_date(rdate):
            out["report_date"] = _field(parse_date(rdate).isoformat(), 0.75)
        test = _first(r"(?:investigation|test|study|examination)\s*[:\-]\s*(.+)$", t)
        if test:
            out["test_name"] = _field(test, 0.7)
    return out


def llm_extract(doc_type: str, text: str, names: list[str]) -> dict:
    """Optional model pass that fills gaps. Never overwrites a rule-based value."""
    from . import llm
    if not llm.enabled():
        return {}
    fields = KEY_FIELDS.get(doc_type, [])
    if not fields:
        return {}
    try:
        data = llm.complete_json(
            "You extract fields from Indian hospital documents. Reply with one JSON object only. "
            "Use null when a field is absent. Dates as YYYY-MM-DD. Amounts as rupees numbers.",
            f"Document type: {doc_type}\nFields: {fields}\n\nText:\n{llm.redact(text, names)[:12000]}")
    except Exception:
        return {}
    out = {}
    for k in fields:
        v = data.get(k)
        if v in (None, "", "[NAME]", "[ID]"):
            continue
        if k == "total_amount":
            v = to_paise(v)
        out[k] = _field(v, 0.6)
    return out


def needs_verification(doc_type: str, verified_fields: dict) -> list[str]:
    return [f for f in KEY_FIELDS.get(doc_type, []) if f not in (verified_fields or {})]


def now_str() -> str:
    return datetime.utcnow().isoformat()


__all__ = ["DOC_TYPES", "KEY_FIELDS", "store_file", "load_file", "guess_mime", "extract_text", "classify",
           "extract_fields", "llm_extract", "needs_verification", "Path"]
