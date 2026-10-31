"""The claims ledger.

Every money amount is stored as integer paise (1 rupee = 100 paise) so totals
never drift. Every timestamp is stored as naive UTC. Every row that belongs to
a hospital carries ``hospital_id``; the API never reads a row without that filter
(and ``sql/rls.sql`` enforces the same rule inside Postgres).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (JSON, BigInteger, Boolean, Date, DateTime, ForeignKey, Index, Integer, String,
                        Text, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSON, list: JSON}


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


# --------------------------------------------------------------------------- org

class Hospital(Base, Timestamped):
    __tablename__ = "hospital"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    code: Mapped[str] = mapped_column(String(8), unique=True)  # prefix for case numbers, e.g. EPI
    registration_ids: Mapped[dict] = mapped_column(JSON, default=dict)  # NABH no, HFR id, ROHINI id...
    empanelments: Mapped[list] = mapped_column(JSON, default=list)  # rule-pack keys
    bank_accounts: Mapped[list] = mapped_column(JSON, default=list)  # [{label, last4, bank}]
    digest_recipients: Mapped[list] = mapped_column(JSON, default=list)  # [{channel, to}]
    settings: Mapped[dict] = mapped_column(JSON, default=dict)


class User(Base, Timestamped):
    __tablename__ = "app_user"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # Hospital staff belong to one hospital. Aeternix staff (clinician, aeternix_admin)
    # have hospital_id NULL and work across the hospitals listed in hospital_ids.
    hospital_id: Mapped[str | None] = mapped_column(ForeignKey("hospital.id"), nullable=True, index=True)
    hospital_ids: Mapped[list] = mapped_column(JSON, default=list)
    email: Mapped[str] = mapped_column(String(200), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(32))  # desk|accountant|owner|hospital_admin|clinician|aeternix_admin
    password_hash: Mapped[str] = mapped_column(String(300))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Payer(Base, Timestamped):
    __tablename__ = "payer"
    __table_args__ = (UniqueConstraint("hospital_id", "code"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    hospital_id: Mapped[str] = mapped_column(ForeignKey("hospital.id"), index=True)
    code: Mapped[str] = mapped_column(String(40))  # CGHS, PMJAY, ECHS, DGEHS, DTC, STAR-TPA ...
    name: Mapped[str] = mapped_column(String(200))
    scheme: Mapped[str] = mapped_column(String(40))  # rule-pack key
    kind: Mapped[str] = mapped_column(String(20))  # government | department | private
    portal: Mapped[str] = mapped_column(String(40))  # adapter key
    # Tax deducted at source by this payer, in basis points (1000 = 10%). NULL = not yet configured.
    expected_tds_bps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    narration_keywords: Mapped[list] = mapped_column(JSON, default=list)  # words seen in bank narrations
    escalation_contacts: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


# -------------------------------------------------------------------------- case

class Case(Base, Timestamped):
    """One admission for one payer. Its ``state`` is the claim state machine."""
    __tablename__ = "claim_case"
    __table_args__ = (UniqueConstraint("hospital_id", "case_no"),
                      Index("ix_case_hospital_state", "hospital_id", "state"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    hospital_id: Mapped[str] = mapped_column(ForeignKey("hospital.id"), index=True)
    payer_id: Mapped[str] = mapped_column(ForeignKey("payer.id"), index=True)
    case_no: Mapped[str] = mapped_column(String(32))
    rule_pack_version: Mapped[str] = mapped_column(String(40))

    # Minimal patient identity: enough to check documents agree, nothing more.
    beneficiary_name: Mapped[str] = mapped_column(String(200))
    beneficiary_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)  # card / member id
    referral_no: Mapped[str | None] = mapped_column(String(80), nullable=True)
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sex: Mapped[str | None] = mapped_column(String(1), nullable=True)

    admitted_at: Mapped[datetime] = mapped_column(DateTime)
    discharged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    procedure_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    ward: Mapped[str | None] = mapped_column(String(80), nullable=True)
    treating_doctor: Mapped[str | None] = mapped_column(String(120), nullable=True)
    provisional_diagnosis: Mapped[str | None] = mapped_column(String(300), nullable=True)
    icd10: Mapped[str | None] = mapped_column(String(16), nullable=True)
    planned_procedure: Mapped[str | None] = mapped_column(String(300), nullable=True)
    package_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    emergency: Mapped[bool] = mapped_column(Boolean, default=False)
    portability: Mapped[bool] = mapped_column(Boolean, default=False)
    implant_used: Mapped[bool] = mapped_column(Boolean, default=False)
    surgical: Mapped[bool] = mapped_column(Boolean, default=False)

    billed_paise: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    bill_no: Mapped[str | None] = mapped_column(String(60), nullable=True)
    bill_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    bill_lines: Mapped[list] = mapped_column(JSON, default=list)  # [{code, description, qty, amount_paise}]

    state: Mapped[str] = mapped_column(String(40), default="REGISTERED")
    state_changed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    portal_registered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    portal_case_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    counter_checks: Mapped[dict] = mapped_column(JSON, default=dict)  # e.g. {"biometric_discharge": "<iso time>"}
    condonation_granted: Mapped[bool] = mapped_column(Boolean, default=False)
    close_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    imported: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    payer: Mapped[Payer] = relationship(lazy="joined")


class Claim(Base, Timestamped):
    """One submission to the payer: a pre-auth, an enhancement or the final claim.
    A resubmission is a new version, never an overwrite."""
    __tablename__ = "claim"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    hospital_id: Mapped[str] = mapped_column(ForeignKey("hospital.id"), index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("claim_case.id"), index=True)
    stage: Mapped[str] = mapped_column(String(20))  # preauth | enhancement | final
    version: Mapped[int] = mapped_column(Integer, default=1)
    amount_paise: Mapped[int] = mapped_column(BigInteger, default=0)
    package_codes: Mapped[list] = mapped_column(JSON, default=list)
    portal_ref: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    submitted_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="submitted")


class Document(Base, Timestamped):
    __tablename__ = "document"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    hospital_id: Mapped[str] = mapped_column(ForeignKey("hospital.id"), index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("claim_case.id"), index=True)
    doc_type: Mapped[str] = mapped_column(String(40), default="other")
    source: Mapped[str] = mapped_column(String(20), default="upload")  # camera|scanner|whatsapp|email|upload
    filename: Mapped[str] = mapped_column(String(300))
    mime: Mapped[str] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    storage_key: Mapped[str] = mapped_column(String(300))
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    ocr_method: Mapped[str | None] = mapped_column(String(20), nullable=True)
    classification_confidence: Mapped[float | None] = mapped_column(nullable=True)
    extracted: Mapped[dict] = mapped_column(JSON, default=dict)  # {field: {value, confidence}}
    verified_fields: Mapped[dict] = mapped_column(JSON, default=dict)  # {field: value} confirmed by a person
    verified_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    voided: Mapped[bool] = mapped_column(Boolean, default=False)


class CheckResult(Base, Timestamped):
    __tablename__ = "check_result"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    hospital_id: Mapped[str] = mapped_column(ForeignKey("hospital.id"), index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("claim_case.id"), index=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    rule: Mapped[str] = mapped_column(String(60))
    layer: Mapped[str] = mapped_column(String(20))  # hard | model | clinician
    result: Mapped[str] = mapped_column(String(10))  # green | amber | red
    message: Mapped[str] = mapped_column(String(500))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    needs_clinician: Mapped[bool] = mapped_column(Boolean, default=False)
    cleared_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    clear_note: Mapped[str | None] = mapped_column(String(500), nullable=True)


class Query(Base, Timestamped):
    __tablename__ = "payer_query"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    hospital_id: Mapped[str] = mapped_column(ForeignKey("hospital.id"), index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("claim_case.id"), index=True)
    stage: Mapped[str] = mapped_column(String(20))  # preauth | claim
    raised_at: Mapped[datetime] = mapped_column(DateTime)
    text: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(30))
    requested_doc_types: Mapped[list] = mapped_column(JSON, default=list)
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cutoff_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="manual")
    status: Mapped[str] = mapped_column(String(30), default="open")  # open|drafted|awaiting_clinician|replied|closed
    draft_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    draft_documents: Mapped[list] = mapped_column(JSON, default=list)
    reply_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    documents_sent: Mapped[list] = mapped_column(JSON, default=list)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    replied_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    clinician_signoff_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    clinician_signoff_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(20), nullable=True)  # cleared | repeat_query | rejected


class QueryLibraryEntry(Base, Timestamped):
    __tablename__ = "query_library"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    hospital_id: Mapped[str | None] = mapped_column(ForeignKey("hospital.id"), nullable=True, index=True)
    scheme: Mapped[str] = mapped_column(String(40))
    category: Mapped[str] = mapped_column(String(30))
    query_text: Mapped[str] = mapped_column(Text)
    reply_text: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(String(20))


class Decision(Base, Timestamped):
    __tablename__ = "decision"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    hospital_id: Mapped[str] = mapped_column(ForeignKey("hospital.id"), index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("claim_case.id"), index=True)
    stage: Mapped[str] = mapped_column(String(20), default="final")
    decision: Mapped[str] = mapped_column(String(20))  # approved | part_approved | rejected
    decided_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    approved_paise: Mapped[int] = mapped_column(BigInteger, default=0)
    deductions: Mapped[list] = mapped_column(JSON, default=list)  # [{reason, amount_paise}]
    rejection_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="manual")


class PaymentAdvice(Base, Timestamped):
    __tablename__ = "payment_advice"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    hospital_id: Mapped[str] = mapped_column(ForeignKey("hospital.id"), index=True)
    payer_id: Mapped[str] = mapped_column(ForeignKey("payer.id"), index=True)
    reference_no: Mapped[str] = mapped_column(String(80), index=True)
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    paid_on: Mapped[date] = mapped_column(Date)
    lines: Mapped[list] = mapped_column(JSON, default=list)  # [{case_id, amount_paise, tds_paise}]
    source: Mapped[str] = mapped_column(String(20), default="manual")


class BankTransaction(Base, Timestamped):
    __tablename__ = "bank_txn"
    __table_args__ = (UniqueConstraint("hospital_id", "account", "txn_date", "amount_paise", "narration",
                                       "reference_no", name="uq_bank_line"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    hospital_id: Mapped[str] = mapped_column(ForeignKey("hospital.id"), index=True)
    account: Mapped[str] = mapped_column(String(60))
    txn_date: Mapped[date] = mapped_column(Date)
    amount_paise: Mapped[int] = mapped_column(BigInteger)  # credits only
    narration: Mapped[str] = mapped_column(String(500))
    reference_no: Mapped[str] = mapped_column(String(80), default="")
    import_batch: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(20), default="unmatched")  # unmatched|proposed|matched|ignored
    match_note: Mapped[str | None] = mapped_column(String(300), nullable=True)


class Allocation(Base, Timestamped):
    __tablename__ = "allocation"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    hospital_id: Mapped[str] = mapped_column(ForeignKey("hospital.id"), index=True)
    bank_txn_id: Mapped[str] = mapped_column(ForeignKey("bank_txn.id"), index=True)
    payment_advice_id: Mapped[str | None] = mapped_column(ForeignKey("payment_advice.id"), nullable=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("claim_case.id"), index=True)
    amount_paise: Mapped[int] = mapped_column(BigInteger)  # net credited for this case
    tax_paise: Mapped[int] = mapped_column(BigInteger, default=0)
    method: Mapped[str] = mapped_column(String(20))  # exact_ref | amount_date | split | manual
    confidence: Mapped[float] = mapped_column(default=1.0)
    status: Mapped[str] = mapped_column(String(20), default="proposed")  # proposed|confirmed|rejected
    confirmed_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Escalation(Base, Timestamped):
    __tablename__ = "escalation"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    hospital_id: Mapped[str] = mapped_column(ForeignKey("hospital.id"), index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("claim_case.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))  # reminder|grievance|condonation|interest_claim|dispute|appeal
    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    addressed_to: Mapped[str | None] = mapped_column(String(300), nullable=True)
    amount_paise: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|sent|responded|closed
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sent_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reference_no: Mapped[str | None] = mapped_column(String(80), nullable=True)
    response: Mapped[str | None] = mapped_column(Text, nullable=True)


class Clock(Base, Timestamped):
    __tablename__ = "clock"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    hospital_id: Mapped[str] = mapped_column(ForeignKey("hospital.id"), index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("claim_case.id"), index=True)
    query_id: Mapped[str | None] = mapped_column(ForeignKey("payer_query.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(40))
    label: Mapped[str] = mapped_column(String(200))
    started_at: Mapped[datetime] = mapped_column(DateTime)
    due_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running|met|missed|cancelled
    source: Mapped[str] = mapped_column(String(300), default="")


class StatusSnapshot(Base, Timestamped):
    """What the extension read from a portal page, exactly as seen."""
    __tablename__ = "status_snapshot"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    hospital_id: Mapped[str] = mapped_column(ForeignKey("hospital.id"), index=True)
    portal: Mapped[str] = mapped_column(String(40))
    portal_ref: Mapped[str] = mapped_column(String(80), index=True)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("claim_case.id"), nullable=True, index=True)
    status_text: Mapped[str] = mapped_column(String(300))
    mapped_event: Mapped[str | None] = mapped_column(String(40), nullable=True)
    approved_paise: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    deduction_paise: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    payment_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)
    payment_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    query_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    changed: Mapped[bool] = mapped_column(Boolean, default=False)
    applied: Mapped[str | None] = mapped_column(String(300), nullable=True)
    seen_by: Mapped[str | None] = mapped_column(String(32), nullable=True)


class InboundEmail(Base, Timestamped):
    __tablename__ = "inbound_email"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    hospital_id: Mapped[str] = mapped_column(ForeignKey("hospital.id"), index=True)
    sender: Mapped[str] = mapped_column(String(300), default="")
    subject: Mapped[str] = mapped_column(String(500), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    parsed: Mapped[dict] = mapped_column(JSON, default=dict)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("claim_case.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="new")  # new | applied | ignored


class DeskActivity(Base):
    """Seconds a desk user spent on a case screen. Feeds 'desk minutes per claim'."""
    __tablename__ = "desk_activity"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hospital_id: Mapped[str] = mapped_column(ForeignKey("hospital.id"), index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("claim_case.id"), index=True)
    user_id: Mapped[str] = mapped_column(String(32))
    seconds: Mapped[int] = mapped_column(Integer)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    """Append-only. The API has no update or delete for this table."""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hospital_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    actor_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actor_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    action: Mapped[str] = mapped_column(String(60))
    entity: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    case_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    before: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Job(Base, Timestamped):
    """Postgres-backed job queue: no extra infrastructure."""
    __tablename__ = "job"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    key: Mapped[str | None] = mapped_column(String(120), nullable=True, unique=True)  # idempotency key
    run_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued|running|done|failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
