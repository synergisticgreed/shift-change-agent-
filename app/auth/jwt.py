"""JWT issue/verify + FastAPI dependencies. Identity comes ONLY from here (FR-2).
Data Flow: 'Authorization: Bearer <jwt>' header -> decode_token -> Identity -> every route/tool."""
import time
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from app.config import settings
from app.db import store
from app.db.schema import employees

ALGO = "HS256"
ROLES = ("employee", "manager")
LOGIN_AGAIN = "Please log in again."
bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Identity:
    company_id: str
    user_id: str
    role: str  # employee | manager


def issue_token(company_id: str, user_id: str, role: str = "employee", ttl_s: int = 3600) -> str:
    claims = {"sub": user_id, "company_id": company_id, "role": role,
              "exp": int(time.time()) + ttl_s}
    return jwt.encode(claims, settings.jwt_secret, algorithm=ALGO)


def decode_token(token: str) -> Identity:
    """Data Flow [2a]: raw JWT -> verify signature + expiry + claims -> Identity.
    Raises jwt.InvalidTokenError on anything wrong."""
    c = jwt.decode(token, settings.jwt_secret, algorithms=[ALGO],  # fixed algo: no "alg=none"
                   options={"require": ["sub", "company_id", "role", "exp"]})
    if c["role"] not in ROLES or not all(isinstance(c[k], str) for k in ("sub", "company_id")):
        raise jwt.InvalidTokenError("bad claims")
    return Identity(c["company_id"], c["sub"], c["role"])


def is_registered(who: Identity) -> bool:
    """Data Flow [2b]: Identity -> employees table (same company + same role) -> exists?"""
    q = select(employees.c.id).where(employees.c.company_id == who.company_id,
                                     employees.c.id == who.user_id,
                                     employees.c.role == who.role)
    with store.engine.connect() as conn:
        return conn.execute(q).first() is not None


def current_user(cred: HTTPAuthorizationCredentials = Depends(bearer)) -> Identity:
    """Data Flow [2]: FastAPI dependency. Header -> decode [2a] -> registered? [2b] -> Identity.
    Every failure is the same plain 401 (no hint about which check failed)."""
    if cred is None:
        raise HTTPException(401, LOGIN_AGAIN)
    try:
        who = decode_token(cred.credentials)
    except jwt.InvalidTokenError:
        raise HTTPException(401, LOGIN_AGAIN)
    if not is_registered(who):
        raise HTTPException(401, LOGIN_AGAIN)
    return who


def current_manager(user: Identity = Depends(current_user)) -> Identity:
    """Data Flow: current_user [2] -> role must be manager -> Identity (else 403)."""
    if user.role != "manager":
        raise HTTPException(403, "Only a transport manager can do this.")
    return user
