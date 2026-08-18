#!/usr/bin/env python3
"""
PAR Ordering — Direct-API fetch (no MCP connector).

Produces the same /tmp/par_refresh/*.json files the par-refresh skill builds via
the Shopify MCP connector's 11 ShopifyQL queries + 2 GraphQL steps, but by
calling the Admin API directly with the "Order Sweep (John)" wholesale token.
Feed the output to refresh_from_mcp.py unchanged.

Why: the MCP connector holds one store's token at a time and has been
intermittently invalidated; this path has its own non-expiring read-only token
and works regardless of connector state.

How: two REST order pulls (current ~98d window + the last-year window) with
cursor pagination, joined to company/location names via one GraphQL
companyLocations pull; the 11 ShopifyQL windows are then bucketed in Python.

REST, not GraphQL search, on purpose: GraphQL `orders(query: "created_at:...")`
rides Shopify's search index, which silently omits orders with
financial_status: pending — i.e. B2B net-terms orders, the bread and butter of
this store (measured live 2026-08-11: 233 of 389 pending orders returned, and
Atomic Cafe's volume undercounted 3x). REST created_at_min/max filters the real
field and returns everything.

Fidelity vs ShopifyQL's `FROM sales` dataset:
  • quantities are GROSS ordered quantities by order date (refund/return rows
    are NOT netted out the way the sales dataset does)
  • cancelled orders and test orders are excluded entirely
  • window boundaries are inclusive on both ends, in the shop's timezone,
    matching ShopifyQL's SINCE/UNTIL behavior
For PAR purposes (relative ordering volume) this matches; if exact parity with
Shopify Analytics is ever needed, ask Brendan for `read_reports` and use
shopifyqlQuery instead.

Usage:
  python3 scripts/fetch_direct.py [--outdir /tmp/par_refresh]

Token: SHOPIFY_TOKEN_WHOLESALE from the environment, falling back to
~/.config/atomic/shopify-tokens.env. Never hardcoded, never printed.
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

DOMAIN = 'serve-atomic.myshopify.com'
API_VERSION = '2026-07'
SHOP_TZ = ZoneInfo('America/New_York')
TOKENS_FILE = Path.home() / '.config/atomic/shopify-tokens.env'

# Same catalog tags as the par-refresh skill's Step 3 merge snippet.
CATALOG_TAGS = {
    'standard-cafe', 'standard-cafe-cans-kegs', 'standard-cafe-pp-cans-kegs',
    'standard-cafe-pp-cans-sankey', 'standard-cafe-pp-cans-dlv', 'standard-cafe-pp-cans-ship',
    'standard-cafe-pp', 'standard-cafe-cans-sankey', 'standard-cafe-cans-dlv',
    'standard-cafe-cans-ship', 'local-market', 'local-market-cans',
}

# window name -> (since_days, until_days) — day-index range, inclusive both ends,
# mirroring ShopifyQL "SINCE -{since}d UNTIL -{until}d" (until=0 → today).
WINDOWS_CURRENT = {
    '90d': (90, 0),
    'w3l': (21, 0),
    'w3p': (42, 21),
    'w7l': (49, 0),
    'w7p': (98, 49),
}
WINDOWS_LY = {
    'ly_w3l': (386, 365),
    'ly_w3p': (407, 386),
    'ly_w7l': (414, 365),
    'ly_w7p': (463, 414),
}


def load_token():
    token = os.environ.get('SHOPIFY_TOKEN_WHOLESALE', '').strip()
    if not token and TOKENS_FILE.exists():
        for line in TOKENS_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith('SHOPIFY_TOKEN_WHOLESALE='):
                token = line.split('=', 1)[1].strip()
                break
    if not token:
        sys.exit(
            'Error: SHOPIFY_TOKEN_WHOLESALE not set and not found in '
            f'{TOKENS_FILE}. The token comes from the "Order Sweep (John)" app.'
        )
    return token


TOKEN = load_token()
ENDPOINT = f'https://{DOMAIN}/admin/api/{API_VERSION}/graphql.json'


def graphql(query, variables=None, max_retries=5):
    """One GraphQL call with throttle retry. Raises on GraphQL errors."""
    payload = json.dumps({'query': query, 'variables': variables or {}}).encode()
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(
            ENDPOINT, data=payload, method='POST',
            headers={'X-Shopify-Access-Token': TOKEN, 'Content-Type': 'application/json'},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            raise
        errors = data.get('errors')
        if errors:
            throttled = any(
                (e.get('extensions') or {}).get('code') == 'THROTTLED' for e in errors
            )
            if throttled and attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f'GraphQL errors: {json.dumps(errors)[:500]}')
        return data['data']
    raise RuntimeError('GraphQL: retries exhausted')


# ── Company / location name maps (GraphQL, one paginated pull) ───────────────

def fetch_company_maps():
    """REST orders carry company/location IDs only; map them to names."""
    company_names, location_names = {}, {}
    cursor = None
    while True:
        data = graphql(
            '''
            query($after: String) {
              companyLocations(first: 250, after: $after) {
                pageInfo { hasNextPage endCursor }
                edges {
                  node {
                    id
                    name
                    company { id name }
                  }
                }
              }
            }
            ''',
            {'after': cursor},
        )['companyLocations']
        gid_num = lambda gid: int(gid.rsplit('/', 1)[-1])
        for edge in data['edges']:
            node = edge['node']
            location_names[gid_num(node['id'])] = node['name']
            company_names[gid_num(node['company']['id'])] = node['company']['name']
        if not data['pageInfo']['hasNextPage']:
            break
        cursor = data['pageInfo']['endCursor']
    print(f'  {len(company_names)} companies, {len(location_names)} locations mapped', flush=True)
    return company_names, location_names


# ── REST order pull ───────────────────────────────────────────────────────────

REST_FIELDS = 'id,created_at,cancelled_at,test,company,line_items'


def resolve_variant_skus(variant_ids):
    """Map variant id → current SKU for line items whose order-time sku is empty
    (custom/draft-order items). ShopifyQL resolves these through the variant;
    without this, e.g. Pressed Cafe's keg volume lands under an empty SKU."""
    skus = {}
    ids = [f'gid://shopify/ProductVariant/{v}' for v in variant_ids]
    for i in range(0, len(ids), 250):
        nodes = graphql(
            '''
            query($ids: [ID!]!) {
              nodes(ids: $ids) { ... on ProductVariant { id sku } }
            }
            ''',
            {'ids': ids[i:i + 250]},
        )['nodes']
        for node in nodes:
            if node and node.get('sku'):
                skus[int(node['id'].rsplit('/', 1)[-1])] = node['sku']
    return skus


