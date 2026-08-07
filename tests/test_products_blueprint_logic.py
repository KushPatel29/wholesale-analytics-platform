import pandas as pd
import pytest
import shutil
import tempfile
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import app.blueprints.products as products_bp
from app.blueprints.products import REQUIRED_PRODUCTS_COLUMNS, SalesParquetStore, ensure_products_parquet_available
from app.config import Config


@pytest.fixture(autouse=True)
def reset_store():
    products_bp._STORE_SINGLETON = None
    yield
    products_bp._STORE_SINGLETON = None


@pytest.fixture
def mock_app(monkeypatch):
    cfg = Config()
    mock = MagicMock()
    mock.config = cfg
    monkeypatch.setattr(products_bp, "current_app", mock)
    return mock


@pytest.fixture
def temp_dir():
    base = Path(tempfile.gettempdir()) / "wa_products_tests"
    base.mkdir(exist_ok=True)
    path = base / f"case-{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def test_missing_parent_directory_is_created(temp_dir, mock_app):
    target = temp_dir / "nested" / "products.parquet"
    mock_app.config.AUTO_CREATE_PRODUCTS_PARQUET = False
    status = ensure_products_parquet_available(target.as_posix(), auto_create=False)

    assert target.parent.exists()
    assert status.created_dir is True
    assert status.available is False


def test_placeholder_created_when_auto_create_enabled(temp_dir, mock_app):
    target = temp_dir / "missing" / "products.parquet"
    mock_app.config.AUTO_CREATE_PRODUCTS_PARQUET = True
    status = ensure_products_parquet_available(target.as_posix(), auto_create=True)

    assert target.exists()
    df = pd.read_parquet(target.as_posix())
    for col in REQUIRED_PRODUCTS_COLUMNS:
        assert col in df.columns
    assert status.available is True


def test_warning_when_auto_create_disabled(temp_dir, mock_app):
    target = temp_dir / "nocreate" / "products.parquet"
    mock_app.config.AUTO_CREATE_PRODUCTS_PARQUET = False
    status = ensure_products_parquet_available(target.as_posix(), auto_create=False)

    assert not target.exists()
    assert status.warning
    assert status.available is False


def test_invalid_schema_returns_empty_frame_and_warning(temp_dir, mock_app):
    target = temp_dir / "bad.parquet"
    mock_app.config.AUTO_CREATE_PRODUCTS_PARQUET = False
    pd.DataFrame({"wrong": [1, 2, 3]}).to_parquet(target.as_posix(), index=False)

    store = SalesParquetStore(target.as_posix())
    df = store.get_df()
    status = store.last_status

    for col in REQUIRED_PRODUCTS_COLUMNS:
        assert col in df.columns
    assert status.warning
    assert status.available is False
