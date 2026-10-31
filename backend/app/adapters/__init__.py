"""Scheme adapters: how each payer's portal (or paper process) maps onto the ledger.

The Python side owns status mapping, payment-reference formats and how case values
are formatted for pre-fill. The page selectors the Chrome extension needs live in
``selectors/<portal>.yaml`` and are served to the extension at runtime, so a
changed portal page is fixed by editing YAML, not by shipping a new extension.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

import yaml

from ..models import Case
from ..money import to_ist

HERE = Path(__file__).parent

# Semantic statuses, checked in this order. Verify against the exact strings each portal
# shows and add them to the adapter's extra_rules once captured (docs/ADAPTERS.md).
GENERIC_RULES: list[tuple[str, str]] = [
    ("paid", r"\b(paid|payment\s+(done|released|processed|credited|made)|settled)\b"),
    ("rejected", r"\b(reject|denied|repudiat|disallowed\s+in\s+full)"),
    ("part_approved", r"(partial|part)[\s-]*(ly\s+)?(approv|settl|pass)|approved\s+with\s+deduction"),
    ("query", r"\bquer(y|ies)\b(?!.*\b(repl|answer|resolv|closed))|pending\s+(with|at)\s+hospital"
              r"|clarification\s+(sought|required|raised)|deficien"),
    ("approved", r"\b(approv|sanction|passed\s+for\s+payment)"),
    ("submitted", r"submit|under\s+(process|review|scrutiny)|pending|in\s+process|forwarded|received"),
]

# Which event a semantic status means, given where the case is now.
EVENT_FOR = {
    "query": {"PREAUTH_SUBMITTED": "raise_preauth_query", "CLAIM_SUBMITTED": "raise_claim_query"},
    "approved": {"PREAUTH_SUBMITTED": "approve_preauth", "PREAUTH_QUERY": "approve_preauth",
                 "ENHANCEMENT_SUBMITTED": "approve_enhancement", "CLAIM_SUBMITTED": "approve",
                 "CLAIM_QUERY": "approve"},
    "part_approved": {"CLAIM_SUBMITTED": "part_approve", "CLAIM_QUERY": "part_approve"},
    "rejected": {"PREAUTH_SUBMITTED": "reject_preauth", "PREAUTH_QUERY": "reject_preauth",
                 "CLAIM_SUBMITTED": "reject", "CLAIM_QUERY": "reject"},
    "paid": {"APPROVED": "payer_paid", "PART_APPROVED": "payer_paid", "DISPUTED": "payer_paid"},
}

FIELD_KEYS = ["case_no", "patient_name", "beneficiary_ref", "referral_no", "age", "sex", "admission_date",
              "discharge_date", "procedure_date", "diagnosis", "icd10", "procedure", "package_code",
              "claim_amount", "bill_no", "bill_date", "treating_doctor", "ward"]


@dataclass
class Adapter:
    key: str
    name: str
    kind: str  # portal | paper | email
    date_format: str = "%d/%m/%Y"
    extra_rules: list[tuple[str, str]] = field(default_factory=list)  # exact portal strings first
    payment_ref_pattern: str = r"\b([A-Z0-9]{12,22})\b"

    def semantic_status(self, text: str) -> str | None:
        t = (text or "").strip().lower()
        for semantic, pattern in [*self.extra_rules, *GENERIC_RULES]:
            if re.search(pattern, t, flags=re.I):
                return semantic
        return None

    def event_for(self, text: str, state: str) -> tuple[str | None, str | None]:
        semantic = self.semantic_status(text)
        if not semantic or semantic == "submitted":
            return semantic, None
        return semantic, EVENT_FOR.get(semantic, {}).get(state)

    def fmt_date(self, v: date | datetime | None) -> str:
        if v is None:
            return ""
        if isinstance(v, datetime):
            v = to_ist(v).date()
        return v.strftime(self.date_format)

    def prefill_values(self, case: Case, claim_amount_paise: int | None) -> dict[str, str]:
        amt = claim_amount_paise if claim_amount_paise is not None else case.billed_paise
        return {
            "case_no": case.case_no,
            "patient_name": case.beneficiary_name or "",
            "beneficiary_ref": case.beneficiary_ref or "",
            "referral_no": case.referral_no or "",
            "age": str(case.age or ""),
            "sex": case.sex or "",
            "admission_date": self.fmt_date(case.admitted_at),
            "discharge_date": self.fmt_date(case.discharged_at),
            "procedure_date": self.fmt_date(case.procedure_date),
            "diagnosis": case.provisional_diagnosis or "",
            "icd10": case.icd10 or "",
            "procedure": case.planned_procedure or "",
            "package_code": case.package_code or "",
            "claim_amount": f"{(amt or 0) / 100:.2f}",
            "bill_no": case.bill_no or "",
            "bill_date": self.fmt_date(case.bill_date),
            "treating_doctor": case.treating_doctor or "",
            "ward": case.ward or "",
        }


ADAPTERS: dict[str, Adapter] = {
    "nha_tms": Adapter("nha_tms", "NHA TMS 2.0 (CGHS + PM-JAY)", "portal"),
    "utiitsl": Adapter("utiitsl", "UTI-ITSL ECHS bill processing", "portal"),
    "tpa": Adapter("tpa", "Insurer / TPA portal", "portal"),
    "paper": Adapter("paper", "Paper or email submission", "paper"),
    "mock": Adapter("mock", "Mock payer portal (test fixture)", "portal", date_format="%Y-%m-%d"),
}


def get_adapter(key: str) -> Adapter:
    return ADAPTERS.get(key) or ADAPTERS["paper"]


@lru_cache
def selector_config(portal: str) -> dict:
    f = HERE / "selectors" / f"{portal}.yaml"
    if not f.exists():
        return {"portal": portal, "status": "not_available"}
    return yaml.safe_load(f.read_text(encoding="utf-8"))


def all_selector_configs() -> list[dict]:
    return [selector_config(p.stem) for p in sorted((HERE / "selectors").glob("*.yaml"))]
