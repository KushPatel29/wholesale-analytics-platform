from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

import pandas as pd
from flask import current_app, render_template, url_for

from app.auth.models import SessionLocal, User, list_effective_permission_keys_for_user
from app.auth.notifications_models import NotificationEvent, NotificationType, UserNotificationPreference
from app.core.exports import dataframes_to_xlsx_bytes, sanitize_filename, xlsx_export_available
from app.core.access_policy import scope_for_user
from app.services import fact_store
from app.services.mailer import send_email
from app.services.notifications_catalog import NOTIFICATION_CATALOG, active_alert_keys, catalog_by_category, catalog_index


_ALLOWED_FREQUENCIES = {"immediate", "daily", "weekly"}
_ALLOWED_SCOPE_MODES = {"rbac", "self"}
_RUNNER_SUPPORTED_KEYS = {"data_freshness_sla"}
_DIGEST_WAIT = {
    "daily": timedelta(hours=20),
    "weekly": timedelta(days=6),
}
_XLSX_MIMETYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_frequency(value: Any, default: str = "daily") -> str:
    token = str(value or "").strip().lower()
    if token in _ALLOWED_FREQUENCIES:
        return token
    return default


def _normalize_scope_mode(value: Any, *, user: Any = None, default: str = "rbac") -> str:
    role = str(getattr(user, "role", "") or "").strip().lower()
    token = str(value or "").strip().lower()
    if role == "sales":
        return "self"
    if token in _ALLOWED_SCOPE_MODES:
        return token
    return default


def _int_value(value: Any, default: int, *, minimum: int = 0, maximum: int = 365) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = int(default)
    return max(minimum, min(maximum, parsed))


def _float_value(value: Any, default: float, *, minimum: float = 0.0, maximum: float = 1_000_000.0) -> float:
    try:
        parsed = float(value)
    except Exception:
        parsed = float(default)
    return max(minimum, min(maximum, parsed))


