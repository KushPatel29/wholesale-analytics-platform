from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Sequence

import pandas as pd
from flask import current_app, has_app_context

from app.services import fact_store


ATTRIBUTION_HISTORICAL = "historical_rep"
ATTRIBUTION_CURRENT_OWNER = "current_owner"
ROSTER_CURRENT_ONLY = "current_only"
ROSTER_INCLUDE_FORMER = "include_former"
BRIDGE_VIEW_NAME = "salesrep_assignment_history"
SUCCESSION_VIEW_NAME = "salesrep_succession_map"
_ORDER_START_FLOOR = pd.Timestamp("1900-01-01")
_ORDER_END_CEILING = pd.Timestamp("2262-04-11")

_BRIDGE_LOCK = threading.RLock()
_BRIDGE_CACHE: dict[str, tuple[pd.DataFrame, "OwnershipBridgeMeta"]] = {}
_SUCCESSION_LOCK = threading.RLock()
_SUCCESSION_CACHE: dict[str, tuple[pd.DataFrame, "SuccessorMapMeta"]] = {}


@dataclass(frozen=True)
class AttributionControls:
    attribution_mode: str = ATTRIBUTION_CURRENT_OWNER
    roster_mode: str = ROSTER_CURRENT_ONLY
    transfer_only: bool = False

    @property
    def is_current_owner_mode(self) -> bool:
        return self.attribution_mode == ATTRIBUTION_CURRENT_OWNER

    def as_dict(self) -> dict[str, Any]:
        return {
            "attribution_mode": self.attribution_mode,
            "roster_mode": self.roster_mode,
            "transfer_only": bool(self.transfer_only),
        }


@dataclass(frozen=True)
class OwnershipBridgeMeta:
    available: bool
    source: str | None
    rows: int
    dropped_rows: int
    overlapping_current_assignments: int
    bridge_kind: str | None
    warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": bool(self.available),
            "source": self.source,
            "rows": int(self.rows),
            "dropped_rows": int(self.dropped_rows),
            "overlapping_current_assignments": int(self.overlapping_current_assignments),
            "bridge_kind": self.bridge_kind,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class SuccessorMapMeta:
    available: bool
    source: str | None
    rows: int
    dropped_rows: int
    scoped_rows: int
    warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": bool(self.available),
            "source": self.source,
            "rows": int(self.rows),
            "dropped_rows": int(self.dropped_rows),
            "scoped_rows": int(self.scoped_rows),
            "warnings": list(self.warnings),
        }


def parse_attribution_controls(args: Any) -> AttributionControls:
    getter = args.get if hasattr(args, "get") else (lambda _k, _d=None: None)

    raw_mode = str(
        getter("attribution_mode")
        or getter("mode")
        or getter("reporting_mode")
        or getter("rep_attribution_mode")
        or ATTRIBUTION_CURRENT_OWNER
    ).strip().lower()
    if raw_mode in {"current", "owner", "current_owner", "current_account_owner", "portfolio"}:
        attribution_mode = ATTRIBUTION_CURRENT_OWNER
    else:
        attribution_mode = ATTRIBUTION_HISTORICAL

    raw_roster = str(
        getter("roster_mode")
        or getter("rep_roster")
        or getter("rep_status")
        or getter("include_former_reps")
        or getter("include_former")
        or ROSTER_CURRENT_ONLY
    ).strip().lower()
    if raw_roster in {"1", "true", "yes", "on", "include_former", "include_former_reps", "all"}:
        roster_mode = ROSTER_INCLUDE_FORMER
    elif raw_roster in {"former", "historical", "include_inactive"}:
        roster_mode = ROSTER_INCLUDE_FORMER
    else:
        roster_mode = ROSTER_CURRENT_ONLY

    raw_transfer = getter("transfer_only") or getter("transfers_only") or getter("moved_accounts_only")
    transfer_only = str(raw_transfer or "").strip().lower() in {"1", "true", "yes", "on"}

    return AttributionControls(
        attribution_mode=attribution_mode,
        roster_mode=roster_mode,
        transfer_only=transfer_only,
    )


