"""Canonical permission registry and role mappings for auth/RBAC."""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Sequence, Set


ALLOWED_ROLES: Set[str] = {
    "sales",
    "sales_manager",
    "manager",
    "warehouse",
    "production",
    "gm",
    "owner",
    "admin",
    "analyst",
    "viewer",
    "returns_only",
}


ROLE_ALIASES: Dict[str, str] = {
    "manager": "sales_manager",
    "general_manager": "gm",
    "general manager": "gm",
    "administrator": "admin",
}


SYSTEM_ROLE_DESCRIPTIONS: Dict[str, str] = {
    "admin": "Full system administration",
    "owner": "Executive ownership access",
    "gm": "General manager access",
    "sales_manager": "Sales manager access",
    "manager": "Manager access (alias of sales_manager)",
    "sales": "Sales representative access",
    "warehouse": "Warehouse operations access",
    "production": "Production operations access",
    "analyst": "Analyst access",
    "viewer": "Read-only access",
    "returns_only": "Returns-only portal access",
}


def _perm(
    key: str,
    label: str,
    group: str,
    *,
    module: str,
    description: str,
    aliases: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "key": str(key).strip().lower(),
        "label": label,
        "group": group,
        "module": module,
        "description": description,
        "aliases": tuple(str(alias).strip().lower() for alias in aliases if str(alias).strip()),
    }


