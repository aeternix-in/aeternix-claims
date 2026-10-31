"""M8 Escalation and recovery: ageing rules that draft the letter a person sends.

Nothing here sends anything. Each rule drafts once per case and kind; a person
edits, sends, and records the reference number (grievance number, diary number).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from string import Template

from sqlalchemy.orm import Session

from . import audit, clocks
from .models import Case, Claim, Escalation, Hospital, Query
from .money import days_between, now_utc, rupees, to_ist
from .recon import case_money
from .rulepacks import get_pack
from .statemachine import OPEN_STATES, latest_decision

TEMPLATES = {
    "grievance": (
        "Payment pending: $payer claim $ref ($case_no)",
        """To: $addressed_to

The claim below was approved on $approved_on for $approved. As of $today, payment has not reached the
hospital ($days days after approval).

  Claim reference:    $ref
  Hospital case:      $case_no
  Admission:          $admitted   Discharge: $discharged
  Amount claimed:     $claimed
  Amount approved:    $approved

We request release of the payment, or the reason it is held.

$hospital, Insurance Desk
"""),
    "interest_claim": (
        "Interest on delayed payment: PM-JAY claim $ref ($case_no)",
        """To: $addressed_to

PM-JAY guidelines provide for payment within $due_days days of claim submission. This claim was submitted on
$submitted_on and payment was due by $due_on. As of $today it is $weeks full week(s) overdue.

  Claim reference:    $ref
  Hospital case:      $case_no
  Amount:             $approved
  Interest at $rate% of the claim amount per week of delay: $interest

