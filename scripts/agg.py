#!/usr/bin/env python3
"""
Incremental aggregator for PAR ordering data.
Called once per page of MCP order data, then once with --finalize to write data.json.

Usage:
  python3 scripts/agg.py /tmp/page.json         # process one page
  python3 scripts/agg.py --finalize             # write data.json from accumulated state
  python3 scripts/agg.py --reset                # clear state (start fresh)
"""

import json
import sys
from datetime import datetime, timezone, timedelta
from collections import defaultdict, Counter
from pathlib import Path

STATE_FILE = Path(__file__).parent / 'agg_state.json'
DATA_FILE  = Path(__file__).parent.parent / 'data.json'

TODAY          = datetime.now(timezone.utc)
WINDOW_DAYS    = 60
W3_LAST_START  = TODAY - timedelta(days=21)
W3_PRIOR_START = TODAY - timedelta(days=42)

DELIVERY_DAYS    = {'monday','tuesday','wednesday','thursday','friday','saturday','sunday'}
INTERNAL_NAMES   = {'Atomic Coffee Roasters (Internal)'}
SKIP_PREFIXES    = ('CYL-','KEGERATOR','HANDLE01','TAP01','COUPLER','REG01','SPOUT01',
                    'TAMP','PALLO','GRNDZ','CFZA','RINZA','SHOTS','BRWTG','KNOCK',
                    'FIL0','SLEEVES','SERVING','COPACK','SMITH','NS-',
                    'TECH-','THIRDPARTY','PL-','FET-','SPACEH','WMFC',
                    'AMOJU','BALINAT','KOMBUCHA','BOOCH','DCF-CANS',
                    'LOUD-CANS','CANS0','CANS1','CANS2',)
SKIP_SUFFIXES    = ('-S','-GC','-LM','-D')
SKIP_FRAGS       = ('sample','screwdriver','knockbox','tamping mat','grindminder','brush',
                    'barista basics','nitrogen','regulator','kegerator','tap handle',
                    'spout','tech service','tech travel','technician','filter cartridge',
                    'filter head','tubing','adapter','compression','hoodie',
                    'cafiza','rinza','john guest','polyethylene','everpure',
                    'pour-','frothing pitcher','wmf clean')

CONSUMABLE_SKUS = {
    # Coffee — keep regardless of name match
    'HSE501','HSE201','BV501','BV201','RKT501','RKT201','COS501','COS201',
    'INT501','INT201','DCF501','DCF201','DSL501','DSL201','CB501','CB201',
    'CAB501','CAB201','COL501','COL201','MAG501','MAG201','LOUD501',
    'HSE1201-RC','BV1201-RC','RKT1201-RC','COS1201-RC','INT1201-RC',
    'DCF1201-RC','DSL1201-RC','CB1201-RC','CAB1201-RC','COL1201-RC',
    'MAG1201-RC','LOUD1201-RC','GEDEO1201-RC','AMOJU1201-RC',
    # Cold brew
    'CONC-BIB1','CBKEG01','CBKEG02','CBK01-S','CBK02-S','JPKEG',
    'CANS01','CANS02','LOUD-CANS','DCF-CANS',
    # Portion packs
    'RKTPP-3.5','RKTPP-5','DCFPP-3.5','DCFPP-2.5',
    # Chai / oat / matcha
    'MF-CHAI','MF-OAT','SMITH-WS-PMTCH',
}


def skip_sku(sku, name):
    if not sku:
        return True
    # Always keep known consumables
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


def classify_sku(sku, name):
    up = (sku or '').upper()
    nl = name.lower()
    if up.endswith('501') or '- 5lb' in nl:
        return ('lbs', 'bag', 5)
    if up.endswith('201') and not up.endswith('201-D'):
        return ('lbs', 'bag', 2)
    if up.endswith('-RC') or 'retail case' in nl:
        return ('lbs', 'case', 4.5)
    if up.startswith('CONC-'):
        return ('boxes', 'box', 1)
    if up.startswith('CBKEG') or up.startswith('CBK0') or up.startswith('JPKEG'):
        return ('kegs', 'keg', 1)
    if 'CANS' in up or up.endswith('-CANS'):
        return ('cans', 'case', 12)
    if 'PP-' in up or 'RKTPP' in up or 'DCFPP' in up:
        return ('units', 'case', 10)
    if 'MF-CHAI' in up or ('CHAI' in up and 'MF' in up):
        return ('cartons', 'case', 4)
    if 'OAT' in up:
        return ('cartons', 'case', 6)
    if 'PMTCH' in up or 'MATCHA' in up:
        return ('tins', 'case', 12)
    return ('units', 'unit', 1)


def extract_delivery_day(tags):
    for t in (tags or []):
        if t.lower() in DELIVERY_DAYS:
            return t.lower()
    return 'wednesday'


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}   # key = "companyId|locationId"


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, default=str))


