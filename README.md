# Atomic PAR Ordering

Internal staff tool showing suggested reorder (PAR) quantities per wholesale
account, location and SKU, computed from Shopify B2B order history.

**Live at <https://par-ordering.acr-ops.com/>** behind the `*.acr-ops.com`
Cloudflare Access wall (staff Google SSO). This repository is the source. It does
**not** host the tool — see [Hosting](#hosting).

---

## What it does

Wholesale partners miss order cutoffs and run out of coffee. The tool projects
on-hand quantity per SKU and recommends an order size, so a rep can see at a
glance who is about to run short. The long-term goal is accounts ordering weekly
against real usage rather than habit.

Views are grouped By Account and By SKU, with tabs for active accounts, accounts
needing review, new signups, and accounts that have missed their delivery cutoff.

## Architecture

A single Cloudflare Worker does everything:

```
Shopify Admin API ──► Worker (scheduled)  ──► guardrails ──► KV: data.json
                          │                                        │
                          └──► Worker (fetch) ──► /api/data.json ───┘
                                             ──► /api/status
                                             ──► /api/refresh  (POST)
                                             ──► static frontend from worker/public/
```

- **Refresh** runs nightly at 12:01 AM Eastern. Cloudflare crons fire on fixed
  UTC with no DST awareness, so `wrangler.toml` registers both `1 4 * * *` and
  `1 5 * * *` and `scheduled()` gates on the real Eastern hour — only the one
  landing just after local midnight does work, the other returns immediately.
- **Guardrails** (`worker/src/guardrails.ts`) compare each freshly built payload
  against the stored one and reject it outright if company count, location count
  or total `qw3l` moves too far, or if the active-location ratio collapses. A
  rejected rebuild leaves the previous payload in KV untouched.
- **Storage** is a single KV key, `data.json`, in the `PAR_DATA` namespace.
  `last_error` holds the most recent guardrail rejection. `refresh_status` holds
  the state of the last run and is what `/api/status` serves.
- **On-demand refresh** is the Admin view's REFRESH NOW button, which POSTs
  `/api/refresh`. The Shopify pull takes minutes — well past a fetch response's
  budget — so it is kicked off with `ctx.waitUntil` and the client polls
  `/api/status`. A run that reports nothing for 15 minutes is assumed dead so a
  crash can't wedge the button permanently.

## Layout

| path | what it is |
|---|---|
| `worker/src/index.ts` | Worker entry: cron handler, the three `/api/*` routes, refresh orchestration |
| `worker/src/shopify.ts` | Shopify Admin API client, order paging, new-signup fetch |
| `worker/src/aggregate.ts` | Windowing, per-SKU accumulation, benchmarks, payload assembly |
| `worker/src/guardrails.ts` | Sanity checks run before anything is written to KV |
| `worker/src/skuRules.ts` | SKU grouping, catalog tags, internal-account exclusions |
| `worker/src/dates.ts` | Shop-timezone-aware date helpers |
| `worker/public/index.html` | The entire frontend, single file |
| `worker/wrangler.toml` | Routes, cron triggers, KV binding |
| `scripts/` | Local Python pipeline — reference and reconciliation only, see below |
| `scripts/phase2/` | Apps Script endpoint receiving saved on-hand counts |
| `index.html` | Redirect stub for the retired GitHub Pages URL |

## Hosting

The tool used to be published on GitHub Pages at
`john-corredor-coffee.github.io/atomic-par-ordering/`. **That was retired on
2026-09-03.** The Pages copy was a static site on a public repo, which meant its
`data.json` — every named wholesale account with order history — was downloadable
by anyone with the URL. The root `index.html` is now a redirect stub so existing
bookmarks still work.

Nothing containing account data belongs in this repository. `.gitignore` covers
`data.json`, `.wrangler/` and `node_modules/`; the wrangler entry matters because
its local dev state writes real KV payloads to disk, and two of them were
committed and publicly served until 2026-09-21.

Deploys go through `/ship-tool`, never `/update-github`.

```bash
cd worker
npx wrangler deploy
```

## The local Python pipeline

`scripts/fetch_direct.py` → `refresh_from_mcp.py` → `data.json` predates the
Worker and reproduces the same aggregation locally. It is kept for cross-checking
the Worker's numbers and for rebuilding the logic if the Worker breaks.

**It does not reach the live tool.** It writes a gitignored `data.json` that
nothing serves. Never report a refresh as live because this pipeline ran.

## Known issues

**The forecasting math is defective and unfixed.** Two bugs partly cancel each
other, so they have to be fixed in one deploy or the tool gets visibly worse:

1. `growth = 0.35·yoy + 0.35·w7 + 0.30·w3` uses unbounded window ratios. `yoy`
   has a validity guard; `w3` and `w7` have none, so a new or ramping account can
   reach a 5–9× multiplier.
2. `avgDaily = qw3l/21` is in order units (bags, cases, kegs), but the frontend
   treats it as usage units and divides by `unitsPerOrder`. For 5 lb bags the ÷5
   roughly cancels the inflation above, which is why the largest coffee SKUs look
   approximately right while everything else does not.

Backtested over 17,414 origins on a 14-day horizon, the live formula scores
MAE 33.61 against 13.67 for plain EWMA — 2.5× worse, with a +17.88 lb bias and
over-forecasting by more than 2× on a quarter of origins. The validated
replacement is `EWMA(α≈0.35)` on observable usage with no growth, trend or
seasonal term and `safetyDays` raised 3 → 5. Holt trend, seasonal indices and
per-account safety buffers were each tested and each failed.

**Phase 2 usage data is collected and then discarded.** Saved on-hand counts POST
to an Apps Script endpoint which returns real `avg4w` / `avg12w` per
company-location-SKU, and `effectiveDailyP2()` correctly prefers it. But
`calcPAR()` takes no rate argument and re-derives internally from
`effectiveDaily()`, so the real rate reaches the display and nothing else. Passing
that rate through `calcPAR` / `recOrders` is the cheapest high-value fix
available and bypasses both bugs above wherever fresh data exists.

**The guardrail can deadlock against a legitimate correction.** The `qw3l` check
rejects anything outside 0.5×–2× of the stored payload, and the stored payload
only advances on success. When `ACCEPTED_FINANCIAL_STATUSES` was widened to
include `PENDING` and `AUTHORIZED` on 2026-09-08 — correcting a bug that had been
dropping the ~70% of B2B orders sitting on net terms — the corrected rebuild came
in around 3× the stored baseline and was rejected. Every subsequent nightly run
failed identically against the same frozen baseline. A one-time bypass is needed
to let a known-good large correction establish a new baseline.
