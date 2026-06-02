#!/usr/bin/env python3
"""
PAR Ordering — Shopify Data Refresh
Fetches the last 60 days of B2B orders and writes data.json for the frontend.

Setup:
  cp scripts/.env.example scripts/.env
  # Edit .env and add your Shopify credentials
  pip install requests python-dotenv
  python3 scripts/refresh_data.py

Shopify custom app needs: read_orders, read_customers
For orders older than 60 days, also add: read_all_orders
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from pathlib import Path

import requests

# ── ENV ────────────────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / '.env')
except ImportError:
    pass  # python-dotenv is optional

SHOPIFY_DOMAIN = os.environ.get('SHOPIFY_DOMAIN', '').strip()
SHOPIFY_ACCESS_TOKEN = os.environ.get('SHOPIFY_ACCESS_TOKEN', '').strip()

if not SHOPIFY_DOMAIN or not SHOPIFY_ACCESS_TOKEN:
    sys.exit(
        "\nError: Missing Shopify credentials.\n"
        "  Set SHOPIFY_DOMAIN and SHOPIFY_ACCESS_TOKEN in scripts/.env\n"
        "  See scripts/.env.example for details.\n"
    )

GQL_ENDPOINT = f"https://{SHOPIFY_DOMAIN}/admin/api/2024-10/graphql.json"
HEADERS = {
    'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN,
    'Content-Type': 'application/json',
}

# ── WINDOW ─────────────────────────────────────────────────────────────────────
WINDOW_DAYS   = 60
TODAY         = datetime.now(timezone.utc)
WINDOW_START  = TODAY - timedelta(days=WINDOW_DAYS)
W3_LAST_START = TODAY - timedelta(days=21)   # last 3 weeks
W3_PRIOR_END  = TODAY - timedelta(days=21)
W3_PRIOR_START = TODAY - timedelta(days=42)  # prior 3 weeks

# ── CONSTANTS ──────────────────────────────────────────────────────────────────
DELIVERY_DAYS = {'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'}
INTERNAL_NAMES = {'Atomic Coffee Roasters (Internal)'}

# One-time purchases to exclude from PAR tracking
SKIP_PREFIXES = (
    'CYL-', 'KEGERATOR', 'HANDLE01', 'TAP01', 'COUPLER', 'REG01', 'SPOUT01',
    'TAMP', 'SCREW', 'PALLO', 'GRNDZ', 'CFZA', 'RINZA', 'SHOTS',
    'BRWTG', 'KNOCK', 'FIL0', 'SLEEVES', 'SERVING', 'COPACK', 'SMITH',
    'NS-', 'JP-', 'BALINAT', 'KOMBUCHA', 'BOOCH',
)
SKIP_SUFFIXES = ('-S', '-GC', '-LM', '-D')   # samples, gift cards, local mkt, distributor
SKIP_FRAGS    = ('sample', 'screwdriver', 'knockbox', 'tamping mat', 'grindminder',
                 'brush', 'barista basics', 'nitrogen cylinder', 'nitrogen regulator',
                 'tap tower', 'faucet', 'sankey', 'coupler', 'regulator', 'kegerator',
                 'tap handle', 'spout')


def skip_sku(sku: str, name: str) -> bool:
    if not sku:
        return True
    up = sku.upper()
    for p in SKIP_PREFIXES:
        if up.startswith(p.upper()):
            return True
    for s in SKIP_SUFFIXES:
        if up.endswith(s.upper()):
            return True
    nl = name.lower()
    for f in SKIP_FRAGS:
        if f in nl:
            return True
    return False


def classify_sku(sku: str, name: str) -> tuple:
    """Return (usageUnit, orderUnit, unitsPerOrder)."""
    up  = (sku or '').upper()
    nl  = name.lower()

    # 5lb coffee bags
    if up.endswith('501') or '- 5lb' in nl:
        return ('lbs', 'bag', 5)
    # 2lb coffee bags
    if up.endswith('201') and not up.endswith('201-D'):
        return ('lbs', 'bag', 2)
    # 12-count retail cases of 12oz bags (72oz ≈ 4.5 lbs each)
    if up.endswith('-RC') or 'retail case' in nl:
        return ('lbs', 'case', 4.5)
    # Cold brew concentrate bag-in-box
    if up.startswith('CONC-'):
        return ('boxes', 'box', 1)
    # Cold brew kegs
    if up.startswith('CBKEG') or up.startswith('CBK0') or up.startswith('JPKEG'):
        return ('kegs', 'keg', 1)
    # Cold brew / nitro cans (sold as cases of 12)
    if 'CANS' in up or up.endswith('-CANS'):
        return ('cans', 'case', 12)
    # Portion packs
    if 'PP-' in up or up.startswith('RKTPP') or up.startswith('DCFPP'):
        return ('units', 'case', 10)
    # Minor Figures chai (sold by the case of 4)
    if 'MF-CHAI' in up or 'CHAI' in up:
        return ('cartons', 'case', 4)
    # Oat milk (case of 6)
    if 'OAT' in up:
        return ('cartons', 'case', 6)
    # Matcha tins
    if 'PMTCH' in up or 'MATCHA' in up:
        return ('tins', 'case', 12)

    return ('units', 'unit', 1)


def extract_delivery_day(tags) -> str:
    for tag in (tags or []):
        if tag.lower() in DELIVERY_DAYS:
            return tag.lower()
    return 'wednesday'


# ── GRAPHQL ────────────────────────────────────────────────────────────────────
ORDER_QUERY = """
query FetchOrders($after: String) {
  orders(
    first: 50,
    after: $after,
    query: "created_at:>={window_start} channel_type:b2b"
  ) {
    edges {
      node {
        processedAt
        customer { tags }
        purchasingEntity {
          ... on PurchasingCompany {
            company { id name }
            location { id name }
          }
        }
        lineItems(first: 30) {
          edges { node { sku name quantity } }
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
""".replace('{window_start}', WINDOW_START.strftime('%Y-%m-%d'))


def gql(query: str, variables: dict = None) -> dict:
    r = requests.post(
        GQL_ENDPOINT,
        headers=HEADERS,
        json={'query': query, 'variables': variables or {}},
        timeout=30,
    )
    r.raise_for_status()
    payload = r.json()
    if 'errors' in payload:
        raise RuntimeError(f"GraphQL errors: {payload['errors']}")
    return payload['data']


def fetch_all_orders() -> list:
    orders, cursor, page = [], None, 0
    while True:
        page += 1
        print(f"  Page {page}...", end=' ', flush=True)
        data  = gql(ORDER_QUERY, {'after': cursor})
        edges = data['orders']['edges']
        for e in edges:
            node = e['node']
            if node.get('purchasingEntity'):
                orders.append(node)
        pi = data['orders']['pageInfo']
        print(f"{len(edges)} orders")
        if not pi['hasNextPage']:
            break
        cursor = pi['endCursor']
    return orders


# ── COMPUTE ────────────────────────────────────────────────────────────────────
def compute(orders: list) -> dict:
    """Aggregate per-location, per-SKU metrics from raw order list."""
    locs = defaultdict(lambda: {
        'company_id': None, 'company_name': None,
        'location_id': None, 'location_name': None,
        'delivery_day': None,
        'skus': defaultdict(lambda: {
            'qty60': 0, 'qty_w3_last': 0, 'qty_w3_prior': 0,
            'last_date': None, 'name': '',
        }),
    })

    for order in orders:
        pe = order.get('purchasingEntity') or {}
        company  = pe.get('company') or {}
        location = pe.get('location') or {}
        if not company or not location:
            continue
        if company.get('name') in INTERNAL_NAMES:
            continue

        cid  = company['id']
        lid  = location['id']
        key  = f"{cid}|{lid}"
        tags = (order.get('customer') or {}).get('tags') or []
        ts   = datetime.fromisoformat(order['processedAt'].replace('Z', '+00:00'))

        loc = locs[key]
        loc['company_id']   = cid
        loc['company_name'] = company['name']
        loc['location_id']  = lid
        loc['location_name'] = location['name']
        if loc['delivery_day'] is None:
            loc['delivery_day'] = extract_delivery_day(tags)

        for ie in order['lineItems']['edges']:
            item = ie['node']
            sku  = item.get('sku') or ''
            name = item.get('name') or ''
            qty  = item.get('quantity') or 0

            if skip_sku(sku, name) or qty == 0:
                continue

            sk = loc['skus'][sku]
            sk['name']   = sk['name'] or name
            sk['qty60'] += qty

            if ts >= W3_LAST_START:
                sk['qty_w3_last'] += qty
            elif ts >= W3_PRIOR_START:
                sk['qty_w3_prior'] += qty

            if sk['last_date'] is None or ts > sk['last_date']:
                sk['last_date'] = ts

    return locs


# ── BUILD JSON ─────────────────────────────────────────────────────────────────
ITEM_ORDER = {'lbs': 0, 'boxes': 1, 'kegs': 2, 'cans': 3, 'cartons': 4, 'tins': 5}


def build_json(locs: dict) -> list:
    by_company = defaultdict(lambda: {'name': None, 'locations': []})

    for loc in locs.values():
        cid   = loc['company_id']
        items = []

        for sku, sk in loc['skus'].items():
            if sk['qty60'] == 0:
                continue

            avg_daily = round(sk['qty60'] / WINDOW_DAYS, 3)

            w3_last  = sk['qty_w3_last']  / 3.0   # weekly avg, last 3 wks
            w3_prior = sk['qty_w3_prior'] / 3.0   # weekly avg, prior 3 wks
            if w3_prior > 0:
                w3 = round(w3_last / w3_prior - 1, 3)
            elif w3_last > 0:
                w3 = 0.0   # no prior data; treat as flat
            else:
                w3 = 0.0

            last_date_str = sk['last_date'].strftime('%Y-%m-%d') if sk['last_date'] else None
            u_unit, o_unit, upo = classify_sku(sku, sk['name'])

            items.append({
                'sku':          sku,
                'name':         sk['name'],
                'usageUnit':    u_unit,
                'orderUnit':    o_unit,
                'unitsPerOrder': upo,
                'avgDaily':     avg_daily,
                'yoy':          0,          # not available; needs read_all_orders scope
                'w3':           w3,
                'lastOrderDate': last_date_str,
                'lastOnHand':   0,          # default; clients edit in UI
            })

        if not items:
            continue

        items.sort(key=lambda i: (ITEM_ORDER.get(i['usageUnit'], 9), i['name']))

        c = by_company[cid]
        c['name'] = loc['company_name']
        c['locations'].append({
            'id':          len(c['locations']),
            'name':        loc['location_name'],
            'deliveryDay': loc['delivery_day'] or 'wednesday',
            'daysOpen':    6,
            'safetyDays':  3,
            'items':       items,
        })

    companies = []
    for idx, c in enumerate(sorted(by_company.values(), key=lambda x: (x['name'] or '').lower())):
        if c['locations']:
            companies.append({'id': idx, 'name': c['name'], 'locations': c['locations']})

    # Always append Atomic internal entry last
    companies.append({
        'id': len(companies),
        'name': 'Atomic Coffee Roasters (Internal)',
        'isInternal': True,
        'locations': [],
    })

    return companies


# ── MAIN ───────────────────────────────────────────────────────────────────────
def main():
    print(f"Window: {WINDOW_START.strftime('%Y-%m-%d')} → {TODAY.strftime('%Y-%m-%d')} ({WINDOW_DAYS} days)")
    print("Fetching orders...")
    orders = fetch_all_orders()
    print(f"  Total B2B orders: {len(orders)}")

    print("Computing metrics...")
    locs     = compute(orders)
    companies = build_json(locs)

    out = Path(__file__).parent.parent / 'data.json'
    payload = {
        'generated':  TODAY.isoformat(),
        'windowDays': WINDOW_DAYS,
        'companies':  companies,
    }
    out.write_text(json.dumps(payload, indent=2, default=str))

    active = len([c for c in companies if not c.get('isInternal')])
    print(f"Written: {out}")
    print(f"  Companies: {active}  |  Locations: {sum(len(c['locations']) for c in companies)}")


if __name__ == '__main__':
    main()
