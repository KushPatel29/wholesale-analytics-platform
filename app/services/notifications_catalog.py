from __future__ import annotations

from typing import Any, Dict, List


AlertDefinition = Dict[str, Any]


NOTIFICATION_CATALOG: List[AlertDefinition] = [
    {
        "key": "customer_revenue_drop",
        "name": "Customer revenue drop",
        "description": "Alert when a customer's month-over-month revenue drops materially.",
        "default_config": {
            "category": "Revenue & Profit",
            "rollout_state": "planned",
            "admin_only": False,
            "enabled": False,
            "frequency": "daily",
            "scope_mode": "rbac",
            "delivery": ["email"],
            "config": {"percent_drop": 30, "min_dollar_impact": 5000, "lookback_months": 1, "cooldown_hours": 24},
        },
    },
    {
        "key": "customer_profit_drop",
        "name": "Customer profit drop",
        "description": "Alert when a customer's month-over-month profit drops materially.",
        "default_config": {
            "category": "Revenue & Profit",
            "rollout_state": "planned",
            "admin_only": False,
            "enabled": False,
            "frequency": "daily",
            "scope_mode": "rbac",
            "delivery": ["email"],
            "config": {"percent_drop": 25, "min_dollar_impact": 2500, "lookback_months": 1, "cooldown_hours": 24},
        },
    },
    {
        "key": "product_margin_below_target",
        "name": "Product margin below target",
        "description": "Alert when products miss the target margin and still carry meaningful revenue.",
        "default_config": {
            "category": "Margin & Pricing",
            "rollout_state": "planned",
            "admin_only": False,
            "enabled": False,
            "frequency": "daily",
            "scope_mode": "rbac",
            "delivery": ["email"],
            "config": {"min_revenue": 5000, "cooldown_hours": 24},
        },
    },
    {
        "key": "negative_margin_products",
        "name": "Negative margin products sold",
        "description": "Alert when products sold below cost appear in the current window.",
        "default_config": {
            "category": "Margin & Pricing",
            "rollout_state": "planned",
            "admin_only": False,
            "enabled": False,
            "frequency": "immediate",
            "scope_mode": "rbac",
            "delivery": ["email"],
            "config": {"top_n": 10, "cooldown_hours": 12},
        },
    },
    {
        "key": "new_large_customer_gain",
        "name": "New large customer gain",
        "description": "Alert on unusually large new customer gains compared with the prior month.",
        "default_config": {
            "category": "Revenue & Profit",
            "rollout_state": "planned",
            "admin_only": False,
            "enabled": False,
            "frequency": "daily",
            "scope_mode": "rbac",
            "delivery": ["email"],
            "config": {"min_revenue_gain": 10000, "percent_gain": 20, "cooldown_hours": 24},
        },
    },
    {
        "key": "concentration_risk_increase",
        "name": "Concentration risk increase",
        "description": "Alert when customer concentration rises above configured share or HHI thresholds.",
        "default_config": {
            "category": "Revenue & Profit",
            "rollout_state": "planned",
            "admin_only": False,
            "enabled": False,
            "frequency": "weekly",
            "scope_mode": "rbac",
            "delivery": ["email"],
            "config": {"top1_share_pct": 35, "top5_share_pct": 70, "hhi_threshold": 1800, "cooldown_hours": 72},
        },
    },
    {
        "key": "data_freshness_sla",
        "name": "Data freshness SLA",
        "description": "Alert when the fact dataset is older than the configured freshness SLA.",
        "default_config": {
            "category": "Data Quality",
            "rollout_state": "active",
            "admin_only": False,
            "enabled": False,
            "frequency": "immediate",
            "scope_mode": "rbac",
            "delivery": ["email"],
            "config": {"max_staleness_days": 1, "cooldown_hours": 12},
        },
    },
    {
        "key": "cost_coverage_drop",
        "name": "Cost coverage drop",
        "description": "Alert when cost coverage falls below the configured threshold.",
        "default_config": {
            "category": "Data Quality",
            "rollout_state": "planned",
            "admin_only": False,
            "enabled": False,
            "frequency": "daily",
            "scope_mode": "rbac",
            "delivery": ["email"],
            "config": {"min_cost_coverage_pct": 95, "cooldown_hours": 24},
        },
    },
    {
        "key": "duplicate_integrity_issue",
        "name": "Duplicate / integrity issue",
        "description": "Alert when duplicate groups or integrity exceptions are detected in the dataset.",
        "default_config": {
            "category": "Data Quality",
            "rollout_state": "planned",
            "admin_only": True,
            "enabled": False,
            "frequency": "immediate",
            "scope_mode": "rbac",
            "delivery": ["email"],
            "config": {"cooldown_hours": 12},
        },
    },
    {
        "key": "at_risk_customers",
        "name": "At-risk customers",
        "description": "Alert on customers trending toward churn based on inactivity.",
        "default_config": {
            "category": "Customer Health",
            "rollout_state": "planned",
            "admin_only": False,
            "enabled": False,
            "frequency": "daily",
            "scope_mode": "rbac",
            "delivery": ["email"],
            "config": {"at_risk_days": 30, "top_n": 10, "cooldown_hours": 24},
        },
    },
    {
        "key": "newly_churned_customers",
        "name": "Newly churned customers",
        "description": "Alert when customers cross the churn threshold.",
        "default_config": {
            "category": "Customer Health",
            "rollout_state": "planned",
            "admin_only": False,
            "enabled": False,
            "frequency": "daily",
            "scope_mode": "rbac",
            "delivery": ["email"],
            "config": {"churn_days": 45, "top_n": 10, "cooldown_hours": 24},
        },
    },
    {
        "key": "reactivated_customers",
        "name": "Reactivated customers",
        "description": "Alert when previously churned customers return.",
        "default_config": {
            "category": "Customer Health",
            "rollout_state": "planned",
            "admin_only": False,
            "enabled": False,
            "frequency": "weekly",
            "scope_mode": "rbac",
            "delivery": ["email"],
            "config": {"churn_days": 45, "cooldown_hours": 72},
        },
    },
    {
        "key": "forecast_drop_anomaly",
        "name": "Forecast anomaly",
        "description": "Alert when the next forecast drops sharply versus the prior forecast.",
        "default_config": {
            "category": "Forecast",
            "rollout_state": "planned",
            "admin_only": False,
            "enabled": False,
            "frequency": "weekly",
            "scope_mode": "rbac",
            "delivery": ["email"],
            "config": {"percent_drop": 20, "cooldown_hours": 72},
        },
    },
    {
        "key": "high_volatility_skus",
        "name": "High volatility SKUs",
        "description": "Alert on volatile SKUs that also carry margin risk.",
        "default_config": {
            "category": "Forecast",
            "rollout_state": "planned",
            "admin_only": False,
            "enabled": False,
            "frequency": "weekly",
            "scope_mode": "rbac",
            "delivery": ["email"],
            "config": {"volatility_cv": 1.2, "cooldown_hours": 72},
        },
    },
]


def catalog_index() -> Dict[str, AlertDefinition]:
    return {str(item["key"]).strip(): dict(item) for item in NOTIFICATION_CATALOG}


def active_alert_keys() -> List[str]:
    return [str(item["key"]).strip() for item in NOTIFICATION_CATALOG if (item.get("default_config") or {}).get("rollout_state") == "active"]


def catalog_by_category() -> Dict[str, List[AlertDefinition]]:
    grouped: Dict[str, List[AlertDefinition]] = {}
    for item in NOTIFICATION_CATALOG:
        config = item.get("default_config") or {}
        category = str(config.get("category") or "Other").strip() or "Other"
        grouped.setdefault(category, []).append(dict(item))
    return grouped
