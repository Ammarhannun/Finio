import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const source = await readFile(new URL('../frontend/page-refresh.js', import.meta.url), 'utf8');
const { watchDailyRefresh, nextLocalDayDelay, publishSavedChange, dataChangeKey } =
  await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));

const flush = async () => { for (let i = 0; i < 15; i++) await Promise.resolve(); };
function harness({ start = '2026-10-29T23:59:50', refresh = () => true, ...options } = {}) {
  let clock = new Date(start).getTime(), nextId = 0, calls = 0;
  const tasks = new Map(), states = [];
  const target = new EventTarget(), doc = new EventTarget();
  doc.hidden = false;
  const timers = {
    setTimeout(fn, delay) { const id = ++nextId; tasks.set(id, { at: clock + delay, fn }); return id; },
    clearTimeout(id) { tasks.delete(id); },
  };
  const watcher = watchDailyRefresh({ userId: 'owner', target, doc, timers,
    now: () => new Date(clock), initialSuccess: new Date(clock),
    refresh: () => { calls++; return refresh(); }, onState: s => states.push(s), ...options });
  return {
    watcher, target, doc, states, tasks,
    get calls() { return calls; },
    get now() { return new Date(clock); },
    async advance(ms) {
      const end = clock + ms;
      while (true) {
        const next = [...tasks.entries()].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break;
        clock = next[1].at; tasks.delete(next[0]); next[1].fn(); await flush();
      }
      clock = end; await flush();
    },
    emit(type, detail) { target.dispatchEvent(new CustomEvent(type, { detail })); },
    stored(key, newValue = 'changed') {
      const event = new Event('storage'); Object.assign(event, { key, newValue }); target.dispatchEvent(event);
    },
    async visibility(hidden) { doc.hidden = hidden; doc.dispatchEvent(new Event('visibilitychange')); await flush(); },
  };
}

test('an open page checks at each local midnight without reloading it', async () => {
  const h = harness();
  await h.advance(9_999); assert.equal(h.calls, 0);
  await h.advance(30); assert.equal(h.calls, 1);
  await h.advance(24 * 60 * 60 * 1000); assert.equal(h.calls, 2);
  assert.equal(h.states.at(-1).status, 'updated');
  h.watcher.destroy(); assert.equal(h.tasks.size, 0);
});

test('midnight follows the local calendar across daylight saving and year boundaries', () => {
  const previous = process.env.TZ; process.env.TZ = 'Australia/Sydney';
  try {
    assert.equal(nextLocalDayDelay(new Date(2026, 9, 4, 0)), 23 * 60 * 60 * 1000 + 25);
    assert.equal(nextLocalDayDelay(new Date(2026, 3, 5, 0)), 25 * 60 * 60 * 1000 + 25);
    assert.equal(nextLocalDayDelay(new Date(2026, 11, 31, 23, 59, 59)), 1025);
  } finally {
    if (previous === undefined) delete process.env.TZ; else process.env.TZ = previous;
  }
});

test('hidden overnight tabs defer work and refresh once on return', async () => {
  const h = harness(); await h.visibility(true);
  await h.advance(48 * 60 * 60 * 1000); assert.equal(h.calls, 0);
  await h.visibility(false); assert.equal(h.calls, 1);
  h.emit('focus'); await flush(); assert.equal(h.calls, 1);
  h.watcher.destroy();
});

test('returning to an older visible tab rechecks, with focus/visibility deduplication', async () => {
  const h = harness({ start: '2026-10-30T12:00:00' });
  h.emit('focus'); await flush(); assert.equal(h.calls, 0);
  await h.advance(61_000); h.emit('focus'); await h.visibility(false);
  assert.equal(h.calls, 1); h.watcher.destroy();
});

test('saved edits and Undo refresh once, including same-account changes in other tabs', async () => {
  const h = harness();
  h.emit('finio:data-changed'); h.emit('finio:data-changed', { userId: 'owner' });
  await h.advance(121); assert.equal(h.calls, 1);
  h.stored(dataChangeKey('another-account')); h.emit('finio:data-changed', { userId: 'another-account' });
  await h.advance(121); assert.equal(h.calls, 1);
  h.stored(dataChangeKey('owner')); await h.advance(121); assert.equal(h.calls, 2);
  h.watcher.destroy();
});

test('changes arriving during a read trigger one follow-up without overlapping reads', async () => {
  let resolve, active = 0, maximum = 0;
  const h = harness({ refresh: () => { active++; maximum = Math.max(maximum, active);
    return new Promise(done => { resolve = () => { active--; done(true); }; }); } });
  const pending = h.watcher.refresh(); await flush();
  h.emit('finio:data-changed'); await h.advance(121);
  h.emit('finio:data-changed'); await h.advance(121); assert.equal(h.calls, 1);
  resolve(); await pending; await h.advance(121); assert.equal(h.calls, 2);
  resolve(); await flush(); assert.equal(maximum, 1); h.watcher.destroy();
});

test('failed checks keep the real last-success date and do not create a retry loop', async () => {
  let okay = false;
  const h = harness({ refresh: () => { if (!okay) throw new Error('offline'); return true; } });
  const last = h.now.getTime();
  assert.equal(await h.watcher.refresh(), false);
  assert.equal(h.states.at(-1).status, 'error');
  assert.equal(h.states.at(-1).lastSuccess.getTime(), last);
  await h.advance(1000); assert.equal(h.calls, 1);
  okay = true; h.emit('online'); await flush();
  assert.equal(h.calls, 2); assert.equal(h.states.at(-1).status, 'updated');
  h.watcher.destroy();
});

test('back-forward cache restoration resumes checks; destroyed pages stop all work', async () => {
  const h = harness(); h.emit('pagehide'); await h.advance(120_000);
  assert.equal(h.calls, 0); assert.equal(h.tasks.size, 0);
  h.emit('pageshow'); await flush(); assert.equal(h.calls, 1);
  h.watcher.destroy(); h.emit('finio:data-changed'); h.emit('focus');
  await h.advance(24 * 60 * 60 * 1000); assert.equal(h.calls, 1);
});

test('successful mutations emit opaque notifications, while reads and proposals emit none', () => {
  const target = new EventTarget(), signals = [], stored = [];
  target.addEventListener('finio:data-changed', e => signals.push(e.detail));
  const storage = { setItem: (...args) => stored.push(args) };
  for (const path of ['/dashboard', '/coach', '/reclassify', '/analyze/preview', '/spend-check']) {
    assert.equal(publishSavedChange(path, 'POST', 'owner', target, storage), false);
  }
  assert.equal(publishSavedChange('/overrides', 'GET', 'owner', target, storage), false);
  for (const path of ['/analyze', '/overrides', '/quiz', '/changes/undo', '/balance', '/reanalyze']) {
    assert.equal(publishSavedChange(path, 'POST', 'owner', target, storage), true);
  }
  assert.equal(signals.length, 6); assert.equal(stored.length, 6);
  assert.deepEqual(signals[0], { userId: 'owner' });
  assert.equal(stored[0][0], dataChangeKey('owner'));
  assert.match(stored[0][1], /^\d+:[\d.]+$/);
  assert.doesNotThrow(() => publishSavedChange('/goal', 'POST', 'owner', target, {
    setItem() { throw new Error('storage blocked'); },
  }));
});
