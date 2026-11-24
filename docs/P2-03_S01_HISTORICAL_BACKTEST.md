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
  missing trade counts return `UNAVAILABLE`.
- Load **2,920 preceding finalized 15m candles plus all 15m evaluation candles**.
  This is the full 30-day empirical-percentile warm-up required by S01.
- Load **120 preceding closed 1h candles**, anchored to the last completed hour
  at or before the evaluation start, plus subsequent 1h candles only when their
  exclusive close boundary is no later than the evaluation end.
- For every 15m signal boundary, S01 selects the latest 1h feature whose close
  is at or before that boundary. Future 1h context is never eligible.
- Load the exact funding `[start_ms, end_ms)` manifest using the stored source
  fingerprints. Missing, incomplete, tampered or conflicting manifests return
  `UNAVAILABLE`. Regular and Special events at one timestamp remain separate.
- Signals are generated from closed 15m candles. The P2-01 engine can enter only
  at the next 1m open; the final exclusive boundary cannot create a fill.

## Manual command

First ensure the P2-02 public dataset has been backfilled and both 1m execution
and funding coverage are READY for the requested period. Apply schema migrations
before running the collector or the backtest:

```bash
alembic upgrade head
```

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
- Strategy version, explicit cost/risk assumptions and model limitations.

JSON serialization uses stable key ordering and Decimal strings. The report has
no wall-clock run timestamp, so replaying unchanged inputs and assumptions
produces byte-for-byte identical JSON.

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
deterministic repeatability, zero database writes, and successful engine replay.
