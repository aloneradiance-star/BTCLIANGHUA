"""Deterministic accounting, event ordering and fail-closed backtest tests."""

from decimal import Decimal

import pytest

from baqs.backtest import (
    BacktestConfig,
    BacktestEngine,
    BacktestUnavailable,
    CostModel,
    EntrySignal,
    FundingCoverage,
    FundingEvent,
)
from baqs.market_data.schema import FinalCandle

START = 900_000


def bar(
    index: int,
    *,
    open_: str = "100",
    high: str = "101",
    low: str = "99",
    close: str = "100",
) -> FinalCandle:
    open_ms = START + index * 60_000
    return FinalCandle(
        "BTCUSDT",
        "1m",
        open_ms,
        open_ms + 60_000,
        Decimal(open_),
        Decimal(high),
        Decimal(low),
        Decimal(close),
        Decimal(1),
        Decimal(100),
        10,
        "REST",
    )


def signal(side: str = "LONG", stop: str = "90", timestamp: int = START) -> EntrySignal:
    return EntrySignal("BTCUSDT", timestamp, side, Decimal(stop), "S01-0.1")  # type: ignore[arg-type]


def run(
    bars: list[FinalCandle],
    signals: list[EntrySignal],
    *,
    events: list[FundingEvent] | None = None,
    expected: tuple[int, ...] = (),
    config: BacktestConfig | None = None,
):
    coverage = FundingCoverage(
        bars[0].open_ms,
        bars[-1].close_ms,
        expected,
        True,
    )
    return BacktestEngine().run(
        bars,
        signals,
        [] if events is None else events,
        coverage,
        CostModel(Decimal("10"), Decimal("5")),
        BacktestConfig() if config is None else config,
    )


def test_long_trade_uses_next_minute_open_and_all_explicit_costs() -> None:
    bars = [
        bar(0),
        bar(1, open_="100", high="125", low="99", close="120"),
        bar(2, open_="120", high="121", low="119", close="120"),
    ]
    funding_time = START + 60_000
    funding = FundingEvent("BTCUSDT", funding_time, Decimal(".001"), Decimal(100))
    result = run(
        bars,
        [signal()],
        events=[funding],
        expected=(funding_time,),
    )
    trade = result.trades[0]
    assert trade.entry_time_ms == START
    assert trade.entry_reference_price == Decimal(100)
    assert trade.entry_fill_price > trade.entry_reference_price
    assert trade.exit_reason == "TAKE_PROFIT"
    assert trade.fees > 0
    assert trade.funding_cost > 0
    assert trade.slippage_cost > 0
    assert result.assumptions["taker_fee_bps"] == "10"
    assert result.assumptions["slippage_bps"] == "5"
    assert result.assumptions["risk_fraction"] == "0.005"
    assert result.assumptions["funding_event_count"] == "1"
    assert trade.net_pnl == trade.gross_pnl - trade.fees - trade.funding_cost
    reconciliation_error = abs(
        result.metrics.final_equity - result.metrics.initial_equity - trade.net_pnl
    )
    assert reconciliation_error < Decimal("1e-20")


def test_short_trade_pays_adverse_slippage_and_can_profit() -> None:
    bars = [
        bar(0),
        bar(1, open_="100", high="101", low="78", close="80"),
        bar(2, open_="80", high="81", low="79", close="80"),
    ]
    result = run(bars, [signal("SHORT", "110")])
    trade = result.trades[0]
    assert trade.side == "SHORT"
    assert trade.entry_fill_price < trade.entry_reference_price
    assert trade.exit_fill_price > trade.exit_reference_price
    assert trade.exit_reason == "TAKE_PROFIT"
    assert trade.net_pnl > 0


def test_same_minute_stop_and_target_uses_adverse_stop_first() -> None:
    bars = [
        bar(0),
        bar(1, open_="100", high="130", low="80", close="110"),
        bar(2),
    ]
    trade = run(bars, [signal()]).trades[0]
    assert trade.exit_reason == "AMBIGUOUS_STOP_FIRST"
    assert trade.exit_reference_price == Decimal(90)
    assert trade.net_pnl < 0


def test_gap_through_stop_fills_at_worse_open_not_stop_price() -> None:
    bars = [
        bar(0),
        bar(1, open_="80", high="82", low="78", close="81"),
        bar(2),
    ]
    trade = run(bars, [signal()]).trades[0]
    assert trade.exit_reason == "STOP_GAP"
    assert trade.exit_reference_price == Decimal(80)
    assert trade.exit_fill_price < Decimal(80)


def test_funding_manifest_must_match_exactly() -> None:
    bars = [bar(0), bar(1), bar(2)]
    with pytest.raises(BacktestUnavailable, match="FUNDING_EVENTS"):
        run(
            bars,
            [signal()],
            expected=(START + 60_000,),
        )


def test_missing_execution_minute_fails_closed() -> None:
    bars = [bar(0), bar(2)]
    with pytest.raises(BacktestUnavailable, match="GAP"):
        run(bars, [signal()])


def test_gross_notional_is_capped_at_one_x_equity() -> None:
    bars = [bar(0), bar(1), bar(2)]
    config = BacktestConfig(
        initial_equity=Decimal(1000),
        risk_fraction=Decimal(".5"),
        max_notional_fraction=Decimal(".2"),
        reward_risk=Decimal(2),
    )
    trade = run(bars, [signal("LONG", "99")], config=config).trades[0]
    assert trade.entry_fill_price * trade.quantity <= Decimal(200)


