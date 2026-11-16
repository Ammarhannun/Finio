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
    if len(authorization) > 8192 or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authorization header must be: Bearer <token>",
        )
    token = authorization[7:].strip()
    if not token or any(char.isspace() for char in token):
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


def authenticate(authorization: Optional[str]) -> AuthUser:
    token = _parse_bearer(authorization)
    require_db()
    try:
        user_id = db.get_user_id(token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return AuthUser(user_id=user_id, token=token)


def get_current_user(request: Request, authorization: Optional[str] = Header(None)):
    user = getattr(request.state, 'upload_user', None) or authenticate(authorization)
    from modules.ai_policy import bind_user
    bind_user(user)
    user_id = user.user_id
    if request.method not in ('GET', 'HEAD', 'OPTIONS') or request.url.path in ('/data/export', '/insight'):
        with mutation_guard(user_id):
            yield user
    else:
        yield user


def get_optional_user(authorization: Optional[str] = Header(None)) -> Optional[AuthUser]:
    if not authorization or not db.is_configured():
        return None
    token = _parse_bearer(authorization)
    try:
        user_id = db.get_user_id(token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return AuthUser(user_id=user_id, token=token)


def require_sensitive_access(user: AuthUser):
    """Fail closed if a live session, MFA or recent identity check is missing."""
    try:
        state = db.security_status(db.get_client(user.token))
    except Exception:
        raise HTTPException(503, detail={'code': 'security_unavailable',
            'message': 'Could not verify account security. Please try again later.'}) from None
    if not state['session_active']:
        raise HTTPException(401, 'Your session has ended. Please sign in again.')
    if not state['mfa_satisfied']:
        raise HTTPException(403, detail={'code': 'mfa_required',
            'message': 'Verify your authenticator code to continue.'})
    if not state['recent_auth']:
        raise HTTPException(403, detail={'code': 'reauth_required',
            'message': 'Confirm your identity again before this action.'})
