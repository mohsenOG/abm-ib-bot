"""Read account state from Interactive Brokers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from logging_setup.logger import get_logger


class AccountReadError(RuntimeError):
    """Raised when an account snapshot cannot be read safely."""


@dataclass(frozen=True)
class AccountValueSnapshot:
    account: str
    tag: str
    value: str
    currency: str
    model_code: str | None


@dataclass(frozen=True)
class PositionSnapshot:
    account: str
    con_id: int | None
    symbol: str
    local_symbol: str
    sec_type: str
    exchange: str
    currency: str
    position: float
    avg_cost: float


@dataclass(frozen=True)
class OpenOrderSnapshot:
    order_id: int | None
    perm_id: int | None
    account: str
    action: str
    order_type: str
    total_quantity: float
    status: str
    filled: float
    remaining: float
    avg_fill_price: float
    con_id: int | None
    symbol: str
    local_symbol: str
    sec_type: str
    exchange: str
    currency: str
    limit_price: float | None
    aux_price: float | None


@dataclass(frozen=True)
class ExecutionSnapshot:
    exec_id: str
    time: str
    account: str
    side: str
    shares: float
    price: float
    order_id: int | None
    perm_id: int | None
    client_id: int | None
    cum_qty: float
    avg_price: float
    con_id: int | None
    symbol: str
    local_symbol: str
    sec_type: str
    exchange: str
    currency: str


@dataclass(frozen=True)
class AccountSnapshot:
    read_at: str
    account_values: tuple[AccountValueSnapshot, ...]
    positions: tuple[PositionSnapshot, ...]
    open_orders: tuple[OpenOrderSnapshot, ...]
    executions: tuple[ExecutionSnapshot, ...]


class AccountReader:
    """Read account values, positions, open orders, and executions from IB."""

    def __init__(self, ib_client: Any) -> None:
        self._ib_client = ib_client
        self._logger = get_logger("ib_gateway.account")

    async def read_snapshot(self, *, include_executions: bool = True) -> AccountSnapshot:
        """Read a full account snapshot from the connected IB client."""

        self._logger.info("Reading IB account snapshot.")
        return AccountSnapshot(
            read_at=datetime.now(timezone.utc).isoformat(),
            account_values=self.read_account_values(),
            positions=await self.read_positions(),
            open_orders=await self.read_open_orders(),
            executions=await self.read_executions() if include_executions else (),
        )

    def read_account_values(self) -> tuple[AccountValueSnapshot, ...]:
        """Return normalized IB account values."""

        ib = self._connected_ib()
        try:
            values = ib.accountValues()
        except Exception as exc:
            self._logger.exception("Failed to read IB account values.")
            raise AccountReadError("Failed to read Interactive Brokers account values.") from exc

        return tuple(_account_value_snapshot(value) for value in values)

    async def read_positions(self) -> tuple[PositionSnapshot, ...]:
        """Return normalized current positions."""

        ib = self._connected_ib()
        try:
            positions = await ib.reqPositionsAsync()
        except Exception as exc:
            self._logger.exception("Failed to read IB positions.")
            raise AccountReadError("Failed to read Interactive Brokers positions.") from exc

        return tuple(_position_snapshot(position) for position in positions)

    async def read_open_orders(self) -> tuple[OpenOrderSnapshot, ...]:
        """Return normalized open orders without submitting or changing orders."""

        ib = self._connected_ib()
        try:
            open_trades = await ib.reqOpenOrdersAsync()
        except Exception as exc:
            self._logger.exception("Failed to read IB open orders.")
            raise AccountReadError("Failed to read Interactive Brokers open orders.") from exc

        return tuple(_open_order_snapshot(trade) for trade in open_trades)

    async def read_executions(self) -> tuple[ExecutionSnapshot, ...]:
        """Return normalized recent executions available from IB."""

        ib = self._connected_ib()
        try:
            fills = await ib.reqExecutionsAsync()
        except Exception as exc:
            self._logger.exception("Failed to read IB executions.")
            raise AccountReadError("Failed to read Interactive Brokers executions.") from exc

        return tuple(_execution_snapshot(fill) for fill in fills)

    def _connected_ib(self) -> Any:
        ib = _resolve_ib_client(self._ib_client)
        is_connected = getattr(ib, "isConnected", None)
        if callable(is_connected) and not is_connected():
            raise AccountReadError("Interactive Brokers is disconnected.")
        return ib


def _resolve_ib_client(ib_client: Any) -> Any:
    if _looks_like_ib_client(ib_client):
        return ib_client

    try:
        candidate = getattr(ib_client, "ib")
    except Exception as exc:
        raise AccountReadError("Could not access Interactive Brokers client.") from exc

    if not _looks_like_ib_client(candidate):
        raise AccountReadError("AccountReader requires an Interactive Brokers client.")

    return candidate


def _looks_like_ib_client(value: Any) -> bool:
    return all(
        callable(getattr(value, name, None))
        for name in (
            "accountValues",
            "reqPositionsAsync",
            "reqOpenOrdersAsync",
            "reqExecutionsAsync",
            "isConnected",
        )
    )


def _account_value_snapshot(value: Any) -> AccountValueSnapshot:
    return AccountValueSnapshot(
        account=_string_attr(value, "account"),
        tag=_string_attr(value, "tag"),
        value=_string_attr(value, "value"),
        currency=_string_attr(value, "currency"),
        model_code=_optional_string_attr(value, "modelCode"),
    )


def _position_snapshot(position: Any) -> PositionSnapshot:
    contract = getattr(position, "contract", None)
    return PositionSnapshot(
        account=_string_attr(position, "account"),
        con_id=_optional_int_attr(contract, "conId"),
        symbol=_string_attr(contract, "symbol"),
        local_symbol=_string_attr(contract, "localSymbol"),
        sec_type=_string_attr(contract, "secType"),
        exchange=_string_attr(contract, "exchange"),
        currency=_string_attr(contract, "currency"),
        position=_float_attr(position, "position"),
        avg_cost=_float_attr(position, "avgCost"),
    )


def _open_order_snapshot(trade: Any) -> OpenOrderSnapshot:
    contract = getattr(trade, "contract", None)
    order = getattr(trade, "order", None)
    status = getattr(trade, "orderStatus", None)
    return OpenOrderSnapshot(
        order_id=_optional_int_attr(order, "orderId"),
        perm_id=_optional_int_attr(order, "permId"),
        account=_string_attr(order, "account"),
        action=_string_attr(order, "action"),
        order_type=_string_attr(order, "orderType"),
        total_quantity=_float_attr(order, "totalQuantity"),
        status=_string_attr(status, "status"),
        filled=_float_attr(status, "filled"),
        remaining=_float_attr(status, "remaining"),
        avg_fill_price=_float_attr(status, "avgFillPrice"),
        con_id=_optional_int_attr(contract, "conId"),
        symbol=_string_attr(contract, "symbol"),
        local_symbol=_string_attr(contract, "localSymbol"),
        sec_type=_string_attr(contract, "secType"),
        exchange=_string_attr(contract, "exchange"),
        currency=_string_attr(contract, "currency"),
        limit_price=_optional_price_attr(order, "lmtPrice"),
        aux_price=_optional_price_attr(order, "auxPrice"),
    )


def _execution_snapshot(fill: Any) -> ExecutionSnapshot:
    contract = getattr(fill, "contract", None)
    execution = getattr(fill, "execution", None)
    return ExecutionSnapshot(
        exec_id=_string_attr(execution, "execId"),
        time=_string_attr(execution, "time"),
        account=_string_attr(execution, "acctNumber"),
        side=_string_attr(execution, "side"),
        shares=_float_attr(execution, "shares"),
        price=_float_attr(execution, "price"),
        order_id=_optional_int_attr(execution, "orderId"),
        perm_id=_optional_int_attr(execution, "permId"),
        client_id=_optional_int_attr(execution, "clientId"),
        cum_qty=_float_attr(execution, "cumQty"),
        avg_price=_float_attr(execution, "avgPrice"),
        con_id=_optional_int_attr(contract, "conId"),
        symbol=_string_attr(contract, "symbol"),
        local_symbol=_string_attr(contract, "localSymbol"),
        sec_type=_string_attr(contract, "secType"),
        exchange=_string_attr(contract, "exchange"),
        currency=_string_attr(contract, "currency"),
    )


def _string_attr(source: Any, name: str) -> str:
    value = getattr(source, name, "")
    if value is None:
        return ""
    return str(value)


def _optional_string_attr(source: Any, name: str) -> str | None:
    value = getattr(source, name, None)
    if value is None:
        return None
    text = str(value)
    return text if text else None


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


def _optional_price_attr(source: Any, name: str) -> float | None:
    value = getattr(source, name, None)
    if value is None:
        return None
    price = float(value)
    return price if price < 1e100 else None
