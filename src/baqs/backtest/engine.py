"""Pure Decimal, event-driven perpetual-futures backtest accounting.

This is a deterministic research simulator, not an exchange execution adapter.
All fees, slippage and funding inputs are explicit; missing funding coverage fails
closed. Signals become eligible only at the next 1m candle open.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from baqs.market_data.schema import FinalCandle

Side = Literal["LONG", "SHORT"]
ZERO = Decimal(0)
BPS = Decimal(10_000)
ONE = Decimal(1)


class BacktestUnavailable(ValueError):
    """Required history, cost assumptions or funding coverage is invalid."""


@dataclass(frozen=True, slots=True)
class CostModel:
    """Explicit all-taker fee and adverse slippage assumptions, in basis points."""

    taker_fee_bps: Decimal
    slippage_bps: Decimal

    def __post_init__(self) -> None:
        if (
            not self.taker_fee_bps.is_finite()
            or not self.slippage_bps.is_finite()
            or not ZERO <= self.taker_fee_bps < BPS
            or not ZERO <= self.slippage_bps < BPS
        ):
            raise ValueError("Fee must be nonnegative and slippage must be in [0, 10000) bps")


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    """Risk sizing defaults are research assumptions, not financial advice."""

    initial_equity: Decimal = Decimal("10000")
    risk_fraction: Decimal = Decimal("0.005")
    max_notional_fraction: Decimal = ONE
    reward_risk: Decimal = Decimal(2)

    def __post_init__(self) -> None:
        if not self.initial_equity.is_finite() or self.initial_equity <= ZERO:
            raise ValueError("Initial equity must be positive")
        if not self.risk_fraction.is_finite() or not ZERO < self.risk_fraction <= ONE:
            raise ValueError("Risk fraction must be in (0, 1]")
        if (
            not self.max_notional_fraction.is_finite()
            or not ZERO < self.max_notional_fraction <= ONE
        ):
            raise ValueError("Gross notional cap must be in (0, 1]; leverage is disabled")
        if not self.reward_risk.is_finite() or self.reward_risk <= ZERO:
            raise ValueError("Reward/risk multiple must be positive")


@dataclass(frozen=True, slots=True)
class EntrySignal:
    symbol: str
    signal_time_ms: int
    side: Side
    stop_price: Decimal
    strategy_version: str

    def __post_init__(self) -> None:
        if self.side not in {"LONG", "SHORT"}:
            raise ValueError("Unsupported signal side")
        if (
            type(self.signal_time_ms) is not int
            or self.signal_time_ms < 0
            or not self.stop_price.is_finite()
            or self.stop_price <= ZERO
            or not self.strategy_version
        ):
            raise ValueError("Invalid entry signal")


@dataclass(frozen=True, slots=True)
class FundingEvent:
    """One public historical funding observation and its contemporaneous mark price."""

    symbol: str
    timestamp_ms: int
    rate: Decimal
    mark_price: Decimal
    rate_type: str = "Regular"

    def __post_init__(self) -> None:
        if (
            type(self.timestamp_ms) is not int
            or self.timestamp_ms < 0
            or self.timestamp_ms % 60_000
            or not self.rate.is_finite()
            or not self.mark_price.is_finite()
            or self.mark_price <= ZERO
            or self.rate_type not in {"Regular", "Special"}
        ):
            raise ValueError("Invalid funding event or mark-price timestamp")


@dataclass(frozen=True, slots=True)
class FundingCoverage:
    """Provider-supplied completeness manifest; expected timestamps are exact."""

    start_ms: int
    end_ms: int
    expected_timestamps_ms: tuple[int, ...]
    complete: bool

    def __post_init__(self) -> None:
        expected = self.expected_timestamps_ms
        if (
            type(self.start_ms) is not int
            or type(self.end_ms) is not int
            or self.start_ms < 0
            or self.end_ms <= self.start_ms
            or type(self.complete) is not bool
            or not self.complete
            or tuple(sorted(expected)) != expected
            or any(
                type(timestamp) is not int
                or timestamp < self.start_ms
                or timestamp >= self.end_ms
                or timestamp % 60_000
                for timestamp in expected
            )
        ):
            raise ValueError("Funding coverage must be complete and strictly ordered")


@dataclass(frozen=True, slots=True)
class Trade:
    symbol: str
    side: Side
    strategy_version: str
    entry_time_ms: int
    exit_bar_open_ms: int
    exit_bar_close_ms: int
    exit_time_precision: str
    entry_reference_price: Decimal
    exit_reference_price: Decimal
    entry_fill_price: Decimal
    exit_fill_price: Decimal
    quantity: Decimal
    initial_stop: Decimal
    take_profit: Decimal
    risk_amount: Decimal
    gross_pnl: Decimal
    fees: Decimal
    funding_cost: Decimal
    slippage_cost: Decimal
    net_pnl: Decimal
    r_multiple: Decimal
    exit_reason: str


@dataclass(frozen=True, slots=True)
class SkippedSignal:
    signal_time_ms: int
    side: Side
    reason: str


@dataclass(frozen=True, slots=True)
class EquityPoint:
    timestamp_ms: int
    cash: Decimal
    unrealized_pnl: Decimal
    equity: Decimal
    drawdown: Decimal


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    initial_equity: Decimal
    final_equity: Decimal
    net_pnl: Decimal
    total_return: Decimal
    gross_pnl: Decimal
    total_fees: Decimal
    total_funding_cost: Decimal
    total_slippage_cost: Decimal
    trade_count: int
    win_rate: Decimal | None
    profit_factor: Decimal | None
    max_drawdown: Decimal
    average_r: Decimal | None
    exposure_fraction: Decimal


@dataclass(frozen=True, slots=True)
class BacktestResult:
    symbol: str
    strategy_version: str
    start_ms: int
    end_ms: int
    trades: tuple[Trade, ...]
    skipped_signals: tuple[SkippedSignal, ...]
    equity_curve: tuple[EquityPoint, ...]
    metrics: BacktestMetrics
    assumptions: dict[str, str]


@dataclass(slots=True)
class _OpenPosition:
    signal: EntrySignal
    entry_time_ms: int
    entry_reference: Decimal
    entry_fill: Decimal
    quantity: Decimal
    stop: Decimal
    target: Decimal
    risk_amount: Decimal
    entry_fee: Decimal
    entry_slippage: Decimal
    funding_cost: Decimal = ZERO


def _validate_bars(bars: Sequence[FinalCandle]) -> str:
    if not bars:
        raise BacktestUnavailable("NO_EXECUTION_CANDLES")
    symbol = bars[0].symbol
    previous_close: int | None = None
    for bar in bars:
        if bar.symbol != symbol or bar.timeframe != "1m":
            raise BacktestUnavailable("MIXED_SYMBOL_OR_NON_1M_EXECUTION_DATA")
        if bar.close_ms != bar.open_ms + 60_000:
            raise BacktestUnavailable("INVALID_1M_CANDLE_BOUNDARY")
        if previous_close is not None and bar.open_ms != previous_close:
            raise BacktestUnavailable("EXECUTION_CANDLE_GAP_OR_DUPLICATE")
        previous_close = bar.close_ms
    return symbol


def _validate_inputs(
    bars: Sequence[FinalCandle],
    signals: Sequence[EntrySignal],
    funding_events: Sequence[FundingEvent],
    funding_coverage: FundingCoverage,
) -> tuple[str, dict[int, EntrySignal], dict[int, list[FundingEvent]]]:
    symbol = _validate_bars(bars)
    start_ms, end_ms = bars[0].open_ms, bars[-1].close_ms
    if funding_coverage.start_ms != start_ms or funding_coverage.end_ms != end_ms:
        raise BacktestUnavailable("FUNDING_COVERAGE_WINDOW_MISMATCH")
    expected = funding_coverage.expected_timestamps_ms
    events = sorted(funding_events, key=lambda event: (event.timestamp_ms, event.rate_type))
    actual = tuple(event.timestamp_ms for event in events)
    if actual != expected:
        raise BacktestUnavailable("FUNDING_EVENTS_DO_NOT_MATCH_COMPLETENESS_MANIFEST")
    funding_by_time: dict[int, list[FundingEvent]] = {}
    bar_opens = {bar.open_ms for bar in bars}
    for event in events:
        if event.symbol != symbol:
            raise BacktestUnavailable("FUNDING_SYMBOL_MISMATCH")
        if event.timestamp_ms not in bar_opens:
            raise BacktestUnavailable("FUNDING_EVENT_NOT_ALIGNED_TO_EXECUTION_BAR")
        funding_by_time.setdefault(event.timestamp_ms, []).append(event)

    signal_by_time: dict[int, EntrySignal] = {}
    strategy_versions: set[str] = set()
    for signal in signals:
        if signal.symbol != symbol:
            raise BacktestUnavailable("SIGNAL_SYMBOL_MISMATCH")
        if signal.signal_time_ms < start_ms:
            continue  # Pre-evaluation warm-up signal; never opened in this run.
        if signal.signal_time_ms == end_ms:
            continue  # Closed at the sample boundary, but no next-minute fill exists.
        if signal.signal_time_ms > end_ms:
            raise BacktestUnavailable("SIGNAL_AFTER_BACKTEST_WINDOW")
        strategy_versions.add(signal.strategy_version)
        if signal.signal_time_ms % 900_000:
            raise BacktestUnavailable("SIGNAL_NOT_AT_CLOSED_15M_BOUNDARY")
        if signal.signal_time_ms not in bar_opens:
            raise BacktestUnavailable("SIGNAL_HAS_NO_NEXT_1M_EXECUTION_BAR")
        if signal.signal_time_ms in signal_by_time:
            raise BacktestUnavailable("DUPLICATE_SIGNAL_TIMESTAMP")
        signal_by_time[signal.signal_time_ms] = signal
    if len(strategy_versions) > 1:
        raise BacktestUnavailable("MIXED_STRATEGY_VERSIONS")
    return symbol, signal_by_time, funding_by_time


def _entry_fill(reference: Decimal, side: Side, slippage: Decimal) -> Decimal:
    return reference * (ONE + slippage) if side == "LONG" else reference * (ONE - slippage)


def _exit_fill(reference: Decimal, side: Side, slippage: Decimal) -> Decimal:
    return reference * (ONE - slippage) if side == "LONG" else reference * (ONE + slippage)


def _close_position(
    position: _OpenPosition,
    bar: FinalCandle,
    reference: Decimal,
    reason: str,
    *,
    exact_close: bool,
    fee_rate: Decimal,
    slippage_rate: Decimal,
    cash: Decimal,
) -> tuple[Trade, Decimal]:
    side_sign = ONE if position.signal.side == "LONG" else -ONE
    fill = _exit_fill(reference, position.signal.side, slippage_rate)
    exit_fee = fill * position.quantity * fee_rate
    gross = side_sign * (fill - position.entry_fill) * position.quantity
    fees = position.entry_fee + exit_fee
    net = gross - fees - position.funding_cost
    slippage_cost = position.entry_slippage + abs(fill - reference) * position.quantity
    trade = Trade(
        symbol=position.signal.symbol,
        side=position.signal.side,
        strategy_version=position.signal.strategy_version,
        entry_time_ms=position.entry_time_ms,
        exit_bar_open_ms=bar.open_ms,
        exit_bar_close_ms=bar.close_ms,
        exit_time_precision="BAR_CLOSE" if exact_close else "WITHIN_1M_BAR",
        entry_reference_price=position.entry_reference,
        exit_reference_price=reference,
        entry_fill_price=position.entry_fill,
        exit_fill_price=fill,
        quantity=position.quantity,
        initial_stop=position.stop,
        take_profit=position.target,
        risk_amount=position.risk_amount,
        gross_pnl=gross,
        fees=fees,
        funding_cost=position.funding_cost,
        slippage_cost=slippage_cost,
        net_pnl=net,
        r_multiple=net / position.risk_amount,
        exit_reason=reason,
    )
    return trade, cash + gross - exit_fee


def _exit_trigger(position: _OpenPosition, bar: FinalCandle) -> tuple[Decimal, str] | None:
    if position.signal.side == "LONG":
        stop_hit = bar.open <= position.stop or bar.low <= position.stop
        target_hit = bar.high >= position.target
        if stop_hit:
            if bar.open <= position.stop:
                return bar.open, "STOP_GAP"
            if target_hit:
                return position.stop, "AMBIGUOUS_STOP_FIRST"
            return position.stop, "STOP_LOSS"
        if target_hit:
            return position.target, "TAKE_PROFIT"
    else:
        stop_hit = bar.open >= position.stop or bar.high >= position.stop
        target_hit = bar.low <= position.target
        if stop_hit:
            if bar.open >= position.stop:
                return bar.open, "STOP_GAP"
            if target_hit:
                return position.stop, "AMBIGUOUS_STOP_FIRST"
            return position.stop, "STOP_LOSS"
        if target_hit:
            return position.target, "TAKE_PROFIT"
    return None


class BacktestEngine:
    """One-position-per-symbol simulator with conservative 1m OHLC bracket fills."""

    def run(
        self,
        execution_bars: Sequence[FinalCandle],
        signals: Sequence[EntrySignal],
        funding_events: Sequence[FundingEvent],
        funding_coverage: FundingCoverage,
        costs: CostModel,
        config: BacktestConfig = BacktestConfig(),
    ) -> BacktestResult:
        symbol, signal_by_time, funding_by_time = _validate_inputs(
            execution_bars, signals, funding_events, funding_coverage
        )
        fee_rate = costs.taker_fee_bps / BPS
        slippage_rate = costs.slippage_bps / BPS
        cash = config.initial_equity
        peak_equity = cash
        max_drawdown = ZERO
        position: _OpenPosition | None = None
        trades: list[Trade] = []
        skipped: list[SkippedSignal] = []
        curve: list[EquityPoint] = []
        active_bars = 0

        for bar in execution_bars:
            had_position_at_open = position is not None

            # Funding is assessed before any entry at this minute boundary.
            events = funding_by_time.get(bar.open_ms, [])
            if position is not None and events and bar.open_ms > position.entry_time_ms:
                side_sign = ONE if position.signal.side == "LONG" else -ONE
                for event in events:
                    funding_cost = side_sign * position.quantity * event.mark_price * event.rate
                    position.funding_cost += funding_cost
                    cash -= funding_cost

            if position is not None:
                trigger = _exit_trigger(position, bar)
                if trigger is not None:
                    reference, reason = trigger
                    trade, cash = _close_position(
                        position,
                        bar,
                        reference,
                        reason,
                        exact_close=False,
                        fee_rate=fee_rate,
                        slippage_rate=slippage_rate,
                        cash=cash,
                    )
                    trades.append(trade)
                    position = None

            # A position closed inside this bar cannot be reopened in that same bar.
            if not had_position_at_open and position is None:
                signal = signal_by_time.get(bar.open_ms)
                if signal is not None:
                    reference = bar.open
                    fill = _entry_fill(reference, signal.side, slippage_rate)
                    valid_stop = (
                        signal.stop_price < fill
                        if signal.side == "LONG"
                        else signal.stop_price > fill
                    )
                    if not valid_stop:
                        skipped.append(
                            SkippedSignal(signal.signal_time_ms, signal.side, "STOP_ON_WRONG_SIDE")
                        )
                    else:
                        stop_distance = abs(fill - signal.stop_price)
                        # Entry slippage is already reflected in stop_distance; reserve
                        # both taker fees and one adverse exit slippage in the risk budget.
                        risk_per_unit = stop_distance + fill * (
                            Decimal(2) * fee_rate + slippage_rate
                        )
                        equity_at_entry = cash
                        risk_budget = equity_at_entry * config.risk_fraction
                        notional_cap = equity_at_entry * config.max_notional_fraction
                        quantity_by_risk = risk_budget / risk_per_unit
                        quantity_by_notional = notional_cap / fill
                        quantity = min(quantity_by_risk, quantity_by_notional)
                        if quantity <= ZERO:
                            skipped.append(
                                SkippedSignal(
                                    signal.signal_time_ms, signal.side, "ZERO_POSITION_SIZE"
                                )
                            )
                        else:
                            side_sign = ONE if signal.side == "LONG" else -ONE
                            target = fill + side_sign * stop_distance * config.reward_risk
                            entry_fee = fill * quantity * fee_rate
                            entry_slippage = abs(fill - reference) * quantity
                            position = _OpenPosition(
                                signal=signal,
                                entry_time_ms=bar.open_ms,
                                entry_reference=reference,
                                entry_fill=fill,
                                quantity=quantity,
                                stop=signal.stop_price,
                                target=target,
                                risk_amount=quantity * stop_distance,
                                entry_fee=entry_fee,
                                entry_slippage=entry_slippage,
                            )
                            cash -= entry_fee
                            # Entry may be stopped/targeted within this same 1m bar.
                            trigger = _exit_trigger(position, bar)
                            if trigger is not None:
                                exit_reference, reason = trigger
                                trade, cash = _close_position(
                                    position,
                                    bar,
                                    exit_reference,
                                    reason,
                                    exact_close=False,
                                    fee_rate=fee_rate,
                                    slippage_rate=slippage_rate,
                                    cash=cash,
                                )
                                trades.append(trade)
                                position = None

            if had_position_at_open or position is not None:
                active_bars += 1

            # Close the last open position at the final observed close, never leave
            # unrealized PnL disguised as realized backtest profit.
            if bar.close_ms == execution_bars[-1].close_ms and position is not None:
                trade, cash = _close_position(
                    position,
                    bar,
                    bar.close,
                    "END_OF_DATA",
                    exact_close=True,
                    fee_rate=fee_rate,
                    slippage_rate=slippage_rate,
                    cash=cash,
                )
                trades.append(trade)
                position = None

            unrealized = ZERO
            if position is not None:
                side_sign = ONE if position.signal.side == "LONG" else -ONE
                unrealized = side_sign * (bar.close - position.entry_fill) * position.quantity
            equity = cash + unrealized
            if equity <= ZERO:
                raise BacktestUnavailable("EQUITY_EXHAUSTED_OR_MARGIN_MODEL_REQUIRED")
            peak_equity = max(peak_equity, equity)
            drawdown = (peak_equity - equity) / peak_equity
            max_drawdown = max(max_drawdown, drawdown)
            curve.append(
                EquityPoint(
                    timestamp_ms=bar.close_ms,
                    cash=cash,
                    unrealized_pnl=unrealized,
                    equity=equity,
                    drawdown=drawdown,
                )
            )

        gross_pnl = sum((trade.gross_pnl for trade in trades), ZERO)
        total_fees = sum((trade.fees for trade in trades), ZERO)
        total_funding = sum((trade.funding_cost for trade in trades), ZERO)
        total_slippage = sum((trade.slippage_cost for trade in trades), ZERO)
        wins = [trade for trade in trades if trade.net_pnl > ZERO]
        losses = [trade for trade in trades if trade.net_pnl < ZERO]
        win_rate = Decimal(len(wins)) / Decimal(len(trades)) if trades else None
        profit_factor = (
            sum((trade.net_pnl for trade in wins), ZERO)
            / abs(sum((trade.net_pnl for trade in losses), ZERO))
            if losses
            else None
        )
        average_r = (
            sum((trade.r_multiple for trade in trades), ZERO) / Decimal(len(trades))
            if trades
            else None
        )
        final_equity = curve[-1].equity
        net_pnl = final_equity - config.initial_equity
        metrics = BacktestMetrics(
            initial_equity=config.initial_equity,
            final_equity=final_equity,
            net_pnl=net_pnl,
            total_return=net_pnl / config.initial_equity,
            gross_pnl=gross_pnl,
            total_fees=total_fees,
            total_funding_cost=total_funding,
            total_slippage_cost=total_slippage,
            trade_count=len(trades),
            win_rate=win_rate,
            profit_factor=profit_factor,
            max_drawdown=max_drawdown,
            average_r=average_r,
            exposure_fraction=Decimal(active_bars) / Decimal(len(execution_bars)),
        )
        return BacktestResult(
            symbol=symbol,
            strategy_version=(
                trades[0].strategy_version
                if trades
                else (signals[0].strategy_version if signals else "NO_SIGNALS")
            ),
            start_ms=execution_bars[0].open_ms,
            end_ms=execution_bars[-1].close_ms,
            trades=tuple(trades),
            skipped_signals=tuple(skipped),
            equity_curve=tuple(curve),
            metrics=metrics,
            assumptions={
                "fill_timing": "NEXT_1M_OPEN",
                "stop_target_resolution": "ADVERSE_STOP_FIRST",
                "fees": "TAKER_BOTH_SIDES",
                "slippage": "ADVERSE_BOTH_SIDES",
                "funding": "EXPLICIT_COMPLETE_EVENT_MANIFEST",
                "gross_notional": "MAX_1X_EQUITY_NO_LEVERAGE",
                "pyramiding": "DISABLED",
                "partial_fills": "NOT_MODELLED",
                "market_impact": "NOT_MODELLED",
                "execution_enabled": "false",
            },
        )
