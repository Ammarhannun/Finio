// Read-only page refreshes. Calendar checks never re-import statements or run AI.
export const dataChangeKey = userId => `finio-data-updated:${userId}`;
const SAVED_PATHS = new Set([
  '/analyze', '/reanalyze', '/overrides', '/quiz', '/budgets', '/goal',
  '/profile', '/balance', '/changes/undo', '/data/delete',
]);

/** Notify only after a successful save, never for previews or coach proposals. */
export function publishSavedChange(path, method, userId, target = window, storage) {
  if (!userId || String(method).toUpperCase() !== 'POST' || !SAVED_PATHS.has(path.split('?')[0])) return false;
  target.dispatchEvent(new CustomEvent('finio:data-changed', { detail: { userId } }));
  // Only an opaque signal is stored, never transaction or account details.
  try { (storage || target.localStorage).setItem(dataChangeKey(userId), `${Date.now()}:${Math.random()}`); } catch { /* private browsing */ }
  return true;
}

export function nextLocalDayDelay(now) {
  const next = new Date(now);
  // A local calendar boundary handles 23/25-hour daylight-saving days.
  next.setHours(24, 0, 0, 25);
  return Math.max(1, next.getTime() - now.getTime());
}

const dayKey = date => `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;

/**
 * Refresh at local midnight, after edits, and when returning to a stale tab.
 * Background tabs defer network work until visible. Failures preserve the last
 * successful check and wait for an explicit retry, reconnect, focus or next day.
 */
export function watchDailyRefresh({
  refresh, userId, initialSuccess = null, onState = () => {},
  target = window, doc = document, now = () => new Date(),
  timers = globalThis, foregroundAge = 60_000, debounceMs = 120,
}) {
  let lastSuccess = initialSuccess && new Date(initialSuccess);
  let dirty = !lastSuccess;
  let busy = null;
  let queued = false;
  let destroyed = false;
  let suspended = false;
  let dayTimer, changeTimer;
  const visible = () => !doc.hidden && !suspended && !destroyed;
  const state = status => { if (!destroyed) onState({ status, lastSuccess }); };
  const noteSuccess = (at = now()) => { lastSuccess = new Date(at); };

  function scheduleDay() {
    timers.clearTimeout(dayTimer);
    if (destroyed || suspended) return;
    dayTimer = timers.setTimeout(() => {
      scheduleDay();
      request({ force: true });
    }, nextLocalDayDelay(now()));
  }

  function queueChange() {
    dirty = true;
    timers.clearTimeout(changeTimer);
    if (visible()) changeTimer = timers.setTimeout(() => request({ force: true }), debounceMs);
  }

  function request({ force = false } = {}) {
    if (!visible()) {
      if (force) dirty = true;
      return Promise.resolve(false);
    }
    const current = now();
    if (!force && !dirty && lastSuccess && dayKey(current) === dayKey(lastSuccess)
        && current - lastSuccess < foregroundAge) return Promise.resolve(false);
    if (busy) {
      if (force) { queued = true; dirty = true; }
      return busy;
    }
    dirty = false;
    state('loading');
    busy = Promise.resolve().then(refresh).then(result => {
      if (result === false) { dirty = true; state('error'); return false; }
      // null means a newer period request superseded this read.
      if (result !== null) noteSuccess();
      state('updated');
      return result !== null;
    }, () => {
      dirty = true; state('error'); return false;
    }).finally(() => {
      busy = null;
      if (queued) { queued = false; queueChange(); }
    });
    return busy;
  }

  const foreground = () => { scheduleDay(); request(); };
  const changed = event => {
    if (event.detail?.userId && event.detail.userId !== userId) return;
    queueChange();
  };
  const stored = event => {
    if (userId && event.key === dataChangeKey(userId) && event.newValue) queueChange();
  };
  const visibility = () => { if (visible()) foreground(); };
  const online = () => request({ force: true });
  const hide = () => {
    suspended = true;
    timers.clearTimeout(dayTimer); timers.clearTimeout(changeTimer);
  };
  const show = () => { suspended = false; foreground(); };
  const listeners = [
    [doc, 'visibilitychange', visibility], [target, 'focus', foreground],
    [target, 'online', online], [target, 'finio:data-changed', changed],
    [target, 'storage', stored], [target, 'pagehide', hide], [target, 'pageshow', show],
  ];
  listeners.forEach(([node, type, fn]) => node.addEventListener(type, fn));
  scheduleDay();
  return {
    refresh: () => request({ force: true }),
    noteSuccess,
    destroy() {
      destroyed = true;
      timers.clearTimeout(dayTimer); timers.clearTimeout(changeTimer);
      listeners.forEach(([node, type, fn]) => node.removeEventListener(type, fn));
    },
  };
}
