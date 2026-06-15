"""Generate BUY/SELL signals from ABM bias threshold crossovers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from typing import Literal

import pandas as pd

from config.defaults import (
    DEFAULT_STRATEGY_ATR_LENGTH,
    DEFAULT_STRATEGY_SL_ATR_MULT,
    DEFAULT_STRATEGY_TP_ATR_MULT,
)
from data.schema import CANDLE_CLOSE, CANDLE_TIMESTAMP
from domain.constants import SIGNAL_DIRECTION_BUY, SIGNAL_DIRECTION_SELL
from ib_gateway.constants import SIGNAL_SYMBOL_XAUUSD

SignalSide = Literal["BUY", "SELL"]
REQUIRED_COLUMNS = (CANDLE_TIMESTAMP, CANDLE_CLOSE, "bias", "confidence")


class SignalEngineError(ValueError):
    """Raised when a signal cannot be generated safely."""


@dataclass(frozen=True)
class Signal:
    signal_id: str
    timestamp: str
    side: SignalSide
    price: float
    bias: float
    confidence: float
    underlying_symbol: str
    underlying_entry_price: float
    atr: float
    atr_pct: float
    underlying_sl_price: float
    underlying_tp_price: float
    underlying_sl_pct: float
    underlying_tp_pct: float


def crossover(x: pd.Series, y: pd.Series) -> pd.Series:
    """Return True where x crosses above y on the current row."""

    return (x.shift(1) <= y.shift(1)) & (x > y)


def crossunder(x: pd.Series, y: pd.Series) -> pd.Series:
    """Return True where x crosses below y on the current row."""

    return (x.shift(1) >= y.shift(1)) & (x < y)


def generate_signal(
    biased_data: pd.DataFrame,
    bias_threshold: float,
    last_signal_id: str | None = None,
    *,
    underlying_symbol: str = SIGNAL_SYMBOL_XAUUSD,
    atr_length: int = DEFAULT_STRATEGY_ATR_LENGTH,
    sl_atr_mult: float = DEFAULT_STRATEGY_SL_ATR_MULT,
    tp_atr_mult: float = DEFAULT_STRATEGY_TP_ATR_MULT,
) -> Signal | None:
    """Return the latest BUY/SELL signal from bias crossover, or None."""

    atr_column = _atr_column_name(atr_length)
    clean = _prepare_biased_data(biased_data, atr_column)
    if len(clean) < 2:
        return None

    threshold = _validate_bias_threshold(bias_threshold)
    symbol = _validate_symbol(underlying_symbol)
    stop_multiplier = _validate_positive_number(sl_atr_mult, "sl_atr_mult")
    take_profit_multiplier = _validate_positive_number(tp_atr_mult, "tp_atr_mult")
    buy_line = pd.Series(threshold, index=clean.index, dtype="float64")
    sell_line = pd.Series(-threshold, index=clean.index, dtype="float64")

    buy_signals = crossover(clean["bias"], buy_line).fillna(False)
    sell_signals = crossunder(clean["bias"], sell_line).fillna(False)

    current_index = clean.index[-1]
    if bool(buy_signals.loc[current_index]):
        side: SignalSide = SIGNAL_DIRECTION_BUY
    elif bool(sell_signals.loc[current_index]):
        side = SIGNAL_DIRECTION_SELL
    else:
        return None

    current = clean.loc[current_index]
    signal = _build_signal(
        current,
        side,
        atr_column=atr_column,
        underlying_symbol=symbol,
        sl_atr_mult=stop_multiplier,
        tp_atr_mult=take_profit_multiplier,
    )
    if signal.signal_id == last_signal_id:
        return None

    return signal


def _prepare_biased_data(biased_data: pd.DataFrame, atr_column: str) -> pd.DataFrame:
    if not isinstance(biased_data, pd.DataFrame):
        raise SignalEngineError("Biased data must be provided as a pandas DataFrame.")

    required_columns = REQUIRED_COLUMNS + (atr_column,)
    missing_columns = [column for column in required_columns if column not in biased_data.columns]
    if missing_columns:
        missing = ", ".join(missing_columns)
        raise SignalEngineError(f"Biased DataFrame is missing required columns: {missing}.")

    result = biased_data.loc[:, required_columns].copy()
    result[CANDLE_TIMESTAMP] = result[CANDLE_TIMESTAMP].map(_normalize_timestamp)
    if result[CANDLE_TIMESTAMP].isna().any():
        raise SignalEngineError("Biased DataFrame contains missing or invalid timestamps.")

    for column in (CANDLE_CLOSE, "bias", "confidence", atr_column):
        result[column] = pd.to_numeric(result[column], errors="raise")

    result = result.drop_duplicates(subset=[CANDLE_TIMESTAMP], keep="last")
    result = result.sort_values(CANDLE_TIMESTAMP).reset_index(drop=True)

    if result.empty:
        raise SignalEngineError("Biased DataFrame contains no rows.")

    checked_columns = (CANDLE_CLOSE, "bias", "confidence", atr_column)
    latest_missing = [column for column in checked_columns if pd.isna(result[column].iloc[-1])]
    if latest_missing:
        missing = ", ".join(latest_missing)
        raise SignalEngineError(f"Latest signal row is incomplete after warmup: {missing}.")

    return result


def _validate_bias_threshold(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SignalEngineError("bias_threshold must be a number.")

    threshold = float(value)
    if pd.isna(threshold) or threshold <= 0 or threshold >= 1:
        raise SignalEngineError("bias_threshold must be greater than 0 and lower than 1.")

    return threshold


def _build_signal(
    row: pd.Series,
    side: SignalSide,
    *,
    atr_column: str,
    underlying_symbol: str,
    sl_atr_mult: float,
    tp_atr_mult: float,
) -> Signal:
    timestamp = row[CANDLE_TIMESTAMP]
    price = row[CANDLE_CLOSE]
    bias = row["bias"]
    confidence = row["confidence"]
    atr = row[atr_column]

    if pd.isna(price) or pd.isna(bias) or pd.isna(confidence) or pd.isna(atr):
        raise SignalEngineError("Signal row contains missing price, bias, confidence, or ATR.")

    timestamp_text = timestamp.isoformat()
    signal_id = f"{timestamp_text}_{side}"
    entry_price = _positive_signal_number(price, "price")
    atr_value = _positive_signal_number(atr, "atr")
    atr_pct = atr_value / entry_price * 100
    sl_distance = atr_value * sl_atr_mult
    tp_distance = atr_value * tp_atr_mult

    if side == SIGNAL_DIRECTION_BUY:
        underlying_sl_price = entry_price - sl_distance
        underlying_tp_price = entry_price + tp_distance
    else:
        underlying_sl_price = entry_price + sl_distance
        underlying_tp_price = entry_price - tp_distance

    if underlying_sl_price <= 0 or underlying_tp_price <= 0:
        raise SignalEngineError("ATR risk model produced a non-positive underlying protective price.")

    underlying_sl_pct = sl_distance / entry_price * 100
    underlying_tp_pct = tp_distance / entry_price * 100
    return Signal(
        signal_id=signal_id,
        timestamp=timestamp_text,
        side=side,
        price=entry_price,
        bias=float(bias),
        confidence=float(confidence),
        underlying_symbol=underlying_symbol,
        underlying_entry_price=entry_price,
        atr=atr_value,
        atr_pct=atr_pct,
        underlying_sl_price=underlying_sl_price,
        underlying_tp_price=underlying_tp_price,
        underlying_sl_pct=underlying_sl_pct,
        underlying_tp_pct=underlying_tp_pct,
    )


def _normalize_timestamp(value: object) -> pd.Timestamp | pd.NaT:
    if value is None:
        return pd.NaT

    try:
        timestamp = pd.Timestamp(value)
    except Exception:
        return pd.NaT

    if pd.isna(timestamp):
        return pd.NaT

    if timestamp.tzinfo is None:
        return timestamp.tz_localize(timezone.utc)

    return timestamp.tz_convert(timezone.utc)


def _atr_column_name(atr_length: int) -> str:
    if isinstance(atr_length, bool) or not isinstance(atr_length, int):
        raise SignalEngineError("atr_length must be an integer.")
    if atr_length <= 0:
        raise SignalEngineError("atr_length must be greater than zero.")
    return f"atr_{atr_length}"


def _validate_symbol(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SignalEngineError("underlying_symbol must be a non-empty string.")
    return value.strip()


def _validate_positive_number(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SignalEngineError(f"{name} must be a positive number.")
    result = float(value)
    if pd.isna(result) or result <= 0:
        raise SignalEngineError(f"{name} must be greater than zero.")
    return result


def _positive_signal_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SignalEngineError(f"Signal {name} must be a positive number.")
    result = float(value)
    if pd.isna(result) or result <= 0:
        raise SignalEngineError(f"Signal {name} must be greater than zero.")
    return result