def test_open_trade_is_closed_at_end_of_data() -> None:
    bars = [bar(0), bar(1), bar(2)]
    result = run(bars, [signal()])
    trade = result.trades[0]
    assert trade.exit_reason == "END_OF_DATA"
    assert trade.exit_time_precision == "BAR_CLOSE"
    assert trade.exit_bar_close_ms == bars[-1].close_ms
    assert result.equity_curve[-1].unrealized_pnl == 0


def test_duplicate_signals_at_one_boundary_are_rejected() -> None:
    bars = [bar(0), bar(1), bar(2)]
    with pytest.raises(BacktestUnavailable, match="DUPLICATE_SIGNAL"):
        run(bars, [signal(), signal()])


def test_signal_at_final_exclusive_boundary_is_not_executable() -> None:
    bars = [bar(i) for i in range(15)]
    # A signal at end_ms has no following execution minute and must not be filled.
    result = run(bars, [signal(timestamp=bars[-1].close_ms)])
    assert result.trades == ()
    assert len(result.skipped_signals) == 1
    assert result.skipped_signals[0].reason == "NO_NEXT_1M_EXECUTION_BAR"


def test_repeat_replay_is_deterministic() -> None:
    bars = [
        bar(0),
        bar(1, high="125", low="99", close="120"),
        bar(2),
    ]
    first = run(bars, [signal()])
    second = run(bars, [signal()])
    assert first == second
    assert first.metrics.trade_count == 1
    assert first.metrics.exposure_fraction > 0
    assert first.metrics.max_drawdown >= 0


def test_cost_and_risk_inputs_fail_closed() -> None:
    with pytest.raises(ValueError):
        CostModel(Decimal(-1), Decimal(1))
    with pytest.raises(ValueError):
        BacktestConfig(max_notional_fraction=Decimal("1.01"))
    with pytest.raises(ValueError):
        FundingCoverage(START, START + 120_000, (START + 60_000, START + 60_000), True)


def test_funding_manifest_must_match_exact_backtest_window() -> None:
    bars = [bar(0), bar(1), bar(2)]
    coverage = FundingCoverage(
        START - 60_000,
        bars[-1].close_ms,
        (),
        True,
    )
    with pytest.raises(BacktestUnavailable, match="COVERAGE_WINDOW_MISMATCH"):
        BacktestEngine().run(
            bars,
            [signal()],
            [],
            coverage,
            CostModel(Decimal("10"), Decimal("5")),
        )


def test_mixed_strategy_versions_are_rejected() -> None:
    bars = [bar(i) for i in range(16)]
    mixed = [
        signal("LONG", "90", START),
        EntrySignal("BTCUSDT", START + 900_000, "LONG", Decimal(90), "S01-0.2"),
    ]
    with pytest.raises(BacktestUnavailable, match="MIXED_STRATEGY_VERSIONS"):
        run(bars, mixed)


def test_short_risk_budget_includes_adverse_stop_exit_costs() -> None:
    bars = [
        bar(0),
        bar(1, open_="100", high="101", low="78", close="80"),
        bar(2, open_="80", high="81", low="79", close="80"),
    ]
    config = BacktestConfig(
        initial_equity=Decimal("1000"),
        risk_fraction=Decimal(".01"),
        max_notional_fraction=Decimal(1),
    )
    trade = run(bars, [signal("SHORT", "110")], config=config).trades[0]
    fee_rate = Decimal("10") / Decimal("10000")
    slippage_rate = Decimal("5") / Decimal("10000")
    stop_exit_fill = Decimal("110") * (Decimal(1) + slippage_rate)
    estimated_stop_loss = (stop_exit_fill - trade.entry_fill_price) * trade.quantity + (
        stop_exit_fill + trade.entry_fill_price
    ) * fee_rate * trade.quantity
    risk_budget = config.initial_equity * config.risk_fraction
    assert estimated_stop_loss <= risk_budget + Decimal("1e-24")


def test_same_bar_entry_and_exit_counts_as_exposure() -> None:
    bars = [
        bar(0, high="130", low="80", close="110"),
        bar(1),
    ]
    result = run(bars, [signal()])
    assert result.metrics.trade_count == 1
    assert result.trades[0].exit_reason == "AMBIGUOUS_STOP_FIRST"
    assert result.metrics.exposure_fraction == Decimal(".5")


def test_signal_while_position_open_is_skipped_without_same_bar_reentry() -> None:
    bars = [bar(i) for i in range(17)]
    bars[15] = bar(15, high="130", low="80", close="110")
    second_signal_time = START + 15 * 60_000
    result = run(
        bars,
        [
            signal(),
            signal("LONG", "90", second_signal_time),
        ],
    )
    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "AMBIGUOUS_STOP_FIRST"
    assert len(result.skipped_signals) == 1
    assert result.skipped_signals[0].reason == "POSITION_ALREADY_OPEN"


def test_nonpositive_short_take_profit_is_skipped() -> None:
    bars = [bar(0), bar(1), bar(2)]
    result = run(bars, [signal("SHORT", "250")])
    assert result.trades == ()
    assert result.skipped_signals[0].reason == "NONPOSITIVE_TAKE_PROFIT"
