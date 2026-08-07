#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${1:-${BASE_URL:-http://127.0.0.1:5000}}"

paths=(
  "/static/css/theme.css"
  "/static/js/salesreps.js"
  "/static/vendor/chartjs/chart.umd.min.js"
  "/static/img/wa-logo-badge.png"
  "/static/images/favicon_symbol.png"
)

for path in "${paths[@]}"; do
  status="$(curl -sS -o /dev/null -w "%{http_code}" "${BASE_URL}${path}")"
  echo "${status} ${path}"
  if [[ "${status}" != "200" ]]; then
    echo "FAIL: ${BASE_URL}${path} returned HTTP ${status}" >&2
    exit 1
  fi
done

echo "OK: static asset checks passed for ${BASE_URL}"
