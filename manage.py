#!/usr/bin/env python3
from __future__ import annotations

import base64
import os
import secrets
from urllib.parse import quote
from pathlib import Path

import click
from sqlalchemy import func

from app.auth.models import init_auth_db, get_session, User
from app.core.audit import log_audit


@click.group()
def cli():
    """Management CLI for auth DB users and utilities."""


@cli.command("init-auth-db")
def init_auth_db_cmd():
    """Create SQLite auth.db and required tables."""
    init_auth_db()
    log_audit("cli", "init_auth_db")
    click.secho("Initialized auth.db and tables.", fg="green")


def _ensure_user(username: str) -> User | None:
    username = (username or "").strip()
    if not username:
        return None
    with get_session() as s:
        u = s.query(User).filter(func.lower(User.username) == username.lower()).first()
        return u


@cli.command("create-admin")
@click.option("--username", required=True, help="Username to create/update")
@click.option("--password", required=False, help="Password for the user")
@click.option("--role", default="admin", show_default=True, help="Role for the user")
@click.option("--email", required=False, help="Optional email address")
def create_admin_cmd(username: str, password: str | None, role: str, email: str | None):
    """Create or update an admin (or specified role) user."""
    if not password:
        password = click.prompt("Password", hide_input=True, confirmation_prompt=True)
    role = (role or "admin").strip().lower()
    email = (email or "").strip().lower() or None

    username = (username or "").strip()
    if not username:
        raise click.ClickException("username is required")

    with get_session() as s:
        u = s.query(User).filter(func.lower(User.username) == username.lower()).first()
        if u is None:
            u = User(
                username=username,
                email=email,
                role=role,
                is_active=True,
                is_approved=True,
                must_reset_password=False,
            )
            u.set_password(password)
            s.add(u)
            s.commit()
            click.secho(f"Created user '{username}' with role '{role}' (active+approved).", fg="green")
            log_audit("cli", "create_user", {"username": username, "role": role, "active": True, "approved": True})
        else:
            u.role = role
            if email:
                u.email = email
            u.is_active = True
            u.is_approved = True
            u.must_reset_password = False
            if password:
                u.set_password(password)
            s.add(u)
            s.commit()
            click.secho(f"Updated user '{username}' with role '{role}' (active+approved).", fg="yellow")
            log_audit("cli", "update_user", {"username": username, "role": role, "active": True, "approved": True})


@cli.command("list-users")
def list_users_cmd():
    """List users in the auth DB."""
    with get_session() as s:
        rows = s.query(User).order_by(User.id.asc()).all()
        if not rows:
            click.echo("No users found.")
            return
        for u in rows:
            click.echo(f"{u.id}\t{u.username}\trole={u.role}\t2FA={'on' if u.totp_confirmed else 'off'}")


@cli.command("reset-password")
@click.option("--username", required=True, help="Username to reset")
@click.option("--password", required=False, help="New password (prompt if omitted)")
def reset_password_cmd(username: str, password: str | None):
    """Reset a user's password."""
    if not password:
        password = click.prompt("New password", hide_input=True, confirmation_prompt=True)
    with get_session() as s:
        u = s.query(User).filter(User.username == username).first()
        if not u:
            raise click.ClickException(f"User '{username}' not found.")
        u.set_password(password)  # type: ignore[arg-type]
        s.add(u)
        s.commit()
        click.secho(f"Password reset for '{username}'.", fg="green")
        log_audit("cli", "reset_password", {"username": username})


@cli.command("check-config")
def check_config_cmd():
    """Validate environment variables and directory structure."""
    import os

    click.secho("🔍 Checking Wholesale Analytics configuration...", fg="cyan")
    
    required_dirs = ["data", "logs", "cache", "instance"]
    for d in required_dirs:
        if os.path.isdir(d):
            click.echo(f"  ✅ Directory '{d}' exists.")
        else:
            click.echo(f"  ⚠️ Directory '{d}' missing (will be auto-created if app starts).")

    # Check for .env file
    if os.path.exists(".env"):
        click.echo("  ✅ .env file found.")
    else:
        click.echo("  ⚠️ .env file missing (using system env or defaults).")

    # Check core analytic dependencies
    parquet_path = os.getenv("PARQUET_PATH", "data/sales_fact.parquet")
    if os.path.exists(parquet_path):
        click.echo(f"  ✅ Parquet dataset found at '{parquet_path}'.")
    else:
        click.echo(f"  ❌ Parquet dataset MISSING at '{parquet_path}'. Run ETL first.")

    click.secho("🌟 Configuration check complete.", fg="green")


@cli.command("enable-2fa")
@click.option("--username", required=True, help="Username to enable 2FA for")
@click.option("--issuer", required=False, default="Wholesale Analytics", show_default=True)
def enable_2fa_cmd(username: str, issuer: str):
    """Enable 2FA for a user and print the TOTP secret + otpauth URL."""
    # 20 random bytes base32 encoded (omit padding)
    secret = base64.b32encode(secrets.token_bytes(20)).decode("ascii").replace("=", "")
    with get_session() as s:
        u = s.query(User).filter(User.username == username).first()
        if not u:
            raise click.ClickException(f"User '{username}' not found.")
        u.totp_secret = secret
        u.totp_confirmed = False
        s.add(u)
        s.commit()
    label = f"{issuer}:{username}"
    otpauth = (
        f"otpauth://totp/{quote(label)}?secret={secret}&issuer={quote(issuer)}&digits=6&period=30"
    )
    click.secho(f"2FA secret for {username}: {secret}", fg="yellow")
    click.echo(f"otpauth URL: {otpauth}")
    log_audit("cli", "enable_2fa", {"username": username})


