-- Fix saving on existing installations where budgets.month is a date.
-- The original schema used text; date values can be assigned to either type.
-- Only function definitions change. No tables, rows or access policies change.
begin;
do $$
declare signature text; definition text;
begin
  foreach signature in array array[
    'public.finio_commit_analysis(jsonb,date,jsonb,jsonb,jsonb,jsonb,jsonb,uuid)',
    'public.finio_edit_analysis(uuid,jsonb,jsonb,text,boolean)'
  ] loop
    definition := pg_get_functiondef(to_regprocedure(signature));
    if definition is null then
      raise exception 'Apply migration 005 before migration 006';
    end if;
    definition := replace(definition, 'p_month::text', 'p_month');
    definition := replace(definition, 's.month::text', 's.month');
    execute definition;
  end loop;
end;
$$;

create or replace function public.finio_ready_v6() returns boolean
language plpgsql security invoker set search_path = '' as $$
begin
  return public.finio_ready_v5()
    and position('p_month::text' in pg_get_functiondef(to_regprocedure(
      'public.finio_commit_analysis(jsonb,date,jsonb,jsonb,jsonb,jsonb,jsonb,uuid)'))) = 0
    and position('s.month::text' in pg_get_functiondef(to_regprocedure(
      'public.finio_edit_analysis(uuid,jsonb,jsonb,text,boolean)'))) = 0;
end;
$$;
create or replace function public.finio_ready() returns boolean
language sql security invoker set search_path = '' as $$ select public.finio_ready_v6(); $$;
revoke all on function public.finio_ready_v6() from public;
grant execute on function public.finio_ready_v6() to anon, authenticated;
notify pgrst, 'reload schema';
commit;
