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
