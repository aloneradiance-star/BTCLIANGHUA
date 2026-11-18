"""Independent controlled-market fixtures for research-only six-regime priority."""

from dataclasses import replace
from decimal import Decimal

import pytest

from baqs.indicators.engine import FeatureRow
from baqs.market_data.schema import FinalCandle
from baqs.regime.rules import RegimeUnavailable, classify_candidate

STEP15 = 900_000
STEP1H = 3_600_000


def bar(index: int, *, timeframe: str = "15m") -> FinalCandle:
    step = STEP1H if timeframe == "1h" else STEP15
    return FinalCandle(
        "BTCUSDT", timeframe, index * step, (index + 1) * step,
        Decimal(100), Decimal(101), Decimal(99), Decimal(100),
        Decimal(1), Decimal(100), 10, "REST",
    )


def feature(candle: FinalCandle, **replace_fields: Decimal | None) -> FeatureRow:
    defaults: dict[str, Decimal | None] = {
        "ema20": Decimal(100),
        "ema60": Decimal(100),
        "rsi14": Decimal(50),
        "atr14": Decimal(2),
        "adx14": Decimal(10),
        "plus_di14": Decimal(0),
        "minus_di14": Decimal(0),
        "bb_mid20": Decimal(100),
        "bb_upper20": Decimal(101),
        "bb_lower20": Decimal(99),
        "bb_width20": Decimal(".02"),
        "bb_width_percentile_30d": Decimal(".5"),
        "vwap_utc": Decimal(100),
        "volume_ratio20": Decimal(1),
        "realized_vol20": Decimal(0),
        "realized_vol_percentile_30d": Decimal(".5"),
    }
    defaults.update(replace_fields)
    return FeatureRow(
        candle.symbol, candle.timeframe, candle.open_ms, candle.close_ms,
        **defaults,  # type: ignore[arg-type]
    )


def fixtures() -> tuple[list[FinalCandle], list[FeatureRow], list[FeatureRow]]:
    candles = [bar(i) for i in range(24)]  # 24 * 15m = 6h, aligned to 1h
    feats15 = [feature(candle) for candle in candles]
    higher = [feature(bar(5, timeframe="1h"))]
    return candles, feats15, higher


def test_range_requires_two_confirmed_tests_of_each_edge() -> None:
    candles, feats15, feats1h = fixtures()
    result = classify_candidate(candles, feats15, feats1h)
    assert result.regime == "RANGE"
    assert result.metrics["range_lower_tests"] is not None
    assert int(result.metrics["range_lower_tests"]) >= 2
    assert int(result.metrics["range_upper_tests"]) >= 2


@pytest.mark.parametrize("direction", ["UP", "DOWN"])
def test_ema_aligned_trends_with_confirmed_adx(direction: str) -> None:
    candles, feats15, feats1h = fixtures()
    if direction == "UP":
        feats15[-1] = replace(feats15[-1], ema20=Decimal(120), ema60=Decimal(100),
                              adx14=Decimal(28))
        feats15[-4] = replace(feats15[-4], ema20=Decimal(119))
        feats1h[-1] = replace(feats1h[-1], ema20=Decimal(120), ema60=Decimal(100))
        expected = "BULL_TREND"
    else:
        feats15[-1] = replace(feats15[-1], ema20=Decimal(80), ema60=Decimal(100),
                              adx14=Decimal(28))
        feats15[-4] = replace(feats15[-4], ema20=Decimal(81))
        feats1h[-1] = replace(feats1h[-1], ema20=Decimal(80), ema60=Decimal(100))
        expected = "BEAR_TREND"
    result = classify_candidate(candles, feats15, feats1h)
    assert result.regime == expected


def test_compression_breakout_has_priority_over_trend() -> None:
    candles, feats15, feats1h = fixtures()
    last = candles[-1]
    candles[-1] = FinalCandle(
        last.symbol, last.timeframe, last.open_ms, last.close_ms,
        Decimal(100), Decimal(123), Decimal(99), Decimal(121),
        Decimal(2), Decimal(200), 12, "REST",
    )
    feats15[-1] = replace(
        feats15[-1], volume_ratio20=Decimal("2"), adx14=Decimal(30),
        ema20=Decimal(120), ema60=Decimal(100),
    )
    feats15[-2] = replace(feats15[-2], bb_width_percentile_30d=Decimal(".1"))
    feats15[-4] = replace(feats15[-4], ema20=Decimal(119))
    feats1h[-1] = replace(feats1h[-1], ema20=Decimal(120), ema60=Decimal(100))
    candidate = classify_candidate(candles, feats15, feats1h)
    assert candidate.regime == "BREAKOUT"
    assert "BREAKOUT_UP" in candidate.reasons


def test_high_risk_preempts_breakout_and_cooldown() -> None:
    candles, feats15, higher = fixtures()
    feats15[-1] = replace(
        feats15[-1], realized_vol20=Decimal(".04"),
        realized_vol_percentile_30d=Decimal(".95"),
    )
    result = classify_candidate(candles, feats15, higher)
    assert result.regime == "HIGH_RISK"
    assert "REALIZED_VOL_90TH_PERCENTILE" in result.reasons


def test_spread_unavailable_is_not_fabricated() -> None:
    bars, feats, higher = fixtures()
    assert classify_candidate(bars, feats, higher).metrics["spread_bps"] is None
    with pytest.raises(RegimeUnavailable, match="INCOMPLETE_SPREAD_FEED"):
        classify_candidate(bars, feats, higher, spread_bps=Decimal(8))
    assert classify_candidate(
        bars, feats, higher, spread_bps=Decimal(8),
        median_spread_bps=Decimal(2),
    ).regime == "HIGH_RISK"


def test_no_valid_state_is_transition() -> None:
    bars, feats, higher = fixtures()
    feats[-1] = replace(feats[-1], adx14=Decimal(20))
    assert classify_candidate(bars, feats, higher).regime == "TRANSITION"


def test_missing_30d_reference_or_wrong_higher_clock_fails_closed() -> None:
    bars, feats, higher = fixtures()
    feats[-1] = replace(feats[-1], bb_width_percentile_30d=None)
    with pytest.raises(RegimeUnavailable, match="BB_30D_NOT_READY"):
        classify_candidate(bars, feats, higher)
    feats[-1] = feature(bars[-1])
    future_hour = feature(bar(6, timeframe="1h"))
    with pytest.raises(RegimeUnavailable, match="HIGHER_TIMEFRAME_LOOKAHEAD"):
        classify_candidate(bars, feats, [future_hour])


def test_range_fails_without_both_edges_tested() -> None:
    bars, feats, higher = fixtures()
    # Wide 15m bar has bound; after-touch bounce is absent if all candles close low.
    altered = [
        replace(bar_, close=Decimal(99)) for bar_ in bars
    ]
    candidate = classify_candidate(altered, feats, higher)
    assert candidate.regime == "TRANSITION"