_PERMISSION_DEFINITIONS: list[dict[str, Any]] = [
    _perm("page.overview.view", "Overview", "Page Access", module="Overview", description="View the Overview page"),
    _perm("page.customers.view", "Customers", "Page Access", module="Customers", description="View the Customers page"),
    _perm("page.products.view", "Products", "Page Access", module="Products", description="View the Products page"),
    _perm("page.regions.view", "Regions", "Page Access", module="Regions", description="View the Regions page"),
    _perm("page.suppliers.view", "Suppliers", "Page Access", module="Suppliers", description="View the Suppliers page"),
    _perm("page.labor.view", "Labor", "Page Access", module="Labor", description="View the Labor Intelligence page"),
    _perm("page.salesreps.view", "Sales Reps", "Page Access", module="Sales Reps", description="View the Sales Reps page"),
    _perm("page.returns.view", "Returns", "Page Access", module="Returns", description="View the Returns portal"),
    _perm(
        "page.admin.view",
        "Admin",
        "Page Access",
        module="Admin",
        description="Access the admin portal",
        aliases=("admin.portal.view",),
    ),
    _perm(
        "page.notifications.view",
        "Notifications",
        "Page Access",
        module="Notifications",
        description="View notifications and alert preferences",
    ),
    _perm(
        "page.customers.drilldown.view",
        "Customer Drilldown",
        "Drilldown Access",
        module="Customers",
        description="Open customer drilldown pages",
    ),
    _perm(
        "page.products.drilldown.view",
        "Product Drilldown",
        "Drilldown Access",
        module="Products",
        description="Open product drilldown pages",
    ),
    _perm(
        "page.regions.drilldown.view",
        "Region Drilldown",
        "Drilldown Access",
        module="Regions",
        description="Open region drilldown pages",
    ),
    _perm(
        "page.suppliers.drilldown.view",
        "Supplier Drilldown",
        "Drilldown Access",
        module="Suppliers",
        description="Open supplier drilldown pages",
    ),
    _perm(
        "page.salesreps.drilldown.view",
        "Sales Rep Drilldown",
        "Drilldown Access",
        module="Sales Reps",
        description="Open sales rep drilldown pages",
    ),
    _perm(
        "export.overview",
        "Overview Exports",
        "Export Access",
        module="Overview",
        description="Export Overview tables and detail datasets",
    ),
    _perm(
        "export.customers",
        "Customer Exports",
        "Export Access",
        module="Customers",
        description="Export customer tables, cohorts, CLV, and drilldowns",
    ),
    _perm(
        "export.products",
        "Product Exports",
        "Export Access",
        module="Products",
        description="Export product tables, movers, drilldowns, and execution lists",
        aliases=("export.products.csv",),
    ),
    _perm(
        "export.regions",
        "Region Exports",
        "Export Access",
        module="Regions",
        description="Export region overview and drilldown datasets",
    ),
    _perm(
        "export.suppliers",
        "Supplier Exports",
        "Export Access",
        module="Suppliers",
        description="Export supplier overview and drilldown datasets",
        aliases=("export.suppliers.csv",),
    ),
    _perm(
        "export.labor",
        "Labor Exports",
        "Export Access",
        module="Labor",
        description="Export labor snapshots, detail, department summaries, and watchlists",
    ),
    _perm(
        "export.salesreps",
        "Sales Rep Exports",
        "Export Access",
        module="Sales Reps",
        description="Export sales rep datasets",
        aliases=("export.salesrep.csv", "export.salesrep.xlsx"),
    ),
    _perm(
        "export.returns",
        "Returns Exports",
        "Export Access",
        module="Returns",
        description="Export returns tracker and analytics datasets",
        aliases=("returns.export",),
    ),
    _perm(
        "feature.customers.dashboard.view",
        "Customer Dashboard",
        "Feature Access",
        module="Customers",
        description="View customer KPI dashboard data",
        aliases=("feature.customers.kpis.view",),
    ),
    _perm(
        "feature.customers.cohorts.view",
        "Cohorts",
        "Feature Access",
        module="Customers",
        description="Access customer cohort analysis",
    ),
    _perm(
        "feature.customers.rfm.view",
        "RFM",
        "Feature Access",
        module="Customers",
        description="Access customer RFM analysis",
    ),
    _perm(
        "feature.customers.clv.view",
        "CLV",
        "Feature Access",
        module="Customers",
        description="Access customer CLV analysis",
    ),
    _perm(
        "feature.products.dashboard.view",
        "Product Dashboard",
        "Feature Access",
        module="Products",
        description="View product overview dashboards",
        aliases=("feature.products.overview.view",),
    ),
    _perm(
        "feature.products.trajectory.view",
        "Trajectory",
        "Feature Access",
        module="Products",
        description="Access product trajectory charts and trend diagnostics",
    ),
    _perm(
        "feature.products.table.view",
        "Product Table",
        "Feature Access",
        module="Products",
        description="Access the product command table and table APIs",
    ),
    _perm(
        "feature.overview.forecast.view",
        "Overview Forecast",
        "Feature Access",
        module="Overview",
        description="Access overview forecast tooling and forecast APIs",
    ),
    _perm(
        "feature.overview.movers.view",
        "Overview Movers",
        "Feature Access",
        module="Overview",
        description="Access overview movers panels and movers drilldowns",
    ),
    _perm(
        "feature.overview.executive_insights.view",
        "Executive Insights",
        "Feature Access",
        module="Overview",
        description="Access executive insights and deterministic recommendations",
    ),
    _perm(
        "feature.products.segments.view",
        "Segments",
        "Feature Access",
        module="Products",
        description="Access product segment and quadrant analysis",
    ),
    _perm(
        "feature.products.pricing.view",
        "Pricing",
        "Feature Access",
        module="Products",
        description="Access pricing and execution views",
    ),
    _perm(
        "feature.products.recommendations.view",
        "Recommendations",
        "Feature Access",
        module="Products",
        description="Access product recommendations and intel panels",
    ),
    _perm(
        "feature.products.forecast.view",
        "Forecast",
        "Feature Access",
        module="Products",
        description="Access product forecasting and forecast APIs",
    ),
    _perm(
        "data.cost.view",
        "Cost Data",
        "Sensitive Data",
        module="Sensitive",
        description="View raw cost data and cost-derived cards",
        aliases=("view_costs",),
    ),
    _perm(
        "data.margin.view",
        "Margin Data",
        "Sensitive Data",
        module="Sensitive",
        description="View margin percent and margin dollar metrics",
    ),
    _perm(
        "data.profit.view",
        "Profit Data",
        "Sensitive Data",
        module="Sensitive",
        description="View profit metrics and profit-derived exports",
    ),
    _perm(
        "data.price_recommendation.view",
        "Pricing Recommendations",
        "Sensitive Data",
        module="Sensitive",
        description="View pricing recommendations derived from cost or margin",
        aliases=("data.pricing_recommendation.view",),
    ),
    _perm(
        "data.margin_risk.view",
        "Margin Risk",
        "Sensitive Data",
        module="Sensitive",
        description="View margin-risk diagnostics and risk tables",
    ),
    _perm(
        "export.sensitive.unmasked",
        "Full Sensitive Exports",
        "Sensitive Data",
        module="Sensitive",
        description="Download unmasked exports for cost and margin sensitive fields",
        aliases=("data.cost_export.unmasked",),
    ),
    _perm(
        "returns.create",
        "Create Returns",
        "Returns Workflow",
        module="Returns",
        description="Create return requests",
    ),
    _perm(
        "returns.approvals.view",
        "Approvals Queue",
        "Returns Workflow",
        module="Returns",
        description="View the returns approvals queue",
    ),
    _perm(
        "returns.warehouse.view",
        "Warehouse View",
        "Returns Workflow",
        module="Returns",
        description="View warehouse-focused returns pages",
        aliases=("page.returns.warehouse",),
    ),
    _perm(
        "returns.warehouse.scan",
        "Warehouse Scan",
        "Returns Workflow",
        module="Returns",
        description="Access warehouse return scan flow",
    ),
    _perm(
        "returns.warehouse.receive",
        "Warehouse Receive",
        "Returns Workflow",
        module="Returns",
        description="Update warehouse receiving fields",
    ),
    _perm(
        "returns.warehouse.inspect",
        "Warehouse Inspect",
        "Returns Workflow",
        module="Returns",
        description="Inspect received returns in warehouse",
    ),
    _perm(
        "returns.approve.wh",
        "Warehouse Approve",
        "Returns Workflow",
        module="Returns",
        description="Approve return requests in warehouse",
    ),
    _perm(
        "returns.approve.mgr",
        "Manager Approve",
        "Returns Workflow",
        module="Returns",
        description="Approve return requests as manager",
    ),
    _perm("returns.reject", "Reject", "Returns Workflow", module="Returns", description="Reject return requests"),
    _perm(
        "page.returns.analytics.view",
        "Returns Analytics",
        "Returns Workflow",
        module="Returns",
        description="View returns analytics",
        aliases=("feature.returns.analytics.view",),
    ),
    _perm(
        "returns.ops.queue.view",
        "Operations Queue",
        "Returns Workflow",
        module="Returns",
        description="View the returns operations queue",
    ),
    _perm(
        "returns.ops.approve",
        "Ops Approve",
        "Returns Workflow",
        module="Returns",
        description="Approve return requests from operations",
    ),
    _perm(
        "returns.ops.deny",
        "Ops Deny",
        "Returns Workflow",
        module="Returns",
        description="Deny return requests from operations",
    ),
    _perm(
        "returns.ops.override",
        "Ops Override",
        "Returns Workflow",
        module="Returns",
        description="Override return workflow state from operations",
    ),
    _perm(
        "page.returns.customer_portal",
        "Customer Portal",
        "Returns Workflow",
        module="Returns",
        description="Access customer return request pages",
    ),
    _perm(
        "page.returns.ops",
        "Returns Ops",
        "Returns Workflow",
        module="Returns",
        description="Access returns operations pages",
    ),
    _perm(
        "admin.returns.manage",
        "Returns Admin",
        "Returns Workflow",
        module="Returns",
        description="Manage returns policies and integrations",
    ),
    _perm(
        "returns.approve",
        "Legacy Approve",
        "Returns Workflow",
        module="Returns",
        description="Legacy approve permission",
    ),
    _perm(
        "returns.deny",
        "Legacy Deny",
        "Returns Workflow",
        module="Returns",
        description="Legacy deny permission",
    ),
    _perm(
        "returns.override",
        "Legacy Override",
        "Returns Workflow",
        module="Returns",
        description="Legacy override permission",
    ),
    _perm(
        "returns.labels.generate",
        "Generate Labels",
        "Returns Workflow",
        module="Returns",
        description="Generate return shipping labels",
    ),
    _perm(
        "returns.refunds.issue",
        "Issue Refunds",
        "Returns Workflow",
        module="Returns",
        description="Issue return refunds or credits",
    ),
    _perm(
        "returns.pdf.export",
        "PDF Export",
        "Returns Workflow",
        module="Returns",
        description="Export return PDFs",
    ),
    _perm(
        "admin.users.manage",
        "Manage Users",
        "Admin Management",
        module="Admin",
        description="Create, update, approve, disable, and invite users",
    ),
    _perm(
        "admin.roles.manage",
        "Manage Roles",
        "Admin Management",
        module="Admin",
        description="Manage roles and role permission defaults",
    ),
    _perm(
        "admin.permissions.manage",
        "Manage Permissions",
        "Admin Management",
        module="Admin",
        description="Manage page, feature, and sensitive-data permissions",
    ),
    _perm(
        "admin.audit.view",
        "View Audit",
        "Admin Management",
        module="Admin",
        description="View admin audit logs",
    ),
    _perm(
        "scope.manage",
        "Manage Scope",
        "Admin Management",
        module="Admin",
        description="Manage user scope and visibility rules",
    ),
    _perm(
        "admin.notifications.defaults",
        "Notification Defaults",
        "Admin Management",
        module="Admin",
        description="Manage notification defaults",
        aliases=("admin.notifications.manage",),
    ),
    _perm(
        "export.suppliers.products_vs_customers",
        "Supplier Products Vs Customers Export",
        "Export Access",
        module="Suppliers",
        description="Export supplier products-vs-customers datasets",
        aliases=("export.suppliers.products-vs-customers",),
    ),
    _perm(
        "manage_features",
        "Manage Features",
        "Admin Management",
        module="Admin",
        description="Manage app feature flags",
    ),
    _perm(
        "manage_branding",
        "Manage Branding",
        "Admin Management",
        module="Admin",
        description="Manage application branding",
    ),
    _perm(
        "manage_visibility",
        "Legacy Visibility Admin",
        "Admin Management",
        module="Admin",
        description="Legacy visibility management permission",
    ),
]


