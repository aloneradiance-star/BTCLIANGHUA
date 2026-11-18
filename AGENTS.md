# BAQS — Codex repository instructions

## Mission
Build BAQS V1.0, a research-first BTCUSDT/ETHUSDT Binance USDⓈ-M perpetual-futures quantitative system. The project owner is the user; ChatGPT is the product/architecture and acceptance lead; Codex is the implementation engineer. All trading strategies and thresholds are **unvalidated research hypotheses**, not profit guarantees.

## Safety and development boundary (mandatory)
- **Phase 1 is public-market-data-only. No live trading, authenticated trading requests, private API keys, account access, wallet operations, order placement, or leverage changes.**
- The default environment is `PAPER` with `LIVE_ENABLED=false`; this must remain false in Phase 1. Fail closed on missing or invalid configuration.
- Never request, store, print, commit, or transmit real API keys, API secrets, passwords, or user-specific account data.
- Do not implement VPN/geo-bypass, prohibited account access, or workarounds for exchange eligibility. Binance.com Futures access must be confirmed separately before any future real-money phase.
- Keep strategy decisions deterministic and auditable; do not let an LLM place orders or override risk controls.
- No background job should be claimed as running unless actually scheduled and verified. Never mark a task complete without tests.

## Phase 1 scope
1. Bootstrap Python 3.12+ package, typed configuration, structured logging, README, Docker Compose for PostgreSQL, CI for lint/type/tests.
2. Persist **public** 1m, 5m, 15m, 1h BTCUSDT and ETHUSDT finalized market candles with UTC timestamps and idempotent database upserts.
3. Implement REST backfill, public WebSocket ingestion and reconnect, duplicate suppression, gap detection, and final-candle gating.
4. Implement reproducible EMA(20/60), Wilder RSI(14), ATR(14), ADX(14), Bollinger(20,2), UTC-session VWAP, volume ratio, rolling BB-width percentile. No look-ahead.
5. Classify one of BULL_TREND, BEAR_TREND, RANGE, BREAKOUT, HIGH_RISK, TRANSITION using versioned, explainable rules; no pseudo-probabilities.
6. Expose **read-only** /health, /market/regimes, /market/candles diagnostics with unit/integration tests and API docs.

No real-money mode, execution engine, private account synchronization, or trading UI in this phase. Stubs may define interfaces but must not call private endpoints.

## Implementation standards
- Prefer a modular monolith. Python 3.12+, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, PostgreSQL 16+, pytest, Ruff, mypy; Polars/Pandas optional where justified.
- Use `Decimal`/PostgreSQL `NUMERIC` for persisted prices and amounts; do not make final money arithmetic rely on float.
- Use UTC-aware datetimes, exchange event time and local ingest time separately. Store provenance and completion status for candles.
- Keep network I/O in exchange adapters; core feature and regime functions are pure/testable and avoid remote calls.
- The API must not expose secrets, controls to trade, or writable state in Phase 1.
- Include fixture-based offline tests (CI must never need Binance network or credentials).
- On missing data, stale candles, sequence gaps, or disconnected streams, flag unhealthy; do not silently generate actionable signals.
- Distinguish market-regime state from risk/system halt. Do not publish a model-derived win probability without out-of-sample calibration.
- Pin or constrain dependencies; check public API semantics against official Binance USDⓈ-M docs before use.

## Working agreement
- Read `docs/PHASE1_CODEX_BRIEF.md` before modifying code.
- Implement one GitHub issue per branch and submit a **draft pull request** to `main` with a checklist, migration notes, test commands/results, and limitations.
- First deliver a short plan, then code and tests, then run the tests, then list changed files and remaining gaps.
- Do not merge your own PR or promote environments without human approval.
- Prefer narrowly scoped changes, no speculative AI models or auto-trading infrastructure in Phase 1.
- Treat unknown API responses and exchange edge cases as untrusted input; validate and log safely.

## Definition of done for any code task
- Deterministic behavior, type checks and unit tests pass.
- `README.md` documents exact local run and test commands.
- CI uses fixtures and passes without credentials.
- No live trading endpoints or credential requirements were introduced.
- New behaviors have failure-case tests and observability.
- PR explicitly says what is implemented vs. not implemented.
