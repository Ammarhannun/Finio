// Real isolated PostgreSQL; Auth tables/claims are synthetic, never live users.
import { readFile, readdir } from 'node:fs/promises';
import { PGlite } from '@electric-sql/pglite';
import { vector } from '@electric-sql/pglite-pgvector';

export const uid = '11111111-1111-1111-1111-111111111111';
export const other = '22222222-2222-2222-2222-222222222222';
export async function databaseFixture() {
  const pg = new PGlite({ extensions: { vector } });
  await pg.exec(`
    create role anon; create role authenticated;
    create schema auth; create schema extensions;
    set search_path=public,extensions;
    create table auth.users(id uuid primary key, email text);
    insert into auth.users(id) values ('${uid}'),('${other}');
    create table auth.sessions(id uuid primary key, user_id uuid references auth.users(id), not_after timestamptz);
    insert into auth.sessions(id,user_id) select id,id from auth.users;
    create table auth.mfa_factors(id uuid primary key, user_id uuid references auth.users(id), status text, factor_type text default 'totp');
    create function auth.uid() returns uuid language sql stable as $$
      select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid;
    $$;
    create function auth.jwt() returns jsonb language sql stable as $$
      select coalesce(nullif(current_setting('request.jwt.claims',true),'')::jsonb,
        jsonb_build_object('sub',auth.uid(),'session_id',auth.uid(),'aal','aal1',
          'amr',jsonb_build_array(jsonb_build_object('method','password','timestamp',extract(epoch from now())))));
    $$;
    grant usage on schema auth,public,extensions to anon,authenticated;
    grant execute on function auth.uid(),auth.jwt() to anon,authenticated;
    alter default privileges in schema public grant all on tables to anon,authenticated;
    alter default privileges in schema public grant all on sequences to anon,authenticated;
  `);
  const files = ['project-plan/supabase_schema.sql', ...(await readdir(new URL('../migrations/', import.meta.url)))
    .filter(file => file.endsWith('.sql')).sort().map(file => 'migrations/'+file)];
  for (let pass=0; pass<2; pass++) {
    for (const file of files) await pg.exec(await readFile(new URL('../'+file, import.meta.url),'utf8'));
  }
  return pg;
}
