"""Strict startup gates for controlled live-mode preparation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from logging_setup.logger import get_logger
from monitoring.account_guard import account_guard_failures, configured_account_id, snapshot_accounts
from monitoring.health import HealthReport
from state.state_store import BotState


class LiveModeGateError(RuntimeError):
    """Raised when live mode cannot start safely."""


@dataclass(frozen=True)
class LiveModeGateResult:
    passed: bool
    checks: tuple[str, ...]


class LiveModeGate:
    """Validate live-mode readiness without enabling live execution."""

    def __init__(self, settings: Any, *, notifier: Any | None = None) -> None:
        self._settings = settings
        self._notifier = notifier
        self._logger = get_logger("monitoring.live_mode")

    def run_startup_checks(
        self,
        *,
        state: BotState,
        account_snapshot: Any,
        health_report: HealthReport,
    ) -> LiveModeGateResult:
        """Run strict live startup checks and fail closed on any issue."""

        failures: list[str] = []
        passed: list[str] = []

        if not getattr(getattr(self._settings, "live", None), "enabled", False):
            failures.append("live.enabled must be true when trading.mode is live.")
        else:
            passed.append("live mode explicitly enabled")

        if state.emergency_stop:
            failures.append("emergency stop is active")
        else:
            passed.append("emergency stop is off")

        if not health_report.ok:
            failures.append(f"health report is not ok: {health_report.level}")
        else:
            passed.append("health report is ok")

        failures.extend(self._account_match_failures(account_snapshot))
        failures.extend(self._reconciliation_failures(account_snapshot, state))
        telegram_failure = self._telegram_failure()
        if telegram_failure is not None:
            failures.append(telegram_failure)
        else:
            passed.append("Telegram live check passed or is explicitly allowed to fail")

        if failures:
            reason = "; ".join(failures)
            self._logger.error("Live mode startup gate failed. reason=%s", reason)
            raise LiveModeGateError(f"Live mode startup checks failed: {reason}.")

        self._logger.info("Live mode startup gate passed. checks=%s", len(passed))
        return LiveModeGateResult(passed=True, checks=tuple(passed))

    def _account_match_failures(self, account_snapshot: Any) -> list[str]:
        return account_guard_failures(
            expected_account=configured_account_id(self._settings),
            observed_accounts=snapshot_accounts(account_snapshot),
            mode="live",
        )

    def _reconciliation_failures(self, account_snapshot: Any, state: BotState) -> list[str]:
        failures: list[str] = []

        unknown_orders = _unknown_active_open_orders(account_snapshot, state)
        if unknown_orders:
            failures.append(f"unknown active open orders exist: {', '.join(unknown_orders)}")

        active_positions = _active_positions(account_snapshot)
        if active_positions:
            failures.append(f"open positions require manual reconciliation: {', '.join(active_positions)}")

        return failures

    def _telegram_failure(self) -> str | None:
        allow_failure = getattr(getattr(self._settings, "live", None), "allow_telegram_failure", False)
        if self._notifier is None:
            return None if allow_failure else "Telegram notifier is unavailable"

        method = getattr(self._notifier, "send_heartbeat", None)
        if not callable(method):
            return None if allow_failure else "Telegram notifier has no heartbeat method"

        try:
            result = method("Live mode readiness check")
        except Exception as exc:
            self._logger.exception("Telegram live check failed.")
            return None if allow_failure else f"Telegram live check failed: {exc}"

        attempted = bool(getattr(result, "attempted", False))
        success = bool(getattr(result, "success", False))

        if allow_failure:
            return None

        if not attempted:
            return "Telegram live check was not attempted"

        if not success:
            return "Telegram live check failed"

        return None


def _unknown_active_open_orders(account_snapshot: Any, state: BotState) -> list[str]:
    unknown: list[str] = []
    known_order_ids = set(state.known_order_ids)
    known_perm_ids = set(state.known_perm_ids)

    for order in getattr(account_snapshot, "open_orders", ()):
        if not _is_active_order(order):
            continue

        order_id = getattr(order, "order_id", None)
        perm_id = getattr(order, "perm_id", None)
        known_by_order_id = isinstance(order_id, int) and order_id in known_order_ids
        known_by_perm_id = isinstance(perm_id, int) and perm_id in known_perm_ids

        if not known_by_order_id and not known_by_perm_id:
            unknown.append(_order_label(order))

    return unknown


def _active_positions(account_snapshot: Any) -> list[str]:
    active: list[str] = []
    for position in getattr(account_snapshot, "positions", ()):
        quantity = float(getattr(position, "position", 0.0) or 0.0)
        if quantity != 0.0:
            active.append(_position_label(position, quantity))
    return active


def _is_active_order(order: Any) -> bool:
    status = str(getattr(order, "status", "") or "")
    remaining = float(getattr(order, "remaining", 0.0) or 0.0)
    return status in {"PendingSubmit", "PreSubmitted", "Submitted", "PartiallyFilled"} and remaining > 0.0


def _order_label(order: Any) -> str:
    order_id = getattr(order, "order_id", None)
    perm_id = getattr(order, "perm_id", None)
    local_symbol = str(getattr(order, "local_symbol", "") or "")
    return f"order_id={order_id} perm_id={perm_id} local_symbol={local_symbol}"


def _position_label(position: Any, quantity: float) -> str:
    con_id = getattr(position, "con_id", None)
    local_symbol = str(getattr(position, "local_symbol", "") or "")
    return f"con_id={con_id} local_symbol={local_symbol} quantity={quantity}"
