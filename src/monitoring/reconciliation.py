"""Fail-closed broker state reconciliation for execution modes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from state.state_store import BotState


ACTIVE_ORDER_STATUSES = {"PendingSubmit", "PreSubmitted", "Submitted", "PartiallyFilled"}


class AccountReconciliationError(RuntimeError):
    """Raised when broker state cannot be reconciled to bot state."""


@dataclass(frozen=True)
class AccountReconciliationResult:
    passed: bool
    checks: tuple[str, ...]


class AccountReconciliationGate:
    """Require every broker-side exposure to reconcile to active bot state."""

    def run_startup_checks(self, *, account_snapshot: Any, state: BotState) -> AccountReconciliationResult:
        failures = account_reconciliation_failures(account_snapshot=account_snapshot, state=state)
        if failures:
            raise AccountReconciliationError(
                "manual broker reconciliation required before trading resumes: " + "; ".join(failures)
            )

        return AccountReconciliationResult(
            passed=True,
            checks=("broker positions, open orders, executions, and active trade state reconciled",),
        )


def account_reconciliation_failures(*, account_snapshot: Any, state: BotState) -> list[str]:
    """Return fail-closed reconciliation failures for startup gates."""

    failures: list[str] = []

    unknown_positions = _unknown_active_positions(account_snapshot, state)
    if unknown_positions:
        failures.append(f"unknown active positions exist: {', '.join(unknown_positions)}")

    unknown_orders = _unknown_active_open_orders(account_snapshot, state)
    if unknown_orders:
        failures.append(f"unknown active open orders exist: {', '.join(unknown_orders)}")

    unknown_executions = _unknown_recent_executions(account_snapshot, state)
    if unknown_executions:
        failures.append(f"unknown recent executions exist: {', '.join(unknown_executions)}")

    return failures


def _unknown_active_positions(account_snapshot: Any, state: BotState) -> list[str]:
    unknown: list[str] = []
    for position in getattr(account_snapshot, "positions", ()):
        quantity = float(getattr(position, "position", 0.0) or 0.0)
        if quantity == 0.0:
            continue
        if not _matches_active_trade_product(position, state.active_trade):
            unknown.append(_position_label(position, quantity))
    return unknown


def _unknown_active_open_orders(account_snapshot: Any, state: BotState) -> list[str]:
    unknown: list[str] = []
    reconciled_order_ids = _reconciled_active_trade_order_ids(state.active_trade)

    for order in getattr(account_snapshot, "open_orders", ()):
        if not _is_active_order(order):
            continue
        if _order_identity(order) not in reconciled_order_ids or not _matches_active_trade_product_identity(
            order,
            state.active_trade,
        ):
            unknown.append(_order_label(order))

    return unknown


def _unknown_recent_executions(account_snapshot: Any, state: BotState) -> list[str]:
    unknown: list[str] = []
    reconciled_order_ids = _reconciled_active_trade_order_ids(state.active_trade)

    for execution in getattr(account_snapshot, "executions", ()):
        if _order_identity(execution) not in reconciled_order_ids or not _matches_active_trade_product_identity(
            execution,
            state.active_trade,
        ):
            unknown.append(_execution_label(execution))

    return unknown


def _reconciled_active_trade_order_ids(active_trade: dict[str, Any]) -> set[tuple[str, int]]:
    identities: set[tuple[str, int]] = set()
    _add_identity(identities, active_trade)

    protective_orders = active_trade.get("protective_orders")
    if isinstance(protective_orders, list):
        for order in protective_orders:
            if isinstance(order, dict):
                _add_identity(identities, order)

    return identities


def _matches_active_trade_product(source: Any, active_trade: dict[str, Any]) -> bool:
    if not active_trade:
        return False

    filled = float(active_trade.get("filled", 0.0) or 0.0)
    if filled <= 0.0:
        return False

    return _matches_active_trade_product_identity(source, active_trade)


def _matches_active_trade_product_identity(source: Any, active_trade: dict[str, Any]) -> bool:
    if not active_trade:
        return False

    product_con_id = _optional_positive_int(active_trade.get("product_con_id"))
    source_con_id = _optional_positive_int(getattr(source, "con_id", None))
    if product_con_id is not None and source_con_id is not None:
        return product_con_id == source_con_id

    product_local_symbol = _optional_text(active_trade.get("product_local_symbol"))
    source_local_symbol = _optional_text(getattr(source, "local_symbol", None))
    if product_local_symbol is not None and source_local_symbol is not None:
        return product_local_symbol == source_local_symbol

    return False


def _is_active_order(order: Any) -> bool:
    status = str(getattr(order, "status", "") or "")
    remaining = float(getattr(order, "remaining", 0.0) or 0.0)
    return status in ACTIVE_ORDER_STATUSES and remaining > 0.0


def _order_identity(source: Any) -> tuple[str, int] | None:
    order_id = _optional_positive_int(getattr(source, "order_id", None))
    if order_id is not None:
        return ("order_id", order_id)

    perm_id = _optional_positive_int(getattr(source, "perm_id", None))
    if perm_id is not None:
        return ("perm_id", perm_id)

    return None


def _add_identity(identities: set[tuple[str, int]], payload: dict[str, Any]) -> None:
    order_id = _optional_positive_int(payload.get("order_id"))
    if order_id is not None:
        identities.add(("order_id", order_id))

    perm_id = _optional_positive_int(payload.get("perm_id"))
    if perm_id is not None:
        identities.add(("perm_id", perm_id))


def _position_label(position: Any, quantity: float) -> str:
    con_id = getattr(position, "con_id", None)
    local_symbol = str(getattr(position, "local_symbol", "") or "")
    return f"con_id={con_id} local_symbol={local_symbol} quantity={quantity}"


def _order_label(order: Any) -> str:
    order_id = getattr(order, "order_id", None)
    perm_id = getattr(order, "perm_id", None)
    local_symbol = str(getattr(order, "local_symbol", "") or "")
    return f"order_id={order_id} perm_id={perm_id} local_symbol={local_symbol}"


def _execution_label(execution: Any) -> str:
    exec_id = str(getattr(execution, "exec_id", "") or "")
    order_id = getattr(execution, "order_id", None)
    perm_id = getattr(execution, "perm_id", None)
    local_symbol = str(getattr(execution, "local_symbol", "") or "")
    return f"exec_id={exec_id} order_id={order_id} perm_id={perm_id} local_symbol={local_symbol}"


def _optional_positive_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        int_value = int(value)
    except (TypeError, ValueError):
        return None
    return int_value if int_value > 0 else None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
