"""JWT issue/verify + FastAPI dependencies. Identity comes ONLY from here (FR-2).
Data Flow: 'Authorization: Bearer <jwt>' header -> decode_token -> Identity -> every route/tool."""
import time
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings

ALGO = "HS256"
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
    """Raises jwt.InvalidTokenError on bad/expired token."""
    c = jwt.decode(token, settings.jwt_secret, algorithms=[ALGO],
                   options={"require": ["sub", "company_id", "role", "exp"]})
    return Identity(c["company_id"], c["sub"], c["role"])


def current_user(cred: HTTPAuthorizationCredentials = Depends(bearer)) -> Identity:
    if cred is None:
        raise HTTPException(401, "Please log in again.")
    try:
        return decode_token(cred.credentials)
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Please log in again.")


def current_manager(user: Identity = Depends(current_user)) -> Identity:
    if user.role != "manager":
        raise HTTPException(403, "Only a transport manager can do this.")
    return user