def register_bridge_view(view_name: str = BRIDGE_VIEW_NAME) -> OwnershipBridgeMeta:
    frame, meta = load_bridge_frame()
    conn = fact_store.get_conn()
    try:
        conn.unregister(view_name)
    except Exception:
        pass
    conn.register(view_name, frame)
    return meta


def register_successor_view(view_name: str = SUCCESSION_VIEW_NAME) -> SuccessorMapMeta:
    frame, meta = load_successor_frame()
    conn = fact_store.get_conn()
    try:
        conn.unregister(view_name)
    except Exception:
        pass
    conn.register(view_name, frame)
    return meta


def load_bridge_frame() -> tuple[pd.DataFrame, OwnershipBridgeMeta]:
    direct_path = _config_path("CUSTOMER_REP_HISTORY_PATH")
    territory_path = _config_path("TERRITORY_REP_HISTORY_PATH")
    customer_territory_path = _config_path("CUSTOMER_TERRITORY_HISTORY_PATH")

    cache_key = _cache_key(direct_path, territory_path, customer_territory_path)
    with _BRIDGE_LOCK:
        cached = _BRIDGE_CACHE.get(cache_key)
        if cached is not None:
            return cached[0].copy(), cached[1]

    warnings: list[str] = []
    dropped_rows = 0
    bridge_kind: str | None = None
    frame = _empty_bridge_frame()

    direct_df = _load_history_source(direct_path, warnings, label="customer rep history")
    if direct_df is not None and not direct_df.empty:
        frame, dropped_rows = _normalize_customer_rep_history(direct_df)
        bridge_kind = "customer_rep_history"
    else:
        territory_df = _load_history_source(territory_path, warnings, label="territory rep history")
        customer_territory_df = _load_history_source(
            customer_territory_path,
            warnings,
            label="customer territory history",
        )
        if territory_df is not None and customer_territory_df is not None and not territory_df.empty and not customer_territory_df.empty:
            frame, dropped_rows = _normalize_territory_bridge(territory_df, customer_territory_df)
            bridge_kind = "territory_rep_history"

    if frame.empty:
        warnings.append(
            "Ownership history bridge not configured; current-owner rollups fall back to current owner fields on fact rows."
        )

    overlap_count = _overlapping_current_assignments(frame)
    if overlap_count > 0:
        warnings.append(
            f"Ownership bridge contains {overlap_count} customer(s) with overlapping current assignments; latest assignment wins in reporting."
        )

    source = direct_path or territory_path or customer_territory_path
    meta = OwnershipBridgeMeta(
        available=not frame.empty,
        source=source,
        rows=int(len(frame.index)),
        dropped_rows=int(dropped_rows),
        overlapping_current_assignments=int(overlap_count),
        bridge_kind=bridge_kind,
        warnings=tuple(dict.fromkeys(warnings)),
    )
    with _BRIDGE_LOCK:
        _BRIDGE_CACHE[cache_key] = (frame.copy(), meta)
    return frame.copy(), meta


def load_successor_frame() -> tuple[pd.DataFrame, SuccessorMapMeta]:
    path_value = _config_path("SALESREP_SUCCESSION_PATH")
    cache_key = _cache_key(path_value)
    with _SUCCESSION_LOCK:
        cached = _SUCCESSION_CACHE.get(cache_key)
        if cached is not None:
            return cached[0].copy(), cached[1]

    warnings: list[str] = []
    frame = _empty_successor_frame()
    dropped_rows = 0

    raw_df = _load_history_source(path_value, warnings, label="sales rep succession history")
    if raw_df is not None and not raw_df.empty:
        frame, dropped_rows = _normalize_successor_history(raw_df)

    scoped_rows = int(
        (
            frame["customer_id"].notna()
            | frame["territory_id"].notna()
        ).sum()
    ) if not frame.empty else 0
    meta = SuccessorMapMeta(
        available=not frame.empty,
        source=path_value,
        rows=int(len(frame.index)),
        dropped_rows=int(dropped_rows),
        scoped_rows=scoped_rows,
        warnings=tuple(dict.fromkeys(warnings)),
    )
    with _SUCCESSION_LOCK:
        _SUCCESSION_CACHE[cache_key] = (frame.copy(), meta)
    return frame.copy(), meta


