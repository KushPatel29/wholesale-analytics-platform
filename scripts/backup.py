#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:  # optional
    def load_dotenv(*a, **k):
        return False


def _root_dir() -> Path:
    # Assume this file is at <root>/scripts/backup.py
    return Path(__file__).resolve().parents[1]


def _parquet_path() -> Path:
    p = os.getenv("PARQUET_PATH", "cache/fact_analytics.parquet")
    return (_root_dir() / p).resolve()


def _auth_db_path() -> Path:
    # auth.db sits in app/auth/auth.db
    return (_root_dir() / "app" / "auth" / "auth.db").resolve()


def _backups_dir() -> Path:
    return (_root_dir() / "backups").resolve()


def create_backup() -> Path:
    load_dotenv(_root_dir() / ".env")
    load_dotenv(_root_dir() / ".env.dev")

    parquet = _parquet_path()
    authdb = _auth_db_path()

    ts = datetime.now().strftime("%Y%m%d_%H%M")
    backups_dir = _backups_dir()
    backups_dir.mkdir(parents=True, exist_ok=True)
    zip_path = backups_dir / f"wholesale_analytics_{ts}.zip"

    with ZipFile(zip_path, mode="w", compression=ZIP_DEFLATED) as z:
        added_any = False
        if parquet.exists():
            z.write(parquet.as_posix(), arcname="fact_analytics.parquet")
            added_any = True
        else:
            print(f"[backup] WARNING: Parquet not found: {parquet}")
        if authdb.exists():
            z.write(authdb.as_posix(), arcname="auth.db")
            added_any = True
        else:
            print(f"[backup] WARNING: Auth DB not found: {authdb}")
        if not added_any:
            # remove empty archive
            try:
                z.close()
            except Exception:
                pass
            zip_path.unlink(missing_ok=True)
            raise RuntimeError("Nothing to back up (no parquet or auth.db)")

    print(f"[backup] Created: {zip_path}")
    return zip_path


def prune_backups(keep_n: int = 10) -> None:
    backups_dir = _backups_dir()
    files = sorted(
        [p for p in backups_dir.glob("wholesale_analytics_*.zip") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if len(files) <= keep_n:
        return
    for p in files[keep_n:]:
        try:
            p.unlink()
            print(f"[backup] Pruned: {p}")
        except Exception as e:
            print(f"[backup] Failed to prune {p}: {e}")


def main(argv: list[str] | None = None) -> int:
    try:
        created = create_backup()
        keep = int(os.getenv("BACKUP_KEEP_N", "10"))
        prune_backups(keep)
        return 0
    except Exception as e:
        print(f"[backup] ERROR: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

