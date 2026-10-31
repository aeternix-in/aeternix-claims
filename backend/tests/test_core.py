"""State machine, clocks, money helpers, rule packs, interest, escalations."""
from datetime import timedelta

import pytest

from app import escalation, services
from app.clocks import running
from app.models import Clock, Escalation
from app.money import parse_date, rupees, to_paise
from app.rulepacks import get_pack, rate_card_for
from app.statemachine import TransitionError, allowed_events, apply
from app.worker import run_once

from .conftest import days_ago, utc_iso


def make_case(db, org, payer="CGHS", admitted_days_ago=10, **kw):
    data = {"payer_id": org["payers"][payer].id, "beneficiary_name": "Test Patient", "beneficiary_ref": "SYN-1",
            "admitted_at": utc_iso(days_ago(admitted_days_ago)), "icd10": "N20.0", "planned_procedure": "PCNL"}
    data.update(kw)
    c = services.create_case(db, org["hospital"].id, data, org["users"]["desk"])
    c.billed_paise = 50_000_00
    db.flush()
    return c


def test_money_helpers():
    assert to_paise("1,23,456.70") == 12345670
    assert to_paise("Rs. 500") == 50000
    assert to_paise(12.5) == 1250
    assert to_paise("(100)") == -10000
    assert rupees(12345670) == "₹1,23,456.70"
    assert rupees(100000_00) == "₹1,00,000"
    assert parse_date("03-Oct-2026").isoformat() == "2026-10-03"
    assert parse_date("03/10/2026").isoformat() == "2026-10-03"


def test_rule_packs_load_and_cite_sources():
    cghs = get_pack("cghs")
    assert cghs.deadline("claim_submission").value == 15
    assert cghs.condonation_slab(30).authority.startswith("Additional Director")
    assert cghs.condonation_slab(60).authority == "Director, CGHS"
    assert cghs.condonation_slab(10) is None
    assert "implant_invoice" in cghs.docs_for("final", {"implant_used": True})
    assert "emergency_certificate" in cghs.docs_for("final", {"emergency": True})
    assert "referral_letter" not in cghs.docs_for("final", {"emergency": True})
    pm = get_pack("pmjay")
    assert pm.interest.rate_per_week_pct == 1.0
    assert {c.key for c in pm.counter_checks} == {"biometric_registration", "biometric_preauth", "biometric_discharge"}
    entries, meta = rate_card_for("cghs", None, None)
    assert meta["sample"] is True and "SAMPLE-URO-001" in entries


def test_state_machine_guards(db, org):
    c = make_case(db, org)
    desk = org["users"]["desk"]
    with pytest.raises(TransitionError, match="needs a pre-auth"):
        apply(db, c, "start_treatment", desk)
    with pytest.raises(TransitionError, match="portal reference"):
        apply(db, c, "submit_preauth", desk, {})
    apply(db, c, "submit_preauth", desk, {"portal_ref": "TMS1"})
    assert c.state == "PREAUTH_SUBMITTED" and c.portal_case_id == "TMS1"
    with pytest.raises(TransitionError, match="Cannot do"):
        apply(db, c, "submit_claim", desk, {"portal_ref": "X"})
    apply(db, c, "approve_preauth", desk)
    apply(db, c, "discharge", desk, {"discharged_at": utc_iso(days_ago(6))})
    with pytest.raises(TransitionError, match="Run the claim check"):
        apply(db, c, "mark_ready", desk)
    with pytest.raises(TransitionError, match="automatically"):
        apply(db, c, "credit_matched", desk)
    with pytest.raises(TransitionError, match="written off"):
        apply(db, c, "write_off", desk, {})
    assert {"event": "write_off", "label": "Written off", "button": "Write off", "target": "CLOSED"} in allowed_events(c)


def test_no_preauth_scheme_and_part_approval(db, org):
    c = make_case(db, org, payer="ECHS")
    desk = org["users"]["desk"]
    apply(db, c, "start_treatment", desk)
    apply(db, c, "discharge", desk, {"discharged_at": utc_iso(days_ago(2))})
    c.state = "READY_TO_SUBMIT"
    apply(db, c, "submit_claim", desk, {"portal_ref": "E1"}, system=True)
    apply(db, c, "approve", desk, {"approved_amount": 45000})  # lower than claimed -> part-approved
    assert c.state == "PART_APPROVED"
    from app.statemachine import latest_decision
    d = latest_decision(db, c)
    assert d.deductions[0]["amount_paise"] == 5000_00


def test_clocks_and_missed_window(db, org):
    c = make_case(db, org, admitted_days_ago=30)
    desk = org["users"]["desk"]
    reg = running(db, c, ["portal_registration"])[0]
    assert (reg.due_at - c.admitted_at) == timedelta(hours=48)
    apply(db, c, "submit_preauth", desk, {"portal_ref": "T"})
    apply(db, c, "approve_preauth", desk)
    apply(db, c, "discharge", desk, {"discharged_at": utc_iso(days_ago(20))})
    win = running(db, c, ["claim_submission"])[0]
    assert (win.due_at - c.discharged_at) == timedelta(days=15)
    db.commit()
    out = run_once(db)
    assert out["windows"] == 1
    db.refresh(c)
    assert c.state == "LATE_CONDONATION"
    esc = db.query(Escalation).filter(Escalation.case_id == c.id, Escalation.kind == "condonation").one()
    assert "Additional Director" in esc.addressed_to
    with pytest.raises(TransitionError, match="condonation"):
        apply(db, c, "submit_claim", desk, {"portal_ref": "C1"})
    apply(db, c, "grant_condonation", desk, {"reference_no": "AD/123"})
    apply(db, c, "submit_claim", desk, {"portal_ref": "C1"})
    assert c.state == "CLAIM_SUBMITTED"
    assert db.query(Clock).filter(Clock.case_id == c.id, Clock.kind == "claim_submission").first().status == "missed"


def test_pmjay_interest_and_letter(db, org):
    c = make_case(db, org, payer="PMJAY", admitted_days_ago=70)
    desk = org["users"]["desk"]
    apply(db, c, "submit_preauth", desk, {"portal_ref": "P1"})
    apply(db, c, "approve_preauth", desk)
    apply(db, c, "discharge", desk, {"discharged_at": utc_iso(days_ago(66))})
    c.state = "READY_TO_SUBMIT"
    apply(db, c, "submit_claim", desk, {"portal_ref": "P1C", "at": utc_iso(days_ago(60))}, system=True)
    apply(db, c, "approve", desk, {"approved_amount": 50000, "at": utc_iso(days_ago(50))})
    info = escalation.pmjay_interest(db, c)
    # due 15 days after submission -> 45 days overdue -> 6 full weeks at 1% of 50,000
    assert info["weeks"] == 6
    assert info["interest_paise"] == 3000_00
    made = escalation.sweep(db, org["hospital"].id)
    kinds = {e.kind for e in made}
    assert "interest_claim" in kinds and "grievance" in kinds
    letter = next(e for e in made if e.kind == "interest_claim")
    assert "₹3,000" in letter.body
    with pytest.raises(ValueError, match="bracketed"):
        escalation.mark_sent(db, letter, desk, "G-1")
    letter.body = letter.body.replace("[Desk: confirm the interest clause and its trigger date in the hospital's "
                                      "contract before sending]", "")
    escalation.mark_sent(db, letter, desk, "G-1")
    assert letter.status == "sent"
    assert escalation.sweep(db, org["hospital"].id) == [] or all(e.kind != "interest_claim"
                                                                  for e in escalation.sweep(db, org["hospital"].id))


def test_parse_date_none():
    assert parse_date("not a date") is None
