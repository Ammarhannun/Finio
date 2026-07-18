"""Embed the knowledge-base files and upsert them into Supabase `kb_chunks`.

Run once after applying migrations/001_pgvector.sql, and again whenever you edit
data/kb/*.md:

    source venv/bin/activate
    python -m scripts.index_kb

Requires OPENAI_API_KEY (embeddings) and SUPABASE_URL/SUPABASE_ANON_KEY in .env.
"""

from dotenv import load_dotenv

load_dotenv()

from modules import db
from modules.embeddings import embed_texts, has_embeddings
from modules.rag import _load_docs


def main():
    if not has_embeddings():
        print("No OPENAI_API_KEY — cannot embed. Set it in .env and retry.")
        return
    docs = _load_docs()
    if not docs:
        print("No KB docs found in data/kb/.")
        return

    vectors = embed_texts([d["text"] for d in docs])
    if vectors is None:
        print("Embedding call failed (check the key / network).")
        return

    rows = [
        {"id": d["id"], "title": d["title"], "content": d["text"], "embedding": v}
        for d, v in zip(docs, vectors)
    ]
    # kb_chunks is a global table protected by RLS — writing it needs the
    # service-role key (admin), not the public anon key.
    client = db.get_admin_client()
    if client is None:
        print("SUPABASE_SERVICE_ROLE_KEY is not set in .env — needed to write the"
              " global kb_chunks table (RLS blocks the anon key).\n"
              "Find it in Supabase → Settings → API → service_role, add\n"
              "  SUPABASE_SERVICE_ROLE_KEY=...\n"
              "to .env, and rerun. Keep that key secret (server-side only).")
        return
    db.upsert_kb_chunks(client, rows)
    print(f"Indexed {len(rows)} KB chunks into kb_chunks: "
          + ", ".join(d["id"] for d in docs))


if __name__ == "__main__":
    main()
