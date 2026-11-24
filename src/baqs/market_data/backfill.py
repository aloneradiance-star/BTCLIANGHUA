"""Manual entry point for PUBLIC historical research backfill.

Run explicitly, never as part of web API startup or a trading execution engine.
"""

import argparse
import asyncio
import json
import logging
from dataclasses import asdict

import httpx

from baqs.db import make_engine
from baqs.logging_config import configure_logging
from baqs.market_data.history import MAX_BARS_BY_TIMEFRAME, DEFAULT_BARS, PublicHistoryBackfill
from baqs.market_data.repository import CandleStore
from baqs.market_data.rest import PUBLIC_BASE_URL, PublicFuturesREST
from baqs.market_data.schema import SYMBOLS
from baqs.settings import get_settings

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manually backfill public finalized Binance USD-M BTC/ETH candles"
    )
    parser.add_argument(
        "--timeframe",
        choices=sorted(DEFAULT_BARS),
        default="15m",
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
        help="Defaults: 43200 (1m), 2920 (15m), 120 (1h); 90-day caps: 129600, 8640, 2160",
    )
    parser.add_argument(
        "--start-ms",
        type=int,
        help="Explicit inclusive UTC window start (requires --end-ms; interval-aligned)",
    )
    parser.add_argument(
        "--end-ms",
        type=int,
        help="Explicit exclusive UTC window end (requires --start-ms; interval-aligned)",
    )
    parser.add_argument(
        "--chunk-bars",
        type=int,
        default=400,
    )
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> None:
    settings = get_settings()
    if settings.environment != "PAPER" or settings.live_enabled:
        raise RuntimeError("P1 history backfill only permits PAPER research mode")
    configure_logging(settings.log_level)
    engine = make_engine(settings)
    try:
        async with httpx.AsyncClient(
            base_url=PUBLIC_BASE_URL,
            timeout=10,
            follow_redirects=False,
        ) as client:
            service = PublicHistoryBackfill(
                PublicFuturesREST(client),
                CandleStore(engine),
                chunk_bars=args.chunk_bars,
            )
            for symbol in args.symbols:
                report = await service.run(
                    symbol,
                    args.timeframe,
                    bars=args.bars,
                    start_ms=args.start_ms,
                    end_ms=args.end_ms,
                )
                print(json.dumps(asdict(report), sort_keys=True))
                logger.info("Public history verified for %s %s", symbol, args.timeframe)
    finally:
        engine.dispose()


def main() -> None:
    asyncio.run(_run(parse_args()))


if __name__ == "__main__":
    main()
