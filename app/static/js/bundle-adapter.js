(() => {
  const updatePacksCoverage = (meta = {}) => {
    const panel = document.getElementById("packsCoveragePanel");
    if (!panel) return;
    const countsEl = document.getElementById("packsCoverageCounts");
    const pctEl = document.getElementById("packsCoveragePct");
    const coverage = meta.packs_coverage || meta.packsCoverage || {};
    const total = Number(coverage.total_orderlines ?? coverage.total ?? 0);
    const has = Number(coverage.has_packs_orderlines ?? coverage.has_packs ?? 0);
    const missing = Number(coverage.missing_packs_orderlines ?? coverage.missing_packs ?? 0);
    const pct = coverage.packs_coverage_pct ?? coverage.pct ?? null;
    if (!total && !missing && (pct == null || Number.isNaN(Number(pct)))) {
      panel.classList.add("d-none");
      return;
    }
    const salesrepsPage = !!document.getElementById("SalesRepsApp");
    if (salesrepsPage && pct != null && !Number.isNaN(Number(pct)) && Number(pct) >= 95) {
      panel.classList.add("d-none");
      return;
    }
    panel.classList.remove("d-none");
    const fmt = new Intl.NumberFormat();
    if (countsEl) {
      countsEl.textContent = `${fmt.format(has)} / ${fmt.format(total)} order lines have packs (${fmt.format(missing)} missing)`;
    }
    if (pctEl) {
      pctEl.textContent = pct == null || Number.isNaN(Number(pct)) ? "n/a" : `${pct}%`;
    }
  };

  const normalizeBundlePayload = (payload) => {
    const out = payload && typeof payload === "object" ? { ...payload } : {};
    out.kpis = out.kpis || out.kpi || out.metrics || {};
    out.charts = out.charts || {};
    const displayNameFor = (row) => {
      if (!row || typeof row !== "object") return null;
      const sku = row.sku || row.product_id || row.key;
      const name = row.product_name || row.name || row.label;
      if (sku && name) return `${sku}  ${name}`;
      return sku || name || null;
    };
    const ensureDisplayName = (row) => {
      if (!row || typeof row !== "object") return row;
      if (!row.display_name) {
        const dn = displayNameFor(row);
        if (dn) row.display_name = dn;
      }
      return row;
    };
    const normalizeArray = (arr) => (Array.isArray(arr) ? arr.map((r) => ensureDisplayName(r)) : arr);
    // Backfill charts.trajectory when legacy trend present
    if (!out.charts.trajectory && out.trend) {
      out.charts.trajectory = { ...(out.trend || {}) };
      if (Array.isArray(out.trend.labels)) out.charts.trajectory.labels = out.trend.labels;
      if (Array.isArray(out.trend.revenue)) out.charts.trajectory.revenue = out.trend.revenue;
      if (Array.isArray(out.trend.qty)) out.charts.trajectory.qty = out.trend.qty;
    }
    if (!out.table && Array.isArray(out.rows)) {
      out.table = { rows: out.rows };
    }
    if (out.table && !Array.isArray(out.table.rows)) {
      out.table.rows = out.table.rows || [];
    }
    if (!out.trend && out.trends) {
      out.trend = out.trends;
    }
    if (out.trend && out.trend.data && !out.trend.labels) {
      out.trend.labels = out.trend.labels || out.trend.data?.labels || [];
    }
    const meta = out.meta = out.meta && typeof out.meta === "object" ? { ...out.meta } : {};
    meta.dataset_version =
      meta.dataset_version ||
      meta.datasetVersion ||
      out.dataset_version ||
      out.datasetVersion ||
      null;
    meta.cached = Boolean(meta.cached ?? meta.cache_hit ?? meta.cacheHit ?? false);
    meta.duckdb_query_count = meta.duckdb_query_count ?? meta.duckdb_count ?? null;
    meta.sort_by = meta.sort_by || meta.sortBy || null;
    meta.sort_dir = meta.sort_dir || meta.sortDir || null;
    updatePacksCoverage(meta);

    const k = out.kpis;
    if (k && typeof k === "object") {
      if (k.orders_last_30 === undefined && k.orders_last_30d !== undefined) {
        k.orders_last_30 = k.orders_last_30d ?? 0;
      }
      if (k.orders_last_90 === undefined && k.orders_last_90d !== undefined) {
        k.orders_last_90 = k.orders_last_90d ?? 0;
      }
      if (k.days_since_last_order === undefined && k.last_order_days !== undefined) {
        k.days_since_last_order = k.last_order_days ?? null;
      }
      if (k.orders_last_30 === null) k.orders_last_30 = 0;
      if (k.orders_last_90 === null) k.orders_last_90 = 0;
    }

    const charts = out.charts = out.charts || {};
    if (!charts.price_velocity && Array.isArray(out.price_vs_velocity)) {
      charts.price_velocity = out.price_vs_velocity.map((p) => ({
        product_id: p.product_id || p.sku || p.key,
        product_name: p.product_name || p.name || p.label,
        unit_price: p.unit_price,
        orders_per_month: p.velocity_per_month ?? p.orders_per_month,
        revenue_share: p.revenue_share,
        margin_pct: p.margin_pct,
        uplift_pct: p.uplift_pct,
        segment: p.segment,
        revenue: p.revenue,
      }));
    }
    if (!out.price_vs_velocity && Array.isArray(charts.price_velocity)) {
      out.price_vs_velocity = charts.price_velocity.map((p) => ({
        sku: p.product_id || p.sku || p.key,
        name: p.product_name || p.name || p.label,
        product_id: p.product_id || p.sku || p.key,
        product_name: p.product_name || p.name || p.label,
        unit_price: p.unit_price,
        velocity_per_month: p.orders_per_month,
        orders_per_month: p.orders_per_month,
        revenue_share: p.revenue_share,
        margin_pct: p.margin_pct,
        uplift_pct: p.uplift_pct,
        segment: p.segment,
        revenue: p.revenue,
      }));
    }
    if (out.table && Array.isArray(out.table.rows)) {
      out.table.rows = normalizeArray(out.table.rows);
    }
    if (out.price_vs_velocity) {
      out.price_vs_velocity = normalizeArray(out.price_vs_velocity);
    }
    if (charts.price_velocity) {
      charts.price_velocity = normalizeArray(charts.price_velocity);
    }
    if (charts.top_products) {
      charts.top_products = normalizeArray(charts.top_products);
    }
    if (charts.movers) {
      charts.movers = normalizeArray(charts.movers);
    }
    if (charts.segments && charts.segments.movers) {
      charts.segments.movers = normalizeArray(charts.segments.movers);
    }
    if (out.performance_bubble && Array.isArray(out.performance_bubble.points)) {
      out.performance_bubble.points = normalizeArray(out.performance_bubble.points);
    }
    if (Array.isArray(out.recommendations)) {
      out.recommendations = out.recommendations.map((r) => {
        if (!r || typeof r !== "object") return r;
        if (!r.display_name) {
          const dn = displayNameFor(r);
          if (dn) r.display_name = dn;
        }
        return r;
      });
    }
    if (out.projected_next_month && Array.isArray(out.insights)) {
      const hasProjected = out.insights.some((i) => i && i.metric === "projected_next_month");
      if (!hasProjected) {
        out.insights = [...out.insights, { metric: "projected_next_month", ...out.projected_next_month }];
      }
    }
    return out;
  };

  const normalizeOptionsPayload = (payload) => {
    const source = payload && typeof payload === "object" ? payload : {};
    const rawOptions =
      source.options && typeof source.options === "object" ? source.options : source;
    const out = { ...source };
    const options = {};
    const canonicalKeys = [
      "regions",
      "methods",
      "ship_methods",
      "customers",
      "suppliers",
      "products",
      "sales_reps",
      "statuses",
    ];
    const aliases = {
      methods: ["methods", "ship_methods", "shipping_methods", "shippingMethods"],
      ship_methods: ["ship_methods", "shipping_methods", "methods"],
      sales_reps: ["sales_reps", "salesRep", "salesRepIds", "salesreps"],
      statuses: ["statuses", "status"],
    };
    let dropped = 0;

    const toOption = (item, bucketKey) => {
      if (item == null) {
        dropped += 1;
        return null;
      }
      if (typeof item === "object") {
        const idRaw =
          item.id ?? item.value ?? item.key ?? item.code ?? item.slug ?? item.name ?? item.label;
        if (idRaw == null) {
          dropped += 1;
          return null;
        }
        const id = String(idRaw).trim();
        if (!id) {
          dropped += 1;
          return null;
        }
        const labelRaw = item.label ?? item.name ?? item.title ?? idRaw;
        const label = String(labelRaw ?? id).trim();
        const bucket = String(item.bucket || bucketKey || "").trim();
        if (!bucket) {
          dropped += 1;
          return null;
        }
        return { id, label: label || id, bucket };
      }
      const id = String(item).trim();
      if (!id) {
        dropped += 1;
        return null;
      }
      const bucket = String(bucketKey || "").trim();
      if (!bucket) {
        dropped += 1;
        return null;
      }
      return { id, label: id, bucket };
    };

    canonicalKeys.forEach((key) => {
      const candidates = aliases[key] || [key];
      let list = null;
      for (const cand of candidates) {
        if (Array.isArray(rawOptions?.[cand])) {
          list = rawOptions[cand];
          break;
        }
      }
      const normalized = [];
      const dedupe = new Set();
      if (Array.isArray(list)) {
        list.forEach((item) => {
          const opt = toOption(item, key);
          if (!opt) return;
          const dedupeKey = `${opt.bucket}::${opt.id}`;
          if (dedupe.has(dedupeKey)) return;
          dedupe.add(dedupeKey);
          normalized.push(opt);
        });
      }
      options[key] = normalized;
    });

    out.options = options;
    if (dropped && (window.__filtersDebug || window.__APP_DEBUG__ === true || window.__APP_DEBUG__ === "true")) {
      console.debug("[filters] normalizeOptionsPayload dropped invalid items", { dropped });
    }
    return out;
  };

  if (typeof window !== "undefined") {
    window.normalizeBundlePayload = normalizeBundlePayload;
    window.normalizeOptionsPayload = normalizeOptionsPayload;
  }
})();
