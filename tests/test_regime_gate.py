"""Confirm hysteresis, immediate safety overrides, cooldown and reboot gaps."""

import pytest

from baqs.regime.gate import FIVE_MIN_MS, GateState, advance


def test_switch_needs_three_completed_5m_observations() -> None:
    state = GateState()
    for n in (1, 2):
        result = advance(state, "BULL_TREND", n * FIVE_MIN_MS)
        assert result.state.current == "TRANSITION"
        assert result.state.confirmations == n
        state = result.state
    result = advance(state, "BULL_TREND", 3 * FIVE_MIN_MS)
    assert result.state.current == "BULL_TREND"
    assert result.state.confirmations == 0
    assert result.switched


def test_high_risk_is_immediate_and_clears_pending() -> None:
    pending = advance(GateState(), "RANGE", FIVE_MIN_MS).state
    result = advance(pending, "HIGH_RISK", 2 * FIVE_MIN_MS)
    assert result.state.current == "HIGH_RISK"
    assert result.state.pending is None
    assert result.switched
    assert "HIGH_RISK_IMMEDIATE" in result.reasons


def test_cooldown_blocks_switch_even_with_three_confirmations() -> None:
    initial = GateState(
        current="BULL_TREND",
        pending="BEAR_TREND",
        confirmations=2,
        last_switch_ms=2 * FIVE_MIN_MS,
        last_processed_ms=2 * FIVE_MIN_MS,
    )
    blocked = advance(initial, "BEAR_TREND", 3 * FIVE_MIN_MS)
    assert blocked.state.current == "BULL_TREND"
    assert "COOLDOWN_ACTIVE" in blocked.reasons
    allowed = advance(blocked.state, "BEAR_TREND", 4 * FIVE_MIN_MS)
    assert allowed.state.current == "BEAR_TREND"
    assert allowed.switched


def test_processing_gap_resets_stale_state_and_pending() -> None:
    initial = GateState(
        current="BULL_TREND",
        pending="RANGE",
        confirmations=2,
        last_switch_ms=FIVE_MIN_MS,
        last_processed_ms=2 * FIVE_MIN_MS,
    )
    result = advance(initial, "RANGE", 5 * FIVE_MIN_MS)
    assert result.state.current == "TRANSITION"
    assert result.state.pending == "RANGE"
    assert result.state.confirmations == 1
    assert "PROCESSING_GAP_RESET" in result.reasons


def test_state_roundtrip_and_invalid_persisted_values_fail() -> None:
    state = GateState("BEAR_TREND", "RANGE", 2, FIVE_MIN_MS, 3 * FIVE_MIN_MS)
    assert GateState.from_dict(state.as_dict()) == state
    corrupted = dict(state.as_dict())
    corrupted["current"] = "GUARANTEED_PROFIT"
    with pytest.raises(ValueError):
        GateState.from_dict(corrupted)


def test_duplicate_and_out_of_order_closes_rejected() -> None:
    state = advance(GateState(), "RANGE", FIVE_MIN_MS).state
    with pytest.raises(ValueError):
        advance(state, "RANGE", FIVE_MIN_MS)
    with pytest.raises(ValueError):
        advance(state, "RANGE", 0)
