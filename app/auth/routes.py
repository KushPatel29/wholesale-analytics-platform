from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, session
from flask_login import login_user, logout_user, current_user, login_required
from datetime import datetime, timezone
import pyotp

from .forms import LoginForm, TwoFAForm, PasswordResetForm
from .models import get_user_by_id, get_user_by_username, record_login_attempt, is_account_locked, SessionLocal
from .password_tokens import (
    consume_password_token,
    get_valid_password_token,
    issue_password_token,
    request_user_agent,
)
from ..limiter import limiter
from ..core.audit import log_audit

bp = Blueprint("auth", __name__, url_prefix="/auth")


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))
    form = LoginForm()
    if form.validate_on_submit():
        username = form.username.data.strip()
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()

        # Check lockout window before verifying credentials
        if is_account_locked(username):
            flash("Invalid username or password", "danger")
            return render_template("auth/login.html", form=form), 200

        user = get_user_by_username(username)
        if user and user.check_password(form.password.data):
            if getattr(user, "must_reset_password", False):
                try:
                    with SessionLocal() as s:
                        token = issue_password_token(
                            s,
                            user_id=int(user.id),
                            purpose="reset",
                            created_by_user_id=int(user.id),
                            request_ip_addr=ip,
                            request_user_agent_value=request_user_agent(),
                            invalidate_existing=True,
                        )
                        s.commit()
                except Exception:
                    current_app.logger.exception("auth.login_issue_reset_token_failed", extra={"user_id": int(user.id)})
                    flash("Unable to start password reset. Please contact an administrator.", "danger")
                    return render_template("auth/login.html", form=form), 200
                flash("Password reset required. Please reset to continue.", "warning")
                return redirect(url_for("auth.set_password", token=token))
            if not getattr(user, "is_approved", False):
                record_login_attempt(username, ip, False)
                flash("Account pending approval. Please contact an admin.", "warning")
                return render_template("auth/login.html", form=form), 200
            if not getattr(user, "is_active", False):
                record_login_attempt(username, ip, False)
                flash("Account disabled. Please contact an admin.", "danger")
                return render_template("auth/login.html", form=form), 200
            # If 2FA is enabled and configured, require TOTP
            if current_app.config.get("APP_FEATURE_FLAGS", {}).get("enable_2fa", False) and getattr(user, "totp_confirmed", False) and getattr(user, "totp_secret", None):
                code = (form.totp_code.data or "").strip()
                if not code:
                    flash("Enter your authenticator code.", "warning")
                    return render_template("auth/login.html", form=form), 200
                totp = pyotp.TOTP(user.totp_secret)
                if not totp.verify(code, valid_window=1):
                    record_login_attempt(username, ip, False)
                    flash("Invalid username or password", "danger")
                    return render_template("auth/login.html", form=form), 200
            # Success (either 2FA not required or code verified)
            record_login_attempt(username, ip, True)
            try:
                with SessionLocal() as s:
                    u = s.get(type(user), int(user.id))
                    if u:
                        u.last_login_at = datetime.now(timezone.utc)
                        u.updated_at = datetime.now(timezone.utc)
                        s.add(u)
                        s.commit()
            except Exception:
                current_app.logger.debug("login.last_login_at_update_failed", exc_info=True)
            login_user(user, remember=bool(form.remember_me.data))
            try:
                log_audit(user, "login")
            except Exception:
                pass
            next_url = request.args.get("next") or url_for("dashboard.index")
            return redirect(next_url)
        else:
            record_login_attempt(username, ip, False)
            flash("Invalid username or password", "danger")
            return render_template("auth/login.html", form=form), 200
    return render_template("auth/login.html", form=form)


@bp.route("/demo", methods=["GET", "POST"])
@limiter.limit("30 per minute")
def demo_login():
    """
    One click into the read-only demo account.

    A portfolio link that opens on a username box loses its reader in the first
    ten seconds, and that reader is the entire audience for this deployment. So
    the login page carries a button that lands here, and this signs the visitor
    straight in as `demo_viewer`.

    GET is allowed on purpose: the point is that a URL pasted into a recruiter's
    address bar - or reached from a bookmark - is enough. That is only safe
    because the account is read-only by role (see `install_read_only_guard`),
    so there is no state for a cross-site GET to change.

    Only reachable when DEMO_MODE is on, so a real deployment of this codebase
    has no anonymous door.
    """
    from ..core.demo_accounts import DEMO_VIEWER_USERNAME, demo_logins_enabled

    if not demo_logins_enabled():
        return redirect(url_for("auth.login"))
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    user = get_user_by_username(DEMO_VIEWER_USERNAME)
    if user is None or not getattr(user, "is_active", False):
        current_app.logger.warning("auth.demo_login_unavailable", extra={"username": DEMO_VIEWER_USERNAME})
        flash(
            "The demo account is not available on this deployment. "
            "Run `python manage.py seed-demo-users` to create it.",
            "warning",
        )
        return redirect(url_for("auth.login"))

    login_user(user, remember=False)
    try:
        log_audit(user, "demo_login")
    except Exception:
        pass
    session["is_demo_session"] = True
    next_url = request.args.get("next") or url_for("dashboard.index")
    # Never bounce off-site on the strength of a query parameter.
    if not str(next_url).startswith("/"):
        next_url = url_for("dashboard.index")
    return redirect(next_url)


