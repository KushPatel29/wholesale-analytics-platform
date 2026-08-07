(() => {
  const meta = document.getElementById("RegionDrilldownV2Meta");
  if (!meta) return;
  if (meta.dataset.bound === "1") return;
  meta.dataset.bound = "1";

  const authFetch = window.authFetch || fetch;
  if (document?.body?.dataset) {
    document.body.dataset.filtersHandler = "ajax";
  }

  const bundleUrl = meta.dataset.bundleUrl || "/api/regions/drilldown/bundle";
  const exportBase = meta.dataset.exportBase || "";
  const churnExportBase = meta.dataset.churnExport || "";
  const regionId = meta.dataset.regionId || "";
  const initialPayload = (() => {
    try {
      return JSON.parse(meta.dataset.initial || "{}");
    } catch (_err) {
      return {};
    }
  })();

  const nfInt = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
  const nfPct = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
  const nfMoney0 = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 0 });
  const nfMoney2 = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });

  const state = {
    filterQs: (window.location.search || "").replace(/^\?/, ""),
    activeFetchController: null,
    lastBundleKey: "",
    currentPayload: {},
    sectionObserver: null,
    retentionSearch: "",
    requestSeq: 0,
  };
  let currentApplyId = "";

  const fmtMoney0 = (v) => (v == null || Number.isNaN(Number(v)) ? "—" : nfMoney0.format(Number(v)));
  const fmtMoney2 = (v) => (v == null || Number.isNaN(Number(v)) ? "—" : nfMoney2.format(Number(v)));
  const fmtPct = (v) => (v == null || Number.isNaN(Number(v)) ? "—" : `${nfPct.format(Number(v))}%`);
  const fmtInt = (v) => (v == null || Number.isNaN(Number(v)) ? "—" : nfInt.format(Number(v)));
  const asNum = (v, fallback = 0) => {
    const n = Number(v);
    return Number.isFinite(n) ? n : fallback;
  };
  const asArr = (v) => (Array.isArray(v) ? v : []);
  const setText = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  };
  const setHtml = (id, html) => {
    const el = document.getElementById(id);
    if (el) el.innerHTML = html;
  };
  const escapeHtml = (value) =>
    String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  const fmtSignedPoints = (value) => {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return "";
    return `${numeric > 0 ? "+" : ""}${nfPct.format(numeric)} pts`;
  };
  const normalizeStatusKey = (value) => String(value || "").trim().toLowerCase();
  const marginStatusClass = (value) => {
    const key = normalizeStatusKey(value);
    if (key === "red") return "is-red";
    if (key === "orange") return "is-orange";
    if (key === "yellow") return "is-yellow";
    if (key === "light_green") return "is-light-green";
    if (key === "green") return "is-green";
    return "is-neutral";
  };
  const marginStatusLabel = (row = {}) => row?.target_status || row?.profitability_band || "Needs review";
  const marginContextText = (row = {}) => {
    const parts = [];
    if (row.target_margin_pct != null) parts.push(`Target ${fmtPct(row.target_margin_pct)}`);
    if (row.minimum_margin_pct != null) parts.push(`Min ${fmtPct(row.minimum_margin_pct)}`);
    if (row.target_gap_pct_points != null) {
      parts.push(`${fmtSignedPoints(row.target_gap_pct_points)} vs target`);
    } else if (row.target_status) {
      parts.push(row.target_status);
    }
    return parts.join(" · ");
  };
  const marginCellHtml = (row = {}) => {
    const status = marginStatusLabel(row);
    const context = marginContextText(row);
    const marginValue = row.margin_pct_current !== null && row.margin_pct_current !== undefined ? row.margin_pct_current : row.margin_pct;
    const pill = status
      ? `<span class="region-status-pill ${marginStatusClass(row.status_key)}">${escapeHtml(status)}</span>`
      : "";
    return `
      <div class="region-metric-stack region-metric-stack-end">
        <div>${fmtPct(marginValue)}</div>
        ${context || pill ? `<div class="region-metric-sub">${pill}${context ? `${pill ? " " : ""}<span>${escapeHtml(context)}</span>` : ""}</div>` : ""}
      </div>
    `;
  };
  const normalizeQs = (qs) => String(qs || "").replace(/^\?/, "").trim();
  const truncate = (value, maxLen = 28) => {
    const text = String(value ?? "");
    return text.length > maxLen ? `${text.slice(0, maxLen - 1)}…` : text;
  };
  const displayWindowEnd = (rawEnd) => {
    if (!rawEnd) return "—";
    try {
      const endDt = new Date(`${rawEnd}T00:00:00Z`);
      endDt.setUTCDate(endDt.getUTCDate() - 1);
      return endDt.toISOString().slice(0, 10);
    } catch (_err) {
      return rawEnd;
    }
  };
  const buildEntityHref = (basePath, entityId) => {
    const id = encodeURIComponent(String(entityId || ""));
    const qs = state.filterQs ? `?${state.filterQs}` : "";
    return `${basePath}/${id}${qs}`;
  };
  const riskBadgeClass = (label) => {
    const token = String(label || "").trim().toLowerCase();
    if (["lost", "churned", "high"].includes(token)) return "region-risk-badge is-high";
    if (["at risk", "warming", "medium"].includes(token)) return "region-risk-badge is-medium";
    return "region-risk-badge is-low";
  };
  const trendClass = (value) => {
    const n = Number(value);
    if (!Number.isFinite(n)) return "";
    if (n > 0) return "text-success";
    if (n < 0) return "text-danger";
    return "";
  };

  const setEmpty = (chartId, emptyId, isEmpty) => {
    const chartEl = document.getElementById(chartId);
    const emptyEl = document.getElementById(emptyId);
    if (emptyEl) emptyEl.classList.toggle("d-none", !isEmpty);
    if (chartEl && isEmpty) chartEl.innerHTML = "";
  };

  const initTooltips = () => {
    if (!window.bootstrap || !window.bootstrap.Tooltip) return;
    document.querySelectorAll(".region-drilldown-v2 [data-bs-toggle='tooltip']").forEach((el) => {
      try {
        window.bootstrap.Tooltip.getOrCreateInstance(el);
      } catch (_err) {
        try {
          new window.bootstrap.Tooltip(el);
        } catch (_e2) {
          // no-op
        }
      }
    });
  };

  const bindSectionNav = () => {
    const links = Array.from(document.querySelectorAll(".region-v2-subnav-link"));
    if (!links.length) return;
    const sections = links.map((link) => document.querySelector(link.getAttribute("href") || "")).filter(Boolean);

    const setActive = (sectionId) => {
      links.forEach((link) => {
        const active = (link.getAttribute("href") || "") === `#${sectionId}`;
        link.classList.toggle("active", active);
      });
    };

    links.forEach((link) => {
      link.addEventListener("click", (evt) => {
        const targetSel = link.getAttribute("href");
        if (!targetSel || !targetSel.startsWith("#")) return;
        const target = document.querySelector(targetSel);
        if (!target) return;
        evt.preventDefault();
        target.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    });

    if (state.sectionObserver && typeof state.sectionObserver.disconnect === "function") {
      state.sectionObserver.disconnect();
      state.sectionObserver = null;
    }

    if (typeof IntersectionObserver !== "undefined") {
      state.sectionObserver = new IntersectionObserver(
        (entries) => {
          let candidate = null;
          entries.forEach((entry) => {
            if (!entry.isIntersecting) return;
            if (!candidate || entry.intersectionRatio > candidate.intersectionRatio) candidate = entry;
          });
          if (candidate?.target?.id) setActive(candidate.target.id);
        },
        { threshold: [0.2, 0.35, 0.55], rootMargin: "-25% 0px -55% 0px" }
      );
      sections.forEach((section) => state.sectionObserver.observe(section));
    }

    if (sections[0]?.id) setActive(sections[0].id);
  };

  const exportUrl = (dataset, format) => {
    const params = new URLSearchParams(state.filterQs || "");
    params.set("dataset", dataset);
    params.set("format", format || "xlsx");
    params.set("region_id", regionId);
    params.set("drilldown_v2", "1");
    params.set("region_drilldown_v2", "1");
    return `${exportBase}?${params.toString()}`;
  };

  const bindExportLinks = () => {
    document.querySelectorAll(".js-region-export").forEach((el) => {
      const dataset = el.getAttribute("data-dataset") || "summary";
      const format = el.getAttribute("data-format") || "xlsx";
      el.setAttribute("href", exportUrl(dataset, format));
    });
    const churnLink = document.getElementById("legacyChurnExport");
    if (churnLink && churnExportBase) {
      const params = new URLSearchParams(state.filterQs || "");
      churnLink.setAttribute("href", params.toString() ? `${churnExportBase}?${params.toString()}` : churnExportBase);
    }
  };

  const buildBundleKey = () => {
    const params = new URLSearchParams(state.filterQs || "");
    params.set("region_id", regionId);
    params.set("top_n", "50");
    params.set("drilldown_v2", "1");
    params.set("region_drilldown_v2", "1");
    return params.toString();
  };

  const renderHeader = (payload) => {
    const v2 = (payload && payload.region_v2) || {};
    const score = (v2 && v2.scorecard) || {};
    const windowMeta = (v2 && v2.window) || {};
    const metaPayload = (payload && payload.meta) || {};
    const freshness = metaPayload.freshness || {};

    setText("v2RegionTitle", score.region_name || regionId || "Region");
    if (windowMeta.start && windowMeta.end) {
      setText("v2WindowSummary", `Active window ${windowMeta.start} to ${displayWindowEnd(windowMeta.end)}.`);
    } else {
      setText("v2WindowSummary", "Computed using current filters and scope.");
    }
    if (windowMeta.prior_start && windowMeta.prior_end) {
      setText(
        "v2ComparisonSummary",
        `Comparison window ${windowMeta.prior_start} to ${displayWindowEnd(windowMeta.prior_end)}.`
      );
    } else {
      setText("v2ComparisonSummary", "Prior period unavailable for this filtered region.");
    }

    const dataCoverage = score.data_quality_flag || "Unknown";
    const costCoverage = score.cost_coverage_pct;
    const packsCoverage = metaPayload?.packs_coverage?.packs_coverage_pct ?? score.packs_coverage_pct;
    setText("badgeDataCoverage", `Data coverage: ${dataCoverage}`);
    setText("badgeCostCoverage", `Cost coverage: ${fmtPct(costCoverage)}`);
    setText("badgePacksCoverage", `Packs coverage: ${fmtPct(packsCoverage)}`);
    setText("badgeFreshness", `Freshness: ${freshness.label || "Unavailable"}`);

    const healthWarn = document.getElementById("badgeHealthWarn");
    if (healthWarn) healthWarn.classList.toggle("d-none", !score.customer_health_warning);
  };

  const renderKpis = (payload) => {
    const score = ((payload && payload.region_v2) || {}).scorecard || {};

    setText("kpiRevenue", fmtMoney0(score.total_revenue));
    setText("kpiRevenueMeta", score.prior_revenue > 0 ? `${fmtMoney0(score.prior_revenue)} prior window revenue` : "Prior period unavailable");
    setText("kpiProfit", fmtMoney0(score.total_profit));
    setText("kpiProfitMeta", score.profit_per_order == null ? "Profit per order unavailable" : `${fmtMoney2(score.profit_per_order)} profit per order`);
    setText("kpiMargin", fmtPct(score.margin_pct));
    setText(
      "kpiMarginMeta",
      marginContextText(score) || (score.revenue_per_unit == null ? "Revenue per unit unavailable" : `${fmtMoney2(score.revenue_per_unit)} revenue per unit`)
    );
    setText("kpiOrders", fmtInt(score.orders));
    setText("kpiOrdersMeta", score.revenue_per_lb == null ? "Revenue per lb unavailable" : `${fmtMoney2(score.revenue_per_lb)} revenue per lb`);
    setText("kpiCustomers", fmtInt(score.customers));
    setText("kpiCustomersMeta", `${fmtInt(score.active_customers_90d)} active in last 90d`);
    setText("kpiAov", fmtMoney0(score.avg_order_value));
    setText("kpiAovMeta", score.asp == null ? "ASP unavailable" : `${fmtMoney2(score.asp)} ASP`);
    setText("kpiRevenuePerCustomer", fmtMoney0(score.revenue_per_customer));
    setText("kpiRevenuePerCustomerMeta", `${fmtMoney0(score.total_revenue)} across ${fmtInt(score.customers)} customers`);
    setText("kpiRepeat", fmtPct(score.repeat_pct));
    setText("kpiRepeatMeta", `${fmtInt(score.returning_customers)} returning customers`);
    setText("kpiChurn", fmtPct(score.churn_pct));
    setText("kpiChurnMeta", `${fmtInt(score.at_risk_customers)} customers currently at risk`);
    setText("kpiNewCustomerShare", fmtPct(score.new_customer_share_pct));
    setText("kpiNewCustomerShareMeta", `${fmtInt(score.new_customers)} new customers in scope`);
    setText("kpiMomGrowth", fmtPct(score.mom_growth));
    setText("kpiMomGrowthMeta", score.mom_growth == null ? "Insufficient prior-month history" : "Current month vs previous month");
    setText("kpiYoyGrowth", fmtPct(score.yoy_growth));
    setText("kpiYoyGrowthMeta", score.yoy_growth == null ? "Insufficient prior-year history" : "Equivalent prior-year window");
    setText("kpiDeltaRevenue", fmtMoney0(score.revenue_delta_window));
    setText("kpiDeltaRevenueMeta", score.revenue_delta_window_pct == null ? "Prior period unavailable" : `${fmtPct(score.revenue_delta_window_pct)} vs prior window`);
    setText("kpiTopCustomerShare", fmtPct(score.top_customer_share_pct));
    setText("kpiTopCustomerShareTop5", fmtPct(score.top_customer_top5_share_pct));
    setText("kpiTopProductShare", fmtPct(score.top_product_share_pct));
    setText("kpiTopProductShareTop5", fmtPct(score.top_product_top5_share_pct));
    setText("kpiQualityFlag", score.data_quality_flag || "Unknown");
    setText(
      "kpiQualityMeta",
      `Cost ${fmtPct(score.cost_coverage_pct)} • Packs ${fmtPct(score.packs_coverage_pct)} • Missing cost ${fmtMoney0(score.missing_cost_revenue)}`
    );

    [
      ["kpiMomGrowth", score.mom_growth],
      ["kpiYoyGrowth", score.yoy_growth],
      ["kpiDeltaRevenue", score.revenue_delta_window],
    ].forEach(([id, value]) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.classList.remove("text-success", "text-danger");
      const cls = trendClass(value);
      if (cls) el.classList.add(cls);
    });
  };

  const renderTrend = (payload) => {
    const trend = ((payload && payload.region_v2) || {}).trend || {};
    const metric = document.getElementById("trendMetricSelect")?.value || "revenue";
    const labels = asArr(trend.labels);
    const values = asArr(trend[metric]);
    const priorLabels = asArr(trend.prior_labels);
    const priorValues = asArr(trend[`prior_${metric}`]);
    const margin = asArr(trend.margin_pct);
    const hasData = labels.length > 0 && values.some((v) => asNum(v) !== 0);
    setEmpty("regionV2TrendChart", "regionV2TrendEmpty", !hasData);
    if (!hasData || typeof window.Plotly === "undefined") return;

    const traces = [
      {
        x: labels,
        y: values,
        type: "scatter",
        mode: "lines+markers",
        name: "Current",
        line: { color: "#0f766e", width: 3 },
      },
    ];
    if (priorLabels.length) {
      traces.push({
        x: priorLabels,
        y: priorValues,
        type: "scatter",
        mode: "lines+markers",
        name: "Prior",
        line: { color: "#94a3b8", width: 2, dash: "dot" },
      });
    }
    if (["revenue", "profit"].includes(metric) && margin.length) {
      traces.push({
        x: labels,
        y: margin,
        type: "scatter",
        mode: "lines",
        name: "Margin %",
        yaxis: "y2",
        line: { color: "#1d4ed8", width: 2 },
        hovertemplate: "%{x}<br>Margin %{y:.1f}%<extra></extra>",
      });
    }

    const yaxis = ["revenue", "profit"].includes(metric)
      ? { tickformat: "$,.0f", title: metric === "profit" ? "Profit" : "Revenue" }
      : { tickformat: ",.0f", title: metric === "orders" ? "Orders" : "Customers" };
    window.Plotly.newPlot(
      "regionV2TrendChart",
      traces,
      {
        margin: { t: 10, r: 50, b: 40, l: 60 },
        height: 360,
        hovermode: "x unified",
        xaxis: { automargin: true },
        yaxis,
        yaxis2: { overlaying: "y", side: "right", tickformat: ",.1f", title: "Margin %" },
        legend: { orientation: "h", y: 1.12 },
      },
      { displayModeBar: false, responsive: true }
    );
  };

  const renderCustomers = (payload) => {
    const customers = ((payload && payload.region_v2) || {}).customers || {};
    const summary = customers.summary || {};
    const concentration = customers.concentration || {};
    const topRows = asArr(customers.top_rows);
    const movers = asArr(customers.movers).slice(0, 12);
    const healthRows = asArr((((payload || {}).region_v2 || {}).retention || {}).rows).slice(0, 12);
    const topN = Number.parseInt(document.getElementById("customersTopN")?.value || "25", 10) || 25;
    const rows = topRows.slice(0, topN);
    const hasData = rows.length > 0 && rows.some((row) => asNum(row.revenue_current || row.revenue) > 0);

    setHtml(
      "customerConcentrationSummary",
      [
        `Top 1 share: ${fmtPct(concentration.top1_share_pct)}`,
        `Top 5 share: ${fmtPct(concentration.top5_share_pct)}`,
        `HHI: ${fmtInt(concentration.hhi)}`,
        `Customers in scope: ${fmtInt(concentration.count)}`,
      ].map((line) => `<li>${escapeHtml(line)}</li>`).join("")
    );

    const pills = [
      `<span class="region-pill is-good">New customers ${fmtInt(summary.new_customers)}</span>`,
      `<span class="region-pill">Returning customers ${fmtInt(summary.returning_customers)}</span>`,
      `<span class="region-pill is-warning">Lost customers ${fmtInt(summary.lost_customers)}</span>`,
      `<span class="region-pill">New customer revenue ${fmtPct(summary.new_customer_share_pct)}</span>`,
      `<span class="region-pill">Returning customer revenue ${fmtPct(summary.returning_customer_share_pct)}</span>`,
    ];
    setHtml("customerMixPills", pills.join(""));

    setHtml(
      "topCustomersTable",
      rows.length
        ? rows
            .map((row) => {
              const href = buildEntityHref("/customers", row.customer_id);
              return `
                <tr>
                  <td><a href="${escapeHtml(href)}">${escapeHtml(row.customer_name || row.customer_id)}</a></td>
                  <td class="text-end">${fmtMoney0(row.revenue_current || row.revenue)}</td>
                  <td class="text-end">${fmtPct(row.revenue_share_pct)}</td>
                  <td>${escapeHtml(row.last_order || "—")}</td>
                  <td class="text-end">${fmtInt(row.orders_current || row.orders)}</td>
                </tr>`;
            })
            .join("")
        : `<tr><td colspan="5" class="text-center text-muted">No customer detail available.</td></tr>`
    );

    setHtml(
      "customerMoversTable",
      movers.length
        ? movers
            .map((row) => {
              const href = buildEntityHref("/customers", row.customer_id);
              const labelClass = trendClass(row.delta_revenue);
              return `
                <tr>
                  <td><a href="${escapeHtml(href)}">${escapeHtml(row.customer_name || row.customer_id)}</a></td>
                  <td class="text-end">${fmtMoney0(row.revenue_current || row.revenue)}</td>
                  <td class="text-end">${fmtMoney0(row.revenue_prior)}</td>
                  <td class="text-end ${labelClass}">${fmtMoney0(row.delta_revenue)}</td>
                  <td><span class="${escapeHtml(riskBadgeClass(row.delta_revenue_status))}">${escapeHtml(row.delta_revenue_label || row.delta_revenue_status || "—")}</span></td>
                </tr>`;
            })
            .join("")
        : `<tr><td colspan="5" class="text-center text-muted">No customer mover data available.</td></tr>`
    );

    setHtml(
      "customerHealthTable",
      healthRows.length
        ? healthRows
            .map((row) => {
              const href = buildEntityHref("/customers", row.customer_id);
              return `
                <tr>
                  <td><a href="${escapeHtml(href)}">${escapeHtml(row.customer_name || row.customer_id)}</a></td>
                  <td class="text-end">${fmtMoney0(row.revenue_current || row.revenue)}</td>
                  <td class="text-end">${fmtMoney0(row.revenue_prior || row.prior_revenue_window)}</td>
                  <td><span class="${escapeHtml(riskBadgeClass(row.risk_level))}">${escapeHtml(row.risk_level || "—")}</span></td>
                  <td class="text-end">${fmtInt(row.days_since_last)}</td>
                </tr>`;
            })
            .join("")
        : `<tr><td colspan="5" class="text-center text-muted">No customer health detail available.</td></tr>`
    );

    setEmpty("regionV2CustomersChart", "regionV2CustomersEmpty", !hasData);
    if (!hasData || typeof window.Plotly === "undefined") return;
    window.Plotly.newPlot(
      "regionV2CustomersChart",
      [
        {
          x: rows.map((row) => asNum(row.revenue_current || row.revenue)).slice().reverse(),
          y: rows.map((row) => truncate(row.customer_name || row.customer_id, 28)).slice().reverse(),
          text: rows.map((row) => row.customer_name || row.customer_id).slice().reverse(),
          type: "bar",
          orientation: "h",
          marker: { color: "#0f766e" },
          customdata: rows.map((row) => [row.revenue_share_pct, row.last_order, row.orders_current || row.orders]).slice().reverse(),
          hovertemplate:
            "<b>%{text}</b><br>Revenue %{x:$,.0f}<br>Share %{customdata[0]:.1f}%<br>Last order %{customdata[1]}<br>Orders %{customdata[2]}<extra></extra>",
        },
      ],
      {
        margin: { t: 10, r: 20, b: 30, l: 10 },
        height: 360,
        xaxis: { tickformat: "$,.0f", title: "Revenue" },
        yaxis: { automargin: true },
      },
      { displayModeBar: false, responsive: true }
    );
  };

  const renderProducts = (payload) => {
    const products = ((payload && payload.region_v2) || {}).products || {};
    const concentration = products.concentration || {};
    const topRows = asArr(products.top_rows);
    const movers = asArr(products.movers).slice(0, 12);
    const marginRiskRows = asArr(products.margin_risk_rows).slice(0, 12);
    const metric = document.getElementById("productsMetricSelect")?.value || "revenue";
    const metricMap = {
      revenue: "revenue_current",
      profit: "profit_current",
      orders: "orders_current",
      qty: "qty_current",
    };
    const metricKey = metricMap[metric] || "revenue_current";
    const rows = topRows.slice(0, 25);
    const hasData = rows.length > 0 && rows.some((row) => asNum(row[metricKey] ?? row.revenue_current) > 0);

    setHtml(
      "productConcentrationSummary",
      [
        `Top 1 share: ${fmtPct(concentration.top1_share_pct)}`,
        `Top 5 share: ${fmtPct(concentration.top5_share_pct)}`,
        `Top 10 share: ${fmtPct(concentration.top10_share_pct)}`,
        `HHI: ${fmtInt(concentration.hhi)}`,
      ].map((line) => `<li>${escapeHtml(line)}</li>`).join("")
    );

    setHtml(
      "topProductsTable",
      rows.length
        ? rows
            .map((row) => {
              const href = buildEntityHref("/products", row.product_id);
              return `
                <tr>
                  <td><a href="${escapeHtml(href)}">${escapeHtml(row.product_name || row.product_id)}</a></td>
                  <td class="text-end">${fmtMoney0(row.revenue_current || row.revenue)}</td>
                  <td class="text-end">${marginCellHtml(row)}</td>
                  <td class="text-end">${fmtPct(row.revenue_share_pct)}</td>
                  <td><span class="${escapeHtml(riskBadgeClass(row.risk_tag))}">${escapeHtml(row.risk_tag || "—")}</span></td>
                </tr>`;
            })
            .join("")
        : `<tr><td colspan="5" class="text-center text-muted">No product detail available.</td></tr>`
    );

    setHtml(
      "productMoversTable",
      movers.length
        ? movers
            .map((row) => {
              const href = buildEntityHref("/products", row.product_id);
              const labelClass = trendClass(row.delta_revenue);
              return `
                <tr>
                  <td><a href="${escapeHtml(href)}">${escapeHtml(row.product_name || row.product_id)}</a></td>
                  <td class="text-end">${fmtMoney0(row.revenue_current || row.revenue)}</td>
                  <td class="text-end">${fmtMoney0(row.revenue_prior)}</td>
                  <td class="text-end ${labelClass}">${fmtMoney0(row.delta_revenue)}</td>
                  <td><span class="${escapeHtml(riskBadgeClass(row.delta_revenue_status))}">${escapeHtml(row.delta_revenue_label || row.delta_revenue_status || "—")}</span></td>
                </tr>`;
            })
            .join("")
        : `<tr><td colspan="5" class="text-center text-muted">No product mover data available.</td></tr>`
    );

    setHtml(
      "marginRiskTable",
      marginRiskRows.length
        ? marginRiskRows
            .map((row) => {
              const href = buildEntityHref("/products", row.product_id);
              return `
                <tr>
                  <td><a href="${escapeHtml(href)}">${escapeHtml(row.product_name || row.product_id)}</a></td>
                  <td class="text-end">${fmtMoney0(row.revenue_current || row.revenue)}</td>
                  <td class="text-end">${marginCellHtml(row)}</td>
                  <td><span class="${escapeHtml(riskBadgeClass(row.risk_tag))}">${escapeHtml(row.risk_tag || "—")}</span></td>
                </tr>`;
            })
            .join("")
        : `<tr><td colspan="4" class="text-center text-muted">No margin-risk products in the current window.</td></tr>`
    );

    setEmpty("regionV2ProductsChart", "regionV2ProductsEmpty", !hasData);
    if (!hasData || typeof window.Plotly === "undefined") return;
    const label = metric === "profit" ? "Profit" : metric === "orders" ? "Orders" : metric === "qty" ? "Units" : "Revenue";
    window.Plotly.newPlot(
      "regionV2ProductsChart",
      [
        {
          x: rows.map((row) => asNum(row[metricKey] ?? row.revenue_current)).slice().reverse(),
          y: rows.map((row) => truncate(row.product_name || row.product_id, 28)).slice().reverse(),
          text: rows.map((row) => row.product_name || row.product_id).slice().reverse(),
          type: "bar",
          orientation: "h",
          marker: { color: "#1d4ed8" },
          hovertemplate:
            metric === "revenue" || metric === "profit"
              ? `<b>%{text}</b><br>${label} %{x:$,.0f}<extra></extra>`
              : `<b>%{text}</b><br>${label} %{x:,.0f}<extra></extra>`,
        },
      ],
      {
        margin: { t: 10, r: 20, b: 30, l: 10 },
        height: 360,
        xaxis: { tickformat: metric === "revenue" || metric === "profit" ? "$,.0f" : ",.0f", title: label },
        yaxis: { automargin: true },
      },
      { displayModeBar: false, responsive: true }
    );
  };

  const renderRetention = (payload) => {
    const retention = ((payload && payload.region_v2) || {}).retention || {};
    const buckets = asArr(retention.buckets);
    let rows = asArr(retention.rows);
    const search = state.retentionSearch.trim().toLowerCase();
    if (search) {
      rows = rows.filter((row) => String(row.customer_name || row.customer_id || "").toLowerCase().includes(search));
    }

    setHtml(
      "retentionBuckets",
      buckets.length
        ? buckets
            .map(
              (bucket) => `
                <div class="d-flex justify-content-between align-items-center border rounded p-2">
                  <div>
                    <div class="fw-semibold">${escapeHtml(bucket.label || "—")}</div>
                    <div class="small text-muted">${fmtMoney0(bucket.revenue)}</div>
                  </div>
                  <div class="fs-5 fw-semibold">${fmtInt(bucket.count)}</div>
                </div>`
            )
            .join("")
        : `<div class="text-muted small">No retention buckets are available for this region.</div>`
    );

    setHtml(
      "retentionTable",
      rows.length
        ? rows
            .map((row) => {
              const href = buildEntityHref("/customers", row.customer_id);
              return `
                <tr>
                  <td><a href="${escapeHtml(href)}">${escapeHtml(row.customer_name || row.customer_id)}</a></td>
                  <td class="text-end">${fmtMoney0(row.revenue_current || row.revenue)}</td>
                  <td>${escapeHtml(row.last_order || "—")}</td>
                  <td class="text-end">${fmtInt(row.days_since_last)}</td>
                  <td class="text-end">${fmtMoney0(row.revenue_prior || row.prior_revenue_window)}</td>
                  <td><span class="${escapeHtml(riskBadgeClass(row.risk_level))}">${escapeHtml(row.risk_level || "—")}</span></td>
                  <td class="text-end">${fmtPct(row.region_revenue_share_lost_pct)}</td>
                </tr>`;
            })
            .join("")
        : `<tr><td colspan="7" class="text-center text-muted">No churn or at-risk customers in the selected window.</td></tr>`
    );
  };

  const renderOperations = (payload) => {
    const operations = ((payload && payload.region_v2) || {}).operations || {};
    const shippingMix = asArr(operations.shipping_mix);
    const weekdayRows = asArr(operations.weekday);
    const supplierMix = asArr(operations.supplier_mix);

    const shippingHasData = shippingMix.length > 0 && shippingMix.some((row) => asNum(row.revenue) > 0);
    setEmpty("regionV2ShippingChart", "regionV2ShippingEmpty", !shippingHasData);
    if (shippingHasData && typeof window.Plotly !== "undefined") {
      window.Plotly.newPlot(
        "regionV2ShippingChart",
        [
          {
            x: shippingMix.map((row) => asNum(row.revenue)).slice().reverse(),
            y: shippingMix.map((row) => truncate(row.method, 26)).slice().reverse(),
            text: shippingMix.map((row) => row.method).slice().reverse(),
            type: "bar",
            orientation: "h",
            marker: { color: "#0f766e" },
            customdata: shippingMix.map((row) => [row.pct, row.orders, row.aov]).slice().reverse(),
            hovertemplate:
              "<b>%{text}</b><br>Revenue %{x:$,.0f}<br>Share %{customdata[0]:.1f}%<br>Orders %{customdata[1]}<br>AOV %{customdata[2]:$,.0f}<extra></extra>",
          },
        ],
        {
          margin: { t: 10, r: 20, b: 30, l: 10 },
          height: 320,
          xaxis: { tickformat: "$,.0f", title: "Revenue" },
          yaxis: { automargin: true },
        },
        { displayModeBar: false, responsive: true }
      );
    }

    const weekdayMetric = document.getElementById("weekdayMetricSelect")?.value || "revenue";
    const weekdayHasData = weekdayRows.length > 0 && weekdayRows.some((row) => asNum(row[weekdayMetric]) > 0);
    setEmpty("regionV2WeekdayChart", "regionV2WeekdayEmpty", !weekdayHasData);
    setHtml(
      "weekdaySummary",
      [
        operations.best_weekday ? `<span class="region-pill is-good">Best day ${escapeHtml(operations.best_weekday.label)} ${fmtMoney0(operations.best_weekday.revenue)}</span>` : "",
        operations.weakest_weekday ? `<span class="region-pill is-warning">Weakest day ${escapeHtml(operations.weakest_weekday.label)} ${fmtMoney0(operations.weakest_weekday.revenue)}</span>` : "",
      ].filter(Boolean).join("")
    );
    if (weekdayHasData && typeof window.Plotly !== "undefined") {
      const tickFormat = weekdayMetric === "revenue" || weekdayMetric === "aov" ? "$,.0f" : ",.0f";
      window.Plotly.newPlot(
        "regionV2WeekdayChart",
        [
          {
            x: weekdayRows.map((row) => row.label),
            y: weekdayRows.map((row) => asNum(row[weekdayMetric])),
            type: "bar",
            marker: { color: "#1d4ed8" },
            customdata: weekdayRows.map((row) => [row.revenue, row.orders, row.aov]),
            hovertemplate:
              "<b>%{x}</b><br>Revenue %{customdata[0]:$,.0f}<br>Orders %{customdata[1]}<br>AOV %{customdata[2]:$,.0f}<extra></extra>",
          },
        ],
        {
          margin: { t: 10, r: 20, b: 40, l: 50 },
          height: 320,
          xaxis: { automargin: true },
          yaxis: { tickformat: tickFormat, title: weekdayMetric === "revenue" ? "Revenue" : weekdayMetric === "aov" ? "AOV" : "Orders" },
        },
        { displayModeBar: false, responsive: true }
      );
    }

    setHtml(
      "supplierMixTable",
      supplierMix.length
        ? supplierMix
            .map(
              (row) => `
                <tr>
                  <td>${escapeHtml(row.supplier_name || row.supplier_id || "—")}</td>
                  <td class="text-end">${fmtMoney0(row.revenue)}</td>
                  <td class="text-end">${fmtMoney0(row.profit)}</td>
                  <td class="text-end">${fmtPct(row.pct)}</td>
                  <td class="text-end">${fmtInt(row.orders)}</td>
                </tr>`
            )
            .join("")
        : `<tr><td colspan="5" class="text-center text-muted">Supplier mix is unavailable for this region.</td></tr>`
    );
  };

  const renderInsights = (payload) => {
    const insights = asArr((((payload || {}).region_v2 || {}).insights));
    const cards = insights.length
      ? insights
          .slice(0, 3)
          .map(
            (item) => `
              <div class="col-lg-4">
                <div class="card insight-card h-100 ${item.tone === "positive" ? "is-positive" : item.tone === "warning" ? "is-warning" : item.tone === "action" ? "is-action" : ""}">
                  <div class="card-body">
                    <div class="text-muted small">${escapeHtml(item.title || "Insight")}</div>
                    <div class="fw-semibold">${escapeHtml(item.text || "—")}</div>
                  </div>
                </div>
              </div>`
          )
          .join("")
      : `
          <div class="col-lg-4"><div class="card h-100"><div class="card-body"><div class="text-muted small">What changed</div><div class="fw-semibold">No insight yet.</div></div></div></div>
          <div class="col-lg-4"><div class="card h-100"><div class="card-body"><div class="text-muted small">What is risky</div><div class="fw-semibold">No insight yet.</div></div></div></div>
          <div class="col-lg-4"><div class="card h-100"><div class="card-body"><div class="text-muted small">What to do next</div><div class="fw-semibold">No insight yet.</div></div></div></div>`;
    setHtml("insightCards", cards);
  };

  const render = (payload) => {
    state.currentPayload = payload || {};
    renderHeader(payload || {});
    renderKpis(payload || {});
    renderTrend(payload || {});
    renderCustomers(payload || {});
    renderProducts(payload || {});
    renderRetention(payload || {});
    renderOperations(payload || {});
    renderInsights(payload || {});
    bindExportLinks();
    initTooltips();
  };

  const consumeApplyId = () => {
    const applyId = currentApplyId;
    currentApplyId = "";
    return applyId;
  };

  const dispatchGlobalApplyAck = (detail = {}) => {
    const payload = { ...detail };
    const applyId = consumeApplyId();
    if (applyId) payload.applyId = applyId;
    if (typeof window.dispatchGlobalFiltersApplied === "function") {
      window.dispatchGlobalFiltersApplied(payload);
      return;
    }
    window.dispatchEvent(new CustomEvent("globalFilters:applied", { detail: payload }));
  };

  const fetchBundle = async () => {
    const requestSeq = ++state.requestSeq;
    const bundleKey = buildBundleKey();
    if (!bundleKey) return;
    if (bundleKey === state.lastBundleKey && Object.keys(state.currentPayload || {}).length > 0) return;
    state.lastBundleKey = bundleKey;

    if (state.activeFetchController && typeof state.activeFetchController.abort === "function") {
      state.activeFetchController.abort();
    }
    const controller = typeof AbortController !== "undefined" ? new AbortController() : null;
    state.activeFetchController = controller;
    try {
      const requestOptions = { headers: { Accept: "application/json" } };
      if (controller) requestOptions.signal = controller.signal;
      const res = await authFetch(`${bundleUrl}?${bundleKey}`, requestOptions);
      if (!res.ok) return;
      const payload = await res.json();
      render(payload || {});
    } catch (err) {
      if (err && err.name === "AbortError") return;
      // keep current UI
    } finally {
      if (requestSeq !== state.requestSeq) return;
      if (state.activeFetchController === controller) state.activeFetchController = null;
      dispatchGlobalApplyAck({ qs: state.filterQs });
    }
  };

  const applyFilters = (qs) => {
    const normalized = normalizeQs(qs);
    if (normalized === state.filterQs) {
      dispatchGlobalApplyAck({ qs: state.filterQs });
      return;
    }
    state.filterQs = normalized;
    bindExportLinks();
    state.lastBundleKey = "";
    fetchBundle();
  };

  const bindControls = () => {
    document.getElementById("trendMetricSelect")?.addEventListener("change", () => renderTrend(state.currentPayload || {}));
    document.getElementById("customersTopN")?.addEventListener("change", () => renderCustomers(state.currentPayload || {}));
    document.getElementById("productsMetricSelect")?.addEventListener("change", () => renderProducts(state.currentPayload || {}));
    document.getElementById("weekdayMetricSelect")?.addEventListener("change", () => renderOperations(state.currentPayload || {}));
    document.getElementById("retentionSearch")?.addEventListener("input", (evt) => {
      state.retentionSearch = evt.target?.value || "";
      renderRetention(state.currentPayload || {});
    });
  };

  const onGlobalFiltersApply = (evt) => {
    currentApplyId = String(evt?.detail?.applyId || "");
    const qs = (evt?.detail && evt.detail.qs) || "";
    applyFilters(qs);
  };
  window.addEventListener("globalFilters:apply", onGlobalFiltersApply);

  const teardown = () => {
    if (state.sectionObserver && typeof state.sectionObserver.disconnect === "function") {
      state.sectionObserver.disconnect();
      state.sectionObserver = null;
    }
    window.removeEventListener("globalFilters:apply", onGlobalFiltersApply);
  };
  window.addEventListener("pagehide", teardown, { once: true });

  bindControls();
  bindSectionNav();
  state.filterQs = normalizeQs(state.filterQs);
  render(initialPayload || {});
  bindExportLinks();
  fetchBundle();
})();
