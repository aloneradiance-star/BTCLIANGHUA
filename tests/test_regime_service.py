"""End-to-end replay of public closed bars -> features -> regime -> persisted API state."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, insert, select, update
from sqlalchemy.orm import Session

from baqs.api import create_app
from baqs.models import Candle, RegimeSnapshot
from baqs.regime.rules import RegimeUnavailable
from baqs.regime.service import RegimeResearchService
from baqs.settings import Settings

FIVE_MIN = 300_000
FIFTEEN_MIN = 900_000
HOUR = 3_600_000
ANCHOR_MS = int(datetime(2026, 11, 18, 12, 0, tzinfo=UTC).timestamp() * 1000)


def _bars(symbol: str, timeframe: str, start_ms: int, step: int, count: int) -> list[dict]:
    return [
        {
            "symbol": symbol,
            "timeframe": timeframe,
            "open_time": datetime.fromtimestamp((start_ms + i * step) / 1000, tz=UTC),
            "close_time": datetime.fromtimestamp((start_ms + (i + 1) * step) / 1000, tz=UTC),
            "open": Decimal(100),
            "high": Decimal(101),
            "low": Decimal(99),
            "close": Decimal(100),
            "volume": Decimal(1),
            "quote_volume": Decimal(100),
            "trade_count": 10,
            "is_final": True,
            "source": "REST",
        }
        for i in range(count)
    ]


@pytest.fixture
def full_history(db_engine: Engine) -> Engine:
    # Includes future 5m rows relative to the first observation to test as-of filtering.
    rows = (
        _bars("BTCUSDT", "15m", ANCHOR_MS - 2920 * FIFTEEN_MIN, FIFTEEN_MIN, 2920)
        + _bars("BTCUSDT", "1h", ANCHOR_MS - 120 * HOUR, HOUR, 120)
        + _bars("BTCUSDT", "5m", ANCHOR_MS - 3 * FIVE_MIN, FIVE_MIN, 5)
    )
    with db_engine.begin() as connection:
        connection.execute(insert(Candle), rows)
    return db_engine


def test_no_sufficient_data_is_unavailable_not_transition(db_engine: Engine) -> None:
    service = RegimeResearchService(db_engine)
    with pytest.raises(RegimeUnavailable, match="5M_HISTORY"):
        service.observe("BTCUSDT", close_ms=ANCHOR_MS, clock_ms=ANCHOR_MS + 1)
    assert service.latest("BTCUSDT", clock_ms=ANCHOR_MS)["status"] == "UNAVAILABLE"


def test_end_to_end_range_switch_requires_three_5m_closes(full_history: Engine) -> None:
    service = RegimeResearchService(full_history)
    outputs = []
    for i in range(3):
        close_ms = ANCHOR_MS + i * FIVE_MIN
        outputs.append(
            service.observe(
                "BTCUSDT",
                close_ms=close_ms,
                clock_ms=close_ms + 1000,
            )
        )
    assert [str(out["regime"]) for out in outputs] == [
        "TRANSITION",
        "TRANSITION",
        "RANGE",
    ]
    assert all(out["status"] == "READY" for out in outputs)
    assert all(out["features"]["candidate"] == "RANGE" for out in outputs)
    assert all(out["features"]["signal_allowed"] is False for out in outputs)
    assert outputs[-1]["features"]["gate_state"]["confirmations"] == 0
    assert "STATE_SWITCH_CONFIRMED" in outputs[-1]["reasons"]

    # Same as-of observation cannot create a second snapshot or double advance.
    again = service.observe(
        "BTCUSDT",
        close_ms=ANCHOR_MS + 2 * FIVE_MIN,
        clock_ms=ANCHOR_MS + 2 * FIVE_MIN + 1500,
    )
    assert again["regime"] == "RANGE"
    with Session(full_history) as session:
        assert session.scalar(select(func.count()).select_from(RegimeSnapshot)) == 3


def test_older_replay_after_newer_snapshot_rejected(full_history: Engine) -> None:
    service = RegimeResearchService(full_history)
    service.observe(
        "BTCUSDT",
        close_ms=ANCHOR_MS + FIVE_MIN,
        clock_ms=ANCHOR_MS + FIVE_MIN + 1,
    )
    with pytest.raises(RegimeUnavailable, match="OUT_OF_ORDER"):
        service.observe("BTCUSDT", close_ms=ANCHOR_MS, clock_ms=ANCHOR_MS + 1)


def test_injected_future_15m_candle_never_enters_past_asof(full_history: Engine) -> None:
    future_end = ANCHOR_MS + FIFTEEN_MIN
    with full_history.begin() as connection:
        connection.execute(insert(Candle), _bars("BTCUSDT", "15m", ANCHOR_MS, FIFTEEN_MIN, 1))
    service = RegimeResearchService(full_history)
    with Session(full_history) as session:
        loaded = service._load(session, "BTCUSDT", "15m", ANCHOR_MS, 2920)
    assert loaded[-1].close_ms == ANCHOR_MS
    assert loaded[-1].close_ms < future_end
    assert (
        service.observe("BTCUSDT", close_ms=ANCHOR_MS, clock_ms=ANCHOR_MS + 1)["features"][
            "source_15m_close_ms"
        ]
        == ANCHOR_MS
    )


def test_missing_historical_candle_fails_closed(full_history: Engine) -> None:
    from sqlalchemy import delete

    with full_history.begin() as connection:
        connection.execute(
            delete(Candle).where(
                Candle.symbol == "BTCUSDT",
                Candle.timeframe == "15m",
                Candle.open_time
                == datetime.fromtimestamp((ANCHOR_MS - 100 * FIFTEEN_MIN) / 1000, tz=UTC),
            )
        )
    with pytest.raises(RegimeUnavailable, match="15M_HISTORY"):
        RegimeResearchService(full_history).observe(
            "BTCUSDT", close_ms=ANCHOR_MS, clock_ms=ANCHOR_MS + 1
        )


def test_regime_api_is_read_only_and_stale_snapshots_unavailable(full_history: Engine) -> None:
    service = RegimeResearchService(full_history)
    service.observe("BTCUSDT", close_ms=ANCHOR_MS, clock_ms=ANCHOR_MS + 1)
    app = create_app(
        settings=Settings(_env_file=None, database_url="sqlite:///:memory:"),
        engine=full_history,
    )
    with TestClient(app) as client:
        result = client.get("/api/v1/market/regimes")
        assert result.status_code == 200
        assert result.json()["execution_enabled"] is False
        assert result.json()["symbols"]["BTCUSDT"]["status"] == "UNAVAILABLE"
        assert result.json()["symbols"]["ETHUSDT"]["status"] == "UNAVAILABLE"
        assert all(
            list(operations) == ["get"]
            for operations in client.get("/openapi.json").json()["paths"].values()
        )



def test_missing_quote_volume_metadata_fails_closed(full_history: Engine) -> None:
    target_open = datetime.fromtimestamp(
        (ANCHOR_MS - 100 * FIFTEEN_MIN) / 1000,
        tz=UTC,
    )
    with full_history.begin() as connection:
        connection.execute(
            update(Candle)
            .where(
                Candle.symbol == "BTCUSDT",
                Candle.timeframe == "15m",
                Candle.open_time == target_open,
            )
            .values(quote_volume=None)
        )
    with pytest.raises(RegimeUnavailable, match="INCOMPLETE_CANDLE_METADATA"):
        RegimeResearchService(full_history).observe(
            "BTCUSDT",
            close_ms=ANCHOR_MS,
            clock_ms=ANCHOR_MS + 1,
        )


def test_untrusted_candle_source_fails_closed(full_history: Engine) -> None:
    target_open = datetime.fromtimestamp(
        (ANCHOR_MS - 100 * FIFTEEN_MIN) / 1000,
        tz=UTC,
    )
    with full_history.begin() as connection:
        connection.execute(
            update(Candle)
            .where(
                Candle.symbol == "BTCUSDT",
                Candle.timeframe == "15m",
                Candle.open_time == target_open,
            )
            .values(source="FIXTURE")
        )
    with pytest.raises(RegimeUnavailable, match="UNTRUSTED_CANDLE_SOURCE"):
        RegimeResearchService(full_history).observe(
            "BTCUSDT",
            close_ms=ANCHOR_MS,
            clock_ms=ANCHOR_MS + 1,
        )


def test_corrupt_gate_state_fails_closed_on_latest_and_duplicate(full_history: Engine) -> None:
    service = RegimeResearchService(full_history)
    service.observe("BTCUSDT", close_ms=ANCHOR_MS, clock_ms=ANCHOR_MS + 1)
    with Session(full_history) as session:
        snapshot = session.scalar(select(RegimeSnapshot))
        assert snapshot is not None
        features = dict(snapshot.features)
        gate_state = dict(features["gate_state"])
        gate_state["last_processed_ms"] = 1
        features["gate_state"] = gate_state
        snapshot.features = features
        session.commit()

    latest = service.latest("BTCUSDT", clock_ms=ANCHOR_MS + 1000)
    assert latest["status"] == "UNAVAILABLE"
    assert latest["reason"] == "CORRUPT_PERSISTED_GATE_STATE"
    with pytest.raises(RegimeUnavailable, match="CORRUPT_PERSISTED_GATE_STATE"):
        service.observe("BTCUSDT", close_ms=ANCHOR_MS, clock_ms=ANCHOR_MS + 1000)
