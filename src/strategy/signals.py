"""Generate BUY/SELL signals from ABM bias threshold crossovers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from typing import Literal

import pandas as pd


SignalSide = Literal["BUY", "SELL"]
REQUIRED_COLUMNS = ("timestamp", "close", "bias", "confidence")


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
) -> Signal | None:
    """Return the latest BUY/SELL signal from bias crossover, or None."""

    clean = _prepare_biased_data(biased_data)
    if len(clean) < 2:
        return None

    threshold = _validate_bias_threshold(bias_threshold)
    buy_line = pd.Series(threshold, index=clean.index, dtype="float64")
    sell_line = pd.Series(-threshold, index=clean.index, dtype="float64")

    buy_signals = crossover(clean["bias"], buy_line).fillna(False)
    sell_signals = crossunder(clean["bias"], sell_line).fillna(False)

    current_index = clean.index[-1]
    if bool(buy_signals.loc[current_index]):
        side: SignalSide = "BUY"
    elif bool(sell_signals.loc[current_index]):
        side = "SELL"
    else:
        return None

    current = clean.loc[current_index]
    signal = _build_signal(current, side)
    if signal.signal_id == last_signal_id:
        return None

    return signal


def _prepare_biased_data(biased_data: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(biased_data, pd.DataFrame):
        raise SignalEngineError("Biased data must be provided as a pandas DataFrame.")

    missing_columns = [column for column in REQUIRED_COLUMNS if column not in biased_data.columns]
    if missing_columns:
        missing = ", ".join(missing_columns)
        raise SignalEngineError(f"Biased DataFrame is missing required columns: {missing}.")

    result = biased_data.loc[:, REQUIRED_COLUMNS].copy()
    result["timestamp"] = result["timestamp"].map(_normalize_timestamp)
    if result["timestamp"].isna().any():
        raise SignalEngineError("Biased DataFrame contains missing or invalid timestamps.")

    for column in ("close", "bias", "confidence"):
        result[column] = pd.to_numeric(result[column], errors="raise")

    result = result.drop_duplicates(subset=["timestamp"], keep="last")
    result = result.sort_values("timestamp").reset_index(drop=True)
    return result


def _validate_bias_threshold(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SignalEngineError("bias_threshold must be a number.")

    threshold = float(value)
    if pd.isna(threshold) or threshold <= 0 or threshold >= 1:
        raise SignalEngineError("bias_threshold must be greater than 0 and lower than 1.")

    return threshold


def _build_signal(row: pd.Series, side: SignalSide) -> Signal:
    timestamp = row["timestamp"]
    price = row["close"]
    bias = row["bias"]
    confidence = row["confidence"]

    if pd.isna(price) or pd.isna(bias) or pd.isna(confidence):
        raise SignalEngineError("Signal row contains missing price, bias, or confidence.")

    timestamp_text = timestamp.isoformat()
    signal_id = f"{timestamp_text}_{side}"

    return Signal(
        signal_id=signal_id,
        timestamp=timestamp_text,
        side=side,
        price=float(price),
        bias=float(bias),
        confidence=float(confidence),
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
