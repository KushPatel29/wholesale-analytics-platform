"""ML helpers for simple forecasting using Prophet with fallback."""

from __future__ import annotations

import warnings
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import numpy as np
from datetime import datetime

# In-memory cache for churn model
_CHURN_MODEL_BUNDLE: Optional[Dict[str, Any]] = None
_MODEL_PATH = Path("cache/churn_model.pkl")


def model_status() -> str:
    return "ml ready"


def forecast_series(df: pd.DataFrame, date_col: str = "Date", value_col: str = "Revenue", periods: int = 6) -> pd.DataFrame:
    """Forecast `periods` future periods using Prophet if available, else moving average.

    Expects a dataframe with at least `date_col` and `value_col`.
    Returns a dataframe with columns: ds (date), yhat, yhat_lower, yhat_upper.
    """

    data = df[[date_col, value_col]].dropna().copy()
    data[date_col] = pd.to_datetime(data[date_col], errors="coerce")
    data = data.dropna(subset=[date_col])
    if data.empty:
        return pd.DataFrame(columns=["ds", "yhat", "yhat_lower", "yhat_upper"])  # nothing to do

    # Aggregate by month
    monthly = data.groupby(pd.to_datetime(data[date_col]).dt.to_period("M").dt.to_timestamp())[value_col].sum()
    df_prophet = monthly.reset_index().rename(columns={"index": "ds", value_col: "y"})
    df_prophet.columns = ["ds", "y"]

    # If insufficient history for a stable model, prefer fallback
    use_prophet = len(df_prophet) >= 12
    if use_prophet:
        try:
            from prophet import Prophet  # type: ignore

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                m = Prophet(interval_width=0.8)
                m.fit(df_prophet)
                future = m.make_future_dataframe(periods=periods, freq="MS")
                fc = m.predict(future)
                out = fc[["ds", "yhat", "yhat_lower", "yhat_upper"]]
                return out
        except Exception:
            # fall back below
            pass

    # Fallback: centered moving average with naive CI bands
    y = df_prophet.set_index("ds")["y"].asfreq("MS").fillna(0)
    window = min(6, max(3, int(len(y) / 4) or 3))
    ma = y.rolling(window=window, min_periods=1, center=True).mean()
    last = ma.dropna().iloc[-1] if not ma.dropna().empty else (y.iloc[-1] if not y.empty else 0.0)
    # naive future = last moving average
    idx_future = pd.date_range(y.index.max() + pd.offsets.MonthBegin(1), periods=periods, freq="MS")
    yhat = pd.Series(last, index=idx_future)
    # simple CI: +/- 1 std of recent residuals
    residuals = (y - ma).dropna()
    std = residuals.tail(12).std() if not residuals.empty else 0.0
    if not np.isfinite(std):
        std = 0.0
    yhat_lower = yhat - 1.0 * std
    yhat_upper = yhat + 1.0 * std
    out = pd.DataFrame({
        "ds": idx_future,
        "yhat": yhat.values,
        "yhat_lower": yhat_lower.values,
        "yhat_upper": yhat_upper.values,
    })
    return out


def _detect_cols(df: pd.DataFrame) -> Tuple[str, str, Optional[str]]:
    rev_col = next((c for c in ["Revenue", "revenue_packs_only", "revenue_shipped", "revenue_ordered"] if c in df.columns), None)
    date_col = "Date" if "Date" in df.columns else next((c for c in df.columns if c.lower().startswith("date")), None)
    region_col = next((c for c in df.columns if c.lower() in {"region_name", "region", "regionname"}), None)
    return date_col or "Date", rev_col or "revenue_ordered", region_col


