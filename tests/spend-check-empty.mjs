import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createContext, SourceTextModule, SyntheticModule } from 'node:vm';

const html = await readFile(new URL('../frontend/spend-check.html', import.meta.url), 'utf8');
const source = html.match(/<script type="module">([\s\S]*?)<\/script>/)[1];

async function renderPage(dashboardResult) {
  const fields = new Map();
  const field = id => {
    if (!fields.has(id)) fields.set(id, {
      style: { display: id === 'no-data' ? 'none' : '' },
      textContent: id === 'saved-balance-info' ? 'Loading your saved balance…' : '',
      innerHTML: '',
      addEventListener() {},
    });
    return fields.get(id);
  };
  const calls = [];
  let periodMounted = false;
  const apiExports = {
    apiFetch: async path => {
      calls.push(path);
      if (path === '/dashboard') {
        if (dashboardResult instanceof Error) throw dashboardResult;
        return dashboardResult;
      }
      if (path === '/spend-check/history') return { checks: [] };
      throw new Error(`Unexpected request: ${path}`);
    },
    requireAuth: async () => true,
    setupNav() {},
    formatAUD: amount => `$${amount.toFixed(2)}`,
    showToast() {},
    mountPeriodBar() { periodMounted = true; },
    escapeHtml: value => value,
    getStoredPeriod: () => 'all',
    storedPeriodParts: () => ({ period: 'all', month: null }),
    formatDay: value => value,
    dateRangeLabel: () => '1 Jan 2026',
  };
  const context = createContext({ document: { getElementById: field } });
  const api = new SyntheticModule(Object.keys(apiExports), function () {
    for (const [name, value] of Object.entries(apiExports)) this.setExport(name, value);
  }, { context });
  const coach = new SyntheticModule(['mountCoachWidget'], function () {
    this.setExport('mountCoachWidget', () => {});
  }, { context });
  const module = new SourceTextModule(source, { context });
  await module.link(specifier => specifier === './api.js' ? api : coach);
  await module.evaluate();
  await new Promise(setImmediate);
  return { field, calls, get periodMounted() { return periodMounted; } };
}

test('an account without a statement sees an upload action, not a balance failure', async () => {
  const error = new Error('no_analysis');
  error.status = 404;
  const page = await renderPage(error);
  assert.equal(page.field('no-data').style.display, 'block');
  assert.equal(page.field('spend-content').style.display, 'none');
  assert.deepEqual(page.calls, ['/dashboard']);
  assert.match(html, /<a href="dashboard\.html" class="btn btn-primary">Go to Dashboard<\/a>/);
});

test('a real loading failure keeps the form and shows a retryable error', async () => {
  const error = new Error('Service unavailable');
  error.status = 503;
  const page = await renderPage(error);
  assert.equal(page.field('no-data').style.display, 'none');
  assert.notEqual(page.field('spend-content').style.display, 'none');
  assert.match(page.field('saved-balance-info').textContent, /could not load/);
});

test('an analysed account retains the normal Spend Check form', async () => {
  const page = await renderPage({ balance_view: { amount: 4155.02, as_of: '2026-01-28', is_current: false }, available_months: ['2026-01'] });
  assert.equal(page.field('no-data').style.display, 'none');
  assert.notEqual(page.field('spend-content').style.display, 'none');
  assert.match(page.field('saved-balance-info').textContent, /Last known balance: \$4155\.02/);
  assert.equal(page.periodMounted, true);
});
