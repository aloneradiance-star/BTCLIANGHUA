"""Auditable, deterministic market-regime classification; NEVER an execution system."""

from baqs.regime.rules import REGIME_VERSION, Candidate, RegimeUnavailable, classify_candidate

__all__ = ["REGIME_VERSION", "Candidate", "RegimeUnavailable", "classify_candidate"]
