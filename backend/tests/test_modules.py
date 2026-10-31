"""Scrubber, documents, queries, tracker, reconciliation, importer, email, digest, tenancy."""
import pytest

from app import queries, recon, reporting, scrubber, services, tracker
from app.adapters import get_adapter
from app.documents import classify, extract_fields
from app.importer import import_outstanding
from app.models import BankTransaction, Case, QueryLibraryEntry
from app.seed import seed
from app.statemachine import apply, latest_run_status

from .conftest import days_ago, utc_iso
from .test_core import make_case


@pytest.fixture()
def seeded(db):
    out = seed(db, with_documents=True)
    return out


def _case(db, no):
    return db.query(Case).filter(Case.case_no == no).one()


def test_seeded_scrubber_catches_planted_defects(db, seeded):
    clean, defects, name, unverified, echs = [_case(db, n) for n in seeded["demo_cases"]]
    assert latest_run_status(db, clean)[0] == "green"
    _, rows = latest_run_status(db, defects)
    reds = {(r.rule, r.message.split(" ")[0]) for r in rows if r.result == "red"}
    rules = {r.rule for r in rows if r.result == "red"}
    assert {"required_documents", "date_consistency", "bill_vs_rate_card", "signatures"} <= rules, reds
    _, rows = latest_run_status(db, name)
    assert any(r.rule == "identity_match" and r.result == "red" for r in rows)
    assert any(r.rule == "diagnosis_supports_procedure" and r.needs_clinician for r in rows)
    _, rows = latest_run_status(db, unverified)
    assert any(r.rule == "fields_verified" for r in rows)
    assert any(r.rule == "counter_checks" for r in rows)
    assert latest_run_status(db, echs)[0] == "amber"


def test_seeded_bank_ladder_and_settlement(db, seeded):
    assert seeded["bank"]["results"] == {"exact_ref": 2, "split": 1, "amount_date": 1, "unmatched": 1}
    split = db.query(BankTransaction).filter(BankTransaction.narration.like("%HCO BILLS%")).one()
    from app.models import Allocation
    allocs = db.query(Allocation).filter(Allocation.bank_txn_id == split.id).all()
    assert len(allocs) == 3 and sum(a.amount_paise for a in allocs) == split.amount_paise
    acct = db.query(__import__("app.models", fromlist=["User"]).User).filter_by(role="accountant").one()
    for a in allocs:
        recon.confirm(db, a, acct)
    for a in allocs:
        assert db.get(Case, a.case_id).state == "CLOSED"
    assert split.status == "matched"


def test_document_classifier_and_extractor():
    text = ("FINAL BILL\nBill No: DH/26/1\nBill Date: 06/10/2026\nPatient Name: Ram Lal\n"
            "SAMPLE-URO-001 PCNL package 1 45000.00\nSAMPLE-RAD-001 CT KUB 1 2500.00\nTotal: 47500.00")
    assert classify(text)[0] == "final_bill"
    f = extract_fields("final_bill", text)
    assert f["total_amount"]["value"] == 47500_00
    assert f["bill_no"]["value"] == "DH/26/1"
    assert [l["code"] for l in f["bill_lines"]["value"]] == ["SAMPLE-URO-001", "SAMPLE-RAD-001"]
    ds = "DISCHARGE SUMMARY\nPatient Name: Ram Lal\nDiagnosis: Left renal calculus (N20.0)\nSignature: ______"
    f = extract_fields("discharge_summary", ds)
    assert f["icd10"]["value"] == "N20.0" and f["signed"]["value"] is False


def test_names_match():
    assert scrubber.names_match("Shri Ram P. Sharma", "Ram Prakash Sharma")
    assert scrubber.names_match("RAM PRAKASH SHARMA", "Ram Prakash Sharma")
    assert not scrubber.names_match("Suresh Chand", "Suresh Chandra Gupta")


def test_query_desk_flow(db, org):
    c = make_case(db, org, admitted_days_ago=20)
    desk, clin = org["users"]["desk"], org["users"]["clinician"]
    apply(db, c, "submit_preauth", desk, {"portal_ref": "T"})
    apply(db, c, "approve_preauth", desk)
    apply(db, c, "discharge", desk, {"discharged_at": utc_iso(days_ago(15))})
    c.state = "READY_TO_SUBMIT"
    apply(db, c, "submit_claim", desk, {"portal_ref": "C"}, system=True)
    assert queries.classify("Kindly submit the implant invoice and sticker")[0] == "missing_document"
    assert queries.classify("Justify the prolonged length of stay")[0] == "medical_necessity"
    assert queries.classify("Package rate is higher than CGHS rate")[0] == "rate"
    q = queries.raise_query(db, c, "Justify the indication for PCNL with clinical notes", desk)
    assert c.state == "CLAIM_QUERY" and q.status == "awaiting_clinician" and q.due_at is not None
    with pytest.raises(ValueError, match="clinician"):
        queries.reply(db, q, desk, "text", [])
    queries.signoff(db, q, clin, "Clinical justification: stone 18 mm with hydronephrosis; PCNL indicated.")
    with pytest.raises(ValueError, match="bracketed"):
        queries.reply(db, q, desk, "Answer [Desk: fill this]", [])
    queries.reply(db, q, desk, q.draft_reply, [])
    assert c.state == "CLAIM_SUBMITTED"
    queries.record_outcome(db, q, desk, "cleared")
    assert db.query(QueryLibraryEntry).count() == 1