def _safe_json_loads(raw: Any, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return dict(default or {})
    try:
        value = json.loads(str(raw))
    except Exception:
        return dict(default or {})
    return dict(value) if isinstance(value, dict) else dict(default or {})


def _safe_json_dumps(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, default=str)


def _full_name(user: Any) -> str:
    parts = [str(getattr(user, "first_name", "") or "").strip(), str(getattr(user, "last_name", "") or "").strip()]
    full = " ".join(part for part in parts if part)
    if full:
        return full
    return str(getattr(user, "username", "") or "").strip()


def _has_notifications_access(user: Any) -> bool:
    if not user:
        return False
    try:
        keys = set(
            list_effective_permission_keys_for_user(
                int(getattr(user, "id", 0) or 0),
                fallback_role=str(getattr(user, "role", "") or "").strip().lower() or None,
            )
        )
    except Exception:
        keys = set()
    return "*" in keys or "page.notifications.view" in keys


def _catalog_defaults(type_key: str) -> Dict[str, Any]:
    item = catalog_index().get(type_key) or {}
    defaults = item.get("default_config") or {}
    return dict(defaults) if isinstance(defaults, dict) else {}


def _humanize_threshold_label(key: str) -> str:
    token = str(key or "").strip()
    mapping = {
        "percent_drop": "Percent drop",
        "percent_gain": "Percent gain",
        "min_dollar_impact": "Minimum $ impact",
        "lookback_months": "Lookback (months)",
        "margin_target_pct": "Margin target %",
        "min_revenue": "Minimum revenue $",
        "top_n": "Top items",
        "top1_share_pct": "Top 1 share %",
        "top5_share_pct": "Top 5 share %",
        "hhi_threshold": "HHI threshold",
        "max_staleness_days": "Max staleness (days)",
        "min_cost_coverage_pct": "Minimum cost coverage %",
        "at_risk_days": "At-risk days",
        "churn_days": "Churn days",
        "volatility_cv": "Volatility CV",
        "cooldown_hours": "Cooldown (hours)",
        "min_revenue_gain": "Minimum revenue gain $",
    }
    if token in mapping:
        return mapping[token]
    return token.replace("_", " ").strip().title()


def _threshold_bounds(key: str, default: Any) -> tuple[float, float, float]:
    token = str(key or "").strip().lower()
    if token.endswith("_pct") or token.startswith("percent_"):
        return (0.0, 100.0, 1.0)
    if token.endswith("_days"):
        return (0.0, 365.0, 1.0)
    if token.endswith("_hours"):
        return (1.0, 168.0, 1.0)
    if token in {"top_n", "lookback_months"}:
        return (1.0, 100.0, 1.0)
    if token == "hhi_threshold":
        return (0.0, 10000.0, 10.0)
    if token == "volatility_cv":
        return (0.0, 10.0, 0.1)
    if "revenue" in token or "impact" in token:
        return (0.0, 1_000_000.0, 100.0)
    if isinstance(default, float):
        return (0.0, 1_000_000.0, 0.1)
    return (0.0, 1_000_000.0, 1.0)


def _coerce_threshold_value(key: str, raw_value: Any, default: Any) -> Any:
    low, high, step = _threshold_bounds(key, default)
    if isinstance(default, float) and not isinstance(default, bool):
        value = _float_value(raw_value, float(default), minimum=low, maximum=high)
        return round(value, 4)
    return _int_value(raw_value, int(default), minimum=int(low), maximum=int(high))


def _threshold_inputs_for_type(type_key: str, thresholds: Mapping[str, Any]) -> List[Dict[str, Any]]:
    fields: List[Dict[str, Any]] = []
    for key, value in thresholds.items():
        if key == "cooldown_hours":
            continue
        low, high, step = _threshold_bounds(key, value)
        fields.append(
            {
                "key": key,
                "input_name": f"{type_key}_{key}",
                "label": _humanize_threshold_label(key),
                "value": value,
                "min": low,
                "max": high,
                "step": step,
            }
        )
    return fields


def _merged_thresholds(defaults: Mapping[str, Any], pref_config: Mapping[str, Any]) -> Dict[str, Any]:
    merged = dict(defaults.get("config") or {})
    user_thresholds = pref_config.get("thresholds") or {}
    if isinstance(user_thresholds, Mapping):
        merged.update({str(k): v for k, v in user_thresholds.items()})
    return merged


def _scope_mode_label(scope_mode: str) -> str:
    return "My customers only" if str(scope_mode or "").strip().lower() == "self" else "Authorized scope"


def _format_threshold_value(key: str, value: Any) -> str:
    token = str(key or "").strip().lower()
    numeric: Optional[float] = None
    try:
        numeric = float(value)
    except Exception:
        numeric = None
    if numeric is None:
        return str(value)
    if token.endswith("_pct") or token.startswith("percent_"):
        if numeric.is_integer():
            return f"{int(numeric)}%"
        return f"{numeric:.1f}%"
    if "revenue" in token or "impact" in token:
        return f"${numeric:,.0f}"
    if token in {"volatility_cv"}:
        return f"{numeric:.2f}"
    if numeric.is_integer():
        return str(int(numeric))
    return f"{numeric:.2f}"


def _threshold_summary_rows(thresholds: Mapping[str, Any]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for key, value in thresholds.items():
        rows.append({"label": _humanize_threshold_label(key), "value": _format_threshold_value(key, value)})
    return rows


def _sample_alert_payload(type_key: str, pref_state: Mapping[str, Any]) -> Dict[str, Any]:
    thresholds = dict(pref_state.get("thresholds") or {})
    scope_mode = str(pref_state.get("scope_mode") or "rbac")
    frequency = str(pref_state.get("frequency") or "daily").title()
    base_details = [
        {"label": "Delivery", "value": "Email"},
        {"label": "Frequency", "value": frequency},
        {"label": "Scope", "value": _scope_mode_label(scope_mode)},
    ]
    summary_map = {
        "customer_revenue_drop": f"Two customers fell more than {_format_threshold_value('percent_drop', thresholds.get('percent_drop', 30))} month over month, exceeding your configured impact threshold.",
        "customer_profit_drop": f"Profit declined materially for two customers and crossed your {_format_threshold_value('percent_drop', thresholds.get('percent_drop', 25))} alert threshold.",
        "product_margin_below_target": "Four SKUs are running below their mapped department target margin while still carrying meaningful revenue.",
        "negative_margin_products": "Negative-margin sales were detected in the current window for multiple SKUs and require follow-up.",
        "new_large_customer_gain": f"A new customer gain exceeded your configured growth threshold of {_format_threshold_value('percent_gain', thresholds.get('percent_gain', 20))}.",
        "concentration_risk_increase": "Customer concentration increased beyond your configured Top 1 / Top 5 or HHI guardrails.",
        "data_freshness_sla": f"Fact data is {int(thresholds.get('max_staleness_days', 1)) + 2} day(s) old and exceeds your freshness SLA.",
        "cost_coverage_drop": f"Cost coverage fell below your {_format_threshold_value('min_cost_coverage_pct', thresholds.get('min_cost_coverage_pct', 95))} threshold in the active window.",
        "duplicate_integrity_issue": "Duplicate groups or integrity exceptions were detected and need admin review.",
        "at_risk_customers": f"Several customers have gone more than {_format_threshold_value('at_risk_days', thresholds.get('at_risk_days', 30))} days without ordering.",
        "newly_churned_customers": f"Customers crossed your {_format_threshold_value('churn_days', thresholds.get('churn_days', 45))}-day churn threshold.",
        "reactivated_customers": "Previously inactive customers returned and may need immediate follow-up.",
        "forecast_drop_anomaly": f"The next forecast snapshot dropped more than {_format_threshold_value('percent_drop', thresholds.get('percent_drop', 20))} versus the prior run.",
        "high_volatility_skus": f"Volatile SKUs exceeded your CV threshold of {_format_threshold_value('volatility_cv', thresholds.get('volatility_cv', 1.2))} and also show margin risk.",
    }
    detail_rows = list(base_details)
    if type_key == "data_freshness_sla":
        max_staleness_days = _int_value(thresholds.get("max_staleness_days"), 1, minimum=0, maximum=30)
        detail_rows.extend(
            [
                {"label": "Max fact date", "value": str((date.today() - timedelta(days=max_staleness_days + 2)).isoformat())},
                {"label": "Age", "value": f"{max_staleness_days + 2} day(s)"},
                {"label": "Configured SLA", "value": f"{max_staleness_days} day(s)"},
            ]
        )
    else:
        detail_rows.extend(_threshold_summary_rows(thresholds))
    return {
        "summary": summary_map.get(type_key, "This is a sample notification using your current alert settings."),
        "status": "sample",
        "details": detail_rows,
        "sample_only": True,
    }


def _user_pref_payload(
    type_key: str,
    defaults: Mapping[str, Any],
    pref: Optional[UserNotificationPreference],
    user: Any,
) -> Dict[str, Any]:
    pref_config = _safe_json_loads(getattr(pref, "config_json", None), {})
    thresholds = _merged_thresholds(defaults, pref_config)
    scope_mode = _normalize_scope_mode(pref_config.get("scope_mode") or defaults.get("scope_mode"), user=user, default="rbac")
    delivery = pref_config.get("delivery") if isinstance(pref_config.get("delivery"), list) else list(defaults.get("delivery") or ["email"])
    enabled = bool(pref.enabled) if pref is not None else _as_bool(defaults.get("enabled"), False)
    frequency = _normalize_frequency(getattr(pref, "frequency", None) if pref is not None else defaults.get("frequency"), "daily")
    rollout_state = str(defaults.get("rollout_state") or "planned").strip().lower()
    return {
        "enabled": bool(enabled),
        "frequency": frequency,
        "scope_mode": scope_mode,
        "scope_label": _scope_mode_label(scope_mode),
        "delivery": delivery or ["email"],
        "thresholds": thresholds,
        "threshold_fields": _threshold_inputs_for_type(type_key, thresholds),
        "threshold_summary": _threshold_summary_rows(thresholds),
        "rollout_state": rollout_state,
        "is_live": rollout_state == "active",
        "runner_supported": type_key in _RUNNER_SUPPORTED_KEYS,
        "category": str(defaults.get("category") or "Other"),
        "admin_only": bool(defaults.get("admin_only")),
        "supports_test_email": True,
    }


def ensure_notification_types_seeded() -> None:
    now = _utcnow()
    with SessionLocal() as s:
        existing = {
            str(row.key).strip(): row
            for row in s.query(NotificationType).all()
        }
        changed = False
        for item in NOTIFICATION_CATALOG:
            type_key = str(item.get("key") or "").strip()
            if not type_key:
                continue
            row = existing.get(type_key)
            defaults = _safe_json_dumps(item.get("default_config") or {})
            if row is None:
                row = NotificationType(
                    key=type_key,
                    name=str(item.get("name") or type_key),
                    description=str(item.get("description") or "").strip() or None,
                    default_config_json=defaults,
                    created_at=now,
                    updated_at=now,
                )
                s.add(row)
                changed = True
                continue
            row_changed = False
            expected_name = str(item.get("name") or type_key)
            expected_desc = str(item.get("description") or "").strip() or None
            if (row.name or "") != expected_name:
                row.name = expected_name
                row_changed = True
            if (row.description or None) != expected_desc:
                row.description = expected_desc
                row_changed = True
            if (row.default_config_json or "") != defaults:
                row.default_config_json = defaults
                row_changed = True
            if row_changed:
                row.updated_at = now
                s.add(row)
                changed = True
        if changed:
            s.commit()


def notifications_enabled() -> bool:
    return _as_bool(current_app.config.get("NOTIFICATIONS_ENABLED"), False)


def admin_defaults_enabled() -> bool:
    return _as_bool(current_app.config.get("ADMIN_NOTIF_DEFAULTS"), False)


def list_notification_types() -> List[NotificationType]:
    ensure_notification_types_seeded()
    with SessionLocal() as s:
        rows = s.query(NotificationType).order_by(NotificationType.name.asc()).all()
        for row in rows:
            s.expunge(row)
        return rows


def list_user_notification_preferences(user_id: int) -> Dict[str, UserNotificationPreference]:
    with SessionLocal() as s:
        rows = (
            s.query(UserNotificationPreference)
            .filter(UserNotificationPreference.user_id == int(user_id))
            .all()
        )
        for row in rows:
            s.expunge(row)
        return {str(row.type_key).strip(): row for row in rows}


def get_notification_settings_for_user(user: Any) -> Dict[str, Any]:
    rows = list_notification_types()
    prefs = list_user_notification_preferences(int(getattr(user, "id", 0) or 0))
    categories = catalog_by_category()
    sections: List[Dict[str, Any]] = []
    by_key: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        defaults = _safe_json_loads(getattr(row, "default_config_json", None), _catalog_defaults(str(row.key)))
        merged = _user_pref_payload(str(row.key), defaults, prefs.get(str(row.key)), user)
        if merged["admin_only"] and str(getattr(user, "role", "") or "").strip().lower() not in {"admin", "owner", "gm", "sales_manager"}:
            continue
        item = {
            "key": str(row.key),
            "name": row.name,
            "description": row.description or "",
            **merged,
        }
        by_key[item["key"]] = item
    for category_name, catalog_items in categories.items():
        items: List[Dict[str, Any]] = []
        for catalog_item in catalog_items:
            key = str(catalog_item.get("key") or "").strip()
            state = by_key.get(key)
            if state:
                items.append(state)
        if items:
            sections.append({"name": category_name, "items": items})
    return {"sections": sections, "by_key": by_key}


def notification_setting_for_user(user: Any, type_key: str) -> Optional[Dict[str, Any]]:
    settings = get_notification_settings_for_user(user)
    return settings["by_key"].get(str(type_key or "").strip())


def _notification_config_from_form(type_key: str, form: Mapping[str, Any], user: Any) -> Dict[str, Any]:
    defaults = _catalog_defaults(type_key)
    thresholds = dict(defaults.get("config") or {})
    for threshold_key, default_value in list(thresholds.items()):
        thresholds[threshold_key] = _coerce_threshold_value(
            threshold_key,
            form.get(f"{type_key}_{threshold_key}"),
            default_value,
        )
    scope_mode = _normalize_scope_mode(form.get(f"{type_key}_scope_mode"), user=user, default=str(defaults.get("scope_mode") or "rbac"))
    return {
        "scope_mode": scope_mode,
        "delivery": ["email"],
        "thresholds": thresholds,
    }


def save_notification_preferences(user: Any, form: Mapping[str, Any], *, type_keys: Optional[Sequence[str]] = None) -> int:
    ensure_notification_types_seeded()
    type_rows = {str(row.key): row for row in list_notification_types()}
    allowed_keys = set(get_notification_settings_for_user(user)["by_key"].keys())
    if type_keys:
        candidate_keys = [str(key or "").strip() for key in type_keys]
    else:
        explicit_type_key = str(form.get("type_key") or "").strip() if hasattr(form, "get") else ""
        if explicit_type_key:
            candidate_keys = [explicit_type_key]
        else:
            suffixes = (
                "_enabled",
                "_frequency",
                "_scope_mode",
                "_cooldown_hours",
                "_max_staleness_days",
            )
            derived: List[str] = []
            seen: set[str] = set()
            for raw_key in form.keys():
                token = str(raw_key or "").strip()
                for suffix in suffixes:
                    if not token.endswith(suffix):
                        continue
                    prefix = token[: -len(suffix)]
                    if prefix and prefix in allowed_keys and prefix not in seen:
                        seen.add(prefix)
                        derived.append(prefix)
                    break
            candidate_keys = derived
    now = _utcnow()
    saved = 0
    with SessionLocal() as s:
        for type_key in candidate_keys:
            if not type_key or type_key not in allowed_keys:
                continue
            row = type_rows.get(type_key)
            if row is None:
                continue
            defaults = _safe_json_loads(row.default_config_json, _catalog_defaults(type_key))
            frequency = _normalize_frequency(form.get(f"{type_key}_frequency"), str(defaults.get("frequency") or "daily"))
            enabled = _as_bool(form.get(f"{type_key}_enabled"), _as_bool(defaults.get("enabled"), False))
            config_json = _safe_json_dumps(_notification_config_from_form(type_key, form, user))
            pref = (
                s.query(UserNotificationPreference)
                .filter(
                    UserNotificationPreference.user_id == int(user.id),
                    UserNotificationPreference.type_key == type_key,
                )
                .first()
            )
            if pref is None:
                pref = UserNotificationPreference(
                    user_id=int(user.id),
                    type_key=type_key,
                    enabled=1 if enabled else 0,
                    frequency=frequency,
                    config_json=config_json,
                    created_at=now,
                    updated_at=now,
                )
            else:
                pref.enabled = 1 if enabled else 0
                pref.frequency = frequency
                pref.config_json = config_json
                pref.updated_at = now
            s.add(pref)
            saved += 1
        s.commit()
    return saved


def _as_public_url(path: str) -> str:
    path = path if path.startswith("/") else f"/{path}"
    base = str(current_app.config.get("APP_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if base:
        return f"{base}{path}"
    try:
        return url_for("pages.home", _external=True).rstrip("/") + path
    except Exception:
        return path


def notifications_manage_url() -> str:
    try:
        return _as_public_url(url_for("notifications.index", _external=False))
    except Exception:
        return _as_public_url("/notifications")


def _alert_details_url(type_key: str) -> str:
    if type_key == "data_freshness_sla":
        try:
            return _as_public_url(url_for("overview_page.overview_landing", _external=False))
        except Exception:
            return _as_public_url("/")
    return notifications_manage_url()


def _xlsx_attachment_filename(stem: str) -> str:
    safe = sanitize_filename(stem, default="alerts")
    if not safe.lower().endswith(".xlsx"):
        safe = f"{safe}.xlsx"
    return safe


def _make_xlsx_attachment(filename: str, sheets: Mapping[str, pd.DataFrame]) -> List[Dict[str, Any]]:
    clean_sheets: Dict[str, pd.DataFrame] = {}
    for name, frame in sheets.items():
        if not name:
            continue
        clean_sheets[str(name)] = frame if frame is not None else pd.DataFrame()
    if not clean_sheets:
        return []
    if not xlsx_export_available():
        current_app.logger.warning(
            "notifications.attachment_unavailable",
            extra={"attachment_name": filename, "sheet_names": list(clean_sheets.keys())},
        )
        return []
    try:
        payload = dataframes_to_xlsx_bytes(clean_sheets)
    except Exception:
        current_app.logger.exception(
            "notifications.attachment_build_failed",
            extra={"attachment_name": filename, "sheet_names": list(clean_sheets.keys())},
        )
        return []
    return [{"filename": filename, "mimetype": _XLSX_MIMETYPE, "data": payload}]


def _build_single_alert_attachments(type_key: str, payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    type_row = _type_row(type_key)
    alert_name = type_row.name if type_row is not None else str(type_key).replace("_", " ").title()
    summary_rows: List[Dict[str, Any]] = [
        {"Field": "Alert", "Value": alert_name},
        {"Field": "Summary", "Value": str(payload.get("summary") or "")},
        {"Field": "Status", "Value": str(payload.get("status") or "pending")},
        {"Field": "Generated At", "Value": _utcnow().isoformat()},
        {"Field": "Related Page", "Value": _alert_details_url(type_key)},
        {"Field": "Manage Alerts", "Value": notifications_manage_url()},
    ]
    for key, label in (
        ("max_fact_date", "Max Fact Date"),
        ("age_days", "Age Days"),
        ("threshold_days", "Threshold Days"),
        ("built_at", "Built At"),
        ("dataset_version", "Dataset Version"),
    ):
        value = payload.get(key)
        if value not in (None, ""):
            summary_rows.append({"Field": label, "Value": value})

    sheets: Dict[str, pd.DataFrame] = {"Summary": pd.DataFrame(summary_rows)}

    details = payload.get("details")
    if isinstance(details, Sequence) and not isinstance(details, (str, bytes, bytearray)):
        detail_rows = [
            {
                "Label": str((item or {}).get("label") or ""),
                "Value": str((item or {}).get("value") or ""),
            }
            for item in details
            if isinstance(item, Mapping)
        ]
        if detail_rows:
            sheets["Details"] = pd.DataFrame(detail_rows)

    scope_snapshot = payload.get("scope_snapshot")
    if isinstance(scope_snapshot, Mapping):
        scope_rows: List[Dict[str, str]] = []
        for key, value in scope_snapshot.items():
            if isinstance(value, (list, tuple, set)):
                rendered = ", ".join(str(v) for v in value)
            else:
                rendered = str(value)
            scope_rows.append({"Field": _humanize_threshold_label(str(key)), "Value": rendered})
        if scope_rows:
            sheets["Scope"] = pd.DataFrame(scope_rows)

    stem = f"wholesale_alert_{type_key}"
    return _make_xlsx_attachment(_xlsx_attachment_filename(stem), sheets)


def _build_digest_attachments(frequency: str, items: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in items:
        rows.append(
            {
                "Alert": str(item.get("alert_name") or ""),
                "Summary": str(item.get("summary") or ""),
                "Details URL": str(item.get("details_url") or ""),
                "Manage URL": str(item.get("manage_url") or ""),
                "Type Key": str(item.get("type_key") or ""),
            }
        )
    if not rows:
        return []
    sheet_name = "Daily Alerts" if frequency == "daily" else "Weekly Alerts"
    stem = f"wholesale_alerts_{frequency}_digest"
    return _make_xlsx_attachment(_xlsx_attachment_filename(stem), {sheet_name: pd.DataFrame(rows)})


def render_single_alert_email(user: Any, type_key: str, payload: Mapping[str, Any]) -> tuple[str, str, str]:
    type_row = _type_row(type_key)
    alert_name = type_row.name if type_row is not None else str(type_key).replace("_", " ").title()
    subject = f"Northgate Retail Analytics Alert: {alert_name}"
    context = {
        "recipient_name": _full_name(user),
        "alert_name": alert_name,
        "payload": dict(payload),
        "manage_url": notifications_manage_url(),
        "details_url": _alert_details_url(type_key),
    }
    text_body = render_template("emails/alert_single.txt", **context)
    html_body = render_template("emails/alert_single.html", **context)
    return subject, text_body, html_body


def render_digest_email(user: Any, frequency: str, items: Sequence[Mapping[str, Any]]) -> tuple[str, str, str]:
    digest_label = "Daily digest" if frequency == "daily" else "Weekly digest"
    subject = f"Northgate Retail Analytics Alerts: {digest_label}"
    normalized_items = [dict(item) for item in items]
    context = {
        "recipient_name": _full_name(user),
        "digest_label": digest_label,
        "items": normalized_items,
        "manage_url": notifications_manage_url(),
    }
    text_body = render_template("emails/digest_daily.txt", **context)
    html_body = render_template("emails/digest_daily.html", **context)
    return subject, text_body, html_body


def send_test_email_for_user(user: Any, type_key: str, *, override_email: Optional[str] = None) -> bool:
    type_key = str(type_key or "").strip()
    settings = get_notification_settings_for_user(user)["by_key"].get(type_key)
    if not settings:
        return False
    payload = _sample_alert_payload(type_key, settings)
    subject, text_body, html_body = render_single_alert_email(user, type_key, payload)
    target_email = str(override_email or getattr(user, "email", "") or "").strip()
    if not target_email:
        return False
    attachments = _build_single_alert_attachments(type_key, payload)
    return send_email(
        target_email,
        subject,
        text_body,
        html_body=html_body,
        attachments=attachments,
        raise_on_error=False,
    )


def send_test_emails_for_user(
    user: Any,
    *,
    type_keys: Optional[Sequence[str]] = None,
    immediate_only: bool = False,
    override_email: Optional[str] = None,
) -> int:
    settings = get_notification_settings_for_user(user)["by_key"]
    keys: List[str]
    if type_keys is not None:
        keys = [str(key or "").strip() for key in type_keys if str(key or "").strip() in settings]
    else:
        keys = list(settings.keys())
    sent = 0
    for type_key in keys:
        pref_state = settings.get(type_key) or {}
        if immediate_only and _normalize_frequency(pref_state.get("frequency"), "daily") != "immediate":
            continue
        if send_test_email_for_user(user, type_key, override_email=override_email):
            sent += 1
    return sent


def _type_row(type_key: str) -> Optional[NotificationType]:
    with SessionLocal() as s:
        row = (
            s.query(NotificationType)
            .filter(NotificationType.key == str(type_key).strip())
            .first()
        )
        if row:
            s.expunge(row)
        return row


def _event_to_dict(event: NotificationEvent) -> Dict[str, Any]:
    return {
        "id": int(event.id),
        "type_key": str(event.type_key),
        "user_id": int(event.user_id),
        "event_hash": str(event.event_hash),
        "payload": _safe_json_loads(event.event_payload_json, {}),
        "window_start": event.window_start,
        "window_end": event.window_end,
        "created_at": event.created_at,
        "sent_at": event.sent_at,
        "status": str(event.status or "pending"),
        "error": event.error,
    }


def create_notification_event(
    *,
    type_key: str,
    user_id: int,
    event_hash: str,
    payload: Mapping[str, Any],
    window_start: Optional[datetime],
    window_end: Optional[datetime],
) -> tuple[Dict[str, Any], bool]:
    now = _utcnow()
    with SessionLocal() as s:
        existing = (
            s.query(NotificationEvent)
            .filter(
                NotificationEvent.type_key == str(type_key).strip(),
                NotificationEvent.user_id == int(user_id),
                NotificationEvent.event_hash == str(event_hash).strip(),
            )
            .first()
        )
        if existing is not None:
            s.expunge(existing)
            return _event_to_dict(existing), False
        row = NotificationEvent(
            type_key=str(type_key).strip(),
            user_id=int(user_id),
            event_hash=str(event_hash).strip(),
            event_payload_json=_safe_json_dumps(payload),
            window_start=window_start,
            window_end=window_end,
            created_at=now,
            status="pending",
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        s.expunge(row)
        return _event_to_dict(row), True


def _mark_events(event_ids: Sequence[int], *, status: str, error: Optional[str] = None, sent: bool = False) -> None:
    if not event_ids:
        return
    now = _utcnow()
    with SessionLocal() as s:
        rows = s.query(NotificationEvent).filter(NotificationEvent.id.in_([int(v) for v in event_ids])).all()
        for row in rows:
            row.status = status
            row.error = error
            if sent:
                row.sent_at = now
            s.add(row)
        s.commit()


def _emails_sent_last_hour(user_id: int, now: datetime) -> int:
    cutoff = now - timedelta(hours=1)
    with SessionLocal() as s:
        return int(
            s.query(NotificationEvent)
            .filter(
                NotificationEvent.user_id == int(user_id),
                NotificationEvent.status == "sent",
                NotificationEvent.sent_at.isnot(None),
                NotificationEvent.sent_at >= cutoff,
            )
            .count()
        )


def _max_emails_per_hour() -> int:
    return _int_value(current_app.config.get("NOTIFICATIONS_MAX_EMAILS_PER_HOUR"), 10, minimum=1, maximum=100)


def _digest_due(user_id: int, type_keys: Sequence[str], frequency: str, now: datetime) -> bool:
    wait = _DIGEST_WAIT.get(frequency)
    if wait is None:
        return True
    if not type_keys:
        return False
    with SessionLocal() as s:
        row = (
            s.query(NotificationEvent.sent_at)
            .filter(
                NotificationEvent.user_id == int(user_id),
                NotificationEvent.type_key.in_([str(k).strip() for k in type_keys]),
                NotificationEvent.status == "sent",
                NotificationEvent.sent_at.isnot(None),
            )
            .order_by(NotificationEvent.sent_at.desc())
            .first()
        )
    last_sent = row[0] if row and row[0] else None
    if last_sent is None:
        return True
    return last_sent <= (now - wait)


def _resolve_scope_snapshot(user: Any, pref_state: Mapping[str, Any]) -> Dict[str, Any]:
    scope = scope_for_user(user, use_cache=True)
    payload = scope.as_dict(include_allowed=True)
    mode = _normalize_scope_mode(pref_state.get("scope_mode"), user=user, default="rbac")
    user_rep = str(getattr(user, "erp_user_id", None) or getattr(user, "sales_rep_id", None) or "").strip().lower()
    role = str(getattr(user, "role", "") or "").strip().lower()
    admin_like = role in {"admin", "owner", "gm"}
    if mode == "self" and user_rep and not admin_like:
        payload["allowed_erp_user_ids"] = [user_rep]
        payload["sales_rep_ids"] = [user_rep]
        payload["scope_mode"] = "list"
    payload["notification_scope_mode"] = mode
    return payload


def _parse_manifest_date(raw: Any) -> Optional[date]:
    if raw in (None, ""):
        return None
    try:
        ts = pd.to_datetime(raw, errors="coerce")
    except Exception:
        return None
    if pd.isna(ts):
        return None
    try:
        return ts.date()
    except Exception:
        return None


def _evaluate_data_freshness(user: Any, pref_state: Mapping[str, Any], now: datetime) -> List[Dict[str, Any]]:
    thresholds = dict(pref_state.get("thresholds") or {})
    max_staleness_days = _int_value(thresholds.get("max_staleness_days"), 1, minimum=0, maximum=30)
    manifest = fact_store.get_meta() or {}
    max_fact_date = (
        _parse_manifest_date(manifest.get("watermark"))
        or _parse_manifest_date(manifest.get("watermark_dt"))
        or _parse_manifest_date(manifest.get("last_refresh_utc"))
        or _parse_manifest_date(manifest.get("built_at"))
    )
    if max_fact_date is None:
        try:
            watermark = fact_store.get_watermark()
        except Exception:
            watermark = None
        if watermark is not None:
            try:
                max_fact_date = watermark.date()
            except Exception:
                max_fact_date = None
    if max_fact_date is None:
        return []
    age_days = max(0, (now.date() - max_fact_date).days)
    if age_days <= max_staleness_days:
        return []
    scope_snapshot = _resolve_scope_snapshot(user, pref_state)
    payload = {
        "summary": f"Fact data is {age_days} day(s) old and exceeds your {max_staleness_days}-day SLA.",
        "status": "stale",
        "max_fact_date": max_fact_date.isoformat(),
        "age_days": age_days,
        "threshold_days": max_staleness_days,
        "built_at": manifest.get("built_at_utc") or manifest.get("built_at") or manifest.get("last_refresh_utc"),
        "dataset_version": manifest.get("dataset_version") or manifest.get("version"),
        "scope_snapshot": {
            "scope_mode": scope_snapshot.get("scope_mode"),
            "notification_scope_mode": scope_snapshot.get("notification_scope_mode"),
            "allowed_erp_user_ids": list(scope_snapshot.get("allowed_erp_user_ids") or []),
            "allowed_customer_ids": list(scope_snapshot.get("allowed_customer_ids") or []),
        },
        "details": [
            {"label": "Max fact date", "value": max_fact_date.isoformat()},
            {"label": "Age", "value": f"{age_days} day(s)"},
            {"label": "Configured SLA", "value": f"{max_staleness_days} day(s)"},
        ],
    }
    event_basis = {
        "type_key": "data_freshness_sla",
        "user_id": int(user.id),
        "max_fact_date": max_fact_date.isoformat(),
        "threshold_days": max_staleness_days,
        "notification_scope_mode": scope_snapshot.get("notification_scope_mode"),
    }
    event_hash = hashlib.sha256(_safe_json_dumps(event_basis).encode("utf-8")).hexdigest()
    window_start = datetime.combine(max_fact_date, time.min, tzinfo=timezone.utc)
    return [
        {
            "type_key": "data_freshness_sla",
            "user_id": int(user.id),
            "event_hash": event_hash,
            "payload": payload,
            "window_start": window_start,
            "window_end": now,
        }
    ]


def _subscription_candidates(now: datetime) -> List[Dict[str, Any]]:
    ensure_notification_types_seeded()
    users: List[User] = []
    with SessionLocal() as s:
        rows = (
            s.query(User)
            .filter(
                User.is_active == True,  # noqa: E712
                User.is_approved == True,  # noqa: E712
                User.email.isnot(None),
            )
            .order_by(User.id.asc())
            .all()
        )
        for row in rows:
            s.expunge(row)
        users = rows

    candidates: List[Dict[str, Any]] = []
    live_keys = set(active_alert_keys())
    for user in users:
        email = str(getattr(user, "email", "") or "").strip()
        if not email or not _has_notifications_access(user):
            continue
        settings = get_notification_settings_for_user(user)["by_key"]
        for type_key, pref_state in settings.items():
            if type_key not in live_keys:
                continue
            if not pref_state.get("enabled"):
                continue
            if type_key == "data_freshness_sla":
                for event_payload in _evaluate_data_freshness(user, pref_state, now):
                    candidates.append(
                        {
                            "user": user,
                            "type_key": type_key,
                            "pref_state": dict(pref_state),
                            **event_payload,
                        }
                    )
    return candidates


def _pending_events() -> Dict[int, List[Dict[str, Any]]]:
    with SessionLocal() as s:
        rows = (
            s.query(NotificationEvent)
            .filter(
                NotificationEvent.status == "pending",
                NotificationEvent.sent_at.is_(None),
            )
            .order_by(NotificationEvent.created_at.asc(), NotificationEvent.id.asc())
            .all()
        )
        for row in rows:
            s.expunge(row)
    grouped: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[int(row.user_id)].append(_event_to_dict(row))
    return grouped


def _load_user(user_id: int) -> Optional[User]:
    with SessionLocal() as s:
        user = s.get(User, int(user_id))
        if user:
            s.expunge(user)
        return user


def _dispatch_pending_notifications(now: datetime, send_email_func: Callable[..., bool]) -> Dict[str, int]:
    stats = {"emails_sent": 0, "email_failures": 0, "events_sent": 0}
    pending_by_user = _pending_events()
    for user_id, events in pending_by_user.items():
        user = _load_user(user_id)
        if user is None:
            continue
        settings = get_notification_settings_for_user(user)["by_key"]
        by_frequency: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for event in events:
            pref_state = settings.get(event["type_key"])
            if not pref_state or not pref_state.get("enabled"):
                continue
            frequency = _normalize_frequency(pref_state.get("frequency"), "daily")
            event["frequency"] = frequency
            by_frequency[frequency].append(event)

        for frequency, bucket in by_frequency.items():
            if not bucket:
                continue
            if _emails_sent_last_hour(user_id, now) >= _max_emails_per_hour():
                continue
            if frequency == "immediate":
                for event in bucket:
                    subject, text_body, html_body = render_single_alert_email(user, event["type_key"], event["payload"])
                    attachments = _build_single_alert_attachments(event["type_key"], event["payload"])
                    ok = send_email_func(
                        str(getattr(user, "email", "") or "").strip(),
                        subject,
                        text_body,
                        html_body=html_body,
                        attachments=attachments,
                        raise_on_error=False,
                    )
                    if ok:
                        _mark_events([event["id"]], status="sent", sent=True)
                        stats["emails_sent"] += 1
                        stats["events_sent"] += 1
                    else:
                        _mark_events([event["id"]], status="error", error="send_failed", sent=False)
                        stats["email_failures"] += 1
                continue

            type_keys = [str(event["type_key"]) for event in bucket]
            if not _digest_due(user_id, type_keys, frequency, now):
                continue
            items: List[Dict[str, Any]] = []
            for event in bucket:
                type_row = _type_row(event["type_key"])
                items.append(
                    {
                        "type_key": event["type_key"],
                        "alert_name": type_row.name if type_row is not None else str(event["type_key"]).replace("_", " ").title(),
                        "summary": str((event.get("payload") or {}).get("summary") or ""),
                        "details_url": _alert_details_url(event["type_key"]),
                        "manage_url": notifications_manage_url(),
                    }
                )
            subject, text_body, html_body = render_digest_email(user, frequency, items)
            attachments = _build_digest_attachments(frequency, items)
            ok = send_email_func(
                str(getattr(user, "email", "") or "").strip(),
                subject,
                text_body,
                html_body=html_body,
                attachments=attachments,
                raise_on_error=False,
            )
            event_ids = [int(event["id"]) for event in bucket]
            if ok:
                _mark_events(event_ids, status="sent", sent=True)
                stats["emails_sent"] += 1
                stats["events_sent"] += len(event_ids)
            else:
                _mark_events(event_ids, status="error", error="send_failed", sent=False)
                stats["email_failures"] += 1
    return stats


def run_notification_cycle(
    *,
    now: Optional[datetime] = None,
    send_email_func: Callable[..., bool] = send_email,
) -> Dict[str, Any]:
    now = now or _utcnow()
    stats: Dict[str, Any] = {
        "feature_enabled": notifications_enabled(),
        "evaluated": 0,
        "events_created": 0,
        "events_deduped": 0,
        "emails_sent": 0,
        "email_failures": 0,
        "events_sent": 0,
    }
    if not notifications_enabled():
        current_app.logger.info("notifications.runner_skipped", extra={"reason": "feature_disabled"})
        return stats

    candidates = _subscription_candidates(now)
    stats["evaluated"] = len(candidates)
    for candidate in candidates:
        event, created = create_notification_event(
            type_key=str(candidate["type_key"]),
            user_id=int(candidate["user_id"]),
            event_hash=str(candidate["event_hash"]),
            payload=candidate["payload"],
            window_start=candidate.get("window_start"),
            window_end=candidate.get("window_end"),
        )
        if created:
            stats["events_created"] += 1
        else:
            stats["events_deduped"] += 1
        current_app.logger.info(
            "notifications.event_evaluated",
            extra={
                "user_id": int(candidate["user_id"]),
                "type_key": str(candidate["type_key"]),
                "event_created": created,
                "event_hash": event["event_hash"],
            },
        )

    dispatch_stats = _dispatch_pending_notifications(now, send_email_func)
    stats.update(dispatch_stats)
    current_app.logger.info("notifications.runner_complete", extra=stats)
    return stats
