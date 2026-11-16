"""Public privacy requests, delivered only to the operator's private inbox.

No database or AI access. Configuration stays on the server. Turnstile is
validated before delivery; requests never delete data or establish account ownership.
The in-memory abuse limits require one worker and one replica.
"""
from collections import deque
import os
import re
import threading
import time
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from modules.ai_privacy import redact_credentials

router = APIRouter(prefix='/privacy/contact', tags=['privacy'])
_TOPICS = {'access': 'Access or export', 'delete': 'Deletion',
           'correct': 'Correction', 'account': 'Account access',
           'question': 'Privacy question'}
_ADDRESS = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
                      r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}")
_TEST_KEYS = {'1x0000000000000000000000000000000AA',
              '2x0000000000000000000000000000000AA',
              '3x0000000000000000000000000000000AA'}
_UNAVAILABLE = 'The request form is unavailable. Please try again later.'
_UNCONFIRMED = 'We could not confirm delivery. Please try again later.'
_LOCK = threading.Lock()
_ATTEMPTS = deque()
_CLIENTS = {}
_SEND_SLOT = threading.BoundedSemaphore(1)


class PrivacyContact(BaseModel):
    model_config = ConfigDict(extra='forbid')
    email: StrictStr = Field(min_length=3, max_length=254)
    topic: Literal['access', 'delete', 'correct', 'account', 'question']
    message: StrictStr = Field(min_length=10, max_length=1500)
    token: StrictStr = Field(min_length=1, max_length=2048)
    website: StrictStr = Field(default='', max_length=200)

    @field_validator('email')
    @classmethod
    def single_address(cls, value):
        if not _ADDRESS.fullmatch(value):
            raise ValueError('Use a single email address.')
        return value

    @field_validator('message')
    @classmethod
    def message_text(cls, value):
        if len(value.strip()) < 10 or any(ord(c) < 32 and c not in '\n\t\r' for c in value):
            raise ValueError('Use plain text.')
        return value.strip()


def _configuration():
    values = {name: os.getenv(name, '') for name in (
        'FINIO_CONTACT_TO', 'FINIO_CONTACT_FROM', 'FINIO_CONTACT_RESEND_KEY',
        'FINIO_CONTACT_TURNSTILE_SITE_KEY', 'FINIO_CONTACT_TURNSTILE_SECRET')}
    if not all(values.values()):
        return None
    if not all(_ADDRESS.fullmatch(values[name]) for name in ('FINIO_CONTACT_TO', 'FINIO_CONTACT_FROM')):
        return None
    # Do not let public dummy credentials turn the deployed form into a relay.
    if os.getenv('FINIO_ENV') == 'production' and (
        values['FINIO_CONTACT_TURNSTILE_SECRET'] in _TEST_KEYS
        or values['FINIO_CONTACT_TURNSTILE_SITE_KEY'].startswith(('1x000000', '2x000000', '3x000000'))
    ):
        return None
    if not re.fullmatch(r'[A-Za-z0-9_-]{10,100}', values['FINIO_CONTACT_TURNSTILE_SITE_KEY']):
        return None
    for name in ('WEB_CONCURRENCY', 'UVICORN_WORKERS'):
        if os.getenv(name, '1') != '1':
            return None
    return values


def _origin(request):
    value = request.headers.get('origin', '')
    allowed = [v.strip() for v in os.getenv('FINIO_ORIGINS', '').split(',') if v.strip()]
    if not allowed and os.getenv('FINIO_ENV') != 'production':
        allowed = ['http://localhost:5500', 'http://127.0.0.1:5500']
    if value not in allowed:
        raise HTTPException(403, 'Open the request form on the Finio website.')
    parsed = urlsplit(value)
    if not parsed.hostname or parsed.username or parsed.password or parsed.path not in ('', '/'):
        raise HTTPException(403, 'Open the request form on the Finio website.')
    if os.getenv('FINIO_ENV') == 'production' and (parsed.scheme != 'https' or '*' in value):
        raise HTTPException(403, 'Open the request form on the Finio website.')
    return parsed.hostname


