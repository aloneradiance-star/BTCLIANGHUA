# BAQS Phase 1 / P1-03 — quantitative indicator specification

## Status
Pure, deterministic, **research-only** indicator library. This is not a trading signal or predictor, nor has it demonstrated profitability. No account, real funds, exchange credentials, order execution, or AI decision making.

## Closed bar contract
Use only `FinalCandle` produced by finalized Binance REST data (server clock verified) or WebSocket kline `x=true`. Input must contain **one symbol, one interval, in ascending contiguous UTC open-time order**. A gap/duplicate/out-of-order observation raises `ValueError`, never silently computes across missing time. The engine is independent of network and database. Upstream database adapters must filter `is_final=true`.

Returned `FeatureRow` values are immutable Python `Decimal` or `None` (insufficient warm-up). `as_dict()` serializes Decimal as string to preserve precision.

## Mathematical conventions (version features-v0.1)
| Feature | Initialization and formula | First valid zero-based index |
| --- | --- | --- |
| EMA20 / EMA60 | Seed with simple average of first N closes; then EMA=prior+2/(N+1)*(close-prior) | 19 / 59 |
| RSI14 | Wilder-smoothed price differences. No up/down movement => 50; only gains => 100; only losses => 0 | 14 |
| ATR14 | Wilder TR using previous close for bars 1..14; first ATR=their mean, then (13*prior+TR)/14 | 14 |
| ADX14 | Wilder smoothed +/-DM and TR; first 14 DX at bars 14..27 averaged, then Wilder | 27 |
| +DI / -DI | 100 * smoothed +/-DM / smoothed TR (zero TR => zero DI) | 14 |
| Bollinger20/2 | Closing SMA +/-2 population standard deviations, bandwidth=(upper-lower)/SMA | 19 |
| VWAP UTC session | Sum[(H+L+C)/3*base-volume] / Sum(base-volume), resets UTC 00:00 | 0, if volume>0 |
| Volume ratio20 | Current closed candle's volume divided by **preceding** 20 bar median | 20 |
| Realized volatility20 | Population standard deviation of 20 close-to-close log returns; **not annualized** | 20 |
| BB Width percentile30d | Inclusive empirical rank of current width vs **previous** 2880 valid 15m widths | 2899 |
| Realized Vol percentile30d | Inclusive empirical rank of current RV vs **previous** 2880 valid 15m RVs | 2900 |

Empirical percentile definition: `count(previous_value <= current_value) / 2880`. Not a statistical probability of profitable trades. If the reference window has even one missing/uninitialized observation, output `None`. Percentiles apply only to 15m candles in V1.0; other intervals return `None`.

## Strong anti-lookahead constraint
The feature at time T must not change if newer finalized candles are appended after T. The code must not use any future extrema, future closes, partially formed Klines, or future percentile distribution data.

## Test requirements and blockers
- Fixtures cover monotonic up, down, flat, exact population variance, UTC-day reset, zero-volume, no-lookahead, gaps, and full 30-day rolling warm-up.
- Performance smoke: a complete 2,903-bar 15m feature series (including both 30-day percentile windows) must complete within a generous 10-second CI bound.
- Compare pure function outputs against hand-derivable constants or independent references, not only against a second call of the same function.
- A 30-day Bollinger-width percentile for 15m requires **2,900 contiguous closed candles**; the realized-volatility percentile requires **2,901** because the first realized-volatility value needs 20 prior close-to-close returns. **P1-02 collector currently bootstraps only ~120 bars per feed**. Therefore P1-04 classification must not declare these percentile features ready until a separately bounded, controlled historical backfill exists and passes data continuity checks.
- Long-running live-stream soak tests, production latency, exchange fee models and actual strategy profit validation are outside this phase.

## Next step
P1-04 uses this feature library to implement a versioned, mutually exclusive regime classifier, requiring valid feature readiness; no trade-capable API until independent risk/execution phases are separately authorized.
