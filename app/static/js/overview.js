(() => {
  const page = document.getElementById("overviewPage");
  if (!page) return;
  if (document?.body?.dataset) {
    document.body.dataset.filtersHandler = "ajax";
  }
  const authFetch = window.authFetch || fetch;
  const pageCache = window.analyticsPageCache || null;
  const PAGE_CACHE_ID = "overview";
  const PAGE_CACHE_POLICY = { freshMs: 90 * 1000, maxAgeMs: 20 * 60 * 1000 };

  const etags = new Map();
  const charts = {};
  const state = {
    payload: null,
    lastSuccessfulQs: null,
    dim: "customer",
    moversDim: "customer",
    moversSort: "delta_abs",
    driverMetric: "revenue",
    trend: { freq: "monthly", overlay: "profit", rolling: true },
    forecast: { metric: "revenue", horizon: 6, includePartial: true, data: null, lastFilters: null, loading: false, stale: false, requestSeq: 0 },
    insights: { data: null, loading: false, error: null, lastFilters: null },
    chartLibraryMissingNotified: false,
  };
  const DEFAULT_FORECAST = { metric: "revenue", horizon: 6, includePartial: true };
  const DEPRECATED_WINDOW_PARAMS = ["include_current_month", "include_current", "include_current_months"];
  let activeController = null;
  let insightsController = null;
  let requestSeq = 0;
  let lastAppliedQs = null;
  let currentApplyId = "";
  let bootstrapped = false;

  /* These four were local Intl formatters, and fmtCurrency1 - one decimal on
     money - is what printed the headline revenue as `$7,192,185.4` and clipped
     it out of its own card. They now delegate to the shared house rules in
     js/format.js, so the ~80 call sites below are corrected in one place and
     cannot drift from what the server renders.

     The `.format()` shape is kept so those call sites stay untouched. */
  const F = window.WAFormat;
  const fmtCurrency0 = { format: (v) => F.currency(v) };
  const fmtCurrency1 = { format: (v) => F.currency(v) };
  const fmtNumber0 = { format: (v) => F.count(v, { missing: "0" }) };
  const fmtNumber1 = { format: (v) => F.decimal(v, { missing: "0" }) };
  const fmtDateShort = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric" });
  const fmtDateTime = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" });
  const fmtPercent1 = (v) => F.percent(v, { missing: "0.0%" });
  const asNumber = (value) => {
    const num = Number(value);
    return Number.isFinite(num) ? num : null;
  };
  const emptyText = (value, fallback = "-") => (value === null || value === undefined || value === "" ? fallback : value);
  // How many SKUs are on the margin-risk watchlist. The bundle sends only the
  // worst N rows, so counting what arrived would under-report; the server sends
  // the population size alongside. Falling back to the array keeps hand-built
  // payloads and older cached bundles rendering the same number as before.
  const marginRiskCount = (profitability) => {
    const rows = Array.isArray(profitability?.margin_risk) ? profitability.margin_risk : [];
    const total = asNumber(profitability?.margin_risk_total_count);
    return total === null ? rows.length : total;
  };
  const escapeHtml = (value) =>
    String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  const formatTimestampish = (value, { withTime = true } = {}) => {
    if (!value) return "-";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return String(value);
    return withTime ? fmtDateTime.format(parsed) : fmtDateShort.format(parsed);
  };
  const formatRefreshAge = (days, hours) => {
    const dayNum = asNumber(days);
    const hourNum = asNumber(hours);
    if (hourNum !== null && hourNum < 48) return `${fmtNumber0.format(hourNum)}h`;
    if (dayNum !== null) return `${fmtNumber0.format(dayNum)}d`;
    return "n/a";
  };
  const isDefaultForecastSelection = () =>
    state.forecast.metric === DEFAULT_FORECAST.metric &&
    Number(state.forecast.horizon) === Number(DEFAULT_FORECAST.horizon) &&
    Boolean(state.forecast.includePartial) === Boolean(DEFAULT_FORECAST.includePartial);

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
        return await window.filtersReady;
      } catch (err) {
        console.warn("[overview] filtersReady rejected", err);
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

  const KPI_META = [
    { key: "qty", deltaKey: "qty", group: "scale", label: "Units", fmt: "number", badge: "Window total", tooltip: "Units shipped or the best available quantity proxy under the active filters." },
    { key: "weight", deltaKey: "weight", group: "scale", label: "Weight", fmt: "number", badge: "Window total", tooltip: "Shipped weight across the active filters. Use it to separate true volume shifts from order-count noise." },
    { key: "orders", deltaKey: "orders", group: "demand", label: "Orders", fmt: "number", badge: "Activity", tooltip: "Unique orders in the active filtered window." },
    { key: "customers", deltaKey: "customers", group: "demand", label: "Active Customers", fmt: "number", badge: "Demand breadth", tooltip: "Distinct customers active in the current filtered window." },
    { key: "aov", deltaKey: "aov", group: "pricing", label: "AOV", fmt: "currency", badge: "Basket", tooltip: "Average revenue per order. Rising AOV with stable orders often signals basket or mix improvement." },
    { key: "asp", deltaKey: "asp", group: "pricing", label: "ASP", fmt: "currency", badge: "Pricing", tooltip: "Average selling price per unit. Use alongside volume and mix to interpret price-led movement.", optional: true },
    { key: "profit_per_order", deltaKey: "profit_per_order", group: "pricing", label: "Profit / Order", fmt: "currency", badge: "Yield", tooltip: "Profit contribution per order. Useful when revenue is growing but order quality is deteriorating.", optional: true },
    { key: "profit_per_lb", deltaKey: "profit_per_lb", group: "pricing", label: "Profit / Lb", fmt: "currency", badge: "Yield", tooltip: "Profit yield per pound shipped. Use it to test whether scale is profitable scale.", optional: true },
  ];

  const TRUST_KPI_META = [
    { key: "cost_coverage_pct", group: "trust", label: "Cost Coverage", fmt: "percent", badge: "Coverage", tooltip: "Visible cost coverage across the current filtered window. Finance-sensitive outputs inherit this ceiling." },
    { key: "packs_coverage_pct", group: "trust", label: "Pack Coverage", fmt: "percent", badge: "Coverage", tooltip: "Pack and weight attribute coverage used by weighted metrics and operational mix diagnostics." },
    { key: "product_mapping_missing", group: "trust", label: "Missing Mapping", fmt: "number", badge: "Mapping", tooltip: "Rows still missing product mapping under the active filters." },
    { key: "refresh_age", group: "trust", label: "Refresh Age", fmt: "text", badge: "Governance", tooltip: "Age of the last governed refresh marker, not the filtered window end date." },
  ];

  const els = {
    banner: document.getElementById("overviewBanner"),
    filterSummary: document.getElementById("filterSummaryText"),
    lastRefresh: document.getElementById("lastRefreshChip"),
    dataWindow: document.getElementById("dataWindowChip"),
    comparisonBasisChip: document.getElementById("comparisonBasisChip"),
    periodModeChip: document.getElementById("periodModeChip"),
    dataCutoffChip: document.getElementById("dataCutoffChip"),
    comparisonNoteText: document.getElementById("comparisonNoteText"),
    filterCountChip: document.getElementById("filterCountChip"),
    scopeModeChip: document.getElementById("scopeModeChip"),
    businessStatusLine: document.getElementById("businessStatusLine"),
    heroTrustStatus: document.getElementById("heroTrustStatus"),
    heroRevenueCurrentChip: document.getElementById("heroRevenueCurrentChip"),
    heroRevenueDeltaLabel: document.getElementById("heroRevenueDeltaLabel"),
    heroRevenueDeltaChip: document.getElementById("heroRevenueDeltaChip"),
    heroRevenueDeltaPctLabel: document.getElementById("heroRevenueDeltaPctLabel"),
    heroRevenueDeltaPctChip: document.getElementById("heroRevenueDeltaPctChip"),
    heroPriorWindowChip: document.getElementById("heroPriorWindowChip"),
    heroCostCoverageBadge: document.getElementById("heroCostCoverageBadge"),
    costCoverageChip: document.getElementById("costCoverageChip"),
    packsCoverageChip: document.getElementById("packsCoverageChip"),
    missingMappingChip: document.getElementById("missingMappingChip"),
    freshnessChip: document.getElementById("freshnessChip"),
    healthRevenueCard: document.getElementById("healthRevenueCard"),
    healthRevenueState: document.getElementById("healthRevenueState"),
    healthRevenueDetail: document.getElementById("healthRevenueDetail"),
    healthProfitCard: document.getElementById("healthProfitCard"),
    healthProfitState: document.getElementById("healthProfitState"),
    healthProfitDetail: document.getElementById("healthProfitDetail"),
    healthMarginCard: document.getElementById("healthMarginCard"),
    healthMarginState: document.getElementById("healthMarginState"),
    healthMarginDetail: document.getElementById("healthMarginDetail"),
    healthMovementCard: document.getElementById("healthMovementCard"),
    healthMovementState: document.getElementById("healthMovementState"),
    healthMovementDetail: document.getElementById("healthMovementDetail"),
    healthRiskCard: document.getElementById("healthRiskCard"),
    healthRiskState: document.getElementById("healthRiskState"),
    healthRiskDetail: document.getElementById("healthRiskDetail"),
    healthTrustCard: document.getElementById("healthTrustCard"),
    healthTrustState: document.getElementById("healthTrustState"),
    healthTrustDetail: document.getElementById("healthTrustDetail"),
    dataHealthActions: document.getElementById("dataHealthActions"),
    downloadSnapshotBtn: document.getElementById("downloadSnapshotBtn"),
    exportDataHealthBtn: document.getElementById("exportDataHealthBtn"),
    moversExportBtn: document.getElementById("moversExportBtn"),
    driversExportBtn: document.getElementById("driversExportBtn"),
    concentrationExportBtn: document.getElementById("concentrationExportBtn"),
    marginRiskExportBtn: document.getElementById("marginRiskExportBtn"),
    marginRiskDrilldownLink: document.getElementById("marginRiskDrilldownLink"),
    driversMoversLink: document.getElementById("driversMoversLink"),
    driversSkuMixLink: document.getElementById("driversSkuMixLink"),
    execRevenueCurrent: document.getElementById("execRevenueCurrent"),
    execRevenueDeltaLabel: document.getElementById("execRevenueDeltaLabel"),
    execRevenueDelta: document.getElementById("execRevenueDelta"),
    execRevenueDeltaPctLabel: document.getElementById("execRevenueDeltaPctLabel"),
    execRevenueDeltaPct: document.getElementById("execRevenueDeltaPct"),
    execComparisonNote: document.getElementById("execComparisonNote"),
    execMainDriver: document.getElementById("execMainDriver"),
    execSecondaryDriver: document.getElementById("execSecondaryDriver"),
    commandMainDriver: document.getElementById("commandMainDriver"),
    commandSecondaryDriver: document.getElementById("commandSecondaryDriver"),
    commandWindowNote: document.getElementById("commandWindowNote"),
    commandTrustNote: document.getElementById("commandTrustNote"),
    execWatchoutsList: document.getElementById("execWatchoutsList"),
    execCostCoverageBadge: document.getElementById("execCostCoverageBadge"),
    briefWinCard: document.getElementById("briefWinCard"),
    briefWinTitle: document.getElementById("briefWinTitle"),
    briefWinValue: document.getElementById("briefWinValue"),
    briefWinDetail: document.getElementById("briefWinDetail"),
    briefWinLink: document.getElementById("briefWinLink"),
    briefDeclineCard: document.getElementById("briefDeclineCard"),
    briefDeclineTitle: document.getElementById("briefDeclineTitle"),
    briefDeclineValue: document.getElementById("briefDeclineValue"),
    briefDeclineDetail: document.getElementById("briefDeclineDetail"),
    briefDeclineLink: document.getElementById("briefDeclineLink"),
    briefRiskCard: document.getElementById("briefRiskCard"),
    briefRiskTitle: document.getElementById("briefRiskTitle"),
    briefRiskValue: document.getElementById("briefRiskValue"),
    briefRiskDetail: document.getElementById("briefRiskDetail"),
    briefActionCard: document.getElementById("briefActionCard"),
    briefActionTitle: document.getElementById("briefActionTitle"),
    briefActionValue: document.getElementById("briefActionValue"),
    briefActionDetail: document.getElementById("briefActionDetail"),
    briefActionLink: document.getElementById("briefActionLink"),
    briefImprovedList: document.getElementById("briefImprovedList"),
    briefDeclinedList: document.getElementById("briefDeclinedList"),
    execNarrativeList: document.getElementById("execNarrativeList"),
    scoreRevenue: document.getElementById("scoreRevenue"),
    scoreProfit: document.getElementById("scoreProfit"),
    scoreMargin: document.getElementById("scoreMargin"),
    scoreRevenueComparisonLabel: document.getElementById("scoreRevenueComparisonLabel"),
    scoreRevenueMom: document.getElementById("scoreRevenueMom"),
    scoreRevenueMeta: document.getElementById("scoreRevenueMeta"),
    scoreRevenueSupport: document.getElementById("scoreRevenueSupport"),
    scoreProfitMeta: document.getElementById("scoreProfitMeta"),
    scoreProfitSupport: document.getElementById("scoreProfitSupport"),
    scoreMarginMeta: document.getElementById("scoreMarginMeta"),
    scoreMarginSupport: document.getElementById("scoreMarginSupport"),
    scoreRevenueMomMeta: document.getElementById("scoreRevenueMomMeta"),
    scoreRevenueMomSupport: document.getElementById("scoreRevenueMomSupport"),
    scoreAsp: document.getElementById("scoreAsp"),
    scoreAov: document.getElementById("scoreAov"),
    scoreProfitPerOrder: document.getElementById("scoreProfitPerOrder"),
    scoreProfitPerLb: document.getElementById("scoreProfitPerLb"),
    scoreNewShare: document.getElementById("scoreNewShare"),
    scoreReturningShare: document.getElementById("scoreReturningShare"),
    scoreConcentration: document.getElementById("scoreConcentration"),
    scoreMarginRisk: document.getElementById("scoreMarginRisk"),
    scoreRetention: document.getElementById("scoreRetention"),
    scoreLogoChurn: document.getElementById("scoreLogoChurn"),
    scoreRevenueChurn: document.getElementById("scoreRevenueChurn"),
    scoreNrr: document.getElementById("scoreNrr"),
    scoreArpa: document.getElementById("scoreArpa"),
    scoreArpaCohorts: document.getElementById("scoreArpaCohorts"),
    salesBasisNote: document.getElementById("salesBasisNote"),
    reconGrossSales: document.getElementById("reconGrossSales"),
    reconDiscounts: document.getElementById("reconDiscounts"),
    reconInvoiceSales: document.getElementById("reconInvoiceSales"),
    reconReturns: document.getElementById("reconReturns"),
    reconAdjustmentCosts: document.getElementById("reconAdjustmentCosts"),
    reconNetSales: document.getElementById("reconNetSales"),
    reconSourceNote: document.getElementById("reconSourceNote"),
    kpiGrid: document.getElementById("kpiGrid"),
    kpiScaleGrid: document.getElementById("kpiScaleGrid"),
    kpiDemandGrid: document.getElementById("kpiDemandGrid"),
    kpiPricingGrid: document.getElementById("kpiPricingGrid"),
    kpiTrustGrid: document.getElementById("kpiTrustGrid"),
    trendChart: document.getElementById("trendChart"),
    trendEmpty: document.getElementById("trendEmpty"),
    trendFreqToggle: document.getElementById("trendFreqToggle"),
    trendOverlayMetric: document.getElementById("trendOverlayMetric"),
    trendRollingToggle: document.getElementById("trendRollingToggle"),
    trendSummaryText: document.getElementById("trendSummaryText"),
    trendSparseText: document.getElementById("trendSparseText"),
    trendExportBtn: document.getElementById("trendExportBtn"),
    mixChart: document.getElementById("mixChart"),
    paretoChart: document.getElementById("paretoChart"),
    healthList: document.getElementById("healthList"),
    healthBadges: document.getElementById("healthBadges"),
    healthRows: document.getElementById("healthRowsChip"),
    dimToggle: document.getElementById("dimToggle"),
    moversDimToggle: document.getElementById("moversDimToggle"),
    moversSortSelect: document.getElementById("moversSortSelect"),
    moversGainersBody: document.getElementById("moversGainersBody"),
    moversDeclinersBody: document.getElementById("moversDeclinersBody"),
    moversSummaryText: document.getElementById("moversSummaryText"),
    topMoversEmpty: document.getElementById("topMoversEmpty"),
    focusLeadCustomerTitle: document.getElementById("focusLeadCustomerTitle"),
    focusLeadCustomerValue: document.getElementById("focusLeadCustomerValue"),
    focusLeadCustomerDetail: document.getElementById("focusLeadCustomerDetail"),
    focusDecliningCustomerTitle: document.getElementById("focusDecliningCustomerTitle"),
    focusDecliningCustomerValue: document.getElementById("focusDecliningCustomerValue"),
    focusDecliningCustomerDetail: document.getElementById("focusDecliningCustomerDetail"),
    focusCustomerMotionTitle: document.getElementById("focusCustomerMotionTitle"),
    focusCustomerMotionValue: document.getElementById("focusCustomerMotionValue"),
    focusCustomerMotionDetail: document.getElementById("focusCustomerMotionDetail"),
    emptyState: document.getElementById("overviewEmpty"),
    forecastChart: document.getElementById("forecastChart"),
    forecastEmpty: document.getElementById("forecastEmpty"),
    forecastStatus: document.getElementById("forecastStatus"),
    forecastFiltersNotice: document.getElementById("forecastFiltersNotice"),
    forecastSummary: document.getElementById("forecastSummary"),
    forecastModelValue: document.getElementById("forecastModelValue"),
    forecastModelDetail: document.getElementById("forecastModelDetail"),
    forecastConfidenceValue: document.getElementById("forecastConfidenceValue"),
    forecastConfidenceDetail: document.getElementById("forecastConfidenceDetail"),
    forecastQualityValue: document.getElementById("forecastQualityValue"),
    forecastQualityDetail: document.getElementById("forecastQualityDetail"),
    forecastHistoryValue: document.getElementById("forecastHistoryValue"),
    forecastHistoryDetail: document.getElementById("forecastHistoryDetail"),
    forecastBasisText: document.getElementById("forecastBasisText"),
    forecastNotesList: document.getElementById("forecastNotesList"),
    forecastRunnerUpsList: document.getElementById("forecastRunnerUpsList"),
    forecastRunBtn: document.getElementById("runForecastBtn"),
    forecastSpinner: document.getElementById("forecastSpinner"),
    forecastHorizon: document.getElementById("forecastHorizon"),
    forecastIncludePartial: document.getElementById("forecastIncludePartial"),
    forecastMetricButtons: document.querySelectorAll("[data-forecast-metric]"),
    insightsList: document.getElementById("insightsList"),
    insightsEmpty: document.getElementById("insightsEmpty"),
    actionRailTopTitle: document.getElementById("actionRailTopTitle"),
    actionRailTopDetail: document.getElementById("actionRailTopDetail"),
    actionRailTopLink: document.getElementById("actionRailTopLink"),
    driversTitle: document.getElementById("driversTitle"),
    driversMetricToggle: document.getElementById("driversMetricToggle"),
    driversCoverage: document.getElementById("driversCoverage"),
    driversEmpty: document.getElementById("driversEmpty"),
    driversMomTitle: document.getElementById("driversMomTitle"),
    driversYoyTitle: document.getElementById("driversYoyTitle"),
    driversMomRows: document.getElementById("driversMomRows"),
    driversYoyRows: document.getElementById("driversYoyRows"),
    driversMomContext: document.getElementById("driversMomContext"),
    driversYoyContext: document.getElementById("driversYoyContext"),
    driversMomInsight: document.getElementById("driversMomInsight"),
    driversYoyInsight: document.getElementById("driversYoyInsight"),
    driversMomDeltaPct: document.getElementById("driversMomDeltaPct"),
    driversYoyDeltaPct: document.getElementById("driversYoyDeltaPct"),
    driversDetailsPanel: document.getElementById("driversDetailsPanel"),
    driversDetailsContent: document.getElementById("driversDetailsContent"),
    concentrationPanel: document.getElementById("concentrationPanel"),
    profitabilityPanel: document.getElementById("profitabilityPanel"),
    focusSkuRiskTitle: document.getElementById("focusSkuRiskTitle"),
    focusSkuRiskValue: document.getElementById("focusSkuRiskValue"),
    focusSkuRiskDetail: document.getElementById("focusSkuRiskDetail"),
    focusSkuRiskCountTitle: document.getElementById("focusSkuRiskCountTitle"),
    focusSkuRiskCountValue: document.getElementById("focusSkuRiskCountValue"),
    focusSkuRiskCountDetail: document.getElementById("focusSkuRiskCountDetail"),
    focusProfitabilityTitle: document.getElementById("focusProfitabilityTitle"),
    focusProfitabilityValue: document.getElementById("focusProfitabilityValue"),
    focusProfitabilityDetail: document.getElementById("focusProfitabilityDetail"),
    marginRiskSummary: document.getElementById("marginRiskSummary"),
    marginRiskList: document.getElementById("marginRiskList"),
    negativeMarginSupplierFilter: document.getElementById("negativeMarginSupplierFilter"),
    negativeMarginProteinFilter: document.getElementById("negativeMarginProteinFilter"),
    customerMomentum: document.getElementById("customerMomentum"),
    opsMixPanel: document.getElementById("opsMixPanel"),
    weekdayChart: document.getElementById("weekdayChart"),
    weekdayEmpty: document.getElementById("weekdayEmpty"),
    weekdayBest: document.getElementById("weekdayBest"),
  };

  const setBanner = (message, variant = "warning") => {
    if (!els.banner) return;
    if (!message) {
      els.banner.classList.add("d-none");
      els.banner.textContent = "";
      return;
    }
    els.banner.classList.remove("d-none");
    els.banner.classList.remove("alert-warning", "alert-danger", "alert-info", "alert-success");
    els.banner.classList.add(`alert-${variant}`);
    els.banner.textContent = message;
  };

  const setEmptyStateMessage = (message, variant = "info") => {
    if (!els.emptyState) return;
    els.emptyState.classList.remove("d-none", "alert-info", "alert-danger", "alert-warning", "alert-success");
    els.emptyState.classList.add(`alert-${variant}`);
    els.emptyState.innerHTML = `<i class="bi bi-info-circle me-1"></i>${message}`;
  };

  const clearEmptyStateMessage = () => {
    if (!els.emptyState) return;
    els.emptyState.classList.add("d-none");
    els.emptyState.classList.remove("alert-danger", "alert-warning", "alert-success");
    els.emptyState.classList.add("alert-info");
    els.emptyState.innerHTML = '<i class="bi bi-info-circle me-1"></i>No data for the selected window or filters.';
  };

  const applyLoadFailureState = (message, { hasSnapshot = false } = {}) => {
    const detail = String(message || "Unable to load overview data.");
    settleLoadingFallbacks("error");
    if (hasSnapshot) {
      setBanner(`Overview refresh failed for the requested filters. The last successful executive snapshot remains on screen. ${detail}`, "danger");
      if (els.businessStatusLine) {
        els.businessStatusLine.textContent = "Requested filter refresh failed. Displaying the last successful business snapshot until a new bundle loads.";
      }
      if (els.heroTrustStatus) els.heroTrustStatus.textContent = "Stale";
      if (els.comparisonNoteText) {
        els.comparisonNoteText.textContent = `Latest refresh failed. The page still shows the last successful snapshot. ${detail}`;
      }
      clearEmptyStateMessage();
      return;
    }
    setBanner(`Overview data failed to load. ${detail}`, "danger");
    if (els.businessStatusLine) {
      els.businessStatusLine.textContent = "Business status is unavailable because the executive overview bundle did not load.";
    }
    if (els.heroTrustStatus) els.heroTrustStatus.textContent = "Blocked";
    if (els.comparisonNoteText) {
      els.comparisonNoteText.textContent = `The executive command center could not load for the active filters. ${detail}`;
    }
    setEmptyStateMessage("Overview data is temporarily unavailable for the requested filters. Retry the refresh or narrow the active window.", "danger");
  };

  const canRenderCharts = () => typeof window.Chart !== "undefined";

  const ensureChartWarning = () => {
    if (canRenderCharts() || state.chartLibraryMissingNotified) return;
    state.chartLibraryMissingNotified = true;
    setBanner("Chart rendering is unavailable right now. KPI and table diagnostics are still live; charts will be replaced with fallbacks.", "warning");
  };

  const showChartFallback = (canvasEl, message) => {
    if (!canvasEl) return;
    canvasEl.classList.add("d-none");
    const parent = canvasEl.parentElement;
    if (!parent) return;
    let fallback = parent.querySelector(`.chart-unavailable[data-for="${canvasEl.id}"]`);
    if (!fallback) {
      fallback = document.createElement("div");
      fallback.className = "chart-unavailable text-muted text-center py-4 small";
      fallback.dataset.for = canvasEl.id;
      parent.appendChild(fallback);
    }
    fallback.textContent = message;
    fallback.classList.remove("d-none");
  };

  const clearChartFallback = (canvasEl) => {
    if (!canvasEl) return;
    canvasEl.classList.remove("d-none");
    const parent = canvasEl.parentElement;
    if (!parent) return;
    const fallback = parent.querySelector(`.chart-unavailable[data-for="${canvasEl.id}"]`);
    if (fallback) fallback.classList.add("d-none");
  };

  const setPendingFallback = (el, fallback) => {
    if (!el) return;
    const current = (el.textContent || "").trim().toLowerCase();
    if (!current || current.includes("loading") || current.includes("preparing")) {
      el.textContent = fallback;
    }
  };

  const setListFallback = (el, fallback) => {
    if (!el) return;
    const current = (el.textContent || "").trim().toLowerCase();
    if (!current || current.includes("loading") || current.includes("preparing")) {
      el.innerHTML = `<li class="text-muted">${fallback}</li>`;
    }
  };

  const settleLoadingFallbacks = (mode = "partial") => {
    setPendingFallback(els.businessStatusLine, mode === "error" ? "Business status is temporarily unavailable. Try refreshing or adjusting filters." : "Business status resolved for the active filter window.");
    setPendingFallback(els.filterSummary, "Default (Current FY)");
    setPendingFallback(els.dataWindow, "Not available");
    setPendingFallback(els.comparisonBasisChip, "Prior comparable window");
    setPendingFallback(els.periodModeChip, "Filtered window");
    setPendingFallback(els.dataCutoffChip, "Not available");
    setPendingFallback(els.comparisonNoteText, "Comparisons follow the active filtered window.");
    setPendingFallback(els.filterCountChip, "0 active");
    setPendingFallback(els.scopeModeChip, "Enterprise");
    setPendingFallback(els.heroTrustStatus, mode === "error" ? "Unavailable" : "Watch");
    setPendingFallback(els.healthRevenueState, "Unavailable");
    setPendingFallback(els.healthProfitState, "Unavailable");
    setPendingFallback(els.healthMarginState, "Unavailable");
    setPendingFallback(els.healthMovementState, "Unavailable");
    setPendingFallback(els.healthRiskState, "Unavailable");
    setPendingFallback(els.healthTrustState, "Unavailable");
    setPendingFallback(els.briefWinTitle, "Insight unavailable");
    setPendingFallback(els.briefDeclineTitle, "Insight unavailable");
    setPendingFallback(els.briefRiskTitle, "Insight unavailable");
    setPendingFallback(els.briefActionTitle, "No action available");
    setPendingFallback(els.actionRailTopTitle, "No recommended next action");
    setPendingFallback(els.actionRailTopDetail, "No additional follow-up is available for the active window.");
    setPendingFallback(els.driversMomContext, "Driver context unavailable.");
    setPendingFallback(els.driversYoyContext, "Driver context unavailable.");
    setPendingFallback(els.trendSparseText, "Trend diagnostics unavailable.");
    setPendingFallback(els.commandWindowNote, "Comparisons follow the active filtered window.");
    setPendingFallback(els.commandTrustNote, "Coverage and governance caveats will appear here.");
    setPendingFallback(els.execComparisonNote, "Comparisons follow the active filtered window.");
    setListFallback(els.execNarrativeList, "Leadership narrative unavailable for current filters.");
    setListFallback(els.briefImprovedList, "No material improvements identified.");
    setListFallback(els.briefDeclinedList, "No material declines identified.");
    setListFallback(els.execWatchoutsList, "No active watchouts available.");
  };

  const applyEtags = (url, headers) => {
    if (pageCache) {
      const prepared = pageCache.prepareHeaders(url, headers);
      Object.keys(headers).forEach((key) => delete headers[key]);
      Object.assign(headers, prepared);
    }
    const et = etags.get(url);
    if (et) headers["If-None-Match"] = et;
  };

  const fetchJson = async (url, signal) => {
    const headers = {};
    applyEtags(url, headers);
    const resp = await authFetch(url, { headers, signal });
    if (resp.status === 304) return { notModified: true };
    if (!resp.ok) {
      const detail = await (async () => {
        try {
          const payload = await resp.clone().json();
          return payload?.error || payload?.detail || JSON.stringify(payload);
        } catch (e) {
          try {
            return await resp.text();
          } catch (_) {
            return "";
          }
        }
      })();
      throw new Error(`Request failed (${resp.status})${detail ? `: ${detail}` : ""}`);
    }
    const et = resp.headers.get("ETag");
    if (et) etags.set(url, et);
    if (pageCache) pageCache.rememberResponse(url, resp);
    return { data: await resp.json() };
  };

  const formatValue = (key, value, fmtOverride = null) => {
    if (value === null || value === undefined || Number.isNaN(value)) return "-";
    const fmt = fmtOverride || (key === "margin_pct" ? "percent" : null);
    if (fmt === "percent") return fmtPercent1(value);
    if (fmt === "currency") return fmtCurrency0.format(Number(value) || 0);
    if (fmt === "number") return fmtNumber0.format(Number(value) || 0);
    if (["revenue", "cost", "profit", "asp", "aov", "profit_per_order", "profit_per_lb"].includes(key)) return fmtCurrency0.format(Number(value) || 0);
    return fmtNumber0.format(Number(value) || 0);
  };

  const MONEY_KEYS = ["revenue", "cost", "profit", "asp", "aov", "profit_per_order", "profit_per_lb"];

  const compactNumber = (num, key, fmtOverride = null) => {
    const n = Number(num);
    if (!Number.isFinite(n)) return null;
    const abs = Math.abs(n);
    if (abs < 10_000) return null;
    if (fmtOverride === "currency" || MONEY_KEYS.includes(key)) return F.compactCurrency(n);
    const short = (div, suffix) => `${F.decimal(n / div)}${suffix}`;
    if (abs >= 1_000_000_000) return short(1_000_000_000, "B");
    if (abs >= 1_000_000) return short(1_000_000, "M");
    return short(1_000, "K");
  };

  const formatDisplay = (key, value, fmtOverride = null) => {
    const full = formatValue(key, value, fmtOverride);
    if (typeof full !== "string") return { text: full, title: "" };
    if (full.length > 12) {
      const compact = compactNumber(value, key, fmtOverride);
      if (compact) return { text: compact, title: full };
    }
    return { text: full, title: "" };
  };

  const formatByFmt = (fmt, value) => {
    if (fmt === "percent") return F.percent(value);
    if (fmt === "currency") return F.currency(value);
    if (fmt === "number") return F.count(value);
    return F.decimal(value);
  };

  /* Signed deltas. `formatSigned` used to build a negative as
     "" + fmtCurrency1.format(-79.48), which Intl renders as `-$79.48` - but
     the same pattern elsewhere concatenated "$" itself and produced `$-79.48`.
     One helper, one answer. */
  const formatSigned = (fmt, value) => {
    if (fmt === "currency") return F.currencySigned(value, { missing: "n/a" });
    if (fmt === "percent") return F.percentSigned(value, { missing: "n/a" });
    const num = F.coerce(value);
    if (num === null) return "n/a";
    return `${num > 0 ? "+" : num < 0 ? "-" : ""}${F.decimal(Math.abs(num))}`;
  };

  const formatSignedPoints = (value) => F.points(value, { missing: "n/a" });

  const normalizeMarginStatusKey = (value) => String(value || "").trim().toLowerCase();
  const clampNumber = (value, lower, upper) => Math.min(Math.max(Number(value), lower), upper);
  const marginStatusBuffers = (minimum, target) => {
    const minimumNum = asNumber(minimum) ?? 0;
    const targetNum = asNumber(target) ?? minimumNum;
    const span = Math.max(targetNum - minimumNum, 0);
    return {
      nearTarget: clampNumber(span * 0.2, 1, 3),
      materiallyBelowMin: clampNumber(span * 0.35, 2, 5),
    };
  };
  const marginStatusClass = (value) => {
    const key = normalizeMarginStatusKey(value);
    if (key === "red") return "is-red";
    if (key === "orange") return "is-orange";
    if (key === "yellow") return "is-yellow";
    if (key === "light_green") return "is-light-green";
    if (key === "green") return "is-green";
    return "is-neutral";
  };
  const deriveMarginStatusKey = (actual, minimum, target, explicit = null) => {
    const explicitKey = normalizeMarginStatusKey(explicit);
    if (explicitKey) return explicitKey;
    const actualNum = asNumber(actual);
    const minimumNum = asNumber(minimum);
    const targetNum = asNumber(target);
    if (actualNum === null) return "no_cost";
    if (minimumNum === null || targetNum === null) return "needs_mapping";
    const { nearTarget, materiallyBelowMin } = marginStatusBuffers(minimumNum, targetNum);
    if (actualNum < (minimumNum - materiallyBelowMin)) return "red";
    if (actualNum < minimumNum) return "orange";
    if (actualNum < (targetNum - nearTarget)) return "yellow";
    if (actualNum <= (targetNum + nearTarget)) return "light_green";
    return "green";
  };
  const marginStatusLabel = (key) => {
    const normalized = normalizeMarginStatusKey(key);
    if (normalized === "red") return "Materially below minimum";
    if (normalized === "orange") return "Near minimum";
    if (normalized === "yellow") return "Between minimum and target";
    if (normalized === "light_green") return "Near target";
    if (normalized === "green") return "Above target";
    if (normalized === "no_cost") return "Cost unavailable";
    return "Needs review";
  };
  const marginTargetSummary = ({ margin_pct, minimum_margin_pct, target_margin_pct, status_key } = {}) => {
    const parts = [];
    if (target_margin_pct !== null && target_margin_pct !== undefined) parts.push(`Target ${formatByFmt("percent", target_margin_pct)}`);
    if (minimum_margin_pct !== null && minimum_margin_pct !== undefined) parts.push(`Min ${formatByFmt("percent", minimum_margin_pct)}`);
    if (margin_pct !== null && margin_pct !== undefined && target_margin_pct !== null && target_margin_pct !== undefined) {
      parts.push(`${formatSignedPoints(Number(margin_pct) - Number(target_margin_pct))} vs target`);
    } else {
      parts.push(marginStatusLabel(status_key));
    }
    return parts.filter(Boolean).join(" · ");
  };
  const marginStatusBadgeHtml = (key, label = null) =>
    `<span class="overview-status-pill ${marginStatusClass(key)}">${escapeHtml(label || marginStatusLabel(key))}</span>`;

  const getWindowMeta = (payload = null) => {
    if (payload?.meta?.window) return payload.meta.window;
    if (state?.payload?.meta?.window) return state.payload.meta.window;
    return {};
  };

  const primaryDeltaLabel = (windowMeta = getWindowMeta()) => String(windowMeta.delta_short_label || "Prior window");
  const primaryCompareLabel = (windowMeta = getWindowMeta()) => String(windowMeta.prior_label || "Prior comparable window");
  const primaryComparisonNote = (windowMeta = getWindowMeta()) => String(windowMeta.note || "Comparisons follow the active filtered window.");
  const currentWindowLabel = (windowMeta = getWindowMeta()) => String(windowMeta.current_window_label || "");
  const priorWindowLabel = (windowMeta = getWindowMeta()) => String(windowMeta.prior_window_label || "");
  const shortPrimaryBadge = (windowMeta = getWindowMeta()) => {
    const label = primaryDeltaLabel(windowMeta);
    if (label === "Prior window") return "Prior";
    if (label === "Current window") return "Compare";
    return label;
  };
  const revenueDeltaLabel = (windowMeta = getWindowMeta()) => {
    const label = primaryDeltaLabel(windowMeta);
    if (label === "Prior window") return "Δ$ vs prior";
    return `${label} Δ$`;
  };
  const revenueDeltaPctLabel = (windowMeta = getWindowMeta()) => {
    const label = primaryDeltaLabel(windowMeta);
    if (label === "Prior window") return "Δ% vs prior";
    return `${label} Δ%`;
  };
  const primaryCardLabel = (windowMeta = getWindowMeta()) => {
    const label = primaryDeltaLabel(windowMeta);
    if (label === "MoM" || label === "MTD" || label === "FYTD" || label === "FQTD" || label === "FY") return `Revenue ${label}`;
    return "Revenue change";
  };
  const periodModeLabel = (windowMeta = getWindowMeta()) => {
    const method = String(windowMeta.method_label || windowMeta.method || "").toLowerCase();
    if (method.includes("fiscal year-to-date")) return "Fiscal year-to-date";
    if (method.includes("fiscal quarter-to-date")) return "Fiscal quarter-to-date";
    if (method.includes("fiscal year")) return "Fiscal year";
    if (method.includes("month-to-date") || method.includes("same_day")) return "Month-to-date";
    if (method.includes("completed")) return "Completed months";
    if (method.includes("matched")) return "Matched days";
    return windowMeta.period_status_label || "Filtered window";
  };

  const drillQueryString = () => {
    let filterState = null;
    try {
      filterState = typeof window.getGlobalFilterState === "function" ? window.getGlobalFilterState() : null;
    } catch (err) {
      filterState = null;
    }
    const rawQs = (filterState && filterState.qs) || window.location.search || "";
    const normalized = sanitizeOverviewQs(String(rawQs || "").replace(/^\?/, ""));
    const qs = normalized ? `?${normalized}` : "";
    if (!qs) return "";
    return qs.startsWith("?") ? qs : `?${qs}`;
  };

  const buildDrillLink = (kind, id) => {
    if (!kind || !id) return null;
    const qs = drillQueryString();
    let base = null;
    if (kind === "customer") base = `/customers/drilldown/${encodeURIComponent(id)}`;
    if (kind === "product") base = `/products/${encodeURIComponent(id)}/drilldown`;
    if (kind === "region") base = `/regions/drilldown/${encodeURIComponent(id)}`;
    if (kind === "supplier") base = `/suppliers/drilldown/${encodeURIComponent(id)}`;
    if (!base) return null;
    return `${base}${qs}`;
  };

  const deltaBadge = (val, label) => {
    if (val === null || val === undefined) return `<span class="text-muted small">${label}: n/a</span>`;
    const dir = Number(val) === 0 ? "neutral" : Number(val) > 0 ? "positive" : "negative";
    const icon = dir === "positive" ? "^" : dir === "negative" ? "v" : "-";
    const cls = dir === "positive" ? "text-success" : dir === "negative" ? "text-danger" : "text-muted";
    return `<span class="${cls} fw-semibold">${icon} ${fmtNumber1.format(Math.abs(val))}% ${label}</span>`;
  };

  const deltaBadgePoints = (val, label) => {
    if (val === null || val === undefined) return `<span class="text-muted small">${label}: n/a</span>`;
    const dir = Number(val) === 0 ? "neutral" : Number(val) > 0 ? "positive" : "negative";
    const icon = dir === "positive" ? "^" : dir === "negative" ? "v" : "-";
    const cls = dir === "positive" ? "text-success" : dir === "negative" ? "text-danger" : "text-muted";
    return `<span class="${cls} fw-semibold">${icon} ${fmtNumber1.format(Math.abs(Number(val) || 0))} pts ${label}</span>`;
  };

  const buildApiUrl = (qsOverride) => {
    const base = page.dataset.api || "/overview/api/bundle";
    const qs =
      qsOverride !== undefined
        ? sanitizeOverviewQs(qsOverride)
        : sanitizeOverviewQs(new URLSearchParams(window.location.search || "").toString());
    return qs ? `${base}?${qs}` : base;
  };

  const buildForecastUrl = () => {
    const base = page.dataset.forecastApi || "/api/overview/forecast";
    const params = new URLSearchParams(window.location.search || "");
    params.set("metric", state.forecast.metric);
    params.set("horizon_months", state.forecast.horizon);
    params.set("granularity", "monthly");
    params.set("include_current_month", state.forecast.includePartial ? "1" : "0");
    params.set("v2", "1");
    const qs = params.toString();
    return qs ? `${base}?${qs}` : base;
  };

  const buildInsightsUrl = (qsOverride) => {
    const base = page.dataset.insightsApi || "/api/overview/insights";
    const qs =
      qsOverride !== undefined
        ? sanitizeOverviewQs(qsOverride)
        : sanitizeOverviewQs(new URLSearchParams(window.location.search || "").toString());
    return qs ? `${base}?${qs}` : base;
  };

  const buildSnapshotExportUrl = (dataset = "all", format = "xlsx") => {
    const base = page.dataset.snapshotExportUrl || "/overview/api/export/snapshot";
    const params = new URLSearchParams(window.location.search || "");
    params.set("dataset", dataset);
    params.set("format", format);
    const qs = sanitizeOverviewQs(params.toString());
    return qs ? `${base}?${qs}` : base;
  };

  const buildTrendExportUrl = (format = "xlsx") => {
    const base = page.dataset.trendExportUrl || "/overview/api/export/trend";
    const params = new URLSearchParams(window.location.search || "");
    params.set("format", format);
    params.set("freq", state.trend.freq || "monthly");
    params.set("metric", state.trend.overlay || "profit");
    const qs = sanitizeOverviewQs(params.toString());
    return qs ? `${base}?${qs}` : base;
  };

  const buildDrilldownUrl = (drilldown, options = {}) => {
    const baseRoot = (page.dataset.drilldownBase || "/overview/api/drilldown").replace(/\/+$/, "");
    const base = `${baseRoot}/${encodeURIComponent(drilldown)}`;
    const params = new URLSearchParams(window.location.search || "");
    if (options.dimension) params.set("dimension", options.dimension);
    if (options.format) params.set("format", options.format);
    const qs = sanitizeOverviewQs(params.toString());
    return qs ? `${base}?${qs}` : base;
  };

  const withScopeQuery = (href) => {
    if (!href || href.startsWith("#") || /^https?:\/\//i.test(href)) return href;
    const qs = drillQueryString();
    if (!qs) return href;
    return href.includes("?") ? `${href}&${qs.slice(1)}` : `${href}${qs}`;
  };

  const setDrilldownPayload = (el, payload) => {
    if (!el) return;
    if (!payload) {
      el.removeAttribute("data-drilldown-payload");
      return;
    }
    try {
      el.setAttribute("data-drilldown-payload", JSON.stringify(payload));
      if (window.universalDrilldown && typeof window.universalDrilldown.enhanceAll === "function") {
        window.universalDrilldown.enhanceAll();
      }
    } catch (_err) {
      el.removeAttribute("data-drilldown-payload");
    }
  };

  const overviewWorkspacePayload = (metric, value, extra = {}) => ({
    source_page: "overview",
    source_section: extra.source_section || "Executive Scorecard",
    source_widget: extra.source_widget || metric,
    requested_target: extra.requested_target || "workspace",
    clicked_metric: metric,
    clicked_metric_value: value,
    clicked_entity_type: extra.clicked_entity_type || null,
    clicked_entity_id: extra.clicked_entity_id || null,
    clicked_entity_label: extra.clicked_entity_label || null,
    extra: extra.extra || { workspace_kind: "fact_orders", filter_mode: "current_window" },
  });

  const setMoversDimension = (dim) => {
    if (!dim) return;
    state.moversDim = dim;
    if (els.moversDimToggle) {
      els.moversDimToggle
        .querySelectorAll("button[data-movers-dim]")
        .forEach((btn) => btn.classList.toggle("active", btn.getAttribute("data-movers-dim") === dim));
    }
    renderTopMovers(state.payload?.top_movers || {}, state.moversDim, state.moversSort);
  };

  const scrollToSection = (selector) => {
    if (!selector) return;
    const target = selector.startsWith("#") ? document.querySelector(selector) : document.getElementById(selector);
    if (!target) return;
    target.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const openMarginRiskWatchlist = () => {
    const collapseEl = document.getElementById("negativeMarginCollapse");
    if (!collapseEl) return;
    if (typeof bootstrap !== "undefined" && bootstrap.Collapse) {
      bootstrap.Collapse.getOrCreateInstance(collapseEl, { toggle: false }).show();
      return;
    }
    collapseEl.classList.add("show");
  };

  const navigateToOverviewTarget = (target) => {
    const normalized = String(target || "").trim().toLowerCase();
    if (!normalized) return false;
    if (normalized === "data_health") {
      scrollToSection("#dataHealthSection");
      return true;
    }
    if (normalized === "margin_risk") {
      openMarginRiskWatchlist();
      scrollToSection("#riskWatchlistCard");
      return true;
    }
    if (normalized === "concentration") {
      scrollToSection("#riskWatchlistCard");
      return true;
    }
    if (normalized === "movers_customer") {
      setMoversDimension("customer");
      scrollToSection("#moversPanel");
      return true;
    }
    if (normalized === "movers_product") {
      setMoversDimension("product");
      scrollToSection("#moversPanel");
      return true;
    }
    if (normalized === "movers_region") {
      setMoversDimension("region");
      scrollToSection("#moversPanel");
      return true;
    }
    return false;
  };

  const syncScopedLinks = () => {
    const scopedLinks = page.querySelectorAll("[data-preserve-filters-link]");
    const qs = drillQueryString();
    scopedLinks.forEach((link) => {
      const originalHref = link.getAttribute("data-base-href") || link.getAttribute("href") || "";
      if (!originalHref || originalHref.startsWith("#")) return;
      if (!link.hasAttribute("data-base-href")) {
        link.setAttribute("data-base-href", originalHref);
      }
      link.setAttribute("href", qs ? withScopeQuery(originalHref) : originalHref);
    });
  };

  const updateLink = (el, href, label = "Open detail", actionTarget = null) => {
    if (!el) return;
    if (!href) {
      el.classList.add("d-none");
      el.removeAttribute("href");
      delete el.dataset.overviewTarget;
      return;
    }
    el.href = href;
    el.classList.remove("d-none");
    el.setAttribute("aria-label", label);
    if (actionTarget) el.dataset.overviewTarget = actionTarget;
    else delete el.dataset.overviewTarget;
  };

  const briefingLinkFor = (item = {}) => {
    const link = item.link || {};
    if (link.kind && link.id) {
      return buildDrillLink(link.kind, link.id);
    }
    const target = item.target || null;
    if (target === "data_health") return "#dataHealthSection";
    if (target === "margin_risk" || target === "concentration") return "#riskWatchlistCard";
    if (target === "movers_customer" || target === "movers_product" || target === "movers_region") return "#moversPanel";
    return null;
  };

  const ensureKpiCards = () => {
    if (!els.kpiGrid || els.kpiGrid.querySelector("[data-metric-card]")) return;
    const groupContainerFor = (group) => {
      if (group === "scale" && els.kpiScaleGrid) return els.kpiScaleGrid;
      if (group === "demand" && els.kpiDemandGrid) return els.kpiDemandGrid;
      if (group === "pricing" && els.kpiPricingGrid) return els.kpiPricingGrid;
      if (group === "trust" && els.kpiTrustGrid) return els.kpiTrustGrid;
      return els.kpiGrid;
    };
    const buildCard = (meta, kind = "metric") => {
      const card = document.createElement("article");
      card.className = "kpi-card shadow-soft";
      card.setAttribute("data-metric-card", meta.key);
      card.setAttribute("data-kpi-kind", kind);
      card.innerHTML = `
        <div class="kpi-head d-flex justify-content-between align-items-center gap-2">
          <div class="kpi-meta">
            <div class="kpi-label fw-semibold">${meta.label}</div>
            <span class="kpi-status" data-kpi-status="${meta.key}">${meta.badge || "Window"}</span>
          </div>
          <i class="bi bi-info-circle text-muted" title="${meta.tooltip || ""}" data-bs-toggle="tooltip"></i>
        </div>
        <div class="kpi-value-row kpi-main">
          <div class="kpi-value-wrap">
            <div class="kpi-value display-6 mb-1" data-kpi-value="${meta.key}">-</div>
          </div>
          <div class="kpi-deltas">
            <span class="kpi-delta-pill" data-kpi-delta="${meta.key}">${kind === "trust" ? "Scoped trust signal" : "Compare: n/a"}</span>
          </div>
        </div>
        <div class="kpi-sub" data-kpi-sub="${meta.key}">${kind === "trust" ? "Filter-aware and RBAC-aware." : "YoY: n/a"}</div>
      `;
      groupContainerFor(meta.group).appendChild(card);
    };
    KPI_META.forEach((meta) => buildCard(meta, "metric"));
    TRUST_KPI_META.forEach((meta) => buildCard(meta, "trust"));
    if (typeof bootstrap !== "undefined" && bootstrap.Tooltip) {
      els.kpiGrid.querySelectorAll('[data-bs-toggle="tooltip"]').forEach((el) => new bootstrap.Tooltip(el));
    }
  };

  const renderKpis = (payload) => {
    ensureKpiCards();
    const kpis = payload.kpis || {};
    const deltas = payload.deltas || {};
    const health = payload.health || {};
    const metaBlock = payload.meta || {};
    const windowMeta = getWindowMeta(payload);
    const primaryBadgeLabel = shortPrimaryBadge(windowMeta);
    const costCoverage = asNumber(health.cost_coverage_pct);
    const packsCoverage = asNumber(health.packs_coverage_pct ?? (health.pack_missing_pct != null ? (100 - health.pack_missing_pct) : null));
    const setCardValue = (key, text, title = "", isEmpty = false) => {
      const valEl = els.kpiGrid.querySelector(`[data-kpi-value="${key}"]`);
      if (!valEl) return;
      valEl.textContent = text;
      valEl.title = title || "";
      valEl.classList.toggle("is-empty", !!isEmpty);
    };
    const setCardMeta = (key, status, pillText, subText) => {
      const statusEl = els.kpiGrid.querySelector(`[data-kpi-status="${key}"]`);
      const pillEl = els.kpiGrid.querySelector(`[data-kpi-delta="${key}"]`);
      const subEl = els.kpiGrid.querySelector(`[data-kpi-sub="${key}"]`);
      if (statusEl) statusEl.textContent = status;
      if (pillEl) pillEl.innerHTML = pillText;
      if (subEl) subEl.textContent = subText;
    };
    KPI_META.forEach((meta) => {
      const card = els.kpiGrid.querySelector(`[data-metric-card="${meta.key}"]`);
      const rawVal = kpis[meta.key];
      const missing = rawVal === null || rawVal === undefined || Number.isNaN(Number(rawVal));
      if (card) {
        card.classList.toggle("is-hidden", Boolean(meta.optional && missing));
        card.setAttribute("aria-hidden", meta.optional && missing ? "true" : "false");
      }
      let status = meta.badge || "Window";
      if (meta.key === "qty" && packsCoverage !== null && packsCoverage < 98) status = "Coverage";
      if (["profit_per_order", "profit_per_lb"].includes(meta.key) && (costCoverage !== null && costCoverage < 90)) status = "Partial";
      if (missing && ["profit_per_order", "profit_per_lb", "asp"].includes(meta.key)) status = "Restricted";
      if (card) card.dataset.coverageState = status.toLowerCase();
      if (card) {
        setDrilldownPayload(card, overviewWorkspacePayload(meta.label, rawVal, {
          source_widget: meta.label,
          extra: { workspace_kind: "fact_orders", filter_mode: "current_window" },
        }));
      }
      if (meta.optional && missing) {
        setCardValue(meta.key, "", "", true);
      } else {
        const { text, title } = formatDisplay(meta.key, rawVal, meta.fmt);
        setCardValue(meta.key, missing ? "N/A" : text, title, missing);
      }
      const delta = deltas[meta.deltaKey || meta.key] || {};
      const pillText = meta.key === "margin_pct"
        ? deltaBadgePoints(delta.mom, primaryBadgeLabel)
        : deltaBadge(delta.mom_pct, primaryBadgeLabel);
      const subText = meta.key === "margin_pct"
        ? `YoY: ${formatSignedPoints(delta.yoy)}`
        : `YoY: ${delta.yoy_pct === null || delta.yoy_pct === undefined ? "n/a" : formatByFmt("percent", delta.yoy_pct)}`;
      setCardMeta(meta.key, status, pillText, subText);
    });

    TRUST_KPI_META.forEach((meta) => {
      const card = els.kpiGrid.querySelector(`[data-metric-card="${meta.key}"]`);
      if (!card) return;
      let rawVal = null;
      let status = meta.badge || "Trust";
      let pillText = "Scoped trust signal";
      let subText = "Filter-aware and RBAC-aware.";

      if (meta.key === "cost_coverage_pct") {
        rawVal = costCoverage;
        if (costCoverage !== null && costCoverage < 80) status = "At risk";
        else if (costCoverage !== null && costCoverage < 90) status = "Watch";
        pillText = costCoverage !== null && costCoverage < 90 ? "Finance view is partially constrained" : "Finance-sensitive KPIs are decision-grade";
        subText = `Current filtered window${metaBlock.data_cutoff ? ` · data cutoff ${formatTimestampish(metaBlock.data_cutoff, { withTime: false })}` : ""}`;
      } else if (meta.key === "packs_coverage_pct") {
        rawVal = packsCoverage;
        if (packsCoverage !== null && packsCoverage < 90) status = "At risk";
        else if (packsCoverage !== null && packsCoverage < 98) status = "Watch";
        pillText = packsCoverage !== null && packsCoverage < 98 ? "Weighted metrics may drift" : "Weighted metrics have strong coverage";
        subText = "Pack and weight diagnostics use the same scoped rows shown on the page.";
      } else if (meta.key === "product_mapping_missing") {
        rawVal = Number(health.product_mapping_missing || 0);
        if (rawVal > 0) status = rawVal >= 50 ? "At risk" : "Watch";
        pillText = rawVal > 0 ? "Unmapped rows weaken movers and mix" : "No material mapping gap detected";
        subText = "Mapping counts respect the current filters and RBAC scope.";
      } else if (meta.key === "refresh_age") {
        rawVal = formatRefreshAge(metaBlock.refresh_age_days, metaBlock.refresh_age_hours);
        const refreshText = formatTimestampish(metaBlock.last_refresh);
        const cutoffText = metaBlock.data_cutoff ? formatTimestampish(metaBlock.data_cutoff, { withTime: false }) : "n/a";
        status = "Governed";
        pillText = `Last refresh ${refreshText}`;
        subText = `Data cutoff ${cutoffText}`;
      }

      const missing = rawVal === null || rawVal === undefined || rawVal === "";
      if (card) {
        card.classList.toggle("is-hidden", false);
        card.dataset.coverageState = status.toLowerCase().replace(/\s+/g, "-");
        setDrilldownPayload(card, overviewWorkspacePayload(meta.label, rawVal, {
          source_section: "Trust and Governance",
          source_widget: meta.label,
          extra: { workspace_kind: "overview_prebuilt", drilldown: "data_health" },
        }));
      }
      if (meta.fmt === "text") {
        setCardValue(meta.key, missing ? "N/A" : String(rawVal), "", missing);
      } else {
        const { text, title } = formatDisplay(meta.key, rawVal, meta.fmt);
        setCardValue(meta.key, missing ? "N/A" : text, title, missing);
      }
      setCardMeta(meta.key, status, pillText, subText);
    });
  };

  const renderList = (el, rows, emptyMessage) => {
    if (!el) return;
    const items = Array.isArray(rows) ? rows.filter(Boolean) : [];
    el.innerHTML = items.length
      ? items.map((row) => `<li>${row}</li>`).join("")
      : `<li class="text-muted">${emptyMessage}</li>`;
  };

  const renderBriefingCard = (cardEl, titleEl, valueEl, detailEl, linkEl, item, valueFallback = "-") => {
    if (!titleEl || !valueEl || !detailEl) return;
    const severity = String(item?.severity || "info").toLowerCase();
    if (cardEl) {
      ["positive", "negative", "warning", "info", "neutral"].forEach((cls) => cardEl.classList.remove(cls));
      cardEl.classList.add(["positive", "negative", "warning", "info"].includes(severity) ? severity : "info");
    }
    titleEl.textContent = item?.title || "Not available";
    if (item?.value_fmt === "text") {
      valueEl.textContent = emptyText(item?.value, valueFallback);
    } else if (item?.value === null || item?.value === undefined) {
      valueEl.textContent = valueFallback;
    } else {
      valueEl.textContent = formatByFmt(item?.value_fmt || "number", item.value);
    }
    detailEl.textContent = item?.detail || "No additional detail available for the selected window.";
    updateLink(linkEl, briefingLinkFor(item), `Open ${item?.title || "detail"}`, item?.target || null);
  };

  const setHealthCard = (cardEl, valueEl, detailEl, tone, value, detail) => {
    if (cardEl) {
      cardEl.dataset.tone = tone || "neutral";
    }
    if (valueEl) valueEl.textContent = value || "-";
    if (detailEl) detailEl.textContent = detail || "No summary available.";
  };

  const setFocusCard = (titleEl, valueEl, detailEl, title, value, detail) => {
    if (titleEl) titleEl.textContent = title || "Not available";
    if (valueEl) valueEl.textContent = value || "-";
    if (detailEl) detailEl.textContent = detail || "No scoped detail available for the active window.";
  };

  const classifyHealthSignal = (value, bands = {}, labels = {}) => {
    if (value === null || value === undefined || Number.isNaN(Number(value))) {
      return { tone: "neutral", value: labels.empty || "Unavailable" };
    }
    const num = Number(value);
    if (bands.negative !== undefined && num <= bands.negative) {
      return { tone: "negative", value: labels.negative || "At risk" };
    }
    if (bands.warning !== undefined && num <= bands.warning) {
      return { tone: "warning", value: labels.warning || "Watch" };
    }
    if (bands.positive !== undefined && num >= bands.positive) {
      return { tone: "positive", value: labels.positive || "Healthy" };
    }
    return { tone: "neutral", value: labels.neutral || "Stable" };
  };

  const renderHealthRail = (payload = {}) => {
    const scorecard = payload.executive_scorecard || {};
    const headline = scorecard.headline || {};
    const windowMeta = getWindowMeta(payload);
    const primaryCompare = primaryCompareLabel(windowMeta);
    const primaryShort = shortPrimaryBadge(windowMeta);
    const growth = scorecard.growth_retention || {};
    const risk = scorecard.risk_indicators || {};
    const health = payload.health || {};
    const profitability = payload.profitability || {};
    const movers = payload.top_movers || {};
    const customerMovers = movers.customer || {};
    const customerGainers = Array.isArray(customerMovers.gainers) ? customerMovers.gainers.length : 0;
    const customerDecliners = Array.isArray(customerMovers.decliners) ? customerMovers.decliners.length : 0;
    const costCoverage = asNumber(risk.cost_coverage_pct ?? health.cost_coverage_pct);
    const packsCoverage = asNumber(risk.packs_coverage_pct ?? health.packs_coverage_pct ?? (health.pack_missing_pct != null ? 100 - health.pack_missing_pct : null));
    const mappingMissing = Number((risk.product_mapping_missing ?? health.product_mapping_missing) || 0);
    const marginRiskShare = asNumber(risk.margin_risk_revenue_share_pct);
    const marginRiskCount = Number(risk.margin_risk_sku_count || 0);
    const top1 = asNumber(risk.top1_customer_share_pct);
    const hhi = asNumber(risk.customer_hhi);
    const newShare = asNumber(growth.new_customer_share_pct);
    const returningShare = asNumber(growth.returning_customer_share_pct);
    const profitVisible = headline.profit !== null && headline.profit !== undefined;
    const marginVisible = headline.margin_pct !== null && headline.margin_pct !== undefined;

    const revenueState = (() => {
      const mom = asNumber(headline.revenue_mom_pct);
      const yoy = asNumber(headline.revenue_yoy_pct);
      // Only the short-term figure could ever move this label down, so a
      // window trading +2.9% against the prior fiscal YTD was labelled
      // "Stable" while its own sentence reported -21.8% year over year. A
      // label that contradicts the number beside it is worse than no label.
      const shortDown = mom !== null && mom <= -5;
      const shortUp = mom !== null && mom >= 5;
      const yearDown = yoy !== null && yoy <= -8;
      const yearUp = yoy !== null && yoy >= 8;
      const basis = `${primaryShort} ${mom === null ? `has no clean ${primaryCompare.toLowerCase()} comparator` : formatSigned("percent", mom)}${yoy !== null ? `, YoY ${formatSigned("percent", yoy)}` : ""}`;

      // Divergence earns its own label rather than being rounded to one side.
      if ((shortUp && yearDown) || (shortDown && yearUp)) {
        return { tone: "warning", value: "Mixed", detail: `${basis}. The two bases disagree; read them together before acting on either.` };
      }
      if (shortDown || yearDown) {
        return { tone: "negative", value: "Softening", detail: `${basis}. Review movers and drivers for declining demand.` };
      }
      if (shortUp || yearUp) {
        return { tone: "positive", value: "Strengthening", detail: `${basis}. Growth is currently favorable under the active window.` };
      }
      return { tone: "neutral", value: "Stable", detail: `${basis}. Both comparisons sit inside their flat bands.` };
    })();

    const profitState = (() => {
      if (!profitVisible) {
        return { tone: "neutral", value: "Restricted", detail: "Profit is masked or unavailable because cost visibility or coverage does not support a safe readout." };
      }
      const mom = asNumber(headline.profit_mom_pct);
      if (mom !== null && mom <= -5) {
        return { tone: "negative", value: "Under pressure", detail: `Profit is ${formatSigned("percent", mom)} versus ${primaryCompare.toLowerCase()}${costCoverage !== null ? ` with cost coverage at ${formatByFmt("percent", costCoverage)}` : ""}.` };
      }
      if (mom !== null && mom >= 5) {
        return { tone: "positive", value: "Improving", detail: `Profit is ${formatSigned("percent", mom)} versus ${primaryCompare.toLowerCase()}${costCoverage !== null ? ` and cost coverage is ${formatByFmt("percent", costCoverage)}` : ""}.` };
      }
      return { tone: costCoverage !== null && costCoverage < 90 ? "warning" : "neutral", value: costCoverage !== null && costCoverage < 90 ? "Partial" : "Steady", detail: costCoverage !== null && costCoverage < 90 ? `Profit is visible, but cost coverage at ${formatByFmt("percent", costCoverage)} can limit confidence.` : "Profit is broadly steady for the selected business window." };
    })();

    const marginState = (() => {
      if (!marginVisible) {
        return { tone: "neutral", value: "Restricted", detail: "Margin is masked or unavailable because finance-sensitive inputs are restricted." };
      }
      const margin = asNumber(headline.margin_pct);
      const mom = asNumber(headline.margin_mom);
      if ((margin !== null && margin < 10) || (mom !== null && mom <= -2)) {
        return { tone: "negative", value: "Compressed", detail: `Margin is ${formatByFmt("percent", margin)}${mom !== null ? ` with ${primaryShort} ${formatSignedPoints(mom)}` : ""}. Review price, mix, and low-margin watchlist.` };
      }
      if ((margin !== null && margin >= 20) && (mom === null || mom >= 0)) {
        return { tone: "positive", value: "Healthy", detail: `Margin is ${formatByFmt("percent", margin)}${mom !== null ? ` and ${formatSignedPoints(mom)} versus ${primaryCompare.toLowerCase()}` : ""}.` };
      }
      return { tone: "warning", value: "Watch", detail: `Margin is ${formatByFmt("percent", margin)}${mom !== null ? ` with ${formatSignedPoints(mom)} versus ${primaryCompare.toLowerCase()}` : ""}. Keep watch on profit quality and price discipline.` };
    })();

    const movementState = (() => {
      if (customerDecliners > customerGainers + 1 || (returningShare !== null && returningShare < 55)) {
        return { tone: "negative", value: "Churn watch", detail: `${fmtNumber0.format(customerDecliners)} customer decliners vs ${fmtNumber0.format(customerGainers)} gainers${returningShare !== null ? `, returning share ${formatByFmt("percent", returningShare)}` : ""}.` };
      }
      if ((newShare !== null && newShare >= 25) || customerGainers > customerDecliners) {
        return { tone: "positive", value: "Expanding", detail: `${fmtNumber0.format(customerGainers)} gainers vs ${fmtNumber0.format(customerDecliners)} decliners${newShare !== null ? `, new-customer share ${formatByFmt("percent", newShare)}` : ""}.` };
      }
      return { tone: "neutral", value: "Balanced", detail: `Customer movement is mixed${returningShare !== null ? ` with returning share ${formatByFmt("percent", returningShare)}` : ""}.` };
    })();

    const riskState = (() => {
      const elevated = (top1 !== null && top1 >= 35) || (hhi !== null && hhi >= 2500) || (marginRiskShare !== null && marginRiskShare >= 20);
      const moderate = (top1 !== null && top1 >= 25) || (hhi !== null && hhi >= 1800) || marginRiskCount >= 5;
      if (elevated) {
        return { tone: "negative", value: "Elevated", detail: `Top 1 customer ${top1 !== null ? formatByFmt("percent", top1) : "n/a"}${hhi !== null ? ` · HHI ${formatByFmt("number", hhi)}` : ""}${marginRiskShare !== null ? ` · margin-risk revenue ${formatByFmt("percent", marginRiskShare)}` : ""}.` };
      }
      if (moderate) {
        return { tone: "warning", value: "Watch", detail: `${fmtNumber0.format(marginRiskCount)} flagged margin-risk items${top1 !== null ? ` with Top 1 share ${formatByFmt("percent", top1)}` : ""}.` };
      }
      return { tone: "positive", value: "Manageable", detail: `Concentration and margin risk are currently contained${marginRiskCount ? ` with ${fmtNumber0.format(marginRiskCount)} items still worth monitoring` : ""}.` };
    })();

    const trustState = (() => {
      if ((costCoverage !== null && costCoverage < 80) || (packsCoverage !== null && packsCoverage < 90)) {
        return { tone: "negative", value: "At risk", detail: `Cost coverage ${costCoverage === null ? "n/a" : formatByFmt("percent", costCoverage)}${packsCoverage !== null ? ` · packs ${formatByFmt("percent", packsCoverage)}` : ""}. Sensitive outputs should be treated cautiously.` };
      }
      if ((costCoverage !== null && costCoverage < 90) || (packsCoverage !== null && packsCoverage < 98) || mappingMissing > 0) {
        return { tone: "warning", value: "Watch", detail: `Coverage is usable but incomplete${mappingMissing ? ` · ${fmtNumber0.format(mappingMissing)} mapping gaps remain` : ""}.` };
      }
      return { tone: "positive", value: "High", detail: "Coverage, mapping, and governed freshness currently support confident executive use." };
    })();

    setHealthCard(els.healthRevenueCard, els.healthRevenueState, els.healthRevenueDetail, revenueState.tone, revenueState.value, revenueState.detail);
    setHealthCard(els.healthProfitCard, els.healthProfitState, els.healthProfitDetail, profitState.tone, profitState.value, profitState.detail);
    setHealthCard(els.healthMarginCard, els.healthMarginState, els.healthMarginDetail, marginState.tone, marginState.value, marginState.detail);
    setHealthCard(els.healthMovementCard, els.healthMovementState, els.healthMovementDetail, movementState.tone, movementState.value, movementState.detail);
    setHealthCard(els.healthRiskCard, els.healthRiskState, els.healthRiskDetail, riskState.tone, riskState.value, riskState.detail);
    setHealthCard(els.healthTrustCard, els.healthTrustState, els.healthTrustDetail, trustState.tone, trustState.value, trustState.detail);

    if (els.businessStatusLine) {
      els.businessStatusLine.textContent = `${revenueState.value} revenue, ${riskState.value.toLowerCase()} risk posture, and ${trustState.value.toLowerCase()} data confidence. ${payload?.executive_briefing?.top_action?.detail || "Review the executive briefing for the most important next action."}`;
    }
  };

  const renderActionRail = (payload = {}) => {
    const briefing = payload.executive_briefing || {};
    const action = briefing.top_action || {};
    if (els.actionRailTopTitle) {
      els.actionRailTopTitle.textContent = action.title || "No priority action identified";
    }
    if (els.actionRailTopDetail) {
      els.actionRailTopDetail.textContent = action.detail || "The current window does not require urgent remediation.";
    }
    updateLink(els.actionRailTopLink, briefingLinkFor(action), `Open ${action.title || "next action"}`, action.target || null);
    if (action.target === "margin_risk") {
      setDrilldownPayload(els.actionRailTopLink, overviewWorkspacePayload(action.title || "Margin Risk", action.value, {
        source_section: "Recommended Actions",
        source_widget: "Top Action",
        extra: { workspace_kind: "overview_prebuilt", drilldown: "margin_risk" },
      }));
    } else if (String(action.target || "").startsWith("movers_")) {
      const dimension = String(action.target || "").replace("movers_", "") || "customer";
      setDrilldownPayload(els.actionRailTopLink, overviewWorkspacePayload(action.title || "Movers", action.value, {
        source_section: "Recommended Actions",
        source_widget: "Top Action",
        extra: { workspace_kind: "overview_prebuilt", drilldown: "movers", dimension },
      }));
    } else if (action.target === "concentration") {
      setDrilldownPayload(els.actionRailTopLink, overviewWorkspacePayload(action.title || "Concentration", action.value, {
        source_section: "Recommended Actions",
        source_widget: "Top Action",
        extra: { workspace_kind: "overview_prebuilt", drilldown: "concentration", dimension: "customer" },
      }));
    } else {
      setDrilldownPayload(els.actionRailTopLink, null);
    }
  };

  const renderExecutiveSummary = (payload = {}) => {
    const contract = payload.overview_metrics || {};
    const executive = contract.executive || {};
    const briefing = payload.executive_briefing || contract.briefing || {};
    const health = payload.health || {};
    const windowMeta = getWindowMeta(payload);
    const compareLabel = primaryCompareLabel(windowMeta);
    const compareNote = primaryComparisonNote(windowMeta);
    const concentration = payload.concentration || {};
    const profitability = payload.profitability || {};
    const deltas = payload.deltas || {};
    const packsCoverage = health.packs_coverage_pct ?? (health.pack_missing_pct != null ? (100 - health.pack_missing_pct) : null);
    const costCoverage = executive.cost_coverage_pct ?? health.cost_coverage_pct;
    const top1Share = concentration.customer && concentration.customer.top1_share !== undefined
      ? Number(concentration.customer.top1_share)
      : null;
    const hhi = concentration.customer && concentration.customer.hhi !== undefined
      ? Number(concentration.customer.hhi)
      : null;

    if (els.execRevenueCurrent) {
      els.execRevenueCurrent.textContent = formatByFmt("currency", executive.revenue_current);
    }
    if (els.heroRevenueCurrentChip) {
      els.heroRevenueCurrentChip.textContent = formatByFmt("currency", executive.revenue_current);
    }
    if (els.heroRevenueDeltaLabel) {
      els.heroRevenueDeltaLabel.textContent = revenueDeltaLabel(windowMeta);
    }
    if (els.heroRevenueDeltaPctLabel) {
      els.heroRevenueDeltaPctLabel.textContent = revenueDeltaPctLabel(windowMeta);
    }
    if (els.execRevenueDeltaLabel) {
      els.execRevenueDeltaLabel.textContent = revenueDeltaLabel(windowMeta);
    }
    if (els.execRevenueDeltaPctLabel) {
      els.execRevenueDeltaPctLabel.textContent = revenueDeltaPctLabel(windowMeta);
    }
    if (els.heroPriorWindowChip) {
      const priorLabel = priorWindowLabel(windowMeta);
      els.heroPriorWindowChip.textContent = priorLabel ? `${compareLabel}: ${priorLabel}` : compareLabel;
    }
    if (els.execRevenueDelta) {
      els.execRevenueDelta.textContent = executive.revenue_mom_delta === null || executive.revenue_mom_delta === undefined
        ? "n/a"
        : formatByFmt("currency", executive.revenue_mom_delta);
      els.execRevenueDelta.classList.toggle("text-success", Number(executive.revenue_mom_delta) > 0);
      els.execRevenueDelta.classList.toggle("text-danger", Number(executive.revenue_mom_delta) < 0);
    }
    if (els.heroRevenueDeltaChip) {
      els.heroRevenueDeltaChip.textContent = executive.revenue_mom_delta === null || executive.revenue_mom_delta === undefined
        ? "n/a"
        : formatByFmt("currency", executive.revenue_mom_delta);
      els.heroRevenueDeltaChip.classList.toggle("text-success", Number(executive.revenue_mom_delta) > 0);
      els.heroRevenueDeltaChip.classList.toggle("text-danger", Number(executive.revenue_mom_delta) < 0);
    }
    if (els.execRevenueDeltaPct) {
      const pct = executive.revenue_mom_delta_pct;
      els.execRevenueDeltaPct.textContent = pct === null || pct === undefined ? "n/a" : formatByFmt("percent", pct);
      els.execRevenueDeltaPct.title = pct === null || pct === undefined ? `n/a because the ${compareLabel.toLowerCase()} value is zero or missing.` : "";
    }
    if (els.heroRevenueDeltaPctChip) {
      const pct = executive.revenue_mom_delta_pct;
      els.heroRevenueDeltaPctChip.textContent = pct === null || pct === undefined ? "n/a" : formatByFmt("percent", pct);
      els.heroRevenueDeltaPctChip.title = pct === null || pct === undefined ? `n/a because the ${compareLabel.toLowerCase()} value is zero or missing.` : "";
    }
    if (els.execMainDriver) {
      els.execMainDriver.textContent = executive.main_driver_sentence || "Main driver: not available.";
    }
    if (els.commandMainDriver) {
      els.commandMainDriver.textContent = executive.main_driver_sentence || "Main driver: not available.";
    }
    if (els.execSecondaryDriver) {
      const revYoy = (deltas.revenue || {}).yoy_pct;
      const profitYoy = (deltas.profit || {}).yoy_pct;
      const marginYoy = (deltas.margin_pct || {}).yoy;
      const bits = [];
      if (revYoy !== null && revYoy !== undefined) bits.push(`Revenue YoY ${formatByFmt("percent", revYoy)}`);
      if (profitYoy !== null && profitYoy !== undefined) bits.push(`Profit YoY ${formatByFmt("percent", profitYoy)}`);
      if (marginYoy !== null && marginYoy !== undefined) bits.push(`Margin YoY ${formatSignedPoints(marginYoy)}`);
      els.execSecondaryDriver.textContent = bits.length ? bits.join(" | ") : "Secondary detail: not available.";
    }
    if (els.commandSecondaryDriver) {
      els.commandSecondaryDriver.textContent = els.execSecondaryDriver ? els.execSecondaryDriver.textContent : "Secondary detail: not available.";
    }
    if (els.execCostCoverageBadge) {
      const coverage = costCoverage;
      els.execCostCoverageBadge.textContent =
        coverage === null || coverage === undefined
          ? "Cost coverage: n/a"
          : `Cost coverage: ${formatByFmt("percent", coverage)}`;
      if (coverage !== null && coverage !== undefined) els.execCostCoverageBadge.classList.toggle("text-bg-warning", Number(coverage) < 90);
    }
    if (els.heroCostCoverageBadge) {
      els.heroCostCoverageBadge.textContent =
        costCoverage === null || costCoverage === undefined
          ? "Cost coverage: n/a"
          : `Cost coverage: ${formatByFmt("percent", costCoverage)}`;
      els.heroCostCoverageBadge.classList.toggle("text-warning", costCoverage !== null && costCoverage !== undefined && Number(costCoverage) < 90);
    }
    const fallbackWatchouts = [];
    if (els.execWatchoutsList) {
      const risks = marginRiskCount(profitability);
      fallbackWatchouts.push(`Margin risk SKUs: ${fmtNumber0.format(risks)}`);
      if (top1Share !== null && Number.isFinite(top1Share)) {
        fallbackWatchouts.push(`Customer concentration: Top 1 ${formatByFmt("percent", top1Share)}${hhi !== null && Number.isFinite(hhi) ? ` | HHI ${fmtNumber0.format(hhi)}` : ""}`);
      } else {
        fallbackWatchouts.push("Customer concentration: not available");
      }
      if (costCoverage !== null && costCoverage !== undefined && Number(costCoverage) < 90) {
        fallbackWatchouts.push(`Coverage caveat: cost coverage at ${formatByFmt("percent", costCoverage)}`);
      } else if (packsCoverage !== null && packsCoverage !== undefined && Number(packsCoverage) < 98) {
        fallbackWatchouts.push(`Coverage caveat: packs coverage at ${formatByFmt("percent", packsCoverage)}`);
      } else {
        fallbackWatchouts.push("Coverage caveat: no major gaps detected");
      }
    }
    renderList(els.execWatchoutsList, briefing.watchouts || fallbackWatchouts, "No material watchouts for the active window.");
    renderList(els.briefImprovedList, briefing.improved || [], "No material improvements flagged.");
    renderList(els.briefDeclinedList, briefing.declined || [], "No material declines flagged.");
    renderBriefingCard(els.briefWinCard, els.briefWinTitle, els.briefWinValue, els.briefWinDetail, els.briefWinLink, briefing.biggest_win || {}, "-");
    renderBriefingCard(els.briefDeclineCard, els.briefDeclineTitle, els.briefDeclineValue, els.briefDeclineDetail, els.briefDeclineLink, briefing.biggest_decline || {}, "-");
    renderBriefingCard(els.briefRiskCard, els.briefRiskTitle, els.briefRiskValue, els.briefRiskDetail, null, briefing.key_risk || {}, "-");
    renderBriefingCard(els.briefActionCard, els.briefActionTitle, els.briefActionValue, els.briefActionDetail, els.briefActionLink, briefing.top_action || {}, "Next");

    if (els.costCoverageChip) {
      els.costCoverageChip.textContent = costCoverage === null || costCoverage === undefined ? "n/a" : formatByFmt("percent", costCoverage);
    }
    if (els.packsCoverageChip) {
      els.packsCoverageChip.textContent = packsCoverage === null || packsCoverage === undefined ? "n/a" : formatByFmt("percent", packsCoverage);
    }
    if (els.missingMappingChip) {
      els.missingMappingChip.textContent = fmtNumber0.format(health.product_mapping_missing || 0);
    }
    if (els.freshnessChip) {
      els.freshnessChip.textContent = formatRefreshAge(health.freshness_sla_days, health.freshness_sla_hours);
      const refreshAt = health.governed_refresh_at ? formatTimestampish(health.governed_refresh_at) : "";
      const cutoff = health.data_cutoff ? formatTimestampish(health.data_cutoff, { withTime: false }) : "";
      els.freshnessChip.title = [refreshAt ? `Last refresh ${refreshAt}` : "", cutoff ? `Data cutoff ${cutoff}` : ""].filter(Boolean).join(" | ");
    }
    if (els.heroTrustStatus) {
      let status = "Healthy";
      if ((costCoverage !== null && Number(costCoverage) < 90) || (packsCoverage !== null && Number(packsCoverage) < 98) || Number(health.product_mapping_missing || 0) > 0) {
        status = "Watch";
      }
      if ((costCoverage !== null && Number(costCoverage) < 80) || (packsCoverage !== null && Number(packsCoverage) < 90)) {
        status = "At risk";
      }
      els.heroTrustStatus.textContent = status;
    }
    if (els.execComparisonNote) {
      els.execComparisonNote.textContent = compareNote;
    }
    if (els.commandWindowNote) {
      const currentLabel = currentWindowLabel(windowMeta);
      const priorLabel = priorWindowLabel(windowMeta);
      const suffix = currentLabel && priorLabel ? ` Current: ${currentLabel}. Comparator: ${priorLabel}.` : "";
      els.commandWindowNote.textContent = `${periodModeLabel(windowMeta)} basis. ${compareNote}${suffix}`;
    }
    if (els.commandTrustNote) {
      const trustBits = [];
      if (costCoverage !== null && costCoverage !== undefined) trustBits.push(`Cost coverage ${formatByFmt("percent", costCoverage)}`);
      if (packsCoverage !== null && packsCoverage !== undefined) trustBits.push(`packs ${formatByFmt("percent", packsCoverage)}`);
      if (Number(health.product_mapping_missing || 0) > 0) trustBits.push(`${fmtNumber0.format(health.product_mapping_missing || 0)} mapping gaps`);
      els.commandTrustNote.textContent = trustBits.length
        ? `${trustBits.join(" · ")}. Sensitive finance signals inherit these caveats.`
        : "Coverage and governance context currently support executive use without major caveats.";
    }
    renderActionRail(payload);
  };

  const renderExecutiveScorecard = (payload = {}) => {
    const scorecard = payload.executive_scorecard || {};
    const headline = scorecard.headline || {};
    const unit = scorecard.unit_economics || {};
    const growth = scorecard.growth_retention || {};
    const reconciliation = scorecard.sales_reconciliation || {};
    const risk = scorecard.risk_indicators || {};
    const profitability = payload.profitability || {};
    const minimumMargin = asNumber(profitability.minimum_margin_pct);
    const targetMargin = asNumber(profitability.target_margin_pct);
    const derivedMarginStatus = deriveMarginStatusKey(headline.margin_pct, minimumMargin, targetMargin);
    const windowMeta = getWindowMeta(payload);
    const compareLabel = primaryCompareLabel(windowMeta);
    const compareNote = primaryComparisonNote(windowMeta);
    const setText = (el, value) => {
      if (!el) return;
      el.textContent = value;
    };
    setText(els.scoreRevenue, formatByFmt("currency", headline.revenue));
    setText(els.scoreProfit, headline.profit === null || headline.profit === undefined ? "Restricted" : formatByFmt("currency", headline.profit));
    setText(els.scoreMargin, headline.margin_pct === null || headline.margin_pct === undefined ? "Restricted" : formatByFmt("percent", headline.margin_pct));
    setText(els.scoreRevenueMom, formatSigned("percent", headline.revenue_mom_pct));
    setText(els.scoreAsp, formatByFmt("currency", unit.asp));
    setText(els.scoreAov, formatByFmt("currency", unit.aov));
    setText(els.scoreProfitPerOrder, unit.profit_per_order === null || unit.profit_per_order === undefined ? "Restricted" : formatByFmt("currency", unit.profit_per_order));
    setText(els.scoreProfitPerLb, unit.profit_per_lb === null || unit.profit_per_lb === undefined ? "Restricted" : formatByFmt("currency", unit.profit_per_lb));
    setText(els.scoreNewShare, formatByFmt("percent", growth.new_customer_share_pct));
    setText(els.scoreReturningShare, formatByFmt("percent", growth.returning_customer_share_pct));
    setText(els.scoreRetention, formatByFmt("percent", growth.retention_rate_pct));
    setText(els.scoreLogoChurn, formatByFmt("percent", growth.logo_churn_rate_pct));
    setText(els.scoreRevenueChurn, formatByFmt("percent", growth.revenue_churn_rate_pct));
    setText(els.scoreNrr, formatByFmt("percent", growth.nrr_pct));
    setText(els.scoreArpa, formatByFmt("currency", growth.arpa));
    setText(
      els.scoreArpaCohorts,
      growth.new_arpa === null || growth.new_arpa === undefined || growth.established_arpa === null || growth.established_arpa === undefined
        ? "n/a"
        : `${formatByFmt("currency", growth.new_arpa)} / ${formatByFmt("currency", growth.established_arpa)}`
    );
    setText(els.reconGrossSales, formatByFmt("currency", reconciliation.gross_sales));
    setText(els.reconDiscounts, formatByFmt("currency", reconciliation.discounts));
    setText(els.reconInvoiceSales, formatByFmt("currency", reconciliation.invoice_sales));
    setText(els.reconReturns, formatByFmt("currency", reconciliation.returns));
    setText(els.reconAdjustmentCosts, formatByFmt("currency", reconciliation.adjustment_costs));
    setText(els.reconNetSales, formatByFmt("currency", reconciliation.net_sales));
    setText(els.salesBasisNote, reconciliation.basis_note || "Gross-to-net components are unavailable for this source snapshot.");
    if (els.reconSourceNote && reconciliation.return_count !== null && reconciliation.return_count !== undefined) {
      els.reconSourceNote.textContent = `${formatByFmt("number", reconciliation.return_count)} approved returns matched to orders in this scope. Discount and handling costs are explicit source fields.`;
    }
    if (els.scoreConcentration) {
      const top1 = risk.top1_customer_share_pct;
      const hhi = risk.customer_hhi;
      els.scoreConcentration.textContent =
        top1 === null || top1 === undefined
          ? "n/a"
          : `${formatByFmt("percent", top1)} / HHI ${hhi === null || hhi === undefined ? "n/a" : formatByFmt("number", hhi)}`;
    }
    if (els.scoreMarginRisk) {
      const count = risk.margin_risk_sku_count;
      const share = risk.margin_risk_revenue_share_pct;
      els.scoreMarginRisk.textContent = `${formatByFmt("number", count)}${share === null || share === undefined ? "" : ` (${formatByFmt("percent", share)})`}`;
    }
    if (els.scoreRevenueComparisonLabel) {
      els.scoreRevenueComparisonLabel.textContent = primaryCardLabel(windowMeta);
    }
    if (els.scoreRevenueMeta) {
      els.scoreRevenueMeta.textContent = headline.revenue_mom_pct === null || headline.revenue_mom_pct === undefined
        ? `${compareLabel} delta unavailable`
        : `${primaryDeltaLabel(windowMeta)} ${formatSigned("percent", headline.revenue_mom_pct)}`;
    }
    if (els.scoreRevenueSupport) {
      els.scoreRevenueSupport.textContent = headline.revenue_yoy_pct === null || headline.revenue_yoy_pct === undefined
        ? "YoY comparator unavailable"
        : `YoY ${formatSigned("percent", headline.revenue_yoy_pct)}`;
    }
    if (els.scoreProfitMeta) {
      els.scoreProfitMeta.textContent = headline.profit_mom_pct === null || headline.profit_mom_pct === undefined
        ? `${compareLabel} profit delta unavailable`
        : `${primaryDeltaLabel(windowMeta)} ${formatSigned("percent", headline.profit_mom_pct)}`;
    }
    if (els.scoreProfitSupport) {
      const coverage = risk.cost_coverage_pct;
      els.scoreProfitSupport.textContent = coverage === null || coverage === undefined
        ? "Cost coverage unavailable"
        : `Cost coverage ${formatByFmt("percent", coverage)}`;
    }
    if (els.scoreMarginMeta) {
      els.scoreMarginMeta.textContent = targetMargin === null && minimumMargin === null
        ? (headline.margin_mom === null || headline.margin_mom === undefined
          ? `${compareLabel} margin delta unavailable`
          : `${primaryDeltaLabel(windowMeta)} ${formatSignedPoints(headline.margin_mom)}`)
        : marginTargetSummary({
          margin_pct: headline.margin_pct,
          minimum_margin_pct: minimumMargin,
          target_margin_pct: targetMargin,
          status_key: derivedMarginStatus,
        });
    }
    if (els.scoreMarginSupport) {
      const riskCount = risk.margin_risk_sku_count;
      const watchText = riskCount === null || riskCount === undefined
        ? marginStatusLabel(derivedMarginStatus)
        : `${marginStatusLabel(derivedMarginStatus)} · ${formatByFmt("number", riskCount)} below-target SKUs`;
      els.scoreMarginSupport.textContent = targetMargin === null && minimumMargin === null
        ? (headline.margin_yoy === null || headline.margin_yoy === undefined
          ? "YoY margin comparator unavailable"
          : `YoY ${formatSignedPoints(headline.margin_yoy)}`)
        : watchText;
    }
    if (els.scoreRevenueMomMeta) {
      els.scoreRevenueMomMeta.textContent = headline.revenue_mom === null || headline.revenue_mom === undefined
        ? "Delta amount unavailable"
        : `${formatByFmt("currency", headline.revenue_mom)} vs ${compareLabel.toLowerCase()}`;
    }
    if (els.scoreRevenueMomSupport) {
      els.scoreRevenueMomSupport.textContent = compareNote;
    }
    if (els.execNarrativeList) {
      const rows = Array.isArray(scorecard.narrative) ? scorecard.narrative.filter(Boolean) : [];
      els.execNarrativeList.innerHTML = rows.length
        ? rows.map((line) => `<li>${line}</li>`).join("")
        : '<li class="text-muted">Narrative not available for current window.</li>';
    }
  };

  const destroyChart = (name) => {
    if (charts[name]) {
      charts[name].destroy();
      charts[name] = null;
    }
  };

  const renderTrend = (trend = {}) => {
    if (!els.trendChart) return;
    destroyChart("trend");
    const windowMeta = getWindowMeta();
    if (!canRenderCharts()) {
      showChartFallback(els.trendChart, "Trend chart is unavailable right now. Numeric diagnostics remain active.");
      if (els.trendEmpty) {
        els.trendEmpty.classList.remove("d-none");
        els.trendEmpty.textContent = "Trend visualization is temporarily unavailable.";
      }
      if (els.trendSparseText) {
        els.trendSparseText.textContent = "Trend points are available in summary mode while charting is unavailable.";
      }
      ensureChartWarning();
      return;
    }
    clearChartFallback(els.trendChart);
    const freq = state.trend.freq || "monthly";
    const trendBlock =
      trend[freq] ||
      (freq === "weekly" ? trend.weekly : trend.monthly) ||
      trend;
    const labels = trendBlock.labels || trendBlock.months || trend.labels || trend.months || [];
    const overlayKey = state.trend.overlay || "profit";
    const overlayLabel = overlayKey === "margin_pct" ? "Margin %" : overlayKey === "units" ? "Units" : "Profit";
    if (els.trendSummaryText) {
      const note = windowMeta.trajectory_note ? `${windowMeta.trajectory_note} ` : "";
      const bucketLabel = freq === "weekly" ? "Weekly" : String(trendBlock.bucket_label || windowMeta.trend_bucket_label || "Monthly");
      els.trendSummaryText.textContent = `${note}${bucketLabel} revenue baseline with ${overlayLabel.toLowerCase()} overlay${state.trend.rolling ? " and rolling average" : ""}.`;
    }
    if (!labels.length) {
      if (els.trendEmpty) els.trendEmpty.classList.remove("d-none");
      els.trendChart.classList.add("d-none");
      if (els.trendSparseText) els.trendSparseText.textContent = "No points available for the selected time window.";
      return;
    }
    if (els.trendEmpty) els.trendEmpty.classList.add("d-none");
    els.trendChart.classList.remove("d-none");
    const revenue = (trendBlock.revenue || []).map((v) => (v === null ? null : Number(v)));
    const overlayRaw = trendBlock[overlayKey] || [];
    const overlay = overlayRaw.map((v) => (v === null ? null : Number(v)));
    if (els.trendSparseText) {
      const populated = revenue.filter((v) => v !== null && v !== undefined).length;
      els.trendSparseText.textContent = populated < 3
        ? `Sparse window: only ${fmtNumber0.format(populated)} populated point${populated === 1 ? "" : "s"} available.`
        : `${fmtNumber0.format(labels.length)} periods loaded for the active filters.${windowMeta.is_partial_period ? " Latest period is partial and recent deltas are aligned to elapsed days." : ""}`;
    }
    const rolling = revenue.map((_, idx) => {
      const start = Math.max(0, idx - 2);
      const window = revenue.slice(start, idx + 1).filter((val) => val !== null && val !== undefined && Number.isFinite(val));
      if (!window.length) return null;
      const sum = window.reduce((acc, val) => acc + Number(val || 0), 0);
      return sum / window.length;
    });

    const overlayMeta = {
      profit: { label: "Profit", color: ChartUtils.seriesColor(5), yAxisID: "y" },
      margin_pct: { label: "Margin %", color: ChartUtils.seriesColor(0), yAxisID: "y1" },
      units: { label: "Units", color: ChartUtils.seriesColor(0), yAxisID: "y1" },
    };
    const selectedOverlay = overlayMeta[overlayKey] || overlayMeta.profit;

    const datasets = [
      { type: "line", label: "Revenue", data: revenue, borderColor: ChartUtils.seriesColor(1), backgroundColor: ChartUtils.seriesFill(1, 0.12), tension: 0.25, yAxisID: "y" },
      { type: "line", label: selectedOverlay.label, data: overlay, borderColor: selectedOverlay.color, backgroundColor: `${selectedOverlay.color}33`, tension: 0.25, yAxisID: selectedOverlay.yAxisID, borderDash: overlayKey === "margin_pct" ? [5, 4] : undefined },
    ];
    if (state.trend.rolling) {
      datasets.push({ type: "line", label: "Revenue Rolling Avg", data: rolling, borderColor: ChartUtils.seriesColor(4), backgroundColor: ChartUtils.seriesFill(4, 0.1), tension: 0.25, yAxisID: "y", borderDash: [6, 4], pointRadius: 0 });
    }

    const ctx = els.trendChart.getContext("2d");
    charts.trend = new Chart(ctx, {
      data: { labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        scales: {
          y: { beginAtZero: true, ticks: { callback: (v) => fmtCurrency0.format(Number(v) || 0) } },
          y1: {
            beginAtZero: true,
            position: "right",
            grid: { drawOnChartArea: false },
            display: overlayKey !== "profit",
            ticks: {
              callback: (v) => {
                if (overlayKey === "margin_pct") return `${fmtNumber1.format(Number(v) || 0)}%`;
                return fmtNumber0.format(Number(v) || 0);
              },
            },
          },
        },
        plugins: {
          legend: { display: true, position: "bottom" },
          // Axis ticks were formatted and the hover label was not, so the
          // same figure read "$1.1M" on the axis and "1,073,546.357" under
          // the pointer. Format both from the same helpers.
          tooltip: {
            callbacks: {
              label: (context) => {
                const value = context.parsed?.y;
                if (value === null || value === undefined) return `${context.dataset.label}: n/a`;
                if (context.dataset.yAxisID === "y") return `${context.dataset.label}: ${fmtCurrency0.format(value)}`;
                if (overlayKey === "margin_pct") return `${context.dataset.label}: ${fmtPercent1(value)}`;
                return `${context.dataset.label}: ${fmtNumber0.format(value)}`;
              },
            },
          },
        },
      },
    });
  };

  const renderMix = (mix = {}, dim = "customer") => {
    if (!els.mixChart) return;
    destroyChart("mix");
    if (!canRenderCharts()) {
      showChartFallback(els.mixChart, "Mix chart unavailable. Use movers and concentration panels for detail.");
      ensureChartWarning();
      return;
    }
    const rows = mix[dim] || [];
    if (!rows.length) {
      showChartFallback(els.mixChart, "No mix data for the selected filters.");
      return;
    }
    clearChartFallback(els.mixChart);
    const labels = rows.map((r) => r.label);
    const values = rows.map((r) => r.value);
    const entityIds = rows.map((r) => r.entity_id || r.label);
    const ctx = els.mixChart.getContext("2d");
    charts.mix = new Chart(ctx, {
      type: "bar",
      data: { labels, datasets: [{ label: "Revenue", data: values, backgroundColor: ChartUtils.seriesFill(1, 0.3), borderColor: ChartUtils.seriesColor(1), borderWidth: 1 }] },
      options: {
        indexAxis: "y",
        responsive: true,
        maintainAspectRatio: false,
        onClick: (_evt, elements) => {
          const idx = elements && elements.length ? elements[0].index : null;
          if (idx === null || idx === undefined) return;
          const href = buildDrillLink(dim, entityIds[idx]);
          if (href) window.location.assign(href);
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: (context) => `${context.dataset.label}: ${fmtCurrency0.format(context.parsed?.x)}`,
            },
          },
        },
        scales: { x: { ticks: { callback: (v) => fmtCurrency0.format(Number(v) || 0) } } },
      },
    });
  };

  const renderPareto = (pareto = {}, dim = "customer") => {
    if (!els.paretoChart) return;
    destroyChart("pareto");
    if (!canRenderCharts()) {
      showChartFallback(els.paretoChart, "Pareto chart unavailable. Concentration scores are still available.");
      ensureChartWarning();
      return;
    }
    const payload = pareto[dim] || {};
    const labels = payload.labels || [];
    if (!labels.length) {
      showChartFallback(els.paretoChart, "No Pareto data for the selected filters.");
      return;
    }
    clearChartFallback(els.paretoChart);
    const values = payload.values || [];
    const entityIds = payload.entity_ids || [];
    const cum = payload.cum_pct || [];
    const ctx = els.paretoChart.getContext("2d");
    charts.pareto = new Chart(ctx, {
      data: {
        labels,
        datasets: [
          { type: "bar", label: "Revenue", data: values, backgroundColor: ChartUtils.seriesFill(1, 0.25), borderColor: ChartUtils.seriesColor(1), borderWidth: 1, yAxisID: "y" },
          { type: "line", label: "Cumulative %", data: cum, borderColor: ChartUtils.seriesColor(0), backgroundColor: ChartUtils.seriesFill(0, 0.10), tension: 0.2, yAxisID: "y1" },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        onClick: (_evt, elements) => {
          const idx = elements && elements.length ? elements[0].index : null;
          if (idx === null || idx === undefined) return;
          const href = buildDrillLink(dim, entityIds[idx] || labels[idx]);
          if (href) window.location.assign(href);
        },
        scales: {
          y: { beginAtZero: true, ticks: { callback: (v) => fmtCurrency0.format(Number(v) || 0) } },
          y1: { beginAtZero: true, position: "right", min: 0, max: 100, grid: { drawOnChartArea: false }, ticks: { callback: (v) => `${v}%` } },
        },
        plugins: {
          legend: { display: true, position: "bottom" },
          tooltip: {
            callbacks: {
              label: (context) => {
                const value = context.parsed?.y;
                if (value === null || value === undefined) return `${context.dataset.label}: n/a`;
                return context.dataset.yAxisID === "y1"
                  ? `${context.dataset.label}: ${fmtPercent1(value)}`
                  : `${context.dataset.label}: ${fmtCurrency0.format(value)}`;
              },
            },
          },
        },
      },
    });
  };

  const renderHealth = (health = {}) => {
    if (els.healthRows) els.healthRows.textContent = `${fmtNumber0.format(health.rows || 0)} rows`;
    if (els.healthBadges) {
      const packsCoverage = health.packs_coverage_pct ?? (health.pack_missing_pct != null ? (100 - health.pack_missing_pct) : null);
      const packsCovered = health.has_packs_orderlines;
      const packsTotal = health.total_orderlines;
      const packsRatio = Number.isFinite(Number(packsCovered)) && Number.isFinite(Number(packsTotal)) && Number(packsTotal) > 0
        ? ` (${fmtNumber0.format(packsCovered)} / ${fmtNumber0.format(packsTotal)})`
        : "";
      const packsLabel = packsCoverage == null ? "n/a" : `${packsCoverage}%${packsRatio}`;
      const costCoverage = health.cost_coverage_pct ?? (health.cost_missing_pct != null ? (100 - health.cost_missing_pct) : null);
      const freshnessLabel = formatRefreshAge(health.freshness_sla_days, health.freshness_sla_hours);
      const trustCard = (label, value, detail) => `
        <article class="trust-card">
          <div class="trust-label">${label}</div>
          <div class="score-value">${value}</div>
          <div class="trust-detail">${detail}</div>
        </article>`;
      els.healthBadges.innerHTML = `
        ${trustCard("Cost coverage", costCoverage == null ? "n/a" : `${fmtNumber1.format(Number(costCoverage) || 0)}%`, costCoverage != null && Number(costCoverage) < 90 ? "Finance outputs should be treated cautiously." : "Coverage is healthy for sensitive metrics.")}
        ${trustCard("Packs coverage", packsLabel, packsCoverage != null && Number(packsCoverage) < 98 ? "Weighted metrics may be understated." : "Weighted metrics have strong pack coverage.")}
        ${trustCard("Missing mapping", fmtNumber0.format(health.product_mapping_missing || 0), Number(health.product_mapping_missing || 0) > 0 ? "Resolve orphaned items to improve movers and mix." : "No significant mapping gaps detected.")}
        ${trustCard("Refresh age", freshnessLabel, freshnessLabel !== "n/a" ? "Age of the latest governed refresh checkpoint, not the filtered period end." : "No governed refresh marker available.")}
      `;
    }
    if (els.healthList) {
      const issues = Array.isArray(health.issues) ? health.issues : [];
      els.healthList.innerHTML = issues.length
        ? issues.map((i) => `<li><span>${i.label}</span><span class="issue-count">${fmtNumber0.format(i.count || 0)}</span></li>`).join("")
        : '<li><span class="text-muted">No major data issues detected.</span><span class="issue-count">0</span></li>';
    }
    if (els.dataHealthActions) {
      const actions = [];
      const costCoverage = Number(health.cost_coverage_pct ?? NaN);
      const packsCoverage = Number(health.packs_coverage_pct ?? NaN);
      if (Number.isFinite(costCoverage) && costCoverage < 90) {
        actions.push("Add missing cost for uncovered SKUs to stabilize profit and margin analytics.");
      }
      if (Number.isFinite(packsCoverage) && packsCoverage < 98) {
        actions.push("Backfill pack mappings to avoid undercounting weighted metrics.");
      }
      if (Number(health.product_mapping_missing || 0) > 0) {
        actions.push("Resolve missing product mappings to prevent orphaned revenue in movers and mix.");
      }
      const comparisonNote = health.comparison_note ? `${health.comparison_note} ` : "";
      els.dataHealthActions.textContent = actions.length
        ? `${comparisonNote}${actions.join(" ")}`
        : `${comparisonNote}Coverage and mapping integrity are currently healthy.`.trim();
    }
  };

  const applyCoverageBanner = (health = {}) => {
    const coverage = health.packs_coverage_pct ?? (health.pack_missing_pct != null ? (100 - health.pack_missing_pct) : null);
    const costCoverage = health.cost_coverage_pct;
    const severePackGap = coverage !== null && coverage !== undefined && Number(coverage) < 85;
    const severeCostGap = costCoverage !== null && costCoverage !== undefined && Number(costCoverage) < 80;
    const severeMappingGap = Number(health.product_mapping_missing || 0) >= 50;
    if (coverage == null && costCoverage == null) {
      setBanner(null);
      return;
    }
    if (severePackGap || severeCostGap || severeMappingGap) {
      setBanner("High-severity data quality risk detected. Review Data Health before using sensitive comparisons.", "warning");
      return;
    }
    setBanner(null);
  };

  const updateGlobalCoveragePanel = (health = {}) => {
    void health;
  };

  const renderTopMovers = (movers = {}, dim = "customer", sortKey = "delta_abs") => {
    if (!els.moversGainersBody || !els.moversDeclinersBody || !els.topMoversEmpty) return;
    const windowMeta = getWindowMeta();
    const bucket = movers[dim] || {};
    let gainers = Array.isArray(bucket.gainers) ? [...bucket.gainers] : [];
    let decliners = Array.isArray(bucket.decliners) ? [...bucket.decliners] : [];

    const sorter = (a, b) => {
      if (sortKey === "delta_pct") {
        const aScore = a.delta_pct === null || a.delta_pct === undefined
          ? (a.delta_pct_label === "New" ? Number.MAX_SAFE_INTEGER : 0)
          : Math.abs(Number(a.delta_pct) || 0);
        const bScore = b.delta_pct === null || b.delta_pct === undefined
          ? (b.delta_pct_label === "New" ? Number.MAX_SAFE_INTEGER : 0)
          : Math.abs(Number(b.delta_pct) || 0);
        return bScore - aScore;
      }
      return Math.abs(Number(b.delta) || 0) - Math.abs(Number(a.delta) || 0);
    };
    gainers = gainers.sort(sorter).slice(0, 10);
    decliners = decliners.sort(sorter).slice(0, 10);
    if (els.moversSummaryText) {
      const dimLabel = dim === "customer" ? "customers" : dim === "product" ? "products" : dim === "region" ? "regions" : dim;
      const priorLabel = priorWindowLabel(windowMeta);
      els.moversSummaryText.textContent = `Showing ${fmtNumber0.format(gainers.length)} gainers and ${fmtNumber0.format(decliners.length)} decliners for ${dimLabel} versus ${primaryCompareLabel(windowMeta).toLowerCase()}${priorLabel ? ` (${priorLabel})` : ""}.`;
    }

    if (!gainers.length && !decliners.length) {
      els.moversGainersBody.innerHTML = "";
      els.moversDeclinersBody.innerHTML = "";
      els.topMoversEmpty.classList.remove("d-none");
      return;
    }
    els.topMoversEmpty.classList.add("d-none");

    const renderRows = (rows) =>
      rows
        .map((r) => {
          const delta = Number(r.delta) || 0;
          const barWidth = Math.min(100, Math.max(8, Math.abs(delta) / 1000));
          const deltaPct = r.delta_pct === null || r.delta_pct === undefined ? null : Number(r.delta_pct);
          let pctText = r.delta_pct_label || null;
          if (!pctText) {
            if (deltaPct === null) {
              const prev = Number(r.previous || 0);
              const curr = Number(r.current || 0);
              pctText = prev === 0 && curr > 0 ? "New" : prev > 0 && curr === 0 ? "Lost" : "n/a";
            } else {
              pctText = fmtPercent1(deltaPct);
            }
          }
          const pctTip = pctText === "New"
            ? 'title="New contributor: prior-period value was $0."'
            : pctText === "Lost"
              ? 'title="Lost contributor: no current-period value from a non-zero prior period."'
              : pctText === "Low base"
                ? 'title="Low-base denominator: percent change is unstable; prioritize $ delta."'
              : "";
          const entityLink = buildDrillLink(dim, r.entity_id || r.label);
          const labelHtml = entityLink
            ? `<a href="${entityLink}" class="text-decoration-none fw-semibold">${r.label || "Unknown"}</a>`
            : `<span>${r.label || "Unknown"}</span>`;
          return `
          <tr>
            <td class="text-truncate" title="${r.label || ""}">
              <div class="d-flex flex-column gap-1">
                ${labelHtml}
                <span style="display:inline-block;height:4px;background:${delta >= 0 ? (window.ChartUtils?.seriesColor?.(0) || '#2f8f70') : (window.ChartUtils?.seriesColor?.(3) || '#c44f4f')};width:${barWidth}px;border-radius:999px;"></span>
              </div>
            </td>
            <td class="text-end">${fmtCurrency1.format(r.current || 0)}</td>
            <td class="text-end ${delta > 0 ? "text-success" : delta < 0 ? "text-danger" : "text-muted"}">${fmtCurrency1.format(delta)}</td>
            <td class="text-end ${delta > 0 ? "text-success" : delta < 0 ? "text-danger" : "text-muted"}" ${pctTip}>${pctText}</td>
          </tr>`;
        })
        .join("");

    els.moversGainersBody.innerHTML = gainers.length ? renderRows(gainers) : '<tr><td colspan="4" class="text-muted">No gainers</td></tr>';
    els.moversDeclinersBody.innerHTML = decliners.length ? renderRows(decliners) : '<tr><td colspan="4" class="text-muted">No decliners</td></tr>';
  };

  const renderCommercialFocus = (payload = {}, insightsPayload = {}) => {
    const customerMovers = ((payload.top_movers || {}).customer || {});
    const gainers = Array.isArray(customerMovers.gainers) ? [...customerMovers.gainers] : [];
    const decliners = Array.isArray(customerMovers.decliners) ? [...customerMovers.decliners] : [];
    gainers.sort((a, b) => Math.abs(Number(b.delta) || 0) - Math.abs(Number(a.delta) || 0));
    decliners.sort((a, b) => Math.abs(Number(a.delta) || 0) - Math.abs(Number(b.delta) || 0));
    const leadGainer = gainers[0] || null;
    const leadDecliner = decliners[0] || null;
    const profitability = insightsPayload.profitability || {};
    const marginRisk = Array.isArray(profitability.margin_risk) ? profitability.margin_risk : [];
    const leadRisk = [...marginRisk].sort((a, b) => Number(a.profit_impact || 0) - Number(b.profit_impact || 0))[0] || null;
    // The bundle caps the watchlist rows it sends, so prefer the share the
    // server computed over the whole population and only fall back to summing
    // the rows on hand (older payloads, and any caller that builds the shape
    // by hand).
    const serverRiskRevenueShare = asNumber(profitability.margin_risk_revenue_share_pct);
    const riskRevenueShare = serverRiskRevenueShare !== null
      ? serverRiskRevenueShare
      : marginRisk.reduce((acc, row) => acc + (Number(row.revenue_share || 0) || 0), 0);
    const marginStats = profitability.margin_pct || {};
    const p50 = asNumber(marginStats.p50);
    const p10 = asNumber(marginStats.p10);
    const belowZero = asNumber(marginStats.below_zero);

    setFocusCard(
      els.focusLeadCustomerTitle,
      els.focusLeadCustomerValue,
      els.focusLeadCustomerDetail,
      leadGainer?.label || "No major gainer",
      leadGainer ? formatByFmt("currency", leadGainer.delta) : "-",
      leadGainer
        ? `${formatByFmt("currency", leadGainer.current)} current revenue${leadGainer.delta_pct_label ? ` • ${leadGainer.delta_pct_label}` : leadGainer.delta_pct !== null && leadGainer.delta_pct !== undefined ? ` • ${formatByFmt("percent", leadGainer.delta_pct)}` : ""}.`
        : "No customer gainer cleared the current top-10 movement threshold."
    );

    setFocusCard(
      els.focusDecliningCustomerTitle,
      els.focusDecliningCustomerValue,
      els.focusDecliningCustomerDetail,
      leadDecliner?.label || "No major decliner",
      leadDecliner ? formatByFmt("currency", leadDecliner.delta) : "-",
      leadDecliner
        ? `${formatByFmt("currency", leadDecliner.current)} current revenue${leadDecliner.delta_pct_label ? ` • ${leadDecliner.delta_pct_label}` : leadDecliner.delta_pct !== null && leadDecliner.delta_pct !== undefined ? ` • ${formatByFmt("percent", leadDecliner.delta_pct)}` : ""}.`
        : "No customer decliner cleared the current top-10 movement threshold."
    );

    setFocusCard(
      els.focusCustomerMotionTitle,
      els.focusCustomerMotionValue,
      els.focusCustomerMotionDetail,
      "Customer breadth",
      `${fmtNumber0.format(gainers.length)} up / ${fmtNumber0.format(decliners.length)} down`,
      gainers.length || decliners.length
        ? `${leadGainer ? `${leadGainer.label} leads gains` : "No lead gainer"}${leadDecliner ? ` while ${leadDecliner.label} leads declines` : ""}.`
        : "Customer movement is muted for the active business window."
    );

    setFocusCard(
      els.focusSkuRiskTitle,
      els.focusSkuRiskValue,
      els.focusSkuRiskDetail,
      leadRisk?.label || "No lead SKU risk",
      leadRisk ? formatByFmt("currency", leadRisk.profit_impact) : "-",
      leadRisk
        ? `${leadRisk.supplier || "Unknown supplier"} / ${leadRisk.protein || "Unknown department"}${leadRisk.revenue !== null && leadRisk.revenue !== undefined ? ` • revenue ${formatByFmt("currency", leadRisk.revenue)}` : ""}.`
        : "No current SKU met the margin-risk watchlist threshold."
    );

    setFocusCard(
      els.focusSkuRiskCountTitle,
      els.focusSkuRiskCountValue,
      els.focusSkuRiskCountDetail,
      "SKU watchlist",
      fmtNumber0.format(marginRiskCount(profitability)),
      marginRisk.length
        ? `${fmtPercent1(riskRevenueShare)} of visible revenue sits in the current top SKU margin-risk watchlist.`
        : "Visible revenue does not currently require a top-10 SKU margin-risk watchlist."
    );

    const profitabilityValue = p50 === null ? "-" : formatByFmt("percent", p50);
    const profitabilityDetail = [
      p10 !== null ? `P10 ${formatByFmt("percent", p10)}` : null,
      belowZero !== null ? `${fmtNumber0.format(belowZero)} SKUs below 0% margin` : null,
    ].filter(Boolean).join(" • ");
    setFocusCard(
      els.focusProfitabilityTitle,
      els.focusProfitabilityValue,
      els.focusProfitabilityDetail,
      "Margin quality",
      profitabilityValue,
      profitabilityDetail || "Profitability distribution will appear when scoped margin diagnostics are available."
    );
  };

  const renderInsights = (insights = {}) => {
    if (!els.insightsList) return;
    const callouts = Array.isArray(insights.callouts) ? insights.callouts : [];
    if (!callouts.length) {
      if (els.insightsEmpty) els.insightsEmpty.classList.remove("d-none");
      if (els.insightsEmpty && insights.message) {
        els.insightsEmpty.textContent = insights.message;
      }
      els.insightsList.innerHTML = "";
      return;
    }
    if (els.insightsEmpty) els.insightsEmpty.classList.add("d-none");
    const severityClass = (sev) => {
      if (sev === "positive") return "text-success";
      if (sev === "negative") return "text-danger";
      if (sev === "warning") return "text-warning";
      if (sev === "neutral") return "text-muted";
      return "text-primary";
    };
    els.insightsList.innerHTML = callouts
      .map((item) => {
        const value = item.value === null || item.value === undefined ? "n/a" : formatByFmt(item.value_fmt, item.value);
        const detail = item.detail ? `<div class="insight-detail">${item.detail}</div>` : "";
        const link = item.link ? buildDrillLink(item.link.kind, item.link.id) : null;
        const linkHtml = link ? `<a class="stretched-link" href="${link}"></a>` : "";
        const tip = item.tooltip
          ? `<i class="bi bi-info-circle text-muted ms-1" data-bs-toggle="tooltip" title="${item.tooltip}"></i>`
          : "";
        return `
          <div class="col-md-6 col-lg-4">
            <div class="insight-item position-relative ${severityClass(item.severity)}">
              <div class="insight-title d-flex align-items-center gap-1">${item.title || ""}${tip}</div>
              <div class="insight-value">${value}</div>
              ${detail}
              ${linkHtml}
            </div>
          </div>`;
      })
      .join("");
    if (typeof bootstrap !== "undefined" && bootstrap.Tooltip) {
      els.insightsList.querySelectorAll('[data-bs-toggle="tooltip"]').forEach((el) => new bootstrap.Tooltip(el));
    }
  };

  const renderDrivers = (drivers = {}) => {
    if (!els.driversMomRows || !els.driversYoyRows) return;

    const windowMeta = getWindowMeta();
    let metric = state.driverMetric || "revenue";
    const profitHasData = ["mom", "yoy"].some((period) => {
      const block = (drivers[period] || {}).profit || {};
      return block.delta !== null && block.delta !== undefined;
    });
    if (!profitHasData && metric === "profit") {
      metric = "revenue";
      state.driverMetric = "revenue";
    }
    const metricLabel = metric === "profit" ? "Profit" : "Revenue";
    if (els.driversTitle) {
      els.driversTitle.textContent = `${metricLabel} drivers and decomposition`;
    }
    if (els.driversMomTitle) {
      const momLabel = (drivers.mom || {}).label || primaryDeltaLabel(windowMeta) || "Primary comparison";
      els.driversMomTitle.textContent = momLabel === "Prior window" ? "Primary comparison" : momLabel;
    }
    if (els.driversYoyTitle) {
      els.driversYoyTitle.textContent = (drivers.yoy || {}).comparison_label || "Same period last year";
    }
    if (els.driversMetricToggle) {
      const revBtn = els.driversMetricToggle.querySelector('button[data-driver-metric="revenue"]');
      const profitBtn = els.driversMetricToggle.querySelector('button[data-driver-metric="profit"]');
      if (revBtn) revBtn.classList.toggle("active", metric === "revenue");
      if (profitBtn) {
        profitBtn.classList.toggle("active", metric === "profit");
        profitBtn.disabled = !profitHasData;
      }
    }
    const method = drivers.methodology || {};
    const definitions = method.definitions || {};

    const getMetricBlock = (periodBlock = {}) => {
      const candidate = periodBlock && typeof periodBlock === "object" ? periodBlock[metric] || {} : {};
      if (candidate && Object.keys(candidate).length) return candidate;
      if (metric === "revenue" && periodBlock && periodBlock.revenue) return periodBlock.revenue;
      if (metric === "profit" && periodBlock && periodBlock.profit) return periodBlock.profit;
      return {};
    };

    const asNum = (v) => (v === null || v === undefined || Number.isNaN(Number(v)) ? null : Number(v));
    const pctOrNa = (v) => (v === null || v === undefined ? "n/a" : `${fmtNumber1.format(Number(v))}%`);
    const signedPctOrNa = (v) => {
      if (v === null || v === undefined || Number.isNaN(Number(v))) return "n/a";
      const num = Number(v);
      return `${num > 0 ? "+" : ""}${fmtNumber1.format(num)}%`;
    };
    const directionMeta = (driverRow, fallbackVal) => {
      const raw = (driverRow && driverRow.direction) || null;
      if (raw === "up" || raw === "down" || raw === "flat") return raw;
      const value = asNum(fallbackVal) || 0;
      if (value > 0) return "up";
      if (value < 0) return "down";
      return "flat";
    };

    const toDriverRows = (metricBlock = {}) => {
      if (Array.isArray(metricBlock.drivers) && metricBlock.drivers.length) return metricBlock.drivers;
      const total = asNum(metricBlock.delta);
      const fallback = [
        { driver: "Price", key: "price_effect", delta: asNum(metricBlock.price_effect) },
        { driver: "Volume", key: "volume_effect", delta: asNum(metricBlock.volume_effect) },
        { driver: "Mix", key: "mix_effect", delta: asNum(metricBlock.mix_effect) },
      ];
      return fallback.map((row) => ({
        ...row,
        share_of_delta_pct: total && Math.abs(total) > 0 ? (Number(row.delta || 0) / total) * 100 : null,
        direction: directionMeta(row, row.delta),
      }));
    };

    const renderRows = (metricBlock = {}) => {
      const rows = toDriverRows(metricBlock);
      return rows
        .map((row) => {
          const val = asNum(row.delta);
          const share = row.share_of_delta_pct === null || row.share_of_delta_pct === undefined ? null : Number(row.share_of_delta_pct);
          const direction = directionMeta(row, val);
          const icon = direction === "up" ? "▲" : direction === "down" ? "▼" : "•";
          const cls = direction === "up" ? "text-success" : direction === "down" ? "text-danger" : "text-muted";
          const title = row.definition || definitions[String(row.driver || "").toLowerCase()] || "";
          return `
            <tr>
              <td title="${title}">${row.driver || "Driver"}</td>
              <td class="text-end ${cls}">${val === null ? "n/a" : formatByFmt("currency", val)}</td>
              <td class="text-end ${cls}">${share === null ? "n/a" : signedPctOrNa(share)}</td>
              <td class="text-end ${cls}">${icon}</td>
            </tr>`;
        })
        .join("");
    };

    const contextText = (periodKey, periodBlock, metricBlock) => {
      const previous = asNum(metricBlock.previous);
      const current = asNum(metricBlock.current);
      const delta = asNum(metricBlock.delta);
      let deltaPct = asNum(metricBlock.delta_pct);
      if (deltaPct === null && previous !== null && previous !== 0 && delta !== null) {
        deltaPct = (delta / Math.abs(previous)) * 100;
      }
      const compareLabel = periodBlock?.comparison_label || (periodKey === "mom" ? primaryCompareLabel(windowMeta) : "Same period last year");
      const window = periodBlock?.window || {};
      const priorRange = window.prior_start && window.prior_end ? F.dayRange(window.prior_start, window.prior_end) : compareLabel;
      return `${compareLabel}: ${formatByFmt("currency", previous)} -> ${formatByFmt("currency", current)} (Δ ${formatByFmt("currency", delta)}, Δ ${pctOrNa(deltaPct)}) - ${priorRange}`;
    };

    const renderContribTable = (rows = [], unitLabel = "Unit value") => {
      if (!Array.isArray(rows) || !rows.length) {
        return '<div class="text-muted small">No contributor breakdown available.</div>';
      }
      const body = rows
        .slice(0, 5)
        .map((r) => {
          const skuLabel = r.sku || r.sku_id || "Unknown";
          const skuMeta = r.sku_id && r.sku_id !== skuLabel ? `<div class="text-muted small">${r.sku_id}</div>` : "";
          return `
          <tr>
            <td class="text-truncate" title="${skuLabel}"><div>${skuLabel}</div>${skuMeta}</td>
            <td class="text-end">${r.contribution === null || r.contribution === undefined ? "n/a" : formatByFmt("currency", r.contribution)}</td>
            <td class="text-end">${r.current_qty === null || r.current_qty === undefined ? "n/a" : formatByFmt("number", r.current_qty)}</td>
            <td class="text-end">${r.prior_qty === null || r.prior_qty === undefined ? "n/a" : formatByFmt("number", r.prior_qty)}</td>
            <td class="text-end">${r.current_unit === null || r.current_unit === undefined ? "n/a" : formatByFmt("currency", r.current_unit)}</td>
            <td class="text-end">${r.prior_unit === null || r.prior_unit === undefined ? "n/a" : formatByFmt("currency", r.prior_unit)}</td>
          </tr>`;
        })
        .join("");
      return `
        <div class="table-responsive">
          <table class="table table-sm align-middle mb-2">
            <thead class="table-light">
              <tr>
                <th>SKU</th>
                <th class="text-end">Contribution</th>
                <th class="text-end">Current Qty</th>
                <th class="text-end">Prior Qty</th>
                <th class="text-end">Current ${unitLabel}</th>
                <th class="text-end">Prior ${unitLabel}</th>
              </tr>
            </thead>
            <tbody>${body}</tbody>
          </table>
        </div>`;
    };

    const buildDetailsSection = (periodKey, periodBlock = {}, metricBlock = {}) => {
      const top = metricBlock.top_contributors || {};
      const unitLabel = metricBlock.unit_label || (metric === "profit" ? "Unit margin" : "Unit price");
      const sectionLabel = periodBlock.label || (periodKey === "mom" ? "Primary comparison" : "YoY");
      return `
        <div class="mb-3">
          <div class="fw-semibold mb-2">${sectionLabel} top contributors</div>
          <div class="drivers-contrib-grid">
            <div>
              <div class="text-muted small mb-1">Price</div>
              ${renderContribTable(top.price_effect || [], unitLabel)}
            </div>
            <div>
              <div class="text-muted small mb-1">Volume</div>
              ${renderContribTable(top.volume_effect || [], unitLabel)}
            </div>
            <div>
              <div class="text-muted small mb-1">Mix</div>
              ${renderContribTable(top.mix_effect || [], unitLabel)}
            </div>
          </div>
        </div>`;
    };

    const momBlock = drivers.mom || {};
    const yoyBlock = drivers.yoy || {};
    const momMetric = getMetricBlock(momBlock);
    const yoyMetric = getMetricBlock(yoyBlock);
    const momHasData = asNum(momMetric.delta) !== null;
    const yoyHasData = asNum(yoyMetric.delta) !== null;
    const hasData = momHasData || yoyHasData;

    if (!hasData) {
      if (els.driversEmpty) els.driversEmpty.classList.remove("d-none");
      if (els.driversMomRows) els.driversMomRows.innerHTML = "";
      if (els.driversYoyRows) els.driversYoyRows.innerHTML = "";
      if (els.driversMomContext) els.driversMomContext.textContent = "Not enough data.";
      if (els.driversYoyContext) els.driversYoyContext.textContent = "Not enough data.";
      if (els.driversMomInsight) els.driversMomInsight.textContent = "";
      if (els.driversYoyInsight) els.driversYoyInsight.textContent = "";
      if (els.driversDetailsContent) {
        els.driversDetailsContent.innerHTML = '<div class="text-muted">Top contributor details are available when decomposition data is present.</div>';
      }
      return;
    }

    if (els.driversEmpty) els.driversEmpty.classList.add("d-none");
    if (els.driversMomRows) els.driversMomRows.innerHTML = renderRows(momMetric);
    if (els.driversYoyRows) els.driversYoyRows.innerHTML = renderRows(yoyMetric);
    if (els.driversMomContext) els.driversMomContext.textContent = contextText("mom", momBlock, momMetric);
    if (els.driversYoyContext) els.driversYoyContext.textContent = contextText("yoy", yoyBlock, yoyMetric);
    if (els.driversMomDeltaPct) els.driversMomDeltaPct.textContent = pctOrNa(asNum(momMetric.delta_pct));
    if (els.driversYoyDeltaPct) els.driversYoyDeltaPct.textContent = pctOrNa(asNum(yoyMetric.delta_pct));
    if (els.driversMomInsight) {
      els.driversMomInsight.textContent = momMetric.insight || momBlock.message || "No primary comparison narrative available.";
    }
    if (els.driversYoyInsight) {
      els.driversYoyInsight.textContent = yoyMetric.insight || yoyBlock.message || "No YoY narrative available.";
    }

    const coverage = drivers.coverage || {};
    const coverageParts = [];
    if (coverage.cost_pct !== null && coverage.cost_pct !== undefined) {
      coverageParts.push(`Cost coverage: ${pctOrNa(coverage.cost_pct)}`);
    }
    coverageParts.push(primaryComparisonNote(windowMeta));
    if (drivers.enabled) coverageParts.push("Method: symmetric SKU-level decomposition");
    else coverageParts.push("Method: legacy aggregate decomposition");
    if (els.driversCoverage) {
      els.driversCoverage.textContent = coverageParts.join(" | ");
    }

    const formulas = Array.isArray(method.formulas) ? method.formulas : [];
    const formulaHtml = formulas.length
      ? `<ul class="mb-2">${formulas.map((f) => `<li>${f}</li>`).join("")}</ul>`
      : '<div class="text-muted mb-2">Formula detail unavailable for this decomposition mode.</div>';
    const defsHtml = `
      <div class="mb-2">
        <div><strong>Price:</strong> ${definitions.price || "Unit-value effect at average quantity."}</div>
        <div><strong>Volume:</strong> ${definitions.volume || "Quantity effect at average unit value."}</div>
        <div><strong>Mix:</strong> ${definitions.mix || "Residual reconciliation term."}</div>
      </div>`;
    if (els.driversDetailsContent) {
      els.driversDetailsContent.innerHTML = `
        <div class="text-muted mb-2">${method.name || "Driver methodology"} · Grain ${method.grain || "SKU"}</div>
        ${defsHtml}
        ${formulaHtml}
        ${buildDetailsSection("mom", momBlock, momMetric)}
        ${buildDetailsSection("yoy", yoyBlock, yoyMetric)}
      `;
    }
  };

  const renderConcentration = (concentration = {}) => {
    if (!els.concentrationPanel) return;
    const cust = concentration.customer || {};
    const prod = concentration.product || {};
    const hasCust = cust.top1_share !== null && cust.top1_share !== undefined;
    const hasProd = prod.top1_share !== null && prod.top1_share !== undefined;
    if (!hasCust && !hasProd) {
      els.concentrationPanel.textContent = "No concentration data available.";
      return;
    }
    const block = (label, data, kind) => {
      const topLabel = data.top1_label || "Top contributor";
      const topLink = buildDrillLink(kind, data.top1_entity_id || data.top1_label || "");
      const topHtml = topLink
        ? `<a href="${topLink}" class="text-decoration-none">${topLabel}</a>`
        : topLabel;
      return `
      <div class="mb-3">
        <div class="text-muted small d-flex align-items-center gap-1">${label}
          <i class="bi bi-info-circle text-muted" data-bs-toggle="tooltip" title="Top 1/Top 5 share of revenue; HHI is a 0-10,000 concentration index."></i>
        </div>
        <div class="score-value">${data.top1_share === null || data.top1_share === undefined ? "n/a" : formatByFmt("percent", data.top1_share)}</div>
        <div class="text-muted small">Top entity ${topHtml}</div>
        <div class="text-muted small">Top 5 ${data.top5_share === null || data.top5_share === undefined ? "n/a" : formatByFmt("percent", data.top5_share)}</div>
        <div class="text-muted small">HHI ${data.hhi === null || data.hhi === undefined ? "n/a" : formatByFmt("number", data.hhi)} · ${data.risk_label || "n/a"}</div>
      </div>`;
    };
    els.concentrationPanel.innerHTML = `${block("Customers", cust, "customer")}${block("Products", prod, "product")}`;
    if (typeof bootstrap !== "undefined" && bootstrap.Tooltip) {
      els.concentrationPanel.querySelectorAll('[data-bs-toggle="tooltip"]').forEach((el) => new bootstrap.Tooltip(el));
    }
  };

  const getRiskPayload = () => state.insights.data || state.payload || {};

  const renderProfitability = (profitability = {}) => {
    if (!els.profitabilityPanel) return;
    const stats = profitability.margin_pct || {};
    const message = profitability.message ? `<div class="text-muted small mb-2">${profitability.message}</div>` : "";
    const coverage = profitability.coverage || {};
    const minimumMargin = asNumber(profitability.minimum_margin_pct);
    const targetMargin = asNumber(profitability.target_margin_pct);
    const covText =
      coverage.cost_pct !== null && coverage.cost_pct !== undefined
        ? `<div class="text-muted small mb-2">Cost coverage: ${formatByFmt("percent", coverage.cost_pct)}</div>`
        : "";
    const targetText =
      minimumMargin === null && targetMargin === null
        ? ""
        : `<div class="text-muted small mb-2">Weighted target ${targetMargin === null ? "n/a" : formatByFmt("percent", targetMargin)} · minimum ${minimumMargin === null ? "n/a" : formatByFmt("percent", minimumMargin)}</div>`;
    if (!stats || (stats.p10 === undefined && stats.p50 === undefined && stats.p90 === undefined)) {
      els.profitabilityPanel.innerHTML = `${message}${covText}${targetText}<div class="text-muted">No profitability distribution available.</div>`;
    } else {
      els.profitabilityPanel.innerHTML = `
        ${message}${covText}${targetText}
        <div class="mini-list">
          <div class="mb-1">P10 <strong>${stats.p10 === null || stats.p10 === undefined ? "n/a" : formatByFmt("percent", stats.p10)}</strong></div>
          <div class="mb-1">P50 <strong>${stats.p50 === null || stats.p50 === undefined ? "n/a" : formatByFmt("percent", stats.p50)}</strong></div>
          <div class="mb-1">P90 <strong>${stats.p90 === null || stats.p90 === undefined ? "n/a" : formatByFmt("percent", stats.p90)}</strong></div>
          <div class="text-muted small">Below 0%: ${stats.below_zero === null || stats.below_zero === undefined ? "n/a" : formatByFmt("number", stats.below_zero)} | Above 50%: ${stats.above_fifty === null || stats.above_fifty === undefined ? "n/a" : formatByFmt("number", stats.above_fifty)}</div>
        </div>`;
    }
    const risks = Array.isArray(profitability.margin_risk) ? profitability.margin_risk : [];
    if (els.marginRiskSummary) {
      const belowZero = stats && stats.below_zero !== undefined ? Number(stats.below_zero || 0) : null;
      const aboveFifty = stats && stats.above_fifty !== undefined ? Number(stats.above_fifty || 0) : null;
      const parts = [`${fmtNumber0.format(marginRiskCount(profitability))} margin risk items`];
      if (targetMargin !== null) parts.push(`Target ${formatByFmt("percent", targetMargin)}`);
      if (minimumMargin !== null) parts.push(`Min ${formatByFmt("percent", minimumMargin)}`);
      if (belowZero !== null) parts.push(`Negative margin: ${fmtNumber0.format(belowZero)}`);
      if (aboveFifty !== null) parts.push(`Above 50% margin: ${fmtNumber0.format(aboveFifty)}`);
      els.marginRiskSummary.textContent = parts.join(" | ");
    }
    if (els.negativeMarginSupplierFilter) {
      const selected = els.negativeMarginSupplierFilter.value;
      const suppliers = Array.from(new Set(risks.map((r) => r.supplier || "Unknown"))).sort();
      els.negativeMarginSupplierFilter.innerHTML = '<option value="">All suppliers</option>' + suppliers.map((s) => `<option value="${s}">${s}</option>`).join("");
      if (suppliers.includes(selected)) els.negativeMarginSupplierFilter.value = selected;
    }
    if (els.negativeMarginProteinFilter) {
      const selected = els.negativeMarginProteinFilter.value;
      const proteins = Array.from(new Set(risks.map((r) => r.protein || "Unknown"))).sort();
      els.negativeMarginProteinFilter.innerHTML = '<option value="">All departments</option>' + proteins.map((p) => `<option value="${p}">${p}</option>`).join("");
      if (proteins.includes(selected)) els.negativeMarginProteinFilter.value = selected;
    }

    if (els.marginRiskList) {
      const supplierFilter = els.negativeMarginSupplierFilter ? els.negativeMarginSupplierFilter.value : "";
      const proteinFilter = els.negativeMarginProteinFilter ? els.negativeMarginProteinFilter.value : "";
      const filtered = risks
        .filter((r) => (!supplierFilter || (r.supplier || "Unknown") === supplierFilter))
        .filter((r) => (!proteinFilter || (r.protein || "Unknown") === proteinFilter))
        .sort((a, b) => Number(a.profit_impact || 0) - Number(b.profit_impact || 0))
        .slice(0, 10);

      if (!filtered.length) {
        els.marginRiskList.innerHTML = '<li class="text-muted">No margin risks detected.</li>';
      } else {
        els.marginRiskList.innerHTML = filtered
          .map((r) => {
            const link = buildDrillLink("product", r.entity_id || r.label);
            const label = r.label || "Unknown";
            const risk = r.risk ? r.risk.replace(/_/g, " ") : "risk";
            const impact = Number(r.profit_impact || 0);
            const rowStatusKey = deriveMarginStatusKey(r.margin_pct, r.minimum_margin_pct, r.target_margin_pct, r.status_key);
            const marginContext = marginTargetSummary({
              margin_pct: r.margin_pct,
              minimum_margin_pct: r.minimum_margin_pct,
              target_margin_pct: r.target_margin_pct,
              status_key: rowStatusKey,
            });
            return `<li>
              <div class="d-flex justify-content-between align-items-start gap-2">
                <span><strong>${link ? `<a href="${link}" class="text-decoration-none">${label}</a>` : label}</strong> <span class="text-muted">(${r.supplier || "Unknown"} / ${r.protein || "Unknown"})</span></span>
                <span class="text-muted">${formatByFmt("currency", impact)}</span>
              </div>
              <div class="text-muted small mt-1">${marginStatusBadgeHtml(rowStatusKey, r.target_status)} <span>${escapeHtml(marginContext)}</span></div>
              <div class="text-muted small mt-1">${risk} exposure${r.revenue === null || r.revenue === undefined ? "" : ` · revenue ${formatByFmt("currency", r.revenue)}`}</div>
            </li>`;
          })
          .join("");
      }
    }
    if (els.marginRiskDrilldownLink) {
      els.marginRiskDrilldownLink.href = buildDrilldownUrl("margin_risk");
    }
  };

  const renderCustomerMomentum = (ops = {}) => {
    if (!els.customerMomentum) return;
    const windowMeta = getWindowMeta();
    const c = ops.customers || {};
    const a = ops.activity || {};
    const current = c.current || 0;
    const prev = c.previous || 0;
    const newShare = current ? (c.new / current) * 100 : null;
    const returningShare = current ? (c.returning / current) * 100 : null;
    const prevShare = prev ? (c.new_prev / prev) * 100 : null;
    const shareDelta = newShare !== null && prevShare !== null ? newShare - prevShare : null;
    els.customerMomentum.innerHTML = `
      <div class="mb-2">
        <div class="text-muted small">${currentWindowLabel(windowMeta) || "Current window"}</div>
        <div class="fw-semibold">New ${formatByFmt("number", c.new)} | Returning ${formatByFmt("number", c.returning)}</div>
        <div class="text-muted small">Shares: New ${formatByFmt("percent", newShare)} · Returning ${formatByFmt("percent", returningShare)}</div>
        <div class="text-muted small">New-share delta vs ${primaryCompareLabel(windowMeta).toLowerCase()}: ${shareDelta !== null ? formatByFmt("percent", shareDelta) : "n/a"}</div>
      </div>
      <div>
        <div class="text-muted small">Active</div>
        <div class="fw-semibold">Customers ${formatByFmt("number", a.active_customers)} | SKUs ${formatByFmt("number", a.active_skus)}</div>
      </div>`;
  };

  const renderOpsMix = (ops = {}) => {
    if (!els.opsMixPanel) return;
    const mix = ops.mix || {};
    const renderList = (title, rows) => {
      const allItems = Array.isArray(rows) ? rows : [];
      const items = allItems.slice(0, 5);
      if (!items.length) return "";
      const otherShare = allItems.slice(5).reduce((acc, r) => acc + (Number(r.share) || 0), 0);
      if (otherShare > 0.01) {
        items.push({ label: "Other", share: otherShare });
      }
      const list = items
        .map((r) => `<li><span>${r.label || "Unknown"}</span><span>${formatByFmt("percent", r.share)}</span></li>`)
        .join("");
      return `<div class="mb-3"><div class="text-muted small">${title}</div><ul class="list-unstyled mini-list mb-0">${list}</ul></div>`;
    };
    const html = `${renderList("Regions", mix.region)}${renderList("Methods", mix.method)}${renderList("Suppliers", mix.supplier)}${renderList("Departments", mix.protein)}`;
    els.opsMixPanel.innerHTML = html || '<div class="text-muted">No mix data available.</div>';
  };

  const renderWeekday = (ops = {}) => {
    if (!els.weekdayChart) return;
    destroyChart("weekday");
    if (!canRenderCharts()) {
      showChartFallback(els.weekdayChart, "Weekday chart unavailable. Operational mix and momentum remain available.");
      if (els.weekdayEmpty) {
        els.weekdayEmpty.classList.remove("d-none");
        els.weekdayEmpty.textContent = "Weekday visualization unavailable.";
      }
      ensureChartWarning();
      return;
    }
    clearChartFallback(els.weekdayChart);
    const rows = Array.isArray(ops.weekday) ? ops.weekday : [];
    if (!rows.length) {
      if (els.weekdayEmpty) els.weekdayEmpty.classList.remove("d-none");
      els.weekdayChart.classList.add("d-none");
      if (els.weekdayBest) els.weekdayBest.textContent = "";
      return;
    }
    if (els.weekdayEmpty) els.weekdayEmpty.classList.add("d-none");
    els.weekdayChart.classList.remove("d-none");
    const labels = rows.map((r) => r.weekday);
    const values = rows.map((r) => Number(r.revenue) || 0);
    const best = rows.reduce((acc, r) => ((r.revenue || 0) > (acc.revenue || 0) ? r : acc), rows[0]);
    if (els.weekdayBest) els.weekdayBest.textContent = best ? `Best: ${best.weekday}` : "";
    const ctx = els.weekdayChart.getContext("2d");
    charts.weekday = new Chart(ctx, {
      type: "bar",
      data: { labels, datasets: [{ label: "Revenue", data: values, backgroundColor: ChartUtils.seriesFill(1, 0.25), borderColor: ChartUtils.seriesColor(1), borderWidth: 1 }] },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: (context) => `${context.dataset.label}: ${fmtCurrency0.format(context.parsed?.y)}`,
            },
          },
        },
        scales: { y: { beginAtZero: true, ticks: { callback: (v) => fmtCurrency0.format(Number(v) || 0) } } },
      },
    });
  };

  const renderOperations = (ops = {}) => {
    renderCustomerMomentum(ops);
    renderOpsMix(ops);
    renderWeekday(ops);
  };

  const renderInsightsSkeleton = () => {
    if (els.insightsEmpty) els.insightsEmpty.classList.add("d-none");
    if (els.insightsList) {
      els.insightsList.innerHTML = Array.from({ length: 6 })
        .map(
          () => `
          <div class="col-md-6 col-lg-4">
            <div class="insight-item placeholder-glow">
              <div class="insight-title"><span class="placeholder col-6"></span></div>
              <div class="insight-value"><span class="placeholder col-5"></span></div>
              <div class="insight-detail"><span class="placeholder col-7"></span></div>
            </div>
          </div>`
        )
        .join("");
    }
    if (els.driversMomRows) {
      els.driversMomRows.innerHTML = '<tr><td colspan="4" class="text-muted">Loading driver decomposition...</td></tr>';
    }
    if (els.driversYoyRows) {
      els.driversYoyRows.innerHTML = '<tr><td colspan="4" class="text-muted">Loading driver decomposition...</td></tr>';
    }
    if (els.driversMomContext) els.driversMomContext.textContent = "Loading...";
    if (els.driversYoyContext) els.driversYoyContext.textContent = "Loading...";
    if (els.driversMomInsight) els.driversMomInsight.textContent = "";
    if (els.driversYoyInsight) els.driversYoyInsight.textContent = "";
    if (els.driversCoverage) els.driversCoverage.textContent = "";
    if (els.driversDetailsContent) {
      els.driversDetailsContent.innerHTML = '<div class="text-muted">Loading decomposition details...</div>';
    }
    if (els.driversEmpty) els.driversEmpty.classList.add("d-none");
    if (els.concentrationPanel) {
      els.concentrationPanel.innerHTML = '<div class="text-muted small">Loading concentration metrics...</div>';
    }
    if (els.profitabilityPanel) {
      els.profitabilityPanel.innerHTML = '<div class="text-muted small">Loading profitability snapshot...</div>';
    }
    if (els.marginRiskList) {
      els.marginRiskList.innerHTML = '<li class="text-muted">Loading margin risks...</li>';
    }
  };

  const loadInsights = async (qsOverride) => {
    const qs = qsOverride !== undefined ? qsOverride : new URLSearchParams(window.location.search || "").toString();
    if (state.insights.loading) return;
    if (state.insights.lastFilters === qs && state.insights.data) return;
    if (insightsController) insightsController.abort();
    insightsController = new AbortController();
    state.insights.loading = true;
    renderInsightsSkeleton();
    try {
      const { data, notModified } = await fetchJson(buildInsightsUrl(qs), insightsController.signal);
      if (notModified) {
        state.insights.loading = false;
        return;
      }
      state.insights.data = data || null;
      state.insights.error = null;
      state.insights.lastFilters = qs;
      renderAll();
    } catch (err) {
      if (err?.name === "AbortError") return;
      state.insights.error = err && err.message ? err.message : "Unable to load insights.";
      state.insights.data = null;
      renderAll();
      if (els.insightsEmpty) {
        els.insightsEmpty.textContent = state.insights.error;
        els.insightsEmpty.classList.remove("d-none");
      }
    } finally {
      state.insights.loading = false;
    }
  };

  const setForecastStatus = (text, variant = "muted") => {
    if (!els.forecastStatus) return;
    els.forecastStatus.textContent = text || "";
    els.forecastStatus.className = `small text-${variant}`;
  };

  const setForecastLoading = (loading) => {
    state.forecast.loading = loading;
    if (els.forecastRunBtn) {
      els.forecastRunBtn.disabled = loading;
    }
    if (els.forecastSpinner) {
      els.forecastSpinner.classList.toggle("d-none", !loading);
    }
  };

  const renderForecastList = (el, items = [], fallback = "") => {
    if (!el) return;
    const values = Array.isArray(items) ? items.filter(Boolean) : [];
    el.replaceChildren();
    if (!values.length) {
      const li = document.createElement("li");
      li.textContent = fallback;
      el.appendChild(li);
      return;
    }
    values.forEach((text) => {
      const li = document.createElement("li");
      li.textContent = text;
      el.appendChild(li);
    });
  };

  const forecastValueClass = (value) => {
    const token = String(value || "").trim().toLowerCase();
    if (!token) return "";
    return `forecast-value-${token.replace(/\s+/g, "-")}`;
  };

  const renderForecastMeta = (data = {}) => {
    const warnings = Array.isArray(data.warnings) ? data.warnings.filter(Boolean) : [];
    const notes = Array.isArray(data.notes) ? data.notes.filter(Boolean) : [];
    const model = data.model || {};
    const diagnostics = data.diagnostics || {};
    const partial = diagnostics.partial_period || {};
    const summary = data.summary || model.selection_reason || "Forecast summary will appear here after the model runs.";
    const historyPoints = model.train_points ?? diagnostics.train_points ?? diagnostics.history_basis_points ?? diagnostics.history_points ?? 0;
    const availableHistoryPoints = diagnostics.available_history_points ?? diagnostics.history_points ?? historyPoints ?? 0;
    const historyStart = diagnostics.history_basis_start || diagnostics.history_start || null;
    const historyEnd = diagnostics.history_basis_end || diagnostics.history_end || null;
    const availableHistoryStart = diagnostics.available_history_start || diagnostics.history_start || null;
    const availableHistoryEnd = diagnostics.available_history_end || diagnostics.history_end || null;
    const trainingCutoff = diagnostics.training_cutoff || null;
    const qualityScore = asNumber(model.quality_score);
    const forecastability = asNumber(model.forecastability_score ?? diagnostics.forecastability_score);
    const smape = asNumber(model.smape);
    const mape = asNumber(model.mape);
    const wape = asNumber(model.wape);
    const bias = asNumber(model.bias);
    const hitRate = asNumber(model.hit_rate_pct);
    const dirAcc = asNumber(model.directional_accuracy);
    const historyExcludedPoints = asNumber(diagnostics.history_excluded_points);
    const historyBasisLabel = diagnostics.history_basis_label || "Comparable history";
    const historyBasisReason = diagnostics.history_basis_reason || "";
    const historyNonZeroShare = asNumber(diagnostics.history_non_zero_share_pct);
    const confidenceBadge = model.confidence_badge || data.confidence_badge || "Watch";
    const runnerUps = Array.isArray(model.runner_ups) ? model.runner_ups : [];
    const statusWarning = warnings[0] || (data.reason && !data.eligible ? data.reason : "");
    const noteItems = [...new Set([...warnings, ...notes])].slice(0, 5);

    if (els.forecastSummary) {
      els.forecastSummary.textContent = summary;
    }
    if (els.forecastModelValue) {
      els.forecastModelValue.textContent = model.display_name || model.name || "-";
    }
    if (els.forecastModelDetail) {
      const modelBits = [];
      if (model.selection_reason) modelBits.push(model.selection_reason);
      if (model.history_mode) modelBits.push(`History mode ${String(model.history_mode).replace(/_/g, " ")}`);
      if (model.candidate_count) modelBits.push(`${fmtNumber0.format(Number(model.candidate_count) || 0)} candidate models scored`);
      els.forecastModelDetail.textContent = modelBits.join(" • ") || "Awaiting forecast run.";
    }
    if (els.forecastConfidenceValue) {
      els.forecastConfidenceValue.textContent = confidenceBadge;
      els.forecastConfidenceValue.className = `forecast-signal-value ${forecastValueClass(confidenceBadge)}`.trim();
    }
    if (els.forecastConfidenceDetail) {
      const confBits = [];
      if (forecastability !== null) confBits.push(`Forecastability ${fmtNumber0.format(forecastability)}/100`);
      if (model.validation_windows) confBits.push(`${fmtNumber0.format(Number(model.validation_windows) || 0)} validation windows`);
      if (partial.detected) confBits.push(partial.included ? "Partial month included" : "Partial month excluded");
      els.forecastConfidenceDetail.textContent = confBits.join(" • ") || "Confidence and forecastability will appear here.";
    }
    if (els.forecastQualityValue) {
      els.forecastQualityValue.textContent = mape !== null ? `MAPE ${fmtNumber1.format(mape)}%` : (qualityScore !== null ? `${fmtNumber0.format(qualityScore)}/100` : "-");
    }
    if (els.forecastQualityDetail) {
      const qualityBits = [];
      if (bias !== null) qualityBits.push(`Bias ${bias >= 0 ? "+" : ""}${fmtNumber1.format(bias)}`);
      if (hitRate !== null) qualityBits.push(`Within ±10% ${fmtNumber1.format(hitRate)}%`);
      if (smape !== null) qualityBits.push(`SMAPE ${fmtNumber1.format(smape)}%`);
      if (wape !== null) qualityBits.push(`WAPE ${fmtNumber1.format(wape)}%`);
      if (dirAcc !== null) qualityBits.push(`Direction ${fmtNumber0.format(dirAcc)}%`);
      els.forecastQualityDetail.textContent = qualityBits.join(" • ") || "Validation metrics will appear here.";
    }
    if (els.forecastHistoryValue) {
      let label = "-";
      if (historyPoints) {
        label = `${fmtNumber0.format(historyPoints)} selected`;
        if (availableHistoryPoints && Number(availableHistoryPoints) !== Number(historyPoints)) {
          label = `${label} / ${fmtNumber0.format(availableHistoryPoints)} available`;
        }
      }
      els.forecastHistoryValue.textContent = label;
      els.forecastHistoryValue.title = historyPoints ? `${fmtNumber0.format(historyPoints)} training month${Number(historyPoints) === 1 ? "" : "s"} used by the selected model.` : "";
    }
    if (els.forecastHistoryDetail) {
      const historyBits = [];
      historyBits.push(historyBasisLabel);
      if (historyStart || historyEnd) historyBits.push(`Selected ${F.dayRange(historyStart, historyEnd, { missing: "?" })}`);
      if ((availableHistoryStart || availableHistoryEnd) && (availableHistoryStart !== historyStart || availableHistoryEnd !== historyEnd)) {
        historyBits.push(`Available ${F.dayRange(availableHistoryStart, availableHistoryEnd, { missing: "?" })}`);
      }
      if (trainingCutoff) historyBits.push(`Cutoff ${F.day(trainingCutoff)}`);
      if (historyExcludedPoints) historyBits.push(`${fmtNumber0.format(historyExcludedPoints)} older month${Number(historyExcludedPoints) === 1 ? "" : "s"} excluded`);
      els.forecastHistoryDetail.textContent = historyBits.join(" • ") || "Training window and cutoff details will appear here.";
    }
    if (els.forecastBasisText) {
      const basisBits = [];
      if (historyPoints) basisBits.push(`${fmtNumber0.format(historyPoints)} month${Number(historyPoints) === 1 ? "" : "s"} selected for training`);
      if (availableHistoryPoints && Number(availableHistoryPoints) > Number(historyPoints || 0)) {
        basisBits.push(`${fmtNumber0.format(availableHistoryPoints)} comparable month${Number(availableHistoryPoints) === 1 ? "" : "s"} available`);
      }
      if (model.history_mode) basisBits.push(`Model window ${String(model.history_mode).replace(/_/g, " ")}`);
      if (diagnostics.seasonality_strength_score !== undefined && diagnostics.seasonality_strength_score !== null) {
        basisBits.push(`Seasonality ${fmtNumber0.format(Number(diagnostics.seasonality_strength_score) || 0)}/100`);
      }
      if (historyNonZeroShare !== null && historyNonZeroShare < 80) {
        basisBits.push(`Non-zero months ${fmtNumber0.format(historyNonZeroShare)}%`);
      }
      if (diagnostics.level_shift_detected) basisBits.push("Recent regime shift detected");
      if (partial.note) basisBits.push(partial.note);
      if (historyBasisReason) basisBits.push(historyBasisReason);
      els.forecastBasisText.textContent = basisBits.join(" • ") || "Forecast basis will explain the history used, selected window mode, and partial-period treatment.";
    }

    renderForecastList(
      els.forecastNotesList,
      noteItems,
      "Forecast notes, caveats, and quality warnings will appear here."
    );
    renderForecastList(
      els.forecastRunnerUpsList,
      runnerUps.map((item) => {
        const parts = [];
        const label = item.display_name || item.name || "Runner-up";
        parts.push(label);
        if (item.smape !== null && item.smape !== undefined) parts.push(`SMAPE ${fmtNumber1.format(Number(item.smape) || 0)}%`);
        if (item.history_mode) parts.push(String(item.history_mode).replace(/_/g, " "));
        return parts.join(" • ");
      }),
      "Run a forecast to compare model alternatives."
    );

    if (statusWarning) {
      setForecastStatus(statusWarning, data.eligible === false ? "danger" : "warning");
    } else if ((data.series && data.series.length) || (data.forecast && data.forecast.length)) {
      const hitText = data.cache_hit ? "from cache" : "fresh";
      setForecastStatus(`Forecast ready (${hitText})`, "success");
    }
  };

  const renderForecastChart = (data = {}) => {
    if (!els.forecastChart) return;
    destroyChart("forecast");
    if (!canRenderCharts()) {
      showChartFallback(els.forecastChart, "Forecast chart unavailable. Model status and confidence details still update.");
      if (els.forecastEmpty) {
        els.forecastEmpty.classList.remove("d-none");
        els.forecastEmpty.textContent = "Forecast visualization is temporarily unavailable.";
      }
      ensureChartWarning();
      return;
    }
    clearChartFallback(els.forecastChart);
    const toLabel = (ds) => (ds ? String(ds).slice(0, 7) : "");
    let labels = [];
    let actual = [];
    let forecast = [];
    let lower = [];
    let upper = [];

    const history = Array.isArray(data.history) ? data.history : null;
    const fc = Array.isArray(data.forecast) ? data.forecast : null;
    if ((history && history.length) || (fc && fc.length)) {
      const actualMap = new Map();
      const forecastMap = new Map();
      const lowerMap = new Map();
      const upperMap = new Map();
      if (history) {
        history.forEach((pt) => {
          const label = toLabel(pt.ds);
          if (!label) return;
          const val = pt.y === null || pt.y === undefined ? null : Number(pt.y);
          actualMap.set(label, Number.isFinite(val) ? val : null);
        });
      }
      if (fc) {
        fc.forEach((pt) => {
          const label = toLabel(pt.ds);
          if (!label) return;
          const yhat = pt.yhat === null || pt.yhat === undefined ? null : Number(pt.yhat);
          const lo = pt.yhat_lower === null || pt.yhat_lower === undefined ? null : Number(pt.yhat_lower);
          const hi = pt.yhat_upper === null || pt.yhat_upper === undefined ? null : Number(pt.yhat_upper);
          forecastMap.set(label, Number.isFinite(yhat) ? yhat : null);
          lowerMap.set(label, Number.isFinite(lo) ? lo : null);
          upperMap.set(label, Number.isFinite(hi) ? hi : null);
        });
      }
      const labelSet = new Set([...actualMap.keys(), ...forecastMap.keys()]);
      labels = Array.from(labelSet).filter(Boolean).sort();
      actual = labels.map((l) => actualMap.get(l) ?? null);
      forecast = labels.map((l) => forecastMap.get(l) ?? null);
      lower = labels.map((l) => lowerMap.get(l) ?? null);
      upper = labels.map((l) => upperMap.get(l) ?? null);
    } else {
      const points = Array.isArray(data.series) ? data.series : [];
      labels = points.map((p) => {
        if (p.month) return String(p.month).slice(0, 7);
        if (p.t) return String(p.t).slice(0, 7);
        return "";
      });
      actual = points.map((p) => (p.actual === null || p.actual === undefined ? null : Number(p.actual)));
      forecast = points.map((p) => {
        if (p.yhat !== null && p.yhat !== undefined) return Number(p.yhat);
        if (p.forecast !== null && p.forecast !== undefined) return Number(p.forecast);
        return null;
      });
      lower = points.map((p) => {
        if (p.yhat_lower !== null && p.yhat_lower !== undefined) return Number(p.yhat_lower);
        if (p.lo !== null && p.lo !== undefined) return Number(p.lo);
        return null;
      });
      upper = points.map((p) => {
        if (p.yhat_upper !== null && p.yhat_upper !== undefined) return Number(p.yhat_upper);
        if (p.hi !== null && p.hi !== undefined) return Number(p.hi);
        return null;
      });
    }

    if (!labels.length) {
      if (els.forecastEmpty) els.forecastEmpty.classList.remove("d-none");
      els.forecastChart.classList.add("d-none");
      return;
    }
    if (els.forecastEmpty) els.forecastEmpty.classList.add("d-none");
    els.forecastChart.classList.remove("d-none");

    const hasBand = upper.some((v) => v !== null && v !== undefined) && lower.some((v) => v !== null && v !== undefined);
    const yTick = (v) => {
      if (state.forecast.metric === "margin") return fmtPercent1(Number(v) || 0);
      return fmtCurrency0.format(Number(v) || 0);
    };
    const yScale = { beginAtZero: state.forecast.metric === "revenue" || state.forecast.metric === "profit", ticks: { callback: (v) => yTick(v) } };
    if (state.forecast.metric === "margin") {
      const allVals = actual.concat(forecast).filter((v) => v !== null && v !== undefined && Number.isFinite(v));
      const minVal = allVals.length ? Math.min(...allVals) : -10;
      const maxVal = allVals.length ? Math.max(...allVals) : 10;
      yScale.suggestedMin = Math.max(-100, Math.min(minVal, -10));
      yScale.suggestedMax = Math.min(100, Math.max(maxVal, 10));
    }

    const datasets = [
      { type: "line", label: "Actual", data: actual, borderColor: ChartUtils.seriesColor(1), backgroundColor: ChartUtils.seriesFill(1, 0.12), tension: 0.25 },
      { type: "line", label: "Forecast", data: forecast, borderColor: ChartUtils.seriesColor(4), backgroundColor: ChartUtils.seriesFill(4, 0.08), borderDash: [6, 4], tension: 0.25 },
    ];
    if (hasBand) {
      datasets.push({
        type: "line",
        label: "Lower CI",
        data: lower,
        borderColor: ChartUtils.seriesFill(4, 0.01),
        backgroundColor: ChartUtils.seriesFill(4, 0.08),
        pointRadius: 0,
        fill: false,
        tension: 0.25,
      });
      datasets.push({
        type: "line",
        label: "Upper CI",
        data: upper,
        borderColor: ChartUtils.seriesFill(4, 0.01),
        backgroundColor: ChartUtils.seriesFill(4, 0.08),
        pointRadius: 0,
        fill: "-1",
        tension: 0.25,
      });
    }

    const ctx = els.forecastChart.getContext("2d");
    charts.forecast = new Chart(ctx, {
      data: { labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        scales: {
          y: yScale,
        },
        plugins: {
          legend: { display: true, position: "bottom" },
          tooltip: {
            callbacks: {
              label: (context) => {
                const value = context.parsed?.y;
                if (value === null || value === undefined) return `${context.dataset.label}: n/a`;
                const formatted = state.forecast.metric === "margin" ? fmtPercent1(value) : fmtCurrency0.format(value);
                return `${context.dataset.label}: ${formatted}`;
              },
            },
          },
        },
      },
    });
  };

  const renderForecastState = (data = state.forecast.data || {}) => {
    renderForecastChart(data);
    renderForecastMeta(data);
    const hasVisibleForecast =
      !!(Array.isArray(data?.forecast) && data.forecast.length) ||
      !!(Array.isArray(data?.series) && data.series.some((row) => row && (row.forecast !== null && row.forecast !== undefined)));
    const hasNowcast = Boolean(data?.nowcast?.applied);
    if (els.forecastFiltersNotice) {
      els.forecastFiltersNotice.classList.toggle("d-none", !state.forecast.stale);
    }
    if (state.forecast.stale) {
      setForecastStatus(
        isDefaultForecastSelection() ? "Filters changed - default forecast refresh is available." : "Filters changed - refresh forecast to realign outlook.",
        "warning"
      );
    } else if (data && data.error) {
      setForecastStatus(data.error, "warning");
    } else if (data && data.eligible === false) {
      if (hasNowcast || hasVisibleForecast) {
        setForecastStatus("Current month estimate ready. Forward forecast remains limited by history depth.", "warning");
        if (els.forecastEmpty) {
          els.forecastEmpty.classList.add("d-none");
        }
      } else if (els.forecastEmpty) {
        setForecastStatus(data.reason || "Forecast unavailable for the current filters.", "warning");
        els.forecastEmpty.classList.remove("d-none");
        els.forecastEmpty.textContent = data.reason || "Forecast unavailable for the current filters.";
      }
    } else if (!data || (!data.series || !data.series.length) && (!data.forecast || !data.forecast.length)) {
      setForecastStatus(
        isDefaultForecastSelection() ? "Preparing smart default forecast for the active business window." : "Refresh forecast to generate predictions for the selected controls.",
        "muted"
      );
      if (els.forecastEmpty) {
        els.forecastEmpty.classList.remove("d-none");
        els.forecastEmpty.textContent = isDefaultForecastSelection()
          ? "The default forecast is preparing for the active window."
          : "Refresh Forecast to generate predictions for the selected metric, horizon, and partial-month treatment.";
      }
    }
  };

  const markForecastStale = () => {
    if (!state.forecast.data || !state.forecast.lastFilters) return;
    const current = window.location.search || "";
    state.forecast.stale = current !== state.forecast.lastFilters;
    if (state.forecast.stale) {
      setForecastStatus(
        isDefaultForecastSelection() ? "Filters changed - default forecast refresh is available." : "Filters changed - refresh forecast to realign outlook.",
        "warning"
      );
    }
    renderForecastState();
  };

  const maybeAutoRunForecast = () => {
    if (state.payload?.meta?.has_data === false) return;
    if (!isDefaultForecastSelection()) return;
    if (state.forecast.loading) return;
    const currentFilters = window.location.search || "";
    if (state.forecast.lastFilters === currentFilters && !state.forecast.stale) return;
    runForecast({ auto: true });
  };

  const runForecast = async ({ auto = false } = {}) => {
    if (!els.forecastRunBtn) return;
    if (state.forecast.loading) return;
    const reqSeq = ++state.forecast.requestSeq;
    const requestFilters = window.location.search || "";
    setForecastLoading(true);
    setForecastStatus(auto ? "Running smart default forecast..." : "Running forecast...", "primary");
    try {
      const resp = await authFetch(buildForecastUrl(), { method: "GET" });
      let data = null;
      try {
        data = await resp.json();
      } catch (err) {
        data = null;
      }
      if (!resp.ok) {
        const message = data && data.error ? data.error : `Forecast request failed (${resp.status})`;
        throw new Error(message);
      }
      if (reqSeq !== state.forecast.requestSeq) return;
      state.forecast.data = data || {};
      state.forecast.lastFilters = requestFilters;
      state.forecast.stale = (window.location.search || "") !== requestFilters;
      renderForecastState(state.forecast.data);
    } catch (err) {
      if (reqSeq !== state.forecast.requestSeq) return;
      setForecastStatus(err && err.message ? `Forecast failed: ${err.message}` : "Forecast failed", "danger");
    } finally {
      if (reqSeq !== state.forecast.requestSeq) return;
      setForecastLoading(false);
      if (state.forecast.stale && auto) maybeAutoRunForecast();
    }
  };

  const resolveLabels = (key, values, meta = {}) => {
    if (meta.filter_labels && Array.isArray(meta.filter_labels[key]) && meta.filter_labels[key].length) {
      return meta.filter_labels[key];
    }
    if (typeof window.getFilterLabels === "function") {
      return window.getFilterLabels(key, values);
    }
    return values || [];
  };

  const renderMeta = (meta = {}) => {
    if (els.lastRefresh) {
      els.lastRefresh.textContent = formatTimestampish(meta.last_refresh);
      if (meta.last_refresh) {
        const refreshAge = formatRefreshAge(meta.refresh_age_days, meta.refresh_age_hours);
        els.lastRefresh.title = `Governed refresh ${formatTimestampish(meta.last_refresh)}${refreshAge !== "n/a" ? ` • age ${refreshAge}` : ""}`;
      } else {
        els.lastRefresh.title = "";
      }
    }
    const w = meta.window || {};
    if (els.dataWindow) {
      els.dataWindow.textContent = currentWindowLabel(w) || (w.start && w.end ? F.dayRange(w.start, w.end) : "Not available");
    }
    if (els.comparisonBasisChip) {
      els.comparisonBasisChip.textContent = primaryCompareLabel(w);
    }
    if (els.periodModeChip) {
      els.periodModeChip.textContent = periodModeLabel(w);
      const statusLabel = emptyText(w.period_status_label, "Filtered period");
      els.periodModeChip.title = `${statusLabel}${w.method_label ? ` • ${w.method_label}` : ""}`;
    }
    if (els.dataCutoffChip) {
      els.dataCutoffChip.textContent = meta.data_cutoff ? formatTimestampish(meta.data_cutoff, { withTime: false }) : "Not available";
      els.dataCutoffChip.title = meta.data_cutoff ? `Latest governed data cutoff ${formatTimestampish(meta.data_cutoff, { withTime: false })}` : "";
    }
    if (els.comparisonNoteText) {
      const priorLabel = priorWindowLabel(w);
      els.comparisonNoteText.textContent = `${primaryComparisonNote(w)}${priorLabel ? ` Comparator window: ${priorLabel}.` : ""}`;
    }
    const filters = meta.filters || {};
    const parts = [];
    /* The window the reader is looking at, in words. This printed
       `2025-10-01T00:00:00 -> 2026-08-09T00:00:00` immediately under the hero,
       and the end date was the *requested* one - 37 days past the last row -
       rather than the window actually analysed. Both are fixed: the label is
       human-readable, and it prefers the clamped window the page computed on. */
    const shownStart = w?.start || filters.start;
    const shownEnd = w?.end || filters.end;
    const dateLabel = shownStart || shownEnd ? F.dayRange(shownStart, shownEnd, { missing: "" }) : null;
    let activeFilterCount = 0;
    if (dateLabel) {
      parts.push(dateLabel);
      if (!meta.defaulted_window) activeFilterCount += 1;
    }
    ["regions", "methods", "customers", "suppliers", "products", "sales_reps"].forEach((key) => {
      const values = resolveLabels(key, filters[key], meta);
      if (Array.isArray(values) && values.length) {
        activeFilterCount += 1;
        parts.push(`${key}: ${values.slice(0, 2).join(", ")}${values.length > 2 ? "..." : ""}`);
      }
    });
    if (els.filterSummary) {
      els.filterSummary.textContent = parts.length ? parts.join(" | ") : "Default (Current FY)";
    }
    if (els.filterCountChip) {
      els.filterCountChip.textContent = `${fmtNumber0.format(activeFilterCount)} active`;
    }
    if (els.scopeModeChip) {
      const scopedKeys = ["customers", "sales_reps", "regions", "suppliers", "products"].filter((key) => Array.isArray(filters[key]) && filters[key].length);
      els.scopeModeChip.textContent = scopedKeys.length ? "Scoped" : "Enterprise";
      els.scopeModeChip.title = scopedKeys.length ? `Scoped by ${scopedKeys.join(", ")}` : "No additional scope filters applied.";
    }
  };

  const safeRender = (name, fn) => {
    try {
      fn();
      return true;
    } catch (err) {
      console.error(`[overview] ${name} render failed`, err);
      return false;
    }
  };

  const renderAll = () => {
    const payload = state.payload || {};
    const meta = payload.meta || {};
    if (els.emptyState) {
      if (meta.has_data === false) {
        setEmptyStateMessage("No data for the selected window or filters.", "info");
      } else {
        clearEmptyStateMessage();
      }
    }
    const failed = [];
    const renderStep = (name, fn) => {
      if (!safeRender(name, fn)) failed.push(name);
    };
    renderStep("meta", () => renderMeta(meta));
    renderStep("scoped-links", () => syncScopedLinks());
    renderStep("executive-summary", () => renderExecutiveSummary(payload));
    renderStep("health-rail", () => renderHealthRail(payload));
    renderStep("executive-scorecard", () => renderExecutiveScorecard(payload));
    renderStep("kpis", () => renderKpis(payload));
    renderStep("trend", () => renderTrend(payload.trend || {}));
    renderStep("mix", () => renderMix(payload.mix || {}, state.dim));
    renderStep("pareto", () => renderPareto(payload.pareto || {}, state.dim));
    renderStep("movers", () => renderTopMovers(payload.top_movers || {}, state.moversDim, state.moversSort));
    renderStep("health", () => renderHealth(payload.health || {}));
    renderStep("coverage-banner", () => applyCoverageBanner(payload.health || {}));
    renderStep("coverage-panel", () => updateGlobalCoveragePanel(payload.health || {}));
    const insightsPayload = state.insights.data || payload;
    renderStep("commercial-focus", () => renderCommercialFocus(payload, insightsPayload));
    renderStep("insights", () => renderInsights(insightsPayload.insights || {}));
    renderStep("drivers", () => renderDrivers(insightsPayload.drivers || {}));
    renderStep("concentration", () => renderConcentration(insightsPayload.concentration || {}));
    renderStep("profitability", () => renderProfitability(insightsPayload.profitability || {}));
    renderStep("operations", () => renderOperations(payload.operations || {}));
    if (failed.length) {
      setBanner(`Some overview sections are in fallback mode (${failed.join(", ")}).`, "warning");
    }
    settleLoadingFallbacks(failed.length ? "partial" : "ready");
    ensureChartWarning();
  };

  const syncControlsFromState = () => {
    if (els.dimToggle) {
      els.dimToggle
        .querySelectorAll("button[data-dim]")
        .forEach((btn) => btn.classList.toggle("active", btn.getAttribute("data-dim") === state.dim));
    }
    if (els.moversDimToggle) {
      els.moversDimToggle
        .querySelectorAll("button[data-movers-dim]")
        .forEach((btn) => btn.classList.toggle("active", btn.getAttribute("data-movers-dim") === state.moversDim));
    }
    if (els.moversSortSelect) els.moversSortSelect.value = state.moversSort || "delta_abs";
    if (els.driversMetricToggle) {
      els.driversMetricToggle
        .querySelectorAll("button[data-driver-metric]")
        .forEach((btn) => btn.classList.toggle("active", btn.getAttribute("data-driver-metric") === state.driverMetric));
    }
    if (els.trendFreqToggle) {
      els.trendFreqToggle
        .querySelectorAll("button[data-trend-freq]")
        .forEach((btn) => btn.classList.toggle("active", btn.getAttribute("data-trend-freq") === state.trend.freq));
    }
    if (els.trendOverlayMetric) els.trendOverlayMetric.value = state.trend.overlay || "profit";
    if (els.trendRollingToggle) els.trendRollingToggle.checked = !!state.trend.rolling;
    if (els.forecastMetricButtons && els.forecastMetricButtons.forEach) {
      els.forecastMetricButtons.forEach((btn) => btn.classList.toggle("active", btn.getAttribute("data-forecast-metric") === state.forecast.metric));
    }
    if (els.forecastHorizon) els.forecastHorizon.value = String(state.forecast.horizon || DEFAULT_FORECAST.horizon);
    if (els.forecastIncludePartial) els.forecastIncludePartial.checked = !!state.forecast.includePartial;
  };

  const snapshotUiState = () => ({
    dim: state.dim,
    moversDim: state.moversDim,
    moversSort: state.moversSort,
    driverMetric: state.driverMetric,
    trend: { ...state.trend },
    forecast: {
      metric: state.forecast.metric,
      horizon: state.forecast.horizon,
      includePartial: !!state.forecast.includePartial,
      data: state.forecast.data,
      lastFilters: state.forecast.lastFilters,
      stale: !!state.forecast.stale,
    },
    insights: {
      data: state.insights.data,
      lastFilters: state.insights.lastFilters,
      error: state.insights.error,
    },
  });

  const applySnapshotUiState = (uiState = {}) => {
    if (!uiState || typeof uiState !== "object") return;
    if (uiState.dim) state.dim = String(uiState.dim);
    if (uiState.moversDim) state.moversDim = String(uiState.moversDim);
    if (uiState.moversSort) state.moversSort = String(uiState.moversSort);
    if (uiState.driverMetric) state.driverMetric = String(uiState.driverMetric);
    if (uiState.trend && typeof uiState.trend === "object") {
      state.trend = {
        freq: uiState.trend.freq || state.trend.freq,
        overlay: uiState.trend.overlay || state.trend.overlay,
        rolling: uiState.trend.rolling !== undefined ? !!uiState.trend.rolling : state.trend.rolling,
      };
    }
    if (uiState.forecast && typeof uiState.forecast === "object") {
      state.forecast.metric = uiState.forecast.metric || state.forecast.metric;
      state.forecast.horizon = uiState.forecast.horizon || state.forecast.horizon;
      state.forecast.includePartial = uiState.forecast.includePartial !== undefined ? !!uiState.forecast.includePartial : state.forecast.includePartial;
      state.forecast.data = uiState.forecast.data || null;
      state.forecast.lastFilters = uiState.forecast.lastFilters || null;
      state.forecast.stale = !!uiState.forecast.stale;
    }
    if (uiState.insights && typeof uiState.insights === "object") {
      state.insights.data = uiState.insights.data || null;
      state.insights.lastFilters = uiState.insights.lastFilters || null;
      state.insights.error = uiState.insights.error || null;
    }
  };

  const persistSnapshot = (payload = state.payload) => {
    const qs = sanitizeOverviewQs(state.lastSuccessfulQs || lastAppliedQs || window.location.search || "");
    if (!pageCache || !payload || !qs) return false;
    return pageCache.saveSnapshot(PAGE_CACHE_ID, {
      qs,
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
    state.payload = snapshot.payload || {};
    state.lastSuccessfulQs = qs;
    syncControlsFromState();
    setBanner(null);
    renderAll();
    renderForecastState(state.forecast.data || {});
    if (restoreScroll) {
      pageCache.restoreScroll(PAGE_CACHE_ID, { qs, ...PAGE_CACHE_POLICY, delayMs: 40 });
    }
    return snapshot;
  };

  const setQueryParam = (key, value) => {
    const params = new URLSearchParams(window.location.search || "");
    if (value === null || value === undefined || value === "") params.delete(key);
    else params.set(key, value);
    const qs = params.toString();
    const next = `${window.location.pathname}${qs ? `?${qs}` : ""}`;
    window.history.replaceState({}, "", next);
  };

  const sanitizeOverviewQs = (qsRaw) => {
    const params = new URLSearchParams((qsRaw || "").toString());
    DEPRECATED_WINDOW_PARAMS.forEach((key) => params.delete(key));
    return params.toString();
  };

  const stripDeprecatedWindowParamsFromUrl = () => {
    const current = new URLSearchParams(window.location.search || "");
    let changed = false;
    DEPRECATED_WINDOW_PARAMS.forEach((key) => {
      if (current.has(key)) {
        current.delete(key);
        changed = true;
      }
    });
    if (!changed) return;
    const qs = current.toString();
    const next = `${window.location.pathname}${qs ? `?${qs}` : ""}`;
    window.history.replaceState({}, "", next);
  };

  const wireDimToggle = () => {
    if (!els.dimToggle) return;
    els.dimToggle.addEventListener("click", (evt) => {
      const btn = evt.target && evt.target.closest ? evt.target.closest("button[data-dim]") : null;
      if (!btn) return;
      const dim = btn.getAttribute("data-dim");
      if (!dim) return;
      state.dim = dim;
      els.dimToggle.querySelectorAll("button[data-dim]").forEach((b) => b.classList.toggle("active", b === btn));
      renderMix(state.payload?.mix || {}, state.dim);
      renderPareto(state.payload?.pareto || {}, state.dim);
    });
  };

  const wireMoversToggle = () => {
    if (els.moversDimToggle) {
      els.moversDimToggle.addEventListener("click", (evt) => {
        const btn = evt.target && evt.target.closest ? evt.target.closest("button[data-movers-dim]") : null;
        if (!btn) return;
        const dim = btn.getAttribute("data-movers-dim");
        if (!dim) return;
        state.moversDim = dim;
        els.moversDimToggle.querySelectorAll("button[data-movers-dim]").forEach((b) => b.classList.toggle("active", b === btn));
        renderTopMovers(state.payload?.top_movers || {}, state.moversDim, state.moversSort);
      });
    }
    if (els.moversSortSelect) {
      els.moversSortSelect.addEventListener("change", () => {
        state.moversSort = els.moversSortSelect.value || "delta_abs";
        renderTopMovers(state.payload?.top_movers || {}, state.moversDim, state.moversSort);
      });
    }
  };

  const wireDriverMetricToggle = () => {
    if (!els.driversMetricToggle) return;
    els.driversMetricToggle.addEventListener("click", (evt) => {
      const btn = evt.target && evt.target.closest ? evt.target.closest("button[data-driver-metric]") : null;
      if (!btn || btn.disabled) return;
      const metric = btn.getAttribute("data-driver-metric");
      if (!metric) return;
      state.driverMetric = metric;
      renderDrivers((state.insights.data || state.payload || {}).drivers || {});
    });
  };

  const wireTrendToggles = () => {
    if (els.trendFreqToggle) {
      els.trendFreqToggle.addEventListener("click", (evt) => {
        const btn = evt.target && evt.target.closest ? evt.target.closest("button[data-trend-freq]") : null;
        if (!btn) return;
        const freq = btn.getAttribute("data-trend-freq");
        if (!freq) return;
        state.trend.freq = freq;
        els.trendFreqToggle.querySelectorAll("button[data-trend-freq]").forEach((node) => node.classList.toggle("active", node === btn));
        renderTrend(state.payload?.trend || {});
      });
    }
    if (els.trendOverlayMetric) {
      els.trendOverlayMetric.value = state.trend.overlay;
      els.trendOverlayMetric.addEventListener("change", () => {
        state.trend.overlay = els.trendOverlayMetric.value || "profit";
        renderTrend(state.payload?.trend || {});
      });
    }
    if (els.trendRollingToggle) {
      els.trendRollingToggle.checked = !!state.trend.rolling;
      els.trendRollingToggle.addEventListener("change", () => {
        state.trend.rolling = !!els.trendRollingToggle.checked;
        renderTrend(state.payload?.trend || {});
      });
    }
  };

  const wireForecastControls = () => {
    if (els.forecastMetricButtons && els.forecastMetricButtons.length) {
      els.forecastMetricButtons.forEach((btn) => {
        btn.addEventListener("click", () => {
          const metric = btn.dataset.forecastMetric;
          if (!metric) return;
          state.forecast.metric = metric;
          els.forecastMetricButtons.forEach((b) => b.classList.toggle("active", b === btn));
          state.forecast.stale = !!state.forecast.data;
          renderForecastState(state.forecast.data);
        });
      });
    }
    if (els.forecastHorizon) {
      els.forecastHorizon.value = state.forecast.horizon;
      els.forecastHorizon.addEventListener("change", () => {
        const val = parseInt(els.forecastHorizon.value, 10);
        state.forecast.horizon = [3, 6, 12].includes(val) ? val : 6;
        els.forecastHorizon.value = state.forecast.horizon;
        state.forecast.stale = !!state.forecast.data;
        renderForecastState(state.forecast.data);
      });
    }
    if (els.forecastIncludePartial) {
      els.forecastIncludePartial.checked = !!state.forecast.includePartial;
      els.forecastIncludePartial.addEventListener("change", () => {
        state.forecast.includePartial = !!els.forecastIncludePartial.checked;
        state.forecast.stale = !!state.forecast.data;
        renderForecastState(state.forecast.data);
      });
    }
    if (els.forecastRunBtn) {
      els.forecastRunBtn.addEventListener("click", () => runForecast());
    }
  };

  const wireMarginRiskFilters = () => {
    if (els.negativeMarginSupplierFilter) {
      els.negativeMarginSupplierFilter.addEventListener("change", () => {
        renderProfitability((getRiskPayload().profitability) || {});
      });
    }
    if (els.negativeMarginProteinFilter) {
      els.negativeMarginProteinFilter.addEventListener("change", () => {
        renderProfitability((getRiskPayload().profitability) || {});
      });
    }
  };

  const wireOverviewActionLinks = () => {
    page.addEventListener("click", (evt) => {
      const link = evt.target && evt.target.closest ? evt.target.closest("a") : null;
      if (!link) return;
      if (link === els.driversMoversLink) {
        evt.preventDefault();
        navigateToOverviewTarget("movers_customer");
        return;
      }
      if (link === els.driversSkuMixLink) {
        evt.preventDefault();
        navigateToOverviewTarget("movers_product");
        return;
      }
      const actionTarget = link.dataset ? link.dataset.overviewTarget : null;
      if (!actionTarget) return;
      if (!String(link.getAttribute("href") || "").startsWith("#")) return;
      evt.preventDefault();
      navigateToOverviewTarget(actionTarget);
    });
  };

  const triggerExport = (url) => {
    if (!url) return;
    window.location.assign(url);
  };

  const wireExportActions = () => {
    if (els.downloadSnapshotBtn) {
      els.downloadSnapshotBtn.addEventListener("click", () => {
        triggerExport(buildSnapshotExportUrl("all", "xlsx"));
      });
    }
    if (els.exportDataHealthBtn) {
      els.exportDataHealthBtn.addEventListener("click", () => {
        triggerExport(buildSnapshotExportUrl("data_health", "xlsx"));
      });
    }
    if (els.moversExportBtn) {
      els.moversExportBtn.addEventListener("click", () => {
        triggerExport(buildDrilldownUrl("movers", { dimension: state.moversDim, format: "xlsx" }));
      });
    }
    if (els.driversExportBtn) {
      els.driversExportBtn.addEventListener("click", () => {
        triggerExport(buildSnapshotExportUrl("drivers", "xlsx"));
      });
    }
    if (els.trendExportBtn) {
      els.trendExportBtn.addEventListener("click", () => {
        triggerExport(buildTrendExportUrl("xlsx"));
      });
    }
    if (els.concentrationExportBtn) {
      els.concentrationExportBtn.addEventListener("click", () => {
        triggerExport(buildSnapshotExportUrl("concentration", "xlsx"));
      });
    }
    if (els.marginRiskExportBtn) {
      els.marginRiskExportBtn.addEventListener("click", () => {
        triggerExport(buildDrilldownUrl("margin_risk", { format: "xlsx" }));
      });
    }
    if (els.marginRiskDrilldownLink) {
      els.marginRiskDrilldownLink.href = buildDrilldownUrl("margin_risk");
    }
    if (els.driversMoversLink) {
      els.driversMoversLink.href = "#moversPanel";
    }
    if (els.driversSkuMixLink) {
      els.driversSkuMixLink.href = "#moversPanel";
    }
  };

  const setLoading = (loading) => {
    document.body.classList.toggle("loading", loading);
    page.classList.toggle("is-loading", loading);
    page.setAttribute("aria-busy", loading ? "true" : "false");
  };

  const consumeApplyId = () => {
    const applyId = currentApplyId;
    currentApplyId = "";
    return applyId;
  };

  const dispatchGlobalApplyAck = (detail = {}) => {
    const payload = { ...detail };
    const applyId = consumeApplyId();
    if (applyId) payload.applyId = applyId;
    if (typeof window.dispatchGlobalFiltersApplied === "function") {
      window.dispatchGlobalFiltersApplied(payload);
      return;
    }
    window.dispatchEvent(new CustomEvent("globalFilters:applied", { detail: payload }));
  };

  const resizeRevealedCharts = (disclosure) => {
    window.dispatchEvent(new Event("resize"));
    disclosure?.querySelectorAll("canvas").forEach((canvas) => {
      window.Chart?.getChart?.(canvas)?.resize?.();
    });
    disclosure?.querySelectorAll(".js-plotly-plot").forEach((plot) => {
      window.Plotly?.Plots?.resize?.(plot);
    });
  };

  const revealDisclosureTarget = (hash = window.location.hash) => {
    if (!hash || hash === "#") return;
    let target = null;
    try {
      target = document.querySelector(hash);
    } catch (_err) {
      return;
    }
    const disclosure = target?.closest?.("details");
    if (disclosure && !disclosure.open) {
      disclosure.open = true;
      window.requestAnimationFrame(() => resizeRevealedCharts(disclosure));
    }
  };

  document.querySelectorAll("#overviewPage details.overview-disclosure").forEach((disclosure) => {
    disclosure.addEventListener("toggle", () => {
      if (disclosure.open) window.requestAnimationFrame(() => resizeRevealedCharts(disclosure));
    });
  });
  document.addEventListener("click", (event) => {
    const link = event.target.closest?.('a[href^="#"]');
    if (link) window.setTimeout(() => revealDisclosureTarget(link.getAttribute("href")), 0);
  });
  window.addEventListener("hashchange", () => revealDisclosureTarget());

  const load = async (qsOverride, options = {}) => {
    const version = ++requestSeq;
    if (activeController) {
      activeController.abort();
    }
    const controller = new AbortController();
    activeController = controller;
    const sanitizedQs = qsOverride !== undefined
      ? sanitizeOverviewQs(qsOverride)
      : sanitizeOverviewQs(new URLSearchParams(window.location.search || "").toString());
    const url = buildApiUrl(sanitizedQs);
    lastAppliedQs = sanitizedQs;
    const hasVisibleSnapshot = !!state.payload;
    if (!(options.background && hasVisibleSnapshot)) {
      setLoading(true);
    }
    try {
      const { data, notModified } = await fetchJson(url, controller.signal);
      if (notModified) {
        markForecastStale();
        maybeAutoRunForecast();
        return;
      }
      state.payload = data || {};
      state.lastSuccessfulQs = sanitizedQs;
      state.insights.data = null;
      state.insights.error = null;
      setBanner(null);
      syncControlsFromState();
      renderAll();
      persistSnapshot(state.payload);
      markForecastStale();
      maybeAutoRunForecast();
    } catch (err) { // eslint-disable-line no-unused-vars
      if (err?.name === "AbortError") return;
      applyLoadFailureState(String(err && err.message ? err.message : err), { hasSnapshot: !!state.payload });
    } finally {
      if (version !== requestSeq) return;
      setLoading(false);
      settleLoadingFallbacks("partial");
      ensureChartWarning();
      dispatchGlobalApplyAck({ qs: lastAppliedQs, requestId: state.payload?.meta?.request_id });
    }
  };

  wireDimToggle();
  wireMoversToggle();
  wireDriverMetricToggle();
  wireTrendToggles();
  wireForecastControls();
  wireMarginRiskFilters();
  wireOverviewActionLinks();
  wireExportActions();
  revealDisclosureTarget();
  stripDeprecatedWindowParamsFromUrl();
  const bootstrap = async (qsHint) => {
    if (bootstrapped) return;
    bootstrapped = true;
    let qs = qsHint;
    if (!qs) {
      const readyDetail = await waitForFiltersReady();
      qs = readyDetail?.qs;
    }
    const sanitizedQs = sanitizeOverviewQs(qs || "");
    const snapshot = restoreSnapshot(sanitizedQs, { restoreScroll: true });
    if (snapshot?.fresh) {
      markForecastStale();
      maybeAutoRunForecast();
      return;
    }
    load(sanitizedQs, { background: !!snapshot?.payload });
  };

  const onApply = (evt) => {
    currentApplyId = String(evt?.detail?.applyId || "");
    const qs = sanitizeOverviewQs(evt?.detail?.qs || "");
    load(qs, { background: !!state.payload });
  };
  const onReady = (evt) => {
    const qs = sanitizeOverviewQs(evt?.detail?.qs || "");
    bootstrap(qs);
  };

  window.addEventListener("globalFilters:apply", onApply);
  window.addEventListener("globalFilters:ready", onReady);
  window.addEventListener("pagehide", () => {
    persistSnapshot();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") persistSnapshot();
  });
  bootstrap();
})();

