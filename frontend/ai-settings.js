export const AI_CONSENT_VERSION = '2026-11-02';

export function mountAISettings({ auth, apiFetch, root = document }) {
  const checkbox = root.getElementById('ai-enabled');
  const save = root.getElementById('ai-save');
  const status = root.getElementById('ai-status');
  let busy = false;
  function feedback(message, tone = '') {
    status.textContent = message;
    status.dataset.tone = tone;
  }
  async function load() {
    checkbox.disabled = save.disabled = true;
    try {
      const setting = await apiFetch('/ai/preferences');
      if (typeof setting?.enabled !== 'boolean' || setting.available === false) throw new Error('unverified');
      checkbox.checked = setting.enabled === true;
      checkbox.disabled = save.disabled = false;
      feedback(setting.enabled ? 'AI sharing is on for new requests.' : 'AI sharing is off for new requests. Finio can still calculate your figures.');
    } catch (_) {
      feedback('Could not check your preference. Reload this page to try again. AI requests stay blocked if the preference cannot be verified.', 'error');
    }
  }
  async function savePreference() {
    if (busy || save.disabled) return;
    busy = true;
    const enabled = checkbox.checked;
    checkbox.disabled = save.disabled = true;
    feedback('Saving your preference…');
    try {
      const result = await auth.updateUser({ data: { finio_ai: { enabled, version: AI_CONSENT_VERSION } } });
      if (result.error) throw new Error('save');
      const verified = await apiFetch('/ai/preferences');
      if (verified.enabled !== enabled || verified.available === false) throw new Error('verify');
      feedback(enabled ? 'Saved. AI sharing is on for new requests.' : 'Saved. AI sharing is off for new requests.', 'success');
      checkbox.disabled = save.disabled = false;
    } catch (_) {
      // Leave controls disabled until a reload establishes the saved state.
      feedback('Could not confirm the saved preference. Reload this page to check it before continuing.', 'error');
    } finally { busy = false; }
  }
  save.addEventListener('click', savePreference);
  return { load, savePreference };
}
