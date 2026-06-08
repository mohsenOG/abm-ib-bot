"""Maintain clean in-memory 1-hour candle history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from typing import Any

import pandas as pd


CANDLE_INTERVAL = pd.Timedelta(hours=1)
REQUIRED_CANDLE_COLUMNS = (
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "average",
    "bar_count",
)
NUMERIC_CANDLE_COLUMNS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "average",
    "bar_count",
)


class CandleStoreError(ValueError):
    """Raised when candle history cannot be stored safely."""


@dataclass(frozen=True)
class CandleGap:
    previous_timestamp: pd.Timestamp
    next_timestamp: pd.Timestamp
    missing_from: pd.Timestamp
    missing_to: pd.Timestamp
    missing_count: int


@dataclass(frozen=True)
class CandleStoreUpdate:
    rows_received: int
    rows_stored: int
    latest_closed_candle_ts: pd.Timestamp | None
    gaps: tuple[CandleGap, ...]


class CandleStore:
    """Store closed 1-hour candles, deduplicated and sorted by timestamp."""

    def __init__(
        self,
        candles: pd.DataFrame | None = None,
        *,
        latest_processed_candle_ts: Any | None = None,
    ) -> None:
        self._candles = _empty_candle_frame()
        self._latest_processed_candle_ts = _optional_timestamp(latest_processed_candle_ts)

        if candles is not None:
            self.update(candles)

    @property
    def latest_processed_candle_ts(self) -> pd.Timestamp | None:
        return self._latest_processed_candle_ts

    @property
    def latest_closed_candle_ts(self) -> pd.Timestamp | None:
        if self._candles.empty:
            return None
        return self._candles.iloc[-1]["timestamp"]

    def update(self, candles: pd.DataFrame) -> CandleStoreUpdate:
        """Merge closed candles into the store and return update metadata."""

        clean_new_candles = _clean_candles(candles)
        if not clean_new_candles.empty:
            self._candles = _merge_candles(self._candles, clean_new_candles)

        return CandleStoreUpdate(
            rows_received=len(candles),
            rows_stored=len(self._candles),
            latest_closed_candle_ts=self.latest_closed_candle_ts,
            gaps=self.detect_missing_candles(),
        )

    def get_candles(self) -> pd.DataFrame:
        """Return a defensive copy of the clean candle history."""

        return self._candles.copy()

    def get_latest_closed_candle(self) -> dict[str, Any] | None:
        """Return the latest stored closed candle as a plain dictionary."""

        if self._candles.empty:
            return None
        return dict(self._candles.iloc[-1])

    def has_new_closed_candle(self) -> bool:
        """Return whether a stored candle is newer than the processed marker."""

        latest_closed = self.latest_closed_candle_ts
        if latest_closed is None:
            return False

        if self._latest_processed_candle_ts is None:
            return True

        return latest_closed > self._latest_processed_candle_ts

    def get_new_closed_candles(self) -> pd.DataFrame:
        """Return stored candles newer than the processed marker."""

        if self._candles.empty:
            return self._candles.copy()

        if self._latest_processed_candle_ts is None:
            return self._candles.copy()

        mask = self._candles["timestamp"] > self._latest_processed_candle_ts
        return self._candles.loc[mask].reset_index(drop=True).copy()

    def mark_processed(self, timestamp: Any) -> None:
        """Mark a stored candle timestamp as processed."""

        processed_ts = _required_timestamp(timestamp, "timestamp")
        if not self._contains_timestamp(processed_ts):
            raise CandleStoreError(f"Cannot mark unknown candle as processed: {processed_ts}.")

        latest_closed = self.latest_closed_candle_ts
        if latest_closed is not None and processed_ts > latest_closed:
            raise CandleStoreError("Cannot mark a future candle as processed.")

        self._latest_processed_candle_ts = processed_ts

    def detect_missing_candles(self) -> tuple[CandleGap, ...]:
        """Return detected gaps larger than the expected 1-hour interval."""

        return _detect_missing_candles(self._candles)

    def _contains_timestamp(self, timestamp: pd.Timestamp) -> bool:
        if self._candles.empty:
            return False
        return bool((self._candles["timestamp"] == timestamp).any())


def _merge_candles(current: pd.DataFrame, new_candles: pd.DataFrame) -> pd.DataFrame:
    merged = pd.concat([current, new_candles], ignore_index=True)
    merged = merged.drop_duplicates(subset=["timestamp"], keep="last")
    merged = merged.sort_values("timestamp").reset_index(drop=True)
    return merged.loc[:, REQUIRED_CANDLE_COLUMNS]


def _clean_candles(candles: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(candles, pd.DataFrame):
        raise CandleStoreError("Candles must be provided as a pandas DataFrame.")

    _validate_columns(candles)

    clean = candles.loc[:, REQUIRED_CANDLE_COLUMNS].copy()
    clean["timestamp"] = clean["timestamp"].map(_normalize_timestamp)

    if clean["timestamp"].isna().any():
        raise CandleStoreError("Candles contain missing or invalid timestamps.")

    for column in NUMERIC_CANDLE_COLUMNS:
        clean[column] = pd.to_numeric(clean[column], errors="raise")

    if clean.loc[:, NUMERIC_CANDLE_COLUMNS].isna().any().any():
        raise CandleStoreError("Candles contain missing numeric values.")

    clean = clean.drop_duplicates(subset=["timestamp"], keep="last")
    clean = clean.sort_values("timestamp").reset_index(drop=True)
    return clean


def _validate_columns(candles: pd.DataFrame) -> None:
    missing = [column for column in REQUIRED_CANDLE_COLUMNS if column not in candles.columns]
    if missing:
        missing_columns = ", ".join(missing)
        raise CandleStoreError(f"Candle DataFrame is missing required columns: {missing_columns}.")


def _detect_missing_candles(candles: pd.DataFrame) -> tuple[CandleGap, ...]:
    if len(candles) < 2:
        return ()

    timestamps = list(candles["timestamp"])
    gaps: list[CandleGap] = []

    for previous_timestamp, next_timestamp in zip(timestamps, timestamps[1:]):
        delta = next_timestamp - previous_timestamp
        if delta <= CANDLE_INTERVAL:
            continue

        missing_count = int(delta / CANDLE_INTERVAL) - 1
        if missing_count <= 0:
            continue

        gaps.append(
            CandleGap(
                previous_timestamp=previous_timestamp,
                next_timestamp=next_timestamp,
                missing_from=previous_timestamp + CANDLE_INTERVAL,
                missing_to=next_timestamp - CANDLE_INTERVAL,
                missing_count=missing_count,
            )
        )

    return tuple(gaps)


def _empty_candle_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=REQUIRED_CANDLE_COLUMNS)


def _required_timestamp(value: Any, name: str) -> pd.Timestamp:
    timestamp = _normalize_timestamp(value)
    if pd.isna(timestamp):
        raise CandleStoreError(f"{name} must be a valid timestamp.")
    return timestamp


def _optional_timestamp(value: Any | None) -> pd.Timestamp | None:
    if value is None:
        return None
    return _required_timestamp(value, "latest_processed_candle_ts")


def _normalize_timestamp(value: Any) -> pd.Timestamp | pd.NaT:
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
