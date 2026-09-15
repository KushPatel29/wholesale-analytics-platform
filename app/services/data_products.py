"""Governed data-product contracts and runtime release evidence.

The control room deliberately derives its claims from repository contracts,
dbt metadata, SQL artefacts, and the active dataset manifest.  It is a
portfolio feature, but it behaves like a production control: missing evidence
degrades the release decision instead of being replaced with a reassuring
placeholder.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from app.services.metrics import metric_catalogue


REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = REPO_ROOT / "governance" / "data_products.yml"
SCHEMA_PATH = REPO_ROOT / "models" / "marts" / "schema.yml"
MODEL_DIRECTORY = REPO_ROOT / "models" / "marts"
DEFAULT_DATASET_PATH = REPO_ROOT / "cache" / "fact_dataset"
FRESHNESS_SLA_HOURS = 24

_TEXT_FIELDS = {
    "key",
    "name",
    "dbt_model",
    "owner",
    "steward",
    "decision",
    "grain",
    "decision_path",
}
_LIST_FIELDS = {"sources", "consumers", "quality_gates"}


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a mapping in {path}")
    return payload


def load_data_product_registry(path: Path = REGISTRY_PATH) -> tuple[dict[str, Any], ...]:
    """Load and validate the machine-readable operating contracts."""
    document = _load_yaml(path)
    if document.get("version") != 1:
        raise ValueError("Data-product registry version must be 1")

    schema_models = {
        str(model.get("name") or "").strip()
        for model in (_load_yaml(SCHEMA_PATH).get("models") or [])
        if isinstance(model, dict)
    }
    products = document.get("data_products") or []
    if not isinstance(products, list) or not products:
        raise ValueError("Data-product registry must contain at least one product")

    seen_keys: set[str] = set()
    seen_models: set[str] = set()
    validated: list[dict[str, Any]] = []
    for raw in products:
        if not isinstance(raw, dict):
            raise ValueError("Each data-product contract must be a mapping")
        missing = (_TEXT_FIELDS | _LIST_FIELDS | {"freshness_sla_hours", "recovery_target_hours"}) - set(raw)
        if missing:
            raise ValueError(f"Data-product contract is missing {sorted(missing)}")

        row = dict(raw)
        for field in _TEXT_FIELDS:
            row[field] = str(row.get(field) or "").strip()
            if not row[field]:
                raise ValueError(f"Data-product field {field!r} cannot be empty")
        for field in _LIST_FIELDS:
            values = row.get(field)
            if not isinstance(values, list) or not values or any(not str(value).strip() for value in values):
                raise ValueError(f"Data-product field {field!r} must be a non-empty list")
            row[field] = [str(value).strip() for value in values]

        row["freshness_sla_hours"] = int(row["freshness_sla_hours"])
        row["recovery_target_hours"] = int(row["recovery_target_hours"])
        if row["freshness_sla_hours"] <= 0 or row["recovery_target_hours"] <= 0:
            raise ValueError("Freshness and recovery targets must be positive")
        if row["key"] in seen_keys or row["dbt_model"] in seen_models:
            raise ValueError("Data-product keys and dbt models must be unique")
        if row["dbt_model"] not in schema_models:
            raise ValueError(f"Unknown dbt model {row['dbt_model']!r}")
        if not (MODEL_DIRECTORY / f"{row['dbt_model']}.sql").exists():
            raise ValueError(f"Missing SQL artefact for {row['dbt_model']!r}")
        seen_keys.add(row["key"])
        seen_models.add(row["dbt_model"])
        validated.append(row)

    return tuple(validated)


def _manifest_path(dataset_path: Path | None = None) -> Path:
    if dataset_path is None:
        configured = os.getenv("FACT_DATASET_PATH") or os.getenv("PARQUET_PATH")
        dataset_path = Path(configured) if configured else DEFAULT_DATASET_PATH
    dataset_path = dataset_path.expanduser()
    return dataset_path.parent / "_manifest.json" if dataset_path.suffix else dataset_path / "_manifest.json"


def dataset_evidence(
    dataset_path: Path | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return explicit runtime evidence without inventing missing values."""
    path = _manifest_path(dataset_path)
    base = {
        "manifest_path": path.as_posix(),
        "observed": False,
        "state": "NOT OBSERVED",
        "age_hours": None,
        "last_refresh_utc": None,
        "row_count": None,
        "min_date": None,
        "max_date": None,
        "dataset_version": None,
        "source": None,
        "synthetic": None,
        "guidance": "Build or refresh the fact dataset before approving a release.",
    }
    if not path.exists():
        return base
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw_refresh = payload.get("last_refresh_utc") or payload.get("built_at_utc") or payload.get("built_at")
        if not raw_refresh:
            raise ValueError("manifest has no refresh timestamp")
        refreshed = datetime.fromisoformat(str(raw_refresh).replace("Z", "+00:00"))
        if refreshed.tzinfo is None:
            refreshed = refreshed.replace(tzinfo=timezone.utc)
        observed_now = now or datetime.now(timezone.utc)
        if observed_now.tzinfo is None:
            observed_now = observed_now.replace(tzinfo=timezone.utc)
        age_hours = max(0.0, (observed_now.astimezone(timezone.utc) - refreshed.astimezone(timezone.utc)).total_seconds() / 3600)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return {**base, "state": "INVALID", "guidance": f"Repair the dataset manifest: {exc}"}

    current = age_hours <= FRESHNESS_SLA_HOURS
    return {
        **base,
        "observed": True,
        "state": "CURRENT" if current else "STALE",
        "age_hours": round(age_hours, 1),
        "last_refresh_utc": refreshed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "row_count": payload.get("row_count", payload.get("rows")),
        "min_date": payload.get("min_date", payload.get("date_min")),
        "max_date": payload.get("max_date", payload.get("date_max")),
        "dataset_version": payload.get("dataset_version"),
        "source": payload.get("source"),
        "synthetic": payload.get("synthetic"),
        "guidance": "Runtime evidence is within the release freshness SLA." if current else "Refresh the fact dataset before approving a release.",
    }


