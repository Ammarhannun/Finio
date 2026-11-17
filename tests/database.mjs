// Executes real PostgreSQL SQL in an isolated, in-memory PGlite database.
// auth.uid() and Supabase default grants are simulated; no live credentials.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { databaseFixture, uid, other } from './database-fixture.mjs';
import { execFileSync } from 'node:child_process';
const pg = await databaseFixture();
console.log('PASS: base schema and all migrations run twice');
await pg.exec(`set role anon;`);
assert.equal((await pg.query('select public.finio_ready_v5() as ready')).rows[0].ready, true);
assert.equal((await pg.query('select public.finio_ready_v6() as ready')).rows[0].ready, true);
assert.equal((await pg.query('select public.finio_ready_v8() as ready')).rows[0].ready, true);
assert.equal((await pg.query('select public.finio_ready() as ready')).rows[0].ready, true);
await assert.rejects(pg.query('select public.finio_delete_data()'), /permission denied/);
await assert.rejects(pg.query('select public.finio_edit_analysis(null)'), /permission denied/);
await assert.rejects(pg.query("select public.finio_cache_insight(null, '{}')"), /permission denied/);
await pg.exec(`reset role; set role authenticated;`);
await pg.query("select set_config('request.jwt.claim.sub', $1, false)", [uid]);
const rows = [{ user_id: other, date: '2026-06-01', amount: 2000, merchant: 'SALARY', category: 'Income', flow: 'income', balance: 2000, position: 0 },
  { date: '2026-06-02', amount: -100, merchant: 'COLES', category: 'Groceries', flow: 'expense', balance: 1900, position: 1 },
  { date: '2026-06-03', amount: 30, merchant: 'COLES', category: 'Groceries', flow: 'expense', balance: 1930, position: 2 }];
const revision = async () => (await pg.query('select revision from snapshots order by updated_at desc limit 1')).rows[0]?.revision ?? null;
const commit = async budgets => pg.query('select public.finio_commit_analysis($1,$2,$3,$4,$5,$6,$7,$8)', [
  JSON.stringify(rows), '2026-06-01', JSON.stringify({metrics: {total_spent: 70}, budget_targets: {Groceries: 400}, forecast: {target_amount:6000, target_date:'2027-12-01', current_saved:1930}, budgets:{budgets:[{category:'Groceries', suggested_limit:400}]}}),
  JSON.stringify({email: 'synthetic@example.invalid', age: 34, income_bracket: 'middle'}),
  JSON.stringify({amount: 6000, target_date: '2027-12-01', current_saved: 1930}),
  JSON.stringify({current_streak: 1, best_streak: 1, last_upload: '2026-09-28'}), JSON.stringify(budgets), await revision()]);
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
const edit = (rev, summary, profile = {}, undo = false) => pg.query(
  'select finio_edit_analysis($1,$2,$3,$4,$5) as state', [rev, summary === null ? null : JSON.stringify(summary), JSON.stringify(profile), 'Test edit', undo]);
const initialRevision = await revision();
const initialSummary = (await pg.query('select summary_json from snapshots')).rows[0].summary_json;
const updatedSummary = structuredClone(initialSummary);
updatedSummary.budget_targets.Groceries = 500;
updatedSummary.budgets.budgets[0].suggested_limit = 500;
updatedSummary.forecast.target_amount = 7000;
const saved = (await edit(initialRevision, updatedSummary, {age:35, income_bracket:null,
  monthly_income:4000, monthly_savings_target:500})).rows[0].state;