def process_page(orders, state):
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
        ts_raw = order.get('processedAt') or order.get('createdAt') or ''
        ts   = datetime.fromisoformat(ts_raw.replace('Z', '+00:00'))

        if key not in state:
            state[key] = {
                'company_id':    cid,
                'company_name':  company['name'],
                'location_id':   lid,
                'location_name': location['name'],
                'delivery_day':  extract_delivery_day(tags),
                'skus': {},
            }
        loc = state[key]
        if not loc['delivery_day']:
            loc['delivery_day'] = extract_delivery_day(tags)

        for ie in order['lineItems']['edges']:
            item = ie['node']
            sku  = item.get('sku') or ''
            name = item.get('name') or ''
            qty  = item.get('quantity') or 0
            if skip_sku(sku, name) or qty == 0:
                continue

            if sku not in loc['skus']:
                loc['skus'][sku] = {'name': name, 'qty60': 0, 'qw3l': 0, 'qw3p': 0, 'last': None}
            sk = loc['skus'][sku]
            sk['name'] = sk['name'] or name
            sk['qty60'] += qty
            if ts >= W3_LAST_START:
                sk['qw3l'] += qty
            elif ts >= W3_PRIOR_START:
                sk['qw3p'] += qty
            ts_str = ts.isoformat()
            if sk['last'] is None or ts_str > sk['last']:
                sk['last'] = ts_str


ITEM_ORDER = {'lbs': 0, 'boxes': 1, 'kegs': 2, 'cans': 3, 'cartons': 4, 'tins': 5}


def finalize(state):
    loc_counts = Counter(loc['company_id'] for loc in state.values())
    sku_rates = defaultdict(list)
    for loc in state.values():
        if loc_counts[loc['company_id']] != 1:
            continue
        for sku, sk in loc['skus'].items():
            if sk['qw3l'] > 0 and sk['qw3p'] > 0:
                sku_rates[sku].append(sk['qw3l'] / 21)
    benchmarks = {sku: round(sum(r) / len(r), 3) for sku, r in sku_rates.items()}

    by_company = defaultdict(lambda: {'name': None, 'locations': []})

    for loc in state.values():
        cid   = loc['company_id']
        items = []
        for sku, sk in loc['skus'].items():
            if sk['qty60'] == 0:
                continue
            bench = benchmarks.get(sku, 0)
            benchmark_seeded = False
            if sk['qw3p'] > 0:
                calc = round(sk['qw3l'] / 21, 3) if sk['qw3l'] > 0 else round(sk['qw3p'] / 21, 3)
                # Floor at 50% of benchmark so inventory-correction orders
                # (ordering less because on-hand is high) don't suppress avgDaily.
                if bench > 0 and calc < bench * 0.5:
                    avg_daily = round(bench * 0.5, 3)
                    benchmark_seeded = True
                else:
                    avg_daily = calc
            elif sk['qw3l'] > 0:
                # New account (<6 weeks history): prefer benchmark over raw order volume.
                if bench > 0:
                    avg_daily = round(bench, 3)
                    benchmark_seeded = True
                else:
                    avg_daily = round(sk['qw3l'] / 21, 3)
            else:
                avg_daily = round(sk['qty60'] / WINDOW_DAYS, 3)
            w3l = sk['qw3l'] / 3.0
            w3p = sk['qw3p'] / 3.0
            w3  = round(w3l / w3p - 1, 3) if w3p > 0 else 0.0
            u, o, upo = classify_sku(sku, sk['name'])
            items.append({
                'sku': sku, 'name': sk['name'],
                'usageUnit': u, 'orderUnit': o, 'unitsPerOrder': upo,
                'avgDaily': avg_daily, 'yoy': 0, 'w3': w3,
                'lastOrderDate': (sk['last'] or '')[:10],
                'lastOnHand': 0,
                'benchmarkSeeded': benchmark_seeded,
            })
        if not items:
            continue
        items.sort(key=lambda i: (ITEM_ORDER.get(i['usageUnit'], 9), i['name']))
        c = by_company[cid]
        c['name'] = loc['company_name']
        c['locations'].append({
            'id': len(c['locations']),
            'name': loc['location_name'],
            'deliveryDay': loc['delivery_day'] or 'wednesday',
            'daysOpen': 6, 'safetyDays': 3,
            'items': items,
        })

    companies = []
    for idx, c in enumerate(sorted(by_company.values(), key=lambda x: (x['name'] or '').lower())):
        if c['locations']:
            companies.append({'id': idx, 'name': c['name'], 'locations': c['locations']})

    companies.append({
        'id': len(companies),
        'name': 'Atomic Coffee Roasters (Internal)',
        'isInternal': True, 'locations': [],
    })

    payload = {
        'generated':  TODAY.isoformat(),
        'windowDays': WINDOW_DAYS,
        'companies':  companies,
    }
    DATA_FILE.write_text(json.dumps(payload, indent=2, default=str))
    active = len([c for c in companies if not c.get('isInternal')])
    print(f"data.json written — {active} companies, {sum(len(c['locations']) for c in companies)} locations")


def main():
    if len(sys.argv) < 2:
        print("Usage: agg.py <page.json> | --finalize | --reset")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == '--reset':
        STATE_FILE.unlink(missing_ok=True)
        print("State cleared.")
        return

    if cmd == '--finalize':
        state = load_state()
        print(f"Finalizing {len(state)} locations...")
        finalize(state)
        return

    # Process a page file
    page_data = json.loads(Path(cmd).read_text())
    orders = page_data if isinstance(page_data, list) else page_data.get('orders', [])
    state = load_state()
    before = sum(sk['qty60'] for loc in state.values() for sk in loc['skus'].values())
    process_page(orders, state)
    after  = sum(sk['qty60'] for loc in state.values() for sk in loc['skus'].values())
    save_state(state)
    print(f"  +{len(orders)} orders | locs={len(state)} | units={after-before} new | total={after}")


if __name__ == '__main__':
    main()
