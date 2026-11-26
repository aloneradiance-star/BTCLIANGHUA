"""End-to-end opt-in public backtest dataset preparation tests."""

import asyncio
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from baqs.api import create_app
from baqs.market_data.funding import FundingStore
from baqs.market_data.prepare_backtest_data import PublicBacktestDataPreparer
from baqs.market_data.repository import CandleStore
from baqs.market_data.schema import FinalCandle, FundingGap, FundingRateRecord
from baqs.settings import Settings

MINUTE = 60_000
ANCHOR = 120 * MINUTE


class StubReader:
    """One deterministic public source for both collectors."""

    def __init__(self, *, funding_offset_ms: int = 0) -> None:
        self.candle_requests = 0
        self.funding_requests = 0
        self.funding_offset_ms = funding_offset_ms

    async def server_time_ms(self) -> int:
        return ANCHOR

    async def fetch_range(
        self,
        symbol: str,
        timeframe: str,
        start_ms: int,
        end_ms: int,
        *,
        max_bars: int = 2000,
        page_size: int = 499,
    ) -> list[FinalCandle]:
        assert timeframe == "1m"
        assert (end_ms - start_ms) // MINUTE <= max_bars
        self.candle_requests += 1
        return [
            FinalCandle(
                symbol,
                timeframe,
                open_ms,
                open_ms + MINUTE,
                Decimal(100),
                Decimal(102),
                Decimal(99),
                Decimal(101),
                Decimal(1),
                Decimal(100),
                5,
                "REST",
            )
            for open_ms in range(start_ms, end_ms, MINUTE)
        ]

    async def fetch_funding_range(
        self,
        symbol: str,
        start_ms: int,
        end_ms: int,
        *,
        page_size: int = 1000,
        max_pages: int = 100,
        pause_seconds: float = 1.25,
    ) -> list[FundingRateRecord]:
        self.funding_requests += 1
        assert (start_ms, end_ms) == (ANCHOR - 60 * MINUTE, ANCHOR)
        return [
            FundingRateRecord(
                symbol,
                start_ms + 30 * MINUTE + self.funding_offset_ms,
                Decimal("0.001"),
                Decimal(100),
                "Regular",
            )
        ]


def test_one_command_prepares_exact_shared_window_and_is_idempotent(
    db_engine: Engine,
) -> None:
    async def run() -> None:
        reader = StubReader()
        preparer = PublicBacktestDataPreparer(
            reader,
            CandleStore(db_engine),
            FundingStore(db_engine),
            chunk_bars=20,
            funding_pause_seconds=0,
        )

        first = await preparer.run("BTCUSDT", bars=60)
        assert first.readiness == "READY"
        assert first.execution_status == "READY"
        assert first.funding_status == "READY"
        assert first.start_ms == ANCHOR - 60 * MINUTE
        assert first.end_ms == ANCHOR
        assert first.execution_bars == 60
        assert first.funding_events == 1
        assert first.created_candles == 60
        assert first.created_funding_events == 1
        assert first.reused_candle_chunks == 0

        second = await preparer.run("BTCUSDT", bars=60)
        assert second.readiness == "READY"
        assert second.created_candles == 0
        assert second.reused_candle_chunks == 3
        assert second.created_funding_events == 0
        assert second.reused_funding_events == 1
        assert reader.candle_requests == 3
        assert reader.funding_requests == 2

    asyncio.run(run())


def test_off_minute_funding_is_archived_but_window_cannot_be_replayed(db_engine: Engine) -> None:
    start_ms = ANCHOR - 60 * MINUTE
    event_ms = start_ms + 30 * MINUTE + 1
    reader = StubReader(funding_offset_ms=1)
    funding_store = FundingStore(db_engine)
    preparer = PublicBacktestDataPreparer(
        reader,
        CandleStore(db_engine),
        funding_store,
        chunk_bars=60,
        funding_pause_seconds=0,
    )
    with pytest.raises(FundingGap, match="OFF_MINUTE_EVENTS"):
        asyncio.run(preparer.run("BTCUSDT", bars=60))
    snapshot = funding_store.load_window("BTCUSDT", start_ms, ANCHOR)
    assert snapshot.expected_timestamps_ms == (event_ms,)
    assert snapshot.events[0].funding_time_ms == event_ms
    assert snapshot.events[0].mark_price == Decimal(100)

    with TestClient(
        create_app(
            settings=Settings(_env_file=None, database_url="sqlite:///:memory:"),
            engine=db_engine,
        )
    ) as client:
        response = client.get(
            "/api/v1/market/backtest-readiness",
            params={"start_ms": start_ms, "end_ms": ANCHOR},
        )
    assert response.status_code == 200
    symbol = response.json()["symbols"]["BTCUSDT"]
    assert symbol["execution_1m"]["status"] == "READY"
    assert symbol["funding"]["status"] == "OFF_MINUTE_EVENTS"
    assert symbol["status"] == "UNAVAILABLE"
