// PAR Tool — Phase 2 On-Hand Write Endpoint
// Deploy as: Google Apps Script Web App (Execute as: Me, Access: Anyone)
// "Anyone" is required so the static PAR tool can call this without a Google login —
// SHARED_SECRET below is the real authorization boundary, not the Access setting.
// Sheet ID: 11iLeJILCJ_ZZE7d96omWV6Awki7xJGAuUpHy_EjpxeI

const SHEET_ID = '11iLeJILCJ_ZZE7d96omWV6Awki7xJGAuUpHy_EjpxeI';
const STALE_WEEKS  = 2;   // flag as stale after this many weeks
const EXPIRE_WEEKS = 4;   // drop from active use after this many weeks
const EWMA_HALF_LIFE_WEEKS = 3; // recent weeks weighted higher than avg4/avg12; tune here

// Must match PHASE2_SECRET in par-ordering/index.html. Rotate by changing both,
// then Deploy → Manage deployments → New version (see DEPLOY.md).
const SHARED_SECRET = '8e9deda99d6e5cdaf70adee3310d4b62f804bf3c6788bcc0';

// ── Fiscal week helper ─────────────────────────────────────────────────────
function getFiscalWeek(date) {
  // Atomic fiscal year starts first Monday of January
  const d = date || new Date();
  const jan1 = new Date(d.getFullYear(), 0, 1);
  const firstMonday = new Date(jan1);
  firstMonday.setDate(jan1.getDate() + ((8 - jan1.getDay()) % 7 || 7));
  const diff = d - firstMonday;
  const week = Math.floor(diff / (7 * 24 * 60 * 60 * 1000)) + 1;
  return Math.max(1, Math.min(52, week));
}

// ── Main POST handler ──────────────────────────────────────────────────────
function doPost(e) {
  let payload;
  try {
    payload = JSON.parse(e.postData.contents);
    const action = payload.action;

    if (payload.secret !== SHARED_SECRET) {
      logError('doPost:auth', 'Invalid or missing secret', payload);
      return respond(401, { error: 'Unauthorized' });
    }

    if (action === 'writeOnHand') {
      return writeOnHand(payload);
    } else if (action === 'getUsageSummary') {
      return getUsageSummary(payload);
    } else {
      return respond(400, { error: 'Unknown action: ' + action });
    }
  } catch (err) {
    logError('doPost', err.message, payload);
    return respond(500, { error: err.message });
  }
}

function doGet(e) {
  // Allow GET for usage summary lookups
  try {
    if (e.parameter.secret !== SHARED_SECRET) {
      logError('doGet:auth', 'Invalid or missing secret', e.parameter);
      return respond(401, { error: 'Unauthorized' });
    }
    const company_id  = e.parameter.company_id;
    const location_id = e.parameter.location_id;
    if (company_id && location_id) {
      return getUsageSummaryByIds(company_id, location_id);
    }
    return respond(400, { error: 'Missing company_id or location_id' });
  } catch (err) {
    logError('doGet', err.message, e.parameter);
    return respond(500, { error: err.message });
  }
}

// ── Error Log — durable record of failed/unauthorized requests ────────────
function logError(context, message, payload) {
  try {
    const ss = SpreadsheetApp.openById(SHEET_ID);
    let errSheet = ss.getSheetByName('Error Log');
    if (!errSheet) {
      errSheet = ss.insertSheet('Error Log');
      errSheet.appendRow(['Timestamp', 'Context', 'Error Message', 'Payload']);
    }
    let payloadStr = '';
    try { payloadStr = JSON.stringify(payload || {}).slice(0, 500); } catch (e2) { payloadStr = String(payload); }
    errSheet.appendRow([new Date().toISOString(), context, message, payloadStr]);
  } catch (logErr) {
    // Logging must never throw and block the real response.
  }
}

// ── Write on-hand entry ────────────────────────────────────────────────────
function writeOnHand(payload) {
  const required = ['company_id','company_name','location_id','location_name','sku','on_hand_qty'];
  for (const f of required) {
    if (payload[f] === undefined || payload[f] === '') {
      return respond(400, { error: 'Missing field: ' + f });
    }
  }

  const ss        = SpreadsheetApp.openById(SHEET_ID);
  const logSheet  = ss.getSheetByName('On Hand Log');
  const now       = new Date();
  const fw        = 'FW' + getFiscalWeek(now);
  const timestamp = now.toISOString();

  logSheet.appendRow([
    payload.company_id,
    payload.company_name,
    payload.location_id,
    payload.location_name,
    payload.sku,
    fw,
    Number(payload.on_hand_qty),
    timestamp,
  ]);

  // Recalculate usage for this location + sku
  recalcUsage(ss, payload.company_id, payload.location_id, payload.sku);

  // Rebuild usage summary row
  rebuildSummary(ss, payload.company_id, payload.company_name,
                     payload.location_id, payload.location_name, payload.sku);

  return respond(200, { ok: true, fiscal_week: fw, entered_at: timestamp });
}

