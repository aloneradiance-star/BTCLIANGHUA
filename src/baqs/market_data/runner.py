"""Opt-in public market collector: PAPER only, no private API credentials."""

import asyncio
import json
import logging
from time import time_ns

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from baqs.db import make_engine
from baqs.logging_config import configure_logging
from baqs.market_data.repository import STREAM_IDLE_TIMEOUT_SECONDS, CandleStore, MarketStreamStore
from baqs.market_data.rest import PUBLIC_BASE_URL, PublicFuturesREST
from baqs.market_data.schema import (
    INTERVAL_MS,
    SYMBOLS,
    InvalidCandle,
    MarketDataError,
    from_ws_message,
)
from baqs.market_data.service import PublicMarketDataService
from baqs.settings import get_settings

logger = logging.getLogger(__name__)
MARKET_WS_BASE = "wss://fstream.binance.com/market/stream?streams="
STREAM_RECEIPT_INTERVAL_SECONDS = 5


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
    stream_store = MarketStreamStore(engine)
    loop = asyncio.get_running_loop()
    backoff = 1.0
    try:
        stream_store.record_message(None)
        async with httpx.AsyncClient(
            base_url=PUBLIC_BASE_URL, timeout=10.0, follow_redirects=False
        ) as client:
            rest = PublicFuturesREST(client)
            service = PublicMarketDataService(rest, CandleStore(engine))
            while True:
                try:
                    async with connect(
                        combined_stream_url(),
                        ping_interval=20,
                        ping_timeout=20,
                        max_queue=256,
                        open_timeout=10,
                        close_timeout=5,
                    ) as ws:
                        try:
                            # Replay REST before consuming buffered events.
                            await service.bootstrap_all()
                            messages = aiter(ws)
                            next_receipt = 0.0
                            while True:
                                async with asyncio.timeout(STREAM_IDLE_TIMEOUT_SECONDS):
                                    message = await anext(messages, None)
                                if message is None:
                                    break
                                if not isinstance(message, str):
                                    continue
                                received_ms = time_ns() // 1_000_000
                                candle = from_ws_message(json.loads(message))
                                if candle is not None:
                                    if candle.close_ms > received_ms:
                                        raise InvalidCandle("WebSocket candle closes after receipt")
                                    await service.ingest(candle)
                                    backoff = 1.0
                                current = loop.time()
                                if current >= next_receipt:
                                    stream_store.record_message(received_ms)
                                    next_receipt = current + STREAM_RECEIPT_INTERVAL_SECONDS
                        finally:
                            stream_store.record_message(None)
                except (
                    ConnectionClosed,
                    InvalidHandshake,
                    OSError,
                    TimeoutError,
                    MarketDataError,
                    json.JSONDecodeError,
                    httpx.HTTPError,
                ) as exc:
                    logger.warning(
                        "Public market stream unhealthy; reconnecting: %s", type(exc).__name__
                    )
                else:
                    logger.info("Public market stream closed; reconnecting")
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2)
    finally:
        engine.dispose()


def main() -> None:
    asyncio.run(run_collector())


if __name__ == "__main__":
    main()
