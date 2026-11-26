"""Manual, public-only historical funding backfill; never auto-started."""

import argparse
import asyncio
import json
import logging
from dataclasses import asdict

import httpx

from baqs.db import make_engine
from baqs.logging_config import configure_logging
from baqs.market_data.funding import FundingStore
from baqs.market_data.funding_history import PublicFundingBackfill
from baqs.market_data.rest import PUBLIC_BASE_URL, PublicFuturesREST
from baqs.market_data.schema import SYMBOLS
from baqs.settings import get_settings

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manually backfill public Binance USD-M funding history"
    )
    parser.add_argument("--start-ms", type=int, required=True)
    parser.add_argument("--end-ms", type=int, required=True)
    parser.add_argument("--symbols", nargs="+", choices=sorted(SYMBOLS), default=sorted(SYMBOLS))
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--pause-seconds", type=float, default=1.25)
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> None:
    settings = get_settings()
    if settings.environment != "PAPER" or settings.live_enabled:
        raise RuntimeError("Public funding backfill only permits PAPER research mode")
    configure_logging(settings.log_level)
    engine = make_engine(settings)
    try:
        async with httpx.AsyncClient(
            base_url=PUBLIC_BASE_URL,
            timeout=10,
            follow_redirects=False,
        ) as client:
            service = PublicFundingBackfill(
                PublicFuturesREST(client),
                FundingStore(engine),
                page_size=args.page_size,
                max_pages=args.max_pages,
                pause_seconds=args.pause_seconds,
            )
            for symbol in args.symbols:
                report = await service.run(symbol, args.start_ms, args.end_ms)
                print(json.dumps(asdict(report), sort_keys=True))
                logger.info("Public funding window verified for %s", symbol)
    finally:
        engine.dispose()


def main() -> None:
    asyncio.run(_run(parse_args()))


if __name__ == "__main__":
    main()
