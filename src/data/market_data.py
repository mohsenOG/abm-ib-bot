"""Fetch closed 1-hour market data from Interactive Brokers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from config.settings import MarketDataSettings
from data.schema import (
    CANDLE_AVERAGE,
    CANDLE_BAR_COUNT,
    CANDLE_CLOSE,
    CANDLE_HIGH,
    CANDLE_LOW,
    CANDLE_OPEN,
    CANDLE_TIMESTAMP,
    CANDLE_VOLUME,
    REQUIRED_CANDLE_COLUMNS,
)
from data.timeframes import bar_size_to_timedelta
from logging_setup.logger import get_logger


class MarketDataError(RuntimeError):
    """Raised when market data cannot be fetched or normalized safely."""


@dataclass(frozen=True)
class HistoricalDataRequest:
    end_datetime: str = ""


class MarketDataClient:
    """Fetch historical bars from IB and return closed configured candles only."""

    def __init__(self, ib_client: Any, *, settings: MarketDataSettings) -> None:
        self._ib_client = ib_client
        self._settings = settings
        self._candle_duration = bar_size_to_timedelta(settings.bar_size)
        self._close_buffer = pd.Timedelta(seconds=settings.candle_close_buffer_seconds)
        self._logger = get_logger("data.market_data")

    async def fetch_historical_bars(
        self,
        contract: Any,
        request: HistoricalDataRequest | None = None,
        *,
        now: datetime | pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        """Fetch normalized historical bars for a qualified contract.

        The returned DataFrame excludes any bar whose configured interval has not
        ended yet. This prevents the current forming candle from reaching the
        strategy layer.
        """

        ib = self._connected_ib()
        data_request = request if request is not None else HistoricalDataRequest()
        _validate_request(data_request)

        self._logger.info(
            "Requesting IB historical bars. bar_size=%s duration=%s what_to_show=%s use_rth=%s",
            self._settings.bar_size,
            self._settings.historical_duration,
            self._settings.what_to_show,
            self._settings.use_rth,
        )

        try:
            bars = await ib.reqHistoricalDataAsync(
                contract,
                endDateTime=data_request.end_datetime,
                durationStr=self._settings.historical_duration,
                barSizeSetting=self._settings.bar_size,
                whatToShow=self._settings.what_to_show,
                useRTH=self._settings.use_rth,
                formatDate=2,
                keepUpToDate=False,
            )
        except Exception as exc:
            self._logger.exception("IB historical data request failed.")
            raise MarketDataError("Failed to fetch Interactive Brokers historical bars.") from exc

        try:
            frame = _bars_to_frame(bars)
            closed = _closed_candles_only(
                frame,
                candle_duration=self._candle_duration,
                close_buffer=self._close_buffer,
                now=now,
            )
        except Exception as exc:
            self._logger.exception("Failed to normalize IB historical bars.")
            raise MarketDataError("Failed to normalize Interactive Brokers historical bars.") from exc

        if frame.empty:
            self._logger.warning("IB historical data request returned no bars.")
        elif closed.empty:
            self._logger.warning("IB historical data contained no closed candles.")
        elif len(closed) < len(frame):
            self._logger.info("Dropped %s incomplete candle(s).", len(frame) - len(closed))

        return closed

    def _connected_ib(self) -> Any:
        ib = _resolve_ib_client(self._ib_client)
        is_connected = getattr(ib, "isConnected", None)
        if callable(is_connected) and not is_connected():
            raise MarketDataError("Interactive Brokers is disconnected.")
        return ib


def _resolve_ib_client(ib_client: Any) -> Any:
    if _looks_like_ib_client(ib_client):
        return ib_client

    try:
        candidate = getattr(ib_client, "ib")
    except Exception as exc:
        raise MarketDataError("Could not access Interactive Brokers client.") from exc

    if not _looks_like_ib_client(candidate):
        raise MarketDataError("MarketDataClient requires an Interactive Brokers client.")

    return candidate


def _looks_like_ib_client(value: Any) -> bool:
    return all(callable(getattr(value, name, None)) for name in ("reqHistoricalDataAsync", "isConnected"))


def _validate_request(request: HistoricalDataRequest) -> None:
    if not isinstance(request.end_datetime, str):
        raise MarketDataError("Historical data end_datetime must be a string.")


def _bars_to_frame(bars: Any) -> pd.DataFrame:
    rows = [_bar_to_row(bar) for bar in bars]
    if not rows:
        return _empty_candle_frame()

    frame = pd.DataFrame(rows, columns=REQUIRED_CANDLE_COLUMNS)
    frame = frame.dropna(subset=[CANDLE_TIMESTAMP])
    frame = frame.drop_duplicates(subset=[CANDLE_TIMESTAMP], keep="last")
    frame = frame.sort_values(CANDLE_TIMESTAMP).reset_index(drop=True)
    return frame


def _bar_to_row(bar: Any) -> dict[str, Any]:
    return {
        CANDLE_TIMESTAMP: _normalize_timestamp(getattr(bar, "date", None)),
        CANDLE_OPEN: _float_attr(bar, "open"),
        CANDLE_HIGH: _float_attr(bar, "high"),
        CANDLE_LOW: _float_attr(bar, "low"),
        CANDLE_CLOSE: _float_attr(bar, "close"),
        CANDLE_VOLUME: _float_attr(bar, "volume"),
        CANDLE_AVERAGE: _float_attr(bar, "average", fallback_attr="wap"),
        CANDLE_BAR_COUNT: _int_attr(bar, "barCount", fallback_attr="count"),
    }


def _closed_candles_only(
    frame: pd.DataFrame,
    *,
    candle_duration: pd.Timedelta,
    close_buffer: pd.Timedelta,
    now: datetime | pd.Timestamp | None = None,
) -> pd.DataFrame:
    if frame.empty:
        return frame

    current_time = _normalize_now(now)
    candle_end = frame[CANDLE_TIMESTAMP] + candle_duration + close_buffer
    closed = frame.loc[candle_end <= current_time].copy()
    return closed.reset_index(drop=True)


def _normalize_timestamp(value: Any) -> pd.Timestamp | pd.NaT:
    if value is None:
        return pd.NaT

    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(timezone.utc)
    else:
        timestamp = timestamp.tz_convert(timezone.utc)

    return timestamp


def _normalize_now(value: datetime | pd.Timestamp | None) -> pd.Timestamp:
    timestamp = pd.Timestamp(value if value is not None else datetime.now(timezone.utc))
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(timezone.utc)
    else:
        timestamp = timestamp.tz_convert(timezone.utc)
    return timestamp


def _empty_candle_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=REQUIRED_CANDLE_COLUMNS)


def _float_attr(source: Any, name: str, *, fallback_attr: str | None = None) -> float:
    value = getattr(source, name, None)
    if value is None and fallback_attr is not None:
        value = getattr(source, fallback_attr, None)
    if value is None:
        return 0.0
    return float(value)


def _int_attr(source: Any, name: str, *, fallback_attr: str | None = None) -> int:
    value = getattr(source, name, None)
    if value is None and fallback_attr is not None:
        value = getattr(source, fallback_attr, None)
    if value is None:
        return 0
    return int(value)
