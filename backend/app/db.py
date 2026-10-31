"""Database engine and sessions. SQLite for a laptop, Postgres in production."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import settings


def _make_engine(url: str):
    kwargs: dict = {"future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if url.startswith("sqlite:///./") or url.startswith("sqlite:///"):
            path = url.replace("sqlite:///", "", 1)
            if path and path != ":memory:":
                from pathlib import Path
                Path(path).parent.mkdir(parents=True, exist_ok=True)
    eng = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(eng, "connect")
        def _fk_on(dbapi_conn, _):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    return eng


engine = _make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=True, expire_on_commit=False, future=True)


IS_POSTGRES = settings.database_url.startswith("postgresql")


def scope_session(db: Session, hospital_id: str | None, bypass: bool = False) -> None:
    """Tell Postgres which hospital this connection may see (enforced by sql/rls.sql).
    Session-level, so it survives the commits inside a request; get_db resets it."""
    if not IS_POSTGRES:
        return
    from sqlalchemy import text
    db.execute(text("SELECT set_config('app.hospital_id', :h, false), set_config('app.bypass_rls', :b, false)"),
               {"h": hospital_id or "", "b": "on" if bypass else "off"})


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        if IS_POSTGRES:
            try:
                db.rollback()
                scope_session(db, None, bypass=False)
                db.commit()
            except Exception:  # pragma: no cover - connection already gone
                pass
        db.close()


def init_db() -> None:
    from . import models  # noqa: F401  (register tables)
    models.Base.metadata.create_all(engine)
