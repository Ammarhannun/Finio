// Values in these cards come from server calculations, never from model prose.
export function factLines(fact, money) {
  const value = n => Number.isFinite(n) ? money(n) : 'Unavailable';
  if (fact.kind === 'balance') return [
    `${fact.is_current ? 'Confirmed available balance' : 'Last known balance'}: ${value(fact.amount)}`,
    `${fact.source === 'saved' ? 'Manually confirmed' : fact.source === 'statement' ? 'Statement balance' : 'Balance unavailable'}${fact.as_of ? ` · As of ${fact.as_of}` : ''}`,
    'No live bank connection. Confirm today’s available cash before a purchase check.',
  ];
  if (fact.kind === 'category_total') return [
    `Matching “${fact.query}” · ${fact.count} transactions`,
    `${value(fact.gross_purchases)} purchases − ${value(fact.refunds)} refunds = ${value(fact.total)} net spending`,
  ];
  if (fact.kind === 'monthly_income') {
    const lines = fact.complete_months > 0 && Number.isFinite(fact.observed_average)
      ? [`Average income: ${value(fact.observed_average)}/month across ${fact.complete_months} complete calendar month${fact.complete_months === 1 ? '' : 's'}. Transfers excluded.`]
      : ['No complete calendar month of uploaded history; monthly statement average unavailable.'];
    if (!fact.complete_months && Number.isFinite(fact.selected_income))
      lines.push(`Selected-period income: ${value(fact.selected_income)} · not a monthly average.`);
    if (Number.isFinite(fact.entered_monthly_income))
      lines.push(`Profile input: ${value(fact.entered_monthly_income)}/month · entered by you, not statement-derived.`);
    return lines;
  }
  if (fact.kind === 'monthly_spending') {
    const label = fact.category || 'Total spending';
    const lines = fact.complete_months > 0 && Number.isFinite(fact.observed_average)
      ? [`${label} average: ${value(fact.observed_average)}/month across ${fact.complete_months} complete calendar month${fact.complete_months === 1 ? '' : 's'}. Refunds netted; transfers excluded.`]
      : [`No complete calendar month of uploaded history; monthly ${label.toLowerCase()} average unavailable.`];
    if (!fact.complete_months && Number.isFinite(fact.selected_spending))
      lines.push(`Selected-period spending: ${value(fact.selected_spending)} · not a monthly average.`);
    return lines;
  }
  if (fact.kind === 'budget_plan') {
    const lines = [Number.isFinite(fact.entered_monthly_income)
      ? `Monthly take-home income: ${value(fact.entered_monthly_income)} · entered in Profile.`
      : 'Monthly take-home income: not entered.'];
    if (Number.isFinite(fact.monthly_savings_target))
      lines.push(`Monthly savings target: ${value(fact.monthly_savings_target)} · ${fact.savings_target_source === 'question' ? 'from this question' : 'entered in Profile'}.`);
    if (fact.complete_months > 0 && Number.isFinite(fact.usual_monthly_spending)) {
      lines.push(`Usual spending: ${value(fact.usual_monthly_spending)}/month across ${fact.complete_months} complete statement month${fact.complete_months === 1 ? '' : 's'}.`);
      if (fact.category && Number.isFinite(fact.category_monthly))
        lines.push(`${fact.category}: ${value(fact.category_monthly)}/month historical average.`);
    } else {
      lines.push('No complete calendar month available for a usual-spending comparison.');
    }
    return lines;
  }
  if (fact.kind === 'summary') {
    const lines = [`Income ${value(fact.income)} · Spending ${value(fact.spent)} · Net saved ${value(fact.saved)}`];
    const review = fact.transfer_review;
    if (review?.count) lines.push(`${review.count} transfers need review: ${value(review.money_out)} out, ${value(review.money_in)} in, excluded from income and spending.`);
    return lines;
  }
  return [];
}

export function showStatementSummary(question) {
  return /\b(?:spend\w*|sav\w*|income|earn\w*|budget\w*|goal\w*|money|cash|transaction\w*|categor\w*|merchant\w*|bill\w*|afford|refund\w*|transfer\w*|purchas\w*|buy|bought|cost\w*|price\w*|financial|how much)\b/i.test(question || '');
}

export function appendFacts(turn, facts, money, question = null) {
  facts = (facts || []).filter(fact => factLines(fact, money).length);
  // Older chats stored a summary card even after a greeting. Keep their
  // history intact, but do not render that irrelevant card on reload.
  if (question !== null && !showStatementSummary(question)) {
    facts = facts.filter(fact => fact.kind !== 'summary');
  }
  if (!facts.length) return;
  const box = document.createElement('aside');
  box.className = 'cw-facts';
  const title = document.createElement('strong');
  title.textContent = facts.every(f => f.kind === 'balance') ? 'Your saved balance details'
    : facts.every(f => f.kind === 'budget_plan') ? 'Your budget inputs and statement history'
    : 'Calculated from your statement';
  box.appendChild(title);
  for (const fact of facts) {
    if (fact.read_scope) {
      const label = document.createElement('p');
      label.className = 'cw-facts-scope'; label.textContent = fact.read_scope;
      box.appendChild(label);
    }
    const range = fact.date_range;
    if (range?.start && range?.end) {
      const scope = document.createElement('p');
      scope.className = 'cw-facts-scope';
      scope.textContent = `${range.start} to ${range.end} · ${fact.currency || ''}`;
      box.appendChild(scope);
    }
    for (const line of factLines(fact, money)) {
      const p = document.createElement('p');
      p.textContent = line;
      box.appendChild(p);
    }
  }
  const note = document.createElement('p');
  note.className = 'cw-facts-scope';
  note.textContent = 'Figures at the time of this reply. AI explanations can make mistakes.';
  box.appendChild(note);
  turn.appendChild(box);
}
