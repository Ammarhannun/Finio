-- Run after 004, before deploying this release. Rerunnable; all RPCs use caller RLS.
begin;
alter table public.snapshots add column if not exists revision uuid not null default gen_random_uuid();
alter table public.snapshots add column if not exists undo_json jsonb;
alter table public.users add column if not exists revision uuid not null default gen_random_uuid();
alter table public.chat_history add column if not exists verified_facts jsonb not null default '[]';

create or replace function public.finio_snapshot_timestamp() returns trigger
language plpgsql set search_path = '' as $$
begin
  if tg_op = 'INSERT' then new.updated_at := clock_timestamp();
  elsif (new.summary_json - 'insight') is distinct from (old.summary_json - 'insight')
     or new.undo_json is distinct from old.undo_json or new.revision is distinct from old.revision then
    new.updated_at := clock_timestamp(); new.revision := gen_random_uuid();
  end if;
  return new;
end;
$$;

-- Remove the unsafe old overload, including on a repeated full migration run.
drop function if exists public.finio_commit_analysis(jsonb,date,jsonb,jsonb,jsonb,jsonb,jsonb);
create or replace function public.finio_commit_analysis(
  p_rows jsonb, p_month date, p_summary jsonb, p_profile jsonb,
  p_goal jsonb, p_streak jsonb, p_budgets jsonb, p_expected_revision uuid default null
) returns void language plpgsql security invoker set search_path = '' as $$
declare uid uuid := auth.uid(); current_revision uuid;
begin
  if uid is null then raise exception 'Authentication required'; end if;
  perform pg_advisory_xact_lock(hashtextextended(uid::text, 0));
  select revision into current_revision from public.snapshots where user_id = uid
    order by updated_at desc limit 1 for update;
  if current_revision is null then
    select revision into current_revision from public.users where id = uid for update;
  end if;
  if current_revision is distinct from p_expected_revision then
    raise exception using errcode = '40001', message = 'Data changed. Reload before replacing history.';
  end if;
  perform public.finio_replace_transactions(p_rows);
  delete from public.snapshots where user_id = uid and month <> p_month;
  insert into public.users(id, email, age, income_bracket)
  values(uid, p_profile->>'email', (p_profile->>'age')::int, p_profile->>'income_bracket')
  on conflict (id) do update set email = excluded.email,
    age = coalesce(excluded.age, public.users.age),
    income_bracket = coalesce(excluded.income_bracket, public.users.income_bracket);
  insert into public.snapshots(user_id, month, summary_json) values(uid, p_month, p_summary)
  on conflict (user_id, month) do update set summary_json = excluded.summary_json, undo_json = null, revision = gen_random_uuid();
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


-- Snapshot, goals, budgets and profile commit together. Undo stores one previous state.
create or replace function public.finio_edit_analysis(
  p_expected_revision uuid, p_summary jsonb default null,
  p_profile jsonb default '{}', p_label text default 'Edit', p_undo boolean default false
) returns jsonb language plpgsql security invoker set search_path = '' as $$
declare
  uid uuid := auth.uid(); s public.snapshots%rowtype; u public.users%rowtype;
  backup jsonb; summary jsonb; patch jsonb; rev uuid;
