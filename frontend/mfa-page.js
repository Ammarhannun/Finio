import { supabase, getSession, initThemeToggle } from './api.js';
import { mfaState, safeReturnPage, verifiedFactors, verifyAuthenticator } from './account-security.js';

const auth = supabase.auth;
try { document.documentElement.setAttribute('data-theme', localStorage.getItem('finio-theme') || 'dark'); }
catch (_) { document.documentElement.setAttribute('data-theme', 'dark'); }
initThemeToggle();
const errorEl = document.getElementById('mfa-error');
const button = document.getElementById('mfa-submit');
const destination = safeReturnPage(new URLSearchParams(location.search).get('next'));
function showError(error) { errorEl.textContent = error.message; errorEl.hidden = false; }
async function init() {
  if (!(await getSession())) { location.replace('login.html'); return; }
  if (!(await mfaState(auth)).required) { location.replace(destination); return; }
  const factors = await verifiedFactors(auth);
  if (!factors.length) throw new Error('No supported authenticator was found. Please use your existing recovery process.');
  const select = document.getElementById('mfa-factor');
  for (const factor of factors) {
    const option = document.createElement('option');
    option.value = factor.id;
    option.textContent = factor.friendly_name || 'Authenticator app';
    select.append(option);
  }
  select.closest('.form-group').hidden = factors.length === 1;
  button.disabled = false;
  document.getElementById('mfa-code').focus();
}
document.getElementById('mfa-form').addEventListener('submit', async event => {
  event.preventDefault(); button.disabled = true; errorEl.hidden = true;
  const input = document.getElementById('mfa-code');
  try {
    await verifyAuthenticator(auth, document.getElementById('mfa-factor').value, input.value);
    location.replace(destination);
  } catch (error) { showError(error); }
  finally { input.value = ''; button.disabled = false; }
});
document.getElementById('mfa-signout').addEventListener('click', async () => {
  await auth.signOut(); location.replace('login.html');
});
init().catch(showError);
