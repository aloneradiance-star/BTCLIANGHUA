"""Integration test against PostgreSQL NUMERIC; never a trading-account test."""

import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, delete, insert, select

from baqs.models import Candle, FundingRate


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
            funding_time = datetime(2026, 11, 17, 16, 0, tzinfo=UTC)
            funding_rate = Decimal("0.000123456789")
            mark_price = Decimal("95000.123456789012")
            conn.execute(
                insert(FundingRate).values(
                    symbol="BTCUSDT",
                    funding_time=funding_time,
                    rate_type="Regular",
                    funding_rate=funding_rate,
                    mark_price=mark_price,
                    source="POSTGRES_CI",
                )
            )
            funding = conn.execute(
                select(FundingRate.funding_rate, FundingRate.mark_price).where(
                    FundingRate.symbol == "BTCUSDT",
                    FundingRate.funding_time == funding_time,
                    FundingRate.rate_type == "Regular",
                )
            ).one()
            assert funding[0] == funding_rate
            assert funding[1] == mark_price
            conn.execute(
                delete(FundingRate).where(
                    FundingRate.symbol == "BTCUSDT",
                    FundingRate.funding_time == funding_time,
                    FundingRate.rate_type == "Regular",
                )
            )
            conn.execute(
                delete(Candle).where(
                    Candle.symbol == "BTCUSDT",
                    Candle.timeframe == "5m",
                    Candle.open_time == open_time,
                )
            )
    finally:
        engine.dispose()