def test_tracker_maps_portal_words(db, org):
    a = get_adapter("nha_tms")
    assert a.event_for("Claim Approved", "CLAIM_SUBMITTED") == ("approved", "approve")
    assert a.event_for("Preauth Approved", "PREAUTH_SUBMITTED") == ("approved", "approve_preauth")
    assert a.event_for("Query Raised", "CLAIM_SUBMITTED") == ("query", "raise_claim_query")
    assert a.event_for("Query Replied", "CLAIM_SUBMITTED")[1] is None
    assert a.event_for("Partially approved", "CLAIM_QUERY") == ("part_approved", "part_approve")
    assert a.event_for("Payment Done", "APPROVED") == ("paid", "payer_paid")
    c = make_case(db, org, admitted_days_ago=20)
    desk = org["users"]["desk"]
    apply(db, c, "submit_preauth", desk, {"portal_ref": "T"})
    apply(db, c, "approve_preauth", desk)
    apply(db, c, "discharge", desk, {"discharged_at": utc_iso(days_ago(15))})
    c.state = "READY_TO_SUBMIT"
    apply(db, c, "submit_claim", desk, {"portal_ref": "CLM-9"}, system=True)
    s = tracker.ingest_snapshot(db, org["hospital"].id, "nha_tms",
                                {"portal_ref": "CLM-9", "status_text": "Claim Approved", "approved_amount": "48,000"}, desk)
    assert s.applied == f"Case {c.case_no} is now 'Part-approved'." and c.state == "PART_APPROVED"
    s2 = tracker.ingest_snapshot(db, org["hospital"].id, "nha_tms",
                                 {"portal_ref": "CLM-9", "status_text": "Claim Approved", "approved_amount": "48,000"}, desk)
    assert s2.changed is False
    s3 = tracker.ingest_snapshot(db, org["hospital"].id, "nha_tms", {"portal_ref": "UNKNOWN", "status_text": "Paid"}, desk)
    assert s3.case_id is None


def test_email_parser():
    p = tracker.parse_email("Payment released for claim CLM-9. UTR No: SBIN12345678901 amount Rs. 48,000.00",
                            subject="Settlement advice")
    assert p["kind"] == "settlement" and p["payment_ref"] == "SBIN12345678901" and 48000_00 in p["amounts_paise"]


def test_statement_parser_variants():
    csv1 = b"Some bank\nTxn Date,Description,Ref No./Cheque No.,Debit,Credit\n03/10/2026,NEFT CR CGHS,UTR1,,1000.00\n04/10/2026,ATM,,500,\n"
    rows = recon.parse_statement("a.csv", csv1)
    assert len(rows) == 1 and rows[0]["amount_paise"] == 1000_00
    csv2 = b"Date;Particulars;Amount;Dr/Cr\n2026-10-03;NEFT CGHS;1,500.50;CR\n2026-10-03;FEE;10;DR\n"
    rows = recon.parse_statement("b.csv", csv2)
    assert len(rows) == 1 and rows[0]["amount_paise"] == 1500_50
    assert recon._subset_sum([("a", 100), ("b", 200), ("c", 300)], 500, 0) == [["c", "b"]]


def test_importer(db, org):
    data = ("Claim No,Patient Name,Scheme,Admission Date,Discharge Date,Claim Amount,Approved Amount,Status\n"
            "TMS-1,A One,CGHS,01/08/2026,04/08/2026,50000,,Under process\n"
            "TMS-2,B Two,CGHS,01/07/2026,03/07/2026,40000,36000,Approved\n"
            "TMS-3,C Three,PMJAY,01/09/2026,02/09/2026,20000,,Query raised\n"
            "TMS-4,D Four,UNKNOWN,01/09/2026,02/09/2026,20000,,Paid\n").encode()
    res = import_outstanding(db, org["hospital"].id, "list.csv", data, org["users"]["desk"], None, services.next_case_no)
    assert res["created"] == 3 and len(res["errors"]) == 1
    states = {c.portal_case_id: c.state for c in db.query(Case).all()}
    assert states == {"TMS-1": "CLAIM_SUBMITTED", "TMS-2": "PART_APPROVED", "TMS-3": "CLAIM_QUERY"}


def test_digest_has_no_patient_names(db, seeded):
    from app.models import Hospital
    h = db.query(Hospital).one()
    text = reporting.digest_text(db, h)
    names = [c.beneficiary_name for c in db.query(Case).all()]
    assert "Money owed" in text
    assert not any(n in text for n in names)
    dash = reporting.dashboard(db, h.id)
    assert dash["total_owed_paise"] > 0
    assert {b["key"] for b in dash["buckets"] if b["amount_paise"]} >= {"A", "B", "C", "D", "E", "F", "S"}
