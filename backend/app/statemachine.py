"""The claim state machine: one admission, from case opening to the rupee in the bank.

Every move goes through ``apply``. It checks the move is allowed from the current
state, runs the guard, writes the side records (claim version, decision, payment
advice), starts and stops clocks, and writes the audit trail. Nothing else in the
product sets ``case.state``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import audit, clocks
from .models import Case, CheckResult, Claim, Decision, PaymentAdvice
from .money import now_utc, parse_date, parse_datetime_ist, to_paise
from .rulepacks import get_pack

STATES = [
    "REGISTERED", "PREAUTH_SUBMITTED", "PREAUTH_QUERY", "TREATING", "ENHANCEMENT_SUBMITTED",
    "DISCHARGED", "READY_TO_SUBMIT", "LATE_CONDONATION", "CLAIM_SUBMITTED", "CLAIM_QUERY",
    "APPROVED", "PART_APPROVED", "REJECTED", "PAID_BY_PAYER", "CREDITED", "SHORT_PAID",
    "DISPUTED", "CLOSED",
]

LABELS = {
    "REGISTERED": "Registered", "PREAUTH_SUBMITTED": "Pre-auth submitted", "PREAUTH_QUERY": "Pre-auth query",
    "TREATING": "Treating", "ENHANCEMENT_SUBMITTED": "Enhancement submitted",
    "DISCHARGED": "Discharged, pack in preparation", "READY_TO_SUBMIT": "Ready to submit",
    "LATE_CONDONATION": "Late, condonation needed", "CLAIM_SUBMITTED": "Claim submitted",
    "CLAIM_QUERY": "Claim query", "APPROVED": "Approved", "PART_APPROVED": "Part-approved",
    "REJECTED": "Rejected", "PAID_BY_PAYER": "Paid by payer", "CREDITED": "Credited",
    "SHORT_PAID": "Short-paid", "DISPUTED": "Disputed", "CLOSED": "Closed",
}

# The five stations of the arc shown on every case: capture → pack → query → payer → bank.
STATION = {
    "REGISTERED": 0, "PREAUTH_SUBMITTED": 0, "PREAUTH_QUERY": 0, "TREATING": 0, "ENHANCEMENT_SUBMITTED": 0,
    "DISCHARGED": 1, "READY_TO_SUBMIT": 1, "LATE_CONDONATION": 1,
    "CLAIM_SUBMITTED": 2, "CLAIM_QUERY": 2,
    "APPROVED": 3, "PART_APPROVED": 3, "REJECTED": 3, "DISPUTED": 3, "PAID_BY_PAYER": 3,
    "CREDITED": 4, "SHORT_PAID": 4, "CLOSED": 4,
}

OPEN_STATES = [s for s in STATES if s != "CLOSED"]


@dataclass(frozen=True)
class Transition:
    event: str
    sources: frozenset[str]
    target: str
    label: str
    system_only: bool = False


def _t(event, sources, target, label, system_only=False):
    return Transition(event, frozenset(sources), target, label, system_only)


TRANSITIONS: list[Transition] = [
    _t("submit_preauth", {"REGISTERED"}, "PREAUTH_SUBMITTED", "Pre-auth submitted on the portal"),
    _t("raise_preauth_query", {"PREAUTH_SUBMITTED"}, "PREAUTH_QUERY", "Payer raised a pre-auth query"),
    _t("reply_preauth_query", {"PREAUTH_QUERY"}, "PREAUTH_SUBMITTED", "Pre-auth query answered"),
    _t("approve_preauth", {"PREAUTH_SUBMITTED"}, "TREATING", "Pre-auth approved"),
    _t("reject_preauth", {"PREAUTH_SUBMITTED", "PREAUTH_QUERY"}, "REJECTED", "Pre-auth rejected"),
    _t("start_treatment", {"REGISTERED"}, "TREATING", "Treatment started (no pre-auth needed)"),
    _t("submit_enhancement", {"TREATING"}, "ENHANCEMENT_SUBMITTED", "Enhancement submitted"),
    _t("approve_enhancement", {"ENHANCEMENT_SUBMITTED"}, "TREATING", "Enhancement decided"),
    _t("discharge", {"TREATING", "ENHANCEMENT_SUBMITTED", "REGISTERED"}, "DISCHARGED", "Patient discharged"),
    _t("mark_ready", {"DISCHARGED"}, "READY_TO_SUBMIT", "Claim pack passed the check"),
    _t("reopen_pack", {"READY_TO_SUBMIT"}, "DISCHARGED", "Claim pack reopened"),
    _t("window_passed", {"DISCHARGED", "READY_TO_SUBMIT"}, "LATE_CONDONATION", "Claim window passed", True),
    _t("grant_condonation", {"LATE_CONDONATION"}, "LATE_CONDONATION", "Condonation granted"),
    _t("submit_claim", {"READY_TO_SUBMIT", "LATE_CONDONATION"}, "CLAIM_SUBMITTED", "Claim submitted on the portal"),
    _t("raise_claim_query", {"CLAIM_SUBMITTED"}, "CLAIM_QUERY", "Payer raised a claim query"),
    _t("reply_claim_query", {"CLAIM_QUERY"}, "CLAIM_SUBMITTED", "Claim query answered"),
    _t("approve", {"CLAIM_SUBMITTED", "CLAIM_QUERY"}, "APPROVED", "Claim approved in full"),
    _t("part_approve", {"CLAIM_SUBMITTED", "CLAIM_QUERY"}, "PART_APPROVED", "Claim approved with deductions"),
    _t("reject", {"CLAIM_SUBMITTED", "CLAIM_QUERY"}, "REJECTED", "Claim rejected"),
    _t("appeal", {"REJECTED"}, "CLAIM_SUBMITTED", "Appeal or reprocess submitted"),
    _t("payer_paid", {"APPROVED", "PART_APPROVED", "DISPUTED"}, "PAID_BY_PAYER", "Payer released payment"),
    _t("credit_matched", {"APPROVED", "PART_APPROVED", "PAID_BY_PAYER", "DISPUTED"}, "CREDITED",
       "Bank credit matched", True),
    _t("short_paid", {"APPROVED", "PART_APPROVED", "PAID_BY_PAYER"}, "SHORT_PAID", "Credit below approved amount",
       True),
    _t("dispute", {"APPROVED", "PART_APPROVED", "SHORT_PAID", "REJECTED"}, "DISPUTED", "Dispute raised"),
    _t("close", {"CREDITED", "SHORT_PAID", "DISPUTED"}, "CLOSED", "Closed"),
    _t("write_off", set(OPEN_STATES), "CLOSED", "Written off"),
]
BY_EVENT = {t.event: t for t in TRANSITIONS}

# What the button says. Transition labels above are past tense because they also name timeline entries.
BUTTONS: dict[str, str] = {
    "submit_preauth": "Record pre-auth submitted", "approve_preauth": "Record pre-auth approval",
    "reject_preauth": "Record pre-auth rejection", "start_treatment": "Start treatment (no pre-auth)",
    "submit_enhancement": "Record enhancement submitted", "approve_enhancement": "Record enhancement decision",
    "discharge": "Record discharge", "mark_ready": "Mark ready to submit", "reopen_pack": "Reopen the claim pack",
    "grant_condonation": "Record condonation granted", "submit_claim": "Record claim submitted",
    "approve": "Record full approval", "part_approve": "Record approval with deductions",
    "reject": "Record rejection", "appeal": "Record appeal sent", "payer_paid": "Record payer payment",
    "dispute": "Raise a dispute", "close": "Close case", "write_off": "Write off",
}


class TransitionError(ValueError):
    """A move that is not allowed. The message is shown to the desk as-is."""


def allowed_events(case: Case, include_system: bool = False) -> list[dict]:
    out = []
    for t in TRANSITIONS:
        if case.state in t.sources and (include_system or not t.system_only):
            out.append({"event": t.event, "label": t.label, "button": BUTTONS.get(t.event, t.label),
                        "target": t.target})
    return out


def latest_run_status(db: Session, case: Case) -> tuple[str | None, list[CheckResult]]:
    run = (db.query(CheckResult.run_id, func.max(CheckResult.created_at).label("at"))
           .filter(CheckResult.case_id == case.id).group_by(CheckResult.run_id)
           .order_by(func.max(CheckResult.created_at).desc()).first())
    if not run:
        return None, []
    rows = db.query(CheckResult).filter(CheckResult.run_id == run.run_id).all()
    if any(r.result == "red" for r in rows):
        return "red", rows
    if any(r.result == "amber" and not r.cleared_at for r in rows):
        return "amber", rows
    return "green", rows


def _require(data: dict, key: str, message: str):
    v = data.get(key)
    if v in (None, ""):
        raise TransitionError(message)
    return v


def _next_version(db: Session, case: Case, stage: str) -> int:
    n = db.query(func.max(Claim.version)).filter(Claim.case_id == case.id, Claim.stage == stage).scalar()
    return (n or 0) + 1


def apply(db: Session, case: Case, event: str, actor, data: dict | None = None, *, system: bool = False) -> Case:
    data = dict(data or {})
    t = BY_EVENT.get(event)
    if t is None:
        raise TransitionError(f"Unknown action '{event}'.")
    if t.system_only and not system:
        raise TransitionError(f"'{t.label}' is recorded automatically and cannot be set by hand.")
    if case.state not in t.sources:
        raise TransitionError(f"Cannot do '{t.label}' while the case is '{LABELS[case.state]}'.")

    pack = get_pack(case.payer.scheme)
    at: datetime = parse_datetime_ist(data.get("at")) or now_utc()
    target = t.target
    before = {"state": case.state}
    extra: dict = {}

    if event == "submit_preauth":
        ref = _require(data, "portal_ref", "Enter the portal reference shown after you submitted the pre-auth.")
        case.portal_case_id = case.portal_case_id or ref
        amount = to_paise(data.get("amount")) if data.get("amount") is not None else (case.billed_paise or 0)
        db.add(Claim(hospital_id=case.hospital_id, case_id=case.id, stage="preauth",
                     version=_next_version(db, case, "preauth"), amount_paise=amount or 0,
                     package_codes=[case.package_code] if case.package_code else [], portal_ref=ref,
                     submitted_at=at, submitted_by=getattr(actor, "id", None)))
    elif event == "start_treatment":
        if pack.needs_preauth(case.emergency):
            raise TransitionError(f"{pack.name} needs a pre-auth before treatment. Submit the pre-auth first.")
    elif event == "submit_enhancement":
        ref = _require(data, "portal_ref", "Enter the portal reference for the enhancement.")
        db.add(Claim(hospital_id=case.hospital_id, case_id=case.id, stage="enhancement",
                     version=_next_version(db, case, "enhancement"),
                     amount_paise=to_paise(data.get("amount")) or 0, portal_ref=ref, submitted_at=at,
                     submitted_by=getattr(actor, "id", None)))
    elif event == "discharge":
        if case.state == "REGISTERED" and pack.needs_preauth(case.emergency):
            raise TransitionError(f"{pack.name} needs a pre-auth before discharge can be recorded.")
        case.discharged_at = parse_datetime_ist(data.get("discharged_at")) or case.discharged_at or at
        if case.discharged_at < case.admitted_at:
            raise TransitionError("Discharge cannot be before admission.")
    elif event == "mark_ready":
        status, _ = latest_run_status(db, case)
        if status is None:
            raise TransitionError("Run the claim check first.")
        if status == "red":
            raise TransitionError("The claim check has red flags. Fix them and run the check again.")
        if status == "amber":
            raise TransitionError("Amber flags are waiting for review. Clear them before marking ready.")
    elif event == "grant_condonation":
        case.condonation_granted = True
        extra["condonation_ref"] = data.get("reference_no")
    elif event == "submit_claim":
        if case.state == "LATE_CONDONATION" and not case.condonation_granted:
            raise TransitionError("The claim window has passed. Record the condonation before submitting.")
        if case.state == "READY_TO_SUBMIT" and not system:
            status, _ = latest_run_status(db, case)
            if status != "green":
                raise TransitionError("The latest claim check is not green. Run the check again.")
        ref = _require(data, "portal_ref",
                       "Enter the claim reference from the portal (or the acknowledgement number for paper bills).")
        amount = to_paise(data["amount"]) if data.get("amount") is not None else case.billed_paise
        if not amount:
            raise TransitionError("The claim amount is missing. Add the final bill amount first.")
        db.add(Claim(hospital_id=case.hospital_id, case_id=case.id, stage="final",
                     version=_next_version(db, case, "final"), amount_paise=amount,
                     package_codes=[case.package_code] if case.package_code else [], portal_ref=ref,
                     submitted_at=at, submitted_by=getattr(actor, "id", None)))
        case.portal_case_id = case.portal_case_id or ref
    elif event in {"approve", "part_approve", "approve_preauth", "approve_enhancement"}:
        stage = {"approve_preauth": "preauth", "approve_enhancement": "enhancement"}.get(event, "final")
        approved = to_paise(data.get("approved_amount")) if data.get("approved_amount") is not None else None
        deductions = [{"reason": d.get("reason", "Unspecified"), "amount_paise": to_paise(d.get("amount")) or 0}
                      for d in data.get("deductions", [])]
        if stage == "final":
            claimed = current_claim_amount(db, case)
            if approved is None:
                approved = claimed - sum(d["amount_paise"] for d in deductions) if claimed else None
            if approved is None:
                raise TransitionError("Enter the approved amount.")
            if event == "approve" and claimed and approved < claimed and not deductions:
                target = "PART_APPROVED"
                deductions = [{"reason": "Unexplained difference", "amount_paise": claimed - approved}]
            if event == "part_approve" and not deductions and claimed:
                deductions = [{"reason": "Unexplained difference", "amount_paise": max(claimed - approved, 0)}]
        decision = "part_approved" if target == "PART_APPROVED" else "approved"
        db.add(Decision(hospital_id=case.hospital_id, case_id=case.id, stage=stage, decision=decision,
                        decided_at=at, approved_paise=approved or 0, deductions=deductions,
                        source=data.get("source", "manual")))
    elif event in {"reject", "reject_preauth"}:
        reason = _require(data, "reason", "Enter the rejection reason given by the payer.")
        db.add(Decision(hospital_id=case.hospital_id, case_id=case.id,
                        stage="preauth" if event == "reject_preauth" else "final", decision="rejected",
                        decided_at=at, approved_paise=0, rejection_reason=reason, source=data.get("source", "manual")))
    elif event == "appeal":
        ref = _require(data, "portal_ref", "Enter the reference of the appeal or reprocess request.")
        has_final = db.query(Claim).filter(Claim.case_id == case.id, Claim.stage == "final").count() > 0
        target = "CLAIM_SUBMITTED" if has_final else "PREAUTH_SUBMITTED"
        stage = "final" if has_final else "preauth"
        db.add(Claim(hospital_id=case.hospital_id, case_id=case.id, stage=stage,
                     version=_next_version(db, case, stage), amount_paise=current_claim_amount(db, case) or 0,
                     portal_ref=ref, submitted_at=at, submitted_by=getattr(actor, "id", None), status="appeal"))
    elif event == "payer_paid":
        ref = _require(data, "payment_ref", "Enter the payment reference (UTR or advice number).")
        paid_on = parse_date(data.get("paid_on")) or at.date()
        amount = to_paise(data.get("amount")) if data.get("amount") is not None else None
        if amount is None:
            amount = net_expected(db, case)
        tds = to_paise(data.get("tds")) if data.get("tds") is not None else expected_tds(db, case)
        if not data.pop("advice_recorded", False):  # a multi-claim advice is recorded once by the caller
            db.add(PaymentAdvice(hospital_id=case.hospital_id, payer_id=case.payer_id, reference_no=ref,
                                 amount_paise=amount, paid_on=paid_on,
                                 lines=[{"case_id": case.id, "amount_paise": amount, "tds_paise": tds}],
                                 source=data.get("source", "manual")))
        data["paid_on_dt"] = datetime(paid_on.year, paid_on.month, paid_on.day, 6, 30)
    elif event in {"write_off", "close"}:
        if event == "write_off":
            case.close_reason = _require(data, "reason", "Enter why this case is being written off.")
        else:
            case.close_reason = data.get("reason") or case.close_reason or "Fully reconciled"
    elif event == "dispute":
        _require(data, "reason", "Enter what is being disputed.")
        extra["reason"] = data["reason"]

    case.state = target
    case.state_changed_at = at
    clocks.on_event(db, case, pack, event, at, data)
    audit.record(db, hospital_id=case.hospital_id, actor=actor or "system", action=f"case.{event}",
                 entity="case", entity_id=case.id, case_id=case.id, before=before,
                 after={"state": target, **{k: v for k, v in data.items() if k not in {"paid_on_dt"}}, **extra})
    db.flush()
    return case


# ---------------------------------------------------------------- money per case

def current_claim_amount(db: Session, case: Case) -> int:
    c = (db.query(Claim).filter(Claim.case_id == case.id, Claim.stage == "final")
         .order_by(Claim.version.desc()).first())
    if c:
        return c.amount_paise
    pending = [o for o in db.new if isinstance(o, Claim) and o.case_id == case.id and o.stage == "final"]
    if pending:
        return pending[-1].amount_paise
    return case.billed_paise or 0


def latest_decision(db: Session, case: Case, stage: str = "final") -> Decision | None:
    pending = [o for o in db.new if isinstance(o, Decision) and o.case_id == case.id and o.stage == stage]
    if pending:
        return pending[-1]
    return (db.query(Decision).filter(Decision.case_id == case.id, Decision.stage == stage)
            .order_by(Decision.decided_at.desc(), Decision.created_at.desc()).first())


def approved_amount(db: Session, case: Case) -> int | None:
    d = latest_decision(db, case)
    return d.approved_paise if d and d.decision != "rejected" else (0 if d else None)


def expected_tds(db: Session, case: Case) -> int:
    approved = approved_amount(db, case) or 0
    bps = case.payer.expected_tds_bps or 0
    return approved * bps // 10000


def net_expected(db: Session, case: Case) -> int:
    return (approved_amount(db, case) or 0) - expected_tds(db, case)
