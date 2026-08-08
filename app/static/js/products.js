(() => {
  const root = document.getElementById("products-main");
  if (!root) return;
  if (root.dataset.productsBootstrapped === "1") return;
  root.dataset.productsBootstrapped = "1";
  if (document?.body?.dataset) {
    document.body.dataset.filtersHandler = "ajax";
  }
  const authFetch = window.authFetch || fetch;
  const pageCache = window.analyticsPageCache || null;

  const bundleUrl = root.dataset.bundleUrl || "/api/products/bundle";
  const drilldownTemplate = root.dataset.drilldownTemplate || "";
  const currency = root.dataset.currency || "USD";
  const isV2 = root.dataset.productsV2 === "1";
  const isV3 = root.dataset.productsV3 === "1";
  const isV4 = root.dataset.productsV4 === "1";
  const PAGE_CACHE_ID = isV4 ? "products-v4-live4" : "products";
  const PAGE_CACHE_POLICY = { freshMs: 90 * 1000, maxAgeMs: 20 * 60 * 1000 };
  const WORKSPACE_STORAGE_KEY = isV4 ? "wholesale:products:v4:workspace" : "";
  const TABLE_PRESET_STORAGE_KEY = isV4 ? "wholesale:products:v4:table-preset" : "";
  const STAGED_ACTIONS_STORAGE_KEY = isV4 ? "wholesale:products:v4:staged-actions" : "";
  const COLUMN_STORAGE_KEY = isV4
    ? "wholesale:products:v4:columns"
    : isV3
      ? "wholesale:products:v3:columns"
      : "wholesale:products:v2:columns";
  const V2_COLUMN_DEFS = [
    { key: "sku", label: "SKU", locked: true, visible: true, exportable: true },
    { key: "product", label: "Product", locked: true, visible: true, exportable: true },
    { key: "segment", label: "Segment", visible: true, exportable: true },
    { key: "revenue", label: "Revenue", visible: true, exportable: true },
    { key: "revenue_share", label: "Rev Share", visible: true, exportable: true },
    { key: "orders", label: "Orders", visible: true, exportable: true },
    { key: "qty", label: "Quantity", visible: true, exportable: true },
    { key: "weight", label: "Weight", visible: false, exportable: true },
    { key: "current_unit_price", label: "Current Price", visible: true, exportable: true },
    { key: "target_price", label: "Target Price", visible: true, exportable: true },
    { key: "uplift_pct", label: "Uplift %", visible: true, exportable: true },
    { key: "cost", label: "Cost", visible: false, exportable: true },
    { key: "profit", label: "Profit", visible: true, exportable: true },
    { key: "profit_share", label: "Profit Share", visible: false, exportable: true },
    { key: "contribution_margin_lb", label: "CM / lb", visible: false, exportable: true },
    { key: "margin_pct", label: "Margin %", visible: true, exportable: true },
    { key: "price_variance_vs_median", label: "vs Median", visible: false, exportable: true },
    { key: "volatility_score", label: "Volatility", visible: false, exportable: true },
    { key: "margin_risk", label: "Margin Risk", visible: true, exportable: true },
    { key: "recommendation", label: "Recommendation", visible: true, exportable: true },
    { key: "first_sold", label: "First Sold", visible: false, exportable: true },
    { key: "last_sold", label: "Last Sold", visible: false, exportable: true },
    { key: "quick_rec", label: "Quick Rec", visible: false, exportable: true },
  ];
  const V3_COLUMN_DEFS = [
    { key: "sku", label: "SKU", locked: true, visible: true, exportable: true },
    { key: "product", label: "Product", locked: true, visible: true, exportable: true },
    { key: "segment", label: "Segment", visible: true, exportable: true },
    { key: "revenue", label: "Revenue", visible: true, exportable: true },
    { key: "revenue_current", label: "Revenue current", visible: true, exportable: true },
    { key: "revenue_prior", label: "Revenue prior", visible: true, exportable: true },
    { key: "revenue_delta", label: "Delta Revenue $", visible: true, exportable: true },
    { key: "revenue_delta_pct", label: "Delta Revenue %", visible: true, exportable: true },
    { key: "revenue_share", label: "Rev Share", visible: true, exportable: true },
    { key: "orders", label: "Orders", visible: true, exportable: true },
    { key: "orders_current", label: "Orders current", visible: false, exportable: true },
    { key: "orders_prior", label: "Orders prior", visible: false, exportable: true },
    { key: "qty", label: "Quantity", visible: true, exportable: true },
    { key: "weight", label: "Weight", visible: false, exportable: true },
    { key: "current_unit_price", label: "Current Price", visible: true, exportable: true },
    { key: "target_price", label: "Target Price", visible: false, exportable: true },
    { key: "uplift_pct", label: "Uplift %", visible: false, exportable: true },
    { key: "cost", label: "Cost", visible: false, exportable: true },
    { key: "profit", label: "Profit", visible: true, exportable: true },
    { key: "profit_current", label: "Profit current", visible: false, exportable: true },
    { key: "profit_prior", label: "Profit prior", visible: false, exportable: true },
    { key: "profit_delta", label: "Delta Profit $", visible: true, exportable: true },
    { key: "profit_share", label: "Profit Share", visible: false, exportable: true },
    { key: "contribution_margin_lb", label: "CM / lb", visible: false, exportable: true },
    { key: "margin_pct", label: "Margin %", visible: true, exportable: true },
    { key: "margin_pct_prior", label: "Margin % Prior", visible: false, exportable: true },
    { key: "margin_delta_pp", label: "Delta Margin pp", visible: true, exportable: true },
    { key: "price_variance_vs_median", label: "vs Median", visible: false, exportable: true },
    { key: "volatility_score", label: "Volatility", visible: false, exportable: true },
    { key: "margin_risk", label: "Margin Risk", visible: true, exportable: true },
    { key: "recommendation", label: "Recommendation", visible: true, exportable: true },
    { key: "first_sold", label: "First Sold", visible: false, exportable: true },
    { key: "last_sold", label: "Last Sold", visible: false, exportable: true },
    { key: "quick_rec", label: "Quick Rec", visible: false, exportable: true },
  ];
  const V4_COLUMN_DEFS = [
    { key: "sku", label: "SKU", locked: true, visible: true, exportable: true },
    { key: "product", label: "Product", locked: true, visible: true, exportable: true },
    { key: "protein_family", label: "Protein / Category", visible: true, exportable: true },
    { key: "product_category", label: "Category", visible: false, exportable: true },
    { key: "segment", label: "Segment", visible: true, exportable: true },
    { key: "supplier", label: "Supplier", visible: false, exportable: true },
    { key: "customer_count", label: "Customer Ct", visible: true, exportable: true },
    { key: "supplier_count", label: "Supplier Ct", visible: false, exportable: true },
    { key: "region_breadth", label: "Region Ct", visible: false, exportable: true },
    { key: "top_customer_share", label: "Top Cust Share", visible: true, exportable: true },
    { key: "customer_hhi", label: "Cust HHI", visible: false, exportable: true },
    { key: "revenue", label: "Revenue", visible: true, exportable: true },
    { key: "revenue_current", label: "Revenue current", visible: true, exportable: true },
    { key: "revenue_prior", label: "Revenue prior", visible: false, exportable: true },
    { key: "revenue_delta", label: "Delta Revenue $", visible: true, exportable: true },
    { key: "revenue_delta_pct", label: "Delta Revenue %", visible: true, exportable: true },
    { key: "revenue_share", label: "Rev Share", visible: true, exportable: true },
    { key: "orders", label: "Orders", visible: true, exportable: true },
    { key: "orders_current", label: "Orders current", visible: false, exportable: true },
    { key: "orders_prior", label: "Orders prior", visible: false, exportable: true },
    { key: "velocity_per_month", label: "Velocity/mo", visible: true, exportable: true },
    { key: "qty", label: "Quantity", visible: true, exportable: true },
    { key: "weight", label: "Weight", visible: false, exportable: true },
    { key: "current_unit_price", label: "Current Price", visible: true, exportable: true },
    { key: "minimum_price", label: "Minimum Price", visible: true, exportable: true },
    { key: "asp_lb", label: "ASP/lb", visible: true, exportable: true },
    { key: "asp_lb_gap_to_min", label: "ASP Gap to Min", visible: true, exportable: true },
    { key: "minimum_price_lb", label: "Minimum Price/lb", visible: true, exportable: true },
    { key: "target_price_lb", label: "Target Price/lb", visible: true, exportable: true },
    { key: "asp_lb_gap_to_target", label: "ASP Gap to Target", visible: true, exportable: true },
    { key: "cost_lb", label: "Effective Cost/lb", visible: true, exportable: true },
    { key: "target_price", label: "Target Price", visible: false, exportable: true },
    { key: "uplift_pct", label: "Uplift %", visible: false, exportable: true },
    { key: "cost", label: "Cost", visible: false, exportable: true },
    { key: "profit", label: "Profit", visible: true, exportable: true },
    { key: "profit_current", label: "Profit current", visible: false, exportable: true },
    { key: "profit_prior", label: "Profit prior", visible: false, exportable: true },
    { key: "profit_delta", label: "Delta Profit $", visible: true, exportable: true },
    { key: "profit_share", label: "Profit Share", visible: false, exportable: true },
    { key: "contribution_margin_lb", label: "Contribution/lb", visible: true, exportable: true },
    { key: "margin_pct", label: "Gross Margin %", visible: true, exportable: true },
    { key: "minimum_margin_pct", label: "Min Gross Margin %", visible: true, exportable: true },
    { key: "target_margin_pct", label: "Target Gross Margin %", visible: true, exportable: true },
    { key: "margin_pct_prior", label: "Margin % Prior", visible: false, exportable: true },
    { key: "margin_delta_pp", label: "Delta Margin pp", visible: true, exportable: true },
    { key: "price_variance_vs_median", label: "vs Median", visible: false, exportable: true },
    { key: "volatility_score", label: "Price CV %", visible: false, exportable: true },
    { key: "margin_risk", label: "Pricing Band", visible: true, exportable: true },
    { key: "recommendation", label: "Recommendation", visible: true, exportable: true },
    { key: "first_sold", label: "First Sold", visible: false, exportable: true },
    { key: "last_sold", label: "Last Sold", visible: false, exportable: true },
    { key: "quick_rec", label: "Quick Rec", visible: false, exportable: true },
  ];
  const ACTIVE_COLUMN_DEFS = isV4 ? V4_COLUMN_DEFS : (isV3 ? V3_COLUMN_DEFS : V2_COLUMN_DEFS);
  const V2_TOOLTIP_TEXT = {
    heroTitle: "This page keeps existing filters, RBAC scope, and exports intact while surfacing pricing, velocity, and margin actions in one place.",
    velocityPulse: "Velocity pulse summarizes weekly movement from the current window. Average weekly and weekly revenue are total filtered activity divided by elapsed weeks.",
    momentum: "Revenue comparison uses the active filtered window against its prior comparable window. Partial months are treated explicitly and not compared against a full prior month.",
    topProduct: "Top product is the highest-revenue SKU in the current window. Use it as a share anchor, not a recommendation on its own.",
    momDelta: "Comparison delta uses the current filtered window and its prior comparable window, with month-to-date safeguards when the latest month is incomplete.",
    projectedNextMonth: "Projected next month is pace-normalized when the current month is incomplete; otherwise it uses recent completed periods.",
    totalRevenue: "Total shipped revenue in the active filter window, after RBAC scope is applied.",
    totalQuantity: "Total shipped quantity in the active filter window. Quantity follows the same unit basis already used by the page.",
    totalWeight: "Total shipped weight in pounds for the filtered window when weight is available.",
    activeProducts: "Distinct SKUs with activity in the current filter window.",
    activeCustomers: "Distinct customers purchasing the visible product set in the current filter window.",
    avgMargin: "Average gross margin percentage where cost is available. Missing-cost rows are excluded from the percentage.",
    avgUnitPrice: "Average realized selling price per pound or per unit, based on the same pricing basis used across the page.",
    medianUnitPrice: "Median realized unit price. This is a sturdier pricing baseline than the average when outliers exist.",
    revenuePerProduct: "Total revenue divided by active SKUs. Use it to compare assortment productivity across filter slices.",
    revenuePerCustomer: "Total revenue divided by active customers in the current filter window.",
    aiSignals: "AI signals are lightweight heuristics from margin coverage, recent momentum, and pricing dispersion. They do not override raw metrics.",
    trajectory: "Performance trajectory plots realized revenue and demand trend. Granularity switches between month and week based on window length.",
    priceVelocity: "Price vs velocity compares realized price against average monthly order velocity. Bubble size reflects revenue exposure and color follows the pricing status bands.",
    recommendations: "Recommendations rank SKUs using revenue share, recent momentum, and pricing dispersion. They are guidance, not automatic changes.",
    performanceBubble: "Performance bubble is gross-margin target focused. X-axis compares current realization against minimum or target price, Y-axis ranks revenue, profit, or velocity, and bubble color uses the shared pricing status bands.",
    priceDistribution: "Unit price distribution shows where most transactions land. P10, P50, and P90 define the practical guardrail band.",
    topMovers: "Top movers compares the current filtered window against the prior comparable window and highlights the biggest absolute change.",
    segmentSummary: "Segment summary groups SKUs using the existing revenue and order heuristics already used elsewhere in Product Intelligence.",
    segmentMovers: "Segment movers shows which SKUs are driving the biggest revenue swings inside each current segment.",
    proteinFamily: "Protein intelligence shows which product families dominate revenue, which families are gaining or losing share, and where family-level margin pressure is building.",
    topProducts: "Top products re-ranks the current dataset by the selected metric without changing the underlying filters.",
    pareto: "Pareto shows how quickly revenue accumulates across the top SKUs. The cumulative line helps identify concentration risk.",
    healthMatrix: "Portfolio matrix classifies SKUs by percentile bands for velocity and profitability, then assigns Protect, Fix Margin, Grow, and Rationalize quadrants.",
    table: "The products table is still backed by the existing server-side table payload. Quick filters and sorting stay on the server so exports match the visible slice.",
  };

  const fmtMoney0 = new Intl.NumberFormat(undefined, { style: "currency", currency, maximumFractionDigits: 0 });
  const fmtMoney2 = new Intl.NumberFormat(undefined, { style: "currency", currency, maximumFractionDigits: 2 });
  const fmtInt = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
  const fmtPct1 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
  const fmtNum1 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
  const EM_DASH = "\u2014";
  const MIDDLE_DOT = "\u00b7";
  const ARROW = "\u2192";
  const ELLIPSIS = "\u2026";
  const DELTA = "\u0394";
  const WORKSPACE_SECTION_KEYS = ["overview", "strategy", "demand", "pricing", "execution", "assortment", "table"];
  const SECTION_GROUPS = {
    summary: ["overview", "strategy", "demand"],
    detail: ["pricing", "execution", "assortment"],
    table: ["table"],
  };
  const SECTION_GROUP_FOR_KEY = {
    overview: "summary",
    strategy: "summary",
    demand: "summary",
    pricing: "detail",
    execution: "detail",
    assortment: "detail",
    table: "table",
  };
  const LOCAL_STATE_PARAM_KEYS = [
    "page",
    "page_size",
    "per_page",
    "sort",
    "sort_by",
    "sort_dir",
    "direction",
    "search",
    "q",
    "segments",
    "segment",
    "quick_filters",
    "quick_filter",
    "bubble_top_n",
    "bubble_x",
    "bubble_color",
    "bubble_y",
    "forecast",
    "forecast_overlay",
    "_sections",
    "sections",
  ];
  const WATCHLIST_PRESETS = {
    clear: { quickFilters: [], emphasis: "revenue" },
    below_minimum: { quickFilters: ["below_minimum_margin"], emphasis: "profit", section: "pricing", mode: "analyst" },
    below_target: { quickFilters: ["below_target_margin"], emphasis: "profit", section: "pricing", mode: "analyst" },
    recover_margin: { quickFilters: ["recover_margin"], emphasis: "profit", section: "pricing" },
    protect_core: { quickFilters: ["protect_core"], emphasis: "revenue", section: "strategy" },
    elastic_risk: { quickFilters: ["elastic_risk"], emphasis: "profit", section: "pricing", mode: "analyst" },
    promote: { quickFilters: ["promote_candidate"], emphasis: "profit", section: "execution" },
    rationalize: { quickFilters: ["rationalize_candidate"], emphasis: "profit", section: "assortment", mode: "analyst" },
  };

  const waitForFiltersReady = async () => {
    const fallbackState = () => {
      try {
        return (window.getGlobalFilterState && window.getGlobalFilterState()) || {};
      } catch (err) {
        return {};
      }
    };
    if (window.filtersReady && typeof window.filtersReady.then === "function") {
      try {
        const timeout = new Promise((resolve) => setTimeout(() => resolve(fallbackState()), 1500));
        return await Promise.race([window.filtersReady, timeout]);
      } catch (err) {
        console.warn("[products] filtersReady rejected", err);
      }
    }
    return new Promise((resolve) => {
      const handler = (evt) => {
        cleanup();
        resolve(evt?.detail || {});
      };
      const cleanup = () => {
        document.removeEventListener("globalFilters:ready", handler);
        window.removeEventListener("globalFilters:ready", handler);
        clearTimeout(timer);
      };
      const timer = setTimeout(() => {
        cleanup();
        resolve(fallbackState());
      }, 1200);
      document.addEventListener("globalFilters:ready", handler, { once: true });
      window.addEventListener("globalFilters:ready", handler, { once: true });
    });
  };

  const state = {
    qs: "",
    page: 1,
    pageSize: 25,
    sortBy: "revenue",
    sortDir: "desc",
    search: "",
    segments: [],
    quickFilters: [],
    visibleColumns: [],
    bubbleTopN: 250,
    bubbleXMetric: "gap_to_target",
    bubbleColorBy: "status_key",
    bubbleYMetric: "velocity",
    showForecast: false,
    workspaceMode: isV4 ? "executive" : "analyst",
    workspaceDensity: "comfortable",
    workspaceEmphasis: "revenue",
    visibleSections: [...WORKSPACE_SECTION_KEYS],
    activeTablePreset: isV4 ? "summary" : "",
    stagedActions: [],
    workbenchSelection: null,
  };

  let lastPayload = null;
  let hasBootstrapped = false;
  let activeProductIntel = null;
  let productIntelOffcanvas = null;
  let pendingGlobalApplyAck = false;
  let pendingGlobalApplyId = "";
  let sectionObserver = null;
  const requestState = {
    summary: { abort: null, reqId: 0, loaded: false, loading: false },
    detail: { abort: null, reqId: 0, loaded: false, loading: false },
    table: { abort: null, reqId: 0, loaded: false, loading: false },
  };
  const charts = {};

  const safeNum = (v) => (Number.isFinite(+v) ? +v : 0);
  const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
  const nullish = (v, fallback = EM_DASH) => (v === null || v === undefined ? fallback : v);
  const hasMeaningfulText = (value) => {
    const text = String(value ?? "").trim();
    return Boolean(text && text !== EM_DASH);
  };
  const displayName = (row) => {
    if (!row || typeof row !== "object") return EM_DASH;
    if (row.display_name) return row.display_name;
    const sku = row.sku || row.product_id || row.key;
    const name = row.product_name || row.name || row.label;
    if (sku && name) return `${sku}  ${name}`;
    return sku || name || EM_DASH;
  };

  const getSku = (row) => {
    if (!row || typeof row !== "object") return "";
    return row.sku || row.product_id || row.key || "";
  };

  const rowRevenue = (row) => numericOrNull(row?.revenue);

  const numericOrNull = (value) => (value == null || Number.isNaN(Number(value)) ? null : Number(value));

  const statusMeta = (key = "") => {
    const meta = ((lastPayload?.margin_matrix || {}).status_meta || {})[key];
    return meta && typeof meta === "object"
      ? meta
      : { label: "Unclassified", short_label: "Unclassified", color: "#7a7f87", tone: "neutral" };
  };

  const visualStatusKey = (row = {}) => String(row?.visual_status_key || row?.price_status_key || row?.status_key || "").toLowerCase();

  const visualStatusLabel = (row = {}) => row?.visual_status || row?.price_status || row?.target_status || statusMeta(visualStatusKey(row)).label;

  const comparablePriceContext = (row = {}) => {
    const explicitBasis = String(row?.pricing_basis || "").toLowerCase();
    const aspLb = numericOrNull(row?.asp_lb);
    const minLb = numericOrNull(row?.minimum_price_lb);
    const targetLb = numericOrNull(row?.target_price_lb);
    const preferLb = explicitBasis === "lb" || (!explicitBasis && (aspLb != null || minLb != null || targetLb != null));
    if (preferLb) {
      return {
        basisLabel: "per lb",
        currentLabel: "ASP/lb",
        minLabel: "Min Price/lb",
        targetLabel: "Target Price/lb",
        current: aspLb,
        minimum: minLb,
        target: targetLb,
        gapToMin: numericOrNull(row?.asp_lb_gap_to_min),
        gapToTarget: numericOrNull(row?.asp_lb_gap_to_target),
      };
    }
    return {
      basisLabel: "per unit",
      currentLabel: "Current Price",
      minLabel: "Minimum Price",
      targetLabel: "Target Price",
      current: numericOrNull(row?.current_price ?? row?.current_unit_price ?? row?.unit_price),
      minimum: numericOrNull(row?.minimum_price),
      target: numericOrNull(row?.target_price),
      gapToMin: numericOrNull(row?.min_price_gap),
      gapToTarget: numericOrNull(row?.target_price_gap),
    };
  };

  const costContext = (row = {}, pricing = comparablePriceContext(row)) => {
    const perLb = pricing?.basisLabel === "per lb";
    const baseCost = perLb
      ? numericOrNull(row?.base_cost_lb ?? row?.base_unit_cost)
      : numericOrNull(row?.base_unit_cost);
    const effectiveCost = perLb
      ? numericOrNull(row?.effective_cost_lb ?? row?.cost_lb ?? row?.effective_unit_cost ?? row?.unit_cost)
      : numericOrNull(row?.effective_unit_cost ?? row?.unit_cost);
    return {
      baseLabel: perLb ? "Meat Manager Cost / lb" : "Meat Manager Cost",
      effectiveLabel: perLb ? "Effective Cost / lb" : "Effective Cost",
      base: baseCost,
      effective: effectiveCost,
    };
  };

  const rowHasPricingVisibility = (row = {}) => {
    const pricing = comparablePriceContext(row);
    const costs = costContext(row, pricing);
    return Boolean(
      pricing.minimum != null ||
      pricing.target != null ||
      costs.effective != null ||
      costs.base != null
    );
  };

  const percentile = (values = [], fraction = 0.5) => {
    const clean = (Array.isArray(values) ? values : [])
      .map((value) => numericOrNull(value))
      .filter((value) => value != null)
      .sort((a, b) => a - b);
    if (!clean.length) return null;
    if (clean.length === 1) return clean[0];
    const index = Math.min(clean.length - 1, Math.max(0, (clean.length - 1) * fraction));
    const lower = Math.floor(index);
    const upper = Math.ceil(index);
    if (lower === upper) return clean[lower];
    const share = index - lower;
    return clean[lower] + ((clean[upper] - clean[lower]) * share);
  };

  const revenueExposureBasis = (row = {}) => {
    const revenue = numericOrNull(row?.revenue);
    if (revenue != null && revenue > 0) return revenue;
    const uplift = numericOrNull(row?.profit_uplift_target);
    if (uplift != null && uplift > 0) return uplift;
    const qty = numericOrNull(row?.qty);
    if (qty != null && qty > 0) return qty;
    const weight = numericOrNull(row?.weight);
    if (weight != null && weight > 0) return weight;
    return 1;
  };

  const bubbleDiameters = (rows = [], resolver = revenueExposureBasis, minSize = 12, maxSize = 44) => {
    const rawValues = (Array.isArray(rows) ? rows : []).map((row) => {
      const value = numericOrNull(resolver(row));
      return value != null && value > 0 ? value : 1;
    });
    const positive = rawValues.filter((value) => value > 0);
    if (!positive.length) return rawValues.map(() => minSize);
    const floor = Math.max(1, percentile(positive, 0.1) || positive[0] || 1);
    const cap = Math.max(floor, percentile(positive, 0.95) || positive[positive.length - 1] || floor);
    const sqrtFloor = Math.sqrt(floor);
    const sqrtCap = Math.sqrt(cap);
    return rawValues.map((value) => {
      const bounded = Math.min(Math.max(value, floor), cap);
      if (sqrtCap <= sqrtFloor) return minSize;
      const ratio = (Math.sqrt(bounded) - sqrtFloor) / (sqrtCap - sqrtFloor);
      return minSize + (Math.max(0, Math.min(1, ratio)) * (maxSize - minSize));
    });
  };

  const escapeHtml = (value) =>
    String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");

  const readStoredColumns = () => {
    if (!isV2 || !window.localStorage) return [];
    try {
      const raw = window.localStorage.getItem(COLUMN_STORAGE_KEY);
      const parsed = raw ? JSON.parse(raw) : [];
      return Array.isArray(parsed) ? parsed.filter(Boolean) : [];
    } catch (err) {
      return [];
    }
  };

  const writeStoredColumns = (columns) => {
    if (!isV2 || !window.localStorage) return;
    try {
      window.localStorage.setItem(COLUMN_STORAGE_KEY, JSON.stringify(columns || []));
    } catch (err) {
      /* ignore storage failures */
    }
  };

  const readWorkspaceState = () => {
    if (!isV4 || !window.localStorage || !WORKSPACE_STORAGE_KEY) return null;
    try {
      const raw = window.localStorage.getItem(WORKSPACE_STORAGE_KEY);
      const parsed = raw ? JSON.parse(raw) : null;
      if (!parsed || typeof parsed !== "object") return null;
      return {
        mode: parsed.mode === "analyst" ? "analyst" : "executive",
        density: parsed.density === "compact" ? "compact" : "comfortable",
        emphasis: ["revenue", "profit", "weight"].includes(parsed.emphasis) ? parsed.emphasis : "revenue",
        visibleSections: Array.isArray(parsed.visibleSections)
          ? parsed.visibleSections.filter((key) => WORKSPACE_SECTION_KEYS.includes(key))
          : [...WORKSPACE_SECTION_KEYS],
      };
    } catch (err) {
      return null;
    }
  };

  const writeWorkspaceState = () => {
    if (!isV4 || !window.localStorage || !WORKSPACE_STORAGE_KEY) return;
    try {
      window.localStorage.setItem(
        WORKSPACE_STORAGE_KEY,
        JSON.stringify({
          mode: state.workspaceMode,
          density: state.workspaceDensity,
          emphasis: state.workspaceEmphasis,
          visibleSections: state.visibleSections,
        })
      );
    } catch (err) {
      /* ignore storage failures */
    }
  };

  const defaultVisibleColumns = () => ACTIVE_COLUMN_DEFS.filter((col) => col.visible).map((col) => col.key);
  const readStoredTablePreset = () => {
    if (!isV4 || !window.localStorage || !TABLE_PRESET_STORAGE_KEY) return "";
    try {
      return window.localStorage.getItem(TABLE_PRESET_STORAGE_KEY) || "";
    } catch (err) {
      return "";
    }
  };

  const writeStoredTablePreset = (presetKey) => {
    if (!isV4 || !window.localStorage || !TABLE_PRESET_STORAGE_KEY) return;
    try {
      if (presetKey) window.localStorage.setItem(TABLE_PRESET_STORAGE_KEY, presetKey);
      else window.localStorage.removeItem(TABLE_PRESET_STORAGE_KEY);
    } catch (err) {
      /* ignore storage failures */
    }
  };

  const readStagedActions = () => {
    if (!isV4 || !window.localStorage || !STAGED_ACTIONS_STORAGE_KEY) return [];
    try {
      const raw = window.localStorage.getItem(STAGED_ACTIONS_STORAGE_KEY);
      const parsed = raw ? JSON.parse(raw) : [];
      return Array.isArray(parsed) ? parsed.filter((row) => row && typeof row === "object") : [];
    } catch (err) {
      return [];
    }
  };

  const writeStagedActions = () => {
    if (!isV4 || !window.localStorage || !STAGED_ACTIONS_STORAGE_KEY) return;
    try {
      window.localStorage.setItem(STAGED_ACTIONS_STORAGE_KEY, JSON.stringify(state.stagedActions || []));
    } catch (err) {
      /* ignore storage failures */
    }
  };

  const COLUMN_GROUPS = {
    performance: ["sku", "product", "protein_family", "segment", "revenue", "revenue_current", "revenue_delta", "revenue_delta_pct", "orders", "velocity_per_month", "qty", "weight", "revenue_share"],
    unit_econ: ["sku", "product", "protein_family", "segment", "profit", "margin_pct", "minimum_margin_pct", "target_margin_pct", "contribution_margin_lb", "asp_lb", "cost_lb", "target_price", "uplift_pct"],
    pricing: ["sku", "product", "protein_family", "segment", "current_unit_price", "minimum_price", "asp_lb", "asp_lb_gap_to_min", "minimum_price_lb", "target_price_lb", "asp_lb_gap_to_target", "price_variance_vs_median", "volatility_score"],
    risk: ["sku", "product", "protein_family", "segment", "margin_risk", "recommendation", "revenue", "profit", "margin_pct", "top_customer_share", "customer_hhi"],
    breadth: ["sku", "product", "protein_family", "segment", "supplier", "customer_count", "supplier_count", "region_breadth", "top_customer_share", "customer_hhi", "revenue"],
  };
  const TABLE_PRESETS = {
    summary: {
      columns: ["sku", "product", "protein_family", "segment", "revenue", "revenue_delta_pct", "orders", "velocity_per_month", "margin_pct", "recommendation"],
      note: "Summary preset keeps only the core portfolio, demand, and margin signals.",
    },
    margin_risk: {
      columns: ["sku", "product", "protein_family", "segment", "revenue", "profit", "current_unit_price", "asp_lb", "cost_lb", "margin_pct", "minimum_margin_pct", "target_margin_pct", "asp_lb_gap_to_target", "margin_risk", "recommendation"],
      note: "Margin risk preset shows cost, ASP, gross margin, gap-to-target, and the recommended action.",
    },
    pricing_ladder: {
      columns: ["sku", "product", "protein_family", "segment", "current_unit_price", "minimum_price", "target_price", "asp_lb", "minimum_price_lb", "target_price_lb", "asp_lb_gap_to_min", "asp_lb_gap_to_target", "price_variance_vs_median", "volatility_score", "recommendation"],
      note: "Pricing ladder preset narrows the table to pricing inputs, guardrails, and ladder gaps.",
    },
    demand: {
      columns: ["sku", "product", "protein_family", "segment", "revenue_current", "revenue_prior", "revenue_delta", "revenue_delta_pct", "orders_current", "orders_prior", "velocity_per_month", "qty", "margin_pct", "recommendation"],
      note: "Demand drop preset focuses on current-versus-prior trend fields and velocity.",
    },
    execution: {
      columns: ["sku", "product", "protein_family", "segment", "revenue", "profit", "margin_pct", "customer_count", "top_customer_share", "margin_risk", "recommendation", "last_sold", "quick_rec"],
      note: "Execution preset centers the table on owner-ready action fields and customer exposure.",
    },
    all: {
      columns: ACTIVE_COLUMN_DEFS.map((col) => col.key),
      note: "All columns restores the full analyst workspace.",
    },
  };

  if (isV2) {
    if (isV4) {
      const storedPreset = readStoredTablePreset();
      if (storedPreset && TABLE_PRESETS[storedPreset]) state.activeTablePreset = storedPreset;
    }
    const stored = readStoredColumns();
    state.visibleColumns = stored.length
      ? stored
      : (isV4 ? [...(TABLE_PRESETS[state.activeTablePreset]?.columns || defaultVisibleColumns())] : defaultVisibleColumns());
  }
  if (isV4) {
    const workspaceState = readWorkspaceState();
    if (workspaceState) {
      state.workspaceMode = workspaceState.mode;
      state.workspaceDensity = workspaceState.density;
      state.workspaceEmphasis = workspaceState.emphasis;
      state.visibleSections = workspaceState.visibleSections.length ? workspaceState.visibleSections : [...WORKSPACE_SECTION_KEYS];
    }
    state.stagedActions = readStagedActions();
  }

  const setText = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.textContent = value ?? EM_DASH;
  };

  const setHtml = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.innerHTML = value ?? "";
  };

  const scrollToSection = (sectionKey) => {
    if (!sectionKey) return;
    const selectorMap = {
      overview: "#products-overview",
      strategy: "#products-strategy-brief, #products-health",
      demand: "#products-trajectory",
      pricing: "#products-pricing",
      execution: "#products-risk-opportunity",
      assortment: "#products-segments",
      table: "#products-table",
    };
    const target = document.querySelector(selectorMap[sectionKey] || "");
    if (!target || typeof target.scrollIntoView !== "function") return;
    target.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const hasOwn = (obj, key) => Object.prototype.hasOwnProperty.call(obj || {}, key);

  const makeInteractiveCard = (node, onClick) => {
    if (!node || typeof onClick !== "function") return;
    node.classList.add("is-clickable-card");
    node.setAttribute("role", "button");
    if (!node.hasAttribute("tabindex")) node.tabIndex = 0;
    if (node.dataset.clickBound === "1") return;
    node.dataset.clickBound = "1";
    node.addEventListener("click", (evt) => {
      if (evt.target.closest("a,button,input,select,label,summary")) return;
      onClick(evt);
    });
    node.addEventListener("keydown", (evt) => {
      if (evt.key !== "Enter" && evt.key !== " ") return;
      evt.preventDefault();
      onClick(evt);
    });
  };

  const usesLocalTableSubset = (options = {}) =>
    hasOwn(options, "quickFilters") ||
    hasOwn(options, "segments") ||
    hasOwn(options, "search") ||
    Boolean(options.sortBy) ||
    Boolean(options.sortDir);

  const localSubsetScrollSection = (preferredSection, options = {}) => {
    const targetSection = preferredSection || "table";
    if (targetSection === "table") return "table";
    return usesLocalTableSubset(options) ? "table" : targetSection;
  };

  const updateTableLayerContextForSubset = (sourceSection, options = {}) => {
    const parts = [];
    if (Array.isArray(state.quickFilters) && state.quickFilters.length) {
      parts.push(`${state.quickFilters.length} quick filter${state.quickFilters.length === 1 ? "" : "s"}`);
    }
    if (Array.isArray(state.segments) && state.segments.length) {
      parts.push(`${state.segments.length} segment${state.segments.length === 1 ? "" : "s"}`);
    }
    if (state.search) {
      parts.push(`search "${state.search}"`);
    }
    const sourceLabel = {
      overview: "Executive scorecard",
      strategy: "Strategy",
      demand: "Demand",
      pricing: "Pricing",
      execution: "Execution",
      assortment: "Assortment",
    }[sourceSection || ""] || "Detail";
    const subsetLabel = parts.length
      ? `Applied ${parts.join(` ${MIDDLE_DOT} `)}.`
      : "A filtered subset is active.";
    setText(
      "tableLayerContext",
      `${sourceLabel} actions open the filtered table workspace so row drilldowns, exports, and pagination stay aligned. ${subsetLabel}`
    );
  };

  const inferTablePreset = (options = {}) => {
    const quickFilters = [...(options.quickFilters || options.quick_filters || [])].map((item) => String(item || "").toLowerCase());
    if (options.tablePreset && TABLE_PRESETS[options.tablePreset]) return options.tablePreset;
    if (quickFilters.some((key) => ["outside_guardrail", "high_price_outlier"].includes(key))) return "pricing_ladder";
    if (quickFilters.some((key) => ["below_target_margin", "below_minimum_margin", "recover_margin", "elastic_risk"].includes(key))) return "margin_risk";
    if (quickFilters.some((key) => ["missing_cost"].includes(key)) || String(options.section || "").toLowerCase() === "execution") return "execution";
    if (String(options.section || "").toLowerCase() === "pricing") return "margin_risk";
    if (String(options.section || "").toLowerCase() === "demand") return "demand";
    if (quickFilters.some((key) => ["promote_candidate", "protect_core"].includes(key))) return "execution";
    return null;
  };

  const applyDetailView = (options = {}) => {
    const targetSection = options.section || "table";
    const nextScrollSection = localSubsetScrollSection(targetSection, options);
    const localSubset = nextScrollSection === "table" && targetSection !== "table";
    const tableNeedsRefresh =
      hasOwn(options, "quickFilters") ||
      hasOwn(options, "segments") ||
      hasOwn(options, "search") ||
      Boolean(options.sortBy) ||
      Boolean(options.sortDir) ||
      targetSection === "table";
    const inferredPreset = inferTablePreset(options);
    if (hasOwn(options, "quickFilters")) state.quickFilters = [...(options.quickFilters || [])];
    if (hasOwn(options, "segments")) state.segments = [...(options.segments || [])];
    if (hasOwn(options, "search")) state.search = options.search || "";
    if (options.sortBy) state.sortBy = options.sortBy;
    if (options.sortDir) state.sortDir = options.sortDir;
    if (inferredPreset) applyTablePreset(inferredPreset, { syncUi: true });
    if (options.mode) state.workspaceMode = options.mode;
    if (options.emphasis && options.emphasis !== state.workspaceEmphasis) {
      applyEmphasisPreset(options.emphasis);
    } else {
      applyWorkspaceSettings();
    }
    const searchEl = document.getElementById("tableSearch");
    if (searchEl && hasOwn(options, "search")) searchEl.value = state.search || "";
    const segmentSelect = document.getElementById("segmentFilter");
    if (segmentSelect && hasOwn(options, "segments")) {
      Array.from(segmentSelect.options || []).forEach((option) => {
        option.selected = state.segments.includes(option.value);
      });
    }
    state.page = 1;
    syncWatchlistButtons();
    syncQuickFilterButtons();
    if (localSubset) {
      let workspaceChanged = false;
      if (!state.visibleSections.includes("table")) {
        state.visibleSections = [...state.visibleSections, "table"];
        workspaceChanged = true;
      }
      if (workspaceChanged) applyWorkspaceSettings();
      updateTableLayerContextForSubset(targetSection, options);
    }
    if (tableNeedsRefresh) refreshTableBundle();
    if (!localSubset && targetSection && targetSection !== "table") {
      ensureSectionGroup(targetSection, { force: !requestState[SECTION_GROUP_FOR_KEY[targetSection]]?.loaded });
    }
    if (options.section || nextScrollSection === "table") {
      setTimeout(() => scrollToSection(nextScrollSection), 120);
    }
  };

  const applySignalAction = (action = {}) => {
    if (!action || typeof action !== "object") return;
    applyDetailView({
      quickFilters: action.quickFilters || action.quick_filters,
      segments: action.segments,
      search: action.search,
      sortBy: action.sortBy || action.sort_by,
      sortDir: action.sortDir || action.sort_dir,
      mode: action.mode,
      emphasis: action.emphasis,
      section: action.section,
    });
  };

  const syncWorkspaceControls = () => {
    if (!isV4) return;
    document.getElementById("workspaceModeExecutive")?.classList.toggle("active", state.workspaceMode === "executive");
    document.getElementById("workspaceModeAnalyst")?.classList.toggle("active", state.workspaceMode === "analyst");
    document.getElementById("workspaceDensityComfortable")?.classList.toggle("active", state.workspaceDensity === "comfortable");
    document.getElementById("workspaceDensityCompact")?.classList.toggle("active", state.workspaceDensity === "compact");
    const emphasis = document.getElementById("workspaceEmphasis");
    if (emphasis && emphasis.value !== state.workspaceEmphasis) emphasis.value = state.workspaceEmphasis;
    document.querySelectorAll("[data-section-toggle]").forEach((input) => {
      const key = input.getAttribute("data-section-toggle");
      if (!key) return;
      input.checked = state.visibleSections.includes(key);
    });
  };

  const applyWorkspaceSettings = () => {
    if (!isV4) return;
    root.dataset.workspaceMode = state.workspaceMode;
    root.dataset.workspaceDensity = state.workspaceDensity;
    root.dataset.workspaceEmphasis = state.workspaceEmphasis;
    const allowed = new Set(state.visibleSections || WORKSPACE_SECTION_KEYS);
    document.querySelectorAll("[data-workspace-section]").forEach((section) => {
      const key = section.getAttribute("data-workspace-section");
      if (!key) return;
      section.classList.toggle("is-hidden-by-workspace", !allowed.has(key));
    });
    syncWorkspaceControls();
    writeWorkspaceState();
    if (typeof setupLazySectionObserver === "function") {
      setupLazySectionObserver();
    }
  };

  const removeSkeleton = (targetId) => {
    document.querySelectorAll(`.skeleton[data-for="${targetId}"]`).forEach((el) => el.remove());
  };

  const destroyChart = (key) => {
    if (charts[key]?.destroy) {
      charts[key].destroy();
    }
    charts[key] = null;
  };

  const isPlainObject = (value) => Object.prototype.toString.call(value) === "[object Object]";

  const mergePayload = (base, patch) => {
    if (!isPlainObject(base)) return isPlainObject(patch) ? { ...patch } : patch;
    const merged = { ...base };
    Object.entries(patch || {}).forEach(([key, value]) => {
      if (value === undefined) return;
      if (Array.isArray(value)) {
        merged[key] = value.slice();
        return;
      }
      if (isPlainObject(value) && isPlainObject(merged[key])) {
        merged[key] = mergePayload(merged[key], value);
        return;
      }
      merged[key] = value;
    });
    return merged;
  };

  const loadedGroups = () =>
    Object.entries(requestState)
      .filter(([, value]) => !!value?.loaded)
      .map(([key]) => key);

  const restoreLoadedGroups = (groups = []) => {
    const active = new Set(Array.isArray(groups) ? groups : []);
    Object.keys(requestState).forEach((group) => {
      requestState[group].loaded = active.has(group);
    });
  };

  const syncControlsFromState = () => {
    const searchEl = document.getElementById("tableSearch");
    const pageSizeEl = document.getElementById("tablePageSize");
    const bubbleTop = document.getElementById("bubbleTopN");
    const bubbleX = document.getElementById("bubbleXMetric");
    const bubbleColor = document.getElementById("bubbleColorBy");
    const bubbleY = document.getElementById("bubbleYMetric");
    const segmentSelect = document.getElementById("segmentFilter");
    const forecastToggle = document.getElementById("toggleForecast");
    if (searchEl) searchEl.value = state.search || "";
    if (pageSizeEl) pageSizeEl.value = String(state.pageSize);
    if (bubbleTop) bubbleTop.value = String(state.bubbleTopN || 250);
    if (bubbleX) bubbleX.value = state.bubbleXMetric || "gap_to_target";
    if (bubbleColor) bubbleColor.value = state.bubbleColorBy || "status_key";
    if (bubbleY) bubbleY.value = state.bubbleYMetric || "velocity";
    if (forecastToggle) forecastToggle.checked = !!state.showForecast;
    if (segmentSelect) {
      Array.from(segmentSelect.options || []).forEach((option) => {
        option.selected = state.segments.includes(option.value);
      });
    }
    syncWatchlistButtons();
    syncQuickFilterButtons();
    applyWorkspaceSettings();
  };

  const snapshotUiState = () => ({
    qs: state.qs,
    page: state.page,
    pageSize: state.pageSize,
    sortBy: state.sortBy,
    sortDir: state.sortDir,
    search: state.search,
    segments: [...state.segments],
    quickFilters: [...state.quickFilters],
    visibleColumns: [...state.visibleColumns],
    bubbleTopN: state.bubbleTopN,
    bubbleXMetric: state.bubbleXMetric,
    bubbleColorBy: state.bubbleColorBy,
    bubbleYMetric: state.bubbleYMetric,
    showForecast: !!state.showForecast,
    workspaceMode: state.workspaceMode,
    workspaceDensity: state.workspaceDensity,
    workspaceEmphasis: state.workspaceEmphasis,
    visibleSections: [...state.visibleSections],
    loadedGroups: loadedGroups(),
  });

  const applySnapshotUiState = (uiState = {}) => {
    if (!uiState || typeof uiState !== "object") return;
    if (uiState.qs != null) state.qs = String(uiState.qs);
    if (Number.isFinite(Number(uiState.page)) && Number(uiState.page) > 0) state.page = Number(uiState.page);
    if (Number.isFinite(Number(uiState.pageSize)) && Number(uiState.pageSize) > 0) state.pageSize = Number(uiState.pageSize);
    if (uiState.sortBy) state.sortBy = String(uiState.sortBy);
    if (uiState.sortDir) state.sortDir = String(uiState.sortDir) === "asc" ? "asc" : "desc";
    if (uiState.search != null) state.search = String(uiState.search);
    if (Array.isArray(uiState.segments)) state.segments = [...uiState.segments];
    if (Array.isArray(uiState.quickFilters)) state.quickFilters = [...uiState.quickFilters];
    if (Array.isArray(uiState.visibleColumns)) state.visibleColumns = [...uiState.visibleColumns];
    if (uiState.bubbleTopN != null) state.bubbleTopN = uiState.bubbleTopN;
    if (uiState.bubbleXMetric) state.bubbleXMetric = String(uiState.bubbleXMetric);
    if (uiState.bubbleColorBy) state.bubbleColorBy = String(uiState.bubbleColorBy);
    if (uiState.bubbleYMetric) state.bubbleYMetric = String(uiState.bubbleYMetric);
    state.showForecast = !!uiState.showForecast;
    if (uiState.workspaceMode) state.workspaceMode = String(uiState.workspaceMode);
    if (uiState.workspaceDensity) state.workspaceDensity = String(uiState.workspaceDensity);
    if (uiState.workspaceEmphasis) state.workspaceEmphasis = String(uiState.workspaceEmphasis);
    if (Array.isArray(uiState.visibleSections) && uiState.visibleSections.length) state.visibleSections = [...uiState.visibleSections];
    restoreLoadedGroups(uiState.loadedGroups || []);
  };

  const persistSnapshot = (payload = lastPayload) => {
    if (!pageCache || !payload || !state.qs) return false;
    return pageCache.saveSnapshot(PAGE_CACHE_ID, {
      qs: state.qs,
      payload,
      uiState: snapshotUiState(),
      scrollY: window.scrollY || 0,
      meta: {
        datasetVersion: payload?.meta?.dataset_version || null,
      },
    });
  };

  const restoreSnapshot = (qs, { restoreScroll = false } = {}) => {
    if (!pageCache) return null;
    const snapshot = pageCache.loadSnapshot(PAGE_CACHE_ID, { qs, ...PAGE_CACHE_POLICY });
    if (!snapshot?.payload) return null;
    applySnapshotUiState(snapshot.ui_state || {});
    lastPayload = snapshot.payload || {};
    renderSummaryBundle(lastPayload);
    renderDetailBundle(lastPayload);
    renderTableBundle(lastPayload);
    finalizeBundleRender();
    syncControlsFromState();
    if (restoreScroll) {
      pageCache.restoreScroll(PAGE_CACHE_ID, { qs, ...PAGE_CACHE_POLICY, delayMs: 40 });
    }
    return snapshot;
  };

  const baseQueryParams = () => {
    const params = new URLSearchParams(state.qs || "");
    LOCAL_STATE_PARAM_KEYS.forEach((key) => params.delete(key));
    return params;
  };

  const buildHistoryQS = () => {
    const params = baseQueryParams();
    params.set("page", String(state.page));
    params.set("page_size", String(state.pageSize));
    params.set("sort_by", state.sortBy);
    params.set("sort_dir", state.sortDir);
    if (state.search) params.set("search", state.search);
    else params.delete("search");
    if (state.segments?.length) params.set("segments", state.segments.join(","));
    else params.delete("segments");
    if (state.quickFilters?.length) params.set("quick_filters", state.quickFilters.join(","));
    else params.delete("quick_filters");
    params.set("bubble_top_n", String(state.bubbleTopN));
    params.set("bubble_x", state.bubbleXMetric);
    params.set("bubble_color", state.bubbleColorBy);
    params.set("bubble_y", state.bubbleYMetric);
    if (!isV3 && state.showForecast) params.set("forecast", "1");
    else params.delete("forecast");
    return params.toString();
  };

  const buildSectionQS = (group) => {
    const params = baseQueryParams();
    params.set("_sections", (SECTION_GROUPS[group] || []).join(","));
    if (group === "table") {
      params.set("page", String(state.page));
      params.set("page_size", String(state.pageSize));
      params.set("sort_by", state.sortBy);
      params.set("sort_dir", state.sortDir);
      if (state.search) params.set("search", state.search);
      if (state.segments?.length) params.set("segments", state.segments.join(","));
      if (state.quickFilters?.length) params.set("quick_filters", state.quickFilters.join(","));
      return params.toString();
    }
    if (group === "detail") {
      params.set("bubble_top_n", String(state.bubbleTopN).toLowerCase() === "all" ? "5000" : String(state.bubbleTopN));
      return params.toString();
    }
    if (!isV3 && state.showForecast) params.set("forecast", "1");
    return params.toString();
  };

  const buildQS = () => buildHistoryQS();

  const syncStateFromQS = (qs) => {
    const params = new URLSearchParams(qs || "");
    const page = Number(params.get("page") || state.page);
    const pageSize = Number(params.get("page_size") || params.get("per_page") || state.pageSize);
    const bubbleTopNRaw = params.get("bubble_top_n");
    const bubbleTopN = Number(bubbleTopNRaw || state.bubbleTopN);
    state.page = Number.isFinite(page) && page > 0 ? page : 1;
    state.pageSize = Number.isFinite(pageSize) && pageSize > 0 ? pageSize : state.pageSize;
    state.sortBy = params.get("sort_by") || state.sortBy;
    state.sortDir = params.get("sort_dir") || state.sortDir;
    state.search = params.get("search") || "";
    state.segments = (params.get("segments") || "")
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
    state.quickFilters = (params.get("quick_filters") || "")
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
    if (String(bubbleTopNRaw || "").toLowerCase() === "all" || (Number.isFinite(bubbleTopN) && bubbleTopN >= 5000)) {
      state.bubbleTopN = "all";
    } else if (Number.isFinite(bubbleTopN) && bubbleTopN > 0) {
      state.bubbleTopN = bubbleTopN;
    }
    const bubbleXParam = params.get("bubble_x");
    const bubbleColorParam = params.get("bubble_color");
    const bubbleYParam = params.get("bubble_y");
    state.bubbleXMetric = ["gap_to_target", "gap_to_min", "margin_pct"].includes(String(bubbleXParam || ""))
      ? String(bubbleXParam)
      : state.bubbleXMetric;
    state.bubbleColorBy = bubbleColorParam === "segment" ? "segment" : "status_key";
    state.bubbleYMetric = ["velocity", "revenue", "profit"].includes(String(bubbleYParam || ""))
      ? String(bubbleYParam)
      : state.bubbleYMetric;
    state.showForecast = params.get("forecast") === "1";
  };

  const currentExportColumns = () => {
    if (!isV2) return [];
    return ACTIVE_COLUMN_DEFS.filter((col) => col.exportable && (col.locked || state.visibleColumns.includes(col.key))).map((col) => col.key);
  };

  const buildExportQS = () => {
    const params = new URLSearchParams(state.qs || resolveInitialQS() || "");
    params.set("sort_by", state.sortBy);
    params.set("sort_dir", state.sortDir);
    if (state.search) params.set("search", state.search);
    else params.delete("search");
    if (state.segments?.length) params.set("segments", state.segments.join(","));
    else params.delete("segments");
    if (state.quickFilters?.length) params.set("quick_filters", state.quickFilters.join(","));
    else params.delete("quick_filters");
    const columns = currentExportColumns();
    if (columns.length) params.set("columns", columns.join(","));
    else params.delete("columns");
    return params.toString();
  };

  const syncExportLinks = () => {
    if (!isV2) return;
    const qs = buildExportQS();
    const targets = [
      ["productsHeroExportExcel", root.dataset.exportXlsx],
      ["productsHeroExportCsv", root.dataset.exportCsv],
      ["productsTableExportExcel", root.dataset.exportXlsx],
      ["productsTableExportCsv", root.dataset.exportCsv],
      ["priceExportCsv", root.dataset.exportCsv],
      ["topProductsExportCsv", root.dataset.exportCsv],
      ["paretoExportCsv", root.dataset.exportCsv],
      ["topMoversCsv", root.dataset.exportMoversCsv || root.dataset.exportCsv],
      ["segmentMixExportCsv", root.dataset.exportSegmentMixCsv || root.dataset.exportCsv],
      ["pricingActionsExport", root.dataset.exportExecutionCsv || root.dataset.exportCsv],
      ["execPricingExport", root.dataset.exportExecutionCsv || root.dataset.exportCsv],
      ["execCostExport", root.dataset.exportExecutionCsv || root.dataset.exportCsv],
      ["execPromoteExport", root.dataset.exportExecutionCsv || root.dataset.exportCsv],
    ];
    targets.forEach(([id, baseUrl]) => {
      const el = document.getElementById(id);
      if (!el || !baseUrl) return;
      const extra = new URLSearchParams();
      if (id === "execPricingExport" || id === "pricingActionsExport") extra.set("list", "pricing_fixes");
      if (id === "execCostExport") extra.set("list", "cost_fixes");
      if (id === "execPromoteExport") extra.set("list", "promote_candidates");
      const extraQs = extra.toString();
      const merged = [extraQs, qs].filter(Boolean).join("&");
      el.href = merged ? `${baseUrl}?${merged}` : baseUrl;
    });
  };

  const appendFiltersToUrl = (url) => {
    if (!url) return "#";
    const qs = state.qs;
    if (!qs) return url;
    const suffix = qs.startsWith("?") ? qs.slice(1) : qs;
    return url.includes("?") ? `${url}&${suffix}` : `${url}?${suffix}`;
  };

  const currentFilterState = () => {
    try {
      const globalState = window.getGlobalFilterState ? window.getGlobalFilterState() : {};
      if (globalState?.filters && typeof globalState.filters === "object") {
        return globalState.filters;
      }
    } catch (_err) {
      /* ignore */
    }
    return {};
  };

  const openUniversal = (payload, el = root) => {
    if (!payload || !window.universalDrilldown || typeof window.universalDrilldown.open !== "function") return false;
    window.universalDrilldown.open(payload, {}, el || root);
    return true;
  };

  const productDrilldownPayload = (row, section, widget, metric, value, extra = {}) => {
    const sku = getSku(row);
    if (!sku) return null;
    return {
      source_page: "products",
      source_section: section,
      source_widget: widget,
      requested_target: "product",
      clicked_entity_type: "product",
      clicked_entity_id: sku,
      clicked_entity_label: displayName(row),
      clicked_metric: metric,
      clicked_metric_value: value,
      active_filter_state: currentFilterState(),
      comparison_context: lastPayload?.comparison || null,
      workspace_state: {
        mode: state.workspaceMode,
        density: state.workspaceDensity,
        emphasis: state.workspaceEmphasis,
        quick_filters: [...(state.quickFilters || [])],
        segments: [...(state.segments || [])],
        search: state.search || "",
      },
      extra,
    };
  };

  const openProductDrilldown = (row, section, widget, metric, value, el = root, extra = {}) => {
    const payload = productDrilldownPayload(row, section, widget, metric, value, extra);
    if (!payload) return;
    if (openUniversal(payload, el)) return;
    if (!drilldownTemplate) return;
    const url = appendFiltersToUrl(drilldownTemplate.replace("__PID__", encodeURIComponent(payload.clicked_entity_id)));
    persistSnapshot(lastPayload);
    window.location.href = url;
  };

  const mergeDefined = (target, source) => {
    Object.entries(source || {}).forEach(([key, value]) => {
      if (value === null || value === undefined || value === "") return;
      target[key] = value;
    });
    return target;
  };

  const resolveProductContextRow = (seed = {}) => {
    const sku = getSku(seed);
    const merged = {};
    const sources = [
      seed,
      ...(lastPayload?.table?.rows || []),
      ...(lastPayload?.sku_metrics || []),
      ...(lastPayload?.price_vs_velocity || []),
      ...((lastPayload?.performance_bubble || {}).points || []),
      ...((lastPayload?.charts || {}).top_products || []),
      ...((lastPayload?.charts || {}).movers || []),
      ...((((lastPayload?.charts || {}).segments || {}).movers) || []),
      ...((lastPayload?.risk_opportunity || {}).margin_risk_top || []),
      ...((lastPayload?.risk_opportunity || {}).high_velocity_low_margin || []),
      ...((lastPayload?.risk_opportunity || {}).high_margin_low_velocity || []),
      ...((lastPayload?.recommendations) || []),
      ...((lastPayload?.pricing_guardrails || {}).rows || []),
      ...((lastPayload?.execution_lists || {}).pricing_fixes || []),
      ...((lastPayload?.execution_lists || {}).cost_fixes || []),
      ...((lastPayload?.execution_lists || {}).promote_candidates || []),
    ];
    sources.forEach((row) => {
      if (!row || getSku(row) !== sku) return;
      mergeDefined(merged, row);
    });
    return merged;
  };

  const resolveTopProductInsight = () => {
    const insight = ((lastPayload?.insights || []).find((row) => row?.metric === "top_product")) || null;
    const candidates = [
      insight,
      ...((lastPayload?.charts || {}).top_products || []),
      ...(lastPayload?.table?.rows || []),
      ...(lastPayload?.sku_metrics || []),
      ...(lastPayload?.price_vs_velocity || []),
      ...((((lastPayload?.performance_bubble || {}).points) || [])),
    ]
      .filter((row) => row && typeof row === "object")
      .map((row) => {
        const resolved = getSku(row) ? resolveProductContextRow(row) : { ...row };
        const label = displayName(resolved);
        const revenue = rowRevenue(resolved);
        return hasMeaningfulText(label) ? { ...resolved, display_name: label, revenue } : null;
      })
      .filter(Boolean)
      .sort((a, b) => (rowRevenue(b) || 0) - (rowRevenue(a) || 0));
    return candidates[0] || insight || null;
  };

  const percentText = (value, suffix = "%") =>
    value == null || Number.isNaN(Number(value)) ? EM_DASH : `${Number(value) > 0 ? "+" : ""}${fmtPct1.format(value)}${suffix}`;

  const countLabel = (count, singular, plural) => `${fmtInt.format(count || 0)} ${count === 1 ? singular : plural}`;

  const formatInsightValue = (label, value) => {
    const key = String(label || "").toLowerCase();
    if (value == null || Number.isNaN(Number(value))) return value == null ? EM_DASH : String(value);
    if (key.includes("margin") || key.includes("uplift")) return `${fmtPct1.format(value)}%`;
    if (key.includes("price") || key.includes("revenue") || key.includes("profit") || key.includes("cost") || key.includes("contribution")) {
      return fmtMoney0.format(value);
    }
    if (key.includes("share")) return `${fmtPct1.format(value)}%`;
    return fmtInt.format(value);
  };

  const deriveSuggestedAction = (row = {}) => {
    const actionText = String(row?.action || row?.recommendation || row?.quick_rec || "").toLowerCase();
    const marginPct = Number(row?.margin_pct);
    const targetMarginPct = Number(row?.target_margin_pct);
    const minimumMarginPct = Number(row?.minimum_margin_pct);
    const topCustomerShare = Number(row?.top_customer_share);
    const statusKey = String(row?.status_key || "").toLowerCase();
    const pricing = comparablePriceContext(row);
    const velocity = Number(row?.velocity_per_month ?? row?.orders_per_month ?? 0);
    const hasTarget = !Number.isNaN(targetMarginPct);
    const belowTarget = !Number.isNaN(marginPct) && hasTarget && marginPct < targetMarginPct;
    const belowMinimum = !Number.isNaN(marginPct) && !Number.isNaN(minimumMarginPct) && marginPct < minimumMarginPct;
    const aboveTarget = !Number.isNaN(marginPct) && hasTarget && marginPct >= targetMarginPct;
    if (row?.unit_cost == null && row?.cost == null) {
      return {
        label: "Review cost coverage",
        note: "Cost is missing, so pricing and margin guidance are less trustworthy until coverage is repaired.",
        view: { quickFilters: ["missing_cost"], section: "execution", emphasis: "profit", mode: "analyst" },
      };
    }
    if (statusKey === "needs_mapping" || row?.needs_protein_mapping) {
      return {
        label: "Fix protein mapping",
        note: "The SKU needs a protein/category rule mapping before minimum and target pricing guidance can be trusted.",
        view: { search: getSku(row), section: "table", mode: "analyst" },
      };
    }
    if (belowMinimum) {
      return {
        label: "Recover minimum price now",
        note: `${pricing.currentLabel} is below the minimum threshold for this protein rule, so pricing review is urgent before additional volume is chased.`,
        view: { quickFilters: ["below_minimum_margin"], section: "pricing", emphasis: "profit", mode: "analyst" },
      };
    }
    if (belowTarget) {
      return {
        label: "Recover margin",
        note: `The SKU is below its ${fmtPct1.format(targetMarginPct)}% protein-aware target gross margin inside the visible scope and should sit in the pricing queue.`,
        view: { quickFilters: ["recover_margin"], section: "pricing", emphasis: "profit", mode: "analyst" },
      };
    }
    if (actionText.includes("promote") || (aboveTarget && velocity > 0 && velocity < 3)) {
      return {
        label: "Promote or expand distribution",
        note: "Margin looks healthy relative to demand, so commercial teams should test feature support or broader placement.",
        view: { quickFilters: ["promote_candidate"], section: "execution", emphasis: "profit", mode: "analyst" },
      };
    }
    if (actionText.includes("rationalize") || String(row?.segment || "").toLowerCase() === "long tail") {
      return {
        label: "Review assortment role",
        note: "Long-tail, low-priority SKUs should be checked for pack simplification, minimum viable coverage, or rationalization.",
        view: { quickFilters: ["rationalize_candidate"], section: "assortment", emphasis: "profit", mode: "analyst" },
      };
    }
    if (!Number.isNaN(topCustomerShare) && topCustomerShare >= 50) {
      return {
        label: "Review customer dependency",
        note: "A narrow customer base increases operational and commercial risk if one account softens.",
        view: { quickFilters: ["high_customer_dependency"], section: "table", emphasis: "revenue", mode: "analyst" },
      };
    }
    return {
      label: "Review in table workspace",
      note: "Use the table workspace to inspect customer breadth, price quality, and the full recommendation context.",
      view: { search: getSku(row), section: "table", mode: "analyst" },
    };
  };

  const buildProductWhyLines = (row = {}, section = "", widget = "", metric = "", value = null) => {
    const lines = [];
    const pricing = comparablePriceContext(row);
    if (widget) {
      const metricText = metric ? `${metric} ${MIDDLE_DOT} ${formatInsightValue(metric, value)}` : "portfolio interaction";
      lines.push(`Selected from ${widget} under ${section || "Product Intelligence"} using ${metricText}.`);
    }
    if (String(row?.status_key || "").toLowerCase() === "needs_mapping") {
      lines.push("Protein/category mapping is missing, so the SKU cannot be compared against the correct minimum and target pricing rules yet.");
    }
    if (row?.revenue_delta_pct != null && !Number.isNaN(Number(row.revenue_delta_pct))) {
      const delta = Number(row.revenue_delta_pct);
      if (delta <= -8) lines.push(`Revenue is softening versus the prior comparable window (${percentText(delta)}).`);
      else if (delta >= 8) lines.push(`Revenue is accelerating versus the prior comparable window (${percentText(delta)}).`);
    }
    if (
      row?.margin_pct != null && !Number.isNaN(Number(row.margin_pct)) &&
      row?.target_margin_pct != null && !Number.isNaN(Number(row.target_margin_pct)) &&
      Number(row.margin_pct) < Number(row.target_margin_pct)
    ) {
      lines.push(`Gross margin is below the ${fmtPct1.format(Number(row.target_margin_pct))}% protein-aware target, so price or cost recovery should be reviewed before broad promotion.`);
    }
    if (
      row?.minimum_margin_pct != null && !Number.isNaN(Number(row.minimum_margin_pct)) &&
      row?.margin_pct != null && !Number.isNaN(Number(row.margin_pct)) &&
      Number(row.margin_pct) < Number(row.minimum_margin_pct)
    ) {
      lines.push(`Current gross margin is below the minimum ${fmtPct1.format(Number(row.minimum_margin_pct))}% gate, which makes the SKU an urgent pricing exception.`);
    }
    if (pricing.gapToTarget != null && pricing.gapToTarget < 0) {
      lines.push(`${pricing.currentLabel} is ${formatSignedMoney2(pricing.gapToTarget)} below target, so there is immediate pricing headroom if demand holds.`);
    }
    if (row?.uplift_pct != null && !Number.isNaN(Number(row.uplift_pct)) && Number(row.uplift_pct) > 0) {
      lines.push(`Target pricing implies roughly ${fmtPct1.format(row.uplift_pct)}% upside from the current realized price.`);
    }
    if (row?.top_customer_share != null && !Number.isNaN(Number(row.top_customer_share)) && Number(row.top_customer_share) >= 50) {
      lines.push(`Customer concentration is elevated: the top account contributes ${fmtPct1.format(row.top_customer_share)}% of SKU revenue.`);
    }
    if (row?.top_customer_name) {
      lines.push(`Top customer in the visible scope is ${row.top_customer_name}${row?.top_region_name ? ` and the lead region is ${row.top_region_name}.` : "."}`);
    } else if (row?.top_region_name) {
      lines.push(`The SKU is currently most concentrated in ${row.top_region_name}.`);
    }
    if (row?.customer_count != null && Number(row.customer_count) > 0 && Number(row.customer_count) <= 2) {
      lines.push(`Customer breadth is narrow at ${countLabel(Number(row.customer_count), "customer", "customers")}.`);
    }
    if (row?.weight != null && !Number.isNaN(Number(row.weight)) && Number(row.weight) > 0) {
      lines.push(`Visible shipped weight is ${fmtInt.format(row.weight)} lb, which matters for meat production and purchasing planning.`);
    }
    if (row?.velocity_per_month != null && !Number.isNaN(Number(row.velocity_per_month)) && Number(row.velocity_per_month) > 0) {
      lines.push(`Repeat demand is running at about ${formatVelocity(row.velocity_per_month)} orders per month in the current visible scope.`);
    }
    return lines.length ? lines : ["Use the full drilldown to inspect demand, pricing, customer mix, and planning relevance in more detail."];
  };

  const estimateElasticitySignal = (row = {}) => {
    const pricing = comparablePriceContext(row);
    const revenueDeltaPct = numericOrNull(row?.revenue_delta_pct);
    const price = numericOrNull(pricing.current);
    const median = numericOrNull(row?.up_p50 ?? row?.median_unit_price);
    const topCustomerShare = numericOrNull(row?.top_customer_share);
    const velocity = numericOrNull(row?.velocity_per_month ?? row?.orders_per_month);
    const premiumPct = (price != null && median != null && median > 0)
      ? ((price - median) / median) * 100
      : null;
    let value = 0.7;
    const reasons = [];

    if (premiumPct != null && premiumPct >= 18) {
      value += 0.48;
      reasons.push(`${fmtPct1.format(premiumPct)}% above median`);
    } else if (premiumPct != null && premiumPct >= 10) {
      value += 0.28;
      reasons.push(`${fmtPct1.format(premiumPct)}% above median`);
    }
    if (revenueDeltaPct != null && revenueDeltaPct <= -12) {
      value += 0.42;
      reasons.push(`${percentText(revenueDeltaPct)} revenue vs prior`);
    } else if (revenueDeltaPct != null && revenueDeltaPct <= -6) {
      value += 0.24;
      reasons.push(`${percentText(revenueDeltaPct)} revenue vs prior`);
    } else if (revenueDeltaPct != null && revenueDeltaPct >= 8) {
      value -= 0.08;
    }
    if (topCustomerShare != null && topCustomerShare >= 50) {
      value += 0.1;
      reasons.push(`${fmtPct1.format(topCustomerShare)}% concentrated demand`);
    }
    if (velocity != null && velocity >= 10) {
      value += 0.08;
    }

    const normalized = clamp(value, 0.45, 1.65);
    return {
      value: normalized,
      label: normalized >= 1.2 ? "High" : normalized >= 0.9 ? "Medium" : "Low",
      note: reasons.length
        ? reasons.join(` ${MIDDLE_DOT} `)
        : "Heuristic only; based on recent demand, price position, and customer concentration.",
      premiumPct,
      revenueDeltaPct,
    };
  };

  const buildElasticGuardrailWatch = (payload = {}) => {
    const bySku = new Map();
    const collect = (row) => {
      const sku = getSku(row);
      if (!sku) return;
      bySku.set(sku, resolveProductContextRow(row));
    };
    (payload?.table?.rows || []).forEach(collect);
    (payload?.price_vs_velocity || []).forEach(collect);
    (((payload?.performance_bubble || {}).points) || []).forEach(collect);

    const rows = Array.from(bySku.values())
      .map((row) => {
        const marginPct = numericOrNull(row?.margin_pct);
        const minimumMarginPct = numericOrNull(row?.minimum_margin_pct);
        const signal = estimateElasticitySignal(row);
        const statusKey = String(row?.status_key || "").toLowerCase();
        const hasStaticCoverage = marginPct != null && minimumMarginPct != null && marginPct >= minimumMarginPct;
        const withinStaticBand = !["red", "orange", "needs_mapping", "no_cost"].includes(statusKey);
        const qualifies = hasStaticCoverage
          && withinStaticBand
          && signal.premiumPct != null
          && signal.premiumPct >= 8
          && signal.revenueDeltaPct != null
          && signal.revenueDeltaPct <= -6;
        if (!qualifies) return null;
        return {
          ...row,
          elasticity_value: signal.value,
          elasticity_label: signal.label,
          elasticity_note: signal.note,
          price_premium_pct: signal.premiumPct,
        };
      })
      .filter(Boolean)
      .sort((a, b) => (numericOrNull(b?.revenue) || 0) - (numericOrNull(a?.revenue) || 0));

    return {
      rows: rows.slice(0, 6),
      count: rows.length,
      revenueAtWatch: rows.reduce((sum, row) => sum + (numericOrNull(row?.revenue) || 0), 0),
      highestLabel: rows[0]?.elasticity_label || EM_DASH,
    };
  };

  const buildRootCauseSummary = (payload = {}) => {
    const comparisonSummary = payload?.comparison_summary || {};
    const comparison = payload?.comparison || {};
    const risk = payload?.risk_opportunity || {};
    const execution = payload?.execution_lists || {};
    const kpis = payload?.kpis || {};
    const movers = Array.isArray((payload?.charts || {}).movers) ? payload.charts.movers : [];
    const compareLabel = comparison?.comparison_label || "prior comparable window";
    const revenueDeltaPct = numericOrNull(comparisonSummary?.revenue_delta_pct);
    const belowTargetCount = numericOrNull(risk?.below_target_count) || 0;
    const belowTargetRevenue = numericOrNull(risk?.below_target_revenue) || 0;
    const missingCost = numericOrNull(kpis?.missing_cost_sku_count) || 0;
    const negativeMover = [...movers]
      .filter((row) => (numericOrNull(row?.delta_revenue ?? row?.delta) || 0) < 0)
      .sort((a, b) => Math.abs(numericOrNull(b?.delta_revenue ?? b?.delta) || 0) - Math.abs(numericOrNull(a?.delta_revenue ?? a?.delta) || 0))[0];
    const positiveMover = [...movers]
      .filter((row) => (numericOrNull(row?.delta_revenue ?? row?.delta) || 0) > 0)
      .sort((a, b) => Math.abs(numericOrNull(b?.delta_revenue ?? b?.delta) || 0) - Math.abs(numericOrNull(a?.delta_revenue ?? a?.delta) || 0))[0];

    const headlineParts = [];
    if (revenueDeltaPct == null) headlineParts.push("Comparable trend is limited under the current filter window.");
    else if (revenueDeltaPct <= -8) headlineParts.push(`Revenue is down ${Math.abs(revenueDeltaPct).toFixed(1)}% versus the ${compareLabel}.`);
    else if (revenueDeltaPct >= 8) headlineParts.push(`Revenue is up ${revenueDeltaPct.toFixed(1)}% versus the ${compareLabel}.`);
    else headlineParts.push(`Revenue is broadly stable at ${revenueDeltaPct >= 0 ? "+" : ""}${revenueDeltaPct.toFixed(1)}% versus the ${compareLabel}.`);
    if (negativeMover) headlineParts.push(`${displayName(negativeMover)} is the largest negative mover.`);
    else if (positiveMover) headlineParts.push(`${displayName(positiveMover)} is leading portfolio growth.`);
    if (belowTargetCount) {
      headlineParts.push(`${fmtInt.format(belowTargetCount)} SKUs remain below target gross margin across ${fmtMoney0.format(belowTargetRevenue)} revenue.`);
    }

    const drivers = [];
    if (negativeMover) {
      drivers.push({
        kicker: "Demand driver",
        title: `${displayName(negativeMover)} softened`,
        detail: `${formatSignedMoney(negativeMover?.delta_revenue ?? negativeMover?.delta)} versus the ${compareLabel}. Review customer, promo, and price position before broad changes.`,
        action: { search: getSku(negativeMover), sortBy: "revenue_delta", sortDir: "asc", section: "table", mode: "analyst", tablePreset: "demand" },
      });
    } else if (positiveMover) {
      drivers.push({
        kicker: "Demand driver",
        title: `${displayName(positiveMover)} accelerated`,
        detail: `${formatSignedMoney(positiveMover?.delta_revenue ?? positiveMover?.delta)} versus the ${compareLabel}. Protect supply and service level on the winning line.`,
        action: { search: getSku(positiveMover), sortBy: "revenue_delta", sortDir: "desc", section: "table", mode: "analyst", tablePreset: "demand" },
      });
    }
    if (belowTargetCount) {
      drivers.push({
        kicker: "Pricing driver",
        title: `${fmtInt.format(belowTargetCount)} SKUs are below target margin`,
        detail: `${fmtMoney0.format(belowTargetRevenue)} of visible revenue is still sitting below target gross margin. This should remain the first pricing queue.`,
        action: { quickFilters: ["recover_margin"], section: "pricing", mode: "analyst", emphasis: "profit", tablePreset: "margin_risk" },
      });
    }
    if (missingCost) {
      drivers.push({
        kicker: "Coverage driver",
        title: `${fmtInt.format(missingCost)} SKUs are missing cost coverage`,
        detail: "Missing cost weakens target-price logic and execution ranking. Close these gaps before pushing broad pricing changes.",
        action: { quickFilters: ["missing_cost"], section: "execution", mode: "analyst", emphasis: "profit", tablePreset: "execution" },
      });
    } else if ((execution?.pricing_fixes || []).length) {
      const lead = execution.pricing_fixes[0];
      drivers.push({
        kicker: "Execution driver",
        title: `${displayName(lead)} leads the pricing queue`,
        detail: `${lead?.action || "Recover margin"} ${MIDDLE_DOT} ${lead?.profit_uplift_target != null ? `${fmtMoney0.format(lead.profit_uplift_target)} upside` : "largest economic upside in scope"}.`,
        action: { search: getSku(lead), quickFilters: lead?.quick_filters || ["recover_margin"], section: "pricing", mode: "analyst", emphasis: "profit", tablePreset: "margin_risk" },
      });
    }

    return {
      headline: headlineParts.join(" ").trim(),
      drivers: drivers.slice(0, 3),
    };
  };

  const buildAlertCandidates = (payload = {}, elasticWatch = {}) => {
    const risk = payload?.risk_opportunity || {};
    const execution = payload?.execution_lists || {};
    const kpis = payload?.kpis || {};
    const alerts = [];
    const belowMinimumCount = numericOrNull(risk?.below_minimum_count) || 0;
    const belowMinimumRevenue = numericOrNull(risk?.below_minimum_revenue) || 0;
    const missingCost = numericOrNull(kpis?.missing_cost_sku_count) || 0;

    if (belowMinimumCount) {
      alerts.push({
        channel: "Slack",
        title: "Margin floor breach",
        detail: `${fmtInt.format(belowMinimumCount)} SKUs slipped below minimum gross margin across ${fmtMoney0.format(belowMinimumRevenue)} revenue.`,
        tags: ["Pricing", "High confidence"],
        action: { quickFilters: ["below_minimum_margin"], section: "pricing", mode: "analyst", emphasis: "profit", tablePreset: "margin_risk" },
      });
    }
    if ((elasticWatch?.rows || []).length) {
      const lead = elasticWatch.rows[0];
      alerts.push({
        channel: "Teams",
        title: "Elasticity watch",
        detail: `${displayName(lead)} is ${fmtPct1.format(lead?.price_premium_pct || 0)} above its median price while recent revenue is weakening. Review before pushing further increases.`,
        tags: ["Demand", `${lead?.elasticity_label || "Medium"} sensitivity`],
        action: { search: getSku(lead), section: "pricing", mode: "analyst", tablePreset: "pricing_ladder" },
      });
    }
    if (missingCost) {
      alerts.push({
        channel: "Email",
        title: "Cost coverage gap",
        detail: `${fmtInt.format(missingCost)} SKUs are missing cost coverage, so margin and target-price alerts are less reliable until those rows are repaired.`,
        tags: ["Costing", "Data quality"],
        action: { quickFilters: ["missing_cost"], section: "execution", mode: "analyst", emphasis: "profit", tablePreset: "execution" },
      });
    }
    if ((execution?.promote_candidates || []).length && alerts.length < 3) {
      alerts.push({
        channel: "Slack",
        title: "Promote candidates ready",
        detail: `${fmtInt.format((execution.promote_candidates || []).length)} SKUs have healthy gross margin but low velocity. Sales can feature them without waiting for another export.`,
        tags: ["Commercial", "Growth"],
        action: { quickFilters: ["promote_candidate"], section: "execution", mode: "analyst", emphasis: "revenue", tablePreset: "execution" },
      });
    }
    return alerts.slice(0, 3);
  };

  const renderRootCauseSummary = (summary = {}) => {
    setText("rootCauseHeadline", summary?.headline || "Root-cause explanation will appear when the scorecard bundle loads.");
    const host = document.getElementById("rootCauseDrivers");
    if (host) {
      const rows = Array.isArray(summary?.drivers) ? summary.drivers : [];
      if (!rows.length) {
        host.innerHTML = '<div class="text-muted small">No root-cause drivers for the current view.</div>';
      } else {
        host.innerHTML = rows
          .map((driver, idx) => `
            <div class="root-cause-driver-card ${driver?.action ? "is-actionable" : ""}" data-root-cause-driver="${idx}">
              <div class="root-cause-driver-kicker">${escapeHtml(driver?.kicker || "Driver")}</div>
              <div class="root-cause-driver-title">${escapeHtml(driver?.title || "Review driver")}</div>
              <div class="root-cause-driver-detail">${escapeHtml(driver?.detail || "")}</div>
            </div>
          `)
          .join("");
        host.querySelectorAll("[data-root-cause-driver]").forEach((node) => {
          const idx = Number(node.getAttribute("data-root-cause-driver"));
          const driver = rows[idx];
          if (!driver?.action) return;
          makeInteractiveCard(node, () => applyDetailView(driver.action));
        });
      }
    }
    const openBtn = document.getElementById("rootCauseOpenTable");
    if (openBtn) {
      openBtn.onclick = () => applyDetailView((summary?.drivers || [])[0]?.action || { sortBy: "revenue_delta", sortDir: "asc", section: "table", mode: "analyst", tablePreset: "demand" });
    }
  };

  const renderAlertCandidates = (hostId, alerts = []) => {
    const host = document.getElementById(hostId);
    if (!host) return;
    const rows = Array.isArray(alerts) ? alerts : [];
    if (!rows.length) {
      host.innerHTML = '<span class="text-muted small">No outbound alert candidates for the current visible scope.</span>';
      return;
    }
    host.innerHTML = `<div class="alert-candidate-list">${rows
      .map((alert, idx) => `
        <div class="alert-candidate-card ${alert?.action ? "is-actionable" : ""}" data-alert-candidate="${hostId}:${idx}">
          <div class="alert-candidate-channel">${escapeHtml(alert?.channel || "Alert")}</div>
          <div class="alert-candidate-title">${escapeHtml(alert?.title || "Priority change")}</div>
          <div class="alert-candidate-detail">${escapeHtml(alert?.detail || "")}</div>
          <div class="alert-candidate-tags">
            ${(alert?.tags || []).map((tag) => `<span class="alert-candidate-tag">${escapeHtml(tag)}</span>`).join("")}
          </div>
        </div>
      `)
      .join("")}</div>`;
    host.querySelectorAll("[data-alert-candidate]").forEach((node) => {
      const idx = Number((node.getAttribute("data-alert-candidate") || "").split(":")[1]);
      const alert = rows[idx];
      if (!alert?.action) return;
      makeInteractiveCard(node, () => applyDetailView(alert.action));
    });
  };

  const renderElasticGuardrails = (watch = {}) => {
    const badge = document.getElementById("elasticGuardrailCount");
    if (badge) badge.textContent = `${fmtInt.format(watch?.count || 0)} watch`;
    setText("elasticGuardrailRevenue", fmtMoney0.format(watch?.revenueAtWatch || 0));
    setText("elasticGuardrailSensitivity", watch?.highestLabel || EM_DASH);
    const host = document.getElementById("elasticGuardrailList");
    if (!host) return;
    const rows = Array.isArray(watch?.rows) ? watch.rows : [];
    if (!rows.length) {
      host.innerHTML = '<span class="text-muted small">No demand-sensitive guardrail issues in the current scope.</span>';
      return;
    }
    host.innerHTML = rows
      .map((row, idx) => `
        <button type="button" class="btn btn-link p-0 text-start text-decoration-none elastic-watch-button" data-elastic-watch="${idx}">
          <span class="elastic-watch-main">
            <span class="elastic-watch-label">${escapeHtml(displayName(row))}</span>
            <span class="elastic-watch-meta">${escapeHtml(row?.elasticity_note || "")}</span>
          </span>
          <span class="elastic-watch-score">${escapeHtml(row?.elasticity_label || "Watch")} ${MIDDLE_DOT} ${row?.revenue != null ? fmtMoney0.format(row.revenue) : EM_DASH}</span>
        </button>
      `)
      .join("");
    host.querySelectorAll("[data-elastic-watch]").forEach((node) => {
      const idx = Number(node.getAttribute("data-elastic-watch"));
      const row = rows[idx];
      if (!row) return;
      node.addEventListener("click", () => {
        openDecisionWorkbench(row, { scroll: true, useRecommended: true, sourceLabel: "Elastic guardrail" });
        renderProductIntel(row, "Pricing Guardrails", "Elastic guardrails", "Revenue", row.revenue || 0);
      });
    });
  };

  const defaultScenarioPctForRow = (row = {}, suggestedAction = deriveSuggestedAction(row)) => {
    const pricing = comparablePriceContext(row);
    const current = numericOrNull(pricing.current);
    if (current == null || current <= 0) return 0;
    const target = numericOrNull(pricing.target);
    const minimum = numericOrNull(pricing.minimum);
    const actionText = String(row?.action || suggestedAction?.label || "").toLowerCase();
    if (actionText.includes("minimum") && minimum != null) return clamp(((minimum - current) / current) * 100, -12, 18);
    if ((actionText.includes("recover") || actionText.includes("target")) && target != null) return clamp(((target - current) / current) * 100, -12, 18);
    if (actionText.includes("reduce")) {
      const anchor = target != null ? target : (minimum != null ? minimum : current * 0.94);
      return clamp(((anchor - current) / current) * 100, -12, 6);
    }
    return 0;
  };

  const buildScenarioProjection = (selection = {}) => {
    const row = selection?.row || {};
    const pricing = comparablePriceContext(row);
    const costs = costContext(row, pricing);
    const currentPrice = numericOrNull(pricing.current);
    const currentRevenue = numericOrNull(row?.revenue);
    const currentProfit = numericOrNull(row?.profit);
    const elasticity = estimateElasticitySignal(row);
    const scenarioPct = numericOrNull(selection?.scenarioPct) || 0;
    const hasScenario = currentPrice != null && currentPrice > 0;
    const baselineVolume = pricing.basisLabel === "per lb"
      ? (numericOrNull(row?.weight) || (currentRevenue != null && currentPrice > 0 ? currentRevenue / currentPrice : null))
      : (numericOrNull(row?.qty) || (currentRevenue != null && currentPrice > 0 ? currentRevenue / currentPrice : null));
    const scenarioPrice = hasScenario ? currentPrice * (1 + (scenarioPct / 100)) : null;
    const volumeFactor = clamp(1 - ((elasticity.value || 0.8) * (scenarioPct / 100)), 0.45, 1.75);
    const projectedVolume = baselineVolume != null ? baselineVolume * volumeFactor : null;
    const projectedRevenue = (scenarioPrice != null && projectedVolume != null) ? scenarioPrice * projectedVolume : null;
    const projectedProfit = (projectedVolume != null && scenarioPrice != null && numericOrNull(costs.effective) != null)
      ? (scenarioPrice - numericOrNull(costs.effective)) * projectedVolume
      : null;
    return {
      pricing,
      costs,
      elasticity,
      hasScenario,
      scenarioPct,
      scenarioPrice,
      projectedVolume,
      projectedRevenue,
      projectedProfit,
      revenueDelta: projectedRevenue != null && currentRevenue != null ? projectedRevenue - currentRevenue : null,
      profitDelta: projectedProfit != null && currentProfit != null ? projectedProfit - currentProfit : null,
      volumeUnitLabel: pricing.basisLabel === "per lb" ? "lb" : "units",
    };
  };

  const renderStagedActions = () => {
    const badge = document.getElementById("stagedActionCountBadge");
    if (badge) badge.textContent = `${fmtInt.format((state.stagedActions || []).length)} staged`;
    const host = document.getElementById("stagedActionsList");
    if (!host) return;
    const rows = Array.isArray(state.stagedActions) ? state.stagedActions : [];
    if (!rows.length) {
      host.innerHTML = '<span class="text-muted small">No staged actions yet.</span>';
      return;
    }
    host.innerHTML = `<div class="staged-action-list">${rows
      .map((row, idx) => `
        <div class="staged-action-card is-actionable" data-staged-action="${idx}">
          <div class="d-flex justify-content-between gap-2">
            <div>
              <div class="staged-action-owner">${escapeHtml(row?.owner || "Decision")}</div>
              <div class="staged-action-title">${escapeHtml(row?.display_name || row?.sku || "Staged action")}</div>
            </div>
            <button type="button" class="btn btn-sm btn-outline-secondary" data-staged-remove="${idx}">Remove</button>
          </div>
          <div class="staged-action-detail">${escapeHtml(row?.summary || "")}</div>
          <div class="staged-action-meta">
            ${row?.scenario_price != null ? `<span class="staged-action-chip">Price ${fmtMoney2.format(row.scenario_price)}</span>` : ""}
            ${row?.projected_profit != null ? `<span class="staged-action-chip">Proj. profit ${fmtMoney0.format(row.projected_profit)}</span>` : ""}
            ${row?.scenario_pct != null ? `<span class="staged-action-chip">${row.scenario_pct > 0 ? "+" : ""}${fmtPct1.format(row.scenario_pct)}%</span>` : ""}
          </div>
        </div>
      `)
      .join("")}</div>`;
    host.querySelectorAll("[data-staged-remove]").forEach((node) => {
      node.addEventListener("click", (evt) => {
        evt.stopPropagation();
        const idx = Number(node.getAttribute("data-staged-remove"));
        state.stagedActions.splice(idx, 1);
        writeStagedActions();
        renderStagedActions();
      });
    });
    host.querySelectorAll("[data-staged-action]").forEach((node) => {
      const idx = Number(node.getAttribute("data-staged-action"));
      const row = rows[idx];
      if (!row) return;
      makeInteractiveCard(node, () => {
        openDecisionWorkbench(row, { scroll: true, sourceLabel: "Staged action" });
        if (row?.table_action) applyDetailView(row.table_action);
      });
    });
  };

  const renderDecisionWorkbench = () => {
    const emptyState = document.getElementById("workbenchEmptyState");
    const panel = document.getElementById("decisionWorkbenchPanel");
    const selection = state.workbenchSelection;
    if (!emptyState || !panel) return;
    if (!selection?.row) {
      emptyState.classList.remove("d-none");
      panel.classList.add("d-none");
      return;
    }
    emptyState.classList.add("d-none");
    panel.classList.remove("d-none");
    const suggestedAction = selection.suggestedAction || deriveSuggestedAction(selection.row);
    const scenario = buildScenarioProjection(selection);
    setText("workbenchProductLabel", displayName(selection.row));
    setText("workbenchActionLabel", `${suggestedAction.label}. ${suggestedAction.note}`);
    setText("workbenchCurrentPrice", scenario.pricing.current != null ? fmtMoney2.format(scenario.pricing.current) : EM_DASH);
    setText("workbenchScenarioPrice", scenario.scenarioPrice != null ? fmtMoney2.format(scenario.scenarioPrice) : EM_DASH);
    setText("workbenchProjectedVolume", scenario.projectedVolume != null ? `${fmtNum1.format(scenario.projectedVolume)} ${scenario.volumeUnitLabel}` : EM_DASH);
    setText("workbenchProjectedRevenue", scenario.projectedRevenue != null ? fmtMoney0.format(scenario.projectedRevenue) : EM_DASH);
    setText("workbenchProjectedProfit", scenario.projectedProfit != null ? fmtMoney0.format(scenario.projectedProfit) : EM_DASH);
    setText("workbenchElasticity", `${scenario.elasticity.label} (${fmtNum1.format(scenario.elasticity.value)})`);
    setText(
      "workbenchScenarioMeta",
      scenario.hasScenario
        ? `${scenario.scenarioPct > 0 ? "+" : ""}${fmtPct1.format(scenario.scenarioPct)}% vs current ${MIDDLE_DOT} Revenue ${scenario.revenueDelta != null ? formatSignedMoney(scenario.revenueDelta) : EM_DASH} ${MIDDLE_DOT} Profit ${scenario.profitDelta != null ? formatSignedMoney(scenario.profitDelta) : EM_DASH}`
        : "This action is not price-modelable because current price or volume basis is missing."
    );
    setText(
      "workbenchScenarioNarrative",
      scenario.hasScenario
        ? `Scenario assumes ${scenario.elasticity.label.toLowerCase()} demand sensitivity using recent demand and price position. Volume moves first, then the model rolls revenue and gross profit from the new realized price.`
        : "Current price, target price, or volume basis is missing, so this workbench can only stage the action without a price-impact forecast."
    );
    const range = document.getElementById("workbenchScenarioRange");
    if (range) {
      range.value = String(scenario.scenarioPct || 0);
      range.disabled = !scenario.hasScenario;
    }
    const useRecommendedBtn = document.getElementById("workbenchUseRecommended");
    if (useRecommendedBtn) useRecommendedBtn.disabled = !scenario.hasScenario;
    const stageBtn = document.getElementById("workbenchStageAction");
    if (stageBtn) stageBtn.disabled = !selection.row;
  };

  const openDecisionWorkbench = (row = {}, options = {}) => {
    const contextRow = resolveProductContextRow(row);
    const sku = getSku(contextRow);
    if (!sku) return;
    const suggestedAction = deriveSuggestedAction(contextRow);
    const recommendedPct = defaultScenarioPctForRow(contextRow, suggestedAction);
    const previous = state.workbenchSelection;
    const preserveScenario = getSku(previous?.row) === sku && !options.useRecommended;
    state.workbenchSelection = {
      row: contextRow,
      suggestedAction,
      recommendedPct,
      scenarioPct: preserveScenario ? (previous?.scenarioPct ?? recommendedPct) : recommendedPct,
      owner: options.sourceLabel || suggestedAction.label,
    };
    renderDecisionWorkbench();
    if (options.scroll) {
      if (!state.visibleSections.includes("execution")) {
        state.visibleSections = [...state.visibleSections, "execution"];
        applyWorkspaceSettings();
      }
      setTimeout(() => scrollToSection("execution"), 90);
    }
  };

  const stageWorkbenchAction = () => {
    const selection = state.workbenchSelection;
    if (!selection?.row) return;
    const scenario = buildScenarioProjection(selection);
    const sku = getSku(selection.row);
    if (!sku) return;
    const suggestedAction = selection.suggestedAction || deriveSuggestedAction(selection.row);
    const staged = {
      ...selection.row,
      sku,
      display_name: displayName(selection.row),
      owner: selection.owner || suggestedAction.label,
      scenario_pct: selection.scenarioPct,
      scenario_price: scenario.scenarioPrice,
      projected_revenue: scenario.projectedRevenue,
      projected_profit: scenario.projectedProfit,
      summary: `${suggestedAction.label}${scenario.scenarioPrice != null ? ` at ${fmtMoney2.format(scenario.scenarioPrice)}` : ""}`,
      table_action: {
        search: sku,
        quickFilters: suggestedAction?.view?.quickFilters || suggestedAction?.view?.quick_filters || [],
        section: "table",
        mode: "analyst",
        tablePreset: inferTablePreset(suggestedAction?.view || {}) || "execution",
      },
    };
    const existingIdx = (state.stagedActions || []).findIndex((row) => getSku(row) === sku);
    if (existingIdx >= 0) state.stagedActions.splice(existingIdx, 1, staged);
    else state.stagedActions.unshift(staged);
    state.stagedActions = state.stagedActions.slice(0, 8);
    writeStagedActions();
    renderStagedActions();
  };

  const renderActiveFilterSummary = () => {
    const host = document.getElementById("activeFilterSummary");
    if (!host) return;
    const filters = currentFilterState();
    const parts = [];
    Object.entries(filters || {}).forEach(([key, rawValue]) => {
      if (rawValue == null || rawValue === "") return;
      const values = Array.isArray(rawValue) ? rawValue.filter(Boolean) : [rawValue].filter(Boolean);
      if (!values.length) return;
      const label = key.replace(/_/g, " ").replace(/\b\w/g, (m) => m.toUpperCase());
      const named = typeof window.getFilterLabels === "function" ? window.getFilterLabels(key, values) : values;
      const preview = named.slice(0, 2).join(", ");
      const remainder = named.length > 2 ? ` +${named.length - 2} more` : "";
      parts.push(`${label}: ${preview}${remainder}`);
    });
    const localRefinements = [];
    if (state.segments?.length) {
      localRefinements.push(`Segments: ${state.segments.slice(0, 2).join(", ")}${state.segments.length > 2 ? ` +${state.segments.length - 2} more` : ""}`);
    }
    if (state.quickFilters?.length) {
      localRefinements.push(`Watchlists: ${state.quickFilters.join(", ")}`);
    }
    if (state.search) {
      localRefinements.push(`Search: ${state.search}`);
    }
    const scopeText = parts.length
      ? `Portfolio KPIs and charts use this global scope: ${parts.join(" | ")}.`
      : "Portfolio KPIs and charts use the current RBAC scope and visible filter window.";
    const localText = localRefinements.length
      ? ` Table workspace refinements only narrow the paginated exploration view: ${localRefinements.join(" | ")}.`
      : "";
    host.textContent = `${scopeText}${localText}`;
  };

  const renderSectionBriefs = (payload = {}) => {
    const comparison = payload?.comparison || {};
    const concentration = payload?.concentration || {};
    const risk = payload?.risk_opportunity || {};
    const posture = payload?.portfolio_posture || {};
    const protein = payload?.protein_insights || {};
    const focusActions = Array.isArray(payload?.focus_actions) ? payload.focus_actions : [];
    const pricingRows = Array.isArray(payload?.pricing_guardrails?.rows) ? payload.pricing_guardrails.rows.length : 0;
    const movers = Array.isArray(payload?.charts?.movers) ? payload.charts.movers : [];
    const watchlistText = state.quickFilters?.length ? `Active watchlists: ${state.quickFilters.join(", ")}.` : "No extra watchlist filters are applied.";
    setText(
      "strategyLayerContext",
      `${posture?.headline || "Portfolio posture will appear here."} ${concentration?.top10_share != null ? `Top 10 SKUs represent ${fmtPct1.format(concentration.top10_share)}% of filtered revenue.` : ""}`.trim()
    );
    setText(
      "demandLayerContext",
      `${comparison?.note || "Demand comparisons follow the current filtered window."} ${movers.length ? `Top movers are ranked inside this exact scope.` : ""}`.trim()
    );
    setText(
      "pricingLayerContext",
      `${fmtInt.format(risk?.below_minimum_count ?? 0)} SKUs are below minimum and ${fmtInt.format(risk?.below_target_count ?? 0)} are below target across ${fmtMoney0.format(risk?.below_target_revenue ?? 0)} of visible revenue; ${fmtInt.format(pricingRows)} pricing actions are ready for review.`
    );
    setText(
      "executionLayerContext",
      focusActions[0]?.detail || "The execution layer ranks the next best pricing, commercial, and planning moves for the current scope."
    );
    setText(
      "assortmentLayerContext",
      `${concentration?.top10_share != null ? `Top 10 SKUs represent ${fmtPct1.format(concentration.top10_share)}% of visible revenue.` : "Assortment concentration is being calculated."} ${protein?.summary?.top_family ? `${protein.summary.top_family} leads the visible protein mix${protein.summary.top_family_share != null ? ` at ${fmtPct1.format(protein.summary.top_family_share)}%.` : "."}` : ""} ${concentration?.skus_to_80 ? `${fmtInt.format(concentration.skus_to_80)} SKUs reach 80% of revenue.` : ""}`.trim()
    );
    setText(
      "tableLayerContext",
      `${comparison?.comparison_label || "Current vs prior comparable columns"} stay aligned with the current scope. ${watchlistText}`
    );
  };

  const cleanupProductIntelPanel = () => {
    if (document.querySelector(".offcanvas.show")) return;
    document.body.classList.remove("modal-open");
    document.body.style.removeProperty("overflow");
    document.body.style.removeProperty("padding-right");
    document.querySelectorAll(".offcanvas-backdrop").forEach((node) => node.remove());
  };

  const hideProductIntel = () => {
    const panel = document.getElementById("productIntelPanel");
    if (!panel) return;
    try {
      if (productIntelOffcanvas) {
        productIntelOffcanvas.hide();
        return;
      }
    } catch (err) {
      console.error("product intel hide", err);
    }
    panel.classList.remove("show");
    panel.style.visibility = "";
    panel.setAttribute("aria-hidden", "true");
    activeProductIntel = null;
    cleanupProductIntelPanel();
  };

  const initProductIntelPanel = () => {
    const panel = document.getElementById("productIntelPanel");
    if (!panel || panel.dataset.bound === "1") return;
    panel.dataset.bound = "1";
    panel.querySelectorAll('[data-bs-dismiss="offcanvas"]').forEach((node) => {
      node.addEventListener("click", (evt) => {
        evt.preventDefault();
        hideProductIntel();
      });
    });
    panel.addEventListener("hidden.bs.offcanvas", () => {
      activeProductIntel = null;
      cleanupProductIntelPanel();
    });
    window.addEventListener("popstate", () => {
      if (panel.classList.contains("show")) hideProductIntel();
    });
  };

  const renderProductIntel = (row, section, widget, metric, value) => {
    const panel = document.getElementById("productIntelPanel");
    if (!panel) return false;
    initProductIntelPanel();
    const contextRow = resolveProductContextRow(row);
    const sku = getSku(contextRow);
    if (!sku) return false;
    const suggestedAction = deriveSuggestedAction(contextRow);
    activeProductIntel = { row: contextRow, suggestedAction, section, widget, metric, value };

    setText("productIntelPanelLabel", displayName(contextRow));
    setText("productIntelSource", `${section || "Product Intelligence"} ${MIDDLE_DOT} ${widget || "Selected interaction"}`);
    setText("productIntelHeadline", `${displayName(contextRow)} under the current visible scope`);
    setText(
      "productIntelSubhead",
      `${(lastPayload?.comparison || {}).comparison_label || "Current vs prior comparable logic"} ${MIDDLE_DOT} ${contextRow?.segment || "Unclassified segment"}`
    );
    setText("productIntelContextNote", (lastPayload?.comparison || {}).note || "This panel inherits the current filter scope and comparable-period logic.");
    const pricing = comparablePriceContext(contextRow);
    const costs = costContext(contextRow, pricing);
    const pricingStatusKey = visualStatusKey(contextRow);
    const pricingStatus = statusMeta(pricingStatusKey);
    const marginStatusKey = String(contextRow?.status_key || contextRow?.margin_status || "").toLowerCase();
    const marginStatus = statusMeta(marginStatusKey);
    const topCustomerText = contextRow?.top_customer_name
      ? `${contextRow.top_customer_name}${contextRow?.top_customer_share != null ? ` ${MIDDLE_DOT} ${fmtPct1.format(contextRow.top_customer_share)}%` : ""}`
      : (contextRow?.top_customer_share != null ? `${fmtPct1.format(contextRow.top_customer_share)}%` : EM_DASH);
    const topRegionText = contextRow?.top_region_name
      ? `${contextRow.top_region_name}${contextRow?.top_region_share != null ? ` ${MIDDLE_DOT} ${fmtPct1.format(contextRow.top_region_share)}%` : ""}`
      : EM_DASH;
    setHtml(
      "productIntelStatusRow",
      [
        renderRiskBadge(visualStatusLabel(contextRow), pricingStatusKey),
        pricingStatusKey && marginStatusKey && pricingStatusKey !== marginStatusKey
          ? `<span class="product-intel-pill">Margin band ${escapeHtml(contextRow?.target_status || marginStatus.label)}</span>`
          : "",
        `<span class="product-intel-pill">${escapeHtml(contextRow?.protein_family || contextRow?.rule_family || "Protein unassigned")}</span>`,
        contextRow?.product_category ? `<span class="product-intel-pill">${escapeHtml(contextRow.product_category)}</span>` : "",
        contextRow?.target_achievement_pct != null ? `<span class="product-intel-pill">Target achievement ${escapeHtml(`${fmtPct1.format(contextRow.target_achievement_pct)}%`)}</span>` : "",
      ].filter(Boolean).join("")
    );
    setHtml(
      "productIntelPricingSummary",
      `
        <div class="product-intel-pricing-grid">
          <div class="product-intel-pricing-cell">
            <div class="product-intel-pricing-label">${escapeHtml(pricing.currentLabel)}</div>
            <div class="product-intel-pricing-value">${escapeHtml(pricing.current != null ? fmtMoney2.format(pricing.current) : EM_DASH)}</div>
          </div>
          <div class="product-intel-pricing-cell">
            <div class="product-intel-pricing-label">${escapeHtml(pricing.minLabel)}</div>
            <div class="product-intel-pricing-value">${escapeHtml(pricing.minimum != null ? fmtMoney2.format(pricing.minimum) : EM_DASH)}</div>
          </div>
          <div class="product-intel-pricing-cell">
            <div class="product-intel-pricing-label">${escapeHtml(pricing.targetLabel)}</div>
            <div class="product-intel-pricing-value">${escapeHtml(pricing.target != null ? fmtMoney2.format(pricing.target) : EM_DASH)}</div>
          </div>
        </div>
        <div class="text-muted small mt-2">
          ${escapeHtml(`Gap to minimum: ${pricing.gapToMin != null ? formatSignedMoney2(pricing.gapToMin) : EM_DASH}`)}
          ${MIDDLE_DOT}
          ${escapeHtml(`Gap to target: ${pricing.gapToTarget != null ? formatSignedMoney2(pricing.gapToTarget) : EM_DASH}`)}
        </div>
      `
    );

    const stats = [
      ["Revenue", contextRow?.revenue != null ? fmtMoney0.format(contextRow.revenue) : EM_DASH],
      ["Profit", contextRow?.profit != null ? fmtMoney0.format(contextRow.profit) : EM_DASH],
      ["ASP", contextRow?.current_unit_price != null ? fmtMoney2.format(contextRow.current_unit_price) : EM_DASH],
      ["ASP / lb", contextRow?.asp_lb != null ? fmtMoney2.format(contextRow.asp_lb) : EM_DASH],
      [costs.baseLabel, costs.base != null ? fmtMoney2.format(costs.base) : EM_DASH],
      [costs.effectiveLabel, costs.effective != null ? fmtMoney2.format(costs.effective) : EM_DASH],
      ["Min Price / lb", contextRow?.minimum_price_lb != null ? fmtMoney2.format(contextRow.minimum_price_lb) : EM_DASH],
      ["Target Price / lb", contextRow?.target_price_lb != null ? fmtMoney2.format(contextRow.target_price_lb) : EM_DASH],
      ["Gap to Minimum", pricing.gapToMin != null ? formatSignedMoney2(pricing.gapToMin) : EM_DASH],
      ["Gap to Target", pricing.gapToTarget != null ? formatSignedMoney2(pricing.gapToTarget) : EM_DASH],
      ["Current Gross Margin %", contextRow?.margin_pct != null ? `${fmtPct1.format(contextRow.margin_pct)}%` : EM_DASH],
      ["Min Gross Margin %", contextRow?.minimum_margin_pct != null ? `${fmtPct1.format(contextRow.minimum_margin_pct)}%` : EM_DASH],
      ["Target Gross Margin %", contextRow?.target_margin_pct != null ? `${fmtPct1.format(contextRow.target_margin_pct)}%` : EM_DASH],
      ["Target Achievement", contextRow?.target_achievement_pct != null ? `${fmtPct1.format(contextRow.target_achievement_pct)}%` : EM_DASH],
      ["Profit Uplift to Target", contextRow?.profit_uplift_target != null ? fmtMoney0.format(contextRow.profit_uplift_target) : EM_DASH],
      ["Revenue Δ %", contextRow?.revenue_delta_pct != null ? formatRevenueDeltaPct(contextRow) : EM_DASH],
      ["Margin Δ pp", contextRow?.margin_delta_pp != null ? `${contextRow.margin_delta_pp > 0 ? "+" : ""}${fmtPct1.format(contextRow.margin_delta_pp)} pp` : EM_DASH],
      ["Customers", contextRow?.customer_count != null ? fmtInt.format(contextRow.customer_count) : EM_DASH],
      ["Top Customer", topCustomerText],
      ["Top Region", topRegionText],
      ["Velocity / mo", contextRow?.velocity_per_month != null ? formatVelocity(contextRow.velocity_per_month) : (contextRow?.orders_per_month != null ? formatVelocity(contextRow.orders_per_month) : EM_DASH)],
      ["Shipped lb", contextRow?.weight != null ? `${fmtInt.format(contextRow.weight)} lb` : EM_DASH],
      ["Quantity", contextRow?.qty != null ? fmtInt.format(contextRow.qty) : EM_DASH],
    ];
    setHtml(
      "productIntelStats",
      stats
        .map(
          ([label, statValue]) => `
            <div class="product-intel-stat">
              <div class="product-intel-stat-label">${escapeHtml(label)}</div>
              <div class="product-intel-stat-value">${escapeHtml(statValue)}</div>
            </div>
          `
        )
        .join("")
    );

    const whyLines = buildProductWhyLines(contextRow, section, widget, metric, value);
    setHtml("productIntelWhy", `<ul class="product-intel-list mb-0">${whyLines.map((line) => `<li>${escapeHtml(line)}</li>`).join("")}</ul>`);
    setText("productIntelAction", `${suggestedAction.label}. ${suggestedAction.note}`);

    const drilldownHref = drilldownTemplate
      ? appendFiltersToUrl(drilldownTemplate.replace("__PID__", encodeURIComponent(sku)))
      : (contextRow?.intel_url ? appendFiltersToUrl(contextRow.intel_url) : "#");
    const drilldownBtn = document.getElementById("productIntelOpenDrilldown");
    if (drilldownBtn) {
      drilldownBtn.href = drilldownHref;
      drilldownBtn.classList.toggle("disabled", drilldownHref === "#");
      drilldownBtn.setAttribute("aria-disabled", drilldownHref === "#" ? "true" : "false");
    }
    const focusBtn = document.getElementById("productIntelFocusTable");
    if (focusBtn) focusBtn.disabled = !sku;
    const applyBtn = document.getElementById("productIntelApplyAction");
    if (applyBtn) applyBtn.disabled = !sku;

    if (typeof bootstrap !== "undefined" && bootstrap?.Offcanvas && panel) {
      productIntelOffcanvas = productIntelOffcanvas || bootstrap.Offcanvas.getOrCreateInstance(panel);
      productIntelOffcanvas.show();
      return true;
    }
    panel.classList.add("show");
    panel.style.visibility = "visible";
    panel.removeAttribute("aria-hidden");
    return true;
  };

  const tooltipInstanceKey = "__productsTooltipBound";
  const hydrateTooltips = (scope = document) => {
    if (typeof bootstrap === "undefined" || !bootstrap.Tooltip) return;
    scope.querySelectorAll('[data-bs-toggle="tooltip"]').forEach((el) => {
      if (el[tooltipInstanceKey]) return;
      el[tooltipInstanceKey] = true;
      new bootstrap.Tooltip(el);
    });
  };

  const appendInfoButton = (target, text) => {
    if (!target || !text || target.querySelector?.(".info-dot")) return;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn-link btn-sm p-0 border-0 info-dot";
    btn.setAttribute("data-bs-toggle", "tooltip");
    btn.setAttribute("data-bs-placement", "top");
    btn.setAttribute("title", text);
    btn.setAttribute("aria-label", "What this means");
    btn.innerHTML = '<i class="bi bi-info-circle"></i>';
    target.appendChild(document.createTextNode(" "));
    target.appendChild(btn);
  };

  const explainMetricValue = (valueId, text) => {
    const valueEl = document.getElementById(valueId);
    if (!valueEl || !valueEl.previousElementSibling) return;
    appendInfoButton(valueEl.previousElementSibling, text);
  };

  const initV2Help = () => {
    if (!isV2) return;
    appendInfoButton(root.querySelector(".products-hero h2"), V2_TOOLTIP_TEXT.heroTitle);
    [
      ["velAvgWeekly", V2_TOOLTIP_TEXT.velocityPulse],
      ["velW13", V2_TOOLTIP_TEXT.velocityPulse],
      ["velWeeklyRevenue", V2_TOOLTIP_TEXT.velocityPulse],
      ["velRevPerProduct", V2_TOOLTIP_TEXT.velocityPulse],
      ["velActive", V2_TOOLTIP_TEXT.velocityPulse],
      ["velRoi", V2_TOOLTIP_TEXT.velocityPulse],
      ["insightMomentum", V2_TOOLTIP_TEXT.momentum],
      ["insightTopProduct", V2_TOOLTIP_TEXT.topProduct],
      ["momDelta", V2_TOOLTIP_TEXT.momDelta],
      ["predictiveRev", V2_TOOLTIP_TEXT.projectedNextMonth],
      ["kpiRevenue", V2_TOOLTIP_TEXT.totalRevenue],
      ["kpiQty", V2_TOOLTIP_TEXT.totalQuantity],
      ["kpiWeight", V2_TOOLTIP_TEXT.totalWeight],
      ["kpiUnique", V2_TOOLTIP_TEXT.activeProducts],
      ["kpiCustomers", V2_TOOLTIP_TEXT.activeCustomers],
      ["kpiMargin", V2_TOOLTIP_TEXT.avgMargin],
      ["kpiAvgPrice", V2_TOOLTIP_TEXT.avgUnitPrice],
      ["kpiMedianPrice", V2_TOOLTIP_TEXT.medianUnitPrice],
      ["kpiRevPerProduct", V2_TOOLTIP_TEXT.revenuePerProduct],
      ["kpiRevPerCustomer", V2_TOOLTIP_TEXT.revenuePerCustomer],
      ["aiMarginRisk", V2_TOOLTIP_TEXT.aiSignals],
      ["aiPricing", V2_TOOLTIP_TEXT.aiSignals],
    ].forEach(([id, text]) => explainMetricValue(id, text));
    [
      ["#products-trajectory .col-xl-8 .card-title", V2_TOOLTIP_TEXT.trajectory],
      ["#products-trajectory .col-xl-4 .card:first-child .card-title", V2_TOOLTIP_TEXT.priceVelocity],
      ["#products-risk-opportunity .col-xl-5 .card-title", V2_TOOLTIP_TEXT.recommendations],
      ["#products-pricing .col-12 .card-title", V2_TOOLTIP_TEXT.performanceBubble],
      ["#products-pricing .col-lg-6 .card-title", V2_TOOLTIP_TEXT.priceDistribution],
      ["#products-pricing .col-lg-6 + .col-lg-6 .card-title", V2_TOOLTIP_TEXT.topMovers],
      ["#products-health .card-title", V2_TOOLTIP_TEXT.healthMatrix],
      ["#products-segments .col-xl-3:nth-child(1) .card-title", V2_TOOLTIP_TEXT.segmentSummary],
      ["#products-segments .col-xl-3:nth-child(2) .card-title", V2_TOOLTIP_TEXT.segmentMovers],
      ["#products-segments .col-xl-3:nth-child(4) .card-title", V2_TOOLTIP_TEXT.proteinFamily],
      ["#products-segments + section .col-xl-6:first-child .card-title", V2_TOOLTIP_TEXT.topProducts],
      ["#products-segments + section .col-xl-6:last-child .card-title", V2_TOOLTIP_TEXT.pareto],
      ["#products-table .card-title", V2_TOOLTIP_TEXT.table],
    ].forEach(([selector, text]) => {
      if (!text) return;
      appendInfoButton(document.querySelector(selector), text);
    });
    hydrateTooltips(document);
  };

  // ---------- Render helpers ----------
  const renderHero = (kpis = {}, meta = {}, comparison = {}) => {
    const windowLbl = comparison?.current_window_label
      || (meta.window && (meta.window.start || meta.window.end)
        ? `${meta.window.start || "?"} ${ARROW} ${meta.window.end || "?"}`
        : "Live filters");
    setText("heroDateRange", windowLbl);
    setText("kpiUniqueHero", fmtInt.format(kpis.products ?? kpis.rows ?? 0));
    setText("kpiCustomersHero", fmtInt.format(kpis.customers ?? 0));
  };

  const renderComparisonContext = (comparison = {}, story = {}) => {
    const currentLabel = comparison?.current_short_label || "Current";
    const priorLabel = comparison?.prior_short_label || "Prior";
    ACTIVE_COLUMN_DEFS.forEach((col) => {
      if (col.key === "revenue_current") col.label = `Revenue ${currentLabel}`;
      if (col.key === "revenue_prior") col.label = `Revenue ${priorLabel}`;
      if (col.key === "orders_current") col.label = `Orders ${currentLabel}`;
      if (col.key === "orders_prior") col.label = `Orders ${priorLabel}`;
      if (col.key === "profit_current") col.label = `Profit ${currentLabel}`;
      if (col.key === "profit_prior") col.label = `Profit ${priorLabel}`;
    });
    setText("comparisonContextNote", comparison?.note || "Current window and prior comparable logic will appear here.");
    setText("portfolioStoryNote", story?.headline || "");
    setText("insightDeltaLabel", comparison?.current_short_label ? `${comparison.current_short_label} revenue` : "Revenue comparison");
    setText("comparisonDeltaLabel", comparison?.comparison_label || "Comparison delta");
    setText("moversContextNote", comparison?.comparison_label || "Current window vs prior comparable window.");
    setText("thRevenueCurrent", `Revenue (${comparison?.current_short_label || "Current"})`);
    setText("thRevenuePrior", `Revenue (${comparison?.prior_short_label || "Prior"})`);
    setText("thRevenueDeltaPct", `Δ Revenue %`);
    setText("thOrdersCurrent", `Orders (${comparison?.current_short_label || "Current"})`);
    setText("thOrdersPrior", `Orders (${comparison?.prior_short_label || "Prior"})`);
    setText("thProfitCurrent", `Profit (${comparison?.current_short_label || "Current"})`);
    setText("thProfitPrior", `Profit (${comparison?.prior_short_label || "Prior"})`);
    renderColumnChooser();
  };

  const renderPortfolioPosture = (posture = {}, focusActions = []) => {
    const headline = posture?.headline || "Review portfolio posture";
    const detail = posture?.detail || "Signals will summarize the dominant portfolio stance here.";
    setText("heroPostureSummary", headline);
    setText("portfolioPostureTitle", headline);
    setText("portfolioPostureDetail", detail);
    const actionSummary = (Array.isArray(focusActions) && focusActions[0]?.title) || "Review the top execution queue";
    setText("heroActionSummary", actionSummary);

    const badge = document.getElementById("heroPostureBadge");
    if (badge) {
      const quadrant = posture?.quadrant || "";
      badge.textContent = quadrant || "";
      badge.classList.toggle("d-none", !quadrant);
    }

    const stats = [];
    if (posture?.quadrant) stats.push(`${posture.quadrant}`);
    if (posture?.revenue_share != null) stats.push(`${fmtPct1.format(posture.revenue_share)}% of revenue`);
    setHtml(
      "portfolioPostureStats",
      stats.map((item) => `<span class="brief-pill">${escapeHtml(item)}</span>`).join("")
    );
  };

  const renderDecisionSignals = (signals = []) => {
    const host = document.getElementById("decisionSignalsBand");
    if (!host) return;
    const rows = Array.isArray(signals) ? signals.slice(0, 5) : [];
    if (!rows.length) {
      host.innerHTML = `
        <div class="decision-signal-card tone-neutral">
          <div class="decision-signal-label">Signals</div>
          <div class="decision-signal-value">Unavailable</div>
          <div class="decision-signal-note">Decision signals will appear when the bundle loads.</div>
        </div>
      `;
      return;
    }
    host.innerHTML = rows
      .map(
        (signal, idx) => `
          <div class="decision-signal-card tone-${escapeHtml(signal?.tone || "neutral")} ${signal?.action ? "has-action" : ""}" data-signal-idx="${idx}">
            <div class="decision-signal-label">${escapeHtml(signal?.label || "Signal")}</div>
            <div class="decision-signal-value">${escapeHtml(signal?.value || EM_DASH)}</div>
            <div class="decision-signal-note">${escapeHtml(signal?.note || "")}</div>
            ${signal?.action ? '<div class="decision-signal-note fw-semibold mt-2">Open detail view</div>' : ""}
          </div>
        `
      )
      .join("");
    host.querySelectorAll("[data-signal-idx]").forEach((node) => {
      const idx = Number(node.getAttribute("data-signal-idx"));
      const signal = rows[idx];
      if (!signal?.action) return;
      makeInteractiveCard(node, () => applySignalAction(signal.action));
    });
  };

  const renderFocusActions = (actions = []) => {
    const summaryHost = document.getElementById("focusActionsBand");
    if (summaryHost) {
      const lead = Array.isArray(actions) && actions[0] ? actions[0] : null;
      summaryHost.innerHTML = `
        <div class="products-brief-card">
          <div class="products-brief-label">Action focus</div>
          <div class="products-brief-value">${escapeHtml(lead?.title || "Keep monitoring execution queues")}</div>
          <div class="products-brief-note">${escapeHtml(lead?.detail || "The highest-priority action will surface here when the bundle loads.")}</div>
        </div>
      `;
    }

    const host = document.getElementById("focusActionList");
    if (!host) return;
    const rows = Array.isArray(actions) ? actions.slice(0, 4) : [];
    if (!rows.length) {
      host.innerHTML = '<span class="text-muted small">No prioritized actions for current filters.</span>';
      return;
    }
    host.innerHTML = `<div class="focus-action-grid">${rows
      .map(
        (action, idx) => `
          <div class="focus-action-card tone-${escapeHtml(action?.tone || "neutral")} ${action?.section ? "has-action" : ""}" data-focus-idx="${idx}">
            <div class="focus-action-owner">${escapeHtml(action?.owner || "Owner")}</div>
            <div class="focus-action-title">${escapeHtml(action?.title || "Review queue")}</div>
            <div class="focus-action-detail">${escapeHtml(action?.detail || "")}</div>
            ${(action?.confidence || action?.upside)
              ? `<div class="focus-action-meta">${escapeHtml(action?.confidence ? `Confidence ${action.confidence}` : "")}${action?.confidence && action?.upside ? ` ${MIDDLE_DOT} ` : ""}${action?.upside ? `Upside ${fmtMoney0.format(action.upside)}` : ""}</div>`
              : ""}
          </div>
        `
      )
      .join("")}</div>`;
    host.querySelectorAll("[data-focus-idx]").forEach((node) => {
      const idx = Number(node.getAttribute("data-focus-idx"));
      const action = rows[idx];
      if (!action?.section && !action?.quick_filters && !action?.quickFilters) return;
      makeInteractiveCard(node, () => applySignalAction(action));
    });
  };

  const renderStrategyBrief = (segments = {}, concentration = {}) => {
    const summaryRows = Array.isArray(segments?.summary) ? segments.summary.slice(0, 4) : [];
    const mixRows = Array.isArray(segments?.mix_shift) ? segments.mix_shift.slice(0, 4) : [];
    setHtml(
      "strategySegmentSummary",
      summaryRows.length
        ? summaryRows
            .map(
              (row) =>
                `<div class="d-flex justify-content-between py-1"><span>${escapeHtml(row?.segment || EM_DASH)}</span><strong>${fmtMoney0.format(row?.revenue || 0)}</strong></div>`
            )
            .join("")
        : '<span class="text-muted small">No segment contribution data.</span>'
    );
    setHtml(
      "strategyMixShift",
      mixRows.length
        ? mixRows
            .map((row) => {
              const delta = row?.share_delta_pp;
              const deltaLabel = delta == null || Number.isNaN(Number(delta))
                ? EM_DASH
                : `${delta > 0 ? "+" : ""}${fmtPct1.format(delta)} pp`;
              return `<div class="d-flex justify-content-between py-1"><span>${escapeHtml(row?.segment || EM_DASH)}</span><strong>${escapeHtml(deltaLabel)}</strong></div>`;
            })
            .join("")
        : '<span class="text-muted small">No mix shift data.</span>'
    );
    setText("strategyTop1Share", concentration?.top1_share != null ? `${fmtPct1.format(concentration.top1_share)}%` : EM_DASH);
    setText("strategyTop10Share", concentration?.top10_share != null ? `${fmtPct1.format(concentration.top10_share)}%` : EM_DASH);
    setText("strategyPareto80", concentration?.skus_to_80 != null ? fmtInt.format(concentration.skus_to_80) : EM_DASH);
  };

  const renderProteinIntelligence = (proteinInsights = {}) => {
    const summary = proteinInsights?.summary || {};
    setText("proteinLeadFamily", summary?.top_family || EM_DASH);
    setText("proteinLeadShare", summary?.top_family_share != null ? `${fmtPct1.format(summary.top_family_share)}%` : EM_DASH);
    setText("proteinFamilyCount", summary?.family_count != null ? fmtInt.format(summary.family_count) : EM_DASH);
    setText(
      "proteinNarrativeHeadline",
      proteinInsights?.narrative?.headline || "Protein family posture will populate when assortment detail loads."
    );
    setText(
      "proteinNarrativeDetail",
      proteinInsights?.narrative?.detail || "Family-level price ladders, margin watch, and execution queues stay aligned with the active filter scope."
    );

    const renderList = (id, rows, formatter, binder) => {
      const host = document.getElementById(id);
      if (!host) return;
      const data = Array.isArray(rows) ? rows.slice(0, 4) : [];
      if (!data.length) {
        host.innerHTML = '<span class="text-muted small">No protein family signal in the current scope.</span>';
        return;
      }
      host.innerHTML = data.map(formatter).join("");
      if (typeof binder === "function") binder(host, data);
    };

    renderList("proteinMixShiftList", proteinInsights?.mix_shift, (row, idx) => {
      const delta = row?.share_delta_pp;
      const deltaLabel = delta == null || Number.isNaN(Number(delta))
        ? EM_DASH
        : `${delta > 0 ? "+" : ""}${fmtPct1.format(delta)} pp`;
      const share = row?.share_current != null ? `${fmtPct1.format(row.share_current)}% share` : "share pending";
      return `<button type="button" class="btn btn-link p-0 text-start text-decoration-none w-100 d-flex justify-content-between gap-2 border-bottom py-1" data-protein-mix-shift="${idx}"><span>${escapeHtml(row?.family || row?.category || EM_DASH)}</span><span class="text-end">${escapeHtml(deltaLabel)} ${MIDDLE_DOT} ${escapeHtml(share)}</span></button>`;
    }, (host, rows) => {
      host.querySelectorAll("[data-protein-mix-shift]").forEach((node) => {
        const idx = Number(node.getAttribute("data-protein-mix-shift"));
        const row = rows[idx];
        if (!row?.family) return;
        node.addEventListener("click", () => applyDetailView({ search: row.family, section: "table", mode: "analyst" }));
      });
    });

    renderList("proteinMarginWatchList", proteinInsights?.margin_watch, (row, idx) => {
      const margin = row?.margin_pct != null ? `${fmtPct1.format(row.margin_pct)}%` : "No cost";
      const revenue = row?.revenue != null ? fmtMoney0.format(row.revenue) : EM_DASH;
      return `<button type="button" class="btn btn-link p-0 text-start text-decoration-none w-100 d-flex justify-content-between gap-2 border-bottom py-1" data-protein-margin-watch="${idx}"><span>${escapeHtml(row?.family || row?.category || EM_DASH)}</span><span class="text-end">${escapeHtml(margin)} ${MIDDLE_DOT} ${escapeHtml(revenue)}</span></button>`;
    }, (host, rows) => {
      host.querySelectorAll("[data-protein-margin-watch]").forEach((node) => {
        const idx = Number(node.getAttribute("data-protein-margin-watch"));
        const row = rows[idx];
        if (!row?.family) return;
        node.addEventListener("click", () => applyDetailView({ search: row.family, quickFilters: ["recover_margin"], section: "table", mode: "analyst" }));
      });
    });

    renderList("proteinPortfolioList", proteinInsights?.portfolio || proteinInsights?.leaders, (row, idx) => {
      const share = row?.share_current != null ? `${fmtPct1.format(row.share_current)}% share` : EM_DASH;
      const margin = row?.margin_pct != null ? `${fmtPct1.format(row.margin_pct)}% margin` : "No cost";
      const signal = row?.signal || row?.tone || "Stable";
      return `<button type="button" class="btn btn-link protein-family-row text-start text-decoration-none w-100" data-protein-portfolio="${idx}">
        <span class="protein-family-main">
          <span class="protein-family-name">${escapeHtml(row?.family || row?.category || EM_DASH)}</span>
          <span class="protein-family-meta">${escapeHtml(share)} ${MIDDLE_DOT} ${escapeHtml(margin)}</span>
        </span>
        <span class="protein-family-signal">${escapeHtml(signal)}</span>
      </button>`;
    }, (host, rows) => {
      host.querySelectorAll("[data-protein-portfolio]").forEach((node) => {
        const idx = Number(node.getAttribute("data-protein-portfolio"));
        const row = rows[idx];
        if (!row?.family) return;
        node.addEventListener("click", () => applyDetailView({ search: row.family, section: "table", mode: "analyst" }));
      });
    });

    renderList("proteinPricingOpportunityList", proteinInsights?.pricing_opportunities, (row, idx) => {
      const skuCount = row?.sku_count != null ? `${fmtInt.format(row.sku_count)} SKU${row.sku_count === 1 ? "" : "s"}` : EM_DASH;
      const atRisk = row?.revenue_at_risk != null ? fmtMoney0.format(row.revenue_at_risk) : EM_DASH;
      const uplift = row?.avg_uplift_pct != null ? `${fmtPct1.format(row.avg_uplift_pct)}% avg uplift` : "Review ladder";
      return `<button type="button" class="btn btn-link protein-family-row text-start text-decoration-none w-100" data-protein-pricing="${idx}">
        <span class="protein-family-main">
          <span class="protein-family-name">${escapeHtml(row?.family || row?.category || EM_DASH)}</span>
          <span class="protein-family-meta">${escapeHtml(skuCount)} ${MIDDLE_DOT} ${escapeHtml(uplift)}</span>
        </span>
        <span class="protein-family-signal">${escapeHtml(atRisk)}</span>
      </button>`;
    }, (host, rows) => {
      host.querySelectorAll("[data-protein-pricing]").forEach((node) => {
        const idx = Number(node.getAttribute("data-protein-pricing"));
        const row = rows[idx];
        if (!row?.family) return;
        node.addEventListener("click", () => applyDetailView({ search: row.family, quickFilters: ["recover_margin"], section: "table", mode: "analyst" }));
      });
    });

    renderList("proteinExecutionWatchList", proteinInsights?.execution_watch, (row, idx) => {
      const counts = [
        row?.pricing_fixes ? `${fmtInt.format(row.pricing_fixes)} pricing` : null,
        row?.cost_gaps ? `${fmtInt.format(row.cost_gaps)} cost` : null,
        row?.promote_candidates ? `${fmtInt.format(row.promote_candidates)} promote` : null,
      ].filter(Boolean).join(` ${MIDDLE_DOT} `);
      const revenue = row?.revenue != null ? fmtMoney0.format(row.revenue) : EM_DASH;
      return `<button type="button" class="btn btn-link protein-family-row text-start text-decoration-none w-100" data-protein-execution="${idx}">
        <span class="protein-family-main">
          <span class="protein-family-name">${escapeHtml(row?.family || EM_DASH)}</span>
          <span class="protein-family-meta">${escapeHtml(counts || "Execution queue")}</span>
        </span>
        <span class="protein-family-signal">${escapeHtml(revenue)}</span>
      </button>`;
    }, (host, rows) => {
      host.querySelectorAll("[data-protein-execution]").forEach((node) => {
        const idx = Number(node.getAttribute("data-protein-execution"));
        const row = rows[idx];
        if (!row?.family) return;
        const quickFilters = row?.cost_gaps ? ["missing_cost"] : row?.pricing_fixes ? ["recover_margin"] : ["promote_candidate"];
        node.addEventListener("click", () => applyDetailView({ search: row.family, quickFilters, section: "table", mode: "analyst" }));
      });
    });
  };

  const renderKpis = (kpis = {}) => {
    setText("kpiRevenue", fmtMoney0.format(kpis.revenue ?? 0));
    setText("kpiQty", fmtInt.format(kpis.qty ?? 0));
    setText("kpiWeight", fmtInt.format(kpis.weight ?? 0));
    setText("kpiUnique", fmtInt.format(kpis.products ?? kpis.rows ?? 0));
    setText("kpiCustomers", fmtInt.format(kpis.customers ?? 0));
    setText("kpiMargin", kpis.margin_pct != null ? `${fmtPct1.format(kpis.margin_pct)}%` : EM_DASH);
    setText("kpiAvgPrice", kpis.avg_price != null ? fmtMoney2.format(kpis.avg_price) : EM_DASH);
    setText("kpiMedianPrice", kpis.median_price != null ? fmtMoney2.format(kpis.median_price) : EM_DASH);
    setText("kpiRevPerProduct", kpis.revenue_per_product != null ? fmtMoney0.format(kpis.revenue_per_product) : EM_DASH);
    setText("kpiRevPerCustomer", kpis.revenue_per_customer != null ? fmtMoney0.format(kpis.revenue_per_customer) : EM_DASH);
    setText("kpiCostCoverage", kpis.cost_coverage_pct != null ? `${fmtPct1.format(kpis.cost_coverage_pct)}%` : EM_DASH);
    setText("kpiMissingCostSkus", fmtInt.format(kpis.missing_cost_sku_count ?? 0));
    setText("kpiContributionP50", kpis.contribution_lb_p50 != null ? fmtMoney2.format(kpis.contribution_lb_p50) : EM_DASH);
    setText("kpiProfitAtRisk", fmtMoney0.format(kpis.profit_at_risk ?? 0));
    setText("kpiUpliftPotential", fmtMoney0.format(kpis.risk_profit_uplift_target ?? 0));
    setText("kpiProfit", fmtMoney0.format((kpis.profit ?? 0)));
    setText("kpiProfitNote", "");
    setText("upP10", kpis.unit_price_p10 != null ? fmtMoney2.format(kpis.unit_price_p10) : EM_DASH);
    setText("upP50", kpis.unit_price_p50 != null ? fmtMoney2.format(kpis.unit_price_p50) : EM_DASH);
    setText("upP90", kpis.unit_price_p90 != null ? fmtMoney2.format(kpis.unit_price_p90) : EM_DASH);
  };

  const bindKpiCards = () => {
    const cardMap = {
      kpiRevenue: { sortBy: "revenue", sortDir: "desc", section: "table", emphasis: "revenue" },
      kpiQty: { sortBy: "qty", sortDir: "desc", section: "table", emphasis: "weight" },
      kpiWeight: { sortBy: "weight", sortDir: "desc", section: "table", emphasis: "weight" },
      kpiUnique: { sortBy: "revenue", sortDir: "desc", section: "table" },
      kpiCustomers: { quickFilters: ["high_customer_dependency"], sortBy: "customer_count", sortDir: "desc", section: "table", mode: "analyst" },
      kpiMargin: { quickFilters: ["recover_margin"], section: "pricing", emphasis: "profit", mode: "analyst" },
      kpiAvgPrice: { sortBy: "current_unit_price", sortDir: "desc", section: "table" },
      kpiMedianPrice: { sortBy: "current_unit_price", sortDir: "desc", section: "table" },
      kpiRevPerProduct: { sortBy: "revenue", sortDir: "desc", section: "assortment" },
      kpiRevPerCustomer: { sortBy: "customer_count", sortDir: "desc", section: "table", mode: "analyst" },
      kpiCostCoverage: { quickFilters: ["missing_cost"], section: "execution", emphasis: "profit", mode: "analyst" },
      kpiMissingCostSkus: { quickFilters: ["missing_cost"], section: "execution", emphasis: "profit", mode: "analyst" },
      kpiContributionP50: { quickFilters: ["recover_margin"], section: "pricing", emphasis: "profit", mode: "analyst" },
      kpiProfitAtRisk: { quickFilters: ["recover_margin"], section: "pricing", emphasis: "profit", mode: "analyst" },
      kpiUpliftPotential: { quickFilters: ["recover_margin"], section: "pricing", emphasis: "profit", mode: "analyst" },
    };
    Object.entries(cardMap).forEach(([id, action]) => {
      const card = document.getElementById(id)?.closest(".metric-card");
      if (!card) return;
      makeInteractiveCard(card, () => applyDetailView(action));
    });
  };

  const bindInsightCards = () => {
    const cardActions = [
      ["insightMomentum", () => applyDetailView({ section: "demand", sortBy: "revenue_delta", sortDir: "desc", mode: "analyst" })],
      ["momDelta", () => {
        const comparisonMetric = ((lastPayload?.insights || []).find((row) => row?.metric === "comparison_delta")) || {};
        applyDetailView({ section: "demand", quickFilters: comparisonMetric?.delta_pct < 0 ? ["promote_candidate"] : ["protect_core"], mode: "analyst" });
      }],
      ["predictiveRev", () => applyDetailView({ section: "demand", sortBy: "revenue_delta", sortDir: "desc", mode: "analyst" })],
      ["insightTopProduct", () => {
        const topProduct = resolveTopProductInsight() || {};
        const row = resolveProductContextRow(topProduct);
        if (!getSku(row)) return;
        renderProductIntel(row, "Executive scorecard", "Top product", "Revenue", row.revenue ?? topProduct.revenue ?? null);
      }],
    ];
    cardActions.forEach(([id, handler]) => {
      const card = document.getElementById(id)?.closest(".insight-card");
      if (!card) return;
      makeInteractiveCard(card, handler);
    });
  };

  const renderVelocity = (velocity = {}) => {
    setText("velAvgWeekly", velocity.avg_weekly != null ? formatVelocity(velocity.avg_weekly) : EM_DASH);
    setText("velWeeklyRevenue", fmtMoney0.format(velocity.weekly_revenue ?? 0));
    setText("velRevPerProduct", fmtMoney0.format(velocity.rev_per_product ?? 0));
    setText("velActive", fmtInt.format(velocity.active_skus ?? 0));
    setText("velRoi", velocity.roi_pct != null ? `${fmtPct1.format(velocity.roi_pct)}%` : EM_DASH);
    setText("velRetail", EM_DASH);
    setText("velTopMover", EM_DASH);
    setText("velW13", EM_DASH);
    const metaHost = document.getElementById("velocityPulseMeta");
    if (metaHost) {
      const parts = [];
      if (velocity?.customers != null) parts.push(`${fmtInt.format(velocity.customers)} customers in scope`);
      if (lastPayload?.comparison?.comparison_label) parts.push(lastPayload.comparison.comparison_label);
      if ((lastPayload?.risk_opportunity || {}).below_target_count != null) {
        parts.push(`${fmtInt.format(lastPayload.risk_opportunity.below_target_count || 0)} SKUs below target`);
      }
      metaHost.textContent = parts.join(` ${MIDDLE_DOT} `) || "Weekly pulse follows the current filtered window and gross-margin coverage.";
    }
  };

  const renderAISignals = (signals = {}) => {
    setText("aiMarginRisk", signals.margin_risk || EM_DASH);
    setText("aiMarginRiskNote", signals.notes || "");
    setText("aiPricing", signals.pricing_action || EM_DASH);
    const confidence = signals.confidence ? `Confidence: ${signals.confidence}` : "";
    setText("aiPricingNote", confidence);
  };

  const renderInsights = (insights = [], projected = null, comparison = {}) => {
    const map = {};
    insights.forEach((i) => { if (i?.metric) map[i.metric] = i; });
    const momentum = map.revenue_momentum || map.comparison_delta || map.mom_delta;
    const compareLabel = momentum?.label || comparison?.comparison_label || "prior comparable window";
    if (momentum) {
      setText("insightMomentum", fmtMoney0.format(momentum.current ?? 0));
      const delta = momentum.delta_pct;
      setText("insightMomentumDelta", delta != null ? `${delta > 0 ? "+" : ""}${fmtPct1.format(delta)}%` : EM_DASH);
      setText(
        "insightMomentumNote",
        momentum.prev != null ? `${compareLabel} ${MIDDLE_DOT} ${fmtMoney0.format(momentum.prev)}` : compareLabel
      );
      setText("momDelta", delta != null ? `${delta > 0 ? "+" : ""}${fmtPct1.format(delta)}%` : EM_DASH);
      setText("momNote", momentum.prev != null ? `${compareLabel} ${MIDDLE_DOT} ${fmtMoney0.format(momentum.prev)}` : compareLabel);
    } else {
      setText("insightMomentum", EM_DASH);
      setText("insightMomentumDelta", EM_DASH);
      setText("insightMomentumNote", comparison?.note || "Not enough comparable data in the current filtered window.");
      setText("momDelta", EM_DASH);
      setText("momNote", comparison?.note || compareLabel);
    }
    const top = resolveTopProductInsight();
    if (top) {
      setText("insightTopProduct", displayName(top));
      const totalRevenue = numericOrNull(lastPayload?.comparison_summary?.revenue_current ?? lastPayload?.kpis?.revenue);
      const share = top.revenue != null && totalRevenue && totalRevenue > 0
        ? (Number(top.revenue) / totalRevenue) * 100
        : null;
      const shareText = share != null ? `${fmtPct1.format(share)}% of visible revenue` : null;
      setText(
        "insightTopProductShare",
        top.revenue != null
          ? `${fmtMoney0.format(top.revenue)}${shareText ? ` ${MIDDLE_DOT} ${shareText}` : ""}`
          : "No SKU revenue in the active scope."
      );
      const contextParts = [];
      const topStatus = visualStatusLabel(top);
      if (hasMeaningfulText(top?.segment)) contextParts.push(top.segment);
      if (hasMeaningfulText(topStatus)) contextParts.push(topStatus);
      if (top?.customer_count != null) contextParts.push(`${fmtInt.format(top.customer_count)} customers`);
      setText("insightTopProductContext", contextParts.join(` ${MIDDLE_DOT} `) || "Highest revenue SKU in the active filtered window.");
    } else {
      setText("insightTopProduct", "No leading SKU");
      setText("insightTopProductShare", "No product revenue is available in the active scope.");
      setText("insightTopProductContext", "Adjust the current filters or date window to surface a product leader.");
    }
    const proj = map.projected_next_month || projected;
    if (proj) {
      setText("predictiveRev", proj.value != null ? fmtMoney0.format(proj.value) : EM_DASH);
      setText("predictiveRevNote", proj.note || "");
    } else {
      setText("predictiveRev", EM_DASH);
      setText("predictiveRevNote", comparison?.projection_note || "Need at least two completed periods to project the next month.");
    }
  };

  const renderTrajectory = (trajectory = {}, forecast = [], comparison = {}) => {
    const labels = trajectory.labels || [];
    const rev = trajectory.revenue || [];
    const qty = trajectory.qty || [];
    const ctx = document.getElementById("trendChart");
    if (!ctx) return;
    destroyChart("trajectory");
    if (!labels.length) {
      ctx.replaceWith(ctx.cloneNode(true));
      return;
    }
    const grain = String(trajectory.grain || "monthly").toLowerCase();
    const subnote = document.getElementById("trajectorySubnote");
    if (subnote) {
      subnote.textContent = comparison?.trajectory_note || (grain === "weekly"
        ? "Revenue and demand trend (weekly bins selected automatically for short windows)."
        : "Revenue and demand trend (monthly bins).");
    }

    const seriesLabels = [...labels];
    const revSeries = [...rev];
    const qtySeries = [...qty];
    const datasets = [
      { label: "Revenue", data: revSeries, backgroundColor: "#7a413a" },
      { label: "Demand", data: qtySeries, type: "line", yAxisID: "y1", borderColor: ChartUtils.seriesColor(1), backgroundColor: ChartUtils.seriesFill(1, .3), fill: false, tension: 0.2 },
    ];
    if (!isV4) {
      const forecastMap = new Map((forecast || []).map((p) => [p.month || p.label, p.revenue]));
      const allLabels = [...seriesLabels];
      (forecast || []).forEach((p) => {
        const label = p.month || p.label;
        if (label && !allLabels.includes(label)) allLabels.push(label);
      });
      if (allLabels.length !== seriesLabels.length) {
        const revMap = new Map(seriesLabels.map((label, idx) => [label, revSeries[idx]]));
        const qtyMap = new Map(seriesLabels.map((label, idx) => [label, qtySeries[idx]]));
        seriesLabels.splice(0, seriesLabels.length, ...allLabels);
        revSeries.splice(0, revSeries.length, ...allLabels.map((label) => (revMap.has(label) ? revMap.get(label) : null)));
        qtySeries.splice(0, qtySeries.length, ...allLabels.map((label) => (qtyMap.has(label) ? qtyMap.get(label) : null)));
      }
      const forecastSeries = seriesLabels.map((l) => (forecastMap.has(l) ? forecastMap.get(l) : null));
      if (forecastSeries.some((v) => v != null)) {
        datasets.push({ label: "Forecast", data: forecastSeries, type: "line", borderColor: ChartUtils.seriesColor(0), borderDash: [4, 4], fill: false });
      }
    }

    charts.trajectory = new Chart(ctx, {
      type: "bar",
      data: { labels: seriesLabels, datasets },
      options: {
        responsive: true,
        interaction: { mode: "index", intersect: false },
        onClick: (_evt, activeEls) => {
          const idx = activeEls?.[0]?.index;
          if (idx == null) return;
          const label = seriesLabels[idx];
          applyDetailView({
            section: "table",
            sortBy: "revenue_delta",
            sortDir: "desc",
            mode: "analyst",
            search: "",
          });
          setText("tableLayerContext", `${label || "Selected period"} was clicked from trajectory. The table is now sorted to inspect the biggest SKU-level changes inside the same visible scope.`);
        },
        scales: {
          x: {
            ticks: {
              autoSkip: true,
              maxRotation: 0,
              minRotation: 0,
              maxTicksLimit: 12,
            },
          },
          y: { beginAtZero: true, ticks: { callback: (v) => fmtMoney0.format(v) } },
          y1: { beginAtZero: true, position: "right", grid: { drawOnChartArea: false }, ticks: { callback: (v) => fmtInt.format(v) } },
        },
      },
    });
    removeSkeleton("trendChart");
  };

  const renderPriceVelocity = (points = []) => {
    const el = document.getElementById("priceVelocityChart");
    if (!el) return;
    removeSkeleton("priceVelocityChart");
    const rows = Array.isArray(points) ? points : [];
    if (!rows.length) {
      el.innerHTML = '<p class="text-muted small">No data.</p>';
      return;
    }
    const filtered = rows
      .map((p) => {
        const pricing = comparablePriceContext(p);
        return {
          ...p,
          pricing,
          velocity: numericOrNull(p.velocity_per_month ?? p.orders_per_month),
          currentPrice: numericOrNull(pricing.current),
        };
      })
      .filter((p) => p.currentPrice != null && p.velocity != null);
    if (!filtered.length) {
      el.innerHTML = '<p class="text-muted small">No price/velocity data.</p>';
      return;
    }
    if (!window.Plotly) {
      el.innerHTML = '<p class="text-muted small">Plotly not loaded.</p>';
      return;
    }
    const sorted = [...filtered].sort((a, b) => (b.revenue || 0) - (a.revenue || 0));
    const topN = sorted.slice(0, 75).map((row, index) => ({ ...row, _rank: index }));
    const sizes = bubbleDiameters(topN, revenueExposureBasis, 15, 40);
    const plottedRows = topN.map((row, index) => ({ ...row, bubbleSize: sizes[index] }));
    const rowBySku = new Map(plottedRows.map((row) => [getSku(row), row]));
    const perLbShare = plottedRows.filter((row) => row?.pricing?.basisLabel === "per lb").length / Math.max(1, plottedRows.length);
    const xAxisTitle = perLbShare >= 0.6 ? "Current realized price on SKU pricing basis" : "Current realized price on SKU pricing basis";
    const xMedian = percentile(plottedRows.map((row) => row.currentPrice), 0.5);
    const yMedian = percentile(plottedRows.map((row) => row.velocity), 0.5);
    const statusOrder = ["red", "orange", "yellow", "light_green", "green", "needs_mapping", "no_cost"];
    const buildHover = (row) => {
      const pricing = row?.pricing || comparablePriceContext(row);
      const status = statusMeta(visualStatusKey(row));
      const topCustomer = row?.top_customer_name
        ? `${row.top_customer_name}${row?.top_customer_share != null ? ` (${fmtPct1.format(row.top_customer_share)}%)` : ""}`
        : EM_DASH;
      return [
        `<b>${displayName(row)}</b>`,
        status.label,
        `${pricing.currentLabel}: ${row?.currentPrice != null ? fmtMoney2.format(row.currentPrice) : EM_DASH}`,
        `${pricing.targetLabel}: ${pricing.target != null ? fmtMoney2.format(pricing.target) : EM_DASH}`,
        `Gap to target: ${pricing.gapToTarget != null ? formatSignedMoney2(pricing.gapToTarget) : EM_DASH}`,
        `Revenue: ${fmtMoney0.format(row?.revenue ?? 0)}`,
        `Velocity: ${formatVelocity(row?.velocity)} /mo`,
        `Gross margin: ${row?.margin_pct != null ? `${fmtPct1.format(row.margin_pct)}%` : EM_DASH}`,
        `Top customer: ${topCustomer}`,
      ].join("<br>");
    };
    const traces = [];
    statusOrder.forEach((statusKey) => {
      const statusRows = plottedRows.filter((row) => visualStatusKey(row) === statusKey);
      if (!statusRows.length) return;
      const meta = statusMeta(statusKey);
      traces.push({
        name: meta.label || meta.short_label || "Status",
        ids: statusRows.map((row) => getSku(row)),
        x: statusRows.map((row) => row.currentPrice),
        y: statusRows.map((row) => row.velocity),
        text: statusRows.map((row) => buildHover(row)),
        mode: "markers",
        type: "scatter",
        hoverinfo: "text",
        marker: {
          size: statusRows.map((row) => row.bubbleSize),
          sizemode: "diameter",
          color: meta.color || "#7a7f87",
          opacity: 0.86,
          line: { color: "rgba(255,255,255,0.9)", width: 1 },
        },
      });
    });
    if (!traces.length) {
      traces.push({
        name: "Visible SKUs",
        ids: plottedRows.map((row) => getSku(row)),
        x: plottedRows.map((row) => row.currentPrice),
        y: plottedRows.map((row) => row.velocity),
        text: plottedRows.map((row) => buildHover(row)),
        mode: "markers",
        type: "scatter",
        hoverinfo: "text",
        marker: {
          size: plottedRows.map((row) => row.bubbleSize),
          sizemode: "diameter",
          color: "#7a7f87",
          opacity: 0.82,
          line: { color: "rgba(255,255,255,0.9)", width: 1 },
        },
      });
    }
    Plotly.newPlot(
      el,
      traces,
      {
        margin: { t: 52, l: 58, r: 18, b: 58 },
        height: 340,
        showlegend: true,
        legend: { orientation: "h", x: 0, y: 1.2, bgcolor: "rgba(255,255,255,0.82)" },
        hoverlabel: { bgcolor: "#111827", bordercolor: "#111827", font: { color: "#fff" } },
        xaxis: { title: xAxisTitle, tickprefix: "$", tickformat: ",.2f", automargin: true, gridcolor: "rgba(148,163,184,0.16)" },
        yaxis: { title: "Velocity / month", automargin: true, gridcolor: "rgba(148,163,184,0.16)" },
        shapes: [
          ...(xMedian != null ? [{ type: "line", x0: xMedian, x1: xMedian, y0: 0, y1: 1, xref: "x", yref: "paper", line: { color: "rgba(100,116,139,0.55)", width: 1, dash: "dot" } }] : []),
          ...(yMedian != null ? [{ type: "line", x0: 0, x1: 1, y0: yMedian, y1: yMedian, xref: "paper", yref: "y", line: { color: "rgba(100,116,139,0.55)", width: 1, dash: "dot" } }] : []),
        ],
      },
      { displayModeBar: false, responsive: true }
    );
    setText(
      "priceVelocityMeta",
      `${xAxisTitle}. Showing ${fmtInt.format(plottedRows.length)} highest-revenue comparable SKUs. Bubble size reflects revenue exposure and color follows the pricing band derived from minimum and target gross-margin rules. Median guide lines are shown for readability. Click a SKU to open SKU intelligence, then continue to full drilldown if needed.`
    );

    if (typeof el.on === "function") {
      if (el.removeAllListeners) el.removeAllListeners("plotly_click");
      el.on("plotly_click", (ev) => {
        const sku = ev?.points?.[0]?.id;
        if (!sku) return;
        const row = rowBySku.get(sku);
        if (!row) return;
        openDecisionWorkbench(row, { sourceLabel: "Price vs velocity" });
        renderProductIntel(row, "Pricing & Velocity", "Price vs Velocity", xAxisTitle, row.currentPrice);
      });
    }
  };

  
  const renderPricingStatusSummary = (bubble = {}) => {
    const host = document.getElementById("priceBubbleSummary");
    if (!host) return;
    const cards = Array.isArray(bubble?.summary_cards) ? bubble.summary_cards : [];
    if (!cards.length) {
      host.innerHTML = '<div class="text-muted small">No pricing status summary for the current scope.</div>';
      return;
    }
    host.innerHTML = cards
      .map((card, idx) => {
        const meta = statusMeta(card?.status_key || "");
        return `
          <button type="button" class="pricing-status-summary-card btn btn-link text-decoration-none" style="--status-accent:${escapeHtml(meta.color || "#7a7f87")}" data-bubble-summary-card="${idx}">
            <div class="pricing-status-summary-head">
              <span class="d-inline-flex align-items-center gap-2">
                <span class="pricing-status-dot" style="background:${escapeHtml(meta.color || "#7a7f87")}"></span>
                <span class="pricing-status-summary-label">${escapeHtml(card?.label || "Status")}</span>
              </span>
              <strong>${fmtInt.format(card?.sku_count || 0)}</strong>
            </div>
            <div class="pricing-status-summary-value">${fmtMoney0.format(card?.revenue || 0)}</div>
            <div class="pricing-status-summary-note">${escapeHtml(`${card?.revenue_share != null ? `${fmtPct1.format(card.revenue_share)}%` : EM_DASH} of visible revenue`)}</div>
          </button>
        `;
      })
      .join("");
    host.querySelectorAll("[data-bubble-summary-card]").forEach((node) => {
      const idx = Number(node.getAttribute("data-bubble-summary-card"));
      const card = cards[idx];
      if (!card) return;
      node.addEventListener("click", () => {
        applySignalAction({
          quickFilters: [...(card.quick_filters || [])],
          section: card.section || "pricing",
          mode: card.mode || "analyst",
          emphasis: card.emphasis || "profit",
        });
      });
    });
  };

  const renderPricingStatusLegend = (bubble = {}) => {
    const host = document.getElementById("priceBubbleLegend");
    if (!host) return;
    const rows = Array.isArray(bubble?.legend) ? bubble.legend : [];
    if (!rows.length) {
      host.innerHTML = "";
      return;
    }
    host.innerHTML = rows
      .map((row) => `
        <span class="pricing-status-legend-item">
          <span class="pricing-status-dot" style="background:${escapeHtml(row?.color || "#7a7f87")}"></span>
          <span>${escapeHtml(row?.label || row?.short_label || "Status")}</span>
          <strong>${fmtInt.format(row?.sku_count || 0)}</strong>
        </span>
      `)
      .join("");
  };

  const renderPerformanceBubble = (bubble = {}) => {
    const el = document.getElementById("priceBubbleChart");
    if (!el) return;
    removeSkeleton("priceBubbleChart");
    const target = bubble.target_margin_label || (bubble.target_margin != null ? `${Math.round(bubble.target_margin * 100)}%` : EM_DASH);
    const floor = bubble.floor_margin_label || (bubble.floor_margin != null ? `${Math.round(bubble.floor_margin * 100)}%` : EM_DASH);
    setText("priceTargetMargin", target);
    setText("priceBaseMargin", floor);
    renderPricingStatusSummary(bubble);
    renderPricingStatusLegend(bubble);

    const rows = Array.isArray(bubble.points) ? bubble.points : [];
    if (!rows.length) {
      el.innerHTML = '<p class="text-muted small">No data.</p>';
      return;
    }
    const includeMissing = document.getElementById("bubbleIncludeMissing")?.checked;
    const colorKey = state.bubbleColorBy || "status_key";
    const xMetric = state.bubbleXMetric || "gap_to_target";
    const yMetric = state.bubbleYMetric || "velocity";

    const xValueForRow = (row) => {
      if (xMetric === "margin_pct") return numericOrNull(row?.margin_pct);
      if (xMetric === "gap_to_min") {
        const pricing = comparablePriceContext(row);
        return pricing.gapToMin;
      }
      const pricing = comparablePriceContext(row);
      return pricing.gapToTarget;
    };
    const yValueForRow = (row) => {
      if (yMetric === "revenue") return numericOrNull(row?.revenue);
      if (yMetric === "profit") return numericOrNull(row?.profit);
      return numericOrNull(row?.velocity_per_month ?? row?.orders_per_month);
    };
    const xAxisTitle = xMetric === "margin_pct"
      ? "Current Gross Margin %"
      : (xMetric === "gap_to_min" ? "Current vs Minimum Price Gap" : "Current vs Target Price Gap");
    const yAxisTitle = yMetric === "revenue" ? "Revenue" : (yMetric === "profit" ? "Profit" : "Velocity / month");

    const filtered = rows
      .filter((row) => includeMissing || rowHasPricingVisibility(row))
      .map((row) => ({ ...row, xValue: xValueForRow(row), yValue: yValueForRow(row) }))
      .filter((row) => row.xValue != null && row.yValue != null);
    if (!filtered.length) {
      el.innerHTML = '<p class="text-muted small">No comparable pricing points for the current view.</p>';
      return;
    }
    if (!window.Plotly) {
      el.innerHTML = '<p class="text-muted small">Plotly not loaded.</p>';
      return;
    }

    const sorted = [...filtered].sort((a, b) => (b.revenue || 0) - (a.revenue || 0));
    const topN = (state.bubbleTopN === "all" ? sorted : sorted.slice(0, Number(state.bubbleTopN) || 250))
      .map((row, index) => ({ ...row, _rank: index }));
    const bubbleMaxSize = topN.length > 300 ? 30 : (topN.length > 150 ? 34 : (topN.length > 75 ? 38 : 42));
    const bubbleOpacity = topN.length > 150 ? 0.74 : 0.82;
    const bubbleSizes = bubbleDiameters(topN, revenueExposureBasis, 12, bubbleMaxSize);
    const plottedRows = topN.map((row, index) => ({ ...row, bubbleSize: bubbleSizes[index] }));
    const rowBySku = new Map(plottedRows.map((row) => [getSku(row), row]));

    const buildHoverText = (row) => {
      const pricing = comparablePriceContext(row);
      const costs = costContext(row, pricing);
      const status = statusMeta(visualStatusKey(row));
      const revenue = row?.revenue != null ? fmtMoney0.format(row.revenue) : EM_DASH;
      const profit = row?.profit != null ? fmtMoney0.format(row.profit) : EM_DASH;
      const margin = row?.margin_pct != null ? `${fmtPct1.format(row.margin_pct)}%` : EM_DASH;
      const minimumMargin = row?.minimum_margin_pct != null ? `${fmtPct1.format(row.minimum_margin_pct)}%` : EM_DASH;
      const targetMargin = row?.target_margin_pct != null ? `${fmtPct1.format(row.target_margin_pct)}%` : EM_DASH;
      const upliftToTarget = row?.profit_uplift_target != null ? fmtMoney0.format(row.profit_uplift_target) : EM_DASH;
      const topCustomer = row?.top_customer_name
        ? `${row.top_customer_name}${row?.top_customer_share != null ? ` (${fmtPct1.format(row.top_customer_share)}%)` : ""}`
        : EM_DASH;
      const topRegion = row?.top_region_name
        ? `${row.top_region_name}${row?.top_region_share != null ? ` (${fmtPct1.format(row.top_region_share)}%)` : ""}`
        : EM_DASH;
      return [
        `<b>${displayName(row)}</b>`,
        `${status.label}`,
        `${pricing.currentLabel}: ${pricing.current != null ? fmtMoney2.format(pricing.current) : EM_DASH}`,
        `${costs.baseLabel}: ${costs.base != null ? fmtMoney2.format(costs.base) : EM_DASH}`,
        `${costs.effectiveLabel}: ${costs.effective != null ? fmtMoney2.format(costs.effective) : EM_DASH}`,
        `${pricing.minLabel}: ${pricing.minimum != null ? fmtMoney2.format(pricing.minimum) : EM_DASH}`,
        `${pricing.targetLabel}: ${pricing.target != null ? fmtMoney2.format(pricing.target) : EM_DASH}`,
        `Gap to target: ${pricing.gapToTarget != null ? formatSignedMoney2(pricing.gapToTarget) : EM_DASH}`,
        `Revenue: ${revenue}`,
        `Profit: ${profit}`,
        `Gross margin: ${margin} ${MIDDLE_DOT} Min: ${minimumMargin} ${MIDDLE_DOT} Target: ${targetMargin}`,
        `Profit uplift to target: ${upliftToTarget}`,
        `Top customer: ${topCustomer}`,
        `Top region: ${topRegion}`,
      ].join("<br>");
    };

    const statusOrder = ["red", "orange", "yellow", "light_green", "green", "needs_mapping", "no_cost"];
    const traces = [];
    if (colorKey === "segment") {
      const segments = Array.from(new Set(plottedRows.map((row) => row.segment || "Other")));
      const palette = ["#7a413a", ChartUtils.seriesColor(1), ChartUtils.seriesColor(0), ChartUtils.seriesColor(5), ChartUtils.seriesColor(4), ChartUtils.seriesColor(0), ChartUtils.seriesColor(3), "#6c757d"];
      segments.forEach((segment, index) => {
        const segmentRows = plottedRows.filter((row) => (row.segment || "Other") === segment);
        if (!segmentRows.length) return;
        traces.push({
          name: segment,
          ids: segmentRows.map((row) => getSku(row)),
          x: segmentRows.map((row) => row.xValue),
          y: segmentRows.map((row) => row.yValue),
          text: segmentRows.map((row) => buildHoverText(row)),
          mode: "markers",
          type: "scatter",
          hoverinfo: "text",
          marker: {
            size: segmentRows.map((row) => row.bubbleSize),
            sizemode: "diameter",
            sizemin: 9,
            color: palette[index % palette.length],
            opacity: bubbleOpacity,
            line: { color: "rgba(255,255,255,0.85)", width: 1 },
          },
        });
      });
    } else {
      statusOrder.forEach((statusKey) => {
        const statusRows = plottedRows.filter((row) => visualStatusKey(row) === statusKey);
        if (!statusRows.length) return;
        const meta = statusMeta(statusKey);
        traces.push({
          name: meta.label || meta.short_label || "Status",
          ids: statusRows.map((row) => getSku(row)),
          x: statusRows.map((row) => row.xValue),
          y: statusRows.map((row) => row.yValue),
          text: statusRows.map((row) => buildHoverText(row)),
          mode: "markers",
          type: "scatter",
          hoverinfo: "text",
          marker: {
            size: statusRows.map((row) => row.bubbleSize),
            sizemode: "diameter",
            sizemin: 9,
            color: meta.color || "#7a7f87",
            opacity: bubbleOpacity,
            line: { color: "rgba(255,255,255,0.85)", width: 1 },
          },
        });
      });
    }
    if (!traces.length) {
      traces.push({
        name: "Visible SKUs",
        ids: plottedRows.map((row) => getSku(row)),
        x: plottedRows.map((row) => row.xValue),
        y: plottedRows.map((row) => row.yValue),
        text: plottedRows.map((row) => buildHoverText(row)),
        mode: "markers",
        type: "scatter",
        hoverinfo: "text",
        marker: {
          size: plottedRows.map((row) => row.bubbleSize),
          sizemode: "diameter",
          sizemin: 9,
          color: "#7a7f87",
          opacity: bubbleOpacity,
          line: { color: "rgba(255,255,255,0.85)", width: 1 },
        },
      });
    }

    const zeroLine = xMetric === "margin_pct"
      ? []
      : [{ type: "line", x0: 0, x1: 0, y0: 0, y1: 1, xref: "x", yref: "paper", line: { color: "#94a3b8", width: 1.2, dash: "dot" } }];

    Plotly.newPlot(
      el,
      traces,
      {
        margin: { t: 26, l: 60, r: 24, b: 60 },
        height: 460,
        showlegend: true,
        legend: { orientation: "h", x: 0, y: 1.16, bgcolor: "rgba(255,255,255,0.82)" },
        hoverlabel: { bgcolor: "#111827", bordercolor: "#111827", font: { color: "#fff" } },
        xaxis: {
          title: xAxisTitle,
          zeroline: xMetric !== "margin_pct",
          zerolinecolor: "#cbd5e1",
          tickformat: xMetric === "margin_pct" ? ".1f" : ",.2f",
          tickprefix: xMetric === "margin_pct" ? "" : "$",
          ticksuffix: xMetric === "margin_pct" ? "%" : "",
          automargin: true,
          gridcolor: "rgba(148,163,184,0.16)",
        },
        yaxis: {
          title: yAxisTitle,
          tickprefix: yMetric === "revenue" || yMetric === "profit" ? "$" : "",
          tickformat: yMetric === "velocity" ? ",.0f" : ",.0f",
          automargin: true,
          gridcolor: "rgba(148,163,184,0.16)",
        },
        shapes: zeroLine,
      },
      { displayModeBar: false, responsive: true }
    );
    const shownCount = plottedRows.length;
    const totalCount = filtered.length;
    setText(
      "priceBubbleMeta",
      `X-axis: ${xAxisTitle}. Y-axis: ${yAxisTitle}. Showing ${fmtInt.format(shownCount)} of ${fmtInt.format(totalCount)} comparable SKUs after the current controls. Bubble size reflects revenue exposure and color follows the pricing band built from minimum and target gross-margin rules. Click a bubble to open SKU intelligence, then continue to full drilldown from the side panel if needed.`
    );

    if (typeof el.on === "function") {
      if (el.removeAllListeners) el.removeAllListeners("plotly_click");
      el.on("plotly_click", (ev) => {
        const sku = ev?.points?.[0]?.id;
        if (!sku) return;
        const row = rowBySku.get(sku);
        if (!row) return;
        openDecisionWorkbench(row, { sourceLabel: "Performance bubble", useRecommended: true });
        renderProductIntel(row, "Pricing & Margin Control", "Performance Bubble", xAxisTitle, row.xValue);
      });
    }
  };

  const renderPriceDist = (dist = []) => {
    const ctx = document.getElementById("priceDistChart");
    if (!ctx) return;
    destroyChart("priceDist");
    if (!dist.length) return;
    const labels = dist.map((b) => `B${b.bucket ?? ""}`);
    const values = dist.map((b) => b.count ?? 0);
    charts.priceDist = new Chart(ctx, {
      type: "bar",
      data: { labels, datasets: [{ label: "Unit price", data: values, backgroundColor: "#9b5f3d" }] },
      options: {
        responsive: true,
        plugins: { legend: { display: false } },
        onClick: (_evt, activeEls) => {
          const idx = activeEls?.[0]?.index;
          if (idx == null) return;
          const mid = Math.floor(labels.length / 2);
          applyDetailView({
            sortBy: "current_unit_price",
            sortDir: idx >= mid ? "desc" : "asc",
            section: "table",
          });
        },
      },
    });
    removeSkeleton("priceDistChart");
  };

  const renderMovers = (movers = []) => {
    const ctx = document.getElementById("moversChart");
    if (!ctx) return;
    destroyChart("movers");
    if (!movers.length) return;
    const metricSel = document.getElementById("moversMetric");
    const metric = (metricSel?.value || "revenue").toLowerCase();
    const deltaField = metric === "profit" ? "delta_profit" : (metric === "qty" ? "delta_qty" : "delta_revenue");
    const labelTitle = metric === "profit" ? `${DELTA} profit` : (metric === "qty" ? `${DELTA} units` : `${DELTA} revenue`);
    const labels = movers.map((m) => displayName(m));
    const delta = movers.map((m) => m[deltaField] ?? m.delta ?? 0);
    const colors = movers.map((m) => (((m[deltaField] ?? m.delta ?? 0) >= 0) ? ChartUtils.seriesColor(1) : ChartUtils.seriesColor(3)));
    charts.movers = new Chart(ctx, {
      type: "bar",
      data: { labels, datasets: [{ label: labelTitle, data: delta, backgroundColor: colors }] },
      options: {
        indexAxis: "y",
        responsive: true,
        plugins: { legend: { display: false } },
        onClick: (_evt, activeEls) => {
          const idx = activeEls?.[0]?.index;
          if (idx == null) return;
          const row = movers[idx];
          if (!row) return;
          renderProductIntel(row, "Trajectory & Movers", "Top Movers", labelTitle, row[deltaField] ?? row.delta);
        },
      },
    });
    removeSkeleton("moversChart");
  };

  const renderTopProducts = (products = []) => {
    const rawMetric = (document.getElementById("topMetric")?.value || "revenue").toLowerCase();
    const metric = rawMetric === "margin" ? "margin_pct" : rawMetric === "avg_price" ? "unit_price" : rawMetric;
    const topN = Number(document.getElementById("topN")?.value || 15);
    const sorted = [...products].sort((a, b) => (b[metric] || 0) - (a[metric] || 0)).slice(0, topN);
    const labels = sorted.map((p) => displayName(p));
    const shortLabels = labels.map((name) => (String(name).length > 34 ? `${String(name).slice(0, 34)}${ELLIPSIS}` : name));
    const values = sorted.map((p) => p[metric] || 0);
    const ctx = document.getElementById("topChart");
    if (!ctx) return;
    destroyChart("top");
    const label = rawMetric === "margin" ? "Margin %" : rawMetric === "avg_price" ? "Avg price" : rawMetric;
    charts.top = new Chart(ctx, {
      type: "bar",
      data: { labels: shortLabels, datasets: [{ label, data: values, backgroundColor: "#7a413a" }] },
      options: {
        indexAxis: "y",
        responsive: true,
        onClick: (_evt, activeEls) => {
          const idx = activeEls?.[0]?.index;
          if (idx == null) return;
          const row = sorted[idx];
          if (!row) return;
          renderProductIntel(row, "Portfolio Ranking", "Top Products", label, row[metric] || 0);
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: (items) => {
                const idx = items?.[0]?.dataIndex ?? 0;
                return labels[idx] || "";
              },
            },
          },
        },
      },
    });
    removeSkeleton("topChart");
  };

  const renderPareto = (pareto = []) => {
    const ctx = document.getElementById("paretoChart");
    if (!ctx) return;
    destroyChart("pareto");
    if (!pareto.length) return;
    const names = pareto.map((p) => p.label || p.display_name || p.product_name || EM_DASH);
    const labels = pareto.map((_, idx) => String(idx + 1));
    const values = pareto.map((p) => p.revenue);
    const cum = pareto.map((p) => p.cumulative);
    let pareto80Rank = null;
    for (let i = 0; i < cum.length; i += 1) {
      if ((cum[i] ?? 0) >= 80) {
        pareto80Rank = i + 1;
        break;
      }
    }
    charts.pareto = new Chart(ctx, {
      data: {
        labels,
        datasets: [
          { type: "bar", label: "Revenue", data: values, backgroundColor: "#9b5f3d" },
          { type: "line", label: "Cumulative %", data: cum, borderColor: ChartUtils.seriesColor(1), yAxisID: "y1", tension: 0.2 },
          { type: "line", label: "80% threshold", data: labels.map(() => 80), borderColor: "#6c757d", borderDash: [4, 4], yAxisID: "y1", pointRadius: 0 },
        ],
      },
      options: {
        responsive: true,
        onClick: (_evt, activeEls) => {
          const idx = activeEls?.[0]?.index;
          if (idx == null) return;
          const row = pareto[idx];
          if (!row) return;
          renderProductIntel(row, "Portfolio Ranking", "Pareto", "Revenue", row.revenue);
        },
        plugins: {
          tooltip: {
            callbacks: {
              title: (items) => {
                const idx = items?.[0]?.dataIndex ?? 0;
                return `Rank ${idx + 1}: ${names[idx] || EM_DASH}`;
              },
            },
          },
        },
        scales: {
          y: { beginAtZero: true, ticks: { callback: (v) => fmtMoney0.format(v) } },
          y1: { beginAtZero: true, position: "right", grid: { drawOnChartArea: false }, ticks: { callback: (v) => `${fmtPct1.format(v)}%` } },
        },
      },
    });
    setText("concPareto80", pareto80Rank != null ? fmtInt.format(pareto80Rank) : EM_DASH);
    removeSkeleton("paretoChart");
  };

  const renderSegmentSummary = (summary = []) => {
    const list = document.getElementById("segmentSummaryList");
    if (list) {
      if (!summary.length) {
        list.innerHTML = '<span class="text-muted small">No segment data.</span>';
      } else {
        list.innerHTML = summary
          .map(
            (s, idx) =>
              `<button type="button" class="btn btn-link p-0 text-start text-decoration-none w-100 d-flex justify-content-between segment-summary-link" data-segment-summary="${idx}"><span>${s.segment || EM_DASH}</span><span>${fmtInt.format(s.sku_count || 0)} ${MIDDLE_DOT} ${fmtMoney0.format(
                s.revenue || 0
              )}</span></button>`
          )
          .join("");
        list.querySelectorAll("[data-segment-summary]").forEach((node) => {
          const idx = Number(node.getAttribute("data-segment-summary"));
          const row = summary[idx];
          if (!row?.segment) return;
          node.addEventListener("click", () => applyDetailView({ segments: [row.segment], section: "table", mode: "analyst" }));
        });
      }
    }
    const select = document.getElementById("segmentFilter");
    if (select) {
      select.innerHTML = "";
      summary.forEach((s) => {
        const opt = document.createElement("option");
        opt.value = s.segment;
        opt.textContent = `${s.segment || EM_DASH} (${fmtInt.format(s.sku_count || 0)})`;
        opt.selected = state.segments.includes(s.segment);
        select.appendChild(opt);
      });
    }
  };

  const renderSegmentMovers = (movers = []) => {
    const list = document.getElementById("segmentMoversList");
    if (!list) return;
    if (!movers.length) {
      list.innerHTML = '<span class="text-muted small">No movers.</span>';
      return;
    }
    list.innerHTML = movers
      .map(
        (m, idx) =>
          `<button type="button" class="btn btn-link p-0 text-start text-decoration-none w-100 d-flex justify-content-between border-bottom py-1" data-segment-mover="${idx}"><span>${displayName(m)} (${m.segment || EM_DASH})</span><span>${formatSignedMoney(
            m.delta || 0
          )} ${MIDDLE_DOT} ${escapeHtml(m.status || "")}</span></button>`
      )
      .join("");
    list.querySelectorAll("[data-segment-mover]").forEach((node) => {
      const idx = Number(node.getAttribute("data-segment-mover"));
      const row = movers[idx];
      if (!row) return;
      node.addEventListener("click", () => renderProductIntel(row, "Segments", "Segment Movers", "Revenue delta", row.delta || 0));
    });
  };

  const renderSegmentMixShift = (rows = []) => {
    const host = document.getElementById("segmentMixShiftList");
    if (!host) return;
    const data = Array.isArray(rows) ? rows : [];
    if (!data.length) {
      host.innerHTML = '<span class="text-muted small">No mix shift data for current filters.</span>';
      return;
    }
    host.innerHTML = data
      .slice(0, 8)
      .map((row, idx) => {
        const delta = row?.share_delta_pp;
        const deltaLabel = delta == null || Number.isNaN(Number(delta))
          ? EM_DASH
          : `${delta > 0 ? "+" : ""}${fmtPct1.format(delta)} pp`;
        const cur = row?.share_current != null ? `${fmtPct1.format(row.share_current)}%` : EM_DASH;
        const prev = row?.share_prior != null ? `${fmtPct1.format(row.share_prior)}%` : EM_DASH;
        return `<button type="button" class="btn btn-link p-0 text-start text-decoration-none w-100 d-flex justify-content-between border-bottom py-1" data-segment-mix="${idx}"><span>${escapeHtml(row?.segment || EM_DASH)}</span><span>${escapeHtml(deltaLabel)} ${MIDDLE_DOT} ${escapeHtml(prev)} ${ARROW} ${escapeHtml(cur)}</span></button>`;
      })
      .join("");
    host.querySelectorAll("[data-segment-mix]").forEach((node) => {
      const idx = Number(node.getAttribute("data-segment-mix"));
      const row = data[idx];
      if (!row?.segment) return;
      node.addEventListener("click", () => applyDetailView({ segments: [row.segment], section: "table", mode: "analyst" }));
    });
  };

  const renderRecommendations = (recs = []) => {
    const el = document.getElementById("recPanel");
    if (!el) return;
    const rows = Array.isArray(recs) ? recs : [];
    if (!rows.length) {
      el.textContent = "No recommendations for current filters.";
      return;
    }
    el.innerHTML = rows
      .slice(0, 5)
      .map((r, idx) => {
        const name = displayName(r);
        const action = r.action || r.recommendation || "Review";
        const uplift = r.uplift_pct_est != null ? `${fmtPct1.format(r.uplift_pct_est)}%` : "";
        const note = r.rationale ? `<div class="text-muted small">${r.rationale}</div>` : "";
        const actionHtml = isV2
          ? `<span class="recommendation-badge">${escapeHtml(action)}${uplift ? ` ${MIDDLE_DOT} ${escapeHtml(uplift)}` : ""}</span>`
          : `<span>${escapeHtml(action)}${uplift ? ` ${MIDDLE_DOT} ${escapeHtml(uplift)}` : ""}</span>`;
        return `<button type="button" class="btn btn-link p-0 text-start text-decoration-none w-100 mb-2" data-recommendation-idx="${idx}"><div class="d-flex justify-content-between gap-2"><span>${escapeHtml(name)}</span>${actionHtml}</div>${note}</button>`;
      })
      .join("");
    el.querySelectorAll("[data-recommendation-idx]").forEach((node) => {
      const idx = Number(node.getAttribute("data-recommendation-idx"));
      const row = rows[idx];
      if (!row) return;
      node.addEventListener("click", () => {
        openDecisionWorkbench(row, { scroll: true, useRecommended: true, sourceLabel: row.action || "Recommendation" });
        renderProductIntel(row, "Execution", "Recommendations", row.action || "Recommendation", row.uplift_pct_est || 0);
      });
    });
  };

  const riskTone = (value, statusKey = "") => {
    const key = String(statusKey || "").toLowerCase();
    if (key === "red") return "danger";
    if (key === "orange") return "warning";
    if (key === "yellow") return "caution";
    if (key === "light_green") return "positive";
    if (key === "green") return "excellent";
    if (key === "needs_mapping" || key === "no_cost") return "neutral";
    const raw = String(value || "").toLowerCase();
    if (raw.includes("minimum") || raw.includes("negative")) return "danger";
    if (raw.includes("target")) return "warning";
    if (raw.includes("above target")) return "excellent";
    return "neutral";
  };

  const formatSignedMoney = (value) => {
    if (value == null || Number.isNaN(Number(value))) return EM_DASH;
    const amount = Number(value);
    const sign = amount > 0 ? "+" : "";
    return `${sign}${fmtMoney0.format(amount)}`;
  };

  const formatRevenueDeltaPct = (row) => {
    const current = Number(row?.revenue_current ?? 0);
    const prior = Number(row?.revenue_prior ?? 0);
    if (prior <= 0 && current > 0) return "New";
    if (current <= 0 && prior > 0) return "Lost";
    if (row?.revenue_low_base) return "Low base";
    if (row?.revenue_delta_pct == null || Number.isNaN(Number(row.revenue_delta_pct))) return EM_DASH;
    const val = Number(row.revenue_delta_pct);
    return `${val > 0 ? "+" : ""}${fmtPct1.format(val)}%`;
  };

  const renderSegmentBadge = (value) => `<span class="tag-badge">${escapeHtml(value || EM_DASH)}</span>`;
  const renderProteinCategoryCell = (row = {}) => {
    const family = row?.protein_family || row?.rule_family || EM_DASH;
    const category = row?.product_category && row.product_category !== family ? row.product_category : "";
    return `
      <div class="product-table-subcell">
        <div class="product-table-subcell-main">${escapeHtml(family)}</div>
        <div class="product-table-subcell-meta">${escapeHtml(category || "Category not mapped")}</div>
      </div>
    `;
  };
  const renderRiskBadge = (value, statusKey = "") =>
    `<span class="risk-badge margin-status-badge tone-${riskTone(value, statusKey)} is-${escapeHtml(String(statusKey || "").toLowerCase())}">${escapeHtml(value || EM_DASH)}</span>`;
  const renderRecommendationBadge = (value) =>
    `<span class="recommendation-badge">${escapeHtml(value || EM_DASH)}</span>`;
  const renderBandValue = (value, statusKey = "", formatter = (v) => v) => {
    if (value == null || Number.isNaN(Number(value))) return EM_DASH;
    return `<span class="risk-badge margin-status-badge tone-${riskTone("", statusKey)} is-${escapeHtml(String(statusKey || "").toLowerCase())}">${escapeHtml(formatter(value))}</span>`;
  };
  const formatSignedMoney2 = (value) => {
    if (value == null || Number.isNaN(Number(value))) return EM_DASH;
    const amount = Number(value);
    const sign = amount > 0 ? "+" : "";
    return `${sign}${fmtMoney2.format(amount)}`;
  };
  const formatVelocity = (value) => {
    if (value == null || Number.isNaN(Number(value))) return EM_DASH;
    return fmtNum1.format(Number(value));
  };

  const applyColumnVisibility = () => {
    if (!isV2) return;
    const visible = new Set((state.visibleColumns || []).concat(["sku", "product"]));
    document.querySelectorAll("#productTable [data-column]").forEach((cell) => {
      const key = cell.getAttribute("data-column");
      if (!key) return;
      cell.classList.toggle("d-none", !visible.has(key));
    });
  };

  const renderColumnChooser = () => {
    if (!isV2) return;
    const host = document.getElementById("productsColumnChooser");
    if (!host) return;
    const visible = new Set((state.visibleColumns || []).concat(["sku", "product"]));
    host.innerHTML = `
      <div class="products-column-chooser-grid">
        ${ACTIVE_COLUMN_DEFS.map((col) => {
          const checked = visible.has(col.key) ? "checked" : "";
          const disabled = col.locked ? "disabled" : "";
          return `
            <label>
              <input type="checkbox" data-column-toggle="${col.key}" ${checked} ${disabled}>
              <span>${escapeHtml(col.label)}</span>
            </label>
          `;
        }).join("")}
      </div>
    `;
    host.querySelectorAll("[data-column-toggle]").forEach((input) => {
      input.addEventListener("change", (evt) => {
        const key = evt.target.getAttribute("data-column-toggle");
        if (!key) return;
        const next = new Set((state.visibleColumns || []).concat(["sku", "product"]));
        if (evt.target.checked) next.add(key);
        else next.delete(key);
        state.visibleColumns = ACTIVE_COLUMN_DEFS.map((col) => col.key).filter((col) => next.has(col));
        state.activeTablePreset = "";
        writeStoredTablePreset("");
        writeStoredColumns(state.visibleColumns);
        applyColumnVisibility();
        syncTablePresetButtons();
        syncExportLinks();
      });
    });
  };

  const syncTablePresetButtons = () => {
    if (!isV4) return;
    document.querySelectorAll("[data-table-preset]").forEach((btn) => {
      btn.classList.toggle("active", btn.getAttribute("data-table-preset") === state.activeTablePreset);
    });
  };

  const applyTablePreset = (presetKey, { syncUi = true } = {}) => {
    if (!isV2) return;
    const preset = TABLE_PRESETS[presetKey];
    if (!preset || !Array.isArray(preset.columns) || !preset.columns.length) return;
    const keys = new Set(["sku", "product", ...preset.columns]);
    state.visibleColumns = ACTIVE_COLUMN_DEFS.map((col) => col.key).filter((key) => keys.has(key));
    state.activeTablePreset = presetKey;
    writeStoredColumns(state.visibleColumns);
    writeStoredTablePreset(presetKey);
    renderColumnChooser();
    applyColumnVisibility();
    syncExportLinks();
    if (syncUi) syncTablePresetButtons();
  };

  const applyColumnGroup = (groupKey) => {
    if (!isV2) return;
    const nextGroup = COLUMN_GROUPS[groupKey];
    if (!Array.isArray(nextGroup) || !nextGroup.length) return;
    const keys = new Set(["sku", "product", ...nextGroup]);
    state.visibleColumns = ACTIVE_COLUMN_DEFS.map((col) => col.key).filter((key) => keys.has(key));
    state.activeTablePreset = "";
    writeStoredTablePreset("");
    writeStoredColumns(state.visibleColumns);
    renderColumnChooser();
    applyColumnVisibility();
    syncTablePresetButtons();
    syncExportLinks();
  };

  const syncWatchlistButtons = () => {
    if (!isV4) return;
    document.querySelectorAll("[data-watchlist-preset]").forEach((btn) => {
      const key = btn.getAttribute("data-watchlist-preset") || "";
      const preset = WATCHLIST_PRESETS[key];
      const current = JSON.stringify((state.quickFilters || []).slice().sort());
      const target = JSON.stringify(((preset?.quickFilters || [])).slice().sort());
      btn.classList.toggle("is-active", current === target);
    });
  };

  const syncQuickFilterButtons = () => {
    document.querySelectorAll("#tableQuickFilters [data-quick-filter]").forEach((chip) => {
      const active = state.quickFilters.includes(chip.getAttribute("data-quick-filter"));
      chip.classList.toggle("is-active", active);
    });
  };

  const applyEmphasisPreset = (emphasis) => {
    const next = ["revenue", "profit", "weight"].includes(emphasis) ? emphasis : "revenue";
    state.workspaceEmphasis = next;
    const topMetric = document.getElementById("topMetric");
    const bubbleX = document.getElementById("bubbleXMetric");
    const moversMetric = document.getElementById("moversMetric");
    const bubbleColor = document.getElementById("bubbleColorBy");
    const bubbleY = document.getElementById("bubbleYMetric");
    if (next === "profit") {
      if (topMetric) topMetric.value = "profit";
      if (moversMetric) moversMetric.value = "profit";
      if (bubbleX) bubbleX.value = "gap_to_target";
      if (bubbleColor) bubbleColor.value = "status_key";
      if (bubbleY) bubbleY.value = "revenue";
      state.bubbleXMetric = "gap_to_target";
      state.bubbleColorBy = "status_key";
      state.bubbleYMetric = "revenue";
    } else if (next === "weight") {
      if (topMetric) topMetric.value = "weight";
      if (moversMetric) moversMetric.value = "qty";
      if (bubbleX) bubbleX.value = "gap_to_target";
      if (bubbleColor) bubbleColor.value = "segment";
      if (bubbleY) bubbleY.value = "velocity";
      state.bubbleXMetric = "gap_to_target";
      state.bubbleColorBy = "segment";
      state.bubbleYMetric = "velocity";
    } else {
      if (topMetric) topMetric.value = "revenue";
      if (moversMetric) moversMetric.value = "revenue";
      if (bubbleX) bubbleX.value = "gap_to_target";
      if (bubbleColor) bubbleColor.value = "status_key";
      if (bubbleY) bubbleY.value = "velocity";
      state.bubbleXMetric = "gap_to_target";
      state.bubbleColorBy = "status_key";
      state.bubbleYMetric = "velocity";
    }
    applyWorkspaceSettings();
    if (lastPayload) {
      renderTopProducts(lastPayload?.charts?.top_products || []);
      renderMovers(lastPayload?.charts?.movers || []);
      renderPerformanceBubble(lastPayload?.performance_bubble || {});
    }
  };

  const applyWatchlistPreset = (presetKey) => {
    const preset = WATCHLIST_PRESETS[presetKey];
    if (!preset) return;
    state.quickFilters = [...(preset.quickFilters || [])];
    state.segments = [];
    state.search = "";
    const searchEl = document.getElementById("tableSearch");
    if (searchEl) searchEl.value = "";
    const segmentSelect = document.getElementById("segmentFilter");
    if (segmentSelect) {
      Array.from(segmentSelect.options || []).forEach((option) => {
        option.selected = false;
      });
    }
    state.page = 1;
    if (preset.emphasis) {
      applyEmphasisPreset(preset.emphasis);
    }
    const inferredPreset = inferTablePreset({ quickFilters: state.quickFilters, section: preset.section, tablePreset: preset.tablePreset });
    if (inferredPreset) applyTablePreset(inferredPreset, { syncUi: true });
    if (preset.mode) {
      state.workspaceMode = preset.mode;
    }
    const nextScrollSection = localSubsetScrollSection(preset.section || "table", { quickFilters: state.quickFilters });
    if (nextScrollSection && !state.visibleSections.includes(nextScrollSection)) {
      state.visibleSections = [...state.visibleSections, nextScrollSection];
    }
    applyWorkspaceSettings();
    syncWatchlistButtons();
    refreshTableBundle();
    if (nextScrollSection === "table" && preset.section && preset.section !== "table") {
      updateTableLayerContextForSubset(preset.section, { quickFilters: state.quickFilters });
    }
    if (nextScrollSection && nextScrollSection !== "table") {
      ensureSectionGroup(preset.section, { force: !requestState[SECTION_GROUP_FOR_KEY[preset.section]]?.loaded });
    }
    if (nextScrollSection) {
      setTimeout(() => scrollToSection(nextScrollSection), 100);
    }
  };

  const ensureQuadrantModal = () => {
    if (document.getElementById("healthQuadrantModal")) return;
    const wrapper = document.createElement("div");
    wrapper.innerHTML = `
      <div class="modal fade" id="healthQuadrantModal" tabindex="-1" aria-hidden="true">
        <div class="modal-dialog modal-lg modal-dialog-scrollable">
          <div class="modal-content">
            <div class="modal-header">
              <h5 class="modal-title" id="healthQuadrantTitle">Quadrant items</h5>
              <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
            </div>
            <div class="modal-body">
              <div id="healthQuadrantBody" class="small text-muted">Loading${ELLIPSIS}</div>
            </div>
          </div>
        </div>
      </div>
    `;
    document.body.appendChild(wrapper.firstElementChild);
  };

  const openQuadrantModal = (quadrantLabel, topItems = [], quadrantKey = "") => {
    ensureQuadrantModal();
    const title = document.getElementById("healthQuadrantTitle");
    const body = document.getElementById("healthQuadrantBody");
    if (!title || !body) return;
    title.textContent = `${quadrantLabel || "Quadrant"} ${MIDDLE_DOT} Top items`;
    const rows = (Array.isArray(topItems) ? topItems : []).slice(0, 10);
    const tableRows = rows.length
      ? rows.map((item) => {
          const sku = getSku(item);
          return `<tr>
            <td>
              <div class="fw-semibold">${escapeHtml(item?.display_name || sku || EM_DASH)}</div>
              <div class="text-muted small">${escapeHtml(visualStatusLabel(item))}</div>
            </td>
            <td class="text-end">${fmtMoney0.format(item?.revenue || 0)}</td>
            <td class="text-end">${item?.margin_pct != null ? `${fmtPct1.format(item.margin_pct)}%` : EM_DASH}</td>
            <td class="text-end">${item?.velocity_per_month != null ? fmtInt.format(item.velocity_per_month) : EM_DASH}</td>
            <td class="text-end">
              <button type="button" class="btn btn-sm btn-outline-primary" data-quadrant-intel="${escapeHtml(sku)}">Open intelligence</button>
            </td>
          </tr>`;
        }).join("")
      : '<tr><td colspan="5" class="text-muted text-center">No items.</td></tr>';
    const exportBase = root.dataset.exportQuadrantCsv || "";
    const exportHref = exportBase ? appendFiltersToUrl(`${exportBase}?quadrant=${encodeURIComponent(quadrantKey || "")}`) : "#";
    body.innerHTML = `
      <div class="text-muted small mb-3">Open SKU intelligence from this list, then continue to the full drilldown from the side panel if needed.</div>
      <div class="d-flex justify-content-end mb-2"><a class="btn btn-sm btn-outline-secondary" href="${exportHref}"><i class="bi bi-download me-1"></i>Export full quadrant</a></div>
      <div class="table-responsive">
        <table class="table table-sm">
          <thead><tr><th>SKU</th><th class="text-end">Revenue</th><th class="text-end">Margin %</th><th class="text-end">Velocity/mo</th><th class="text-end">Action</th></tr></thead>
          <tbody>${tableRows}</tbody>
        </table>
      </div>
    `;
    body.querySelectorAll("[data-quadrant-intel]").forEach((node) => {
      node.addEventListener("click", () => {
        const sku = node.getAttribute("data-quadrant-intel") || "";
        const seedRow = rows.find((item) => getSku(item) === sku) || {};
        const modalEl = document.getElementById("healthQuadrantModal");
        try {
          const modal = modalEl ? bootstrap.Modal.getOrCreateInstance(modalEl) : null;
          modal?.hide();
        } catch (_err) {
          /* ignore */
        }
        renderProductIntel(seedRow, "Portfolio strategy", `${quadrantLabel || "Quadrant"} Top 10`, "Revenue", seedRow?.revenue || 0);
      });
    });
    try {
      const modal = bootstrap.Modal.getOrCreateInstance(document.getElementById("healthQuadrantModal"));
      modal.show();
    } catch (err) {
      console.error("quadrant modal", err);
    }
  };

  const quadrantActionMap = {
    protect: { quickFilters: ["protect_core"], section: "table", mode: "analyst" },
    fix_margin: { quickFilters: ["recover_margin"], section: "pricing", mode: "analyst" },
    grow: { quickFilters: ["promote_candidate"], section: "execution", mode: "analyst" },
    rationalize: { quickFilters: ["rationalize_candidate"], section: "assortment", mode: "analyst" },
  };
  const quadrantActionHints = {
    protect: "Protect supply, service level, and price discipline on core winners.",
    fix_margin: "Prioritize pricing and cost recovery on fast-moving low-margin SKUs.",
    grow: "Promote profitable laggards with distribution and sales support.",
    rationalize: "Review tail SKUs for simplification, pack changes, or exit.",
  };

  const renderHealthMatrix = (matrix = {}) => {
    const host = document.getElementById("healthMatrixPanel");
    if (!host) return;
    const quadrants = Array.isArray(matrix?.quadrants) ? matrix.quadrants : [];
    if (!quadrants.length) {
      host.innerHTML = '<div class="text-muted small">No product health data for the current filters.</div>';
      return;
    }
    const velocityLow = matrix.velocity_cutoff_low != null ? fmtInt.format(matrix.velocity_cutoff_low) : EM_DASH;
    const velocityHigh = matrix.velocity_cutoff_high != null ? fmtInt.format(matrix.velocity_cutoff_high) : EM_DASH;
    const profitabilityLow = matrix.profitability_cutoff_low != null ? `${matrix.profitability_cutoff_low > 0 ? "+" : ""}${fmtPct1.format(matrix.profitability_cutoff_low)} pts` : EM_DASH;
    const profitabilityHigh = matrix.profitability_cutoff_high != null ? `${matrix.profitability_cutoff_high > 0 ? "+" : ""}${fmtPct1.format(matrix.profitability_cutoff_high)} pts` : EM_DASH;
    host.innerHTML = quadrants
      .map((quadrant) => {
        const key = quadrant?.key || "";
        const topItems = Array.isArray(quadrant?.top_items) ? quadrant.top_items : [];
        const lead = topItems[0];
        const leadText = lead
          ? `${displayName(lead)} ${MIDDLE_DOT} ${fmtMoney0.format(lead?.revenue || 0)}`
          : "No qualifying SKU in the current visible scope.";
        const leadStatus = lead ? visualStatusLabel(lead) : "";
        const actionHint = quadrantActionHints[key] || quadrant?.description || "";
        return `
          <div class="health-card tone-${escapeHtml(quadrant.tone || "neutral")}">
            <div class="health-card-head">
              <div>
                <div class="health-kicker">${escapeHtml(quadrant.label || EM_DASH)}</div>
                <div class="health-value">${fmtInt.format(quadrant.sku_count || 0)}</div>
              </div>
              <div class="health-chip">${fmtPct1.format(quadrant.revenue_share || 0)}% revenue</div>
            </div>
            <div class="health-meta mt-2">${escapeHtml(actionHint)}</div>
            <div class="health-stat-grid mt-3">
              <div class="health-stat-cell">
                <div class="health-stat-label">Revenue</div>
                <div class="health-stat-value">${fmtMoney0.format(quadrant.revenue || 0)}</div>
              </div>
              <div class="health-stat-cell">
                <div class="health-stat-label">Profit</div>
                <div class="health-stat-value">${fmtMoney0.format(quadrant.profit || 0)}</div>
              </div>
              <div class="health-stat-cell">
                <div class="health-stat-label">Profit share</div>
                <div class="health-stat-value">${fmtPct1.format(quadrant.profit_share || 0)}%</div>
              </div>
            </div>
            <div class="health-top-item mt-3">
              <div class="health-top-label">Lead SKU</div>
              <div>${escapeHtml(leadText)}</div>
              ${leadStatus ? `<div class="health-meta mt-1">${escapeHtml(leadStatus)}</div>` : ""}
            </div>
            <div class="health-meta mt-3">Velocity bands: low <= ${escapeHtml(velocityLow)}, high >= ${escapeHtml(velocityHigh)} ${MIDDLE_DOT} Profitability bands: low <= ${escapeHtml(profitabilityLow)}, high >= ${escapeHtml(profitabilityHigh)} vs target</div>
            <div class="health-actions d-flex gap-2 mt-3">
              ${lead ? `<button type="button" class="btn btn-sm btn-outline-dark" data-health-intel="${escapeHtml(key)}">Open intelligence</button>` : ""}
              <button type="button" class="btn btn-sm btn-outline-secondary" data-health-open="${escapeHtml(key)}">Top 10 items</button>
              <button type="button" class="btn btn-sm btn-outline-primary" data-health-view="${escapeHtml(key)}">Open view</button>
            </div>
          </div>
        `;
      })
      .join("");

    host.querySelectorAll("[data-health-open]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const key = btn.getAttribute("data-health-open") || "";
        const rows = (quadrants.find((q) => (q?.key || "") === key)?.top_items || []);
        const label = quadrants.find((q) => (q?.key || "") === key)?.label || "Quadrant";
        openQuadrantModal(label, rows, key);
      });
    });
    host.querySelectorAll("[data-health-intel]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const key = btn.getAttribute("data-health-intel") || "";
        const lead = (quadrants.find((q) => (q?.key || "") === key)?.top_items || [])[0];
        if (!lead) return;
        renderProductIntel(lead, "Portfolio strategy", "Portfolio map (2x2)", "Revenue", lead?.revenue || 0);
      });
    });
    host.querySelectorAll("[data-health-view]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const key = btn.getAttribute("data-health-view") || "";
        const action = quadrantActionMap[key];
        if (action) applySignalAction(action);
      });
    });
  };

  const renderRiskOpportunity = (concentration = {}, risk = {}) => {
    setText(
      "concTop1Share",
      concentration?.top1_share != null ? `${fmtPct1.format(concentration.top1_share)}%` : EM_DASH
    );
    setText(
      "concTop10Share",
      concentration?.top10_share != null ? `${fmtPct1.format(concentration.top10_share)}%` : EM_DASH
    );
    setText(
      "concHhi",
      concentration?.hhi != null ? fmtInt.format(concentration.hhi) : EM_DASH
    );
    setText("concPareto80", concentration?.skus_to_80 ? fmtInt.format(concentration.skus_to_80) : EM_DASH);

    setText(
      "riskBelowTargetCount",
      `${fmtInt.format(risk?.below_target_count ?? 0)}${risk?.below_minimum_count ? ` (${fmtInt.format(risk.below_minimum_count)} below min)` : ""}`
    );
    setText("riskBelowTargetRevenue", fmtMoney0.format(risk?.below_target_revenue ?? 0));
    setText("riskNegativeCount", fmtInt.format(risk?.negative_margin_count ?? 0));
    setText("riskProfitUplift", fmtMoney0.format(risk?.profit_uplift_target ?? 0));

    const marginHost = document.getElementById("marginRiskList");
    if (marginHost) {
      const rows = Array.isArray(risk?.margin_risk_top) ? risk.margin_risk_top : [];
      if (!rows.length) {
        marginHost.innerHTML = '<span class="text-muted small">No below-target SKUs for current filters.</span>';
      } else {
        marginHost.innerHTML = rows
          .map((row, idx) => {
            const label = displayName(row);
            const margin = row?.margin_pct != null ? `${fmtPct1.format(row.margin_pct)}%` : EM_DASH;
            const target = row?.target_margin_pct != null ? `${fmtPct1.format(row.target_margin_pct)}%` : EM_DASH;
            const revenue = fmtMoney0.format(row?.revenue ?? 0);
            const gap = comparablePriceContext(row).gapToTarget;
            return `<button type="button" class="btn btn-link p-0 text-start text-decoration-none w-100 d-flex justify-content-between border-bottom py-1" data-margin-risk-idx="${idx}"><span>${escapeHtml(label)}</span><span>${revenue} ${MIDDLE_DOT} ${margin} / ${target} ${MIDDLE_DOT} ${escapeHtml(gap != null ? formatSignedMoney2(gap) : EM_DASH)}</span></button>`;
          })
          .join("");
        marginHost.querySelectorAll("[data-margin-risk-idx]").forEach((node) => {
          const idx = Number(node.getAttribute("data-margin-risk-idx"));
          const row = rows[idx];
          if (!row) return;
          node.addEventListener("click", () => {
            openDecisionWorkbench(row, { scroll: true, useRecommended: true, sourceLabel: "Margin risk" });
            renderProductIntel(row, "Pricing & Margin", "Margin risk", "Revenue", row.revenue || 0);
          });
        });
      }
    }

    const oppHost = document.getElementById("velocityOpportunityList");
    if (oppHost) {
      const hvlm = Array.isArray(risk?.high_velocity_low_margin) ? risk.high_velocity_low_margin.slice(0, 5) : [];
      const hmlv = Array.isArray(risk?.high_margin_low_velocity) ? risk.high_margin_low_velocity.slice(0, 5) : [];
      const build = (title, rows) => {
        if (!rows.length) return `<div class="text-muted">${escapeHtml(title)}: no matches.</div>`;
        const items = rows
          .map((row, idx) => {
            const label = displayName(row);
            const velocity = row?.orders_per_month != null ? fmtInt.format(row.orders_per_month) : EM_DASH;
            const margin = row?.margin_pct != null ? `${fmtPct1.format(row.margin_pct)}%` : EM_DASH;
            const revenue = fmtMoney0.format(row?.revenue ?? 0);
            return `<li><button type="button" class="btn btn-link p-0 text-start text-decoration-none velocity-opportunity-link" data-opportunity-row="${idx}" data-opportunity-title="${escapeHtml(title)}"><strong>${escapeHtml(label)}</strong> ${MIDDLE_DOT} ${revenue} ${MIDDLE_DOT} ${margin} ${MIDDLE_DOT} ${velocity}/mo</button></li>`;
          })
          .join("");
        return `<div class="mb-3"><div class="fw-semibold mb-1">${escapeHtml(title)}</div><ul class="mb-0">${items}</ul></div>`;
      };
      oppHost.innerHTML = `${build("High velocity, low margin", hvlm)}${build("High margin, low velocity", hmlv)}`;
      oppHost.querySelectorAll("[data-opportunity-row]").forEach((node) => {
        node.addEventListener("click", () => {
          const idx = Number(node.getAttribute("data-opportunity-row"));
          const title = node.getAttribute("data-opportunity-title") || "Opportunity";
          const sourceRows = title.includes("low margin") ? hvlm : hmlv;
          const row = sourceRows[idx];
          if (!row) return;
          renderProductIntel(row, "Execution", title, "Revenue", row.revenue || 0);
        });
      });
    }
  };

  const renderPricingGuardrails = (guardrails = {}) => {
    setText("guardrailHighOutliers", fmtInt.format(guardrails?.high_outlier_count ?? 0));
    setText("guardrailLowOutliers", fmtInt.format(guardrails?.low_outlier_count ?? 0));
    const outsidePct = guardrails?.outside_pct;
    setText(
      "guardrailOutsidePct",
      outsidePct != null ? `${fmtPct1.format(outsidePct)}% (${fmtInt.format(guardrails?.outside_count ?? 0)})` : EM_DASH
    );
    const host = document.getElementById("guardrailActionList");
    if (!host) return;
    const rows = Array.isArray(guardrails?.rows) ? guardrails.rows.slice(0, 12) : [];
    if (!rows.length) {
      host.innerHTML = '<span class="text-muted small">No pricing guardrail actions for current filters.</span>';
      return;
    }
    host.innerHTML = rows
      .map((row, idx) => {
        const asp = row?.unit_price != null ? fmtMoney2.format(row.unit_price) : EM_DASH;
        const p10 = row?.p10 != null ? fmtMoney2.format(row.p10) : EM_DASH;
        const p50 = row?.p50 != null ? fmtMoney2.format(row.p50) : EM_DASH;
        const p90 = row?.p90 != null ? fmtMoney2.format(row.p90) : EM_DASH;
        const cv = row?.price_cv_pct != null ? `${fmtPct1.format(row.price_cv_pct)}%` : EM_DASH;
        return `
          <button type="button" class="btn btn-link p-0 text-start text-decoration-none w-100 border-bottom py-2 pricing-action-link" data-pricing-action-idx="${idx}">
            <div class="d-flex justify-content-between gap-2">
              <span class="fw-semibold">${escapeHtml(row?.display_name || row?.product_id || EM_DASH)}</span>
              <span class="recommendation-badge">${escapeHtml(row?.action || "Hold")}</span>
            </div>
            <div class="text-muted small">ASP ${asp} ${MIDDLE_DOT} P10 ${p10} ${MIDDLE_DOT} P50 ${p50} ${MIDDLE_DOT} P90 ${p90} ${MIDDLE_DOT} CV ${cv}</div>
            <div class="text-muted small">${escapeHtml(row?.reason || "")}</div>
          </button>
        `;
      })
      .join("");
    host.querySelectorAll("[data-pricing-action-idx]").forEach((node) => {
      const idx = Number(node.getAttribute("data-pricing-action-idx"));
      const row = rows[idx];
      if (!row) return;
      node.addEventListener("click", () => {
        openDecisionWorkbench(row, { scroll: true, useRecommended: true, sourceLabel: "Pricing action" });
        renderProductIntel(row, "Pricing & Margin", "Pricing actions", row.action || "Pricing action", row.unit_price ?? row.revenue ?? 0);
      });
    });
  };

  const renderExecutionListBlock = (hostId, rows = []) => {
    const host = document.getElementById(hostId);
    if (!host) return;
    const data = Array.isArray(rows) ? rows.slice(0, 10) : [];
    if (!data.length) {
      host.innerHTML = '<span class="text-muted small">No matches.</span>';
      return;
    }
    host.innerHTML = data
      .map((row, idx) => {
        const revenue = fmtMoney0.format(row?.revenue ?? 0);
        const margin = row?.margin_pct != null ? `${fmtPct1.format(row.margin_pct)}%` : EM_DASH;
        const velocity = row?.orders_per_month != null ? formatVelocity(row.orders_per_month) : EM_DASH;
        const gap = row?.gap_to_minimum != null
          ? formatSignedMoney2(row.gap_to_minimum)
          : (row?.gap_to_target != null ? formatSignedMoney2(row.gap_to_target) : EM_DASH);
        const uplift = row?.profit_uplift_target != null ? fmtMoney0.format(row.profit_uplift_target) : EM_DASH;
        const status = renderRiskBadge(row?.target_status || row?.status_key || "Review", row?.status_key);
        return `
          <button type="button" class="btn btn-link p-0 text-start text-decoration-none w-100 border-bottom py-2" data-execution-row="${idx}" data-execution-host="${hostId}">
            <div class="d-flex justify-content-between gap-2">
              <span>${escapeHtml(row?.display_name || row?.product_id || EM_DASH)}</span>
              <span class="recommendation-badge">${escapeHtml(row?.action || "Review")}</span>
            </div>
            <div class="text-muted small">${revenue} ${MIDDLE_DOT} ${margin} ${MIDDLE_DOT} ${velocity}/mo ${MIDDLE_DOT} Gap ${escapeHtml(gap)}</div>
            <div class="text-muted small">Upside ${escapeHtml(uplift)} ${MIDDLE_DOT} ${status}</div>
            <div class="text-muted small">${escapeHtml(row?.reason || "")}</div>
          </button>
        `;
      })
      .join("");
    host.querySelectorAll("[data-execution-row]").forEach((node) => {
      const idx = Number(node.getAttribute("data-execution-row"));
      const row = data[idx];
      if (!row) return;
      node.addEventListener("click", () => {
        openDecisionWorkbench(row, { scroll: true, useRecommended: true, sourceLabel: row.action || "Execution action" });
        renderProductIntel(row, "Execution", hostId.replace(/^execution/, ""), row.action || "Execution", row.revenue || 0);
      });
    });
  };

  const renderExecutionLists = (execution = {}) => {
    renderExecutionListBlock("executionPricingFixes", execution?.pricing_fixes || []);
    renderExecutionListBlock("executionCostFixes", execution?.cost_fixes || []);
    renderExecutionListBlock("executionPromoteCandidates", execution?.promote_candidates || []);
  };

  const renderTableV1 = (tbody, rows, table, statusEl) => {
    rows.forEach((r) => {
      const sku = getSku(r);
      const intelUrl = drilldownTemplate
        ? drilldownTemplate.replace("__PID__", encodeURIComponent(sku))
        : (r.intel_url || "#");
      const link = appendFiltersToUrl(intelUrl);
      const recLabel = r.recommendation || EM_DASH;
      const quickRec = r.quick_rec || EM_DASH;
      const marginVal = r.margin != null ? fmtMoney2.format(r.margin) : EM_DASH;
      const tr = document.createElement("tr");
      tr.classList.add("table-row-clickable");
      tr.innerHTML = `
        <td>${escapeHtml(displayName(r))}</td>
        <td>${escapeHtml(r.segment ?? EM_DASH)}</td>
        <td class="text-end">${fmtMoney0.format(r.revenue ?? 0)}</td>
        <td class="text-end">${r.revenue_share != null ? `${fmtPct1.format(r.revenue_share)}%` : EM_DASH}</td>
        <td class="text-end">${fmtInt.format(r.qty ?? 0)}</td>
        <td class="text-end">${r.qty_share != null ? `${fmtPct1.format(r.qty_share)}%` : EM_DASH}</td>
        <td class="text-end">${r.unit_price != null ? fmtMoney2.format(r.unit_price) : EM_DASH}</td>
        <td class="text-end">${r.cost != null ? fmtMoney2.format(r.cost) : EM_DASH}</td>
        <td class="text-end">${r.profit != null ? fmtMoney2.format(r.profit) : EM_DASH}</td>
        <td class="text-end">${r.target_price != null ? fmtMoney2.format(r.target_price) : EM_DASH}</td>
        <td class="text-end">${r.uplift_pct != null ? `${fmtPct1.format(r.uplift_pct)}%` : EM_DASH}</td>
        <td class="text-end">${r.margin_pct != null ? `${fmtPct1.format(r.margin_pct)}%` : EM_DASH}</td>
        <td class="text-end">${marginVal}</td>
        <td>${escapeHtml(recLabel)}</td>
        <td>${escapeHtml(r.first_sold || EM_DASH)}</td>
        <td>${escapeHtml(r.last_sold || EM_DASH)}</td>
        <td>${escapeHtml(quickRec)}</td>
        <td class="text-center"><a class="btn btn-sm btn-outline-primary intel-btn" href="${link}" data-link="${link}" data-sku="${escapeHtml(sku)}">Intel</a></td>
      `;
      tr.querySelector(".intel-btn")?.addEventListener("click", (evt) => {
        evt.preventDefault();
        persistSnapshot(lastPayload);
        window.location.href = link;
      });
      tr.addEventListener("click", (evt) => {
        if (evt.target.closest("a,button,input,select,label")) return;
        persistSnapshot(lastPayload);
        window.location.href = link;
      });
      tbody.appendChild(tr);
    });
    if (statusEl) statusEl.textContent = `Page ${table.page || 1} ${MIDDLE_DOT} ${rows.length} rows (of ${table.total ?? rows.length})`;
  };

  const renderTableV2 = (tbody, rows, table, statusEl) => {
    rows.forEach((r) => {
      const sku = getSku(r);
      const intelUrl = drilldownTemplate
        ? drilldownTemplate.replace("__PID__", encodeURIComponent(sku))
        : (r.intel_url || "#");
      const link = appendFiltersToUrl(intelUrl);
      const productLabel = r.product_name || r.label || r.display_name || sku || EM_DASH;
      const tr = document.createElement("tr");
      tr.classList.add("table-row-clickable");
      tr.dataset.statusKey = String(r?.status_key || "").toLowerCase();
      tr.innerHTML = `
        <td class="sticky-col sticky-col-1" data-column="sku">${escapeHtml(r.sku || sku || EM_DASH)}</td>
        <td class="sticky-col sticky-col-2" data-column="product">
          <div class="product-table-subcell">
            <span class="product-name-cell" title="${escapeHtml(productLabel)}">${escapeHtml(productLabel)}</span>
            <div class="product-table-subcell-meta">${escapeHtml(r.top_customer_name ? `Top customer: ${r.top_customer_name}` : (r.top_region_name ? `Lead region: ${r.top_region_name}` : "Click for SKU detail"))}</div>
          </div>
        </td>
        <td data-column="protein_family">${renderProteinCategoryCell(r)}</td>
        <td data-column="product_category">${escapeHtml(r.product_category || EM_DASH)}</td>
        <td data-column="segment">${renderSegmentBadge(r.segment)}</td>
        <td data-column="supplier">${escapeHtml(r.supplier || EM_DASH)}</td>
        <td class="text-end" data-column="customer_count">${fmtInt.format(r.customer_count ?? 0)}</td>
        <td class="text-end" data-column="supplier_count">${fmtInt.format(r.supplier_count ?? 0)}</td>
        <td class="text-end" data-column="region_breadth">${fmtInt.format(r.region_breadth ?? 0)}</td>
        <td class="text-end" data-column="top_customer_share">${r.top_customer_share != null ? `${fmtPct1.format(r.top_customer_share)}%` : EM_DASH}</td>
        <td class="text-end" data-column="customer_hhi">${r.customer_hhi != null ? fmtInt.format(r.customer_hhi) : EM_DASH}</td>
        <td class="text-end" data-column="revenue">${fmtMoney0.format(r.revenue ?? 0)}</td>
        <td class="text-end" data-column="revenue_current">${fmtMoney0.format(r.revenue_current ?? 0)}</td>
        <td class="text-end" data-column="revenue_prior">${fmtMoney0.format(r.revenue_prior ?? 0)}</td>
        <td class="text-end" data-column="revenue_delta">${formatSignedMoney(r.revenue_delta)}</td>
        <td class="text-end" data-column="revenue_delta_pct">${formatRevenueDeltaPct(r)}</td>
        <td class="text-end" data-column="revenue_share">${r.revenue_share != null ? `${fmtPct1.format(r.revenue_share)}%` : EM_DASH}</td>
        <td class="text-end" data-column="orders">${fmtInt.format(r.orders ?? 0)}</td>
        <td class="text-end" data-column="orders_current">${fmtInt.format(r.orders_current ?? 0)}</td>
        <td class="text-end" data-column="orders_prior">${fmtInt.format(r.orders_prior ?? 0)}</td>
        <td class="text-end" data-column="velocity_per_month">${r.velocity_per_month != null ? formatVelocity(r.velocity_per_month) : (r.orders_per_month != null ? formatVelocity(r.orders_per_month) : EM_DASH)}</td>
        <td class="text-end" data-column="qty">${fmtInt.format(r.qty ?? 0)}</td>
        <td class="text-end" data-column="weight">${fmtInt.format(r.weight ?? 0)}</td>
        <td class="text-end" data-column="current_unit_price">${r.current_unit_price != null ? fmtMoney2.format(r.current_unit_price) : EM_DASH}</td>
        <td class="text-end" data-column="minimum_price">${r.minimum_price != null ? fmtMoney2.format(r.minimum_price) : EM_DASH}</td>
        <td class="text-end" data-column="target_price">${r.target_price != null ? fmtMoney2.format(r.target_price) : EM_DASH}</td>
        <td class="text-end" data-column="uplift_pct">${r.uplift_pct != null ? `${fmtPct1.format(r.uplift_pct)}%` : EM_DASH}</td>
        <td class="text-end" data-column="cost">${r.cost != null ? fmtMoney2.format(r.cost) : EM_DASH}</td>
        <td class="text-end" data-column="profit">${r.profit != null ? fmtMoney2.format(r.profit) : EM_DASH}</td>
        <td class="text-end" data-column="profit_current">${r.profit_current != null ? fmtMoney2.format(r.profit_current) : EM_DASH}</td>
        <td class="text-end" data-column="profit_prior">${r.profit_prior != null ? fmtMoney2.format(r.profit_prior) : EM_DASH}</td>
        <td class="text-end" data-column="profit_delta">${formatSignedMoney(r.profit_delta)}</td>
        <td class="text-end" data-column="profit_share">${r.profit_share != null ? `${fmtPct1.format(r.profit_share)}%` : EM_DASH}</td>
        <td class="text-end" data-column="contribution_margin_lb">${r.contribution_margin_lb != null ? fmtMoney2.format(r.contribution_margin_lb) : EM_DASH}</td>
        <td class="text-end" data-column="asp_lb">${r.asp_lb != null ? fmtMoney2.format(r.asp_lb) : EM_DASH}</td>
        <td class="text-end" data-column="asp_lb_gap_to_min">${renderBandValue(r.asp_lb_gap_to_min, r.price_band_status || r.margin_status || r.status_key, formatSignedMoney2)}</td>
        <td class="text-end" data-column="minimum_price_lb">${r.minimum_price_lb != null ? fmtMoney2.format(r.minimum_price_lb) : EM_DASH}</td>
        <td class="text-end" data-column="target_price_lb">${r.target_price_lb != null ? fmtMoney2.format(r.target_price_lb) : EM_DASH}</td>
        <td class="text-end" data-column="asp_lb_gap_to_target">${renderBandValue(r.asp_lb_gap_to_target, r.price_band_status || r.margin_status || r.status_key, formatSignedMoney2)}</td>
        <td class="text-end" data-column="cost_lb">${r.cost_lb != null ? fmtMoney2.format(r.cost_lb) : EM_DASH}</td>
        <td class="text-end" data-column="margin_pct">${renderBandValue(r.margin_pct, r.margin_band_status || r.margin_status || r.status_key, (value) => `${fmtPct1.format(value)}%`)}</td>
        <td class="text-end" data-column="minimum_margin_pct">${r.minimum_margin_pct != null ? `${fmtPct1.format(r.minimum_margin_pct)}%` : EM_DASH}</td>
        <td class="text-end" data-column="target_margin_pct">${r.target_margin_pct != null ? `${fmtPct1.format(r.target_margin_pct)}%` : EM_DASH}</td>
        <td class="text-end" data-column="margin_pct_prior">${r.margin_pct_prior != null ? `${fmtPct1.format(r.margin_pct_prior)}%` : EM_DASH}</td>
        <td class="text-end" data-column="margin_delta_pp">${r.margin_delta_pp != null ? `${r.margin_delta_pp > 0 ? "+" : ""}${fmtPct1.format(r.margin_delta_pp)} pp` : EM_DASH}</td>
        <td class="text-end" data-column="price_variance_vs_median">${r.price_variance_vs_median != null ? fmtMoney2.format(r.price_variance_vs_median) : EM_DASH}</td>
        <td class="text-end" data-column="volatility_score">${r.volatility_score != null ? `${fmtPct1.format(r.volatility_score)}%` : EM_DASH}</td>
        <td data-column="margin_risk">${renderRiskBadge(r.target_status || r.margin_risk, r.margin_status || r.status_key)}</td>
        <td data-column="recommendation">${renderRecommendationBadge(r.recommendation || r.quick_rec || "Review")}</td>
        <td data-column="first_sold">${escapeHtml(r.first_sold || EM_DASH)}</td>
        <td data-column="last_sold">${escapeHtml(r.last_sold || EM_DASH)}</td>
        <td data-column="quick_rec">${escapeHtml(r.quick_rec || EM_DASH)}</td>
        <td class="text-center"><a class="btn btn-sm btn-outline-primary intel-btn" href="${link}" data-link="${link}" data-sku="${escapeHtml(sku)}">Intel</a></td>
      `;
      tr.querySelector(".intel-btn")?.addEventListener("click", (evt) => {
        evt.preventDefault();
        persistSnapshot(lastPayload);
        window.location.href = link;
      });
      tr.addEventListener("click", (evt) => {
        if (evt.target.closest("a,button,input,select,label")) return;
        persistSnapshot(lastPayload);
        window.location.href = link;
      });
      tbody.appendChild(tr);
    });
    applyColumnVisibility();
    if (statusEl) {
      const quick = state.quickFilters?.length ? ` ${MIDDLE_DOT} ${state.quickFilters.length} quick filter${state.quickFilters.length > 1 ? "s" : ""}` : "";
      statusEl.textContent = `Page ${table.page || 1} ${MIDDLE_DOT} ${rows.length} rows (of ${table.total ?? rows.length})${quick}`;
    }
  };

  const renderTable = (table = {}) => {
    const tbody = document.getElementById("productTbody");
    const statusEl = document.getElementById("tableStatus");
    if (!tbody) return;
    tbody.innerHTML = "";
    const rows = table.rows || [];
    if (!rows.length) {
      const colspan = isV4 ? 57 : (isV3 ? 34 : (isV2 ? 24 : 18));
      tbody.innerHTML = `<tr><td colspan="${colspan}" class="text-center text-muted">No data for current filters.</td></tr>`;
      if (isV2) applyColumnVisibility();
      if (statusEl) statusEl.textContent = "0 rows";
      return;
    }
    if (isV2) renderTableV2(tbody, rows, table, statusEl);
    else renderTableV1(tbody, rows, table, statusEl);
    const prev = document.getElementById("tablePrev");
    const next = document.getElementById("tableNext");
    if (prev) prev.disabled = (table.page || 1) <= 1;
    if (next) next.disabled = (table.page || 1) * (table.page_size || state.pageSize) >= (table.total || 0);
    removeSkeleton("productTbody");
  };

  // ---------- Fetch + orchestrate ----------
  const hideProductsError = () => {
    const box = document.getElementById("productsError");
    if (box) box.classList.add("d-none");
  };

  const showProductsError = (message) => {
    const box = document.getElementById("productsError");
    if (!box) return;
    box.classList.remove("d-none");
    box.textContent = message || "Unable to load products.";
  };

  const renderSummaryBundle = (payload = {}) => {
    const elasticWatch = buildElasticGuardrailWatch(payload);
    const rootCause = buildRootCauseSummary(payload);
    const alertCandidates = buildAlertCandidates(payload, elasticWatch);
    renderHero(payload.kpis || {}, payload.meta || {}, payload.comparison || {});
    renderComparisonContext(payload.comparison || {}, payload.story || {});
    renderActiveFilterSummary();
    renderSectionBriefs(payload);
    renderPortfolioPosture(payload.portfolio_posture || {}, payload.focus_actions || []);
    renderDecisionSignals(payload.decision_signals || []);
    renderFocusActions(payload.focus_actions || []);
    renderRootCauseSummary(rootCause);
    renderAlertCandidates("alertCandidateList", alertCandidates);
    renderKpis(payload.kpis || {});
    bindKpiCards();
    renderVelocity(payload.velocity || {});
    renderInsights(payload.insights || [], payload.projected_next_month || null, payload.comparison || {});
    bindInsightCards();
    renderAISignals(payload.ai_signals || {});
    renderStrategyBrief((payload.charts || {}).segments || {}, payload.concentration || {});
    renderTrajectory((payload.charts || {}).trajectory || {}, payload.forecast_overlay || [], payload.comparison || {});
    renderHealthMatrix(payload.health_matrix || {});
  };

  const renderDetailBundle = (payload = {}) => {
    const chartsPayload = payload.charts || {};
    const elasticWatch = buildElasticGuardrailWatch(payload);
    const alertCandidates = buildAlertCandidates(payload, elasticWatch);
    renderSectionBriefs(payload);
    renderVelocity(payload.velocity || {});
    renderInsights(payload.insights || [], payload.projected_next_month || null, payload.comparison || {});
    bindInsightCards();
    renderHealthMatrix(payload.health_matrix || {});
    renderProteinIntelligence(payload.protein_insights || {});
    renderPriceVelocity(payload.price_vs_velocity || chartsPayload.price_velocity || []);
    renderPerformanceBubble(payload.performance_bubble || {});
    renderPriceDist(chartsPayload.unit_price_dist || []);
    renderMovers(chartsPayload.movers || []);
    renderTopProducts(chartsPayload.top_products || []);
    renderPareto(chartsPayload.pareto || []);
    const segments = chartsPayload.segments || {};
    renderSegmentSummary(segments.summary || []);
    renderSegmentMovers(segments.movers || []);
    renderSegmentMixShift(segments.mix_shift || []);
    renderRecommendations(payload.recommendations || []);
    renderRiskOpportunity(payload.concentration || {}, payload.risk_opportunity || {});
    renderPricingGuardrails(payload.pricing_guardrails || {});
    renderElasticGuardrails(elasticWatch);
    renderAlertCandidates("pricingAlertCandidateList", alertCandidates);
    renderExecutionLists(payload.execution_lists || {});
    renderDecisionWorkbench();
    renderStagedActions();
  };

  const renderTableBundle = (payload = {}) => {
    renderTable(payload.table || {});
  };

  const finalizeBundleRender = () => {
    applyWorkspaceSettings();
    syncWatchlistButtons();
    syncQuickFilterButtons();
    syncTablePresetButtons();
    syncExportLinks();
    hydrateTooltips(document);
  };

  const setTableLoadingState = (message = `Loading${ELLIPSIS}`) => {
    const statusEl = document.getElementById("tableStatus");
    if (statusEl) statusEl.textContent = message;
  };

  const logBundleDebug = (group, payload) => {
    if (payload?.meta?.cached === undefined) return;
    console.debug("[products bundle]", {
      group,
      cached: payload.meta.cached,
      sections: payload.meta.sections,
      bundle_mode: payload.meta.bundle_mode,
      duckdb_query_count: payload.meta.duckdb_query_count,
      duckdb_ms: payload.meta.duckdb_ms,
      total_ms: payload.meta.total_ms || payload.meta.duration_ms,
      dataset_version: payload.meta.dataset_version,
    });
  };

  const dispatchGlobalApplyAck = () => {
    if (!pendingGlobalApplyAck) return;
    pendingGlobalApplyAck = false;
    const applyId = pendingGlobalApplyId;
    pendingGlobalApplyId = "";
    const detail = { qs: state.qs };
    if (applyId) detail.applyId = applyId;
    try {
      if (typeof window.dispatchGlobalFiltersApplied === "function") {
        window.dispatchGlobalFiltersApplied(detail);
      } else {
        window.dispatchEvent(new CustomEvent("globalFilters:applied", { detail }));
      }
    } catch (err) {
      /* ignore */
    }
  };

  const abortGroupRequest = (group) => {
    const groupState = requestState[group];
    if (groupState?.abort) {
      groupState.abort.abort();
      groupState.abort = null;
    }
    if (groupState) groupState.loading = false;
  };

  const resetBundleRequests = () => {
    Object.keys(requestState).forEach((group) => {
      abortGroupRequest(group);
      requestState[group].loaded = false;
    });
  };

  const fetchBundleSection = async (group, { force = false } = {}) => {
    const groupState = requestState[group];
    if (!groupState) return null;
    if (groupState.loaded && !force) return lastPayload;
    abortGroupRequest(group);
    const controller = new AbortController();
    const reqId = ++groupState.reqId;
    groupState.abort = controller;
    groupState.loading = true;
    state.qs = buildHistoryQS();
    replaceHistory(state.qs);
    const qs = buildSectionQS(group);
    const url = `${bundleUrl}?${qs}`;
    if (group === "table") setTableLoadingState();
    hideProductsError();
    try {
      const res = await authFetch(url, {
        signal: controller.signal,
        credentials: "same-origin",
        headers: pageCache ? pageCache.prepareHeaders(url, { Accept: "application/json" }) : { Accept: "application/json" },
      });
      if (pageCache) pageCache.rememberResponse(url, res);
      if (res.status === 304) {
        groupState.loaded = true;
        if (group === "summary" && pendingGlobalApplyAck) dispatchGlobalApplyAck();
        return lastPayload;
      }
      const raw = await res.json();
      if (reqId !== groupState.reqId) return null;
      const partialPayload = window.normalizeBundlePayload ? window.normalizeBundlePayload(raw) : raw;
      if (!res.ok) throw new Error(partialPayload?.error?.message || `HTTP ${res.status}`);
      lastPayload = mergePayload(lastPayload || {}, partialPayload || {});
      if (group === "summary") renderSummaryBundle(lastPayload);
      if (group === "detail") renderDetailBundle(lastPayload);
      if (group === "table") renderTableBundle(lastPayload);
      finalizeBundleRender();
      groupState.loaded = true;
      persistSnapshot(lastPayload);
      logBundleDebug(group, partialPayload);
      if (group === "summary") dispatchGlobalApplyAck();
      return lastPayload;
    } catch (err) {
      if (err?.name === "AbortError") return;
      if (reqId !== groupState.reqId) return null;
      console.error("products bundle failed", err);
      if (group === "table") setTableLoadingState("Table refresh failed");
      showProductsError(err?.message || "Unable to load products.");
      return null;
    } finally {
      if (reqId === groupState.reqId) {
        groupState.loading = false;
        groupState.abort = null;
      }
    }
  };

  // ---------- Drilldown modal ----------
  const ensureIntelModal = () => {
    if (document.getElementById("intelModal")) return;
    const modal = document.createElement("div");
    modal.innerHTML = `
      <div class="modal fade" id="intelModal" tabindex="-1" aria-hidden="true">
        <div class="modal-dialog modal-lg modal-dialog-scrollable">
          <div class="modal-content">
            <div class="modal-header">
              <h5 class="modal-title" id="intelTitle">Product Intel</h5>
              <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
            </div>
            <div class="modal-body">
              <div id="intelBody">Loading${ELLIPSIS}</div>
            </div>
          </div>
        </div>
      </div>`;
    document.body.appendChild(modal.firstElementChild);
  };

  const renderIntelModal = (payload) => {
    ensureIntelModal();
    const body = document.getElementById("intelBody");
    const title = document.getElementById("intelTitle");
    if (!body || !title) return;
    const k = payload.kpis || {};
    const entityName = payload.meta?.entity_display_name || payload.meta?.entity_label || payload.meta?.entity_id || k.product_id || "Product";
    title.textContent = `Intel ${EM_DASH} ${entityName}`;
    const rows = (payload.table && payload.table.rows) || [];
    const list = rows
      .map(
        (r) =>
          `<tr><td>${r.label || r.key}</td><td class="text-end">${fmtMoney0.format(r.revenue || 0)}</td><td class="text-end">${fmtInt.format(
            r.qty || 0
          )}</td></tr>`
      )
      .join("");
    body.innerHTML = `
      <div class="row g-2 mb-3">
        <div class="col-6"><div class="mini-kpi-label">Revenue</div><div class="mini-kpi-value">${fmtMoney0.format(k.revenue || 0)}</div></div>
        <div class="col-6"><div class="mini-kpi-label">Quantity</div><div class="mini-kpi-value">${fmtInt.format(k.qty || 0)}</div></div>
        <div class="col-6"><div class="mini-kpi-label">Customers</div><div class="mini-kpi-value">${fmtInt.format(k.customers || 0)}</div></div>
        <div class="col-6"><div class="mini-kpi-label">Orders</div><div class="mini-kpi-value">${fmtInt.format(k.orders || 0)}</div></div>
      </div>
      <h6 class="mt-2">Top customers</h6>
      <div class="table-responsive">
        <table class="table table-sm">
          <thead><tr><th>Customer</th><th class="text-end">Revenue</th><th class="text-end">Qty</th></tr></thead>
          <tbody>${list || '<tr><td colspan="3" class="text-muted text-center">No customer data</td></tr>'}</tbody>
        </table>
      </div>
    `;
    try {
      const modal = bootstrap.Modal.getOrCreateInstance(document.getElementById("intelModal"));
      modal.show();
    } catch (err) {
      console.error("intel modal", err);
    }
  };

  const fetchDrilldown = async (sku, fallbackLink) => {
    if (!sku) {
      if (fallbackLink) window.location.href = fallbackLink;
      return;
    }
    ensureIntelModal();
    const body = document.getElementById("intelBody");
    if (body) body.textContent = `Loading${ELLIPSIS}`;
    const qs = state.qs ? `${state.qs.replace(/^[?]/, "")}&` : "";
    const url = `/api/products/drilldown/bundle?sku=${encodeURIComponent(sku)}${qs}`;
    try {
    const res = await authFetch(url, { headers: { Accept: "application/json" }, credentials: "same-origin" });
      const payload = await res.json();
      if (!res.ok) throw new Error(payload?.error?.message || `HTTP ${res.status}`);
      renderIntelModal(payload);
    } catch (err) {
      console.error("drilldown fetch failed", err);
      if (fallbackLink) window.location.href = fallbackLink;
    }
  };

  // ---------- Event wiring ----------
  const wireSorting = () => {
    document.querySelectorAll("#productTable th.sortable").forEach((th) => {
      th.addEventListener("click", () => {
        const key = th.dataset.key || "revenue";
        if (state.sortBy === key) {
          state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
        } else {
          state.sortBy = key;
          state.sortDir = "desc";
        }
        state.page = 1;
        refreshTableBundle();
        document.querySelectorAll("#productTable th.sortable").forEach((t) => t.classList.remove("asc", "desc"));
        th.classList.add(state.sortDir);
      });
    });
  };

  const wireControls = () => {
    if (isV4) {
      document.getElementById("workspaceModeExecutive")?.addEventListener("click", () => {
        state.workspaceMode = "executive";
        applyWorkspaceSettings();
      });
      document.getElementById("workspaceModeAnalyst")?.addEventListener("click", () => {
        state.workspaceMode = "analyst";
        applyWorkspaceSettings();
      });
      document.getElementById("workspaceDensityComfortable")?.addEventListener("click", () => {
        state.workspaceDensity = "comfortable";
        applyWorkspaceSettings();
      });
      document.getElementById("workspaceDensityCompact")?.addEventListener("click", () => {
        state.workspaceDensity = "compact";
        applyWorkspaceSettings();
      });
      document.getElementById("workspaceEmphasis")?.addEventListener("change", (evt) => {
        applyEmphasisPreset(evt.target.value);
      });
      document.querySelectorAll("[data-section-toggle]").forEach((input) => {
        input.addEventListener("change", (evt) => {
          const key = evt.target.getAttribute("data-section-toggle");
          if (!key) return;
          const next = new Set(state.visibleSections || []);
          if (evt.target.checked) next.add(key);
          else next.delete(key);
          state.visibleSections = WORKSPACE_SECTION_KEYS.filter((sectionKey) => next.has(sectionKey));
          applyWorkspaceSettings();
        });
      });
      document.querySelectorAll("[data-watchlist-preset]").forEach((btn) => {
        btn.addEventListener("click", () => {
          applyWatchlistPreset(btn.getAttribute("data-watchlist-preset") || "");
        });
      });
      document.querySelectorAll("[data-table-preset]").forEach((btn) => {
        btn.addEventListener("click", () => {
          const presetKey = btn.getAttribute("data-table-preset") || "";
          applyTablePreset(presetKey, { syncUi: true });
          if (TABLE_PRESETS[presetKey]?.note) setText("tableLayerContext", TABLE_PRESETS[presetKey].note);
        });
      });
    }

    const searchEl = document.getElementById("tableSearch");
    if (searchEl) {
      let timer = null;
      searchEl.addEventListener("input", (e) => {
        clearTimeout(timer);
        timer = setTimeout(() => {
          state.search = e.target.value.trim();
          state.page = 1;
          refreshTableBundle();
        }, 300);
      });
    }

    const pageSizeEl = document.getElementById("tablePageSize");
    if (pageSizeEl) {
      pageSizeEl.addEventListener("change", (e) => {
        state.pageSize = Number(e.target.value || 25);
        state.page = 1;
        refreshTableBundle();
      });
    }

    const prev = document.getElementById("tablePrev");
    const next = document.getElementById("tableNext");
    if (prev) prev.addEventListener("click", () => { state.page = Math.max(1, state.page - 1); refreshTableBundle(); });
    if (next) next.addEventListener("click", () => { state.page += 1; refreshTableBundle(); });

    const topMetric = document.getElementById("topMetric");
    const topN = document.getElementById("topN");
    [topMetric, topN].forEach((el) => el && el.addEventListener("change", () => renderTopProducts(lastPayload?.charts?.top_products || [])));
    const moversMetric = document.getElementById("moversMetric");
    if (moversMetric) {
      moversMetric.addEventListener("change", () => renderMovers(lastPayload?.charts?.movers || []));
    }

    const segmentSelect = document.getElementById("segmentFilter");
    if (segmentSelect) {
      segmentSelect.addEventListener("change", () => {
        const chosen = Array.from(segmentSelect.selectedOptions || []).map((o) => o.value).filter(Boolean);
        state.segments = chosen;
        state.page = 1;
        refreshTableBundle();
      });
    }

    if (isV2) {
      document.querySelectorAll("#tableQuickFilters [data-quick-filter]").forEach((btn) => {
        btn.addEventListener("click", () => {
          const key = btn.getAttribute("data-quick-filter");
          if (!key) return;
          const next = new Set(state.quickFilters || []);
          if (next.has(key)) next.delete(key);
          else next.add(key);
          state.quickFilters = Array.from(next);
          state.page = 1;
          syncQuickFilterButtons();
          syncWatchlistButtons();
          refreshTableBundle();
        });
      });
      document.querySelectorAll("#columnGroups [data-col-group]").forEach((btn) => {
        btn.addEventListener("click", () => {
          const group = btn.getAttribute("data-col-group");
          if (!group) return;
          applyColumnGroup(group);
          document.querySelectorAll("#columnGroups [data-col-group]").forEach((chip) => {
            chip.classList.toggle("active", chip.getAttribute("data-col-group") === group);
          });
        });
      });
    }

    const forecastToggle = document.getElementById("toggleForecast");
    if (forecastToggle) {
      forecastToggle.addEventListener("change", (e) => {
        state.showForecast = Boolean(e.target.checked);
        refreshSummaryBundle();
      });
    }

    const bubbleTop = document.getElementById("bubbleTopN");
    const bubbleX = document.getElementById("bubbleXMetric");
    const bubbleColor = document.getElementById("bubbleColorBy");
    const bubbleY = document.getElementById("bubbleYMetric");
    const bubbleReset = document.getElementById("bubbleResetZoom");
    const bubbleInclude = document.getElementById("bubbleIncludeMissing");
    [bubbleTop, bubbleX, bubbleColor, bubbleY].forEach((el) =>
      el &&
      el.addEventListener("change", (e) => {
        const id = e.target.id;
        if (id === "bubbleTopN") state.bubbleTopN = e.target.value;
        if (id === "bubbleXMetric") state.bubbleXMetric = e.target.value;
        if (id === "bubbleColorBy") state.bubbleColorBy = e.target.value;
        if (id === "bubbleYMetric") state.bubbleYMetric = e.target.value;
        if (id === "bubbleTopN") {
          refreshDetailBundle();
          return;
        }
        if (!requestState.detail.loaded) {
          ensureGroupLoaded("detail");
          return;
        }
        renderPerformanceBubble(lastPayload?.performance_bubble || {});
      })
    );
    if (bubbleInclude) {
      bubbleInclude.addEventListener("change", () => renderPerformanceBubble(lastPayload?.performance_bubble || {}));
    }
    if (bubbleReset) {
      bubbleReset.addEventListener("click", () => {
        renderPerformanceBubble(lastPayload?.performance_bubble || {});
      });
    }
    initProductIntelPanel();

    document.getElementById("productIntelFocusTable")?.addEventListener("click", () => {
      const sku = getSku(activeProductIntel?.row);
      if (!sku) return;
      hideProductIntel();
      applyDetailView({
        search: sku,
        section: "table",
        mode: "analyst",
        tablePreset: inferTablePreset(activeProductIntel?.suggestedAction?.view || {}) || "summary",
      });
    });
    document.getElementById("productIntelOpenDrilldown")?.addEventListener("click", (evt) => {
      if (evt.currentTarget.classList.contains("disabled")) {
        evt.preventDefault();
        return;
      }
      evt.preventDefault();
      persistSnapshot(lastPayload);
      window.location.href = evt.currentTarget.href;
    });
    document.getElementById("productIntelApplyAction")?.addEventListener("click", () => {
      const row = activeProductIntel?.row;
      if (!row) return;
      openDecisionWorkbench(row, { scroll: true, useRecommended: true, sourceLabel: "SKU intelligence" });
      stageWorkbenchAction();
      hideProductIntel();
    });

    document.getElementById("workbenchScenarioRange")?.addEventListener("input", (evt) => {
      if (!state.workbenchSelection) return;
      state.workbenchSelection.scenarioPct = Number(evt.target.value || 0);
      renderDecisionWorkbench();
    });
    document.getElementById("workbenchUseRecommended")?.addEventListener("click", () => {
      if (!state.workbenchSelection) return;
      state.workbenchSelection.scenarioPct = state.workbenchSelection.recommendedPct || 0;
      renderDecisionWorkbench();
    });
    document.getElementById("workbenchStageAction")?.addEventListener("click", () => {
      stageWorkbenchAction();
    });
    document.getElementById("workbenchClearAction")?.addEventListener("click", () => {
      state.workbenchSelection = null;
      renderDecisionWorkbench();
    });
    document.getElementById("stagedActionsClearAll")?.addEventListener("click", () => {
      state.stagedActions = [];
      writeStagedActions();
      renderStagedActions();
    });
  };

  const replaceHistory = (qs) => {
    if (!window.history || typeof window.history.replaceState !== "function") return;
    const nextUrl = qs ? `${window.location.pathname}?${qs}` : window.location.pathname;
    window.history.replaceState({}, "", nextUrl);
  };

  const ensureGroupLoaded = (group, options = {}) => fetchBundleSection(group, options);

  const ensureSectionGroup = (sectionKey, options = {}) => {
    const group = SECTION_GROUP_FOR_KEY[sectionKey];
    if (!group) return Promise.resolve(lastPayload);
    return ensureGroupLoaded(group, options);
  };

  const setupLazySectionObserver = () => {
    if (sectionObserver?.disconnect) sectionObserver.disconnect();
    sectionObserver = null;
    const targets = [
      { group: "detail", selector: "#products-pricing" },
      { group: "table", selector: "#products-table" },
    ].filter(({ group }) => (SECTION_GROUPS[group] || []).some((key) => state.visibleSections.includes(key)));
    if (!targets.length) return;
    if (typeof window.IntersectionObserver !== "function") {
      window.setTimeout(() => {
        targets.forEach(({ group }) => ensureGroupLoaded(group));
      }, 120);
      return;
    }
    sectionObserver = new window.IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          const group = entry.target.getAttribute("data-bundle-group");
          if (!group) return;
          ensureGroupLoaded(group);
          sectionObserver?.unobserve(entry.target);
        });
      },
      { rootMargin: "320px 0px" }
    );
    targets.forEach(({ group, selector }) => {
      const node = document.querySelector(selector);
      if (!node) return;
      node.setAttribute("data-bundle-group", group);
      sectionObserver.observe(node);
    });
  };

  const refreshSummaryBundle = () => {
    state.qs = buildHistoryQS();
    replaceHistory(state.qs);
    requestState.summary.loaded = false;
    return fetchBundleSection("summary", { force: true }).then((payload) => {
      setupLazySectionObserver();
      return payload;
    });
  };

  const refreshDetailBundle = () => {
    state.qs = buildHistoryQS();
    replaceHistory(state.qs);
    requestState.detail.loaded = false;
    return fetchBundleSection("detail", { force: true });
  };

  const refreshTableBundle = () => {
    state.qs = buildHistoryQS();
    replaceHistory(state.qs);
    requestState.table.loaded = false;
    return fetchBundleSection("table", { force: true });
  };

  const triggerFetch = ({ ackAfter = false, preserveExisting = true } = {}) => {
    pendingGlobalApplyAck = pendingGlobalApplyAck || ackAfter;
    resetBundleRequests();
    if (!preserveExisting) lastPayload = null;
    requestState.summary.loaded = false;
    requestState.detail.loaded = false;
    requestState.table.loaded = false;
    return refreshSummaryBundle();
  };

  const applyFilters = (qs) => {
    state.qs = qs || "";
    state.page = 1;
    syncStateFromQS(state.qs);
    syncControlsFromState();
    triggerFetch({ ackAfter: true, preserveExisting: true });
  };

  const resolveInitialQS = () => {
    if (state.qs) return state.qs;
    try {
      if (window.getGlobalFilterState) {
        const gs = window.getGlobalFilterState();
        if (gs?.qs) return gs.qs;
      }
    } catch (err) { /* noop */ }
    try {
      if (window.FilterState && typeof window.FilterState.get === "function" && typeof window.FilterState.toQueryString === "function") {
        const filters = window.FilterState.get();
        const qs = window.FilterState.toQueryString(filters);
        if (qs) return qs;
      }
    } catch (err) { /* noop */ }
    return window.location.search ? window.location.search.replace(/^\?/, "") : "";
  };

  const syncFiltersFromState = () => {
    if (state.qs) return;
    state.qs = resolveInitialQS();
  };

  const bootstrap = async (qsHint) => {
    if (hasBootstrapped) return;
    hasBootstrapped = true;
    let qs = qsHint || "";
    if (!qs) {
      const readyDetail = await waitForFiltersReady();
      qs = (readyDetail && readyDetail.qs) || "";
    }
    if (qs) {
      state.qs = qs;
      syncStateFromQS(state.qs);
    } else {
      syncFiltersFromState();
      syncStateFromQS(state.qs);
    }
    const bubbleTop = document.getElementById("bubbleTopN");
    if (bubbleTop) bubbleTop.value = String(state.bubbleTopN || 250);
    const bubbleX = document.getElementById("bubbleXMetric");
    if (bubbleX) bubbleX.value = state.bubbleXMetric || "gap_to_target";
    const bubbleColor = document.getElementById("bubbleColorBy");
    if (bubbleColor) bubbleColor.value = state.bubbleColorBy || "status_key";
    const bubbleY = document.getElementById("bubbleYMetric");
    if (bubbleY) bubbleY.value = state.bubbleYMetric || "velocity";
    const forecastToggle = document.getElementById("toggleForecast");
    if (forecastToggle) forecastToggle.checked = Boolean(state.showForecast);
    const snapshot = restoreSnapshot(state.qs, { restoreScroll: true });
    if (snapshot?.fresh) {
      setupLazySectionObserver();
      dispatchGlobalApplyAck();
      return;
    }
    triggerFetch({ preserveExisting: true });
  };

  const onApply = (evt) => {
    pendingGlobalApplyId = String(evt?.detail?.applyId || "");
    const qs = (evt?.detail && evt.detail.qs) || "";
    applyFilters(qs);
  };

  const onReady = (evt) => {
    const qs = (evt?.detail && evt.detail.qs) || "";
    bootstrap(qs);
  };

  window.addEventListener("globalFilters:apply", onApply);
  window.addEventListener("globalFilters:ready", onReady);

  wireSorting();
  wireControls();
  renderColumnChooser();
  initV2Help();
  applyWorkspaceSettings();
  if (isV4) applyEmphasisPreset(state.workspaceEmphasis);
  renderDecisionWorkbench();
  renderStagedActions();
  syncWatchlistButtons();
  syncQuickFilterButtons();
  syncTablePresetButtons();
  syncExportLinks();
  // Intel buttons use plain navigation via their hrefs.
  window.addEventListener("pagehide", () => {
    persistSnapshot();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") persistSnapshot();
  });

  // Boot when filters are already ready (or fall back after delay).
  bootstrap();
  setTimeout(() => {
    if (!hasBootstrapped) bootstrap(resolveInitialQS());
  }, 900);
})();
