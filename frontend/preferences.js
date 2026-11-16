// Account-scoped display preferences; never credentials or financial records.
export function browserStorage() {
  try { return window.localStorage; } catch (_) { return null; }
}

export function preferenceKey(name, userId) {
  return userId ? `finio:${name}:${userId}` : null;
}

export function readPreference(storage, key, fallback, validate) {
  if (!key) return fallback;
  try {
    const raw = storage.getItem(key);
    if (raw === null) return fallback;
    const parsed = JSON.parse(raw);
    return validate(parsed) ? parsed : fallback;
  } catch (_) { return fallback; }
}

export function writePreference(storage, key, value) {
  if (!key) return false;
  try { storage.setItem(key, JSON.stringify(value)); return true; }
  catch (_) { return false; }
}