begin
  if uid is null then raise exception 'Authentication required'; end if;
  perform pg_advisory_xact_lock(hashtextextended(uid::text, 0));
  select * into s from public.snapshots where user_id = uid order by updated_at desc limit 1 for update;
  select * into u from public.users where id = uid for update;
  rev := coalesce(s.revision, u.revision);
  if rev is distinct from p_expected_revision then
    raise exception using errcode = '40001', message = 'Data changed. Reload before saving.';
  end if;
  if s.id is null then
    if p_undo or p_summary is not null then raise exception 'Upload a statement first'; end if;
    insert into public.users(id, email, age, income_bracket)
      values(uid, p_profile->>'email', (p_profile->>'age')::int, p_profile->>'income_bracket')
      on conflict(id) do update set
        age = case when p_profile ? 'age' then (p_profile->>'age')::int else public.users.age end,
        income_bracket = case when p_profile ? 'income_bracket' then p_profile->>'income_bracket' else public.users.income_bracket end,
        revision = gen_random_uuid()
      returning revision into rev;
    return jsonb_build_object('revision', rev, 'can_undo', false);
  end if;
  if p_undo then
    if s.undo_json is null then raise exception 'Nothing to undo'; end if;
    summary := s.undo_json->'summary'; patch := s.undo_json->'profile'; backup := null;
  else
    summary := coalesce(p_summary, s.summary_json) - 'insight'; patch := p_profile;
    backup := jsonb_build_object('summary', s.summary_json - 'insight',
      'profile', jsonb_build_object('age', u.age, 'income_bracket', u.income_bracket),
      'label', left(p_label, 100));
  end if;
  update public.snapshots set summary_json = summary, undo_json = backup, revision = gen_random_uuid()
    where id = s.id returning revision into rev;
  update public.users set
    age = case when patch ? 'age' then (patch->>'age')::int else age end,
    income_bracket = case when patch ? 'income_bracket' then patch->>'income_bracket' else income_bracket end,
    revision = gen_random_uuid() where id = uid;
  insert into public.goals(user_id, name, target_amount, target_date, current_saved)
    values(uid, 'Savings goal', (summary#>>'{forecast,target_amount}')::numeric,
      (summary#>>'{forecast,target_date}')::date, (summary#>>'{forecast,current_saved}')::numeric)
    on conflict(user_id) do update set target_amount = excluded.target_amount,
      target_date = excluded.target_date, current_saved = excluded.current_saved;
  delete from public.budgets where user_id = uid;
  insert into public.budgets(user_id, category, monthly_limit, month)
    select uid, b.category, b.suggested_limit, s.month::text
    from jsonb_to_recordset(coalesce(summary#>'{budgets,budgets}', '[]'))
      as b(category text, suggested_limit numeric) where b.suggested_limit is not null;
  return jsonb_build_object('revision', rev, 'can_undo', backup is not null, 'undo_label', backup->>'label');
end;
$$;

-- An in-flight AI recap may only attach to the revision it read.
create or replace function public.finio_cache_insight(p_expected_revision uuid, p_insight jsonb)
returns boolean language plpgsql security invoker set search_path = '' as $$
declare uid uuid := auth.uid();
begin
  if uid is null then raise exception 'Authentication required'; end if;
  perform pg_advisory_xact_lock(hashtextextended(uid::text, 0));
  update public.snapshots set summary_json = jsonb_set(summary_json, '{insight}', p_insight)
    where user_id = uid and revision = p_expected_revision;
  return found;
end;
$$;

create or replace function public.finio_ready_v5() returns boolean
language plpgsql security invoker set search_path = '' as $$
begin
  perform flow, balance, position from public.transactions limit 0;
  perform revision, undo_json from public.snapshots limit 0;
  perform revision from public.users limit 0;
  perform chat_id, verified_facts from public.chat_history limit 0;
  perform id from public.spend_checks limit 0;
  perform user_id from public.merchant_embeddings limit 0;
  return to_regprocedure('public.finio_commit_analysis(jsonb,date,jsonb,jsonb,jsonb,jsonb,jsonb,uuid)') is not null
    and to_regprocedure('public.finio_edit_analysis(uuid,jsonb,jsonb,text,boolean)') is not null
    and to_regprocedure('public.finio_cache_insight(uuid,jsonb)') is not null;
end;
$$;
create or replace function public.finio_ready() returns boolean
language sql security invoker set search_path = '' as $$ select public.finio_ready_v5(); $$;
revoke all on function public.finio_ready_v5() from public;
grant execute on function public.finio_ready_v5() to anon, authenticated;
revoke all on function public.finio_commit_analysis(jsonb,date,jsonb,jsonb,jsonb,jsonb,jsonb,uuid) from public, anon;
revoke all on function public.finio_edit_analysis(uuid,jsonb,jsonb,text,boolean) from public, anon;
revoke all on function public.finio_cache_insight(uuid,jsonb) from public, anon;
grant execute on function public.finio_commit_analysis(jsonb,date,jsonb,jsonb,jsonb,jsonb,jsonb,uuid) to authenticated;
grant execute on function public.finio_edit_analysis(uuid,jsonb,jsonb,text,boolean) to authenticated;
grant execute on function public.finio_cache_insight(uuid,jsonb) to authenticated;
commit;
