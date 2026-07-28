// Calendar-day-string based date math. All window boundaries and order dates are
// represented as 'YYYY-MM-DD' strings in shop-local time — comparing these strings
// lexicographically is equivalent to comparing them chronologically, which sidesteps
// timezone-conversion bugs during arithmetic (only the initial UTC->local conversion
// of an order's timestamp needs real timezone awareness; everything after that is
// plain string/date-part math anchored at UTC noon to avoid DST edge cases).

export function localDateString(utcIso: string, ianaTz: string): string {
  // en-CA formats as YYYY-MM-DD, exactly the ISO date-key format we want.
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: ianaTz,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(new Date(utcIso));
}

function parseDateKey(dateKey: string): { y: number; m: number; d: number } {
  const [y, m, d] = dateKey.split('-').map(Number);
  return { y, m, d };
}

// Anchor at UTC noon so subtracting whole-day increments never crosses a DST
// boundary in a way that shifts the resulting calendar date.
function anchorUtcNoon(dateKey: string): Date {
  const { y, m, d } = parseDateKey(dateKey);
  return new Date(Date.UTC(y, m - 1, d, 12, 0, 0));
}

export function daysBefore(dateKey: string, days: number): string {
  const anchor = anchorUtcNoon(dateKey);
  const shifted = new Date(anchor.getTime() - days * 86400000);
  const y = shifted.getUTCFullYear();
  const m = String(shifted.getUTCMonth() + 1).padStart(2, '0');
  const d = String(shifted.getUTCDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

export function todayLocal(ianaTz: string): string {
  return localDateString(new Date().toISOString(), ianaTz);
}

export interface WindowBounds {
  since: string; // inclusive lower bound
  until: string; // upper bound
  untilInclusive: boolean; // true only for windows whose upper edge is "today"
}

export function inWindow(dateKey: string, w: WindowBounds): boolean {
  if (dateKey < w.since) return false;
  return w.untilInclusive ? dateKey <= w.until : dateKey < w.until;
}
