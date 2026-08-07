(function () {
  const CHART_IDS = [
    "ciwTrendChart",
    "ciwWeightValueChart",
    "ciwWeekdayChart",
    "ciwSeasonalityChart",
    "ciwTopMixChart",
  ];

  function byId(id) {
    return document.getElementById(id);
  }

  function asArray(value) {
    return Array.isArray(value) ? value : [];
  }

  function asNumber(value) {
    const num = Number(value);
    return Number.isFinite(num) ? num : null;
  }

  function normalizeToken(value) {
    return String(value || "").trim().toLowerCase();
  }

  function readPayload() {
    const el = byId("customerWorkspaceData");
    if (!el) return null;
    try {
      return JSON.parse(el.textContent || "{}");
    } catch (_err) {
      return null;
    }
  }

  function readMeta() {
    const el = byId("CustomerDrilldownMeta");
    return {
      customerId: el?.dataset?.entityId || null,
      customerLabel: el?.dataset?.entityLabel || null,
    };
  }

  function afterFirstPaint(fn) {
    if (typeof fn !== "function") return;
    if (typeof window === "undefined") {
      fn();
      return;
    }
    if (typeof window.requestAnimationFrame === "function") {
      window.requestAnimationFrame(function () {
        window.setTimeout(fn, 0);
      });
      return;
    }
    window.setTimeout(fn, 0);
  }

  function openDrilldown(payload) {
    if (!window.universalDrilldown || typeof window.universalDrilldown.open !== "function") return;
    window.universalDrilldown.open(payload, {}, byId("CustomerDrilldownMeta"));
  }

  function attachPlotDrilldown(el, handler) {
    if (!el || typeof el.on !== "function") return;
    if (typeof el.removeAllListeners === "function") {
      el.removeAllListeners("plotly_click");
    }
    el.on("plotly_click", function (event) {
      const point = event?.points?.[0];
      const payload = typeof handler === "function" ? handler(point) : null;
      if (!payload) return;
      openDrilldown(payload);
    });
  }

  function purgeChart(el) {
    if (!el) return;
    if (typeof Plotly === "undefined" || typeof Plotly.purge !== "function") return;
    try {
      Plotly.purge(el);
    } catch (_err) {
      // Ignore purge failures and fall through to empty-state rendering.
    }
  }

  function renderEmpty(el, message) {
    if (!el) return;
    purgeChart(el);
    const text = String(message || "This visualization is unavailable for the current customer.");
    el.innerHTML = `<div class="ciw-empty">${text}</div>`;
  }

  function renderWorkspaceFallback(message) {
    const text = String(message || "Chart data is unavailable for the current customer.");
    CHART_IDS.forEach(function (id) {
      renderEmpty(byId(id), text);
    });
  }

  function getChartState(data, key) {
    const chartStates = data?.chartStates;
    const state = chartStates && typeof chartStates === "object" ? chartStates[key] : null;
    return state && typeof state === "object" ? state : {};
  }

  function hasPositiveValue(values) {
    return asArray(values).some(function (value) {
      const num = asNumber(value);
      return num !== null && num > 0;
    });
  }

  function hasRenderableValue(values) {
    return asArray(values).some(function (value) {
      return asNumber(value) !== null;
    });
  }

  function normalizeSeries(values, size) {
    const out = asArray(values).slice(0, size).map(asNumber);
    while (out.length < size) out.push(null);
    return out;
  }

  function safePlot(el, traces, layout, config, fallbackMessage) {
    if (!el) return;
    if (typeof Plotly === "undefined") {
      renderEmpty(el, fallbackMessage || "Charts are temporarily unavailable.");
      return;
    }
    try {
      purgeChart(el);
      Plotly.newPlot(el, traces, layout, config);
    } catch (_err) {
      renderEmpty(el, fallbackMessage || "This visualization is unavailable for the current customer.");
    }
  }

  function plotTrend(data) {
    const el = byId("ciwTrendChart");
    if (!el) return;
    const meta = readMeta();
    const state = getChartState(data, "trend");
    if (state.status === "empty") {
      renderEmpty(el, state.reason || "No trend data in the selected window.");
      return;
    }
    const labels = asArray(data?.trend?.labels);
    const revenue = normalizeSeries(data?.trend?.revenue, labels.length);
    const orders = normalizeSeries(data?.trend?.orders, labels.length);
    if (!labels.length || (!hasPositiveValue(revenue) && !hasPositiveValue(orders))) {
      renderEmpty(el, state.reason || "No trend data in the selected window.");
      return;
    }
    const traces = [
      {
        x: labels,
        y: revenue,
        type: "bar",
        name: "Revenue",
        marker: { color: "#2c6f86" },
        hovertemplate: "%{x}<br>Revenue %{y:$,.0f}<extra></extra>",
      },
      {
        x: labels,
        y: orders,
        type: "scatter",
        mode: "lines+markers",
        name: "Orders",
        yaxis: "y2",
        line: { color: "#9a5c11", width: 2.4 },
        hovertemplate: "%{x}<br>Orders %{y:,d}<extra></extra>",
      },
    ];
    const rolling = asArray(data?.trend?.rolling_revenue).map(asNumber);
    if (rolling.some((value) => value !== null)) {
      traces.push({
        x: labels,
        y: rolling,
        type: "scatter",
        mode: "lines",
        name: "Rolling Revenue",
        line: { color: "#17313d", dash: "dot", width: 2 },
        hovertemplate: "%{x}<br>Rolling Revenue %{y:$,.0f}<extra></extra>",
      });
    }
    const previous = asArray(data?.trend?.previous_year_revenue).map(asNumber);
    if (previous.some((value) => value !== null)) {
      traces.push({
        x: labels,
        y: previous,
        type: "scatter",
        mode: "lines",
        name: "Prior Year",
        line: { color: "#6f7f87", dash: "dot", width: 1.8 },
        hovertemplate: "%{x}<br>Prior Year %{y:$,.0f}<extra></extra>",
      });
    }
    safePlot(
      el,
      traces,
      {
        margin: { t: 10, r: 48, l: 54, b: 52 },
        xaxis: { type: "category", automargin: true },
        yaxis: { title: "Revenue", tickformat: "$,.0f", gridcolor: "rgba(23,49,61,0.08)" },
        yaxis2: { title: "Orders", overlaying: "y", side: "right", gridcolor: "rgba(0,0,0,0)" },
        hovermode: "x unified",
        legend: { orientation: "h" },
        paper_bgcolor: "rgba(0,0,0,0)",
        plot_bgcolor: "rgba(0,0,0,0)",
      },
      { displayModeBar: false, responsive: true },
      state.reason || "Trend data is unavailable for the selected window."
    );
    attachPlotDrilldown(el, function (point) {
      const month = String(point?.x || "").trim();
      if (!month) return null;
      return {
        source_page: "customer_drilldown",
        source_section: "Trend, Lifecycle, and Value per lb",
        source_widget: "Revenue and Orders Trend",
        source_entity_type: "customer",
        source_entity_id: meta.customerId,
        source_entity_label: meta.customerLabel,
        requested_target: "workspace",
        clicked_metric: point?.data?.name || "Revenue",
        clicked_metric_value: point?.y,
        clicked_time_grain: "month",
        clicked_time_value: month,
        extra: {
          workspace_kind: "fact_orders",
          filter_mode: "current_window",
          target_filters: { customer_ids: meta.customerId ? [meta.customerId] : [] },
        },
      };
    });
  }

  function plotWeightValue(data) {
    const el = byId("ciwWeightValueChart");
    if (!el) return;
    const meta = readMeta();
    const state = getChartState(data, "weight_value");
    if (state.status === "empty") {
      renderEmpty(el, state.reason || "No weight trend data in the selected window.");
      return;
    }
    const labels = asArray(data?.trend?.labels);
    const weight = normalizeSeries(data?.trend?.weight_lb, labels.length);
    if (!labels.length || !hasPositiveValue(weight)) {
      renderEmpty(el, state.reason || "No weight trend data in the selected window.");
      return;
    }
    const traces = [
      {
        x: labels,
        y: weight,
        type: "bar",
        name: "Weight lb",
        marker: { color: "#1d7f5f" },
        hovertemplate: "%{x}<br>Weight %{y:,.0f} lb<extra></extra>",
      },
      {
        x: labels,
        y: normalizeSeries(data?.trend?.revenue_per_lb, labels.length),
        type: "scatter",
        mode: "lines+markers",
        name: "Revenue/lb",
        yaxis: "y2",
        line: { color: "#9a5c11", width: 2.2 },
        hovertemplate: "%{x}<br>Revenue/lb %{y:$,.2f}<extra></extra>",
      },
    ];
    if (Boolean(data?.showCosts)) {
      const profitPerLb = normalizeSeries(data?.trend?.profit_per_lb, labels.length);
      if (profitPerLb.some((value) => value !== null)) {
        traces.push({
          x: labels,
          y: profitPerLb,
          type: "scatter",
          mode: "lines",
          name: "Profit/lb",
          yaxis: "y2",
          line: { color: "#9e3d34", width: 2, dash: "dot" },
          hovertemplate: "%{x}<br>Profit/lb %{y:$,.2f}<extra></extra>",
        });
      }
    }
    safePlot(
      el,
      traces,
      {
        margin: { t: 10, r: 52, l: 54, b: 52 },
        xaxis: { type: "category", automargin: true },
        yaxis: { title: "Weight lb", gridcolor: "rgba(23,49,61,0.08)" },
        yaxis2: { title: "Value/lb", overlaying: "y", side: "right", tickformat: "$,.2f" },
        hovermode: "x unified",
        legend: { orientation: "h" },
        paper_bgcolor: "rgba(0,0,0,0)",
        plot_bgcolor: "rgba(0,0,0,0)",
      },
      { displayModeBar: false, responsive: true },
      state.reason || "No weight trend data in the selected window."
    );
    attachPlotDrilldown(el, function (point) {
      const month = String(point?.x || "").trim();
      if (!month) return null;
      return {
        source_page: "customer_drilldown",
        source_section: "Weight & Operational Value",
        source_widget: "Weight and Value Trend",
        source_entity_type: "customer",
        source_entity_id: meta.customerId,
        source_entity_label: meta.customerLabel,
        requested_target: "workspace",
        clicked_metric: point?.data?.name || "Weight lb",
        clicked_metric_value: point?.y,
        clicked_time_grain: "month",
        clicked_time_value: month,
        extra: {
          workspace_kind: "fact_orders",
          filter_mode: "current_window",
          target_filters: { customer_ids: meta.customerId ? [meta.customerId] : [] },
        },
      };
    });
  }

  function plotWeekday(data) {
    const el = byId("ciwWeekdayChart");
    if (!el) return;
    const state = getChartState(data, "weekday");
    if (state.status === "empty") {
      renderEmpty(el, state.reason || "No weekday ordering pattern is available.");
      return;
    }
    const weekday = data?.weekday || {};
    const labels = asArray(weekday?.labels);
    const revenue = normalizeSeries(weekday?.revenue, labels.length);
    const weight = normalizeSeries(weekday?.weight_lb, labels.length);
    const orders = normalizeSeries(weekday?.orders, labels.length);
    if (!labels.length || (!hasPositiveValue(revenue) && !hasPositiveValue(weight) && !hasPositiveValue(orders))) {
      renderEmpty(el, state.reason || "No weekday ordering pattern is available.");
      return;
    }
    safePlot(
      el,
      [
        {
          x: labels,
          y: revenue,
          type: "bar",
          name: "Revenue",
          marker: { color: "#2c6f86" },
          hovertemplate: "%{x}<br>Revenue %{y:$,.0f}<extra></extra>",
        },
        {
          x: labels,
          y: weight,
          type: "scatter",
          mode: "lines+markers",
          name: "Weight lb",
          yaxis: "y2",
          line: { color: "#1d7f5f", width: 2.3 },
          hovertemplate: "%{x}<br>Weight %{y:,.0f} lb<extra></extra>",
        },
      ],
      {
        margin: { t: 10, r: 52, l: 52, b: 40 },
        xaxis: { type: "category" },
        yaxis: { title: "Revenue", tickformat: "$,.0f", gridcolor: "rgba(23,49,61,0.08)" },
        yaxis2: { title: "Weight lb", overlaying: "y", side: "right" },
        hovermode: "x unified",
        legend: { orientation: "h" },
        paper_bgcolor: "rgba(0,0,0,0)",
        plot_bgcolor: "rgba(0,0,0,0)",
      },
      { displayModeBar: false, responsive: true },
      state.reason || "No weekday ordering pattern is available."
    );
  }

  function plotSeasonality(data) {
    const el = byId("ciwSeasonalityChart");
    if (!el) return;
    const meta = readMeta();
    const state = getChartState(data, "seasonality");
    if (state.status === "empty") {
      renderEmpty(el, state.reason || "No seasonality history is available yet.");
      return;
    }
    const seasonality = data?.seasonality || {};
    const months = asArray(seasonality?.months);
    const years = asArray(seasonality?.years).map(String);
    const matrix = asArray(seasonality?.matrix).map(function (row) {
      return normalizeSeries(row, months.length);
    });
    const hasSignal = matrix.some(function (row) {
      return hasPositiveValue(row);
    });
    if (!months.length || !years.length || !matrix.length || !hasSignal) {
      renderEmpty(el, state.reason || "No seasonality history is available yet.");
      return;
    }
    safePlot(
      el,
      [
        {
          type: "heatmap",
          z: matrix,
          x: months,
          y: years,
          colorscale: "YlGnBu",
          hovertemplate: "%{y} %{x}<br>Revenue %{z:$,.0f}<extra></extra>",
        },
      ],
      {
        margin: { t: 10, r: 10, l: 56, b: 40 },
        xaxis: { type: "category" },
        yaxis: { automargin: true },
        paper_bgcolor: "rgba(0,0,0,0)",
        plot_bgcolor: "rgba(0,0,0,0)",
      },
      { displayModeBar: false, responsive: true },
      state.reason || "No seasonality history is available yet."
    );
    attachPlotDrilldown(el, function (point) {
      const month = String(point?.x || "").trim();
      const year = String(point?.y || "").trim();
      if (!month || !year) return null;
      return {
        source_page: "customer_drilldown",
        source_section: "Seasonality Heatmap & Weekday Value",
        source_widget: "Seasonality Heatmap",
        source_entity_type: "customer",
        source_entity_id: meta.customerId,
        source_entity_label: meta.customerLabel,
        requested_target: "workspace",
        clicked_metric: "Seasonality Revenue",
        clicked_metric_value: point?.z,
        clicked_time_grain: "month",
        clicked_time_value: `${year}-${String(new Date(`${month} 1, 2000`).getMonth() + 1).padStart(2, "0")}`,
        extra: {
          workspace_kind: "fact_orders",
          filter_mode: "lifetime_visible",
          target_filters: { customer_ids: meta.customerId ? [meta.customerId] : [] },
        },
      };
    });
  }

  function plotTopMix(data, mode, state) {
    const el = byId("ciwTopMixChart");
    if (!el) return;
    const meta = readMeta();
    const chartState = getChartState(data, "top_mix");
    if (chartState.status === "empty") {
      renderEmpty(el, chartState.reason || "No product mix is available for the selected customer.");
      return;
    }
    const proteinFocus = normalizeToken(state?.proteinFocus);
    const rows = asArray(data?.topMixRows || data?.productRows)
      .filter(function (row) {
        return row && typeof row === "object";
      })
      .filter(function (row) {
        return !proteinFocus || normalizeToken(row?.protein_family) === proteinFocus;
      })
      .slice();
    if (!rows.length) {
      renderEmpty(
        el,
        proteinFocus
          ? "No product mix matches the selected protein family in the current filter window."
          : chartState.reason || "No product mix is available for the selected customer."
      );
      return;
    }
    const selected = mode || "revenue";
    const metric = selected === "weight" ? "weight_lb" : selected === "profit" ? "profit" : "revenue";
    const label = selected === "weight" ? "Weight lb" : selected === "profit" ? "Profit" : "Revenue";
    rows.sort((a, b) => (Number(b?.[metric] || 0) - Number(a?.[metric] || 0)));
    const top = rows
      .filter(function (row) {
        const value = asNumber(row?.[metric]);
        return value !== null && value > 0;
      })
      .slice(0, 12)
      .reverse();
    if (!top.length) {
      renderEmpty(
        el,
        selected === "weight"
          ? "No weight-bearing product mix is available in the current filter window."
          : selected === "profit"
          ? "No profit-bearing product mix is available in the current filter window."
          : chartState.reason || "No product mix is available for the selected customer."
      );
      return;
    }
    safePlot(
      el,
      [
        {
          type: "bar",
          orientation: "h",
          x: top.map((row) => Number(row?.[metric] || 0)),
          y: top.map((row) => row?.product || row?.sku || "Product"),
          customdata: top.map((row) => row?.sku || row?.product || null),
          marker: { color: selected === "weight" ? "#1d7f5f" : selected === "profit" ? "#9e3d34" : "#2c6f86" },
          hovertemplate: `%{y}<br>${label}: %{x:,.2f}<extra></extra>`,
        },
      ],
      {
        margin: { t: 8, r: 12, l: 170, b: 34 },
        xaxis: { title: label },
        yaxis: { automargin: true, type: "category" },
        paper_bgcolor: "rgba(0,0,0,0)",
        plot_bgcolor: "rgba(0,0,0,0)",
      },
      { displayModeBar: false, responsive: true },
      chartState.reason || "No product mix is available for the selected customer."
    );
    attachPlotDrilldown(el, function (point) {
      const productId = point?.customdata || point?.y;
      if (!productId) return null;
      return {
        source_page: "customer_drilldown",
        source_section: "Product & Category Intelligence",
        source_widget: "Top Mix",
        source_entity_type: "customer",
        source_entity_id: meta.customerId,
        source_entity_label: meta.customerLabel,
        requested_target: "product",
        clicked_entity_type: "product",
        clicked_entity_id: productId,
        clicked_entity_label: point?.y,
        clicked_metric: label,
        clicked_metric_value: point?.x,
        extra: {
          target_filters: { customer_ids: meta.customerId ? [meta.customerId] : [] },
        },
      };
    });
  }

  function applyProteinFocus(state) {
    const focus = normalizeToken(state?.proteinFocus);
    const targets = Array.from(document.querySelectorAll("[data-family-focus-target]"));
    targets.forEach(function (el) {
      const token = normalizeToken(el.getAttribute("data-family-focus-target"));
      const show = !focus || token === focus;
      el.style.display = show ? "" : "none";
    });
    const chips = Array.from(document.querySelectorAll("[data-protein-focus]"));
    chips.forEach(function (chip) {
      chip.classList.toggle("is-active", normalizeToken(chip.dataset.proteinFocus) === focus);
    });
  }

  function applyActionLaneFocus(state) {
    const lane = normalizeToken(state?.actionLane);
    const targets = Array.from(document.querySelectorAll("[data-action-lane-target]"));
    targets.forEach(function (el) {
      const token = normalizeToken(el.getAttribute("data-action-lane-target"));
      const show = !lane || token === lane;
      el.style.display = show ? "" : "none";
    });
    const chips = Array.from(document.querySelectorAll("[data-action-lane]"));
    chips.forEach(function (chip) {
      chip.classList.toggle("is-active", normalizeToken(chip.dataset.actionLane) === lane);
    });
  }

  function updateExportLinks(state) {
    const links = Array.from(document.querySelectorAll("[data-export-link]"));
    if (!links.length || typeof window === "undefined") return;
    const negativeOnly = Boolean(byId("ciwNegativeOnly")?.checked);
    const belowTargetOnly = Boolean(byId("ciwBelowTargetOnly")?.checked);
    links.forEach(function (link) {
      const currentHref = link.getAttribute("href");
      if (!currentHref) return;
      if (!link.dataset.baseHref) link.dataset.baseHref = currentHref;
      let url;
      try {
        url = new URL(link.dataset.baseHref, window.location.origin);
      } catch (_err) {
        return;
      }
      if (state?.proteinFocus) {
        url.searchParams.set("protein_focus", state.proteinFocusLabel || state.proteinFocus);
      } else {
        url.searchParams.delete("protein_focus");
      }
      if (state?.actionLane) {
        url.searchParams.set("action_lane", state.actionLane);
      } else {
        url.searchParams.delete("action_lane");
      }
      if (negativeOnly) {
        url.searchParams.set("negative_only", "1");
      } else {
        url.searchParams.delete("negative_only");
      }
      if (belowTargetOnly) {
        url.searchParams.set("below_target_only", "1");
      } else {
        url.searchParams.delete("below_target_only");
      }
      link.setAttribute("href", `${url.pathname}${url.search}${url.hash}`);
    });
  }

  function wireProteinFocus(state, onChange) {
    const chips = Array.from(document.querySelectorAll("[data-protein-focus]"));
    if (!chips.length) return;
    const apply = function () {
      applyProteinFocus(state);
      if (typeof onChange === "function") onChange();
    };
    chips.forEach(function (chip) {
      chip.addEventListener("click", function () {
        state.proteinFocus = normalizeToken(chip.dataset.proteinFocus);
        state.proteinFocusLabel = String(chip.dataset.proteinFocus || "").trim();
        apply();
      });
    });
    apply();
  }

  function wireActionLaneFocus(state, onChange) {
    const chips = Array.from(document.querySelectorAll("[data-action-lane]"));
    if (!chips.length) return;
    const apply = function () {
      applyActionLaneFocus(state);
      if (typeof onChange === "function") onChange();
    };
    chips.forEach(function (chip) {
      chip.addEventListener("click", function () {
        state.actionLane = normalizeToken(chip.dataset.actionLane);
        apply();
      });
    });
    apply();
  }

  function wireProductTable(state, onChange) {
    const table = byId("ciwProductTable");
    if (!table) {
      return function () {
        if (typeof onChange === "function") onChange();
      };
    }
    const rows = Array.from(table.querySelectorAll("tbody tr"));
    const search = byId("ciwProductSearch");
    const negativeOnly = byId("ciwNegativeOnly");
    const belowTargetOnly = byId("ciwBelowTargetOnly");
    const count = byId("ciwProductCount");
    const apply = function () {
      const query = String(search?.value || "").trim().toLowerCase();
      const onlyNegative = Boolean(negativeOnly?.checked);
      const onlyBelow = Boolean(belowTargetOnly?.checked);
      const proteinFocus = normalizeToken(state?.proteinFocus);
      let visible = 0;
      rows.forEach((row) => {
        const name = String(row.getAttribute("data-product") || "");
        const protein = normalizeToken(row.getAttribute("data-protein"));
        const isNegative = row.getAttribute("data-negative") === "1";
        const isBelow = row.getAttribute("data-below-target") === "1";
        const show = (!query || name.includes(query)) && (!proteinFocus || protein === proteinFocus) && (!onlyNegative || isNegative) && (!onlyBelow || isBelow);
        row.style.display = show ? "" : "none";
        if (show) visible += 1;
      });
      if (count) {
        const focusLabel = String(state?.proteinFocusLabel || "").trim();
        count.textContent = focusLabel ? `${visible} rows · ${focusLabel}` : `${visible} rows`;
      }
      if (typeof onChange === "function") onChange();
    };
    [search, negativeOnly, belowTargetOnly].forEach(function (el) {
      if (!el) return;
      el.addEventListener("input", apply);
      el.addEventListener("change", apply);
    });
    apply();
    return apply;
  }

  document.addEventListener("DOMContentLoaded", function () {
    const data = readPayload();
    const workspaceState = { proteinFocus: "", proteinFocusLabel: "", actionLane: "" };
    const mixMode = byId("ciwMixMode");
    const redrawTopMix = function () {
      if (!data) return;
      plotTopMix(data, mixMode?.value || (data.showCosts ? "profit" : "revenue"), workspaceState);
    };
    if (!data) {
      renderWorkspaceFallback("Chart data is unavailable for this customer.");
      const syncExports = function () {
        updateExportLinks(workspaceState);
      };
      const applyProductTable = wireProductTable(workspaceState, syncExports);
      wireProteinFocus(workspaceState, applyProductTable);
      wireActionLaneFocus(workspaceState, syncExports);
      syncExports();
      return;
    }
    afterFirstPaint(function () {
      plotTrend(data);
      plotWeightValue(data);
      plotWeekday(data);
      plotSeasonality(data);
      redrawTopMix();
      const syncExports = function () {
        updateExportLinks(workspaceState);
      };
      const applyProductTable = wireProductTable(workspaceState, syncExports);
      wireProteinFocus(workspaceState, function () {
        applyProductTable();
        redrawTopMix();
        syncExports();
      });
      wireActionLaneFocus(workspaceState, syncExports);
      syncExports();
      if (mixMode) {
        mixMode.addEventListener("change", function (event) {
          plotTopMix(data, event?.target?.value || "revenue", workspaceState);
        });
      }
      if (window.universalDrilldown && typeof window.universalDrilldown.enhanceAll === "function") {
        window.universalDrilldown.enhanceAll();
      }
    });
  });
})();
