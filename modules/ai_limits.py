"""Bound concurrent paid provider requests in the single web worker."""
from contextlib import contextmanager
from threading import BoundedSemaphore

_SLOTS = BoundedSemaphore(4)


class AICapacityError(RuntimeError):
    pass


@contextmanager
def ai_request_slot():
    from modules.ai_policy import provider_allowed, AISharingDisabled
    if not provider_allowed():
        raise AISharingDisabled('AI sharing is off for this account')
    if not _SLOTS.acquire(blocking=False):
        raise AICapacityError('AI capacity is busy')
    try:
        yield
    finally:
        _SLOTS.release()
