import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createContext, SourceTextModule, SyntheticModule } from 'node:vm';
const apiSource = await readFile(new URL('../frontend/api.js', import.meta.url), 'utf8');
const refreshSource = await readFile(new URL('../frontend/page-refresh.js', import.meta.url), 'utf8');
const sessionPrivacySource = await readFile(new URL('../frontend/session-privacy.js', import.meta.url), 'utf8');
const accountSecuritySource = await readFile(new URL('../frontend/account-security.js', import.meta.url), 'utf8');
const flush = async () => { for (let i = 0; i < 15; i++) await Promise.resolve(); };
const response = (data, status = 200) => ({ status, ok: status < 400, json: async () => data });

async function harness() {
  const target = new EventTarget(), stored = new Map(), signals = [], rendered = [], pending = [];
  let authListener, cleared = 0;
  const redirects = [];
  let assurance = { currentLevel: 'aal1', nextLevel: 'aal1' };
  target.location = { href: '', replace: path => redirects.push(path), reload: () => redirects.push('reload') };
  target.localStorage = { getItem: k => stored.get(k), setItem: (k, v) => stored.set(k, v) };
  target.addEventListener('finio:data-changed', e => signals.push(e.detail));
  const elements = { loading: { style: {} }, content: { style: { display: 'block' } }, 'no-data': { style: {} },
    'period-mount': { innerHTML: '', querySelector: () => null } };
  let fetcher = async () => response({ label: 'initial', available_months: ['2026-10'] });
  const context = createContext({ window: target, localStorage: target.localStorage,
    location: { hostname: 'localhost', pathname: '/dashboard.html' }, FormData, CustomEvent, console,
    document: { getElementById: id => elements[id] || null, body: {replaceChildren(){cleared++;}} },
    fetch: (...args) => fetcher(...args),
  });
  const stub = exports => new SyntheticModule(Object.keys(exports), function () {
    for (const [key, value] of Object.entries(exports)) this.setExport(key, value);
  }, { context });
  const modules = {
    './mobile-navigation.js': stub({ mountMobileNavigation() {} }),
    './page-refresh.js': new SourceTextModule(refreshSource, { context }),
    './session-privacy.js': new SourceTextModule(sessionPrivacySource, { context }),
    './account-security.js': new SourceTextModule(accountSecuritySource, { context }),
    './config.js': stub({ API_BASE_URL: 'http://api.invalid', CURRENCY: 'AUD', LOCALE: 'en-AU' }),
    'https://esm.sh/@supabase/supabase-js@2.117.2': stub({ createClient: () => ({ auth: {
      getSession: async () => ({ data: { session: { access_token: 'synthetic', user: { id: 'owner' } } } }),
      signOut: async () => {},
      mfa: { getAuthenticatorAssuranceLevel: async () => ({data:assurance}) },
      onAuthStateChange: callback => { authListener = callback; return {data:{subscription:{unsubscribe(){}}}}; },
    } }) }),
  };
  const api = new SourceTextModule(apiSource, { context });
  await api.link(specifier => modules[specifier]); await api.evaluate();
  return { api: api.namespace, signals, stored, elements, rendered, pending, redirects,
    setAssurance(value){assurance=value;},
    get cleared(){return cleared;},
    auth(event,session){authListener(event,session);},
    restore(){const event = new Event('pageshow'); event.persisted = true; target.dispatchEvent(event);},
    setFetch(fn) { fetcher = fn; },
    defer() { fetcher = (url, options) => new Promise(resolve => pending.push({ url, options, resolve })); },
  };
}

test('authenticated pages clear their financial DOM before leaving a signed-out account', async () => {
  const h=await harness(); await h.api.requireAuth();
  h.auth('SIGNED_OUT',null);
  assert.equal(h.cleared,1);assert.deepEqual(h.redirects,['login.html']);
});

test('restored authenticated pages clear their old DOM and reload through auth', async () => {
  const h=await harness();await h.api.requireAuth();h.restore();
  assert.equal(h.cleared,1);assert.deepEqual(h.redirects,['reload']);
});

