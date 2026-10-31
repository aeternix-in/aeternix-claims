"""M9 Owner view, the desk worklist, the payer scorecard and the pilot metrics."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import median

from sqlalchemy.orm import Session

from .config import settings
from .escalation import pmjay_interest
from .models import (Allocation, BankTransaction, Case, CheckResult, Claim, Clock, Decision, DeskActivity,
                     Escalation, Hospital, Payer, Query)
from .money import days_between, ist_today, now_utc, rupees, to_ist
from .recon import case_money
from .rulepacks import get_pack
from .statemachine import LABELS, OPEN_STATES, latest_run_status
from .tracker import stalled_cases

BUCKETS = {
    "A": ("Not yet submitted", True),
    "B": ("Late, needs condonation", True),
    "S": ("With payer, awaiting decision", True),
    "C": ("In query", True),
    "D": ("Deducted, short-paid or rejected", True),
    "E": ("Approved but unpaid", False),
    "F": ("Paid but not matched to a bank credit", True),
}
AGE_BANDS = [(0, 30, "0–30 days"), (31, 60, "31–60"), (61, 90, "61–90"), (91, 180, "91–180"), (181, 10**6, "180+")]


def buckets_for(db: Session, case: Case, money: dict | None = None) -> list[tuple[str, int]]:
    m = money or case_money(db, case)
    claimed = m["claimed_paise"] or case.billed_paise or 0
    approved = m["approved_paise"] or 0
    s = case.state
    if s in {"DISCHARGED", "READY_TO_SUBMIT"}:
        return [("A", claimed)]
    if s == "LATE_CONDONATION":
        return [("B", claimed)]
    if s == "CLAIM_SUBMITTED":
        return [("S", claimed)]
    if s == "CLAIM_QUERY":
        return [("C", claimed)]
    if s == "APPROVED":
        return [("E", approved)]
    if s == "PART_APPROVED":
        return [("E", approved), ("D", m["deductions_paise"] or max(claimed - approved, 0))]
    if s == "REJECTED":
        return [("D", claimed)] if case.discharged_at else []
    if s == "SHORT_PAID":
        return [("D", m["short_paid_paise"] or 0)]
    if s == "DISPUTED":
        return [("D", max(claimed - (m["credited_paise"] or 0) - (m["tax_paise"] or 0), 0))]
    if s == "PAID_BY_PAYER":
        return [("F", m["paid_by_payer_paise"] or approved)]
    return []


def age_days(case: Case, now: datetime) -> int:
    return days_between(case.discharged_at or case.admitted_at, now)


def dashboard(db: Session, hospital_id: str, now: datetime | None = None) -> dict:
    now = now or now_utc()
    totals = {k: 0 for k in BUCKETS}
    counts = {k: 0 for k in BUCKETS}
    by_payer: dict[str, dict] = {}
    ageing = {k: {b[2]: 0 for b in AGE_BANDS} for k in BUCKETS}
    oldest = []
    for case in db.query(Case).filter(Case.hospital_id == hospital_id, Case.state.in_(OPEN_STATES)).all():
        parts = [(b, a) for b, a in buckets_for(db, case) if a]
        if not parts:
            continue
        age = age_days(case, now)
        band = next(lbl for lo, hi, lbl in AGE_BANDS if lo <= age <= hi)
        p = by_payer.setdefault(case.payer.code, {"payer": case.payer.name, "code": case.payer.code,
                                                  **{k: 0 for k in BUCKETS}, "total": 0})
        for b, amt in parts:
            totals[b] += amt
            counts[b] += 1
            p[b] += amt
            p["total"] += amt
            ageing[b][band] += amt
        oldest.append({"case_id": case.id, "case_no": case.case_no, "payer": case.payer.code, "age_days": age,
                       "state": case.state, "state_label": LABELS[case.state],
                       "amount_paise": sum(a for _, a in parts), "buckets": [b for b, _ in parts]})
    unmatched = db.query(BankTransaction).filter(BankTransaction.hospital_id == hospital_id,
                                                 BankTransaction.status.in_(["unmatched", "proposed"])).all()
    oldest.sort(key=lambda r: (-r["age_days"], -r["amount_paise"]))
    return {
        "as_of": now.isoformat(),
        "buckets": [{"key": k, "label": BUCKETS[k][0], "controllable": BUCKETS[k][1], "amount_paise": totals[k],
                     "cases": counts[k]} for k in BUCKETS],
        "total_owed_paise": sum(totals.values()),
        "by_payer": sorted(by_payer.values(), key=lambda r: -r["total"]),
        "ageing": [{"bucket": k, "label": BUCKETS[k][0], "bands": [{"band": b, "amount_paise": v}
                                                                   for b, v in ageing[k].items()]} for k in BUCKETS],
        "oldest": oldest[:12],
        "unmatched_credits": {"count": len(unmatched), "amount_paise": sum(t.amount_paise for t in unmatched)},
    }


# ------------------------------------------------------------------ worklist

def worklist(db: Session, hospital_id: str, role: str, now: datetime | None = None) -> list[dict]:
    now = now or now_utc()
    items: list[dict] = []

    def add(kind, priority, case, title, due=None, amount=None, detail=None):
        items.append({"kind": kind, "priority": priority, "case_id": case.id if case else None,
                      "case_no": case.case_no if case else None, "payer": case.payer.code if case else None,
                      "state": case.state if case else None, "title": title, "detail": detail,
                      "due_at": due.isoformat() if due else None,
                      "overdue": bool(due and due < now), "amount_paise": amount})

    cases = {c.id: c for c in db.query(Case).filter(Case.hospital_id == hospital_id, Case.state.in_(OPEN_STATES)).all()}
    if role in {"desk", "owner", "hospital_admin", "aeternix_admin"}:
        for q in db.query(Query).filter(Query.hospital_id == hospital_id,
                                        Query.status.in_(["open", "drafted", "awaiting_clinician"])).all():
            c = cases.get(q.case_id)
            if not c:
                continue
            waiting = q.status == "awaiting_clinician"
            add("query", 0 if q.due_at and q.due_at < now else 1, c,
                "Clinician is reviewing the reply" if waiting else "Answer the payer's query",
                q.due_at, c.billed_paise, q.text[:140])
        for cl in db.query(Clock).filter(Clock.hospital_id == hospital_id, Clock.status == "running").all():
            c = cases.get(cl.case_id)
            if not c or cl.query_id:
                continue
            soon = cl.due_at - now <= timedelta(days=settings.claim_window_warning_days)
            if cl.kind == "claim_submission" and c.state == "LATE_CONDONATION":
                continue  # the condonation item below carries this case
            if cl.kind == "claim_submission" and soon:
                add("claim_window", 0 if cl.due_at < now else 2, c, "Submit the claim before the window closes",
                    cl.due_at, c.billed_paise)
            elif cl.kind in {"portal_registration", "intimation"} and cl.due_at - now <= timedelta(hours=24):
                add("registration", 0 if cl.due_at < now else 1, c, cl.label, cl.due_at)
            elif cl.kind == "bank_credit_expected" and cl.due_at < now:
                add("credit_missing", 2, c, "Payer shows paid but no bank credit is matched yet", cl.due_at,
                    c.billed_paise)
        for c in cases.values():
            if c.state == "LATE_CONDONATION":
                window = db.query(Clock).filter(Clock.case_id == c.id, Clock.kind == "claim_submission") \
                    .order_by(Clock.due_at.desc()).first()
                if c.condonation_granted:
                    add("condonation", 1, c, "Condonation granted: submit the claim", None, c.billed_paise)
                else:
                    add("condonation", 0, c, "Window closed: request condonation, then submit",
                        window.due_at if window else None, c.billed_paise)
            elif c.state in {"DISCHARGED", "READY_TO_SUBMIT"}:
                status, rows = latest_run_status(db, c)
                if status is None:
                    add("pack", 2, c, "Build the claim pack and run the check", None, c.billed_paise)
                elif status == "red":
                    n = sum(r.result == "red" for r in rows)
                    add("pack", 2, c, f"Fix {n} red flag{'s' if n > 1 else ''} in the claim check", None, c.billed_paise)
                elif status == "amber":
                    add("pack", 3, c, "Waiting for amber flags to be cleared", None, c.billed_paise)
                elif c.state == "DISCHARGED":
                    add("pack", 2, c, "Check is green: mark ready and submit", None, c.billed_paise)
                else:
                    add("submit", 1, c, "Submit on the portal", None, c.billed_paise)
            elif c.state == "TREATING" and c.discharged_at is None and c.counter_checks is not None:
                pass
        for c, days, limit in stalled_cases(db, hospital_id):
            add("stalled", 3, c, f"No movement for {days} days (limit {limit})", None, c.billed_paise,
                LABELS[c.state])
        for e in db.query(Escalation).filter(Escalation.hospital_id == hospital_id, Escalation.status == "draft").all():
            c = cases.get(e.case_id)
            if c:
                add("letter", 3, c, f"Review and send: {e.subject}", None, e.amount_paise)
    if role in {"accountant", "owner", "hospital_admin", "aeternix_admin"}:
        for t in db.query(BankTransaction).filter(BankTransaction.hospital_id == hospital_id,
                                                  BankTransaction.status.in_(["unmatched", "proposed"])).all():
            items.append({"kind": "bank", "priority": 2, "case_id": None, "case_no": None, "payer": None,
                          "state": None, "bank_txn_id": t.id,
                          "title": "Confirm the match" if t.status == "proposed" else "Match this credit",
                          "detail": f"{t.txn_date:%d/%m/%Y} {t.narration[:80]}", "due_at": None, "overdue": False,
                          "amount_paise": t.amount_paise})
    items.sort(key=lambda i: (i["priority"], i["due_at"] or "9999"))
    return items


# ------------------------------------------------------------------ scorecard

def scorecard(db: Session, hospital_id: str, month: str | None = None) -> list[dict]:
    today = ist_today()
    y, m = (int(x) for x in (month or f"{today.year}-{today.month:02d}").split("-"))
    start = datetime(y, m, 1)
    end = datetime(y + (m == 12), (m % 12) + 1, 1)
    out = []
    for p in db.query(Payer).filter(Payer.hospital_id == hospital_id).all():
        claims = db.query(Claim).join(Case, Claim.case_id == Case.id).filter(
            Case.payer_id == p.id, Claim.stage == "final", Claim.submitted_at >= start, Claim.submitted_at < end).all()
        decisions = db.query(Decision).join(Case, Decision.case_id == Case.id).filter(
            Case.payer_id == p.id, Decision.stage == "final", Decision.decided_at >= start, Decision.decided_at < end).all()
        queries = db.query(Query).join(Case, Query.case_id == Case.id).filter(
            Case.payer_id == p.id, Query.stage == "claim", Query.raised_at >= start, Query.raised_at < end).count()
        credited = db.query(Allocation).join(Case, Allocation.case_id == Case.id).filter(
            Case.payer_id == p.id, Allocation.status == "confirmed", Allocation.confirmed_at >= start,
            Allocation.confirmed_at < end).all()
        days_to_pay = []
        for a in credited:
            first = (db.query(Claim).filter(Claim.case_id == a.case_id, Claim.stage == "final")
                     .order_by(Claim.version).first())
            txn = db.get(BankTransaction, a.bank_txn_id)
            if first and first.submitted_at and txn:
                days_to_pay.append(days_between(first.submitted_at, txn.txn_date))
        claimed_sum = 0
        for d in decisions:
            cl = (db.query(Claim).filter(Claim.case_id == d.case_id, Claim.stage == "final")
                  .order_by(Claim.version.desc()).first())
            claimed_sum += cl.amount_paise if cl else 0
        deducted = sum(sum(x.get("amount_paise", 0) for x in d.deductions or []) for d in decisions)
        rejected = sum(1 for d in decisions if d.decision == "rejected")
        if not (claims or decisions or queries or credited):
            continue
        out.append({"payer": p.name, "code": p.code, "claims_submitted": len(claims), "decisions": len(decisions),
                    "median_days_to_pay": median(days_to_pay) if days_to_pay else None,
                    "deduction_rate": round(deducted / claimed_sum, 4) if claimed_sum else None,
                    "query_rate": round(queries / len(claims), 3) if claims else None,
                    "rejections": rejected})
    return out


# ------------------------------------------------------------------ digest

def digest_text(db: Session, hospital: Hospital, now: datetime | None = None) -> str:
    """Case IDs only. Never a patient name, diagnosis or a name tied to an amount."""
    now = now or now_utc()
    today = ist_today(now)
    y = today - timedelta(days=1)
    credits = (db.query(Allocation).join(BankTransaction, Allocation.bank_txn_id == BankTransaction.id)
               .filter(Allocation.hospital_id == hospital.id, Allocation.status == "confirmed",
                       BankTransaction.txn_date == y).all())
    dash = dashboard(db, hospital.id, now)
    wl = worklist(db, hospital.id, "owner", now)
    due_today = [i for i in wl if i["kind"] == "claim_window"]
    q_over = [i for i in wl if i["kind"] == "query" and i["overdue"]]
    q_due = [i for i in wl if i["kind"] == "query" and not i["overdue"]]
    lines = [f"Aeternix daily digest: {hospital.name}, {to_ist(now):%d %b %Y}", "",
             f"Credited yesterday: {rupees(sum(a.amount_paise for a in credits))} ({len(credits)} claims)",
             f"Claims to submit soon: {len(due_today)}" + (f" ({', '.join(i['case_no'] for i in due_today[:5])})" if due_today else ""),
             f"Queries: {len(q_over)} overdue, {len(q_due)} open",
             "", f"Money owed: {rupees(dash['total_owed_paise'])}"]
    for b in dash["buckets"]:
        if b["amount_paise"]:
            lines.append(f"  {b['key']} {b['label']}: {rupees(b['amount_paise'])} ({b['cases']})")
    if dash["unmatched_credits"]["count"]:
        lines.append(f"Unmatched bank credits: {dash['unmatched_credits']['count']} "
                     f"({rupees(dash['unmatched_credits']['amount_paise'])})")
    if dash["oldest"]:
        o = dash["oldest"][0]
        lines += ["", f"Oldest: {o['case_no']} ({o['payer']}, {o['age_days']} days, {o['state_label'].lower()})"]
    return "\n".join(lines)


# ------------------------------------------------------------------ pilot metrics

def pilot_metrics(db: Session, hospital_id: str, since: date | None = None, until: date | None = None) -> dict:
    since_dt = datetime.combine(since, datetime.min.time()) if since else datetime(2000, 1, 1)
    until_dt = datetime.combine(until, datetime.max.time()) if until else now_utc()
    finals = db.query(Claim).filter(Claim.hospital_id == hospital_id, Claim.stage == "final", Claim.version == 1,
                                    Claim.submitted_at >= since_dt, Claim.submitted_at <= until_dt).all()
    within, d2s = 0, []
    for cl in finals:
        case = db.get(Case, cl.case_id)
        if not case.discharged_at:
            continue
        d = days_between(case.discharged_at, cl.submitted_at)
        d2s.append(d)
        window = get_pack(case.payer.scheme).deadline("claim_submission")
        if window is None or d <= window.value:
            within += 1
    qs = db.query(Query).filter(Query.hospital_id == hospital_id, Query.raised_at >= since_dt,
                                Query.raised_at <= until_dt).all()
    answered = [q for q in qs if q.replied_at]
    reply_hours = [(q.replied_at - q.raised_at).total_seconds() / 3600 for q in answered]
    on_time = [q for q in answered if not q.due_at or q.replied_at <= q.due_at]
    decided = db.query(Decision).filter(Decision.hospital_id == hospital_id, Decision.stage == "final",
                                        Decision.decided_at >= since_dt, Decision.decided_at <= until_dt).all()
    first_pass = 0
    for d in decided:
        had_query = db.query(Query).filter(Query.case_id == d.case_id, Query.stage == "claim").count() > 0
        if d.decision == "approved" and not had_query:
            first_pass += 1
    allocs = db.query(Allocation).filter(Allocation.hospital_id == hospital_id, Allocation.status == "confirmed",
                                         Allocation.confirmed_at >= since_dt, Allocation.confirmed_at <= until_dt).all()
    recovered_old = 0
    for a in allocs:
        case = db.get(Case, a.case_id)
        if case.discharged_at and days_between(case.discharged_at, a.confirmed_at) > 90:
            recovered_old += a.amount_paise + a.tax_paise
    credits = db.query(BankTransaction).filter(BankTransaction.hospital_id == hospital_id,
                                               BankTransaction.txn_date >= since_dt.date(),
                                               BankTransaction.txn_date <= until_dt.date()).all()
    matched = [t for t in credits if t.status == "matched"]
    secs = db.query(DeskActivity).filter(DeskActivity.hospital_id == hospital_id, DeskActivity.at >= since_dt,
                                         DeskActivity.at <= until_dt).all()
    per_case: dict[str, int] = defaultdict(int)
    for s in secs:
        per_case[s.case_id] += s.seconds
    submitted_ids = {cl.case_id for cl in finals}
    mins = [per_case[c] / 60 for c in submitted_ids if c in per_case]
    return {
        "claims_submitted": len(finals),
        "share_within_window": round(within / len(finals), 3) if finals else None,
        "median_days_discharge_to_submission": median(d2s) if d2s else None,
        "queries_raised": len(qs),
        "median_query_reply_hours": round(median(reply_hours), 1) if reply_hours else None,
        "share_queries_on_time": round(len(on_time) / len(answered), 3) if answered else None,
        "first_pass_approval_rate": round(first_pass / len(decided), 3) if decided else None,
        "recovered_over_90_days_paise": recovered_old,
        "share_credits_matched": round(len(matched) / len(credits), 3) if credits else None,
        "desk_minutes_per_claim": round(median(mins), 1) if mins else None,
    }


def case_flags(db: Session, case: Case) -> dict:
    status, rows = latest_run_status(db, case)
    return {"check_status": status, "red": sum(r.result == "red" for r in rows),
            "amber_open": sum(r.result == "amber" and not r.cleared_at for r in rows),
            "interest": pmjay_interest(db, case)}


__all__ = ["dashboard", "worklist", "scorecard", "digest_text", "pilot_metrics", "buckets_for", "BUCKETS",
           "CheckResult", "case_flags"]
