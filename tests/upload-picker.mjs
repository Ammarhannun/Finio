import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
// Import as ESM without changing the project's module type or loading auth/AI.
const source = await readFile(new URL('../frontend/upload-picker.js', import.meta.url), 'utf8');
const { initUploadPicker } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));

function setup() {
  const handlers = new Map(), classes = new Set();
  const zone = { addEventListener: (name, fn) => handlers.set(name, fn),
    classList: { add: name => classes.add(name), remove: name => classes.delete(name) } };
  let changes = 0, error = '', clicked = 0, busy = false;
  const input = { files: [], addEventListener: (name, fn) => handlers.set(name, fn), click: () => clicked++ };
  Object.defineProperty(input, 'value', { set: () => { input.files = []; } });
  const status = {};
  initUploadPicker({ zone, input, status, onChange: () => changes++, onError: text => { error = text; }, isBusy: () => busy });
  const fire = (name, files = [], types = ['Files']) => {
    const event = { dataTransfer: { files, types }, prevented: false, preventDefault() { this.prevented = true; } };
    handlers.get(name)(event);
    return event;
  };
  return { input, status, classes, fire, changes: () => changes, error: () => error,
    clicked: () => clicked, busy: () => { busy = true; } };
}
const file = (name = 'statement.csv', size = 100) => ({ name, size });

test('drop selects a file and invalidates the old review without submitting', () => {
  const s = setup(), files = [file()];
  assert.equal(s.fire('drop', files).prevented, true);
  assert.equal(s.input.files, files);
  assert.equal(s.changes(), 1);
  assert.equal(s.error(), '');
  assert.match(s.status.textContent, /statement.csv selected/);
});
test('click opens the native picker; uppercase PDF selection is accepted', () => {
  const s = setup(); s.fire('click'); assert.equal(s.clicked(), 1);
  s.input.files = [file('STATEMENT.PDF')]; s.fire('change');
  assert.equal(s.error(), ''); assert.equal(s.changes(), 1);
});
test('drag-over enables dropping; nested children do not flicker the highlight', () => {
  const s = setup(); s.fire('dragenter'); s.fire('dragenter'); s.fire('dragleave');
  assert.equal(s.classes.has('drag-over'), true);
  const event = s.fire('dragover'); assert.equal(event.prevented, true);
  assert.equal(event.dataTransfer.dropEffect, 'copy');
  s.fire('drop', [file()]); assert.equal(s.classes.has('drag-over'), false);
});
for (const [label, files, message] of [
  ['multiple', [file(), file('other.pdf')], /one statement/],
  ['unsupported', [file('image.png')], /CSV or PDF/],
  ['oversize', [file('large.csv', 10 * 1024 * 1024 + 1)], /10 MB/],
  ['empty', [file('empty.csv', 0)], /empty/],
]) test(`${label} drops clear previous selection and show a useful error`, () => {
  const s = setup(); s.fire('drop', [file()]); s.fire('drop', files);
  assert.equal(s.input.files.length, 0); assert.match(s.error(), message);
  assert.equal(s.changes(), 2);
});
test('an invalid native-picker selection uses the same validation', () => {
  const s = setup(); s.input.files = [file('image.png')]; s.fire('change');
  assert.equal(s.input.files.length, 0); assert.match(s.error(), /CSV or PDF/);
});
test('busy uploads cannot be replaced by another drop or click', () => {
  const s = setup(); s.fire('drop', [file('first.csv')]); s.busy();
  s.fire('drop', [file('second.csv')]); s.fire('click');
  assert.equal(s.input.files[0].name, 'first.csv'); assert.equal(s.clicked(), 0);
  assert.equal(s.changes(), 1);
});
test('non-file drags are left alone', () => {
  const s = setup(); assert.equal(s.fire('dragover', [], ['text/plain']).prevented, false);
  s.fire('drop', [], ['text/plain']); assert.equal(s.changes(), 0);
});