assert.notEqual(saved.revision, initialRevision);
assert.equal(saved.can_undo, true);
assert.equal((await pg.query('select target_amount from goals')).rows[0].target_amount, '7000');
assert.equal((await pg.query('select monthly_limit from budgets')).rows[0].monthly_limit, '500');
assert.equal((await pg.query('select age, income_bracket from users')).rows[0].age, 35);
assert.equal((await pg.query('select income_bracket from users')).rows[0].income_bracket, null);
assert.equal((await pg.query('select monthly_income from users')).rows[0].monthly_income, '4000.00');
assert.equal((await pg.query('select monthly_savings_target from users')).rows[0].monthly_savings_target, '500.00');
await assert.rejects(edit(initialRevision, initialSummary), /Data changed/);
await assert.rejects(edit(null, initialSummary), /Data changed/);
const broken = structuredClone(updatedSummary);
broken.budgets.budgets[0].category = null;
await assert.rejects(edit(saved.revision, broken, {age:99}), /not-null constraint/);
assert.equal(await revision(), saved.revision);
assert.equal((await pg.query('select age from users')).rows[0].age, 35);
assert.equal((await pg.query('select monthly_limit from budgets')).rows[0].monthly_limit, '500');
// Slow insight requests cannot overwrite a newer edit; a cache fill cannot consume undo.
assert.equal((await pg.query('select finio_cache_insight($1,$2) as ok', [initialRevision, '{}'])).rows[0].ok, false);
assert.equal((await pg.query('select finio_cache_insight($1,$2) as ok', [saved.revision, '{"text":"recap"}'])).rows[0].ok, true);
assert.equal(await revision(), saved.revision);
const undone = (await edit(saved.revision, null, {}, true)).rows[0].state;
assert.equal(undone.can_undo, false);
assert.notEqual(undone.revision, saved.revision);
assert.deepEqual((await pg.query('select summary_json from snapshots')).rows[0].summary_json, initialSummary);
assert.equal((await pg.query('select age from users')).rows[0].age, 34);
assert.equal((await pg.query('select income_bracket from users')).rows[0].income_bracket, 'middle');
assert.equal((await pg.query('select monthly_income from users')).rows[0].monthly_income, null);
assert.equal((await pg.query('select monthly_savings_target from users')).rows[0].monthly_savings_target, null);
assert.equal((await pg.query('select target_amount from goals')).rows[0].target_amount, '6000');
assert.equal((await pg.query('select monthly_limit from budgets')).rows[0].monthly_limit, '400');
await assert.rejects(edit(undone.revision, null, {}, true), /Nothing to undo/);
// One coach "Apply all budgets" request writes every limit under one revision
// and exposes one Undo for the whole group, not a series of stale proposals.
const beforeBatch = await revision();
const batchSummary = structuredClone(initialSummary);
batchSummary.budget_targets['Food & Dining'] = 800;
batchSummary.budget_targets.Transport = 250;
batchSummary.budgets.budgets.push(
  {category:'Food & Dining', suggested_limit:800},
  {category:'Transport', suggested_limit:250});
const batchSaved = (await edit(beforeBatch, batchSummary)).rows[0].state;
assert.equal(batchSaved.can_undo, true);
assert.deepEqual((await pg.query('select category, monthly_limit from budgets order by category')).rows,
  [{category:'Food & Dining',monthly_limit:'800'},
   {category:'Groceries',monthly_limit:'400'},
   {category:'Transport',monthly_limit:'250'}]);
await assert.rejects(edit(beforeBatch, batchSummary), /Data changed/);
await edit(batchSaved.revision, null, {}, true);
assert.deepEqual((await pg.query('select category, monthly_limit from budgets order by category')).rows,
  [{category:'Groceries',monthly_limit:'400'}]);
await assert.rejects(pg.query('select finio_commit_analysis($1,$2,$3,$4,$5,$6,$7,$8)',
  [JSON.stringify(rows), '2026-06-01', '{}', '{}', '{}', '{}', '[]', initialRevision]), /Data changed/);
await edit(await revision(), updatedSummary);
await commit([{category:'Groceries', suggested_limit:400}]);
assert.equal((await pg.query('select undo_json from snapshots')).rows[0].undo_json, null);
await pg.query("insert into chat_history(user_id,role,message,chat_id,verified_facts) values($1,'assistant','Synthetic answer','acceptance',$2)", [uid, JSON.stringify([{kind:'category_total',total:100,gross_purchases:110,refunds:10}])]);
assert.equal((await pg.query('select verified_facts from chat_history')).rows[0].verified_facts[0].total, 100);
console.log('PASS: atomic edits, late rollback, stale saves/uploads, null profile clears, persistent single undo, saved coach facts and insight CAS');
// Exercise the actual Python summary/RPC payload, including refunds and transfer corrections.
const pythonEdits = JSON.parse(execFileSync(process.env.FINIO_TEST_PYTHON || 'python3',
  ['-m', 'tests.edit_payloads'], {cwd:new URL('..',import.meta.url), env:{...process.env,FINIO_DISABLE_LLM:'1'}, encoding:'utf8'}));
