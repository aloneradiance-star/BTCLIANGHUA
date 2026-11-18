# P1-02A — manually backfill and verify 30 days of public Binance USD-M history

**Status:** Research-only, opt-in backfill. This subsystem has no keys, signed
requests, account access, leverage settings, or trading endpoints.

## Why
The 15-minute 30-day empirical percentile has 2880 previous reference observations.
With 20-bar Bollinger warm-up, the first 30-day BB-width percentile is at index
2899. With 20 log-return realized volatility warm-up, the first realized-volatility
30-day percentile is at index 2900 (0-based). The minimum complete sequence is
**2901 consecutive finalized 15m candles**. For safety the default is 2920.

The normal P1-02 public collector only replays roughly 120 bars at first startup.
Therefore a feed being fresh does **not** imply sufficient 30-day feature coverage.
P1-04 must gate percentile-dependent decisions on this separate historical readiness.

## Running (explicit manual step)
Do not run the collector simultaneously against the same database in Phase 1;
multi-process writer locking has not been implemented. Complete Alembic migrations first.

```bash
docker compose up -d db
alembic upgrade head

# 2920 finalized BTCUSDT + ETHUSDT 15m candles per market
python -m baqs.market_data.backfill --timeframe 15m --symbols BTCUSDT ETHUSDT

# Additional 1h EMA60 warm-up
python -m baqs.market_data.backfill --timeframe 1h --symbols BTCUSDT ETHUSDT
```

The command uses only public REST `/fapi/v1/time` and `/fapi/v1/klines`.
Per-request pages are bounded (default 400 candles); retry behavior is
defined by the existing public REST adapter, with a 0.2 second delay between
chunks. No secret is necessary or permitted.

All stored prices are exact PostgreSQL NUMERIC values. UTC candle close is
the **exclusive** interval boundary; unclosed bars are excluded. The target
window is anchored to the exchange server's most recent completed interval.

## Idempotent checkpoint/restart
The durable checkpoint is the `candles` table itself. Each requested chunk
is checked for **all expected finalized open timestamps** before any network
call. Complete chunks are skipped; missing/partially completed chunks are
refetched and inserted idempotently. Conflicting previously finalized data
raise an exception and cannot be silently replaced. Re-running the same
anchored range makes no data changes. When the exchange clock has advanced,
a subsequent run may need a fresh trailing chunk.

If the process stops mid-chunk, already persisted candles remain; resume
by invoking the command again. A chunk is never treated as complete just
because some or most rows exist.

## Readiness
`GET /api/v1/market/history-readiness` exposes per-symbol `15m` and
`1h` health for a UTC observation time. A ready result requires exactly
the requested trailing window of expected completed candles and no gaps
within it. `READY`, `NO_DATA`, `INSUFFICIENT`, `GAP` or `STALE`
must remain explicit; no market probabilities or trade signals.

Readiness reports describe **Kline coverage only**. They do not validate
price predictions, strategy profitability, funding rates, slippage or
order execution. Production deployment and concurrent-collector soak
tests remain future milestones.

## Acceptance
- 2901+ gapless closed 15m candles for each chosen symbol.
- Existing complete chunks are skipped; interrupted/partial chunks are repaired.
- Missing exchange rows, out-of-order data, malformed candles and conflicting
  stored snapshots fail closed.
- CI tests run with mocked public sources, without Binance network access or keys.
- Real-time last candle must be fresh for `READY` at the read-only API,
  independent from historical completeness.
