"""Account-owned AI preference, checked against live Auth data, never JWT claims.

This preference controls provider transmission only. It grants no access to
financial records. RLS and Auth remain responsible for authorization.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock

CONSENT_VERSION = '2026-11-02'
_REQUEST = ContextVar('finio_ai_request', default=None)


class AISharingDisabled(RuntimeError):
    pass


class RequestPolicy:
    def __init__(self):
        self.loader = None
        self.loaded = False
        self.enabled = False
        self.available = False
        self.lock = Lock()

    def status(self):
        with self.lock:
            if not self.loaded:
                self.loaded = True
                try:
                    preference = self.loader() if self.loader else None
                    self.available = isinstance(preference, dict)
                    self.enabled = (self.available and preference.get('enabled') is True
                                    and preference.get('version') == CONSENT_VERSION)
                except Exception:
                    self.enabled = self.available = False
        return {'enabled': self.enabled, 'available': self.available,
                'consent_version': CONSENT_VERSION}


@contextmanager
def request_policy():
    token = _REQUEST.set(RequestPolicy())
    try:
        yield _REQUEST.get()
    finally:
        _REQUEST.reset(token)


def bind_user(user):
    policy = _REQUEST.get()
    if policy is None:
        return
    def load():
        from modules import db
        account = db.get_user(user.token)
        if account.id != user.user_id:
            return None
        metadata = account.user_metadata
        value = metadata.get('finio_ai', {}) if isinstance(metadata, dict) else {}
        return value if isinstance(value, dict) else {}
    policy.loader = load


def preference_status():
    policy = _REQUEST.get()
    return policy.status() if policy else {'enabled': False, 'available': False,
                                          'consent_version': CONSENT_VERSION}


def provider_allowed():
    policy = _REQUEST.get()
    # Existing CLI evaluation/indexing tools use their explicit operator key.
    # Every HTTP request receives a policy and defaults to deny.
    return True if policy is None else policy.status()['enabled']


class AIRequestMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        with request_policy():
            await self.app(scope, receive, send)
