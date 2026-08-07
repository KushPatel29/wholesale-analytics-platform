(() => {
  const root = document.getElementById("SuppliersV2App");
  if (!root || root.dataset.bound === "1") return;
  root.dataset.bound = "1";

  const authFetch = window.authFetch || fetch;
  const pageCache = window.analyticsPageCache || null;
  const bundleUrl = root.dataset.bundleUrl || "/api/suppliers/bundle";
  const exportCsvUrl = root.dataset.exportCsvUrl || "/api/suppliers/export.csv";
  const exportXlsxUrl = root.dataset.exportXlsxUrl || "/api/suppliers/export.xlsx";
  const PAGE_CACHE_ID = "suppliers";
  const PAGE_CACHE_POLICY = { freshMs: 90 * 1000, maxAgeMs: 20 * 60 * 1000 };
  const showCosts = (() => {
    try {
      return JSON.parse(root.dataset.showCosts || "true") !== false;
    } catch (_err) {
      return true;
    }
  })();

  const state = {
    page: 1,
    pageSize: 50,
    sortBy: "revenue_current",
    sortDir: "desc",
    search: "",
    quickFilter: "all",
    proteinFilter: "",
    filterQs: (window.location.search || "").replace(/^\?/, ""),
    loading: false,
    totalRows: 0,
    requestId: 0,
  };
  let currentApplyId = "";
  let bootstrapped = false;
  let lastPayload = null;

  const QUICK_FILTER_LABELS = {
    all: "All suppliers",
    strategic: "Strategic suppliers",
    growth: "Growth suppliers",
    margin_risk: "Margin-risk suppliers",
    data_risk: "Data-risk suppliers",
    high_concentration: "Highly concentrated suppliers",
    below_target_margin: "Below-target suppliers",
    missing_cost: "Missing-cost suppliers",
    at_risk: "Inactive 90+ day suppliers",
    new: "New suppliers",
    lost: "Lost suppliers",
    long_tail: "Long-tail suppliers",
  };

  const els = {
    windowSummary: document.getElementById("suppliersWindowSummary"),
    windowNote: document.getElementById("suppliersWindowNote"),
    comparisonSummary: document.getElementById("suppliersComparisonSummary"),
    comparisonNote: document.getElementById("suppliersComparisonNote"),
    narrative: document.getElementById("supplierNarrative"),
    coverageSummary: document.getElementById("supplierCoverageSummary"),
    moversSummary: document.getElementById("supplierTopMoversSummary"),
    chips: document.getElementById("supplierHealthChips"),
    postureCards: document.getElementById("supplierPostureCards"),
    postureNarrative: document.getElementById("supplierPostureNarrative"),
    commandHeadline: document.getElementById("supplierCommandHeadline"),
    commandNote: document.getElementById("supplierCommandNote"),
    topGainer: document.getElementById("supplierTopGainer"),
    topDecliner: document.getElementById("supplierTopDecliner"),
    topProtein: document.getElementById("supplierTopProtein"),
    atRiskSignal: document.getElementById("supplierAtRiskSignal"),
    coverageState: document.getElementById("supplierCoverageState"),
    heroSecondaryLabel: document.getElementById("supplierHeroSecondaryLabel"),
    heroRevenueValue: document.getElementById("supplierHeroRevenueValue"),
    heroRevenueMeta: document.getElementById("supplierHeroRevenueMeta"),
    heroSecondaryValue: document.getElementById("supplierHeroSecondaryValue"),
    heroSecondaryMeta: document.getElementById("supplierHeroSecondaryMeta"),
    heroSuppliersValue: document.getElementById("supplierHeroSuppliersValue"),
    heroSuppliersMeta: document.getElementById("supplierHeroSuppliersMeta"),
    heroConcentrationValue: document.getElementById("supplierHeroConcentrationValue"),
    heroConcentrationMeta: document.getElementById("supplierHeroConcentrationMeta"),
    kpiRevenue: document.getElementById("kpiRevenue"),
    kpiRevenueDelta: document.getElementById("kpiRevenueDelta"),
    kpiProfit: document.getElementById("kpiProfit"),
    kpiProfitDelta: document.getElementById("kpiProfitDelta"),
    kpiMargin: document.getElementById("kpiMargin"),
    kpiMarginDelta: document.getElementById("kpiMarginDelta"),
    kpiActiveSuppliers: document.getElementById("kpiActiveSuppliers"),
    kpiActiveSuppliersMeta: document.getElementById("kpiActiveSuppliersMeta"),
    kpiTopShare: document.getElementById("kpiTopShare"),
    kpiTopShareMeta: document.getElementById("kpiTopShareMeta"),
    kpiCostCoverage: document.getElementById("kpiCostCoverage"),
    kpiCostCoverageMeta: document.getElementById("kpiCostCoverageMeta"),
    kpiRevenueAtRisk: document.getElementById("kpiRevenueAtRisk"),
    kpiRevenueAtRiskMeta: document.getElementById("kpiRevenueAtRiskMeta"),
    kpiConcentration: document.getElementById("kpiConcentration"),
    kpiConcentrationMeta: document.getElementById("kpiConcentrationMeta"),
    kpiNewLost: document.getElementById("kpiNewLost"),
    kpiNewLostMeta: document.getElementById("kpiNewLostMeta"),
    kpiStrategic: document.getElementById("kpiStrategic"),
    kpiStrategicMeta: document.getElementById("kpiStrategicMeta"),
    strategicReadout: document.getElementById("strategicReadout"),
    opportunityNarrative: document.getElementById("supplierOpportunityNarrative"),
    proteinNarrative: document.getElementById("supplierProteinNarrative"),
    proteinFocusNarrative: document.getElementById("supplierProteinFocusNarrative"),
    dependencyNarrative: document.getElementById("supplierDependencyNarrative"),
    riskNarrative: document.getElementById("supplierRiskNarrative"),
    segmentNarrative: document.getElementById("supplierSegmentNarrative"),
    moverNarrative: document.getElementById("supplierMoverNarrative"),
    concentrationSummary: document.getElementById("concentrationSummary"),
    actionCards: document.getElementById("supplierActionCards"),
    proteinHighlights: document.getElementById("supplierProteinHighlights"),
    proteinChips: document.getElementById("supplierProteinChips"),
    proteinFocus: document.getElementById("supplierProteinFocus"),
    proteinMixRows: document.getElementById("proteinMixRows"),
    marginLeakageRows: document.getElementById("marginLeakageRows"),
    dataRiskRows: document.getElementById("dataRiskRows"),
    dependencyRows: document.getElementById("dependencyRows"),
    moversRows: document.getElementById("moversRows"),
    segmentSummaryRows: document.getElementById("segmentSummaryRows"),
    tableBody: document.getElementById("supV2TableBody"),
    tableStatus: document.getElementById("supV2TableStatus"),
    tableNarrative: document.getElementById("supplierTableNarrative"),
    loadMore: document.getElementById("supV2LoadMore"),
    resetProtein: root.querySelector(".supplier-protein-reset"),
    pageSize: document.getElementById("supV2PageSize"),
    search: document.getElementById("supV2Search"),
    clearSearch: document.getElementById("supV2SearchClear"),
    exportTableCsv: document.getElementById("supV2ExportCsv"),
    exportTableXlsx: document.getElementById("supV2ExportXlsx"),
    exportProtein: document.getElementById("supV2ExportProtein"),
    exportActions: document.getElementById("supV2ExportActions"),
  };

  const nfInt = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
  const nfMoney0 = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 0 });
  const nfMoney2 = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const nfPct1 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
  const dfLong = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" });

  const asNum = (value, fallback = null) => {
    const num = Number(value);
    return Number.isFinite(num) ? num : fallback;
  };
  const asArr = (value) => (Array.isArray(value) ? value : []);
  const money0 = (v) => (v == null || Number.isNaN(Number(v)) ? "—" : nfMoney0.format(Number(v)));
  const money2 = (v) => (v == null || Number.isNaN(Number(v)) ? "—" : nfMoney2.format(Number(v)));
  const int0 = (v) => (v == null || Number.isNaN(Number(v)) ? "—" : nfInt.format(Number(v)));
  const pct1 = (v) => (v == null || Number.isNaN(Number(v)) ? "—" : `${nfPct1.format(Number(v))}%`);
  const signedMoney0 = (v) => {
    const numeric = asNum(v, null);
    if (numeric === null) return "—";
    return `${numeric > 0 ? "+" : numeric < 0 ? "-" : ""}${money0(Math.abs(numeric))}`;
  };
  const signedPct1 = (v) => {
    const numeric = asNum(v, null);
    if (numeric === null) return "—";
    return `${numeric > 0 ? "+" : numeric < 0 ? "-" : ""}${nfPct1.format(Math.abs(numeric))}%`;
  };
  const signedPoints = (v) => {
    const numeric = asNum(v, null);
    if (numeric === null) return "";
    return `${numeric > 0 ? "+" : ""}${nfPct1.format(numeric)} pts`;
  };
  const pluralize = (value, one, many = `${one}s`) => {
    const count = asNum(value, null);
    return count === 1 ? one : many;
  };
  const formatDate = (value) => {
    if (!value) return "";
    try {
      const raw = String(value);
      const parsed = /^\d{4}-\d{2}-\d{2}$/.test(raw) ? new Date(`${raw}T00:00:00`) : new Date(raw);
      if (Number.isNaN(parsed.getTime())) return String(value);
      return dfLong.format(parsed);
    } catch (_err) {
      return String(value);
    }
  };
  const formatRange = (start, end) => {
    if (!start && !end) return "Live filter scope";
    if (start && end) return `${formatDate(start)} - ${formatDate(end)}`;
    return formatDate(start || end);
  };
  const escapeHtml = (value) =>
    String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  const marginStatusKey = (value) => String(value || "").trim().toLowerCase();
  const marginStatusClass = (value) => {
    const key = marginStatusKey(value);
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
    if (row.target_margin_pct != null) parts.push(`Target ${pct1(row.target_margin_pct)}`);
    if (row.minimum_margin_pct != null) parts.push(`Min ${pct1(row.minimum_margin_pct)}`);
    if (row.target_gap_pct_points != null) {
      parts.push(`${signedPoints(row.target_gap_pct_points)} vs target`);
    } else if (row.target_status) {
      parts.push(row.target_status);
    }
    return parts.join(" · ");
  };
  const marginCellHtml = (row = {}) => {
    const status = marginStatusLabel(row);
    const context = marginContextText(row);
    const pill = status
      ? `<span class="suppliers-status-pill ${marginStatusClass(row.status_key)}">${escapeHtml(status)}</span>`
      : "";
    return `
      <div class="suppliers-metric-stack suppliers-metric-stack-end">
        <div>${showCosts ? pct1(row.margin_pct) : "—"}</div>
        ${showCosts && (context || pill) ? `<div class="suppliers-metric-sub">${pill}${context ? `${pill ? " " : ""}<span>${escapeHtml(context)}</span>` : ""}</div>` : ""}
      </div>
    `;
  };
  const setStatusBadge = (el, text, tone) => {
    if (!el) return;
    el.textContent = text;
    el.classList.remove("is-healthy", "is-watch", "is-risk");
    if (tone) el.classList.add(`is-${tone}`);
  };
  const coverageTone = (value) => {
    const token = String(value || "").trim().toLowerCase();
    if (token === "healthy") return "healthy";
    if (token === "risk") return "risk";
    return "watch";
  };
  const buildExecutiveHeadline = (summary = {}, kpis = {}) => {
    const delta = asNum(kpis.revenue_delta, null);
    const deltaPct = asNum(kpis.revenue_delta_pct, null);
    const activeSuppliers = int0(kpis.active_suppliers);
    if (delta != null) {
      const direction = delta >= 0 ? "up" : "down";
      const deltaText = deltaPct != null ? `${money0(Math.abs(delta))} (${nfPct1.format(Math.abs(deltaPct))}%)` : money0(Math.abs(delta));
      return `Revenue ${direction} ${deltaText} vs prior across ${activeSuppliers} active suppliers.`;
    }
    return summary.narrative || "Supplier portfolio summary is loading.";
  };

  const currentFilterState = () => {
    try {
      const globalState = window.getGlobalFilterState ? window.getGlobalFilterState() : {};
      if (globalState?.filters && typeof globalState.filters === "object") return globalState.filters;
    } catch (_err) {
      /* ignore */
    }
    return {};
  };

  const waitForFiltersReady = async () => {
    const fallbackState = () => {
      try {
        return (window.getGlobalFilterState && window.getGlobalFilterState()) || {};
      } catch (_err) {
        return {};
      }
    };
    if (window.filtersReady && typeof window.filtersReady.then === "function") {
      try {
        const timeout = new Promise((resolve) => setTimeout(() => resolve(fallbackState()), 1500));
        return await Promise.race([window.filtersReady, timeout]);
      } catch (_err) {
        return fallbackState();
      }
    }
    return fallbackState();
  };

  const supplierPayload = (supplierId, supplierLabel, section, widget, metric, value, extra = {}) => {
    const cleanId = String(supplierId || "").trim();
    if (!cleanId) return null;
    return {
      source_page: "suppliers",
      source_section: section,
      source_widget: widget,
      requested_target: "supplier",
      clicked_entity_type: "supplier",
      clicked_entity_id: cleanId,
      clicked_entity_label: String(supplierLabel || cleanId),
      clicked_metric: metric,
      clicked_metric_value: value,
      active_filter_state: currentFilterState(),
      extra,
    };
  };

  const drillAttr = (payload) => {
    if (!payload) return "";
    return ` data-drilldown-payload="${escapeHtml(JSON.stringify(payload))}"`;
  };

  const riskToneClass = (value) => {
    const token = String(value || "").toLowerCase();
    if (token === "high" || token === "risk") return "risk-high";
    if (token === "medium" || token === "watch") return "risk-medium";
    if (token === "low" || token === "healthy") return "risk-low";
    return "";
  };

  const setText = (el, text) => {
    if (el) el.textContent = text;
  };

  const renderCallouts = (items) => {
    if (!els.strategicReadout) return;
    if (!Array.isArray(items) || !items.length) {
      els.strategicReadout.innerHTML = '<div class="suppliers-callout-item">No supplier portfolio summary available.</div>';
      return;
    }
    els.strategicReadout.innerHTML = items
      .map((item) => `<div class="suppliers-callout-item">${item}</div>`)
      .join("");
  };

  const renderPostureCards = (posture = {}) => {
    if (!els.postureCards) return;
    const cards = asArr(posture.cards);
    if (!cards.length) {
      els.postureCards.innerHTML = `
        <div class="suppliers-posture-card">
          <div class="suppliers-posture-label">Posture</div>
          <div class="suppliers-posture-value">No supplier posture available</div>
          <div class="suppliers-posture-note">Portfolio shape, activity rhythm, protein exposure, and trust will appear here when supplier data is available.</div>
        </div>`;
    } else {
      els.postureCards.innerHTML = cards
        .map((card) => `
          <div class="suppliers-posture-card is-${escapeHtml(card.tone || "neutral")}">
            <div class="suppliers-posture-label">${escapeHtml(card.label || "Posture")}</div>
            <div class="suppliers-posture-value">${escapeHtml(card.value || "—")}</div>
            <div class="suppliers-posture-note">${escapeHtml(card.note || "")}</div>
            <div class="suppliers-posture-meta">${escapeHtml(card.meta || "")}</div>
          </div>
        `)
        .join("");
    }
    setText(els.postureNarrative, posture.narrative || "No supplier posture summary available.");
  };

  const buildParams = ({ includePage = true } = {}) => {
    const params = new URLSearchParams(state.filterQs || "");
    params.set("suppliers_v2", "1");
    params.set("sort", state.sortBy);
    params.set("sort_dir", state.sortDir);
    params.set("quick_filter", state.quickFilter);
    if (state.search) params.set("search", state.search);
    if (state.proteinFilter) params.set("protein", state.proteinFilter);
    if (includePage) {
      params.set("page", String(state.page));
      params.set("page_size", String(state.pageSize));
    }
    return params;
  };

  const updateExportLinks = () => {
    const base = buildParams({ includePage: false });
    const setHref = (el, url, scope) => {
      if (!el) return;
      const p = new URLSearchParams(base.toString());
      p.set("scope", scope);
      el.href = `${url}?${p.toString()}`;
    };
    setHref(els.exportTableCsv, exportCsvUrl, "table");
    setHref(els.exportTableXlsx, exportXlsxUrl, "table");
    setHref(els.exportProtein, exportCsvUrl, "protein");
    setHref(els.exportActions, exportCsvUrl, "actions");
  };

  const renderChips = (kpis = {}, summary = {}) => {
    if (!els.chips) return;
    const chips = [
      `Top supplier share ${pct1(kpis.concentration_top1_share)}`,
      `Top 5 share ${pct1(kpis.concentration_top5_share)}`,
      `80% of revenue sits in ${int0(kpis.suppliers_for_80_pct)} suppliers`,
      `Missing-cost revenue ${money0(kpis.missing_cost_revenue)}`,
      `At-risk suppliers ${int0(summary.at_risk_suppliers)}`,
      `Leading protein ${(summary.top_protein_family || "—")} ${pct1(summary.top_protein_share_pct)}`,
    ];
    els.chips.innerHTML = chips.map((text) => `<span class="suppliers-chip">${escapeHtml(text)}</span>`).join("");
  };

  const renderHeader = (payload) => {
    const summary = payload.executive_summary || {};
    const kpis = payload.kpis || {};
    const comparison = summary.comparison || {};
    const topGainers = asArr(summary.top_gainers);
    const topDecliners = asArr(summary.top_decliners);
    const topGainer = topGainers[0] || {};
    const topDecliner = topDecliners[0] || {};
    const coverageStatus = summary.coverage_status || "Watch";
    const currentDays = asNum(comparison.current_days, null);
    const priorDays = asNum(comparison.prior_days, null);

    setText(els.windowSummary, formatRange(comparison.current_start, comparison.current_end));
    setText(
      els.windowNote,
      [
        comparison.current_label || "Current filtered window",
        currentDays != null && currentDays > 0 ? `${int0(currentDays)} ${pluralize(currentDays, "day")}` : "",
      ]
        .filter(Boolean)
        .join(" · ") || "Current supplier window follows the active global filters."
    );
    setText(
      els.comparisonSummary,
      comparison.prior_start || comparison.prior_end
        ? formatRange(comparison.prior_start, comparison.prior_end)
        : "Waiting for prior comparable window"
    );
    setText(
      els.comparisonNote,
      comparison.prior_start || comparison.prior_end
        ? [
            comparison.prior_label || "Prior comparable window",
            priorDays != null && priorDays > 0 ? `${int0(priorDays)} ${pluralize(priorDays, "day")}` : "",
          ]
            .filter(Boolean)
            .join(" · ")
        : comparison.note || "Prior comparable window appears when enough history is available."
    );

    setText(els.narrative, summary.narrative || kpis.narrative || "Supplier narrative unavailable.");
    setText(
      els.coverageSummary,
      `${pct1(summary.cost_coverage_pct)} of supplier revenue is cost-covered, leaving ${money0(summary.missing_cost_revenue)} without reliable cost support.`
    );
    setText(
      els.moversSummary,
      `Top gainers: ${topGainers.slice(0, 2).map((row) => row.supplier_name || "n/a").join(", ") || "n/a"}. Top decliners: ${topDecliners.slice(0, 2).map((row) => row.supplier_name || "n/a").join(", ") || "n/a"}. ${summary.top_protein_family ? `${summary.top_protein_family} leads mix at ${pct1(summary.top_protein_share_pct)}.` : ""}`.trim()
    );

    setText(els.commandHeadline, buildExecutiveHeadline(summary, kpis));
    setText(
      els.commandNote,
      [
        comparison.comparison_label || "Current window vs prior comparable",
        `${int0(kpis.active_suppliers)} active suppliers`,
        `${pct1(kpis.concentration_top5_share)} revenue in the top 5 suppliers`,
      ].join(" · ")
    );
    setText(els.topGainer, topGainer.supplier_name || "—");
    setText(els.topDecliner, topDecliner.supplier_name || "—");
    setText(els.topProtein, summary.top_protein_family || "—");
    setText(els.atRiskSignal, money0(summary.revenue_at_risk));
    setStatusBadge(els.coverageState, `${coverageStatus} trust`, coverageTone(coverageStatus));

    renderChips(kpis, summary);
  };

  const renderKpis = (kpis = {}) => {
    setText(els.heroRevenueValue, money0(kpis.total_revenue));
    setText(
      els.heroRevenueMeta,
      asNum(kpis.revenue_delta, null) != null
        ? `vs ${kpis.window?.prior_label || "prior"}: ${signedMoney0(kpis.revenue_delta)}${asNum(kpis.revenue_delta_pct, null) != null ? ` (${signedPct1(kpis.revenue_delta_pct)})` : ""}`
        : "Prior comparable window unavailable"
    );
    setText(els.heroSecondaryLabel, showCosts ? "Profit" : "Cost coverage");
    setText(els.heroSecondaryValue, showCosts ? money0(kpis.total_profit) : pct1(kpis.cost_coverage_pct));
    setText(
      els.heroSecondaryMeta,
      showCosts
        ? `${pct1(kpis.margin_pct)} margin${asNum(kpis.profit_delta, null) != null ? ` · ${signedMoney0(kpis.profit_delta)} vs prior` : ""}`
        : `${money0(kpis.missing_cost_revenue)} missing-cost revenue`
    );
    setText(els.heroSuppliersValue, int0(kpis.active_suppliers));
    setText(
      els.heroSuppliersMeta,
      `${int0(kpis.active_suppliers_30d)} active in 30d · ${int0(kpis.at_risk_suppliers)} inactive 90d+`
    );
    setText(els.heroConcentrationValue, pct1(kpis.concentration_top5_share));
    setText(
      els.heroConcentrationMeta,
      `Top supplier ${pct1(kpis.concentration_top1_share)} · ${int0(kpis.suppliers_for_80_pct)} suppliers reach 80%`
    );

    setText(els.kpiRevenue, money0(kpis.total_revenue));
    setText(
      els.kpiRevenueDelta,
      `vs ${kpis.window?.prior_label || "prior"}: ${signedMoney0(kpis.revenue_delta)}${asNum(kpis.revenue_delta_pct, null) != null ? ` (${signedPct1(kpis.revenue_delta_pct)})` : ""}`
    );
    setText(els.kpiProfit, showCosts ? money0(kpis.total_profit) : "—");
    setText(
      els.kpiProfitDelta,
      showCosts
        ? `vs ${kpis.window?.prior_label || "prior"}: ${signedMoney0(kpis.profit_delta)}`
        : "Costs hidden"
    );
    setText(els.kpiMargin, showCosts ? pct1(kpis.margin_pct) : "—");
    setText(
      els.kpiMarginDelta,
      showCosts ? (marginContextText(kpis) || `${signedPoints(kpis.margin_delta_pp)} vs prior`) : "Costs hidden"
    );
    setText(els.kpiActiveSuppliers, int0(kpis.active_suppliers));
    setText(els.kpiActiveSuppliersMeta, `${int0(kpis.active_suppliers_30d)} active in the last 30 days`);
    setText(els.kpiTopShare, pct1(kpis.concentration_top1_share));
    setText(els.kpiTopShareMeta, `Top 5 ${pct1(kpis.concentration_top5_share)} · Top 10 ${pct1(kpis.concentration_top10_share)}`);
    setText(els.kpiCostCoverage, pct1(kpis.cost_coverage_pct));
    setText(els.kpiCostCoverageMeta, `${money0(kpis.missing_cost_revenue)} missing-cost revenue · row coverage ${pct1(kpis.cost_coverage_row_pct)}`);
    setText(els.kpiRevenueAtRisk, money0(kpis.revenue_at_risk));
    setText(els.kpiRevenueAtRiskMeta, `${int0(kpis.at_risk_suppliers)} suppliers inactive for 90+ days`);
    setText(els.kpiConcentration, Number.isFinite(Number(kpis.concentration_hhi)) ? int0(kpis.concentration_hhi) : "—");
    setText(
      els.kpiConcentrationMeta,
      `${int0(kpis.suppliers_for_80_pct)} suppliers drive 80% of revenue · covered profit top 5 ${pct1(kpis.profit_concentration_top5_share)}`
    );
    setText(els.kpiNewLost, `${int0(kpis.new_suppliers)} / ${int0(kpis.lost_suppliers)}`);
    setText(els.kpiNewLostMeta, "New suppliers / lost suppliers in the comparable window");
    setText(els.kpiStrategic, `${int0(kpis.strategic_suppliers)} / ${int0(kpis.long_tail_suppliers)}`);
    setText(els.kpiStrategicMeta, `${int0(kpis.active_suppliers_30d)} active in 30d · ${int0(kpis.at_risk_suppliers)} inactive 90d+`);
  };

  const renderExecutiveReadout = (payload) => {
    const kpis = payload.kpis || {};
    const protein = payload.protein_intelligence || {};
    const dependency = payload.dependency || {};
    const actions = payload.actions || {};
    const segments = payload.segments || {};
    const risk = payload.risk_opportunities || {};
    const posture = payload.portfolio_posture || {};
    const topProtein = (protein.summary || {}).top_family;
    const cards = asArr(actions.cards);
    const topSegment = asArr(segments.summary)[0] || {};

    renderCallouts([
      `<strong>Commercial readout.</strong> ${int0(kpis.active_suppliers)} suppliers produced ${money0(kpis.total_revenue)} in revenue and ${showCosts ? `${money0(kpis.total_profit)} in covered profit` : "sales-only results"} under the active scope.`,
      `<strong>Concentration posture.</strong> Top supplier share is ${pct1(kpis.concentration_top1_share)} and the top five suppliers carry ${pct1(kpis.concentration_top5_share)} of visible revenue. ${int0((dependency.summary || {}).high_dependency_suppliers)} suppliers are materially concentrated in one protein or SKU family.`,
      `<strong>Portfolio shape.</strong> ${int0(topSegment.suppliers)} suppliers sit in ${topSegment.segment || "the leading"} segment, while ${int0(kpis.at_risk_suppliers)} suppliers have been inactive for at least 90 days. Protein leadership currently sits in ${topProtein || "the top visible family"}.`,
    ]);

    setText(els.proteinNarrative, protein.narrative || "Protein-family contribution summary unavailable.");
    setText(
      els.dependencyNarrative,
      `Revenue HHI ${int0((dependency.summary || {}).hhi)} · covered profit top 5 ${pct1((dependency.summary || {}).profit_top5_share)} · ${int0((dependency.summary || {}).suppliers_for_80_pct)} suppliers drive 80% of revenue.`
    );
    setText(
      els.riskNarrative,
      `${int0((risk.summary || {}).margin_risk_suppliers)} suppliers are below target margin across ${money0((risk.summary || {}).margin_risk_revenue)} of revenue. Missing-cost exposure totals ${money0((risk.summary || {}).missing_cost_revenue)}.`
    );
    const recover = cards.find((card) => card.key === "below_target_margin") || cards[1] || {};
    setText(
      els.opportunityNarrative,
      `${recover.title || "Recover margin"} covers ${int0(recover.supplier_count)} suppliers and ${money0(recover.revenue)} in revenue. Estimated profit uplift target is ${money0(recover.uplift_target)}.`
    );
    setText(
      els.segmentNarrative,
      `${int0(kpis.strategic_suppliers)} strategic suppliers anchor the portfolio while ${int0(kpis.long_tail_suppliers)} long-tail suppliers require lighter-touch management.`
    );
    renderPostureCards(posture);

    if (!els.actionCards) return;
    if (!cards.length) {
      els.actionCards.innerHTML = '<div class="suppliers-action-card"><div class="suppliers-action-label">Actions</div><div class="suppliers-action-value">No action signals</div><div class="suppliers-action-note">Action cards appear when supplier segments and risk signals are available.</div></div>';
      return;
    }
    els.actionCards.innerHTML = cards
      .map((card) => {
        const exposure = card.quick_filter === "below_target_margin" && card.uplift_target != null
          ? `Target uplift ${money0(card.uplift_target)}`
          : `${money0(card.revenue)} revenue in scope`;
        return `
          <button type="button" class="suppliers-action-card is-clickable is-${escapeHtml(card.tone || "neutral")} w-100 text-start" data-action-quick="${escapeHtml(card.quick_filter || "")}">
            <div class="suppliers-action-label">${escapeHtml(card.label || "Action")}</div>
            <div class="suppliers-action-value">${escapeHtml(card.title || "Action focus")}</div>
            <div class="suppliers-action-note">${escapeHtml(card.note || "")}</div>
            <div class="suppliers-action-note">${int0(card.supplier_count)} suppliers · ${exposure}${card.examples ? ` · ${escapeHtml(card.examples)}` : ""}</div>
          </button>
        `;
      })
      .join("");
  };

  const renderProteinHighlights = (protein = {}) => {
    if (!els.proteinHighlights) return;
    const cards = asArr(protein.focus_cards);
    if (!cards.length) {
      els.proteinHighlights.innerHTML = "";
      setText(els.proteinFocusNarrative, "Protein-family mix shift and dependency details will appear here when protein data is available.");
      return;
    }
    els.proteinHighlights.innerHTML = cards
      .map((card) => `
        <div class="suppliers-posture-card is-${escapeHtml(card.tone || "neutral")}">
          <div class="suppliers-posture-label">${escapeHtml(card.label || "Protein")}</div>
          <div class="suppliers-posture-value">${escapeHtml(card.value || "—")}</div>
          <div class="suppliers-posture-note">${escapeHtml(card.note || "")}</div>
        </div>
      `)
      .join("");

    const mixShift = asArr(protein.mix_shift);
    const gain = mixShift.find((row) => asNum(row.share_delta_pp, 0) > 0);
    const decline = [...mixShift]
      .sort((a, b) => asNum(a.share_delta_pp, 0) - asNum(b.share_delta_pp, 0))
      .find((row) => asNum(row.share_delta_pp, 0) < 0);
    setText(
      els.proteinFocusNarrative,
      `${gain ? `${gain.family} is gaining ${Math.abs(asNum(gain.share_delta_pp, 0)).toFixed(1)} pp of mix share.` : "No material positive mix shift."} ${decline ? `${decline.family} is softening by ${Math.abs(asNum(decline.share_delta_pp, 0)).toFixed(1)} pp.` : "No material negative mix shift."}`
    );
  };

  const setChartEmpty = (id, message) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (!message) {
      el.innerHTML = "";
      return;
    }
    el.innerHTML = `<div class="text-muted small py-5 text-center">${escapeHtml(message)}</div>`;
  };

  const renderPlot = (targetId, traces, layout) => {
    if (!window.Plotly) {
      setChartEmpty(targetId, "Chart library is unavailable.");
      return null;
    }
    const el = document.getElementById(targetId);
    if (!el) return null;
    setChartEmpty(targetId, null);
    const plot = window.Plotly.react ? window.Plotly.react(el, traces, layout, { displayModeBar: false, responsive: true }) : window.Plotly.newPlot(el, traces, layout, { displayModeBar: false, responsive: true });
    return plot;
  };

  const renderTrend = (trend = {}) => {
    const labels = asArr(trend.labels);
    const revenue = asArr(trend.revenue);
    const profit = asArr(trend.profit);
    const margin = asArr(trend.margin_pct);
    if (!labels.length) {
      setChartEmpty("supTrendChart", "No monthly trend data for the current supplier scope.");
      return;
    }
    renderPlot("supTrendChart", [
      { x: labels, y: revenue, type: "bar", name: "Revenue", marker: { color: "#2f6fec" }, hovertemplate: "%{x}<br>Revenue %{y:$,.0f}<extra></extra>" },
      { x: labels, y: profit, type: "scatter", mode: "lines+markers", name: "Profit", line: { color: "#0f9b6d", width: 3 }, hovertemplate: "%{x}<br>Profit %{y:$,.0f}<extra></extra>", visible: showCosts ? true : "legendonly" },
      { x: labels, y: margin, type: "scatter", mode: "lines", name: "Margin %", yaxis: "y2", line: { color: "#b86b16", width: 2, dash: "dot" }, hovertemplate: "%{x}<br>Margin %{y:.1f}%<extra></extra>", visible: showCosts ? true : "legendonly" },
    ], {
      height: 360,
      margin: { t: 10, r: 56, b: 56, l: 56 },
      hovermode: "x unified",
      paper_bgcolor: "rgba(0,0,0,0)",
      plot_bgcolor: "rgba(0,0,0,0)",
      xaxis: { tickangle: -35, automargin: true },
      yaxis: { tickformat: "$,.0f" },
      yaxis2: { overlaying: "y", side: "right", ticksuffix: "%", showgrid: false },
      legend: { orientation: "h", y: 1.08 },
    });
  };

  const renderPareto = (rows = [], kpis = {}) => {
    const list = asArr(rows);
    if (!list.length) {
      setChartEmpty("supParetoChart", "No concentration data for current filters.");
      setText(els.concentrationSummary, "No concentration data for current filters.");
      return;
    }
    renderPlot("supParetoChart", [
      {
        x: list.map((r) => r.supplier_name || r.supplier_id),
        y: list.map((r) => asNum(r.share_pct, 0)),
        type: "bar",
        name: "Revenue share",
        marker: { color: "#7a3f51" },
        hovertemplate: "%{x}<br>%{y:.1f}% share<extra></extra>",
      },
      {
        x: list.map((r) => r.supplier_name || r.supplier_id),
        y: list.map((r) => asNum(r.cumulative_share_pct, 0)),
        type: "scatter",
        mode: "lines+markers",
        name: "Cumulative share",
        yaxis: "y2",
        line: { color: "#244995", width: 3 },
        hovertemplate: "%{x}<br>%{y:.1f}% cumulative<extra></extra>",
      },
    ], {
      height: 360,
      margin: { t: 10, r: 56, b: 88, l: 42 },
      hovermode: "x unified",
      paper_bgcolor: "rgba(0,0,0,0)",
      plot_bgcolor: "rgba(0,0,0,0)",
      xaxis: { tickangle: -48, automargin: true },
      yaxis: { title: "Share %", ticksuffix: "%" },
      yaxis2: { overlaying: "y", side: "right", ticksuffix: "%", showgrid: false },
      legend: { orientation: "h", y: 1.08 },
    });
    setText(
      els.concentrationSummary,
      `Revenue HHI ${int0(kpis.concentration_hhi)} · Top 1 ${pct1(kpis.concentration_top1_share)} · Top 5 ${pct1(kpis.concentration_top5_share)} · Profit top 5 ${pct1(kpis.profit_concentration_top5_share)}`
    );
  };

  const renderProteinChart = (protein = {}) => {
    const rows = asArr(protein.mix).slice(0, 8);
    if (!rows.length) {
      setChartEmpty("supProteinChart", "Protein-family mix is unavailable for the current supplier scope.");
      return;
    }
    renderPlot("supProteinChart", [
      {
        x: rows.map((row) => row.family || "Unassigned"),
        y: rows.map((row) => asNum(row.revenue, 0)),
        type: "bar",
        name: "Revenue",
        marker: { color: "#7f3143" },
        hovertemplate: "%{x}<br>Revenue %{y:$,.0f}<extra></extra>",
      },
      {
        x: rows.map((row) => row.family || "Unassigned"),
        y: rows.map((row) => asNum(row.margin_pct, null)),
        type: "scatter",
        mode: "lines+markers",
        name: "Margin %",
        yaxis: "y2",
        line: { color: "#1e8665", width: 3 },
        hovertemplate: "%{x}<br>Margin %{y:.1f}%<extra></extra>",
        visible: showCosts ? true : "legendonly",
      },
      {
        x: rows.map((row) => row.family || "Unassigned"),
        y: rows.map((row) => asNum(row.share_delta_pp, null)),
        type: "scatter",
        mode: "markers",
        name: "Mix shift (pp)",
        yaxis: "y3",
        marker: { color: "#c18a20", size: 10, symbol: "diamond" },
        hovertemplate: "%{x}<br>Mix shift %{y:.1f} pp<extra></extra>",
      },
    ], {
      height: 360,
      margin: { t: 10, r: 78, b: 58, l: 56 },
      hovermode: "x unified",
      paper_bgcolor: "rgba(0,0,0,0)",
      plot_bgcolor: "rgba(0,0,0,0)",
      xaxis: { automargin: true },
      yaxis: { tickformat: "$,.0f", title: "Revenue" },
      yaxis2: { overlaying: "y", side: "right", ticksuffix: "%", showgrid: false, title: "Margin %" },
      yaxis3: { overlaying: "y", side: "right", anchor: "free", position: 1, ticksuffix: "pp", showgrid: false, title: "Mix shift", titlefont: { size: 11 }, tickfont: { size: 10 } },
      legend: { orientation: "h", y: 1.08 },
    });

    const chartEl = document.getElementById("supProteinChart");
    if (chartEl && typeof chartEl.on === "function") {
      if (chartEl.removeAllListeners) chartEl.removeAllListeners("plotly_click");
      chartEl.on("plotly_click", (event) => {
        const family = event?.points?.[0]?.x;
        if (!family) return;
        state.proteinFilter = String(family);
        state.page = 1;
        applyActiveStates();
        fetchBundle({ append: false });
      });
    }
  };

  const renderSegmentChart = (rows = []) => {
    const list = asArr(rows);
    if (!list.length) {
      setChartEmpty("supSegmentChart", "Segment composition is unavailable.");
      return;
    }
    renderPlot("supSegmentChart", [{
      x: list.map((row) => asNum(row.share_pct, 0)),
      y: list.map((row) => row.segment || "Unknown"),
      type: "bar",
      orientation: "h",
      marker: { color: "#274d96" },
      hovertemplate: "%{y}<br>%{x:.1f}% share<extra></extra>",
    }], {
      height: 290,
      margin: { t: 10, r: 20, b: 28, l: 120 },
      paper_bgcolor: "rgba(0,0,0,0)",
      plot_bgcolor: "rgba(0,0,0,0)",
      xaxis: { ticksuffix: "%" },
      yaxis: { automargin: true },
    });
  };

  const renderMiniRows = (target, rows, rowBuilder, emptyCols) => {
    if (!target) return;
    const list = asArr(rows);
    if (!list.length) {
      target.innerHTML = `<tr><td colspan="${emptyCols}" class="text-muted">No data</td></tr>`;
      return;
    }
    target.innerHTML = list.map(rowBuilder).join("");
  };

  const renderProteinRows = (protein = {}) => {
    const rows = asArr(protein.mix).slice(0, 8);
    if (!els.proteinChips) return;
    els.proteinChips.innerHTML = rows
      .slice(0, 6)
      .map((row) => {
        const family = row.family || "Unassigned";
        const active = state.proteinFilter && state.proteinFilter.toLowerCase() === String(family).toLowerCase();
        return `<button type="button" class="btn btn-sm btn-outline-secondary supplier-protein-chip ${active ? "active" : ""}" data-protein="${escapeHtml(family)}">${escapeHtml(family)} · ${pct1(row.share_current)}</button>`;
      })
      .join("");

    renderMiniRows(
      els.proteinMixRows,
      rows,
      (row) => {
        const family = row.family || "Unassigned";
        return `
          <tr data-protein-row="${escapeHtml(family)}">
            <td><button type="button" class="btn btn-link btn-sm p-0 supplier-protein-row" data-protein="${escapeHtml(family)}">${escapeHtml(family)}</button></td>
            <td>${escapeHtml(row.top_supplier_name || "—")}</td>
            <td class="text-end">${money0(row.revenue)}</td>
            <td class="text-end">${pct1(row.share_current)}</td>
            <td class="text-end">${asNum(row.share_delta_pp) == null ? "—" : `${asNum(row.share_delta_pp, 0).toFixed(1)} pp`}</td>
            <td class="text-end">${marginCellHtml(row)}</td>
          </tr>
        `;
      },
      6
    );
  };

  const renderRiskTables = (risk = {}, dependency = {}) => {
    renderMiniRows(
      els.marginLeakageRows,
      asArr(risk.margin_leakage).slice(0, 8),
      (row) => `
        <tr data-supplier-link="${escapeHtml(row.supplier_id || "")}"${drillAttr(supplierPayload(row.supplier_id, row.supplier_name || row.supplier_id, "Margin recovery", "Below target suppliers", "Profit uplift", row.profit_uplift_target))}>
          <td>${escapeHtml(row.supplier_name || row.supplier_id || "")}</td>
          <td>${escapeHtml(row.top_protein || "Unassigned")}</td>
          <td class="text-end">${marginCellHtml(row)}</td>
          <td class="text-end">${showCosts ? money0(row.profit_uplift_target) : "—"}</td>
        </tr>
      `,
      4
    );
    renderMiniRows(
      els.dataRiskRows,
      asArr(risk.data_risk).slice(0, 8),
      (row) => `
        <tr data-supplier-link="${escapeHtml(row.supplier_id || "")}"${drillAttr(supplierPayload(row.supplier_id, row.supplier_name || row.supplier_id, "Data risk", "Missing cost exposure", "Missing cost revenue", row.missing_cost_revenue))}>
          <td>${escapeHtml(row.supplier_name || row.supplier_id || "")}</td>
          <td class="text-end">${money0(row.missing_cost_revenue)}</td>
          <td class="text-end">${pct1(row.cost_coverage_pct)}</td>
          <td>${escapeHtml(row.top_protein || "Unassigned")}</td>
        </tr>
      `,
      4
    );
    renderMiniRows(
      els.dependencyRows,
      asArr(dependency.concentrated_suppliers).slice(0, 8),
      (row) => `
        <tr data-supplier-link="${escapeHtml(row.supplier_id || "")}"${drillAttr(supplierPayload(row.supplier_id, row.supplier_name || row.supplier_id, "Dependency", "Supplier dependency", "Protein dependency", row.top_protein_share_pct))}>
          <td>${escapeHtml(row.supplier_name || row.supplier_id || "")}</td>
          <td>${escapeHtml(row.top_protein || "Unassigned")}</td>
          <td class="text-end">${pct1(row.top_protein_share_pct)}</td>
          <td class="text-end">${escapeHtml(row.posture || "Balanced")}</td>
        </tr>
      `,
      4
    );
  };

  const renderMoverRows = (movers = {}) => {
    const rows = asArr(movers.top_gainers).slice(0, 4).concat(asArr(movers.top_decliners).slice(0, 4));
    const allRows = asArr(movers.rows);
    const positiveCount = allRows.filter((row) => asNum(row.delta_revenue, 0) > 0).length;
    const negativeCount = allRows.filter((row) => asNum(row.delta_revenue, 0) < 0).length;
    setText(
      els.moverNarrative,
      `${positiveCount} suppliers are up versus the prior comparable window and ${negativeCount} are down. Use this section to separate concentrated supplier swings from broad-based portfolio movement.`
    );
    renderMiniRows(
      els.moversRows,
      rows,
      (row) => `
        <tr data-supplier-link="${escapeHtml(row.supplier_id || "")}"${drillAttr(supplierPayload(row.supplier_id, row.supplier_name || row.supplier_id, "Movers", "Supplier movers", "Delta revenue", row.delta_revenue))}>
          <td>
            <div class="fw-semibold">${escapeHtml(row.supplier_name || row.supplier_id || "")}</div>
            <div class="text-muted small">${escapeHtml(row.top_protein || "Unassigned")} · ${escapeHtml(row.segment_label || "Segment")}</div>
          </td>
          <td class="text-end">${money0(row.delta_revenue)}</td>
          <td class="text-end">${showCosts ? pct1(row.delta_margin_pp) : "—"}</td>
        </tr>
      `,
      3
    );
  };

  const renderSegments = (segments = {}) => {
    const rows = asArr(segments.summary);
    const strategic = rows.find((row) => row.segment_key === "strategic");
    const growth = rows.find((row) => row.segment_key === "growth");
    const marginRisk = rows.find((row) => row.segment_key === "margin_risk");
    setText(
      els.segmentNarrative,
      `${strategic ? `${int0(strategic.suppliers)} strategic suppliers carry ${pct1(strategic.share_pct)} of revenue.` : "No strategic concentration detected."} ${growth ? `${int0(growth.suppliers)} suppliers sit in growth mode.` : ""} ${marginRisk ? `${int0(marginRisk.suppliers)} suppliers need margin recovery.` : ""}`.trim()
    );
    renderSegmentChart(rows);
    renderMiniRows(
      els.segmentSummaryRows,
      rows,
      (row) => `
        <tr data-segment-filter="${escapeHtml(row.segment_key || "")}">
          <td><button type="button" class="btn btn-link btn-sm p-0 supplier-segment-row" data-segment="${escapeHtml(row.segment_key || "")}">${escapeHtml(row.segment || "")}</button></td>
          <td class="text-end">${int0(row.suppliers)}</td>
          <td class="text-end">${money0(row.revenue)}</td>
          <td class="text-end">${pct1(row.delta_revenue_pct)}</td>
          <td class="text-end">${pct1(row.share_pct)}</td>
        </tr>
      `,
      5
    );
  };

  const buildDrilldownHref = (supplierId) => {
    const params = new URLSearchParams(state.filterQs || "");
    params.set("suppliers_v2", "1");
    if (state.proteinFilter) params.set("protein", state.proteinFilter);
    return `/suppliers/${encodeURIComponent(String(supplierId || ""))}?${params.toString()}`;
  };

  const mergePayload = (base, patch, { append = false } = {}) => {
    if (!base || typeof base !== "object") return patch || {};
    if (!patch || typeof patch !== "object") return base || {};
    const merged = { ...base, ...patch };
    merged.table = { ...(base.table || {}), ...(patch.table || {}) };
    if (append) {
      const priorRows = asArr(base.table?.rows);
      const nextRows = asArr(patch.table?.rows);
      merged.table.rows = [...priorRows, ...nextRows];
    }
    return merged;
  };

  const syncControlsFromState = () => {
    if (els.pageSize) els.pageSize.value = String(state.pageSize);
    if (els.search) els.search.value = state.search || "";
    applyActiveStates();
  };

  const snapshotUiState = () => ({
    page: state.page,
    pageSize: state.pageSize,
    sortBy: state.sortBy,
    sortDir: state.sortDir,
    search: state.search,
    quickFilter: state.quickFilter,
    proteinFilter: state.proteinFilter,
    totalRows: state.totalRows,
  });

  const applySnapshotUiState = (uiState = {}) => {
    if (!uiState || typeof uiState !== "object") return;
    if (Number.isFinite(Number(uiState.page)) && Number(uiState.page) > 0) state.page = Number(uiState.page);
    if (Number.isFinite(Number(uiState.pageSize)) && Number(uiState.pageSize) > 0) state.pageSize = Number(uiState.pageSize);
    if (uiState.sortBy) state.sortBy = String(uiState.sortBy);
    if (uiState.sortDir) state.sortDir = String(uiState.sortDir) === "asc" ? "asc" : "desc";
    if (uiState.search != null) state.search = String(uiState.search);
    if (uiState.quickFilter != null) state.quickFilter = String(uiState.quickFilter);
    if (uiState.proteinFilter != null) state.proteinFilter = String(uiState.proteinFilter);
    if (Number.isFinite(Number(uiState.totalRows)) && Number(uiState.totalRows) >= 0) state.totalRows = Number(uiState.totalRows);
  };

  const persistSnapshot = (payload = lastPayload) => {
    if (!pageCache || !payload || !state.filterQs) return false;
    return pageCache.saveSnapshot(PAGE_CACHE_ID, {
      qs: state.filterQs,
      payload,
      uiState: snapshotUiState(),
      scrollY: window.scrollY || 0,
      meta: {
        datasetVersion: payload?.meta?.dataset_version || null,
      },
    });
  };

  const restoreSnapshot = (qs, { restoreScroll = false } = {}) => {
    if (!pageCache) return null;
    const snapshot = pageCache.loadSnapshot(PAGE_CACHE_ID, { qs, ...PAGE_CACHE_POLICY });
    if (!snapshot?.payload) return null;
    applySnapshotUiState(snapshot.ui_state || {});
    syncControlsFromState();
    renderPayload(snapshot.payload || {}, { append: false });
    if (restoreScroll) {
      pageCache.restoreScroll(PAGE_CACHE_ID, { qs, ...PAGE_CACHE_POLICY, delayMs: 40 });
    }
    return snapshot;
  };

  const renderCommandTable = (table = {}, { append = false } = {}) => {
    const rows = asArr(table.rows);
    state.totalRows = Number(table.total_rows || rows.length || 0);
    if (!append) els.tableBody.innerHTML = "";
    if (!rows.length && !append) {
      els.tableBody.innerHTML = '<tr><td colspan="16" class="text-center text-muted py-4">No suppliers match the current filters.</td></tr>';
    } else if (rows.length) {
      const html = rows
        .map((row) => {
          const href = buildDrilldownHref(row.supplier_id);
          const deltaPct = row.low_base_warning ? "low base" : pct1(row.delta_revenue_pct);
          const riskClass = riskToneClass(row.risk_band);
          const action = row.action_bucket || (
            row.segment_key === "strategic"
              ? "Protect"
              : row.segment_key === "growth"
              ? "Expand"
              : row.segment_key === "margin_risk"
              ? "Recover"
              : row.segment_key === "data_risk"
              ? "Review"
              : "Rationalize"
          );
          return `
            <tr data-row="supplier" data-id="${escapeHtml(row.supplier_id || "")}" tabindex="0"${drillAttr(
              supplierPayload(row.supplier_id, row.supplier_name || row.supplier_id, "Supplier command table", "Supplier row", "Revenue", row.revenue_current)
            )}>
              <td>
                <div class="fw-semibold">${escapeHtml(row.supplier_name || row.supplier_id || "Unknown")}</div>
                <div class="text-muted small">${escapeHtml(row.segment_reason || row.segment_label || "")}</div>
              </td>
              <td>${escapeHtml(row.segment_label || "Long tail")}</td>
              <td class="text-end"><span class="suppliers-risk-pill ${riskClass}">${escapeHtml(row.risk_band || "Unknown")}</span></td>
              <td class="text-end">${money0(row.revenue_current)}</td>
              <td class="text-end">${money0(row.revenue_prior)}</td>
              <td class="text-end">${money0(row.delta_revenue)}</td>
              <td class="text-end">${deltaPct}</td>
              <td class="text-end">${marginCellHtml(row)}</td>
              <td class="text-end">${int0(row.orders)}</td>
              <td class="text-end">${pct1(row.cost_coverage_pct)}</td>
              <td class="text-end">${money0(row.missing_cost_revenue_current)}</td>
              <td class="text-end">${int0(row.days_since_last_order)}</td>
              <td>
                <span class="suppliers-protein-pill">${escapeHtml(row.top_protein || "Unassigned")}</span>
                <div class="text-muted small mt-1">${escapeHtml(row.top_category || "Unassigned")}</div>
              </td>
              <td class="text-end">${pct1(row.top_protein_share_pct)}</td>
              <td class="text-end">${pct1(row.top_sku_share_pct)}</td>
              <td>
                <a class="btn btn-sm btn-outline-primary" href="${href}">${escapeHtml(action)}</a>
                <div class="text-muted small mt-1">${escapeHtml(row.protein_dependency_posture || "")}</div>
              </td>
            </tr>
          `;
        })
        .join("");
      els.tableBody.insertAdjacentHTML("beforeend", html);
    }

    const shown = els.tableBody.querySelectorAll("tr[data-row='supplier']").length;
    setText(els.tableStatus, `${int0(shown)} of ${int0(state.totalRows)} suppliers shown`);
    const summary = table.summary || {};
    const quickLabel = QUICK_FILTER_LABELS[state.quickFilter] || QUICK_FILTER_LABELS.all;
    const proteinTxt = state.proteinFilter ? ` Protein focus is ${state.proteinFilter}.` : "";
    setText(
      els.tableNarrative,
      `${quickLabel} currently represent ${money0(summary.revenue)} in revenue across ${int0(summary.supplier_count)} suppliers.${proteinTxt} Missing-cost exposure in this slice is ${money0(summary.missing_cost_revenue)}.`
    );
    if (els.loadMore) els.loadMore.disabled = shown >= state.totalRows;
  };

  const renderPayload = (payload, { append = false } = {}) => {
    lastPayload = mergePayload(lastPayload || {}, payload || {}, { append });
    renderHeader(payload);
    renderKpis(payload.kpis || {});
    renderExecutiveReadout(payload);
    renderTrend((payload.charts || {}).revenue_profit_trend || payload.trend || {});
    renderPareto((payload.charts || {}).concentration_pareto || [], payload.kpis || {});
    renderProteinChart(payload.protein_intelligence || {});
    renderProteinRows(payload.protein_intelligence || {});
    renderProteinHighlights(payload.protein_intelligence || {});
    renderRiskTables(payload.risk_opportunities || {}, payload.dependency || {});
    renderMoverRows(payload.movers || {});
    renderSegments(payload.segments || {});
    renderCommandTable(payload.table || {}, { append });
    updateExportLinks();
    applyActiveStates();
    if (window.universalDrilldown && typeof window.universalDrilldown.enhanceAll === "function") {
      window.universalDrilldown.enhanceAll();
    }
    persistSnapshot(lastPayload);
  };

  const applyActiveStates = () => {
    root.querySelectorAll(".supplier-quick-chip").forEach((btn) => {
      const active = (btn.dataset.quick || "all") === state.quickFilter;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-pressed", active ? "true" : "false");
    });
    root.querySelectorAll(".supplier-protein-chip").forEach((btn) => {
      const token = String(btn.dataset.protein || "").toLowerCase();
      const active = !!state.proteinFilter && token === String(state.proteinFilter).toLowerCase();
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-pressed", active ? "true" : "false");
    });
    if (els.resetProtein) els.resetProtein.disabled = !state.proteinFilter;
    setText(
      els.proteinFocus,
      state.proteinFilter
        ? `Protein focus: ${state.proteinFilter}. Clear protein focus to return to the full supplier command table.`
        : "No protein focus selected."
    );
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

  let inflight = null;
  const fetchBundle = async ({ append = false, snapshot = null } = {}) => {
    state.loading = true;
    state.requestId += 1;
    const requestId = state.requestId;
    if (inflight) inflight.abort();
    inflight = new AbortController();
    setText(els.tableStatus, append ? "Loading more suppliers..." : "Loading supplier intelligence...");
    const url = `${bundleUrl}?${buildParams({ includePage: true }).toString()}`;
    try {
      const res = await authFetch(url, {
        signal: inflight.signal,
        headers: pageCache ? pageCache.prepareHeaders(url, { Accept: "application/json" }) : { Accept: "application/json" },
      });
      if (pageCache) pageCache.rememberResponse(url, res);
      if (res.status === 304) {
        if (!lastPayload && snapshot?.payload) renderPayload(snapshot.payload || {}, { append: false });
        setText(els.tableStatus, "Loaded");
        return;
      }
      if (!res.ok) throw new Error(`Bundle request failed (${res.status})`);
      const raw = await res.json();
      const payload = window.normalizeBundlePayload ? window.normalizeBundlePayload(raw) : raw;
      if (requestId !== state.requestId) return;
      renderPayload(payload || {}, { append });
      setText(els.tableStatus, "Loaded");
    } catch (err) {
      if (err?.name === "AbortError") return;
      console.error("[suppliers-v2] bundle fetch failed", err);
      if (!append && !lastPayload) {
        els.tableBody.innerHTML = '<tr><td colspan="16" class="text-center text-danger py-4">Failed to load supplier intelligence.</td></tr>';
      }
      setText(els.tableStatus, lastPayload ? "Refresh failed" : "Failed to load");
      setText(
        els.narrative,
        lastPayload
          ? "Supplier intelligence refresh failed. The last successful snapshot remains on screen."
          : "Supplier intelligence could not be loaded. Retry after checking filters or page scope."
      );
    } finally {
      if (requestId !== state.requestId) return;
      state.loading = false;
      dispatchGlobalApplyAck({ qs: state.filterQs });
    }
  };

  const resetAndFetch = () => {
    state.page = 1;
    updateExportLinks();
    syncControlsFromState();
    fetchBundle({ append: false });
  };

  const wireControls = () => {
    if (els.pageSize) {
      els.pageSize.value = String(state.pageSize);
      els.pageSize.addEventListener("change", () => {
        state.pageSize = Math.max(1, Number.parseInt(els.pageSize.value || "50", 10) || 50);
        resetAndFetch();
      });
    }
    if (els.search) {
      let timer = null;
      els.search.addEventListener("input", () => {
        window.clearTimeout(timer);
        timer = window.setTimeout(() => {
          state.search = (els.search.value || "").trim();
          resetAndFetch();
        }, 250);
      });
    }
    if (els.clearSearch) {
      els.clearSearch.addEventListener("click", () => {
        if (els.search) els.search.value = "";
        state.search = "";
        resetAndFetch();
      });
    }
    if (els.loadMore) {
      els.loadMore.addEventListener("click", () => {
        if (state.loading) return;
        const shown = els.tableBody.querySelectorAll("tr[data-row='supplier']").length;
        if (shown >= state.totalRows) return;
        state.page += 1;
        fetchBundle({ append: true });
      });
    }
    root.querySelectorAll("#suppliersCommandTable thead th.sortable").forEach((th) => {
      th.addEventListener("click", () => {
        const sort = th.dataset.sort;
        if (!sort) return;
        if (state.sortBy === sort) {
          state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
        } else {
          state.sortBy = sort;
          state.sortDir = "desc";
        }
        resetAndFetch();
      });
    });

    root.addEventListener("click", (event) => {
      const quick = event.target.closest(".supplier-quick-chip");
      if (quick) {
        state.quickFilter = quick.dataset.quick || "all";
        resetAndFetch();
        return;
      }
      const action = event.target.closest("[data-action-quick]");
      if (action) {
        state.quickFilter = action.getAttribute("data-action-quick") || "all";
        resetAndFetch();
        document.getElementById("suppliers-command-table")?.scrollIntoView({ behavior: "smooth", block: "start" });
        return;
      }
      const proteinChip = event.target.closest("[data-protein]");
      if (proteinChip) {
        state.proteinFilter = proteinChip.getAttribute("data-protein") || "";
        resetAndFetch();
        return;
      }
      const segmentBtn = event.target.closest("[data-segment]");
      if (segmentBtn) {
        state.quickFilter = segmentBtn.getAttribute("data-segment") || "all";
        resetAndFetch();
        return;
      }
      const supplierLink = event.target.closest("[data-supplier-link]");
      if (supplierLink && !event.target.closest("a,button")) {
        const supplierId = supplierLink.getAttribute("data-supplier-link") || "";
        if (!supplierId) return;
        window.location.href = buildDrilldownHref(supplierId);
        return;
      }
      const resetProtein = event.target.closest(".supplier-protein-reset");
      if (resetProtein) {
        state.proteinFilter = "";
        resetAndFetch();
      }
    });

    els.tableBody?.addEventListener("click", (event) => {
      const row = event.target.closest("tr[data-row='supplier']");
      if (!row || event.target.closest("a,button")) return;
      const supplierId = row.dataset.id;
      if (!supplierId) return;
      window.location.href = buildDrilldownHref(supplierId);
    });
    els.tableBody?.addEventListener("keydown", (event) => {
      const row = event.target.closest("tr[data-row='supplier']");
      if (!row) return;
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      const supplierId = row.dataset.id;
      if (!supplierId) return;
      window.location.href = buildDrilldownHref(supplierId);
    });
  };

  const onGlobalApply = (evt) => {
    currentApplyId = String(evt?.detail?.applyId || "");
    const qs = (evt && evt.detail && evt.detail.qs) || "";
    state.filterQs = (qs || "").replace(/^\?/, "");
    resetAndFetch();
  };

  const bootstrap = async (qsHint) => {
    if (bootstrapped) return;
    bootstrapped = true;
    let qs = (qsHint || "").replace(/^\?/, "");
    if (!qs) {
      const readyDetail = await waitForFiltersReady();
      qs = String(readyDetail?.qs || state.filterQs || "").replace(/^\?/, "");
    }
    state.filterQs = qs;
    syncControlsFromState();
    updateExportLinks();
    const snapshot = restoreSnapshot(qs, { restoreScroll: true });
    if (snapshot?.fresh) {
      dispatchGlobalApplyAck({ qs: state.filterQs });
      return;
    }
    fetchBundle({ append: false, snapshot });
  };

  window.addEventListener("globalFilters:apply", onGlobalApply);
  window.addEventListener("globalFilters:ready", (evt) => {
    bootstrap((evt?.detail && evt.detail.qs) || "");
  });
  window.addEventListener("pagehide", () => {
    persistSnapshot();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") persistSnapshot();
  });
  wireControls();
  bootstrap(state.filterQs);
})();
