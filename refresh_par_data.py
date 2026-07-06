#!/usr/bin/env python3
"""
refresh_par_data.py
Pulls 90 days of Shopify B2B orders, computes per-account/location/SKU PAR metrics,
and upserts the results as PAR Profile metaobjects in Shopify.

Usage:
    SHOPIFY_CLIENT_ID=<id> SHOPIFY_CLIENT_SECRET=<secret> python3 refresh_par_data.py

Requirements:
    pip install requests python-dateutil
"""

from __future__ import annotations
import os
import json
import math
import time
import requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from dateutil.parser import parse as parse_date

# ─── CONFIG ──────────────────────────────────────────────────────────────────

STORE_DOMAIN    = os.environ.get("SHOPIFY_STORE_DOMAIN", "serve-atomic.myshopify.com")
SHOPIFY_CLIENT_ID     = os.environ.get("SHOPIFY_CLIENT_ID", "")
SHOPIFY_CLIENT_SECRET = os.environ.get("SHOPIFY_CLIENT_SECRET", "")
API_VERSION   = "2024-10"
GRAPHQL_URL   = f"https://{STORE_DOMAIN}/admin/api/{API_VERSION}/graphql.json"

def get_access_token() -> str:
    r = requests.post(
        f"https://{STORE_DOMAIN}/admin/oauth/access_token",
        data={
            "grant_type": "client_credentials",
            "client_id": SHOPIFY_CLIENT_ID,
            "client_secret": SHOPIFY_CLIENT_SECRET,
        },
    )
    r.raise_for_status()
    return r.json()["access_token"]

# How many days of history to use for avgDaily
WINDOW_DAYS   = 90
# W3 trend: compare last N days vs prior N days
W3_DAYS       = 21

# ─── SKU EXCLUSIONS ──────────────────────────────────────────────────────────
# SKUs that start with any of these prefixes are ignored (tech, signage, etc.)
EXCLUDE_PREFIXES = (
    "TECH-", "THIRDPARTY-", "BRWTG-", "LP-POS-", "SMITH-POS",
    "CYL-", "PL-", "EL-", "EVERPURE", "WMFCLEAN", "CAFIZA",
    "RINZA", "GRINDZ", "SCALE", "TAMPER", "PITCHER", "BRUSH",
    "KNOCKBOX", "AIRPOT", "THERMOMETER", "PORTAFILTER", "GASKET",
    "SCREW", "BURR", "SINGLETRACK-POS", "ATOMIC-TT",
)
EXCLUDE_TITLES = (
    "samples", "table tent", "poster", "tee", "hoodie", "hat",
    "snapback", "crewneck", "sleeve", "mug", "cup", "pin",
    "long sleeve", "standard spout", "tap handle", "aeropress",
    "hario", "acaia", "cj4000", "aws-drip", "mahlkonig",
    "la marzocco", "nuova simonelli", "fetco", "ceado", "bunn",
    "barista basics", "pällo", "pallo", "escali", "kegerator",
    "bob", "braided", "polyethylene", "john guest", "electrical cord",
    "everpure", "parts sales tax", "miscellaneous", "proudly serving",
    "blank brew tag", "can lid", "can tray", "can glass", "diner mug",
    "good vibes", "building blocks", "wave hat", "logo dad",
    "new rules of coffee", "grinder/counter", "stubby screwdriver",
    "standard spout", "nitrogen", "nitro keg additional",
    "keg cleaning", "keg brew & fill", "toll roast",
    "green coffee (per", "12oz aluminum can", "12oz can production",
    "1 gallon bag-in-box production", "1 gallon bag-in-box packaging",
    "pressed - pb", "third-party technician",
)

# ─── UNIT MAPPING ────────────────────────────────────────────────────────────
# Maps variant_title pattern → (lbs_per_unit, display_unit)
# For null variant_title, falls back to SKU prefix lookup below.
VARIANT_LBS = {
    "5lb": (5.0, "lbs"),
    "2lb": (2.0, "lbs"),
    "1lb": (1.0, "lbs"),
    "retail case (6)": (4.5, "lbs"),   # 6 × 12oz bags ≈ 4.5 lbs
    "ground case": (6.0, "lbs"),       # 6 × 1lb ground bags
}

# SKU prefix → (units_per_order, display_unit)
SKU_UNITS = {
    "CBKEG01":   (1.0, "kegs"),
    "CBKEG02":   (1.0, "kegs"),
    "CBK01-S":   (1.0, "kegs"),
    "CBK02-S":   (1.0, "kegs"),
    "CANS01":    (1.0, "cases"),
    "CANS02":    (1.0, "cases"),
    "MF-CHAI":   (1.0, "cartons"),
    "CONC-BIB1": (1.0, "units"),
    "RKTPP-3.5": (1.0, "boxes"),
    "RKTPP-2.5": (1.0, "boxes"),
    "RKTPP-5":   (1.0, "boxes"),
    "DCFPP-3.5": (1.0, "boxes"),
    "DCFPP-2.5": (1.0, "boxes"),
    "BOOCH-BC":  (1.0, "kegs"),
    "BOOCH-RB":  (1.0, "kegs"),
    "KOMBUCHA":  (1.0, "cases"),   # prefix match for can SKUs
    "JP LICKS":  (1.0, "kegs"),
    "CBKEG":     (1.0, "kegs"),    # fallback for any keg variant
}

