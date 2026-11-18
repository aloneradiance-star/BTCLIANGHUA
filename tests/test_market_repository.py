"""DB idempotency, stale-data health and gap detection tests."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from baqs.market_data.repository import CandleStore
from baqs.market_data.schema import CandleConflict, FinalCandle
from baqs.models import Candle

STEP = 60_000


def bar(index: int, price: str = "100") -> FinalCandle:
    return FinalCandle(
        symbol="BTCUSDT", timeframe="1m",
        open_ms=index * STEP, close_ms=(index + 1) * STEP,
        open=Decimal(price), high=Decimal("103"),
        low=Decimal("99"), close=Decimal("101"),
        volume=Decimal("5"), quote_volume=Decimal("500"),
        trade_count=10, source="WS",
    )


def test_duplicate_is_idempotent_and_conflict_is_not_silently_revised(db_engine: Engine) -> None:
    store = CandleStore(db_engine)
    assert store.upsert(bar(1)) == "CREATED"
    assert store.upsert(bar(1)) == "DUPLICATE"
    with pytest.raises(CandleConflict):
        store.upsert(bar(1, "102"))
    assert store.latest_open_ms("BTCUSDT", "1m") == STEP


def test_gaps_and_health_are_visible(db_engine: Engine) -> None:
    store = CandleStore(db_engine)
    for i in (1, 2, 4):
        store.upsert(bar(i))
    assert store.gap_ranges("BTCUSDT", "1m") == [(3 * STEP, 4 * STEP)]
    health = store.health("BTCUSDT", "1m", 5 * STEP)
    assert health["status"] == "GAP"
    assert health["gap_count"] == 1
    store.upsert(bar(3))
    assert store.health("BTCUSDT", "1m", 5 * STEP)["status"] == "READY"
    assert store.health("BTCUSDT", "1m", 9 * STEP)["status"] == "STALE"


def test_no_data_is_unavailable(db_engine: Engine) -> None:
    result = CandleStore(db_engine).health("ETHUSDT", "15m", 3_000_000)
    assert result["status"] == "NO_DATA"


def test_stored_provisional_candle_is_promoted(db_engine: Engine) -> None:
    store = CandleStore(db_engine)
    first = bar(1)
    with Session(db_engine) as session:
        session.add(Candle(
            symbol=first.symbol, timeframe=first.timeframe,
            open_time=first.open_time, close_time=first.close_time,
            open=first.open, high=first.high, low=first.low, close=first.close,
            volume=first.volume, quote_volume=first.quote_volume,
            trade_count=first.trade_count, is_final=False, source="OLD_TEST",
        ))
        session.commit()
    assert store.upsert(first) == "PROMOTED"
    assert store.upsert(first) == "DUPLICATE"
