"""Deterministic feature computation from complete, contiguous public candles.

Feature rows are research observations, never an action/order or calibrated probability.
"""

from collections.abc import Sequence
from dataclasses import dataclass, fields
from decimal import Decimal

from baqs.indicators.core import (
    bollinger,
    ema,
    past_volume_ratio,
    realized_volatility,
    trailing_percentile,
    utc_session_vwap,
    wilder_atr_adx,
    wilder_rsi,
)
from baqs.market_data.schema import INTERVAL_MS, SYMBOLS, FinalCandle


@dataclass(frozen=True, slots=True)
class FeatureRow:
    symbol: str
    timeframe: str
    open_ms: int
    close_ms: int
    ema20: Decimal | None
    ema60: Decimal | None
    rsi14: Decimal | None
    atr14: Decimal | None
    adx14: Decimal | None
    plus_di14: Decimal | None
    minus_di14: Decimal | None
    bb_mid20: Decimal | None
    bb_upper20: Decimal | None
    bb_lower20: Decimal | None
    bb_width20: Decimal | None
    bb_width_percentile_30d: Decimal | None
    vwap_utc: Decimal | None
    volume_ratio20: Decimal | None
    realized_vol20: Decimal | None
    realized_vol_percentile_30d: Decimal | None

    def as_dict(self) -> dict[str, str | int | None]:
        """JSON-safe presentation with exact Decimal string values."""
        result: dict[str, str | int | None] = {}
        for field in fields(self):
            value = getattr(self, field.name)
            result[field.name] = str(value) if isinstance(value, Decimal) else value
        return result


def _validate_bars(bars: Sequence[FinalCandle]) -> None:
    """One symbol/timeframe, complete closed bars; never bridge missing intervals."""
    if not bars:
        return
    symbol, timeframe = bars[0].symbol, bars[0].timeframe
    if symbol not in SYMBOLS or timeframe not in INTERVAL_MS:
        raise ValueError("Unexpected market or interval")
    step = INTERVAL_MS[timeframe]
    for i, bar in enumerate(bars):
        if bar.symbol != symbol or bar.timeframe != timeframe:
            raise ValueError("Cannot combine symbols or timeframes")
        if bar.close_ms != bar.open_ms + step:
            raise ValueError("Invalid exclusive candle close")
        if i and bar.open_ms != bars[i - 1].open_ms + step:
            raise ValueError("Bars are missing, duplicated or out of order")


def compute_features(bars: Sequence[FinalCandle]) -> list[FeatureRow]:
    """Calculate each feature using only that row and preceding *finalized* bars.

    The upstream adapter must only supply FinalCandle (closed REST / x=true WS).
    Rolling 30d empirical percentiles are computed only for 15m series, where
    30 days are exactly 2880 closed 15m bars. Missing warm-up yields None.
    """
    _validate_bars(bars)
    if not bars:
        return []
    closes = [bar.close for bar in bars]
    ema20 = ema(closes, 20)
    ema60 = ema(closes, 60)
    rsi14 = wilder_rsi(closes)
    atr14, adx14, plus_di, minus_di = wilder_atr_adx(bars)
    bb_mid, bb_upper, bb_lower, bb_width = bollinger(closes)
    vwap = utc_session_vwap(bars)
    vol_ratio = past_volume_ratio(bars)
    realized = realized_volatility(closes)
    if bars[0].timeframe == "15m":
        lookback = 30 * 24 * 60 // 15
        bb_percentile = trailing_percentile(bb_width, lookback)
        realized_percentile = trailing_percentile(realized, lookback)
    else:
        bb_percentile = [None] * len(bars)
        realized_percentile = [None] * len(bars)

    return [
        FeatureRow(
            symbol=bar.symbol, timeframe=bar.timeframe,
            open_ms=bar.open_ms, close_ms=bar.close_ms,
            ema20=ema20[i], ema60=ema60[i], rsi14=rsi14[i], atr14=atr14[i],
            adx14=adx14[i], plus_di14=plus_di[i], minus_di14=minus_di[i],
            bb_mid20=bb_mid[i], bb_upper20=bb_upper[i], bb_lower20=bb_lower[i],
            bb_width20=bb_width[i], bb_width_percentile_30d=bb_percentile[i],
            vwap_utc=vwap[i], volume_ratio20=vol_ratio[i],
            realized_vol20=realized[i],
            realized_vol_percentile_30d=realized_percentile[i],
        )
        for i, bar in enumerate(bars)
    ]
