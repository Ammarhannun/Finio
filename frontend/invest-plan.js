export const DEFAULT_PLAN = Object.freeze({ mode: 'example', needs: 50, wants: 30, savings: 20 });
export function validPlan(value) {
  return value && ['none', 'example', 'custom'].includes(value.mode)
    && ['needs','wants','savings'].every(key => Number.isInteger(value[key]) && value[key] >= 0 && value[key] <= 100)
    && value.needs + value.wants + value.savings === 100;
}
export function planAmounts(income, plan = DEFAULT_PLAN) {
  if (!validPlan(plan) || plan.mode === 'none' || !Number.isFinite(income) || income <= 0) return null;
  const cents = Math.round(income * 100);
  const needs = Math.round(cents * plan.needs / 100);
  const wants = Math.round(cents * (plan.needs + plan.wants) / 100) - needs;
  return {needs:needs/100, wants:wants/100, savings:(cents-needs-wants)/100};
}
export function goalFigures(data) {
  const source = data.forecast || data.invest?.goal || data.goal || {};
  const current = source.current_saved == null ? NaN : Number(source.current_saved);
  const target = source.target_amount == null ? NaN : Number(source.target_amount);
  return {current:Number.isFinite(current) ? current : null, target:Number.isFinite(target) && target > 0 ? target : null,
          date:source.target_date || data.goal?.target_date,
          projected:source.projected_total != null && Number.isFinite(Number(source.projected_total)) ? Number(source.projected_total) : null};
}