We request release of the claim amount together with the interest due under the hospital's PM-JAY contract.
[Desk: confirm the interest clause and its trigger date in the hospital's contract before sending]

$hospital, Insurance Desk
"""),
    "condonation": (
        "Request for condonation of delay: $case_no",
        """To: $addressed_to

We request condonation of a delay of $days_late days in submitting the bill for the case below
(discharged on $discharged; the window is $window days).

  Hospital case:      $case_no
  Beneficiary ref:    $beneficiary_ref
  Amount:             $claimed

Reason for the delay: [Desk: state the reason, with any supporting document]

The complete claim pack is enclosed.

$hospital, Insurance Desk
"""),
    "reminder": (
        "Status of claim $ref ($case_no)",
        """To: $addressed_to

The claim below was submitted on $submitted_on and has shown no change for $days days.

  Claim reference:    $ref
  Hospital case:      $case_no
  Amount claimed:     $claimed

Please let us know its status, or any document still needed.

$hospital, Insurance Desk
"""),
    "dispute": (
        "Short payment on claim $ref ($case_no)",
        """To: $addressed_to

The amount credited for the claim below is less than the amount approved.

  Claim reference:    $ref
  Hospital case:      $case_no
  Approved:           $approved
  Credited to bank:   $credited
  Tax deducted:       $tax
  Shortfall:          $shortfall

Please release the balance or send the deduction details.

$hospital, Insurance Desk
"""),
    "appeal": (
        "Request to reconsider rejected claim $ref ($case_no)",
        """To: $addressed_to

The claim below was rejected with the reason: "$reason".

  Claim reference:    $ref
  Hospital case:      $case_no
  Amount claimed:     $claimed

Our response: [Desk or clinician: answer the rejection reason point by point, citing the enclosed pages]

We request that the claim be reprocessed.

$hospital, Insurance Desk
"""),
}

KIND_LABEL = {"grievance": "Grievance", "interest_claim": "Interest claim", "condonation": "Condonation request",
              "reminder": "Status reminder", "dispute": "Short-payment dispute", "appeal": "Appeal"}


def _fmt_date(dt) -> str:
    if dt is None:
        return "—"
    if isinstance(dt, datetime):
        return to_ist(dt).strftime("%d/%m/%Y")
    return dt.strftime("%d/%m/%Y")


def _last_final_claim(db: Session, case: Case) -> Claim | None:
    return (db.query(Claim).filter(Claim.case_id == case.id, Claim.stage == "final")
            .order_by(Claim.version.desc()).first())


def pmjay_interest(db: Session, case: Case, as_of: datetime | None = None) -> dict | None:
    pack = get_pack(case.payer.scheme)
    if not pack.interest or case.state not in {"APPROVED", "PART_APPROVED", "DISPUTED", "CLAIM_SUBMITTED",
                                               "CLAIM_QUERY"}:
        return None
    claim = _last_final_claim(db, case)
    if not claim or not claim.submitted_at:
        return None
    d = pack.deadline("payment_due_portability" if case.portability else "payment_due")
    if not d:
        return None
    as_of = as_of or now_utc()
    due = claim.submitted_at + timedelta(days=d.value)
    days_over = days_between(due, as_of)
    weeks = max(days_over // 7, 0)
    dec = latest_decision(db, case)
    amount = dec.approved_paise if dec and dec.decision != "rejected" else claim.amount_paise
    interest = amount * pack.interest.rate_per_week_pct * weeks // 100
    return {"due_days": int(d.value), "submitted_on": claim.submitted_at, "due_on": due, "days_overdue": max(days_over, 0),
            "weeks": int(weeks), "rate_pct": pack.interest.rate_per_week_pct, "amount_paise": amount,
            "interest_paise": int(interest), "source": pack.interest.source}


def _context(db: Session, case: Case, hospital: Hospital, now: datetime) -> dict:
    money = case_money(db, case)
    dec = latest_decision(db, case)
    claim = _last_final_claim(db, case)
    contacts = case.payer.escalation_contacts or []
    return {
        "payer": case.payer.name, "ref": case.portal_case_id or (claim.portal_ref if claim else None) or case.case_no,
        "case_no": case.case_no, "hospital": hospital.name, "today": _fmt_date(now),
        "admitted": _fmt_date(case.admitted_at), "discharged": _fmt_date(case.discharged_at),
        "claimed": rupees(money["claimed_paise"]), "approved": rupees(money["approved_paise"]),
        "credited": rupees(money["credited_paise"] or 0), "tax": rupees(money["tax_paise"] or 0),
        "shortfall": rupees(money["short_paid_paise"] or 0), "approved_on": _fmt_date(dec.decided_at if dec else None),
        "submitted_on": _fmt_date(claim.submitted_at if claim else None),
        "days": days_between(case.state_changed_at, now), "beneficiary_ref": case.beneficiary_ref or "—",
        "reason": (dec.rejection_reason if dec and dec.rejection_reason else "not stated"),
        "addressed_to": "; ".join(f"{c.get('role', '')} {c.get('name', '')} {c.get('email', '')}".strip()
                                  for c in contacts) or f"{case.payer.name} (add the escalation contact in Setup)",
    }


def _exists(db: Session, case: Case, kind: str, now: datetime) -> Escalation | None:
    rows = db.query(Escalation).filter(Escalation.case_id == case.id, Escalation.kind == kind).all()
    for r in rows:
        if r.status == "draft":
            return r
        if r.status == "sent" and r.sent_at and (now - r.sent_at).days < 15:
            return r
    return None


def draft(db: Session, case: Case, kind: str, now: datetime | None = None, extra: dict | None = None,
          actor="system") -> Escalation | None:
    now = now or now_utc()
    existing = _exists(db, case, kind, now)
    hospital = db.get(Hospital, case.hospital_id)
    ctx = {**_context(db, case, hospital, now), **(extra or {})}
    subject_t, body_t = TEMPLATES[kind]
    subject, body = Template(subject_t).safe_substitute(ctx), Template(body_t).safe_substitute(ctx)
    amount = (extra or {}).get("_amount_paise")
    if existing:
        if existing.status == "draft":  # keep the draft current (e.g. interest grows weekly)
            existing.body, existing.subject, existing.amount_paise = body, subject, amount
        return None
    e = Escalation(hospital_id=case.hospital_id, case_id=case.id, kind=kind, subject=subject, body=body,
                   addressed_to=ctx["addressed_to"], amount_paise=amount)
    db.add(e)
    audit.record(db, hospital_id=case.hospital_id, actor=actor, action="escalation.drafted", entity="escalation",
                 entity_id=e.id, case_id=case.id, after={"kind": kind})
    return e


def sweep(db: Session, hospital_id: str, now: datetime | None = None) -> list[Escalation]:
    now = now or now_utc()
    made: list[Escalation] = []
    for case in db.query(Case).filter(Case.hospital_id == hospital_id, Case.state.in_(OPEN_STATES)).all():
        pack = get_pack(case.payer.scheme)
        days = days_between(case.state_changed_at, now)
        stall = pack.stall_days.get(case.state)
        letters: list[Escalation | None] = []
        if case.state in {"APPROVED", "PART_APPROVED"}:
            info = pmjay_interest(db, case, now)
            if info and info["weeks"] >= 1:
                letters.append(draft(db, case, "interest_claim", now, {
                    "due_days": info["due_days"], "submitted_on": _fmt_date(info["submitted_on"]),
                    "due_on": _fmt_date(info["due_on"]), "weeks": info["weeks"], "rate": f"{info['rate_pct']:g}",
                    "interest": rupees(info["interest_paise"]), "_amount_paise": info["interest_paise"]}))
            if stall is not None and days > stall:
                letters.append(draft(db, case, "grievance", now))
        elif case.state == "CLAIM_SUBMITTED" and stall is not None and days > stall:
            letters.append(draft(db, case, "reminder", now))
        elif case.state == "LATE_CONDONATION" and not case.condonation_granted:
            window = pack.deadline("claim_submission")
            used = days_between(case.discharged_at, now) if case.discharged_at else 0
            slab = pack.condonation_slab(used)
            letters.append(draft(db, case, "condonation", now, {
                "days_late": max(used - int(window.value), 0) if window else used,
                "window": int(window.value) if window else "—",
                "addressed_to": slab.authority if slab else case.payer.name}))
        elif case.state == "SHORT_PAID":
            letters.append(draft(db, case, "dispute", now))
        elif case.state == "REJECTED":
            letters.append(draft(db, case, "appeal", now))
        made += [e for e in letters if e]
    db.flush()
    return made


def mark_sent(db: Session, esc: Escalation, actor, reference_no: str | None, body: str | None = None) -> Escalation:
    if body:
        esc.body = body
    if "[Desk" in esc.body or "[desk" in esc.body:
        raise ValueError("The letter still has a bracketed instruction. Complete or remove it before sending.")
    esc.status = "sent"
    esc.sent_at = now_utc()
    esc.sent_by = actor.id
    esc.reference_no = reference_no
    audit.record(db, hospital_id=esc.hospital_id, actor=actor, action="escalation.sent", entity="escalation",
                 entity_id=esc.id, case_id=esc.case_id, after={"reference_no": reference_no, "kind": esc.kind})
    db.flush()
    return esc


def overdue_queries(db: Session, hospital_id: str, now: datetime | None = None) -> list[Query]:
    now = now or now_utc()
    return db.query(Query).filter(Query.hospital_id == hospital_id, Query.status.in_(["open", "drafted",
                                                                                      "awaiting_clinician"]),
                                  Query.due_at.isnot(None), Query.due_at < now).all()


__all__ = ["sweep", "draft", "mark_sent", "pmjay_interest", "KIND_LABEL", "clocks"]
