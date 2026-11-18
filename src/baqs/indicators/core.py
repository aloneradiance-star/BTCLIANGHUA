"""Pure, deterministic technical indicators with explicit warm-up definitions.

No network calls, DB access, current unfinished candles, exchange account, or trade signals.
The first Wilder ATR/RSI index is period (requires prior close); first ADX index
is (2 * period - 1). EMA is SMA seeded at period - 1. Standard deviation is
population (ddof=0). All calculations preserve Decimal inputs and outputs.
"""

from datetime import UTC, datetime
from decimal import Decimal, localcontext
from typing import Sequence

from baqs.market_data.schema import FinalCandle

ZERO = Decimal(0)
ONE_HUNDRED = Decimal(100)


def ema(prices: Sequence[Decimal], period: int) -> list[Decimal | None]:
    """EMA with an exact SMA seed; None until period complete closes exist."""
    if period < 1:
        raise ValueError("EMA period must be positive")
    result: list[Decimal | None] = [None] * len(prices)
    if len(prices) < period:
        return result
    p = Decimal(period)
    alpha = Decimal(2) / (p + 1)
    previous = sum(prices[:period], ZERO) / p
    result[period - 1] = previous
    for i in range(period, len(prices)):
        previous = previous + alpha * (prices[i] - previous)
        result[i] = previous
    return result


def _rsi_value(average_up: Decimal, average_down: Decimal) -> Decimal:
    if average_down == 0:
        return Decimal(50) if average_up == 0 else ONE_HUNDRED
    if average_up == 0:
        return ZERO
    return ONE_HUNDRED - ONE_HUNDRED / (1 + average_up / average_down)


def wilder_rsi(prices: Sequence[Decimal], period: int = 14) -> list[Decimal | None]:
    """Wilder RSI: 14 price differences require 15 closed candles."""
    if period < 1:
        raise ValueError("RSI period must be positive")
    result: list[Decimal | None] = [None] * len(prices)
    if len(prices) <= period:
        return result
    gains = [
        max(prices[i] - prices[i - 1], ZERO)
        for i in range(1, period + 1)
    ]
    losses = [
        max(prices[i - 1] - prices[i], ZERO)
        for i in range(1, period + 1)
    ]
    p = Decimal(period)
    average_up = sum(gains, ZERO) / p
    average_down = sum(losses, ZERO) / p
    result[period] = _rsi_value(average_up, average_down)
    for i in range(period + 1, len(prices)):
        change = prices[i] - prices[i - 1]
        gain = max(change, ZERO)
        loss = max(-change, ZERO)
        average_up = (average_up * (p - 1) + gain) / p
        average_down = (average_down * (p - 1) + loss) / p
        result[i] = _rsi_value(average_up, average_down)
    return result


def wilder_atr_adx(
    bars: Sequence[FinalCandle], period: int = 14,
) -> tuple[
    list[Decimal | None],
    list[Decimal | None],
    list[Decimal | None],
    list[Decimal | None],
]:
    """Wilder ATR/ADX and +DI/-DI; no first-bar synthetic prior close.

    Initial ATR at index period is mean(TR[1..period]).
    Initial directional movement sums and TR sum cover the same differences.
    DX first available at index period; ADX first available at index 2*period-1.
    """
    if period < 1:
        raise ValueError("ATR/ADX period must be positive")
    n = len(bars)
    atr: list[Decimal | None] = [None] * n
    adx: list[Decimal | None] = [None] * n
    plus_di: list[Decimal | None] = [None] * n
    minus_di: list[Decimal | None] = [None] * n
    if n <= period:
        return atr, adx, plus_di, minus_di
    true_ranges: list[Decimal] = [ZERO] * n
    positive_dm: list[Decimal] = [ZERO] * n
    negative_dm: list[Decimal] = [ZERO] * n
    for i in range(1, n):
        current, previous = bars[i], bars[i - 1]
        true_ranges[i] = max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        upward = current.high - previous.high
        downward = previous.low - current.low
        positive_dm[i] = upward if upward > downward and upward > 0 else ZERO
        negative_dm[i] = downward if downward > upward and downward > 0 else ZERO

    p = Decimal(period)
    tr_sum = sum(true_ranges[1:period + 1], ZERO)
    plus_sum = sum(positive_dm[1:period + 1], ZERO)
    minus_sum = sum(negative_dm[1:period + 1], ZERO)
    dx_values: list[Decimal] = []

    for i in range(period, n):
        if i > period:
            tr_sum = tr_sum - tr_sum / p + true_ranges[i]
            plus_sum = plus_sum - plus_sum / p + positive_dm[i]
            minus_sum = minus_sum - minus_sum / p + negative_dm[i]
        atr[i] = tr_sum / p
        if tr_sum == 0:
            plus = minus = ZERO
        else:
            plus = ONE_HUNDRED * plus_sum / tr_sum
            minus = ONE_HUNDRED * minus_sum / tr_sum
        plus_di[i], minus_di[i] = plus, minus
        total_di = plus + minus
        dx = (
            ONE_HUNDRED * abs(plus - minus) / total_di
            if total_di != 0 else ZERO
        )
        dx_values.append(dx)
        if len(dx_values) == period:
            adx[i] = sum(dx_values, ZERO) / p
        elif len(dx_values) > period:
            previous_adx = adx[i - 1]
            assert previous_adx is not None
            adx[i] = (previous_adx * (p - 1) + dx) / p

    return atr, adx, plus_di, minus_di


