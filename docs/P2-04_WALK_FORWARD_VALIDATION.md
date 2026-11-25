# P2-04 / Long-horizon public history and S01 walk-forward validation

**Research-only.** No account credentials, private endpoints, orders, leverage,
auto-deployment, parameter optimization or profitability guarantees.

## 90-day public-history range

P2-04 extends the opt-in public candle backfill to explicit UTC
`[start_ms,end_ms)` ranges. Both bounds must be aligned to the selected
candle interval, the end must not exceed Binance server time, and the hard
90-day limits are:

| Timeframe | 90-day maximum |
|---|---:|
| 1m | 129,600 bars |
| 15m | 8,640 bars |
| 1h | 2,160 bars |

The default behavior is unchanged: 1m defaults to 43,200 bars / 30 days,
15m to 2,920 bars and 1h to 120 bars. Explicit ranges are bounded, chunked,
restartable and idempotent. Finalized candle conflicts still fail closed.

Choose 90-day HISTORY_START_MS / HISTORY_END_MS bounds aligned to 15m (and therefore also 1m). For 1h, round the start down to an hour and use floor(EVAL_END_MS / 3600000) * 3600000 as the exclusive end, so the 1h range remains within its 90-day cap while covering every fold context:

```bash
python -m baqs.market_data.backfill --timeframe 1m --symbols BTCUSDT ETHUSDT --start-ms <HISTORY_START_MS> --end-ms <HISTORY_END_MS>
python -m baqs.market_data.backfill --timeframe 15m --symbols BTCUSDT ETHUSDT --start-ms <HISTORY_START_MS> --end-ms <HISTORY_END_MS>
python -m baqs.market_data.backfill --timeframe 1h --symbols BTCUSDT ETHUSDT --start-ms <HISTORY_START_HOUR_MS> --end-ms <EVAL_END_HOUR_MS>
```

## Evaluation layout

S01 requires 2,920 preceding finalized 15m bars (30.4167 days) and 120
preceding closed 1h bars for every fold. A 90-day history window therefore
does not provide 90 days of out-of-sample evaluation. The recommended
reproducible layout uses the first 2,920 15m bars only as warm-up, then a
57-day evaluation interval split into three adjacent 19-day folds:

- `EVAL_START_MS = HISTORY_START_MS + 2920 * 900000`
- `EVAL_END_MS = EVAL_START_MS + 5472 * 900000`
- Each fold contains 1,824 15m bars / 27,360 1m execution bars.

The evaluation duration plus the 2,920-bar warm-up must fit inside the 90-day history cap; the runner enforces this. The remaining 2.58 days of the 90-day history window are a buffer, not an additional test fold. Each fold is half-open and starts exactly when the
previous fold ends. The runner rejects non-divisible or non-15m-aligned
windows, and each fold remains below P2-03's 50,000 execution-minute cap.

## Exact funding manifests

Funding coverage is keyed to exact half-open windows. Backfill funding
separately for every fold and every symbol; do not create one broad manifest
and reuse it for smaller folds:

```bash
python -m baqs.market_data.funding_backfill --start-ms <FOLD_1_START_MS> --end-ms <FOLD_1_END_MS>
python -m baqs.market_data.funding_backfill --start-ms <FOLD_2_START_MS> --end-ms <FOLD_2_END_MS>
python -m baqs.market_data.funding_backfill --start-ms <FOLD_3_START_MS> --end-ms <FOLD_3_END_MS>
```

The funding CLI defaults to both BTCUSDT and ETHUSDT; use `--symbols BTCUSDT` or `--symbols ETHUSDT` to run one asset only. Each fold must have an exact,
complete, fingerprint-verified funding manifest. Missing or tampered
coverage returns UNAVAILABLE.

The read-only `GET /api/v1/market/backtest-readiness` endpoint checks an
exact window of up to 129,600 execution minutes. A 90-day readiness check
verifies archive coverage for that window. Each simulated fold still needs
its own exact manifest and remains within the 50,000-minute replay cap.

## Walk-forward command

```bash
python -m baqs.backtest.walk_forward \
  --symbol BTCUSDT \
  --start-ms <EVAL_START_MS> \
  --end-ms <EVAL_END_MS> \
  --fold-count 3 \
  --minimum-trades-per-fold 5 \
  --cost-scenario base:5:2 \
  --cost-scenario stress:10:10
```

Cost scenario format is `NAME:TAKER_FEE_BPS:SLIPPAGE_BPS`. Scenario names must
be unique, with at least two distinct fee and slippage combinations. The
example values illustrate the format. Supply explicit costs for the research
scenario. S01-0.1 uses frozen thresholds throughout the evaluation.

The runner evaluates BTCUSDT and ETHUSDT separately. Every fold reuses
P2-03's read-only loader, exact 1m execution data, 2,920 preceding 15m bars,
120 preceding closed 1h bars, and P2-01's Decimal event engine. Source
candles/funding are never modified and reports are not persisted.

## Report interpretation

- `READY` means every fold and cost scenario passed data-quality checks and
  each fold met the configured minimum trade count. It does not mean
  profitable, robust, deployable or independently validated.
- `regime_candidates_by_fold` reports counts from the existing six-state
  candidate rules, unavailable observations and states absent from the fold.
  Each 15m interval is labelled using only 15m and 1h candles already closed
  at its opening. The evaluation-end boundary is excluded. Coverage is
  calculated once per fold from the first scenario's verified input set.
- `market_regime_coverage` reports `EVALUATED` when all intervals have a
  candidate, `PARTIAL` when some are unavailable, and `UNAVAILABLE` when
  none can be classified. A failed fold replay leaves the overall measure
  `NOT_EVALUATED`. Candidate counts omit the Phase 1 5m confirmation gate.
  `robustness_assessment=NOT_MADE` requires a separate review of real data
  and trade samples under the observed market states.
- `UNAVAILABLE` means at least one fold has incomplete or invalid inputs.
- `INSUFFICIENT_SAMPLE` means one or more folds did not meet the minimum
  trade count. Do not describe such a result as robust.
- Aggregate PnL is the sum of independent fold PnL; each fold starts with
  its own configured initial equity. It is **not** a compounded portfolio
  return. Worst-fold drawdown and duration-weighted exposure are reported.
  Aggregation uses the same isolated Decimal precision and rounding as the
  single-window replay. The cost matrix sums the monetary totals recorded
  for each fold.
- The scenario summary counts skipped signals by fold and reason. An interior
  boundary signal belongs to the later fold. A signal at the evaluation end
  is recorded as `NO_NEXT_1M_EXECUTION_BAR` in the final fold.
- 1m OHLC cannot reconstruct intrabar paths, partial fills, order-book
  liquidity, market impact, liquidation or margin tiers. Close-to-close
  drawdown can understate intrabar risk.
- No live 90-day data download or real historical backtest runs in CI.
  CI remains fixture-based and offline. A real-data run is a manual,
  separately reviewed research operation.
