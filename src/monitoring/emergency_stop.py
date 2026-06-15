"""Persistent emergency stop for blocking new trades."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from domain.constants import PROTECTED_TRADING_MODES
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
        settings: Any | None = None,
    ) -> None:
        self._state_store = state_store
        self._notifier = notifier
        self._journal = journal
        self._settings = settings
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

        def update(locked_state: BotState) -> BotState:
            locked_state.emergency_stop = True
            locked_state.active_trade = {
                **locked_state.active_trade,
                "emergency_stop_reason": reason_text,
                "emergency_stop_activated_at": activated_at,
            }
            return locked_state

        current_state = self._state_store.transaction(update)

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
            result = method(reason=reason)
            attempted = bool(getattr(result, "attempted", False))
            success = bool(getattr(result, "success", True))
            failed_count = getattr(result, "failed_count", None)
            if not attempted and _notification_delivery_required(self._settings):
                self._logger.error("Emergency stop notification was not attempted.")
                raise EmergencyStopError("Required emergency stop notification was not attempted.")
            if attempted and not success:
                self._logger.error("Emergency stop notification delivery failed. failed_count=%s", failed_count)
                if _notification_delivery_required(self._settings):
                    raise EmergencyStopError("Required emergency stop notification delivery failed.")

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


def _notification_delivery_required(settings: Any | None) -> bool:
    if settings is None:
        return False
    telegram_settings = getattr(settings, "telegram", None)
    if not bool(getattr(telegram_settings, "require_critical_delivery", True)):
        return False
    trading_mode = getattr(getattr(settings, "trading", None), "mode", None)
    return trading_mode in PROTECTED_TRADING_MODES
