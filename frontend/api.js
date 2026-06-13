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
    window.location.href = 'index.html';
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

  const res = await fetch(`${API_BASE}${path}`, { ...options, headers });

  if (res.status === 401) {
    await supabase.auth.signOut();
    window.location.href = 'index.html';
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
  // Show the icon for the mode you'd switch TO.
  if (btn) btn.textContent = theme === 'light' ? '🌙' : '☀️';
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

export function setupNav(activePage) {
  initThemeToggle();

  document.querySelectorAll('.nav-links a').forEach(a => {
    if (a.dataset.page === activePage) a.classList.add('active');
  });

  const logoutBtn = document.getElementById('nav-logout');
  if (logoutBtn) {
    logoutBtn.addEventListener('click', async () => {
      await supabase.auth.signOut();
      window.location.href = 'index.html';
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

export function formatAUD(amount) {
  if (amount == null || isNaN(Number(amount))) return '—';
  return new Intl.NumberFormat('en-AU', {
    style: 'currency',
    currency: 'AUD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(amount);
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
