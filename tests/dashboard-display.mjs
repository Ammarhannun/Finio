import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const source = await readFile(new URL('../frontend/dashboard-display.js', import.meta.url), 'utf8');
const {merchantLabel, surplusLabel} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));

test('unreviewed transfers make the surplus provisional', () => {
  assert.equal(surplusLabel({count:2}), 'Surplus · provisional');
  assert.equal(surplusLabel({count:0}), 'Income − spending');
  assert.equal(surplusLabel(null), 'Income − spending');
});
test('merchant labels simplify known bank descriptions without changing unknown merchants', () => {
  const raw = 'UBER EATS HELP.UBER.COM AU';
  assert.equal(merchantLabel(raw), 'Uber Eats');
  assert.equal(raw, 'UBER EATS HELP.UBER.COM AU');
  assert.equal(merchantLabel('WOOLWORTHS 1339 RYDE'), 'Woolworths');
  assert.equal(merchantLabel('TRANSPORTFORNSW TAP'), 'Transport for NSW');
  assert.equal(merchantLabel('UBER EATSY PRIVATE CAFE'), 'UBER EATSY PRIVATE CAFE');
  assert.equal(merchantLabel('ALEX SMITH'), 'ALEX SMITH');
});
