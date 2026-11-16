export const PAGE_MIN_PX = 440;
export const COACH_MIN_PX = 340;

export function splitLayout(width, percentage = 42) {
  if (width < PAGE_MIN_PX + COACH_MIN_PX) return {mode:'full', percentage:100};
  const min = COACH_MIN_PX / width * 100;
  const max = 100 - PAGE_MIN_PX / width * 100;
  return {mode:'split', percentage:Math.max(min, Math.min(max, Number.isFinite(percentage) ? percentage : 42))};
}
