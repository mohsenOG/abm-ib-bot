"""Simple fixed-slot position sizing."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR, InvalidOperation
from typing import Any

from config.defaults import (
    DEFAULT_SIZING_ALLOW_FRACTIONAL,
    DEFAULT_SIZING_MIN_QUANTITY,
    DEFAULT_SIZING_QUANTITY_STEP,
)


class RiskSizingError(ValueError):
    """Raised when a trade quantity cannot be sized safely."""


@dataclass(frozen=True)
class QuantityRules:
    min_quantity: Decimal = DEFAULT_SIZING_MIN_QUANTITY
    quantity_step: Decimal = DEFAULT_SIZING_QUANTITY_STEP
    allow_fractional: bool = DEFAULT_SIZING_ALLOW_FRACTIONAL


def quantity_rules_from_settings(settings: Any) -> QuantityRules:
    """Build exact quantity rules from loaded sizing settings."""

    return QuantityRules(
        min_quantity=_positive_decimal(getattr(settings, "min_quantity", None), "sizing.min_quantity"),
        quantity_step=_positive_decimal(getattr(settings, "quantity_step", None), "sizing.quantity_step"),
        allow_fractional=_bool_setting(getattr(settings, "allow_fractional", None), "sizing.allow_fractional"),
    )


def calculate_capital_per_position(initial_capital: float, capital_slots: int) -> float:
    """Return fixed capital allocated to each risk slot."""

    capital = _positive_decimal(initial_capital, "initial_capital")
    slots = _positive_int(capital_slots, "capital_slots")
    return float(capital / Decimal(slots))


def calculate_quantity(
    capital_per_position: float,
    *,
    product_price: float | None = None,
    quantity_rules: QuantityRules | None = None,
) -> float:
    """Calculate a simple order quantity for a selected derivative product.

    A validated product price is required so sizing stays tied to the
    intended capital allocation.
    """

    rules = quantity_rules or QuantityRules()
    _validate_quantity_rules(rules)
    capital = _positive_decimal(capital_per_position, "capital_per_position")

    if product_price is None:
        raise RiskSizingError("product_price is required to calculate quantity.")

    price = _positive_decimal(product_price, "product_price")
    raw_quantity = capital / price
    stepped_quantity = _floor_to_step(raw_quantity, rules.quantity_step)

    if stepped_quantity < rules.min_quantity:
        raise RiskSizingError(
            "Calculated quantity is below the minimum quantity for the selected execution product."
        )

    return _quantity_result(stepped_quantity, rules)


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    steps = (value / step).to_integral_value(rounding=ROUND_FLOOR)
    return steps * step


def _quantity_result(value: Decimal, rules: QuantityRules) -> float:
    if not rules.allow_fractional and value != value.to_integral_value():
        raise RiskSizingError("Fractional quantity is not allowed by the configured quantity rules.")

    if value <= 0:
        raise RiskSizingError("Calculated quantity must be greater than zero.")

    return float(value)


def _validate_quantity_rules(rules: QuantityRules) -> None:
    if not isinstance(rules, QuantityRules):
        raise RiskSizingError("quantity_rules must be a QuantityRules instance.")

    min_quantity = _positive_decimal(rules.min_quantity, "quantity_rules.min_quantity")
    quantity_step = _positive_decimal(rules.quantity_step, "quantity_rules.quantity_step")

    if not rules.allow_fractional:
        if min_quantity != min_quantity.to_integral_value():
            raise RiskSizingError("quantity_rules.min_quantity must be a whole number.")
        if quantity_step != quantity_step.to_integral_value():
            raise RiskSizingError("quantity_rules.quantity_step must be a whole number.")


def _positive_int(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RiskSizingError(f"{field_name} must be an integer.")
    if value <= 0:
        raise RiskSizingError(f"{field_name} must be greater than zero.")
    return value


def _positive_decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise RiskSizingError(f"{field_name} must be a positive number.")

    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RiskSizingError(f"{field_name} must be a positive number.") from exc

    if result <= 0:
        raise RiskSizingError(f"{field_name} must be greater than zero.")

    return result


def _bool_setting(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise RiskSizingError(f"{field_name} must be true or false.")
    return value
