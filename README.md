# BAQS — BTC/ETH Adaptive Quant System

Research infrastructure for public Binance USDⓈ-M futures market data. **Not a live trading bot.** No private API clients or execution endpoints exist in Phase 1.

## Scope and safeguards
- Symbols: `BTCUSDT`, `ETHUSDT`.
- Default mode: `PAPER`. A `BAQS_LIVE_ENABLED=true` value makes startup fail.
- Read-only API; no exchange keys, no account access, no order placement.
- The health endpoint returns `503 DEGRADED` when the required database schema cannot be read.
- Rules and thresholds are research hypotheses, not validated profits.
- Do not use this project to circumvent account eligibility or jurisdictional restrictions.

## Local development (Python 3.12+)
```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
docker compose up -d db
alembic upgrade head
uvicorn baqs.api:app --reload
```
Open `http://127.0.0.1:8000/docs`. API endpoints are read-only:
`GET /api/v1/health`, `GET /api/v1/market/health`,
`GET /api/v1/market/history-readiness`, `GET /api/v1/market/candles`, and
`GET /api/v1/market/regimes?timeframe=5m`. The regime endpoint defaults to
`5m`, accepts `1m`, `5m`, `15m`, or `1h`, and only returns a snapshot for
the requested timeframe; a missing timeframe is explicitly `UNAVAILABLE`.
Application logs in the `baqs` namespace are JSON lines on stderr. Uvicorn
access/error logging keeps the server's own logging configuration.

The database begins empty. Public market ingestion is implemented in Issue #2,
historical coverage is gated by Issue #9, and market classification remains Issue #4.
The history-readiness endpoint reports candle coverage only, not alpha or trading signals.
On empty data, endpoints must report unavailable data, not fabricated signals.

## Opt-in public Binance market collector
After `alembic upgrade head`, in a separate process run:
```bash
python -m baqs.market_data.runner
```
Only public REST time/klines and public WebSocket `/market/stream` are used.
This research collector is **not enabled automatically** by the API server.
See `docs/P1-02_PUBLIC_DATA.md` for finalized-candle rules, gap repair and
limitations. The `GET /api/v1/market/health` endpoint reports the collector
connection status and per-feed candle quality. `GET /api/v1/health` returns
HTTP 503 when the collector is unavailable or any feed has missing, stale
or gapped data.

## Docker development
```bash
docker compose up --build
```
The api container applies the Phase 1 schema then starts port 8000. The database credentials in the compose file are local development examples, **not production credentials**.

## Tests
```bash
python -m pytest -q
ruff check .
ruff format --check .
mypy src
```
Tests use offline SQLite fixtures; no network or Binance credentials are required.
Migration smoke test (SQLite) using a temporary file:
```bash
BAQS_DATABASE_URL=sqlite:////tmp/baqs-migration-smoke.db alembic upgrade head
```

## Engineering
See `AGENTS.md`, `docs/PHASE1_CODEX_BRIEF.md` and Issue #1. Schema migrations live in `migrations/`. Phase 1 now includes public REST/WebSocket ingestion, deterministic indicators and a research-only six-state classifier. A real-money phase requires separate risk, compliance and user authorization gates.

## Phase 1 / Six-state market regime (Issue #4)
After completing the **explicit public 30-day backfill** (see
`docs/P1-02A_HISTORY_READINESS.md`) and running the public data collector,
manually run one research observation:
```bash
python -m baqs.regime.runner --once
```
Optional continuous research-only polling:
```bash
python -m baqs.regime.runner --poll-seconds 15
```
`GET /api/v1/market/regimes?timeframe=5m` returns the latest verified
research state or `UNAVAILABLE` with a reason when the latest snapshot or
required candles are unavailable, or the collector connection is unhealthy.
Other supported timeframe values expose historical snapshots only. Output is
**not** a trade order, not a calibrated probability and not an investment
recommendation. See `docs/P1-04_REGIME_CLASSIFIER.md`.

## Phase 2 / P2-01 offline S01 backtesting

P2-01 adds a pure Decimal event-driven simulator and the research-only
`S01TrendPullbackStrategy`. See `docs/P2-01_BACKTEST_ENGINE.md`.
Signals use completed 15m/1h bars and may fill no earlier than the next 1m
open. Fee, adverse slippage and a complete funding-event manifest are required.
No HTTP write endpoint, private exchange API, live order or leverage is present.
P1-02A provides public 15m/1h history; P2-02 adds explicit 1m execution
bars and historical funding/mark-price events for backtests, subject to the
backtest-readiness gate. A CI pass is not a profitability result.

## P2-02 / Public historical backtest data

Manual public 1m execution-history backfill (default 43,200 bars / 30 days):
```bash
python -m baqs.market_data.backfill --timeframe 1m --symbols BTCUSDT ETHUSDT
```
Use the exact `start_ms` and `anchored_end_ms` printed in the candle backfill
report for the matching funding window:
```bash
python -m baqs.market_data.funding_backfill --start-ms <START_MS> --end-ms <END_MS>
```
The official public funding endpoint supplies `fundingRate`, `fundingTime`,
associated `markPrice` and `rateType`. Readiness is exposed at
`GET /api/v1/market/backtest-readiness?start_ms=...&end_ms=...`.
Missing 1m candles, missing funding rows, altered source fingerprints or an
incomplete coverage manifest keep the result UNAVAILABLE. No private API or
trading order path is introduced.

## P2-03 / S01 historical research replay

See [the historical replay guide](docs/P2-03_S01_HISTORICAL_BACKTEST.md) for
the exact data window, indicator warm-up, funding manifest, and
`python -m baqs.backtest.research` command with explicit costs.
Run the offline acceptance tests with:

```bash
python -m pytest -q tests/test_s01_research.py
```