test('only committed API writes notify other views; failed writes leave them alone', async () => {
  const h = await harness();
  await h.api.apiFetch('/quiz', { method: 'POST', body: '{}' });
  assert.equal(h.signals.length, 1); assert.ok(h.stored.get('finio-data-updated:owner'));
  await h.api.apiFetch('/coach', { method: 'POST', body: '{}' });
  await h.api.apiFetch('/overrides'); assert.equal(h.signals.length, 1);
  h.setFetch(async () => response({ detail: 'Stale revision' }, 409));
  await assert.rejects(h.api.apiFetch('/quiz', { method: 'POST', body: '{}' }), /Stale revision/);
  assert.equal(h.signals.length, 1);
});

test('a slow old period cannot replace a newer selection or its month options', async () => {
  const h = await harness();
  const load = await h.api.bootstrapPeriodPage('/dashboard', data => h.rendered.push(data.label));
  h.defer();
  const old = load('weekly'), latest = load('all'); await flush();
  assert.equal(h.pending.length, 2);
  assert.equal(h.pending[1].url, 'http://api.invalid/dashboard?period=all');
  assert.equal(h.pending[1].options.cache, 'no-store');
  h.pending[1].resolve(response({ label: 'new', available_months: ['2026-11', '2026-10'] }));
  assert.equal(await latest, true);
  h.pending[0].resolve(response({ label: 'old', available_months: ['2026-09'] }));
  assert.equal(await old, null);
  assert.deepEqual(h.rendered, ['initial', 'new']);
  assert.match(h.elements['period-mount'].innerHTML, /November 2026/);
  assert.doesNotMatch(h.elements['period-mount'].innerHTML, /September 2026/);
});

test('a failed daily read preserves displayed data and a retry uses the selected period', async () => {
  const h = await harness();
  const load = await h.api.bootstrapPeriodPage('/dashboard', data => h.rendered.push(data.label));
  h.setFetch(async () => { throw new Error('offline'); });
  assert.equal(await load('month:2026-10', { quiet: true }), false);
  assert.deepEqual(h.rendered, ['initial']);
  h.setFetch(async url => {
    assert.equal(url, 'http://api.invalid/dashboard?period=monthly&month=2026-10');
    return response({ label: 'recovered' });
  });
  assert.equal(await load('month:2026-10', { quiet: true }), true);
  assert.deepEqual(h.rendered, ['initial', 'recovered']);
});

test('deleted data is cleared from an open view instead of lingering as a stale result', async () => {
  const h = await harness();
  const load = await h.api.bootstrapPeriodPage('/dashboard', () => { h.elements.content.style.display = 'block'; });
  h.setFetch(async () => response({ detail: 'No analysis' }, 404));
  assert.equal(await load('all', { quiet: true }), false);
  assert.equal(h.elements.content.style.display, 'none');
  assert.equal(h.elements['no-data'].style.display, 'block');
  h.setFetch(async () => response({ label: 'uploaded again' }));
  assert.equal(await load('all', { quiet: true }), true);
  assert.equal(h.elements.content.style.display, 'block');
  assert.equal(h.elements['no-data'].style.display, 'none');
});

test('a pending authenticator step clears private content and uses a safe return page', async () => {
  const h = await harness();
  h.setAssurance({currentLevel:'aal1',nextLevel:'aal2'});
  await assert.rejects(h.api.requireAuth(),/mfa_required/);
  assert.equal(h.cleared,1);
  assert.deepEqual(h.redirects,['mfa.html?next=dashboard.html']);
});

test('sensitive-action errors retain an actionable message and verification code', async () => {
  const h = await harness();
  h.setFetch(async()=>response({detail:{code:'reauth_required',message:'Confirm your identity again.'}},403));
  await assert.rejects(h.api.apiFetch('/data/export'),error=>
    error.code==='reauth_required' && error.status===403 && error.message==='Confirm your identity again.');
});