def build_churn_training_df(fact_df: pd.DataFrame) -> pd.DataFrame:
    """Build customer-level features and 90d churn label from fact dataframe.

    Features:
      - Recency (days since last order)
      - Frequency (unique orders)
      - Monetary (sum revenue)
      - MonthsActive (span from first to last order in months)
      - Last3M_Orders (unique orders in last 3 months)
      - Last6M_Revenue (sum revenue in last 6 months)
      - Region one-hot (if available)
    Label:
      - churned_90d: 1 if Recency > 90 else 0 (snapshot at dataset max date)
    """

    if fact_df is None or fact_df.empty:
        return pd.DataFrame()

    date_col, rev_col, region_col = _detect_cols(fact_df)
    df = fact_df.copy()
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    df = df.dropna(subset=[date_col])
    if df.empty:
        return pd.DataFrame()

    ref = df[date_col].max()
    # Core aggregates per customer
    freq = df.groupby("CustomerId")["OrderId"].nunique().rename("Frequency")
    mon = pd.to_numeric(df[rev_col], errors="coerce").fillna(0).groupby(df["CustomerId"]).sum().rename("Monetary")
    first = df.groupby("CustomerId")[date_col].min().rename("FirstOrder")
    last = df.groupby("CustomerId")[date_col].max().rename("LastOrder")
    recency = (ref - last).dt.days.rename("Recency")
    months_active = ((last.dt.year - first.dt.year) * 12 + (last.dt.month - first.dt.month) + 1).rename("MonthsActive")

    # Windows
    last3 = ref - pd.DateOffset(months=3)
    last6 = ref - pd.DateOffset(months=6)
    last3_orders = df[df[date_col] >= last3].groupby("CustomerId")["OrderId"].nunique().rename("Last3M_Orders")
    last6_rev = (
        pd.to_numeric(df.loc[df[date_col] >= last6, rev_col], errors="coerce")
        .fillna(0)
        .groupby(df.loc[df[date_col] >= last6, "CustomerId"]).sum().rename("Last6M_Revenue")
    )

    feat = pd.concat([freq, mon, first, last, recency, months_active, last3_orders, last6_rev], axis=1).fillna(0)
    feat["CustomerId"] = feat.index
    # Add names if present
    name_col = next((c for c in df.columns if c.lower() in {"customername", "customer_name", "name", "customer"}), None)
    if name_col:
        names = df.dropna(subset=["CustomerId", name_col]).drop_duplicates("CustomerId").set_index("CustomerId")[[name_col]]
        names = names.rename(columns={name_col: "CustomerName"})
        feat = feat.join(names, how="left")
    else:
        feat["CustomerName"] = feat["CustomerId"].astype(str)

    # Region one-hot
    if region_col and region_col in df.columns:
        region_mode = (
            df[["CustomerId", region_col]]
            .dropna()
            .groupby("CustomerId")[region_col]
            .agg(lambda s: s.mode().iloc[0] if not s.mode().empty else s.iloc[0])
        )
        feat = feat.join(region_mode.rename("Region"), on="CustomerId")
        feat = pd.get_dummies(feat, columns=["Region"], prefix="Region", dummy_na=False)

    # Label
    feat["churned_90d"] = (feat["Recency"] > 90).astype(int)
    return feat.reset_index(drop=True)


def train_churn_model(df: pd.DataFrame) -> Tuple[Dict[str, Any], List[str], Any]:
    if df is None or df.empty or "churned_90d" not in df.columns:
        raise ValueError("Training dataframe is empty or missing label")

    label = df["churned_90d"].astype(int).values
    # Feature columns: numeric and one-hot, exclude ids/names/dates/label
    drop_cols = {"CustomerId", "CustomerName", "FirstOrder", "LastOrder", "churned_90d"}
    feature_cols = [c for c in df.columns if c not in drop_cols]
    X = df[feature_cols].fillna(0).astype(float).values

    # Lazy import for split
    from sklearn.model_selection import train_test_split  # type: ignore
    X_train, X_test, y_train, y_test = train_test_split(
        X,
        label,
        test_size=0.2,
        random_state=42,
        stratify=label if label.sum() and (len(label) - label.sum()) else None,
    )

    # Lazy import to avoid heavy sklearn import during app/test startup
    from sklearn.preprocessing import StandardScaler  # type: ignore
    scaler = StandardScaler()
    X_train_s = scaler.fit_transform(X_train)
    X_test_s = scaler.transform(X_test)

    # Binary classifier with liblinear solver
    # Lazy imports for model and metrics
    from sklearn.linear_model import LogisticRegression  # type: ignore
    from sklearn.metrics import roc_auc_score, accuracy_score  # type: ignore
    model = LogisticRegression(max_iter=1000, solver="liblinear")
    model.fit(X_train_s, y_train)
    # Metrics
    proba = model.predict_proba(X_test_s)[:, 1]
    try:
        auc = roc_auc_score(y_test, proba)
    except Exception:
        auc = float('nan')
    acc = accuracy_score(y_test, (proba >= 0.5).astype(int))

    bundle = {
        "model": model,
        "feature_cols": feature_cols,
        "scaler": scaler,
        "metrics": {"auc": float(auc), "accuracy": float(acc)},
        "trained_at": datetime.now(timezone.utc).isoformat(),
    }
    return bundle, feature_cols, scaler


