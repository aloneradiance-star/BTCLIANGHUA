"""Research strategy hypotheses, separate from execution and accounting."""

from baqs.strategies.s01 import (
    S01Config,
    S01TrendPullbackStrategy,
    evaluate_s01_signal,
    latest_completed_feature,
)

__all__ = [
    "S01Config",
    "S01TrendPullbackStrategy",
    "evaluate_s01_signal",
    "latest_completed_feature",
]
