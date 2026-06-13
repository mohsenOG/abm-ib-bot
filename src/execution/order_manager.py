"""Submit and track paper orders through Interactive Brokers."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from logging_setup.logger import get_logger
from monitoring.account_guard import account_guard_failures, configured_account_id
from state.state_store import BotState, StateStore
from trade_journal.journal import TradeJournal

from execution.order_builder import BuiltOrderSet, EntryOrderType, OrderBuilder


PAPER_MODE = "paper"
ACTIVE_STATUSES = {"PendingSubmit", "PreSubmitted", "Submitted", "PartiallyFilled"}
TERMINAL_STATUSES = {"Filled", "Cancelled", "Inactive", "Rejected"}
KNOWN_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES

JournalStatusEvent = Literal[
    "order_submitted",
    "order_partially_filled",
    "order_filled",
    "order_rejected",
    "order_cancelled",
    "order_inactive",
]


class OrderManagerError(RuntimeError):
    """Raised when a paper order cannot be submitted or tracked safely."""


@dataclass(frozen=True)
class ManagedOrderStatus:
    order_id: int | None
    perm_id: int | None
    status: str
    action: str
    order_type: str
    total_quantity: float
    filled: float
    remaining: float
    avg_fill_price: float
    signal_id: str | None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


@dataclass(frozen=True)
class ManagedOrderResult:
    trade: Any
    status: ManagedOrderStatus
    state: BotState | None


class OrderManager:
    """Submit approved orders in paper mode and record lifecycle events."""

    def __init__(
        self,
        settings: Any,
        ib_client: Any,
        *,
        state_store: StateStore | None = None,
        journal: TradeJournal | None = None,
        notifier: Any | None = None,
        order_builder: OrderBuilder | None = None,
    ) -> None:
        self._settings = settings
        self._ib_client = ib_client
        self._state_store = state_store
        self._journal = journal
        self._notifier = notifier
        self._builder = order_builder or OrderBuilder(account_id=getattr(getattr(settings, "ib", None), "account_id", None))
        self._logger = get_logger("execution.order_manager")
        self._seen_order_events: set[str] = set()

    async def submit_trade_plan(
        self,
        *,
        contract: Any,
        trade_plan: Any,
        order_type: EntryOrderType = "market",
        limit_price: float | None = None,
        state: BotState | None = None,
    ) -> ManagedOrderResult:
        """Build and submit a paper entry order for an approved trade plan."""

        self._require_paper_mode()
        ib = self._connected_ib()
        current_state = self._load_state(state)
        self._guard_duplicate_submission(trade_plan, current_state)

        order_set = self._builder.build_order_set(
            trade_plan,
            order_type=order_type,
            limit_price=limit_price,
        )
        return await self.submit_order_set(
            contract=contract,
            trade_plan=trade_plan,
            order_set=order_set,
            state=current_state,
            ib=ib,
        )

    async def submit_order_set(
        self,
        *,
        contract: Any,
        trade_plan: Any,
        order_set: BuiltOrderSet,
        state: BotState | None = None,
        ib: Any | None = None,
    ) -> ManagedOrderResult:
        """Submit a prebuilt entry order set.

        The first paper version submits one entry order only. Attached
        protective order submission is intentionally left for a later confirmed
        design because parent/transmit behavior is instrument-sensitive.
        """

        self._require_paper_mode()
        if order_set.has_protective_orders:
            raise OrderManagerError("Protective order submission is not enabled in the first paper manager version.")

        active_ib = ib if ib is not None else self._connected_ib()
        current_state = self._load_state(state)
        self._guard_duplicate_submission(trade_plan, current_state)
        self._guard_connected_account(active_ib)
        self._guard_order_accounts(order_set)

        self._logger.info(
            "Submitting paper order. signal_id=%s action=%s quantity=%s",
            getattr(trade_plan, "signal_id", None),
            getattr(order_set.entry_order, "action", ""),
            getattr(order_set.entry_order, "totalQuantity", 0),
        )

        try:
            trade = active_ib.placeOrder(contract, order_set.entry_order)
        except Exception as exc:
            self._logger.exception("Paper order submission failed.")
            raise OrderManagerError("Failed to submit paper order to Interactive Brokers.") from exc

        self._attach_trade_event_handlers(trade, trade_plan, current_state)
        await asyncio.sleep(0)

        status = _managed_status(trade, trade_plan)
        updated_state = self._process_status_update(
            status,
            trade_plan,
            current_state,
            source="submit",
            force=True,
        )

        self._logger.info(
            "Paper order submitted. signal_id=%s order_id=%s perm_id=%s status=%s",
            status.signal_id,
            status.order_id,
            status.perm_id,
            status.status,
        )
        return ManagedOrderResult(trade=trade, status=status, state=updated_state)

    def refresh_order_status(
        self,
        trade: Any,
        *,
        trade_plan: Any | None = None,
        state: BotState | None = None,
    ) -> ManagedOrderStatus:
        """Normalize, persist, journal, and notify the latest status for a trade."""

        status = _managed_status(trade, trade_plan)
        current_state = self._load_state(state)
        updated_state = self._update_state_after_status(current_state, trade_plan, status)
        self._record_status(status, trade_plan)
        self._notify_status(status, trade_plan)
        if updated_state is not None:
            self._logger.info("Paper order status refreshed. order_id=%s status=%s", status.order_id, status.status)
        return status

    def _require_paper_mode(self) -> None:
        mode = getattr(getattr(self._settings, "trading", None), "mode", None)
        if mode != PAPER_MODE:
            raise OrderManagerError("OrderManager submits orders only when trading.mode is paper.")

    def _guard_connected_account(self, ib: Any) -> None:
        expected_account = configured_account_id(self._settings)
        if expected_account is None:
            raise OrderManagerError("IB_ACCOUNT_ID is required before submitting paper orders.")

        managed_accounts = getattr(ib, "managedAccounts", None)
        if not callable(managed_accounts):
            raise OrderManagerError("Interactive Brokers client cannot report managed accounts.")

        try:
            observed_accounts = {account for account in (_optional_text(account) for account in managed_accounts()) if account}
        except Exception as exc:
            self._logger.exception("Could not verify IB managed accounts before order submission.")
            raise OrderManagerError("Could not verify IB managed accounts before order submission.") from exc

        failures = account_guard_failures(
            expected_account=expected_account,
            observed_accounts=observed_accounts,
            mode=PAPER_MODE,
        )
        if failures:
            raise OrderManagerError("; ".join(failures))

    def _guard_order_accounts(self, order_set: BuiltOrderSet) -> None:
        expected_account = configured_account_id(self._settings)
        if expected_account is None:
            raise OrderManagerError("IB_ACCOUNT_ID is required before submitting paper orders.")

        for order in order_set.orders:
            order_account = _optional_text_attr(order, "account")
            if order_account is None:
                raise OrderManagerError("Order account is missing; refusing to submit to Interactive Brokers.")
            if order_account != expected_account:
                raise OrderManagerError(
                    f"Order account mismatch. expected={expected_account} observed={order_account}."
                )

    def _connected_ib(self) -> Any:
        ib = _resolve_ib_client(self._ib_client)
        is_connected = getattr(ib, "isConnected", None)
        if callable(is_connected) and not is_connected():
            raise OrderManagerError("Interactive Brokers is disconnected.")
        return ib

    def _load_state(self, state: BotState | None) -> BotState | None:
        if state is not None:
            return state
        if self._state_store is None:
            return None
        return self._state_store.load()

    def _guard_duplicate_submission(self, trade_plan: Any, state: BotState | None) -> None:
        if state is None:
            return

        signal_id = _optional_text_attr(trade_plan, "signal_id")
        active_trade = state.active_trade
        active_signal_id = active_trade.get("signal_id")
        active_status = active_trade.get("status")

        if signal_id is not None and active_signal_id == signal_id and active_status not in TERMINAL_STATUSES:
            raise OrderManagerError(f"Active trade already exists for signal_id={signal_id}.")

        known_signal_id = active_trade.get("submitted_signal_id")
        if signal_id is not None and known_signal_id == signal_id:
            raise OrderManagerError(f"Signal was already submitted: {signal_id}.")

    def _update_state_after_status(
        self,
        state: BotState | None,
        trade_plan: Any | None,
        status: ManagedOrderStatus,
    ) -> BotState | None:
        if state is None:
            return None

        if status.order_id is not None and status.order_id not in state.known_order_ids:
            state.known_order_ids.append(status.order_id)

        if status.perm_id is not None and status.perm_id not in state.known_perm_ids:
            state.known_perm_ids.append(status.perm_id)

        state.active_trade = _active_trade_payload(trade_plan, status)
        if trade_plan is not None:
            state.last_signal_id = _optional_text_attr(trade_plan, "signal_id") or state.last_signal_id

        if self._state_store is not None:
            self._state_store.save(state)

        return state

    def _record_status(self, status: ManagedOrderStatus, trade_plan: Any | None) -> None:
        if self._journal is None:
            return

        event_type = _journal_event_type(status)
        if event_type is None:
            return

        self._journal.record(
            event_type,
            signal_id=status.signal_id,
            side=getattr(trade_plan, "signal_side", None),
            quantity=status.total_quantity,
            price=status.avg_fill_price if status.avg_fill_price > 0 else None,
            order_id=status.order_id,
            perm_id=status.perm_id,
            status=status.status,
            raw_json=asdict(status),
        )

    def _notify_status(self, status: ManagedOrderStatus, trade_plan: Any | None) -> None:
        if self._notifier is None:
            return

        side = getattr(trade_plan, "signal_side", None)

        if status.status in {"PendingSubmit", "PreSubmitted", "Submitted"}:
            _safe_notify(
                self._notifier,
                "send_order_submitted",
                order_id=status.order_id,
                side=side,
                quantity=status.total_quantity,
                price=None,
            )
        elif status.status == "PartiallyFilled":
            _safe_notify(
                self._notifier,
                "send_fill",
                order_id=status.order_id,
                perm_id=status.perm_id,
                side=side,
                quantity=status.filled,
                price=status.avg_fill_price,
            )
        elif status.status == "Filled":
            _safe_notify(
                self._notifier,
                "send_fill",
                order_id=status.order_id,
                perm_id=status.perm_id,
                side=side,
                quantity=status.filled,
                price=status.avg_fill_price,
            )
        elif status.status == "Cancelled":
            _safe_notify(self._notifier, "send_order_cancelled", order_id=status.order_id, reason="Order cancelled.")
        elif status.status in {"Inactive", "Rejected"}:
            _safe_notify(self._notifier, "send_order_rejected", order_id=status.order_id, reason=status.status)

    def _attach_trade_event_handlers(self, trade: Any, trade_plan: Any, state: BotState | None) -> None:
        """Attach event handlers to persist order updates from ib_async Trade events."""

        def on_status_update(*args: Any) -> None:
            updated_trade = _event_trade_arg(args, trade)
            status = _managed_status(updated_trade, trade_plan)
            self._process_status_update(status, trade_plan, state, source="status_event")

        def on_fill_update(*args: Any) -> None:
            updated_trade = _event_trade_arg(args, trade)
            status = _managed_status(updated_trade, trade_plan)
            self._process_status_update(status, trade_plan, state, source="fill_event")

        _add_event_handler(getattr(trade, "statusEvent", None), on_status_update, self._logger)
        _add_event_handler(getattr(trade, "fillEvent", None), on_fill_update, self._logger)
        _add_event_handler(getattr(trade, "filledEvent", None), on_fill_update, self._logger)
        _add_event_handler(getattr(trade, "cancelledEvent", None), on_status_update, self._logger)

    def _process_status_update(
        self,
        status: ManagedOrderStatus,
        trade_plan: Any | None,
        state: BotState | None,
        *,
        source: str,
        force: bool = False,
    ) -> BotState | None:
        event_key = _status_event_key(status)
        if not force and self._is_duplicate_status_event(event_key, status, state):
            self._logger.debug(
                "Duplicate order status event skipped. source=%s order_id=%s status=%s",
                source,
                status.order_id,
                status.status,
            )
            return state

        self._seen_order_events.add(event_key)
        updated_state = self._update_state_after_status(state, trade_plan, status)
        self._record_status(status, trade_plan)
        self._notify_status(status, trade_plan)
        return updated_state

    def _is_duplicate_status_event(
        self,
        event_key: str,
        status: ManagedOrderStatus,
        state: BotState | None,
    ) -> bool:
        if event_key in self._seen_order_events:
            return True

        if state is None:
            return False

        active_trade = state.active_trade
        return (
            _optional_int_payload(active_trade, "order_id") == status.order_id
            and _optional_int_payload(active_trade, "perm_id") == status.perm_id
            and str(active_trade.get("status", "") or "") == status.status
            and float(active_trade.get("filled", 0.0) or 0.0) == status.filled
            and float(active_trade.get("remaining", 0.0) or 0.0) == status.remaining
            and float(active_trade.get("avg_fill_price", 0.0) or 0.0) == status.avg_fill_price
        )


def _managed_status(trade: Any, trade_plan: Any | None) -> ManagedOrderStatus:
    order = getattr(trade, "order", None)
    order_status = getattr(trade, "orderStatus", None)
    status = str(getattr(order_status, "status", "") or "Unknown")

    return ManagedOrderStatus(
        order_id=_optional_int_attr(order, "orderId"),
        perm_id=_optional_int_attr(order, "permId"),
        status=status,
        action=str(getattr(order, "action", "") or ""),
        order_type=str(getattr(order, "orderType", "") or ""),
        total_quantity=_float_attr(order, "totalQuantity"),
        filled=_float_attr(order_status, "filled"),
        remaining=_float_attr(order_status, "remaining"),
        avg_fill_price=_float_attr(order_status, "avgFillPrice"),
        signal_id=_optional_text_attr(trade_plan, "signal_id") if trade_plan is not None else None,
    )


def _event_trade_arg(args: tuple[Any, ...], fallback_trade: Any) -> Any:
    if args and hasattr(args[0], "orderStatus"):
        return args[0]
    return fallback_trade


def _add_event_handler(event: Any, handler: Any, logger: Any) -> None:
    if event is None:
        return
    try:
        event += handler
    except Exception as exc:
        logger.warning("Could not attach ib_async trade event handler: %s", exc)


def _status_event_key(status: ManagedOrderStatus) -> str:
    return ":".join(
        (
            str(status.order_id or ""),
            str(status.perm_id or ""),
            status.status,
            str(status.filled),
            str(status.remaining),
            str(status.avg_fill_price),
        )
    )


def _journal_event_type(status: ManagedOrderStatus) -> JournalStatusEvent | None:
    if status.status in {"PendingSubmit", "PreSubmitted", "Submitted"}:
        return "order_submitted"
    if status.status == "PartiallyFilled":
        return "order_partially_filled"
    if status.status == "Filled":
        return "order_filled"
    if status.status == "Cancelled":
        return "order_cancelled"
    if status.status == "Inactive":
        return "order_inactive"
    if status.status == "Rejected":
        return "order_rejected"
    return None


def _active_trade_payload(trade_plan: Any | None, status: ManagedOrderStatus) -> dict[str, Any]:
    payload = asdict(status)
    payload["updated_at"] = datetime.now(UTC).isoformat()

    if trade_plan is not None:
        payload.update(
            {
                "submitted_signal_id": _optional_text_attr(trade_plan, "signal_id"),
                "signal_timestamp": getattr(trade_plan, "signal_timestamp", None),
                "signal_side": getattr(trade_plan, "signal_side", None),
                "execution_side": getattr(trade_plan, "execution_side", None),
                "capital_allocated": getattr(trade_plan, "capital_allocated", None),
            }
        )

    return payload


def _resolve_ib_client(ib_client: Any) -> Any:
    if _looks_like_ib_client(ib_client):
        return ib_client

    try:
        candidate = getattr(ib_client, "ib")
    except Exception as exc:
        raise OrderManagerError("Could not access Interactive Brokers client.") from exc

    if not _looks_like_ib_client(candidate):
        raise OrderManagerError("OrderManager requires an Interactive Brokers client.")

    return candidate


def _looks_like_ib_client(value: Any) -> bool:
    return all(callable(getattr(value, name, None)) for name in ("placeOrder", "managedAccounts", "isConnected"))


def _safe_notify(notifier: Any, method_name: str, **kwargs: Any) -> None:
    method = getattr(notifier, method_name, None)
    if callable(method):
        method(**kwargs)


def _optional_text_attr(source: Any, name: str) -> str | None:
    value = getattr(source, name, None)
    return _optional_text(value)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _float_attr(source: Any, name: str) -> float:
    value = getattr(source, name, 0.0)
    if value is None:
        return 0.0
    return float(value)


def _optional_int_attr(source: Any, name: str) -> int | None:
    value = getattr(source, name, None)
    if value is None:
        return None
    int_value = int(value)
    return int_value if int_value > 0 else None


def _optional_int_payload(source: dict[str, Any], name: str) -> int | None:
    value = source.get(name)
    if value is None:
        return None
    int_value = int(value)
    return int_value if int_value > 0 else None
