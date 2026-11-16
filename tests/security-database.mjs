import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { databaseFixture, uid, other } from './database-fixture.mjs';

const pg = await databaseFixture();
const claims = (overrides={}) => ({sub:uid,session_id:uid,aal:'aal1',
  amr:[{method:'password',timestamp:Math.floor(Date.now()/1000)}],...overrides});
async function signedIn(subject=uid, jwt=claims()) {
  await pg.exec('reset role;set role authenticated;');
  await pg.query("select set_config('request.jwt.claim.sub',$1,false)",[subject]);
  await pg.query("select set_config('request.jwt.claims',$1,false)",[JSON.stringify(jwt)]);
}
const state = async () => (await pg.query('select public.finio_security_status() as state')).rows[0].state;
const visible = async () => (await pg.query('select count(*)::int as n from transactions')).rows[0].n;
await pg.query("insert into transactions(user_id,date,amount,merchant,flow) values($1,'2026-06-01',-10,'SYNTHETIC A','expense'),($2,'2026-06-01',-20,'SYNTHETIC B','expense')",[uid,other]);
await signedIn();
assert.deepEqual(await state(),{session_active:true,mfa_satisfied:true,recent_auth:true});
assert.equal(await visible(),1);
assert.equal((await pg.query('select public.finio_security_ready() as ready')).rows[0].ready,true);
await signedIn(uid,claims({session_id:other}));
assert.equal((await state()).session_active,false);
assert.equal(await visible(),0);
await signedIn(uid,{});
assert.equal((await state()).session_active,false);
assert.equal(await visible(),0);
await signedIn(uid,claims({amr:[{method:'password',timestamp:Math.floor(Date.now()/1000)-600}]}));
assert.equal((await state()).recent_auth,false);
assert.equal(await visible(),1); // Reading remains allowed; sensitive APIs enforce freshness.
await signedIn(uid,claims({amr:[{method:'password',timestamp:'invalid'}]}));
assert.equal((await state()).recent_auth,false);
await pg.exec('reset role;');
await pg.query("insert into auth.mfa_factors(id,user_id,status) values($1,$2,'unverified')",[other,uid]);
await signedIn();
assert.equal((await state()).mfa_satisfied,true);
await pg.exec("reset role;update auth.mfa_factors set status='verified';");
await signedIn();
assert.equal((await state()).mfa_satisfied,false);
assert.equal(await visible(),0);
await assert.rejects(pg.query("insert into transactions(user_id,date,amount,merchant,flow) values($1,'2026-06-02',-1,'blocked','expense')",[uid]),/row-level security/);
await signedIn(uid,claims({aal:'aal2'}));
assert.equal((await state()).mfa_satisfied,true);
assert.equal(await visible(),1);
await pg.exec("reset role;update auth.sessions set not_after=now()-interval '1 second';");
await signedIn(uid,claims({aal:'aal2'}));
assert.equal((await state()).session_active,false);
assert.equal(await visible(),0);
await pg.exec('reset role;delete from auth.sessions;');
await signedIn(uid,claims({aal:'aal2'}));
assert.equal((await state()).session_active,false);
assert.equal(await visible(),0);
await pg.exec('reset role;set role anon;');
await assert.rejects(pg.query('select public.finio_security_status()'),/permission denied/);
await assert.rejects(pg.query('select finio_private.security_state()'),/permission denied/);
await pg.exec('reset role;');
assert.equal((await pg.query("select n.nspname from pg_extension e join pg_namespace n on n.oid=e.extnamespace where e.extname='vector'")).rows[0].nspname,'extensions');
const vector='['+Array(1536).fill(0).map((_,i)=>i===0?1:0).join(',')+']';
await pg.query("insert into kb_chunks(id,title,content,embedding) values('synthetic','Synthetic','Public test reference',$1::extensions.vector)",[vector]);
await pg.exec('set role anon;');
assert.equal((await pg.query('select * from match_kb($1,1)',[vector])).rows[0].id,'synthetic');
await pg.exec(`reset role;
  create function public.handle_new_user() returns trigger language plpgsql security definer set search_path=public as $$
    begin insert into public.users(id,email) values(new.id,new.email); return new; end;
  $$;
  create trigger synthetic_signup after insert on auth.users for each row execute function public.handle_new_user();
  create function public.rls_auto_enable() returns event_trigger language plpgsql security definer set search_path=pg_catalog as $$ begin null; end; $$;
`);
await pg.exec(await readFile(new URL('../migrations/20261101060230_account_security.sql',import.meta.url),'utf8'));
for (const role of ['anon','authenticated']) {
  for (const name of ['handle_new_user','rls_auto_enable']) {
    assert.equal((await pg.query('select has_function_privilege($1,$2,\'EXECUTE\') as allowed',[role,'public.'+name+'()'])).rows[0].allowed,false);
  }
}
await pg.query("insert into auth.users(id,email) values('33333333-3333-3333-3333-333333333333','synthetic-signup@example.invalid')");
assert.equal((await pg.query("select email from public.users where id='33333333-3333-3333-3333-333333333333'")).rows[0].email,'synthetic-signup@example.invalid');
console.log('PASS: real SQL session ownership, revoked/expired/missing sessions, opted-in MFA, write denial, recent authentication, private function grants and relocated vector search');
console.log('PASS: trigger-helper public privileges revoked while new-user trigger still runs');
await pg.close();
