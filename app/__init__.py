from flask import Flask, abort, render_template, jsonify, request, redirect, url_for
import os
import json
from dotenv import load_dotenv
from flask_login import LoginManager, login_required
from flask_wtf import CSRFProtect
from pathlib import Path
import click
from datetime import datetime

from .config import Config, validate_config_settings
from .core.logging_setup import configure_json_logging
from .core.instrumentation import install_request_logging, patch_data_loader_logging, patch_pandas_logging
from .core.access_policy import require_admin, get_current_scope
from .core.features import load_flags, get_flags
from .core.branding import load_branding, get_branding
from .limiter import limiter
from .cache import cache
from .core.exports import fmt_currency, fmt_percent, fmt_intcomma
from .auth.models import init_auth_db, get_user_by_id, audit_default_rbac_configuration
from flask import session, g
import sys as _sys
from .services.filters import (
    ACTIVE_SAVED_VIEW_SESSION_KEY,
    FILTERS_LAST_APPLIED_SESSION_KEY,
    build_filter_summary,
    capture_filters_from,
    canonical_filters_hash,
    clear_sticky_filters_in_session,
    mark_filters_last_applied,
    read_sticky_filters_from_session,
    serialize_saved_view,
    write_sticky_filters_to_session,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.exceptions import HTTPException
from app.core.exceptions import DatasetNotBuiltError


def register_blueprints(app: Flask) -> None:
    # Import locally to avoid circular imports
    from .blueprints.pages import pages as pages_bp
    from .blueprints.overview import bp as overview_bp, page_bp as overview_page_bp
    from .blueprints.dashboard import bp as dashboard_bp
    from .blueprints.customers import bp as customers_bp
    from .blueprints.products import bp as products_bp
    from .blueprints.inventory import bp as inventory_bp
    from .blueprints.finance import bp as finance_bp
    from .blueprints.marketing import bp as marketing_bp
    from .blueprints.regions import bp as regions_bp
    from .blueprints.suppliers import bp as suppliers_bp
    from .blueprints.labor import bp as labor_bp
    from .blueprints.salesreps import bp as salesreps_bp
    from .blueprints.stakeholder_report import bp as stakeholder_report_bp
    from .blueprints.stakeholder_report import legacy_bp as stakeholder_report_legacy_bp
    from .blueprints.bundles import bp as bundles_bp
    from .blueprints.filters_api import bp as filters_api_bp
    from .blueprints.filters_actions import bp as filters_actions_bp
    from .blueprints.admin import bp as admin_bp
    from .blueprints.api_slice import bp as api_slice_bp
    from .blueprints.views import bp as views_bp
    from .blueprints.events import bp as events_bp
    from .blueprints.admin_api import bp as admin_api_bp
    from .blueprints.notifications import bp as notifications_bp
    from .blueprints.drilldowns import bp as drilldowns_bp
    from .assistant.routes import bp as assistant_bp
    from .auth.routes import bp as auth_bp
    from .returns.blueprints import (
        admin_bp as returns_admin_bp,
        ops_bp as returns_ops_bp,
        portal_bp as returns_portal_bp,
        warehouse_bp as returns_warehouse_bp,
        webhooks_bp as returns_webhooks_bp,
    )

    # Ensure pages (overview HTML) is registered before dashboard so '/' resolves here
    app.register_blueprint(pages_bp)
    app.register_blueprint(overview_page_bp)
    # Overview API
    app.logger.info("Registering overview blueprint...")
    app.register_blueprint(overview_bp)
    app.logger.info("Overview blueprint registered.")
    app.register_blueprint(dashboard_bp, url_prefix="/dashboard")

    # Exempt JSON POST overview API from CSRF (we authenticate via session + login_required)
    try:  # best-effort; keep CSRF for forms
        csrf_ext = app.extensions.get('csrf')
        if csrf_ext:
            csrf_ext.exempt(overview_bp)
    except Exception:
        pass
    app.register_blueprint(customers_bp)
    app.register_blueprint(products_bp)
    app.register_blueprint(inventory_bp)
    app.register_blueprint(finance_bp)
    app.register_blueprint(marketing_bp)
    app.register_blueprint(regions_bp)
    app.register_blueprint(suppliers_bp)
    if bool(app.config.get("LABOR_ANALYTICS_ENABLED", True)):
        app.register_blueprint(labor_bp)
    app.register_blueprint(salesreps_bp)
    app.register_blueprint(stakeholder_report_bp)
    app.register_blueprint(stakeholder_report_legacy_bp)
    app.register_blueprint(filters_api_bp)
    app.register_blueprint(filters_actions_bp)
    app.register_blueprint(api_slice_bp)
    app.register_blueprint(bundles_bp)
    app.register_blueprint(views_bp)
    app.register_blueprint(events_bp)
    app.register_blueprint(notifications_bp)
    app.register_blueprint(drilldowns_bp)
    if bool(app.config.get("AI_ENABLED", False)):
        app.register_blueprint(assistant_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(returns_portal_bp)
    app.register_blueprint(returns_ops_bp)
    app.register_blueprint(returns_warehouse_bp)
    app.register_blueprint(returns_webhooks_bp)
    try:  # carrier callbacks are authenticated via HMAC/idempotency instead of CSRF
        csrf_ext = app.extensions.get('csrf')
        if csrf_ext:
            csrf_ext.exempt(returns_webhooks_bp)
    except Exception:
        pass
    admin_enabled = bool(app.config.get("ADMIN_PORTAL_ENABLED", True))
    if admin_enabled:
        app.register_blueprint(admin_api_bp)
        app.register_blueprint(admin_bp)
        app.register_blueprint(returns_admin_bp)

    @app.context_processor
    def _inject_demo_logins():
        """Advertise the demo credentials on the public demo only."""
        try:
            from .core.demo_accounts import DEMO_PASSWORD, demo_login_hints, demo_logins_enabled

            if not demo_logins_enabled():
                return {"demo_logins": None, "demo_password": None}
            return {"demo_logins": demo_login_hints(), "demo_password": DEMO_PASSWORD}
        except Exception:
            return {"demo_logins": None, "demo_password": None}

    @app.context_processor
    def _inject_nav_flags():
        try:
            return {
                "has_salesreps": "salesreps" in app.blueprints,
                "has_labor": "labor" in app.blueprints,
                "admin_portal_enabled": bool(app.config.get("ADMIN_PORTAL_ENABLED", True)),
                "notifications_enabled": bool(app.config.get("NOTIFICATIONS_ENABLED", False)),
                "admin_notif_defaults_enabled": bool(app.config.get("ADMIN_NOTIF_DEFAULTS", False)),
                "returns_enabled": bool(app.config.get("RETURNS_ENABLED", False)),
                "ai_enabled": bool(app.config.get("AI_ENABLED", False)),
            }
        except Exception:
            return {
                "has_salesreps": False,
                "has_labor": False,
                "admin_portal_enabled": False,
                "notifications_enabled": False,
                "admin_notif_defaults_enabled": False,
                "returns_enabled": False,
                "ai_enabled": False,
            }

    @app.context_processor
    def _inject_data_freshness():
        """
        The last date the dataset holds, for the global filter bar.

        Cheap: a memoised manifest read, not a query. See
        app/services/comparison.py.
        """
        try:
            from .services.comparison import data_cutoff, format_day

            cutoff = data_cutoff()
            if cutoff is None:
                return {"data_through": None, "data_through_label": None}
            return {"data_through": cutoff, "data_through_label": format_day(cutoff)}
        except Exception:
            return {"data_through": None, "data_through_label": None}

    @app.context_processor
    def _inject_page_guide():
        try:
            from .core.page_guides import guide_for_request

            return {"page_guide": guide_for_request(request)}
        except Exception:
            return {"page_guide": None}

    # Exposed as a global rather than a context processor on purpose: a context
    # processor runs for every template render, including partials and error
    # pages that have no filter bar, and this one costs a query on a cold cache.
    # As a global it is only evaluated where `_filters.html` actually asks.
    def _inline_filter_options():
        from .services import filters_service

        return filters_service.inline_options_bootstrap()

    app.jinja_env.globals["inline_filter_options"] = _inline_filter_options

    @app.context_processor
    def _inject_static_site_link():
        """Where the prerendered half of the demo lives, if it is deployed.

        The two halves are separate origins: analytics is prerendered onto a
        CDN, and this app keeps the writes - returns, approvals, admin. The
        `before_request` above already bounces an analytics path back to the
        CDN, but a redirect is a round trip a reviewer pays for on a cold
        container, and it leaves the nav pointing somewhere it will not stay.
        Given the URL, the nav can link straight there instead.
        """
        root = str(os.getenv("DEMO_STATIC_SITE_URL", "")).strip().rstrip("/")
        if not root:
            return {"static_site_url": None, "static_page_url": None}

        def static_page_url(suffix: str = "") -> str:
            return f"{root}/{str(suffix or '').lstrip('/')}"

        return {"static_site_url": root, "static_page_url": static_page_url}

    @app.context_processor
    def _inject_active_drilldown_context():
        try:
            from flask_login import current_user
            from .services import drilldown_service

            token = request.args.get("drill_context")
            if not token:
                return {"active_drilldown_context": None, "active_drilldown_context_error": None}
            try:
                banner = drilldown_service.build_context_banner(token, user_obj=current_user)
                return {"active_drilldown_context": banner, "active_drilldown_context_error": None}
            except PermissionError:
                return {
                    "active_drilldown_context": None,
                    "active_drilldown_context_error": {
                        "message": "The drilled context is no longer valid for your current RBAC scope.",
                        "clear_href": request.path,
                    },
                }
            except Exception:
                return {
                    "active_drilldown_context": None,
                    "active_drilldown_context_error": {
                        "message": "The drilled context is invalid or expired.",
                        "clear_href": request.path,
                    },
                }
        except Exception:
            return {"active_drilldown_context": None, "active_drilldown_context_error": None}

# Ensure import alias so tests can `from app import __init__ as app_module`
# by exposing submodule key pointing to this module object.
_sys.modules.setdefault("app.__init__", _sys.modules[__name__])
# Also expose as attribute so `from app import __init__` returns this module
globals()["__init__"] = _sys.modules[__name__]


def init_extensions(app: Flask) -> None:
    # Flask-Login
    login_manager = LoginManager()
    login_manager.login_view = "auth.login"

    @login_manager.user_loader
    def load_user(user_id: str):  # pragma: no cover - runtime fetch
        return get_user_by_id(user_id)

    login_manager.init_app(app)

    # CSRF protect all unsafe methods (POST/PUT/PATCH/DELETE)
    csrf = CSRFProtect()
    csrf.init_app(app)

    # Flask-Limiter
    limiter.init_app(app)
    # Flask-Caching
    cache_config = getattr(cache, "config", {}) or {}
    app.config.setdefault("CACHE_TYPE", cache_config.get("CACHE_TYPE", "SimpleCache"))
    app.config.setdefault("CACHE_DEFAULT_TIMEOUT", cache_config.get("CACHE_DEFAULT_TIMEOUT", 300))
    if cache_config.get("CACHE_REDIS_URL"):
        app.config.setdefault("CACHE_REDIS_URL", cache_config["CACHE_REDIS_URL"])
    cache.init_app(app)

    @app.before_request
    def _serve_public_demo_workspaces_from_cdn():
        """Keep analytics traffic off the memory-constrained live workflow host."""
        static_root = str(os.getenv("DEMO_STATIC_SITE_URL", "")).strip().rstrip("/")
        if not static_root or request.method != "GET":
            return None
        try:
            from flask_login import current_user

            if not getattr(current_user, "is_authenticated", False):
                return None
        except Exception:
            return None

        workspaces = {
            "/": "",
            "/overview/": "",
            "/customers/": "customers/",
            "/products/": "products/",
            "/inventory/": "inventory/",
            "/regions/": "regions/",
            "/suppliers/": "suppliers/",
            "/labor/": "labor/",
            "/salesreps/": "salesreps/",
            "/planning/": "planning/",
            "/finance/": "finance/",
            "/marketing/": "marketing/",
            "/metrics/": "metrics/",
        }
        suffix = workspaces.get(request.path)
        if suffix is None:
            return None
        target = f"{static_root}/{suffix}"
        if request.query_string:
            target = f"{target}?{request.query_string.decode('utf-8', errors='ignore')}"
        return redirect(target, code=302)

    # Jinja filters (formatting)
    app.jinja_env.filters["currency"] = fmt_currency
    app.jinja_env.filters["percent"] = fmt_percent
    app.jinja_env.filters["intcomma"] = fmt_intcomma

    # House rules for numbers and dates, shared with app/static/js/format.js.
    # Registered alongside the older filters rather than replacing them: several
    # templates depend on `currency` always printing two decimals.
    try:
        from .services.formatting import register_jinja_filters

        register_jinja_filters(app)
    except Exception:  # pragma: no cover - never block boot on a filter
        app.logger.exception("formatting.filters_install_failed")

    @app.before_request
    def _load_saved_views():
        try:
            from .auth.models import list_saved_views
            from flask_login import current_user
            user_id = current_user.get_id() if getattr(current_user, "is_authenticated", False) else None
            g.saved_views = list_saved_views(current_user)
            g.active_saved_view_id = session.get(ACTIVE_SAVED_VIEW_SESSION_KEY)
            current_filters = read_sticky_filters_from_session(session, user_id=user_id) or session.get("filters") or {}
            g.global_filters_summary = build_filter_summary(current_filters)
            g.global_filters_hash = canonical_filters_hash(current_filters)
            g.saved_views_ui = [serialize_saved_view(view, active_id=g.active_saved_view_id) for view in (g.saved_views or [])]
            g.active_saved_view = next((item for item in g.saved_views_ui if item.get("active")), None)
            g.saved_view_dirty = bool(
                g.active_saved_view and g.active_saved_view.get("filters_hash") != g.global_filters_hash
            )
            raw_last_applied = session.get(FILTERS_LAST_APPLIED_SESSION_KEY)
            g.filters_last_applied_at = None
            if raw_last_applied:
                try:
                    parsed = datetime.fromisoformat(str(raw_last_applied).replace("Z", "+00:00"))
                    g.filters_last_applied_at = parsed.strftime("%b %d, %Y %I:%M %p UTC")
                except Exception:
                    g.filters_last_applied_at = str(raw_last_applied)
        except Exception:
            g.saved_views = []
            g.saved_views_ui = []
            g.active_saved_view_id = None
            g.active_saved_view = None
            g.saved_view_dirty = False
            g.global_filters_summary = build_filter_summary({})
            g.global_filters_hash = canonical_filters_hash({})
            g.filters_last_applied_at = None

    @app.teardown_request
    def _close_duckdb_conn(_exc=None):
        try:
            from .services import fact_store
            fact_store.close_duckdb_conn()
        except Exception:
            pass
        try:
            from .services import labor_store
            labor_store.close_duckdb_conn()
        except Exception:
            pass


def _bootstrap_background_refresh(app: Flask) -> None:
    """Start the periodic parquet refresh scheduler in the background."""

    app.logger.info("Skipping in-process background refresh; run `python run.py refresh-fact` separately.")
    return


def _handle_dataset_not_built(exc: DatasetNotBuiltError):
    return _problem_response(503, "Dataset not built. Run ETL job.", detail=str(exc))


def _problem_response(status: int, title: str, *, detail: str | None = None):
    """RFC7807-style problem details for API responses."""
    try:
        instance = request.path
    except Exception:
        instance = None
    try:
        req_id = getattr(g, "request_id", None)
    except Exception:
        req_id = None
    payload = {
        "type": "about:blank",
        "title": title,
        "status": status,
        "detail": detail or title,
        "instance": instance,
        "request_id": req_id,
        # Backward-compatible key for existing clients/tests
        "error": title,
    }
    resp = jsonify(payload)
    resp.status_code = status
    resp.mimetype = "application/problem+json"
    return resp


def create_app() -> Flask:
    # Load environment from a .env file if present.
    #
    # WA_IGNORE_DOTENV lets the test suite opt out. The demo config turns on
    # every v2/v3 feature flag, so a developer who has run the demo would
    # otherwise get a completely different set of test results from CI - see
    # the root conftest.py.
    if os.getenv("WA_IGNORE_DOTENV", "").strip().lower() not in {"1", "true", "yes", "on"}:
        load_dotenv()
    # Explicitly mark this process as a web worker to block live SQL usage.
    os.environ.setdefault("APP_MODE", "web")

    # Calculate the project root explicitly
    project_root = Path(__file__).resolve().parent.parent
    app_folder = Path(__file__).resolve().parent

    app = Flask(
        __name__,
        root_path=str(project_root), # Explicitly set the project root
        static_folder=str(app_folder / "static"),      # Absolute path to static folder
        template_folder=str(app_folder / "templates"), # Absolute path to templates folder
        static_url_path="/static"
    )
    app.config.from_object(Config())
    # Respect proxy headers when running behind nginx/ALB
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)
    # Allow runtime env flags to override auth for demos/dev without rebuilding Config
    def _flag(name, default=False):
        val = os.getenv(name)
        if val is None:
            return app.config.get(name, default)
        return str(val).strip().lower() in {"1", "true", "yes", "on"}
    app.config["LOGIN_DISABLED"] = _flag("LOGIN_DISABLED", app.config.get("LOGIN_DISABLED", False))
    app.config["AUTHZ_DISABLED"] = _flag("AUTHZ_DISABLED", app.config.get("AUTHZ_DISABLED", False))
    app.config["STICKY_FILTERS"] = _flag("STICKY_FILTERS", app.config.get("STICKY_FILTERS", True))
    app.config["FILTERS_CANONICAL_V2"] = _flag("FILTERS_CANONICAL_V2", app.config.get("FILTERS_CANONICAL_V2", False))
    app.config["PRODUCT_INTELLIGENCE_V2"] = _flag("PRODUCT_INTELLIGENCE_V2", app.config.get("PRODUCT_INTELLIGENCE_V2", False))
    app.config["PRODUCTS_V3"] = _flag("PRODUCTS_V3", app.config.get("PRODUCTS_V3", False))
    app.config["PRODUCTS_V4"] = _flag("PRODUCTS_V4", app.config.get("PRODUCTS_V4", False))
    app.config["PRODUCT_DRILLDOWN_V2"] = _flag("PRODUCT_DRILLDOWN_V2", app.config.get("PRODUCT_DRILLDOWN_V2", False))
    app.config["PRODUCT_FORECAST_V1"] = _flag("PRODUCT_FORECAST_V1", app.config.get("PRODUCT_FORECAST_V1", False))
    app.config["NOTIFICATIONS_ENABLED"] = _flag("NOTIFICATIONS_ENABLED", app.config.get("NOTIFICATIONS_ENABLED", False))
    app.config["ADMIN_NOTIF_DEFAULTS"] = _flag("ADMIN_NOTIF_DEFAULTS", app.config.get("ADMIN_NOTIF_DEFAULTS", False))
    app.config["SALESREP_DRILLDOWN_V2"] = _flag("SALESREP_DRILLDOWN_V2", app.config.get("SALESREP_DRILLDOWN_V2", False))
    app.config["CUSTOMERS_KPIS_V2"] = _flag("CUSTOMERS_KPIS_V2", app.config.get("CUSTOMERS_KPIS_V2", False))
    app.config["CUSTOMERS_KPIS_V3"] = _flag("CUSTOMERS_KPIS_V3", app.config.get("CUSTOMERS_KPIS_V3", False))
    app.config["CUSTOMERS_RFM_V2"] = _flag("CUSTOMERS_RFM_V2", app.config.get("CUSTOMERS_RFM_V2", False))
    app.config["CUSTOMERS_CLV_V2"] = _flag("CUSTOMERS_CLV_V2", app.config.get("CUSTOMERS_CLV_V2", False))
    app.config["CUSTOMER_DRILLDOWN_V2"] = _flag("CUSTOMER_DRILLDOWN_V2", app.config.get("CUSTOMER_DRILLDOWN_V2", False))
    app.config["SUPPLIERS_V2"] = _flag("SUPPLIERS_V2", app.config.get("SUPPLIERS_V2", False))
    app.config["SUPPLIER_DRILLDOWN_V2"] = _flag("SUPPLIER_DRILLDOWN_V2", app.config.get("SUPPLIER_DRILLDOWN_V2", False))
    app.config["REGIONS_V2"] = _flag("REGIONS_V2", app.config.get("REGIONS_V2", False))
    app.config["REGION_OVERVIEW_V2"] = _flag("REGION_OVERVIEW_V2", app.config.get("REGION_OVERVIEW_V2", False))
    app.config["REGION_DRILLDOWN_V2"] = _flag("REGION_DRILLDOWN_V2", app.config.get("REGION_DRILLDOWN_V2", False))
    app.config["OVERVIEW_V2"] = _flag("OVERVIEW_V2", app.config.get("OVERVIEW_V2", False))
    app.config["OVERVIEW_V3"] = _flag("OVERVIEW_V3", app.config.get("OVERVIEW_V3", False))
    app.config["OVERVIEW_V2_ADMIN_ONLY"] = _flag("OVERVIEW_V2_ADMIN_ONLY", app.config.get("OVERVIEW_V2_ADMIN_ONLY", False))
    app.config["OVERVIEW_FORECAST_V2"] = _flag("OVERVIEW_FORECAST_V2", app.config.get("OVERVIEW_FORECAST_V2", False))
    app.config["OVERVIEW_MOVERS_FAST"] = _flag("OVERVIEW_MOVERS_FAST", app.config.get("OVERVIEW_MOVERS_FAST", False))
    app.config["COHORTS_V2"] = _flag("COHORTS_V2", app.config.get("COHORTS_V2", False))
    app.config["ADMIN_USER_SELECT"] = _flag("ADMIN_USER_SELECT", app.config.get("ADMIN_USER_SELECT", True))
    app.config["ADMIN_PORTAL_ENABLED"] = _flag("ADMIN_PORTAL_ENABLED", app.config.get("ADMIN_PORTAL_ENABLED", True))
    app.config["ADMIN_PERMISSIONS_V2"] = _flag("ADMIN_PERMISSIONS_V2", app.config.get("ADMIN_PERMISSIONS_V2", False))
    app.config["AUTHZ_ENFORCEMENT"] = _flag("AUTHZ_ENFORCEMENT", app.config.get("AUTHZ_ENFORCEMENT", False))
    app.config["AUTHZ_DB_PERMISSIONS"] = _flag("AUTHZ_DB_PERMISSIONS", app.config.get("AUTHZ_DB_PERMISSIONS", True))
    app.config["RETURNS_ENABLED"] = _flag("RETURNS_ENABLED", app.config.get("RETURNS_ENABLED", False))
    app.config["RETURNS_FINAL_V1"] = _flag("RETURNS_FINAL_V1", app.config.get("RETURNS_FINAL_V1", False))
    app.config["RETURNS_V2"] = _flag("RETURNS_V2", app.config.get("RETURNS_V2", False))
    app.config["RETURNS_V2_UI"] = _flag("RETURNS_V2_UI", app.config.get("RETURNS_V2_UI", app.config.get("RETURNS_V2", False)))
    app.config["RETURNS_CUSTOMER_PORTAL_ENABLED"] = _flag(
        "RETURNS_CUSTOMER_PORTAL_ENABLED",
        app.config.get("RETURNS_CUSTOMER_PORTAL_ENABLED", True),
    )
    app.config["RETURNS_LABELS_ENABLED"] = _flag(
        "RETURNS_LABELS_ENABLED",
        app.config.get("RETURNS_LABELS_ENABLED", False),
    )
    app.config["RETURNS_REFUNDS_ENABLED"] = _flag(
        "RETURNS_REFUNDS_ENABLED",
        app.config.get("RETURNS_REFUNDS_ENABLED", True),
    )
    app.config["RETURNS_AI_ENABLED"] = _flag("RETURNS_AI_ENABLED", app.config.get("RETURNS_AI_ENABLED", False))
    app.config["AI_ENABLED"] = _flag("AI_ENABLED", app.config.get("AI_ENABLED", False))
    app.config["AI_ENABLE_RAG"] = _flag("AI_ENABLE_RAG", app.config.get("AI_ENABLE_RAG", True))
    app.config["AI_ENABLE_AUDIT"] = _flag("AI_ENABLE_AUDIT", app.config.get("AI_ENABLE_AUDIT", True))
    app.config["AI_ENABLE_PAGE_CONTEXT"] = _flag("AI_ENABLE_PAGE_CONTEXT", app.config.get("AI_ENABLE_PAGE_CONTEXT", True))
    app.config["AI_ENABLE_SUGGESTED_PROMPTS"] = _flag(
        "AI_ENABLE_SUGGESTED_PROMPTS",
        app.config.get("AI_ENABLE_SUGGESTED_PROMPTS", True),
    )
    app.config["AI_ENABLE_GLOSSARY"] = _flag("AI_ENABLE_GLOSSARY", app.config.get("AI_ENABLE_GLOSSARY", True))
    app.config["AI_ENABLE_PROACTIVE_INSIGHTS"] = _flag(
        "AI_ENABLE_PROACTIVE_INSIGHTS",
        app.config.get("AI_ENABLE_PROACTIVE_INSIGHTS", True),
    )
    app.config["AI_ENABLE_WORKFLOW_ASSIST"] = _flag(
        "AI_ENABLE_WORKFLOW_ASSIST",
        app.config.get("AI_ENABLE_WORKFLOW_ASSIST", True),
    )
    app.config["AI_ENABLE_VOICE_READY"] = _flag(
        "AI_ENABLE_VOICE_READY",
        app.config.get("AI_ENABLE_VOICE_READY", True),
    )
    app.config["AI_REQUIRE_TOOL_BACKING_FOR_METRICS"] = _flag(
        "AI_REQUIRE_TOOL_BACKING_FOR_METRICS",
        app.config.get("AI_REQUIRE_TOOL_BACKING_FOR_METRICS", True),
    )
    app.config["AI_PROVIDER"] = (os.getenv("AI_PROVIDER") or app.config.get("AI_PROVIDER") or "ollama").strip().lower()
    app.config["AI_MODEL"] = (os.getenv("AI_MODEL") or app.config.get("AI_MODEL") or "llama3.1:8b-instruct-q4_K_M").strip()
    app.config["AI_MODEL_PATH"] = (os.getenv("AI_MODEL_PATH") or app.config.get("AI_MODEL_PATH") or "").strip()
    app.config["AI_BASE_URL"] = (os.getenv("AI_BASE_URL") or app.config.get("AI_BASE_URL") or "http://127.0.0.1:11434").strip().rstrip("/")
    try:
        app.config["AI_TIMEOUT_SECONDS"] = int(os.getenv("AI_TIMEOUT_SECONDS", app.config.get("AI_TIMEOUT_SECONDS", 25)))
    except Exception:
        app.config["AI_TIMEOUT_SECONDS"] = 25
    try:
        app.config["AI_CONTEXT_WINDOW"] = int(
            os.getenv("AI_CONTEXT_WINDOW", app.config.get("AI_CONTEXT_WINDOW", 4096))
        )
    except Exception:
        app.config["AI_CONTEXT_WINDOW"] = 4096
    try:
        app.config["AI_MAX_TOKENS"] = int(os.getenv("AI_MAX_TOKENS", app.config.get("AI_MAX_TOKENS", 384)))
    except Exception:
        app.config["AI_MAX_TOKENS"] = 384
    try:
        app.config["AI_THREADS"] = int(
            os.getenv("AI_THREADS", app.config.get("AI_THREADS", max(1, min(4, os.cpu_count() or 2))))
        )
    except Exception:
        app.config["AI_THREADS"] = max(1, min(4, os.cpu_count() or 2))
    try:
        app.config["AI_BATCH_SIZE"] = int(os.getenv("AI_BATCH_SIZE", app.config.get("AI_BATCH_SIZE", 256)))
    except Exception:
        app.config["AI_BATCH_SIZE"] = 256
    try:
        app.config["AI_GPU_LAYERS"] = int(os.getenv("AI_GPU_LAYERS", app.config.get("AI_GPU_LAYERS", 0)))
    except Exception:
        app.config["AI_GPU_LAYERS"] = 0
    try:
        app.config["AI_MAX_TOOL_CALLS"] = int(os.getenv("AI_MAX_TOOL_CALLS", app.config.get("AI_MAX_TOOL_CALLS", 6)))
    except Exception:
        app.config["AI_MAX_TOOL_CALLS"] = 6
    try:
        app.config["AI_MAX_PROACTIVE_ITEMS"] = int(
            os.getenv("AI_MAX_PROACTIVE_ITEMS", app.config.get("AI_MAX_PROACTIVE_ITEMS", 6))
        )
    except Exception:
        app.config["AI_MAX_PROACTIVE_ITEMS"] = 6
    if not app.config.get("AI_BASE_URL"):
        app.config["AI_BASE_URL"] = "http://127.0.0.1:11434"
    if int(app.config.get("AI_CONTEXT_WINDOW", 0) or 0) <= 0:
        app.config["AI_CONTEXT_WINDOW"] = 4096
    if int(app.config.get("AI_MAX_TOKENS", 0) or 0) <= 0:
        app.config["AI_MAX_TOKENS"] = 384
    if int(app.config.get("AI_THREADS", 0) or 0) <= 0:
        app.config["AI_THREADS"] = max(1, min(4, os.cpu_count() or 2))
    if int(app.config.get("AI_BATCH_SIZE", 0) or 0) <= 0:
        app.config["AI_BATCH_SIZE"] = 256
    if int(app.config.get("AI_GPU_LAYERS", 0) or 0) < 0:
        app.config["AI_GPU_LAYERS"] = 0
    app.config["AUTHZ_ENFORCEMENT_MODE"] = (
        (os.getenv("AUTHZ_ENFORCEMENT_MODE") or app.config.get("AUTHZ_ENFORCEMENT_MODE") or "warn").strip().lower()
    )
    if app.config["AUTHZ_ENFORCEMENT_MODE"] not in {"warn", "enforce"}:
        app.config["AUTHZ_ENFORCEMENT_MODE"] = "warn"
    # Record app start time for uptime
    try:
        import time as _boot_time
        app.config["STARTED_AT"] = float(_boot_time.time())
    except Exception:
        app.config["STARTED_AT"] = None
    # Fail fast on unsafe/invalid configuration when running in production
    strict_validation = (
        app.config.get("ENV") == "production"
        and not app.config.get("TESTING")
        and not os.getenv("PYTEST_CURRENT_TEST")
    )
    validate_config_settings(app.config, strict=strict_validation)
    # Configure JSON logs to rotating file
    try:
        # Overridable so a second process (a static build, a test run) can keep
        # its own file. Two processes sharing one rotating handler fight over
        # the rename on Windows and each rotation raises into stderr.
        configure_json_logging(app.logger, log_path=os.getenv("LOG_PATH", "logs/app.jsonl"))
        import logging as _logging
        _logging.getLogger("werkzeug").setLevel(_logging.WARNING)
    except Exception:
        pass
    try:
        patch_pandas_logging(app.logger)
        allow_live_sql = str(os.getenv("ALLOW_LIVE_SQL", "")).strip().lower() in {"1", "true", "yes", "on"}
        if allow_live_sql:
            patch_data_loader_logging(app.logger)
    except Exception:
        try:
            app.logger.debug("observability.install_failed", exc_info=True)
        except Exception:
            pass
    # Load feature flags from file (memoized with hot-reload)
    try:
        app.config["APP_FEATURE_FLAGS"] = load_flags()
    except Exception:
        pass
    try:
        app.config["BRANDING"] = load_branding()
    except Exception:
        app.config["BRANDING"] = {"brand_name": "Northgate Retail Analytics", "primary_color": "#0d6efd", "logo_filename": None}

    # Validate fact schema early to surface missing cost/date columns
    try:
        from .services import fact_store as _fact_store  # type: ignore

        app.config["FACT_SCHEMA_STATUS"] = _fact_store.validate_fact_schema(strict=strict_validation)
    except Exception as exc:
        app.config["FACT_SCHEMA_STATUS"] = {"ok": False, "error": str(exc)}
        if strict_validation:
            raise
        app.logger.warning("fact_schema.validation_failed", extra={"error": str(exc)})

    @app.template_filter('number')
    def format_number(value):
        if value is None:
            return ''
        return f"{int(value):,}"  # Formats as integer with commas (e.g., 1234 -> 1,234)
    
    init_extensions(app)
    # Initialize SQLite auth DB and seed admin
    init_auth_db()
    try:
        audit_default_rbac_configuration(app.logger)
    except Exception:
        app.logger.warning("rbac.startup_audit_failed", exc_info=True)
    register_blueprints(app)
    if app.config.get("ADMIN_PORTAL_ENABLED", True):
        try:
            from app.blueprints import admin_api as _admin_api  # type: ignore

            app.add_url_rule(
                "/api/admin/erp-users",
                endpoint="admin_api.erp_users_alias",
                view_func=_admin_api.erp_users,
                methods=["GET"],
            )
            app.add_url_rule(
                "/admin/api/users/search",
                endpoint="admin_api.users_search_alias",
                view_func=_admin_api.users_search,
                methods=["GET"],
            )
            app.add_url_rule(
                "/admin/api/reps/search",
                endpoint="admin_api.reps_search_alias",
                view_func=_admin_api.reps_search,
                methods=["GET"],
            )
            app.add_url_rule(
                "/admin/api/customers/search",
                endpoint="admin_api.customers_search_alias",
                view_func=_admin_api.customers_search,
                methods=["GET"],
            )
        except Exception:
            pass

    @app.context_processor
    def _inject_filter_endpoints():
        try:
            from app.services import fact_store as _fact_store  # type: ignore

            return {
                "filter_api": {
                    "schema_url": url_for("filters_api.schema"),
                    "apply_url": url_for("filters_actions.apply_filters"),
                    "reset_url": url_for("filters_actions.reset_filters"),
                    "dataset_version": _fact_store.cache_buster(),
                },
                "feature_flags": app.config.get("APP_FEATURE_FLAGS", {}),
                "filters_canonical_v2": bool(app.config.get("FILTERS_CANONICAL_V2", False)),
            }
        except Exception:
            return {
                "filter_api": {
                    "schema_url": "/api/filters/schema",
                    "apply_url": "/filters/apply",
                    "reset_url": "/filters/reset",
                    "dataset_version": "",
                },
                "feature_flags": app.config.get("APP_FEATURE_FLAGS", {}),
                "filters_canonical_v2": bool(app.config.get("FILTERS_CANONICAL_V2", False)),
            }

    @app.context_processor
    def _inject_access_scope():
        try:
            return {"access_scope": get_current_scope(use_cache=True)}
        except Exception:
            return {}

    @app.context_processor
    def _inject_permissions():
        try:
            from flask_login import current_user
            from app.auth.permissions import canonical_permission_key
            from app.core.rbac import effective_permissions
            
            login_disabled = bool(app.config.get("LOGIN_DISABLED", False))
            if login_disabled:
                perms = {"*"}
            elif getattr(current_user, "is_authenticated", False):
                perms = effective_permissions(current_user)
            else:
                perms = set()
        except Exception:
            perms = set()

        def _can(permission: str) -> bool:
            try:
                if "*" in perms:
                    return True
                token = canonical_permission_key(permission)
                return bool(token and token in perms)
            except Exception:
                return False

        # `is_read_only` lets a template hide a write control rather than render
        # one that 403s. The guard in app/core/rbac.py is what enforces it; this
        # is only about not offering a dead button to the demo visitor.
        try:
            from .core.rbac import user_is_read_only

            read_only = bool(user_is_read_only())
        except Exception:
            read_only = False

        return {"effective_permissions": perms, "can_permission": _can, "is_read_only": read_only}

    app.register_error_handler(DatasetNotBuiltError, _handle_dataset_not_built)
    _register_cli_commands(app)
    _bootstrap_background_refresh(app)

    # ETL now runs outside the web process; surface a warning instead of rebuilding here.
    try:
        if not app.config.get("TESTING"):
            from app.services import fact_store as _fact_store  # type: ignore

            manifest = _fact_store.get_meta()
            if not manifest:
                app.logger.warning("fact_dataset.missing_manifest", extra={"path": _fact_store.META_PATH.as_posix()})
    except Exception as exc:
        app.logger.warning("cache.ensure_failed", extra={"error": str(exc)})

    @app.before_request
    def _require_login_global():
        try:
            if app.config.get("LOGIN_DISABLED"):
                return None
            if request.method == "OPTIONS":
                return None
            path = request.path or ""
            if path.startswith("/static/"):
                return None
            if path.startswith("/returns/webhooks"):
                return None
            # /healthz is the container liveness probe. It was missing from
            # this list, so the global gate redirected it to the login page and
            # the orchestrator read a healthy process as unhealthy.
            if path in {"/login", "/logout", "/health", "/healthz", "/health/returns", "/favicon.ico"}:
                return None
            if (
                path.startswith("/auth/login")
                or path.startswith("/auth/logout")
                # The one-click demo entry point signs the visitor in, so it has
                # to be reachable while logged out - the whole point of it.
                or path.startswith("/auth/demo")
                or path.startswith("/auth/reset-password")
                or path.startswith("/auth/set-password")
            ):
                return None
            from flask_login import current_user

            if getattr(current_user, "is_authenticated", False):
                return None
            if path.startswith("/api/"):
                payload = {"error": "auth_required", "login_url": url_for("login_alias")}
                resp = jsonify(payload)
                resp.status_code = 401
                return resp
            next_url = request.full_path
            if next_url.endswith("?"):
                next_url = next_url[:-1]
            return redirect(url_for("login_alias", next=next_url))
        except HTTPException:
            raise
        except Exception:
            return None

    @app.before_request
    def _enforce_returns_only_gate():
        try:
            if app.config.get("LOGIN_DISABLED"):
                return None
            from flask_login import current_user

            if not getattr(current_user, "is_authenticated", False):
                return None
            from app.core.rbac import roles_for

            user_roles = roles_for(current_user)
            is_returns_only = bool(getattr(current_user, "returns_only", False) or ("returns_only" in user_roles))
            if not is_returns_only:
                return None

            path = request.path or ""
            endpoint = request.endpoint or ""
            if (
                endpoint == "static"
                or endpoint.startswith("returns_")
                or endpoint.startswith("assistant.")
                or endpoint.startswith("auth.")
            ):
                return None
            if endpoint in {"login_alias", "logout_alias"}:
                return None
            if path.startswith("/static/") or path.startswith("/auth/"):
                return None
            if path in {"/returns", "/returns/", "/health", "/health/returns", "/favicon.ico"} or path.startswith("/returns/"):
                return None
            if path == "/":
                return redirect(url_for("returns_portal.index"))
            if path.startswith("/api/"):
                return _problem_response(403, "Returns-only accounts can access Returns endpoints only.")
            abort(403, description="Returns-only accounts can access Returns endpoints only.")
        except HTTPException:
            raise
        except Exception:
            return None

    @app.before_request
    def _sync_filter_session_keys():  # pragma: no cover
        if app.config.get("FILTERS_CANONICAL_V2", False):
            return
        if not app.config.get("STICKY_FILTERS", True):
            return
        try:
            from flask_login import current_user

            user_id = current_user.get_id() if getattr(current_user, "is_authenticated", False) else None
        except Exception:
            user_id = None
        payload = read_sticky_filters_from_session(session, user_id=user_id)
        if not isinstance(payload, dict):
            return
        try:
            if session.get("filters") != payload:
                session["filters"] = payload
            if session.get("global_filters") != payload:
                session["global_filters"] = payload
            # Promote legacy payloads to the versioned key when needed.
            write_sticky_filters_to_session(session, payload, user_id=user_id)
        except Exception:
            pass

    @app.before_request
    def _capture_global_filters():  # pragma: no cover
        if app.config.get("FILTERS_CANONICAL_V2", False):
            return
        if not app.config.get("STICKY_FILTERS", True):
            return
        try:
            path = (request.path or "").lower()
            blueprint = (request.blueprint or "").lower()
            if blueprint in {"admin", "admin_api"} or path == "/admin" or path.startswith("/admin/") or path.startswith("/api/_admin"):
                return
            source = request.args or request.form
            reset_flag = None
            try:
                reset_flag = source.get("_gf_reset") if hasattr(source, "get") else None
            except Exception:
                reset_flag = None
            reset_requested = str(reset_flag or "").strip().lower() in {"1", "true", "yes", "on"}
            if not reset_requested and request.method in {"POST", "PUT", "PATCH"} and request.is_json:
                try:
                    json_payload = request.get_json(silent=True) or {}
                    reset_flag = json_payload.get("_gf_reset")
                    reset_requested = str(reset_flag or "").strip().lower() in {"1", "true", "yes", "on"}
                except Exception:
                    reset_requested = False
            if reset_requested:
                clear_sticky_filters_in_session(session)
                mark_filters_last_applied(session)
                return
            payload = capture_filters_from(source)
            if payload is None and request.method in {"POST", "PUT", "PATCH"}:
                if request.is_json:
                    json_payload = request.get_json(silent=True) or {}
                    payload = capture_filters_from(json_payload)
            if payload:
                try:
                    from flask_login import current_user

                    user_id = current_user.get_id() if getattr(current_user, "is_authenticated", False) else None
                except Exception:
                    user_id = None
                write_sticky_filters_to_session(session, payload, user_id=user_id)
                mark_filters_last_applied(session)
        except Exception:
            pass

    @app.before_request
    def _enforce_page_permissions():  # pragma: no cover - covered by integration tests
        try:
            if app.config.get("LOGIN_DISABLED") or app.config.get("AUTHZ_DISABLED"):
                return None
            if not app.config.get("AUTHZ_ENFORCEMENT", False) and not app.config.get("ADMIN_PERMISSIONS_V2", False):
                return None
            if request.method == "OPTIONS":
                return None
            path = (request.path or "").lower()
            if (
                path.startswith("/static/")
                or path.startswith("/auth/")
                or path in {"/login", "/logout", "/health", "/healthz", "/readyz", "/favicon.ico"}
            ):
                return None
            from flask_login import current_user

            if not getattr(current_user, "is_authenticated", False):
                return None
            from app.core.rbac import has_any_permission, has_permission, log_rbac_decision, request_permission_policy

            if (path.startswith("/admin") or path.startswith("/api/_admin")) and not app.config.get("ADMIN_PORTAL_ENABLED", True):
                return _problem_response(404, "Not Found")

            policy = request_permission_policy(path)
            if policy is None:
                return None
            required = list(policy.get("all") or ())
            required_any_groups = list(policy.get("any") or ())
            missing = [perm for perm in required if not has_permission(perm)]
            missing_any_groups = [group for group in required_any_groups if not has_any_permission(*group)]
            allowed = not missing and not missing_any_groups
            required_any_flat = tuple(
                {
                    perm
                    for group in required_any_groups
                    for perm in group
                }
            )
            log_rbac_decision(
                allowed=allowed,
                reason="page_permission_granted" if allowed else "missing_permission",
                required_all_perms=tuple(required),
                required_any_perms=required_any_flat,
                user=current_user,
            )
            if allowed:
                return None

            missing_labels = list(missing)
            missing_labels.extend([f"any({', '.join(group)})" for group in missing_any_groups])

            mode = str(app.config.get("AUTHZ_ENFORCEMENT_MODE", "warn") or "warn").strip().lower()
            app.logger.warning(
                "authz.permission_denied",
                extra={
                    "path": path,
                    "user_id": getattr(current_user, "id", None),
                    "role": getattr(current_user, "role", None),
                    "missing_permissions": missing_labels,
                    "mode": mode,
                },
            )
            if mode != "enforce":
                return None
            if path.startswith("/api/"):
                return _problem_response(403, "Forbidden", detail=f"Missing permission(s): {', '.join(missing_labels)}")
            try:
                return render_template(
                    "errors/403.html",
                    required_permissions=missing_labels,
                    user_role=str(getattr(current_user, "role", "") or "").strip().lower(),
                ), 403
            except Exception:
                return _problem_response(403, "Forbidden", detail=f"Missing permission(s): {', '.join(missing_labels)}")
        except Exception:
            return None

    @app.after_request
    def _compress_text_responses(response):  # pragma: no cover - integration behavior
        """Gzip sizeable text payloads without another runtime dependency."""
        try:
            import gzip

            accepted = (request.headers.get("Accept-Encoding") or "").lower()
            if "gzip" not in accepted or response.status_code < 200 or response.status_code in {204, 304}:
                return response
            if response.headers.get("Content-Encoding") or response.status_code == 206:
                return response
            mimetype = (response.mimetype or "").lower()
            compressible = (
                mimetype.startswith("text/")
                or mimetype in {"application/json", "application/javascript", "image/svg+xml"}
            )
            if not compressible:
                return response
            # Flask serves static files in direct-passthrough mode. Disable it
            # only after confirming the response is compressible so CSS/JS get
            # the same transfer savings as JSON without buffering downloads.
            if response.direct_passthrough:
                response.direct_passthrough = False
            body = response.get_data()
            if len(body) < 1024:
                return response
            compressed = gzip.compress(body, compresslevel=5)
            if len(compressed) >= len(body):
                return response
            response.set_data(compressed)
            response.headers["Content-Encoding"] = "gzip"
            response.headers["Content-Length"] = str(len(compressed))
            vary = [item.strip() for item in (response.headers.get("Vary") or "").split(",") if item.strip()]
            if not any(item.lower() == "accept-encoding" for item in vary):
                vary.append("Accept-Encoding")
            response.headers["Vary"] = ", ".join(vary)
        except Exception:
            return response
        return response

    @app.after_request
    def _mask_sensitive_json(response):  # pragma: no cover - integration behavior
        try:
            if app.config.get("LOGIN_DISABLED") or app.config.get("AUTHZ_DISABLED"):
                return response
            if response.status_code >= 400 or not getattr(response, "is_json", False):
                return response
            path = (request.path or "").lower()
            if path.startswith("/api/_admin") or path.startswith("/admin") or path.startswith("/auth/"):
                return response
            from flask_login import current_user
            from app.core.payload_permissions import apply_payload_permissions
            from app.core.sensitive_data import mask_json_payload

            if not getattr(current_user, "is_authenticated", False):
                return response
            payload = response.get_json(silent=True)
            if payload is None:
                return response
            payload = apply_payload_permissions(payload, current_user, path=path)
            masked = mask_json_payload(payload, current_user)
            response.set_data(app.json.dumps(masked))
            response.headers["Content-Length"] = str(len(response.get_data()))
            return response
        except Exception:
            return response

    @app.before_request
    def _hot_reload_flags():  # pragma: no cover
        try:
            app.config["APP_FEATURE_FLAGS"] = get_flags()
        except Exception:
            pass
        try:
            app.config["BRANDING"] = get_branding()
        except Exception:
            pass

    # Per-request logging (request-id + payload fingerprint)
    install_request_logging(app)

    # Read-only roles (the public demo login) can never reach a write route,
    # whatever that route's own decorators happen to say.
    try:
        from .core.rbac import install_read_only_guard

        install_read_only_guard(app)
    except Exception:  # pragma: no cover - never block boot on this
        app.logger.exception("rbac.read_only_guard_install_failed")

    @app.errorhandler(Exception)
    def _handle_uncaught(err):  # pragma: no cover - side-effect logging
        status = 500
        message = "Unexpected server error"
        is_http = isinstance(err, HTTPException)
        if is_http:
            status = err.code or 500
            message = err.description or message
        req_id = getattr(g, "request_id", None)

        # Avoid noisy stack traces for expected 4xx such as missing static files
        if status >= 500:
            app.logger.exception(
                "unhandled_error",
                extra={"request_id": req_id, "path": request.path, "status": status},
            )
        else:
            app.logger.warning(
                "http_error",
                extra={"request_id": req_id, "path": request.path, "status": status, "error": message},
            )

        wants_json = (
            request.is_json
            or request.path.startswith("/api/")
            or request.path.startswith("/products/api")
            or "application/json" in (request.headers.get("Accept") or "")
        )
        if wants_json:
            detail = str(getattr(err, "description", "") or message)
            return _problem_response(status, message, detail=detail)
        try:
            # Every 4xx used to render errors/403.html, so a mistyped or dead
            # URL told the visitor "Forbidden (403) - you do not have permission",
            # which reads as a broken login rather than a wrong address.
            if status >= 500:
                template_name = "errors/500.html"
            elif status == 404:
                template_name = "errors/404.html"
            else:
                template_name = "errors/403.html"
            body = render_template(template_name, error=message, request_id=req_id)
        except Exception:
            body = f"Error: {message}. Request ID: {req_id or ''}"
        response = app.make_response((body, status))
        response.headers["X-Request-ID"] = req_id or ""
        return response

    # Liveness. Deliberately unauthenticated: this is what a container
    # orchestrator polls, and it has no session. Behind @login_required it
    # answered 302, which every health checker reads as unhealthy - so the
    # platform restarts a process that is in fact serving fine.
    #
    # It reports only that this process is alive. Anything that reveals
    # configuration or data state belongs on /readyz, which stays authenticated.
    @app.get("/healthz")
    def healthz():  # pragma: no cover - trivial
        from datetime import datetime, timezone

        return jsonify(
            status="ok",
            time=datetime.now(tz=timezone.utc).isoformat(),
        ), 200

    # Short aliases for auth routes (kept for backward compatibility)
    @app.get("/login")
    def login_alias():  # pragma: no cover - trivial
        return redirect(url_for("auth.login"))

    @app.get("/logout")
    def logout_alias():  # pragma: no cover - trivial
        return redirect(url_for("auth.logout"))

    # Lightweight health for load balancers (includes dataset version)
    @app.get("/health")
    def health():  # pragma: no cover - trivial
        try:
            from app.services import fact_store as _fact_store  # type: ignore
            dataset_version = _fact_store.cache_buster()
        except Exception:
            dataset_version = None
        return jsonify(status="ok", dataset_version=dataset_version), 200

    # Readiness: data, auth DB, and optional MSSQL check
    @app.get("/readyz")
    @login_required
    def readyz():  # pragma: no cover - environment dependent
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        from .auth.models import SessionLocal as _SessionLocal, User as _User
        from app.services import fact_store as _fact_store  # type: ignore

        checks = {}
        ok = True

        # Parquet exists & readable with rows
        manifest = _fact_store.get_meta()
        stale_min = int(os.getenv("STALE_AFTER_MIN", "60"))
        pth = _fact_store.FACT_PATH
        checks["parquet_path"] = str(pth)
        parquet_exists = pth.exists()
        checks["parquet_exists"] = parquet_exists
        built_at = None
        if isinstance(manifest, dict):
            checks["manifest"] = manifest
            checks["rows"] = manifest.get("row_count") or manifest.get("rows")
            built_text = manifest.get("last_refresh_utc") or manifest.get("built_at")
        else:
            checks["manifest"] = None
            checks["rows"] = None
            built_text = None
        if built_text:
            try:
                built_at = _dt.fromisoformat(built_text)
            except Exception:
                built_at = None
        fresh = False
        if built_at:
            age = _dt.now(tz=_tz.utc) - built_at
            checks["parquet_age_seconds"] = int(age.total_seconds())
            fresh = age <= _td(minutes=stale_min)
        else:
            checks["parquet_age_seconds"] = None

        rows_valid = bool(manifest and isinstance(manifest.get("row_count"), int) and manifest.get("row_count", 0) > 0)
        ok = ok and parquet_exists and bool(manifest) and fresh and rows_valid
        checks["rows_valid"] = rows_valid

        # Auth SQLite reachable
        try:
            with _SessionLocal() as s:
                # lightweight check
                _ = s.query(_User).count()
            checks["auth_db"] = "ok"
        except Exception as e:
            checks["auth_db"] = f"error: {str(e)[:300]}"
            ok = False

        # Optional MSSQL ping via loader engine
        try:
            import data_loader as _loader  # type: ignore
            from sqlalchemy import text as _sa_text

            cfg = _loader.get_config()
            if not cfg.server:
                checks["mssql"] = "skipped (MSSQL_SERVER not set)"
            else:
                try:
                    eng = _loader.create_mssql_engine(cfg)
                    with eng.connect() as conn:
                        _ = conn.execute(_sa_text("SELECT 1"))
                    checks["mssql"] = "ok"
                except Exception as e:
                    checks["mssql"] = f"error: {str(e)[:300]}"
                    ok = False
        except Exception as e:
            checks["mssql"] = f"skipped ({str(e)[:100]})"

        status_code = 200 if ok else 503
        payload = {
            "status": "ok" if ok else "fail",
            "time": _dt.now(tz=_tz.utc).isoformat(),
            "env": str(app.config.get("ENV", "")),
            "checks": checks,
        }
        return jsonify(payload), status_code

    # Favicon handler to avoid 404s in browsers
    @app.get("/favicon.ico")
    def favicon():  # pragma: no cover - trivial static
        from flask import send_from_directory as _send_from_directory
        try:
            return _send_from_directory("static", "favicon.svg", mimetype="image/svg+xml")
        except Exception:
            # If missing, just return 204 to silence errors
            return ("", 204)

    # Admin metrics JSON + manual refresh endpoint
    @app.get("/metrics")
    @login_required
    @require_admin
    def metrics():  # pragma: no cover
        import pandas as _pd
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        from pathlib import Path as _Path
        from .auth.models import get_session as _get_session, User as _User, AuditLog as _AuditLog
        import time as _time

        out = {}
        # Uptime
        try:
            started = float(app.config.get("STARTED_AT") or _time.time())
            out["uptime_seconds"] = max(0, int(_time.time() - started))
        except Exception:
            out["uptime_seconds"] = None

        pth = _Path(os.getenv("PARQUET_PATH", "cache/fact_dataset")).resolve()
        try:
            from app.services import fact_store as _fact_store  # type: ignore

            pth = getattr(_fact_store, "FACT_PATH", pth)
            meta = _fact_store.get_meta()  # type: ignore
        except Exception:
            meta = {}
        out["rows"] = 0
        out["parquet_mtime"] = None
        if pth.exists():
            try:
                out["parquet_mtime"] = _dt.fromtimestamp(pth.stat().st_mtime, tz=_tz.utc).isoformat()
                if pth.is_dir():
                    out["rows"] = int(meta.get("row_count") or 0)
                else:
                    try:
                        df = _pd.read_parquet(pth.as_posix(), engine="pyarrow")
                    except Exception:
                        df = _pd.read_parquet(pth.as_posix(), engine="fastparquet")
                    out["rows"] = int(len(df))
            except Exception:
                pass
        marker = pth.parent / ".last_refresh"
        if marker.exists():
            try:
                out["last_refresh"] = _dt.fromtimestamp(marker.stat().st_mtime, tz=_tz.utc).isoformat()
            except Exception:
                out["last_refresh"] = out.get("parquet_mtime")
        else:
            out["last_refresh"] = out.get("parquet_mtime")
        if meta:
            out["watermark"] = meta.get("watermark_dt")
            out["last_refresh_meta"] = meta.get("last_refresh_utc")

        try:
            with _get_session() as s:
                out["users_count"] = int(s.query(_User).count())
        except Exception:
            out["users_count"] = None
        try:
            cutoff = _dt.now(tz=_tz.utc) - _td(hours=24)
            with _get_session() as s:
                out["audits_24h"] = int(s.query(_AuditLog).filter(_AuditLog.ts >= cutoff).count())
        except Exception:
            out["audits_24h"] = None
        return jsonify(out), 200

    @app.get("/health/data")
    @login_required
    @require_admin
    def data_health():  # pragma: no cover - diagnostic
        from app.services import fact_store as _fact_store  # type: ignore

        parquet_path = _fact_store.FACT_PATH
        persisted = {
            "rows": 0,
            "revenue": 0.0,
            "cost": 0.0,
            "cost_missing_rate": 1.0,
            "distinct_products": 0,
            "date_min": None,
            "date_max": None,
            "last_refresh_ts": None,
            "path": parquet_path.as_posix(),
        }
        manifest = _fact_store.get_meta()
        if manifest:
            persisted["rows"] = int(manifest.get("row_count") or manifest.get("rows") or 0)
            persisted["date_min"] = manifest.get("min_date") or manifest.get("date_min")
            persisted["date_max"] = manifest.get("max_date") or manifest.get("date_max")
            persisted["last_refresh_ts"] = manifest.get("last_refresh_utc") or manifest.get("watermark") or manifest.get("built_at")
        loader_version = manifest.get("dataset_version") if isinstance(manifest, dict) else None
        payload = {
            "persisted": persisted,
            "fact_rowcount": persisted.get("rows", 0),
            "manifest": manifest,
            "loader_version": loader_version,
            "cache_buster": _fact_store.cache_buster(),
        }
        return jsonify(payload), 200

    @app.get("/refresh")
    @login_required
    @require_admin
    def refresh():  # pragma: no cover
        return (
            jsonify(
                {
                    "error": "Dataset refresh is disabled on web workers. Run `python run.py build-fact` or schedule the ETL job.",
                }
            ),
            503,
        )

    # Force HTTPS in production behind proxies (based on X-Forwarded-Proto)
    @app.before_request
    def _force_https_redirect():  # pragma: no cover - env dependent
        try:
            if app.config.get("FORCE_HTTPS") and str(app.config.get("ENV", "")).lower() == "production":
                xf_proto = request.headers.get("X-Forwarded-Proto", "http").lower()
                if xf_proto != "https":
                    url = request.url.replace("http://", "https://", 1)
                    return redirect(url, code=308)
        except Exception:
            pass

    @app.after_request
    def _attach_fact_meta(resp):
        # Attach per-request fact meta for debugging when enabled
        try:
            flag = (os.getenv("DATA_DEBUG") or os.getenv("DEBUG_FILTERS") or "").strip().lower()
            debug_on = flag in {"1", "true", "yes", "on"}
        except Exception:
            debug_on = False
        if not debug_on:
            return resp
        try:
            meta = getattr(g, "fact_meta", None)
        except Exception:
            meta = None
        if not meta:
            return resp
        try:
            payload = resp.get_json(silent=True) if hasattr(resp, "get_json") else None
            if isinstance(payload, dict):
                payload.setdefault("meta", meta)
                resp.set_data(json.dumps(payload, default=str))
                resp.mimetype = "application/json"
        except Exception:
            try:
                app.logger.debug("attach_fact_meta_failed", exc_info=True)
            except Exception:
                pass
        return resp

    @app.after_request
    def _attach_cache_headers(resp):
        try:
            meta = getattr(g, "fact_meta", None)
        except Exception:
            meta = None
        if not isinstance(meta, dict):
            return resp
        try:
            if meta.get("cache_hit") is not None:
                resp.headers["X-Cache-Hit"] = str(bool(meta.get("cache_hit"))).lower()
            if meta.get("cache_key"):
                resp.headers["X-Cache-Key"] = str(meta.get("cache_key"))
            if meta.get("cache_age_seconds") is not None:
                resp.headers["X-Cache-Age-Seconds"] = str(int(meta.get("cache_age_seconds") or 0))
        except Exception:
            pass
        return resp

    # Global security headers
    @app.after_request
    def add_security_headers(resp):  # pragma: no cover - header behavior
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer-when-downgrade"
        # `img-src` was the only directive that allowed `data:`, and CSP does
        # not inherit per-type allowances - anything not named falls back to
        # `default-src`, which does not include it. Fonts embedded as data URIs
        # were therefore blocked outright, which is how a chart or icon library
        # that inlines its own typeface silently loses it.
        #
        # `font-src` and `style-src` are named explicitly so the fallback stops
        # deciding for them.
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self' https: 'unsafe-inline' 'unsafe-eval' blob:; "
            "img-src 'self' https: data: blob:; "
            "font-src 'self' https: data:; "
            "style-src 'self' https: 'unsafe-inline';"
        )
        try:
            path = request.path or ""
            is_static = path.startswith("/static/")
            if not is_static:
                from flask_login import current_user
                if getattr(current_user, "is_authenticated", False):
                    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
                    resp.headers["Pragma"] = "no-cache"
                    resp.headers["Expires"] = "0"
                else:
                    resp.headers.setdefault("Cache-Control", "no-store")
        except Exception:
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # Root route handled by dashboard blueprint

    # Prime the bundle caches in the background so the first visitor to a page
    # does not pay for building it. No-op unless DEMO_WARMUP is set.
    try:
        from .core.warmup import start_warmup

        start_warmup(app)
    except Exception:
        app.logger.warning("warmup.start_failed", exc_info=True)

    return app