@bp.route("/logout")
def logout():
    if current_user.is_authenticated:
        try:
            log_audit(current_user, "logout")
        except Exception:
            pass
    try:
        logout_user()
    except Exception:
        pass
    try:
        session.clear()
    except Exception:
        pass

    resp = redirect(url_for("login_alias"))
    session_cookie = current_app.config.get("SESSION_COOKIE_NAME", "session")
    remember_cookie = current_app.config.get("REMEMBER_COOKIE_NAME", "remember_token")
    resp.set_cookie(session_cookie, "", expires=0)
    resp.set_cookie(remember_cookie, "", expires=0)
    resp.delete_cookie(session_cookie)
    resp.delete_cookie(remember_cookie)
    return resp


@bp.route("/setup-2fa", methods=["GET"])
@login_required
def setup_2fa():
    # Feature gate
    if not current_app.config.get("APP_FEATURE_FLAGS", {}).get("enable_2fa", False):
        flash("Two-factor authentication is disabled.", "warning")
        return redirect(url_for("dashboard.index"))

    if getattr(current_user, "totp_confirmed", False) and getattr(current_user, "totp_secret", None):
        return render_template("auth/setup_2fa.html", enabled=True)

    # Generate a pending secret in session if not already
    pending = session.get("pending_totp_secret")
    if not pending:
        pending = pyotp.random_base32()
        session["pending_totp_secret"] = pending

    issuer = current_app.config.get("TOTP_ISSUER", "Northgate Retail Analytics")
    uri = pyotp.totp.TOTP(pending).provisioning_uri(name=current_user.username, issuer_name=issuer)

    # Optional ASCII QR using qrcode if available
    ascii_qr = None
    try:
        import qrcode  # type: ignore
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(uri)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        lines = []
        # pad borders for better scanning
        border = "\u2588" * (len(matrix[0]) * 2 + 4)
        lines.append(border)
        for row in matrix:
            line = "\u2588\u2588"  # left border
            for cell in row:
                line += ("\u2588\u2588" if cell else "  ")
            line += "\u2588\u2588"  # right border
            lines.append(line)
        lines.append(border)
        ascii_qr = "\n".join(lines)
    except Exception:
        ascii_qr = None

    return render_template("auth/setup_2fa.html", enabled=False, secret=pending, otpauth_uri=uri, ascii_qr=ascii_qr, form=TwoFAForm())


@bp.route("/confirm-2fa", methods=["POST"])
@login_required
def confirm_2fa():
    if not current_app.config.get("APP_FEATURE_FLAGS", {}).get("enable_2fa", False):
        flash("Two-factor authentication is disabled.", "warning")
        return redirect(url_for("dashboard.index"))

    form = TwoFAForm()
    if not form.validate_on_submit():
        return redirect(url_for("auth.setup_2fa"))

    pending = session.get("pending_totp_secret")
    if not pending:
        flash("No 2FA setup in progress.", "warning")
        return redirect(url_for("auth.setup_2fa"))

    totp = pyotp.TOTP(pending)
    if not totp.verify((form.totp_code.data or "").strip(), valid_window=1):
        flash("Invalid authenticator code.", "danger")
        return redirect(url_for("auth.setup_2fa"))

    # Persist to user
    from .models import SessionLocal, User
    with SessionLocal() as s:
        u = s.get(User, int(current_user.get_id()))
        if not u:
            flash("User not found.", "danger")
            return redirect(url_for("auth.setup_2fa"))
        u.totp_secret = pending
        u.totp_confirmed = True
        s.add(u)
        s.commit()

    session.pop("pending_totp_secret", None)
    flash("Two-factor authentication enabled.", "success")
    return redirect(url_for("dashboard.index"))


@bp.route("/set-password/<token>", methods=["GET", "POST"])
@bp.route("/reset-password/<token>", endpoint="reset_password", methods=["GET", "POST"])
@limiter.limit("20 per hour")
def set_password(token: str):
    form = PasswordResetForm()
    with SessionLocal() as s:
        token_row = get_valid_password_token(s, token)
    if token_row is None:
        flash("Invalid or expired reset link.", "danger")
        return redirect(url_for("auth.login"))

    if form.validate_on_submit():
        token_purpose = None
        user_id = None
        can_auto_login = False
        with SessionLocal() as s:
            user, consumed_token = consume_password_token(
                s,
                token,
                new_password=form.password.data,
            )
            if user is None or consumed_token is None:
                s.rollback()
                flash("Invalid or expired reset link.", "danger")
                return redirect(url_for("auth.login"))
            token_purpose = consumed_token.purpose
            user_id = int(user.id)
            can_auto_login = bool(getattr(user, "is_active", False)) and bool(getattr(user, "is_approved", False))
            s.commit()

        login_candidate = get_user_by_id(user_id) if user_id is not None else None
        try:
            log_audit(
                login_candidate or str(user_id or ""),
                "password_set_via_token",
                target_user_id=user_id,
                meta={"purpose": token_purpose},
            )
        except Exception:
            pass

        if can_auto_login and login_candidate is not None:
            try:
                if login_user(login_candidate, remember=False):
                    flash("Password set successfully.", "success")
                    return redirect(url_for("dashboard.index"))
            except Exception:
                current_app.logger.debug("auth.set_password_auto_login_failed", exc_info=True)
        flash("Password set successfully. Please sign in.", "success")
        return redirect(url_for("auth.login"))
    return render_template("auth/set_password.html", form=form)
