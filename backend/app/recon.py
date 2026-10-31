"""M7 Bank reconciliation: match every credit in the hospital's account to claims.

Matching ladder, strictest first:
  1. exact reference — a payment advice or claim reference appears in the bank line
  2. amount + date + payer — one open claim whose expected net equals the credit
  3. one credit, many claims — a unique set of open claims for that payer sums to the credit
  4. a person decides — everything else waits in the unmatched queue

Nothing moves a case to Credited until a person confirms the allocation.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, timedelta

from sqlalchemy.orm import Session

from . import audit
from .config import settings
from .models import Allocation, BankTransaction, Case, Payer, PaymentAdvice, new_id
from .money import now_utc, parse_date, to_paise
from .statemachine import TransitionError, apply, approved_amount, expected_tds

MATCHABLE_STATES = ["APPROVED", "PART_APPROVED", "PAID_BY_PAYER", "DISPUTED"]

# ----------------------------------------------------------------- parsing

_COLS = {
    "date": ["txn date", "transaction date", "value date", "date", "posting date", "tran date"],
    "narration": ["narration", "description", "particulars", "remarks", "transaction remarks", "details"],
    "ref": ["ref no", "reference no", "chq/ref no", "cheque no", "chq no", "utr", "reference", "ref no./cheque no.",
            "chq./ref.no.", "transaction id"],
    "credit": ["credit", "deposit", "deposits", "credit amount", "cr amount", "amount (cr)", "credit(inr)"],
    "debit": ["debit", "withdrawal", "withdrawals", "debit amount", "dr amount", "amount (dr)"],
    "amount": ["amount", "transaction amount"],
    "drcr": ["dr/cr", "cr/dr", "type"],
}


def _norm(h: str) -> str:
    return re.sub(r"\s+", " ", (h or "").strip().lower().replace("\n", " "))


def _find_header(rows: list[list[str]]) -> tuple[int, dict[str, int]] | None:
    for i, row in enumerate(rows[:40]):
        cells = [_norm(c) for c in row]
        mapping: dict[str, int] = {}
        for key, names in _COLS.items():
            for j, c in enumerate(cells):
                if c in names or any(c.startswith(n) for n in names if len(n) > 4):
                    mapping.setdefault(key, j)
        if "date" in mapping and "narration" in mapping and ("credit" in mapping or "amount" in mapping):
            return i, mapping
    return None


def _rows_from_table(rows: list[list[str]]) -> list[dict]:
    found = _find_header(rows)
    if not found:
        raise ValueError("Could not find the header row. The statement needs date, narration and credit columns.")
    start, m = found
    out = []
    for row in rows[start + 1:]:
        def cell(k):
            j = m.get(k)
            return (row[j] if j is not None and j < len(row) else "") or ""
        d = parse_date(cell("date"))
        if not d:
            continue
        credit = to_paise(cell("credit")) if "credit" in m else None
        if credit is None and "amount" in m:
            amt = to_paise(cell("amount"))
            drcr = cell("drcr").strip().lower()
            credit = amt if amt and (not drcr or drcr.startswith("c")) else None
        if not credit or credit <= 0:
            continue
        out.append({"date": d, "amount_paise": credit, "narration": re.sub(r"\s+", " ", cell("narration")).strip(),
                    "reference_no": cell("ref").strip()})
    return out


def parse_statement(filename: str, data: bytes) -> list[dict]:
    name = filename.lower()
    if name.endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        rows = [[("" if v is None else (v.strftime("%d/%m/%Y") if hasattr(v, "strftime") else str(v))) for v in r]
                for r in wb.active.iter_rows(values_only=True)]
        return _rows_from_table(rows)
    if name.endswith(".pdf") or data[:4] == b"%PDF":
        import pdfplumber
        rows: list[list[str]] = []
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages:
                for table in page.extract_tables():
                    rows += [[c or "" for c in r] for r in table]
        return _rows_from_table(rows)
    text = data.decode("utf-8-sig", errors="replace")
    # Banks put titles and account details above the table, so pick the delimiter that
    # appears most often across lines rather than trusting the first line.
    lines = [ln for ln in text.splitlines()[:60] if ln.strip()]
    delim = max(",;\t|", key=lambda d: sum(ln.count(d) for ln in lines)) if lines else ","
    return _rows_from_table(list(csv.reader(io.StringIO(text), delimiter=delim)))


def import_statement(db: Session, hospital_id: str, account: str, filename: str, data: bytes, actor) -> dict:
    rows = parse_statement(filename, data)
    batch = new_id()
    added, dupes = [], 0
    for r in rows:
        exists = db.query(BankTransaction).filter_by(
            hospital_id=hospital_id, account=account, txn_date=r["date"], amount_paise=r["amount_paise"],
            narration=r["narration"][:500], reference_no=r["reference_no"][:80]).first()
        if exists:
            dupes += 1
            continue
        t = BankTransaction(hospital_id=hospital_id, account=account, txn_date=r["date"],
                            amount_paise=r["amount_paise"], narration=r["narration"][:500],
                            reference_no=r["reference_no"][:80], import_batch=batch)
        db.add(t)
        added.append(t)
    db.flush()
    audit.record(db, hospital_id=hospital_id, actor=actor, action="bank.import", entity="bank_batch", entity_id=batch,
                 after={"account": account, "file": filename, "added": len(added), "duplicates": dupes})
    results = match_all(db, hospital_id, actor, only=[t.id for t in added])
    return {"batch": batch, "added": len(added), "duplicates": dupes, **results}


# ----------------------------------------------------------------- matching

def _clean(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def _payers_in_narration(payers: list[Payer], narration: str) -> list[Payer]:
    n = narration.upper()
    hits = []
    for p in payers:
        words = [w.upper() for w in (p.narration_keywords or []) if w] or [p.code.upper()]
        if any(w in n for w in words):
            hits.append(p)
    return hits


def expected_net(db: Session, case: Case) -> int:
    return (approved_amount(db, case) or 0) - expected_tds(db, case)


def _open_cases(db: Session, hospital_id: str, payer_ids: list[str] | None = None) -> list[Case]:
    q = db.query(Case).filter(Case.hospital_id == hospital_id, Case.state.in_(MATCHABLE_STATES))
    if payer_ids:
        q = q.filter(Case.payer_id.in_(payer_ids))
    taken = {a.case_id for a in db.query(Allocation).filter(Allocation.hospital_id == hospital_id,
                                                            Allocation.status.in_(["proposed", "confirmed"])).all()}
    return [c for c in q.all() if c.id not in taken]


def _subset_sum(cands: list[tuple[str, int]], target: int, tol: int, limit: int = 24) -> list[list[str]]:
    """All subsets (size >= 2) of up to ``limit`` candidates whose amounts sum within tol of target.
    Stops after finding 2 solutions: we only act on a unique one."""
    cands = sorted(cands, key=lambda c: -c[1])[:limit]
    sols: list[list[str]] = []

    def rec(i: int, total: int, chosen: list[str]):
        if len(sols) >= 2:
            return
        if abs(total - target) <= tol and len(chosen) >= 2:
            sols.append(list(chosen))
            return
        if i >= len(cands) or total > target + tol:
            return
        cid, amt = cands[i]
        chosen.append(cid)
        rec(i + 1, total + amt, chosen)
        chosen.pop()
        rec(i + 1, total, chosen)

    rec(0, 0, [])
    return sols


def _propose(db: Session, txn: BankTransaction, items: list[tuple[Case, int, int]], method: str, conf: float,
             advice: PaymentAdvice | None = None) -> list[Allocation]:
    out = []
    for case, net, tax in items:
        a = Allocation(hospital_id=txn.hospital_id, bank_txn_id=txn.id, case_id=case.id, amount_paise=net,
                       tax_paise=tax, method=method, confidence=conf, status="proposed",
                       payment_advice_id=advice.id if advice else None)
        db.add(a)
        out.append(a)
    txn.status = "proposed"
    n = len(items)
    txn.match_note = {
        "exact_ref": "The payer's reference is in the narration",
        "amount_date": "Amount, date and payer line up with one open claim",
        "split": f"One credit covers {n} claims; the amounts add up exactly",
    }.get(method, f"{n} claim{'s' if n != 1 else ''} proposed")
    return out


def match_one(db: Session, txn: BankTransaction) -> str:
    tol = settings.recon_tolerance_paise
    hay = _clean(txn.narration) + "|" + _clean(txn.reference_no)
    payers = db.query(Payer).filter(Payer.hospital_id == txn.hospital_id).all()

    # 1. exact reference: payment advice
    for adv in db.query(PaymentAdvice).filter(PaymentAdvice.hospital_id == txn.hospital_id).all():
        ref = _clean(adv.reference_no)
        if len(ref) >= 6 and ref in hay:
            items = []
            for ln in adv.lines or []:
                case = db.get(Case, ln["case_id"])
                if case and case.state in MATCHABLE_STATES:
                    items.append((case, int(ln.get("amount_paise") or 0), int(ln.get("tds_paise") or 0)))
            if items:
                _propose(db, txn, items, "exact_ref", 0.98, adv)
                return "exact_ref"
    # 1b. exact reference: a claim or case reference in the narration
    for case in _open_cases(db, txn.hospital_id):
        for ref in filter(None, [case.portal_case_id, case.case_no]):
            r = _clean(ref)
            if len(r) >= 6 and r in hay:
                _propose(db, txn, [(case, txn.amount_paise, expected_tds(db, case))], "exact_ref", 0.95)
                return "exact_ref"

    named = _payers_in_narration(payers, txn.narration)
    cands = _open_cases(db, txn.hospital_id, [p.id for p in named] if named else None)
    window = timedelta(days=settings.recon_date_window_days)

    def in_window(c: Case) -> bool:
        anchor = c.state_changed_at.date()
        return anchor - window <= txn.txn_date <= anchor + timedelta(days=180)

    # 2. amount + date + payer
    single = [c for c in cands if abs(expected_net(db, c) - txn.amount_paise) <= tol and in_window(c)]
    if len(single) == 1 and named:
        c = single[0]
        _propose(db, txn, [(c, txn.amount_paise, expected_tds(db, c))], "amount_date", 0.8)
        return "amount_date"

    # 3. one credit, many claims (only when the payer is named in the narration)
    if named:
        pool = [(c.id, expected_net(db, c)) for c in cands if in_window(c) and expected_net(db, c) > 0]
        sols = _subset_sum(pool, txn.amount_paise, tol)
        if len(sols) == 1:
            chosen = [db.get(Case, cid) for cid in sols[0]]
            _propose(db, txn, [(c, expected_net(db, c), expected_tds(db, c)) for c in chosen], "split", 0.7)
            return "split"
        if len(sols) > 1:
            txn.match_note = "More than one set of claims adds up to this credit. Choose by hand."
            return "ambiguous"
    if len(single) > 1:
        txn.match_note = f"{len(single)} claims have this amount. Choose by hand."
        return "ambiguous"
    txn.match_note = "No claim matches yet." if named else "Payer not recognised in the narration."
    return "unmatched"


def match_all(db: Session, hospital_id: str, actor, only: list[str] | None = None) -> dict:
    q = db.query(BankTransaction).filter(BankTransaction.hospital_id == hospital_id,
                                         BankTransaction.status == "unmatched")
    if only:
        q = q.filter(BankTransaction.id.in_(only))
    counts: dict[str, int] = {}
    for t in q.order_by(BankTransaction.txn_date).all():
        r = match_one(db, t)
        counts[r] = counts.get(r, 0) + 1
        db.flush()
    return {"results": counts}


def confirm(db: Session, alloc: Allocation, actor) -> Allocation:
    if alloc.status != "proposed":
        raise ValueError("This match is not waiting for confirmation.")
    alloc.status = "confirmed"
    alloc.confirmed_by = actor.id
    alloc.confirmed_at = now_utc()
    _settle_case(db, db.get(Case, alloc.case_id), actor, alloc)
    txn = db.get(BankTransaction, alloc.bank_txn_id)
    remaining = db.query(Allocation).filter(Allocation.bank_txn_id == txn.id, Allocation.status == "proposed").count()
    if not remaining:
        txn.status = "matched"
    audit.record(db, hospital_id=alloc.hospital_id, actor=actor, action="bank.allocation_confirmed",
                 entity="allocation", entity_id=alloc.id, case_id=alloc.case_id,
                 after={"amount_paise": alloc.amount_paise, "method": alloc.method})
    db.flush()
    return alloc


def reject(db: Session, alloc: Allocation, actor) -> Allocation:
    alloc.status = "rejected"
    txn = db.get(BankTransaction, alloc.bank_txn_id)
    if not db.query(Allocation).filter(Allocation.bank_txn_id == txn.id,
                                       Allocation.status.in_(["proposed", "confirmed"])).count():
        txn.status = "unmatched"
        txn.match_note = "Match rejected. Choose by hand."
    audit.record(db, hospital_id=alloc.hospital_id, actor=actor, action="bank.allocation_rejected",
                 entity="allocation", entity_id=alloc.id, case_id=alloc.case_id)
    db.flush()
    return alloc


def allocate_manual(db: Session, txn: BankTransaction, lines: list[dict], actor) -> list[Allocation]:
    total = sum(to_paise(l.get("amount")) or 0 for l in lines)
    if total > txn.amount_paise:
        raise ValueError("The allocations add up to more than the credit.")
    out = []
    for l in lines:
        case = db.query(Case).filter(Case.hospital_id == txn.hospital_id, Case.id == l["case_id"]).first()
        if not case:
            raise ValueError("Case not found.")
        a = Allocation(hospital_id=txn.hospital_id, bank_txn_id=txn.id, case_id=case.id,
                       amount_paise=to_paise(l.get("amount")) or 0, tax_paise=to_paise(l.get("tax")) or 0,
                       method="manual", confidence=1.0, status="proposed")
        db.add(a)
        db.flush()
        out.append(confirm(db, a, actor))
    txn.status = "matched"
    db.flush()
    return out


def _settle_case(db: Session, case: Case, actor, alloc: Allocation) -> None:
    approved = approved_amount(db, case) or 0
    credited = sum(a.amount_paise + a.tax_paise for a in db.query(Allocation).filter(
        Allocation.case_id == case.id, Allocation.status == "confirmed").all())
    shortfall = approved - credited
    try:
        if shortfall > settings.recon_tolerance_paise:
            apply(db, case, "short_paid", actor, {"shortfall_paise": shortfall}, system=True)
        else:
            apply(db, case, "credit_matched", actor, {"credited_paise": credited}, system=True)
            apply(db, case, "close", actor, {"reason": "Credited in full"})
    except TransitionError:
        pass


def case_money(db: Session, case: Case) -> dict:
    from .statemachine import current_claim_amount, latest_decision
    d = latest_decision(db, case)
    allocs = db.query(Allocation).filter(Allocation.case_id == case.id, Allocation.status == "confirmed").all()
    advices = [a for a in db.query(PaymentAdvice).filter(PaymentAdvice.hospital_id == case.hospital_id).all()
               if any(l.get("case_id") == case.id for l in a.lines or [])]
    paid = sum(int(l.get("amount_paise") or 0) for a in advices for l in a.lines if l.get("case_id") == case.id)
    credited = sum(a.amount_paise for a in allocs)
    tax = sum(a.tax_paise for a in allocs)
    claimed = current_claim_amount(db, case)
    approved = d.approved_paise if d and d.decision != "rejected" else (0 if d else None)
    deductions = sum(x.get("amount_paise", 0) for x in (d.deductions or [])) if d else 0
    short = (approved - credited - tax) if approved is not None and allocs else None
    return {"billed_paise": case.billed_paise, "claimed_paise": claimed, "approved_paise": approved,
            "deductions_paise": deductions, "deductions": d.deductions if d else [],
            "paid_by_payer_paise": paid or None, "credited_paise": credited or None, "tax_paise": tax or None,
            "short_paid_paise": short if short and short > 0 else (0 if short is not None else None),
            "expected_tds_paise": expected_tds(db, case) if approved else None}


def unmatched_older_than(db: Session, hospital_id: str, days: int) -> list[BankTransaction]:
    cutoff: date = now_utc().date() - timedelta(days=days)
    return db.query(BankTransaction).filter(BankTransaction.hospital_id == hospital_id,
                                            BankTransaction.status == "unmatched",
                                            BankTransaction.txn_date <= cutoff).all()
