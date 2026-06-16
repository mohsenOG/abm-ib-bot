"""Maintain clean in-memory 1-hour candle history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from data.schema import CANDLE_TIMESTAMP, NUMERIC_CANDLE_COLUMNS, REQUIRED_CANDLE_COLUMNS
from data.timeframes import bar_size_to_timedelta


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
    rows_dropped_unfinished: int = 0


class CandleStore:
    """Store closed candles, deduplicated and sorted by timestamp."""

    def __init__(
        self,
        candles: pd.DataFrame | None = None,
        *,
        bar_size: str,
        latest_processed_candle_ts: Any | None = None,
        candle_close_buffer_seconds: float = 0,
    ) -> None:
        self._candles = _empty_candle_frame()
        self._candle_interval = bar_size_to_timedelta(bar_size)
        self._latest_processed_candle_ts = _optional_timestamp(latest_processed_candle_ts)
        self._close_buffer = _non_negative_timedelta(candle_close_buffer_seconds, "candle_close_buffer_seconds")

        if candles is not None:
            self.update(candles)

    @property
    def latest_processed_candle_ts(self) -> pd.Timestamp | None:
        return self._latest_processed_candle_ts

    @property
    def latest_closed_candle_ts(self) -> pd.Timestamp | None:
        if self._candles.empty:
            return None
        return self._candles.iloc[-1][CANDLE_TIMESTAMP]

    @property
    def candle_interval(self) -> pd.Timedelta:
        return self._candle_interval

    def update(self, candles: pd.DataFrame, *, now: datetime | pd.Timestamp | None = None) -> CandleStoreUpdate:
        """Merge closed candles into the store and return update metadata."""

        clean_new_candles = _clean_candles(candles)
        if not clean_new_candles.empty:
            self._candles = _merge_candles(self._candles, clean_new_candles)

        rows_before_closed_filter = len(self._candles)
        self._candles = _closed_candles_only(
            self._candles,
            candle_interval=self._candle_interval,
            close_buffer=self._close_buffer,
            now=now,
        )
        rows_dropped_unfinished = rows_before_closed_filter - len(self._candles)
        _validate_candle_spacing(self._candles, self._candle_interval)

        return CandleStoreUpdate(
            rows_received=len(candles),
            rows_stored=len(self._candles),
            latest_closed_candle_ts=self.latest_closed_candle_ts,
            gaps=self.detect_missing_candles(),
            rows_dropped_unfinished=rows_dropped_unfinished,
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

        mask = self._candles[CANDLE_TIMESTAMP] > self._latest_processed_candle_ts
        return self._candles.loc[mask].reset_index(drop=True).copy()

    def trim_to_latest(self, max_rows: int) -> None:
        """Keep only the most recent candles needed by the runtime session."""

        if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows <= 0:
            raise CandleStoreError("max_rows must be a positive integer.")
        if len(self._candles) <= max_rows:
            return
        self._candles = self._candles.tail(max_rows).reset_index(drop=True)

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
        """Return detected gaps larger than the configured candle interval."""

        return _detect_missing_candles(self._candles, self._candle_interval)

    def _contains_timestamp(self, timestamp: pd.Timestamp) -> bool:
        if self._candles.empty:
            return False
        return bool((self._candles[CANDLE_TIMESTAMP] == timestamp).any())


def _merge_candles(current: pd.DataFrame, new_candles: pd.DataFrame) -> pd.DataFrame:
    merged = pd.concat([current, new_candles], ignore_index=True)
    merged = merged.drop_duplicates(subset=[CANDLE_TIMESTAMP], keep="last")
    merged = merged.sort_values(CANDLE_TIMESTAMP).reset_index(drop=True)
    return merged.loc[:, REQUIRED_CANDLE_COLUMNS]


def _closed_candles_only(
    candles: pd.DataFrame,
    *,
    candle_interval: pd.Timedelta,
    close_buffer: pd.Timedelta,
    now: datetime | pd.Timestamp | None = None,
) -> pd.DataFrame:
    if candles.empty:
        return candles

    current_time = _normalize_now(now)
    candle_end = candles[CANDLE_TIMESTAMP] + candle_interval + close_buffer
    closed = candles.loc[candle_end <= current_time].copy()
    return closed.reset_index(drop=True)


def _clean_candles(candles: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(candles, pd.DataFrame):
        raise CandleStoreError("Candles must be provided as a pandas DataFrame.")

    _validate_columns(candles)

    clean = candles.loc[:, REQUIRED_CANDLE_COLUMNS].copy()
    clean[CANDLE_TIMESTAMP] = clean[CANDLE_TIMESTAMP].map(_normalize_timestamp)

    if clean[CANDLE_TIMESTAMP].isna().any():
        raise CandleStoreError("Candles contain missing or invalid timestamps.")

    for column in NUMERIC_CANDLE_COLUMNS:
        clean[column] = pd.to_numeric(clean[column], errors="raise")

    if clean.loc[:, NUMERIC_CANDLE_COLUMNS].isna().any().any():
        raise CandleStoreError("Candles contain missing numeric values.")

    clean = clean.drop_duplicates(subset=[CANDLE_TIMESTAMP], keep="last")
    clean = clean.sort_values(CANDLE_TIMESTAMP).reset_index(drop=True)
    return clean


def _validate_columns(candles: pd.DataFrame) -> None:
    missing = [column for column in REQUIRED_CANDLE_COLUMNS if column not in candles.columns]
    if missing:
        missing_columns = ", ".join(missing)
        raise CandleStoreError(f"Candle DataFrame is missing required columns: {missing_columns}.")


def _detect_missing_candles(candles: pd.DataFrame, candle_interval: pd.Timedelta) -> tuple[CandleGap, ...]:
    if len(candles) < 2:
        return ()

    timestamps = list(candles[CANDLE_TIMESTAMP])
    gaps: list[CandleGap] = []

    for previous_timestamp, next_timestamp in zip(timestamps, timestamps[1:]):
        delta = next_timestamp - previous_timestamp
        if delta <= candle_interval:
            continue

        missing_count = int(delta / candle_interval) - 1
        if missing_count <= 0:
            continue

        gaps.append(
            CandleGap(
                previous_timestamp=previous_timestamp,
                next_timestamp=next_timestamp,
                missing_from=previous_timestamp + candle_interval,
                missing_to=next_timestamp - candle_interval,
                missing_count=missing_count,
            )
        )

    return tuple(gaps)


def _validate_candle_spacing(candles: pd.DataFrame, candle_interval: pd.Timedelta) -> None:
    if len(candles) < 2:
        return

    timestamps = list(candles[CANDLE_TIMESTAMP])
    for previous_timestamp, next_timestamp in zip(timestamps, timestamps[1:]):
        delta = next_timestamp - previous_timestamp
        if delta < candle_interval:
            raise CandleStoreError(
                f"Candle spacing is shorter than configured interval: "
                f"previous={previous_timestamp} next={next_timestamp} delta={delta}."
            )
        if delta % candle_interval != pd.Timedelta(0):
            raise CandleStoreError(
                f"Candle spacing is not aligned to configured interval: "
                f"previous={previous_timestamp} next={next_timestamp} delta={delta}."
            )


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


def _non_negative_timedelta(value: Any, name: str) -> pd.Timedelta:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CandleStoreError(f"{name} must be a non-negative number.")
    if value < 0:
        raise CandleStoreError(f"{name} must be zero or greater.")
    return pd.Timedelta(seconds=float(value))


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


def _normalize_now(value: datetime | pd.Timestamp | None) -> pd.Timestamp:
    timestamp = pd.Timestamp(value if value is not None else datetime.now(timezone.utc))
    if timestamp.tzinfo is None:
        return timestamp.tz_localize(timezone.utc)
    return timestamp.tz_convert(timezone.utc)
