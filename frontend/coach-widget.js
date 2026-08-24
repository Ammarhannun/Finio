// Floating AI-coach widget, mounted on every page.
// - Bottom-right bubble → slide-up chat panel → split-screen with the current page.
// - Drag the divider to give the coach (or page) more room; drag fully for 100% coach.
// - MULTIPLE chats: a drawer lists your conversations; start a new one anytime.
// - Sends the current page + platform period so answers fit what's on screen.
// - Renders the coach's proposed transaction edits as cards with an Apply
//   button (human-confirmed writes through POST /overrides).

import { apiFetch, escapeHtml, formatAUD, renderMarkdown, showToast,
         storedPeriodParts } from './api.js';

let mounted = false;

const CHAT_KEY = 'finio-chat-id';
// The app is a set of separate HTML pages, so navigating destroys the widget.
// Remembering whether the coach was open (and in split mode) lets it come back
// exactly as it was, which makes it feel like one persistent assistant.
const OPEN_KEY = 'finio-coach-open';
const SPLIT_KEY = 'finio-coach-split-pct';
const FAB_POS_KEY = 'finio-coach-fab-pos';
const PANEL_POS_KEY = 'finio-coach-panel-pos';
const RAIL_KEY = 'finio-coach-rail-px';
const SPLIT_DEFAULT = 42;
const RAIL_DEFAULT = 180;
const RAIL_MIN = 128;
const RAIL_MAX = 340;

function currentChat() {
  try { return localStorage.getItem(CHAT_KEY) || 'default'; } catch (e) { return 'default'; }
}
function setCurrentChat(id) {
  try { localStorage.setItem(CHAT_KEY, id); } catch (e) { /* ignore */ }
}

function loadOpenState() {
  try { return localStorage.getItem(OPEN_KEY) || 'closed'; } catch (e) { return 'closed'; }
}
function saveOpenState(state) {
  try { localStorage.setItem(OPEN_KEY, state); } catch (e) { /* ignore */ }
}

// Floating-panel position, saved as a PERCENTAGE of the viewport so a window
// resize (or a different screen) keeps it roughly where the user put it
// instead of stranding it off-screen.
function loadPanelPos() {
  try {
    const raw = JSON.parse(localStorage.getItem(PANEL_POS_KEY) || 'null');
    if (raw && Number.isFinite(raw.x) && Number.isFinite(raw.y)) return raw;
  } catch (e) { /* ignore */ }
  return null;
}
function savePanelPos(pos) {
  try { localStorage.setItem(PANEL_POS_KEY, JSON.stringify(pos)); } catch (e) { /* ignore */ }
}
function clearPanelPos() {
  try { localStorage.removeItem(PANEL_POS_KEY); } catch (e) { /* ignore */ }
}

function loadSplitPct() {
  try {
    const n = Number(localStorage.getItem(SPLIT_KEY));
    if (Number.isFinite(n) && n >= 0 && n <= 100) return n;
  } catch (e) { /* ignore */ }
  return SPLIT_DEFAULT;
}
function saveSplitPct(pct) {
  try { localStorage.setItem(SPLIT_KEY, String(Math.round(pct))); } catch (e) { /* ignore */ }
}

function loadFabPos() {
  try {
    const raw = JSON.parse(localStorage.getItem(FAB_POS_KEY) || 'null');
    if (raw && Number.isFinite(raw.x) && Number.isFinite(raw.y)) return raw;
  } catch (e) { /* ignore */ }
  return null;
}
function saveFabPos(pos) {
  try { localStorage.setItem(FAB_POS_KEY, JSON.stringify(pos)); } catch (e) { /* ignore */ }
}

function loadRailPx() {
  try {
    const n = Number(localStorage.getItem(RAIL_KEY));
    if (Number.isFinite(n) && n >= RAIL_MIN && n <= RAIL_MAX) return n;
  } catch (e) { /* ignore */ }
  return RAIL_DEFAULT;
}
function saveRailPx(px) {
  try { localStorage.setItem(RAIL_KEY, String(Math.round(px))); } catch (e) { /* ignore */ }
}

function isNarrow() {
  return window.matchMedia('(max-width: 720px)').matches;
}

function clamp(n, lo, hi) {
  return Math.max(lo, Math.min(hi, n));
}

// Shared with the dashboard insight so there is ONE markdown renderer.
const fmtMsg = renderMarkdown;

