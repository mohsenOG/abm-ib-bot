"""Submit and track paper orders through Interactive Brokers."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from domain.constants import (
    ACTIVE_ORDER_STATUSES,
    ORDER_STATUS_CANCELLED,
    ORDER_STATUS_FILLED,
    ORDER_STATUS_INACTIVE,
    ORDER_STATUS_PARTIALLY_FILLED,
    ORDER_STATUS_PENDING_SUBMIT,
    ORDER_STATUS_PRE_SUBMITTED,
    ORDER_STATUS_REJECTED,
    ORDER_STATUS_SUBMITTED,
    PAPER_MODE,
    TERMINAL_ORDER_STATUSES,
)
from logging_setup.logger import get_logger
from monitoring.account_guard import account_guard_failures, configured_account_id
from state.state_store import BotState, StateStore
from trade_journal.journal import TradeJournal

from execution.order_builder import BuiltOrderSet, EntryOrderType, OrderBuilder


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
        return self.status in TERMINAL_ORDER_STATUSES


@dataclass(frozen=True)
class ManagedOrderResult:
    trade: Any
    status: ManagedOrderStatus
    state: BotState | None
    protective_trades: tuple[Any, ...] = ()


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
        execution_settings = getattr(settings, "execution", None)
        self._entry_order_type = _entry_order_type_setting(execution_settings)
        self._entry_fill_timeout_seconds = _positive_float_setting(
            execution_settings,
            "entry_fill_timeout_seconds",
        )
        self._protective_submit_timeout_seconds = _positive_float_setting(
            execution_settings,
            "protective_submit_timeout_seconds",
        )
        self._status_poll_seconds = _positive_float_setting(execution_settings, "status_poll_seconds")
        self._logger = get_logger("execution.order_manager")
        self._seen_order_events: set[str] = set()

    async def submit_trade_plan(
        self,
        *,
        contract: Any,
        trade_plan: Any,
        order_type: EntryOrderType | None = None,
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
            order_type=order_type or self._entry_order_type,
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
        """Submit an entry order and, after fill, broker-side OCA protection."""

        self._require_paper_mode()
        if order_set.has_protective_orders:
            raise OrderManagerError(
                "Prebuilt protective orders are not accepted; OrderManager builds post-fill OCA protection."
            )

        active_ib = ib if ib is not None else self._connected_ib()
        current_state = self._load_state(state)
        if current_state is None:
            raise OrderManagerError("Bot state is required to track broker-side protective orders.")
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

        filled_status = await self._wait_for_entry_fill(
            active_ib,
            trade,
            trade_plan=trade_plan,
            state=updated_state,
            timeout_seconds=self._entry_fill_timeout_seconds,
        )
        updated_state = self._process_status_update(
            filled_status,
            trade_plan,
            updated_state,
            source="fill_wait",
            force=True,
        )

        protective_trades, updated_state = await self._submit_protective_oca_orders(
            active_ib,
            contract=contract,
            trade_plan=trade_plan,
            entry_status=filled_status,
            state=updated_state,
        )

        self._logger.info(
            "Paper order protected. signal_id=%s entry_order_id=%s entry_perm_id=%s protective_orders=%s",
            filled_status.signal_id,
            filled_status.order_id,
            filled_status.perm_id,
            len(protective_trades),
        )
        return ManagedOrderResult(
            trade=trade,
            status=filled_status,
            state=updated_state,
            protective_trades=tuple(protective_trades),
        )

    async def _wait_for_entry_fill(
        self,
        ib: Any,
        trade: Any,
        *,
        trade_plan: Any,
        state: BotState | None,
        timeout_seconds: float,
    ) -> ManagedOrderStatus:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        last_status = _managed_status(trade, trade_plan)

        while asyncio.get_running_loop().time() <= deadline:
            last_status = _managed_status(trade, trade_plan)
            if last_status.status == ORDER_STATUS_FILLED:
                if last_status.avg_fill_price <= 0 or last_status.filled <= 0:
                    raise OrderManagerError("Entry order filled without a usable average fill price.")
                return last_status

            if last_status.status in TERMINAL_ORDER_STATUSES - {ORDER_STATUS_FILLED}:
                raise OrderManagerError(f"Entry order reached terminal status before fill: {last_status.status}.")

            await asyncio.sleep(self._status_poll_seconds)

        if last_status.filled > 0 and last_status.avg_fill_price > 0:
            raise OrderManagerError(
                "Entry order partially filled before timeout; manual reconciliation is required before continuing."
            )

        self._cancel_unfilled_entry(ib, trade)
        self._process_status_update(last_status, trade_plan, state, source="fill_timeout", force=True)
        raise OrderManagerError("Entry order did not fill before protective order timeout; unfilled entry was cancelled.")

    async def _submit_protective_oca_orders(
        self,
        ib: Any,
        *,
        contract: Any,
        trade_plan: Any,
        entry_status: ManagedOrderStatus,
        state: BotState | None,
    ) -> tuple[tuple[Any, ...], BotState | None]:
        stop_price, take_profit_price = _protective_product_prices(entry_status, trade_plan)
        oca_group = _oca_group_name(entry_status, trade_plan)
        stop_loss_order, take_profit_order = self._builder.build_exit_oca_orders(
            trade_plan,
            quantity=entry_status.filled,
            stop_loss_price=stop_price,
            take_profit_price=take_profit_price,
            oca_group=oca_group,
        )
        protective_order_set = BuiltOrderSet(
            entry_order=stop_loss_order,
            take_profit_order=take_profit_order,
        )
        self._guard_order_accounts(protective_order_set)

        self._logger.info(
            "Submitting protective OCA orders. signal_id=%s quantity=%s stop=%s take_profit=%s oca_group=%s",
            getattr(trade_plan, "signal_id", None),
            entry_status.filled,
            stop_price,
            take_profit_price,
            oca_group,
        )

        try:
            stop_trade = ib.placeOrder(contract, stop_loss_order)
            take_profit_trade = ib.placeOrder(contract, take_profit_order)
        except Exception as exc:
            self._logger.exception("Protective OCA order submission failed.")
            raise OrderManagerError("Failed to submit broker-side protective OCA orders.") from exc

        protective_trades = (stop_trade, take_profit_trade)
        await self._wait_for_protective_submission(protective_trades, trade_plan)
        updated_state = self._persist_protective_orders(
            state,
            trade_plan=trade_plan,
            protective_trades=protective_trades,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            oca_group=oca_group,
        )
        return protective_trades, updated_state

    async def _wait_for_protective_submission(
        self,
        protective_trades: tuple[Any, ...],
        trade_plan: Any,
    ) -> None:
        deadline = asyncio.get_running_loop().time() + self._protective_submit_timeout_seconds

        while asyncio.get_running_loop().time() <= deadline:
            statuses = [_managed_status(trade, trade_plan) for trade in protective_trades]
            if all(status.order_id is not None and status.status in ACTIVE_ORDER_STATUSES for status in statuses):
                return

            failed = [status for status in statuses if status.status in TERMINAL_ORDER_STATUSES - {ORDER_STATUS_FILLED}]
            if failed:
                details = ", ".join(f"order_id={status.order_id} status={status.status}" for status in failed)
                raise OrderManagerError(f"Protective OCA order rejected or inactive: {details}.")

            await asyncio.sleep(self._status_poll_seconds)

        details = ", ".join(
            f"order_id={status.order_id} status={status.status}"
            for status in (_managed_status(trade, trade_plan) for trade in protective_trades)
        )
        raise OrderManagerError(f"Protective OCA orders were not confirmed by IB before timeout: {details}.")

    def _persist_protective_orders(
        self,
        state: BotState | None,
        *,
        trade_plan: Any,
        protective_trades: tuple[Any, ...],
        stop_price: float,
        take_profit_price: float,
        oca_group: str,
    ) -> BotState | None:
        if state is None:
            return None

        protective_statuses = [_managed_status(trade, trade_plan) for trade in protective_trades]
        for status in protective_statuses:
            if status.order_id is not None and status.order_id not in state.known_order_ids:
                state.known_order_ids.append(status.order_id)
            if status.perm_id is not None and status.perm_id not in state.known_perm_ids:
                state.known_perm_ids.append(status.perm_id)

        state.active_trade["protective_oca_group"] = oca_group
        state.active_trade["protective_orders_confirmed"] = True
        state.active_trade["product_stop_price"] = stop_price
        state.active_trade["product_take_profit_price"] = take_profit_price
        state.active_trade["protective_orders"] = [_protective_order_payload(status) for status in protective_statuses]

        if self._state_store is not None:
            self._state_store.save(state)

        return state

    def _cancel_unfilled_entry(self, ib: Any, trade: Any) -> None:
        cancel_order = getattr(ib, "cancelOrder", None)
        if not callable(cancel_order):
            self._logger.warning("Entry did not fill and IB client has no cancelOrder method.")
            return

        try:
            cancel_order(getattr(trade, "order", None))
        except Exception:
            self._logger.exception("Failed to cancel unfilled entry order after fill timeout.")

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

        if signal_id is not None and active_signal_id == signal_id and active_status not in TERMINAL_ORDER_STATUSES:
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

        if status.status in {ORDER_STATUS_PENDING_SUBMIT, ORDER_STATUS_PRE_SUBMITTED, ORDER_STATUS_SUBMITTED}:
            _safe_notify(
                self._notifier,
                "send_order_submitted",
                order_id=status.order_id,
                side=side,
                quantity=status.total_quantity,
                price=None,
            )
        elif status.status == ORDER_STATUS_PARTIALLY_FILLED:
            _safe_notify(
                self._notifier,
                "send_fill",
                order_id=status.order_id,
                perm_id=status.perm_id,
                side=side,
                quantity=status.filled,
                price=status.avg_fill_price,
            )
        elif status.status == ORDER_STATUS_FILLED:
            _safe_notify(
                self._notifier,
                "send_fill",
                order_id=status.order_id,
                perm_id=status.perm_id,
                side=side,
                quantity=status.filled,
                price=status.avg_fill_price,
            )
        elif status.status == ORDER_STATUS_CANCELLED:
            _safe_notify(self._notifier, "send_order_cancelled", order_id=status.order_id, reason="Order cancelled.")
        elif status.status in {ORDER_STATUS_INACTIVE, ORDER_STATUS_REJECTED}:
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
    if status.status in {ORDER_STATUS_PENDING_SUBMIT, ORDER_STATUS_PRE_SUBMITTED, ORDER_STATUS_SUBMITTED}:
        return "order_submitted"
    if status.status == ORDER_STATUS_PARTIALLY_FILLED:
        return "order_partially_filled"
    if status.status == ORDER_STATUS_FILLED:
        return "order_filled"
    if status.status == ORDER_STATUS_CANCELLED:
        return "order_cancelled"
    if status.status == ORDER_STATUS_INACTIVE:
        return "order_inactive"
    if status.status == ORDER_STATUS_REJECTED:
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
                "underlying_symbol": getattr(trade_plan, "underlying_symbol", None),
                "underlying_entry_price": getattr(trade_plan, "underlying_entry_price", None),
                "atr": getattr(trade_plan, "atr", None),
                "atr_pct": getattr(trade_plan, "atr_pct", None),
                "underlying_sl_price": getattr(trade_plan, "underlying_sl_price", None),
                "underlying_tp_price": getattr(trade_plan, "underlying_tp_price", None),
                "underlying_sl_pct": getattr(trade_plan, "underlying_sl_pct", None),
                "underlying_tp_pct": getattr(trade_plan, "underlying_tp_pct", None),
                "product_leverage": getattr(trade_plan, "product_leverage", None),
                "product_sl_pct": getattr(trade_plan, "product_sl_pct", None),
                "product_tp_pct": getattr(trade_plan, "product_tp_pct", None),
            }
        )
        product = getattr(trade_plan, "product", None)
        if product is not None:
            payload.update(
                {
                    "product_asset_class": getattr(product, "asset_class", None),
                    "product_con_id": getattr(product, "con_id", None),
                    "product_local_symbol": getattr(product, "local_symbol", None),
                    "product_exchange": getattr(product, "exchange", None),
                    "product_currency": getattr(product, "currency", None),
                }
            )

    return payload


def _protective_product_prices(status: ManagedOrderStatus, trade_plan: Any) -> tuple[float, float]:
    avg_fill_price = status.avg_fill_price
    if avg_fill_price <= 0:
        raise OrderManagerError("Cannot compute protective prices without a positive average fill price.")

    product_sl_pct = _positive_float_attr(trade_plan, "product_sl_pct")
    product_tp_pct = _positive_float_attr(trade_plan, "product_tp_pct")
    stop_price = avg_fill_price * (1 - product_sl_pct / 100)
    take_profit_price = avg_fill_price * (1 + product_tp_pct / 100)

    if stop_price <= 0:
        raise OrderManagerError("Computed product stop price is not positive; refusing unprotected entry.")

    return stop_price, take_profit_price


def _oca_group_name(status: ManagedOrderStatus, trade_plan: Any) -> str:
    order_id = status.order_id
    if order_id is not None:
        return f"ABM_ENTRY_{order_id}_OCA"

    signal_id = _optional_text_attr(trade_plan, "signal_id") or "UNKNOWN"
    sanitized = "".join(character if character.isalnum() else "_" for character in signal_id)
    return f"ABM_{sanitized}_OCA"


def _protective_order_payload(status: ManagedOrderStatus) -> dict[str, Any]:
    return {
        "order_id": status.order_id,
        "perm_id": status.perm_id,
        "status": status.status,
        "action": status.action,
        "order_type": status.order_type,
        "total_quantity": status.total_quantity,
    }


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


def _entry_order_type_setting(source: Any) -> EntryOrderType:
    value = getattr(source, "entry_order_type", None)
    if value == "market":
        return "market"
    raise OrderManagerError("execution.entry_order_type must be market.")


def _positive_float_setting(source: Any, name: str) -> float:
    value = getattr(source, name, None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OrderManagerError(f"execution.{name} must be a positive number.")
    result = float(value)
    if result <= 0:
        raise OrderManagerError(f"execution.{name} must be greater than zero.")
    return result


def _safe_notify(notifier: Any, method_name: str, **kwargs: Any) -> None:
    method = getattr(notifier, method_name, None)
    if callable(method):
        method(**kwargs)


def _positive_float_attr(source: Any, name: str) -> float:
    value = getattr(source, name, None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OrderManagerError(f"{name} must be a positive number.")
    result = float(value)
    if result <= 0:
        raise OrderManagerError(f"{name} must be greater than zero.")
    return result


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
