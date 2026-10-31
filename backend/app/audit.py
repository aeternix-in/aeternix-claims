"""Append-only audit trail. Every write in the product goes through ``record``."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from .models import AuditLog


def _clean(v: Any) -> Any:
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


def snapshot(obj, fields: list[str]) -> dict:
    return {f: _clean(getattr(obj, f, None)) for f in fields}


def record(db: Session, *, hospital_id: str | None, actor, action: str, entity: str, entity_id: str | None,
           case_id: str | None = None, before: dict | None = None, after: dict | None = None) -> AuditLog:
    """``actor`` is a User, the string 'system', or None."""
    actor_id = getattr(actor, "id", None)
    actor_name = getattr(actor, "name", None) or (actor if isinstance(actor, str) else None)
    row = AuditLog(hospital_id=hospital_id, actor_id=actor_id, actor_name=actor_name, action=action,
                   entity=entity, entity_id=entity_id, case_id=case_id,
                   before=_clean(before) if before else None, after=_clean(after) if after else None)
    db.add(row)
    return row
