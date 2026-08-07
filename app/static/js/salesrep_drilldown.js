(() => {
  const metaEl = document.getElementById("SalesRepDrilldownMeta");
  if (!metaEl) return;

  const authFetch = window.authFetch || fetch;
  const ChartLib = window.Chart;
  const bundleUrl = metaEl.dataset.bundleUrl || "/api/salesreps/drilldown/bundle";
  const repId = metaEl.dataset.entityId || "";
  const v2Enabled = metaEl.dataset.v2Enabled === "1";
  const bootPayloadEl = document.getElementById("SalesRepDrilldownBoot");
  const charts = {};
  let portfolioMap = null;
  let portfolioMapReady = false;
  let portfolioMapPopup = null;
  let portfolioMapPendingRows = [];
  let portfolioMapHoveredId = null;
  let portfolioMapAnimationId = null;

  let controller = null;
  let filtersQS = window.location.search ? window.location.search.replace(/^\?/, "") : "";
  let bootstrapped = false;
  let bootPayload = null;
  let bootPayloadUsed = false;
  let currentReqId = 0;
  let currentApplyId = null;
  let trendGrain = "monthly";
  let trendRolling = false;
  let currentPayload = null;
  let customerRows = [];
  let productRows = [];
  let attributionMode = "current_owner";
  let rosterMode = "current_only";
  let transferOnly = false;

  if (document?.body?.dataset) {
    document.body.dataset.filtersHandler = "ajax";
  }

  const fmtMoney = new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  });
  const fmtMoney2 = new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  const fmtMoneyCompact = new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    notation: "compact",
    maximumFractionDigits: 1,
  });
  const fmtInt = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
  const fmtPct = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
  const fmtFloat2 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
  const NA = "N/A";
  const SAFE_REP_BUCKETS = new Set(["unassigned / needs review", "unknown rep", "needs mapping"]);

  if (bootPayloadEl) {
    try {
      bootPayload = JSON.parse(bootPayloadEl.textContent || "null");
    } catch (_err) {
      bootPayload = null;
    }
  }

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
      // ignore
    }
  };

  const safeNum = (value, fallback = 0) => {
    const num = Number(value);
    return Number.isFinite(num) ? num : fallback;
  };

  const safeOptional = (value) => {
    const num = Number(value);
    return Number.isFinite(num) ? num : null;
  };

  const formatCurrency = (value) => {
    const num = safeOptional(value);
    return num == null ? NA : fmtMoney.format(num);
  };
  const formatCurrency2 = (value) => {
    const num = safeOptional(value);
    return num == null ? NA : fmtMoney2.format(num);
  };
  const formatCurrencyCompact = (value) => {
    const num = safeOptional(value);
    return num == null ? NA : fmtMoneyCompact.format(num);
  };

  const formatInt = (value) => fmtInt.format(safeNum(value));

  const formatPct = (value, scaleShare = false) => {
    const num = safeOptional(value);
    if (num == null) return NA;
    const display = scaleShare && num <= 1.01 ? num * 100 : num;
    return `${fmtPct.format(display)}%`;
  };
  const formatSignedCurrency2 = (value) => {
    const num = safeOptional(value);
    if (num == null) return NA;
    return `${num > 0 ? "+" : ""}${fmtMoney2.format(num)}`;
  };
  const formatSignedPoints = (value) => {
    const num = safeOptional(value);
    if (num == null) return NA;
    return `${num > 0 ? "+" : ""}${fmtPct.format(num)} pts`;
  };

  const cleanText = (value) => {
    const text = String(value ?? "").trim();
    if (!text || ["none", "null", "nan"].includes(text.toLowerCase())) return "";
    return text;
  };

  const escapeHtml = (value) =>
    String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");

  const ratioPct = (value, total) => {
    const numerator = safeOptional(value);
    const denominator = safeOptional(total);
    if (numerator == null || denominator == null || denominator === 0) return null;
    return (numerator / denominator) * 100;
  };

  const toneFromStatus = (label, color) => {
    const text = `${cleanText(label)} ${cleanText(color)}`.toLowerCase();
    if (!text) return "neutral";
    if (
      ["materially below minimum", "negative", "red", "#c2413b", "#b23a3a", "#dc3545", "#b42318"].some((token) =>
        text.includes(token)
      )
    ) {
      return "risk";
    }
    if (
      ["below target", "near minimum", "between minimum", "orange", "yellow", "#dd6b20", "#caa33a"].some((token) =>
        text.includes(token)
      )
    ) {
      return "warn";
    }
    if (
      ["near target", "above target", "green", "light green", "#21884f", "#7bbf6a", "#137a50"].some((token) =>
        text.includes(token)
      )
    ) {
      return "good";
    }
    if (text.includes("inherited")) return "accent";
    return "neutral";
  };

  const toneFromSeverity = (severity) => {
    const text = cleanText(severity).toLowerCase();
    if (!text) return "neutral";
    if (text === "high") return "risk";
    if (text === "medium") return "warn";
    if (text === "ok" || text === "low") return "good";
    return "neutral";
  };

  const toneFromDelta = (value) => {
    const num = safeOptional(value);
    if (num == null) return "neutral";
    if (num > 0) return "good";
    if (num < 0) return "risk";
    return "neutral";
  };

  const renderPill = (label, tone = "neutral") => {
    const text = cleanText(label) || NA;
    return `<span class="srpd-pill srpd-pill--${tone}">${escapeHtml(text)}</span>`;
  };

  const isTechnicalRepId = (value) => {
    const text = cleanText(value);
    if (!text) return false;
    const lower = text.toLowerCase();
    if (SAFE_REP_BUCKETS.has(lower)) return false;
    if (/@|\/|\\/.test(text)) return true;
    if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(text)) return true;
    if (!/\s/.test(text) && /\d/.test(text) && /^[A-Za-z]{1,6}[-_ ]?\d[\w-]*$/.test(text)) return true;
    return !/\s/.test(text) && text.length >= 12 && /^[A-Za-z0-9_-]+$/.test(text);
  };

  const businessRepName = (name, fallbackId = null, defaultLabel = "Needs Mapping") => {
    const primary = cleanText(name);
    const fallback = cleanText(fallbackId);
    for (const candidate of [primary, fallback]) {
      if (!candidate) continue;
      if (SAFE_REP_BUCKETS.has(candidate.toLowerCase())) return candidate;
      if (!isTechnicalRepId(candidate)) return candidate;
    }
    return defaultLabel;
  };
  const renderStatusBadge = (label, color) => {
    const text = cleanText(label);
    if (!text) return NA;
    return renderPill(text, toneFromStatus(label, color));
  };
  const renderColorMetric = (value, color, formatter) => {
    const num = safeOptional(value);
    if (num == null) return NA;
    const tone = color ? ` style="color: ${color}; font-weight: 600;"` : "";
    return `<span${tone}>${formatter(num)}</span>`;
  };

  const deltaClass = (value) => {
    const num = safeOptional(value);
    if (num == null) return "";
    if (num > 0) return "kpi-delta-positive";
    if (num < 0) return "kpi-delta-negative";
    return "";
  };

  const formatDelta = (value) => {
    const num = safeOptional(value);
    if (num == null) return "N/A";
    const sign = num > 0 ? "+" : "";
    return `${sign}${fmtPct.format(num)}%`;
  };

  const getCanvas = (id) => {
    const el = document.getElementById(id);
    if (!el || typeof el.getContext !== "function") return null;
    return el;
  };

  const destroyChart = (key) => {
    if (charts[key]?.destroy) {
      charts[key].destroy();
    }
    charts[key] = null;
  };

  const toggleEmpty = (canvasId, show, message = "No data for selected filters.") => {
    const canvas = getCanvas(canvasId);
    if (!canvas) return;
    const holder = canvas.parentElement;
    if (!holder) return;
    if (!holder.style.position) holder.style.position = "relative";

    let emptyEl = holder.querySelector("[data-empty-state]");
    if (!emptyEl) {
      emptyEl = document.createElement("div");
      emptyEl.dataset.emptyState = "1";
      emptyEl.className =
        "position-absolute top-0 start-0 w-100 h-100 d-flex align-items-center justify-content-center text-muted small";
      emptyEl.style.background = "rgba(255,255,255,0.78)";
      emptyEl.style.pointerEvents = "none";
      holder.appendChild(emptyEl);
    }

    emptyEl.textContent = message;
    emptyEl.classList.toggle("d-none", !show);
    canvas.classList.toggle("d-none", !!show);
  };

  const setText = (id, text) => {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  };

  const setHTML = (id, html) => {
    const el = document.getElementById(id);
    if (el) el.innerHTML = html;
  };

  const csvList = (value) => {
    if (!value) return [];
    return String(value)
      .split(",")
      .map((v) => v.trim())
      .filter(Boolean);
  };

  const summarizeActiveFilters = (queryString) => {
    const params = new URLSearchParams(queryString || "");
    const ignore = new Set([
      "start",
      "end",
      "preset",
      "dataset",
      "format",
      "include_history",
      "rep_id",
      "salesrep_id",
      "sales_rep_id",
      "id",
      "export_type",
      "page",
      "page_size",
      "sort",
      "dir",
      "sort_dir",
      "attribution_mode",
      "roster_mode",
      "transfer_only",
    ]);
    const parts = [];
    params.forEach((rawValue, key) => {
      if (ignore.has(key)) return;
      const values = csvList(rawValue);
      if (!values.length) return;
      if (values.length === 1) {
        parts.push(`${key}: ${values[0]}`);
      } else {
        parts.push(`${key}: ${values.length} selected`);
      }
    });
    if (!parts.length) return "Filters: All";
    return `Filters: ${parts.slice(0, 4).join(" | ")}${parts.length > 4 ? " | ..." : ""}`;
  };

  const syncLocalControls = () => {
    const attributionSelect = document.getElementById("drAttributionMode");
    if (attributionSelect) attributionSelect.value = attributionMode;
    const formerWrap = document.getElementById("drFormerToggleWrap");
    if (formerWrap) formerWrap.classList.toggle("d-none", attributionMode !== "historical_rep");
    const includeFormer = document.getElementById("drIncludeFormerReps");
    if (includeFormer) includeFormer.checked = rosterMode === "include_former";
  };

  const syncLocalStateFromQS = (queryString) => {
    const params = new URLSearchParams(String(queryString || "").replace(/^\?/, ""));
    attributionMode = params.get("attribution_mode") === "historical_rep" ? "historical_rep" : "current_owner";
    rosterMode = params.get("roster_mode") === "include_former" ? "include_former" : "current_only";
    transferOnly = ["1", "true", "yes", "on"].includes(String(params.get("transfer_only") || "").toLowerCase());
    syncLocalControls();
  };

  const applyLocalOverridesFromQS = (queryString) => {
    const params = new URLSearchParams(String(queryString || "").replace(/^\?/, ""));
    if (params.has("attribution_mode")) {
      attributionMode = params.get("attribution_mode") === "historical_rep" ? "historical_rep" : "current_owner";
    }
    if (params.has("roster_mode")) {
      rosterMode = params.get("roster_mode") === "include_former" ? "include_former" : "current_only";
    }
    if (params.has("transfer_only")) {
      transferOnly = ["1", "true", "yes", "on"].includes(String(params.get("transfer_only") || "").toLowerCase());
    }
    syncLocalControls();
  };

  const mergeLocalControlsIntoQS = (queryString) => {
    const params = new URLSearchParams(String(queryString || "").replace(/^\?/, ""));
    params.set("attribution_mode", attributionMode);
    params.set("roster_mode", rosterMode);
    if (transferOnly) params.set("transfer_only", "1");
    else params.delete("transfer_only");
    return params.toString();
  };

  const updateBackLink = () => {
    const link = document.getElementById("drBackLink");
    if (!link) return;
    const base = link.dataset.baseHref || link.getAttribute("href") || "/salesreps/";
    const baseHref = base.split("?")[0];
    link.dataset.baseHref = baseHref;
    const params = new URLSearchParams(filtersQS || "");
    [
      "rep_id",
      "salesrep_id",
      "sales_rep_id",
      "id",
      "dataset",
      "format",
      "export_type",
      "include_history",
      "attribution_mode",
      "roster_mode",
      "transfer_only",
    ].forEach((key) => params.delete(key));
    const qs = params.toString();
    link.setAttribute("href", qs ? `${baseHref}?${qs}` : baseHref);
  };

  const renderContext = (payload) => {
    const meta = payload?.meta || {};
    const kpis = payload?.kpis || {};
    const params = new URLSearchParams(filtersQS || "");

    const start = params.get("start") || meta.window_start || kpis.start || "--";
    const end = params.get("end") || meta.window_end || kpis.end || "--";
    setText("drDateChip", `Window: ${start} to ${end}`);
    setText("drFilterChip", summarizeActiveFilters(filtersQS));

    const lastRefresh = kpis.last_refresh || meta.last_refresh || "--";
    setText("drLastRefresh", `Last refresh: ${lastRefresh}`);
    const attribution = meta.attribution || {};
    const bridge = meta.ownership_bridge || {};
    const succession = meta.ownership_succession || {};
    const snapshot = meta.ownership_snapshot || {};
    const warningCount = safeNum(meta.warning_count ?? (Array.isArray(payload?.warnings) ? payload.warnings.length : 0));
    if (attribution.attribution_mode) {
      attributionMode = attribution.attribution_mode === "current_owner" ? "current_owner" : "historical_rep";
    }
    if (attribution.roster_mode) {
      rosterMode = attribution.roster_mode === "include_former" ? "include_former" : "current_only";
    }
    if (attribution.transfer_only != null) {
      transferOnly = !!attribution.transfer_only;
    }
    syncLocalControls();
    setText(
      "drModeChip",
      `Mode: ${attributionMode === "current_owner" ? "Current Account Owner" : "Historical Rep"}`
    );
    setText(
      "drBridgeChip",
      bridge.available && succession.available
        ? `Owner mapping: ${formatInt(bridge.rows)} assignments + ${formatInt(succession.rows)} successor rules`
        : bridge.available
          ? `Owner mapping: ${formatInt(bridge.rows)} assignments`
          : snapshot.available
            ? `Owner mapping: ${formatInt(snapshot.rows)} customer owner snapshots`
          : succession.available
            ? `Owner mapping: ${formatInt(succession.rows)} successor rules`
            : "Owner mapping: unresolved"
    );
    setText("drWarningChip", `Warnings: ${formatInt(warningCount)}`);

    const whatChanged = payload?.insights?.what_changed || kpis.what_changed || "No change summary available.";
    setText("drWhatChanged", whatChanged);
    setText(
      "drHeroSupport",
      `${attributionMode === "current_owner" ? "Current owner" : "Historical rep"} view for ${start} to ${end}. ${
        warningCount > 0 ? `${formatInt(warningCount)} ownership note(s) require review.` : "No ownership warnings are active in this scope."
      }`
    );
  };

  const renderKpis = (payload) => {
    const kpis = payload?.kpis || {};
    const meta = payload?.meta || {};

    const name = businessRepName(kpis.rep_name || meta.entity_label, repId, NA);
    setText("salesrepName", name);
    setText(
      "salesrepId",
      attributionMode === "current_owner" ? "Current owner portfolio context" : "Historical rep accountability view"
    );

    document.querySelectorAll("[data-kpi-key]").forEach((el) => {
      const key = el.dataset.kpiKey;
      if (!key) return;
      const value = kpis[key];
      if (["revenue", "profit", "cost", "asp", "asp_lb", "below_target_margin_revenue", "negative_margin_revenue"].includes(key)) {
        el.textContent = formatCurrency(value);
        return;
      }
      if (["margin_pct", "momentum_pct", "cost_coverage_pct"].includes(key)) {
        el.textContent = formatPct(value, false);
        return;
      }
      if (["top_customer_share", "top5_customer_share", "top_product_share"].includes(key)) {
        el.textContent = formatPct(value, true);
        return;
      }
      if (key === "customer_hhi") {
        const num = safeOptional(value);
        el.textContent = num == null ? NA : fmtFloat2.format(num);
        return;
      }
      if (["orders", "customers", "active_customers_curr", "active_customers_prev", "below_target_margin_skus", "negative_margin_skus", "weight_lb", "units"].includes(key)) {
        el.textContent = formatInt(value);
        return;
      }
      el.textContent = value == null || value === "" ? NA : String(value);
    });

    const marginValueEl = document.querySelector('[data-kpi-key="margin_pct"]');
    if (marginValueEl) {
      marginValueEl.style.color = kpis.status_color ? String(kpis.status_color) : "";
    }
    const marginMetaEl = document.getElementById("drMarginTargetMeta");
    if (marginMetaEl) {
      const parts = [];
      if (kpis.target_margin_pct != null) parts.push(`Target ${formatPct(kpis.target_margin_pct, false)}`);
      if (kpis.minimum_margin_pct != null) parts.push(`Min ${formatPct(kpis.minimum_margin_pct, false)}`);
      if (kpis.target_gap_pct_points != null) {
        parts.push(`${formatSignedPoints(kpis.target_gap_pct_points)} vs target`);
      } else if (kpis.target_status) {
        parts.push(String(kpis.target_status));
      }
      marginMetaEl.textContent = parts.join(" · ") || "Target context unavailable";
      marginMetaEl.style.color = kpis.status_color ? String(kpis.status_color) : "";
    }
    const marginBadgeEl = document.getElementById("drMarginHealthBadge");
    if (marginBadgeEl) {
      const tone = toneFromStatus(kpis.target_status, kpis.status_color);
      marginBadgeEl.className = `srpd-pill srpd-pill--${tone}`;
      marginBadgeEl.textContent = cleanText(kpis.target_status) || "Target context";
    }

    document.querySelectorAll("[data-kpi-delta]").forEach((el) => {
      const key = el.dataset.kpiDelta;
      if (!key) return;
      const value = kpis[key];
      if (key === "active_customers_delta") {
        const num = safeOptional(value);
        const text = num == null ? "Delta: N/A" : `Delta: ${num > 0 ? "+" : ""}${fmtInt.format(num)}`;
        el.textContent = text;
        el.className = `small mt-1 ${deltaClass(value)}`.trim();
        return;
      }
      const prefix = key.endsWith("_mom_pct") ? "MoM" : key.endsWith("_yoy_pct") ? "YoY" : "Delta";
      el.textContent = `${prefix}: ${formatDelta(value)}`;
      el.classList.remove("kpi-delta-positive", "kpi-delta-negative");
      const cls = deltaClass(value);
      if (cls) el.classList.add(cls);
    });

    setText("drBelowTargetCount", formatInt(kpis.below_target_margin_skus));
    setText("drNegativeMarginCount", formatInt(kpis.negative_margin_skus));
  };

  const renderWarnings = () => {
    const holder = document.getElementById("drWarnings");
    if (!holder) return;
    const warnings = Array.isArray(currentPayload?.warnings) ? currentPayload.warnings.filter(Boolean) : [];
    if (!warnings.length) {
      holder.innerHTML = "";
      return;
    }
    holder.innerHTML = `
      <div class="alert alert-warning border-0 mb-0" role="status">
        <div class="fw-semibold mb-1">Ownership and attribution notes</div>
        <ul class="mb-0 ps-3">
          ${warnings.slice(0, 4).map((msg) => `<li>${escapeHtml(msg)}</li>`).join("")}
        </ul>
      </div>
    `;
  };

  const renderLoadFailure = (message) => {
    const text = cleanText(message) || "Unable to load sales rep drilldown data.";
    let holder = document.getElementById("drWarnings");
    if (!holder) {
      holder = document.getElementById("SalesRepDrilldownError");
      if (!holder) {
        holder = document.createElement("div");
        holder.id = "SalesRepDrilldownError";
        holder.className = "mb-3";
        metaEl.insertAdjacentElement("afterend", holder);
      }
    }
    holder.innerHTML = `
      <div class="alert alert-warning border-0 mb-0" role="alert">
        <div class="fw-semibold mb-1">Sales rep drilldown data is temporarily unavailable</div>
        <div>${escapeHtml(text)}</div>
      </div>
    `;
  };

  const renderOwnershipSummary = () => {
    const kpis = currentPayload?.kpis || {};
    const meta = currentPayload?.meta || {};
    const attribution = meta.attribution || {};
    const bridge = meta.ownership_bridge || {};
    const succession = meta.ownership_succession || {};
    const snapshot = meta.ownership_snapshot || {};
    const revenue = safeOptional(kpis.revenue);
    const inheritedRevenue = safeOptional(kpis.inherited_revenue);
    const directRevenue = revenue != null && inheritedRevenue != null ? Math.max(revenue - inheritedRevenue, 0) : null;
    const inheritedShare = ratioPct(inheritedRevenue, revenue);
    const directShare = ratioPct(directRevenue, revenue);
    const top1Share = safeOptional(kpis.top_customer_share);
    const top5Share = safeOptional(kpis.top5_customer_share);
    const belowTarget = safeNum(kpis.below_target_margin_skus);
    const negativeMargin = safeNum(kpis.negative_margin_skus);
    const mappingLabel = bridge.available
      ? `${formatInt(bridge.rows)} bridge assignment(s)`
      : snapshot.available
        ? `${formatInt(snapshot.rows)} snapshot owner record(s)`
        : "No owner bridge";
    const modeLabel = attribution.attribution_mode === "current_owner" ? "Current owner portfolio roll-up" : "Historical seller accountability";

    setText("drCurrentOwnedCustomers", formatInt(kpis.current_owned_customers));
    setText(
      "drCurrentOwnedRevenue",
      `Current owner revenue: ${formatCurrency(kpis.current_owner_revenue)}`
    );
    setText("drInheritedCustomers", formatInt(kpis.inherited_customers));
    setText(
      "drGainedLost",
      `${formatInt(kpis.gained_customers)} gained | ${formatInt(kpis.lost_customers)} lost`
    );
    setText(
      "drHistoricalVsCurrent",
      `${formatCurrency(kpis.historical_revenue)} | ${formatCurrency(kpis.current_owner_revenue)}`
    );
    setText(
      "drTransferSummary",
      `Transferred in: ${formatCurrency(kpis.transferred_in_revenue)} | out: ${formatCurrency(kpis.transferred_out_revenue)}`
    );
    setText("drModeNarrative", modeLabel);
    const unassigned = formatInt(kpis.unassigned_customers);
    const transferNote = kpis.transfer_only ? " | Transfer-only filter on" : "";
    setText("drOwnerSummary", `Unassigned customers: ${unassigned}${transferNote}`);

    setText(
      "drHeroMixValue",
      directShare != null ? `Direct ${formatPct(directShare, false)}` : formatCurrencyCompact(revenue)
    );
    setText(
      "drHeroMixDetail",
      directRevenue != null || inheritedRevenue != null
        ? `${formatCurrency(directRevenue)} direct | ${formatCurrency(inheritedRevenue)} inherited`
        : "Direct and inherited split unavailable for this filter window."
    );
    setText(
      "drHeroRiskValue",
      top1Share != null ? `Top account ${formatPct(top1Share, true)}` : "Concentration pending"
    );
    setText(
      "drHeroRiskDetail",
      top5Share != null
        ? `${formatPct(top5Share, true)} of revenue sits in the top five customers. HHI ${kpis.customer_hhi == null ? "N/A" : fmtFloat2.format(kpis.customer_hhi)}.`
        : "Top-customer concentration is not available for this scope."
    );
    setText(
      "drHeroMarginValue",
      negativeMargin > 0 ? `${formatInt(negativeMargin)} negative-margin SKU(s)` : `${formatInt(belowTarget)} SKU(s) below target`
    );
    setText(
      "drHeroMarginDetail",
      `Below-target exposure ${formatCurrency(kpis.below_target_margin_revenue)} | negative-margin exposure ${formatCurrency(kpis.negative_margin_revenue)}`
    );

    setText(
      "drOwnershipNarrative",
      inheritedShare != null && inheritedShare > 20
        ? `${formatPct(inheritedShare, false)} of visible revenue is inherited into the current-owner book.`
        : directShare != null
          ? `${formatPct(directShare, false)} of visible revenue is direct to the current-owner book.`
          : "Ownership mix is not fully available in this scope."
    );
    setText(
      "drOwnershipNarrativeDetail",
      `${formatInt(kpis.current_owned_customers)} owned customers | ${formatInt(kpis.inherited_customers)} inherited accounts | ${mappingLabel}`
    );
    setText(
      "drOwnershipModeNarrative",
      attribution.roster_mode === "include_former" && attribution.attribution_mode === "historical_rep"
        ? `${modeLabel} with former reps included.`
        : `${modeLabel}.`
    );
    setText(
      "drOwnershipBridgeNarrative",
      bridge.available && succession.available
        ? `${mappingLabel} plus ${formatInt(succession.rows)} successor rule(s) are active.`
        : bridge.available
          ? `${mappingLabel} are active in this view.`
          : snapshot.available
            ? `Using customer snapshot ownership because no bridge assignments were available in scope.`
            : "Ownership mapping is unresolved for at least part of this scope."
    );

    const watchpoints = [
      {
        label: inheritedShare != null && inheritedShare > 20 ? "Inherited exposure" : "Direct book",
        note:
          inheritedShare != null
            ? `${formatPct(inheritedShare, false)} inherited`
            : directShare != null
              ? `${formatPct(directShare, false)} direct`
              : "Mix unavailable",
      },
      {
        label: "Unassigned customers",
        note: `${formatInt(kpis.unassigned_customers)} needing review`,
      },
      {
        label: "Transfer flow",
        note: `${formatCurrency(kpis.transferred_in_revenue)} in | ${formatCurrency(kpis.transferred_out_revenue)} out`,
      },
    ];
    setHTML(
      "drOwnershipWatchList",
      watchpoints
        .map(
          (item) =>
            `<li><strong>${escapeHtml(item.label)}</strong><span>${escapeHtml(item.note)}</span></li>`
        )
        .join("")
    );
  };

  const latestTrendText = (labels, revenue, profit) => {
    if (!labels?.length) return "";
    const idx = labels.length - 1;
    const month = labels[idx] || "Latest";
    const revText = formatCurrency(revenue?.[idx]);
    const profitText = formatCurrency(profit?.[idx]);
    return `Latest ${month}: Revenue ${revText} | Profit ${profitText}`;
  };

  const renderTrend = () => {
    const canvasId = "drTrend";
    const canvas = getCanvas(canvasId);
    if (!canvas || !ChartLib) return;

    const trendRoot = currentPayload?.trend || {};
    const trend = trendRoot[trendGrain] || trendRoot.monthly || currentPayload?.charts?.trend || {};

    const labels = Array.isArray(trend.labels) ? trend.labels : [];
    const revenue = Array.isArray(trend.revenue) ? trend.revenue : [];
    const profit = Array.isArray(trend.profit) ? trend.profit : [];
    const margin = Array.isArray(trend.margin_pct) ? trend.margin_pct : [];
    const hasData = labels.length > 0;
    toggleEmpty(
      canvasId,
      !hasData,
      trendGrain === "weekly" ? "No weekly trend is available for the selected filters." : "No monthly trend is available for the selected filters."
    );

    destroyChart("trend");
    if (!hasData) {
      setText("drTrendNarrative", "No trend history is available inside the current filter window.");
      return;
    }

    const datasets = [
      {
        label: "Revenue",
        data: revenue,
        borderColor: "#0d6efd",
        backgroundColor: "rgba(13,110,253,0.10)",
        borderWidth: 3,
        tension: 0.25,
        pointRadius: labels.length === 1 ? 5 : 2,
        pointHoverRadius: 5,
        fill: true,
      },
    ];

    if (profit.some((v) => safeOptional(v) != null)) {
      datasets.push({
        label: "Profit",
        data: profit,
        borderColor: "#198754",
        backgroundColor: "rgba(25,135,84,0.10)",
        borderWidth: 2.5,
        tension: 0.25,
        pointRadius: labels.length === 1 ? 5 : 2,
        pointHoverRadius: 5,
        fill: true,
      });
    }

    if (margin.some((v) => safeOptional(v) != null)) {
      datasets.push({
        label: "Margin %",
        data: margin,
        borderColor: "#fd7e14",
        backgroundColor: "rgba(253,126,20,0.10)",
        borderWidth: 2,
        tension: 0.25,
        yAxisID: "y1",
        pointRadius: labels.length === 1 ? 5 : 2,
        pointHoverRadius: 5,
      });
    }

    if (trendRolling) {
      const rollingRevenue = trendGrain === "weekly" ? trend.rolling_revenue_4w : trend.rolling_revenue_3m;
      if (Array.isArray(rollingRevenue) && rollingRevenue.some((v) => safeOptional(v) != null)) {
        datasets.push({
          label: trendGrain === "weekly" ? "Revenue 4W Avg" : "Revenue 3M Avg",
          data: rollingRevenue,
          borderColor: "#6c757d",
          backgroundColor: "rgba(108,117,125,0.08)",
          borderWidth: 2,
          borderDash: [6, 4],
          tension: 0.25,
          pointRadius: 0,
        });
      }
      const rollingProfit = trend.rolling_profit_3m;
      if (trendGrain === "monthly" && Array.isArray(rollingProfit) && rollingProfit.some((v) => safeOptional(v) != null)) {
        datasets.push({
          label: "Profit 3M Avg",
          data: rollingProfit,
          borderColor: "#20c997",
          backgroundColor: "rgba(32,201,151,0.08)",
          borderWidth: 2,
          borderDash: [6, 4],
          tension: 0.25,
          pointRadius: 0,
        });
      }
    }

    const subtitle = latestTrendText(labels, revenue, profit);
    charts.trend = new ChartLib(canvas, {
      type: "line",
      data: {
        labels,
        datasets,
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: {
            position: "bottom",
            labels: { usePointStyle: true, boxWidth: 10 },
          },
          subtitle: { display: !!subtitle, text: subtitle },
          tooltip: {
            callbacks: {
              label: (ctx) => {
                const label = ctx.dataset?.label || "Value";
                if (ctx.dataset?.yAxisID === "y1") return `${label}: ${formatPct(ctx.raw, false)}`;
                return `${label}: ${formatCurrency(ctx.raw)}`;
              },
            },
          },
        },
        scales: {
          x: {
            grid: { color: "rgba(148,163,184,0.10)" },
          },
          y: {
            beginAtZero: true,
            ticks: {
              callback: (value) => formatCurrency(value),
            },
            grid: { color: "rgba(148,163,184,0.14)" },
          },
          y1: {
            beginAtZero: true,
            position: "right",
            grid: { drawOnChartArea: false },
            ticks: {
              callback: (value) => `${value}%`,
            },
          },
        },
      },
    });

    const latestRevenue = revenue.length ? revenue[revenue.length - 1] : null;
    const latestProfit = profit.length ? profit[profit.length - 1] : null;
    const latestMargin = margin.length ? margin[margin.length - 1] : null;
    setText(
      "drTrendNarrative",
      `${trendGrain === "weekly" ? "Weekly" : "Monthly"} view: ${labels.length === 1 ? "single visible period" : `${labels.length} visible periods`} with latest revenue ${formatCurrency(latestRevenue)}, profit ${formatCurrency(latestProfit)}, and margin ${formatPct(latestMargin, false)}.`
    );
  };

  const renderConcentration = () => {
    const canvasId = "drConcentration";
    const canvas = getCanvas(canvasId);
    if (!canvas || !ChartLib) return;

    const concentration = currentPayload?.charts?.concentration || {};
    const top1 = safeOptional(concentration.top_customer_share);
    const top5 = safeOptional(concentration.top5_customer_share);

    const labels = ["Top 1", "Top 5"];
    const data = [top1 == null ? null : top1 * 100, top5 == null ? null : top5 * 100];
    const hasData = data.some((v) => safeOptional(v) != null);

    toggleEmpty(canvasId, !hasData);
    destroyChart("concentration");
    if (!hasData) {
      setText("drConcentrationNarrative", "Concentration metrics are not available for the current filter window.");
      return;
    }

    charts.concentration = new ChartLib(canvas, {
      type: "bar",
      data: {
        labels,
        datasets: [
          {
            label: "Share %",
            data,
            backgroundColor: ["rgba(178,58,58,0.82)", "rgba(240,136,40,0.82)"],
            borderRadius: 10,
            maxBarThickness: 36,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: (ctx) => `Share: ${formatPct(ctx.raw, false)}`,
            },
          },
        },
        scales: {
          x: {
            grid: { display: false },
          },
          y: {
            beginAtZero: true,
            suggestedMax: Math.max(35, safeNum(data[1], 0) + 10),
            ticks: {
              callback: (value) => `${value}%`,
            },
            grid: { color: "rgba(148,163,184,0.14)" },
          },
        },
      },
    });

    const hhi = safeOptional(concentration.customer_hhi ?? currentPayload?.kpis?.customer_hhi);
    setText("drHHI", hhi == null ? NA : fmtFloat2.format(hhi));
    setText(
      "drConcentrationNarrative",
      top1 != null && top1 > 0.25
        ? `High concentration: the top customer represents ${formatPct(top1, true)} of visible revenue and the top five represent ${formatPct(top5, true)}.`
        : `Concentration is more balanced: top customer ${formatPct(top1, true)} and top five ${formatPct(top5, true)} of visible revenue.`
    );
  };

  const renderMonthlyCompare = () => {
    const canvasId = "drMonthlyCompare";
    const canvas = getCanvas(canvasId);
    if (!canvas || !ChartLib) return;

    const compare = currentPayload?.trend?.monthly_compare || currentPayload?.charts?.monthly_compare || {};
    const labels = Array.isArray(compare.labels) ? compare.labels : [];
    const revenue = Array.isArray(compare.revenue) ? compare.revenue : [];
    const revenueYoY = Array.isArray(compare.revenue_yoy) ? compare.revenue_yoy : [];
    const profit = Array.isArray(compare.profit) ? compare.profit : [];
    const profitYoY = Array.isArray(compare.profit_yoy) ? compare.profit_yoy : [];
    const weight = Array.isArray(compare.weight_lb) ? compare.weight_lb : [];
    const weightYoY = Array.isArray(compare.weight_lb_yoy) ? compare.weight_lb_yoy : [];
    const hasData = labels.length > 0 && [revenue, revenueYoY, profit, profitYoY, weight, weightYoY].some(
      (series) => Array.isArray(series) && series.some((value) => safeOptional(value) != null)
    );

    toggleEmpty(canvasId, !hasData);
    destroyChart("monthlyCompare");
    if (!hasData) {
      setText("drCompareNarrative", "Year-over-year comparison is unavailable for the selected window.");
      return;
    }

    charts.monthlyCompare = new ChartLib(canvas, {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "Revenue",
            data: revenue,
            borderColor: "#0d6efd",
            backgroundColor: "rgba(13,110,253,0.10)",
            borderWidth: 2,
            tension: 0.25,
          },
          {
            label: "Revenue YoY",
            data: revenueYoY,
            borderColor: "#6610f2",
            backgroundColor: "rgba(102,16,242,0.08)",
            borderDash: [6, 4],
            borderWidth: 2,
            tension: 0.25,
          },
          {
            label: "Profit",
            data: profit,
            borderColor: "#198754",
            backgroundColor: "rgba(25,135,84,0.08)",
            borderWidth: 2,
            tension: 0.25,
            hidden: profit.every((value) => safeOptional(value) == null),
          },
          {
            label: "Profit YoY",
            data: profitYoY,
            borderColor: "#20c997",
            backgroundColor: "rgba(32,201,151,0.08)",
            borderDash: [4, 4],
            borderWidth: 2,
            tension: 0.25,
            hidden: profitYoY.every((value) => safeOptional(value) == null),
          },
          {
            label: "Weight (lb)",
            data: weight,
            borderColor: "#fd7e14",
            backgroundColor: "rgba(253,126,20,0.08)",
            borderWidth: 2,
            tension: 0.25,
            yAxisID: "y1",
            hidden: weight.every((value) => safeOptional(value) == null),
          },
          {
            label: "Weight YoY (lb)",
            data: weightYoY,
            borderColor: "#adb5bd",
            backgroundColor: "rgba(173,181,189,0.08)",
            borderDash: [4, 4],
            borderWidth: 2,
            tension: 0.25,
            yAxisID: "y1",
            hidden: weightYoY.every((value) => safeOptional(value) == null),
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { position: "bottom", labels: { usePointStyle: true, boxWidth: 10 } },
          tooltip: {
            callbacks: {
              label: (ctx) =>
                ctx.dataset?.yAxisID === "y1"
                  ? `${ctx.dataset?.label || "Weight"}: ${formatInt(ctx.raw)}`
                  : `${ctx.dataset?.label || "Value"}: ${formatCurrency(ctx.raw)}`,
            },
          },
        },
        scales: {
          y: {
            beginAtZero: true,
            ticks: {
              callback: (value) => fmtMoney.format(value),
            },
          },
          y1: {
            beginAtZero: true,
            position: "right",
            grid: { drawOnChartArea: false },
            ticks: {
              callback: (value) => fmtInt.format(value),
            },
          },
        },
      },
    });

    const idx = labels.length - 1;
    setText(
      "drCompareNarrative",
      `Latest comparable month ${labels[idx] || "latest"}: revenue ${formatCurrency(revenue[idx])} vs ${formatCurrency(revenueYoY[idx])} last year, profit ${formatCurrency(profit[idx])}, and weight ${formatInt(weight[idx])}.`
    );
  };

  const renderOwnershipCompare = () => {
    const canvasId = "drOwnershipCompare";
    const canvas = getCanvas(canvasId);
    if (!canvas || !ChartLib) return;

    const compare = currentPayload?.charts?.ownership_compare || {};
    const historical = safeOptional(compare.historical_revenue);
    const currentOwner = safeOptional(compare.current_owner_revenue);
    const transferredIn = safeOptional(compare.transferred_in_revenue);
    const transferredOut = safeOptional(compare.transferred_out_revenue);
    const delta =
      historical != null && currentOwner != null ? currentOwner - historical : null;
    const data = [
      historical,
      currentOwner,
      transferredIn,
      transferredOut == null ? null : transferredOut * -1,
    ];
    const hasData = data.some((value) => value != null);

    toggleEmpty(canvasId, !hasData);
    destroyChart("ownershipCompare");
    if (!hasData) {
      setText("drOwnershipSummary", "No ownership comparison data for this filter window.");
      setText("drOwnershipHistoricalValue", NA);
      setText("drOwnershipCurrentValue", NA);
      setText("drOwnershipTransferInValue", NA);
      setText("drOwnershipTransferOutValue", NA);
      return;
    }

    setText("drOwnershipHistoricalValue", formatCurrency(historical));
    setText("drOwnershipCurrentValue", formatCurrency(currentOwner));
    setText("drOwnershipTransferInValue", formatCurrency(transferredIn));
    setText("drOwnershipTransferOutValue", formatCurrency(transferredOut));

    charts.ownershipCompare = new ChartLib(canvas, {
      type: "bar",
      data: {
        labels: ["Historical", "Current Owner", "Transferred In", "Transferred Out"],
        datasets: [
          {
            label: "Revenue",
            data,
            backgroundColor: [
              "rgba(148,163,184,0.88)",
              "rgba(13,110,253,0.86)",
              "rgba(25,135,84,0.84)",
              "rgba(220,53,69,0.84)",
            ],
            borderRadius: 10,
            maxBarThickness: 46,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: (ctx) => formatCurrency(ctx.raw),
            },
          },
        },
        scales: {
          y: {
            ticks: {
              callback: (value) => fmtMoney.format(value),
            },
            grid: { color: "rgba(148,163,184,0.14)" },
          },
        },
      },
    });

    setText(
      "drOwnershipSummary",
      `Historical ${formatCurrency(historical)} | Current ${formatCurrency(currentOwner)} | Delta ${formatCurrency(delta)}`
    );
  };

  const topNRows = (rows, n = 10) => (Array.isArray(rows) ? rows.slice(0, n) : []);

  const renderMix = () => {
    const products = topNRows(currentPayload?.charts?.mix || currentPayload?.charts?.top_products || currentPayload?.tables?.products || [], 10);
    const totalRevenue = products.reduce((sum, row) => sum + safeNum(row.revenue), 0);

    const mixBarCanvas = getCanvas("drMixBar");
    if (mixBarCanvas && ChartLib) {
      destroyChart("mixBar");
      const labels = products.map((r) => r.product_name || r.product_id || "Product");
      const data = products.map((r) => safeNum(r.revenue));
      toggleEmpty("drMixBar", labels.length === 0);
      if (labels.length > 0) {
        charts.mixBar = new ChartLib(mixBarCanvas, {
          type: "bar",
          data: {
            labels,
            datasets: [{ label: "Revenue", data, backgroundColor: "rgba(13,110,253,0.82)", borderRadius: 10 }],
          },
          options: {
            indexAxis: "y",
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
              legend: { display: false },
              tooltip: {
                callbacks: {
                  label: (ctx) => `${ctx.label}: ${formatCurrency(ctx.raw)}`,
                },
              },
            },
            scales: {
              x: {
                beginAtZero: true,
                ticks: { callback: (value) => formatCurrencyCompact(value) },
                grid: { color: "rgba(148,163,184,0.14)" },
              },
              y: {
                grid: { display: false },
              },
            },
          },
        });
      }
    }

    const mixCanvas = getCanvas("drMix");
    if (mixCanvas && ChartLib) {
      destroyChart("mix");
      const labels = products.map((r) => r.product_name || r.product_id || "Product");
      const data = products.map((r) => safeNum(r.revenue));
      toggleEmpty("drMix", labels.length === 0);
      if (labels.length > 0) {
        charts.mix = new ChartLib(mixCanvas, {
          type: "doughnut",
          data: { labels, datasets: [{ data }] },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
              legend: {
                position: "bottom",
                labels: { boxWidth: 12 },
              },
            },
          },
        });
      }
    }

    const leadProduct = products[0];
    setText(
      "drMixNarrative",
      leadProduct
        ? `${cleanText(leadProduct.product_name || leadProduct.product_id || "Top product")} leads the current mix at ${formatCurrency(leadProduct.revenue)}${totalRevenue > 0 ? `, ${formatPct(leadProduct.revenue / totalRevenue, true)} of the visible top-product set.` : "."}`
        : "No product mix data is available for this scope."
    );
  };

  const renderCustomersChart = () => {
    const canvas = getCanvas("drCustomers");
    if (!canvas || !ChartLib) return;

    const rows = topNRows(currentPayload?.charts?.top_customers || currentPayload?.tables?.customers || [], 10);
    const labels = rows.map((r) => r.customer_name || r.customer_id || "Customer");
    const data = rows.map((r) => safeNum(r.revenue));

    toggleEmpty("drCustomers", labels.length === 0);
    destroyChart("customers");
    if (!labels.length) return;

    charts.customers = new ChartLib(canvas, {
      type: "bar",
      data: {
        labels,
        datasets: [{ label: "Revenue", data, backgroundColor: "#0d6efd" }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: { y: { beginAtZero: true } },
        plugins: { legend: { display: false } },
      },
    });
  };

  const rowDrillUrl = (type, idValue) => {
    const id = String(idValue || "").trim();
    if (!id) return "";
    const encoded = encodeURIComponent(id);
    let base;
    if (type === "customer") {
      base = `/customers/drilldown/${encoded}`;
    } else if (type === "product") {
      base = `/products/${encoded}/drilldown`;
    } else {
      return "";
    }

    const params = new URLSearchParams(filtersQS || "");
    if (repId) {
      params.set("salesrep_id", repId);
    }
    const qs = params.toString();
    return qs ? `${base}?${qs}` : base;
  };

  const attachRowLink = (tr, url) => {
    if (!tr || !url) return;
    tr.classList.add("drill-row-link");
    tr.setAttribute("role", "link");
    tr.tabIndex = 0;
    tr.addEventListener("click", () => {
      window.location.assign(url);
    });
    tr.addEventListener("keydown", (evt) => {
      if (evt.key === "Enter" || evt.key === " ") {
        evt.preventDefault();
        window.location.assign(url);
      }
    });
  };

  const PORTFOLIO_MAP_DEFAULT_CENTER = Object.freeze([-123.11, 49.27]);
  const PORTFOLIO_MAP_THEME = Object.freeze({
    direct: "#1e5aa6",
    inherited: "#b06609",
    unassigned: "#b23a3a",
    ink: "#142033",
    halo: "#c43b31",
  });

  const portfolioMapStyle = () => ({
    version: 8,
    sources: {
      carto_light: {
        type: "raster",
        tiles: ["https://basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png"],
        tileSize: 256,
        attribution: '© <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a> © CARTO',
      },
    },
    layers: [{ id: "carto-light", type: "raster", source: "carto_light" }],
  });

  const portfolioAlpha = (hex, opacity = 1) => {
    const value = String(hex || "").replace("#", "").trim();
    if (value.length === 3) {
      const [r, g, b] = value.split("").map((part) => parseInt(part + part, 16));
      return `rgba(${r}, ${g}, ${b}, ${opacity})`;
    }
    if (value.length === 6) {
      const r = parseInt(value.slice(0, 2), 16);
      const g = parseInt(value.slice(2, 4), 16);
      const b = parseInt(value.slice(4, 6), 16);
      return `rgba(${r}, ${g}, ${b}, ${opacity})`;
    }
    return hex;
  };

  const portfolioStableHash = (value = "") => {
    const text = String(value || "");
    let hash = 0;
    for (let idx = 0; idx < text.length; idx += 1) hash = ((hash * 33) + text.charCodeAt(idx)) >>> 0;
    return hash >>> 0;
  };

  const validCoordinatePair = (latValue, lngValue) => {
    const lat = Number(latValue);
    const lng = Number(lngValue);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) return null;
    if (Math.abs(lat) < 0.0001 && Math.abs(lng) < 0.0001) return null;
    if (lat < -90 || lat > 90 || lng < -180 || lng > 180) return null;
    return [lng, lat];
  };

  const portfolioMapReferenceDate = () => {
    const params = new URLSearchParams(filtersQS || window.location.search || "");
    const rawEnd = cleanText(params.get("end")) || cleanText(currentPayload?.meta?.end_date) || cleanText(currentPayload?.meta?.window_end);
    const resolved = rawEnd ? new Date(`${rawEnd}T00:00:00`) : new Date();
    return Number.isNaN(resolved.getTime()) ? new Date() : resolved;
  };

  const portfolioSilentDays = (row = {}) => {
    const lastOrderRaw = cleanText(row.last_order_date || row.last_sale_date);
    if (!lastOrderRaw) return null;
    const lastOrder = new Date(`${lastOrderRaw}T00:00:00`);
    if (Number.isNaN(lastOrder.getTime())) return null;
    const ref = portfolioMapReferenceDate();
    return Math.max(0, Math.round((ref.getTime() - lastOrder.getTime()) / 86400000));
  };

  const portfolioPointColor = (row = {}) => {
    const attribution = cleanText(row.revenue_attribution_type).toLowerCase();
    if (attribution === "inherited") return PORTFOLIO_MAP_THEME.inherited;
    if (attribution === "unassigned") return PORTFOLIO_MAP_THEME.unassigned;
    return PORTFOLIO_MAP_THEME.direct;
  };

  const portfolioQuantile = (values = [], q = 0.5) => {
    const sorted = (values || []).filter((value) => Number.isFinite(value)).sort((a, b) => a - b);
    if (!sorted.length) return 0;
    const clamped = Math.max(0, Math.min(1, q));
    const index = (sorted.length - 1) * clamped;
    const lower = Math.floor(index);
    const upper = Math.ceil(index);
    if (lower === upper) return sorted[lower];
    const weight = index - lower;
    return sorted[lower] + ((sorted[upper] - sorted[lower]) * weight);
  };

  const buildPortfolioRadiusScale = (rows = []) => {
    const revenues = (rows || [])
      .map((row) => safeNum(row.revenue))
      .filter((value) => Number.isFinite(value) && value > 0)
      .sort((a, b) => a - b);
    const minRadius = 7;
    const maxRadius = 24;
    if (!revenues.length) return () => 8;
    const domainMin = revenues.length >= 6 ? portfolioQuantile(revenues, 0.1) : revenues[0];
    const domainMax = revenues.length >= 6 ? portfolioQuantile(revenues, 0.92) : revenues[revenues.length - 1];
    if (!Number.isFinite(domainMin) || !Number.isFinite(domainMax) || Math.abs(domainMax - domainMin) < 0.0001) {
      const fallback = revenues.length === 1 ? 14 : (minRadius + maxRadius) / 2;
      return () => fallback;
    }
    return (revenueValue) => {
      const revenue = Math.max(safeNum(revenueValue), 0);
      if (revenue <= 0) return 8;
      const clamped = Math.max(domainMin, Math.min(revenue, domainMax));
      const normalized = Math.max(0, Math.min(1, (clamped - domainMin) / (domainMax - domainMin)));
      return +(minRadius + (Math.sqrt(normalized) * (maxRadius - minRadius))).toFixed(2);
    };
  };

  const averageCoordinates = (coords = []) => {
    if (!coords.length) return PORTFOLIO_MAP_DEFAULT_CENTER.slice();
    return [
      +(coords.reduce((sum, [lng]) => sum + lng, 0) / coords.length).toFixed(6),
      +(coords.reduce((sum, [, lat]) => sum + lat, 0) / coords.length).toFixed(6),
    ];
  };

  const buildPortfolioCoordinateMaps = (rows = []) => {
    const territoryBuckets = new Map();
    const cityBuckets = new Map();
    const exactCoords = [];
    const pushBucket = (map, key, coords) => {
      if (!key) return;
      if (!map.has(key)) map.set(key, []);
      map.get(key).push(coords);
    };
    (rows || []).forEach((row) => {
      const coords = validCoordinatePair(row.delivery_lat, row.delivery_lng);
      if (!coords) return;
      exactCoords.push(coords);
      pushBucket(territoryBuckets, cleanText(row.territory_name).toLowerCase(), coords);
      pushBucket(cityBuckets, `${cleanText(row.delivery_city).toLowerCase()}|${cleanText(row.delivery_province).toLowerCase()}`.replace(/^\|+|\|+$/g, ""), coords);
    });
    return {
      defaultCenter: averageCoordinates(exactCoords),
      territoryCentroids: new Map(Array.from(territoryBuckets.entries()).map(([key, coords]) => [key, averageCoordinates(coords)])),
      cityCentroids: new Map(Array.from(cityBuckets.entries()).map(([key, coords]) => [key, averageCoordinates(coords)])),
    };
  };

  const resolvePortfolioCoordinate = (row = {}, coordinateMaps) => {
    const exact = validCoordinatePair(row.delivery_lat, row.delivery_lng);
    if (exact) return { coordinates: exact, approx: false, approx_reason: "" };
    const cityKey = `${cleanText(row.delivery_city).toLowerCase()}|${cleanText(row.delivery_province).toLowerCase()}`.replace(/^\|+|\|+$/g, "");
    const territoryKey = cleanText(row.territory_name).toLowerCase();
    const base =
      coordinateMaps.cityCentroids.get(cityKey)
      || coordinateMaps.territoryCentroids.get(territoryKey)
      || coordinateMaps.defaultCenter
      || PORTFOLIO_MAP_DEFAULT_CENTER;
    const hash = portfolioStableHash(row.customer_id || row.customer_name || territoryKey || cityKey || "portfolio");
    const angle = ((hash % 360) * Math.PI) / 180;
    const orbit = 0.0038 + ((hash % 5) * 0.0011);
    const approxReason = coordinateMaps.cityCentroids.get(cityKey)
      ? "city_centroid"
      : coordinateMaps.territoryCentroids.get(territoryKey)
        ? "territory_centroid"
        : "portfolio_centroid";
    return {
      coordinates: [
        +(base[0] + (Math.cos(angle) * orbit * 1.12)).toFixed(6),
        +(base[1] + (Math.sin(angle) * orbit)).toFixed(6),
      ],
      approx: true,
      approx_reason: approxReason,
    };
  };

  const spreadPortfolioFeatures = (features = []) => {
    const buckets = new Map();
    (features || []).forEach((feature) => {
      const approx = Number(feature?.properties?.approx) === 1;
      const bucketSize = approx ? 0.0042 : 0.0012;
      const sourceLng = Number(feature?.properties?.source_lng);
      const sourceLat = Number(feature?.properties?.source_lat);
      if (!Number.isFinite(sourceLng) || !Number.isFinite(sourceLat)) return;
      const key = `${approx ? "approx" : "exact"}:${Math.round(sourceLng / bucketSize)}:${Math.round(sourceLat / bucketSize)}`;
      if (!buckets.has(key)) buckets.set(key, []);
      buckets.get(key).push(feature);
    });

    buckets.forEach((group) => {
      if (!group.length) return;
      const anchorLng = group.reduce((sum, feature) => sum + Number(feature.properties.source_lng || 0), 0) / group.length;
      const anchorLat = group.reduce((sum, feature) => sum + Number(feature.properties.source_lat || 0), 0) / group.length;
      const lngFactor = 1 / Math.max(Math.cos((anchorLat * Math.PI) / 180), 0.35);
      const sorted = group.slice().sort((left, right) => {
        const riskDelta = Number(right?.properties?.is_risk || 0) - Number(left?.properties?.is_risk || 0);
        if (riskDelta) return riskDelta;
        return safeNum(right?.properties?.revenue) - safeNum(left?.properties?.revenue);
      });
      sorted.forEach((feature, index) => {
        feature.properties.overlap_count = sorted.length;
        feature.properties.overlap_index = index;
        if (index === 0) {
          feature.geometry.coordinates = [+(anchorLng).toFixed(6), +(anchorLat).toFixed(6)];
          return;
        }
        const approx = Number(feature?.properties?.approx) === 1;
        const ring = Math.floor((index - 1) / 6) + 1;
        const slot = (index - 1) % 6;
        const angleJitter = ((portfolioStableHash(feature?.properties?.customer_id || feature?.properties?.customer_name || index) % 18) - 9) * (Math.PI / 180);
        const angle = ((slot / 6) * Math.PI * 2) + angleJitter;
        const bubbleRadius = safeNum(feature?.properties?.radius, 10);
        const latOrbit = ((approx ? 0.0033 : 0.0017) * ring) + (bubbleRadius * 0.000045);
        const lngOrbit = latOrbit * lngFactor;
        feature.geometry.coordinates = [
          +(anchorLng + (Math.cos(angle) * lngOrbit)).toFixed(6),
          +(anchorLat + (Math.sin(angle) * latOrbit)).toFixed(6),
        ];
      });
    });
    return features;
  };

  const portfolioConvexHull = (points = []) => {
    const deduped = Array.from(
      new Map(
        (points || [])
          .filter((point) => Array.isArray(point) && point.length === 2 && Number.isFinite(point[0]) && Number.isFinite(point[1]))
          .map((point) => [`${Number(point[0]).toFixed(6)}:${Number(point[1]).toFixed(6)}`, [Number(point[0]), Number(point[1])]])
      ).values()
    );
    if (deduped.length <= 2) return deduped;
    const cross = (origin, a, b) => ((a[0] - origin[0]) * (b[1] - origin[1])) - ((a[1] - origin[1]) * (b[0] - origin[0]));
    const sorted = deduped.slice().sort((a, b) => (a[0] === b[0] ? a[1] - b[1] : a[0] - b[0]));
    const lower = [];
    sorted.forEach((point) => {
      while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], point) <= 0) lower.pop();
      lower.push(point);
    });
    const upper = [];
    sorted.slice().reverse().forEach((point) => {
      while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], point) <= 0) upper.pop();
      upper.push(point);
    });
    lower.pop();
    upper.pop();
    return lower.concat(upper);
  };

  const portfolioTerritoryPolygon = (points = []) => {
    const hull = portfolioConvexHull(points);
    if (!hull.length) return null;
    const lngs = hull.map((point) => point[0]);
    const lats = hull.map((point) => point[1]);
    const spreadLng = Math.max(...lngs) - Math.min(...lngs);
    const spreadLat = Math.max(...lats) - Math.min(...lats);
    const radiusLng = Math.max(0.06, spreadLng * 0.38);
    const radiusLat = Math.max(0.04, spreadLat * 0.38);
    if (hull.length === 1) {
      const [centerLng, centerLat] = hull[0];
      const coords = [];
      for (let idx = 0; idx < 14; idx += 1) {
        const angle = (idx / 14) * Math.PI * 2;
        coords.push([
          +(centerLng + (Math.cos(angle) * radiusLng)).toFixed(6),
          +(centerLat + (Math.sin(angle) * radiusLat)).toFixed(6),
        ]);
      }
      coords.push(coords[0]);
      return coords;
    }
    return hull.concat([hull[0]]);
  };

  const buildPortfolioTerritoryFeatures = (features = []) => {
    const grouped = new Map();
    (features || []).forEach((feature) => {
      const territory = cleanText(feature?.properties?.territory_name) || "Unassigned";
      if (!grouped.has(territory)) grouped.set(territory, []);
      grouped.get(territory).push(feature);
    });
    return Array.from(grouped.entries()).map(([territory, territoryFeatures], index) => {
      const polygon = portfolioTerritoryPolygon(territoryFeatures.map((feature) => feature.geometry.coordinates));
      if (!polygon) return null;
      const attributionRevenue = new Map();
      territoryFeatures.forEach((feature) => {
        const attribution = cleanText(feature?.properties?.revenue_attribution_type) || "direct";
        attributionRevenue.set(attribution, (attributionRevenue.get(attribution) || 0) + safeNum(feature?.properties?.revenue));
      });
      const dominantAttribution = Array.from(attributionRevenue.entries()).sort((a, b) => b[1] - a[1])[0]?.[0] || "direct";
      const swatch = portfolioPointColor({ revenue_attribution_type: dominantAttribution });
      return {
        type: "Feature",
        id: index + 1,
        properties: {
          territory,
          color: swatch,
        },
        geometry: { type: "Polygon", coordinates: [polygon] },
      };
    }).filter(Boolean);
  };

  const portfolioPointRowFromFeature = (props = {}) => ({
    customer_id: props.customer_id || "",
    customer_name: props.customer_name || props.customer_id || "Customer",
    account_owner_name: props.account_owner_name || "",
    account_owner_id: props.account_owner_id || "",
    territory_name: props.territory_name || "",
    revenue: safeNum(props.revenue),
    last_order_date: props.last_order_date || "",
    delivery_city: props.delivery_city || "",
    delivery_province: props.delivery_province || "",
    revenue_attribution_type: props.revenue_attribution_type || "direct",
    silent_days: props.silent_days == null ? null : Number(props.silent_days),
    approx: Number(props.approx) === 1,
    approx_reason: props.approx_reason || "",
  });

  const portfolioPopupHtml = (row = {}) => {
    const url = rowDrillUrl("customer", row.customer_id || row.customer_name);
    const name = cleanText(row.customer_name || row.customer_id || "Customer");
    const nameHtml = url
      ? `<a class="srpd-map-popup-name" href="${escapeHtml(url)}">${escapeHtml(name)}</a>`
      : `<div class="srpd-map-popup-name">${escapeHtml(name)}</div>`;
    const attribution = cleanText(row.revenue_attribution_type).toLowerCase();
    const attributionLabel =
      attribution === "inherited" ? "Inherited book" : attribution === "unassigned" ? "Needs review" : "Direct book";
    const silentDays = row.silent_days == null ? portfolioSilentDays(row) : Number(row.silent_days);
    const approxLabel = row.approx_reason === "city_centroid"
      ? "Location estimated from nearby city centroid"
      : row.approx_reason === "territory_centroid"
        ? "Location estimated from territory centroid"
        : row.approx
          ? "Location estimated from visible portfolio coverage"
          : "";
    return `
      <div class="srpd-map-popup">
        ${nameHtml}
        <div class="srpd-map-popup-meta">${escapeHtml(businessRepName(row.account_owner_name, row.account_owner_id, "Needs Mapping"))}${row.territory_name ? ` · ${escapeHtml(row.territory_name)}` : ""}</div>
        <div class="srpd-map-popup-row">Revenue <strong>${formatCurrency(row.revenue)}</strong></div>
        <div class="srpd-map-popup-row">Last invoice <strong>${escapeHtml(row.last_order_date || NA)}</strong></div>
        ${silentDays != null ? `<div class="srpd-map-popup-row">Silent <strong>${formatInt(silentDays)}d</strong></div>` : ""}
        <div class="srpd-map-popup-row">Attribution <strong>${escapeHtml(attributionLabel)}</strong></div>
        ${approxLabel ? `<div class="srpd-map-popup-note">${escapeHtml(approxLabel)}</div>` : ""}
        ${url ? `<a class="srpd-map-popup-open" href="${escapeHtml(url)}">Open customer drilldown</a>` : ""}
      </div>
    `;
  };

  const setPortfolioMapEmpty = (show, message = "Portfolio map will render when customer coordinates are available.") => {
    const emptyEl = document.getElementById("drPortfolioMapEmpty");
    if (!emptyEl) return;
    emptyEl.textContent = message;
    emptyEl.classList.toggle("is-visible", !!show);
  };

  const buildPortfolioFeatures = (rows = []) => {
    const visibleRows = Array.isArray(rows) ? rows : [];
    const coordinateMaps = buildPortfolioCoordinateMaps(visibleRows);
    const radiusForRevenue = buildPortfolioRadiusScale(visibleRows);
    const features = visibleRows.map((row, index) => {
      const location = resolvePortfolioCoordinate(row, coordinateMaps);
      const silentDays = portfolioSilentDays(row);
      return {
        type: "Feature",
        id: index + 1,
        properties: {
          customer_id: row.customer_id || "",
          customer_name: row.customer_name || row.customer_id || "Customer",
          account_owner_name: row.account_owner_name || "",
          account_owner_id: row.account_owner_id || "",
          territory_name: row.territory_name || "",
          delivery_city: row.delivery_city || "",
          delivery_province: row.delivery_province || "",
          revenue: safeNum(row.revenue),
          last_order_date: row.last_order_date || "",
          revenue_attribution_type: row.revenue_attribution_type || "direct",
          radius: radiusForRevenue(row.revenue),
          color: portfolioPointColor(row),
          approx: location.approx ? 1 : 0,
          approx_reason: location.approx_reason || "",
          is_risk: silentDays != null && silentDays > 45 ? 1 : 0,
          silent_days: silentDays,
          overlap_count: 1,
          overlap_index: 0,
          source_lng: location.coordinates[0],
          source_lat: location.coordinates[1],
        },
        geometry: { type: "Point", coordinates: location.coordinates },
      };
    });
    spreadPortfolioFeatures(features);
    return features;
  };

  const fitPortfolioMap = (features = []) => {
    if (!portfolioMap || !portfolioMapReady || !window.maplibregl) return;
    if (!Array.isArray(features) || !features.length) {
      portfolioMap.easeTo({ center: PORTFOLIO_MAP_DEFAULT_CENTER, zoom: 6.4, duration: 700 });
      return;
    }
    if (features.length === 1) {
      portfolioMap.easeTo({ center: features[0].geometry.coordinates, zoom: 10.8, duration: 700 });
      return;
    }
    const bounds = new window.maplibregl.LngLatBounds(features[0].geometry.coordinates, features[0].geometry.coordinates);
    features.forEach((feature) => bounds.extend(feature.geometry.coordinates));
    portfolioMap.fitBounds(bounds, {
      padding: { top: 56, right: 56, bottom: 56, left: 56 },
      maxZoom: 10.4,
      duration: 850,
    });
  };

  const fitPortfolioTerritory = (feature) => {
    if (!portfolioMap || !window.maplibregl || !feature?.geometry?.coordinates?.[0]?.length) return;
    const coords = feature.geometry.coordinates[0];
    const bounds = new window.maplibregl.LngLatBounds(coords[0], coords[0]);
    coords.forEach((coord) => bounds.extend(coord));
    portfolioMap.fitBounds(bounds, {
      padding: { top: 48, right: 48, bottom: 48, left: 48 },
      maxZoom: 10.6,
      duration: 700,
    });
  };

  const setPortfolioHoveredFeature = (feature = null) => {
    if (!portfolioMap) return;
    const nextId = feature && feature.id != null ? feature.id : null;
    try {
      if (portfolioMapHoveredId != null && portfolioMapHoveredId !== nextId) {
        portfolioMap.setFeatureState({ source: "portfolio-customers", id: portfolioMapHoveredId }, { hover: false });
      }
      if (nextId != null && portfolioMapHoveredId !== nextId) {
        portfolioMap.setFeatureState({ source: "portfolio-customers", id: nextId }, { hover: true });
      }
    } catch (_err) {
      /* transient style refresh */
    }
    portfolioMapHoveredId = nextId;
  };

  const animatePortfolioHalo = () => {
    if (
      !portfolioMap
      || !portfolioMapReady
      || !portfolioMap.getLayer("portfolio-risk-halo-fill")
      || !portfolioMap.getLayer("portfolio-risk-halo-ring")
    ) {
      portfolioMapAnimationId = null;
      return;
    }
    if (portfolioMapAnimationId) cancelAnimationFrame(portfolioMapAnimationId);
    const step = (Date.now() % 2400) / 2400;
    const pulse = Math.sin(step * Math.PI);
    const haloOpacity = 0.12 + (0.18 * pulse);
    const haloRadiusPadding = 5.6 + (3.8 * pulse);
    const ringOpacity = 0.28 + (0.28 * pulse);
    const ringWidth = 1.3 + (1.3 * pulse);
    const ringRadiusPadding = 4 + (2.8 * pulse);
    try {
      portfolioMap.setPaintProperty("portfolio-risk-halo-fill", "circle-opacity", haloOpacity);
      portfolioMap.setPaintProperty("portfolio-risk-halo-fill", "circle-radius", ["+", ["get", "radius"], haloRadiusPadding]);
      portfolioMap.setPaintProperty("portfolio-risk-halo-ring", "circle-stroke-opacity", ringOpacity);
      portfolioMap.setPaintProperty("portfolio-risk-halo-ring", "circle-stroke-width", ringWidth);
      portfolioMap.setPaintProperty("portfolio-risk-halo-ring", "circle-radius", ["+", ["get", "radius"], ringRadiusPadding]);
    } catch (_err) {
      /* ignore transient style swaps */
    }
    portfolioMapAnimationId = requestAnimationFrame(animatePortfolioHalo);
  };

  const pushPortfolioMapData = (rows = []) => {
    if (!portfolioMap || !portfolioMapReady) {
      portfolioMapPendingRows = Array.isArray(rows) ? rows : [];
      return;
    }
    const features = buildPortfolioFeatures(rows);
    const territoryFeatures = buildPortfolioTerritoryFeatures(features);
    ["portfolio-risk-halo-fill", "portfolio-risk-halo-ring", "portfolio-bubble-glow", "portfolio-points", "portfolio-point-core", "portfolio-territories-outline", "portfolio-territories-fill"].forEach((id) => {
      if (portfolioMap.getLayer(id)) portfolioMap.removeLayer(id);
    });
    if (portfolioMap.getSource("portfolio-customers")) portfolioMap.removeSource("portfolio-customers");
    if (portfolioMap.getSource("portfolio-territories")) portfolioMap.removeSource("portfolio-territories");

    if (territoryFeatures.length) {
      portfolioMap.addSource("portfolio-territories", {
        type: "geojson",
        data: { type: "FeatureCollection", features: territoryFeatures },
        generateId: true,
      });
      portfolioMap.addLayer({
        id: "portfolio-territories-fill",
        type: "fill",
        source: "portfolio-territories",
        paint: {
          "fill-color": ["coalesce", ["get", "color"], portfolioAlpha(PORTFOLIO_MAP_THEME.direct, 0.08)],
          "fill-opacity": 0.12,
        },
      });
      portfolioMap.addLayer({
        id: "portfolio-territories-outline",
        type: "line",
        source: "portfolio-territories",
        paint: {
          "line-color": ["coalesce", ["get", "color"], PORTFOLIO_MAP_THEME.direct],
          "line-opacity": 0.55,
          "line-width": 1.2,
          "line-dasharray": [2, 1.4],
        },
      });
    }

    portfolioMap.addSource("portfolio-customers", {
      type: "geojson",
      data: { type: "FeatureCollection", features },
      cluster: false,
      generateId: true,
    });

    const customerPointFilter = ["!", ["has", "point_count"]];
    const hoverState = ["boolean", ["feature-state", "hover"], false];
    const approxState = ["==", ["get", "approx"], 1];

    portfolioMap.addLayer({
      id: "portfolio-risk-halo-fill",
      type: "circle",
      source: "portfolio-customers",
      filter: ["all", customerPointFilter, ["==", ["get", "is_risk"], 1]],
      paint: {
        "circle-radius": ["+", ["get", "radius"], 5.8],
        "circle-color": portfolioAlpha(PORTFOLIO_MAP_THEME.halo, 0.38),
        "circle-opacity": 0.18,
        "circle-blur": 0.72,
      },
    });

    portfolioMap.addLayer({
      id: "portfolio-risk-halo-ring",
      type: "circle",
      source: "portfolio-customers",
      filter: ["all", customerPointFilter, ["==", ["get", "is_risk"], 1]],
      paint: {
        "circle-radius": ["+", ["get", "radius"], 4.1],
        "circle-color": "rgba(0,0,0,0)",
        "circle-stroke-color": portfolioAlpha(PORTFOLIO_MAP_THEME.halo, 0.84),
        "circle-stroke-width": 1.8,
        "circle-stroke-opacity": 0.42,
      },
    });

    portfolioMap.addLayer({
      id: "portfolio-bubble-glow",
      type: "circle",
      source: "portfolio-customers",
      filter: customerPointFilter,
      paint: {
        "circle-radius": ["+", ["get", "radius"], ["case", hoverState, 4.4, 3]],
        "circle-color": ["get", "color"],
        "circle-opacity": ["case", hoverState, 0.28, ["case", approxState, 0.14, 0.18]],
        "circle-blur": 0.78,
      },
    });

    portfolioMap.addLayer({
      id: "portfolio-points",
      type: "circle",
      source: "portfolio-customers",
      filter: customerPointFilter,
      paint: {
        "circle-radius": ["+", ["get", "radius"], ["case", hoverState, 1.5, 0]],
        "circle-color": ["get", "color"],
        "circle-opacity": ["case", hoverState, 0.98, ["case", approxState, 0.76, 0.92]],
        "circle-stroke-width": ["case", hoverState, 3, 2],
        "circle-stroke-color": ["case", hoverState, "#ffffff", PORTFOLIO_MAP_THEME.ink],
        "circle-stroke-opacity": ["case", hoverState, 1, 0.94],
      },
    });

    portfolioMap.addLayer({
      id: "portfolio-point-core",
      type: "circle",
      source: "portfolio-customers",
      filter: customerPointFilter,
      paint: {
        "circle-radius": ["max", 2.5, ["*", ["get", "radius"], 0.34]],
        "circle-color": "#ffffff",
        "circle-opacity": ["case", hoverState, 0.72, ["case", approxState, 0.4, 0.56]],
      },
    });

    const mapEl = document.getElementById("drPortfolioMap");
    const exactCount = features.filter((feature) => Number(feature?.properties?.approx) !== 1).length;
    const approxCount = Math.max(features.length - exactCount, 0);
    if (mapEl) {
      mapEl.dataset.customerCount = String(features.length);
      mapEl.dataset.territoryCount = String(territoryFeatures.length);
      mapEl.dataset.exactCount = String(exactCount);
      mapEl.dataset.approxCount = String(approxCount);
      mapEl.dataset.haloEnabled = portfolioMap.getLayer("portfolio-risk-halo-ring") ? "1" : "0";
      mapEl.dataset.groupingEnabled = "0";
    }

    animatePortfolioHalo();
    setPortfolioMapEmpty(!features.length, !features.length ? "No customer accounts are visible for this rep and filter scope." : undefined);
    setText(
      "drPortfolioMapSummary",
      `${formatInt(features.length)} mapped account(s) · ${formatInt(exactCount)} exact · ${formatInt(approxCount)} fallback`
    );
    fitPortfolioMap(features);
  };

  const ensurePortfolioMap = () => {
    const mapEl = document.getElementById("drPortfolioMap");
    if (!mapEl) return false;
    if (!window.maplibregl) {
      const scriptEl = document.querySelector("script[src*='maplibre-gl']");
      if (scriptEl && scriptEl.dataset.drPortfolioRetry !== "1") {
        scriptEl.dataset.drPortfolioRetry = "1";
        scriptEl.addEventListener("load", () => renderOperatingConsole(), { once: true });
      }
      setPortfolioMapEmpty(true, "Map library is still loading.");
      setText("drPortfolioMapSummary", "Waiting for map library...");
      return false;
    }
    if (portfolioMap) return true;
    portfolioMap = new window.maplibregl.Map({
      container: "drPortfolioMap",
      style: portfolioMapStyle(),
      center: PORTFOLIO_MAP_DEFAULT_CENTER,
      zoom: 6.4,
      maxBounds: [[-145, 40], [-50, 75]],
    });
    portfolioMap.addControl(new window.maplibregl.NavigationControl({ showCompass: false }), "top-right");
    portfolioMap.addControl(new window.maplibregl.ScaleControl({ maxWidth: 100, unit: "metric" }), "bottom-left");
    portfolioMapPopup = new window.maplibregl.Popup({
      closeButton: true,
      closeOnClick: false,
      maxWidth: "280px",
      className: "srpd-map-popup-shell",
    });
    portfolioMap.on("style.load", () => {
      portfolioMapReady = true;
      portfolioMap.resize();
      pushPortfolioMapData(portfolioMapPendingRows);
    });
    portfolioMap.on("mouseenter", "portfolio-points", (evt) => {
      portfolioMap.getCanvas().style.cursor = "pointer";
      const feature = evt.features?.[0] || null;
      setPortfolioHoveredFeature(feature);
      portfolioMapPopup?.setLngLat(evt.lngLat).setHTML(portfolioPopupHtml(portfolioPointRowFromFeature(feature?.properties || {}))).addTo(portfolioMap);
    });
    portfolioMap.on("mousemove", "portfolio-points", (evt) => {
      const feature = evt.features?.[0] || null;
      setPortfolioHoveredFeature(feature);
      portfolioMapPopup?.setLngLat(evt.lngLat).setHTML(portfolioPopupHtml(portfolioPointRowFromFeature(feature?.properties || {}))).addTo(portfolioMap);
    });
    portfolioMap.on("mouseleave", "portfolio-points", () => {
      portfolioMap.getCanvas().style.cursor = "";
      setPortfolioHoveredFeature(null);
      portfolioMapPopup?.remove();
    });
    ["portfolio-territories-fill", "portfolio-territories-outline"].forEach((layerId) => {
      portfolioMap.on("mouseenter", layerId, () => {
        portfolioMap.getCanvas().style.cursor = "pointer";
      });
      portfolioMap.on("mouseleave", layerId, () => {
        portfolioMap.getCanvas().style.cursor = "";
      });
      portfolioMap.on("click", layerId, (evt) => {
        const feature = evt.features?.[0];
        if (feature) fitPortfolioTerritory(feature);
      });
    });
    portfolioMap.on("click", "portfolio-points", (evt) => {
      const props = evt.features?.[0]?.properties || {};
      const row = portfolioPointRowFromFeature(props);
      const url = rowDrillUrl("customer", row.customer_id || row.customer_name);
      if (url) {
        window.location.assign(url);
      } else {
        portfolioMapPopup?.setLngLat(evt.lngLat).setHTML(portfolioPopupHtml(row)).addTo(portfolioMap);
      }
    });
    portfolioMap.on("click", (evt) => {
      const rendered = portfolioMap.queryRenderedFeatures(evt.point, { layers: ["portfolio-points"] });
      if (!rendered.length) {
        setPortfolioHoveredFeature(null);
        portfolioMapPopup?.remove();
      }
    });
    return true;
  };

  const renderGapMatrix = () => {
    const head = document.getElementById("drGapMatrixHead");
    const body = document.getElementById("drGapMatrixBody");
    if (!head || !body) return;
    const matrix = currentPayload?.modules?.product_gap_matrix || {};
    const columns = Array.isArray(matrix.columns) ? matrix.columns : [];
    const rows = (Array.isArray(matrix.rows) ? [...matrix.rows] : []).sort((left, right) => safeNum(right.revenue) - safeNum(left.revenue));
    const summary = matrix.summary || {};

    head.innerHTML = `
      <tr>
        <th scope="col">Customer</th>
        <th scope="col" class="text-end">Revenue</th>
        ${columns.map((column) => `<th scope="col" class="text-center">${escapeHtml(column.label || column.key || "Protein")}</th>`).join("")}
        <th scope="col" class="text-end">Open Gaps</th>
      </tr>
    `;

    if (!rows.length) {
      body.innerHTML = `<tr><td colspan="${columns.length + 3}" class="text-muted">No gap matrix is available for this scope.</td></tr>`;
      setText("drGapMatrixSummary", "No top-customer whitespace was detected in the visible scope.");
      return;
    }

    body.innerHTML = rows.map((row) => {
      const url = rowDrillUrl("customer", row.customer_id || row.customer_name);
      const customerLabel = url
        ? `<a href="${escapeHtml(url)}" class="srpd-gap-customer-link">${escapeHtml(row.customer_name || row.customer_id || NA)}</a>`
        : escapeHtml(row.customer_name || row.customer_id || NA);
      const cells = (Array.isArray(row.cells) ? row.cells : []).map((cell) => `
        <td class="text-center">
          ${cell.gap
            ? `<span class="srpd-gap-cell is-gap" title="Target gap — not yet purchased"><span aria-hidden="true">○</span><span class="sr-only">Gap</span></span>`
            : `<span class="srpd-gap-cell is-owned" title="Active buyer — revenue confirmed"><span aria-hidden="true">✓</span><span class="sr-only">Active</span></span>`
          }
        </td>
      `).join("");
      return `
        <tr>
          <td>
            <div class="srpd-entity-title">${customerLabel}</div>
            <div class="srpd-entity-sub">${escapeHtml(row.territory_name || row.account_owner_name || "")}</div>
          </td>
          <td class="text-end">${formatCurrency(row.revenue)}</td>
          ${cells}
          <td class="text-end">${formatInt(row.missing_count)}</td>
        </tr>
      `;
    }).join("");

    const leadingGap = Object.entries(summary).sort((left, right) => safeNum(right[1]) - safeNum(left[1]))[0];
    setText(
      "drGapMatrixSummary",
      leadingGap && safeNum(leadingGap[1]) > 0
        ? `${formatInt(leadingGap[1])} of the top customers are missing ${leadingGap[0].charAt(0).toUpperCase()}${leadingGap[0].slice(1)}.`
        : "The top customers already cover the core protein groups in this view."
    );
  };

  const renderSmartNotes = () => {
    const holder = document.getElementById("drSmartNotes");
    if (!holder) return;
    const notes = Array.isArray(currentPayload?.modules?.smart_notes) ? currentPayload.modules.smart_notes : [];
    if (!notes.length) {
      holder.innerHTML = '<div class="srpd-note srpd-note--neutral">No rep-specific notes are available for this scope.</div>';
      return;
    }
    holder.innerHTML = notes.map((note) => {
      const tone = cleanText(note.tone).toLowerCase() || "neutral";
      return `
        <article class="srpd-note srpd-note--${escapeHtml(tone)}">
          <div class="srpd-note-kicker">${escapeHtml(tone === "risk" ? "Risk" : tone === "warn" ? "Watch" : tone === "good" ? "Strength" : "Action")}</div>
          <div class="srpd-note-text">${escapeHtml(note.text || "")}</div>
        </article>
      `;
    }).join("");
  };

  // Normalise any array-or-{rows:[]} shaped customer list
  const _bubbleCustomerRows = () => {
    const t = currentPayload?.tables?.customers;
    if (Array.isArray(t)) return t;
    if (Array.isArray(t?.rows)) return t.rows;
    const m = currentPayload?.modules?.portfolio_map?.customers;
    if (Array.isArray(m)) return m;
    if (Array.isArray(m?.rows)) return m.rows;
    // Fall back to the top-level customer rows hydrated at boot
    return Array.isArray(customerRows) ? customerRows : [];
  };

  const renderBubbleChart = () => {
    const canvas = document.getElementById("drBubbleChart");
    if (!canvas || !ChartLib) return;
    const customers = _bubbleCustomerRows();
    if (!customers.length) return;

    // rowDrillUrl resolves customer URLs using the bundle meta URLs

    const SAPPHIRE = "#1f5f9a";
    const EMERALD = "#0f8c5a";
    const CRIMSON = "#c53939";

    const dataPoints = customers
      .filter((c) => c.revenue != null && c.margin_pct != null)
      .map((c) => {
        const health = safeNum(c.health_score ?? c.health_index_pct ?? 50);
        const r = Math.max(6, Math.min(30, 6 + (health / 100) * 24));
        const tone = health >= 75 ? EMERALD : health < 40 ? CRIMSON : SAPPHIRE;
        return {
          x: safeNum(c.revenue),
          y: safeNum(c.margin_pct),
          r,
          customer_id: c.customer_id || c.customer_name,
          customer_name: c.customer_name || c.customer_id || "Customer",
          health,
          backgroundColor: tone + "88",
          borderColor: tone,
        };
      });

    if (charts.bubble) { charts.bubble.destroy(); charts.bubble = null; }
    charts.bubble = new ChartLib(canvas, {
      type: "bubble",
      data: {
        datasets: [{
          label: "Customers",
          data: dataPoints,
          backgroundColor: dataPoints.map((d) => d.backgroundColor),
          borderColor: dataPoints.map((d) => d.borderColor),
          borderWidth: 1.5,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: (ctx) => {
                const d = ctx.raw;
                return [
                  d.customer_name,
                  `Revenue: $${d.x.toLocaleString("en-US", { maximumFractionDigits: 0 })}`,
                  `Margin: ${d.y.toFixed(1)}%`,
                  `Health: ${Math.round(d.health)}%`,
                ];
              },
            },
          },
        },
        scales: {
          x: {
            title: { display: true, text: "Revenue ($)" },
            ticks: { callback: (v) => "$" + (v >= 1_000_000 ? (v / 1_000_000).toFixed(1) + "M" : v >= 1_000 ? (v / 1_000).toFixed(0) + "K" : v) },
          },
          y: {
            title: { display: true, text: "Margin %" },
            ticks: { callback: (v) => v.toFixed(1) + "%" },
          },
        },
        onClick: (_evt, elements) => {
          if (!elements.length) return;
          const pt = dataPoints[elements[0].index];
          if (!pt?.customer_id) return;
          const url = rowDrillUrl("customer", pt.customer_id);
          if (url) window.location.href = url;
        },
      },
    });
    canvas.style.cursor = "pointer";
  };

  const renderOperatingConsole = () => {
    if (!v2Enabled) return;
    const modules = currentPayload?.modules || {};
    const mapRows = Array.isArray(modules.portfolio_map?.customers) ? modules.portfolio_map.customers : [];
    const mappedCount = buildPortfolioFeatures(mapRows).length;
    const summaryCounts = modules.product_gap_matrix?.summary || {};
    const whitespaceCount = Object.values(summaryCounts).reduce((sum, value) => sum + safeNum(value), 0);
    setText("drNavOperating", `${formatInt(mappedCount)} mapped | ${formatInt(whitespaceCount)} gaps`);
    if (ensurePortfolioMap()) {
      pushPortfolioMapData(mapRows);
    }
    try { renderBubbleChart(); } catch (_e) { /* bubble chart is non-critical */ }
    renderGapMatrix();
    renderSmartNotes();
  };

  const renderDecomposition = () => {
    const tbody = document.getElementById("drDecompTable");
    if (!tbody) return;

    const decomp = currentPayload?.decomposition || {};
    const rows = [
      ["Price", decomp.price_impact],
      ["Volume", decomp.volume_impact],
      ["Mix", decomp.mix_impact],
      ["Total", decomp.total_change],
    ];

    tbody.innerHTML = "";
    if (!rows.some((r) => safeOptional(r[1]) != null)) {
      tbody.innerHTML = '<tr><td colspan="2" class="text-muted">Not enough data for decomposition.</td></tr>';
      setText("drDecompReadout", "The visible window is too narrow to separate price, volume, and mix effects with confidence.");
    } else {
      rows.forEach(([label, value]) => {
        const tr = document.createElement("tr");
        const cls = safeNum(value) < 0 ? "text-danger" : "text-success";
        tr.innerHTML = `<td>${label}</td><td class="text-end ${cls}">${formatCurrency(value)}</td>`;
        tbody.appendChild(tr);
      });
      const strongest = rows
        .filter((row) => row[0] !== "Total" && safeOptional(row[1]) != null)
        .sort((a, b) => Math.abs(safeNum(b[1])) - Math.abs(safeNum(a[1])))[0];
      setText(
        "drDecompReadout",
        strongest
          ? `${strongest[0]} was the largest month-over-month driver at ${formatCurrency(strongest[1])}; total change was ${formatCurrency(decomp.total_change)}.`
          : "Month-over-month change is available but no dominant driver stood out."
      );
    }

    const method = decomp.methodology || "--";
    setText("drDecompMethod", `Method: ${method}`);
  };

  const renderMoversTable = (tbodyId, movers, nameKey) => {
    const tbody = document.getElementById(tbodyId);
    if (!tbody) return;

    const gainers = Array.isArray(movers?.gainers) ? movers.gainers.slice(0, 5) : [];
    const decliners = Array.isArray(movers?.decliners) ? movers.decliners.slice(0, 5) : [];
    const combined = [...gainers, ...decliners];

    tbody.innerHTML = "";
    if (!combined.length) {
      tbody.innerHTML = '<tr><td colspan="3" class="text-muted">No movers for this period.</td></tr>';
      if (tbodyId === "drMoversCustomersTable") {
        setText("drCustomerMoversNarrative", "No customer movers were detected for the current period.");
      } else if (tbodyId === "drMoversProductsTable") {
        setText("drProductMoversNarrative", "No product movers were detected for the current period.");
      }
      return;
    }

    combined.forEach((row) => {
      const name = row[nameKey] || row[`${nameKey.replace("_name", "")}_id`] || NA;
      const delta = safeOptional(row.mom_revenue_delta ?? row.delta_revenue);
      const pct = safeOptional(row.mom_revenue_pct ?? row.delta_revenue_pct);
      const cls = delta == null ? "" : delta >= 0 ? "text-success" : "text-danger";
      const isGainer = delta != null && delta >= 0;
      const isFullChurn = !isGainer && safeNum(row.revenue_last_30) === 0 && (tbodyId === "drMoversCustomersTable");

      // Format: "-$61,062 (−47%)" or "+$55,790 (+18%)"
      let deltaDisplay = formatCurrency(delta);
      if (pct != null) {
        const sign = pct >= 0 ? "+" : "−";
        deltaDisplay += ` (${sign}${Math.abs(pct).toFixed(0)}%)`;
      }

      // Prior → Now sub-line
      const prevRev = safeOptional(row.revenue_prev_30 ?? row.prior_revenue);
      const currRev = safeOptional(row.revenue_last_30 ?? row.revenue);
      const subLine = (prevRev != null && currRev != null)
        ? `<div style="font-size:0.75rem;color:#5b6676;">Prior: ${formatCurrency(prevRev)} → Now: ${formatCurrency(currRev)}</div>`
        : "";

      const lostBadge = isFullChurn
        ? `<span style="background:#dc3545;color:#fff;font-size:0.65rem;padding:1px 5px;border-radius:8px;margin-left:4px;vertical-align:middle;">LOST</span>`
        : "";

      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>
          <div>${escapeHtml(name)}${lostBadge}</div>
          ${subLine}
        </td>
        <td class="text-end ${cls}">${deltaDisplay}</td>
        <td class="text-end ${cls}">${formatPct(pct, false)}</td>
      `;
      tbody.appendChild(tr);
    });

    const topGainer = gainers[0];
    const topDecliner = decliners[0];
    const narrative = topGainer || topDecliner
      ? `${topGainer ? `${topGainer[nameKey] || topGainer[`${nameKey.replace("_name", "")}_id`] || "Top gainer"} is up ${formatCurrency(topGainer.mom_revenue_delta ?? topGainer.delta_revenue)}.` : ""} ${topDecliner ? `${topDecliner[nameKey] || topDecliner[`${nameKey.replace("_name", "")}_id`] || "Top decliner"} is down ${formatCurrency(topDecliner.mom_revenue_delta ?? topDecliner.delta_revenue)}.` : ""}`.trim()
      : "No movement details available.";
    if (tbodyId === "drMoversCustomersTable") {
      setText("drCustomerMoversNarrative", narrative);
    } else if (tbodyId === "drMoversProductsTable") {
      setText("drProductMoversNarrative", narrative);
    }
  };

  const renderRiskFlags = () => {
    const list = document.getElementById("drRiskFlags");
    if (!list) return;

    const flags = Array.isArray(currentPayload?.risk_flags) ? currentPayload.risk_flags : [];
    const atRisk = Array.isArray(currentPayload?.tables?.at_risk_customers) ? currentPayload.tables.at_risk_customers.length : 0;
    setText("drAtRiskCount", formatInt(atRisk));

    list.innerHTML = "";
    if (!flags.length) {
      list.innerHTML = '<li class="list-group-item px-0 text-muted">No risk flags triggered.</li>';
      return;
    }

    flags.forEach((flag) => {
      const severity = String(flag.severity || "ok").toLowerCase();
      const tone = toneFromSeverity(severity);
      const label = flag.label || flag.key || "Risk";
      const count = formatInt(flag.count);

      const item = document.createElement("li");
      item.className = "list-group-item px-0 d-flex justify-content-between align-items-center";
      item.innerHTML = `
        <span>${escapeHtml(label)}</span>
        <span>${renderPill(severity, tone)} <span class="ms-1">${count}</span></span>
      `;
      list.appendChild(item);
    });
  };

  const renderMarginRiskTable = () => {
    const tbody = document.getElementById("drMarginRiskTable");
    if (!tbody) return;

    const rows = topNRows(currentPayload?.tables?.margin_risk_products || [], 10);
    tbody.innerHTML = "";
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="4" class="text-muted">No margin leakage products in scope.</td></tr>';
      setText("drMarginRiskNarrative", "No below-target or negative-margin products were identified in the current scope.");
      return;
    }

    rows.forEach((row) => {
      const tone = toneFromStatus(row.target_status, row.status_color);
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>
          <div class="srpd-entity-title">${escapeHtml(row.product_name || row.product_id || NA)}</div>
          <div class="srpd-entity-sub">${renderPill(row.target_status || "Watch", tone)}</div>
        </td>
        <td class="text-end ${safeNum(row.margin_pct) < 0 ? "text-danger" : ""}">${formatPct(row.margin_pct, false)}</td>
        <td class="text-end">${formatCurrency(row.leakage_to_target)}</td>
        <td class="text-end">${formatCurrency(row.revenue)}</td>
      `;
      const url = rowDrillUrl("product", row.product_id || row.product_name);
      attachRowLink(tr, url);
      tbody.appendChild(tr);
    });

    const lead = rows[0];
    setText(
      "drMarginRiskNarrative",
      `${formatInt(rows.length)} margin-risk SKU(s) are in scope; largest leakage sits on ${cleanText(lead?.product_name || lead?.product_id || "the lead SKU")} at ${formatCurrency(lead?.leakage_to_target)}.`
    );
  };

  const normalizeRows = (rows) => (Array.isArray(rows) ? rows : []);

  const filterRowsByQuery = (rows, query, keys) => {
    const q = String(query || "").trim().toLowerCase();
    if (!q) return rows;
    return rows.filter((row) => keys.some((key) => String(row[key] || "").toLowerCase().includes(q)));
  };

  // Customer view state: "all" | "best" | "risk"
  let customerViewMode = "all";

  const computeCustomerScore = (rows) => {
    // Compute composite score = (revenue_rank * 0.40) + (profit_rank * 0.35) + (mom_growth_rank * 0.25)
    // rank = position when sorted ascending (higher revenue = higher rank)
    if (!rows.length) return rows;
    const n = rows.length;

    const byRevenue = [...rows].sort((a, b) => (a.revenue || 0) - (b.revenue || 0));
    const byProfit = [...rows].sort((a, b) => (a.profit || 0) - (b.profit || 0));
    const byMom = [...rows].sort((a, b) => (a.mom_revenue_pct || 0) - (b.mom_revenue_pct || 0));

    const revenueRank = new Map(byRevenue.map((r, i) => [r.customer_id, i + 1]));
    const profitRank = new Map(byProfit.map((r, i) => [r.customer_id, i + 1]));
    const momRank = new Map(byMom.map((r, i) => [r.customer_id, i + 1]));

    return rows.map((row) => ({
      ...row,
      _composite_score:
        (revenueRank.get(row.customer_id) || 1) * 0.40 +
        (profitRank.get(row.customer_id) || 1) * 0.35 +
        (momRank.get(row.customer_id) || 1) * 0.25,
    }));
  };

  const renderCustomersTable = () => {
    const tbody = document.getElementById("drCustomersTable");
    if (!tbody) return;

    const query = document.getElementById("drCustomerSearch")?.value || "";
    let baseRows = filterRowsByQuery(customerRows, query, ["customer_name", "customer_id"]);

    // Apply view mode
    let viewRows = baseRows;
    let badge = "";
    if (customerViewMode === "best") {
      const scored = computeCustomerScore(baseRows).sort((a, b) => b._composite_score - a._composite_score);
      viewRows = scored.slice(0, 10);
      badge = `<span style="background:#198754;color:#fff;font-size:0.7rem;padding:2px 6px;border-radius:12px;margin-left:6px;">★ Top Performer</span>`;
    } else if (customerViewMode === "risk") {
      const scored = computeCustomerScore(baseRows).sort((a, b) => a._composite_score - b._composite_score);
      viewRows = scored.slice(0, 10);
      badge = `<span style="background:#dc3545;color:#fff;font-size:0.7rem;padding:2px 6px;border-radius:12px;margin-left:6px;">⚠ At-Risk</span>`;
    }

    const rows = viewRows;
    tbody.innerHTML = "";
    setText(
      "drCustomerTableSummary",
      `${formatInt(rows.length)} of ${formatInt(customerRows.length)} customer row(s) ${query ? `match "${query}"` : "in the visible book"}.`
    );
    if (!rows.length) {
      const colspan = v2Enabled ? 13 : 3;
      tbody.innerHTML = `<tr><td colspan="${colspan}" class="text-muted">No customer data.</td></tr>`;
      return;
    }

    // Animate table rows with a 150ms fade transition
    tbody.style.opacity = "0";
    setTimeout(() => { tbody.style.transition = "opacity 0.15s"; tbody.style.opacity = "1"; }, 10);

    rows.slice(0, 250).forEach((row) => {
      const tr = document.createElement("tr");
      const margin = safeOptional(row.margin_pct);
      const targetMargin = safeOptional(row.target_margin_pct);
      const marginTone = margin == null ? "neutral" : targetMargin != null ? (margin < targetMargin ? "risk" : "good") : margin < 0 ? "risk" : "neutral";

      // At-risk view: highlight rows with mom_revenue_pct < -15
      const momPct = safeOptional(row.mom_revenue_pct);
      if (customerViewMode === "risk" && momPct != null && momPct < -15) {
        tr.style.backgroundColor = "rgba(220,53,69,0.06)";
      }

      if (v2Enabled) {
        const ownerStatus = row.owner_missing
          ? renderPill("Needs review", "risk")
          : renderPill("Mapped", "good");
        const inheritedBadge = row.inherited_flag
          ? renderPill("Inherited", "accent")
          : renderPill("Direct", "neutral");
        tr.innerHTML = `
          <td>
            <div class="srpd-entity-title">${escapeHtml(row.customer_name || row.customer_id || NA)}${badge}</div>
            <div class="srpd-entity-sub">${escapeHtml(row.customer_id || "")}</div>
          </td>
          <td>
            <div class="srpd-entity-title">${escapeHtml(businessRepName(row.account_owner_name, row.account_owner_id, NA))}</div>
            <div class="srpd-entity-sub">Current owner</div>
          </td>
          <td>
            <div class="srpd-entity-title">${escapeHtml(businessRepName(row.last_sales_rep_name, row.last_sales_rep_id, NA))}</div>
            <div class="srpd-entity-sub">${escapeHtml(row.last_sale_date || row.last_order_date || "Latest visible seller")}</div>
          </td>
          <td class="text-end">
            <div class="srpd-metric-main">${formatCurrency(row.revenue)}</div>
            <div class="srpd-metric-sub">${formatCurrencyCompact(row.revenue)}</div>
          </td>
          <td class="text-end">
            <div class="srpd-metric-main">${formatCurrency(row.profit)}</div>
            <div class="srpd-metric-sub">${margin != null ? formatPct(margin, false) : NA} margin</div>
          </td>
          <td class="text-end">${renderPill(formatPct(margin, false), marginTone)}</td>
          <td class="text-end">
            <div class="srpd-metric-main">${formatInt(row.orders)}</div>
            <div class="srpd-metric-sub">Orders</div>
          </td>
          <td class="text-end">
            <div class="srpd-metric-main">${formatInt(row.weight_lb)}</div>
            <div class="srpd-metric-sub">lb</div>
          </td>
          <td class="text-end">
            <div class="srpd-metric-main">${formatCurrency(row.asp_lb)}</div>
            <div class="srpd-metric-sub">ASP/LB</div>
          </td>
          <td class="text-end">${renderPill(formatPct(row.yoy_revenue_pct, false), toneFromDelta(row.yoy_revenue_pct))}</td>
          <td class="text-end">${inheritedBadge}</td>
          <td>${ownerStatus}</td>
          <td class="text-end">${escapeHtml(row.last_sale_date || row.last_order_date || NA)}</td>
        `;
      } else {
        tr.innerHTML = `
          <td>${escapeHtml(row.customer_name || row.customer_id || NA)}</td>
          <td class="text-end">${formatCurrency(row.revenue)}</td>
          <td class="text-end">${formatInt(row.orders)}</td>
        `;
      }

      const url = rowDrillUrl("customer", row.customer_id || row.customer_name);
      attachRowLink(tr, url);
      tbody.appendChild(tr);
    });
  };

  const renderProductsTable = () => {
    const tbody = document.getElementById("drProductsTable");
    if (!tbody) return;

    const query = document.getElementById("drProductSearch")?.value || "";
    const rows = filterRowsByQuery(productRows, query, ["product_name", "product_id", "protein_family", "category_name"]);

    tbody.innerHTML = "";
    setText(
      "drProductTableSummary",
      `${formatInt(rows.length)} of ${formatInt(productRows.length)} product row(s) ${query ? `match "${query}"` : "in the visible book"}.`
    );
    if (!rows.length) {
      const colspan = v2Enabled ? 15 : 3;
      tbody.innerHTML = `<tr><td colspan="${colspan}" class="text-muted">No product data.</td></tr>`;
      return;
    }

    rows.slice(0, 250).forEach((row) => {
      const tr = document.createElement("tr");

      if (v2Enabled) {
        const statusTone = toneFromStatus(row.target_status, row.status_color);
        tr.innerHTML = `
          <td>
            <div class="srpd-entity-title">${escapeHtml(row.product_name || row.product_id || NA)}</div>
            <div class="srpd-entity-sub">${escapeHtml(row.product_id || "")}</div>
          </td>
          <td>
            <div class="srpd-entity-title">${escapeHtml(row.protein_family || row.category_name || NA)}</div>
            <div class="srpd-entity-sub">Family / category</div>
          </td>
          <td class="text-end">
            <div class="srpd-metric-main">${formatCurrency(row.revenue)}</div>
            <div class="srpd-metric-sub">${formatCurrencyCompact(row.revenue)}</div>
          </td>
          <td class="text-end">
            <div class="srpd-metric-main">${formatCurrency(row.profit)}</div>
            <div class="srpd-metric-sub">${formatPct(row.margin_pct, false)} margin</div>
          </td>
          <td class="text-end">${renderPill(formatPct(row.margin_pct, false), statusTone)}</td>
          <td class="text-end">${renderPill(formatPct(row.target_margin_pct, false), "neutral")}</td>
          <td class="text-end">
            <div class="srpd-metric-main">${formatInt(row.orders)}</div>
            <div class="srpd-metric-sub">Orders</div>
          </td>
          <td class="text-end">
            <div class="srpd-metric-main">${formatInt(row.weight_lb)}</div>
            <div class="srpd-metric-sub">lb</div>
          </td>
          <td class="text-end">
            <div class="srpd-metric-main">${formatCurrency2(row.asp_lb)}</div>
            <div class="srpd-metric-sub">Current</div>
          </td>
          <td class="text-end">
            <div class="srpd-metric-main">${formatCurrency2(row.target_price_lb)}</div>
            <div class="srpd-metric-sub">Target</div>
          </td>
          <td class="text-end">${renderPill(formatSignedCurrency2(row.asp_lb_gap_to_target), toneFromDelta(row.asp_lb_gap_to_target))}</td>
          <td class="text-end">${renderPill(formatPct(row.yoy_revenue_pct, false), toneFromDelta(row.yoy_revenue_pct))}</td>
          <td class="text-end">${renderPill(formatPct(row.price_change_pct, false), toneFromDelta(row.price_change_pct))}</td>
          <td>${renderStatusBadge(row.target_status, row.status_color)}</td>
          <td class="text-end">${escapeHtml(row.last_order_date || NA)}</td>
        `;
      } else {
        tr.innerHTML = `
          <td>${escapeHtml(row.product_name || row.product_id || NA)}</td>
          <td class="text-end">${formatCurrency(row.revenue)}</td>
          <td class="text-end">${formatInt(row.orders)}</td>
        `;
      }

      const url = rowDrillUrl("product", row.product_id || row.product_name);
      attachRowLink(tr, url);
      tbody.appendChild(tr);
    });
  };

  const renderAtRiskTable = () => {
    const tbody = document.getElementById("drAtRiskTable");
    if (!tbody) return;

    const rows = topNRows(currentPayload?.tables?.at_risk_customers || [], 100);
    tbody.innerHTML = "";
    setText(
      "drAtRiskSummary",
      rows.length ? `${formatInt(rows.length)} at-risk customer row(s) exceed the inactivity threshold.` : "No at-risk customers are active in the current scope."
    );

    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="3" class="text-muted">No at-risk customers in this window.</td></tr>';
      return;
    }

    rows.forEach((row) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>
          <div class="srpd-entity-title">${escapeHtml(row.customer_name || row.customer_id || NA)}</div>
          <div class="srpd-entity-sub">${escapeHtml(row.customer_id || "")}</div>
        </td>
        <td class="text-end">${formatInt(row.days_since_last_order)}</td>
        <td class="text-end">${formatCurrency(row.prior_period_revenue)}</td>
      `;
      const url = rowDrillUrl("customer", row.customer_id || row.customer_name);
      attachRowLink(tr, url);
      tbody.appendChild(tr);
    });
  };

  const renderExecutiveSummary = () => {
    const kpis = currentPayload?.kpis || {};
    const meta = currentPayload?.meta || {};
    const customerMovers = currentPayload?.tables?.movers_customers || {};
    const productMovers = currentPayload?.tables?.movers_products || {};
    const marginRows = currentPayload?.tables?.margin_risk_products || [];
    const topCustomers = currentPayload?.tables?.customers || [];

    const topCustomer = topCustomers[0];
    const topCustomerShare = safeOptional(kpis.top_customer_share);
    const inheritedShare = ratioPct(kpis.inherited_revenue, kpis.revenue);
    const leadCustomerDown = (Array.isArray(customerMovers.decliners) ? customerMovers.decliners : []).find(
      (row) => safeNum(row.mom_revenue_delta ?? row.delta_revenue) < 0
    );
    const leadCustomerUp = Array.isArray(customerMovers.gainers) ? customerMovers.gainers[0] : null;
    const leadProductUp = Array.isArray(productMovers.gainers) ? productMovers.gainers[0] : null;
    const leadRiskProduct = marginRows[0];
    const belowTarget = safeNum(kpis.below_target_margin_skus);
    const negativeMargin = safeNum(kpis.negative_margin_skus);
    const marginPressure = negativeMargin > 0 || belowTarget > 0;
    const concentrationHigh = topCustomerShare != null && topCustomerShare > 0.25;

    let actionHeadline = "Maintain current coverage and monitor execution.";
    let actionSupport = "No single risk dominates the current view, so leadership can keep attention on operating cadence and ownership continuity.";
    let heroActionValue = "Monitor current book";
    let heroActionDetail = "The visible scope is balanced enough to stay in operating-review mode.";

    if (marginPressure && leadRiskProduct) {
      actionHeadline = `Recover margin on ${cleanText(leadRiskProduct.product_name || leadRiskProduct.product_id || "priority SKUs")}.`;
      actionSupport = `${formatInt(belowTarget)} SKU(s) are below target and ${formatInt(negativeMargin)} SKU(s) are negative margin, with the largest leakage at ${formatCurrency(leadRiskProduct.leakage_to_target)}.`;
      heroActionValue = "Recover margin";
      heroActionDetail = `${cleanText(leadRiskProduct.product_name || leadRiskProduct.product_id || "Priority SKU")} is the largest immediate leakage point.`;
    } else if (concentrationHigh && topCustomer) {
      actionHeadline = `Protect ${cleanText(topCustomer.customer_name || topCustomer.customer_id || "the largest account")} and reduce concentration risk.`;
      actionSupport = `The top customer carries ${formatPct(topCustomerShare, true)} of visible revenue, so account retention and diversification should lead the next review.`;
      heroActionValue = "Protect concentration";
      heroActionDetail = `${cleanText(topCustomer.customer_name || topCustomer.customer_id || "Largest customer")} is carrying an elevated share of the visible book.`;
    } else if (leadCustomerDown) {
      actionHeadline = `Investigate the decline in ${cleanText(leadCustomerDown.customer_name || leadCustomerDown.customer_id || "the lead declining account")}.`;
      actionSupport = `This customer is down ${formatCurrency(leadCustomerDown.mom_revenue_delta ?? leadCustomerDown.delta_revenue)} month over month and is the clearest near-term revenue drag.`;
      heroActionValue = "Stabilize decline";
      heroActionDetail = `${cleanText(leadCustomerDown.customer_name || leadCustomerDown.customer_id || "Lead declining customer")} is the first account to review.`;
    } else if (leadCustomerUp || leadProductUp) {
      const label = cleanText(
        leadCustomerUp?.customer_name ||
          leadCustomerUp?.customer_id ||
          leadProductUp?.product_name ||
          leadProductUp?.product_id ||
          "the strongest mover"
      );
      const value = leadCustomerUp?.mom_revenue_delta ?? leadCustomerUp?.delta_revenue ?? leadProductUp?.mom_revenue_delta ?? leadProductUp?.delta_revenue;
      actionHeadline = `Scale the momentum behind ${label}.`;
      actionSupport = `${label} is contributing ${formatCurrency(value)} of month-over-month upside and is the clearest growth vector in the visible scope.`;
      heroActionValue = "Scale winner";
      heroActionDetail = `${label} is the strongest upside signal in the current book.`;
    }

    setText("drActionHeadline", actionHeadline);
    setText("drActionSupport", actionSupport);
    setText("drHeroActionValue", heroActionValue);
    setText("drHeroActionDetail", heroActionDetail);

    const actionItems = [
      concentrationHigh && topCustomer
        ? `Protect ${cleanText(topCustomer.customer_name || topCustomer.customer_id || "the top account")}: ${formatPct(topCustomerShare, true)} of revenue sits in one customer.`
        : `Customer concentration is ${topCustomerShare == null ? "not available" : topCustomerShare > 0.18 ? "meaningful but manageable" : "balanced"} in the current scope.`,
      leadCustomerDown
        ? `Review ${cleanText(leadCustomerDown.customer_name || leadCustomerDown.customer_id || "the lead declining account")} for service, pricing, or cadence issues after a ${formatCurrency(leadCustomerDown.mom_revenue_delta ?? leadCustomerDown.delta_revenue)} MoM change.`
        : leadCustomerUp
          ? `Lean into ${cleanText(leadCustomerUp.customer_name || leadCustomerUp.customer_id || "the top gainer")} while the account is contributing ${formatCurrency(leadCustomerUp.mom_revenue_delta ?? leadCustomerUp.delta_revenue)} of MoM upside.`
          : "No single customer mover dominates the current view, so keep the account plan broad.",
      leadRiskProduct
        ? `Use ${cleanText(leadRiskProduct.product_name || leadRiskProduct.product_id || "the leading risk SKU")} as the first margin review item because it carries ${formatCurrency(leadRiskProduct.leakage_to_target)} of target leakage.`
        : leadProductUp
          ? `Promote ${cleanText(leadProductUp.product_name || leadProductUp.product_id || "the top product mover")} as it adds ${formatCurrency(leadProductUp.mom_revenue_delta ?? leadProductUp.delta_revenue)} of MoM upside.`
          : "No specific product issue overrides the current revenue story.",
    ];
    setHTML(
      "drActionList",
      actionItems.map((item) => `<li>${escapeHtml(item)}</li>`).join("")
    );

    const filtersText = summarizeActiveFilters(filtersQS).replace(/^Filters:\s*/, "");
    const riskPosture = marginPressure
      ? `${formatInt(belowTarget + negativeMargin)} SKU issue(s) visible`
      : concentrationHigh
        ? "Concentration-led watch"
        : "No major risk cluster";
    setText(
      "drActionExportContext",
      "This page preserves active filters, attribution mode, exports, and downstream customer/product drill links."
    );
    setHTML(
      "drActionContextList",
      [
        { label: "Filters", value: filtersText || "All" },
        { label: "Ownership view", value: attributionMode === "current_owner" ? "Current owner" : "Historical rep" },
        { label: "Risk posture", value: riskPosture },
      ]
        .map((item) => `<li><strong>${escapeHtml(item.label)}</strong><span>${escapeHtml(item.value)}</span></li>`)
        .join("")
    );

    setText("drNavKpi", `${formatCurrencyCompact(kpis.revenue)} | ${formatPct(kpis.margin_pct, false)}`);
    setText(
      "drNavOwnership",
      inheritedShare != null ? `${formatPct(inheritedShare, false)} inherited` : `${formatInt(kpis.current_owned_customers)} owned`
    );
    setText("drNavTrend", kpis.revenue_mom_pct == null ? "Limited" : `MoM ${formatDelta(kpis.revenue_mom_pct)}`);
    setText("drNavRisk", riskPosture);
    setText("drNavCustomers", `${formatInt(kpis.active_customers_curr)} active`);
    setText("drNavProducts", `${formatInt(productRows.length)} SKU rows`);
    setText("drNavActions", heroActionValue);
  };

  const initTrendControls = () => {
    document.querySelectorAll("[data-trend-grain]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const next = btn.dataset.trendGrain;
        if (!next || next === trendGrain) return;
        trendGrain = next;
        document.querySelectorAll("[data-trend-grain]").forEach((b) => {
          b.classList.toggle("active", b.dataset.trendGrain === trendGrain);
        });
        renderTrend();
      });
    });

    const toggle = document.getElementById("drTrendRollingToggle");
    if (toggle) {
      toggle.addEventListener("change", () => {
        trendRolling = !!toggle.checked;
        renderTrend();
      });
    }
  };

  const initSearchInputs = () => {
    const customerSearch = document.getElementById("drCustomerSearch");
    if (customerSearch) {
      customerSearch.addEventListener("input", () => renderCustomersTable());
    }

    const productSearch = document.getElementById("drProductSearch");
    if (productSearch) {
      productSearch.addEventListener("input", () => renderProductsTable());
    }
  };

  const initCustomerViewToggle = () => {
    const toggleGroup = document.getElementById("drCustomerViewToggle");
    if (!toggleGroup) return;
    toggleGroup.querySelectorAll("[data-customer-view]").forEach((btn) => {
      btn.addEventListener("click", () => {
        customerViewMode = btn.dataset.customerView || "all";
        // Update button styles
        toggleGroup.querySelectorAll("[data-customer-view]").forEach((b) => {
          if (b.dataset.customerView === customerViewMode) {
            b.style.backgroundColor = "#965951";
            b.style.borderColor = "#965951";
            b.style.color = "#fff";
            b.classList.add("active");
            b.classList.remove("btn-outline-secondary");
          } else {
            b.style.backgroundColor = "";
            b.style.borderColor = "";
            b.style.color = "";
            b.classList.remove("active");
            b.classList.add("btn-outline-secondary");
          }
        });
        renderCustomersTable();
      });
    });
  };

  const renderLostAccounts = () => {
    const contentEl = document.getElementById("drLostAccountsContent");
    const badgeEl = document.getElementById("drLostAccountsBadge");
    if (!contentEl) return;

    const lost = Array.isArray(currentPayload?.lost_accounts) ? currentPayload.lost_accounts : [];
    const n = lost.length;

    // Update badge
    if (badgeEl) {
      badgeEl.textContent = String(n);
      badgeEl.style.background = n > 0 ? "#dc3545" : "#198754";
    }

    if (n === 0) {
      contentEl.innerHTML = `
        <div class="text-success d-flex align-items-center gap-2 py-2">
          <i class="bi bi-check-circle fs-5"></i>
          <span>✓ No lost accounts this period — every prior customer placed an order in the current 30-day window.</span>
        </div>`;
      return;
    }

    // Sort by priority_score
    const sorted = [...lost].sort((a, b) => (b.priority_score ?? 0) - (a.priority_score ?? 0));

    let html = `<div class="table-responsive"><table class="table table-sm align-middle mb-0">
      <thead><tr>
        <th>Customer / Opportunity</th>
        <th class="text-center">Priority</th>
        <th class="text-end">Prior 30d Rev</th>
        <th class="text-end">Last Order Date</th>
        <th class="text-end">Days Silent</th>
        <th>Action</th>
      </tr></thead><tbody>`;

    sorted.forEach((row) => {
      const name = escapeHtml(row.customer_name || row.customer_id || NA);
      const rev = formatCurrency(row.revenue_prev_30);
      const date = escapeHtml(row.last_order_date || NA);
      const days = row.days_since_order != null ? formatInt(row.days_since_order) : NA;
      const score = row.priority_score ?? 0;
      const urgency = row.urgency_label || "Medium";
      const reason = escapeHtml(row.opportunity_reason || "");
      
      const phone = row.customer_phone ? `<div class="small text-muted"><i class="bi bi-telephone"></i> ${escapeHtml(row.customer_phone)}</div>` : "";
      const email = row.customer_email ? `<div class="small text-muted"><i class="bi bi-envelope"></i> ${escapeHtml(row.customer_email)}</div>` : "";

      let urgencyClass = "bg-secondary";
      if (urgency === "Critical") urgencyClass = "bg-danger";
      else if (urgency === "High") urgencyClass = "bg-warning text-dark";

      const mailtoSubject = encodeURIComponent(`RE: ${row.customer_name || row.customer_id} — Re-engagement Opportunity`);
      const mailtoBody = encodeURIComponent(
        `Hi,\n\nI am reaching out regarding ${row.customer_name || row.customer_id}.\n\n` +
        `Signal: ${row.opportunity_reason}.\n` +
        `Prior monthly revenue was ${rev}. Last order was on ${date} (${days} days ago).\n\n` +
        `Please prioritize this follow-up today.\n\nThanks`
      );

      html += `<tr>
        <td>
            <div class="fw-bold">${name}</div>
            <div class="small text-muted">${reason}</div>
            ${phone}
            ${email}
        </td>
        <td class="text-center">
            <span class="badge ${urgencyClass}" style="font-size:0.7rem">${urgency.toUpperCase()}</span>
            <div class="small text-muted">Score: ${score}</div>
        </td>
        <td class="text-end">${rev}</td>
        <td class="text-end">${date}</td>
        <td class="text-end"><span class="badge" style="background:#fd7e14;color:#fff;">${days}d</span></td>
        <td><a href="mailto:${row.customer_email || ""}?subject=${mailtoSubject}&body=${mailtoBody}" class="btn btn-sm btn-outline-secondary">
          <i class="bi bi-envelope"></i> Follow Up
        </a></td>
      </tr>`;
    });

    html += "</tbody></table></div>";
    html += `<p class="text-muted small mt-2 mb-0">
        Priority queue ranks accounts by lost revenue and silence duration. 
        Re-engage within the first 60 days for best recovery results.
    </p>`;
    contentEl.innerHTML = html;
  };

  const applyLocalControls = () => {
    filtersQS = mergeLocalControlsIntoQS(filtersQS);
    syncLocalControls();
    replaceHistory();
    updateExportLink();
    fetchBundle();
  };

  const initOwnershipControls = () => {
    const attributionSelect = document.getElementById("drAttributionMode");
    const includeFormer = document.getElementById("drIncludeFormerReps");
    if (attributionSelect) {
      attributionSelect.addEventListener("change", () => {
        const next = attributionSelect.value === "current_owner" ? "current_owner" : "historical_rep";
        if (next === attributionMode) return;
        attributionMode = next;
        if (attributionMode !== "historical_rep") {
          rosterMode = "current_only";
          if (includeFormer) includeFormer.checked = false;
        }
        applyLocalControls();
      });
    }

    if (includeFormer) {
      includeFormer.addEventListener("change", () => {
        const next = includeFormer.checked ? "include_former" : "current_only";
        if (next === rosterMode) return;
        rosterMode = next;
        applyLocalControls();
      });
    }
  };

  const initTooltips = () => {
    if (!window.bootstrap || !window.bootstrap.Tooltip) return;
    document.querySelectorAll('[title]').forEach((el) => {
      if (el.dataset.tooltipBound === "1") return;
      try {
        new window.bootstrap.Tooltip(el, { trigger: "hover focus" });
        el.dataset.tooltipBound = "1";
      } catch (_err) {
        // ignore tooltip init failures
      }
    });
  };

  const updateExportLink = () => {
    const links = Array.from(document.querySelectorAll("a[data-export-dataset]"));
    links.forEach((link) => {
      const base = link.dataset.baseHref || link.getAttribute("href") || "";
      const baseHref = base.split("?")[0];
      link.dataset.baseHref = baseHref;

      const params = new URLSearchParams(filtersQS || "");
      const dataset = link.dataset.exportDataset || "all";
      const format = link.dataset.exportFormat || "xlsx";
      params.set("dataset", dataset);
      params.set("export_type", dataset);
      params.set("format", format);
      if (link.dataset.includeHistory === "1") {
        params.set("include_history", "1");
      }
      const qs = params.toString();
      link.setAttribute("href", qs ? `${baseHref}?${qs}` : baseHref);
    });
    updateBackLink();
  };

  const renderPacingNarrative = () => {
    const el = document.getElementById("drPacingNarrative");
    if (!el) return;
    const kpis = currentPayload?.kpis || {};
    const trend = currentPayload?.trend || currentPayload?.charts?.trend || {};
    const monthly = currentPayload?.charts?.monthly_compare || currentPayload?.trend?.monthly_compare || {};

    const now = new Date();
    const dayOfMonth = now.getDate();
    const daysInMonth = new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate();
    const pctElapsed = dayOfMonth / daysInMonth;

    const totalRevenue = kpis.revenue != null ? Number(kpis.revenue) : null;
    const priorMonthRevenue = (() => {
      const rows = Array.isArray(monthly.rows) ? monthly.rows : [];
      if (rows.length < 2) return null;
      const sorted = [...rows].sort((a, b) => String(b.period || "").localeCompare(String(a.period || "")));
      const v = sorted[1]?.revenue;
      return v != null ? Number(v) : null;
    })();

    const fmtMoney = (v) => v == null ? "—" : "$" + v.toLocaleString("en-US", { maximumFractionDigits: 0 });
    const fmtPct = (v) => (v >= 0 ? "+" : "") + (v * 100).toFixed(1) + "%";

    let html = "";
    if (totalRevenue !== null && pctElapsed > 0) {
      const runRate = totalRevenue / pctElapsed;
      let vs = "";
      if (priorMonthRevenue !== null && priorMonthRevenue > 0) {
        const delta = (runRate - priorMonthRevenue) / priorMonthRevenue;
        const tone = delta >= 0.02 ? "positive" : delta >= 0 ? "warning" : "negative";
        vs = ` — <span class="sr-pacing-delta sr-pacing-delta--${tone}">${fmtPct(delta)} vs prior month</span>`;
      }
      html = `
        <div class="sr-pacing-narrative">
          <span class="sr-pacing-icon" aria-hidden="true">📅</span>
          <span class="sr-pacing-text">
            <strong>Day ${dayOfMonth} of ${daysInMonth}</strong> (${Math.round(pctElapsed * 100)}% elapsed) ·
            MTD revenue <strong>${fmtMoney(totalRevenue)}</strong> ·
            implied run rate <strong>${fmtMoney(runRate)}</strong>${vs}
          </span>
        </div>`;
    } else {
      html = `<div class="sr-pacing-narrative sr-pacing-narrative--unavailable">Pacing data unavailable for the selected window.</div>`;
    }
    el.innerHTML = html;
  };

  const renderV2OnlyBlocks = () => {
    if (!v2Enabled) return;
    renderContext(currentPayload || {});
    renderWarnings();
    renderPacingNarrative();
    renderOwnershipSummary();
    renderOperatingConsole();
    renderMonthlyCompare();
    renderOwnershipCompare();
    renderDecomposition();
    renderMoversTable(
      "drMoversCustomersTable",
      currentPayload?.tables?.movers_customers,
      "customer_name"
    );
    renderMoversTable(
      "drMoversProductsTable",
      currentPayload?.tables?.movers_products,
      "product_name"
    );
    renderConcentration();
    renderRiskFlags();
    renderAtRiskTable();
    renderMarginRiskTable();
    renderExecutiveSummary();
    renderLostAccounts();
  };

  const hydrate = (payload) => {
    currentPayload = window.normalizeBundlePayload ? window.normalizeBundlePayload(payload) : payload;

    const tables = currentPayload?.tables || {};
    const chartsPayload = currentPayload?.charts || {};
    customerRows = normalizeRows(tables.customers || chartsPayload.top_customers || currentPayload?.table?.rows);
    productRows = normalizeRows(tables.products || chartsPayload.top_products || []);

    renderKpis(currentPayload);
    renderTrend();
    renderMix();
    renderCustomersChart();
    renderCustomersTable();
    renderProductsTable();
    renderV2OnlyBlocks();
  };

  const buildQS = () => {
    const params = new URLSearchParams(mergeLocalControlsIntoQS(filtersQS));
    if (repId) params.set("salesrep_id", repId);
    return params.toString();
  };

  const fetchBundle = async () => {
    if (!bundleUrl) return;
    if (controller) controller.abort();
    controller = new AbortController();

    const qs = buildQS();
    const url = qs ? `${bundleUrl}?${qs}` : bundleUrl;
    const reqId = ++currentReqId;

    try {
      const res = await authFetch(url, {
        signal: controller.signal,
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      });
      const payload = await res.json();
      if (reqId !== currentReqId) return;
      if (!res.ok) {
        throw new Error(payload?.error?.message || `HTTP ${res.status}`);
      }
      hydrate(payload);
    } catch (err) {
      if (err?.name === "AbortError") return;
      console.error("salesrep drilldown bundle failed", err);
      renderLoadFailure(err?.message || "Unable to load sales rep drilldown data.");
      ["drTrend", "drMonthlyCompare", "drOwnershipCompare", "drMix", "drMixBar", "drCustomers", "drConcentration"].forEach((id) => {
        toggleEmpty(id, true);
      });
    } finally {
      if (reqId === currentReqId) {
        dispatchGlobalApplyAck({ qs: filtersQS, page: "salesrep_drilldown", rep_id: repId });
      }
    }
  };

  const replaceHistory = () => {
    if (!window.history || typeof window.history.replaceState !== "function") return;
    const qs = filtersQS ? `?${filtersQS}` : "";
    window.history.replaceState({}, "", `${window.location.pathname}${qs}`);
  };

  const bootstrap = async (qsHint) => {
    if (bootstrapped) return;
    bootstrapped = true;

    let qs = filtersQS || qsHint || "";
    if (!qs) {
      const ready = await waitForFiltersReady();
      qs = ready?.qs || "";
    }
    syncLocalStateFromQS(qs);
    filtersQS = mergeLocalControlsIntoQS(qs);

    replaceHistory();
    updateExportLink();
    initTooltips();
    initTrendControls();
    initSearchInputs();
    initCustomerViewToggle();
    initOwnershipControls();
    if (!bootPayloadUsed && bootPayload && typeof bootPayload === "object" && !bootPayload.error) {
      bootPayloadUsed = true;
      hydrate(bootPayload);
      dispatchGlobalApplyAck({ qs: filtersQS, page: "salesrep_drilldown", rep_id: repId });
      return;
    }
    fetchBundle();
  };

  const onApply = (evt) => {
    currentApplyId = evt?.detail?.applyId || null;
    const qs = (evt?.detail && evt.detail.qs) || "";
    applyLocalOverridesFromQS(qs);
    filtersQS = mergeLocalControlsIntoQS(qs);
    replaceHistory();
    updateExportLink();
    fetchBundle();
  };

  window.addEventListener("globalFilters:apply", onApply);
  window.addEventListener("globalFilters:ready", (evt) => bootstrap((evt?.detail && evt.detail.qs) || ""));

  bootstrap();
})();
