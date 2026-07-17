// Floating AI-coach widget, mounted on every page.
// - Bottom-right bubble → slide-up chat panel → expand to full screen.
// - Shares the same /coach history as the AI Coach page.
// - Sends the current page + platform period so answers fit what's on screen.
// - Renders the coach's proposed transaction edits as cards with an Apply
//   button (human-confirmed writes through POST /overrides).

import { apiFetch, escapeHtml, showToast, storedPeriodParts } from './api.js';

let mounted = false;
let historyLoaded = false;

function fmtMsg(text) {
  // Escape first (XSS), then allow minimal emphasis the LLM may emit.
  let t = escapeHtml(text)
    .replace(/^#+\s*/gm, '')                 // stray markdown headings
    .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
    .replace(/\n/g, '<br>');
  return t;
}

export function mountCoachWidget(page) {
  if (mounted || document.getElementById('coach-fab')) return;
  mounted = true;

  const root = document.createElement('div');
  root.id = 'coach-widget';
  root.innerHTML = `
    <button id="coach-fab" aria-label="Open AI coach" title="Ask your AI coach">✦</button>
    <div id="coach-panel" role="dialog" aria-label="AI coach">
      <div class="cw-head">
        <span class="cw-title">✦ AI Coach</span>
        <div class="cw-actions">
          <button id="cw-expand" class="cw-btn" aria-label="Toggle full screen" title="Full screen">⤢</button>
          <button id="cw-close" class="cw-btn" aria-label="Close" title="Close">×</button>
        </div>
      </div>
      <div id="cw-messages" class="cw-messages">
        <div class="cw-msg assistant">Hey! Ask me anything about your money — or about this page.</div>
      </div>
      <form id="cw-form" class="cw-form">
        <input id="cw-input" type="text" placeholder="Ask your coach…" maxlength="2000" autocomplete="off">
        <button type="submit" class="cw-send" aria-label="Send">→</button>
      </form>
    </div>`;
  document.body.appendChild(root);

  const panel = document.getElementById('coach-panel');
  const messagesEl = document.getElementById('cw-messages');
  const input = document.getElementById('cw-input');

  const scroll = () => { messagesEl.scrollTop = messagesEl.scrollHeight; };

  function addMsg(role, html) {
    const div = document.createElement('div');
    div.className = `cw-msg ${role}`;
    div.innerHTML = html;
    messagesEl.appendChild(div);
    scroll();
    return div;
  }

  // The coach proposed a reclassification → show it as a confirm card.
  function addProposal(p) {
    const what = [
      p.category ? `category → <b>${escapeHtml(p.category)}</b>` : '',
      p.flow ? `counted as <b>${escapeHtml(p.flow)}</b>` : '',
    ].filter(Boolean).join(', ');
    const card = document.createElement('div');
    card.className = 'cw-proposal';
    card.innerHTML = `
      <div class="cw-prop-text">Change transactions matching
        “<b>${escapeHtml(p.match)}</b>” — ${what}
        <span class="cw-prop-count">(${p.affected_count} match${p.affected_count === 1 ? '' : 'es'} this period)</span>
      </div>
      <button class="cw-apply">Apply</button>`;
    card.querySelector('.cw-apply').addEventListener('click', async (e) => {
      const btn = e.target;
      btn.disabled = true; btn.textContent = 'Applying…';
      try {
        const existing = await apiFetch('/overrides').catch(() => ({ overrides: [], custom_categories: [] }));
        const rules = (existing.overrides || []).filter(r =>
          !(r.match && r.match.toLowerCase() === p.match.toLowerCase()));
        const rule = { match: p.match };
        if (p.category) rule.category = p.category;
        if (p.flow) rule.flow = p.flow;
        rules.push(rule);
        await apiFetch('/overrides', { method: 'POST', body: JSON.stringify({
          rules, custom_categories: existing.custom_categories || [] }) });
        btn.textContent = 'Applied ✓';
        showToast('Updated your numbers');
        // Let the current page refresh itself (transactions page listens).
        window.dispatchEvent(new CustomEvent('finio:overrides-applied'));
      } catch (err) {
        btn.disabled = false; btn.textContent = 'Apply';
        showToast(err.message || 'Could not apply that change');
      }
    });
    messagesEl.appendChild(card);
    scroll();
  }

  async function loadHistory() {
    if (historyLoaded) return;
    historyLoaded = true;
    try {
      const res = await apiFetch('/coach/history');
      const rows = res.history || [];
      if (rows.length) {
        messagesEl.innerHTML = '';
        rows.slice(-12).forEach(r => addMsg(r.role === 'user' ? 'user' : 'assistant', fmtMsg(r.message)));
      }
    } catch (_) { /* not logged in / no data yet */ }
  }

  function setOpen(open) {
    panel.classList.toggle('open', open);
    document.getElementById('coach-fab').classList.toggle('hidden', open);
    if (open) { loadHistory(); setTimeout(() => input.focus(), 150); }
  }

  document.getElementById('coach-fab').addEventListener('click', () => setOpen(true));
  document.getElementById('cw-close').addEventListener('click', () => {
    panel.classList.remove('full');
    setOpen(false);
  });
  document.getElementById('cw-expand').addEventListener('click', () =>
    panel.classList.toggle('full'));
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && panel.classList.contains('open')) {
      panel.classList.remove('full');
      setOpen(false);
    }
  });

  document.getElementById('cw-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const msg = input.value.trim();
    if (!msg) return;
    input.value = '';
    addMsg('user', fmtMsg(msg));
    const typing = addMsg('assistant typing', '<span class="cw-dots"><span></span><span></span><span></span></span>');
    try {
      const body = { message: msg, page, ...storedPeriodParts() };
      const res = await apiFetch('/coach', { method: 'POST', body: JSON.stringify(body) });
      typing.remove();
      addMsg('assistant', fmtMsg(res.text || '—'));
      (res.proposed_actions || []).forEach(addProposal);
    } catch (err) {
      typing.remove();
      if (err.message === 'no_analysis' || err.status === 404) {
        addMsg('assistant', 'I don\'t have your data yet — upload a statement on the Dashboard first.');
      } else {
        addMsg('assistant', fmtMsg(err.message || 'Sorry, something went wrong.'));
      }
    }
  });
}
