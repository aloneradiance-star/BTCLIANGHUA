# BAQS Phase 1 / P1-04 six-state regime classifier

**Research-only.** Explicitly no order placing, Binance account authentication,
trading signals, leverage modification, risk approval, or calibrated probability.

## Running
After migrating PostgreSQL and completing a separate opt-in 2920-bar 15m +
120-bar 1h public historical backfill (P1-02A), keep the P1-02 public
collector running for finalized 5m/15m/1h bars.

```bash
python -m baqs.regime.runner --once
# or manually enable a read-only loop:
python -m baqs.regime.runner --poll-seconds 15
```

The loop is deliberately **not** auto-started with the read-only web API.
Use only one classifier writer per DB. A unique regime snapshot index prevents
duplicates but this is not a distributed lock.

`GET /api/v1/market/regimes?timeframe=5m` shows BTCUSDT and ETHUSDT independent
verified classifier states. The accepted `1m`, `15m` and `1h` values expose only
explicitly labelled historical snapshots; the current v0.1 classifier runs on 5m.
Returns UNAVAILABLE with a specific reason for missing/gapped or stale data.
The runner uses UTC wall clock; keep the host NTP-synchronized or it will fail closed.

## Required inputs and clocks
- Every evaluation occurs on a **closed** 5m UTC candle. Confirmation uses
  three *distinct, consecutive* closed 5m observations.
- 15m inputs must have 2920 **contiguous finalized** bars, through the latest
  15m close at or before the evaluated 5m close; the 30-day reference window
  contains 2880 prior 15m bars and indicator warm-up.
- 1h inputs must have 120 consecutive finalized bars, through the latest 1h
  close at or before the evaluation's 5m close.
- 5m inputs must contain three finalized contiguous bars ending at the
  evaluated 5m close. Duplicate 5m observations do not update the gate twice.
- No future 15m/1h bar may affect an earlier 5m classification. Readiness also rejects untrusted candle sources, missing quote-volume/trade-count metadata and incorrect exclusive close boundaries.
- If missing history/indicators, return UNAVAILABLE rather than TRANSITION.
  TRANSITION is a valid **market state** only on otherwise healthy data.

## Priority and v0.1 rules (not validated alpha)
1. **HIGH_RISK:** 20-return realized volatility above zero and its 30-day
   *past-only* empirical percentile >=0.90. Optional public spread values
   may also trigger when >=3x their recent median; no values are synthesized
   when the trusted spread stream has not been implemented.
2. **BREAKOUT:** one of the *preceding three* 15m BB width percentiles <=0.20,
   a finalized 15m close beyond the previous 20 finalized 15m-bar high/low
   by >0.15 ATR14, plus volume > or equal to 1.5x the preceding 20-bar
   median. Direction is logged as BREAKOUT_UP or BREAKOUT_DOWN but the
   regime itself remains one exclusive BREAKOUT state.
3. **BULL_TREND:** 15m and 1h EMA20>EMA60, 15m ADX14>23, and 15m EMA20
   is higher than three finalized 15m candles previously.
4. **BEAR_TREND:** symmetrical.
5. **RANGE:** 15m ADX14<18; previous 20-bar price band at least 0.7% wide;
   current last close inside it; both upper and lower boundaries show at
   least two *confirmed* tests separated by >=3 15m bars. A test is a
   wick within 0.25 ATR of the edge, followed by >=0.5 ATR reversal in
   a completed candle within the next three historical 15m bars.
6. **TRANSITION:** no valid trend, breakout or range on valid, complete data.

High-risk overrides all other states immediately. All other state changes
require >=3 consecutive 5m confirmations, with 10m cooling after the last
switch. Startup begins in no-trade TRANSITION until confirmed; a missed
**processing** interval resets state and pending confirmations to
TRANSITION. This differs from gaps in market data, which cause UNAVAILABLE.

## Audit / persistence
Each completed 5m classification is appended as a `regime_snapshots`
record referencing the finalized source 5m candle (existing FK), with
classifier version `rules-0.1`, exclusive candle close time, candidate
regime, final held regime, reason codes, 15m/1h source close boundaries,
and a serialized restart-safe gate state in JSONB features. No probability
field is populated. Exact duplicate requests return the existing snapshot;
out-of-order observations and corrupt gate state fail closed.

## Important limitations / review gate
- The same 15m regime candidate may be carried over three consecutive 5m
  closes; this is temporal confirmation, **not** three independent 15m signals.
- High-risk spread anomaly requires a separately verified public spread feed
  and is not silently assumed present in this version.
- Historical empirical percentiles are **not** predictive win probabilities.
  High-risk triggers may require adjustment against a validated volatility
  baseline; backtest and forward testing are outside Phase 1.
- Rule thresholds and EMA conventions are initial research defaults.
- No historical out-of-order rewrite or concurrent multi-writer support.
- Computing full 2920-bar Decimal features every 5m is correct but not yet
  performance-optimized. Cache/benchmark before any production operation.
- A CI-green pure classifier does not prove robust real-time trading quality.
  Before Phase 2, do controlled market-data soak tests and verify classifier
  outputs across historical bull/bear/range/volatility phases.

## Example read-only response

Illustrative shape for `GET /api/v1/market/regimes?timeframe=5m` (values are
examples, not live market observations):

```json
{
  "observed_at": "2026-11-18T12:15:01+00:00",
  "timeframe": "5m",
  "symbols": {
    "BTCUSDT": {
      "status": "READY",
      "symbol": "BTCUSDT",
      "regime": "TRANSITION",
      "timeframe": "5m",
      "candle_close_time": "2026-11-18T12:15:00+00:00",
      "classifier_version": "rules-0.1",
      "features": {
        "candidate": "TRANSITION",
        "signal_allowed": false,
        "probability_calibrated": false
      },
      "reasons": ["NO_VALID_MARKET_STATE"],
      "execution_enabled": false
    },
    "ETHUSDT": {
      "status": "UNAVAILABLE",
      "symbol": "ETHUSDT",
      "timeframe": "5m",
      "reason": "NO_CLASSIFICATION"
    }
  },
  "execution_enabled": false
}
```

## Offline end-to-end fixture demo

Run the deterministic no-network replay, including persistence, three distinct
5m confirmations, current read-only state and duplicate idempotency:

```bash
python -m pytest tests/test_regime_service.py::test_end_to_end_range_switch_requires_three_5m_closes -q
```
