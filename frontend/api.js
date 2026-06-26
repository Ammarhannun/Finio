import { createClient } from 'https://esm.sh/@supabase/supabase-js@2';
import { SUPABASE_URL, SUPABASE_ANON_KEY } from './config.js';

export const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY);

const API_BASE = 'http://127.0.0.1:8000';

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
      <button class="nav-avatar" id="nav-avatar" aria-haspopup="true" aria-expanded="false" title="Account">${initial}</button>
      <div class="nav-menu" id="nav-menu" role="menu">
        <div class="nav-menu-email">${email}</div>
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

export function formatAUD(amount) {
  if (amount == null || isNaN(Number(amount))) return '—';
  return new Intl.NumberFormat('en-AU', {
    style: 'currency',
    currency: 'AUD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(amount);
}

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

// Human label for a date range, in Month Year only.
export function dateRangeLabel(dr) {
  if (!dr?.start) return '';
  const start = formatMonthYear(dr.start);
  const end = formatMonthYear(dr.end);
  return start === end ? start : `${start} to ${end}`;
}

// ── Shared period selector ──
// The same window control on every page so the whole platform moves together.
const BASE_PERIODS = [
  { value: 'monthly', label: 'Latest month' },
  { value: 'weekly', label: 'Latest week' },
  { value: 'daily', label: 'Latest day' },
  { value: 'all', label: 'All time' },
];

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

// Mount a period <select> into `container`. `onChange(value)` fires on change.
// Defaults to the platform-wide stored period and persists every change.
export function mountPeriodBar(container, months, onChange, current = getStoredPeriod()) {
  if (!container) return;
  const monthOpts = (months || []).slice().reverse()
    .map(m => `<option value="month:${m}"${`month:${m}` === current ? ' selected' : ''}>${formatMonthYear(m)}</option>`).join('');
  container.innerHTML = `
    <div class="period-bar">
      <span class="period-tag">Period</span>
      <select aria-label="Time period">
        ${BASE_PERIODS.map(p =>
          `<option value="${p.value}"${p.value === current ? ' selected' : ''}>${p.label}</option>`
        ).join('')}
        ${monthOpts ? `<optgroup label="Specific month">${monthOpts}</optgroup>` : ''}
      </select>
    </div>`;
  container.querySelector('select').addEventListener('change', (e) => {
    setStoredPeriod(e.target.value);   // platform-wide
    onChange(e.target.value);
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
