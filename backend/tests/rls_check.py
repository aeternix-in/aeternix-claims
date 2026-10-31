"""Manual check that Postgres row-level security isolates hospitals (not part of the pytest run).

    OWNER_URL=postgresql+psycopg://postgres@/aeternix_rls?host=/tmp&port=5433 \
    APP_URL=postgresql+psycopg://aeternix_app:apppass@/aeternix_rls?host=/tmp&port=5433 \
    python -m tests.rls_check
"""
import os
import subprocess
import sys

owner, app_url = os.environ["OWNER_URL"], os.environ["APP_URL"]

# 1. create tables and seed two hospitals as the owner
os.environ["DATABASE_URL"] = owner
os.environ["DATA_DIR"] = "/tmp/aeternix-rls-data"
from sqlalchemy import create_engine, text  # noqa: E402

from app.models import Base, Hospital, Payer, User  # noqa: E402
from app.security import hash_password  # noqa: E402

eng = create_engine(owner)
Base.metadata.drop_all(eng)
Base.metadata.create_all(eng)
from sqlalchemy.orm import Session  # noqa: E402

with Session(eng) as s:
    for code in ["AAA", "BBB"]:
        h = Hospital(name=f"Hospital {code}", code=code)
        s.add(h)
        s.flush()
        s.add(Payer(hospital_id=h.id, code="CGHS", name="CGHS", scheme="cghs", kind="government", portal="nha_tms"))
        s.add(User(email=f"desk@{code.lower()}.local", name="Desk", role="desk", hospital_id=h.id,
                   password_hash=hash_password("pw-1234567890")))
    s.commit()
psql_url = owner.replace("postgresql+psycopg://", "postgresql://")
r = subprocess.run(["psql", psql_url, "-v", "app_password=apppass", "-v", "ON_ERROR_STOP=1", "-q",
                    "-f", "sql/rls.sql"], capture_output=True, text=True)
print("rls.sql:", "ok" if r.returncode == 0 else r.stderr)
if r.returncode:
    sys.exit(1)

# 2. talk to the API as the restricted role
os.environ["DATABASE_URL"] = app_url
for m in [k for k in sys.modules if k.startswith("app")]:
    del sys.modules[m]
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

c = TestClient(app)


def login(email):
    return {"Authorization": "Bearer " + c.post("/api/auth/login", json={"email": email, "password": "pw-1234567890"}).json()["token"]}


a, b = login("desk@aaa.local"), login("desk@bbb.local")
pa = c.get("/api/payers", headers=a).json()[0]["id"]
r = c.post("/api/cases", headers=a, json={"payer_id": pa, "beneficiary_name": "Only A"})
assert r.status_code == 200, r.text
cid = r.json()["id"]
assert len(c.get("/api/cases", headers=a).json()) == 1
assert c.get("/api/cases", headers=b).json() == []
assert c.get(f"/api/cases/{cid}", headers=b).status_code == 404
# even raw SQL as the app role sees nothing without the hospital setting
with create_engine(app_url).connect() as conn:
    assert conn.execute(text("select count(*) from claim_case")).scalar() == 0
    conn.execute(text("select set_config('app.hospital_id', :h, false)"),
                 {"h": c.get("/api/hospital", headers=a).json()["id"]})
    assert conn.execute(text("select count(*) from claim_case")).scalar() == 1
    try:
        conn.execute(text("update audit_log set action='x'"))
        print("FAIL: audit_log was updatable")
        sys.exit(1)
    except Exception:
        print("audit_log is append-only for the app role: ok")
print("RLS isolation: ok")
