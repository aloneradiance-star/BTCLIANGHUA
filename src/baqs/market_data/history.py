"""Explicit, resumable, bounded, PUBLIC Binance USD-M historical research backfill.

The persisted finalized-candle composite key doubles as the durable checkpoint.
No credentials, exchange account connection, trading API, or live-order path.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

from baqs.market_data.repository import CandleStore
from baqs.market_data.schema import INTERVAL_MS, SYMBOLS, CandleGap, FinalCandle

logger = logging.getLogger(__name__)
MIN_15M_BARS = 2920  # Operational minimum includes the classifier warm-up buffer.
MIN_1H_BARS = 120  # Classifier readiness uses a 120-bar 1h buffer.
DEFAULT_BARS = {"1m": 43_200, "15m": 2920, "1h": 120}
MINIMUM_BARS = {"1m": 1, "15m": MIN_15M_BARS, "1h": MIN_1H_BARS}
MAX_BACKFILL_BARS = 50_000


class PublicHistoryReader(Protocol):
    async def server_time_ms(self) -> int: ...

    async def fetch_range(
        self,
        symbol: str,
        timeframe: str,
        start_ms: int,
        end_ms: int,
        *,
        max_bars: int = 2000,
        page_size: int = 499,
    ) -> list[FinalCandle]: ...


@dataclass(frozen=True, slots=True)
class BackfillReport:
    symbol: str
    timeframe: str
    target_bars: int
    anchored_end_ms: int
    fetched_bars: int
    created_bars: int
    reused_chunks: int
    readiness: str


class PublicHistoryBackfill:
    """Revalidates complete chunks and only refetches missing/partial chunks on resume."""

    def __init__(
        self,
        reader: PublicHistoryReader,
        store: CandleStore,
        *,
        chunk_bars: int = 400,
        pause_seconds: float = 0.20,
    ) -> None:
        if not 1 <= chunk_bars <= 499:
            raise ValueError("Chunk must be 1..499 bars")
        if not 0 <= pause_seconds <= 30:
            raise ValueError("Pause must be 0..30 seconds")
        self.reader = reader
        self.store = store
        self.chunk_bars = chunk_bars
        self.pause_seconds = pause_seconds

    async def run(
        self,
        symbol: str,
        timeframe: str,
        *,
        bars: int | None = None,
    ) -> BackfillReport:
        if symbol not in SYMBOLS or timeframe not in DEFAULT_BARS:
            raise ValueError("Only BTC/ETH 1m, 15m and 1h public history is supported")
        requested = DEFAULT_BARS[timeframe] if bars is None else bars
        minimum = MINIMUM_BARS[timeframe]
        if type(requested) is not int or not minimum <= requested <= MAX_BACKFILL_BARS:
            raise ValueError(f"{timeframe} backfill requires {minimum}..{MAX_BACKFILL_BARS} bars")

        step = INTERVAL_MS[timeframe]
        server_ms = await self.reader.server_time_ms()
        if type(server_ms) is not int or server_ms < 0:
            raise CandleGap("Invalid Binance server clock")
        anchor = server_ms // step * step  # excludes all unfinished candles
        start_ms = anchor - requested * step
        if start_ms < 0:
            raise CandleGap("Insufficient exchange history for requested range")
        created, fetched, reused = 0, 0, 0

        for chunk_start in range(start_ms, anchor, self.chunk_bars * step):
            chunk_end = min(anchor, chunk_start + self.chunk_bars * step)
            chunk_count = (chunk_end - chunk_start) // step
            readiness = self.store.history_readiness(
                symbol,
                timeframe,
                chunk_end,
                required_bars=chunk_count,
            )
            if readiness["status"] == "READY":
                reused += 1
                continue

            segment = await self.reader.fetch_range(
                symbol,
                timeframe,
                chunk_start,
                chunk_end,
                max_bars=self.chunk_bars,
                page_size=self.chunk_bars,
            )
            if len(segment) != chunk_count:
                raise CandleGap("REST history returned an incomplete chunk")
            for index, candle in enumerate(segment):
                if (
                    candle.symbol != symbol
                    or candle.timeframe != timeframe
                    or candle.open_ms != chunk_start + index * step
                    or candle.close_ms != chunk_start + (index + 1) * step
                ):
                    raise CandleGap(
                        "REST history returned mismatched, out-of-order or incomplete bars"
                    )
            fetched += len(segment)
            outcomes = self.store.upsert_many(segment)
            created += outcomes.count("CREATED")

            verification = self.store.history_readiness(
                symbol,
                timeframe,
                chunk_end,
                required_bars=chunk_count,
            )
            if verification["status"] != "READY":
                raise CandleGap(f"Stored history chunk verification failed: {verification}")
            logger.info(
                "History chunk verified %s %s: [%s,%s)",
                symbol,
                timeframe,
                chunk_start,
                chunk_end,
            )
            if self.pause_seconds:
                await asyncio.sleep(self.pause_seconds)

        final = self.store.history_readiness(
            symbol,
            timeframe,
            anchor,
            required_bars=requested,
        )
        if final["status"] != "READY":
            raise CandleGap(f"Full historical window is not ready: {final}")
        return BackfillReport(
            symbol=symbol,
            timeframe=timeframe,
            target_bars=requested,
            anchored_end_ms=anchor,
            fetched_bars=fetched,
            created_bars=created,
            reused_chunks=reused,
            readiness="READY",
        )
