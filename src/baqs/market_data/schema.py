"""Strict closed-candle parsing from Binance public REST and WebSocket feeds."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

INTERVAL_MS: dict[str, int] = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}
SYMBOLS = frozenset({"BTCUSDT", "ETHUSDT"})


class MarketDataError(Exception):
    """Public market-data quality or connectivity failure."""


class InvalidCandle(MarketDataError):
    """Malformed, inconsistent or unsupported candle."""


class CandleGap(MarketDataError):
    """Historical REST response has missing closed candles."""


class CandleConflict(MarketDataError):
    """An already finalized historical candle has conflicting values."""


def _ms_datetime(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _as_decimal(value: object) -> Decimal:
    if not isinstance(value, str | int):
        raise InvalidCandle("Price and quantity must be decimal strings or integers")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise InvalidCandle("Invalid numeric value") from exc
    if not result.is_finite():
        raise InvalidCandle("Non-finite numeric value")
    return result


def _as_ms(value: object) -> int:
    if type(value) is not int or value < 0:
        raise InvalidCandle("Timestamp must be a nonnegative integer in milliseconds")
    return value


@dataclass(frozen=True)
class FinalCandle:
    symbol: str
    timeframe: str
    open_ms: int
    close_ms: int  # exclusive UTC boundary, unlike Binance's inclusive T value
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal
    trade_count: int
    source: str

    def __post_init__(self) -> None:
        if self.symbol not in SYMBOLS or self.timeframe not in INTERVAL_MS:
            raise InvalidCandle("Unsupported candle symbol or interval")
        if self.source not in {"REST", "WS"}:
            raise InvalidCandle("Unsupported candle source")
        if self.open_ms < 0 or self.open_ms % INTERVAL_MS[self.timeframe]:
            raise InvalidCandle("Candle opening is not aligned to UTC interval")
        if self.close_ms != self.open_ms + INTERVAL_MS[self.timeframe]:
            raise InvalidCandle("Candle close boundary is inconsistent with interval")
        if not all(
            v.is_finite()
            for v in (self.open, self.high, self.low, self.close, self.volume, self.quote_volume)
        ):
            raise InvalidCandle("Non-finite OHLCV")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise InvalidCandle("OHLC prices must be positive")
        if self.high < max(self.open, self.close, self.low):
            raise InvalidCandle("High is below another price")
        if self.low > min(self.open, self.close):
            raise InvalidCandle("Low is above another price")
        if self.volume < 0 or self.quote_volume < 0 or self.trade_count < 0:
            raise InvalidCandle("Negative volume or trade count")

    @property
    def open_time(self) -> datetime:
        return _ms_datetime(self.open_ms)

    @property
    def close_time(self) -> datetime:
        return _ms_datetime(self.close_ms)

    def fingerprint(self) -> tuple[object, ...]:
        return (
            self.open,
            self.high,
            self.low,
            self.close,
            self.volume,
            self.quote_volume,
            self.trade_count,
        )


def from_rest_row(symbol: str, timeframe: str, row: object, now_ms: int) -> FinalCandle | None:
    """Binance REST has [open_ms, O,H,L,C,V, close_ms_inclusive, quote_volume, trades,...]."""
    if not isinstance(row, list) or len(row) < 9:
        raise InvalidCandle("REST candle is not the documented 9+ element array")
    open_ms, end_ms = _as_ms(row[0]), _as_ms(row[6]) + 1
    if end_ms > now_ms:
        return None  # Binance REST can include the currently forming candle
    count = row[8]
    if type(count) is not int:
        raise InvalidCandle("REST trade count is not an integer")
    return FinalCandle(
        symbol,
        timeframe,
        open_ms,
        end_ms,
        _as_decimal(row[1]),
        _as_decimal(row[2]),
        _as_decimal(row[3]),
        _as_decimal(row[4]),
        _as_decimal(row[5]),
        _as_decimal(row[7]),
        count,
        "REST",
    )


def from_ws_message(payload: object) -> FinalCandle | None:
    """Decode combined or raw websocket kline, ignoring updates with x=false."""
    if not isinstance(payload, dict):
        raise InvalidCandle("WebSocket message must be an object")
    event: Any = payload.get("data", payload)
    if not isinstance(event, dict):
        raise InvalidCandle("WebSocket event is malformed")
    if event.get("e") != "kline":
        return None  # subscriptions/other events are not candles
    k = event.get("k")
    if not isinstance(k, dict):
        raise InvalidCandle("WebSocket kline object is missing")
    if k.get("x") is not True:
        return None
    symbol, timeframe = event.get("s"), k.get("i")
    if symbol != k.get("s") or not isinstance(symbol, str) or not isinstance(timeframe, str):
        raise InvalidCandle("Symbol/interval mismatch")
    expected_stream = f"{symbol.lower()}@kline_{timeframe}"
    if "stream" in payload and payload["stream"] != expected_stream:
        raise InvalidCandle("WebSocket stream and payload disagree")
    count = k.get("n")
    if type(count) is not int:
        raise InvalidCandle("WebSocket trade count is not an integer")
    return FinalCandle(
        symbol,
        timeframe,
        _as_ms(k.get("t")),
        _as_ms(k.get("T")) + 1,
        _as_decimal(k.get("o")),
        _as_decimal(k.get("h")),
        _as_decimal(k.get("l")),
        _as_decimal(k.get("c")),
        _as_decimal(k.get("v")),
        _as_decimal(k.get("q")),
        count,
        "WS",
    )
