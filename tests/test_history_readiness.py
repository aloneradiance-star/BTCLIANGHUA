"""Exact-timeframe, full-window readiness; a fresh stream is not 30d history."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, insert

from datetime import UTC, datetime

from baqs.models import Candle

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


def test_full_2920_bar_15m_percentile_reference_is_contiguous(db_engine: Engine) -> None:
    # Bulk insert through SQL Core, not the live collector, to stress-test readiness.
    step = 900_000
    n = 2920
    end_ms = (n + 80) * step
    first_ms = end_ms - n * step
    rows = [
        {
            "symbol": "BTCUSDT", "timeframe": "15m",
            "open_time": datetime.fromtimestamp((first_ms + i * step) / 1000, tz=UTC),
            "close_time": datetime.fromtimestamp((first_ms + (i + 1) * step) / 1000, tz=UTC),
            "open": Decimal(100), "high": Decimal(102), "low": Decimal(99),
            "close": Decimal(101), "volume": Decimal(1),
            "quote_volume": Decimal(100), "trade_count": 5,
            "is_final": True, "source": "TEST_FIXTURE",
        }
        for i in range(n)
    ]
    with db_engine.begin() as connection:
        connection.execute(insert(Candle), rows)
    store = CandleStore(db_engine)
    report = store.history_readiness(
        "BTCUSDT", "15m", end_ms, required_bars=n, require_latest=True
    )
    assert report["status"] == "READY"
    assert report["present_bars"] == n

    # Even one missing historical row breaks the readiness certification.
    with db_engine.begin() as connection:
        connection.execute(
            delete(Candle).where(
                Candle.symbol == "BTCUSDT",
                Candle.timeframe == "15m",
                Candle.open_time == datetime.fromtimestamp(
                    (first_ms + 100 * step) / 1000, tz=UTC
                ),
            )
        )
    gap = store.history_readiness(
        "BTCUSDT", "15m", end_ms, required_bars=n, require_latest=True
    )
    assert gap["status"] == "GAP"
    assert gap["first_missing_open_ms"] == first_ms + 100 * step
