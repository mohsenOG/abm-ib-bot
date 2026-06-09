"""Persistent emergency stop for blocking new trades."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from logging_setup.logger import get_logger
from state.state_store import BotState, StateStore
from trade_journal.journal import TradeJournal


class EmergencyStopError(RuntimeError):
    """Raised when trading is attempted while emergency stop is active."""


@dataclass(frozen=True)
class EmergencyStopActivation:
    activated: bool
    reason: str
    activated_at: str


class EmergencyStop:
    """Manage a persisted emergency-stop flag."""

    def __init__(
        self,
        state_store: StateStore,
        *,
        notifier: Any | None = None,
        journal: TradeJournal | None = None,
    ) -> None:
        self._state_store = state_store
        self._notifier = notifier
        self._journal = journal
        self._logger = get_logger("monitoring.emergency_stop")

    def is_active(self, state: BotState | None = None) -> bool:
        """Return whether emergency stop is active."""

        current_state = state if state is not None else self._state_store.load()
        return bool(current_state.emergency_stop)

    def assert_trading_allowed(self, state: BotState | None = None) -> None:
        """Fail closed when emergency stop is active."""

        if self.is_active(state):
            raise EmergencyStopError("Emergency stop is active. New trades are blocked.")

    def activate(self, reason: str, *, state: BotState | None = None) -> EmergencyStopActivation:
        """Activate emergency stop, persist it, and send alerts."""

        reason_text = _required_reason(reason)
        activated_at = datetime.now(UTC).isoformat()
        current_state = state if state is not None else self._state_store.load()
        already_active = current_state.emergency_stop

        current_state.emergency_stop = True
        current_state.active_trade = {
            **current_state.active_trade,
            "emergency_stop_reason": reason_text,
            "emergency_stop_activated_at": activated_at,
        }
        self._state_store.save(current_state)

        if not already_active:
            self._logger.critical("Emergency stop activated. reason=%s", reason_text)
            self._notify(reason_text)
            self._journal_event(reason_text, activated_at)
        else:
            self._logger.warning("Emergency stop already active. reason=%s", reason_text)

        return EmergencyStopActivation(
            activated=not already_active,
            reason=reason_text,
            activated_at=activated_at,
        )

    def block_reason(self, state: BotState | None = None) -> str | None:
        """Return a human-readable block reason when active."""

        current_state = state if state is not None else self._state_store.load()
        if not current_state.emergency_stop:
            return None

        reason = current_state.active_trade.get("emergency_stop_reason")
        if isinstance(reason, str) and reason.strip():
            return reason.strip()
        return "Emergency stop is active."

    def _notify(self, reason: str) -> None:
        if self._notifier is None:
            return

        method = getattr(self._notifier, "send_emergency_stop", None)
        if callable(method):
            method(reason=reason)

    def _journal_event(self, reason: str, activated_at: str) -> None:
        if self._journal is None:
            return

        self._journal.record(
            "emergency_stop",
            timestamp=activated_at,
            reason=reason,
            raw_json={"reason": reason, "activated_at": activated_at},
        )


def _required_reason(reason: str) -> str:
    if not isinstance(reason, str) or not reason.strip():
        raise EmergencyStopError("Emergency stop reason is required.")
    return reason.strip()
