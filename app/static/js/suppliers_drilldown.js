(() => {
  const meta = document.getElementById("SupplierDrilldownMeta");
  if (!meta) return;
  const authFetch = window.authFetch || fetch;

  if (document?.body?.dataset) {
    document.body.dataset.filtersHandler = "ajax";
  }

  const bundleUrl = meta.dataset.bundleUrl || "/suppliers/api/drilldown/bundle";
  const supplierId = meta.dataset.entityId || "";
  const showCosts = (() => {
    try {
      return JSON.parse(meta.dataset.showCosts || "true") !== false;
    } catch (err) {
      return true;
    }
  })();

  const state = {
    filterQs: (window.location.search || "").replace(/^\?/, ""),
    loading: false,
    bootstrapped: false,
    lastFetchKey: "",
  };

  let controller = null;
  let requestSeq = 0;
  let currentApplyId = null;

  const exportBtn = document.getElementById("supplierProductsExportBtn");

  const fmtMoney0 = new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  });
  const fmtMoney2 = new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  const fmtNum0 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
  const fmtPct1 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });

  const setText = (id, value) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = value;
  };

  const money0 = (val) => (val == null ? "—" : fmtMoney0.format(Number(val) || 0));
  const money2 = (val) => (val == null ? "—" : fmtMoney2.format(Number(val) || 0));
  const num0 = (val) => (val == null ? "—" : fmtNum0.format(Number(val) || 0));
  const pct1 = (val) => (val == null ? "—" : `${fmtPct1.format(Number(val) || 0)}%`);
  const isFiniteNumber = (val) => Number.isFinite(Number(val));

  const removeSkeleton = (id) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.remove("skeleton");
    el.querySelectorAll(".loading.shimmer").forEach((node) => node.remove());
  };

  const emptyChart = (id, message) => {
    const el = document.getElementById(id);
    if (!el) return;
    removeSkeleton(id);
    el.innerHTML = `<p class="text-muted text-center mt-3">${message}</p>`;
  };

  const aoaToCSV = (aoa) =>
    (aoa || [])
      .map((row) => row.map((v) => `"${String(v ?? "").replace(/"/g, '""')}"`).join(","))
      .join("\n");

  const downloadCSV = (aoa, name) => {
    const blob = new Blob([aoaToCSV(aoa)], { type: "text/csv;charset=utf-8;" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${name}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  // Existing template buttons call these helpers by name.
  window.exportPlotly = (divId, name) => {
    const gd = document.getElementById(divId);
    if (!gd || !gd.data || !gd.data.length) return;
    const traces = gd.data;
    const hasX = traces.some((t) => Array.isArray(t.x));
    const header = [hasX ? "X" : "Index", ...traces.map((t, i) => t.name || `Trace ${i + 1}`)];
    const maxLen = Math.max(
      0,
      ...traces.map((t) => (t.x?.length || t.y?.length || t.values?.length || 0))
    );
    const rows = [header];
    const xTrace = traces.find((t) => Array.isArray(t.x));
    for (let i = 0; i < maxLen; i += 1) {
      const xVal = xTrace?.x?.[i];
      const row = [hasX ? (xVal ?? i + 1) : i + 1];
      traces.forEach((t) => {
        const v = Array.isArray(t.y) ? t.y[i] : Array.isArray(t.values) ? t.values[i] : "";
        row.push(v ?? "");
      });
      rows.push(row);
    }
    downloadCSV(rows, name);
  };

  window.exportBarsCSV = (divId, name) => {
    const gd = document.getElementById(divId);
    if (!gd || !gd.data || !gd.data.length) return;
    const t = gd.data[0];
    const labels = Array.isArray(t.y) ? t.hovertext || t.y : t.x || [];
    const values = Array.isArray(t.x) ? t.x : t.y || [];
    const rows = [["Label", "Revenue"]];
    (labels || []).forEach((lab, i) => rows.push([lab, values?.[i] ?? ""]));
    downloadCSV(rows, name);
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
        console.warn("[supplier drilldown] filtersReady rejected", err);
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

  const topNValue = () => {
    const topNEl = document.getElementById("topN");
    return topNEl && topNEl.value ? topNEl.value : "";
  };

  const buildParams = () => {
    const params = new URLSearchParams(state.filterQs || "");
    params.set("supplier_id", supplierId);
    const topN = topNValue();
    if (topN) params.set("topN", topN);
    return params;
  };

  const replaceHistory = () => {
    if (!window.history || typeof window.history.replaceState !== "function") return;
    const nextUrl = state.filterQs ? `${window.location.pathname}?${state.filterQs}` : window.location.pathname;
    window.history.replaceState({}, "", nextUrl);
  };

  const updateExportHref = () => {
    if (!exportBtn) return;
    const params = new URLSearchParams(state.filterQs || "");
    const topN = topNValue();
    if (topN) params.set("topN", topN);
    const qs = params.toString();
    exportBtn.href = qs
      ? `/api/suppliers/${encodeURIComponent(String(supplierId))}/products/export.csv?${qs}`
      : `/api/suppliers/${encodeURIComponent(String(supplierId))}/products/export.csv`;
  };

  const updateHeader = (kpis = {}) => {
    const h2 = document.querySelector(".row.align-items-center h2");
    if (!h2) return;
    const name = kpis.supplier_name || supplierId || "Unknown";
    const sid = kpis.supplier_id || supplierId || "—";
    h2.innerHTML = `Supplier: ${name} <span class="text-muted">(ID: ${sid})</span>`;
  };

  const computeTrendStats = (trend = {}) => {
    const labels = Array.isArray(trend.labels) ? trend.labels : [];
    const revenue = Array.isArray(trend.revenue) ? trend.revenue : [];
    const monthsActive = labels.length;
    const revenueSum = revenue.reduce((acc, v) => acc + (Number(v) || 0), 0);
    const avgMonthly = monthsActive > 0 ? revenueSum / monthsActive : null;
    const lastMonth = monthsActive > 0 ? Number(revenue[monthsActive - 1]) || 0 : null;
    const prevMonth = monthsActive > 1 ? Number(revenue[monthsActive - 2]) || 0 : null;
    let momPct = null;
    if (lastMonth != null && prevMonth != null && prevMonth > 0) {
      momPct = ((lastMonth - prevMonth) / prevMonth) * 100;
    }
    return { monthsActive, avgMonthly, lastMonth, momPct };
  };

  const renderKpis = (payload = {}) => {
    const kpis = payload.kpis || {};
    const charts = payload.charts || {};
    const trend = payload.trend || {};
    const prodConc = (charts.top_products && charts.top_products.concentration) || {};
    const custConc = (charts.top_customers && charts.top_customers.concentration) || {};
    const unitStats = (charts.unit_price && charts.unit_price.stats) || {};
    const marginStats = charts.margin_stats || {};

    updateHeader(kpis);

    const trendStats = computeTrendStats(trend);
    const daysSinceLast = kpis.days_since_last_order;
    const lastSoldText =
      isFiniteNumber(daysSinceLast) && Number(daysSinceLast) >= 0
        ? ` · ${num0(daysSinceLast)}d since last order`
        : "";

    setText("kpiTotalRevenue", money0(kpis.revenue));
    setText("kpiMonthsActive", `${num0(trendStats.monthsActive)} months active${lastSoldText}`);
    setText("kpiAvgMonthly", money0(trendStats.avgMonthly));
    setText("kpiLastMonth", money0(trendStats.lastMonth));

    const momEl = document.getElementById("kpiMoM");
    if (momEl) {
      momEl.classList.remove("text-success", "text-danger");
      if (trendStats.momPct == null || Number.isNaN(Number(trendStats.momPct))) {
        momEl.textContent = "MoM: —";
      } else {
        const momVal = Number(trendStats.momPct) || 0;
        momEl.textContent = `MoM: ${pct1(momVal)}`;
        if (momVal > 0) momEl.classList.add("text-success");
        if (momVal < 0) momEl.classList.add("text-danger");
      }
    }

    setText("kpiTopProdShare", pct1(prodConc.top1_share));
    setText("kpiProdHHI", `HHI: ${Math.round(Number(prodConc.hhi || 0))}`);
    setText("kpiTopCustShare", pct1(custConc.top1_share));
    setText("kpiCustHHI", `HHI: ${Math.round(Number(custConc.hhi || 0))}`);
    setText("kpiUP50", money2(unitStats.p50));

    if (showCosts && document.getElementById("kpiM50")) {
      const m50 = marginStats.p50;
      setText("kpiM50", m50 == null ? "—" : pct1(m50));
    }
  };

  const rollingAvg = (values, windowSize = 3) => {
    const out = [];
    let sum = 0;
    for (let i = 0; i < values.length; i += 1) {
      sum += Number(values[i]) || 0;
      if (i >= windowSize) sum -= Number(values[i - windowSize]) || 0;
      out.push(i >= windowSize - 1 ? sum / windowSize : null);
    }
    return out;
  };

  const renderTrend = (trend = {}) => {
    const labels = Array.isArray(trend.labels) ? trend.labels : [];
    const revenue = Array.isArray(trend.revenue) ? trend.revenue : [];
    const profit = Array.isArray(trend.profit) ? trend.profit : [];

    if (!window.Plotly) {
      emptyChart("supTrend", "Plotly not loaded.");
      return;
    }
    if (!labels.length || !revenue.length) {
      emptyChart("supTrend", "No data available.");
      return;
    }
    removeSkeleton("supTrend");

    const traces = [
      {
        x: labels,
        y: revenue,
        type: "bar",
        name: "Revenue",
        hovertemplate: "%{x}: %{y:$,.0f}<extra></extra>",
      },
    ];

    const ma3 = rollingAvg(revenue, 3);
    traces.push({
      x: labels,
      y: ma3,
      type: "scatter",
      mode: "lines+markers",
      name: "3-mo Avg",
      line: { dash: "dot" },
      hovertemplate: "%{x}: %{y:$,.0f}<extra></extra>",
    });

    if (showCosts && profit.some(isFiniteNumber)) {
      traces.push({
        x: labels,
        y: profit,
        type: "scatter",
        mode: "lines+markers",
        name: "Profit",
        hovertemplate: "%{x}: %{y:$,.0f}<extra></extra>",
      });
    }

    window.Plotly.newPlot(
      "supTrend",
      traces,
      {
        margin: { t: 10, r: 20, b: 50, l: 60 },
        xaxis: { tickangle: -40, automargin: true },
        yaxis: { title: "Revenue", tickformat: "$,.0f" },
        hovermode: "x unified",
        height: 380,
      },
      { displayModeBar: false, responsive: true }
    );
  };

  const hBar = (divId, labels, values) => {
    const el = document.getElementById(divId);
    if (!el) return;
    if (!window.Plotly) {
      emptyChart(divId, "Plotly not loaded.");
      return;
    }
    if (!labels || !labels.length) {
      emptyChart(divId, "No data available.");
      return;
    }
    removeSkeleton(divId);
    window.Plotly.newPlot(
      divId,
      [
        {
          x: values,
          y: labels.map((s) => (s && s.length > 48 ? `${s.slice(0, 45)}…` : s)),
          type: "bar",
          orientation: "h",
          hovertext: labels,
          hovertemplate: "<b>%{hovertext}</b><br>Revenue: %{x:$,.0f}<extra></extra>",
        },
      ],
      {
        margin: { t: 10, r: 20, b: 30, l: 10 },
        xaxis: { title: "Revenue", tickformat: "$,.0f" },
        yaxis: { automargin: true },
        height: 360,
      },
      { displayModeBar: false, responsive: true }
    );
  };

  const metricData = (rows, metricKey) => {
    const labels = [];
    const values = [];
    (rows || []).forEach((row) => {
      if (!row) return;
      const label = row.product_name || row.product_id || "";
      const raw = row[metricKey];
      const num = Number(raw);
      if (label && Number.isFinite(num)) {
        labels.push(label);
        values.push(num);
      }
    });
    return { labels, values };
  };

  const renderMetricChart = (divId, data, opts = {}) => {
    const el = document.getElementById(divId);
    if (!el) return;
    if (!window.Plotly) {
      emptyChart(divId, "Plotly not loaded.");
      return;
    }
    const labels = data.labels || [];
    const values = data.values || [];
    if (!labels.length) {
      emptyChart(divId, "No data available.");
      return;
    }
    removeSkeleton(divId);
    window.Plotly.newPlot(
      divId,
      [
        {
          x: values,
          y: labels.map((s) => (s && s.length > 48 ? `${s.slice(0, 45)}…` : s)),
          type: "bar",
          orientation: "h",
          hovertext: labels,
          hovertemplate: opts.hoverTemplate || "<b>%{hovertext}</b><br>Value: %{x:,.2f}<extra></extra>",
        },
      ],
      {
        margin: { t: 10, r: 20, b: 30, l: 10 },
        xaxis: {
          title: opts.xTitle || "",
          tickformat: opts.tickFormat || ",.2f",
          ticksuffix: opts.tickSuffix || "",
        },
        yaxis: { automargin: true },
        height: 360,
      },
      { displayModeBar: false, responsive: true }
    );
  };

  const renderUnitPriceHist = (unitPrice = {}) => {
    const values = Array.isArray(unitPrice.values) ? unitPrice.values : [];
    const stats = unitPrice.stats || {};
    if (!window.Plotly) {
      emptyChart("upHist", "Plotly not loaded.");
      return;
    }
    if (!values.length) {
      emptyChart("upHist", "No unit price samples.");
      return;
    }
    removeSkeleton("upHist");

    const shapes = [];
    const addLine = (val, color) => {
      if (!isFiniteNumber(val)) return;
      shapes.push({
        type: "line",
        x0: Number(val),
        x1: Number(val),
        y0: 0,
        y1: 1,
        xref: "x",
        yref: "paper",
        line: { dash: "dot", width: 1.5, color },
      });
    };
    addLine(stats.p10, "#6c757d");
    addLine(stats.p50, "#0d6efd");
    addLine(stats.p90, "#198754");

    window.Plotly.newPlot(
      "upHist",
      [
        {
          x: values,
          type: "histogram",
          nbinsx: 40,
          name: "Unit Price",
          hovertemplate: "%{x:$,.2f}<extra></extra>",
        },
      ],
      {
        margin: { t: 10, r: 20, b: 50, l: 60 },
        xaxis: { title: "Unit Price", tickformat: "$,.2f" },
        yaxis: { title: "Count" },
        shapes,
        height: 360,
      },
      { displayModeBar: false, responsive: true }
    );
  };

  const renderCharts = (payload = {}) => {
    const trend = payload.trend || {};
    const charts = payload.charts || {};
    const topProducts = charts.top_products || {};
    const topCustomers = charts.top_customers || {};
    const rows = (payload.table && payload.table.rows) || topProducts.rows || [];

    renderTrend(trend);
    hBar("prodBar", topProducts.labels || [], topProducts.values || []);
    hBar("custBar", topCustomers.labels || [], topCustomers.values || []);

    const avgSale = metricData(rows, "avg_sale_price");
    if (avgSale.labels.length) {
      renderMetricChart("prodAvgPrice", avgSale, {
        xTitle: "Avg Sale Price",
        tickFormat: "$,.2f",
        hoverTemplate: "<b>%{hovertext}</b><br>Avg Sale Price: %{x:$,.2f}<extra></extra>",
      });
    } else {
      emptyChart("prodAvgPrice", "No data available.");
    }

    if (showCosts) {
      const marginData = metricData(rows, "margin_pct");
      if (marginData.labels.length) {
        renderMetricChart("prodMetricSecondary", marginData, {
          xTitle: "Margin %",
          tickFormat: ",.1f",
          tickSuffix: "%",
          hoverTemplate: "<b>%{hovertext}</b><br>Margin: %{x:.1f}%<extra></extra>",
        });
      } else {
        emptyChart("prodMetricSecondary", "No margin data available.");
      }
    } else {
      const unitsData = metricData(rows, "units");
      if (unitsData.labels.length) {
        renderMetricChart("prodMetricSecondary", unitsData, {
          xTitle: "Units",
          tickFormat: ",.0f",
          hoverTemplate: "<b>%{hovertext}</b><br>Units: %{x:,.0f}<extra></extra>",
        });
      } else {
        emptyChart("prodMetricSecondary", "No unit data available.");
      }
    }

    renderUnitPriceHist(charts.unit_price || {});
  };

  const buildFetchKey = (params) => `${bundleUrl}?${params.toString()}`;

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
      // no-op
    }
  };

  const fetchBundle = async () => {
    const params = buildParams();
    const fetchKey = buildFetchKey(params);
    if (state.lastFetchKey === fetchKey) return;

    state.lastFetchKey = fetchKey;
    state.loading = true;
    updateExportHref();
    const currentSeq = ++requestSeq;

    let payload = null;
    let error = null;

    try {
      if (controller) controller.abort();
      controller = new AbortController();
      const res = await authFetch(fetchKey, {
        signal: controller.signal,
        headers: { Accept: "application/json" },
      });
      if (!res.ok) {
        const text = await res.text();
        console.error("[supplier drilldown] bundle failed", res.status, text.slice(0, 400));
        throw new Error(`Drilldown bundle request failed (${res.status})`);
      }
      const json = await res.json();
      payload = window.normalizeBundlePayload ? window.normalizeBundlePayload(json) : json;
      renderKpis(payload);
      renderCharts(payload);
      updateExportHref();
    } catch (err) {
      if (err?.name === "AbortError") return;
      error = err;
      console.error("[supplier drilldown] bundle error", err);
      emptyChart("supTrend", "Unable to load trend.");
      emptyChart("prodBar", "Unable to load products.");
      emptyChart("custBar", "Unable to load customers.");
      emptyChart("prodAvgPrice", "Unable to load product pricing.");
      emptyChart("prodMetricSecondary", "Unable to load product metrics.");
      emptyChart("upHist", "Unable to load unit price distribution.");
    } finally {
      if (currentSeq !== requestSeq) return;
      state.loading = false;
      state.lastFetchKey = "";
      dispatchGlobalApplyAck({
        page: "supplier_drilldown",
        qs: state.filterQs,
        supplier_id: supplierId,
        error: error ? String(error) : null,
        cached: Boolean(payload?.meta?.cached),
        duckdb_query_count: payload?.meta?.duckdb_query_count ?? null,
      });
    }
  };

  const applyFilters = (qs) => {
    state.filterQs = (qs || "").replace(/^\?/, "");
    state.lastFetchKey = "";
    replaceHistory();
    fetchBundle();
  };

  const onApply = (evt) => {
    currentApplyId = evt?.detail?.applyId || null;
    const qs = (evt?.detail && evt.detail.qs) || "";
    applyFilters(qs);
  };

  const bootstrap = async () => {
    if (state.bootstrapped) return;
    state.bootstrapped = true;
    const readyDetail = await waitForFiltersReady();
    const qs = (readyDetail && readyDetail.qs) || state.filterQs || "";
    applyFilters(qs);
  };

  window.addEventListener("globalFilters:apply", onApply);
  updateExportHref();
  bootstrap();
  setTimeout(() => {
    if (!state.bootstrapped) bootstrap();
  }, 900);
})();
