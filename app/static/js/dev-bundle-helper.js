(() => {
  if (typeof window === "undefined") return;
  const isDebug =
    window.__APP_DEBUG__ === true ||
    window.__APP_DEBUG__ === "true" ||
    document.body.dataset.debug === "true";
  if (!isDebug) return;

  const REQUIRED_CONTAINERS = {
    overview: ["kpiGrid", "trendChart", "mixChart", "paretoChart", "topMoversBody"],
    sales: ["bundleKpiRevenue", "bundleTableBody", "bundleTrend"],
    products: ["kpiRevenue", "kpiQty", "productTbody", "priceBubbleChart"],
    regions: ["regionsChart", "regionsTable"],
    suppliers: ["suppliersTable", "trendChart"],
    salesreps: ["salesrepsRevenue", "salesrepsQty", "salesrepsMargin", "salesreps-table-body"],
    velocity: ["velocityRevenue", "velocityQty", "velocityMargin", "velocity-table-body"],
  };

  const findPageName = () => {
    const bodyPage = document.body.dataset.page || document.body.dataset.pageName;
    if (bodyPage) return bodyPage;
    const elWithPage = document.querySelector("[data-page]");
    if (elWithPage?.dataset?.page) return elWithPage.dataset.page;
    const path = window.location.pathname.split("/").filter(Boolean)[0];
    return path || "home";
  };

  const findBundleUrl = () => {
    const el =
      document.querySelector("[data-bundle-url]") ||
      document.querySelector("[data-bundle-endpoint]") ||
      document.querySelector("[data-bundle]");
    if (el) return el.dataset.bundleUrl || el.dataset.bundleEndpoint || el.dataset.bundle;
    return null;
  };

  const currentFilters = () => {
    try {
      if (window.FilterState && typeof window.FilterState.get === "function") {
        return window.FilterState.get();
      }
    } catch (_) {
      /* ignore */
    }
    const form = document.getElementById("filtersForm");
    if (!form) return {};
    const data = new FormData(form);
    const out = {};
    data.forEach((v, k) => {
      if (!out[k]) out[k] = [];
      out[k].push(v);
    });
    return out;
  };

  const missingContainers = (page) => {
    const required = REQUIRED_CONTAINERS[page] || [];
    return required.filter((id) => !document.getElementById(id));
  };

  const logSnapshot = (label, extra = {}) => {
    const page = findPageName();
    const bundleUrl = findBundleUrl();
    const missing = missingContainers(page);
    const filters = currentFilters();
    console.info("[dev-bundle-helper]", {
      label,
      page,
      bundleUrl,
      filters,
      missingContainers: missing,
      ...extra,
    });
  };

  const patchFetch = () => {
    if (window.__devFetchPatched) return;
    const originalFetch = window.fetch;
    window.fetch = async (...args) => {
      const res = await originalFetch(...args);
      try {
        const req = args[0];
        const url = typeof req === "string" ? req : req?.url || "";
        if (url && /\/api\/.+bundle/.test(url)) {
          res
            .clone()
            .json()
            .then((payload) => {
              const keys = payload ? Object.keys(payload) : [];
              const meta = payload?.meta || {};
              logSnapshot("bundle-response", {
                url,
                payloadKeys: keys,
                metaKeys: meta ? Object.keys(meta) : [],
              });
            })
            .catch(() => {
              /* ignore JSON parse errors for non-JSON responses */
            });
        }
      } catch (err) {
        console.warn("dev helper fetch hook failed", err);
      }
      return res;
    };
    window.__devFetchPatched = true;
  };

  document.addEventListener("DOMContentLoaded", () => {
    logSnapshot("page-load");
    patchFetch();
  });

  window.addEventListener("globalFilters:apply", (evt) => {
    const qs = evt?.detail?.qs;
    logSnapshot("filters-apply", { querystring: qs });
  });
})();
