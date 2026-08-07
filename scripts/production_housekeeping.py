#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import shutil
import time
from pathlib import Path


ROTATED_LOG_RE = re.compile(r"^(?P<base>.+)\.(?P<index>\d+)$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prune stale production backups and rotated logs safely.")
    parser.add_argument("--backups-root", default="/opt/wholesale_analytics/backups")
    parser.add_argument("--logs-root", default="/opt/wholesale_analytics/logs")
    parser.add_argument("--keep-backups", type=int, default=8)
    parser.add_argument("--keep-rotated-logs", type=int, default=4)
    parser.add_argument("--devserver-max-age-days", type=int, default=7)
    parser.add_argument("--apply", action="store_true", help="Delete the planned files instead of printing them.")
    return parser.parse_args()


def sorted_backup_dirs(root: Path) -> list[Path]:
    if not root.exists():
        return []
    dirs = [path for path in root.iterdir() if path.is_dir()]
    return sorted(dirs, key=lambda path: path.stat().st_mtime, reverse=True)


def prune_backups(root: Path, keep_backups: int) -> list[Path]:
    dirs = sorted_backup_dirs(root)
    return dirs[max(0, keep_backups):]


def prune_logs(root: Path, keep_rotated_logs: int, devserver_max_age_days: int) -> list[Path]:
    if not root.exists():
        return []
    deletions: list[Path] = []
    grouped: dict[str, list[tuple[int, Path]]] = {}
    cutoff = time.time() - max(1, devserver_max_age_days) * 86400

    for path in root.iterdir():
        if not path.is_file():
            continue
        if path.name.startswith("devserver.") and path.name.endswith(".log"):
            try:
                if path.stat().st_mtime <= cutoff:
                    deletions.append(path)
            except FileNotFoundError:
                continue
            continue
        match = ROTATED_LOG_RE.match(path.name)
        if not match:
            continue
        base = match.group("base")
        index = int(match.group("index"))
        grouped.setdefault(base, []).append((index, path))

    for items in grouped.values():
        for index, path in sorted(items, key=lambda item: item[0]):
            if index > keep_rotated_logs:
                deletions.append(path)
    return sorted({path for path in deletions}, key=lambda item: item.as_posix())


def apply_deletions(paths: list[Path], apply: bool) -> None:
    action = "DELETE" if apply else "KEEP-DRY-RUN"
    for path in paths:
        print(f"{action} {path}")
        if not apply:
            continue
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=False)
        else:
            path.unlink(missing_ok=True)


def main() -> int:
    args = parse_args()
    backups_root = Path(args.backups_root).expanduser().resolve()
    logs_root = Path(args.logs_root).expanduser().resolve()

    backup_deletions = prune_backups(backups_root, max(0, args.keep_backups))
    log_deletions = prune_logs(logs_root, max(0, args.keep_rotated_logs), max(1, args.devserver_max_age_days))

    print(f"Backups root: {backups_root}")
    print(f"Logs root: {logs_root}")
    print(f"Backup deletions planned: {len(backup_deletions)}")
    print(f"Log deletions planned: {len(log_deletions)}")

    apply_deletions(backup_deletions, args.apply)
    apply_deletions(log_deletions, args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