export function mountCoachWidget(page) {
  if (mounted || document.getElementById('coach-fab')) return;
  mounted = true;

  const root = document.createElement('div');
  root.id = 'coach-widget';
  root.innerHTML = `
    <button id="coach-fab" aria-label="Open AI coach" title="Drag to move · click to open">✦</button>
    <div id="cw-splitter" class="cw-splitter" hidden aria-hidden="true" title="Drag to resize"></div>
    <div id="coach-panel" role="dialog" aria-label="AI coach">
      <div class="cw-head">
        <div class="cw-head-left">
          <button id="cw-chats" class="cw-btn" aria-label="Your chats" title="Your chats">☰</button>
          <span class="cw-title">✦ AI Coach</span>
        </div>
        <div class="cw-actions">
          <button id="cw-new" class="cw-btn" aria-label="New chat" title="New chat">＋</button>
          <button id="cw-expand" class="cw-btn" aria-label="Split screen with this page" title="Split screen">⤢</button>
          <button id="cw-close" class="cw-btn" aria-label="Close" title="Close">×</button>
        </div>
      </div>
      <div class="cw-body">
        <aside id="cw-drawer" class="cw-drawer" aria-label="Past chats">
          <div class="cw-drawer-head">Chats</div>
          <div id="cw-chat-list"></div>
        </aside>
        <div id="cw-rail-splitter" class="cw-rail-splitter" hidden title="Drag to resize chats"></div>
        <div class="cw-main">
          <div id="cw-messages" class="cw-messages"></div>
          <p class="cw-disclaimer">General information only, not financial advice</p>
      <form id="cw-form" class="cw-form">
            <input id="cw-input" type="text" placeholder="Ask your coach…" maxlength="2000" autocomplete="off">
            <button type="submit" class="cw-send" aria-label="Send">→</button>
          </form>
        </div>
      </div>
    </div>`;
  document.body.appendChild(root);

  const panel = document.getElementById('coach-panel');
  const drawer = document.getElementById('cw-drawer');
  const messagesEl = document.getElementById('cw-messages');
  const input = document.getElementById('cw-input');
  const splitter = document.getElementById('cw-splitter');
  const railSplitter = document.getElementById('cw-rail-splitter');
  const expandBtn = document.getElementById('cw-expand');
  const fab = document.getElementById('coach-fab');

  function applyRailWidth(px) {
    const w = clamp(px, RAIL_MIN, RAIL_MAX);
    panel.style.setProperty('--cw-rail', `${w}px`);
    return w;
  }
  applyRailWidth(loadRailPx());

  function setChatsOpen(open) {
    drawer.classList.toggle('open', open);
    railSplitter.hidden = !open;
    if (open) {
      applyRailWidth(loadRailPx());
      loadChats();
    }
  }

  // ── FAB position (drag anywhere) + which side split docks to ──
  function applyFabPos(pos) {
    if (!pos) {
      fab.style.left = '';
      fab.style.top = '';
      fab.style.right = '1.4rem';
      fab.style.bottom = '1.4rem';
      return;
    }
    fab.style.right = 'auto';
    fab.style.bottom = 'auto';
    fab.style.left = `calc(${pos.x}% - 27px)`;
    fab.style.top = `calc(${pos.y}% - 27px)`;
  }
  applyFabPos(loadFabPos());

  function preferredSide() {
    // Use the fab's centre (or last saved spot if it's hidden).
    let cx;
    if (!fab.classList.contains('hidden')) {
      const r = fab.getBoundingClientRect();
      cx = r.left + r.width / 2;
    } else {
      const pos = loadFabPos();
      cx = pos ? (pos.x / 100) * window.innerWidth : window.innerWidth;
    }
    return cx < window.innerWidth / 2 ? 'left' : 'right';
  }

  function applyDockSide(side) {
    document.body.dataset.coachSide = side;
    document.body.classList.toggle('coach-side-left', side === 'left');
    document.body.classList.toggle('coach-side-right', side === 'right');
  }

  function placeFloatingPanel() {
    if (panel.classList.contains('split') || panel.classList.contains('full')) {
      panel.style.left = '';
      panel.style.right = '';
      panel.style.top = '';
      panel.style.bottom = '';
      return;
    }
    // A position the user dragged to wins over the default docking.
    const dragged = loadPanelPos();
    if (dragged) {
      const w = panel.offsetWidth || 360;
      const h = panel.offsetHeight || 520;
      panel.style.right = 'auto';
      panel.style.bottom = 'auto';
      panel.style.left = `${Math.round(clamp(dragged.x / 100 * window.innerWidth, 8, window.innerWidth - w - 8))}px`;
      panel.style.top = `${Math.round(clamp(dragged.y / 100 * window.innerHeight, 8, window.innerHeight - h - 8))}px`;
      return;
    }
    const side = preferredSide();
    applyDockSide(side);
    const pos = loadFabPos();
    const fabBottom = pos
      ? ((100 - pos.y) / 100) * window.innerHeight - 27
      : 22;
    const bottom = clamp(fabBottom, 12, window.innerHeight - 120);
    panel.style.top = 'auto';
    panel.style.bottom = `${Math.round(bottom)}px`;
    if (side === 'left') {
      const left = pos ? Math.max(12, (pos.x / 100) * window.innerWidth - 27) : 22;
      panel.style.left = `${Math.round(clamp(left, 12, window.innerWidth - 280))}px`;
      panel.style.right = 'auto';
    } else {
      const right = pos
        ? Math.max(12, window.innerWidth - (pos.x / 100) * window.innerWidth - 27)
        : 22;
      panel.style.right = `${Math.round(clamp(right, 12, window.innerWidth - 280))}px`;
      panel.style.left = 'auto';
    }
  }

  // Drag the bubble around; a short press (no real move) still opens the chat.
  let fabDrag = null;
  fab.addEventListener('pointerdown', (e) => {
    if (e.button != null && e.button !== 0) return;
    fabDrag = { x0: e.clientX, y0: e.clientY, moved: false };
    fab.setPointerCapture?.(e.pointerId);
  });
  fab.addEventListener('pointermove', (e) => {
    if (!fabDrag) return;
    const dx = e.clientX - fabDrag.x0;
    const dy = e.clientY - fabDrag.y0;
    if (!fabDrag.moved && (Math.abs(dx) > 6 || Math.abs(dy) > 6)) {
      fabDrag.moved = true;
      fab.classList.add('dragging');
    }
    if (!fabDrag.moved) return;
    const x = clamp((e.clientX / window.innerWidth) * 100, 4, 96);
    const y = clamp((e.clientY / window.innerHeight) * 100, 4, 96);
    applyFabPos({ x, y });
  });
  function endFabDrag(e) {
    if (!fabDrag) return;
    const moved = fabDrag.moved;
    fab.classList.remove('dragging');
    if (moved) {
      const r = fab.getBoundingClientRect();
      const pos = {
        x: clamp(((r.left + r.width / 2) / window.innerWidth) * 100, 4, 96),
        y: clamp(((r.top + r.height / 2) / window.innerHeight) * 100, 4, 96),
      };
      saveFabPos(pos);
      applyFabPos(pos);
      applyDockSide(preferredSide());
    }
    fabDrag = null;
    if (!moved) setOpen(true);
  }
  fab.addEventListener('pointerup', endFabDrag);
  fab.addEventListener('pointercancel', () => {
    fab.classList.remove('dragging');
    fabDrag = null;
  });

  const scroll = () => { messagesEl.scrollTop = messagesEl.scrollHeight; };

  function sleep(ms) {
    return new Promise(r => setTimeout(r, ms));
  }

  async function copyText(text, btn) {
    try {
      await navigator.clipboard.writeText(text);
      if (btn) {
        // Flash a tick in place of the icon so there's feedback without the
        // button changing width.
        const prev = btn.innerHTML;
        btn.innerHTML = ICONS.check;
        btn.classList.add('copied');
        setTimeout(() => { btn.innerHTML = prev; btn.classList.remove('copied'); }, 1200);
      } else {
        showToast('Copied');
      }
    } catch (_) {
      showToast('Could not copy');
    }
  }

  function plainFromEl(el) {
    return el?.dataset?.plain || el?.querySelector('.cw-msg-body')?.innerText || '';
  }

  // Outlined icons (same family as the reference) rather than text labels, so
  // the actions sit quietly under the message.
  const ICONS = {
    copy: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
             stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
             <rect x="9" y="9" width="11" height="11" rx="2.5"/>
             <path d="M5 15H4.5A1.5 1.5 0 0 1 3 13.5v-9A1.5 1.5 0 0 1 4.5 3h9A1.5 1.5 0 0 1 15 4.5V5"/>
           </svg>`,
    check: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
              stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M4 12.5 9 17.5 20 6.5"/>
            </svg>`,
    edit: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
             stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
             <path d="M4 20h4L19.5 8.5a2.5 2.5 0 0 0-3.5-3.5L4.5 16.5 4 20Z"/>
           </svg>`,
  };

  function attachActions(turn, role, plain) {
    turn.dataset.plain = plain;
    const actions = document.createElement('div');
    actions.className = 'cw-msg-actions';
    const copyBtn = document.createElement('button');
    copyBtn.type = 'button';
    copyBtn.className = 'cw-act';
    copyBtn.innerHTML = ICONS.copy;
    copyBtn.title = 'Copy';
    copyBtn.setAttribute('aria-label', 'Copy message');
    copyBtn.addEventListener('click', () => copyText(plainFromEl(turn), copyBtn));
    actions.appendChild(copyBtn);
    if (role === 'user') {
      const editBtn = document.createElement('button');
      editBtn.type = 'button';
      editBtn.className = 'cw-act';
      editBtn.innerHTML = ICONS.edit;
      editBtn.title = 'Edit';
      editBtn.setAttribute('aria-label', 'Edit message');
      editBtn.addEventListener('click', () => startEdit(turn));
      actions.appendChild(editBtn);
    }
    turn.appendChild(actions);
  }

  function startEdit(turn) {
    // Claude-style: pull this user message into the input and drop everything after it.
    const plain = plainFromEl(turn);
    let node = turn.nextSibling;
    while (node) {
      const next = node.nextSibling;
      node.remove();
      node = next;
    }
    turn.remove();
    input.value = plain;
    input.focus();
    input.setSelectionRange(plain.length, plain.length);
  }

  function addMsg(role, html, plainText) {
    // First real message replaces the welcome screen.
    messagesEl.querySelector('.cw-empty')?.remove();
    const baseRole = role.split(/\s+/)[0];
    const turn = document.createElement('div');
    turn.className = `cw-turn ${baseRole}${role.includes('typing') ? ' typing' : ''}`;
    const wrap = document.createElement('div');
    wrap.className = `cw-msg ${role}`;
    const body = document.createElement('div');
    body.className = 'cw-msg-body';
    body.innerHTML = html;
    wrap.appendChild(body);
    turn.appendChild(wrap);
    messagesEl.appendChild(turn);
    const plain = plainText != null ? plainText : body.innerText;
    if (!role.includes('typing')) attachActions(turn, baseRole, plain);
    scroll();
    return turn;
  }

  async function typeAssistant(text) {
    const turn = document.createElement('div');
    turn.className = 'cw-turn assistant';
    const wrap = document.createElement('div');
    wrap.className = 'cw-msg assistant streaming';
    const body = document.createElement('div');
    body.className = 'cw-msg-body';
    wrap.appendChild(body);
    turn.appendChild(wrap);
    messagesEl.appendChild(turn);

    const plain = String(text || '');
    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduceMotion || plain.length < 2) {
      body.innerHTML = fmtMsg(plain);
      wrap.classList.remove('streaming');
      attachActions(turn, 'assistant', plain);
      scroll();
      return turn;
    }
    const step = plain.length > 400 ? 4 : plain.length > 180 ? 2 : 1;
    for (let i = 0; i < plain.length; i += step) {
      body.textContent = plain.slice(0, Math.min(i + step, plain.length));
      if (i % (step * 4) === 0) scroll();
      await sleep(step === 1 ? 12 : 10);
    }
    body.innerHTML = fmtMsg(plain);
    wrap.classList.remove('streaming');
    attachActions(turn, 'assistant', plain);
    scroll();
    return turn;
  }

  // Openers offered on an empty chat. Page-specific ones first so the coach
  // picks up whatever the user is looking at, then the evergreen ones.
  const STARTERS_BY_PAGE = {
    dashboard: ['Where is my money actually going?', 'Am I on track to save?'],
    transactions: ['What did I spend at Uber Eats?', 'Count transfers from family as income'],
    patterns: ['What habit is costing me most?', 'Which bills repeat every month?'],
    invest: ['What do I need before I can invest?', 'What is an ETF?'],
    spend: ['Can I afford a $900 laptop?', 'How much can I safely spend this week?'],
    profile: ['How much do I usually spend a month?', 'Am I on track to save?'],
  };
  const STARTERS_GENERAL = [
    'How much did I spend last month?',
    'What is my biggest category?',
    'Set a $200 takeaway budget',
  ];

  // A chat is never blank: an empty thread shows a welcome with one-tap
  // openers, the way a fresh conversation should look.
  function greet() {
    const starters = [...(STARTERS_BY_PAGE[page] || []), ...STARTERS_GENERAL].slice(0, 4);
    messagesEl.innerHTML = `
      <div class="cw-empty">
        <div class="cw-empty-mark" aria-hidden="true">✦</div>
        <h2 class="cw-empty-title">Start chatting with Finio</h2>
        <p class="cw-empty-sub">
          Ask about your spending, your goals, or anything money.
          I can also make changes for you — just say what you want.
        </p>
        <div class="cw-starters">
          ${starters.map(s =>
            `<button type="button" class="cw-starter">${escapeHtml(s)}</button>`).join('')}
        </div>
      </div>`;
    messagesEl.querySelectorAll('.cw-starter').forEach(b =>
      b.addEventListener('click', () => {
        input.value = b.textContent;
        document.getElementById('cw-form')
          .dispatchEvent(new Event('submit', { cancelable: true }));
      }));
  }

  // The coach proposed a reclassification → show it as a confirm card.
  // The coach can propose changes across the platform (reclassify transactions,
  // set a budget, move the savings goal, update the profile). Nothing is ever
  // written without the user clicking Apply — that confirmation IS the guardrail.
  const PROPOSALS = {
    reclassify: {
      describe: p => {
        const what = [
          p.category ? `category to <b>${escapeHtml(p.category)}</b>` : '',
          p.flow ? `counted as <b>${escapeHtml(p.flow)}</b>` : '',
        ].filter(Boolean).join(', ');
        const n = p.affected_count;
        return `Change transactions matching “<b>${escapeHtml(p.match)}</b>” — ${what}`
          + `<span class="cw-prop-count">${n} match${n === 1 ? '' : 'es'}</span>`;
      },
      apply: async (p) => {
        const existing = await apiFetch('/overrides')
          .catch(() => ({ overrides: [], custom_categories: [] }));
        const rules = (existing.overrides || []).filter(r =>
          !(r.match && r.match.toLowerCase() === p.match.toLowerCase()));
        const rule = { match: p.match };
        if (p.category) rule.category = p.category;
        if (p.flow) rule.flow = p.flow;
        rules.push(rule);
        await apiFetch('/overrides', { method: 'POST', body: JSON.stringify({
          rules, custom_categories: existing.custom_categories || [] }) });
      },
      event: 'finio:overrides-applied',
    },
    budget: {
      describe: p => `Set a <b>${escapeHtml(p.category)}</b> budget of `
        + `<b>${formatAUD(p.amount)}</b> a month`
        + (p.why ? `<span class="cw-prop-count">${escapeHtml(p.why)}</span>` : ''),
      apply: (p) => apiFetch('/budgets', { method: 'POST',
        body: JSON.stringify({ targets: { [p.category]: p.amount } }) }),
      event: 'finio:budgets-applied',
    },
    goal: {
      describe: p => {
        const bits = [
          p.amount != null ? `target <b>${formatAUD(p.amount)}</b>` : '',
          p.target_date ? `by <b>${escapeHtml(p.target_date)}</b>` : '',
        ].filter(Boolean).join(' ');
        return `Update your savings goal — ${bits}`
          + (p.why ? `<span class="cw-prop-count">${escapeHtml(p.why)}</span>` : '');
      },
      apply: async (p) => {
        // /goal needs both fields; fill the missing one from the current goal.
        const dash = await apiFetch('/dashboard').catch(() => ({}));
        const cur = dash.goal || {};
        const rec = dash.goal_recommendation || {};
        const amount = p.amount != null ? p.amount : (cur.target_amount ?? rec.amount);
        const date = (p.target_date || cur.target_date || rec.target_date || '').slice(0, 10);
        if (!amount || !date) throw new Error('Need an amount and a date for the goal');
        await apiFetch('/goal', { method: 'POST',
          body: JSON.stringify({ amount, target_date: date }) });
      },
      event: 'finio:goal-applied',
    },
    profile: {
      describe: p => {
        const bits = [
          p.age != null ? `age <b>${p.age}</b>` : '',
          p.income_bracket ? `situation <b>${escapeHtml(p.income_bracket.replace('_', ' '))}</b>` : '',
        ].filter(Boolean).join(', ');
        return `Update your profile — ${bits}`;
      },
      apply: (p) => {
        const body = {};
        if (p.age != null) body.age = p.age;
        if (p.income_bracket) body.income_bracket = p.income_bracket;
        return apiFetch('/profile', { method: 'POST', body: JSON.stringify(body) });
      },
      event: 'finio:profile-applied',
    },
  };

  function addProposal(p) {
    const kind = PROPOSALS[p.kind] || PROPOSALS.reclassify;
    const card = document.createElement('div');
    card.className = 'cw-proposal';
    card.innerHTML = `
      <div class="cw-prop-text">${kind.describe(p)}</div>
      <button class="cw-apply">Apply</button>`;
    card.querySelector('.cw-apply').addEventListener('click', async (e) => {
      const btn = e.target;
      btn.disabled = true; btn.textContent = 'Applying…';
      try {
        await kind.apply(p);
        btn.textContent = 'Applied ✓';
        showToast('Done — your numbers are updated');
        window.dispatchEvent(new CustomEvent(kind.event));
        window.dispatchEvent(new CustomEvent('finio:data-changed'));
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
        rows.slice(-14).forEach(r => {
          const role = r.role === 'user' ? 'user' : 'assistant';
          addMsg(role, fmtMsg(r.message), r.message);
        });
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
          <span class="cw-chat-title">${escapeHtml(c.title || 'New chat')}</span>
          <span class="cw-chat-count">${c.count} msg${c.count === 1 ? '' : 's'}</span>
        </button>`).join('');
      list.querySelectorAll('.cw-chat-item[data-id]').forEach(b =>
        b.addEventListener('click', () => {
          setCurrentChat(b.dataset.id);
          list.querySelectorAll('.cw-chat-item').forEach(x => x.classList.remove('active'));
          b.classList.add('active');
          loadHistory();
        }));
    } catch (_) {
      list.innerHTML = '<div class="cw-chat-item muted">Could not load chats</div>';
    }
  }

  document.getElementById('cw-chats').addEventListener('click', () => {
    setChatsOpen(!drawer.classList.contains('open'));
  });

  document.getElementById('cw-new').addEventListener('click', () => {
    setCurrentChat('c' + Date.now().toString(36));
    greet();
    if (drawer.classList.contains('open')) loadChats();
    input.focus();
  });

  // Resize the past-chats rail vs the current conversation.
  let railDragging = false;
  railSplitter.addEventListener('pointerdown', (e) => {
    if (railSplitter.hidden) return;
    railDragging = true;
    panel.classList.add('cw-rail-dragging');
    railSplitter.setPointerCapture?.(e.pointerId);
    e.preventDefault();
  });
  window.addEventListener('pointermove', (e) => {
    if (!railDragging) return;
    const rect = panel.getBoundingClientRect();
    const px = applyRailWidth(e.clientX - rect.left);
    // Keep the current chat readable — don't let the rail eat the whole panel.
    const maxForPanel = Math.max(RAIL_MIN, rect.width - 220);
    if (px > maxForPanel) applyRailWidth(maxForPanel);
  });
  window.addEventListener('pointerup', () => {
    if (!railDragging) return;
    railDragging = false;
    panel.classList.remove('cw-rail-dragging');
    const raw = parseFloat(getComputedStyle(panel).getPropertyValue('--cw-rail')) || RAIL_DEFAULT;
    saveRailPx(applyRailWidth(raw));
  });
  window.addEventListener('pointercancel', () => {
    railDragging = false;
    panel.classList.remove('cw-rail-dragging');
  });

  // ── Split screen (page | coach) with a draggable divider ──
  // Both sides constrict until a min, then the other slides over (no crushing).
  const PAGE_MIN_PX = 440;
  const COACH_MIN_PX = 340;

  function bumpLayout() {
    window.dispatchEvent(new Event('resize'));
  }

  function setSplitPct(pct) {
    const clamped = Math.max(0, Math.min(100, pct));
    const vw = window.innerWidth || 1;
    const coachPx = (clamped / 100) * vw;

    document.body.style.setProperty('--coach-pane', `${clamped}%`);

    if (clamped <= 3) {
      document.body.classList.remove('coach-overlay', 'coach-page-over');
      document.body.style.removeProperty('--page-col');
      document.body.style.removeProperty('--coach-col');
      splitter.setAttribute('aria-valuenow', String(Math.round(clamped)));
      return clamped;
    }

    if (coachPx > vw - PAGE_MIN_PX) {
      // Growing coach past page min → page freezes, coach slides over it.
      document.body.classList.add('coach-overlay');
      document.body.classList.remove('coach-page-over');
      document.body.style.setProperty('--page-col', `${PAGE_MIN_PX}px`);
      document.body.style.removeProperty('--coach-col');
    } else if (coachPx < COACH_MIN_PX) {
      // Shrinking coach past coach min → coach freezes, page goes full over it.
      document.body.classList.add('coach-page-over');
      document.body.classList.remove('coach-overlay');
      document.body.style.setProperty('--coach-col', `${COACH_MIN_PX}px`);
      document.body.style.removeProperty('--page-col');
    } else {
      // Normal push: both sides share the screen.
      document.body.classList.remove('coach-overlay', 'coach-page-over');
      document.body.style.removeProperty('--page-col');
      document.body.style.removeProperty('--coach-col');
    }

    splitter.setAttribute('aria-valuenow', String(Math.round(clamped)));
    return clamped;
  }

  function syncExpandBtn(splitOn) {
    if (splitOn) {
      expandBtn.setAttribute('aria-label', 'Exit split screen');
      expandBtn.title = 'Exit split screen';
      expandBtn.textContent = '⤡';
    } else {
      expandBtn.setAttribute('aria-label', 'Split screen with this page');
      expandBtn.title = 'Split screen';
      expandBtn.textContent = '⤢';
    }
  }

  function enterSplit(pct = loadSplitPct()) {
    // Phones: keep the old full-bleed overlay — no room for a useful split.
    if (isNarrow()) {
      panel.classList.add('full', 'open');
      panel.classList.remove('split');
      document.body.classList.remove('coach-split');
      splitter.hidden = true;
      syncExpandBtn(true);
      fab.classList.add('hidden');
      hydrate();
      return;
    }
    applyDockSide(preferredSide());
    panel.classList.remove('full');
    panel.classList.add('split', 'open');
    fab.classList.add('hidden');
    hydrate();
    panel.style.left = '';
    panel.style.right = '';
    panel.style.top = '';
    panel.style.bottom = '';
    document.body.classList.add('coach-split');
    const saved = setSplitPct(pct < 12 ? SPLIT_DEFAULT : pct);
    saveSplitPct(saved);
    saveOpenState('split');
    splitter.hidden = false;
    syncExpandBtn(true);
    fab.classList.add('hidden');
    requestAnimationFrame(bumpLayout);
  }

  function exitSplit({ keepOpen = true } = {}) {
    panel.classList.remove('full', 'split');
    document.body.style.removeProperty('--coach-pane');
    document.body.style.removeProperty('--page-col');
    document.body.style.removeProperty('--coach-col');
    document.body.classList.remove('coach-split', 'coach-splitting', 'coach-overlay', 'coach-page-over');
    splitter.hidden = true;
    syncExpandBtn(false);
    if (!keepOpen) {
      panel.classList.remove('open');
      fab.classList.remove('hidden');
      saveOpenState('closed');
      placeFloatingPanel();
    } else {
      placeFloatingPanel();
    }
    requestAnimationFrame(bumpLayout);
  }

  function toggleSplit() {
    if (panel.classList.contains('split') || panel.classList.contains('full')) {
      exitSplit({ keepOpen: true });
    } else {
      enterSplit();
    }
  }

  // Drag the vertical handle. Side depends on where the fab lives.
  let dragging = false;
  function onDragMove(clientX) {
    const side = document.body.dataset.coachSide || 'right';
    let pct = side === 'left'
      ? (clientX / window.innerWidth) * 100
      : ((window.innerWidth - clientX) / window.innerWidth) * 100;
    if (pct >= 97) pct = 100;
    if (pct <= 3) pct = 0;
    setSplitPct(pct);
  }
  function endDrag() {
    if (!dragging) return;
    dragging = false;
    document.body.classList.remove('coach-splitting');
    const raw = parseFloat(getComputedStyle(document.body).getPropertyValue('--coach-pane')) || SPLIT_DEFAULT;
    const vw = window.innerWidth || 1;
    // Dragged past the frozen coach min toward the edge → close split.
    if (raw <= 3 || (raw / 100) * vw < COACH_MIN_PX * 0.55) {
      exitSplit({ keepOpen: true });
      return;
    }
    const pct = setSplitPct(raw);
    saveSplitPct(pct);
    bumpLayout();
  }

  splitter.addEventListener('pointerdown', (e) => {
    if (!panel.classList.contains('split')) return;
    dragging = true;
    document.body.classList.add('coach-splitting');
    splitter.setPointerCapture?.(e.pointerId);
    e.preventDefault();
  });
  window.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    onDragMove(e.clientX);
  });
  window.addEventListener('pointerup', endDrag);
  window.addEventListener('pointercancel', endDrag);

  splitter.tabIndex = 0;
  splitter.setAttribute('role', 'separator');
  splitter.setAttribute('aria-orientation', 'vertical');
  splitter.setAttribute('aria-valuemin', '0');
  splitter.setAttribute('aria-valuemax', '100');
  splitter.addEventListener('keydown', (e) => {
    if (!panel.classList.contains('split')) return;
    const step = e.shiftKey ? 8 : 3;
    const side = document.body.dataset.coachSide || 'right';
    const cur = parseFloat(getComputedStyle(document.body).getPropertyValue('--coach-pane')) || SPLIT_DEFAULT;
    // Arrow toward the page shrinks the coach; toward the coach edge grows it.
    const grow = side === 'left' ? 'ArrowRight' : 'ArrowLeft';
    const shrink = side === 'left' ? 'ArrowLeft' : 'ArrowRight';
    if (e.key === grow) {
      e.preventDefault();
      saveSplitPct(setSplitPct(cur + step));
      bumpLayout();
    } else if (e.key === shrink) {
      e.preventDefault();
      const next = setSplitPct(cur - step);
      if (next <= 3) exitSplit({ keepOpen: true });
      else { saveSplitPct(next); bumpLayout(); }
    }
  });

  window.addEventListener('resize', () => {
    if (panel.classList.contains('split')) {
      if (isNarrow()) {
        enterSplit();
        return;
      }
      const cur = parseFloat(getComputedStyle(document.body).getPropertyValue('--coach-pane')) || SPLIT_DEFAULT;
      setSplitPct(cur);
      bumpLayout();
      return;
    }
    if (!fab.classList.contains('hidden')) applyFabPos(loadFabPos());
    if (panel.classList.contains('open')) placeFloatingPanel();
  });

  // Load the conversation once per page load, whichever way the panel opened.
  // setOpen() used to be the only caller of loadHistory(), so restoring in
  // SPLIT mode (which goes through enterSplit) reopened the panel completely
  // blank — the thread was still on the server, nothing ever fetched it.
  let hydrated = false;
  function hydrate() {
    if (hydrated) return;
    hydrated = true;
    loadHistory();
  }

  // ── Drag the small panel by its header ──────────────────────────────────
  // Only in floating mode: split is docked to an edge and full is the phone
  // overlay, so neither has anywhere to be dragged to.
  (function makeDraggable() {
    const head = panel.querySelector('.cw-head');
    if (!head) return;
    let dragging = false, startX = 0, startY = 0, baseX = 0, baseY = 0, moved = false;

    const floating = () =>
      panel.classList.contains('open')
      && !panel.classList.contains('split')
      && !panel.classList.contains('full');

    head.addEventListener('pointerdown', (e) => {
      // Never hijack the header's buttons (close, expand, new chat...).
      if (e.target.closest('button, a, input, select')) return;
      if (!floating() || e.button !== 0) return;
      const r = panel.getBoundingClientRect();
      dragging = true; moved = false;
      startX = e.clientX; startY = e.clientY;
      baseX = r.left; baseY = r.top;
      // Pin to the measured box first, so switching off right/bottom docking
      // does not make the panel jump before the first move.
      panel.style.left = `${Math.round(r.left)}px`;
      panel.style.top = `${Math.round(r.top)}px`;
      panel.style.right = 'auto';
      panel.style.bottom = 'auto';
      panel.classList.add('dragging');
      head.setPointerCapture?.(e.pointerId);
    });

    head.addEventListener('pointermove', (e) => {
      if (!dragging) return;
      const dx = e.clientX - startX;
      const dy = e.clientY - startY;
      if (!moved && Math.abs(dx) + Math.abs(dy) < 3) return;   // ignore jitter
      moved = true;
      const w = panel.offsetWidth, h = panel.offsetHeight;
      // Always leave the panel reachable — never let it be dragged off-screen.
      panel.style.left = `${Math.round(clamp(baseX + dx, 8, window.innerWidth - w - 8))}px`;
      panel.style.top = `${Math.round(clamp(baseY + dy, 8, window.innerHeight - h - 8))}px`;
      e.preventDefault();
    });

    const end = (e) => {
      if (!dragging) return;
      dragging = false;
      panel.classList.remove('dragging');
      head.releasePointerCapture?.(e.pointerId);
      if (!moved) return;
      const r = panel.getBoundingClientRect();
      savePanelPos({
        x: (r.left / window.innerWidth) * 100,
        y: (r.top / window.innerHeight) * 100,
      });
    };
    head.addEventListener('pointerup', end);
    head.addEventListener('pointercancel', end);

    // Double-click the header to snap back to the default corner.
    head.addEventListener('dblclick', (e) => {
      if (e.target.closest('button, a, input, select') || !floating()) return;
      clearPanelPos();
      placeFloatingPanel();
      showToast('Coach position reset');
    });

    // A saved position can end up off-screen after a resize — pull it back.
    window.addEventListener('resize', () => { if (floating()) placeFloatingPanel(); });
  })();

  function setOpen(open) {
    if (!open) {
      exitSplit({ keepOpen: false });
      setChatsOpen(false);
      return;
    }
    applyDockSide(preferredSide());
    placeFloatingPanel();
    panel.classList.add('open');
    fab.classList.add('hidden');
    saveOpenState('open');
    hydrate();
    setTimeout(() => input.focus(), 150);
  }

  document.getElementById('cw-close').addEventListener('click', () => setOpen(false));
  expandBtn.addEventListener('click', () => {
    if (!panel.classList.contains('open')) setOpen(true);
    toggleSplit();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && panel.classList.contains('open')) {
      if (drawer.classList.contains('open')) {
        setChatsOpen(false);
      } else if (panel.classList.contains('split') || panel.classList.contains('full')) {
        exitSplit({ keepOpen: true });
      } else {
        setOpen(false);
      }
    }
  });

  document.getElementById('cw-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const msg = input.value.trim();
    if (!msg) return;
    input.value = '';
    addMsg('user', fmtMsg(msg), msg);
    const typing = addMsg(
      'assistant typing',
      `<span class="cw-coin" aria-label="Thinking">
        <span class="cw-coin-face"></span>
      </span>`
    );
    try {
      const body = { message: msg, page, chat_id: currentChat(), ...storedPeriodParts() };
      const res = await apiFetch('/coach', { method: 'POST', body: JSON.stringify(body) });
      typing.remove();
      await typeAssistant(res.text || '—');
      (res.proposed_actions || []).forEach(addProposal);
      if (drawer.classList.contains('open') || res.chat_title) loadChats();
    } catch (err) {
      typing.remove();
      const fallback = (err.message === 'no_analysis' || err.status === 404)
        ? 'I don\'t have your data yet — upload a statement on the Dashboard first.'
        : (err.message || 'Sorry, something went wrong.');
      await typeAssistant(fallback);
    }
  });

  // Reopen exactly as the user left it on the previous page. Deferred with a
  // timer rather than requestAnimationFrame: rAF is suspended in background
  // tabs, so a page opened in one would never restore the coach.
  const restore = loadOpenState();
  if (restore === 'split' || restore === 'open') {
    setTimeout(() => {
      if (restore === 'split' && !isNarrow()) enterSplit();
      else setOpen(true);
    }, 0);
  }
}
