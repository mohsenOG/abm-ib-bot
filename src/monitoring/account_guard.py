"""Fail-closed account identity and account-type checks for IB sessions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from domain.constants import LIVE_MODE, PAPER_MODE, PROTECTED_TRADING_MODES

PAPER_ACCOUNT_PREFIX = "DU"


class AccountGuardError(RuntimeError):
    """Raised when the connected IB account cannot be proven safe for the mode."""


@dataclass(frozen=True)
class AccountGuardResult:
    passed: bool
    checks: tuple[str, ...]


class AccountGuard:
    """Validate configured account id, observed accounts, and mode account type."""

    def __init__(self, settings: Any) -> None:
        self._settings = settings

    def run_startup_checks(self, *, account_snapshot: Any) -> AccountGuardResult:
        mode = _trading_mode(self._settings)
        if mode not in PROTECTED_TRADING_MODES:
            return AccountGuardResult(passed=True, checks=("account guard skipped for non-execution mode",))

        expected_account = configured_account_id(self._settings)
        observed_accounts = snapshot_accounts(account_snapshot)
        failures = account_guard_failures(
            expected_account=expected_account,
            observed_accounts=observed_accounts,
            mode=mode,
        )
        if failures:
            raise AccountGuardError("; ".join(failures))

        return AccountGuardResult(
            passed=True,
            checks=(
                f"IB_ACCOUNT_ID is configured for {mode} mode",
                f"all observed IB accounts match {expected_account}",
                f"configured account id matches expected {mode} account type",
            ),
        )


def configured_account_id(settings: Any) -> str | None:
    account_id = getattr(getattr(settings, "ib", None), "account_id", None)
    if account_id is None:
        return None
    text = str(account_id).strip()
    return text or None


def account_guard_failures(
    *,
    expected_account: str | None,
    observed_accounts: set[str],
    mode: str,
) -> list[str]:
    failures: list[str] = []

    if expected_account is None:
        failures.append(f"IB_ACCOUNT_ID is required when trading.mode is {mode}")
        return failures

    if mode == PAPER_MODE and not is_paper_account_id(expected_account):
        failures.append("IB_ACCOUNT_ID must be an IBKR paper account id starting with DU when trading.mode is paper")

    if mode == LIVE_MODE and is_paper_account_id(expected_account):
        failures.append("IB_ACCOUNT_ID must be a live IBKR account id when trading.mode is live")

    if not observed_accounts:
        failures.append("no IB accounts were observed from the connected session")
        return failures

    mismatches = sorted(account for account in observed_accounts if account != expected_account)
    if mismatches:
        failures.append(f"IB account mismatch. expected={expected_account} observed={','.join(mismatches)}")

    if expected_account not in observed_accounts:
        failures.append(f"configured IB_ACCOUNT_ID was not observed in the connected IB session: {expected_account}")

    return failures


def snapshot_accounts(account_snapshot: Any) -> set[str]:
    accounts: set[str] = set()

    for account in getattr(account_snapshot, "managed_accounts", ()):
        _add_account(accounts, account)

    for value in getattr(account_snapshot, "account_values", ()):
        _add_account(accounts, getattr(value, "account", ""))

    for position in getattr(account_snapshot, "positions", ()):
        _add_account(accounts, getattr(position, "account", ""))

    for order in getattr(account_snapshot, "open_orders", ()):
        _add_account(accounts, getattr(order, "account", ""))

    for execution in getattr(account_snapshot, "executions", ()):
        _add_account(accounts, getattr(execution, "account", ""))

    return accounts


def is_paper_account_id(account_id: str) -> bool:
    return account_id.upper().startswith(PAPER_ACCOUNT_PREFIX)


def _trading_mode(settings: Any) -> str:
    return str(getattr(getattr(settings, "trading", None), "mode", "") or "")


def _add_account(accounts: set[str], account: Any) -> None:
    text = str(account or "").strip()
    if text:
        accounts.add(text)