def _cache_key(*paths: str | None) -> str:
    parts: list[str] = []
    for raw_path in paths:
        if not raw_path:
            parts.append("")
            continue
        path = Path(raw_path).expanduser()
        try:
            stat = path.stat()
            parts.append(f"{path.resolve()}:{int(stat.st_mtime)}:{int(stat.st_size)}")
        except Exception:
            parts.append(str(path))
    return "|".join(parts)


def _config_path(name: str) -> str | None:
    if has_app_context():
        try:
            value = current_app.config.get(name)
            if value:
                return str(value).strip() or None
        except Exception:
            pass
    value = os.getenv(name)
    return str(value).strip() or None if value else None


def _load_history_source(path_value: str | None, warnings: list[str], *, label: str) -> pd.DataFrame | None:
    if not path_value:
        return None
    path = Path(path_value).expanduser()
    if not path.exists():
        warnings.append(f"Configured {label} file was not found.")
        return None
    try:
        if path.suffix.lower() in {".parquet", ".pq"}:
            return pd.read_parquet(path)
        if path.suffix.lower() in {".csv", ".txt"}:
            return pd.read_csv(path)
        warnings.append(f"Unsupported {label} file format configured.")
        return None
    except Exception as exc:
        warnings.append(f"Failed to read {label} ({exc.__class__.__name__}).")
        return None


def _norm_label(value: Any) -> str:
    return "".join(ch for ch in str(value or "").strip().lower() if ch.isalnum())


def _find_column(df: pd.DataFrame, candidates: Iterable[str]) -> str | None:
    cols = list(df.columns)
    lower_map = {str(col).strip().lower(): col for col in cols}
    norm_map = {_norm_label(col): col for col in cols}
    for cand in candidates:
        if cand in df.columns:
            return cand
        raw = str(cand).strip().lower()
        if raw in lower_map:
            return lower_map[raw]
        norm = _norm_label(cand)
        if norm in norm_map:
            return norm_map[norm]
    return None


def _normalize_text(series: pd.Series | None) -> pd.Series:
    if series is None:
        return pd.Series(dtype="string")
    out = series.astype("string").str.strip()
    return out.where(out.notna() & (out != ""))


def _normalize_bool(series: pd.Series | None) -> pd.Series:
    if series is None:
        return pd.Series(dtype="boolean")
    if str(series.dtype).lower() in {"bool", "boolean"}:
        return series.astype("boolean")
    lowered = series.astype("string").str.strip().str.lower()
    return lowered.map(
        {
            "1": True,
            "true": True,
            "yes": True,
            "y": True,
            "on": True,
            "0": False,
            "false": False,
            "no": False,
            "n": False,
            "off": False,
        }
    ).astype("boolean")


def _normalize_date(series: pd.Series | None) -> pd.Series:
    if series is None:
        return pd.Series(dtype="datetime64[ns]")
    out = pd.to_datetime(series, errors="coerce")
    try:
        if getattr(out.dt, "tz", None) is not None:
            out = out.dt.tz_localize(None)
    except Exception:
        pass
    return out.dt.normalize()


def _empty_bridge_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "customer_id": pd.Series(dtype="string"),
            "territory_id": pd.Series(dtype="string"),
            "territory_name": pd.Series(dtype="string"),
            "rep_id": pd.Series(dtype="string"),
            "rep_name": pd.Series(dtype="string"),
            "prior_rep_id": pd.Series(dtype="string"),
            "prior_rep_name": pd.Series(dtype="string"),
            "assignment_start_date": pd.Series(dtype="datetime64[ns]"),
            "assignment_end_date": pd.Series(dtype="datetime64[ns]"),
            "is_current": pd.Series(dtype="boolean"),
            "ownership_type": pd.Series(dtype="string"),
            "rep_is_active": pd.Series(dtype="boolean"),
            "mapping_confidence": pd.Series(dtype="string"),
            "dq_status": pd.Series(dtype="string"),
        }
    )


