"""S01 rule tests with independently constructed, aligned feature rows."""

from dataclasses import replace
from decimal import Decimal

import pytest

from baqs.indicators.engine import FeatureRow
from baqs.market_data.schema import FinalCandle
from baqs.strategies.s01 import (
    S01Config,
    S01TrendPullbackStrategy,
    evaluate_s01_signal,
    latest_completed_feature,
)

BASE = 2_700_000


def candle(
    timeframe: str,
    open_ms: int,
    *,
    open_: str,
    high: str,
    low: str,
    close: str,
) -> FinalCandle:
    step = 900_000 if timeframe == "15m" else 3_600_000
    return FinalCandle(
        "BTCUSDT",
        timeframe,
        open_ms,
        open_ms + step,
        Decimal(open_),
        Decimal(high),
        Decimal(low),
        Decimal(close),
        Decimal(10),
        Decimal(1000),
        10,
        "REST",
    )


def feature(
    timeframe: str,
    open_ms: int,
    *,
    ema20: str = "105",
    ema60: str = "100",
    rsi: str = "55",
    adx: str = "28",
    atr: str = "2",
    volume_ratio: str = "1.2",
) -> FeatureRow:
    step = 900_000 if timeframe == "15m" else 3_600_000
    return FeatureRow(
        symbol="BTCUSDT",
        timeframe=timeframe,
        open_ms=open_ms,
        close_ms=open_ms + step,
        ema20=Decimal(ema20),
        ema60=Decimal(ema60),
        rsi14=Decimal(rsi),
        atr14=Decimal(atr),
        adx14=Decimal(adx),
        plus_di14=Decimal(20),
        minus_di14=Decimal(10),
        bb_mid20=Decimal(100),
        bb_upper20=Decimal(110),
        bb_lower20=Decimal(90),
        bb_width20=Decimal(".2"),
        bb_width_percentile_30d=Decimal(".5"),
        vwap_utc=Decimal(100),
        volume_ratio20=Decimal(volume_ratio),
        realized_vol20=Decimal(".01"),
        realized_vol_percentile_30d=Decimal(".5"),
    )


def test_s01_long_trend_pullback_reclaim_signal() -> None:
    previous_bar = candle("15m", BASE, open_="105", high="107", low="103", close="104")
    current_bar = candle("15m", BASE + 900_000, open_="105", high="112", low="104", close="110")
    previous = feature("15m", BASE)
    current = feature("15m", BASE + 900_000)
    higher = feature("1h", 0, ema20="120", ema60="110")
    signal = evaluate_s01_signal(current_bar, previous_bar, current, previous, higher)
    assert signal is not None
    assert signal.side == "LONG"
    assert signal.signal_time_ms == current_bar.close_ms
    assert signal.stop_price == Decimal("102.50")


def test_s01_short_trend_pullback_rejection_signal() -> None:
    previous_bar = candle("15m", BASE, open_="96", high="98", low="94", close="97")
    current_bar = candle("15m", BASE + 900_000, open_="95", high="96", low="88", close="90")
    previous = feature("15m", BASE, ema20="95", ema60="100", rsi="45")
    current = feature("15m", BASE + 900_000, ema20="95", ema60="100", rsi="45")
    higher = feature("1h", 0, ema20="90", ema60="100")
    signal = evaluate_s01_signal(current_bar, previous_bar, current, previous, higher)
    assert signal is not None
    assert signal.side == "SHORT"
    assert signal.stop_price == Decimal("98.50")


@pytest.mark.parametrize(
    "current,higher",
    [
        (feature("15m", BASE + 900_000, adx="15"), feature("1h", 0)),
        (feature("15m", BASE + 900_000, volume_ratio=".5"), feature("1h", 0)),
        (feature("15m", BASE + 900_000, rsi="80"), feature("1h", 0)),
        (
            feature("15m", BASE + 900_000),
            feature("1h", 0, ema20="90", ema60="100"),
        ),
    ],
)
def test_s01_filters_weak_or_misaligned_setups(current: FeatureRow, higher: FeatureRow) -> None:
    previous_bar = candle("15m", BASE, open_="105", high="107", low="103", close="104")
    current_bar = candle("15m", BASE + 900_000, open_="105", high="112", low="104", close="110")
    previous = feature("15m", BASE)
    assert evaluate_s01_signal(current_bar, previous_bar, current, previous, higher) is None


