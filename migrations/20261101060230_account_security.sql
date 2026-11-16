-- Account security without changing financial rows or existing RPC signatures.
-- Test and apply before releasing the matching MFA and sensitive-action UI.
begin;

create schema if not exists extensions;
do $$
begin
  if exists(select 1 from pg_extension e join pg_namespace n on n.oid=e.extnamespace
            where e.extname='vector' and n.nspname='public') then
    alter extension vector set schema extensions;
  end if;
end;
$$;
grant usage on schema extensions to anon, authenticated;

-- Both schemas reject CREATE from client roles. Pin lookup for vector operators
-- and existing text-embedding RPCs after relocating the extension.
do $$
declare item record;
begin
  for item in select p.oid::regprocedure as signature from pg_proc p
    join pg_namespace n on n.oid=p.pronamespace
    where n.nspname='public' and p.proname in ('match_kb','match_merchants')
  loop
    execute format('alter function %s set search_path = public, extensions',item.signature);
  end loop;
  for item in select p.oid::regprocedure as signature from pg_proc p
    join pg_namespace n on n.oid=p.pronamespace
    where n.nspname='public' and p.proname in ('handle_new_user','rls_auto_enable')
      and p.prorettype in ('trigger'::regtype,'event_trigger'::regtype)
  loop
    execute format('revoke all on function %s from public, anon, authenticated',item.signature);
  end loop;
end;
$$;

create schema if not exists finio_private;
revoke all on schema finio_private from public, anon;
grant usage on schema finio_private to authenticated;

-- No parameters: callers can inspect only the identity and session in their
-- server-verified JWT. The private helper never exposes Auth records or secrets.
create or replace function finio_private.security_state() returns jsonb
language plpgsql stable security definer set search_path = '' as $$
declare
  claims jsonb := auth.jwt();
  owner_id uuid := auth.uid();
  active boolean;
  needs_mfa boolean;
  fresh boolean;
begin
  active := exists(select 1 from auth.sessions s
    where s.id::text=claims->>'session_id' and s.user_id=owner_id
      and (s.not_after is null or s.not_after>now()));
  needs_mfa := exists(select 1 from auth.mfa_factors f
    where f.user_id=owner_id and f.status::text='verified');
  select exists(select 1 from jsonb_array_elements(
    case when jsonb_typeof(claims->'amr')='array' then claims->'amr' else '[]'::jsonb end
  ) entry where entry->>'method' in ('password','otp','oauth','totp','mfa/totp','webauthn','sso/saml')
    and case when entry->>'timestamp' ~ '^[0-9]+([.][0-9]+)?$'
      then (entry->>'timestamp')::numeric between extract(epoch from now())-300 and extract(epoch from now())+30
      else false end) into fresh;
  return jsonb_build_object('session_active',active,
    'mfa_satisfied',not needs_mfa or coalesce(claims->>'aal'='aal2',false),
    'recent_auth',fresh);
end;
$$;
revoke all on function finio_private.security_state() from public, anon;
grant execute on function finio_private.security_state() to authenticated;

create or replace function finio_private.access_allowed() returns boolean
language sql stable security invoker set search_path = '' as $$
  select (state->>'session_active')::boolean and (state->>'mfa_satisfied')::boolean
  from (select finio_private.security_state() as state) checks;
$$;
revoke all on function finio_private.access_allowed() from public, anon;
grant execute on function finio_private.access_allowed() to authenticated;

create or replace function public.finio_security_status() returns jsonb
language sql stable security invoker set search_path = '' as $$
  select finio_private.security_state();
$$;
revoke all on function public.finio_security_status() from public, anon;
grant execute on function public.finio_security_status() to authenticated;

-- Additional restrictive policies preserve the existing owner policies. Enforce
-- session revocation and opted-in MFA on the direct Data API as well as Finio.
do $$
declare table_name text;
begin
  foreach table_name in array array['users','transactions','snapshots','goals','budgets',
    'streaks','merchant_embeddings','chat_history','spend_checks']
  loop
    execute format('drop policy if exists finio_active_session on public.%I',table_name);
    execute format('create policy finio_active_session on public.%I as restrictive for all to authenticated using ((select finio_private.access_allowed())) with check ((select finio_private.access_allowed()))',table_name);
  end loop;
end;
$$;

create or replace function public.finio_security_ready() returns boolean
language sql stable security invoker set search_path = '' as $$
  select to_regprocedure('public.finio_security_status()') is not null
    and (select count(*) from pg_policies where schemaname='public'
      and policyname='finio_active_session' and permissive='RESTRICTIVE')=9;
$$;
grant execute on function public.finio_security_ready() to anon, authenticated;
notify pgrst,'reload schema';
commit;