def save_churn_model(bundle: Dict[str, Any]) -> str:
    _MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_MODEL_PATH, "wb") as f:
        pickle.dump(bundle, f)
    global _CHURN_MODEL_BUNDLE
    _CHURN_MODEL_BUNDLE = bundle
    return _MODEL_PATH.as_posix()


def load_churn_model() -> Optional[Dict[str, Any]]:
    global _CHURN_MODEL_BUNDLE
    if _CHURN_MODEL_BUNDLE is not None:
        return _CHURN_MODEL_BUNDLE
    if _MODEL_PATH.exists():
        try:
            with open(_MODEL_PATH, "rb") as f:
                _CHURN_MODEL_BUNDLE = pickle.load(f)
                return _CHURN_MODEL_BUNDLE
        except Exception:
            return None
    return None


def score_churn(fact_df: pd.DataFrame, model_bundle: Dict[str, Any]) -> pd.DataFrame:
    if fact_df is None or fact_df.empty or not model_bundle:
        return pd.DataFrame(columns=["CustomerId", "CustomerName", "churn_prob"]) 
    feat_df = build_churn_training_df(fact_df)
    if feat_df.empty:
        return pd.DataFrame(columns=["CustomerId", "CustomerName", "churn_prob"]) 
    feature_cols = model_bundle.get("feature_cols", [])
    scaler = model_bundle.get("scaler")
    model = model_bundle.get("model")
    X = feat_df[feature_cols].reindex(columns=feature_cols, fill_value=0).astype(float).values
    Xs = scaler.transform(X)
    proba = model.predict_proba(Xs)[:, 1]
    out = feat_df[["CustomerId", "CustomerName"]].copy()
    out["churn_prob"] = proba
    return out.sort_values("churn_prob", ascending=False).reset_index(drop=True)


def get_cached_churn_model() -> Optional[Dict[str, Any]]:
    return load_churn_model()


def _detect_price_col(df: pd.DataFrame) -> Optional[str]:
    for c in ["Price", "UnitPrice", "Unit_Price", "UnitCost", "unit_price"]:
        if c in df.columns:
            return c
    return None


def _detect_qty_col(df: pd.DataFrame) -> Optional[str]:
    for c in ["QuantityShipped", "QuantityOrdered", "pack_item_count_sum", "pack_weight_lb_sum"]:
        if c in df.columns:
            return c
    return None


