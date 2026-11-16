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
  if (fact.kind === 'summary') {
    const lines = [`Income ${value(fact.income)} · Spending ${value(fact.spent)} · Net saved ${value(fact.saved)}`];
    const review = fact.transfer_review;
    if (review?.count) lines.push(`${review.count} transfers need review: ${value(review.money_out)} out, ${value(review.money_in)} in, excluded from income and spending.`);
    return lines;
  }
  return [];
}

export function appendFacts(turn, facts, money) {
  facts = (facts || []).filter(fact => factLines(fact, money).length);
  if (!facts.length) return;
  const box = document.createElement('aside');
  box.className = 'cw-facts';
  const title = document.createElement('strong');
  title.textContent = facts.every(f => f.kind === 'balance') ? 'Your saved balance details' : 'Calculated from your statement';
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