def _register_cli_commands(app: Flask) -> None:
    @app.cli.command("data-refresh")
    @click.option("--full", is_flag=True, help="Force a full rebuild instead of the incremental window.")
    def data_refresh(full: bool) -> None:
        """Manually refresh analytics data."""

        from app.services import event_bus
        from etl import fact_refresh as etl

        result = etl.build_initial_dataset() if full else etl.incremental_update()
        click.echo(f"ETL refresh complete -> {result.get('path')}")
        try:
            event_bus.publish(
                {
                    "type": "data_refresh",
                    "version": result.get("dataset_version") or result.get("watermark"),
                    "built_at": result.get("last_refresh_utc"),
                }
            )
        except Exception as exc:
            click.echo(f"Warning: failed to broadcast event ({exc})", err=True)

    @app.cli.command("products-build-parquet")
    @click.option("--output", help="Override products parquet output path")
    @click.option("--force", is_flag=True, help="Force rebuild even if the file is fresh")
    def products_build_parquet(output: str | None, force: bool = False) -> None:
        """Manually rebuild the products parquet snapshot."""
        from app.blueprints import products as products_bp

        status = products_bp.rebuild_products_parquet(path=output, force=force)
        click.echo(f"Products parquet -> {status.path}")
        if status.warning:
            click.echo(f"Warning: {status.warning}", err=True)

    @app.cli.command("labor-refresh")
    @click.option(
        "--mode",
        type=click.Choice(["incremental", "recent-repair", "backfill"]),
        default="incremental",
        show_default=True,
        help="Refresh strategy for the labor parquet dataset.",
    )
    @click.option("--start", default=None, help="Optional override start date (YYYY-MM-DD).")
    @click.option("--end", default="today", show_default=True, help="Optional override end date (YYYY-MM-DD or today).")
    @click.option("--keep-prev", is_flag=True, help="Keep the previous dataset directory as a rollback point.")
    @click.option("--skip-raw", is_flag=True, help="Skip raw landing file writes for this run.")
    def labor_refresh(mode: str, start: str | None, end: str | None, keep_prev: bool, skip_raw: bool) -> None:
        """Refresh the Synerion labor parquet dataset."""
        from app.services import labor_etl

        if mode == "backfill":
            result = labor_etl.backfill_labor(start=start, end=end, keep_prev=keep_prev, write_raw=not skip_raw)
        elif mode == "recent-repair":
            result = labor_etl.repair_recent_labor(end=end, keep_prev=keep_prev, write_raw=not skip_raw)
        else:
            result = labor_etl.refresh_labor(mode="incremental", start=start, end=end, keep_prev=keep_prev, write_raw=not skip_raw)
        click.echo(json.dumps(result, indent=2, default=str))
