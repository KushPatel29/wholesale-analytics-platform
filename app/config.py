import os
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import List, Dict


_BOOL_TRUE = {"1", "true", "yes", "on", "y", "t"}
_BOOL_FALSE = {"0", "false", "no", "off", "n"}


def _coerce_bool(value: str | bool | None, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in _BOOL_TRUE:
        return True
    if normalized in _BOOL_FALSE:
        return False
    try:
        return bool(int(normalized))
    except Exception:
        return default


def _get_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    return _coerce_bool(val, default)


def _get_int(name: str, default: int) -> int:
    val = os.getenv(name)
    try:
        return int(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _get_float(name: str, default: float) -> float:
    val = os.getenv(name)
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _get_list(name: str, default: List[str] | None = None) -> List[str]:
    val = os.getenv(name)
    if not val:
        return default or []
    return [item.strip() for item in val.split(',') if item.strip()]


_CONFIG_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_PARQUET_PATH = (_CONFIG_ROOT / "cache" / "fact_dataset").resolve().as_posix()
_DEFAULT_DATA_DIR = (_CONFIG_ROOT / "cache").resolve()
_DEFAULT_PRODUCTS_PARQUET = (_DEFAULT_DATA_DIR / "products.parquet").resolve().as_posix()
_DEFAULT_LABOR_DIR = (_DEFAULT_DATA_DIR / "labor").resolve()
_DEFAULT_LABOR_DATASET = (_DEFAULT_LABOR_DIR / "fact_dataset").resolve().as_posix()
_DEFAULT_LABOR_RAW = (_DEFAULT_LABOR_DIR / "raw").resolve().as_posix()


@dataclass
class Config:
    # Environment
    ENV: str = (os.getenv("ENV") or os.getenv("FLASK_ENV", "production")).strip().lower()
    DEBUG: bool = _get_bool("DEBUG", False)
    TESTING: bool = _get_bool("TESTING", False)
    FORCE_HTTPS: bool = _get_bool("FORCE_HTTPS", False)
    DEV_INSECURE_COOKIES: bool = _get_bool("DEV_INSECURE_COOKIES", True)
    DEFAULT_MONTH_WINDOW: int = _get_int("DEFAULT_MONTH_WINDOW", 3)

    # Flask settings
    SECRET_KEY: str = os.getenv("SECRET_KEY", "")
    WTF_CSRF_ENABLED: bool = True
    LOGIN_DISABLED: bool = _get_bool("LOGIN_DISABLED", False)
    STICKY_FILTERS: bool = _get_bool("STICKY_FILTERS", True)
    FILTERS_CANONICAL_V2: bool = _get_bool("FILTERS_CANONICAL_V2", False)
    PRODUCT_INTELLIGENCE_V2: bool = _get_bool("PRODUCT_INTELLIGENCE_V2", False)
    PRODUCTS_V3: bool = _get_bool("PRODUCTS_V3", False)
    PRODUCTS_V4: bool = _get_bool("PRODUCTS_V4", False)
    PRODUCT_DRILLDOWN_V2: bool = _get_bool("PRODUCT_DRILLDOWN_V2", False)
    PRODUCT_FORECAST_V1: bool = _get_bool("PRODUCT_FORECAST_V1", False)
    NOTIFICATIONS_ENABLED: bool = _get_bool("NOTIFICATIONS_ENABLED", False)
    ADMIN_NOTIF_DEFAULTS: bool = _get_bool("ADMIN_NOTIF_DEFAULTS", False)
    SALESREP_DRILLDOWN_V2: bool = _get_bool("SALESREP_DRILLDOWN_V2", False)
    SALESREPS_V2: bool = _get_bool("SALESREPS_V2", False)
    CUSTOMERS_KPIS_V2: bool = _get_bool("CUSTOMERS_KPIS_V2", False)
    CUSTOMERS_KPIS_V3: bool = _get_bool("CUSTOMERS_KPIS_V3", False)
    CUSTOMERS_RFM_V2: bool = _get_bool("CUSTOMERS_RFM_V2", False)
    CUSTOMERS_CLV_V2: bool = _get_bool("CUSTOMERS_CLV_V2", False)
    CUSTOMER_DRILLDOWN_V2: bool = _get_bool("CUSTOMER_DRILLDOWN_V2", False)
    SUPPLIERS_V2: bool = _get_bool("SUPPLIERS_V2", False)
    SUPPLIER_DRILLDOWN_V2: bool = _get_bool("SUPPLIER_DRILLDOWN_V2", False)
    OVERVIEW_V2: bool = _get_bool("OVERVIEW_V2", False)
    OVERVIEW_V3: bool = _get_bool("OVERVIEW_V3", False)
    OVERVIEW_V2_ADMIN_ONLY: bool = _get_bool("OVERVIEW_V2_ADMIN_ONLY", False)
    OVERVIEW_FORECAST_V2: bool = _get_bool("OVERVIEW_FORECAST_V2", False)
    OVERVIEW_MOVERS_FAST: bool = _get_bool("OVERVIEW_MOVERS_FAST", False)
    COHORTS_V2: bool = _get_bool("COHORTS_V2", False)
    ADMIN_USER_SELECT: bool = _get_bool("ADMIN_USER_SELECT", True)
    ADMIN_PORTAL_ENABLED: bool = _get_bool("ADMIN_PORTAL_ENABLED", True)
    AUTHZ_ENFORCEMENT: bool = _get_bool("AUTHZ_ENFORCEMENT", False)
    AUTHZ_ENFORCEMENT_MODE: str = (os.getenv("AUTHZ_ENFORCEMENT_MODE", "warn") or "warn").strip().lower()
    AUTHZ_DB_PERMISSIONS: bool = _get_bool("AUTHZ_DB_PERMISSIONS", True)

    # Session / cookies
    SESSION_COOKIE_HTTPONLY: bool = True
    SESSION_COOKIE_SAMESITE: str = "Lax"
    SESSION_COOKIE_SECURE: bool = True
    PERMANENT_SESSION_LIFETIME: timedelta = timedelta(hours=12)
    REMEMBER_COOKIE_SECURE: bool = True
    REMEMBER_COOKIE_HTTPONLY: bool = True
    REMEMBER_COOKIE_SAMESITE: str = "Lax"
    REMEMBER_COOKIE_DURATION: timedelta = timedelta(days=14)

    # Rate limiting
    RATELIMIT_ENABLED: bool = _get_bool("RATELIMIT_ENABLED", True)

    # Mail + invite/reset flow
    SMTP_SERVER: str = (os.getenv("SMTP_SERVER", "") or "").strip()
    SMTP_PORT: int = _get_int("SMTP_PORT", 25)
    SMTP_USE_TLS: bool = _get_bool("SMTP_USE_TLS", False)
    SMTP_TIMEOUT_SECONDS: int = _get_int("SMTP_TIMEOUT_SECONDS", 20)
    MAIL_FROM: str = (os.getenv("MAIL_FROM", "Northgate Retail Analytics <no-reply@northgateretail.example.com>") or "").strip()
    MAIL_SUPPRESS_SEND: bool = _get_bool("MAIL_SUPPRESS_SEND", False)
    INVITES_ENABLED: bool = _get_bool("INVITES_ENABLED", True)
    NOTIFICATIONS_MAX_EMAILS_PER_HOUR: int = _get_int("NOTIFICATIONS_MAX_EMAILS_PER_HOUR", 10)
    RESET_TOKEN_TTL_SECONDS: int = _get_int("RESET_TOKEN_TTL_SECONDS", 86400)
    RESET_TOKEN_PEPPER: str | None = os.getenv("RESET_TOKEN_PEPPER")
    APP_PUBLIC_BASE_URL: str = (os.getenv("APP_PUBLIC_BASE_URL", "") or "").strip()
    RETURNS_ENABLED: bool = _get_bool("RETURNS_ENABLED", False)
    RETURNS_FINAL_V1: bool = _get_bool("RETURNS_FINAL_V1", False)
    RETURNS_V2: bool = _get_bool("RETURNS_V2", False)
    RETURNS_V2_UI: bool = _get_bool("RETURNS_V2_UI", _get_bool("RETURNS_V2", False))
    RETURNS_UI_EXCEL_FORM: bool = _get_bool(
        "RETURNS_UI_EXCEL_FORM",
        _get_bool("RETURNS_V2_UI", _get_bool("RETURNS_V2", False)),
    )
    RETURNS_AUTOFILL_ORDER: bool = _get_bool(
        "RETURNS_AUTOFILL_ORDER",
        _get_bool("RETURNS_UI_EXCEL_FORM", _get_bool("RETURNS_V2_UI", _get_bool("RETURNS_V2", False))),
    )
    RETURNS_ANALYTICS: bool = _get_bool("RETURNS_ANALYTICS", False)
    RETURNS_CUSTOMER_PORTAL_ENABLED: bool = _get_bool("RETURNS_CUSTOMER_PORTAL_ENABLED", True)
    RETURNS_LABELS_ENABLED: bool = _get_bool("RETURNS_LABELS_ENABLED", False)
    RETURNS_REFUNDS_ENABLED: bool = _get_bool("RETURNS_REFUNDS_ENABLED", True)
    RETURNS_AI_ENABLED: bool = _get_bool("RETURNS_AI_ENABLED", False)
    AI_ENABLED: bool = _get_bool("AI_ENABLED", False)
    AI_PROVIDER: str = (os.getenv("AI_PROVIDER", "ollama") or "ollama").strip().lower()
    AI_MODEL: str = (os.getenv("AI_MODEL", "llama3.1:8b-instruct-q4_K_M") or "llama3.1:8b-instruct-q4_K_M").strip()
    AI_BASE_URL: str = (os.getenv("AI_BASE_URL", "http://127.0.0.1:11434") or "http://127.0.0.1:11434").strip()
    AI_MODEL_PATH: str = (os.getenv("AI_MODEL_PATH", "") or "").strip()
    AI_TIMEOUT_SECONDS: int = _get_int("AI_TIMEOUT_SECONDS", 25)
    AI_CONTEXT_WINDOW: int = _get_int("AI_CONTEXT_WINDOW", 4096)
    AI_MAX_TOKENS: int = _get_int("AI_MAX_TOKENS", 384)
    AI_THREADS: int = _get_int("AI_THREADS", max(1, min(4, os.cpu_count() or 2)))
    AI_BATCH_SIZE: int = _get_int("AI_BATCH_SIZE", 256)
    AI_GPU_LAYERS: int = _get_int("AI_GPU_LAYERS", 0)
    AI_MAX_TOOL_CALLS: int = _get_int("AI_MAX_TOOL_CALLS", 6)
    AI_ENABLE_RAG: bool = _get_bool("AI_ENABLE_RAG", True)
    AI_ENABLE_AUDIT: bool = _get_bool("AI_ENABLE_AUDIT", True)
    AI_ENABLE_PAGE_CONTEXT: bool = _get_bool("AI_ENABLE_PAGE_CONTEXT", True)
    AI_ENABLE_SUGGESTED_PROMPTS: bool = _get_bool("AI_ENABLE_SUGGESTED_PROMPTS", True)
    AI_ENABLE_GLOSSARY: bool = _get_bool("AI_ENABLE_GLOSSARY", True)
    AI_ENABLE_PROACTIVE_INSIGHTS: bool = _get_bool("AI_ENABLE_PROACTIVE_INSIGHTS", True)
    AI_ENABLE_WORKFLOW_ASSIST: bool = _get_bool("AI_ENABLE_WORKFLOW_ASSIST", True)
    AI_ENABLE_VOICE_READY: bool = _get_bool("AI_ENABLE_VOICE_READY", True)
    AI_MAX_PROACTIVE_ITEMS: int = _get_int("AI_MAX_PROACTIVE_ITEMS", 6)
    AI_REQUIRE_TOOL_BACKING_FOR_METRICS: bool = _get_bool("AI_REQUIRE_TOOL_BACKING_FOR_METRICS", True)
    RETURNS_MARGIN_TARGET: float = _get_float("RETURNS_MARGIN_TARGET", 0.27)
    RETURNS_FREQUENT_THRESHOLD: int = _get_int("RETURNS_FREQUENT_THRESHOLD", 3)
    RETURNS_POLICY_DAYS: int = _get_int("RETURNS_POLICY_DAYS", 14)
    RETURNS_ORDER_LOOKUP_CACHE_SECONDS: int = _get_int("RETURNS_ORDER_LOOKUP_CACHE_SECONDS", 180)
    RETURNS_UPLOAD_DIR: str = os.getenv(
        "RETURNS_UPLOAD_DIR",
        (_CONFIG_ROOT / "instance" / "returns_uploads").as_posix(),
    )
    RETURNS_WEBHOOK_SECRET: str = (os.getenv("RETURNS_WEBHOOK_SECRET", "") or "").strip()

    # Data paths and refresh
    DATA_DIR: str = os.getenv("DATA_DIR", _DEFAULT_DATA_DIR.as_posix())
    CACHE_DIR: str = os.getenv("CACHE_DIR", _DEFAULT_DATA_DIR.as_posix())
    PARQUET_PATH: str = os.getenv("PARQUET_PATH", _DEFAULT_PARQUET_PATH)
    CUSTOMER_REP_HISTORY_PATH: str = (os.getenv("CUSTOMER_REP_HISTORY_PATH", "") or "").strip()
    TERRITORY_REP_HISTORY_PATH: str = (os.getenv("TERRITORY_REP_HISTORY_PATH", "") or "").strip()
    CUSTOMER_TERRITORY_HISTORY_PATH: str = (os.getenv("CUSTOMER_TERRITORY_HISTORY_PATH", "") or "").strip()
    SALESREP_SUCCESSION_PATH: str = (os.getenv("SALESREP_SUCCESSION_PATH", "") or "").strip()
    PRODUCTS_PARQUET_PATH: str = (
        os.getenv("PRODUCTS_PARQUET_PATH")
        or os.getenv("PRODUCTS_SALES_PARQUET")
        or _DEFAULT_PRODUCTS_PARQUET
    )
    PRODUCTS_PARQUET_SCHEMA_VERSION: str = os.getenv("PRODUCTS_PARQUET_SCHEMA_VERSION", "1")
    PRODUCTS_SALES_PARQUET: str = field(init=False)
    LABOR_ANALYTICS_ENABLED: bool = _get_bool("LABOR_ANALYTICS_ENABLED", True)
    LABOR_PAGE_DEFAULT_DAYS: int = _get_int("LABOR_PAGE_DEFAULT_DAYS", 90)
    SYNERION_BASE_URL: str = (os.getenv("SYNERION_BASE_URL", "https://api.synerionagile.com") or "").strip()
    SYNERION_USERNAME: str = (os.getenv("SYNERION_USERNAME", "") or "").strip()
    SYNERION_PASSWORD: str = (os.getenv("SYNERION_PASSWORD", "") or "").strip()
    SYNERION_API_KEY: str = (os.getenv("SYNERION_API_KEY", "") or "").strip()
    SYNERION_SUBDOMAIN: str = (os.getenv("SYNERION_SUBDOMAIN", "") or "").strip()
    SYNERION_APP_REGION: str = (os.getenv("SYNERION_APP_REGION", "CAE") or "CAE").strip()
    SYNERION_PER_PAGE: int = _get_int("SYNERION_PER_PAGE", 100)
    SYNERION_CONNECT_TIMEOUT_SECONDS: int = _get_int("SYNERION_CONNECT_TIMEOUT_SECONDS", 10)
    SYNERION_READ_TIMEOUT_SECONDS: int = _get_int("SYNERION_READ_TIMEOUT_SECONDS", 60)
    SYNERION_MAX_RETRIES: int = _get_int("SYNERION_MAX_RETRIES", 4)
    SYNERION_BACKOFF_FACTOR: float = _get_float("SYNERION_BACKOFF_FACTOR", 1.0)
    LABOR_START_DATE: str = (os.getenv("LABOR_START_DATE", "2022-01-01") or "2022-01-01").strip()
    LABOR_PARQUET_PATH: str = os.getenv("LABOR_PARQUET_PATH", _DEFAULT_LABOR_DATASET)
    LABOR_RAW_PATH: str = os.getenv("LABOR_RAW_PATH", _DEFAULT_LABOR_RAW)
    LABOR_INCREMENTAL_DAYS: int = _get_int("LABOR_INCREMENTAL_DAYS", 7)
    LABOR_RECENT_RELOAD_DAYS: int = _get_int("LABOR_RECENT_RELOAD_DAYS", 45)
    AUTO_REFRESH: bool = _get_bool("AUTO_REFRESH", False)
    REFRESH_EVERY_MIN: int = _get_int("REFRESH_EVERY_MIN", 60)
    ENABLE_INPROCESS_REFRESH: bool = field(init=False)
    ALLOW_GUNICORN_INPROCESS_REFRESH: bool = _get_bool("ALLOW_GUNICORN_INPROCESS_REFRESH", False)
    FACT_REFRESH_INTERVAL_SECONDS: int = _get_int("FACT_REFRESH_INTERVAL_SECONDS", 300)
    FACT_REFRESH_LOOKBACK_DAYS: int = _get_int("FACT_REFRESH_LOOKBACK_DAYS", 14)
    FACT_REFRESH_JITTER_SECONDS: int = _get_int("FACT_REFRESH_JITTER_SECONDS", 15)
    AUTO_CREATE_PRODUCTS_PARQUET: bool = field(init=False)
    ORDER_STATUSES: List[str] = field(default_factory=lambda: _get_list("ORDER_STATUSES", ["packed", "invoiced", "shipped", "delivered"]))
    ENABLE_SSE: bool = _get_bool("ENABLE_SSE", True)

    # SQLAlchemy (optional; not configured by default)
    SQLALCHEMY_DATABASE_URI: str | None = os.getenv("DATABASE_URL")
    SQLALCHEMY_TRACK_MODIFICATIONS: bool = False

    # MSSQL (for data_loader.py usage)
    MSSQL_SERVER: str | None = os.getenv("MSSQL_SERVER")
    MSSQL_DB: str | None = os.getenv("MSSQL_DB", "Northgate Retail Analytics")
    MSSQL_USER: str | None = os.getenv("MSSQL_USER")
    MSSQL_PASSWORD: str | None = os.getenv("MSSQL_PASSWORD")
    MSSQL_TRUSTED: bool = _get_bool("MSSQL_TRUSTED", True)

    # Feature flags
    APP_FEATURE_FLAGS: Dict[str, bool] = field(default_factory=lambda: {
        "enable_churn": True,
        "enable_prophet": True,
        "enable_2fa": True,
        "enable_legacy_pandas_endpoints": False,
    })

    def __post_init__(self):
        # Normalize ENV
        if self.ENV not in {"production", "development"}:
            self.ENV = "production"

        if self.SMTP_PORT <= 0:
            self.SMTP_PORT = 25
        if self.SMTP_TIMEOUT_SECONDS <= 0:
            self.SMTP_TIMEOUT_SECONDS = 20
        if self.NOTIFICATIONS_MAX_EMAILS_PER_HOUR <= 0:
            self.NOTIFICATIONS_MAX_EMAILS_PER_HOUR = 10
        if self.RESET_TOKEN_TTL_SECONDS <= 0:
            self.RESET_TOKEN_TTL_SECONDS = 86400
        if self.RETURNS_MARGIN_TARGET <= 0:
            self.RETURNS_MARGIN_TARGET = 0.27
        if self.RETURNS_FREQUENT_THRESHOLD <= 0:
            self.RETURNS_FREQUENT_THRESHOLD = 3
        if self.RETURNS_POLICY_DAYS <= 0:
            self.RETURNS_POLICY_DAYS = 14
        if self.RETURNS_ORDER_LOOKUP_CACHE_SECONDS <= 0:
            self.RETURNS_ORDER_LOOKUP_CACHE_SECONDS = 180
        if self.AI_TIMEOUT_SECONDS <= 0:
            self.AI_TIMEOUT_SECONDS = 25
        if self.AI_CONTEXT_WINDOW <= 0:
            self.AI_CONTEXT_WINDOW = 4096
        if self.AI_MAX_TOKENS <= 0:
            self.AI_MAX_TOKENS = 384
        if self.AI_THREADS <= 0:
            self.AI_THREADS = max(1, min(4, os.cpu_count() or 2))
        if self.AI_BATCH_SIZE <= 0:
            self.AI_BATCH_SIZE = 256
        if self.AI_GPU_LAYERS < 0:
            self.AI_GPU_LAYERS = 0
        if self.AI_MAX_TOOL_CALLS <= 0:
            self.AI_MAX_TOOL_CALLS = 6
        if self.AI_MAX_PROACTIVE_ITEMS <= 0:
            self.AI_MAX_PROACTIVE_ITEMS = 6
        if not self.AI_PROVIDER:
            self.AI_PROVIDER = "ollama"
        if not self.AI_MODEL:
            self.AI_MODEL = "llama3.1:8b-instruct-q4_K_M"
        self.AI_MODEL_PATH = str(self.AI_MODEL_PATH or "").strip()
        self.AI_BASE_URL = self.AI_BASE_URL.rstrip("/")
        if not self.AI_BASE_URL:
            self.AI_BASE_URL = "http://127.0.0.1:11434"
        self.APP_PUBLIC_BASE_URL = self.APP_PUBLIC_BASE_URL.rstrip("/")

        try:
            self.DATA_DIR = Path(self.DATA_DIR).expanduser().resolve().as_posix()
        except Exception:
            self.DATA_DIR = _DEFAULT_DATA_DIR.as_posix()
        try:
            self.CACHE_DIR = Path(self.CACHE_DIR or self.DATA_DIR).expanduser().resolve().as_posix()
        except Exception:
            self.CACHE_DIR = self.DATA_DIR
        try:
            self.RETURNS_UPLOAD_DIR = Path(self.RETURNS_UPLOAD_DIR).expanduser().resolve().as_posix()
        except Exception:
            self.RETURNS_UPLOAD_DIR = (_CONFIG_ROOT / "instance" / "returns_uploads").resolve().as_posix()

        try:
            self.PRODUCTS_PARQUET_PATH = Path(
                self.PRODUCTS_PARQUET_PATH or (_DEFAULT_PRODUCTS_PARQUET)
            ).expanduser().resolve().as_posix()
        except Exception:
            self.PRODUCTS_PARQUET_PATH = _DEFAULT_PRODUCTS_PARQUET
        try:
            self.LABOR_PARQUET_PATH = Path(
                self.LABOR_PARQUET_PATH or _DEFAULT_LABOR_DATASET
            ).expanduser().resolve().as_posix()
        except Exception:
            self.LABOR_PARQUET_PATH = _DEFAULT_LABOR_DATASET
        try:
            self.LABOR_RAW_PATH = Path(
                self.LABOR_RAW_PATH or _DEFAULT_LABOR_RAW
            ).expanduser().resolve().as_posix()
        except Exception:
            self.LABOR_RAW_PATH = _DEFAULT_LABOR_RAW

        self.PRODUCTS_SALES_PARQUET = self.PRODUCTS_PARQUET_PATH

        # Default to auto-creating the products parquet unless explicitly disabled
        auto_create_default = True
        self.AUTO_CREATE_PRODUCTS_PARQUET = _get_bool("AUTO_CREATE_PRODUCTS_PARQUET", auto_create_default)
        self.SYNERION_BASE_URL = self.SYNERION_BASE_URL.rstrip("/")
        if not self.SYNERION_BASE_URL:
            self.SYNERION_BASE_URL = "https://api.synerionagile.com"
        if self.SYNERION_PER_PAGE <= 0:
            self.SYNERION_PER_PAGE = 100
        if self.SYNERION_CONNECT_TIMEOUT_SECONDS <= 0:
            self.SYNERION_CONNECT_TIMEOUT_SECONDS = 10
        if self.SYNERION_READ_TIMEOUT_SECONDS <= 0:
            self.SYNERION_READ_TIMEOUT_SECONDS = 60
        if self.SYNERION_MAX_RETRIES < 0:
            self.SYNERION_MAX_RETRIES = 4
        if self.SYNERION_BACKOFF_FACTOR < 0:
            self.SYNERION_BACKOFF_FACTOR = 1.0
        if self.LABOR_PAGE_DEFAULT_DAYS <= 0:
            self.LABOR_PAGE_DEFAULT_DAYS = 90
        if self.LABOR_INCREMENTAL_DAYS <= 0:
            self.LABOR_INCREMENTAL_DAYS = 7
        if self.LABOR_RECENT_RELOAD_DAYS <= 0:
            self.LABOR_RECENT_RELOAD_DAYS = 45
        if self.LABOR_RECENT_RELOAD_DAYS < self.LABOR_INCREMENTAL_DAYS:
            self.LABOR_RECENT_RELOAD_DAYS = self.LABOR_INCREMENTAL_DAYS

        inprocess_default = self.ENV != "production"
        self.ENABLE_INPROCESS_REFRESH = _get_bool("ENABLE_INPROCESS_REFRESH", inprocess_default)
        if self.AUTHZ_ENFORCEMENT_MODE not in {"warn", "enforce"}:
            self.AUTHZ_ENFORCEMENT_MODE = "warn"

        if self.ENV == "production":
            # Lockdown defaults in production
            self.DEBUG = False
            self.TESTING = False
            self.SESSION_COOKIE_SECURE = True
            self.REMEMBER_COOKIE_SECURE = True
            self.SESSION_COOKIE_SAMESITE = "Lax"
            self.SESSION_COOKIE_HTTPONLY = True
            self.REMEMBER_COOKIE_HTTPONLY = True
            self.REMEMBER_COOKIE_SAMESITE = "Lax"
        else:
            # Development: allow insecure cookies for local testing
            if self.DEV_INSECURE_COOKIES:
                self.SESSION_COOKIE_SECURE = False
                self.REMEMBER_COOKIE_SECURE = False


def validate_config_settings(cfg: Config | dict, *, strict: bool = True) -> None:
    """
    Fail fast when critical settings are missing or unsafe for production.
    """
    getter = cfg.get if isinstance(cfg, dict) else getattr  # type: ignore[attr-defined]
    errors: list[str] = []

    secret = getter("SECRET_KEY", None)
    if strict and (not secret or str(secret).strip() in {"", "change-me"}):
        errors.append("SECRET_KEY must be set to a strong value in production")

    if strict:
        if not getter("SESSION_COOKIE_SECURE", False):
            errors.append("SESSION_COOKIE_SECURE must be True in production")
        if not getter("SESSION_COOKIE_HTTPONLY", True):
            errors.append("SESSION_COOKIE_HTTPONLY must be True in production")
        same_site = getter("SESSION_COOKIE_SAMESITE", "Lax")
        if str(same_site).lower() not in {"lax", "strict"}:
            errors.append("SESSION_COOKIE_SAMESITE must be Lax or Strict in production")

    # Optional DB validation: if a URI is provided, ensure it is non-empty
    if getter("SQLALCHEMY_DATABASE_URI", None) == "":
        errors.append("SQLALCHEMY_DATABASE_URI is empty; unset or provide a valid URI")

    if errors:
        raise RuntimeError("Invalid configuration: " + "; ".join(errors))
