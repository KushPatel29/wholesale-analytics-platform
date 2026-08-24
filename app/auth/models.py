"""Local authentication models using SQLAlchemy ORM on SQLite.

This auth DB is separate from MSSQL and lives in a SQLite file
`auth.db` stored alongside this module.
"""

from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Optional, Iterable, List, Sequence, Dict, Any, Mapping

from datetime import datetime, timedelta, timezone, date
from sqlalchemy import Column, Integer, String, create_engine, select, Boolean, DateTime, text, Index, ForeignKey, Date, func, insert
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session
from sqlalchemy.types import Text
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
import logging

from .permissions import (
    ALLOWED_ROLES,
    DEFAULT_PERMISSION_CATALOG,
    DEFAULT_ROLE_PERMISSION_KEYS,
    ROLE_ALIASES,
    canonical_permission_key,
    canonicalize_permission_keys,
    ROLE_PERMISSION_SYNC_KEYS,
    SYSTEM_ROLE_DESCRIPTIONS,
)


BASE_DIR = Path(__file__).resolve().parent
_LOG = logging.getLogger(__name__)


def _resolve_auth_db_path() -> Path:
    override = os.getenv("AUTH_DB_PATH") or os.getenv("AUTH_SQLITE_PATH")
    if not override and os.getenv("PYTEST_CURRENT_TEST"):
        # Keep test runs isolated from the production auth DB
        tmp_root = Path(os.getenv("TMPDIR") or os.getenv("TEMP") or BASE_DIR)
        override = tmp_root / "auth_test.db"
    path = Path(override) if override else BASE_DIR / "auth.db"
    try:
        path = path.expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # pragma: no cover - best effort fallback
        _LOG.warning("Failed to prepare auth DB path %s (%s); using module directory.", path, exc)
        path = BASE_DIR / "auth.db"
    return path


DB_PATH = _resolve_auth_db_path()
ENGINE = create_engine(f"sqlite:///{DB_PATH.as_posix()}", connect_args={"check_same_thread": False})
SessionLocal = scoped_session(sessionmaker(bind=ENGINE, autoflush=False, autocommit=False))

Base = declarative_base()


_RBAC_AUDIT_CRITICAL_PERMISSIONS: tuple[str, ...] = (
    "admin.portal.view",
    "admin.users.manage",
    "admin.roles.manage",
    "page.customers.view",
)


def audit_default_rbac_configuration(logger: Any = None) -> list[str]:
    target_logger = logger or _LOG
    warnings: list[str] = []
    try:
        with SessionLocal() as s:
            permission_keys = {
                str(row[0]).strip().lower()
                for row in s.query(Permission.key).all()
                if row and row[0]
            }
            missing_keys = sorted(set(_RBAC_AUDIT_CRITICAL_PERMISSIONS) - permission_keys)
            if missing_keys:
                warnings.append(f"missing_permissions:{','.join(missing_keys)}")

            role_rows = {
                str(role.name or "").strip().lower(): int(role.id)
                for role in s.query(Role).all()
                if getattr(role, "id", None) is not None and str(role.name or "").strip()
            }

            def _role_perm_keys(role_name: str) -> set[str]:
                role_id = role_rows.get(str(role_name or "").strip().lower())
                if role_id is None:
                    warnings.append(f"missing_role:{role_name}")
                    return set()
                rows = (
                    s.query(Permission.key)
                    .join(RolePermission, RolePermission.permission_id == Permission.id)
                    .filter(RolePermission.role_id == int(role_id))
                    .all()
                )
                return {str(row[0]).strip().lower() for row in rows if row and row[0]}

            admin_perms = _role_perm_keys("admin")
            missing_admin_perms = sorted(
                {"admin.portal.view", "admin.users.manage", "admin.roles.manage"} - admin_perms
            )
            if missing_admin_perms:
                warnings.append(f"admin_missing_permissions:{','.join(missing_admin_perms)}")

            sales_perms = _role_perm_keys("sales")
            if sales_perms and "page.customers.view" not in sales_perms:
                warnings.append("sales_missing_permissions:page.customers.view")
    except Exception as exc:
        warnings.append(f"audit_failed:{exc}")

    for warning in warnings:
        try:
            target_logger.warning("rbac.startup_audit", extra={"warning": warning})
        except Exception:
            pass
    return warnings


class User(Base, UserMixin):
    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_email", "email", unique=True),
        Index("ix_users_role", "role"),
        Index("ix_users_status", "is_active", "is_approved"),
    )

    id = Column(Integer, primary_key=True)
    username = Column(String(150), unique=True, nullable=False, index=True)
    email = Column(String(255), unique=True, nullable=True)
    first_name = Column(String(120), nullable=True)
    last_name = Column(String(120), nullable=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(50), nullable=False, default="sales")
    sales_rep_id = Column(String(50), nullable=True)
    erp_user_id = Column(String(50), nullable=True, unique=True)
    region_id = Column(String(50), nullable=True)
    sales_visibility = Column(String(20), nullable=False, default="self")
    totp_secret = Column(String(255), nullable=True)
    totp_confirmed = Column(Boolean, nullable=False, default=False, server_default=text("0"))
    is_active = Column(Boolean, nullable=False, default=False, server_default=text("0"))
    is_approved = Column(Boolean, nullable=False, default=False, server_default=text("0"))
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), server_default=text("CURRENT_TIMESTAMP"))
    last_login_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), server_default=text("CURRENT_TIMESTAMP"))
    must_reset_password = Column(Boolean, nullable=False, default=False, server_default=text("0"))
    returns_only = Column(Boolean, nullable=False, default=False, server_default=text("0"))

    # UserMixin supplies: is_authenticated, is_active, is_anonymous, get_id

    @property
    def full_name(self) -> str:
        first = (self.first_name or "").strip()
        last = (self.last_name or "").strip()
        name = " ".join([p for p in (first, last) if p])
        return name or (self.username or "")

    def set_password(self, password: str) -> None:
        # Speed up hashing in tests when WA_FAST_PWHASH=1 or running under pytest
        fast = os.getenv("WA_FAST_PWHASH") == "1" or bool(os.getenv("PYTEST_CURRENT_TEST"))
        method = "pbkdf2:sha256:1" if fast else "pbkdf2:sha256"
        self.password_hash = generate_password_hash(password, method=method)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "email": self.email,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "full_name": self.full_name,
            "role": self.role,
            "sales_rep_id": self.sales_rep_id,
            "erp_user_id": self.erp_user_id,
            "region_id": self.region_id,
            "sales_visibility": self.sales_visibility,
            "totp_confirmed": bool(self.totp_confirmed),
            "is_active": bool(self.is_active),
            "is_approved": bool(self.is_approved),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "must_reset_password": bool(self.must_reset_password),
            "returns_only": bool(self.returns_only),
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.username} role={self.role}>"


class UserScope(Base):
    __tablename__ = "user_scopes"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    region_ids = Column(Text, nullable=True)
    sales_rep_ids = Column(Text, nullable=True)
    is_super_user = Column(Boolean, nullable=False, default=False, server_default=text("0"))
    max_history_days = Column(Integer, nullable=True)
    effective_start_date = Column(Date, nullable=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    def as_dict(self) -> dict:
        return {
            "region_ids": _split_scope_tokens(self.region_ids),
            "sales_rep_ids": _split_scope_tokens(self.sales_rep_ids),
            "is_super_user": bool(self.is_super_user),
            "max_history_days": self.max_history_days,
            "effective_start_date": self.effective_start_date.isoformat() if self.effective_start_date else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def update_from_payload(self, payload: dict) -> None:
        self.region_ids = _join_scope_tokens(payload.get("region_ids"))
        self.sales_rep_ids = _join_scope_tokens(payload.get("sales_rep_ids"))
        self.is_super_user = bool(payload.get("is_super_user", self.is_super_user))
        self.max_history_days = payload.get("max_history_days", self.max_history_days)
        start_raw = payload.get("effective_start_date")
        if start_raw:
            try:
                self.effective_start_date = date.fromisoformat(str(start_raw))
            except Exception:
                pass
        self.updated_at = datetime.now(timezone.utc)


class Role(Base):
    __tablename__ = "roles"
    __table_args__ = (Index("ix_roles_name", "name", unique=True),)

    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False, unique=True, index=True)
    description = Column(String(255), nullable=True)
    is_system = Column(Boolean, nullable=False, default=True, server_default=text("1"))
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), server_default=text("CURRENT_TIMESTAMP"))


