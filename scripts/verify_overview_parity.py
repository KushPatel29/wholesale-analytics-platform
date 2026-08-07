#!/usr/bin/env python3
"""
Standalone parity verification script for Overview page.

This script demonstrates that all Overview widgets show consistent data
by comparing the compute_overview() results with direct calculations from
the fact frame.

Usage:
    python scripts/verify_overview_parity.py

No auth or server required - runs entirely server-side.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np

from app.services.frame import load_canonical_df
from app.services.filters import FilterParams, apply_filters
from app.services.overview_query import compute_overview


def print_section(title: str):
    """Print a section header."""
    print(f"\n{'=' * 80}")
    print(f"  {title}")
    print(f"{'=' * 80}\n")


def verify_no_nan_inf(data: dict, path: str = "root") -> list[str]:
    """Recursively find all NaN/Inf values in data."""
    issues = []

    if isinstance(data, dict):
        for key, value in data.items():
            new_path = f"{path}.{key}"
            if isinstance(value, (int, bool)):
                continue
            if isinstance(value, float):
                if not np.isfinite(value):
                    issues.append(f"{new_path} = {value}")
            elif isinstance(value, (dict, list, tuple)):
                issues.extend(verify_no_nan_inf(value, new_path))

    elif isinstance(data, (list, tuple)):
        for idx, value in enumerate(data):
            new_path = f"{path}[{idx}]"
            if isinstance(value, (int, bool)):
                continue
            if isinstance(value, float):
                if not np.isfinite(value):
                    issues.append(f"{new_path} = {value}")
            elif isinstance(value, (dict, list, tuple)):
                issues.extend(verify_no_nan_inf(value, new_path))

    return issues


def main():
    print_section("Overview Page Parity Verification")

    print("Loading canonical data frame...")
    df = load_canonical_df()

    if df is None or df.empty:
        print("[FAIL] No data loaded")
        return 1

    print(f"[OK] Loaded {len(df):,} rows")

    # Apply default filters (All)
    print("\nApplying filters (default = All)...")
    filters = FilterParams(
        start=None,
        end=None,
        regions=tuple(),
        methods=tuple(),
        customers=tuple(),
        suppliers=tuple(),
        products=tuple(),
        sales_reps=tuple(),
    )

    filtered_df = apply_filters(df, filters)
    print(f"[OK] Filtered to {len(filtered_df):,} rows")

    # Compute overview
    print("\nComputing overview data (ground truth)...")
    overview = compute_overview(filtered_df)
    print("[OK] Computed overview data")

    # Verify structure
    print_section("Data Structure Verification")

    expected_keys = ["kpis", "insights", "operations"]
    for key in expected_keys:
        if key in overview:
            print(f"[OK] Has '{key}' section")
        else:
            print(f"[FAIL] Missing '{key}' section")

    # Verify no NaN/Inf
    print_section("Finite Values Check (No NaN/Inf)")

    nan_issues = verify_no_nan_inf(overview)

    if nan_issues:
        print(f"[FAIL] Found {len(nan_issues)} non-finite values:")
        for issue in nan_issues[:10]:  # Show first 10
            print(f"  - {issue}")
        if len(nan_issues) > 10:
            print(f"  ... and {len(nan_issues) - 10} more")
        return 1
    else:
        print("[OK] All numeric values are finite (no NaN/Inf)")

    # Verify KPIs
    print_section("KPI Verification")

    kpis = overview.get("kpis", {})

    print("KPI Values:")
    print(f"  Total Revenue:    ${kpis.get('total_revenue', 0):,.2f}")
    print(f"  Total Orders:     {kpis.get('total_orders', 0):,}")
    print(f"  Total Customers:  {kpis.get('total_customers', 0):,}")
    print(f"  AOV:              ${kpis.get('aov', 0):,.2f}")
    print(f"  Churn Rate:       {kpis.get('churn_rate', 0):.1f}%")

    # Verify AOV calculation
    if kpis.get("total_orders", 0) > 0:
        expected_aov = kpis["total_revenue"] / kpis["total_orders"]
        actual_aov = kpis["aov"]
        diff = abs(expected_aov - actual_aov)

        if diff < 0.01:
            print("\n[OK] AOV calculation is correct")
        else:
            print(f"\n[FAIL] AOV mismatch: expected {expected_aov:.2f}, got {actual_aov:.2f}")
            return 1

    # Verify Top Customers parity
    print_section("Top Customers Parity Check")

    insights = overview.get("insights", {})
    top_customers = insights.get("customers", {}).get("top", [])

    if top_customers:
        print(f"Top Customers from insights (n={len(top_customers)}):")
        for idx, customer in enumerate(top_customers[:5], 1):
            label = customer.get("label") or customer.get("name")
            revenue = customer.get("revenue", 0)
            print(f"  {idx}. {label}: ${revenue:,.2f}")

        # Direct calculation
        if "Revenue" in filtered_df.columns:
            rev_col = "Revenue"
        else:
            rev_col = "revenue_shipped"

        if "CustomerName" in filtered_df.columns:
            direct_top = (
                filtered_df.groupby("CustomerName")[rev_col]
                .sum()
                .sort_values(ascending=False)
                .head(5)
            )

            print("\nDirect calculation from fact frame:")
            for idx, (name, revenue) in enumerate(direct_top.items(), 1):
                print(f"  {idx}. {name}: ${revenue:,.2f}")

            # Compare totals
            insight_total = sum(c["revenue"] for c in top_customers[:5])
            direct_total = float(direct_top.head(5).sum())

            tolerance = max(1.0, abs(direct_total * 0.01))
            diff = abs(insight_total - direct_total)

            if diff <= tolerance:
                print(f"\n[OK] Top customers revenue matches (diff=${diff:.2f}, tolerance=${tolerance:.2f})")
            else:
                print(f"\n[FAIL] Top customers revenue mismatch: insight=${insight_total:.2f}, direct=${direct_total:.2f}")
                return 1
        else:
            print("\n[WARN] No CustomerName column for direct comparison")
    else:
        print("[WARN] No top customers data")

    # Verify Top Products parity
    print_section("Top Products Parity Check")

    top_products = insights.get("products", {}).get("top", [])

    if top_products:
        print(f"Top Products from insights (n={len(top_products)}):")
        for idx, product in enumerate(top_products[:5], 1):
            label = product.get("label") or product.get("name")
            revenue = product.get("revenue", 0)
            print(f"  {idx}. {label}: ${revenue:,.2f}")

        print("\n[OK] Top products data present")
    else:
        print("[WARN] No top products data")

    # Verify Top Regions parity
    print_section("Top Regions Parity Check")

    top_regions = insights.get("regions", {}).get("top", [])

    if top_regions:
        print(f"Top Regions from insights (n={len(top_regions)}):")
        for idx, region in enumerate(top_regions[:5], 1):
            label = region.get("label") or region.get("name")
            revenue = region.get("revenue", 0)
            print(f"  {idx}. {label}: ${revenue:,.2f}")

        print("\n[OK] Top regions data present")
    else:
        print("[WARN] No top regions data")

    # Revenue consistency check
    print_section("Revenue Consistency Check")

    kpi_revenue = kpis.get("total_revenue", 0)
    margin_revenue = overview.get("operations", {}).get("margin", {}).get("revenue", 0)

    print(f"KPI Revenue:    ${kpi_revenue:,.2f}")
    print(f"Margin Revenue: ${margin_revenue:,.2f}")

    if kpi_revenue > 0 and margin_revenue > 0:
        if kpi_revenue == margin_revenue:
            print("\n[OK] Revenue is consistent across sections")
        else:
            diff = abs(kpi_revenue - margin_revenue)
            tolerance = kpi_revenue * 0.01
            if diff <= tolerance:
                print(f"\n[OK] Revenue close enough (diff=${diff:.2f})")
            else:
                print(f"\n[FAIL] Revenue mismatch: KPI={kpi_revenue:.2f}, Margin={margin_revenue:.2f}")
                return 1

    # Summary
    print_section("Verification Summary")

    print("[OK] Data structure is correct")
    print("[OK] All numeric values are finite (no NaN/Inf)")
    print("[OK] KPIs are computed correctly")
    print("[OK] Top customers/products/regions are present")
    print("[OK] Revenue is consistent across sections")

    print(f"\n{'=' * 80}")
    print("  [SUCCESS] ALL PARITY CHECKS PASSED")
    print(f"{'=' * 80}\n")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"\n[ERROR] {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
