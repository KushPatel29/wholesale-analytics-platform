import sys
from pathlib import Path
import os
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import functools
import types
import pytest
import pandas as pd
import numpy as np
import tempfile
import uuid
import shutil
import re
from _pytest.tmpdir import TempPathFactory

import data_loader


def _patch_numpy_no_value_compat() -> None:
    """
    Some local test runs can end up with mixed NumPy sentinel instances across
    reload boundaries, which breaks pandas index take/drop internals with:
    TypeError: ... '_NoValueType'
    Patch is test-only and activates only when the issue is detected.
    """
    try:
        methods = np._core._methods  # type: ignore[attr-defined]
        umath = np._core._multiarray_umath  # type: ignore[attr-defined]
    except Exception:
        return

    if getattr(methods, "_wa_no_value_patch", False):
        return

    def _coerce_initial(initial):
        if type(initial).__name__ == "_NoValueType":
            return None
        return initial

    def _amax_compat(a, axis=None, out=None, keepdims=False, initial=np._NoValue, where=True):
        coerced = _coerce_initial(initial)
        if coerced is None:
            return umath.maximum.reduce(a, axis=axis, dtype=None, out=out, keepdims=keepdims, where=where)
        return umath.maximum.reduce(a, axis=axis, dtype=None, out=out, keepdims=keepdims, initial=coerced, where=where)

    def _amin_compat(a, axis=None, out=None, keepdims=False, initial=np._NoValue, where=True):
        coerced = _coerce_initial(initial)
        if coerced is None:
            return umath.minimum.reduce(a, axis=axis, dtype=None, out=out, keepdims=keepdims, where=where)
        return umath.minimum.reduce(a, axis=axis, dtype=None, out=out, keepdims=keepdims, initial=coerced, where=where)

    def _sum_compat(a, axis=None, dtype=None, out=None, keepdims=False, initial=np._NoValue, where=True):
        coerced = _coerce_initial(initial)
        if coerced is None:
            return umath.add.reduce(a, axis=axis, dtype=dtype, out=out, keepdims=keepdims, where=where)
        return umath.add.reduce(a, axis=axis, dtype=dtype, out=out, keepdims=keepdims, initial=coerced, where=where)

    def _prod_compat(a, axis=None, dtype=None, out=None, keepdims=False, initial=np._NoValue, where=True):
        coerced = _coerce_initial(initial)
        if coerced is None:
            return umath.multiply.reduce(a, axis=axis, dtype=dtype, out=out, keepdims=keepdims, where=where)
        return umath.multiply.reduce(a, axis=axis, dtype=dtype, out=out, keepdims=keepdims, initial=coerced, where=where)

    methods._amax = _amax_compat
    methods._amin = _amin_compat
    methods._sum = _sum_compat
    methods._prod = _prod_compat
    methods._wa_no_value_patch = True


_patch_numpy_no_value_compat()

_TMP_ROOT = Path(tempfile.gettempdir()) / "wa_pytest"
_TMP_BASE = _TMP_ROOT / f"session_{uuid.uuid4().hex}"
_TMP_BASE.mkdir(parents=True, exist_ok=True)
# Stable pytest base temp to avoid permission issues with pytest-of-* on Windows ACLs
_BASE_TEMP_OVERRIDE = _TMP_BASE / "pytest_basetemp"
_BASE_TEMP_OVERRIDE.mkdir(parents=True, exist_ok=True)
for _env in ("TMPDIR", "TEMP", "TMP"):
    os.environ[_env] = str(_TMP_BASE)
tempfile.tempdir = str(_TMP_BASE)
# Remove stale pytest temp roots that may have restricted permissions
for _d in _TMP_ROOT.glob("pytest-of-*"):
    try:
        if _d.is_dir():
            shutil.rmtree(_d, ignore_errors=True)
    except Exception:
        pass


def _safe_getbasetemp(self):
    if self._basetemp is not None:
        return self._basetemp
    base = Path(self._given_basetemp) if self._given_basetemp is not None else Path(
        tempfile.mkdtemp(prefix="wa_pytest_base_", dir=_TMP_ROOT)
    )
    base.mkdir(parents=True, exist_ok=True)
    self._basetemp = base
    return base


def _safe_mktemp(self, basename: str, numbered: bool = True):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", basename)
    base = self.getbasetemp()
    if numbered:
        idx = 0
        while True:
            candidate = base / f"{safe}-{idx}"
            try:
                candidate.mkdir(parents=True, exist_ok=False)
                break
            except FileExistsError:
                idx += 1
        return candidate
    candidate = base / safe
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


