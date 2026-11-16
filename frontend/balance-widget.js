import { apiFetch, formatAUD, dateRangeLabel, showToast } from './api.js';

export function mountBalanceWidget(getData, onSaved) {
  const dialog = document.getElementById('balance-dialog');
  const form = document.getElementById('balance-form');
  const amount = document.getElementById('balance-amount');
  const asOf = document.getElementById('balance-as-of');
  const error = document.getElementById('balance-error');
  const save = document.getElementById('balance-save');
  const clear = document.getElementById('balance-clear');
  let revision = null, pending = false;
  function open() {
    const data = getData();
    if (!data || data.editing_available === false) return;
    revision = data.revision;
    const view = data.balance_view || {};
    amount.value = view.amount == null ? '' : view.amount;
    asOf.value = view.today || '';
    asOf.max = view.today || '';
    clear.hidden = data.cash_balance?.amount == null;
    error.hidden = true;
    dialog.showModal(); amount.focus();
  }
  document.getElementById('balance-update').addEventListener('click', open);
  document.getElementById('balance-cancel').addEventListener('click', () => dialog.close());
  async function persist(value, date) {
    if (pending) return;
    pending = true; save.disabled = clear.disabled = true; error.hidden = true;
    let saved = false;
    try {
      await apiFetch('/balance', {method:'POST', body:JSON.stringify({revision, amount:value, as_of:date})});
      saved = true; dialog.close();
      await onSaved();
      showToast(value == null ? 'Saved balance removed. Your statement balance is shown if available.' : 'Current balance saved. Spend Check will use it while it is current.');
    } catch (err) {
      if (saved) showToast('Balance saved. Refresh the dashboard to see the latest figures.');
      else { error.textContent = err.message || 'Could not save. Your entry is kept.'; error.hidden = false; }
    } finally { pending = false; save.disabled = clear.disabled = false; }
  }
  form.addEventListener('submit', event => {
    event.preventDefault();
    const value = Number(amount.value);
    if (!amount.value.trim() || !Number.isFinite(value) || Math.abs(value) > 100000000 || !asOf.value) {
      error.textContent = 'Enter a valid balance and its date.'; error.hidden = false; return;
    }
    persist(value, asOf.value);
  });
  clear.addEventListener('click', () => persist(null, null));
  function render(data) {
    const view = data.balance_view || {};
    document.getElementById('balance-title').textContent = view.amount != null && !view.is_current
      ? 'Last known balance' : 'Current balance';
    document.getElementById('balance-value').textContent = formatAUD(view.amount);
    const state = document.getElementById('balance-state');
    state.textContent = view.label || 'Add your balance';
    state.dataset.tone = ['green','yellow','red'].includes(view.tone) ? view.tone : 'neutral';
    const date = view.as_of ? dateRangeLabel({start:view.as_of, end:view.as_of}) : '';
    document.getElementById('balance-date').textContent = view.amount == null ? 'Available cash from your bank app'
      : `${view.source === 'saved' ? 'Manually confirmed' : 'Statement balance'} · ${date}${view.is_current ? '' : ' · last known'}`;
    document.getElementById('balance-summary').textContent = view.message || 'Enter the available balance shown in your banking app.';
    const comparison = document.getElementById('balance-comparison');
    comparison.hidden = view.monthly_spending == null;
    comparison.textContent = view.monthly_spending == null ? ''
      : `${view.days_covered == null ? '' : `About ${Math.floor(view.days_covered)} days covered · `}Usual spending ${formatAUD(view.monthly_spending)}/month${view.provisional ? ' · provisional' : ''}`;
    const review = document.getElementById('balance-review');
    review.hidden = !view.transfer_review_count;
    review.textContent = `Review ${view.transfer_review_count || 0} transfers across all history →`;
    document.getElementById('balance-update').textContent = view.amount == null ? 'Add balance' : 'Update balance';
    document.getElementById('balance-update').disabled = data.editing_available === false;
  }
  return {render, open};
}
