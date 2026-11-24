"""Chronological, frozen-parameter S01 walk-forward research validation."""

import argparse
import json
from dataclasses import asdict, dataclass, is_dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, cast

from sqlalchemy.engine import Engine

from baqs.backtest.engine import BacktestConfig, CostModel
from baqs.backtest.research import (
    MAX_EXECUTION_BARS,
    WARMUP_15M_BARS,
    S01ResearchReport,
    S01ResearchService,
)
from baqs.db import make_engine
from baqs.market_data.schema import INTERVAL_MS, SYMBOLS
from baqs.settings import get_settings
from baqs.strategies.s01 import S01Config

ZERO = Decimal(0)
MAX_HISTORY_WINDOW_MS = 90 * 24 * 60 * 60 * 1000


@dataclass(frozen=True, slots=True)
class CostScenario:
    """One explicitly named fee/slippage assumption; never infer the user's fee tier."""

    name: str
    taker_fee_bps: Decimal
    slippage_bps: Decimal

    def __post_init__(self) -> None:
        if not self.name or self.name.strip() != self.name or len(self.name) > 32:
            raise ValueError("Cost scenario name must be a nonempty, trimmed label of <=32 chars")
        if not isinstance(self.taker_fee_bps, Decimal) or not isinstance(
            self.slippage_bps, Decimal
        ):
            raise ValueError("Cost scenario assumptions must be Decimal values")
        CostModel(self.taker_fee_bps, self.slippage_bps)


@dataclass(frozen=True, slots=True)
class FoldWindow:
    index: int
    start_ms: int
    end_ms: int


@dataclass(frozen=True, slots=True)
class WalkForwardCell:
    fold_index: int
    cost_scenario: str
    report: S01ResearchReport


@dataclass(frozen=True, slots=True)
class WalkForwardReport:
    status: Literal["READY", "UNAVAILABLE", "INSUFFICIENT_SAMPLE"]
    symbol: str
    start_ms: int
    end_ms: int
    fold_count: int
    minimum_trades_per_fold: int
    strategy_version: str
    strategy_parameters: dict[str, str]
    cost_scenarios: dict[str, dict[str, str]]
    fold_windows: tuple[FoldWindow, ...]
    cells: tuple[WalkForwardCell, ...]
    aggregate_by_scenario: dict[str, dict[str, object]]
    reason: str | None
    market_regime_coverage: str = "NOT_EVALUATED"
    robustness_assessment: str = "NOT_MADE"


def fold_windows(start_ms: int, end_ms: int, fold_count: int) -> tuple[FoldWindow, ...]:
    """Split one 15m-aligned evaluation interval into equal, adjacent half-open folds."""
    step = INTERVAL_MS["15m"]
    if (
        type(start_ms) is not int
        or type(end_ms) is not int
        or start_ms < 0
        or end_ms <= start_ms
        or start_ms % step
        or end_ms % step
        or type(fold_count) is not int
        or not 3 <= fold_count <= 12
    ):
        raise ValueError("Walk-forward requires a positive 15m-aligned range and 3..12 folds")
    duration = end_ms - start_ms
    if duration % fold_count:
        raise ValueError("Evaluation range must divide evenly across folds")
    fold_duration = duration // fold_count
    if duration + WARMUP_15M_BARS * step > MAX_HISTORY_WINDOW_MS:
        raise ValueError("Evaluation plus 15m warm-up must fit within the 90-day history cap")
    if fold_duration % step:
        raise ValueError("Every fold boundary must remain 15m-aligned")
    if fold_duration // INTERVAL_MS["1m"] > MAX_EXECUTION_BARS:
        raise ValueError(f"Each fold is limited to {MAX_EXECUTION_BARS} execution minutes")
    return tuple(
        FoldWindow(
            index=index + 1,
            start_ms=start_ms + index * fold_duration,
            end_ms=start_ms + (index + 1) * fold_duration,
        )
        for index in range(fold_count)
    )


def _json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    if is_dataclass(value):
        return _json_value(asdict(cast(Any, value)))
    return value


def walk_forward_report_json(report: WalkForwardReport) -> str:
    """Serialize the full fold ledger and aggregate with deterministic Decimal strings."""
    return json.dumps(_json_value(report), sort_keys=True, separators=(",", ":"))


