"""Active monitoring for open turbo positions between candle cycles."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isclose, isfinite
from types import SimpleNamespace
from typing import Any

from domain.constants import (
    ACTIVE_ORDER_STATUSES,
    BROKER_ACTION_BUY,
    BROKER_ACTION_SELL,
    SIGNAL_DIRECTIONS,
    SIGNAL_DIRECTION_BUY,
    TERMINAL_TRADE_STATUSES,
)
from execution.order_builder import OrderBuilder
from execution.product_selector import ProductQuote
from ib_gateway.account import AccountReader, AccountSnapshot, OpenOrderSnapshot, PositionSnapshot
from ib_gateway.constants import IB_UNSET_PRICE, LIVE_MARKET_DATA_TYPE
from ib_gateway.contracts import build_execution_product_contract, qualify_contract
from logging_setup.logger import get_logger
from monitoring.emergency_stop import EmergencyStop
from state.state_store import BotState, StateStore


class ActiveTradeMonitorError(RuntimeError):
    """Raised when an active trade cannot be monitored safely."""


@dataclass(frozen=True)
class ProtectiveHealth:
    stop_order: OpenOrderSnapshot | None
    take_profit_order: OpenOrderSnapshot | None

    @property
    def has_stop(self) -> bool:
        return self.stop_order is not None

    @property
    def has_take_profit(self) -> bool:
        return self.take_profit_order is not None

    @property
    def healthy(self) -> bool:
        return self.has_stop and self.has_take_profit

    @property
    def missing_labels(self) -> tuple[str, ...]:
        missing: list[str] = []
        if self.stop_order is None:
            missing.append("stop_loss")
        if self.take_profit_order is None:
            missing.append("take_profit")
        return tuple(missing)


class ActiveTradeMonitor:
    """Check quotes, positions, and broker-side protection while a turbo is held."""

    def __init__(
        self,
        settings: Any,
        ib_client: Any,
        *,
        state_store: StateStore,
        notifier: Any | None,
        emergency_stop: EmergencyStop,
        interval_seconds: int | None = None,
        order_builder: OrderBuilder | None = None,
    ) -> None:
        self._settings = settings
        self._ib_client = ib_client
        self._state_store = state_store
        self._notifier = notifier
        self._emergency_stop = emergency_stop
        self._client_id = settings.ib.client_id
        self._interval_seconds = interval_seconds or settings.runtime.active_trade_monitor_seconds
        execution_settings = getattr(settings, "execution", None)
        self._protective_submit_timeout_seconds = _positive_float_setting(
            execution_settings,
            "protective_submit_timeout_seconds",
        )
        self._status_poll_seconds = _positive_float_setting(execution_settings, "status_poll_seconds")
        self._quote_poll_seconds = _positive_float_setting(execution_settings, "quote_poll_seconds")
        account_id = getattr(getattr(settings, "ib", None), "account_id", None)
        self._builder = order_builder or OrderBuilder(account_id=account_id)
        self._logger = get_logger("monitoring.active_trade")
        self._reported_issue_keys: set[str] = set()

    async def run_forever(self) -> None:
        """Run until cancelled by the owning runtime."""

        self._logger.info("Active trade monitor started. interval_seconds=%s", self._interval_seconds)
        try:
            while True:
                await self.check_once()
                await asyncio.sleep(self._interval_seconds)
        except asyncio.CancelledError:
            self._logger.info("Active trade monitor stopped.")
            raise

    async def check_once(self) -> None:
        """Run one monitoring pass if a monitorable active trade exists."""

        state = self._state_store.load()
        active_trade = state.active_trade
        if not _is_monitorable_active_trade(active_trade):
            return

        try:
            contract = await self._qualified_product_contract(active_trade)
            quote = await self._fetch_quote(contract)
        except Exception as exc:
            reason = f"Active trade quote check failed: {exc}"
            self._alert_once("quote_loss", reason, active_trade=active_trade)
            self._emergency_stop.activate(reason, state=state)
            return

        state = self._save_quote_state(state, quote)

        try:
            snapshot = await AccountReader(self._ib_client, client_id=self._client_id).read_snapshot()
        except Exception as exc:
            reason = f"Active trade account reconciliation failed: {exc}"
            self._alert_once("account_snapshot", reason, active_trade=active_trade)
            self._emergency_stop.activate(reason, state=state)
            return

        active_trade = state.active_trade
        position = _matching_position(snapshot, active_trade)
        if position is None:
            self._handle_missing_position(state, snapshot, active_trade)
            return

        health = _protective_health(snapshot, active_trade)
        if health.healthy:
            self._clear_resubmit_marker(state, health)
            return

        await self._repair_or_escalate(
            state=state,
            active_trade=active_trade,
            position=position,
            contract=contract,
            health=health,
        )

    async def _qualified_product_contract(self, active_trade: dict[str, Any]) -> Any:
        product = SimpleNamespace(
            sec_type=_required_text(active_trade, "product_asset_class"),
            con_id=_required_int(active_trade, "product_con_id"),
            exchange=_required_text(active_trade, "product_exchange"),
            currency=_required_text(active_trade, "product_currency"),
        )
        return await qualify_contract(self._ib_client, build_execution_product_contract(product))

    async def _fetch_quote(self, contract: Any) -> ProductQuote:
        ib = self._ib_client
        request_market_data_type = getattr(ib, "reqMarketDataType", None)
        if callable(request_market_data_type):
            request_market_data_type(LIVE_MARKET_DATA_TYPE)

        ticker = None
        try:
            ticker = ib.reqMktData(contract, "", False, False)
            return await self._wait_for_quote(ticker)
        finally:
            cancel_market_data = getattr(ib, "cancelMktData", None)
            if callable(cancel_market_data):
                cancel_market_data(contract)

    async def _wait_for_quote(self, ticker: Any) -> ProductQuote:
        max_age_seconds = self._settings.execution_products.quote_max_age_seconds
        deadline = asyncio.get_running_loop().time() + max_age_seconds
        last_error = "No quote received."

        while asyncio.get_running_loop().time() <= deadline:
            try:
                return _quote_from_ticker(ticker, max_age_seconds=max_age_seconds)
            except ActiveTradeMonitorError as exc:
                last_error = str(exc)
                await asyncio.sleep(self._quote_poll_seconds)

        raise ActiveTradeMonitorError(last_error)

    def _save_quote_state(self, state: BotState, quote: ProductQuote) -> BotState:
        def update(current: BotState) -> BotState:
            current.active_trade = {
                **current.active_trade,
                "monitor_last_checked_at": datetime.now(UTC).isoformat(),
                "monitor_last_bid": quote.bid,
                "monitor_last_ask": quote.ask,
                "monitor_last_quote_time": quote.quote_time.isoformat(),
                "monitor_last_spread_pct": quote.spread_pct,
            }
            return current

        return self._state_store.transaction(update)

    def _handle_missing_position(
        self,
        state: BotState,
        snapshot: AccountSnapshot,
        active_trade: dict[str, Any],
    ) -> None:
        execution = _matching_protective_execution(snapshot, active_trade)
        if execution is None:
            reason = _monitor_message(
                "Active trade position is missing but no matching protective fill was found.",
                active_trade,
                extra={"read_at": snapshot.read_at},
            )
            self._alert_once("missing_position_without_fill", reason, active_trade=active_trade)
            self._emergency_stop.activate(reason, state=state)
            return

        closed_at = datetime.now(UTC).isoformat()
        self._state_store.transaction(lambda current: _clear_active_trade_state(current))
        self._alert_once(
            "protective_fill_closed",
            _monitor_message(
                "Protective order fill reconciled; active trade state closed.",
                active_trade,
                extra={
                    "closed_at": closed_at,
                    "execution_order_id": getattr(execution, "order_id", None),
                    "execution_perm_id": getattr(execution, "perm_id", None),
                    "execution_price": getattr(execution, "price", None),
                },
            ),
            active_trade=active_trade,
        )

    async def _repair_or_escalate(
        self,
        *,
        state: BotState,
        active_trade: dict[str, Any],
        position: PositionSnapshot,
        contract: Any,
        health: ProtectiveHealth,
    ) -> None:
        issue_key = _issue_key("missing_protection", active_trade, ",".join(health.missing_labels))
        if active_trade.get("protective_resubmit_issue_key") == issue_key:
            reason = _monitor_message(
                "Broker-side protection is still missing after one resubmission attempt.",
                active_trade,
                extra={
                    "missing": ",".join(health.missing_labels),
                    "position": position.position,
                    "manual_action": "Create or verify protective SL/TP manually in IB.",
                },
            )
            self._alert_once("missing_protection_after_resubmit", reason, active_trade=active_trade)
            self._emergency_stop.activate(reason, state=state)
            return

        state = self._state_store.transaction(
            lambda current: _merge_active_trade_state(
                current,
                {
                    **active_trade,
                    "protective_resubmit_issue_key": issue_key,
                    "protective_resubmit_attempted_at": datetime.now(UTC).isoformat(),
                },
            )
        )

        try:
            await self._resubmit_missing_protection(
                contract=contract,
                active_trade=state.active_trade,
                position=position,
                health=health,
            )
            snapshot = await AccountReader(self._ib_client, client_id=self._client_id).read_snapshot(
                include_executions=False
            )
            repaired_health = _protective_health(snapshot, state.active_trade)
            if not repaired_health.healthy:
                raise ActiveTradeMonitorError(
                    f"resubmitted orders are not active. missing={','.join(repaired_health.missing_labels)}"
                )
        except Exception as exc:
            reason = _monitor_message(
                f"Failed to restore broker-side protection: {exc}",
                active_trade,
                extra={
                    "missing": ",".join(health.missing_labels),
                    "position": position.position,
                    "manual_action": "Create or verify protective SL/TP manually in IB.",
                },
            )
            self._alert_once("missing_protection_resubmit_failed", reason, active_trade=active_trade)
            self._emergency_stop.activate(reason, state=state)
            return

        repaired_state = self._state_store.transaction(
            lambda current: _mark_repaired_protection(current, repaired_health)
        )
        self._notify(
            _monitor_message(
                "Broker-side protection restored by active trade monitor.",
                repaired_state.active_trade,
                extra={"restored": ",".join(health.missing_labels)},
            )
        )

    async def _resubmit_missing_protection(
        self,
        *,
        contract: Any,
        active_trade: dict[str, Any],
        position: PositionSnapshot,
        health: ProtectiveHealth,
    ) -> None:
        quantity = abs(float(position.position))
        trade_plan = SimpleNamespace(
            order_action=_entry_order_action(active_trade),
            signal_id=(
                _optional_text(active_trade.get("signal_id"))
                or _optional_text(active_trade.get("submitted_signal_id"))
            ),
        )
        stop_order, take_profit_order = self._builder.build_exit_oca_orders(
            trade_plan,
            quantity=quantity,
            stop_loss_price=_required_float(active_trade, "product_stop_price"),
            take_profit_price=_required_float(active_trade, "product_take_profit_price"),
            oca_group=_required_text(active_trade, "protective_oca_group"),
        )

        orders = []
        if not health.has_stop:
            if health.has_take_profit:
                stop_order.transmit = True
            orders.append(stop_order)
        if not health.has_take_profit:
            take_profit_order.transmit = True
            orders.append(take_profit_order)

        if not orders:
            return

        for order in orders:
            self._logger.warning(
                "Resubmitting missing protective order. signal_id=%s order_type=%s quantity=%s",
                trade_plan.signal_id,
                getattr(order, "orderType", ""),
                getattr(order, "totalQuantity", 0),
            )
            self._ib_client.placeOrder(contract, order)

        await self._wait_for_protection(active_trade)

    async def _wait_for_protection(self, active_trade: dict[str, Any]) -> None:
        deadline = asyncio.get_running_loop().time() + self._protective_submit_timeout_seconds
        last_missing: tuple[str, ...] = ()

        while asyncio.get_running_loop().time() <= deadline:
            snapshot = await AccountReader(self._ib_client, client_id=self._client_id).read_snapshot(
                include_executions=False
            )
            health = _protective_health(snapshot, active_trade)
            if health.healthy:
                return
            last_missing = health.missing_labels
            await asyncio.sleep(self._status_poll_seconds)

        raise ActiveTradeMonitorError(f"protective orders were not confirmed. missing={','.join(last_missing)}")

    def _clear_resubmit_marker(self, state: BotState, health: ProtectiveHealth) -> None:
        active_trade = state.active_trade
        if "protective_resubmit_issue_key" not in active_trade:
            return
        active_trade = {**active_trade}
        active_trade.pop("protective_resubmit_issue_key", None)
        active_trade["protective_orders_confirmed"] = True
        active_trade["protective_orders"] = _protective_order_payloads(health)
        self._state_store.transaction(lambda current: _merge_active_trade_state(current, active_trade))

    def _alert_once(self, key: str, message: str, *, active_trade: dict[str, Any]) -> None:
        issue_key = _issue_key(key, active_trade)
        if issue_key in self._reported_issue_keys:
            return
        self._reported_issue_keys.add(issue_key)
        self._logger.critical(message)
        self._notify(message)

    def _notify(self, message: str) -> None:
        if self._notifier is None:
            return
        method = getattr(self._notifier, "send_critical_error", None)
        if callable(method):
            method(message="Active trade monitor alert", details=message)
            return
        send_message = getattr(self._notifier, "send_message", None)
        if callable(send_message):
            send_message(message)


def _is_monitorable_active_trade(active_trade: dict[str, Any]) -> bool:
    if not active_trade:
        return False
    status = _optional_text(active_trade.get("status"))
    if status in TERMINAL_TRADE_STATUSES:
        return False
    filled = float(active_trade.get("filled", 0.0) or 0.0)
    return filled > 0 and active_trade.get("product_con_id") is not None


def _clear_active_trade_state(state: BotState) -> BotState:
    state.active_trade = {}
    return state


def _merge_active_trade_state(state: BotState, active_trade: dict[str, Any]) -> BotState:
    state.active_trade = {**state.active_trade, **active_trade}
    return state


def _mark_repaired_protection(state: BotState, health: ProtectiveHealth) -> BotState:
    active_trade = {
        **state.active_trade,
        "protective_orders_confirmed": True,
        "protective_orders": _protective_order_payloads(health),
    }
    active_trade.pop("protective_resubmit_issue_key", None)
    state.active_trade = active_trade
    return state


def _matching_position(snapshot: AccountSnapshot, active_trade: dict[str, Any]) -> PositionSnapshot | None:
    product_con_id = _required_int(active_trade, "product_con_id")
    for position in snapshot.positions:
        if position.con_id == product_con_id and abs(float(position.position)) > 0:
            return position
    return None


def _protective_health(snapshot: AccountSnapshot, active_trade: dict[str, Any]) -> ProtectiveHealth:
    product_con_id = _required_int(active_trade, "product_con_id")
    exit_action = _exit_order_action(active_trade)
    stop_price = _required_float(active_trade, "product_stop_price")
    take_profit_price = _required_float(active_trade, "product_take_profit_price")
    known_stop_ids, known_take_profit_ids = _known_protective_ids(active_trade)

    stop_order = None
    take_profit_order = None
    for order in snapshot.open_orders:
        if order.con_id != product_con_id:
            continue
        if order.action != exit_action:
            continue
        if order.status not in ACTIVE_ORDER_STATUSES:
            continue

        order_key = _order_identity(order)
        if _is_stop_order(order):
            if order_key in known_stop_ids or _price_matches(order.aux_price, stop_price):
                stop_order = order
        elif _is_take_profit_order(order):
            if order_key in known_take_profit_ids or _price_matches(order.limit_price, take_profit_price):
                take_profit_order = order

    return ProtectiveHealth(stop_order=stop_order, take_profit_order=take_profit_order)


def _matching_protective_execution(snapshot: AccountSnapshot, active_trade: dict[str, Any]) -> Any | None:
    known_ids = set().union(*_known_protective_ids(active_trade))
    product_con_id = _required_int(active_trade, "product_con_id")
    exit_action = _exit_order_action(active_trade)
    for execution in snapshot.executions:
        if execution.con_id != product_con_id:
            continue
        if not _execution_side_matches(execution.side, exit_action):
            continue
        if _order_identity(execution) in known_ids:
            return execution
    return None


def _known_protective_ids(active_trade: dict[str, Any]) -> tuple[set[tuple[str, int]], set[tuple[str, int]]]:
    stop_ids: set[tuple[str, int]] = set()
    take_profit_ids: set[tuple[str, int]] = set()
    orders = active_trade.get("protective_orders")
    if not isinstance(orders, list):
        return stop_ids, take_profit_ids

    for order in orders:
        if not isinstance(order, dict):
            continue
        order_type = str(order.get("order_type", "") or "").upper()
        if order_type in {"STP", "STOP"}:
            target = stop_ids
        elif order_type in {"LMT", "LIMIT"}:
            target = take_profit_ids
        else:
            target = None
        if target is None:
            continue
        order_id = _optional_positive_int(order.get("order_id"))
        perm_id = _optional_positive_int(order.get("perm_id"))
        if order_id is not None:
            target.add(("order_id", order_id))
        if perm_id is not None:
            target.add(("perm_id", perm_id))

    return stop_ids, take_profit_ids


def _protective_order_payloads(health: ProtectiveHealth) -> list[dict[str, Any]]:
    payloads = []
    for order in (health.stop_order, health.take_profit_order):
        if order is None:
            continue
        payloads.append(
            {
                "order_id": order.order_id,
                "perm_id": order.perm_id,
                "status": order.status,
                "action": order.action,
                "order_type": order.order_type,
                "total_quantity": order.total_quantity,
            }
        )
    return payloads


def _quote_from_ticker(ticker: Any, *, max_age_seconds: float) -> ProductQuote:
    bid = _usable_price_attr(ticker, "bid")
    ask = _usable_price_attr(ticker, "ask")
    if ask < bid:
        raise ActiveTradeMonitorError(f"Quote is crossed. bid={bid} ask={ask}.")

    quote_time = _required_quote_time(ticker)
    age_seconds = (datetime.now(UTC) - quote_time).total_seconds()
    if age_seconds < 0:
        raise ActiveTradeMonitorError("Quote timestamp is in the future.")
    if age_seconds > max_age_seconds:
        raise ActiveTradeMonitorError(
            f"Quote is stale. age_seconds={age_seconds:.3f} max_age_seconds={max_age_seconds}."
        )

    midpoint = (bid + ask) / 2
    spread_pct = (ask - bid) / midpoint * 100
    return ProductQuote(bid=bid, ask=ask, quote_time=quote_time, spread_pct=spread_pct)


def _required_quote_time(ticker: Any) -> datetime:
    value = getattr(ticker, "time", None)
    if value is None:
        value = getattr(ticker, "timestamp", None)
    if value is None:
        raise ActiveTradeMonitorError("Quote timestamp is missing.")

    timestamp = value if isinstance(value, datetime) else None
    if timestamp is None:
        try:
            timestamp = datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise ActiveTradeMonitorError("Quote timestamp is invalid.") from exc

    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


def _usable_price_attr(source: Any, name: str) -> float:
    value = getattr(source, name, None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ActiveTradeMonitorError(f"Quote {name} is missing.")
    result = float(value)
    if not isfinite(result) or result <= 0 or result >= IB_UNSET_PRICE:
        raise ActiveTradeMonitorError(f"Quote {name} is invalid.")
    return result


def _is_stop_order(order: OpenOrderSnapshot) -> bool:
    return order.order_type.upper() in {"STP", "STOP"} or order.aux_price is not None


def _is_take_profit_order(order: OpenOrderSnapshot) -> bool:
    return order.order_type.upper() in {"LMT", "LIMIT"} or order.limit_price is not None


def _entry_order_action(active_trade: dict[str, Any]) -> str:
    action = _optional_text(active_trade.get("action"))
    if action in SIGNAL_DIRECTIONS:
        return action
    signal_side = _optional_text(active_trade.get("signal_side"))
    if signal_side in SIGNAL_DIRECTIONS:
        return signal_side
    raise ActiveTradeMonitorError("Active trade entry action is missing.")


def _exit_order_action(active_trade: dict[str, Any]) -> str:
    return BROKER_ACTION_SELL if _entry_order_action(active_trade) == BROKER_ACTION_BUY else BROKER_ACTION_BUY


def _execution_side_matches(side: str, exit_action: str) -> bool:
    normalized = str(side or "").upper()
    if normalized == exit_action:
        return True
    if exit_action == SIGNAL_DIRECTION_BUY:
        return normalized == "BOT"
    return normalized == "SLD"


def _price_matches(value: float | None, expected: float) -> bool:
    return value is not None and isclose(float(value), expected, rel_tol=0.0, abs_tol=0.000001)


def _order_identity(source: Any) -> tuple[str, int] | None:
    order_id = _optional_positive_int(getattr(source, "order_id", None))
    if order_id is not None:
        return ("order_id", order_id)
    perm_id = _optional_positive_int(getattr(source, "perm_id", None))
    if perm_id is not None:
        return ("perm_id", perm_id)
    return None


def _required_text(source: dict[str, Any], name: str) -> str:
    value = _optional_text(source.get(name))
    if value is None:
        raise ActiveTradeMonitorError(f"Active trade {name} is missing.")
    return value


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _required_int(source: dict[str, Any], name: str) -> int:
    value = _optional_positive_int(source.get(name))
    if value is None:
        raise ActiveTradeMonitorError(f"Active trade {name} is missing or invalid.")
    return value


def _required_float(source: dict[str, Any], name: str) -> float:
    value = source.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ActiveTradeMonitorError(f"Active trade {name} is missing or invalid.")
    result = float(value)
    if result <= 0:
        raise ActiveTradeMonitorError(f"Active trade {name} must be greater than zero.")
    return result


def _optional_positive_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _positive_float_setting(source: Any, name: str) -> float:
    value = getattr(source, name, None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ActiveTradeMonitorError(f"execution.{name} must be a positive number.")
    result = float(value)
    if result <= 0:
        raise ActiveTradeMonitorError(f"execution.{name} must be greater than zero.")
    return result


def _issue_key(prefix: str, active_trade: dict[str, Any], suffix: str | None = None) -> str:
    parts = [
        prefix,
        str(active_trade.get("submitted_signal_id") or active_trade.get("signal_id") or ""),
        str(active_trade.get("product_con_id") or ""),
    ]
    if suffix:
        parts.append(suffix)
    return ":".join(parts)


def _monitor_message(
    title: str,
    active_trade: dict[str, Any],
    *,
    extra: dict[str, Any] | None = None,
) -> str:
    fields = {
        "signal_id": active_trade.get("submitted_signal_id") or active_trade.get("signal_id"),
        "con_id": active_trade.get("product_con_id"),
        "local_symbol": active_trade.get("product_local_symbol"),
        "entry_order_id": active_trade.get("order_id"),
        "stop_price": active_trade.get("product_stop_price"),
        "take_profit_price": active_trade.get("product_take_profit_price"),
    }
    if extra:
        fields.update(extra)

    lines = [title]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        lines.append(f"{key}: {value}")
    return "\n".join(lines)