// ── Recalculate usage log from on-hand + order history ────────────────────
function recalcUsage(ss, company_id, location_id, sku) {
  const ohLog    = ss.getSheetByName('On Hand Log');
  const usageLog = ss.getSheetByName('Usage Log');

  // Pull all on-hand entries for this location+sku, sorted by fiscal week
  const ohData = ohLog.getDataRange().getValues();
  const entries = [];
  for (let i = 1; i < ohData.length; i++) {
    const row = ohData[i];
    if (String(row[0]) === String(company_id) &&
        String(row[2]) === String(location_id) &&
        String(row[4]) === String(sku)) {
      entries.push({
        fw:  row[5],
        qty: Number(row[6]),
        ts:  new Date(row[7]),
      });
    }
  }
  if (entries.length < 2) return; // need at least 2 data points to calc usage

  entries.sort((a, b) => a.ts - b.ts);

  // For each consecutive pair, calculate usage
  // usage = on_hand_start + orders_in_period - on_hand_end
  // Orders come from data.json (already aggregated by week)
  const ordersByWeek = getOrdersByWeek(company_id, location_id, sku);

  for (let i = 1; i < entries.length; i++) {
    const prev = entries[i - 1];
    const curr = entries[i];
    const orders = ordersByWeek[curr.fw] || 0;
    const usage  = prev.qty + orders - curr.qty;
    const isEst  = false;

    // Check if this week already exists in usage log; if so update, else append
    const usageData = usageLog.getDataRange().getValues();
    let found = false;
    for (let j = 1; j < usageData.length; j++) {
      const r = usageData[j];
      if (String(r[0]) === String(company_id) &&
          String(r[2]) === String(location_id) &&
          String(r[4]) === String(sku) &&
          String(r[5]) === String(curr.fw)) {
        usageLog.getRange(j + 1, 7, 1, 5).setValues([[
          prev.qty, orders, curr.qty, Math.max(0, usage), isEst
        ]]);
        found = true;
        break;
      }
    }
    if (!found) {
      usageLog.appendRow([
        company_id, entries[i].fw ? '' : '',  // company_name filled below
        location_id, '',
        sku, curr.fw,
        prev.qty, orders, curr.qty,
        Math.max(0, usage), isEst,
      ]);
    }
  }
}

// ── Get orders for a location+sku from data.json (via Drive) ──────────────
function getOrdersByWeek(company_id, location_id, sku) {
  // Returns {FW1: 5, FW3: 10, ...} from PAR data.json
  // For now returns empty — will be wired to data.json in a future step
  // when data.json is updated to include fiscal week order history
  return {};
}

// ── Exponentially weighted average — recent weeks count more than old ones ──
// Discrete form of the exponential-decay equation (dy/dt = -k(y - x(t))).
// halfLifeWeeks controls how fast old weeks fade: lower = more reactive to
// a recent ramp-up/down, higher = closer to a flat average like avg4/avg12.
function ewma(usages, halfLifeWeeks) {
  if (!usages.length) return null;
  const alpha = 1 - Math.exp(-Math.LN2 / halfLifeWeeks);
  let acc = usages[0];
  for (let i = 1; i < usages.length; i++) {
    acc = alpha * usages[i] + (1 - alpha) * acc;
  }
  return acc;
}

// ── Trend slope — simple discrete derivative over the last 3 weeks ─────────
// Positive = usage ramping up, negative = sliding down. Early-warning signal
// that shows up before weeksSince crosses the STALE/EXPIRE thresholds.
function trendSlope(usages) {
  const last3 = usages.slice(-3);
  if (last3.length < 3) return null;
  return (last3[2] - last3[0]) / 2;
}

