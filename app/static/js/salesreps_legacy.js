(() => {
  const root = document.getElementById("SalesRepsApp");
  if (!root) return;
  const authFetch = window.authFetch || fetch;
  if (document?.body?.dataset) {
    document.body.dataset.filtersHandler = "ajax";
  }

  const bundleUrl = root.dataset.bundleUrl || "/api/salesreps/bundle";
  const drilldownTemplate = root.dataset.drilldownTemplate || "";
  const exportXlsx = document.getElementById("salesrepsExportXlsx");
  const exportCsv = document.getElementById("salesrepsExportCsv");
  const ChartLib = window.Chart;

  const fmtMoney = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 0 });
  const fmtMoney2 = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 2 });
  const fmtInt = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
  const fmtPct = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
  const NA = "N/A";

  const state = {
    qs: "",
    page: 1,
    pageSize: 25,
    sortBy: "revenue",
    sortDir: "desc",
  };

  const charts = {};
  let currentAbort = null;
  let currentReqId = 0;
  let bootstrapped = false;
  let currentApplyId = null;

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
        console.warn("[salesreps] filtersReady rejected", err);
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

  const safeNum = (val, fallback = 0) => (Number.isFinite(+val) ? +val : fallback);
  const normalizeQS = (qs) => (qs || "").replace(/^\?/, "");
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
  const formatPercent = (val, isShare = false) => {
    if (val === null || val === undefined || val === "") return NA;
    let num = Number(val);
    if (!Number.isFinite(num)) return NA;
    if (isShare && num <= 1.01) num *= 100;
    return `${fmtPct.format(num)}%`;
  };

  const destroyChart = (key) => {
    if (charts[key]?.destroy) {
      charts[key].destroy();
    }
    charts[key] = null;
  };

  const toggleEmpty = (canvasId, show, message = "No data for selected filters.") => {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    const holder = canvas.parentElement;
    if (!holder) return;
    if (!holder.style.position) holder.style.position = "relative";
    let emptyEl = holder.querySelector("[data-empty-state]");
    if (!emptyEl) {
      emptyEl = document.createElement("div");
      emptyEl.dataset.emptyState = "true";
      emptyEl.className =
        "position-absolute top-0 start-0 w-100 h-100 d-flex align-items-center justify-content-center text-muted small";
      emptyEl.style.background = "rgba(255,255,255,0.75)";
      emptyEl.style.pointerEvents = "none";
      holder.appendChild(emptyEl);
    }
    emptyEl.textContent = message || "No data";
    emptyEl.classList.toggle("d-none", !show);
    canvas.classList.toggle("d-none", !!show);
  };

  const setText = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  };

  const updateColumnLabels = (meta = {}) => {
    const units = meta.units_label || root.dataset.unitsLabel || "Units";
    const asp = meta.asp_label || root.dataset.aspLabel || "ASP";
    const aspLb = meta.asp_lb_label || root.dataset.aspLbLabel || "ASP / lb";
    document.querySelectorAll("[data-column-label='units']").forEach((el) => { el.textContent = units; });
    document.querySelectorAll("[data-column-label='asp']").forEach((el) => { el.textContent = asp; });
    document.querySelectorAll("[data-column-label='asp_lb']").forEach((el) => { el.textContent = aspLb; });
  };

  const buildQS = () => {
    const params = new URLSearchParams(state.qs || "");
    params.set("page", String(state.page));
    params.set("page_size", String(state.pageSize));
    params.set("sort_by", state.sortBy);
    params.set("sort_dir", state.sortDir);
    return params.toString();
  };

  const appendFiltersToUrl = (url, extraQS = "") => {
    if (!url) return "#";
    const params = new URLSearchParams(state.qs || "");
    const extra = new URLSearchParams(extraQS || "");
    extra.forEach((val, key) => params.set(key, val));
    const qs = params.toString();
    if (!qs) return url;
    return url.includes("?") ? `${url}&${qs}` : `${url}?${qs}`;
  };

  const updateExportLinks = () => {
    const exportQS = new URLSearchParams(state.qs || "");
    exportQS.set("sort_by", state.sortBy);
    exportQS.set("sort_dir", state.sortDir);
    const qs = exportQS.toString();
    if (exportXlsx) {
      const base = exportXlsx.dataset.baseHref || exportXlsx.getAttribute("href") || "";
      exportXlsx.dataset.baseHref = base.split("?")[0];
      exportXlsx.setAttribute("href", exportXlsx.dataset.baseHref + (qs ? `?${qs}` : ""));
    }
    if (exportCsv) {
      const base = exportCsv.dataset.baseHref || exportCsv.getAttribute("href") || "";
      exportCsv.dataset.baseHref = base.split("?")[0];
      exportCsv.setAttribute("href", exportCsv.dataset.baseHref + (qs ? `?${qs}` : ""));
    }
  };

  const renderKpis = (kpis = {}) => {
    document.querySelectorAll("[data-kpi-key]").forEach((el) => {
      const key = el.dataset.kpiKey;
      if (!key) return;
      const val = kpis[key];
      if (["revenue", "profit", "cost", "asp", "asp_lb"].includes(key)) {
        el.textContent = val != null ? fmtMoney.format(val) : NA;
        return;
      }
      if (key === "margin_pct" || key === "momentum_pct") {
        el.textContent = formatPercent(val, false);
        return;
      }
      if (key === "top_customer_share") {
        el.textContent = formatPercent(val, true);
        return;
      }
      if (key === "weight_lb") {
        el.textContent = fmtInt.format(safeNum(val));
        return;
      }
      if (key === "units") {
        el.textContent = fmtInt.format(safeNum(val));
        return;
      }
      if (key === "orders" || key === "customers") {
        el.textContent = fmtInt.format(safeNum(val));
        return;
      }
      el.textContent = val != null ? String(val) : NA;
    });
  };

  const renderTrend = (trend = {}) => {
    const canvasId = "trendChart";
    const labels = trend.labels || [];
    const series = trend.series || [];
    const hasData = labels.length && series.length;
    toggleEmpty(canvasId, !hasData);
    if (!ChartLib) return;
    destroyChart("trend");
    if (!hasData) return;

    const palette = ["#0d6efd", "#198754", "#fd7e14", "#6f42c1", "#dc3545", "#20c997", "#0dcaf0", "#adb5bd"];
    const datasets = series.map((s, idx) => ({
      label: s.rep_name || s.rep_id || `Rep ${idx + 1}`,
      data: s.revenue || [],
      borderColor: palette[idx % palette.length],
      backgroundColor: "rgba(13,110,253,0.08)",
      tension: 0.25,
      borderWidth: 2,
      fill: false,
    }));

    charts.trend = new ChartLib(document.getElementById(canvasId), {
      type: "line",
      data: { labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        scales: { y: { ticks: { callback: (v) => fmtMoney.format(v) } } },
      },
    });
  };

  const renderTopReps = (rows = []) => {
    const canvasId = "topRepsChart";
    const top = Array.isArray(rows) ? rows.slice(0, 10) : [];
    const hasData = top.length > 0;
    toggleEmpty(canvasId, !hasData);
    if (!ChartLib) return;
    destroyChart("topReps");
    if (!hasData) return;
    const labels = top.map((r) => r.rep_name || r.rep_id || NA);
    const values = top.map((r) => safeNum(r.revenue));
    charts.topReps = new ChartLib(document.getElementById(canvasId), {
      type: "bar",
      data: { labels, datasets: [{ label: "Revenue", data: values, backgroundColor: "#0d6efd" }] },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } } },
    });
  };

  const renderEfficiency = (rows = []) => {
    const canvasId = "effChart";
    const points = Array.isArray(rows) ? rows : [];
    const hasData = points.length > 0;
    toggleEmpty(canvasId, !hasData);
    if (!ChartLib) return;
    destroyChart("efficiency");
    if (!hasData) return;
    const data = points.map((r) => ({
      x: safeNum(r.customers),
      y: safeNum(r.revenue),
      rep_name: r.rep_name || r.rep_id || "",
      rep_id: r.rep_id || r.rep_name || "",
    }));
    charts.efficiency = new ChartLib(document.getElementById(canvasId), {
      type: "scatter",
      data: { datasets: [{ label: "Rep", data, backgroundColor: "rgba(25,135,84,0.6)" }] },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: {
          x: { title: { display: true, text: "Customers" }, ticks: { callback: (v) => fmtInt.format(v) } },
          y: { title: { display: true, text: "Revenue" }, ticks: { callback: (v) => fmtMoney.format(v) } },
        },
        plugins: {
          tooltip: {
            callbacks: {
              label: (ctx) => {
                const raw = ctx.raw || {};
                const name = raw.rep_name || NA;
                return `${name}: ${fmtMoney.format(raw.y || 0)} revenue, ${fmtInt.format(raw.x || 0)} customers`;
              },
            },
          },
        },
      },
    });
  };

  const renderConcentration = (rows = []) => {
    const canvasId = "concentrationChart";
    const list = Array.isArray(rows) ? rows : [];
    const sorted = [...list]
      .filter((r) => r.top_customer_share != null)
      .sort((a, b) => (b.top_customer_share || 0) - (a.top_customer_share || 0))
      .slice(0, 10);
    const hasData = sorted.length > 0;
    toggleEmpty(canvasId, !hasData);
    if (!ChartLib) return;
    destroyChart("concentration");
    if (!hasData) return;
    const labels = sorted.map((r) => r.rep_name || r.rep_id || NA);
    const values = sorted.map((r) => (safeNum(r.top_customer_share) <= 1.01 ? safeNum(r.top_customer_share) * 100 : safeNum(r.top_customer_share)));
    charts.concentration = new ChartLib(document.getElementById(canvasId), {
      type: "bar",
      data: { labels, datasets: [{ label: "Top customer share %", data: values, backgroundColor: "#fd7e14" }] },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: { y: { ticks: { callback: (v) => `${fmtPct.format(v)}%` } } },
        plugins: { legend: { display: false } },
      },
    });
  };

  const renderProfitRevenue = (rows = []) => {
    const canvasId = "profitRevenueChart";
    const points = Array.isArray(rows) ? rows : [];
    const hasData = points.length > 0;
    toggleEmpty(canvasId, !hasData);
    if (!ChartLib) return;
    destroyChart("profitRevenue");
    if (!hasData) return;
    const data = points.map((r) => ({
      x: safeNum(r.revenue),
      y: r.profit == null ? null : safeNum(r.profit),
      rep_name: r.rep_name || r.rep_id || "",
    })).filter((p) => p.y != null);
    const hasPoints = data.length > 0;
    toggleEmpty(canvasId, !hasPoints, "No profit data available.");
    if (!hasPoints) return;
    charts.profitRevenue = new ChartLib(document.getElementById(canvasId), {
      type: "scatter",
      data: { datasets: [{ label: "Profit", data, backgroundColor: "rgba(220,53,69,0.6)" }] },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: {
          x: { title: { display: true, text: "Revenue" }, ticks: { callback: (v) => fmtMoney.format(v) } },
          y: { title: { display: true, text: "Profit" }, ticks: { callback: (v) => fmtMoney.format(v) } },
        },
        plugins: {
          tooltip: {
            callbacks: {
              label: (ctx) => {
                const raw = ctx.raw || {};
                return `${raw.rep_name || NA}: ${fmtMoney.format(raw.y || 0)} profit`;
              },
            },
          },
        },
      },
    });
  };

  const renderAspLeaders = (rows = []) => {
    const canvasId = "aspChart";
    const list = Array.isArray(rows) ? rows : [];
    const sorted = [...list].filter((r) => r.asp != null).slice(0, 10);
    const hasData = sorted.length > 0;
    toggleEmpty(canvasId, !hasData);
    if (!ChartLib) return;
    destroyChart("asp");
    if (!hasData) return;
    const labels = sorted.map((r) => r.rep_name || r.rep_id || NA);
    const values = sorted.map((r) => safeNum(r.asp));
    charts.asp = new ChartLib(document.getElementById(canvasId), {
      type: "bar",
      data: { labels, datasets: [{ label: "ASP", data: values, backgroundColor: "#6f42c1" }] },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { y: { ticks: { callback: (v) => fmtMoney2.format(v) } } } },
    });
  };

  const renderMarginRanking = (rows = []) => {
    const canvasId = "marginRankChart";
    const list = Array.isArray(rows) ? rows : [];
    const sorted = [...list].filter((r) => r.margin_pct != null).slice(0, 10);
    const hasData = sorted.length > 0;
    toggleEmpty(canvasId, !hasData, "Margin data not available.");
    if (!ChartLib) return;
    destroyChart("margin");
    if (!hasData) return;
    const labels = sorted.map((r) => r.rep_name || r.rep_id || NA);
    const values = sorted.map((r) => safeNum(r.margin_pct));
    charts.margin = new ChartLib(document.getElementById(canvasId), {
      type: "bar",
      data: { labels, datasets: [{ label: "Margin %", data: values, backgroundColor: "#20c997" }] },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { y: { ticks: { callback: (v) => `${fmtPct.format(v)}%` } } } },
    });
  };

  const renderPareto = (rows = []) => {
    const canvasId = "revenueShareChart";
    const list = Array.isArray(rows) ? rows : [];
    const hasData = list.length > 0;
    toggleEmpty(canvasId, !hasData);
    if (!ChartLib) return;
    destroyChart("pareto");
    if (!hasData) return;
    const labels = list.map((r) => r.rep_name || r.rep_id || NA);
    const values = list.map((r) => safeNum(r.revenue));
    const cum = list.map((r) => (r.cumulative_pct != null ? safeNum(r.cumulative_pct) : null));
    charts.pareto = new ChartLib(document.getElementById(canvasId), {
      data: {
        labels,
        datasets: [
          { type: "bar", label: "Revenue", data: values, backgroundColor: "#0dcaf0" },
          { type: "line", label: "Cumulative %", data: cum, borderColor: "#0d6efd", yAxisID: "y1", fill: false, tension: 0.25 },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: {
          y: { beginAtZero: true, ticks: { callback: (v) => fmtMoney.format(v) } },
          y1: { beginAtZero: true, position: "right", grid: { drawOnChartArea: false }, ticks: { callback: (v) => `${fmtPct.format(v)}%` } },
        },
      },
    });
  };

  const renderTable = (table = {}) => {
    const tbody = document.getElementById("salesreps-table-body");
    if (!tbody) return;
    tbody.innerHTML = "";
    const rows = table.rows || [];
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="11" class="text-center text-muted">No data for current filters.</td></tr>';
    } else {
      rows.forEach((r) => {
        const repId = r.rep_id || r.key || r.rep_name || "";
        const repName = r.rep_name || r.label || repId || NA;
        const drillUrl = drilldownTemplate ? drilldownTemplate.replace("__ID__", encodeURIComponent(repId)) : "#";
        const link = appendFiltersToUrl(drillUrl);
        const tr = document.createElement("tr");
        tr.dataset.href = link;
        const momentum = r.momentum_pct != null ? formatPercent(r.momentum_pct, false) : NA;
        const topShare = r.top_customer_share_pct != null ? formatPercent(r.top_customer_share_pct, false) : formatPercent(r.top_customer_share, true);
        tr.innerHTML = `
          <td>${repName}</td>
          <td class="text-end">${fmtMoney.format(safeNum(r.revenue))}</td>
          <td class="text-end">${fmtInt.format(safeNum(r.orders))}</td>
          <td class="text-end">${fmtInt.format(safeNum(r.customers))}</td>
          <td class="text-end">${fmtInt.format(safeNum(r.weight_lb))}</td>
          <td class="text-end">${fmtInt.format(safeNum(r.units))}</td>
          <td class="text-end">${r.asp_lb != null ? fmtMoney2.format(r.asp_lb) : NA}</td>
          <td class="text-end">${r.asp != null ? fmtMoney2.format(r.asp) : NA}</td>
          <td class="text-end">${momentum}</td>
          <td class="text-end">${topShare}</td>
          <td class="text-end"><a class="btn btn-sm btn-outline-primary" href="${link}">View</a></td>
        `;
        tbody.appendChild(tr);
      });
    }

    const page = table.page || state.page;
    const pageSize = table.page_size || state.pageSize;
    const total = table.total_rows || 0;
    const totalPages = table.total_pages || Math.max(1, Math.ceil(total / pageSize));

    const summary = document.getElementById("salesrepsPagerSummary");
    if (summary) {
      if (total > 0) {
        const start = (page - 1) * pageSize + 1;
        const end = Math.min(page * pageSize, total);
        summary.textContent = `Showing ${start}-${end} of ${total}`;
      } else {
        summary.textContent = "No rows";
      }
    }
    const indicator = document.getElementById("salesrepsPagerIndicator");
    if (indicator) indicator.textContent = totalPages ? `Page ${page} of ${totalPages}` : "";

    const prev = document.getElementById("salesrepsPrev");
    const next = document.getElementById("salesrepsNext");
    if (prev) prev.disabled = page <= 1;
    if (next) next.disabled = page >= totalPages;
  };

  const renderBundle = (payload = {}) => {
    const data = window.normalizeBundlePayload ? window.normalizeBundlePayload(payload) : payload;
    renderKpis(data.kpis || {});
    updateColumnLabels(data.meta || {});
    renderTrend(data.charts?.trend || data.trend || {});
    renderTopReps(data.charts?.top_reps || []);
    renderEfficiency(data.charts?.scatter || []);
    renderConcentration(data.charts?.concentration || []);
    renderProfitRevenue(data.charts?.profit_vs_revenue || []);
    renderAspLeaders(data.charts?.asp_leaders || []);
    renderMarginRanking(data.charts?.margin_ranking || []);
    renderPareto(data.charts?.pareto || []);
    renderTable(data.table || {});
  };

  const fetchBundle = async () => {
    if (!bundleUrl) return;
    const reqId = ++currentReqId;
    if (currentAbort) currentAbort.abort();
    const controller = new AbortController();
    currentAbort = controller;
    updateExportLinks();
    const qs = buildQS();
    const url = qs ? `${bundleUrl}?${qs}` : bundleUrl;
    try {
      const res = await authFetch(url, { signal: controller.signal, credentials: "same-origin", headers: { Accept: "application/json" } });
      const raw = await res.json();
      if (reqId !== currentReqId) return;
      if (!res.ok) throw new Error(raw?.error?.message || `HTTP ${res.status}`);
      renderBundle(raw);
    } catch (err) {
      if (err?.name === "AbortError") return;
      console.error("salesreps bundle failed", err);
      toggleEmpty("trendChart", true);
      toggleEmpty("topRepsChart", true);
      toggleEmpty("effChart", true);
      toggleEmpty("concentrationChart", true);
      toggleEmpty("profitRevenueChart", true);
      toggleEmpty("aspChart", true);
      toggleEmpty("marginRankChart", true);
      toggleEmpty("revenueShareChart", true);
    } finally {
      if (reqId === currentReqId) {
        dispatchGlobalApplyAck({ qs: state.qs, page: "salesreps" });
      }
    }
  };

  const wireSorting = () => {
    document.querySelectorAll("#SalesRepsApp th.sortable").forEach((th) => {
      th.addEventListener("click", () => {
        const key = (th.dataset.sortKey || "revenue").toLowerCase();
        if (state.sortBy === key) {
          state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
        } else {
          state.sortBy = key;
          state.sortDir = "desc";
        }
        state.page = 1;
        document.querySelectorAll("#SalesRepsApp th.sortable").forEach((el) => el.classList.remove("asc", "desc"));
        th.classList.add(state.sortDir);
        fetchBundle();
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

  const wireRowClicks = () => {
    const tbody = document.getElementById("salesreps-table-body");
    if (!tbody) return;
    tbody.addEventListener("click", (evt) => {
      const target = evt.target;
      if (target && target.closest("a")) return;
      const row = target?.closest("tr");
      if (row && row.dataset.href) {
        window.location.href = row.dataset.href;
      }
    });
  };

  const replaceHistory = (qs) => {
    if (!window.history || typeof window.history.replaceState !== "function") return;
    const nextUrl = qs ? `${window.location.pathname}?${qs}` : window.location.pathname;
    window.history.replaceState({}, "", nextUrl);
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

  const applyFilters = (qs) => {
    state.qs = normalizeQS(qs);
    state.page = 1;
    replaceHistory(state.qs);
    fetchBundle();
  };

  const bootstrap = async (qsHint) => {
    if (bootstrapped) return;
    bootstrapped = true;
    let qs = qsHint || "";
    if (!qs) {
      const readyDetail = await waitForFiltersReady();
      qs = readyDetail?.qs || "";
    }
    if (!qs) qs = resolveInitialQS();
    if (qs) state.qs = normalizeQS(qs);
    updateExportLinks();
    fetchBundle();
  };

  const onApply = (evt) => {
    currentApplyId = evt?.detail?.applyId || null;
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
  wirePager();
  wireRowClicks();

  bootstrap();
  setTimeout(() => {
    if (!bootstrapped) bootstrap(resolveInitialQS());
  }, 900);
})();
