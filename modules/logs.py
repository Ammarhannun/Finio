"""Logging for the parts of Finio that are allowed to fail.

Several features are deliberately best-effort: embeddings, pgvector retrieval,
chat titles. If they break the app must keep working — but it must not go
QUIET. Swallowing these exceptions with a bare `pass` meant a broken OpenAI key
or an unrun migration looked exactly like a working system, which is the worst
possible failure mode to debug.

`warn(...)` keeps the same non-fatal behaviour while leaving a one-line record
of what degraded and why.
"""

import logging
import os

_LEVEL = os.getenv("FINIO_LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, _LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)

log = logging.getLogger("finio")


def warn(what, exc, *, hint=None):
    """Record a non-fatal degradation. `what` names the feature that fell back."""
    detail = f"{type(exc).__name__}: {exc}"
    log.warning("%s unavailable — %s%s", what, detail, f" ({hint})" if hint else "")


def debug(what, exc):
    """An expected, uninteresting fallback (e.g. an optional column missing)."""
    log.debug("%s fell back — %s: %s", what, type(exc).__name__, exc)
