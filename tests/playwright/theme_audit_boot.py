from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import watermark_store
from etl import partition_writer
from tests.labor_helpers import build_labor_dataset


OUTPUT_DIR = ROOT / ".playwright" / "theme-audit"
SNAPSHOT_PATH = OUTPUT_DIR / "theme_audit_sales.parquet"
FACT_DATASET_PATH = OUTPUT_DIR / "fact_dataset"
LABOR_DATASET_PATH = OUTPUT_DIR / "labor" / "fact_dataset"


def _sales_rows() -> list[dict[str, object]]:
    customers = [
        {"id": "C_MAIN", "name": "North Shore Bistro", "rep_id": "R1", "rep_name": "Alex North", "region": "Region-01"},
        {"id": "C_WEST", "name": "West Market Hall", "rep_id": "R1", "rep_name": "Alex North", "region": "Region-01"},
        {"id": "C_CHAIN", "name": "Harbor Foods Group", "rep_id": "R1", "rep_name": "Alex North", "region": "Region-02"},
        {"id": "C_GOLD", "name": "Gold Leaf Hotels", "rep_id": "R2", "rep_name": "Bea Stone", "region": "Region-02"},
        {"id": "C_EAST", "name": "Eastline Deli", "rep_id": "R2", "rep_name": "Bea Stone", "region": "Region-03"},
        {"id": "C_SOUTH", "name": "South Ridge Grocer", "rep_id": "R3", "rep_name": "Chris Vale", "region": "Region-04"},
    ]
    products = {
        "SKU-001": {
            "name": "Prime Ribeye",
            "supplier_id": "SUP_A",
            "supplier_name": "Summit Farms",
            "protein": "Beef",
            "category": "Steak",
            "base_price": 156.0,
            "base_qty": 6.0,
            "weight_per_unit": 2.45,
            "cost_ratio": 0.66,
        },
        "SKU-002": {
            "name": "Striploin Reserve",
            "supplier_id": "SUP_A",
            "supplier_name": "Summit Farms",
            "protein": "Beef",
            "category": "Steak",
            "base_price": 132.0,
            "base_qty": 8.0,
            "weight_per_unit": 2.1,
            "cost_ratio": 0.67,
        },
        "SKU-003": {
            "name": "Bacon Slab",
            "supplier_id": "SUP_B",
            "supplier_name": "Prairie Smoke",
            "protein": "Pork",
            "category": "Bacon",
            "base_price": 94.0,
            "base_qty": 10.0,
            "weight_per_unit": 1.8,
            "cost_ratio": 0.64,
        },
        "SKU-004": {
            "name": "Sausage Coil",
            "supplier_id": "SUP_B",
            "supplier_name": "Prairie Smoke",
            "protein": "Pork",
            "category": "Sausage",
            "base_price": 87.0,
            "base_qty": 9.0,
            "weight_per_unit": 1.55,
            "cost_ratio": 0.63,
        },
        "SKU-005": {
            "name": "Chicken Breast",
            "supplier_id": "SUP_C",
            "supplier_name": "Coastal Poultry",
            "protein": "Chicken",
            "category": "Poultry",
            "base_price": 74.0,
            "base_qty": 12.0,
            "weight_per_unit": 1.32,
            "cost_ratio": 0.62,
        },
        "SKU-006": {
            "name": "Turkey Roast",
            "supplier_id": "SUP_D",
            "supplier_name": "Harvest Valley",
            "protein": "Turkey",
            "category": "Roast",
            "base_price": 111.0,
            "base_qty": 7.0,
            "weight_per_unit": 2.75,
            "cost_ratio": 0.68,
        },
    }
    purchase_plan = {
        "C_MAIN": ["SKU-001", "SKU-002", "SKU-003", "SKU-005"],
        "C_WEST": ["SKU-001", "SKU-005"],
        "C_CHAIN": ["SKU-001", "SKU-003", "SKU-006"],
        "C_GOLD": ["SKU-003", "SKU-004", "SKU-006"],
        "C_EAST": ["SKU-005", "SKU-006"],
        "C_SOUTH": ["SKU-002", "SKU-004"],
    }
    ship_methods = ["Delivery", "Ground", "Pickup", "Courier"]
    month_starts = pd.date_range("2025-01-01", "2026-03-01", freq="MS")

    rows: list[dict[str, object]] = []
    order_seq = 1
    line_seq = 1
    for month_idx, month_start in enumerate(month_starts):
        for customer_idx, customer in enumerate(customers):
            skus = purchase_plan[customer["id"]]
            for sku_idx, sku in enumerate(skus):
                product = products[sku]
                day = 5 + ((month_idx * 3 + customer_idx * 2 + sku_idx) % 20)
                ship_date = month_start + pd.Timedelta(days=day)
                qty = product["base_qty"] + float((month_idx + customer_idx + sku_idx) % 4)
                price_per_unit = round(
                    product["base_price"] * (1 + (month_idx * 0.012)) * (1 + (customer_idx * 0.008)),
                    2,
                )
                cost_per_unit = round(price_per_unit * product["cost_ratio"], 2)
                revenue = round(qty * price_per_unit, 2)
                cost = round(qty * cost_per_unit, 2)
                weight_lb = round(qty * product["weight_per_unit"], 2)
                order_id = f"TA-{order_seq:05d}"
                line_id = f"TA-LINE-{line_seq:06d}"
                ship_method = ship_methods[(month_idx + customer_idx + sku_idx) % len(ship_methods)]
                timestamp = ship_date.strftime("%Y-%m-%d")
                base_row = {
                    "Date": timestamp,
                    "DateExpected": timestamp,
                    "ShipDate": timestamp,
                    "EffectiveDate": timestamp,
                    "UpdatedAt": f"{timestamp}T12:00:00Z",
                    "OrderId": order_id,
                    "OrderID": order_id,
                    "OrderLineId": line_id,
                    "CustomerId": customer["id"],
                    "CustomerID": customer["id"],
                    "CustomerName": customer["name"],
                    "ProductId": sku,
                    "ProductID": sku,
                    "SKU": sku,
                    "ProductName": product["name"],
                    "SupplierId": product["supplier_id"],
                    "SupplierName": product["supplier_name"],
                    "SalesRepId": customer["rep_id"],
                    "SalesRepName": customer["rep_name"],
                    "PrimarySalesRepId": customer["rep_id"],
                    "RegionName": customer["region"],
                    "Region": customer["region"],
                    "ShippingMethodName": ship_method,
                    "OrderStatus": "packed",
                    "Protein": product["protein"],
                    "ProteinName": product["protein"],
                    "ProteinFamily": product["protein"],
                    "protein_family": product["protein"],
                    "ProteinType": None,
                    "Category": product["category"],
                    "ProductCategory": product["category"],
                    "product_category": product["category"],
                    "Revenue": revenue,
                    "revenue_ordered": revenue,
                    "Cost": cost,
                    "cost_ordered": cost,
                    "Profit": round(revenue - cost, 2),
                    "Price": price_per_unit,
                    "CostPrice": cost_per_unit,
                    "QuantityShipped": qty,
                    "QuantityOrdered": qty,
                    "ShippedItems": qty,
                    "WeightLb": weight_lb,
                    "pack_item_count_sum": qty,
                    "pack_weight_lb_sum": weight_lb,
                    "pack_count": 1,
                    "UnitOfBillingId": 1,
                }
                rows.append(base_row)
                order_seq += 1
                line_seq += 1

    # Add a few sparse rows so empty/sparse sections can render without breaking discovery.
    rows.append(
        {
            "Date": "2025-03-07",
            "DateExpected": "2025-03-07",
            "ShipDate": "2025-03-07",
            "EffectiveDate": "2025-03-07",
            "UpdatedAt": "2025-03-07T12:00:00Z",
            "OrderId": "TA-SPARSE-001",
            "OrderID": "TA-SPARSE-001",
            "OrderLineId": "TA-LINE-SPARSE-001",
            "CustomerId": "C_SPARSE",
            "CustomerID": "C_SPARSE",
            "CustomerName": "Sparse Customer",
            "ProductId": "SKU-007",
            "ProductID": "SKU-007",
            "SKU": "SKU-007",
            "ProductName": "Trim Pack",
            "SupplierId": "SUP_E",
            "SupplierName": "Metro Cuts",
            "SalesRepId": "R1",
            "SalesRepName": "Alex North",
            "PrimarySalesRepId": "R1",
            "RegionName": "Region-01",
            "Region": "Region-01",
            "ShippingMethodName": "Ground",
            "OrderStatus": "packed",
            "Protein": "Beef",
            "ProteinName": "Beef",
            "ProteinFamily": "Beef",
            "protein_family": "Beef",
            "ProteinType": None,
            "Category": "Trim",
            "ProductCategory": "Trim",
            "product_category": "Trim",
            "Revenue": 75.0,
            "revenue_ordered": 75.0,
            "Cost": 49.5,
            "cost_ordered": 49.5,
            "Profit": 25.5,
            "Price": 75.0,
            "CostPrice": 49.5,
            "QuantityShipped": 1.0,
            "QuantityOrdered": 1.0,
            "ShippedItems": 1.0,
            "WeightLb": 0.0,
            "pack_item_count_sum": 1.0,
            "pack_weight_lb_sum": 0.0,
            "pack_count": 1,
            "UnitOfBillingId": 1,
        }
    )
    return rows


