#!/usr/bin/env python3
"""
Quick smoke-check for the analytics parquet cache.

Prints:
- Row count
- Date min/max for the primary date column
- Sample customers (3)
- Monthly aggregation head (counts per month)

Exits with code 1 if dataframe is empty, date column is missing or NaT-only,
customer field cannot be sampled, or monthly aggregation is empty.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

import pandas as pd
from dotenv import load_dotenv


def _load_env() -> None:
    root = Path(__file__).resolve().parents[1]
    env_dev = root / ".env.dev"
    if env_dev.exists():
        load_dotenv(env_dev, override=True)
        os.environ.setdefault("FLASK_ENV", "development")
    load_dotenv(override=False)


def _pick_date_column(df: pd.DataFrame) -> Optional[str]:
    # Prefer already-datetime dtypes, especially canonical columns
    preferred = ["Date", "ShipDate", "DateOrdered_line", "DateOrdered", "DateShipped_line", "SubmittedAt", "CreatedAt"]
    datetime_like = [c for c in df.columns if str(df[c].dtype).startswith("datetime64")]
    for c in preferred:
        if c in datetime_like and df[c].notna().any():
            return c

    # Try preferred columns by parsing
    for c in preferred:
        if c in df.columns:
            s = pd.to_datetime(df[c], errors="coerce")
            if s.notna().any():
                return c

    # General heuristic: parse and require reasonable year range and density
    for c in df.columns:
        try:
            s = pd.to_datetime(df[c], errors="coerce")
            if not s.notna().any():
                continue
            valid = s.dropna()
            ratio = len(valid) / max(len(s), 1)
            years = valid.dt.year
            if ratio >= 0.05 and years.min() >= 1990 and years.max() <= 2100:
                return c
        except Exception:
            continue
    return None


def _pick_customer_column(df: pd.DataFrame) -> Optional[str]:
    candidates = [
        "Name_customer",
        "CustomerName",
        "Customer_Name",
        "Customer",
        "Name",
    ]
    for c in candidates:
        if c in df.columns and df[c].dropna().astype(str).str.strip().ne("").any():
            return c
    # Fall back to CustomerId for sampling
    if "CustomerId" in df.columns:
        return "CustomerId"
    return None


def main() -> int:
    _load_env()

    path = os.getenv("PARQUET_PATH", "cache/fact_analytics.parquet")
    p = Path(path)
    if not p.exists():
        print(f"Parquet not found at: {p}. Run the loader or set PARQUET_PATH.")
        return 1

    try:
        df = pd.read_parquet(p.as_posix())
    except Exception as e:
        if "pyarrow" in str(e).lower() or "fastparquet" in str(e).lower():
            print(
                "Reading parquet requires 'pyarrow' or 'fastparquet'.\n"
                "Try: pip install pyarrow (recommended)"
            )
        print(f"Failed to read parquet: {e}")
        return 1

    # Row count
    n = len(df)
    print(f"rows: {n}")
    if n == 0:
        print("Dataframe is empty.")
        return 1

    # Date min/max
    date_col = _pick_date_column(df)
    if not date_col:
        print("No suitable date column found (or all NaT).")
        return 1
    s_date = pd.to_datetime(df[date_col], errors="coerce")
    if s_date.notna().any():
        dmin = s_date.min()
        dmax = s_date.max()
        print(f"date_column: {date_col}")
        print(f"date_min: {dmin}")
        print(f"date_max: {dmax}")
    else:
        print(f"Date column '{date_col}' is NaT-only.")
        return 1

    # Customers sample
    cust_col = _pick_customer_column(df)
    if not cust_col:
        print("No suitable customer column found for sampling.")
        return 1
    sample_vals = (
        df[cust_col]
        .dropna()
        .astype(str)
        .str.strip()
        .replace({"": None})
        .dropna()
        .head(100)
        .unique()[:3]
        .tolist()
    )
    if not sample_vals:
        print(f"Customer column '{cust_col}' has no sample-able values.")
        return 1
    print(f"sample_customers[{cust_col}]: {sample_vals}")

    # Monthly aggregation (counts)
    month = s_date.dt.to_period("M")
    monthly_counts = month.value_counts().sort_index()
    if monthly_counts.empty:
        print("Monthly aggregation is empty.")
        return 1
    print("monthly_counts (head):")
    print(monthly_counts.head().to_string())

    return 0


if __name__ == "__main__":
    sys.exit(main())
