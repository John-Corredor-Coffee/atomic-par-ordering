# PAR Ordering Tool — Handoff for Brendan Burke

Script is updated and the token is minting fine — one scope is missing that's blocking the first full test run. Here's where everything stands and what I need from you.

## What I need you to do
1. Add `read_customers` to the Par Data Refresh app scopes in the Dev Dashboard and reinstall on `serve-atomic.myshopify.com` — this unblocks the `purchasingEntity` field on orders (B2B company + location data)
2. Once reinstalled, let me know and I'll run the full test end-to-end
3. Before Phase 2: transfer the repo from `John-Corredor-Coffee/atomic-par-ordering` to an Atomic-owned GitHub account (treating this as your gate, same as you called out)
4. Confirm the path/URL for the tool on your existing web infrastructure so I know where it'll land

## Questions for you
1. The `par_profile` metaobject definition already exists on the store with all the right fields — do you know how it got there? I don't have a record of creating it on my end.

## Assumptions I made (push back if any are wrong)
- `read_customers` is the only missing scope — everything else (`read_orders`, `read_all_orders`, `write_metaobjects`) is sufficient once that's added
- The tool stays on my personal GitHub through testing and moves to an Atomic account before Phase 2 (not before)
- The `.env` with Client ID + Secret stays local for now — I'll move credentials when the infrastructure moves
- Phase 2 (per-customer auth + scheduled refresh) doesn't start until the repo is on Atomic infrastructure

## What was built
**Live tool:** https://john-corredor-coffee.github.io/atomic-par-ordering/ (password protected)
**Dashboard:** https://john-corredor-coffee.github.io/atomic-dashboard/ — password: `BUFenway`
**Repo:** `John-Corredor-Coffee/atomic-par-ordering`

**Script:** `refresh_par_data.py`
- Updated from static `SHOPIFY_ACCESS_TOKEN` to client credentials grant (your new app model)
- Calls `POST https://serve-atomic.myshopify.com/admin/oauth/access_token` at runtime to mint a 24h token
- Credentials via env vars: `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET` (in `scripts/.env`)
- Fetches 180 days of paid B2B orders via GraphQL, paginated
- Computes `avg_daily`, `yoy`, `w3` trend, `last_order_date` per company + location + SKU
- Upserts results as `par_profile` metaobjects on `serve-atomic.myshopify.com`
- API version: `2024-10`

**Test result so far:** Token minted successfully. Script failed on page 1 of orders — `ACCESS_DENIED` on `purchasingEntity` field. Root cause: `read_customers` scope not granted. Everything else is clear.

**App:** "Par Data Refresh" — Client ID `984fff8a3d751da30eec75e1ac7fd28e`

## Testing
Once scope is updated and app reinstalled:
1. `export $(cat scripts/.env | xargs) && python3 refresh_par_data.py`
2. Confirm script fetches orders and upserts without errors
3. Check Shopify admin → Content → Metaobjects → `par_profile` for entries

## Deadline
None.