def _build_sales_dataset() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if FACT_DATASET_PATH.exists():
        shutil.rmtree(FACT_DATASET_PATH, ignore_errors=True)

    frame = pd.DataFrame(_sales_rows())
    frame.to_parquet(SNAPSHOT_PATH, index=False)

    min_date = pd.to_datetime(frame["Date"], errors="coerce").min()
    max_date = pd.to_datetime(frame["Date"], errors="coerce").max()
    manifest = {
        "dataset_type": "sales",
        "source_system": "playwright-theme-audit",
        "dataset_version": "playwright-theme-audit-v1",
        "last_refresh_utc": "2026-04-09T00:00:00Z",
        "built_at_utc": "2026-04-09T00:00:00Z",
        "date_column": "Date",
    }
    partition_writer.upsert_dataset(
        frame,
        dataset_path=FACT_DATASET_PATH,
        pk_col="OrderLineId",
        date_col="Date",
        existing_manifest=watermark_store.read_manifest(FACT_DATASET_PATH),
        manifest_updates=manifest,
        replace_window_start=min_date,
        replace_window_end=max_date,
        keep_prev=False,
    )


def _build_labor_seed() -> None:
    if LABOR_DATASET_PATH.exists():
        shutil.rmtree(LABOR_DATASET_PATH, ignore_errors=True)
    LABOR_DATASET_PATH.parent.mkdir(parents=True, exist_ok=True)
    build_labor_dataset(LABOR_DATASET_PATH)


