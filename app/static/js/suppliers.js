(() => {
  const root = document.getElementById("SuppliersApp");
  if (!root) return;
  const authFetch = window.authFetch || fetch;

  if (document?.body?.dataset) {
    document.body.dataset.filtersHandler = "ajax";
  }

  const bundleUrl = root.dataset.bundleUrl || "/api/suppliers/bundle";
  const showCosts = (() => {
    try {
      return JSON.parse(root.dataset.showCosts || "true") !== false;
    } catch (err) {
      return true;
    }
  })();

  const defaultPageSize = Number.parseInt(root.dataset.pageSize || "50", 10) || 50;

  const state = {
    page: 1,
    pageSize: defaultPageSize,
    sortBy: "revenue",
    sortDir: "desc",
    search: "",
    filterQs: (window.location.search || "").replace(/^\?/, ""),
    loading: false,
    totalRows: 0,
    lastFetchKey: "",
    bootstrapped: false,
  };

  let controller = null;
  let currentRequestSeq = 0;
  let currentApplyId = null;

  const tbody = document.getElementById("supTbody");
  const statusEl = document.getElementById("tableStatus");
  const countEl = document.getElementById("tableCount");
  const loadMoreBtn = document.getElementById("loadMore");
  const sentinel = document.getElementById("infiniteSentinel");
  const searchInput = document.getElementById("tableSearch");
  const clearSearchBtn = document.getElementById("btnClearSearch");
  const exportXlsxBtn = document.getElementById("suppliersExportXlsx");
  const exportCsvBtn = document.getElementById("suppliersExportCsv");

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
  const fmtNum2 = new Intl.NumberFormat(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  const fmtPct1 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });

  const setText = (id, value) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = value;
  };

  const money0 = (val) => (val == null ? "-" : fmtMoney0.format(Number(val) || 0));
  const money2 = (val) => (val == null ? "—" : fmtMoney2.format(Number(val) || 0));
  const num0 = (val) => (val == null ? "—" : fmtNum0.format(Number(val) || 0));
  const num2 = (val) => (val == null ? "—" : fmtNum2.format(Number(val) || 0));
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

  // Lightweight client export helpers used by existing template buttons.
  window.ExportKit = window.ExportKit || {
    exportArrayToCSV(name, labels, values, xName = "Label", yName = "Value") {
      const rows = [[xName, yName]];
      (labels || []).forEach((lab, i) => rows.push([lab, (values || [])[i] ?? ""]));
      downloadCSV(rows, name);
    },
    exportTableToCSV(tableId, filenameBase) {
      const table = document.getElementById(tableId);
      if (!table) return;
      const head = Array.from(table.tHead?.rows?.[0]?.cells || []).map((th) => th.innerText.trim());
      const rows = [head];
      Array.from(table.tBodies?.[0]?.rows || []).forEach((tr) => {
        rows.push(Array.from(tr.cells).map((td) => td.innerText.trim()));
      });
      downloadCSV(rows, filenameBase);
    },
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
        console.warn("[suppliers] filtersReady rejected", err);
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

  const buildParams = ({ includePage = true } = {}) => {
    const params = new URLSearchParams(state.filterQs || "");
    if (includePage) {
      params.set("page", String(state.page));
      params.set("page_size", String(state.pageSize));
    }
    params.set("sort", state.sortBy);
    params.set("sort_dir", state.sortDir);
    if (state.search) params.set("search", state.search);
    const topN = topNValue();
    if (topN) params.set("topN", topN);
    return params;
  };

  const replaceHistory = () => {
    if (!window.history || typeof window.history.replaceState !== "function") return;
    const nextUrl = state.filterQs ? `${window.location.pathname}?${state.filterQs}` : window.location.pathname;
    window.history.replaceState({}, "", nextUrl);
  };

  const updateLoadMoreState = () => {
    if (!loadMoreBtn || !tbody) return;
    const shown = tbody.querySelectorAll("tr[data-row='supplier']").length;
    const total = Number(state.totalRows || 0);
    loadMoreBtn.disabled = !(total > 0 && shown < total);
  };

  const updateCount = () => {
    if (!countEl || !tbody) return;
    const shown = tbody.querySelectorAll("tr[data-row='supplier']").length;
    if (state.totalRows > 0) {
      countEl.textContent = `${fmtNum0.format(shown)} of ${fmtNum0.format(state.totalRows)} shown`;
    } else {
      countEl.textContent = `${fmtNum0.format(shown)} shown`;
    }
  };

  const setStatus = (msg) => {
    if (statusEl) statusEl.textContent = msg || "";
  };

  const renderKpis = (kpis = {}) => {
    setText("kpiRevenue", money0(kpis.total_revenue));
    setText("kpiSuppliers", fmtNum0.format(Number(kpis.total_suppliers || 0)));
    setText("kpiAOV", money0(kpis.avg_order_value));
    if (document.getElementById("kpiMargin")) {
      const marginVal = kpis.avg_margin_pct;
      setText("kpiMargin", marginVal == null ? "-" : pct1(marginVal));
    }
    setText("kpiHHI", fmtNum0.format(Math.round(Number(kpis.concentration_hhi || 0))));
    setText("kpiTop1", pct1(kpis.concentration_top1_share));
    setText("kpiTop5", pct1(kpis.concentration_top5_share));
  };

  const renderTrend = (trend = {}) => {
    const labels = Array.isArray(trend.labels) ? trend.labels : [];
    const revenue = Array.isArray(trend.revenue) ? trend.revenue : [];
    const profit = Array.isArray(trend.profit) ? trend.profit : [];
    const margin = Array.isArray(trend.margin_pct) ? trend.margin_pct : [];

    // Preserve legacy globals used by the template export button.
    window.trendLabels = labels;
    window.trendValues = revenue;

    if (!window.Plotly) {
      emptyChart("trendChart", "Plotly not loaded.");
      return;
    }
    if (!labels.length || !revenue.length) {
      emptyChart("trendChart", "No data available.");
      return;
    }
    removeSkeleton("trendChart");

    const traces = [
      {
        x: labels,
        y: revenue,
        type: "bar",
        name: "Revenue",
        hovertemplate: "%{x}<br>%{y:$,.0f}<extra></extra>",
      },
    ];

    const hasProfit = showCosts && profit.some(isFiniteNumber);
    if (hasProfit) {
      traces.push({
        x: labels,
        y: profit,
        type: "scatter",
        mode: "lines+markers",
        name: "Profit",
        hovertemplate: "%{x}<br>%{y:$,.0f}<extra></extra>",
      });
    }

    const hasMargin = showCosts && margin.some(isFiniteNumber);
    const layout = {
      margin: { t: 10, r: hasMargin ? 60 : 20, b: 50, l: 60 },
      xaxis: { tickangle: -40, automargin: true },
      yaxis: { title: "Revenue", tickformat: "$,.0f" },
      hovermode: "x unified",
      height: 380,
    };

    if (hasMargin) {
      traces.push({
        x: labels,
        y: margin,
        type: "scatter",
        mode: "lines",
        name: "Margin %",
        yaxis: "y2",
        hovertemplate: "%{x}<br>%{y:.1f}%<extra></extra>",
      });
      layout.yaxis2 = {
        overlaying: "y",
        side: "right",
        tickformat: ",.0f",
        ticksuffix: "%",
        title: "Margin %",
      };
    }

    window.Plotly.newPlot("trendChart", traces, layout, { displayModeBar: false, responsive: true });
  };

  const renderTopSuppliers = (top = {}) => {
    const topRows = Array.isArray(top.rows) ? top.rows : [];
    let labels = Array.isArray(top.labels) ? top.labels : [];
    let values = Array.isArray(top.values) ? top.values : [];
    if (!labels.length && topRows.length) {
      labels = topRows.map((r) => r.supplier_name || r.SupplierName || r.label || r.supplier_id || r.SupplierId);
      values = topRows.map((r) => Number(r.revenue ?? r.Revenue) || 0);
    }

    if (!window.Plotly) {
      emptyChart("topSuppliersChart", "Plotly not loaded.");
      return;
    }
    if (!labels.length) {
      emptyChart("topSuppliersChart", "No data available.");
      return;
    }
    removeSkeleton("topSuppliersChart");

    window.Plotly.newPlot(
      "topSuppliersChart",
      [
        {
          x: labels,
          y: values,
          type: "bar",
          hovertemplate: "%{x}<br>%{y:$,.0f}<extra></extra>",
        },
      ],
      {
        margin: { t: 10, r: 20, b: 100, l: 60 },
        xaxis: { automargin: true },
        yaxis: { title: "Revenue", tickformat: "$,.0f" },
        height: 380,
      },
      { displayModeBar: false, responsive: true }
    );
  };

  const buildDrilldownHref = (supplierId) => {
    const sid = encodeURIComponent(String(supplierId || ""));
    const qs = state.filterQs ? `?${state.filterQs}` : "";
    return `/suppliers/${sid}${qs}`;
  };

  const buildProductsExportHref = (supplierId) => {
    const sid = encodeURIComponent(String(supplierId || ""));
    const params = buildParams({ includePage: false });
    return `/api/suppliers/${sid}/products/export.csv?${params.toString()}`;
  };

  const renderRow = (row) => {
    const supplierId = row.supplier_id || row.SupplierId || row.key || "";
    const supplierName = row.supplier_name || row.SupplierName || row.label || supplierId || "Unknown";
    const revenue = Number(row.revenue ?? row.Revenue) || 0;
    const cost = row.cost ?? row.Cost;
    const profit = row.profit ?? row.Profit;
    const marginPct = row.margin_pct ?? row.MarginPct;
    const roiPct = row.roi_pct ?? row.ROIPct;
    const units = row.units ?? row.Units;
    const weightLb = row.weight_lb ?? row.WeightLb;
    const avgSalePricePerLb = row.avg_sale_price_per_lb ?? row.AvgSalePricePerLb;
    const avgCostPerUnit = row.avg_cost_per_unit ?? row.AvgCostPerUnit;
    const avgCostPerLb = row.avg_cost_per_lb ?? row.AvgCostPerLb;
    const profitPerUnit = row.profit_per_unit ?? row.ProfitPerUnit;
    const profitPerLb = row.profit_per_lb ?? row.ProfitPerLb;
    const products = row.products ?? row.Products;
    const orders = row.orders ?? row.Orders;
    const lastSold = row.last_sold ?? row.LastSold ?? "—";

    const viewHref = buildDrilldownHref(supplierId);
    const exportHref = buildProductsExportHref(supplierId);

    const costCell = showCosts && isFiniteNumber(cost) ? money0(cost) : "—";
    const profitCell = showCosts && isFiniteNumber(profit) ? money0(profit) : "—";
    const marginCell = showCosts && isFiniteNumber(marginPct) ? pct1(marginPct) : "—";
    const roiCell = showCosts && isFiniteNumber(roiPct) ? pct1(roiPct) : "—";
    const avgCostUnitCell = showCosts && isFiniteNumber(avgCostPerUnit) ? money2(avgCostPerUnit) : "—";
    const avgCostLbCell = showCosts && isFiniteNumber(avgCostPerLb) ? money2(avgCostPerLb) : "—";
    const profitUnitCell = showCosts && isFiniteNumber(profitPerUnit) ? money2(profitPerUnit) : "—";
    const profitLbCell = showCosts && isFiniteNumber(profitPerLb) ? money2(profitPerLb) : "—";

    return `
      <tr data-row="supplier">
        <td class="text-truncate" style="max-width:360px;" title="${supplierName}">
          <a class="text-decoration-none link-primary" href="${viewHref}">${supplierName}</a>
        </td>
        <td class="num">${money0(revenue)}</td>
        <td class="num">${costCell}</td>
        <td class="num">${profitCell}</td>
        <td class="num">${marginCell}</td>
        <td class="num">${roiCell}</td>
        <td class="num">${num0(units)}</td>
        <td class="num">${num2(weightLb)}</td>
        <td class="num">${money2(avgSalePricePerLb)}</td>
        <td class="num">${avgCostUnitCell}</td>
        <td class="num">${avgCostLbCell}</td>
        <td class="num">${profitUnitCell}</td>
        <td class="num">${profitLbCell}</td>
        <td class="num">${num0(products)}</td>
        <td class="num">${num0(orders)}</td>
        <td>${lastSold}</td>
        <td>
          <div class="d-flex gap-1">
            <a class="btn btn-sm btn-outline-primary" href="${viewHref}">View</a>
            <a class="btn btn-sm btn-outline-secondary" href="${exportHref}">Export Products</a>
          </div>
        </td>
      </tr>
    `;
  };

  const renderTable = (table = {}, { append = false } = {}) => {
    if (!tbody) return;
    const rows = Array.isArray(table.rows) ? table.rows : [];
    const totalRows = Number(table.total_rows ?? table.total ?? state.totalRows ?? 0);
    state.totalRows = totalRows;

    if (!append) {
      tbody.innerHTML = "";
    }

    if (!rows.length && !append) {
      tbody.innerHTML =
        '<tr><td colspan="17" class="text-center text-muted">No suppliers for current filters.</td></tr>';
      updateCount();
      updateLoadMoreState();
      return;
    }

    const html = rows.map((row) => renderRow(row)).join("");
    const frag = document.createElement("tbody");
    frag.innerHTML = html;
    while (frag.firstChild) tbody.appendChild(frag.firstChild);

    updateCount();
    updateLoadMoreState();
  };

  const updateExportLinks = () => {
    const params = buildParams({ includePage: false });
    const qs = params.toString();
    if (exportXlsxBtn) exportXlsxBtn.href = qs ? `/api/suppliers/export.xlsx?${qs}` : "/api/suppliers/export.xlsx";
    if (exportCsvBtn) exportCsvBtn.href = qs ? `/api/suppliers/export.csv?${qs}` : "/api/suppliers/export.csv";
  };

  const sortKeyMap = {
    SupplierName: "supplier_name",
    Revenue: "revenue",
    Cost: "cost",
    Profit: "profit",
    MarginPct: "margin_pct",
    ROIPct: "roi_pct",
    Units: "units",
    WeightLb: "weight_lb",
    AvgSalePricePerLb: "avg_sale_price_per_lb",
    AvgCostPerUnit: "avg_cost_per_unit",
    AvgCostPerLb: "avg_cost_per_lb",
    ProfitPerUnit: "profit_per_unit",
    ProfitPerLb: "profit_per_lb",
    Products: "products",
    Orders: "orders",
    LastSold: "last_sold",
  };

  const updateSortIndicators = () => {
    document.querySelectorAll("#supTable thead th.sortable").forEach((th) => {
      th.classList.remove("asc", "desc");
      const key = th.dataset.key;
      const mapped = sortKeyMap[key] || "";
      if (mapped && mapped === state.sortBy) {
        th.classList.add(state.sortDir === "asc" ? "asc" : "desc");
      }
    });
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

  const fetchBundle = async ({ append = false } = {}) => {
    const params = buildParams({ includePage: true });
    const fetchKey = buildFetchKey(params);
    if (state.lastFetchKey === fetchKey) return;

    state.lastFetchKey = fetchKey;
    state.loading = true;
    setStatus(append ? "Loading more…" : "Loading…");
    const requestSeq = ++currentRequestSeq;

    if (controller) controller.abort();
    controller = new AbortController();

    let payload = null;
    let error = null;

    try {
      const res = await authFetch(fetchKey, { signal: controller.signal, headers: { Accept: "application/json" } });
      if (!res.ok) {
        const text = await res.text();
        console.error("[suppliers] bundle failed", res.status, text.slice(0, 400));
        throw new Error(`Bundle request failed (${res.status})`);
      }
      const json = await res.json();
      payload = window.normalizeBundlePayload ? window.normalizeBundlePayload(json) : json;
      renderKpis(payload.kpis || {});
      if (!append) {
        renderTrend((payload.charts || {}).trend_12m || {});
        renderTopSuppliers((payload.charts || {}).top_suppliers || {});
      }
      renderTable(payload.table || {}, { append });
      updateExportLinks();
      updateSortIndicators();

      const cached = payload?.meta?.cached;
      setStatus(cached ? "Loaded (cached)." : "Loaded.");
    } catch (err) {
      if (err?.name === "AbortError") return;
      error = err;
      console.error("[suppliers] bundle error", err);
      if (!append) {
        emptyChart("trendChart", "Unable to load overview.");
        emptyChart("topSuppliersChart", "Unable to load top suppliers.");
        renderTable({ rows: [] }, { append: false });
      }
      setStatus("Failed to load data.");
    } finally {
      if (requestSeq !== currentRequestSeq) return;
      state.loading = false;
      state.lastFetchKey = "";
      dispatchGlobalApplyAck({
        page: "suppliers",
        qs: state.filterQs,
        error: error ? String(error) : null,
        cached: Boolean(payload?.meta?.cached),
        duckdb_query_count: payload?.meta?.duckdb_query_count ?? null,
      });
    }
  };

  const applyFilters = (qs) => {
    state.filterQs = (qs || "").replace(/^\?/, "");
    state.page = 1;
    state.lastFetchKey = "";
    replaceHistory();
    fetchBundle({ append: false });
  };

  const loadMore = () => {
    if (state.loading) return;
    const shown = tbody ? tbody.querySelectorAll("tr[data-row='supplier']").length : 0;
    if (state.totalRows > 0 && shown >= state.totalRows) {
      updateLoadMoreState();
      return;
    }
    state.page += 1;
    fetchBundle({ append: true });
  };

  const wireSorting = () => {
    document.querySelectorAll("#supTable thead th.sortable").forEach((th) => {
      th.addEventListener("click", () => {
        const key = th.dataset.key;
        const mapped = sortKeyMap[key];
        if (!mapped) return;
        if (state.sortBy === mapped) {
          state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
        } else {
          state.sortBy = mapped;
          state.sortDir = "desc";
        }
        state.page = 1;
        state.lastFetchKey = "";
        fetchBundle({ append: false });
      });
    });
  };

  const wireSearch = () => {
    if (!searchInput) return;
    let timer = null;
    searchInput.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        state.search = (searchInput.value || "").trim();
        state.page = 1;
        state.lastFetchKey = "";
        fetchBundle({ append: false });
      }, 250);
    });
    if (clearSearchBtn) {
      clearSearchBtn.addEventListener("click", () => {
        searchInput.value = "";
        searchInput.focus();
        state.search = "";
        state.page = 1;
        state.lastFetchKey = "";
        fetchBundle({ append: false });
      });
    }
  };

  const wirePaging = () => {
    if (loadMoreBtn) loadMoreBtn.addEventListener("click", loadMore);
    if ("IntersectionObserver" in window && sentinel) {
      const io = new IntersectionObserver(
        (entries) => {
          const entry = entries[0];
          if (entry.isIntersecting && !state.loading) {
            loadMore();
          }
        },
        { rootMargin: "600px 0px" }
      );
      io.observe(sentinel);
    }
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

  wireSorting();
  wireSearch();
  wirePaging();
  updateSortIndicators();
  updateExportLinks();
  bootstrap();
  setTimeout(() => {
    if (!state.bootstrapped) bootstrap();
  }, 900);
})();
