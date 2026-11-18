"""ORM integrity and precision tests."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from baqs.models import Candle, RegimeSnapshot


def candle(**overrides: object) -> Candle:
    params: dict[str, object] = {
        "symbol": "BTCUSDT",
        "timeframe": "5m",
        "open_time": datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc),
        "close_time": datetime(2026, 11, 17, 12, 5, tzinfo=timezone.utc),
        "open": Decimal("95000.123456789012"),
        "high": Decimal("95005"),
        "low": Decimal("94990"),
        "close": Decimal("95001"),
        "volume": Decimal("0.012345678901"),
        "source": "TEST_FIXTURE",
        "is_final": True,
    }
    params.update(overrides)
    return Candle(**params)  # type: ignore[arg-type]


def test_candle_precision_and_composite_key(db_engine: Engine) -> None:
    with Session(db_engine) as session:
        session.add(candle())
        session.commit()

    with Session(db_engine) as session:
        stored = session.get(
            Candle, ("BTCUSDT", "5m", datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc))
        )
        assert stored is not None
        assert stored.open == Decimal("95000.123456789012")
        assert stored.volume == Decimal("0.012345678901")


def test_duplicate_candle_key_is_rejected(db_engine: Engine) -> None:
    with Session(db_engine) as session:
        session.add(candle())
        session.commit()
    with pytest.raises(IntegrityError), Session(db_engine) as session:
        session.add(candle(close=Decimal("99999")))
        session.commit()


def test_invalid_ohlc_rejected(db_engine: Engine) -> None:
    with pytest.raises(IntegrityError), Session(db_engine) as session:
        session.add(candle(high=Decimal("1")))
        session.commit()


def test_regime_snapshot_requires_existing_candle(db_engine: Engine) -> None:
    with pytest.raises(IntegrityError), Session(db_engine) as session:
        session.add(
            RegimeSnapshot(
                symbol="BTCUSDT",
                timeframe="15m",
                candle_open_time=datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc),
                candle_close_time=datetime(2026, 11, 17, 12, 15, tzinfo=timezone.utc),
                regime="TRANSITION",
                features={},
                reasons=["WARMUP"],
                classifier_version="rules-0.1",
            )
        )
        session.commit()


def test_regime_saved_with_existing_candle(db_engine: Engine) -> None:
    bar = candle(timeframe="15m", close_time=datetime(2026, 11, 17, 12, 15, tzinfo=timezone.utc))
    with Session(db_engine) as session:
        session.add(bar)
        session.flush()
        session.add(
            RegimeSnapshot(
                symbol=bar.symbol,
                timeframe=bar.timeframe,
                candle_open_time=bar.open_time,
                candle_close_time=bar.close_time,
                regime="TRANSITION",
                features={"atr": "10.01"},
                reasons=["NO_VALID_TREND"],
                classifier_version="rules-0.1",
            )
        )
        session.commit()
