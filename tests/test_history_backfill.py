"""Checkpoint recovery, REST strictness and fail-closed public-only historical sync."""

import asyncio
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import Engine

from baqs.market_data.history import PublicHistoryBackfill
from baqs.market_data.repository import CandleStore
from baqs.market_data.rest import PublicFuturesREST
from baqs.market_data.schema import CandleConflict, CandleGap, FinalCandle

STEP = 3_600_000
ANCHOR = 200 * STEP


def closed(index: int, *, price: int = 100) -> FinalCandle:
    return FinalCandle(
        "BTCUSDT",
        "1h",
        index * STEP,
        (index + 1) * STEP,
        Decimal(price),
        Decimal(104),
        Decimal(98),
        Decimal(101),
        Decimal(1),
        Decimal(100),
        5,
        "REST",
    )


class StubReader:
    def __init__(self, *, fail_start: int | None = None, missing: int | None = None) -> None:
        self.fail_start = fail_start
        self.missing = missing
        self.calls: list[tuple[int, int]] = []

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
        assert symbol == "BTCUSDT" and timeframe == "1h"
        assert page_size <= 499 and (end_ms - start_ms) // STEP <= max_bars
        self.calls.append((start_ms, end_ms))
        if start_ms == self.fail_start:
            raise CandleGap("simulated public REST interruption")
        return [closed(i) for i in range(start_ms // STEP, end_ms // STEP) if i != self.missing]


def test_complete_backfill_and_rerun_skips_all_verified_chunks(db_engine: Engine) -> None:
    async def run() -> None:
        store = CandleStore(db_engine)
        reader = StubReader()
        history = PublicHistoryBackfill(reader, store, chunk_bars=20, pause_seconds=0)
        first = await history.run("BTCUSDT", "1h", bars=120)
        assert first.readiness == "READY"
        assert first.created_bars == first.fetched_bars == 120
        assert first.reused_chunks == 0
        assert (
            store.history_readiness("BTCUSDT", "1h", ANCHOR, required_bars=120)["status"] == "READY"
        )
        again = await history.run("BTCUSDT", "1h", bars=120)
        assert again.fetched_bars == again.created_bars == 0
        assert again.reused_chunks == 6
        assert len(reader.calls) == 6

    asyncio.run(run())


def test_restart_resumes_from_finalized_database_checkpoint(db_engine: Engine) -> None:
    async def run() -> None:
        store = CandleStore(db_engine)
        fail_second = 100 * STEP
        reader = StubReader(fail_start=fail_second)
        history = PublicHistoryBackfill(reader, store, chunk_bars=20, pause_seconds=0)
        with pytest.raises(CandleGap, match="interruption"):
            await history.run("BTCUSDT", "1h", bars=120)
        assert (
            store.history_readiness("BTCUSDT", "1h", 100 * STEP, required_bars=20)["status"]
            == "READY"
        )
        reader.fail_start = None
        resumed = await history.run("BTCUSDT", "1h", bars=120)
        assert resumed.reused_chunks == 1
        assert resumed.created_bars == 100
        assert resumed.readiness == "READY"

    asyncio.run(run())


def test_missing_candle_causes_failure_without_ready_status(db_engine: Engine) -> None:
    async def run() -> None:
        reader = StubReader(missing=85)
        store = CandleStore(db_engine)
        history = PublicHistoryBackfill(reader, store, chunk_bars=20, pause_seconds=0)
        with pytest.raises(CandleGap, match="incomplete chunk"):
            await history.run("BTCUSDT", "1h", bars=120)
        report = store.history_readiness("BTCUSDT", "1h", ANCHOR, required_bars=120)
        assert report["status"] != "READY"

    asyncio.run(run())


def test_conflicting_final_candle_is_never_silently_replaced(db_engine: Engine) -> None:
    async def run() -> None:
        store = CandleStore(db_engine)
        store.upsert(closed(85, price=103))
        history = PublicHistoryBackfill(
            StubReader(),
            store,
            chunk_bars=20,
            pause_seconds=0,
        )
        with pytest.raises(CandleConflict):
            await history.run("BTCUSDT", "1h", bars=120)

    asyncio.run(run())


def test_strict_minimum_15m_reference_window(db_engine: Engine) -> None:
    async def run() -> None:
        history = PublicHistoryBackfill(StubReader(), CandleStore(db_engine))
        with pytest.raises(ValueError, match="2920"):
            await history.run("BTCUSDT", "15m", bars=2919)
        with pytest.raises(ValueError, match="120"):
            await history.run("ETHUSDT", "1h", bars=119)
        with pytest.raises(ValueError):
            await history.run("SOLUSDT", "15m")

    asyncio.run(run())


def test_public_http_transport_integration_with_all_expected_bars(db_engine: Engine) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": ANCHOR})
        assert request.url.path == "/fapi/v1/klines"
        assert request.url.params["symbol"] == "BTCUSDT"
        start = int(request.url.params["startTime"]) // STEP
        end = int(request.url.params["endTime"]) // STEP
        limit = int(request.url.params["limit"])
        return httpx.Response(
            200,
            json=[
                [
                    i * STEP,
                    "100",
                    "104",
                    "98",
                    "101",
                    "1",
                    (i + 1) * STEP - 1,
                    "100",
                    5,
                    "0",
                    "0",
                    "0",
                ]
                for i in range(start, min(end + 1, start + limit))
            ],
        )

    async def run() -> None:
        store = CandleStore(db_engine)
        async with httpx.AsyncClient(
            base_url="https://fapi.binance.com",
            transport=httpx.MockTransport(handler),
        ) as client:
            report = await PublicHistoryBackfill(
                PublicFuturesREST(client),
                store,
                chunk_bars=20,
                pause_seconds=0,
            ).run("BTCUSDT", "1h", bars=120)
        assert report.fetched_bars == 120
        assert report.readiness == "READY"

    asyncio.run(run())


def test_partial_chunk_crash_resumes_without_duplicate_inserts(db_engine: Engine) -> None:
    class FailingStore(CandleStore):
        def __init__(self, engine: Engine) -> None:
            super().__init__(engine)
            self.attempts = 0

        def upsert_many(self, candles: list[FinalCandle]) -> list[str]:
            self.attempts += len(candles)
            if self.attempts >= 7:
                super().upsert_many(candles[:6])
                raise RuntimeError("Injected crash halfway through a chunk")
            return super().upsert_many(candles)

    async def run() -> None:
        failing_store = FailingStore(db_engine)
        reader = StubReader()
        with pytest.raises(RuntimeError, match="halfway"):
            await PublicHistoryBackfill(reader, failing_store, chunk_bars=20, pause_seconds=0).run(
                "BTCUSDT", "1h", bars=120
            )
        recovered_store = CandleStore(db_engine)
        assert (
            recovered_store.history_readiness("BTCUSDT", "1h", 100 * STEP, required_bars=20)[
                "status"
            ]
            != "READY"
        )
        recovered = await PublicHistoryBackfill(
            reader, recovered_store, chunk_bars=20, pause_seconds=0
        ).run("BTCUSDT", "1h", bars=120)
        assert recovered.readiness == "READY"
        assert recovered.created_bars == 114
        assert recovered.fetched_bars == 120
        assert recovered.reused_chunks == 0

    asyncio.run(run())


def test_1m_execution_backfill_supports_30d_default_and_chunk_resume(db_engine: Engine) -> None:
    from baqs.market_data.history import DEFAULT_BARS, MAX_BACKFILL_BARS

    minute = 60_000
    anchor = 100 * minute

    class MinuteReader:
        async def server_time_ms(self) -> int:
            return anchor

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
            assert symbol == "BTCUSDT" and timeframe == "1m"
            assert (end_ms - start_ms) // minute <= max_bars
            return [
                FinalCandle(
                    symbol,
                    timeframe,
                    open_ms,
                    open_ms + minute,
                    Decimal(100),
                    Decimal(102),
                    Decimal(99),
                    Decimal(101),
                    Decimal(1),
                    Decimal(100),
                    5,
                    "REST",
                )
                for open_ms in range(start_ms, end_ms, minute)
            ]

    async def run() -> None:
        assert DEFAULT_BARS["1m"] == 43_200
        assert MAX_BACKFILL_BARS >= DEFAULT_BARS["1m"]
        report = await PublicHistoryBackfill(
            MinuteReader(), CandleStore(db_engine), chunk_bars=20, pause_seconds=0
        ).run("BTCUSDT", "1m", bars=60)
        assert report.readiness == "READY"
        assert report.created_bars == 60

    asyncio.run(run())


def test_explicit_aligned_range_backfill_uses_exact_window(db_engine: Engine) -> None:
    async def run() -> None:
        store = CandleStore(db_engine)
        reader = StubReader()
        report = await PublicHistoryBackfill(
            reader, store, chunk_bars=20, pause_seconds=0
        ).run(
            "BTCUSDT",
            "1h",
            start_ms=80 * STEP,
            end_ms=ANCHOR,
        )
        assert report.target_bars == 120
        assert report.anchored_end_ms == ANCHOR
        assert report.fetched_bars == 120
        assert report.readiness == "READY"
        assert reader.calls[0] == (80 * STEP, 100 * STEP)

    asyncio.run(run())


def test_explicit_range_rejects_invalid_bounds_and_ambiguous_bars(db_engine: Engine) -> None:
    async def run() -> None:
        history = PublicHistoryBackfill(StubReader(), CandleStore(db_engine))
        with pytest.raises(ValueError, match="both start_ms and end_ms"):
            await history.run("BTCUSDT", "1h", start_ms=80 * STEP)
        with pytest.raises(ValueError, match="not both"):
            await history.run(
                "BTCUSDT", "1h", bars=120, start_ms=80 * STEP, end_ms=ANCHOR
            )
        with pytest.raises(ValueError, match="interval-aligned"):
            await history.run(
                "BTCUSDT", "1h", start_ms=80 * STEP + 1, end_ms=ANCHOR
            )
        with pytest.raises(ValueError, match="120..2160"):
            await history.run("BTCUSDT", "1h", start_ms=0, end_ms=2161 * STEP)
        with pytest.raises(CandleGap, match="beyond Binance server time"):
            await history.run(
                "BTCUSDT", "1h", start_ms=80 * STEP, end_ms=ANCHOR + STEP
            )

    asyncio.run(run())
