
(function () {
  const root = document.getElementById("LaborPage");
  const dataNode = document.getElementById("LaborPageData");
  if (!root || !dataNode) return;

  let payload = {};
  try {
    payload = JSON.parse(dataNode.textContent || "{}");
  } catch (_err) {
    payload = {};
  }

  const filters = (payload && payload.filters) || {};

  function number(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }

  function hasRows(rows) {
    return Array.isArray(rows) && rows.length > 0;
  }

  function renderEmpty(target, message) {
    if (!target) return;
    target.innerHTML = `<div class="labor-empty">${message}</div>`;
  }

  function updateUrl(mutator) {
    const url = new URL(window.location.href);
    const params = url.searchParams;
    mutator(params);
    window.location.assign(url.toString());
  }

  function applyFilters(updates, clearKeys = []) {
    updateUrl((params) => {
      params.delete("page");
      clearKeys.forEach((key) => params.delete(key));
      Object.entries(updates || {}).forEach(([key, value]) => {
        params.delete(key);
        if (Array.isArray(value)) {
          value.filter((item) => item !== null && item !== undefined && item !== "").forEach((item) => params.append(key, String(item)));
          return;
        }
        if (value !== null && value !== undefined && value !== "") {
          params.append(key, String(value));
        }
      });
    });
  }

  function setSingleFilter(key, value) {
    applyFilters({ [key]: value });
  }

  function setDateWindow(start, end) {
    applyFilters({ start, end });
  }

  function setSort(sortKey) {
    updateUrl((params) => {
      const currentSort = params.get("sort") || filters.sort_by || "labor_cost";
      const currentDir = params.get("sort_dir") || filters.sort_dir || "desc";
      const nextDir = currentSort === sortKey && currentDir === "desc" ? "asc" : "desc";
      params.set("sort", sortKey);
      params.set("sort_dir", nextDir);
      params.delete("page");
    });
  }

  function setPage(page) {
    if (!page || page < 1) return;
    updateUrl((params) => params.set("page", String(page)));
  }

  function bindInteractions() {
    document.addEventListener("click", (event) => {
      const filterTarget = event.target.closest(".js-filter-link");
      if (filterTarget) {
        event.preventDefault();
        const key = filterTarget.getAttribute("data-param");
        const value = filterTarget.getAttribute("data-value");
        if (key && value) setSingleFilter(key, value);
        return;
      }

      const sortTarget = event.target.closest(".js-sort");
      if (sortTarget) {
        event.preventDefault();
        const sortKey = sortTarget.getAttribute("data-sort");
        if (sortKey) setSort(sortKey);
        return;
      }

      const pageTarget = event.target.closest(".js-page");
      if (pageTarget) {
        event.preventDefault();
        const page = Number(pageTarget.getAttribute("data-page") || "0");
        if (page > 0) setPage(page);
      }
    });
  }

  function formatValue(value, format = "number", multiplier = 1) {
    const adjusted = number(value) * multiplier;
    if (format === "currency") {
      return new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: "USD",
        maximumFractionDigits: adjusted >= 100 ? 0 : 2,
      }).format(adjusted);
    }
    if (format === "percent") return `${adjusted.toFixed(1)}%`;
    if (format === "score") return `${adjusted.toFixed(0)}`;
    if (format === "integer") return `${adjusted.toFixed(0)}`;
    return `${adjusted.toFixed(1)}`;
  }

  function baseLayout(extra = {}) {
    return {
      margin: { l: 58, r: 24, t: 14, b: 48 },
      paper_bgcolor: "transparent",
      plot_bgcolor: "transparent",
      font: { color: "#15313b", family: "inherit" },
      hovermode: "closest",
      ...extra,
    };
  }

  function renderBarChart(targetId, rows, { valueKey, labelKey, clickParam = null, clickKey = null, color = "#0d766f", format = "number", orientation = "h", multiplier = 1 } = {}) {
    const target = document.getElementById(targetId);
    if (!target || !window.Plotly) return;
    if (!hasRows(rows)) {
      renderEmpty(target, "No data is available for this chart.");
      return;
    }

    const values = rows.map((row) => number(row[valueKey]) * multiplier);
    const labels = rows.map((row) => row[labelKey] || "Unknown");
    const custom = rows.map((row) => row[clickKey || labelKey] || "Unknown");
    const texts = values.map((value) => formatValue(value / multiplier, format, multiplier));
    const reversed = orientation === "h";

    Plotly.newPlot(
      target,
      [
        {
          type: "bar",
          orientation,
          x: reversed ? values.slice().reverse() : labels,
          y: reversed ? labels.slice().reverse() : values,
          customdata: reversed ? custom.slice().reverse() : custom,
          text: reversed ? texts.slice().reverse() : texts,
          marker: { color, line: { color: "rgba(12, 37, 43, 0.14)", width: 1 } },
          hovertemplate: orientation === "h" ? "%{y}<br>%{text}<extra></extra>" : "%{x}<br>%{text}<extra></extra>",
        },
      ],
      baseLayout(
        orientation === "h"
          ? { xaxis: { gridcolor: "#e6edf1" }, yaxis: { automargin: true } }
          : { xaxis: { tickangle: -28 }, yaxis: { gridcolor: "#e6edf1" } }
      ),
      { displayModeBar: false, responsive: true }
    );

    if (clickParam) {
      target.on("plotly_click", (event) => {
        const point = event.points && event.points[0];
        if (!point || !point.customdata) return;
        setSingleFilter(clickParam, point.customdata);
      });
    }
  }

  function renderGroupedShareChart(targetId, rows, labelKey, clickParam, clickKey = null) {
    const target = document.getElementById(targetId);
    if (!target || !window.Plotly) return;
    if (!hasRows(rows)) {
      renderEmpty(target, "No share rows are available.");
      return;
    }
    const limited = rows.slice(0, 12);
    Plotly.newPlot(
      target,
      [
        {
          type: "bar",
          x: limited.map((row) => row[labelKey] || "Unknown"),
          y: limited.map((row) => number(row.premium_share_pct) * 100),
          name: "Premium %",
          customdata: limited.map((row) => row[clickKey || labelKey] || "Unknown"),
          marker: { color: "#b67718" },
          hovertemplate: "%{x}<br>Premium %{y:.1f}%<extra></extra>",
        },
        {
          type: "bar",
          x: limited.map((row) => row[labelKey] || "Unknown"),
          y: limited.map((row) => number(row.absence_share_pct) * 100),
          name: "Absence %",
          customdata: limited.map((row) => row[clickKey || labelKey] || "Unknown"),
          marker: { color: "#bc4935" },
          hovertemplate: "%{x}<br>Absence %{y:.1f}%<extra></extra>",
        },
      ],
      baseLayout({ barmode: "group", xaxis: { tickangle: -28 }, yaxis: { gridcolor: "#e6edf1", ticksuffix: "%" }, legend: { orientation: "h" } }),
      { displayModeBar: false, responsive: true }
    );
    if (clickParam) {
      target.on("plotly_click", (event) => {
        const point = event.points && event.points[0];
        if (!point || !point.customdata) return;
        setSingleFilter(clickParam, point.customdata);
      });
    }
  }

  function renderScatterChart(targetId, rows) {
    const target = document.getElementById(targetId);
    if (!target || !window.Plotly) return;
    if (!hasRows(rows)) {
      renderEmpty(target, "No department comparison is available.");
      return;
    }
    Plotly.newPlot(
      target,
      [
        {
          type: "scatter",
          mode: "markers+text",
          x: rows.map((row) => number(row.paid_hours)),
          y: rows.map((row) => number(row.labor_cost)),
          text: rows.map((row) => row.department_name || "Unknown"),
          textposition: "top center",
          customdata: rows.map((row) => row.department_name || "Unknown"),
          marker: {
            size: rows.map((row) => Math.max(12, Math.min(34, number(row.priority_score) / 3 || 14))),
            color: rows.map((row) => number(row.blended_rate)),
            colorscale: "Tealgrn",
            line: { color: "rgba(12, 39, 45, 0.18)", width: 1 },
            showscale: false,
          },
          hovertemplate: "%{text}<br>Hours %{x:,.1f}<br>Cost %{y:$,.2f}<extra></extra>",
        },
      ],
      baseLayout({ xaxis: { title: "Paid Hours", gridcolor: "#e6edf1" }, yaxis: { title: "Labor Cost", tickprefix: "$", gridcolor: "#e6edf1" } }),
      { displayModeBar: false, responsive: true }
    );
    target.on("plotly_click", (event) => {
      const point = event.points && event.points[0];
      if (!point || !point.customdata) return;
      setSingleFilter("department", point.customdata);
    });
  }

  function renderTrendChart(targetId, rows) {
    const target = document.getElementById(targetId);
    if (!target || !window.Plotly) return;
    if (!hasRows(rows)) {
      renderEmpty(target, "No trend data is available.");
      return;
    }
    const dates = rows.map((row) => row.labor_date);
    Plotly.newPlot(
      target,
      [
        {
          type: "scatter",
          mode: "lines+markers",
          name: "Labor Cost",
          x: dates,
          y: rows.map((row) => number(row.labor_cost)),
          customdata: dates,
          line: { color: "#0d766f", width: 3 },
          hovertemplate: "%{x}<br>Cost %{y:$,.2f}<extra></extra>",
        },
        {
          type: "scatter",
          mode: "lines",
          name: "Paid Hours",
          x: dates,
          y: rows.map((row) => number(row.paid_hours)),
          yaxis: "y2",
          line: { color: "#b67718", width: 2 },
          hovertemplate: "%{x}<br>Hours %{y:,.1f}<extra></extra>",
        },
        {
          type: "scatter",
          mode: "lines",
          name: "Premium Cost",
          x: dates,
          y: rows.map((row) => number(row.premium_cost)),
          line: { color: "#d48a1d", width: 1.6, dash: "dot" },
          hovertemplate: "%{x}<br>Premium %{y:$,.2f}<extra></extra>",
        },
        {
          type: "scatter",
          mode: "lines",
          name: "Absence Cost",
          x: dates,
          y: rows.map((row) => number(row.absence_cost)),
          line: { color: "#bc4935", width: 1.6, dash: "dash" },
          hovertemplate: "%{x}<br>Absence %{y:$,.2f}<extra></extra>",
        },
      ],
      baseLayout({ yaxis: { title: "Cost", tickprefix: "$", gridcolor: "#e6edf1" }, yaxis2: { title: "Hours", overlaying: "y", side: "right", showgrid: false }, legend: { orientation: "h" } }),
      { displayModeBar: false, responsive: true }
    );
    target.on("plotly_click", (event) => {
      const point = event.points && event.points[0];
      if (!point || !point.customdata) return;
      setDateWindow(point.customdata, point.customdata);
    });
  }

  function renderRateTrendChart(targetId, rows) {
    const target = document.getElementById(targetId);
    if (!target || !window.Plotly) return;
    if (!hasRows(rows)) {
      renderEmpty(target, "No blended-rate trend is available.");
      return;
    }
    const dates = rows.map((row) => row.labor_date);
    Plotly.newPlot(
      target,
      [
        {
          type: "scatter",
          mode: "lines+markers",
          name: "Blended Rate",
          x: dates,
          y: rows.map((row) => number(row.blended_rate)),
          customdata: dates,
          line: { color: "#295fa5", width: 3 },
          hovertemplate: "%{x}<br>Rate %{y:$,.2f}<extra></extra>",
        },
        {
          type: "scatter",
          mode: "lines",
          name: "Premium Share %",
          x: dates,
          y: rows.map((row) => number(row.premium_share_pct) * 100),
          yaxis: "y2",
          line: { color: "#b67718", width: 2 },
          hovertemplate: "%{x}<br>Premium %{y:.1f}%<extra></extra>",
        },
        {
          type: "scatter",
          mode: "lines",
          name: "Absence Share %",
          x: dates,
          y: rows.map((row) => number(row.absence_share_pct) * 100),
          yaxis: "y2",
          line: { color: "#bc4935", width: 2, dash: "dot" },
          hovertemplate: "%{x}<br>Absence %{y:.1f}%<extra></extra>",
        },
      ],
      baseLayout({ yaxis: { title: "Rate", tickprefix: "$", gridcolor: "#e6edf1" }, yaxis2: { title: "Share %", overlaying: "y", side: "right", ticksuffix: "%", showgrid: false }, legend: { orientation: "h" } }),
      { displayModeBar: false, responsive: true }
    );
  }

  function renderWeekdayChart(targetId, rows) {
    const target = document.getElementById(targetId);
    if (!target || !window.Plotly) return;
    if (!hasRows(rows)) {
      renderEmpty(target, "No weekday pattern is available.");
      return;
    }
    Plotly.newPlot(
      target,
      [
        {
          type: "bar",
          name: "Avg Daily Cost",
          x: rows.map((row) => row.weekday_name),
          y: rows.map((row) => number(row.avg_daily_labor_cost)),
          marker: { color: "#0d766f" },
          hovertemplate: "%{x}<br>Avg cost %{y:$,.2f}<extra></extra>",
        },
        {
          type: "scatter",
          mode: "lines+markers",
          name: "Avg Daily Hours",
          x: rows.map((row) => row.weekday_name),
          y: rows.map((row) => number(row.avg_daily_paid_hours)),
          yaxis: "y2",
          line: { color: "#b67718", width: 2 },
          hovertemplate: "%{x}<br>Avg hours %{y:,.1f}<extra></extra>",
        },
      ],
      baseLayout({ yaxis: { tickprefix: "$", gridcolor: "#e6edf1" }, yaxis2: { overlaying: "y", side: "right", showgrid: false }, legend: { orientation: "h" } }),
      { displayModeBar: false, responsive: true }
    );
  }

  function renderPieChart(targetId, rows, { labelKey, valueKey, clickParam = null, format = "currency" } = {}) {
    const target = document.getElementById(targetId);
    if (!target || !window.Plotly) return;
    if (!hasRows(rows)) {
      renderEmpty(target, "No category mix is available.");
      return;
    }
    const values = rows.map((row) => number(row[valueKey]));
    if (values.every((value) => value <= 0)) {
      renderEmpty(target, "No measurable mix is available for this chart.");
      return;
    }
    Plotly.newPlot(
      target,
      [
        {
          type: "pie",
          labels: rows.map((row) => row[labelKey] || "Unknown"),
          values,
          customdata: rows.map((row) => row[labelKey] || "Unknown"),
          hole: 0.52,
          textinfo: "label+percent",
          hovertemplate: format === "currency" ? "%{label}<br>%{value:$,.2f}<br>%{percent}<extra></extra>" : "%{label}<br>%{value:,.1f}<br>%{percent}<extra></extra>",
        },
      ],
      baseLayout({ margin: { l: 20, r: 20, t: 10, b: 10 } }),
      { displayModeBar: false, responsive: true }
    );
    if (clickParam) {
      target.on("plotly_click", (event) => {
        const point = event.points && event.points[0];
        if (!point || !point.customdata) return;
        setSingleFilter(clickParam, point.customdata);
      });
    }
  }

  function renderGroupedLineChart(targetId, rows, { groupKey, dateKey, valueKey, clickParam = null, format = "currency" } = {}) {
    const target = document.getElementById(targetId);
    if (!target || !window.Plotly) return;
    if (!hasRows(rows)) {
      renderEmpty(target, "No grouped trend is available.");
      return;
    }
    const groups = {};
    rows.forEach((row) => {
      const key = row[groupKey] || "Unknown";
      groups[key] = groups[key] || [];
      groups[key].push(row);
    });
    const traces = Object.entries(groups).map(([name, points]) => ({
      type: "scatter",
      mode: "lines+markers",
      name,
      x: points.map((row) => row[dateKey]),
      y: points.map((row) => number(row[valueKey])),
      hovertemplate: format === "currency" ? `${name}<br>%{x}<br>%{y:$,.2f}<extra></extra>` : `${name}<br>%{x}<br>%{y:,.1f}<extra></extra>`,
    }));
    Plotly.newPlot(target, traces, baseLayout({ yaxis: format === "currency" ? { tickprefix: "$", gridcolor: "#e6edf1" } : { gridcolor: "#e6edf1" }, legend: { orientation: "h" } }), { displayModeBar: false, responsive: true });
    if (clickParam) {
      target.on("plotly_click", (event) => {
        const point = event.points && event.points[0];
        if (!point || !point.data || !point.data.name) return;
        setSingleFilter(clickParam, point.data.name);
      });
    }
  }

  function renderCharts() {
    const charts = (payload && payload.charts) || {};
    const focus = (payload && payload.focus) || {};
    const categoryMixMeta = charts.category_mix_meta || { value_key: "labor_cost", label: "Labor cost mix" };

    renderBarChart("laborDeptCostChart", charts.department_cost || [], { valueKey: "labor_cost", labelKey: "department_name", clickParam: "department", color: "#0d766f", format: "currency" });
    renderBarChart("laborDeptChangeChart", charts.department_change || [], { valueKey: "cost_delta_pct", labelKey: "department_name", clickParam: "department", color: "#295fa5", format: "percent", multiplier: 100 });
    renderScatterChart("laborDeptScatterChart", charts.department_scatter || []);
    renderGroupedShareChart("laborDeptRiskChart", charts.department_risk || [], "department_name", "department");
    renderBarChart("laborDeptVolatilityChart", charts.department_volatility || [], { valueKey: "cost_volatility", labelKey: "department_name", clickParam: "department", color: "#bc4935", format: "percent", multiplier: 100 });

    renderTrendChart("laborDepartmentFocusTrendChart", ((focus.department || {}).trend_rows) || []);
    renderPieChart("laborCategoryMixChart", charts.category_mix || [], { labelKey: "time_category", valueKey: categoryMixMeta.value_key || "labor_cost", clickParam: "time_category", format: categoryMixMeta.value_key === "paid_hours" ? "number" : "currency" });
    renderGroupedLineChart("laborCategoryTrendChart", charts.category_trend || [], { groupKey: "time_category", dateKey: "labor_date", valueKey: "labor_cost", clickParam: "time_category", format: "currency" });
    renderTrendChart("laborCategoryFocusTrendChart", ((focus.category || {}).trend_rows) || []);

    renderBarChart("laborWorkerCostChart", charts.worker_cost || [], { valueKey: "labor_cost", labelKey: "employee_name", clickParam: "employee", clickKey: "employee_code", color: "#0d766f", format: "currency" });
    renderBarChart("laborWorkerHoursChart", charts.worker_hours || [], { valueKey: "paid_hours", labelKey: "employee_name", clickParam: "employee", clickKey: "employee_code", color: "#b67718", format: "number" });
    renderGroupedShareChart("laborWorkerRiskChart", charts.worker_risk || [], "employee_name", "employee", "employee_code");
    renderTrendChart("laborWorkerFocusTrendChart", ((focus.worker || {}).trend_rows) || []);

    renderTrendChart("laborTrendChart", charts.daily_trend || []);
    renderRateTrendChart("laborRateTrendChart", charts.rate_trend || []);
    renderWeekdayChart("laborWeekdayChart", charts.weekday_pattern || []);
    renderGroupedLineChart("laborDeptTrendChart", charts.monthly_department_trend || [], { groupKey: "department_name", dateKey: "labor_month", valueKey: "labor_cost", clickParam: "department", format: "currency" });
  }

  bindInteractions();
  renderCharts();
})();
