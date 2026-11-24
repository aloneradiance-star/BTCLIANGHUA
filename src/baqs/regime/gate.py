"""Pure, restart-safe 5-minute confirmation and market-state hysteresis."""

from dataclasses import dataclass
from typing import Any

from baqs.regime.rules import REGIMES

FIVE_MIN_MS = 300_000
COOLDOWN_MS = 600_000
CONFIRMATION_BARS = 3


@dataclass(frozen=True, slots=True)
class GateState:
    current: str = "TRANSITION"
    pending: str | None = None
    confirmations: int = 0
    last_switch_ms: int | None = None
    last_processed_ms: int | None = None

    def as_dict(self) -> dict[str, str | int | None]:
        return {
            "current": self.current,
            "pending": self.pending,
            "confirmations": self.confirmations,
            "last_switch_ms": self.last_switch_ms,
            "last_processed_ms": self.last_processed_ms,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "GateState":
        if not isinstance(value, dict):
            raise ValueError("Invalid persisted gate object")
        current = value.get("current")
        pending = value.get("pending")
        confirms = value.get("confirmations")
        last_switch = value.get("last_switch_ms")
        last_processed = value.get("last_processed_ms")
        if (
            not isinstance(current, str)
            or current not in REGIMES
            or (pending is not None and (not isinstance(pending, str) or pending not in REGIMES))
            or type(confirms) is not int
            or not 0 <= confirms <= CONFIRMATION_BARS
            or (last_switch is not None and (type(last_switch) is not int or last_switch < 0))
            or (
                last_processed is not None
                and (type(last_processed) is not int or last_processed < 0)
            )
            or (last_switch is not None and last_switch % FIVE_MIN_MS != 0)
            or (last_processed is not None and last_processed % FIVE_MIN_MS != 0)
            or (
                last_switch is not None and (last_processed is None or last_switch > last_processed)
            )
            or (pending is None and confirms != 0)
            or (
                pending is not None
                and (confirms < 1 or pending == current or pending == "HIGH_RISK")
            )
            or (current == "HIGH_RISK" and pending is not None)
            or (current != "TRANSITION" and last_switch is None)
        ):
            raise ValueError("Invalid persisted gate fields")
        return cls(current, pending, confirms, last_switch, last_processed)


@dataclass(frozen=True, slots=True)
class GateResult:
    state: GateState
    reasons: tuple[str, ...]
    switched: bool


def advance(state: GateState, candidate: str, closed_5m_ms: int) -> GateResult:
    """Apply a completed 5m observation; HIGH_RISK is immediate and overrides cooldown."""
    if candidate not in REGIMES:
        raise ValueError("Unknown candidate")
    if closed_5m_ms < 0 or closed_5m_ms % FIVE_MIN_MS:
        raise ValueError("Clock must be an aligned 5-minute close boundary")
    if state.last_processed_ms is not None and closed_5m_ms <= state.last_processed_ms:
        raise ValueError("Repeated or backward observation")
    reasons: list[str] = []
    if (
        state.last_processed_ms is not None
        and closed_5m_ms - state.last_processed_ms != FIVE_MIN_MS
    ):
        # Missing processed observations invalidate past confirmation and state.
        state = GateState(
            current="TRANSITION",
            last_processed_ms=state.last_processed_ms,
        )
        reasons.append("PROCESSING_GAP_RESET")

    if candidate == "HIGH_RISK":
        switched = state.current != "HIGH_RISK"
        next_state = GateState(
            current="HIGH_RISK",
            confirmations=0,
            last_switch_ms=closed_5m_ms if switched else state.last_switch_ms,
            last_processed_ms=closed_5m_ms,
        )
        return GateResult(next_state, tuple(reasons + ["HIGH_RISK_IMMEDIATE"]), switched)

    if candidate == state.current:
        return GateResult(
            GateState(state.current, None, 0, state.last_switch_ms, closed_5m_ms),
            tuple(reasons + ["STATE_STABLE"]),
            False,
        )

    count = state.confirmations + 1 if state.pending == candidate else 1
    cooled = state.last_switch_ms is None or closed_5m_ms - state.last_switch_ms >= COOLDOWN_MS
    if count >= CONFIRMATION_BARS and cooled:
        return GateResult(
            GateState(candidate, None, 0, closed_5m_ms, closed_5m_ms),
            tuple(reasons + ["STATE_SWITCH_CONFIRMED"]),
            True,
        )

    return GateResult(
        GateState(
            state.current,
            candidate,
            min(count, CONFIRMATION_BARS),
            state.last_switch_ms,
            closed_5m_ms,
        ),
        tuple(reasons + (["COOLDOWN_ACTIVE"] if not cooled else ["PENDING_5M_CONFIRMATION"])),
        False,
    )
