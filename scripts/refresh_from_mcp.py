#!/usr/bin/env python3
"""
PAR Ordering — Build data.json from MCP ShopifyQL output files.

Account membership is driven by order-activity windows ("Company Orders" reports):
  • Ordered in last 90 days  → included in the tool
  • Ordered in last 30 days  → reviewStatus 'active'  (Independent / Groups tabs)
  • In 90d but NOT in 30d     → reviewStatus 'needs-review'
  • Not in 90d                → dropped entirely (never written)
  • Created <90d ago, 0 orders→ reviewStatus 'new' (kept as a separate "New" group)
There are no day-count thresholds in the front-end anymore; status is baked in here.

Usage (Claude Code runs the queries via MCP, then calls this):
  python3 scripts/refresh_from_mcp.py <90d> <30d> <w3l> <w3p> <last> <ly_w3l> <ly_w3p> <w7l> <w7p> <ly_w7l> <ly_w7p> [companies] [lr_skus] [brew_tag_skus]

Each file is the raw JSON saved by the run-analytics-query MCP tool:
  { "columns": [{"name": ...}], "rows": [[...], ...], "rowCount": N, ... }

Queries:
  90d    — base/inclusion window. FROM sales SHOW quantity_ordered GROUP BY company_name, company_location_name, product_variant_sku, product_title WHERE company_name != '' ORDER BY quantity_ordered DESC LIMIT 5000 SINCE -90d UNTIL today
  30d    — active membership. FROM sales SHOW orders GROUP BY company_name, company_location_name WHERE company_name != '' SINCE -30d UNTIL today
  w3l    — SINCE -21d  UNTIL today
  w3p    — SINCE -42d  UNTIL -21d
  last   — FROM sales SHOW quantity_ordered, day GROUP BY ..., day SINCE -90d UNTIL today ORDER BY day DESC LIMIT 5000
  ly_w3l — SINCE -386d UNTIL -365d  (LY 21-day window matching w3l)
  ly_w3p — SINCE -407d UNTIL -386d  (LY 21-day window matching w3p)
  w7l    — SINCE -49d  UNTIL today
  w7p    — SINCE -98d  UNTIL -49d
  ly_w7l — SINCE -414d UNTIL -365d  (LY 49-day window matching w7l)
  ly_w7p — SINCE -463d UNTIL -414d  (LY 49-day window matching w7p)
"""

import json
import sys
from datetime import datetime, timezone, timedelta
from collections import defaultdict, Counter
from pathlib import Path

# ── CONSTANTS (mirrors agg.py) ────────────────────────────────────────────────
TODAY       = datetime.now(timezone.utc)
WINDOW_DAYS = 90   # base/inclusion window. NOTE: the JSON key 'qty60' below holds
                   # this window's qty (kept named qty60 for front-end compatibility).

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

# ── PHASE 2: Limited Release group SKUs ──────────────────────────────────────
# LR SKUs from the Shopify "Limited Release" collection are grouped by format
# instead of tracked individually (since specific coffees rotate).
# Brew Tag Label products are excluded entirely.
LR_GROUP_NAMES = {
    'LR-2LB': 'Limited Release (2lb bag)',
    'LR-5LB': 'Limited Release (5lb bag)',
    'LR-RC':  'Limited Release (Retail Case)',
}


def load_lr_skus(path):
    """Load set of SKUs in the Shopify 'Limited Release' collection (brew tag already excluded).
    Expected file format: {"skus": ["SKU1", "SKU2", ...]}
    Returns empty set if file not provided or missing.
    """
    if not path or not Path(path).exists():
        return set()
    data = json.loads(Path(path).read_text())
    skus = set(data.get('skus', []))
    print(f"  Limited Release SKUs loaded: {len(skus)}")
    return skus


