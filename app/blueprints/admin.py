from __future__ import annotations

import io
import os
import re
import secrets
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, send_file, jsonify
from flask_login import current_user, login_required

from ..core.rbac import (
    ALLOWED_ROLES,
    current_roles as rbac_current_roles,
    effective_permissions,
    has_permission,
    permission_required,
)
from ..core.access_policy import require_admin
from ..auth.models import (
    SessionLocal,
    User,
    AuditLog,
    list_visibility_for_user,
    list_permissions,
)
from ..auth.password_tokens import (
    build_set_password_link,
    issue_password_token,
    request_ip as token_request_ip,
    request_user_agent as token_request_user_agent,
)
from ..core.audit import log_audit
from ..core.features import get_flags, save_flags
from ..core.branding import get_branding, save_branding
from ..services.user_invites import send_password_email
from werkzeug.utils import secure_filename
from wtforms import Form, PasswordField
from wtforms.validators import DataRequired

from app.services.data_access import get_fact_context
from app.services import analytics_utils as au
from app.services import fact_store

LIVE_SQL_ALLOWED = (
    str(os.getenv("ALLOW_LIVE_SQL", "")).strip().lower() in {"1", "true", "yes", "on"}
    and (os.getenv("APP_MODE", "web").strip().lower() in {"etl", "job"})
)

bp = Blueprint("admin", __name__, url_prefix="/admin")

_ADMIN_USER_STATUSES = {"active", "disabled", "pending", "all"}


@bp.after_request
def _disable_admin_caching(response):
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    vary = [part.strip() for part in str(response.headers.get("Vary") or "").split(",") if part.strip()]
    if "Cookie" not in vary:
        vary.append("Cookie")
    response.headers["Vary"] = ", ".join(vary)
    return response

# Default CSV path (overridden by form "path" or USERS_CSV_PATH)
_fallback_csv = (Path(__file__).resolve().parent.parent / "core" / "userid.csv").resolve()
DEFAULT_CSV_PATH = Path(os.getenv("USERS_CSV_PATH", str(_fallback_csv))).expanduser().resolve()

# Ensure parent dir exists at import time (safe, idempotent)
try:
    DEFAULT_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
except Exception:
    # Don't hard-fail on import; we'll handle at runtime with a flash
    pass

def _users_csv_root() -> Path:
    base = os.getenv("USERS_CSV_BASE")
    root = Path(base).expanduser() if base else DEFAULT_CSV_PATH.parent
    resolved = root.resolve()
    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return resolved

def _is_within_users_root(path: Path) -> bool:
    try:
        path.resolve().relative_to(_users_csv_root())
        return True
    except ValueError:
        return False


