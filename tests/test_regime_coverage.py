"""Causal coverage from the actual Phase 1 candidate rules."""

from dataclasses import replace
from decimal import Decimal

from baqs.backtest.regime_coverage import measure_regime_candidates
from baqs.market_data.schema import INTERVAL_MS, FinalCandle

QUARTER = INTERVAL_MS["15m"]
HOUR = INTERVAL_MS["1h"]
ANCHOR = 100_000 * QUARTER
START = ANCHOR + QUARTER
END = START + 2 * QUARTER


def _bar(timeframe: str, open_ms: int) -> FinalCandle:
    return FinalCandle(
        symbol="BTCUSDT",
        timeframe=timeframe,
        open_ms=open_ms,
        close_ms=open_ms + INTERVAL_MS[timeframe],
        open=Decimal(100),
        high=Decimal(101),
        low=Decimal(99),
        close=Decimal(100),
        volume=Decimal(10),
        quote_volume=Decimal(1000),
        trade_count=10,
        source="REST",
    )


def _15m_bars() -> list[FinalCandle]:
    return [_bar("15m", timestamp) for timestamp in range(START - 2920 * QUARTER, END, QUARTER)]


def test_counts_only_candidates_already_available_within_the_fold() -> None:
    bars15 = _15m_bars()
    hours = [_bar("1h", timestamp) for timestamp in range(ANCHOR - 120 * HOUR, ANCHOR, HOUR)]
    baseline = measure_regime_candidates(bars15[:-1], hours, START, END)
    assert baseline.expected_intervals == 2
    assert baseline.counts_by_regime["RANGE"] == 2
    assert baseline.unavailable_intervals == 0
    assert len(baseline.missing_regimes) == 5

    final_bar = replace(bars15[-1], high=Decimal(150), close=Decimal(140))
    future_hour = replace(_bar("1h", ANCHOR), high=Decimal(150), close=Decimal(130))
    assert (
        measure_regime_candidates(bars15[:-1] + [final_bar], hours + [future_hour], START, END)
        == baseline
    )


def test_missing_hourly_context_does_not_fabricate_a_regime() -> None:
    future_hour = _bar("1h", ANCHOR)
    coverage = measure_regime_candidates(_15m_bars(), [future_hour], START, END)
    assert coverage.unavailable_intervals == coverage.expected_intervals == 2
    assert sum(coverage.counts_by_regime.values()) == 0
    assert len(coverage.missing_regimes) == 6
