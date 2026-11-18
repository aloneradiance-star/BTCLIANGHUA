"""Exact-timeframe, full-window readiness; a fresh stream is not 30d history."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from baqs.api import create_app
from baqs.market_data.repository import CandleStore
from baqs.market_data.schema import FinalCandle
from baqs.settings import Settings

STEP = 3_600_000


def closed(index: int) -> FinalCandle:
    return FinalCandle(
        "BTCUSDT",
        "1h",
        index * STEP,
        (index + 1) * STEP,
        Decimal(100),
        Decimal(102),
        Decimal(99),
        Decimal(101),
        Decimal(1),
        Decimal(100),
        5,
        "REST",
    )


def test_full_gapless_window_ready_but_newer_required_window_stale(db_engine: Engine) -> None:
    store = CandleStore(db_engine)
    for i in (1, 2, 3):
        store.upsert(closed(i))
    report = store.history_readiness("BTCUSDT", "1h", 4 * STEP, required_bars=3)
    assert report["status"] == "READY"
    assert report["present_bars"] == 3
    assert report["first_missing_open_ms"] is None
    stale = store.history_readiness("BTCUSDT", "1h", 5 * STEP, required_bars=3, require_latest=True)
    assert stale["status"] == "STALE"


def test_incomplete_start_and_internal_gap_are_not_ready(db_engine: Engine) -> None:
    store = CandleStore(db_engine)
    store.upsert(closed(2))
    store.upsert(closed(3))
    start_missing = store.history_readiness("BTCUSDT", "1h", 4 * STEP, required_bars=3)
    assert start_missing["status"] == "INSUFFICIENT"
    assert start_missing["first_missing_open_ms"] == STEP
    store.upsert(closed(1))
    assert store.history_readiness("BTCUSDT", "1h", 4 * STEP, required_bars=3)["status"] == "READY"


def test_internal_missing_open_time_reports_gap(db_engine: Engine) -> None:
    store = CandleStore(db_engine)
    store.upsert(closed(1))
    store.upsert(closed(3))
    report = store.history_readiness("BTCUSDT", "1h", 4 * STEP, required_bars=3)
    assert report["status"] == "GAP"
    assert report["first_missing_open_ms"] == 2 * STEP


def test_initial_empty_database_is_not_ready(db_engine: Engine) -> None:
    report = CandleStore(db_engine).history_readiness(
        "ETHUSDT",
        "15m",
        2_920 * 900_000,
        required_bars=2_920,
        require_latest=True,
    )
    assert report["status"] == "NO_DATA"


@pytest.mark.parametrize(
    "bars,end",
    [
        (0, 5 * STEP),
        (3, 2 * STEP),
        (3, 4 * STEP + 1),
        (True, 4 * STEP),
    ],
)
def test_invalid_window_parameters_rejected(db_engine: Engine, bars: int, end: int) -> None:
    with pytest.raises(ValueError):
        CandleStore(db_engine).history_readiness(
            "BTCUSDT",
            "1h",
            end,
            required_bars=bars,
        )


def test_api_shows_readiness_not_fake_market_signals(db_engine: Engine) -> None:
    settings = Settings(_env_file=None, database_url="sqlite:///:memory:")
    client = TestClient(create_app(settings=settings, engine=db_engine))
    response = client.get("/api/v1/market/history-readiness")
    assert response.status_code == 200
    assert response.json()["execution_enabled"] is False
    expected = response.json()["symbols"]["BTCUSDT"]["15m"]
    assert expected["status"] == "NO_DATA"
    assert expected["required_bars"] == 2920
    assert "orders" not in response.json()
