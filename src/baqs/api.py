"""Read-only public data API. Absolutely no trading endpoints in Phase 1."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast

from fastapi import FastAPI, HTTPException, Query, Request, Response
from sqlalchemy import Engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from baqs.db import make_engine
from baqs.logging_config import configure_logging
from baqs.market_data.repository import CandleStore, MarketStreamStore
from baqs.market_data.schema import INTERVAL_MS
from baqs.models import Candle, RegimeSnapshot
from baqs.regime.service import RegimeResearchService
from baqs.settings import Settings, get_settings

SYMBOLS = ("BTCUSDT", "ETHUSDT")
TIMEFRAMES = ("1m", "5m", "15m", "1h")

logger = logging.getLogger(__name__)


def _engine(request: Request) -> Engine:
    engine = cast(Engine | None, request.app.state.engine)
    if engine is None:
        engine = make_engine(cast(Settings, request.app.state.settings))
        request.app.state.engine = engine
    return engine


def create_app(*, settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    runtime_settings = settings if settings is not None else get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        configure_logging(runtime_settings.log_level)
        logger.info("application_started")
        yield

    app = FastAPI(
        title="BAQS Phase 1",
        description="Read-only public market research. No orders or account access.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = runtime_settings
    app.state.engine = engine

    @app.get("/api/v1/health")
    def health(request: Request, response: Response) -> dict[str, object]:
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        try:
            database = _engine(request)
            with database.connect() as conn:
                conn.execute(select(Candle).limit(0))
                conn.execute(select(RegimeSnapshot).limit(0))
            store = CandleStore(database)
            candles_ready = all(
                store.health(symbol, timeframe, now_ms)["status"] == "READY"
                for symbol in SYMBOLS
                for timeframe in TIMEFRAMES
            )
            ready = MarketStreamStore(database).health()["status"] == "CONNECTED" and candles_ready
        except (SQLAlchemyError, ImportError, OSError):
            response.status_code = 503
            return {
                "status": "DEGRADED",
                "database": "UNAVAILABLE",
                "market_data": "UNAVAILABLE",
                "execution_enabled": False,
            }
        if not ready:
            response.status_code = 503
        return {
            "status": "DATA_READY" if ready else "DEGRADED",
            "database": "CONNECTED",
            "market_data": "READY" if ready else "UNAVAILABLE",
            "execution_enabled": False,
        }

    @app.get("/api/v1/market/health")
    def market_health(request: Request) -> dict[str, object]:
        observed_at = datetime.now(UTC)
        now_ms = int(observed_at.timestamp() * 1000)
        database = _engine(request)
        store = CandleStore(database)
        return {
            "observed_at": observed_at.isoformat(),
            "collector": MarketStreamStore(database).health(),
            "feeds": {
                symbol: {
                    timeframe: store.health(symbol, timeframe, now_ms) for timeframe in INTERVAL_MS
                }
                for symbol in SYMBOLS
            },
            "execution_enabled": False,
        }

    @app.get("/api/v1/market/history-readiness")
    def history_readiness(request: Request) -> dict[str, object]:
        """Read-only historical completeness gate for future classifier."""
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        store = CandleStore(_engine(request))
        required = {"15m": 2920, "1h": 120}
        return {
            "observed_at": datetime.now(UTC).isoformat(),
            "symbols": {
                symbol: {
                    timeframe: store.history_readiness(
                        symbol,
                        timeframe,
                        (now_ms // INTERVAL_MS[timeframe]) * INTERVAL_MS[timeframe],
                        required_bars=bars,
                        require_latest=True,
                    )
                    for timeframe, bars in required.items()
                }
                for symbol in SYMBOLS
            },
            "execution_enabled": False,
        }

    @app.get("/api/v1/market/candles")
    def candles(
        request: Request,
        symbol: str = Query(default="BTCUSDT"),
        timeframe: str = Query(default="5m"),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, object]:
        if symbol not in SYMBOLS or timeframe not in TIMEFRAMES:
            raise HTTPException(status_code=422, detail="Invalid symbol or timeframe")
        with Session(_engine(request)) as session:
            items = session.scalars(
                select(Candle)
                .where(
                    Candle.symbol == symbol,
                    Candle.timeframe == timeframe,
                    Candle.is_final.is_(True),
                )
                .order_by(Candle.open_time.desc())
                .limit(limit)
            ).all()
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "source": "PERSISTED_FINAL_CANDLES",
            "items": [
                {
                    "open_time": item.open_time.isoformat(),
                    "close_time": item.close_time.isoformat(),
                    "open": str(item.open),
                    "high": str(item.high),
                    "low": str(item.low),
                    "close": str(item.close),
                    "volume": str(item.volume),
                }
                for item in reversed(items)
            ],
        }

    @app.get("/api/v1/market/regimes")
    def regimes(
        request: Request,
        timeframe: str = Query(
            default="5m",
            description=(
                "Supported values: 1m, 5m, 15m, 1h. "
                "The current verified classifier runs on 5m; "
                "other intervals expose historical snapshots."
            ),
        ),
    ) -> dict[str, object]:
        """Return a verified 5m classifier state or explicitly historical snapshots."""
        if timeframe not in TIMEFRAMES:
            raise HTTPException(status_code=422, detail="Invalid timeframe")

        output: dict[str, object] = {}
        if timeframe == "5m":
            clock_ms = int(datetime.now(UTC).timestamp() * 1000)
            service = RegimeResearchService(_engine(request))
            for symbol in SYMBOLS:
                item = service.latest(symbol, clock_ms=clock_ms)
                if item.get("status") == "UNAVAILABLE":
                    item = {**item, "symbol": symbol, "timeframe": timeframe}
                output[symbol] = item
        else:
            with Session(_engine(request)) as session:
                for symbol in SYMBOLS:
                    item = session.scalar(
                        select(RegimeSnapshot)
                        .where(
                            RegimeSnapshot.symbol == symbol,
                            RegimeSnapshot.timeframe == timeframe,
                        )
                        .order_by(
                            RegimeSnapshot.candle_close_time.desc(),
                            RegimeSnapshot.created_at.desc(),
                            RegimeSnapshot.classifier_version.desc(),
                        )
                        .limit(1)
                    )
                    if item is None:
                        output[symbol] = {
                            "status": "UNAVAILABLE",
                            "symbol": symbol,
                            "timeframe": timeframe,
                            "reason": "NO_CLASSIFICATION",
                        }
                    else:
                        output[symbol] = {
                            "status": "HISTORICAL_SNAPSHOT",
                            "symbol": symbol,
                            "timeframe": timeframe,
                            "regime": item.regime,
                            "candle_close_time": item.candle_close_time.isoformat(),
                            "classifier_version": item.classifier_version,
                            "features": item.features,
                            "reasons": item.reasons,
                            "execution_enabled": False,
                        }
        return {
            "observed_at": datetime.now(UTC).isoformat(),
            "timeframe": timeframe,
            "symbols": output,
            "execution_enabled": False,
        }

    return app


app = create_app()
