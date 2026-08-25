"""Which build is actually running.

The live demo served a stale image for days while `/healthz` answered `{"status":
"ok"}`. It was telling the truth - that process was alive - but the question
anyone actually had was *which commit is it running*, and nothing on the service
could answer it. Every `/work` route 404'd in production while returning 200
locally, and the only way to notice was to diff a CSS class name out of the
login page.

So the fingerprint is deliberately boring and deliberately public: the commit,
the release, and nothing else. Render injects `RENDER_GIT_COMMIT` at runtime;
`GIT_SHA` is baked at build time so an image built anywhere else can still name
itself. Both fall back to `unknown` rather than to a lie - a health check that
cannot identify its build must say so, because "unknown" is what lets the
keep-warm workflow fail loudly instead of reporting green on the wrong image.

Nothing here reads a secret. The fields are exactly what a public status page
would show, which is why this endpoint can stay unauthenticated.
"""

from __future__ import annotations

import os
from typing import Any

UNKNOWN = "unknown"

# Render sets these on every service. Ordered most-specific first: the runtime
# value is authoritative because a rebuilt image keeps its baked ARG but gets a
# fresh RENDER_GIT_COMMIT.
_SHA_VARS = ("RENDER_GIT_COMMIT", "GIT_SHA", "SOURCE_COMMIT", "HEROKU_SLUG_COMMIT")
_RELEASE_VARS = ("RENDER_RELEASE_ID", "RENDER_INSTANCE_ID", "RELEASE_ID")
_BRANCH_VARS = ("RENDER_GIT_BRANCH", "GIT_BRANCH")
_SERVICE_VARS = ("RENDER_SERVICE_NAME", "SERVICE_NAME")


def _first_env(names: tuple[str, ...]) -> str | None:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


def git_sha() -> str:
    """Full commit SHA of the running build, or `unknown`."""
    return _first_env(_SHA_VARS) or UNKNOWN


def short_sha(value: str | None = None) -> str:
    sha = value if value is not None else git_sha()
    return sha[:7] if sha and sha != UNKNOWN else UNKNOWN


def release_info() -> dict[str, Any]:
    """Safe, public build identity. Never includes configuration or secrets."""
    sha = git_sha()
    return {
        "git_sha": sha,
        "git_sha_short": short_sha(sha),
        "git_branch": _first_env(_BRANCH_VARS) or UNKNOWN,
        "release_id": _first_env(_RELEASE_VARS) or UNKNOWN,
        "service": _first_env(_SERVICE_VARS) or UNKNOWN,
        # `identified` is the field automation should gate on. A build that
        # cannot name its commit is not a build you can call green.
        "identified": sha != UNKNOWN,
    }
