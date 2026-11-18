"""Independent mathematical references and indicator edge-case tests.

Fixtures use deterministic hand-derivable sequences, not copied outputs from the
production feature implementation. Offline only, no Binance access.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from baqs.indicators.core import (
    bollinger,
    ema,
    past_volume_ratio,
    realized_volatility,
    trailing_percentile,
    utc_session_vwap,
    wilder_atr_adx,
    wilder_rsi,
)
from baqs.market_data.schema import FinalCandle


def linear_bars(n: int, *, timeframe: str = "15m") -> list[FinalCandle]:
    step = 900_000 if timeframe == "15m" else 60_000
    return [
        FinalCandle(
            symbol="BTCUSDT", timeframe=timeframe,
            open_ms=i * step, close_ms=(i + 1) * step,
            open=Decimal(100 + i), high=Decimal(101 + i),
            low=Decimal(99 + i), close=Decimal(100 + i),
            volume=Decimal(i + 1), quote_volume=Decimal(1000),
            trade_count=10, source="REST",
        )
        for i in range(n)
    ]


def test_ema_sma_seed_and_next_recursive_value() -> None:
    prices = [Decimal(i) for i in range(1, 22)]
    values = ema(prices, period=20)
    assert all(value is None for value in values[:19])
    assert values[19] == Decimal("10.5")
    assert values[20] == Decimal("11.5")
    assert ema(prices[:10], 20) == [None] * 10


def test_wilder_rsi_increasing_falling_and_flat_series() -> None:
    increasing = [Decimal(i) for i in range(30)]
    falling = [Decimal(100 - i) for i in range(30)]
    flat = [Decimal(100)] * 30
    assert wilder_rsi(increasing)[:14] == [None] * 14
    assert wilder_rsi(increasing)[14] == Decimal(100)
    assert wilder_rsi(falling)[14] == Decimal(0)
    assert wilder_rsi(flat)[14] == Decimal(50)


def test_wilder_atr_adx_linear_uptrend_known_values() -> None:
    # Constant high-low range=2; consecutive price increase=1;
    # True range=2, +DM=1, -DM=0 for each of 29 changes.
    bars = linear_bars(40)
    atr, adx, plus, minus = wilder_atr_adx(bars, period=14)
    assert atr[13] is None
    assert atr[14] == Decimal(2)
    assert all(value == Decimal(2) for value in atr[14:])
    assert plus[14] == Decimal(50)
    assert minus[14] == Decimal(0)
    assert adx[26] is None
    assert adx[27] == Decimal(100)
    assert adx[39] == Decimal(100)


def test_atr_adx_constant_price_no_division_by_zero() -> None:
    bars = [
        FinalCandle(
            "BTCUSDT", "1m", i * 60_000, (i + 1) * 60_000,
            Decimal(100), Decimal(100), Decimal(100), Decimal(100),
            Decimal(1), Decimal(100), 1, "REST",
        )
        for i in range(40)
    ]
    atr, adx, plus, minus = wilder_atr_adx(bars)
    assert atr[14] == 0
    assert plus[14] == 0
    assert minus[14] == 0
    assert adx[27] == 0


def test_bollinger_population_variance_and_warmup() -> None:
    closes = [Decimal(i) for i in range(1, 22)]
    middle, upper, lower, width = bollinger(closes, 20, 2)
    assert all(value is None for value in width[:19])
    assert middle[19] == Decimal("10.5")
    # For arithmetic progression 1..20: population variance is 33.25.
    reference_std = Decimal("33.25").sqrt()
    assert upper[19] is not None
    assert lower[19] is not None
    assert width[19] is not None
    assert abs(upper[19] - (Decimal("10.5") + 2 * reference_std)) < Decimal("1e-24")
    assert abs(lower[19] - (Decimal("10.5") - 2 * reference_std)) < Decimal("1e-24")
    assert abs(width[19] - 4 * reference_std / Decimal("10.5")) < Decimal("1e-24")


def test_utc_session_vwap_resets_at_midnight() -> None:
    start_ms = int(datetime(2026, 11, 17, 23, 55, tzinfo=UTC).timestamp() * 1000)
    bars = [
        FinalCandle(
            "BTCUSDT", "5m", start_ms + i * 300_000, start_ms + (i + 1) * 300_000,
            Decimal(100 * (i + 1)), Decimal(100 * (i + 1)),
            Decimal(100 * (i + 1)), Decimal(100 * (i + 1)),
            Decimal(1), Decimal(100), 1, "REST",
        )
        for i in range(2)
    ]
    assert utc_session_vwap(bars) == [Decimal(100), Decimal(200)]


def test_vwap_zero_volume_yields_none_until_volume_appears() -> None:
    bars = linear_bars(3)
    zero_bars = [
        FinalCandle(
            bar.symbol, bar.timeframe, bar.open_ms, bar.close_ms,
            bar.open, bar.high, bar.low, bar.close,
            Decimal(0), Decimal(0), 0, "REST",
        )
        for bar in bars
    ]
    assert utc_session_vwap(zero_bars) == [None, None, None]


def test_volume_ratio_excludes_current_candle() -> None:
    bars = linear_bars(21)
    current = bars[20]
    bars[20] = FinalCandle(
        current.symbol, current.timeframe, current.open_ms, current.close_ms,
        current.open, current.high, current.low, current.close,
        Decimal(42), Decimal(5000), current.trade_count, "REST",
    )
    ratios = past_volume_ratio(bars)
    assert ratios[:20] == [None] * 20
    assert ratios[20] == Decimal(4)  # 42 / median(1..20) == 4


def test_trailing_percentile_uses_past_only_and_requires_full_history() -> None:
    values: list[Decimal | None] = [
        None, Decimal(1), Decimal(2), Decimal(0), Decimal(3), Decimal(1),
    ]
    scores = trailing_percentile(values, 2)
    assert scores[2] is None  # first window contains the missing seed
    assert scores[3] == Decimal(0)  # 0 is below past values 1 and 2
    assert scores[4] == Decimal(1)  # 3 is above past values 2 and 0
    assert scores[5] == Decimal("0.5")  # 1 ranks between past 0 and 3


def test_realized_volatility_flat_and_geometric_constant_returns() -> None:
    flat = [Decimal(100)] * 30
    geometric = [Decimal(2) ** i for i in range(30)]
    assert realized_volatility(flat)[19] is None
    assert realized_volatility(flat)[20] == Decimal(0)
    assert realized_volatility(geometric)[20] == Decimal(0)


@pytest.mark.parametrize("operation", [
    lambda: ema([Decimal(1)], 0),
    lambda: wilder_rsi([Decimal(1)], 0),
    lambda: wilder_atr_adx([], 0),
    lambda: bollinger([Decimal(1)], 1),
    lambda: past_volume_ratio([], 0),
    lambda: trailing_percentile([], 0),
    lambda: realized_volatility([Decimal(1)], 1),
])
def test_invalid_window_parameters_fail(operation: object) -> None:
    with pytest.raises(ValueError):
        operation()  # type: ignore[operator]
