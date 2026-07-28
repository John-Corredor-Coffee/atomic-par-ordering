var __defProp = Object.defineProperty;
var __name = (target, value) => __defProp(target, "name", { value, configurable: true });

// src/shopify.ts
async function getAccessToken(env) {
  const res = await fetch(`https://${env.SHOPIFY_STORE_DOMAIN}/admin/oauth/access_token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "client_credentials",
      client_id: env.SHOPIFY_CLIENT_ID,
      client_secret: env.SHOPIFY_CLIENT_SECRET
    })
  });
  if (!res.ok) throw new Error(`Shopify OAuth token exchange failed: HTTP ${res.status}`);
  const data = await res.json();
  return data.access_token;
}
__name(getAccessToken, "getAccessToken");
function makeClient(env, token) {
  return {
    gqlUrl: `https://${env.SHOPIFY_STORE_DOMAIN}/admin/api/2024-10/graphql.json`,
    token
  };
}
__name(makeClient, "makeClient");
async function graphqlRequest(client, query, variables = {}, attempt = 0) {
  const res = await fetch(client.gqlUrl, {
    method: "POST",
    headers: {
      "X-Shopify-Access-Token": client.token,
      "Content-Type": "application/json"
    },
    body: JSON.stringify({ query, variables })
  });
  const payload = await res.json();
  const throttled = payload.errors?.some((e) => e.extensions?.code === "THROTTLED");
  if (throttled && attempt < 3) {
    await sleep(2e3);
    return graphqlRequest(client, query, variables, attempt + 1);
  }
  if (payload.errors?.length) {
    throw new Error(`Shopify GraphQL error: ${JSON.stringify(payload.errors)}`);
  }
  const cost = payload.extensions?.cost;
  if (cost?.throttleStatus) {
    const { currentlyAvailable, restoreRate } = cost.throttleStatus;
    const requested = cost.requestedQueryCost;
    if (currentlyAvailable < requested) {
      const waitMs = (requested - currentlyAvailable) / Math.max(restoreRate, 1) * 1e3 + 500;
      await sleep(waitMs);
    }
  }
  return payload.data;
}
__name(graphqlRequest, "graphqlRequest");
function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
__name(sleep, "sleep");
async function fetchShopTimezone(client) {
  const query = `{ shop { ianaTimezone } }`;
  const data = await graphqlRequest(client, query);
  return data.shop.ianaTimezone;
}
__name(fetchShopTimezone, "fetchShopTimezone");
var ORDERS_QUERY = `
  query FetchOrders($cursor: String, $searchQuery: String!) {
    orders(first: 25, after: $cursor, query: $searchQuery, sortKey: PROCESSED_AT) {
      edges {
        node {
          id
          processedAt
          displayFinancialStatus
          purchasingEntity {
            __typename
            ... on PurchasingCompany {
              company { name }
              location { name }
            }
          }
          lineItems(first: 50) {
            pageInfo { hasNextPage }
            edges { node { sku title currentQuantity } }
          }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
`;
var ACCEPTED_FINANCIAL_STATUSES = /* @__PURE__ */ new Set(["PAID", "PARTIALLY_REFUNDED", "PARTIALLY_PAID"]);
async function fetchAllOrders(client, sinceDateIso) {
  const orders = [];
  let truncatedOrderCount = 0;
  let cursor = null;
  const searchQuery = `created_at:>=${sinceDateIso}`;
  while (true) {
    const data = await graphqlRequest(client, ORDERS_QUERY, { cursor, searchQuery });
    const edges = data.orders.edges;
    for (const { node } of edges) {
      if (node.purchasingEntity?.__typename !== "PurchasingCompany") continue;
      if (!ACCEPTED_FINANCIAL_STATUSES.has(node.displayFinancialStatus)) continue;
      if (node.lineItems.pageInfo.hasNextPage) truncatedOrderCount += 1;
      orders.push({
        processedAt: node.processedAt,
        displayFinancialStatus: node.displayFinancialStatus,
        companyName: node.purchasingEntity.company?.name ?? null,
        locationName: node.purchasingEntity.location?.name ?? null,
        lineItems: node.lineItems.edges.map((e) => ({
          sku: e.node.sku,
          title: e.node.title,
          currentQuantity: e.node.currentQuantity
        }))
      });
    }
    if (!data.orders.pageInfo.hasNextPage) break;
    cursor = data.orders.pageInfo.endCursor;
  }
  return { orders, truncatedOrderCount };
}
__name(fetchAllOrders, "fetchAllOrders");
var COMPANIES_QUERY = `
  query FetchCompanies($cursor: String, $searchQuery: String!) {
    companies(first: 50, after: $cursor, query: $searchQuery) {
      edges {
        node {
          name
          createdAt
          ordersCount { count }
          locations(first: 20) { edges { node { name } } }
          mainContact { customer { tags } }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
`;
async function fetchNewSignups(client, cutoffDateIso, excludeNames, catalogTags) {
  const results = [];
  let cursor = null;
  const searchQuery = `created_at:>${cutoffDateIso}`;
  while (true) {
    const data = await graphqlRequest(client, COMPANIES_QUERY, { cursor, searchQuery });
    const edges = data.companies.edges;
    for (const { node } of edges) {
      if (excludeNames.has(node.name)) continue;
      if (node.ordersCount.count !== 0) continue;
      const tags = node.mainContact?.customer?.tags ?? [];
      const catalogTag = tags.map((t) => t.toLowerCase()).find((t) => catalogTags.has(t)) ?? "";
      const locNames = node.locations.edges.map((e) => e.node.name);
      results.push({
        name: node.name,
        createdAt: node.createdAt,
        ordersCount: node.ordersCount.count,
        locations: locNames.length ? locNames : [node.name],
        catalogTag
      });
    }
    if (!data.companies.pageInfo.hasNextPage) break;
    cursor = data.companies.pageInfo.endCursor;
  }
  return results;
}
__name(fetchNewSignups, "fetchNewSignups");