class Permission(Base):
    __tablename__ = "permissions"
    __table_args__ = (Index("ix_permissions_key", "key", unique=True),)

    id = Column(Integer, primary_key=True)
    key = Column(String(128), nullable=False, unique=True, index=True)
    description = Column(String(255), nullable=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), server_default=text("CURRENT_TIMESTAMP"))


class RolePermission(Base):
    __tablename__ = "role_permissions"
    __table_args__ = (
        Index("ix_role_permissions_role", "role_id"),
        Index("ix_role_permissions_permission", "permission_id"),
        Index("ux_role_permissions_pair", "role_id", "permission_id", unique=True),
    )

    id = Column(Integer, primary_key=True)
    role_id = Column(Integer, ForeignKey("roles.id"), nullable=False, index=True)
    permission_id = Column(Integer, ForeignKey("permissions.id"), nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), server_default=text("CURRENT_TIMESTAMP"))


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (
        Index("ix_user_roles_user", "user_id"),
        Index("ix_user_roles_role", "role_id"),
        Index("ux_user_roles_pair", "user_id", "role_id", unique=True),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    role_id = Column(Integer, ForeignKey("roles.id"), nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), server_default=text("CURRENT_TIMESTAMP"))


class UserPermission(Base):
    __tablename__ = "user_permissions"
    __table_args__ = (
        Index("ix_user_permissions_user", "user_id"),
        Index("ix_user_permissions_permission", "permission_id"),
        Index("ux_user_permissions_pair", "user_id", "permission_id", unique=True),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    permission_id = Column(Integer, ForeignKey("permissions.id"), nullable=False, index=True)
    mode = Column(String(16), nullable=False, default="allow", server_default=text("'allow'"))
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), server_default=text("CURRENT_TIMESTAMP"))


class UserScopeRule(Base):
    __tablename__ = "user_scope_rules"
    __table_args__ = (
        Index("ix_user_scope_rules_user", "user_id"),
        Index("ix_user_scope_rules_type", "scope_type"),
        Index("ix_user_scope_rules_value", "scope_value"),
        Index("ux_user_scope_rules", "user_id", "scope_type", "scope_value", "scope_mode", unique=True),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    scope_type = Column(String(32), nullable=False, index=True)  # rep | customer | region | supplier
    scope_value = Column(String(128), nullable=False, index=True)
    scope_mode = Column(String(16), nullable=False, default="allow", server_default=text("'allow'"))
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), server_default=text("CURRENT_TIMESTAMP"))


class ScopeGroup(Base):
    __tablename__ = "scope_groups"
    __table_args__ = (
        Index("ix_scope_groups_name", "name", unique=True),
        Index("ix_scope_groups_type", "scope_type"),
    )

    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False, unique=True, index=True)
    description = Column(String(255), nullable=True)
    scope_type = Column(String(32), nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), server_default=text("CURRENT_TIMESTAMP"))


class ScopeGroupMember(Base):
    __tablename__ = "scope_group_members"
    __table_args__ = (
        Index("ix_scope_group_members_group", "group_id"),
        Index("ix_scope_group_members_value", "scope_value"),
        Index("ux_scope_group_members_pair", "group_id", "scope_value", unique=True),
    )

    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("scope_groups.id"), nullable=False, index=True)
    scope_value = Column(String(128), nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), server_default=text("CURRENT_TIMESTAMP"))


class UserScopeGroup(Base):
    __tablename__ = "user_scope_groups"
    __table_args__ = (
        Index("ix_user_scope_groups_user", "user_id"),
        Index("ix_user_scope_groups_group", "group_id"),
        Index("ux_user_scope_groups_pair", "user_id", "group_id", unique=True),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    group_id = Column(Integer, ForeignKey("scope_groups.id"), nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), server_default=text("CURRENT_TIMESTAMP"))