def _admit(client):
    now = time.monotonic()
    with _LOCK:
        while _ATTEMPTS and now - _ATTEMPTS[0] >= 86400:
            _ATTEMPTS.popleft()
        for key in list(_CLIENTS):
            hits = _CLIENTS[key]
            while hits and now - hits[0] >= 3600:
                hits.popleft()
            if not hits:
                del _CLIENTS[key]
        hits = _CLIENTS.get(client, deque())
        # Count attempts, including failed delivery/challenges. Do not trust a
        # caller-supplied forwarding header to obtain more delivery attempts.
        if len(_ATTEMPTS) >= 20 or len(hits) >= 3:
            retry = (86400 - (now - _ATTEMPTS[0]) if len(_ATTEMPTS) >= 20
                     else 3600 - (now - hits[0]))
            raise HTTPException(429, 'Too many requests. Please try again later.',
                                headers={'Retry-After': str(max(1, int(retry) + 1))})
        _ATTEMPTS.append(now)
        hits.append(now)
        _CLIENTS[client] = hits


def _verify(token, secret, hostname):
    try:
        with httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client:
            response = client.post('https://challenges.cloudflare.com/turnstile/v0/siteverify',
                                   json={'secret': secret, 'response': token})
            response.raise_for_status()
            result = response.json()
        return (isinstance(result, dict) and result.get('success') is True
                and result.get('hostname') == hostname
                and result.get('action') == 'privacy-contact')
    except Exception:
        # Provider exceptions may contain submitted tokens. Never return them.
        return False


def _deliver(config, payload, reference):
    text = (
        f'Reference: {reference}\nTopic: {_TOPICS[payload.topic]}\n\n'
        'The supplied reply address is not verified. Verify identity separately\n'
        'before disclosing account information or changing/deleting any account.\n\n'
        + redact_credentials(payload.message) + '\n')
    # HTTPS works on free test hosts that block outbound SMTP. No SDK or new
    # dependency is needed. A fixed destination prevents use as an open relay.
    with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
        response = client.post('https://api.resend.com/emails',
            headers={'Authorization': 'Bearer ' + config['FINIO_CONTACT_RESEND_KEY'],
                     'Idempotency-Key': 'finio-privacy-' + reference},
            json={'from': config['FINIO_CONTACT_FROM'], 'to': [config['FINIO_CONTACT_TO']],
                  'reply_to': payload.email,
                  'subject': f'Finio privacy request: {_TOPICS[payload.topic]} [{reference}]',
                  'text': text})
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or not isinstance(result.get('id'), str) or not result['id']:
            raise RuntimeError('Message not accepted')


@router.get('/config')
def public_config():
    config = _configuration()
    return {'enabled': bool(config), 'site_key': config['FINIO_CONTACT_TURNSTILE_SITE_KEY'] if config else None}


@router.post('', status_code=202)
def submit(payload: PrivacyContact, request: Request):
    config = _configuration()
    if config is None:
        raise HTTPException(503, _UNAVAILABLE)
    hostname = _origin(request)
    if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
        raise HTTPException(415, 'Use the request form on the Finio website.')
    _admit(request.client.host if request.client else 'unknown')
    if payload.website or not _SEND_SLOT.acquire(blocking=False):
        raise HTTPException(429, 'Please try again shortly.')
    try:
        if not _verify(payload.token, config['FINIO_CONTACT_TURNSTILE_SECRET'], hostname):
            raise HTTPException(400, 'The security check failed or expired. Please try again.')
        reference = uuid4().hex[:12]
        try:
            _deliver(config, payload, reference)
        except Exception:
            raise HTTPException(503, _UNCONFIRMED) from None
        # Acknowledgement only after provider acceptance. No message or addresses
        # are returned, logged, saved in the database or shared with an LLM.
        return {'received': True, 'reference': reference}
    finally:
        _SEND_SLOT.release()
