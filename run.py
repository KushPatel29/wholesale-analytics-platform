#!/usr/bin/env python3
import os
import sys
import subprocess
import argparse
import time
import json
import random
import atexit
import signal

import threading
import webbrowser
from pathlib import Path
from typing import Optional, Any, Dict

from dotenv import load_dotenv
import importlib.util as _importutil

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
try:
    os.chdir(ROOT_DIR)
except Exception:
    pass


def _bool_env(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except Exception:
        return default


def load_dev_env() -> None:
    """Load .env.dev if present, overriding default .env values."""
    root = Path(__file__).resolve().parent
    env_dev = root / ".env.dev"
    if env_dev.exists():
        load_dotenv(dotenv_path=env_dev, override=True)
        # Default FLASK_ENV to development when using .env.dev
        os.environ.setdefault("FLASK_ENV", "development")
    # Also load default .env if present (non-overriding so .env.dev wins)
    load_dotenv(override=False)


def print_config_summary() -> None:
    try:
        from app.core.features import load_flags  # light-weight file read
        flags = load_flags()
    except Exception:
        flags = {}
    env = (os.getenv("ENV") or os.getenv("FLASK_ENV") or "production").strip().lower()
    ppath = os.getenv("FACT_DATASET_PATH") or os.getenv("PARQUET_PATH", "cache/fact_dataset")
    try:
        from app.services import watermark_store as _watermark_store  # type: ignore

        resolved_dataset = _watermark_store.resolve_dataset_path().as_posix()
    except Exception:
        resolved_dataset = None
    cache_dir = os.getenv("CACHE_DIR", "cache")
    inprocess_default = env != "production"
    inprocess_enabled = _bool_env("ENABLE_INPROCESS_REFRESH", inprocess_default)
    allow_gunicorn = _bool_env("ALLOW_GUNICORN_INPROCESS_REFRESH", False)
    refresh_interval = _int_env("FACT_REFRESH_INTERVAL_SECONDS", 300)
    refresh_lookback = _int_env("FACT_REFRESH_LOOKBACK_DAYS", 14)
    refresh_lag = _int_env("FACT_REFRESH_LAG_DAYS", refresh_lookback)
    refresh_hot_window = _int_env("FACT_REFRESH_HOT_WINDOW_DAYS", 45)
    refresh_tz = os.getenv("FACT_REFRESH_TZ", "America/Vancouver")
    refresh_jitter = _int_env("FACT_REFRESH_JITTER_SECONDS", 15)
    print("\n=== Config ===")
    print(f"ENV={env}")
    print(f"PARQUET_PATH={ppath}")
    if resolved_dataset:
        print(f"FACT_DATASET_RESOLVED={resolved_dataset}")
    print(f"CACHE_DIR={cache_dir}")
    print(f"ENABLE_INPROCESS_REFRESH={int(inprocess_enabled)}")
    print(f"ALLOW_GUNICORN_INPROCESS_REFRESH={int(allow_gunicorn)}")
    print(f"FACT_REFRESH_INTERVAL_SECONDS={refresh_interval}")
    print(f"FACT_REFRESH_LOOKBACK_DAYS={refresh_lookback}")
    print(f"FACT_REFRESH_LAG_DAYS={refresh_lag}")
    print(f"FACT_REFRESH_HOT_WINDOW_DAYS={refresh_hot_window}")
    print(f"FACT_REFRESH_TZ={refresh_tz}")
    print(f"FACT_REFRESH_JITTER_SECONDS={refresh_jitter}")
    if flags:
        print(f"Feature flags={flags}")
    else:
        print("Feature flags=(default)")


def _check_env_vars() -> tuple[bool, str]:
    ok = True
    msgs: list[str] = []
    env_file = Path(__file__).resolve().parent / ".env"
    if not env_file.exists():
        msgs.append(".env file not found in project root")
        ok = False
    secret = os.getenv("SECRET_KEY")
    if not secret or secret.strip() in {"", "change-me"}:
        msgs.append("SECRET_KEY is missing or uses default; set a strong random value in .env")
        ok = False
    server = os.getenv("MSSQL_SERVER")
    user = os.getenv("MSSQL_USER")
    password = os.getenv("MSSQL_PASSWORD") or os.getenv("MSSQL_PASS")
    if not server:
        msgs.append("MSSQL_SERVER not set (set for DB connectivity or provide existing PARQUET_PATH)")
    # Since the data loader is tightened to SQL auth only, require MSSQL_USER + MSSQL_PASS when server is set
    if server and (not user or not password):
        msgs.append("MSSQL_USER and MSSQL_PASSWORD are required for data loading")
    return ok, "\n".join(msgs)


def _check_parquet() -> tuple[bool, str]:
    try:
        from app.services import fact_store as _fact_store  # type: ignore

        p = getattr(_fact_store, "FACT_PATH", Path(os.getenv("PARQUET_PATH", "cache/fact_dataset")))
    except Exception:
        p = Path(os.getenv("PARQUET_PATH", "cache/fact_dataset"))
    p = Path(p).resolve()
    if not p.exists():
        return False, f"Parquet not found at {p} - run with --force-refresh or set PARQUET_PATH"
    try:
        if p.is_dir():
            manifest = p / "_manifest.json"
            if not manifest.exists():
                return False, f"Manifest missing at {manifest}"
            parquet_files = list(p.rglob("*.parquet"))
            if not parquet_files:
                return False, f"No parquet files found under {p}"
            return True, f"Parquet dataset OK ({len(parquet_files)} files)"
        import pandas as pd  # type: ignore
        try:
            df = pd.read_parquet(p.as_posix(), engine="pyarrow")
        except Exception:
            df = pd.read_parquet(p.as_posix(), engine="fastparquet")
        n = int(len(df))
        if n <= 0:
            return False, f"Parquet at {p} has 0 rows"
        return True, f"Parquet OK ({n} rows)"
    except Exception as e:
        return False, f"Failed reading parquet: {e}"


def _check_auth_db() -> tuple[bool, str]:
    try:
        m = _load_auth_models_module()
        with m.get_session() as s:
            admins = int(s.query(m.User).filter(m.User.role == "admin").count())
        if admins >= 1:
            return True, f"Auth DB OK ({admins} admin users)"
        return False, "Auth DB has no admin users - create one via: python manage.py create-admin --username=admin"
    except Exception as e:
        return False, f"Auth DB check failed: {e}"


def _check_nginx() -> tuple[bool, str]:
    cfg = Path(__file__).resolve().parent / "deploy" / "nginx_wholesale.conf"
    if not cfg.exists():
        return False, f"Nginx site config missing: {cfg}"
    try:
        proc = subprocess.run(["nginx", "-t"], check=False, capture_output=True, text=True)
        if proc.returncode == 0:
            return True, "nginx -t OK"
        return False, f"nginx -t failed: {proc.stderr or proc.stdout}"
    except FileNotFoundError:
        return True, "nginx not installed - skipping test"


def _check_systemd() -> tuple[bool, str]:
    svc = Path(__file__).resolve().parent / "deploy" / "wholesale_analytics.service"
    if not svc.exists():
        return False, f"Systemd unit missing: {svc}"
    cmds = (
        "sudo cp deploy/wholesale_analytics.service /etc/systemd/system/wholesale_analytics.service\n"
        "sudo systemctl daemon-reload\n"
        "sudo systemctl enable --now wholesale_analytics\n"
        "systemctl status wholesale_analytics\n"
    )
    return True, f"systemd unit present. Enable/start with:\n{cmds}".rstrip()


def _check_disk_and_logs() -> tuple[bool, str]:
    import shutil
    here = Path(__file__).resolve().parent
    du = shutil.disk_usage(here)
    free_ok = du.free >= (1 * 1024 ** 3)
    logs = here / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    writable = False
    try:
        t = logs / ".write_test"
        t.write_text("ok", encoding="utf-8")
        t.unlink(missing_ok=True)
        writable = True
    except Exception:
        writable = False
    ok = free_ok and writable
    msg = f"Disk free={du.free/1024/1024/1024:.1f}GB ({'OK' if free_ok else 'LOW'}); logs/ writable={'yes' if writable else 'no'}"
    return ok, msg


def preflight_checks() -> int:
    print("\n=== Preflight Checks ===")
    overall_ok = True

    env_ok, env_msg = _check_env_vars()
    print(f"[env] {env_msg or 'OK'}")
    overall_ok = overall_ok and env_ok

    p_ok, p_msg = _check_parquet()
    print(f"[parquet] {p_msg}")
    overall_ok = overall_ok and p_ok

    db_ok, db_msg = _check_auth_db()
    print(f"[auth.db] {db_msg}")
    overall_ok = overall_ok and db_ok

    nx_ok, nx_msg = _check_nginx()
    print(f"[nginx] {nx_msg}")
    overall_ok = overall_ok and nx_ok

    sd_ok, sd_msg = _check_systemd()
    print(f"[systemd] {sd_msg}")
    overall_ok = overall_ok and sd_ok

    fs_ok, fs_msg = _check_disk_and_logs()
    print(f"[disk] {fs_msg}")
    overall_ok = overall_ok and fs_ok

    if overall_ok:
        print("Preflight OK.")
        return 0
    print("Preflight FAILED - see items above for remediation.")
    return 1


def _load_auth_models_module():
    """Load app/auth/models.py without importing app package (avoid flask_wtf dependency)."""
    import importlib.util
    models_path = Path(__file__).resolve().parent / "app" / "auth" / "models.py"
    spec = importlib.util.spec_from_file_location("auth_models", models_path.as_posix())
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load auth models")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


def seed_test_users(reset_passwords: bool = False) -> None:
    """Initialize auth DB and seed a few test users.

    - Creates users if missing (idempotent).
    - If reset_passwords=True, also updates existing users' passwords/roles/ids
      to the canonical test values so creds are guaranteed to work.
    """
    try:
        m = _load_auth_models_module()
        # Ensure DB and tables
        try:
            m.init_auth_db()
        except Exception:
            pass
        with m.get_session() as s:
            def _ensure_user(username: str, role: str, password: str, sales_rep_id: str | None = None, region_id: str | None = None):
                u = s.query(m.User).filter(m.User.username == username).first()
                if not u:
                    u = m.User(username=username, role=role, sales_rep_id=sales_rep_id, region_id=region_id)
                    u.set_password(password)
                    s.add(u)
                else:
                    if reset_passwords:
                        u.role = role
                        u.sales_rep_id = sales_rep_id
                        u.region_id = region_id
                        u.set_password(password)
                return u

            _ensure_user("admin", "admin", "admin")
            _ensure_user("sales1", "sales", "test", sales_rep_id="S1")
            _ensure_user("manager1", "sales_manager", "test", region_id="East")
            _ensure_user("prod1", "production", "test")
            _ensure_user("gm1", "gm", "test")
            _ensure_user("owner1", "owner", "test")
            s.commit()
        print("Seeded users (reset_passwords=" + ("true" if reset_passwords else "false") + "):")
        print("  admin/admin (admin)")
        print("  sales1/test (sales, S1)")
        print("  manager1/test (sales_manager, East)")
        print("  prod1/test (production)")
        print("  gm1/test (gm)")
        print("  owner1/test (owner)")
    except Exception as e:
        print(f"Failed to seed users: {e}")


_backfill_thread_started = False


def ensure_parquet_cache(force_refresh: bool = False) -> None:
    """Ensure the analytics Parquet cache exists; if not, hydrate it once."""
    try:
        from app.services import fact_store  # type: ignore
    except Exception:
        print("fact_store unavailable; skipping parquet ensure.")
        return

    target = Path(os.getenv("PARQUET_PATH", fact_store.FACT_PATH.as_posix())).resolve()
    if target.exists() and not force_refresh:
        print(f"Parquet cache present: {target}")
        return

    try:
        start_date = os.getenv("INITIAL_START_DATE", "2017-01-01")
        meta = fact_store.refresh_once(start_date=start_date) if target.exists() else fact_store.build_full(start_date=start_date)
        path = meta.get("path") if isinstance(meta, dict) else target.as_posix()
        print(f"Sales fact parquet ready at: {path}")
    except Exception as e:
        print(f"Data load failed: {e}")
        raise


def _start_background_backfill(parquet_path: str, start_date: Optional[str] = None) -> None:
    """Kick off a daemon thread that backfills the full analytics dataset to parquet."""
    global _backfill_thread_started

    if _backfill_thread_started:
        return
    if not _bool_env("ENABLE_BACKGROUND_BACKFILL", True):
        print("ENABLE_BACKGROUND_BACKFILL=0 → skipping historical backfill.")
        return

    start_date = start_date or os.getenv("BACKGROUND_BACKFILL_START", "2017-01-01")
    _backfill_thread_started = True

    def _task() -> None:
        label = start_date or "historical start"
        print(f"[backfill] Starting background data load from {label} …")
        try:
            from data_loader import get_dataframe, write_parquet_atomic  # type: ignore

            df = get_dataframe(start=start_date, end=None)
            out = write_parquet_atomic(df, parquet_path)
            print(f"[backfill] Completed full history load -> {out}")
        except Exception as exc:
            print(f"[backfill] Failed: {exc}")

    threading.Thread(target=_task, name="LoaderBackfill", daemon=True).start()


def _handle_fact_command(argv: list[str]) -> None:
    cmd = argv[0]
    if cmd == "build-fact":
        parser = argparse.ArgumentParser(description="Build the partitioned fact dataset")
        parser.add_argument("--start", default="2017-01-01", help="Start date (YYYY-MM-DD)")
        parser.add_argument("--end", default="today", help="End date (YYYY-MM-DD or 'today')")
        parser.add_argument("--keep-prev", action="store_true", help="Keep *_prev dataset dir for rollback")
        args = parser.parse_args(argv[1:])
        from etl.incremental_refresh import initial_build

        result = initial_build(start=args.start, end=args.end, keep_prev=bool(args.keep_prev))
        print(json.dumps(result, indent=2, default=str))
        return
    if cmd == "refresh-fact":
        parser = argparse.ArgumentParser(description="Incremental fact dataset refresh")
        parser.add_argument("--once", action="store_true", help="Run a single refresh and exit")
        parser.add_argument("--loop", action="store_true", help="Run continuous refresh loop")
        parser.add_argument("--interval", type=int, default=None, help="Refresh interval seconds")
        parser.add_argument(
            "--full-rebuild",
            action="store_true",
            help="Rebuild the full dataset from scratch and exit (deterministic; overwrites all partitions)",
        )
        parser.add_argument(
            "--start",
            default=None,
            help="Start date (YYYY-MM-DD). For --full-rebuild: rebuild window start. Otherwise: minimum incremental start.",
        )
        parser.add_argument(
            "--end",
            default="today",
            help="End date (YYYY-MM-DD or 'today') for --full-rebuild (ignored for incremental refresh).",
        )
        parser.add_argument(
            "--mode",
            choices=["full", "gap-backfill"],
            default="full",
            help="Refresh mode: full (incremental + gap backfill) or gap-backfill only",
        )
        parser.add_argument("--backfill-days", type=int, default=None, help="Gap backfill horizon in days")
        parser.add_argument("--keep-prev", action="store_true", help="Keep *_prev dataset dir for rollback")
        args = parser.parse_args(argv[1:])
        from etl.incremental_refresh import initial_build, refresh_once
        from app.services import watermark_store

        interval = args.interval or watermark_store.REFRESH_INTERVAL_SECONDS
        run_loop = args.loop
        if not args.once and not args.loop:
            run_loop = False
            args.once = True

        start_default = os.getenv("INITIAL_START_DATE", "2017-01-01")
        min_start = args.start or start_default

        if bool(args.full_rebuild):
            if bool(args.loop):
                raise SystemExit("--full-rebuild cannot be used with --loop")
            result = initial_build(
                start=min_start,
                end=args.end,
                keep_prev=bool(args.keep_prev),
            )
            print(json.dumps(result, indent=2, default=str))
            return

        def _run_once() -> Dict[str, Any]:
            return refresh_once(
                start=min_start,
                mode=args.mode,
                backfill_days=args.backfill_days,
                keep_prev=bool(args.keep_prev),
            )

        if not run_loop:
            result = _run_once()
            print(json.dumps(result, indent=2, default=str))
            return

        print(f"Starting refresh loop: interval={interval}s")
        while True:
            try:
                result = _run_once()
                status = result.get("status") if isinstance(result, dict) else "ok"
                print(f"[refresh] status={status} dataset_version={result.get('dataset_version')}")
            except Exception as exc:
                print(f"[refresh] failed: {exc}")
            jitter = random.uniform(0, max(1.0, float(interval) * 0.1))
            time.sleep(max(1, int(interval + jitter)))
        return

    raise SystemExit(f"Unknown fact command: {cmd}")


def _handle_labor_command(argv: list[str]) -> None:
    cmd = argv[0]
    if cmd == "build-labor":
        parser = argparse.ArgumentParser(description="Backfill the labor parquet dataset from Synerion")
        parser.add_argument("--start", default="2022-01-01", help="Start date (YYYY-MM-DD)")
        parser.add_argument("--end", default="today", help="End date (YYYY-MM-DD or today)")
        parser.add_argument("--keep-prev", action="store_true", help="Keep a *_prev rollback directory")
        parser.add_argument("--skip-raw", action="store_true", help="Skip raw landing writes")
        args = parser.parse_args(argv[1:])
        from app.services import labor_etl

        result = labor_etl.backfill_labor(
            start=args.start,
            end=args.end,
            keep_prev=bool(args.keep_prev),
            write_raw=not bool(args.skip_raw),
        )
        print(json.dumps(result, indent=2, default=str))
        return
    if cmd == "refresh-labor":
        parser = argparse.ArgumentParser(description="Refresh the labor parquet dataset from Synerion")
        parser.add_argument("--once", action="store_true", help="Run a single refresh and exit")
        parser.add_argument("--loop", action="store_true", help="Run a continuous refresh loop")
        parser.add_argument("--interval", type=int, default=None, help="Refresh interval seconds")
        parser.add_argument(
            "--mode",
            choices=["incremental", "recent-repair", "backfill"],
            default="incremental",
            help="Refresh mode for the labor dataset",
        )
        parser.add_argument("--start", default=None, help="Optional start date override (YYYY-MM-DD)")
        parser.add_argument("--end", default="today", help="Optional end date override (YYYY-MM-DD or today)")
        parser.add_argument("--keep-prev", action="store_true", help="Keep a *_prev rollback directory")
        parser.add_argument("--skip-raw", action="store_true", help="Skip raw landing writes")
        args = parser.parse_args(argv[1:])
        from app.services import labor_etl

        interval = args.interval or _int_env("LABOR_REFRESH_INTERVAL_SECONDS", 3600)
        run_loop = bool(args.loop)
        if not args.once and not args.loop:
            run_loop = False
            args.once = True

        def _run_once() -> Dict[str, Any]:
            if args.mode == "backfill":
                return labor_etl.backfill_labor(
                    start=args.start,
                    end=args.end,
                    keep_prev=bool(args.keep_prev),
                    write_raw=not bool(args.skip_raw),
                )
            if args.mode == "recent-repair":
                return labor_etl.repair_recent_labor(
                    end=args.end,
                    keep_prev=bool(args.keep_prev),
                    write_raw=not bool(args.skip_raw),
                )
            return labor_etl.refresh_labor(
                mode="incremental",
                start=args.start,
                end=args.end,
                keep_prev=bool(args.keep_prev),
                write_raw=not bool(args.skip_raw),
            )

        if not run_loop:
            result = _run_once()
            print(json.dumps(result, indent=2, default=str))
            return

        print(f"Starting labor refresh loop: interval={interval}s")
        while True:
            try:
                result = _run_once()
                print(
                    f"[labor-refresh] status={result.get('status')} "
                    f"dataset_version={result.get('dataset_version')} "
                    f"rows_last_pull={result.get('rows_last_pull')}"
                )
            except Exception as exc:
                print(f"[labor-refresh] failed: {exc}")
            time.sleep(max(1, int(interval)))
        return

    raise SystemExit(f"Unknown labor command: {cmd}")


def run_tool(cmd: list[str], name: str) -> int:
    print(f"\n--- {name} ---\n$ {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, check=False)
        return proc.returncode
    except FileNotFoundError:
        print(f"{name} not found. Install it to enable this check.")
        return 0


def run_checks_and_tests(quick: bool = False, include_slow: bool = False) -> bool:
    # Ruff/mypy: skip when --quick
    if not quick:
        # Ruff: run only if available
        if _importutil.find_spec("ruff") is not None:
            t0 = time.perf_counter()
            _ = run_tool([sys.executable, "-m", "ruff", "format", "--check", "."], name="ruff (format check)")
            print(f"ruff check took {time.perf_counter() - t0:.2f}s")
        else:
            print("ruff not installed; skipping format check.")

        # mypy: run only if available
        if _importutil.find_spec("mypy") is not None:
            t1 = time.perf_counter()
            _ = run_tool([sys.executable, "-m", "mypy", "--ignore-missing-imports", "-p", "app", "wsgi.py", "data_loader.py"], name="mypy")
            print(f"mypy took {time.perf_counter() - t1:.2f}s")
        else:
            print("mypy not installed; skipping type check.")
    else:
        print("--quick enabled: skipping ruff and mypy")

    # pytest: gate server start
    pytest_cmd = [sys.executable, "-m", "pytest", "-q", "--maxfail=1"]
    if include_slow:
        print("Including pytest tests marked @slow.")
    else:
        pytest_cmd.extend(["-m", "not slow"])
        print("Skipping pytest tests marked @slow; pass --with-slow-tests to include them.")
    t3 = time.perf_counter()
    code = run_tool(pytest_cmd, name="pytest")
    print(f"pytest took {time.perf_counter() - t3:.2f}s")
    if code != 0:
        print("Tests failed; not starting the dev server.")
        return False
    print("Tests passed.")
    return True


def print_urls(base: str) -> None:
    urls = [
        f"{base}/",
        f"{base}/customers/",
        f"{base}/products/",
        f"{base}/regions/",
        f"{base}/suppliers/",
        f"{base}/labor/",
        f"{base}/recommendations/",
        f"{base}/admin/",
        f"{base}/api/slice/",
    ]
    print("\nLocal server running at:")
    print(f"  {base}")
    print("\nQuick links:")
    for u in urls:
        print(f"  {u}")


def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    if argv and argv[0] in {"build-fact", "refresh-fact"}:
        load_dev_env()
        _handle_fact_command(argv)
        return
    if argv and argv[0] in {"build-labor", "refresh-labor"}:
        load_dev_env()
        _handle_labor_command(argv)
        return

    parser = argparse.ArgumentParser(description="Dev runner for Wholesale Analytics")
    # Speed/behavior toggles
    parser.add_argument("--quick", action="store_true", help="Skip linting (ruff, mypy) for faster test run")
    parser.add_argument("--fast", action="store_true", help="Fast startup: implies --quick, --skip-seed, --skip-smoke, --skip-tests")
    parser.add_argument("--skip-tests", action="store_true", help="Do not run pytest before starting the server")
    parser.add_argument("--skip-smoke", action="store_true", help="Skip running scripts/smoke.py")
    parser.add_argument("--skip-seed", action="store_true", help="Skip seeding test users")
    parser.add_argument("--pytest-args", type=str, default=None, help="Additional arguments to pass to pytest")
    parser.add_argument("--with-slow-tests", action="store_true", help="Include tests marked @pytest.mark.slow in the pytest run")
    # Data cache & env
    parser.add_argument("--force-refresh", action="store_true", help="Force rebuild of parquet cache via loader")
    parser.add_argument("--preflight", action="store_true", help="Run environment preflight checks and exit")
    # Server selection
    parser.add_argument("--server", action="store_true", help="Start Flask dev server after checks (default)")
    parser.add_argument("--gunicorn", action="store_true", help="Run Gunicorn bound to 127.0.0.1:8000 after checks")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Listen host for Flask dev server")
    parser.add_argument("--port", type=int, default=5000, help="Listen port for Flask dev server")
    parser.add_argument(
        "--seed-users",
        action="store_true",
        help="Reset seed users to known credentials (also creates if missing)"
    )
    parser.add_argument("--open", action="store_true", help="Open the app in your default browser")
    parser.add_argument("--no-reloader", action="store_true", help="Disable Flask reloader (workaround for Windows socket issues)")
    parser.add_argument("--no-debug", action="store_true", help="Disable Flask debug mode")
    args = parser.parse_args(argv)

    load_dev_env()
    env_token = (os.getenv("ENV") or os.getenv("FLASK_ENV") or "production").strip().lower()
    is_prod = env_token == "production"
    os.environ.setdefault("RUN_TESTS_ON_BOOT", "0")
    os.environ.setdefault("STARTUP_FETCH", "0")
    print_config_summary()

    # Resolve composite speed flags
    if args.fast:
        args.quick = True
        args.skip_seed = True
        args.skip_smoke = True
        args.skip_tests = True

    # Always ensure test users exist; optionally reset known passwords/roles
    # Pass --seed-users or set env SEED_RESET=1 to force reset
    if not args.skip_seed:
        seed_reset = args.seed_users or _bool_env("SEED_RESET", False)
        seed_test_users(reset_passwords=seed_reset)
    else:
        print("Skipping user seeding (--skip-seed).")
    if args.preflight:
        code = preflight_checks()
        sys.exit(code)

    # Skip heavy refresh on boot unless explicitly requested
    if args.force_refresh:
        try:
            t0 = time.perf_counter()
            ensure_parquet_cache(force_refresh=True)
            print(f"ensure_parquet_cache took {time.perf_counter() - t0:.2f}s")
        except Exception as e:
            print("Hint: If you don't need live DB loading, you can also set PARQUET_PATH to an existing parquet file.")
            print(f"Error ensuring cache: {e}")
    else:
        print("Skipping parquet refresh on startup (use --force-refresh to rebuild cache).")

    # Smoke test optional (defaults to disabled for faster boot)
    if args.skip_smoke or not _bool_env("RUN_SMOKE_ON_BOOT", False):
        print("Skipping smoke check.")
    else:
        t2 = time.perf_counter()
        smoke_code = run_tool([sys.executable, "scripts/smoke.py"], name="smoke")
        print(f"smoke took {time.perf_counter() - t2:.2f}s")
        if smoke_code != 0:
            print("Smoke check failed. Remediation: ensure PARQUET_PATH exists and is readable, or install parquet readers (pyarrow or fastparquet). If using DB load, ensure ODBC driver + pyodbc are installed and MSSQL env vars are set.")
            sys.exit(1)

    t_checks = time.perf_counter()
    if not args.skip_tests and _bool_env("RUN_TESTS_ON_BOOT", False):
        # Allow passing custom pytest args; default remains quick single-fail
        if args.pytest_args:
            # Split respecting spaces; users can quote as needed
            extra = args.pytest_args.split()
            cmd = [sys.executable, "-m", "pytest", *extra]
            has_marker = any(part == "-m" or part.startswith("-m") for part in extra)
            if args.with_slow_tests:
                print("Including pytest tests marked @slow via --with-slow-tests.")
            elif not has_marker:
                cmd.extend(["-m", "not slow"])
                print("Appending -m 'not slow' to skip tests marked @slow. Pass --with-slow-tests or provide your own -m flag to override.")
            code = run_tool(cmd, name="pytest")
            should_start = (code == 0)
        else:
            should_start = run_checks_and_tests(quick=args.quick, include_slow=args.with_slow_tests)
        print(f"pre-server checks took {time.perf_counter() - t_checks:.2f}s total")
        if not should_start:
            sys.exit(1)
    else:
        print("Skipping tests on startup.")

    # Prefer Gunicorn automatically in production unless explicitly overridden
    if is_prod and not args.server and not args.gunicorn:
        args.gunicorn = True
        print("Production environment detected; defaulting to Gunicorn. Pass --server to force Flask dev server.")

    # Start server (default: Flask dev; or Gunicorn if requested/forced)
    if args.gunicorn:
        base = "http://127.0.0.1:8000"
        print_urls(base)
        if args.open:
            # Open shortly after spawning Gunicorn
            threading.Timer(1.0, lambda: webbrowser.open(base)).start()
        print("Starting Gunicorn on 127.0.0.1:8000...")
        code = run_tool([sys.executable, "-m", "gunicorn", "-b", "127.0.0.1:8000", "-c", "gunicorn_conf.py", "wsgi:app"], name="gunicorn")
        sys.exit(code)
    else:
        # Default behavior: start Flask dev server
        if not args.server:
            print("No --server flag provided; starting Flask dev server by default.")
        # Import after environment is loaded
        try:
            from app import create_app
        except ModuleNotFoundError as e:
            if "prophet" in str(e).lower():
                print(
                    "Prophet-related modules missing. Prophet is optional; to use forecasting, "
                    "install 'prophet' (or 'cmdstanpy' backend) or disable the feature flag in .env/.env.dev."
                )
            raise

        app = create_app()
        base = f"http://{args.host}:{args.port}"
        print_urls(base)
        if args.open:
            # Delay opening slightly so the server is ready
            threading.Timer(0.8, lambda: webbrowser.open(base)).start()
        try:
            import platform
            # Default: disable reloader on Windows to avoid WinError 10038 in werkzeug/selector threads
            win = platform.system().lower().startswith("windows")
            # Env override: USE_RELOADER=1/0
            env_use_reloader = os.getenv("USE_RELOADER")
            if env_use_reloader is not None:
                use_reloader = env_use_reloader.strip() not in {"0", "false", "no", "off"}
            else:
                use_reloader = not args.no_reloader and not win

            debug = False if is_prod else (not args.no_debug)
            refresh_handle = None

            def _stop_refresh(*_args: Any) -> None:
                nonlocal refresh_handle
                if refresh_handle is None:
                    return
                try:
                    refresh_handle.stop()
                except Exception as exc:
                    print(f"Failed to stop in-process refresh: {exc}")
                finally:
                    refresh_handle = None

            def _install_signal_handler(sig: int) -> None:
                try:
                    previous = signal.getsignal(sig)
                except Exception:
                    previous = None

                def _handler(signum: int, frame: Any) -> None:
                    _stop_refresh()
                    if callable(previous):
                        previous(signum, frame)
                        return
                    if previous == signal.SIG_DFL:
                        raise KeyboardInterrupt

                try:
                    signal.signal(sig, _handler)
                except Exception:
                    pass

            try:
                from app.core import runtime as runtime_utils
                from app.services import inprocess_refresh

                debug_for_reloader = bool(debug and use_reloader)
                if runtime_utils.should_start_refresh_loop(debug=debug_for_reloader):
                    refresh_handle = inprocess_refresh.start_background_refresh(app)
                    atexit.register(_stop_refresh)
                    _install_signal_handler(getattr(signal, "SIGINT", signal.SIGINT))
                    if hasattr(signal, "SIGTERM"):
                        _install_signal_handler(signal.SIGTERM)
                    interval = _int_env("FACT_REFRESH_INTERVAL_SECONDS", 300)
                    lookback = _int_env("FACT_REFRESH_LOOKBACK_DAYS", 14)
                    print(f"In-process fact refresh: ENABLED (interval={interval}s lookback={lookback}d)")
                else:
                    print("In-process fact refresh: DISABLED (prod-safe).")
            except Exception as exc:
                print(f"In-process fact refresh setup failed: {exc}")

            app.run(host=args.host, port=args.port, debug=debug, use_reloader=use_reloader)
        except Exception as e:
            print(f"Failed to start Flask dev server: {e}")
            raise


if __name__ == "__main__":
    main()