TempPathFactory.getbasetemp = _safe_getbasetemp
TempPathFactory.mktemp = _safe_mktemp

# Isolate auth DB per test run so migrations and seeds don't clash with local data
_AUTH_DB = _TMP_BASE / "auth_test.db"
os.environ["AUTH_DB_PATH"] = str(_AUTH_DB)
try:
    _AUTH_DB.unlink()
except FileNotFoundError:
    pass

os.environ.setdefault("COVERAGE_CORE", "ctrace")
os.environ["MSSQL_SERVER"] = ""
os.environ.setdefault("MSSQL_DB", "")
os.environ.setdefault("MSSQL_USER", "")
os.environ.setdefault("MSSQL_PASSWORD", "")


def pytest_configure(config):
    """
    Force pytest tmp path factory to use a clean, writable directory we control.
    This avoids Windows ACL issues with default pytest-of-* temp roots.
    """
    config.option.basetemp = str(_BASE_TEMP_OVERRIDE)
    try:
        shutil.rmtree(_BASE_TEMP_OVERRIDE, ignore_errors=True)
    except Exception:
        pass
    _BASE_TEMP_OVERRIDE.mkdir(parents=True, exist_ok=True)
    factory = TempPathFactory.from_config(config, _ispytest=True)
    factory._given_basetemp = _BASE_TEMP_OVERRIDE
    factory._basetemp = _BASE_TEMP_OVERRIDE
    config._tmp_path_factory = factory


class _DummyCache:
    def __init__(self, config=None):
        self.config = config or {}
        self._store = {}

    def init_app(self, app):
        return self

    def cached(self, timeout=None, key_prefix=None):
        def decorator(func):
            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                return func(*args, **kwargs)
            return wrapper
        return decorator

    def memoize(self, timeout=None):
        def decorator(func):
            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                return func(*args, **kwargs)
            return wrapper
        return decorator

    def get(self, key):
        return self._store.get(key)

    def set(self, key, value, timeout=None):
        self._store[key] = value

    def delete(self, key):
        self._store.pop(key, None)

try:
    import flask_caching  # type: ignore  # noqa: F401
except ModuleNotFoundError:
    module = types.ModuleType("flask_caching")
    module.Cache = _DummyCache
    sys.modules["flask_caching"] = module


os.environ.setdefault("MSSQL_SERVER", "")


# Provide a lightweight Flask test app/client for API tests
@pytest.fixture(scope="session")
def app():
    os.environ.setdefault("FLASK_ENV", "development")
    os.environ.setdefault("WTF_CSRF_ENABLED", "false")
    os.environ.setdefault("WA_FAST_PWHASH", "1")
    from app import create_app
    _app = create_app()
    # Disable login in API-focused tests unless overridden
    _app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test", LOGIN_DISABLED=True)
    return _app


@pytest.fixture()
def client(app):
    with app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def _restore_shared_app_config(request):
    """Prevent tests from leaking Flask feature flags into later tests.

    The ``app`` fixture is session scoped for speed, while many tests mutate
    ``app.config`` directly.  Without restoring the mapping, enabling a v2
    page in one module silently changes the routes and templates exercised by
    unrelated legacy tests later in the same full-suite run.
    """
    shared_app = None
    original = None
    if "app" in request.fixturenames:
        shared_app = request.getfixturevalue("app")
        original = dict(shared_app.config)
    yield
    if shared_app is not None and original is not None:
        for key in set(shared_app.config).difference(original):
            shared_app.config.pop(key, None)
        shared_app.config.update(original)

@pytest.fixture
def fake_user():
    return SimpleNamespace(username="sales.alex", role="sales", sales_rep_id="GUID-1", region_id=None)

@pytest.fixture
def app_client(monkeypatch):
    from app import create_app

    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test", LOGIN_DISABLED=True)
    with app.test_client() as client:
        yield client

@pytest.fixture(autouse=True)
def _patch_loader_get_dataframe_for_user(monkeypatch, request):
    if request.node.get_closest_marker("requires_real_loader"):
        return

    def _fake_loader(*args, **kwargs):
        return pd.DataFrame()

    monkeypatch.setattr(data_loader, "get_dataframe_for_user", _fake_loader)


_COLLECT_IGNORE = {"temp_mode_test", "temp_mode_test2"}


def pytest_ignore_collect(collection_path, config):  # type: ignore[override]
    """Skip recursing into temp dirs created during local debugging."""
    try:
        name = getattr(collection_path, "name", None) or getattr(collection_path, "basename", None)
    except Exception:
        name = None
    if name and name in _COLLECT_IGNORE:
        return True
