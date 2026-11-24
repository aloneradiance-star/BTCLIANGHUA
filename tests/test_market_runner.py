"""Collector reconnect pacing on normal WebSocket closure."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import Engine

from baqs.market_data import runner
from baqs.market_data.schema import FinalCandle
from baqs.settings import Settings


def test_normal_websocket_closures_back_off_and_reset_on_a_candle(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    connections = iter(([], [], ["{}"], []))
    delays: list[float] = []
    candle = FinalCandle(
        "BTCUSDT",
        "1m",
        0,
        60_000,
        Decimal(100),
        Decimal(101),
        Decimal(99),
        Decimal(100),
        Decimal(1),
        Decimal(100),
        1,
        "WS",
    )

    async def frames(messages: list[str]) -> AsyncIterator[str]:
        for message in messages:
            yield message

    @asynccontextmanager
    async def connect(*args: object, **kwargs: object) -> AsyncIterator[AsyncIterator[str]]:
        messages = next(connections, None)
        if messages is None:
            raise asyncio.CancelledError
        yield frames(messages)

    async def sleep(delay: float) -> None:
        delays.append(delay)

    service = Mock(bootstrap_all=AsyncMock(), ingest=AsyncMock(return_value="CREATED"))
    settings = Settings(_env_file=None, database_url="sqlite:///:memory:")
    monkeypatch.setattr(runner, "get_settings", lambda: settings)
    monkeypatch.setattr(runner, "configure_logging", lambda _level: None)
    monkeypatch.setattr(runner, "make_engine", lambda _settings: db_engine)
    monkeypatch.setattr(runner, "PublicMarketDataService", Mock(return_value=service))
    monkeypatch.setattr(runner, "connect", connect)
    monkeypatch.setattr(runner, "from_ws_message", Mock(return_value=candle))
    monkeypatch.setattr(runner.asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runner.run_collector())

    assert delays == [1.0, 2.0, 1.0, 2.0]
    assert service.bootstrap_all.await_count == 4
    service.ingest.assert_awaited_once_with(candle)