class LoginAttempt(Base):
    __tablename__ = "login_attempts"

    id = Column(Integer, primary_key=True)
    username = Column(String(150), index=True, nullable=False)
    ip = Column(String(64), nullable=False)
    ts = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    success = Column(Boolean, nullable=False, default=False)


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"
    __table_args__ = (
        Index("ix_password_reset_tokens_user", "user_id"),
        Index("ix_password_reset_tokens_expires_at", "expires_at"),
        Index("ix_password_reset_tokens_purpose", "purpose"),
        Index("ix_password_reset_tokens_used_at", "used_at"),
        Index("ix_password_reset_tokens_created_by", "created_by_user_id"),
        Index("ux_password_reset_tokens_token_hash", "token_hash", unique=True),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    purpose = Column(String(20), nullable=False, default="reset", server_default=text("'reset'"))
    created_at = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=text("CURRENT_TIMESTAMP"),
    )
    expires_at = Column(DateTime, nullable=False, index=True)
    used_at = Column(DateTime, nullable=True, index=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    request_ip = Column(String(64), nullable=True)
    request_user_agent = Column(String(255), nullable=True)


def init_auth_db() -> None:
    # Create tables if not exist. On long-lived SQLite DBs, duplicate legacy indexes
    # can still raise "already exists" during metadata index creation; keep startup
    # resilient and let explicit IF NOT EXISTS migrations below converge schema.
    from . import notifications_models as _notifications_models  # noqa: F401
    from app.returns import models as _returns_models  # noqa: F401
    from app import planning_scenario_models as _planning_scenario_models  # noqa: F401
    from app.decision_ops import models as _decision_ops_models  # noqa: F401

    try:
        Base.metadata.create_all(ENGINE, checkfirst=True)
    except Exception as exc:
        msg = str(exc).lower()
        if "already exists" in msg and "index" in msg:
            _LOG.warning("Auth DB create_all index already exists; continuing with safe migrations: %s", exc)
            # SQLite can retain a legacy index whose name now collides with an
            # index declared on another existing table. SQLAlchemy aborts the
            # whole metadata pass at that point, which previously meant every
            # table ordered after the collision (including new bounded
            # contexts) was silently missing. Create each still-missing table
            # independently; checkfirst skips existing tables and preserves all
            # user data.
            for table in Base.metadata.sorted_tables:
                try:
                    table.create(ENGINE, checkfirst=True)
                except Exception as table_exc:  # pragma: no cover - legacy DB convergence
                    _LOG.warning("Auth DB table convergence skipped '%s': %s", table.name, table_exc)
        else:
            raise

    def _safe_exec(conn, sql: str, label: str) -> None:
        try:
            conn.exec_driver_sql(sql)
        except Exception as exc:  # pragma: no cover - defensive logging only
            _LOG.warning("Auth DB migration '%s' failed/skipped: %s", label, exc)

    def _apply_schema_migrations() -> None:
        try:
            with ENGINE.begin() as conn:
                cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)")}

                def add_user_column(name: str, ddl: str) -> None:
                    nonlocal cols
                    if name not in cols:
                        _safe_exec(conn, ddl, f"add users.{name}")
                        cols.add(name)

                add_user_column("email", "ALTER TABLE users ADD COLUMN email VARCHAR(255)")
                add_user_column("first_name", "ALTER TABLE users ADD COLUMN first_name VARCHAR(120)")
                add_user_column("last_name", "ALTER TABLE users ADD COLUMN last_name VARCHAR(120)")
                add_user_column("totp_secret", "ALTER TABLE users ADD COLUMN totp_secret VARCHAR(255)")
                add_user_column("totp_confirmed", "ALTER TABLE users ADD COLUMN totp_confirmed BOOLEAN NOT NULL DEFAULT 0")
                add_user_column("sales_visibility", "ALTER TABLE users ADD COLUMN sales_visibility VARCHAR(20) NOT NULL DEFAULT 'self'")
                add_user_column("is_active", "ALTER TABLE users ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT 0")
                add_user_column("is_approved", "ALTER TABLE users ADD COLUMN is_approved BOOLEAN NOT NULL DEFAULT 0")
                add_user_column("created_at", "ALTER TABLE users ADD COLUMN created_at DATETIME")
                add_user_column("last_login_at", "ALTER TABLE users ADD COLUMN last_login_at DATETIME")
                add_user_column("updated_at", "ALTER TABLE users ADD COLUMN updated_at DATETIME")
                add_user_column("must_reset_password", "ALTER TABLE users ADD COLUMN must_reset_password BOOLEAN NOT NULL DEFAULT 0")
                add_user_column("erp_user_id", "ALTER TABLE users ADD COLUMN erp_user_id VARCHAR(50)")
                add_user_column("returns_only", "ALTER TABLE users ADD COLUMN returns_only BOOLEAN NOT NULL DEFAULT 0")

                if "created_at" in cols:
                    _safe_exec(conn, "UPDATE users SET created_at = COALESCE(created_at, CURRENT_TIMESTAMP)", "backfill users.created_at")
                if "updated_at" in cols:
                    _safe_exec(conn, "UPDATE users SET updated_at = COALESCE(updated_at, CURRENT_TIMESTAMP)", "backfill users.updated_at")
                if "is_active" in cols:
                    _safe_exec(conn, "UPDATE users SET is_active = COALESCE(is_active, 1) WHERE username = 'admin'", "backfill users.is_active")
                if "is_approved" in cols:
                    _safe_exec(conn, "UPDATE users SET is_approved = COALESCE(is_approved, 1) WHERE username = 'admin'", "backfill users.is_approved")
                if "erp_user_id" in cols:
                    _safe_exec(
                        conn,
                        "UPDATE users SET erp_user_id = COALESCE(erp_user_id, sales_rep_id) WHERE erp_user_id IS NULL AND sales_rep_id IS NOT NULL",
                        "backfill users.erp_user_id",
                    )
                if "returns_only" in cols:
                    _safe_exec(
                        conn,
                        "UPDATE users SET returns_only = CASE WHEN LOWER(COALESCE(role, '')) = 'returns_only' THEN 1 ELSE COALESCE(returns_only, 0) END",
                        "backfill users.returns_only",
                    )

                _safe_exec(conn, "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users(email)", "index users.email")
                _safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_users_role ON users(role)", "index users.role")
                _safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_users_status ON users(is_active, is_approved)", "index users.status")
                _safe_exec(conn, "CREATE UNIQUE INDEX IF NOT EXISTS ux_users_erp_user_id ON users(erp_user_id)", "index users.erp_user_id")
                _safe_exec(
                    conn,
                    """
                    CREATE TABLE IF NOT EXISTS password_reset_tokens (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        token_hash VARCHAR(64) NOT NULL UNIQUE,
                        purpose VARCHAR(20) NOT NULL DEFAULT 'reset',
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        expires_at DATETIME NOT NULL,
                        used_at DATETIME,
                        created_by_user_id INTEGER,
                        request_ip VARCHAR(64),
                        request_user_agent VARCHAR(255)
                    )
                    """,
                    "ensure password_reset_tokens",
                )
                token_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(password_reset_tokens)")}
                if token_cols:
                    if "created_by_user_id" not in token_cols:
                        _safe_exec(
                            conn,
                            "ALTER TABLE password_reset_tokens ADD COLUMN created_by_user_id INTEGER",
                            "add password_reset_tokens.created_by_user_id",
                        )
                        token_cols.add("created_by_user_id")
                    if "request_ip" not in token_cols:
                        _safe_exec(
                            conn,
                            "ALTER TABLE password_reset_tokens ADD COLUMN request_ip VARCHAR(64)",
                            "add password_reset_tokens.request_ip",
                        )
                        token_cols.add("request_ip")
                    if "request_user_agent" not in token_cols:
                        _safe_exec(
                            conn,
                            "ALTER TABLE password_reset_tokens ADD COLUMN request_user_agent VARCHAR(255)",
                            "add password_reset_tokens.request_user_agent",
                        )
                        token_cols.add("request_user_agent")
                    if "used_at" not in token_cols:
                        _safe_exec(
                            conn,
                            "ALTER TABLE password_reset_tokens ADD COLUMN used_at DATETIME",
                            "add password_reset_tokens.used_at",
                        )
                        token_cols.add("used_at")
                    if "purpose" not in token_cols:
                        _safe_exec(
                            conn,
                            "ALTER TABLE password_reset_tokens ADD COLUMN purpose VARCHAR(20) NOT NULL DEFAULT 'reset'",
                            "add password_reset_tokens.purpose",
                        )
                        token_cols.add("purpose")
                    if "expires_at" not in token_cols:
                        _safe_exec(
                            conn,
                            "ALTER TABLE password_reset_tokens ADD COLUMN expires_at DATETIME",
                            "add password_reset_tokens.expires_at",
                        )
                        token_cols.add("expires_at")
                    if "created_at" not in token_cols:
                        _safe_exec(
                            conn,
                            "ALTER TABLE password_reset_tokens ADD COLUMN created_at DATETIME",
                            "add password_reset_tokens.created_at",
                        )
                        token_cols.add("created_at")

                _safe_exec(
                    conn,
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_password_reset_tokens_hash ON password_reset_tokens(token_hash)",
                    "index password_reset_tokens.token_hash",
                )
                _safe_exec(
                    conn,
                    "CREATE INDEX IF NOT EXISTS ix_password_reset_tokens_user_id ON password_reset_tokens(user_id)",
                    "index password_reset_tokens.user_id",
                )
                _safe_exec(
                    conn,
                    "CREATE INDEX IF NOT EXISTS ix_password_reset_tokens_expires_at ON password_reset_tokens(expires_at)",
                    "index password_reset_tokens.expires_at",
                )
                _safe_exec(
                    conn,
                    "CREATE INDEX IF NOT EXISTS ix_password_reset_tokens_used_at ON password_reset_tokens(used_at)",
                    "index password_reset_tokens.used_at",
                )
                _safe_exec(
                    conn,
                    "CREATE INDEX IF NOT EXISTS ix_password_reset_tokens_purpose ON password_reset_tokens(purpose)",
                    "index password_reset_tokens.purpose",
                )
                _safe_exec(
                    conn,
                    "CREATE INDEX IF NOT EXISTS ix_password_reset_tokens_created_by ON password_reset_tokens(created_by_user_id)",
                    "index password_reset_tokens.created_by_user_id",
                )

                _safe_exec(
                    conn,
                    """
                    CREATE TABLE IF NOT EXISTS saved_views (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        name VARCHAR(150) NOT NULL,
                        filters_json TEXT NOT NULL,
                        created_at DATETIME NOT NULL
                    )
                    """,
                    "ensure saved_views",
                )
                _safe_exec(
                    conn,
                    """
                    CREATE TABLE IF NOT EXISTS user_scopes (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        region_ids TEXT,
                        sales_rep_ids TEXT,
                        is_super_user BOOLEAN NOT NULL DEFAULT 0,
                        max_history_days INTEGER,
                        effective_start_date DATE,
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL
                    )
                    """,
                    "ensure user_scopes",
                )
                _safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_user_scopes_user_id ON user_scopes(user_id)", "index user_scopes.user_id")
                _safe_exec(
                    conn,
                    """
                    CREATE TABLE IF NOT EXISTS user_visibility_salesrep (
                        id INTEGER PRIMARY KEY,
                        app_user_id INTEGER NOT NULL,
                        visible_erp_user_id VARCHAR(50) NOT NULL,
                        created_at DATETIME NOT NULL
                    )
                    """,
                    "ensure user_visibility_salesrep",
                )
                _safe_exec(
                    conn,
                    "CREATE INDEX IF NOT EXISTS ix_visibility_user ON user_visibility_salesrep(app_user_id)",
                    "index visibility.user",
                )
                _safe_exec(
                    conn,
                    "CREATE INDEX IF NOT EXISTS ix_visibility_erp_user ON user_visibility_salesrep(visible_erp_user_id)",
                    "index visibility.erp_user",
                )
                _safe_exec(
                    conn,
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_visibility_pair ON user_visibility_salesrep(app_user_id, visible_erp_user_id)",
                    "unique visibility pair",
                )
                _safe_exec(
                    conn,
                    """
                    CREATE TABLE IF NOT EXISTS roles (
                        id INTEGER PRIMARY KEY,
                        name VARCHAR(64) NOT NULL UNIQUE,
                        description VARCHAR(255),
                        is_system BOOLEAN NOT NULL DEFAULT 1,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """,
                    "ensure roles",
                )
                _safe_exec(
                    conn,
                    """
                    CREATE TABLE IF NOT EXISTS permissions (
                        id INTEGER PRIMARY KEY,
                        key VARCHAR(128) NOT NULL UNIQUE,
                        description VARCHAR(255),
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """,
                    "ensure permissions",
                )
                _safe_exec(
                    conn,
                    """
                    CREATE TABLE IF NOT EXISTS role_permissions (
                        id INTEGER PRIMARY KEY,
                        role_id INTEGER NOT NULL,
                        permission_id INTEGER NOT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """,
                    "ensure role_permissions",
                )
                _safe_exec(
                    conn,
                    """
                    CREATE TABLE IF NOT EXISTS user_roles (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        role_id INTEGER NOT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """,
                    "ensure user_roles",
                )
                _safe_exec(
                    conn,
                    """
                    CREATE TABLE IF NOT EXISTS user_permissions (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        permission_id INTEGER NOT NULL,
                        mode VARCHAR(16) NOT NULL DEFAULT 'allow',
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """,
                    "ensure user_permissions",
                )
                user_permission_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(user_permissions)")}
                if "mode" not in user_permission_cols:
                    _safe_exec(
                        conn,
                        "ALTER TABLE user_permissions ADD COLUMN mode VARCHAR(16) NOT NULL DEFAULT 'allow'",
                        "add user_permissions.mode",
                    )
                _safe_exec(
                    conn,
                    """
                    CREATE TABLE IF NOT EXISTS user_scope_rules (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        scope_type VARCHAR(32) NOT NULL,
                        scope_value VARCHAR(128) NOT NULL,
                        scope_mode VARCHAR(16) NOT NULL DEFAULT 'allow',
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """,
                    "ensure user_scope_rules",
                )
                _safe_exec(
                    conn,
                    """
                    CREATE TABLE IF NOT EXISTS scope_groups (
                        id INTEGER PRIMARY KEY,
                        name VARCHAR(120) NOT NULL UNIQUE,
                        description VARCHAR(255),
                        scope_type VARCHAR(32) NOT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """,
                    "ensure scope_groups",
                )
                _safe_exec(
                    conn,
                    """
                    CREATE TABLE IF NOT EXISTS scope_group_members (
                        id INTEGER PRIMARY KEY,
                        group_id INTEGER NOT NULL,
                        scope_value VARCHAR(128) NOT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """,
                    "ensure scope_group_members",
                )
                _safe_exec(
                    conn,
                    """
                    CREATE TABLE IF NOT EXISTS user_scope_groups (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        group_id INTEGER NOT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """,
                    "ensure user_scope_groups",
                )
                _safe_exec(
                    conn,
                    """
                    CREATE TABLE IF NOT EXISTS login_attempts (
                        id INTEGER PRIMARY KEY,
                        username VARCHAR(150) NOT NULL,
                        ip VARCHAR(64) NOT NULL,
                        ts DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        success BOOLEAN NOT NULL DEFAULT 0
                    )
                    """,
                    "ensure login_attempts",
                )
                _safe_exec(conn, "CREATE UNIQUE INDEX IF NOT EXISTS ix_roles_name ON roles(name)", "index roles.name")
                _safe_exec(conn, "CREATE UNIQUE INDEX IF NOT EXISTS ix_permissions_key ON permissions(key)", "index permissions.key")
                _safe_exec(
                    conn,
                    "CREATE INDEX IF NOT EXISTS ix_login_attempts_username ON login_attempts(username)",
                    "index login_attempts.username",
                )
                _safe_exec(
                    conn,
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_role_permissions_pair ON role_permissions(role_id, permission_id)",
                    "index role_permissions pair",
                )
                _safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_role_permissions_role ON role_permissions(role_id)", "index role_permissions role")
                _safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_role_permissions_permission ON role_permissions(permission_id)", "index role_permissions permission")
                _safe_exec(
                    conn,
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_user_roles_pair ON user_roles(user_id, role_id)",
                    "index user_roles pair",
                )
                _safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_user_roles_user ON user_roles(user_id)", "index user_roles user")
                _safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_user_roles_role ON user_roles(role_id)", "index user_roles role")
                _safe_exec(
                    conn,
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_user_permissions_pair ON user_permissions(user_id, permission_id)",
                    "index user_permissions pair",
                )
                _safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_user_permissions_user ON user_permissions(user_id)", "index user_permissions user")
                _safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_user_permissions_permission ON user_permissions(permission_id)", "index user_permissions permission")
                _safe_exec(
                    conn,
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_user_scope_rules ON user_scope_rules(user_id, scope_type, scope_value, scope_mode)",
                    "index user_scope_rules pair",
                )
                _safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_user_scope_rules_user ON user_scope_rules(user_id)", "index user_scope_rules user")
                _safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_user_scope_rules_type ON user_scope_rules(scope_type)", "index user_scope_rules type")
                _safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_user_scope_rules_value ON user_scope_rules(scope_value)", "index user_scope_rules value")
                _safe_exec(conn, "CREATE UNIQUE INDEX IF NOT EXISTS ux_scope_groups_name ON scope_groups(name)", "index scope_groups name")
                _safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_scope_groups_type ON scope_groups(scope_type)", "index scope_groups type")
                _safe_exec(
                    conn,
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_scope_group_members_pair ON scope_group_members(group_id, scope_value)",
                    "index scope_group_members pair",
                )
                _safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_scope_group_members_group ON scope_group_members(group_id)", "index scope_group_members group")
                _safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_scope_group_members_value ON scope_group_members(scope_value)", "index scope_group_members value")
                _safe_exec(
                    conn,
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_user_scope_groups_pair ON user_scope_groups(user_id, group_id)",
                    "index user_scope_groups pair",
                )
                _safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_user_scope_groups_user ON user_scope_groups(user_id)", "index user_scope_groups user")
                _safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_user_scope_groups_group ON user_scope_groups(group_id)", "index user_scope_groups group")
                _safe_exec(
                    conn,
                    """
                    CREATE TABLE IF NOT EXISTS notification_types (
                        id INTEGER PRIMARY KEY,
                        key VARCHAR(120) NOT NULL UNIQUE,
                        name VARCHAR(255) NOT NULL,
                        description TEXT,
                        default_config_json TEXT NOT NULL DEFAULT '{}',
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """,
                    "ensure notification_types",
                )
                _safe_exec(
                    conn,
                    """
                    CREATE TABLE IF NOT EXISTS user_notification_prefs (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        type_key VARCHAR(120) NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 0,
                        frequency VARCHAR(20) NOT NULL DEFAULT 'daily',
                        config_json TEXT NOT NULL DEFAULT '{}',
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """,
                    "ensure user_notification_prefs",
                )
                _safe_exec(
                    conn,
                    """
                    CREATE TABLE IF NOT EXISTS notification_events (
                        id INTEGER PRIMARY KEY,
                        type_key VARCHAR(120) NOT NULL,
                        user_id INTEGER NOT NULL,
                        event_hash VARCHAR(64) NOT NULL,
                        event_payload_json TEXT NOT NULL DEFAULT '{}',
                        window_start DATETIME,
                        window_end DATETIME,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        sent_at DATETIME,
                        status VARCHAR(32) NOT NULL DEFAULT 'pending',
                        error TEXT
                    )
                    """,
                    "ensure notification_events",
                )
                _safe_exec(conn, "CREATE UNIQUE INDEX IF NOT EXISTS ix_notification_types_key ON notification_types(key)", "index notification_types.key")
                _safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_user_notification_prefs_user ON user_notification_prefs(user_id)", "index user_notification_prefs.user")
                _safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_user_notification_prefs_type ON user_notification_prefs(type_key)", "index user_notification_prefs.type")
                _safe_exec(
                    conn,
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_user_notification_prefs_pair ON user_notification_prefs(user_id, type_key)",
                    "index user_notification_prefs pair",
                )
                _safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_notification_events_type ON notification_events(type_key)", "index notification_events.type")
                _safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_notification_events_user ON notification_events(user_id)", "index notification_events.user")
                _safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_notification_events_status ON notification_events(status)", "index notification_events.status")
                _safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_notification_events_sent_at ON notification_events(sent_at)", "index notification_events.sent_at")
                _safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_notification_events_created_at ON notification_events(created_at)", "index notification_events.created_at")
                _safe_exec(
                    conn,
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_notification_events_hash ON notification_events(type_key, user_id, event_hash)",
                    "index notification_events hash",
                )
                _returns_models.apply_returns_schema_migrations(conn, _safe_exec)

                audit_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(audit_log)")}
                if audit_cols:
                    if "actor_user_id" not in audit_cols:
                        _safe_exec(conn, "ALTER TABLE audit_log ADD COLUMN actor_user_id INTEGER", "add audit_log.actor_user_id")
                        audit_cols.add("actor_user_id")
                    if "target_user_id" not in audit_cols:
                        _safe_exec(conn, "ALTER TABLE audit_log ADD COLUMN target_user_id INTEGER", "add audit_log.target_user_id")
                        audit_cols.add("target_user_id")
                    if "created_at" not in audit_cols:
                        _safe_exec(conn, "ALTER TABLE audit_log ADD COLUMN created_at DATETIME", "add audit_log.created_at")
                        audit_cols.add("created_at")
                    if "user_agent" not in audit_cols:
                        _safe_exec(conn, "ALTER TABLE audit_log ADD COLUMN user_agent VARCHAR(255)", "add audit_log.user_agent")
                        audit_cols.add("user_agent")
                    if "before_json" not in audit_cols:
                        _safe_exec(conn, "ALTER TABLE audit_log ADD COLUMN before_json TEXT", "add audit_log.before_json")
                        audit_cols.add("before_json")
                    if "after_json" not in audit_cols:
                        _safe_exec(conn, "ALTER TABLE audit_log ADD COLUMN after_json TEXT", "add audit_log.after_json")
                        audit_cols.add("after_json")

                _safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_audit_actor ON audit_log(actor_user_id)", "index audit_log.actor_user_id")
                _safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_audit_target ON audit_log(target_user_id)", "index audit_log.target_user_id")
                _safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_audit_action ON audit_log(action)", "index audit_log.action")
                _safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_audit_created_at ON audit_log(created_at)", "index audit_log.created_at")
        except Exception as exc:  # pragma: no cover - defensive logging only
            _LOG.warning("Auth DB migrations failed: %s", exc)

    _apply_schema_migrations()

    def _env_bool(name: str, default: bool = False) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    def _seed_admin() -> None:
        admin_username = (os.getenv("BOOTSTRAP_ADMIN_USERNAME") or "admin").strip() or "admin"
        admin_password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD") or "admin"
        admin_email = (os.getenv("BOOTSTRAP_ADMIN_EMAIL") or "admin@example.com").strip() or None
        force_password = _env_bool("BOOTSTRAP_ADMIN_FORCE_PASSWORD", False)

        with SessionLocal() as s:
            admin = (
                s.execute(
                    select(User).where(func.lower(User.username) == admin_username.lower())
                )
                .scalars()
                .first()
            )
            if admin is None:
                admin = User(
                    username=admin_username,
                    email=admin_email,
                    role="admin",
                    is_active=True,
                    is_approved=True,
                    must_reset_password=False,
                )
                admin.set_password(admin_password)
                s.add(admin)
                s.commit()
                return

            changed = False
            if (admin.role or "").strip().lower() != "admin":
                admin.role = "admin"
                changed = True
            if not bool(getattr(admin, "is_active", False)):
                admin.is_active = True
                changed = True
            if not bool(getattr(admin, "is_approved", False)):
                admin.is_approved = True
                changed = True
            if bool(getattr(admin, "must_reset_password", False)):
                admin.must_reset_password = False
                changed = True
            if admin_email and not (admin.email or "").strip():
                admin.email = admin_email
                changed = True
            if force_password:
                admin.set_password(admin_password)
                changed = True
            if changed:
                admin.updated_at = datetime.now(timezone.utc)
                s.add(admin)
                s.commit()

    def _normalize_role_name(role: Optional[str]) -> str:
        token = str(role or "").strip().lower()
        token = ROLE_ALIASES.get(token, token)
        if not token:
            return "sales"
        return token

    def _seed_rbac_defaults() -> None:
        sync_permissions()

    def _seed_notification_types() -> None:
        try:
            from app.services.notifications_catalog import NOTIFICATION_CATALOG  # type: ignore
            from app.auth.notifications_models import NotificationType  # type: ignore
        except Exception as exc:
            _LOG.warning("Notification catalog import failed during auth DB seed: %s", exc)
            return

        now = datetime.now(timezone.utc)
        with SessionLocal() as s:
            existing = {
                (row.key or "").strip(): row
                for row in s.query(NotificationType).all()
            }
            changed = False
            for item in NOTIFICATION_CATALOG:
                type_key = str(item.get("key") or "").strip()
                if not type_key:
                    continue
                payload = json.dumps(item.get("default_config") or {}, sort_keys=True, default=str)
                row = existing.get(type_key)
                expected_name = str(item.get("name") or type_key)
                expected_description = str(item.get("description") or "").strip() or None
                if row is None:
                    s.add(
                        NotificationType(
                            key=type_key,
                            name=expected_name,
                            description=expected_description,
                            default_config_json=payload,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    changed = True
                    continue
                row_changed = False
                if (row.name or "") != expected_name:
                    row.name = expected_name
                    row_changed = True
                if (row.description or None) != expected_description:
                    row.description = expected_description
                    row_changed = True
                if (row.default_config_json or "") != payload:
                    row.default_config_json = payload
                    row_changed = True
                if row_changed:
                    row.updated_at = now
                    s.add(row)
                    changed = True
            if changed:
                s.commit()

    try:
        _seed_admin()
        _seed_rbac_defaults()
        _seed_notification_types()
    except Exception as exc:  # pragma: no cover - defensive logging only
        _LOG.warning("Auth DB seed failed; retrying after migrations: %s", exc)
        _apply_schema_migrations()
        _seed_admin()
        _seed_rbac_defaults()
        _seed_notification_types()


def get_session():
    return SessionLocal()


def get_user_by_id(user_id: str | int) -> Optional[User]:
    try:
        uid = int(user_id)
    except Exception:
        return None
    with SessionLocal() as s:
        return s.get(User, uid)


def get_user_by_username(username: str) -> Optional[User]:
    username = (username or "").strip()
    if not username:
        return None
    needle = username.lower()
    with SessionLocal() as s:
        return (
            s.execute(
                select(User).where(
                    (func.lower(User.username) == needle) | (func.lower(User.email) == needle)
                )
            )
            .scalars()
            .first()
        )


def list_saved_views(user: Optional[User], include_all_for_admin: bool = False):
    with SessionLocal() as s:
        if include_all_for_admin and user and (getattr(user, "role", "").lower() == "admin"):
            rows = s.query(SavedView).order_by(SavedView.created_at.desc()).all()
        else:
            uid = int(getattr(user, "id", 0) or 0)
            rows = s.query(SavedView).filter(SavedView.user_id == uid).order_by(SavedView.created_at.desc()).all()
        return rows


def get_saved_view(view_id: int) -> Optional[SavedView]:
    with SessionLocal() as s:
        return s.get(SavedView, view_id)


def save_view(user_id: int, name: str, filters_json: str) -> int:
    with SessionLocal() as s:
        v = SavedView(user_id=user_id, name=name, filters_json=filters_json)
        s.add(v)
        s.commit()
        return int(v.id)


def update_view(view_id: int, *, name: str | None = None, filters_json: str | None = None) -> bool:
    with SessionLocal() as s:
        v = s.get(SavedView, view_id)
        if not v:
            return False
        if name is not None:
            v.name = str(name)
        if filters_json is not None:
            v.filters_json = str(filters_json)
        s.commit()
        return True


def delete_view(view_id: int) -> None:
    with SessionLocal() as s:
        v = s.get(SavedView, view_id)
        if v:
            s.delete(v)
            s.commit()


def record_login_attempt(username: str, ip: str, success: bool) -> None:
    with SessionLocal() as s:
        attempt = LoginAttempt(username=username, ip=ip, success=success, ts=datetime.now(timezone.utc))
        s.add(attempt)
        s.commit()


def is_account_locked(username: str, window_minutes: int = 15, threshold: int = 5) -> bool:
    """Return True if username has >= threshold failed attempts in last window_minutes."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    with SessionLocal() as s:
        stmt = (
            select(LoginAttempt)
            .where(LoginAttempt.username == username)
            .where(LoginAttempt.ts >= cutoff)
            .where(LoginAttempt.success == False)  # noqa: E712
        )
        failures = s.execute(stmt).scalars().all()
        return len(failures) >= threshold


def _split_scope_tokens(raw: Optional[str | Iterable[str]]) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = [p.strip() for p in str(raw).replace(";", ",").replace("|", ",").split(",")]
        return [p for p in parts if p]
    values: List[str] = []
    for item in raw:
        if item is None:
            continue
        val = str(item).strip()
        if val:
            values.append(val)
    return values


def _join_scope_tokens(values: Optional[Sequence[str] | str]) -> Optional[str]:
    if values is None:
        return None
    if isinstance(values, str):
        values = [v.strip() for v in values.replace(";", ",").replace("|", ",").split(",")]
    cleaned = [str(v).strip() for v in values if str(v).strip()]
    return ",".join(cleaned) if cleaned else None


def get_scope_for_user(user_id: int) -> UserScope:
    with SessionLocal() as s:
        scope = s.query(UserScope).filter(UserScope.user_id == int(user_id)).first()
        if not scope:
            now = datetime.now(timezone.utc)
            scope = UserScope(user_id=int(user_id), created_at=now, updated_at=now)
            s.add(scope)
            s.commit()
            s.refresh(scope)
        return scope


def upsert_scope(user_id: int, payload: dict) -> UserScope:
    with SessionLocal() as s:
        scope = s.query(UserScope).filter(UserScope.user_id == int(user_id)).first()
        now = datetime.now(timezone.utc)
        if not scope:
            scope = UserScope(user_id=int(user_id), created_at=now, updated_at=now)
        scope.update_from_payload(payload)
        if not scope.created_at:
            scope.created_at = now
        scope.updated_at = now
        s.add(scope)
        s.commit()
        s.refresh(scope)
        return scope


def list_visibility_for_user(user_id: int) -> List[str]:
    try:
        with SessionLocal() as s:
            rows = (
                s.query(UserVisibilitySalesRep.visible_erp_user_id)
                .filter(UserVisibilitySalesRep.app_user_id == int(user_id))
                .all()
            )
            return [r[0] for r in rows if r and r[0]]
    except Exception:
        return []


def replace_visibility_for_user(user_id: int, erp_user_ids: Sequence[str]) -> List[str]:
    cleaned = _split_scope_tokens(erp_user_ids)
    with SessionLocal() as s:
        s.query(UserVisibilitySalesRep).filter(UserVisibilitySalesRep.app_user_id == int(user_id)).delete()
        for erp_user_id in cleaned:
            s.add(
                UserVisibilitySalesRep(
                    app_user_id=int(user_id),
                    visible_erp_user_id=str(erp_user_id).strip(),
                    created_at=datetime.now(timezone.utc),
                )
            )
        s.commit()
    return cleaned


_SCOPE_TYPE_ALIASES: Dict[str, str] = {
    "rep": "rep",
    "reps": "rep",
    "sales_rep": "rep",
    "sales_reps": "rep",
    "sales_rep_ids": "rep",
    "salesrep_ids": "rep",
    "allowed_erp_user_ids": "rep",
    "customer": "customer",
    "customers": "customer",
    "customer_ids": "customer",
    "region": "region",
    "regions": "region",
    "region_ids": "region",
    "supplier": "supplier",
    "suppliers": "supplier",
    "supplier_ids": "supplier",
}
_SCOPE_TYPES = ("rep", "customer", "region", "supplier")


def _canonical_role_name(role: Optional[str]) -> str:
    token = str(role or "").strip().lower()
    token = ROLE_ALIASES.get(token, token)
    if not token:
        return "sales"
    return token


def _coerce_scope_type(raw: Any) -> Optional[str]:
    token = str(raw or "").strip().lower()
    return _SCOPE_TYPE_ALIASES.get(token)


def _ensure_role_row(session, role_name: str) -> Optional[Role]:
    canonical = _canonical_role_name(role_name)
    role = session.query(Role).filter(func.lower(Role.name) == canonical).first()
    if role:
        return role
    role = Role(
        name=canonical,
        description=f"{canonical} role",
        is_system=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    session.add(role)
    session.flush()
    return role


def _sqlite_insert_or_ignore(session: Any, model: Any, values: Mapping[str, Any]) -> bool:
    result = session.execute(insert(model).values(**dict(values)).prefix_with("OR IGNORE"))
    try:
        return int(result.rowcount or 0) > 0
    except Exception:
        return False


def sync_permissions() -> dict[str, int]:
    """Converge system permissions, role mappings, and user-role backfills.

    Inserts missing data, updates descriptions, and applies targeted cleanup
    for system-role permissions that must remain read-only by default.
    """

    now = datetime.now(timezone.utc)
    created_roles = 0
    created_permissions = 0
    created_role_permissions = 0
    removed_role_permissions = 0
    created_user_roles = 0
    created_scope_rules = 0

    with SessionLocal() as s:
        roles_by_name: Dict[str, Role] = {
            (row.name or "").strip().lower(): row
            for row in s.query(Role).all()
            if str(getattr(row, "name", "") or "").strip()
        }
        desired_role_names = sorted({_canonical_role_name(name) for name in ALLOWED_ROLES} | set(ROLE_PERMISSION_SYNC_KEYS.keys()))
        for role_name in desired_role_names:
            role_obj = roles_by_name.get(role_name)
            desired_desc = SYSTEM_ROLE_DESCRIPTIONS.get(role_name)
            if role_obj is None:
                role_obj = Role(
                    name=role_name,
                    description=desired_desc,
                    is_system=True,
                    created_at=now,
                    updated_at=now,
                )
                s.add(role_obj)
                s.flush()
                roles_by_name[role_name] = role_obj
                created_roles += 1
                continue
            changed = False
            if desired_desc and (role_obj.description or "") != desired_desc:
                role_obj.description = desired_desc
                changed = True
            if not bool(getattr(role_obj, "is_system", False)):
                role_obj.is_system = True
                changed = True
            if changed:
                role_obj.updated_at = now
                s.add(role_obj)

        perms_by_key: Dict[str, Permission] = {
            (row.key or "").strip().lower(): row
            for row in s.query(Permission).all()
            if str(getattr(row, "key", "") or "").strip()
        }
        for key, description in DEFAULT_PERMISSION_CATALOG.items():
            perm_key = str(key).strip().lower()
            if not perm_key:
                continue
            perm_obj = perms_by_key.get(perm_key)
            if perm_obj is None:
                perm_obj = Permission(key=perm_key, description=description, created_at=now)
                s.add(perm_obj)
                s.flush()
                perms_by_key[perm_key] = perm_obj
                created_permissions += 1
                continue
            if description and (perm_obj.description or "") != description:
                perm_obj.description = description
                s.add(perm_obj)

        existing_pairs = {
            (int(row.role_id), int(row.permission_id))
            for row in s.query(RolePermission).all()
        }
        # A role whose key list is ["*"] means "every permission". The wildcard
        # itself was filtered out and nothing was put in its place, so the
        # admin role ended up with zero role_permission rows and
        # list_role_permissions("admin") returned an empty set - which reads as
        # "an admin can do nothing" to anything that inspects the tables rather
        # than going through the is-admin short circuit. Expand it instead, so
        # the stored data says what is actually true.
        _universe = {
            str(key).strip().lower()
            for keys in ROLE_PERMISSION_SYNC_KEYS.values()
            for key in keys
            if str(key).strip() and str(key).strip() != "*"
        }
        _universe |= {
            str(key).strip().lower()
            for key in DEFAULT_PERMISSION_CATALOG
            if str(key).strip() and str(key).strip() != "*"
        }

        for role_name, perm_keys in ROLE_PERMISSION_SYNC_KEYS.items():
            role_obj = roles_by_name.get(str(role_name).strip().lower())
            if role_obj is None:
                role_obj = _ensure_role_row(s, role_name)
                if role_obj is None:
                    continue
                roles_by_name[(role_obj.name or "").strip().lower()] = role_obj
            _raw = {str(key).strip().lower() for key in perm_keys if str(key).strip()}
            _effective = set(_universe) if "*" in _raw else (_raw - {"*"})
            for perm_key in sorted(_effective):
                perm_obj = perms_by_key.get(perm_key)
                if perm_obj is None:
                    perm_obj = Permission(key=perm_key, description=DEFAULT_PERMISSION_CATALOG.get(perm_key), created_at=now)
                    s.add(perm_obj)
                    s.flush()
                    perms_by_key[perm_key] = perm_obj
                    created_permissions += 1
                pair = (int(role_obj.id), int(perm_obj.id))
                if pair in existing_pairs:
                    continue
                if _sqlite_insert_or_ignore(
                    s,
                    RolePermission,
                    {"role_id": int(role_obj.id), "permission_id": int(perm_obj.id), "created_at": now},
                ):
                    existing_pairs.add(pair)
                    created_role_permissions += 1

        restricted_role_permissions = {
            "owner": {"admin.roles.manage", "admin.permissions.manage"},
            "gm": {"admin.roles.manage", "admin.permissions.manage"},
            "production": {
                "page.customers.view",
                "page.customers.drilldown.view",
                "export.customers",
                "feature.customers.dashboard.view",
            },
        }
        for role_name, restricted_keys in restricted_role_permissions.items():
            role_obj = roles_by_name.get(role_name)
            if role_obj is None or getattr(role_obj, "id", None) is None:
                continue
            permission_ids = [
                int(perm_obj.id)
                for perm_key in restricted_keys
                for perm_obj in [perms_by_key.get(perm_key)]
                if perm_obj is not None and getattr(perm_obj, "id", None) is not None
            ]
            if not permission_ids:
                continue
            removed = (
                s.query(RolePermission)
                .filter(
                    RolePermission.role_id == int(role_obj.id),
                    RolePermission.permission_id.in_(permission_ids),
                )
                .delete(synchronize_session=False)
            )
            if removed:
                removed_role_permissions += int(removed)
                existing_pairs = {
                    pair for pair in existing_pairs
                    if not (pair[0] == int(role_obj.id) and pair[1] in permission_ids)
                }

        role_ids = {
            name: int(role.id)
            for name, role in roles_by_name.items()
            if getattr(role, "id", None) is not None
        }
        existing_user_roles = {
            (int(row.user_id), int(row.role_id))
            for row in s.query(UserRole).all()
        }
        users = s.query(User).all()
        for user in users:
            canonical = _canonical_role_name(getattr(user, "role", None))
            role_id = role_ids.get(canonical)
            if role_id is None:
                continue
            pair = (int(user.id), int(role_id))
            if pair not in existing_user_roles:
                if _sqlite_insert_or_ignore(
                    s,
                    UserRole,
                    {"user_id": int(user.id), "role_id": int(role_id), "created_at": now},
                ):
                    existing_user_roles.add(pair)
                    created_user_roles += 1

        for user in users:
            canonical = _canonical_role_name(getattr(user, "role", None))
            if canonical in {"admin", "owner", "gm"}:
                continue
            rep_id = str(getattr(user, "erp_user_id", None) or getattr(user, "sales_rep_id", None) or "").strip()
            if not rep_id:
                continue
            has_rule = (
                s.query(UserScopeRule.id)
                .filter(
                    UserScopeRule.user_id == int(user.id),
                    UserScopeRule.scope_type == "rep",
                    UserScopeRule.scope_mode == "allow",
                )
                .first()
            )
            if has_rule:
                continue
            has_visibility = (
                s.query(UserVisibilitySalesRep.id)
                .filter(UserVisibilitySalesRep.app_user_id == int(user.id))
                .first()
            )
            if has_visibility:
                continue
            if _sqlite_insert_or_ignore(
                s,
                UserScopeRule,
                {
                    "user_id": int(user.id),
                    "scope_type": "rep",
                    "scope_value": rep_id,
                    "scope_mode": "allow",
                    "created_at": now,
                },
            ):
                created_scope_rules += 1

        s.commit()

    return {
        "roles_created": created_roles,
        "permissions_created": created_permissions,
        "role_permissions_created": created_role_permissions,
        "role_permissions_removed": removed_role_permissions,
        "user_roles_created": created_user_roles,
        "scope_rules_created": created_scope_rules,
    }


def list_roles() -> List[Role]:
    with SessionLocal() as s:
        return s.query(Role).order_by(Role.name.asc()).all()


def list_permissions() -> List[Permission]:
    with SessionLocal() as s:
        return s.query(Permission).order_by(Permission.key.asc()).all()


def list_role_permissions(role_name: str) -> List[str]:
    canonical = _canonical_role_name(role_name)
    with SessionLocal() as s:
        role = s.query(Role).filter(func.lower(Role.name) == canonical).first()
        if not role:
            return []
        rows = (
            s.query(Permission.key)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
                .filter(RolePermission.role_id == int(role.id))
                .all()
        )
        return sorted({canonical_permission_key(r[0]) for r in rows if r and r[0]})


def replace_role_permissions(role_name: str, permission_keys: Sequence[str]) -> List[str]:
    canonical = _canonical_role_name(role_name)
    cleaned_keys = sorted({key for key in canonicalize_permission_keys(permission_keys) if key and key != "*"})
    now = datetime.now(timezone.utc)
    with SessionLocal() as s:
        role = _ensure_role_row(s, canonical)
        if role is None:
            return []
        perms = {
            str(p.key).strip().lower(): p
            for p in s.query(Permission).filter(func.lower(Permission.key).in_(cleaned_keys)).all()
        }
        s.query(RolePermission).filter(RolePermission.role_id == int(role.id)).delete()
        for key in cleaned_keys:
            perm = perms.get(key)
            if not perm:
                perm = Permission(key=key, description=DEFAULT_PERMISSION_CATALOG.get(key), created_at=now)
                s.add(perm)
                s.flush()
                perms[key] = perm
            s.add(RolePermission(role_id=int(role.id), permission_id=int(perm.id), created_at=now))
        role.updated_at = now
        s.add(role)
        s.commit()
    return cleaned_keys


def list_user_role_names(user_id: int) -> List[str]:
    with SessionLocal() as s:
        rows = (
            s.query(Role.name)
            .join(UserRole, UserRole.role_id == Role.id)
            .filter(UserRole.user_id == int(user_id))
            .all()
        )
        return sorted({str(r[0]).strip().lower() for r in rows if r and r[0]})


def replace_user_roles(user_id: int, role_names: Sequence[str]) -> List[str]:
    cleaned = sorted({_canonical_role_name(r) for r in role_names if str(r or "").strip()})
    now = datetime.now(timezone.utc)
    with SessionLocal() as s:
        s.query(UserRole).filter(UserRole.user_id == int(user_id)).delete()
        for role_name in cleaned:
            role = _ensure_role_row(s, role_name)
            if not role:
                continue
            s.add(UserRole(user_id=int(user_id), role_id=int(role.id), created_at=now))
        user = s.get(User, int(user_id))
        if user and cleaned:
            # Preserve legacy column for backward-compatible decorators/queries.
            user.role = cleaned[0]
            user.updated_at = now
            s.add(user)
        s.commit()
    return cleaned


def list_user_permission_overrides(user_id: int) -> List[str]:
    rules = list_user_permission_rules(user_id)
    return sorted(rules.get("allow") or [])


def list_user_permission_rules(user_id: int) -> Dict[str, List[str]]:
    with SessionLocal() as s:
        rows = (
            s.query(Permission.key, UserPermission.mode)
            .join(UserPermission, UserPermission.permission_id == Permission.id)
            .filter(UserPermission.user_id == int(user_id))
            .all()
        )
        allow: set[str] = set()
        deny: set[str] = set()
        for row in rows:
            if not row or not row[0]:
                continue
            key = canonical_permission_key(row[0])
            if not key:
                continue
            mode = str(row[1] or "allow").strip().lower()
            if mode == "deny":
                deny.add(key)
            else:
                allow.add(key)
        return {"allow": sorted(allow), "deny": sorted(deny)}


def replace_user_permission_overrides(user_id: int, permission_keys: Sequence[str]) -> List[str]:
    rules = replace_user_permission_rules(user_id, allow_keys=permission_keys, deny_keys=())
    return sorted(rules.get("allow") or [])


def replace_user_permission_rules(
    user_id: int,
    *,
    allow_keys: Sequence[str] = (),
    deny_keys: Sequence[str] = (),
) -> Dict[str, List[str]]:
    allow_clean = sorted({key for key in canonicalize_permission_keys(allow_keys) if key and key != "*"})
    deny_clean = sorted({key for key in canonicalize_permission_keys(deny_keys) if key and key != "*"})
    now = datetime.now(timezone.utc)
    with SessionLocal() as s:
        s.query(UserPermission).filter(UserPermission.user_id == int(user_id)).delete()
        lookup_keys = sorted(set(allow_clean) | set(deny_clean))
        perms = {}
        if lookup_keys:
            perms = {
                canonical_permission_key(p.key): p
                for p in s.query(Permission).filter(func.lower(Permission.key).in_(lookup_keys)).all()
            }
        for key in allow_clean + deny_clean:
            perm = perms.get(key)
            if not perm:
                perm = Permission(key=key, description=DEFAULT_PERMISSION_CATALOG.get(key), created_at=now)
                s.add(perm)
                s.flush()
                perms[key] = perm
        for key in allow_clean:
            perm = perms.get(key)
            if not perm:
                continue
            s.add(
                UserPermission(
                    user_id=int(user_id),
                    permission_id=int(perm.id),
                    mode="allow",
                    created_at=now,
                )
            )
        for key in deny_clean:
            perm = perms.get(key)
            if not perm:
                continue
            s.add(
                UserPermission(
                    user_id=int(user_id),
                    permission_id=int(perm.id),
                    mode="deny",
                    created_at=now,
                )
            )
        s.commit()
    return {"allow": allow_clean, "deny": deny_clean}


def list_effective_permission_keys_for_user(user_id: int, fallback_role: Optional[str] = None) -> List[str]:
    uid = int(user_id)
    with SessionLocal() as s:
        role_rows = (
            s.query(Role.name)
            .join(UserRole, UserRole.role_id == Role.id)
            .filter(UserRole.user_id == uid)
            .all()
        )
        role_names = {_canonical_role_name(r[0]) for r in role_rows if r and r[0]}
        if not role_names and fallback_role:
            role_names.add(_canonical_role_name(fallback_role))

        perm_keys: set[str] = set()
        if role_names:
            role_perm_rows = (
                s.query(Permission.key)
                .join(RolePermission, RolePermission.permission_id == Permission.id)
                .join(Role, Role.id == RolePermission.role_id)
                .filter(func.lower(Role.name).in_(sorted(role_names)))
                .all()
            )
            perm_keys.update({canonical_permission_key(r[0]) for r in role_perm_rows if r and r[0]})
        if not perm_keys and fallback_role:
            default_keys = DEFAULT_ROLE_PERMISSION_KEYS.get(_canonical_role_name(fallback_role), set())
            perm_keys.update({canonical_permission_key(k) for k in default_keys if str(k).strip() and str(k).strip() != "*"})

        user_perm_rows = (
            s.query(Permission.key, UserPermission.mode)
            .join(UserPermission, UserPermission.permission_id == Permission.id)
            .filter(UserPermission.user_id == uid)
            .all()
        )
        allow_keys = {
            canonical_permission_key(r[0])
            for r in user_perm_rows
            if r and r[0] and str(r[1] or "allow").strip().lower() != "deny"
        }
        deny_keys = {
            canonical_permission_key(r[0])
            for r in user_perm_rows
            if r and r[0] and str(r[1] or "allow").strip().lower() == "deny"
        }
        perm_keys.update(allow_keys)
        perm_keys.difference_update(deny_keys)
        return sorted(perm_keys)


def list_user_scope_rules(user_id: int) -> Dict[str, List[str]]:
    uid = int(user_id)
    results: Dict[str, set[str]] = {k: set() for k in _SCOPE_TYPES}
    with SessionLocal() as s:
        direct_rows = (
            s.query(UserScopeRule.scope_type, UserScopeRule.scope_value)
            .filter(
                UserScopeRule.user_id == uid,
                UserScopeRule.scope_mode == "allow",
            )
            .all()
        )
        for scope_type, scope_value in direct_rows:
            canonical = _coerce_scope_type(scope_type)
            value = str(scope_value or "").strip()
            if canonical and value:
                results.setdefault(canonical, set()).add(value)

        group_rows = (
            s.query(ScopeGroup.scope_type, ScopeGroupMember.scope_value)
            .join(UserScopeGroup, UserScopeGroup.group_id == ScopeGroup.id)
            .join(ScopeGroupMember, ScopeGroupMember.group_id == ScopeGroup.id)
            .filter(UserScopeGroup.user_id == uid)
            .all()
        )
        for scope_type, scope_value in group_rows:
            canonical = _coerce_scope_type(scope_type)
            value = str(scope_value or "").strip()
            if canonical and value:
                results.setdefault(canonical, set()).add(value)

    return {k: sorted(v) for k, v in results.items()}


def replace_user_scope_rules(user_id: int, payload: Mapping[str, Any]) -> Dict[str, List[str]]:
    uid = int(user_id)
    now = datetime.now(timezone.utc)
    cleaned: Dict[str, List[str]] = {k: [] for k in _SCOPE_TYPES}
    for raw_key, raw_values in (payload or {}).items():
        canonical = _coerce_scope_type(raw_key)
        if not canonical:
            continue
        tokens = _split_scope_tokens(raw_values)
        seen: set[str] = set()
        keep: List[str] = []
        for token in tokens:
            sval = str(token).strip()
            if not sval or sval in seen:
                continue
            seen.add(sval)
            keep.append(sval)
        cleaned[canonical] = keep

    with SessionLocal() as s:
        s.query(UserScopeRule).filter(
            UserScopeRule.user_id == uid,
            UserScopeRule.scope_type.in_(list(_SCOPE_TYPES)),
        ).delete(synchronize_session=False)
        for scope_type, values in cleaned.items():
            for value in values:
                s.add(
                    UserScopeRule(
                        user_id=uid,
                        scope_type=scope_type,
                        scope_value=value,
                        scope_mode="allow",
                        created_at=now,
                    )
                )
        user = s.get(User, uid)
        if user:
            user.updated_at = now
            s.add(user)
        s.commit()
    return list_user_scope_rules(uid)


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_actor", "actor_user_id"),
        Index("ix_audit_target", "target_user_id"),
        Index("ix_audit_action", "action"),
        Index("ix_audit_created_at", "created_at"),
    )

    id = Column(Integer, primary_key=True)
    actor_user_id = Column(Integer, nullable=True)
    username = Column(String(150), nullable=True, index=True)
    action = Column(String(120), nullable=False)
    ts = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    target_user_id = Column(Integer, nullable=True)
    ip = Column(String(64), nullable=True)
    user_agent = Column(String(255), nullable=True)
    meta = Column(Text, nullable=True)
    before_json = Column(Text, nullable=True)
    after_json = Column(Text, nullable=True)


class SavedView(Base):
    __tablename__ = "saved_views"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    name = Column(String(150), nullable=False)
    filters_json = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))


class UserVisibilitySalesRep(Base):
    __tablename__ = "user_visibility_salesrep"
    __table_args__ = (
        Index("ix_visibility_user", "app_user_id"),
        Index("ix_visibility_erp_user", "visible_erp_user_id"),
        Index("ux_visibility_pair", "app_user_id", "visible_erp_user_id", unique=True),
    )

    id = Column(Integer, primary_key=True)
    app_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    visible_erp_user_id = Column(String(50), nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
