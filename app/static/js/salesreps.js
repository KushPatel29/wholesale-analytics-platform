(() => {
  const root = document.getElementById("SalesRepsApp");
  if (!root) return;

  const debugEnabled = (() => {
    try {
      const raw = String(window.__APP_DEBUG__ || "").trim().toLowerCase();
      return raw === "true" || raw === "1" || raw === "development";
    } catch (_err) {
      return false;
    }
  })();
  const logWarn = (...args) => {
    if (debugEnabled) console.warn(...args);
  };
  const logError = (...args) => {
    if (debugEnabled) console.error(...args);
  };

  const authFetch = window.authFetch || window.fetch.bind(window);
  const pageCache = window.analyticsPageCache || null;
  const bundleUrl = root.dataset.bundleUrl || "/api/salesreps/bundle";
  const exportXlsx = document.getElementById("salesrepsExportXlsx");
  const exportCsv = document.getElementById("salesrepsExportCsv");
  const actionCrm = document.getElementById("salesrepsActionCrm");
  const actionSlack = document.getElementById("salesrepsActionSlack");
  const drilldownTemplate = root.dataset.drilldownTemplate || "";
  const ChartLib = window.Chart;
  const PAGE_CACHE_ID = "salesreps";
  const PAGE_CACHE_POLICY = { freshMs: 90 * 1000, maxAgeMs: 20 * 60 * 1000 };
  const LOCALE = "en-US";
  const CURRENCY = "USD";
  document.getElementById("GlobalFilters")?.classList.add("sr-global-filters");

  const TEXT_EMPTY = "None";
  /* An em dash, not "0.00".
   *
   * Every missing number on this page was printed as a measured zero. "Margin
   * Leakage 0.00" does not read as "we have no figure for this" - it reads as
   * "we checked, and there is none", which is a claim the page cannot support
   * and the opposite of what a blank means. It also hid the absence: a KPI
   * showing 0.00 looks answered, so nobody goes looking for why.
   *
   * The dash matches how the rest of the app renders an unavailable figure. */
  const NUMERIC_EMPTY = "—";
  const NA = TEXT_EMPTY;
  const fmtMoney0 = new Intl.NumberFormat(LOCALE, { style: "currency", currency: CURRENCY, maximumFractionDigits: 0 });
  const fmtMoney2 = new Intl.NumberFormat(LOCALE, { style: "currency", currency: CURRENCY, minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const fmtInt = new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 0 });
  const fmtPct = new Intl.NumberFormat(LOCALE, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  const THEME_READY = true;
  window.THEME_READY = THEME_READY;
  const SR_THEME = Object.freeze({
    espresso: "#1E293B",
    espressoSoft: "#334155",
    cream: "#FFFFFF",
    creamStrong: "#F8FAFC",
    tan: "#F1F5F9",
    tanSoft: "#E2E8F0",
    oxblood: "#720e0e",
    blood: "#991b1b",
    forest: "#059669",
    gold: "#C5A059",
    bronze: "#946c37",
    tick: "#64748B",
    grid: "#E2E8F0",
    gridStrong: "#CBD5E1",
  });
  const READABLE_REP_FALLBACK = "Needs Review";
  const UNASSIGNED_REP_FALLBACK = "Unassigned / Needs Review";
  const CHART_IDS = [
    "trendChart",
    "topRepsChart",
    "srProteinChart",
    "concentrationChart",
    "srTerritoryChart",
  ];
  const COLUMN_STORAGE_KEY = "salesreps.columnVisibility.v1";
  const DEFAULT_COLUMN_VISIBILITY = {
    revenue: true,
    profit: true,
    margin_pct: true,
    weight_lb: true,
    active_customers: true,
    current_owned_customers: true,
    inherited_customers: true,
    transferred_in_revenue: true,
    transferred_out_revenue: true,
    yoy_revenue_pct: true,
    silent_days: true,
    mom_revenue_delta: true,
    yoy_revenue_delta: true,
    territory_count: true,
    replaced_reps: false,
    top_territory: false,
    top_customer: true,
    top_protein: true,
    leakage: true,
    protein_penetration: true,
    overdue_customers: true,
    flags: true,
  };
  const SAFE_REP_BUCKET_ALIASES = new Map([
    ["unassigned", UNASSIGNED_REP_FALLBACK],
    ["unassigned / needs review", UNASSIGNED_REP_FALLBACK],
    ["unknown rep", READABLE_REP_FALLBACK],
    ["needs mapping", READABLE_REP_FALLBACK],
    ["needs review", READABLE_REP_FALLBACK],
  ]);
  const SAFE_REP_BUCKETS = new Set(Array.from(SAFE_REP_BUCKET_ALIASES.values()).map((value) => value.toLowerCase()));
  const sanitizeDisplayText = (value) =>
    String(value ?? "")
      .replace(/\[(?:\s*)?drill(?:\s*)?\]/gi, "")
      .replace(/\bdrill\b/gi, "")
      .replace(/\s{2,}/g, " ")
      .trim();

  if (ChartLib?.defaults) {
    ChartLib.defaults.color = SR_THEME.tick;
    ChartLib.defaults.borderColor = SR_THEME.grid;
    ChartLib.defaults.font.family = '"Avenir Next", "Segoe UI", system-ui, sans-serif';
    if (ChartLib.defaults.plugins?.legend?.labels) {
      ChartLib.defaults.plugins.legend.labels.color = SR_THEME.tick;
    }
    if (ChartLib.defaults.plugins?.tooltip) {
      ChartLib.defaults.plugins.tooltip.backgroundColor = "rgba(30, 41, 59, 0.94)";
      ChartLib.defaults.plugins.tooltip.titleColor = "#FFFFFF";
      ChartLib.defaults.plugins.tooltip.bodyColor = "#FFFFFF";
      ChartLib.defaults.plugins.tooltip.borderColor = "#720e0e";
      ChartLib.defaults.plugins.tooltip.borderWidth = 1;
    }
    if (ChartLib.defaults.scale) {
      ChartLib.defaults.scale.grid.color = SR_THEME.grid;
      ChartLib.defaults.scale.borderColor = SR_THEME.grid;
      ChartLib.defaults.scale.ticks.color = SR_THEME.tick;
      ChartLib.defaults.scale.title.color = SR_THEME.tick;
    }
  }

  const escapeHtml = (value) =>
    sanitizeDisplayText(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");

  const parseCssColor = (value) => {
    const input = String(value || "").trim();
    if (!input) return null;
    const rgbMatch = input.match(
      /^rgba?\(\s*([0-9.]+)\s*,\s*([0-9.]+)\s*,\s*([0-9.]+)(?:\s*[,/]\s*([0-9.]+))?\s*\)$/i,
    );
    if (rgbMatch) {
      return {
        r: Number(rgbMatch[1]),
        g: Number(rgbMatch[2]),
        b: Number(rgbMatch[3]),
        a: rgbMatch[4] == null ? 1 : Number(rgbMatch[4]),
      };
    }
    const hexMatch = input.match(/^#([0-9a-f]{3,8})$/i);
    if (!hexMatch) return null;
    const hex = hexMatch[1];
    if (hex.length === 3 || hex.length === 4) {
      return {
        r: parseInt(hex[0] + hex[0], 16),
        g: parseInt(hex[1] + hex[1], 16),
        b: parseInt(hex[2] + hex[2], 16),
        a: hex.length === 4 ? parseInt(hex[3] + hex[3], 16) / 255 : 1,
      };
    }
    if (hex.length === 6 || hex.length === 8) {
      return {
        r: parseInt(hex.slice(0, 2), 16),
        g: parseInt(hex.slice(2, 4), 16),
        b: parseInt(hex.slice(4, 6), 16),
        a: hex.length === 8 ? parseInt(hex.slice(6, 8), 16) / 255 : 1,
      };
    }
    return null;
  };

  const compositeColor = (fg, bg = { r: 255, g: 255, b: 255, a: 1 }) => {
    const alpha = fg.a + bg.a * (1 - fg.a);
    if (alpha <= 0) return { r: 255, g: 255, b: 255, a: 0 };
    return {
      r: Math.round((fg.r * fg.a + bg.r * bg.a * (1 - fg.a)) / alpha),
      g: Math.round((fg.g * fg.a + bg.g * bg.a * (1 - fg.a)) / alpha),
      b: Math.round((fg.b * fg.a + bg.b * bg.a * (1 - fg.a)) / alpha),
      a: alpha,
    };
  };

  const luminance = (color) =>
    [color.r, color.g, color.b]
      .map((channel) => {
        const normalized = channel / 255;
        return normalized <= 0.03928
          ? normalized / 12.92
          : Math.pow((normalized + 0.055) / 1.055, 2.4);
      })
      .reduce((total, channel, index) => total + channel * [0.2126, 0.7152, 0.0722][index], 0);

  const contrastRatio = (foreground, background) => {
    const lighter = Math.max(luminance(foreground), luminance(background));
    const darker = Math.min(luminance(foreground), luminance(background));
    return (lighter + 0.05) / (darker + 0.05);
  };

  const readableBadgeForeground = (background) => {
    const parsed = parseCssColor(background) || parseCssColor(SR_THEME.bronze);
    const resolvedBackground = compositeColor(parsed, { r: 255, g: 255, b: 255, a: 1 });
    const light = { r: 255, g: 255, b: 255, a: 1 };
    const dark = { r: 26, g: 15, b: 10, a: 1 };
    return contrastRatio(light, resolvedBackground) >= contrastRatio(dark, resolvedBackground) ? "#ffffff" : SR_THEME.espresso;
  };

  const healthBadgeStyle = (background, fontSize = "0.72rem") =>
    `background:${escapeHtml(background || SR_THEME.bronze)};color:${readableBadgeForeground(background)};font-size:${fontSize};font-weight:700;`;

  const state = {
    qs: "",
    page: 1,
    pageSize: 25,
    sortBy: "revenue",
    sortDir: "desc",
    search: "",
    metric: "revenue",
    trendMetric: "revenue",
    trendGrain: "monthly",
    trendView: "absolute",
    trendSelectedReps: [],
    trendFocusMode: false,
    topN: 10,
    topCustomersSortBy: "revenue",
    topCustomersSortDir: "desc",
    proteinSortBy: "revenue",
    proteinSortDir: "desc",
    attributionMode: "current_owner",
    rosterMode: "current_only",
    transferOnly: false,
    leaderboardScope: "all",
    focusedRepIds: [],
    focusedRepLabels: [],
    scrollToFocusedRep: false,
  };
  let focusedCustomer = null;
  let pendingCustomerFocus = null;
  let viewportHeightTicking = false;
  let followUpDrawerWired = false;
  let filterDrawerMounted = false;
  let filterDrawerWired = false;

  // ── 4D: Rep comparison state ──
  let selectedRepIds = new Set();
  let selectedRepRows = new Map();

  const charts = {};
  let currentAbort = null;
  let reqId = 0;
  let currentApplyId = "";
  let bootstrapped = false;
  let lastPayload = null;
  let deferredChartToken = 0;
  const deferredChartTimers = new Set();
  const renderMemo = new Map();
  const virtualTable = {
    wrapper: null,
    tbody: null,
    rows: [],
    rowHeight: 88,
    overscan: 6,
    lastRange: "",
    scheduled: false,
  };
  const customerVirtualTable = {
    wrapper: null,
    tbody: null,
    rows: [],
    rowHeight: 74,
    overscan: 8,
    lastRange: "",
    scheduled: false,
    emptyMessage: "",
  };
  let systemHealthPopover = null;

  const emptyMessage = "No data for selected filters.";

  const metricConfig = {
    revenue: { label: "Revenue", fmt: (v) => fmtMoney0.format(num(v)), value: (r) => num(r.revenue) },
    profit: { label: "Profit", fmt: (v) => fmtMoney0.format(num(v)), value: (r) => num(r.profit) },
    margin_dollar: { label: "Margin $", fmt: (v) => fmtMoney0.format(num(v)), value: (r) => num(r.profit) },
    margin_pct: { label: "Margin %", fmt: (v) => `${fmtPct.format(num(v))}%`, value: (r) => num(r.margin_pct) },
    orders: { label: "Orders", fmt: (v) => fmtInt.format(num(v)), value: (r) => num(r.orders) },
    customers: { label: "Customers", fmt: (v) => fmtInt.format(num(v)), value: (r) => num(r.customers) },
    weight_lb: { label: "Weight (LB)", fmt: (v) => fmtInt.format(num(v)), value: (r) => num(r.weight_lb) },
  };

  const num = (v, fallback = 0) => {
    const n = Number(v);
    return Number.isFinite(n) ? n : fallback;
  };

  const opt = (v) => {
    if (v === null || v === undefined || v === "") return null;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  };

  const pct = (v, fromShare = false) => {
    const n = opt(v);
    if (n === null) return `${fmtPct.format(0)}%`;
    const val = fromShare && n <= 1.01 ? n * 100 : n;
    return `${fmtPct.format(val)}%`;
  };

  const fmtSignedPoints = (value) => {
    const numeric = opt(value);
    if (numeric === null) return NUMERIC_EMPTY;
    return `${numeric > 0 ? "+" : ""}${fmtPct.format(numeric)} pts`;
  };

  const money = (v, compact = true) => {
    const n = opt(v);
    if (n === null) return NUMERIC_EMPTY;
    return compact ? fmtMoney0.format(n) : fmtMoney2.format(n);
  };

  /* `Jul 6, 2026`, matching every other page and the server. This rendered
     `08/09/2026` - a different date format from the rest of the app, and
     ambiguous to half the world. */
  const formatDateCA = (raw) => {
    if (!raw) return TEXT_EMPTY;
    return window.WAFormat.day(raw, { missing: TEXT_EMPTY });
  };

  /* "Now" is the data cutoff, not the browser clock. Ageing an account against
     the wall clock on a dataset that stops earlier makes every account look
     dormant - the same fault that emptied this page's KPIs. */
  const referenceDate = () => {
    const through = document.body?.dataset?.dataThrough;
    if (through) {
      const parsed = new Date(`${through}T00:00:00`);
      if (!Number.isNaN(parsed.valueOf())) return parsed;
    }
    return new Date();
  };

  const silentAge = (rawDate, explicitDays = null) => {
    const direct = opt(explicitDays);
    if (direct !== null) {
      return {
        days: Math.max(Math.round(direct), 0),
        dateLabel: rawDate ? formatDateCA(rawDate) : null,
      };
    }
    if (!rawDate) return { days: null, dateLabel: null };
    const dt = new Date(rawDate);
    if (Number.isNaN(dt.valueOf())) return { days: null, dateLabel: null };
    return {
      days: Math.max(Math.floor((referenceDate() - dt) / 86400000), 0),
      dateLabel: formatDateCA(rawDate),
    };
  };

  const silentTone = (days) => {
    if (days == null) return "is-fresh";
    if (days > 60) return "is-critical";
    if (days >= 31) return "is-warning";
    if (days >= 15) return "is-watch";
    return "is-fresh";
  };

  const silentCellHtml = (rawDate, explicitDays = null) => {
    const meta = silentAge(rawDate, explicitDays);
    if (meta.days == null) return `<span class="sr-silent-chip is-fresh">${NA}</span>`;
    return `
      <span class="sr-silent-cell" title="Last order ${escapeHtml(meta.dateLabel || NA)}">
        <span class="sr-silent-chip ${silentTone(meta.days)}">${meta.days}d</span>
      </span>
    `;
  };

  const setScorecardLoading = (loading) => {
    document.getElementById("srKpiGrid")?.classList.toggle("sr-kpi-grid--loading", !!loading);
  };

  const setSummaryNarrativeLoading = (loading) => {
    document.getElementById("srSummaryNarrative")?.classList.toggle("sr-summary-narrative--loading", !!loading);
  };

  const chartShellFor = (canvasId) => document.getElementById(canvasId)?.parentElement || null;

  const setChartShellLoading = (canvasId, loading) => {
    const shell = chartShellFor(canvasId);
    if (shell) shell.classList.toggle("sr-chart-shell--loading", !!loading);
  };

  const setAllChartsLoading = (loading) => {
    CHART_IDS.forEach((canvasId) => setChartShellLoading(canvasId, loading));
  };

  const clearDeferredChartWork = () => {
    deferredChartToken += 1;
    deferredChartTimers.forEach((entry) => {
      if (entry?.idle && typeof window.cancelIdleCallback === "function") {
        window.cancelIdleCallback(entry.handle);
        return;
      }
      window.clearTimeout(entry?.handle);
    });
    deferredChartTimers.clear();
  };

  const scheduleDeferredChartWork = (fn, { delay = 0, idle = false } = {}) => {
    const token = deferredChartToken;
    const entry = { handle: null, idle: false };
    const run = () => {
      deferredChartTimers.delete(entry);
      if (token !== deferredChartToken) return;
      fn();
    };
    if (idle && typeof window.requestIdleCallback === "function") {
      entry.handle = window.requestIdleCallback(run, { timeout: 800 });
      entry.idle = true;
      deferredChartTimers.add(entry);
      return;
    }
    entry.handle = window.setTimeout(run, delay);
    deferredChartTimers.add(entry);
  };

  const signatureForRows = (rows = [], keys = []) =>
    JSON.stringify(
      (Array.isArray(rows) ? rows : []).map((row) => keys.map((key) => row?.[key] ?? null))
    );

  const memoizedRender = (key, signature, renderFn) => {
    if (renderMemo.get(key) === signature) return false;
    renderMemo.set(key, signature);
    renderFn();
    return true;
  };

  const readColumnVisibility = () => {
    try {
      const raw = window.localStorage?.getItem(COLUMN_STORAGE_KEY);
      const parsed = raw ? JSON.parse(raw) : {};
      return { ...DEFAULT_COLUMN_VISIBILITY, ...(parsed || {}) };
    } catch (_err) {
      return { ...DEFAULT_COLUMN_VISIBILITY };
    }
  };

  const persistColumnVisibility = (visibility) => {
    try {
      window.localStorage?.setItem(COLUMN_STORAGE_KEY, JSON.stringify(visibility || DEFAULT_COLUMN_VISIBILITY));
    } catch (_err) {
      /* ignore */
    }
  };

  let columnVisibility = readColumnVisibility();

  const cleanText = (value) => {
    const text = sanitizeDisplayText(value);
    if (!text || ["none", "null", "nan"].includes(text.toLowerCase())) return "";
    return text;
  };

  const normalizeRepBucket = (value) => {
    const text = cleanText(value);
    if (!text) return "";
    return SAFE_REP_BUCKET_ALIASES.get(text.toLowerCase()) || "";
  };

  const marginStatusKey = (value) => String(value || "").trim().toLowerCase();
  const marginStatusClass = (value) => {
    const key = marginStatusKey(value);
    if (key === "red") return "is-red";
    if (key === "orange") return "is-orange";
    if (key === "yellow") return "is-yellow";
    if (key === "light_green") return "is-light-green";
    if (key === "green") return "is-green";
    return "is-neutral";
  };
  const marginStatusLabel = (row = {}) => row?.target_status || row?.profitability_band || "Needs review";
  const isCriticalMargin = (row = {}) => {
    const actual = opt(row.margin_pct);
    const minimum = opt(row.minimum_margin_pct);
    return actual !== null && minimum !== null && actual < minimum;
  };
  const marginContextText = (row = {}) => {
    const parts = [];
    if (row.target_margin_pct != null) parts.push(`Target ${pct(row.target_margin_pct)}`);
    if (row.minimum_margin_pct != null) parts.push(`Min ${pct(row.minimum_margin_pct)}`);
    if (row.target_gap_pct_points != null) {
      parts.push(`${fmtSignedPoints(row.target_gap_pct_points)} vs target`);
    } else if (row.target_status) {
      parts.push(row.target_status);
    }
    return parts.join(" · ");
  };
  const marginCellHtml = (row = {}) => {
    const marginPct   = opt(row.margin_pct);
    const targetMgn   = opt(row.target_margin_pct);
    const minMgn      = opt(row.minimum_margin_pct);
    const context     = marginContextText(row);
    const critical    = isCriticalMargin(row);

    // ── Phase 4A: threshold-based pill (replaces generic "Needs review") ──
    let pill = "";
    if (marginPct != null && (targetMgn != null || minMgn != null)) {
      const t = targetMgn ?? Infinity;
      const m = minMgn  ?? -Infinity;
      if (marginPct >= t) {
        pill = '<span class="sr-margin-pill sr-margin-above">&#10003; On target</span>';
      } else if (marginPct >= m) {
        pill = '<span class="sr-margin-pill sr-margin-mid">&#9888; Below target</span>';
      } else {
        pill = '<span class="sr-margin-pill sr-margin-low">&#10007; Below min</span>';
      }
    } else if (critical) {
      pill = '<span class="sr-margin-pill sr-margin-low">&#10007; Critical</span>';
    } else {
      const status = marginStatusLabel(row);
      if (status && status !== "Needs review") {
        pill = `<span class="sr-status-pill ${marginStatusClass(row.status_key)}">${escapeHtml(status)}</span>`;
      }
    }

    return `
      <div class="sr-metric-stack sr-metric-stack-end">
        <div>${pct(marginPct, false)}</div>
        ${context || pill ? `<div class="sr-metric-sub">${pill}${context ? `${pill ? " " : ""}<span>${escapeHtml(context)}</span>` : ""}</div>` : ""}
      </div>
    `;
  };

  const isTechnicalRepId = (value) => {
    const text = cleanText(value);
    if (!text) return false;
    if (normalizeRepBucket(text)) return false;
    const lower = text.toLowerCase();
    if (SAFE_REP_BUCKETS.has(lower)) return false;
    if (/@|\/|\\/.test(text)) return true;
    if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(text)) return true;
    if (!/\s/.test(text) && /\d/.test(text) && /^[A-Za-z]{1,6}[-_ ]?\d[\w-]*$/.test(text)) return true;
    return !/\s/.test(text) && text.length >= 12 && /^[A-Za-z0-9_-]+$/.test(text);
  };

  const businessRepName = (name, fallbackId = null, defaultLabel = READABLE_REP_FALLBACK) => {
    const primary = cleanText(name);
    const fallback = cleanText(fallbackId);
    for (const candidate of [primary, fallback]) {
      if (!candidate) continue;
      const normalized = normalizeRepBucket(candidate);
      if (normalized) return normalized;
      if (!isTechnicalRepId(candidate)) return candidate;
    }
    return defaultLabel;
  };

  const repDisplayName = (row, defaultLabel = READABLE_REP_FALLBACK) =>
    businessRepName(row?.rep_name || row?.repName || row?.label, row?.rep_id || row?.repId || row?.key, defaultLabel);

  const currentFilterState = () => {
    try {
      const globalState = window.getGlobalFilterState ? window.getGlobalFilterState() : {};
      if (globalState?.filters && typeof globalState.filters === "object") return globalState.filters;
    } catch (_err) {
      /* ignore */
    }
    return {};
  };

  const focusedRepIdsFromFilters = (filters = {}) =>
    Array.from(
      new Set(
        (Array.isArray(filters?.sales_reps) ? filters.sales_reps : [])
          .map((value) => String(value || "").trim())
          .filter(Boolean)
      )
    );

  const focusedRepLabelsFromIds = (repIds = []) => {
    if (typeof window.getFilterLabels === "function") {
      const labels = window.getFilterLabels("sales_reps", repIds) || [];
      const cleaned = labels.map((value) => String(value || "").trim()).filter(Boolean);
      if (cleaned.length) return cleaned;
    }
    return repIds;
  };

  const syncFocusedReps = (filters = {}, { scroll = false } = {}) => {
    state.focusedRepIds = focusedRepIdsFromFilters(filters);
    state.focusedRepLabels = focusedRepLabelsFromIds(state.focusedRepIds);
    state.scrollToFocusedRep = !!scroll && state.focusedRepIds.length > 0;
  };

  const openUniversal = (payload, el = root) => {
    if (!payload || !window.universalDrilldown || typeof window.universalDrilldown.open !== "function") return false;
    window.universalDrilldown.open(payload, {}, el || root);
    return true;
  };

  const setDrillPayload = (el, payload) => {
    if (!el || !window.universalDrilldown || typeof window.universalDrilldown.setPayload !== "function") return;
    window.universalDrilldown.setPayload(el, payload);
  };

  const drillAttr = (payload) => {
    if (!payload) return "";
    return ` data-drilldown-payload="${escapeHtml(JSON.stringify(payload))}"`;
  };

  const currentTargetQuery = () => ({
    attribution_mode: state.attributionMode,
    roster_mode: state.rosterMode,
    transfer_only: !!state.transferOnly,
    metric: state.metric,
    leaderboard_metric: state.metric,
    leaderboard_scope: state.leaderboardScope,
    trend_metric: state.trendMetric,
    trend_grain: state.trendGrain,
    trend_view: state.trendView,
    top_n: state.topN,
  });

  const salesrepPayload = (row, section, widget, metric, value, extra = {}) => {
    const repId = row?.rep_id || row?.repId || row?.key || row?.rep_name;
    if (!repId) return null;
    return {
      source_page: "salesreps",
      source_section: section,
      source_widget: widget,
      requested_target: "salesrep",
      clicked_entity_type: "salesrep",
      clicked_entity_id: String(repId),
      clicked_entity_label: repDisplayName(row, READABLE_REP_FALLBACK),
      clicked_metric: metric,
      clicked_metric_value: value,
      active_filter_state: currentFilterState(),
      target_query: currentTargetQuery(),
      extra,
    };
  };

  const workspacePayload = (section, widget, metric, value, extra = {}) => ({
    source_page: "salesreps",
    source_section: section,
    source_widget: widget,
    requested_target: "workspace",
    clicked_metric: metric,
    clicked_metric_value: value,
    active_filter_state: currentFilterState(),
    target_query: currentTargetQuery(),
    extra,
  });

  const entityPayload = (target, section, widget, entityType, entityId, label, metric, value, extra = {}) => {
    if (!entityId) return null;
    return {
      source_page: "salesreps",
      source_section: section,
      source_widget: widget,
      requested_target: target,
      clicked_entity_type: entityType,
      clicked_entity_id: String(entityId),
      clicked_entity_label: cleanText(label) || String(entityId),
      clicked_metric: metric,
      clicked_metric_value: value,
      active_filter_state: currentFilterState(),
      target_query: currentTargetQuery(),
      extra,
    };
  };

  const customerPayload = (row, section, widget, metric, value, extra = {}) =>
    entityPayload(
      "customer",
      section,
      widget,
      "customer",
      row?.customer_id || row?.key || row?.customer_name,
      row?.customer_name || row?.label || row?.customer_id,
      metric,
      value,
      extra
    );

  const attributedWorkspacePayload = (section, widget, metric, value, extra = {}) =>
    workspacePayload(section, widget, metric, value, { workspace_kind: "salesreps_attributed", ...extra });

  const territoryPayload = (territoryName, section, widget, metric, value, extra = {}) =>
    cleanText(territoryName)
      ? attributedWorkspacePayload(section, widget, metric, value, {
        bucket_type: "territory",
        territory_name: territoryName,
        ...extra,
      })
      : null;

  const proteinPayload = (proteinFamily, section, widget, metric, value, extra = {}) =>
    cleanText(proteinFamily)
      ? attributedWorkspacePayload(section, widget, metric, value, {
        bucket_type: "protein",
        protein_family: proteinFamily,
        ...extra,
      })
      : null;

  const repWorkspacePayload = (row, section, widget, metric, value, extra = {}) =>
    attributedWorkspacePayload(section, widget, metric, value, {
      rep_id: row?.rep_id || row?.repId || row?.key || row?.rep_name,
      ...extra,
    });

  const sortRows = (rows, key, dir = "desc", valueFn = null) => {
    const list = Array.isArray(rows) ? [...rows] : [];
    const direction = dir === "asc" ? 1 : -1;
    return list.sort((a, b) => {
      const aRaw = valueFn ? valueFn(a, key) : a?.[key];
      const bRaw = valueFn ? valueFn(b, key) : b?.[key];
      const aNum = opt(aRaw);
      const bNum = opt(bRaw);
      if (aNum !== null || bNum !== null) return (aNum ?? -Infinity) > (bNum ?? -Infinity) ? direction : (aNum ?? -Infinity) < (bNum ?? -Infinity) ? -direction : 0;
      const aText = cleanText(aRaw).toLowerCase();
      const bText = cleanText(bRaw).toLowerCase();
      if (aText === bText) return 0;
      return aText > bText ? direction : -direction;
    });
  };

  const bucketLabelFromKey = (bucket, grain = "monthly", ttm = false) => {
    const raw = cleanText(bucket);
    if (!raw) return "--";
    if (ttm) return `TTM End ${raw}`;
    if (grain === "quarterly") return raw;
    if (grain === "yearly") return raw;
    if (/^\d{4}-\d{2}$/.test(raw)) {
      const [year, month] = raw.split("-");
      const dt = new Date(Number(year), Number(month) - 1, 1);
      return dt.toLocaleDateString(LOCALE, { month: "short", year: "numeric" });
    }
    return raw;
  };

  const stableColor = (index) => {
    const palette = [
      "#10B981", // Emerald
      "#3B82F6", // Electric Blue
      "#8B5CF6", // Royal Purple
      "#F97316", // Sunset Orange
      "#D946EF", // Deep Pink
      "#06B6D4", // Cyan
      "#84CC16", // Lime
      "#6366F1", // Indigo
      "#DC2626", // Ruby Red
      "#F59E0B", // Amber
    ];
    return palette[index % palette.length];
  };

  const alphaColor = (value, alpha = 0.18) => {
    const parsed = parseCssColor(value);
    if (!parsed) return value;
    return `rgba(${Math.round(parsed.r)}, ${Math.round(parsed.g)}, ${Math.round(parsed.b)}, ${alpha})`;
  };

  const sparklineSvg = (values = []) => {
    const points = (Array.isArray(values) ? values : []).map((value) => Math.max(0, num(value)));
    if (!points.length || points.every((value) => value === 0)) {
      return `
        <svg class="sr-sparkline sr-sparkline-flat" viewBox="0 0 78 24" aria-hidden="true" focusable="false">
          <polyline points="4,16 37,16 74,16"></polyline>
        </svg>
      `;
    }
    const maxValue = Math.max(...points, 1);
    const minValue = Math.min(...points, 0);
    const range = Math.max(maxValue - minValue, 1);
    const step = points.length === 1 ? 0 : 70 / (points.length - 1);
    const linePoints = points
      .map((value, idx) => {
        const x = 4 + (idx * step);
        const y = 20 - (((value - minValue) / range) * 14);
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(" ");
    const first = points[0] || 0;
    const last = points[points.length - 1] || 0;
    const tone = last > first ? "up" : last < first ? "down" : "flat";
    return `
      <svg class="sr-sparkline sr-sparkline-${tone}" viewBox="0 0 78 24" aria-hidden="true" focusable="false">
        <polyline points="${linePoints}"></polyline>
      </svg>
    `;
  };

  const showInlineToast = (message) => {
    const toast = document.getElementById("srFollowUpToast");
    if (!toast) {
      showActionPlaceholder(message);
      return;
    }
    toast.textContent = message;
    toast.style.display = "block";
    window.clearTimeout(showInlineToast._timer);
    showInlineToast._timer = window.setTimeout(() => {
      toast.style.display = "none";
    }, 2600);
  };

  const copyTextToClipboard = async (text, successMessage) => {
    const payload = String(text || "").trim();
    if (!payload) {
      showActionPlaceholder("No action context is available for this row.");
      return false;
    }
    try {
      if (navigator?.clipboard?.writeText) {
        await navigator.clipboard.writeText(payload);
        showInlineToast(successMessage);
        return true;
      }
    } catch (_err) {
      /* clipboard fallback below */
    }
    const input = document.createElement("textarea");
    input.value = payload;
    input.setAttribute("readonly", "readonly");
    input.style.position = "absolute";
    input.style.left = "-9999px";
    document.body.appendChild(input);
    input.select();
    let copied = false;
    try {
      copied = document.execCommand("copy");
    } catch (_err) {
      copied = false;
    }
    document.body.removeChild(input);
    if (copied) {
      showInlineToast(successMessage);
      return true;
    }
    showActionPlaceholder("Clipboard access is unavailable in this browser session.");
    return false;
  };

  const monthKeyToDate = (bucket) => {
    const raw = cleanText(bucket);
    if (!/^\d{4}-\d{2}$/.test(raw)) return null;
    const [year, month] = raw.split("-").map((v) => Number(v));
    const dt = new Date(year, month - 1, 1);
    return Number.isNaN(dt.valueOf()) ? null : dt;
  };

  const monthKeyFromDate = (dt) => {
    if (!(dt instanceof Date) || Number.isNaN(dt.valueOf())) return "";
    return `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}`;
  };

  const periodKey = (bucket, grain) => {
    const dt = monthKeyToDate(bucket);
    if (!dt) return bucket;
    if (grain === "yearly") return String(dt.getFullYear());
    if (grain === "quarterly") return `${dt.getFullYear()}-Q${Math.floor(dt.getMonth() / 3) + 1}`;
    return bucket;
  };

  const aggregateRepTrendDetail = (rows = [], grain = "monthly") => {
    const monthlyByRep = new Map();
    (Array.isArray(rows) ? rows : []).forEach((row) => {
      const repId = cleanText(row.rep_id || row.rep_name);
      const bucket = cleanText(row.bucket);
      if (!repId || !bucket) return;
      if (!monthlyByRep.has(repId)) monthlyByRep.set(repId, { rep_id: repId, rep_name: repDisplayName(row), points: [] });
      monthlyByRep.get(repId).points.push({
        bucket,
        rep_id: repId,
        rep_name: repDisplayName(row),
        revenue: num(row.revenue),
        revenue_yoy: opt(row.revenue_yoy),
        profit: opt(row.profit),
        profit_yoy: opt(row.profit_yoy),
        weight_lb: num(row.weight_lb),
        weight_lb_yoy: opt(row.weight_lb_yoy),
        customers: num(row.customers),
        customers_yoy: opt(row.customers_yoy),
        direct_revenue: opt(row.direct_revenue),
        inherited_revenue: opt(row.inherited_revenue),
        direct_customers: opt(row.direct_customers),
        inherited_customers: opt(row.inherited_customers),
        observed_days: opt(row.observed_days),
        observed_days_yoy: opt(row.observed_days_yoy),
      });
    });

    const aggregatePoints = (points, nextBucket) => {
      const revenue = points.reduce((acc, p) => acc + num(p.revenue), 0);
      const revenueYoY = points.reduce((acc, p) => acc + num(p.revenue_yoy), 0);
      const profit = points.some((p) => opt(p.profit) !== null) ? points.reduce((acc, p) => acc + num(p.profit), 0) : null;
      const profitYoY = points.some((p) => opt(p.profit_yoy) !== null) ? points.reduce((acc, p) => acc + num(p.profit_yoy), 0) : null;
      const weight = points.reduce((acc, p) => acc + num(p.weight_lb), 0);
      const weightYoY = points.some((p) => opt(p.weight_lb_yoy) !== null) ? points.reduce((acc, p) => acc + num(p.weight_lb_yoy), 0) : null;
      const customers = points.some((p) => opt(p.customers) !== null) ? points.reduce((acc, p) => acc + num(p.customers), 0) : 0;
      const customersYoY = points.some((p) => opt(p.customers_yoy) !== null) ? points.reduce((acc, p) => acc + num(p.customers_yoy), 0) : null;
      const directRevenue = points.some((p) => opt(p.direct_revenue) !== null) ? points.reduce((acc, p) => acc + num(p.direct_revenue), 0) : null;
      const inheritedRevenue = points.some((p) => opt(p.inherited_revenue) !== null) ? points.reduce((acc, p) => acc + num(p.inherited_revenue), 0) : null;
      const directCustomers = points.some((p) => opt(p.direct_customers) !== null) ? points.reduce((acc, p) => acc + num(p.direct_customers), 0) : null;
      const inheritedCustomers = points.some((p) => opt(p.inherited_customers) !== null) ? points.reduce((acc, p) => acc + num(p.inherited_customers), 0) : null;
      const observedDays = points.some((p) => opt(p.observed_days) !== null) ? points.reduce((acc, p) => acc + num(p.observed_days), 0) : null;
      const observedDaysYoY = points.some((p) => opt(p.observed_days_yoy) !== null) ? points.reduce((acc, p) => acc + num(p.observed_days_yoy), 0) : null;
      return {
        bucket: nextBucket,
        rep_id: points[0]?.rep_id,
        rep_name: points[0]?.rep_name,
        revenue,
        revenue_yoy: revenueYoY,
        profit,
        profit_yoy: profitYoY,
        margin_pct: revenue > 0 && profit !== null ? (profit / revenue) * 100 : null,
        margin_pct_yoy: revenueYoY > 0 && profitYoY !== null ? (profitYoY / revenueYoY) * 100 : null,
        weight_lb: weight,
        weight_lb_yoy: weightYoY,
        customers,
        customers_yoy: customersYoY,
        direct_revenue: directRevenue,
        inherited_revenue: inheritedRevenue,
        direct_customers: directCustomers,
        inherited_customers: inheritedCustomers,
        observed_days: observedDays,
        observed_days_yoy: observedDaysYoY,
      };
    };

    const aggregated = [];
    monthlyByRep.forEach((entry) => {
      const sorted = entry.points.sort((a, b) => cleanText(a.bucket).localeCompare(cleanText(b.bucket)));
      if (grain === "monthly") {
        sorted.forEach((point) => aggregated.push({ ...point, margin_pct: point.revenue && point.profit != null ? (num(point.profit) / point.revenue) * 100 : null, margin_pct_yoy: num(point.revenue_yoy) && point.profit_yoy != null ? (num(point.profit_yoy) / num(point.revenue_yoy)) * 100 : null }));
        return;
      }
      if (grain === "ttm") {
        if (sorted.length < 12) return;
        for (let idx = 11; idx < sorted.length; idx += 1) {
          const windowPoints = sorted.slice(idx - 11, idx + 1);
          aggregated.push(aggregatePoints(windowPoints, sorted[idx].bucket));
        }
        return;
      }
      const groups = new Map();
      sorted.forEach((point) => {
        const key = periodKey(point.bucket, grain);
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(point);
      });
      Array.from(groups.entries())
        .sort((a, b) => a[0].localeCompare(b[0]))
        .forEach(([key, bucketRows]) => aggregated.push(aggregatePoints(bucketRows, key)));
    });

    return aggregated.sort((a, b) => {
      if (a.rep_id === b.rep_id) return cleanText(a.bucket).localeCompare(cleanText(b.bucket));
      return cleanText(a.rep_name).localeCompare(cleanText(b.rep_name));
    });
  };

  const trendMetricValue = (point, metric) => {
    if (!point) return 0;
    if (metric === "profit") return opt(point.profit) ?? 0;
    if (metric === "margin_pct") return opt(point.margin_pct) ?? 0;
    if (metric === "customers") return num(point.customers);
    if (metric === "weight_lb") return num(point.weight_lb);
    return num(point.revenue);
  };

  const trendMetricPriorValue = (point, metric) => {
    if (!point) return null;
    if (metric === "profit") return opt(point.profit_yoy);
    if (metric === "margin_pct") return opt(point.margin_pct_yoy);
    if (metric === "customers") return opt(point.customers_yoy);
    if (metric === "weight_lb") return opt(point.weight_lb_yoy);
    return opt(point.revenue_yoy);
  };

  const trendMetricFormatter = (metric, value) => {
    if (metric === "margin_pct") return `${fmtPct.format(num(value))}%`;
    if (metric === "customers") return fmtInt.format(num(value));
    if (metric === "weight_lb") return fmtInt.format(num(value));
    return money(value);
  };

  const comparableObservedDays = (currentDays, priorDays) => {
    const current = opt(currentDays);
    const prior = opt(priorDays);
    if (current == null || prior == null || current <= 0 || prior <= 0) return false;
    return Math.abs(current - prior) <= 2;
  };

  const trendMoM = (points, idx, metric) => {
    if (!Array.isArray(points) || idx <= 0) return null;
    const current = trendMetricValue(points[idx], metric);
    const previous = trendMetricValue(points[idx - 1], metric);
    if (!Number.isFinite(current) || !Number.isFinite(previous) || previous === 0) return null;
    return ((current - previous) / Math.abs(previous)) * 100;
  };

  const isoDateLabel = (raw) => {
    if (!raw) return "--";
    const dt = new Date(raw);
    if (Number.isNaN(dt.valueOf())) return String(raw);
    return dt.toLocaleString(LOCALE);
  };

  const destroyChart = (key) => {
    if (charts[key]?.destroy) charts[key].destroy();
    charts[key] = null;
  };

  const resolveChartCanvas = (canvasId) => {
    const el = document.getElementById(canvasId);
    if (!el) {
      logWarn(`[salesreps] missing chart canvas: #${canvasId}`);
      return null;
    }
    if (!(el instanceof HTMLCanvasElement)) {
      logWarn(`[salesreps] invalid chart element for #${canvasId}; expected <canvas>.`);
      return null;
    }
    const ctx = el.getContext("2d");
    if (!ctx) {
      logWarn(`[salesreps] unable to get 2d context for #${canvasId}`);
      return null;
    }
    return { el, ctx };
  };

  const createChart = (key, canvasId, config) => {
    if (!ChartLib) return null;
    destroyChart(key);
    const resolved = resolveChartCanvas(canvasId);
    if (!resolved) return null;
    try {
      charts[key] = new ChartLib(resolved.ctx, config);
      return charts[key];
    } catch (err) {
      logError(`[salesreps] chart init failed: #${canvasId}`, err);
      return null;
    }
  };

  const toggleEmpty = (canvasId, show, message = emptyMessage) => {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    const holder = canvas.parentElement;
    if (!holder) return;
    holder.style.position = holder.style.position || "relative";

    let emptyEl = holder.querySelector("[data-empty-state]");
    if (!emptyEl) {
      emptyEl = document.createElement("div");
      emptyEl.dataset.emptyState = "true";
      emptyEl.className = "position-absolute top-0 start-0 w-100 h-100 d-flex align-items-center justify-content-center text-muted small";
      emptyEl.style.background = "rgba(245,245,220,0.92)";
      emptyEl.style.pointerEvents = "none";
      holder.appendChild(emptyEl);
    }
    emptyEl.textContent = message;
    emptyEl.classList.toggle("d-none", !show);
    canvas.classList.toggle("d-none", !!show);
    setChartShellLoading(canvasId, false);
  };

  const setText = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  };

  /* The comparison basis, as the server resolved it. Hardcoding "MoM (FMTD)"
     here was wrong twice over: the delta is against the prior fiscal
     year-to-date, not the prior month, and the label did not change when the
     filter did. It is now taken from the bundle, which reads it from the one
     module that decides the window. */
  let comparisonBasisLabel = "vs prior period";

  const setDelta = (id, value, suffix = "%") => {
    const el = document.getElementById(id);
    if (!el) return;
    const n = opt(value);
    el.classList.remove("delta-up", "delta-down");
    if (n === null) {
      el.innerHTML = `<span class="sr-kpi-delta sr-kpi-delta--neutral">${comparisonBasisLabel} —</span>`;
      return;
    }
    const cls = n >= 0 ? "sr-kpi-delta--pos" : "sr-kpi-delta--neg";
    const sign = n >= 0 ? "+" : "";
    el.innerHTML = `<span class="sr-kpi-delta ${cls}">${sign}${fmtPct.format(n)}${suffix} ${comparisonBasisLabel}</span>`;
  };

  const updateColumnLabels = (meta = {}) => {
    if (meta.basis_short_label) comparisonBasisLabel = meta.basis_short_label;
    const units = meta.units_label || root.dataset.unitsLabel || "Units";
    const asp = meta.asp_label || root.dataset.aspLabel || "ASP";
    const aspLb = meta.asp_lb_label || root.dataset.aspLbLabel || "ASP / LB";
    setText("kpiUnitsLabel", units);
    setText("kpiAspLabel", asp);
    setText("kpiAspLbLabel", aspLb);
    document.querySelectorAll("[data-column-label='units']").forEach((el) => { el.textContent = units; });
    document.querySelectorAll("[data-column-label='asp']").forEach((el) => { el.textContent = asp; });
    document.querySelectorAll("[data-column-label='asp_lb']").forEach((el) => { el.textContent = aspLb; });
  };

  const healthIndexPct = (row = {}) => {
    const active = num(row.active_customers);
    const healthy = num(row.healthy_customers);
    if (active <= 0) return 0;
    return Math.max(0, Math.min(100, (healthy / active) * 100));
  };

  const ringTone = (pctValue) => {
    if (pctValue >= 80) return SR_THEME.forest;
    if (pctValue >= 60) return SR_THEME.gold;
    return SR_THEME.blood;
  };

  const healthRingHtml = (row = {}) => {
    const pctValue = healthIndexPct(row);
    const label = `${Math.round(pctValue)}%`;
    return `
      <div class="sr-health-ring" style="--health-pct:${pctValue.toFixed(1)};--ring-color:${ringTone(pctValue)}" title="${label} of the visible portfolio placed an order within the last 30 days">
        <svg viewBox="0 0 42 42" aria-hidden="true" focusable="false">
          <circle class="sr-health-ring-track" cx="21" cy="21" r="15.915"></circle>
          <circle class="sr-health-ring-value" cx="21" cy="21" r="15.915"></circle>
        </svg>
        <span class="sr-health-ring-label">${escapeHtml(label)}</span>
      </div>
    `;
  };

  const directInheritedRatioHtml = (row = {}) => {
    const directRevenue = Math.max(num(row.direct_revenue), 0);
    const inheritedRevenue = Math.max(num(row.transferred_in_revenue), 0);
    const total = directRevenue + inheritedRevenue;
    const directPct = total > 0 ? (directRevenue / total) * 100 : 100;
    const inheritedPct = total > 0 ? (inheritedRevenue / total) * 100 : 0;
    return `
      <div class="sr-ratio-stack" title="Direct ${directPct.toFixed(1)}% · Inherited ${inheritedPct.toFixed(1)}%">
        <div class="sr-ratio-bar">
          <span class="sr-ratio-bar-direct" style="width:${directPct.toFixed(1)}%"></span>
          <span class="sr-ratio-bar-inherited" style="width:${inheritedPct.toFixed(1)}%"></span>
        </div>
        <div class="sr-ratio-labels">
          <span><strong style="color:#1E293B">${directPct.toFixed(0)}%</strong> direct</span>
          <span><strong style="color:#1E293B">${inheritedPct.toFixed(0)}%</strong> inherited</span>
        </div>
      </div>
    `;
  };

  const momentumArrowHtml = (row = {}) => {
    const pctValue = opt(row.mom_revenue_pct);
    if (pctValue === null) {
      return `
        <div class="sr-momentum-stack">
          <span class="sr-momentum-arrow is-flat">• ${NUMERIC_EMPTY}</span>
          <span class="sr-momentum-sub">${TEXT_EMPTY}</span>
        </div>
      `;
    }
    const tone = pctValue > 1 ? "is-up" : pctValue < -1 ? "is-down" : "is-flat";
    const arrow = pctValue > 1 ? "▲" : pctValue < -1 ? "▼" : "•";
    const label = pctValue > 1 ? "Accelerating" : pctValue < -1 ? "Decelerating" : "Flat";
    return `
      <div class="sr-momentum-stack">
        <span class="sr-momentum-arrow ${tone}">${arrow} ${pctValue > 0 ? "+" : ""}${fmtPct.format(pctValue)}%</span>
        <span class="sr-momentum-sub">${label}</span>
      </div>
    `;
  };

  const renderTableFooter = (payload = {}) => {
    const foot = document.getElementById("salesreps-table-foot");
    if (!foot) return;
    const benchmarks = payload.benchmarks || {};
    const healthPct = opt(benchmarks.avg_health_index_pct);
    const directPct = opt(benchmarks.avg_direct_revenue_share_pct);
    const inheritedPct = directPct === null ? 0 : Math.max(0, 100 - directPct);
    const momentumPct = opt(benchmarks.avg_mom_revenue_pct);
    foot.innerHTML = `
      <tr class="sr-table-average-row">
        <th class="col-select text-center">Avg</th>
        <th class="sticky-col">Company Average</th>
        <td class="col-health text-center">${healthPct === null ? TEXT_EMPTY : healthRingHtml({ health_score: healthPct, health_label: "Average" })}</td>
        <td class="col-direct_ratio">${directPct === null ? TEXT_EMPTY : directInheritedRatioHtml({ direct_revenue: directPct, transferred_in_revenue: inheritedPct })}</td>
        <td class="col-quartile text-center"><span class="text-muted small">Benchmark</span></td>
        <td class="text-end col-revenue">${money(benchmarks.avg_revenue)}</td>
        <td class="text-end col-profit">${money(benchmarks.avg_profit)}</td>
        <td class="text-end col-margin_pct">${pct(benchmarks.avg_margin_pct, false)}</td>
        <td class="text-end col-silent_days">30d window</td>
        <td class="text-end col-mom_revenue_delta">${momentumArrowHtml({ mom_revenue_pct: momentumPct })}</td>
        <td class="text-end col-yoy_revenue_delta">${TEXT_EMPTY}</td>
        <td class="text-end col-weight_lb">${TEXT_EMPTY}</td>
        <td class="text-end col-active_customers">${benchmarks.avg_customers == null ? NUMERIC_EMPTY : fmtInt.format(num(benchmarks.avg_customers))}</td>
        <td class="text-end col-current_owned_customers">${TEXT_EMPTY}</td>
        <td class="text-end col-inherited_customers">${TEXT_EMPTY}</td>
        <td class="text-end col-transferred_in_revenue">${TEXT_EMPTY}</td>
        <td class="text-end col-transferred_out_revenue">${TEXT_EMPTY}</td>
        <td class="text-end col-yoy_revenue_pct">${TEXT_EMPTY}</td>
        <td class="text-end col-territory_count">${TEXT_EMPTY}</td>
        <td class="col-replaced_reps">${TEXT_EMPTY}</td>
        <td class="col-top_territory">${TEXT_EMPTY}</td>
        <td class="col-top_customer">${TEXT_EMPTY}</td>
        <td class="col-top_protein">${TEXT_EMPTY}</td>
        <td class="col-flags">${TEXT_EMPTY}</td>
        <td class="text-end">${TEXT_EMPTY}</td>
      </tr>
    `;
    applyColumnVisibility();
  };

  const syncViewportHeights = () => {
    const targets = [
      { id: "srTopCustomersWrap", minHeight: 380, bottomOffset: 52 },
      { id: "srTableWrap", minHeight: 460, bottomOffset: 28 },
    ];
    targets.forEach(({ id, minHeight, bottomOffset }) => {
      const el = document.getElementById(id);
      if (!el) return;
      const top = el.getBoundingClientRect().top;
      const available = Math.max(Math.floor(window.innerHeight - top - bottomOffset), minHeight);
      el.style.height = `${available}px`;
    });
  };

  const scheduleViewportHeightSync = () => {
    if (viewportHeightTicking) return;
    viewportHeightTicking = true;
    window.requestAnimationFrame(() => {
      viewportHeightTicking = false;
      syncViewportHeights();
      scheduleVirtualCustomerRender({ force: true });
      scheduleVirtualTableRender({ force: true });
    });
  };

  const currentUrlFilters = () => {
    const params = new URLSearchParams(state.qs || window.location.search || "");
    return {
      customers: params.getAll("customers").filter(Boolean),
      regions: params.getAll("regions").filter(Boolean),
      sales_reps: params.getAll("sales_reps").filter(Boolean),
    };
  };

  const buildFilterBreadcrumb = (payload = {}) => {
    const filterState = currentFilterState();
    const qsFilters = currentUrlFilters();
    const labels = [];
    const start = payload?.meta?.window_start;
    const end = payload?.meta?.window_end;
    if (start || end) labels.push(`${formatDateCA(start)} to ${formatDateCA(end)}`);
    const regionLabels = typeof window.getFilterLabels === "function"
      ? window.getFilterLabels("regions", filterState?.regions || qsFilters.regions || [])
      : (filterState?.regions || qsFilters.regions || []);
    const repLabels = typeof window.getFilterLabels === "function"
      ? window.getFilterLabels("sales_reps", filterState?.sales_reps || qsFilters.sales_reps || [])
      : (filterState?.sales_reps || qsFilters.sales_reps || []);
    const customerLabels = typeof window.getFilterLabels === "function"
      ? window.getFilterLabels("customers", filterState?.customers || qsFilters.customers || [])
      : (filterState?.customers || qsFilters.customers || []);
    if (regionLabels.length) labels.push(regionLabels.slice(0, 2).join(", "));
    if (repLabels.length) labels.push(repLabels.slice(0, 2).join(", "));
    if (focusedCustomer?.customer_name) labels.push(focusedCustomer.customer_name);
    else if (customerLabels.length) labels.push(customerLabels.slice(0, 1).join(", "));
    return labels.filter(Boolean).join(" | ") || "All Time | All Regions | All Reps";
  };

  const renderFilterBreadcrumb = (payload = {}) => {
    const el = document.getElementById("srFilterBreadcrumb");
    if (el) el.textContent = buildFilterBreadcrumb(payload);
  };

  const syncBodyScrollLock = () => {
    const hasOpenDrawer = !!document.querySelector("#srFilterDrawer.is-open, #srFollowUpDrawer.is-open");
    document.body.style.overflow = hasOpenDrawer ? "hidden" : "";
  };

  const mountFilterDrawer = () => {
    if (filterDrawerMounted) return;
    const filters = document.getElementById("GlobalFilters");
    const mount = document.getElementById("srFilterDrawerMount");
    if (!filters || !mount) return;
    mount.appendChild(filters);
    filterDrawerMounted = true;
  };

  const openFilterDrawer = () => {
    const drawer = document.getElementById("srFilterDrawer");
    if (!drawer) return;
    mountFilterDrawer();
    drawer.style.display = "flex";
    window.requestAnimationFrame(() => {
      drawer.classList.add("is-open");
      syncBodyScrollLock();
    });
  };

  const closeFilterDrawer = () => {
    const drawer = document.getElementById("srFilterDrawer");
    if (!drawer || !drawer.classList.contains("is-open")) return;
    drawer.classList.remove("is-open");
    drawer.addEventListener("transitionend", () => {
      if (!drawer.classList.contains("is-open")) drawer.style.display = "none";
      syncBodyScrollLock();
    }, { once: true });
  };

  const wireFilterDrawer = () => {
    if (filterDrawerWired) return;
    const toggle = document.getElementById("srFilterDrawerToggle");
    const close = document.getElementById("srFilterDrawerClose");
    const backdrop = document.getElementById("srFilterDrawerBackdrop");
    const drawer = document.getElementById("srFilterDrawer");
    if (!toggle || !close || !backdrop || !drawer) return;
    toggle.addEventListener("click", openFilterDrawer);
    close.addEventListener("click", closeFilterDrawer);
    backdrop.addEventListener("click", closeFilterDrawer);
    document.addEventListener("keydown", (evt) => {
      if (evt.key !== "Escape") return;
      if (drawer.classList.contains("is-open")) closeFilterDrawer();
    });
    mountFilterDrawer();
    filterDrawerWired = true;
  };

  const syncStateFromQS = (qs) => {
    const params = new URLSearchParams(String(qs || "").replace(/^\?/, ""));
    const page = Number(params.get("page"));
    if (Number.isFinite(page) && page > 0) state.page = page;
    const pageSize = Number(params.get("page_size"));
    if (Number.isFinite(pageSize) && pageSize > 0) state.pageSize = pageSize;
    const sortBy = params.get("sort");
    if (sortBy) state.sortBy = sortBy;
    const sortDir = params.get("dir");
    if (sortDir) state.sortDir = sortDir === "asc" ? "asc" : "desc";
    const search = params.get("q");
    if (search != null) state.search = search;
    const mode = params.get("attribution_mode");
    if (mode) state.attributionMode = mode;
    const roster = params.get("roster_mode");
    if (roster) state.rosterMode = roster;
    const transferOnly = params.get("transfer_only");
    if (transferOnly != null) state.transferOnly = ["1", "true", "yes", "on"].includes(String(transferOnly).toLowerCase());
    const metric = params.get("metric");
    if (metric && metricConfig[metric]) state.metric = metric;
    const trendMetric = params.get("trend_metric");
    if (trendMetric && metricConfig[trendMetric]) state.trendMetric = trendMetric;
    const trendGrain = params.get("trend_grain");
    if (trendGrain && ["monthly", "quarterly", "yearly", "ttm"].includes(trendGrain)) state.trendGrain = trendGrain;
    const trendView = params.get("trend_view");
    if (trendView && ["absolute", "yoy_delta", "index"].includes(trendView)) state.trendView = trendView;
    const topN = Number(params.get("top_n") || params.get("topN"));
    if (Number.isFinite(topN) && topN > 0) state.topN = topN;
    const leaderboardScope = params.get("leaderboard_scope");
    if (leaderboardScope && ["all", "direct_only"].includes(leaderboardScope)) {
      state.leaderboardScope = leaderboardScope;
    }
  };

  const baseQuery = () => {
    const params = new URLSearchParams(state.qs || "");
    params.set("page", String(state.page));
    params.set("page_size", String(state.pageSize));
    params.set("sort", state.sortBy);
    params.set("dir", state.sortDir);
    params.set("metric", state.metric);
    params.set("trend_metric", state.trendMetric);
    params.set("trend_grain", state.trendGrain);
    params.set("trend_view", state.trendView);
    params.set("top_n", String(state.topN));
    params.set("attribution_mode", state.attributionMode);
    params.set("roster_mode", state.rosterMode);
    params.set("leaderboard_scope", state.leaderboardScope);
    if (state.transferOnly) params.set("transfer_only", "1");
    else params.delete("transfer_only");
    if (state.search) params.set("q", state.search);
    else params.delete("q");
    return params;
  };

  const buildQueryString = () => baseQuery().toString();

  const syncBrowserUrl = () => {
    const nextQs = buildQueryString();
    state.qs = nextQs;
    if (!window.history?.replaceState) return nextQs;
    const nextUrl = `${window.location.pathname}${nextQs ? `?${nextQs}` : ""}${window.location.hash || ""}`;
    const currentUrl = `${window.location.pathname}${window.location.search || ""}${window.location.hash || ""}`;
    if (nextUrl !== currentUrl) {
      window.history.replaceState(window.history.state || null, "", nextUrl);
    }
    return nextQs;
  };

  const updateExportLinks = () => {
    const exportParams = baseQuery();
    exportParams.delete("page");
    exportParams.delete("page_size");
    const qs = exportParams.toString();
    if (exportXlsx) {
      const base = exportXlsx.dataset.baseHref || exportXlsx.getAttribute("href") || root.dataset.exportXlsx || "";
      exportXlsx.dataset.baseHref = base.split("?")[0];
      exportXlsx.setAttribute("href", exportXlsx.dataset.baseHref + (qs ? `?${qs}` : ""));
    }
    if (exportCsv) {
      const base = exportCsv.dataset.baseHref || exportCsv.getAttribute("href") || root.dataset.exportCsv || "";
      exportCsv.dataset.baseHref = base.split("?")[0];
      exportCsv.setAttribute("href", exportCsv.dataset.baseHref + (qs ? `?${qs}` : ""));
    }
  };

  const syncControlsFromState = () => {
    const metricToggle = document.getElementById("srMetricToggle");
    const leaderboardDirectOnly = document.getElementById("srLeaderboardDirectOnly");
    const trendMetric = document.getElementById("srTrendMetric");
    const trendGrain = document.getElementById("srTrendGrain");
    const trendView = document.getElementById("srTrendView");
    const topN = document.getElementById("srTopN");
    const pageSize = document.getElementById("srPageSize");
    const search = document.getElementById("srSearchInput");
    const attributionMode = document.getElementById("srAttributionMode");
    const includeFormer = document.getElementById("srIncludeFormerReps");
    const transferOnly = document.getElementById("srTransferOnly");

    if (metricToggle) metricToggle.value = state.metric;
    if (leaderboardDirectOnly) leaderboardDirectOnly.checked = state.leaderboardScope === "direct_only";
    if (trendMetric) trendMetric.value = state.trendMetric;
    if (trendGrain) trendGrain.value = state.trendGrain;
    if (trendView) trendView.value = state.trendView;
    if (topN) topN.value = String(state.topN);
    if (pageSize) pageSize.value = String(state.pageSize);
    if (search) search.value = state.search || "";
    if (attributionMode) attributionMode.value = state.attributionMode;
    if (includeFormer) includeFormer.checked = state.rosterMode === "include_former";
    if (transferOnly) transferOnly.checked = !!state.transferOnly;
  };

  const snapshotUiState = () => ({
    page: state.page,
    pageSize: state.pageSize,
    sortBy: state.sortBy,
    sortDir: state.sortDir,
    search: state.search,
    metric: state.metric,
    trendMetric: state.trendMetric,
    trendGrain: state.trendGrain,
    trendView: state.trendView,
    trendSelectedReps: Array.isArray(state.trendSelectedReps) ? [...state.trendSelectedReps] : [],
    trendFocusMode: !!state.trendFocusMode,
    topN: state.topN,
    topCustomersSortBy: state.topCustomersSortBy,
    topCustomersSortDir: state.topCustomersSortDir,
    proteinSortBy: state.proteinSortBy,
    proteinSortDir: state.proteinSortDir,
    attributionMode: state.attributionMode,
    rosterMode: state.rosterMode,
    transferOnly: !!state.transferOnly,
    leaderboardScope: state.leaderboardScope,
  });

  const applySnapshotUiState = (uiState = {}) => {
    if (!uiState || typeof uiState !== "object") return;
    if (Number.isFinite(Number(uiState.page)) && Number(uiState.page) > 0) state.page = Number(uiState.page);
    if (Number.isFinite(Number(uiState.pageSize)) && Number(uiState.pageSize) > 0) state.pageSize = Number(uiState.pageSize);
    if (uiState.sortBy) state.sortBy = String(uiState.sortBy);
    if (uiState.sortDir) state.sortDir = String(uiState.sortDir) === "asc" ? "asc" : "desc";
    if (uiState.search != null) state.search = String(uiState.search);
    if (uiState.metric && metricConfig[uiState.metric]) state.metric = uiState.metric;
    if (uiState.trendMetric && metricConfig[uiState.trendMetric]) state.trendMetric = uiState.trendMetric;
    if (uiState.trendGrain && ["monthly", "quarterly", "yearly", "ttm"].includes(uiState.trendGrain)) state.trendGrain = uiState.trendGrain;
    if (uiState.trendView && ["absolute", "yoy_delta", "index"].includes(uiState.trendView)) state.trendView = uiState.trendView;
    if (Array.isArray(uiState.trendSelectedReps)) state.trendSelectedReps = [...uiState.trendSelectedReps];
    state.trendFocusMode = !!uiState.trendFocusMode;
    if (Number.isFinite(Number(uiState.topN)) && Number(uiState.topN) > 0) state.topN = Number(uiState.topN);
    if (uiState.topCustomersSortBy) state.topCustomersSortBy = String(uiState.topCustomersSortBy);
    if (uiState.topCustomersSortDir) state.topCustomersSortDir = String(uiState.topCustomersSortDir) === "asc" ? "asc" : "desc";
    if (uiState.proteinSortBy) state.proteinSortBy = String(uiState.proteinSortBy);
    if (uiState.proteinSortDir) state.proteinSortDir = String(uiState.proteinSortDir) === "asc" ? "asc" : "desc";
    if (uiState.attributionMode) state.attributionMode = String(uiState.attributionMode);
    if (uiState.rosterMode) state.rosterMode = String(uiState.rosterMode);
    state.transferOnly = !!uiState.transferOnly;
    if (uiState.leaderboardScope && ["all", "direct_only"].includes(String(uiState.leaderboardScope))) {
      state.leaderboardScope = String(uiState.leaderboardScope);
    }
  };

  const persistSnapshot = (payload = lastPayload) => {
    if (!pageCache || !payload) return false;
    const fullQs = buildQueryString();
    if (!fullQs) return false;
    return pageCache.saveSnapshot(PAGE_CACHE_ID, {
      qs: fullQs,
      payload,
      uiState: snapshotUiState(),
      scrollY: window.scrollY || 0,
      meta: {
        datasetVersion: payload?.meta?.dataset_version || null,
        cacheTtl: payload?.meta?.cache_ttl || null,
      },
    });
  };

  const restoreSnapshot = (qs, { restoreScroll = false } = {}) => {
    if (!pageCache) return null;
    const snapshot = pageCache.loadSnapshot(PAGE_CACHE_ID, { qs, ...PAGE_CACHE_POLICY });
    if (!snapshot?.payload) return null;
    applySnapshotUiState(snapshot.ui_state || {});
    syncControlsFromState();
    renderBundle(snapshot.payload);
    if (restoreScroll) {
      pageCache.restoreScroll(PAGE_CACHE_ID, { qs, ...PAGE_CACHE_POLICY, delayMs: 40 });
    }
    return snapshot;
  };

  const rowMetricValue = (row, metric) => {
    const conf = metricConfig[metric] || metricConfig.revenue;
    return conf.value(row);
  };

  const sortedByMetric = (rows, metric) => {
    const list = Array.isArray(rows) ? [...rows] : [];
    return list.sort((a, b) => rowMetricValue(b, metric) - rowMetricValue(a, metric));
  };

  const setScorecardBorder = (valueId, tone = "") => {
    const card = document.getElementById(valueId)?.closest(".sr-kpi");
    if (!card) return;
    card.classList.remove("sr-kpi--status-green", "sr-kpi--status-yellow", "sr-kpi--status-red");
    if (tone) card.classList.add(`sr-kpi--status-${tone}`);
  };

  const renderSystemHealth = (payload = {}) => {
    const button = document.getElementById("srSystemHealthBtn");
    if (!button) return;
    const k = payload.kpis || {};
    const packs = payload.meta?.packs_coverage || {};
    const warnings = Array.isArray(payload.warnings) ? payload.warnings.filter(Boolean) : [];
    const ownershipCoverage = opt(k.ownership_coverage_pct);
    const costCoverage = opt(k.cost_coverage_pct);
    const packsCoverage = opt(k.packs_coverage_pct ?? packs.packs_coverage_pct);
    const hasRisk = [ownershipCoverage, costCoverage, packsCoverage].some((value) => value !== null && value < 95)
      || warnings.length > 0;
    const hasWatch = !hasRisk && [ownershipCoverage, costCoverage, packsCoverage].some((value) => value !== null && value < 98);
    button.classList.remove("is-good", "is-watch", "is-risk");
    button.classList.add(hasRisk ? "is-risk" : hasWatch ? "is-watch" : "is-good");

    const notes = warnings.slice(0, 3).map((msg) => `<li>${escapeHtml(msg)}</li>`).join("");
    const content = `
      <div class="sr-system-health">
        <div class="sr-system-health__metric">
          <span class="sr-system-health__label">Ownership Coverage</span>
          <span class="sr-system-health__value">${ownershipCoverage === null ? NA : `${fmtPct.format(ownershipCoverage)}%`}</span>
        </div>
        <div class="sr-system-health__metric">
          <span class="sr-system-health__label">Cost Coverage</span>
          <span class="sr-system-health__value">${costCoverage === null ? NA : `${fmtPct.format(costCoverage)}%`}</span>
        </div>
        <div class="sr-system-health__metric">
          <span class="sr-system-health__label">Packs Coverage</span>
          <span class="sr-system-health__value">${packsCoverage === null ? NA : `${fmtPct.format(packsCoverage)}%`}</span>
        </div>
        ${
          notes
            ? `<ul class="sr-system-health__notes">${notes}</ul>`
            : `<div class="text-muted small">No active data-trust warnings in the current slice.</div>`
        }
      </div>
    `;

    if (window.bootstrap?.Popover) {
      if (systemHealthPopover) systemHealthPopover.dispose();
      systemHealthPopover = new window.bootstrap.Popover(button, {
        html: true,
        sanitize: false,
        customClass: "sr-system-health-popover",
        title: "System Health",
        content,
        placement: "bottom",
      });
    } else {
      button.setAttribute("title", `Ownership ${ownershipCoverage ?? NA} | Cost ${costCoverage ?? NA} | Packs ${packsCoverage ?? NA}`);
    }
  };

  const renderExecutive = (payload = {}) => {
    const k = payload.kpis || {};
    const meta = payload.meta || {};
    const attribution = meta.attribution || {};
    const bridge = meta.ownership_bridge || {};
    const succession = meta.ownership_succession || {};
    const snapshot = meta.ownership_snapshot || {};
    const analysis = payload.analysis || {};
    const portfolio = analysis.portfolio || {};

    setText("kpiRevenue", money(k.revenue));
    setText("kpiProfit", k.profit == null ? NA : money(k.profit));
    setText("kpiMargin", k.margin_pct == null ? NA : `${fmtPct.format(num(k.margin_pct))}%`);
    setText("kpiOrders", fmtInt.format(num(k.orders)));
    setText("kpiCustomers", fmtInt.format(num(k.customers)));
    setText("kpiWeight", fmtInt.format(num(k.weight_lb)));
    setText("kpiUnits", fmtInt.format(num(k.units)));
    setText("kpiAspLb", k.asp_lb == null ? NA : money(k.asp_lb, false));
    setText("kpiAsp", k.asp == null ? NA : money(k.asp, false));
    setText("kpiAov", k.avg_order_value == null ? NA : money(k.avg_order_value, false));
    setText("kpiPpo", k.profit_per_order == null ? "—" : money(k.profit_per_order, false));
    setText("kpiActiveCustomers", fmtInt.format(num(k.active_customers)));
    setText("kpiAvgOrderValue", k.avg_order_value == null ? NA : money(k.avg_order_value, false));
    setText("kpiRevenuePerCustomer", k.revenue_per_customer == null ? NA : money(k.revenue_per_customer, false));
    setText("kpiLeakage", money(k.leakage_revenue));
    setText("kpiQuota", k.quota_attainment_pct == null ? NA : `${fmtPct.format(num(k.quota_attainment_pct))}%`);
    setText("kpiNrr", k.nrr_pct == null ? NA : `${fmtPct.format(num(k.nrr_pct))}%`);
    setText(
      "kpiQuotaDelta",
      k.reps_scored_for_quota
        ? `${fmtInt.format(num(k.reps_at_or_above_quota))} of ${fmtInt.format(num(k.reps_scored_for_quota))} reps at or above goal`
        : "Quota data unavailable for this scope"
    );
    setText("kpiInheritedRevenue", k.inherited_revenue == null ? NA : money(k.inherited_revenue));
    setText("srTransferredAccounts", fmtInt.format(num(k.transferred_accounts_count)));
    setText("srTransferredRevenue", `${money(k.transferred_in_revenue)} in | ${money(k.transferred_out_revenue)} out`);

    setDelta("kpiRevenueDelta", k.revenue_mom_pct);
    setDelta("kpiProfitDelta", k.profit_mom_pct);
    setDelta("kpiAovDelta", k.aov_mom_pct);
    setDelta("kpiPpoDelta", k.ppo_mom_pct);
    setText("kpiMarginDelta", marginContextText(k) || (k.margin_mom_pct == null ? NA : fmtSignedPoints(k.margin_mom_pct)));

    // ── 2C: Margin guardrail progress bar ──
    (function () {
      const marginCard = document.getElementById("kpiMargin")?.closest(".sr-kpi");
      if (!marginCard) return;
      const current  = opt(k.margin_pct);
      const minMgn   = opt(k.minimum_margin_pct);
      const targetMgn = opt(k.target_margin_pct);
      if (current === null || minMgn === null || targetMgn === null) return;
      const existingBar = marginCard.querySelector(".sr-margin-bar-wrap");
      if (existingBar) existingBar.remove();
      const scaleMax  = targetMgn + 5;
      const fillPct   = Math.min((current / scaleMax) * 100, 100);
      const colour    = current >= targetMgn ? SR_THEME.forest : current >= minMgn ? SR_THEME.gold : SR_THEME.blood;
      const bar = document.createElement("div");
      bar.className = "sr-margin-bar-wrap";
      bar.title = `Current: ${fmtPct.format(current)}% | Min: ${fmtPct.format(minMgn)}% | Target: ${fmtPct.format(targetMgn)}%`;
      bar.innerHTML = `
        <div class="sr-margin-bar-track">
          <div class="sr-margin-bar-fill" style="width:${fillPct}%;background:${colour}"></div>
        </div>
        <div class="sr-margin-bar-labels">
          <span>Min: ${fmtPct.format(minMgn)}%</span>
          <span>Target: ${fmtPct.format(targetMgn)}%</span>
        </div>
      `;
      marginCard.appendChild(bar);
    })();

    setText("kpiActiveCustomersDelta", `${fmtInt.format(num(k.inherited_customers))} inherited`);
    setText("kpiInheritedRevenueDelta", `${fmtInt.format(num(k.unassigned_customers))} unassigned cust.`);

    const activeReps = k.active_reps || payload.table?.total_rows || 0;
    setText("srActiveRepsChip", `Visible reps: ${fmtInt.format(num(activeReps))}`);
    setText(
      "srModeChip",
      `Mode: ${attribution.attribution_mode === "current_owner" ? "Current Owner Roll-Up" : "Historical Rep View"}`
    );
    setText(
      "srBridgeChip",
      bridge.available && succession.available
        ? `Owner mapping: ${fmtInt.format(num(bridge.rows))} assignments + ${fmtInt.format(num(succession.rows))} successor rules`
        : bridge.available
          ? `Owner mapping: ${fmtInt.format(num(bridge.rows))} assignments`
          : snapshot.available
            ? `Owner mapping: ${fmtInt.format(num(snapshot.rows))} customer owner snapshots`
          : succession.available
            ? `Owner mapping: ${fmtInt.format(num(succession.rows))} successor rules`
            : "Owner mapping: Needs review"
    );

    const coverage = opt(k.ownership_coverage_pct);
    const coverageEl = document.getElementById("srCoverageChip");
    if (coverageEl) {
      if (coverage !== null && num(coverage) >= 95) {
        coverageEl.classList.add("d-none");
        // removed debug log for production

      } else {
        coverageEl.classList.remove("d-none");
        setText("srCoverageChip", coverage == null ? `Coverage: ${NA}` : `Coverage: ${fmtPct.format(coverage)}%`);
      }
    }
    renderSystemHealth(payload);

    setText("srLastRefresh", `Last refresh: ${isoDateLabel(meta.last_refresh || k.last_refresh || meta.dataset_version)}`);

    const whatChangedEl = document.getElementById("srWhatChanged");
    if (whatChangedEl) {
      const insights = Array.isArray(k.what_changed) ? k.what_changed : [k.what_changed || "No major change detected."];
      whatChangedEl.innerHTML = `<ul class="mb-0 ps-3"><li>${insights.join("</li><li>")}</li></ul>`;
    }

    const setStatus = (id, color) => {
      const el = document.getElementById(id);
      if (el) {
        el.className = "status-dot " + color;
      }
    };

    const margin = num(k.margin_pct);
    if (k.margin_pct != null) {
      if (margin >= 30) setStatus("statusMargin", "green");
      else if (margin >= 27) setStatus("statusMargin", "yellow");
      else setStatus("statusMargin", "red");
    } else {
      setStatus("statusMargin", "");
    }
    const marginGap = opt(k.target_gap_pct_points);
    const marginTone = marginGap === null
      ? (k.margin_pct == null ? "" : margin >= 30 ? "green" : margin >= 27 ? "yellow" : "red")
      : marginGap >= 0 ? "green" : marginGap >= -2 ? "yellow" : "red";
    setScorecardBorder("kpiMargin", marginTone);

    const revVariance = opt(k.revenue_yoy_pct) ?? opt(k.revenue_mom_pct);
    const revMom = num(k.revenue_mom_pct);
    if (k.revenue_mom_pct != null) {
      if (revMom > 0) setStatus("statusRevenue", "green");
      else if (revMom >= -5) setStatus("statusRevenue", "yellow");
      else setStatus("statusRevenue", "red");
    } else {
      setStatus("statusRevenue", "");
    }
    const revenueTone = revVariance === null ? "" : revVariance >= 0 ? "green" : revVariance >= -5 ? "yellow" : "red";
    setScorecardBorder("kpiRevenue", revenueTone);
    const attributionSelect = document.getElementById("srAttributionMode");
    if (attributionSelect) attributionSelect.value = state.attributionMode;
    const metricToggle = document.getElementById("srMetricToggle");
    if (metricToggle) metricToggle.value = state.metric;
    const leaderboardDirectOnly = document.getElementById("srLeaderboardDirectOnly");
    if (leaderboardDirectOnly) leaderboardDirectOnly.checked = state.leaderboardScope === "direct_only";
    const trendMetric = document.getElementById("srTrendMetric");
    if (trendMetric) trendMetric.value = state.trendMetric;
    const trendGrain = document.getElementById("srTrendGrain");
    if (trendGrain) trendGrain.value = state.trendGrain;
    const trendView = document.getElementById("srTrendView");
    if (trendView) trendView.value = state.trendView;
    const topNSelect = document.getElementById("srTopN");
    if (topNSelect) topNSelect.value = String(state.topN);
    const formerWrap = document.getElementById("srFormerToggleWrap");
    if (formerWrap) formerWrap.classList.toggle("d-none", state.attributionMode !== "historical_rep");
    const includeFormer = document.getElementById("srIncludeFormerReps");
    if (includeFormer) includeFormer.checked = state.rosterMode === "include_former";
    setText("srInheritedCustomers", fmtInt.format(num(k.inherited_customers)));
    setText(
      "srInheritedCustomersNote",
      `${fmtInt.format(num(k.gained_customers))} gained | ${fmtInt.format(num(k.lost_customers))} lost`
    );
    setText("srUnassignedCustomers", fmtInt.format(num(k.unassigned_customers)));
    setText("srUnassignedRevenueNote", k.unassigned_revenue == null ? NA : money(k.unassigned_revenue));
    setText("srYoyRevenue", k.revenue_yoy_pct == null ? NA : `${fmtPct.format(num(k.revenue_yoy_pct))}%`);
    setText("srYoyProfit", k.profit_yoy_pct == null ? `Profit YoY: ${NA}` : `Profit YoY: ${fmtPct.format(num(k.profit_yoy_pct))}%`);
    setText("srYoyMargin", k.margin_yoy_delta == null ? NA : `${fmtPct.format(num(k.margin_yoy_delta))} pts`);
    setText(
      "srModeNarrative",
      attribution.attribution_mode === "current_owner"
        ? snapshot.available
          ? "Main view rolls inherited history under the current customer owner"
          : "Main view rolls inherited history forward to the current owner"
        : "Performance by rep at time of sale"
    );
    setText(
      "srPortfolioFocus",
      portfolio.visible_rep_count === 1
        ? `Focused owner: ${portfolio.top_rep_name || NA}`
        : `Visible portfolio: ${fmtInt.format(num(portfolio.visible_rep_count || activeReps))} reps`
    );
    setText("srPortfolioTerritoryCount", fmtInt.format(num(k.territory_count)));
    setText(
      "srPortfolioTopTerritory",
      portfolio.territories?.[0]?.territory_name
        ? `${portfolio.territories[0].territory_name} · ${money(portfolio.territories[0].revenue)}`
        : "No active territory mix"
    );
    setText("srPortfolioReplacedCount", fmtInt.format(num(k.replaced_rep_count)));
    setText(
      "srPortfolioReplacedNames",
      portfolio.replacement_names?.length ? portfolio.replacement_names.slice(0, 3).join(", ") : "No inherited predecessor reps"
    );

    setText("srDirectRevenue", k.direct_revenue == null ? NA : money(k.direct_revenue));
    setText(
      "srDirectRevenueNote",
      k.revenue ? `${fmtPct.format((num(k.direct_revenue) / Math.max(num(k.revenue), 1)) * 100)}% of visible revenue` : "No direct book in scope"
    );
    setText("srDirectCustomers", fmtInt.format(num(k.direct_customers)));
    setText("srDirectCustomersNote", `${fmtInt.format(num(k.inherited_customers))} inherited cust.`);

    const concentrationChip = (analysis.insights?.chips || []).find((chip) => chip.key === "largest_concentration_risk");
    setText("srConcentrationSummary", concentrationChip?.rep_name || "No outsized risk");
    setText(
      "srConcentrationNote",
      concentrationChip?.display_value ? `Top customer share ${concentrationChip.display_value}` : "Largest books remain reasonably diversified"
    );
    setText(
      "srAccountMovesSummary",
      `${fmtInt.format(num(k.largest_gained_accounts_count))} / ${fmtInt.format(num(k.largest_lost_accounts_count))}`
    );
    setText(
      "srAccountMovesNote",
      `${fmtInt.format(num(k.gained_customers))} gained | ${fmtInt.format(num(k.lost_customers))} lost`
    );

    setDrillPayload(
      document.getElementById("kpiRevenue")?.closest(".sr-kpi"),
      attributedWorkspacePayload("Executive Scorecard", "Revenue", "Revenue", k.revenue, {
        filter_mode: "current_window",
        detail: "Attributed current-window revenue under the visible owner portfolio.",
      })
    );
    setDrillPayload(
      document.getElementById("kpiProfit")?.closest(".sr-kpi"),
      attributedWorkspacePayload("Executive Scorecard", "Profit", "Profit", k.profit, {
        filter_mode: "current_window",
        detail: "Attributed current-window profit under the visible owner portfolio.",
      })
    );
    setDrillPayload(
      document.getElementById("kpiCustomers")?.closest(".sr-kpi"),
      attributedWorkspacePayload("Executive Scorecard", "Customers", "Customers", k.customers, {
        filter_mode: "current_window",
        detail: "Distinct customers covered by visible sales reps under the active filter window.",
      })
    );
    setDrillPayload(
      document.getElementById("kpiActiveCustomers")?.closest(".sr-kpi"),
      attributedWorkspacePayload("Executive Scorecard", "Active Customers", "Active Customers", k.active_customers, {
        filter_mode: "current_window",
        detail: "Active customers in the visible owner portfolio.",
      })
    );
    setDrillPayload(
      document.getElementById("kpiInheritedRevenue")?.closest(".sr-kpi"),
      attributedWorkspacePayload("Executive Scorecard", "Inherited Revenue", "Inherited Revenue", k.inherited_revenue, {
        filter_mode: "current_window",
        inherited_only: true,
        detail: "Orders attributed to inherited customers under the current-owner roll-up.",
      })
    );
    setDrillPayload(
      document.getElementById("srTransferredAccounts")?.closest(".sr-kpi"),
      attributedWorkspacePayload("Executive Scorecard", "Transferred Accounts", "Transferred Accounts", k.transferred_accounts_count, {
        filter_mode: "current_window",
        transfer_activity_only: true,
        detail: "Transferred account activity in the current owner portfolio.",
      })
    );
    setDrillPayload(
      document.getElementById("srDirectRevenueCard"),
      attributedWorkspacePayload("Insight Strip", "Direct Revenue", "Direct Revenue", k.direct_revenue, {
        filter_mode: "current_window",
        direct_only: true,
      })
    );
    setDrillPayload(
      document.getElementById("srDirectCustomersCard"),
      attributedWorkspacePayload("Insight Strip", "Direct Customers", "Direct Customers", k.direct_customers, {
        filter_mode: "current_window",
        direct_only: true,
      })
    );
    setDrillPayload(
      document.getElementById("srConcentrationCard"),
      concentrationChip?.rep_id
        ? salesrepPayload({ rep_id: concentrationChip.rep_id, rep_name: concentrationChip.rep_name }, "Insight Strip", "Concentration Risk", "Top customer share", concentrationChip.metric_value)
        : null
    );
    setDrillPayload(
      document.getElementById("srAccountMovesCard"),
      attributedWorkspacePayload("Insight Strip", "Largest Moves", "Customer Moves", k.gained_customers, {
        filter_mode: "current_window",
        detail: "Customers with the largest period-over-period movement in the visible portfolio.",
      })
    );
    setScorecardLoading(false);

    // ── 6D: Dynamic section subtitles ──
    (function () {
      const activeRepsN = num(k.active_reps || payload.table?.total_rows || 0);
      const periodLabel = meta.date_range_label || meta.period_label || "";
      const repScope = `${fmtInt.format(activeRepsN)} rep${activeRepsN !== 1 ? "s" : ""} in scope`;
      if (periodLabel) {
        setText("srSectionLeadershipSubtitle", `${periodLabel} · ${repScope} · ${money(k.revenue)} total revenue`);
      }
      const coverage = opt(k.ownership_coverage_pct);
      if (coverage != null) {
        setText("srSectionOwnershipSubtitle", `${fmtPct.format(coverage)}% owner coverage · ${fmtInt.format(num(k.inherited_customers))} inherited customers · ${fmtInt.format(num(k.territory_count))} territories`);
      }
      const mc = payload.charts?.monthly_compare ?? payload.trend?.monthly_compare ?? {};
      const periodCount = (mc.labels || []).length;
      if (periodCount) {
        setText("srSectionTrendSubtitle", `${periodCount} comparable periods · rep-level momentum and YoY overlap · click any series to focus`);
      }
      if (k.active_customers) {
        setText("srSectionCustomerSubtitle", `${fmtInt.format(num(k.active_customers))} active customers · click Best / At-Risk to filter by health`);
      }
    })();

    // ── 2A: KPI micro-sparklines ──
    (function () {
      const mc = payload.charts?.monthly_compare ?? payload.trend?.monthly_compare ?? {};
      const sparkDefs = [
        { kpiId: "kpiRevenue",  data: mc.revenue  ?? [] },
        { kpiId: "kpiProfit",   data: mc.profit   ?? [] },
        { kpiId: "kpiWeight",   data: mc.weight_lb ?? [] },
        { kpiId: "kpiAov",      data: mc.avg_order_value ?? [] },
        { kpiId: "kpiPpo",      data: mc.profit_per_order ?? [] },
      ];
      sparkDefs.forEach(({ kpiId, data }) => {
        const card = document.getElementById(kpiId)?.closest(".sr-kpi");
        if (!card) return;
        card.querySelector(".sr-kpi-sparkline")?.remove();
        const vals = data.filter((v) => v != null).slice(-12);
        if (vals.length < 3) return;
        const W = 72, H = 22, pad = 2;
        const lo = Math.min(...vals), hi = Math.max(...vals);
        const range = hi - lo || 1;
        const toX = (i) => pad + ((i / (vals.length - 1)) * (W - pad * 2));
        const toY = (v) => H - pad - ((v - lo) / range) * (H - pad * 2);
        const pts = vals.map((v, i) => `${toX(i).toFixed(1)},${toY(v).toFixed(1)}`).join(" ");
        const trend = vals[vals.length - 1] >= vals[0];
        const lineColor = trend ? SR_THEME.oxblood : SR_THEME.gold;
        const polyFill = [
          `${toX(0).toFixed(1)},${H - pad}`,
          ...vals.map((v, i) => `${toX(i).toFixed(1)},${toY(v).toFixed(1)}`),
          `${toX(vals.length - 1).toFixed(1)},${H - pad}`,
        ].join(" ");
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
        svg.setAttribute("width", W);
        svg.setAttribute("height", H);
        svg.classList.add("sr-kpi-sparkline");
        svg.setAttribute("aria-hidden", "true");
        svg.innerHTML = `
          <polygon points="${polyFill}" fill="${lineColor}" fill-opacity="0.12"/>
          <polyline points="${pts}" fill="none" stroke="${lineColor}" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>
        `;
        card.appendChild(svg);
      });
    })();
  };

  const renderWarnings = (warnings = [], payload = {}) => {
    const holder = document.getElementById("srWarnings");
    if (!holder) return;
    const rows = Array.isArray(warnings) ? warnings.filter(Boolean) : [];
    if (!rows.length) {
      holder.innerHTML = "";
      return;
    }
    const attribution = payload?.meta?.attribution || {};
    const header = attribution.attribution_mode === "current_owner" ? "Current owner roll-up notes" : "Historical attribution notes";
    holder.innerHTML = `
      <div class="alert alert-warning border-0 mb-0" role="status">
        <div class="fw-semibold mb-1">${header}</div>
        <ul class="mb-0 ps-3">
          ${rows.slice(0, 4).map((msg) => `<li>${escapeHtml(msg)}</li>`).join("")}
        </ul>
      </div>
    `;
  };

  const renderInsights = (payload = {}) => {
    const analysis = payload.analysis || {};
    const insights = analysis.insights || {};
    const chipsHolder = document.getElementById("srInsightChips");
    const narrativeEl = document.getElementById("srInsightNarrative");
    if (narrativeEl) {
      narrativeEl.textContent = cleanText(insights.narrative) || cleanText(payload.kpis?.what_changed) || "No standout signal detected in the visible slice.";
    }
    if (!chipsHolder) return;
    const chips = Array.isArray(insights.chips) ? insights.chips : [];
    if (!chips.length) {
      chipsHolder.innerHTML = '<span class="sr-empty-list">No major directional signals are active in the current slice.</span>';
      return;
    }
    chipsHolder.innerHTML = chips.map((chip) => {
      const chipPayload = chip?.rep_id
        ? salesrepPayload({ rep_id: chip.rep_id, rep_name: chip.rep_name }, "Insight Strip", chip.label, chip.metric_key || chip.label, chip.metric_value)
        : null;
      const repName = chip.rep_name;
      const clickable = !!repName;
      const clickAttr = clickable
        ? ` onclick="srChipClick(${JSON.stringify(escapeHtml(repName))})" title="Click to find ${escapeHtml(repName)} in the table below"`
        : "";
      return `
        <span class="sr-chip${clickable ? " is-clickable" : ""}"${drillAttr(chipPayload)}${clickAttr}>
          <span class="sr-chip-label">${escapeHtml(chip.label || "Signal")}</span>
          <span class="sr-chip-value">${escapeHtml(repName || "--")} ${escapeHtml(chip.display_value || "")}</span>
        </span>
      `;
    }).join("");
  };

  const buildSummaryNarrative = (payload = {}) => {
    const kpis = payload.kpis || {};
    const insights = payload.analysis?.insights || {};
    const proteins = Array.isArray(payload.analysis?.proteins) ? payload.analysis.proteins : [];
    const avgMarginPct = opt(payload.benchmarks?.avg_margin_pct);
    const revenueMoM = opt(kpis.revenue_mom_pct);
    const highestGrowth = (insights.chips || []).find((chip) => chip.key === "highest_growth_rep");
    const biggestDrag = (insights.chips || []).find((chip) => chip.key === "biggest_yoy_drag");
    const proteinDrag = avgMarginPct == null
      ? null
      : proteins
        .map((row) => {
          const marginPct = opt(row.margin_pct);
          if (marginPct == null) return null;
          const gapPts = avgMarginPct - marginPct;
          return gapPts > 0
            ? { ...row, gap_pts: gapPts, drag_score: gapPts * Math.max(num(row.revenue), 1) }
            : null;
        })
        .filter(Boolean)
        .sort((a, b) => num(b.drag_score) - num(a.drag_score))[0];

    const parts = [];
    if (revenueMoM !== null) {
      parts.push(`Revenue is ${revenueMoM >= 0 ? "up" : "down"} ${fmtPct.format(Math.abs(revenueMoM))}% MoM`);
    } else {
      parts.push("Comparable MoM revenue is unavailable for the current window");
    }

    if (proteinDrag?.protein_family) {
      parts.push(
        `${proteinDrag.protein_family} is dragging total margin by ${fmtPct.format(num(proteinDrag.gap_pts))} pts versus the portfolio average`
      );
    } else if (revenueMoM !== null && revenueMoM < 0 && biggestDrag?.rep_name) {
      parts.push(`${biggestDrag.rep_name} is the sharpest YoY drag at ${biggestDrag.display_value || NA}`);
    }

    if (highestGrowth?.rep_name) {
      parts.push(`${highestGrowth.rep_name} is the strongest growth rep at ${highestGrowth.display_value || NA}`);
    }

    parts.push(
      revenueMoM !== null && revenueMoM < 0
        ? "Recommend reviewing pricing guardrails and near-term recovery plays."
        : "Use the leading rep and department mix as the template for the next action."
    );

    return `${parts.filter(Boolean).join(". ")}.`;
  };

  // ── Business Overview Signal Pills (3 fast-scan executive signals) ──
  const renderSummaryNarrative = (payload = {}) => {
    const el = document.getElementById("srSummaryNarrative");
    if (!el) return;

    const kpis = payload.kpis || {};
    const analysis = payload.analysis || {};
    const insights = analysis.insights || {};
    const riskFlags = Array.isArray(payload.risk_flags) ? payload.risk_flags : [];
    const chips = Array.isArray(insights.chips) ? insights.chips : [];

    // Executive Narrative Logic (Performance Story)
    let storyHtml = "";
    const revMoM = opt(kpis.revenue_mom_pct);
    const freshRow = (analysis.proteins || []).find(p => p.protein_family === "Fresh & Produce");
    if (freshRow && revMoM !== null && revMoM < -10) {
      const freshMargin = opt(freshRow.margin_pct);
      if (freshMargin !== null && freshMargin < 22) {
        storyHtml = `
          <div class="alert alert-danger border-0 shadow-sm mb-3 py-3 px-4" style="background: #fff5f5; border-left: 5px solid #ef4444 !important;">
            <div class="d-flex align-items-center gap-2 mb-1">
              <span class="badge bg-danger">Critical Alert</span>
              <span class="fw-bold text-danger">Executive Briefing</span>
            </div>
            <div class="fs-5 text-dark fw-semibold">
              ${money(kpis.revenue)} Portfolio showing ${Math.abs(revMoM).toFixed(1)}% momentum loss driven by Fresh margin compression (${freshMargin.toFixed(1)}% avg margin).
            </div>
          </div>
        `;
      }
    }

    if (!storyHtml) {
      const direction = (revMoM || 0) >= 0 ? "up" : "down";
      storyHtml = `
        <div class="alert alert-info border-0 shadow-sm mb-3 py-3 px-4" style="background: #f0f9ff; border-left: 5px solid #0ea5e9 !important;">
          <div class="fw-bold text-info mb-1 small text-uppercase">Performance Narrative</div>
          <div class="fs-5 text-dark fw-semibold">
            The portfolio is trending ${direction} ${Math.abs(revMoM || 0).toFixed(1)}% month-over-month. 
            ${kpis.active_customers || 0} active accounts contributing ${money(kpis.revenue)}.
          </div>
        </div>
      `;
    }

    // Signal 1 — Momentum: revenue velocity MoM
    const momentumCls = revMoM === null ? "sr-signal-neutral"
      : revMoM >= 5  ? "sr-signal-positive"
      : revMoM >= 0  ? "sr-signal-caution"
      : "sr-signal-negative";
    const momentumIcon = revMoM === null ? "◦" : revMoM >= 0 ? "▲" : "▼";
    const momentumText = revMoM === null
      ? "Revenue Velocity: N/A"
      : `Revenue Velocity: ${revMoM >= 0 ? "+" : ""}${fmtPct.format(revMoM)}%`;

    // Signal 2 — Risk: count of accounts silent > 45 days
    const silentCount = riskFlags.filter((f) => f.flag === "silent_account" || (f.silent_days != null && num(f.silent_days) > 45)).length;
    const criticalCount = riskFlags.filter((f) => f.severity === "critical" || (f.silent_days != null && num(f.silent_days) > 90)).length;
    const riskCls = criticalCount > 0 ? "sr-signal-negative" : silentCount > 0 ? "sr-signal-caution" : "sr-signal-positive";
    const riskText = criticalCount > 0
      ? `${criticalCount} Critical Account${criticalCount !== 1 ? "s" : ""} Silent`
      : silentCount > 0
      ? `${silentCount} Account${silentCount !== 1 ? "s" : ""} At Risk`
      : "No Critical Silent Accounts";

    // Signal 3 — Winner: top growth rep
    const topGrowthChip = chips.find((c) => c.key === "highest_growth_rep");
    const topGrowthName = topGrowthChip?.rep_name || topGrowthChip?.display_value || null;
    const topGrowthVal = topGrowthChip?.display_value || "";
    const winnerCls = topGrowthName ? "sr-signal-positive" : "sr-signal-neutral";
    const winnerText = topGrowthName
      ? `Top Growth: ${topGrowthName}${topGrowthVal ? " · " + topGrowthVal : ""}`
      : "Top Growth: N/A";

    el.innerHTML = `
      ${storyHtml}
      <div class="sr-signal-pills" aria-label="Executive signal overview">
        <div class="sr-signal-pill ${momentumCls}" title="Revenue momentum: month-over-month change">
          <span class="sr-signal-icon">${momentumIcon}</span>
          <div class="sr-signal-content">
            <span class="sr-signal-type">Momentum</span>
            <span class="sr-signal-value">${escapeHtml(momentumText)}</span>
          </div>
        </div>
        <div class="sr-signal-pill ${riskCls}" title="Silent accounts — customers with no orders beyond the risk threshold">
          <span class="sr-signal-icon">⚑</span>
          <div class="sr-signal-content">
            <span class="sr-signal-type">Risk</span>
            <span class="sr-signal-value">${escapeHtml(riskText)}</span>
          </div>
        </div>
        <div class="sr-signal-pill ${winnerCls}" title="Highest month-over-month revenue growth rep in this view">
          <span class="sr-signal-icon">★</span>
          <div class="sr-signal-content">
            <span class="sr-signal-type">Winner</span>
            <span class="sr-signal-value">${escapeHtml(winnerText)}</span>
          </div>
        </div>
      </div>
    `;
    setSummaryNarrativeLoading(false);
  };

  // ── 2B: Signal chip → anchor-jump to rep table ──
  window.srChipClick = (repName) => {
    const tableSection = document.getElementById("srTableSection");
    tableSection?.scrollIntoView({ behavior: "smooth", block: "start" });
    const searchInput = document.getElementById("srSearchInput");
    if (searchInput) {
      searchInput.value = repName;
      searchInput.dispatchEvent(new Event("input", { bubbles: true }));
    }
    setTimeout(() => {
      document.querySelectorAll("#srTable tbody tr").forEach((row) => {
        if (row.textContent.includes(repName)) {
          row.style.background = SR_THEME.tan;
          setTimeout(() => { row.style.background = ""; }, 1000);
        }
      });
    }, 300);
  };

  const renderTrend = (trend = {}) => {
    const canvasId = "trendChart";
    const detailRows = aggregateRepTrendDetail(trend.detail || [], state.trendGrain);
    const chartMetric = state.trendMetric;
    const selectionEl = document.getElementById("srTrendSelectionSummary");
    const resetEl = document.getElementById("srTrendReset");
    const topList = new Set(
      sortRows(
        Array.from(new Set(detailRows.map((row) => row.rep_id))).map((repId) => {
          const repRows = detailRows.filter((row) => row.rep_id === repId);
          return {
            rep_id: repId,
            rep_name: repRows[0]?.rep_name,
            total: repRows.reduce((acc, row) => acc + Math.abs(trendMetricValue(row, chartMetric)), 0),
          };
        }),
        "total",
        "desc"
      ).slice(0, state.topN).map((row) => row.rep_id)
    );

    state.trendSelectedReps = state.trendSelectedReps.filter((repId) => detailRows.some((row) => row.rep_id === repId));
    const visibleRepIds = state.trendFocusMode && state.trendSelectedReps.length
      ? state.trendSelectedReps
      : Array.from(new Set([...topList, ...state.trendSelectedReps]));

    const repSeries = visibleRepIds.map((repId) => {
      const points = detailRows.filter((row) => row.rep_id === repId).sort((a, b) => cleanText(a.bucket).localeCompare(cleanText(b.bucket)));
      return { rep_id: repId, rep_name: businessRepName(points[0]?.rep_name, repId, READABLE_REP_FALLBACK), points };
    }).filter((row) => row.points.length);

    // Only include buckets where at least one rep has real current-period activity.
    // Excluding YoY-only months (revenue=0, revenue_yoy>0) prevents phantom x-axis
    // entries that break lines with spanGaps:false.
    const activeSet = new Set();
    repSeries.forEach((series) => {
      series.points.forEach((point) => {
        if (trendMetricValue(point, chartMetric) > 0) activeSet.add(point.bucket);
      });
    });
    const allBuckets = Array.from(activeSet).sort((a, b) => cleanText(a).localeCompare(cleanText(b)));

    const hasData = repSeries.length > 0 && allBuckets.length > 0;
    const emptyMessage = state.trendGrain === "ttm"
      ? "Need at least 12 monthly periods to render trailing 12M rep trends."
      : "No rep trend detail is available for the selected filters.";
    toggleEmpty(canvasId, !hasData, emptyMessage);
    if (selectionEl) {
      if (state.trendFocusMode && state.trendSelectedReps.length) {
        selectionEl.textContent = `Focus mode: ${state.trendSelectedReps.map((repId) => businessRepName(repSeries.find((row) => row.rep_id === repId)?.rep_name, repId, READABLE_REP_FALLBACK)).join(", ")}.`;
      } else if (state.trendSelectedReps.length) {
        selectionEl.textContent = `Showing Top ${state.topN} plus selected reps: ${state.trendSelectedReps.map((repId) => businessRepName(repSeries.find((row) => row.rep_id === repId)?.rep_name, repId, READABLE_REP_FALLBACK)).join(", ")}.`;
      } else {
        selectionEl.textContent = `Showing Top ${state.topN} visible reps using ${state.trendGrain === "ttm" ? "trailing 12M" : state.trendGrain} ${state.trendView === "absolute" ? "value" : state.trendView === "yoy_delta" ? "YoY %" : "index"} mode.`;
      }
    }
    if (resetEl) resetEl.classList.toggle("d-none", !state.trendSelectedReps.length);
    if (!ChartLib) return;
    if (!hasData) {
      destroyChart("trend");
      return;
    }

    const conf = metricConfig[chartMetric] || metricConfig.revenue;
    const viewLabel = state.trendView === "absolute" ? conf.label : state.trendView === "yoy_delta" ? `${conf.label} YoY %` : `${conf.label} Index`;
    const datasets = repSeries.map((series, idx) => {
      const pointMap = new Map(series.points.map((point) => [point.bucket, point]));
      const alignedPoints = allBuckets.map((bucket) => pointMap.get(bucket) || null);
      const basePoint = alignedPoints.find((point) => point && trendMetricValue(point, chartMetric) > 0);
      const baseValue = basePoint ? trendMetricValue(basePoint, chartMetric) : null;
      const metaPoints = alignedPoints.map((point, pointIdx) => {
        const current = point ? trendMetricValue(point, chartMetric) : null;
        const prior = point ? trendMetricPriorValue(point, chartMetric) : null;
        const comparableYoY = comparableObservedDays(point?.observed_days, point?.observed_days_yoy);
        // Use prior > 0 guard only (same as monthly compare chart) — comparableObservedDays is too strict for monthly grains
        const yoyPct = current != null && prior != null && prior > 0 ? ((current - prior) / Math.abs(prior)) * 100 : null;
        const displayValue = state.trendView === "absolute"
          ? current
          : state.trendView === "yoy_delta"
            ? yoyPct
            : baseValue && current != null
              ? (current / baseValue) * 100
              : null;
        return {
          ...point,
          current,
          prior,
          yoyPct,
          comparableYoY,
          momPct: trendMoM(alignedPoints, pointIdx, chartMetric),
          displayValue,
          label: bucketLabelFromKey(allBuckets[pointIdx], state.trendGrain, state.trendGrain === "ttm"),
          rawBucket: allBuckets[pointIdx],
        };
      });
      return {
        label: series.rep_name,
        repId: series.rep_id,
        metricLabel: viewLabel,
        metaPoints,
        data: metaPoints.map((point) => {
          const v = point.displayValue;
          // For absolute revenue view: treat 0 as null so reps start from their
          // first real order month, not a flat $0 line from period start
          if (state.trendView === "absolute" && (v === 0 || v === null)) return null;
          return v ?? null;
        }),
        borderColor: stableColor(idx),
        backgroundColor: `${stableColor(idx)}22`,
        // ── Phase 2: top rep gets thicker line and larger points ──
        borderWidth: state.trendFocusMode ? 3 : (idx === 0 ? 2.5 : 1.5),
        tension: 0.28,
        spanGaps: false,   // false = show true start date, no phantom line before first order
        pointRadius: state.trendFocusMode ? 3 : (idx === 0 ? 5 : 3),
        pointHoverRadius: 5,
        fill: false,
      };
    });

    const chart = createChart("trend", canvasId, {
      type: "line",
      data: {
        labels: allBuckets.map((bucket) => bucketLabelFromKey(bucket, state.trendGrain, state.trendGrain === "ttm")),
        datasets,
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "nearest", intersect: false },
        onClick: (_evt, activeEls, chartInstance) => {
          const hit = activeEls?.[0];
          if (!hit) return;
          const ds = chartInstance?.data?.datasets?.[hit.datasetIndex];
          const metaPoint = ds?.metaPoints?.[hit.index];
          if (!ds?.repId || !metaPoint) return;
          const payload = repWorkspacePayload(
            { rep_id: ds.repId, rep_name: ds.label },
            "Trend Intelligence",
            "Revenue Trend by Rep",
            ds.metricLabel,
            metaPoint.displayValue,
            {
              filter_mode: metaPoint.yoyPct != null && state.trendView === "yoy_delta" ? "comparison_window" : "current_window",
              include_yoy_window: metaPoint.yoyPct != null && state.trendView === "yoy_delta",
            }
          );
          payload.clicked_time_grain = state.trendGrain === "quarterly" ? "quarter" : state.trendGrain === "yearly" ? "year" : "month";
          payload.clicked_time_value = metaPoint.rawBucket;
          openUniversal(payload, document.getElementById(canvasId));
        },
        plugins: {
          legend: {
            position: "bottom",
            onClick: (evt, item, legend) => {
              const ds = legend?.chart?.data?.datasets?.[item.datasetIndex];
              const repId = ds?.repId;
              if (!repId) return;
              const multi = !!(evt?.native?.ctrlKey || evt?.native?.metaKey || evt?.native?.shiftKey);
              if (multi) {
                state.trendFocusMode = false;
                state.trendSelectedReps = state.trendSelectedReps.includes(repId)
                  ? state.trendSelectedReps.filter((value) => value !== repId)
                  : [...state.trendSelectedReps, repId];
              } else {
                const alreadyFocused = state.trendFocusMode && state.trendSelectedReps.length === 1 && state.trendSelectedReps[0] === repId;
                state.trendFocusMode = !alreadyFocused;
                state.trendSelectedReps = alreadyFocused ? [] : [repId];
              }
              renderTrend(lastPayload?.charts?.trend || lastPayload?.trend || {});
            },
          },
          tooltip: {
            filter: (item) => item.parsed?.y != null,
            itemSort: (a, b) => (b.parsed?.y ?? 0) - (a.parsed?.y ?? 0),
            callbacks: {
              beforeBody: (items) => {
                if (items.length > 5) items.splice(5);
              },
              title: (items) => items?.[0]?.dataset?.metaPoints?.[items?.[0]?.dataIndex]?.label || items?.[0]?.label || "",
              label: (ctx) => {
                const point = ctx.dataset?.metaPoints?.[ctx.dataIndex];
                if (!point) return `${ctx.dataset.label}: ${ctx.formattedValue}`;
                if (state.trendView === "yoy_delta") return `${ctx.dataset.label}: ${point.displayValue == null ? NA : `${fmtPct.format(num(point.displayValue))}%`}`;
                if (state.trendView === "index") return `${ctx.dataset.label}: ${point.displayValue == null ? NA : fmtInt.format(num(point.displayValue))}`;
                // ── 5B: enhanced tooltip: rep — value · ±X% vs prior month ──
                const valStr = trendMetricFormatter(chartMetric, point.displayValue);
                const momStr = point.momPct != null ? ` · ${point.momPct >= 0 ? "+" : ""}${fmtPct.format(point.momPct)}% MoM` : "";
                return `${ctx.dataset.label} \u2014 ${valStr}${momStr}`;
              },
              afterLabel: (ctx) => {
                const point = ctx.dataset?.metaPoints?.[ctx.dataIndex];
                if (!point) return [];
                const lines = [];
                if (point.current != null && state.trendView !== "absolute") lines.push(`Current: ${trendMetricFormatter(chartMetric, point.current)}`);
                if (point.prior != null) lines.push(`Prior year: ${trendMetricFormatter(chartMetric, point.prior)}`);
                if (point.yoyPct != null) lines.push(`YoY: ${point.yoyPct >= 0 ? "+" : ""}${fmtPct.format(point.yoyPct)}%`);
                if (point.momPct != null) lines.push(`MoM: ${point.momPct >= 0 ? "+" : ""}${fmtPct.format(point.momPct)}%`);
                if (point.direct_revenue != null || point.inherited_revenue != null) {
                  lines.push(`Direct / Inherited: ${money(point.direct_revenue)} / ${money(point.inherited_revenue)}`);
                }
                if (point.customers != null) lines.push(`Customers: ${fmtInt.format(num(point.customers))}`);
                if (point.observed_days != null || point.observed_days_yoy != null) {
                  lines.push(`Observed days: ${fmtInt.format(num(point.observed_days))} / ${fmtInt.format(num(point.observed_days_yoy))}`);
                }
                return lines;
              },
            },
          },
        },
        scales: {
          y: {
            ticks: {
              callback: (value) => {
                if (state.trendView === "yoy_delta") return `${fmtPct.format(value)}%`;
                if (state.trendView === "index") return fmtInt.format(value);
                return conf.fmt(value);
              },
            },
          },
        },
      },
    });
    if (!chart) toggleEmpty(canvasId, true, "Chart unavailable.");
  };

  const renderMonthlyCompare = (chartData = {}) => {
    const canvasId = "monthlyCompareChart";
    const detail = Array.isArray(chartData.detail) ? chartData.detail : [];
    const rows = detail.length
      ? detail
      : (Array.isArray(chartData.labels) ? chartData.labels.map((label, idx) => ({
        bucket: label,
        revenue: chartData.revenue?.[idx],
        revenue_yoy: chartData.revenue_yoy?.[idx],
        profit: chartData.profit?.[idx],
        profit_yoy: chartData.profit_yoy?.[idx],
        weight_lb: chartData.weight_lb?.[idx],
        weight_lb_yoy: chartData.weight_lb_yoy?.[idx],
      })) : []);
    const labels = rows.map((row) => bucketLabelFromKey(row.bucket, "monthly"));
    const revenue = rows.map((row) => num(row.revenue) || null);   // null skips bar for empty months
    const revenueYoY = rows.map((row) => {
      const v = opt(row.revenue_yoy);
      return v !== null && v > 0 ? v : null;                        // null for missing/zero/negative prior
    });
    const yoyPct = rows.map((row) => {
      const current = num(row.revenue);
      const prior = opt(row.revenue_yoy);
      // Remove strict comparableObservedDays guard — just require positive prior revenue
      return prior !== null && prior > 0
        ? ((current - prior) / Math.abs(prior)) * 100
        : null;
    });
    const hasData = rows.length > 0 && rows.some((row) => num(row.revenue) !== 0 || opt(row.revenue_yoy) !== null);

    toggleEmpty(canvasId, !hasData);
    if (!ChartLib) return;
    if (!hasData) {
      destroyChart("monthlyCompare");
      return;
    }

    // ── Phase 1 fix: calibrate y1 (right axis) from actual YoY values ──
    const validYoy = yoyPct.filter((v) => v != null);
    const y1Min = validYoy.length ? Math.floor(Math.min(...validYoy) - 3) : -25;
    const y1Max = validYoy.length ? Math.ceil(Math.max(...validYoy) + 3) : 5;

    // "Today" marker: find current month bucket in labels array
    const todayBucket = new Date().toISOString().slice(0, 7);  // "YYYY-MM"
    const rawBuckets = rows.map((r) => r.bucket || "");
    const todayIndex = rawBuckets.findIndex((b) => String(b).slice(0, 7) === todayBucket);

    const chart = createChart("monthlyCompare", canvasId, {
      type: "bar",                                     // ← root type bar for proper grouping
      data: {
        labels,
        datasets: [
          {
            type: "bar",
            label: "Current Revenue",
            data: revenue,
            yAxisID: "y",
            borderColor: SR_THEME.oxblood,
            backgroundColor: alphaColor(SR_THEME.oxblood, 0.68),
            borderWidth: 1,
            borderRadius: 4,
            order: 2,
          },
          {
            type: "bar",
            label: "Prior-Year Revenue",
            data: revenueYoY,
            yAxisID: "y",
            borderColor: SR_THEME.gold,
            backgroundColor: alphaColor(SR_THEME.gold, 0.42),
            borderWidth: 1,
            borderRadius: 4,
            order: 2,
          },
          {
            type: "line",
            label: "YoY %",
            data: yoyPct,
            yAxisID: "y1",
            borderColor: SR_THEME.forest,
            backgroundColor: alphaColor(SR_THEME.forest, 0.12),
            borderWidth: 2,
            tension: 0.25,
            pointRadius: 4,
            pointBackgroundColor: SR_THEME.forest,
            fill: false,
            spanGaps: true,
            order: 1,                                  // ← render on top of bars
          },
        ],
      },
      plugins: [
        {
          id: "mcTodayMarker",
          afterDraw: (chartInst) => {
            if (todayIndex < 0) return;
            const xScale = chartInst.scales.x;
            const yScale = chartInst.scales.y;
            if (!xScale || !yScale) return;
            const x = xScale.getPixelForValue ? xScale.getPixelForValue(todayIndex) : xScale.getPixelForIndex(todayIndex);
            const ctx2 = chartInst.ctx;
            ctx2.save();
            ctx2.beginPath();
            ctx2.moveTo(x, yScale.top);
            ctx2.lineTo(x, yScale.bottom);
            ctx2.strokeStyle = SR_THEME.oxblood;
            ctx2.lineWidth = 1.5;
            ctx2.setLineDash([4, 3]);
            ctx2.stroke();
            ctx2.setLineDash([]);
            ctx2.fillStyle = SR_THEME.oxblood;
            ctx2.font = '10px "Avenir Next", "Segoe UI", sans-serif';
            ctx2.fillText("Today", x + 4, yScale.top + 12);
            ctx2.restore();
          },
        },
      ],
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        onClick: (_evt, activeEls) => {
          const idx = activeEls?.[0]?.index;
          if (idx == null) return;
          const row = rows[idx];
          if (!row) return;
          const payload = attributedWorkspacePayload("Trend Intelligence", "Current vs Prior-Year Trend", "Revenue", row.revenue, {
            filter_mode: "comparison_window",
            include_yoy_window: true,
          });
          payload.clicked_time_grain = "month";
          payload.clicked_time_value = row.bucket;
          openUniversal(payload, document.getElementById(canvasId));
        },
        plugins: {
          legend: {
            position: "top",
            labels: { font: { size: 12, family: "Inter, sans-serif" } },
          },
          tooltip: {
            callbacks: {
              title: (items) => items?.[0]?.label || "",
              label: (ctx) => {
                if (ctx.dataset.label === "YoY %") {
                  const v = ctx.parsed.y;
                  if (v == null) return null;           // hide null YoY% entirely
                  return `YoY Change: ${(v >= 0 ? "+" : "") + fmtPct.format(v) + "%"}`;
                }
                const v = ctx.parsed.y;
                if (v == null) return null;             // hide null bars — don't show "$0"
                return `${ctx.dataset.label}: ${fmtMoney0.format(v)}`;
              },
              afterBody: (items) => {
                const idx = items?.[0]?.dataIndex;
                const row = idx == null ? null : rows[idx];
                if (!row) return [];
                const lines = [];
                // Revenue delta vs prior year
                const cur = opt(row.revenue);
                const prior = opt(row.revenue_yoy);
                if (cur != null && prior != null && prior > 0) {
                  const delta = cur - prior;
                  const sign = delta >= 0 ? "+" : "";
                  lines.push(`vs Prior Year: ${sign}${fmtMoney0.format(delta)}`);
                }
                if (row.direct_revenue != null || row.inherited_revenue != null) {
                  lines.push(`Direct / Inherited: ${money(row.direct_revenue)} / ${money(row.inherited_revenue)}`);
                }
                if (row.customers != null || row.customers_yoy != null) {
                  lines.push(`Customers: ${fmtInt.format(num(row.customers))} / ${fmtInt.format(num(row.customers_yoy))} (curr / prior)`);
                }
                if (row.observed_days != null || row.observed_days_yoy != null) {
                  lines.push(`Observed days: ${fmtInt.format(num(row.observed_days))} / ${fmtInt.format(num(row.observed_days_yoy))}`);
                }
                return lines;
              },
            },
          },
        },
        scales: {
          x: {
            type: "category",
            stacked: false,
            grid: { display: false },
            ticks: { maxRotation: 45, font: { size: 11 } },
          },
          y: {
            type: "linear",
            position: "left",
            stacked: false,
            beginAtZero: true,
            ticks: { callback: (v) => fmtMoney0.format(v) },
            grid: { color: SR_THEME.grid },
          },
          y1: {
            // ── RIGHT axis — YoY % only ──
            type: "linear",
            position: "right",
            stacked: false,
            min: y1Min,                              // ← calibrated to data range
            max: y1Max,
            grid: { drawOnChartArea: false },
            ticks: {
              callback: (v) => `${fmtPct.format(v)}%`,
              color: SR_THEME.forest,
            },
          },
        },
      },
    });
    if (!chart) toggleEmpty(canvasId, true, "Chart unavailable.");

    // ── Phase 1: wire PNG download button ──
    const dlBtn = document.getElementById("btnMonthlyComparePng");
    const chartContainer = dlBtn?.parentElement;
    if (dlBtn && chartContainer) {
      dlBtn.onclick = () => {
        const img = chart?.toBase64Image?.();
        if (!img) return;
        const a = document.createElement("a");
        a.href = img;
        a.download = "wholesale_trend_compare.png";
        a.click();
      };
      chartContainer.addEventListener("mouseenter", () => { dlBtn.style.display = "block"; });
      chartContainer.addEventListener("mouseleave", () => { dlBtn.style.display = "none"; });
    }
  };

  const renderOwnershipDelta = (rows = []) => {
    const canvasId = "ownershipDeltaChart";
    const ranked = (Array.isArray(rows) ? rows : []).slice(0, 10);
    const hasData = ranked.length > 0;

    toggleEmpty(canvasId, !hasData);
    if (!ChartLib) return;
    if (!hasData) {
      destroyChart("ownershipDelta");
      return;
    }

    const chart = createChart("ownershipDelta", canvasId, {
      type: "bar",
      data: {
        labels: ranked.map((r) => repDisplayName(r)),
        datasets: [
          {
            label: "Historical Revenue",
            data: ranked.map((r) => num(r.historical_revenue)),
            backgroundColor: alphaColor(SR_THEME.bronze, 0.72),
          },
          {
            label: "Current Owner Revenue",
            data: ranked.map((r) => num(r.current_owner_revenue)),
            backgroundColor: SR_THEME.oxblood,
          },
        ],
      },
      options: {
        indexAxis: "y",
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          tooltip: {
            callbacks: {
              afterBody: (items) => {
                const idx = items?.[0]?.dataIndex;
                const row = idx == null ? null : ranked[idx];
                return row ? `Delta: ${money(row.ownership_delta_revenue)}` : "";
              },
            },
          },
        },
      },
    });
    if (!chart) toggleEmpty(canvasId, true, "Chart unavailable.");
  };

  const renderTransfers = (rows = []) => {
    const canvasId = "transferChart";
    const ranked = (Array.isArray(rows) ? rows : []).slice(0, 10);
    const hasData = ranked.length > 0;

    toggleEmpty(canvasId, !hasData);
    if (!ChartLib) return;
    if (!hasData) {
      destroyChart("transfer");
      return;
    }

    const chart = createChart("transfer", canvasId, {
      type: "bar",
      data: {
        labels: ranked.map((r) => repDisplayName(r)),
        datasets: [
          {
            label: "Transferred In",
            data: ranked.map((r) => num(r.transferred_in_revenue)),
            backgroundColor: SR_THEME.forest,
          },
          {
            label: "Transferred Out",
            data: ranked.map((r) => num(r.transferred_out_revenue) * -1),
            backgroundColor: SR_THEME.blood,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        onClick: (_evt, activeEls) => {
          const idx = activeEls?.[0]?.index;
          if (idx == null) return;
          const row = ranked[idx];
          if (!row) return;
          openUniversal(
            repWorkspacePayload(row, "Trend Intelligence", "Transferred Revenue", "Transferred Revenue", num(row.transferred_in_revenue) - num(row.transferred_out_revenue), {
              filter_mode: "current_window",
              transfer_activity_only: true,
              detail: "Transferred account activity for the selected current owner.",
            }),
            document.getElementById(canvasId)
          );
        },
        scales: {
          y: { ticks: { callback: (v) => fmtMoney0.format(v) } },
        },
        plugins: {
          tooltip: {
            callbacks: {
              afterBody: (items) => {
                const idx = items?.[0]?.dataIndex;
                const row = idx == null ? null : ranked[idx];
                if (!row) return [];
                return [
                  `Direct revenue: ${money(row.direct_revenue)}`,
                  `Inherited customers: ${fmtInt.format(num(row.inherited_customers))}`,
                  `Gained / Lost: ${fmtInt.format(num(row.gained_customers))} / ${fmtInt.format(num(row.lost_customers))}`,
                ];
              },
            },
          },
        },
      },
    });
    if (!chart) toggleEmpty(canvasId, true, "Chart unavailable.");
  };

  const renderOwnershipHighlights = (payload = {}) => {
    const holder = document.getElementById("srOwnershipHighlights");
    if (!holder) return;
    const k = payload.kpis || {};
    const meta = payload.meta || {};
    const bridge = meta.ownership_bridge || {};
    const ownership = payload.analysis?.ownership_breakdown || {};
    const items = [
      {
        label: "Direct vs inherited revenue",
        value: `${money(ownership.direct_revenue)} / ${money(ownership.inherited_revenue)}`,
        payload: attributedWorkspacePayload("Insight Strip", "Ownership Highlights", "Revenue Split", ownership.direct_revenue, { filter_mode: "current_window" }),
      },
      {
        label: "Direct vs inherited customers",
        value: `${fmtInt.format(num(ownership.direct_customers))} / ${fmtInt.format(num(ownership.inherited_customers))}`,
        payload: attributedWorkspacePayload("Insight Strip", "Ownership Highlights", "Customer Split", ownership.direct_customers, { filter_mode: "current_window" }),
      },
      {
        label: "Transferred-in customers",
        value: fmtInt.format(num(ownership.transferred_in_customers)),
        payload: attributedWorkspacePayload("Insight Strip", "Ownership Highlights", "Transferred Customers", ownership.transferred_in_customers, {
          filter_mode: "current_window",
          inherited_only: true,
        }),
      },
      {
        label: "Ownership mapping coverage",
        value: bridge.available ? `${fmtInt.format(num(bridge.rows))} bridge assignments` : "Fact fallback visible",
        payload: null,
      },
    ];
    holder.innerHTML = items
      .map((item) => `
        <li class="risk-item"${drillAttr(item.payload)}>
          <span>${escapeHtml(item.label)}</span>
          <span class="sr-badge-neutral">${escapeHtml(item.value)}</span>
        </li>
      `)
      .join("");
  };

  const renderSimpleList = (id, rows = [], renderItem) => {
    const holder = document.getElementById(id);
    if (!holder) return;
    if (!Array.isArray(rows) || !rows.length) {
      holder.innerHTML = '<li class="sr-empty-list">No visible activity in the selected window.</li>';
      return;
    }
    holder.innerHTML = rows.map(renderItem).join("");
  };

  // ── 3A+3B: Territory stacked area chart & summary chips ──
  const renderTerritoryChart = (payload = {}) => {
    const analysis = payload.analysis || {};
    const territories = Array.isArray(analysis.territories) ? analysis.territories : [];
    const territoryTrend = analysis.territory_trend || {};
    const spotlight = document.getElementById("srTerritorySpotlight");
    const monthSummary = document.getElementById("srTerritoryMonthSummary");
    const chips = document.getElementById("srTerritorySummaryChips");
    const series = Array.isArray(territoryTrend.series) ? territoryTrend.series.slice(0, 5) : [];
    const targetFallbacks = series.filter((row) => !row.has_prior_year).length;

    if (chips && territories.length) {
      const topT = territories[0] || {};
      const mostReps = [...territories].sort((a, b) => num(b.rep_count ?? b.reps) - num(a.rep_count ?? a.reps))[0] || {};
      chips.innerHTML = [
        `<span class="sr-badge-neutral">Territories: ${territories.length}</span>`,
        topT.territory_name ? `<span class="sr-badge-neutral">Top: ${escapeHtml(topT.territory_name)}</span>` : "",
        topT.revenue ? `<span class="sr-badge-neutral">Largest: ${money(topT.revenue)}</span>` : "",
        mostReps.territory_name ? `<span class="sr-badge-neutral">Most reps: ${escapeHtml(mostReps.territory_name)} (${fmtInt.format(num(mostReps.rep_count ?? mostReps.reps))})</span>` : "",
        targetFallbacks ? `<span class="sr-badge-neutral">${fmtInt.format(targetFallbacks)} target fallback${targetFallbacks !== 1 ? "s" : ""}</span>` : "",
      ].filter(Boolean).join("");
    } else if (chips) {
      chips.innerHTML = "";
    }

    const list = document.getElementById("srTerritoryList");
    const labels = Array.isArray(territoryTrend.labels) ? territoryTrend.labels : [];
    const hasTrendData = labels.length > 0 && series.some((row) => (row.revenue || []).some((value) => num(value) > 0));
    const territoryMeta = new Map(
      territories.map((row) => [String(row.territory_name || "").trim(), row]),
    );
    const lastActiveIndex = (values = []) => {
      for (let idx = values.length - 1; idx >= 0; idx -= 1) {
        if (num(values[idx]) > 0) return idx;
      }
      return values.length ? values.length - 1 : -1;
    };
    const signalForTerritory = (latest, previous, prior, hasPriorYear) => {
      if (!hasPriorYear || prior === null || prior <= 0) {
        return {
          label: "Target",
          className: "is-fallback",
          note: growthPct !== null
            ? `Target line using ${growthPct >= 0 ? "+" : ""}${fmtPct.format(growthPct)}% team growth`
            : "Target line shown because prior-year actuals are unavailable",
        };
      }
      const yoyPct = ((latest - prior) / Math.abs(prior)) * 100;
      if (yoyPct >= 6) {
        return {
          label: "Ahead",
          className: "is-strong",
          note: `YoY ${yoyPct >= 0 ? "+" : ""}${fmtPct.format(yoyPct)}% versus prior year`,
        };
      }
      if (yoyPct <= -6) {
        return {
          label: "Soft",
          className: "is-soft",
          note: `YoY ${yoyPct >= 0 ? "+" : ""}${fmtPct.format(yoyPct)}% versus prior year`,
        };
      }
      const momPct = previous > 0 ? ((latest - previous) / Math.abs(previous)) * 100 : null;
      return {
        label: "Steady",
        className: "is-steady",
        note: momPct === null
          ? `YoY ${yoyPct >= 0 ? "+" : ""}${fmtPct.format(yoyPct)}% versus prior year`
          : `MoM ${momPct >= 0 ? "+" : ""}${fmtPct.format(momPct)}% with YoY ${yoyPct >= 0 ? "+" : ""}${fmtPct.format(yoyPct)}%`,
      };
    };
    const growthPct = opt(payload.kpis?.revenue_yoy_pct);

    if (monthSummary) {
      if (!hasTrendData) {
        monthSummary.innerHTML = "";
      } else {
        let latestStackIndex = labels.length - 1;
        for (let idx = labels.length - 1; idx >= 0; idx -= 1) {
          const stackedTotal = series.reduce((sum, row) => sum + num(row.revenue?.[idx]), 0);
          if (stackedTotal > 0) {
            latestStackIndex = idx;
            break;
          }
        }
        const priorStackIndex = latestStackIndex > 0 ? latestStackIndex - 1 : null;
        const latestStackTotal = series.reduce((sum, row) => sum + num(row.revenue?.[latestStackIndex]), 0);
        const priorStackTotal = priorStackIndex === null
          ? null
          : series.reduce((sum, row) => sum + num(row.revenue?.[priorStackIndex]), 0);
        const stackDeltaPct = priorStackTotal && priorStackTotal > 0
          ? ((latestStackTotal - priorStackTotal) / Math.abs(priorStackTotal)) * 100
          : null;
        const strongestTerritory = [...series].sort((a, b) => num(b.total_revenue) - num(a.total_revenue))[0] || {};
        const mostReps = [...territories].sort((a, b) => num(b.rep_count ?? b.reps) - num(a.rep_count ?? a.reps))[0] || {};
        monthSummary.innerHTML = `
          <div class="sr-territory-month-card">
            <span class="sr-territory-month-label">Latest Fiscal Month</span>
            <span class="sr-territory-month-value">${escapeHtml(bucketLabelFromKey(labels[latestStackIndex], "monthly"))}</span>
            <span class="sr-territory-month-note">${fmtInt.format(series.length)} territories contributing to the visible stack</span>
          </div>
          <div class="sr-territory-month-card">
            <span class="sr-territory-month-label">Stack Total</span>
            <span class="sr-territory-month-value">${money(latestStackTotal)}</span>
            <span class="sr-territory-month-note ${stackDeltaPct !== null ? (stackDeltaPct >= 0 ? "is-positive" : "is-negative") : ""}">${stackDeltaPct === null ? "No prior fiscal month in view" : `${stackDeltaPct >= 0 ? "+" : ""}${fmtPct.format(stackDeltaPct)}% versus prior fiscal month`}</span>
          </div>
          <div class="sr-territory-month-card">
            <span class="sr-territory-month-label">Territory Callout</span>
            <span class="sr-territory-month-value">${escapeHtml(strongestTerritory.territory_name || mostReps.territory_name || NA)}</span>
            <span class="sr-territory-month-note">${targetFallbacks ? `${fmtInt.format(targetFallbacks)} territory target fallback${targetFallbacks !== 1 ? "s" : ""}` : `Most reps: ${escapeHtml(mostReps.territory_name || NA)} (${fmtInt.format(num(mostReps.rep_count ?? mostReps.reps))})`}</span>
          </div>
        `;
      }
    }

    if (spotlight) {
      if (!hasTrendData) {
        spotlight.innerHTML = '<div class="sr-territory-empty">No fiscal territory trend is visible for the selected filters.</div>';
      } else {
        const scopedTotalRevenue = Math.max(series.reduce((sum, row) => sum + num(row.total_revenue), 0), 1);
        spotlight.innerHTML = series.map((row, idx) => {
          const territoryName = String(row.territory_name || "").trim();
          const color = stableColor(idx);
          const meta = territoryMeta.get(territoryName) || {};
          const activeIndex = Math.max(lastActiveIndex(row.revenue || []), 0);
          const latestRevenue = num(row.revenue?.[activeIndex]);
          const previousRevenue = activeIndex > 0 ? num(row.revenue?.[activeIndex - 1]) : 0;
          const priorRevenue = opt(row.revenue_yoy?.[activeIndex]);
          const signal = signalForTerritory(latestRevenue, previousRevenue, priorRevenue, !!row.has_prior_year);
          const shareRatio = opt(meta.revenue_share_pct);
          const sharePct = shareRatio !== null
            ? (shareRatio <= 1.01 ? shareRatio * 100 : shareRatio)
            : (num(row.total_revenue) / scopedTotalRevenue) * 100;
          const territoryLabel = bucketLabelFromKey(labels[activeIndex], "monthly");
          const customerCount = num(meta.customer_count ?? row.customer_count);
          const repCount = num(meta.rep_count ?? row.rep_count);
          const inheritedRevenue = num(meta.inherited_revenue);
          const drill = territoryPayload(territoryName, "Ownership & Portfolio", "Top Territories", "Revenue", meta.revenue ?? row.total_revenue, {
            filter_mode: "current_window",
            detail: "Territory performance spotlight from the stacked fiscal trend.",
          });
          return `
            <div class="sr-territory-card ${idx === 0 ? "is-active" : ""}" style="--territory-swatch:${color}"${drillAttr(drill)}>
              <div class="sr-territory-card-head">
                <div class="sr-territory-card-title">
                  <span class="sr-territory-swatch" aria-hidden="true"></span>
                  <div>
                    <div class="sr-list-main">${escapeHtml(territoryName || NA)}</div>
                    <div class="sr-list-sub">${fmtInt.format(repCount)} reps · ${fmtInt.format(customerCount)} customers · ${pct(sharePct)}</div>
                  </div>
                </div>
                <div class="sr-territory-card-metric">${money(meta.revenue ?? row.total_revenue)}</div>
              </div>
              <div class="sr-territory-share"><span style="width:${Math.max(8, Math.min(100, sharePct)).toFixed(1)}%"></span></div>
              <div class="sr-territory-card-foot">
                <span class="sr-territory-signal ${signal.className}">${escapeHtml(signal.label)}</span>
                <div class="sr-list-sub">Latest ${escapeHtml(territoryLabel)}: ${money(latestRevenue)} · Inherited ${money(inheritedRevenue)}</div>
                <div class="sr-list-sub">${escapeHtml(signal.note)}</div>
              </div>
            </div>
          `;
        }).join("");
      }
    }

    destroyChart("territory");
    setChartShellLoading("srTerritoryChart", false);
    if (!ChartLib || !hasTrendData) {
      if (list) list.classList.remove("d-none");
      return;
    }

    const resolved = resolveChartCanvas("srTerritoryChart");
    if (!resolved) {
      if (list) list.classList.remove("d-none");
      return;
    }

    const datasets = [];
    series.forEach((row, idx) => {
      const color = stableColor(idx);
      const territoryMeta2 = territoryMeta.get(String(row.territory_name || "").trim()) || {};
      const seriesRepCount = num(territoryMeta2.rep_count ?? row.rep_count);
      // Rep count badge in chart label: e.g. "West · 5 Reps"
      const chartLabel = row.territory_name
        ? (seriesRepCount > 0 ? `${row.territory_name} · ${seriesRepCount} Rep${seriesRepCount !== 1 ? "s" : ""}` : row.territory_name)
        : `Territory ${idx + 1}`;
      datasets.push({
        type: "line",
        label: chartLabel,
        data: labels.map((bucket, pointIdx) => ({
          x: bucketLabelFromKey(bucket, "monthly"),
          y: num(row.revenue?.[pointIdx]),
          rawBucket: bucket,
          territory_name: row.territory_name,
          revenue_yoy: row.revenue_yoy?.[pointIdx],
          has_prior_year: !!row.has_prior_year,
          customer_count: row.customer_count,
          rep_count: row.rep_count,
        })),
        borderColor: color,
        backgroundColor: (context) => {
          const chart = context.chart;
          const area = chart?.chartArea;
          if (!area) return alphaColor(color, 0.18);
          const gradient = chart.ctx.createLinearGradient(0, area.top, 0, area.bottom);
          gradient.addColorStop(0, alphaColor(color, idx === 0 ? 0.72 : 0.52));
          gradient.addColorStop(0.48, alphaColor(color, 0.36));
          gradient.addColorStop(1, alphaColor(color, 0.08));
          return gradient;
        },
        borderWidth: 2.35,
        fill: idx === 0 ? "origin" : "-1",
        stack: "territory-revenue",
        tension: 0.3,
        cubicInterpolationMode: "monotone",
        pointRadius: 0,
        pointHoverRadius: 4,
        pointHitRadius: 18,
        pointBackgroundColor: color,
        pointBorderColor: SR_THEME.cream,
        pointBorderWidth: 1.2,
        spanGaps: true,
      });

      if (!row.has_prior_year && growthPct !== null) {
        datasets.push({
          type: "line",
          label: `${row.territory_name} Target`,
          data: labels.map((bucket, pointIdx) => ({
            x: bucketLabelFromKey(bucket, "monthly"),
            y: num(row.revenue?.[pointIdx]) * (1 + (growthPct / 100)),
            rawBucket: bucket,
            territory_name: row.territory_name,
            target_growth_pct: growthPct,
          })),
          borderColor: color,
          backgroundColor: "transparent",
          borderDash: [7, 4],
          borderWidth: 1.8,
          fill: false,
          tension: 0.2,
          pointRadius: 0,
          pointHoverRadius: 0,
        });
      }
    });

    charts["territory"] = new ChartLib(resolved.ctx, {
      type: "line",
      data: {
        labels: labels.map((bucket) => bucketLabelFromKey(bucket, "monthly")),
        datasets,
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: {
          mode: "index",
          intersect: false,
        },
        plugins: {
          legend: {
            display: false,
          },
          tooltip: {
            callbacks: {
              title: (items) => items?.[0]?.label || "",
              label: (ctx) => {
                const point = ctx.raw || {};
                if (String(ctx.dataset.label || "").endsWith(" Target")) {
                  const growthLabel = point.target_growth_pct == null ? NA : `${point.target_growth_pct >= 0 ? "+" : ""}${fmtPct.format(point.target_growth_pct)}%`;
                  return `${ctx.dataset.label}: ${money(ctx.parsed.y)} at team growth (${growthLabel})`;
                }
                return `${ctx.dataset.label}: ${money(ctx.parsed.y)}`;
              },
              afterLabel: (ctx) => {
                const point = ctx.raw || {};
                if (String(ctx.dataset.label || "").endsWith(" Target")) return [];
                const lines = [];
                if (opt(point.revenue_yoy) !== null && opt(point.revenue_yoy) > 0) {
                  const prior = num(point.revenue_yoy);
                  const deltaPct = ((num(ctx.parsed.y) - prior) / Math.abs(prior)) * 100;
                  lines.push(`Prior year: ${money(prior)}`);
                  lines.push(`YoY: ${deltaPct >= 0 ? "+" : ""}${fmtPct.format(deltaPct)}%`);
                } else {
                  lines.push("Prior year: target fallback");
                }
                if (point.rep_count != null || point.customer_count != null) {
                  lines.push(`${fmtInt.format(num(point.rep_count))} rep${num(point.rep_count) !== 1 ? "s" : ""} · ${fmtInt.format(num(point.customer_count))} customers`);
                }
                return lines;
              },
              footer: (items) => {
                const stackTotal = (items || [])
                  .filter((item) => !String(item.dataset?.label || "").endsWith(" Target"))
                  .reduce((sum, item) => sum + num(item.parsed?.y), 0);
                return [`Stack total: ${money(stackTotal)}`];
              },
            },
          },
        },
        scales: {
          x: {
            stacked: true,
            grid: {
              display: false,
            },
            ticks: {
              maxTicksLimit: 8,
              maxRotation: 0,
              color: SR_THEME.tick,
            },
          },
          y: {
            stacked: true,
            beginAtZero: true,
            grid: {
              color: SR_THEME.grid,
              drawBorder: false,
            },
            ticks: {
              callback: (value) => fmtMoney0.format(value),
              maxTicksLimit: 5,
              color: SR_THEME.tick,
            },
          },
        },
      },
    });
    if (list) list.classList.add("d-none");
  };

  const renderPortfolioSection = (payload = {}) => {
    const analysis = payload.analysis || {};
    renderTerritoryChart(payload);
    // Keep fallback list for no-chart scenarios
    renderSimpleList("srTerritoryList", analysis.territories || [], (row) => `
      <li${drillAttr(territoryPayload(row.territory_name, "Ownership & Portfolio", "Top Territories", "Revenue", row.revenue, {
        filter_mode: "current_window",
        detail: "Territory-specific attributed detail from the current owner portfolio.",
      }))}>
        <div>
          <div class="sr-list-main">${escapeHtml(row.territory_name || NA)}</div>
          <div class="sr-list-sub">${fmtInt.format(num(row.customer_count))} customers · ${pct(row.revenue_share_pct, true)} share · ${money(row.inherited_revenue)} inherited</div>
        </div>
        <div class="sr-list-metric">${money(row.revenue)}</div>
      </li>
    `);
    renderSimpleList("srReplacementPairs", analysis.replacement_pairs || [], (row) => `
      <li${drillAttr(attributedWorkspacePayload("Ownership & Portfolio", "Replacement / Transfer Audit", "Inherited Revenue", row.inherited_revenue, {
        filter_mode: "current_window",
        inherited_only: true,
        current_owner_id: row.current_owner_key,
        current_owner_name: row.current_owner_name,
        prior_rep_name: row.prior_rep_name,
        detail: "Attributed replacement-pair detail including inherited customers and territories.",
      }))}>
        <div>
          <div class="sr-list-main">${escapeHtml(businessRepName(row.current_owner_name, row.current_owner_key, UNASSIGNED_REP_FALLBACK))}</div>
          <div class="sr-list-sub">Inherited from ${escapeHtml(businessRepName(row.prior_rep_name, row.prior_rep_key, READABLE_REP_FALLBACK))}${row.territories ? ` · ${escapeHtml(row.territories)}` : ""}${row.time_window ? ` · ${escapeHtml(row.time_window)}` : ""}</div>
        </div>
        <div class="sr-list-metric">${money(row.inherited_revenue)} · ${fmtInt.format(num(row.customer_count))} cust.</div>
      </li>
    `);

    setDrillPayload(
      document.getElementById("srInheritedCustomers")?.closest(".sr-highlight-card"),
      attributedWorkspacePayload("Ownership & Portfolio", "Inherited Customers", "Inherited Customers", payload.kpis?.inherited_customers, {
        filter_mode: "current_window",
        inherited_only: true,
      })
    );
    setDrillPayload(
      document.getElementById("srPortfolioTerritoryCount")?.closest(".sr-highlight-card"),
      territoryPayload(analysis.territories?.[0]?.territory_name, "Ownership & Portfolio", "Top Territories", "Revenue", analysis.territories?.[0]?.revenue, {
        filter_mode: "current_window",
      })
    );
    setDrillPayload(
      document.getElementById("srPortfolioReplacedCount")?.closest(".sr-highlight-card"),
      attributedWorkspacePayload("Ownership & Portfolio", "Replaced Reps", "Replaced Reps", payload.kpis?.replaced_rep_count, {
        filter_mode: "current_window",
        inherited_only: true,
        detail: "Visible inherited-book activity grouped by prior rep and current owner.",
      })
    );
    setDrillPayload(
      document.getElementById("srUnassignedCustomers")?.closest(".sr-highlight-card"),
      attributedWorkspacePayload("Ownership & Portfolio", "Unassigned Customers", "Unassigned Customers", payload.kpis?.unassigned_customers, {
        filter_mode: "current_window",
        dq_bucket: "unassigned",
      })
    );
  };

  const renderDataQuality = (rows = []) => {
    const tbody = document.getElementById("srDataQualityBody");
    if (!tbody) return;
    const bucketLabel = (bucket) => {
      const normalized = String(bucket || "").trim().toLowerCase();
      const labels = {
        fact_fallback: "Fact owner fallback",
        fact_owner_only: "Fact owner fallback",
        inactive_current_owner: "Inactive current owner",
        needs_review: "Needs review",
        unassigned: "Unassigned / needs review",
      };
      if (labels[normalized]) return labels[normalized];
      return String(bucket || NA)
        .replace(/_/g, " ")
        .replace(/\b\w/g, (ch) => ch.toUpperCase());
    };
    if (!Array.isArray(rows) || !rows.length) {
      tbody.innerHTML = '<tr><td colspan="3" class="text-muted">No mapping exceptions detected in the visible scope.</td></tr>';
      return;
    }
    tbody.innerHTML = rows
      .map((row) => `
        <tr${drillAttr(attributedWorkspacePayload("Ownership & Portfolio", "Coverage & Exceptions", "Customers", row.customer_count, {
          filter_mode: "current_window",
          dq_bucket: row.bucket,
          detail: "Attributed ownership exception detail for the selected data-quality bucket.",
        }))}>
          <td>${escapeHtml(bucketLabel(row.bucket))}</td>
          <td class="text-end">${fmtInt.format(num(row.customer_count))}</td>
          <td class="text-end">${money(row.revenue)}</td>
        </tr>
      `)
      .join("");
  };

  const customerSilentDays = (row) => silentAge(row?.last_order_date, row?.days_since_order).days;
  const customerMoMValue = (row) => opt(row?.mom_revenue_pct ?? row?.vs_prior_pct);
  const customerYoYValue = (row) => opt(row?.yoy_revenue_pct ?? row?.yoy_pct);
  const isCriticalCustomer = (row) => {
    const silentDays = customerSilentDays(row);
    const momPct = customerMoMValue(row);
    return silentDays != null && silentDays > 30 && momPct != null && momPct < -20;
  };

  // ── Phase 3A: customer risk signal ──
  const computeCustomerRisk = (row) => {
    if (isCriticalCustomer(row)) return { signal: "critical", label: "Critical", score: 4 };
    if ((row.revenue_last_30 ?? row.revenue ?? 0) === 0 && (row.revenue_prev_30 ?? 0) > 0) {
      return { signal: "lost", label: "Lost", score: 3 };
    }
    let neg = 0;
    if ((customerMoMValue(row) ?? 0) < -5) neg += 1;
    if ((customerYoYValue(row) ?? 0) < -10) neg += 1;
    if ((customerSilentDays(row) ?? 0) > 45) neg += 1;
    if (neg === 0) return { signal: "healthy", label: "Healthy", score: 0 };
    if (neg === 1) return { signal: "watch", label: "Watch", score: 1 };
    return { signal: "atrisk", label: "At Risk", score: 2 };
  };

  const _riskPillHtml = (risk) => {
    const cls = {
      healthy: "sr-risk-healthy",
      watch: "sr-risk-watch",
      atrisk: "sr-risk-atrisk",
      critical: "sr-risk-critical",
      lost: "sr-risk-lost",
    }[risk.signal] || "";
    return `<span class="sr-risk-pill ${cls}">${escapeHtml(risk.label)}</span>`;
  };

  // ── Phase 3B: customer search + owner pill state ──
  let _customerSearchQ  = "";
  let _activeOwnerFilter = "";

  // ── Customer view toggle state (1A) ──
  let _customerViewMode = "all";
  let _allCustomerRows = [];

  const _computeCustomerScores = (rows) => {
    // Composite score: revenue_rank * 0.40 + profit_rank * 0.35 + mom_rank * 0.25
    // Uses vs_prior_pct (prior-period MoM equivalent) as the momentum signal
    const n = rows.length;
    if (n === 0) return [];

    const hasProfitData = rows.some((r) => opt(r.profit) !== null && opt(r.profit) !== 0);
    const weights = hasProfitData ? { rev: 0.40, profit: 0.35, mom: 0.25 } : { rev: 0.60, profit: 0, mom: 0.40 };

    const ranked = [...rows].map((r, origIdx) => ({ ...r, _origIdx: origIdx }));

    const rankBy = (arr, fn) => {
      const sorted = [...arr].sort((a, b) => fn(a) - fn(b));
      const rankMap = new Map();
      sorted.forEach((r, i) => rankMap.set(r._origIdx, i + 1));
      return rankMap;
    };

    const revRanks   = rankBy(ranked, (r) => num(r.revenue));
    const profitRanks = hasProfitData ? rankBy(ranked, (r) => num(r.profit)) : null;
    // MoM revenue pct is the short-term velocity signal for follow-up priority.
    const momRanks   = rankBy(ranked, (r) => num(customerMoMValue(r) ?? customerYoYValue(r)));

    return rows.map((r, i) => {
      const score =
        revRanks.get(i) * weights.rev +
        (profitRanks ? profitRanks.get(i) * weights.profit : 0) +
        momRanks.get(i) * weights.mom;
      return { ...r, _score: score };
    });
  };

  const _custDaysSilentCell = (row) => {
    if (row.is_overdue) {
      const avg = num(row.avg_days_between_orders || row.avg_days);
      const since = num(row.days_since_order);
      return `
        <span class="sr-silent-cell" title="Predictive Churn: Missed ${fmtInt.format(avg)}d cycle by ${fmtInt.format(since - avg)}d">
          <span class="sr-silent-chip is-critical">Overdue</span>
          <div style="font-size:0.62rem;color:${SR_THEME.blood};margin-top:1px">${fmtInt.format(avg)}d cycle</div>
        </span>
      `;
    }
    return silentCellHtml(row.last_order_date, row.days_since_order);
  };

  const _custProfitCell = (row) => {
    if (row.profit == null) return NA;
    const profitStr = money(row.profit);
    const rev = num(row.revenue);
    const derivedMargin = (rev > 0 && row.profit != null)
      ? (num(row.profit) / rev) * 100
      : null;
    if (derivedMargin === null) return profitStr;
    const TARGET = 29.1, MIN = 20.1;
    const [bg, fg] = derivedMargin >= TARGET
      ? [alphaColor(SR_THEME.forest, 0.16), SR_THEME.forest]
      : derivedMargin >= MIN
        ? [alphaColor(SR_THEME.gold, 0.18), SR_THEME.bronze]
        : [alphaColor(SR_THEME.blood, 0.14), SR_THEME.blood];
    return `${profitStr}<br><span style="font-size:0.68rem;font-weight:600;padding:1px 6px;border-radius:8px;background:${bg};color:${fg};display:inline-block;margin-top:2px">${fmtPct.format(derivedMargin)}%</span>`;
  };

  const _customerFilterRows = (rows) => {
    let filtered = Array.isArray(rows) ? [...rows] : [];
    if (_activeOwnerFilter) filtered = filtered.filter((r) => (r.account_owner_name || "") === _activeOwnerFilter);
    const q = (_customerSearchQ || "").trim().toLowerCase();
    if (q) {
      filtered = filtered.filter((r) =>
        (r.customer_name || "").toLowerCase().includes(q) ||
        (r.customer_id || "").toLowerCase().includes(q) ||
        (r.account_owner_name || "").toLowerCase().includes(q) ||
        (r.territory_name || "").toLowerCase().includes(q)
      );
    }
    return filtered;
  };

  const _customerGapThreshold = (rows) => {
    const values = (Array.isArray(rows) ? rows : [])
      .map((row) => num(row.revenue))
      .filter((value) => value > 0)
      .sort((a, b) => a - b);
    if (!values.length) return 0;
    return values[Math.floor((values.length - 1) / 2)] || 0;
  };

  const _customerSortValue = (row, key) => {
    if (key === "customer_name") return row.customer_name || row.customer_id;
    if (key === "_risk_score") return computeCustomerRisk(row).score;
    if (key === "last_order_date") return customerSilentDays(row);
    if (key === "mom_revenue_pct") return customerMoMValue(row);
    if (key === "yoy_revenue_pct") return customerYoYValue(row);
    return row[key];
  };

  const _customerGapIcon = (label, extraClass = "", title = "") =>
    `<span class="sr-gap-icon${extraClass ? ` ${extraClass}` : ""}" title="${escapeHtml(title || label)}">${escapeHtml(label)}</span>`;

  const _customerGapAnalysisHtml = (row) => {
    const freshRevenue = num(row.fresh_revenue);
    const consumablesRevenue = num(row.consumables_revenue);
    const gmRevenue = num(row.gm_revenue);
    const highValueThreshold = num(row._gapHighValueThreshold);
    const isHighValue = highValueThreshold > 0 && num(row.revenue) >= highValueThreshold;
    const icons = [];
    if (freshRevenue > 0) {
      icons.push(_customerGapIcon("FR", "is-anchor", `Fresh active: ${money(freshRevenue)}`));
    }
    if (consumablesRevenue > 0) {
      icons.push(_customerGapIcon("CN", "is-owned", `Consumables active: ${money(consumablesRevenue)}`));
    } else if (isHighValue && freshRevenue > 0) {
      icons.push(_customerGapIcon("CN", "is-gap", "Cross-sell gap: strong fresh buyer with no consumables"));
    }
    if (gmRevenue > 0) {
      icons.push(_customerGapIcon("GM", "is-owned", `Gen. merch active: ${money(gmRevenue)}`));
    }
    if (!icons.length) return `<span class="sr-gap-dash">${NA}</span>`;
    return icons.join("");
  };

  const _customerYoyDeltaHtml = (row) => {
    const yoyDelta = opt(row.yoy_delta_revenue);
    const yoyPct = customerYoYValue(row);
    if (yoyDelta == null && yoyPct == null) return `<span class="text-muted">${NA}</span>`;
    const deltaClass = yoyDelta == null ? "text-muted" : yoyDelta > 0 ? "delta-up" : yoyDelta < 0 ? "delta-down" : "text-muted";
    const pctLabel = yoyPct == null ? NA : `${yoyPct >= 0 ? "+" : ""}${fmtPct.format(yoyPct)}% YoY`;
    return `
      <div class="${deltaClass}">${yoyDelta == null ? NA : money(yoyDelta)}</div>
      <div class="sr-momentum-sub">${pctLabel}</div>
    `;
  };

  const _customerMomentumHtml = (row) => {
    const momPct = customerMoMValue(row);
    if (momPct == null) return `<span class="sr-momentum-pill is-flat">${NA}</span>`;
    const cls = momPct > 2 ? "is-up" : momPct < -2 ? "is-down" : "is-flat";
    const icon = momPct > 2 ? "▲" : momPct < -2 ? "▼" : "•";
    const label = momPct > 2 ? "Accelerating" : momPct < -2 ? "Slowing" : "Flat";
    return `
      <span class="sr-momentum-pill ${cls}">${icon} ${momPct > 0 ? "+" : ""}${fmtPct.format(momPct)}%</span>
      <span class="sr-momentum-sub">${label}</span>
    `;
  };

  const customerFocusKey = (row) =>
    cleanText(row?.customer_id || row?.key || row?.customer_name).toLowerCase();

  const findCustomerRow = (rows = [], needle = null) => {
    const targetKey = customerFocusKey(needle);
    if (!targetKey || !Array.isArray(rows)) return null;
    return rows.find((row) => customerFocusKey(row) === targetKey) || null;
  };

  const setFocusedCustomer = (row = null, { rerenderTable = true } = {}) => {
    focusedCustomer = row
      ? {
        customer_id: row.customer_id || row.key || null,
        customer_name: row.customer_name || row.customer_id || row.key || TEXT_EMPTY,
        account_owner_name: row.account_owner_name || TEXT_EMPTY,
        territory_name: row.territory_name || TEXT_EMPTY,
        revenue: row.revenue,
        profit: row.profit,
        orders: row.orders,
        fresh_revenue: row.fresh_revenue,
        consumables_revenue: row.consumables_revenue,
        gm_revenue: row.gm_revenue,
        last_order_date: row.last_order_date,
        days_since_order: row.days_since_order,
        mom_revenue_pct: row.mom_revenue_pct,
        yoy_revenue_pct: row.yoy_revenue_pct,
        delivery_lat: row.delivery_lat,
        delivery_lng: row.delivery_lng,
        delivery_city: row.delivery_city,
        delivery_province: row.delivery_province,
        shipping_method: row.shipping_method,
      }
      : null;
    renderGapFocusPanel(focusedCustomer);
    renderFilterBreadcrumb(lastPayload || {});
    if (rerenderTable && Array.isArray(_allCustomerRows) && _allCustomerRows.length) {
      _applyCustomerView(_allCustomerRows, _customerViewMode);
    }
  };

  const renderGapFocusPanel = (row = null) => {
    const panel = document.getElementById("srGapFocusPanel");
    const title = document.getElementById("srGapFocusTitle");
    const meta = document.getElementById("srGapFocusMeta");
    const grid = document.getElementById("srGapFocusGrid");
    if (!panel || !title || !meta || !grid) return;
    if (!row) {
      panel.classList.add("d-none");
      title.textContent = "Customer gap analysis";
      meta.textContent = "Select a customer from the priority queue to open gap analysis details.";
      grid.innerHTML = "";
      return;
    }

    const risk = computeCustomerRisk(row);
    const daysSilent = customerSilentDays(row);
    const momPct = customerMoMValue(row);
    const yoyPct = customerYoYValue(row);
    const freshRevenue = num(row.fresh_revenue);
    const consumablesRevenue = num(row.consumables_revenue);
    const gmRevenue = num(row.gm_revenue);
    const opportunities = [];
    if (freshRevenue > 0 && consumablesRevenue <= 0) opportunities.push("Open consumables cross-sell");
    if (freshRevenue > 0 && gmRevenue <= 0) opportunities.push("Add general merchandise mix");
    if (!opportunities.length && freshRevenue > 0) opportunities.push("Department mix is already attached");
    if (!opportunities.length) opportunities.push("No protein anchor in the visible window");

    title.textContent = row.customer_name || row.customer_id || TEXT_EMPTY;
    meta.textContent = [
      row.account_owner_name || TEXT_EMPTY,
      row.territory_name || TEXT_EMPTY,
      daysSilent == null ? "Silent window unavailable" : `${fmtInt.format(daysSilent)} days silent`,
    ].filter(Boolean).join(" | ");
    grid.innerHTML = [
      {
        label: "Revenue",
        value: money(row.revenue),
        note: row.orders == null ? "Current fiscal window" : `${fmtInt.format(num(row.orders))} orders in scope`,
      },
      {
        label: "Risk Signal",
        value: risk.label,
        note: row.last_order_date ? `Last invoice ${formatDateCA(row.last_order_date)}` : "Last invoice None",
      },
      {
        label: "Momentum",
        value: momPct == null ? TEXT_EMPTY : `${momPct >= 0 ? "+" : ""}${fmtPct.format(momPct)}%`,
        note: yoyPct == null ? "YoY None" : `YoY ${yoyPct >= 0 ? "+" : ""}${fmtPct.format(yoyPct)}%`,
      },
      {
        label: "Owner",
        value: row.account_owner_name || TEXT_EMPTY,
        note: row.territory_name ? `Territory ${row.territory_name}` : "Territory None",
      },
      {
        label: "Department Mix",
        value: `${money(freshRevenue)} BF | ${money(consumablesRevenue)} PT | ${money(gmRevenue)} PK`,
        note: row.shipping_method ? `Ship via ${row.shipping_method}` : "Ship method None",
      },
      {
        label: "Action",
        value: opportunities[0],
        note: opportunities.slice(1).join(" | ") || "Map focus stays pinned at zoom 14",
      },
    ].map((card) => `
      <div class="sr-gap-focus-card">
        <span class="sr-gap-focus-card-label">${escapeHtml(card.label)}</span>
        <span class="sr-gap-focus-card-value">${escapeHtml(card.value)}</span>
        <span class="sr-gap-focus-card-note">${escapeHtml(card.note)}</span>
      </div>
    `).join("");
    panel.classList.remove("d-none");
  };

  const dispatchPageFilters = (filters, source = "manual") => {
    let nextFilters = { ...(filters || {}) };
    if (window.FilterState && typeof window.FilterState.sanitize === "function") {
      nextFilters = window.FilterState.sanitize(nextFilters);
    }
    if (window.FilterState && typeof window.FilterState.set === "function") {
      window.FilterState.set(nextFilters, { persist: true });
      if (typeof window.FilterState.hydrateForm === "function") {
        window.FilterState.hydrateForm(document.getElementById("filtersForm"));
      }
    }
    let qs = "";
    if (window.FilterState && typeof window.FilterState.toQueryString === "function") {
      qs = window.FilterState.toQueryString(nextFilters);
    } else {
      const params = new URLSearchParams();
      Object.entries(nextFilters).forEach(([key, value]) => {
        if (Array.isArray(value)) {
          value.filter((item) => item != null && item !== "").forEach((item) => params.append(key, String(item)));
          return;
        }
        if (value == null || value === "" || value === false) return;
        params.set(key, String(value));
      });
      qs = params.toString();
    }
    const detail = {
      filters: nextFilters,
      qs,
      meta: { source },
    };
    if (typeof window.dispatchGlobalFiltersApply === "function") {
      window.dispatchGlobalFiltersApply(detail);
      return;
    }
    window.dispatchEvent(new CustomEvent("globalFilters:apply", { detail }));
  };

  const dispatchFiltersForCustomer = (row) => {
    const customerId = cleanText(row?.customer_id || row?.key || row?.customer_name);
    if (!customerId) return;
    dispatchPageFilters(
      {
        ...currentFilterState(),
        customers: [customerId],
      },
      "priority_queue",
    );
  };

  const clearCustomerFocus = () => {
    pendingCustomerFocus = null;
    setFocusedCustomer(null);
    const current = currentFilterState();
    dispatchPageFilters({ ...current, customers: [] }, "customer_focus_clear");
  };

  const focusCustomerFromPriority = (row) => {
    if (!row) return;
    pendingCustomerFocus = {
      customer_id: row.customer_id || row.key || null,
      customer_name: row.customer_name || row.customer_id || TEXT_EMPTY,
      account_owner_name: row.account_owner_name || TEXT_EMPTY,
      territory_name: row.territory_name || TEXT_EMPTY,
      delivery_lat: row.delivery_lat,
      delivery_lng: row.delivery_lng,
      delivery_city: row.delivery_city,
      delivery_province: row.delivery_province,
    };
    setFocusedCustomer(row);
    if (typeof focusMapOnCustomer === "function") focusMapOnCustomer(row, { zoom: 14, duration: 900 });
    window.closeFollowUpDrawer();
    document.getElementById("srLiveMap")?.scrollIntoView({ behavior: "smooth", block: "center" });
    dispatchFiltersForCustomer(row);
  };

  const _visibleCustomerRows = (rows) => {
    const filtered = _customerFilterRows(rows);
    if (!filtered.length) return [];
    const maxRevenue = Math.max(...filtered.map((r) => num(r.revenue)), 1);
    const highValueThreshold = _customerGapThreshold(filtered);
    const enrich = (row, badge = null) => ({
      ...row,
      _maxRevenue: maxRevenue,
      _gapHighValueThreshold: highValueThreshold,
      _customerBadge: badge,
    });

    if (_customerViewMode === "best") {
      return _computeCustomerScores(filtered)
        .sort((a, b) => b._score - a._score)
        .slice(0, 10)
        .map((row) => enrich(row, { cls: "sr-cust-badge-best", text: "Top Performer", view: "best" }));
    }

    if (_customerViewMode === "atrisk") {
      return _computeCustomerScores(filtered)
        .sort((a, b) => a._score - b._score)
        .slice(0, 10)
        .map((row) => enrich(row, { cls: "sr-cust-badge-risk", text: "Priority Follow-Up", view: "atrisk" }));
    }

    return sortRows(filtered, state.topCustomersSortBy, state.topCustomersSortDir, _customerSortValue)
      .map((row) => enrich(row));
  };

  const _buildCustomerRowHtml = (row, badge = null) => {
    const badgeHtml = badge?.text ? `<span class="sr-cust-badge ${badge.cls || ""}">${escapeHtml(badge.text)}</span>` : "";
    const focusClass = focusedCustomer && customerFocusKey(row) === customerFocusKey(focusedCustomer) ? " is-focused-customer" : "";
    const rowClass = `${badge?.view === "best" ? "sr-cust-best-row" : badge?.view === "atrisk" ? "sr-cust-risk-row" : ""}${focusClass}`;
    const risk = computeCustomerRisk(row);
    const riskHtml = _riskPillHtml(risk);
    const rev = num(row.revenue);
    const maxRev = row._maxRevenue || rev || 1;
    const barPct = Math.min(100, Math.round((rev / maxRev) * 100));
    const revBarHtml = `<div style="height:3px;width:${barPct}%;background:${alphaColor(SR_THEME.oxblood, 0.4)};border-radius:2px;margin-top:2px"></div>`;
    const ordersVal = row.orders != null ? num(row.orders) : null;
    const ordersHtml = ordersVal != null && ordersVal > 0
      ? `<div style="font-size:0.68rem;color:${SR_THEME.tick};margin-top:1px">${fmtInt.format(ordersVal)} order${ordersVal !== 1 ? "s" : ""}</div>`
      : "";
    const silentCell = _custDaysSilentCell(row);

    return `
      <tr class="sr-virtual-row ${rowClass.trim()}" data-customer-id="${escapeHtml(row.customer_id || row.key || row.customer_name || "")}"${drillAttr(customerPayload(row, "Customer Intelligence", "Top Customers", "Revenue", row.revenue))}>
        <td>
          ${badgeHtml}
          <span class="sr-link"${drillAttr(customerPayload(row, "Customer Intelligence", "Top Customers", "Revenue", row.revenue))}>${escapeHtml(row.customer_name || row.customer_id || NA)}</span>
        </td>
        <td>${riskHtml}</td>
        <td><span class="sr-link"${drillAttr(salesrepPayload({ rep_id: row.account_owner_id || row.account_owner_name, rep_name: row.account_owner_name }, "Customer Intelligence", "Top Customers", "Revenue", row.revenue))}>${escapeHtml(businessRepName(row.account_owner_name, row.account_owner_id, READABLE_REP_FALLBACK))}</span></td>
        <td><span class="sr-link"${drillAttr(territoryPayload(row.territory_name, "Customer Intelligence", "Top Customers", "Revenue", row.revenue, { filter_mode: "current_window" }))}>${escapeHtml(row.territory_name || NA)}</span></td>
        <td class="text-end">${money(rev)}${revBarHtml}${ordersHtml}</td>
        <td class="text-end">${_custProfitCell(row)}</td>
        <td class="text-end">${_customerYoyDeltaHtml(row)}</td>
        <td class="text-end">${_customerMomentumHtml(row)}</td>
        <td class="text-center sr-gap-cell">${_customerGapAnalysisHtml(row)}</td>
        <td class="text-end">${silentCell}</td>
      </tr>
    `;
  };

  const _applyCustomerView = (rows, viewMode) => {
    const tbody = document.getElementById("srTopCustomersBody");
    if (!tbody) return;
    _customerViewMode = viewMode || _customerViewMode;
    customerVirtualTable.tbody = tbody;
    customerVirtualTable.wrapper = document.getElementById("srTopCustomersWrap");
    customerVirtualTable.rows = _visibleCustomerRows(rows);
    customerVirtualTable.lastRange = "";
    const q = (_customerSearchQ || "").trim();
    customerVirtualTable.emptyMessage = !Array.isArray(rows) || !rows.length
      ? "No customer activity for the selected filters."
      : q
        ? `No customers match “${escapeHtml(q)}”. Try a shorter search term.`
        : _activeOwnerFilter
          ? `No customers match ${escapeHtml(_activeOwnerFilter)}.`
          : "No customers match the current filter.";
    if (customerVirtualTable.wrapper) customerVirtualTable.wrapper.scrollTop = 0;
    renderVirtualCustomerRows({ force: true });
  };

  const renderTopCustomers = (rows = [], lostAccounts = []) => {
    _allCustomerRows = Array.isArray(rows) ? rows : [];
    buildOwnerPills(_allCustomerRows);
    _applyCustomerView(_allCustomerRows, _customerViewMode);

    // ── Customer summary stat bar ──
    const summaryEl = document.getElementById("srCustSummaryLine");
    if (summaryEl) {
      const totalActive = _allCustomerRows.filter((r) => num(r.revenue ?? r.revenue_last_30) > 0).length;
      const gained      = _allCustomerRows.filter((r) => (r.revenue_prev_30 ?? 0) === 0 && num(r.revenue_last_30 ?? r.revenue) > 0).length;
      const lostN       = lostAccounts.length;
      const atRisk      = _allCustomerRows.filter((r) => computeCustomerRisk(r).score >= 2).length;
      const totalRev    = _allCustomerRows.reduce((s, r) => s + num(r.revenue ?? r.revenue_last_30), 0);
      summaryEl.innerHTML = `
        <span style="display:inline-flex;flex-wrap:wrap;gap:12px;align-items:center;font-size:0.78rem">
          <span><span style="color:${SR_THEME.oxblood};font-weight:700">${fmtInt.format(totalActive)}</span> active</span>
          <span style="color:${SR_THEME.tanSoft}">|</span>
          <span style="font-weight:600">${fmtMoney0.format(totalRev)}</span> total rev
          <span style="color:${SR_THEME.tanSoft}">|</span>
          <span style="color:${SR_THEME.forest};font-weight:700">+${gained}</span> gained
          <span style="color:${SR_THEME.tanSoft}">|</span>
          <span style="color:${SR_THEME.blood};font-weight:700">${atRisk}</span> at-risk
          <span style="color:${SR_THEME.tanSoft}">|</span>
          <span style="color:${SR_THEME.blood};font-weight:700">${lostN}</span> lost
        </span>`;
    }
  };

  // Wire up toggle buttons once DOM is ready (called after renderBundle)
  const initCustomerViewToggle = () => {
    document.querySelectorAll("[data-customer-view]").forEach((btn) => {
      btn.addEventListener("click", () => {
        _customerViewMode = btn.dataset.customerView;
        document.querySelectorAll("[data-customer-view]").forEach((b) => b.classList.toggle("active", b === btn));
        _applyCustomerView(_allCustomerRows, _customerViewMode);
      });
    });

    // ── Phase 3B: search input (debounced 200ms) ──
    const searchInput = document.getElementById("srCustomerSearch");
    if (searchInput) {
      let searchTimer;
      searchInput.addEventListener("input", () => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => {
          _customerSearchQ = searchInput.value;
          _applyCustomerView(_allCustomerRows, _customerViewMode);
        }, 200);
      });
    }

    // ── Export workbook view ──
    const btnExportCust = document.getElementById("btnExportCustCSV");
    if (btnExportCust) {
      btnExportCust.addEventListener("click", () => {
        updateExportLinks();
        const href = exportXlsx?.getAttribute("href") || root.dataset.exportXlsx || "";
        if (!href) {
          showActionPlaceholder("Workbook export is not available for this page.");
          return;
        }
        window.location.assign(href);
      });
    }
  };

  // ── Phase 3B: owner pills builder (call after customer data loads) ──
  const buildOwnerPills = (rows) => {
    const container = document.getElementById("srOwnerPills");
    if (!container || !Array.isArray(rows)) return;
    const owners = Array.from(new Set(rows.map((r) => r.account_owner_name || "").filter(Boolean))).sort();
    if (!owners.length) { container.innerHTML = ""; return; }
    if (_activeOwnerFilter && !owners.includes(_activeOwnerFilter)) _activeOwnerFilter = "";

    const truncate = (s, n) => s.length > n ? s.slice(0, n) + "…" : s;
    const allBtn = `<button class="sr-grain-pill ${_activeOwnerFilter ? "" : "active"}" data-owner-filter="" style="margin-right:4px">All Owners</button>`;
    const ownerBtns = owners.map((o) =>
      `<button class="sr-grain-pill ${_activeOwnerFilter === o ? "active" : ""}" data-owner-filter="${escapeHtml(o)}" title="${escapeHtml(o)}">${escapeHtml(truncate(o, 18))}</button>`
    ).join("");
    container.innerHTML = allBtn + ownerBtns;

    if (container.dataset.bound === "1") return;
    container.dataset.bound = "1";
    container.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-owner-filter]");
      if (!btn) return;
      _activeOwnerFilter = btn.dataset.ownerFilter;
      container.querySelectorAll("[data-owner-filter]").forEach((b) => b.classList.toggle("active", b === btn));
      _applyCustomerView(_allCustomerRows, _customerViewMode);
    });
  };

  // ── Phase 3B: Follow-Up List CSV export ──
  window.exportFollowUpList = () => {
    const atRisk = _allCustomerRows.filter((r) => {
      const sig = computeCustomerRisk(r).signal;
      return sig === "critical" || sig === "atrisk" || sig === "lost";
    });
    const toast = document.getElementById("srFollowUpToast");
    if (!atRisk.length) {
      if (toast) { toast.textContent = "✓ No at-risk customers to export"; toast.style.display = "block"; setTimeout(() => { toast.style.display = "none"; }, 3000); }
      return;
    }
    const today = new Date().toISOString().slice(0, 10);
    const header = ["Customer", "Owner", "Territory", "Last Revenue (prior 30d)", "Risk Signal", "Days Silent", "Suggested Action"];
    const csvLines = [header.join(","), ...atRisk.map((r) => {
      const risk = computeCustomerRisk(r);
      const days = customerSilentDays(r) ?? "";
      const momPct = customerMoMValue(r);
      const action = risk.signal === "critical"
        ? `Critical recovery call - silent ${days || "?"}d and MoM ${momPct == null ? NA : `${momPct.toFixed(1)}%`}`
        : risk.signal === "lost"
          ? `Re-engagement call - no orders in ${days || "?"} days`
          : "Account review - declining momentum";
      return [
        `"${(r.customer_name || r.customer_id || "").replace(/"/g, '""')}"`,
        `"${(r.account_owner_name || "").replace(/"/g, '""')}"`,
        `"${(r.territory_name || "").replace(/"/g, '""')}"`,
        `"${fmtMoney0.format(num(r.revenue_prev_30 ?? r.revenue))}"`,
        `"${risk.label}"`,
        `"${days}"`,
        `"${action}"`,
      ].join(",");
    })];
    const blob = new Blob([csvLines.join("\n")], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `wholesale_followup_${today}.csv`;
    a.click();
  };

  // ── v4.0: Follow-Up Priority Queue Drawer ──
  const _buildDrawerQueueHtml = (repName) => {
    const scopedRep = cleanText(repName).toLowerCase();
    const atRisk = _allCustomerRows
      .filter((r) => {
        const sig = computeCustomerRisk(r).signal;
        if (!(sig === "critical" || sig === "atrisk" || sig === "lost")) return false;
        if (!scopedRep) return true;
        return businessRepName(r.account_owner_name, r.account_owner_id, UNASSIGNED_REP_FALLBACK).toLowerCase() === scopedRep;
      })
      .sort((a, b) => {
        // Sort: critical first, then by silent days desc
        const sigOrder = { critical: 0, lost: 1, atrisk: 2 };
        const aSig = computeCustomerRisk(a).signal;
        const bSig = computeCustomerRisk(b).signal;
        const aOrd = sigOrder[aSig] ?? 3;
        const bOrd = sigOrder[bSig] ?? 3;
        if (aOrd !== bOrd) return aOrd - bOrd;
        return (customerSilentDays(b) ?? 0) - (customerSilentDays(a) ?? 0);
      });

    if (!atRisk.length) {
      return '<div class="text-muted text-center py-4">&#9989; No at-risk accounts in the current view.</div>';
    }

    const repLabel = repName ? `for ${escapeHtml(repName)}` : "";
    const subtitle = document.getElementById("srDrawerSubtitle");
    if (subtitle) subtitle.textContent = `${atRisk.length} accounts require follow-up${repLabel ? " " + repLabel : ""}`;

    return atRisk.map((r, idx) => {
      const risk = computeCustomerRisk(r);
      const days = customerSilentDays(r);
      const momPct = customerMoMValue(r);
      const velLabel = momPct !== null
        ? `MoM ${momPct >= 0 ? "+" : ""}${fmtPct.format(momPct)}%`
        : "";
      const action = risk.signal === "critical"
        ? `Recovery call - silent ${days ?? "?"}d${velLabel ? " | " + velLabel : ""}`
        : risk.signal === "lost"
          ? `Re-engagement - no orders in ${days ?? "?"} days`
          : `Account review - declining momentum${velLabel ? " | " + velLabel : ""}`;
      const sigClass = risk.signal === "critical" ? "is-critical" : risk.signal === "atrisk" ? "is-atrisk" : "";
      const customerId = cleanText(r.customer_id || r.key || r.customer_name || "");
      return `
        <div class="sr-priority-item ${sigClass}">
          <div class="sr-priority-rank">#${idx + 1}</div>
          <div class="sr-priority-meta">
            <div class="sr-priority-name">
              <button
                type="button"
                class="sr-priority-link"
                data-priority-customer-id="${escapeHtml(customerId)}"
                data-priority-owner="${escapeHtml(r.account_owner_name || "")}"
              >${escapeHtml(r.customer_name || r.customer_id || NA)}</button>
            </div>
            <div class="sr-priority-sub">${escapeHtml(r.account_owner_name || "Unassigned")}${r.territory_name ? " · " + escapeHtml(r.territory_name) : ""}</div>
            <div class="sr-priority-sub">${days !== null && days !== undefined ? `Silent: ${days}d` : "Silent: unknown"} · Revenue: ${money(r.revenue_prev_30 ?? r.revenue)}</div>
            <div class="sr-priority-action">&#128204; ${escapeHtml(action)}</div>
          </div>
          <span class="badge text-bg-${risk.signal === "critical" ? "danger" : risk.signal === "lost" ? "secondary" : "warning"} ms-1" style="align-self:flex-start;font-size:0.65rem">${escapeHtml(risk.label)}</span>
        </div>
      `;
    }).join("");
  };

  const wireFollowUpDrawer = () => {
    if (followUpDrawerWired) return;
    const body = document.getElementById("srFollowUpDrawerBody");
    if (!body) return;
    body.addEventListener("click", (evt) => {
      const trigger = evt.target.closest("[data-priority-customer-id]");
      if (!trigger) return;
      evt.preventDefault();
      const customerId = cleanText(trigger.getAttribute("data-priority-customer-id"));
      const row = findCustomerRow(_allCustomerRows, { customer_id: customerId });
      if (row) focusCustomerFromPriority(row);
    });
    followUpDrawerWired = true;
  };

  window.openFollowUpDrawer = (repName) => {
    const drawer = document.getElementById("srFollowUpDrawer");
    if (!drawer) return;
    wireFollowUpDrawer();
    const body = document.getElementById("srFollowUpDrawerBody");
    if (body) body.innerHTML = _buildDrawerQueueHtml(repName);
    drawer.style.display = "flex";
    requestAnimationFrame(() => {
      drawer.classList.add("is-open");
      syncBodyScrollLock();
    });
  };

  window.closeFollowUpDrawer = () => {
    const drawer = document.getElementById("srFollowUpDrawer");
    if (!drawer) return;
    drawer.classList.remove("is-open");
    drawer.addEventListener("transitionend", () => {
      drawer.style.display = "none";
      syncBodyScrollLock();
    }, { once: true });
  };

  const buildLostAccountActionText = (account = {}) => {
    const days = account.days_since_order != null ? `${account.days_since_order} days` : "an unknown number of days";
    const territory = account.territory_name ? ` Territory: ${account.territory_name}.` : "";
    const lastDate = account.last_order_date || "unknown";
    const revenueText = money(account.revenue_prev_30 ?? 0);
    const reason = account.opportunity_reason || `${revenueText}/mo account silent for ${days}`;
    
    const proteinText = (account.historical_proteins || []).length 
      ? ` historically purchased ${account.historical_proteins.join(", ")}` 
      : "";
    const proteinBody = (account.historical_proteins || []).length
      ? `This customer has historically purchased: ${account.historical_proteins.join(", ")}. Use this to tailor your outreach.`
      : "Check their historical order guide for department-specific opportunities.";

    return {
      emailSubject: `RE: ${account.customer_name || account.customer_id || "Customer"}${proteinText ? ` (${account.historical_proteins[0]} orders)` : ""} - Re-engagement Opportunity`,
      emailBody:
        `Hi ${account.account_owner_name || "Team"},\n\n` +
        `This is an automated priority alert for ${account.customer_name || account.customer_id || "Customer"}.\n\n` +
        `Signal: ${reason}. (Last invoice: ${lastDate}).` +
        `${territory}\n\n` +
        `${proteinBody}\n\n` +
        `Prior-period monthly revenue was ${revenueText}. This account is currently in the recovery sweet-spot. Please reach out today to secure the next order.\n\nThanks`,
      callBrief:
        `Follow-up priority: ${account.customer_name || account.customer_id || "Customer"}. ${reason}. ` +
        `Last invoice: ${lastDate}. Prior revenue: ${revenueText}.` +
        `${proteinText ? ` Historically buys ${account.historical_proteins.join(", ")}.` : ""}` +
        `${account.territory_name ? ` Territory: ${account.territory_name}.` : ""}`,
      noteText:
        `Recovery Opportunity: ${account.customer_name || account.customer_id || "Customer"} - ${reason}, ` +
        `last invoice ${lastDate}, prior revenue ${revenueText}.` +
        `${proteinText ? ` Species: ${account.historical_proteins.join(", ")}.` : ""}`,
    };
  };

  const bindLostAccountQuickActions = () => {
    const body = document.getElementById("lostAccountsBody");
    if (!body || body.dataset.quickActionsBound === "1") return;
    body.dataset.quickActionsBound = "1";
    body.addEventListener("click", async (evt) => {
      const button = evt.target.closest("[data-followup-action]");
      if (!button) return;
      evt.preventDefault();
      const action = button.getAttribute("data-followup-action") || "";
      const encoded = button.getAttribute("data-followup-text") || "";
      const text = encoded ? decodeURIComponent(encoded) : "";
      if (action === "call") {
        await copyTextToClipboard(text, "Call brief copied");
        return;
      }
      if (action === "note") {
        await copyTextToClipboard(text, "Follow-up note copied");
      }
    });
  };

  // ── 4D: Rep Comparison Modal ──
  const toggleRepSelect = (repId, rowData) => {
    const id = String(repId);
    if (selectedRepIds.has(id)) {
      selectedRepIds.delete(id);
      selectedRepRows.delete(id);
    } else if (selectedRepIds.size < 4) {
      selectedRepIds.add(id);
      if (rowData) selectedRepRows.set(id, rowData);
    }
    renderCompareToolbar();
    renderVirtualTableRows({ force: true });
  };

  const renderCompareToolbar = () => {
    const toolbar = document.getElementById("srCompareToolbar");
    const countEl = document.getElementById("srCompareCount");
    const compareBtn = document.getElementById("srCompareBtn");
    if (!toolbar) return;
    const count = selectedRepIds.size;
    if (count === 0) {
      toolbar.classList.add("d-none");
      return;
    }
    toolbar.classList.remove("d-none");
    if (countEl) countEl.textContent = `${count} rep${count !== 1 ? "s" : ""} selected`;
    if (compareBtn) compareBtn.disabled = count < 2;
  };

  const renderCompareModal = () => {
    const body = document.getElementById("srCompareModalBody");
    if (!body) return;
    const reps = Array.from(selectedRepIds).map(id => selectedRepRows.get(id)).filter(Boolean);
    if (reps.length < 2) return;

    const findBest = (vals, higher = true) => {
      const nums = vals.map(v => (v == null ? null : num(v)));
      const valid = nums.filter(v => v !== null);
      if (!valid.length) return -1;
      const target = higher ? Math.max(...valid) : Math.min(...valid);
      return nums.findIndex(v => v !== null && Math.abs(v - target) < 0.00001);
    };

    const metrics = [
      { label: "Revenue",          key: r => num(r.revenue),            fmt: r => money(r.revenue),                                            higher: true  },
      { label: "Profit",           key: r => r.profit == null ? null : num(r.profit),    fmt: r => r.profit == null ? NA : money(r.profit),  higher: true  },
      { label: "Margin %",         key: r => r.margin_pct == null ? null : num(r.margin_pct),  fmt: r => r.margin_pct == null ? NA : `${fmtPct.format(num(r.margin_pct))}%`, higher: true },
      { label: "YoY Revenue %",    key: r => r.yoy_revenue_pct == null ? null : num(r.yoy_revenue_pct), fmt: r => pct(r.yoy_revenue_pct, false), higher: true },
      { label: "Health Score",     key: r => r.health_score == null ? null : num(r.health_score), fmt: r => r.health_label ? `<span class="badge" style="${healthBadgeStyle(r.health_color, '0.7rem')}">${escapeHtml(r.health_label)}</span>&nbsp;${r.health_score ?? ""}/100` : NA, higher: true, raw: true },
      { label: "Active Customers", key: r => num(r.active_customers),   fmt: r => fmtInt.format(num(r.active_customers)),                     higher: true  },
      { label: "Orders",           key: r => num(r.orders),             fmt: r => fmtInt.format(num(r.orders)),                               higher: true  },
      { label: "Shipped LB",       key: r => num(r.weight_lb),          fmt: r => fmtInt.format(num(r.weight_lb)),                            higher: true  },
      { label: "Revenue Rank",     key: null, fmt: r => escapeHtml(r.quartile_label || NA) },
      { label: "Top Customer",     key: null, fmt: r => escapeHtml(r.top_customer_name || NA) },
      { label: "Top Territory",    key: null, fmt: r => escapeHtml(r.top_territory_name || NA) },
      { label: "Top Protein",      key: null, fmt: r => escapeHtml(r.top_protein_family || NA) },
    ];

    const headerCells = reps.map(r => `<th class="text-center align-middle" style="min-width:150px">${escapeHtml(repDisplayName(r, READABLE_REP_FALLBACK))}</th>`).join("");
    const metricRows = metrics.map(m => {
      const vals = reps.map(r => m.key ? m.key(r) : null);
      const winnerIdx = m.key ? findBest(vals, m.higher !== false) : -1;
      const cells = reps.map((r, idx) => {
        const display = m.raw ? m.fmt(r) : escapeHtml(m.fmt(r));
        const highlight = (winnerIdx === idx && m.key) ? " fw-semibold text-success" : "";
        return `<td class="text-center${highlight}">${display}</td>`;
      }).join("");
      return `<tr><th scope="row" class="text-muted small fw-normal py-2">${m.label}</th>${cells}</tr>`;
    }).join("");

    body.innerHTML = `<div class="table-responsive"><table class="table table-sm table-hover align-middle sr-compare-table mb-0"><thead class="table-light sticky-top"><tr><th scope="col" style="width:140px" class="text-muted">Metric</th>${headerCells}</tr></thead><tbody>${metricRows}</tbody></table></div>`;
  };

  const wireCompare = () => {
    // Checkbox delegation
    document.getElementById("salesreps-table-body")?.addEventListener("change", (evt) => {
      const cb = evt.target.closest(".sr-rep-select-cb");
      if (!cb) return;
      const repId = cb.dataset.repId;
      if (!repId) return;
      const row = (virtualTable.rows || []).find(r => String(r.rep_id || r.key || r.rep_name) === repId);
      toggleRepSelect(repId, row);
    });

    document.getElementById("srSelectAll")?.addEventListener("change", (evt) => {
      if (evt.target.checked) {
        (virtualTable.rows || []).slice(0, 4).forEach(r => {
          const id = String(r.rep_id || r.key || r.rep_name);
          selectedRepIds.add(id);
          selectedRepRows.set(id, r);
        });
      } else {
        selectedRepIds.clear();
        selectedRepRows.clear();
      }
      renderCompareToolbar();
      renderVirtualTableRows({ force: true });
    });

    document.getElementById("srCompareBtn")?.addEventListener("click", () => {
      renderCompareModal();
      const modalEl = document.getElementById("srCompareModal");
      if (modalEl && window.bootstrap?.Modal) {
        window.bootstrap.Modal.getOrCreateInstance(modalEl).show();
      }
    });

    document.getElementById("srClearCompare")?.addEventListener("click", () => {
      selectedRepIds.clear();
      selectedRepRows.clear();
      const sel = document.getElementById("srSelectAll");
      if (sel) sel.checked = false;
      renderCompareToolbar();
      renderVirtualTableRows({ force: true });
    });
  };

  // ── Lost Accounts panel (1B) ──
  const renderLostAccountsPanel = (lostAccounts = []) => {
    const badge  = document.getElementById("lostAccountsBadge");
    const body   = document.getElementById("lostAccountsBody");
    const chevron = document.getElementById("lostPanelChevron");
    const header  = body?.previousElementSibling;
    if (!badge || !body) return;

    // Use priority_score if available, otherwise fallback to days_since_order
    const sorted = [...lostAccounts].sort((a, b) => {
      if (a.priority_score != null && b.priority_score != null) {
        return b.priority_score - a.priority_score;
      }
      const da = a.days_since_order ?? 0;
      const db = b.days_since_order ?? 0;
      return db - da;
    });

    const n = sorted.length;
    const criticalCount = sorted.filter(a => a.urgency_label === "Critical" || (a.days_since_order ?? 0) > 60).length;
    const highCount     = sorted.filter(a => a.urgency_label === "High" || ((a.days_since_order ?? 0) > 45 && (a.days_since_order ?? 0) <= 60)).length;
    const totalRevAtRisk = sorted.reduce((s, a) => s + (a.revenue_prev_30 ?? 0), 0);

    badge.textContent = n;
    badge.className = `badge ${n > 0 ? "bg-danger" : "bg-success"}`;

    // Auto-expand when there are lost accounts
    if (n > 0 && body.style.display === "none") {
      body.style.display = "block";
      if (chevron) chevron.style.transform = "rotate(180deg)";
      if (header) header.setAttribute("aria-expanded", "true");
    }

    if (n === 0) {
      body.innerHTML = '<p class="text-success mb-0 px-2 py-2">\u2713 No lost accounts. Every prior customer placed an order this period.</p>';
      return;
    }

    const rows = sorted.map((a) => {
      const days = a.days_since_order ?? null;
      const daysStr = days !== null ? `${days}d` : "\u2014";
      const score = a.priority_score ?? 0;
      const urgency = a.urgency_label || (days > 60 ? "Critical" : days > 30 ? "High" : "Medium");
      
      let urgencyClass = "text-bg-secondary";
      let rowBg = "";
      
      if (urgency === "Critical") { 
        urgencyClass = "text-bg-danger";
        rowBg = `background:${alphaColor(SR_THEME.blood, 0.08)};`; 
      }
      else if (urgency === "High") { 
        urgencyClass = "text-bg-warning";
        rowBg = `background:${alphaColor(SR_THEME.gold, 0.12)};`; 
      }

      const owner     = a.account_owner_name || "\u2014";
      const territory = a.territory_name     || "\u2014";
      const lastDate  = a.last_order_date    || "\u2014";
      const reason    = a.opportunity_reason || `${money(a.revenue_prev_30 ?? 0)} silent for ${daysStr}`;
      
      const phone = a.customer_phone ? `<div class="small text-muted"><i class="bi bi-telephone"></i> ${escapeHtml(a.customer_phone)}</div>` : "";
      const email = a.customer_email ? `<div class="small text-muted"><i class="bi bi-envelope"></i> ${escapeHtml(a.customer_email)}</div>` : "";

      const actionText = buildLostAccountActionText(a);
      const subject = encodeURIComponent(actionText.emailSubject);
      const bodyText = encodeURIComponent(actionText.emailBody);
      const callText = encodeURIComponent(actionText.callBrief);
      const noteText = encodeURIComponent(actionText.noteText);

      const proteinBadges = (a.historical_proteins || []).slice(0, 3).map(p => {
        const lower = String(p).toLowerCase();
        const color = lower.includes("fresh") || lower.includes("meat") ? "bg-danger" : lower.includes("apparel") || lower.includes("electronics") ? "bg-warning text-dark" : lower.includes("grocery") || lower.includes("dairy") ? "bg-success" : "bg-info";
        return `<span class="badge ${color}" style="font-size:0.6rem;padding:0.2em 0.4em">${escapeHtml(p.slice(0,4))}</span>`;
      }).join(" ");

      return `<tr style="${rowBg}">
        <td>
            <div class="fw-bold text-dark">${escapeHtml(a.customer_name || a.customer_id || NA)}</div>
            <div class="d-flex align-items-center gap-1 mt-1">
              ${proteinBadges}
              <span class="small text-muted ms-1" style="font-size:0.65rem">${escapeHtml(territory)}</span>
            </div>
            ${phone}
            ${email}
        </td>
        <td class="text-center">
            <span class="badge ${urgencyClass}" style="font-size:0.7rem">${urgency.toUpperCase()}</span>
            <div class="small text-muted" title="Priority Score: 0-100 (Revenue + Churn Probability)">Score: ${score}</div>
        </td>
        <td>
          <div class="small fw-semibold text-oxblood">${escapeHtml(reason)}</div>
          <div class="small text-muted mt-1">
            Last invoice: ${escapeHtml(lastDate)}
            ${days !== null ? ` · ${daysStr} since` : ""}
          </div>
        </td>
        <td class="text-end" style="font-variant-numeric:tabular-nums">
          <div class="fw-bold">${money(a.revenue_prev_30 ?? 0)}</div>
          <div class="small text-muted" style="font-size:0.65rem">Prior LTM</div>
        </td>
        <td>
          <div class="sr-quick-actions">
            <a href="mailto:${a.customer_email || ""}?subject=${subject}&body=${bodyText}" class="sr-quick-action" title="Email follow-up briefing" aria-label="Email follow-up briefing">
              <i class="bi bi-envelope"></i>
            </a>
            <button type="button" class="sr-quick-action" data-followup-action="call" data-followup-text="${callText}" title="Copy call brief" aria-label="Copy call brief">
              <i class="bi bi-telephone"></i>
            </button>
            <button type="button" class="sr-quick-action" data-followup-action="note" data-followup-text="${noteText}" title="Copy note" aria-label="Copy note">
              <i class="bi bi-journal-text"></i>
            </button>
          </div>
        </td>
      </tr>`;
    }).join("");

    const summaryBar = `
      <div style="display:flex;gap:1.5rem;padding:8px 12px 10px;border-bottom:1px solid ${SR_THEME.gridStrong};background:${alphaColor(SR_THEME.cream, 0.94)};font-size:0.82rem;flex-wrap:wrap">
        <span style="color:${SR_THEME.blood};font-weight:700">\uD83D\uDD34 ${criticalCount} critical</span>
        <span style="color:${SR_THEME.bronze};font-weight:600">\uD83D\uDFE1 ${highCount} high priority</span>
        <span style="color:${SR_THEME.espresso}">Opportunities: <strong>${n}</strong></span>
        <span style="color:${SR_THEME.espresso};margin-left:auto">Revenue at risk: <strong>${money(totalRevAtRisk)}</strong></span>
      </div>`;

    body.innerHTML = `
      ${summaryBar}
      <div class="table-responsive">
        <table class="table table-sm table-hover mb-0" style="font-size:0.85rem">
          <thead class="table-light">
            <tr>
              <th>Customer / Species Focus</th>
              <th class="text-center">Urgency</th>
              <th>Reason for Opportunity</th>
              <th class="text-end">Risk Exposure</th>
              <th class="text-end">Re-engage</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      <p class="text-muted mb-0 px-2 py-2" style="font-size:0.78rem">
        Priority queue uses <strong>Predictive Churn</strong> (Custom Frequency Pulse) and <strong>Silent Account</strong> detection to identify high-value re-engagement opportunities.
      </p>
    `;
    bindLostAccountQuickActions();
  };

  // Toggle lost accounts panel open/closed
  window.srToggleLostPanel = () => {
    const body    = document.getElementById("lostAccountsBody");
    const chevron = document.getElementById("lostPanelChevron");
    const header  = body?.previousElementSibling;
    if (!body) return;
    const isOpen = body.style.display !== "none";
    body.style.display = isOpen ? "none" : "block";
    if (chevron) chevron.style.transform = isOpen ? "" : "rotate(180deg)";
    if (header) header.setAttribute("aria-expanded", String(!isOpen));
  };

  const renderCustomerMovers = (analysis = {}) => {
    const movers = analysis.customer_movers || {};

    const moverItemHtml = (row, isDown = false) => {
      const revNow  = opt(row.revenue) ?? 0;
      const delta   = opt(row.delta_revenue) ?? 0;
      const revPrev = revNow - delta;
      const pctVal  = opt(row.delta_pct);
      const isNew = !isDown && revPrev <= 0 && revNow > 0;
      const isLost = isDown && revNow === 0 && revPrev > 0;
      const badge = isNew
        ? '<span class="sr-mover-badge is-new">NEW</span>'
        : isLost
          ? '<span class="sr-mover-badge is-lost">LOST</span>'
          : "";
      const pctStr = revPrev === 0
        ? "(new)"
        : pctVal !== null
          ? `(${pctVal >= 0 ? "+" : ""}${fmtPct.format(pctVal)}%)`
          : "";
      const deltaClass = isDown ? "delta-down" : "delta-up";
      const subline = `<div class="sr-mover-subline">Prior: ${money(revPrev)} &rarr; Now: ${money(revNow)}</div>`;
      const velocity = sparklineSvg(row.velocity_points || []);
      let tintStyle = "";
      if (isDown) {
        if (isLost) {
          tintStyle = ` style="background:${alphaColor(SR_THEME.blood, 0.08)}"`;
        } else if (pctVal !== null && pctVal <= -50) {
          tintStyle = ` style="background:${alphaColor(SR_THEME.blood, 0.05)}"`;
        } else if (pctVal !== null && pctVal <= -25) {
          tintStyle = ` style="background:${alphaColor(SR_THEME.gold, 0.08)}"`;
        }
      }
      return `
        <li${tintStyle}${drillAttr(customerPayload(row, "Customer Intelligence", "Customer Movers", "Revenue Delta", row.delta_revenue))}>
          <div>
            <div class="sr-mover-header">
              <div class="sr-list-main">${escapeHtml(row.customer_name || row.customer_id || NA)}</div>
              ${badge}
            </div>
            <div class="sr-list-sub">${escapeHtml(businessRepName(row.account_owner_name, row.account_owner_id, READABLE_REP_FALLBACK))}${row.territory_name ? ` · ${escapeHtml(row.territory_name)}` : ""}${row.yoy_revenue != null ? ` · PY ${money(row.yoy_revenue)}` : ""}</div>
            ${subline}
            <div class="sr-mover-meta">
              <div class="sr-list-sub">${isNew ? "New revenue win in the visible book." : isLost ? "Lost account: no current revenue in the selected period." : "Three-month velocity leading into the change."}</div>
              <div class="sr-mover-velocity">
                <span class="sr-mover-velocity-label">Velocity</span>
                ${velocity}
              </div>
            </div>
          </div>
          <div class="sr-list-metric ${deltaClass}">${money(row.delta_revenue)} ${pctStr ? `<span style="font-weight:400;font-size:0.8em">${pctStr}</span>` : ""}</div>
        </li>
      `;
    };

    renderSimpleList("srCustomerMoversUp",   movers.up   || [], (row) => moverItemHtml(row, false));
    renderSimpleList("srCustomerMoversDown", movers.down || [], (row) => moverItemHtml(row, true));

    // Update header stat line (1C-4): "N gained · N lost ↓"
    const gained = (movers.up   || []).length;
    const lostN  = (movers.down || []).filter((r) => (opt(r.revenue) ?? 0) === 0).length;
    const statEl = document.getElementById("srMoversStatLine");
    if (statEl) {
      const lostLink = lostN > 0
        ? `<a href="#lostAccountsPanel" class="text-danger fw-semibold" onclick="document.getElementById('lostAccountsBody')?.style.display==='none'&&srToggleLostPanel();setTimeout(()=>document.getElementById('lostAccountsPanel')?.scrollIntoView({behavior:'smooth'}),80);return false;">${lostN} lost &darr;</a>`
        : `<span>${lostN} lost</span>`;
      statEl.innerHTML = `${gained} gained &middot; ${lostLink}`;
    }
  };

  const renderProteinTable = (rows = []) => {
    const tbody = document.getElementById("srProteinTableBody");
    if (!tbody) return;
    const totalRevenue = (Array.isArray(rows) ? rows : []).reduce((acc, row) => acc + num(row.revenue), 0);
    const enriched = (Array.isArray(rows) ? rows : []).map((row) => ({ ...row, share_pct: totalRevenue > 0 ? (num(row.revenue) / totalRevenue) * 100 : null }));
    const ranked = sortRows(enriched, state.proteinSortBy, state.proteinSortDir);
    if (!ranked.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="text-muted">No department mix is available for the selected filters.</td></tr>';
      return;
    }
    tbody.innerHTML = ranked
      .map((row) => `
        <tr${drillAttr(proteinPayload(row.protein_family, "Department Intelligence", "Department Performance", "Revenue", row.revenue, { filter_mode: "current_window" }))}>
          <td><span class="sr-link"${drillAttr(proteinPayload(row.protein_family, "Department Intelligence", "Department Performance", "Revenue", row.revenue, { filter_mode: "current_window" }))}>${escapeHtml(row.protein_family || NA)}</span></td>
          <td class="text-end">${money(row.revenue)}</td>
          <td class="text-end">${row.profit == null ? NA : money(row.profit)}</td>
          <td class="text-end">${marginCellHtml(row)}</td>
          <td class="text-end">${row.share_pct == null ? NA : `${fmtPct.format(num(row.share_pct))}%`}</td>
          <td class="text-end">${row.yoy_delta_revenue == null ? NA : money(row.yoy_delta_revenue)}</td>
        </tr>
      `)
      .join("");
  };

  const renderProteinChart = (rows = []) => {
    const canvasId = "srProteinChart";
    const ranked = (Array.isArray(rows) ? rows : []).slice(0, 8);
    const hasData = ranked.length > 0;
    toggleEmpty(canvasId, !hasData);
    if (!ChartLib) return;
    if (!hasData) {
      destroyChart("protein");
      return;
    }
    const chart = createChart("protein", canvasId, {
      type: "bar",
      data: {
        labels: ranked.map((row) => row.protein_family || NA),
        datasets: [
          {
            label: "Revenue",
            data: ranked.map((row) => num(row.revenue)),
            backgroundColor: SR_THEME.gold,
          },
          {
            label: "YoY Δ Revenue",
            data: ranked.map((row) => num(row.yoy_delta_revenue)),
            backgroundColor: SR_THEME.oxblood,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        onClick: (_evt, activeEls) => {
          const idx = activeEls?.[0]?.index;
          if (idx == null) return;
          const row = ranked[idx];
          if (!row) return;
          openUniversal(
            proteinPayload(row.protein_family, "Department Intelligence", "Department / Category Mix", "Revenue", row.revenue, {
              filter_mode: "current_window",
            }),
            document.getElementById(canvasId)
          );
        },
        scales: {
          y: { ticks: { callback: (v) => fmtMoney0.format(v) } },
        },
      },
    });
    if (!chart) toggleEmpty(canvasId, true, "Chart unavailable.");
  };

  const renderTopReps = (rows = []) => {
    const canvasId = "topRepsChart";
    const metric = state.metric;
    const conf = metricConfig[metric] || metricConfig.revenue;
    const scopeMap = {
      revenue: "direct_revenue",
      profit: "direct_profit",
      margin_dollar: "direct_profit",
      margin_pct: "direct_margin_pct",
      customers: "direct_customers",
      weight_lb: "direct_weight_lb",
    };
    const scopedMetricKey = state.leaderboardScope === "direct_only" ? (scopeMap[metric] || metric) : metric;
    const scopedMetricLabel = state.leaderboardScope === "direct_only"
      ? {
        revenue: "Direct Revenue",
        profit: "Direct Profit",
        margin_dollar: "Direct Margin $",
        margin_pct: "Direct Margin %",
        customers: "Direct Customers",
        weight_lb: "Direct Weight (LB)",
      }[metric] || conf.label
      : conf.label;
    const scopedMetricValue = (row) => {
      if (scopedMetricKey === "direct_margin_pct") {
        if (opt(row.direct_margin_pct) != null) return num(row.direct_margin_pct);
        const directRevenue = opt(row.direct_revenue);
        const directProfit = opt(row.direct_profit);
        return directRevenue && directProfit != null ? (directProfit / directRevenue) * 100 : 0;
      }
      return num(row?.[scopedMetricKey]);
    };
    const topRows = [...(Array.isArray(rows) ? rows : [])]
      .sort((a, b) => scopedMetricValue(b) - scopedMetricValue(a))
      .slice(0, state.topN);
    const hasData = topRows.length > 0;
    const averageValue = Array.isArray(rows) && rows.length
      ? rows.reduce((sum, row) => sum + scopedMetricValue(row), 0) / rows.length
      : null;

    toggleEmpty(canvasId, !hasData);
    if (!ChartLib) return;
    if (!hasData) {
      destroyChart("topReps");
      return;
    }

    // ── 5A: team average reference line ──
    const avgLinePlugin = {
      id: "avgLine_topReps",
      afterDraw(chartInst) {
        if (averageValue == null) return;
        const ctx2 = chartInst.ctx;
        const xAxis = chartInst.scales.x;
        const yAxis = chartInst.scales.y;
        if (!xAxis || !yAxis) return;
        const x = xAxis.getPixelForValue(averageValue);
        if (x < xAxis.left || x > xAxis.right) return;
        ctx2.save();
        ctx2.setLineDash([6, 4]);
        ctx2.strokeStyle = alphaColor(SR_THEME.oxblood, 0.55);
        ctx2.lineWidth = 1.5;
        ctx2.beginPath();
        ctx2.moveTo(x, yAxis.top);
        ctx2.lineTo(x, yAxis.bottom);
        ctx2.stroke();
        ctx2.fillStyle = SR_THEME.oxblood;
        ctx2.font = '11px "Avenir Next", "Segoe UI", system-ui';
        ctx2.textAlign = "right";
        ctx2.fillText(`Avg: ${conf.fmt(averageValue)}`, xAxis.right - 4, yAxis.top + 14);
        ctx2.restore();
      },
    };

    const chart = createChart("topReps", canvasId, {
      type: "bar",
      plugins: [avgLinePlugin],
      data: {
        labels: topRows.map((r) => repDisplayName(r)),
        datasets: [{
          label: scopedMetricLabel,
          data: topRows.map((r) => scopedMetricValue(r)),
          backgroundColor: SR_THEME.gold,
          borderRadius: 4,
        }],
      },
      options: {
        indexAxis: "y",
        responsive: true,
        maintainAspectRatio: false,
        onClick: (_evt, activeEls) => {
          const idx = activeEls?.[0]?.index;
          if (idx == null) return;
          const row = topRows[idx];
          if (!row) return;
          _flyToRep(row.rep_name);
          openUniversal(salesrepPayload(row, "Ranking & Performance", "Top Reps", scopedMetricLabel, scopedMetricValue(row)), document.getElementById(canvasId));
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: (ctx) => `${scopedMetricLabel}: ${conf.fmt(ctx.raw)}`,
              afterBody: (items) => {
                const idx = items?.[0]?.dataIndex;
                const row = idx == null ? null : topRows[idx];
                if (!row) return [];
                const rankChange = opt(row.rank_change);
                const rankLine = rankChange == null ? `Rank movement: ${NA}` : `Rank movement: ${rankChange > 0 ? "+" : ""}${fmtInt.format(rankChange)}`;
                return [
                  `Total book revenue: ${money(row.revenue)}`,
                  `Direct / Inherited: ${money(row.direct_revenue)} / ${money(row.inherited_revenue)}`,
                  rankLine,
                ];
              },
            },
          },
        },
      },
    });
    if (!chart) toggleEmpty(canvasId, true, "Chart unavailable.");
  };

  const renderPareto = (rows = []) => {
    const canvasId = "revenueShareChart";
    const metric = state.metric;
    const conf = metricConfig[metric] || metricConfig.revenue;
    const sorted = sortedByMetric(rows, metric).slice(0, state.topN);
    const total = sorted.reduce((acc, r) => acc + Math.max(0, rowMetricValue(r, metric)), 0);
    let running = 0;
    const cumulative = sorted.map((r) => {
      running += Math.max(0, rowMetricValue(r, metric));
      return total > 0 ? (running / total) * 100 : 0;
    });
    const hasData = sorted.length > 0;

    toggleEmpty(canvasId, !hasData);
    if (!ChartLib) return;
    if (!hasData) {
      destroyChart("pareto");
      return;
    }

    const chart = createChart("pareto", canvasId, {
      data: {
        labels: sorted.map((r) => repDisplayName(r)),
        datasets: [
          {
            type: "bar",
            label: conf.label,
            data: sorted.map((r) => rowMetricValue(r, metric)),
            backgroundColor: SR_THEME.gold,
          },
          {
            type: "line",
            label: "Cumulative %",
            data: cumulative,
            borderColor: SR_THEME.oxblood,
            yAxisID: "y1",
            tension: 0.25,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        onClick: (_evt, activeEls) => {
          const idx = activeEls?.[0]?.index;
          if (idx == null) return;
          const row = sorted[idx];
          if (!row) return;
          openUniversal(salesrepPayload(row, "Ranking & Performance", "Revenue Share", conf.label, rowMetricValue(row, metric)), document.getElementById(canvasId));
        },
        scales: {
          y: {
            ticks: {
              callback: (v) => conf.fmt(v),
            },
          },
          y1: {
            position: "right",
            grid: { drawOnChartArea: false },
            ticks: {
              callback: (v) => `${fmtPct.format(v)}%`,
            },
          },
        },
      },
    });
    if (!chart) toggleEmpty(canvasId, true, "Chart unavailable.");
  };

  const renderQuotaAttainment = (rows = []) => {
    const canvasId = "srQuotaChart";
    const ranked = (Array.isArray(rows) ? rows : [])
      .filter((row) => opt(row.attainment_pct) !== null)
      .slice(0, state.topN);
    const hasData = ranked.length > 0;

    toggleEmpty(canvasId, !hasData, "Quota data is unavailable for the current scope.");
    if (!ChartLib) return;
    if (!hasData) {
      destroyChart("quota");
      return;
    }

    const chart = createChart("quota", canvasId, {
      data: {
        labels: ranked.map((row) => repDisplayName(row)),
        datasets: [
          {
            type: "bar",
            label: "Quota attainment",
            data: ranked.map((row) => num(row.attainment_pct)),
            backgroundColor: ranked.map((row) => num(row.attainment_pct) >= 100 ? SR_THEME.forest : SR_THEME.gold),
            borderRadius: 4,
          },
          {
            type: "line",
            label: "Goal",
            data: ranked.map(() => 100),
            borderColor: SR_THEME.oxblood,
            borderDash: [6, 4],
            borderWidth: 2,
            pointRadius: 0,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: {
          y: {
            beginAtZero: true,
            ticks: { callback: (value) => `${fmtPct.format(value)}%` },
            title: { display: true, text: "Sales ÷ governed quota" },
          },
        },
        plugins: {
          tooltip: {
            callbacks: {
              afterBody: (items) => {
                const idx = items?.[0]?.dataIndex;
                const row = idx == null ? null : ranked[idx];
                return row ? [`Sales: ${money(row.sales)}`, `Quota: ${money(row.quota)}`] : [];
              },
            },
          },
        },
      },
    });
    if (!chart) toggleEmpty(canvasId, true, "Chart unavailable.");
  };

  const renderEfficiency = (rows = []) => {
    const canvasId = "effChart";
    const points = (Array.isArray(rows) ? rows : []).map((r) => ({
      x: num(r.customers),
      y: num(r.revenue),
      r: Math.max(4, Math.min(18, Math.sqrt(Math.abs(num(r.profit || 0))) / 45)),
      rep_name: repDisplayName(r),
      margin_pct: opt(r.margin_pct),
      profit: opt(r.profit),
    }));
    const hasData = points.length > 0;

    toggleEmpty(canvasId, !hasData);
    if (!ChartLib) return;
    if (!hasData) {
      destroyChart("eff");
      return;
    }

    const chart = createChart("eff", canvasId, {
      type: "bubble",
      data: { datasets: [{ data: points, backgroundColor: alphaColor(SR_THEME.gold, 0.55) }] },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        onClick: (_evt, activeEls) => {
          const idx = activeEls?.[0]?.index;
          if (idx == null) return;
          const row = rows[idx];
          if (!row) return;
          openUniversal(salesrepPayload(row, "Efficiency & Risk", "Rep Efficiency", "Revenue", row.revenue), document.getElementById(canvasId));
        },
        scales: {
          x: { title: { display: true, text: "Customers" }, ticks: { callback: (v) => fmtInt.format(v) } },
          y: { title: { display: true, text: "Revenue" }, ticks: { callback: (v) => fmtMoney0.format(v) } },
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: (ctx) => {
                const raw = ctx.raw || {};
                const margin = raw.margin_pct == null ? NA : `${fmtPct.format(raw.margin_pct)}%`;
                const profit = raw.profit == null ? NA : fmtMoney0.format(raw.profit);
                return `${raw.rep_name || "Rep"}: ${fmtMoney0.format(raw.y || 0)} revenue, ${fmtInt.format(raw.x || 0)} customers, ${profit} profit, ${margin} margin`;
              },
            },
          },
        },
      },
    });
    if (!chart) toggleEmpty(canvasId, true, "Chart unavailable.");
  };

  const renderConcentration = (rows = []) => {
    const canvasId = "concentrationChart";
    const ranked = (Array.isArray(rows) ? [...rows] : [])
      .sort((a, b) => num(b.top_customer_share) - num(a.top_customer_share))
      .slice(0, state.topN);

    const hasData = ranked.length > 0;
    toggleEmpty(canvasId, !hasData);
    if (!ChartLib) return;
    if (!hasData) {
      destroyChart("concentration");
      return;
    }

    const chart = createChart("concentration", canvasId, {
      type: "bar",
      data: {
        labels: ranked.map((r) => repDisplayName(r)),
        datasets: [
          {
            label: "Top 1 Share %",
            data: ranked.map((r) => num(r.top_customer_share) * 100),
            backgroundColor: SR_THEME.oxblood,
          },
          {
            label: "Top 5 Share %",
            data: ranked.map((r) => num(r.top_5_customer_share) * 100),
            backgroundColor: SR_THEME.gold,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        onClick: (_evt, activeEls) => {
          const idx = activeEls?.[0]?.index;
          if (idx == null) return;
          const row = ranked[idx];
          if (!row) return;
          openUniversal(salesrepPayload(row, "Efficiency & Risk", "Concentration Risk", "Top customer share", num(row.top_customer_share) * 100), document.getElementById(canvasId));
        },
        scales: {
          y: {
            ticks: { callback: (v) => `${fmtPct.format(v)}%` },
          },
        },
        plugins: {
          tooltip: {
            callbacks: {
              afterBody: (items) => {
                const i = items?.[0]?.dataIndex;
                if (i == null) return "";
                const row = ranked[i] || {};
                return `HHI: ${fmtPct.format(num(row.customer_hhi) * 100)} | Top customer: ${row.top_customer_name || NA}`;
              },
            },
          },
        },
      },
    });
    if (!chart) toggleEmpty(canvasId, true, "Chart unavailable.");
  };

  const renderProfitRevenue = (rows = []) => {
    const canvasId = "profitRevenueChart";
    const points = (Array.isArray(rows) ? rows : [])
      .map((r) => ({
        x: num(r.revenue),
        y: opt(r.profit),
        rep_name: repDisplayName(r),
        rep_id: r.rep_id || r.key || r.rep_name,
      }))
      .filter((r) => r.y !== null);

    const hasData = points.length > 0;
    toggleEmpty(canvasId, !hasData, "No profit data available.");
    if (!ChartLib) return;
    if (!hasData) {
      destroyChart("profitRevenue");
      return;
    }

    const midX = points.reduce((acc, p) => acc + p.x, 0) / points.length;
    const midY = points.reduce((acc, p) => acc + p.y, 0) / points.length;
    const maxX = Math.max(...points.map((p) => p.x), 0);
    const maxY = Math.max(...points.map((p) => p.y), 0);

    const chart = createChart("profitRevenue", canvasId, {
      type: "scatter",
      data: {
        datasets: [
          {
            label: "Reps",
            data: points,
            backgroundColor: alphaColor(SR_THEME.oxblood, 0.65),
          },
          {
            type: "line",
            label: "Revenue midpoint",
            data: [{ x: midX, y: 0 }, { x: midX, y: maxY * 1.05 }],
            borderColor: alphaColor(SR_THEME.gold, 0.62),
            borderDash: [6, 6],
            pointRadius: 0,
          },
          {
            type: "line",
            label: "Profit midpoint",
            data: [{ x: 0, y: midY }, { x: maxX * 1.05, y: midY }],
            borderColor: alphaColor(SR_THEME.forest, 0.62),
            borderDash: [6, 6],
            pointRadius: 0,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        onClick: (_evt, activeEls) => {
          const hit = activeEls?.[0];
          if (!hit || hit.datasetIndex !== 0) return;
          const point = points[hit.index];
          if (!point) return;
          openUniversal(
            salesrepPayload(point, "Efficiency & Risk", "Profit vs Revenue", "Profit", point.y),
            document.getElementById(canvasId)
          );
        },
        scales: {
          x: { title: { display: true, text: "Revenue" }, ticks: { callback: (v) => fmtMoney0.format(v) } },
          y: { title: { display: true, text: "Profit" }, ticks: { callback: (v) => fmtMoney0.format(v) } },
        },
        plugins: {
          tooltip: {
            callbacks: {
              label: (ctx) => {
                if (!ctx.raw || ctx.datasetIndex !== 0) return ctx.dataset.label;
                return `${ctx.raw.rep_name}: ${fmtMoney0.format(ctx.raw.y)} profit on ${fmtMoney0.format(ctx.raw.x)} revenue`;
              },
            },
          },
        },
      },
    });
    if (!chart) toggleEmpty(canvasId, true, "Chart unavailable.");
  };

  const renderAspLeaders = (rows = []) => {
    const canvasId = "aspChart";
    const sorted = (Array.isArray(rows) ? [...rows] : [])
      .filter((r) => opt(r.asp) !== null)
      .sort((a, b) => num(b.asp) - num(a.asp))
      .slice(0, 10);

    const hasData = sorted.length > 0;
    toggleEmpty(canvasId, !hasData);
    if (!ChartLib) return;
    if (!hasData) {
      destroyChart("asp");
      return;
    }

    const chart = createChart("asp", canvasId, {
      type: "bar",
      data: {
        labels: sorted.map((r) => repDisplayName(r)),
        datasets: [{ label: "ASP", data: sorted.map((r) => num(r.asp)), backgroundColor: SR_THEME.bronze }],
      },
      options: {
        indexAxis: "y",
        responsive: true,
        maintainAspectRatio: false,
        onClick: (_evt, activeEls) => {
          const idx = activeEls?.[0]?.index;
          if (idx == null) return;
          const row = sorted[idx];
          if (!row) return;
          openUniversal(salesrepPayload(row, "Efficiency & Risk", "ASP Leaders", "ASP", row.asp), document.getElementById(canvasId));
        },
        plugins: { legend: { display: false } },
        scales: { x: { ticks: { callback: (v) => fmtMoney2.format(v) } } },
      },
    });
    if (!chart) toggleEmpty(canvasId, true, "Chart unavailable.");
  };

  const riskBadgeClass = (severity) => {
    if (severity === "high") return "text-bg-danger";
    if (severity === "medium") return "text-bg-warning";
    return "text-bg-secondary";
  };

  const renderRiskFlags = (flags = [], payload = {}) => {
    const holder = document.getElementById("srRiskFlags");
    if (!holder) return;
    holder.innerHTML = "";
    const rows = Array.isArray(flags) ? [...flags] : [];
    const criticalCustomers = (payload?.analysis?.top_customers || []).filter((row) => isCriticalCustomer(row)).length;
    if (criticalCustomers > 0) {
      rows.unshift({
        key: "critical_customers",
        label: "Critical customers: Silent > 30d and MoM revenue < -20%",
        count: criticalCustomers,
        severity: "high",
      });
    }
    if (!rows.length) {
      holder.innerHTML = '<li class="text-muted small">No active risk flags.</li>';
      return;
    }
    rows.forEach((f) => {
      const li = document.createElement("li");
      li.className = "risk-item";
      const payload = f.key === "top_customer_concentration"
        ? attributedWorkspacePayload("Insight Strip", "Risk Watch", f.label, f.count, { filter_mode: "current_window" })
        : f.key === "low_margin"
          ? attributedWorkspacePayload("Insight Strip", "Risk Watch", f.label, f.count, { filter_mode: "current_window" })
          : f.key === "profit_decline"
            ? attributedWorkspacePayload("Insight Strip", "Risk Watch", f.label, f.count, { filter_mode: "current_window" })
            : f.key === "unassigned_customers"
              ? attributedWorkspacePayload("Insight Strip", "Risk Watch", f.label, f.count, { filter_mode: "current_window", dq_bucket: "unassigned" })
              : f.key === "critical_customers"
                ? attributedWorkspacePayload("Insight Strip", "Risk Watch", f.label, f.count, { filter_mode: "current_window" })
              : null;
      if (payload) li.setAttribute("data-drilldown-payload", JSON.stringify(payload));
      li.innerHTML = `<span>${f.label || f.key || "Risk"}</span><span class="badge ${riskBadgeClass(f.severity)}">${fmtInt.format(num(f.count))}</span>`;
      holder.appendChild(li);
    });
  };

  const appendFilterQS = (url) => {
    if (!url) return "#";
    const params = baseQuery();
    params.delete("page");
    params.delete("page_size");
    params.delete("sort");
    params.delete("dir");
    params.delete("q");
    const q = params.toString();
    if (!q) return url;
    return url.includes("?") ? `${url}&${q}` : `${url}?${q}`;
  };

  const rowSignalChip = (row) => {
    const chips = [];
    const targetMargin = opt(row.target_margin_pct);
    const statusKey = marginStatusKey(row.status_key);
    if (["red", "orange"].includes(statusKey)) {
      chips.push('<span class="chip-danger">Below min</span>');
    } else if (statusKey === "yellow" || (opt(row.margin_pct) !== null && targetMargin !== null && num(row.margin_pct) < targetMargin)) {
      chips.push('<span class="chip-warn">Below target</span>');
    }
    if (opt(row.top_5_customer_share) !== null && num(row.top_5_customer_share) > 0.65) chips.push('<span class="chip-warn">High concentration</span>');
    return chips.join(" ");
  };

  const rankChangeChip = (value) => {
    const change = opt(value);
    if (change == null || change === 0) return '<span class="sr-badge-neutral">Flat</span>';
    if (change > 0) return `<span class="sr-badge-neutral sr-badge-up">+${fmtInt.format(change)}</span>`;
    return `<span class="sr-badge-neutral sr-badge-down">${fmtInt.format(change)}</span>`;
  };

  const focusedRepSummary = () => {
    const labels = (Array.isArray(state.focusedRepLabels) ? state.focusedRepLabels : []).filter(Boolean);
    if (!labels.length) return "";
    if (labels.length === 1) return labels[0];
    if (labels.length === 2) return labels.join(", ");
    return `${labels.slice(0, 2).join(", ")} +${labels.length - 2}`;
  };

  const buildTableRowHtml = (row, rowIndex) => {
    const repId = row.rep_id || row.key || row.rep_name || "";
    const repName = repDisplayName(row, READABLE_REP_FALLBACK);
    const baseUrl = drilldownTemplate ? drilldownTemplate.replace("__ID__", encodeURIComponent(repId)) : "#";
    const href = appendFilterQS(baseUrl);
    const payload = salesrepPayload(row, "Detailed Table", "Sales Rep Table", "Revenue", row.revenue);
    const signals = rowSignalChip(row);
    const focused = state.focusedRepIds.includes(String(repId));
    const selected = selectedRepIds.has(String(repId));
    const silentMeta = silentAge(row.last_order_date);
    return `
      <tr class="sr-virtual-row${focused ? " is-rep-focus" : ""}${row.revenue_quartile === 4 ? " sr-row-q4" : row.revenue_quartile === 1 ? " sr-row-q1" : ""}${selected ? " sr-row-selected" : ""}" tabindex="0" data-row-index="${rowIndex}" data-rep-id="${escapeHtml(repId)}" data-href="${escapeHtml(href)}"${payload ? ` data-drilldown-payload="${escapeHtml(JSON.stringify(payload))}"` : ""}>
        <td class="col-select text-center" onclick="event.stopPropagation()">
          <input type="checkbox" class="form-check-input sr-rep-select-cb" data-rep-id="${escapeHtml(String(repId))}" ${selected ? "checked" : ""} aria-label="Select ${escapeHtml(repName)} for comparison">
        </td>
        <td class="sticky-col" title="${escapeHtml(repName)}">
          <div class="d-flex align-items-center justify-content-between gap-2">
            <span class="sr-link"${drillAttr(payload)}>${escapeHtml(repName)}</span>
            ${rankChangeChip(row.rank_change)}
          </div>
          <div class="sr-secondary-metric">${fmtInt.format(num(row.current_owned_customers))} current owned · ${fmtInt.format(num(row.inherited_customers))} inherited</div>
        </td>
        <td class="col-health text-center">
          ${healthRingHtml(row)}
        </td>
        <td class="col-direct_ratio">${directInheritedRatioHtml(row)}</td>
        <td class="col-quartile text-center">
          <div title="Smart Rank: composite score weighting 60% revenue + 40% margin. Lower number = stronger overall performer."
               class="sr-composite-rank${row._composite_rank === 1 ? " sr-rank-gold" : row._composite_rank <= 3 ? " sr-rank-silver" : ""}">
            ${row._composite_rank != null ? `#${row._composite_rank}` : escapeHtml(row.quartile_label || NA)}
          </div>
          <div class="sr-rank-sub">${escapeHtml(row.quartile_label || "")}</div>
        </td>
        <td class="text-end col-revenue">
          <div>${money(row.revenue)}</div>
          <div class="sr-secondary-metric">${money(row.direct_revenue)} direct · ${money(row.transferred_in_revenue)} inherited</div>
        </td>
        <td class="text-end col-profit">${row.profit == null ? NA : money(row.profit)}</td>
        <td class="text-end col-margin_pct">${marginCellHtml(row)}</td>
        <td class="text-end col-silent_days ${silentMeta.days > 60 ? "text-danger fw-bold" : silentMeta.days > 30 ? "text-warning" : ""}">${silentMeta.days == null ? TEXT_EMPTY : fmtInt.format(silentMeta.days)}</td>
        <td class="text-end col-mom_revenue_delta">${momentumArrowHtml(row)}</td>
        <td class="text-end col-yoy_revenue_delta">${money(row.yoy_revenue_delta)}</td>
        <td class="text-end col-weight_lb">${fmtInt.format(num(row.weight_lb))}</td>
        <td class="text-end col-active_customers">${fmtInt.format(num(row.active_customers))}</td>
        <td class="text-end col-current_owned_customers">${fmtInt.format(num(row.current_owned_customers))}</td>
        <td class="text-end col-inherited_customers">${fmtInt.format(num(row.inherited_customers))}</td>
        <td class="text-end col-transferred_in_revenue">${row.transferred_in_revenue == null ? NA : money(row.transferred_in_revenue)}</td>
        <td class="text-end col-transferred_out_revenue">${row.transferred_out_revenue == null ? NA : money(row.transferred_out_revenue)}</td>
        <td class="text-end col-yoy_revenue_pct">${pct(row.yoy_revenue_pct, false)}</td>
        <td class="text-end col-territory_count">${fmtInt.format(num(row.territory_count))}</td>
        <td class="col-replaced_reps" title="${escapeHtml(row.replaced_rep_names || "")}">${row.replaced_rep_count ? `${fmtInt.format(num(row.replaced_rep_count))} · ${escapeHtml(row.replaced_rep_names || "")}` : NA}</td>
        <td class="col-top_territory" title="${escapeHtml(row.top_territory_name || NA)}"><span class="sr-link"${drillAttr(territoryPayload(row.top_territory_name, "Detailed Table", "Top Territory", "Revenue", row.top_territory_revenue, { filter_mode: "current_window" }))}>${escapeHtml(row.top_territory_name || NA)}</span></td>
        <td class="col-top_customer" title="${escapeHtml(row.top_customer_name || NA)}"><span class="sr-link"${drillAttr(repWorkspacePayload(row, "Detailed Table", "Top Customer", "Revenue", row.top_customer_revenue, { filter_mode: "current_window" }))}>${escapeHtml(row.top_customer_name || NA)}</span></td>
        <td class="col-top_protein"><span class="sr-link"${drillAttr(proteinPayload(row.top_protein_family, "Detailed Table", "Top Protein", "Revenue", row.top_protein_revenue, { filter_mode: "current_window" }))}>${escapeHtml(row.top_protein_family || NA)}</span></td>
        <td class="text-end col-leakage" title="Revenue sold below minimum department margin target">
          <div class="${row.leakage_revenue > 0 ? "text-danger fw-bold" : "text-muted"}">${money(row.leakage_revenue)}</div>
          <div class="sr-secondary-metric">${fmtInt.format(num(row.leakage_count))} lines</div>
        </td>
        <td class="col-protein_penetration">
          <div class="d-flex flex-wrap gap-1">
            ${(row.protein_penetration || []).slice(0, 3).map(p => `
              <span class="badge ${p.penetration_pct > 50 ? "bg-success" : p.penetration_pct > 20 ? "bg-info" : "bg-light text-dark"}" style="font-size:0.65rem" title="${escapeHtml(p.family)}: ${fmtInt.format(p.customers)} customers (${pct(p.penetration_pct)} penetration)">
                ${escapeHtml(p.family.slice(0,3))} ${pct(p.penetration_pct)}
              </span>
            `).join('')}
          </div>
        </td>
        <td class="text-end col-overdue_customers">
          <div class="${row.overdue_customers > 0 ? "text-danger fw-bold" : ""}">${fmtInt.format(num(row.overdue_customers))}</div>
          <div class="sr-secondary-metric">Predictive Churn</div>
        </td>
        <td class="col-flags">${signals || '<span class="text-muted small">--</span>'}</td>
        <td class="text-end"><a class="btn btn-sm btn-outline-primary sr-row-open" href="${href}" aria-label="Open detail for ${repName}">&#8599;</a></td>
      </tr>
    `;
  };

  const renderVirtualTableRows = ({ force = false } = {}) => {
    const tbody = virtualTable.tbody || document.getElementById("salesreps-table-body");
    const wrapper = virtualTable.wrapper || document.getElementById("srTableWrap");
    virtualTable.tbody = tbody;
    virtualTable.wrapper = wrapper;
    if (!tbody || !wrapper) return;

    const rows = Array.isArray(virtualTable.rows) ? virtualTable.rows : [];
    if (!rows.length) {
      virtualTable.lastRange = "";
      return;
    }

    const viewportHeight = Math.max(wrapper.clientHeight || 0, 320);
    const rowHeight = Math.max(virtualTable.rowHeight || 88, 64);
    const scrollTop = Math.max(wrapper.scrollTop || 0, 0);
    const startIndex = Math.max(0, Math.floor(scrollTop / rowHeight) - virtualTable.overscan);
    const visibleCount = Math.ceil(viewportHeight / rowHeight) + (virtualTable.overscan * 2);
    const endIndex = Math.min(rows.length, startIndex + visibleCount);
    const rangeKey = `${startIndex}:${endIndex}:${rows.length}`;
    if (!force && virtualTable.lastRange === rangeKey) return;
    virtualTable.lastRange = rangeKey;

    const topSpacer = startIndex * rowHeight;
    const bottomSpacer = Math.max((rows.length - endIndex) * rowHeight, 0);
    const colSpan = Math.max(document.querySelectorAll("#srTable thead th").length, 1);
    const parts = [];
    if (topSpacer > 0) {
      parts.push(`<tr class="sr-virtual-spacer" aria-hidden="true"><td colspan="${colSpan}" style="height:${topSpacer}px"></td></tr>`);
    }
    rows.slice(startIndex, endIndex).forEach((row, idx) => {
      parts.push(buildTableRowHtml(row, startIndex + idx));
    });
    if (bottomSpacer > 0) {
      parts.push(`<tr class="sr-virtual-spacer" aria-hidden="true"><td colspan="${colSpan}" style="height:${bottomSpacer}px"></td></tr>`);
    }
    tbody.innerHTML = parts.join("");

    const measuredRow = tbody.querySelector("tr.sr-virtual-row");
    if (measuredRow && !force) {
      const measuredHeight = Math.round(measuredRow.getBoundingClientRect().height);
      if (measuredHeight >= 64 && Math.abs(measuredHeight - virtualTable.rowHeight) > 6) {
        virtualTable.rowHeight = measuredHeight;
        renderVirtualTableRows({ force: true });
        return;
      }
    }

    applyColumnVisibility();
    if (window.universalDrilldown && typeof window.universalDrilldown.enhanceAll === "function") {
      window.universalDrilldown.enhanceAll();
    }
  };

  const renderVirtualCustomerRows = ({ force = false } = {}) => {
    const tbody = customerVirtualTable.tbody || document.getElementById("srTopCustomersBody");
    const wrapper = customerVirtualTable.wrapper || document.getElementById("srTopCustomersWrap");
    customerVirtualTable.tbody = tbody;
    customerVirtualTable.wrapper = wrapper;
    if (!tbody || !wrapper) return;

    const rows = Array.isArray(customerVirtualTable.rows) ? customerVirtualTable.rows : [];
    if (!rows.length) {
      customerVirtualTable.lastRange = "";
      tbody.innerHTML = `<tr><td colspan="10" class="text-muted">${customerVirtualTable.emptyMessage || emptyMessage}</td></tr>`;
      return;
    }

    const viewportHeight = Math.max(wrapper.clientHeight || 0, 320);
    const rowHeight = Math.max(customerVirtualTable.rowHeight || 74, 64);
    const scrollTop = Math.max(wrapper.scrollTop || 0, 0);
    const startIndex = Math.max(0, Math.floor(scrollTop / rowHeight) - customerVirtualTable.overscan);
    const visibleCount = Math.ceil(viewportHeight / rowHeight) + (customerVirtualTable.overscan * 2);
    const endIndex = Math.min(rows.length, startIndex + visibleCount);
    const rangeKey = `${startIndex}:${endIndex}:${rows.length}`;
    if (!force && customerVirtualTable.lastRange === rangeKey) return;
    customerVirtualTable.lastRange = rangeKey;

    const topSpacer = startIndex * rowHeight;
    const bottomSpacer = Math.max((rows.length - endIndex) * rowHeight, 0);
    const parts = [];
    if (topSpacer > 0) {
      parts.push(`<tr class="sr-virtual-spacer" aria-hidden="true"><td colspan="10" style="height:${topSpacer}px"></td></tr>`);
    }
    rows.slice(startIndex, endIndex).forEach((row) => {
      parts.push(_buildCustomerRowHtml(row, row._customerBadge || null));
    });
    if (bottomSpacer > 0) {
      parts.push(`<tr class="sr-virtual-spacer" aria-hidden="true"><td colspan="10" style="height:${bottomSpacer}px"></td></tr>`);
    }
    tbody.innerHTML = parts.join("");

    const measuredRow = tbody.querySelector("tr.sr-virtual-row");
    if (measuredRow && !force) {
      const measuredHeight = Math.round(measuredRow.getBoundingClientRect().height);
      if (measuredHeight >= 64 && Math.abs(measuredHeight - customerVirtualTable.rowHeight) > 6) {
        customerVirtualTable.rowHeight = measuredHeight;
        renderVirtualCustomerRows({ force: true });
        return;
      }
    }

    if (window.universalDrilldown && typeof window.universalDrilldown.enhanceAll === "function") {
      window.universalDrilldown.enhanceAll();
    }
  };

  const scheduleVirtualCustomerRender = ({ force = false } = {}) => {
    if (force) {
      customerVirtualTable.scheduled = false;
      renderVirtualCustomerRows({ force: true });
      return;
    }
    if (customerVirtualTable.scheduled) return;
    customerVirtualTable.scheduled = true;
    window.requestAnimationFrame(() => {
      customerVirtualTable.scheduled = false;
      renderVirtualCustomerRows();
    });
  };

  const scheduleVirtualTableRender = ({ force = false } = {}) => {
    if (force) {
      virtualTable.scheduled = false;
      renderVirtualTableRows({ force: true });
      return;
    }
    if (virtualTable.scheduled) return;
    virtualTable.scheduled = true;
    window.requestAnimationFrame(() => {
      virtualTable.scheduled = false;
      renderVirtualTableRows();
    });
  };

  const scrollFocusedRepIntoView = () => {
    if (!state.scrollToFocusedRep || !state.focusedRepIds.length || !virtualTable.wrapper || !virtualTable.rows.length) return;
    const targetIndex = virtualTable.rows.findIndex((row) => state.focusedRepIds.includes(String(row.rep_id || row.key || row.rep_name || "")));
    state.scrollToFocusedRep = false;
    if (targetIndex < 0) return;
    document.getElementById("srTableSection")?.scrollIntoView({ behavior: "smooth", block: "start" });
    const nextScrollTop = Math.max((targetIndex * virtualTable.rowHeight) - (virtualTable.wrapper.clientHeight * 0.14), 0);
    virtualTable.wrapper.scrollTop = nextScrollTop;
    renderVirtualTableRows({ force: true });
  };

  const renderTable = (table = {}) => {
    const tbody = document.getElementById("salesreps-table-body");
    if (!tbody) return;
    virtualTable.tbody = tbody;
    virtualTable.wrapper = document.getElementById("srTableWrap");
    tbody.innerHTML = "";

    const rows = Array.isArray(table.rows) ? table.rows : [];

    // ── Smart Rank: composite score = 60% revenue percentile + 40% margin percentile ──
    // Weighting is explicit and stable so leadership can verify/adjust
    if (rows.length > 1) {
      const revs = rows.map((r) => num(r.revenue));
      const margins = rows.map((r) => opt(r.margin_pct) ?? 0);
      const maxRev = Math.max(...revs, 1);
      const maxMargin = Math.max(...margins, 1);
      rows.forEach((r) => {
        const revScore = num(r.revenue) / maxRev;          // 0..1
        const marginScore = Math.max(opt(r.margin_pct) ?? 0, 0) / maxMargin; // 0..1
        r._composite_score = 0.6 * revScore + 0.4 * marginScore;
      });
      const sorted = [...rows].sort((a, b) => (b._composite_score ?? 0) - (a._composite_score ?? 0));
      sorted.forEach((r, idx) => { r._composite_rank = idx + 1; });
    } else if (rows.length === 1) {
      rows[0]._composite_score = 1;
      rows[0]._composite_rank = 1;
    }

    virtualTable.rows = rows;
    virtualTable.lastRange = "";
    // ── Phase 6D: empty state ──
    const emptyEl = document.getElementById("srTableEmpty");
    if (!rows.length) {
      if (emptyEl) emptyEl.style.display = "block";
      if (virtualTable.wrapper) virtualTable.wrapper.scrollTop = 0;
    } else {
      if (emptyEl) emptyEl.style.display = "none";
      if (virtualTable.wrapper) virtualTable.wrapper.scrollTop = 0;
      renderVirtualTableRows({ force: true });
    }

    const page = num(table.page || state.page, 1);
    const pageSize = num(table.page_size || state.pageSize, state.pageSize);
    const total = num(table.total_rows || table.total || table.all_rows, 0);
    const totalPages = Math.max(1, num(table.total_pages || Math.ceil(total / Math.max(pageSize, 1)), 1));

    const start = total > 0 ? (page - 1) * pageSize + 1 : 0;
    const end = total > 0 ? Math.min(page * pageSize, total) : 0;

    const focusedText = focusedRepSummary();
    let summaryText = total > 0 ? `Showing ${start}-${end} of ${fmtInt.format(total)} reps` : "No rows";
    if (total > 0 && rows.length) {
      const totalRev = rows.reduce((s, r) => s + num(r.revenue), 0);
      const marginRows = rows.filter(r => r.margin_pct != null);
      const wMarginNum = marginRows.reduce((s, r) => s + num(r.revenue) * num(r.margin_pct), 0);
      const wMarginRev = marginRows.reduce((s, r) => s + num(r.revenue), 0);
      const weightedMargin = wMarginRev > 0 ? wMarginNum / wMarginRev : null;
      // ── Phase 4C: add avg health score (revenue-weighted) ──
      const healthRows = rows.filter(r => r.health_score != null);
      const avgHealth = healthRows.length
        ? Math.round(healthRows.reduce((s, r) => s + num(r.health_score), 0) / healthRows.length)
        : null;
      summaryText += ` · Total Rev: ${fmtMoney0.format(totalRev)}`;
      if (weightedMargin !== null) summaryText += ` · Avg Margin: ${fmtPct.format(weightedMargin)}%`;
      if (avgHealth !== null)      summaryText += ` · Avg Health: ${avgHealth}/100`;
    }
    setText("salesrepsPagerSummary", focusedText ? `Sales rep focus: ${focusedText} · ${summaryText}` : summaryText);
    setText("salesrepsPagerIndicator", `Page ${page} of ${totalPages}`);

    const prev = document.getElementById("salesrepsPrev");
    const next = document.getElementById("salesrepsNext");
    if (prev) prev.disabled = page <= 1;
    if (next) next.disabled = page >= totalPages;
    scrollFocusedRepIntoView();
  };

  const resolveFocusedCustomerFromPayload = (payload = {}) => {
    const rows = Array.isArray(payload.analysis?.top_customers) ? payload.analysis.top_customers : [];
    const customerFilters = currentUrlFilters().customers || [];
    const requested = pendingCustomerFocus
      || (customerFilters.length
        ? { customer_id: customerFilters[0], customer_name: focusedCustomer?.customer_name || customerFilters[0] }
        : null);
    if (!requested) {
      focusedCustomer = null;
      return null;
    }
    const match = findCustomerRow(rows, requested);
    if (match) {
      focusedCustomer = match;
      pendingCustomerFocus = null;
      return match;
    }
    if (!focusedCustomer || customerFocusKey(focusedCustomer) !== customerFocusKey(requested)) {
      focusedCustomer = requested;
    }
    return focusedCustomer;
  };

  const applyColumnVisibility = () => {
    document.querySelectorAll("[data-col-toggle]").forEach((cb) => {
      const key = cb.dataset.colToggle;
      if (!key) return;
      const visible = columnVisibility[key] !== false;
      cb.checked = visible;
      document.querySelectorAll(`.col-${key}`).forEach((el) => {
        el.classList.toggle("sr-hidden-col", !visible);
      });
    });
  };

  // ── Phase 4D: column presets ──
  const COLUMN_PRESETS = {
    "Full View":    null,   // all visible
    "Executive":    ["revenue", "profit", "margin_pct", "active_customers", "health", "quartile", "protein_penetration"],
    "Risk View":    ["health", "quartile", "revenue", "yoy_revenue_pct", "margin_pct", "leakage", "overdue_customers", "flags"],
    "Scott's View": ["revenue", "active_customers", "yoy_revenue_pct", "margin_pct", "top_customer", "protein_penetration"],
  };
  const ALL_COLUMN_KEYS = Object.keys(DEFAULT_COLUMN_VISIBILITY);

  const applyColumnPreset = (presetName) => {
    const cols = COLUMN_PRESETS[presetName];
    const labelEl = document.getElementById("srColumnPresetLabel");
    if (labelEl) labelEl.textContent = `${presetName} ▾`;
    sessionStorage.setItem("wholesale_col_preset", presetName);
    ALL_COLUMN_KEYS.forEach((key) => {
      columnVisibility[key] = cols === null ? (DEFAULT_COLUMN_VISIBILITY[key] ?? true) : cols.includes(key);
    });
    applyColumnVisibility();
    renderVirtualTableRows({ force: true });
  };

  document.querySelectorAll("[data-col-preset]").forEach((btn) => {
    btn.addEventListener("click", () => applyColumnPreset(btn.dataset.colPreset));
  });

  const syncSortClasses = () => {
    document.querySelectorAll("#srTable .sortable").forEach((th) => {
      th.classList.remove("asc", "desc");
      if (th.dataset.sortKey === state.sortBy) th.classList.add(state.sortDir);
    });
    document.querySelectorAll("[data-top-customers-sort]").forEach((th) => {
      th.classList.remove("asc", "desc");
      if (th.dataset.topCustomersSort === state.topCustomersSortBy) th.classList.add(state.topCustomersSortDir);
    });
    document.querySelectorAll("[data-protein-sort]").forEach((th) => {
      th.classList.remove("asc", "desc");
      if (th.dataset.proteinSort === state.proteinSortBy) th.classList.add(state.proteinSortDir);
    });
  };

  const renderBundle = (rawPayload = {}) => {
    const payload = window.normalizeBundlePayload ? window.normalizeBundlePayload(rawPayload) : rawPayload;
    lastPayload = payload;
    // Service by manager. Grouped by the rep on the line rather than through
    // the ownership attribution used for revenue: delivery performance is a
    // property of the shipment, not of who owns the account today.
    if (window.renderAvailabilityStrip) {
      window.renderAvailabilityStrip("salesrepsAvailability", payload.inventory, {
        eyebrow: "Service behind the book",
        rowsKey: "by_rep",
        rowLabel: "Market manager",
        note: "What each manager's stores actually received. Ranked by OTIF, worst first."
      });
    }
    clearDeferredChartWork();
    
    try {
      updateColumnLabels(payload.meta || {});
      resolveFocusedCustomerFromPayload(payload);
      renderExecutive(payload);
      renderQuotaAttainment(payload.charts?.quota_attainment || []);
      renderSummaryNarrative(payload);
      renderWarnings(payload.warnings, payload);
      renderInsights(payload);
      renderOwnershipHighlights(payload);
    } catch (err) {
      logError("Header rendering failed", err);
    }

    const analysis = payload.analysis || {};
    
    try {
      renderPortfolioSection(payload);
      renderTopCustomers(analysis.top_customers || [], payload.lost_accounts ?? []);
      renderCustomerMovers(analysis);
      renderLostAccountsPanel(payload.lost_accounts ?? []);
    } catch (err) {
      logError("Portfolio/Customers rendering failed", err);
    }

    try {
      // Nothing called this. The panel markup and this one line went together
      // in the performance pass, leaving a complete map renderer, its CSS and
      // its `map_customers` payload in the build with no way to reach any of it.
      initLiveMap(payload);
    } catch (err) {
      logError("Live map rendering failed", err);
    }

    try {
      renderProteinTable(analysis.proteins || []);
      // 6D: Protein section subtitle
      const proteins = analysis.proteins || [];
      if (proteins.length) {
        setText("srSectionProteinSubtitle", `${proteins.length} protein famil${proteins.length !== 1 ? "ies" : "y"} in scope · margin benchmarks applied where available`);
      }
    } catch (err) {
      logError("Department table rendering failed", err);
    }

    try {
      renderDataQuality(analysis.data_quality || []);
      renderTable(payload.table || {});
      renderTableFooter(payload);
      renderRiskFlags(payload.risk_flags || [], payload);
      renderGapFocusPanel(focusedCustomer);
      renderFilterBreadcrumb(payload);
    } catch (err) {
      logError("Table/Flags rendering failed", err);
    }

    if (window.universalDrilldown && typeof window.universalDrilldown.enhanceAll === "function") {
      window.universalDrilldown.enhanceAll();
    }
    
    syncSortClasses();
    scheduleViewportHeightSync();
    
    const tableRows = payload.table?.rows || [];
    const topRepRows = payload.charts?.top_reps || tableRows;

    scheduleDeferredChartWork(() => {
      try {
        const proteinSignature = signatureForRows(analysis.proteins || [], ["protein_family", "revenue", "profit", "margin_pct", "minimum_margin_pct", "target_margin_pct", "status_key"]);
        memoizedRender("protein-chart", proteinSignature, () => renderProteinChart(analysis.proteins || []));
        
        renderTopReps(topRepRows);
        // 6D: Comparison section subtitle
        const n = topRepRows.length;
        if (n) setText("srSectionComparisonSubtitle", `${n} rep${n !== 1 ? "s" : ""} · select checkboxes in the table below to compare side-by-side`);
        
        renderTrend(payload.charts?.trend || payload.trend || {});
        renderConcentration(payload.charts?.concentration || []);

        // Restored. The performance pass removed these six call sites and their
        // markup but left both the renderers below and the server-side series:
        // the bundle was still computing and shipping `monthly_compare`,
        // `transfers`, `pareto`, `scatter`, `profit_vs_revenue` and
        // `asp_leaders` - 95 KB of a 290 KB payload - to a page that drew none
        // of it. These are the charts, not new ones; the data was never gone.
        renderMonthlyCompare(payload.charts?.monthly_compare || payload.trend?.monthly_compare || {});
        renderTransfers(payload.charts?.transfers || []);
        renderPareto(payload.charts?.pareto || payload.table?.rows || []);
        renderEfficiency(payload.charts?.scatter || []);
        renderProfitRevenue(payload.charts?.profit_vs_revenue || []);
        renderAspLeaders(payload.charts?.asp_leaders || []);
      } catch (err) {
        logError("Deferred chart rendering failed", err);
      } finally {
        setAllChartsLoading(false);
      }
    }, { delay: 30 });

    setScorecardLoading(false);
    setSummaryNarrativeLoading(false);
    
    persistSnapshot(payload);
  };

  const fetchBundle = async (options = {}) => {
    reqId += 1;
    const thisReq = reqId;
    if (currentAbort) currentAbort.abort();
    currentAbort = new AbortController();
    if (!lastPayload && !options?.snapshot?.payload) {
      setScorecardLoading(true);
      setSummaryNarrativeLoading(true);
      setAllChartsLoading(true);
    }
    const qs = syncBrowserUrl();
    updateExportLinks();
    const url = qs ? `${bundleUrl}?${qs}` : bundleUrl;
    const snapshot = options.snapshot || null;

    try {
      const headers = pageCache ? pageCache.prepareHeaders(url, { Accept: "application/json" }) : { Accept: "application/json" };
      const res = await authFetch(url, {
        method: "GET",
        credentials: "same-origin",
        signal: currentAbort.signal,
        headers,
      });
      if (pageCache) pageCache.rememberResponse(url, res);
      if (res.status === 304) {
      // A 304 says "your copy is current", but the page cache stores only the
      // ETag - the body lives in a separate snapshot that may be absent,
      // expired, or too large to have been kept. With neither a rendered
      // payload nor a snapshot there is nothing to be current, so re-request
      // unconditionally rather than leaving the page on its placeholders.
        if (lastPayload) return;
        if (snapshot?.payload) { renderBundle(snapshot.payload); return; }
        const retry = await fetch(url, {
          method: "GET",
          credentials: "same-origin",
          signal: currentAbort.signal,
          headers: { Accept: "application/json", "Cache-Control": "no-cache" },
        });
        if (pageCache) pageCache.rememberResponse(url, retry);
        const retryPayload = await retry.json();
        if (thisReq !== reqId) return;
        if (!retry.ok) throw new Error(retryPayload?.error?.message || `HTTP ${retry.status}`);
        renderBundle(retryPayload);
        return;
      }
      const payload = await res.json();
      if (thisReq !== reqId) return;
      if (!res.ok) throw new Error(payload?.error?.message || `HTTP ${res.status}`);
      renderBundle(payload);
    } catch (err) {
      if (err?.name === "AbortError") return;
      logError("salesreps bundle failed", err);
      if (!lastPayload) {
        CHART_IDS.forEach((id) => toggleEmpty(id, true));
        setScorecardLoading(false);
        setSummaryNarrativeLoading(false);
      }
      setText("srWhatChanged", lastPayload ? "What changed: refresh failed. Displaying the last successful snapshot." : "What changed: failed to load bundle.");
    } finally {
      if (thisReq !== reqId) return;
      const detail = { qs: state.qs };
      if (currentApplyId) {
        detail.applyId = currentApplyId;
        currentApplyId = "";
      }
      try {
        if (typeof window.dispatchGlobalFiltersApplied === "function") {
          window.dispatchGlobalFiltersApplied(detail);
        } else {
          window.dispatchEvent(new CustomEvent("globalFilters:applied", { detail }));
        }
      } catch (_e) {
        // no-op
      }
    }
  };

  const waitForFiltersReady = async () => {
    const fallback = () => {
      try {
        return (window.getGlobalFilterState && window.getGlobalFilterState()) || {};
      } catch (_e) {
        return {};
      }
    };
    if (window.filtersReady && typeof window.filtersReady.then === "function") {
      try {
        return await window.filtersReady;
      } catch (_e) {
        return fallback();
      }
    }
    return fallback();
  };

  const resolveInitialQS = () => {
    const locationQs = (window.location.search || "").replace(/^\?/, "");
    if (locationQs) return locationQs;
    try {
      if (window.getGlobalFilterState) {
        const st = window.getGlobalFilterState();
        if (st?.qs) return String(st.qs).replace(/^\?/, "");
      }
    } catch (_e) {
      // ignore
    }
    return "";
  };

  const applyFilters = (qs, filters = null, { scroll = false } = {}) => {
    state.qs = String(qs || "").replace(/^\?/, "");
    syncStateFromQS(state.qs);
    syncFocusedReps(filters || currentFilterState(), { scroll });
    state.page = 1;
    syncControlsFromState();
    fetchBundle();
  };

  const debounce = (fn, delay = 250) => {
    let timer = null;
    return (...args) => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => fn(...args), delay);
    };
  };

  const rerenderLocalState = () => {
    if (!lastPayload) return;
    syncBrowserUrl();
    updateExportLinks();
    renderBundle(lastPayload);
  };

  // ── Phase 5B: KPI card click-to-sort ──
  const wireKpiSort = () => {
    document.querySelectorAll(".sr-kpi[data-kpi-sort]").forEach((card) => {
      card.addEventListener("click", () => {
        const key = card.dataset.kpiSort;
        if (!key) return;
        if (state.sortBy === key) {
          state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
        } else {
          state.sortBy = key;
          state.sortDir = "desc";
        }
        state.page = 1;
        // Scroll to table so the result is visible
        const tableEl = document.getElementById("srTable");
        if (tableEl) tableEl.scrollIntoView({ behavior: "smooth", block: "start" });
        fetchBundle();
      });
    });
  };

  const wireSorting = () => {
    document.querySelectorAll("#srTable .sortable").forEach((th) => {
      const doSort = () => {
        const key = th.dataset.sortKey || "revenue";
        if (state.sortBy === key) state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
        else {
          state.sortBy = key;
          state.sortDir = key === "rep_name" ? "asc" : "desc";
        }
        state.page = 1;
        fetchBundle();
      };
      th.addEventListener("click", doSort);
      th.addEventListener("keydown", (evt) => {
        if (evt.key === "Enter" || evt.key === " ") {
          evt.preventDefault();
          doSort();
        }
      });
    });
  };

  const wirePager = () => {
    const prev = document.getElementById("salesrepsPrev");
    const next = document.getElementById("salesrepsNext");
    if (prev) {
      prev.addEventListener("click", () => {
        state.page = Math.max(1, state.page - 1);
        fetchBundle();
      });
    }
    if (next) {
      next.addEventListener("click", () => {
        state.page += 1;
        fetchBundle();
      });
    }
  };

  const showActionPlaceholder = (message) => {
    const holder = document.getElementById("srWarnings");
    if (!holder) return;
    holder.innerHTML = `
      <div class="alert alert-info border-0 mb-0" role="status">
        <div class="fw-semibold mb-1">Action Placeholder</div>
        <div>${escapeHtml(message)}</div>
      </div>
    `;
  };

  const wireVirtualTable = () => {
    const wrapper = document.getElementById("srTableWrap");
    if (!wrapper || wrapper.dataset.virtualized === "1") return;
    wrapper.dataset.virtualized = "1";
    virtualTable.wrapper = wrapper;
    wrapper.addEventListener("scroll", () => scheduleVirtualTableRender(), { passive: true });
    window.addEventListener("resize", () => scheduleVirtualTableRender(), { passive: true });
  };

  const wireCustomerVirtualTable = () => {
    const wrapper = document.getElementById("srTopCustomersWrap");
    if (!wrapper || wrapper.dataset.virtualized === "1") return;
    wrapper.dataset.virtualized = "1";
    customerVirtualTable.wrapper = wrapper;
    customerVirtualTable.tbody = document.getElementById("srTopCustomersBody");
    wrapper.addEventListener("scroll", () => scheduleVirtualCustomerRender(), { passive: true });
    window.addEventListener("resize", () => scheduleVirtualCustomerRender(), { passive: true });
  };

  const wireRowClicks = () => {
    const tbody = document.getElementById("salesreps-table-body");
    if (!tbody) return;
    const openRow = (row) => {
      if (!row?.dataset?.href) return;
      if (window.universalDrilldown) return;
      window.location.href = row.dataset.href;
    };
    tbody.addEventListener("click", (evt) => {
      const target = evt.target;
      if (target && target.closest("a")) return;
      const row = target?.closest("tr");
      if (row) openRow(row);
    });
    tbody.addEventListener("keydown", (evt) => {
      if (evt.key !== "Enter") return;
      const row = evt.target?.closest("tr");
      if (row) openRow(row);
    });
  };

  const wireMiniSorts = () => {
    document.querySelectorAll("[data-top-customers-sort]").forEach((th) => {
      const applySort = () => {
        const key = th.dataset.topCustomersSort || "revenue";
        if (state.topCustomersSortBy === key) state.topCustomersSortDir = state.topCustomersSortDir === "asc" ? "desc" : "asc";
        else {
          state.topCustomersSortBy = key;
          state.topCustomersSortDir = key === "customer_name" || key === "account_owner_name" || key === "territory_name" ? "asc" : key === "last_order_date" ? "asc" : "desc";
        }
        rerenderLocalState();
      };
      th.addEventListener("click", applySort);
      th.addEventListener("keydown", (evt) => {
        if (evt.key === "Enter" || evt.key === " ") {
          evt.preventDefault();
          applySort();
        }
      });
    });

    document.querySelectorAll("[data-protein-sort]").forEach((th) => {
      const applySort = () => {
        const key = th.dataset.proteinSort || "revenue";
        if (state.proteinSortBy === key) state.proteinSortDir = state.proteinSortDir === "asc" ? "desc" : "asc";
        else {
          state.proteinSortBy = key;
          state.proteinSortDir = key === "protein_family" ? "asc" : "desc";
        }
        rerenderLocalState();
      };
      th.addEventListener("click", applySort);
      th.addEventListener("keydown", (evt) => {
        if (evt.key === "Enter" || evt.key === " ") {
          evt.preventDefault();
          applySort();
        }
      });
    });
  };

  const wireControls = () => {
    const metricToggle = document.getElementById("srMetricToggle");
    const leaderboardDirectOnly = document.getElementById("srLeaderboardDirectOnly");
    const trendMetric = document.getElementById("srTrendMetric");
    const trendGrain = document.getElementById("srTrendGrain");
    const trendView = document.getElementById("srTrendView");
    const topN = document.getElementById("srTopN");
    const trendReset = document.getElementById("srTrendReset");
    const pageSize = document.getElementById("srPageSize");
    const search = document.getElementById("srSearchInput");
    const attributionMode = document.getElementById("srAttributionMode");
    const includeFormer = document.getElementById("srIncludeFormerReps");
    const transferOnly = document.getElementById("srTransferOnly");

    if (metricToggle) {
      metricToggle.value = state.metric;
      metricToggle.addEventListener("change", () => {
        state.metric = metricToggle.value;
        state.page = 1;
        rerenderLocalState();
      });
    }

    if (leaderboardDirectOnly) {
      leaderboardDirectOnly.checked = state.leaderboardScope === "direct_only";
      leaderboardDirectOnly.addEventListener("change", () => {
        state.leaderboardScope = leaderboardDirectOnly.checked ? "direct_only" : "all";
        rerenderLocalState();
      });
    }

    if (trendMetric) {
      trendMetric.value = state.trendMetric;
      trendMetric.addEventListener("change", () => {
        state.trendMetric = trendMetric.value || "revenue";
        rerenderLocalState();
      });
    }

    if (trendGrain) {
      trendGrain.value = state.trendGrain;
      trendGrain.addEventListener("change", () => {
        state.trendGrain = trendGrain.value || "monthly";
        state.trendSelectedReps = [];
        state.trendFocusMode = false;
        // ── Phase 2: sync grain pills with hidden select ──
        document.querySelectorAll("#srGrainPills .sr-grain-pill").forEach((btn) => {
          btn.classList.toggle("active", btn.dataset.grain === state.trendGrain);
        });
        rerenderLocalState();
      });
    }

    // ── Phase 2: grain pill buttons sync to hidden srTrendGrain select ──
    document.querySelectorAll("#srGrainPills .sr-grain-pill").forEach((btn) => {
      btn.addEventListener("click", () => {
        const grain = btn.dataset.grain;
        if (!grain) return;
        if (trendGrain) {
          trendGrain.value = grain;
          trendGrain.dispatchEvent(new Event("change", { bubbles: true }));
        }
      });
    });

    if (trendView) {
      trendView.value = state.trendView;
      trendView.addEventListener("change", () => {
        state.trendView = trendView.value || "absolute";
        rerenderLocalState();
      });
    }

    if (topN) {
      topN.value = String(state.topN);
      topN.addEventListener("change", () => {
        state.topN = num(topN.value, 10);
        fetchBundle();
      });
    }

    if (trendReset) {
      trendReset.addEventListener("click", () => {
        state.trendSelectedReps = [];
        state.trendFocusMode = false;
        renderTrend(lastPayload?.charts?.trend || lastPayload?.trend || {});
      });
    }

    if (pageSize) {
      pageSize.value = String(state.pageSize);
      pageSize.addEventListener("change", () => {
        const allowed = new Set([25, 50, 100]);
        const next = num(pageSize.value, 25);
        state.pageSize = allowed.has(next) ? next : 25;
        pageSize.value = String(state.pageSize);
        state.page = 1;
        fetchBundle();
      });
    }

    if (search) {
      const debounced = debounce(() => {
        state.search = search.value.trim();
        state.page = 1;
        fetchBundle();
      }, 300);
      search.addEventListener("input", debounced);
    }

    if (attributionMode) {
      attributionMode.value = state.attributionMode;
      attributionMode.addEventListener("change", () => {
        state.attributionMode = attributionMode.value || "current_owner";
        if (state.attributionMode !== "historical_rep") {
          state.rosterMode = "current_only";
          if (includeFormer) includeFormer.checked = false;
        }
        state.page = 1;
        fetchBundle();
      });
    }

    if (includeFormer) {
      includeFormer.checked = state.rosterMode === "include_former";
      includeFormer.addEventListener("change", () => {
        state.rosterMode = includeFormer.checked ? "include_former" : "current_only";
        state.page = 1;
        fetchBundle();
      });
    }

    if (transferOnly) {
      transferOnly.checked = !!state.transferOnly;
      transferOnly.addEventListener("change", () => {
        state.transferOnly = !!transferOnly.checked;
        state.page = 1;
        fetchBundle();
      });
    }

    document.querySelectorAll("[data-col-toggle]").forEach((cb) => {
      const key = cb.dataset.colToggle;
      if (!key) return;
      cb.checked = columnVisibility[key] !== false;
      cb.addEventListener("change", () => {
        columnVisibility = { ...columnVisibility, [key]: !!cb.checked };
        persistColumnVisibility(columnVisibility);
        applyColumnVisibility();
        scheduleVirtualTableRender({ force: true });
        // ── Phase 4D: manual toggle resets preset label to "Custom" ──
        const labelEl = document.getElementById("srColumnPresetLabel");
        if (labelEl) labelEl.textContent = "Custom ▾";
        sessionStorage.removeItem("wholesale_col_preset");
      });
    });

    if (actionCrm) {
      actionCrm.addEventListener("click", () => {
        showActionPlaceholder("Sync to CRM is a placeholder. Wire the destination integration and audit trail before enabling it.");
      });
    }

    if (actionSlack) {
      actionSlack.addEventListener("click", () => {
        showActionPlaceholder("Notify Rep via Slack is a placeholder. Connect the workspace and delivery rules before enabling it.");
      });
    }
  };

  const wireTooltips = () => {
    if (!window.bootstrap || !window.bootstrap.Tooltip) return;
    document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach((el) => {
      if (el.dataset.tooltipReady === "1") return;
      el.dataset.tooltipReady = "1";
      new window.bootstrap.Tooltip(el);
    });
  };

  // ── v4.0: Live Map — MapLibre GL JS, zero cost, OSM tiles ──
  let _liveMap = null;
  let _mapStyleReady = false;   // true after "style.load" fires
  let _mapPopup = null;         // single reusable popup instance
  let _pendingMapPayload = null; // payload queued before map was ready
  let _mapAnimationId = null;
  let _mapStyleFallbackTimer = null;
  let _lastMapFeatures = [];
  const MAP_DEFAULT_VIEW = { center: [-96.5, 38.5], zoom: 3.6 };
  const MAP_SILENT_RISK_DAYS = 30;

  // ── Rep colour palette (hex-matched to brand tokens) ──
  /* Rep colours.
   *
   * This used to match a rep by name substring - "fraser", "rachel", "scott" -
   * against a list from the app this was rebuilt from. None of the current
   * reps match any of those keys, so every one of them fell through to a hash
   * over their name modulo a ten-colour list. A hash does not avoid
   * collisions: Marcus Oyelaran and Elliot Vance both landed on red, Tomasz
   * Bielski and Sam Okonkwo both on blue, Dana Whitfield and Priya
   * Raghunathan both on light green. A legend with three duplicate pairs
   * cannot identify anybody, which is the only thing a legend is for.
   *
   * Assign by position in the sorted roster instead. Distinct by construction
   * up to the length of the palette, and stable across renders because the
   * ordering is the roster's, not the arrival order of the rows.
   *
   * Okabe-Ito: eight hues chosen to stay distinguishable under deuteranopia,
   * protanopia and tritanopia. Reds and greens that read as "good/bad"
   * elsewhere are not in play here - these are identity colours, not scales.
   */
  const REP_PALETTE = [
    "#0072B2", // blue
    "#E69F00", // orange
    "#009E73", // bluish green
    "#CC79A7", // reddish purple
    "#56B4E9", // sky blue
    "#D55E00", // vermillion
    "#F0E442", // yellow
    "#332288", // indigo
  ];
  const _repColorIndex = new Map();

  const _assignRepColors = (repNames) => {
    const unique = [...new Set((repNames || [])
      .map((name) => String(name || "").trim())
      .filter(Boolean))].sort((a, b) => a.localeCompare(b));
    _repColorIndex.clear();
    unique.forEach((name, index) => {
      _repColorIndex.set(name.toLowerCase(), REP_PALETTE[index % REP_PALETTE.length]);
    });
    return unique.length;
  };

  const _repColor = (repName) => {
    if (!repName) return "#9AA5B1";
    const key = String(repName).trim().toLowerCase();
    if (_repColorIndex.has(key)) return _repColorIndex.get(key);
    // A rep who appears after the roster was assigned still gets a stable
    // colour rather than the unassigned grey.
    let hash = 0;
    for (let i = 0; i < key.length; i += 1) hash = (hash * 31 + key.charCodeAt(i)) >>> 0;
    return REP_PALETTE[hash % REP_PALETTE.length];
  };

  // ── Market / store-city centroid lookup ──
  // Keys lowercase; fuzzy matching handles region prefixes and typos.
  // The US entries below cover the store network in seed/catalog.py; the
  // Canadian entries are kept so an older extract still plots.
  const _TERRITORY_CENTROIDS_RAW = {
    // ── Regions ──
    "texas & gulf":         [-96.80, 30.60],
    "southeast":            [-84.39, 33.75],
    "midwest":              [-93.10, 41.60],
    "mid-atlantic":         [-77.04, 39.30],
    "mountain west":        [-105.00, 39.74],
    "pacific":              [-121.49, 38.58],
    "northeast":            [-73.20, 42.60],
    // ── States in the network ──
    "tx": [-97.74, 30.27],  "ga": [-84.39, 33.75],  "oh": [-82.99, 39.96],
    "pa": [-75.16, 39.95],  "co": [-104.99, 39.74], "ca": [-121.49, 38.58],
    "ny": [-78.88, 42.89],
    // ── Store cities ──
    "dallas": [-96.80, 32.78],        "houston": [-95.37, 29.76],
    "san antonio": [-98.49, 29.42],   "austin": [-97.74, 30.27],
    "baton rouge": [-91.19, 30.45],   "atlanta": [-84.39, 33.75],
    "charlotte": [-80.84, 35.23],     "nashville": [-86.78, 36.16],
    "orlando": [-81.38, 28.54],       "birmingham": [-86.80, 33.52],
    "columbus": [-82.99, 39.96],      "indianapolis": [-86.16, 39.77],
    "kansas city": [-94.58, 39.10],   "des moines": [-93.61, 41.59],
    "omaha": [-95.94, 41.26],         "philadelphia": [-75.16, 39.95],
    "richmond": [-77.44, 37.54],      "pittsburgh": [-79.996, 40.44],
    "baltimore": [-76.61, 39.29],     "denver": [-104.99, 39.74],
    "salt lake city": [-111.89, 40.76], "boise": [-116.20, 43.62],
    "albuquerque": [-106.65, 35.08],  "sacramento": [-121.49, 38.58],
    "portland": [-122.68, 45.52],     "spokane": [-117.43, 47.66],
    "fresno": [-119.79, 36.74],       "buffalo": [-78.88, 42.89],
    "hartford": [-72.68, 41.76],      "worcester": [-71.80, 42.26],
    "manchester": [-71.45, 42.99],

    // ── Legacy Canadian extract ──
    // ── Province abbreviations ──
    "bc":  [-123.11, 49.27],
    "ab":  [-113.49, 53.55],
    "on":  [-79.38,  43.70],
    "qc":  [-73.56,  45.50],
    "mb":  [-97.13,  49.89],
    "sk":  [-104.61, 50.45],
    "ns":  [-63.58,  44.68],
    "nb":  [-66.46,  46.50],
    "nl":  [-52.71,  47.56],
    "pei": [-63.13,  46.23],
    "nt":  [-114.37, 62.45],
    "nu":  [-86.79,  70.30],
    "yk":  [-135.06, 60.72],
    // ── Province full names ──
    "british columbia":     [-123.11, 49.27],
    "alberta":              [-113.49, 53.55],
    "ontario":              [-79.38,  43.70],
    "quebec":               [-73.56,  45.50],
    "manitoba":             [-97.13,  49.89],
    "saskatchewan":         [-104.61, 50.45],
    "nova scotia":          [-63.58,  44.68],
    "new brunswick":        [-66.46,  46.50],
    "newfoundland":         [-52.71,  47.56],
    "prince edward island": [-63.13,  46.23],
    "northwest territories":[-114.37, 62.45],
    "nunavut":              [-86.79,  70.30],
    "yukon":                [-135.06, 60.72],

    // ── Greater Vancouver & Lower Mainland (real GPS averages from fact data) ──
    "vancouver":            [-123.11, 49.27],
    "vancouver w":          [-123.18, 49.26],
    "vancouver e":          [-123.05, 49.27],
    "vancouver ns":         [-123.08, 49.32],   // north shore
    "vancouver dt":         [-123.12, 49.28],   // downtown
    "north vancouver":      [-123.04, 49.31],
    "north van":            [-123.04, 49.31],
    "west vancouver":       [-123.16, 49.33],
    "west van":             [-123.14, 49.33],
    "burnaby":              [-122.98, 49.21],
    "richmond":             [-123.16, 49.16],
    "surrey":               [-122.86, 49.19],
    "delta":                [-122.94, 49.09],
    "ladner":               [-123.09, 49.09],
    "tsawwassen":           [-123.08, 49.03],
    "langley":              [-122.67, 49.11],
    "langley twp":          [-122.67, 49.17],
    "fort langley":         [-122.57, 49.17],
    "white rock":           [-122.80, 49.03],
    "new westminster":      [-122.91, 49.20],
    "new west":             [-122.91, 49.20],
    "coquitlam":            [-122.83, 49.25],
    "port coquitlam":       [-122.78, 49.27],
    "port moody":           [-122.85, 49.28],
    "maple ridge":          [-122.60, 49.22],
    "pitt meadows":         [-122.69, 49.23],
    "abbotsford":           [-122.31, 49.05],
    "mission":              [-122.31, 49.13],
    "chilliwack":           [-121.95, 49.16],
    "greater vancouver":    [-122.85, 49.25],
    "lower mainland":       [-122.85, 49.22],

    // ── Sea to Sky Corridor (real GPS averages) ──
    "squamish":             [-123.14, 49.71],
    "brackendale":          [-123.15, 49.77],
    "britannia beach":      [-123.21, 49.62],
    "lions bay":            [-123.24, 49.46],
    "loins bay":            [-123.24, 49.46],  // typo variant in data
    "bowen island":         [-123.33, 49.38],
    "whistler":             [-122.96, 50.11],
    "pemberton":            [-122.80, 50.32],
    "sea to sky":           [-123.00, 49.90],
    "s2s":                  [-123.00, 49.90],

    // ── Vancouver Island & Sunshine Coast ──
    "victoria":             [-123.37, 48.43],
    "north saanich":        [-123.42, 48.62],
    "brentwood bay":        [-123.46, 48.57],
    "sooke":                [-123.73, 48.38],
    "mill bay":             [-123.55, 48.65],
    "duncan":               [-123.70, 48.78],
    "nanaimo":              [-123.94, 49.17],
    "parksville":           [-124.31, 49.32],
    "qualicum beach":       [-124.43, 49.35],
    "tofino":               [-125.91, 49.15],
    "ucluelet":             [-125.55, 48.94],
    "campbell river":       [-125.25, 50.02],
    "north island":         [-125.50, 50.70],
    "west coast":           [-125.55, 49.20],
    "salt spring island":   [-123.49, 48.80],
    "salt spring":          [-123.49, 48.80],
    "sunshine coast":       [-123.75, 49.80],
    "gibsons":              [-123.51, 49.40],

    // ── BC Interior — Okanagan ──
    "kelowna":              [-119.50, 49.89],
    "west kelowna":         [-119.59, 49.86],
    "lake country":         [-119.42, 50.05],
    "naramata":             [-119.60, 49.59],
    "penticton":            [-119.59, 49.49],
    "oliver":               [-119.55, 49.18],
    "osoyoos":              [-119.47, 49.03],
    "vernon":               [-119.27, 50.27],
    "int kelowna":          [-119.50, 49.89],
    "int penticton":        [-119.59, 49.49],
    "int vernon":           [-119.27, 50.27],
    "interior":             [-119.50, 50.00],

    // ── BC Interior — Kootenays ──
    "nelson":               [-117.29, 49.50],
    "south slocan":         [-117.54, 49.49],
    "castlegar":            [-117.66, 49.32],
    "trail":                [-117.71, 49.10],
    "cranbrook":            [-115.77, 49.51],
    "kimberley":            [-116.00, 49.68],
    "golden":               [-116.96, 51.30],
    "revelstoke":           [-118.20, 51.00],
    "int castlegar":        [-117.66, 49.32],
    "int nelson":           [-117.29, 49.50],
    "int cranbrook":        [-115.77, 49.51],

    // ── BC Interior — Central / North ──
    "kamloops":             [-120.33, 50.68],
    "prince george":        [-122.75, 53.92],
    "terrace":              [-128.60, 54.52],
    "smithers":             [-127.17, 54.78],
    "haida gwaii":          [-132.07, 53.25],
    "queen charlotte":      [-132.07, 53.25],
    "int kamloops":         [-120.33, 50.68],
    "int terrace":          [-128.60, 54.52],
    "int smithers":         [-127.17, 54.78],
    "int prince rupert":    [-130.32, 54.32],
    "int williams lake":    [-122.14, 52.14],
    "prince rupert":        [-130.32, 54.32],
    "williams lake":        [-122.14, 52.14],

    // ── Named sales regions (from RegionName column) ──
    "wholesale house":           [-123.11, 49.27],   // company internal → Vancouver
    "jason house":          [-123.11, 49.27],
    "d2c":                  [-123.11, 49.27],
    "alberta":              [-113.49, 53.55],
    "crossfit north van":   [-123.07, 49.32],
    "rebel fitness squam":  [-123.14, 49.71],

    // ── Other Canadian cities (fallback) ──
    "toronto":              [-79.38,  43.70],
    "montreal":             [-73.56,  45.50],
    "calgary":              [-114.07, 51.05],
    "edmonton":             [-113.49, 53.55],
    "winnipeg":             [-97.13,  49.89],
    "ottawa":               [-75.70,  45.42],

    // ── Generic fallbacks ──
    "canada":               [-123.11, 49.27],   // default to Vancouver (all data is BC)
  };

  // Fuzzy territory/city → [lng, lat]: always returns a valid coord (never null)
  const _coordForTerritory = (territoryName) => {
    if (!territoryName) return [-123.11, 49.27]; // default: Vancouver (all data is BC)
    const lower = String(territoryName).toLowerCase().trim();
    if (!lower) return [-123.11, 49.27];
    if (_TERRITORY_CENTROIDS_RAW[lower]) return _TERRITORY_CENTROIDS_RAW[lower];
    for (const [key, coord] of Object.entries(_TERRITORY_CENTROIDS_RAW)) {
      if (lower.includes(key) || key.includes(lower)) return coord;
    }
    return [-123.11, 49.27]; // fallback: Vancouver (all customers are BC)
  };

  // ── Basemap ──
  //
  // State outlines from a vendored GeoJSON, not raster tiles from a CDN.
  //
  // The tiles were the last third-party dependency left in the build, and the
  // failure mode was not "a plainer map": MapLibre holds `style.load` until its
  // sources settle, the account layers are added *in* `style.load`, so a blocked
  // tile server produced an empty grey panel with no bubbles at all. Verified -
  // that is what this page did in a sandboxed browser.
  //
  // Outlines also read better here than streets and terrain do. The question
  // this panel answers is which accounts sit where, and a photographic basemap
  // competes with the bubbles for attention while adding nothing to it.
  const _baseMapStyle = () => {
    const css = getComputedStyle(document.documentElement);
    const pick = (name, fallback) => (css.getPropertyValue(name) || "").trim() || fallback;
    const host = document.getElementById("srLiveMap");
    const url = (host && host.dataset.basemapUrl) || "";
    const style = {
      version: 8,
      sources: {},
      layers: [
        { id: "ground", type: "background", paint: { "background-color": pick("--wa-surface-2", "#f4f7fa") } },
      ],
    };
    if (!url) return style;
    style.sources.states = { type: "geojson", data: url };
    style.layers.push(
      {
        id: "states-fill",
        type: "fill",
        source: "states",
        paint: { "fill-color": pick("--wa-surface", "#ffffff"), "fill-opacity": 0.92 },
      },
      {
        id: "states-line",
        type: "line",
        source: "states",
        paint: { "line-color": pick("--wa-hairline", "#d6dde5"), "line-width": 1 },
      },
    );
    return style;
  };

  // Kept under the old name so the style-fallback path below, which predates
  // this change, still resolves to the basemap the map actually uses.
  const _fallbackLightRasterStyle = _baseMapStyle;

  const _stableHash = (value) => {
    const raw = String(value || "").trim().toLowerCase();
    let hash = 0;
    for (let idx = 0; idx < raw.length; idx += 1) {
      hash = ((hash << 5) - hash + raw.charCodeAt(idx)) >>> 0;
    }
    return hash >>> 0;
  };

  const _validCoordinatePair = (latValue, lngValue) => {
    const lat = Number(latValue);
    const lng = Number(lngValue);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) return null;
    if (Math.abs(lat) < 0.0001 && Math.abs(lng) < 0.0001) return null;
    if (lat < -90 || lat > 90 || lng < -180 || lng > 180) return null;
    return [lng, lat];
  };

  /* The accounts the map plots.
   *
   * `map_customers` is a section the bundle declares and never fills - no SELECT
   * emits the `map_customer` dataset - so it is always `[]`. And `[]` is truthy,
   * which is why `map_customers || top_customers` never reached the fallback and
   * the map drew nothing at all: a style with twelve layers, a source with zero
   * features. Ask for length, not truthiness. */
  const _mapRows = (payload = {}) => {
    const analysis = payload.analysis || {};
    const mapped = Array.isArray(analysis.map_customers) ? analysis.map_customers : [];
    if (mapped.length) return mapped;
    return Array.isArray(analysis.top_customers) ? analysis.top_customers : [];
  };

  const _resolvedCoordinate = (row = {}) => {
    const actual = _validCoordinatePair(
      row.delivery_lat ?? row.lat ?? row.latitude,
      row.delivery_lng ?? row.delivery_long ?? row.lng ?? row.longitude,
    );
    if (actual) return { coordinates: actual, approx: false, approx_reason: "" };

    // The synthetic dataset carries no delivery coordinates, so in practice
    // every account lands here. City first, then region: an account placed on
    // its region's centroid is honest about the precision it has, and the
    // legend says so.
    const city = cleanText(row.delivery_city || row.city || "").toLowerCase();
    const territory = cleanText(row.territory_name || row.text_1 || "").toLowerCase();
    const province = cleanText(row.delivery_province || row.province || "").toLowerCase();

    let centroid = null;
    let reason = "network_centroid";

    if (city && _TERRITORY_CENTROIDS_RAW[city]) {
      centroid = _TERRITORY_CENTROIDS_RAW[city];
      reason = "city_centroid";
    } else if (territory && _TERRITORY_CENTROIDS_RAW[territory]) {
      centroid = _TERRITORY_CENTROIDS_RAW[territory];
      reason = "territory_centroid";
    } else if (province && _TERRITORY_CENTROIDS_RAW[province]) {
      centroid = _TERRITORY_CENTROIDS_RAW[province];
      reason = "province_centroid";
    }

    if (!centroid) centroid = MAP_DEFAULT_VIEW.center;

    // Accounts that resolve to the same centroid would stack into one bubble.
    // Scatter them deterministically around it - wide enough to read as a
    // cluster of separate accounts, tight enough to stay inside the region.
    const hash = _stableHash(row.customer_id || row.key || row.customer_name || JSON.stringify(row));
    const angle = ((hash % 360) * Math.PI) / 180;
    const ring = 1 + ((hash >> 9) % 4);
    const radius = 0.34 * ring;
    const lngOffset = Math.cos(angle) * radius;
    const latOffset = Math.sin(angle) * radius * 0.68;
    
    return {
      coordinates: [centroid[0] + lngOffset, centroid[1] + latOffset],
      approx: true,
      approx_reason: reason,
    };
  };

  const _focusedMapFeature = (features = _lastMapFeatures) => {
    const requested = pendingCustomerFocus || focusedCustomer;
    const targetKey = customerFocusKey(requested);
    if (!targetKey || !Array.isArray(features)) return null;
    return features.find((feature) => String(feature?.properties?.focus_key || "") === targetKey) || null;
  };

  const focusMapOnCustomer = (row, { zoom = 14, duration = 950 } = {}) => {
    if (!_liveMap) return false;
    const feature = _focusedMapFeature(_lastMapFeatures) || _lastMapFeatures.find((item) => String(item?.properties?.focus_key || "") === customerFocusKey(row));
    const center = feature?.geometry?.coordinates || _resolvedCoordinate(row).coordinates;
    if (!Array.isArray(center) || center.length !== 2) return false;
    try {
      if (_liveMap.resize) _liveMap.resize();
      _liveMap.flyTo({ center, zoom, duration, essential: true });
      return true;
    } catch (_err) {
      return false;
    }
  };

  const _fitMapToFeatures = (features = []) => {
    if (!_liveMap || !_mapStyleReady || !window.maplibregl) return;
    if (!Array.isArray(features) || !features.length) {
      _liveMap.easeTo({ center: MAP_DEFAULT_VIEW.center, zoom: MAP_DEFAULT_VIEW.zoom, duration: 800 });
      return;
    }
    const focusedFeature = _focusedMapFeature(features);
    if (focusedFeature) {
      _liveMap.flyTo({ center: focusedFeature.geometry.coordinates, zoom: 14, duration: 900, essential: true });
      return;
    }
    if (features.length === 1) {
      _liveMap.easeTo({ center: features[0].geometry.coordinates, zoom: 11.2, duration: 800 });
      return;
    }
    const bounds = new window.maplibregl.LngLatBounds(features[0].geometry.coordinates, features[0].geometry.coordinates);
    features.forEach((feature) => bounds.extend(feature.geometry.coordinates));
    _liveMap.fitBounds(bounds, {
      padding: { top: 72, right: 72, bottom: 72, left: 72 },
      maxZoom: 10.8,
      duration: 900,
    });
  };

  const _buildCustomerFeatures = (customers = []) => {
    // Fix the palette against the whole roster before any point is coloured,
    // so a rep keeps the same colour whichever accounts happen to be in scope.
    _assignRepColors((customers || []).map((row) => row.account_owner_name || row.owner_name));
    const totalRev = Math.max((customers || []).reduce((sum, row) => sum + num(row.revenue), 0), 1);
    const features = (customers || []).map((row) => {
      const silentDays = customerSilentDays(row) ?? (Number(row.silent_days) || 0);
      const revenue = num(row.revenue);
      const yoyDelta = opt(row.yoy_delta_revenue);
      const yoyRevenue = opt(row.yoy_revenue) ?? (yoyDelta === null ? null : Math.max(revenue - yoyDelta, 0));
      
      // Use precise coordinates from backend if available (metric_12/13)
      let location = _resolvedCoordinate(row);
      if (num(row.metric_12) !== 0 && num(row.metric_13) !== 0) {
        location = { coordinates: [num(row.metric_13), num(row.metric_12)], approx: false, approx_reason: "" };
      }

      const revShare = revenue / totalRev;
      const radius = Math.max(6, Math.min(38, Math.sqrt(revShare) * 140));
      return {
        type: "Feature",
        properties: {
          id: row.customer_id || row.key || "",
          focus_key: customerFocusKey(row),
          name: row.customer_name || row.customer_id || "Customer",
          owner_name: row.account_owner_name || "",
          territory: row.territory_name || "",
          city: row.delivery_city || "",
          province: row.delivery_province || "",
          shipping_method: row.shipping_method || "",
          revenue,
          profit: opt(row.profit),
          yoy_revenue: yoyRevenue,
          fresh_revenue: num(row.fresh_revenue || row.metric_7),
          consumables_revenue: num(row.consumables_revenue || row.metric_8),
          gm_revenue: num(row.gm_revenue || row.metric_9),
          is_overdue: Number(row.is_overdue || row.metric_10 || 0),
          silent_days: silentDays,
          avg_days: num(row.avg_days_between_orders || row.metric_11),
          opportunity_score: num(row.opportunity_score || row.metric_14 || 0),
          opportunity_reasons: row.opportunity_reasons || "",
          historical_proteins: row.historical_proteins || [],
          is_lost: Number(row.is_lost ?? 0),
          approx: location.approx ? 1 : 0,
          approx_reason: location.approx_reason || "",
          // The ring promises one thing in the UI: no order in 30 days. The
          // bundle's legacy `is_overdue` flag is evaluated against the server
          // wall clock, which ages a fixed demo snapshot forever and once put
          // a red halo around nearly every account. Use the cutoff-aware age.
          is_risk: silentDays > MAP_SILENT_RISK_DAYS ? 1 : 0,
          radius,
          color: _repColor(row.account_owner_name),
          mom_pct: opt(row.mom_revenue_pct ?? row.vs_prior_pct),
          last_order_date: row.last_order_date || "",
        },
        geometry: { type: "Point", coordinates: location.coordinates },
      };
    });
    _lastMapFeatures = features;
    return features;
  };

  const _pushMapData = (features) => {
    if (!_liveMap || !_mapStyleReady) return;
    [
      "customers-risk-halo", 
      "customers-bubbles", 
      "customers-heatmap", 
      "opportunity-heatmap", 
      "risk-heatmap",
      "fresh-heatmap",
      "consumables-heatmap",
      "gm-heatmap",
      "customers-risk-clusters"
    ].forEach((id) => {
      if (_liveMap.getLayer(id)) _liveMap.removeLayer(id);
    });
    if (_liveMap.getSource("customers")) _liveMap.removeSource("customers");

    _liveMap.addSource("customers", {
      type: "geojson",
      data: { type: "FeatureCollection", features: Array.isArray(features) ? features : [] },
      cluster: false, // User requested no clustering for full visibility
    });

    // 1. Revenue Heatmap Layer
    _liveMap.addLayer({
      id: "customers-heatmap",
      type: "heatmap",
      source: "customers",
      paint: {
        "heatmap-weight": ["interpolate", ["linear"], ["get", "revenue"], 0, 0, 50000, 1],
        "heatmap-intensity": ["interpolate", ["linear"], ["zoom"], 0, 1, 9, 3],
        "heatmap-color": [
          "interpolate", ["linear"], ["heatmap-density"],
          0, "rgba(33,102,172,0)",
          0.2, "rgb(103,169,207)",
          0.4, "rgb(209,229,240)",
          0.6, "rgb(253,219,199)",
          0.8, "rgb(239,138,98)",
          1, "rgb(178,24,43)"
        ],
        "heatmap-radius": ["interpolate", ["linear"], ["zoom"], 0, 2, 9, 20],
        "heatmap-opacity": ["interpolate", ["linear"], ["zoom"], 10, 0.8, 13, 0],
      },
      layout: { visibility: "none" }
    });

    // 2. Opportunity Heatmap Layer (Optimized Green)
    _liveMap.addLayer({
      id: "opportunity-heatmap",
      type: "heatmap",
      source: "customers",
      paint: {
        "heatmap-weight": ["interpolate", ["linear"], ["get", "opportunity_score"], 0, 0, 60, 1],
        "heatmap-intensity": ["interpolate", ["linear"], ["zoom"], 0, 1, 9, 3],
        "heatmap-color": [
          "interpolate", ["linear"], ["heatmap-density"],
          0, "rgba(0,0,0,0)",
          0.1, "#f7fcf5",
          0.3, "#e5f5e0",
          0.5, "#a1d99b",
          0.7, "#41ab5d",
          0.9, "#006d2c"
        ],
        "heatmap-radius": ["interpolate", ["linear"], ["zoom"], 0, 2, 9, 30],
        "heatmap-opacity": 0.85,
      },
      layout: { visibility: "none" }
    });

    // 3. Risk Heatmap Layer (Optimized Red)
    _liveMap.addLayer({
      id: "risk-heatmap",
      type: "heatmap",
      source: "customers",
      paint: {
        "heatmap-weight": ["interpolate", ["linear"], ["get", "silent_days"], 0, 0, 90, 1],
        "heatmap-intensity": ["interpolate", ["linear"], ["zoom"], 0, 1, 9, 3],
        "heatmap-color": [
          "interpolate", ["linear"], ["heatmap-density"],
          0, "rgba(0,0,0,0)",
          0.1, "#fff5f0",
          0.3, "#fee0d2",
          0.5, "#fc9272",
          0.7, "#ef3b2c",
          0.9, "#99000d"
        ],
        "heatmap-radius": ["interpolate", ["linear"], ["zoom"], 0, 2, 9, 30],
        "heatmap-opacity": 0.85,
      },
      layout: { visibility: "none" }
    });

    // 4. Fresh penetration heatmap
    _liveMap.addLayer({
      id: "fresh-heatmap",
      type: "heatmap",
      source: "customers",
      paint: {
        "heatmap-weight": ["interpolate", ["linear"], ["get", "fresh_revenue"], 0, 0, 10000, 1],
        "heatmap-color": ["interpolate", ["linear"], ["heatmap-density"], 0, "rgba(0,0,0,0)", 0.2, "#fee2e2", 0.5, "#ef4444", 0.9, "#7f1d1d"],
        "heatmap-radius": ["interpolate", ["linear"], ["zoom"], 0, 2, 9, 30],
      },
      layout: { visibility: "none" }
    });

    // 5. Consumables penetration heatmap
    _liveMap.addLayer({
      id: "consumables-heatmap",
      type: "heatmap",
      source: "customers",
      paint: {
        "heatmap-weight": ["interpolate", ["linear"], ["get", "consumables_revenue"], 0, 0, 5000, 1],
        "heatmap-color": ["interpolate", ["linear"], ["heatmap-density"], 0, "rgba(0,0,0,0)", 0.2, "#f0fdf4", 0.5, "#22c55e", 0.9, "#14532d"],
        "heatmap-radius": ["interpolate", ["linear"], ["zoom"], 0, 2, 9, 30],
      },
      layout: { visibility: "none" }
    });

    // 6. General merchandise penetration heatmap
    _liveMap.addLayer({
      id: "gm-heatmap",
      type: "heatmap",
      source: "customers",
      paint: {
        "heatmap-weight": ["interpolate", ["linear"], ["get", "gm_revenue"], 0, 0, 5000, 1],
        "heatmap-color": ["interpolate", ["linear"], ["heatmap-density"], 0, "rgba(0,0,0,0)", 0.2, "#fff7ed", 0.5, "#f97316", 0.9, "#7c2d12"],
        "heatmap-radius": ["interpolate", ["linear"], ["zoom"], 0, 2, 9, 30],
      },
      layout: { visibility: "none" }
    });

    _liveMap.addLayer({
      id: "customers-lost-halo",
      type: "circle",
      source: "customers",
      filter: ["==", ["get", "is_lost"], 1],
      paint: {
        "circle-radius": ["*", ["coalesce", ["get", "radius"], 6], 2.2],
        "circle-color": "#64748b", // Slate grey for lost
        "circle-opacity": 0,
        "circle-stroke-width": 2,
        "circle-stroke-color": "#64748b",
        "circle-stroke-opacity": 0.6,
      },
    });

    _liveMap.addLayer({
      id: "customers-risk-halo",
      type: "circle",
      source: "customers",
      filter: ["==", ["get", "is_risk"], 1],
      paint: {
        "circle-radius": ["*", ["coalesce", ["get", "radius"], 6], 1.7],
        "circle-color": SR_THEME.blood,
        "circle-opacity": 0,
        "circle-stroke-width": 3,
        "circle-stroke-color": SR_THEME.blood,
        "circle-stroke-opacity": 0.42,
      },
    });

    _liveMap.addLayer({
      id: "customers-bubbles",
      type: "circle",
      source: "customers",
      paint: {
        "circle-radius": ["coalesce", ["get", "radius"], 6],
        "circle-color": ["coalesce", ["get", "color"], "#CCCCCC"],
        "circle-opacity": [
          "interpolate", ["linear"], ["zoom"],
          3, ["case", ["==", ["get", "approx"], 1], 0.62, 0.95],
          10, ["case", ["==", ["get", "approx"], 1], 0.85, 1.0],
        ],
        "circle-stroke-width": ["interpolate", ["linear"], ["zoom"], 10, 2, 15, 3],
        "circle-stroke-color": "#1E293B", 
        "circle-stroke-opacity": 0.9,
      },
    });

    _animateHalo();
    _fitMapToFeatures(features);
    
    // Maintain current mode
    const activeMode = document.querySelector('input[name="srMapMode"]:checked')?.value || "bubbles";
    _updateMapMode(activeMode);
    // A pulsing halo keeps repainting, so MapLibre's global `idle` event is not
    // a dependable freeze signal. Mark the host after the first complete frame
    // with the account layers; the static build can then capture real pixels.
    const mapHost = document.getElementById("srLiveMap");
    _liveMap.once("render", () => { if (mapHost) mapHost.dataset.waMapIdle = "1"; });
    _liveMap.triggerRepaint();
  };

  const _animateHalo = () => {
    if (!_liveMap || !_mapStyleReady || !_liveMap.getLayer("customers-risk-halo")) {
      _mapAnimationId = null;
      return;
    }
    if (_mapAnimationId) cancelAnimationFrame(_mapAnimationId);
    const step = (Date.now() % 2200) / 2200;
    const opacity = 0.2 + (0.58 * Math.sin(step * Math.PI));
    const radiusScale = 1.34 + (0.4 * Math.sin(step * Math.PI));
    try {
      if (_liveMap.getLayer("customers-risk-halo")) {
        _liveMap.setPaintProperty("customers-risk-halo", "circle-stroke-opacity", opacity);
        _liveMap.setPaintProperty("customers-risk-halo", "circle-radius", ["*", ["get", "radius"], radiusScale]);
      }
    } catch (_err) {
      /* ignore transient style swaps */
    }
    _mapAnimationId = requestAnimationFrame(_animateHalo);
  };

  // ── Map legend hover: highlight matching rep bubbles, dim others ──
  const _highlightRepOnMap = (repName) => {
    if (!_liveMap || !_mapStyleReady) return;
    try {
      _liveMap.setPaintProperty("customers-bubbles", "circle-opacity", [
        "case",
        ["==", ["get", "owner_name"], repName], 1.0,
        0.12,
      ]);
      _liveMap.setPaintProperty("customers-bubbles", "circle-radius", [
        "case",
        ["==", ["get", "owner_name"], repName],
        ["*", ["get", "radius"], 1.35],
        ["get", "radius"],
      ]);
    } catch (_err) { /* transient paint failures are non-critical */ }
  };

  const _clearMapRepHighlight = () => {
    if (!_liveMap || !_mapStyleReady) return;
    try {
      _liveMap.setPaintProperty("customers-bubbles", "circle-opacity", [
        "interpolate", ["linear"], ["zoom"],
        3, ["case", ["==", ["get", "approx"], 1], 0.62, 0.95],
        8, ["case", ["==", ["get", "approx"], 1], 0.78, 1.0],
      ]);
      _liveMap.setPaintProperty("customers-bubbles", "circle-radius", ["get", "radius"]);
    } catch (_err) { /* ignore */ }
  };

  const _buildMapLegend = (customers = [], mode = "bubbles") => {
    const legendEl = document.getElementById("srMapLegend");
    if (!legendEl) return;

    if (mode === "opportunity") {
      legendEl.innerHTML = `
        <span class="sr-map-legend-item">
          <span class="sr-map-legend-gradient" style="background:linear-gradient(to right, rgb(232,245,233), rgb(27,94,32))"></span>
          Opportunity Score (High = Dark Green)
        </span>
      `;
      return;
    }

    if (mode === "risk") {
      legendEl.innerHTML = `
        <span class="sr-map-legend-item">
          <span class="sr-map-legend-gradient" style="background:linear-gradient(to right, rgb(255,235,238), rgb(183,28,28))"></span>
          Risk Intensity (High = Dark Red)
        </span>
      `;
      return;
    }

    const reps = [...new Set((customers || []).map((row) => row.account_owner_name || row.owner_name).filter(Boolean))].slice(0, 8);
    const approxCount = (customers || []).filter((row) => row.approx === 1).length;
    const riskCount = (customers || []).filter((row) => (row.silent_days ?? 0) > MAP_SILENT_RISK_DAYS).length;
    legendEl.innerHTML = [
      ...reps.map((rep) => `
        <span class="sr-map-legend-item sr-map-legend-rep" data-rep-name="${escapeHtml(rep)}" role="button" tabindex="0" aria-label="Highlight ${escapeHtml(rep)} on map">
          <span class="sr-map-legend-dot" style="background:${_repColor(rep)}"></span>
          ${escapeHtml(rep)}
        </span>
      `),
      riskCount > 0
        ? `<span class="sr-map-legend-item sr-map-legend-risk">
            <span class="sr-map-legend-dot" style="border:2px solid ${SR_THEME.blood};background:transparent"></span>
            ${fmtInt.format(riskCount)} silent&nbsp;&gt;${MAP_SILENT_RISK_DAYS}d
          </span>`
        : "",
      approxCount > 0
        ? `<span class="sr-map-legend-item sr-map-legend-approx">
            <span class="sr-map-legend-dot" style="background:${SR_THEME.bronze};opacity:0.58"></span>
            ${fmtInt.format(approxCount)} centroid fallback
          </span>`
        : "",
    ].filter(Boolean).join("");

    // Bind hover/focus events for rep highlight
    legendEl.querySelectorAll(".sr-map-legend-rep").forEach((item) => {
      const repName = item.dataset.repName;
      item.addEventListener("mouseenter", () => _highlightRepOnMap(repName));
      item.addEventListener("mouseleave", () => _clearMapRepHighlight());
      item.addEventListener("focus", () => _highlightRepOnMap(repName));
      item.addEventListener("blur", () => _clearMapRepHighlight());
      item.addEventListener("keydown", (evt) => { if (evt.key === "Enter" || evt.key === " ") _highlightRepOnMap(repName); });
    });
  };

  const _scheduleMapStyleFallback = () => {
    if (_mapStyleFallbackTimer) window.clearTimeout(_mapStyleFallbackTimer);
    _mapStyleFallbackTimer = window.setTimeout(() => {
      if (!_liveMap || _mapStyleReady) return;
      try {
        _liveMap.setStyle(_fallbackLightRasterStyle());
      } catch (err) {
        logWarn("Live map style fallback failed", err);
      }
    }, 3500);
  };

  const _updateMapMode = (mode) => {
    if (!_liveMap || !_mapStyleReady) return;
    const layers = [
      "customers-bubbles", 
      "customers-risk-halo", 
      "customers-lost-halo",
      "customers-heatmap", 
      "opportunity-heatmap", 
      "risk-heatmap",
      "fresh-heatmap",
      "consumables-heatmap",
      "gm-heatmap"
    ];
    layers.forEach(id => {
      if (_liveMap.getLayer(id)) {
        let visible = false;
        if (mode === "bubbles") {
          visible = (id === "customers-bubbles" || id === "customers-risk-halo" || id === "customers-lost-halo");
        } else if (mode === "opportunity") {
          visible = (id === "opportunity-heatmap");
        } else if (mode === "risk") {
          visible = (id === "risk-heatmap");
        } else if (mode === "fresh") {
          visible = (id === "fresh-heatmap");
        } else if (mode === "consumables") {
          visible = (id === "consumables-heatmap");
        } else if (mode === "gm") {
          visible = (id === "gm-heatmap");
        } else if (mode === "hybrid") {
          visible = (id === "opportunity-heatmap" || id === "customers-bubbles");
        }
        _liveMap.setLayoutProperty(id, "visibility", visible ? "visible" : "none");
      }
    });
    _buildMapLegend(_lastMapFeatures?.map(f => f.properties) || [], mode);
  };

  const wireMapModes = () => {
    document.querySelectorAll('input[name="srMapMode"]').forEach(input => {
      input.addEventListener("change", (evt) => {
        _updateMapMode(evt.target.value);
      });
    });
  };

  const _flyToRep = (repName) => {
    if (!_liveMap || !_mapStyleReady || !repName) return;
    const features = _lastMapFeatures.filter(f => f.properties.owner_name === repName);
    if (!features.length) return;
    
    _fitMapToFeatures(features);
  };

  const dispatchFiltersForTerritory = (territory) => {
    const territoryName = cleanText(territory);
    if (!territoryName) return;
    dispatchPageFilters(
      {
        ...currentFilterState(),
        regions: [territoryName],
      },
      "map_territory",
    );
  };

  const _rowFromFeatureProps = (props = {}) => ({
    customer_id: props.id || null,
    customer_name: props.name || TEXT_EMPTY,
    account_owner_name: props.owner_name || TEXT_EMPTY,
    territory_name: props.territory || TEXT_EMPTY,
    delivery_city: props.city || "",
    delivery_province: props.province || "",
    shipping_method: props.shipping_method || "",
    revenue: opt(props.revenue),
    days_since_order: props.silent_days == null ? null : Number(props.silent_days),
    last_order_date: props.last_order_date || "",
    mom_revenue_pct: props.mom_pct == null || props.mom_pct === "null" ? null : Number(props.mom_pct),
  });

  const _initMapOnce = () => {
    const mapEl = document.getElementById("srLiveMap");
    if (!mapEl || _liveMap) return;
    _liveMap = new window.maplibregl.Map({
      container: "srLiveMap",
      style: _fallbackLightRasterStyle(),
      center: MAP_DEFAULT_VIEW.center,
      zoom: MAP_DEFAULT_VIEW.zoom,
      // Latitudes 40-75 is Canada, left over from before the retail pivot. The
      // store network runs from Houston at 29.8°N to Spokane at 47.7°N, so the
      // old box excluded most of it *and* sat north of the default centre at
      // 38.5°N - which MapLibre resolves by clamping the view away from the
      // data. Bound the continental US with a margin instead.
      maxBounds: [[-130, 21], [-63, 53]],
      // The static build replaces this canvas with a picture of itself, and a
      // WebGL canvas reads back blank unless the drawing buffer is kept. Every
      // frozen page would otherwise publish an empty grey panel.
      preserveDrawingBuffer: true,
    });

    // Keep `idle` as a second completion signal for non-animated states. The
    // account-layer render callback in `_pushMapData` is the deterministic
    // signal used for the initial static capture.
    mapEl._waMap = _liveMap;
    _liveMap.on("idle", () => { mapEl.dataset.waMapIdle = "1"; });
    _liveMap.on("movestart", () => { delete mapEl.dataset.waMapIdle; });

    _liveMap.addControl(new window.maplibregl.NavigationControl({ showCompass: false }), "top-right");
    _liveMap.addControl(new window.maplibregl.ScaleControl({ maxWidth: 100, unit: "metric" }), "bottom-left");

    _mapPopup = new window.maplibregl.Popup({
      closeButton: true,
      closeOnClick: false,
      maxWidth: "280px",
      className: "sr-map-popup",
    });

    _scheduleMapStyleFallback();

    _liveMap.on("style.load", () => {
      _mapStyleReady = true;
      if (_mapStyleFallbackTimer) {
        window.clearTimeout(_mapStyleFallbackTimer);
        _mapStyleFallbackTimer = null;
      }
      _liveMap.resize();
      if (_pendingMapPayload) {
        const payload = _pendingMapPayload;
        const customers = _mapRows(payload);
        const features = _buildCustomerFeatures(customers);
        _pushMapData(features);
        _buildMapLegend(customers, document.querySelector('input[name="srMapMode"]:checked')?.value || "bubbles");
        _pendingMapPayload = null;
      } else if (_lastMapFeatures.length) {
        _pushMapData(_lastMapFeatures);
      }
    });

    _liveMap.on("error", (evt) => {
      if (!_mapStyleReady) logWarn("Live map style error", evt?.error || evt);
    });

    _liveMap.on("mouseenter", "customers-bubbles", (evt) => {
      _liveMap.getCanvas().style.cursor = "pointer";
      const props = evt.features?.[0]?.properties || {};
      const silentDays = Number(props.silent_days || 0);
      const silentColor = silentDays > 60 ? SR_THEME.blood : silentDays > MAP_SILENT_RISK_DAYS ? SR_THEME.bronze : SR_THEME.forest;
      
      const revenue = num(props.revenue);
      const profit = num(props.profit);
      const margin = revenue > 0 ? (profit / revenue) * 100 : 0;
      
      const momPct = props.mom_pct;
      const momStr = (momPct != null && momPct !== "null")
        ? `<div class="sr-map-popup-row">MoM Velocity: <b style="color:${Number(momPct) >= 0 ? SR_THEME.forest : SR_THEME.blood}">${Number(momPct) >= 0 ? "+" : ""}${fmtPct.format(Number(momPct))}%</b></div>`
        : "";
      
      // Calculate YoY if available
      const yoyRev = num(props.yoy_revenue);
      const yoyStr = (yoyRev > 0)
        ? `<div class="sr-map-popup-row">YoY Growth: <b style="color:${revenue >= yoyRev ? SR_THEME.forest : SR_THEME.blood}">${revenue >= yoyRev ? "+" : ""}${fmtPct.format(((revenue - yoyRev) / yoyRev) * 100)}%</b></div>`
        : "";

      const lastOrder = props.last_order_date ? formatDateCA(props.last_order_date) : "Never";
      
      const riskBadge = Number(props.is_risk) === 1
        ? '<span class="sr-map-popup-risk-badge">&#9888; SILENT RISK</span>'
        : "";
      
      const overdueDetail = Number(props.is_risk) === 1 && Number(props.is_overdue) === 1
        ? `<div class="sr-map-popup-row text-danger small fw-bold">Pulse Missed: ${fmtInt.format(num(props.avg_days))}d expected cycle</div>`
        : "";

      const proteins = Array.isArray(props.historical_proteins) ? props.historical_proteins : (props.historical_proteins || "").split(",").filter(Boolean);
      const proteinHtml = proteins.length
        ? `<div class="sr-map-popup-row text-muted" style="font-size:0.68rem">Core Species: ${proteins.slice(0,3).join(", ")}</div>`
        : "";

      const lostBadge = Number(props.is_lost) === 1
        ? `<span class="sr-map-popup-risk-badge" style="background:#64748b;color:white">LOST ACCOUNT</span>`
        : "";
      
      const oppScore = Number(props.opportunity_score || 0);
      const oppReasons = (props.opportunity_reasons || "").replace(/;\s*$/, "");
      const oppBadge = oppScore > 40
        ? `<span class="sr-map-popup-risk-badge" style="background:${SR_THEME.forest};color:white" title="${escapeHtml(oppReasons)}">HIGH OPPORTUNITY (${oppScore})</span>`
        : oppScore > 20
          ? `<span class="sr-map-popup-risk-badge" style="background:${SR_THEME.bronze};color:white" title="${escapeHtml(oppReasons)}">MID OPPORTUNITY (${oppScore})</span>`
          : "";

      _mapPopup
        .setLngLat(evt.lngLat)
        .setHTML(`
          <div class="sr-map-popup-inner">
            <div class="sr-map-popup-header">
              <div class="sr-map-popup-name">${escapeHtml(props.name || "Unknown Customer")}</div>
              <div class="sr-map-popup-meta">
                ${escapeHtml(props.owner_name || "Unassigned")} &middot; ${escapeHtml(props.territory || "No Territory")}
              </div>
            </div>
            <div class="sr-map-popup-divider"></div>
            <div class="sr-map-popup-stats">
              <div class="sr-map-popup-row">Window Revenue: <b>${money(revenue)}</b></div>
              <div class="sr-map-popup-row">Gross Margin: <b style="color:${margin >= 25 ? SR_THEME.forest : SR_THEME.blood}">${fmtPct.format(margin)}%</b></div>
              ${momStr}
              ${yoyStr}
              <div class="sr-map-popup-row">Last Order: <b>${lastOrder}</b> (${fmtInt.format(silentDays)}d ago)</div>
              ${overdueDetail}
              ${proteinHtml}
              ${props.shipping_method ? `<div class="sr-map-popup-row text-muted" style="font-size:0.7rem">Method: ${escapeHtml(props.shipping_method)}</div>` : ""}
            </div>
            <div class="mt-2 d-flex flex-wrap gap-1">
              ${riskBadge}${lostBadge}${oppBadge}
            </div>
            ${Number(props.approx) === 1 ? `<div class="sr-map-popup-approx mt-1" style="font-size:0.65rem;color:${SR_THEME.bronze}">&#9432; Location fallback active</div>` : ""}
            <div class="sr-map-popup-footer mt-2" style="font-size:0.68rem;color:${SR_THEME.brand};border-top:1px solid rgba(0,0,0,0.05);padding-top:4px">
              Click bubble to focus this account
            </div>
          </div>`)
        .addTo(_liveMap);
        
      if (evt.features.length > 0) {
        _liveMap.setPaintProperty('customers-bubbles', 'circle-stroke-width', [
          'case',
          ['==', ['get', 'id'], evt.features[0].properties.id],
          4,
          2
        ]);
        _liveMap.setPaintProperty('customers-bubbles', 'circle-stroke-color', [
          'case',
          ['==', ['get', 'id'], evt.features[0].properties.id],
          '#FBBF24',
          '#FFFFFF'
        ]);
      }
    });

    _liveMap.on("mouseleave", "customers-bubbles", () => {
      _liveMap.getCanvas().style.cursor = "";
      _mapPopup.remove();
      _liveMap.setPaintProperty('customers-bubbles', 'circle-stroke-width', 2);
      _liveMap.setPaintProperty('customers-bubbles', 'circle-stroke-color', '#FFFFFF');
    });

    _liveMap.on("click", "customers-bubbles", (evt) => {
      const props = evt.features?.[0]?.properties || {};
      _mapPopup.remove();
      const existingRow = findCustomerRow(_allCustomerRows, { customer_id: props.id, customer_name: props.name });
      focusCustomerFromPriority(existingRow || _rowFromFeatureProps(props));
    });

    document.getElementById("srMapResetBtn")?.addEventListener("click", () => {
      pendingCustomerFocus = null;
      // Clear territory and rep scope filters so all dependent widgets return to global view
      const current = currentFilterState();
      const hasRegions = Array.isArray(current.regions) && current.regions.length > 0;
      const hasSalesReps = Array.isArray(current.sales_reps) && current.sales_reps.length > 0;
      const hasCustomers = Array.isArray(current.customers) && current.customers.length > 0;
      if (hasRegions || hasSalesReps || hasCustomers) {
        dispatchPageFilters({ ...current, regions: [], sales_reps: [], customers: [] }, "map_reset");
      }
      // Reset map view to show all features
      if (_lastMapFeatures.length) {
        _fitMapToFeatures(_lastMapFeatures);
      } else {
        _liveMap?.easeTo({ center: MAP_DEFAULT_VIEW.center, zoom: MAP_DEFAULT_VIEW.zoom, duration: 800 });
      }
    });
  };

  const initLiveMap = (payload) => {
    const mapEl = document.getElementById("srLiveMap");
    const placeholder = document.getElementById("srMapPlaceholder");
    if (!mapEl) return;

    if (!window.maplibregl) {
      if (placeholder) placeholder.classList.add("active");
      mapEl.style.display = "none";
      
      if (!window._srMapPolling) {
        window._srMapPolling = true;
        let attempts = 0;
        const poll = setInterval(() => {
          attempts += 1;
          if (window.maplibregl) {
            clearInterval(poll);
            window._srMapPolling = false;
            initLiveMap(payload);
          } else if (attempts > 30) {
            clearInterval(poll);
            window._srMapPolling = false;
            logError("MapLibre GL failed to load after 30 attempts");
          }
        }, 200);
      }
      return;
    }

    if (placeholder) placeholder.classList.remove("active");
    mapEl.style.display = "block";
    mapEl.style.minHeight = "420px";

    const customers = _mapRows(payload);
    const features = _buildCustomerFeatures(customers);

    if (!_liveMap) {
      _pendingMapPayload = payload;
      _initMapOnce();
      _buildMapLegend(customers, document.querySelector('input[name="srMapMode"]:checked')?.value || "bubbles");
      return;
    }

    _liveMap.resize();
    if (_mapStyleReady) {
      _pushMapData(features);
      _buildMapLegend(customers, document.querySelector('input[name="srMapMode"]:checked')?.value || "bubbles");
    } else {
      _pendingMapPayload = payload;
    }
  };

  const bootstrap = async (qsHint) => {
    if (bootstrapped) return;
    bootstrapped = true;
    const detail = await waitForFiltersReady();
    const qs = (qsHint || detail?.qs || resolveInitialQS() || "").replace(/^\?/, "");
    state.qs = qs;
    syncStateFromQS(qs);
    syncFocusedReps(detail?.filters || currentFilterState());
    syncControlsFromState();
    applyColumnVisibility();
    wireTooltips();
    const snapshot = restoreSnapshot(qs, { restoreScroll: true });
    if (snapshot?.fresh) {
      updateExportLinks();
      return;
    }
    fetchBundle({ snapshot });
  };

  wireKpiSort();
  wireSorting();
  wirePager();
  wireVirtualTable();
  wireCustomerVirtualTable();
  wireRowClicks();
  wireMiniSorts();
  wireControls();
  wireCompare();
  wireMapModes();
  initCustomerViewToggle();
  wireFilterDrawer();
  wireFollowUpDrawer();

  document.getElementById("srGapFocusClear")?.addEventListener("click", (evt) => {
    evt.preventDefault();
    clearCustomerFocus();
  });

  window.addEventListener("resize", scheduleViewportHeightSync, { passive: true });

  window.addEventListener("globalFilters:apply", (evt) => {
    currentApplyId = String(evt?.detail?.applyId || "");
    applyFilters(evt?.detail?.qs || "", evt?.detail?.filters || null, { scroll: focusedRepIdsFromFilters(evt?.detail?.filters || {}).length > 0 });
  });
  window.addEventListener("globalFilters:applied", () => {
    closeFilterDrawer();
    if (!currentUrlFilters().customers.length && !pendingCustomerFocus && focusedCustomer) {
      setFocusedCustomer(null);
    }
  });
  window.addEventListener("globalFilters:ready", (evt) => bootstrap(evt?.detail?.qs || ""));
  window.addEventListener("pagehide", () => {
    persistSnapshot();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") persistSnapshot();
  });

  bootstrap();
})();
