from __future__ import annotations

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Global limiter instance to allow decorator usage in blueprints
# Explicitly use in-memory storage to avoid noisy warnings in dev/tests.
limiter = Limiter(key_func=get_remote_address, default_limits=[], storage_uri="memory://")
