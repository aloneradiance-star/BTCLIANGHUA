"""Read-only coverage of six-state rule candidates over one evaluation window."""

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass

from baqs.indicators import compute_features
from baqs.market_data.schema import INTERVAL_MS, FinalCandle
from baqs.regime.rules import REGIME_VERSION, REGIMES, RegimeUnavailable, classify_candidate


@dataclass(frozen=True, slots=True)
class RegimeCandidateCoverage:
    classifier_version: str
    timeframe: str
    expected_intervals: int
    counts_by_regime: dict[str, int]
    unavailable_intervals: int
    missing_regimes: tuple[str, ...]


def measure_regime_candidates(
    bars15: Sequence[FinalCandle],
    bars1h: Sequence[FinalCandle],
    start_ms: int,
    end_ms: int,
) -> RegimeCandidateCoverage:
    """Count the candidate available at the start of each half-open 15m interval."""
    features15 = compute_features(bars15)
    features1h = compute_features(bars1h)
    hourly_closes = [row.close_ms for row in features1h]
    counts = dict.fromkeys(sorted(REGIMES), 0)
    for index, row in enumerate(features15):
        if not start_ms <= row.close_ms < end_ms:
            continue
        higher = bisect_right(hourly_closes, row.close_ms) - 1
        if higher < 0 or row.close_ms - hourly_closes[higher] >= INTERVAL_MS["1h"]:
            continue

        # The candidate uses at most the previous 23 closed rows and the
        # current row. Their features already include the full causal warm-up.
        window_start = max(0, index - 23)
        try:
            candidate = classify_candidate(
                bars15[window_start : index + 1],
                features15[window_start : index + 1],
                features1h[higher : higher + 1],
            )
        except RegimeUnavailable:
            continue
        counts[candidate.regime] += 1

    expected = (end_ms - start_ms) // INTERVAL_MS["15m"]
    return RegimeCandidateCoverage(
        classifier_version=REGIME_VERSION,
        timeframe="15m",
        expected_intervals=expected,
        counts_by_regime=counts,
        unavailable_intervals=expected - sum(counts.values()),
        missing_regimes=tuple(regime for regime, count in counts.items() if count == 0),
    )