DEMO_USERS = {
    # Full access, including the admin portal. The `admin` role short-circuits
    # the scope check entirely, so it needs no rules.
    "admin": dict(role="admin", scope={}, note="everything, plus the admin portal"),
    # Sees the whole company but cannot administer it. "*" is the wildcard the
    # access policy recognises as unrestricted.
    "gm": dict(role="gm", scope={"rep": ("*",)}, note="everything, no admin portal"),
    # Scoped to two regions: every page silently narrows to that territory.
    "manager.coast": dict(
        role="sales_manager",
        scope={"region": ("RG01", "RG02")},
        note="Lower Mainland + Vancouver Island only",
    ),
    # Scoped to a single rep's own book.
    "rep.dana": dict(role="sales", scope={"rep": ("R01",)}, note="Dana Whitfield's accounts only"),
    "rep.tomasz": dict(role="sales", scope={"rep": ("R04",)}, note="Tomasz Bielski's accounts only"),
    # Sees every row but has no view_costs permission, so cost, margin and
    # profit come back masked rather than merely hidden in the template.
    "viewer.nocost": dict(
        role="warehouse",
        scope={"rep": ("*",)},
        note="all rows, but cost/margin columns masked",
    ),
}

DEMO_PASSWORD = "demo-password-1234"


@cli.command("seed-demo-users")
@click.option("--password", default=DEMO_PASSWORD, show_default=True, help="Password for every demo login")
def seed_demo_users_cmd(password: str) -> None:
    """
    Create the demo logins used by the synthetic dataset.

    Each one exercises a different slice of the access model, so the RBAC
    scoping and the cost-masking rules can be seen by logging in rather than
    taken on trust.
    """
    from app.auth.models import UserScopeRule, sync_permissions

    # Role and permission rows are what AUTHZ_DB_PERMISSIONS reads. Without
    # this, permission-gated pages (returns, notifications) 404 for everyone,
    # including admin.
    synced = sync_permissions()
    click.echo(f"Synced roles/permissions: {synced}")

    with get_session() as s:
        for username, spec in DEMO_USERS.items():
            u = s.query(User).filter(func.lower(User.username) == username).first()
            if u is None:
                u = User(username=username, role=spec["role"], is_active=True, is_approved=True)
                s.add(u)
            u.role = spec["role"]
            u.is_active = True
            u.is_approved = True
            u.must_reset_password = False
            u.email = f"{username}@wholesaleprovisions.example.com"
            reps = spec["scope"].get("rep", ())
            regions = spec["scope"].get("region", ())
            u.sales_rep_id = reps[0] if reps and reps[0] != "*" else None
            u.region_id = regions[0] if regions else None
            u.set_password(password)
            s.flush()

            # Rewrite this user's scope rules from scratch so re-running the
            # command is idempotent rather than additive.
            s.query(UserScopeRule).filter(UserScopeRule.user_id == u.id).delete()
            for scope_type, values in spec["scope"].items():
                for value in values:
                    s.add(
                        UserScopeRule(
                            user_id=u.id,
                            scope_type=scope_type,
                            scope_value=value,
                            scope_mode="allow",
                        )
                    )
        s.commit()

    click.secho(f"Seeded {len(DEMO_USERS)} demo users (password: {password})", fg="green")
    for username, spec in DEMO_USERS.items():
        click.echo(f"  {username:16} role={spec['role']:14} {spec['note']}")
    log_audit("cli", "seed_demo_users", {"count": len(DEMO_USERS)})


@cli.command("build-products-parquet")
@click.option(
    "--output",
    required=False,
    help="Override the output path (defaults to PRODUCTS_PARQUET_PATH/DATA_DIR).",
)
@click.option(
    "--source",
    type=click.Choice(["snapshot", "live"]),
    default="snapshot",
    show_default=True,
    help="Use existing cached snapshot or live SQL via data_loader.",
)
def build_products_parquet_cmd(output: str | None, source: str) -> None:
    """Generate the products parquet with the required schema."""
    from app.blueprints import products as products_bp

    target = products_bp.resolve_products_parquet_path(output)
    schema_version = os.getenv("PRODUCTS_PARQUET_SCHEMA_VERSION", "1")
    click.echo(f"Target products parquet: {target}")

    try:
        import data_loader

        df_source = data_loader.get_dataframe() if source == "live" else data_loader.load_snapshot()
    except Exception as exc:
        click.echo(
            "Unable to load source data. Run your ETL to build the base snapshot "
            "or set PARQUET_PATH to an existing parquet before retrying.",
            err=True,
        )
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc

    if df_source is None or df_source.empty:
        click.echo(
            "Source dataset is empty. Run `python run.py --force-refresh` or your ETL job, "
            "then rerun build-products-parquet.",
            err=True,
        )
        raise SystemExit(1)

    df = products_bp._standardize_sales_df(df_source)
    try:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(target, index=False)
        products_bp._write_schema_meta(Path(target), schema_version)
    except Exception as exc:  # pragma: no cover - depends on local engines
        click.echo(f"Failed to write products parquet to {target}: {exc}", err=True)
        raise SystemExit(1) from exc

    click.echo(f"Wrote products parquet to {target} with {len(df)} rows (schema v{schema_version}).")


if __name__ == "__main__":
    cli()