def bollinger(
    prices: Sequence[Decimal], period: int = 20, deviations: int = 2,
) -> tuple[
    list[Decimal | None],
    list[Decimal | None],
    list[Decimal | None],
    list[Decimal | None],
]:
    """SMA ± deviations * population standard deviation; width=(upper-lower)/SMA."""
    if period < 2 or deviations < 0:
        raise ValueError("Bollinger period >= 2 and deviation >= 0 are required")
    n = len(prices)
    middle: list[Decimal | None] = [None] * n
    upper: list[Decimal | None] = [None] * n
    lower: list[Decimal | None] = [None] * n
    width: list[Decimal | None] = [None] * n
    p = Decimal(period)
    mult = Decimal(deviations)
    for i in range(period - 1, n):
        subset = prices[i - period + 1:i + 1]
        with localcontext() as context:
            context.prec = 40
            average = sum(subset, ZERO) / p
            variance = sum(((x - average) ** 2 for x in subset), ZERO) / p
            stdev = variance.sqrt()
            up = average + mult * stdev
            low = average - mult * stdev
            bw = (up - low) / average if average != 0 else None
        middle[i], upper[i], lower[i], width[i] = average, up, low, bw
    return middle, upper, lower, width


def utc_session_vwap(bars: Sequence[FinalCandle]) -> list[Decimal | None]:
    """HLC3 x base volume, resetting at UTC midnight, no look-ahead."""
    output: list[Decimal | None] = []
    current_day = None
    pv_sum, volume_sum = ZERO, ZERO
    for bar in bars:
        bar_day = datetime.fromtimestamp(bar.open_ms / 1000, tz=UTC).date()
        if bar_day != current_day:
            current_day = bar_day
            pv_sum = volume_sum = ZERO
        typical_price = (bar.high + bar.low + bar.close) / Decimal(3)
        pv_sum += typical_price * bar.volume
        volume_sum += bar.volume
        output.append(pv_sum / volume_sum if volume_sum > 0 else None)
    return output


def past_volume_ratio(
    bars: Sequence[FinalCandle], period: int = 20,
) -> list[Decimal | None]:
    """Current closed-bar base volume over median of preceding period bars only."""
    if period < 1:
        raise ValueError("Volume median window must be positive")
    output: list[Decimal | None] = [None] * len(bars)
    for i in range(period, len(bars)):
        previous_volumes = sorted(bar.volume for bar in bars[i - period:i])
        mid = period // 2
        historical_median = (
            previous_volumes[mid]
            if period % 2 else (previous_volumes[mid - 1] + previous_volumes[mid]) / 2
        )
        if historical_median > 0:
            output[i] = bars[i].volume / historical_median
    return output


def realized_volatility(
    closes: Sequence[Decimal], period: int = 20,
) -> list[Decimal | None]:
    """Population stdev of preceding-inclusive 'period' log returns; not annualized."""
    if period < 2:
        raise ValueError("Realized volatility window must be >= 2")
    result: list[Decimal | None] = [None] * len(closes)
    if len(closes) <= period:
        return result
    with localcontext() as context:
        context.prec = 40
        log_returns = [
            (closes[i] / closes[i - 1]).ln()
            for i in range(1, len(closes))
        ]
        p = Decimal(period)
        for i in range(period, len(closes)):
            window = log_returns[i - period:i]
            average = sum(window, ZERO) / p
            variance = sum(((value - average) ** 2 for value in window), ZERO) / p
            result[i] = variance.sqrt()
    return result


def trailing_percentile(
    values: Sequence[Decimal | None], lookback: int,
) -> list[Decimal | None]:
    """Inclusive rank of current value against EXACTLY lookback past valid values.

    Result is an empirical percentile in [0,1], not a calibrated trade win probability.
    Never includes the current value in the reference history.
    """
    if lookback < 1:
        raise ValueError("Percentile window must be positive")
    result: list[Decimal | None] = [None] * len(values)
    for i in range(lookback, len(values)):
        current = values[i]
        previous = values[i - lookback:i]
        if current is None or any(x is None for x in previous):
            continue
        assert all(x is not None for x in previous)
        count = sum(1 for value in previous if value is not None and value <= current)
        result[i] = Decimal(count) / Decimal(lookback)
    return result
