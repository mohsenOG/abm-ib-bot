"""Load and validate bot settings."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from data.timeframes import SUPPORTED_BAR_SIZE_1_HOUR
from domain.constants import EXECUTION_SIDE_LONG, EXECUTION_SIDE_SHORT, TRADING_MODES
from ib_gateway.constants import (
    EUR_CURRENCY,
    EXECUTION_SEC_TYPE_IOPT,
    SIGNAL_ASSET_CLASS_CMDTY,
    USD_CURRENCY,
)

MARKET_ENTRY_ORDER_TYPE = "market"
SUPPORTED_ENTRY_ORDER_TYPES = {MARKET_ENTRY_ORDER_TYPE}
IB_HISTORICAL_DURATION_UNITS = frozenset({"S", "D", "W", "M", "Y"})
EXACT_IB_HISTORICAL_DURATION_UNITS = frozenset({"S", "D", "W"})


class SettingsValidationError(ValueError):
    """Raised when configuration is missing or invalid."""


@dataclass(frozen=True)
class AllowedDirectionsSettings:
    long: bool
    short: bool


@dataclass(frozen=True)
class TradingSettings:
    mode: str
    allowed_directions: AllowedDirectionsSettings


@dataclass(frozen=True)
class MarketDataSettings:
    bar_size: str
    historical_duration: str
    what_to_show: str
    use_rth: bool
    gap_block_recent_bars: int
    gap_backfill_duration: str


@dataclass(frozen=True)
class IBSettings:
    host: str
    port: int
    client_id: int
    account_id: str | None


@dataclass(frozen=True)
class TelegramSettings:
    enabled: bool
    bot_token: str | None
    chat_ids: tuple[str, ...]
    max_retries: int
    retry_delay_seconds: float
    timeout_seconds: float
    require_critical_delivery: bool


@dataclass(frozen=True)
class InstrumentSettings:
    asset_class: str
    symbol: str
    exchange: str
    currency: str
    expiry: str | None


@dataclass(frozen=True)
class ExecutionSettings:
    products_file: Path
    entry_order_type: str
    entry_fill_timeout_seconds: float
    protective_submit_timeout_seconds: float
    status_poll_seconds: float
    quote_poll_seconds: float


@dataclass(frozen=True)
class ExecutionProductSettings:
    side: str
    sec_type: str
    con_id: int
    exchange: str
    currency: str
    leverage: float
    enabled: bool
    issuer_fee_pct: float


@dataclass(frozen=True)
class ExecutionProductsSettings:
    quote_max_age_seconds: float
    max_spread_pct: float
    max_order_value_eur: float
    long: tuple[ExecutionProductSettings, ...]
    short: tuple[ExecutionProductSettings, ...]


@dataclass(frozen=True)
class SizingSettings:
    min_quantity: Decimal
    quantity_step: Decimal
    allow_fractional: bool


@dataclass(frozen=True)
class RiskSettings:
    initial_capital: float
    capital_slots: int
    capital_per_position: float
    max_concurrent_position_slots: int


@dataclass(frozen=True)
class StrategySettings:
    bias_threshold: float
    use_heikin_ashi: bool
    atr_length: int
    sl_atr_mult: float
    tp_atr_mult: float


@dataclass(frozen=True)
class HealthSettings:
    market_data_max_age_seconds: float
    last_processed_candle_max_age_seconds: float
    repeated_error_limit: int


@dataclass(frozen=True)
class RuntimeSettings:
    candle_close_buffer_seconds: float
    bar_retry_seconds: float
    bar_retry_attempts: int
    clock_advisory_enabled: bool
    clock_advisory_warn_ms: int
    active_trade_monitor_seconds: int


@dataclass(frozen=True)
class LiveModeSettings:
    enabled: bool
    allow_telegram_failure: bool


@dataclass(frozen=True)
class LoggerSettings:
    file_path: Path
    level: str
    format: str
    date_format: str


@dataclass(frozen=True)
class PathSettings:
    state_file: Path
    trade_journal_file: Path


@dataclass(frozen=True)
class AppSettings:
    project_root: Path
    trading: TradingSettings
    market_data: MarketDataSettings
    ib: IBSettings
    telegram: TelegramSettings
    signal_instrument: InstrumentSettings
    instrument: InstrumentSettings
    execution: ExecutionSettings
    execution_products: ExecutionProductsSettings
    sizing: SizingSettings
    risk: RiskSettings
    strategy: StrategySettings
    health: HealthSettings
    runtime: RuntimeSettings
    live: LiveModeSettings
    logger: LoggerSettings
    paths: PathSettings


def load_settings(
    settings_file: str | Path | None = None,
    env_file: str | Path | None = None,
) -> AppSettings:
    """Load root settings.yml and required environment settings."""

    project_root = _find_project_root()
    settings_path = Path(settings_file) if settings_file is not None else project_root / "settings.yml"
    env_path = Path(env_file) if env_file is not None else project_root / ".env"

    if env_path.exists():
        from dotenv import load_dotenv

        load_dotenv(env_path)

    raw = _load_yaml(settings_path)
    signal_instrument = _load_signal_instrument(raw)
    execution = _load_execution(raw, project_root)

    return AppSettings(
        project_root=project_root,
        trading=_load_trading(raw),
        market_data=_load_market_data(raw),
        ib=_load_ib(raw),
        telegram=_load_telegram(raw),
        signal_instrument=signal_instrument,
        instrument=signal_instrument,
        execution=execution,
        execution_products=_load_execution_products(execution.products_file),
        sizing=_load_sizing(raw),
        risk=_load_risk(raw),
        strategy=_load_strategy(raw),
        health=_load_health(raw),
        runtime=_load_runtime(raw),
        live=_load_live(raw),
        logger=_load_logger(raw, project_root),
        paths=_load_paths(raw, project_root),
    )


def _find_project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_yaml(settings_path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ModuleNotFoundError as exc:
        raise SettingsValidationError(
            "PyYAML is required to load settings.yml. Install dependencies from requirements.txt."
        ) from exc

    if not settings_path.exists():
        raise SettingsValidationError(f"Settings file not found: {settings_path}")

    with settings_path.open("r", encoding="utf-8") as file_obj:
        loaded = yaml.safe_load(file_obj)

    if not isinstance(loaded, dict):
        raise SettingsValidationError("settings.yml must contain a YAML mapping at the root.")

    return loaded


def _load_json(json_path: Path) -> dict[str, Any]:
    if not json_path.exists():
        raise SettingsValidationError(f"Execution products file not found: {json_path}")

    with json_path.open("r", encoding="utf-8") as file_obj:
        loaded = json.load(file_obj)

    if not isinstance(loaded, dict):
        raise SettingsValidationError("execution_products.json must contain a JSON object at the root.")

    return loaded


def _load_trading(raw: dict[str, Any]) -> TradingSettings:
    section = _required_section(raw, "trading")
    mode = _required_string(section, "mode", "trading.mode")
    allowed_directions = _load_allowed_directions(section)

    if mode not in TRADING_MODES:
        allowed = ", ".join(sorted(TRADING_MODES))
        raise SettingsValidationError(f"trading.mode must be one of: {allowed}.")

    return TradingSettings(mode=mode, allowed_directions=allowed_directions)


def _load_market_data(raw: dict[str, Any]) -> MarketDataSettings:
    section = _required_section(raw, "market_data")
    bar_size = _required_string(section, "bar_size", "market_data.bar_size")
    historical_duration = _required_string(section, "historical_duration", "market_data.historical_duration")
    what_to_show = _required_string(section, "what_to_show", "market_data.what_to_show")
    use_rth = _required_bool(section, "use_rth", "market_data.use_rth")
    gap_block_recent_bars = _required_int(section, "gap_block_recent_bars", "market_data.gap_block_recent_bars")
    gap_backfill_duration = _required_string(section, "gap_backfill_duration", "market_data.gap_backfill_duration")

    if bar_size != SUPPORTED_BAR_SIZE_1_HOUR:
        raise SettingsValidationError(
            f"market_data.bar_size must be exactly '{SUPPORTED_BAR_SIZE_1_HOUR}' for this strategy."
        )

    if gap_block_recent_bars <= 0:
        raise SettingsValidationError("market_data.gap_block_recent_bars must be greater than zero.")
    _validate_ib_duration(
        historical_duration,
        "market_data.historical_duration",
        allowed_units=IB_HISTORICAL_DURATION_UNITS,
    )
    _validate_ib_duration(
        gap_backfill_duration,
        "market_data.gap_backfill_duration",
        allowed_units=EXACT_IB_HISTORICAL_DURATION_UNITS,
    )

    return MarketDataSettings(
        bar_size=bar_size,
        historical_duration=historical_duration,
        what_to_show=what_to_show,
        use_rth=use_rth,
        gap_block_recent_bars=gap_block_recent_bars,
        gap_backfill_duration=gap_backfill_duration,
    )


def _load_allowed_directions(section: dict[str, Any]) -> AllowedDirectionsSettings:
    directions = _required_section(section, "allowed_directions")
    long_enabled = _required_bool(directions, "long", "trading.allowed_directions.long")
    short_enabled = _required_bool(directions, "short", "trading.allowed_directions.short")

    if not long_enabled and not short_enabled:
        raise SettingsValidationError("At least one trading direction must be enabled.")

    return AllowedDirectionsSettings(long=long_enabled, short=short_enabled)


def _load_ib(raw: dict[str, Any]) -> IBSettings:
    section = _required_section(raw, "ib")
    host = _required_string(section, "host", "ib.host")
    port = _required_int(section, "port", "ib.port")
    client_id = _required_int(section, "client_id", "ib.client_id")
    account_id = _optional_env_string("IB_ACCOUNT_ID")

    if port < 1 or port > 65535:
        raise SettingsValidationError("ib.port must be between 1 and 65535.")

    if client_id < 0:
        raise SettingsValidationError("ib.client_id must be zero or greater.")

    return IBSettings(host=host, port=port, client_id=client_id, account_id=account_id)


def _load_telegram(raw: dict[str, Any]) -> TelegramSettings:
    section = _required_section(raw, "telegram")
    enabled = _required_bool(section, "enabled", "telegram.enabled")
    max_retries = _required_int(section, "max_retries", "telegram.max_retries")
    retry_delay_seconds = _required_float(
        section,
        "retry_delay_seconds",
        "telegram.retry_delay_seconds",
    )
    timeout_seconds = _required_float(
        section,
        "timeout_seconds",
        "telegram.timeout_seconds",
    )
    require_critical_delivery = _required_bool(
        section,
        "require_critical_delivery",
        "telegram.require_critical_delivery",
    )

    if max_retries < 0:
        raise SettingsValidationError("telegram.max_retries must be zero or greater.")

    if retry_delay_seconds < 0:
        raise SettingsValidationError("telegram.retry_delay_seconds must be zero or greater.")

    if timeout_seconds <= 0:
        raise SettingsValidationError("telegram.timeout_seconds must be greater than zero.")

    if not enabled:
        return TelegramSettings(
            enabled=False,
            bot_token=None,
            chat_ids=(),
            max_retries=max_retries,
            retry_delay_seconds=retry_delay_seconds,
            timeout_seconds=timeout_seconds,
            require_critical_delivery=require_critical_delivery,
        )

    bot_token = _required_env_string("TELEGRAM_BOT_TOKEN")
    chat_ids_raw = _required_env_string("TELEGRAM_CHAT_IDS")
    chat_ids = tuple(chat_id.strip() for chat_id in chat_ids_raw.split(",") if chat_id.strip())

    if not chat_ids:
        raise SettingsValidationError("TELEGRAM_CHAT_IDS must contain at least one chat ID.")

    return TelegramSettings(
        enabled=True,
        bot_token=bot_token,
        chat_ids=chat_ids,
        max_retries=max_retries,
        retry_delay_seconds=retry_delay_seconds,
        timeout_seconds=timeout_seconds,
        require_critical_delivery=require_critical_delivery,
    )


def _load_signal_instrument(raw: dict[str, Any]) -> InstrumentSettings:
    section = _required_section(raw, "signal_instrument")

    asset_class = _required_string(section, "asset_class", "signal_instrument.asset_class")
    symbol = _required_string(section, "symbol", "signal_instrument.symbol")
    exchange = _required_string(section, "exchange", "signal_instrument.exchange")
    currency = _required_string(section, "currency", "signal_instrument.currency")
    expiry = _required_nullable_string(section, "expiry", "signal_instrument.expiry")

    asset_class = asset_class.upper()
    currency = currency.upper()
    exchange = exchange.upper()

    if currency != USD_CURRENCY:
        raise SettingsValidationError(f"signal_instrument.currency must be {USD_CURRENCY} for this gold bot.")

    if asset_class != SIGNAL_ASSET_CLASS_CMDTY:
        raise SettingsValidationError(
            f"signal_instrument.asset_class must be {SIGNAL_ASSET_CLASS_CMDTY} for XAUUSD signals."
        )

    return InstrumentSettings(
        asset_class=asset_class,
        symbol=symbol,
        exchange=exchange,
        currency=currency,
        expiry=expiry,
    )


def _load_execution(raw: dict[str, Any], project_root: Path) -> ExecutionSettings:
    section = _required_section(raw, "execution")
    products_file = _resolve_project_path(
        project_root,
        _required_string(section, "products_file", "execution.products_file"),
    )
    entry_order_type = _required_string(section, "entry_order_type", "execution.entry_order_type").lower()
    entry_fill_timeout_seconds = _required_float(
        section,
        "entry_fill_timeout_seconds",
        "execution.entry_fill_timeout_seconds",
    )
    protective_submit_timeout_seconds = _required_float(
        section,
        "protective_submit_timeout_seconds",
        "execution.protective_submit_timeout_seconds",
    )
    status_poll_seconds = _required_float(section, "status_poll_seconds", "execution.status_poll_seconds")
    quote_poll_seconds = _required_float(section, "quote_poll_seconds", "execution.quote_poll_seconds")

    if entry_order_type not in SUPPORTED_ENTRY_ORDER_TYPES:
        allowed = ", ".join(sorted(SUPPORTED_ENTRY_ORDER_TYPES))
        raise SettingsValidationError(
            f"execution.entry_order_type must be one of: {allowed}."
        )

    if entry_fill_timeout_seconds <= 0:
        raise SettingsValidationError("execution.entry_fill_timeout_seconds must be greater than zero.")
    if protective_submit_timeout_seconds <= 0:
        raise SettingsValidationError("execution.protective_submit_timeout_seconds must be greater than zero.")
    if status_poll_seconds <= 0:
        raise SettingsValidationError("execution.status_poll_seconds must be greater than zero.")
    if quote_poll_seconds <= 0:
        raise SettingsValidationError("execution.quote_poll_seconds must be greater than zero.")

    # Relative execution product paths are resolved against the project root,
    # matching the rest of the app's path settings and the repository layout.
    return ExecutionSettings(
        products_file=products_file,
        entry_order_type=entry_order_type,
        entry_fill_timeout_seconds=entry_fill_timeout_seconds,
        protective_submit_timeout_seconds=protective_submit_timeout_seconds,
        status_poll_seconds=status_poll_seconds,
        quote_poll_seconds=quote_poll_seconds,
    )


def _load_execution_products(json_path: Path) -> ExecutionProductsSettings:
    raw = _load_json(json_path)
    section = _required_section(raw, "execution_products")
    quote_max_age_seconds = _required_float(
        section,
        "quote_max_age_seconds",
        "execution_products.quote_max_age_seconds",
    )
    max_spread_pct = _required_float(section, "max_spread_pct", "execution_products.max_spread_pct")
    max_order_value_eur = _required_float(
        section,
        "max_order_value_eur",
        "execution_products.max_order_value_eur",
    )

    if quote_max_age_seconds <= 0:
        raise SettingsValidationError("execution_products.quote_max_age_seconds must be greater than zero.")
    if max_spread_pct <= 0:
        raise SettingsValidationError("execution_products.max_spread_pct must be greater than zero.")
    if max_order_value_eur <= 0:
        raise SettingsValidationError("execution_products.max_order_value_eur must be greater than zero.")

    return ExecutionProductsSettings(
        quote_max_age_seconds=quote_max_age_seconds,
        max_spread_pct=max_spread_pct,
        max_order_value_eur=max_order_value_eur,
        long=_load_execution_product_side(section, EXECUTION_SIDE_LONG),
        short=_load_execution_product_side(section, EXECUTION_SIDE_SHORT),
    )


def _load_execution_product_side(
    section: dict[str, Any],
    side: str,
) -> tuple[ExecutionProductSettings, ...]:
    products = section.get(side)
    if not isinstance(products, list):
        raise SettingsValidationError(f"execution_products.{side} must be a list.")

    return tuple(_load_execution_product(product, side, index) for index, product in enumerate(products))


def _load_execution_product(
    product: Any,
    side: str,
    index: int,
) -> ExecutionProductSettings:
    if not isinstance(product, dict):
        raise SettingsValidationError(f"execution_products.{side}[{index}] must be an object.")

    prefix = f"execution_products.{side}[{index}]"
    sec_type = _required_string(product, "secType", f"{prefix}.secType").upper()
    con_id = _required_int(product, "conId", f"{prefix}.conId")
    exchange = _required_string(product, "exchange", f"{prefix}.exchange").upper()
    currency = _required_string(product, "currency", f"{prefix}.currency").upper()
    leverage = _required_float(product, "leverage", f"{prefix}.leverage")
    enabled = _required_bool(product, "enabled", f"{prefix}.enabled")
    issuer_fee_pct = _required_float(product, "issuer_fee_pct", f"{prefix}.issuer_fee_pct")

    if sec_type != EXECUTION_SEC_TYPE_IOPT:
        raise SettingsValidationError(f"{prefix}.secType must be {EXECUTION_SEC_TYPE_IOPT}.")
    if con_id <= 0:
        raise SettingsValidationError(f"{prefix}.conId must be greater than zero.")
    if currency != EUR_CURRENCY:
        raise SettingsValidationError(f"{prefix}.currency must be {EUR_CURRENCY}.")
    if leverage <= 0:
        raise SettingsValidationError(f"{prefix}.leverage must be greater than zero.")
    if issuer_fee_pct < 0:
        raise SettingsValidationError(f"{prefix}.issuer_fee_pct must be zero or greater.")

    return ExecutionProductSettings(
        side=side,
        sec_type=sec_type,
        con_id=con_id,
        exchange=exchange,
        currency=currency,
        leverage=leverage,
        enabled=enabled,
        issuer_fee_pct=issuer_fee_pct,
    )


def _load_sizing(raw: dict[str, Any]) -> SizingSettings:
    section = _required_section(raw, "sizing")
    min_quantity = _required_decimal(section, "min_quantity", "sizing.min_quantity")
    quantity_step = _required_decimal(section, "quantity_step", "sizing.quantity_step")
    allow_fractional = _required_bool(section, "allow_fractional", "sizing.allow_fractional")

    if min_quantity <= 0:
        raise SettingsValidationError("sizing.min_quantity must be greater than zero.")
    if quantity_step <= 0:
        raise SettingsValidationError("sizing.quantity_step must be greater than zero.")

    return SizingSettings(
        min_quantity=min_quantity,
        quantity_step=quantity_step,
        allow_fractional=allow_fractional,
    )


def _load_risk(raw: dict[str, Any]) -> RiskSettings:
    section = _required_section(raw, "risk")
    initial_capital = _required_float(section, "initial_capital", "risk.initial_capital")
    capital_slots = _required_int(section, "capital_slots", "risk.capital_slots")
    max_concurrent_position_slots = _required_int(
        section,
        "max_concurrent_position_slots",
        "risk.max_concurrent_position_slots",
    )

    if initial_capital <= 0:
        raise SettingsValidationError("risk.initial_capital must be greater than zero.")

    if capital_slots <= 0:
        raise SettingsValidationError("risk.capital_slots must be greater than zero.")

    if max_concurrent_position_slots <= 0:
        raise SettingsValidationError("risk.max_concurrent_position_slots must be greater than zero.")

    if max_concurrent_position_slots > capital_slots:
        raise SettingsValidationError(
            "risk.max_concurrent_position_slots cannot be greater than risk.capital_slots."
        )

    return RiskSettings(
        initial_capital=initial_capital,
        capital_slots=capital_slots,
        capital_per_position=initial_capital / capital_slots,
        max_concurrent_position_slots=max_concurrent_position_slots,
    )


def _load_strategy(raw: dict[str, Any]) -> StrategySettings:
    section = _required_section(raw, "strategy")
    bias_threshold = _required_float(section, "bias_threshold", "strategy.bias_threshold")
    use_heikin_ashi = _required_bool(section, "use_heikin_ashi", "strategy.use_heikin_ashi")
    atr_length = _required_int(section, "atr_length", "strategy.atr_length")
    sl_atr_mult = _required_float(section, "sl_atr_mult", "strategy.sl_atr_mult")
    tp_atr_mult = _required_float(section, "tp_atr_mult", "strategy.tp_atr_mult")

    if bias_threshold <= 0 or bias_threshold >= 1:
        raise SettingsValidationError("strategy.bias_threshold must be greater than 0 and lower than 1.")

    if atr_length <= 0:
        raise SettingsValidationError("strategy.atr_length must be greater than zero.")

    if sl_atr_mult <= 0:
        raise SettingsValidationError("strategy.sl_atr_mult must be greater than zero.")

    if tp_atr_mult <= 0:
        raise SettingsValidationError("strategy.tp_atr_mult must be greater than zero.")

    return StrategySettings(
        bias_threshold=bias_threshold,
        use_heikin_ashi=use_heikin_ashi,
        atr_length=atr_length,
        sl_atr_mult=sl_atr_mult,
        tp_atr_mult=tp_atr_mult,
    )


def _load_health(raw: dict[str, Any]) -> HealthSettings:
    section = _required_section(raw, "health")
    market_data_max_age_seconds = _required_float(
        section,
        "market_data_max_age_seconds",
        "health.market_data_max_age_seconds",
    )
    last_processed_candle_max_age_seconds = _required_float(
        section,
        "last_processed_candle_max_age_seconds",
        "health.last_processed_candle_max_age_seconds",
    )
    repeated_error_limit = _required_int(section, "repeated_error_limit", "health.repeated_error_limit")

    if market_data_max_age_seconds <= 0:
        raise SettingsValidationError("health.market_data_max_age_seconds must be greater than zero.")
    if last_processed_candle_max_age_seconds <= 0:
        raise SettingsValidationError("health.last_processed_candle_max_age_seconds must be greater than zero.")
    if repeated_error_limit <= 0:
        raise SettingsValidationError("health.repeated_error_limit must be greater than zero.")

    return HealthSettings(
        market_data_max_age_seconds=market_data_max_age_seconds,
        last_processed_candle_max_age_seconds=last_processed_candle_max_age_seconds,
        repeated_error_limit=repeated_error_limit,
    )


def _load_runtime(raw: dict[str, Any]) -> RuntimeSettings:
    section = _required_section(raw, "runtime")
    candle_close_buffer_seconds = _required_float(
        section,
        "candle_close_buffer_seconds",
        "runtime.candle_close_buffer_seconds",
    )
    bar_retry_seconds = _required_float(section, "bar_retry_seconds", "runtime.bar_retry_seconds")
    bar_retry_attempts = _required_int(section, "bar_retry_attempts", "runtime.bar_retry_attempts")
    clock_advisory_enabled = _required_bool(
        section,
        "clock_advisory_enabled",
        "runtime.clock_advisory_enabled",
    )
    clock_advisory_warn_ms = _required_int(
        section,
        "clock_advisory_warn_ms",
        "runtime.clock_advisory_warn_ms",
    )
    active_trade_monitor_seconds = _required_int(
        section,
        "active_trade_monitor_seconds",
        "runtime.active_trade_monitor_seconds",
    )

    if candle_close_buffer_seconds < 0:
        raise SettingsValidationError("runtime.candle_close_buffer_seconds must be zero or greater.")
    if bar_retry_seconds <= 0:
        raise SettingsValidationError("runtime.bar_retry_seconds must be greater than zero.")
    if bar_retry_attempts <= 0:
        raise SettingsValidationError("runtime.bar_retry_attempts must be greater than zero.")
    if clock_advisory_warn_ms < 0:
        raise SettingsValidationError("runtime.clock_advisory_warn_ms must be zero or greater.")
    if active_trade_monitor_seconds < 5 or active_trade_monitor_seconds > 15:
        raise SettingsValidationError("runtime.active_trade_monitor_seconds must be between 5 and 15.")

    return RuntimeSettings(
        candle_close_buffer_seconds=candle_close_buffer_seconds,
        bar_retry_seconds=bar_retry_seconds,
        bar_retry_attempts=bar_retry_attempts,
        clock_advisory_enabled=clock_advisory_enabled,
        clock_advisory_warn_ms=clock_advisory_warn_ms,
        active_trade_monitor_seconds=active_trade_monitor_seconds,
    )


def _load_live(raw: dict[str, Any]) -> LiveModeSettings:
    section = _required_section(raw, "live")
    enabled = _required_bool(section, "enabled", "live.enabled")
    allow_telegram_failure = _required_bool(section, "allow_telegram_failure", "live.allow_telegram_failure")

    return LiveModeSettings(
        enabled=enabled,
        allow_telegram_failure=allow_telegram_failure,
    )


def _load_logger(raw: dict[str, Any], project_root: Path) -> LoggerSettings:
    section = _required_section(raw, "logger")
    file_path = _resolve_project_path(project_root, _required_string(section, "file_path", "logger.file_path"))
    level = _required_string(section, "level", "logger.level").upper()
    log_format = _required_string(section, "format", "logger.format")
    date_format = _required_string(section, "date_format", "logger.date_format")

    valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    if level not in valid_levels:
        allowed = ", ".join(sorted(valid_levels))
        raise SettingsValidationError(f"logger.level must be one of: {allowed}.")

    return LoggerSettings(
        file_path=file_path,
        level=level,
        format=log_format,
        date_format=date_format,
    )


def _load_paths(raw: dict[str, Any], project_root: Path) -> PathSettings:
    section = _required_section(raw, "paths")
    state_file = _resolve_project_path(project_root, _required_string(section, "state_file", "paths.state_file"))
    trade_journal_file = _resolve_project_path(
        project_root,
        _required_string(section, "trade_journal_file", "paths.trade_journal_file"),
    )

    return PathSettings(
        state_file=state_file,
        trade_journal_file=trade_journal_file,
    )


def _required_section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise SettingsValidationError(f"Missing or invalid settings section: {key}.")
    return value


def _required_string(section: dict[str, Any], key: str, field_name: str) -> str:
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SettingsValidationError(f"{field_name} is required.")
    return value.strip()


def _validate_ib_duration(value: str, field_name: str, *, allowed_units: frozenset[str]) -> None:
    parts = value.strip().split()
    if len(parts) != 2:
        raise SettingsValidationError(f"{field_name} must use '<positive integer> <unit>', for example '1 D'.")

    quantity_text, unit_text = parts
    try:
        quantity = int(quantity_text)
    except ValueError as exc:
        raise SettingsValidationError(f"{field_name} quantity must be a positive integer.") from exc

    if quantity <= 0:
        raise SettingsValidationError(f"{field_name} quantity must be a positive integer.")

    unit = unit_text.upper()
    if unit not in allowed_units:
        allowed = ", ".join(sorted(allowed_units))
        raise SettingsValidationError(f"{field_name} unit must be one of: {allowed}.")


def _required_nullable_string(section: dict[str, Any], key: str, field_name: str) -> str | None:
    if key not in section:
        raise SettingsValidationError(f"{field_name} is required.")

    value = section[key]
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SettingsValidationError(f"{field_name} must be null or a non-empty string.")
    return value.strip()


def _required_int(section: dict[str, Any], key: str, field_name: str) -> int:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingsValidationError(f"{field_name} must be an integer.")
    return value


def _required_float(section: dict[str, Any], key: str, field_name: str) -> float:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsValidationError(f"{field_name} must be a number.")
    return float(value)


def _required_decimal(section: dict[str, Any], key: str, field_name: str) -> Decimal:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise SettingsValidationError(f"{field_name} must be a number.")

    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SettingsValidationError(f"{field_name} must be a number.") from exc

    return result


def _required_bool(section: dict[str, Any], key: str, field_name: str) -> bool:
    value = section.get(key)
    if not isinstance(value, bool):
        raise SettingsValidationError(f"{field_name} must be true or false.")
    return value


def _required_env_string(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise SettingsValidationError(f"Environment variable {name} is required.")
    return value.strip()


def _optional_env_string(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def _resolve_project_path(project_root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return project_root / path