def load_brew_tag_skus(path):
    """Load set of SKUs tagged 'Brew Tag Label' in Shopify.
    Expected file format: {"skus": ["SKU1", "SKU2", ...]}
    Returns empty set if file not provided or missing.
    """
    if not path or not Path(path).exists():
        return set()
    data = json.loads(Path(path).read_text())
    skus = set(data.get('skus', []))
    print(f"  Brew Tag Label SKUs loaded (will be excluded): {len(skus)}")
    return skus


def get_lr_group_sku(sku, name, lr_skus):
    """If SKU belongs to the Limited Release collection, return its group SKU.
    Returns None if not an LR SKU.
    """
    if not lr_skus or sku not in lr_skus:
        return None
    u, o, upo = classify_sku(sku, name)
    if u == 'lbs':
        if o == 'case':
            return 'LR-RC'
        if upo == 2:
            return 'LR-2LB'
        return 'LR-5LB'   # 5lb bag or unknown size → default to 5lb group
    return 'LR-5LB'       # fallback


def skip_sku(sku, name, brew_tag_skus=None, lr_skus=None):
    if not sku:
        return True
    # Phase 2: always exclude Brew Tag Label products
    if brew_tag_skus and sku in brew_tag_skus:
        return True
    # Phase 2: LR SKUs are explicitly allowed even if prefix matches SKIP_PREFIXES
    if lr_skus and sku in lr_skus:
        return False
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
    # Phase 2: synthetic LR group SKUs
    if up == 'LR-2LB': return ('lbs', 'bag',  2)
    if up == 'LR-5LB': return ('lbs', 'bag',  5)
    if up == 'LR-RC':  return ('lbs', 'case', 4.5)
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


def load_mcp_file(path):
    """Parse MCP tool-result JSON → list of row dicts."""
    data = json.loads(Path(path).read_text())
    cols = [c['name'] for c in data['columns']]
    return [dict(zip(cols, row)) for row in data['rows']]


def load_membership(path):
    """Parse the 30-day membership query (SHOW orders GROUP BY company, location).
    Returns set of (company_name, location_name) with at least one order in the window.
    These locations are 'active'; included-but-absent locations are 'needs-review'.
    """
    data = json.loads(Path(path).read_text())
    cols = [c['name'] for c in data['columns']]
    active = set()
    for row in data['rows']:
        r = dict(zip(cols, row))
        cname = (r.get('company_name') or '').strip()
        lname = (r.get('company_location_name') or '').strip()
        if not cname or cname in INTERNAL_NAMES:
            continue
        try:
            orders = float(r.get('orders') or 0)
        except (ValueError, TypeError):
            orders = 0
        if orders > 0:
            active.add((cname, lname))
    return active


