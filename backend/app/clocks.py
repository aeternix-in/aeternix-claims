"""Deadline clocks. Each scheme deadline in a rule pack becomes a clock on the case."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from .config import settings
from .models import Case, Clock
from .money import add, now_utc
from .rulepacks import Deadline, RulePack


def start(db: Session, case: Case, kind: str, base: datetime | None, deadline: Deadline | None,
          query_id: str | None = None) -> Clock | None:
    if deadline is None or base is None:
        return None
    # One running clock per (case, kind, query).
    for c in running(db, case, [kind], query_id=query_id):
        c.status = "cancelled"
        c.stopped_at = now_utc()
    clock = Clock(hospital_id=case.hospital_id, case_id=case.id, query_id=query_id, kind=kind,
                  label=deadline.label, started_at=base, due_at=add(base, deadline.value, deadline.unit),
                  source=deadline.source)
    db.add(clock)
    return clock


def start_fixed(db: Session, case: Case, kind: str, label: str, base: datetime, due: datetime, source: str) -> Clock:
    for c in running(db, case, [kind]):
        c.status = "cancelled"
        c.stopped_at = now_utc()
    clock = Clock(hospital_id=case.hospital_id, case_id=case.id, kind=kind, label=label, started_at=base,
                  due_at=due, source=source)
    db.add(clock)
    return clock


def running(db: Session, case: Case, kinds: list[str] | None = None, query_id: str | None = None) -> list[Clock]:
    q = db.query(Clock).filter(Clock.case_id == case.id, Clock.status == "running")
    if kinds:
        q = q.filter(Clock.kind.in_(kinds))
    if query_id:
        q = q.filter(Clock.query_id == query_id)
    # include clocks added in this session but not yet flushed
    pending = [o for o in db.new if isinstance(o, Clock) and o.case_id == case.id and o.status == "running"
               and (not kinds or o.kind in kinds) and (not query_id or o.query_id == query_id)]
    return list({id(c): c for c in [*q.all(), *pending]}.values())


def stop(db: Session, case: Case, kinds: list[str], at: datetime | None = None, query_id: str | None = None,
         cancel: bool = False) -> list[Clock]:
    at = at or now_utc()
    out = []
    for c in running(db, case, kinds, query_id=query_id):
        c.stopped_at = at
        c.status = "cancelled" if cancel else ("met" if at <= c.due_at else "missed")
        out.append(c)
    return out


def on_case_created(db: Session, case: Case, pack: RulePack) -> None:
    if pack.deadline("portal_registration"):
        start(db, case, "portal_registration", case.admitted_at, pack.deadline("portal_registration"))
    elif pack.deadline("intimation") and pack.deadline("intimation").from_ == "admission":
        start(db, case, "intimation", case.admitted_at, pack.deadline("intimation"))


def on_portal_registered(db: Session, case: Case, pack: RulePack, at: datetime) -> None:
    stop(db, case, ["portal_registration"], at)
    d = pack.deadline("intimation")
    if d and d.from_ == "portal_registration":
        start(db, case, "intimation", at, d)


def on_event(db: Session, case: Case, pack: RulePack, event: str, at: datetime, data: dict) -> None:
    qid = data.get("query_id")
    if event == "submit_preauth":
        stop(db, case, ["intimation", "portal_registration"], at)
        start(db, case, "payer_cashless_decision", at, pack.deadline("payer_cashless_decision"))
    elif event == "raise_preauth_query":
        start(db, case, "preauth_query_reply", data.get("raised_at", at), pack.deadline("preauth_query_reply"), qid)
    elif event == "reply_preauth_query":
        stop(db, case, ["preauth_query_reply"], at, query_id=qid)
    elif event in {"approve_preauth", "reject_preauth"}:
        stop(db, case, ["payer_cashless_decision"], at)
    elif event == "start_treatment":
        stop(db, case, ["intimation", "portal_registration"], at)
    elif event == "discharge":
        start(db, case, "claim_submission", case.discharged_at, pack.deadline("claim_submission"))
        start(db, case, "payer_final_authorisation", at, pack.deadline("payer_final_authorisation"))
    elif event == "submit_claim":
        stop(db, case, ["claim_submission"], at)
        stop(db, case, ["payer_final_authorisation"], at, cancel=True)
        due = pack.deadline("payment_due_portability") if case.portability else pack.deadline("payment_due")
        start(db, case, "payment_due", at, due)
    elif event == "raise_claim_query":
        raised = data.get("raised_at", at)
        start(db, case, "claim_query_reply", raised, pack.deadline("claim_query_reply"), qid)
        start(db, case, "claim_query_cutoff", raised, pack.deadline("claim_query_cutoff"), qid)
    elif event == "reply_claim_query":
        stop(db, case, ["claim_query_reply", "claim_query_cutoff"], at, query_id=qid)
    elif event == "payer_paid":
        stop(db, case, ["payment_due"], at)
        paid_on = data.get("paid_on_dt", at)
        d = Deadline.model_validate({"value": settings.bank_credit_expected_days, "unit": "days",
                                     "label": "Payment should reach the bank", "from": "payer_paid",
                                     "source": "Aeternix setting BANK_CREDIT_EXPECTED_DAYS"})
        start(db, case, "bank_credit_expected", paid_on, d)
    elif event in {"credit_matched", "short_paid"}:
        stop(db, case, ["bank_credit_expected", "payment_due"], at)
    elif event in {"close", "write_off", "reject", "reject_preauth"}:
        stop(db, case, [c.kind for c in running(db, case)], at, cancel=True)


def is_overdue(clock: Clock, now: datetime | None = None) -> bool:
    return clock.status == "running" and clock.due_at < (now or now_utc())