def test_future_higher_timeframe_feature_is_rejected() -> None:
    previous_bar = candle("15m", BASE, open_="105", high="107", low="103", close="104")
    current_bar = candle("15m", BASE + 900_000, open_="105", high="112", low="104", close="110")
    with pytest.raises(ValueError, match="future-dated"):
        evaluate_s01_signal(
            current_bar,
            previous_bar,
            feature("15m", BASE + 900_000),
            feature("15m", BASE),
            feature("1h", BASE + 900_000),
        )


def test_latest_completed_feature_never_uses_future_hour() -> None:
    older = feature("1h", 0)
    future = feature("1h", 3_600_000)
    assert latest_completed_feature([older, future], 3_600_000) == older
    assert latest_completed_feature([older, future], 7_200_000) == future


@pytest.mark.parametrize("wrong_feature", ["current", "previous", "higher"])
def test_s01_rejects_features_without_exact_candle_boundaries(wrong_feature: str) -> None:
    previous_bar = candle("15m", BASE, open_="105", high="107", low="103", close="104")
    current_bar = candle("15m", BASE + 900_000, open_="105", high="112", low="104", close="110")
    features = {
        "current": feature("15m", BASE + 900_000),
        "previous": feature("15m", BASE),
        "higher": feature("1h", 0, ema20="120", ema60="110"),
    }
    invalid = features[wrong_feature]
    features[wrong_feature] = replace(
        invalid,
        open_ms=invalid.open_ms + 60_000,
        close_ms=invalid.close_ms + (60_000 if wrong_feature == "higher" else 0),
    )
    with pytest.raises(ValueError, match="alignment"):
        evaluate_s01_signal(
            current_bar,
            previous_bar,
            features["current"],
            features["previous"],
            features["higher"],
        )


def test_missing_feature_warmup_returns_no_signal() -> None:
    previous_bar = candle("15m", BASE, open_="105", high="107", low="103", close="104")
    current_bar = candle("15m", BASE + 900_000, open_="105", high="112", low="104", close="110")
    current = replace(feature("15m", BASE + 900_000), atr14=None)
    signal = evaluate_s01_signal(
        current_bar,
        previous_bar,
        current,
        feature("15m", BASE),
        feature("1h", 0, ema20="120", ema60="110"),
    )
    assert signal is None


def test_invalid_strategy_parameters_rejected() -> None:
    with pytest.raises(ValueError):
        S01Config(long_rsi_min=Decimal(70), long_rsi_max=Decimal(60))


def test_s01_rejects_mixed_symbol_candles_and_features() -> None:
    previous_bar = replace(
        candle("15m", BASE, open_="105", high="107", low="103", close="104"),
        symbol="ETHUSDT",
    )
    previous = replace(feature("15m", BASE), symbol="ETHUSDT")
    current_bar = candle("15m", BASE + 900_000, open_="105", high="112", low="104", close="110")
    with pytest.raises(ValueError, match="alignment"):
        evaluate_s01_signal(
            current_bar,
            previous_bar,
            feature("15m", BASE + 900_000),
            previous,
            feature("1h", 0, ema20="120", ema60="110"),
        )


def test_s01_rejects_stale_higher_timeframe_context() -> None:
    previous_bar = candle("15m", 7_200_000, open_="105", high="107", low="103", close="104")
    current_bar = candle("15m", 8_100_000, open_="105", high="112", low="104", close="110")
    with pytest.raises(ValueError, match="stale"):
        evaluate_s01_signal(
            current_bar,
            previous_bar,
            feature("15m", 8_100_000),
            feature("15m", 7_200_000),
            feature("1h", 0, ema20="120", ema60="110"),
        )


def test_s01_missing_history_is_not_reported_as_no_signal() -> None:
    with pytest.raises(ValueError, match="nonempty"):
        S01TrendPullbackStrategy().generate_signals([], [])