_META_BY_KEY: Dict[str, dict[str, Any]] = {}
PERMISSION_ALIAS_MAP: Dict[str, str] = {}
for item in _PERMISSION_DEFINITIONS:
    key = item["key"]
    _META_BY_KEY[key] = item
    PERMISSION_ALIAS_MAP[key] = key
    for alias in item.get("aliases", ()):
        PERMISSION_ALIAS_MAP[str(alias).strip().lower()] = key


def canonical_permission_key(key: str | None) -> str:
    token = str(key or "").strip().lower()
    if not token:
        return ""
    return PERMISSION_ALIAS_MAP.get(token, token)


def canonicalize_permission_keys(keys: Iterable[str] | None) -> Set[str]:
    normalized: Set[str] = set()
    for key in keys or ():
        token = canonical_permission_key(key)
        if token:
            normalized.add(token)
    return normalized


def permission_metadata(key: str | None) -> dict[str, Any] | None:
    token = canonical_permission_key(key)
    if not token:
        return None
    return _META_BY_KEY.get(token)


def permission_registry() -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for item in _PERMISSION_DEFINITIONS:
        bucket = groups.setdefault(
            (item["group"], item["module"]),
            {
                "group": item["group"],
                "module": item["module"],
                "permissions": [],
            },
        )
        bucket["permissions"].append(
            {
                "key": item["key"],
                "label": item["label"],
                "description": item["description"],
                "aliases": list(item.get("aliases", ())),
            }
        )
    ordered: list[dict[str, Any]] = []
    for key in sorted(groups.keys(), key=lambda value: (value[0], value[1])):
        group = groups[key]
        group["permissions"] = sorted(group["permissions"], key=lambda item: item["label"])
        ordered.append(group)
    return ordered


DEFAULT_PERMISSION_CATALOG: Dict[str, str] = {
    item["key"]: item["description"]
    for item in _PERMISSION_DEFINITIONS
}


PAGE_PERMISSION_KEYS: Set[str] = {
    "page.overview.view",
    "page.customers.view",
    "page.products.view",
    "page.regions.view",
    "page.suppliers.view",
    "page.salesreps.view",
    "page.returns.view",
    "page.admin.view",
    "page.notifications.view",
}


DRILLDOWN_PERMISSION_KEYS: Set[str] = {
    "page.customers.drilldown.view",
    "page.products.drilldown.view",
    "page.regions.drilldown.view",
    "page.suppliers.drilldown.view",
    "page.salesreps.drilldown.view",
}


EXPORT_PERMISSION_KEYS: Set[str] = {
    "export.overview",
    "export.customers",
    "export.products",
    "export.regions",
    "export.suppliers",
    "export.suppliers.products_vs_customers",
    "export.salesreps",
    "export.returns",
}


SENSITIVE_DATA_PERMISSION_KEYS: Set[str] = {
    "data.cost.view",
    "data.margin.view",
    "data.profit.view",
    "data.price_recommendation.view",
    "data.margin_risk.view",
    "export.sensitive.unmasked",
}


CUSTOMER_FEATURE_PERMISSION_KEYS: Set[str] = {
    "feature.customers.dashboard.view",
    "feature.customers.cohorts.view",
    "feature.customers.rfm.view",
    "feature.customers.clv.view",
}


OVERVIEW_FEATURE_PERMISSION_KEYS: Set[str] = {
    "feature.overview.forecast.view",
    "feature.overview.movers.view",
    "feature.overview.executive_insights.view",
}


PRODUCT_FEATURE_PERMISSION_KEYS: Set[str] = {
    "feature.products.dashboard.view",
    "feature.products.trajectory.view",
    "feature.products.segments.view",
    "feature.products.pricing.view",
    "feature.products.table.view",
    "feature.products.recommendations.view",
    "feature.products.forecast.view",
}


SALESREP_RETURN_PERMISSION_KEYS: Set[str] = {
    "page.returns.view",
    "returns.create",
    "export.returns",
    "returns.pdf.export",
    "page.returns.analytics.view",
    "page.returns.customer_portal",
}


WAREHOUSE_RETURN_PERMISSION_KEYS: Set[str] = {
    "page.returns.view",
    "returns.approvals.view",
    "returns.warehouse.view",
    "returns.warehouse.scan",
    "returns.warehouse.receive",
    "returns.warehouse.inspect",
    "returns.approve.wh",
    "returns.reject",
    "export.returns",
    "returns.pdf.export",
}


