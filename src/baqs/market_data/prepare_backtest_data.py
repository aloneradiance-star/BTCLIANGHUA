"""Manual one-command preparation of complete public 1m backtest datasets.

This is opt-in research data collection. It never runs at API startup and never
accesses account endpoints, credentials, private streams or trading routes.
"""

import argparse
import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from typing import Protocol

import httpx

from baqs.db import make_engine
from baqs.logging_config import configure_logging
from baqs.market_data.funding import FundingStore
from baqs.market_data.funding_history import PublicFundingBackfill, PublicFundingReader
from baqs.market_data.history import DEFAULT_BARS, PublicHistoryBackfill, PublicHistoryReader
from baqs.market_data.repository import CandleStore
from baqs.market_data.rest import PUBLIC_BASE_URL, PublicFuturesREST
from baqs.market_data.schema import INTERVAL_MS, SYMBOLS, CandleGap, FundingGap
from baqs.settings import get_settings

logger = logging.getLogger(__name__)


class PublicBacktestDataReader(PublicHistoryReader, PublicFundingReader, Protocol):
    """The public unsigned REST operations required by both bounded collectors."""


@dataclass(frozen=True, slots=True)
class BacktestDatasetReport:
    symbol: str
    start_ms: int
    end_ms: int
    execution_bars: int
    funding_events: int
    created_candles: int
    reused_candle_chunks: int
    created_funding_events: int
    reused_funding_events: int
    execution_status: str
    funding_status: str
    readiness: str


class PublicBacktestDataPreparer:
    """Backfill 1m candles and funding for one exact, shared half-open UTC window."""

    def __init__(
        self,
        reader: PublicBacktestDataReader,
        candle_store: CandleStore,
        funding_store: FundingStore,
        *,
        chunk_bars: int = 400,
        funding_page_size: int = 1000,
        funding_max_pages: int = 100,
        funding_pause_seconds: float = 1.25,
    ) -> None:
        self.reader = reader
        self.candle_store = candle_store
        self.funding_store = funding_store
        self.chunk_bars = chunk_bars
        self.funding_page_size = funding_page_size
        self.funding_max_pages = funding_max_pages
        self.funding_pause_seconds = funding_pause_seconds

    async def run(self, symbol: str, *, bars: int = DEFAULT_BARS["1m"]) -> BacktestDatasetReport:
        candle_report = await PublicHistoryBackfill(
            self.reader,
            self.candle_store,
            chunk_bars=self.chunk_bars,
        ).run(symbol, "1m", bars=bars)

        end_ms = candle_report.anchored_end_ms
        start_ms = end_ms - candle_report.target_bars * INTERVAL_MS["1m"]
        funding_report = await PublicFundingBackfill(
            self.reader,
            self.funding_store,
            page_size=self.funding_page_size,
            max_pages=self.funding_max_pages,
            pause_seconds=self.funding_pause_seconds,
        ).run(symbol, start_ms, end_ms)

        execution = self.candle_store.history_readiness(
            symbol,
            "1m",
            end_ms,
            required_bars=candle_report.target_bars,
        )
        funding = self.funding_store.coverage_readiness(symbol, start_ms, end_ms)
        execution_status = str(execution["status"])
        funding_status = str(funding["status"])
        if execution_status != "READY":
            raise CandleGap(f"Backtest execution window is not complete: {execution}")
        if funding_status != "READY":
            raise FundingGap(f"Backtest funding window is not complete: {funding}")

        return BacktestDatasetReport(
            symbol=symbol,
            start_ms=start_ms,
            end_ms=end_ms,
            execution_bars=candle_report.target_bars,
            funding_events=funding_report.fetched_events,
            created_candles=candle_report.created_bars,
            reused_candle_chunks=candle_report.reused_chunks,
            created_funding_events=funding_report.created_events,
            reused_funding_events=funding_report.reused_events,
            execution_status=execution_status,
            funding_status=funding_status,
            readiness="READY",
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manually prepare public BTC/ETH 1m candles and exact funding history"
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        choices=sorted(SYMBOLS),
        default=sorted(SYMBOLS),
    )
    parser.add_argument(
        "--bars",
        type=int,
        default=DEFAULT_BARS["1m"],
        help="Number of finalized 1m bars per symbol (default: 43200 / 30 days)",
    )
    parser.add_argument("--chunk-bars", type=int, default=400)
    parser.add_argument("--funding-page-size", type=int, default=1000)
    parser.add_argument("--funding-max-pages", type=int, default=100)
    parser.add_argument("--funding-pause-seconds", type=float, default=1.25)
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> None:
    settings = get_settings()
    if settings.environment != "PAPER" or settings.live_enabled:
        raise RuntimeError("Backtest data preparation only permits PAPER research mode")
    configure_logging(settings.log_level)
    engine = make_engine(settings)
    try:
        async with httpx.AsyncClient(
            base_url=PUBLIC_BASE_URL,
            timeout=10,
            follow_redirects=False,
        ) as client:
            reader = PublicFuturesREST(client)
            preparer = PublicBacktestDataPreparer(
                reader,
                CandleStore(engine),
                FundingStore(engine),
                chunk_bars=args.chunk_bars,
                funding_page_size=args.funding_page_size,
                funding_max_pages=args.funding_max_pages,
                funding_pause_seconds=args.funding_pause_seconds,
            )
            for symbol in args.symbols:
                report = await preparer.run(symbol, bars=args.bars)
                print(json.dumps(asdict(report), sort_keys=True))
                logger.info(
                    "Backtest dataset READY for %s [%s,%s)",
                    symbol,
                    report.start_ms,
                    report.end_ms,
                )
    finally:
        engine.dispose()


def main() -> None:
    asyncio.run(_run(parse_args()))


if __name__ == "__main__":
    main()
