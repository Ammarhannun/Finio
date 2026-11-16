import { mountMobileNavigation } from './mobile-navigation.js';
import { publishSavedChange } from './page-refresh.js';
import { mountSessionPrivacy } from './session-privacy.js';
import { mfaState, safeReturnPage } from './account-security.js';
import { createClient } from 'https://esm.sh/@supabase/supabase-js@2.117.2';
import * as cfg from './config.js';

// config.js is a file the USER edits (it holds their Supabase keys), so this
// module must never hard-require a key that an older copy might not have.
// A named import of a missing export is a fatal module error. api.js would
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
const API_BASE = API_BASE_URL.replace(/\/$/, '')
  || (['localhost', '127.0.0.1', '[::1]', ''].includes(location.hostname)
        ? 'http://127.0.0.1:8000'
        : '');

export async function getSession() {
  const { data: { session } } = await supabase.auth.getSession();
  return session;
}

let protectedAccount = null;
export async function requireAuth() {
  const session = await getSession();
  if (!session) {
    window.location.href = 'login.html';
    throw new Error('Not authenticated');
  }
  if ((await mfaState(supabase.auth)).required) {
    document.body.replaceChildren();
    const next = safeReturnPage(location.pathname.split('/').pop());
    window.location.replace('mfa.html?next=' + encodeURIComponent(next));
    throw new Error('mfa_required');
  }
  if (protectedAccount === null) {
    protectedAccount = session.user.id;
    mountSessionPrivacy({
      userId: protectedAccount,
      subscribe: callback => supabase.auth.onAuthStateChange(callback),
      target: window,
      invalidate: () => {
        document.body.replaceChildren();
        window.location.replace('login.html');
      },
      restore: () => {
        // Reload a back-forward cached document through authentication before
        // reusing its previous account's in-memory financial view.
        document.body.replaceChildren();
        window.location.reload();
      },
    });
  }
  return session;
}

