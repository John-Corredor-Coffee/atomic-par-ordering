// Direct port of scripts/refresh_from_mcp.py's SKU filtering/classification tables.
// LR-group/brew-tag handling intentionally omitted — the live /par-refresh skill never
// actually passes those optional args in production, so this matches real current behavior.

export const INTERNAL_NAMES = new Set(['Atomic Coffee Roasters (Internal)']);

const SKIP_PREFIXES = [
  'CYL-', 'KEGERATOR', 'HANDLE01', 'TAP01', 'COUPLER', 'REG01', 'SPOUT01',
  'TAMP', 'PALLO', 'GRNDZ', 'CFZA', 'RINZA', 'SHOTS', 'BRWTG', 'KNOCK',
  'FIL0', 'SLEEVES', 'SERVING', 'COPACK', 'SMITH', 'NS-',
  'TECH-', 'THIRDPARTY', 'PL-', 'FET-', 'SPACEH', 'WMFC',
  'AMOJU', 'BALINAT', 'KOMBUCHA', 'BOOCH', 'DCF-CANS',
  'LOUD-CANS', 'CANS0', 'CANS1', 'CANS2',
];
const SKIP_SUFFIXES = ['-S', '-GC', '-D'];
const SKIP_FRAGS = [
  'sample', 'screwdriver', 'knockbox', 'tamping mat', 'grindminder', 'brush',
  'barista basics', 'nitrogen', 'regulator', 'kegerator', 'tap handle',
  'spout', 'tech service', 'tech travel', 'technician', 'filter cartridge',
  'filter head', 'tubing', 'adapter', 'compression', 'hoodie',
  'cafiza', 'rinza', 'john guest', 'polyethylene', 'everpure',
  'pour-', 'frothing pitcher', 'wmf clean',
];
const CONSUMABLE_SKUS = new Set([
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
]);

export function skipSku(sku: string, name: string): boolean {
  if (!sku) return true;
  if (CONSUMABLE_SKUS.has(sku)) return false;
  const up = sku.toUpperCase();
  for (const p of SKIP_PREFIXES) if (up.startsWith(p.toUpperCase())) return true;
  for (const s of SKIP_SUFFIXES) if (up.endsWith(s.toUpperCase())) return true;
  const nl = name.toLowerCase();
  for (const f of SKIP_FRAGS) if (nl.includes(f)) return true;
  return false;
}

export type UsageUnit = 'lbs' | 'boxes' | 'kegs' | 'cans' | 'cartons' | 'tins' | 'units';
export type OrderUnit = 'bag' | 'case' | 'keg' | 'box' | 'unit';

export function classifySku(sku: string, name: string): [UsageUnit, OrderUnit, number] {
  const up = (sku || '').toUpperCase();
  const nl = (name || '').toLowerCase();
  if (up.endsWith('501') || nl.includes('- 5lb')) return ['lbs', 'bag', 5];
  if (up.endsWith('201') && !up.endsWith('201-D')) return ['lbs', 'bag', 2];
  if (up.endsWith('-LM') || nl.includes('local market case')) return ['lbs', 'case', 12];
  if (up.endsWith('-RC') || nl.includes('retail case')) return ['lbs', 'case', 4.5];
  if (up.startsWith('CONC-')) return ['boxes', 'case', 2];
  if (up.startsWith('CBKEG') || up.startsWith('CBK0') || up.startsWith('JPKEG')) return ['kegs', 'keg', 1];
  if (up.includes('CANS') || up.endsWith('-CANS')) return ['cans', 'case', 12];
  if (up.includes('PP-') || up.includes('RKTPP') || up.includes('DCFPP')) return ['boxes', 'box', 1];
  if (up.includes('MF-CHAI') || (up.includes('CHAI') && up.includes('MF'))) return ['cartons', 'case', 4];
  if (up.includes('OAT')) return ['cartons', 'case', 6];
  if (up.includes('PMTCH') || up.includes('MATCHA')) return ['tins', 'case', 12];
  return ['units', 'unit', 1];
}

export const ITEM_ORDER: Record<string, number> = { lbs: 0, boxes: 1, kegs: 2, cans: 3, cartons: 4, tins: 5 };

export const CATALOG_TAGS = new Set([
  'standard-cafe', 'standard-cafe-cans-kegs', 'standard-cafe-pp-cans-kegs',
  'standard-cafe-pp-cans-sankey', 'standard-cafe-pp-cans-dlv', 'standard-cafe-pp-cans-ship',
  'standard-cafe-pp', 'standard-cafe-cans-sankey', 'standard-cafe-cans-dlv',
  'standard-cafe-cans-ship', 'local-market', 'local-market-cans',
]);

export function skuMatchesCatalog(sku: string, name: string, catalogTag: string): boolean {
  const tag = (catalogTag || '').toLowerCase();
  if (!tag) return false;
  const [u, o] = classifySku(sku, name);
  const isCafe = tag.startsWith('standard-cafe');
  const isMarket = tag.startsWith('local-market');
  const hasCans = tag.includes('cans');
  const hasKegs = tag.includes('kegs') || tag.includes('sankey');
  const hasPp = tag.includes('-pp');
  if (isCafe) {
    if (u === 'lbs' && o === 'bag') return true;
    if (u === 'cartons' || u === 'tins') return true;
    if (u === 'cans' && hasCans) return true;
    if (u === 'kegs' && hasKegs) return true;
    if (u === 'boxes' && o === 'box' && hasPp) return true;
    if (u === 'boxes' && o === 'case' && (hasKegs || hasCans)) return true;
  }
  if (isMarket) {
    if (u === 'lbs' && o === 'case') return true;
    if (u === 'cans' && hasCans) return true;
  }
  return false;
}
