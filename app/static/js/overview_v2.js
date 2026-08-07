(() => {
  const page = document.getElementById("overviewPageV2");
  if (!page) return;

  if (document?.body?.dataset) {
    document.body.dataset.filtersHandler = "ajax";
  }

  const authFetch = window.authFetch || fetch;
  const etags = new Map();
  const charts = { trend: null, forecast: null };
  const DEPRECATED_WINDOW_PARAMS = ["include_current_month", "include_current", "include_current_months"];
  const forecastV2Enabled = String(page.dataset.overviewForecastV2 || "0") === "1";
  const moversFastEnabled = String(page.dataset.overviewMoversFast || "0") === "1";

  const state = {
    context: null,
    trendFreq: "monthly",
    trendMetric: "revenue",
    trendRolling: true,
    moversDim: "customer",
    moversSort: "delta_abs",
    moversRows: [],
    moversMeta: null,
    moversCache: {},
    forecastMetric: "revenue",
    forecastHorizon: 6,
    forecastGranularity: "monthly",
    lastForecastPayload: null,
  };

  const fmtCurrency0 = new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  });
  const fmtCurrency1 = new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 1,
  });
  const fmtNumber0 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
  const fmtNumber1 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });

  const els = {
    banner: document.getElementById("overviewBanner"),
    empty: document.getElementById("overviewEmpty"),
    filterSummaryText: document.getElementById("filterSummaryText"),
    lastRefreshChip: document.getElementById("lastRefreshChip"),
    dataWindowChip: document.getElementById("dataWindowChip"),
    rowsChip: document.getElementById("rowsChip"),
    ordersChip: document.getElementById("ordersChip"),
    customersChip: document.getElementById("customersChip"),
    costCoverageChip: document.getElementById("costCoverageChip"),
    packsCoverageChip: document.getElementById("packsCoverageChip"),
    missingMappingChip: document.getElementById("missingMappingChip"),
    freshnessChip: document.getElementById("freshnessChip"),
    downloadSnapshotBtn: document.getElementById("downloadSnapshotBtn"),
    trendExportBtn: document.getElementById("trendExportBtn"),
    driversExportBtn: document.getElementById("driversExportBtn"),
    moversExportBtn: document.getElementById("moversExportBtn"),
    exportDataHealthBtn: document.getElementById("exportDataHealthBtn"),
    whatChangedList: document.getElementById("whatChangedList"),
    watchoutsList: document.getElementById("watchoutsList"),
    actionsList: document.getElementById("actionsList"),
    trendFreqSelect: document.getElementById("trendFreqSelect"),
    trendMetricSelect: document.getElementById("trendMetricSelect"),
    trendRollingToggle: document.getElementById("trendRollingToggle"),
    trendEmpty: document.getElementById("trendEmpty"),
    trendChart: document.getElementById("trendChart"),
    driversMethodNote: document.getElementById("driversMethodNote"),
    driversMomRows: document.getElementById("driversMomRows"),
    driversYoyRows: document.getElementById("driversYoyRows"),
    moversDimToggle: document.getElementById("moversDimToggle"),
    moversSortSelect: document.getElementById("moversSortSelect"),
    moversMinBaseline: document.getElementById("moversMinBaseline"),
    moversExcludeLowBase: document.getElementById("moversExcludeLowBase"),
    moversApplyBtn: document.getElementById("moversApplyBtn"),
    moversMetaText: document.getElementById("moversMetaText"),
    moversReconcileText: document.getElementById("moversReconcileText"),
    moversGainersBody: document.getElementById("moversGainersBody"),
    moversDeclinersBody: document.getElementById("moversDeclinersBody"),
    concentrationSummary: document.getElementById("concentrationSummary"),
    profitabilitySummary: document.getElementById("profitabilitySummary"),
    marginRiskList: document.getElementById("marginRiskList"),
    healthCostBadge: document.getElementById("healthCostBadge"),
    healthPacksBadge: document.getElementById("healthPacksBadge"),
    healthMappingBadge: document.getElementById("healthMappingBadge"),
    healthFreshnessBadge: document.getElementById("healthFreshnessBadge"),
    dataHealthIssuesList: document.getElementById("dataHealthIssuesList"),
    forecastGateMessage: document.getElementById("forecastGateMessage"),
    forecastMeta: document.getElementById("forecastMeta"),
    forecastMetricSelect: document.getElementById("forecastMetricSelect"),
    forecastHorizonSelect: document.getElementById("forecastHorizonSelect"),
    forecastGranularitySelect: document.getElementById("forecastGranularitySelect"),
    forecastRunBtn: document.getElementById("forecastRunBtn"),
    forecastDownloadBtn: document.getElementById("forecastDownloadBtn"),
    forecastEmpty: document.getElementById("forecastEmpty"),
    forecastChart: document.getElementById("forecastChart"),
    forecastModelName: document.getElementById("forecastModelName"),
    forecastQuality: document.getElementById("forecastQuality"),
    forecastHistory: document.getElementById("forecastHistory"),
    forecastConfidence: document.getElementById("forecastConfidence"),
  };

  const metricDefs = {
    revenue: { fmt: "currency", deltaKey: "revenue" },
    profit: { fmt: "currency", deltaKey: "profit" },
    margin_pct: { fmt: "percent", deltaKey: "margin_pct", deltaAsPp: true },
    orders: { fmt: "number", deltaKey: "orders" },
    customers: { fmt: "number", deltaKey: "customers" },
    qty: { fmt: "number", deltaKey: "qty" },
    weight: { fmt: "number", deltaKey: "weight" },
    aov: { fmt: "currency", deltaKey: "aov" },
    asp: { fmt: "currency", deltaKey: "asp" },
    profit_per_order: { fmt: "currency", deltaKey: null },
    profit_per_lb: { fmt: "currency", deltaKey: null },
  };

  function sanitizeQs(raw) {
    const val = typeof raw === "string" ? raw : "";
    const normalized = val.startsWith("?") ? val.slice(1) : val;
    const params = new URLSearchParams(normalized);
    DEPRECATED_WINDOW_PARAMS.forEach((name) => params.delete(name));
    return params.toString();
  }

  function currentFilterQs() {
    try {
      const statePayload = window.getGlobalFilterState ? window.getGlobalFilterState() : null;
      if (statePayload && typeof statePayload.qs === "string") {
        return sanitizeQs(statePayload.qs);
      }
    } catch (_err) {
      // Ignore and fallback.
    }
    return sanitizeQs(window.location.search || "");
  }

  function withQs(baseUrl, extraParams) {
    const [basePath, existingQuery] = String(baseUrl || "").split("?");
    const params = new URLSearchParams(existingQuery || "");
    const filterQs = currentFilterQs();
    if (filterQs) {
      const filterParams = new URLSearchParams(filterQs);
      for (const [key, val] of filterParams.entries()) {
        params.set(key, val);
      }
    }
    if (extraParams && typeof extraParams === "object") {
      Object.entries(extraParams).forEach(([key, val]) => {
        if (val === undefined || val === null || val === "") {
          params.delete(key);
        } else {
          params.set(key, String(val));
        }
      });
    }
    const qs = sanitizeQs(params.toString());
    return qs ? `${basePath}?${qs}` : basePath;
  }

  function setBanner(message, variant = "warning") {
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
  }

  function maybeShowEmpty(isEmpty) {
    if (!els.empty) return;
    if (isEmpty) {
      els.empty.classList.remove("d-none");
    } else {
      els.empty.classList.add("d-none");
    }
  }

  function applyEtag(url, headers) {
    const etag = etags.get(url);
    if (etag) headers["If-None-Match"] = etag;
  }

  async function fetchJson(url, options = {}) {
    const headers = {};
    applyEtag(url, headers);
    const fetchOpts = { headers };
    if (options.signal) fetchOpts.signal = options.signal;
    const resp = await authFetch(url, fetchOpts);
    if (resp.status === 304) return { notModified: true };
    if (!resp.ok) {
      let detail = "";
      try {
        const body = await resp.clone().json();
        detail = body?.error || body?.detail || JSON.stringify(body);
      } catch (_e) {
        try {
          detail = await resp.text();
        } catch (_e2) {
          detail = "";
        }
      }
      throw new Error(detail || `Request failed (${resp.status})`);
    }
    const etag = resp.headers.get("ETag");
    if (etag) etags.set(url, etag);
    return { data: await resp.json() };
  }

  function fmtCurrency(value, compact = false) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "n/a";
    const num = Number(value);
    if (!compact) return fmtCurrency0.format(num);
    const abs = Math.abs(num);
    if (abs >= 1_000_000_000) return `${fmtCurrency1.format(num / 1_000_000_000)}B`;
    if (abs >= 1_000_000) return `${fmtCurrency1.format(num / 1_000_000)}M`;
    if (abs >= 10_000) return `${fmtCurrency1.format(num / 1_000)}K`;
    return fmtCurrency0.format(num);
  }

  function fmtNumber(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "n/a";
    return fmtNumber0.format(Number(value));
  }

  function fmtPercent(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "n/a";
    return `${fmtNumber1.format(Number(value))}%`;
  }

  function formatByKind(value, kind) {
    if (kind === "currency") return fmtCurrency(value, true);
    if (kind === "percent") return fmtPercent(value);
    return fmtNumber(value);
  }

  function formatDelta(value, kind, asPp = false) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "n/a";
    const num = Number(value);
    const sign = num > 0 ? "+" : "";
    if (kind === "currency") return `${sign}${fmtCurrency(num)}`;
    if (kind === "percent") {
      if (asPp) return `${sign}${fmtNumber1.format(num)} pp`;
      return `${sign}${fmtNumber1.format(num)}%`;
    }
    return `${sign}${fmtNumber1.format(num)}`;
  }

  function valueCell(metric, value) {
    const node = document.querySelector(`[data-value="${metric}"]`);
    if (!node) return;
    const def = metricDefs[metric] || { fmt: "number" };
    node.textContent = formatByKind(value, def.fmt);
  }

  function deltaCell(metric, deltaAbs, deltaPct, periodLabel) {
    const node = document.querySelector(`[data-${periodLabel}="${metric}"]`);
    if (!node) return;
    const def = metricDefs[metric] || { fmt: "number", deltaAsPp: false };
    const absText = formatDelta(deltaAbs, def.fmt, Boolean(def.deltaAsPp));
    const pctText =
      deltaPct === null || deltaPct === undefined || Number.isNaN(Number(deltaPct))
        ? "n/a"
        : `${Number(deltaPct) > 0 ? "+" : ""}${fmtNumber1.format(Number(deltaPct))}%`;
    node.textContent = `${periodLabel.toUpperCase()}: ${absText} (${pctText})`;
    node.classList.remove("text-muted", "text-success", "text-danger");
    if (deltaAbs === null || deltaAbs === undefined || Number.isNaN(Number(deltaAbs))) {
      node.classList.add("text-muted");
      return;
    }
    if (Number(deltaAbs) > 0) node.classList.add("text-success");
    else if (Number(deltaAbs) < 0) node.classList.add("text-danger");
    else node.classList.add("text-muted");
  }

  function summarizeFilters(meta) {
    const labels = meta?.filter_labels || {};
    const buckets = [
      ["regions", "Regions"],
      ["methods", "Methods"],
      ["customers", "Customers"],
      ["suppliers", "Suppliers"],
      ["products", "Products"],
      ["sales_reps", "Sales reps"],
    ];
    const parts = [];
    buckets.forEach(([key, title]) => {
      const vals = Array.isArray(labels[key]) ? labels[key] : [];
      if (!vals.length) return;
      const preview = vals.slice(0, 2).join(", ");
      const suffix = vals.length > 2 ? ` +${vals.length - 2}` : "";
      parts.push(`${title}: ${preview}${suffix}`);
    });
    const start = meta?.window?.start || meta?.filters?.start;
    const end = meta?.window?.end || meta?.filters?.end;
    const range = start || end ? `${start || "start"} to ${end || "end"}` : "window not set";
    return parts.length ? `${range} | ${parts.join(" | ")}` : `${range} | no extra filters`;
  }

  function renderHeader(context) {
    const meta = context?.meta || {};
    const bundle = context?.bundle || {};
    const health = context?.data_health || {};
    const kpis = bundle?.kpis || {};
    const windowObj = meta?.window || {};

    if (els.filterSummaryText) els.filterSummaryText.textContent = summarizeFilters(meta);
    if (els.lastRefreshChip) els.lastRefreshChip.textContent = meta?.last_refresh || page.dataset.lastRefresh || "unknown";
    if (els.dataWindowChip) {
      const start = windowObj?.start || "n/a";
      const end = windowObj?.end || "n/a";
      els.dataWindowChip.textContent = `${start} → ${end}`;
    }

    if (els.rowsChip) els.rowsChip.textContent = `Rows: ${fmtNumber(health?.rows)}`;
    if (els.ordersChip) els.ordersChip.textContent = `Orders: ${fmtNumber(kpis?.orders)}`;
    if (els.customersChip) els.customersChip.textContent = `Customers: ${fmtNumber(kpis?.customers)}`;
    if (els.costCoverageChip) {
      els.costCoverageChip.textContent = `Cost coverage: ${fmtPercent(health?.cost_coverage_pct)}`;
    }
    if (els.packsCoverageChip) {
      els.packsCoverageChip.textContent = `Packs coverage: ${fmtPercent(health?.packs_coverage_pct)}`;
    }
    if (els.missingMappingChip) {
      els.missingMappingChip.textContent = `Missing mapping: ${fmtNumber(health?.product_mapping_missing)}`;
    }
    if (els.freshnessChip) {
      const days = health?.freshness_sla_days;
      els.freshnessChip.textContent = `Freshness SLA: ${days === null || days === undefined ? "n/a" : `${fmtNumber(days)}d`}`;
    }
  }

  function renderScorecard(context) {
    const score = context?.scorecard_kpis || {};
    const bundle = context?.bundle || {};
    const deltas = bundle?.deltas || {};
    Object.keys(metricDefs).forEach((metric) => {
      valueCell(metric, score[metric]);
      const metricDef = metricDefs[metric] || {};
      if (!metricDef.deltaKey) {
        deltaCell(metric, null, null, "mom");
        deltaCell(metric, null, null, "yoy");
        return;
      }

      let momAbs = null;
      let momPct = null;
      let yoyAbs = null;
      let yoyPct = null;

      if (metricDef.deltaKey === "revenue") {
        momAbs = score.revenue_mom;
        momPct = score.revenue_mom_pct;
        yoyAbs = score.revenue_yoy;
        yoyPct = score.revenue_yoy_pct;
      } else if (metricDef.deltaKey === "profit") {
        momAbs = score.profit_mom;
        momPct = score.profit_mom_pct;
        yoyAbs = score.profit_yoy;
        yoyPct = score.profit_yoy_pct;
      } else if (metricDef.deltaKey === "margin_pct") {
        momAbs = score.margin_mom;
        momPct = score.margin_mom_pct;
        yoyAbs = score.margin_yoy;
        yoyPct = score.margin_yoy_pct;
      } else {
        const bucket = deltas?.[metricDef.deltaKey] || {};
        momAbs = bucket?.mom;
        momPct = bucket?.mom_pct;
        yoyAbs = bucket?.yoy;
        yoyPct = bucket?.yoy_pct;
      }

      deltaCell(metric, momAbs, momPct, "mom");
      deltaCell(metric, yoyAbs, yoyPct, "yoy");
    });
  }

  function renderList(el, lines, fallback) {
    if (!el) return;
    const items = Array.isArray(lines) ? lines.filter(Boolean) : [];
    if (!items.length) {
      el.innerHTML = `<li class="text-muted">${fallback}</li>`;
      return;
    }
    el.innerHTML = items.map((line) => `<li>${String(line)}</li>`).join("");
  }

  function buildActionLinks() {
    const base = currentFilterQs();
    const qs = base ? `?${base}` : "";
    const links = {};
    links.margin = `/products/${qs ? qs + "&" : "?"}quick_filter=below_target_margin`;
    links.movers = `#moversSection`;
    return links;
  }

  function renderNarrative(context) {
    const insights = context?.narrative_insights || {};
    const narrative = Array.isArray(insights?.narrative) ? insights.narrative : [];
    const watchouts = Array.isArray(insights?.watchouts) ? insights.watchouts : [];
    const callouts = Array.isArray(insights?.callouts) ? insights.callouts : [];
    const calloutLines = callouts
      .slice(0, 3)
      .map((item) => {
        const title = item?.title || "Insight";
        const detail = item?.detail || "";
        return detail ? `${title}: ${detail}` : title;
      });
    renderList(els.whatChangedList, [...narrative, ...calloutLines].slice(0, 6), "No material movement for selected window.");
    renderList(els.watchoutsList, watchouts.slice(0, 4), "No watchouts detected.");
    if (els.actionsList) {
      const links = buildActionLinks();
      const marginLink = els.actionsList.querySelector("#actionMarginRiskLink");
      if (marginLink) marginLink.setAttribute("href", links.margin);
      const moversLink = els.actionsList.querySelector("#actionMoversLink");
      if (moversLink) moversLink.setAttribute("href", links.movers);
    }
  }

  function destroyChart(name) {
    const existing = charts[name];
    if (existing && typeof existing.destroy === "function") {
      existing.destroy();
    }
    charts[name] = null;
  }

  function movingAverage(values, window = 3) {
    const out = [];
    for (let i = 0; i < values.length; i += 1) {
      const slice = values.slice(Math.max(0, i - window + 1), i + 1).filter((v) => v !== null && v !== undefined && Number.isFinite(Number(v)));
      if (!slice.length) out.push(null);
      else out.push(slice.reduce((sum, val) => sum + Number(val), 0) / slice.length);
    }
    return out;
  }

  function metricAxisFormatter(metric, raw) {
    const n = Number(raw);
    if (!Number.isFinite(n)) return "";
    if (metric === "margin_pct") return `${fmtNumber1.format(n)}%`;
    if (metric === "units") return fmtNumber0.format(n);
    return fmtCurrency(n, true);
  }

  function renderTrend(context) {
    const trend = context?.trend_series || {};
    const freq = state.trendFreq;
    const metric = state.trendMetric;
    const block = trend?.[freq] || trend?.monthly || trend;
    const rawLabels = Array.isArray(block?.labels) && block.labels.length ? block.labels : block?.months;
    const labels = Array.isArray(rawLabels) ? rawLabels.map((x) => String(x)) : [];
    const metricSeries = Array.isArray(block?.[metric]) ? block[metric] : [];

    if (!els.trendChart || !window.Chart) {
      if (els.trendEmpty) {
        els.trendEmpty.classList.remove("d-none");
        els.trendEmpty.textContent = "Chart.js unavailable; trend chart cannot be rendered.";
      }
      return;
    }

    destroyChart("trend");
    if (!labels.length || !metricSeries.length) {
      if (els.trendEmpty) els.trendEmpty.classList.remove("d-none");
      return;
    }
    if (els.trendEmpty) els.trendEmpty.classList.add("d-none");

    const series = metricSeries.map((v) => (v === null || v === undefined ? null : Number(v)));
    const datasets = [
      {
        label: metric === "margin_pct" ? "Margin %" : metric.charAt(0).toUpperCase() + metric.slice(1),
        data: series,
        borderColor: "#1f4f9d",
        backgroundColor: "rgba(31,79,157,0.12)",
        borderWidth: 2,
        tension: 0.28,
        pointRadius: 2,
      },
    ];
    if (state.trendRolling) {
      datasets.push({
        label: "Rolling avg (3)",
        data: movingAverage(series, 3),
        borderColor: "#f08c00",
        borderWidth: 2,
        borderDash: [5, 4],
        pointRadius: 0,
        tension: 0.2,
      });
    }

    const ctx = els.trendChart.getContext("2d");
    charts.trend = new window.Chart(ctx, {
      type: "line",
      data: { labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: true, labels: { boxWidth: 12 } },
          tooltip: {
            callbacks: {
              label: (item) => `${item.dataset.label}: ${metricAxisFormatter(metric, item.raw)}`,
            },
          },
        },
        scales: {
          x: {
            ticks: { autoSkip: true, maxRotation: 0, minRotation: 0 },
            grid: { color: "rgba(0,0,0,0.06)" },
          },
          y: {
            ticks: { callback: (val) => metricAxisFormatter(metric, val) },
            grid: { color: "rgba(0,0,0,0.08)" },
          },
        },
      },
    });
  }

  function renderDriverRows(target, block) {
    if (!target) return;
    const fallback = '<tr><td colspan="3" class="text-muted">No decomposition data.</td></tr>';
    if (!block || typeof block !== "object") {
      target.innerHTML = fallback;
      return;
    }
    let rows = Array.isArray(block?.drivers) ? block.drivers : [];
    if (!rows.length) {
      rows = [
        { driver: "Price", delta: block?.price_effect, share_of_delta_pct: null },
        { driver: "Volume", delta: block?.volume_effect, share_of_delta_pct: null },
        { driver: "Mix", delta: block?.mix_effect, share_of_delta_pct: null },
      ].filter((r) => r.delta !== null && r.delta !== undefined);
    }
    if (!rows.length) {
      target.innerHTML = fallback;
      return;
    }
    target.innerHTML = rows
      .map((row) => {
        const delta = Number(row?.delta);
        const signClass = Number.isFinite(delta) ? (delta > 0 ? "text-success" : delta < 0 ? "text-danger" : "text-muted") : "text-muted";
        const shareText =
          row?.share_of_delta_pct === null || row?.share_of_delta_pct === undefined
            ? "n/a"
            : `${Number(row.share_of_delta_pct) > 0 ? "+" : ""}${fmtNumber1.format(Number(row.share_of_delta_pct))}%`;
        return `<tr>
          <td>${row?.driver || "Driver"}</td>
          <td class="text-end ${signClass}">${formatDelta(row?.delta, "currency")}</td>
          <td class="text-end">${shareText}</td>
        </tr>`;
      })
      .join("");
  }

  function renderDrivers(context) {
    const drivers = context?.drivers || {};
    const method = drivers?.methodology?.name || "Symmetric price/volume/mix decomposition.";
    if (els.driversMethodNote) els.driversMethodNote.textContent = `Method: ${method}`;
    const momRevenue = (drivers?.mom || {}).revenue || {};
    const yoyRevenue = (drivers?.yoy || {}).revenue || {};
    renderDriverRows(els.driversMomRows, momRevenue);
    renderDriverRows(els.driversYoyRows, yoyRevenue);
  }

  function renderMoverTable(target, rows) {
    if (!target) return;
    if (!rows.length) {
      target.innerHTML = '<tr><td colspan="4" class="text-muted">No rows</td></tr>';
      return;
    }
    target.innerHTML = rows
      .map((row) => {
        const delta = Number(row?.delta || 0);
        const deltaClass = delta > 0 ? "text-success" : delta < 0 ? "text-danger" : "text-muted";
        const pctLabelRaw = String(row?.delta_pct_label || "").trim();
        let pctDisplay = "n/a";
        if (pctLabelRaw) {
          pctDisplay = `<span class="badge text-bg-light border">${pctLabelRaw}</span>`;
        } else if (row?.delta_pct !== null && row?.delta_pct !== undefined && Number.isFinite(Number(row.delta_pct))) {
          const pctNum = Number(row.delta_pct);
          pctDisplay = `${pctNum > 0 ? "+" : ""}${fmtNumber1.format(pctNum)}%`;
        }
        return `<tr>
          <td>${row?.label || "Unknown"}</td>
          <td class="text-end">${fmtCurrency(row?.current)}</td>
          <td class="text-end ${deltaClass}">${formatDelta(row?.delta, "currency")}</td>
          <td class="text-end">${pctDisplay}</td>
        </tr>`;
      })
      .join("");
  }

  function sortMovers(rows) {
    const all = Array.isArray(rows) ? rows.slice() : [];
    if (state.moversSort === "delta_pct") {
      all.sort((a, b) => Math.abs(Number(b?.delta_pct || 0)) - Math.abs(Number(a?.delta_pct || 0)));
      return all;
    }
    all.sort((a, b) => Math.abs(Number(b?.delta || 0)) - Math.abs(Number(a?.delta || 0)));
    return all;
  }

  function renderMovers() {
    const sorted = sortMovers(state.moversRows || []);
    const gainers = sorted.filter((row) => Number(row?.delta || 0) > 0).slice(0, 10);
    const decliners = sorted.filter((row) => Number(row?.delta || 0) < 0).slice(0, 10);
    renderMoverTable(els.moversGainersBody, gainers);
    renderMoverTable(els.moversDeclinersBody, decliners);

    if (els.moversMetaText) {
      const g = state.moversMeta?.guardrails || {};
      if (Object.keys(g).length) {
        els.moversMetaText.textContent = `Rows: ${fmtNumber(state.moversMeta?.rows)} | low-base threshold ${fmtCurrency(g.min_baseline)} | filtered ${fmtNumber(g.rows_filtered)}`;
      } else {
        els.moversMetaText.textContent = `Rows: ${fmtNumber(state.moversMeta?.rows || sorted.length)}`;
      }
    }

    if (els.moversReconcileText) {
      const totalDelta = sorted.reduce((sum, row) => sum + Number(row?.delta || 0), 0);
      const expected = Number((state.context?.bundle?.deltas || {}).revenue?.mom || 0);
      const residual = expected - totalDelta;
      els.moversReconcileText.textContent = `Reconcile check (dimension ${state.moversDim}): movers sum ${fmtCurrency(totalDelta)} vs window delta ${fmtCurrency(expected)} (residual ${fmtCurrency(residual)}).`;
    }
  }

  async function loadMoversLegacy() {
    const base = `${page.dataset.drilldownBase || "/overview/api/drilldown"}/movers`;
    const minBaseline = Math.max(0, Number(els.moversMinBaseline?.value || 0));
    const excludeLowBase = Boolean(els.moversExcludeLowBase?.checked);
    const url = withQs(base, {
      dimension: state.moversDim,
      format: "json",
      min_baseline: minBaseline,
      min_new_current: minBaseline,
      min_lost_prior: minBaseline,
      exclude_low_base: excludeLowBase ? "1" : "0",
    });
    const result = await fetchJson(url);
    if (result.notModified) return;
    const payload = result.data || {};
    state.moversRows = Array.isArray(payload?.rows) ? payload.rows : [];
    state.moversMeta = payload?.meta || {};
    renderMovers();
  }

  async function loadMoversFast() {
    const minBaseline = Math.max(0, Number(els.moversMinBaseline?.value || 0));
    const excludeLowBase = Boolean(els.moversExcludeLowBase?.checked);
    const cacheKey = `${state.moversDim}|${minBaseline}|${excludeLowBase ? 1 : 0}`;
    if (state.moversCache[cacheKey]) {
      const cached = state.moversCache[cacheKey];
      state.moversRows = cached.rows || [];
      state.moversMeta = cached.meta || {};
      renderMovers();
      return;
    }

    const base = page.dataset.moversApi || "/overview/api/movers";
    const url = withQs(base, {
      dimension: state.moversDim,
      min_baseline: minBaseline,
      min_new_current: minBaseline,
      min_lost_prior: minBaseline,
      exclude_low_base: excludeLowBase ? "1" : "0",
    });
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    let result = null;
    try {
      result = await fetchJson(url, { signal: controller.signal });
    } catch (err) {
      if (els.moversMetaText) {
        els.moversMetaText.textContent = err?.name === "AbortError" ? "Movers request timed out. Try again." : `Movers load failed: ${err?.message || "unknown error"}`;
      }
      return;
    } finally {
      clearTimeout(timeout);
    }
    if (!result) return;
    if (result.notModified) return;
    const payload = result.data || {};
    state.moversRows = Array.isArray(payload?.rows) ? payload.rows : [];
    state.moversMeta = payload?.meta || {};
    state.moversCache[cacheKey] = { rows: state.moversRows, meta: state.moversMeta };
    renderMovers();
  }

  async function loadMovers() {
    if (moversFastEnabled) {
      await loadMoversFast();
      return;
    }
    await loadMoversLegacy();
  }

  function renderRisk(context) {
    const risk = context?.risk || {};
    const concentration = risk?.concentration || {};
    const customer = concentration?.customer || {};
    const product = concentration?.product || {};
    const profitability = risk?.profitability || {};
    const marginStats = profitability?.margin_pct || {};
    const marginRisk = Array.isArray(profitability?.margin_risk) ? profitability.margin_risk : [];

    if (els.concentrationSummary) {
      els.concentrationSummary.innerHTML = [
        `Customer concentration: Top 1 ${fmtPercent(customer?.top1_share)}, Top 5 ${fmtPercent(customer?.top5_share)}, HHI ${fmtNumber(customer?.hhi)}.`,
        `Product concentration: Top 1 ${fmtPercent(product?.top1_share)}, Top 5 ${fmtPercent(product?.top5_share)}, HHI ${fmtNumber(product?.hhi)}.`,
      ].join("<br>");
    }
    if (els.profitabilitySummary) {
      els.profitabilitySummary.innerHTML = [
        `Margin dispersion (P10/P50/P90): ${fmtPercent(marginStats?.p10)} / ${fmtPercent(marginStats?.p50)} / ${fmtPercent(marginStats?.p90)}.`,
        `Negative margin SKUs: ${fmtNumber(marginStats?.negative_count)} on ${fmtCurrency(marginStats?.negative_revenue)} revenue.`,
      ].join("<br>");
    }
    renderList(
      els.marginRiskList,
      marginRisk.slice(0, 5).map((row) => {
        const label = row?.product || row?.label || "Unknown SKU";
        return `${label}: margin ${fmtPercent(row?.margin_pct)}, revenue ${fmtCurrency(row?.revenue)}`;
      }),
      "No margin risk SKUs detected."
    );
  }

  function renderDataHealth(context) {
    const health = context?.data_health || {};
    if (els.healthCostBadge) els.healthCostBadge.textContent = `Cost coverage: ${fmtPercent(health?.cost_coverage_pct)}`;
    if (els.healthPacksBadge) els.healthPacksBadge.textContent = `Packs coverage: ${fmtPercent(health?.packs_coverage_pct)}`;
    if (els.healthMappingBadge) els.healthMappingBadge.textContent = `Missing mapping: ${fmtNumber(health?.product_mapping_missing)}`;
    if (els.healthFreshnessBadge) {
      const days = health?.freshness_sla_days;
      els.healthFreshnessBadge.textContent = `Freshness SLA: ${days === null || days === undefined ? "n/a" : `${fmtNumber(days)}d`}`;
    }

    const issues = [];
    if (health?.cost_coverage_pct !== null && health?.cost_coverage_pct !== undefined && Number(health.cost_coverage_pct) < 90) {
      issues.push(`Cost coverage is ${fmtPercent(health.cost_coverage_pct)}; profit metrics may be understated.`);
    }
    if (health?.packs_coverage_pct !== null && health?.packs_coverage_pct !== undefined && Number(health.packs_coverage_pct) < 98) {
      issues.push(`Packs coverage is ${fmtPercent(health.packs_coverage_pct)}; weighted metrics may be noisy.`);
    }
    if (Number(health?.product_mapping_missing || 0) > 0) {
      issues.push(`${fmtNumber(health.product_mapping_missing)} row(s) have missing product mapping.`);
    }
    renderList(els.dataHealthIssuesList, issues, "No material data health issues for current filters.");
  }

  function renderForecastSeries(payload) {
    if (!els.forecastChart || !window.Chart) {
      if (els.forecastEmpty) {
        els.forecastEmpty.textContent = "Chart.js unavailable; forecast chart cannot be rendered.";
      }
      return;
    }

    destroyChart("forecast");
    const points = Array.isArray(payload?.series) ? payload.series : [];
    if (!points.length) {
      if (els.forecastEmpty) els.forecastEmpty.textContent = "No forecast output available.";
      return;
    }
    if (els.forecastEmpty) els.forecastEmpty.textContent = "";

    const v2Shape = points.length > 0 && Object.prototype.hasOwnProperty.call(points[0], "t");
    const labels = points.map((p) => String(v2Shape ? p?.t : p?.month || p?.period || ""));
    const actual = points.map((p) => {
      const raw = v2Shape ? p?.actual : p?.actual ?? p?.y;
      return raw === null || raw === undefined ? null : Number(raw);
    });
    const yhat = points.map((p) => {
      const raw = v2Shape ? p?.forecast : p?.yhat;
      return raw === null || raw === undefined ? null : Number(raw);
    });
    const lower = points.map((p) => {
      const raw = v2Shape ? p?.lo : p?.yhat_lower;
      return raw === null || raw === undefined ? null : Number(raw);
    });
    const upper = points.map((p) => {
      const raw = v2Shape ? p?.hi : p?.yhat_upper;
      return raw === null || raw === undefined ? null : Number(raw);
    });

    let boundaryIndex = -1;
    for (let i = 0; i < points.length; i += 1) {
      if (yhat[i] !== null && yhat[i] !== undefined && actual[i] === null) {
        boundaryIndex = i;
        break;
      }
    }

    const metric = state.forecastMetric;
    const ctx = els.forecastChart.getContext("2d");
    charts.forecast = new window.Chart(ctx, {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "Actual",
            data: actual,
            borderColor: "#334155",
            backgroundColor: "rgba(51,65,85,0.12)",
            borderWidth: 2,
            pointRadius: 2,
            tension: 0.2,
          },
          {
            label: "Forecast",
            data: yhat,
            borderColor: "#0f766e",
            borderDash: [5, 4],
            borderWidth: 2,
            pointRadius: 2,
            tension: 0.2,
          },
          {
            label: "Lower CI",
            data: lower,
            borderColor: "rgba(15,118,110,0.0)",
            backgroundColor: "rgba(15,118,110,0.14)",
            pointRadius: 0,
          },
          {
            label: "Upper CI",
            data: upper,
            borderColor: "rgba(15,118,110,0.0)",
            backgroundColor: "rgba(15,118,110,0.14)",
            pointRadius: 0,
            fill: "-1",
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: {
            labels: {
              filter: (item) => item.text !== "Lower CI" && item.text !== "Upper CI",
            },
          },
          tooltip: {
            callbacks: {
              label: (item) => `${item.dataset.label}: ${metricAxisFormatter(metric === "margin" ? "margin_pct" : metric, item.raw)}`,
            },
          },
        },
        scales: {
          x: { ticks: { autoSkip: true, maxRotation: 0, minRotation: 0 } },
          y: { ticks: { callback: (val) => metricAxisFormatter(metric === "margin" ? "margin_pct" : metric, val) } },
        },
      },
    });

    if (boundaryIndex >= 0 && els.forecastMeta) {
      const mark = labels[boundaryIndex];
      const text = els.forecastMeta.textContent || "";
      els.forecastMeta.textContent = text ? `${text} | Forecast starts: ${mark}` : `Forecast starts: ${mark}`;
    }
  }

  function renderForecastGate(context) {
    const gate = context?.forecast || {};
    const enabled = forecastV2Enabled ? true : Boolean(gate?.enabled);
    if (els.forecastRunBtn) els.forecastRunBtn.disabled = !enabled;
    if (els.forecastMetricSelect) els.forecastMetricSelect.disabled = !enabled;
    if (els.forecastHorizonSelect) els.forecastHorizonSelect.disabled = !enabled;
    if (els.forecastGranularitySelect) els.forecastGranularitySelect.disabled = !enabled || !forecastV2Enabled;
    if (els.forecastDownloadBtn) els.forecastDownloadBtn.disabled = true;
    if (els.forecastGateMessage) {
      if (forecastV2Enabled) {
        els.forecastGateMessage.textContent = "Run forecast to evaluate eligibility for selected granularity.";
        els.forecastGateMessage.classList.remove("text-danger");
      } else if (enabled) {
        els.forecastGateMessage.textContent = `Eligible for forecast (${fmtNumber(gate?.history_points)} history points).`;
        els.forecastGateMessage.classList.remove("text-danger");
      } else {
        els.forecastGateMessage.textContent = gate?.reason || "Forecast unavailable for current window.";
        els.forecastGateMessage.classList.add("text-danger");
      }
    }
    if (forecastV2Enabled) {
      if (els.forecastModelName) els.forecastModelName.textContent = "Model: -";
      if (els.forecastQuality) els.forecastQuality.textContent = "Quality: -";
      if (els.forecastHistory) els.forecastHistory.textContent = "History: -";
      if (els.forecastConfidence) els.forecastConfidence.textContent = "Confidence: -";
    }
    if (els.forecastMeta) {
      if (forecastV2Enabled) {
        els.forecastMeta.textContent = "Model and quality details appear after running.";
      } else {
        els.forecastMeta.textContent = `Minimum required history: ${fmtNumber(gate?.min_history_points)} points`;
      }
    }
  }

  function updateForecastModelCardV2(payload) {
    const model = payload?.model || {};
    const smape = model?.smape;
    const quality = smape === null || smape === undefined ? "n/a" : `${fmtNumber1.format(Number(smape))}% sMAPE`;
    if (els.forecastModelName) els.forecastModelName.textContent = `Model: ${model?.name || "n/a"}`;
    if (els.forecastQuality) els.forecastQuality.textContent = `Quality: ${quality}`;
    if (els.forecastHistory) els.forecastHistory.textContent = `History: ${fmtNumber(model?.train_points)}`;
    if (els.forecastConfidence) els.forecastConfidence.textContent = `Confidence: ${model?.confidence || "n/a"}`;
  }

  function csvEscape(val) {
    if (val === null || val === undefined) return "";
    const s = String(val);
    if (s.includes('"') || s.includes(",") || s.includes("\n")) {
      return `"${s.replace(/"/g, '""')}"`;
    }
    return s;
  }

  function downloadForecastCsv() {
    const payload = state.lastForecastPayload;
    if (!forecastV2Enabled || !payload || !Array.isArray(payload.series) || !payload.series.length) return;
    const rows = payload.series.map((row) => [row.t, row.actual, row.forecast, row.lo, row.hi]);
    const header = ["period", "actual", "forecast", "lo", "hi"];
    const lines = [header, ...rows].map((vals) => vals.map(csvEscape).join(","));
    const blob = new Blob([`${lines.join("\n")}\n`], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    const gran = state.forecastGranularity || "monthly";
    a.download = `overview_forecast_${state.forecastMetric}_${gran}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  async function runForecast() {
    if (!forecastV2Enabled && !state.context?.forecast?.enabled) return;
    const url = withQs(page.dataset.forecastApi || "/api/overview/forecast", {
      metric: state.forecastMetric,
      horizon_months: state.forecastHorizon,
      granularity: state.forecastGranularity,
      v2: forecastV2Enabled ? "1" : undefined,
    });
    if (els.forecastRunBtn) els.forecastRunBtn.disabled = true;
    if (els.forecastMeta) els.forecastMeta.textContent = "Running forecast...";
    state.lastForecastPayload = null;
    if (els.forecastDownloadBtn) els.forecastDownloadBtn.disabled = true;
    try {
      const result = await fetchJson(url);
      if (result.notModified) return;
      const payload = result.data || {};
      if (forecastV2Enabled) {
        if (!payload?.eligible) {
          if (els.forecastGateMessage) {
            els.forecastGateMessage.textContent = payload?.reason || "Forecast unavailable for the selected settings.";
            els.forecastGateMessage.classList.add("text-danger");
          }
          if (els.forecastMeta) {
            const notes = Array.isArray(payload?.notes) ? payload.notes.join(" | ") : "";
            els.forecastMeta.textContent = notes || "Try widening the date window.";
          }
          destroyChart("forecast");
          if (els.forecastEmpty) els.forecastEmpty.textContent = "Forecast unavailable.";
          updateForecastModelCardV2(payload);
          return;
        }
        if (els.forecastGateMessage) {
          els.forecastGateMessage.textContent = "Forecast generated using rolling backtest model selection.";
          els.forecastGateMessage.classList.remove("text-danger");
        }
        renderForecastSeries(payload);
        updateForecastModelCardV2(payload);
        if (els.forecastMeta) {
          const notes = Array.isArray(payload?.notes) ? payload.notes.join(" | ") : "";
          els.forecastMeta.textContent = notes || "Forecast generated.";
        }
        state.lastForecastPayload = payload;
        if (els.forecastDownloadBtn) els.forecastDownloadBtn.disabled = false;
      } else {
        renderForecastSeries(payload);
        if (els.forecastMeta) {
          const info = payload?.model_info || {};
          const backtest = payload?.backtest || {};
          const model = payload?.model_used || info?.model_name || "n/a";
          const smape = backtest?.smape;
          const quality = smape === null || smape === undefined ? "n/a" : `${fmtNumber1.format(Number(smape))}% sMAPE`;
          els.forecastMeta.textContent = `Model: ${model} | Quality: ${quality} | History: ${fmtNumber(payload?.history_points)}`;
        }
      }
    } catch (err) {
      if (els.forecastMeta) els.forecastMeta.textContent = err?.message || "Forecast request failed.";
    } finally {
      if (els.forecastRunBtn) els.forecastRunBtn.disabled = false;
    }
  }

  function exportUrl(baseUrl, extra) {
    return withQs(baseUrl, extra);
  }

  function wireExports() {
    if (els.downloadSnapshotBtn) {
      els.downloadSnapshotBtn.addEventListener("click", () => {
        const url = exportUrl(page.dataset.snapshotExportUrl || "/overview/api/export/snapshot", { format: "xlsx", dataset: "all" });
        window.location.assign(url);
      });
    }
    if (els.trendExportBtn) {
      els.trendExportBtn.addEventListener("click", () => {
        const url = exportUrl(page.dataset.trendExportUrl || "/overview/api/export/trend", {
          format: "xlsx",
          freq: state.trendFreq,
        });
        window.location.assign(url);
      });
    }
    if (els.driversExportBtn) {
      els.driversExportBtn.addEventListener("click", () => {
        const url = exportUrl(page.dataset.snapshotExportUrl || "/overview/api/export/snapshot", {
          format: "xlsx",
          dataset: "drivers_mom",
        });
        window.location.assign(url);
      });
    }
    if (els.moversExportBtn) {
      els.moversExportBtn.addEventListener("click", () => {
        const minBaseline = Math.max(0, Number(els.moversMinBaseline?.value || 0));
        const url = exportUrl(`${page.dataset.drilldownBase || "/overview/api/drilldown"}/movers`, {
          format: "xlsx",
          dimension: state.moversDim,
          min_baseline: minBaseline,
          min_new_current: minBaseline,
          min_lost_prior: minBaseline,
          exclude_low_base: els.moversExcludeLowBase?.checked ? "1" : "0",
        });
        window.location.assign(url);
      });
    }
    if (els.exportDataHealthBtn) {
      els.exportDataHealthBtn.addEventListener("click", () => {
        const url = exportUrl(`${page.dataset.drilldownBase || "/overview/api/drilldown"}/data_health`, { format: "xlsx" });
        window.location.assign(url);
      });
    }
  }

  function wireTrendControls() {
    if (els.trendFreqSelect) {
      els.trendFreqSelect.value = state.trendFreq;
      els.trendFreqSelect.addEventListener("change", () => {
        state.trendFreq = els.trendFreqSelect.value || "monthly";
        renderTrend(state.context);
      });
    }
    if (els.trendMetricSelect) {
      els.trendMetricSelect.value = state.trendMetric;
      els.trendMetricSelect.addEventListener("change", () => {
        state.trendMetric = els.trendMetricSelect.value || "revenue";
        renderTrend(state.context);
      });
    }
    if (els.trendRollingToggle) {
      els.trendRollingToggle.checked = state.trendRolling;
      els.trendRollingToggle.addEventListener("change", () => {
        state.trendRolling = Boolean(els.trendRollingToggle.checked);
        renderTrend(state.context);
      });
    }
  }

  function wireMoversControls() {
    if (els.moversDimToggle) {
      const btns = Array.from(els.moversDimToggle.querySelectorAll("[data-movers-dim]"));
      btns.forEach((btn) => {
        btn.addEventListener("click", async () => {
          const dim = btn.getAttribute("data-movers-dim") || "customer";
          state.moversDim = dim;
          btns.forEach((node) => node.classList.toggle("active", node === btn));
          await loadMovers();
        });
      });
    }
    if (els.moversSortSelect) {
      state.moversSort = els.moversSortSelect.value || "delta_abs";
      els.moversSortSelect.addEventListener("change", () => {
        state.moversSort = els.moversSortSelect.value || "delta_abs";
        renderMovers();
      });
    }
    if (els.moversApplyBtn) {
      els.moversApplyBtn.addEventListener("click", async () => {
        await loadMovers();
      });
    }
  }

  function wireForecastControls() {
    if (els.forecastMetricSelect) {
      state.forecastMetric = els.forecastMetricSelect.value || "revenue";
      els.forecastMetricSelect.addEventListener("change", () => {
        state.forecastMetric = els.forecastMetricSelect.value || "revenue";
      });
    }
    if (els.forecastHorizonSelect) {
      state.forecastHorizon = Number(els.forecastHorizonSelect.value || 6);
      els.forecastHorizonSelect.addEventListener("change", () => {
        state.forecastHorizon = Number(els.forecastHorizonSelect.value || 6);
      });
    }
    if (els.forecastGranularitySelect) {
      if (!forecastV2Enabled) {
        els.forecastGranularitySelect.classList.add("d-none");
      } else {
        state.forecastGranularity = els.forecastGranularitySelect.value || "monthly";
        els.forecastGranularitySelect.addEventListener("change", () => {
          state.forecastGranularity = els.forecastGranularitySelect.value || "monthly";
        });
      }
    }
    if (els.forecastDownloadBtn) {
      if (!forecastV2Enabled) {
        els.forecastDownloadBtn.classList.add("d-none");
      } else {
        els.forecastDownloadBtn.addEventListener("click", () => downloadForecastCsv());
      }
    }
    if (els.forecastRunBtn) {
      els.forecastRunBtn.addEventListener("click", async () => {
        await runForecast();
      });
    }
  }

  function chartUnavailableMessage() {
    if (window.Chart) return;
    setBanner("Chart library is not loaded. Numeric insights and exports remain available.", "warning");
  }

  async function waitForFiltersReady() {
    const fallback = () => {
      try {
        return (window.getGlobalFilterState && window.getGlobalFilterState()) || {};
      } catch (_err) {
        return {};
      }
    };
    if (window.filtersReady && typeof window.filtersReady.then === "function") {
      try {
        const timeout = new Promise((resolve) => setTimeout(() => resolve(fallback()), 1500));
        await Promise.race([window.filtersReady, timeout]);
        return;
      } catch (_err) {
        return;
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }

  function renderContext(context) {
    state.context = context;
    const hasData = Boolean(context?.meta?.has_data);
    maybeShowEmpty(!hasData);
    renderHeader(context);
    renderScorecard(context);
    renderNarrative(context);
    renderTrend(context);
    renderDrivers(context);
    renderRisk(context);
    renderDataHealth(context);
    renderForecastGate(context);
  }

  function renderMoversLoading() {
    if (els.moversGainersBody) {
      els.moversGainersBody.innerHTML = '<tr><td colspan="4" class="text-muted">Loading movers…</td></tr>';
    }
    if (els.moversDeclinersBody) {
      els.moversDeclinersBody.innerHTML = '<tr><td colspan="4" class="text-muted">Loading movers…</td></tr>';
    }
    if (els.moversMetaText) {
      els.moversMetaText.textContent = "Fetching movers asynchronously...";
    }
  }

  async function loadContext() {
    const apiUrl = withQs(page.dataset.api || "/overview/api/context");
    const result = await fetchJson(apiUrl);
    if (result.notModified && state.context) return;
    const context = result.data || {};
    state.moversCache = {};
    renderContext(context);
    if (moversFastEnabled) {
      renderMoversLoading();
    }
    await loadMovers();
  }

  async function bootstrap() {
    chartUnavailableMessage();
    wireExports();
    wireTrendControls();
    wireMoversControls();
    wireForecastControls();
    await waitForFiltersReady();
    try {
      setBanner("");
      await loadContext();
    } catch (err) {
      setBanner(err?.message || "Failed to load Business Performance.", "danger");
      maybeShowEmpty(true);
    }

    const refresh = async () => {
      try {
        await loadContext();
      } catch (err) {
        setBanner(err?.message || "Refresh failed.", "danger");
      }
    };
    document.addEventListener("globalFilters:changed", refresh);
    window.addEventListener("globalFilters:changed", refresh);
    window.addEventListener("popstate", refresh);
  }

  bootstrap();
})();
