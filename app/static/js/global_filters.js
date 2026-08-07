/* global_filters.js
 * Single source of truth for global filters across pages.
 * - Persists state to localStorage
 * - Provides helpers to hydrate/read the filters form
 * - Dispatches a single `globalFilters:apply` event with canonical payload
 */

(function () {
  const BASE_STORAGE_KEY = "wholesale.globalFilters.v2";
  let namespace = "default";
  const state = {
    defaults: {},
    filters: {},
  };

  const storageKey = () => `${BASE_STORAGE_KEY}::${namespace}`;
  const nextApplyId = () =>
    `filterstate-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;

  const setNamespace = (ns, { reset = false } = {}) => {
    const next = ns && String(ns).trim() ? String(ns).trim() : "default";
    if (next === namespace) return;
    namespace = next;
    if (reset) {
      state.filters = {};
      try {
        localStorage.removeItem(storageKey());
      } catch (err) { /* ignore */ }
    } else {
      hydrateFromStorage();
    }
  };

  const readStoragePayload = (key) => {
    try {
      const raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : null;
    } catch (err) { // eslint-disable-line no-unused-vars
      return null;
    }
  };

  const readStorage = () => {
    const payload = readStoragePayload(storageKey());
    if (payload && (payload.namespace === namespace || !payload.namespace)) {
      return payload.filters || payload;
    }
    // Fallback to legacy key if no namespaced payload
    const legacy = readStoragePayload(BASE_STORAGE_KEY);
    if (legacy && (!legacy.namespace || legacy.namespace === namespace)) {
      return legacy.filters || legacy;
    }
    return null;
  };

  const writeStorage = (filters) => {
    try {
      localStorage.setItem(storageKey(), JSON.stringify({ namespace, filters: filters || {} }));
    } catch (err) { // eslint-disable-line no-unused-vars
      /* ignore */
    }
  };

  const extractToken = (val) => {
    if (val === undefined || val === null) return null;
    if (typeof val === "object") {
      const raw = val.id ?? val.value ?? val.key ?? val.code ?? val.slug ?? val.label ?? val.name;
      if (raw === undefined || raw === null) return null;
      const text = String(raw).trim();
      return text === "" ? null : text;
    }
    const text = String(val).trim();
    return text === "" ? null : text;
  };

  const coerceList = (val) => {
    if (val === undefined || val === null) return [];
    const arr = Array.isArray(val) ? val : [val];
    const out = [];
    const seen = new Set();
    arr.forEach((item) => {
      const token = extractToken(item);
      if (token === null) return;
      if (token.toLowerCase() === "all") return;
      if (seen.has(token)) return;
      seen.add(token);
      out.push(token);
    });
    return out;
  };

  const coerceScalar = (val) => {
    if (val === undefined || val === null) return null;
    const arr = Array.isArray(val) ? val : [val];
    for (const item of arr) {
      if (item === undefined || item === null) continue;
      const text = String(item).trim();
      if (text !== "") return text;
    }
    return null;
  };

  const coerceBool = (val, fallback = null) => {
    if (val === undefined || val === null || val === "") return fallback;
    if (typeof val === "boolean") return val;
    const text = String(Array.isArray(val) ? val[0] : val).trim().toLowerCase();
    if (["1", "true", "yes", "on"].includes(text)) return true;
    if (["0", "false", "no", "off"].includes(text)) return false;
    return fallback;
  };

  const normalize = (raw) => {
    const obj = raw || {};
    return {
      start: obj.start || obj.start_date || obj.date_start || null,
      end: obj.end || obj.end_date || obj.date_end || null,
      date_preset: obj.date_preset || obj.preset || obj.range_preset || null,
      date_type: obj.date_type || obj.dateType || null,
      statuses: coerceList(obj.statuses),
      regions: coerceList(obj.regions || obj.region_ids),
      methods: coerceList(obj.methods || obj.shipping_methods || obj.ship_method_ids),
      customers: coerceList(obj.customers || obj.customer_ids),
      suppliers: coerceList(obj.suppliers || obj.supplier_ids),
      products: coerceList(obj.products || obj.product_ids),
      sales_reps: coerceList(obj.sales_reps || obj.sales_rep_ids),
      protein_groups: coerceList(obj.protein_groups || obj.protein_group || obj.meat_type || obj.species),
      yield_min: coerceScalar(obj.yield_min),
      yield_max: coerceScalar(obj.yield_max),
      protein_min: coerceScalar(obj.protein_min),
      protein_max: coerceScalar(obj.protein_max),
      protein_name_like: coerceScalar(obj.protein_name_like || obj.protein_name || obj.protein),
      complete_months_only: coerceBool(obj.complete_months_only ?? obj.completeMonthsOnly ?? obj.full_months_only, null),
    };
  };

  const sanitize = (raw, optionsMap) => {
    const base = normalize(raw);
    if (optionsMap && typeof optionsMap === "object") {
      Object.keys(optionsMap).forEach((key) => {
        if (!(key in base)) return;
        const allowed = optionsMap[key];
        if (!allowed || typeof allowed.has !== "function") return;
        base[key] = (base[key] || []).filter((val) => allowed.has(String(val)));
      });
    }
    return base;
  };

  const hydrateFromStorage = () => {
    const stored = readStorage();
    if (stored) state.filters = sanitize(stored);
  };

  const hydrateFromLocation = () => {
    const qs = new URLSearchParams(window.location.search || "");
    const keys = [
      "start", "start_date", "date_start",
      "end", "end_date", "date_end",
      "date_preset", "preset", "range_preset",
      "date_type", "dateType",
      "regions", "region_ids",
      "methods", "shipping_methods", "ship_method_ids",
      "customers", "customer_ids",
      "suppliers", "supplier_ids",
      "products", "product_ids",
      "sales_reps", "sales_rep_ids",
      "protein_groups", "protein_group", "meat_type", "species",
      "yield_min", "yield_max",
      "statuses",
      "protein_min", "protein_max", "protein_name", "protein_name_like",
      "complete_months_only", "completeMonthsOnly", "full_months_only",
    ];
    const hasFilters = keys.some((k) => qs.has(k));
    if (!hasFilters) return;
    const payload = {};
    keys.forEach((k) => {
      const vals = qs.getAll(k);
      if (!vals || vals.length === 0) return;
      payload[k] = vals.length === 1 ? vals[0] : vals;
    });
    state.filters = sanitize({ ...state.filters, ...payload });
  };

  const get = () => ({
    ...state.defaults,
    ...state.filters,
  });

  const setDefaults = (defaults, { applyLocation = true } = {}) => {
    state.defaults = sanitize(defaults);
    // If no explicit filters saved, seed with defaults
    if (!state.filters || Object.keys(state.filters || {}).length === 0) {
      state.filters = sanitize(defaults);
    }
    if (applyLocation) hydrateFromLocation();
  };

  const set = (next, { persist = true } = {}) => {
    state.filters = sanitize(next);
    if (persist) writeStorage(state.filters);
  };

  const configure = ({ datasetVersion, userId, scopeKey, resetOnChange = true } = {}) => {
    const parts = [];
    if (datasetVersion) parts.push(String(datasetVersion));
    if (userId) parts.push(String(userId));
    if (scopeKey) parts.push(String(scopeKey));
    const ns = parts.length ? parts.join("::") : "default";
    setNamespace(ns, { reset: resetOnChange });
  };

  const toQueryString = (filters) => {
    const params = new URLSearchParams();
    const payload = normalize(filters || get());
    if (payload.start) params.set("start", payload.start);
    if (payload.end) params.set("end", payload.end);
    if (payload.date_preset) params.set("date_preset", payload.date_preset);
    if (payload.date_type) params.set("date_type", payload.date_type);
    const appendAll = (key, values) => {
      (values || []).forEach((v) => params.append(key, v));
    };
    appendAll("statuses", payload.statuses);
    appendAll("regions", payload.regions);
    appendAll("methods", payload.methods);
    appendAll("customers", payload.customers);
    appendAll("suppliers", payload.suppliers);
    appendAll("products", payload.products);
    appendAll("sales_reps", payload.sales_reps);
    appendAll("protein_groups", payload.protein_groups);
    if (payload.yield_min !== null) params.set("yield_min", payload.yield_min);
    if (payload.yield_max !== null) params.set("yield_max", payload.yield_max);
    if (payload.protein_min !== null) params.set("protein_min", payload.protein_min);
    if (payload.protein_max !== null) params.set("protein_max", payload.protein_max);
    if (payload.protein_name_like) params.set("protein_name_like", payload.protein_name_like);
    if (payload.complete_months_only !== null) params.set("complete_months_only", payload.complete_months_only ? "1" : "0");
    params.set("_gf", "1");

    // Preserve any non-filter params already in the URL
    const current = new URLSearchParams(window.location.search || "");
    current.forEach((value, key) => {
      if (params.has(key)) return;
      if (/^(_gf)$/.test(key)) return;
      if (/^(start|end|date_preset|date_type|statuses|regions|region_ids|methods|shipping_methods|ship_method_ids|customers|customer_ids|suppliers|supplier_ids|products|product_ids|sales_reps|sales_rep_ids|protein_groups|protein_group|meat_type|species|yield_min|yield_max|protein_min|protein_max|protein_name|protein_name_like|complete_months_only|completeMonthsOnly|full_months_only)/.test(key)) return;
      params.append(key, value);
    });
    return params.toString();
  };

  const fromForm = (form) => {
    if (!form) return get();
    const data = new FormData(form);
    const toArray = (name) => data.getAll(name).filter((v) => v !== undefined && v !== null && String(v).trim() !== "");
    const current = get();
    const readScalar = (...names) => {
      for (const name of names) {
        const field = form.elements?.namedItem?.(name) || document.querySelector(`[name="${name}"]`);
        if (!field) continue;
        if (field instanceof RadioNodeList) {
          const choice = field.value;
          if (choice !== undefined && choice !== null && String(choice).trim() !== "") return choice;
          continue;
        }
        if (field.type === "checkbox") return field.checked ? "1" : "0";
        const value = data.get(name);
        if (value !== undefined && value !== null && String(value).trim() !== "") return value;
      }
      return null;
    };
    const methods = toArray("methods");
    return normalize({
      ...current,
      start: data.get("start") || data.get("start_date") || data.get("date_start"),
      end: data.get("end") || data.get("end_date") || data.get("date_end"),
      date_preset: data.get("date_preset") || data.get("preset"),
      date_type: data.get("date_type") || data.get("dateType"),
      statuses: toArray("statuses"),
      regions: toArray("regions"),
      methods: methods.length ? methods : toArray("shipping_methods"),
      customers: toArray("customers"),
      suppliers: toArray("suppliers"),
      products: toArray("products"),
      sales_reps: toArray("sales_reps"),
      protein_groups: toArray("protein_groups"),
      yield_min: readScalar("yield_min"),
      yield_max: readScalar("yield_max"),
      protein_min: readScalar("protein_min"),
      protein_max: readScalar("protein_max"),
      protein_name_like: readScalar("protein_name_like", "protein_name", "protein"),
      complete_months_only: readScalar("complete_months_only", "completeMonthsOnly", "full_months_only"),
    });
  };

  const hydrateForm = (form) => {
    if (!form) return;
    const filters = get();
    const setValue = (selector, value) => {
      const el = form.querySelector(selector);
      if (!el) return;
      if (el.multiple) {
        Array.from(el.options).forEach((opt) => {
          opt.selected = (filters[el.name] || []).includes(String(opt.value));
        });
      } else {
        el.value = value || "";
      }
      try {
        el.dispatchEvent(new Event("change", { bubbles: true }));
      } catch (err) {
        /* ignore */
      }
    };
    setValue("#fStart", filters.start);
    setValue("#fEnd", filters.end);
    setValue("#fDatePreset", filters.date_preset);
    setValue("#fDateType", filters.date_type);
    const setMulti = (id, values) => {
      const el = form.querySelector(id);
      if (!el) return;
      if (el._msx) {
        el._msx.setValue(values || []);
      } else if (el.tomselect && Array.isArray(values)) {
        el.tomselect.setValue(values);
      } else {
        Array.from(el.options).forEach((opt) => {
          opt.selected = (values || []).includes(String(opt.value));
        });
      }
    };
    setMulti("#fStatuses", filters.statuses);
    setMulti("#fRegions", filters.regions);
    setMulti("#fMethods", filters.methods);
    setMulti("#fCustomers", filters.customers);
    setMulti("#fSuppliers", filters.suppliers);
    setMulti("#fProducts", filters.products);
    setMulti("#fSalesReps", filters.sales_reps);
    setMulti("#fProteinGroups", filters.protein_groups);
    const setNamedValue = (names, value) => {
      (names || []).forEach((name) => {
        const el = form.querySelector(`[name="${name}"]`) || document.querySelector(`[name="${name}"]`);
        if (!el) return;
        if (el.type === "checkbox") {
          el.checked = Boolean(value);
        } else {
          el.value = value ?? "";
        }
      });
    };
    setNamedValue(["yield_min"], filters.yield_min);
    setNamedValue(["yield_max"], filters.yield_max);
    setNamedValue(["protein_min"], filters.protein_min);
    setNamedValue(["protein_max"], filters.protein_max);
    setNamedValue(["protein_name_like", "protein_name"], filters.protein_name_like);
    setNamedValue(["complete_months_only", "completeMonthsOnly", "full_months_only"], filters.complete_months_only);
  };

  const apply = (filters, meta = {}) => {
    if (filters) set(filters);
    const payload = get();
    writeStorage(payload);
    const qs = toQueryString(payload);
    const detail = {
      filters: payload,
      qs,
      meta,
      applyId: meta?.applyId || nextApplyId(),
    };
    if (typeof window.dispatchGlobalFiltersApply === "function") {
      window.dispatchGlobalFiltersApply(detail);
      return detail;
    }
    try {
      document.dispatchEvent(new CustomEvent("globalFilters:apply", { detail }));
    } catch (err) { /* ignore */ }
    try {
      window.dispatchEvent(new CustomEvent("globalFilters:apply", { detail }));
    } catch (err) { /* ignore */ }
    return detail;
  };

  hydrateFromStorage();

  window.FilterState = {
    get,
    set,
    setDefaults,
    setNamespace,
    configure,
    fromForm,
    hydrateForm,
    apply,
    toQueryString,
    storageKey: storageKey,
    sanitize,
  };

  // Lightweight helpers exposed for consumers that don't want to depend on FilterState directly.
  window.getFilterState = () => get();
  window.setFilterState = (state, opts = {}) => {
    const silent = opts.silent === true;
    set(state);
    if (!silent && typeof apply === "function") {
      return apply(state, { source: opts.source || "setFilterState" });
    }
    return get();
  };
})();
