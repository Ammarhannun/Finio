import test from 'node:test';
import assert from 'node:assert/strict';
import { buildBudgetBatch, saveBudgetBatch, undoBudgetBatch } from '../frontend/coach-batch.js';

const revision = '11111111-1111-1111-1111-111111111111';
const budget = (category, amount, extra = {}) =>
  ({ kind: 'budget', category, amount, revision, ...extra });

test('multiple budget cards become one save with one revision', () => {
  const proposals = [budget('Food & Dining', 800), budget('Transport', 250), budget('Groceries', 600)];
  const batch = buildBudgetBatch(proposals);
  assert.deepEqual(batch, { revision, count: 3,
    targets: { 'Food & Dining': 800, Transport: 250, Groceries: 600 } });
  assert.equal(proposals[0].amount, 800); // preparing a batch never changes the cards
});

test('one card or a mixed action reply never offers Apply all budgets', () => {
  assert.equal(buildBudgetBatch([budget('Groceries', 600)]), null);
  assert.equal(buildBudgetBatch([budget('Groceries', 600),
    { kind: 'goal', amount: 6000, revision }]), null);
});

test('stale, missing, or mismatched revisions cannot be batched', () => {
  assert.equal(buildBudgetBatch([budget('Groceries', 600), budget('Transport', 250, { expired: true })]), null);
  assert.equal(buildBudgetBatch([budget('Groceries', 600), budget('Transport', 250, { revision: 'other' })]), null);
  assert.equal(buildBudgetBatch([budget('Groceries', 600), budget('Transport', 250, { revision: null })]), null);
});

test('ambiguous or invalid budget proposals cannot be bundled', () => {
  assert.equal(buildBudgetBatch([budget('Groceries', 600), budget('groceries', 550)]), null);
  assert.equal(buildBudgetBatch([budget('Groceries', 600), budget('Transport', -1)]), null);
  assert.equal(buildBudgetBatch([budget('Groceries', 600), budget('Transport', Infinity)]), null);
  assert.equal(buildBudgetBatch([budget('Groceries', 600), budget('', 250)]), null);
});

test('Apply all makes one request, and Undo targets that one saved revision', async () => {
  const calls = [];
  const request = async (path, options) => {
    calls.push({ path, method: options.method, body: JSON.parse(options.body) });
    return { revision: '22222222-2222-2222-2222-222222222222' };
  };
  const batch = buildBudgetBatch([budget('Food & Dining', 800), budget('Transport', 250)]);
  const state = await saveBudgetBatch(batch, request);
  assert.deepEqual(calls, [{ path: '/budgets', method: 'POST',
    body: { revision, targets: { 'Food & Dining': 800, Transport: 250 } } }]);
  await undoBudgetBatch(state.revision, request);
  assert.deepEqual(calls[1], { path: '/changes/undo', method: 'POST',
    body: { revision: state.revision } });
});

test('a failed batch save does not send any later budget or undo requests', async () => {
  const calls = [];
  const request = async (path, options) => {
    calls.push(path);
    throw Object.assign(new Error('Data changed'), { status: 409 });
  };
  const batch = buildBudgetBatch([budget('Groceries', 600), budget('Transport', 250)]);
  await assert.rejects(saveBudgetBatch(batch, request), /Data changed/);
  assert.deepEqual(calls, ['/budgets']);
});