// ── Rebuild usage summary for one location+sku ───────────────────────────
function rebuildSummary(ss, company_id, company_name, location_id, location_name, sku) {
  const usageLog     = ss.getSheetByName('Usage Log');
  const summarySheet = ss.getSheetByName('Usage Summary');
  const ohLog        = ss.getSheetByName('On Hand Log');
  const now          = new Date();

  // Pull usage log entries for this location+sku
  const usageData = usageLog.getDataRange().getValues();
  const usages = [];
  for (let i = 1; i < usageData.length; i++) {
    const r = usageData[i];
    if (String(r[0]) === String(company_id) &&
        String(r[2]) === String(location_id) &&
        String(r[4]) === String(sku)) {
      usages.push(Number(r[9])); // calculated_usage col
    }
  }

  // 4W and 12W averages
  const last4  = usages.slice(-4);
  const last12 = usages.slice(-12);
  const avg4   = last4.length  ? last4.reduce((a,b)=>a+b,0)  / last4.length  : null;
  const avg12  = last12.length ? last12.reduce((a,b)=>a+b,0) / last12.length : null;
  const avgEwma = ewma(usages, EWMA_HALF_LIFE_WEEKS);
  const trend   = trendSlope(usages);

  // Last on-hand entry
  const ohData = ohLog.getDataRange().getValues();
  let lastOH = null, lastOHDate = null;
  for (let i = 1; i < ohData.length; i++) {
    const r = ohData[i];
    if (String(r[0]) === String(company_id) &&
        String(r[2]) === String(location_id) &&
        String(r[4]) === String(sku)) {
      const ts = new Date(r[7]);
      if (!lastOHDate || ts > lastOHDate) {
        lastOHDate = ts;
        lastOH     = Number(r[6]);
      }
    }
  }

  // Weeks since last on-hand
  const weeksSince = lastOHDate
    ? Math.floor((now - lastOHDate) / (7 * 24 * 60 * 60 * 1000))
    : null;

  // Suggested order = 4W avg − current on-hand (floor 0)
  const suggested = avg4 !== null && lastOH !== null && weeksSince !== null && weeksSince <= EXPIRE_WEEKS
    ? Math.max(0, Math.round(avg4 - lastOH))
    : null;

  // Find existing summary row or append
  const summaryData = summarySheet.getDataRange().getValues();
  let found = false;
  for (let i = 1; i < summaryData.length; i++) {
    const r = summaryData[i];
    if (String(r[0]) === String(company_id) &&
        String(r[2]) === String(location_id) &&
        String(r[4]) === String(sku)) {
      summarySheet.getRange(i + 1, 1, 1, 13).setValues([[
        company_id, company_name, location_id, location_name, sku,
        avg4 !== null ? Math.round(avg4 * 100) / 100 : '',
        avg12 !== null ? Math.round(avg12 * 100) / 100 : '',
        lastOHDate ? lastOHDate.toISOString().split('T')[0] : '',
        lastOH !== null ? lastOH : '',
        weeksSince !== null ? weeksSince : '',
        suggested !== null ? suggested : '',
        avgEwma !== null ? Math.round(avgEwma * 100) / 100 : '',
        trend !== null ? Math.round(trend * 100) / 100 : '',
      ]]);
      found = true;
      break;
    }
  }
  if (!found) {
    summarySheet.appendRow([
      company_id, company_name, location_id, location_name, sku,
      avg4 !== null ? Math.round(avg4 * 100) / 100 : '',
      avg12 !== null ? Math.round(avg12 * 100) / 100 : '',
      lastOHDate ? lastOHDate.toISOString().split('T')[0] : '',
      lastOH !== null ? lastOH : '',
      weeksSince !== null ? weeksSince : '',
      suggested !== null ? suggested : '',
      avgEwma !== null ? Math.round(avgEwma * 100) / 100 : '',
      trend !== null ? Math.round(trend * 100) / 100 : '',
    ]);
  }
}

// ── Get usage summary for a location ─────────────────────────────────────
function getUsageSummaryByIds(company_id, location_id) {
  const ss           = SpreadsheetApp.openById(SHEET_ID);
  const summarySheet = ss.getSheetByName('Usage Summary');
  const data         = summarySheet.getDataRange().getValues();
  const results      = [];

  for (let i = 1; i < data.length; i++) {
    const r = data[i];
    if (String(r[0]) === String(company_id) &&
        String(r[2]) === String(location_id)) {
      results.push({
        sku:               r[4],
        avg4w:             r[5],
        avg12w:            r[6],
        last_on_hand_date: r[7],
        last_on_hand_qty:  r[8],
        weeks_since:       r[9],
        suggested_order:   r[10],
        avg_ewma:          r[11],
        trend_slope:       r[12],
      });
    }
  }

  return respond(200, { ok: true, items: results });
}

function getUsageSummary(payload) {
  return getUsageSummaryByIds(payload.company_id, payload.location_id);
}

// ── Response helper ───────────────────────────────────────────────────────
function respond(code, data) {
  return ContentService
    .createTextOutput(JSON.stringify(data))
    .setMimeType(ContentService.MimeType.JSON);
}
