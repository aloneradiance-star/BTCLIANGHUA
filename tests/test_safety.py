"""Verify the foundational app cannot perform private account or live trading calls."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from baqs.api import create_app
from baqs.settings import Settings


def test_trading_mode_cannot_be_enabled() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, live_enabled=True)


def test_routes_are_read_only() -> None:
    # OpenAPI generation is local and requires no DB connection.
    app = create_app(settings=Settings(_env_file=None, database_url="sqlite:///:memory:"))
    schema = TestClient(app).get("/openapi.json").json()
    assert all(set(methods).issubset({"get"}) for methods in schema["paths"].values())


def test_no_exchange_secrets_or_execution_module() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "baqs"
    assert not (root / "execution").exists()
    assert not (root / "exchange" / "binance_private.py").exists()
    source = "\n".join(p.read_text() for p in root.rglob("*.py"))
    assert "fapi/v1/order" not in source
    assert "fapi/v1/algoOrder" not in source