for (const payload of pythonEdits) {
  const previous = (await pg.query('select summary_json from snapshots')).rows[0].summary_json;
  const state = (await pg.query('select finio_edit_analysis($1,$2,$3,$4,$5) as state',
    [await revision(), JSON.stringify(payload.p_summary), JSON.stringify(payload.p_profile), payload.p_label, false])).rows[0].state;
  const stored = (await pg.query('select summary_json from snapshots')).rows[0].summary_json;
  assert.equal(stored.metrics.total_spent, payload.p_label === 'transfer' ? 125 : 100);
  assert.equal((await pg.query('select target_amount from goals')).rows[0].target_amount, payload.p_label === 'goal' ? '7000' : '6000');
  if (payload.p_label === 'budget') assert.equal((await pg.query("select monthly_limit from budgets where category='Groceries'")).rows[0].monthly_limit,'350');
  if (payload.p_label === 'profile') assert.equal((await pg.query('select age from users')).rows[0].age,35);
  if (payload.p_label === 'balance') assert.deepEqual(stored.cash_balance,{amount:1234.56,as_of:'2026-06-30'});
  if (payload.p_label === 'statement_balance') assert.deepEqual(stored.statement_balance,{amount:1080,as_of:'2026-06-11'});
  await edit(state.revision, null, {}, true);
  assert.deepEqual((await pg.query('select summary_json from snapshots')).rows[0].summary_json,previous);
}
console.log('PASS: real Python budget, goal, profile, transfer and cash balance payloads persist and undo in PostgreSQL');
// Production has date budget months and bigint IDs; reproduce its month type.
await pg.exec('reset role; alter table budgets alter column month type date using month::date;');
await pg.exec(await readFile(new URL('../migrations/005_verified_edits.sql', import.meta.url),'utf8'));
await pg.exec('set role authenticated;');
assert.equal((await pg.query('select finio_ready_v6() as ready')).rows[0].ready,false);
await assert.rejects(edit(await revision(), pythonEdits[0].p_summary), {code:'42804'});
await pg.exec('reset role;');
await pg.exec(await readFile(new URL('../migrations/006_budget_month_compatibility.sql', import.meta.url),'utf8'));
await pg.exec(await readFile(new URL('../migrations/006_budget_month_compatibility.sql', import.meta.url),'utf8'));
await pg.exec('set role authenticated;');
assert.equal((await pg.query('select finio_ready_v6() as ready')).rows[0].ready,true);
for (const payload of pythonEdits) {
  const state = (await edit(await revision(),payload.p_summary,payload.p_profile)).rows[0].state;
  assert.equal(state.can_undo,true);
  assert.equal((await pg.query('select summary_json from snapshots')).rows[0].summary_json.metrics.total_spent,payload.p_label==='transfer'?125:100);
  await edit(state.revision,null,{},true);
}
await commit([{category:'Groceries',suggested_limit:400}]);
console.log('PASS: reproduced live 42804 failure; migration 006 fixes edits, undo and uploads with date budget months');
await pg.query("select set_config('request.jwt.claim.sub', $1, false)", [other]);
assert.equal((await pg.query('update transactions set amount = 999 where user_id = $1 returning id', [uid])).rows.length, 0);
assert.equal((await pg.query('delete from transactions where user_id = $1 returning id', [uid])).rows.length, 0);
await assert.rejects(pg.query('insert into snapshots(user_id,month,summary_json) values($1,$2,$3)',
  [uid, '2026-07-01', '{}']), /row-level security/);
