(() => {
  const root = document.getElementById("RegionsOverviewV2App");
  if (!root) return;

  const authFetch = window.authFetch || fetch;
  const pageCache = window.analyticsPageCache || null;
  if (document?.body?.dataset) {
    document.body.dataset.filtersHandler = "ajax";
  }

  const bundleUrl = root.dataset.bundleUrl || "/api/regions/bundle";
  const exportUrl = root.dataset.exportUrl || "/regions/export";
  const exportMomentumUrl = root.dataset.exportMomentumUrl || "/regions/export_momentum";
  const PAGE_CACHE_ID = "regions";
  const PAGE_CACHE_POLICY = { freshMs: 90 * 1000, maxAgeMs: 20 * 60 * 1000 };

  let filterQs = (window.location.search || "").replace(/^\?/, "");
  let controller = null;
  let fetchSeq = 0;
  let currentApplyId = "";
  let bootstrapped = false;
  let lastFetchKey = "";

  let page = 1;
  let pageSize = 25;
  let sortBy = "revenue";
  let sortDir = "desc";
  let search = "";
  let quickFilter = "";

  let chartLabels = [];
  let chartValues = [];
  let profitabilityRows = [];
  let momentumRows = [];
  let momentumTab = "all";
  let momentumSort = { key: "delta_revenue", dir: "desc" };
  let tableMeta = {};
  let lastPayload = null;

  const fmtMoney0 = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 0 });
  const fmtMoney2 = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 2 });
  const fmtInt = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
  const fmtPct1 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
  const fmtCompact = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });

  const setText = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  };

  const fmtCurrency = (value) => (value == null || Number.isNaN(Number(value)) ? "-" : fmtMoney0.format(Number(value)));
  const fmtCurrency2 = (value) => (value == null || Number.isNaN(Number(value)) ? "-" : fmtMoney2.format(Number(value)));
  const fmtPercent = (value) => (value == null || Number.isNaN(Number(value)) ? "-" : `${fmtPct1.format(Number(value))}%`);
  const fmtCompactCurrency = (value) => (value == null || Number.isNaN(Number(value)) ? "-" : `$${fmtCompact.format(Number(value))}`);
  const fmtSignedPoints = (value) => {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return "";
    return `${numeric > 0 ? "+" : ""}${fmtPct1.format(numeric)} pts`;
  };
  const asNumber = (value, fallback = 0) => {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numeric : fallback;
  };
  const truncate = (value, maxLen = 24) => {
    const text = String(value ?? "");
    return text.length > maxLen ? `${text.slice(0, maxLen - 1)}…` : text;
  };
  const displayWindowEnd = (rawEnd) => {
    if (!rawEnd) return "-";
    try {
      const endDt = new Date(`${rawEnd}T00:00:00Z`);
      endDt.setUTCDate(endDt.getUTCDate() - 1);
      return endDt.toISOString().slice(0, 10);
    } catch (_err) {
      return rawEnd;
    }
  };

  const escapeHtml = (value) =>
    String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");

  const normalizeStatusKey = (value) => String(value || "").trim().toLowerCase();
  const statusPillClass = (value) => {
    const key = normalizeStatusKey(value);
    if (key === "red") return "status-red";
    if (key === "orange") return "status-orange";
    if (key === "yellow") return "status-yellow";
    if (key === "light_green") return "status-light-green";
    if (key === "green") return "status-green";
    return "status-neutral";
  };
  const statusLabel = (row = {}) => row?.target_status || row?.profitability_band || "Needs review";
  const marginContextText = (row = {}) => {
    const parts = [];
    if (row.target_margin_pct != null) parts.push(`Target ${fmtPercent(row.target_margin_pct)}`);
    if (row.minimum_margin_pct != null) parts.push(`Min ${fmtPercent(row.minimum_margin_pct)}`);
    if (row.target_gap_pct_points != null) {
      parts.push(`${fmtSignedPoints(row.target_gap_pct_points)} vs target`);
    } else if (row.target_status) {
      parts.push(row.target_status);
    }
    return parts.join(" · ");
  };
  const marginCellHtml = (row = {}) => {
    const status = statusLabel(row);
    const context = marginContextText(row);
    const pill = status
      ? `<span class="status-pill ${statusPillClass(row.status_key)}">${escapeHtml(status)}</span>`
      : "";
    return `
      <div class="metric-stack metric-stack-end">
        <div>${fmtPercent(row.margin_pct)}</div>
        ${context || pill ? `<div class="metric-sub">${pill}${context ? `${pill ? " " : ""}<span>${escapeHtml(context)}</span>` : ""}</div>` : ""}
      </div>
    `;
  };

  const currentFilterState = () => {
    try {
      const globalState = window.getGlobalFilterState ? window.getGlobalFilterState() : {};
      if (globalState?.filters && typeof globalState.filters === "object") return globalState.filters;
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

  const setDrillPayload = (el, payload) => {
    if (!el || !window.universalDrilldown || typeof window.universalDrilldown.setPayload !== "function") return;
    window.universalDrilldown.setPayload(el, payload);
  };

  const drillAttr = (payload) => {
    if (!payload) return "";
    return ` data-drilldown-payload="${escapeHtml(JSON.stringify(payload))}"`;
  };

  const regionDrillPayload = (regionId, regionLabel, section, widget, metric, value, extra = {}) => {
    const cleanId = String(regionId || regionLabel || "").trim();
    if (!cleanId) return null;
    return {
      source_page: "regions",
      source_section: section,
      source_widget: widget,
      requested_target: "region",
      clicked_entity_type: "region",
      clicked_entity_id: cleanId,
      clicked_entity_label: String(regionLabel || cleanId),
      clicked_metric: metric,
      clicked_metric_value: value,
      active_filter_state: currentFilterState(),
      extra,
    };
  };

  const workspacePayload = (section, widget, metric, value, extra = {}) => ({
    source_page: "regions",
    source_section: section,
    source_widget: widget,
    requested_target: "workspace",
    clicked_metric: metric,
    clicked_metric_value: value,
    active_filter_state: currentFilterState(),
    extra,
  });

  const waitForFiltersReady = async () => {
    const fallbackState = () => {
      try {
        return (window.getGlobalFilterState && window.getGlobalFilterState()) || {};
      } catch (_err) {
        return {};
      }
    };
    if (window.filtersReady && typeof window.filtersReady.then === "function") {
      try {
        const timeout = new Promise((resolve) => setTimeout(() => resolve(fallbackState()), 1500));
        return await Promise.race([window.filtersReady, timeout]);
      } catch (_err) {
        return fallbackState();
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

  const buildBundleParams = () => {
    const params = new URLSearchParams(filterQs || "");
    params.set("page", String(page));
    params.set("page_size", String(pageSize));
    params.set("sort", sortBy);
    params.set("sort_dir", sortDir);
    if (search) params.set("search", search);
    if (quickFilter) params.set("quick_filter", quickFilter);
    return params;
  };

  const appendExportHref = (base, options = {}) => {
    const params = new URLSearchParams(filterQs || "");
    Object.entries(options).forEach(([key, value]) => {
      if (value == null || value === "") return;
      params.set(key, String(value));
    });
    const query = params.toString();
    return query ? `${base}?${query}` : base;
  };

  const updateExports = () => {
    const summaryXlsx = document.getElementById("regionsV2ExportXlsx");
    const summaryCsv = document.getElementById("regionsV2ExportCsv");
    const summaryCardXlsx = document.getElementById("regionsV2SummaryXlsx");
    const summaryCardCsv = document.getElementById("regionsV2SummaryCsv");
    const tableCsv = document.getElementById("regionsV2TableCsv");
    const tableXlsx = document.getElementById("regionsV2TableXlsx");
    const tableCardCsv = document.getElementById("regionsV2ExportTableCsv");
    const tableCardXlsx = document.getElementById("regionsV2ExportTableXlsx");
    const momentumCsv = document.getElementById("regionsV2MomentumCsv");
    const momentumXlsx = document.getElementById("regionsV2MomentumXlsx");
    const momentumCardCsv = document.getElementById("regionsV2ExportMomentumCsv");
    const momentumCardXlsx = document.getElementById("regionsV2ExportMomentumXlsx");
    const riskCsv = document.getElementById("regionsV2ExportRiskCsv");
    const riskXlsx = document.getElementById("regionsV2ExportRiskXlsx");

    const summaryXlsxHref = appendExportHref(exportUrl, { format: "xlsx", dataset: "summary" });
    const summaryCsvHref = appendExportHref(exportUrl, { format: "csv", dataset: "summary" });
    const tableXlsxHref = appendExportHref(exportUrl, {
      format: "xlsx",
      dataset: "table",
      search,
      quick_filter: quickFilter,
      sort: sortBy,
      sort_dir: sortDir,
    });
    const tableCsvHref = appendExportHref(exportUrl, {
      format: "csv",
      dataset: "table",
      search,
      quick_filter: quickFilter,
      sort: sortBy,
      sort_dir: sortDir,
    });
    const riskXlsxHref = appendExportHref(exportUrl, { format: "xlsx", dataset: "risk" });
    const riskCsvHref = appendExportHref(exportUrl, { format: "csv", dataset: "risk" });
    const momentumXlsxHref = appendExportHref(exportMomentumUrl, { format: "xlsx" });
    const momentumCsvHref = appendExportHref(exportMomentumUrl, { format: "csv" });

    [summaryXlsx, summaryCardXlsx].forEach((el) => el && el.setAttribute("href", summaryXlsxHref));
    [summaryCsv, summaryCardCsv].forEach((el) => el && el.setAttribute("href", summaryCsvHref));
    [tableXlsx, tableCardXlsx].forEach((el) => el && el.setAttribute("href", tableXlsxHref));
    [tableCsv, tableCardCsv].forEach((el) => el && el.setAttribute("href", tableCsvHref));
    [riskXlsx].forEach((el) => el && el.setAttribute("href", riskXlsxHref));
    [riskCsv].forEach((el) => el && el.setAttribute("href", riskCsvHref));
    [momentumXlsx, momentumCardXlsx].forEach((el) => el && el.setAttribute("href", momentumXlsxHref));
    [momentumCsv, momentumCardCsv].forEach((el) => el && el.setAttribute("href", momentumCsvHref));
  };

  const syncControlsFromState = () => {
    const searchInput = document.getElementById("regionsV2Search");
    if (searchInput) searchInput.value = search || "";
    document.querySelectorAll("[data-quick-filter]").forEach((button) => {
      button.classList.toggle("active", (button.dataset.quickFilter || "") === quickFilter);
    });
    document.querySelectorAll("[data-momentum-tab]").forEach((button) => {
      button.classList.toggle("active", (button.dataset.momentumTab || "all") === momentumTab);
    });
  };

  const snapshotUiState = () => ({
    page,
    pageSize,
    sortBy,
    sortDir,
    search,
    quickFilter,
    momentumTab,
    momentumSort: { ...momentumSort },
  });

  const applySnapshotUiState = (uiState = {}) => {
    if (!uiState || typeof uiState !== "object") return;
    if (Number.isFinite(Number(uiState.page)) && Number(uiState.page) > 0) page = Number(uiState.page);
    if (Number.isFinite(Number(uiState.pageSize)) && Number(uiState.pageSize) > 0) pageSize = Number(uiState.pageSize);
    if (uiState.sortBy) sortBy = String(uiState.sortBy);
    if (uiState.sortDir) sortDir = String(uiState.sortDir) === "asc" ? "asc" : "desc";
    if (uiState.search != null) search = String(uiState.search);
    if (uiState.quickFilter != null) quickFilter = String(uiState.quickFilter);
    if (uiState.momentumTab) momentumTab = String(uiState.momentumTab);
    if (uiState.momentumSort && typeof uiState.momentumSort === "object") {
      momentumSort = {
        key: String(uiState.momentumSort.key || momentumSort.key || "delta_revenue"),
        dir: String(uiState.momentumSort.dir || momentumSort.dir || "desc") === "asc" ? "asc" : "desc",
      };
    }
  };

  const persistSnapshot = (payload = lastPayload) => {
    if (!pageCache || !payload || !filterQs) return false;
    return pageCache.saveSnapshot(PAGE_CACHE_ID, {
      qs: filterQs,
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
    syncControlsFromState();
    applyPayload(snapshot.payload);
    if (restoreScroll) {
      pageCache.restoreScroll(PAGE_CACHE_ID, { qs, ...PAGE_CACHE_POLICY, delayMs: 40 });
    }
    return snapshot;
  };

  const renderBadge = (id, text, tone = "") => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = text;
    el.classList.remove("text-bg-success", "text-bg-warning", "text-bg-danger", "text-bg-info", "text-bg-light");
    el.classList.add(tone || "text-bg-light");
  };

  const renderHero = (kpis = {}, meta = {}) => {
    const packs = meta?.packs_coverage || {};
    const freshness = meta?.freshness || {};

    renderBadge("regionsV2Window", `Window: ${kpis.start || "-"} to ${displayWindowEnd(kpis.end)}`, "text-bg-light");
    renderBadge("regionsV2Regions", `Regions in scope: ${fmtInt.format(asNumber(kpis.regions_count))}`, "text-bg-light");
    renderBadge("regionsV2Customers", `Customers in scope: ${fmtInt.format(asNumber(kpis.customers))}`, "text-bg-light");
    renderBadge("regionsV2Orders", `Orders in scope: ${fmtInt.format(asNumber(kpis.orders))}`, "text-bg-light");

    const packsPct = packs?.packs_coverage_pct;
    const packsTone = packsPct == null ? "text-bg-light" : packsPct >= 98 ? "text-bg-success" : packsPct >= 90 ? "text-bg-warning" : "text-bg-danger";
    renderBadge("regionsV2PacksCoverage", `Packs coverage: ${fmtPercent(packsPct)}`, packsTone);

    const costPct = kpis?.cost_coverage_pct;
    const costTone = costPct == null ? "text-bg-light" : Number(costPct) >= 95 ? "text-bg-success" : Number(costPct) >= 85 ? "text-bg-warning" : "text-bg-danger";
    renderBadge("regionsV2CostCoverage", `Cost coverage: ${fmtPercent(costPct)}`, costTone);

    const freshnessStatus = freshness?.status;
    const freshnessTone = freshnessStatus === "fresh" ? "text-bg-success" : freshnessStatus === "watch" ? "text-bg-warning" : freshnessStatus === "stale" ? "text-bg-danger" : "text-bg-light";
    renderBadge("regionsV2Freshness", `Freshness: ${freshness?.label || "-"}`, freshnessTone);
  };

  const renderKpis = (kpis = {}) => {
    setText("kpiRevenue", fmtCurrency(kpis.total_revenue));
    setText("kpiRevenueMeta", `${fmtInt.format(asNumber(kpis.orders))} orders under current filters`);
    setText("kpiProfit", fmtCurrency(kpis.profit));
    setText("kpiProfitMeta", `${fmtPercent(kpis.cost_coverage_pct)} cost coverage`);
    setText("kpiMargin", fmtPercent(kpis.margin_pct));
    setText(
      "kpiMarginMeta",
      marginContextText(kpis) || `Profit ${fmtCurrency(kpis.profit)} / Revenue ${fmtCompactCurrency(kpis.total_revenue)}`
    );
    setText("kpiRegionCount", fmtInt.format(asNumber(kpis.regions_count)));
    setText("kpiRegionCountMeta", `${fmtInt.format(asNumber(kpis.customers))} customers across regions`);
    setText("kpiAov", fmtCurrency(kpis.avg_order_value));
    setText("kpiAovMeta", `${fmtInt.format(asNumber(kpis.orders))} distinct orders`);
    setText("kpiYoy", fmtPercent(kpis.yoy_growth));
    setText("kpiYoyMeta", "Same window last year");
    setText("kpiDeltaRevenue", fmtCurrency(kpis.revenue_delta_prior));
    setText("kpiDeltaRevenueMeta", kpis.revenue_delta_prior_pct == null ? "Prior period unavailable" : `${fmtPercent(kpis.revenue_delta_prior_pct)} vs prior period`);
    setText("kpiConcentration", kpis.revenue_hhi == null ? "-" : fmtInt.format(asNumber(kpis.revenue_hhi)));
    setText("kpiConcentrationMeta", `Top 1 ${fmtPercent(kpis.concentration_top1_pct)} | Top 5 ${fmtPercent(kpis.concentration_top5_pct)}`);
    setText("kpiVolatility", fmtPercent(kpis.revenue_volatility_pct));
    setText("kpiVolatilityMeta", kpis.stability_score == null ? "12-month volatility unavailable" : `Stability score ${fmtInt.format(asNumber(kpis.stability_score))}`);
    setText("kpiRepeatRate", fmtPercent(kpis.repeat_rate_pct));
    setText("kpiRepeatRateMeta", "Share of customers with repeat orders");
    setText("kpiRiskRegions", fmtInt.format(asNumber(kpis.churn_risk_regions_count)));
    setText("kpiRiskRegionsMeta", "High-risk regions needing attention");
    setText("kpiNewCustomerShare", fmtPercent(kpis.new_customer_share_pct));
    setText("kpiNewCustomerShareMeta", "Revenue mix from new customers");

    setDrillPayload(
      document.getElementById("kpiRevenue")?.closest(".card"),
      workspacePayload("Executive Scorecard", "Commercial Value", "Total Revenue", kpis.total_revenue, {
        workspace_kind: "fact_orders",
      })
    );
    setDrillPayload(
      document.getElementById("kpiRiskRegions")?.closest(".card"),
      workspacePayload("Executive Scorecard", "Risk Regions", "Churn Risk Regions", kpis.churn_risk_regions_count, {
        workspace_kind: "narrative",
        detail: "Regions with elevated churn or retention risk under the active filtered window.",
      })
    );
  };

  const currentRevenueSeries = () => {
    const topN = Number.parseInt(document.getElementById("regionsV2TopN")?.value || "15", 10) || 15;
    const points = chartLabels.map((label, idx) => ({ label, value: asNumber(chartValues[idx]) }));
    const sliced = topN > 0 ? points.slice(0, topN) : points;
    return {
      labels: sliced.map((item) => item.label),
      values: sliced.map((item) => item.value),
    };
  };

  const renderRevenueChart = () => {
    const chartEl = document.getElementById("regionsV2RevenueChart");
    const emptyEl = document.getElementById("regionsV2RevenueEmpty");
    if (!chartEl || typeof window.Plotly === "undefined") return;
    const series = currentRevenueSeries();
    const hasData = series.labels.length > 0 && series.values.some((value) => value > 0);
    if (emptyEl) emptyEl.classList.toggle("d-none", hasData);
    if (!hasData) {
      chartEl.innerHTML = "";
      return;
    }
    const logScale = Boolean(document.getElementById("regionsV2LogScale")?.checked);
    window.Plotly.newPlot(
      chartEl,
      [
        {
          x: series.values.slice().reverse(),
          y: series.labels.map((label) => truncate(label, 28)).slice().reverse(),
          text: series.labels.slice().reverse(),
          type: "bar",
          orientation: "h",
          marker: { color: "#1d4ed8" },
          hovertemplate: "%{text}<br>%{x:$,.0f}<extra></extra>",
        },
      ],
      {
        margin: { t: 10, r: 20, b: 35, l: 120 },
        height: Math.max(340, series.labels.length * 26),
        xaxis: { tickprefix: "$", tickformat: "~s", type: logScale ? "log" : "linear" },
        yaxis: { automargin: true },
      },
      { displayModeBar: false, responsive: true }
    );
    if (typeof chartEl.on === "function") {
      if (chartEl.removeAllListeners) chartEl.removeAllListeners("plotly_click");
      chartEl.on("plotly_click", (event) => {
        const point = event?.points?.[0];
        if (!point?.text) return;
        openUniversal(
          regionDrillPayload(point.text, point.text, "Ranking & Performance", "Revenue by Region", "Revenue", point.x),
          chartEl
        );
      });
    }
  };

  const profitabilitySeries = () => {
    const metric = (document.getElementById("regionsV2ProfitMetric")?.value || "revenue").toLowerCase();
    const rows = (profitabilityRows || []).slice();
    rows.sort((a, b) => asNumber(b?.[metric], Number.NEGATIVE_INFINITY) - asNumber(a?.[metric], Number.NEGATIVE_INFINITY));
    const picked = rows.slice(0, 12);
    return { metric, rows: picked };
  };

  const renderProfitChart = () => {
    const chartEl = document.getElementById("regionsV2ProfitChart");
    const emptyEl = document.getElementById("regionsV2ProfitEmpty");
    if (!chartEl || typeof window.Plotly === "undefined") return;
    const series = profitabilitySeries();
    const labels = series.rows.map((row) => row.region || "Unknown");
    const values = series.rows.map((row) => row[series.metric]);
    const hasData = labels.length > 0 && values.some((value) => value != null && Number(value) !== 0);
    if (emptyEl) emptyEl.classList.toggle("d-none", hasData);
    if (!hasData) {
      chartEl.innerHTML = "";
      return;
    }
    const isPct = series.metric === "margin_pct";
    const color = series.metric === "profit" ? "#16a34a" : isPct ? "#0891b2" : series.metric === "aov" ? "#f59e0b" : "#4338ca";
    window.Plotly.newPlot(
      chartEl,
      [
        {
          x: values.slice().reverse(),
          y: labels.map((label) => truncate(label, 28)).slice().reverse(),
          text: labels.slice().reverse(),
          type: "bar",
          orientation: "h",
          marker: { color },
          hovertemplate: isPct ? "%{text}<br>%{x:.1f}%<extra></extra>" : "%{text}<br>%{x:$,.2f}<extra></extra>",
        },
      ],
      {
        margin: { t: 10, r: 20, b: 35, l: 120 },
        height: Math.max(340, labels.length * 26),
        xaxis: isPct ? { ticksuffix: "%" } : { tickprefix: "$", tickformat: "~s" },
        yaxis: { automargin: true },
      },
      { displayModeBar: false, responsive: true }
    );
    if (typeof chartEl.on === "function") {
      if (chartEl.removeAllListeners) chartEl.removeAllListeners("plotly_click");
      chartEl.on("plotly_click", (event) => {
        const point = event?.points?.[0];
        if (!point?.text) return;
        openUniversal(
          regionDrillPayload(point.text, point.text, "Ranking & Performance", "Profitability by Region", series.metric, point.x),
          chartEl
        );
      });
    }
  };

  const pillClass = (value, prefix) => {
    const text = String(value || "").toLowerCase();
    if (text === "high" || text === "critical") return `risk-pill ${prefix}-high`;
    if (text === "medium" || text === "watch") return `risk-pill ${prefix}-medium`;
    return `risk-pill ${prefix}-low`;
  };

  const deltaClass = (status) => {
    const text = String(status || "").toLowerCase();
    if (["decliner", "lost"].includes(text)) return "delta-down";
    if (["gainer", "new"].includes(text)) return "delta-up";
    return "delta-stable";
  };

  const renderOperations = (operations = {}) => {
    const tbody = document.getElementById("regionsV2OperationsBody");
    if (!tbody) return;
    const rows = Array.isArray(operations.rows) ? operations.rows : [];
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="4" class="text-center text-muted">No operational mix available.</td></tr>';
      return;
    }
    tbody.innerHTML = rows
      .slice(0, 12)
      .map((row) => `
        <tr${drillAttr(regionDrillPayload(row.region_id || row.region, row.region, "Operating Pattern", "Operational Mix", "Supplier count", row.supplier_count))}>
          <td>${row.region || ""}</td>
          <td class="text-end">${row.dominant_ship_method ? `${row.dominant_ship_method} (${fmtPercent(row.dominant_ship_share_pct)})` : "-"}</td>
          <td class="text-end">${fmtInt.format(asNumber(row.supplier_count))}</td>
          <td class="text-end">${fmtInt.format(asNumber(row.product_count))}</td>
        </tr>
      `)
      .join("");
  };

  const renderUnitEconomics = (payload = {}) => {
    const tbody = document.getElementById("regionsV2UnitBody");
    if (!tbody) return;
    const rows = Array.isArray(payload.rows) ? payload.rows : [];
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="4" class="text-center text-muted">No unit economics available.</td></tr>';
      return;
    }
    tbody.innerHTML = rows
      .slice(0, 12)
      .map((row) => `
        <tr${drillAttr(regionDrillPayload(row.region_id || row.region, row.region, "Unit Economics", "Region Unit Economics", "Revenue per customer", row.revenue_per_customer))}>
          <td>${row.region || ""}</td>
          <td class="text-end">${fmtCurrency2(row.revenue_per_customer)}</td>
          <td class="text-end">${fmtCurrency2(row.profit_per_order)}</td>
          <td class="text-end">${fmtCurrency2(row.revenue_per_unit)}</td>
        </tr>
      `)
      .join("");
  };

  const renderConcentration = (payload = {}) => {
    setText(
      "regionsV2ConcentrationSummary",
      `Top 1 ${fmtPercent(payload?.summary?.top1_share_pct)} | Top 5 ${fmtPercent(payload?.summary?.top5_share_pct)} | HHI ${payload?.summary?.hhi == null ? "-" : fmtInt.format(asNumber(payload.summary.hhi))}`
    );
    const tbody = document.getElementById("regionsV2ConcentrationBody");
    if (!tbody) return;
    const rows = Array.isArray(payload.over_reliant_regions) ? payload.over_reliant_regions : [];
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="4" class="text-center text-muted">No over-reliant regions under current filters.</td></tr>';
      return;
    }
    tbody.innerHTML = rows
      .map((row) => `
        <tr${drillAttr(regionDrillPayload(row.region_id || row.region, row.region, "Dependency & Concentration", "Over-Reliant Regions", "Top customer share", row.top_customer_share_pct))}>
          <td>${row.region || ""}</td>
          <td class="text-end">${fmtPercent(row.top_customer_share_pct)}</td>
          <td class="text-end">${fmtPercent(row.top_product_share_pct)}</td>
          <td class="text-end">${fmtPercent(row.top_supplier_share_pct)}</td>
        </tr>
      `)
      .join("");
  };

  const renderOpportunityMatrix = (payload = {}) => {
    const chartEl = document.getElementById("regionsV2OpportunityChart");
    const emptyEl = document.getElementById("regionsV2OpportunityEmpty");
    if (!chartEl || typeof window.Plotly === "undefined") return;
    const points = Array.isArray(payload.points) ? payload.points : [];
    if (emptyEl) emptyEl.classList.toggle("d-none", points.length >= 3);
    if (points.length < 3) {
      chartEl.innerHTML = "";
      return;
    }
    const palette = { Scale: "#16a34a", Protect: "#f59e0b", Fix: "#dc2626", Watch: "#2563eb" };
    window.Plotly.newPlot(
      chartEl,
      [
        {
          x: points.map((point) => asNumber(point.revenue)),
          y: points.map((point) => asNumber(point.margin_pct)),
          text: points.map((point) => point.region || ""),
          mode: "markers+text",
          type: "scatter",
          textposition: "top center",
          marker: {
            size: 14,
            color: points.map((point) => palette[point.quadrant] || "#2563eb"),
            opacity: 0.85,
          },
          hovertemplate: "%{text}<br>Revenue %{x:$,.0f}<br>Margin %{y:.1f}%<extra></extra>",
        },
      ],
      {
        margin: { t: 10, r: 20, b: 45, l: 55 },
        height: 340,
        xaxis: { title: "Revenue", tickprefix: "$", tickformat: "~s" },
        yaxis: { title: "Margin %", ticksuffix: "%" },
        shapes: [
          {
            type: "line",
            x0: payload.revenue_median,
            x1: payload.revenue_median,
            y0: 0,
            y1: 1,
            yref: "paper",
            line: { color: "rgba(15, 23, 42, 0.25)", dash: "dot" },
          },
          {
            type: "line",
            y0: payload.margin_median,
            y1: payload.margin_median,
            x0: 0,
            x1: 1,
            xref: "paper",
            line: { color: "rgba(15, 23, 42, 0.25)", dash: "dot" },
          },
        ],
      },
      { displayModeBar: false, responsive: true }
    );
    if (typeof chartEl.on === "function") {
      if (chartEl.removeAllListeners) chartEl.removeAllListeners("plotly_click");
      chartEl.on("plotly_click", (event) => {
        const point = event?.points?.[0];
        if (!point?.text) return;
        openUniversal(
          regionDrillPayload(point.text, point.text, "Opportunity Matrix", "Protect / Fix / Scale", "Margin %", point.y),
          chartEl
        );
      });
    }
  };

  const renderRetention = (payload = {}) => {
    const tbody = document.getElementById("regionsV2RetentionBody");
    if (!tbody) return;
    const rows = Array.isArray(payload.rows) ? payload.rows : [];
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="text-center text-muted">No retention rows available.</td></tr>';
      return;
    }
    tbody.innerHTML = rows
      .map((row) => `
        <tr${drillAttr(regionDrillPayload(row.region_id || row.region, row.region, "Retention", "Region Retention", "Repeat %", row.repeat_pct))}>
          <td>${row.region || ""}</td>
          <td class="text-end">${fmtPercent(row.repeat_pct)}</td>
          <td class="text-end">${fmtPercent(row.churn_pct)}</td>
          <td class="text-end">${fmtInt.format(asNumber(row.active_customers_30d))} / ${fmtInt.format(asNumber(row.active_customers_90d))}</td>
          <td class="text-end">${fmtPercent(row.new_customer_pct)}</td>
        </tr>
      `)
      .join("");
  };

  const renderRisk = (payload = {}) => {
    const tbody = document.getElementById("regionsV2RiskBody");
    if (!tbody) return;
    const rows = Array.isArray(payload.rows) ? payload.rows : [];
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="text-center text-muted">No risk rows available.</td></tr>';
      return;
    }
    tbody.innerHTML = rows
      .map((row) => `
        <tr${drillAttr(regionDrillPayload(row.region_id || row.region, row.region, "Risk & Coverage", "Region Risk", "Revenue delta", row.delta_revenue))}>
          <td>${row.region || ""}</td>
          <td><span class="${pillClass(row.risk_band, "risk")}">${row.risk_band || "Low"}</span></td>
          <td>${row.risk_summary || "-"}</td>
          <td class="text-end">${fmtCurrency(row.delta_revenue)}</td>
          <td><span class="${pillClass(row.data_quality_flag, "quality")}">${row.data_quality_flag || "OK"}</span></td>
        </tr>
      `)
      .join("");
  };

  const renderMomentumHighlights = (payload = {}) => {
    const topGainer = Array.isArray(payload.gainers) ? payload.gainers[0] : null;
    const topDecliner = Array.isArray(payload.decliners) ? payload.decliners[0] : null;
    setText("regionsV2TopGainer", topGainer?.region || "No gainer");
    setText("regionsV2TopGainerMeta", topGainer ? `${fmtCurrency(topGainer.delta_revenue)} (${topGainer.delta_revenue_label || fmtPercent(topGainer.delta_revenue_pct)})` : "No positive delta in window");
    setText("regionsV2TopDecliner", topDecliner?.region || "No decliner");
    setText("regionsV2TopDeclinerMeta", topDecliner ? `${fmtCurrency(topDecliner.delta_revenue)} (${topDecliner.delta_revenue_label || fmtPercent(topDecliner.delta_revenue_pct)})` : "No negative delta in window");
    setText("regionsV2PriorStatus", payload?.window?.has_prior_period ? "Prior period available" : "Prior period unavailable");
  };

  const filteredMomentumRows = () => {
    let rows = Array.isArray(momentumRows) ? momentumRows.slice() : [];
    if (momentumTab === "gainers") rows = rows.filter((row) => asNumber(row.delta_revenue) > 0);
    if (momentumTab === "decliners") rows = rows.filter((row) => asNumber(row.delta_revenue) < 0);
    rows.sort((a, b) => {
      const av = a?.[momentumSort.key];
      const bv = b?.[momentumSort.key];
      const aValue = typeof av === "string" ? av.toLowerCase() : asNumber(av, Number.NEGATIVE_INFINITY);
      const bValue = typeof bv === "string" ? bv.toLowerCase() : asNumber(bv, Number.NEGATIVE_INFINITY);
      if (aValue < bValue) return momentumSort.dir === "asc" ? -1 : 1;
      if (aValue > bValue) return momentumSort.dir === "asc" ? 1 : -1;
      return 0;
    });
    return rows;
  };

  const renderMomentumTable = () => {
    const tbody = document.getElementById("regionsV2MomentumBody");
    if (!tbody) return;
    const rows = filteredMomentumRows();
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="9" class="text-center text-muted">No momentum rows match the current selection.</td></tr>';
      return;
    }
    tbody.innerHTML = rows
      .map((row) => `
        <tr${drillAttr(regionDrillPayload(row.region_id || row.region, row.region, "Momentum", "Region Momentum", "Delta revenue", row.delta_revenue))}>
          <td>${row.region || ""}</td>
          <td class="text-end">${fmtCurrency(row.revenue_current)}</td>
          <td class="text-end">${fmtCurrency(row.revenue_prior)}</td>
          <td class="text-end">${fmtCurrency(row.delta_revenue)}</td>
          <td class="text-end ${deltaClass(row.delta_revenue_status)}">${row.delta_revenue_label || fmtPercent(row.delta_revenue_pct)}</td>
          <td class="text-end">${fmtInt.format(asNumber(row.delta_orders))}</td>
          <td class="text-end">${fmtInt.format(asNumber(row.delta_customers))}</td>
          <td class="text-end">${fmtCurrency(row.profit_delta)}</td>
          <td class="text-end">${fmtPercent(row.margin_delta_pp)}</td>
        </tr>
      `)
      .join("");
    document.querySelectorAll("#regionsV2MomentumTable thead th.sortable").forEach((th) => {
      th.classList.remove("asc", "desc");
      if ((th.dataset.momentumSort || "") === momentumSort.key) {
        th.classList.add(momentumSort.dir);
      }
    });
  };

  const buildDrilldownHref = (regionId) => {
    const encoded = encodeURIComponent(String(regionId || ""));
    return filterQs ? `/regions/${encoded}?${filterQs}` : `/regions/${encoded}`;
  };

  const buildRowExportHref = (regionId) => {
    const encoded = encodeURIComponent(String(regionId || ""));
    return appendExportHref(`/regions/${encoded}/export`, { format: "xlsx" });
  };

  const renderTable = (table = {}) => {
    const tbody = document.getElementById("regionsV2TableBody");
    const status = document.getElementById("regionsV2TableStatus");
    if (!tbody) return;
    const rows = Array.isArray(table.rows) ? table.rows : [];
    tableMeta = table || {};
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="16" class="text-center text-muted">No regions match the current filters.</td></tr>';
      if (status) status.textContent = "No regions";
      updatePager();
      updateSortHeaders();
      return;
    }
    tbody.innerHTML = rows
      .map((row) => {
        const viewHref = buildDrilldownHref(row.region_id || row.region);
        const exportHref = buildRowExportHref(row.region_id || row.region);
        const payload = regionDrillPayload(row.region_id || row.region, row.region, "Region Table", "Region Command Center", "Revenue", row.revenue);
        return `
          <tr${drillAttr(payload)}>
            <td><a class="text-decoration-none fw-semibold" href="${viewHref}">${row.region || ""}</a></td>
            <td class="text-end">${fmtInt.format(asNumber(row.customers))}</td>
            <td class="text-end">${fmtInt.format(asNumber(row.orders))}</td>
            <td class="text-end">${fmtCurrency(row.revenue)}</td>
            <td class="text-end">${fmtCurrency(row.profit)}</td>
            <td class="text-end">${marginCellHtml(row)}</td>
            <td class="text-end">${fmtCurrency(row.aov)}</td>
            <td class="text-end">${fmtPercent(row.repeat_pct)}</td>
            <td class="text-end">${fmtPercent(row.churn_pct)}</td>
            <td class="text-end">${fmtPercent(row.new_customer_pct)}</td>
            <td class="text-end">${fmtPercent(row.top_customer_share_pct)}</td>
            <td class="text-end">${fmtPercent(row.top_product_share_pct)}</td>
            <td class="text-end">${fmtCurrency(row.delta_revenue)}</td>
            <td class="text-end ${deltaClass(row.delta_revenue_status)}">${row.delta_revenue_label || fmtPercent(row.delta_revenue_pct)}</td>
            <td>
              <div class="d-flex flex-column gap-1">
                <span class="${pillClass(row.risk_band, "risk")}">${row.risk_band || "Low"}</span>
                <span class="${pillClass(row.data_quality_flag, "quality")}">${row.data_quality_flag || "OK"}</span>
              </div>
            </td>
            <td>
              <div class="btn-group btn-group-sm">
                <a class="btn btn-primary" href="${viewHref}">View</a>
                <a class="btn btn-outline-secondary" href="${exportHref}">Export</a>
              </div>
            </td>
          </tr>
        `;
      })
      .join("");
    if (status) {
      const total = asNumber(table.total);
      const startRow = total > 0 ? (asNumber(table.page, 1) - 1) * asNumber(table.page_size, pageSize) + 1 : 0;
      const endRow = total > 0 ? Math.min(total, startRow + rows.length - 1) : 0;
      status.textContent = total > 0 ? `Showing ${startRow}-${endRow} of ${total}` : "No regions";
    }
    updatePager();
    updateSortHeaders();
  };

  const updatePager = () => {
    const total = asNumber(tableMeta.total);
    const currentPage = asNumber(tableMeta.page, 1);
    const size = asNumber(tableMeta.page_size, pageSize);
    const totalPages = total > 0 ? Math.max(1, Math.ceil(total / size)) : 0;
    const prevBtn = document.getElementById("regionsV2Prev");
    const nextBtn = document.getElementById("regionsV2Next");
    if (prevBtn) prevBtn.disabled = !(totalPages > 0 && currentPage > 1);
    if (nextBtn) nextBtn.disabled = !(totalPages > 0 && currentPage < totalPages);
  };

  const updateSortHeaders = () => {
    document.querySelectorAll("#regionsV2Table thead th.sortable").forEach((th) => {
      th.classList.remove("asc", "desc");
      if ((th.dataset.sort || "") === sortBy) th.classList.add(sortDir);
    });
  };

  const applyPayload = (payload = {}) => {
    lastPayload = payload || {};
    chartLabels = ((payload.charts || {}).revenue_by_region || {}).labels || [];
    chartValues = ((payload.charts || {}).revenue_by_region || {}).values || [];
    profitabilityRows = ((payload.charts || {}).profitability_by_region || {}).rows || [];
    momentumRows = ((payload.momentum || {}).rows || []).slice();

    renderHero(payload.kpis || {}, payload.meta || {});
    renderKpis(payload.kpis || {});
    renderRevenueChart();
    renderProfitChart();
    renderOperations(payload.operations || {});
    renderUnitEconomics(payload.unit_economics || {});
    renderConcentration(payload.concentration || {});
    renderOpportunityMatrix(payload.opportunity_matrix || {});
    renderRetention(payload.retention || {});
    renderRisk(payload.risk || {});
    renderMomentumHighlights(payload.momentum || {});
    renderMomentumTable();
    renderTable(payload.table || {});
    if (window.universalDrilldown && typeof window.universalDrilldown.enhanceAll === "function") {
      window.universalDrilldown.enhanceAll();
    }
    persistSnapshot(payload);
  };

  const dispatchApplied = () => {
    const detail = { page: "regions", qs: filterQs };
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
    } catch (_err) {
      /* ignore */
    }
  };

  const fetchBundle = async (force = false, options = {}) => {
    const requestId = ++fetchSeq;
    const params = buildBundleParams();
    const fetchKey = params.toString();
    if (!force && fetchKey === lastFetchKey) {
      dispatchApplied();
      return;
    }
    lastFetchKey = fetchKey;
    if (controller) controller.abort();
    controller = new AbortController();
    updateExports();
    const url = `${bundleUrl}?${params.toString()}`;
    const snapshot = options.snapshot || null;
    try {
      const response = await authFetch(url, {
        signal: controller.signal,
        credentials: "same-origin",
        headers: pageCache ? pageCache.prepareHeaders(url, { Accept: "application/json" }) : { Accept: "application/json" },
      });
      if (pageCache) pageCache.rememberResponse(url, response);
      if (response.status === 304) {
        if (!lastPayload && snapshot?.payload) applyPayload(snapshot.payload);
        return;
      }
      const payload = await response.json();
      if (!response.ok) throw new Error(payload?.error?.message || `HTTP ${response.status}`);
      applyPayload(payload);
    } catch (error) {
      if (error?.name === "AbortError") return;
      console.error("regions v2 bundle failed", error);
      if (!lastPayload) {
        const tbody = document.getElementById("regionsV2TableBody");
        if (tbody) {
          tbody.innerHTML = '<tr><td colspan="16" class="text-center text-danger">Failed to load regions.</td></tr>';
        }
      }
    } finally {
      if (requestId !== fetchSeq) return;
      dispatchApplied();
    }
  };

  const wireInteractions = () => {
    document.getElementById("regionsV2TopN")?.addEventListener("change", () => renderRevenueChart());
    document.getElementById("regionsV2LogScale")?.addEventListener("change", () => renderRevenueChart());
    document.getElementById("regionsV2ProfitMetric")?.addEventListener("change", () => renderProfitChart());

    document.querySelectorAll("#regionsV2MomentumTable thead th.sortable").forEach((th) => {
      th.addEventListener("click", () => {
        const key = th.dataset.momentumSort || "";
        if (!key) return;
        if (momentumSort.key === key) {
          momentumSort.dir = momentumSort.dir === "asc" ? "desc" : "asc";
        } else {
          momentumSort = { key, dir: key === "region" ? "asc" : "desc" };
        }
        renderMomentumTable();
      });
    });

    document.querySelectorAll("[data-momentum-tab]").forEach((button) => {
      button.addEventListener("click", () => {
        momentumTab = button.dataset.momentumTab || "all";
        document.querySelectorAll("[data-momentum-tab]").forEach((item) => item.classList.remove("active"));
        button.classList.add("active");
        renderMomentumTable();
      });
    });

    document.querySelectorAll("#regionsV2Table thead th.sortable").forEach((th) => {
      th.addEventListener("click", () => {
        const key = th.dataset.sort || "";
        if (!key) return;
        if (sortBy === key) {
          sortDir = sortDir === "asc" ? "desc" : "asc";
        } else {
          sortBy = key;
          sortDir = key === "region" ? "asc" : "desc";
        }
        page = 1;
        fetchBundle(true);
      });
    });

    let searchTimer = null;
    document.getElementById("regionsV2Search")?.addEventListener("input", (evt) => {
      const value = (evt.target.value || "").trim();
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => {
        search = value;
        page = 1;
        updateExports();
        fetchBundle(true);
      }, 200);
    });

    document.getElementById("regionsV2SearchClear")?.addEventListener("click", () => {
      search = "";
      const input = document.getElementById("regionsV2Search");
      if (input) input.value = "";
      page = 1;
      updateExports();
      fetchBundle(true);
    });

    document.querySelectorAll("[data-quick-filter]").forEach((button) => {
      button.addEventListener("click", () => {
        quickFilter = button.dataset.quickFilter || "";
        page = 1;
        document.querySelectorAll("[data-quick-filter]").forEach((item) => item.classList.remove("active"));
        button.classList.add("active");
        updateExports();
        fetchBundle(true);
      });
    });

    document.getElementById("regionsV2Prev")?.addEventListener("click", () => {
      if (page <= 1) return;
      page -= 1;
      fetchBundle(true);
    });
    document.getElementById("regionsV2Next")?.addEventListener("click", () => {
      page += 1;
      fetchBundle(true);
    });
  };

  const applyFilters = (qsHint) => {
    filterQs = (qsHint || "").replace(/^\?/, "");
    page = 1;
    updateExports();
    syncControlsFromState();
    fetchBundle(true);
  };

  const bootstrap = async (qsHint) => {
    if (bootstrapped) return;
    bootstrapped = true;
    wireInteractions();
    let nextQs = qsHint || "";
    if (!nextQs) {
      const readyDetail = await waitForFiltersReady();
      nextQs = (readyDetail && readyDetail.qs) || filterQs;
    }
    filterQs = (nextQs || filterQs || "").replace(/^\?/, "");
    syncControlsFromState();
    const snapshot = restoreSnapshot(filterQs, { restoreScroll: true });
    updateExports();
    if (snapshot?.fresh) {
      dispatchApplied();
      return;
    }
    fetchBundle(true, { snapshot });
  };

  window.addEventListener("globalFilters:apply", (evt) => {
    currentApplyId = String(evt?.detail?.applyId || "");
    const nextQs = (evt?.detail && evt.detail.qs) || "";
    applyFilters(nextQs);
  });

  window.addEventListener("globalFilters:ready", (evt) => {
    const nextQs = (evt?.detail && evt.detail.qs) || "";
    bootstrap(nextQs);
  });
  window.addEventListener("pagehide", () => {
    persistSnapshot();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") persistSnapshot();
  });

  bootstrap(filterQs);
  setTimeout(() => {
    if (!bootstrapped) bootstrap(filterQs);
  }, 800);
})();