// src/dates.ts
function localDateString(utcIso, ianaTz) {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: ianaTz,
    year: "numeric",
    month: "2-digit",
    day: "2-digit"
  }).format(new Date(utcIso));
}
__name(localDateString, "localDateString");
function parseDateKey(dateKey) {
  const [y, m, d] = dateKey.split("-").map(Number);
  return { y, m, d };
}
__name(parseDateKey, "parseDateKey");
function anchorUtcNoon(dateKey) {
  const { y, m, d } = parseDateKey(dateKey);
  return new Date(Date.UTC(y, m - 1, d, 12, 0, 0));
}
__name(anchorUtcNoon, "anchorUtcNoon");
function daysBefore(dateKey, days) {
  const anchor = anchorUtcNoon(dateKey);
  const shifted = new Date(anchor.getTime() - days * 864e5);
  const y = shifted.getUTCFullYear();
  const m = String(shifted.getUTCMonth() + 1).padStart(2, "0");
  const d = String(shifted.getUTCDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}
__name(daysBefore, "daysBefore");
function todayLocal(ianaTz) {
  return localDateString((/* @__PURE__ */ new Date()).toISOString(), ianaTz);
}
__name(todayLocal, "todayLocal");
function inWindow(dateKey, w) {
  if (dateKey < w.since) return false;
  return w.untilInclusive ? dateKey <= w.until : dateKey < w.until;
}
__name(inWindow, "inWindow");

// src/skuRules.ts
var INTERNAL_NAMES = /* @__PURE__ */ new Set(["Atomic Coffee Roasters (Internal)"]);
var SKIP_PREFIXES = [
  "CYL-",
  "KEGERATOR",
  "HANDLE01",
  "TAP01",
  "COUPLER",
  "REG01",
  "SPOUT01",
  "TAMP",
  "PALLO",
  "GRNDZ",
  "CFZA",
  "RINZA",
  "SHOTS",
  "BRWTG",
  "KNOCK",
  "FIL0",
  "SLEEVES",
  "SERVING",
  "COPACK",
  "SMITH",
  "NS-",
  "TECH-",
  "THIRDPARTY",
  "PL-",
  "FET-",
  "SPACEH",
  "WMFC",
  "AMOJU",
  "BALINAT",
  "KOMBUCHA",
  "BOOCH",
  "DCF-CANS",
  "LOUD-CANS",
  "CANS0",
  "CANS1",
  "CANS2"
];
var SKIP_SUFFIXES = ["-S", "-GC", "-D"];
var SKIP_FRAGS = [
  "sample",
  "screwdriver",
  "knockbox",
  "tamping mat",
  "grindminder",
  "brush",
  "barista basics",
  "nitrogen",
  "regulator",
  "kegerator",
  "tap handle",
  "spout",
  "tech service",
  "tech travel",
  "technician",
  "filter cartridge",
  "filter head",
  "tubing",
  "adapter",
  "compression",
  "hoodie",
  "cafiza",
  "rinza",
  "john guest",
  "polyethylene",
  "everpure",
  "pour-",
  "frothing pitcher",
  "wmf clean"
];
var CONSUMABLE_SKUS = /* @__PURE__ */ new Set([
  "HSE501",
  "HSE201",
  "BV501",
  "BV201",
  "RKT501",
  "RKT201",
  "COS501",
  "COS201",
  "INT501",
  "INT201",
  "DCF501",
  "DCF201",
  "DSL501",
  "DSL201",
  "CB501",
  "CB201",
  "CAB501",
  "CAB201",
  "COL501",
  "COL201",
  "MAG501",
  "MAG201",
  "LOUD501",
  "HSE1201-RC",
  "BV1201-RC",
  "RKT1201-RC",
  "COS1201-RC",
  "INT1201-RC",
  "DCF1201-RC",
  "DSL1201-RC",
  "CB1201-RC",
  "CAB1201-RC",
  "COL1201-RC",
  "MAG1201-RC",
  "LOUD1201-RC",
  "GEDEO1201-RC",
  "AMOJU1201-RC",
  "CONC-BIB1",
  "CBKEG01",
  "CBKEG02",
  "CBK01-S",
  "CBK02-S",
  "JPKEG",
  "CANS01",
  "CANS02",
  "LOUD-CANS",
  "DCF-CANS",
  "RKTPP-3.5",
  "RKTPP-5",
  "DCFPP-3.5",
  "DCFPP-2.5",
  "MF-CHAI",
  "MF-OAT",
  "SMITH-WS-PMTCH"
]);
function skipSku(sku, name) {
  if (!sku) return true;
  if (CONSUMABLE_SKUS.has(sku)) return false;
  const up = sku.toUpperCase();
  for (const p of SKIP_PREFIXES) if (up.startsWith(p.toUpperCase())) return true;
  for (const s of SKIP_SUFFIXES) if (up.endsWith(s.toUpperCase())) return true;
  const nl = name.toLowerCase();
  for (const f of SKIP_FRAGS) if (nl.includes(f)) return true;
  return false;
}
__name(skipSku, "skipSku");
function classifySku(sku, name) {
  const up = (sku || "").toUpperCase();
  const nl = (name || "").toLowerCase();
  if (up.endsWith("501") || nl.includes("- 5lb")) return ["lbs", "bag", 5];
  if (up.endsWith("201") && !up.endsWith("201-D")) return ["lbs", "bag", 2];
  if (up.endsWith("-LM") || nl.includes("local market case")) return ["lbs", "case", 12];
  if (up.endsWith("-RC") || nl.includes("retail case")) return ["lbs", "case", 4.5];
  if (up.startsWith("CONC-")) return ["boxes", "case", 2];
  if (up.startsWith("CBKEG") || up.startsWith("CBK0") || up.startsWith("JPKEG")) return ["kegs", "keg", 1];
  if (up.includes("CANS") || up.endsWith("-CANS")) return ["cans", "case", 12];
  if (up.includes("PP-") || up.includes("RKTPP") || up.includes("DCFPP")) return ["boxes", "box", 1];
  if (up.includes("MF-CHAI") || up.includes("CHAI") && up.includes("MF")) return ["cartons", "case", 4];
  if (up.includes("OAT")) return ["cartons", "case", 6];
  if (up.includes("PMTCH") || up.includes("MATCHA")) return ["tins", "case", 12];
  return ["units", "unit", 1];
}
__name(classifySku, "classifySku");
var ITEM_ORDER = { lbs: 0, boxes: 1, kegs: 2, cans: 3, cartons: 4, tins: 5 };
var CATALOG_TAGS = /* @__PURE__ */ new Set([
  "standard-cafe",
  "standard-cafe-cans-kegs",
  "standard-cafe-pp-cans-kegs",
  "standard-cafe-pp-cans-sankey",
  "standard-cafe-pp-cans-dlv",
  "standard-cafe-pp-cans-ship",
  "standard-cafe-pp",
  "standard-cafe-cans-sankey",
  "standard-cafe-cans-dlv",
  "standard-cafe-cans-ship",
  "local-market",
  "local-market-cans"
]);
function skuMatchesCatalog(sku, name, catalogTag) {
  const tag = (catalogTag || "").toLowerCase();
  if (!tag) return false;
  const [u, o] = classifySku(sku, name);
  const isCafe = tag.startsWith("standard-cafe");
  const isMarket = tag.startsWith("local-market");
  const hasCans = tag.includes("cans");
  const hasKegs = tag.includes("kegs") || tag.includes("sankey");
  const hasPp = tag.includes("-pp");
  if (isCafe) {
    if (u === "lbs" && o === "bag") return true;
    if (u === "cartons" || u === "tins") return true;
    if (u === "cans" && hasCans) return true;
    if (u === "kegs" && hasKegs) return true;
    if (u === "boxes" && o === "box" && hasPp) return true;
    if (u === "boxes" && o === "case" && (hasKegs || hasCans)) return true;
  }
  if (isMarket) {
    if (u === "lbs" && o === "case") return true;
    if (u === "cans" && hasCans) return true;
  }
  return false;
}
__name(skuMatchesCatalog, "skuMatchesCatalog");

// src/aggregate.ts
var WINDOW_DAYS = 90;
var KEY_SEP = "\0";
var locKey = /* @__PURE__ */ __name((cname, lname) => `${cname}${KEY_SEP}${lname}`, "locKey");
var skuKey = /* @__PURE__ */ __name((cname, lname, sku) => `${cname}${KEY_SEP}${lname}${KEY_SEP}${sku}`, "skuKey");
function buildWindows(today, daysBefore2) {
  const w = /* @__PURE__ */ __name((since, until) => ({
    since: daysBefore2(today, since),
    until: until === 0 ? today : daysBefore2(today, until),
    untilInclusive: until === 0
  }), "w");
  return {
    qty60: w(90, 0),
    qw3l: w(21, 0),
    qw3p: w(42, 21),
    qly_w3l: w(386, 365),
    qly_w3p: w(407, 386),
    qw7l: w(49, 0),
    qw7p: w(98, 49),
    qly_w7l: w(414, 365),
    qly_w7p: w(463, 414),
    membership30d: w(30, 0)
  };
}
__name(buildWindows, "buildWindows");
function accumulateFromOrders(orders, windows, deliveryDays, ianaTz, truncatedOrderCount) {
  const locs = /* @__PURE__ */ new Map();
  const activeSet = /* @__PURE__ */ new Set();
  const lastByDay = /* @__PURE__ */ new Map();
  const ensureLoc = /* @__PURE__ */ __name((cname, lname) => {
    const key = locKey(cname, lname);
    let loc = locs.get(key);
    if (!loc) {
      loc = {
        companyName: cname,
        locationName: lname,
        deliveryDay: deliveryDays.get(key) ?? "wednesday",
        skus: /* @__PURE__ */ new Map()
      };
      locs.set(key, loc);
    }
    return loc;
  }, "ensureLoc");
  for (const order of orders) {
    const cname = (order.companyName ?? "").trim();
    const lname = (order.locationName ?? "").trim();
    if (!cname || INTERNAL_NAMES.has(cname)) continue;
    const localDate = localDateString(order.processedAt, ianaTz);
    if (inWindow(localDate, windows.membership30d)) {
      activeSet.add(locKey(cname, lname));
    }
    for (const li of order.lineItems) {
      const sku = (li.sku ?? "").trim();
      const name = (li.title ?? "").trim();
      const qty = li.currentQuantity;
      if (!sku || !qty) continue;
      if (skipSku(sku, name)) continue;
      const loc = ensureLoc(cname, lname);
      let bucket = loc.skus.get(sku);
      if (!bucket) {
        bucket = { name, qty60: 0, qw3l: 0, qw3p: 0, qly_w3l: 0, qly_w3p: 0, qw7l: 0, qw7p: 0, qly_w7l: 0, qly_w7p: 0 };
        loc.skus.set(sku, bucket);
      }
      if (!bucket.name) bucket.name = name;
      if (inWindow(localDate, windows.qty60)) bucket.qty60 += qty;
      if (inWindow(localDate, windows.qw3l)) bucket.qw3l += qty;
      if (inWindow(localDate, windows.qw3p)) bucket.qw3p += qty;
      if (inWindow(localDate, windows.qly_w3l)) bucket.qly_w3l += qty;
      if (inWindow(localDate, windows.qly_w3p)) bucket.qly_w3p += qty;
      if (inWindow(localDate, windows.qw7l)) bucket.qw7l += qty;
      if (inWindow(localDate, windows.qw7p)) bucket.qw7p += qty;
      if (inWindow(localDate, windows.qly_w7l)) bucket.qly_w7l += qty;
      if (inWindow(localDate, windows.qly_w7p)) bucket.qly_w7p += qty;
      if (inWindow(localDate, windows.qty60)) {
        const key = skuKey(cname, lname, sku);
        let byDay = lastByDay.get(key);
        if (!byDay) {
          byDay = /* @__PURE__ */ new Map();
          lastByDay.set(key, byDay);
        }
        byDay.set(localDate, (byDay.get(localDate) ?? 0) + qty);
      }
    }
  }
  const lastLookup = /* @__PURE__ */ new Map();
  for (const [key, byDay] of lastByDay) {
    let bestDate = "";
    let bestQty = 0;
    for (const [date, qty] of byDay) {
      if (date > bestDate) {
        bestDate = date;
        bestQty = qty;
      }
    }
    lastLookup.set(key, { date: bestDate, qty: bestQty });
  }
  return { locs, activeSet, lastLookup, truncatedOrderCount };
}
__name(accumulateFromOrders, "accumulateFromOrders");
var round3 = /* @__PURE__ */ __name((x) => Math.round(x * 1e3) / 1e3, "round3");
function computeBenchmarks(locs) {
  const locCounts = /* @__PURE__ */ new Map();
  for (const loc of locs.values()) {
    locCounts.set(loc.companyName, (locCounts.get(loc.companyName) ?? 0) + 1);
  }
  const skuRates = /* @__PURE__ */ new Map();
  for (const loc of locs.values()) {
    if (locCounts.get(loc.companyName) !== 1) continue;
    for (const [sku, sk] of loc.skus) {
      if (sk.qw3l > 0 && sk.qw3p > 0) {
        if (!skuRates.has(sku)) skuRates.set(sku, []);
        skuRates.get(sku).push(sk.qw3l / 21);
      }
    }
  }
  const benchmarks = /* @__PURE__ */ new Map();
  for (const [sku, rates] of skuRates) {
    benchmarks.set(sku, round3(rates.reduce((a, b) => a + b, 0) / rates.length));
  }
  return benchmarks;
}
__name(computeBenchmarks, "computeBenchmarks");
function collectSkuNames(locs) {
  const names = /* @__PURE__ */ new Map();
  for (const loc of locs.values()) {
    for (const [sku, sk] of loc.skus) {
      if (!names.has(sku) && sk.name) names.set(sku, sk.name);
    }
  }
  return names;
}
__name(collectSkuNames, "collectSkuNames");
function buildJson(locs, lastLookup, benchmarks, activeSet, newCompanies, skuNames, todayIso, daysBeforeIso) {
  const byCompany = /* @__PURE__ */ new Map();
  const getCompany = /* @__PURE__ */ __name((name) => {
    let c = byCompany.get(name);
    if (!c) {
      c = { name, locations: [] };
      byCompany.set(name, c);
    }
    return c;
  }, "getCompany");
  for (const loc of locs.values()) {
    const items = [];
    for (const [sku, sk] of loc.skus) {
      if (sk.qty60 === 0) continue;
      const w3l = sk.qw3l, w3p = sk.qw3p;
      let avgDaily;
      if (w3p > 0) {
        avgDaily = w3l > 0 ? round3(w3l / 21) : round3(w3p / 21);
      } else if (w3l > 0) {
        const bench = benchmarks.get(sku) ?? 0;
        avgDaily = bench > 0 ? round3(bench) : round3(w3l / 21);
      } else {
        avgDaily = round3(sk.qty60 / WINDOW_DAYS);
      }
      const lyW3l = sk.qly_w3l, lyW3p = sk.qly_w3p;
      const w7l = sk.qw7l, w7p = sk.qw7p;
      const lyW7l = sk.qly_w7l, lyW7p = sk.qly_w7p;
      const w3 = w3p > 0 ? round3(w3l / w3p - 1) : 0;
      const lyW3 = lyW3p > 0 ? round3(lyW3l / lyW3p - 1) : 0;
      const w7 = w7p > 0 ? round3(w7l / w7p - 1) : 0;
      const lyW7 = lyW7p > 0 ? round3(lyW7l / lyW7p - 1) : 0;
      const yoyValid = lyW3l >= 1 && (w3l === 0 || lyW3l / w3l >= 0.1);
      const yoy = yoyValid ? round3((w3l - lyW3l) / lyW3l) : 0;
      const [u, o, upo] = classifySku(sku, sk.name);
      const key = skuKey(loc.companyName, loc.locationName, sku);
      let lastDate = lastLookup.get(key)?.date ?? "";
      let lastQty = lastLookup.get(key)?.qty ?? 0;
      if (!lastDate) {
        if (w3l > 0) lastDate = daysBeforeIso(todayIso, 10);
        else if (sk.qw3p > 0) lastDate = daysBeforeIso(todayIso, 31);
        else if (sk.qty60 > 0) lastDate = daysBeforeIso(todayIso, 51);
      }
      items.push({
        sku,
        name: sk.name,
        usageUnit: u,
        orderUnit: o,
        unitsPerOrder: upo,
        avgDaily,
        yoy,
        w3,
        ly_w3: lyW3,
        w7,
        ly_w7: lyW7,
        qw3l: w3l,
        qly_w3l: lyW3l,
        lastOrderDate: lastDate,
        lastOnHand: lastQty
      });
    }
    if (!items.length) continue;
    items.sort((a, b) => (ITEM_ORDER[a.usageUnit] ?? 9) - (ITEM_ORDER[b.usageUnit] ?? 9) || a.name.localeCompare(b.name));
    const c = getCompany(loc.companyName);
    const reviewStatus = activeSet.has(locKey(loc.companyName, loc.locationName)) ? "active" : "needs-review";
    c.locations.push({
      id: c.locations.length,
      name: loc.locationName,
      deliveryDay: loc.deliveryDay || "wednesday",
      daysOpen: 7,
      safetyDays: 3,
      reviewStatus,
      items
    });
  }
  for (const co of newCompanies) {
    const cname = co.name.trim();
    if (!cname || byCompany.has(cname)) continue;
    if (!co.catalogTag) continue;
    const items = [];
    const sortedBench = [...benchmarks.entries()].sort((a, b) => b[1] - a[1]);
    for (const [sku, rate] of sortedBench) {
      const sname = skuNames.get(sku) ?? sku;
      if (!skuMatchesCatalog(sku, sname, co.catalogTag)) continue;
      const [u, o, upo] = classifySku(sku, sname);
      items.push({
        sku,
        name: sname,
        usageUnit: u,
        orderUnit: o,
        unitsPerOrder: upo,
        avgDaily: rate,
        yoy: 0,
        w3: 0,
        ly_w3: 0,
        w7: 0,
        ly_w7: 0,
        qw3l: 0,
        qly_w3l: 0,
        lastOrderDate: "",
        lastOnHand: 0,
        weekZero: true
      });
    }
    if (!items.length) continue;
    items.sort((a, b) => (ITEM_ORDER[a.usageUnit] ?? 9) - (ITEM_ORDER[b.usageUnit] ?? 9) || a.name.localeCompare(b.name));
    const c = getCompany(cname);
    for (const locName of co.locations.length ? co.locations : [cname]) {
      c.locations.push({
        id: c.locations.length,
        name: locName,
        deliveryDay: "wednesday",
        daysOpen: 7,
        safetyDays: 5,
        reviewStatus: "new",
        items
      });
    }
  }
  const companies = [];
  const sortedCompanies = [...byCompany.values()].filter((c) => c.locations.length).sort((a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase()));
  sortedCompanies.forEach((c, idx) => companies.push({ id: idx, name: c.name, locations: c.locations }));
  companies.push({ id: companies.length, name: "Atomic Coffee Roasters (Internal)", isInternal: true, locations: [] });
  return companies;
}
__name(buildJson, "buildJson");
function buildPayload(companies, generatedIso) {
  return { generated: generatedIso, windowDays: WINDOW_DAYS, companies };
}
__name(buildPayload, "buildPayload");
function loadDeliveryDays(existing) {
  const lookup = /* @__PURE__ */ new Map();
  if (!existing) return lookup;
  for (const company of existing.companies) {
    for (const loc of company.locations) {
      lookup.set(locKey(company.name, loc.name), loc.deliveryDay || "wednesday");
    }
  }
  return lookup;
}
__name(loadDeliveryDays, "loadDeliveryDays");

// src/guardrails.ts
function runGuardrails(newCompanies, prev) {
  const failures = [];
  if (!prev) {
    if (!newCompanies.some((c) => !c.isInternal)) failures.push("no companies produced on first run");
    return { ok: failures.length === 0, failures };
  }
  const prevReal = prev.companies.filter((c) => !c.isInternal);
  const newReal = newCompanies.filter((c) => !c.isInternal);
  const prevCoCount = prevReal.length;
  const newCoCount = newReal.length;
  const prevLocCount = prevReal.reduce((n, c) => n + c.locations.length, 0);
  const newLocCount = newReal.reduce((n, c) => n + c.locations.length, 0);
  const sumQw3l = /* @__PURE__ */ __name((companies) => companies.reduce((n, c) => n + c.locations.reduce((m, l) => m + l.items.reduce((k, i) => k + i.qw3l, 0), 0), 0), "sumQw3l");
  const prevQty = sumQw3l(prevReal);
  const newQty = sumQw3l(newReal);
  const activeRatio = newLocCount === 0 ? 0 : newReal.reduce((n, c) => n + c.locations.filter((l) => l.reviewStatus === "active").length, 0) / newLocCount;
  if (!(0.8 * prevCoCount <= newCoCount && newCoCount <= 1.25 * prevCoCount)) {
    failures.push(`company count ${newCoCount} outside +/-20% of prior ${prevCoCount}`);
  }
  if (!(0.8 * prevLocCount <= newLocCount && newLocCount <= 1.25 * prevLocCount)) {
    failures.push(`location count ${newLocCount} outside +/-20% of prior ${prevLocCount}`);
  }
  if (newQty === 0 || !(0.5 * prevQty <= newQty && newQty <= 2 * prevQty)) {
    failures.push(`total qw3l ${newQty} outside [0.5x,2x] of prior ${prevQty}`);
  }
  if (activeRatio < 0.4) {
    failures.push(`active-location ratio ${activeRatio.toFixed(2)} below 0.40 -- likely broken 30d membership calc`);
  }
  const last = newCompanies[newCompanies.length - 1];
  if (!last || last.name !== "Atomic Coffee Roasters (Internal)" || !last.isInternal) {
    failures.push("internal company entry missing or misplaced");
  }
  return { ok: failures.length === 0, failures };
}
__name(runGuardrails, "runGuardrails");

// src/index.ts
var DATA_KEY = "data.json";
var LAST_ERROR_KEY = "last_error";
var STATUS_KEY = "refresh_status";
var RUNNING_LOCK_MS = 15 * 60 * 1e3;
var NEW_SIGNUP_EXCLUDE = /* @__PURE__ */ new Set([...INTERNAL_NAMES, "Test Cafe"]);
var JSON_HEADERS = { "Content-Type": "application/json", "Cache-Control": "no-store" };
var src_default = {
  async scheduled(_event, env, ctx) {
    const easternHour = Number(
      new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "2-digit", hourCycle: "h23" }).format(/* @__PURE__ */ new Date())
    );
    if (easternHour !== 0) return;
    ctx.waitUntil(runRefreshTracked(env, "cron"));
  },
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/api/data.json") {
      const stored = await env.PAR_DATA.get(DATA_KEY);
      if (!stored) {
        return new Response(JSON.stringify({ error: "no data available yet" }), { status: 503, headers: JSON_HEADERS });
      }
      return new Response(stored, { headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } });
    }
    if (url.pathname === "/api/status") {
      const status = await getStatus(env);
      return new Response(JSON.stringify(status ?? { state: "idle" }), { headers: JSON_HEADERS });
    }
    if (url.pathname === "/api/refresh" && request.method === "POST") {
      const existing = await getStatus(env);
      if (existing?.state === "running" && !isStale(existing)) {
        return new Response(JSON.stringify({ ...existing, alreadyRunning: true }), { status: 202, headers: JSON_HEADERS });
      }
      const started = { state: "running", trigger: "manual", startedAt: (/* @__PURE__ */ new Date()).toISOString() };
      await env.PAR_DATA.put(STATUS_KEY, JSON.stringify(started));
      ctx.waitUntil(runRefreshTracked(env, "manual"));
      return new Response(JSON.stringify(started), { status: 202, headers: JSON_HEADERS });
    }
    return new Response("Not found", { status: 404 });
  }
};
async function getStatus(env) {
  const raw = await env.PAR_DATA.get(STATUS_KEY);
  return raw ? JSON.parse(raw) : null;
}
__name(getStatus, "getStatus");
function isStale(status) {
  return Date.now() - new Date(status.startedAt).getTime() > RUNNING_LOCK_MS;
}
__name(isStale, "isStale");
async function runRefreshTracked(env, trigger) {
  const startedAt = (/* @__PURE__ */ new Date()).toISOString();
  await env.PAR_DATA.put(STATUS_KEY, JSON.stringify({ state: "running", trigger, startedAt }));
  try {
    const result = await runRefresh(env);
    const finished = result.ok ? { state: "ok", trigger, startedAt, finishedAt: (/* @__PURE__ */ new Date()).toISOString(), summary: result.summary } : { state: "error", trigger, startedAt, finishedAt: (/* @__PURE__ */ new Date()).toISOString(), failures: result.failures };
    await env.PAR_DATA.put(STATUS_KEY, JSON.stringify(finished));
  } catch (err) {
    const finished = {
      state: "error",
      trigger,
      startedAt,
      finishedAt: (/* @__PURE__ */ new Date()).toISOString(),
      error: err?.message ?? String(err)
    };
    await env.PAR_DATA.put(STATUS_KEY, JSON.stringify(finished));
  }
}
__name(runRefreshTracked, "runRefreshTracked");
async function runRefresh(env) {
  const token = await getAccessToken(env);
  const client = makeClient(env, token);
  const ianaTz = await fetchShopTimezone(client);
  const today = todayLocal(ianaTz);
  const windows = buildWindows(today, daysBefore);
  const sinceDate = daysBefore(today, 463 + 14);
  const { orders, truncatedOrderCount } = await fetchAllOrders(client, sinceDate);
  const existingRaw = await env.PAR_DATA.get(DATA_KEY);
  const existing = existingRaw ? JSON.parse(existingRaw) : null;
  const deliveryDays = loadDeliveryDays(existing);
  const { locs, activeSet, lastLookup } = accumulateFromOrders(orders, windows, deliveryDays, ianaTz, truncatedOrderCount);
  const benchmarks = computeBenchmarks(locs);
  const skuNames = collectSkuNames(locs);
  const cutoffDate = daysBefore(today, 60 + 2);
  const rawSignups = await fetchNewSignups(client, cutoffDate, NEW_SIGNUP_EXCLUDE, CATALOG_TAGS);
  const cutoffExact = daysBefore(today, 60);
  const newCompanies = rawSignups.filter((co) => {
    const createdLocal = co.createdAt.slice(0, 10);
    return createdLocal >= cutoffExact;
  });
  const generatedIso = (/* @__PURE__ */ new Date()).toISOString();
  const companies = buildJson(locs, lastLookup, benchmarks, activeSet, newCompanies, skuNames, today, daysBefore);
  const payload = buildPayload(companies, generatedIso);
  const guard = runGuardrails(companies, existing);
  if (!guard.ok) {
    await env.PAR_DATA.put(
      LAST_ERROR_KEY,
      JSON.stringify({ at: generatedIso, failures: guard.failures, truncatedOrderCount })
    );
    return { ok: false, failures: guard.failures };
  }
  await env.PAR_DATA.put(DATA_KEY, JSON.stringify(payload));
  const realCompanies = companies.filter((c) => !c.isInternal);
  const nLocs = realCompanies.reduce((n, c) => n + c.locations.length, 0);
  const statusCounts = { active: 0, "needs-review": 0, new: 0 };
  for (const c of realCompanies) for (const l of c.locations) statusCounts[l.reviewStatus] = (statusCounts[l.reviewStatus] ?? 0) + 1;
  const summary = `data.json written -- ${realCompanies.length} companies, ${nLocs} locations (active=${statusCounts.active} needs-review=${statusCounts["needs-review"]} new=${statusCounts.new})` + (truncatedOrderCount > 0 ? ` [warning: ${truncatedOrderCount} orders had >50 line items, truncated]` : "");
  return { ok: true, summary };
}
__name(runRefresh, "runRefresh");