def rest_get(url, max_retries=5):
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(
            url, headers={'X-Shopify-Access-Token': TOKEN, 'Accept': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read()), resp.headers.get('Link') or ''
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < max_retries:
                retry_after = e.headers.get('Retry-After')
                time.sleep(float(retry_after) if retry_after else 2 ** attempt)
                continue
            raise
    raise RuntimeError('REST: retries exhausted')


def next_link(link_header):
    for part in link_header.split(', '):
        if 'rel="next"' in part:
            return part[part.index('<') + 1:part.index('>')]
    return None


def fetch_orders_rest(created_min_iso, created_max_iso, label, company_names, location_names):
    """Pull every order in the window via REST (cursor pagination), keep the
    B2B company orders. Returns [{company, location, day, items:[(sku,title,qty)]}]."""
    from urllib.parse import quote
    url = (
        f'https://{DOMAIN}/admin/api/{API_VERSION}/orders.json'
        f'?status=any&limit=250&fields={REST_FIELDS}'
        f'&created_at_min={quote(created_min_iso)}&created_at_max={quote(created_max_iso)}'
    )
    orders, pages, raw_count, unmapped = [], 0, 0, set()
    while url:
        data, link = rest_get(url)
        pages += 1
        for o in data.get('orders', []):
            raw_count += 1
            if o.get('test') or o.get('cancelled_at'):
                continue
            company_ref = o.get('company') or {}
            cid = company_ref.get('id')
            if not cid:
                continue  # not a B2B order → ShopifyQL's WHERE company_name != '' drops it
            company = company_names.get(int(cid))
            if not company:
                unmapped.add(cid)
                continue
            location = location_names.get(int(company_ref.get('location_id') or 0), '')
            day = (
                datetime.fromisoformat(o['created_at'])
                .astimezone(SHOP_TZ)
                .date()
            )
            items = []
            for li in o.get('line_items', []):
                sku = (li.get('sku') or '').strip()
                title = (li.get('title') or '').strip()
                qty = int(li.get('quantity') or 0)
                if qty:
                    items.append((sku, title, qty, li.get('variant_id')))
            orders.append({'company': company, 'location': location, 'day': day, 'items': items})
        url = next_link(link)
        time.sleep(0.5)  # 2 req/s sustained
    print(f'  {label}: {raw_count} orders over {pages} pages → {len(orders)} company orders', flush=True)
    if unmapped:
        print(f'  WARNING {label}: {len(unmapped)} orders referenced unknown company ids '
              f'(deleted companies?): {sorted(unmapped)[:5]}', flush=True)

    # Backfill empty SKUs from the variant, the way ShopifyQL does.
    empty_variants = {
        vid for order in orders for (sku, _, _, vid) in order['items'] if not sku and vid
    }
    resolved = resolve_variant_skus(empty_variants) if empty_variants else {}
    if resolved:
        print(f'  {label}: backfilled {len(resolved)} empty line-item SKUs via variant', flush=True)
    for order in orders:
        order['items'] = [
            (sku or resolved.get(vid, ''), title, qty)
            for (sku, title, qty, vid) in order['items']
        ]
    return orders


# ── Window files ──────────────────────────────────────────────────────────────

def write_mcp_file(path, columns, rows):
    Path(path).write_text(json.dumps({
        'columns': [{'name': c} for c in columns],
        'rows': rows,
        'rowCount': len(rows),
    }))
    print(f'  wrote {path} ({len(rows)} rows)', flush=True)


def build_window_files(orders, windows, today, outdir):
    """Aggregate parsed orders into the ShopifyQL-shaped window files."""
    for name, (since_d, until_d) in windows.items():
        agg = defaultdict(int)
        for order in orders:
            idx = (today - order['day']).days
            if not (until_d <= idx <= since_d):
                continue
            for sku, title, qty in order['items']:
                agg[(order['company'], order['location'], sku, title)] += qty
        rows = [
            [c, l, s, t, q]
            for (c, l, s, t), q in sorted(agg.items(), key=lambda kv: -kv[1])
        ]
        write_mcp_file(
            outdir / f'{name}.json',
            ['company_name', 'company_location_name', 'product_variant_sku',
             'product_title', 'quantity_ordered'],
            rows,
        )


def build_30d_file(orders, today, outdir):
    counts = defaultdict(int)
    for order in orders:
        if 0 <= (today - order['day']).days <= 30:
            counts[(order['company'], order['location'])] += 1
    rows = [[c, l, n] for (c, l), n in sorted(counts.items(), key=lambda kv: -kv[1])]
    write_mcp_file(outdir / '30d.json',
                   ['company_name', 'company_location_name', 'orders'], rows)


def build_last_file(orders, today, outdir):
    agg = defaultdict(int)
    for order in orders:
        if 0 <= (today - order['day']).days <= 90:
            for sku, title, qty in order['items']:
                agg[(order['company'], order['location'], sku, title,
                     order['day'].isoformat())] += qty
    # ORDER BY day DESC is load-bearing: the transform takes the first hit per
    # key as the most recent order date.
    rows = [
        [c, l, s, t, q, d]
        for (c, l, s, t, d), q in sorted(agg.items(), key=lambda kv: kv[0][4], reverse=True)
    ]
    write_mcp_file(
        outdir / 'last.json',
        ['company_name', 'company_location_name', 'product_variant_sku',
         'product_title', 'quantity_ordered', 'day'],
        rows,
    )


# ── Step 3: new zero-order companies ─────────────────────────────────────────

def fetch_new_companies(today, outdir):
    cutoff = (today - timedelta(days=90)).isoformat()
    nodes, cursor = [], None
    while True:
        data = graphql(
            '''
            query($q: String!, $after: String) {
              companies(first: 50, query: $q, after: $after) {
                pageInfo { hasNextPage endCursor }
                edges {
                  node {
                    name
                    createdAt
                    ordersCount { count }
                    locations(first: 20) { edges { node { name } } }
                    mainContact { customer { tags } }
                  }
                }
              }
            }
            ''',
            {'q': f'created_at:>{cutoff}', 'after': cursor},
        )['companies']
        nodes += [e['node'] for e in data['edges']]
        if not data['pageInfo']['hasNextPage']:
            break
        cursor = data['pageInfo']['endCursor']

    companies = []
    for n in nodes:
        if n['ordersCount']['count'] > 0:
            continue
        mc = n.get('mainContact')
        tags = mc['customer']['tags'] if mc and mc.get('customer') else []
        catalog_tag = next((t for t in tags if t in CATALOG_TAGS), None)
        locs = [e['node']['name'] for e in n.get('locations', {}).get('edges', [])]
        companies.append({
            'name': n['name'], 'createdAt': n['createdAt'],
            'locations': locs, 'catalogTag': catalog_tag,
        })
    (outdir / 'companies.json').write_text(json.dumps({'companies': companies}))
    print(f'  wrote companies.json ({len(companies)} zero-order accounts)', flush=True)


# ── Step 3b: Limited Release + Brew Tag Label SKUs ───────────────────────────

def resolve_lr_handle():
    """Find the Limited Release collection by TITLE — its handle has already
    drifted once (limited-release → limited-release-1 when it was recreated)."""
    collections = graphql(
        '{ collections(first: 100) { edges { node { handle title } } } }'
    )['collections']['edges']
    for edge in collections:
        if edge['node']['title'].strip().lower() == 'limited release':
            return edge['node']['handle']
    return None


# The LR collection mixes coffee, brew-tag-label variants, cans, and merch.
# lr_skus.json is a SKIP-RULE OVERRIDE in refresh_from_mcp.py — anything in it
# gets pulled into the tool, and unclassifiable SKUs default to LR-5LB (so a $6
# enamel pin would land as a 5lb bag of coffee). Only coffee formats belong:
LR_COFFEE_SKU = re.compile(r'^[A-Z]+(1201-RC|1201|501|201)$')


def fetch_phase2_skus(outdir):
    handle = resolve_lr_handle()
    all_skus, cursor = [], None
    while handle:
        data = graphql(
            '''
            query($handle: String!, $after: String) {
              collectionByHandle(handle: $handle) {
                products(first: 50, after: $after) {
                  pageInfo { hasNextPage endCursor }
                  edges {
                    node {
                      variants(first: 20) { edges { node { sku } } }
                    }
                  }
                }
              }
            }
            ''',
            {'handle': handle, 'after': cursor},
        )['collectionByHandle']
        if data is None:
            break
        products = data['products']
        for edge in products['edges']:
            all_skus += [
                v['node']['sku'] for v in edge['node']['variants']['edges'] if v['node']['sku']
            ]
        if not products['pageInfo']['hasNextPage']:
            break
        cursor = products['pageInfo']['endCursor']
    if not handle:
        print('  WARNING: no collection titled "Limited Release"; lr_skus.json empty', flush=True)

    # Split per the par-refresh SKILL.md Step 3b rules: coffee formats →
    # lr_skus, BRWTG-* variants → brew_tag_skus, everything else (cans,
    # apparel, merch) → dropped so the normal skip rules keep handling it.
    lr_skus = [s for s in all_skus if LR_COFFEE_SKU.match(s)]
    brew_skus = [s for s in all_skus if s.startswith('BRWTG-')]
    dropped = sorted(set(all_skus) - set(lr_skus) - set(brew_skus))

    (outdir / 'lr_skus.json').write_text(json.dumps({'skus': lr_skus}))
    print(f'  wrote lr_skus.json ({len(lr_skus)} coffee SKUs)', flush=True)
    (outdir / 'brew_tag_skus.json').write_text(json.dumps({'skus': brew_skus}))
    print(f'  wrote brew_tag_skus.json ({len(brew_skus)} BRWTG-* SKUs)', flush=True)
    if dropped:
        print(f'  dropped {len(dropped)} non-coffee LR SKUs (left to normal skip rules): '
              f'{", ".join(dropped[:12])}{"…" if len(dropped) > 12 else ""}', flush=True)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    outdir = Path('/tmp/par_refresh')
    if '--outdir' in sys.argv:
        outdir = Path(sys.argv[sys.argv.index('--outdir') + 1])
    outdir.mkdir(parents=True, exist_ok=True)

    today = datetime.now(SHOP_TZ).date()

    def iso(day_offset, end_of_day=False):
        d = today - timedelta(days=day_offset)
        t = '23:59:59' if end_of_day else '00:00:00'
        return datetime.fromisoformat(f'{d.isoformat()}T{t}').replace(tzinfo=SHOP_TZ).isoformat()

    # Shop verification — fail loudly if this token somehow isn't the wholesale store.
    shop = graphql('{ shop { name myshopifyDomain } }')['shop']
    if shop['myshopifyDomain'] != DOMAIN:
        sys.exit(f'Error: token resolves to {shop["myshopifyDomain"]}, expected {DOMAIN}')
    print(f'Connected: {shop["name"]} ({shop["myshopifyDomain"]})', flush=True)

    print('Mapping company/location ids to names...', flush=True)
    company_names, location_names = fetch_company_maps()

    print('Pulling current window (98d → today)...', flush=True)
    current = fetch_orders_rest(iso(98), iso(0, end_of_day=True), 'current',
                                company_names, location_names)

    print('Pulling last-year window (463d → 365d)...', flush=True)
    ly = fetch_orders_rest(iso(463), iso(365, end_of_day=True), 'last-year',
                           company_names, location_names)

    print('Building window files...', flush=True)
    build_window_files(current, WINDOWS_CURRENT, today, outdir)
    build_window_files(ly, WINDOWS_LY, today, outdir)
    build_30d_file(current, today, outdir)
    build_last_file(current, today, outdir)

    print('Fetching new zero-order companies...', flush=True)
    fetch_new_companies(today, outdir)

    print('Fetching Phase 2 SKU lists...', flush=True)
    fetch_phase2_skus(outdir)

    print(f'\nDone. All files in {outdir}. Next:\n'
          f'  python3 scripts/refresh_from_mcp.py ' + ' '.join(
              str(outdir / f'{n}.json') for n in
              ['90d', '30d', 'w3l', 'w3p', 'last', 'ly_w3l', 'ly_w3p',
               'w7l', 'w7p', 'ly_w7l', 'ly_w7p', 'companies', 'lr_skus', 'brew_tag_skus']
          ))


if __name__ == '__main__':
    main()
