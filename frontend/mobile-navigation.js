// Screen-wide navigation is independent of the narrower desktop coach pane.
const PHONE_NAV_WIDTH = 700;
const icons = {
  dashboard: '<path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1z"/>',
  transactions: '<path d="M4 7h15m-4-4 4 4-4 4M20 17H5m4-4-4 4 4 4"/>',
  patterns: '<path d="M4 20V10m8 10V4m8 16v-7"/>',
  spend: '<path d="M20 11v9a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-9m-1 0 2-6h14l2 6zM9 5V3h6v2m-6 6v2m6-2v2m-7 4 3 3 5-5"/>',
  coach: '<path d="m12 2 2.8 7.2L22 12l-7.2 2.8L12 22l-2.8-7.2L2 12l7.2-2.8z"/>',
};
const svg = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${icons[name]}</svg>`;
const routes = [
  ['dashboard', 'Dashboard', 'dashboard.html'],
  ['transactions', 'Transactions', 'transactions.html'],
  ['patterns', 'Patterns', 'patterns.html'],
  ['spend', 'Spend Check', 'spend-check.html'],
];

export function mountMobileNavigation(activePage) {
  if (document.getElementById('mobile-navigation')) return;
  const shell = document.createElement('div');
  shell.className = 'mobile-nav-shell';
  const links = routes.map(([id, label, href]) => `<a class="mobile-nav-link${activePage === id ? ' active' : ''}" href="${href}"${activePage === id ? ' aria-current="page"' : ''}>${svg(id)}<span>${label}</span></a>`);
  const coach = `<button type="button" id="mobile-coach" class="mobile-coach" aria-label="Open AI coach" aria-expanded="false" aria-controls="coach-panel"><span class="mobile-coach-disc">${svg('coach')}</span></button>`;
  shell.innerHTML = `<div id="mobile-navigation" class="mobile-nav" role="navigation" aria-label="Main navigation">${links.slice(0, 2).join('')}${coach}${links.slice(2).join('')}</div>`;
  document.body.appendChild(shell);
  shell.querySelector('#mobile-coach').addEventListener('click', () => window.dispatchEvent(new Event('finio:toggle-coach')));
  shell.querySelectorAll('a').forEach(link => link.addEventListener('click', event => {
    if (event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) {
      window.dispatchEvent(new Event('finio:close-coach'));
    }
  }));

  let phone = null;
  function updateWidth(width) {
    const next = width <= PHONE_NAV_WIDTH;
    if (next === phone) return;
    phone = next;
    document.body.classList.toggle('phone-navigation', next);
    updateMobileCoachState(Boolean(document.getElementById('coach-panel')?.classList.contains('open')));
    if (next) {
      document.querySelector('.nav-links')?.classList.remove('open');
      document.getElementById('nav-hamburger')?.setAttribute('aria-expanded', 'false');
    }
    window.dispatchEvent(new CustomEvent('finio:phone-navigation', {detail:{active:next}}));
    updateViewport();
  }
  function updateViewport() {
    const viewport = window.visualViewport;
    const height = viewport?.height || window.innerHeight;
    document.body.style.setProperty('--phone-visible-height', `${height}px`);
    document.body.style.setProperty('--phone-visible-top', `${viewport?.offsetTop || 0}px`);
    const editing = document.activeElement?.matches('input, textarea, [contenteditable="true"]');
    const keyboard = Boolean(phone && editing && window.innerHeight - height > 150);
    document.body.classList.toggle('phone-keyboard-open', keyboard);
  }
  // Measure the screen-wide shell, so a desktop split does not get phone tabs.
  updateWidth(shell.getBoundingClientRect().width);
  if (typeof ResizeObserver !== 'undefined') {
    new ResizeObserver(entries => updateWidth(entries[0].contentRect.width)).observe(shell);
  } else {
    window.addEventListener('resize', () => updateWidth(shell.getBoundingClientRect().width));
  }
  window.visualViewport?.addEventListener('resize', updateViewport);
  window.visualViewport?.addEventListener('scroll', updateViewport);
  window.addEventListener('resize', updateViewport);
  document.addEventListener('focusin', updateViewport);
  document.addEventListener('focusout', () => requestAnimationFrame(updateViewport));
}

export function updateMobileCoachState(open) {
  const button = document.getElementById('mobile-coach');
  if (!button) return;
  button.setAttribute('aria-expanded', String(open));
  button.setAttribute('aria-label', open ? 'Close AI coach' : 'Open AI coach');
  button.classList.toggle('active', open);
  const covered = open && document.body.classList.contains('phone-navigation');
  document.querySelectorAll('body > main, body > nav').forEach(element => { element.inert = covered; });
}
