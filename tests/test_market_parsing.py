"""Strict Binance public REST/WS finalization tests with no network."""

from decimal import Decimal

import pytest

from baqs.market_data.schema import InvalidCandle, from_rest_row, from_ws_message


def rest_row(start: int = 0) -> list[object]:
    return [
        start,
        "95000.00",
        "95005",
        "94990",
        "95001",
        "1.25",
        start + 59_999,
        "100",
        12,
        "0.5",
        "50",
        "0",
    ]


def ws_event(start: int = 0, *, closed: bool = True) -> dict[str, object]:
    return {
        "stream": "btcusdt@kline_1m",
        "data": {
            "e": "kline",
            "E": start + 59_999,
            "s": "BTCUSDT",
            "k": {
                "t": start,
                "T": start + 59_999,
                "s": "BTCUSDT",
                "i": "1m",
                "o": "95000.00",
                "h": "95005",
                "l": "94990",
                "c": "95001",
                "v": "1.25",
                "q": "100",
                "n": 12,
                "x": closed,
            },
        },
    }


def test_rest_parses_closed_candle_and_exact_prices() -> None:
    bar = from_rest_row("BTCUSDT", "1m", rest_row(), 60_000)
    assert bar is not None
    assert bar.open == Decimal("95000.00")
    assert bar.close_ms == 60_000
    assert bar.source == "REST"
    assert bar.open_time.tzinfo is not None


def test_rest_does_not_accept_unclosed_candle() -> None:
    assert from_rest_row("BTCUSDT", "1m", rest_row(), 59_999) is None


def test_websocket_ignores_nonfinal_updates() -> None:
    assert from_ws_message(ws_event(closed=False)) is None
    assert from_ws_message({"result": None, "id": 1}) is None


def test_websocket_final_matches_rest() -> None:
    bar = from_ws_message(ws_event())
    assert bar is not None
    assert bar.source == "WS"
    assert bar.close_ms == 60_000
    assert bar.volume == Decimal("1.25")


@pytest.mark.parametrize(
    "field,value",
    [
        ("stream", "ethusdt@kline_1m"),
        ("data", {"e": "kline", "k": {}}),
    ],
)
def test_mismatched_or_invalid_websocket_rejected(field: str, value: object) -> None:
    payload = ws_event()
    payload[field] = value
    with pytest.raises(InvalidCandle):
        from_ws_message(payload)


def test_invalid_ohlc_rejected() -> None:
    row = rest_row()
    row[2] = "1"
    with pytest.raises(InvalidCandle):
        from_rest_row("BTCUSDT", "1m", row, 60_000)


def test_misaligned_open_rejected() -> None:
    with pytest.raises(InvalidCandle):
        from_rest_row("BTCUSDT", "1m", rest_row(42), 60_042)


def test_nonfinite_price_rejected() -> None:
    row = rest_row()
    row[1] = "NaN"
    with pytest.raises(InvalidCandle):
        from_rest_row("BTCUSDT", "1m", row, 60_000)


def test_spoofed_market_and_bool_timestamp_rejected() -> None:
    with pytest.raises(InvalidCandle):
        from_rest_row("SOLUSDT", "1m", rest_row(), 60_000)
    row = rest_row()
    row[0] = True
    with pytest.raises(InvalidCandle):
        from_rest_row("BTCUSDT", "1m", row, 60_000)
