"""Frozen-parameter walk-forward fold boundaries, costs and read-only replay tests."""

from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal, Inexact, localcontext

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from baqs.backtest.engine import EntrySignal
from baqs.backtest.walk_forward import (
    CostScenario,
    WalkForwardResearchService,
    fold_windows,
    walk_forward_report_json,
)
from baqs.market_data.funding import FundingStore
from baqs.market_data.repository import CandleStore
from baqs.market_data.schema import FinalCandle, FundingRateRecord
from baqs.models import Candle, FundingCoverageRecord, FundingRate
from baqs.strategies.s01 import S01TrendPullbackStrategy

MINUTE = 60_000
QUARTER = 15 * MINUTE
HOUR = 60 * MINUTE
DAY = 24 * HOUR
START = QUARTER * 100_000
END = START + 3 * DAY


def _candle(
    timeframe: str,
    open_ms: int,
    step: int,
    *,
    high: str = "101",
    close: str = "100",
) -> FinalCandle:
    return FinalCandle(
        "BTCUSDT",
        timeframe,
        open_ms,
        open_ms + step,
        Decimal(100),
        Decimal(high),
        Decimal(98),
        Decimal(close),
        Decimal(10),
        Decimal(1000),
        10,
        "REST",
    )


def _seed_walk_forward(
    engine: Engine, *, funding_records: tuple[FundingRateRecord, ...] = ()
) -> None:
    store = CandleStore(engine)
    bars_1m = [
        _candle(
            "1m",
            START + i * MINUTE,
            MINUTE,
            high="125" if i % 1440 == 1 else "101",
            close="120" if i % 1440 == 1 else "100",
        )
        for i in range(3 * 1440)
    ]
    bars_15m = [
        _candle("15m", START - 2920 * QUARTER + i * QUARTER, QUARTER) for i in range(2920 + 3 * 96)
    ]
    bars_1h = [_candle("1h", START - 120 * HOUR + i * HOUR, HOUR) for i in range(120 + 3 * 24)]
    store.upsert_many(bars_1m)
    store.upsert_many(bars_15m)
    store.upsert_many(bars_1h)
    funding = FundingStore(engine)
    funding.upsert_many(funding_records)
    for index in range(3):
        fold_start = START + index * DAY
        fold_end = fold_start + DAY
        funding.complete_window(
            "BTCUSDT",
            fold_start,
            fold_end,
            [
                record
                for record in funding_records
                if fold_start <= record.funding_time_ms < fold_end
            ],
        )


def _table_counts(engine: Engine) -> tuple[int, int, int]:
    with Session(engine) as session:
        return (
            session.scalar(select(func.count()).select_from(Candle)) or 0,
            session.scalar(select(func.count()).select_from(FundingRate)) or 0,
            session.scalar(select(func.count()).select_from(FundingCoverageRecord)) or 0,
        )


def test_fold_windows_are_equal_adjacent_and_half_open() -> None:
    windows = fold_windows(START, END, 3)
    assert len(windows) == 3
    assert windows[0].start_ms == START
    assert windows[-1].end_ms == END
    assert all(
        left.end_ms == right.start_ms for left, right in zip(windows, windows[1:], strict=False)
    )
    assert all(window.end_ms - window.start_ms == DAY for window in windows)


def test_fold_windows_enforces_90_day_history_cap_including_warmup() -> None:
    with pytest.raises(ValueError, match="90-day history cap"):
        fold_windows(START, START + 60 * DAY, 3)


@pytest.mark.parametrize(
    ("start_ms", "end_ms", "fold_count"),
    [
        (START + 1, END, 3),
        (START, END + MINUTE, 3),
        (START, END, 2),
        (START, END + QUARTER, 3),
    ],
)
def test_fold_windows_reject_unaligned_or_nondivisible_ranges(
    start_ms: int, end_ms: int, fold_count: int
) -> None:
    with pytest.raises(ValueError):
        fold_windows(start_ms, end_ms, fold_count)


def test_cost_scenarios_require_explicit_valid_assumptions() -> None:
    assert CostScenario("base", Decimal("5"), Decimal("2")).name == "base"
    with pytest.raises(ValueError):
        CostScenario("", Decimal("5"), Decimal("2"))
    with pytest.raises(ValueError):
        CostScenario("invalid", Decimal("-1"), Decimal("2"))


def test_cost_scenarios_require_distinct_assumption_pairs(db_engine: Engine) -> None:
    scenarios = [
        CostScenario("base", Decimal("5.0"), Decimal("2")),
        CostScenario("renamed", Decimal("5"), Decimal("2.00")),
    ]
    with pytest.raises(ValueError, match="distinct fee and slippage combinations"):
        WalkForwardResearchService(db_engine, cost_scenarios=scenarios)


