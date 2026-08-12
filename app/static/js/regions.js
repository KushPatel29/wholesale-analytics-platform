(() => {
  const root = document.getElementById("RegionsApp");
  if (!root) return;
  const authFetch = window.authFetch || fetch;

  if (document?.body?.dataset) {
    document.body.dataset.filtersHandler = "ajax";
  }

  const bundleUrl = root.dataset.bundleUrl || "/api/regions/bundle";
  const defaultPageSize = Number.parseInt(root.dataset.pageSize || "25", 10) || 25;
  let sortBy = (root.dataset.sortBy || "revenue").toLowerCase();
  let sortDir = (root.dataset.sortDir || "desc").toLowerCase() === "asc" ? "asc" : "desc";
  let page = 1;
  let pageSize = defaultPageSize;
  let search = "";
  let filterQs = (window.location.search || "").replace(/^\?/, "");
  let controller = null;
  let fetchSeq = 0;
  let currentApplyId = "";
  let bootstrapped = false;
  let lastFetchKey = "";
  let tableRows = [];
  let chartLabelsFull = [];
  let chartValuesFull = [];
  let profitabilityRowsFull = [];

  const fmtMoney0 = new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  });
  const fmtMoney2 = new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  });
  const fmtCompact = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });
  const fmtInt = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
  const fmtPct1 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });

  const setText = (id, value) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = value;
  };

  const fmtCurrency = (val) => (val == null ? "-" : fmtMoney0.format(Number(val) || 0));
  const fmtCurrency2 = (val) => (val == null ? "-" : fmtMoney2.format(Number(val) || 0));
  const fmtCurrencyCompact = (val) => {
    if (val == null || Number.isNaN(Number(val))) return "-";
    return `$${fmtCompact.format(Number(val))}`;
  };
  const fmtPercent = (val) => (val == null ? "-" : `${fmtPct1.format(Number(val) || 0)}%`);
  const truncateLabel = (value, maxLen = 22) => {
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
        return await window.filtersReady;
      } catch (err) {
        console.warn("[regions] filtersReady rejected", err);
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
    params.set("page", String(page));
    params.set("page_size", String(pageSize));
    params.set("sort", sortBy);
    params.set("sort_dir", sortDir);
    const topN = document.getElementById("topN")?.value;
    if (topN) params.set("topN", topN);
    if (search) params.set("search", search);
    return params.toString();
  };

  const updateUrl = () => {
    if (!window.history || typeof window.history.replaceState !== "function") return;
    const nextUrl = filterQs ? `${window.location.pathname}?${filterQs}` : window.location.pathname;
    window.history.replaceState({}, "", nextUrl);
  };

  const renderSummaryRibbon = (kpis = {}, table = {}, meta = {}) => {
    const windowStart = kpis.start || "-";
    const windowEnd = displayWindowEnd(kpis.end);
    setText("regionsSummaryWindow", window.WAFormat.dayRange(windowStart, windowEnd));
    setText("regionsSummaryRegions", `Regions shown: ${fmtInt.format(table.total || kpis.regions_count || 0)}`);
    setText("regionsSummaryCustomers", `Customers: ${fmtInt.format(kpis.customers || 0)}`);
    setText("regionsSummaryOrders", `Orders: ${fmtInt.format(kpis.orders || 0)}`);

    const packsChip = document.getElementById("regionsSummaryPacksCoverage");
    const packsCoverage = meta?.packs_coverage || {};
    const packsPct = packsCoverage.packs_coverage_pct ?? packsCoverage.pct ?? null;
    if (packsChip) {
      if (packsPct == null || Number.isNaN(Number(packsPct))) {
        packsChip.classList.add("d-none");
      } else {
        packsChip.classList.remove("d-none");
        packsChip.textContent = `Packs coverage: ${fmtPct1.format(Number(packsPct))}%`;
      }
    }

    const costChip = document.getElementById("regionsSummaryCostCoverage");
    if (costChip) {
      const costPct = kpis.cost_coverage_pct;
      if (costPct == null || Number.isNaN(Number(costPct))) {
        costChip.classList.add("d-none");
      } else {
        costChip.classList.remove("d-none");
        costChip.textContent = `Cost coverage: ${fmtPct1.format(Number(costPct))}%`;
      }
    }
  };

  const renderKpis = (kpis = {}) => {
    setText("regionsKpiRevenue", fmtCurrency(kpis.total_revenue));
    setText("regionsKpiCount", fmtInt.format(kpis.regions_count || 0));
    setText("regionsKpiAov", fmtCurrency(kpis.avg_order_value));
    setText("regionsKpiYoy", kpis.yoy_growth == null ? "-" : `${fmtPct1.format(kpis.yoy_growth)}%`);

    setText("regionsKpiProfit", fmtCurrency(kpis.profit));
    setText("regionsKpiMargin", fmtPercent(kpis.margin_pct));
    setText("regionsKpiTop1", fmtPercent(kpis.concentration_top1_pct));
    setText("regionsKpiTop5", fmtPercent(kpis.concentration_top5_pct));
    setText("regionsKpiHhi", kpis.revenue_hhi == null ? "-" : fmtInt.format(kpis.revenue_hhi));
    setText("regionsKpiDeltaRev", fmtCurrency(kpis.revenue_delta_prior));
    setText("regionsKpiDeltaRevPct", fmtPercent(kpis.revenue_delta_prior_pct));
    setText("regionsKpiVolatility", fmtPercent(kpis.revenue_volatility_pct));

    const warn = document.getElementById("regionsProfitWarning");
    if (warn) {
      const costPct = Number(kpis.cost_coverage_pct);
      const lowCoverage = Number.isFinite(costPct) && costPct < 90;
      warn.classList.toggle("d-none", !lowCoverage);
      if (lowCoverage) {
        warn.textContent = `Low cost coverage (${fmtPct1.format(costPct)}%)`;
      }
    }
  };

  const currentChartSeries = () => {
    const topN = Number.parseInt(document.getElementById("topN")?.value || "0", 10) || 0;
    const pairs = chartLabelsFull.map((label, idx) => ({ label, value: Number(chartValuesFull[idx]) || 0 }));
    pairs.sort((a, b) => b.value - a.value);
    const sliced = topN > 0 ? pairs.slice(0, topN) : pairs;
    return {
      labels: sliced.map((p) => p.label),
      values: sliced.map((p) => p.value),
    };
  };

  const renderChart = () => {
    const chartEl = document.getElementById("regionsChart");
    const emptyEl = document.getElementById("regionsChartEmpty");
    if (!chartEl || !window.Plotly) return;
    const series = currentChartSeries();
    const hasData = series.labels.length > 0 && series.values.some((v) => v !== 0);
    if (emptyEl) emptyEl.classList.toggle("d-none", hasData);
    if (!hasData) {
      chartEl.innerHTML = "";
      return;
    }
    const fullLabels = series.labels.map((label) => String(label ?? ""));
    const shortLabels = fullLabels.map((label) => truncateLabel(label, 20));
    const logScale = Boolean(document.getElementById("logScale")?.checked);
    const layout = {
      margin: { t: 10, r: 20, b: 100, l: 60 },
      xaxis: { automargin: true, tickangle: -25 },
      yaxis: { title: "Revenue", tickformat: "~s", tickprefix: "$", type: logScale ? "log" : "linear" },
      height: 420,
    };
    const data = [
      {
        x: shortLabels,
        y: series.values,
        customdata: fullLabels,
        type: "bar",
        hovertemplate: "%{customdata}<br>%{y:$,.0f}<extra></extra>",
      },
    ];
    window.Plotly.newPlot(chartEl, data, layout, { displayModeBar: false, responsive: true });
  };

  const profitabilitySeries = () => {
    const metric = (document.getElementById("profitabilityMetric")?.value || "revenue").toLowerCase();
    const topN = Number.parseInt(document.getElementById("topN")?.value || "0", 10) || 0;
    const rows = (profitabilityRowsFull || []).slice();
    rows.sort((a, b) => (Number(b?.[metric]) || 0) - (Number(a?.[metric]) || 0));
    const picked = topN > 0 ? rows.slice(0, topN) : rows;
    const fullLabels = picked.map((r) => String(r.region || ""));
    const shortLabels = fullLabels.map((label) => truncateLabel(label, 20));
    let values = picked.map((r) => Number(r?.[metric]) || 0);
    if (metric === "margin_pct") {
      values = picked.map((r) => (r?.margin_pct == null ? null : Number(r.margin_pct)));
    }
    return { metric, fullLabels, shortLabels, values };
  };

  const renderProfitabilityChart = () => {
    const chartEl = document.getElementById("regionsProfitChart");
    const emptyEl = document.getElementById("regionsProfitChartEmpty");
    if (!chartEl || !window.Plotly) return;
    const series = profitabilitySeries();
    const hasData = series.shortLabels.length > 0 && series.values.some((v) => v != null && Number(v) !== 0);
    if (emptyEl) emptyEl.classList.toggle("d-none", hasData);
    if (!hasData) {
      chartEl.innerHTML = "";
      return;
    }
    const isPct = series.metric === "margin_pct";
    const barName = series.metric === "profit" ? "Profit" : series.metric === "margin_pct" ? "Margin %" : "Revenue";
    const layout = {
      margin: { t: 10, r: 20, b: 100, l: 60 },
      xaxis: { automargin: true, tickangle: -25 },
      yaxis: isPct ? { title: "Margin %", ticksuffix: "%" } : { title: barName, tickformat: "~s", tickprefix: "$" },
      height: 340,
    };
    const data = [
      {
        x: series.shortLabels,
        y: series.values,
        customdata: series.fullLabels,
        type: "bar",
        marker: { color: isPct ? "#0891b2" : series.metric === "profit" ? "#16a34a" : "#1d4ed8" },
        hovertemplate: isPct
          ? "%{customdata}<br>%{y:.1f}%<extra></extra>"
          : "%{customdata}<br>%{y:$,.0f}<extra></extra>",
      },
    ];
    window.Plotly.newPlot(chartEl, data, layout, { displayModeBar: false, responsive: true });
  };

  const renderMomentumTable = (momentum = {}) => {
    const tbody = document.getElementById("regionsMomentumTbody");
    if (!tbody) return;
    const rows = Array.isArray(momentum.rows) ? momentum.rows : [];
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted">No momentum rows for current filters.</td></tr>';
      return;
    }
    tbody.innerHTML = rows
      .map(
        (row) => `
      <tr>
        <td>${row.region || ""}</td>
        <td class="text-end">${fmtCurrency(row.revenue_current)}</td>
        <td class="text-end">${fmtCurrency(row.revenue_prior)}</td>
        <td class="text-end">${fmtCurrency(row.delta_revenue)}</td>
        <td class="text-end">${fmtPercent(row.delta_revenue_pct)}</td>
        <td class="text-end">${fmtInt.format(row.delta_orders || 0)}</td>
        <td class="text-end">${fmtInt.format(row.delta_customers || 0)}</td>
      </tr>
    `
      )
      .join("");
  };

  const updateSortIndicators = () => {
    document.querySelectorAll("#regionsTable thead th.sortable").forEach((th) => {
      th.classList.remove("asc", "desc");
      const key = (th.dataset.sort || "").toLowerCase();
      if (key === sortBy) {
        th.classList.add(sortDir === "asc" ? "asc" : "desc");
      }
    });
  };

  const updatePager = (meta = {}) => {
    const total = Number(meta.total || 0);
    const currentPage = Number(meta.page || 1);
    const size = Number(meta.page_size || pageSize || 25);
    const totalPages = total > 0 ? Math.max(1, Math.ceil(total / size)) : 0;
    const startRow = total > 0 ? (currentPage - 1) * size + 1 : 0;
    const endRow = total > 0 ? Math.min(total, startRow + size - 1) : 0;

    setText("regionsPagerSummary", total > 0 ? `Showing ${startRow}-${endRow} of ${total}` : "No regions");
    setText("regionsPagerIndicator", totalPages > 0 ? `Page ${currentPage} of ${totalPages}` : "");

    const prevBtn = document.getElementById("regionsPrev");
    const nextBtn = document.getElementById("regionsNext");
    if (prevBtn) prevBtn.disabled = !(totalPages > 0 && currentPage > 1);
    if (nextBtn) nextBtn.disabled = !(totalPages > 0 && currentPage < totalPages);
  };

  const buildDrilldownHref = (regionId) => {
    const rid = encodeURIComponent(String(regionId || ""));
    const qs = filterQs ? `?${filterQs}` : "";
    return `/regions/${rid}${qs}`;
  };

  const buildExportHref = (regionId) => {
    const rid = encodeURIComponent(String(regionId || ""));
    const qs = filterQs ? `&${filterQs}` : "";
    return `/regions/${rid}/export?format=xlsx${qs}`;
  };

  const renderTable = (table = {}) => {
    const tbody = document.getElementById("regionsTbody");
    if (!tbody) return;
    tbody.innerHTML = "";
    tableRows = table.rows || [];
    if (!tableRows.length) {
      tbody.innerHTML = '<tr><td colspan="10" class="text-center text-muted">No regions available.</td></tr>';
      updatePager(table);
      updateSortIndicators();
      return;
    }
    const rowsHtml = tableRows
      .map((row) => {
        const regionId = row.region_id || row.region || row.label || "Unknown";
        const regionLabel = row.region || row.label || regionId;
        const revenue = fmtCurrency(row.revenue);
        const orders = fmtInt.format(row.orders || 0);
        const customers = fmtInt.format(row.customers || 0);
        const aov = fmtCurrency(row.aov);
        const repeatPct = fmtPercent(row.repeat_pct);
        const churnPct = fmtPercent(row.churn_pct);
        const topCust = fmtPercent(row.top_customer_share_pct);
        const topProd = fmtPercent(row.top_product_share_pct);
        const viewHref = buildDrilldownHref(regionId);
        const exportHref = buildExportHref(regionId);
        return `
          <tr>
            <td><a class="text-decoration-none" href="${viewHref}">${regionLabel}</a></td>
            <td class="text-end">${customers}</td>
            <td class="text-end">${orders}</td>
            <td class="text-end">${revenue}</td>
            <td class="text-end">${aov}</td>
            <td class="text-end">${repeatPct}</td>
            <td class="text-end">${churnPct}</td>
            <td class="text-end">${topCust}</td>
            <td class="text-end">${topProd}</td>
            <td>
              <div class="btn-group btn-group-sm">
                <a class="btn btn-primary" href="${viewHref}">View</a>
                <a class="btn btn-outline-secondary" href="${exportHref}" title="Export drilldown">Export</a>
              </div>
            </td>
          </tr>
        `;
      })
      .join("");
    tbody.innerHTML = rowsHtml;
    setText("regionsTableStatus", `Rows: ${tableRows.length}`);
    updatePager(table);
    updateSortIndicators();
  };

  const applyChartData = (payload = {}) => {
    const chart = (payload.charts || {}).revenue_by_region || {};
    chartLabelsFull = chart.labels || [];
    chartValuesFull = chart.values || [];
    const profitability = (payload.charts || {}).profitability_by_region || {};
    profitabilityRowsFull = Array.isArray(profitability.rows) ? profitability.rows : [];
    if (!profitabilityRowsFull.length && chartLabelsFull.length) {
      profitabilityRowsFull = chartLabelsFull.map((label, idx) => ({
        region: label,
        revenue: Number(chartValuesFull[idx]) || 0,
        profit: null,
        margin_pct: null,
      }));
    }
    // Plotly loads on idle and routinely arrives after this payload does; both
    // renderers return early without it and nothing asked them again, so the
    // page settled with two empty frames.
    const draw = () => {
      renderChart();
      renderProfitabilityChart();
    };
    if (window.ChartUtils && window.ChartUtils.whenPlotlyReady) {
      window.ChartUtils.whenPlotlyReady(draw);
    } else {
      draw();
    }
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
    } catch (err) {
      /* ignore */
    }
  };

  const updateMomentumExportHref = () => {
    const btn = document.getElementById("regionsMomentumExportBtn");
    if (!btn) return;
    const base = "/regions/export_momentum";
    const params = new URLSearchParams(filterQs || "");
    params.set("format", "csv");
    btn.setAttribute("href", `${base}?${params.toString()}`);
  };

  const fetchBundle = async (force = false) => {
    const requestId = ++fetchSeq;
    const requestQs = buildRequestQs();
    const fetchKey = requestQs;
    if (!force && fetchKey === lastFetchKey) {
      dispatchApplied();
      return;
    }
    lastFetchKey = fetchKey;

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
      if (!res.ok) {
        throw new Error(payload?.error?.message || `HTTP ${res.status}`);
      }
      renderKpis(payload.kpis || {});
      renderSummaryRibbon(payload.kpis || {}, payload.table || {}, payload.meta || {});
      applyChartData(payload);
      renderTable(payload.table || {});
      renderMomentumTable(payload.momentum || {});
      updateMomentumExportHref();
      const meta = payload.meta || {};
      console.debug("[regions bundle]", {
        cached: meta.cached,
        duckdb_query_count: meta.duckdb_query_count,
        duckdb_ms: meta.duckdb_ms,
        dataset_version: meta.dataset_version,
      });
    } catch (err) {
      if (err?.name === "AbortError") return;
      console.error("regions bundle failed", err);
      const tbody = document.getElementById("regionsTbody");
      if (tbody && !tbody.innerHTML) {
        tbody.innerHTML = '<tr><td colspan="10" class="text-center text-danger">Failed to load regions.</td></tr>';
      }
    } finally {
      if (requestId !== fetchSeq) return;
      dispatchApplied();
    }
  };

  const onSortClick = (evt) => {
    const th = evt.currentTarget;
    const key = (th.dataset.sort || "").toLowerCase();
    if (!key) return;
    if (key === sortBy) {
      sortDir = sortDir === "asc" ? "desc" : "asc";
    } else {
      sortBy = key;
      sortDir = key === "region" ? "asc" : "desc";
    }
    page = 1;
    fetchBundle(true);
  };

  const wireInteractions = () => {
    document.querySelectorAll("#regionsTable thead th.sortable").forEach((th) => {
      th.addEventListener("click", onSortClick);
    });

    document.getElementById("regionsPrev")?.addEventListener("click", () => {
      if (page <= 1) return;
      page -= 1;
      fetchBundle(true);
    });
    document.getElementById("regionsNext")?.addEventListener("click", () => {
      page += 1;
      fetchBundle(true);
    });

    let searchTimer = null;
    document.getElementById("tableSearch")?.addEventListener("input", (evt) => {
      const next = (evt.target.value || "").trim();
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => {
        search = next;
        page = 1;
        fetchBundle(true);
      }, 250);
    });

    document.getElementById("topN")?.addEventListener("change", () => {
      renderChart();
      renderProfitabilityChart();
    });
    document.getElementById("logScale")?.addEventListener("change", () => renderChart());
    document.getElementById("profitabilityMetric")?.addEventListener("change", () => renderProfitabilityChart());

    document.getElementById("chartCsvBtn")?.addEventListener("click", () => {
      const series = currentChartSeries();
      const rows = [["Region", "Revenue"]].concat(series.labels.map((l, i) => [l, series.values[i]]));
      downloadCSV(rows, "region_revenue");
    });

    document.getElementById("profitabilityCsvBtn")?.addEventListener("click", () => {
      const series = profitabilitySeries();
      const metricLabel = series.metric === "profit" ? "Profit" : series.metric === "margin_pct" ? "MarginPct" : "Revenue";
      const rows = [["Region", metricLabel]].concat(series.fullLabels.map((label, idx) => [label, series.values[idx] ?? ""]));
      downloadCSV(rows, "region_profitability");
    });

    document.getElementById("tableCsvBtn")?.addEventListener("click", () => {
      const header = [
        "Region",
        "Customers",
        "Orders",
        "Revenue",
        "AOV",
        "RepeatPct",
        "ChurnPct",
        "TopCustomerSharePct",
        "TopProductSharePct",
      ];
      const rows = [header].concat(
        (tableRows || []).map((row) => [
          row.region || row.label,
          row.customers || 0,
          row.orders || 0,
          row.revenue || 0,
          row.aov || 0,
          row.repeat_pct || 0,
          row.churn_pct || 0,
          row.top_customer_share_pct || 0,
          row.top_product_share_pct || 0,
        ])
      );
      downloadCSV(rows, "regions_kpis");
    });
  };

  const applyFilters = (qsHint) => {
    filterQs = (qsHint || "").replace(/^\?/, "");
    page = 1;
    updateUrl();
    updateMomentumExportHref();
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

  window.addEventListener("globalFilters:apply", (evt) => {
    currentApplyId = String(evt?.detail?.applyId || "");
    const nextQs = (evt?.detail && evt.detail.qs) || "";
    applyFilters(nextQs);
  });

  window.addEventListener("globalFilters:ready", (evt) => {
    const nextQs = (evt?.detail && evt.detail.qs) || "";
    bootstrap(nextQs);
  });

  bootstrap(filterQs);
  setTimeout(() => {
    if (!bootstrapped) bootstrap(filterQs);
  }, 800);
})();
