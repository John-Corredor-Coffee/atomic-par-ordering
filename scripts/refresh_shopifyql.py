#!/usr/bin/env python3
"""
PAR Ordering — ShopifyQL Refresh
Replaces the 36-page GraphQL pagination pipeline with 5 fast ShopifyQL calls.
Writes data.json in the same format as agg.py --finalize.

Setup:
  Same .env as refresh_data.py — SHOPIFY_DOMAIN + SHOPIFY_ACCESS_TOKEN
  pip install requests python-dotenv   (python-dotenv optional)

Usage:
  python3 scripts/refresh_shopifyql.py
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from pathlib import Path

import requests

# ── ENV ────────────────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / '.env')
except ImportError:
    pass

SHOPIFY_DOMAIN       = os.environ.get('SHOPIFY_DOMAIN', '').strip()
SHOPIFY_ACCESS_TOKEN = os.environ.get('SHOPIFY_ACCESS_TOKEN', '').strip()

if not SHOPIFY_DOMAIN or not SHOPIFY_ACCESS_TOKEN:
    sys.exit(
        "\nError: Missing Shopify credentials.\n"
        "  Set SHOPIFY_DOMAIN and SHOPIFY_ACCESS_TOKEN in scripts/.env\n"
    )

GQL_ENDPOINT = f"https://{SHOPIFY_DOMAIN}/admin/api/2024-10/graphql.json"
HEADERS = {
    'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN,
    'Content-Type': 'application/json',
}

# ── WINDOWS ────────────────────────────────────────────────────────────────────
TODAY       = datetime.now(timezone.utc)
WINDOW_DAYS = 60   # for avgDaily denominator

# ── SKIP LOGIC (mirrors agg.py) ────────────────────────────────────────────────
INTERNAL_NAMES = {'Atomic Coffee Roasters (Internal)'}

SKIP_PREFIXES = (
    'CYL-', 'KEGERATOR', 'HANDLE01', 'TAP01', 'COUPLER', 'REG01', 'SPOUT01',
    'TAMP', 'PALLO', 'GRNDZ', 'CFZA', 'RINZA', 'SHOTS', 'BRWTG', 'KNOCK',
    'FIL0', 'SLEEVES', 'SERVING', 'COPACK', 'SMITH', 'NS-',
    'TECH-', 'THIRDPARTY', 'PL-', 'FET-', 'SPACEH', 'WMFC',
    'AMOJU', 'BALINAT', 'KOMBUCHA', 'BOOCH', 'DCF-CANS',
    'LOUD-CANS', 'CANS0', 'CANS1', 'CANS2',
)
SKIP_SUFFIXES = ('-S', '-GC', '-D')
SKIP_FRAGS    = (
    'sample', 'screwdriver', 'knockbox', 'tamping mat', 'grindminder', 'brush',
    'barista basics', 'nitrogen', 'regulator', 'kegerator', 'tap handle',
    'spout', 'tech service', 'tech travel', 'technician', 'filter cartridge',
    'filter head', 'tubing', 'adapter', 'compression', 'hoodie',
    'cafiza', 'rinza', 'john guest', 'polyethylene', 'everpure',
    'pour-', 'frothing pitcher', 'wmf clean',
)

CONSUMABLE_SKUS = {
    'HSE501', 'HSE201', 'BV501', 'BV201', 'RKT501', 'RKT201', 'COS501', 'COS201',
    'INT501', 'INT201', 'DCF501', 'DCF201', 'DSL501', 'DSL201', 'CB501', 'CB201',
    'CAB501', 'CAB201', 'COL501', 'COL201', 'MAG501', 'MAG201', 'LOUD501',
    'HSE1201-RC', 'BV1201-RC', 'RKT1201-RC', 'COS1201-RC', 'INT1201-RC',
    'DCF1201-RC', 'DSL1201-RC', 'CB1201-RC', 'CAB1201-RC', 'COL1201-RC',
    'MAG1201-RC', 'LOUD1201-RC', 'GEDEO1201-RC', 'AMOJU1201-RC',
    'CONC-BIB1', 'CBKEG01', 'CBKEG02', 'CBK01-S', 'CBK02-S', 'JPKEG',
    'CANS01', 'CANS02', 'LOUD-CANS', 'DCF-CANS',
    'RKTPP-3.5', 'RKTPP-5', 'DCFPP-3.5', 'DCFPP-2.5',
    'MF-CHAI', 'MF-OAT', 'SMITH-WS-PMTCH',
}


def skip_sku(sku: str, name: str) -> bool:
    if not sku:
        return True
    if sku in CONSUMABLE_SKUS:
        return False
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
    up = (sku or '').upper()
    nl = name.lower()
    if up.endswith('501') or '- 5lb' in nl:
        return ('lbs', 'bag', 5)
    if up.endswith('201') and not up.endswith('201-D'):
        return ('lbs', 'bag', 2)
    if up.endswith('-LM') or 'local market case' in nl:
        return ('lbs', 'case', 12)
    if up.endswith('-RC') or 'retail case' in nl:
        return ('lbs', 'case', 4.5)
    if up.startswith('CONC-'):
        return ('boxes', 'case', 2)
    if up.startswith('CBKEG') or up.startswith('CBK0') or up.startswith('JPKEG'):
        return ('kegs', 'keg', 1)
    if 'CANS' in up or up.endswith('-CANS'):
        return ('cans', 'case', 12)
    if 'PP-' in up or 'RKTPP' in up or 'DCFPP' in up:
        return ('boxes', 'box', 1)
    if 'MF-CHAI' in up or ('CHAI' in up and 'MF' in up):
        return ('cartons', 'case', 4)
    if 'OAT' in up:
        return ('cartons', 'case', 6)
    if 'PMTCH' in up or 'MATCHA' in up:
        return ('tins', 'case', 12)
    return ('units', 'unit', 1)


# ── DELIVERY DAY LOOKUP ────────────────────────────────────────────────────────
def load_delivery_days() -> dict:
    """
    Build (company_name, location_name) → delivery_day from existing data.json.
    Falls back to agg_state.json if data.json isn't available.
    Returns empty dict if neither exists — callers default to 'wednesday'.
    """
    lookup = {}

    data_file = Path(__file__).parent.parent / 'data.json'
    if data_file.exists():
        payload = json.loads(data_file.read_text())
        for company in payload.get('companies', []):
            cname = company.get('name', '')
            for loc in company.get('locations', []):
                key = (cname, loc.get('name', ''))
                lookup[key] = loc.get('deliveryDay', 'wednesday')
        return lookup

    state_file = Path(__file__).parent / 'agg_state.json'
    if state_file.exists():
        state = json.loads(state_file.read_text())
        for entry in state.values():
            key = (entry.get('company_name', ''), entry.get('location_name', ''))
            lookup[key] = entry.get('delivery_day') or 'wednesday'

    return lookup


# ── SHOPIFYQL ──────────────────────────────────────────────────────────────────
SHOPIFYQL_GQL = """
query RunShopifyQL($q: String!) {
  shopifyqlQuery(query: $q) {
    ... on TableResponse {
      tableData {
        rowData
        columns { name dataType }
      }
    }
    ... on ParseError {
      parseErrorMessage
    }
  }
}
"""


def run_shopifyql(sql: str) -> list[dict]:
    """Execute a ShopifyQL query; return list of row dicts."""
    r = requests.post(
        GQL_ENDPOINT,
        headers=HEADERS,
        json={'query': SHOPIFYQL_GQL, 'variables': {'q': sql}},
        timeout=60,
    )
    r.raise_for_status()
    payload = r.json()
    if 'errors' in payload:
        raise RuntimeError(f"GraphQL errors: {payload['errors']}")

    result = payload['data']['shopifyqlQuery']
    if 'parseErrorMessage' in result:
        raise RuntimeError(f"ShopifyQL parse error: {result['parseErrorMessage']}")

    table = result['tableData']
    cols  = [c['name'] for c in table['columns']]
    return [dict(zip(cols, row)) for row in table['rowData']]


def fetch_window(since: str, until: str, label: str) -> list[dict]:
    """Fetch quantity_ordered per (company, location, sku, title) for a date range."""
    query = (
        "FROM sales "
        "SHOW quantity_ordered "
        "GROUP BY company_name, company_location_name, product_variant_sku, product_title "
        f"SINCE {since} UNTIL {until} "
        "WHERE company_name != '' "
        "ORDER BY quantity_ordered DESC "
        "LIMIT 5000"
    )
    print(f"  [{label}] {since} → {until} ...", end=' ', flush=True)
    rows = run_shopifyql(query)
    print(f"{len(rows)} rows")
    return rows


def fetch_last_orders() -> list[dict]:
    """
    Fetch one row per (company, location, sku, day) over the last 60 days,
    ordered day DESC so the first hit per key = most recent order date and qty.
    """
    query = (
        "FROM sales "
        "SHOW quantity_ordered, day "
        "GROUP BY company_name, company_location_name, product_variant_sku, product_title, day "
        "SINCE -60d UNTIL today "
        "WHERE company_name != '' "
        "ORDER BY day DESC "
        "LIMIT 5000"
    )
    print("  [last order] -60d → today ...", end=' ', flush=True)
    rows = run_shopifyql(query)
    print(f"{len(rows)} rows")
    return rows


def fetch_ly_w3l() -> list[dict]:
    """Fetch quantity_ordered for the LY same-period window (-386d to -365d, matches w3l)."""
    query = (
        "FROM sales "
        "SHOW quantity_ordered "
        "GROUP BY company_name, company_location_name, product_variant_sku, product_title "
        "SINCE -386d UNTIL -365d "
        "WHERE company_name != '' "
        "ORDER BY quantity_ordered DESC "
        "LIMIT 5000"
    )
    print("  [ly_w3l]     -386d → -365d ...", end=' ', flush=True)
    rows = run_shopifyql(query)
    print(f"{len(rows)} rows")
    return rows


def fetch_ly_w3p() -> list[dict]:
    """Fetch quantity_ordered for the LY prior-period window (-407d to -386d, matches w3p)."""
    query = (
        "FROM sales "
        "SHOW quantity_ordered "
        "GROUP BY company_name, company_location_name, product_variant_sku, product_title "
        "SINCE -407d UNTIL -386d "
        "WHERE company_name != '' "
        "ORDER BY quantity_ordered DESC "
        "LIMIT 5000"
    )
    print("  [ly_w3p]     -407d → -386d ...", end=' ', flush=True)
    rows = run_shopifyql(query)
    print(f"{len(rows)} rows")
    return rows


def fetch_w7l() -> list[dict]:
    """Fetch quantity_ordered for the last 7 weeks (-49d to today)."""
    return fetch_window('-49d', 'today', 'last 7 wks (w7l) ')


def fetch_w7p() -> list[dict]:
    """Fetch quantity_ordered for the prior 7 weeks (-98d to -49d)."""
    return fetch_window('-98d', '-49d', 'prior 7 wks (w7p)')


def fetch_ly_w7l() -> list[dict]:
    """Fetch quantity_ordered for the LY same 49-day window (-414d to -365d, matches w7l)."""
    return fetch_window('-414d', '-365d', 'LY last 7 wks    ')


def fetch_ly_w7p() -> list[dict]:
    """Fetch quantity_ordered for the LY prior 49-day window (-463d to -414d, matches w7p)."""
    return fetch_window('-463d', '-414d', 'LY prior 7 wks   ')


def build_last_lookup(rows: list[dict]) -> dict:
    """
    Build {(company, location, sku): (date_str, qty)} from last-order query rows.
    Rows must be sorted ORDER BY day DESC — first hit per key = most recent date.
    """
    lookup = {}
    for row in rows:
        cname = (row.get('company_name') or '').strip()
        lname = (row.get('company_location_name') or '').strip()
        sku   = (row.get('product_variant_sku') or '').strip()
        name  = (row.get('product_title') or '').strip()
        date_str = (row.get('day') or '').strip()
        try:
            qty = int(float(row.get('quantity_ordered') or 0))
        except (ValueError, TypeError):
            qty = 0
        if not cname or cname in INTERNAL_NAMES or not sku or qty == 0 or not date_str:
            continue
        if skip_sku(sku, name):
            continue
        key = (cname, lname, sku)
        if key not in lookup:
            lookup[key] = (date_str, qty)
    return lookup




# ── AGGREGATE ──────────────────────────────────────────────────────────────────
def aggregate(rows_60d, rows_w3l, rows_w3p, rows_ly_w3l, rows_ly_w3p,
              rows_w7l, rows_w7p, rows_ly_w7l, rows_ly_w7p, delivery_days: dict) -> dict:
    """
    Merge 9 sets of ShopifyQL rows into per-location, per-SKU buckets.
    Returns dict keyed by (company_name, location_name).
    """
    locs = {}

    def ensure_loc(cname, lname):
        key = (cname, lname)
        if key not in locs:
            locs[key] = {
                'company_name':  cname,
                'location_name': lname,
                'delivery_day':  delivery_days.get(key, 'wednesday'),
                'skus': {},
            }
        return locs[key]

    def ensure_sku(loc, sku, name):
        if sku not in loc['skus']:
            loc['skus'][sku] = {'name': name, 'qty60': 0,
                                'qw3l': 0, 'qw3p': 0, 'qly_w3l': 0, 'qly_w3p': 0,
                                'qw7l': 0, 'qw7p': 0, 'qly_w7l': 0, 'qly_w7p': 0}
        elif not loc['skus'][sku]['name']:
            loc['skus'][sku]['name'] = name
        return loc['skus'][sku]

    for field, rows in [('qty60', rows_60d), ('qw3l', rows_w3l), ('qw3p', rows_w3p),
                        ('qly_w3l', rows_ly_w3l), ('qly_w3p', rows_ly_w3p),
                        ('qw7l', rows_w7l), ('qw7p', rows_w7p),
                        ('qly_w7l', rows_ly_w7l), ('qly_w7p', rows_ly_w7p)]:
        for row in rows:
            cname = (row.get('company_name') or '').strip()
            lname = (row.get('company_location_name') or '').strip()
            sku   = (row.get('product_variant_sku') or '').strip()
            name  = (row.get('product_title') or '').strip()
            qty   = int(row.get('quantity_ordered') or 0)
            if not cname or cname in INTERNAL_NAMES or not sku or qty == 0:
                continue
            if skip_sku(sku, name):
                continue
            loc = ensure_loc(cname, lname)
            sk  = ensure_sku(loc, sku, name)
            sk[field] += qty

    return locs


# ── BUILD JSON ─────────────────────────────────────────────────────────────────
ITEM_ORDER = {'lbs': 0, 'boxes': 1, 'kegs': 2, 'cans': 3, 'cartons': 4, 'tins': 5}


def build_json(locs: dict, last_lookup: dict) -> list:
    by_company = defaultdict(lambda: {'name': None, 'locations': []})

    for (cname, lname), loc in locs.items():
        items = []
        for sku, sk in loc['skus'].items():
            if sk['qty60'] == 0:
                continue
            avg_daily  = round(sk['qty60'] / WINDOW_DAYS, 3)
            w3l        = sk['qw3l'];    w3p     = sk['qw3p']
            ly_w3l     = sk['qly_w3l']; ly_w3p  = sk['qly_w3p']
            w7l        = sk['qw7l'];    w7p     = sk['qw7p']
            ly_w7l     = sk['qly_w7l']; ly_w7p  = sk['qly_w7p']
            w3         = round(w3l  / w3p    - 1, 3) if w3p    > 0 else 0.0
            ly_w3      = round(ly_w3l / ly_w3p - 1, 3) if ly_w3p > 0 else 0.0
            w7         = round(w7l  / w7p    - 1, 3) if w7p    > 0 else 0.0
            ly_w7      = round(ly_w7l / ly_w7p - 1, 3) if ly_w7p > 0 else 0.0
            _yoy_valid = ly_w3l >= 1 and (w3l == 0 or ly_w3l / w3l >= 0.10)
            yoy        = round((w3l - ly_w3l) / ly_w3l, 3) if _yoy_valid else 0.0
            u, o, upo  = classify_sku(sku, sk['name'])
            last_date, last_qty = last_lookup.get((cname, lname, sku), ('', 0))
            if not last_date:
                if w3l > 0:
                    last_date = (TODAY - timedelta(days=10)).strftime('%Y-%m-%d')
                elif sk['qw3p'] > 0:
                    last_date = (TODAY - timedelta(days=31)).strftime('%Y-%m-%d')
                elif sk['qty60'] > 0:
                    last_date = (TODAY - timedelta(days=51)).strftime('%Y-%m-%d')
            items.append({
                'sku':           sku,
                'name':          sk['name'],
                'usageUnit':     u,
                'orderUnit':     o,
                'unitsPerOrder': upo,
                'avgDaily':      avg_daily,
                'yoy':           yoy,
                'w3':            w3,
                'ly_w3':         ly_w3,
                'w7':            w7,
                'ly_w7':         ly_w7,
                'qw3l':          w3l,
                'qly_w3l':       ly_w3l,
                'lastOrderDate': last_date,
                'lastOnHand':    last_qty,
            })

        if not items:
            continue

        items.sort(key=lambda i: (ITEM_ORDER.get(i['usageUnit'], 9), i['name']))

        c = by_company[cname]
        c['name'] = cname
        c['locations'].append({
            'id':          len(c['locations']),
            'name':        lname,
            'deliveryDay': loc['delivery_day'] or 'wednesday',
            'daysOpen':    7,
            'safetyDays':  3,
            'items':       items,
        })

    companies = []
    for idx, c in enumerate(sorted(by_company.values(), key=lambda x: (x['name'] or '').lower())):
        if c['locations']:
            companies.append({'id': idx, 'name': c['name'], 'locations': c['locations']})

    companies.append({
        'id':         len(companies),
        'name':       'Atomic Coffee Roasters (Internal)',
        'isInternal': True,
        'locations':  [],
    })

    return companies


# ── MAIN ───────────────────────────────────────────────────────────────────────
def main():
    print(f"PAR Refresh — ShopifyQL  ({TODAY.strftime('%Y-%m-%d')})")
    print()

    # Load delivery day lookup from existing data.json / agg_state.json
    delivery_days = load_delivery_days()
    print(f"Delivery day lookup: {len(delivery_days)} known locations")
    print()

    # 10 ShopifyQL calls replace 36 pages of GraphQL pagination
    print("Fetching ShopifyQL windows...")
    rows_60d    = fetch_window('-60d',  'today', '60d window (qty60)')
    rows_w3l    = fetch_window('-21d',  'today', 'last 3 wks (w3l) ')
    rows_w3p    = fetch_window('-42d',  '-21d',  'prior 3 wks (w3p)')
    rows_last   = fetch_last_orders()
    rows_ly_w3l = fetch_ly_w3l()
    rows_ly_w3p = fetch_ly_w3p()
    rows_w7l    = fetch_w7l()
    rows_w7p    = fetch_w7p()
    rows_ly_w7l = fetch_ly_w7l()
    rows_ly_w7p = fetch_ly_w7p()
    print()

    print("Building lookups...")
    last_lookup = build_last_lookup(rows_last)
    print(f"  Last-order: {len(last_lookup)} SKUs")
    print()

    print("Aggregating...")
    locs = aggregate(rows_60d, rows_w3l, rows_w3p, rows_ly_w3l, rows_ly_w3p,
                     rows_w7l, rows_w7p, rows_ly_w7l, rows_ly_w7p, delivery_days)
    print(f"  {len(locs)} locations, {sum(len(l['skus']) for l in locs.values())} SKUs")

    print("Building data.json...")
    companies = build_json(locs, last_lookup)

    out = Path(__file__).parent.parent / 'data.json'
    payload = {
        'generated':  TODAY.isoformat(),
        'windowDays': WINDOW_DAYS,
        'companies':  companies,
    }
    out.write_text(json.dumps(payload, indent=2, default=str))

    active = len([c for c in companies if not c.get('isInternal')])
    locs_count = sum(len(c['locations']) for c in companies)
    print(f"\ndata.json written — {active} companies, {locs_count} locations")
    print(f"File: {out}")


if __name__ == '__main__':
    main()
