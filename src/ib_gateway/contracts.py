"""Build and qualify Interactive Brokers contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from ib_insync import Commodity, Contract

from logging_setup.logger import get_logger


ExecutionSide = Literal["long", "short"]


class ContractBuildError(ValueError):
    """Raised when configured contract fields are missing or unsupported."""


class ContractQualificationError(RuntimeError):
    """Raised when IB cannot qualify a contract exactly."""


@dataclass(frozen=True)
class SignalContractConfig:
    asset_class: str
    symbol: str
    exchange: str
    currency: str
    expiry: str | None


@dataclass(frozen=True)
class ExecutionContractConfig:
    side: ExecutionSide
    asset_class: str
    con_id: int | None
    local_symbol: str | None
    exchange: str | None
    currency: str


def build_contract(settings: Any) -> Contract:
    """Build the configured signal contract.

    This keeps the historical Task 8 entry point focused on the signal instrument.
    Use ``build_execution_contract`` for side-specific turbo products.
    """

    return build_signal_contract(settings)


def build_signal_contract(settings: Any) -> Contract:
    """Build the configured XAUUSD signal contract without qualifying it."""

    config = _load_signal_contract_config(settings)
    _validate_signal_contract_config(config)

    if config.asset_class == "CMDTY":
        return Commodity(
            symbol=config.symbol,
            exchange=config.exchange,
            currency=config.currency,
        )

    raise ContractBuildError(f"Unsupported signal asset_class: {config.asset_class}.")


def build_execution_contract(settings: Any, side: ExecutionSide) -> Contract:
    """Build a configured long or short execution contract without qualifying it."""

    config = _load_execution_contract_config(settings, side)
    _validate_execution_contract_config(config)

    contract = Contract(
        secType=config.asset_class,
        exchange=config.exchange,
        currency=config.currency,
    )

    if config.con_id is not None:
        contract.conId = config.con_id

    if config.local_symbol is not None:
        contract.localSymbol = config.local_symbol

    return contract


def qualify_contract(ib: Any, contract: Contract) -> Contract:
    """Qualify a contract through IB and return the single qualified result."""

    logger = get_logger("ib_gateway.contracts")
    _require_connected(ib)

    logger.info(
        "Qualifying IB contract. sec_type=%s symbol=%s local_symbol=%s con_id=%s exchange=%s currency=%s",
        getattr(contract, "secType", ""),
        getattr(contract, "symbol", ""),
        getattr(contract, "localSymbol", ""),
        getattr(contract, "conId", 0),
        getattr(contract, "exchange", ""),
        getattr(contract, "currency", ""),
    )

    try:
        qualified = ib.qualifyContracts(contract)
    except Exception as exc:
        logger.exception("IB contract qualification failed.")
        raise ContractQualificationError("Failed to qualify Interactive Brokers contract.") from exc

    if not qualified:
        raise ContractQualificationError(_qualification_message("No matching IB contract found", contract))

    if len(qualified) != 1:
        raise ContractQualificationError(
            _qualification_message(f"Expected one IB contract, got {len(qualified)}", contract)
        )

    qualified_contract = qualified[0]
    logger.info(
        "IB contract qualified. sec_type=%s symbol=%s local_symbol=%s con_id=%s exchange=%s currency=%s",
        getattr(qualified_contract, "secType", ""),
        getattr(qualified_contract, "symbol", ""),
        getattr(qualified_contract, "localSymbol", ""),
        getattr(qualified_contract, "conId", 0),
        getattr(qualified_contract, "exchange", ""),
        getattr(qualified_contract, "currency", ""),
    )
    return qualified_contract


def _load_signal_contract_config(settings: Any) -> SignalContractConfig:
    instrument = getattr(settings, "signal_instrument", None)
    if instrument is None:
        instrument = getattr(settings, "instrument", settings)

    return SignalContractConfig(
        asset_class=_required_string(instrument, "asset_class").upper(),
        symbol=_required_string(instrument, "symbol"),
        exchange=_required_string(instrument, "exchange").upper(),
        currency=_required_string(instrument, "currency").upper(),
        expiry=_optional_string(instrument, "expiry"),
    )


def _load_execution_contract_config(settings: Any, side: ExecutionSide) -> ExecutionContractConfig:
    if side not in ("long", "short"):
        raise ContractBuildError("Execution side must be 'long' or 'short'.")

    execution_instruments = getattr(settings, "execution_instruments", None)
    if execution_instruments is None:
        raise ContractBuildError("execution_instruments settings are required for turbo execution contracts.")

    instrument = getattr(execution_instruments, side, None)
    if instrument is None:
        raise ContractBuildError(f"execution_instruments.{side} settings are required.")

    exchange = _optional_string(instrument, "exchange")

    return ExecutionContractConfig(
        side=side,
        asset_class=_required_string(instrument, "asset_class").upper(),
        con_id=_optional_int(instrument, "con_id"),
        local_symbol=_optional_string(instrument, "local_symbol"),
        exchange=exchange.upper() if exchange is not None else None,
        currency=_required_string(instrument, "currency").upper(),
    )


def _validate_signal_contract_config(config: SignalContractConfig) -> None:
    if config.asset_class != "CMDTY":
        raise ContractBuildError("signal_instrument.asset_class must be CMDTY.")

    if config.symbol != "XAUUSD":
        raise ContractBuildError("signal_instrument.symbol must be XAUUSD for this gold bot.")

    if config.exchange != "SMART":
        raise ContractBuildError("signal_instrument.exchange must be SMART for XAUUSD.")

    if config.currency != "USD":
        raise ContractBuildError("signal_instrument.currency must be USD.")

    if config.expiry is not None:
        raise ContractBuildError("signal_instrument.expiry must be empty for CMDTY XAUUSD.")


def _validate_execution_contract_config(config: ExecutionContractConfig) -> None:
    if config.asset_class != "IOPT":
        raise ContractBuildError(f"execution_instruments.{config.side}.asset_class must be IOPT.")

    if config.con_id is None and config.local_symbol is None:
        raise ContractBuildError(
            f"execution_instruments.{config.side} requires con_id or local_symbol before execution use."
        )

    if config.exchange is None:
        raise ContractBuildError(f"execution_instruments.{config.side}.exchange is required before execution use.")

    if config.currency != "EUR":
        raise ContractBuildError(f"execution_instruments.{config.side}.currency must be EUR.")


def _require_connected(ib: Any) -> None:
    is_connected = getattr(ib, "isConnected", None)
    if callable(is_connected) and not is_connected():
        raise ContractQualificationError("Interactive Brokers is disconnected.")


def _qualification_message(prefix: str, contract: Contract) -> str:
    return (
        f"{prefix}: sec_type={getattr(contract, 'secType', '')}, "
        f"symbol={getattr(contract, 'symbol', '')}, "
        f"local_symbol={getattr(contract, 'localSymbol', '')}, "
        f"con_id={getattr(contract, 'conId', 0)}, "
        f"exchange={getattr(contract, 'exchange', '')}, "
        f"currency={getattr(contract, 'currency', '')}."
    )


def _required_string(source: Any, name: str) -> str:
    value = getattr(source, name, None)
    if not isinstance(value, str) or not value.strip():
        raise ContractBuildError(f"{name} is required.")
    return value.strip()


def _optional_string(source: Any, name: str) -> str | None:
    value = getattr(source, name, None)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ContractBuildError(f"{name} must be a non-empty string when provided.")
    return value.strip()


def _optional_int(source: Any, name: str) -> int | None:
    value = getattr(source, name, None)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractBuildError(f"{name} must be an integer when provided.")
    if value <= 0:
        raise ContractBuildError(f"{name} must be greater than zero when provided.")
    return value
