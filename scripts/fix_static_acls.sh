#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${1:-/opt/wholesale_analytics}"
NGINX_USER="${NGINX_USER:-www-data}"
STATIC_ROOT="${APP_ROOT}/app/static"
APP_DIR="${APP_ROOT}/app"

if ! command -v setfacl >/dev/null 2>&1; then
  echo "setfacl is required but not installed" >&2
  exit 1
fi

if [[ ! -d "${STATIC_ROOT}" ]]; then
  echo "Static root not found: ${STATIC_ROOT}" >&2
  exit 1
fi

if [[ ! -d "${APP_DIR}" ]]; then
  echo "App directory not found: ${APP_DIR}" >&2
  exit 1
fi

# Keep the app tree private while letting nginx traverse to static assets.
setfacl -m "u:${NGINX_USER}:--x" "${APP_ROOT}"
setfacl -m "u:${NGINX_USER}:--x" "${APP_DIR}"

# Existing and future static assets must be readable by nginx.
find "${STATIC_ROOT}" -type d -exec setfacl -m "u:${NGINX_USER}:r-x" {} +
find "${STATIC_ROOT}" -type d -exec setfacl -d -m "u:${NGINX_USER}:r-x" {} +
find "${STATIC_ROOT}" -type f -exec setfacl -m "u:${NGINX_USER}:r--" {} +

echo "Applied static ACLs for ${NGINX_USER} under ${STATIC_ROOT}"
