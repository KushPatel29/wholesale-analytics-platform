import pandas as pd

from app.services.filters import build_filter_summary, get_fiscal_periods, normalize_filters, parse_filters


def test_parse_filters_defaults_to_current_fy():
    params = parse_filters({})
    assert params.preset == "current_fy"
    assert params.date_type == "fiscal"
    assert params.start is not None
    assert params.end is not None
    assert params.start <= params.end


def test_normalize_filters_keeps_explicit_preset_all():
    params = normalize_filters({"date_preset": "all"})
    assert params.preset == "all"
    assert params.start is None
    assert params.end is None


def test_parse_filters_ignores_drilldown_salesrep_id():
    params = parse_filters({"salesrep_id": "R1"})
    assert params.sales_reps == ()


def test_parse_filters_accepts_plural_sales_rep_keys():
    params = parse_filters({"sales_rep_ids": ["R1", "R2"]})
    assert params.sales_reps == ("R1", "R2")


def test_parse_filters_supports_today_preset():
    params = parse_filters({"date_preset": "today"})
    assert params.preset == "today"
    assert params.start is not None
    assert params.end is not None
    assert params.start == params.end


def test_parse_filters_marks_new_fiscal_presets_as_fiscal():
    params = parse_filters({"date_preset": "current_fm"})
    periods = get_fiscal_periods(pd.Timestamp.now(tz="UTC"))
    assert params.preset == "current_fm"
    assert params.date_type == "fiscal"
    assert params.start == periods["current_fm"]["start"]
    assert params.end == periods["current_fm"]["end"]


def test_fixed_fiscal_year_preset_uses_the_canonical_october_year():
    params = parse_filters({"date_preset": "fy2025"})

    assert params.date_type == "fiscal"
    assert str(params.start.date()) == "2024-10-01"
    assert str(params.end.date()) == "2025-09-30"


def test_get_fiscal_periods_uses_october_year_start():
    periods = get_fiscal_periods(pd.Timestamp("2026-04-08"))
    assert str(periods["current_fy"]["start"].date()) == "2025-10-01"
    assert str(periods["current_fy"]["end"].date()) == "2026-04-08"
    assert str(periods["previous_fy"]["start"].date()) == "2024-10-01"
    assert str(periods["previous_fy"]["end"].date()) == "2025-09-30"
    assert str(periods["current_fq"]["start"].date()) == "2026-04-01"
    assert str(periods["current_fq"]["end"].date()) == "2026-04-08"
    assert str(periods["previous_fq"]["start"].date()) == "2026-01-01"
    assert str(periods["previous_fq"]["end"].date()) == "2026-03-31"
    assert str(periods["current_fm"]["start"].date()) == "2026-04-01"
    assert str(periods["current_fm"]["end"].date()) == "2026-04-08"
    assert str(periods["current_fm"]["comparison_start"].date()) == "2026-03-01"
    assert str(periods["current_fm"]["comparison_end"].date()) == "2026-03-08"
    assert str(periods["previous_fm"]["start"].date()) == "2026-03-01"
    assert str(periods["previous_fm"]["end"].date()) == "2026-03-31"
    assert str(periods["fytd_comparison"]["comparison_start"].date()) == "2024-10-01"
    assert str(periods["fytd_comparison"]["comparison_end"].date()) == "2025-04-08"


def test_demo_fiscal_periods_anchor_to_the_immutable_dataset(monkeypatch):
    from app.services import fact_store

    monkeypatch.setenv("DEMO_PREBUILT_CACHE_READ", "1")
    monkeypatch.setattr(fact_store, "get_meta", lambda: {"date_max": "2020-06-15"})

    periods = get_fiscal_periods()

    assert str(periods["current_fy"]["end"].date()) == "2020-06-15"

    monkeypatch.setenv("DEMO_PREBUILT_CACHE_READ", "0")
    monkeypatch.setenv("DEMO_PREBUILT_CACHE_WRITE", "1")
    build_periods = get_fiscal_periods()
    assert str(build_periods["current_fy"]["end"].date()) == "2020-06-15"
    parsed_preset_periods = get_fiscal_periods(pd.Timestamp("2022-01-15"))
    assert str(parsed_preset_periods["current_fy"]["end"].date()) == "2020-06-15"


def test_parse_filters_accepts_schema_alias_names():
    params = parse_filters(
        {
            "date_start": "2025-01-01",
            "date_end": "2025-01-31",
            "region_ids": ["west"],
            "ship_method_ids": ["ground"],
        }
    )
    assert str(params.start.date()) == "2025-01-01"
    assert str(params.end.date()) == "2025-01-31"
    assert params.regions == ("west",)
    assert params.methods == ("ground",)


def test_build_filter_summary_compacts_dimension_labels():
    summary = build_filter_summary(
        {
            "date_preset": "current_fy",
            "date_type": "fiscal",
            "regions": ["Burnaby", "Delta", "Vancouver W"],
            "suppliers": ["Alberta Bison", "Wholesale Provisions", "Northshore"],
        }
    )
    assert summary["date_label"] == "Current FY"
    assert summary["active_count"] == 3
    assert summary["dimension_count"] == 2
    chips = {chip["key"]: chip for chip in summary["dimension_chips"]}
    assert chips["regions"]["summary"] == "Burnaby, Delta +1"
    assert chips["suppliers"]["summary"] == "Alberta Bison, Northshore +1"


def test_build_filter_summary_includes_advanced_filter_chips():
    summary = build_filter_summary(
        {
            "start": "2025-01-01",
            "end": "2025-01-31",
            "protein_min": 10,
            "protein_max": 25,
            "protein_name": "beef",
            "complete_months_only": "1",
        }
    )
    chips = {chip["key"]: chip for chip in summary["dimension_chips"]}
    assert chips["protein_range"]["summary"] == ">= 10 <= 25"
    assert chips["protein_name_like"]["summary"] == "beef"
    assert chips["complete_months_only"]["summary"] == "Full months only"


def test_build_filter_summary_orders_sales_reps_between_customers_and_suppliers():
    summary = build_filter_summary(
        {
            "customers": ["Customer A"],
            "sales_reps": ["R-001"],
            "suppliers": ["Supplier A"],
        }
    )
    assert [chip["key"] for chip in summary["dimension_chips"]] == ["customers", "sales_reps", "suppliers"]
