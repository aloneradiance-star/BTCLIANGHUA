"""Final-candle engine tests: temporal consistency, anti-lookahead, 30d warm-up."""

from dataclasses import replace
from decimal import ROUND_DOWN, Decimal, Inexact, localcontext
from time import perf_counter

import pytest

from baqs.indicators import compute_features
from baqs.market_data.schema import FinalCandle

STEP = 900_000


def series(n: int, *, step: int = STEP, timeframe: str = "15m") -> list[FinalCandle]:
    out = []
    for i in range(n):
        price = Decimal(1000 + ((i * 7) % 53)) + Decimal(i) / Decimal(100)
        out.append(
            FinalCandle(
                symbol="BTCUSDT",
                timeframe=timeframe,
                open_ms=i * step,
                close_ms=(i + 1) * step,
                open=price,
                high=price + 2,
                low=price - 2,
                close=price,
                volume=Decimal(1 + (i % 13)),
                quote_volume=Decimal(1000),
                trade_count=10,
                source="REST",
            )
        )
    return out


def test_no_future_data_leakage_and_deterministic_replay() -> None:
    bars = series(140)
    prefix = compute_features(bars[:100])
    full = compute_features(bars)
    assert prefix == full[:100]
    assert compute_features(bars) == full
    assert full[-1].ema60 is not None
    assert full[-1].adx14 is not None
    assert full[-1].bb_width20 is not None


@pytest.mark.parametrize("trap_inexact", [False, True])
def test_feature_replay_is_independent_of_decimal_context(trap_inexact: bool) -> None:
    bars = series(2903)
    expected = compute_features(bars)
    expected_payloads = [row.as_dict() for row in expected[-3:]]
    with localcontext() as caller:
        caller.prec = 9
        caller.rounding = ROUND_DOWN
        caller.capitals = 0
        caller.traps[Inexact] = trap_inexact
        caller.clear_flags()
        actual = compute_features(bars)
        assert actual == expected
        assert [row.as_dict() for row in actual[-3:]] == expected_payloads
        assert caller.prec == 9
        assert caller.rounding == ROUND_DOWN
        assert caller.capitals == 0
        assert caller.traps[Inexact] is trap_inexact
        assert not caller.flags[Inexact]


def test_warmup_none_and_decimal_safe_serialization() -> None:
    result = compute_features(series(16))
    assert result[0].ema20 is None
    assert result[-1].ema60 is None
    assert result[14].rsi14 is not None
    assert result[14].atr14 is not None
    assert result[-1].adx14 is None
    payload = result[-1].as_dict()
    assert payload["ema20"] is None
    assert isinstance(payload["rsi14"], str)
    assert payload["symbol"] == "BTCUSDT"
    scientific = replace(result[-1], realized_vol20=Decimal("1.234E-30"))
    with localcontext() as caller:
        caller.capitals = 0
        assert scientific.as_dict()["realized_vol20"] == "1.234E-30"


def test_15m_30day_percentiles_wait_for_full_past_window() -> None:
    started = perf_counter()
    features = compute_features(series(2903))
    elapsed = perf_counter() - started
    assert elapsed < 10.0  # generous CI smoke bound for a full 30-day 15m window
    # Bollinger first valid index 19; 30d=2880 previous 15m bars.
    assert features[2898].bb_width_percentile_30d is None
    assert features[2899].bb_width_percentile_30d is not None
    # 20 log returns means first realized vol at index 20.
    assert features[2899].realized_vol_percentile_30d is None
    assert features[2900].realized_vol_percentile_30d is not None
    i = 2902
    previous = [row.bb_width20 for row in features[i - 2880 : i]]
    current = features[i].bb_width20
    assert current is not None
    expected = Decimal(sum(v is not None and v <= current for v in previous)) / 2880
    assert features[i].bb_width_percentile_30d == expected


def test_no_30day_15m_percentile_on_other_timeframes() -> None:
    features = compute_features(series(100, step=60_000, timeframe="1m"))
    assert features[-1].bb_width_percentile_30d is None
    assert features[-1].realized_vol_percentile_30d is None


def test_missing_or_duplicate_bar_is_never_silently_ignored() -> None:
    bars = series(30)
    with pytest.raises(ValueError, match="missing, duplicated"):
        compute_features(bars[:10] + bars[11:])
    with pytest.raises(ValueError, match="missing, duplicated"):
        compute_features(bars[:10] + [bars[9]] + bars[10:])


def test_out_of_order_or_cross_symbol_series_fails() -> None:
    bars = series(40)
    with pytest.raises(ValueError):
        compute_features([bars[1], bars[0]])
    eth = FinalCandle(
        "ETHUSDT",
        "15m",
        bars[1].open_ms,
        bars[1].close_ms,
        bars[1].open,
        bars[1].high,
        bars[1].low,
        bars[1].close,
        bars[1].volume,
        bars[1].quote_volume,
        10,
        "REST",
    )
    with pytest.raises(ValueError, match="Cannot combine"):
        compute_features([bars[0], eth])


def test_empty_input_is_empty_result() -> None:
    assert compute_features([]) == []
