import * as config from './config.js';

const form = document.getElementById('privacy-contact-form');
const fields = document.getElementById('privacy-contact-fields');
const status = document.getElementById('privacy-contact-status');
const send = document.getElementById('privacy-contact-send');
const api = (config.API_BASE_URL || '').replace(/\/$/, '');
let token = '';
let widget;
let sending = false;
let hasOutcome = false;

function say(message) { status.textContent = message; }
function challenge(value = '') {
  token = value;
  send.disabled = sending || !token;
}

form.addEventListener('input', () => {
  if (!sending && hasOutcome) {
    hasOutcome = false;
    say(token ? 'Ready to send.' : 'Complete the security check before sending.');
  }
});

async function request(path, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 20000);
  try {
    return await fetch(api + path, { ...options, signal: controller.signal,
      credentials: 'omit', redirect: 'error', referrerPolicy: 'no-referrer' });
  } finally { clearTimeout(timeout); }
}

// Attach the handler before enabling any field. A failed module load leaves
// the form disabled and cannot submit personal text to the static host.
form.addEventListener('submit', async event => {
  event.preventDefault();
  if (sending || !token || !form.reportValidity()) return;
  sending = true;
  fields.disabled = true;
  send.disabled = true;
  say('Sending your request...');
  try {
    const response = await request('/privacy/contact', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        email: form.elements.email.value.trim(),
        topic: form.elements.topic.value,
        message: form.elements.message.value.trim(),
        website: form.elements.website.value,
        token,
      }) });
    const result = await response.json().catch(() => ({}));
    if (!response.ok || result.received !== true || !/^[a-f0-9]{12}$/.test(result.reference || '')) {
      throw new Error(response.status === 429
        ? 'Too many requests. Please try again later.'
        : response.status === 400
          ? 'The security check failed or expired. Please try again.'
          : 'We could not confirm delivery. Your text is still here. Please try again later.');
    }
    form.reset();
    hasOutcome = true;
    say(`Request sent. Reference: ${result.reference}. We may need to verify your identity before acting on it.`);
  } catch (error) {
    hasOutcome = true;
    say(error.name === 'AbortError' || error instanceof TypeError
      ? 'We could not confirm delivery. Your text is still here. Check your connection and try again later.'
      : error.message);
  } finally {
    sending = false;
    fields.disabled = false;
    challenge();
    if (widget !== undefined) window.turnstile.reset(widget);
  }
});

async function start() {
  try {
    if (!api) throw new Error('unconfigured');
    const response = await request('/privacy/contact/config');
    const settings = response.ok ? await response.json() : {};
    if (settings.enabled !== true || !/^[A-Za-z0-9_-]{10,100}$/.test(settings.site_key || '')) {
      throw new Error('unconfigured');
    }
    const script = document.createElement('script');
    script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
    script.async = true;
    script.referrerPolicy = 'no-referrer';
    script.onerror = () => say('The security check could not load. Please refresh and try again.');
    script.onload = () => {
      try {
        widget = window.turnstile.render('#privacy-contact-challenge', {
          sitekey: settings.site_key, action: 'privacy-contact', size: 'flexible',
          callback: value => { challenge(value); if (!hasOutcome && !sending) say('Ready to send.'); },
          'expired-callback': () => { challenge(); if (!hasOutcome) say('The security check expired. Please complete it again.'); },
          'error-callback': () => { challenge(); if (!hasOutcome) say('The security check failed. Please refresh and try again.'); },
        });
        fields.disabled = false;
        say('Complete the security check before sending.');
      } catch { say('The security check could not load. Please refresh and try again.'); }
    };
    document.head.appendChild(script);
  } catch {
    say('This form is being set up. You can export or delete saved data in Profile.');
  }
}
start();
