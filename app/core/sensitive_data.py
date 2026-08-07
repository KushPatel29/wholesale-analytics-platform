"""Helpers for masking cost- and margin-sensitive data consistently."""

from __future__ import annotations

from typing import Any, Dict

import pandas as pd


_COST_TOKENS = ("cost", "cogs", "spend", "coverage")
_MARGIN_TOKENS = ("margin", "gm", "contribution")
_PROFIT_TOKENS = ("profit",)
_RECOMMENDATION_TOKENS = ("recommendation", "pricing", "price_target", "price_floor", "uplift")
_RISK_TOKENS = ("margin_risk", "risk_margin", "negative_margin", "below_target_margin", "risk_tag")


def sensitive_access_flags(user: Any = None) -> Dict[str, bool]:
    from app.core import rbac

    cost = bool(rbac.can_view_costs(user))
    margin = bool(cost and rbac.can_view_margin(user))
    profit = bool(cost and rbac.can_view_profit(user))
    recommendations = bool(cost and rbac.can_view_price_recommendations(user))
    margin_risk = bool(cost and rbac.can_view_margin_risk(user))
    export_sensitive = bool(cost and rbac.can_export_sensitive_data(user))
    return {
        "cost": cost,
        "margin": margin,
        "profit": profit,
        "recommendations": recommendations,
        "margin_risk": margin_risk,
        "export_sensitive": export_sensitive,
    }


def _contains_any(name: str, tokens: tuple[str, ...]) -> bool:
    lowered = str(name or "").strip().lower()
    return bool(lowered) and any(token in lowered for token in tokens)


def sensitive_field_category(name: str) -> str | None:
    lowered = str(name or "").strip().lower()
    if not lowered:
        return None
    if _contains_any(lowered, _RECOMMENDATION_TOKENS):
        return "recommendations"
    if _contains_any(lowered, _RISK_TOKENS):
        return "margin_risk"
    if _contains_any(lowered, _PROFIT_TOKENS):
        return "profit"
    if _contains_any(lowered, _MARGIN_TOKENS):
        return "margin"
    if _contains_any(lowered, _COST_TOKENS):
        return "cost"
    return None


def _mask_scalar(value: Any, *, for_export: bool) -> Any:
    if for_export and isinstance(value, str):
        return ""
    return None


def mask_json_payload(payload: Any, user: Any = None) -> Any:
    flags = sensitive_access_flags(user)
    return _mask_json_value(payload, flags=flags)


def _mask_json_value(value: Any, *, flags: Dict[str, bool], key: str | None = None) -> Any:
    category = sensitive_field_category(key or "")
    if category == "recommendations" and not flags["recommendations"]:
        return [] if isinstance(value, list) else None
    if category == "margin_risk" and not flags["margin_risk"]:
        if isinstance(value, dict) and "rows" in value:
            out = dict(value)
            out["rows"] = []
            return out
        return [] if isinstance(value, list) else None
    if category == "profit" and not flags["profit"]:
        return None
    if category == "margin" and not flags["margin"]:
        return None
    if category == "cost" and not flags["cost"]:
        return None
    if isinstance(value, dict):
        return {sub_key: _mask_json_value(sub_value, flags=flags, key=sub_key) for sub_key, sub_value in value.items()}
    if isinstance(value, list):
        return [_mask_json_value(item, flags=flags) for item in value]
    return value


def mask_dataframe(df: pd.DataFrame | None, user: Any = None, *, for_export: bool = True) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame() if df is None else df
    flags = sensitive_access_flags(user)
    if flags["cost"] and flags["margin"] and flags["profit"] and flags["recommendations"] and flags["margin_risk"]:
        if not for_export or flags["export_sensitive"]:
            return df
    out = df.copy()
    for column in list(out.columns):
        category = sensitive_field_category(str(column))
        if category is None:
            continue
        if category == "cost" and flags["cost"] and (not for_export or flags["export_sensitive"]):
            continue
        if category == "margin" and flags["margin"] and (not for_export or flags["export_sensitive"]):
            continue
        if category == "profit" and flags["profit"] and (not for_export or flags["export_sensitive"]):
            continue
        if category == "recommendations" and flags["recommendations"] and (not for_export or flags["export_sensitive"]):
            continue
        if category == "margin_risk" and flags["margin_risk"] and (not for_export or flags["export_sensitive"]):
            continue
        out[column] = [_mask_scalar(value, for_export=for_export) for value in out[column].tolist()]
    return out


def mask_export_sheets(sheets: dict[str, pd.DataFrame], user: Any = None) -> dict[str, pd.DataFrame]:
    return {name: mask_dataframe(frame, user=user, for_export=True) for name, frame in (sheets or {}).items()}
