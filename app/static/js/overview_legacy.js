(() => {
  const page = document.getElementById("overviewPage");
  if (!page) return;
  if (document?.body?.dataset) {
    document.body.dataset.filtersHandler = "ajax";
  }
  const authFetch = window.authFetch || fetch;

  const etags = new Map();
  const charts = {};
  const state = {
    payload: null,
    dim: "customer",
    trendOptions: { profit: true, margin: true },
    forecast: { metric: "revenue", horizon: 6, data: null, lastFilters: null, loading: false, stale: false },
    insights: { data: null, loading: false, error: null, lastFilters: null },
  };
  const DEPRECATED_WINDOW_PARAMS = ["include_current_month", "include_current", "include_current_months"];
  let activeController = null;
  let insightsController = null;
  let requestSeq = 0;
  let lastAppliedQs = null;
  let bootstrapped = false;
  let currentApplyId = null;

  const fmtCurrency0 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
  const fmtCurrency1 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 1 });
  const fmtNumber0 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
  const fmtNumber1 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });
  const fmtPercent1 = (v) => `${fmtNumber1.format(Number(v) || 0)}%`;

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
    { key: "revenue", deltaKey: "revenue", label: "Revenue", fmt: "currency", tooltip: "Total revenue across the selected window." },
    { key: "cost", deltaKey: "cost", label: "Cost", fmt: "currency", tooltip: "Total landed cost for fulfilled orders." },
    { key: "profit", deltaKey: "profit", label: "Profit", fmt: "currency", tooltip: "Revenue minus cost." },
    { key: "margin_pct", deltaKey: "margin_pct", label: "Margin %", fmt: "percent", tooltip: "Profit as a percent of revenue." },
    { key: "orders", deltaKey: "orders", label: "Orders", fmt: "number", tooltip: "Unique orders in window." },
    { key: "customers", deltaKey: "customers", label: "Customers", fmt: "number", tooltip: "Unique customers in window." },
    { key: "qty", deltaKey: "units", label: "Units", fmt: "number", tooltip: "Units/items shipped (or best available quantity)." },
    { key: "aov", deltaKey: "aov", label: "AOV", fmt: "currency", tooltip: "Average revenue per order." },
    { key: "asp", deltaKey: "asp", label: "ASP", fmt: "currency", tooltip: "Average revenue per unit.", optional: true },
  ];

  const els = {
    banner: document.getElementById("overviewBanner"),
    filterSummary: document.getElementById("filterSummaryText"),
    lastRefresh: document.getElementById("lastRefreshChip"),
    dataWindow: document.getElementById("dataWindowChip"),
    kpiGrid: document.getElementById("kpiGrid"),
    trendChart: document.getElementById("trendChart"),
    trendEmpty: document.getElementById("trendEmpty"),
    mixChart: document.getElementById("mixChart"),
    paretoChart: document.getElementById("paretoChart"),
    healthList: document.getElementById("healthList"),
    healthBadges: document.getElementById("healthBadges"),
    healthRows: document.getElementById("healthRowsChip"),
    dimToggle: document.getElementById("dimToggle"),
    topMoversBody: document.getElementById("topMoversBody"),
    topMoversEmpty: document.getElementById("topMoversEmpty"),
    emptyState: document.getElementById("overviewEmpty"),
    trendToggles: document.querySelectorAll("[data-trend-toggle]"),
    forecastChart: document.getElementById("forecastChart"),
    forecastEmpty: document.getElementById("forecastEmpty"),
    forecastStatus: document.getElementById("forecastStatus"),
    forecastFiltersNotice: document.getElementById("forecastFiltersNotice"),
    forecastWarnings: document.getElementById("forecastWarnings"),
    forecastModel: document.getElementById("forecastModelChip"),
    forecastQuality: document.getElementById("forecastQualityChip"),
    forecastRunBtn: document.getElementById("runForecastBtn"),
    forecastSpinner: document.getElementById("forecastSpinner"),
    forecastHorizon: document.getElementById("forecastHorizon"),
    forecastMetricButtons: document.querySelectorAll("[data-forecast-metric]"),
    insightsList: document.getElementById("insightsList"),
    insightsEmpty: document.getElementById("insightsEmpty"),
    driversTable: document.getElementById("driversTable"),
    driversEmpty: document.getElementById("driversEmpty"),
    concentrationPanel: document.getElementById("concentrationPanel"),
    profitabilityPanel: document.getElementById("profitabilityPanel"),
    marginRiskList: document.getElementById("marginRiskList"),
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

  const applyEtags = (url, headers) => {
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
      throw new Error(detail || `Request failed (${resp.status})`);
    }
    const et = resp.headers.get("ETag");
    if (et) etags.set(url, et);
    return { data: await resp.json() };
  };

  const formatValue = (key, value, fmtOverride = null) => {
    if (value === null || value === undefined || Number.isNaN(value)) return "-";
    const fmt = fmtOverride || (key === "margin_pct" ? "percent" : null);
    if (fmt === "percent") return fmtPercent1(value);
    if (fmt === "currency") return fmtCurrency0.format(Number(value) || 0);
    if (fmt === "number") return fmtNumber0.format(Number(value) || 0);
    if (["revenue", "cost", "profit", "asp", "aov"].includes(key)) return fmtCurrency0.format(Number(value) || 0);
    return fmtNumber0.format(Number(value) || 0);
  };

  const compactNumber = (num, key, fmtOverride = null) => {
    const n = Number(num);
    if (!Number.isFinite(n)) return null;
    const abs = Math.abs(n);
    const short = (div, suffix) => {
      const base = n / div;
      if (fmtOverride === "currency" || ["revenue", "cost", "profit", "asp", "aov"].includes(key)) {
        return `${fmtCurrency1.format(base)}${suffix}`;
      }
      if (fmtOverride === "percent" || key === "margin_pct") return `${fmtNumber1.format(base)}${suffix}`;
      return `${fmtNumber1.format(base)}${suffix}`;
    };
    if (abs >= 1_000_000_000) return short(1_000_000_000, "B");
    if (abs >= 1_000_000) return short(1_000_000, "M");
    if (abs >= 10_000) return short(1_000, "K");
    return null;
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
    if (value === null || value === undefined || Number.isNaN(value)) return "-";
    if (fmt === "percent") return fmtPercent1(value);
    if (fmt === "currency") return fmtCurrency1.format(Number(value) || 0);
    if (fmt === "number") return fmtNumber0.format(Number(value) || 0);
    return fmtNumber1.format(Number(value) || 0);
  };

  const drillQueryString = () => {
    const qs = (window.getGlobalFilterState && window.getGlobalFilterState().qs) || window.location.search || "";
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

  const ensureKpiCards = () => {
    if (!els.kpiGrid || els.kpiGrid.childElementCount) return;
    KPI_META.forEach((meta) => {
      const card = document.createElement("article");
      card.className = "kpi-card shadow-soft";
      card.setAttribute("data-metric-card", meta.key);
      card.innerHTML = `
        <div class="kpi-head d-flex justify-content-between align-items-center gap-2">
          <div class="kpi-label fw-semibold">${meta.label}</div>
          <i class="bi bi-info-circle text-muted" title="${meta.tooltip || ""}" data-bs-toggle="tooltip"></i>
        </div>
        <div class="kpi-value-row kpi-main">
          <div class="kpi-value-wrap">
            <div class="kpi-value display-6 mb-1" data-kpi-value="${meta.key}">-</div>
          </div>
          <div class="kpi-deltas">
            <span class="kpi-delta-pill" data-kpi-delta="${meta.deltaKey || meta.key}:mom">MoM: n/a</span>
          </div>
        </div>
        <div class="kpi-sub" data-kpi-sub="${meta.deltaKey || meta.key}:yoy">YoY: n/a</div>
      `;
      els.kpiGrid.appendChild(card);
    });
    if (typeof bootstrap !== "undefined" && bootstrap.Tooltip) {
      els.kpiGrid.querySelectorAll('[data-bs-toggle="tooltip"]').forEach((el) => new bootstrap.Tooltip(el));
    }
  };

  const renderKpis = (payload) => {
    ensureKpiCards();
    if (els.kpiGrid && !document.getElementById("kpiCoverageBadge")) {
      const badge = document.createElement("div");
      badge.id = "kpiCoverageBadge";
      badge.className = "kpi-coverage-badge";
      badge.textContent = "";
      els.kpiGrid.prepend(badge);
    }
    const kpis = payload.kpis || {};
    const deltas = payload.deltas || {};
    KPI_META.forEach((meta) => {
      const card = els.kpiGrid.querySelector(`[data-metric-card="${meta.key}"]`);
      const rawVal = kpis[meta.key];
      const missing = rawVal === null || rawVal === undefined || Number.isNaN(Number(rawVal));
      if (card) {
        card.classList.toggle("is-hidden", Boolean(meta.optional && missing));
        card.setAttribute("aria-hidden", meta.optional && missing ? "true" : "false");
      }
      const valEl = els.kpiGrid.querySelector(`[data-kpi-value="${meta.key}"]`);
      if (valEl) {
        if (meta.optional && missing) {
          valEl.textContent = "";
        } else {
          const { text, title } = formatDisplay(meta.key, rawVal, meta.fmt);
          valEl.textContent = missing ? "N/A" : text;
          valEl.title = title || "";
          valEl.classList.toggle("is-empty", missing);
        }
      }
      const momEl = els.kpiGrid.querySelector(`[data-kpi-delta="${meta.deltaKey || meta.key}:mom"]`);
      const yoyEl = els.kpiGrid.querySelector(`[data-kpi-sub="${meta.deltaKey || meta.key}:yoy"]`);
      const delta = deltas[meta.deltaKey || meta.key] || {};
      if (momEl) momEl.innerHTML = deltaBadge(delta.mom_pct, "MoM");
      if (yoyEl) yoyEl.innerHTML = deltaBadge(delta.yoy_pct, "YoY");
    });
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
    const labels = trend.months || [];
    if (!labels.length) {
      if (els.trendEmpty) els.trendEmpty.classList.remove("d-none");
      els.trendChart.classList.add("d-none");
      return;
    }
    if (els.trendEmpty) els.trendEmpty.classList.add("d-none");
    els.trendChart.classList.remove("d-none");
    const revenue = trend.revenue || [];
    const units = trend.units || [];
    const asp = (trend.asp || []).map((v) => (v === null ? null : Number(v)));
    const profit = trend.profit || [];
    const margin = (trend.margin_pct || []).map((v) => (v === null ? null : Number(v)));

    const datasets = [
      { type: "line", label: "Revenue", data: revenue, borderColor: "#0d6efd", backgroundColor: "rgba(13,110,253,0.12)", tension: 0.25, yAxisID: "y" },
      { type: "bar", label: "Units", data: units, backgroundColor: "rgba(25,135,84,0.25)", borderColor: "#198754", borderWidth: 1, yAxisID: "y1" },
      { type: "line", label: "ASP", data: asp, borderColor: "#6f42c1", backgroundColor: "rgba(111,66,193,0.10)", tension: 0.25, yAxisID: "y" },
    ];
    if (state.trendOptions.profit) {
      datasets.push({ type: "line", label: "Profit", data: profit, borderColor: "#fd7e14", backgroundColor: "rgba(253,126,20,0.15)", tension: 0.25, yAxisID: "y" });
    }
    if (state.trendOptions.margin) {
      datasets.push({ type: "line", label: "Margin %", data: margin, borderColor: "#20c997", borderDash: [5, 4], tension: 0.25, yAxisID: "y2" });
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
          y1: { beginAtZero: true, position: "right", grid: { drawOnChartArea: false }, ticks: { callback: (v) => fmtNumber0.format(Number(v) || 0) } },
          y2: { position: "right", grid: { drawOnChartArea: false }, ticks: { callback: (v) => `${fmtNumber1.format(Number(v) || 0)}%` }, display: state.trendOptions.margin },
        },
        plugins: { legend: { display: true, position: "bottom" } },
      },
    });
  };

  const renderMix = (mix = {}, dim = "customer") => {
    if (!els.mixChart) return;
    destroyChart("mix");
    const rows = mix[dim] || [];
    if (!rows.length) return;
    const labels = rows.map((r) => r.label);
    const values = rows.map((r) => r.value);
    const ctx = els.mixChart.getContext("2d");
    charts.mix = new Chart(ctx, {
      type: "bar",
      data: { labels, datasets: [{ label: "Revenue", data: values, backgroundColor: "rgba(13,110,253,0.3)", borderColor: "#0d6efd", borderWidth: 1 }] },
      options: { indexAxis: "y", responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { ticks: { callback: (v) => fmtCurrency0.format(Number(v) || 0) } } } },
    });
  };

  const renderPareto = (pareto = {}, dim = "customer") => {
    if (!els.paretoChart) return;
    destroyChart("pareto");
    const payload = pareto[dim] || {};
    const labels = payload.labels || [];
    if (!labels.length) return;
    const values = payload.values || [];
    const cum = payload.cum_pct || [];
    const ctx = els.paretoChart.getContext("2d");
    charts.pareto = new Chart(ctx, {
      data: {
        labels,
        datasets: [
          { type: "bar", label: "Revenue", data: values, backgroundColor: "rgba(13,110,253,0.25)", borderColor: "#0d6efd", borderWidth: 1, yAxisID: "y" },
          { type: "line", label: "Cumulative %", data: cum, borderColor: "#198754", backgroundColor: "rgba(25,135,84,0.10)", tension: 0.2, yAxisID: "y1" },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        scales: {
          y: { beginAtZero: true, ticks: { callback: (v) => fmtCurrency0.format(Number(v) || 0) } },
          y1: { beginAtZero: true, position: "right", min: 0, max: 100, grid: { drawOnChartArea: false }, ticks: { callback: (v) => `${v}%` } },
        },
        plugins: { legend: { display: true, position: "bottom" } },
      },
    });
  };

  const renderHealth = (health = {}) => {
    if (els.healthRows) els.healthRows.textContent = `${fmtNumber0.format(health.rows || 0)} rows`;
    if (els.healthBadges) {
      const packsCoverage = health.packs_coverage_pct ?? (health.pack_missing_pct != null ? (100 - health.pack_missing_pct) : null);
      const packsLabel = packsCoverage == null ? "n/a" : `${packsCoverage}%`;
      els.healthBadges.innerHTML = `
        <span class="badge rounded-pill text-bg-light border">Missing cost: ${health.cost_missing_pct ?? "n/a"}%</span>
        <span class="badge rounded-pill text-bg-light border">Packs coverage: ${packsLabel}</span>
        <span class="badge rounded-pill text-bg-light border">Missing product mapping: ${fmtNumber0.format(health.product_mapping_missing || 0)}</span>
      `;
    }
    if (els.healthList) {
      const issues = Array.isArray(health.issues) ? health.issues : [];
      const packLine = health.total_orderlines
        ? `<li class="mb-1 d-flex justify-content-between"><span>Packs coverage</span><span class="fw-semibold">${fmtNumber0.format(health.has_packs_orderlines || 0)} / ${fmtNumber0.format(health.total_orderlines || 0)}</span></li>`
        : "";
      els.healthList.innerHTML = issues.length
        ? issues.map((i) => `<li class="mb-1 d-flex justify-content-between"><span>${i.label}</span><span class="fw-semibold">${fmtNumber0.format(i.count || 0)}</span></li>`).join("")
        : '<li class="text-muted">No major data issues detected.</li>';
      if (packLine) {
        els.healthList.innerHTML = `${packLine}${els.healthList.innerHTML}`;
      }
    }
  };

  const applyCoverageBanner = (health = {}) => {
    const coverage = health.packs_coverage_pct ?? (health.pack_missing_pct != null ? (100 - health.pack_missing_pct) : null);
    const badge = document.getElementById("kpiCoverageBadge");
    if (badge) {
      if (coverage == null) {
        badge.style.display = "none";
        badge.textContent = "";
      } else {
        badge.style.display = coverage < 98 ? "block" : "none";
        badge.textContent = `Packs coverage: ${coverage}%. Revenue excludes missing packs.`;
      }
    }
    if (coverage == null) {
      setBanner(null);
      return;
    }
    if (coverage < 98) {
      setBanner(`Packs coverage: ${coverage}%. Revenue excludes missing packs.`, "warning");
      return;
    }
    setBanner(null);
  };

  const updateGlobalCoveragePanel = (health = {}) => {
    const panel = document.getElementById("packsCoveragePanel");
    if (!panel) return;
    const countsEl = document.getElementById("packsCoverageCounts");
    const pctEl = document.getElementById("packsCoveragePct");
    const total = Number(health.total_orderlines ?? health.rows ?? 0);
    const has = Number(health.has_packs_orderlines ?? (total - (health.missing_packs_orderlines || 0)) ?? 0);
    const missing = Number(health.missing_packs_orderlines ?? 0);
    const pct = health.packs_coverage_pct ?? (health.pack_missing_pct != null ? (100 - health.pack_missing_pct) : null);
    if (!total && !missing && (pct == null || Number.isNaN(Number(pct)))) {
      panel.classList.add("d-none");
      return;
    }
    panel.classList.remove("d-none");
    const fmt = new Intl.NumberFormat();
    if (countsEl) {
      countsEl.textContent = `${fmt.format(has)} / ${fmt.format(total)} order lines have packs (${fmt.format(missing)} missing)`;
    }
    if (pctEl) {
      pctEl.textContent = pct == null || Number.isNaN(Number(pct)) ? "n/a" : `${pct}%`;
    }
  };

  const renderTopMovers = (movers = {}, dim = "customer") => {
    if (!els.topMoversBody || !els.topMoversEmpty) return;
    const bucket = movers[dim] || {};
    const gainers = bucket.gainers || [];
    const decliners = bucket.decliners || [];
    if (!gainers.length && !decliners.length) {
        els.topMoversBody.innerHTML = "";
        els.topMoversEmpty.classList.remove("d-none");
        return;
    }
    els.topMoversEmpty.classList.add("d-none");
    const renderRows = (rows) =>
      rows
        .map(
          (r) => `
        <tr>
          <td class="text-truncate" title="${r.label}">${r.label}</td>
          <td class="text-end">${fmtCurrency1.format(r.current || 0)}</td>
          <td class="text-end">${fmtCurrency1.format(r.delta || 0)}</td>
          <td class="text-end ${r.delta > 0 ? "text-success" : r.delta < 0 ? "text-danger" : "text-muted"}">
            ${r.delta_pct === null || r.delta_pct === undefined ? "n/a" : fmtPercent1(r.delta_pct)}
          </td>
        </tr>`
        )
        .join("");
    els.topMoversBody.innerHTML = `
      <tr class="table-group-divider"><th colspan="4" class="text-uppercase text-muted small">Top Gainers</th></tr>
      ${gainers.length ? renderRows(gainers) : '<tr><td colspan="4" class="text-muted">No gainers</td></tr>'}
      <tr class="table-group-divider"><th colspan="4" class="text-uppercase text-muted small">Top Decliners</th></tr>
      ${decliners.length ? renderRows(decliners) : '<tr><td colspan="4" class="text-muted">No decliners</td></tr>'}
    `;
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
    if (!els.driversTable) return;
    const renderBlock = (label, payload) => {
      if (!payload || !payload.revenue) return "";
      const rev = payload.revenue || {};
      const prof = payload.profit || {};
      const message = payload.message ? `<div class="text-muted small mb-1">${payload.message}</div>` : "";
      const row = (title, data) => `
        <tr>
          <td>${title}</td>
          <td class="text-end">${data.price_effect === null || data.price_effect === undefined ? "n/a" : formatByFmt("currency", data.price_effect)}</td>
          <td class="text-end">${data.volume_effect === null || data.volume_effect === undefined ? "n/a" : formatByFmt("currency", data.volume_effect)}</td>
          <td class="text-end">${data.mix_effect === null || data.mix_effect === undefined ? "n/a" : formatByFmt("currency", data.mix_effect)}</td>
          <td class="text-end fw-semibold">${data.delta === null || data.delta === undefined ? "n/a" : formatByFmt("currency", data.delta)}</td>
        </tr>`;
      return `
        <div class="mb-3">
          <div class="fw-semibold mb-1">${label}</div>
          ${message}
          <div class="table-responsive">
            <table class="table table-sm align-middle mb-0">
              <thead class="table-light">
                <tr>
                  <th></th>
                  <th class="text-end">Price</th>
                  <th class="text-end">Volume</th>
                  <th class="text-end">Mix</th>
                  <th class="text-end">Total</th>
                </tr>
              </thead>
              <tbody>
                ${row("Revenue", rev)}
                ${row("Profit", prof)}
              </tbody>
            </table>
          </div>
        </div>`;
    };
    const mom = renderBlock("MoM", drivers.mom || {});
    const yoy = renderBlock("YoY", drivers.yoy || {});
    if (!mom && !yoy) {
      if (els.driversEmpty) els.driversEmpty.classList.remove("d-none");
      if (els.driversEmpty && drivers.message) {
        els.driversEmpty.textContent = drivers.message;
      }
      els.driversTable.innerHTML = "";
      return;
    }
    if (els.driversEmpty) els.driversEmpty.classList.add("d-none");
    const coverage = drivers.coverage || {};
    const covText =
      coverage.cost_pct !== null && coverage.cost_pct !== undefined
        ? `<div class="text-muted small mb-2">Cost coverage: ${formatByFmt("percent", coverage.cost_pct)}</div>`
        : "";
    els.driversTable.innerHTML = `${covText}${mom}${yoy}`;
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
    const block = (label, data) => `
      <div class="mb-3">
        <div class="text-muted small d-flex align-items-center gap-1">${label}
          <i class="bi bi-info-circle text-muted" data-bs-toggle="tooltip" title="Top 1/Top 5 share of revenue; HHI is a 0-10,000 concentration index."></i>
        </div>
        <div class="fw-semibold">Top 1: ${data.top1_share === null || data.top1_share === undefined ? "n/a" : formatByFmt("percent", data.top1_share)}</div>
        <div class="text-muted small">Top 5: ${data.top5_share === null || data.top5_share === undefined ? "n/a" : formatByFmt("percent", data.top5_share)} | HHI ${data.hhi === null || data.hhi === undefined ? "n/a" : formatByFmt("number", data.hhi)}</div>
      </div>`;
    els.concentrationPanel.innerHTML = `${block("Customers", cust)}${block("Products", prod)}`;
    if (typeof bootstrap !== "undefined" && bootstrap.Tooltip) {
      els.concentrationPanel.querySelectorAll('[data-bs-toggle="tooltip"]').forEach((el) => new bootstrap.Tooltip(el));
    }
  };

  const renderProfitability = (profitability = {}) => {
    if (!els.profitabilityPanel) return;
    const stats = profitability.margin_pct || {};
    const message = profitability.message ? `<div class="text-muted small mb-2">${profitability.message}</div>` : "";
    const coverage = profitability.coverage || {};
    const covText =
      coverage.cost_pct !== null && coverage.cost_pct !== undefined
        ? `<div class="text-muted small mb-2">Cost coverage: ${formatByFmt("percent", coverage.cost_pct)}</div>`
        : "";
    if (!stats || (stats.p10 === undefined && stats.p50 === undefined && stats.p90 === undefined)) {
      els.profitabilityPanel.innerHTML = `${message}${covText}<div class="text-muted">No profitability distribution available.</div>`;
    } else {
      els.profitabilityPanel.innerHTML = `
        ${message}${covText}
        <div class="mini-list">
          <div class="mb-1">P10: ${stats.p10 === null || stats.p10 === undefined ? "n/a" : formatByFmt("percent", stats.p10)}</div>
          <div class="mb-1">P50: ${stats.p50 === null || stats.p50 === undefined ? "n/a" : formatByFmt("percent", stats.p50)}</div>
          <div class="mb-1">P90: ${stats.p90 === null || stats.p90 === undefined ? "n/a" : formatByFmt("percent", stats.p90)}</div>
          <div class="text-muted small">Below 0%: ${stats.below_zero === null || stats.below_zero === undefined ? "n/a" : formatByFmt("number", stats.below_zero)} | Above 50%: ${stats.above_fifty === null || stats.above_fifty === undefined ? "n/a" : formatByFmt("number", stats.above_fifty)}</div>
        </div>`;
    }
    if (els.marginRiskList) {
      const risks = Array.isArray(profitability.margin_risk) ? profitability.margin_risk : [];
      if (!risks.length) {
        els.marginRiskList.innerHTML = '<li class="text-muted">No margin risks detected.</li>';
      } else {
        els.marginRiskList.innerHTML = risks
          .slice(0, 5)
          .map((r) => {
            const link = buildDrillLink("product", r.entity_id || r.label);
            const label = r.label || "Unknown";
            const risk = r.risk ? r.risk.replace(/_/g, " ") : "risk";
            let value = formatByFmt("percent", r.margin_pct);
            if (r.risk === "margin_drop" && r.margin_delta !== null && r.margin_delta !== undefined) {
              value = `${formatByFmt("number", r.margin_delta)} pp`;
            } else if (r.risk === "below_target" && r.gap_to_target !== null && r.gap_to_target !== undefined) {
              value = `gap ${formatByFmt("number", r.gap_to_target)} pp`;
            }
            return `<li class="d-flex justify-content-between align-items-center">
              <span>${link ? `<a href="${link}" class="text-decoration-none">${label}</a>` : label}</span>
              <span class="text-muted">${risk} (${value})</span>
            </li>`;
          })
          .join("");
      }
    }
  };

  const renderCustomerMomentum = (ops = {}) => {
    if (!els.customerMomentum) return;
    const c = ops.customers || {};
    const a = ops.activity || {};
    const current = c.current || 0;
    const prev = c.previous || 0;
    const newShare = current ? (c.new / current) * 100 : null;
    const prevShare = prev ? (c.new_prev / prev) * 100 : null;
    const shareDelta = newShare !== null && prevShare !== null ? newShare - prevShare : null;
    els.customerMomentum.innerHTML = `
      <div class="mb-2">
        <div class="text-muted small">New vs Returning (current)</div>
        <div class="fw-semibold">New ${formatByFmt("number", c.new)} | Returning ${formatByFmt("number", c.returning)}</div>
        <div class="text-muted small">New share ${formatByFmt("percent", newShare)}${shareDelta !== null ? ` (delta ${formatByFmt("percent", shareDelta)})` : ""}</div>
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
      const items = Array.isArray(rows) ? rows.slice(0, 5) : [];
      if (!items.length) return "";
      const list = items
        .map((r) => `<li><span>${r.label || "Unknown"}</span><span>${formatByFmt("percent", r.share)}</span></li>`)
        .join("");
      return `<div class="mb-3"><div class="text-muted small">${title}</div><ul class="list-unstyled mini-list mb-0">${list}</ul></div>`;
    };
    const html = `${renderList("Regions", mix.region)}${renderList("Methods", mix.method)}${renderList("Suppliers", mix.supplier)}`;
    els.opsMixPanel.innerHTML = html || '<div class="text-muted">No mix data available.</div>';
  };

  const renderWeekday = (ops = {}) => {
    if (!els.weekdayChart) return;
    destroyChart("weekday");
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
      data: { labels, datasets: [{ label: "Revenue", data: values, backgroundColor: "rgba(13,110,253,0.25)", borderColor: "#0d6efd", borderWidth: 1 }] },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
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
    if (els.driversTable) {
      els.driversTable.innerHTML = '<div class="text-muted small">Loading driver decomposition...</div>';
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

  const renderForecastMeta = (data = {}) => {
    const warnings = Array.isArray(data.warnings) ? data.warnings.filter(Boolean) : [];
    if (els.forecastWarnings) {
      els.forecastWarnings.textContent = warnings.join(" - ");
    }
    if (els.forecastModel) {
      const modelInfo = data.model_info || {};
      const modelName = modelInfo.name || data.model_used;
      const historyPoints = modelInfo.n_points ?? data.history_points ?? 0;
      if (modelName) {
        const conf = data.confidence ? ` - ${data.confidence} confidence` : "";
        const train = data.last_train_date ? ` - Train: ${data.last_train_date}` : "";
        els.forecastModel.textContent = `Model: ${modelName} - History: ${historyPoints} months${conf}${train}`;
        els.forecastModel.classList.remove("d-none");
      } else {
        els.forecastModel.classList.add("d-none");
        els.forecastModel.textContent = "";
      }
    }
    if (els.forecastQuality) {
      const smape =
        data.model_info && data.model_info.smape !== undefined
          ? data.model_info.smape
          : data.backtest && data.backtest.smape !== undefined
            ? data.backtest.smape
            : null;
      if (smape === null || smape === undefined || Number.isNaN(smape)) {
        els.forecastQuality.classList.add("d-none");
        els.forecastQuality.textContent = "";
      } else {
        const quality = Math.max(0, 100 - Number(smape));
        els.forecastQuality.textContent = `Forecast quality: ${fmtNumber1.format(quality)}% (SMAPE ${fmtNumber1.format(smape)}%)`;
        els.forecastQuality.classList.remove("d-none");
      }
    }
    if (warnings.length) {
      setForecastStatus(warnings[0], "warning");
    } else if ((data.series && data.series.length) || (data.forecast && data.forecast.length)) {
      const hitText = data.cache_hit ? "from cache" : "fresh";
      setForecastStatus(`Forecast ready (${hitText})`, "success");
    }
  };

  const renderForecastChart = (data = {}) => {
    if (!els.forecastChart) return;
    destroyChart("forecast");
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
      labels = points.map((p) => (p.month ? p.month.slice(0, 7) : ""));
      actual = points.map((p) => (p.actual === null || p.actual === undefined ? null : Number(p.actual)));
      forecast = points.map((p) => (p.yhat === null || p.yhat === undefined ? null : Number(p.yhat)));
      lower = points.map((p) => (p.yhat_lower === null || p.yhat_lower === undefined ? null : Number(p.yhat_lower)));
      upper = points.map((p) => (p.yhat_upper === null || p.yhat_upper === undefined ? null : Number(p.yhat_upper)));
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
    const yScale = { beginAtZero: state.forecast.metric === "revenue", ticks: { callback: (v) => yTick(v) } };
    if (state.forecast.metric === "margin") {
      const allVals = actual.concat(forecast).filter((v) => v !== null && v !== undefined && Number.isFinite(v));
      const minVal = allVals.length ? Math.min(...allVals) : -10;
      const maxVal = allVals.length ? Math.max(...allVals) : 10;
      yScale.suggestedMin = Math.max(-100, Math.min(minVal, -10));
      yScale.suggestedMax = Math.min(100, Math.max(maxVal, 10));
    }

    const datasets = [
      { type: "line", label: "Actual", data: actual, borderColor: "#0d6efd", backgroundColor: "rgba(13,110,253,0.12)", tension: 0.25 },
      { type: "line", label: "Forecast", data: forecast, borderColor: "#6f42c1", backgroundColor: "rgba(111,66,193,0.08)", borderDash: [6, 4], tension: 0.25 },
    ];
    if (hasBand) {
      datasets.push({
        type: "line",
        label: "Lower CI",
        data: lower,
        borderColor: "rgba(111,66,193,0.01)",
        backgroundColor: "rgba(111,66,193,0.08)",
        pointRadius: 0,
        fill: false,
        tension: 0.25,
      });
      datasets.push({
        type: "line",
        label: "Upper CI",
        data: upper,
        borderColor: "rgba(111,66,193,0.01)",
        backgroundColor: "rgba(111,66,193,0.08)",
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
        plugins: { legend: { display: true, position: "bottom" } },
      },
    });
  };

  const renderForecastState = (data = state.forecast.data || {}) => {
    renderForecastChart(data);
    renderForecastMeta(data);
    if (els.forecastFiltersNotice) {
      els.forecastFiltersNotice.classList.toggle("d-none", !state.forecast.stale);
    }
    if (state.forecast.stale) {
      setForecastStatus("Filters changed - run forecast again.", "warning");
    } else if (data && data.error) {
      setForecastStatus(data.error, "warning");
    } else if (!data || (!data.series || !data.series.length) && (!data.forecast || !data.forecast.length)) {
      setForecastStatus("Click Run Forecast to generate predictions.", "muted");
    }
  };

  const markForecastStale = () => {
    if (!state.forecast.data || !state.forecast.lastFilters) return;
    const current = window.location.search || "";
    state.forecast.stale = current !== state.forecast.lastFilters;
    if (state.forecast.stale) {
      setForecastStatus("Filters changed - run forecast again.", "warning");
    }
    renderForecastState();
  };

  const runForecast = async () => {
    if (!els.forecastRunBtn) return;
    setForecastLoading(true);
    setForecastStatus("Running forecast...", "primary");
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
      state.forecast.data = data || {};
      state.forecast.lastFilters = window.location.search || "";
      state.forecast.stale = false;
      renderForecastState(state.forecast.data);
    } catch (err) {
      setForecastStatus(err && err.message ? `Forecast failed: ${err.message}` : "Forecast failed", "danger");
    } finally {
      setForecastLoading(false);
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
    if (els.lastRefresh && meta.last_refresh) els.lastRefresh.textContent = meta.last_refresh;
    const w = meta.window || {};
    if (els.dataWindow) {
      const start = w.start || null;
      const end = w.end || null;
      els.dataWindow.textContent = start && end ? `${start} -> ${end}` : "Not available";
    }
    const filters = meta.filters || {};
    const parts = [];
    const dateLabel = filters.start || filters.end ? `${filters.start || "start"} -> ${filters.end || "end"}` : null;
    if (dateLabel) parts.push(dateLabel);
    ["regions", "methods", "customers", "suppliers", "products", "sales_reps"].forEach((key) => {
      const values = resolveLabels(key, filters[key], meta);
      if (Array.isArray(values) && values.length) {
        parts.push(`${key}: ${values.slice(0, 2).join(", ")}${values.length > 2 ? "..." : ""}`);
      }
    });
    if (els.filterSummary) {
      els.filterSummary.textContent = parts.length ? parts.join(" | ") : "Default (last 3 closed months)";
    }
  };

  const renderAll = () => {
    const payload = state.payload || {};
    const meta = payload.meta || {};
    if (els.emptyState) {
      if (meta.has_data === false) els.emptyState.classList.remove("d-none");
      else els.emptyState.classList.add("d-none");
    }
    renderMeta(meta);
    renderKpis(payload);
    renderTrend(payload.trend || {});
    renderMix(payload.mix || {}, state.dim);
    renderPareto(payload.pareto || {}, state.dim);
    renderTopMovers(payload.top_movers || {}, state.dim);
    renderHealth(payload.health || {});
    applyCoverageBanner(payload.health || {});
    updateGlobalCoveragePanel(payload.health || {});
    const insightsPayload = state.insights.data || payload;
    renderInsights(insightsPayload.insights || {});
    renderDrivers(insightsPayload.drivers || {});
    renderConcentration(insightsPayload.concentration || {});
    renderProfitability(insightsPayload.profitability || {});
    renderOperations(payload.operations || {});
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
      renderTopMovers(state.payload?.top_movers || {}, state.dim);
    });
  };

  const wireTrendToggles = () => {
    els.trendToggles.forEach((toggle) => {
      toggle.addEventListener("change", () => {
        const key = toggle.dataset.trendToggle;
        state.trendOptions[key] = toggle.checked;
        renderTrend(state.payload?.trend || {});
      });
    });
  };

  const wireForecastControls = () => {
    if (els.forecastMetricButtons && els.forecastMetricButtons.length) {
      els.forecastMetricButtons.forEach((btn) => {
        btn.addEventListener("click", () => {
          const metric = btn.dataset.forecastMetric;
          if (!metric) return;
          state.forecast.metric = metric;
          els.forecastMetricButtons.forEach((b) => b.classList.toggle("active", b === btn));
          if (state.forecast.data) {
            state.forecast.stale = true;
            renderForecastState(state.forecast.data);
          }
        });
      });
    }
    if (els.forecastHorizon) {
      els.forecastHorizon.value = state.forecast.horizon;
      els.forecastHorizon.addEventListener("change", () => {
        const val = parseInt(els.forecastHorizon.value, 10);
        state.forecast.horizon = [3, 6, 12].includes(val) ? val : 6;
        els.forecastHorizon.value = state.forecast.horizon;
      });
    }
    if (els.forecastRunBtn) {
      els.forecastRunBtn.addEventListener("click", () => runForecast());
    }
  };

  const setLoading = (loading) => {
    document.body.classList.toggle("loading", loading);
  };

  const consumeApplyId = () => {
    const applyId = currentApplyId;
    currentApplyId = null;
    return applyId;
  };

  const dispatchGlobalApplyAck = (detail = {}) => {
    const payload = { ...detail };
    const applyId = consumeApplyId();
    if (applyId && !payload.applyId) payload.applyId = applyId;
    try {
      if (typeof window.dispatchGlobalFiltersApplied === "function") {
        window.dispatchGlobalFiltersApplied(payload);
        return;
      }
      window.dispatchEvent(new CustomEvent("globalFilters:applied", { detail: payload }));
    } catch (_err) {
      /* ignore */
    }
  };

  const load = async (qsOverride) => {
    if (typeof Chart === "undefined") {
      setBanner("Chart.js failed to load. Check /static/vendor/chartjs/chart.umd.min.js.", "danger");
      return;
    }
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
    setLoading(true);
    try {
      const { data, notModified } = await fetchJson(url, controller.signal);
      if (notModified) {
        markForecastStale();
        loadInsights(lastAppliedQs);
        return;
      }
      state.payload = data || {};
      state.insights.data = null;
      state.insights.error = null;
      renderAll();
      loadInsights(lastAppliedQs);
      markForecastStale();
    } catch (err) { // eslint-disable-line no-unused-vars
      if (err?.name === "AbortError") return;
      setBanner(String(err && err.message ? err.message : err), "danger");
    } finally {
      if (version !== requestSeq) return;
      setLoading(false);
      dispatchGlobalApplyAck({ qs: lastAppliedQs, requestId: state.payload?.meta?.request_id });
    }
  };

  wireDimToggle();
  wireTrendToggles();
  wireForecastControls();
  stripDeprecatedWindowParamsFromUrl();
  const bootstrap = async (qsHint) => {
    if (bootstrapped) return;
    bootstrapped = true;
    let qs = qsHint;
    if (!qs) {
      const readyDetail = await waitForFiltersReady();
      qs = readyDetail?.qs;
    }
    load(qs);
  };

  const onApply = (evt) => {
    currentApplyId = evt?.detail?.applyId || null;
    const qs = sanitizeOverviewQs(evt?.detail?.qs || "");
    load(qs);
  };
  const onReady = (evt) => {
    const qs = sanitizeOverviewQs(evt?.detail?.qs || "");
    bootstrap(qs);
  };

  window.addEventListener("globalFilters:apply", onApply);
  window.addEventListener("globalFilters:ready", onReady);
  bootstrap();
})();
