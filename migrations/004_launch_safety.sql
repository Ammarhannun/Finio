-- Required before deploying this release. Run after migrations 001–003.
-- All operations are rerunnable. RPCs use the caller's JWT/RLS, never a supplied uid.
begin;

alter table public.transactions add column if not exists flow text;
alter table public.transactions add column if not exists balance numeric;
alter table public.transactions add column if not exists position integer not null default 0;
update public.transactions set flow = case
  when category = 'Transfers' then 'transfer'
  when is_expense or amount < 0 then 'expense'
  else 'income' end where flow is null;
alter table public.transactions alter column flow set not null;
alter table public.transactions drop constraint if exists transactions_flow_check;
alter table public.transactions add constraint transactions_flow_check
  check (flow in ('income', 'expense', 'transfer'));
create index if not exists transactions_user_date_position_idx
  on public.transactions(user_id, date, position, id);

alter table public.snapshots add column if not exists updated_at timestamptz;
update public.snapshots set updated_at = month::timestamptz where updated_at is null;
alter table public.snapshots alter column updated_at set default now();
alter table public.snapshots alter column updated_at set not null;
create or replace function public.finio_snapshot_timestamp() returns trigger
language plpgsql set search_path = '' as $$
begin new.updated_at := clock_timestamp(); return new; end;
$$;
drop trigger if exists finio_snapshot_updated on public.snapshots;
create trigger finio_snapshot_updated before insert or update on public.snapshots
for each row execute function public.finio_snapshot_timestamp();

create or replace function public.finio_replace_transactions(p_rows jsonb)
returns void language plpgsql security invoker set search_path = '' as $$
declare uid uuid := auth.uid();
begin
  if uid is null then raise exception 'Authentication required'; end if;
  if jsonb_typeof(p_rows) is distinct from 'array' or jsonb_array_length(p_rows) not between 1 and 20000 then
    raise exception 'Expected 1 to 20000 transactions';
  end if;
  perform pg_advisory_xact_lock(hashtextextended(uid::text, 0));
  delete from public.transactions where user_id = uid;
  insert into public.transactions(user_id, date, amount, merchant, category, is_expense, flow, balance, position)
  select uid, r.date, r.amount, r.merchant, r.category, r.flow = 'expense', r.flow, r.balance, r.position
  from jsonb_to_recordset(p_rows) as r(date date, amount numeric, merchant text,
    category text, flow text, balance numeric, position integer);
end;
$$;

create or replace function public.finio_commit_analysis(
  p_rows jsonb, p_month date, p_summary jsonb, p_profile jsonb,
  p_goal jsonb, p_streak jsonb, p_budgets jsonb
) returns void language plpgsql security invoker set search_path = '' as $$
declare uid uuid := auth.uid();
begin
  if uid is null then raise exception 'Authentication required'; end if;
  perform pg_advisory_xact_lock(hashtextextended(uid::text, 0));
  perform public.finio_replace_transactions(p_rows);
  insert into public.users(id, email, age, income_bracket)
  values(uid, p_profile->>'email', (p_profile->>'age')::int, p_profile->>'income_bracket')
  on conflict (id) do update set email = excluded.email,
    age = coalesce(excluded.age, public.users.age),
    income_bracket = coalesce(excluded.income_bracket, public.users.income_bracket);
  insert into public.snapshots(user_id, month, summary_json) values(uid, p_month, p_summary)
  on conflict (user_id, month) do update set summary_json = excluded.summary_json;
  insert into public.goals(user_id, name, target_amount, target_date, current_saved)
  values(uid, 'Savings goal', (p_goal->>'amount')::numeric,
    (p_goal->>'target_date')::date, (p_goal->>'current_saved')::numeric)
  on conflict (user_id) do update set target_amount = excluded.target_amount,
    target_date = excluded.target_date, current_saved = excluded.current_saved;
  insert into public.streaks(user_id, current_streak, best_streak, last_upload)
  values(uid, (p_streak->>'current_streak')::int, (p_streak->>'best_streak')::int,
    (p_streak->>'last_upload')::date)
  on conflict (user_id) do update set current_streak = excluded.current_streak,
    best_streak = excluded.best_streak, last_upload = excluded.last_upload;
  delete from public.budgets where user_id = uid;
  insert into public.budgets(user_id, category, monthly_limit, month)
  select uid, b.category, b.suggested_limit, p_month::text
  from jsonb_to_recordset(p_budgets) as b(category text, suggested_limit numeric)
  where b.suggested_limit is not null;
end;
$$;

create or replace function public.finio_delete_data()
returns void language plpgsql security invoker set search_path = '' as $$
declare uid uuid := auth.uid();
begin
  if uid is null then raise exception 'Authentication required'; end if;
  perform pg_advisory_xact_lock(hashtextextended(uid::text, 0));
  delete from public.merchant_embeddings where user_id = uid;
  delete from public.spend_checks where user_id = uid;
  delete from public.chat_history where user_id = uid;
  delete from public.budgets where user_id = uid;
  delete from public.goals where user_id = uid;
  delete from public.streaks where user_id = uid;
  delete from public.snapshots where user_id = uid;
  delete from public.transactions where user_id = uid;
  delete from public.users where id = uid;
end;
$$;

-- Reads required columns/tables and resolves required functions with no user rows.
create or replace function public.finio_ready() returns boolean
language plpgsql security invoker set search_path = '' as $$
begin
  perform flow, balance, position from public.transactions limit 0;
  perform updated_at from public.snapshots limit 0;
  perform chat_id from public.chat_history limit 0;
  perform id from public.spend_checks limit 0;
  perform user_id from public.merchant_embeddings limit 0;
  return to_regprocedure('public.finio_commit_analysis(jsonb,date,jsonb,jsonb,jsonb,jsonb,jsonb)') is not null;
end;
$$;
revoke all on function public.finio_replace_transactions(jsonb) from public, anon;
revoke all on function public.finio_commit_analysis(jsonb,date,jsonb,jsonb,jsonb,jsonb,jsonb) from public, anon;
revoke all on function public.finio_delete_data() from public, anon;
grant execute on function public.finio_replace_transactions(jsonb) to authenticated;
grant execute on function public.finio_commit_analysis(jsonb,date,jsonb,jsonb,jsonb,jsonb,jsonb) to authenticated;
grant execute on function public.finio_delete_data() to authenticated;
revoke all on function public.finio_ready() from public;
grant execute on function public.finio_ready() to anon, authenticated;
commit;
