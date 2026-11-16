import { reauthenticatePassword, verifiedFactors, verifyAuthenticator } from './account-security.js';

// Secrets stay in this page's memory and are removed when setup ends.
export function mountAccountSecurity({ auth, getSession, apiFetch }) {
  const field = id => document.getElementById(id);
  const error = field('security-error');
  const dialog = field('security-confirm');
  const input = field('security-proof');
  const select = field('security-factor');
  let pendingFactor = null, factors = [], proofFactors = [], resolveProof = null;
  let proofBusy = false, checkingProof = false, setupBusy = false, active = true;
  const showError = err => { error.textContent = err.message; error.hidden = false; };
  function updateSetupControls() {
    field('security-enable').hidden = factors.length > 0 || pendingFactor !== null;
    field('security-enable').disabled = setupBusy;
    field('security-setup-submit').disabled = setupBusy;
    field('security-setup-cancel').disabled = setupBusy;
  }
  function clearSetup() {
    field('security-setup').hidden = true;
    field('security-qr').removeAttribute('src');
    field('security-secret').textContent = '';
    field('security-setup-code').value = '';
  }
  async function load() {
    const state = await apiFetch('/security/status');
    factors = await verifiedFactors(auth);
    field('security-status').textContent = factors.length
      ? 'Two-step verification is on.' : 'Add an authenticator app to protect your account.';
    updateSetupControls();
    field('security-factors').replaceChildren();
    for (const factor of factors) {
      const row = document.createElement('div'); row.className = 'security-factor-row';
      const label = document.createElement('span'); label.textContent = factor.friendly_name || 'Authenticator app';
      const remove = document.createElement('button');
      remove.type = 'button'; remove.className = 'btn btn-outline btn-sm'; remove.textContent = 'Remove';
      remove.addEventListener('click', async () => {
        remove.disabled = true; error.hidden = true;
        try {
          if (!(await confirmRecent())) return;
          const result = await auth.mfa.unenroll({ factorId: factor.id });
          if (result.error) throw new Error('Could not remove this authenticator. Please try again.');
          const refreshed = await auth.refreshSession();
          if (refreshed.error) throw new Error('Sign in again to refresh your account security.');
          await load();
        } catch (err) { showError(err); }
        finally { remove.disabled = false; }
      });
      row.append(label, remove); field('security-factors').append(row);
    }
    return state;
  }
  function closeProof(result) {
    input.value = ''; dialog.close();
    const done = resolveProof; resolveProof = null; done?.(result);
  }
  async function confirmRecent() {
    if (!active) return false;
    if (checkingProof || proofBusy || resolveProof) throw new Error('Finish the current verification first.');
    checkingProof = true;
    try {
      const state = await apiFetch('/security/status');
      if (!active) return false;
      if (state.session_active && state.mfa_satisfied && state.recent_auth) return true;
      proofFactors = await verifiedFactors(auth);
      if (!active) return false;
      select.replaceChildren();
      for (const factor of proofFactors) {
        const option = document.createElement('option'); option.value = factor.id;
        option.textContent = factor.friendly_name || 'Authenticator app'; select.append(option);
      }
      select.closest('.form-group').hidden = proofFactors.length < 2;
      field('security-proof-label').textContent = proofFactors.length ? 'Authenticator code' : 'Current password';
      input.type = proofFactors.length ? 'text' : 'password';
      input.autocomplete = proofFactors.length ? 'one-time-code' : 'current-password';
      input.inputMode = proofFactors.length ? 'numeric' : 'text';
      input.removeAttribute('maxlength');
      if (proofFactors.length) input.maxLength = 6;
      input.value = ''; field('security-confirm-error').hidden = true;
      dialog.showModal(); input.focus();
      return await new Promise(resolve => { resolveProof = resolve; });
    } finally { checkingProof = false; }
  }
  field('security-proof-form').addEventListener('submit', async event => {
    event.preventDefault(); if (proofBusy || !resolveProof || !active) return;
    proofBusy = true; field('security-proof-submit').disabled = true;
    field('security-confirm-error').hidden = true;
    try {
      if (proofFactors.length) await verifyAuthenticator(auth, select.value, input.value);
      else await reauthenticatePassword(auth, await getSession(), input.value);
      if (!active || !resolveProof) return;
      const state = await apiFetch('/security/status');
      if (!active || !resolveProof) return;
      if (!state.session_active || !state.mfa_satisfied || !state.recent_auth)
        throw new Error('Could not confirm a fresh sign-in. Please sign in again.');
      closeProof(true);
    } catch (err) {
      if (!active || !resolveProof) return;
      field('security-confirm-error').textContent = err.message;
      field('security-confirm-error').hidden = false;
    } finally { input.value = ''; proofBusy = false; field('security-proof-submit').disabled = false; }
  });
  field('security-proof-cancel').addEventListener('click', () => closeProof(false));
  dialog.addEventListener('cancel', event => { event.preventDefault(); closeProof(false); });
  field('security-enable').addEventListener('click', async event => {
    if (pendingFactor || setupBusy || !active) return;
    setupBusy = true; updateSetupControls(); error.hidden = true;
    try {
      if (!(await confirmRecent())) return;
      const { data, error: failure } = await auth.mfa.enroll({ factorType: 'totp', friendlyName: 'Finio authenticator' });
      if (failure) throw new Error('Could not start authenticator setup. Please try again.');
      if (!active) {
        await auth.mfa.unenroll({factorId:data.id});
        return;
      }
      pendingFactor = data.id;
      updateSetupControls();
      const qr = String(data.totp.qr_code);
      if (qr.startsWith('data:image/svg+xml')) field('security-qr').src = qr;
      else if (/^<svg[\s>]/.test(qr)) field('security-qr').src = 'data:image/svg+xml,' + encodeURIComponent(qr);
      else throw new Error('Could not display the authenticator setup code.');
      field('security-secret').textContent = data.totp.secret;
      field('security-setup').hidden = false;
      field('security-setup-code').focus();
    } catch (err) {
      const factorId = pendingFactor; pendingFactor = null; clearSetup();
      if (factorId) {
        try { await auth.mfa.unenroll({factorId}); } catch (_) { /* Unverified factor cannot grant access. */ }
      }
      showError(err);
    }
    finally { setupBusy = false; updateSetupControls(); }
  });
  field('security-setup-form').addEventListener('submit', async event => {
    event.preventDefault(); if (!pendingFactor || setupBusy || !active) return;
    const factorId = pendingFactor;
    setupBusy = true; updateSetupControls(); error.hidden = true;
    try {
      await verifyAuthenticator(auth, factorId, field('security-setup-code').value);
      if (!active) return;
      // Verification succeeded even if the following settings refresh fails.
      factors = [...factors, {id:factorId, status:'verified'}];
      pendingFactor = null; clearSetup(); updateSetupControls(); await load();
    } catch (err) { showError(err); }
    finally { field('security-setup-code').value = ''; setupBusy = false; updateSetupControls(); }
  });
  field('security-setup-cancel').addEventListener('click', async () => {
    if (setupBusy || !active) return;
    setupBusy = true;
    const factorId = pendingFactor; pendingFactor = null; clearSetup();
    updateSetupControls();
    try {
      if (factorId) {
        const result = await auth.mfa.unenroll({ factorId });
        if (result.error) throw result.error;
      }
    } catch (_) { showError(new Error('Setup was closed. An unverified setup may remain on your account.')); }
    finally { setupBusy = false; updateSetupControls(); }
  });
  window.addEventListener('pagehide', () => {
    active = false; closeProof(false); clearSetup(); pendingFactor = null;
  });
  return { confirmRecent, init: () => load().catch(showError) };
}
