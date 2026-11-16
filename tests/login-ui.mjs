import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createContext, SourceTextModule, SyntheticModule } from 'node:vm';

const html = await readFile(new URL('../frontend/login.html', import.meta.url), 'utf8');
const source = html.match(/<script type="module">([\s\S]*?)<\/script>/)[1];

async function loginPage(search = '') {
  const fields = new Map();
  const element = () => ({ value: '', textContent: '', style: {}, hidden: false,
    group: { hidden: false }, classList: { toggle() {} },
    closest() { return this.group; }, addEventListener() {},
  });
  const field = id => { if (!fields.has(id)) fields.set(id, element()); return fields.get(id); };
  const tabs = ['login', 'signup'].map(tab => ({ ...element(), dataset: { tab } }));
  let sessionCallback, authCallback;
  const location = { search, hash: '', href: 'https://app.example.invalid/login.html' + search };
  const context = createContext({ URLSearchParams, URL, location, window: { location },
    document: { getElementById: field, querySelectorAll: () => tabs, querySelector: field },
  });
  const api = new SyntheticModule(['supabase', 'getSession', 'initThemeToggle'], function () {
    this.setExport('supabase', { auth: { onAuthStateChange(fn) { authCallback = fn; } } });
    this.setExport('getSession', () => ({ then(fn) { sessionCallback = fn; } }));
    this.setExport('initThemeToggle', () => {});
  }, { context });
  const module = new SourceTextModule(source, { context });
  await module.link(() => api); await module.evaluate();
  return { location, field, session: value => sessionCallback(value), event: value => authCallback(value) };
}

test('an existing session goes straight to Dashboard without a removed signup helper', async () => {
  const page = await loginPage();
  await page.session({ user: { id: 'synthetic' } });
  assert.equal(page.location.href, 'dashboard.html');
});

test('a signed-out visitor stays on the login form', async () => {
  const page = await loginPage();
  await page.session(null);
  assert.equal(page.location.href, 'https://app.example.invalid/login.html');
  assert.equal(page.field('auth-btn').textContent, 'Log in');
});

test('password recovery keeps its form even when a recovery session exists', async () => {
  const page = await loginPage('?reset=1');
  await page.session({ user: { id: 'synthetic' } });
  assert.match(page.location.href, /reset=1$/);
  assert.equal(page.field('auth-btn').textContent, 'Save new password');
  assert.equal(page.field('email').group.hidden, true);
  assert.equal(page.field('password').minLength, 12);
});

test('the recovery event takes priority over the ordinary session redirect', async () => {
  const page = await loginPage();
  page.event('PASSWORD_RECOVERY');
  await page.session({ user: { id: 'synthetic' } });
  assert.equal(page.field('auth-btn').textContent, 'Save new password');
  assert.equal(page.location.href, 'https://app.example.invalid/login.html');
});
