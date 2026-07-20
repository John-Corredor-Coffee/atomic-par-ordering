# Phase 2 Apps Script — Deployment Instructions

## What this is
A Google Apps Script web app that acts as the write endpoint for the PAR tool.
The PAR tool (static GitHub Pages site) cannot write to a database directly —
this script bridges the gap, accepting POST requests from the browser and writing
to the Phase 2 Google Sheet.

## Sheet ID
`11iLeJILCJ_ZZE7d96omWV6Awki7xJGAuUpHy_EjpxeI`
https://docs.google.com/spreadsheets/d/11iLeJILCJ_ZZE7d96omWV6Awki7xJGAuUpHy_EjpxeI

## One-time deployment steps

1. Open the Phase 2 data store sheet
2. Go to **Extensions → Apps Script**
3. Delete any existing code in `Code.gs`
4. Paste the full contents of `Code.gs` from this folder
5. Click **Save** (floppy disk icon)
6. Click **Deploy → New deployment**
7. Settings:
   - Type: **Web app**
   - Execute as: **Me (john.corredor@atomicroastery.com)**
   - Who has access: **Anyone**
8. Click **Deploy** → authorize when prompted
9. Copy the **Web app URL** — looks like:
   `https://script.google.com/macros/s/AKfycb.../exec`
10. Paste that URL into `par-ordering/index.html` as `PHASE2_ENDPOINT`

## Deployed endpoint
- **Deployment ID:** `AKfycbyUhpWNe0dj8f8qOYn26W2NKVVoO9Tl2Wi1JkJSWrP26cNKTTzjKdIcrhqLoBCf7jXiQA`
- **Web app URL:** `https://script.google.com/macros/s/AKfycbyUhpWNe0dj8f8qOYn26W2NKVVoO9Tl2Wi1JkJSWrP26cNKTTzjKdIcrhqLoBCf7jXiQA/exec`

## Auth — shared secret
This endpoint is deployed with **Access: Anyone**, because a static site (no login) has to be able to call it — that setting alone gives no real access control. The actual gate is `SHARED_SECRET` in `Code.gs`, which must match `PHASE2_SECRET` in `par-ordering/index.html`. Every request without the correct secret is rejected with a 401 and logged to the **Error Log** tab.

**Note on CORS:** Apps Script always adds a permissive `Access-Control-Allow-Origin` header itself for "Anyone" deployments — this cannot be restricted from within `Code.gs` (there used to be a `corsHeaders()` function here that implied otherwise; it was dead code, never wired into the response, and has been removed). The shared secret — not CORS — is what actually blocks unauthorized writes.

### Rotating the secret
1. Generate a new random value, e.g. `openssl rand -hex 24`.
2. Update `SHARED_SECRET` in `Code.gs`.
3. Update `PHASE2_SECRET` in `par-ordering/index.html` to match.
4. Redeploy (see below) and push the `index.html` change.

## Re-deploying after code changes
- Go to Deploy → Manage deployments
- Click the pencil (edit) on your deployment
- Change version to **New version**
- Click Deploy

## API reference

### POST /exec — Write on-hand entry
```json
{
  "action": "writeOnHand",
  "secret": "<SHARED_SECRET>",
  "company_id": "gid://shopify/Company/123",
  "company_name": "Daily Harvest",
  "location_id": "gid://shopify/CompanyLocation/456",
  "location_name": "Daily Harvest",
  "sku": "HSE501",
  "on_hand_qty": 3
}
```
Response: `{ "ok": true, "fiscal_week": "FW23", "entered_at": "2026-06-05T..." }`
Missing/incorrect secret: `401` `{ "error": "Unauthorized" }`

### GET /exec?company_id=...&location_id=...&secret=... — Get usage summary
Response:
```json
{
  "ok": true,
  "items": [
    {
      "sku": "HSE501",
      "avg4w": 9.5,
      "avg12w": 9.1,
      "last_on_hand_date": "2026-06-05",
      "last_on_hand_qty": 3,
      "weeks_since": 0,
      "suggested_order": 7,
      "avg_ewma": 10.2,
      "trend_slope": 0.8
    }
  ]
}
```
`avg_ewma` and `trend_slope` are informational only for now — `suggested_order` still derives from `avg4w`. `avg_ewma` (exponentially weighted, ~3-week half-life) reacts to a usage change faster than `avg4w`/`avg12w`; `trend_slope` is bags/week change over the last 3 weeks (positive = ramping up, negative = sliding down). Once these prove out against real accounts, `suggested_order` can switch to `avg_ewma`.

**One-time sheet change required:** the Usage Summary tab needs two new header columns — **L: `avg_ewma`**, **M: `trend_slope`** — added after the existing `suggested_order` column, or the new values will land in unlabeled columns.

## Stale / expiry logic
- `weeks_since` ≥ 2 → stale (PAR tool shows amber badge)
- `weeks_since` ≥ 4 → expired (PAR tool falls back to order-inferred avg)

## Error Log
`doPost`/`doGet` log two things to an auto-created **Error Log** tab (Timestamp, Context, Error Message, Payload): any 401 (bad/missing secret) and any unhandled exception (500). This is the durable record for failures that used to only show up as a browser-console line or a 3-second "✗ Error" flash in the PAR tool UI. It does **not** catch failures where the request never reaches the server at all (e.g. the browser is offline) — those still only surface client-side.
