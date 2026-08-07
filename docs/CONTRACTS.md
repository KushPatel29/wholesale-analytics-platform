# Page & Drilldown Contracts (2026-01-13 refresh)

Compact reference of bundle contracts, templates, and required payload keys after the reliability pass. Keep in sync with bundle endpoints and templates.

| Page | Template | JS | Bundle endpoint | Required DOM | Required payload keys | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| overview | app/templates/overview/index.html | app/static/js/overview.js | /overview/api/bundle | kpiGrid, trendChart, mixChart, paretoChart, topMoversBody, forecastChart, healthList | meta, kpis, series, mix, pareto, top, health | Uses dedicated overview API; filters via global adapter. |
| products | app/templates/products/index.html | app/static/js/products.js | /api/products/bundle | kpiRevenue, kpiQty, kpiMargin, kpiUnique, kpiCustomers, productTbody | meta, kpis, trend, table, charts (alias of trend), warnings? | data-page="products", bundle-adapter in base. |
| customers | app/templates/customers/kpis_unified.html | (SSR) | /api/customers/bundle | KPI cards server-rendered; table body | meta, kpis, trend, table | SSR keeps filters_handler=ssr; bundle API used by tests/export. |
| regions | app/templates/regions/index.html | inline/SSR | /api/regions/bundle | KPI cards, #regionsChart, #regionsTable | meta, kpis, trend, table | Inline charts rely on bundle data when enabled. |
| suppliers | app/templates/suppliers/index.html | inline/SSR | /api/suppliers/bundle | KPI cards, trendChart, mixChart, #suppliersTable | meta, kpis, trend, table | Filters via global bar. |
| salesreps | app/templates/salesreps/index.html | app/static/js/salesreps.js | /api/salesreps/bundle | salesrepsRevenue, salesrepsQty, salesrepsMargin, #salesreps-table-body | meta, kpis, trend, table | Drilldown links point to /salesreps/rep/<id>. |
| product drilldown | app/templates/products/drilldown.html | (inline/SSR) | /api/products/drilldown/bundle | data-page="product_drilldown", header + charts/table containers | meta{entity_id,label}, kpis, trend, table, charts | One bundle per apply; accepts product_id/sku query. |
| customer drilldown | app/templates/customers/drilldown.html | (inline/SSR) | /api/customers/drilldown/bundle | data-page="customer_drilldown" | meta{entity_id,label}, kpis, trend, table, charts | Query param: customer_id. |
| supplier drilldown | app/templates/suppliers/drilldown.html | (inline/SSR) | /api/suppliers/drilldown/bundle | data-page="supplier_drilldown" | meta{entity_id,label}, kpis, trend, table, charts | Query param: supplier_id. |
| region drilldown | app/templates/regions/drilldown.html | (inline/SSR) | /api/regions/drilldown/bundle | data-page="region_drilldown" | meta{entity_id,label}, kpis, trend, table, charts | Query param: region_id/region. |
| salesrep drilldown | app/templates/salesreps/drilldown.html | app/static/js/salesrep_drilldown.js (legacy) | /api/salesreps/drilldown/bundle | data-page="salesrep_drilldown" | meta{entity_id,label}, kpis, trend, table, charts | Query param: salesrep_id/rep_id. |

Key rules:
- Filter bar must be present (base.html includes _filters.html).
- All bundle payloads: include meta.dataset_version, meta.cached/cache_hit, duckdb_query_count<=3, serialize_ms/payload_bytes when available.
- Drilldown bundle cache keys include entity id + filter hash + RBAC scope + dataset_version + pagination params.
- /api/filters/options is the only options source for selectors.