def _aggregate_scenario(
    scenario: CostScenario,
    folds: tuple[FoldWindow, ...],
    cells: list[WalkForwardCell],
    minimum_trades_per_fold: int,
) -> dict[str, object]:
    reports = [cell.report for cell in cells if cell.cost_scenario == scenario.name]
    results = [report.result for report in reports]
    if len(results) != len(folds) or any(result is None for result in results):
        raise ValueError("Cannot aggregate incomplete walk-forward scenario results")
    completed = [result for result in results if result is not None]
    trades = [trade for result in completed for trade in result.trades]
    trade_count = len(trades)
    wins = sum(trade.net_pnl > ZERO for trade in trades)
    positive_pnl = sum((trade.net_pnl for trade in trades if trade.net_pnl > ZERO), ZERO)
    negative_pnl = abs(sum((trade.net_pnl for trade in trades if trade.net_pnl < ZERO), ZERO))
    gross_pnl = sum((trade.gross_pnl for trade in trades), ZERO)
    net_pnl = sum((trade.net_pnl for trade in trades), ZERO)
    fees = sum((trade.fees for trade in trades), ZERO)
    funding = sum((trade.funding_cost for trade in trades), ZERO)
    slippage = sum((trade.slippage_cost for trade in trades), ZERO)
    metrics = [result.metrics for result in completed]
    durations = [fold.end_ms - fold.start_ms for fold in folds]
    total_duration = sum(durations)
    exposure = sum(
        (
            metric.exposure_fraction * Decimal(duration)
            for metric, duration in zip(metrics, durations, strict=True)
        ),
        ZERO,
    ) / Decimal(total_duration)
    trade_counts = [metric.trade_count for metric in metrics]
    average_r = (
        sum((trade.r_multiple for trade in trades), ZERO) / Decimal(trade_count)
        if trade_count
        else None
    )
    return {
        "fold_count": len(folds),
        "trade_counts_by_fold": trade_counts,
        "trade_count": trade_count,
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl,
        "total_fees": fees,
        "total_funding_cost": funding,
        "total_slippage_cost": slippage,
        "win_rate": Decimal(wins) / Decimal(trade_count) if trade_count else None,
        "profit_factor": positive_pnl / negative_pnl if negative_pnl else None,
        "average_r": average_r,
        "worst_fold_max_drawdown": max(metric.max_drawdown for metric in metrics),
        "duration_weighted_exposure_fraction": exposure,
        "minimum_trades_per_fold": minimum_trades_per_fold,
        "sufficient_sample": all(count >= minimum_trades_per_fold for count in trade_counts),
        "aggregation_basis": "summed independent fold PnL; not a compounded portfolio return",
        "market_regime_coverage": "NOT_EVALUATED",
        "robustness_assessment": "NOT_MADE",
    }


