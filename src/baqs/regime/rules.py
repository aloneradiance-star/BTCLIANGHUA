"""Pure six-regime rules for BTC/ETH public finalized candles.

This is a hypothesis for research; it is NOT a buy/sell signal, a calibrated
probability, or proof of trading profitability. Missing features fail closed.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from baqs.indicators.engine import FeatureRow
from baqs.market_data.schema import FinalCandle

REGIME_VERSION = "rules-0.1"
REGIMES = frozenset(
    {
        "BULL_TREND",
        "BEAR_TREND",
        "RANGE",
        "BREAKOUT",
        "HIGH_RISK",
        "TRANSITION",
    }
)


class RegimeUnavailable(Exception):
    """System/data invalid; do not fabricate a market state."""


@dataclass(frozen=True, slots=True)
class Candidate:
    regime: str
    reasons: tuple[str, ...]
    metrics: dict[str, str | None]

    def __post_init__(self) -> None:
        if self.regime not in REGIMES:
            raise ValueError("Invalid market regime")


def _must(value: Decimal | None, reason: str) -> Decimal:
    if value is None or not value.is_finite():
        raise RegimeUnavailable(reason)
    return value


def _boundary_tests(
    candles: Sequence[FinalCandle],
    *,
    boundary: Decimal,
    atr: Decimal,
    side: str,
) -> int:
    """Count confirmed edge tests with >=3 15m-bar spacing.

    A wick must enter 0.25 ATR of one boundary, then a future-within-the-
    historical-window close must reverse >=0.5 ATR within three closed bars.
    The candidate bar is excluded from this historical test.
    """
    count, last_test = 0, -3
    for i in range(len(candles) - 1):
        if i - last_test < 3:
            continue
        after = candles[i + 1 : i + 4]
        if side == "LOWER":
            touched = candles[i].low <= boundary + Decimal("0.25") * atr
            reversed_ = any(bar.close >= boundary + Decimal("0.5") * atr for bar in after)
        else:
            touched = candles[i].high >= boundary - Decimal("0.25") * atr
            reversed_ = any(bar.close <= boundary - Decimal("0.5") * atr for bar in after)
        if touched and reversed_:
            count += 1
            last_test = i
    return count


def classify_candidate(
    bars15: Sequence[FinalCandle],
    features15: Sequence[FeatureRow],
    features1h: Sequence[FeatureRow],
    *,
    spread_bps: Decimal | None = None,
    median_spread_bps: Decimal | None = None,
) -> Candidate:
    """Select one mutually exclusive regime using past/closed evidence only.

    HIGH_RISK outranks BREAKOUT, then trends, then RANGE, then TRANSITION.
    Spread-based risk is only evaluated if both real validated spread values exist.
    """
    if len(bars15) < 24 or len(features15) != len(bars15) or not features1h:
        raise RegimeUnavailable("INSUFFICIENT_CLOSED_BARS")
    if bars15[-1].timeframe != "15m" or features1h[-1].timeframe != "1h":
        raise RegimeUnavailable("TIMEFRAME_MISMATCH")
    current = features15[-1]
    if current.symbol != bars15[-1].symbol or current.close_ms != bars15[-1].close_ms:
        raise RegimeUnavailable("FEATURE_BAR_MISMATCH")
    higher = features1h[-1]
    if higher.symbol != current.symbol or higher.close_ms > current.close_ms:
        raise RegimeUnavailable("HIGHER_TIMEFRAME_LOOKAHEAD")
    if (spread_bps is None) != (median_spread_bps is None):
        raise RegimeUnavailable("INCOMPLETE_SPREAD_FEED")

    e20 = _must(current.ema20, "EMA15_WARMUP")
    e60 = _must(current.ema60, "EMA15_WARMUP")
    e20past = _must(features15[-4].ema20, "EMA_SLOPE_WARMUP")
    e20higher = _must(higher.ema20, "EMA1H_WARMUP")
    e60higher = _must(higher.ema60, "EMA1H_WARMUP")
    adx = _must(current.adx14, "ADX_WARMUP")
    atr = _must(current.atr14, "ATR_WARMUP")
    bb_percentile = _must(current.bb_width_percentile_30d, "BB_30D_NOT_READY")
    rv_percentile = _must(current.realized_vol_percentile_30d, "RV_30D_NOT_READY")
    realized = _must(current.realized_vol20, "RV_WARMUP")
    vol_ratio = _must(current.volume_ratio20, "VOLUME_WARMUP")
    if atr < 0 or not Decimal(0) <= bb_percentile <= 1 or not Decimal(0) <= rv_percentile <= 1:
        raise RegimeUnavailable("FEATURE_OUT_OF_RANGE")
    if spread_bps is not None and median_spread_bps is not None:
        if (
            not spread_bps.is_finite()
            or not median_spread_bps.is_finite()
            or spread_bps < 0
            or median_spread_bps <= 0
        ):
            raise RegimeUnavailable("INVALID_SPREAD_FEED")

    metrics: dict[str, str | None] = {
        "ema20_15m": str(e20),
        "ema60_15m": str(e60),
        "ema20_1h": str(e20higher),
        "ema60_1h": str(e60higher),
        "adx14_15m": str(adx),
        "atr14_15m": str(atr),
        "bb_width_percentile": str(bb_percentile),
        "realized_vol_percentile": str(rv_percentile),
        "realized_vol": str(realized),
        "volume_ratio20": str(vol_ratio),
        "spread_bps": None if spread_bps is None else str(spread_bps),
    }

    # A flat zero-return series is not a high-volatility crisis merely because
    # the inclusive percentile assigns tied zero values rank 1.
    volatile = realized > 0 and rv_percentile >= Decimal("0.9")
    wide_spread = (
        spread_bps is not None
        and median_spread_bps is not None
        and spread_bps >= Decimal(3) * median_spread_bps
    )
    if volatile or wide_spread:
        reasons = []
        if volatile:
            reasons.append("REALIZED_VOL_90TH_PERCENTILE")
        if wide_spread:
            reasons.append("SPREAD_3X_MEDIAN")
        return Candidate("HIGH_RISK", tuple(reasons), metrics)

    previous20 = bars15[-21:-1]
    high = max(bar.high for bar in previous20)
    low = min(bar.low for bar in previous20)
    last = bars15[-1]
    recent_compressed = any(
        row.bb_width_percentile_30d is not None and row.bb_width_percentile_30d <= Decimal("0.2")
        for row in features15[-4:-1]
    )
    up_breakout = last.close > high + Decimal("0.15") * atr
    down_breakout = last.close < low - Decimal("0.15") * atr
    if recent_compressed and vol_ratio >= Decimal("1.5"):
        if up_breakout:
            return Candidate(
                "BREAKOUT", ("COMPRESSION", "BREAKOUT_UP", "VOLUME_CONFIRMED"), metrics
            )
        if down_breakout:
            return Candidate(
                "BREAKOUT", ("COMPRESSION", "BREAKOUT_DOWN", "VOLUME_CONFIRMED"), metrics
            )

    if adx > Decimal(23):
        if e20 > e60 and e20higher > e60higher and e20 > e20past:
            return Candidate("BULL_TREND", ("EMA_ALIGNED_UP", "ADX_GT_23", "EMA_RISING"), metrics)
        if e20 < e60 and e20higher < e60higher and e20 < e20past:
            return Candidate(
                "BEAR_TREND", ("EMA_ALIGNED_DOWN", "ADX_GT_23", "EMA_FALLING"), metrics
            )

    width = high - low
    midpoint = (high + low) / 2
    if (
        adx < Decimal(18)
        and atr > 0
        and midpoint > 0
        and width / midpoint >= Decimal("0.007")
        and low <= last.close <= high
    ):
        lower_tests = _boundary_tests(previous20, boundary=low, atr=atr, side="LOWER")
        upper_tests = _boundary_tests(previous20, boundary=high, atr=atr, side="UPPER")
        metrics["range_lower_tests"] = str(lower_tests)
        metrics["range_upper_tests"] = str(upper_tests)
        if lower_tests >= 2 and upper_tests >= 2:
            return Candidate("RANGE", ("ADX_LT_18", "BOTH_EDGES_TESTED"), metrics)

    return Candidate("TRANSITION", ("NO_VALID_MARKET_STATE",), metrics)
