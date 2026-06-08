"""Load and validate bot settings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ALLOWED_TRADING_MODES = {"alert_only", "paper", "live"}
REQUIRED_TIMEFRAME = "1 hour"


class SettingsValidationError(ValueError):
    """Raised when configuration is missing or invalid."""


@dataclass(frozen=True)
class TradingSettings:
    mode: str
    timeframe: str


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


@dataclass(frozen=True)
class InstrumentSettings:
    asset_class: str
    symbol: str
    exchange: str
    currency: str
    expiry: str | None


@dataclass(frozen=True)
class ExecutionInstrumentSettings:
    asset_class: str
    con_id: int | None
    local_symbol: str | None
    exchange: str | None
    currency: str


@dataclass(frozen=True)
class ExecutionInstrumentsSettings:
    long: ExecutionInstrumentSettings
    short: ExecutionInstrumentSettings


@dataclass(frozen=True)
class RiskSettings:
    initial_capital: float
    capital_slots: int
    capital_per_position: float
    max_concurrent_position_slots: int


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
    ib: IBSettings
    telegram: TelegramSettings
    signal_instrument: InstrumentSettings
    instrument: InstrumentSettings
    execution_instruments: ExecutionInstrumentsSettings
    risk: RiskSettings
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

    return AppSettings(
        project_root=project_root,
        trading=_load_trading(raw),
        ib=_load_ib(raw),
        telegram=_load_telegram(raw),
        signal_instrument=signal_instrument,
        instrument=signal_instrument,
        execution_instruments=_load_execution_instruments(raw),
        risk=_load_risk(raw),
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


def _load_trading(raw: dict[str, Any]) -> TradingSettings:
    section = _required_section(raw, "trading")
    mode = _required_string(section, "mode", "trading.mode")
    timeframe = _required_string(section, "timeframe", "trading.timeframe")

    if mode not in ALLOWED_TRADING_MODES:
        allowed = ", ".join(sorted(ALLOWED_TRADING_MODES))
        raise SettingsValidationError(f"trading.mode must be one of: {allowed}.")

    if timeframe != REQUIRED_TIMEFRAME:
        raise SettingsValidationError(f"trading.timeframe must be exactly '{REQUIRED_TIMEFRAME}'.")

    return TradingSettings(mode=mode, timeframe=timeframe)


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
    section = _optional_section(raw, "telegram")
    enabled = _optional_bool(section, "enabled", "telegram.enabled", True)
    max_retries = _optional_int(section, "max_retries", "telegram.max_retries", 3)
    retry_delay_seconds = _optional_float(
        section,
        "retry_delay_seconds",
        "telegram.retry_delay_seconds",
        2.0,
    )
    timeout_seconds = _optional_float(section, "timeout_seconds", "telegram.timeout_seconds", 10.0)

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
    )


def _load_signal_instrument(raw: dict[str, Any]) -> InstrumentSettings:
    section = _optional_section(raw, "signal_instrument")
    if not section:
        section = _required_section(raw, "instrument")

    asset_class = _required_string(section, "asset_class", "instrument.asset_class")
    symbol = _required_string(section, "symbol", "instrument.symbol")
    exchange = _required_string(section, "exchange", "instrument.exchange")
    currency = _required_string(section, "currency", "instrument.currency")
    expiry = _optional_string(section, "expiry", "instrument.expiry")

    asset_class = asset_class.upper()
    currency = currency.upper()
    exchange = exchange.upper()

    if currency != "USD":
        raise SettingsValidationError("signal_instrument.currency must be USD for this gold bot.")

    if asset_class != "CMDTY":
        raise SettingsValidationError("signal_instrument.asset_class must be CMDTY for XAUUSD signals.")

    return InstrumentSettings(
        asset_class=asset_class,
        symbol=symbol,
        exchange=exchange,
        currency=currency,
        expiry=expiry,
    )


def _load_execution_instruments(raw: dict[str, Any]) -> ExecutionInstrumentsSettings:
    section = _optional_section(raw, "execution_instruments")
    if not section:
        return ExecutionInstrumentsSettings(
            long=_empty_execution_instrument("long"),
            short=_empty_execution_instrument("short"),
        )

    return ExecutionInstrumentsSettings(
        long=_load_execution_instrument(section, "long"),
        short=_load_execution_instrument(section, "short"),
    )


def _load_execution_instrument(
    section: dict[str, Any],
    side: str,
) -> ExecutionInstrumentSettings:
    instrument_section = _optional_section(section, side)
    if not instrument_section:
        return _empty_execution_instrument(side)

    asset_class = _required_string(
        instrument_section,
        "asset_class",
        f"execution_instruments.{side}.asset_class",
    ).upper()
    con_id = _optional_int_or_none(
        instrument_section,
        "con_id",
        f"execution_instruments.{side}.con_id",
    )
    local_symbol = _optional_string(
        instrument_section,
        "local_symbol",
        f"execution_instruments.{side}.local_symbol",
    )
    exchange = _optional_string(
        instrument_section,
        "exchange",
        f"execution_instruments.{side}.exchange",
    )
    currency = _required_string(
        instrument_section,
        "currency",
        f"execution_instruments.{side}.currency",
    ).upper()

    if asset_class != "IOPT":
        raise SettingsValidationError(f"execution_instruments.{side}.asset_class must be IOPT.")

    return ExecutionInstrumentSettings(
        asset_class=asset_class,
        con_id=con_id,
        local_symbol=local_symbol,
        exchange=exchange.upper() if exchange is not None else None,
        currency=currency,
    )


def _empty_execution_instrument(side: str) -> ExecutionInstrumentSettings:
    return ExecutionInstrumentSettings(
        asset_class="IOPT",
        con_id=None,
        local_symbol=None,
        exchange=None,
        currency="EUR",
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


def _optional_section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SettingsValidationError(f"Invalid settings section: {key}.")
    return value


def _required_string(section: dict[str, Any], key: str, field_name: str) -> str:
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SettingsValidationError(f"{field_name} is required.")
    return value.strip()


def _optional_string(section: dict[str, Any], key: str, field_name: str) -> str | None:
    value = section.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SettingsValidationError(f"{field_name} must be a non-empty string when provided.")
    return value.strip()


def _required_int(section: dict[str, Any], key: str, field_name: str) -> int:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingsValidationError(f"{field_name} must be an integer.")
    return value


def _optional_int(section: dict[str, Any], key: str, field_name: str, default: int) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingsValidationError(f"{field_name} must be an integer.")
    return value


def _optional_int_or_none(section: dict[str, Any], key: str, field_name: str) -> int | None:
    value = section.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingsValidationError(f"{field_name} must be an integer when provided.")
    if value <= 0:
        raise SettingsValidationError(f"{field_name} must be greater than zero when provided.")
    return value


def _required_float(section: dict[str, Any], key: str, field_name: str) -> float:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsValidationError(f"{field_name} must be a number.")
    return float(value)


def _optional_float(section: dict[str, Any], key: str, field_name: str, default: float) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsValidationError(f"{field_name} must be a number.")
    return float(value)


def _optional_bool(section: dict[str, Any], key: str, field_name: str, default: bool) -> bool:
    value = section.get(key, default)
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
