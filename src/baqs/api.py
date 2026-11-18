"""Read-only public data API. Absolutely no trading endpoints in Phase 1."""

from datetime import datetime
from typing import cast

from fastapi import FastAPI, HTTPException, Query, Request, Response
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from baqs.db import make_engine
from baqs.models import Candle, RegimeSnapshot
from baqs.settings import Settings, get_settings

SYMBOLS = ("BTCUSDT", "ETHUSDT")
TIMEFRAMES = ("1m", "5m", "15m", "1h")


def _engine(request: Request) -> Engine:
    engine = cast(Engine | None, request.app.state.engine)
    if engine is None:
        engine = make_engine(cast(Settings, request.app.state.settings))
        request.app.state.engine = engine
    return engine


def create_app(*, settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    app = FastAPI(
        title="BAQS Phase 1",
        description="Read-only public market research. No orders or account access.",
        version="0.1.0",
    )
    app.state.settings = settings if settings is not None else get_settings()
    app.state.engine = engine

    @app.get("/api/v1/health")
    def health(request: Request, response: Response) -> dict[str, object]:
        try:
            with _engine(request).connect() as conn:
                conn.execute(text("SELECT 1"))
        except (SQLAlchemyError, ImportError, OSError):
            response.status_code = 503
            return {
                "status": "DEGRADED",
                "database": "UNAVAILABLE",
                "market_data": "NOT_IMPLEMENTED",
                "execution_enabled": False,
            }
        return {
            "status": "FOUNDATION_READY",
            "database": "CONNECTED",
            "market_data": "NOT_IMPLEMENTED",
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
    def regimes(request: Request) -> dict[str, object]:
        output: dict[str, object] = {}
        with Session(_engine(request)) as session:
            for symbol in SYMBOLS:
                item = session.scalar(
                    select(RegimeSnapshot)
                    .where(RegimeSnapshot.symbol == symbol)
                    .order_by(RegimeSnapshot.candle_close_time.desc())
                    .limit(1)
                )
                if item is None:
                    output[symbol] = {"status": "UNAVAILABLE", "reason": "NO_CLASSIFICATION"}
                else:
                    output[symbol] = {
                        "status": "HISTORICAL_SNAPSHOT",
                        "regime": item.regime,
                        "candle_close_time": item.candle_close_time.isoformat(),
                        "classifier_version": item.classifier_version,
                        "features": item.features,
                        "reasons": item.reasons,
                    }
        return {"observed_at": datetime.now().astimezone().isoformat(), "symbols": output}

    return app


app = create_app()
