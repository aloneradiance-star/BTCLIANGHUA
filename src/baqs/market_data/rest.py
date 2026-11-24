"""Bounded, public-only Binance USD-M historical candle backfill."""

import asyncio
from collections.abc import Sequence
from typing import cast

import httpx

from baqs.market_data.schema import (
    INTERVAL_MS,
    SYMBOLS,
    CandleGap,
    FinalCandle,
    FundingGap,
    FundingRateRecord,
    InvalidCandle,
    MarketDataError,
    from_funding_row,
    from_rest_row,
)

PUBLIC_BASE_URL = "https://fapi.binance.com"


class PublicFuturesREST:
    """Inject an HTTP client for completely offline tests; never add signed requests."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def _request(self, path: str, params: dict[str, int | str] | None = None) -> object:
        for attempt in range(3):
            delay = 0.25 * 2**attempt
            try:
                response = await self.client.get(path, params=params, timeout=10.0)
                response.raise_for_status()
                return response.json()
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == 2:
                    raise MarketDataError("Public REST connection failed") from exc
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise MarketDataError(f"Public REST returned HTTP {status}") from exc
                if status == 429:
                    retry_after = exc.response.headers.get("Retry-After")
                    if retry_after is not None:
                        try:
                            delay = min(60.0, max(0.0, float(retry_after)))
                        except ValueError:
                            pass
            except ValueError as exc:
                raise InvalidCandle("Public REST response is not JSON") from exc
            await asyncio.sleep(delay)
        raise MarketDataError("Public REST retry budget exceeded")

    async def server_time_ms(self) -> int:
        value = await self._request("/fapi/v1/time")
        if not isinstance(value, dict) or type(value.get("serverTime")) is not int:
            raise InvalidCandle("Public server time response is invalid")
        return cast(int, value["serverTime"])

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


    async def fetch_funding_range(
        self, symbol: str, start_ms: int, end_ms: int, *,
        page_size: int = 1000, max_pages: int = 100, pause_seconds: float = 1.0,
    ) -> list[FundingRateRecord]:
        """Fetch [start_ms, end_ms) with inclusive REST bounds and bounded pagination."""
        if symbol not in SYMBOLS:
            raise ValueError("Unsupported funding symbol")
        if (
            type(start_ms) is not int or type(end_ms) is not int
            or start_ms < 0 or end_ms <= start_ms
            or start_ms % 60_000 or end_ms % 60_000
        ):
            raise ValueError("Funding range must be a valid 1m-aligned UTC window")
        if (
            type(page_size) is not int or not 1 <= page_size <= 1000
            or type(max_pages) is not int or not 1 <= max_pages <= 1000
            or not 0 <= pause_seconds <= 60
        ):
            raise ValueError("Unsafe funding pagination limits")
        if end_ms > await self.server_time_ms():
            raise ValueError("Funding range cannot extend beyond Binance server time")

        cursor = start_ms
        previous_time = -1
        seen_keys: set[tuple[int, str]] = set()
        result: list[FundingRateRecord] = []
        for page_index in range(max_pages):
            response = await self._request(
                "/fapi/v1/fundingRate",
                {"symbol": symbol, "startTime": cursor, "endTime": end_ms - 1, "limit": page_size},
            )
            if not isinstance(response, list):
                raise InvalidCandle("Funding endpoint did not return a list")
            if not response:
                return result
            if len(response) > page_size:
                raise InvalidCandle("Funding endpoint exceeded requested page size")
            page = [from_funding_row(symbol, row) for row in response]
            for item in page:
                key = (item.funding_time_ms, item.rate_type)
                if (
                    item.funding_time_ms < cursor or item.funding_time_ms >= end_ms
                    or item.funding_time_ms < previous_time or key in seen_keys
                ):
                    raise FundingGap("Funding events are duplicated, out of order or out of range")
                seen_keys.add(key)
                previous_time = item.funding_time_ms
                result.append(item)
            if len(page) < page_size:
                return result

            boundary_time = page[-1].funding_time_ms
            boundary_response = await self._request(
                "/fapi/v1/fundingRate",
                {
                    "symbol": symbol,
                    "startTime": boundary_time,
                    "endTime": boundary_time,
                    "limit": 1000,
                },
            )
            if not isinstance(boundary_response, list) or len(boundary_response) >= 1000:
                raise FundingGap("Funding pagination boundary is ambiguous")
            boundary_events = [from_funding_row(symbol, row) for row in boundary_response]
            boundary_keys = {(item.funding_time_ms, item.rate_type) for item in boundary_events}
            page_boundary_keys = {
                (item.funding_time_ms, item.rate_type)
                for item in page if item.funding_time_ms == boundary_time
            }
            if len(boundary_keys) != len(boundary_events) or boundary_keys != page_boundary_keys:
                raise FundingGap("Funding page split events sharing one funding timestamp")
            cursor = boundary_time + 1
            if cursor >= end_ms:
                return result
            if page_index + 1 >= max_pages:
                raise FundingGap("Funding history exceeded bounded page budget")
            if pause_seconds:
                await asyncio.sleep(pause_seconds)
        raise FundingGap("Funding history exceeded bounded page budget")
