"""Select the cheapest safe execution product from the curated universe."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from ib_async import MarketOrder

from config.settings import AppSettings, ExecutionProductSettings
from ib_gateway.contracts import build_execution_product_contract, qualify_contract
from logging_setup.logger import get_logger
from risk.risk_manager import ExecutionProduct
from risk.sizing import QuantityRules, RiskSizingError, calculate_quantity


SignalSide = Literal["BUY", "SELL"]
ExecutionSide = Literal["long", "short"]
ORDER_ACTION = "BUY"
UNSET_PRICE = 1e100
LIVE_MARKET_DATA_TYPE = 1
NON_LIVE_MARKET_DATA_TYPES = {2, 3, 4}


class ProductSelectionError(RuntimeError):
    """Raised when no curated execution product is safe to trade."""


@dataclass(frozen=True)
class ProductQuote:
    bid: float
    ask: float
    quote_time: datetime
    spread_pct: float


@dataclass(frozen=True)
class ProductCandidateEvaluation:
    product: ExecutionProductSettings
    accepted: bool
    reason: str | None = None
    selected_product: ExecutionProduct | None = None


class ProductSelector:
    """Qualify, quote-check, cost-check, and select a curated product."""

    def __init__(
        self,
        settings: AppSettings,
        ib_client: Any,
        *,
        quantity_rules: QuantityRules | None = None,
    ) -> None:
        self._settings = settings
        self._ib_client = ib_client
        self._quantity_rules = quantity_rules or QuantityRules()
        self._logger = get_logger("execution.product_selector")

    async def select_for_signal(self, side: SignalSide) -> tuple[ExecutionProduct, Any]:
        """Return the selected execution product and its qualified IB contract."""

        execution_side = _execution_side_for_signal(side)
        products = getattr(self._settings.execution_products, execution_side)
        enabled_products = tuple(product for product in products if product.enabled)
        if not enabled_products:
            raise ProductSelectionError(f"No enabled {execution_side} execution products are configured.")

        evaluations: list[ProductCandidateEvaluation] = []
        selected_contracts: dict[int, Any] = {}

        for product in enabled_products:
            evaluation, qualified_contract = await self._evaluate_product(product)
            evaluations.append(evaluation)
            if evaluation.selected_product is not None:
                selected_contracts[evaluation.selected_product.con_id] = qualified_contract

        valid_products = [
            evaluation.selected_product
            for evaluation in evaluations
            if evaluation.accepted and evaluation.selected_product is not None
        ]
        if not valid_products:
            reasons = "; ".join(
                f"con_id={evaluation.product.con_id}: {evaluation.reason or 'rejected'}"
                for evaluation in evaluations
            )
            raise ProductSelectionError(f"No valid {execution_side} execution products: {reasons}.")

        selected_product = min(valid_products, key=_selection_cost)
        qualified_contract = selected_contracts[selected_product.con_id]
        self._logger.info(
            "Selected execution product. side=%s con_id=%s spread_pct=%s issuer_fee_pct=%s commission_pct=%s total_cost_pct=%s",
            execution_side,
            selected_product.con_id,
            selected_product.spread_pct,
            selected_product.issuer_fee_pct,
            selected_product.commission_pct,
            selected_product.estimated_total_cost_pct,
        )
        return selected_product, qualified_contract

    async def _evaluate_product(
        self,
        product: ExecutionProductSettings,
    ) -> tuple[ProductCandidateEvaluation, Any | None]:
        try:
            qualified_contract = await qualify_contract(
                self._connected_ib(),
                build_execution_product_contract(product),
            )
            _validate_qualified_contract(product, qualified_contract)
            quote = await self._live_quote(qualified_contract)
            _validate_spread(product, quote, max_spread_pct=self._settings.execution_products.max_spread_pct)
            quantity = calculate_quantity(
                self._settings.risk.capital_per_position,
                product_price=quote.ask,
                quantity_rules=self._quantity_rules,
            )
            order_value = quantity * quote.ask
            _validate_order_value(order_value, max_order_value=self._settings.execution_products.max_order_value)
            commission_pct = await self._commission_pct(
                qualified_contract,
                quantity=quantity,
                order_value=order_value,
                currency=product.currency,
            )
            total_cost_pct = quote.spread_pct + product.issuer_fee_pct + commission_pct
        except (ProductSelectionError, RiskSizingError) as exc:
            self._logger.info("Rejected execution product. con_id=%s reason=%s", product.con_id, exc)
            return ProductCandidateEvaluation(product=product, accepted=False, reason=str(exc)), None
        except Exception as exc:
            reason = f"Product evaluation failed: {exc}"
            self._logger.info("Rejected execution product. con_id=%s reason=%s", product.con_id, reason)
            return ProductCandidateEvaluation(product=product, accepted=False, reason=reason), None

        selected_product = ExecutionProduct(
            asset_class=product.sec_type,
            con_id=product.con_id,
            local_symbol=_optional_contract_text(qualified_contract, "localSymbol"),
            exchange=product.exchange,
            currency=product.currency,
            leverage=product.leverage,
            issuer_fee_pct=product.issuer_fee_pct,
            bid=quote.bid,
            ask=quote.ask,
            quote_time=quote.quote_time.isoformat(),
            spread_pct=quote.spread_pct,
            commission_pct=commission_pct,
            estimated_total_cost_pct=total_cost_pct,
        )
        return (
            ProductCandidateEvaluation(product=product, accepted=True, selected_product=selected_product),
            qualified_contract,
        )

    async def _live_quote(self, contract: Any) -> ProductQuote:
        ib = self._connected_ib()
        request_market_data_type = getattr(ib, "reqMarketDataType", None)
        if callable(request_market_data_type):
            request_market_data_type(LIVE_MARKET_DATA_TYPE)

        try:
            ticker = ib.reqMktData(contract, "", False, False)
            quote = await self._wait_for_quote(ticker)
        finally:
            cancel_market_data = getattr(ib, "cancelMktData", None)
            if callable(cancel_market_data):
                cancel_market_data(contract)

        return quote

    async def _wait_for_quote(self, ticker: Any) -> ProductQuote:
        max_age_seconds = self._settings.execution_products.quote_max_age_seconds
        deadline = asyncio.get_running_loop().time() + max_age_seconds
        last_error = "No quote received."

        while asyncio.get_running_loop().time() <= deadline:
            try:
                return _quote_from_ticker(ticker, max_age_seconds=max_age_seconds)
            except ProductSelectionError as exc:
                last_error = str(exc)
                await asyncio.sleep(0.25)

        raise ProductSelectionError(last_error)

    async def _commission_pct(
        self,
        contract: Any,
        *,
        quantity: float,
        order_value: float,
        currency: str,
    ) -> float:
        ib = self._connected_ib()
        order = MarketOrder(ORDER_ACTION, quantity)
        account_id = getattr(self._settings.ib, "account_id", None)
        if account_id is not None:
            order.account = account_id

        try:
            order_state = await ib.whatIfOrderAsync(contract, order)
        except Exception as exc:
            raise ProductSelectionError("IB what-if commission estimate failed.") from exc

        commission = _usable_money_attr(order_state, "commission")
        commission_currency = _optional_text_attr(order_state, "commissionCurrency")
        if commission_currency is not None and commission_currency.upper() != currency:
            raise ProductSelectionError(
                f"IB commission currency mismatch. expected={currency} observed={commission_currency}."
            )

        return commission / order_value * 100

    def _connected_ib(self) -> Any:
        ib = _resolve_ib_client(self._ib_client)
        is_connected = getattr(ib, "isConnected", None)
        if callable(is_connected) and not is_connected():
            raise ProductSelectionError("Interactive Brokers is disconnected.")
        return ib


def _execution_side_for_signal(side: SignalSide) -> ExecutionSide:
    if side == "BUY":
        return "long"
    if side == "SELL":
        return "short"
    raise ProductSelectionError("Signal side must be BUY or SELL.")


def _validate_qualified_contract(product: ExecutionProductSettings, contract: Any) -> None:
    con_id = _optional_int_attr(contract, "conId")
    if con_id != product.con_id:
        raise ProductSelectionError(f"Qualified contract con_id mismatch. expected={product.con_id} observed={con_id}.")

    sec_type = _required_contract_text(contract, "secType").upper()
    if sec_type != product.sec_type:
        raise ProductSelectionError(
            f"Qualified contract secType mismatch. expected={product.sec_type} observed={sec_type}."
        )

    currency = _required_contract_text(contract, "currency").upper()
    if currency != product.currency:
        raise ProductSelectionError(
            f"Qualified contract currency mismatch. expected={product.currency} observed={currency}."
        )

    observed_exchanges = {
        exchange
        for exchange in (
            _optional_contract_text(contract, "exchange"),
            _optional_contract_text(contract, "primaryExchange"),
        )
        if exchange is not None
    }
    observed_exchanges = {exchange.upper() for exchange in observed_exchanges}
    if product.exchange not in observed_exchanges:
        observed = ", ".join(sorted(observed_exchanges)) or "<none>"
        raise ProductSelectionError(
            f"Qualified contract exchange mismatch. expected={product.exchange} observed={observed}."
        )


def _quote_from_ticker(ticker: Any, *, max_age_seconds: float) -> ProductQuote:
    market_data_type = _optional_int_attr(ticker, "marketDataType")
    if market_data_type in NON_LIVE_MARKET_DATA_TYPES:
        raise ProductSelectionError(f"Market data is not live. market_data_type={market_data_type}.")

    bid = _usable_price_attr(ticker, "bid")
    ask = _usable_price_attr(ticker, "ask")
    if ask < bid:
        raise ProductSelectionError(f"Quote is crossed. bid={bid} ask={ask}.")

    quote_time = _required_quote_time(ticker)
    age_seconds = (datetime.now(UTC) - quote_time).total_seconds()
    if age_seconds < 0:
        raise ProductSelectionError("Quote timestamp is in the future.")
    if age_seconds > max_age_seconds:
        raise ProductSelectionError(
            f"Quote is stale. age_seconds={age_seconds:.3f} max_age_seconds={max_age_seconds}."
        )

    midpoint = (bid + ask) / 2
    spread_pct = (ask - bid) / midpoint * 100
    return ProductQuote(bid=bid, ask=ask, quote_time=quote_time, spread_pct=spread_pct)


def _validate_spread(product: ExecutionProductSettings, quote: ProductQuote, *, max_spread_pct: float) -> None:
    if quote.spread_pct > max_spread_pct:
        raise ProductSelectionError(
            f"Spread is too wide for con_id={product.con_id}. spread_pct={quote.spread_pct:.6f} "
            f"max_spread_pct={max_spread_pct}."
        )


def _validate_order_value(order_value: float, *, max_order_value: float) -> None:
    if order_value > max_order_value:
        raise ProductSelectionError(
            f"Estimated order value exceeds configured max_order_value. value={order_value:.6f} "
            f"max_order_value={max_order_value}."
        )


def _selection_cost(product: ExecutionProduct) -> float:
    cost = product.estimated_total_cost_pct
    if cost is None:
        raise ProductSelectionError("Accepted product is missing estimated_total_cost_pct.")
    return cost


def _resolve_ib_client(ib_client: Any) -> Any:
    required_methods = ("reqMktData", "whatIfOrderAsync", "isConnected")
    if all(callable(getattr(ib_client, name, None)) for name in required_methods):
        return ib_client

    candidate = getattr(ib_client, "ib", None)
    if all(callable(getattr(candidate, name, None)) for name in required_methods):
        return candidate

    raise ProductSelectionError("ProductSelector requires an Interactive Brokers client.")


def _required_quote_time(ticker: Any) -> datetime:
    value = getattr(ticker, "time", None)
    if value is None:
        value = getattr(ticker, "timestamp", None)
    if value is None:
        raise ProductSelectionError("Quote timestamp is missing.")

    timestamp = value if isinstance(value, datetime) else None
    if timestamp is None:
        try:
            timestamp = datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise ProductSelectionError("Quote timestamp is invalid.") from exc

    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


def _usable_price_attr(source: Any, name: str) -> float:
    value = getattr(source, name, None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProductSelectionError(f"Quote {name} is missing.")
    result = float(value)
    if not math.isfinite(result) or result <= 0 or result >= UNSET_PRICE:
        raise ProductSelectionError(f"Quote {name} is invalid.")
    return result


def _usable_money_attr(source: Any, name: str) -> float:
    value = getattr(source, name, None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProductSelectionError(f"IB {name} is missing.")
    result = float(value)
    if not math.isfinite(result) or result < 0 or result >= UNSET_PRICE:
        raise ProductSelectionError(f"IB {name} is invalid.")
    return result


def _required_contract_text(source: Any, name: str) -> str:
    value = _optional_contract_text(source, name)
    if value is None:
        raise ProductSelectionError(f"Qualified contract {name} is missing.")
    return value


def _optional_contract_text(source: Any, name: str) -> str | None:
    value = getattr(source, name, None)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_text_attr(source: Any, name: str) -> str | None:
    value = getattr(source, name, None)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int_attr(source: Any, name: str) -> int | None:
    value = getattr(source, name, None)
    if value is None:
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None