// ../../../../.npm/_npx/d77349f55c2be1c0/node_modules/wrangler/templates/middleware/middleware-ensure-req-body-drained.ts
var drainBody = /* @__PURE__ */ __name(async (request, env, _ctx, middlewareCtx) => {
  try {
    return await middlewareCtx.next(request, env);
  } finally {
    try {
      if (request.body !== null && !request.bodyUsed) {
        const reader = request.body.getReader();
        while (!(await reader.read()).done) {
        }
      }
    } catch (e) {
      console.error("Failed to drain the unused request body.", e);
    }
  }
}, "drainBody");
var middleware_ensure_req_body_drained_default = drainBody;

// ../../../../.npm/_npx/d77349f55c2be1c0/node_modules/wrangler/templates/middleware/middleware-miniflare3-json-error.ts
function reduceError(e) {
  return {
    name: e?.name,
    message: e?.message ?? String(e),
    stack: e?.stack,
    cause: e?.cause === void 0 ? void 0 : reduceError(e.cause)
  };
}
__name(reduceError, "reduceError");
var jsonError = /* @__PURE__ */ __name(async (request, env, _ctx, middlewareCtx) => {
  try {
    return await middlewareCtx.next(request, env);
  } catch (e) {
    const error = reduceError(e);
    const body = JSON.stringify(error);
    const headers = {
      "Content-Type": "application/json",
      "MF-Experimental-Error-Stack": "true"
    };
    const encoded = encodeURIComponent(body);
    if (encoded.length <= 8192) {
      headers["MF-Experimental-Error-Stack-Payload"] = encoded;
    }
    return new Response(body, { status: 500, headers });
  }
}, "jsonError");
var middleware_miniflare3_json_error_default = jsonError;

