"""FastAPI dependencies — Supabase JWT auth."""

from dataclasses import dataclass
from typing import Optional
from contextlib import contextmanager
from threading import Lock

from fastapi import Header, HTTPException, Request

from modules import db


@dataclass
class AuthUser:
    user_id: str
    token: str


def _parse_bearer(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authorization header must be: Bearer <token>",
        )
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Empty bearer token")
    return token


def require_db():
    if not db.is_configured():
        raise HTTPException(
            status_code=503,
            detail="Database not configured — set SUPABASE_URL and SUPABASE_ANON_KEY",
        )


_WRITERS = set()
_WRITERS_LOCK = Lock()


@contextmanager
def mutation_guard(user_id):
    # Prevent stale read/modify/write races across tabs, including deletion
    # racing a goal edit or coach request. One process/replica is required.
    with _WRITERS_LOCK:
        if user_id in _WRITERS:
            raise HTTPException(409, "Another change is processing. Wait for it to finish, then try again.")
        _WRITERS.add(user_id)
    try:
        yield
    finally:
        with _WRITERS_LOCK:
            _WRITERS.discard(user_id)


def get_current_user(request: Request, authorization: Optional[str] = Header(None)):
    require_db()
    token = _parse_bearer(authorization)
    try:
        user_id = db.get_user_id(token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    user = AuthUser(user_id=user_id, token=token)
    if request.method not in ('GET', 'HEAD', 'OPTIONS') or request.url.path == '/data/export':
        with mutation_guard(user_id):
            yield user
    else:
        yield user


def get_optional_user(authorization: Optional[str] = Header(None)) -> Optional[AuthUser]:
    if not authorization or not db.is_configured():
        return None
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authorization header must be: Bearer <token>",
        )
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        return None
    try:
        user_id = db.get_user_id(token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return AuthUser(user_id=user_id, token=token)