class WalkForwardResearchService:
    """Run fixed S01-0.1 parameters over adjacent chronological folds and cost scenarios."""

    def __init__(
        self,
        engine: Engine,
        *,
        cost_scenarios: tuple[CostScenario, ...] | list[CostScenario],
        fold_count: int = 3,
        minimum_trades_per_fold: int = 5,
        config: BacktestConfig | None = None,
    ) -> None:
        scenarios = tuple(cost_scenarios)
        if len(scenarios) < 2 or len({scenario.name for scenario in scenarios}) != len(scenarios):
            raise ValueError("At least two uniquely named cost scenarios are required")
        if type(fold_count) is not int or not 3 <= fold_count <= 12:
            raise ValueError("Walk-forward fold_count must be 3..12")
        if type(minimum_trades_per_fold) is not int or not 1 <= minimum_trades_per_fold <= 1000:
            raise ValueError("minimum_trades_per_fold must be 1..1000")
        self.engine = engine
        self.cost_scenarios = scenarios
        self.fold_count = fold_count
        self.minimum_trades_per_fold = minimum_trades_per_fold
        self.config = config if config is not None else BacktestConfig()

    def run(self, symbol: str, start_ms: int, end_ms: int) -> WalkForwardReport:
        if symbol not in SYMBOLS:
            raise ValueError("Only BTCUSDT and ETHUSDT are supported")
        folds = fold_windows(start_ms, end_ms, self.fold_count)
        cells: list[WalkForwardCell] = []
        strategy_parameters: dict[str, str] = {}
        strategy_config = S01Config()
        strategy_version = strategy_config.strategy_version

        for fold in folds:
            for scenario in self.cost_scenarios:
                report = S01ResearchService(
                    self.engine,
                    costs=CostModel(scenario.taker_fee_bps, scenario.slippage_bps),
                    config=self.config,
                    strategy_config=strategy_config,
                ).run(symbol, fold.start_ms, fold.end_ms)
                cells.append(WalkForwardCell(fold.index, scenario.name, report))
                if report.status != "READY":
                    return WalkForwardReport(
                        status="UNAVAILABLE",
                        symbol=symbol,
                        start_ms=start_ms,
                        end_ms=end_ms,
                        fold_count=self.fold_count,
                        minimum_trades_per_fold=self.minimum_trades_per_fold,
                        strategy_version=strategy_version,
                        strategy_parameters=report.strategy_parameters,
                        cost_scenarios={
                            item.name: {
                                "taker_fee_bps": format(item.taker_fee_bps, "f"),
                                "slippage_bps": format(item.slippage_bps, "f"),
                            }
                            for item in self.cost_scenarios
                        },
                        fold_windows=folds,
                        cells=tuple(cells),
                        aggregate_by_scenario={},
                        reason=f"FOLD_{fold.index}_{scenario.name}: {report.reason}",
                    )
                if not strategy_parameters:
                    strategy_parameters = report.strategy_parameters
                    strategy_version = report.strategy_version

        aggregates = {
            scenario.name: _aggregate_scenario(
                scenario,
                folds,
                cells,
                self.minimum_trades_per_fold,
            )
            for scenario in self.cost_scenarios
        }
        sufficient = all(bool(aggregate["sufficient_sample"]) for aggregate in aggregates.values())
        return WalkForwardReport(
            status="READY" if sufficient else "INSUFFICIENT_SAMPLE",
            symbol=symbol,
            start_ms=start_ms,
            end_ms=end_ms,
            fold_count=self.fold_count,
            minimum_trades_per_fold=self.minimum_trades_per_fold,
            strategy_version=strategy_version,
            strategy_parameters=strategy_parameters,
            cost_scenarios={
                item.name: {
                    "taker_fee_bps": format(item.taker_fee_bps, "f"),
                    "slippage_bps": format(item.slippage_bps, "f"),
                }
                for item in self.cost_scenarios
            },
            fold_windows=folds,
            cells=tuple(cells),
            aggregate_by_scenario=aggregates,
            reason=None if sufficient else "MINIMUM_TRADES_PER_FOLD_NOT_MET",
        )


def _parse_cost_scenario(value: str) -> CostScenario:
    parts = value.split(":")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("Use NAME:TAKER_FEE_BPS:SLIPPAGE_BPS")
    try:
        return CostScenario(parts[0], Decimal(parts[1]), Decimal(parts[2]))
    except (InvalidOperation, ValueError) as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run frozen-parameter, read-only S01 chronological walk-forward research"
    )
    parser.add_argument("--symbol", required=True, choices=sorted(SYMBOLS))
    parser.add_argument("--start-ms", required=True, type=int)
    parser.add_argument("--end-ms", required=True, type=int)
    parser.add_argument("--fold-count", type=int, default=3)
    parser.add_argument("--minimum-trades-per-fold", type=int, default=5)
    parser.add_argument(
        "--cost-scenario",
        action="append",
        type=_parse_cost_scenario,
        required=True,
        help="Repeat at least twice: NAME:TAKER_FEE_BPS:SLIPPAGE_BPS",
    )
    parser.add_argument("--initial-equity", type=Decimal, default=Decimal("10000"))
    parser.add_argument("--risk-fraction", type=Decimal, default=Decimal("0.005"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if len(args.cost_scenario) < 2:
        raise SystemExit("At least two --cost-scenario values are required")
    settings = get_settings()
    if settings.environment != "PAPER" or settings.live_enabled:
        raise RuntimeError("Walk-forward research is permitted only in PAPER mode")
    config = BacktestConfig(
        initial_equity=args.initial_equity,
        risk_fraction=args.risk_fraction,
        max_notional_fraction=Decimal(1),
    )
    engine = make_engine(settings)
    try:
        report = WalkForwardResearchService(
            engine,
            cost_scenarios=args.cost_scenario,
            fold_count=args.fold_count,
            minimum_trades_per_fold=args.minimum_trades_per_fold,
            config=config,
        ).run(args.symbol, args.start_ms, args.end_ms)
        print(walk_forward_report_json(report))
        if report.status != "READY":
            raise SystemExit(2)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