def test_walk_forward_cost_sensitivity_is_deterministic_and_read_only(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundary = START + DAY
    first_minute = boundary + MINUTE
    _seed_walk_forward(
        db_engine,
        funding_records=(
            FundingRateRecord("BTCUSDT", boundary, Decimal("0.001"), Decimal(99), "Regular"),
            FundingRateRecord("BTCUSDT", boundary, Decimal("0.003"), Decimal(101), "Special"),
            FundingRateRecord("BTCUSDT", first_minute, Decimal("0.002"), Decimal(103), "Regular"),
        ),
    )

    def signals(self, bars15, bars1h):
        evaluation_start = bars15[2920].open_ms
        evaluation_end = bars15[-1].close_ms
        assert bars15[0].open_ms == evaluation_start - 2920 * QUARTER
        assert bars1h[0].open_ms == evaluation_start - 120 * HOUR
        assert bars1h[-1].close_ms == evaluation_end
        return [
            EntrySignal(
                symbol=bars15[0].symbol,
                signal_time_ms=signal_time_ms,
                side="LONG",
                stop_price=Decimal(90),
                strategy_version=self.config.strategy_version,
            )
            for signal_time_ms in (evaluation_start, evaluation_end)
        ]

    monkeypatch.setattr(S01TrendPullbackStrategy, "generate_signals", signals)
    before = _table_counts(db_engine)
    scenarios = (
        CostScenario("low", Decimal("2"), Decimal("1")),
        CostScenario("stress", Decimal("20"), Decimal("10")),
    )
    service = WalkForwardResearchService(
        db_engine,
        cost_scenarios=scenarios,
        fold_count=3,
        minimum_trades_per_fold=1,
    )
    first = service.run("BTCUSDT", START, END)
    with localcontext() as caller_context:
        caller_context.prec = 9
        caller_context.rounding = ROUND_DOWN
        caller_context.traps[Inexact] = True
        caller_context.clear_flags()
        second = service.run("BTCUSDT", START, END)
        assert not caller_context.flags[Inexact]

    assert first.status == "READY"
    assert first.market_regime_coverage == "NOT_EVALUATED"
    assert first.robustness_assessment == "NOT_MADE"
    assert len(first.cells) == 6
    assert first.aggregate_by_scenario["low"]["trade_counts_by_fold"] == [1, 1, 1]
    assert first.aggregate_by_scenario["stress"]["trade_counts_by_fold"] == [1, 1, 1]
    for cell in first.cells:
        result = cell.report.result
        assert result is not None
        assert result.trades[0].entry_time_ms == START + (cell.fold_index - 1) * DAY
        funding = cell.report.data_provenance["funding"]
        assert isinstance(funding, dict)
        if cell.fold_index == 2:
            assert funding["expected_timestamps_ms"] == [boundary, boundary, first_minute]
            trade = result.trades[0]
            assert trade.funding_cost == trade.quantity * Decimal(103) * Decimal("0.002")
        else:
            assert funding["expected_timestamps_ms"] == []
            assert result.trades[0].funding_cost == 0
        if cell.fold_index < first.fold_count:
            assert not result.skipped_signals
        else:
            assert len(result.skipped_signals) == 1
            assert result.skipped_signals[0].signal_time_ms == END
            assert result.skipped_signals[0].reason == "NO_NEXT_1M_EXECUTION_BAR"
    for scenario, aggregate in first.aggregate_by_scenario.items():
        fold_results = [
            cell.report.result
            for cell in first.cells
            if cell.cost_scenario == scenario and cell.report.result is not None
        ]
        assert aggregate["net_pnl"] == sum(result.metrics.net_pnl for result in fold_results)
        assert aggregate["skipped_signal_count"] == 1
        assert aggregate["skipped_signal_counts_by_fold"] == [0, 0, 1]
        assert aggregate["skipped_signal_counts_by_reason"] == {"NO_NEXT_1M_EXECUTION_BAR": 1}
    assert first.aggregate_by_scenario["low"]["robustness_assessment"] == "NOT_MADE"
    low_net = first.aggregate_by_scenario["low"]["net_pnl"]
    stress_net = first.aggregate_by_scenario["stress"]["net_pnl"]
    assert low_net > stress_net
    assert walk_forward_report_json(first) == walk_forward_report_json(second)
    assert _table_counts(db_engine) == before


def test_walk_forward_refuses_to_call_too_few_trades_robust(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_walk_forward(db_engine)

    def no_signals(self, bars15, bars1h):
        return []

    monkeypatch.setattr(S01TrendPullbackStrategy, "generate_signals", no_signals)
    report = WalkForwardResearchService(
        db_engine,
        cost_scenarios=[
            CostScenario("base", Decimal("5"), Decimal("2")),
            CostScenario("stress", Decimal("20"), Decimal("10")),
        ],
        fold_count=3,
        minimum_trades_per_fold=1,
    ).run("BTCUSDT", START, END)
    assert report.status == "INSUFFICIENT_SAMPLE"
    assert report.reason == "MINIMUM_TRADES_PER_FOLD_NOT_MET"
    assert report.aggregate_by_scenario["base"]["trade_count"] == 0


def test_walk_forward_fails_closed_without_each_fold_funding_manifest(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_walk_forward(db_engine)
    with Session(db_engine) as session:
        session.execute(
            delete(FundingCoverageRecord).where(
                FundingCoverageRecord.start_time
                == datetime.fromtimestamp((START + DAY) / 1000, tz=UTC)
            )
        )
        session.commit()

    def no_signals(self, bars15, bars1h):
        return []

    monkeypatch.setattr(S01TrendPullbackStrategy, "generate_signals", no_signals)
    report = WalkForwardResearchService(
        db_engine,
        cost_scenarios=[
            CostScenario("base", Decimal("5"), Decimal("2")),
            CostScenario("stress", Decimal("20"), Decimal("10")),
        ],
        fold_count=3,
        minimum_trades_per_fold=1,
    ).run("BTCUSDT", START, END)
    assert report.status == "UNAVAILABLE"
    assert report.cells[-1].fold_index == 2
    assert not report.aggregate_by_scenario
    assert "no complete coverage manifest" in (report.reason or "")