// .wrangler/tmp/bundle-Bpb9X4/middleware-insertion-facade.js
var __INTERNAL_WRANGLER_MIDDLEWARE__ = [
  middleware_ensure_req_body_drained_default,
  middleware_miniflare3_json_error_default
];
var middleware_insertion_facade_default = src_default;

// ../../../../.npm/_npx/d77349f55c2be1c0/node_modules/wrangler/templates/middleware/common.ts
var __facade_middleware__ = [];
function __facade_register__(...args) {
  __facade_middleware__.push(...args.flat());
}
__name(__facade_register__, "__facade_register__");
function __facade_invokeChain__(request, env, ctx, dispatch, middlewareChain) {
  const [head, ...tail] = middlewareChain;
  const middlewareCtx = {
    dispatch,
    next(newRequest, newEnv) {
      return __facade_invokeChain__(newRequest, newEnv, ctx, dispatch, tail);
    }
  };
  return head(request, env, ctx, middlewareCtx);
}
__name(__facade_invokeChain__, "__facade_invokeChain__");
function __facade_invoke__(request, env, ctx, dispatch, finalMiddleware) {
  return __facade_invokeChain__(request, env, ctx, dispatch, [
    ...__facade_middleware__,
    finalMiddleware
  ]);
}
__name(__facade_invoke__, "__facade_invoke__");

