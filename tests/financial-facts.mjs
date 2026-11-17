import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const source = await readFile(new URL('../frontend/financial-facts.js', import.meta.url), 'utf8');
const { factLines, showStatementSummary } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
test('greetings and general concepts do not display a saved statement summary', () => {
  assert.equal(showStatementSummary('hi'), false);
  assert.equal(showStatementSummary('What is an ETF?'), false);
  assert.equal(showStatementSummary('Am I on track to save?'), true);
  assert.equal(showStatementSummary('How much did I spend?'), true);
});
const money = n => `$${n.toFixed(2)}`;
test('refunds are displayed once beside the authoritative net total', () => {
  const lines = factLines({kind:'category_total', query:'COLES', count:2,
    gross_purchases:110, refunds:10, total:100}, money);
  assert.equal(lines[1], '$110.00 purchases − $10.00 refunds = $100.00 net spending');
});
test('transfer exclusions stay visible alongside summary totals', () => {
  const lines = factLines({kind:'summary', income:2000, spent:100, saved:1900,
    transfer_review:{count:1, money_out:25, money_in:0}}, money);
  assert.equal(lines[0], 'Income $2000.00 · Spending $100.00 · Net saved $1900.00');
  assert.match(lines[1], /1 transfers need review.*\$25.00 out.*excluded/);
});
test('missing data cannot appear as a zero balance or amount', () => {
  assert.match(factLines({kind:'summary', income:null, spent:undefined, saved:NaN}, money)[0], /Income Unavailable · Spending Unavailable · Net saved Unavailable/);
});
test('unrecognized model-generated records cannot render calculated facts', () => {
  assert.deepEqual(factLines({kind:'model_claim', total:42}, money), []);
});
test('bank balance facts identify their actual source and date',()=>{
  const lines=factLines({kind:'balance',amount:9500,as_of:'2026-10-29',source:'saved',is_current:true},money);
  assert.match(lines[0],/Confirmed available balance: \$9500.00/);
  assert.equal(lines[1],'Manually confirmed · As of 2026-10-29');
  assert.match(lines[2],/No live bank connection/);
});
test('monthly income facts distinguish complete-month averages from profile input and partial periods', () => {
  const full = factLines({kind:'monthly_income', complete_months:2, observed_average:4000,
    entered_monthly_income:3500}, money);
  assert.match(full[0], /Average income: \$4000.00\/month across 2 complete calendar months/);
  assert.match(full[1], /\$3500.00\/month · entered by you, not statement-derived/);
  const partial = factLines({kind:'monthly_income', complete_months:0, observed_average:null,
    selected_income:2180, entered_monthly_income:3500}, money);
  assert.match(partial[0], /monthly statement average unavailable/);
  assert.match(partial[1], /Selected-period income: \$2180.00 · not a monthly average/);
  assert.match(partial[2], /Profile input: \$3500.00\/month/);
});
test('monthly spending facts identify the historical category average, not the selected partial month', () => {
  const lines = factLines({kind:'monthly_spending', category:'Groceries', complete_months:3,
    observed_average:500}, money);
  assert.match(lines[0], /Groceries average: \$500.00\/month across 3 complete calendar months/);
  assert.match(lines[0], /Refunds netted; transfers excluded/);
});
test('budget facts distinguish entered assumptions from complete-month statement averages', () => {
  const lines = factLines({kind:'budget_plan', entered_monthly_income:4000,
    monthly_savings_target:1000, savings_target_source:'question', complete_months:3,
    usual_monthly_spending:2600, category:'Food & Dining', category_monthly:350}, money);
  assert.match(lines[0], /\$4000.00 · entered in Profile/);
  assert.match(lines[1], /\$1000.00 · from this question/);
  assert.match(lines[2], /\$2600.00\/month across 3 complete statement months/);
  assert.match(lines[3], /Food & Dining: \$350.00\/month historical average/);
});
