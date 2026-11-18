"""Manual or opt-in read-only classifier loop. PAPER research mode exclusively."""

import argparse
import asyncio
import json
import logging
from datetime import UTC, datetime

from baqs.db import make_engine
from baqs.logging_config import configure_logging
from baqs.market_data.schema import SYMBOLS
from baqs.regime.rules import RegimeUnavailable
from baqs.regime.service import FIVE_MIN_MS, RegimeResearchService
from baqs.settings import get_settings

logger = logging.getLogger(__name__)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Classify completed BTC/ETH 5m bars using only public stored candles"
    )
    parser.add_argument("--once", action="store_true", help="Classify one observation then exit")
    parser.add_argument("--poll-seconds", type=int, default=15)
    return parser.parse_args()


async def run(*, once: bool = False, poll_seconds: int = 15) -> None:
    settings = get_settings()
    if settings.environment != "PAPER" or settings.live_enabled:
        raise RuntimeError("Regime classifier supports only PAPER research")
    if not 5 <= poll_seconds <= 300:
        raise ValueError("Poll interval must be between 5 and 300 seconds")
    configure_logging(settings.log_level)
    engine = make_engine(settings)
    service = RegimeResearchService(engine)
    try:
        while True:
            now_ms = int(datetime.now(UTC).timestamp() * 1000)
            candle_close = now_ms // FIVE_MIN_MS * FIVE_MIN_MS
            for symbol in sorted(SYMBOLS):
                try:
                    output = service.observe(symbol, close_ms=candle_close, clock_ms=now_ms)
                    print(json.dumps(output, ensure_ascii=False, default=str))
                except RegimeUnavailable as exc:
                    logger.warning("%s: market state unavailable: %s", symbol, exc)
                    if once:
                        raise
            if once:
                break
            await asyncio.sleep(poll_seconds)
    finally:
        engine.dispose()


def main() -> None:
    args = _args()
    asyncio.run(run(once=args.once, poll_seconds=args.poll_seconds))


if __name__ == "__main__":
    main()
