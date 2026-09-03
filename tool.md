name: par-ordering
tier: internal
hosting: cloudflare
audience: all-staff
data_isolation: na
data_source: Shopify Admin API (serve-atomic.myshopify.com) — B2B order history, read live via a Cloudflare Worker Cron Trigger; never copied anywhere but Cloudflare KV
storage: open
data_touched: wholesale order history, B2B company/location/SKU aggregates
owner: John Corredor <john.corredor@atomicroastery.com>
description: Internal staff tool showing suggested PAR reorder quantities per wholesale account/location/SKU, computed from Shopify order history.
drive_folder: https://drive.google.com/drive/folders/1HNpR0xNz6P1rslHM2udwyYRPUIHNdi4w
last_deployed: 2026-09-03 (version 0caea9f8-eca1-49a1-927f-72271ec810ad) — removed the origin-scoped
  localStorage 'acr_auth' gate that redirect-looped this host against the GitHub Pages dashboard;
  access is unchanged, still the *.acr-ops.com all-staff Cloudflare Access wall.
