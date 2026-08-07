from __future__ import annotations

import copy
from decimal import Decimal

import numpy as np
import pandas as pd

from app.core.json_sanitizer import sanitize_for_json, dumps_sanitized
from app.blueprints import products as products_bp


def test_sanitize_for_json_strips_nan_and_inf():
    payload = {
        "nan_val": float("nan"),
        "inf_val": np.inf,
        "decimal": Decimal("10.50"),
        "timestamp": pd.Timestamp("2024-01-01"),
        "list": [np.nan, np.float64(1.2)],
    }

    safe = sanitize_for_json(payload)
    assert safe["nan_val"] is None
    assert safe["inf_val"] is None
    assert isinstance(safe["decimal"], float) and safe["decimal"] == 10.5
    assert isinstance(safe["timestamp"], str) and "2024-01-01" in safe["timestamp"]
    assert safe["list"][0] is None

    dumped = dumps_sanitized(payload)
    assert "NaN" not in dumped
    assert "Infinity" not in dumped


def test_products_overview_api_sanitizes_numbers(client, monkeypatch):
    payload = {
        "kpis": {"total_revenue": float("nan")},
        "trend": {"labels": ["2024-01"], "revenue": [np.inf], "qty": [], "margin": [], "weight": [], "asp": []},
        "price_dist": {"p50": Decimal("5.0"), "p10": None, "p90": None},
        "top_products": [],
        "breakdowns": {},
        "top_movers": [],
        "insights": [],
    }

    monkeypatch.setattr(
        products_bp,
        "build_overview_payload",
        lambda include_forecast=False, stage=None, filters=None, fallback_months=6: copy.deepcopy(payload),
    )
    monkeypatch.setattr("app.core.rbac._get_user", lambda: type("U", (), {"is_authenticated": True, "role": "admin"})(), raising=False)
    monkeypatch.setattr("app.core.rbac.has_role", lambda *args, **kwargs: True, raising=False)

    # Ensure cache isolation for this test
    try:
        products_bp.cache._store.clear()  # type: ignore[attr-defined]
    except Exception:
        try:
            products_bp.cache.clear()  # type: ignore[attr-defined]
        except Exception:
            pass

    resp = client.get("/products/api/overview")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "NaN" not in body
    assert "Infinity" not in body
    data = resp.get_json()
    assert data["kpis"]["total_revenue"] is None
    assert data["trend"]["revenue"][0] is None

    try:
        products_bp.cache._store.clear()  # type: ignore[attr-defined]
    except Exception:
        try:
            products_bp.cache.clear()  # type: ignore[attr-defined]
        except Exception:
            pass


def test_products_table_api_sanitizes(client, monkeypatch):
    payload = {
        "rows": [{"ProductId": 1, "Revenue": float("nan"), "Margin%": float("inf")}],
        "page": 1,
        "per_page": 50,
        "total": 1,
    }

    monkeypatch.setattr(
        products_bp,
        "build_table_payload",
        lambda page, per_page, sort_by="revenue", sort_dir="desc", **kwargs: copy.deepcopy(payload),
    )
    monkeypatch.setattr("app.core.rbac._get_user", lambda: type("U", (), {"is_authenticated": True, "role": "admin"})(), raising=False)
    monkeypatch.setattr("app.core.rbac.has_role", lambda *args, **kwargs: True, raising=False)

    resp = client.get("/products/api/table")
    text = resp.get_data(as_text=True)
    assert "NaN" not in text
    assert "Infinity" not in text
    data = resp.get_json()
    assert data["rows"][0]["Revenue"] is None
    assert data["rows"][0]["Margin%"] is None
