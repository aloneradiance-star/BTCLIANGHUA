"""Bounded, public-only Binance USD-M historical candle backfill."""

import asyncio
from collections.abc import Sequence

import httpx

from baqs.market_data.schema import (
    INTERVAL_MS,
    SYMBOLS,
    CandleGap,
    FinalCandle,
    InvalidCandle,
    MarketDataError,
    from_rest_row,
)

PUBLIC_BASE_URL = "https://fapi.binance.com"


class PublicFuturesREST:
    """Inject an HTTP client for completely offline tests; never add signed requests."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def _request(self, path: str, params: dict[str, int | str] | None = None) -> object:
        for attempt in range(3):
            try:
                response = await self.client.get(path, params=params, timeout=10.0)
                response.raise_for_status()
                return response.json()
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == 2:
                    raise MarketDataError("Public REST connection failed") from exc
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise MarketDataError(
                        f"Public REST returned HTTP {exc.response.status_code}"
                    ) from exc
            except ValueError as exc:
                raise InvalidCandle("Public REST response is not JSON") from exc
            await asyncio.sleep(0.25 * 2**attempt)
        raise MarketDataError("Public REST retry budget exceeded")

    async def server_time_ms(self) -> int:
        value = await self._request("/fapi/v1/time")
        if not isinstance(value, dict) or type(value.get("serverTime")) is not int:
            raise InvalidCandle("Public server time response is invalid")
        return value["serverTime"]

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
        """Fetch a complete [start_ms, end_ms) sequence of finalized candles.

        Raises CandleGap, never silently approves incomplete history.
        End and start must be aligned to candle boundaries.
        """
        if symbol not in SYMBOLS or timeframe not in INTERVAL_MS:
            raise ValueError("Unsupported symbol or interval")
        step = INTERVAL_MS[timeframe]
        if start_ms < 0 or end_ms < start_ms or start_ms % step or end_ms % step:
            raise ValueError("Unaligned or reversed candle range")
        if max_bars < 1 or not 1 <= page_size <= 499:
            raise ValueError("Unsafe pagination parameters")
        server_ms = await self.server_time_ms()
        upper = min(end_ms, (server_ms // step) * step)
        total_bars = (upper - start_ms) // step
        if total_bars > max_bars:
            raise CandleGap("Backfill range exceeds bounded retry budget")
        if total_bars <= 0:
            return []

        result: list[FinalCandle] = []
        cursor = start_ms
        while cursor < upper:
            amount = min(page_size, (upper - cursor) // step)
            response = await self._request(
                "/fapi/v1/klines",
                {
                    "symbol": symbol,
                    "interval": timeframe,
                    "startTime": cursor,
                    "endTime": upper - 1,
                    "limit": amount,
                },
            )
            if not isinstance(response, list):
                raise InvalidCandle("Kline endpoint did not return a list")
            rows: Sequence[FinalCandle] = [
                candle
                for item in response
                if (candle := from_rest_row(symbol, timeframe, item, server_ms)) is not None
            ]
            if not rows:
                raise CandleGap(f"No candles available at {cursor}")
            for candle in rows:
                if candle.open_ms != cursor:
                    raise CandleGap(f"Missing or out-of-order candle at {cursor}")
                result.append(candle)
                cursor += step
                if cursor >= upper:
                    break
        return result