MANAGER_RETURN_PERMISSION_KEYS: Set[str] = (
    SALESREP_RETURN_PERMISSION_KEYS
    | WAREHOUSE_RETURN_PERMISSION_KEYS
    | {
        "returns.approve.mgr",
        "returns.ops.queue.view",
        "returns.ops.approve",
        "returns.ops.deny",
        "returns.ops.override",
        "page.returns.ops",
        "returns.approve",
        "returns.deny",
        "returns.override",
        "returns.labels.generate",
    }
)


ADMIN_RETURN_PERMISSION_KEYS: Set[str] = (
    MANAGER_RETURN_PERMISSION_KEYS
    | {
        "admin.returns.manage",
        "returns.refunds.issue",
    }
)


RETURNS_ONLY_PERMISSION_KEYS: Set[str] = {
    "page.returns.view",
    "returns.pdf.export",
}


def _merge_permissions(*groups: Set[str]) -> Set[str]:
    merged: Set[str] = set()
    for group in groups:
        merged.update(group)
    return merged


_ADMIN_BASE: Set[str] = set(DEFAULT_PERMISSION_CATALOG.keys())
_OWNER_GM_ADMIN_BASE: Set[str] = {
    "page.admin.view",
    "admin.users.manage",
    "admin.audit.view",
    "scope.manage",
    "page.notifications.view",
    "admin.notifications.defaults",
    "manage_features",
    "manage_branding",
    "manage_visibility",
}

_OVERVIEW_ACCESS: Set[str] = {
    "page.overview.view",
    "export.overview",
} | OVERVIEW_FEATURE_PERMISSION_KEYS

_CUSTOMERS_ACCESS: Set[str] = {
    "page.customers.view",
    "page.customers.drilldown.view",
    "export.customers",
} | CUSTOMER_FEATURE_PERMISSION_KEYS

_PRODUCTS_ACCESS: Set[str] = {
    "page.products.view",
    "page.products.drilldown.view",
    "export.products",
} | PRODUCT_FEATURE_PERMISSION_KEYS

_REGIONS_ACCESS: Set[str] = {
    "page.regions.view",
    "page.regions.drilldown.view",
    "export.regions",
}

_SUPPLIERS_ACCESS: Set[str] = {
    "page.suppliers.view",
    "page.suppliers.drilldown.view",
    "export.suppliers",
    "export.suppliers.products_vs_customers",
}

_LABOR_ACCESS: Set[str] = {
    "page.labor.view",
    "export.labor",
}

_SALESREPS_ACCESS: Set[str] = {
    "page.salesreps.view",
    "page.salesreps.drilldown.view",
    "export.salesreps",
}

_SENSITIVE_FULL_ACCESS: Set[str] = set(SENSITIVE_DATA_PERMISSION_KEYS)


DEFAULT_ROLE_PERMISSION_KEYS: Dict[str, Set[str]] = {
    "admin": {"*"},
    "owner": _merge_permissions(
        _OWNER_GM_ADMIN_BASE,
        _OVERVIEW_ACCESS,
        _CUSTOMERS_ACCESS,
        _PRODUCTS_ACCESS,
        _REGIONS_ACCESS,
        _SUPPLIERS_ACCESS,
        _LABOR_ACCESS,
        _SALESREPS_ACCESS,
        _SENSITIVE_FULL_ACCESS,
        ADMIN_RETURN_PERMISSION_KEYS,
    ),
    "gm": _merge_permissions(
        _OWNER_GM_ADMIN_BASE,
        _OVERVIEW_ACCESS,
        _CUSTOMERS_ACCESS,
        _PRODUCTS_ACCESS,
        _REGIONS_ACCESS,
        _SUPPLIERS_ACCESS,
        _LABOR_ACCESS,
        _SALESREPS_ACCESS,
        _SENSITIVE_FULL_ACCESS,
        ADMIN_RETURN_PERMISSION_KEYS,
    ),
    "sales_manager": _merge_permissions(
        _OVERVIEW_ACCESS,
        _CUSTOMERS_ACCESS,
        _PRODUCTS_ACCESS,
        _REGIONS_ACCESS,
        _LABOR_ACCESS,
        _SALESREPS_ACCESS,
        {
            "page.notifications.view",
            "admin.notifications.defaults",
        },
        {
            "data.cost.view",
            "data.margin.view",
            "data.profit.view",
            "data.price_recommendation.view",
            "data.margin_risk.view",
            "export.sensitive.unmasked",
        },
        MANAGER_RETURN_PERMISSION_KEYS,
    ),
    "sales": _merge_permissions(
        _OVERVIEW_ACCESS,
        {
            "page.customers.view",
            "page.customers.drilldown.view",
            "export.customers",
            "feature.customers.dashboard.view",
            "feature.customers.cohorts.view",
            "feature.customers.rfm.view",
            "feature.customers.clv.view",
        },
        {
            "page.products.view",
            "page.products.drilldown.view",
            "export.products",
            "feature.products.dashboard.view",
            "feature.products.trajectory.view",
            "feature.products.segments.view",
            "feature.products.table.view",
            "feature.products.forecast.view",
        },
        _SALESREPS_ACCESS,
        {
            "page.notifications.view",
        },
        SALESREP_RETURN_PERMISSION_KEYS,
    ),
    "warehouse": _merge_permissions(
        WAREHOUSE_RETURN_PERMISSION_KEYS,
    ),
    "production": _merge_permissions(
        _OVERVIEW_ACCESS,
        {
            "page.products.view",
            "page.products.drilldown.view",
            "export.products",
            "feature.products.dashboard.view",
            "feature.products.trajectory.view",
            "feature.products.segments.view",
            "feature.products.table.view",
            "feature.products.forecast.view",
        },
        _LABOR_ACCESS,
        _SUPPLIERS_ACCESS,
        WAREHOUSE_RETURN_PERMISSION_KEYS,
    ),
    "analyst": _merge_permissions(
        _OVERVIEW_ACCESS,
        {
            "page.products.view",
            "page.products.drilldown.view",
            "export.products",
            "feature.products.dashboard.view",
            "feature.products.trajectory.view",
            "feature.products.segments.view",
            "feature.products.table.view",
            "feature.products.pricing.view",
            "feature.products.recommendations.view",
            "feature.products.forecast.view",
        },
        {
            "data.cost.view",
            "data.margin.view",
            "data.profit.view",
            "data.price_recommendation.view",
            "data.margin_risk.view",
            "export.sensitive.unmasked",
        },
        _LABOR_ACCESS,
    ),
    "viewer": _merge_permissions(
        _OVERVIEW_ACCESS,
        {
            "page.products.view",
            "page.products.drilldown.view",
            "export.products",
            "feature.products.dashboard.view",
            "feature.products.trajectory.view",
            "feature.products.segments.view",
            "feature.products.table.view",
            "feature.products.forecast.view",
        },
    ),
    "returns_only": set(RETURNS_ONLY_PERMISSION_KEYS),
}