assert.equal((await pg.query('select * from match_merchants($1, null, 5)', [uid])).rows.length, 0);
for (const table of ['users','transactions','snapshots','goals','budgets','streaks','merchant_embeddings','chat_history','spend_checks']) {
  assert.equal((await pg.query(`select count(*)::int as n from ${table}`)).rows[0].n, 0, table);
}
await assert.rejects(edit(await revision(), updatedSummary, {}, true), /Upload a statement first/);
await assert.rejects(edit(initialRevision, updatedSummary), /Data changed/);
const early = (await edit(null, null, {email:'other-synthetic@example.invalid',age:28})).rows[0].state;
assert.equal(early.can_undo, false);
await assert.rejects(edit(null, null, {age:29}), /Data changed/);
const earlySaved = (await edit(early.revision, null, {age:29})).rows[0].state;
assert.notEqual(earlySaved.revision, early.revision);
await pg.exec(`reset role;
  drop policy if exists users_own on users;
`);
// Match deployed SELECT/INSERT/UPDATE-only layouts for the three affected tables.
for (const table of ['users','chat_history','streaks']) {
  const policies = (await pg.query('select policyname from pg_policies where tablename=$1 and cmd=\'ALL\'', [table])).rows;
  for (const {policyname} of policies) await pg.exec(`drop policy "${policyname.replaceAll('"','""')}" on ${table}`);
  const col = table === 'users' ? 'id' : 'user_id';
  await pg.exec(`create policy audit_read on ${table} for select to authenticated using (auth.uid()=${col});
    create policy audit_insert on ${table} for insert to authenticated with check (auth.uid()=${col});
    create policy audit_update on ${table} for update to authenticated using (auth.uid()=${col}) with check (auth.uid()=${col});`);
}
await pg.exec('drop policy finio_delete_own_profile on users; set role authenticated;');
assert.equal((await pg.query('select finio_ready_v7() as ready')).rows[0].ready, false);
await pg.query("insert into transactions(user_id,date,amount,merchant,flow) values($1,'2026-10-01',-7,'Rollback fixture','expense')", [other]);
await pg.query("insert into chat_history(user_id,role,message,chat_id) values($1,'assistant','Keep on failed deletion','rollback')", [other]);
await assert.rejects(pg.query('select finio_delete_data()'), /Deletion incomplete/);
assert.equal((await pg.query('select count(*)::int as n from users')).rows[0].n, 1);
assert.equal((await pg.query('select count(*)::int as n from transactions')).rows[0].n, 1);
assert.equal((await pg.query('select count(*)::int as n from chat_history')).rows[0].n, 1);
await pg.exec('reset role;');
await pg.exec(await readFile(new URL('../migrations/20261029120515_privacy_and_chat_contracts.sql', import.meta.url),'utf8'));
await pg.exec('set role authenticated;');
assert.equal((await pg.query('select finio_ready_v7() as ready')).rows[0].ready, true);
await pg.query('select public.finio_delete_data()');
await pg.query("select set_config('request.jwt.claim.sub', $1, false)", [uid]);
assert.equal((await pg.query('select count(*)::int as n from transactions')).rows[0].n, 3);
await pg.query("insert into chat_history(user_id,role,message,chat_id) values($1,'title','Saved title','test')", [uid]);
await pg.query("insert into chat_history(user_id,role,message,chat_id) values($1,'assistant','Hello','test')", [uid]);
await pg.query('select public.finio_delete_data()');
for (const table of ['users','transactions','snapshots','goals','budgets','streaks','merchant_embeddings','chat_history','spend_checks']) {
  assert.equal((await pg.query(`select count(*)::int as n from ${table}`)).rows[0].n, 0, table);
}
assert.equal((await pg.query('select count(*)::int as n from transactions')).rows[0].n, 0);
await pg.exec('reset role');
assert.equal((await pg.query('select count(*)::int as n from auth.users')).rows[0].n, 2);
console.log('PASS: two-user isolation, deletion scope, retained auth accounts, anonymous RPC protection');
await pg.close();
