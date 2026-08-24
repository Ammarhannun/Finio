-- Finio — spend-check history.
-- Run once in the Supabase SQL editor.
--
-- Kept in its own table rather than inside snapshots.summary_json (where
-- overrides and budget targets live) because a re-upload rebuilds that blob
-- from scratch — the history would be wiped every time a new statement was
-- analysed. It is also a log, not part of an analysis snapshot.

create table if not exists spend_checks (
    id          bigint generated always as identity primary key,
    user_id     uuid not null references auth.users (id) on delete cascade,
    merchant    text,
    amount      numeric(12, 2) not null,
    days_ahead  integer,
    verdict     text not null,                  -- green | yellow | red
    projected_balance numeric(12, 2),
    checked_at  timestamptz not null default now()
);

-- Newest-first per user is the only access pattern.
create index if not exists spend_checks_user_time_idx
    on spend_checks (user_id, checked_at desc);

alter table spend_checks enable row level security;

-- Same shape as the rest of the schema: a user sees and writes only their own.
drop policy if exists "own spend checks" on spend_checks;
create policy "own spend checks" on spend_checks
    for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
