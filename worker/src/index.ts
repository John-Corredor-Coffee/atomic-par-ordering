import type { Env, DataPayload } from './types';
import { getAccessToken, makeClient, fetchShopTimezone, fetchAllOrders, fetchNewSignups } from './shopify';
import { todayLocal, daysBefore } from './dates';
import { accumulateFromOrders, buildWindows, computeBenchmarks, collectSkuNames, buildJson, buildPayload, loadDeliveryDays } from './aggregate';
import { runGuardrails } from './guardrails';
import { INTERNAL_NAMES, CATALOG_TAGS } from './skuRules';
import type { NewCompany } from './types';

const DATA_KEY = 'data.json';
const LAST_ERROR_KEY = 'last_error';
const STATUS_KEY = 'refresh_status';

// A refresh that started more than this long ago and never reported back is assumed
// dead (Worker evicted mid-run, etc.) — a new manual trigger is allowed past it so a
// crashed run can't wedge the button permanently.
const RUNNING_LOCK_MS = 15 * 60 * 1000;

type RefreshState = 'running' | 'ok' | 'error';
interface RefreshStatus {
  state: RefreshState;
  trigger: 'cron' | 'manual';
  startedAt: string;
  finishedAt?: string;
  summary?: string;
  failures?: string[];
  error?: string;
}

// Companies never eligible for the "new zero-order signup" injection, regardless
// of what Shopify returns — hardened per a past incident where a human had to
// manually exclude John's test account from a live run.
const NEW_SIGNUP_EXCLUDE = new Set([...INTERNAL_NAMES, 'Test Cafe']);

