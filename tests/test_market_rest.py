"""REST paging, incomplete-candle gating, throttling and hard gap failure."""

import asyncio

import httpx
import pytest

from baqs.market_data.rest import PublicFuturesREST
from baqs.market_data.schema import CandleGap, InvalidCandle, MarketDataError

STEP = 60_000


def row(index: int) -> list[object]:
    start = index * STEP
    return [
        start, "100", "103", "99", "101", "5",
        start + STEP - 1, "500", 10, "0", "0", "0",
    ]


def make_client(*, missing: set[int] | None = None, invalid: bool = False) -> httpx.AsyncClient:
    missing = missing or set()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 11 * STEP})
        if request.url.path == "/fapi/v1/klines":
            start = int(request.url.params["startTime"]) // STEP
            end = int(request.url.params["endTime"]) // STEP
            limit = int(request.url.params["limit"])
            assert limit <= 499
            if invalid:
                return httpx.Response(200, json={"not": "rows"})
            return httpx.Response(200, json=[
                row(i) for i in range(start, end + 1) if i not in missing
            ][:limit])
        raise AssertionError(f"Unexpected private or unknown endpoint: {request.url.path}")

    return httpx.AsyncClient(
        base_url="https://fapi.binance.com", transport=httpx.MockTransport(handler)
    )


def test_paginated_history_is_contiguous() -> None:
    async def run() -> None:
        async with make_client() as client:
            bars = await PublicFuturesREST(client).fetch_range(
                "BTCUSDT", "1m", 0, 10 * STEP, page_size=3,
            )
        assert [bar.open_ms for bar in bars] == list(range(0, 10 * STEP, STEP))
    asyncio.run(run())


def test_no_partial_last_candle_included() -> None:
    async def run() -> None:
        async with make_client() as client:
            bars = await PublicFuturesREST(client).fetch_range(
                "BTCUSDT", "1m", 0, 12 * STEP, page_size=499,
            )
        assert len(bars) == 11
    asyncio.run(run())


def test_gap_never_silently_accepted() -> None:
    async def run() -> None:
        async with make_client(missing={4}) as client:
            with pytest.raises(CandleGap):
                await PublicFuturesREST(client).fetch_range("BTCUSDT", "1m", 0, 10 * STEP)
    asyncio.run(run())


def test_invalid_payload_rejected() -> None:
    async def run() -> None:
        async with make_client(invalid=True) as client:
            with pytest.raises(InvalidCandle):
                await PublicFuturesREST(client).fetch_range("BTCUSDT", "1m", 0, STEP)
    asyncio.run(run())


def test_unbounded_backfill_rejected() -> None:
    async def run() -> None:
        async with make_client() as client:
            with pytest.raises(CandleGap):
                await PublicFuturesREST(client).fetch_range(
                    "BTCUSDT", "1m", 0, 11 * STEP, max_bars=5,
                )
    asyncio.run(run())


def test_http_403_fail_closed() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"msg": "restricted"})
    async def run() -> None:
        async with httpx.AsyncClient(
            base_url="https://fapi.binance.com", transport=httpx.MockTransport(handler)
        ) as client:
            with pytest.raises(MarketDataError, match="HTTP 403"):
                await PublicFuturesREST(client).server_time_ms()
    asyncio.run(run())
