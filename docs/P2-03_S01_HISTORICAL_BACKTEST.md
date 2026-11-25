# P2-03 / Reproducible S01 historical backtest

## Purpose and safety

This is a manual, PAPER-only, read-only historical research command. It does not
start from FastAPI, place orders, alter candle/funding records, save results to
the database, enable leverage, or make profitability claims.

The runner connects only to the configured BAQS database. It loads existing
public history and refuses to run unless the requested execution window and all
required context are complete and verified.

## Data contract

For a requested 15m-aligned half-open UTC window `[start_ms, end_ms)`:

- Load exactly every finalized 1m execution candle in the requested interval.
  Missing minutes, non-final rows, invalid boundaries, missing quote volume or
  missing trade counts return `UNAVAILABLE`. Exact timestamp checks apply to
  all three candle timeframes, including sub-millisecond offsets. Future
  evaluation windows are rejected even if future rows have been inserted.
- Load **2,920 preceding finalized 15m candles plus all 15m evaluation candles**.
  This is the fixed warm-up for the historical research runner.
- Load **120 preceding closed 1h candles**, anchored to the last completed hour
  at or before the evaluation start, plus subsequent 1h candles only when their
  exclusive close boundary is no later than the evaluation end.
- For every 15m signal boundary, S01 selects the latest 1h feature whose close
  is at or before that boundary. Future 1h context is never eligible.
- Load the exact funding `[start_ms, end_ms)` manifest using the stored source
  fingerprints. Missing, incomplete, tampered or conflicting manifests return
  `UNAVAILABLE`. Regular and Special events at one timestamp remain separate.
  A verified manifest with between-minute settlements returns
  `UNAVAILABLE` with reason `OFF_MINUTE_EVENTS`.
- Signals are generated from closed 15m candles. The P2-01 engine can enter only
  at the next 1m open; the final exclusive boundary cannot create a fill.

## Manual command

First apply schema migrations:

```bash
alembic upgrade head
```

The runner requires persisted 1m execution candles, 15m signal candles, 1h
context candles and a funding manifest for the **same exact 15m-aligned sample
window**. The P2-02 1m+funding convenience command does not backfill the 15m/1h
warm-up, and a wider funding manifest is intentionally not substituted for an
exact-window manifest.

For a one-day sample, choose `END_MS` as the `anchored_end_ms` returned by a
15m backfill (it is a closed 15m boundary), then set
`START_MS = END_MS - 1440 * 60000`. Backfill enough context and execution data:

```bash
python -m baqs.market_data.backfill --timeframe 1m --bars 1500
python -m baqs.market_data.backfill --timeframe 15m --bars 3016
python -m baqs.market_data.backfill --timeframe 1h --bars 144
```

The counts above cover a 24-hour evaluation plus 2,920 preceding 15m bars and
120 preceding closed 1h bars. For a different sample duration, increase the
requested history counts to include the entire evaluation interval plus those
warm-up bars. Use the exact same `START_MS` and `END_MS` to certify funding:

```bash
python -m baqs.market_data.funding_backfill --start-ms <START_MS> --end-ms <END_MS>
```

A successful funding backfill certifies only that exact half-open interval.
The one-day sample must fit inside the persisted 1m history window.

Example command (5 bps taker fee and 2 bps adverse slippage are illustrative
assumptions only; replace them with the fee tier and slippage scenario being
studied):

```bash
python -m baqs.backtest.research \
  --symbol BTCUSDT \
  --start-ms <START_MS> \
  --end-ms <END_MS> \
  --taker-fee-bps 5 \
  --slippage-bps 2
```

Run ETH separately by changing `--symbol ETHUSDT`. The interval must be aligned
to 15m UTC boundaries. Taker fee and slippage are mandatory inputs; initial
equity defaults to 10,000 and risk per trade defaults to 0.5%. Gross notional is
hard-capped at 1x equity. The command prints one deterministic JSON object to
stdout. A valid run has `status=READY`; incomplete data has
`status=UNAVAILABLE` and exits with code 2.

## Report contents

- Trade ledger with entry/exit references and fills, quantity, stop/target,
  gross/net PnL, fees, funding, slippage, R multiple and exit reason.
- Skipped signals, minute-close equity curve and performance metrics.
- Exact execution window, 15m/1h warm-up boundaries, row counts, source counts
  and SHA-256 dataset fingerprints.
- Funding manifest timestamps, event count and fingerprint digest.
- Strategy version and every active S01 threshold, explicit cost/risk assumptions
  and model limitations.

JSON serialization uses stable key ordering and Decimal strings. Replay
arithmetic uses an isolated Decimal context with 28 significant digits and
round-half-even. The feature engine retains its documented 40-digit arithmetic
for Bollinger bands and realized volatility. With unchanged inputs, settings
and code, the report JSON is byte-for-byte reproducible.

## Model limitations

The 1m OHLC engine cannot reconstruct intrabar paths; ambiguous stop/target
touches are resolved stop-first. Partial fills, order-book liquidity, market
impact, exchange outages, liquidation, margin tiers and portfolio-level
cross-symbol risk are not modelled. Fee and slippage inputs are scenarios, not
verified historical execution prices. A deterministic backtest is not evidence
of future performance.

## Acceptance coverage

Offline integration tests cover exact-window loading, 15m/1h warm-up, future
hour exclusion, missing/gapped/non-final data, missing quote/trade metadata,
absent/tampered funding manifests, same-time Regular/Special events,
off-minute funding, exact timestamp boundaries, deterministic repeatability,
zero database writes, and an end-to-end replay using the actual S01 rules.
