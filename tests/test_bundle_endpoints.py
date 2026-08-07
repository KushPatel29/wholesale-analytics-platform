import pytest
from urllib.parse import quote

from app.services import fact_schema as fs
from app.services import fact_store


PAGES = [
    "products",
    "customers",
    "regions",
    "suppliers",
    "salesreps",
]

DRILLDOWNS = [
    ("products", "product_id", fs.CANON.product_id),
    ("customers", "customer_id", fs.CANON.customer_id),
    ("suppliers", "supplier_id", fs.CANON.supplier_id),
    ("regions", "region_id", fs.CANON.region),
    ("salesreps", "salesrep_id", fs.CANON.sales_rep),
]

BUNDLE_BUDGETS = {
    "customers": {"query_string": {"sections": "overview"}, "max_queries": 6, "expected_sections": ["overview"]},
}

DRILLDOWN_BUDGETS = {
    "customers": 12,
    "regions": 5,
}


def _pick_entity_id(column: str) -> str | None:
    try:
        cols = fact_store.list_columns()
        if column not in cols:
            return None
        conn = fact_store.get_conn()
        row = conn.execute(f"SELECT {column} FROM fact WHERE {column} IS NOT NULL LIMIT 1").fetchone()
        return row[0] if row else None
    except Exception:
        return None


@pytest.mark.parametrize("page", PAGES)
def test_bundle_endpoints_basic(client, page):
    budget = BUNDLE_BUDGETS.get(page, {})
    resp = client.get(f"/api/{page}/bundle", query_string=budget.get("query_string"))
    if resp.status_code == 503:
        pytest.skip("Dataset not built for bundle smoke test")
    assert resp.status_code == 200, f"{resp.status_code}: {resp.get_json()}"
    payload = resp.get_json()
    assert isinstance(payload, dict)
    for key in ("kpis", "trend", "table", "meta"):
        assert key in payload
    meta = payload.get("meta", {})
    assert "cached" in meta
    assert meta.get("dataset_version") is not None
    q_count = int(meta.get("duckdb_query_count", 0) or 0)
    assert q_count <= int(budget.get("max_queries", 3))
    expected_sections = budget.get("expected_sections")
    if expected_sections is not None:
        assert meta.get("sections") == expected_sections
    assert resp.headers.get("X-Bundle-Cached") is not None
    assert resp.headers.get("ETag") is not None
    assert meta.get("payload_bytes") is not None
    assert resp.headers.get("X-Payload-Bytes") is not None


@pytest.mark.parametrize("page", PAGES)
def test_bundle_endpoints_support_conditional_revalidation(client, page):
    budget = BUNDLE_BUDGETS.get(page, {})
    resp = client.get(f"/api/{page}/bundle", query_string=budget.get("query_string"))
    if resp.status_code == 503:
        pytest.skip("Dataset not built for bundle etag test")
    assert resp.status_code == 200, f"{resp.status_code}: {resp.get_json()}"
    etag = resp.headers.get("ETag")
    assert etag

    resp_304 = client.get(
        f"/api/{page}/bundle",
        query_string=budget.get("query_string"),
        headers={"If-None-Match": etag},
    )
    assert resp_304.status_code == 304
    assert resp_304.headers.get("ETag") == etag
    assert resp_304.get_data(as_text=True) == ""


@pytest.mark.parametrize("entity, param, col", DRILLDOWNS)
def test_drilldown_bundle_endpoints(client, entity, param, col):
    entity_id = _pick_entity_id(col)
    if not entity_id:
        pytest.skip(f"No entity id found for {entity}")
    resp = client.get(
        f"/api/{entity}/drilldown/bundle",
        query_string={param: entity_id, "start": "2000-01-01", "end": "2100-12-31"},
    )
    if resp.status_code == 503:
        pytest.skip("Dataset not built for drilldown smoke test")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert isinstance(payload, dict)
    for key in ("kpis", "trend", "table", "meta"):
        assert key in payload
    meta = payload.get("meta", {})
    assert meta.get("entity_id") == str(entity_id)
    assert meta.get("dataset_version") is not None
    q_count = int(meta.get("duckdb_query_count", 0) or 0)
    assert q_count <= DRILLDOWN_BUDGETS.get(entity, 3)
    assert resp.headers.get("X-Payload-Bytes") is not None


