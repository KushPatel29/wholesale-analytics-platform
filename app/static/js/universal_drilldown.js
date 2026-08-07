(function () {
  const GO_ENDPOINT = "/drilldowns/go";
  const DRILL_ATTR = "data-drilldown-payload";

  function safeParse(raw) {
    if (!raw) return null;
    try {
      return JSON.parse(raw);
    } catch (_err) {
      return null;
    }
  }

  function readGlobalFilters() {
    try {
      if (typeof window.getGlobalFilterState === "function") {
        const state = window.getGlobalFilterState() || {};
        return state.filters || {};
      }
    } catch (_err) {
      return {};
    }
    return {};
  }

  function pageMeta(el) {
    const scopedRoot =
      el?.closest?.("[data-page]") ||
      document.querySelector("#CustomerDrilldownMeta") ||
      document.querySelector("[data-page]");
    const sourcePage = scopedRoot?.dataset?.page || document.body?.dataset?.page || null;
    return {
      source_page: sourcePage,
      source_module: scopedRoot?.dataset?.page || null,
      source_entity_id: scopedRoot?.dataset?.entityId || null,
      source_entity_label: scopedRoot?.dataset?.entityLabel || null,
    };
  }

  function encodePayload(payload) {
    const json = JSON.stringify(payload || {});
    return btoa(unescape(encodeURIComponent(json)))
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/g, "");
  }

  function buildUrl(payload) {
    const encoded = encodePayload(payload);
    return `${GO_ENDPOINT}?context=${encodeURIComponent(encoded)}`;
  }

  function withDefaults(payload, el) {
    const base = payload && typeof payload === "object" ? { ...payload } : {};
    const meta = pageMeta(el);
    if (!base.source_page && meta.source_page) base.source_page = meta.source_page;
    if (!base.source_module && meta.source_module) base.source_module = meta.source_module;
    if (!base.source_entity_id && meta.source_entity_id) base.source_entity_id = meta.source_entity_id;
    if (!base.source_entity_label && meta.source_entity_label) base.source_entity_label = meta.source_entity_label;
    if (!base.active_filter_state) base.active_filter_state = readGlobalFilters();
    return base;
  }

  function navigate(payload, options = {}) {
    if (!payload || typeof payload !== "object") return;
    const url = buildUrl(payload);
    if (options.newTab) {
      window.open(url, "_blank", "noopener");
      return;
    }
    window.location.assign(url);
  }

  function payloadFromElement(el) {
    if (!el) return null;
    return safeParse(el.getAttribute(DRILL_ATTR));
  }

  function enhance(el) {
    if (!el || el.dataset.drilldownBound === "1") return;
    el.dataset.drilldownBound = "1";
    el.classList.add("is-drillable");
    if (!/^(A|BUTTON)$/.test(el.tagName)) {
      el.setAttribute("role", "button");
      if (!el.hasAttribute("tabindex")) el.setAttribute("tabindex", "0");
    }
    if (!el.getAttribute("title")) {
      el.setAttribute("title", "Click to drill into this detail");
    }
  }

  function enhanceAll() {
    document.querySelectorAll(`[${DRILL_ATTR}]`).forEach(enhance);
  }

  document.addEventListener("click", function (event) {
    const target = event.target?.closest?.(`[${DRILL_ATTR}]`);
    if (!target) return;
    const payload = withDefaults(payloadFromElement(target), target);
    if (!payload || !payload.source_page) return;
    event.preventDefault();
    navigate(payload, { newTab: event.metaKey || event.ctrlKey });
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Enter" && event.key !== " ") return;
    const target = event.target?.closest?.(`[${DRILL_ATTR}]`);
    if (!target) return;
    const payload = withDefaults(payloadFromElement(target), target);
    if (!payload || !payload.source_page) return;
    event.preventDefault();
    navigate(payload, { newTab: event.metaKey || event.ctrlKey });
  });

  document.addEventListener("DOMContentLoaded", enhanceAll);

  window.universalDrilldown = {
    buildUrl: function (payload, el) {
      return buildUrl(withDefaults(payload, el));
    },
    setPayload: function (el, payload) {
      if (!el) return;
      if (!payload) {
        el.removeAttribute(DRILL_ATTR);
        return;
      }
      el.setAttribute(DRILL_ATTR, JSON.stringify(payload));
      enhance(el);
    },
    open: function (payload, options = {}, el) {
      navigate(withDefaults(payload, el), options || {});
    },
    enhanceAll: enhanceAll,
  };
})();
