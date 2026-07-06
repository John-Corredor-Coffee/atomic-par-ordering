# PAR Ordering Tool — Handoff for Spencer

I built a PAR ordering tool for wholesale accounts — I need you to host it on serveatomiccoffee.com once the repo is moved to a shared Atomic account.

## What I need you to do
1. Confirm the destination URL/path on serveatomiccoffee.com (e.g. `/par` or `/wholesale/par`)
2. Wait for me to transfer the repo from `John-Corredor-Coffee/atomic-par-ordering` to the shared Atomic GitHub account
3. Once transferred, host the static `index.html` at the agreed path on serveatomiccoffee.com
4. Confirm the live URL so I can share it with wholesale accounts

## Questions for you
1. What path should this live at on serveatomiccoffee.com?

## Assumptions I made (push back if any are wrong)
- The tool will be hosted as a static file for now; the Shopify backend doesn't exist yet — I'm building that separately once I get time with Brendan
- The repo transfer happens before you set up hosting (I'll let you know when it's ready)
- No QR code or printed materials point to the current GitHub Pages URL, so the cutover is clean

## What was built
**Live (current):** https://john-corredor-coffee.github.io/atomic-par-ordering/
**Repo:** https://github.com/John-Corredor-Coffee/atomic-par-ordering (needs to move to shared Atomic account)
**File:** single self-contained `index.html` — no build step, no dependencies to install

**What it does:** Shows each wholesale account their recommended order quantities based on average daily usage, delivery day, and a safety stock buffer. Three permission tiers are designed in (not yet enforced without a backend):
- **Standard** — account sees only their own data (e.g. Cafe Selah)
- **Admin** — sees all child locations (e.g. JP Licks with multiple locations)
- **Atomic internal** — sees all accounts

**Data:** Currently hardcoded. Cafe Selah has real Shopify order history (Feb 6–May 7 2026). Sodexo entries are demo estimates. Last updated: 2026-05-08. A Google Sheet is also manually copied per account for larger input from wholesale accounts — this is a parallel workflow, not wired into the tool yet.

**Backend (Phase 2):** I'm planning to build a Shopify-connected backend myself. I need a scoping session with Brendan first to check for API limitations and resolve one open question: whether Sodexo admin accounts should be restricted from seeing sibling accounts (probably yes, but TBD).

## Testing
1. Open the hosted URL — confirm the page loads and shows the account dropdown
2. Select "Cafe Selah" — confirm PAR table renders with order recommendations
3. Select "Sodexo" — confirm multi-location dropdown appears and Admin view toggle works
4. Resize to mobile — confirm table is readable

## Deadline
None specified.