def resolve_units(sku: str, variant_title: str | None) -> tuple[float, str]:
    """Return (multiplier_per_unit, display_unit) for a given SKU/variant."""
    if variant_title:
        key = variant_title.lower().strip()
        if key in VARIANT_LBS:
            return VARIANT_LBS[key]
        # Portion pack: variant like "Wholesale Case (100 sachets)" — treat as 1 unit
        if "sachet" in key or "case" in key:
            return (1.0, "cases")
    # Fall back to SKU lookup
    for prefix, mapping in SKU_UNITS.items():
        if sku.upper().startswith(prefix.upper()):
            return mapping
    # Default: 1 unit if we can't resolve
    return (1.0, "units")

def should_exclude(sku: str, title: str) -> bool:
    sku_upper = (sku or "").upper()
    title_lower = (title or "").lower()
    for p in EXCLUDE_PREFIXES:
        if sku_upper.startswith(p.upper()):
            return True
    for t in EXCLUDE_TITLES:
        if t in title_lower:
            return True
    # Distributor SKUs — keep but they'll be tagged separately
    return False

# ─── GRAPHQL HELPERS ─────────────────────────────────────────────────────────

HEADERS = {
    "Content-Type": "application/json",
    "X-Shopify-Access-Token": "",  # populated at runtime via get_access_token()
}

