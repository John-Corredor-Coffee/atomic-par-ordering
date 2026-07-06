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
  "company_id": "gid://shopify/Company/123",
  "company_name": "Daily Harvest",
  "location_id": "gid://shopify/CompanyLocation/456",
  "location_name": "Daily Harvest",
  "sku": "HSE501",
  "on_hand_qty": 3
}
```
Response: `{ "ok": true, "fiscal_week": "FW23", "entered_at": "2026-06-05T..." }`

### GET /exec?company_id=...&location_id=... — Get usage summary
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
      "suggested_order": 7
    }
  ]
}
```

## Stale / expiry logic
- `weeks_since` ≥ 2 → stale (PAR tool shows amber badge)
- `weeks_since` ≥ 4 → expired (PAR tool falls back to order-inferred avg)