def data_product_control_room(
    registry_path: Path = REGISTRY_PATH,
    dataset_path: Path | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compose contracts, catalogue coverage, and runtime evidence for the UI."""
    products = [dict(product) for product in load_data_product_registry(registry_path)]
    metrics = metric_catalogue()
    metrics_by_model: dict[str, list[dict[str, str]]] = {}
    for metric in metrics:
        metrics_by_model.setdefault(metric["model_name"], []).append(metric)

    mapped_metric_keys: set[str] = set()
    for product in products:
        product_metrics = metrics_by_model.get(product["dbt_model"], [])
        mapped_metric_keys.update(metric["key"] for metric in product_metrics)
        product["metric_count"] = len(product_metrics)
        product["implemented_metric_count"] = sum(metric["status"] == "Implemented" for metric in product_metrics)
        product["certifications"] = sorted({metric["certification"] for metric in product_metrics})

    runtime = dataset_evidence(dataset_path, now=now)
    total_metrics = len(metrics)
    mapped_metrics = len(mapped_metric_keys)
    contract_ready = all(product["metric_count"] > 0 for product in products) and mapped_metrics == total_metrics
    if runtime["state"] == "CURRENT" and contract_ready:
        release_state = "GO"
        release_guidance = "All product contracts resolve and runtime evidence is current. Proceed with the executive review."
    elif runtime["state"] == "STALE" and contract_ready:
        release_state = "REVIEW"
        release_guidance = "Contracts resolve, but runtime evidence is stale. Refresh before making a new operating decision."
    else:
        release_state = "NO-GO"
        release_guidance = "Do not approve this data release until missing contract or runtime evidence is restored."

    registry_bytes = registry_path.read_bytes()
    fingerprint_material = registry_bytes + "|".join(sorted(mapped_metric_keys)).encode("utf-8")
    fingerprint = hashlib.sha256(fingerprint_material).hexdigest()[:12].upper()
    quality_gate_count = sum(len(product["quality_gates"]) for product in products)
    consumers = sorted({consumer for product in products for consumer in product["consumers"]})
    return {
        "release_state": release_state,
        "release_guidance": release_guidance,
        "fingerprint": fingerprint,
        "products": products,
        "product_count": len(products),
        "metric_count": total_metrics,
        "mapped_metric_count": mapped_metrics,
        "contract_coverage_pct": round((mapped_metrics / total_metrics * 100) if total_metrics else 0),
        "quality_gate_count": quality_gate_count,
        "consumer_count": len(consumers),
        "runtime": runtime,
    }
