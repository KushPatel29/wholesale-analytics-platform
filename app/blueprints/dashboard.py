from __future__ import annotations

from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from flask_login import current_user, login_required
import pandas as pd

from ..core.data_service import get_fact_df, apply_global_filters
from ..core.filters import build_global_filter_form
from ..core.access_policy import require_admin
from ..core import ml as churn_ml
from ..core.audit import log_audit
from ..services import analytics_utils as au


bp = Blueprint("dashboard", __name__)  # mounted at '/'


def _select_revenue_column(df: pd.DataFrame) -> str:
    return au.revenue_column(df) or "Revenue"


@bp.route("/", methods=["GET", "POST"])
@login_required
def index():
    """Redirect to modern overview page (single source of truth)."""
    return redirect(url_for("pages.home"), code=301)


@bp.route("/admin/churn-train")
@login_required
@require_admin
def churn_train():
    # Use full dataset for training
    df = get_fact_df()
    train_df = churn_ml.build_churn_training_df(df)
    if train_df.empty:
        flash("Not enough data to train churn model.", "warning")
        return redirect(url_for("customers.kpis"))
    bundle, *_ = churn_ml.train_churn_model(train_df)
    churn_ml.save_churn_model(bundle)
    metrics = bundle.get("metrics", {})
    try:
        log_audit(current_user, "model_train", {"auc": metrics.get("auc"), "accuracy": metrics.get("accuracy")})
    except Exception:
        pass
    flash(f"Churn model trained. AUC={metrics.get('auc'):.3f}, ACC={metrics.get('accuracy'):.3f}", "success")
    return redirect(url_for("customers.kpis"))
