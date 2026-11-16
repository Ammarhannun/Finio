/** Compact statement actions for a narrow dashboard, using the existing buttons. */
export function mountDashboardActions() {
  const wrap = document.getElementById('dash-more');
  const toggle = document.getElementById('dash-more-toggle');
  const menu = document.getElementById('dash-actions-menu');
  if (!wrap || !toggle || !menu) return;

  const buttons = () => [...menu.querySelectorAll('button:not([hidden]):not(:disabled)')];
  const close = (restoreFocus = false) => {
    wrap.classList.remove('open');
    toggle.setAttribute('aria-expanded', 'false');
    if (restoreFocus && toggle.getClientRects().length) toggle.focus();
  };
  const open = () => {
    wrap.classList.add('open');
    toggle.setAttribute('aria-expanded', 'true');
    menu.classList.remove('above');
    const bounds = menu.getBoundingClientRect();
    const anchor = toggle.getBoundingClientRect();
    const footer = document.getElementById('mobile-navigation')?.getBoundingClientRect();
    const bottom = footer?.height ? Math.min(window.innerHeight, footer.top) : window.innerHeight;
    if (bounds.bottom > bottom - 8 && anchor.top > bounds.height + 8) menu.classList.add('above');
  };
  toggle.addEventListener('click', () => {
    if (wrap.classList.contains('open')) close(); else open();
  });
  wrap.addEventListener('keydown', event => {
    if (!toggle.getClientRects().length) return;
    if (event.key === 'Escape' && wrap.classList.contains('open')) {
      event.preventDefault(); event.stopPropagation(); close(true);
    }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault(); open();
      const items = buttons();
      if (!items.length) return;
      const current = items.indexOf(document.activeElement);
      const next = current < 0 ? (event.key === 'ArrowDown' ? 0 : items.length - 1)
        : (current + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      items[next].focus();
    }
  });
  menu.addEventListener('click', event => {
    // The original Undo/Re-analyse handlers retain their revision and error handling.
    if (event.target.closest('button')) close(true);
  });
  document.addEventListener('click', event => { if (!wrap.contains(event.target)) close(); });
  document.addEventListener('focusin', event => { if (!wrap.contains(event.target)) close(); });
  // A coach split can resize the dashboard independently of the screen.
  if (typeof ResizeObserver !== 'undefined') {
    new ResizeObserver(() => close()).observe(wrap.closest('.dash-controls'));
  } else window.addEventListener('resize', () => close());
}
