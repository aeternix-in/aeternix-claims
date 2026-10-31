"""Operator commands for a real deployment (the demo uses app.seed instead).

    python -m app.manage init
    python -m app.manage create-hospital --name "Epitome Kidney Urology Institute" --code EPI --schemes cghs,pmjay,echs
    python -m app.manage create-user --hospital EPI --email desk@hospital.in --name "Desk" --role desk
    python -m app.manage seed-demo            # demo hospital, only if it is not there yet
"""
from __future__ import annotations

import argparse
import secrets
import sys

from .db import SessionLocal, init_db, scope_session
from .models import Hospital, Payer, User
from .rulepacks import get_pack
from .security import hash_password

ROLES = ["desk", "owner", "accountant", "hospital_admin", "clinician", "aeternix_admin"]

# Payer rows created for each empanelment. Narration keywords are a starting point for bank matching;
# check them against the hospital's real statements in the first week and edit them in Setup.
STANDARD_PAYERS = {
    "cghs": ("CGHS", "CGHS (pensioners)", "government", "nha_tms", ["CGHS", "PFMS"]),
    "pmjay": ("PMJAY", "AB PM-JAY", "government", "nha_tms", ["PMJAY", "SHA", "AYUSHMAN"]),
    "echs": ("ECHS", "ECHS", "government", "utiitsl", ["ECHS"]),
    "dgehs": ("DGEHS", "DGEHS (Delhi Govt)", "government", "paper", ["DGEHS", "GNCTD"]),
    "department": ("DEPT", "Department / PSU panel", "department", "paper", []),
    "private_tpa": ("TPA", "Insurer / TPA", "private", "tpa", []),
}


def create_hospital(db, name: str, code: str, schemes: list[str]) -> Hospital:
    code = code.strip().upper()
    if db.query(Hospital).filter(Hospital.code == code).first():
        raise SystemExit(f"A hospital with code {code} already exists.")
    unknown = [s for s in schemes if s not in STANDARD_PAYERS]
    if unknown:
        raise SystemExit(f"Unknown scheme(s): {', '.join(unknown)}. Use: {', '.join(STANDARD_PAYERS)}")
    h = Hospital(name=name, code=code, empanelments=schemes, bank_accounts=[], digest_recipients=[])
    db.add(h)
    db.flush()
    for s in schemes:
        get_pack(s)  # fails loudly if the rule pack is missing
        pcode, pname, kind, portal, kws = STANDARD_PAYERS[s]
        db.add(Payer(hospital_id=h.id, code=pcode, name=pname, scheme=s, kind=kind, portal=portal,
                     narration_keywords=kws, expected_tds_bps=0, escalation_contacts=[]))
    return h


def create_user(db, hospital_code: str | None, email: str, name: str, role: str, password: str | None) -> tuple[User, str]:
    if role not in ROLES:
        raise SystemExit(f"Role must be one of: {', '.join(ROLES)}")
    email = email.strip().lower()
    if db.query(User).filter(User.email == email).first():
        raise SystemExit(f"{email} already has an account.")
    pw = password or secrets.token_urlsafe(12)
    if len(pw) < 10:
        raise SystemExit("Use a password of at least 10 characters.")
    u = User(email=email, name=name, role=role, password_hash=hash_password(pw))
    if hospital_code:
        h = db.query(Hospital).filter(Hospital.code == hospital_code.strip().upper()).first()
        if not h:
            raise SystemExit(f"No hospital with code {hospital_code}.")
        if role in {"clinician", "aeternix_admin"}:
            u.hospital_ids = [h.id]
        else:
            u.hospital_id = h.id
    elif role not in {"clinician", "aeternix_admin"}:
        raise SystemExit("Hospital staff need --hospital.")
    db.add(u)
    return u, pw


def seed_demo_if_needed(db):
    """Seed the synthetic demo hospital once per database.

    Returns the seed result when created, or None when the DEMO hospital
    already exists. The caller must set the cross-hospital scope first.
    """
    if db.query(Hospital).filter(Hospital.code == "DEMO").first():
        return None

    from .seed import seed
    return seed(db)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m app.manage")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create the tables")
    h = sub.add_parser("create-hospital")
    h.add_argument("--name", required=True)
    h.add_argument("--code", required=True)
    h.add_argument("--schemes", default="cghs,pmjay,echs", help="comma-separated: " + ",".join(STANDARD_PAYERS))
    u = sub.add_parser("create-user")
    u.add_argument("--hospital", help="hospital code (Aeternix staff may omit it)")
    u.add_argument("--email", required=True)
    u.add_argument("--name", required=True)
    u.add_argument("--role", required=True, choices=ROLES)
    u.add_argument("--password", help="omit to generate one, printed once")
    sub.add_parser("seed-demo", help="add the synthetic demo hospital if it is not there")
    a = ap.parse_args(argv)

    init_db()
    if a.cmd == "init":
        print("Tables ready.")
        return
    db = SessionLocal()
    scope_session(db, None, bypass=True)  # operator commands work across hospitals
    try:
        if a.cmd == "create-hospital":
            hosp = create_hospital(db, a.name, a.code, [s.strip() for s in a.schemes.split(",") if s.strip()])
            db.commit()
            print(f"Created {hosp.name} ({hosp.code}) with payers for: {', '.join(hosp.empanelments)}.")
        elif a.cmd == "create-user":
            user, pw = create_user(db, a.hospital, a.email, a.name, a.role, a.password)
            db.commit()
            print(f"Created {user.email} ({user.role}).")
            if not a.password:
                print(f"Password (shown once): {pw}")
        elif a.cmd == "seed-demo":
            out = seed_demo_if_needed(db)
            if out is None:
                print("Demo hospital already there; nothing to do.")
            else:
                print(f"Seeded {out['cases']} synthetic cases. Password for the demo users: {out['password']}")
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover
    main(sys.argv[1:])