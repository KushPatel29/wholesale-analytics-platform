/*
 * Finance page renderer.
 *
 * Formatting goes through window.WAFormat throughout - the legacy `currency`
 * and `percent` helpers always print two decimals and are the reason one figure
 * once appeared three ways on this app. Compact currency ($1.2M) is used for
 * axis ticks only, never for a stated figure.
 *
 * Every chart is `type: "scatter"` or `"bar"`, never `scattergl`: the static
 * build freezes charts through Plotly.toImage({format:'svg'}), which cannot
 * serialise a WebGL trace and would publish a blank frame.
 */
(() => {
  "use strict";

  const root = document.getElementById("FinanceApp");
  if (!root) return;

  const F = window.WAFormat || {};
  const MISSING = F.MISSING || "—";

  const num = (value) => (Number.isFinite(Number(value)) ? Number(value) : null);
  // `forceDecimals: false` is the option WAFormat.currency actually reads - it
  // defaults to cents below WHOLE_DOLLAR_THRESHOLD, which put "$2,084.78" in a
  // statement column next to whole-dollar figures. A statement is read down the
  // column, so every line on it is stated the same way.
  const money = (value) => (num(value) == null ? MISSING : F.currency(value, { forceDecimals: false }));
  const pct = (value) => (num(value) == null ? MISSING : F.percent(value, { decimals: 1 }));
  const mult = (value) => (num(value) == null ? MISSING : `${F.decimal(value, { decimals: 2 })}×`);
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
    window.Plotly.newPlot(host, traces, layout, {
      displayModeBar: false,
      responsive: true,
      staticPlot: false,
    });
  };

  // ── ratios ────────────────────────────────────────────────────────────────
  const renderRatios = (ratios) => {
    const host = document.getElementById("financeRatios");
    if (!host) return;
    host.innerHTML = (ratios || [])
      .map(
        (ratio) => `
      <article class="finance-ratio">
        <span class="finance-ratio__label">${escapeHtml(ratio.label)}</span>
        <span class="finance-ratio__value">${escapeHtml(formatByUnit(ratio.value, ratio.unit))}</span>
        <span class="finance-ratio__formula">${escapeHtml(ratio.formula)}</span>
        <span class="finance-ratio__basis">${escapeHtml(ratio.basis)}</span>
        ${ratio.note ? `<span class="finance-ratio__note">${escapeHtml(ratio.note)}</span>` : ""}
      </article>`
      )
      .join("");
  };

  // ── income statement, with every fiscal year as a comparative column ──────
  const renderStatement = (payload) => {
    const comparatives = payload.comparatives || [];
    const lines = payload.income_statement?.lines || [];
    const selectedLabel = payload.selected?.label;

    const head = document.getElementById("financeStatementHead");
    if (head) {
      head.innerHTML =
        '<th scope="col">Line</th>' +
        comparatives
          .map((fy) => {
            const suffix = fy.complete ? "" : ` (${fy.months} mo)`;
            return `<th scope="col" class="num">${escapeHtml(fy.label + suffix)}</th>`;
          })
          .join("") +
        `<th scope="col" class="num">% of sales${selectedLabel ? ` (${escapeHtml(selectedLabel)})` : ""}</th>`;
    }

    // The comparative columns need the same lines the selected year shows, so
    // they are keyed off the statement's own line keys rather than re-derived.
    const columnValue = (fy, key) => (key in fy ? fy[key] : undefined);

    const body = document.getElementById("financeStatementBody");
    if (!body) return;
    body.innerHTML = lines
      .map((line) => {
        const rowClass =
          line.kind === "total"
            ? "finance-row--total"
            : line.kind === "subtotal"
            ? "finance-row--subtotal"
            : line.kind === "cost"
            ? "finance-row--indent"
            : "";
        const cells = comparatives
          .map((fy) => {
            const value = columnValue(fy, line.key);
            // A line the comparative summary does not carry is left blank
            // rather than filled with the selected year's figure.
            if (value === undefined) {
              const own = fy.label === selectedLabel ? line.amount : null;
              return `<td class="num">${escapeHtml(own == null ? MISSING : money(own))}</td>`;
            }
            return `<td class="num">${escapeHtml(money(value))}</td>`;
          })
          .join("");
        return `<tr class="${rowClass}">
          <td>${escapeHtml(line.label)}</td>
          ${cells}
          <td class="num">${escapeHtml(pct(line.pct_of_revenue))}</td>
        </tr>`;
      })
      .join("");
  };

  // ── gross → net bridge ───────────────────────────────────────────────────
  const renderBridge = (bridge) => {
    const body = document.getElementById("financeBridgeBody");
    if (!body) return;
    const rows = [
      ["Gross sales", bridge.gross_sales, false],
      ["Less discounts", bridge.discounts, true],
      ["Less return credits", bridge.returns, true],
      ["Less discount & return handling", bridge.adjustment_costs, true],
      ["Net sales", bridge.net_sales, false],
    ];
    body.innerHTML = rows
      .map(([label, value, deduct], index) => {
        const last = index === rows.length - 1;
        const shown = num(value) == null ? MISSING : (deduct ? "(" + money(value) + ")" : money(value));
        return `<tr class="${last ? "finance-row--total" : ""}">
          <td>${escapeHtml(label)}</td>
          <td class="num">${escapeHtml(shown)}</td>
        </tr>`;
      })
      .join("") +
      `<tr><td>Invoiced sales (the basis above)</td><td class="num">${escapeHtml(money(bridge.invoice_sales))}</td></tr>`;

    const detail = bridge.returns_available
      ? `${bridge.return_count ?? 0} approved return credits are netted here.`
      : "Return credits are unavailable, so net sales is not stated.";
    text("financeBridgeBasis", detail);
  };

  // ── balance sheet ────────────────────────────────────────────────────────
  const renderBalanceSheet = (sheet) => {
    const rowsFor = (items, totals) =>
      (items || [])
        .map((item) => {
          const isTotal = totals.includes(item.key);
          return `<tr class="${isTotal ? "finance-row--subtotal" : "finance-row--indent"}">
            <td>${escapeHtml(item.label)}</td>
            <td class="num">${escapeHtml(money(item.amount))}</td>
          </tr>`;
        })
        .join("");

    const assets = document.getElementById("financeAssets");
    if (assets) assets.innerHTML = rowsFor(sheet.assets, ["current_assets", "total_assets"]);
    const liabilities = document.getElementById("financeLiabilities");
    if (liabilities) {
      liabilities.innerHTML = rowsFor(sheet.liabilities, ["current_liabilities", "total_liabilities"]);
    }
    text("financeBalanceAsOf", `Balances as at ${sheet.as_of}.`);
  };

  // ── monthly detail ───────────────────────────────────────────────────────
  const renderMonthly = (rows) => {
    const body = document.getElementById("financeMonthly");
    if (!body) return;
    body.innerHTML = (rows || [])
      .map(
        (row) => `<tr>
        <td>${escapeHtml(row.label)}</td>
        <td class="num">${escapeHtml(money(row.revenue))}</td>
        <td class="num">${escapeHtml(money(row.gross_profit))}</td>
        <td class="num">${escapeHtml(money(row.operating_expenses))}</td>
        <td class="num${num(row.net_income) != null && row.net_income < 0 ? " finance-negative" : ""}">${escapeHtml(money(row.net_income))}</td>
        <td class="num">${escapeHtml(pct(row.net_margin))}</td>
        <td class="num">${escapeHtml(money(row.working_capital))}</td>
        <td class="num">${escapeHtml(mult(row.current_ratio))}</td>
      </tr>`
      )
      .join("");
  };

  // ── charts ───────────────────────────────────────────────────────────────
  const drawProfitBridge = (charts, theme) => {
    const bridge = charts.profit_bridge || {};
    const values = (bridge.values || []).map(num);
    if (!values.length) return;

    // Built from `bar` with a `base` offset rather than Plotly's `waterfall`.
    // base.html loads plotly-basic for this page, and plotly-basic registers
    // exactly three trace types - bar, pie, scatter. A waterfall trace is
    // accepted without error and simply draws nothing, which is the worst of
    // both worlds: no console message and an empty frame in the published SVG.
    const bases = [];
    const heights = [];
    const colours = [];
    let running = 0;
    values.forEach((value, index) => {
      const isTotal = index === 0 || index === values.length - 1;
      if (value == null) {
        bases.push(0);
        heights.push(0);
        colours.push(theme.grid);
        return;
      }
      if (isTotal) {
        bases.push(0);
        heights.push(value);
        colours.push(theme.info);
        running = value;
      } else if (value >= 0) {
        bases.push(running);
        heights.push(value);
        colours.push(theme.accent);
        running += value;
      } else {
        running += value;
        bases.push(running);
        heights.push(-value);
        colours.push(theme.danger);
      }
    });

    plot(
      "financeProfitBridgeChart",
      [
        {
          type: "bar",
          x: bridge.labels || [],
          y: heights,
          base: bases,
          marker: { color: colours },
          customdata: values,
          hovertemplate: "%{x}<br>%{customdata:$,.0f}<extra></extra>",
        },
      ],
      baseLayout(theme, {
        yaxis: { gridcolor: theme.grid, zerolinecolor: theme.grid, tickformat: "$.2s" },
        xaxis: { tickangle: -30 },
        margin: { l: 62, r: 18, t: 12, b: 86 },
      })
    );
    const revenue = values[0];
    const net = values[values.length - 1];
    if (revenue != null && net != null) {
      text(
        "financeProfitBridgeRead",
        `Of every dollar invoiced, ${pct((net / revenue) * 100)} reaches net income; ` +
          `cost of goods takes the largest single step.`
      );
    }
  };

  const drawResult = (charts, theme) => {
    const result = charts.monthly_result || {};
    if (!(result.labels || []).length) return;
    plot(
      "financeResultChart",
      [
        {
          type: "bar",
          name: "Invoiced sales",
          x: result.labels,
          y: (result.revenue || []).map(num),
          marker: { color: theme.info, opacity: 0.55 },
          hovertemplate: "%{x}<br>Invoiced sales %{y:$,.0f}<extra></extra>",
        },
        {
          type: "bar",
          name: "Net income",
          x: result.labels,
          y: (result.net_income || []).map(num),
          marker: { color: theme.accent },
          hovertemplate: "%{x}<br>Net income %{y:$,.0f}<extra></extra>",
        },
        {
          type: "scatter",
          mode: "lines+markers",
          name: "Net margin",
          x: result.labels,
          y: (result.net_margin || []).map(num),
          yaxis: "y2",
          line: { color: theme.warn, width: 2 },
          marker: { size: 5 },
          hovertemplate: "%{x}<br>Net margin %{y:.1f}%<extra></extra>",
        },
      ],
      baseLayout(theme, {
        barmode: "group",
        showlegend: true,
        legend: { orientation: "h", y: 1.14, x: 0 },
        xaxis: { tickangle: -35 },
        yaxis: { gridcolor: theme.grid, tickformat: "$.2s" },
        yaxis2: { overlaying: "y", side: "right", ticksuffix: "%", showgrid: false },
        margin: { l: 62, r: 56, t: 34, b: 66 },
      })
    );

    const margins = (result.net_margin || []).map(num).filter((value) => value != null);
    const incomes = (result.net_income || []).map(num).filter((value) => value != null);
    if (margins.length && incomes.length) {
      text(
        "financeResultRead",
        `Net income ran from ${money(Math.min(...incomes))} to ${money(Math.max(...incomes))} ` +
          `across the year, on a margin between ${pct(Math.min(...margins))} and ${pct(Math.max(...margins))}.`
      );
    }
  };

  const drawOpex = (charts, theme) => {
    const opex = charts.opex_composition || {};
    const values = (opex.values || []).map(num);
    if (!values.length) return;
    const pairs = (opex.labels || [])
      .map((label, index) => ({ label, value: values[index] }))
      .filter((pair) => pair.value != null)
      .sort((a, b) => b.value - a.value);
    plot(
      "financeOpexChart",
      [
        {
          type: "bar",
          orientation: "h",
          x: pairs.map((pair) => pair.value),
          y: pairs.map((pair) => pair.label),
          marker: { color: theme.accent },
          hovertemplate: "%{y}<br>%{x:$,.0f}<extra></extra>",
        },
      ],
      baseLayout(theme, {
        xaxis: { gridcolor: theme.grid, tickformat: "$.2s" },
        yaxis: { automargin: true },
        margin: { l: 10, r: 18, t: 12, b: 44 },
      })
    );
    const total = pairs.reduce((sum, pair) => sum + pair.value, 0);
    if (pairs.length && total > 0) {
      // `text` writes through textContent, so escaping here would publish a
      // literal "&amp;" in "Selling & sales compensation".
      text(
        "financeOpexRead",
        `${pairs[0].label} is the largest line at ${pct((pairs[0].value / total) * 100)} of operating expense.`
      );
    }
  };

  const drawAging = (charts, theme) => {
    const aging = charts.ar_aging || {};
    const values = (aging.values || []).map(num);
    if (!values.length) return;
    // Current is healthy, the tail is not - colour carries the reading.
    const colours = values.map((_, index) =>
      index === 0 ? theme.accent : index >= values.length - 2 ? theme.danger : theme.warn
    );
    plot(
      "financeAgingChart",
      [
        {
          type: "bar",
          x: aging.labels || [],
          y: values,
          marker: { color: colours },
          hovertemplate: "%{x}<br>%{y:$,.0f}<extra></extra>",
        },
      ],
      baseLayout(theme, {
        yaxis: { gridcolor: theme.grid, tickformat: "$.2s" },
        margin: { l: 62, r: 18, t: 12, b: 44 },
      })
    );
    const total = values.reduce((sum, value) => sum + (value || 0), 0);
    const overdue = values.slice(1).reduce((sum, value) => sum + (value || 0), 0);
    if (total > 0) {
      text(
        "financeAgingRead",
        `${pct((overdue / total) * 100)} of the receivable book is past invoice date; ` +
          `${money(values[values.length - 1])} has aged beyond 90 days.`
      );
    }
  };

  const drawLiquidity = (charts, theme) => {
    const liquidity = charts.liquidity_trend || {};
    if (!(liquidity.labels || []).length) return;
    plot(
      "financeLiquidityChart",
      [
        {
          type: "scatter",
          mode: "lines",
          name: "Working capital",
          x: liquidity.labels,
          y: (liquidity.working_capital || []).map(num),
          line: { color: theme.accent, width: 2 },
          fill: "tozeroy",
          fillcolor: "rgba(52,211,153,.12)",
          hovertemplate: "%{x}<br>Working capital %{y:$,.0f}<extra></extra>",
        },
        {
          type: "scatter",
          mode: "lines",
          name: "Current ratio",
          x: liquidity.labels,
          y: (liquidity.current_ratio || []).map(num),
          yaxis: "y2",
          line: { color: theme.warn, width: 2, dash: "dot" },
          hovertemplate: "%{x}<br>Current ratio %{y:.2f}×<extra></extra>",
        },
      ],
      baseLayout(theme, {
        showlegend: true,
        legend: { orientation: "h", y: 1.14, x: 0 },
        xaxis: { tickangle: -35 },
        yaxis: { gridcolor: theme.grid, tickformat: "$.2s" },
        yaxis2: { overlaying: "y", side: "right", showgrid: false, ticksuffix: "×" },
        margin: { l: 62, r: 52, t: 34, b: 66 },
      })
    );
    const ratios = (liquidity.current_ratio || []).map(num).filter((value) => value != null);
    if (ratios.length) {
      const low = Math.min(...ratios);
      const high = Math.max(...ratios);
      text(
        "financeLiquidityRead",
        `The current ratio holds between ${mult(low)} and ${mult(high)} across the period.`
      );
    }
  };

  // ── boot ─────────────────────────────────────────────────────────────────
  const render = (payload) => {
    const body = document.getElementById("financeBody");
    const unavailable = document.getElementById("financeUnavailable");

    if (!payload || payload.available === false) {
      if (unavailable) unavailable.hidden = false;
      text("financeUnavailableDetail", payload?.warning || "The seeded financial layer is missing.");
      return;
    }
    if (unavailable) unavailable.hidden = true;
    if (body) body.hidden = false;

    const selected = payload.selected || {};
    const basis = payload.basis || {};

    text("financeScope", basis.scope || "");
    text(
      "financeWindow",
      selected.label
        ? `${selected.label}: ${selected.start} to ${selected.end}${selected.complete ? "" : ` (${selected.months} of 12 months)`}`
        : MISSING
    );
    text("financeGrain", basis.grain || MISSING);
    text("financeStatementBasis", basis.revenue_source || "");
    text(
      "financeMonthlyBasis",
      `${selected.label || ""} by month. ${basis.modelled || ""}`.trim()
    );

    renderRatios(payload.ratios);
    renderStatement(payload);
    renderBridge(payload.income_statement?.gross_to_net || {});
    renderBalanceSheet(payload.balance_sheet || {});
    renderMonthly(payload.monthly);

    const excluded = basis.incomplete_months_excluded || [];
    text(
      "financeProvenance",
      [
        basis.modelled || "",
        excluded.length
          ? `Incomplete months excluded from every figure on this page: ${excluded.join(", ")}.`
          : "",
        payload.dataset_version ? `Dataset version ${payload.dataset_version}.` : "",
      ]
        .filter(Boolean)
        .join(" ")
    );

    // base.html loads plotly-basic lazily, on requestIdleCallback after window
    // load, so it is routinely absent when this bundle resolves. Drawing
    // straight away made `plot()` return silently: the prose captions appeared,
    // the chart frames stayed empty, and the static build would have frozen
    // five blank SVGs. `whenPlotlyReady` is the house wait.
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
      draw("profit bridge", drawProfitBridge);
      draw("monthly result", drawResult);
      draw("opex", drawOpex);
      draw("aging", drawAging);
      draw("liquidity", drawLiquidity);
    };
    if (window.ChartUtils && window.ChartUtils.whenPlotlyReady) {
      window.ChartUtils.whenPlotlyReady(drawAll);
    } else if (window.Plotly) {
      drawAll();
    }
  };

  const boot = () => {
    // A plain fetch on purpose. The static build installs a shim that replaces
    // window.fetch and answers from the payloads it captured at build time, so
    // reading its JSON blob directly here would duplicate the shim and risk
    // matching the wrong path.
    const url = root.dataset.bundleUrl;
    fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then((response) => (response.ok ? response.json() : Promise.reject(new Error(String(response.status)))))
      .then(render)
      .catch((error) => {
        console.warn("finance bundle failed", error);
        render({ available: false, warning: "The finance bundle could not be loaded." });
      });
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
