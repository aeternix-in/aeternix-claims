"""Authentication, roles and per-hospital scoping.

Every request resolves to a ``Ctx`` that carries exactly one ``hospital_id``.
Routers filter every query by it; nothing in the API takes a hospital id from
the request body.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db, scope_session
from .models import User

HOSPITAL_ROLES = {"desk", "accountant", "owner", "hospital_admin"}
AETERNIX_ROLES = {"clinician", "aeternix_admin"}
ALL_ROLES = HOSPITAL_ROLES | AETERNIX_ROLES

DESK_WRITE = {"desk", "owner", "hospital_admin", "aeternix_admin"}
BANK_WRITE = {"accountant", "owner", "hospital_admin", "aeternix_admin"}
CLINICAL = {"clinician", "aeternix_admin"}
ADMIN = {"hospital_admin", "aeternix_admin"}
SUPER = {"aeternix_admin"}

_ITER = 200_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITER)
    return f"pbkdf2_sha256${_ITER}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_b64, dk_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt_b64), int(iters))
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except (ValueError, TypeError):
        return False


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue_token(user_id: str, ttl_hours: int | None = None) -> str:
    payload = {"uid": user_id, "exp": int(time.time()) + 3600 * (ttl_hours or settings.token_ttl_hours)}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(settings.secret_key.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def read_token(token: str) -> str | None:
    try:
        body, sig = token.split(".")
        good = _b64(hmac.new(settings.secret_key.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            return None
        payload = json.loads(_unb64(body))
        if payload.get("exp", 0) < time.time():
            return None
        return payload.get("uid")
    except (ValueError, json.JSONDecodeError):
        return None


@dataclass
class Ctx:
    user: User
    hospital_id: str

    @property
    def role(self) -> str:
        return self.user.role

    @property
    def user_id(self) -> str:
        return self.user.id


def current_user(authorization: str | None = Header(default=None), db: Session = Depends(get_db)) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in to continue.")
    uid = read_token(authorization.split(" ", 1)[1].strip())
    user = db.get(User, uid) if uid else None
    if not user or not user.active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Your session has ended. Sign in again.")
    return user


def get_ctx(user: User = Depends(current_user), x_hospital_id: str | None = Header(default=None),
            db: Session = Depends(get_db)) -> Ctx:
    if user.role in HOSPITAL_ROLES:
        if not user.hospital_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "This account is not linked to a hospital.")
        if x_hospital_id and x_hospital_id != user.hospital_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "This account cannot open another hospital.")
        scope_session(db, user.hospital_id)
        return Ctx(user, user.hospital_id)
    # Aeternix staff choose a hospital per request.
    allowed = list(user.hospital_ids or [])
    if user.role == "aeternix_admin" and not allowed:
        from .models import Hospital
        allowed = [h.id for h in db.query(Hospital.id).all()]
    hid = x_hospital_id or (allowed[0] if len(allowed) == 1 else None)
    if not hid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Choose a hospital (X-Hospital-Id header).")
    if hid not in allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account has no access to that hospital.")
    scope_session(db, hid)
    return Ctx(user, hid)


def require(roles: set[str]):
    def dep(ctx: Ctx = Depends(get_ctx)) -> Ctx:
        if ctx.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Your role cannot do this.")
        return ctx
    return dep
