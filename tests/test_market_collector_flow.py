"""End-to-end collector recovery using a loopback WebSocket and mocked public REST."""

import asyncio
import json
from contextlib import suppress
from datetime import UTC

import httpx
import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session
from websockets.asyncio.server import ServerConnection, serve

from baqs.market_data import runner
from baqs.market_data.repository import CandleStore
from baqs.market_data.schema import INTERVAL_MS, SYMBOLS
from baqs.models import Candle
from baqs.settings import Settings

MINUTE = INTERVAL_MS["1m"]
HOUR = INTERVAL_MS["1h"]
ANCHOR = 1_700_000_000_000 // HOUR * HOUR
RECOVERY = ANCHOR + 6 * HOUR
FINAL_OPEN = RECOVERY + 2 * MINUTE


def _rest_row(open_ms: int, step: int) -> list[object]:
    return [open_ms, "100", "103", "99", "101", "5", open_ms + step - 1, "500", 10]


def _ws_frame(open_ms: int, *, closed: bool = True) -> str:
    return json.dumps(
        {
            "stream": "btcusdt@kline_1m",
            "data": {
                "e": "kline",
                "s": "BTCUSDT",
                "k": {
                    "s": "BTCUSDT",
                    "i": "1m",
                    "x": closed,
                    "t": open_ms,
                    "T": open_ms + MINUTE - 1,
                    "o": "100",
                    "h": "103",
                    "l": "99",
                    "c": "101",
                    "v": "5",
                    "q": "500",
                    "n": 10,
                },
            },
        }
    )


def test_collector_recovers_all_feeds_after_websocket_closures(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        clocks = (ANCHOR, RECOVERY, FINAL_OPEN)
        last_feed_requested = {clock: asyncio.Event() for clock in clocks}
        clock_ms = ANCHOR
        connection_index = -1
        store = CandleStore(db_engine)
        client = httpx.AsyncClient

        def respond(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET" and "authorization" not in request.headers
            assert "x-mbx-apikey" not in request.headers and "signature" not in request.url.params
            now_ms = clock_ms
            if request.url.path == "/fapi/v1/time":
                return httpx.Response(200, json={"serverTime": now_ms})
            assert request.url.path == "/fapi/v1/klines"
            params = request.url.params
            symbol, timeframe = params["symbol"], params["interval"]
            assert symbol in SYMBOLS
            step = INTERVAL_MS[timeframe]
            start, end = int(params["startTime"]), int(params["endTime"]) + 1
            opens = range(start, end, step)[: int(params["limit"])]
            assert opens and opens[-1] + step <= now_ms
            if symbol == "ETHUSDT" and timeframe == "1h":
                last_feed_requested[now_ms].set()
            return httpx.Response(200, json=[_rest_row(open_ms, step) for open_ms in opens])

        async def serve_connection(socket: ServerConnection) -> None:
            nonlocal clock_ms, connection_index
            connection_index += 1
            current = connection_index
            await last_feed_requested[clocks[current]].wait()
            if current == 0:
                await socket.send(_ws_frame(ANCHOR))
                await socket.send(_ws_frame(ANCHOR + MINUTE, closed=False))
                clock_ms = RECOVERY
                await socket.close(code=1000)
            elif current == 1:
                await socket.send(_ws_frame(ANCHOR + HOUR))  # Already recovered via REST.
                await socket.send(_ws_frame(RECOVERY))
                clock_ms = FINAL_OPEN
                await socket.close(code=1011)
            else:
                await socket.send(_ws_frame(FINAL_OPEN))
                await socket.wait_closed()

        settings = Settings(_env_file=None, database_url="sqlite:///:memory:")
        monkeypatch.setattr(runner, "get_settings", lambda: settings)
        monkeypatch.setattr(runner, "configure_logging", lambda _level: None)
        monkeypatch.setattr(runner, "make_engine", lambda _settings: db_engine)
        monkeypatch.setenv("NO_PROXY", "127.0.0.1")
        monkeypatch.setattr(
            runner.httpx,
            "AsyncClient",
            lambda **kwargs: client(
                **kwargs, transport=httpx.MockTransport(respond), trust_env=False
            ),
        )

        async with serve(serve_connection, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            monkeypatch.setattr(runner, "combined_stream_url", lambda: f"ws://127.0.0.1:{port}")
            collector = asyncio.create_task(runner.run_collector())
            try:
                async with asyncio.timeout(25):
                    while store.latest_open_ms("BTCUSDT", "1m") != FINAL_OPEN:
                        if collector.done():
                            collector.result()
                            pytest.fail("Collector stopped before recovery")
                        await asyncio.sleep(0.01)

                assert connection_index == 2
                with Session(db_engine) as session:
                    ws_opens = session.scalars(
                        select(Candle.open_time)
                        .where(Candle.source == "WS")
                        .order_by(Candle.open_time)
                    ).all()
                    assert [
                        int(value.replace(tzinfo=UTC).timestamp() * 1000) for value in ws_opens
                    ] == [ANCHOR, RECOVERY, FINAL_OPEN]
                    for symbol in sorted(SYMBOLS):
                        for timeframe, step in INTERVAL_MS.items():
                            end = FINAL_OPEN // step * step
                            if symbol == "BTCUSDT" and timeframe == "1m":
                                end += MINUTE
                            expected = 120 + (end - ANCHOR) // step
                            count = session.scalar(
                                select(func.count())
                                .select_from(Candle)
                                .where(Candle.symbol == symbol, Candle.timeframe == timeframe)
                            )
                            assert count == expected, (symbol, timeframe)
                            assert store.latest_open_ms(symbol, timeframe) == end - step
                            assert store.gap_ranges(symbol, timeframe, window=expected) == []
                            assert (
                                store.health(symbol, timeframe, FINAL_OPEN + MINUTE)["status"]
                                == "READY"
                            )
            finally:
                collector.cancel()
                with suppress(asyncio.CancelledError):
                    await collector

    asyncio.run(run())
