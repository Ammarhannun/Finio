// Executes real PostgreSQL SQL in an isolated, in-memory PGlite database.
// auth.uid() and Supabase default grants are simulated; no live credentials.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { PGlite } from '@electric-sql/pglite';
import { vector } from '@electric-sql/pglite-pgvector';
const pg = new PGlite({ extensions: { vector } });
const uid = '11111111-1111-1111-1111-111111111111';
const other = '22222222-2222-2222-2222-222222222222';
await pg.exec(`
  create role anon; create role authenticated;
  create schema auth;
  create table auth.users(id uuid primary key);
  insert into auth.users values ('${uid}'), ('${other}');
  create function auth.uid() returns uuid language sql stable as $$
    select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid;
  $$;
  grant usage on schema auth, public to anon, authenticated;
  grant execute on function auth.uid() to anon, authenticated;
  alter default privileges in schema public grant all on tables to anon, authenticated;
  alter default privileges in schema public grant all on sequences to anon, authenticated;
`);
const files = ['project-plan/supabase_schema.sql', 'migrations/001_pgvector.sql',
  'migrations/002_chats.sql', 'migrations/003_spend_checks.sql', 'migrations/004_launch_safety.sql'];
for (let pass = 0; pass < 2; pass++) {
  for (const file of files) await pg.exec(await readFile(new URL('../' + file, import.meta.url), 'utf8'));
}
console.log('PASS: base schema and all four migrations run twice');
await pg.exec(`set role anon;`);
assert.equal((await pg.query('select public.finio_ready() as ready')).rows[0].ready, true);
await assert.rejects(pg.query('select public.finio_delete_data()'), /permission denied/);
await pg.exec(`reset role; set role authenticated;`);
await pg.query("select set_config('request.jwt.claim.sub', $1, false)", [uid]);
const rows = [{ user_id: other, date: '2026-06-01', amount: 2000, merchant: 'SALARY', category: 'Income', flow: 'income', balance: 2000, position: 0 },
  { date: '2026-06-02', amount: -100, merchant: 'COLES', category: 'Groceries', flow: 'expense', balance: 1900, position: 1 },
  { date: '2026-06-03', amount: 30, merchant: 'COLES', category: 'Groceries', flow: 'expense', balance: 1930, position: 2 }];
const commit = budgets => pg.query('select public.finio_commit_analysis($1,$2,$3,$4,$5,$6,$7)', [
  JSON.stringify(rows), '2026-06-01', JSON.stringify({metrics: {total_spent: 70}, budget_targets: {Groceries: 400}}),
  JSON.stringify({email: 'synthetic@example.invalid', age: 34, income_bracket: 'middle'}),
  JSON.stringify({amount: 6000, target_date: '2027-12-01', current_saved: 1930}),
  JSON.stringify({current_streak: 1, best_streak: 1, last_upload: '2026-09-28'}), JSON.stringify(budgets)]);
await commit([{category: 'Groceries', suggested_limit: 400}]);
assert.equal((await pg.query('select count(*)::int as n from transactions')).rows[0].n, 3);
assert.equal((await pg.query('select distinct user_id from transactions')).rows[0].user_id, uid);
assert.equal((await pg.query('select flow from transactions where amount = 30')).rows[0].flow, 'expense');
// A late failure (after deletes/inserts) must roll back every part of the upload.
await assert.rejects(commit([{category: null, suggested_limit: 999}]), /not-null constraint/);
assert.equal((await pg.query('select monthly_limit from budgets')).rows[0].monthly_limit, '400');
assert.equal((await pg.query('select count(*)::int as n from transactions')).rows[0].n, 3);
await assert.rejects(pg.query('select public.finio_replace_transactions($1)', ['[]']), /Expected/);
await assert.rejects(pg.query('insert into transactions(user_id,date,amount,merchant,flow) values($1,$2,1,$3,$4)',
  [other,'2026-06-01','attack','income']), /row-level security/);
console.log('PASS: atomic upload, late rollback, refund flow, empty replacement protection');
await pg.query("select set_config('request.jwt.claim.sub', $1, false)", [other]);
for (const table of ['users','transactions','snapshots','goals','budgets','streaks','merchant_embeddings','chat_history','spend_checks']) {
  assert.equal((await pg.query(`select count(*)::int as n from ${table}`)).rows[0].n, 0, table);
}
await pg.query('select public.finio_delete_data()');
await pg.query("select set_config('request.jwt.claim.sub', $1, false)", [uid]);
assert.equal((await pg.query('select count(*)::int as n from transactions')).rows[0].n, 3);
await pg.query('select public.finio_delete_data()');
assert.equal((await pg.query('select count(*)::int as n from transactions')).rows[0].n, 0);
await pg.exec('reset role');
assert.equal((await pg.query('select count(*)::int as n from auth.users')).rows[0].n, 2);
console.log('PASS: two-user isolation, deletion scope, retained auth accounts, anonymous RPC protection');
await pg.close();
