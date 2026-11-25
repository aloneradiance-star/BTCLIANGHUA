"""Gap-aware ingestion orchestration for finalized public BTC and ETH candles."""

import logging

from baqs.market_data.repository import CandleStore, StoreResult
from baqs.market_data.rest import PublicFuturesREST
from baqs.market_data.schema import INTERVAL_MS, SYMBOLS, CandleGap, FinalCandle

logger = logging.getLogger(__name__)


class PublicMarketDataService:
    def __init__(
        self,
        rest: PublicFuturesREST,
        store: CandleStore,
        *,
        initial_bars: int = 120,
        overlap_bars: int = 20,
    ) -> None:
        if initial_bars < 1 or overlap_bars < 1:
            raise ValueError("Lookback and overlap must be positive")
        self.rest = rest
        self.store = store
        self.initial_bars = initial_bars
        self.overlap_bars = overlap_bars

    async def bootstrap(self, symbol: str, timeframe: str) -> int:
        """On startup and reconnect: replay recent REST closed bars and repair window gaps."""
        if symbol not in SYMBOLS or timeframe not in INTERVAL_MS:
            raise ValueError("Unsupported symbol or timeframe")
        step = INTERVAL_MS[timeframe]
        server_time = await self.rest.server_time_ms()
        upper = (server_time // step) * step
        latest = self.store.latest_open_ms(symbol, timeframe)
        start = max(
            0,
            upper - self.initial_bars * step
            if latest is None
            else latest - (self.overlap_bars - 1) * step,
        )
        if latest is None and start == 0:
            start = 0
        fetched = await self.rest.fetch_range(symbol, timeframe, start, upper)
        for candle in fetched:
            self.store.upsert(candle)
        # Inspect a bounded recent window, including gaps older than replay overlap.
        for gap_start, gap_end in self.store.gap_ranges(symbol, timeframe):
            repaired = await self.rest.fetch_range(symbol, timeframe, gap_start, gap_end)
            for candle in repaired:
                self.store.upsert(candle)
        check = self.store.health(symbol, timeframe, server_time)
        if check["status"] != "READY":
            raise CandleGap(f"{symbol}/{timeframe} unhealthy after bootstrap: {check}")
        logger.info("Public candle feed synchronized: %s %s", symbol, timeframe)
        return len(fetched)

    async def bootstrap_all(self) -> None:
        """A reconnect is only healthy once all eight feeds have been repaired."""
        for symbol in sorted(SYMBOLS):
            for timeframe in INTERVAL_MS:
                await self.bootstrap(symbol, timeframe)

    async def ingest(self, candle: FinalCandle) -> StoreResult:
        """Repair a missing interval before accepting the later closed candle."""
        latest = self.store.latest_open_ms(candle.symbol, candle.timeframe)
        step = INTERVAL_MS[candle.timeframe]
        if latest is not None and candle.open_ms > latest + step:
            gap_start = latest + step
            missing = await self.rest.fetch_range(
                candle.symbol, candle.timeframe, gap_start, candle.open_ms
            )
            expected = (candle.open_ms - gap_start) // step
            if len(missing) != expected:
                raise CandleGap("REST repair did not cover the full interval before the WS candle")
            for recovered in missing:
                self.store.upsert(recovered)
        result = self.store.upsert(candle)
        return result
