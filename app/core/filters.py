"""Template filters and global filter forms â€” production ready."""

from __future__ import annotations

from typing import Iterable, Tuple, Mapping, Any, Sequence
from datetime import datetime, date

from flask_wtf import FlaskForm
from wtforms import DateField, SelectMultipleField, SubmitField

from app.services.filters import shipping_name_series  # canonical names for methods
import pandas as pd


# -------------------- Jinja filters --------------------
def currency(value):
    try:
        v = float(value)
        return f"${v:,.2f}"
    except Exception:
        return "" if value in (None, "") else str(value)


def percent(value, decimals: int = 1):
    try:
        v = float(value)
        fmt = f"{{:.{decimals}f}}%"
        return fmt.format(v)
    except Exception:
        return "" if value in (None, "") else str(value)


def intcomma(value):
    try:
        v = int(float(value))
        return f"{v:,}"
    except Exception:
        return "" if value in (None, "") else str(value)


# -------------------- Form model --------------------
class GlobalFilterForm(FlaskForm):
    start_date = DateField("Start", format="%Y-%m-%d", default=None)
    end_date = DateField("End", format="%Y-%m-%d", default=None)
    statuses = SelectMultipleField("Statuses", choices=[], default=["All"], coerce=str)
    regions = SelectMultipleField("Regions", choices=[], default=["All"], coerce=str)
    shipping_methods = SelectMultipleField("Shipping Methods", choices=[], default=["All"], coerce=str)
    customers = SelectMultipleField("Customers", choices=[], default=["All"], coerce=str)
    suppliers = SelectMultipleField("Suppliers", choices=[], default=["All"], coerce=str)
    products = SelectMultipleField("Products", choices=[], default=["All"], coerce=str)
    sales_reps = SelectMultipleField("Sales Reps", choices=[], default=["All"], coerce=str)
    submit = SubmitField("Apply")


# -------------------- Helpers --------------------
_SENTINELS_ALL = {"all", "*", "__all__", "All"}

def _choices_from_series(values: Iterable[Any]) -> list[Tuple[str, str]]:
    uniq = sorted({str(v).strip() for v in values if v is not None and str(v).strip()})
    return [("All", "All")] + [(v, v) for v in uniq]

def _as_list(x: Any) -> Sequence[Any]:
    if x is None:
        return ()
    if isinstance(x, (list, tuple, set)):
        return list(x)
    return [x]

def _split_csv_mixed(values: Sequence[Any]) -> list[str]:
    out: list[str] = []
    for v in values:
        if v in (None, ""):
            continue
        s = str(v)
        # split CSV tokens but keep simple values
        parts = [p.strip() for p in s.split(",")]
        out.extend([p for p in parts if p])
    return out

def _extract_multi(args: Mapping[str, Any] | Any, *keys: str) -> list[str]:
    """
    Extract list values from Flask's request.args (MultiDict) or a plain dict.
    Supports repeated params, []-suffixed params, and CSV in a single token.
    """
    # MultiDict-like
    if hasattr(args, "getlist"):
        vals: list[str] = []
        for k in keys:
            vals.extend(args.getlist(k) or [])
            vals.extend(args.getlist(f"{k}[]") or [])
        return _split_csv_mixed(vals)

    # Plain Mapping
    vals: list[str] = []
    for k in keys:
        if k in args:
            vals.extend(_as_list(args.get(k)))
        kb = f"{k}[]"
        if kb in args:
            vals.extend(_as_list(args.get(kb)))
    return _split_csv_mixed(vals)

def _coerce_date(x: Any) -> date | None:
    if not x:
        return None
    if isinstance(x, date) and not isinstance(x, datetime):
        return x
    # parse permissively
    if isinstance(x, datetime):
        return x.date()
    s = str(x)
    try:
        return datetime.fromisoformat(s).date()
    except Exception:
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except Exception:
            return None

def _normalize_all_exclusive(values: list[str]) -> list[str]:
    """
    If any specific values (not 'All') are present, drop 'All'.
    If empty -> return ['All'] (for a consistent default).
    """
    if not values:
        return ["All"]
    has_specific = any(v not in _SENTINELS_ALL for v in values)
    if has_specific:
        return [v for v in values if v not in _SENTINELS_ALL]
    # only sentinel(s)
    return ["All"]

def _union_choices_with_selected(choices: list[Tuple[str, str]], selected: list[str]) -> list[Tuple[str, str]]:
    """
    Ensure that all user-selected values exist in choices so WTForms
    doesn't drop them as invalid. Keep a stable, sorted order after 'All'.
    """
    labels = {c[0] for c in choices}
    missing = [v for v in selected if v not in labels and v not in _SENTINELS_ALL]
    if not missing:
        return choices
    # place 'All' first, then union of existing + missing sorted alpha
    rest = sorted({c[0] for c in choices if c[0] != "All"} | set(missing))
    return [("All", "All")] + [(v, v) for v in rest]


