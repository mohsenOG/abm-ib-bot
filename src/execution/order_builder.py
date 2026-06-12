"""Build Interactive Brokers orders from approved trade plans."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from ib_async import LimitOrder, MarketOrder, Order, StopOrder


OrderAction = Literal["BUY", "SELL"]
EntryOrderType = Literal["market", "limit"]


class OrderBuilderError(ValueError):
    """Raised when an order cannot be built safely."""


@dataclass(frozen=True)
class BuiltOrderSet:
    entry_order: Order
    stop_loss_order: Order | None = None
    take_profit_order: Order | None = None

    @property
    def orders(self) -> tuple[Order, ...]:
        result = [self.entry_order]
        if self.stop_loss_order is not None:
            result.append(self.stop_loss_order)
        if self.take_profit_order is not None:
            result.append(self.take_profit_order)
        return tuple(result)

    @property
    def has_protective_orders(self) -> bool:
        return self.stop_loss_order is not None or self.take_profit_order is not None


class OrderBuilder:
    """Convert approved trade plans into IB order objects without submitting."""

    def __init__(self, *, account_id: str | None = None) -> None:
        self._account_id = _optional_text(account_id, "account_id")

    def build_order_set(
        self,
        trade_plan: Any,
        *,
        order_type: EntryOrderType = "market",
        limit_price: float | None = None,
        stop_loss_price: float | None = None,
        take_profit_price: float | None = None,
        parent_order_id: int | None = None,
    ) -> BuiltOrderSet:
        """Build an entry order and optional protective orders.

        Protective orders are attached only when ``parent_order_id`` is supplied.
        This keeps the builder side-effect free while still allowing the order
        manager to create attached orders after it has a real IB order id.
        """

        action = _order_action(trade_plan)
        quantity = _positive_float_attr(trade_plan, "quantity")
        signal_id = _optional_text(getattr(trade_plan, "signal_id", None), "signal_id")

        entry_order = self._build_entry_order(
            action=action,
            quantity=quantity,
            order_type=order_type,
            limit_price=limit_price,
        )
        _apply_common_fields(entry_order, account_id=self._account_id, order_ref=signal_id)

        protective_orders = self._build_protective_orders(
            action=action,
            quantity=quantity,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            parent_order_id=parent_order_id,
            order_ref=signal_id,
        )

        if protective_orders and parent_order_id is None:
            raise OrderBuilderError("parent_order_id is required when building protective orders.")

        if protective_orders:
            entry_order.transmit = False

        return BuiltOrderSet(
            entry_order=entry_order,
            stop_loss_order=protective_orders[0],
            take_profit_order=protective_orders[1],
        )

    def build_market_order(self, trade_plan: Any) -> Order:
        """Build a single market entry order."""

        return self.build_order_set(trade_plan, order_type="market").entry_order

    def build_limit_order(self, trade_plan: Any, *, limit_price: float) -> Order:
        """Build a single limit entry order."""

        return self.build_order_set(trade_plan, order_type="limit", limit_price=limit_price).entry_order

    def _build_entry_order(
        self,
        *,
        action: OrderAction,
        quantity: float,
        order_type: EntryOrderType,
        limit_price: float | None,
    ) -> Order:
        if order_type == "market":
            if limit_price is not None:
                raise OrderBuilderError("limit_price is only valid for limit orders.")
            return MarketOrder(action, quantity)

        if order_type == "limit":
            price = _positive_float(limit_price, "limit_price")
            return LimitOrder(action, quantity, price)

        raise OrderBuilderError("order_type must be 'market' or 'limit'.")

    def _build_protective_orders(
        self,
        *,
        action: OrderAction,
        quantity: float,
        stop_loss_price: float | None,
        take_profit_price: float | None,
        parent_order_id: int | None,
        order_ref: str | None,
    ) -> tuple[Order | None, Order | None]:
        stop_loss_order: Order | None = None
        take_profit_order: Order | None = None
        exit_action = _opposite_action(action)

        if stop_loss_price is not None:
            stop_loss_order = StopOrder(exit_action, quantity, _positive_float(stop_loss_price, "stop_loss_price"))
            _apply_attached_fields(
                stop_loss_order,
                account_id=self._account_id,
                parent_order_id=parent_order_id,
                order_ref=order_ref,
            )

        if take_profit_price is not None:
            take_profit_order = LimitOrder(
                exit_action,
                quantity,
                _positive_float(take_profit_price, "take_profit_price"),
            )
            _apply_attached_fields(
                take_profit_order,
                account_id=self._account_id,
                parent_order_id=parent_order_id,
                order_ref=order_ref,
            )

        if stop_loss_order is not None:
            stop_loss_order.transmit = take_profit_order is None
        if take_profit_order is not None:
            take_profit_order.transmit = True

        return stop_loss_order, take_profit_order


def _order_action(trade_plan: Any) -> OrderAction:
    value = getattr(trade_plan, "order_action", None)
    if value not in {"BUY", "SELL"}:
        raise OrderBuilderError("trade_plan.order_action must be BUY or SELL.")
    return value


def _opposite_action(action: OrderAction) -> OrderAction:
    return "SELL" if action == "BUY" else "BUY"


def _apply_common_fields(order: Order, *, account_id: str | None, order_ref: str | None) -> None:
    if account_id is not None:
        order.account = account_id
    if order_ref is not None:
        order.orderRef = order_ref


def _apply_attached_fields(
    order: Order,
    *,
    account_id: str | None,
    parent_order_id: int | None,
    order_ref: str | None,
) -> None:
    _apply_common_fields(order, account_id=account_id, order_ref=order_ref)
    if parent_order_id is not None:
        order.parentId = parent_order_id


def _positive_float_attr(source: Any, name: str) -> float:
    return _positive_float(getattr(source, name, None), name)


def _positive_float(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OrderBuilderError(f"{name} must be a positive number.")
    result = float(value)
    if result <= 0:
        raise OrderBuilderError(f"{name} must be greater than zero.")
    return result


def _optional_text(value: Any | None, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise OrderBuilderError(f"{name} must be a non-empty string when provided.")
    return value.strip()
