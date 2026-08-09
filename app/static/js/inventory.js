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
