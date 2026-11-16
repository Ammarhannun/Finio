import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const source = await readFile(new URL('../frontend/financial-facts.js', import.meta.url), 'utf8');
const { factLines } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const money = n => `$${n.toFixed(2)}`;
test('refunds are displayed once beside the authoritative net total', () => {
  const lines = factLines({kind:'category_total', query:'COLES', count:2,
    gross_purchases:110, refunds:10, total:100}, money);
  assert.equal(lines[1], '$110.00 purchases − $10.00 refunds = $100.00 net spending');
});
test('transfer exclusions stay visible alongside summary totals', () => {
  const lines = factLines({kind:'summary', income:2000, spent:100, saved:1900,
    transfer_review:{count:1, money_out:25, money_in:0}}, money);
  assert.equal(lines[0], 'Income $2000.00 · Spending $100.00 · Net saved $1900.00');
  assert.match(lines[1], /1 transfers need review.*\$25.00 out.*excluded/);
});
test('missing data cannot appear as a zero balance or amount', () => {
  assert.match(factLines({kind:'summary', income:null, spent:undefined, saved:NaN}, money)[0], /Income Unavailable · Spending Unavailable · Net saved Unavailable/);
});
test('unrecognized model-generated records cannot render calculated facts', () => {
  assert.deepEqual(factLines({kind:'model_claim', total:42}, money), []);
});
test('bank balance facts identify their actual source and date',()=>{
  const lines=factLines({kind:'balance',amount:9500,as_of:'2026-10-29',source:'saved',is_current:true},money);
  assert.match(lines[0],/Confirmed available balance: \$9500.00/);
  assert.equal(lines[1],'Manually confirmed · As of 2026-10-29');
  assert.match(lines[2],/No live bank connection/);
});
