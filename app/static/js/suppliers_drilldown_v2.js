(() => {
  const meta = document.getElementById("SupplierDrilldownV2Meta");
  if (!meta) return;
  if (meta.dataset.bound === "1") return;
  meta.dataset.bound = "1";
  const authFetch = window.authFetch || fetch;

  if (document?.body?.dataset) {
    document.body.dataset.filtersHandler = "ajax";
  }

  const supplierId = meta.dataset.entityId || "";
  const bundleUrl = meta.dataset.bundleUrl || "/api/suppliers/drilldown/bundle";
  const exportCsvBase = meta.dataset.exportCsvBase || "";
  const exportXlsxBase = meta.dataset.exportXlsxBase || "";
  const initialPageNotice = (meta.dataset.pageNotice || "").trim();
  const showCosts = (() => {
    try {
      return JSON.parse(meta.dataset.showCosts || "true") !== false;
    } catch (_err) {
      return true;
    }
  })();
  const initialPayload = (() => {
    try {
      return JSON.parse(meta.dataset.initial || "{}");
    } catch (_err) {
      return {};
    }
  })();

  const nfInt = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
  const nfNum2 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
  const nfPct = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
  const nfMoney0 = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 0 });
  const nfMoney2 = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const ASP_LB_LABEL = "ASP/lb";

  const state = {
    filterQs: (window.location.search || "").replace(/^\?/, ""),
    sortBy: "revenue",
    sortDir: "desc",
    search: "",
    proteinFocus: "",
    tagFocus: "",
    productsRows: [],
    charts: {},
    loading: false,
    activeFetchController: null,
    lastBundleKey: "",
    currentV2: {},
    sectionObserver: null,
    requestSeq: 0,
  };
  let currentApplyId = "";

  const fmtMoney0 = (v) => (v == null || Number.isNaN(Number(v)) ? "—" : nfMoney0.format(Number(v)));
  const fmtMoney2 = (v) => (v == null || Number.isNaN(Number(v)) ? "—" : nfMoney2.format(Number(v)));
  const fmtPct = (v) => (v == null || Number.isNaN(Number(v)) ? "—" : `${nfPct.format(Number(v))}%`);
  const fmtInt = (v) => (v == null || Number.isNaN(Number(v)) ? "—" : nfInt.format(Number(v)));
  const fmtNum2Safe = (v) => (v == null || Number.isNaN(Number(v)) ? "—" : nfNum2.format(Number(v)));
  const asNum = (v, d = null) => {
    const n = Number(v);
    return Number.isFinite(n) ? n : d;
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
  const normalizeQs = (qs) => String(qs || "").replace(/^\?/, "").trim();
  const truncate = (value, maxLen = 56) => {
    const text = String(value ?? "");
    return text.length > maxLen ? `${text.slice(0, maxLen - 1)}…` : text;
  };

  const productDisplay = (row, variant = "full") => {
    const sku = row?.sku || row?.product_id || "";
    const productName = row?.product_name || (sku ? "Unnamed Product" : "");
    const full = row?.display_name || row?.product_label || [sku, productName].filter(Boolean).join(" — ") || sku || productName || "Unknown Product";
    if (variant === "axis") return row?.display_name_axis || truncate(full, 34);
    if (variant === "short") return row?.display_name_short || truncate(full, 58);
    return full;
  };

  const productHref = (row) => {
    const productId = row?.product_id;
    if (!productId) return "";
    const qs = state.filterQs ? `?${state.filterQs}` : "";
    return `/products/${encodeURIComponent(String(productId))}/drilldown${qs}`;
  };

  const customerHref = (row) => {
    const customerId = row?.customer_id;
    if (!customerId) return "";
    const qs = state.filterQs ? `?${state.filterQs}` : "";
    return `/customers/drilldown/${encodeURIComponent(String(customerId))}${qs}`;
  };

  const showPageNotice = (title, message, tone = "warn", showRetry = false) => {
    const root = document.getElementById("v2PageNotice");
    const titleEl = document.getElementById("v2PageNoticeTitle");
    const textEl = document.getElementById("v2PageNoticeText");
    const retryBtn = document.getElementById("v2RetryButton");
    if (!root || !titleEl || !textEl) return;
    root.classList.remove("d-none", "is-warn", "is-risk", "is-accent", "is-good");
    root.classList.add(`is-${tone || "warn"}`);
    titleEl.textContent = title || "Supplier detail notice";
    textEl.textContent = message || "Supplier detail is unavailable right now.";
    if (retryBtn) retryBtn.classList.toggle("d-none", !showRetry);
  };

  const hidePageNotice = () => {
    const root = document.getElementById("v2PageNotice");
    if (!root) return;
    root.classList.add("d-none");
  };

  const signalTone = (tag) => {
    const token = String(tag || "").trim().toLowerCase();
    if (token.includes("cost")) return "warn";
    if (token.includes("concentration")) return "warn";
    if (token.includes("below target") || token.includes("volatility")) return "warn";
    if (token.includes("outlier") || token.includes("risk")) return "risk";
    if (token.includes("strong") || token.includes("diversified")) return "good";
    return "accent";
  };

  const renderSignalBadges = (tags) => {
    if (!Array.isArray(tags) || !tags.length) return '<span class="text-muted">—</span>';
    return tags.map((tag) => `<span class="supplier-v2-signal is-${signalTone(tag)}">${escapeHtml(tag)}</span>`).join("");
  };
  const renderStatusBadge = (label, color) => {
    const text = String(label || "").trim();
    if (!text) return '<span class="text-muted">—</span>';
    const badgeColor = color || "#7a7f87";
    return `<span class="badge rounded-pill" style="background-color: ${escapeHtml(badgeColor)}; color: #fff;">${escapeHtml(text)}</span>`;
  };
  const renderColorMetric = (value, color, formatter) => {
    if (value == null || Number.isNaN(Number(value))) return "—";
    const tone = color ? ` style="color: ${escapeHtml(color)}; font-weight: 600;"` : "";
    return `<span${tone}>${escapeHtml(formatter(value))}</span>`;
  };
  const fmtSignedMoney2 = (value) => {
    if (value == null || Number.isNaN(Number(value))) return "—";
    const amount = Number(value);
    const sign = amount > 0 ? "+" : "";
    return `${sign}${nfMoney2.format(amount)}`;
  };
  const fmtSignedPts = (value) => {
    if (value == null || Number.isNaN(Number(value))) return "—";
    const amount = Number(value);
    const sign = amount > 0 ? "+" : "";
    return `${sign}${nfPct.format(amount)} pts`;
  };

  const statusTone = (value) => {
    const token = String(value || "").trim().toLowerCase();
    if (["strong", "stable", "diversified", "healthy"].includes(token)) return "good";
    if (["watch", "growth", "new", "concentrated"].includes(token)) return "warn";
    if (["risk", "decline"].includes(token)) return "risk";
    return "accent";
  };

  const setTone = (el, tone) => {
    if (!el) return;
    ["is-good", "is-warn", "is-risk", "is-accent"].forEach((cls) => el.classList.remove(cls));
    el.classList.add(`is-${tone || "accent"}`);
  };

  const setChip = (id, text, tone = "accent") => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = text;
    setTone(el, tone);
  };

  const renderProductCell = (row, metaBits = []) => {
    const fullLabel = productDisplay(row, "full");
    const shortLabel = productDisplay(row, "short");
    const href = productHref(row);
    const cleanMetaBits = metaBits.filter((bit) => String(bit || "").trim());
    const metaHtml = cleanMetaBits.length ? `<div class="supplier-v2-cell-meta">${cleanMetaBits.map((bit) => escapeHtml(bit)).join(" · ")}</div>` : "";
    if (href) {
      return `<div class="supplier-v2-product-cell"><a class="supplier-v2-product-link" href="${href}" title="${escapeHtml(fullLabel)}">${escapeHtml(shortLabel)}</a>${metaHtml}</div>`;
    }
    return `<div class="supplier-v2-product-cell"><span class="fw-semibold" title="${escapeHtml(fullLabel)}">${escapeHtml(shortLabel)}</span>${metaHtml}</div>`;
  };

  const renderCustomerCell = (row, metaBits = []) => {
    const label = row?.customer_name || row?.customer_id || "Unknown Customer";
    const href = customerHref(row);
    const cleanMetaBits = metaBits.filter((bit) => String(bit || "").trim());
    const metaHtml = cleanMetaBits.length ? `<div class="supplier-v2-cell-meta">${cleanMetaBits.map((bit) => escapeHtml(bit)).join(" · ")}</div>` : "";
    if (href) {
      return `<div class="supplier-v2-product-cell"><a class="supplier-v2-product-link" href="${href}" title="${escapeHtml(label)}">${escapeHtml(label)}</a>${metaHtml}</div>`;
    }
    return `<div class="supplier-v2-product-cell"><span class="fw-semibold">${escapeHtml(label)}</span>${metaHtml}</div>`;
  };

  const destroyChart = (name) => {
    const chart = state.charts[name];
    if (chart && typeof chart.destroy === "function") {
      chart.destroy();
    }
    delete state.charts[name];
  };

  const setChartEmpty = (canvasId, message) => {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    const wrap = canvas.closest(".chart-wrap");
    if (!wrap) return;
    let empty = wrap.querySelector(".chart-empty");
    if (message) {
      if (!empty) {
        empty = document.createElement("div");
        empty.className = "chart-empty text-muted small";
        wrap.appendChild(empty);
      }
      empty.textContent = message;
      wrap.classList.add("is-empty");
      canvas.classList.add("d-none");
      return;
    }
    if (empty) empty.remove();
    wrap.classList.remove("is-empty");
    canvas.classList.remove("d-none");
  };

  const initTooltips = () => {
    if (!window.bootstrap || !window.bootstrap.Tooltip) return;
    document.querySelectorAll(".supplier-drilldown-v2 [data-bs-toggle='tooltip']").forEach((el) => {
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
    const links = Array.from(document.querySelectorAll(".supplier-v2-subnav-link"));
    if (!links.length) return;
    const sections = links
      .map((link) => document.querySelector(link.getAttribute("href") || ""))
      .filter(Boolean);

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
            if (!candidate || entry.intersectionRatio > candidate.intersectionRatio) {
              candidate = entry;
            }
          });
          if (candidate?.target?.id) {
            setActive(candidate.target.id);
          }
        },
        { threshold: [0.2, 0.35, 0.55], rootMargin: "-25% 0px -55% 0px" }
      );
      sections.forEach((section) => state.sectionObserver.observe(section));
    }

    if (sections[0]?.id) {
      setActive(sections[0].id);
    }
  };

  const makeHistogram = (values, bins = 16) => {
    const nums = (values || []).map((v) => Number(v)).filter((v) => Number.isFinite(v));
    if (!nums.length) return { labels: [], counts: [] };
    const min = Math.min(...nums);
    const max = Math.max(...nums);
    if (max <= min) {
      return { labels: [`${min.toFixed(2)}`], counts: [nums.length] };
    }
    const width = (max - min) / bins;
    const counts = new Array(bins).fill(0);
    nums.forEach((v) => {
      const raw = Math.floor((v - min) / width);
      const idx = Math.max(0, Math.min(bins - 1, raw));
      counts[idx] += 1;
    });
    const labels = counts.map((_, i) => {
      const lo = min + i * width;
      const hi = lo + width;
      return `${lo.toFixed(1)}-${hi.toFixed(1)}`;
    });
    return { labels, counts };
  };

  const exportUrl = (dataset, format) => {
    const base = format === "xlsx" ? exportXlsxBase : exportCsvBase;
    const params = new URLSearchParams(state.filterQs || "");
    params.set("dataset", dataset);
    params.set("supplier_drilldown_v2", "1");
    return `${base}?${params.toString()}`;
  };

  const bindExportLinks = () => {
    document.querySelectorAll(".js-v2-export").forEach((el) => {
      const dataset = el.getAttribute("data-dataset") || "products";
      const format = el.getAttribute("data-format") || "csv";
      el.setAttribute("href", exportUrl(dataset, format));
    });
  };

  const buildBundleParams = () => {
    const params = new URLSearchParams(state.filterQs || "");
    params.set("supplier_id", supplierId);
    params.set("supplier_drilldown_v2", "1");
    params.set("top_n", "250");
    return params;
  };

  const renderHeader = (v2) => {
    const header = (v2 && v2.header) || {};
    const score = (v2 && v2.scorecard) || {};
    const windowMeta = (v2 && v2.window) || {};

    setText("v2Title", score.supplier_name || supplierId || "Supplier");
    setChip("badgeSupplierId", `Supplier ID: ${score.supplier_id || supplierId || "—"}`, "accent");
    setText("v2HeroSubtitle", header.coverage_note || "Executive supplier view for pricing quality, margin trust, customer dependency, and protein concentration under the active scope.");
    setText("v2HeroNarrative", header.narrative || "Supplier detail narrative will populate after scoped supplier metrics load.");

    if (windowMeta.start && windowMeta.end) {
      const priorSummary = windowMeta.prior_start && windowMeta.prior_end
        ? ` · prior ${windowMeta.prior_start} to ${windowMeta.prior_end}`
        : "";
      setText("v2WindowSummary", `Active window ${windowMeta.start} to ${windowMeta.end}${priorSummary}.`);
    } else {
      setText("v2WindowSummary", "Computed using current filters and scope.");
    }
    if (windowMeta.prior_start && windowMeta.prior_end) {
      setText("v2ComparisonSummary", `Prior comparison ${windowMeta.prior_start} to ${windowMeta.prior_end}.`);
    } else {
      setText("v2ComparisonSummary", "Prior-period context updates when comparison data is available.");
    }

    setChip("badgeLifecycle", `Lifecycle: ${score.lifecycle || "Stable"}`, statusTone(score.lifecycle || "Stable"));
    setChip("badgeClass", `Class: ${score.classification || "—"}`, statusTone(score.classification || "Watch"));
    setChip("badgeCoverage", `Cost coverage: ${fmtPct(score.cost_coverage_pct)}`, asNum(score.cost_coverage_pct, 0) >= 90 ? "good" : asNum(score.cost_coverage_pct, 0) >= 75 ? "warn" : "risk");
    setChip("badgeTopProtein", `Top protein: ${score.top_protein || "—"}`, statusTone((score.top_protein_share_pct || 0) >= 65 ? "concentrated" : "diversified"));
    setChip("badgeTopSku", `Top SKU: ${truncate(score.top_sku || "—", 40)}`, statusTone((score.top_sku_share_pct || 0) >= 20 ? "watch" : "stable"));
    setChip("badgeLastSold", `Last sold: ${score.last_sold || "—"}`, "accent");

    setText("heroHealthScore", fmtNum2Safe(score.health_score));
    setChip("heroHealthLabel", score.health_label || "Watch", statusTone(score.health_label || "Watch"));
    setText("heroCoverageValue", fmtPct(score.cost_coverage_pct));
    setText("heroSkuCount", fmtInt(score.active_skus));
    setText("heroCustomerCount", fmtInt(score.active_customers));

    const warn = document.getElementById("badgeDataWarn");
    if (warn) {
      const warnOn = asNum(score.cost_coverage_pct, 100) < 85 || asNum(score.cost_missing_rows, 0) > 0;
      warn.classList.toggle("d-none", !warnOn);
      if (warnOn) {
        setTone(warn, "warn");
        warn.textContent = `${fmtInt(score.cost_missing_rows)} cost gaps in window`;
      }
    }

    const trustBits = [];
    if (score.cost_coverage_pct != null) trustBits.push(`${fmtPct(score.cost_coverage_pct)} cost coverage`);
    if (score.rows_total != null && score.cost_missing_rows != null) trustBits.push(`${fmtInt(score.cost_missing_rows)} rows without cost`);
    if (score.last_sold) trustBits.push(`last activity ${score.last_sold}`);
    const trustNote = document.getElementById("heroTrustNote");
    if (trustNote) {
      if (asNum(score.cost_coverage_pct, 100) < 85) {
        trustNote.textContent = `Margin signals are directional because only ${fmtPct(score.cost_coverage_pct)} of rows have reliable cost coverage.`;
      } else if (trustBits.length) {
        trustNote.textContent = `Trust summary: ${trustBits.join(" · ")}.`;
      } else {
        trustNote.textContent = "Margin conclusions are most reliable when cost coverage remains healthy and trend history is stable.";
      }
    }
  };

  const renderOverviewStories = (v2) => {
    const trend = (v2 && v2.trend) || {};
    const mix = (v2 && v2.mix) || {};
    const protein = (v2 && v2.protein) || {};
    const pricing = (v2 && v2.pricing) || {};
    const playbook = (v2 && v2.playbook) || {};
    setText("v2CommercialStory", trend.narrative || "Commercial story will populate after trend diagnostics load.");
    setText("v2RiskStory", `${mix.narrative || "Concentration diagnostics pending."} ${protein.narrative || ""}`.trim());
    setText("v2ActionStory", playbook.narrative || pricing.narrative || "Action guidance will populate after the supplier bundle loads.");
  };

  const renderScorecard = (v2) => {
    const score = (v2 && v2.scorecard) || {};
    const trend = (v2 && v2.trend) || {};
    const mix = (v2 && v2.mix) || {};
    const custConc = mix.customer_concentration || {};
    const skuConc = mix.product_concentration || {};

    setText("kpiRevenue", fmtMoney0(score.total_revenue));
    setText("kpiRevenueDelta", `Δ window: ${fmtMoney0(score.revenue_delta_window)} (${fmtPct(score.revenue_delta_window_pct)})`);
    setText("kpiProfit", showCosts ? fmtMoney0(score.total_profit) : "—");
    setText("kpiMargin", showCosts ? fmtPct(score.gross_margin_pct) : "—");
    const marginEl = document.getElementById("kpiMargin");
    if (marginEl) {
      marginEl.style.color = showCosts && score.status_color ? String(score.status_color) : "";
    }
    setText(
      "kpiMarginMeta",
      showCosts
        ? `Target ${fmtPct(score.target_margin_pct)} · Min ${fmtPct(score.minimum_margin_pct)}`
        : "Gross margin on cost-covered rows"
    );
    setText(
      "kpiMarginStatus",
      showCosts
        ? `${score.target_status || "Target context unavailable"} · ${fmtSignedPts(score.target_gap_pct_points)} vs target`
        : "Margin target context hidden by permissions"
    );
    setText("kpiOrders", fmtInt(score.orders));
    setText("kpiUnits", fmtInt(score.units));
    setText("kpiWeight", fmtInt(score.weight_lb));
    setText("kpiSkus", fmtInt(score.active_skus));
    setText("kpiCustomers", fmtInt(score.active_customers));
    setText("kpiAsp", fmtMoney2(score.asp_lb));
    setText("kpiAspDelta", `${ASP_LB_LABEL} Δ vs prior: ${fmtPct(score.asp_lb_delta_pct)}`);

    setText("kpiCustHHI", fmtNum2Safe(score.customer_hhi ?? custConc.hhi));
    setText("kpiSkuHHI", fmtNum2Safe(score.sku_hhi ?? skuConc.hhi));
    setText("kpiTopShares", `${fmtPct(score.customer_top1_share ?? custConc.top1_share)} / ${fmtPct(score.customer_top5_share ?? custConc.top5_share)}`);
    setText("kpiTop10Shares", `${fmtPct(custConc.top10_share)} customers · ${fmtPct(skuConc.top10_share)} SKUs`);

    setText("kpiVolatility", showCosts ? fmtPct(score.margin_volatility) : "—");
    setText("kpiMoMDelta", `${fmtMoney0(score.revenue_delta_mom)} (${fmtPct(score.revenue_delta_mom_pct)})`);

    const rollingRevenue = asArr(trend.rolling_revenue_3m).filter((v) => Number.isFinite(Number(v))).map((v) => Number(v));
    const recent3m = rollingRevenue.length ? rollingRevenue[rollingRevenue.length - 1] : null;
    setText("kpiRecent3mAvg", fmtMoney0(recent3m));

    setText("kpiHealthScore", fmtNum2Safe(score.health_score));
    setText("kpiHealthLabel", score.health_label || "Watch");
    setTone(document.getElementById("kpiHealthLabel"), statusTone(score.health_label || "Watch"));
    setText("kpiHealthFormula", score.health_formula || "");
    renderOverviewStories(v2);
  };

  const renderNavBadges = (v2) => {
    const score = (v2 && v2.scorecard) || {};
    const trend = (v2 && v2.trend) || {};
    const pricing = (v2 && v2.pricing) || {};
    const protein = (v2 && v2.protein) || {};
    const opportunities = (v2 && v2.opportunities) || {};
    const playbook = (v2 && v2.playbook) || {};
    const productsTable = (v2 && v2.products_table) || {};

    setText("navOverviewBadge", score.health_label || "—");
    setText("navTrendBadge", `${asArr(trend.labels).length || 0} mo`);
    setText("navMixBadge", fmtInt(score.active_skus));
    setText("navProteinBadge", fmtInt(asArr(protein.rows).length));
    setText("navPricingBadge", fmtInt(asArr(pricing.outliers).length + asArr(opportunities.margin_at_risk).length));
    setText("navCustomersBadge", fmtInt(score.active_customers));
    setText("navProductsBadge", fmtInt(productsTable.total_rows));
    setText("navActionsBadge", fmtInt(asArr(playbook.cards).length));
    setText("navExportsBadge", "7");
  };

  const renderTrend = (v2) => {
    destroyChart("trend");
    const trend = (v2 && v2.trend) || {};
    const labels = asArr(trend.labels);
    const trendNote = document.getElementById("v2TrendDeltaNote");

    if (!labels.length || typeof Chart === "undefined") {
      setChartEmpty("v2TrendChart", "No monthly trend data for the selected window.");
      if (trendNote) trendNote.textContent = "Add a broader date range to view monthly trend diagnostics.";
      return;
    }

    const ctx = document.getElementById("v2TrendChart");
    if (!ctx) return;
    setChartEmpty("v2TrendChart", null);

    const revenueSeries = asArr(trend.revenue).map((v) => asNum(v, 0));
    const profitSeries = asArr(trend.profit).map((v) => asNum(v, null));
    const marginSeries = asArr(trend.margin_pct).map((v) => asNum(v, null));

    state.charts.trend = new Chart(ctx, {
      type: "bar",
      data: {
        labels,
        datasets: [
          {
            type: "bar",
            label: "Revenue",
            data: revenueSeries,
            yAxisID: "yRevenue",
            backgroundColor: "#2b6cb0",
          },
          {
            type: "line",
            label: "Profit",
            data: profitSeries,
            yAxisID: "yRevenue",
            borderColor: "#16a34a",
            backgroundColor: "#16a34a",
            tension: 0.2,
            hidden: !showCosts,
          },
          {
            type: "line",
            label: "Margin %",
            data: marginSeries,
            yAxisID: "yMargin",
            borderColor: "#b45309",
            backgroundColor: "#b45309",
            tension: 0.2,
            hidden: !showCosts,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          tooltip: {
            callbacks: {
              label: (ctx) => {
                if (ctx.dataset?.label === "Margin %") return `Margin ${fmtPct(ctx.parsed.y)}`;
                return `${ctx.dataset?.label || "Value"} ${fmtMoney0(ctx.parsed.y)}`;
              },
            },
          },
        },
        scales: {
          x: { ticks: { maxRotation: 0, minRotation: 0 } },
          yRevenue: { position: "left", ticks: { callback: (v) => fmtMoney0(v) } },
          yMargin: { position: "right", grid: { drawOnChartArea: false }, ticks: { callback: (v) => `${v}%` } },
        },
      },
    });

    if (trendNote && revenueSeries.length >= 2) {
      const last = revenueSeries[revenueSeries.length - 1];
      const prev = revenueSeries[revenueSeries.length - 2];
      const delta = Number.isFinite(last) && Number.isFinite(prev) ? last - prev : null;
      const deltaPct = delta != null && prev > 0 ? (delta / prev) * 100 : null;
      trendNote.textContent = `Latest month revenue delta: ${fmtMoney0(delta)} (${fmtPct(deltaPct)}).`;
    }
  };

  const renderTopBars = (v2) => {
    destroyChart("prodRevenue");
    destroyChart("prodProfit");

    const mix = (v2 && v2.mix) || {};
    const revRows = asArr(mix.top_products_revenue).slice(0, 15);
    const profitRows = asArr(mix.top_products_profit).slice(0, 15);
    if (typeof Chart === "undefined") {
      setChartEmpty("v2TopProductsRevenueChart", "Chart library is unavailable.");
      setChartEmpty("v2TopProductsProfitChart", "Chart library is unavailable.");
      return;
    }

    const revCanvas = document.getElementById("v2TopProductsRevenueChart");
    if (revCanvas && revRows.length) {
      setChartEmpty("v2TopProductsRevenueChart", null);
      state.charts.prodRevenue = new Chart(revCanvas, {
        type: "bar",
        data: {
          labels: revRows.map((r) => productDisplay(r, "axis")),
          datasets: [{ label: "Revenue", data: revRows.map((r) => asNum(r.revenue, 0)), backgroundColor: "#1d4ed8" }],
        },
        options: {
          indexAxis: "y",
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            tooltip: {
              callbacks: {
                title: (items) => {
                  const item = items?.[0];
                  return item ? productDisplay(revRows[item.dataIndex], "full") : "";
                },
                label: (ctx) => `Revenue ${fmtMoney0(ctx.parsed.x)}`,
              },
            },
          },
        },
      });
    } else {
      setChartEmpty("v2TopProductsRevenueChart", "No product revenue rows for selected filters.");
    }

    const profitCanvas = document.getElementById("v2TopProductsProfitChart");
    if (profitCanvas && profitRows.length) {
      setChartEmpty("v2TopProductsProfitChart", null);
      state.charts.prodProfit = new Chart(profitCanvas, {
        type: "bar",
        data: {
          labels: profitRows.map((r) => productDisplay(r, "axis")),
          datasets: [{ label: "Profit", data: profitRows.map((r) => asNum(r.profit, 0)), backgroundColor: "#16a34a" }],
        },
        options: {
          indexAxis: "y",
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            tooltip: {
              callbacks: {
                title: (items) => {
                  const item = items?.[0];
                  return item ? productDisplay(profitRows[item.dataIndex], "full") : "";
                },
                label: (ctx) => `Profit ${fmtMoney0(ctx.parsed.x)}`,
              },
            },
          },
        },
      });
    } else {
      setChartEmpty("v2TopProductsProfitChart", showCosts ? "No product profit rows for selected filters." : "Cost permission is required to view profit diagnostics.");
    }
  };

  const renderProteinSection = (v2) => {
    destroyChart("proteinRevenue");
    destroyChart("categoryRevenue");
    const protein = (v2 && v2.protein) || {};
    const proteinRows = asArr(protein.rows).slice(0, 12);
    const categoryRows = asArr(protein.category_rows).slice(0, 12);
    const proteinNarrative = document.getElementById("v2ProteinNarrative");
    const categoryNarrative = document.getElementById("v2CategoryNarrative");
    if (proteinNarrative) proteinNarrative.textContent = protein.narrative || "Protein concentration commentary will populate after the supplier bundle loads.";
    if (categoryNarrative) categoryNarrative.textContent = protein.category_narrative || "Category breadth commentary will populate after the supplier bundle loads.";

    const focusWrap = document.getElementById("v2ProteinFocusCards");
    if (focusWrap) {
      const cards = asArr(protein.focus_cards);
      focusWrap.innerHTML = cards.length
        ? cards
            .map(
              (card) => `
            <article class="supplier-v2-focus-card is-${escapeHtml(card.tone || "accent")}">
              <div class="supplier-v2-focus-label">${escapeHtml(card.title || "Focus")}</div>
              <div class="supplier-v2-focus-value">${escapeHtml(card.value || "—")}</div>
              <div class="supplier-v2-focus-meta">${escapeHtml(card.meta || "")}</div>
            </article>
          `
            )
            .join("")
        : '<div class="supplier-v2-empty-copy">Protein focus cards will appear once mapped supplier rows are available.</div>';
    }

    const chipWrap = document.getElementById("v2ProteinFocusChips");
    if (chipWrap) {
      const chips = proteinRows.slice(0, 8);
      const activeProtein = state.proteinFocus;
      chipWrap.innerHTML = chips.length
        ? [
            `<button type="button" class="supplier-v2-chip-filter ${activeProtein ? "" : "is-active"}" data-protein-focus="">All proteins</button>`,
            ...chips.map(
              (row) => `<button type="button" class="supplier-v2-chip-filter ${activeProtein === row.protein_family ? "is-active" : ""}" data-protein-focus="${escapeHtml(
                row.protein_family || ""
              )}">${escapeHtml(row.protein_family || "Unassigned")} <span>${fmtPct(row.revenue_share_pct)}</span></button>`
            ),
          ].join("")
        : '<div class="supplier-v2-empty-copy">No protein filters are available for the current scope.</div>';
    }

    const proteinTbody = document.getElementById("v2ProteinRows");
    if (proteinTbody) {
      proteinTbody.innerHTML = proteinRows.length
        ? proteinRows
            .map(
              (row) => `
            <tr>
              <td><button type="button" class="supplier-v2-table-link" data-protein-focus="${escapeHtml(row.protein_family || "")}">${escapeHtml(row.protein_family || "Unassigned")}</button></td>
              <td>${escapeHtml(row.lead_category || "—")}</td>
              <td class="text-end">${fmtMoney0(row.revenue)}</td>
              <td class="text-end">${fmtPct(row.revenue_share_pct)}</td>
              <td class="text-end">${fmtPct(row.mix_shift_pp)}</td>
              <td class="text-end">${showCosts ? fmtPct(row.margin_pct) : "—"}</td>
            </tr>
          `
            )
            .join("")
        : '<tr><td colspan="6" class="text-muted text-center">No protein diagnostics in the selected window.</td></tr>';
    }

    const categoryTbody = document.getElementById("v2CategoryRows");
    if (categoryTbody) {
      categoryTbody.innerHTML = categoryRows.length
        ? categoryRows
            .map(
              (row) => `
            <tr>
              <td>${escapeHtml(row.product_category || "Unassigned")}</td>
              <td class="text-end">${fmtMoney0(row.revenue)}</td>
              <td class="text-end">${fmtPct(row.revenue_share_pct)}</td>
              <td class="text-end">${fmtPct(row.mix_shift_pp)}</td>
              <td class="text-end">${showCosts ? fmtPct(row.margin_pct) : "—"}</td>
              <td class="text-end">${fmtInt(row.sku_count)}</td>
            </tr>
          `
            )
            .join("")
        : '<tr><td colspan="6" class="text-muted text-center">No category diagnostics in the selected window.</td></tr>';
    }

    if (typeof Chart === "undefined") {
      setChartEmpty("v2ProteinRevenueChart", "Chart library is unavailable.");
      setChartEmpty("v2CategoryRevenueChart", "Chart library is unavailable.");
      return;
    }

    const proteinCanvas = document.getElementById("v2ProteinRevenueChart");
    if (proteinCanvas && proteinRows.length) {
      setChartEmpty("v2ProteinRevenueChart", null);
      state.charts.proteinRevenue = new Chart(proteinCanvas, {
        type: "bar",
        data: {
          labels: proteinRows.map((row) => row.protein_family || "Unassigned"),
          datasets: [
            {
              label: "Revenue",
              data: proteinRows.map((row) => asNum(row.revenue, 0)),
              backgroundColor: "#7f1d1d",
              borderRadius: 8,
            },
          ],
        },
        options: {
          indexAxis: "y",
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            tooltip: {
              callbacks: {
                label: (ctx) => {
                  const row = proteinRows[ctx.dataIndex] || {};
                  return [`Revenue ${fmtMoney0(row.revenue)}`, `Share ${fmtPct(row.revenue_share_pct)}`, `Mix shift ${fmtPct(row.mix_shift_pp)}`];
                },
              },
            },
          },
        },
      });
    } else {
      setChartEmpty("v2ProteinRevenueChart", "No protein-family revenue is available for the selected window.");
    }

    const categoryCanvas = document.getElementById("v2CategoryRevenueChart");
    if (categoryCanvas && categoryRows.length) {
      setChartEmpty("v2CategoryRevenueChart", null);
      state.charts.categoryRevenue = new Chart(categoryCanvas, {
        type: "bar",
        data: {
          labels: categoryRows.map((row) => truncate(row.product_category || "Unassigned", 24)),
          datasets: [
            {
              label: "Revenue",
              data: categoryRows.map((row) => asNum(row.revenue, 0)),
              backgroundColor: "#0f766e",
              borderRadius: 8,
            },
          ],
        },
        options: {
          indexAxis: "y",
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            tooltip: {
              callbacks: {
                title: (items) => {
                  const item = items?.[0];
                  return item ? categoryRows[item.dataIndex]?.product_category || "" : "";
                },
                label: (ctx) => {
                  const row = categoryRows[ctx.dataIndex] || {};
                  return [`Revenue ${fmtMoney0(row.revenue)}`, `Share ${fmtPct(row.revenue_share_pct)}`, `Mix shift ${fmtPct(row.mix_shift_pp)}`];
                },
              },
            },
          },
        },
      });
    } else {
      setChartEmpty("v2CategoryRevenueChart", "No category revenue is available for the selected window.");
    }
  };

  const renderDistributions = (v2) => {
    destroyChart("unitDist");
    destroyChart("marginDist");
    if (typeof Chart === "undefined") {
      setChartEmpty("v2UnitPriceDistChart", "Chart library is unavailable.");
      setChartEmpty("v2MarginDistChart", "Chart library is unavailable.");
      return;
    }

    const pricing = (v2 && v2.pricing) || {};

    const unitHist = makeHistogram(pricing.asp_lb_samples || [], 18);
    const unitCanvas = document.getElementById("v2UnitPriceDistChart");
    if (unitCanvas && unitHist.labels.length) {
      setChartEmpty("v2UnitPriceDistChart", null);
      state.charts.unitDist = new Chart(unitCanvas, {
        type: "bar",
        data: {
          labels: unitHist.labels,
          datasets: [{ label: ASP_LB_LABEL, data: unitHist.counts, backgroundColor: "#2563eb" }],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          scales: {
            x: { ticks: { autoSkip: true, maxTicksLimit: 8 }, title: { display: true, text: ASP_LB_LABEL } },
            y: { title: { display: true, text: "Rows" } },
          },
        },
      });
    } else {
      setChartEmpty("v2UnitPriceDistChart", `No ${ASP_LB_LABEL} observations for active filters.`);
    }

    const marginHist = makeHistogram(pricing.margin_samples || [], 18);
    const marginCanvas = document.getElementById("v2MarginDistChart");
    if (marginCanvas && marginHist.labels.length && showCosts) {
      setChartEmpty("v2MarginDistChart", null);
      state.charts.marginDist = new Chart(marginCanvas, {
        type: "bar",
        data: {
          labels: marginHist.labels,
          datasets: [{ label: "Rows", data: marginHist.counts, backgroundColor: "#10b981" }],
        },
        options: { responsive: true, maintainAspectRatio: false, scales: { x: { ticks: { autoSkip: true, maxTicksLimit: 8 } } } },
      });
    } else {
      setChartEmpty("v2MarginDistChart", showCosts ? "No margin observations under active filters." : "Cost permission is required to view margin diagnostics.");
    }
  };

  const renderPriceVelocity = (v2) => {
    destroyChart("priceVelocity");
    const pricing = (v2 && v2.pricing) || {};
    const points = asArr(pricing.price_velocity);
    const valid = points.filter((p) => Number.isFinite(Number(p.asp_lb)) && Number.isFinite(Number(p.velocity)));
    const summary = document.getElementById("v2ElasticitySummary");
    if (summary) {
      const e = pricing.elasticity || {};
      summary.textContent = e.insufficient_variation
        ? `Insufficient ${ASP_LB_LABEL} variation for a stable elasticity proxy.`
        : `Elasticity proxy correlation ${fmtNum2Safe(e.correlation)}, slope ${fmtNum2Safe(e.slope)} using ${ASP_LB_LABEL} (indicative only).`;
    }
    if (!valid.length || typeof Chart === "undefined") {
      setChartEmpty("v2PriceVelocityChart", `No sufficient ${ASP_LB_LABEL}/velocity points for the elasticity proxy.`);
      return;
    }
    const canvas = document.getElementById("v2PriceVelocityChart");
    if (!canvas) return;
    setChartEmpty("v2PriceVelocityChart", null);
    state.charts.priceVelocity = new Chart(canvas, {
      type: "bubble",
      data: {
        datasets: [
          {
            label: "SKUs",
            data: valid.map((p) => ({
              x: asNum(p.asp_lb, 0),
              y: asNum(p.velocity, 0),
              r: Math.max(3, Math.min(18, asNum(p.revenue_share_pct, 1) / 2)),
            })),
            backgroundColor: "rgba(37,99,235,0.45)",
            borderColor: "rgba(30,64,175,0.9)",
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: {
          x: { title: { display: true, text: ASP_LB_LABEL } },
          y: { title: { display: true, text: "Units / month" } },
        },
        plugins: {
          tooltip: {
            callbacks: {
              title: (items) => {
                const item = items?.[0];
                return item ? productDisplay(valid[item.dataIndex], "full") : "";
              },
              label: (ctx) => {
                const row = valid[ctx.dataIndex] || {};
                return [
                  `${ASP_LB_LABEL} ${fmtMoney2(row.asp_lb)}`,
                  `Velocity ${fmtNum2Safe(row.velocity)} units/mo`,
                  `Revenue share ${fmtPct(row.revenue_share_pct)}`,
                  `Margin ${showCosts ? fmtPct(row.margin_pct) : "—"}`,
                ];
              },
            },
          },
        },
      },
    });
  };

  const renderOutliers = (v2) => {
    const rows = ((v2 && v2.pricing) || {}).outliers || [];
    const tbody = document.getElementById("v2OutliersRows");
    if (!tbody) return;
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="4" class="text-muted text-center">No ${ASP_LB_LABEL} outliers detected under active filters.</td></tr>`;
      return;
    }
    tbody.innerHTML = rows.slice(0, 30).map((r) => `
      <tr>
        <td>${renderProductCell(r, [r.outlier_type === "high" ? "Above peer band" : "Below peer band", r.last_sold ? `Last sold ${r.last_sold}` : ""])}</td>
        <td class="text-end">${fmtMoney2(r.asp_lb)}</td>
        <td class="text-end">${fmtPct(r.delta_pct_vs_peer)}</td>
        <td>${renderSignalBadges([r.outlier_type === "high" ? "High price outlier" : "Low price outlier"])}</td>
      </tr>
    `).join("");
  };

  const renderMarginRisk = (v2) => {
    const rows = (((v2 && v2.opportunities) || {}).margin_at_risk) || [];
    const tbody = document.getElementById("v2MarginRiskRows");
    if (!tbody) return;
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="text-muted text-center">No below-target-margin products in the selected window.</td></tr>';
      return;
    }
    tbody.innerHTML = rows.slice(0, 30).map((r) => `
      <tr>
        <td>${renderProductCell(r, [`Target ${fmtPct(r.target_margin_pct)}`, r.asp_lb != null ? `${ASP_LB_LABEL} ${fmtMoney2(r.asp_lb)}` : ""])}</td>
        <td class="text-end">${fmtMoney0(r.revenue)}</td>
        <td class="text-end">${showCosts ? fmtPct(r.margin_pct) : "—"}</td>
        <td class="text-end">${showCosts ? fmtMoney0(r.uplift_to_target) : "—"}</td>
        <td>${escapeHtml(r.last_sold || "")}</td>
      </tr>
    `).join("");
  };

  const renderCustomers = (v2) => {
    const customers = (v2 && v2.customers) || {};
    const topRows = asArr(customers.top_rows);
    const declineRows = asArr(customers.decliners);
    const topTbody = document.getElementById("v2TopCustomersRows");
    const decTbody = document.getElementById("v2DeclinerRows");
    if (topTbody) {
      if (!topRows.length) {
        topTbody.innerHTML = '<tr><td colspan="7" class="text-muted text-center">No customer data in current selection.</td></tr>';
      } else {
        topTbody.innerHTML = topRows.slice(0, 30).map((r) => `
          <tr>
            <td>${renderCustomerCell(r, [r.delta_revenue != null ? `Δ ${fmtMoney0(r.delta_revenue)}` : "", r.last_order_date ? `Last order ${r.last_order_date}` : ""])}</td>
            <td class="text-end">${fmtMoney0(r.revenue)}</td>
            <td class="text-end">${fmtPct(r.revenue_share_pct)}</td>
            <td class="text-end">${showCosts ? fmtMoney0(r.profit) : "—"}</td>
            <td class="text-end">${showCosts ? fmtPct(r.margin_pct) : "—"}</td>
            <td class="text-end">${fmtInt(r.orders)}</td>
            <td>${escapeHtml(r.last_order_date || "")}</td>
          </tr>
        `).join("");
      }
    }
    if (decTbody) {
      if (!declineRows.length) {
        decTbody.innerHTML = '<tr><td colspan="4" class="text-muted text-center">No customer declines detected in the selected window.</td></tr>';
      } else {
        decTbody.innerHTML = declineRows.slice(0, 30).map((r) => `
          <tr>
            <td>${renderCustomerCell(r, [r.last_order_date ? `Last order ${r.last_order_date}` : ""])}</td>
            <td class="text-end">${fmtMoney0(r.revenue_current)}</td>
            <td class="text-end">${fmtMoney0(r.revenue_prior)}</td>
            <td class="text-end">${fmtMoney0(r.delta_revenue)}</td>
          </tr>
        `).join("");
      }
    }

    const mix = (v2 && v2.mix) || {};
    const custConc = mix.customer_concentration || {};
    const summary = customers.summary || {};
    const repeatShare = summary.repeat_customer_revenue_share_pct;
    const newShare = summary.new_customer_revenue_share_pct;

    setText("kpiCustomerTop10", fmtPct(custConc.top10_share));
    setText("kpiRepeatShare", fmtPct(repeatShare));
    setText("kpiNewShare", fmtPct(newShare));
    setText("kpiDeclinerCount", fmtInt(summary.decliner_count ?? declineRows.length));

    const note = document.getElementById("v2CustomerSummaryNote");
    if (note) {
      note.textContent = customers.narrative || (!topRows.length
        ? "No customer dependency issues detected for the selected window."
        : (summary.decliner_count ?? declineRows.length) > 0
          ? "Declining customers detected. Review recent service and pricing changes for at-risk accounts."
          : "Customer base is stable under the current scope.");
    }
  };

  const renderConcentrationSummary = (v2) => {
    const mix = (v2 && v2.mix) || {};
    const score = (v2 && v2.scorecard) || {};
    const custConc = mix.customer_concentration || {};
    const skuConc = mix.product_concentration || {};

    setHtml(
      "v2ConcentrationSummary",
      [
        `<li>Customer HHI <strong>${fmtNum2Safe(custConc.hhi)}</strong> · Top 1/5/10 share <strong>${fmtPct(custConc.top1_share)} / ${fmtPct(custConc.top5_share)} / ${fmtPct(custConc.top10_share)}</strong></li>`,
        `<li>SKU HHI <strong>${fmtNum2Safe(skuConc.hhi)}</strong> · Top 1/5/10 share <strong>${fmtPct(skuConc.top1_share)} / ${fmtPct(skuConc.top5_share)} / ${fmtPct(skuConc.top10_share)}</strong></li>`,
        `<li>80% revenue breadth: <strong>${fmtInt(custConc.customers_for_80_pct)}</strong> customers · <strong>${fmtInt(skuConc.skus_for_80_pct)}</strong> SKUs</li>`,
        `<li>Top protein <strong>${escapeHtml(score.top_protein || "—")}</strong> carries <strong>${fmtPct(score.top_protein_share_pct)}</strong> of supplier revenue</li>`,
      ].join("")
    );
  };

  const renderProductDiagnostics = (v2) => {
    const rows = asArr(((v2 && v2.products_table) || {}).rows).slice(0, 12);
    const tbody = document.getElementById("v2ProductDiagRows");
    if (!tbody) return;
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="4" class="text-center text-muted">No product diagnostics in selected window.</td></tr>';
      return;
    }
    tbody.innerHTML = rows.map((r) => {
      return `
        <tr>
          <td>${renderProductCell(r, [r.protein_family || "", r.product_category || "", r.last_sold ? `Last sold ${r.last_sold}` : ""])}</td>
          <td class="text-end">${fmtMoney0(r.revenue)}</td>
          <td class="text-end">${showCosts ? fmtPct(r.margin_pct) : "—"}</td>
          <td>${renderSignalBadges(r.tags)}</td>
        </tr>
      `;
    }).join("");
  };

  const productSort = (rows) => {
    const dir = state.sortDir === "asc" ? 1 : -1;
    const key = state.sortBy;
    return rows.slice().sort((a, b) => {
      const va = a[key];
      const vb = b[key];
      const na = Number(va);
      const nb = Number(vb);
      if (Number.isFinite(na) && Number.isFinite(nb)) return (na - nb) * dir;
      return String(va ?? "").localeCompare(String(vb ?? "")) * dir;
    });
  };

  const renderProducts = (v2) => {
    const table = (v2 && v2.products_table) || {};
    const allRows = Array.isArray(table.rows) ? table.rows : [];
    state.productsRows = allRows;
    const token = (state.search || "").toLowerCase();
    let rows = allRows;
    if (state.proteinFocus) {
      rows = rows.filter((r) => String(r.protein_family || "").toLowerCase() === String(state.proteinFocus || "").toLowerCase());
    }
    if (state.tagFocus) {
      rows = rows.filter((r) => Array.isArray(r.tags) && r.tags.some((tag) => String(tag || "").toLowerCase() === String(state.tagFocus || "").toLowerCase()));
    }
    if (token) {
      rows = rows.filter((r) => {
        const text = `${r.sku || r.product_id || ""} ${r.product_name || ""} ${r.display_name || ""} ${r.protein_family || ""} ${r.product_category || ""}`.toLowerCase();
        return text.includes(token);
      });
    }
    rows = productSort(rows);
    const tbody = document.getElementById("v2ProductsRows");
    const countEl = document.getElementById("v2ProductResultCount");
    const filterState = document.getElementById("v2ProductFilterState");
    if (countEl) countEl.textContent = `${fmtInt(rows.length)} products`;
    if (filterState) {
      const scopes = [];
      if (state.proteinFocus) scopes.push(`${state.proteinFocus} protein`);
      if (state.tagFocus) scopes.push(state.tagFocus);
      filterState.textContent = scopes.length
        ? `Focused on ${scopes.join(" · ")} inside the active supplier scope.`
        : "Showing all visible supplier SKUs in the active filter window.";
    }
    if (!tbody) return;
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="19" class="text-muted text-center">No products in current selection.</td></tr>';
      return;
    }
    tbody.innerHTML = rows.slice(0, 500).map((r) => {
      return `
        <tr>
          <td>${renderProductCell(r, [r.sku ? `SKU ${r.sku}` : "", r.top_customer_name ? `Top customer ${r.top_customer_name}` : "", r.revenue_share_pct != null ? `${fmtPct(r.revenue_share_pct)} share` : ""])}</td>
          <td>${escapeHtml(r.protein_family || "Unassigned")}</td>
          <td>${escapeHtml(r.product_category || "Unassigned")}</td>
          <td class="text-end">${fmtMoney0(r.revenue)}</td>
          <td class="text-end">${showCosts ? fmtMoney0(r.profit) : "—"}</td>
          <td class="text-end">${showCosts ? renderColorMetric(r.margin_pct, r.status_color, fmtPct) : "—"}</td>
          <td class="text-end">${showCosts ? fmtPct(r.target_margin_pct) : "—"}</td>
          <td class="text-end">${fmtInt(r.orders)}</td>
          <td class="text-end">${fmtInt(r.units)}</td>
          <td class="text-end">${fmtInt(r.weight_lb)}</td>
          <td class="text-end">${fmtMoney2(r.asp_lb)}</td>
          <td class="text-end">${showCosts ? fmtMoney2(r.minimum_price_lb) : "—"}</td>
          <td class="text-end">${showCosts ? fmtMoney2(r.target_price_lb) : "—"}</td>
          <td class="text-end">${showCosts ? renderColorMetric(r.asp_lb_gap_to_target, r.status_color, fmtSignedMoney2) : "—"}</td>
          <td class="text-end">${fmtPct(r.asp_lb_delta_pct)}</td>
          <td class="text-end">${fmtPct(r.revenue_share_pct)}</td>
          <td>${escapeHtml(r.last_sold || "")}</td>
          <td>${renderStatusBadge(r.target_status, r.status_color)}</td>
          <td>${renderSignalBadges(r.tags)}</td>
        </tr>
      `;
    }).join("");
  };

  const renderPlaybook = (v2) => {
    const playbook = (v2 && v2.playbook) || {};
    const wrap = document.getElementById("v2ActionCards");
    const summary = document.getElementById("v2ActionSummary");
    if (summary) summary.textContent = playbook.narrative || "Action guidance will populate after the supplier bundle loads.";
    if (!wrap) return;
    const cards = asArr(playbook.cards);
    if (!cards.length) {
      wrap.innerHTML = '<div class="supplier-v2-empty-copy">No immediate action lanes are flagged beyond routine supplier monitoring.</div>';
      return;
    }
    wrap.innerHTML = cards
      .map(
        (card) => `
        <article class="supplier-v2-action-card is-${escapeHtml(card.tone || "accent")}" data-action-target="${escapeHtml(card.target || "")}" data-tag-focus="${escapeHtml(card.tag || "")}" data-protein-focus="${escapeHtml(card.protein_family || "")}">
          <div class="supplier-v2-action-card-head">
            <div class="supplier-v2-action-card-title">${escapeHtml(card.title || "Action")}</div>
            <div class="supplier-v2-action-card-exposure">${escapeHtml(card.exposure || "")}</div>
          </div>
          <div class="supplier-v2-action-card-body">${escapeHtml(card.body || "")}</div>
          <div class="supplier-v2-action-card-link">${escapeHtml(card.target === "protein" ? "Focus protein mix" : card.target === "customers" ? "Open customer layer" : "Open related products")}</div>
        </article>
      `
      )
      .join("");
  };

  const renderInsights = (v2) => {
    const header = (v2 && v2.header) || {};
    const mix = (v2 && v2.mix) || {};
    const opportunities = (v2 && v2.opportunities) || {};
    const playbook = (v2 && v2.playbook) || {};
    const pricing = (v2 && v2.pricing) || {};
    const customers = (v2 && v2.customers) || {};

    const changed = header.narrative || "Supplier detail narrative will populate after scoped metrics load.";
    const risk = `${mix.narrative || "Concentration diagnostics pending."} ${pricing.narrative || ""}`.trim();

    const actions = asArr(playbook.actions).filter((x) => String(x || "").trim());
    const actionText = actions.length
      ? actions.slice(0, 2).join(" ")
      : customers.narrative || "Prioritize margin fixes on high-velocity low-margin SKUs and recover declining customers.";

    setText("v2InsightChanged", changed);
    setText("v2InsightRisk", risk);
    setText("v2InsightAction", actionText);
  };

  const render = (payload) => {
    const v2 = payload?.supplier_v2 || payload?.v2 || {};
    state.currentV2 = v2 || {};
    const payloadError = payload?.error?.message || "";
    if (payloadError) {
      showPageNotice("Supplier detail notice", payloadError, "warn", true);
    } else {
      hidePageNotice();
    }
    renderHeader(v2);
    renderScorecard(v2);
    renderNavBadges(v2);
    renderTrend(v2);
    renderTopBars(v2);
    renderProteinSection(v2);
    renderDistributions(v2);
    renderPriceVelocity(v2);
    renderOutliers(v2);
    renderMarginRisk(v2);
    renderCustomers(v2);
    renderConcentrationSummary(v2);
    renderProductDiagnostics(v2);
    renderProducts(v2);
    renderPlaybook(v2);
    renderInsights(v2);
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
    const params = buildBundleParams();
    const bundleKey = params.toString();
    const hasPayload = state.currentV2 && Object.keys(state.currentV2).length > 0;
    if (bundleKey === state.lastBundleKey && hasPayload) return;
    state.lastBundleKey = bundleKey;
    if (state.activeFetchController && typeof state.activeFetchController.abort === "function") {
      state.activeFetchController.abort();
    }
    const controller = typeof AbortController !== "undefined" ? new AbortController() : null;
    state.activeFetchController = controller;
    state.loading = true;
    try {
      const requestOptions = { headers: { Accept: "application/json" } };
      if (controller) requestOptions.signal = controller.signal;
      const res = await authFetch(`${bundleUrl}?${bundleKey}`, requestOptions);
      if (!res.ok) {
        let message = `Supplier detail request failed (${res.status})`;
        try {
          const text = await res.text();
          if (text) {
            try {
              const parsed = JSON.parse(text);
              message = parsed?.error?.message || parsed?.message || message;
            } catch (_err) {
              message = text.slice(0, 240) || message;
            }
          }
        } catch (_err) {
          // no-op
        }
        showPageNotice("Supplier detail unavailable", message, res.status >= 500 ? "risk" : "warn", true);
        state.lastBundleKey = "";
        return;
      }
      const payload = await res.json();
      hidePageNotice();
      render(payload || {});
    } catch (err) {
      if (err && err.name === "AbortError") return;
      showPageNotice("Supplier detail refresh failed", err?.message || "The supplier detail bundle could not be refreshed.", "risk", true);
      state.lastBundleKey = "";
    } finally {
      if (requestSeq !== state.requestSeq) return;
      if (state.activeFetchController === controller) {
        state.activeFetchController = null;
      }
      state.loading = false;
      dispatchGlobalApplyAck({ qs: state.filterQs });
    }
  };

  const bindTableControls = () => {
    const search = document.getElementById("v2ProductSearch");
    if (search) {
      search.addEventListener("input", () => {
        state.search = search.value || "";
        renderProducts(state.currentV2 || {});
      });
    }
    document.querySelectorAll("#v2ProductsTable thead th.sortable").forEach((th) => {
      th.addEventListener("click", () => {
        const key = th.getAttribute("data-sort");
        if (!key) return;
        if (state.sortBy === key) {
          state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
        } else {
          state.sortBy = key;
          state.sortDir = "desc";
        }
        renderProducts(state.currentV2 || {});
      });
    });
    const retryButton = document.getElementById("v2RetryButton");
    if (retryButton) {
      retryButton.addEventListener("click", () => {
        state.lastBundleKey = "";
        fetchBundle();
      });
    }
  };

  const bindInteractiveFilters = () => {
    const root = document.querySelector(".supplier-drilldown-v2");
    if (!root) return;
    root.addEventListener("click", (evt) => {
      const actionTarget = evt.target.closest(".supplier-v2-action-card");
      if (actionTarget) {
        const protein = actionTarget.getAttribute("data-protein-focus") || "";
        const tag = actionTarget.getAttribute("data-tag-focus") || "";
        const sectionId = actionTarget.getAttribute("data-action-target") || "products";
        if (protein || tag) evt.preventDefault();
        state.proteinFocus = protein;
        state.tagFocus = tag;
        if (protein || tag) {
          renderProducts(state.currentV2 || {});
          renderProteinSection(state.currentV2 || {});
        }
        const targetSection = document.getElementById(sectionId === "pricing" ? "sec-products" : `sec-${sectionId}`) || document.getElementById("sec-products");
        if (targetSection) targetSection.scrollIntoView({ behavior: "smooth", block: "start" });
        return;
      }

      const proteinTarget = evt.target.closest("[data-protein-focus]");
      if (proteinTarget) {
        evt.preventDefault();
        state.proteinFocus = proteinTarget.getAttribute("data-protein-focus") || "";
        state.tagFocus = "";
        renderProducts(state.currentV2 || {});
        renderProteinSection(state.currentV2 || {});
        return;
      }
    });
  };

  const applyFilters = (qs) => {
    const normalized = normalizeQs(qs);
    if (normalized === state.filterQs) {
      dispatchGlobalApplyAck({ qs: state.filterQs });
      return;
    }
    state.filterQs = normalized;
    state.proteinFocus = "";
    state.tagFocus = "";
    bindExportLinks();
    state.lastBundleKey = "";
    fetchBundle();
  };

  const onGlobalFiltersApply = (evt) => {
    currentApplyId = String(evt?.detail?.applyId || "");
    const qs = (evt?.detail && evt.detail.qs) || "";
    applyFilters(qs);
  };
  window.addEventListener("globalFilters:apply", onGlobalFiltersApply);

  const teardown = () => {
    Object.keys(state.charts).forEach((name) => destroyChart(name));
    if (state.sectionObserver && typeof state.sectionObserver.disconnect === "function") {
      state.sectionObserver.disconnect();
      state.sectionObserver = null;
    }
    window.removeEventListener("globalFilters:apply", onGlobalFiltersApply);
  };
  window.addEventListener("pagehide", teardown, { once: true });

  bindTableControls();
  bindInteractiveFilters();
  bindSectionNav();
  state.filterQs = normalizeQs(state.filterQs);
  if (initialPageNotice) {
    showPageNotice("Supplier detail notice", initialPageNotice, "warn", true);
  }
  render(initialPayload || {});
  bindExportLinks();
  fetchBundle();
})();
