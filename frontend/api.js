import { createClient } from 'https://esm.sh/@supabase/supabase-js@2';
import * as cfg from './config.js';

// config.js is a file the USER edits (it holds their Supabase keys), so this
// module must never hard-require a key that an older copy might not have.
// A named import of a missing export is a fatal module error — api.js would
// fail to load and every page would silently lose its event handlers, so the
// login button would just do nothing. A namespace import degrades instead.
const SUPABASE_URL = cfg.SUPABASE_URL;
const SUPABASE_ANON_KEY = cfg.SUPABASE_ANON_KEY;
const API_BASE_URL = cfg.API_BASE_URL || '';
const CURRENCY = cfg.CURRENCY || 'AUD';
const LOCALE = cfg.LOCALE || 'en-AU';

export const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY);

// Local dev talks to the backend on :8000; anywhere else (a deployed frontend)
// must point at the deployed API, so the host is config, not a constant.
const API_BASE = API_BASE_URL
  || (['localhost', '127.0.0.1', '[::1]', ''].includes(location.hostname)
        ? 'http://127.0.0.1:8000'
        : `${location.origin}/api`);

export async function getSession() {
  const { data: { session } } = await supabase.auth.getSession();
  return session;
}

export async function requireAuth() {
  const session = await getSession();
  if (!session) {
    window.location.href = 'login.html';
    throw new Error('Not authenticated');
  }
  return session;
}

