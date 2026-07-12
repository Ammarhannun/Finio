-- Finio — pgvector migration for semantic RAG + merchant search.
-- Run this once in the Supabase SQL editor (Database → SQL Editor → New query).
-- Embeddings are OpenAI text-embedding-3-small → 1536 dimensions.

-- 1. Enable the vector type.
create extension if not exists vector;

-- 2. Global knowledge-base chunks (one row per data/kb/*.md file). Not user data.
create table if not exists kb_chunks (
    id        text primary key,
    title     text,
    content   text,
    embedding vector(1536)
);

-- 3. Per-user merchant embeddings (for semantic merchant search / clustering).
create table if not exists merchant_embeddings (
    user_id   uuid not null references auth.users (id) on delete cascade,
    merchant  text not null,
    category  text,
    embedding vector(1536),
    primary key (user_id, merchant)
);

-- 4. Row Level Security: a user can only see their own merchant rows.
alter table merchant_embeddings enable row level security;

drop policy if exists "own merchant embeddings" on merchant_embeddings;
create policy "own merchant embeddings" on merchant_embeddings
    for all
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

-- 5. Match functions. The embedding arrives as TEXT (a JSON-style array string)
--    and is cast to vector inside — this avoids PostgREST mistyping a float[].
create or replace function match_kb(query_embedding text, match_count int)
returns table (id text, title text, content text, similarity float)
language sql stable as $$
    select c.id, c.title, c.content,
           1 - (c.embedding <=> query_embedding::vector) as similarity
    from kb_chunks c
    order by c.embedding <=> query_embedding::vector
    limit match_count;
$$;

create or replace function match_merchants(uid uuid, query_embedding text, match_count int)
returns table (merchant text, category text, similarity float)
language sql stable as $$
    select m.merchant, m.category,
           1 - (m.embedding <=> query_embedding::vector) as similarity
    from merchant_embeddings m
    where m.user_id = uid
    order by m.embedding <=> query_embedding::vector
    limit match_count;
$$;
