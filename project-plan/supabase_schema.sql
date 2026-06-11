-- Finio Supabase schema (Phase 2 / 7)
-- Run in Supabase SQL Editor if tables are missing or upserts fail.

-- users profile (id matches auth.users)
create table if not exists users (
  id uuid primary key references auth.users (id) on delete cascade,
  email text,
  age int,
  income_bracket text
);

create table if not exists transactions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users (id) on delete cascade,
  date date not null,
  amount numeric not null,
  merchant text not null,
  category text,
  is_expense boolean not null default true
);

create table if not exists budgets (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users (id) on delete cascade,
  category text not null,
  monthly_limit numeric not null,
  month text not null,
  unique (user_id, category, month)
);

-- NOTE: month is stored as a date (YYYY-MM-01). The app converts "YYYY-MM" → first of month.
create table if not exists snapshots (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users (id) on delete cascade,
  month date not null,
  summary_json jsonb not null,
  unique (user_id, month)
);

create table if not exists goals (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users (id) on delete cascade,
  name text not null,
  target_amount numeric not null,
  target_date date not null,
  current_saved numeric not null default 0,
  unique (user_id)
);

create table if not exists streaks (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users (id) on delete cascade,
  current_streak int not null default 0,
  best_streak int not null default 0,
  last_upload date,
  unique (user_id)
);

create table if not exists chat_history (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users (id) on delete cascade,
  role text not null,
  message text not null,
  timestamp timestamptz not null default now()
);

-- RLS: enable and policies (user_id = auth.uid())
alter table users enable row level security;
alter table transactions enable row level security;
alter table budgets enable row level security;
alter table snapshots enable row level security;
alter table goals enable row level security;
alter table streaks enable row level security;
alter table chat_history enable row level security;

create policy "users_own_row" on users for all using (id = auth.uid());
create policy "transactions_own_rows" on transactions for all using (user_id = auth.uid());
create policy "budgets_own_rows" on budgets for all using (user_id = auth.uid());
create policy "snapshots_own_rows" on snapshots for all using (user_id = auth.uid());
create policy "goals_own_rows" on goals for all using (user_id = auth.uid());
create policy "streaks_own_rows" on streaks for all using (user_id = auth.uid());
create policy "chat_own_rows" on chat_history for all using (user_id = auth.uid());
