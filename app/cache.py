from __future__ import annotations

import os

from flask_caching import Cache

_default_timeout = int(os.getenv("CACHE_DEFAULT_TIMEOUT", "300"))
_redis_url = os.getenv("REDIS_URL")
_cache_type = os.getenv("CACHE_TYPE") or ("RedisCache" if _redis_url else "SimpleCache")

_config = {
    "CACHE_TYPE": _cache_type,
    "CACHE_DEFAULT_TIMEOUT": _default_timeout,
}

if _redis_url:
    _config["CACHE_REDIS_URL"] = _redis_url

# Shared cache instance for API memoization and option lookups.
cache = Cache(config=_config)