def _admin_user_filters_from_request() -> dict[str, str]:
    status = str(request.args.get("status") or "active").strip().lower()
    if status not in _ADMIN_USER_STATUSES:
        status = "active"
    return {
        "query": str(request.args.get("query") or "").strip(),
        "role": str(request.args.get("role") or "").strip().lower(),
        "status": status,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
SUPER_USERS = {
    "kush patel": "admin",
    "jason pleym": "owner",
    "kyle mclaw": "gm",
}

SALES_VISIBILITY_CHOICES = [
    ("self", "Own sales only"),
    ("org", "All sales reps"),
]

def _slugify_username(first: str, last: str) -> str:
    base = re.sub(r"[^a-z0-9]+", ".", f"{first}.{last}".lower()).strip(".")
    base = re.sub(r"\.+", ".", base)
    return base or "user"


def _assign_role_for_name(first: str, last: str, default_role: str = "sales") -> str:
    key = f"{first} {last}".strip().lower()
    return SUPER_USERS.get(key, default_role)


def _ensure_unique_username(s, username: str) -> str:
    """Ensure username uniqueness by appending a counter if needed."""
    candidate = username
    i = 2
    while s.query(User).filter(User.username == candidate).first() is not None:
        candidate = f"{username}{i}"
        i += 1
    return candidate


def _strong_default_password() -> str:
    # Meat-themed 10-character password: easy to communicate yet unique
    prefixes = ("steak", "bacon", "beefy", "porks", "hammy")
    prefix = secrets.choice(prefixes)
    digits_needed = max(1, 10 - len(prefix))
    suffix = "".join(secrets.choice(string.digits) for _ in range(digits_needed))
    candidate = (prefix + suffix)[:10]
    if len(candidate) < 10:
        candidate += "".join(secrets.choice(string.digits) for _ in range(10 - len(candidate)))
    return candidate


# ─────────────────────────────────────────────────────────────────────────────
# Index & Users
# ─────────────────────────────────────────────────────────────────────────────
@bp.route("/")
@login_required
@require_admin
@permission_required("admin.users.manage")
def index():
    return redirect(url_for("admin.users"))


@bp.route("/users")
@login_required
@require_admin
@permission_required("admin.users.manage")
def users():
    roles = sorted(ALLOWED_ROLES)
    return render_template(
        "admin/users.html",
        roles=roles,
        admin_user_select_enabled=bool(current_app.config.get("ADMIN_USER_SELECT", True)),
        admin_user_filters=_admin_user_filters_from_request(),
    )


@bp.route("/roles")
@login_required
@require_admin
def roles():
    return render_template(
        "admin/roles.html",
        permissions=list_permissions(),
        can_manage_roles=has_permission("admin.roles.manage"),
    )


@bp.route("/users/add", methods=["POST"])
@login_required
@require_admin
@permission_required("admin.users.manage")
def users_add():
    form = request.form
    username = (form.get("username") or "").strip()
    password = (form.get("password") or "").strip()
    role = (form.get("role") or "sales").strip().lower()
    sales_rep_id = (form.get("sales_rep_id") or "").strip() or None
    region_id = (form.get("region_id") or "").strip() or None
    sales_visibility = (form.get("sales_visibility") or "self").strip().lower()
    allowed_visibility = {choice[0] for choice in SALES_VISIBILITY_CHOICES}
    if sales_visibility not in allowed_visibility:
        sales_visibility = "self"

    if not username or not password:
        flash("Username and password are required.", "warning")
        return redirect(url_for("admin.users"))

    # Enforce password policy
    from ..auth.forms import password_policy_validators
    class _P(Form):
        pw = PasswordField(validators=[DataRequired()] + password_policy_validators)
    f = _P(data={"pw": password})
    if not f.validate():
        msg = "; ".join(err for errs in f.errors.values() for err in errs)
        flash(f"Password policy: {msg}", "danger")
        return redirect(url_for("admin.users"))

    if role not in ALLOWED_ROLES:
        flash("Invalid role.", "danger")
        return redirect(url_for("admin.users"))

    with SessionLocal() as s:
        if s.query(User).filter(User.username == username).first():
            flash("Username already exists.", "warning")
            return redirect(url_for("admin.users"))
        u = User(
            username=username,
            role=role,
            sales_rep_id=sales_rep_id,
            erp_user_id=sales_rep_id,
            region_id=region_id,
            sales_visibility=sales_visibility,
        )
        u.set_password(password)
        u.updated_at = datetime.now(timezone.utc)
        s.add(u)
        s.commit()
    try:
        log_audit(current_user, "admin_create_user", {"username": username, "role": role})
    except Exception:
        pass

    flash("User added.", "success")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:user_id>/update", methods=["POST"])
@login_required
@require_admin
@permission_required("admin.users.manage")
def users_update(user_id: int):
    form = request.form
    role = (form.get("role") or "sales").strip().lower()
    sales_rep_id = (form.get("sales_rep_id") or "").strip() or None
    region_id = (form.get("region_id") or "").strip() or None
    password = (form.get("password") or "").strip()
    sales_visibility = (form.get("sales_visibility") or "self").strip().lower()
    allowed_visibility = {choice[0] for choice in SALES_VISIBILITY_CHOICES}
    if sales_visibility not in allowed_visibility:
        sales_visibility = "self"

    if role not in ALLOWED_ROLES:
        flash("Invalid role.", "danger")
        return redirect(url_for("admin.users"))

    from ..auth.forms import password_policy_validators
    with SessionLocal() as s:
        u = s.get(User, user_id)
        if not u:
            flash("User not found.", "danger")
            return redirect(url_for("admin.users"))
        u.role = role
        u.sales_rep_id = sales_rep_id
        u.erp_user_id = sales_rep_id or u.erp_user_id
        u.region_id = region_id
        u.sales_visibility = sales_visibility
        if password:
            class _P(Form):
                pw = PasswordField(validators=[DataRequired()] + password_policy_validators)
            f = _P(data={"pw": password})
            if not f.validate():
                msg = "; ".join(err for errs in f.errors.values() for err in errs)
                flash(f"Password policy: {msg}", "danger")
                return redirect(url_for("admin.users"))
            u.set_password(password)
        # 2FA toggle
        twofa = (form.get("twofa_enabled") == "on")
        if not twofa:
            u.totp_confirmed = False
            u.totp_secret = None
        u.updated_at = datetime.now(timezone.utc)
        s.add(u)
        s.commit()
    try:
        log_audit(current_user, "admin_update_user", {"user_id": user_id})
    except Exception:
        pass
    flash("User updated.", "success")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:user_id>/delete", methods=["POST"])
@login_required
@require_admin
@permission_required("admin.users.manage")
def users_delete(user_id: int):
    with SessionLocal() as s:
        u = s.get(User, user_id)
        if not u:
            flash("User not found.", "danger")
            return redirect(url_for("admin.users"))
        s.delete(u)
        s.commit()
    flash("User deleted.", "success")
    return redirect(url_for("admin.users"))


@bp.route("/users/reset-password/<int:user_id>", methods=["POST"])
@login_required
@require_admin
@permission_required("admin.users.manage")
def users_reset_password(user_id: int):
    if not current_app.config.get("INVITES_ENABLED", True):
        flash("Password reset email is disabled by INVITES_ENABLED=0.", "warning")
        return redirect(url_for("admin.users"))

    link = None
    to_email = None
    full_name = None
    with SessionLocal() as s:
        u = s.get(User, user_id)
        if not u:
            flash("User not found.", "danger")
            return redirect(url_for("admin.users"))
        to_email = (u.email or "").strip().lower() or None
        full_name = u.full_name
        if not to_email:
            flash("User has no email address.", "warning")
            return redirect(url_for("admin.users"))
        try:
            token = issue_password_token(
                s,
                user_id=int(u.id),
                purpose="reset",
                created_by_user_id=int(getattr(current_user, "id", 0) or 0) or None,
                request_ip_addr=token_request_ip(),
                request_user_agent_value=token_request_user_agent(),
                invalidate_existing=True,
            )
            link = build_set_password_link(token)
        except Exception:
            s.rollback()
            current_app.logger.exception("admin.users_reset_password_token_failed", extra={"user_id": int(user_id)})
            flash("Failed to create reset token.", "danger")
            return redirect(url_for("admin.users"))
        u.must_reset_password = True
        u.updated_at = datetime.now(timezone.utc)
        s.add(u)
        s.commit()

    sent = send_password_email(
        to_email=to_email,
        recipient_name=full_name,
        set_password_link=link or "",
        purpose="reset",
    )
    if sent:
        flash("Password reset email sent.", "success")
    else:
        flash("Reset token created but email delivery failed.", "warning")
    try:
        log_audit(current_user, "admin_reset_password", {"user_id": user_id, "reset_link": "[hidden]", "email_sent": bool(sent)})
    except Exception:
        pass
    return redirect(url_for("admin.users"))


@bp.route("/users/prepare-csv-dir", methods=["POST"])
@login_required
@require_admin
@permission_required("admin.users.manage")
def users_prepare_csv_dir():
    """
    Create (or re-create) the parent folder for the users CSV and drop a placeholder file.
    Default path: app/core/userid.csv (override via USERS_CSV_PATH).
    """
    csv_path_str = (request.form.get("path") or os.getenv("USERS_CSV_PATH") or str(DEFAULT_CSV_PATH)).strip()
    path = Path(csv_path_str).expanduser().resolve()
    root = _users_csv_root()
    if not _is_within_users_root(path):
        flash(f"CSV path must stay within {root}", "danger")
        return redirect(url_for("admin.users"))

    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        # Write a placeholder so the dir shows up in Docker bind mounts / backups
        with open(parent / ".placeholder", "w", encoding="utf-8") as f:
            f.write("Place userid.csv here. Expected columns: UserId,FirstName,LastName\n")
        flash(f"CSV folder ready: {parent}", "success")
    except Exception as e:
        flash(f"Failed to prepare CSV folder at {parent}: {e}", "danger")
        return redirect(url_for("admin.users"))

    try:
        log_audit(current_user, "admin_prepare_csv_dir", {"dir": str(parent)})
    except Exception:
        pass
    return redirect(url_for("admin.users"))


@bp.route("/users/upload-csv", methods=["POST"])
@login_required
@require_admin
@permission_required("admin.users.manage")
def users_upload_csv():
    """
    Upload a new userid CSV and store it at the configured default path.
    Validates the CSV before replacing the on-disk file.
    """
    uploaded = request.files.get("csv_file")
    csv_path_str = (request.form.get("path") or os.getenv("USERS_CSV_PATH") or str(DEFAULT_CSV_PATH)).strip()
    path = Path(csv_path_str).expanduser().resolve()

    if not uploaded or not uploaded.filename:
        flash("Select a CSV file to upload.", "warning")
        return redirect(url_for("admin.users"))

    filename = secure_filename(uploaded.filename)
    if not filename.lower().endswith(".csv"):
        flash("Uploads must be CSV files.", "danger")
        return redirect(url_for("admin.users"))

    try:
        payload = uploaded.read()
    except Exception as e:
        flash(f"Failed to read uploaded file: {e}", "danger")
        return redirect(url_for("admin.users"))

    if not payload:
        flash("Uploaded file is empty.", "danger")
        return redirect(url_for("admin.users"))

    try:
        df = pd.read_csv(io.BytesIO(payload))
    except Exception as e:
        flash(f"Failed to parse CSV: {e}", "danger")
        return redirect(url_for("admin.users"))

    required_cols = {"UserId", "FirstName", "LastName"}
    if not required_cols.issubset(set(df.columns)):
        flash(f"CSV must include columns: {sorted(required_cols)}", "danger")
        return redirect(url_for("admin.users"))

    root = _users_csv_root()
    if not _is_within_users_root(path):
        flash(f"CSV path must stay within {root}", "danger")
        return redirect(url_for("admin.users"))

    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        flash(f"Could not ensure CSV folder {parent}: {e}", "danger")
        return redirect(url_for("admin.users"))

    try:
        with open(path, "wb") as f:
            f.write(payload)
    except Exception as e:
        flash(f"Failed to write CSV to {path}: {e}", "danger")
        return redirect(url_for("admin.users"))

    try:
        log_audit(current_user, "admin_upload_users_csv", {"path": str(path), "rows": int(len(df))})
    except Exception:
        pass

    flash(f"Uploaded CSV saved to {path} ({len(df)} rows). Run sync to apply changes.", "success")
    return redirect(url_for("admin.users"))


@bp.route("/users/download-csv", methods=["GET"])
@login_required
@require_admin
@permission_required("admin.users.manage")
def users_download_csv():
    csv_path_str = (request.args.get("path") or os.getenv("USERS_CSV_PATH") or str(DEFAULT_CSV_PATH)).strip()
    path = Path(csv_path_str).expanduser().resolve()
    root = _users_csv_root()
    if not _is_within_users_root(path):
        flash(f"CSV path must stay within {root}", "danger")
        return redirect(url_for("admin.users"))
    if not path.exists():
        flash(f"CSV file not found at {path}. Upload a new file first.", "warning")
        return redirect(url_for("admin.users"))
    try:
        return send_file(path, mimetype="text/csv", as_attachment=True, download_name=path.name)
    except Exception as e:
        flash(f"Failed to send CSV file: {e}", "danger")
        return redirect(url_for("admin.users"))


# ─────────────────────────────────────────────────────────────────────────────
# CSV Sync: seed/update users from userid.csv (UserId, FirstName, LastName)
# ─────────────────────────────────────────────────────────────────────────────
@bp.route("/users/sync-from-csv", methods=["POST"])
@login_required
@require_admin
@permission_required("admin.users.manage")
def users_sync_from_csv():
    """
    Ingests a CSV with columns: UserId, FirstName, LastName
    - Creates users that don't exist (username: first.last)
    - Sets sales_rep_id = UserId (GUID) for scoping
    - Default role 'sales' except:
        Kush Patel -> admin
        Jason Pleym -> owner
        Kyle McLaw  -> gm
    """
    csv_path = (request.form.get("path") or os.getenv("USERS_CSV_PATH") or str(DEFAULT_CSV_PATH)).strip()
    path = Path(csv_path).expanduser().resolve()
    root = _users_csv_root()
    if not _is_within_users_root(path):
        flash(f"CSV path must stay within {root}", "danger")
        return redirect(url_for("admin.users"))

    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        flash(f"Could not ensure CSV folder {parent}: {e}", "danger")
        return redirect(url_for("admin.users"))
    if not path.exists():
        flash(f"CSV not found: {path}", "danger")
        return redirect(url_for("admin.users"))

    try:
        df = pd.read_csv(path)
    except Exception as e:
        flash(f"Failed to read CSV: {e}", "danger")
        return redirect(url_for("admin.users"))

    required_cols = {"UserId", "FirstName", "LastName"}
    if not required_cols.issubset(set(df.columns)):
        flash(f"CSV must include columns: {sorted(required_cols)}", "danger")
        return redirect(url_for("admin.users"))

    created, updated = 0, 0
    with SessionLocal() as s:
        for _, row in df.iterrows():
            guid = str(row["UserId"]).strip()
            first = str(row["FirstName"]).strip()
            last = str(row["LastName"]).strip()
            if not guid or not first or not last:
                continue

            # Prefer match by GUID (erp_user_id / sales_rep_id)
            u: Optional[User] = s.query(User).filter((User.erp_user_id == guid) | (User.sales_rep_id == guid)).first()
            if not u:
                # Or by username slug
                base_username = _slugify_username(first, last)
                u = s.query(User).filter(User.username == base_username).first()

            role = _assign_role_for_name(first, last, default_role="sales")

            if not u:
                username = _ensure_unique_username(s, _slugify_username(first, last))
                u = User(
                    username=username,
                    role=role,
                    sales_rep_id=guid,  # legacy
                    erp_user_id=guid,   # primary ERP mapping
                    region_id=None,
                )
                u.set_password(_strong_default_password())
                s.add(u)
                created += 1
            else:
                # Update GUID & elevate role if they are in the SUPER_USERS list
                u.sales_rep_id = guid or u.sales_rep_id
                u.erp_user_id = guid or u.erp_user_id
                # Only bump role upward for the three named users; don't downgrade existing admins
                if role in {"admin", "owner", "gm"} and u.role != role:
                    u.role = role
                updated += 1

        s.commit()

    try:
        log_audit(current_user, "admin_sync_users_from_csv", {"path": str(path), "created": created, "updated": updated})
    except Exception:
        pass

    flash(f"CSV sync complete: {created} created, {updated} updated.", "success")
    return redirect(url_for("admin.users"))


# ─────────────────────────────────────────────────────────────────────────────
# Features / Branding / System (unchanged except minor tidy-ups)
# ─────────────────────────────────────────────────────────────────────────────
@bp.route("/audit")
@login_required
@require_admin
@permission_required("admin.audit.view")
def audit():
    with SessionLocal() as s:
        entries = s.query(AuditLog).order_by(AuditLog.ts.desc()).limit(200).all()
    return render_template("admin/audit.html", entries=entries)


@bp.route("/users/<int:user_id>/preview", methods=["GET"])
@login_required
@require_admin
@permission_required("admin.users.manage")
def user_preview(user_id: int):
    """Return scope summary + sample counts for a user (no impersonation)."""
    with SessionLocal() as s:
        u = s.get(User, user_id)
        if not u:
            return jsonify({"error": "User not found"}), 404
    visibility = list_visibility_for_user(user_id)
    summary = {
        "id": u.id,
        "username": u.username,
        "email": u.email,
        "full_name": u.full_name,
        "role": u.role,
        "erp_user_id": u.erp_user_id,
        "visibility": visibility,
    }
    preview = {"rows": 0, "regions": [], "sales_rep_ids": []}
    try:
        scope = {"scope_mode": "list" if visibility else "none", "allowed_erp_user_ids": visibility}
        df = fact_store.query_fact(
            filters=None,
            scope=scope,
            columns=["RegionName", "SalesRepId", "SalesRepName", "PrimarySalesRepName"],
            limit=500,
        )
        if isinstance(df, pd.DataFrame) and not df.empty:
            preview["rows"] = int(len(df))
            if "RegionName" in df.columns:
                preview["regions"] = sorted({str(v) for v in df["RegionName"].dropna().unique()})[:10]
            rep_cols = [c for c in df.columns if c.lower() in {"salesrepid", "sales_rep_id", "primarysalesrepid", "primary_sales_rep_id", "salesrepname", "primarysalesrepname"}]
            reps = set()
            for col in rep_cols:
                reps.update({str(v) for v in df[col].dropna().unique()})
            preview["sales_rep_ids"] = sorted(reps)[:10]
    except Exception:
        current_app.logger.debug("admin.user_preview_failed", exc_info=True)
    return jsonify({"user": summary, "preview": preview})


@bp.route("/features", methods=["GET", "POST"])
@login_required
@require_admin
@permission_required("manage_features")
def features():
    if request.method == "POST":
        flags = {
            "enable_churn": (request.form.get("enable_churn") == "on"),
            "enable_prophet": (request.form.get("enable_prophet") == "on"),
            "enable_2fa": (request.form.get("enable_2fa") == "on"),
        }
        save_flags(flags)
        try:
            log_audit(current_user, "toggle_features", flags)
        except Exception:
            pass
        flash("Feature flags updated.", "success")
        return redirect(url_for("admin.features"))
    flags = get_flags()
    return render_template("admin/features.html", flags=flags)


@bp.route("/branding", methods=["GET", "POST"])
@login_required
@require_admin
@permission_required("manage_branding")
def branding():
    if request.method == "POST":
        brand_name = (request.form.get("brand_name") or "Wholesale Analytics").strip()
        primary_color = (request.form.get("primary_color") or "#0d6efd").strip()
        if not primary_color.startswith("#") and len(primary_color) in (6, 7):
            primary_color = ("#" + primary_color.replace("#", "")).strip()
        # Handle logo upload
        logo_file = request.files.get("logo")
        logo_filename = None
        if logo_file and logo_file.filename:
            fn = secure_filename(logo_file.filename)
            ext = Path(fn).suffix.lower()
            if ext in {".png", ".jpg", ".jpeg", ".svg"}:
                out_dir = Path(__file__).resolve().parent.parent / "static" / "branding"
                out_dir.mkdir(parents=True, exist_ok=True)
                out_path = out_dir / ("logo_" + fn)
                logo_file.save(out_path.as_posix())
                logo_filename = out_path.name
        data = get_branding()
        if logo_filename:
            data["logo_filename"] = logo_filename
        data["brand_name"] = brand_name
        data["primary_color"] = primary_color
        save_branding(data)
        try:
            log_audit(current_user, "update_branding", {"brand_name": brand_name})
        except Exception:
            pass
        flash("Branding updated.", "success")
        return redirect(url_for("admin.branding"))
    branding = get_branding()
    return render_template("admin/branding.html", branding=branding)


def _human_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    s = float(n)
    for u in units:
        if s < 1024.0:
            return f"{s:.1f} {u}"
        s /= 1024.0
    return f"{s:.1f} PB"


@bp.route("/system")
@login_required
@require_admin
@permission_required("admin.audit.view")
def system():
    checks: list[Dict[str, Any]] = []
    cfg = None
    cfg_error = None

    direct_sql_mode = False
    driver_info: Dict[str, Any] = {"status": "skip", "detail": "Live SQL disabled in web workers", "drivers": []}
    checks.append(
        {
            "name": "Live SQL",
            "status": "warn",
            "detail": "Blocked in web mode (APP_MODE=web). Use ETL job for refresh.",
        }
    )

    # Writable cache and free disk
    parquet_path = getattr(fact_store, "FACT_PATH", None) or os.getenv("PARQUET_PATH", "cache/fact_dataset")
    cache_path = Path(parquet_path or "cache/fact_dataset").resolve()
    cache_dir = cache_path.parent
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        test_file = cache_dir / ".write_test"
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("ok")
        test_file.unlink(missing_ok=True)
        checks.append({"name": "Cache directory writable", "status": "ok", "detail": str(cache_dir)})
    except Exception as e:
        checks.append({"name": "Cache directory writable", "status": "fail", "detail": f"{cache_dir}: {e}"})

    try:
        import shutil
        du = shutil.disk_usage(cache_dir)
        free = du.free
        lvl = "ok" if free >= 1 * 1024**3 else ("warn" if free >= 200 * 1024**2 else "fail")
        checks.append({"name": "Free disk", "status": lvl, "detail": _human_bytes(free)})
    except Exception as e:
        checks.append({"name": "Free disk", "status": "warn", "detail": str(e)})

    # Versions
    versions: Dict[str, str] = {}
    try:
        import platform, flask, pandas, numpy, sqlalchemy, pyodbc  # type: ignore
        versions["python"] = platform.python_version()
        versions["flask"] = flask.__version__
        versions["pandas"] = pandas.__version__
        versions["numpy"] = numpy.__version__
        versions["sqlalchemy"] = sqlalchemy.__version__
        versions["pyodbc"] = getattr(pyodbc, "version", "installed")
        if driver_info.get("drivers"):
            versions["odbc_drivers"] = " | ".join(driver_info["drivers"])
    except Exception:
        pass

    # Parquet stats
    cache_rows = 0
    parquet_mtime = None
    try:
        if cache_path.exists():
            from datetime import datetime, timezone
            parquet_mtime = datetime.fromtimestamp(cache_path.stat().st_mtime, tz=timezone.utc)
            if cache_path.is_dir():
                meta = fact_store.get_meta() if hasattr(fact_store, "get_meta") else {}
                cache_rows = int(meta.get("row_count") or 0) if isinstance(meta, dict) else 0
            else:
                df = pd.read_parquet(cache_path.as_posix())
                cache_rows = int(len(df))
    except Exception:
        pass

    rbac_roles = sorted(rbac_current_roles())
    rbac_perms = sorted(effective_permissions())

    return render_template(
        "admin/system.html",
        checks=checks,
        versions=versions,
        cache_rows=cache_rows,
        parquet_mtime=parquet_mtime,
        cache_dir=str(cache_dir),
        loader_cfg=cfg,
        loader_cfg_error=cfg_error,
        driver_info=driver_info,
        direct_sql_mode=direct_sql_mode,
        rbac_roles=rbac_roles,
        rbac_permissions=rbac_perms,
    )


@bp.get("/api/fact_status")
@login_required
@require_admin
@permission_required("admin.audit.view")
def fact_status():
    """Admin-only fact dataset status (refresh loop + manifest)."""
    try:
        from app.services import inprocess_refresh
        status = inprocess_refresh.get_status()
    except Exception:
        status = {}
    try:
        from app.services import watermark_store
        manifest = watermark_store.read_manifest()
    except Exception:
        manifest = {}

    def _pick(*vals):
        for v in vals:
            if v:
                return v
        return None

    payload = {
        "dataset_version": _pick(status.get("dataset_version"), manifest.get("dataset_version")),
        "watermark": _pick(status.get("watermark"), manifest.get("watermark"), manifest.get("last_sql_watermark")),
        "min_date": _pick(status.get("min_date"), manifest.get("min_date"), manifest.get("min_dateexpected")),
        "max_date": _pick(status.get("max_date"), manifest.get("max_date"), manifest.get("max_dateexpected")),
        "last_refresh_at": _pick(status.get("last_refresh_at"), manifest.get("last_refresh_utc"), manifest.get("built_at_utc")),
        "last_error": status.get("last_error"),
        "last_status": status.get("last_status"),
        "running": status.get("running", False),
    }
    return jsonify(payload)


@bp.get("/data-health")
@login_required
@require_admin
@permission_required("admin.audit.view")
def data_health():
    """Admin diagnostic for revenue/pack coverage."""
    try:
        from data import store as duck_store  # type: ignore

        conn = duck_store.get_conn()
        duck_store.init_views(conn)
        stats = conn.execute(
            """
            SELECT
              COUNT(*) AS rows,
              COUNT(DISTINCT OrderLineId) AS orderlines,
              COUNT(DISTINCT OrderId) AS orders,
              COUNT(DISTINCT ProductId) AS products,
              SUM(CASE WHEN Cost IS NULL THEN 1 ELSE 0 END) AS cost_missing,
              SUM(CASE WHEN pack_weight_lb_sum IS NULL AND pack_item_count_sum IS NULL THEN 1 ELSE 0 END) AS pack_missing
            FROM fact
            """
        ).fetchone()
        rows = stats[0] or 0
        payload = {
            "source": "parquet",
            "rows": int(rows),
            "orderlines": int(stats[1] or 0),
            "orders": int(stats[2] or 0),
            "products": int(stats[3] or 0),
            "cost_missing_rows": int(stats[4] or 0),
            "cost_missing_pct": round(((stats[4] or 0) / rows * 100), 2) if rows else None,
            "pack_missing_pct": round(((stats[5] or 0) / rows * 100), 2) if rows else None,
        }
    except Exception as exc:
        current_app.logger.exception("data_health.load_failed", extra={"error": str(exc)})
        return jsonify({"error": "unable to load fact data"}), 500
    return jsonify(payload)


@bp.get("/data-control-totals")
@login_required
@require_admin
@permission_required("admin.audit.view")
def data_control_totals():
    """Admin diagnostic endpoint comparing per-tab totals to the base fact."""
    ctx = get_fact_context(current_user, filters=request.args or {})
    df = ctx.df
    rev_col = au.revenue_column(df) or "Revenue"
    revenue = au.to_numeric_safe(df.get(rev_col, pd.Series(dtype=float)))
    base_total = float(round(revenue.sum(), 2))

    def _group_total(col: str) -> Optional[float]:
        if col not in df.columns:
            return None
        grouped = revenue.groupby(df[col], dropna=False).sum()
        return float(round(grouped.sum(), 2))

    totals = {
        "overview": base_total,
        "products": _group_total("ProductId") or base_total,
        "customers": _group_total("CustomerId") or base_total,
        "suppliers": _group_total("SupplierId") or base_total,
        "regions": _group_total("RegionName") or base_total,
        "velocity": base_total,
    }

    mismatches = {
        key: round(val - base_total, 2)
        for key, val in totals.items()
        if val is not None and abs(val - base_total) > max(1.0, base_total * 0.01)
    }

    payload = {
        "base": ctx.meta,
        "totals": totals,
        "mismatches": mismatches,
        "filters": {"start": ctx.meta.get("start"), "end": ctx.meta.get("end"), "statuses": ctx.meta.get("statuses")},
    }
    return jsonify(payload)
