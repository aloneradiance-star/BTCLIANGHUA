"""S01 v0.1: multi-timeframe trend-pullback research hypothesis.

Signals are emitted only from closed 15m candles and the latest 1h candle
whose exclusive close boundary is <= the 15m signal boundary.
"""

from bisect import bisect_right
from dataclasses import dataclass
from decimal import Decimal

from baqs.backtest.engine import EntrySignal
from baqs.indicators.engine import FeatureRow, compute_features
from baqs.market_data.schema import FinalCandle

ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class S01Config:
    adx_min: Decimal = Decimal(23)
    long_rsi_min: Decimal = Decimal(45)
    long_rsi_max: Decimal = Decimal(65)
    short_rsi_min: Decimal = Decimal(35)
    short_rsi_max: Decimal = Decimal(55)
    volume_ratio_min: Decimal = Decimal(1)
    stop_atr_buffer: Decimal = Decimal("0.25")
    strategy_version: str = "S01-0.1"

    def __post_init__(self) -> None:
        numeric = (
            self.adx_min,
            self.long_rsi_min,
            self.long_rsi_max,
            self.short_rsi_min,
            self.short_rsi_max,
            self.volume_ratio_min,
            self.stop_atr_buffer,
        )
        if any(not value.is_finite() for value in numeric):
            raise ValueError("S01 thresholds must be finite Decimal values")
        if (
            self.adx_min <= ZERO
            or not ZERO <= self.long_rsi_min <= self.long_rsi_max <= Decimal(100)
            or not ZERO <= self.short_rsi_min <= self.short_rsi_max <= Decimal(100)
            or self.volume_ratio_min < ZERO
            or self.stop_atr_buffer < ZERO
            or not self.strategy_version
        ):
            raise ValueError("Invalid S01 research parameters")


def latest_completed_feature(
    features: list[FeatureRow], as_of_close_ms: int
) -> FeatureRow | None:
    """Return the most recent feature whose candle is closed by the as-of boundary."""
    close_times = [feature.close_ms for feature in features]
    if any(right <= left for left, right in zip(close_times, close_times[1:], strict=False)):
        raise ValueError("Feature timestamps must be strictly increasing")
    index = bisect_right(close_times, as_of_close_ms) - 1
    return None if index < 0 else features[index]


def evaluate_s01_signal(
    current_bar: FinalCandle,
    previous_bar: FinalCandle,
    current: FeatureRow,
    previous: FeatureRow,
    higher: FeatureRow | None,
    config: S01Config = S01Config(),
) -> EntrySignal | None:
    """Evaluate one closed 15m pullback using only aligned, already-closed features."""
    if (
        current_bar.timeframe != "15m"
        or previous_bar.timeframe != "15m"
        or current.timeframe != "15m"
        or previous.timeframe != "15m"
        or current.symbol != current_bar.symbol
        or previous.symbol != previous_bar.symbol
        or current.close_ms != current_bar.close_ms
        or previous.close_ms != previous_bar.close_ms
        or current_bar.open_ms != previous_bar.open_ms + 900_000
    ):
        raise ValueError("S01 15m candle and feature alignment failed")
    if higher is None:
        return None
    if (
        higher.symbol != current.symbol
        or higher.timeframe != "1h"
        or higher.close_ms > current.close_ms
    ):
        raise ValueError("S01 higher-timeframe feature is future-dated or mismatched")

    required_current = (
        current.ema20,
        current.ema60,
        current.adx14,
        current.atr14,
        current.rsi14,
        current.volume_ratio20,
    )
    required_previous = (previous.ema20,)
    required_higher = (higher.ema20, higher.ema60)
    if any(value is None for value in required_current + required_previous + required_higher):
        return None

    assert current.ema20 is not None
    assert current.ema60 is not None
    assert current.adx14 is not None
    assert current.atr14 is not None
    assert current.rsi14 is not None
    assert current.volume_ratio20 is not None
    assert previous.ema20 is not None
    assert higher.ema20 is not None
    assert higher.ema60 is not None

    if current.atr14 <= ZERO or current.adx14 < config.adx_min:
        return None
    if current.volume_ratio20 < config.volume_ratio_min:
        return None

    long_trend = (
        higher.ema20 > higher.ema60
        and current.ema20 > current.ema60
        and current_bar.close > current.ema60
    )
    long_pullback = (
        previous_bar.close <= previous.ema20
        and current_bar.low <= current.ema20
        and current_bar.close > current.ema20
        and config.long_rsi_min <= current.rsi14 <= config.long_rsi_max
    )
    if long_trend and long_pullback:
        stop = (
            min(current_bar.low, previous_bar.low)
            - current.atr14 * config.stop_atr_buffer
        )
        if ZERO < stop < current_bar.close:
            return EntrySignal(
                current.symbol,
                current.close_ms,
                "LONG",
                stop,
                config.strategy_version,
            )

    short_trend = (
        higher.ema20 < higher.ema60
        and current.ema20 < current.ema60
        and current_bar.close < current.ema60
    )
    short_pullback = (
        previous_bar.close >= previous.ema20
        and current_bar.high >= current.ema20
        and current_bar.close < current.ema20
        and config.short_rsi_min <= current.rsi14 <= config.short_rsi_max
    )
    if short_trend and short_pullback:
        stop = (
            max(current_bar.high, previous_bar.high)
            + current.atr14 * config.stop_atr_buffer
        )
        if stop > current_bar.close:
            return EntrySignal(
                current.symbol,
                current.close_ms,
                "SHORT",
                stop,
                config.strategy_version,
            )
    return None


class S01TrendPullbackStrategy:
    """Generate causal 15m signals with a 1h EMA trend filter."""

    def __init__(self, config: S01Config = S01Config()) -> None:
        self.config = config

    def generate_signals(
        self,
        bars15: list[FinalCandle],
        bars1h: list[FinalCandle],
    ) -> list[EntrySignal]:
        if not bars15 or not bars1h:
            return []
        if bars15[0].timeframe != "15m" or bars1h[0].timeframe != "1h":
            raise ValueError("S01 requires 15m signal bars and 1h trend bars")
        if bars15[0].symbol != bars1h[0].symbol:
            raise ValueError("S01 cannot mix symbols")
        features15 = compute_features(bars15)
        features1h = compute_features(bars1h)
        close_times = [feature.close_ms for feature in features1h]
        if any(right <= left for left, right in zip(close_times, close_times[1:], strict=False)):
            raise ValueError("1h features must be strictly increasing")

        signals: list[EntrySignal] = []
        for index in range(1, len(bars15)):
            current_bar = bars15[index]
            higher_index = bisect_right(close_times, current_bar.close_ms) - 1
            higher = features1h[higher_index] if higher_index >= 0 else None
            signal = evaluate_s01_signal(
                current_bar,
                bars15[index - 1],
                features15[index],
                features15[index - 1],
                higher,
                self.config,
            )
            if signal is not None:
                signals.append(signal)
        return signals
