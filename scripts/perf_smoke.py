from __future__ import annotations

import time
from typing import Any

import pandas as pd

try:
    import data_loader as loader  # type: ignore
except ImportError as exc:  # pragma: no cover
    raise SystemExit(f"data_loader import failed: {exc}") from exc


def _describe_frame(df: pd.DataFrame | Any) -> str:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return "0 rows"
    rows = len(df.index)
    cols = len(df.columns)
    return f"{rows:,} rows × {cols} cols"


def main() -> None:
    t0 = time.perf_counter()
    try:
        df = loader.get_dataframe(start=None, end=None, window_days=None)
    except Exception as exc:
        duration = time.perf_counter() - t0
        print(f"[perf] loader.get_dataframe failed after {duration:.2f}s: {exc}")
        return

    duration = time.perf_counter() - t0
    print(f"[perf] fetched { _describe_frame(df) } in {duration:.2f}s")


if __name__ == "__main__":
    main()