# -------------------- Public: build form --------------------
def build_global_filter_form(
    df: pd.DataFrame | None,
    data: Mapping[str, Any] | None = None,
    *,
    sales_rep_choices: Iterable[Tuple[str, str]] | None = None,
) -> GlobalFilterForm:
    """
    Build the GlobalFilterForm with choices derived from df and selections taken
    from `data` (usually request.args or a saved view mapping).

    - Accepts CSV and repeated params in `data`
    - Ensures 'All' is mutually exclusive
    - Includes selected values in choices to avoid WTForms validation drops
    - Parses dates permissively for display
    """
    # 1) Derive prefilled selections from `data`
    data = data or {}
    start_v = data.get("start") or data.get("start_date") or data.get("startDate")
    end_v   = data.get("end")   or data.get("end_date")   or data.get("endDate")

    # Multi fields (support repeated, [] and CSV)
    sel_statuses  = _normalize_all_exclusive(_extract_multi(data, "statuses", "status", "order_status"))
    sel_regions   = _normalize_all_exclusive(_extract_multi(data, "regions", "region"))
    sel_methods   = _normalize_all_exclusive(_extract_multi(data, "methods", "shipping_methods", "shipping_method", "shippingMethods"))
    sel_customers = _normalize_all_exclusive(_extract_multi(data, "customers", "customer_ids", "customer", "customerId"))
    sel_suppliers = _normalize_all_exclusive(_extract_multi(data, "suppliers", "supplier_ids", "supplier", "supplierId"))
    sel_products  = _normalize_all_exclusive(_extract_multi(data, "products", "product_ids", "product", "productId"))
    # Keep drilldown entity params (e.g. salesrep_id) out of global filter hydration.
    sel_sales_reps = _normalize_all_exclusive(_extract_multi(data, "sales_reps", "sales_rep_ids", "salesreps", "salesrep_ids"))

    # 2) Create form with those data (WTForms populates selected flags)
    form = GlobalFilterForm(data={
        "start_date": _coerce_date(start_v),
        "end_date": _coerce_date(end_v),
        "statuses": sel_statuses,
        "regions": sel_regions,
        "shipping_methods": sel_methods,
        "customers": sel_customers,
        "suppliers": sel_suppliers,
        "products": sel_products,
        "sales_reps": sel_sales_reps,
    })

    # 3) Build choices from df
    # Regions
    if df is not None and not df.empty:
        cols_lower = {c.lower(): c for c in df.columns}
    else:
        cols_lower = {}

    # Statuses
    status_candidate_keys = ("orderstatus", "order_status", "status")
    if any(k in cols_lower for k in status_candidate_keys):
        col = next((cols_lower.get(k) for k in status_candidate_keys if k in cols_lower), None)
        status_choices = _choices_from_series(df[col].dropna()) if col else [("All", "All")]
    else:
        status_choices = [("All", "All")]
    form.statuses.choices = _union_choices_with_selected(status_choices, sel_statuses)

    # Regions
    region_candidate_keys = ("region_name", "region", "regionname", "province", "stateprovince", "state", "salesregion")
    if any(k in cols_lower for k in region_candidate_keys):
        col = next((cols_lower.get(k) for k in region_candidate_keys if k in cols_lower), None)
        region_choices = _choices_from_series(df[col].dropna()) if col else [("All", "All")]
    else:
        region_choices = [("All", "All")]
    form.regions.choices = _union_choices_with_selected(region_choices, sel_regions)

    # Shipping methods (canonical via shipping_name_series)
    ship_series = shipping_name_series(df) if df is not None and not df.empty else pd.Series(dtype="string")
    if ship_series is not None and not ship_series.empty:
        method_choices = _choices_from_series(ship_series.dropna())
    else:
        method_choices = [("All", "All")]
    form.shipping_methods.choices = _union_choices_with_selected(method_choices, sel_methods)

    # Customers (by name)
    if any(k in cols_lower for k in ("customername", "customer_name", "name", "customer")):
        col = cols_lower.get("customername") or cols_lower.get("customer_name") or cols_lower.get("customer") or cols_lower.get("name")
        cust_choices = _choices_from_series(df[col].dropna())
    else:
        cust_choices = [("All", "All")]
    form.customers.choices = _union_choices_with_selected(cust_choices, sel_customers)

    # Suppliers (by name)
    if any(k in cols_lower for k in ("suppliername", "supplier_name", "supplier", "name")):
        col = cols_lower.get("suppliername") or cols_lower.get("supplier_name") or cols_lower.get("supplier") or cols_lower.get("name")
        sup_choices = _choices_from_series(df[col].dropna())
    else:
        sup_choices = [("All", "All")]
    form.suppliers.choices = _union_choices_with_selected(sup_choices, sel_suppliers)

    # Products (prefer product name / SKU)
    if any(k in cols_lower for k in ("productname", "product_name", "product", "name", "sku", "itemname")):
        col = (
            cols_lower.get("productname")
            or cols_lower.get("product_name")
            or cols_lower.get("product")
            or cols_lower.get("sku")
            or cols_lower.get("itemname")
            or cols_lower.get("name")
        )
        product_choices = _choices_from_series(df[col].dropna()) if col else [("All", "All")]
    else:
        product_choices = [("All", "All")]
    form.products.choices = _union_choices_with_selected(product_choices, sel_products)

    # Sales reps (options supplied or derived best-effort)
    if sales_rep_choices is not None:
        rep_choices = [("All", "All")] + [(str(val).strip(), str(lbl).strip()) for val, lbl in sales_rep_choices if str(val).strip()]
    else:
        rep_choices = [("All", "All")]
        if df is not None and not df.empty:
            rep_cols = []
            for candidate in ("SalesRepName", "SalesRepId", "PrimarySalesRepId", "PrimarySalesRepName"):
                if candidate in df.columns:
                    rep_cols.append(candidate)
            values: set[str] = set()
            for col in rep_cols:
                series = df[col].astype("string").str.strip().dropna()
                values.update({v for v in series if v and v not in _SENTINELS_ALL})
            rep_choices = [("All", "All")] + [(v, v) for v in sorted(values)]
    form.sales_reps.choices = _union_choices_with_selected(rep_choices, sel_sales_reps)

    # 4) Normalize date display if strings sneaked in (defensive)
    try:
        for fld in (form.start_date, form.end_date):
            v = getattr(fld, "data", None)
            if isinstance(v, str) and v.strip():
                co = _coerce_date(v)
                if co:
                    fld.data = co
    except Exception:
        pass

    return form