export async function apiFetch(path, options = {}) {
  if (!API_BASE) throw new Error('The service is being configured. Please try again later.');
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
    // blocked. turn that into a message a human can act on.
    throw new Error('Could not connect to Finio. Check your connection and try again.');
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
    const detail = Array.isArray(body.detail)
      ? body.detail.map(e => `${(e.loc || []).slice(1).join(' ')}: ${e.msg}`).join('; ')
      : typeof body.detail === 'object' ? body.detail?.message : body.detail;
    const error = new Error(detail || `Something went wrong (${res.status}). Please try again.`);
    error.code = body.detail?.code;
    error.status = res.status;
    throw error;
  }

  const data = await res.json();
  publishSavedChange(path, options.method, session?.user?.id);
  return data;
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
  mountMobileNavigation(activePage);

  document.querySelectorAll('.nav-links a').forEach(a => {
    if (a.dataset.page === activePage) { a.classList.add('active'); a.setAttribute('aria-current', 'page'); }
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
      <button class="nav-avatar" id="nav-avatar" aria-haspopup="menu" aria-controls="nav-menu" aria-expanded="false" title="Account">${escapeHtml(initial)}</button>
      <div class="nav-menu" id="nav-menu" role="menu" hidden>
        <div class="nav-menu-email">${escapeHtml(email)}</div>
        <a href="profile.html" role="menuitem">Profile &amp; settings</a>
        <button class="nav-menu-item" id="nav-logout-item" role="menuitem">Log out</button>
      </div>`;
    logoutBtn.replaceWith(wrap);

    const avatar = wrap.querySelector('#nav-avatar');
    const menu = wrap.querySelector('#nav-menu');
    // A body-level menu escapes the split navigation's scrolling/clipping box.
    document.body.append(menu);
    const close = () => {
      menu.hidden = true; wrap.classList.remove('open'); avatar.setAttribute('aria-expanded', 'false');
    };
    const position = () => {
      if (menu.hidden) return;
      const rect = avatar.getBoundingClientRect();
      const width = menu.getBoundingClientRect().width;
      const height = menu.getBoundingClientRect().height;
      menu.style.left = Math.max(8, Math.min(innerWidth - width - 8, rect.right - width)) + 'px';
      menu.style.top = (rect.bottom + height + 8 <= innerHeight ? rect.bottom + 8 : Math.max(8, rect.top - height - 8)) + 'px';
    };
    const openMenu = () => {
      menu.hidden = false; wrap.classList.add('open'); avatar.setAttribute('aria-expanded', 'true'); position();
    };
    avatar.addEventListener('click', (e) => {
      e.stopPropagation();
      if (menu.hidden) openMenu(); else close();
    });
    avatar.addEventListener('keydown', e => {
      if (e.key === 'Escape' && !menu.hidden) { e.preventDefault(); e.stopPropagation(); close(); }
      if (e.key === 'ArrowDown') { e.preventDefault(); openMenu(); menu.querySelector('[role="menuitem"]').focus(); }
    });
    menu.addEventListener('keydown', e => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); avatar.focus(); }
      const items = [...menu.querySelectorAll('[role="menuitem"]')];
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault(); const direction = e.key === 'ArrowDown' ? 1 : -1;
        items[(items.indexOf(document.activeElement) + direction + items.length) % items.length].focus();
      }
    });
    window.addEventListener('resize', position);
    window.addEventListener('scroll', position, true);
    if (typeof ResizeObserver !== 'undefined') new ResizeObserver(position).observe(wrap.closest('nav'));
    document.addEventListener('click', (e) => {
      if (!wrap.contains(e.target) && !menu.contains(e.target)) close();
    });
    menu.querySelector('#nav-logout-item').addEventListener('click', async () => {
      await supabase.auth.signOut();
      window.location.href = 'login.html';
    });
  }

  const hamburger = document.getElementById('nav-hamburger');
  const navLinks = document.querySelector('.nav-links');
  if (hamburger && navLinks) {
    hamburger.setAttribute('aria-expanded', 'false');
    hamburger.addEventListener('click', () => {
      const open = navLinks.classList.toggle('open');
      hamburger.setAttribute('aria-expanded', String(open));
    });
    document.addEventListener('click', (e) => {
      if (!hamburger.contains(e.target) && !navLinks.contains(e.target)) {
        navLinks.classList.remove('open');
        hamburger.setAttribute('aria-expanded', 'false');
      }
    });
  }
}

// Escape text before putting it inside innerHTML. Bank/merchant descriptions
// are untrusted input. without this a transaction named "<img onerror=…>"
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
/**
 * Render the coach's light markdown as safe HTML.
 *
 * The model is asked to keep **bold** and the backend deliberately preserves
 * those markers, so anything printing the text with textContent shows the
 * asterisks raw. Escaping happens FIRST, so merchant names and model output
 * can never inject markup.
 */
export function cleanPunctuation(text) {
  return String(text ?? '').replace(/[\u2014\u2013]|(?<!-)--(?!-)/g, '. ');
}

export function renderMarkdown(text) {
  return escapeHtml(cleanPunctuation(text))
    .replace(/^#+\s*/gm, '')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|\s)\*([^*\n]+)\*(?=\s|$)/g, '$1$2')
    .replace(/\*\*/g, '')
    .replace(/\n/g, '<br>');
}

export function formatMoney(amount) {
  if (amount == null || isNaN(Number(amount))) return 'Unavailable';
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

// Show exact coverage so a partial statement cannot masquerade as a full month.
export function dateRangeLabel(dr) {
  if (!dr?.start) return '';
  const format = value => {
    const m = String(value || '').match(/^(\d{4})-(\d{2})-(\d{2})/);
    return m ? `${Number(m[3])} ${MONTHS_SHORT[Number(m[2]) - 1]} ${m[1]}` : String(value || '');
  };
  const start = format(dr.start);
  const end = format(dr.end);
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

/** Every page uses actual totals for the same selected statement window. */
export function mountPeriodBar(container, months, onChange,
                               current = getStoredPeriod(), tag = 'Window') {
  if (!container) return;
  const known = (months || []).map(m => typeof m === 'string' ? m : m.month).filter(m => /^\d{4}-\d{2}$/.test(m));
  container.innerHTML = `
    <div class="period-bar">
      <span class="period-tag">${escapeHtml(tag)}</span>
      <select aria-label="Statement period">
        ${SEGMENTS.map(p => `<option value="${p.value}"${p.value === current ? ' selected' : ''}>${p.value === 'all' ? 'All history' : 'Latest ' + p.label.toLowerCase()}</option>`).join('')}
        ${known.length ? `<optgroup label="Choose a statement month">${known.map(m => `<option value="month:${m}"${current === 'month:' + m ? ' selected' : ''}>${escapeHtml(formatMonthYear(m + '-01'))}</option>`).join('')}</optgroup>` : ''}
      </select>
    </div>`;
  const change = value => {
    setStoredPeriod(value);
    mountPeriodBar(container, months, onChange, value, tag);
    onChange(value);
  };
  container.querySelector('select')?.addEventListener('change', e => { if (e.target.value) change(e.target.value); });
}

/** Undo uses the revision returned by the last read/save; stale undo is rejected. */
export function mountUndo(button, state, reload) {
  if (!button) return;
  button.hidden = !state?.can_undo;
  button.textContent = 'Undo ' + (state?.undo_label || 'last edit');
  button.onclick = async () => {
    button.disabled = true;
    try {
      await apiFetch('/changes/undo', { method: 'POST', body: JSON.stringify({ revision: state.revision }) });
      await reload();
      window.dispatchEvent(new CustomEvent('finio:data-changed'));
      showToast('Previous values restored');
    } catch (err) { showToast(err.message || 'Could not undo this edit'); }
    finally { button.disabled = false; }
  };
}

export function showEditingStatus(container, state) {
  let notice = container.querySelector('[data-editing-status]');
  if (state?.editing_available !== false) { notice?.remove(); return; }
  if (!notice) {
    notice = document.createElement('p');
    notice.dataset.editingStatus = '';
    notice.className = 'service-status';
    notice.setAttribute('role', 'status');
    container.prepend(notice);
  }
  notice.textContent = 'Your saved data is shown. Editing is temporarily unavailable while the service is updated.';
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
 * Every one of these pages did the same four things. auth, fetch for the
 * stored period, render, mount the period bar. plus an identical error ladder
 * distinguishing "no analysis yet" from a real failure. That block was copied
 * per page, so a fix to the error handling had to be made in each one.
 *
 * @param {string}   endpoint  API path, e.g. '/invest'
 * @param {Function} render    called with the response payload
 * @param {object}   opts      { loadingId, noDataId, mountId, tag }
 * @returns {Promise<Function>} loader: true on render, false on failure, null if superseded
 */
export async function bootstrapPeriodPage(endpoint, render, opts = {}) {
  const {
    loadingId = 'loading',
    noDataId = 'no-data',
    mountId = 'period-mount',
    tag = 'Window',
  } = opts;

  let requestVersion = 0;
  const commit = (data, value) => {
    const noData = document.getElementById(noDataId);
    if (noData) noData.style.display = 'none';
    render(data);
    mountPeriodBar(document.getElementById(mountId),
      data.available_months || [], load, value, tag);
  };
  const load = async (value, { quiet = false } = {}) => {
    const version = ++requestVersion;
    try {
      const data = await apiFetch(`${endpoint}?${periodQuery(value)}`, { cache: 'no-store' });
      if (version !== requestVersion) return null;
      commit(data, value);
      return true;
    } catch (err) {
      if (version !== requestVersion) return null;
      if (err.status === 404 || err.message === 'no_analysis') {
        // Another tab may have removed the saved data. Do not keep showing it.
        const content = document.getElementById('content');
        const noData = document.getElementById(noDataId);
        if (content) content.style.display = 'none';
        if (noData) noData.style.display = 'block';
        return false;
      }
      if (!quiet) showToast(err.message || 'Could not load that period');
      return false;
    }
  };

  try {
    await requireAuth();
    const value = getStoredPeriod();
    const data = await apiFetch(`${endpoint}?${periodQuery(value)}`, { cache: 'no-store' });
    commit(data, value);
  } catch (err) {
    const loadingEl = document.getElementById(loadingId);
    if (loadingEl) loadingEl.style.display = 'none';
    // A redirect to login is already in flight for these two. say nothing.
    const redirecting = err.message === 'Not authenticated'
                     || err.message === 'session_expired';
    if (!redirecting) {
      const noData = document.getElementById(noDataId);
      if (noData) {
        noData.style.display = 'block';
        if (err.message !== 'no_analysis' && err.status !== 404) {
          const message = document.createElement('p'); message.textContent = err.message;
          const retry = document.createElement('button'); retry.className = 'btn btn-outline'; retry.textContent = 'Retry loading';
          retry.onclick = () => location.reload();
          noData.replaceChildren(message, retry);
        }
      }
    }
  }
  return load;
}
