import os
import tempfile
from datetime import timedelta, timezone

import pytest

_TMP = tempfile.mkdtemp(prefix="aeternix-test-")
os.environ.setdefault("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", f"sqlite:///{_TMP}/test.db"))
os.environ["DATA_DIR"] = _TMP
os.environ["DEMO_MODE"] = "1"
os.environ["LLM_PROVIDER"] = "none"
os.environ["SECRET_KEY"] = "test-secret"

from app.db import SessionLocal, engine, init_db  # noqa: E402
from app.models import Base, Hospital, Payer, User  # noqa: E402
from app.money import now_utc  # noqa: E402
from app.security import hash_password  # noqa: E402


@pytest.fixture()
def db():
    Base.metadata.drop_all(engine)
    init_db()
    s = SessionLocal()
    yield s
    s.close()


@pytest.fixture()
def org(db):
    """One hospital with a CGHS and a PM-JAY payer and the usual users."""
    h = Hospital(name="Test Hospital", code="TST", digest_recipients=[{"channel": "console", "to": "x"}])
    db.add(h)
    db.flush()
    users = {}
    for role in ["desk", "owner", "accountant", "hospital_admin"]:
        users[role] = User(email=f"{role}@t.local", name=role.title(), role=role,
                           password_hash=hash_password("pw-1234567890"), hospital_id=h.id)
        db.add(users[role])
    users["clinician"] = User(email="clin@t.local", name="Clinician", role="clinician",
                              password_hash=hash_password("pw-1234567890"), hospital_ids=[h.id])
    db.add(users["clinician"])
    cghs = Payer(hospital_id=h.id, code="CGHS", name="CGHS", scheme="cghs", kind="government", portal="nha_tms",
                 narration_keywords=["CGHS", "PFMS"], expected_tds_bps=0)
    pmjay = Payer(hospital_id=h.id, code="PMJAY", name="PM-JAY", scheme="pmjay", kind="government", portal="nha_tms",
                  narration_keywords=["PMJAY", "SHA"], expected_tds_bps=0)
    echs = Payer(hospital_id=h.id, code="ECHS", name="ECHS", scheme="echs", kind="government", portal="utiitsl",
                 narration_keywords=["ECHS"], expected_tds_bps=1000)
    db.add_all([cghs, pmjay, echs])
    db.commit()
    return {"hospital": h, "users": users, "payers": {"CGHS": cghs, "PMJAY": pmjay, "ECHS": echs}}


def utc_iso(dt):
    return dt.replace(tzinfo=timezone.utc).isoformat()


def days_ago(n):
    return now_utc() - timedelta(days=n)
