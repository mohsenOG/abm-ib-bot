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
    PROTECTED_TRADING_MODES,
    TRADE_STATUS_CLOSED,
    TERMINAL_ORDER_STATUSES,
)
from config.defaults import DEFAULT_EXECUTION_ENTRY_ORDER_TYPE
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


class PartialFillTimeout(OrderManagerError):
    """Raised internally when an entry timeout leaves a real partial position."""

    def __init__(
        self,
        message: str,
        *,
        status: ManagedOrderStatus,
        cancellation_confirmed: bool,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.cancellation_confirmed = cancellation_confirmed


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
        emergency_stop: Any | None = None,
        order_builder: OrderBuilder | None = None,
    ) -> None:
        self._settings = settings
        self._ib_client = ib_client
        self._state_store = state_store
        self._journal = journal
        self._notifier = notifier
        self._emergency_stop = emergency_stop
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
        """Submit a broker-side bracket order and verify attached protection."""

        self._require_paper_mode()
        if order_set.has_protective_orders:
            raise OrderManagerError(
                "Prebuilt protective orders are not accepted; OrderManager builds broker-side bracket protection."
            )

        active_ib = ib if ib is not None else self._connected_ib()
        current_state = self._load_state(state)
        if current_state is None:
            raise OrderManagerError("Bot state is required to track broker-side protective orders.")
        self._guard_duplicate_submission(trade_plan, current_state)
        self._guard_connected_account(active_ib)
        self._guard_order_accounts(order_set)

        entry_order_id = self._ensure_order_id(active_ib, order_set.entry_order)
        stop_price, take_profit_price = _protective_product_prices_from_trade_plan(trade_plan)
        oca_group = _oca_group_name_from_order_id(entry_order_id)
        stop_loss_order, take_profit_order = self._builder.build_attached_exit_orders(
            trade_plan,
            quantity=getattr(order_set.entry_order, "totalQuantity", 0),
            stop_loss_price=stop_price,
            take_profit_price=take_profit_price,
            parent_order_id=entry_order_id,
            oca_group=oca_group,
        )
        bracket_order_set = BuiltOrderSet(
            entry_order=order_set.entry_order,
            stop_loss_order=stop_loss_order,
            take_profit_order=take_profit_order,
        )
        bracket_order_set.entry_order.transmit = False
        self._guard_order_accounts(bracket_order_set)

        self._logger.info(
            "Submitting broker-side paper bracket. signal_id=%s action=%s quantity=%s entry_order_id=%s stop=%s take_profit=%s",
            getattr(trade_plan, "signal_id", None),
            getattr(bracket_order_set.entry_order, "action", ""),
            getattr(bracket_order_set.entry_order, "totalQuantity", 0),
            entry_order_id,
            stop_price,
            take_profit_price,
        )

        trade = None
        protective_trades: tuple[Any, ...] = ()
        updated_state = current_state
        try:
            trade = active_ib.placeOrder(contract, bracket_order_set.entry_order)
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

            stop_trade = active_ib.placeOrder(contract, stop_loss_order)
            take_profit_trade = active_ib.placeOrder(contract, take_profit_order)
            protective_trades = (stop_trade, take_profit_trade)
        except Exception as exc:
            updated_state = await self._handle_bracket_failure(
                active_ib,
                contract=contract,
                trade_plan=trade_plan,
                entry_trade=trade,
                protective_trades=protective_trades,
                state=updated_state,
                stop_price=stop_price,
                take_profit_price=take_profit_price,
                oca_group=oca_group,
                cause=exc,
            )
            if _exposure_failure_was_handled(updated_state) and trade is not None:
                return ManagedOrderResult(
                    trade=trade,
                    status=_managed_status(trade, trade_plan),
                    state=updated_state,
                )
            if isinstance(exc, OrderManagerError):
                raise
            raise OrderManagerError("Failed to submit broker-side bracket order set.") from exc

        try:
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

            await self._wait_for_protective_submission(protective_trades, trade_plan)
            updated_state = self._persist_protective_orders(
                updated_state,
                trade_plan=trade_plan,
                protective_trades=protective_trades,
                stop_price=stop_price,
                take_profit_price=take_profit_price,
                oca_group=oca_group,
            )
        except PartialFillTimeout as exc:
            replacement_trades, updated_state = await self._handle_partial_fill_timeout(
                active_ib,
                contract=contract,
                trade_plan=trade_plan,
                entry_trade=trade,
                original_protective_trades=protective_trades,
                state=updated_state,
                partial_status=exc.status,
                cancellation_confirmed=exc.cancellation_confirmed,
            )
            return ManagedOrderResult(
                trade=trade,
                status=exc.status,
                state=updated_state,
                protective_trades=tuple(replacement_trades),
            )
        except Exception as exc:
            updated_state = await self._handle_bracket_failure(
                active_ib,
                contract=contract,
                trade_plan=trade_plan,
                entry_trade=trade,
                protective_trades=protective_trades,
                state=updated_state,
                stop_price=stop_price,
                take_profit_price=take_profit_price,
                oca_group=oca_group,
                cause=exc,
            )
            if _exposure_failure_was_handled(updated_state):
                return ManagedOrderResult(
                    trade=trade,
                    status=_managed_status(trade, trade_plan),
                    state=updated_state,
                )
            if isinstance(exc, OrderManagerError):
                raise
            raise OrderManagerError("Broker-side bracket protection could not be confirmed.") from exc

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
                if last_status.filled > 0 and last_status.avg_fill_price > 0:
                    self._process_status_update(
                        last_status,
                        trade_plan,
                        state,
                        source="partial_fill_terminal",
                        force=True,
                    )
                    raise PartialFillTimeout(
                        f"Entry order stopped at partial fill with terminal status: {last_status.status}.",
                        status=_managed_status(trade, trade_plan),
                        cancellation_confirmed=True,
                    )
                raise OrderManagerError(f"Entry order reached terminal status before fill: {last_status.status}.")

            await asyncio.sleep(self._status_poll_seconds)

        if last_status.filled > 0 and last_status.avg_fill_price > 0:
            self._process_status_update(last_status, trade_plan, state, source="partial_fill_timeout", force=True)
            self._cancel_unfilled_entry(ib, trade)
            cancel_status, cancellation_confirmed = await self._wait_for_entry_cancel_confirmation(
                trade,
                trade_plan=trade_plan,
                timeout_seconds=self._protective_submit_timeout_seconds,
            )
            if cancel_status.status == ORDER_STATUS_FILLED:
                if cancel_status.avg_fill_price <= 0 or cancel_status.filled <= 0:
                    raise OrderManagerError("Entry order filled without a usable average fill price.")
                return cancel_status

            self._process_status_update(
                cancel_status,
                trade_plan,
                state,
                source="partial_fill_cancel_wait",
                force=True,
            )
            raise PartialFillTimeout(
                "Entry order partially filled before timeout; remaining quantity was cancelled."
                if cancellation_confirmed
                else "Entry order partially filled before timeout; remaining quantity cancellation was not confirmed.",
                status=cancel_status,
                cancellation_confirmed=cancellation_confirmed,
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

    async def _handle_partial_fill_timeout(
        self,
        ib: Any,
        *,
        contract: Any,
        trade_plan: Any,
        entry_trade: Any,
        original_protective_trades: tuple[Any, ...],
        state: BotState | None,
        partial_status: ManagedOrderStatus,
        cancellation_confirmed: bool,
    ) -> tuple[tuple[Any, ...], BotState | None]:
        reason = (
            "Entry order partially filled before timeout. "
            f"filled={partial_status.filled} remaining={partial_status.remaining} "
            f"entry_order_id={partial_status.order_id} cancellation_confirmed={cancellation_confirmed}"
        )
        stop_price, take_profit_price = _protective_product_prices(partial_status, trade_plan)
        oca_group = _oca_group_name(partial_status, trade_plan)
        self._logger.critical(reason)
        self._critical_alert("Partial entry fill", reason)

        if not cancellation_confirmed:
            await self._handle_bracket_failure(
                ib,
                contract=contract,
                trade_plan=trade_plan,
                entry_trade=entry_trade,
                protective_trades=original_protective_trades,
                state=state,
                stop_price=stop_price,
                take_profit_price=take_profit_price,
                oca_group=oca_group,
                cause=OrderManagerError("Entry remaining quantity cancellation was not confirmed after partial fill."),
            )
            raise OrderManagerError(
                "Entry partially filled and remaining quantity cancellation was not confirmed; defensive exit attempted."
            )

        if not await self._cancel_trades_and_wait(
            ib,
            original_protective_trades,
            trade_plan=trade_plan,
            timeout_seconds=self._protective_submit_timeout_seconds,
        ):
            await self._handle_bracket_failure(
                ib,
                contract=contract,
                trade_plan=trade_plan,
                entry_trade=entry_trade,
                protective_trades=original_protective_trades,
                state=state,
                stop_price=stop_price,
                take_profit_price=take_profit_price,
                oca_group=oca_group,
                cause=OrderManagerError("Original full-quantity bracket children were not cancelled after partial fill."),
            )
            raise OrderManagerError(
                "Entry partially filled and original bracket child cancellation was not confirmed; defensive exit attempted."
            )

        try:
            replacement_trades, updated_state = await self._submit_protective_oca_orders(
                ib,
                contract=contract,
                trade_plan=trade_plan,
                entry_status=partial_status,
                state=state,
            )
        except Exception as exc:
            await self._handle_bracket_failure(
                ib,
                contract=contract,
                trade_plan=trade_plan,
                entry_trade=entry_trade,
                protective_trades=(),
                state=state,
                stop_price=stop_price,
                take_profit_price=take_profit_price,
                oca_group=oca_group,
                cause=exc,
            )
            raise OrderManagerError(
                "Entry partially filled but replacement broker-side OCA protection could not be confirmed."
            ) from exc

        updated_state = self._mark_partial_fill_protected(
            updated_state,
            partial_status=partial_status,
            reason=reason,
        )
        updated_state = self._activate_emergency_stop(reason, updated_state)
        return replacement_trades, updated_state

    def _mark_partial_fill_protected(
        self,
        state: BotState | None,
        *,
        partial_status: ManagedOrderStatus,
        reason: str,
    ) -> BotState | None:
        if state is None:
            return None

        state.active_trade = {
            **state.active_trade,
            "status": partial_status.status,
            "filled": partial_status.filled,
            "remaining": partial_status.remaining,
            "avg_fill_price": partial_status.avg_fill_price,
            "partial_fill_timeout": True,
            "partial_fill_reason": reason,
            "partial_fill_handled_at": datetime.now(UTC).isoformat(),
            "manual_reconciliation_required": True,
        }
        if self._state_store is not None:
            self._state_store.save(state)
        return state

    async def _handle_bracket_failure(
        self,
        ib: Any,
        *,
        contract: Any,
        trade_plan: Any,
        entry_trade: Any | None,
        protective_trades: tuple[Any, ...],
        state: BotState | None,
        stop_price: float,
        take_profit_price: float,
        oca_group: str,
        cause: Exception,
    ) -> BotState | None:
        if entry_trade is None:
            self._logger.exception("Broker-side bracket submission failed before entry order was accepted.")
            return state

        entry_status = _managed_status(entry_trade, trade_plan)
        updated_state = self._process_status_update(
            entry_status,
            trade_plan,
            state,
            source="bracket_failure",
            force=True,
        )

        if entry_status.filled <= 0:
            self._logger.exception("Broker-side bracket failed before any entry fill; cancelling staged orders.")
            self._cancel_orders(ib, (entry_trade, *protective_trades))
            return self._mark_cancelled_bracket_failure(
                updated_state,
                reason=f"Broker-side bracket failed before entry fill: {type(cause).__name__}: {cause}",
            )

        reason = (
            "Entry exposure exists without confirmed broker-side SL/TP protection. "
            f"cause={type(cause).__name__}: {cause}"
        )
        self._logger.critical(reason)
        missing_protection = _missing_protection_type(protective_trades, trade_plan)
        updated_state = self._mark_unprotected_entry(
            updated_state,
            trade_plan=trade_plan,
            entry_status=entry_status,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            oca_group=oca_group,
            missing_protection=missing_protection,
            reason=reason,
        )
        alert_details = _protection_failure_message(
            updated_state.active_trade if updated_state is not None else _active_trade_payload(trade_plan, entry_status),
            reason=reason,
            missing_protection=missing_protection,
        )
        self._critical_alert("Unprotected entry exposure", alert_details)
        updated_state = self._activate_emergency_stop(reason, updated_state)

        try:
            self._cancel_orders(ib, protective_trades)
            replacement_trades, replacement_state = await self._submit_protective_oca_orders(
                ib,
                contract=contract,
                trade_plan=trade_plan,
                entry_status=entry_status,
                state=updated_state,
            )
            repaired_state = self._mark_protection_resubmitted(
                replacement_state,
                protective_trades=replacement_trades,
                reason=reason,
            )
            self._critical_alert(
                "Protection resubmitted after failure",
                _protection_resubmitted_message(
                    repaired_state.active_trade if repaired_state is not None else {},
                    reason=reason,
                ),
            )
            return repaired_state
        except Exception as exc:
            self._logger.critical("Protective remediation resubmit failed; attempting defensive exit. error=%s", exc)
            self._critical_alert(
                "Protection resubmit failed",
                f"{alert_details}\nresubmit_error: {exc}\nnext_action: defensive market exit",
            )

        return await self._attempt_defensive_market_exit(
            ib,
            contract=contract,
            trade_plan=trade_plan,
            entry_status=entry_status,
            state=updated_state,
            reason=reason,
        )

    def _mark_cancelled_bracket_failure(self, state: BotState | None, *, reason: str) -> BotState | None:
        if state is None:
            return None

        state.active_trade = {
            **state.active_trade,
            "status": ORDER_STATUS_CANCELLED,
            "bracket_failure_reason": reason,
            "bracket_failure_at": datetime.now(UTC).isoformat(),
            "protective_orders_confirmed": False,
        }
        if self._state_store is not None:
            self._state_store.save(state)
        return state

    def _mark_unprotected_entry(
        self,
        state: BotState | None,
        *,
        trade_plan: Any,
        entry_status: ManagedOrderStatus,
        stop_price: float,
        take_profit_price: float,
        oca_group: str,
        missing_protection: str,
        reason: str,
    ) -> BotState | None:
        if state is None:
            return None

        state.active_trade = {
            **_active_trade_payload(trade_plan, entry_status),
            "protective_oca_group": oca_group,
            "protective_orders_confirmed": False,
            "protection_missing": True,
            "missing_protection_type": missing_protection,
            "product_stop_price": stop_price,
            "product_take_profit_price": take_profit_price,
            "protection_failure_reason": reason,
            "protection_failure_at": datetime.now(UTC).isoformat(),
            "manual_reconciliation_required": True,
        }
        if entry_status.order_id is not None and entry_status.order_id not in state.known_order_ids:
            state.known_order_ids.append(entry_status.order_id)
        if entry_status.perm_id is not None and entry_status.perm_id not in state.known_perm_ids:
            state.known_perm_ids.append(entry_status.perm_id)
        if self._state_store is not None:
            self._state_store.save(state)
        return state

    def _mark_protection_resubmitted(
        self,
        state: BotState | None,
        *,
        protective_trades: tuple[Any, ...],
        reason: str,
    ) -> BotState | None:
        if state is None:
            return None

        state.active_trade = {
            **state.active_trade,
            "protection_missing": False,
            "protective_orders_confirmed": True,
            "protection_resubmitted_after_failure": True,
            "protection_resubmitted_at": datetime.now(UTC).isoformat(),
            "protection_failure_reason": reason,
            "manual_reconciliation_required": True,
        }
        state.active_trade.pop("missing_protection_type", None)
        for trade in protective_trades:
            status = _managed_status(trade, None)
            if status.order_id is not None and status.order_id not in state.known_order_ids:
                state.known_order_ids.append(status.order_id)
            if status.perm_id is not None and status.perm_id not in state.known_perm_ids:
                state.known_perm_ids.append(status.perm_id)
        if self._state_store is not None:
            self._state_store.save(state)
        return state

    def _activate_emergency_stop(self, reason: str, state: BotState | None) -> BotState | None:
        if self._emergency_stop is not None:
            try:
                self._emergency_stop.activate(reason, state=state)
            except Exception:
                self._logger.exception("Emergency stop activation failed after unprotected entry.")
            return self._load_state(None) or state

        if state is None:
            return None

        state.emergency_stop = True
        state.active_trade = {
            **state.active_trade,
            "emergency_stop_reason": reason,
            "emergency_stop_activated_at": datetime.now(UTC).isoformat(),
        }
        if self._state_store is not None:
            self._state_store.save(state)
        return state

    async def _attempt_defensive_market_exit(
        self,
        ib: Any,
        *,
        contract: Any,
        trade_plan: Any,
        entry_status: ManagedOrderStatus,
        state: BotState | None,
        reason: str,
    ) -> BotState | None:
        try:
            exit_order = self._builder.build_market_exit_order(trade_plan, quantity=entry_status.filled)
            self._guard_order_accounts(BuiltOrderSet(entry_order=exit_order))
            exit_trade = ib.placeOrder(contract, exit_order)
            await asyncio.sleep(0)
            exit_status = await self._wait_for_defensive_exit(
                exit_trade,
                trade_plan=trade_plan,
                timeout_seconds=self._entry_fill_timeout_seconds,
            )
        except Exception as exc:
            failure_reason = f"Defensive market exit failed after unprotected entry: {exc}"
            self._logger.critical(failure_reason)
            self._critical_alert("Defensive exit failed", failure_reason)
            return self._persist_defensive_exit_state(
                state,
                status=None,
                submitted=False,
                confirmed=False,
                reason=failure_reason,
            )

        self._record_status(exit_status, trade_plan)
        self._notify_status(exit_status, trade_plan)
        if exit_status.status == ORDER_STATUS_FILLED:
            self._critical_alert(
                "Defensive exit filled",
                (
                    "Defensive market exit filled after unprotected entry. "
                    f"exit_order_id={exit_status.order_id} quantity={exit_status.filled}"
                ),
            )
            return self._persist_defensive_exit_state(
                state,
                status=exit_status,
                submitted=True,
                confirmed=True,
                reason=reason,
            )

        return self._persist_defensive_exit_state(
            state,
            status=exit_status,
            submitted=True,
            confirmed=False,
            reason="Defensive exit did not reach Filled status before timeout.",
        )

    async def _wait_for_defensive_exit(
        self,
        trade: Any,
        *,
        trade_plan: Any,
        timeout_seconds: float,
    ) -> ManagedOrderStatus:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        last_status = _managed_status(trade, trade_plan)

        while asyncio.get_running_loop().time() <= deadline:
            last_status = _managed_status(trade, trade_plan)
            if last_status.status == ORDER_STATUS_FILLED:
                return last_status
            if last_status.status in TERMINAL_ORDER_STATUSES - {ORDER_STATUS_FILLED}:
                return last_status
            await asyncio.sleep(self._status_poll_seconds)

        return last_status

    def _persist_defensive_exit_state(
        self,
        state: BotState | None,
        *,
        status: ManagedOrderStatus | None,
        submitted: bool,
        confirmed: bool,
        reason: str,
    ) -> BotState | None:
        if state is None:
            return None

        defensive_exit: dict[str, Any] = {
            "submitted": submitted,
            "confirmed": confirmed,
            "reason": reason,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        if status is not None:
            defensive_exit.update(_protective_order_payload(status))
            defensive_exit["filled"] = status.filled
            defensive_exit["remaining"] = status.remaining
            defensive_exit["avg_fill_price"] = status.avg_fill_price
            if status.order_id is not None and status.order_id not in state.known_order_ids:
                state.known_order_ids.append(status.order_id)
            if status.perm_id is not None and status.perm_id not in state.known_perm_ids:
                state.known_perm_ids.append(status.perm_id)

        state.active_trade = {
            **state.active_trade,
            "defensive_exit": defensive_exit,
            "manual_reconciliation_required": not confirmed,
        }
        if confirmed:
            state.active_trade["status"] = TRADE_STATUS_CLOSED
            state.active_trade["closed_at"] = datetime.now(UTC).isoformat()

        if self._state_store is not None:
            self._state_store.save(state)
        return state

    def _critical_alert(self, message: str, details: str) -> None:
        if self._notifier is None:
            return
        method = getattr(self._notifier, "send_critical_error", None)
        if callable(method):
            result = method(message=message, details=details)
            _raise_on_required_notification_failure(
                result,
                "send_critical_error",
                settings=self._settings,
                logger=self._logger,
            )

    def _cancel_orders(self, ib: Any, trades: tuple[Any, ...]) -> None:
        cancel_order = getattr(ib, "cancelOrder", None)
        if not callable(cancel_order):
            self._logger.warning("IB client has no cancelOrder method for failed bracket cleanup.")
            return

        for trade in trades:
            order = getattr(trade, "order", None)
            if order is None:
                continue
            try:
                cancel_order(order)
            except Exception:
                self._logger.exception("Failed to cancel staged bracket order.")

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

    async def _wait_for_entry_cancel_confirmation(
        self,
        trade: Any,
        *,
        trade_plan: Any,
        timeout_seconds: float,
    ) -> tuple[ManagedOrderStatus, bool]:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        last_status = _managed_status(trade, trade_plan)

        while asyncio.get_running_loop().time() <= deadline:
            last_status = _managed_status(trade, trade_plan)
            if last_status.status == ORDER_STATUS_FILLED:
                return last_status, True
            if last_status.status in TERMINAL_ORDER_STATUSES - {ORDER_STATUS_FILLED}:
                return last_status, True
            await asyncio.sleep(self._status_poll_seconds)

        return last_status, False

    async def _cancel_trades_and_wait(
        self,
        ib: Any,
        trades: tuple[Any, ...],
        *,
        trade_plan: Any,
        timeout_seconds: float,
    ) -> bool:
        if not trades:
            return True

        self._cancel_orders(ib, trades)
        deadline = asyncio.get_running_loop().time() + timeout_seconds

        while asyncio.get_running_loop().time() <= deadline:
            statuses = [_managed_status(trade, trade_plan) for trade in trades]
            if all(status.status in TERMINAL_ORDER_STATUSES for status in statuses):
                return True
            await asyncio.sleep(self._status_poll_seconds)

        details = ", ".join(
            f"order_id={status.order_id} status={status.status}"
            for status in (_managed_status(trade, trade_plan) for trade in trades)
        )
        self._logger.critical("Timed out waiting for bracket child cancellation after partial fill. %s", details)
        return False

    def _ensure_order_id(self, ib: Any, order: Any) -> int:
        order_id = _optional_int_attr(order, "orderId")
        if order_id is not None:
            return order_id

        client = getattr(ib, "client", None)
        get_req_id = getattr(client, "getReqId", None)
        if not callable(get_req_id):
            raise OrderManagerError("Interactive Brokers client cannot allocate a parent order id for bracket orders.")

        try:
            order_id = int(get_req_id())
        except Exception as exc:
            raise OrderManagerError("Could not allocate a parent order id for bracket orders.") from exc

        if order_id <= 0:
            raise OrderManagerError("Interactive Brokers returned an invalid parent order id for bracket orders.")

        order.orderId = order_id
        return order_id

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
                settings=self._settings,
                logger=self._logger,
                order_id=status.order_id,
                side=side,
                quantity=status.total_quantity,
                price=None,
            )
        elif status.status == ORDER_STATUS_PARTIALLY_FILLED:
            _safe_notify(
                self._notifier,
                "send_fill",
                settings=self._settings,
                logger=self._logger,
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
                settings=self._settings,
                logger=self._logger,
                order_id=status.order_id,
                perm_id=status.perm_id,
                side=side,
                quantity=status.filled,
                price=status.avg_fill_price,
            )
        elif status.status == ORDER_STATUS_CANCELLED:
            _safe_notify(
                self._notifier,
                "send_order_cancelled",
                settings=self._settings,
                logger=self._logger,
                order_id=status.order_id,
                reason="Order cancelled.",
            )
        elif status.status in {ORDER_STATUS_INACTIVE, ORDER_STATUS_REJECTED}:
            _safe_notify(
                self._notifier,
                "send_order_rejected",
                settings=self._settings,
                logger=self._logger,
                order_id=status.order_id,
                reason=status.status,
            )

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

    return _protective_product_prices_from_reference(avg_fill_price, trade_plan)


def _protective_product_prices_from_trade_plan(trade_plan: Any) -> tuple[float, float]:
    product_price = _positive_float_attr(trade_plan, "product_price")
    return _protective_product_prices_from_reference(product_price, trade_plan)


def _protective_product_prices_from_reference(reference_price: float, trade_plan: Any) -> tuple[float, float]:
    product_sl_pct = _positive_float_attr(trade_plan, "product_sl_pct")
    product_tp_pct = _positive_float_attr(trade_plan, "product_tp_pct")
    stop_price = reference_price * (1 - product_sl_pct / 100)
    take_profit_price = reference_price * (1 + product_tp_pct / 100)

    if stop_price <= 0:
        raise OrderManagerError("Computed product stop price is not positive; refusing unprotected entry.")

    return stop_price, take_profit_price


def _oca_group_name(status: ManagedOrderStatus, trade_plan: Any) -> str:
    order_id = status.order_id
    if order_id is not None:
        return _oca_group_name_from_order_id(order_id)

    signal_id = _optional_text_attr(trade_plan, "signal_id") or "UNKNOWN"
    sanitized = "".join(character if character.isalnum() else "_" for character in signal_id)
    return f"ABM_{sanitized}_OCA"


def _oca_group_name_from_order_id(order_id: int) -> str:
    return f"ABM_ENTRY_{order_id}_OCA"


def _protective_order_payload(status: ManagedOrderStatus) -> dict[str, Any]:
    return {
        "order_id": status.order_id,
        "perm_id": status.perm_id,
        "status": status.status,
        "action": status.action,
        "order_type": status.order_type,
        "total_quantity": status.total_quantity,
    }


def _exposure_failure_was_handled(state: BotState | None) -> bool:
    if state is None:
        return False
    active_trade = state.active_trade
    defensive_exit = active_trade.get("defensive_exit")
    return bool(
        active_trade.get("protection_resubmitted_after_failure")
        or (isinstance(defensive_exit, dict) and defensive_exit.get("submitted"))
    )


def _missing_protection_type(protective_trades: tuple[Any, ...], trade_plan: Any | None) -> str:
    if not protective_trades:
        return "stop_loss,take_profit"

    missing: list[str] = []
    for trade in protective_trades:
        status = _managed_status(trade, trade_plan)
        if status.status in ACTIVE_ORDER_STATUSES:
            continue
        order_type = status.order_type.upper()
        if order_type in {"STP", "STOP"}:
            missing.append("stop_loss")
        elif order_type in {"LMT", "LIMIT"}:
            missing.append("take_profit")
        else:
            missing.append(order_type or "unknown")

    return ",".join(missing) if missing else "unknown"


def _protection_failure_message(
    active_trade: dict[str, Any],
    *,
    reason: str,
    missing_protection: str,
) -> str:
    return _format_key_value_message(
        "Entry exposure has no confirmed broker-side protection.",
        {
            "reason": reason,
            "con_id": active_trade.get("product_con_id"),
            "local_symbol": active_trade.get("product_local_symbol"),
            "quantity": active_trade.get("filled"),
            "fill_price": active_trade.get("avg_fill_price"),
            "missing_order_type": missing_protection,
            "manual_action": "Verify or create protective SL/TP in IB immediately.",
        },
    )


def _protection_resubmitted_message(active_trade: dict[str, Any], *, reason: str) -> str:
    return _format_key_value_message(
        "Replacement broker-side protection was submitted after failure.",
        {
            "reason": reason,
            "con_id": active_trade.get("product_con_id"),
            "local_symbol": active_trade.get("product_local_symbol"),
            "quantity": active_trade.get("filled"),
            "fill_price": active_trade.get("avg_fill_price"),
            "manual_action": "Emergency stop remains active; manually verify broker orders before resuming.",
        },
    )


def _format_key_value_message(title: str, fields: dict[str, Any]) -> str:
    lines = [title]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


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
    if value == DEFAULT_EXECUTION_ENTRY_ORDER_TYPE:
        return DEFAULT_EXECUTION_ENTRY_ORDER_TYPE
    raise OrderManagerError(f"execution.entry_order_type must be {DEFAULT_EXECUTION_ENTRY_ORDER_TYPE}.")


def _positive_float_setting(source: Any, name: str) -> float:
    value = getattr(source, name, None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OrderManagerError(f"execution.{name} must be a positive number.")
    result = float(value)
    if result <= 0:
        raise OrderManagerError(f"execution.{name} must be greater than zero.")
    return result


def _safe_notify(
    notifier: Any,
    method_name: str,
    *,
    settings: Any | None = None,
    logger: Any | None = None,
    **kwargs: Any,
) -> None:
    method = getattr(notifier, method_name, None)
    if not callable(method):
        if _notification_delivery_required(settings, method_name):
            raise OrderManagerError(f"Required notification method is unavailable: {method_name}.")
        return

    try:
        result = method(**kwargs)
    except Exception as exc:
        if logger is not None:
            logger.exception("Notification method failed. method=%s", method_name)
        if _notification_delivery_required(settings, method_name):
            raise OrderManagerError(f"Required notification failed: {method_name}: {exc}") from exc
        return

    _raise_on_required_notification_failure(result, method_name, settings=settings, logger=logger)


def _raise_on_required_notification_failure(
    result: Any,
    method_name: str,
    *,
    settings: Any | None,
    logger: Any | None = None,
) -> None:
    attempted = bool(getattr(result, "attempted", False))
    success = bool(getattr(result, "success", True))
    failed_count = getattr(result, "failed_count", None)
    if not attempted:
        message = f"Required notification was not attempted. method={method_name}"
        if logger is not None:
            logger.error(message)
        if _notification_delivery_required(settings, method_name):
            raise OrderManagerError(message)
        return

    if success:
        return

    message = f"Notification delivery failed. method={method_name} failed_count={failed_count}"
    if logger is not None:
        logger.error(message)
    if _notification_delivery_required(settings, method_name):
        raise OrderManagerError(message)


def _notification_delivery_required(settings: Any | None, method_name: str) -> bool:
    if settings is None:
        return False
    telegram_settings = getattr(settings, "telegram", None)
    if not bool(getattr(telegram_settings, "require_critical_delivery", True)):
        return False
    trading_mode = getattr(getattr(settings, "trading", None), "mode", None)
    if trading_mode not in PROTECTED_TRADING_MODES:
        return False
    return method_name in {"send_order_submitted", "send_fill", "send_critical_error", "send_emergency_stop"}


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
