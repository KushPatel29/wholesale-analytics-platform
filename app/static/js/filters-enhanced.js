/* Shared enterprise global filters
   - Keeps canonical querystring builder and ready/apply events stable
   - Loads schema/options once and hydrates the shared filters shell
   - Renders compact filter summaries, removable chips, and saved-view state
*/

(() => {
  const authFetch = window.authFetch || fetch;
  const debugEnabled =
    (() => {
      try {
        const flag = String(window.__APP_DEBUG__ || window.ENV || window.FLASK_ENV || "").toLowerCase();
        return flag === "true" || flag === "1" || flag === "development" || flag === "dev";
      } catch (err) {
        return false;
      }
    })() || window.__filtersDebug === true;
  const dlog = (...args) => {
    if (debugEnabled) console.debug("[filters]", ...args);
  };

  if (window.__filtersInitialized) return;
  window.__filtersInitialized = true;

  const createDeferred = () => {
    let resolve;
    let reject;
    const promise = new Promise((res, rej) => {
      resolve = res;
      reject = rej;
    });
    return { promise, resolve, reject };
  };

  let readyDeferred = createDeferred();
  const setReadyDeferred = () => {
    readyDeferred = createDeferred();
    window.filtersReady = readyDeferred.promise;
    window.__filtersReady = readyDeferred.promise;
    if (window.filtersReady && typeof window.filtersReady.catch === "function") {
      window.filtersReady.catch(() => {});
    }
  };
  setReadyDeferred();

  const pageKey = () => {
    const explicitPage =
      document.querySelector("[data-page]")?.dataset?.page ||
      document.body?.dataset?.page ||
      document.body?.dataset?.pageId ||
      "";
    if (explicitPage) return explicitPage;
    try {
      const pathname = String(window.location?.pathname || "")
        .trim()
        .replace(/^\/+|\/+$/g, "");
      if (!pathname) return "home";
      return pathname.replace(/[^a-z0-9/_-]+/gi, "_").replace(/\//g, ":");
    } catch (_err) {
      return "unknown";
    }
  };

  const state = {
    schemaEndpoint: "/api/filters/schema",
    optionsEndpoint: "/api/filters/options",
    apiApplyEndpoint: "/api/filters/apply",
    apiResetEndpoint: "/api/filters/reset",
    optionsAbort: null,
    optionsAbortMeta: null,
    optionsRequestId: 0,
    optionsEtag: null,
    lastOptionsPayload: null,
    lastHealthyOptionsPayload: null,
    optionsFetchMs: null,
    datasetVersion: null,
    scopePayload: null,
    scopeNotice: "",
    schemaDefaults: {},
    labelMap: {},
    lastReadyDetail: null,
    baselineHash: "",
    activeSavedViewId: "",
    selectedSavedViewId: "",
    savedViews: [],
    listenersWired: false,
    initState: "idle",
    initStartedAt: null,
    retryTimer: null,
    deferredHydrationTimer: null,
    activeDimensionKey: "",
    loadedDimensions: new Set(),
    optionsState: "idle",
    schemaLoaded: false,
    readyPublished: false,
    lifecycle: "idle",
    appliedFilters: null,
    appliedQs: "",
    pendingFilters: null,
    pendingHash: "",
    applyAckTimer: null,
    applyInFlight: false,
    optionsFailureCount: 0,
    optionsCooldownUntil: 0,
    dimensionHealth: {},
    optionsInFlightKey: "",
    optionsInFlightPromise: null,
    backgroundRefreshTracker: Object.create(null),
    applySequence: 0,
    activeApplyId: "",
    activeApplyTargetUrl: "",
    activeApplyQs: "",
  };

  const INIT_RETRY_MS = 2000;
  const INIT_RETRY_INTERVAL = 100;
  const BOOTSTRAP_OPTIONS_TIMEOUT_MS = 7000;
  const DEFERRED_OPTIONS_TIMEOUT_MS = 15000;
  const SCHEMA_REQUEST_TIMEOUT_MS = 2200;
  const APPLY_ACK_TIMEOUT_MS = 12000;
  const OPTIONS_FAILURE_COOLDOWN_MS = 15000;
  const BACKGROUND_REFRESH_MIN_INTERVAL_MS = 8000;
  const OPTIONS_STORAGE_KEY = "wholesale.globalFilterOptions.v1";
  const OPTIONS_STORAGE_TTL_MS = 1000 * 60 * 60 * 48;
  const INIT_EVENTS = ["DOMContentLoaded", "pageshow"];
  const CUSTOM_INIT_EVENTS = ["page:ready"];
  const BOOTSTRAP_OPTION_DIMENSIONS = ["statuses", "regions", "methods"];
  const FILTER_KEY_REGEX = /^(start|start_date|end|end_date|date_preset|preset|range_preset|date_type|statuses|regions|methods|shipping_methods|customers|suppliers|products|sales_reps|protein_groups|yield_min|yield_max|protein_min|protein_max|protein_name|protein_name_like|complete_months_only|full_months_only|_gf)$/i;
  const FISCAL_PRESETS = new Set([
    "current_fy",
    "previous_fy",
    "current_fq",
    "previous_fq",
    "current_fm",
    "previous_fm",
    "fytd_comparison",
  ]);
  const FISCAL_START_MONTH_INDEX = 9;
  const FISCAL_START_DAY = 1;
  const DIMENSIONS = [
    { id: "fStatuses", key: "statuses", label: "Status", emptyLabel: "All statuses", countId: "statusesCount" },
    { id: "fRegions", key: "regions", label: "Region", emptyLabel: "All regions", countId: "regionsCount" },
    { id: "fMethods", key: "methods", label: "Shipping Method", emptyLabel: "All methods", countId: "methodsCount" },
    { id: "fCustomers", key: "customers", label: "Customer", emptyLabel: "All customers", countId: "customersCount" },
    { id: "fSalesReps", key: "sales_reps", label: "Sales Rep", emptyLabel: "All sales reps", countId: "salesRepsCount" },
    { id: "fSuppliers", key: "suppliers", label: "Supplier", emptyLabel: "All suppliers", countId: "suppliersCount" },
    { id: "fProducts", key: "products", label: "Product", emptyLabel: "All products", countId: "productsCount" },
    { id: "fProteinGroups", key: "protein_groups", label: "Protein Group", emptyLabel: "All species", countId: "proteinGroupsCount" },
    { id: "fYieldRange", key: "yield_range", label: "Yield Performance", emptyLabel: "All yields", countId: "yieldRangeCount" },
  ];
  const LABEL_ALIASES = {
    methods: ["methods", "shipping_methods", "ship_methods"],
    shipping_methods: ["shipping_methods", "methods", "ship_methods"],
    sales_reps: ["sales_reps", "sales_rep_ids"],
  };
  const PRESET_LABELS = {
    current_fy: "Current FY",
    previous_fy: "Previous FY",
    current_fq: "Current FQ",
    previous_fq: "Previous FQ",
    current_fm: "Current FM",
    previous_fm: "Previous FM",
    fytd_comparison: "FYTD Comparison",
    today: "Today",
    yesterday: "Yesterday",
    "7d": "Last 7 Days",
    last_7_days: "Last 7 Days",
    "30d": "Last 30 Days",
    last_30_days: "Last 30 Days",
    "90d": "Last 90 Days",
    last_90_days: "Last 90 Days",
    last_3_months: "Last 90 Days",
    mtd: "Month to Date",
    qtd: "Quarter to Date",
    ytd: "Year to Date",
    last_month: "Last Month",
    "last-month": "Last Month",
    last_quarter: "Last Quarter",
    "last-quarter": "Last Quarter",
    custom: "Custom",
    all: "All Time",
    all_time: "All Time",
  };

  const isBackgroundOptionsPhase = (phase) => ["deferred", "post-apply"].includes(String(phase || "").toLowerCase());
  const isTimeoutError = (err) => /timed out/i.test(String(err?.message || err || ""));
  const clonePayload =
    typeof structuredClone === "function"
      ? (value) => structuredClone(value)
      : (value) => {
          try {
            return JSON.parse(JSON.stringify(value));
          } catch (_err) {
            return value;
          }
        };
  const cloneOptionsList = (items = []) =>
    Array.isArray(items)
      ? items.map((item) => (item && typeof item === "object" ? { ...item } : item))
      : [];

  const recordOptionsFailure = ({ phase = "interactive" } = {}) => {
    if (isBackgroundOptionsPhase(phase)) return;
    state.optionsFailureCount += 1;
    if (state.optionsFailureCount >= 3) {
      state.optionsCooldownUntil = Date.now() + OPTIONS_FAILURE_COOLDOWN_MS;
    }
  };

  const rootEl = () => document.getElementById("GlobalFilters");

  const setLifecycle = (next, detail = "") => {
    state.lifecycle = String(next || "idle");
    const root = rootEl();
    if (root) root.dataset.filtersState = state.lifecycle;
    if (detail) dlog("filters lifecycle", { state: state.lifecycle, detail });
  };

  const clearApplyAckTimer = () => {
    if (!state.applyAckTimer) return;
    window.clearTimeout(state.applyAckTimer);
    state.applyAckTimer = null;
  };

  const normalizeApplyId = (value) => String(value || "").trim();

  const nextApplyId = () => `${pageKey()}-${Date.now()}-${++state.applySequence}`;

  const clearActiveApply = () => {
    state.activeApplyId = "";
    state.activeApplyTargetUrl = "";
    state.activeApplyQs = "";
  };

  const clearDeferredHydrationTimer = () => {
    if (!state.deferredHydrationTimer) return;
    window.clearTimeout(state.deferredHydrationTimer);
    state.deferredHydrationTimer = null;
  };

  const dimensionsKey = (dimensions = []) => normalizeDimensionList(dimensions).join(",");

  const backgroundRefreshKey = ({ dimensions = [], phase = "interactive" } = {}) =>
    `${String(phase || "interactive").toLowerCase()}:${dimensionsKey(dimensions)}:${state.appliedQs || window.location.search || ""}`;

  const shouldSkipBackgroundRefresh = (key) => {
    if (!key || !state.lastOptionsPayload) return false;
    const entry = state.backgroundRefreshTracker[key];
    if (!entry || !entry.at) return false;
    return Date.now() - Number(entry.at) < BACKGROUND_REFRESH_MIN_INTERVAL_MS;
  };

  const markBackgroundRefresh = (key, status) => {
    if (!key) return;
    state.backgroundRefreshTracker[key] = {
      at: Date.now(),
      status: String(status || ""),
    };
  };

  const clearFilterError = () => {
    const banner = document.getElementById("filtersErrorBanner");
    const retryWrap = document.getElementById("filtersRetryWrap");
    const retryBtn = document.getElementById("filtersRetryBtn");
    if (banner) {
      banner.classList.add("d-none");
      banner.textContent = "";
    }
    retryWrap?.classList.add("d-none");
    retryBtn?.classList.add("d-none");
  };

  const showFilterError = (message) => {
    const banner = document.getElementById("filtersErrorBanner");
    const retryWrap = document.getElementById("filtersRetryWrap");
    const retryBtn = document.getElementById("filtersRetryBtn");
    if (banner) {
      banner.textContent = message || "Filters are temporarily unavailable.";
      banner.classList.remove("d-none");
    }
    retryWrap?.classList.remove("d-none");
    retryBtn?.classList.remove("d-none");
  };

  const parseInlineJson = (id) => {
    try {
      const node = document.getElementById(id);
      const raw = node?.textContent || "";
      return raw ? JSON.parse(raw) : null;
    } catch (_err) {
      return null;
    }
  };

  const labelKeys = (key) => LABEL_ALIASES[key] || [key];

  const setLabelMap = (el, items) => {
    const key = (el?.name || el?.id || "").replace(/\[\]$/, "");
    if (!key) return;
    const map = new Map();
    (items || []).forEach(({ value, label }) => {
      if (value === undefined || value === null) return;
      map.set(String(value), String(label ?? value));
    });
    labelKeys(key).forEach((alias) => {
      state.labelMap[alias] = map;
    });
  };

  const labelFor = (key, value) => {
    const safeKey = (key || "").replace(/\[\]$/, "");
    const candidates = labelKeys(safeKey);
    for (const candidate of candidates) {
      const map = state.labelMap[candidate];
      if (map && map.has(String(value))) return map.get(String(value));
    }
    return value;
  };

  const labelsFor = (key, values = []) => {
    const vals = Array.isArray(values) ? values : [values];
    return vals.map((value) => labelFor(key, value)).filter((value) => value !== undefined && value !== null);
  };

  if (typeof window !== "undefined") {
    window.getFilterLabel = (key, value) => labelFor(key, value);
    window.getFilterLabels = (key, values = []) => labelsFor(key, values);
    window.getGlobalFilterState = () => {
      const filters = getAppliedFilters();
      const qs = state.appliedQs || buildQueryStringForFilters(filters);
      const pendingFilters = stableFilters(state.pendingFilters || gatherFilters());
      return {
        filters,
        qs,
        pendingFilters,
        pendingQs: buildQueryStringForFilters(pendingFilters),
        datasetVersion: state.datasetVersion,
        scope: state.scopePayload,
        detail: state.lastReadyDetail,
      };
    };
    window.getPendingGlobalFilterState = () => {
      const pendingFilters = stableFilters(state.pendingFilters || gatherFilters());
      return {
        filters: pendingFilters,
        qs: buildQueryStringForFilters(pendingFilters),
        datasetVersion: state.datasetVersion,
        scope: state.scopePayload,
      };
    };
  }

  class MultiSelectX {
    constructor(selectEl, mountEl) {
      this.select = selectEl;
      this.mount = mountEl;
      if (!this.select || !this.mount) return;

      this.mount.classList.add("msx-wrap");
      const baseRaw = this.select.id || this.select.name || "msx";
      this.baseId = String(baseRaw || "msx").replace(/[\s'"<>]+/g, "_");
      this.formId = `${this.baseId}_proxy_form`;
      this.ensureProxyForm();
      const searchId = `${this.baseId}_search`;
      const selectLabel =
        this.select.getAttribute("aria-label") ||
        this.select.dataset.label ||
        this.select.getAttribute("name") ||
        "options";

      this.mount.innerHTML = `
        <div class="msx-header">
          <div class="msx-summary">
            <div class="msx-chips" aria-live="polite"></div>
          </div>
          <div class="msx-controls">
            <input class="form-control form-control-sm msx-search" type="search" id="${searchId}" name="${searchId}" form="${this.formId}" placeholder="Search ${selectLabel}..." aria-label="Search ${selectLabel}" autocomplete="off">
            <div class="btn-group btn-group-sm">
              <button type="button" class="btn btn-outline-secondary" data-act="all">All values</button>
              <button type="button" class="btn btn-outline-secondary" data-act="visible">Add visible</button>
              <button type="button" class="btn btn-outline-secondary" data-act="invert">Invert</button>
            </div>
          </div>
        </div>
        <div class="msx-empty-state d-none" role="status" aria-live="polite"></div>
        <div class="msx-list" tabindex="0" role="listbox" aria-multiselectable="true"></div>
        <div class="msx-footer"><span class="msx-count">0</span> selected <span class="msx-footnote">Leave empty to include all values.</span></div>
      `;

      this.searchInput = this.mount.querySelector(".msx-search");
      this.listEl = this.mount.querySelector(".msx-list");
      this.countEl = this.mount.querySelector(".msx-count");
      this.chipsEl = this.mount.querySelector(".msx-chips");
      this.emptyStateEl = this.mount.querySelector(".msx-empty-state");
      this.lastQuery = "";
      this.valueSet = new Set(
        Array.from(this.select.options)
          .filter((opt) => opt.selected)
          .map((opt) => String(opt.value))
      );

      this.buildRows();

      this.listEl.addEventListener("change", (event) => {
        if (!event.target.matches('input[type="checkbox"]')) return;
        const row = event.target.closest(".msx-row");
        const value = row?.dataset.value ?? "";
        if (event.target.checked) this.valueSet.add(value);
        else this.valueSet.delete(value);
        this.syncSelect();
      });

      this.mount.querySelectorAll("[data-act]").forEach((button) => {
        button.addEventListener("click", () => this.bulkAction(button.dataset.act));
      });

      let debounce = null;
      this.searchInput.addEventListener("input", () => {
        clearTimeout(debounce);
        debounce = setTimeout(() => this.applyFilter(this.searchInput.value), 80);
      });

      this.attachShim();
      this.applyFilter("");
      this.syncSelect();
      this.mount.classList.remove("d-none");
      this.select.classList.add("d-none");
    }

    ensureProxyForm() {
      if (!this.formId || document.getElementById(this.formId)) return;
      const proxy = document.createElement("form");
      proxy.id = this.formId;
      proxy.hidden = true;
      proxy.setAttribute("aria-hidden", "true");
      const attach = () => {
        if (document.body) document.body.appendChild(proxy);
        else requestAnimationFrame(attach);
      };
      attach();
    }

    attachShim() {
      const api = {
        setValue: (values) => this.setValue(Array.isArray(values) ? values : [values]),
        clear: () => this.setValue([]),
        get items() {
          return this.getValues();
        },
      };
      Object.defineProperty(this.select, "tomselect", {
        value: api,
        configurable: true,
      });
      this.tomselect = api;
    }

    buildRows() {
      const fragment = document.createDocumentFragment();
      this.rows = Array.from(this.select.options)
        .filter((opt) => !/^all$/i.test(String(opt.value || "")))
        .map((opt, index) => {
          const row = document.createElement("label");
          row.className = "msx-row";
          row.dataset.value = String(opt.value ?? "");

          const checkbox = document.createElement("input");
          checkbox.type = "checkbox";
          checkbox.className = "form-check-input";
          checkbox.id = `${this.baseId}_msx_${index}`;
          checkbox.name = `${this.baseId}__msx`;
          checkbox.value = String(opt.value ?? "");
          checkbox.setAttribute("form", this.formId);
          checkbox.checked = this.valueSet.has(String(opt.value));

          const labelSpan = document.createElement("span");
          labelSpan.className = "msx-label";
          labelSpan.title = opt.text;
          labelSpan.textContent = opt.text;

          row.appendChild(checkbox);
          row.appendChild(labelSpan);
          fragment.appendChild(row);
          return row;
        });

      this.listEl.innerHTML = "";
      this.listEl.appendChild(fragment);
      this.updateListState();
    }

    applyFilter(query) {
      const needle = String(query || "").trim().toLowerCase();
      this.lastQuery = needle;
      this.rows.forEach((row) => {
        const text = row.querySelector(".msx-label")?.textContent?.toLowerCase() || "";
        row.style.display = !needle || text.includes(needle) ? "" : "none";
      });
      this.updateListState();
    }

    updateListState() {
      const totalRows = Array.isArray(this.rows) ? this.rows.length : 0;
      const visibleRows = (this.rows || []).filter((row) => row.style.display !== "none").length;
      let message = "";
      if (!totalRows) {
        message = "No options available for the current scope.";
      } else if (this.lastQuery && visibleRows === 0) {
        message = "No matches for the current search.";
      }
      if (this.emptyStateEl) {
        this.emptyStateEl.textContent = message;
        this.emptyStateEl.classList.toggle("d-none", !message);
      }
      this.listEl.classList.toggle("d-none", !!message);
    }

    bulkAction(action) {
      const visible = this.rows.filter((row) => row.style.display !== "none");
      if (action === "all") {
        this.valueSet.clear();
        this.rows.forEach((row) => {
          const checkbox = row.querySelector("input");
          if (checkbox) checkbox.checked = false;
        });
      } else if (action === "invert") {
        visible.forEach((row) => {
          const checkbox = row.querySelector("input");
          if (!checkbox) return;
          checkbox.checked = !checkbox.checked;
          if (checkbox.checked) this.valueSet.add(String(row.dataset.value));
          else this.valueSet.delete(String(row.dataset.value));
        });
      } else if (action === "visible") {
        visible.forEach((row) => {
          const checkbox = row.querySelector("input");
          if (checkbox) checkbox.checked = true;
          this.valueSet.add(String(row.dataset.value));
        });
      }
      this.syncSelect();
    }

    getValues() {
      return Array.from(this.valueSet);
    }

    setValue(values) {
      this.valueSet = new Set((values || []).map((value) => String(value)));
      this.rows.forEach((row) => {
        const checkbox = row.querySelector("input");
        if (checkbox) checkbox.checked = this.valueSet.has(String(row.dataset.value));
      });
      this.syncSelect();
    }

    renderChips() {
      const chosen = Array.from(this.select.options).filter((opt) => this.valueSet.has(String(opt.value)));
      this.chipsEl.innerHTML = "";
      if (!chosen.length) {
        const chip = document.createElement("span");
        chip.className = "msx-chip is-empty";
        chip.textContent = "All values";
        this.chipsEl.appendChild(chip);
        this.countEl.textContent = "0";
        return;
      }

      chosen.slice(0, 3).forEach((opt) => {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "msx-chip";
        chip.innerHTML = `<span>${opt.text}</span><i class="bi bi-x-lg" aria-hidden="true"></i>`;
        chip.setAttribute("aria-label", `Remove ${opt.text}`);
        chip.addEventListener("click", () => {
          this.valueSet.delete(String(opt.value));
          const row = this.rows.find((entry) => String(entry.dataset.value) === String(opt.value));
          if (row) {
            const checkbox = row.querySelector("input");
            if (checkbox) checkbox.checked = false;
          }
          this.syncSelect();
        });
        this.chipsEl.appendChild(chip);
      });

      if (chosen.length > 3) {
        const chip = document.createElement("span");
        chip.className = "msx-chip is-overflow";
        chip.textContent = `+${chosen.length - 3}`;
        this.chipsEl.appendChild(chip);
      }

      this.countEl.textContent = String(this.valueSet.size);
    }

    syncSelect() {
      Array.from(this.select.options).forEach((opt) => {
        opt.selected = this.valueSet.has(String(opt.value));
      });
      this.renderChips();
      this.updateListState();
      this.select.dispatchEvent(new Event("change", { bubbles: true }));
      this.select.dispatchEvent(new Event("input", { bubbles: true }));
    }
  }

  const pad = (value) => String(value).padStart(2, "0");
  const fmtDate = (date) => `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;

  const humanizePreset = (preset) => {
    const token = String(preset || "").trim().toLowerCase();
    if (!token) return "";
    if (PRESET_LABELS[token]) return PRESET_LABELS[token];
    return token.replace(/[_-]+/g, " ").replace(/\b\w/g, (match) => match.toUpperCase());
  };

  const normalizeDateType = (dateType, preset = "") => {
    const explicit = String(dateType || "").trim().toLowerCase();
    const token = String(preset || "").trim().toLowerCase();
    if (FISCAL_PRESETS.has(token)) return "fiscal";
    if (explicit === "fiscal" || explicit === "calendar") return explicit;
    return explicit || null;
  };

  const getFiscalPeriods = (referenceDate = new Date()) => {
    const today = new Date(referenceDate.getFullYear(), referenceDate.getMonth(), referenceDate.getDate());
    const fiscalYearStartYear =
      today.getMonth() < FISCAL_START_MONTH_INDEX ||
      (today.getMonth() === FISCAL_START_MONTH_INDEX && today.getDate() < FISCAL_START_DAY)
        ? today.getFullYear() - 1
        : today.getFullYear();
    const currentFyStart = new Date(fiscalYearStartYear, FISCAL_START_MONTH_INDEX, FISCAL_START_DAY);
    const previousFyStart = new Date(fiscalYearStartYear - 1, FISCAL_START_MONTH_INDEX, FISCAL_START_DAY);
    const previousFyEnd = new Date(currentFyStart.getTime() - 86400000);
    const priorFyStart = new Date(fiscalYearStartYear - 2, FISCAL_START_MONTH_INDEX, FISCAL_START_DAY);
    const priorFyEnd = new Date(previousFyStart.getTime() - 86400000);

    const monthsSinceFyStart = (today.getFullYear() - currentFyStart.getFullYear()) * 12 + (today.getMonth() - currentFyStart.getMonth());
    const currentFqStart = new Date(currentFyStart.getFullYear(), currentFyStart.getMonth() + Math.floor(monthsSinceFyStart / 3) * 3, 1);
    const previousFqStart = new Date(currentFqStart.getFullYear(), currentFqStart.getMonth() - 3, 1);
    const previousFqEnd = new Date(currentFqStart.getTime() - 86400000);
    const priorFqStart = new Date(previousFqStart.getFullYear(), previousFqStart.getMonth() - 3, 1);
    const priorFqEnd = new Date(previousFqStart.getTime() - 86400000);

    const currentFmStart = new Date(currentFyStart.getFullYear(), currentFyStart.getMonth() + monthsSinceFyStart, 1);
    const previousFmStart = new Date(currentFmStart.getFullYear(), currentFmStart.getMonth() - 1, 1);
    const previousFmEnd = new Date(currentFmStart.getTime() - 86400000);
    const priorFmStart = new Date(previousFmStart.getFullYear(), previousFmStart.getMonth() - 1, 1);
    const priorFmEnd = new Date(previousFmStart.getTime() - 86400000);

    const fyElapsedDays = Math.max(0, Math.round((today.getTime() - currentFyStart.getTime()) / 86400000));
    const fqElapsedDays = Math.max(0, Math.round((today.getTime() - currentFqStart.getTime()) / 86400000));
    const fmElapsedDays = Math.max(0, Math.round((today.getTime() - currentFmStart.getTime()) / 86400000));
    const previousFyYtdEnd = new Date(previousFyStart.getFullYear(), previousFyStart.getMonth(), previousFyStart.getDate() + fyElapsedDays);
    const previousFqQtdEnd = new Date(previousFqStart.getFullYear(), previousFqStart.getMonth(), previousFqStart.getDate() + fqElapsedDays);
    const previousFmMtdEnd = new Date(previousFmStart.getFullYear(), previousFmStart.getMonth(), previousFmStart.getDate() + fmElapsedDays);

    return {
      current_fy: { start: currentFyStart, end: today, compareStart: previousFyStart, compareEnd: previousFyYtdEnd > previousFyEnd ? previousFyEnd : previousFyYtdEnd },
      previous_fy: { start: previousFyStart, end: previousFyEnd, compareStart: priorFyStart, compareEnd: priorFyEnd },
      current_fq: { start: currentFqStart, end: today, compareStart: previousFqStart, compareEnd: previousFqQtdEnd > previousFqEnd ? previousFqEnd : previousFqQtdEnd },
      previous_fq: { start: previousFqStart, end: previousFqEnd, compareStart: priorFqStart, compareEnd: priorFqEnd },
      current_fm: { start: currentFmStart, end: today, compareStart: previousFmStart, compareEnd: previousFmMtdEnd > previousFmEnd ? previousFmEnd : previousFmMtdEnd },
      previous_fm: { start: previousFmStart, end: previousFmEnd, compareStart: priorFmStart, compareEnd: priorFmEnd },
      fytd_comparison: { start: currentFyStart, end: today, compareStart: previousFyStart, compareEnd: previousFyYtdEnd > previousFyEnd ? previousFyEnd : previousFyYtdEnd },
    };
  };

  const presetRange = (preset) => {
    const today = new Date();
    const token = String(preset || "").trim().toLowerCase();
    let start = null;
    let end = null;
    if (FISCAL_PRESETS.has(token)) {
      const period = getFiscalPeriods(today)[token];
      start = period?.start || null;
      end = period?.end || null;
    } else if (token === "today") {
      start = new Date(today);
      end = new Date(today);
    } else if (token === "yesterday") {
      start = new Date(today);
      start.setDate(start.getDate() - 1);
      end = new Date(start);
    } else if (token === "7d" || token === "last_7_days") {
      start = new Date(today);
      start.setDate(start.getDate() - 6);
      end = new Date(today);
    } else if (token === "30d" || token === "last_30_days") {
      start = new Date(today);
      start.setDate(start.getDate() - 29);
      end = new Date(today);
    } else if (token === "90d" || token === "last_90_days" || token === "last_3_months") {
      start = new Date(today);
      start.setMonth(start.getMonth() - 3);
      start = new Date(start.getFullYear(), start.getMonth(), 1);
      end = new Date(today);
    } else if (token === "mtd") {
      start = new Date(today.getFullYear(), today.getMonth(), 1);
      end = new Date(today);
    } else if (token === "qtd") {
      const quarterStart = Math.floor(today.getMonth() / 3) * 3;
      start = new Date(today.getFullYear(), quarterStart, 1);
      end = new Date(today);
    } else if (token === "ytd") {
      start = new Date(today.getFullYear(), 0, 1);
      end = new Date(today);
    } else if (token === "last-month" || token === "last_month") {
      start = new Date(today.getFullYear(), today.getMonth() - 1, 1);
      end = new Date(today.getFullYear(), today.getMonth(), 0);
    } else if (token === "last-quarter" || token === "last_quarter") {
      const quarterStart = Math.floor(today.getMonth() / 3) * 3;
      const thisQuarterStart = new Date(today.getFullYear(), quarterStart, 1);
      end = new Date(thisQuarterStart.getTime() - 86400000);
      const prevQuarterStart = Math.floor(end.getMonth() / 3) * 3;
      start = new Date(end.getFullYear(), prevQuarterStart, 1);
    } else if (token === "all") {
      return { start: "", end: "" };
    } else if (token === "custom") {
      return { start: null, end: null };
    }

    return {
      start: start ? fmtDate(start) : "",
      end: end ? fmtDate(end) : "",
    };
  };

  const multiValues = (el) => {
    if (!el) return [];
    if (el._msx && typeof el._msx.getValues === "function") {
      return el._msx.getValues().filter((value) => !/^all$/i.test(String(value || "").trim()));
    }
    if (el.tomselect && Array.isArray(el.tomselect.items)) {
      return [...el.tomselect.items].filter((value) => !/^all$/i.test(String(value || "").trim()));
    }
    if (el.multiple) {
      return Array.from(el.selectedOptions)
        .map((option) => String(option.value))
        .filter((value) => !/^all$/i.test(value.trim()));
    }
    return el.value ? [String(el.value)] : [];
  };

  const stableList = (values) =>
    (values || [])
      .map((value) => String(value).trim())
      .filter(Boolean)
      .sort((a, b) => a.localeCompare(b));

  const stableFilters = (raw) => ({
    start: raw?.start || null,
    end: raw?.end || null,
    date_preset: raw?.date_preset || null,
    date_type: normalizeDateType(raw?.date_type, raw?.date_preset),
    statuses: stableList(raw?.statuses),
    regions: stableList(raw?.regions),
    methods: stableList(raw?.methods),
    customers: stableList(raw?.customers),
    suppliers: stableList(raw?.suppliers),
    products: stableList(raw?.products),
    sales_reps: stableList(raw?.sales_reps),
    protein_groups: stableList(raw?.protein_groups),
    yield_min: raw?.yield_min ?? null,
    yield_max: raw?.yield_max ?? null,
    protein_min: raw?.protein_min ?? null,
    protein_max: raw?.protein_max ?? null,
    protein_name_like: raw?.protein_name_like || null,
    complete_months_only: raw?.complete_months_only ?? null,
  });

  const filtersHash = (filters) => JSON.stringify(stableFilters(filters));

  const normalizeServerFilters = (raw) => {
    if (window.FilterState && typeof window.FilterState.sanitize === "function") {
      return stableFilters(window.FilterState.sanitize(raw || {}, buildOptionsIndex(state.lastOptionsPayload?.options || {})));
    }
    return stableFilters(raw || {});
  };

  const gatherFilters = () => {
    const form = document.getElementById("filtersForm");
    if (window.FilterState && typeof window.FilterState.fromForm === "function") {
      return stableFilters(window.FilterState.fromForm(form));
    }
    return stableFilters({
      start: document.getElementById("fStart")?.value || null,
      end: document.getElementById("fEnd")?.value || null,
      date_preset: document.getElementById("fDatePreset")?.value || null,
      date_type: document.getElementById("fDateType")?.value || null,
      statuses: multiValues(document.getElementById("fStatuses")),
      regions: multiValues(document.getElementById("fRegions")),
      methods: multiValues(document.getElementById("fMethods")),
      customers: multiValues(document.getElementById("fCustomers")),
      suppliers: multiValues(document.getElementById("fSuppliers")),
      products: multiValues(document.getElementById("fProducts")),
      sales_reps: multiValues(document.getElementById("fSalesReps")),
      protein_groups: multiValues(document.getElementById("fProteinGroups")),
      yield_min: document.getElementById("fYieldMin")?.value || null,
      yield_max: document.getElementById("fYieldMax")?.value || null,
    });
  };

  const getAppliedFilters = () => stableFilters(state.appliedFilters || gatherFilters());

  const buildQueryStringForFilters = (filters) => {
    if (window.FilterState && typeof window.FilterState.toQueryString === "function") {
      return window.FilterState.toQueryString(stableFilters(filters));
    }
    return localBuildFilterQS(filters);
  };

  const setAppliedState = (filters, { syncForm = false } = {}) => {
    const normalized = stableFilters(filters);
    state.appliedFilters = normalized;
    state.appliedQs = buildQueryStringForFilters(normalized);
    state.baselineHash = filtersHash(normalized);
    if (syncForm && window.FilterState && typeof window.FilterState.set === "function") {
      window.FilterState.set(normalized, { persist: true });
      if (typeof window.FilterState.hydrateForm === "function") {
        window.FilterState.hydrateForm(document.getElementById("filtersForm"));
      }
    }
  };

  const dispatchGlobalFiltersApply = (detail) => {
    try {
      document.dispatchEvent(new CustomEvent("globalFilters:apply", { detail }));
    } catch (_err) {
      /* ignore */
    }
    try {
      window.dispatchEvent(new CustomEvent("globalFilters:apply", { detail }));
    } catch (_err) {
      /* ignore */
    }
  };

  const dispatchGlobalFiltersApplied = (detail = {}) => {
    try {
      document.dispatchEvent(new CustomEvent("globalFilters:applied", { detail }));
    } catch (_err) {
      /* ignore */
    }
    try {
      window.dispatchEvent(new CustomEvent("globalFilters:applied", { detail }));
    } catch (_err) {
      /* ignore */
    }
  };

  if (typeof window !== "undefined") {
    window.dispatchGlobalFiltersApply = dispatchGlobalFiltersApply;
    window.dispatchGlobalFiltersApplied = dispatchGlobalFiltersApplied;
  }

  const formatDateLabel = (filters) => {
    const preset = humanizePreset(filters?.date_preset);
    if (preset && String(filters?.date_preset || "").toLowerCase() !== "custom") return preset;
    if (filters?.start && filters?.end) return `${filters.start} to ${filters.end}`;
    if (filters?.start) return `Since ${filters.start}`;
    if (filters?.end) return `Through ${filters.end}`;
    return preset || "All Time";
  };

  const summarizeValues = (key, values, maxItems = 2) => {
    const labels = labelsFor(key, values).map((label) => String(label).trim()).filter(Boolean);
    if (!labels.length) return "All";
    if (labels.length === 1) return labels[0];
    const visible = labels.slice(0, maxItems);
    if (labels.length <= visible.length) return visible.join(", ");
    return `${visible.join(", ")} +${labels.length - visible.length}`;
  };

  const buildSummary = (filters) => {
    const dimensionChips = DIMENSIONS.map((config) => {
      const values = stableList(filters?.[config.key]);
      if (!values.length) return null;
      return {
        key: config.key,
        label: config.label,
        count: values.length,
        summary: summarizeValues(config.key, values, 2),
      };
    }).filter(Boolean);
    const advancedChips = [];
    const proteinBounds = [];
    if (filters?.protein_min !== null && filters?.protein_min !== undefined && `${filters.protein_min}`.trim() !== "") {
      proteinBounds.push(`>= ${filters.protein_min}`);
    }
    if (filters?.protein_max !== null && filters?.protein_max !== undefined && `${filters.protein_max}`.trim() !== "") {
      proteinBounds.push(`<= ${filters.protein_max}`);
    }
    if (proteinBounds.length) {
      advancedChips.push({
        key: "protein_range",
        label: "Protein",
        count: proteinBounds.length,
        summary: proteinBounds.join(" "),
      });
    }
    if (filters?.protein_name_like) {
      advancedChips.push({
        key: "protein_name_like",
        label: "Protein Name",
        count: 1,
        summary: String(filters.protein_name_like),
      });
    }

    const yieldBounds = [];
    if (filters?.yield_min !== null && filters?.yield_min !== undefined && `${filters.yield_min}`.trim() !== "") {
      yieldBounds.push(`>= ${filters.yield_min}%`);
    }
    if (filters?.yield_max !== null && filters?.yield_max !== undefined && `${filters.yield_max}`.trim() !== "") {
      yieldBounds.push(`<= ${filters.yield_max}%`);
    }
    if (yieldBounds.length) {
      advancedChips.push({
        key: "yield_range",
        label: "Yield",
        count: yieldBounds.length,
        summary: yieldBounds.join(" "),
      });
    }

    if (filters?.complete_months_only === true) {
      advancedChips.push({
        key: "complete_months_only",
        label: "Month Window",
        count: 1,
        summary: "Full months only",
      });
    }

    const dateLabel = formatDateLabel(filters);
    return {
      dateLabel,
      activeCount: dimensionChips.length + advancedChips.length + (dateLabel ? 1 : 0),
      dimensionChips: dimensionChips.concat(advancedChips),
      chips: [{ key: "date", label: "Date", summary: dateLabel }].concat(dimensionChips, advancedChips),
    };
  };

  const localBuildFilterQS = (extra = {}) => {
    const baseFilters =
      window.FilterState && typeof window.FilterState.fromForm === "function"
        ? window.FilterState.fromForm(document.getElementById("filtersForm"))
        : gatherFilters();
    const merged = stableFilters({ ...(baseFilters || {}), ...(extra || {}) });
    if (window.FilterState && typeof window.FilterState.toQueryString === "function") {
      return window.FilterState.toQueryString(merged);
    }

    const params = new URLSearchParams();
    if (merged.start) params.set("start", merged.start);
    if (merged.end) params.set("end", merged.end);
    if (merged.date_preset) params.set("date_preset", merged.date_preset);
    if (merged.date_type) params.set("date_type", merged.date_type);
    ["statuses", "regions", "methods", "customers", "suppliers", "products", "sales_reps"].forEach((key) => {
      (merged[key] || []).forEach((value) => params.append(key, value));
    });
    params.set("_gf", "1");

    const current = new URLSearchParams(window.location.search || "");
    current.forEach((value, key) => {
      if (params.has(key)) return;
      if (FILTER_KEY_REGEX.test(key)) return;
      params.append(key, value);
    });
    return params.toString();
  };

  if (typeof window !== "undefined" && typeof window.buildFilterQS !== "function") {
    window.buildFilterQS = localBuildFilterQS;
  }

  const normalizeItem = (item) => {
    if (item == null) return null;
    if (typeof item === "object") {
      const value =
        item.id ?? item.value ?? item.code ?? item.slug ?? item.name ?? item.label;
      const label = item.label ?? item.name ?? item.title ?? value;
      if (value == null) return null;
      return { value: String(value).trim(), label: String(label ?? value).trim() };
    }
    return { value: String(item).trim(), label: String(item).trim() };
  };

  const mergeOptions = (el, items, selectedValues) => {
    const normalized = Array.isArray(items)
      ? items
          .map(normalizeItem)
          .filter(Boolean)
          .filter(({ value }) => !/^all$/i.test(String(value || "")))
      : [];
    setLabelMap(el, normalized);

    const existing = new Map(
      Array.from(el.options)
        .filter((option) => !/^all$/i.test(String(option.value || "")))
        .map((option) => [String(option.value), option])
    );

    const keep = new Set(normalized.map(({ value }) => value));
    Array.from(selectedValues || []).forEach((value) => keep.add(String(value)));

    existing.forEach((option, value) => {
      if (!keep.has(value)) {
        option.remove();
        existing.delete(value);
      }
    });

    const fragment = document.createDocumentFragment();
    normalized.forEach(({ value, label }) => {
      if (existing.has(value)) {
        existing.get(value).textContent = label;
        return;
      }
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      fragment.appendChild(option);
      existing.set(value, option);
    });
    if (fragment.childNodes.length) el.appendChild(fragment);

    Array.from(el.options).forEach((option) => {
      option.selected = selectedValues.has(String(option.value));
    });
  };

  const resolveOptionsList = (options, key) => {
    if (key === "methods") return options.methods || options.ship_methods || options.shipping_methods || [];
    return options[key] || [];
  };

  const aliasOptions = (rawOptions, keys = []) => {
    for (const key of keys) {
      const items = rawOptions && Array.isArray(rawOptions[key]) ? rawOptions[key] : null;
      if (items) return items;
    }
    return [];
  };

  const normalizeOptionsPayload =
    typeof window !== "undefined" && typeof window.normalizeOptionsPayload === "function"
      ? window.normalizeOptionsPayload
      : (payload) => {
          const source = payload && typeof payload === "object" ? payload : {};
          const rawOptions = source.options && typeof source.options === "object" ? source.options : source;
          const output = { ...source, options: {} };
          output.options.statuses = aliasOptions(rawOptions, ["statuses"]).map(normalizeItem).filter(Boolean).map((item) => ({
            id: item.value,
            label: item.label || item.value,
            bucket: "statuses",
            value: item.value,
          }));
          output.options.regions = aliasOptions(rawOptions, ["regions"]).map(normalizeItem).filter(Boolean).map((item) => ({
            id: item.value,
            label: item.label || item.value,
            bucket: "regions",
            value: item.value,
          }));
          output.options.methods = aliasOptions(rawOptions, ["methods", "shipping_methods", "ship_methods"]).map(normalizeItem).filter(Boolean).map((item) => ({
            id: item.value,
            label: item.label || item.value,
            bucket: "methods",
            value: item.value,
          }));
          output.options.ship_methods = aliasOptions(rawOptions, ["ship_methods", "shipping_methods", "methods"]).map(normalizeItem).filter(Boolean).map((item) => ({
            id: item.value,
            label: item.label || item.value,
            bucket: "ship_methods",
            value: item.value,
          }));
          ["customers", "suppliers", "products", "sales_reps"].forEach((key) => {
            output.options[key] = aliasOptions(rawOptions, [key]).map(normalizeItem).filter(Boolean).map((item) => ({
              id: item.value,
              label: item.label || item.value,
              bucket: key,
              value: item.value,
            }));
          });
          if (!output.options.ship_methods.length && output.options.methods.length) {
            output.options.ship_methods = output.options.methods.map((item) => ({ ...item, bucket: "ship_methods" }));
          }
          return output;
        };

  const buildOptionsIndex = (options) => {
    const index = {};
    Object.entries(options || {}).forEach(([key, items]) => {
      index[key] = new Set(
        (items || [])
          .map((item) => item?.id ?? item?.value ?? item)
          .filter((value) => value !== undefined && value !== null)
          .map((value) => String(value).trim())
          .filter(Boolean)
      );
    });
    return index;
  };

  const validateOptionsPayload = (payload) => {
    const options = payload && payload.options ? payload.options : payload || {};
    Object.entries(options || {}).forEach(([key, items]) => {
      if (!Array.isArray(items)) {
        throw new Error(`Invalid options payload for ${key}`);
      }
      items.forEach((item) => {
        if (!item || typeof item !== "object") {
          throw new Error(`Invalid option entry for ${key}`);
        }
      });
    });
    return true;
  };

  if (typeof window !== "undefined") {
    window.validateOptionsPayload = validateOptionsPayload;
  }

  const applyOptions = (options, selectedFilters = {}) => {
    DIMENSIONS.forEach((config) => {
      const el = document.getElementById(config.id);
      if (!el) return;
      const selected = new Set(stableList(selectedFilters[config.key]));
      mergeOptions(el, resolveOptionsList(options || {}, config.key), selected);
      if (el._msx && typeof el._msx.buildRows === "function") {
        el._msx.buildRows();
        el._msx.setValue(Array.from(selected));
      }
    });
  };

  const applyDimensionAvailability = (options, selectedFilters = {}) => {
    DIMENSIONS.forEach((config) => {
      const tile = document.querySelector(`.filter-tile[data-filter-key="${config.key}"]`);
      const panel = document.querySelector(`.filter-workspace[data-filter-key="${config.key}"]`);
      if (!tile) return;
      const available = resolveOptionsList(options || {}, config.key) || [];
      const selected = stableList(selectedFilters?.[config.key]);
      const isUnavailable = available.length === 0 && selected.length === 0;
      tile.disabled = isUnavailable;
      tile.dataset.unavailable = isUnavailable ? "1" : "0";
      tile.setAttribute("aria-disabled", isUnavailable ? "true" : "false");
      const hint = tile.querySelector(".filter-tile__hint");
      if (hint) hint.textContent = isUnavailable ? "No values available" : "Open selector";
      if (panel) panel.dataset.unavailable = isUnavailable ? "1" : "0";
      if (isUnavailable && state.activeDimensionKey === config.key) {
        setActiveDimension("");
      }
    });
  };

  const applyDimensionHealth = (dimensionMeta = {}) => {
    state.dimensionHealth = dimensionMeta || {};
    DIMENSIONS.forEach((config) => {
      const tile = document.querySelector(`.filter-tile[data-filter-key="${config.key}"]`);
      const panel = document.querySelector(`.filter-workspace[data-filter-key="${config.key}"]`);
      const hint = tile?.querySelector(".filter-tile__hint");
      const help = panel?.querySelector(".filter-workspace__help");
      const meta = state.dimensionHealth[config.key] || {};
      const hasError = meta.status === "error" && !meta.preserved;
      const hasPreservedFallback = meta.status === "error" && !!meta.preserved;
      if (tile) {
        tile.classList.toggle("is-error", hasError);
        tile.dataset.error = hasError ? "1" : "0";
      }
      if (panel) panel.dataset.error = hasError ? "1" : "0";
      if (hint && tile?.dataset.unavailable !== "1") {
        hint.textContent = hasError ? "Retry unavailable" : hasPreservedFallback ? "Using saved values" : "Open selector";
      }
      if (help) {
        if (!help.dataset.baseText) help.dataset.baseText = help.textContent || "";
        help.textContent = hasError
          ? `${help.dataset.baseText} This dimension is temporarily unavailable.`
          : hasPreservedFallback
            ? `${help.dataset.baseText} Using last known values while live refresh is unavailable.`
            : help.dataset.baseText;
      }
    });
  };

  const normalizeDimensionList = (values = []) => {
    const list = Array.isArray(values) ? values : [values];
    const normalized = [];
    const seen = new Set();
    list.forEach((value) => {
      const token = String(value || "").trim().toLowerCase().replace(/-/g, "_");
      if (!token) return;
      const mapped =
        token === "shipping_methods" || token === "ship_methods" || token === "shipping_method" || token === "ship_method"
          ? "methods"
          : token === "salesrep" || token === "salesreps" || token === "sales_rep"
            ? "sales_reps"
            : token;
      if (!DIMENSIONS.some((config) => config.key === mapped) && mapped !== "statuses") return;
      if (seen.has(mapped)) return;
      seen.add(mapped);
      normalized.push(mapped);
    });
    return normalized;
  };

  const selectedDimensionKeys = (filters) =>
    DIMENSIONS.filter((config) => stableList(filters?.[config.key]).length > 0).map((config) => config.key);

  const resolveBootstrapDimensions = (filters) =>
    normalizeDimensionList([...BOOTSTRAP_OPTION_DIMENSIONS, ...selectedDimensionKeys(filters)]);

  const resolveRemainingDimensions = () =>
    normalizeDimensionList(
      DIMENSIONS.map((config) => config.key).filter((key) => !state.loadedDimensions.has(key))
    );

  const resolveDomBootstrapDimensions = () =>
    normalizeDimensionList(
      DIMENSIONS.filter((config) => document.getElementById(config.id)).map((config) => config.key)
    );

  const countOptionItems = (options = {}) => {
    const counts = {};
    Object.entries(options || {}).forEach(([key, items]) => {
      counts[key] = Array.isArray(items) ? items.length : 0;
    });
    return counts;
  };

  const extractDomOptionsForDimension = (key) => {
    const config = DIMENSIONS.find((candidate) => candidate.key === key);
    const el = config ? document.getElementById(config.id) : null;
    if (!el) return [];
    return Array.from(el.options || [])
      .map((option) => {
        const value = String(option?.value || "").trim();
        if (!value || /^all$/i.test(value)) return null;
        const label = String(option?.textContent || option?.label || value).trim() || value;
        return { id: value, label, bucket: key, value };
      })
      .filter(Boolean);
  };

  const buildDomOptionsPayload = ({ dimensions = [], source = "dom-select-fallback" } = {}) => {
    const requested = normalizeDimensionList(dimensions.length ? dimensions : resolveDomBootstrapDimensions());
    const options = {};
    const dimensionMeta = {};
    requested.forEach((key) => {
      const items = extractDomOptionsForDimension(key);
      options[key] = items;
      if (key === "methods") {
        options.ship_methods = items.map((item) => ({ ...item, bucket: "ship_methods" }));
      }
      dimensionMeta[key] = {
        status: items.length ? "ready" : "unavailable",
        duration_ms: 0,
        error: null,
        option_count: items.length,
        cached: true,
        source,
      };
    });
    return {
      options,
      dataset_version: state.datasetVersion || null,
      scope: state.scopePayload || {},
      filters: stableFilters(gatherFilters()),
      meta: {
        requested_dimensions: requested,
        option_counts: countOptionItems(options),
        dimension_meta: dimensionMeta,
        partial_failures: [],
        degraded: false,
        source,
        dom_fallback: true,
      },
    };
  };

  const hasUsableOptionsPayload = (payload, requestedDimensions = []) => {
    const requested = normalizeDimensionList(
      requestedDimensions.length ? requestedDimensions : payload?.meta?.requested_dimensions || Object.keys(payload?.options || {})
    );
    if (!requested.length) return false;
    return requested.every((key) => {
      const available = resolveOptionsList(payload?.options || {}, key) || [];
      if (available.length) return true;
      const config = DIMENSIONS.find((candidate) => candidate.key === key);
      const el = config ? document.getElementById(config.id) : null;
      if (!el) return false;
      const selected = stableList(gatherFilters()?.[key]);
      if (!selected.length) return false;
      const domValues = new Set(
        Array.from(el.options || [])
          .map((option) => String(option?.value || "").trim())
          .filter(Boolean)
      );
      return selected.every((value) => domValues.has(String(value)));
    });
  };

  const hasHydratableOptionsPayload = (payload) => {
    const requested = normalizeDimensionList(payload?.meta?.requested_dimensions || Object.keys(payload?.options || {}));
    if (!requested.length) return false;
    return requested.some((key) => {
      const available = resolveOptionsList(payload?.options || {}, key) || [];
      if (available.length) return true;
      return stableList(payload?.filters?.[key]).length > 0;
    });
  };

  const resolveInlineDeferredDimensions = (payload) =>
    normalizeDimensionList([
      ...resolveRemainingDimensions(),
      ...(payload?.meta?.partial_failures || []),
      ...(payload?.meta?.stale_dimensions || []),
    ]);

  const optionsStorageUserId = () => {
    try {
      return String((window.__FILTER_CTX__ && window.__FILTER_CTX__.user_id) || "anon").trim() || "anon";
    } catch (_err) {
      return "anon";
    }
  };

  const optionsStorageKey = () => `${OPTIONS_STORAGE_KEY}::${pageKey()}::${optionsStorageUserId()}`;

  const persistOptionsPayload = (payload) => {
    if (typeof window === "undefined" || !payload) return;
    try {
      if (!window.localStorage) return;
      const normalized = normalizeOptionsPayload(payload || {});
      const requested = normalizeDimensionList(normalized?.meta?.requested_dimensions || Object.keys(normalized?.options || {}));
      if (!requested.length) return;
      window.localStorage.setItem(
        optionsStorageKey(),
        JSON.stringify({
          saved_at: Date.now(),
          dataset_version: normalized?.dataset_version || normalized?.datasetVersion || state.datasetVersion || null,
          scope_hash: state.scopePayload?.scope_hash || null,
          etag: state.optionsEtag || normalized?.etag || normalized?.meta?.etag || null,
          requested_dimensions: requested,
          payload: normalized,
        })
      );
    } catch (_err) {
      /* ignore */
    }
  };

  const readPersistedOptionsPayload = ({ dimensions = [] } = {}) => {
    if (typeof window === "undefined") return null;
    try {
      if (!window.localStorage) return null;
      const key = optionsStorageKey();
      const raw = window.localStorage.getItem(key);
      if (!raw) return null;
      const envelope = JSON.parse(raw);
      if (!envelope || typeof envelope !== "object") return null;

      const savedAt = Number(envelope.saved_at || 0);
      if (savedAt && Date.now() - savedAt > OPTIONS_STORAGE_TTL_MS) {
        window.localStorage.removeItem(key);
        return null;
      }

      const currentDatasetVersion = state.datasetVersion || null;
      const cachedDatasetVersion = envelope.dataset_version || null;
      if (currentDatasetVersion && cachedDatasetVersion && currentDatasetVersion !== cachedDatasetVersion) {
        return null;
      }

      const currentScopeHash = state.scopePayload?.scope_hash || null;
      const cachedScopeHash = envelope.scope_hash || null;
      if (currentScopeHash && cachedScopeHash && currentScopeHash !== cachedScopeHash) {
        return null;
      }

      const payload = normalizeOptionsPayload(envelope.payload || {});
      const requested = normalizeDimensionList(
        dimensions.length
          ? dimensions
          : envelope.requested_dimensions || payload?.meta?.requested_dimensions || Object.keys(payload?.options || {})
      );
      if (!requested.length || !hasUsableOptionsPayload(payload, requested)) {
        return null;
      }

      payload.meta = {
        ...(payload.meta || {}),
        requested_dimensions: requested,
        cached: true,
        stale: true,
        persisted: true,
        source: "local-storage",
        persisted_at: savedAt || null,
      };
      if (envelope.etag) payload.etag = envelope.etag;
      if (!payload.dataset_version && cachedDatasetVersion) payload.dataset_version = cachedDatasetVersion;
      if ((!payload.scope || typeof payload.scope !== "object" || !Object.keys(payload.scope).length) && state.scopePayload) {
        payload.scope = state.scopePayload;
      }
      return payload;
    } catch (_err) {
      return null;
    }
  };

  const hydratePersistedOptions = ({ dimensions = [], syncFilters = false, source = "local-storage" } = {}) => {
    const payload = readPersistedOptionsPayload({ dimensions });
    if (!payload) return null;
    payload.meta = { ...(payload.meta || {}), source };
    return applyOptionsPayload(payload, { syncFilters, persist: false });
  };

  const mergeOptionsPayload = (payload) => {
    const normalized = normalizeOptionsPayload(payload || {});
    const requested = normalizeDimensionList(normalized?.meta?.requested_dimensions || Object.keys(normalized?.options || {}));
    const base = state.lastOptionsPayload && typeof state.lastOptionsPayload === "object"
      ? state.lastOptionsPayload
      : { options: {} };
    const baseDimensionMeta =
      base?.meta?.dimension_meta && typeof base.meta.dimension_meta === "object"
        ? base.meta.dimension_meta
        : {};
    const incomingDimensionMeta =
      normalized?.meta?.dimension_meta && typeof normalized.meta.dimension_meta === "object"
        ? normalized.meta.dimension_meta
        : {};
    const mergedDimensionMeta = { ...baseDimensionMeta, ...incomingDimensionMeta };
    const merged = {
      ...base,
      ...normalized,
      options: { ...(base.options || {}) },
      meta: { ...(base.meta || {}), ...(normalized.meta || {}), dimension_meta: mergedDimensionMeta },
    };
    requested.forEach((key) => {
      const nextList = cloneOptionsList(resolveOptionsList(normalized.options || {}, key) || []);
      const previousList = cloneOptionsList(resolveOptionsList(base.options || {}, key) || []);
      const meta =
        mergedDimensionMeta[key] && typeof mergedDimensionMeta[key] === "object"
          ? { ...mergedDimensionMeta[key] }
          : {};
      const shouldPreserve = meta.status === "error" && previousList.length > 0 && nextList.length === 0;
      merged.options[key] = shouldPreserve ? previousList : nextList;
      if (shouldPreserve) {
        mergedDimensionMeta[key] = {
          ...meta,
          preserved: true,
          option_count: previousList.length,
        };
      } else if (Object.keys(meta).length) {
        mergedDimensionMeta[key] = {
          ...meta,
          preserved: false,
        };
      }
      if (key === "methods") {
        const nextShipMethods = cloneOptionsList(normalized.options?.ship_methods || merged.options.methods || []);
        const previousShipMethods = cloneOptionsList((base.options || {}).ship_methods || previousList);
        const shipMethodMeta =
          mergedDimensionMeta.ship_methods && typeof mergedDimensionMeta.ship_methods === "object"
            ? { ...mergedDimensionMeta.ship_methods }
            : mergedDimensionMeta.methods && typeof mergedDimensionMeta.methods === "object"
              ? { ...mergedDimensionMeta.methods }
              : {};
        const preserveShipMethods =
          (shipMethodMeta.status === "error" || shouldPreserve) &&
          previousShipMethods.length > 0 &&
          nextShipMethods.length === 0;
        merged.options.ship_methods = preserveShipMethods ? previousShipMethods : nextShipMethods;
        if (preserveShipMethods) {
          mergedDimensionMeta.ship_methods = {
            ...shipMethodMeta,
            preserved: true,
            option_count: previousShipMethods.length,
          };
        } else if (Object.keys(shipMethodMeta).length) {
          mergedDimensionMeta.ship_methods = {
            ...shipMethodMeta,
            preserved: false,
          };
        }
      }
      state.loadedDimensions.add(key);
    });
    merged.meta.requested_dimensions = Array.from(state.loadedDimensions);
    state.lastOptionsPayload = merged;
    return merged;
  };

  const publishReady = () => {
    const currentFilters = getAppliedFilters();
    updateSummary();
    state.lastReadyDetail = {
      filters: currentFilters,
      qs: state.appliedQs || buildQueryStringForFilters(currentFilters),
      datasetVersion: state.datasetVersion,
      scope: state.scopePayload,
      optionsEtag: state.optionsEtag,
      optionsMs: state.optionsFetchMs,
    };
    if (state.readyPublished) return state.lastReadyDetail;
    state.readyPublished = true;
    document.dispatchEvent(new CustomEvent("filters:ready", { detail: state.lastReadyDetail }));
    document.dispatchEvent(new CustomEvent("globalFilters:ready", { detail: state.lastReadyDetail }));
    window.dispatchEvent(new CustomEvent("globalFilters:ready", { detail: state.lastReadyDetail }));
    readyDeferred.resolve(state.lastReadyDetail);
    window.__FILTERS_READY = true;
    return state.lastReadyDetail;
  };

  const applyOptionsPayload = (payload, { syncFilters = false, persist = true } = {}) => {
    if (!payload) return state.lastOptionsPayload;
    const mergedPayload = mergeOptionsPayload(payload);
    validateOptionsPayload(mergedPayload);
    state.datasetVersion = mergedPayload?.dataset_version || mergedPayload?.datasetVersion || state.datasetVersion;
    state.scopePayload = mergedPayload?.scope || state.scopePayload;
    state.scopeNotice = mergedPayload?.meta?.filters_notice || state.scopeNotice;
    state.optionsEtag = mergedPayload?.etag || mergedPayload?.meta?.etag || state.optionsEtag;
    const activeFilters = syncFilters
      ? normalizeServerFilters(
          mergedPayload && typeof mergedPayload.filters === "object" ? mergedPayload.filters : gatherFilters()
        )
      : stableFilters(state.pendingFilters || gatherFilters());

    if (syncFilters) {
      setAppliedState(activeFilters);
      if (window.FilterState && typeof window.FilterState.set === "function") {
        window.FilterState.set(activeFilters, { persist: true });
      }
    }

    applyOptions(mergedPayload.options || {}, activeFilters);
    applyDimensionAvailability(mergedPayload.options || {}, activeFilters);
    applyDimensionHealth(mergedPayload?.meta?.dimension_meta || {});
    if (syncFilters && window.FilterState && typeof window.FilterState.hydrateForm === "function") {
      window.FilterState.hydrateForm(document.getElementById("filtersForm"));
    }
    enhanceSelects();
    ensureDefaultPreset();
    updateNoticeBanner(state.scopeNotice);
    clearFilterError();
    if (syncFilters) {
      state.baselineHash = filtersHash(activeFilters);
    }
    publishReady();
    state.optionsState = mergedPayload?.meta?.degraded ? "failed_partial" : "ready";
    if (!mergedPayload?.meta?.degraded && hasUsableOptionsPayload(mergedPayload, Array.from(state.loadedDimensions))) {
      state.lastHealthyOptionsPayload = clonePayload(mergedPayload);
    }
    state.optionsFailureCount = 0;
    state.optionsCooldownUntil = 0;
    if (persist) persistOptionsPayload(mergedPayload);
    return mergedPayload;
  };

  const hydrateDomOptions = ({ dimensions = [], syncFilters = false, source = "dom-select-fallback" } = {}) => {
    const requested = normalizeDimensionList(dimensions.length ? dimensions : resolveDomBootstrapDimensions());
    if (!requested.length) return null;
    const payload = buildDomOptionsPayload({ dimensions: requested, source });
    if (!hasUsableOptionsPayload(payload, requested)) return null;
    return applyOptionsPayload(payload, { syncFilters, persist: false });
  };

  const hydrateOptions = async ({ dimensions = [], timeoutMs = null, syncFilters = false, bypassCooldown = false, phase = "interactive" } = {}) => {
    const requested = normalizeDimensionList(dimensions);
    if (!requested.length) return state.lastOptionsPayload;
    state.optionsState = "loading";
    const payload = await fetchOptions({ dimensions: requested, timeoutMs, bypassCooldown, phase });
    if (!payload) return state.lastOptionsPayload;
    return applyOptionsPayload(payload, { syncFilters, persist: true });
  };

  const refreshOptionsInBackground = ({ dimensions = [], timeoutMs = DEFERRED_OPTIONS_TIMEOUT_MS, phase = "interactive" } = {}) => {
    const requested = normalizeDimensionList(dimensions);
    if (!requested.length) return Promise.resolve(state.lastOptionsPayload);
    const refreshKey = backgroundRefreshKey({ dimensions: requested, phase });
    if (shouldSkipBackgroundRefresh(refreshKey)) {
      dlog("filters options refresh skipped", { page: pageKey(), phase, dimensions: requested });
      return Promise.resolve(state.lastOptionsPayload);
    }
    markBackgroundRefresh(refreshKey, "started");
    return hydrateOptions({ dimensions: requested, timeoutMs, phase })
      .then((payload) => {
        markBackgroundRefresh(refreshKey, "success");
        if (state.optionsState === "ready" || state.optionsState === "failed_partial") clearFilterError();
        return payload;
      })
      .catch((err) => {
        markBackgroundRefresh(refreshKey, "failed");
        if (!state.lastOptionsPayload) {
          const persistedPayload = hydratePersistedOptions({
            dimensions: requested,
            syncFilters: false,
            source: `local-storage-${phase}-fallback`,
          });
          if (!persistedPayload) {
            const fallbackPayload = buildDomOptionsPayload({ dimensions: requested, source: `dom-${phase}-fallback` });
            if (hasUsableOptionsPayload(fallbackPayload, requested)) {
              applyOptionsPayload(fallbackPayload, { syncFilters: false, persist: false });
            }
          }
        }
        const hasUsableOptions = !!(state.lastHealthyOptionsPayload || state.lastOptionsPayload);
        state.optionsState = hasUsableOptions && state.lastHealthyOptionsPayload ? "ready" : hasUsableOptions ? "failed_partial" : "failed";
        if (hasUsableOptions) {
          if (isBackgroundOptionsPhase(phase) || isTimeoutError(err)) {
            dlog("filters options fallback", { page: pageKey(), phase, error: err?.message || err });
          } else {
            console.warn(`filters.options.${phase}.fail page=${pageKey()} err=${err?.message || err}`);
          }
        } else {
          console.error(`filters.options.${phase}.fail page=${pageKey()} err=${err?.message || err}`);
          showFilterError(err?.message || "Some filter options are temporarily unavailable.");
        }
        return state.lastOptionsPayload;
      });
  };

  const enhanceSelects = () => {
    document.querySelectorAll("select[data-msx]").forEach((select) => {
      const container = document.querySelector(`.msx[data-enhance="${select.id}"]`);
      if (!container) return;
      if (select._msx) return;
      try {
        select._msx = new MultiSelectX(select, container);
      } catch (err) {
        console.error("filters msx init failed", err);
      }
    });
  };

  const setMultiValue = (id, values) => {
    const el = document.getElementById(id);
    if (!el) return;
    const list = stableList(values);
    if (el._msx && typeof el._msx.setValue === "function") {
      el._msx.setValue(list);
      return;
    }
    Array.from(el.options).forEach((option) => {
      option.selected = list.includes(String(option.value));
    });
    el.dispatchEvent(new Event("change", { bubbles: true }));
  };

  const setPreset = (preset, { syncPicker = true } = {}) => {
    const token = String(preset || "").trim().toLowerCase();
    const hidden = document.getElementById("fDatePreset");
    if (hidden) hidden.value = token;
    const dateTypeInput = document.getElementById("fDateType");
    if (dateTypeInput) {
      dateTypeInput.value = normalizeDateType(dateTypeInput.value, token) || "fiscal";
      if (token === "all") dateTypeInput.value = "fiscal";
    }
    const picker = document.getElementById("date-range-preset-picker");
    if (picker && syncPicker) picker.value = token;

    const startInput = document.getElementById("fStart");
    const endInput = document.getElementById("fEnd");
    const range = presetRange(token);
    if (range.start !== null && startInput) startInput.value = range.start || "";
    if (range.end !== null && endInput) endInput.value = range.end || "";
    if (token === "custom" && startInput) startInput.focus();
    updateQuickRangeButtons();
  };

  const detectPresetFromDates = (filters) => {
    const token = String(filters?.date_preset || "").trim().toLowerCase();
    if (token === "last_3_months") return "90d";
    if (token === "last_7_days") return "7d";
    if (token === "last_30_days") return "30d";
    if (token === "last_month") return "last-month";
    if (token === "last_quarter") return "last-quarter";
    if (token && token !== "custom") return token;
    if (filters?.start || filters?.end) return "custom";
    return "all";
  };

  const updateQuickRangeButtons = () => {
    const filters = gatherFilters();
    const activePreset = detectPresetFromDates(filters);
    document.querySelectorAll(".filter-range-pill[data-range]").forEach((button) => {
      button.classList.toggle("is-active", String(button.dataset.range || "").toLowerCase() === activePreset);
    });
  };

  const ensureDefaultPreset = () => {
    const filters = gatherFilters();
    if (!filters.start && !filters.end && !filters.date_preset) {
      setPreset("current_fy");
    } else {
      updateQuickRangeButtons();
    }
  };

  const syncFilterCard = (config, filters) => {
    const values = stableList(filters?.[config.key]);
    const summaryEl = document.querySelector(`[data-summary-for="${config.id}"]`);
    const workspaceSummaryEl = document.querySelector(`[data-workspace-summary-for="${config.id}"]`);
    const workspaceCountEl = document.querySelector(`[data-workspace-count-for="${config.id}"]`);
    const countEl = document.getElementById(config.countId);
    const tile = document.querySelector(`.filter-tile[data-filter-key="${config.key}"]`);
    const summaryText = values.length ? summarizeValues(config.key, values, 2) : config.emptyLabel;
    const countText = values.length ? `${values.length}` : "All";
    if (summaryEl) summaryEl.textContent = summaryText;
    if (workspaceSummaryEl) workspaceSummaryEl.textContent = summaryText;
    if (countEl) countEl.textContent = countText;
    if (workspaceCountEl) workspaceCountEl.textContent = countText;
    if (tile) {
      tile.dataset.active = values.length ? "1" : "0";
      tile.classList.toggle("has-value", values.length > 0);
    }
  };

  const renderHeaderChips = (summary) => {
    const container = document.getElementById("filtersHeaderChips");
    if (!container) return;
    container.innerHTML = "";
    summary.dimensionChips.slice(0, 2).forEach((chip) => {
      const pill = document.createElement("span");
      pill.className = "filter-chip filter-chip--muted";
      pill.textContent = `${chip.label}: ${chip.summary}`;
      container.appendChild(pill);
    });
    if (summary.dimensionChips.length > 2) {
      const pill = document.createElement("span");
      pill.className = "filter-chip filter-chip--muted";
      pill.textContent = `+${summary.dimensionChips.length - 2} more`;
      container.appendChild(pill);
    }
  };

  const renderAppliedChips = (summary) => {
    const container = document.getElementById("filtersAppliedChips");
    const emptyState = document.getElementById("filtersSummaryEmpty");
    if (!container) return;
    container.innerHTML = "";
    summary.chips.forEach((chip) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `filter-chip filter-chip--removable${chip.key === "date" ? " is-date" : ""}`;
      button.dataset.clearFilter = chip.key;
      button.innerHTML = `
        <span class="filter-chip__label">${chip.label}</span>
        <span class="filter-chip__value">${chip.summary}</span>
        <span class="filter-chip__remove" aria-hidden="true"><i class="bi bi-x-lg"></i></span>
      `;
      container.appendChild(button);
    });
    if (emptyState) emptyState.classList.toggle("d-none", summary.chips.length > 0);
  };

  const updateNoticeBanner = (message) => {
    const banner = document.getElementById("filtersNoticeBanner");
    if (!banner) return;
    const text = String(message || "").trim();
    banner.textContent = text;
    banner.classList.toggle("d-none", !text);
  };

  const updateLastAppliedLabel = (stamp) => {
    const label = document.getElementById("filtersLastAppliedLabel");
    if (!label) return;
    const raw = String(stamp || "").trim();
    if (!raw) {
      label.textContent = "Last applied not recorded";
      return;
    }
    const parsed = new Date(raw);
    if (Number.isNaN(parsed.getTime())) {
      label.textContent = `Last applied ${raw}`;
      return;
    }
    label.textContent = `Last applied ${parsed.toLocaleString()}`;
  };

  const updateSavedViewButtons = (filters) => {
    const loadBtn = document.getElementById("loadViewBtn");
    const deleteBtn = document.getElementById("deleteViewBtn");
    const updateBtn = document.getElementById("updateSavedViewBtn");
    const selected = state.savedViews.find((view) => String(view.id) === String(state.selectedSavedViewId || ""));
    const currentHash = filtersHash(filters);
    if (loadBtn) loadBtn.disabled = !selected;
    if (deleteBtn) deleteBtn.disabled = !selected;
    if (updateBtn) updateBtn.disabled = !selected || String(selected.filters_hash || "") === currentHash;
  };

  const renderSavedViewState = (filters) => {
    const currentHash = filtersHash(filters);
    const activeView = state.savedViews.find((view) => String(view.id) === String(state.activeSavedViewId || ""));
    const badge = document.getElementById("filtersSavedViewBadge");
    const viewStateText = document.getElementById("filtersViewStateText");
    const selectionSummary = document.getElementById("savedViewsSelectionSummary");
    const pendingState = document.getElementById("filtersPendingState");
    const hasPendingChanges = state.baselineHash && currentHash !== state.baselineHash;

    if (pendingState) {
      pendingState.textContent = hasPendingChanges ? "Pending changes" : "Applied state";
      pendingState.dataset.pending = hasPendingChanges ? "1" : "0";
    }

    if (!activeView) {
      if (badge) {
        badge.className = "filters-status-badge is-neutral";
        badge.textContent = "No saved view linked";
      }
      if (viewStateText) viewStateText.textContent = hasPendingChanges ? "Apply filters to refresh the page state." : "Save a view to reuse this state later.";
      if (selectionSummary && state.savedViews.length) selectionSummary.textContent = "Select a saved view to load, update, or delete it.";
      updateSavedViewButtons(filters);
      return;
    }

    const matchesActive = String(activeView.filters_hash || "") === currentHash;
    if (badge) {
      badge.className = `filters-status-badge ${matchesActive ? "is-neutral" : "is-warning"}`;
      badge.textContent = matchesActive ? `Synced to ${activeView.name}` : `Differs from ${activeView.name}`;
    }
    if (viewStateText) {
      viewStateText.textContent = matchesActive
        ? "Current filters match the active saved view."
        : "Current filters differ from the active saved view.";
    }
    if (selectionSummary) {
      const selected = state.savedViews.find((view) => String(view.id) === String(state.selectedSavedViewId || ""));
      selectionSummary.textContent = selected
        ? `Selected view: ${selected.name}`
        : `Active view: ${activeView.name}`;
    }
    updateSavedViewButtons(filters);
  };

  const updateActionState = (filters = null) => {
    const pendingFilters = stableFilters(filters || state.pendingFilters || gatherFilters());
    state.pendingFilters = pendingFilters;
    state.pendingHash = filtersHash(pendingFilters);
    const appliedHash = state.baselineHash || filtersHash(getAppliedFilters());
    const hasPendingChanges = appliedHash !== state.pendingHash;
    const applyBtn = document.getElementById("filtersApply");
    const spinner = document.getElementById("filtersApplySpinner");
    const icon = document.getElementById("filtersApplyIcon");
    const busy = ["bootstrapping", "applying", "saving_view", "loading_view"].includes(state.lifecycle);
    if (applyBtn) {
      applyBtn.disabled = busy || !hasPendingChanges;
      applyBtn.setAttribute("aria-disabled", applyBtn.disabled ? "true" : "false");
    }
    if (spinner) spinner.classList.toggle("d-none", state.lifecycle !== "applying");
    if (icon) icon.classList.toggle("d-none", state.lifecycle === "applying");
    const pendingState = document.getElementById("filtersPendingState");
    if (pendingState && state.lifecycle === "bootstrapping") {
      pendingState.textContent = "Loading filters";
      pendingState.dataset.pending = "0";
    } else if (pendingState && state.lifecycle === "failed_partial") {
      pendingState.textContent = hasPendingChanges ? "Pending changes" : "Partial filter outage";
      pendingState.dataset.pending = hasPendingChanges ? "1" : "0";
    } else if (pendingState && state.lifecycle === "failed_fatal") {
      pendingState.textContent = "Filters unavailable";
      pendingState.dataset.pending = "0";
    }
    return hasPendingChanges;
  };

  const visibleTileButtons = () =>
    Array.from(document.querySelectorAll(".filter-tile[data-filter-key]")).filter((button) => !button.disabled);

  const workspacePanelIdForKey = (key = "") => {
    const button = document.querySelector(`.filter-tile[data-filter-key="${key}"]`);
    return button?.dataset?.filterPanel || "";
  };

  const setActiveDimension = (key = "", { focusSearch = false } = {}) => {
    const nextKey = String(key || "");
    const nextPanelId = workspacePanelIdForKey(nextKey);
    state.activeDimensionKey = nextPanelId ? nextKey : "";

    document.querySelectorAll(".filter-tile[data-filter-key]").forEach((button) => {
      const isActive = state.activeDimensionKey && String(button.dataset.filterKey || "") === state.activeDimensionKey;
      button.classList.toggle("is-open", !!isActive);
      button.setAttribute("aria-expanded", isActive ? "true" : "false");
    });

    document.querySelectorAll(".filter-workspace[data-filter-key]").forEach((panel) => {
      const isActive = state.activeDimensionKey && String(panel.dataset.filterKey || "") === state.activeDimensionKey;
      panel.hidden = !isActive;
      panel.classList.toggle("is-active", !!isActive);
    });

    document.getElementById("filtersWorkspaceEmpty")?.classList.toggle("d-none", !!state.activeDimensionKey);

    const workspace = document.getElementById("filtersDimensionWorkspace");
    if (workspace) {
      workspace.classList.toggle("is-open", !!state.activeDimensionKey);
    }

    if (!focusSearch || !state.activeDimensionKey || !nextPanelId) return;
    window.requestAnimationFrame(() => {
      document.querySelector(`#${nextPanelId} .msx-search`)?.focus();
    });
  };

  const moveTileFocus = (currentButton, direction) => {
    const buttons = visibleTileButtons();
    if (!buttons.length) return;
    const currentIndex = Math.max(0, buttons.indexOf(currentButton));
    let nextIndex = currentIndex;
    if (direction === "home") nextIndex = 0;
    else if (direction === "end") nextIndex = buttons.length - 1;
    else nextIndex = (currentIndex + direction + buttons.length) % buttons.length;
    buttons[nextIndex]?.focus();
  };

  const updateSummary = () => {
    const filters = gatherFilters();
    state.pendingFilters = filters;
    const summary = buildSummary(filters);
    const activeCount = document.getElementById("filtersActiveCount");
    const dateSummary = document.getElementById("filtersDateSummary");
    const dateWindowLabel = document.getElementById("filtersDateWindowLabel");

    if (activeCount) activeCount.textContent = `${summary.activeCount} active`;
    if (dateSummary) dateSummary.textContent = summary.dateLabel;
    if (dateWindowLabel) dateWindowLabel.textContent = summary.dateLabel;

    DIMENSIONS.forEach((config) => {
      if (!document.getElementById(config.id)) return;
      syncFilterCard(config, filters);
    });

    renderHeaderChips(summary);
    renderAppliedChips(summary);
    renderSavedViewState(filters);
    updateQuickRangeButtons();
    updateActionState(filters);
    return filters;
  };

  const postForm = async (url, data) => {
    const response = await authFetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams(data),
    });
    if (response.redirected) {
      window.location.assign(response.url);
      return;
    }
    if (response.ok) {
      window.location.reload();
      return;
    }
    throw new Error(`Request failed (${response.status})`);
  };

  const currentFiltersFormData = () => {
    const filters = gatherFilters();
    const data = {
      start: filters.start || "",
      end: filters.end || "",
      date_preset: filters.date_preset || "",
      date_type: filters.date_type || "",
    };
    ["statuses", "regions", "methods", "customers", "suppliers", "products", "sales_reps"].forEach((key) => {
      if (filters[key] && filters[key].length) data[key] = filters[key];
    });
    return data;
  };

  const csrfToken = () =>
    rootEl()?.dataset?.csrfToken ||
    document.getElementById("filtersCsrf")?.value ||
    document.getElementById("svCsrf")?.value ||
    document.querySelector('#filtersForm input[name="csrf_token"]')?.value ||
    "";

  const buildFilterPersistParams = (filters, { reset = false } = {}) => {
    const params = new URLSearchParams();
    const token = csrfToken();
    if (token) params.set("csrf_token", token);
    if (reset) return params;
    const normalized = stableFilters(filters || gatherFilters());
    if (normalized.start) params.set("start", normalized.start);
    if (normalized.end) params.set("end", normalized.end);
    if (normalized.date_preset) params.set("date_preset", normalized.date_preset);
    if (normalized.date_type) params.set("date_type", normalized.date_type);
    ["statuses", "regions", "methods", "customers", "suppliers", "products", "sales_reps"].forEach((key) => {
      (normalized[key] || []).forEach((value) => params.append(key, value));
    });
    if (normalized.protein_min !== null && normalized.protein_min !== undefined && `${normalized.protein_min}` !== "") {
      params.set("protein_min", String(normalized.protein_min));
    }
    if (normalized.protein_max !== null && normalized.protein_max !== undefined && `${normalized.protein_max}` !== "") {
      params.set("protein_max", String(normalized.protein_max));
    }
    if (normalized.protein_name_like) params.set("protein_name_like", normalized.protein_name_like);
    if (normalized.yield_min !== null && normalized.yield_min !== undefined && `${normalized.yield_min}` !== "") {
      params.set("yield_min", String(normalized.yield_min));
    }
    if (normalized.yield_max !== null && normalized.yield_max !== undefined && `${normalized.yield_max}` !== "") {
      params.set("yield_max", String(normalized.yield_max));
    }
    if (normalized.complete_months_only !== null && normalized.complete_months_only !== undefined) {
      params.set("complete_months_only", normalized.complete_months_only ? "1" : "0");
    }
    return params;
  };

  const persistFilterState = async ({ action = "apply", filters = null } = {}) => {
    const isReset = action === "reset";
    const endpoint = isReset ? state.apiResetEndpoint : state.apiApplyEndpoint;
    const response = await authFetch(endpoint, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: buildFilterPersistParams(filters, { reset: isReset }),
    });
    if (!response.ok) {
      throw new Error(`Filter ${action} request failed (${response.status})`);
    }
    return response.json();
  };

  const loadSavedViews = () => {
    try {
      const script = document.getElementById("filtersSavedViewsData");
      state.savedViews = script?.textContent ? JSON.parse(script.textContent) : [];
    } catch (err) {
      state.savedViews = [];
    }
    const root = document.getElementById("GlobalFilters");
    state.activeSavedViewId = String(root?.dataset?.activeSavedViewId || "");
    state.selectedSavedViewId = String(document.getElementById("savedViewSelect")?.value || state.activeSavedViewId || "");
    if (!state.selectedSavedViewId && state.savedViews.length) {
      state.selectedSavedViewId = String(state.savedViews[0].id || "");
    }
  };

  const selectSavedView = (id) => {
    state.selectedSavedViewId = String(id || "");
    const input = document.getElementById("savedViewSelect");
    if (input) input.value = state.selectedSavedViewId;
    document.querySelectorAll(".saved-view-card[data-view-id]").forEach((button) => {
      button.classList.toggle("is-selected", String(button.dataset.viewId || "") === state.selectedSavedViewId);
    });
    updateSummary();
  };

  const wireSavedViews = () => {
    loadSavedViews();
    document.getElementById("saveViewForm")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const nameInput = document.getElementById("save_view_name");
      const name = String(nameInput?.value || "").trim();
      if (!name) {
        nameInput?.focus();
        return;
      }
      try {
        setLifecycle("saving_view");
        await postForm("/views/save", {
          csrf_token: document.getElementById("svCsrf")?.value || "",
          next: document.getElementById("svNext")?.value || window.location.pathname,
          name,
          ...currentFiltersFormData(),
        });
      } catch (err) {
        setLifecycle("failed_partial", "save-view");
        console.error("save view failed", err);
        updateActionState();
      }
    });
    document.querySelectorAll(".saved-view-card[data-view-id]").forEach((button) => {
      button.addEventListener("click", () => selectSavedView(button.dataset.viewId || ""));
    });

    document.getElementById("openSaveViewBtn")?.addEventListener("click", () => {
      const input = document.getElementById("save_view_name");
      document.getElementById("savedViewsSection")?.scrollIntoView({ behavior: "smooth", block: "start" });
      input?.focus();
    });

    document.getElementById("loadViewBtn")?.addEventListener("click", async () => {
      if (!state.selectedSavedViewId) return;
      try {
        setLifecycle("loading_view");
        await postForm(`/views/load/${state.selectedSavedViewId}`, {
          csrf_token: document.getElementById("svCsrf")?.value || "",
          next: document.getElementById("svNext")?.value || window.location.pathname,
        });
      } catch (err) {
        setLifecycle("failed_partial", "load-view");
        console.error("load saved view failed", err);
        updateActionState();
      }
    });

    document.getElementById("deleteViewBtn")?.addEventListener("click", async () => {
      if (!state.selectedSavedViewId) return;
      if (!window.confirm("Delete this saved view?")) return;
      try {
        setLifecycle("loading_view");
        await postForm(`/views/delete/${state.selectedSavedViewId}`, {
          csrf_token: document.getElementById("svCsrf")?.value || "",
          next: document.getElementById("svNext")?.value || window.location.pathname,
        });
      } catch (err) {
        setLifecycle("failed_partial", "delete-view");
        console.error("delete saved view failed", err);
        updateActionState();
      }
    });

    document.getElementById("updateSavedViewBtn")?.addEventListener("click", async () => {
      if (!state.selectedSavedViewId) return;
      try {
        setLifecycle("saving_view");
        await postForm(`/views/update/${state.selectedSavedViewId}`, {
          csrf_token: document.getElementById("svCsrf")?.value || "",
          next: document.getElementById("svNext")?.value || window.location.pathname,
          ...currentFiltersFormData(),
        });
      } catch (err) {
        setLifecycle("failed_partial", "update-view");
        console.error("update saved view failed", err);
        updateActionState();
      }
    });

    if (state.selectedSavedViewId) selectSavedView(state.selectedSavedViewId);
  };

  const clearDimensions = () => {
    DIMENSIONS.forEach((config) => {
      if (!document.getElementById(config.id)) return;
      setMultiValue(config.id, []);
    });
    updateSummary();
  };

  const clearFilterChip = (key) => {
    if (key === "date") {
      setPreset("current_fy");
    } else {
      const config = DIMENSIONS.find((entry) => entry.key === key);
      if (config) setMultiValue(config.id, []);
    }
    updateSummary();
  };

  const wireAppliedChips = () => {
    document.addEventListener("click", (event) => {
      const button = event.target.closest("[data-clear-filter]");
      if (!button) return;
      clearFilterChip(button.dataset.clearFilter || "");
    });
  };

  const wireFilterTiles = () => {
    visibleTileButtons().forEach((button) => {
      button.addEventListener("click", () => {
        const key = String(button.dataset.filterKey || "");
        setActiveDimension(state.activeDimensionKey === key ? "" : key, { focusSearch: state.activeDimensionKey !== key });
      });
      button.addEventListener("keydown", (event) => {
        if (event.key === "ArrowRight" || event.key === "ArrowDown") {
          event.preventDefault();
          moveTileFocus(button, 1);
        } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
          event.preventDefault();
          moveTileFocus(button, -1);
        } else if (event.key === "Home") {
          event.preventDefault();
          moveTileFocus(button, "home");
        } else if (event.key === "End") {
          event.preventDefault();
          moveTileFocus(button, "end");
        } else if (event.key === "Escape" && state.activeDimensionKey) {
          event.preventDefault();
          setActiveDimension("");
          button.focus();
        }
      });
    });
  };

  const wireShellCollapse = () => {
    const body = document.getElementById("filtersBody");
    const label = document.getElementById("filtersCollapseLabel");
    const icon = document.getElementById("filtersToggleIcon");
    if (!body) return;
    const STORAGE_KEY = "wa-filters-expanded";
    const sync = (expanded) => {
      if (label) label.textContent = expanded ? "Hide filters" : "Filters";
      if (icon) {
        icon.classList.toggle("bi-chevron-up", expanded);
        icon.classList.toggle("bi-chevron-down", !expanded);
      }
    };

    // The panel ships collapsed so the dashboard is the first thing on screen.
    // Anyone who opens it is working in it, so remember that and stop making
    // them re-open it on every page.
    let remembered = null;
    try {
      remembered = window.localStorage.getItem(STORAGE_KEY);
    } catch (err) {
      remembered = null;
    }
    if (remembered === "1" && !body.classList.contains("show")) {
      body.classList.add("show");
      const trigger = document.querySelector('[data-bs-target="#filtersBody"]');
      if (trigger) trigger.setAttribute("aria-expanded", "true");
    }

    const remember = (expanded) => {
      try {
        window.localStorage.setItem(STORAGE_KEY, expanded ? "1" : "0");
      } catch (err) {
        /* private browsing - the default is fine */
      }
    };

    sync(body.classList.contains("show"));
    body.addEventListener("show.bs.collapse", () => {
      sync(true);
      remember(true);
    });
    body.addEventListener("hide.bs.collapse", () => {
      sync(false);
      remember(false);
    });
  };

  const wirePresetControls = () => {
    document.querySelectorAll(".filter-range-pill[data-range]").forEach((button) => {
      button.addEventListener("click", () => {
        setPreset(button.dataset.range || "");
        updateSummary();
      });
    });

    document.getElementById("fStart")?.addEventListener("change", () => {
      const preset = document.getElementById("fDatePreset");
      if (preset) preset.value = "custom";
      const dateType = document.getElementById("fDateType");
      if (dateType && !dateType.value) dateType.value = "fiscal";
      updateSummary();
    });
    document.getElementById("fEnd")?.addEventListener("change", () => {
      const preset = document.getElementById("fDatePreset");
      if (preset) preset.value = "custom";
      const dateType = document.getElementById("fDateType");
      if (dateType && !dateType.value) dateType.value = "fiscal";
      updateSummary();
    });
  };

  const wireActionButtons = () => {
    document.getElementById("clearDimensionFiltersBtn")?.addEventListener("click", () => {
      clearDimensions();
    });
  };

  const submitForm = () => {
    const form = document.getElementById("filtersForm");
    if (!form) return;
    if (typeof form.requestSubmit === "function") form.requestSubmit();
    else form.submit();
  };

  const wireReset = () => {
    document.getElementById("resetFiltersBtn")?.addEventListener("click", (event) => {
      event.preventDefault();
      const form = document.getElementById("filtersForm");
      const prgMode = String(form?.dataset?.prg || "0") === "1";
      if (prgMode) {
        if (window.FilterState && typeof window.FilterState.set === "function") {
          window.FilterState.set({
            start: null,
            end: null,
            date_preset: null,
            date_type: null,
            statuses: [],
            regions: [],
            methods: [],
            customers: [],
            suppliers: [],
            products: [],
            sales_reps: [],
          });
        }
        const resetForm = document.getElementById("filtersResetForm");
        if (resetForm) {
          if (typeof resetForm.requestSubmit === "function") resetForm.requestSubmit();
          else resetForm.submit();
        }
        return;
      }

      clearDimensions();
      setPreset("current_fy");
      if (window.FilterState && typeof window.FilterState.set === "function") {
        window.FilterState.set({
          start: document.getElementById("fStart")?.value || null,
          end: document.getElementById("fEnd")?.value || null,
          date_preset: document.getElementById("fDatePreset")?.value || "current_fy",
          date_type: document.getElementById("fDateType")?.value || "fiscal",
          statuses: [],
          regions: [],
          methods: [],
          customers: [],
          suppliers: [],
          products: [],
          sales_reps: [],
        });
      }
      window.__gfResetPending = true;
      submitForm();
    });
  };

  const wireFormSubmit = () => {
    const form = document.getElementById("filtersForm");
    const prgMode = String(form?.dataset?.prg || "0") === "1";
    form?.addEventListener("submit", async (event) => {
      if (state.applyInFlight) {
        event.preventDefault();
        dlog("ignoring submit while apply is already in flight", {
          page: pageKey(),
          activeApplyId: state.activeApplyId,
        });
        updateActionState();
        return;
      }
      const filters = stableFilters(gatherFilters());
      state.pendingFilters = filters;
      if (window.FilterState && typeof window.FilterState.set === "function") {
        window.FilterState.set(filters);
      }
      setLifecycle("applying");
      state.applyInFlight = true;
      updateActionState(filters);

      if (prgMode) return;
      event.preventDefault();

      const handlerMode = ((document.body.dataset && document.body.dataset.filtersHandler) || "ssr").toLowerCase();
      const isReset = window.__gfResetPending === true;
      const banner = document.getElementById("filtersErrorBanner");
      const retryWrap = document.getElementById("filtersRetryWrap");
      const retryBtn = document.getElementById("filtersRetryBtn");
      try {
        let appliedFilters = filters;
        let responsePayload = null;
        if (handlerMode === "ajax") {
          responsePayload = await persistFilterState({ action: isReset ? "reset" : "apply", filters });
          appliedFilters = normalizeServerFilters(responsePayload?.filters || filters);
          state.scopePayload = responsePayload?.scope || state.scopePayload;
          setAppliedState(appliedFilters, { syncForm: true });
          state.scopeNotice = responsePayload?.meta?.filters_notice || state.scopeNotice;
          updateNoticeBanner(state.scopeNotice);
          updateLastAppliedLabel(responsePayload?.last_applied_at || "");
        }

        const qs = buildQueryStringForFilters(appliedFilters);
        const targetUrl = qs ? `${(form.action || window.location.pathname).split("?")[0]}?${qs}` : (form.action || window.location.pathname);
        const applyId = handlerMode === "ajax" ? nextApplyId() : "";
        if (window.history && typeof window.history.replaceState === "function") {
          window.history.replaceState({}, "", targetUrl);
        }
        if (banner) {
          banner.classList.add("d-none");
          banner.textContent = "";
        }
        retryWrap?.classList.add("d-none");
        retryBtn?.classList.add("d-none");
        window.__gfResetPending = false;

        if (handlerMode !== "ajax") {
          clearActiveApply();
          window.location.assign(targetUrl);
          return;
        }

        state.activeApplyId = applyId;
        state.activeApplyTargetUrl = targetUrl;
        state.activeApplyQs = qs;
        clearApplyAckTimer();
        state.applyAckTimer = window.setTimeout(() => {
          if (state.activeApplyId !== applyId) return;
          clearApplyAckTimer();
          state.applyInFlight = false;
          setLifecycle("failed_partial", "apply-timeout");
          const fallbackUrl = state.activeApplyTargetUrl || targetUrl;
          clearActiveApply();
          if (banner) {
            banner.textContent = "The page did not confirm the filter refresh. Reloading once to recover.";
            banner.classList.remove("d-none");
          }
          retryWrap?.classList.remove("d-none");
          retryBtn?.classList.remove("d-none");
          updateActionState();
          if (fallbackUrl) {
            window.location.assign(fallbackUrl);
          }
        }, APPLY_ACK_TIMEOUT_MS);

        dispatchGlobalFiltersApply({
          applyId,
          filters: appliedFilters,
          qs,
          meta: responsePayload?.meta || {},
          summary: responsePayload?.summary || null,
          datasetVersion: state.datasetVersion,
          scope: state.scopePayload,
        });
      } catch (err) {
        window.__gfResetPending = false;
        state.applyInFlight = false;
        clearApplyAckTimer();
        clearActiveApply();
        setLifecycle(state.readyPublished ? "failed_partial" : "failed_fatal", "apply-request");
        if (banner) {
          banner.textContent = err?.message || "Failed to apply filters.";
          banner.classList.remove("d-none");
        }
        retryWrap?.classList.remove("d-none");
        retryBtn?.classList.remove("d-none");
        updateActionState();
        console.error("filters apply failed", err);
      }
    });

    window.addEventListener("globalFilters:applied", (evt) => {
      if (!state.applyInFlight && !state.applyAckTimer) {
        dlog("ignoring unsolicited globalFilters:applied", evt?.detail || {});
        return;
      }
      const incomingApplyId = normalizeApplyId(evt?.detail?.applyId);
      if (normalizeApplyId(state.activeApplyId) && incomingApplyId !== normalizeApplyId(state.activeApplyId)) {
        dlog("ignoring stale globalFilters:applied", {
          page: pageKey(),
          activeApplyId: state.activeApplyId,
          incomingApplyId,
          detail: evt?.detail || {},
        });
        return;
      }
      clearApplyAckTimer();
      state.applyInFlight = false;
      clearActiveApply();
      const detailFilters = evt?.detail?.filters ? normalizeServerFilters(evt.detail.filters) : getAppliedFilters();
      setAppliedState(detailFilters);
      setLifecycle("ready");
      updateSummary();
      try {
        const detail = {
          applyId: incomingApplyId,
          filters: getAppliedFilters(),
          qs: state.appliedQs || buildQueryStringForFilters(getAppliedFilters()),
          datasetVersion: state.datasetVersion,
          scope: state.scopePayload,
        };
        document.dispatchEvent(new CustomEvent("globalFilters:changed", { detail }));
        window.dispatchEvent(new CustomEvent("globalFilters:changed", { detail }));
      } catch (_err) {
        /* ignore */
      }
      clearDeferredHydrationTimer();
      refreshOptionsInBackground({
        dimensions: Array.from(state.loadedDimensions),
        timeoutMs: DEFERRED_OPTIONS_TIMEOUT_MS,
        phase: "post-apply",
      });
    });
  };

  const applyRootConfig = (root) => {
    state.schemaEndpoint = root.dataset.schemaEndpoint || state.schemaEndpoint;
    state.optionsEndpoint = root.dataset.optionsEndpoint || state.optionsEndpoint;
    state.apiApplyEndpoint = root.dataset.apiApplyEndpoint || state.apiApplyEndpoint;
    state.apiResetEndpoint = root.dataset.apiResetEndpoint || state.apiResetEndpoint;
    state.datasetVersion = root.dataset.datasetVersion || state.datasetVersion;
    state.activeSavedViewId = String(root.dataset.activeSavedViewId || "");
    updateLastAppliedLabel(root.dataset.lastApplied || "");
  };

  const readInlineSchemaPayload = () => {
    const payload = parseInlineJson("filtersBootstrapData");
    return payload && typeof payload === "object" ? payload : null;
  };

  const readInlineOptionsPayload = () => {
    const payload = readInlineSchemaPayload();
    const optionsPayload = payload?.options_payload;
    return optionsPayload && typeof optionsPayload === "object" ? optionsPayload : null;
  };

  const buildLocalSchemaPayload = (root) => ({
    defaults: stableFilters(gatherFilters()),
    dataset_version: root?.dataset?.datasetVersion || state.datasetVersion || null,
    scope: state.scopePayload || {},
    meta: {
      filters_notice: state.scopeNotice || "",
      source: "dom-fallback",
    },
  });

  const applySchemaPayload = (schemaPayload, { hydrateForm = true } = {}) => {
    const payload = schemaPayload && typeof schemaPayload === "object" ? schemaPayload : {};
    state.schemaLoaded = true;
    state.schemaDefaults = payload?.defaults || state.schemaDefaults || {};
    state.datasetVersion = payload?.dataset_version || payload?.datasetVersion || state.datasetVersion;
    state.scopePayload = payload?.scope || state.scopePayload;
    state.scopeNotice = payload?.meta?.filters_notice || state.scopeNotice || "";

    const userId = (window.__FILTER_CTX__ && window.__FILTER_CTX__.user_id) || "anon";
    const scopeKey = state.scopePayload ? JSON.stringify(state.scopePayload) : undefined;

    if (window.FilterState && typeof window.FilterState.configure === "function") {
      window.FilterState.configure({
        datasetVersion: state.datasetVersion,
        userId,
        scopeKey,
        resetOnChange: true,
      });
    }
    if (window.FilterState && typeof window.FilterState.setDefaults === "function") {
      window.FilterState.setDefaults(state.schemaDefaults, { applyLocation: false });
      if (hydrateForm && typeof window.FilterState.hydrateForm === "function") {
        window.FilterState.hydrateForm(document.getElementById("filtersForm"));
      }
    }
    updateNoticeBanner(state.scopeNotice);
    return payload;
  };

  const fetchSchema = async ({ timeoutMs = SCHEMA_REQUEST_TIMEOUT_MS } = {}) => {
    const controller = new AbortController();
    let timeoutId = null;
    if (timeoutMs && Number(timeoutMs) > 0 && typeof window !== "undefined" && typeof window.setTimeout === "function") {
      timeoutId = window.setTimeout(() => {
        try {
          controller.abort();
        } catch (_err) {
          /* ignore */
        }
      }, Number(timeoutMs));
    }
    try {
      const response = await authFetch(state.schemaEndpoint, { credentials: "same-origin", signal: controller.signal });
      if (!response.ok) throw new Error(`Schema request failed (${response.status})`);
      return response.json();
    } catch (err) {
      if (err?.name === "AbortError" && timeoutMs) {
        throw new Error(`Schema request timed out (${timeoutMs}ms)`);
      }
      throw err;
    } finally {
      if (timeoutId) window.clearTimeout(timeoutId);
    }
  };

  const refreshSchemaInBackground = ({ timeoutMs = SCHEMA_REQUEST_TIMEOUT_MS } = {}) => {
    return fetchSchema({ timeoutMs })
      .then((payload) => applySchemaPayload(payload, { hydrateForm: false }))
      .catch((err) => {
        console.warn(`filters.schema.refresh.fail page=${pageKey()} err=${err?.message || err}`);
        return null;
      });
  };

  const fetchOptions = async ({ dimensions = [], timeoutMs = null, bypassCooldown = false, phase = "interactive" } = {}) => {
    const requestPhase = String(phase || "interactive");
    if (!bypassCooldown && state.optionsCooldownUntil && Date.now() < state.optionsCooldownUntil) {
      throw new Error("Filters are temporarily cooling down after repeated failures. Use Retry filters.");
    }
    const locationParams = new URLSearchParams(window.location.search || "");
    const passthrough = new URLSearchParams();
    locationParams.forEach((value, key) => {
      if (!FILTER_KEY_REGEX.test(key)) return;
      passthrough.append(key, value);
    });

    const requestedDimensions = normalizeDimensionList(dimensions);
    if (requestedDimensions.length) {
      passthrough.set("dimensions", requestedDimensions.join(","));
    }
    passthrough.set("page", pageKey());
    passthrough.set("phase", String(phase || "interactive"));

    const url = passthrough.toString() ? `${state.optionsEndpoint}?${passthrough.toString()}` : state.optionsEndpoint;
    const requestKeyParams = new URLSearchParams(passthrough);
    requestKeyParams.delete("phase");
    const requestKey = JSON.stringify({
      url: requestKeyParams.toString() ? `${state.optionsEndpoint}?${requestKeyParams.toString()}` : state.optionsEndpoint,
      etag: state.optionsEtag || "",
    });
    if (state.optionsInFlightPromise && state.optionsInFlightKey === requestKey) {
      return state.optionsInFlightPromise;
    }
    const headers = {};
    if (state.optionsEtag) headers["If-None-Match"] = state.optionsEtag;

    if (state.optionsAbort) {
      try {
        if (state.optionsAbortMeta && state.optionsAbortMeta.controller === state.optionsAbort) {
          state.optionsAbortMeta.reason = "superseded";
        }
        state.optionsAbort.abort();
      } catch (err) {
          /* ignore */
      }
    }

    state.optionsRequestId += 1;
    const requestId = state.optionsRequestId;
    const controller = new AbortController();
    const abortMeta = { controller, reason: "", phase: requestPhase };
    state.optionsAbort = controller;
    state.optionsAbortMeta = abortMeta;
    let timeoutId = null;
    if (timeoutMs && Number(timeoutMs) > 0 && typeof window !== "undefined" && typeof window.setTimeout === "function") {
      timeoutId = window.setTimeout(() => {
        try {
          abortMeta.reason = "timeout";
          controller.abort();
        } catch (err) {
          /* ignore */
        }
      }, Number(timeoutMs));
    }
    const startedAt = typeof performance !== "undefined" && performance.now ? performance.now() : Date.now();
    let requestPromise = null;
    requestPromise = (async () => {
      try {
        const response = await authFetch(url, { credentials: "same-origin", headers, signal: controller.signal });
        if (requestId !== state.optionsRequestId) return null;
        const durationMs = Math.round(((typeof performance !== "undefined" && performance.now) ? performance.now() : Date.now()) - startedAt);
        state.optionsFetchMs = durationMs;
        if (response.status === 304 && state.lastOptionsPayload) {
          return state.lastOptionsPayload;
        }
        if (!response.ok) throw new Error(`Options request failed (${response.status})`);
        state.optionsEtag = response.headers.get("ETag") || state.optionsEtag;
        const payload = await response.json();
        if (requestId !== state.optionsRequestId) return null;
        return payload;
      } catch (err) {
        if (requestId !== state.optionsRequestId) {
          return null;
        }
        if (err?.name === "AbortError") {
          if (abortMeta.reason === "superseded") {
            return null;
          }
          if (abortMeta.reason === "timeout" && timeoutMs) {
            recordOptionsFailure({ phase: requestPhase });
            throw new Error(`Options request timed out (${timeoutMs}ms)`);
          }
          return null;
        }
        recordOptionsFailure({ phase: requestPhase });
        throw err;
      } finally {
        if (timeoutId) window.clearTimeout(timeoutId);
        if (state.optionsAbort === controller) {
          state.optionsAbort = null;
        }
        if (state.optionsAbortMeta === abortMeta) {
          state.optionsAbortMeta = null;
        }
        if (state.optionsInFlightPromise === requestPromise) {
          state.optionsInFlightPromise = null;
          state.optionsInFlightKey = "";
        }
      }
    })();
    state.optionsInFlightKey = requestKey;
    state.optionsInFlightPromise = requestPromise;
    return requestPromise;
  };

  const bootstrap = async (root) => {
    const overlay = root.querySelector("#filtersLoadingOverlay") || document.getElementById("filtersLoadingOverlay");
    clearDeferredHydrationTimer();
    clearFilterError();
    overlay?.classList.remove("d-none");
    setLifecycle("bootstrapping");

    try {
      state.loadedDimensions = new Set();
      state.readyPublished = false;
      state.optionsState = "idle";
      state.schemaLoaded = false;
      const inlineSchemaPayload = readInlineSchemaPayload();
      if (inlineSchemaPayload) {
        applySchemaPayload(inlineSchemaPayload);
      } else {
        applySchemaPayload(buildLocalSchemaPayload(root));
        console.warn(`filters.schema.bootstrap.inline-missing page=${pageKey()} source=dom-fallback`);
      }

      ensureDefaultPreset();
      setAppliedState(gatherFilters());
      loadSavedViews();
      setActiveDimension("");
      state.baselineHash = filtersHash(getAppliedFilters());
      updateNoticeBanner(state.scopeNotice);
      const bootstrapDimensions = resolveBootstrapDimensions(gatherFilters());
      const inlineOptionsPayload = readInlineOptionsPayload();
      let bootstrappedFromInline = false;
      let bootstrappedFromDom = false;
      let bootstrappedFromStorage = false;
      if (inlineOptionsPayload && hasHydratableOptionsPayload(inlineOptionsPayload)) {
        applyOptionsPayload(
          {
            ...inlineOptionsPayload,
            meta: {
              ...(inlineOptionsPayload.meta || {}),
              source: inlineOptionsPayload?.meta?.source || "server-inline",
            },
          },
          { syncFilters: false, persist: true }
        );
        bootstrappedFromInline = !!state.lastOptionsPayload;
      }
      const domBootstrapDimensions = resolveDomBootstrapDimensions();
      if (!bootstrappedFromInline && domBootstrapDimensions.length) {
        bootstrappedFromDom = !!hydrateDomOptions({
          dimensions: domBootstrapDimensions,
          syncFilters: true,
          source: "dom-bootstrap",
        });
      }
      if (!bootstrappedFromInline && !bootstrappedFromDom) {
        bootstrappedFromStorage = !!hydratePersistedOptions({
          dimensions: bootstrapDimensions,
          syncFilters: false,
          source: "local-storage-bootstrap",
        });
      }
      if (!bootstrappedFromInline && !bootstrappedFromDom && !bootstrappedFromStorage) {
        try {
          await hydrateOptions({
            dimensions: bootstrapDimensions,
            timeoutMs: BOOTSTRAP_OPTIONS_TIMEOUT_MS,
            syncFilters: true,
            phase: "bootstrap",
          });
        } catch (optionsErr) {
          const persistedPayload = hydratePersistedOptions({
            dimensions: bootstrapDimensions,
            syncFilters: false,
            source: "local-storage-bootstrap-fallback",
          });
          const domFallbackPayload = persistedPayload
            ? null
            : hydrateDomOptions({
                dimensions: bootstrapDimensions,
                syncFilters: true,
                source: "dom-bootstrap-fallback",
              });
          if (persistedPayload || domFallbackPayload) {
            bootstrappedFromStorage = bootstrappedFromStorage || !!persistedPayload;
            bootstrappedFromDom = bootstrappedFromDom || !!domFallbackPayload;
            state.optionsState = "ready";
            dlog("filters bootstrap fallback", {
              page: pageKey(),
              error: optionsErr?.message || optionsErr,
              source: persistedPayload ? "local-storage" : "dom",
            });
          } else {
            state.optionsState = "failed";
            setLifecycle("failed_partial", "bootstrap-options");
            console.error(`filters.options.bootstrap.fail page=${pageKey()} err=${optionsErr?.message || optionsErr}`);
            showFilterError(optionsErr?.message || "Filters are temporarily unavailable.");
          }
        }
      }

      state.initState = "done";
      const detail = publishReady();
      if (state.lifecycle !== "failed_partial") {
        setLifecycle("ready");
      }
      if (state.lastOptionsPayload && state.optionsState === "ready") {
        console.info(`filters.init.ok page=${pageKey()} options_ms=${state.optionsFetchMs ?? "n/a"} options_etag=${state.optionsEtag ?? "none"}`);
      } else {
        console.warn(`filters.init.degraded page=${pageKey()} options_state=${state.optionsState}`);
      }

      const deferredDimensions = bootstrappedFromInline
        ? resolveInlineDeferredDimensions(inlineOptionsPayload)
        : bootstrappedFromDom || bootstrappedFromStorage
          ? normalizeDimensionList(DIMENSIONS.map((config) => config.key))
          : resolveRemainingDimensions();
      if (deferredDimensions.length) {
        state.deferredHydrationTimer = window.setTimeout(() => {
          state.deferredHydrationTimer = null;
          refreshOptionsInBackground({
            dimensions: deferredDimensions,
            timeoutMs: DEFERRED_OPTIONS_TIMEOUT_MS,
            phase: "deferred",
          });
        }, bootstrappedFromInline || bootstrappedFromDom || bootstrappedFromStorage ? 100 : 250);
      }
      return detail;
    } catch (err) {
      state.initState = "failed";
      setLifecycle("failed_fatal", "bootstrap-fatal");
      window.__FILTERS_READY = false;
      readyDeferred.reject(err);
      console.error(`filters.init.fail page=${pageKey()} err=${err?.message || err}`);
      showFilterError(err?.message || "Filters are temporarily unavailable.");
      throw err;
    } finally {
      overlay?.classList.add("d-none");
      updateActionState();
    }
  };

  const wireListeners = () => {
    if (state.listenersWired) return;
    state.listenersWired = true;

    wirePresetControls();
    wireFilterTiles();
    wireShellCollapse();
    wireActionButtons();
    wireSavedViews();
    wireAppliedChips();
    wireFormSubmit();
    wireReset();

    document.addEventListener("change", (event) => {
      if (
        ["fStart", "fEnd", "fStatuses", "fRegions", "fMethods", "fCustomers", "fSuppliers", "fProducts", "fSalesReps"].includes(
          event.target?.id
        )
      ) {
        updateSummary();
      }
    });

    document.getElementById("filtersRetryBtn")?.addEventListener("click", () => {
      state.initStartedAt = null;
      state.optionsFailureCount = 0;
      state.optionsCooldownUntil = 0;
      clearApplyAckTimer();
      clearDeferredHydrationTimer();
      if (state.optionsAbort) {
        try {
          state.optionsAbort.abort();
        } catch (_err) {
          /* ignore */
        }
      }
      const shouldResetReady = state.initState !== "done" || !state.readyPublished;
      state.initState = "idle";
      if (shouldResetReady) setReadyDeferred();
      initGlobalFilters("retry-click", true);
    });
  };

  const initGlobalFilters = (source = "manual", force = false) => {
    const now = (typeof performance !== "undefined" && performance.now) ? performance.now() : Date.now();
    if (state.initStartedAt === null) state.initStartedAt = now;
    const root = document.getElementById("GlobalFilters");
    if (!root) {
      if (now - state.initStartedAt < INIT_RETRY_MS && !state.retryTimer) {
        state.retryTimer = setTimeout(() => {
          state.retryTimer = null;
          initGlobalFilters("retry-missing-root", force);
        }, INIT_RETRY_INTERVAL);
      }
      return window.filtersReady;
    }

    if (!window.FilterState) {
      if (now - state.initStartedAt < INIT_RETRY_MS && !state.retryTimer) {
        state.retryTimer = setTimeout(() => {
          state.retryTimer = null;
          initGlobalFilters("retry-filterstate", force);
        }, INIT_RETRY_INTERVAL);
      }
      return window.filtersReady;
    }

    if (state.initState === "in-progress" && !force) return window.filtersReady;
    if (state.initState === "done" && !force) return window.filtersReady;
    if (state.initState === "failed") {
      setReadyDeferred();
      setLifecycle("idle", "retry-init");
    }

    applyRootConfig(root);
    wireListeners();
    state.initState = "in-progress";
    dlog("filters init", { source, schema: state.schemaEndpoint, options: state.optionsEndpoint });
    bootstrap(root).catch(() => {});
    return window.filtersReady;
  };

  INIT_EVENTS.forEach((eventName) => {
    window.addEventListener(eventName, () => initGlobalFilters(eventName));
  });
  CUSTOM_INIT_EVENTS.forEach((eventName) => {
    document.addEventListener(eventName, () => initGlobalFilters(eventName));
  });

  if (typeof window !== "undefined") window.initGlobalFilters = initGlobalFilters;
  initGlobalFilters("immediate");
})();