# Sync both the canonical manager role and the historical "manager" alias so
# existing rows never fall out of compliance.
ROLE_PERMISSION_SYNC_KEYS: Mapping[str, Set[str]] = {
    **DEFAULT_ROLE_PERMISSION_KEYS,
    "admin": {"*"},
    "manager": set(DEFAULT_ROLE_PERMISSION_KEYS["sales_manager"]),
}


def _editor_item(
    key: str,
    label: str,
    *,
    item_type: str,
    description: str,
    aliases: Sequence[str] = (),
    requires: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "key": canonical_permission_key(key),
        "label": label,
        "type": item_type,
        "description": description,
        "aliases": [canonical_permission_key(alias) or str(alias).strip().lower() for alias in aliases if str(alias).strip()],
        "requires": [canonical_permission_key(dep) or str(dep).strip().lower() for dep in requires if str(dep).strip()],
    }


PERMISSION_EDITOR_MODULES: list[dict[str, Any]] = [
    {
        "id": "overview",
        "label": "Overview",
        "description": "Business performance landing page and executive diagnostics.",
        "items": [
            _editor_item("page.overview.view", "View page", item_type="page", description="Allow the Overview landing page."),
            _editor_item(
                "export.overview",
                "Export snapshot",
                item_type="export",
                description="Allow Overview exports.",
                requires=("page.overview.view",),
            ),
            _editor_item(
                "feature.overview.forecast.view",
                "View forecast",
                item_type="feature",
                description="Allow Overview forecast controls and forecast APIs.",
                requires=("page.overview.view",),
            ),
            _editor_item(
                "feature.overview.movers.view",
                "View movers",
                item_type="feature",
                description="Allow Overview movers panels and movers drilldowns.",
                requires=("page.overview.view",),
            ),
            _editor_item(
                "feature.overview.executive_insights.view",
                "View executive insights",
                item_type="feature",
                description="Allow deterministic insight narratives and recommended actions.",
                requires=("page.overview.view",),
            ),
        ],
    },
    {
        "id": "customers",
        "label": "Customers",
        "description": "Customer analytics pages, drilldowns, and customer exports.",
        "items": [
            _editor_item("page.customers.view", "View main page", item_type="page", description="Allow the Customers landing page."),
            _editor_item(
                "feature.customers.dashboard.view",
                "View KPI tab",
                item_type="feature",
                description="Allow the KPI / dashboard experience on Customers.",
                aliases=("feature.customers.kpis.view",),
                requires=("page.customers.view",),
            ),
            _editor_item(
                "feature.customers.rfm.view",
                "View RFM",
                item_type="feature",
                description="Allow customer RFM analysis.",
                requires=("page.customers.view",),
            ),
            _editor_item(
                "feature.customers.cohorts.view",
                "View Cohorts",
                item_type="feature",
                description="Allow cohort retention analysis.",
                requires=("page.customers.view",),
            ),
            _editor_item(
                "feature.customers.clv.view",
                "View CLV",
                item_type="feature",
                description="Allow customer CLV analysis.",
                requires=("page.customers.view",),
            ),
            _editor_item(
                "page.customers.drilldown.view",
                "View drilldown",
                item_type="drilldown",
                description="Allow customer drilldown pages and drilldown APIs.",
                requires=("page.customers.view",),
            ),
            _editor_item(
                "export.customers",
                "Export",
                item_type="export",
                description="Allow customer exports.",
                requires=("page.customers.view",),
            ),
        ],
    },
    {
        "id": "products",
        "label": "Products",
        "description": "Product intelligence pages, tabs, drilldowns, and exports.",
        "items": [
            _editor_item("page.products.view", "View main page", item_type="page", description="Allow the Products landing page."),
            _editor_item(
                "feature.products.dashboard.view",
                "View Overview tab",
                item_type="feature",
                description="Allow product overview KPIs and base product bundle access.",
                aliases=("feature.products.overview.view",),
                requires=("page.products.view",),
            ),
            _editor_item(
                "feature.products.trajectory.view",
                "View Trajectory",
                item_type="feature",
                description="Allow product trajectory charts and trend APIs.",
                requires=("page.products.view",),
            ),
            _editor_item(
                "feature.products.pricing.view",
                "View Pricing",
                item_type="feature",
                description="Allow pricing distributions, pricing guardrails, and pricing intelligence.",
                requires=("page.products.view",),
            ),
            _editor_item(
                "feature.products.segments.view",
                "View Segments",
                item_type="feature",
                description="Allow product segment and quadrant analysis.",
                requires=("page.products.view",),
            ),
            _editor_item(
                "feature.products.table.view",
                "View Table",
                item_type="feature",
                description="Allow the product command table and table APIs.",
                requires=("page.products.view",),
            ),
            _editor_item(
                "page.products.drilldown.view",
                "View drilldown",
                item_type="drilldown",
                description="Allow product drilldowns and drilldown bundles.",
                requires=("page.products.view",),
            ),
            _editor_item(
                "feature.products.recommendations.view",
                "View recommendations",
                item_type="feature",
                description="Allow product recommendation panels and AI signals.",
                requires=("page.products.view",),
            ),
            _editor_item(
                "feature.products.forecast.view",
                "View forecast",
                item_type="feature",
                description="Allow product forecast tools and forecast APIs.",
                requires=("page.products.view", "page.products.drilldown.view"),
            ),
            _editor_item(
                "export.products",
                "Export",
                item_type="export",
                description="Allow product exports.",
                requires=("page.products.view",),
            ),
        ],
    },
    {
        "id": "regions",
        "label": "Regions",
        "description": "Region overview, drilldown, and exports.",
        "items": [
            _editor_item("page.regions.view", "View main page", item_type="page", description="Allow the Regions page."),
            _editor_item(
                "page.regions.drilldown.view",
                "View drilldown",
                item_type="drilldown",
                description="Allow region drilldowns and region drilldown bundles.",
                requires=("page.regions.view",),
            ),
            _editor_item(
                "export.regions",
                "Export",
                item_type="export",
                description="Allow region exports.",
                requires=("page.regions.view",),
            ),
        ],
    },
    {
        "id": "suppliers",
        "label": "Suppliers",
        "description": "Supplier overview, drilldown, and supplier export tools.",
        "items": [
            _editor_item("page.suppliers.view", "View main page", item_type="page", description="Allow the Suppliers page."),
            _editor_item(
                "page.suppliers.drilldown.view",
                "View drilldown",
                item_type="drilldown",
                description="Allow supplier drilldowns and supplier drilldown bundles.",
                requires=("page.suppliers.view",),
            ),
            _editor_item(
                "export.suppliers",
                "Export",
                item_type="export",
                description="Allow supplier exports.",
                requires=("page.suppliers.view",),
            ),
            _editor_item(
                "export.suppliers.products_vs_customers",
                "Export products vs customers",
                item_type="export",
                description="Allow supplier products-vs-customers exports.",
                requires=("page.suppliers.view", "page.suppliers.drilldown.view", "export.suppliers"),
            ),
        ],
    },
    {
        "id": "labor",
        "label": "Labor",
        "description": "Labor Intelligence page with department-based cost, hours, watchlists, and exports.",
        "items": [
            _editor_item("page.labor.view", "View main page", item_type="page", description="Allow the Labor Intelligence page."),
            _editor_item(
                "export.labor",
                "Export",
                item_type="export",
                description="Allow labor snapshot, detail, department summary, and watchlist exports.",
                requires=("page.labor.view",),
            ),
        ],
    },
    {
        "id": "salesreps",
        "label": "Sales Reps",
        "description": "Sales rep overview, drilldown, and exports.",
        "items": [
            _editor_item("page.salesreps.view", "View main page", item_type="page", description="Allow the Sales Reps page."),
            _editor_item(
                "page.salesreps.drilldown.view",
                "View drilldown",
                item_type="drilldown",
                description="Allow sales rep drilldowns and drilldown bundles.",
                requires=("page.salesreps.view",),
            ),
            _editor_item(
                "export.salesreps",
                "Export",
                item_type="export",
                description="Allow sales rep exports.",
                requires=("page.salesreps.view",),
            ),
        ],
    },
    {
        "id": "returns",
        "label": "Returns",
        "description": "Returns portal, workflow actions, analytics, and admin actions.",
        "items": [
            _editor_item("page.returns.view", "View page", item_type="page", description="Allow the Returns portal."),
            _editor_item(
                "returns.create",
                "Create return",
                item_type="feature",
                description="Allow new returns.",
                requires=("page.returns.view",),
            ),
            _editor_item(
                "returns.approvals.view",
                "View approvals",
                item_type="feature",
                description="Allow returns approvals queue access.",
                requires=("page.returns.view",),
            ),
            _editor_item(
                "returns.warehouse.scan",
                "Warehouse scan",
                item_type="feature",
                description="Allow warehouse scan workflow access.",
                requires=("page.returns.view",),
            ),
            _editor_item(
                "page.returns.analytics.view",
                "View analytics",
                item_type="feature",
                description="Allow returns analytics.",
                aliases=("feature.returns.analytics.view",),
                requires=("page.returns.view",),
            ),
            _editor_item(
                "admin.returns.manage",
                "Returns admin",
                item_type="feature",
                description="Allow returns admin and policy management.",
                requires=("page.returns.view", "page.admin.view"),
            ),
            _editor_item(
                "export.returns",
                "Export / PDF / PO",
                item_type="export",
                description="Allow returns exports.",
                requires=("page.returns.view",),
            ),
        ],
    },
    {
        "id": "admin",
        "label": "Admin",
        "description": "Administrative pages and privileged management actions.",
        "items": [
            _editor_item("page.admin.view", "Access admin page", item_type="page", description="Allow the Admin portal."),
            _editor_item(
                "admin.users.manage",
                "Manage users",
                item_type="feature",
                description="Allow user management.",
                requires=("page.admin.view",),
            ),
            _editor_item(
                "admin.roles.manage",
                "Manage roles",
                item_type="feature",
                description="Allow role management.",
                requires=("page.admin.view",),
            ),
            _editor_item(
                "admin.permissions.manage",
                "Manage permissions",
                item_type="feature",
                description="Allow role and permission editing.",
                requires=("page.admin.view",),
            ),
            _editor_item(
                "admin.notifications.defaults",
                "Manage notifications",
                item_type="feature",
                description="Allow notification defaults management.",
                aliases=("admin.notifications.manage",),
                requires=("page.admin.view",),
            ),
            _editor_item(
                "admin.audit.view",
                "View audit",
                item_type="feature",
                description="Allow audit log access.",
                requires=("page.admin.view",),
            ),
        ],
    },
    {
        "id": "notifications",
        "label": "Notifications",
        "description": "User notifications page and alert preferences.",
        "items": [
            _editor_item(
                "page.notifications.view",
                "View page",
                item_type="page",
                description="Allow the notifications page and preferences.",
            ),
        ],
    },
]


