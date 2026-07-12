"""Text embeddings for semantic search (RAG + merchant search).

Uses OpenAI `text-embedding-3-small` when a key is present. Returns None when
there's no key so every caller can fall back (TF-IDF retrieval, no merchant
search) and the app still runs fully offline.
"""

import os

from config import EMBED_MODEL


def has_embeddings():
    return bool(os.getenv("OPENAI_API_KEY"))


_client = None


def _get_client():
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI()
    return _client


def embed_texts(texts):
    """Embed a list of strings in ONE batched API call.

    Returns a list of float vectors aligned to `texts`, or None if there's no
    key or the call fails (callers then fall back gracefully).
    """
    if not texts or not has_embeddings():
        return None
    try:
        resp = _get_client().embeddings.create(model=EMBED_MODEL, input=list(texts))
        # API preserves input order.
        return [d.embedding for d in resp.data]
    except Exception:
        return None


def embed_one(text):
    """Embed a single string → one vector, or None on no-key/failure."""
    out = embed_texts([text])
    return out[0] if out else None
