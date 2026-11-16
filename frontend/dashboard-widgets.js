import { browserStorage, preferenceKey, readPreference, writePreference } from './preferences.js';

export const WIDGETS = [
  ['balance', 'Current balance'],
  ['income', 'Income'], ['spending', 'Spending'], ['surplus', 'Statement surplus'],
  ['merchants', 'Top merchants'], ['history', 'Monthly history'], ['budgets', 'Budgets'],
  ['goal', 'Savings goal'], ['monthly-context', 'Monthly averages'],
  ['anomalies', 'Unusual transactions'], ['personality', 'Money personality'],
  ['insight', 'Statement recap'], ['transactions', 'Transaction shortcut'],
];
export const BASIC_WIDGETS = ['balance', 'income', 'spending', 'surplus', 'merchants'];
const allowed = new Set(WIDGETS.map(([id]) => id));
export const validWidgets = value => Array.isArray(value) && value.every(id => allowed.has(id)) && new Set(value).size === value.length;

export function initialWidgets(storage, userId) {
  const key = preferenceKey('dashboard-widgets-v2', userId);
  const current = readPreference(storage, key, null, validWidgets);
  if (current !== null) return current;
  const legacy = readPreference(storage, preferenceKey('dashboard-widgets-v1', userId), BASIC_WIDGETS, validWidgets);
  const migrated = ['balance', ...legacy.filter(id => id !== 'balance')];
  writePreference(storage, key, migrated);
  return migrated;
}

export function mountDashboardWidgets(userId, onChange = () => {}) {
  const key = preferenceKey('dashboard-widgets-v2', userId);
  const storage = browserStorage();
  let selected = initialWidgets(storage, userId);
  let data = {};
  const dialog = document.getElementById('widget-dialog');
  const choices = document.getElementById('widget-choices');
  const status = document.getElementById('widget-status');
  for (const [id, name] of WIDGETS) {
    const label = document.createElement('label');
    const input = document.createElement('input');
    input.type = 'checkbox'; input.value = id; input.checked = selected.includes(id);
    input.addEventListener('change', () => {
      selected = [...choices.querySelectorAll('input:checked')].map(el => el.value);
      save(); update(); onChange();
    });
    label.append(input, document.createTextNode(name)); choices.append(label);
  }
  const open = () => dialog.showModal();
  document.getElementById('widgets-button').addEventListener('click', open);
  document.getElementById('widgets-empty-button').addEventListener('click', open);
  document.getElementById('widgets-done').addEventListener('click', () => dialog.close());
  document.getElementById('widgets-reset').addEventListener('click', () => {
    selected = [...BASIC_WIDGETS];
    choices.querySelectorAll('input').forEach(input => { input.checked = selected.includes(input.value); });
    save(); update(); onChange();
  });
  function save() {
    status.textContent = writePreference(storage, key, selected)
      ? 'Layout saved for this account on this browser.'
      : 'Layout changed for this visit. This browser could not save it.';
  }
  function update(next) {
    if (next) data = next;
    const available = {
      history: (data.averages?.spend_series?.length || 0) >= 2,
      anomalies: Boolean(data.anomalies?.length),
      personality: Boolean(data.personality) && !data.review?.count,
      insight: !data.review?.count,
      'monthly-context': Boolean(data.averages?.monthly),
    };
    document.querySelectorAll('[data-widget]').forEach(node => {
      const id = node.dataset.widget;
      node.hidden = !selected.includes(id) || available[id] === false;
    });
    const metrics = document.getElementById('metrics-grid');
    const count = [...metrics.children].filter(node => !node.hidden).length;
    metrics.hidden = count === 0; metrics.dataset.visible = count;
    const grid = document.getElementById('widget-grid');
    const visible = [...grid.children].filter(node => !node.hidden && node.style.display !== 'none').length;
    grid.hidden = visible === 0; grid.classList.toggle('single-widget', visible === 1);
    document.getElementById('widgets-empty').hidden = selected.length !== 0;
  }
  function reveal(id) {
    if (!allowed.has(id) || selected.includes(id)) return;
    selected.push(id);
    choices.querySelectorAll('input').forEach(input => { input.checked = selected.includes(input.value); });
    save(); update();
  }
  return { update, reveal, has: id => selected.includes(id) };
}