export async function apiFetch(path, options = {}) {
  const session = await getSession();
  const headers = { ...(options.headers || {}) };

  if (session?.access_token) {
    headers['Authorization'] = `Bearer ${session.access_token}`;
  }

  // Let browser set Content-Type for FormData (needs boundary param)
  if (!(options.body instanceof FormData) && options.body && !headers['Content-Type']) {
    headers['Content-Type'] = 'application/json';
  }

  let res;
  try {
    res = await fetch(`${API_BASE}${path}`, { ...options, headers });
  } catch (e) {
    // fetch() rejects with a TypeError when the server is unreachable / CORS
    // blocked — turn that into a message a human can act on.
    throw new Error('Could not reach the server. Is the backend running on :8000?');
  }

  if (res.status === 401) {
    await supabase.auth.signOut();
    window.location.href = 'login.html';
    throw new Error('session_expired');
  }

  if (res.status === 404) {
    const err = new Error('no_analysis');
    err.status = 404;
    throw err;
  }

  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Error ${res.status}`);
  }

  return res.json();
}

// ── Theme (light / dark) ──
export function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  try { localStorage.setItem('finio-theme', theme); } catch (e) { /* ignore */ }
  const btn = document.getElementById('theme-toggle');
  // Show the symbol for the mode you'd switch TO (monochrome dingbats, no emoji).
  if (btn) btn.textContent = theme === 'light' ? '☾' : '☼';
}

export function initThemeToggle() {
  const current = document.documentElement.getAttribute('data-theme') || 'dark';
  applyTheme(current);
  const btn = document.getElementById('theme-toggle');
  if (btn) {
    btn.addEventListener('click', () => {
      const next = document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
      applyTheme(next);
    });
  }
}

export async function setupNav(activePage) {
  initThemeToggle();

  document.querySelectorAll('.nav-links a').forEach(a => {
    if (a.dataset.page === activePage) a.classList.add('active');
  });

  // Top-right account menu (avatar → dropdown), like a normal web app. Built in
  // place of the old bare Logout button so every page gets it from one edit.
  const logoutBtn = document.getElementById('nav-logout');
  if (logoutBtn) {
    const session = await getSession();
    const email = session?.user?.email || '';
    const initial = (email.trim()[0] || 'U').toUpperCase();

    const wrap = document.createElement('div');
    wrap.className = 'nav-profile';
    if (activePage === 'profile') wrap.classList.add('active');
    wrap.innerHTML = `
      <button class="nav-avatar" id="nav-avatar" aria-haspopup="true" aria-expanded="false" title="Account">${escapeHtml(initial)}</button>
      <div class="nav-menu" id="nav-menu" role="menu">
        <div class="nav-menu-email">${escapeHtml(email)}</div>
        <a href="profile.html" role="menuitem">Profile</a>
        <button class="nav-menu-item" id="nav-logout-item" role="menuitem">Log out</button>
      </div>`;
    logoutBtn.replaceWith(wrap);

    const avatar = wrap.querySelector('#nav-avatar');
    avatar.addEventListener('click', (e) => {
      e.stopPropagation();
      const open = wrap.classList.toggle('open');
      avatar.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    document.addEventListener('click', (e) => {
      if (!wrap.contains(e.target)) wrap.classList.remove('open');
    });
    wrap.querySelector('#nav-logout-item').addEventListener('click', async () => {
      await supabase.auth.signOut();
      window.location.href = 'login.html';
    });
  }

  const hamburger = document.getElementById('nav-hamburger');
  const navLinks = document.querySelector('.nav-links');
  if (hamburger && navLinks) {
    hamburger.addEventListener('click', () => navLinks.classList.toggle('open'));
    document.addEventListener('click', (e) => {
      if (!hamburger.contains(e.target) && !navLinks.contains(e.target)) {
        navLinks.classList.remove('open');
      }
    });
  }
}

// Escape text before putting it inside innerHTML. Bank/merchant descriptions
// are untrusted input — without this a transaction named "<img onerror=…>"
// would execute. Use on EVERY dynamic value interpolated into innerHTML.
export function escapeHtml(value) {
  if (value == null) return '';
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// Currency and locale come from config so they're set in ONE place. The old
// name is kept as an alias because it's used across every page.
export function formatMoney(amount) {
  if (amount == null || isNaN(Number(amount))) return '—';
  return new Intl.NumberFormat(LOCALE, {
    style: 'currency',
    currency: CURRENCY,
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(amount);
}
export const formatAUD = formatMoney;

// ── Dates: always shown as "June 2026" for easy reading ──
const MONTHS = ['January','February','March','April','May','June',
  'July','August','September','October','November','December'];

export function formatMonthYear(value) {
  if (!value) return '';
  const s = String(value);
  // Accept "2026-06" or "2026-06-18" or a full ISO date.
  const m = s.match(/^(\d{4})-(\d{2})/);
  if (m) return `${MONTHS[parseInt(m[2], 10) - 1]} ${m[1]}`;
  const d = new Date(s);
  if (!isNaN(d)) return `${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
  return s;
}

// Row-level date: "2 Jan" (day matters when listing transactions).
const MONTHS_SHORT = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
export function formatDay(value) {
  if (!value) return '';
  const m = String(value).match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (m) return `${parseInt(m[3], 10)} ${MONTHS_SHORT[parseInt(m[2], 10) - 1]}`;
  const d = new Date(value);
  return isNaN(d) ? String(value) : `${d.getDate()} ${MONTHS_SHORT[d.getMonth()]}`;
}

// Human label for a date range, in Month Year only.
export function dateRangeLabel(dr) {
  if (!dr?.start) return '';
  const start = formatMonthYear(dr.start);
  const end = formatMonthYear(dr.end);
  return start === end ? start : `${start} to ${end}`;
}

// ── Shared period selector ──
// The selected period is platform-wide: one value persisted in localStorage so
// changing it on ANY page (dashboard, patterns, invest, coach, spend) carries
// to every other page. Format: 'monthly'|'weekly'|'daily'|'all'|'month:YYYY-MM'.
const PERIOD_KEY = 'finio-period';
export function getStoredPeriod() {
  try { return localStorage.getItem(PERIOD_KEY) || 'monthly'; } catch (e) { return 'monthly'; }
}
export function setStoredPeriod(value) {
  try { localStorage.setItem(PERIOD_KEY, value || 'monthly'); } catch (e) { /* ignore */ }
}
// Parse the stored value into the {period, month} shape some pages keep.
export function storedPeriodParts() {
  const v = getStoredPeriod();
  return v.startsWith('month:') ? { period: 'monthly', month: v.slice(6) } : { period: v, month: null };
}

// Turn a selector value into the query string the API expects.
export function periodQuery(value) {
  if (value && value.startsWith('month:')) {
    return `period=monthly&month=${encodeURIComponent(value.slice(6))}`;
  }
  return `period=${encodeURIComponent(value || 'monthly')}`;
}

// Short labels for the shared segmented control (same widget the dashboard
// uses, so the whole platform looks and behaves alike).
const SEGMENTS = [
  { value: 'daily', label: 'Day' },
  { value: 'weekly', label: 'Week' },
  { value: 'monthly', label: 'Month' },
  { value: 'all', label: 'All' },
];

/**
 * Mount the platform period control.
 *
 * The dashboard asks "what do I USUALLY spend per day/week/month", while
 * Patterns / Invest / Spend Check analyse ONE window of transactions. Same
 * control, same stored value — the `tag` spells out which question this page
 * is answering so the numbers are never ambiguous.
 */
export function mountPeriodBar(container, months, onChange,
                               current = getStoredPeriod(), tag = 'Window') {
  if (!container) return;
  container.innerHTML = `
    <div class="period-bar">
      <span class="period-tag">${tag}</span>
      <div class="segmented" role="group" aria-label="Time period">
        ${SEGMENTS.map(p =>
          `<button type="button" data-period="${p.value}"${p.value === current ? ' class="active"' : ''}>${p.label}</button>`
        ).join('')}
      </div>
    </div>`;
  container.querySelectorAll('.segmented button').forEach(btn => {
    btn.addEventListener('click', () => {
      const value = btn.dataset.period;
      container.querySelectorAll('.segmented button').forEach(b =>
        b.classList.toggle('active', b === btn));
      setStoredPeriod(value);   // platform-wide
      onChange(value);
    });
  });
}

export function showToast(msg) {
  const toast = document.getElementById('toast');
  if (!toast) return;
  toast.textContent = msg;
  toast.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => toast.classList.remove('show'), 3500);
}