def _server_env(host: str, port: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PYTHONUNBUFFERED": "1",
            "FLASK_ENV": "development",
            "USE_RELOADER": "0",
            "RUN_TESTS_ON_BOOT": "0",
            "RUN_SMOKE_ON_BOOT": "0",
            "LOGIN_DISABLED": "1",
            "AUTHZ_DISABLED": "1",
            "DEV_INSECURE_COOKIES": "1",
            "RATELIMIT_ENABLED": "0",
            "NOTIFICATIONS_ENABLED": "0",
            "AI_ENABLED": "0",
            "RETURNS_ENABLED": "0",
            "ENABLE_SSE": "0",
            "AUTO_REFRESH": "0",
            "ENABLE_INPROCESS_REFRESH": "0",
            "SUPPLIERS_V2": "1",
            "SUPPLIER_DRILLDOWN_V2": "1",
            "SALESREPS_V2": "1",
            "SALESREP_DRILLDOWN_V2": "1",
            "CUSTOMERS_KPIS_V3": "1",
            "CUSTOMERS_RFM_V2": "1",
            "CUSTOMERS_CLV_V2": "1",
            "CUSTOMER_DRILLDOWN_V2": "1",
            "COHORTS_V2": "1",
            "PRODUCTS_V4": "1",
            "PRODUCT_DRILLDOWN_V2": "1",
            "REGIONS_V2": "1",
            "REGION_OVERVIEW_V2": "1",
            "REGION_DRILLDOWN_V2": "1",
            "OVERVIEW_V3": "1",
            "OVERVIEW_FORECAST_V2": "1",
            "LABOR_ANALYTICS_ENABLED": "1",
            "RETURNS_ENABLED": "1",
            "RETURNS_V2": "1",
            "RETURNS_ANALYTICS": "1",
            "RETURNS_FINAL_V1": "1",
            "RETURNS_AUTOFILL_ORDER": "1",
            "MAIL_SUPPRESS_SEND": "1",
            "LOGIN_DISABLED": "1",
            "AUTHZ_DISABLED": "1",
            "WTF_CSRF_ENABLED": "0",
            "SECRET_KEY": "enterprise-test-secret",
            "PARQUET_PATH": SNAPSHOT_PATH.as_posix(),
            "PRODUCTS_PARQUET_PATH": SNAPSHOT_PATH.as_posix(),
            "PRODUCTS_SALES_PARQUET": SNAPSHOT_PATH.as_posix(),
            "FACT_DATASET_PATH": FACT_DATASET_PATH.as_posix(),
            "LABOR_PARQUET_PATH": LABOR_DATASET_PATH.as_posix(),
            "PLAYWRIGHT_THEME_AUDIT_SEED": "1",
            "PLAYWRIGHT_HOST": host,
            "PLAYWRIGHT_PORT": port,
        }
    )
    return env


def main() -> None:
    host = os.getenv("PLAYWRIGHT_HOST", "127.0.0.1")
    port = os.getenv("PLAYWRIGHT_PORT", "4173")
    _build_sales_dataset()
    _build_labor_seed()
    env = _server_env(host, port)
    os.chdir(ROOT)
    os.execvpe(
        sys.executable,
        [
            sys.executable,
            "run.py",
            "--fast",
            "--skip-seed",
            "--server",
            "--host",
            host,
            "--port",
            port,
            "--no-debug",
            "--no-reloader",
        ],
        env,
    )


if __name__ == "__main__":
    main()