def suggest_price_for_customer_product(df: pd.DataFrame, customer_id: str | int, product_id: str | int) -> Dict[str, Any]:
    """Suggest a price for a given customer and product using simple elasticity estimation.

    - Build a panel from past orders: price vs quantity (or weight)
    - If >= 10 points with price variance, fit log(Q) ~ log(P) to estimate elasticity e
    - Target margin ~30%; suggest price improving margin while keeping predicted Q >= 80% of current
    - Clamp suggested price within +/-15% of current price
    - Fallbacks: cost+markup (30%) if CostPrice available; else region median price for the product

    Returns dict with: current_price, suggested_price, rationale, elasticity (optional), predicted_qty_ratio (optional)
    """
    out: Dict[str, Any] = {}
    if df is None or df.empty:
        out.update(current_price=None, suggested_price=None, rationale="No data available")
        return out

    price_col = _detect_price_col(df)
    qty_col = _detect_qty_col(df)
    cost_col = "CostPrice" if "CostPrice" in df.columns else None
    region_col = next((c for c in df.columns if c.lower() in {"region_name", "region", "regionname"}), None)

    if price_col is None or qty_col is None:
        out.update(current_price=None, suggested_price=None, rationale="Missing price or quantity fields")
        return out

    dff = df.copy()
    dff[price_col] = pd.to_numeric(dff[price_col], errors="coerce")
    dff[qty_col] = pd.to_numeric(dff[qty_col], errors="coerce")
    if cost_col:
        dff[cost_col] = pd.to_numeric(dff[cost_col], errors="coerce")

    prod_df = dff[dff.get("ProductId").astype(str) == str(product_id)].copy()
    if prod_df.empty:
        out.update(current_price=None, suggested_price=None, rationale="No product history")
        return out

    # Customer-specific recent price as current
    cust_rows = prod_df[prod_df.get("CustomerId").astype(str) == str(customer_id)]
    current_price = float(cust_rows[price_col].dropna().iloc[-1]) if not cust_rows.dropna(subset=[price_col]).empty else float(prod_df[price_col].median())
    out["current_price"] = current_price

    # Filter to same region as customer if possible
    if region_col and not cust_rows.empty and not cust_rows[region_col].dropna().empty:
        cust_region = str(cust_rows[region_col].dropna().mode().iloc[0])
        hist_df = prod_df[prod_df[region_col].astype(str) == cust_region].copy()
    else:
        hist_df = prod_df

    panel = hist_df[[price_col, qty_col]].dropna()
    panel = panel[(panel[price_col] > 0) & (panel[qty_col] > 0)]
    n_pts = int(len(panel))

    # Fallback generators
    def fallback_suggestion(reason: str) -> Dict[str, Any]:
        # Prefer cost+markup
        markup = 0.30
        if cost_col and not cust_rows.dropna(subset=[cost_col]).empty:
            cost_val = float(cust_rows[cost_col].dropna().median())
        elif cost_col and not hist_df.dropna(subset=[cost_col]).empty:
            cost_val = float(hist_df[cost_col].dropna().median())
        else:
            cost_val = None
        if cost_val is not None and cost_val > 0:
            target = cost_val * (1.0 + markup)
        else:
            # Region/product median price
            target = float(hist_df[price_col].median())
        # Clamp within +/-15% of current when current available
        if np.isfinite(current_price) and current_price > 0:
            lo = current_price * 0.85
            hi = current_price * 1.15
            target = float(np.clip(target, lo, hi))
        return {
            "current_price": current_price,
            "suggested_price": target,
            "rationale": reason,
        }

    if n_pts < 10 or panel[price_col].nunique() < 3:
        return fallback_suggestion("Insufficient history; using cost+markup or median price")

    # Elasticity fit: log(Q) = a0 + e*log(P)
    x = np.log(panel[price_col].values)
    y = np.log(panel[qty_col].values)
    try:
        e, a0 = np.polyfit(x, y, 1)  # slope=e, intercept=a0
    except Exception:
        return fallback_suggestion("Elasticity fit failed; using fallback")

    out["elasticity"] = float(e)

    # Baseline quantity at current price
    a = np.exp(a0)
    q_pred_current = float(a * (current_price ** e)) if current_price > 0 else float(np.exp(y).median())
    q_floor = 0.8 * q_pred_current

    # Target margin
    target_margin = 0.30
    cost_val = None
    if cost_col and not cust_rows.dropna(subset=[cost_col]).empty:
        cost_val = float(cust_rows[cost_col].dropna().median())
    elif cost_col and not hist_df.dropna(subset=[cost_col]).empty:
        cost_val = float(hist_df[cost_col].dropna().median())

    # Candidate price for target margin
    if cost_val and cost_val > 0:
        p_margin = cost_val / (1.0 - target_margin)
    else:
        p_margin = float(hist_df[price_col].median())

    # Clamp within +/-15% of current
    lo = current_price * 0.85
    hi = current_price * 1.15
    p_suggest = float(np.clip(p_margin, lo, hi))

    # Ensure demand constraint q(P) >= 0.8 * q_current; adjust downwards if needed
    def q_of(p: float) -> float:
        return float(a * (p ** e))

    q_at_suggest = q_of(p_suggest)
    if q_at_suggest < q_floor:
        # Reduce price until demand constraint met or lower bound
        # Solve a * p^e = q_floor -> p = (q_floor / a)^(1/e)
        try:
            p_req = float((q_floor / a) ** (1.0 / e))
            p_suggest = max(p_req, lo)
        except Exception:
            # Fallback to lower bound
            p_suggest = lo
        q_at_suggest = q_of(p_suggest)

    out.update({
        "current_price": current_price,
        "suggested_price": p_suggest,
        "predicted_qty_ratio": float(q_at_suggest / q_pred_current) if q_pred_current > 0 else None,
        "rationale": f"Elasticity e={e:.2f}; target margin {int(target_margin*100)}%; demand ratio {((q_at_suggest/q_pred_current)*100):.0f}%",
    })
    return out
