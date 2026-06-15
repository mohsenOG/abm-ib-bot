"""Strict startup gates for controlled live-mode preparation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from domain.constants import LIVE_MODE
from logging_setup.logger import get_logger
from monitoring.account_guard import account_guard_failures, configured_account_id, snapshot_accounts
from monitoring.health import HealthReport
from monitoring.reconciliation import account_reconciliation_failures
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
            mode=LIVE_MODE,
        )

    def _reconciliation_failures(self, account_snapshot: Any, state: BotState) -> list[str]:
        return account_reconciliation_failures(account_snapshot=account_snapshot, state=state)

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
