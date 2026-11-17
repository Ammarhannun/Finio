-- User-entered monthly planning figures. No existing limits or transactions change.
begin;
alter table public.users add column if not exists monthly_income numeric(12,2);
alter table public.users add column if not exists monthly_savings_target numeric(12,2);
alter table public.users drop constraint if exists users_monthly_income_check;
alter table public.users add constraint users_monthly_income_check
  check (monthly_income is null or monthly_income > 0);
alter table public.users drop constraint if exists users_monthly_savings_target_check;
alter table public.users add constraint users_monthly_savings_target_check
  check (monthly_savings_target is null or monthly_savings_target >= 0);

-- Preserve the existing revision/undo contract for profile edits. This is
-- security invoker and uses the caller's existing owner-scoped RLS policies.
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
    insert into public.users(id, email, age, income_bracket, monthly_income, monthly_savings_target)
      values(uid, p_profile->>'email', (p_profile->>'age')::int, p_profile->>'income_bracket',
        (p_profile->>'monthly_income')::numeric, (p_profile->>'monthly_savings_target')::numeric)
      on conflict(id) do update set
        age = case when p_profile ? 'age' then (p_profile->>'age')::int else public.users.age end,
        income_bracket = case when p_profile ? 'income_bracket' then p_profile->>'income_bracket' else public.users.income_bracket end,
        monthly_income = case when p_profile ? 'monthly_income' then (p_profile->>'monthly_income')::numeric else public.users.monthly_income end,
        monthly_savings_target = case when p_profile ? 'monthly_savings_target' then (p_profile->>'monthly_savings_target')::numeric else public.users.monthly_savings_target end,
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
      'profile', jsonb_build_object('age', u.age, 'income_bracket', u.income_bracket,
        'monthly_income', u.monthly_income, 'monthly_savings_target', u.monthly_savings_target),
      'label', left(p_label, 100));
  end if;
  update public.snapshots set summary_json = summary, undo_json = backup, revision = gen_random_uuid()
    where id = s.id returning revision into rev;
  update public.users set
    age = case when patch ? 'age' then (patch->>'age')::int else age end,
    income_bracket = case when patch ? 'income_bracket' then patch->>'income_bracket' else income_bracket end,
    monthly_income = case when patch ? 'monthly_income' then (patch->>'monthly_income')::numeric else monthly_income end,
    monthly_savings_target = case when patch ? 'monthly_savings_target' then (patch->>'monthly_savings_target')::numeric else monthly_savings_target end,
    revision = gen_random_uuid() where id = uid;
  insert into public.goals(user_id, name, target_amount, target_date, current_saved)
    values(uid, 'Savings goal', (summary#>>'{forecast,target_amount}')::numeric,
      (summary#>>'{forecast,target_date}')::date, (summary#>>'{forecast,current_saved}')::numeric)
    on conflict(user_id) do update set target_amount = excluded.target_amount,
      target_date = excluded.target_date, current_saved = excluded.current_saved;
  delete from public.budgets where user_id = uid;
  insert into public.budgets(user_id, category, monthly_limit, month)
    select uid, b.category, b.suggested_limit, s.month
    from jsonb_to_recordset(coalesce(summary#>'{budgets,budgets}', '[]'))
      as b(category text, suggested_limit numeric) where b.suggested_limit is not null;
  return jsonb_build_object('revision', rev, 'can_undo', backup is not null, 'undo_label', backup->>'label');
end;
$$;

create or replace function public.finio_ready_v8() returns boolean
language sql security invoker set search_path = '' as $$
  select public.finio_ready_v7()
    and exists (select 1 from pg_catalog.pg_attribute where attrelid = 'public.users'::regclass
      and attname = 'monthly_income' and not attisdropped)
    and exists (select 1 from pg_catalog.pg_attribute where attrelid = 'public.users'::regclass
      and attname = 'monthly_savings_target' and not attisdropped)
    and position('monthly_savings_target' in pg_get_functiondef(
      'public.finio_edit_analysis(uuid,jsonb,jsonb,text,boolean)'::regprocedure)) > 0;
$$;
create or replace function public.finio_ready() returns boolean
language sql security invoker set search_path = '' as $$ select public.finio_ready_v8(); $$;
revoke all on function public.finio_ready_v8() from public;
grant execute on function public.finio_ready_v8() to anon, authenticated;
revoke all on function public.finio_edit_analysis(uuid,jsonb,jsonb,text,boolean) from public, anon;
grant execute on function public.finio_edit_analysis(uuid,jsonb,jsonb,text,boolean) to authenticated;
notify pgrst, 'reload schema';
commit;
