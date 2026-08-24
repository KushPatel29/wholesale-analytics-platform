"""Fail when dbt marts and the application metric catalogue drift apart."""

from __future__ import annotations

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MARTS = ROOT / "models" / "marts"
SCHEMA = MARTS / "schema.yml"


def main() -> int:
    document = yaml.safe_load(SCHEMA.read_text(encoding="utf-8")) or {}
    models = document.get("models") or []
    declared_models = {str(model.get("name") or "").strip() for model in models}
    sql_models = {path.stem for path in MARTS.glob("*.sql")}
    if declared_models != sql_models:
        raise SystemExit(
            f"dbt schema/model drift: declared-only={sorted(declared_models - sql_models)}, "
            f"sql-only={sorted(sql_models - declared_models)}"
        )

    rows = []
    for model in models:
        rows.extend((((model.get("config") or {}).get("meta") or {}).get("metric_catalogue") or []))
    keys = [str(row.get("key") or "").strip() for row in rows]
    if not keys or len(keys) != len(set(keys)):
        raise SystemExit("metric catalogue keys must be present and unique")

    unknown_marts = sorted({str(row.get("model_name") or "") for row in rows} - sql_models)
    if unknown_marts:
        raise SystemExit(f"catalogue rows reference missing marts: {unknown_marts}")

    for row in rows:
        if row.get("certification") == "Withheld" and not str(row.get("missing_source") or "").strip():
            raise SystemExit(f"withheld metric lacks source explanation: {row.get('key')}")

    for path in ROOT.joinpath("models", "staging").glob("*.sql"):
        if not re.search(r"\bsource\s*\(", path.read_text(encoding="utf-8")):
            raise SystemExit(f"staging model does not use source(): {path.relative_to(ROOT)}")
    for path in MARTS.glob("*.sql"):
        if not re.search(r"\bref\s*\(", path.read_text(encoding="utf-8")):
            raise SystemExit(f"mart does not use ref(): {path.relative_to(ROOT)}")

    print(f"dbt semantic layer OK: {len(sql_models)} marts, {len(rows)} governed definitions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