/*
 * Acquisition economics panel.
 *
 * A separate IIFE with its own fetch, on purpose. CAC needs the campaign layer
 * and CLV:CAC needs the Customers page's governed CLV; computing either inside
 * the overview bundle added ~5s to a request that already takes ~12s cold, and
 * this page's cold start is the one that decides whether a reviewer waits. So
 * the main payload renders first and this fills one panel afterwards.
 *
 * The panel stays hidden unless the fetch returns real numbers — an
 * "acquisition economics" card showing four dashes is worse than no card.
 */
(() => {
  "use strict";

  const card = document.getElementById("acquisitionEconomicsCard");
  if (!card) return;

  const F = window.WAFormat || {};
  const MISSING = F.MISSING || "—";
  const num = (v) => (Number.isFinite(Number(v)) ? Number(v) : null);
  const money = (v) => (num(v) == null ? MISSING : F.currency(v, { forceDecimals: false }));
  const mult = (v) => (num(v) == null ? MISSING : `${F.decimal(v, { decimals: 2 })}×`);
  const setText = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  };

  fetch("/marketing/api/bundle", { credentials: "same-origin", headers: { Accept: "application/json" } })
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
    .then((payload) => {
      if (!payload || payload.available === false) return;
      const by = {};
      (payload.kpis || []).forEach((k) => { by[k.key] = k.value; });
      const clv = payload.totals?.average_clv;
      if (num(by.cac) == null || num(clv) == null) return;

      setText("acqCac", money(by.cac));
      setText("acqClv", money(clv));
      setText("acqClvCac", mult(by.clv_cac));
      setText("acqPayback", num(by.cac_payback) == null
        ? MISSING
        : `${F.decimal(by.cac_payback, { decimals: 1 })} mo`);
      setText("acqBasisNote",
        `${payload.selected?.label || ""} acquisition economics. CLV is the 12-month discounted `
        + "gross-profit figure from the Customers page, so the ratio is a first-year measure, not "
        + "the lifetime basis the 3:1 benchmark assumes.");
      setText("acqSourceNote",
        `${payload.totals?.new_customers ?? 0} accounts won on `
        + `${money(payload.totals?.total_acquisition_spend)} of marketing and acquisition selling spend. `
        + "Full channel detail is on the Marketing page.");
      card.hidden = false;
    })
    .catch(() => { /* panel simply stays hidden */ });
})();
