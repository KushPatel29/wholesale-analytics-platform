(() => {
  const root = document.getElementById("RegionDrilldownApp");
  if (!root) return;
  const authFetch = window.authFetch || fetch;

  if (document?.body?.dataset) {
    document.body.dataset.filtersHandler = "ajax";
  }

  const bundleUrl = root.dataset.bundleUrl || "/api/regions/drilldown/bundle";
  const regionId = root.dataset.entityId || "";
  let filterQs = (window.location.search || "").replace(/^\?/, "");
  let controller = null;
  let bootstrapped = false;
  let lastFetchKey = "";
  let fetchSeq = 0;
  let currentApplyId = null;

  let trendLabels = [];
  let trendRevenue = [];
  let topCustomers = [];
  let topProducts = [];
  let churnRows = [];
  let shippingMix = [];
  let weekdayRevenue = [];

  const fmtMoney0 = new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  });
  const fmtInt = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
  const fmtPct1 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });

  const setText = (id, value) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = value;
  };

  const fmtCurrency = (val) => (val == null ? "-" : fmtMoney0.format(Number(val) || 0));
  const fmtPercent = (val) => (val == null ? "-" : `${fmtPct1.format(Number(val) || 0)}%`);

  const aoaToCSV = (aoa) =>
    aoa
      .map((row) =>
        row.map((v) => `"${String(v ?? "").replace(/"/g, '""')}"`).join(",")
      )
      .join("\n");

  const downloadCSV = (aoa, name) => {
    const blob = new Blob([aoaToCSV(aoa)], { type: "text/csv;charset=utf-8;" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${name}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
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
        console.warn("[region drilldown] filtersReady rejected", err);
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

  const buildRequestQs = () => {
    const params = new URLSearchParams(filterQs || "");
    params.set("region_id", regionId);
    params.set("topN", "25");
    return params.toString();
  };

  const updateUrl = () => {
    if (!window.history || typeof window.history.replaceState !== "function") return;
    const nextUrl = filterQs ? `${window.location.pathname}?${filterQs}` : window.location.pathname;
    window.history.replaceState({}, "", nextUrl);
  };

  const setGrowth = (id, value) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.remove("text-success", "text-danger");
    if (value == null || Number.isNaN(Number(value))) {
      el.textContent = "-";
      return;
    }
    const num = Number(value) || 0;
    el.textContent = `${fmtPct1.format(num)}%`;
    if (num > 0) el.classList.add("text-success");
    if (num < 0) el.classList.add("text-danger");
  };

  const renderKpis = (kpis = {}) => {
    setText("regionKpiRevenue", fmtCurrency(kpis.revenue));
    setText("regionKpiOrders", fmtInt.format(kpis.orders || 0));
    setGrowth("regionKpiMom", kpis.mom_growth);
    setGrowth("regionKpiWow", kpis.wow_growth);
    setGrowth("regionKpiYoy", kpis.yoy_growth);

    setText("regionKpiCustomers", fmtInt.format(kpis.customers || 0));
    setText("regionKpiAov", fmtCurrency(kpis.avg_order_value));
    setText("regionKpiProfit", fmtCurrency(kpis.profit));
    setText("regionKpiMargin", kpis.margin_pct == null ? "-" : `${fmtPct1.format(kpis.margin_pct)}%`);
    setText("regionKpiRepeat", fmtPercent(kpis.repeat_pct));
    setText("regionKpiChurn", fmtPercent(kpis.churn_pct));
  };

  const renderTrend = () => {
    const chartEl = document.getElementById("region_rev_chart");
    const emptyEl = document.getElementById("regionTrendEmpty");
    if (!chartEl || !window.Plotly) return;
    const hasData = trendLabels.length > 0 && trendRevenue.some((v) => v !== 0);
    if (emptyEl) emptyEl.classList.toggle("d-none", hasData);
    if (!hasData) {
      chartEl.innerHTML = "";
      return;
    }
    const logScale = Boolean(document.getElementById("logScale")?.checked);
    const layout = {
      margin: { t: 10, r: 20, b: 40, l: 60 },
      yaxis: { title: "Revenue", tickformat: "$,.0f", type: logScale ? "log" : "linear" },
      xaxis: { automargin: true },
      height: 360,
      hovermode: "x unified",
    };
    const data = [
      {
        x: trendLabels,
        y: trendRevenue,
        type: "scatter",
        mode: "lines+markers",
        hovertemplate: "%{x}<br>%{y:$,.0f}<extra></extra>",
      },
    ];
    window.Plotly.newPlot(chartEl, data, layout, { displayModeBar: false, responsive: true });
  };

  const sliceTop = (rows, n) => {
    const copy = [...(rows || [])];
    copy.sort((a, b) => (Number(b.revenue) || 0) - (Number(a.revenue) || 0));
    return n > 0 ? copy.slice(0, n) : copy;
  };

  const renderTopCustomers = () => {
    const chartEl = document.getElementById("region_cust_bar");
    const emptyEl = document.getElementById("regionTopCustEmpty");
    if (!chartEl || !window.Plotly) return;
    const n = Number.parseInt(document.getElementById("custTopN")?.value || "10", 10) || 10;
    const rows = sliceTop(topCustomers, n);
    const labels = rows.map((r) => r.customer_name || r.customer_id || "Unknown");
    const values = rows.map((r) => Number(r.revenue) || 0);
    const hasData = labels.length > 0 && values.some((v) => v !== 0);
    if (emptyEl) emptyEl.classList.toggle("d-none", hasData);
    if (!hasData) {
      chartEl.innerHTML = "";
      return;
    }
    window.Plotly.newPlot(
      chartEl,
      [
        {
          x: values,
          y: labels.map((s) => (s && s.length > 42 ? `${s.slice(0, 39)}...` : s)),
          type: "bar",
          orientation: "h",
          hovertext: labels,
          hovertemplate: "<b>%{hovertext}</b><br>%{x:$,.0f}<extra></extra>",
        },
      ],
      {
        margin: { t: 10, r: 30, b: 30, l: 10 },
        xaxis: { tickformat: "$,.0f", title: "Revenue" },
        yaxis: { automargin: true },
        height: 360,
      },
      { displayModeBar: false, responsive: true }
    );
  };

  const renderTopProducts = () => {
    const chartEl = document.getElementById("region_prod_bar");
    const emptyEl = document.getElementById("regionTopProdEmpty");
    if (!chartEl || !window.Plotly) return;
    const n = Number.parseInt(document.getElementById("prodTopN")?.value || "10", 10) || 10;
    const rows = sliceTop(topProducts, n);
    const labels = rows.map((r) => r.product_name || r.product_id || "Unknown");
    const values = rows.map((r) => Number(r.revenue) || 0);
    const hasData = labels.length > 0 && values.some((v) => v !== 0);
    if (emptyEl) emptyEl.classList.toggle("d-none", hasData);
    if (!hasData) {
      chartEl.innerHTML = "";
      return;
    }
    window.Plotly.newPlot(
      chartEl,
      [
        {
          x: values,
          y: labels.map((s) => (s && s.length > 42 ? `${s.slice(0, 39)}...` : s)),
          type: "bar",
          orientation: "h",
          hovertext: labels,
          hovertemplate: "<b>%{hovertext}</b><br>%{x:$,.0f}<extra></extra>",
        },
      ],
      {
        margin: { t: 10, r: 30, b: 30, l: 10 },
        xaxis: { tickformat: "$,.0f", title: "Revenue" },
        yaxis: { automargin: true },
        height: 360,
      },
      { displayModeBar: false, responsive: true }
    );
  };

  const renderShippingMix = () => {
    const chartEl = document.getElementById("region_ship_mix");
    const emptyEl = document.getElementById("regionShipEmpty");
    if (!chartEl || !window.Plotly) return;
    const rows = sliceTop(shippingMix, 0).filter((r) => (r.method || "").trim() !== "");
    const labels = rows.map((r) => r.method || "Unknown");
    const pctValues = rows.map((r) => (r.pct == null ? null : Number(r.pct)));
    const revValues = rows.map((r) => Number(r.revenue) || 0);
    const usePct = pctValues.some((v) => v != null);
    const values = usePct ? pctValues.map((v) => (v == null ? 0 : v)) : revValues;
    const hasData = labels.length > 0 && values.some((v) => v !== 0);
    if (emptyEl) emptyEl.classList.toggle("d-none", hasData);
    if (!hasData) {
      chartEl.innerHTML = "";
      return;
    }
    window.Plotly.newPlot(
      chartEl,
      [
        {
          x: labels,
          y: values,
          type: "bar",
          hovertemplate: usePct ? "%{x}<br>%{y:.1f}%<extra></extra>" : "%{x}<br>%{y:$,.0f}<extra></extra>",
        },
      ],
      {
        margin: { t: 10, r: 20, b: 80, l: 60 },
        xaxis: { automargin: true },
        yaxis: usePct ? { title: "Share %", ticksuffix: "%" } : { title: "Revenue", tickformat: "$,.0f" },
        height: 320,
      },
      { displayModeBar: false, responsive: true }
    );
  };

  const weekdayLabels = {
    "0": "Sun",
    "1": "Mon",
    "2": "Tue",
    "3": "Wed",
    "4": "Thu",
    "5": "Fri",
    "6": "Sat",
  };
  const weekdayOrder = ["1", "2", "3", "4", "5", "6", "0"];

  const renderWeekdayRevenue = () => {
    const chartEl = document.getElementById("region_weekday_chart");
    const emptyEl = document.getElementById("regionWeekdayEmpty");
    if (!chartEl || !window.Plotly) return;
    const rows = [...(weekdayRevenue || [])];
    rows.sort((a, b) => weekdayOrder.indexOf(String(a.weekday)) - weekdayOrder.indexOf(String(b.weekday)));
    const labels = rows.map((r) => weekdayLabels[String(r.weekday)] || String(r.weekday));
    const values = rows.map((r) => Number(r.revenue) || 0);
    const hasData = labels.length > 0 && values.some((v) => v !== 0);
    if (emptyEl) emptyEl.classList.toggle("d-none", hasData);
    if (!hasData) {
      chartEl.innerHTML = "";
      return;
    }
    window.Plotly.newPlot(
      chartEl,
      [
        {
          x: labels,
          y: values,
          type: "bar",
          hovertemplate: "%{x}<br>%{y:$,.0f}<extra></extra>",
        },
      ],
      {
        margin: { t: 10, r: 20, b: 60, l: 60 },
        xaxis: { automargin: true },
        yaxis: { title: "Revenue", tickformat: "$,.0f" },
        height: 320,
      },
      { displayModeBar: false, responsive: true }
    );
  };

  const renderChurnRows = (rows) => {
    const tbody = document.getElementById("churnTbody");
    if (!tbody) return;
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="4" class="text-center text-muted">No churned customers for this region.</td></tr>';
      return;
    }
    tbody.innerHTML = rows
      .map((row) => {
        const cid = row.customer_id || row.customerId || "";
        const cname = row.customer_name || row.customerName || cid || "Unknown";
        const href = cid ? `/customers/drilldown/${encodeURIComponent(String(cid))}` : "#";
        const revenue = fmtCurrency(row.revenue);
        const lastOrder = row.last_order || row.lastOrder || "-";
        const daysSince = row.days_since_last == null ? "-" : fmtInt.format(row.days_since_last);
        return `
          <tr>
            <td class="text-truncate" style="max-width:360px;"><a href="${href}">${cname}</a></td>
            <td class="text-end">${revenue}</td>
            <td>${lastOrder}</td>
            <td class="text-end">${daysSince}</td>
          </tr>
        `;
      })
      .join("");
  };

  const applyChurnSearch = () => {
    const q = (document.getElementById("churnSearch")?.value || "").trim().toLowerCase();
    if (!q) {
      renderChurnRows(churnRows);
      return;
    }
    const filtered = (churnRows || []).filter((row) => {
      const cid = String(row.customer_id || row.customerId || "").toLowerCase();
      const cname = String(row.customer_name || row.customerName || "").toLowerCase();
      return cid.includes(q) || cname.includes(q);
    });
    renderChurnRows(filtered);
  };

  const applyPayload = (payload = {}) => {
    const trend = payload.trend || {};
    trendLabels = trend.labels || [];
    trendRevenue = (trend.revenue || []).map((v) => Number(v) || 0);

    const charts = payload.charts || {};
    topCustomers = charts.top_customers || [];
    topProducts = charts.top_products || [];
    churnRows = charts.churned_customers || [];
    shippingMix = charts.shipping_mix || [];
    weekdayRevenue = charts.weekday_revenue || [];

    renderKpis(payload.kpis || {});
    renderChurnRows(churnRows);

    // The five chart renderers below all return early without Plotly, which
    // loads on idle and so usually arrives after this payload. Nothing asked
    // them a second time, so the page settled with empty frames.
    const drawCharts = () => {
      renderTrend();
      renderTopCustomers();
      renderTopProducts();
      renderShippingMix();
      renderWeekdayRevenue();
    };
    if (window.ChartUtils && window.ChartUtils.whenPlotlyReady) {
      window.ChartUtils.whenPlotlyReady(drawCharts);
    } else {
      drawCharts();
    }
  };

  const consumeApplyId = () => {
    const applyId = currentApplyId;
    currentApplyId = null;
    return applyId;
  };

  const dispatchApplied = (detail = {}) => {
    const payload = {
      page: "region_drilldown",
      qs: filterQs,
      region_id: regionId,
      ...detail,
    };
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

  const fetchBundle = async (force = false) => {
    const requestQs = buildRequestQs();
    if (!force && requestQs === lastFetchKey) {
      dispatchApplied();
      return;
    }
    lastFetchKey = requestQs;
    const requestId = ++fetchSeq;

    if (controller) controller.abort();
    controller = new AbortController();
    const url = requestQs ? `${bundleUrl}?${requestQs}` : bundleUrl;
    try {
      const res = await authFetch(url, {
        signal: controller.signal,
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      const raw = await res.json();
      const payload = window.normalizeBundlePayload ? window.normalizeBundlePayload(raw) : raw;
      if (!res.ok) throw new Error(payload?.error?.message || `HTTP ${res.status}`);
      applyPayload(payload);
      const meta = payload.meta || {};
      console.debug("[region drilldown bundle]", {
        cached: meta.cached,
        duckdb_query_count: meta.duckdb_query_count,
        duckdb_ms: meta.duckdb_ms,
        dataset_version: meta.dataset_version,
      });
    } catch (err) {
      if (err?.name === "AbortError") return;
      console.error("region drilldown bundle failed", err);
    } finally {
      if (requestId !== fetchSeq) return;
      dispatchApplied();
    }
  };

  const wireInteractions = () => {
    document.getElementById("logScale")?.addEventListener("change", () => renderTrend());
    document.getElementById("custTopN")?.addEventListener("change", () => renderTopCustomers());
    document.getElementById("prodTopN")?.addEventListener("change", () => renderTopProducts());
    document.getElementById("churnSearch")?.addEventListener("input", () => applyChurnSearch());

    document.getElementById("revCsvBtn")?.addEventListener("click", () => {
      const rows = [["Month", "Revenue"]];
      trendLabels.forEach((m, idx) => rows.push([m, trendRevenue[idx] || 0]));
      downloadCSV(rows, `region_${regionId}_revenue`);
    });
    document.getElementById("revPngBtn")?.addEventListener("click", () => {
      const gd = document.getElementById("region_rev_chart");
      if (!gd || !window.Plotly || !gd.data) return;
      window.Plotly.downloadImage(gd, { format: "png", filename: "region_revenue" });
    });
    document.getElementById("custCsvBtn")?.addEventListener("click", () => {
      const n = Number.parseInt(document.getElementById("custTopN")?.value || "10", 10) || 10;
      const rows = sliceTop(topCustomers, n);
      const aoa = [["Customer", "Revenue"]].concat(rows.map((r) => [r.customer_name || r.customer_id, r.revenue || 0]));
      downloadCSV(aoa, `region_${regionId}_top_customers`);
    });
    document.getElementById("prodCsvBtn")?.addEventListener("click", () => {
      const n = Number.parseInt(document.getElementById("prodTopN")?.value || "10", 10) || 10;
      const rows = sliceTop(topProducts, n);
      const aoa = [["Product", "Revenue"]].concat(rows.map((r) => [r.product_name || r.product_id, r.revenue || 0]));
      downloadCSV(aoa, `region_${regionId}_top_products`);
    });
  };

  const applyFilters = (qsHint) => {
    filterQs = (qsHint || "").replace(/^\?/, "");
    updateUrl();
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
    applyFilters(nextQs || filterQs);
  };

  const onApply = (evt) => {
    currentApplyId = evt?.detail?.applyId || null;
    const nextQs = (evt?.detail && evt.detail.qs) || "";
    applyFilters(nextQs);
  };
  window.addEventListener("globalFilters:apply", onApply);
  window.addEventListener("globalFilters:ready", (evt) => {
    const nextQs = (evt?.detail && evt.detail.qs) || "";
    bootstrap(nextQs);
  });

  bootstrap(filterQs);
  setTimeout(() => {
    if (!bootstrapped) bootstrap(filterQs);
  }, 800);
})();
