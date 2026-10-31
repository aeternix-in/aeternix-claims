"""Background worker: run ``python -m app.worker``. One process is enough for many hospitals.

Every minute it: fires missed claim windows, drafts escalation letters, and sends each
hospital's owner digest once a day at DIGEST_HOUR (IST). Jobs are rows in the ``job``
table, keyed so a restart never sends the same digest twice.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import escalation
from .config import settings
from .db import SessionLocal, init_db, scope_session
from .models import Case, Clock, Hospital, Job
from .money import now_utc, to_ist
from .notify import send
from .reporting import digest_text
from .statemachine import TransitionError, apply

log = logging.getLogger("aeternix.worker")


def fire_missed_windows(db: Session, now: datetime) -> int:
    n = 0
    rows = db.query(Clock).filter(Clock.kind == "claim_submission", Clock.status == "running", Clock.due_at < now).all()
    for clock in rows:
        case = db.get(Case, clock.case_id)
        if case and case.state in {"DISCHARGED", "READY_TO_SUBMIT"}:
            try:
                apply(db, case, "window_passed", "system", {}, system=True)
                n += 1
            except TransitionError:
                pass
    return n


def _claim_job(db: Session, key: str, kind: str) -> bool:
    try:
        with db.begin_nested():
            db.add(Job(kind=kind, key=key, status="running"))
        return True
    except IntegrityError:
        return False


def send_digests(db: Session, now: datetime) -> int:
    local = to_ist(now)
    if local.hour < settings.digest_hour:
        return 0
    sent = 0
    for h in db.query(Hospital).all():
        key = f"digest:{h.id}:{local.date().isoformat()}"
        if not h.digest_recipients or not _claim_job(db, key, "digest"):
            continue
        text = digest_text(db, h, now)
        results = [send(r.get("channel", "console"), r.get("to", ""), f"Aeternix digest: {h.name}", text)
                   for r in h.digest_recipients]
        job = db.query(Job).filter(Job.key == key).one()
        job.status = "done" if all(r.get("ok") for r in results) else "failed"
        job.payload = {"results": results}
        job.finished_at = now_utc()
        sent += 1
    return sent


def run_once(db: Session, now: datetime | None = None) -> dict:
    now = now or now_utc()
    scope_session(db, None, bypass=True)  # the worker sweeps every hospital
    out = {"windows": fire_missed_windows(db, now), "letters": 0, "digests": 0}
    for h in db.query(Hospital).all():
        out["letters"] += len(escalation.sweep(db, h.id, now))
    out["digests"] = send_digests(db, now)
    db.commit()
    return out


def main() -> None:  # pragma: no cover - loop
    logging.basicConfig(level=logging.INFO)
    init_db()
    while True:
        db = SessionLocal()
        try:
            log.info("sweep %s", run_once(db))
        except Exception:
            log.exception("sweep failed")
            db.rollback()
        finally:
            db.close()
        time.sleep(60)


if __name__ == "__main__":  # pragma: no cover
    main()
