from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict

from app.services import watermark_store
from etl import incremental_refresh

logger = logging.getLogger(__name__)

# ETL runs are the only place live SQL is allowed.
os.environ.setdefault("APP_MODE", "etl")
os.environ.setdefault("ALLOW_LIVE_SQL", "1")

DATASET_PATH = watermark_store.resolve_dataset_path()
LOCK_PATH = DATASET_PATH / ".refresh.lock"
MANIFEST_PATH = DATASET_PATH / watermark_store.MANIFEST_NAME


def get_manifest() -> Dict[str, Any]:
    """Return the current manifest if it exists."""
    return watermark_store.read_manifest(DATASET_PATH)


def build_initial_dataset(from_date: str = "2017-01-01") -> Dict[str, Any]:
    """Full rebuild of the partitioned parquet dataset (ETL only)."""
    return incremental_refresh.initial_build(start=from_date, dataset_path=DATASET_PATH)


def incremental_update() -> Dict[str, Any]:
    """Incremental refresh using the manifest watermark + lookback."""
    return incremental_refresh.refresh_once(start=os.getenv("INITIAL_START_DATE", "2017-01-01"), dataset_path=DATASET_PATH)


def update_manifest(manifest: Dict[str, Any]):
    """Write a manifest atomically. Intended for maintenance jobs."""
    return watermark_store.write_manifest_atomic(manifest, dataset_path=DATASET_PATH)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fact dataset ETL")
    parser.add_argument("--build", action="store_true", help="Run the full bootstrap from 2017-01-01")
    parser.add_argument("--incremental", action="store_true", help="Run an incremental refresh")
    parser.add_argument("--from-date", dest="from_date", help="Override the bootstrap start date (YYYY-MM-DD)")
    args = parser.parse_args()

    if args.build:
        result = build_initial_dataset(from_date=args.from_date or "2017-01-01")
    elif args.incremental:
        result = incremental_update()
    else:
        parser.error("Specify --build or --incremental")
    print(json.dumps(result, indent=2, default=str))

