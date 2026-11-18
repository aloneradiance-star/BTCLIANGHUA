"""Opt-in public market collector: PAPER only, no private API credentials."""

import asyncio
import json
import logging

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from baqs.db import make_engine
from baqs.logging_config import configure_logging
from baqs.market_data.repository import CandleStore
from baqs.market_data.rest import PUBLIC_BASE_URL, PublicFuturesREST
from baqs.market_data.schema import INTERVAL_MS, SYMBOLS, MarketDataError, from_ws_message
from baqs.market_data.service import PublicMarketDataService
from baqs.settings import get_settings

logger = logging.getLogger(__name__)
MARKET_WS_BASE = "wss://fstream.binance.com/market/stream?streams="


def combined_stream_url() -> str:
    streams = [
        f"{symbol.lower()}@kline_{timeframe}"
        for symbol in sorted(SYMBOLS)
        for timeframe in INTERVAL_MS
    ]
    return MARKET_WS_BASE + "/".join(streams)


async def run_collector() -> None:
    settings = get_settings()  # Will reject LIVE_ENABLED=true
    if settings.environment != "PAPER":
        raise RuntimeError("Collector only operates in PAPER research mode")
    configure_logging(settings.log_level)
    engine = make_engine(settings)
    backoff = 1.0
    try:
        async with httpx.AsyncClient(
            base_url=PUBLIC_BASE_URL, timeout=10.0, follow_redirects=False
        ) as client:
            rest = PublicFuturesREST(client)
            service = PublicMarketDataService(rest, CandleStore(engine))
            while True:
                try:
                    async with connect(
                        combined_stream_url(), ping_interval=20, ping_timeout=20,
                        max_queue=256, open_timeout=10, close_timeout=5,
                    ) as ws:
                        # REST replay AFTER websocket connection; buffered events are
                        # processed after reconciliation to close the reconnect race.
                        await service.bootstrap_all()
                        backoff = 1.0
                        async for message in ws:
                            if not isinstance(message, str):
                                continue
                            candle = from_ws_message(json.loads(message))
                            if candle is not None:
                                await service.ingest(candle)
                except (
                    ConnectionClosed, InvalidHandshake, OSError, TimeoutError,
                    MarketDataError, json.JSONDecodeError, httpx.HTTPError,
                ) as exc:
                    logger.warning("Public market stream unhealthy; reconnecting: %s", type(exc).__name__)
                    await asyncio.sleep(backoff)
                    backoff = min(30.0, backoff * 2)
    finally:
        engine.dispose()


def main() -> None:
    asyncio.run(run_collector())


if __name__ == "__main__":
    main()