PERMISSION_EDITOR_SENSITIVE_CONTROLS: list[dict[str, Any]] = [
    _editor_item(
        "data.cost.view",
        "Show Cost columns, cards, and charts",
        item_type="sensitive",
        description="Allow raw cost values across tables, cards, charts, drilldowns, APIs, and exports that otherwise stay masked.",
    ),
    _editor_item(
        "data.margin.view",
        "Show Margin %",
        item_type="sensitive",
        description="Allow margin percentage metrics derived from cost across pages, drilldowns, APIs, and exports.",
    ),
    _editor_item(
        "data.profit.view",
        "Show Profit $",
        item_type="sensitive",
        description="Allow profit metrics derived from cost across pages, drilldowns, APIs, and exports.",
    ),
    _editor_item(
        "data.price_recommendation.view",
        "Show Pricing recommendations",
        item_type="sensitive",
        description="Allow pricing recommendations and price-uplift suggestions derived from cost or margin.",
        aliases=("data.pricing_recommendation.view",),
    ),
    _editor_item(
        "data.margin_risk.view",
        "Show Margin risk analysis",
        item_type="sensitive",
        description="Allow margin-risk analysis and sensitive cost coverage diagnostics.",
    ),
    _editor_item(
        "export.sensitive.unmasked",
        "Show sensitive exports unmasked",
        item_type="sensitive",
        description="Allow unmasked cost, margin, profit, pricing, and other finance-sensitive fields in exports.",
        aliases=("data.cost_export.unmasked",),
    ),
]