def _empty_successor_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "customer_id": pd.Series(dtype="string"),
            "territory_id": pd.Series(dtype="string"),
            "territory_name": pd.Series(dtype="string"),
            "prior_rep_id": pd.Series(dtype="string"),
            "prior_rep_name": pd.Series(dtype="string"),
            "successor_rep_id": pd.Series(dtype="string"),
            "successor_rep_name": pd.Series(dtype="string"),
            "effective_start_date": pd.Series(dtype="datetime64[ns]"),
            "effective_end_date": pd.Series(dtype="datetime64[ns]"),
            "is_current": pd.Series(dtype="boolean"),
            "successor_rep_is_active": pd.Series(dtype="boolean"),
            "mapping_confidence": pd.Series(dtype="string"),
            "dq_status": pd.Series(dtype="string"),
        }
    )


def _normalize_customer_rep_history(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    customer_col = _find_column(df, ("customer_id", "customerid", "CustomerId", "customer"))
    rep_id_col = _find_column(df, ("rep_id", "repid", "sales_rep_id", "salesrepid", "owner_rep_id"))
    rep_name_col = _find_column(df, ("rep_name", "sales_rep_name", "salesrepname", "owner_rep_name", "owner"))
    if customer_col is None or (rep_id_col is None and rep_name_col is None):
        return _empty_bridge_frame(), int(len(df.index))

    territory_id_col = _find_column(df, ("territory_id", "territoryid", "region_id", "regionid"))
    territory_name_col = _find_column(df, ("territory_name", "territory", "territoryname", "region_name", "region"))
    prior_rep_id_col = _find_column(df, ("prior_rep_id", "previous_rep_id", "former_rep_id", "replaced_rep_id"))
    prior_rep_name_col = _find_column(df, ("prior_rep_name", "previous_rep_name", "former_rep_name", "replaced_rep_name"))
    start_col = _find_column(df, ("assignment_start_date", "start_date", "effective_start_date", "valid_from"))
    end_col = _find_column(df, ("assignment_end_date", "end_date", "effective_end_date", "valid_to"))
    current_col = _find_column(df, ("is_current", "current_flag", "active_assignment"))
    ownership_type_col = _find_column(df, ("ownership_type", "assignment_type", "owner_type"))
    active_col = _find_column(df, ("rep_is_active", "active_rep", "is_active", "rep_active"))
    confidence_col = _find_column(df, ("mapping_confidence", "confidence", "attribution_confidence"))
    dq_col = _find_column(df, ("dq_status", "data_quality_status", "ownership_dq_status", "assignment_status"))

    out = pd.DataFrame(
        {
            "customer_id": _normalize_text(df[customer_col]),
            "territory_id": _normalize_text(df[territory_id_col]) if territory_id_col else pd.Series(dtype="string"),
            "territory_name": _normalize_text(df[territory_name_col]) if territory_name_col else pd.Series(dtype="string"),
            "rep_id": _normalize_text(df[rep_id_col]) if rep_id_col else pd.Series(dtype="string"),
            "rep_name": _normalize_text(df[rep_name_col]) if rep_name_col else pd.Series(dtype="string"),
            "prior_rep_id": _normalize_text(df[prior_rep_id_col]) if prior_rep_id_col else pd.Series(dtype="string"),
            "prior_rep_name": _normalize_text(df[prior_rep_name_col]) if prior_rep_name_col else pd.Series(dtype="string"),
            "assignment_start_date": _normalize_date(df[start_col]) if start_col else pd.Series(dtype="datetime64[ns]"),
            "assignment_end_date": _normalize_date(df[end_col]) if end_col else pd.Series(dtype="datetime64[ns]"),
            "is_current": _normalize_bool(df[current_col]) if current_col else pd.Series(dtype="boolean"),
            "ownership_type": _normalize_text(df[ownership_type_col]) if ownership_type_col else pd.Series(dtype="string"),
            "rep_is_active": _normalize_bool(df[active_col]) if active_col else pd.Series(dtype="boolean"),
            "mapping_confidence": _normalize_text(df[confidence_col]) if confidence_col else pd.Series(dtype="string"),
            "dq_status": _normalize_text(df[dq_col]) if dq_col else pd.Series(dtype="string"),
        }
    )
    out["mapping_confidence"] = out["mapping_confidence"].fillna("customer_history")
    return _finalize_bridge_frame(out)


def _normalize_successor_history(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    customer_col = _find_column(df, ("customer_id", "customerid", "CustomerId", "customer"))
    territory_id_col = _find_column(df, ("territory_id", "territoryid", "region_id", "regionid"))
    territory_name_col = _find_column(df, ("territory_name", "territory", "territoryname", "region_name", "region"))
    prior_rep_id_col = _find_column(
        df,
        ("prior_rep_id", "previous_rep_id", "former_rep_id", "replaced_rep_id", "old_rep_id", "from_rep_id"),
    )
    prior_rep_name_col = _find_column(
        df,
        ("prior_rep_name", "previous_rep_name", "former_rep_name", "replaced_rep_name", "old_rep_name", "from_rep_name"),
    )
    successor_rep_id_col = _find_column(
        df,
        (
            "successor_rep_id",
            "current_rep_id",
            "replacement_rep_id",
            "new_rep_id",
            "rep_id",
            "owner_rep_id",
        ),
    )
    successor_rep_name_col = _find_column(
        df,
        (
            "successor_rep_name",
            "current_rep_name",
            "replacement_rep_name",
            "new_rep_name",
            "rep_name",
            "owner_rep_name",
        ),
    )
    if (prior_rep_id_col is None and prior_rep_name_col is None) or (
        successor_rep_id_col is None and successor_rep_name_col is None
    ):
        return _empty_successor_frame(), int(len(df.index))

    start_col = _find_column(df, ("effective_start_date", "assignment_start_date", "start_date", "valid_from", "transfer_date"))
    end_col = _find_column(df, ("effective_end_date", "assignment_end_date", "end_date", "valid_to"))
    current_col = _find_column(df, ("is_current", "current_flag", "current_owner_flag", "active_assignment"))
    active_col = _find_column(df, ("successor_rep_is_active", "rep_is_active", "current_rep_is_active", "is_active", "rep_active"))
    confidence_col = _find_column(df, ("mapping_confidence", "confidence", "attribution_confidence"))
    dq_col = _find_column(df, ("dq_status", "data_quality_status", "ownership_dq_status", "assignment_status"))

    out = pd.DataFrame(
        {
            "customer_id": _normalize_text(df[customer_col]) if customer_col else pd.Series(dtype="string"),
            "territory_id": _normalize_text(df[territory_id_col]) if territory_id_col else pd.Series(dtype="string"),
            "territory_name": _normalize_text(df[territory_name_col]) if territory_name_col else pd.Series(dtype="string"),
            "prior_rep_id": _normalize_text(df[prior_rep_id_col]) if prior_rep_id_col else pd.Series(dtype="string"),
            "prior_rep_name": _normalize_text(df[prior_rep_name_col]) if prior_rep_name_col else pd.Series(dtype="string"),
            "successor_rep_id": _normalize_text(df[successor_rep_id_col]) if successor_rep_id_col else pd.Series(dtype="string"),
            "successor_rep_name": _normalize_text(df[successor_rep_name_col]) if successor_rep_name_col else pd.Series(dtype="string"),
            "effective_start_date": _normalize_date(df[start_col]) if start_col else pd.Series(dtype="datetime64[ns]"),
            "effective_end_date": _normalize_date(df[end_col]) if end_col else pd.Series(dtype="datetime64[ns]"),
            "is_current": _normalize_bool(df[current_col]) if current_col else pd.Series(dtype="boolean"),
            "successor_rep_is_active": _normalize_bool(df[active_col]) if active_col else pd.Series(dtype="boolean"),
            "mapping_confidence": _normalize_text(df[confidence_col]) if confidence_col else pd.Series(dtype="string"),
            "dq_status": _normalize_text(df[dq_col]) if dq_col else pd.Series(dtype="string"),
        }
    )
    out["mapping_confidence"] = out["mapping_confidence"].fillna("succession_map")
    out["dq_status"] = out["dq_status"].fillna("ok")
    return _finalize_successor_frame(out)


def _normalize_territory_bridge(
    territory_rep_df: pd.DataFrame,
    customer_territory_df: pd.DataFrame,
) -> tuple[pd.DataFrame, int]:
    terr_id_col = _find_column(territory_rep_df, ("territory_id", "territoryid", "region_id", "regionid"))
    rep_id_col = _find_column(territory_rep_df, ("rep_id", "sales_rep_id", "salesrepid", "owner_rep_id"))
    rep_name_col = _find_column(territory_rep_df, ("rep_name", "sales_rep_name", "salesrepname", "owner_rep_name", "owner"))
    if terr_id_col is None or (rep_id_col is None and rep_name_col is None):
        return _empty_bridge_frame(), int(len(territory_rep_df.index) + len(customer_territory_df.index))

    terr_name_col = _find_column(territory_rep_df, ("territory_name", "territory", "territoryname", "region_name", "region"))
    terr_start_col = _find_column(territory_rep_df, ("assignment_start_date", "start_date", "effective_start_date", "valid_from"))
    terr_end_col = _find_column(territory_rep_df, ("assignment_end_date", "end_date", "effective_end_date", "valid_to"))
    terr_current_col = _find_column(territory_rep_df, ("is_current", "current_flag", "active_assignment"))
    terr_owner_type_col = _find_column(territory_rep_df, ("ownership_type", "assignment_type", "owner_type"))
    terr_active_col = _find_column(territory_rep_df, ("rep_is_active", "active_rep", "is_active", "rep_active"))

    customer_id_col = _find_column(customer_territory_df, ("customer_id", "customerid", "CustomerId", "customer"))
    customer_territory_id_col = _find_column(customer_territory_df, ("territory_id", "territoryid", "region_id", "regionid"))
    if customer_id_col is None or customer_territory_id_col is None:
        return _empty_bridge_frame(), int(len(territory_rep_df.index) + len(customer_territory_df.index))

    cust_start_col = _find_column(customer_territory_df, ("assignment_start_date", "start_date", "effective_start_date", "valid_from"))
    cust_end_col = _find_column(customer_territory_df, ("assignment_end_date", "end_date", "effective_end_date", "valid_to"))
    cust_current_col = _find_column(customer_territory_df, ("is_current", "current_flag", "active_assignment"))

    territory = pd.DataFrame(
        {
            "territory_id": _normalize_text(territory_rep_df[terr_id_col]),
            "territory_name": _normalize_text(territory_rep_df[terr_name_col]) if terr_name_col else pd.Series(dtype="string"),
            "rep_id": _normalize_text(territory_rep_df[rep_id_col]) if rep_id_col else pd.Series(dtype="string"),
            "rep_name": _normalize_text(territory_rep_df[rep_name_col]) if rep_name_col else pd.Series(dtype="string"),
            "territory_start": _normalize_date(territory_rep_df[terr_start_col]) if terr_start_col else pd.Series(dtype="datetime64[ns]"),
            "territory_end": _normalize_date(territory_rep_df[terr_end_col]) if terr_end_col else pd.Series(dtype="datetime64[ns]"),
            "territory_current": _normalize_bool(territory_rep_df[terr_current_col]) if terr_current_col else pd.Series(dtype="boolean"),
            "ownership_type": _normalize_text(territory_rep_df[terr_owner_type_col]) if terr_owner_type_col else pd.Series(dtype="string"),
            "rep_is_active": _normalize_bool(territory_rep_df[terr_active_col]) if terr_active_col else pd.Series(dtype="boolean"),
        }
    )
    territory = territory[territory["territory_id"].notna()].copy()

    customer_territory = pd.DataFrame(
        {
            "customer_id": _normalize_text(customer_territory_df[customer_id_col]),
            "territory_id": _normalize_text(customer_territory_df[customer_territory_id_col]),
            "customer_start": _normalize_date(customer_territory_df[cust_start_col]) if cust_start_col else pd.Series(dtype="datetime64[ns]"),
            "customer_end": _normalize_date(customer_territory_df[cust_end_col]) if cust_end_col else pd.Series(dtype="datetime64[ns]"),
            "customer_current": _normalize_bool(customer_territory_df[cust_current_col]) if cust_current_col else pd.Series(dtype="boolean"),
        }
    )
    customer_territory = customer_territory[
        customer_territory["customer_id"].notna() & customer_territory["territory_id"].notna()
    ].copy()

    merged = customer_territory.merge(territory, on="territory_id", how="inner")
    if merged.empty:
        return _empty_bridge_frame(), int(len(territory_rep_df.index) + len(customer_territory_df.index))

    max_start = merged[["customer_start", "territory_start"]].max(axis=1)
    end_cols = [merged[col] for col in ("customer_end", "territory_end")]
    min_end = pd.concat(end_cols, axis=1).min(axis=1)

    out = pd.DataFrame(
        {
            "customer_id": merged["customer_id"].astype("string"),
            "territory_id": merged["territory_id"].astype("string"),
            "territory_name": merged["territory_name"].astype("string"),
            "rep_id": merged["rep_id"].astype("string"),
            "rep_name": merged["rep_name"].astype("string"),
            "prior_rep_id": pd.Series(dtype="string"),
            "prior_rep_name": pd.Series(dtype="string"),
            "assignment_start_date": max_start,
            "assignment_end_date": min_end,
            "is_current": (merged["customer_current"].fillna(False) & merged["territory_current"].fillna(False)).astype("boolean"),
            "ownership_type": merged["ownership_type"].astype("string"),
            "rep_is_active": merged["rep_is_active"].astype("boolean"),
            "mapping_confidence": pd.Series("territory_history", index=merged.index, dtype="string"),
            "dq_status": pd.Series("ok", index=merged.index, dtype="string"),
        }
    )
    return _finalize_bridge_frame(out)


def _finalize_bridge_frame(frame: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    if frame is None or frame.empty:
        return _empty_bridge_frame(), 0
    work = frame.copy()
    for col in (
        "customer_id",
        "territory_id",
        "territory_name",
        "rep_id",
        "rep_name",
        "prior_rep_id",
        "prior_rep_name",
        "ownership_type",
        "mapping_confidence",
        "dq_status",
    ):
        if col not in work.columns:
            work[col] = pd.Series(dtype="string")
        work[col] = _normalize_text(work[col])
    for col in ("assignment_start_date", "assignment_end_date"):
        if col not in work.columns:
            work[col] = pd.Series(dtype="datetime64[ns]")
        work[col] = _normalize_date(work[col])
    for col in ("is_current", "rep_is_active"):
        if col not in work.columns:
            work[col] = pd.Series(dtype="boolean")
        work[col] = _normalize_bool(work[col])

    work["rep_id"] = work["rep_id"].where(work["rep_id"].notna(), work["rep_name"])
    work["rep_name"] = work["rep_name"].where(work["rep_name"].notna(), work["rep_id"])
    work["ownership_type"] = work["ownership_type"].fillna("account_owner")
    work["mapping_confidence"] = work["mapping_confidence"].fillna("ownership_history")
    work["dq_status"] = work["dq_status"].fillna("ok")
    inferred_current = work["assignment_end_date"].isna()
    work["is_current"] = work["is_current"].where(work["is_current"].notna(), inferred_current).astype("boolean")

    invalid = work["customer_id"].isna() | work["rep_id"].isna()
    invalid |= (
        work["assignment_start_date"].notna()
        & work["assignment_end_date"].notna()
        & (work["assignment_start_date"] > work["assignment_end_date"])
    )
    dropped_rows = int(invalid.sum())
    work = work.loc[~invalid].copy()
    if work.empty:
        return _empty_bridge_frame(), dropped_rows

    work = work.drop_duplicates(
        subset=[
            "customer_id",
            "territory_id",
            "rep_id",
            "assignment_start_date",
            "assignment_end_date",
            "ownership_type",
        ]
    ).reset_index(drop=True)

    order_start = work["assignment_start_date"].fillna(_ORDER_START_FLOOR)
    order_end = work["assignment_end_date"].fillna(_ORDER_END_CEILING)
    work = work.assign(_order_start=order_start, _order_end=order_end)
    work = work.sort_values(
        ["customer_id", "_order_start", "_order_end", "rep_name", "rep_id"],
        ascending=[True, True, True, True, True],
        kind="mergesort",
    ).reset_index(drop=True)

    derived_prior_id = work.groupby("customer_id")["rep_id"].shift(1)
    derived_prior_name = work.groupby("customer_id")["rep_name"].shift(1)
    changed_owner = derived_prior_id.ne(work["rep_id"]) | derived_prior_name.ne(work["rep_name"])
    changed_owner = changed_owner.fillna(False)
    work["prior_rep_id"] = work["prior_rep_id"].where(work["prior_rep_id"].notna(), derived_prior_id.where(changed_owner))
    work["prior_rep_name"] = work["prior_rep_name"].where(work["prior_rep_name"].notna(), derived_prior_name.where(changed_owner))
    work["prior_rep_id"] = work["prior_rep_id"].where(work["prior_rep_id"] != work["rep_id"])
    work["prior_rep_name"] = work["prior_rep_name"].where(work["prior_rep_name"] != work["rep_name"])
    work["dq_status"] = work["dq_status"].where(work["dq_status"].notna(), "ok")
    work = work.drop(columns=["_order_start", "_order_end"], errors="ignore")
    return work, dropped_rows


def _overlapping_current_assignments(frame: pd.DataFrame) -> int:
    if frame is None or frame.empty:
        return 0
    current = frame.loc[frame["is_current"].fillna(False)].copy()
    if current.empty:
        current = frame.loc[frame["assignment_end_date"].isna()].copy()
    if current.empty:
        return 0
    counts = current.groupby("customer_id")["rep_id"].nunique(dropna=True)
    return int((counts > 1).sum())


def _finalize_successor_frame(frame: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    if frame is None or frame.empty:
        return _empty_successor_frame(), 0

    work = frame.copy()
    for col in (
        "customer_id",
        "territory_id",
        "territory_name",
        "prior_rep_id",
        "prior_rep_name",
        "successor_rep_id",
        "successor_rep_name",
        "mapping_confidence",
        "dq_status",
    ):
        if col not in work.columns:
            work[col] = pd.Series(dtype="string")
        work[col] = _normalize_text(work[col])
    for col in ("effective_start_date", "effective_end_date"):
        if col not in work.columns:
            work[col] = pd.Series(dtype="datetime64[ns]")
        work[col] = _normalize_date(work[col])
    for col in ("is_current", "successor_rep_is_active"):
        if col not in work.columns:
            work[col] = pd.Series(dtype="boolean")
        work[col] = _normalize_bool(work[col])

    work["prior_rep_id"] = work["prior_rep_id"].where(work["prior_rep_id"].notna(), work["prior_rep_name"])
    work["prior_rep_name"] = work["prior_rep_name"].where(work["prior_rep_name"].notna(), work["prior_rep_id"])
    work["successor_rep_id"] = work["successor_rep_id"].where(
        work["successor_rep_id"].notna(),
        work["successor_rep_name"],
    )
    work["successor_rep_name"] = work["successor_rep_name"].where(
        work["successor_rep_name"].notna(),
        work["successor_rep_id"],
    )
    work["mapping_confidence"] = work["mapping_confidence"].fillna("succession_map")
    work["dq_status"] = work["dq_status"].fillna("ok")
    inferred_current = work["effective_end_date"].isna()
    work["is_current"] = work["is_current"].where(work["is_current"].notna(), inferred_current).astype("boolean")

    invalid = (
        work["prior_rep_id"].isna()
        | work["successor_rep_id"].isna()
        | (
            work["effective_start_date"].notna()
            & work["effective_end_date"].notna()
            & (work["effective_start_date"] > work["effective_end_date"])
        )
        | (work["prior_rep_id"] == work["successor_rep_id"])
    )
    dropped_rows = int(invalid.sum())
    work = work.loc[~invalid].copy()
    if work.empty:
        return _empty_successor_frame(), dropped_rows

    work = work.drop_duplicates(
        subset=[
            "customer_id",
            "territory_id",
            "prior_rep_id",
            "successor_rep_id",
            "effective_start_date",
            "effective_end_date",
        ]
    ).reset_index(drop=True)

    order_start = work["effective_start_date"].fillna(_ORDER_START_FLOOR)
    order_end = work["effective_end_date"].fillna(_ORDER_END_CEILING)
    work = work.assign(_order_start=order_start, _order_end=order_end)
    work = work.sort_values(
        ["customer_id", "territory_id", "_order_start", "_order_end", "prior_rep_name", "successor_rep_name"],
        ascending=[True, True, True, True, True, True],
        kind="mergesort",
    ).reset_index(drop=True)
    work = work.drop(columns=["_order_start", "_order_end"], errors="ignore")
    return work, dropped_rows
