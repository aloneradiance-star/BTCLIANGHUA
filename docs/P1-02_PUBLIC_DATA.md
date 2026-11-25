# P1-02 — Binance public USD-M market-data collector

Research-only data ingestion. The only remote calls made by the collector:
- REST GET `https://fapi.binance.com/fapi/v1/time`
- REST GET `https://fapi.binance.com/fapi/v1/klines`
- WSS `wss://fstream.binance.com/market/stream?streams=...`

**No API keys, signed endpoints, order requests, user streams, positions, margin or account access.** No strategy in this phase places orders.

## Data conventions
- BTCUSDT and ETHUSDT; `1m`, `5m`, `15m`, `1h`.
- UTC timestamps, aligned to candle opening times.
- Binance documents REST/WS close timestamps as inclusive milliseconds (`T`), but local `close_time` stores the **exclusive interval boundary** (`T+1ms`).
- REST may include an unfinished live candle. Accept only if `close_ms <= serverTime`.
- WebSocket provisional messages with `k.x != true` are ignored.
- The collector rejects a WebSocket candle whose close boundary is later than
  its local UTC receipt time. Reconnect reconciliation stays unhealthy when
  a stored finalized candle extends beyond the exchange clock.
- Invalid OHLCV, unsupported symbol, invalid stream symbol or missing bars fail closed.
- A finalized candle is immutable; conflicting duplicate REST/WS data raises `CandleConflict` and requires investigation. Do not silently rewrite historical facts.
- All financial values are persisted as PostgreSQL `NUMERIC(28,12)` and parsed via Decimal.
- Health reports `READY`, `NO_DATA`, `STALE` or `GAP`. A future close boundary
  prevents readiness. Each report checks the most recent 240 finalized candles;
  more distant historical completeness is **not** guaranteed by that window alone.

## Run
```bash
# First migrate the database:
docker compose up -d db
alembic upgrade head

# Explicitly enable ONLY the public data collector:
python -m baqs.market_data.runner
```
The research runner does not start automatically when the API service starts. Its runtime is restricted to `PAPER`. Run one collector instance per database; concurrent multi-process writes are not supported in Phase 1.

A clean start fetches a bounded last-120-bar REST history for each feed after establishing the WebSocket. On reconnect, re-fetch overlapping finalized history and repair detected gaps. Bounded repair limits prevent unbounded burst traffic after prolonged outages. When the range exceeds the budget, ingestion stays unhealthy and requires controlled manual backfill.

Normal WebSocket closures and connection failures both delay the next attempt. Consecutive retries start at one second and increase up to 30 seconds. Processing a finalized candle resets the delay. When the REST clock only covers part of a gap, the collector retries reconciliation before storing the later WebSocket candle.

The offline collector-flow test starts a loopback WebSocket and mocks public REST.
It exercises normal closure, an abnormal closure and a simulated six-hour gap,
then checks exact persisted coverage for all eight feeds.

`GET /api/v1/market/health` exposes per-feed candle quality and a separate
`collector` object for the combined public connection. The collector records
receipt of a processed text frame in PostgreSQL after reconnect reconciliation
completes, then refreshes the timestamp at most every five seconds. Closure
or a handled failure clears the timestamp. A silent socket is retried after 30
seconds without a frame, and the API treats a receipt 30 seconds old as stale
even if the process stopped without clearing it. Overall readiness requires
`collector.status == "CONNECTED"` and eight ready candle feeds.

A clean database reports `NOT_STARTED`. After the first connection attempt,
the collector reports `DISCONNECTED` until it processes a frame after
reconciliation. A lapsed or future receipt reports `STALE`.

Migration `0002_market_stream` adds the collector state table. Apply it with
`alembic upgrade head` before starting the API and collector. Downgrading to
`0001_public` removes collector state and preserves candles and regime snapshots.

`GET /api/v1/market/candles` returns finalized bars whose close time has arrived. No price predictions or trades are returned.

## API references (reviewed November 2026)
- https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data
- https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/market

## Future engineering
Add an advisory DB lock to prohibit multiple concurrent collectors, configurable bounded initial backfill, durable health event table, live stream outage soak tests and comprehensive historical reconciliation. Current API shows the most recent 240-bar quality window, not a historical certification.