def _sorted_permissions(keys: Iterable[str]) -> list[str]:
    return sorted(canonicalize_permission_keys(keys))


PERMISSION_EDITOR_BUNDLES: list[dict[str, Any]] = [
    {
        "id": "select_all_pages",
        "label": "Select all pages",
        "description": "Enable every top-level page in the app.",
        "mode": "add",
        "permissions": _sorted_permissions(PAGE_PERMISSION_KEYS),
    },
    {
        "id": "clear_all_pages",
        "label": "Clear all pages",
        "description": "Disable every top-level page in the app.",
        "mode": "remove",
        "permissions": _sorted_permissions(PAGE_PERMISSION_KEYS),
    },
    {
        "id": "select_all_drilldowns",
        "label": "Select all drilldowns",
        "description": "Enable all drilldown pages.",
        "mode": "add",
        "permissions": _sorted_permissions(DRILLDOWN_PERMISSION_KEYS),
    },
    {
        "id": "clear_all_drilldowns",
        "label": "Clear all drilldowns",
        "description": "Disable all drilldown pages.",
        "mode": "remove",
        "permissions": _sorted_permissions(DRILLDOWN_PERMISSION_KEYS),
    },
    {
        "id": "enable_all_exports",
        "label": "Enable all exports",
        "description": "Enable all export permissions.",
        "mode": "add",
        "permissions": _sorted_permissions(EXPORT_PERMISSION_KEYS),
    },
    {
        "id": "disable_all_exports",
        "label": "Disable all exports",
        "description": "Disable all export permissions.",
        "mode": "remove",
        "permissions": _sorted_permissions(EXPORT_PERMISSION_KEYS),
    },
    {
        "id": "enable_customer_tabs",
        "label": "Enable all customer tabs",
        "description": "Enable all customer analysis tabs.",
        "mode": "add",
        "permissions": _sorted_permissions(CUSTOMER_FEATURE_PERMISSION_KEYS | {"page.customers.view"}),
    },
    {
        "id": "enable_product_tabs",
        "label": "Enable all product tabs",
        "description": "Enable all product overview tabs.",
        "mode": "add",
        "permissions": _sorted_permissions(PRODUCT_FEATURE_PERMISSION_KEYS | {"page.products.view"}),
    },
    {
        "id": "enable_returns_tools",
        "label": "Enable all returns tools",
        "description": "Enable returns workflow tools and analytics.",
        "mode": "add",
        "permissions": _sorted_permissions(ADMIN_RETURN_PERMISSION_KEYS),
    },
    {
        "id": "hide_all_sensitive_finance",
        "label": "Hide all sensitive finance",
        "description": "Hide cost, margin, profit, pricing recommendations, and margin risk.",
        "mode": "remove",
        "permissions": _sorted_permissions(SENSITIVE_DATA_PERMISSION_KEYS),
    },
    {
        "id": "show_all_sensitive_finance",
        "label": "Show all finance-sensitive data",
        "description": "Enable cost, margin, profit, pricing recommendations, and margin risk.",
        "mode": "add",
        "permissions": _sorted_permissions(SENSITIVE_DATA_PERMISSION_KEYS),
    },
    {
        "id": "hide_cost_visibility",
        "label": "Hide all cost data",
        "description": "Remove cost visibility while leaving non-sensitive page access unchanged.",
        "mode": "remove",
        "permissions": _sorted_permissions({"data.cost.view", "export.sensitive.unmasked"}),
    },
    {
        "id": "show_cost_visibility",
        "label": "Show all cost data",
        "description": "Restore cost visibility and unmasked sensitive exports.",
        "mode": "add",
        "permissions": _sorted_permissions({"data.cost.view", "export.sensitive.unmasked"}),
    },
    {
        "id": "analyst_finance_bundle",
        "label": "Enable analyst finance bundle",
        "description": "Allow common analyst finance visibility controls.",
        "mode": "add",
        "permissions": _sorted_permissions(
            {
                "data.cost.view",
                "data.margin.view",
                "data.profit.view",
                "data.price_recommendation.view",
                "data.margin_risk.view",
                "export.sensitive.unmasked",
            }
        ),
    },
    {
        "id": "executive_read_only_bundle",
        "label": "Enable executive read-only bundle",
        "description": "Enable broad read-only access without admin or returns workflow actions.",
        "mode": "replace",
        "permissions": _sorted_permissions(
            _OVERVIEW_ACCESS
            | _CUSTOMERS_ACCESS
            | _PRODUCTS_ACCESS
            | _REGIONS_ACCESS
            | _SUPPLIERS_ACCESS
            | _SALESREPS_ACCESS
            | {"page.returns.view", "page.returns.analytics.view"}
            | _SENSITIVE_FULL_ACCESS
        ),
    },
]


