"""Static-demo contracts for the final portfolio page upgrades.

These pages run twice: once as the Flask app and once as script-free GitHub
Pages output.  The checks below guard the wiring that is easiest to lose while
refactoring a renderer: the chart mount, the hermetic map inputs, and the small
runtime bindings that survive the freeze pass.
"""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_inventory_upgrade_mounts_aging_and_holding_cost_charts():
    template = _read("app/templates/inventory/index.html")
    script = _read("app/static/js/inventory.js")

    assert 'id="inventoryAging"' in template
    assert 'id="inventoryHoldingSplit"' in template
    assert "drawAging(payload?.aging" in script
    assert "drawHolding(payload?.holding_cost" in script
    assert 'type: "scattergl"' not in script


def test_supplier_upgrade_is_derived_from_the_existing_table_payload():
    template = _read("app/templates/suppliers/index.html")
    script = _read("app/static/js/suppliers.js")

    assert 'id="supplierPriorities"' in template
    assert 'id="supplierBreadthChart"' in template
    assert "renderSupplierPriorities(payload.table || {})" in script
    assert "renderSupplierBreadth(payload.table || {})" in script
    assert "supplierRows(table)" in script


def test_account_map_has_a_local_basemap_and_a_nonempty_fallback():
    template = _read("app/templates/salesreps/index.html")
    script = _read("app/static/js/salesreps.js")
    builder = _read("build_static.py")

    assert "vendor/geo/us-states.geojson" in template
    assert (ROOT / "app/static/vendor/geo/us-states.geojson").stat().st_size > 10_000
    assert "if (mapped.length) return mapped" in script
    assert "analysis.top_customers" in script
    assert "preserveDrawingBuffer: true" in script
    assert "silent_days: silentDays" in script
    assert "maplibre-gl@4.7.1" in builder
    assert "[data-live-map-controls]" in builder


def test_planner_print_and_view_controls_survive_the_static_freeze():
    template = _read("app/templates/stakeholder_report/index.html")
    script = _read("app/static/js/stakeholder_report.js")
    builder = _read("build_static.py")

    assert "data-print-report" in template
    assert "onclick=\"window.print()\"" not in template
    assert "[data-print-report]" in script
    assert "function bindPrint()" in builder
    assert "function bindReportViews()" in builder
    assert "body[data-static-page] .report-section{opacity:1!important" in builder


def test_static_shell_menu_and_pointer_aligned_details_survive_the_freeze():
    builder = _read("build_static.py")

    assert "function bindShellMenus()" in builder
    assert "function bindHoverDetails()" in builder
    assert "bindShellMenus(); bindHoverDetails();" in builder
    assert '[data-bs-toggle="dropdown"]' in builder
    assert '[data-bs-toggle="collapse"]' in builder
    assert "event.clientX, event.clientY" in builder
    assert ".wa-tip-runtime [data-wa-tip]::after" in builder


def test_static_chart_gate_requires_data_and_drawn_pixels():
    builder = _read("build_static.py")

    assert "const canvasPainted = (canvas)" in builder
    assert "Chart.getChart(canvas)" in builder
    assert "plot._fullData" in builder
    assert "visible charts never rendered" in builder
    assert "canvas export contains no visible chart pixels" in builder
    assert "sharpenCanvas" not in builder


def test_products_empty_trajectory_resolves_to_an_intentional_state():
    script = _read("app/static/js/products.js")

    assert 'removeSkeleton("trendChart")' in script
    assert 'ctx.classList.add("d-none")' in script
    assert 'emptyState.id = "trendChartEmptyState"' in script
    assert "No revenue or demand activity is available for this filter window." in script
    assert 'ctx.classList.remove("d-none")' in script


def test_long_decision_pages_publish_a_consistent_reading_path():
    overview = _read("app/templates/overview/index_v3.html")
    inventory = _read("app/templates/inventory/index.html")
    salesreps = _read("app/templates/salesreps/index.html")

    assert 'class="overview-routebar"' in overview
    assert 'href="#trendDriversSection"' in overview
    assert 'class="inventory-routebar"' in inventory
    assert 'href="#inventory-actions"' in inventory
    assert 'class="sr-routebar mb-3"' in salesreps
    assert 'href="#srMomentumSection"' in salesreps
