"""Approve or block trades using simple fixed-capital slots."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from config.settings import AppSettings, RiskSettings
from risk.sizing import QuantityRules, RiskSizingError, calculate_quantity


SignalSide = Literal["BUY", "SELL"]
ExecutionSide = Literal["long", "short"]
OrderAction = Literal["BUY", "SELL"]
RiskDecisionStatus = Literal["approved", "blocked"]

ACTIVE_ORDER_STATUSES = {
    "PendingSubmit",
    "PreSubmitted",
    "Submitted",
    "PartiallyFilled",
}


class RiskManagerError(ValueError):
    """Raised when risk cannot evaluate a trade safely."""


@dataclass(frozen=True)
class ExecutionProduct:
    asset_class: str
    con_id: int
    local_symbol: str | None
    exchange: str
    currency: str
    leverage: float
    issuer_fee_pct: float
    bid: float | None = None
    ask: float | None = None
    quote_time: str | None = None
    spread_pct: float | None = None
    commission_pct: float | None = None
    estimated_total_cost_pct: float | None = None


@dataclass(frozen=True)
class TradePlan:
    signal_id: str
    signal_timestamp: str
    signal_side: SignalSide
    execution_side: ExecutionSide
    order_action: OrderAction
    quantity: float
    capital_allocated: float
    signal_price: float
    underlying_symbol: str
    underlying_entry_price: float
    atr: float
    atr_pct: float
    underlying_sl_price: float
    underlying_tp_price: float
    underlying_sl_pct: float
    underlying_tp_pct: float
    product_leverage: float
    product_sl_pct: float
    product_tp_pct: float
    product_price: float | None
    product: ExecutionProduct


@dataclass(frozen=True)
class RiskDecision:
    status: RiskDecisionStatus
    reason: str | None
    trade_plan: TradePlan | None

    @property
    def approved(self) -> bool:
        return self.status == "approved"


class RiskManager:
    """Evaluate signals against current account exposure and fixed slot limits."""

    def __init__(
        self,
        settings: AppSettings,
        *,
        quantity_rules: QuantityRules | None = None,
    ) -> None:
        self._settings = settings
        self._risk = settings.risk
        self._quantity_rules = quantity_rules or QuantityRules()

    def evaluate_signal(
        self,
        signal: Any,
        account_snapshot: Any,
        *,
        last_signal_id: str | None = None,
        selected_product: ExecutionProduct | None = None,
        product_price: float | None = None,
    ) -> RiskDecision:
        """Return an approved trade plan or a blocked reason."""

        normalized_signal = _normalize_signal(signal)
        if selected_product is None:
            return _blocked("No selected execution product was provided.")
        product = _normalize_product(selected_product)
        quote_error = _validate_product_quote(
            product,
            max_age_seconds=self._settings.execution_products.quote_max_age_seconds,
        )
        if quote_error is not None:
            return _blocked(quote_error)
        effective_product_price = product.ask

        if self._settings.trading.mode == "alert_only":
            return _blocked("Trading mode is alert_only.")

        if normalized_signal.signal_id == last_signal_id:
            return _blocked("Signal was already processed.")

        if normalized_signal.side == "BUY" and not self._settings.trading.allowed_directions.long:
            return _blocked("Long trades are disabled by trading.allowed_directions.long.")

        if normalized_signal.side == "SELL" and not self._settings.trading.allowed_directions.short:
            return _blocked("Short trades are disabled by trading.allowed_directions.short.")

        active_position_slots = _count_active_position_slots(account_snapshot, product)
        active_order_slots = _count_active_order_slots(account_snapshot, product)
        active_slots = active_position_slots + active_order_slots

        if active_order_slots > 0:
            return _blocked("An active order already exists for the selected execution product.")

        if active_slots >= self._risk.max_concurrent_position_slots:
            return _blocked("No position slots are available.")

        product_sl_pct = normalized_signal.underlying_sl_pct * product.leverage
        product_tp_pct = normalized_signal.underlying_tp_pct * product.leverage

        if product_sl_pct >= 100:
            return _blocked("Product stop-loss percentage is 100% or greater; protective stop price would be invalid.")

        try:
            quantity = calculate_quantity(
                self._risk.capital_per_position,
                product_price=effective_product_price,
                quantity_rules=self._quantity_rules,
            )
        except RiskSizingError as exc:
            return _blocked(str(exc))

        estimated_value = quantity * effective_product_price
        if estimated_value > self._settings.execution_products.max_order_value_eur:
            return _blocked(
                "Estimated order value exceeds configured max_order_value_eur. "
                f"value={estimated_value:.6f} "
                f"max_order_value_eur={self._settings.execution_products.max_order_value_eur}."
            )

        return RiskDecision(
            status="approved",
            reason=None,
            trade_plan=TradePlan(
                signal_id=normalized_signal.signal_id,
                signal_timestamp=normalized_signal.timestamp,
                signal_side=normalized_signal.side,
                execution_side=_execution_side_for_signal(normalized_signal.side),
                order_action=_order_action_for_signal(normalized_signal.side),
                quantity=quantity,
                capital_allocated=self._risk.capital_per_position,
                signal_price=normalized_signal.price,
                underlying_symbol=normalized_signal.underlying_symbol,
                underlying_entry_price=normalized_signal.underlying_entry_price,
                atr=normalized_signal.atr,
                atr_pct=normalized_signal.atr_pct,
                underlying_sl_price=normalized_signal.underlying_sl_price,
                underlying_tp_price=normalized_signal.underlying_tp_price,
                underlying_sl_pct=normalized_signal.underlying_sl_pct,
                underlying_tp_pct=normalized_signal.underlying_tp_pct,
                product_leverage=product.leverage,
                product_sl_pct=product_sl_pct,
                product_tp_pct=product_tp_pct,
                product_price=effective_product_price,
                product=product,
            ),
        )

    @property
    def risk_settings(self) -> RiskSettings:
        return self._risk

@dataclass(frozen=True)
class _NormalizedSignal:
    signal_id: str
    timestamp: str
    side: SignalSide
    price: float
    underlying_symbol: str
    underlying_entry_price: float
    atr: float
    atr_pct: float
    underlying_sl_price: float
    underlying_tp_price: float
    underlying_sl_pct: float
    underlying_tp_pct: float


def _normalize_signal(signal: Any) -> _NormalizedSignal:
    signal_id = _required_text_attr(signal, "signal_id")
    timestamp = _required_text_attr(signal, "timestamp")
    raw_side = _required_text_attr(signal, "side").upper()
    price = _positive_float_attr(signal, "price")

    if raw_side not in {"BUY", "SELL"}:
        raise RiskManagerError("signal.side must be BUY or SELL.")

    return _NormalizedSignal(
        signal_id=signal_id,
        timestamp=timestamp,
        side=raw_side,  # type: ignore[arg-type]
        price=price,
        underlying_symbol=_required_text_attr(signal, "underlying_symbol"),
        underlying_entry_price=_positive_float_attr(signal, "underlying_entry_price"),
        atr=_positive_float_attr(signal, "atr"),
        atr_pct=_positive_float_attr(signal, "atr_pct"),
        underlying_sl_price=_positive_float_attr(signal, "underlying_sl_price"),
        underlying_tp_price=_positive_float_attr(signal, "underlying_tp_price"),
        underlying_sl_pct=_positive_float_attr(signal, "underlying_sl_pct"),
        underlying_tp_pct=_positive_float_attr(signal, "underlying_tp_pct"),
    )


def _normalize_product(product: ExecutionProduct) -> ExecutionProduct:
    return ExecutionProduct(
        asset_class=_required_text_attr(product, "asset_class").upper(),
        con_id=_required_int_attr(product, "con_id"),
        local_symbol=_optional_text_attr(product, "local_symbol"),
        exchange=_required_text_attr(product, "exchange").upper(),
        currency=_required_text_attr(product, "currency").upper(),
        leverage=_positive_float_attr(product, "leverage"),
        issuer_fee_pct=_non_negative_float_attr(product, "issuer_fee_pct"),
        bid=_optional_positive_float_attr(product, "bid"),
        ask=_optional_positive_float_attr(product, "ask"),
        quote_time=_optional_text_attr(product, "quote_time"),
        spread_pct=_optional_non_negative_float_attr(product, "spread_pct"),
        commission_pct=_optional_non_negative_float_attr(product, "commission_pct"),
        estimated_total_cost_pct=_optional_non_negative_float_attr(product, "estimated_total_cost_pct"),
    )


def _validate_product_quote(product: ExecutionProduct, *, max_age_seconds: float) -> str | None:
    if product.ask is None:
        return "Selected execution product ask price is missing."

    if product.quote_time is None:
        return "Selected execution product quote timestamp is missing."

    try:
        quote_time = datetime.fromisoformat(product.quote_time)
    except ValueError:
        return "Selected execution product quote timestamp is invalid."

    if quote_time.tzinfo is None:
        quote_time = quote_time.replace(tzinfo=UTC)
    else:
        quote_time = quote_time.astimezone(UTC)

    age_seconds = (datetime.now(UTC) - quote_time).total_seconds()
    if age_seconds < 0:
        return "Selected execution product quote timestamp is in the future."
    if age_seconds > max_age_seconds:
        return f"Selected execution product quote is stale. age_seconds={age_seconds:.3f} max_age_seconds={max_age_seconds}."

    return None


def _execution_side_for_signal(side: SignalSide) -> ExecutionSide:
    return "long" if side == "BUY" else "short"


def _order_action_for_signal(side: SignalSide) -> OrderAction:
    return "BUY"


def _count_active_position_slots(account_snapshot: Any, product: ExecutionProduct) -> int:
    positions = getattr(account_snapshot, "positions", ())
    return sum(1 for position in positions if _is_active_position(position) and _matches_product(position, product))


def _count_active_order_slots(account_snapshot: Any, product: ExecutionProduct) -> int:
    open_orders = getattr(account_snapshot, "open_orders", ())
    return sum(1 for order in open_orders if _is_active_order(order) and _matches_product(order, product))


def _is_active_position(position: Any) -> bool:
    quantity = float(getattr(position, "position", 0.0) or 0.0)
    return quantity != 0.0


def _is_active_order(order: Any) -> bool:
    status = str(getattr(order, "status", "") or "")
    remaining = float(getattr(order, "remaining", 0.0) or 0.0)
    return status in ACTIVE_ORDER_STATUSES and remaining > 0.0


def _matches_product(source: Any, product: ExecutionProduct) -> bool:
    if product.con_id is not None:
        return _optional_int_attr(source, "con_id") == product.con_id

    if product.local_symbol:
        source_local_symbol = _optional_text_attr(source, "local_symbol")
        return source_local_symbol == product.local_symbol

    return True


def _blocked(reason: str) -> RiskDecision:
    return RiskDecision(status="blocked", reason=reason, trade_plan=None)


def _required_text_attr(source: Any, name: str) -> str:
    value = getattr(source, name, None)
    if not isinstance(value, str) or not value.strip():
        raise RiskManagerError(f"{name} is required.")
    return value.strip()


def _optional_text_attr(source: Any, name: str) -> str | None:
    value = getattr(source, name, None)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise RiskManagerError(f"{name} must be a non-empty string when provided.")
    return value.strip()


def _optional_int_attr(source: Any, name: str) -> int | None:
    value = getattr(source, name, None)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise RiskManagerError(f"{name} must be an integer when provided.")
    if value <= 0:
        raise RiskManagerError(f"{name} must be greater than zero when provided.")
    return value


def _required_int_attr(source: Any, name: str) -> int:
    value = _optional_int_attr(source, name)
    if value is None:
        raise RiskManagerError(f"{name} is required.")
    return value


def _positive_float_attr(source: Any, name: str) -> float:
    value = getattr(source, name, None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RiskManagerError(f"{name} must be a number.")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise RiskManagerError(f"{name} must be greater than zero.")
    return result


def _optional_positive_float_attr(source: Any, name: str) -> float | None:
    value = getattr(source, name, None)
    if value is None:
        return None
    return _positive_float_attr(source, name)


def _non_negative_float_attr(source: Any, name: str) -> float:
    value = getattr(source, name, None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RiskManagerError(f"{name} must be a number.")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise RiskManagerError(f"{name} must be zero or greater.")
    return result


def _optional_non_negative_float_attr(source: Any, name: str) -> float | None:
    value = getattr(source, name, None)
    if value is None:
        return None
    return _non_negative_float_attr(source, name)
