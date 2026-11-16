-- Owner-scoped deletion and durable chat titles on existing Supabase layouts.
begin;
drop policy if exists finio_delete_own_chat on public.chat_history;
create policy finio_delete_own_chat on public.chat_history for delete to authenticated
  using ((select auth.uid()) = user_id);
drop policy if exists finio_delete_own_streak on public.streaks;
create policy finio_delete_own_streak on public.streaks for delete to authenticated
  using ((select auth.uid()) = user_id);
drop policy if exists finio_delete_own_profile on public.users;
create policy finio_delete_own_profile on public.users for delete to authenticated
  using ((select auth.uid()) = id);

alter table public.chat_history drop constraint if exists chat_history_role_check;
alter table public.chat_history add constraint chat_history_role_check
  check (role in ('user', 'assistant', 'title'));

create or replace function public.finio_delete_data()
returns void language plpgsql security invoker set search_path = '' as $$
declare uid uuid := auth.uid(); remaining boolean;
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
  -- finio_delete_verified_v7: RLS can silently leave rows behind. Abort the
  -- entire transaction if the caller can still see any of their saved data.
  select exists (
    select 1 from public.merchant_embeddings where user_id = uid union all
    select 1 from public.spend_checks where user_id = uid union all
    select 1 from public.chat_history where user_id = uid union all
    select 1 from public.budgets where user_id = uid union all
    select 1 from public.goals where user_id = uid union all
    select 1 from public.streaks where user_id = uid union all
    select 1 from public.snapshots where user_id = uid union all
    select 1 from public.transactions where user_id = uid union all
    select 1 from public.users where id = uid
  ) into remaining;
  if remaining then
    raise exception 'Deletion incomplete; no data was deleted' using errcode = 'P0001';
  end if;
end;
$$;
revoke all on function public.finio_delete_data() from public, anon;
grant execute on function public.finio_delete_data() to authenticated;

create or replace function public.finio_ready_v7() returns boolean
language sql security invoker set search_path = '' as $$
  select public.finio_ready_v6()
    and (select count(*) = 3 from pg_catalog.pg_policies
      where schemaname = 'public' and cmd = 'DELETE'
        and roles = array['authenticated']::name[] and qual is not null
        and (tablename, policyname) in (
          ('chat_history','finio_delete_own_chat'),
          ('streaks','finio_delete_own_streak'),
          ('users','finio_delete_own_profile')))
    and exists (select 1 from pg_catalog.pg_constraint
      where conrelid = 'public.chat_history'::regclass
        and conname = 'chat_history_role_check'
        and position('title' in pg_catalog.pg_get_constraintdef(oid)) > 0)
    and position('finio_delete_verified_v7' in pg_catalog.pg_get_functiondef(
      'public.finio_delete_data()'::regprocedure)) > 0;
$$;
create or replace function public.finio_ready() returns boolean
language sql security invoker set search_path = '' as $$ select public.finio_ready_v7(); $$;
revoke all on function public.finio_ready_v7() from public;
grant execute on function public.finio_ready_v7() to anon, authenticated;
notify pgrst, 'reload schema';
commit;
