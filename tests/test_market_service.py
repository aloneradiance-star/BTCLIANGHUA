"""Reconnection, gap repair, out-of-order, and bounded feed tests."""

import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import Engine

from baqs.market_data.repository import CandleStore
from baqs.market_data.schema import CandleGap, FinalCandle
from baqs.market_data.service import PublicMarketDataService

STEP = 60_000


def bar(index: int, *, source: str = "REST") -> FinalCandle:
    return FinalCandle(
        "BTCUSDT", "1m", index * STEP, (index + 1) * STEP,
        Decimal("100"), Decimal("103"), Decimal("99"), Decimal("101"),
        Decimal("5"), Decimal("500"), 10, source,
    )


class StubREST:
    def __init__(self, latest_ms: int, missing: set[int] | None = None) -> None:
        self.latest_ms = latest_ms
        self.missing = missing or set()
        self.calls: list[tuple[int, int]] = []

    async def server_time_ms(self) -> int:
        return self.latest_ms

    async def fetch_range(
        self, symbol: str, timeframe: str, start_ms: int, end_ms: int,
        *, max_bars: int = 2000, page_size: int = 499,
    ) -> list[FinalCandle]:
        assert symbol == "BTCUSDT" and timeframe == "1m"
        self.calls.append((start_ms, end_ms))
        result = [bar(i) for i in range(start_ms // STEP, end_ms // STEP)
                  if i not in self.missing]
        if len(result) != (end_ms - start_ms) // STEP:
            raise CandleGap("Simulated REST history gap")
        return result


def test_bootstrap_recovers_hole_older_than_overlap(db_engine: Engine) -> None:
    async def run() -> None:
        store = CandleStore(db_engine)
        for i in (0, 1, 2, 4, 5, 6, 7, 8):
            store.upsert(bar(i))
        stub = StubREST(9 * STEP)
        service = PublicMarketDataService(stub, store, initial_bars=5, overlap_bars=2)  # type: ignore[arg-type]
        await service.bootstrap("BTCUSDT", "1m")
        assert store.gap_ranges("BTCUSDT", "1m") == []
        assert (3 * STEP, 4 * STEP) in stub.calls
    asyncio.run(run())


def test_gap_filled_before_accepting_later_ws_candle(db_engine: Engine) -> None:
    async def run() -> None:
        store = CandleStore(db_engine)
        store.upsert(bar(1))
        stub = StubREST(6 * STEP)
        service = PublicMarketDataService(stub, store)  # type: ignore[arg-type]
        await service.ingest(bar(4, source="WS"))
        assert store.latest_open_ms("BTCUSDT", "1m") == 4 * STEP
        assert store.gap_ranges("BTCUSDT", "1m") == []
        assert (2 * STEP, 4 * STEP) in stub.calls
        # Out-of-order duplicate is non-destructive
        assert await service.ingest(bar(2, source="WS")) == "DUPLICATE"
    asyncio.run(run())


def test_failed_repair_does_not_persist_later_ws_candle(db_engine: Engine) -> None:
    async def run() -> None:
        store = CandleStore(db_engine)
        store.upsert(bar(1))
        service = PublicMarketDataService(
            StubREST(6 * STEP, missing={2}), store  # type: ignore[arg-type]
        )
        with pytest.raises(CandleGap):
            await service.ingest(bar(4, source="WS"))
        assert store.latest_open_ms("BTCUSDT", "1m") == STEP
    asyncio.run(run())
