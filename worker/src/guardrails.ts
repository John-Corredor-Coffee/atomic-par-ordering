import type { Company, DataPayload } from './types';

export interface GuardrailResult {
  ok: boolean;
  failures: string[];
}

// Sanity-checks the freshly built payload against the currently-stored one before
// anything gets written to KV. A broken API response or logic bug fails loudly here
// instead of silently going live to real wholesale accounts placing real orders.
export function runGuardrails(newCompanies: Company[], prev: DataPayload | null): GuardrailResult {
  const failures: string[] = [];
  if (!prev) {
    // First-ever run — nothing to compare against. Only check for outright emptiness.
    if (!newCompanies.some((c) => !c.isInternal)) failures.push('no companies produced on first run');
    return { ok: failures.length === 0, failures };
  }

  const prevReal = prev.companies.filter((c) => !c.isInternal);
  const newReal = newCompanies.filter((c) => !c.isInternal);

  const prevCoCount = prevReal.length;
  const newCoCount = newReal.length;
  const prevLocCount = prevReal.reduce((n, c) => n + c.locations.length, 0);
  const newLocCount = newReal.reduce((n, c) => n + c.locations.length, 0);
  const sumQw3l = (companies: Company[]) =>
    companies.reduce((n, c) => n + c.locations.reduce((m, l) => m + l.items.reduce((k, i) => k + i.qw3l, 0), 0), 0);
  const prevQty = sumQw3l(prevReal);
  const newQty = sumQw3l(newReal);
  const activeRatio =
    newLocCount === 0
      ? 0
      : newReal.reduce((n, c) => n + c.locations.filter((l) => l.reviewStatus === 'active').length, 0) / newLocCount;

  if (!(0.8 * prevCoCount <= newCoCount && newCoCount <= 1.25 * prevCoCount)) {
    failures.push(`company count ${newCoCount} outside +/-20% of prior ${prevCoCount}`);
  }
  if (!(0.8 * prevLocCount <= newLocCount && newLocCount <= 1.25 * prevLocCount)) {
    failures.push(`location count ${newLocCount} outside +/-20% of prior ${prevLocCount}`);
  }
  if (newQty === 0 || !(0.5 * prevQty <= newQty && newQty <= 2.0 * prevQty)) {
    failures.push(`total qw3l ${newQty} outside [0.5x,2x] of prior ${prevQty}`);
  }
  if (activeRatio < 0.4) {
    failures.push(`active-location ratio ${activeRatio.toFixed(2)} below 0.40 -- likely broken 30d membership calc`);
  }
  const last = newCompanies[newCompanies.length - 1];
  if (!last || last.name !== 'Atomic Coffee Roasters (Internal)' || !last.isInternal) {
    failures.push('internal company entry missing or misplaced');
  }

  return { ok: failures.length === 0, failures };
}
