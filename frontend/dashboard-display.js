// Presentation only. Keep bank descriptions unchanged for matching and edits.
export function merchantLabel(description) {
  const raw = String(description || '').trim().replace(/\s+/g, ' ');
  const known = [
    [/^UBER\s*EATS\b/i, 'Uber Eats'],
    [/^WOOLWORTHS\b/i, 'Woolworths'],
    [/^COSTCO\s+WHOLESALE\b/i, 'Costco'],
    [/^TRANSPORTFORNSW\b/i, 'Transport for NSW'],
    [/^ENGIE\b/i, 'ENGIE'],
  ];
  return known.find(([pattern]) => pattern.test(raw))?.[1] || raw || 'Unknown merchant';
}

export function surplusLabel(review) {
  return review?.count > 0 ? 'Surplus · provisional' : 'Income − spending';
}
