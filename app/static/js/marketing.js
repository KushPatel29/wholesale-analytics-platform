/*
 * Marketing page renderer.
 *
 * Shares finance.css, and the same two hard-won rules as finance.js:
 * formatting goes through window.WAFormat, and charts wait for
 * ChartUtils.whenPlotlyReady because base.html loads plotly-basic lazily.
 * Only bar/pie/scatter exist in that build - no funnel trace, no waterfall.
 */
(() => {
  "use strict";

  const root = document.getElementById("MarketingApp");
  if (!root) return;

  const F = window.WAFormat || {};
  const MISSING = F.MISSING || "—";

  const num = (value) => (Number.isFinite(Number(value)) ? Number(value) : null);
  const money = (value) => (num(value) == null ? MISSING : F.currency(value, { forceDecimals: false }));
  const pct = (value) => (num(value) == null ? MISSING : F.percent(value, { decimals: 1 }));
  const mult = (value) => (num(value) == null ? MISSING : `${F.decimal(value, { decimals: 2 })}×`);
  const count = (value) => (num(value) == null ? MISSING : F.count(value));
  const months = (value) => (num(value) == null ? MISSING : `${F.decimal(value, { decimals: 1 })} mo`);
  const escapeHtml = (value) =>
    String(value ?? "").replace(/[&<>'"]/g, (char) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));

  const text = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  };

  const formatByUnit = (value, unit) => {
    if (num(value) == null) return MISSING;
    if (unit === "currency") return money(value);
    if (unit === "percent") return pct(value);
    if (unit === "multiple") return mult(value);
    if (unit === "months") return months(value);
    return String(value);
  };

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

  const baseLayout = (theme, extra) =>
    Object.assign(
      {
        paper_bgcolor: "rgba(0,0,0,0)",
        plot_bgcolor: "rgba(0,0,0,0)",
        font: { color: theme.text, size: 11 },
        margin: { l: 62, r: 18, t: 12, b: 46 },
        showlegend: false,
      },
      extra || {}
    );

  const plot = (id, traces, layout) => {
    const host = document.getElementById(id);
    if (!host || !window.Plotly) return;
    window.Plotly.newPlot(host, traces, layout, { displayModeBar: false, responsive: true });
  };

  const renderKpis = (kpis) => {
    const host = document.getElementById("marketingKpis");
    if (!host) return;
    host.innerHTML = (kpis || [])
      .map(
        (kpi) => `
      <article class="finance-ratio">
        <span class="finance-ratio__label">${escapeHtml(kpi.label)}</span>
        <span class="finance-ratio__value">${escapeHtml(formatByUnit(kpi.value, kpi.unit))}</span>
        <span class="finance-ratio__formula">${escapeHtml(kpi.formula)}</span>
        <span class="finance-ratio__basis">${escapeHtml(kpi.basis)}</span>
        ${kpi.note ? `<span class="finance-ratio__note">${escapeHtml(kpi.note)}</span>` : ""}
      </article>`
      )
      .join("");
  };

  const renderChannels = (rows) => {
    const body = document.getElementById("marketingChannels");
    if (!body) return;
    body.innerHTML = (rows || [])
      .map(
        (row) => `<tr>
        <td>${escapeHtml(row.channel)}</td>
        <td class="num">${escapeHtml(money(row.spend))}</td>
        <td class="num">${escapeHtml(count(row.leads))}</td>
        <td class="num">${escapeHtml(count(row.qualified_leads))}</td>
        <td class="num">${escapeHtml(count(row.new_customers))}</td>
        <td class="num">${escapeHtml(money(row.cpl))}</td>
        <td class="num">${escapeHtml(money(row.cost_per_acquisition))}</td>
      </tr>`
      )
      .join("");
  };

  const renderMonthly = (rows) => {
    const body = document.getElementById("marketingMonthly");
    if (!body) return;
    body.innerHTML = (rows || [])
      .map(
        (row) => `<tr>
        <td>${escapeHtml(row.label)}</td>
        <td class="num">${escapeHtml(money(row.spend))}</td>
        <td class="num">${escapeHtml(money(row.acquisition_selling_spend))}</td>
        <td class="num">${escapeHtml(money(row.total_acquisition_spend))}</td>
        <td class="num">${escapeHtml(count(row.leads))}</td>
        <td class="num">${escapeHtml(count(row.qualified_leads))}</td>
        <td class="num">${escapeHtml(count(row.new_customers))}</td>
        <td class="num">${escapeHtml(money(row.cac))}</td>
      </tr>`
      )
      .join("");
  };

  const drawSpend = (charts, theme) => {
    const data = charts.spend_vs_acquisitions || {};
    if (!(data.labels || []).length) return;
    plot(
      "marketingSpendChart",
      [
        {
          type: "bar",
          name: "Acquisition spend",
          x: data.labels,
          y: (data.spend || []).map(num),
          marker: { color: theme.info, opacity: 0.6 },
          hovertemplate: "%{x}<br>Spend %{y:$,.0f}<extra></extra>",
        },
        {
          type: "scatter",
          mode: "lines+markers",
          name: "Accounts won",
          x: data.labels,
          y: (data.new_customers || []).map(num),
          yaxis: "y2",
          line: { color: theme.accent, width: 2 },
          marker: { size: 6 },
          hovertemplate: "%{x}<br>Won %{y}<extra></extra>",
        },
      ],
      baseLayout(theme, {
        showlegend: true,
        legend: { orientation: "h", y: 1.14, x: 0 },
        xaxis: { tickangle: -35 },
        yaxis: { gridcolor: theme.grid, tickformat: "$.2s" },
        yaxis2: { overlaying: "y", side: "right", showgrid: false, rangemode: "tozero" },
        margin: { l: 62, r: 46, t: 34, b: 66 },
      })
    );
    const won = (data.new_customers || []).map(num).filter((v) => v != null);
    const cacs = (data.cac || []).map(num).filter((v) => v != null);
    if (won.length && cacs.length) {
      text(
        "marketingSpendRead",
        `${count(won.reduce((a, b) => a + b, 0))} accounts won across the year, at a monthly CAC ` +
          `between ${money(Math.min(...cacs))} and ${money(Math.max(...cacs))}.`
      );
    }
  };

  const drawFunnel = (charts, theme) => {
    const funnel = charts.funnel || {};
    const values = (funnel.values || []).map(num);
    if (!values.length || values[0] == null) return;
    // A horizontal bar rather than Plotly's `funnel` trace: plotly-basic does
    // not register funnel, and it would draw nothing without erroring.
    plot(
      "marketingFunnelChart",
      [
        {
          type: "bar",
          orientation: "h",
          x: values,
          y: funnel.labels || [],
          marker: { color: [theme.info, theme.warn, theme.accent] },
          hovertemplate: "%{y}<br>%{x:,.0f}<extra></extra>",
        },
      ],
      baseLayout(theme, {
        xaxis: { gridcolor: theme.grid, type: "log", title: { text: "log scale" } },
        yaxis: { automargin: true, autorange: "reversed" },
        margin: { l: 10, r: 18, t: 12, b: 52 },
      })
    );
    const [leads, qualified, won] = values;
    if (leads && qualified != null && won != null) {
      text(
        "marketingFunnelRead",
        `${pct((qualified / leads) * 100)} of leads qualify and ${pct((won / qualified) * 100)} of ` +
          `those close — ${pct((won / leads) * 100)} of all leads become accounts.`
      );
    }
  };

  const drawChannels = (charts, theme) => {
    const data = charts.channel_spend || {};
    const spend = (data.spend || []).map(num);
    if (!spend.length) return;
    plot(
      "marketingChannelChart",
      [
        {
          type: "bar",
          orientation: "h",
          x: spend,
          y: data.labels || [],
          marker: { color: theme.accent },
          hovertemplate: "%{y}<br>%{x:$,.0f}<extra></extra>",
        },
      ],
      baseLayout(theme, {
        xaxis: { gridcolor: theme.grid, tickformat: "$.2s" },
        yaxis: { automargin: true, autorange: "reversed" },
        margin: { l: 10, r: 18, t: 12, b: 44 },
      })
    );
    const total = spend.reduce((a, b) => a + (b || 0), 0);
    if (total > 0 && (data.labels || []).length) {
      text(
        "marketingChannelRead",
        `${data.labels[0]} takes ${pct((spend[0] / total) * 100)} of marketing spend.`
      );
    }
  };

  const render = (payload) => {
    const body = document.getElementById("marketingBody");
    const unavailable = document.getElementById("marketingUnavailable");

    if (!payload || payload.available === false) {
      if (unavailable) unavailable.hidden = false;
      text("marketingUnavailableDetail", payload?.warning || "The seeded campaign layer is missing.");
      return;
    }
    if (unavailable) unavailable.hidden = true;
    if (body) body.hidden = false;

    const selected = payload.selected || {};
    const basis = payload.basis || {};
    text("marketingScope", basis.scope || "");
    text(
      "marketingWindow",
      selected.label
        ? `${selected.label}${selected.complete ? "" : ` (${selected.months} of 12 months)`}`
        : MISSING
    );
    text("marketingGrain", basis.grain || MISSING);
    text(
      "marketingChannelBasis",
      "Cost per win is marketing spend only — the selling-compensation share is a company-level " +
        "split with no channel attribution behind it."
    );
    text("marketingMonthlyBasis", basis.modelled || "");

    renderKpis(payload.kpis);
    renderChannels(payload.channels);
    renderMonthly(payload.monthly);

    text(
      "marketingProvenance",
      [basis.new_customer_basis, basis.spend_basis, basis.excluded_reason]
        .filter(Boolean)
        .join(" ")
    );

    const charts = payload.charts || {};
    const drawAll = () => {
      const theme = plotTheme();
      const draw = (name, fn) => {
        try {
          fn(charts, theme);
        } catch (error) {
          console.warn(`${name} chart failed`, error);
        }
      };
      draw("spend", drawSpend);
      draw("funnel", drawFunnel);
      draw("channels", drawChannels);
    };
    if (window.ChartUtils && window.ChartUtils.whenPlotlyReady) {
      window.ChartUtils.whenPlotlyReady(drawAll);
    } else if (window.Plotly) {
      drawAll();
    }
  };

  const boot = () => {
    fetch(root.dataset.bundleUrl, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then((response) => (response.ok ? response.json() : Promise.reject(new Error(String(response.status)))))
      .then(render)
      .catch((error) => {
        console.warn("marketing bundle failed", error);
        render({ available: false, warning: "The marketing bundle could not be loaded." });
      });
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