def build_last_lookup(rows, lr_skus=None, brew_tag_skus=None):
    """
    Build {(company, location, sku): (date_str, qty)} from the last-order query rows.
    Rows must be pre-sorted ORDER BY day DESC so the first hit per key = most recent date.
    LR SKUs are remapped to their group SKU; last-order date for the group = most recent
    order date across all individual LR coffees in that group for that location.
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
        if skip_sku(sku, name, brew_tag_skus, lr_skus):
            continue
        effective_sku = get_lr_group_sku(sku, name, lr_skus) or sku
        key = (cname, lname, effective_sku)
        if key not in lookup:
            lookup[key] = (date_str, qty)
    return lookup


def build_yoy_lookup(rows, lr_skus=None, brew_tag_skus=None):
    """Build {(company, location, sku): total_qty} from the prior-year 21-day window (same as w3l)."""
    lookup = {}
    for row in rows:
        cname = (row.get('company_name') or '').strip()
        lname = (row.get('company_location_name') or '').strip()
        sku   = (row.get('product_variant_sku') or '').strip()
        name  = (row.get('product_title') or '').strip()
        try:
            qty = int(float(row.get('quantity_ordered') or 0))
        except (ValueError, TypeError):
            qty = 0
        if not cname or cname in INTERNAL_NAMES or not sku or qty == 0:
            continue
        if skip_sku(sku, name, brew_tag_skus, lr_skus):
            continue
        effective_sku = get_lr_group_sku(sku, name, lr_skus) or sku
        key = (cname, lname, effective_sku)
        lookup[key] = lookup.get(key, 0) + qty
    return lookup


def load_delivery_days():
    lookup = {}
    data_file = Path(__file__).parent.parent / 'data.json'
    if data_file.exists():
        payload = json.loads(data_file.read_text())
        for company in payload.get('companies', []):
            for loc in company.get('locations', []):
                lookup[(company['name'], loc['name'])] = loc.get('deliveryDay', 'wednesday')
    return lookup


def aggregate(rows_60d, rows_w3l, rows_w3p, rows_ly_w3l, rows_ly_w3p,
              rows_w7l, rows_w7p, rows_ly_w7l, rows_ly_w7p, delivery_days,
              lr_skus=None, brew_tag_skus=None):
    locs = {}

    def ensure(cname, lname):
        key = (cname, lname)
        if key not in locs:
            locs[key] = {
                'company_name':  cname,
                'location_name': lname,
                'delivery_day':  delivery_days.get(key, 'wednesday'),
                'skus': {},
            }
        return locs[key]

    def add(rows, field):
        for row in rows:
            cname = (row.get('company_name') or '').strip()
            lname = (row.get('company_location_name') or '').strip()
            sku   = (row.get('product_variant_sku') or '').strip()
            name  = (row.get('product_title') or '').strip()
            try:
                qty = int(float(row.get('quantity_ordered') or 0))
            except (ValueError, TypeError):
                qty = 0
            if not cname or cname in INTERNAL_NAMES or not sku or qty == 0:
                continue
            if skip_sku(sku, name, brew_tag_skus, lr_skus):
                continue
            # Phase 2: remap LR SKUs to group SKU
            group_sku = get_lr_group_sku(sku, name, lr_skus)
            effective_sku  = group_sku or sku
            effective_name = LR_GROUP_NAMES.get(effective_sku, name)
            loc = ensure(cname, lname)
            if effective_sku not in loc['skus']:
                loc['skus'][effective_sku] = {'name': effective_name, 'qty60': 0,
                                              'qw3l': 0, 'qw3p': 0, 'qly_w3l': 0, 'qly_w3p': 0,
                                              'qw7l': 0, 'qw7p': 0, 'qly_w7l': 0, 'qly_w7p': 0}
            loc['skus'][effective_sku][field] += qty
            if not loc['skus'][effective_sku]['name']:
                loc['skus'][effective_sku]['name'] = effective_name

    add(rows_60d,    'qty60')
    add(rows_w3l,    'qw3l')
    add(rows_w3p,    'qw3p')
    add(rows_ly_w3l, 'qly_w3l')
    add(rows_ly_w3p, 'qly_w3p')
    add(rows_w7l,    'qw7l')
    add(rows_w7p,    'qw7p')
    add(rows_ly_w7l, 'qly_w7l')
    add(rows_ly_w7p, 'qly_w7p')
    return locs


ITEM_ORDER = {'lbs': 0, 'boxes': 1, 'kegs': 2, 'cans': 3, 'cartons': 4, 'tins': 5}

CATALOG_TAGS = {
    'standard-cafe', 'standard-cafe-cans-kegs', 'standard-cafe-pp-cans-kegs',
    'standard-cafe-pp-cans-sankey', 'standard-cafe-pp-cans-dlv', 'standard-cafe-pp-cans-ship',
    'standard-cafe-pp', 'standard-cafe-cans-sankey', 'standard-cafe-cans-dlv',
    'standard-cafe-cans-ship', 'local-market', 'local-market-cans',
}


def sku_matches_catalog(sku, name, catalog_tag):
    """Return True if this SKU should be shown for the given catalog tag."""
    tag = (catalog_tag or '').lower()
    if not tag:
        return False
    u, o, _ = classify_sku(sku, name)
    is_cafe   = tag.startswith('standard-cafe')
    is_market = tag.startswith('local-market')
    has_cans  = 'cans' in tag
    has_kegs  = 'kegs' in tag or 'sankey' in tag
    has_pp    = '-pp' in tag
    if is_cafe:
        if u == 'lbs' and o == 'bag':    return True   # whole beans
        if u in ('cartons', 'tins'):     return True   # specialty
        if u == 'cans'  and has_cans:    return True
        if u == 'kegs'  and has_kegs:    return True
        if u == 'boxes' and o == 'box'   and has_pp:              return True  # portion packs
        if u == 'boxes' and o == 'case'  and (has_kegs or has_cans): return True  # concentrate
    if is_market:
        if u == 'lbs'  and o == 'case':  return True   # retail cases
        if u == 'cans' and has_cans:     return True
    return False


def collect_sku_names(locs):
    """Build {sku: product_name} from aggregated order data."""
    names = {}
    for loc in locs.values():
        for sku, sk in loc['skus'].items():
            if sku not in names and sk['name']:
                names[sku] = sk['name']
    return names


def parse_companies_file(path):
    """
    Parse the GraphQL companies JSON saved by the par-refresh skill.
    Expected format: {"companies": [{name, createdAt, ordersCount, locations, catalogTag}, ...]}
    Returns list of dicts for companies created < 60 days ago with ordersCount == 0.
    """
    data = json.loads(Path(path).read_text())
    companies = data.get('companies', [])
    result = []
    cutoff = TODAY - timedelta(days=60)
    for co in companies:
        try:
            created = datetime.fromisoformat(co['createdAt'].replace('Z', '+00:00'))
        except (KeyError, ValueError):
            continue
        if created.replace(tzinfo=None) >= cutoff.replace(tzinfo=None):
            result.append(co)
    return result


def compute_benchmarks(locs):
    """Per-SKU average daily rate from established single-location (independent) accounts."""
    loc_counts = Counter(cname for (cname, _) in locs)
    sku_rates = defaultdict(list)
    for (cname, _), loc in locs.items():
        if loc_counts[cname] != 1:
            continue
        for sku, sk in loc['skus'].items():
            if sk['qw3l'] > 0 and sk['qw3p'] > 0:
                sku_rates[sku].append(sk['qw3l'] / 21)
    return {sku: round(sum(rates) / len(rates), 3) for sku, rates in sku_rates.items()}


def build_json(locs, last_lookup, benchmarks, active_set, new_companies=None, sku_names=None):
    by_company = defaultdict(lambda: {'name': None, 'locations': []})

    for (cname, lname), loc in locs.items():
        items = []
        for sku, sk in loc['skus'].items():
            if sk['qty60'] == 0:
                continue
            w3l        = sk['qw3l'];  w3p     = sk['qw3p']
            if w3p > 0:
                avg_daily = round(w3l / 21, 3) if w3l > 0 else round(w3p / 21, 3)
            elif w3l > 0:
                bench = benchmarks.get(sku, 0)
                avg_daily = round(bench, 3) if bench > 0 else round(w3l / 21, 3)
            else:
                avg_daily = round(sk['qty60'] / WINDOW_DAYS, 3)
            ly_w3l     = sk['qly_w3l']; ly_w3p = sk['qly_w3p']
            w7l        = sk['qw7l'];  w7p     = sk['qw7p']
            ly_w7l     = sk['qly_w7l']; ly_w7p = sk['qly_w7p']
            w3         = round(w3l / w3p - 1, 3)     if w3p    > 0 else 0.0
            ly_w3      = round(ly_w3l / ly_w3p - 1, 3) if ly_w3p > 0 else 0.0
            w7         = round(w7l / w7p - 1, 3)     if w7p    > 0 else 0.0
            ly_w7      = round(ly_w7l / ly_w7p - 1, 3) if ly_w7p > 0 else 0.0
            _yoy_valid = ly_w3l >= 1 and (w3l == 0 or ly_w3l / w3l >= 0.10)
            yoy        = round((w3l - ly_w3l) / ly_w3l, 3) if _yoy_valid else 0.0
            u, o, upo  = classify_sku(sku, sk['name'])
            last_date, last_qty = last_lookup.get((cname, lname, sku), ('', 0))
            # If day-level query missed this SKU (LIMIT 5000 cutoff), estimate from windows.
            if not last_date:
                if w3l > 0:
                    last_date = (TODAY - timedelta(days=10)).strftime('%Y-%m-%d')
                elif sk['qw3p'] > 0:
                    last_date = (TODAY - timedelta(days=31)).strftime('%Y-%m-%d')
                elif sk['qty60'] > 0:
                    last_date = (TODAY - timedelta(days=51)).strftime('%Y-%m-%d')
            items.append({
                'sku': sku, 'name': sk['name'],
                'usageUnit': u, 'orderUnit': o, 'unitsPerOrder': upo,
                'avgDaily': avg_daily, 'yoy': yoy,
                'w3': w3, 'ly_w3': ly_w3,
                'w7': w7, 'ly_w7': ly_w7,
                'qw3l': w3l, 'qly_w3l': ly_w3l,
                'lastOrderDate': last_date, 'lastOnHand': last_qty,
            })
        if not items:
            continue
        items.sort(key=lambda i: (ITEM_ORDER.get(i['usageUnit'], 9), i['name']))
        c = by_company[cname]
        c['name'] = cname
        # Active if it ordered in the last 30 days; otherwise it's in the 90d
        # window but not the 30d window → needs review.
        review_status = 'active' if (cname, lname) in active_set else 'needs-review'
        c['locations'].append({
            'id': len(c['locations']),
            'name': lname,
            'deliveryDay': loc['delivery_day'] or 'wednesday',
            'daysOpen': 7, 'safetyDays': 3,
            'reviewStatus': review_status,
            'items': items,
        })

    # Inject week-zero accounts: companies <60 days old with no order history
    if new_companies:
        sku_names = sku_names or {}
        for co in new_companies:
            cname = co.get('name', '').strip()
            if not cname or cname in by_company:
                continue
            catalog_tag = co.get('catalogTag') or ''
            if catalog_tag not in CATALOG_TAGS:
                continue
            items = []
            for sku, rate in sorted(benchmarks.items(), key=lambda x: -x[1]):
                sname = sku_names.get(sku, sku)
                u, o, upo = classify_sku(sku, sname)
                if sku_matches_catalog(sku, sname, catalog_tag):
                    items.append({
                        'sku': sku, 'name': sname,
                        'usageUnit': u, 'orderUnit': o, 'unitsPerOrder': upo,
                        'avgDaily': rate, 'yoy': 0,
                        'w3': 0, 'ly_w3': 0, 'w7': 0, 'ly_w7': 0,
                        'qw3l': 0, 'qly_w3l': 0,
                        'lastOrderDate': '', 'lastOnHand': 0,
                        'weekZero': True,
                    })
            if not items:
                continue
            items.sort(key=lambda i: (ITEM_ORDER.get(i['usageUnit'], 9), i['name']))
            c = by_company[cname]
            c['name'] = cname
            for loc_name in (co.get('locations') or [cname]):
                c['locations'].append({
                    'id': len(c['locations']),
                    'name': loc_name,
                    'deliveryDay': co.get('deliveryDay', 'wednesday'),
                    'daysOpen': 7, 'safetyDays': 5,
                    'reviewStatus': 'new',
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
    return companies


def main():
    if len(sys.argv) not in (12, 13, 14, 15):
        sys.exit("Usage: refresh_from_mcp.py <90d> <30d> <w3l> <w3p> <last> <ly_w3l> <ly_w3p> <w7l> <w7p> <ly_w7l> <ly_w7p> [companies] [lr_skus] [brew_tag_skus]")

    args = sys.argv[1:]
    f90, f30, fw3l, fw3p, flast, fly_w3l, fly_w3p, fw7l, fw7p, fly_w7l, fly_w7p = args[:11]
    fcompanies    = args[11] if len(args) >= 12 else None
    flr_skus      = args[12] if len(args) >= 13 else None
    fbrew_tag     = args[13] if len(args) >= 14 else None

    print("Loading MCP result files...")
    rows_60d    = load_mcp_file(f90)   # base/inclusion window (90d) → fills qty60 field
    rows_w3l    = load_mcp_file(fw3l)
    rows_w3p    = load_mcp_file(fw3p)
    rows_last   = load_mcp_file(flast)
    rows_ly_w3l = load_mcp_file(fly_w3l)
    rows_ly_w3p = load_mcp_file(fly_w3p)
    rows_w7l    = load_mcp_file(fw7l)
    rows_w7p    = load_mcp_file(fw7p)
    rows_ly_w7l = load_mcp_file(fly_w7l)
    rows_ly_w7p = load_mcp_file(fly_w7p)
    print(f"  60d:{len(rows_60d)} w3l:{len(rows_w3l)} w3p:{len(rows_w3p)} last:{len(rows_last)} "
          f"ly_w3l:{len(rows_ly_w3l)} ly_w3p:{len(rows_ly_w3p)} "
          f"w7l:{len(rows_w7l)} w7p:{len(rows_w7p)} ly_w7l:{len(rows_ly_w7l)} ly_w7p:{len(rows_ly_w7p)}")

    # Phase 2: load LR collection and Brew Tag exclusion lists
    lr_skus       = load_lr_skus(flr_skus)
    brew_tag_skus = load_brew_tag_skus(fbrew_tag)

    active_set = load_membership(f30)
    print(f"  Active membership (ordered in last 30d): {len(active_set)} locations")

    delivery_days = load_delivery_days()
    print(f"  Delivery day lookup: {len(delivery_days)} locations")

    last_lookup = build_last_lookup(rows_last, lr_skus, brew_tag_skus)
    print(f"  Last-order lookup: {len(last_lookup)} SKUs")

    locs = aggregate(rows_60d, rows_w3l, rows_w3p, rows_ly_w3l, rows_ly_w3p,
                     rows_w7l, rows_w7p, rows_ly_w7l, rows_ly_w7p, delivery_days,
                     lr_skus, brew_tag_skus)
    benchmarks = compute_benchmarks(locs)
    print(f"  Independent benchmarks: {len(benchmarks)} SKUs")
    sku_names = collect_sku_names(locs)
    new_companies = None
    if fcompanies:
        new_companies = parse_companies_file(fcompanies)
        print(f"  New accounts (< 60 days, 0 orders): {len(new_companies)}")
    companies = build_json(locs, last_lookup, benchmarks, active_set, new_companies, sku_names)

    out = Path(__file__).parent.parent / 'data.json'
    payload = {
        'generated':  TODAY.isoformat(),
        'windowDays': WINDOW_DAYS,
        'companies':  companies,
    }
    out.write_text(json.dumps(payload, indent=2, default=str))

    active = len([c for c in companies if not c.get('isInternal')])
    status_counts = Counter(
        loc.get('reviewStatus', 'active')
        for c in companies if not c.get('isInternal')
        for loc in c['locations']
    )
    n_locs = sum(len(c['locations']) for c in companies if not c.get('isInternal'))
    print(f"\ndata.json written — {active} companies, {n_locs} locations")
    print(f"  by status: active={status_counts.get('active', 0)} "
          f"needs-review={status_counts.get('needs-review', 0)} new={status_counts.get('new', 0)}")


if __name__ == '__main__':
    main()