PERMISSION_EDITOR_PRESETS: list[dict[str, Any]] = [
    {
        "id": "full_admin",
        "label": "Full Admin",
        "description": "Full administrative access across the app.",
        "permissions": _sorted_permissions(DEFAULT_ROLE_PERMISSION_KEYS["admin"]),
    },
    {
        "id": "executive_viewer",
        "label": "Executive Viewer",
        "description": "Read-mostly access with finance visibility and exports.",
        "permissions": _sorted_permissions(
            _OVERVIEW_ACCESS
            | _CUSTOMERS_ACCESS
            | _PRODUCTS_ACCESS
            | _REGIONS_ACCESS
            | _SUPPLIERS_ACCESS
            | _SALESREPS_ACCESS
            | {"page.returns.view", "page.returns.analytics.view", "export.returns"}
            | _SENSITIVE_FULL_ACCESS
        ),
    },
    {
        "id": "manager_preset",
        "label": "Manager Preset",
        "description": "Apply the default sales manager permission model.",
        "permissions": _sorted_permissions(DEFAULT_ROLE_PERMISSION_KEYS["sales_manager"]),
    },
    {
        "id": "sales_analyst",
        "label": "Sales Analyst",
        "description": "Customers, products, sales reps, and exports without admin access.",
        "permissions": _sorted_permissions(
            _OVERVIEW_ACCESS
            | _CUSTOMERS_ACCESS
            | _PRODUCTS_ACCESS
            | _SALESREPS_ACCESS
            | {
                "page.notifications.view",
                "data.margin.view",
                "data.profit.view",
                "data.price_recommendation.view",
            }
        ),
    },
    {
        "id": "products_analyst",
        "label": "Products Analyst",
        "description": "Products-only analyst with pricing, recommendations, drilldowns, and exports.",
        "permissions": _sorted_permissions(
            {
                "page.products.view",
                "page.products.drilldown.view",
                "export.products",
            }
            | PRODUCT_FEATURE_PERMISSION_KEYS
            | {
                "data.cost.view",
                "data.margin.view",
                "data.profit.view",
                "data.price_recommendation.view",
                "data.margin_risk.view",
                "export.sensitive.unmasked",
            }
        ),
    },
    {
        "id": "returns_only_user",
        "label": "Returns-only User",
        "description": "Returns portal and workflow only.",
        "permissions": _sorted_permissions(ADMIN_RETURN_PERMISSION_KEYS),
    },
    {
        "id": "customers_only_user",
        "label": "Customers-only User",
        "description": "Customers page, tabs, drilldowns, and exports.",
        "permissions": _sorted_permissions(_CUSTOMERS_ACCESS),
    },
    {
        "id": "read_only_no_cost_viewer",
        "label": "Read-only No-Cost Viewer",
        "description": "Broad read-only access without cost-derived visibility.",
        "permissions": _sorted_permissions(
            {
                "page.overview.view",
                "feature.overview.movers.view",
                "feature.overview.executive_insights.view",
                "page.customers.view",
                "feature.customers.dashboard.view",
                "feature.customers.cohorts.view",
                "feature.customers.rfm.view",
                "feature.customers.clv.view",
                "page.products.view",
                "feature.products.dashboard.view",
                "feature.products.trajectory.view",
                "feature.products.segments.view",
                "feature.products.table.view",
                "feature.products.forecast.view",
                "page.regions.view",
                "page.suppliers.view",
                "page.salesreps.view",
            }
        ),
    },
]


def permission_editor_schema() -> dict[str, Any]:
    return {
        "modules": PERMISSION_EDITOR_MODULES,
        "sensitive_data": PERMISSION_EDITOR_SENSITIVE_CONTROLS,
        "bundles": PERMISSION_EDITOR_BUNDLES,
        "presets": PERMISSION_EDITOR_PRESETS,
    }


def permission_editor_dependency_map() -> dict[str, tuple[str, ...]]:
    deps: dict[str, tuple[str, ...]] = {}
    schema = permission_editor_schema()
    for section in list(schema.get("modules") or []) + [{"items": schema.get("sensitive_data") or []}]:
        for item in section.get("items") or []:
            key = canonical_permission_key(item.get("key"))
            if not key:
                continue
            requires = tuple(
                dict.fromkeys(
                    dep for dep in canonicalize_permission_keys(item.get("requires") or []) if dep and dep != key
                )
            )
            deps[key] = requires
    return deps


def normalize_permission_selection(permission_keys: Iterable[str]) -> set[str]:
    selected = set(canonicalize_permission_keys(permission_keys))
    dependency_map = permission_editor_dependency_map()
    changed = True
    while changed:
        changed = False
        for key in tuple(selected):
            for dep in dependency_map.get(key, ()):
                if dep not in selected:
                    selected.add(dep)
                    changed = True
    return selected


def permission_selection_warnings(permission_keys: Iterable[str]) -> list[str]:
    selected = set(canonicalize_permission_keys(permission_keys))
    warnings: list[str] = []
    checks = [
        ("Customers", "page.customers.view", "page.customers.drilldown.view", "export.customers"),
        ("Products", "page.products.view", "page.products.drilldown.view", "export.products"),
        ("Regions", "page.regions.view", "page.regions.drilldown.view", "export.regions"),
        ("Suppliers", "page.suppliers.view", "page.suppliers.drilldown.view", "export.suppliers"),
        ("Labor", "page.labor.view", "page.labor.view", "export.labor"),
        ("Sales Reps", "page.salesreps.view", "page.salesreps.drilldown.view", "export.salesreps"),
    ]
    for label, page_key, drill_key, export_key in checks:
        if drill_key != page_key and drill_key in selected and page_key not in selected:
            warnings.append(f"{label} drilldown is enabled without {label} page access.")
        if export_key in selected and page_key not in selected:
            warnings.append(f"{label} export is enabled without {label} page access.")
    if (
        {"data.margin.view", "data.profit.view", "data.price_recommendation.view", "data.margin_risk.view", "export.sensitive.unmasked"}
        & selected
        and "data.cost.view" not in selected
    ):
        warnings.append("Cost visibility is hidden while other cost-derived finance permissions remain enabled.")
    if "feature.products.recommendations.view" in selected and "data.price_recommendation.view" not in selected:
        warnings.append("Products recommendations are enabled, but pricing recommendations are hidden.")
    if "feature.products.forecast.view" in selected and "page.products.drilldown.view" not in selected:
        warnings.append("Product forecast is enabled without product drilldown access.")
    top_level_pages = {
        "page.overview.view",
        "page.customers.view",
        "page.products.view",
        "page.regions.view",
        "page.suppliers.view",
        "page.labor.view",
        "page.salesreps.view",
        "page.admin.view",
        "page.notifications.view",
    }
    if "page.returns.view" in selected and not (top_level_pages & selected):
        warnings.append("User currently has Returns-only access.")
    return warnings
