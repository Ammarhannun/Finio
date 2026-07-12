"""Tiny retrieval layer over a curated AU personal-finance knowledge base.

Uses TF-IDF + cosine similarity (scikit-learn, already a dependency) so the
coach can ground answers about concepts (ETFs, super, HECS, budgeting…) in real
sourced text instead of the model's memory. No embeddings service, no API key.
"""

from config import PROJECT_ROOT
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

KB_DIR = PROJECT_ROOT / "data" / "kb"
_INDEX = None  # cached (vectorizer, matrix, docs)


def _load_docs():
    docs = []
    if not KB_DIR.exists():
        return docs
    for path in sorted(KB_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            continue
        title = text.splitlines()[0].lstrip("#").strip()
        docs.append({"id": path.stem, "title": title, "text": text})
    return docs


def _get_index():
    global _INDEX
    if _INDEX is None:
        docs = _load_docs()
        if not docs:
            _INDEX = (None, None, [])
        else:
            vec = TfidfVectorizer(stop_words="english")
            matrix = vec.fit_transform([d["text"] for d in docs])
            _INDEX = (vec, matrix, docs)
    return _INDEX


def _tfidf_search(query, k, min_score):
    """Keyword retrieval over the local KB files — the no-key fallback."""
    vec, matrix, docs = _get_index()
    if not docs or not query:
        return []
    sims = cosine_similarity(vec.transform([query]), matrix)[0]
    ranked = sorted(zip(sims, docs), key=lambda x: x[0], reverse=True)
    return [
        {"id": d["id"], "title": d["title"], "text": d["text"], "score": round(float(s), 3)}
        for s, d in ranked[:k] if s >= min_score
    ]


def search(query, k=2, min_score=0.05):
    """Return up to k KB snippets most relevant to the query.

    Tries semantic search first (OpenAI embedding + pgvector `match_kb`), which
    matches on meaning. Falls back to TF-IDF keyword matching when there's no
    key, no migration, or any error — so retrieval always works.
    """
    if not query:
        return []
    from modules.embeddings import embed_one

    vector = embed_one(query)
    if vector is not None:
        try:
            from modules import db
            hits = db.match_kb(db.get_client(), vector, k)  # kb_chunks is global
            results = [
                {"id": h["id"], "title": h["title"], "text": h["content"],
                 "score": round(float(h.get("similarity", 0)), 3)}
                for h in hits if h.get("similarity", 0) >= min_score
            ]
            if results:
                return results
        except Exception:
            pass  # pgvector not set up / unreachable → fall back
    return _tfidf_search(query, k, min_score)