def _pick_product_from_bundle(client):
    resp = client.get("/api/products/bundle")
    if resp.status_code == 503:
        pytest.skip("Dataset not built for products bundle display_name test")
    assert resp.status_code == 200, f"{resp.status_code}: {resp.get_json()}"
    payload = resp.get_json()
    rows = (payload.get("table", {}) or {}).get("rows", []) or []
    if not rows:
        pytest.skip("No product rows available for display_name test")
    return rows[0]


def test_products_bundle_display_name(client):
    row = _pick_product_from_bundle(client)
    display_name = row.get("display_name") or row.get("label")
    assert display_name
    assert str(display_name).lower() != "none"
    sku = row.get("sku") or row.get("product_id")
    name = row.get("product_name")
    if sku and name:
        assert str(sku) in str(display_name)
        assert str(name) in str(display_name)


def test_products_drilldown_route_no_redirect(client):
    row = _pick_product_from_bundle(client)
    product_id = row.get("product_id") or row.get("sku")
    if not product_id:
        pytest.skip("No product id available for drilldown route test")
    pid_path = quote(str(product_id), safe="")
    resp = client.get(f"/products/{pid_path}/drilldown", follow_redirects=False)
    if resp.status_code == 503:
        pytest.skip("Dataset not built for drilldown route test")
    assert resp.status_code not in (301, 302, 307, 308)


def test_products_drilldown_bundle_sections(client):
    row = _pick_product_from_bundle(client)
    product_id = row.get("product_id") or row.get("sku")
    if not product_id:
        pytest.skip("No product id available for drilldown bundle section test")
    resp = client.get("/api/products/drilldown/bundle", query_string={"product_id": product_id})
    if resp.status_code == 503:
        pytest.skip("Dataset not built for products drilldown bundle section test")
    assert resp.status_code == 200
    payload = resp.get_json()
    for key in ("classification", "lifecycle", "forecast", "bought_together"):
        assert key in payload
    bt = payload.get("bought_together") or {}
    assert "rows" in bt


def _suppliers_payload(client):
    resp = client.get("/api/suppliers/bundle")
    if resp.status_code == 503:
        pytest.skip("Dataset not built for suppliers bundle sanity test")
    assert resp.status_code == 200, f"{resp.status_code}: {resp.get_json()}"
    payload = resp.get_json()
    assert isinstance(payload, dict)
    return payload


def test_suppliers_bundle_sanity(client):
    payload = _suppliers_payload(client)
    kpis = payload.get("kpis", {}) or {}
    assert float(kpis.get("total_revenue") or 0.0) >= 0.0

    rows = (payload.get("table", {}) or {}).get("rows", []) or []
    if not rows:
        pytest.skip("No supplier rows available for sanity checks")

    reasonable_margins = []
    revenue_rows = []
    for row in rows:
        revenue = float(row.get("revenue") or row.get("Revenue") or 0.0)
        cost = row.get("cost")
        if cost is None:
            cost = row.get("Cost")
        margin = row.get("margin_pct")
        if margin is None:
            margin = row.get("MarginPct")
        units = float(row.get("units") or row.get("Units") or 0.0)

        if revenue > 0:
            revenue_rows.append(units)
        if revenue > 0 and cost is not None and float(cost) >= 0 and margin is not None:
            reasonable_margins.append(float(margin))

    if reasonable_margins:
        assert all(-200.0 <= m <= 200.0 for m in reasonable_margins), reasonable_margins[:5]
    if revenue_rows:
        assert any(u > 0 for u in revenue_rows), "Units are zero for all revenue-bearing suppliers"


def test_suppliers_drilldown_bundle_sanity(client):
    payload = _suppliers_payload(client)
    rows = (payload.get("table", {}) or {}).get("rows", []) or []
    if not rows:
        pytest.skip("No suppliers available for drilldown sanity test")
    supplier_id = rows[0].get("supplier_id") or rows[0].get("SupplierId") or rows[0].get("key")
    if not supplier_id:
        pytest.skip("No supplier_id found in bundle response")

    resp = client.get("/api/suppliers/drilldown/bundle", query_string={"supplier_id": supplier_id})
    if resp.status_code == 503:
        pytest.skip("Dataset not built for suppliers drilldown sanity test")
    assert resp.status_code == 200, f"{resp.status_code}: {resp.get_json()}"
    drilldown = resp.get_json()
    assert isinstance(drilldown, dict)
    for key in ("kpis", "trend", "charts", "table", "meta"):
        assert key in drilldown
    meta = drilldown.get("meta", {}) or {}
    assert str(meta.get("entity_id")) == str(supplier_id)
    q_count = int(meta.get("duckdb_query_count", 0) or 0)
    assert q_count <= 3
