from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from app.services.data_products import (
    REGISTRY_PATH,
    data_product_control_room,
    dataset_evidence,
    load_data_product_registry,
)
from app.services.metrics import metric_catalogue


NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def _write_manifest(path, *, refreshed=NOW, rows=125_400):
    path.mkdir(parents=True, exist_ok=True)
    (path / "_manifest.json").write_text(
        json.dumps(
            {
                "last_refresh_utc": refreshed.isoformat(),
                "dataset_version": "release-2026-09-15",
                "row_count": rows,
                "min_date": "2024-01-01",
                "max_date": "2026-09-14",
                "source": "seed.generate_synthetic_data",
                "synthetic": True,
            }
        ),
        encoding="utf-8",
    )


def test_registry_defines_nine_decision_products():
    products = load_data_product_registry()
    assert len(products) == 9
    assert {product["key"] for product in products} == {
        "commercial_performance",
        "customer_movement",
        "inventory_performance",
        "labor_performance",
        "forecast_performance",
        "returns_performance",
        "corrective_actions",
        "finance_monthly",
        "marketing_performance",
    }


def test_every_contract_has_accountability_and_service_levels():
    for product in load_data_product_registry():
        assert product["owner"]
        assert product["steward"]
        assert product["quality_gates"]
        assert product["freshness_sla_hours"] > 0
        assert product["recovery_target_hours"] > 0


def test_every_catalogue_metric_maps_to_one_data_product(tmp_path):
    _write_manifest(tmp_path)
    room = data_product_control_room(dataset_path=tmp_path, now=NOW)
    assert room["mapped_metric_count"] == len(metric_catalogue()) == 62
    assert room["contract_coverage_pct"] == 100
    assert all(product["metric_count"] > 0 for product in room["products"])


def test_current_runtime_evidence_is_observed(tmp_path):
    _write_manifest(tmp_path, refreshed=NOW - timedelta(hours=8))
    evidence = dataset_evidence(tmp_path, now=NOW)
    assert evidence["state"] == "CURRENT"
    assert evidence["observed"] is True
    assert evidence["age_hours"] == 8.0
    assert evidence["row_count"] == 125_400


def test_stale_runtime_evidence_requires_refresh(tmp_path):
    _write_manifest(tmp_path, refreshed=NOW - timedelta(hours=25))
    evidence = dataset_evidence(tmp_path, now=NOW)
    assert evidence["state"] == "STALE"
    assert "Refresh" in evidence["guidance"]


def test_missing_runtime_evidence_is_explicit(tmp_path):
    evidence = dataset_evidence(tmp_path / "not-built", now=NOW)
    assert evidence["state"] == "NOT OBSERVED"
    assert evidence["observed"] is False
    assert evidence["row_count"] is None


def test_invalid_runtime_evidence_fails_closed(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "_manifest.json").write_text("not-json", encoding="utf-8")
    evidence = dataset_evidence(tmp_path, now=NOW)
    assert evidence["state"] == "INVALID"
    assert "Repair" in evidence["guidance"]


def test_release_is_go_only_with_current_runtime_and_complete_contracts(tmp_path):
    _write_manifest(tmp_path, refreshed=NOW - timedelta(hours=2))
    room = data_product_control_room(dataset_path=tmp_path, now=NOW)
    assert room["release_state"] == "GO"
    assert room["quality_gate_count"] == 27
    assert len(room["fingerprint"]) == 12


def test_stale_runtime_downgrades_release_to_review(tmp_path):
    _write_manifest(tmp_path, refreshed=NOW - timedelta(days=2))
    room = data_product_control_room(dataset_path=tmp_path, now=NOW)
    assert room["release_state"] == "REVIEW"


def test_registry_validation_rejects_an_unowned_product(tmp_path):
    document = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    document["data_products"][0]["owner"] = ""
    bad_registry = tmp_path / "data_products.yml"
    bad_registry.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError, match="owner"):
        load_data_product_registry(bad_registry)


def test_metrics_route_renders_runtime_and_contract_evidence(client, monkeypatch, tmp_path):
    _write_manifest(tmp_path, refreshed=datetime.now(timezone.utc) - timedelta(minutes=10))
    monkeypatch.setenv("FACT_DATASET_PATH", str(tmp_path))
    response = client.get("/metrics/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Data Product Control Room" in html
    assert "The 90-second review path" in html
    assert "125400" in html
    assert "9 dbt-backed data products" in html
    assert "62 implemented" not in html
    assert "62" in html
