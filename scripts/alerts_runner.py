#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app
from app.services.notifications import run_notification_cycle


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Wholesale Analytics Analytics notifications and alerts cycle.")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate and create events but suppress outbound email delivery.")
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        if args.dry_run:
            def _noop_send(*_args, **_kwargs):
                return True

            result = run_notification_cycle(send_email_func=_noop_send)
        else:
            result = run_notification_cycle()
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