// .wrangler/tmp/bundle-Bpb9X4/middleware-loader.entry.ts
var __Facade_ScheduledController__ = class ___Facade_ScheduledController__ {
  constructor(scheduledTime, cron, noRetry) {
    this.scheduledTime = scheduledTime;
    this.cron = cron;
    this.#noRetry = noRetry;
  }
  static {
    __name(this, "__Facade_ScheduledController__");
  }
  #noRetry;
  noRetry() {
    if (!(this instanceof ___Facade_ScheduledController__)) {
      throw new TypeError("Illegal invocation");
    }
    this.#noRetry();
  }
};
function wrapExportedHandler(worker) {
  if (__INTERNAL_WRANGLER_MIDDLEWARE__ === void 0 || __INTERNAL_WRANGLER_MIDDLEWARE__.length === 0) {
    return worker;
  }
  for (const middleware of __INTERNAL_WRANGLER_MIDDLEWARE__) {
    __facade_register__(middleware);
  }
  const fetchDispatcher = /* @__PURE__ */ __name(function(request, env, ctx) {
    if (worker.fetch === void 0) {
      throw new Error("Handler does not export a fetch() function.");
    }
    return worker.fetch(request, env, ctx);
  }, "fetchDispatcher");
  return {
    ...worker,
    fetch(request, env, ctx) {
      const dispatcher = /* @__PURE__ */ __name(function(type, init) {
        if (type === "scheduled" && worker.scheduled !== void 0) {
          const controller = new __Facade_ScheduledController__(
            Date.now(),
            init.cron ?? "",
            () => {
            }
          );
          return worker.scheduled(controller, env, ctx);
        }
      }, "dispatcher");
      return __facade_invoke__(request, env, ctx, dispatcher, fetchDispatcher);
    }
  };
}
__name(wrapExportedHandler, "wrapExportedHandler");
function wrapWorkerEntrypoint(klass) {
  if (__INTERNAL_WRANGLER_MIDDLEWARE__ === void 0 || __INTERNAL_WRANGLER_MIDDLEWARE__.length === 0) {
    return klass;
  }
  for (const middleware of __INTERNAL_WRANGLER_MIDDLEWARE__) {
    __facade_register__(middleware);
  }
  return class extends klass {
    #fetchDispatcher = /* @__PURE__ */ __name((request, env, ctx) => {
      this.env = env;
      this.ctx = ctx;
      if (super.fetch === void 0) {
        throw new Error("Entrypoint class does not define a fetch() function.");
      }
      return super.fetch(request);
    }, "#fetchDispatcher");
    #dispatcher = /* @__PURE__ */ __name((type, init) => {
      if (type === "scheduled" && super.scheduled !== void 0) {
        const controller = new __Facade_ScheduledController__(
          Date.now(),
          init.cron ?? "",
          () => {
          }
        );
        return super.scheduled(controller);
      }
    }, "#dispatcher");
    fetch(request) {
      return __facade_invoke__(
        request,
        this.env,
        this.ctx,
        this.#dispatcher,
        this.#fetchDispatcher
      );
    }
  };
}
__name(wrapWorkerEntrypoint, "wrapWorkerEntrypoint");
var WRAPPED_ENTRY;
if (typeof middleware_insertion_facade_default === "object") {
  WRAPPED_ENTRY = wrapExportedHandler(middleware_insertion_facade_default);
} else if (typeof middleware_insertion_facade_default === "function") {
  WRAPPED_ENTRY = wrapWorkerEntrypoint(middleware_insertion_facade_default);
}
var middleware_loader_entry_default = WRAPPED_ENTRY;
export {
  __INTERNAL_WRANGLER_MIDDLEWARE__,
  middleware_loader_entry_default as default
};
//# sourceMappingURL=index.js.map
