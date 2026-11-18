"""Integration test against PostgreSQL NUMERIC; never a trading-account test."""

import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, delete, insert, select

from baqs.models import Candle


@pytest.mark.skipif(
    not os.environ.get("BAQS_TEST_POSTGRES_URL"),
    reason="PostgreSQL integration URL not provided",
)
def test_postgresql_numeric_precision_and_utc() -> None:
    engine = create_engine(os.environ["BAQS_TEST_POSTGRES_URL"])
    open_time = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
    price = Decimal("95000.123456789012")
    try:
        with engine.begin() as conn:
            conn.execute(
                insert(Candle).values(
                    symbol="BTCUSDT",
                    timeframe="5m",
                    open_time=open_time,
                    close_time=datetime(2026, 11, 17, 12, 5, tzinfo=UTC),
                    open=price,
                    high=Decimal("95005"),
                    low=Decimal("94990"),
                    close=Decimal("95001"),
                    volume=Decimal("0.012345678901"),
                    is_final=True,
                    source="POSTGRES_CI",
                )
            )
            result = conn.execute(
                select(Candle.open, Candle.open_time).where(
                    Candle.symbol == "BTCUSDT",
                    Candle.timeframe == "5m",
                    Candle.open_time == open_time,
                )
            ).one()
            assert result[0] == price
            assert result[1].utcoffset().total_seconds() == 0
            conn.execute(
                delete(Candle).where(
                    Candle.symbol == "BTCUSDT",
                    Candle.timeframe == "5m",
                    Candle.open_time == open_time,
                )
            )
    finally:
        engine.dispose()
