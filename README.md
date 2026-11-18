# BAQS — BTC/ETH Adaptive Quant System

Research infrastructure for public Binance USDⓈ-M futures market data. **Not a live trading bot.** No private API clients or execution endpoints exist in Phase 1.

## Scope and safeguards
- Symbols: `BTCUSDT`, `ETHUSDT`.
- Default mode: `PAPER`. A `BAQS_LIVE_ENABLED=true` value makes startup fail.
- Read-only API; no exchange keys, no account access, no order placement.
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
`GET /api/v1/health`, `GET /api/v1/market/candles`, and `GET /api/v1/market/regimes`.
The database begins empty; **market ingestion is Issue #2**, and regimes are Issue #4.
On empty data, endpoints must report unavailable data, not fabricated signals.

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
See `AGENTS.md`, `docs/PHASE1_CODEX_BRIEF.md` and Issue #1. Schema migrations live in `migrations/`. Later phases will add public REST/WebSocket ingestion, deterministic indicators and state classification. A real-money phase requires separate risk, compliance and user authorization gates.
