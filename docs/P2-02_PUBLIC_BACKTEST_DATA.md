# P2-02 / Public 1m execution and historical funding data

**Research-only public data.** This task does not authenticate a Binance account,
subscribe to private user streams, submit orders, or start automatically.

## Verified official USDⓈ-M REST contract (2026-11-23)

- `GET /fapi/v1/klines` supports `interval=1m`; candles are uniquely
  identified by open time and the documented maximum page limit is 1500.
  This collector uses the existing bounded 400-bar chunks for predictable
  request weight and restartable database checkpoints.
- `GET /fapi/v1/fundingRate` uses inclusive `startTime` and `endTime`,
  returns ascending events, and documents a maximum `limit=1000` (default
  100). A full page is followed by a cursor at the last `fundingTime + 1 ms`.
- Funding response fields include `symbol`, `fundingRate`, `fundingTime`,
  the mark price associated with that funding charge (`markPrice`), and
  `rateType` (`Regular` or `Special`). The associated mark price is used
  directly; do not substitute spot price, contract close, or mark-price Kline.
- Funding history shares a documented 500 requests / 5 minutes / IP limit
  with `/fapi/v1/fundingInfo`. The client uses a 1.25-second inter-page pause,
  bounded pagination, exponential retry, and honours numeric `Retry-After`
  values on HTTP 429.
- A full-page boundary is queried at its exact timestamp before advancing.
  A split event, changed funding rate or changed associated mark price
  invalidates the fetch and prevents completeness certification.
  A server retry delay over 60 seconds aborts the fetch so an operator can
  resume after the full requested wait.
- Funding events are **not** assumed to occur every eight hours. Returned
  timestamps and rate types are the source of truth.

Official references:
- https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Kline-Candlestick-Data
- https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-History

## Manual collection

Before the first run, start the configured PostgreSQL database, set `BAQS_DATABASE_URL` (or `.env`), and apply the checked-in schema migrations with `alembic upgrade head`. The collector intentionally does not create or migrate production tables automatically.
The `0003_public_backtest` revision joins the collector and funding schemas.
Databases already at either earlier revision advance to that same head
while retaining their stored rows.

**Recommended one-command path** (opt-in; defaults to BTCUSDT and ETHUSDT, 43,200
finalized 1m candles / 30 days each):

```bash
python -m baqs.market_data.prepare_backtest_data
```

The command runs the existing candle and funding collectors sequentially for
each symbol, reuses the exact half-open UTC window anchored by the finalized
candle backfill, verifies both persisted datasets, and prints one JSON report
per symbol. A successful report has `readiness=READY`; it also includes
`start_ms`, `end_ms`, candle/funding counts and created-versus-reused counts.
It is safe to rerun after interruption because verified candle chunks and
funding events are idempotent. To run a smaller research smoke test:

```bash
python -m baqs.market_data.prepare_backtest_data --symbols BTCUSDT --bars 1440
```

The lower-level collectors remain available when separate operation is needed:

1. Run the public 1m candle backfill:
   ```bash
   python -m baqs.market_data.backfill --timeframe 1m --symbols BTCUSDT ETHUSDT
   ```
2. Use the exact `start_ms` and `end_ms` printed by the candle report for funding:
   ```bash
   python -m baqs.market_data.funding_backfill --start-ms <START_MS> --end-ms <END_MS>
   ```
   The end boundary is exclusive internally; the client sends inclusive
   `endTime=end_ms-1` to the exchange.
3. Query readiness for that same exact window:
   ```
   GET /api/v1/market/backtest-readiness?start_ms=<START_MS>&end_ms=<END_MS>
   ```

## Persistence and readiness

- The existing `candles` table stores 1m execution bars. Backfill supports
  explicit, interval-aligned UTC ranges up to 90 days (1m: 129,600 bars;
  15m: 8,640; 1h: 2,160) and commits each verified 400-bar chunk atomically.
  The default 1m command remains 43,200 bars / 30 days.
- `funding_rates` has a composite key `(symbol, funding_time, rate_type)`
  so Regular and Special observations at the same funding timestamp are both
  retained. Funding rate and associated mark price use Decimal/Numeric storage.
- `funding_coverage` stores the exact half-open window, expected event
  timestamps (duplicates intentionally retained for multiple rate types),
  event count, source, completion flag, and SHA-256 fingerprints of each
  immutable source record.
- A coverage manifest is written only after all funding pages are exhausted
  and the persisted event fingerprints match the full REST response. Reruns
  are idempotent. Conflicting values or missing/tampered rows produce
  UNAVAILABLE/GAP, not zero funding.
- `GET /api/v1/market/backtest-readiness` is read-only and reports both 1m
  candle continuity and exact funding-manifest readiness for BTC/ETH. It never
  enables execution.
- The archive preserves exact funding timestamps in milliseconds. The readiness
  API reports verified coverage and `off_minute_event_count`. Replay checks
  whether any intra-minute settlement and exit require finer timing evidence.
- The verified funding snapshot can be converted to P2-01 engine inputs by
  mapping each record to `FundingEvent(symbol, funding_time_ms, funding_rate,
  mark_price, rate_type)` and using the exact timestamp tuple in
  `FundingCoverage(start_ms, end_ms, timestamps, complete=True)`.

## Remaining limits

- No real 30-day dataset is downloaded as part of CI. All network behavior is
  tested with deterministic mocked public responses.
- The funding endpoint is treated as authoritative for the events it returns;
  the collector exhausts every page and preserves the exact source response.
  It does not synthesize an eight-hour schedule.
- This covers contract-price 1m execution candles and funding-time mark prices.
  Separate mark-price Klines, liquidation simulation, market impact, partial
  fills, and exchange microstructure are out of scope.
- A CI-green collector is not evidence that S01 is profitable. Historical
  backtests remain blocked until both readiness components are READY and the
  strategy has been independently reviewed.
