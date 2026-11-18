# BAQS V1.0 — Phase 1 Codex implementation brief

**Status:** approved for *research infrastructure development only*; no real-funds trading approval.
**Repository:** `aloneradiance-star/BTCLIANGHUA`.
**Phase:** P1: market data + indicators + regime classification + read-only API.
**Assets:** BTCUSDT / ETHUSDT public USDⓈ-M futures market data.
**Intervals:** 1m, 5m, 15m, 1h (1m feed and 5/15/1h analytics).
**Clock:** UTC; use only finalized candles for state and decision logic.

## Product acceptance
- A clean clone can start Postgres and the backend using documented commands; CI passes offline.
- Historical candle REST backfill and live public market WebSocket ingestion work; no private exchange access.
- Candle persistence is idempotent, duplicates handled, missing intervals detected, and incomplete candles excluded.
- Core indicators are reproducible against independent fixture/reference calculations; no future leakage.
- Latest market regime for each symbol is returned via read-only API with timestamp, classifier version, feature snapshot and explanations; on data-quality failure report unavailable/unhealthy rather than fake certainty.
- Evidence: tests, logs, schema migrations, deterministic fixtures and example response payloads.

## Architecture and task order
1. Foundation: packaging, runtime config defaults, typed settings, safe logging, local Postgres, Alembic migration, CI.
2. Public data: exchange info and kline adapters, timestamp validation, websocket client, persistent candle repository, rest gap/backfill.
3. Features: EMA20/60; Wilder RSI14, ATR14, ADX14; Bollinger(20,2); UTC anchored VWAP; volume median ratio; 30-day rolling BB width percentile.
4. Regime rules + read-only API. Strategy execution is strictly out of scope in Phase 1.

## Data contract
`candles(symbol, timeframe, open_time, close_time, open, high, low, close, volume, quote_volume, trade_count, is_final, source, ingested_at)` unique PK `(symbol,timeframe,open_time)`. Prices and quantities are numeric, times timezone-aware. Detect gaps per interval and store health counters. Prefer per-candle version/hash or immutable finalized updates policy and log revisions.

`regime_snapshots(symbol, candle_close_time, regime, classifier_version, features, reasons, created_at)` with a unique key over symbol, timeframe/candle close and version. If using JSONB features, never save credentials or sensitive account values.

## Regime v0.1 rules — *research defaults, not validated alpha*
Priority:
1. System unready/stale/missing data => UNAVAILABLE health (not a trade signal).
2. HIGH_RISK: rolling realized-volatility >= own past 30-day 90th percentile, or spread >= 3x trailing median **only if reliable spread feed exists**. Missing spread feed must not be fabricated.
3. BREAKOUT candidate: recent BB width percentile <= 20th in 30-day *past* observations, then closed 15m candle > prior 20 closed-candle high (+0.15*ATR) or < prior 20 low (-0.15*ATR), with volume >= 1.5x preceding 20-candle median. Define compression in prior candles to avoid contradiction.
4. BULL_TREND: 1h and 15m EMA20 > EMA60, ADX15m >23, EMA20_15m rising over past three closed 15m bars.
5. BEAR_TREND: symmetrical.
6. RANGE: ADX15m <18 and quantitatively verified 20-bar range with at least two separated tests of each side; document the test logic.
7. TRANSITION default.

Normal regime switching: require persistence of signal over 3 *closed 5-minute* bars and a 10-minute cooling period; HIGH_RISK can take effect immediately. Confidence in v0.1 is **not a calibrated probability**; expose rule strength or reasons instead of probabilistic percentages.

## Mandatory test classes
- Indicator parity with independent calculation fixtures, warm-up and NaN/empty handling.
- No future candle access: appending future data cannot change historical feature snapshots.
- Candle out-of-order, duplication, final/nonfinal update, outages, REST gap filling.
- Regime priority ties, low sample count, stale feed, startup warmup, cooldown, persistence.
- Invalid parameters fail closed; no live trading configuration or authenticated trading request can be made.
- Repeatable startup, deterministic local fixture demo, read-only API.
- CI formatting/type/test gates.

## Research and security notes
- Do not assume exchange API format without consulting official Binance public USDⓈ-M docs. Avoid fragile third-party wrappers when direct public endpoints suffice.
- Deployment eligibility and account/region permissions must be reviewed before any future live implementation. Do not circumvent restrictions.
- No leverage, margin, position, order, Algo endpoint, user stream or trading API code in Phase 1.
- Do not promise precision or profitability. Short-term signal performance must later be checked out of sample with realistic fees, slippage, funding, partial fills and adverse conditions.

## Codex issue plan
- P1-01 Foundation + schema + CI (implement first)
- P1-02 Public REST/WebSocket market-data ingestion + quality
- P1-03 Reproducible indicator computations
- P1-04 Regime classifier + read-only API + end-to-end fixture demo

Each task goes on its own branch and draft PR; the architecture lead reviews changes against acceptance criteria and failure-case coverage.
