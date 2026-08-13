(() => {
  "use strict";

  const root = document.getElementById("InventoryApp");
  if (!root) return;

  const currency = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
  const number = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
  const decimal = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });
  const state = { payload: null, page: 1, pageSize: 25, search: "", posture: "", controller: null, timer: null };
  const text = (id, value) => { const el = document.getElementById(id); if (el) el.textContent = value; };
  const num = (value) => Number.isFinite(Number(value)) ? Number(value) : 0;
  const money = (value) => value == null ? "Restricted" : currency.format(num(value));
  const fmt = (value) => number.format(num(value));
  const one = (value) => value == null ? "—" : decimal.format(num(value));
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));

  const actionFor = (row) => {
    if (row.posture_key === "critical") return "Expedite or substitute; confirm the stockout and backorder.";
    if (row.posture_key === "reorder") return `Review a buy of ${fmt(row.suggested_buy_units)} units to reach ${one(row.target_days)} days.`;
    if (row.posture_key === "excess") return "Pause or reduce buying; review transfer, markdown, or assortment action.";
    return "Maintain the current replenishment rhythm and monitor turns.";
  };

  const renderInsights = (rows) => {
    const target = document.getElementById("inventoryInsights");
    if (!target) return;
    target.innerHTML = (rows || []).map((row) => `
      <article class="inventory-insight" data-tone="${escapeHtml(row.tone)}">
        <span>${escapeHtml(row.label)}</span><strong>${escapeHtml(row.headline)}</strong><p>${escapeHtml(row.detail)}</p>
      </article>`).join("");
  };

  const renderKpis = (kpi) => {
    text("inventoryValue", money(kpi.inventory_value));
    text("inventoryUnits", fmt(kpi.on_hand_qty));
    text("inventoryWeeks", `${one(kpi.weeks_on_hand)} wks`);
    text("inventoryTurns", one(kpi.annual_turns));
    text("inventoryReorder", fmt(num(kpi.critical_skus) + num(kpi.reorder_skus)));
    text("inventoryExcess", fmt(kpi.excess_skus));
    text("inventoryBackorders", fmt(kpi.backorder_units));
    text("inventoryHolding", money(kpi.holding_cost_annual));
    text("inventoryWindow", kpi.start && kpi.end ? window.WAFormat.dayRange(kpi.start, kpi.end) : "Active filtered window");
  };

  const renderPosture = (rows) => {
    const target = document.getElementById("inventoryPosture");
    if (!target) return;
    const keyMap = { Critical: "critical", Reorder: "reorder", Healthy: "healthy", Excess: "excess" };
    const copy = { Critical: "Below safety stock or stocked out", Reorder: "At reorder point or below cover", Healthy: "Inside the category cover band", Excess: "Above the cover guardrail" };
    target.innerHTML = (rows || []).map((row) => `
      <article class="stock-posture-card" data-key="${keyMap[row.label] || "healthy"}">
        <strong>${fmt(row.count)}</strong><span>${escapeHtml(row.label)}</span>
        <small>${escapeHtml(copy[row.label] || "Inventory posture")} · ${money(row.inventory_value)}</small>
      </article>`).join("");
  };

  const renderActionList = (id, rows, kind) => {
    const target = document.getElementById(id);
    if (!target) return;
    if (!rows?.length) { target.innerHTML = '<div class="inventory-empty">No candidates in the active scope.</div>'; return; }
    target.innerHTML = rows.slice(0, 8).map((row) => {
      const value = kind === "buy" ? `${fmt(row.suggested_buy_units)} units` : `${one(row.days_supply)} days`;
      const detail = kind === "buy" ? `${row.posture} · ${money(row.suggested_buy_cost)}` : `${row.svsi_label} · SVSI ${one(row.svsi)}`;
      return `<div class="inventory-list-row"><div><strong title="${escapeHtml(row.product_name)}">${escapeHtml(row.product_name)}</strong><span>${escapeHtml(row.product_id)} · ${escapeHtml(detail)}</span></div><div class="inventory-list-row__value"><strong>${value}</strong><span>${escapeHtml(row.supplier)}</span></div></div>`;
    }).join("");
  };

  const renderBars = (id, rows, valueKey = "inventory_value", formatter = money) => {
    const target = document.getElementById(id);
    if (!target) return;
    const values = (rows || []).map((row) => num(row[valueKey]));
    const max = Math.max(...values, 1);
    target.innerHTML = (rows || []).map((row) => `
      <div class="inventory-bar"><div class="inventory-bar__head"><span>${escapeHtml(row.label)}</span><strong>${formatter(row[valueKey])} · ${fmt(row.count)} SKUs</strong></div><div class="inventory-bar__track"><div class="inventory-bar__fill" style="width:${Math.max(2, num(row[valueKey]) / max * 100)}%"></div></div></div>`).join("");
  };

  const renderHolding = (holding) => {
    const rows = ["capital", "service", "storage", "risk"].map((key) => ({ label: key[0].toUpperCase() + key.slice(1), value: holding[key], count: `${key === "capital" ? 5 : key === "service" ? 1 : key === "storage" ? 2 : 3}%` }));
    const target = document.getElementById("inventoryHoldingSplit");
    if (!target) return;
    const max = Math.max(...rows.map((row) => num(row.value)), 1);
    target.innerHTML = rows.map((row) => `<div class="inventory-bar"><div class="inventory-bar__head"><span>${row.label} · ${row.count}</span><strong>${money(row.value)}</strong></div><div class="inventory-bar__track"><div class="inventory-bar__fill" style="width:${Math.max(2, num(row.value) / max * 100)}%"></div></div></div>`).join("");
  };

  const renderMatrix = (rows) => {
    const target = document.getElementById("inventoryMatrix");
    if (!target) return;
    target.innerHTML = (rows || []).map((row) => `<article class="inventory-matrix-card" data-state="${escapeHtml(row.svsi_label)}"><strong title="${escapeHtml(row.product_name)}">${escapeHtml(row.product_name)}</strong><span>${escapeHtml(row.svsi_label)} · SVSI ${one(row.svsi)}</span><span>${one(row.days_supply)} days · ${one(row.annual_turns)} turns</span></article>`).join("");
  };

  const renderSuppliers = (rows) => {
    const body = document.getElementById("inventorySupplierBody");
    if (!body) return;
    body.innerHTML = rows?.length ? rows.map((row) => `<tr><td>${escapeHtml(row.supplier)}</td><td class="text-end">${fmt(row.skus)}</td><td class="text-end">${money(row.inventory_value)}</td><td class="text-end">${fmt(row.reorder_skus)}</td><td class="text-end">${fmt(row.backorders)}</td></tr>`).join("") : '<tr><td colspan="5" class="text-center text-muted py-4">No supplier exposure in scope.</td></tr>';
  };

  const renderTable = (table) => {
    const body = document.getElementById("inventoryTableBody");
    if (!body) return;
    const rows = table?.rows || [];
    body.innerHTML = rows.length ? rows.map((row) => `<tr>
      <td><div class="inventory-product-name">${escapeHtml(row.product_name)}<small>${escapeHtml(row.product_id)} · ${escapeHtml(row.supplier)}</small></div></td>
      <td><span class="inventory-status" data-key="${escapeHtml(row.posture_key)}">${escapeHtml(row.posture)}</span></td>
      <td><strong>${escapeHtml(row.abc_class)}</strong></td><td class="text-end">${fmt(row.on_hand_qty)}</td><td class="text-end">${one(row.days_supply)}</td><td class="text-end">${one(row.annual_turns)}</td><td class="text-end">${one(row.svsi)}</td><td class="text-end">${money(row.inventory_value)}</td><td>${escapeHtml(actionFor(row))}</td>
    </tr>`).join("") : '<tr><td colspan="9" class="text-center text-muted py-5">No inventory rows match this view.</td></tr>';
    const totalPages = Math.max(1, Math.ceil(num(table.total) / num(table.page_size || state.pageSize)));
    state.page = num(table.page) || 1;
    text("inventoryTableStatus", `${fmt(table.total)} SKUs in the current view`);
    text("inventoryPage", `Page ${state.page} of ${totalPages}`);
    document.getElementById("inventoryPrev").disabled = state.page <= 1;
    document.getElementById("inventoryNext").disabled = state.page >= totalPages;
  };

  // ---------------------------------------------------------------------
  // Charts
  //
  // Five shapes the bar lists cannot carry: ABC is a concentration claim;
  // cover and aging are distributions whose means hide both ends; movement is
  // a relationship between two axes; carrying cost is a composition. Plotly
  // is fetched on idle after first paint, so every draw waits for it and the
  // page is complete without it - the static build freezes these to SVG.
  // ---------------------------------------------------------------------
  const plotTheme = () => {
    const css = getComputedStyle(document.documentElement);
    const pick = (name, fallback) => (css.getPropertyValue(name) || "").trim() || fallback;
    return {
      text: pick("--wa-text-dim", "#94a3b8"),
      grid: pick("--wa-border", "rgba(148,163,184,.22)"),
      accent: pick("--wa-accent", "#34d399"),
      danger: pick("--wa-danger", "#f87171"),
      warn: pick("--wa-warning", "#fbbf24"),
      info: pick("--wa-info", "#60a5fa"),
    };
  };

  const baseLayout = (theme, extra) => Object.assign({
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: theme.text, size: 11 },
    margin: { l: 56, r: 18, t: 12, b: 44 },
    showlegend: false,
  }, extra || {});

  const drawPareto = (charts, theme) => {
    const host = document.getElementById("inventoryParetoChart");
    const data = charts?.abc_pareto;
    if (!host || !data?.cum_value_pct?.length) return;
    const skuPct = data.sku_share_pct;
    const cumPct = data.cum_value_pct;
    const aEdge = data.a_edge ? (data.a_edge / data.sku_count) * 100 : null;

    // The headline reading, stated rather than left to the eye.
    const aShare = data.a_edge ? (data.a_edge / data.sku_count) * 100 : null;
    if (aShare != null) {
      text("inventoryParetoRead",
        `The top ${aShare.toFixed(0)}% of SKUs (class A) hold 80% of inventory value. `
        + `The curve's steepness is the concentration.`);
    }

    const shapes = [];
    if (aEdge != null) {
      shapes.push({
        type: "rect", xref: "x", yref: "paper", x0: 0, x1: aEdge, y0: 0, y1: 1,
        fillcolor: theme.accent, opacity: 0.10, line: { width: 0 }, layer: "below",
      });
    }
    window.Plotly.newPlot(host, [
      {
        type: "scatter", mode: "lines", x: skuPct, y: cumPct,
        line: { color: theme.accent, width: 3 },
        hovertemplate: "Top %{x:.0f}% of SKUs<br>%{y:.1f}% of inventory value<extra></extra>",
        name: "Cumulative value",
      },
      {
        // Perfect-evenness reference: without it "steep" has nothing to be steep against.
        type: "scatter", mode: "lines", x: [0, 100], y: [0, 100],
        line: { color: theme.grid, width: 1, dash: "dot" },
        hoverinfo: "skip", name: "Even distribution",
      },
    ], baseLayout(theme, {
      height: 260,
      xaxis: { title: "Share of SKUs", ticksuffix: "%", range: [0, 100], gridcolor: theme.grid, linecolor: theme.grid, zeroline: false },
      yaxis: { title: "Share of value", ticksuffix: "%", range: [0, 100], gridcolor: theme.grid, linecolor: theme.grid, zeroline: false },
      shapes,
    }), { displayModeBar: false, responsive: true });
  };

  const drawCover = (charts, theme) => {
    const host = document.getElementById("inventoryCoverChart");
    const data = charts?.cover_histogram;
    if (!host || !data?.bins?.length) return;
    const bins = data.bins;
    const perishable = num(data.target_perishable_days);
    const ambient = num(data.target_ambient_days);

    // Colour by where a bucket sits against the two targets: short of the
    // perishable target is a service risk, past the ambient target is cash.
    const colour = (bin) => {
      if (bin.high != null && bin.high <= perishable) return theme.danger;
      if (bin.low >= ambient) return theme.warn;
      return theme.accent;
    };
    const short = bins.filter((b) => b.high != null && b.high <= perishable).reduce((a, b) => a + num(b.skus), 0);
    const over = bins.filter((b) => b.low >= ambient).reduce((a, b) => a + num(b.skus), 0);
    text("inventoryCoverRead",
      `${fmt(short)} SKUs sit under the ${perishable}-day perishable target; `
      + `${fmt(over)} carry more than the ${ambient}-day ambient target. The middle is working stock.`);

    window.Plotly.newPlot(host, [{
      type: "bar",
      x: bins.map((b) => b.label),
      y: bins.map((b) => num(b.skus)),
      marker: { color: bins.map(colour), opacity: 0.85 },
      customdata: bins.map((b) => num(b.value)),
      hovertemplate: "%{x} days of supply<br>%{y} SKUs<br>%{customdata:$,.0f} of stock<extra></extra>",
    }], baseLayout(theme, {
      height: 260,
      xaxis: { title: "Days of supply", gridcolor: "rgba(0,0,0,0)", linecolor: theme.grid },
      yaxis: { title: "SKUs", gridcolor: theme.grid, linecolor: theme.grid, zeroline: false },
    }), { displayModeBar: false, responsive: true });
  };

  const drawMovement = (charts, theme) => {
    const host = document.getElementById("inventoryMovementChart");
    const data = charts?.movement;
    if (!host || !data?.value?.length) return;

    const palette = {
      "Core / protect": theme.accent,
      "Fast / lean": theme.info,
      "Slow / cash tied": theme.warn,
      "Tail / monitor": theme.text,
    };
    const groups = new Map();
    data.value.forEach((value, i) => {
      const key = data.quadrant[i] || "Unclassified";
      if (!groups.has(key)) groups.set(key, { x: [], y: [], label: [], days: [] });
      const bucket = groups.get(key);
      bucket.x.push(num(data.usage[i]));
      bucket.y.push(num(value));
      bucket.label.push(data.label[i]);
      bucket.days.push(data.days_supply[i]);
    });

    const slow = groups.get("Slow / cash tied");
    if (slow) {
      const tied = slow.y.reduce((a, b) => a + b, 0);
      text("inventoryMovementRead",
        `Each point is a SKU: weekly usage against inventory value, split at the portfolio medians. `
        + `${fmt(slow.y.length)} SKUs in "slow / cash tied" hold ${money(tied)} — high value, low movement.`);
    }

    const traces = [...groups.entries()].map(([name, bucket]) => ({
      // Deliberately `scatter`, not `scattergl`: the static build freezes every
      // chart through `Plotly.toImage(..., {format:'svg'})`, and a WebGL trace
      // has nothing for the SVG exporter to serialise - it would publish a
      // blank frame. A few hundred SVG markers is well inside comfort.
      type: "scatter", mode: "markers", name,
      x: bucket.x, y: bucket.y,
      customdata: bucket.label.map((label, i) => [label, bucket.days[i]]),
      marker: { size: 7, opacity: 0.72, color: palette[name] || theme.text },
      hovertemplate: "<b>%{customdata[0]}</b><br>%{x:,.1f} units/week<br>%{y:$,.0f} on hand<br>%{customdata[1]} days of supply<extra>" + name + "</extra>",
    }));

    window.Plotly.newPlot(host, traces, baseLayout(theme, {
      height: 380,
      showlegend: true,
      legend: { orientation: "h", y: -0.18, font: { size: 10 } },
      margin: { l: 68, r: 18, t: 12, b: 56 },
      // Both axes are heavily skewed - a handful of SKUs carry most of the
      // value - so a linear scale would stack every other point on the origin.
      xaxis: { title: "Average weekly usage (units)", type: "log", gridcolor: theme.grid, linecolor: theme.grid },
      yaxis: { title: "Inventory value", type: "log", tickprefix: "$", gridcolor: theme.grid, linecolor: theme.grid },
    }), { displayModeBar: false, responsive: true });
  };

  const drawAging = (rows, theme) => {
    const host = document.getElementById("inventoryAging");
    const data = Array.isArray(rows) ? rows : [];
    if (!host || !data.length) return;
    const labels = data.map((row) => String(row.label || "Unknown"));
    const values = data.map((row) => num(row.inventory_value));
    const counts = data.map((row) => num(row.count));
    const totalValue = values.reduce((sum, value) => sum + value, 0);
    const aged = (label) => /^(91-180|181-365|365\+)$/.test(label);
    const agedValue = data.reduce((sum, row, index) => sum + (aged(labels[index]) ? num(row.inventory_value) : 0), 0);
    const agedCount = data.reduce((sum, row, index) => sum + (aged(labels[index]) ? num(row.count) : 0), 0);
    text("inventoryAgingRead", totalValue > 0
      ? `${fmt(agedCount)} SKUs older than 90 days hold ${money(agedValue)} (${(agedValue / totalValue * 100).toFixed(1)}% of inventory value).`
      : `${fmt(agedCount)} SKUs are older than 90 days in the active scope.`);

    window.Plotly.newPlot(host, [
      {
        type: "bar", name: "Inventory value", x: labels, y: values,
        marker: { color: labels.map((label) => aged(label) ? theme.warn : theme.accent), opacity: 0.82 },
        hovertemplate: "%{x} days<br>%{y:$,.0f} of inventory<extra></extra>",
      },
      {
        type: "scatter", mode: "lines+markers", name: "SKUs", x: labels, y: counts, yaxis: "y2",
        line: { color: theme.info, width: 2 }, marker: { color: theme.info, size: 7 },
        hovertemplate: "%{x} days<br>%{y:,.0f} SKUs<extra></extra>",
      },
    ], baseLayout(theme, {
      height: 250,
      showlegend: true,
      legend: { orientation: "h", y: 1.16, x: 0, font: { size: 10 } },
      margin: { l: 64, r: 52, t: 32, b: 46 },
      xaxis: { title: "Age of stock position (days)", gridcolor: "rgba(0,0,0,0)", linecolor: theme.grid },
      yaxis: { title: "Inventory value", tickprefix: "$", gridcolor: theme.grid, linecolor: theme.grid, zeroline: false },
      yaxis2: { title: "SKUs", overlaying: "y", side: "right", rangemode: "tozero", gridcolor: "rgba(0,0,0,0)", zeroline: false },
    }), { displayModeBar: false, responsive: true });
  };

  const drawHolding = (holding, theme) => {
    const host = document.getElementById("inventoryHoldingSplit");
    if (!host) return;
    const rows = [
      { label: "Capital", rate: 5, value: holding?.capital },
      { label: "Service", rate: 1, value: holding?.service },
      { label: "Storage", rate: 2, value: holding?.storage },
      { label: "Risk", rate: 3, value: holding?.risk },
    ].filter((row) => row.value != null && Number.isFinite(Number(row.value)));
    if (!rows.length) return;
    const total = rows.reduce((sum, row) => sum + num(row.value), 0);
    text("inventoryHoldingRead", `${money(total)} a year under the explicit 11% planning assumption; capital and risk explain ${money(num(holding.capital) + num(holding.risk))}.`);

    window.Plotly.newPlot(host, [{
      type: "pie", hole: 0.58,
      labels: rows.map((row) => row.label),
      values: rows.map((row) => num(row.value)),
      customdata: rows.map((row) => row.rate),
      marker: { colors: [theme.info, theme.accent, theme.warn, theme.danger] },
      textinfo: "label+percent", textposition: "outside",
      hovertemplate: "<b>%{label}</b><br>%{value:$,.0f} a year<br>%{customdata}% of inventory value<extra></extra>",
      sort: false,
    }], baseLayout(theme, {
      height: 250,
      margin: { l: 42, r: 42, t: 18, b: 18 },
      annotations: [{
        text: `${money(total)}<br><span style='font-size:10px'>annual</span>`,
        showarrow: false, font: { size: 13, color: theme.text },
      }],
    }), { displayModeBar: false, responsive: true });
  };

  const drawCharts = (payload) => {
    const charts = payload?.charts;
    if (!charts) return;
    const draw = () => {
      const theme = plotTheme();
      try { drawPareto(charts, theme); } catch (error) { console.warn("pareto chart failed", error); }
      try { drawCover(charts, theme); } catch (error) { console.warn("cover chart failed", error); }
      try { drawMovement(charts, theme); } catch (error) { console.warn("movement chart failed", error); }
      try { drawAging(payload?.aging, theme); } catch (error) { console.warn("aging chart failed", error); }
      try { drawHolding(payload?.holding_cost || {}, theme); } catch (error) { console.warn("holding-cost chart failed", error); }
    };
    if (window.ChartUtils && window.ChartUtils.whenPlotlyReady) window.ChartUtils.whenPlotlyReady(draw);
    else if (window.Plotly) draw();
  };

  const render = (payload) => {
    state.payload = payload;
    renderInsights(payload.insights);
    renderKpis(payload.kpis || {});
    renderPosture(payload.posture);
    renderActionList("purchasePlanList", payload.purchase_plan, "buy");
    renderActionList("rebalanceList", payload.rebalancing, "excess");
    text("purchasePlanCount", `${fmt(payload.purchase_plan?.length)} SKUs`);
    text("rebalanceCount", `${fmt(payload.rebalancing?.length)} SKUs`);
    renderBars("inventoryAbc", payload.abc);
    renderBars("inventoryQuadrants", payload.movement_quadrants);
    renderBars("inventoryAging", payload.aging);
    renderHolding(payload.holding_cost || {});
    renderMatrix(payload.demand_matrix);
    renderSuppliers(payload.suppliers);
    renderTable(payload.table);
    drawCharts(payload);
    text("inventorySource", payload.meta?.inventory_source || "Latest observed SKU inventory snapshot");
    document.getElementById("inventoryExportBtn").disabled = false;
  };

  const requestUrl = () => {
    const params = new URLSearchParams(window.location.search);
    params.set("page", String(state.page)); params.set("page_size", String(state.pageSize)); params.set("sort_by", "priority");
    if (state.search) params.set("search", state.search); else params.delete("search");
    if (state.posture) params.set("posture", state.posture); else params.delete("posture");
    return `${root.dataset.bundleUrl}?${params.toString()}`;
  };

  const load = async () => {
    state.controller?.abort(); state.controller = new AbortController();
    try {
      const fetcher = window.authFetch ? window.authFetch : window.fetch.bind(window);
      const response = await fetcher(requestUrl(), { credentials: "same-origin", signal: state.controller.signal, headers: { Accept: "application/json" } });
      const payload = await response.json();
      if (!response.ok || payload.error) throw new Error(payload.error?.message || `Inventory request failed (${response.status})`);
      document.getElementById("inventoryAlert").classList.add("d-none");
      render(payload);
    } catch (error) {
      if (error?.name === "AbortError") return;
      const alert = document.getElementById("inventoryAlert");
      alert.textContent = error?.message || "Inventory analysis could not be loaded."; alert.classList.remove("d-none");
    }
  };

  const csvCell = (value) => `"${String(value ?? "").replace(/"/g, '""')}"`;
  const exportCsv = () => {
    const rows = state.payload?.table?.rows || [];
    const header = ["SKU","Product","Supplier","Posture","ABC","OnHandQty","DaysOfSupply","AnnualTurns","SVSI","InventoryValue","SuggestedBuyUnits","Action"];
    const lines = [header, ...rows.map((row) => [row.product_id,row.product_name,row.supplier,row.posture,row.abc_class,row.on_hand_qty,row.days_supply,row.annual_turns,row.svsi,row.inventory_value,row.suggested_buy_units,actionFor(row)])].map((row) => row.map(csvCell).join(","));
    const url = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" }));
    const link = document.createElement("a"); link.href = url; link.download = "inventory_analysis.csv"; link.click(); URL.revokeObjectURL(url);
  };

  document.getElementById("inventorySearch")?.addEventListener("input", (event) => { clearTimeout(state.timer); state.timer = setTimeout(() => { state.search = event.target.value.trim(); state.page = 1; load(); }, 250); });
  document.getElementById("inventoryPostureFilter")?.addEventListener("change", (event) => { state.posture = event.target.value; state.page = 1; load(); });
  document.getElementById("inventoryPrev")?.addEventListener("click", () => { if (state.page > 1) { state.page -= 1; load(); } });
  document.getElementById("inventoryNext")?.addEventListener("click", () => { state.page += 1; load(); });
  document.getElementById("inventoryExportBtn")?.addEventListener("click", exportCsv);
  window.addEventListener("globalFilters:changed", () => { state.page = 1; load(); });
  window.addEventListener("globalFilters:applied", (event) => { if (event.detail?.page !== "inventory") { state.page = 1; load(); } });
  load();
})();