export function severityClass(s) {
  const map = { high: 'red', medium: 'yellow', low: 'green', good: 'green', info: 'yellow' };
  return map[(s || '').toLowerCase()] || 'yellow';
}

/**
 * Boot a period-driven page (Invest, Patterns, …).
 *
 * Every one of these pages did the same four things — auth, fetch for the
 * stored period, render, mount the period bar — plus an identical error ladder
 * distinguishing "no analysis yet" from a real failure. That block was copied
 * per page, so a fix to the error handling had to be made in each one.
 *
 * @param {string}   endpoint  API path, e.g. '/invest'
 * @param {Function} render    called with the response payload
 * @param {object}   opts      { loadingId, noDataId, mountId, tag }
 * @returns {Function} the period-change handler, for callers that need it
 */
export async function bootstrapPeriodPage(endpoint, render, opts = {}) {
  const {
    loadingId = 'loading',
    noDataId = 'no-data',
    mountId = 'period-mount',
    tag = 'Window',
  } = opts;

  const load = async (value) => {
    try {
      render(await apiFetch(`${endpoint}?${periodQuery(value)}`));
    } catch (err) {
      showToast(err.message || 'Could not load that period');
    }
  };

  try {
    await requireAuth();
    const data = await apiFetch(`${endpoint}?${periodQuery(getStoredPeriod())}`);
    render(data);
    mountPeriodBar(document.getElementById(mountId),
      data.available_months || [], load, getStoredPeriod(), tag);
  } catch (err) {
    const loadingEl = document.getElementById(loadingId);
    if (loadingEl) loadingEl.style.display = 'none';
    // A redirect to login is already in flight for these two — say nothing.
    const redirecting = err.message === 'Not authenticated'
                     || err.message === 'session_expired';
    if (!redirecting) {
      const noData = document.getElementById(noDataId);
      if (noData) noData.style.display = 'block';
      if (err.message !== 'no_analysis' && err.status !== 404) showToast(err.message);
    }
  }
  return load;
}
