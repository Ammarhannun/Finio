// Floating AI-coach widget, mounted on every page.
// - Bottom-right bubble → slide-up chat panel → expand to full screen.
// - MULTIPLE chats: a drawer lists your conversations; start a new one anytime.
// - Sends the current page + platform period so answers fit what's on screen.
// - Renders the coach's proposed transaction edits as cards with an Apply
//   button (human-confirmed writes through POST /overrides).

import { apiFetch, escapeHtml, showToast, storedPeriodParts } from './api.js';

let mounted = false;

const CHAT_KEY = 'finio-chat-id';
function currentChat() {
  try { return localStorage.getItem(CHAT_KEY) || 'default'; } catch (e) { return 'default'; }
}
function setCurrentChat(id) {
  try { localStorage.setItem(CHAT_KEY, id); } catch (e) { /* ignore */ }
}

function fmtMsg(text) {
  // Server strips markdown now, but old history may still carry it — escape
  // first (XSS), then drop any leftover markdown noise.
  let t = escapeHtml(text)
    .replace(/^#+\s*/gm, '')
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/(^|\s)\*([^*\n]+)\*(?=\s|$)/g, '$1$2')
    .replace(/\*\*/g, '').replace(/\n/g, '<br>');
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
        <div class="cw-head-left">
          <button id="cw-chats" class="cw-btn" aria-label="Your chats" title="Your chats">☰</button>
          <span class="cw-title">✦ AI Coach</span>
        </div>
        <div class="cw-actions">
          <button id="cw-new" class="cw-btn" aria-label="New chat" title="New chat">＋</button>
          <button id="cw-expand" class="cw-btn" aria-label="Toggle full screen" title="Full screen">⤢</button>
          <button id="cw-close" class="cw-btn" aria-label="Close" title="Close">×</button>
        </div>
      </div>
      <div id="cw-drawer" class="cw-drawer">
        <div class="cw-drawer-head">Your chats</div>
        <div id="cw-chat-list"></div>
      </div>
      <div id="cw-messages" class="cw-messages"></div>
      <form id="cw-form" class="cw-form">
        <input id="cw-input" type="text" placeholder="Ask your coach…" maxlength="2000" autocomplete="off">
        <button type="submit" class="cw-send" aria-label="Send">→</button>
      </form>
    </div>`;
  document.body.appendChild(root);

  const panel = document.getElementById('coach-panel');
  const drawer = document.getElementById('cw-drawer');
  const messagesEl = document.getElementById('cw-messages');
  const input = document.getElementById('cw-input');

  const scroll = () => { messagesEl.scrollTop = messagesEl.scrollHeight; };

  function greet() {
    messagesEl.innerHTML = '';
    addMsg('assistant', 'Hey! Ask me anything about your money — or about this page.');
  }

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
        <span class="cw-prop-count">(${p.affected_count} match${p.affected_count === 1 ? '' : 'es'})</span>
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
    try {
      const res = await apiFetch('/coach/history?chat_id=' + encodeURIComponent(currentChat()));
      const rows = res.history || [];
      if (rows.length) {
        messagesEl.innerHTML = '';
        rows.slice(-14).forEach(r => addMsg(r.role === 'user' ? 'user' : 'assistant', fmtMsg(r.message)));
      } else {
        greet();
      }
    } catch (_) { greet(); }
  }

  // ── Chats drawer ──
  async function loadChats() {
    const list = document.getElementById('cw-chat-list');
    list.innerHTML = '<div class="cw-chat-item muted">Loading…</div>';
    try {
      const res = await apiFetch('/chats');
      const chats = res.chats || [];
      if (!chats.length) { list.innerHTML = '<div class="cw-chat-item muted">No chats yet</div>'; return; }
      list.innerHTML = chats.map(c => `
        <button class="cw-chat-item${c.chat_id === currentChat() ? ' active' : ''}" data-id="${escapeHtml(c.chat_id)}">
          <span class="cw-chat-title">${escapeHtml(c.title)}</span>
          <span class="cw-chat-count">${c.count}</span>
        </button>`).join('');
      list.querySelectorAll('.cw-chat-item[data-id]').forEach(b =>
        b.addEventListener('click', () => {
          setCurrentChat(b.dataset.id);
          drawer.classList.remove('open');
          loadHistory();
        }));
    } catch (_) {
      list.innerHTML = '<div class="cw-chat-item muted">Could not load chats</div>';
    }
  }

  document.getElementById('cw-chats').addEventListener('click', () => {
    const open = drawer.classList.toggle('open');
    if (open) loadChats();
  });

  document.getElementById('cw-new').addEventListener('click', () => {
    setCurrentChat('c' + Date.now().toString(36));
    drawer.classList.remove('open');
    greet();
    input.focus();
  });

  function setOpen(open) {
    panel.classList.toggle('open', open);
    document.getElementById('coach-fab').classList.toggle('hidden', open);
    if (open) { loadHistory(); setTimeout(() => input.focus(), 150); }
  }

  document.getElementById('coach-fab').addEventListener('click', () => setOpen(true));
  document.getElementById('cw-close').addEventListener('click', () => {
    panel.classList.remove('full');
    drawer.classList.remove('open');
    setOpen(false);
  });
  document.getElementById('cw-expand').addEventListener('click', () =>
    panel.classList.toggle('full'));
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && panel.classList.contains('open')) {
      panel.classList.remove('full');
      drawer.classList.remove('open');
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
      const body = { message: msg, page, chat_id: currentChat(), ...storedPeriodParts() };
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
