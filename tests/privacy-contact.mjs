import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { SourceTextModule, SyntheticModule, createContext } from 'node:vm';

const source = await readFile(new URL('../frontend/privacy-contact.js', import.meta.url), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));

async function mount({ enabled = true, response = { received: true, reference: '012345abcdef' }, statusCode = 202 } = {}) {
  const elements = {
    'privacy-contact-fields': { disabled: true },
    'privacy-contact-status': { textContent: '' },
    'privacy-contact-send': { disabled: true },
  };
  const listeners = {};
  const form = {
    elements: { email: { value: 'qa@example.invalid' }, topic: { value: 'question' },
      message: { value: 'A synthetic privacy request.' }, website: { value: '' } },
    addEventListener: (event, fn) => { listeners[event] = fn; },
    reportValidity: () => true,
    reset() { this.elements.email.value = ''; this.elements.message.value = ''; },
  };
  elements['privacy-contact-form'] = form;
  let callbacks, postCount = 0;
  const window = { turnstile: {
    render(_target, options) { callbacks = options; return 1; },
    // Emulate the asynchronous success callback after resetting a dummy widget.
    reset() { queueMicrotask(() => callbacks.callback('new-synthetic-token')); },
  } };
  const context = createContext({ window, AbortController, setTimeout, clearTimeout, TypeError,
    document: { getElementById: id => elements[id], createElement: () => ({}),
      head: { appendChild: script => queueMicrotask(() => script.onload()) } },
    fetch: async (_path, options) => {
      if (!options.method) return { ok: true, json: async () => ({ enabled, site_key: 'synthetic-site-key' }) };
      postCount++;
      assert.equal(options.credentials, 'omit');
      assert.equal(options.redirect, 'error');
      return { ok: statusCode === 202, status: statusCode, json: async () => response };
    },
  });
  const config = new SyntheticModule(['API_BASE_URL'], function () {
    this.setExport('API_BASE_URL', 'https://synthetic-api.example.invalid');
  }, { context });
  const script = new SourceTextModule(source, { context });
  await script.link(() => config);
  await script.evaluate();
  await tick();
  return { form, elements, callbacks, listeners, posts: () => postCount,
    submit: () => listeners.submit({ preventDefault() {} }) };
}

{
  const ui = await mount();
  await ui.submit();
  assert.equal(ui.posts(), 0, 'No challenge token must mean no submission');
  ui.callbacks.callback('synthetic-token');
  await ui.submit(); await tick();
  assert.equal(ui.posts(), 1);
  assert.match(ui.elements['privacy-contact-status'].textContent, /Request sent.*012345abcdef/);
  assert.equal(ui.form.elements.message.value, '');
  assert.equal(ui.elements['privacy-contact-fields'].disabled, false);
}
{
  const ui = await mount({ statusCode: 503 });
  ui.callbacks.callback('synthetic-token');
  await ui.submit(); await tick();
  assert.match(ui.elements['privacy-contact-status'].textContent, /could not confirm delivery/);
  assert.equal(ui.form.elements.message.value, 'A synthetic privacy request.');
  ui.callbacks.callback('another-synthetic-token');
  assert.match(ui.elements['privacy-contact-status'].textContent, /could not confirm delivery/);
  ui.listeners.input();
  assert.equal(ui.elements['privacy-contact-status'].textContent, 'Ready to send.');
}
{
  const ui = await mount({ enabled: false });
  assert.equal(ui.elements['privacy-contact-fields'].disabled, true);
  assert.equal(ui.elements['privacy-contact-send'].disabled, true);
  assert.match(ui.elements['privacy-contact-status'].textContent, /being set up/);
  assert.equal(ui.posts(), 0);
}
{
  const ui = await mount({ response: { detail: '<script>untrusted provider text</script>' } });
  ui.callbacks.callback('synthetic-token');
  await ui.submit(); await tick();
  assert.match(ui.elements['privacy-contact-status'].textContent, /could not confirm delivery/);
  assert.doesNotMatch(ui.elements['privacy-contact-status'].textContent, /script|provider text/);
  assert.equal(ui.form.elements.message.value, 'A synthetic privacy request.');
}
console.log('PASS: contact form requires a challenge, preserves drafts and feedback, fails closed and ignores untrusted provider text');
