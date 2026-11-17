// A group of coach budget proposals must be saved under one data revision.
// Other action types have different validation and are never bundled here.
export function buildBudgetBatch(proposals) {
  if (!Array.isArray(proposals) || proposals.length < 2 ||
      proposals.some(p => p?.kind !== 'budget')) return null;
  const revision = proposals[0].revision;
  if (typeof revision !== 'string' || !revision) return null;
  const seen = new Set();
  const entries = [];
  for (const proposal of proposals) {
    const category = proposal.category;
    if (proposal.expired || proposal.revision !== revision ||
        typeof category !== 'string' || !category.trim() || category.length > 60 ||
        seen.has(category.toLowerCase()) || !Number.isFinite(proposal.amount) ||
        proposal.amount < 0 || proposal.amount > 100_000_000) return null;
    seen.add(category.toLowerCase());
    entries.push([category, proposal.amount]);
  }
  return { revision, targets: Object.fromEntries(entries), count: entries.length };
}

export function saveBudgetBatch(batch, request) {
  if (!batch) throw new Error('No valid budget batch to save');
  return request('/budgets', { method: 'POST',
    body: JSON.stringify({ revision: batch.revision, targets: batch.targets }) });
}

export function undoBudgetBatch(revision, request) {
  if (!revision) throw new Error('No saved budget batch to undo');
  return request('/changes/undo', { method: 'POST',
    body: JSON.stringify({ revision }) });
}
