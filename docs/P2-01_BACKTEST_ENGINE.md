# P2-01 — Event-driven backtest engine and S01 v0.1

**Research-only.** This PR provides a pure offline simulator. It is not a
broker, exchange adapter, order router, live signal service or proof of alpha.

## Inputs and data readiness

- Execution: contiguous, finalized 1m candles for one symbol.
- Strategy: contiguous, finalized 15m candles and 1h candles for the same symbol.
- Signals are evaluated at the exclusive close boundary of a completed 15m bar.
  The earliest possible entry is the next 1m bar open at that same boundary.
- All evaluation bars must be present; duplicates, mixed symbols, non-1m fills,
  gaps and out-of-order data fail closed.
- Funding is mandatory as an explicit input. Each event contains the symbol,
  event timestamp, signed funding rate and contemporaneous mark price. A
  provider-supplied `FundingCoverage` lists the exact expected event timestamps
  and asserts complete coverage over the entire backtest window. The supplied
  events must exactly match that manifest. Empty events are accepted only when
  the provider explicitly attests that the complete window has no events.
- Fee and slippage assumptions are required values; no exchange fee tier,
  spread, fill quality or funding rate is silently invented.

P1-02A currently backfills 15m and 1h candles only. It does not provide the
1m execution history, historical funding-rate history or funding-time mark
prices required for a trusted historical S01 run. A follow-up public-data issue
must add those sources and provenance. Until then the engine can be validated
with synthetic deterministic fixtures only.

## S01 trend-pullback v0.1 hypothesis

A signal is generated only at a completed 15m close.

**Long**
1. Latest completed 1h EMA20 > EMA60.
2. 15m EMA20 > EMA60, current close > EMA60 and ADX14 >= 23.
3. Previous 15m close <= previous EMA20; current 15m low <= current EMA20
   and current close > current EMA20 (pullback and reclaim).
4. RSI14 in [45, 65] and volume ratio >= 1.0.
5. Initial stop = min(current low, previous low) - 0.25 * ATR14.

**Short** is symmetric: 1h/15m EMA20 < EMA60, current close below EMA60,
ADX14 >= 23, previous close >= previous EMA20, current high >= current EMA20,
current close below EMA20, RSI14 in [35,55], volume ratio >= 1.0. Stop =
max(current high, previous high) + 0.25 * ATR14.

The thresholds are transparent research defaults, not validated or optimized
parameters. The signal is not a calibrated probability or trade recommendation.

## Execution and accounting rules

- One open position per symbol; no pyramiding and no leverage.
- Position size is the lesser of risk-budget size (default 0.5% of current
  equity, inclusive of estimated round-trip fees and adverse exit slippage)
  and a 1.0x equity gross-notional cap.
- Fixed take-profit is 2R from the adverse-slippage entry fill; stop is the
  strategy's structural stop.
- All fills are assumed taker. Fee and adverse slippage in bps are explicit.
- 1m OHLC handles stops and targets. If both levels are touched in one 1m
  candle, stop executes first. A gap through a stop fills at the worse bar open.
  This is conservative but does not model order-book depth, partial fills,
  spread dynamics or market impact.
- Funding is assessed only when a position was already open before the funding
  timestamp. Positive funding costs longs and credits shorts; negative funding
  reverses the cash flow.
- An open position is force-closed at the final observed close and labelled
  `END_OF_DATA`; it is not left as hidden unrealized profit.
- The engine records gross PnL, fees, funding, slippage, net PnL, R multiple,
  per-minute mark-to-market equity, drawdown, win rate, profit factor and
  exposure. Undefined statistics remain null rather than fabricated.

## Run from Python

```python
from decimal import Decimal
from baqs.backtest import (
    BacktestConfig,
    BacktestEngine,
    CostModel,
    FundingCoverage,
)
from baqs.strategies import S01TrendPullbackStrategy

signals = S01TrendPullbackStrategy().generate_signals(bars15, bars1h)
result = BacktestEngine().run(
    execution_bars=bars1m,
    signals=signals,
    funding_events=funding_events,
    funding_coverage=funding_coverage,
    costs=CostModel(
        taker_fee_bps=Decimal("5"),
        slippage_bps=Decimal("2"),
    ),
    config=BacktestConfig(initial_equity=Decimal("10000")),
)
print(result.metrics)
```

The bps above are example user-supplied assumptions, not a statement of
current Binance fees or achievable fills.

## Acceptance and limitations

- Deterministic Decimal PnL accounting and offline tests.
- Next-minute fill timing and causal 1h/15m signal alignment.
- Explicit, exact funding completeness manifest; no silent zero-funding run.
- Stop-first ambiguity, stop gap-through, fees, adverse slippage, risk cap,
  end-of-data close and replay determinism are tested.
- No parameter search, in-sample optimization, walk-forward analysis, UI,
  API endpoint, database migration, live/private API or order execution.
- This implementation is not evidence of profitability. Before strategy
  evaluation, obtain verified 1m candles and historical funding + mark prices,
  independently validate their completeness, then run out-of-sample,
  walk-forward and sensitivity tests with realistic fee/slippage assumptions.