const JSON_HEADERS = { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' };

export default {
  async scheduled(_event: ScheduledEvent, env: Env, ctx: ExecutionContext): Promise<void> {
    // Cloudflare crons fire on fixed UTC with no DST awareness. To hit 12:01 AM
    // Eastern year-round we register TWO UTC crons (04:01 and 05:01) that straddle
    // the EST/EDT offset, and gate here so only the one landing just after local
    // midnight actually runs — the other is a no-op.
    const easternHour = Number(
      new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', hour: '2-digit', hourCycle: 'h23' }).format(new Date())
    );
    if (easternHour !== 0) return;
    ctx.waitUntil(runRefreshTracked(env, 'cron'));
  },

  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);

    // Current dataset (served same-origin to the frontend).
    if (url.pathname === '/api/data.json') {
      const stored = await env.PAR_DATA.get(DATA_KEY);
      if (!stored) {
        return new Response(JSON.stringify({ error: 'no data available yet' }), { status: 503, headers: JSON_HEADERS });
      }
      return new Response(stored, { headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' } });
    }

    // Refresh status — the frontend polls this after kicking off a manual refresh.
    if (url.pathname === '/api/status') {
      const status = await getStatus(env);
      return new Response(JSON.stringify(status ?? { state: 'idle' }), { headers: JSON_HEADERS });
    }

    // Manual refresh trigger (the Admin-view "Refresh now" button). The full Shopify
    // pull takes minutes, well past a fetch response's budget, so we kick it off in
    // the background (ctx.waitUntil) and return immediately; the client polls /api/status.
    if (url.pathname === '/api/refresh' && request.method === 'POST') {
      const existing = await getStatus(env);
      if (existing?.state === 'running' && !isStale(existing)) {
        return new Response(JSON.stringify({ ...existing, alreadyRunning: true }), { status: 202, headers: JSON_HEADERS });
      }
      const started: RefreshStatus = { state: 'running', trigger: 'manual', startedAt: new Date().toISOString() };
      await env.PAR_DATA.put(STATUS_KEY, JSON.stringify(started));
      ctx.waitUntil(runRefreshTracked(env, 'manual'));
      return new Response(JSON.stringify(started), { status: 202, headers: JSON_HEADERS });
    }

    return new Response('Not found', { status: 404 });
  },
};

async function getStatus(env: Env): Promise<RefreshStatus | null> {
  const raw = await env.PAR_DATA.get(STATUS_KEY);
  return raw ? (JSON.parse(raw) as RefreshStatus) : null;
}

function isStale(status: RefreshStatus): boolean {
  return Date.now() - new Date(status.startedAt).getTime() > RUNNING_LOCK_MS;
}

// Wraps runRefresh with status bookkeeping so both the cron and the manual button
// leave a machine-readable record of the last run in KV.
async function runRefreshTracked(env: Env, trigger: 'cron' | 'manual'): Promise<void> {
  const startedAt = new Date().toISOString();
  await env.PAR_DATA.put(STATUS_KEY, JSON.stringify({ state: 'running', trigger, startedAt } satisfies RefreshStatus));
  try {
    const result = await runRefresh(env);
    const finished: RefreshStatus = result.ok
      ? { state: 'ok', trigger, startedAt, finishedAt: new Date().toISOString(), summary: result.summary }
      : { state: 'error', trigger, startedAt, finishedAt: new Date().toISOString(), failures: result.failures };
    await env.PAR_DATA.put(STATUS_KEY, JSON.stringify(finished));
  } catch (err: any) {
    const finished: RefreshStatus = {
      state: 'error',
      trigger,
      startedAt,
      finishedAt: new Date().toISOString(),
      error: err?.message ?? String(err),
    };
    await env.PAR_DATA.put(STATUS_KEY, JSON.stringify(finished));
  }
}

async function runRefresh(env: Env): Promise<{ ok: boolean; summary?: string; failures?: string[] }> {
  const token = await getAccessToken(env);
  const client = makeClient(env, token);

  const ianaTz = await fetchShopTimezone(client);
  const today = todayLocal(ianaTz);
  const windows = buildWindows(today, daysBefore);

  const sinceDate = daysBefore(today, 463 + 14); // +14d pad for created_at vs processedAt drift
  const { orders, truncatedOrderCount } = await fetchAllOrders(client, sinceDate);

  const existingRaw = await env.PAR_DATA.get(DATA_KEY);
  const existing: DataPayload | null = existingRaw ? JSON.parse(existingRaw) : null;
  const deliveryDays = loadDeliveryDays(existing);

  const { locs, activeSet, lastLookup } = accumulateFromOrders(orders, windows, deliveryDays, ianaTz, truncatedOrderCount);
  const benchmarks = computeBenchmarks(locs);
  const skuNames = collectSkuNames(locs);

  const cutoffDate = daysBefore(today, 60 + 2); // +2d pad, exact >=60d check applied below
  const rawSignups = await fetchNewSignups(client, cutoffDate, NEW_SIGNUP_EXCLUDE, CATALOG_TAGS);
  const cutoffExact = daysBefore(today, 60);
  const newCompanies: NewCompany[] = rawSignups.filter((co) => {
    const createdLocal = co.createdAt.slice(0, 10); // createdAt is already UTC ISO; date-only compare matches Python's tzinfo-stripped comparison
    return createdLocal >= cutoffExact;
  });

  const generatedIso = new Date().toISOString();
  const companies = buildJson(locs, lastLookup, benchmarks, activeSet, newCompanies, skuNames, today, daysBefore);
  const payload = buildPayload(companies, generatedIso);

  const guard = runGuardrails(companies, existing);
  if (!guard.ok) {
    await env.PAR_DATA.put(
      LAST_ERROR_KEY,
      JSON.stringify({ at: generatedIso, failures: guard.failures, truncatedOrderCount })
    );
    return { ok: false, failures: guard.failures };
  }

  await env.PAR_DATA.put(DATA_KEY, JSON.stringify(payload));

  const realCompanies = companies.filter((c) => !c.isInternal);
  const nLocs = realCompanies.reduce((n, c) => n + c.locations.length, 0);
  const statusCounts = { active: 0, 'needs-review': 0, new: 0 } as Record<string, number>;
  for (const c of realCompanies) for (const l of c.locations) statusCounts[l.reviewStatus] = (statusCounts[l.reviewStatus] ?? 0) + 1;
  const summary = `data.json written -- ${realCompanies.length} companies, ${nLocs} locations (active=${statusCounts.active} needs-review=${statusCounts['needs-review']} new=${statusCounts.new})` +
    (truncatedOrderCount > 0 ? ` [warning: ${truncatedOrderCount} orders had >50 line items, truncated]` : '');

  return { ok: true, summary };
}