ORDERS_QUERY = """
query PAROrders($cursor: String, $since: String!) {
  orders(
    first: 50
    after: $cursor
    query: $since
    sortKey: PROCESSED_AT
  ) {
    edges {
      node {
        processedAt
        purchasingEntity {
          ... on PurchasingCompany {
            company { name id }
            location { name }
          }
        }
        lineItems(first: 30) {
          edges {
            node {
              title
              variantTitle
              sku
              currentQuantity
            }
          }
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

def graphql(query: str, variables: dict = None) -> dict:
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    r = requests.post(GRAPHQL_URL, headers=HEADERS, json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data["data"]

def fetch_orders(since_iso: str) -> list[dict]:
    """Fetch all orders since since_iso, paginated."""
    orders = []
    cursor = None
    query_filter = f"created_at:>={since_iso} financial_status:paid"
    page = 0
    while True:
        page += 1
        print(f"  Fetching orders page {page}...", end="\r")
        data = graphql(ORDERS_QUERY, {"cursor": cursor, "since": query_filter})
        edges = data["orders"]["edges"]
        for edge in edges:
            node = edge["node"]
            pe = node.get("purchasingEntity") or {}
            if not pe.get("company"):
                continue  # skip non-B2B orders
            orders.append(node)
        page_info = data["orders"]["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]
        time.sleep(0.3)  # respect rate limits
    print(f"  Fetched {len(orders)} B2B orders.          ")
    return orders

# ─── AGGREGATION ─────────────────────────────────────────────────────────────

def aggregate(orders: list[dict], since_dt: datetime, until_dt: datetime) -> dict:
    """
    Aggregate orders into:
      key = (company_id, company_name, location_name, sku, product_title, variant_title)
      value = { total_units, last_order_date }
    Only includes orders within [since_dt, until_dt].
    """
    result = defaultdict(lambda: {"total_units": 0.0, "last_order_date": None})

    for order in orders:
        processed = parse_date(order["processedAt"]).replace(tzinfo=timezone.utc)
        if not (since_dt <= processed <= until_dt):
            continue

        pe = order["purchasingEntity"]
        company_id   = pe["company"]["id"]
        company_name = pe["company"]["name"]
        location     = pe["location"]["name"]

        for edge in order["lineItems"]["edges"]:
            item = edge["node"]
            sku   = item.get("sku") or ""
            title = item.get("title") or ""
            vtitle = item.get("variantTitle") or ""
            qty   = item.get("currentQuantity") or 0

            if should_exclude(sku, title):
                continue
            if qty <= 0:
                continue

            multiplier, unit = resolve_units(sku, vtitle)
            units = qty * multiplier

            key = (company_id, company_name, location, sku, title, vtitle, unit)
            bucket = result[key]
            bucket["total_units"] += units
            if bucket["last_order_date"] is None or processed > bucket["last_order_date"]:
                bucket["last_order_date"] = processed

    return result

# ─── METAOBJECT UPSERT ───────────────────────────────────────────────────────

UPSERT_QUERY = """
mutation UpsertPARProfile($handle: String!, $fields: [MetaobjectFieldInput!]!) {
  metaobjectUpsert(
    handle: { type: "par_profile", handle: $handle }
    metaobject: { fields: $fields }
  ) {
    metaobject { id handle }
    userErrors { field message }
  }
}
"""

def safe_handle(s: str) -> str:
    """Convert a string to a valid Shopify metaobject handle."""
    import re
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:255]

def upsert_par_profile(
    company_id: str, company_name: str, location_name: str,
    sku: str, product_title: str, variant_title: str, usage_unit: str,
    avg_daily: float, units_90d: float,
    last_order_date: datetime | None,
    yoy: float | None, w3: float | None,
    refreshed_at: datetime,
) -> dict:
    handle = safe_handle(f"{company_name}-{location_name}-{sku}")
    fields = [
        {"key": "company_id",      "value": company_id},
        {"key": "company_name",    "value": company_name},
        {"key": "location_name",   "value": location_name},
        {"key": "sku",             "value": sku},
        {"key": "product_title",   "value": product_title},
        {"key": "variant_title",   "value": variant_title or ""},
        {"key": "avg_daily",       "value": str(round(avg_daily, 4))},
        {"key": "usage_unit",      "value": usage_unit},
        {"key": "units_90d",       "value": str(round(units_90d, 2))},
        {"key": "refreshed_at",    "value": refreshed_at.strftime("%Y-%m-%dT%H:%M:%SZ")},
    ]
    if last_order_date:
        fields.append({"key": "last_order_date", "value": last_order_date.strftime("%Y-%m-%d")})
    if yoy is not None:
        fields.append({"key": "yoy", "value": str(round(yoy, 6))})
    if w3 is not None:
        fields.append({"key": "w3", "value": str(round(w3, 6))})

    data = graphql(UPSERT_QUERY, {"handle": handle, "fields": fields})
    errors = data["metaobjectUpsert"]["userErrors"]
    if errors:
        print(f"  ⚠ Upsert errors for {handle}: {errors}")
    return data["metaobjectUpsert"]["metaobject"]

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    if not SHOPIFY_CLIENT_ID or not SHOPIFY_CLIENT_SECRET:
        raise SystemExit("Set SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET env vars before running.")

    print("Fetching Shopify access token...")
    HEADERS["X-Shopify-Access-Token"] = get_access_token()

    now       = datetime.now(timezone.utc)
    since_90  = now - timedelta(days=WINDOW_DAYS)
    since_180 = now - timedelta(days=WINDOW_DAYS * 2)   # prior 90d for yoy proxy
    w3_start  = now - timedelta(days=W3_DAYS)
    w3_prior  = now - timedelta(days=W3_DAYS * 2)

    print("=== PAR Data Refresh ===")
    print(f"Window:  {since_90.date()} → {now.date()} (90 days)")

    # Fetch orders going back 180 days (covers current 90d + prior 90d for yoy)
    print("\nFetching orders (180 days)...")
    orders = fetch_orders(since_180.strftime("%Y-%m-%d"))

    print("\nAggregating — current 90d window...")
    current = aggregate(orders, since_90, now)

    print("Aggregating — prior 90d window (for yoy)...")
    prior   = aggregate(orders, since_180, since_90)

    print("Aggregating — last 21 days (w3 current)...")
    w3_curr = aggregate(orders, w3_start, now)

    print("Aggregating — prior 21 days (w3 baseline)...")
    w3_base = aggregate(orders, w3_prior, w3_start)

    # Build combined key set from current window only
    keys = set(current.keys())
    print(f"\nFound {len(keys)} active company/location/SKU combinations.")

    print("\nUpserting PAR Profile metaobjects...")
    success = 0
    for i, key in enumerate(sorted(keys)):
        company_id, company_name, location_name, sku, product_title, variant_title, usage_unit = key
        bucket = current[key]

        total_units = bucket["total_units"]
        avg_daily   = total_units / WINDOW_DAYS
        last_order  = bucket["last_order_date"]

        # YoY: compare current 90d vs prior 90d
        prior_units = prior.get(key, {}).get("total_units", 0.0)
        if prior_units > 0:
            yoy = (total_units - prior_units) / prior_units
        else:
            yoy = None

        # W3 trend
        w3c = w3_curr.get(key, {}).get("total_units", 0.0)
        w3b = w3_base.get(key, {}).get("total_units", 0.0)
        if w3b > 0:
            w3 = (w3c - w3b) / w3b
        else:
            w3 = None

        print(f"  [{i+1}/{len(keys)}] {company_name} / {location_name} / {sku}", end="\r")

        try:
            upsert_par_profile(
                company_id=company_id,
                company_name=company_name,
                location_name=location_name,
                sku=sku,
                product_title=product_title,
                variant_title=variant_title,
                usage_unit=usage_unit,
                avg_daily=avg_daily,
                units_90d=total_units,
                last_order_date=last_order,
                yoy=yoy,
                w3=w3,
                refreshed_at=now,
            )
            success += 1
        except Exception as e:
            print(f"\n  ✗ Failed {sku} @ {company_name}: {e}")

        time.sleep(0.15)  # ~6 req/sec, well within Shopify's 10/sec limit

    print(f"\n\n✓ Done — {success}/{len(keys)} PAR profiles upserted to Shopify.")
    print(f"  View at: https://{STORE_DOMAIN}/admin/content/par_profile")

if __name__ == "__main__":
    main()
