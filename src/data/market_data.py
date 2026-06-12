"""Fetch closed 1-hour market data from Interactive Brokers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from logging_setup.logger import get_logger


BAR_SIZE = "1 hour"
DEFAULT_DURATION = "30 D"
DEFAULT_WHAT_TO_SHOW = "MIDPOINT"
DEFAULT_USE_RTH = False
CANDLE_DURATION = pd.Timedelta(hours=1)
CANDLE_CLOSE_BUFFER = pd.Timedelta(seconds=5)
CANDLE_COLUMNS = (
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "average",
    "bar_count",
)


class MarketDataError(RuntimeError):
    """Raised when market data cannot be fetched or normalized safely."""


@dataclass(frozen=True)
class HistoricalDataRequest:
    duration: str = DEFAULT_DURATION
    what_to_show: str = DEFAULT_WHAT_TO_SHOW
    use_rth: bool = DEFAULT_USE_RTH
    end_datetime: str = ""


class MarketDataClient:
    """Fetch historical bars from IB and return closed 1-hour candles only."""

    def __init__(self, ib_client: Any) -> None:
        self._ib_client = ib_client
        self._logger = get_logger("data.market_data")

    async def fetch_historical_bars(
        self,
        contract: Any,
        request: HistoricalDataRequest | None = None,
        *,
        now: datetime | pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        """Fetch normalized 1-hour historical bars for a qualified contract.

        The returned DataFrame excludes any bar whose 1-hour interval has not
        ended yet. This prevents the current forming candle from reaching the
        strategy layer.
        """

        ib = self._connected_ib()
        data_request = request if request is not None else HistoricalDataRequest()
        _validate_request(data_request)

        self._logger.info(
            "Requesting IB historical bars. bar_size=%s duration=%s what_to_show=%s use_rth=%s",
            BAR_SIZE,
            data_request.duration,
            data_request.what_to_show,
            data_request.use_rth,
        )

        try:
            bars = await ib.reqHistoricalDataAsync(
                contract,
                endDateTime=data_request.end_datetime,
                durationStr=data_request.duration,
                barSizeSetting=BAR_SIZE,
                whatToShow=data_request.what_to_show,
                useRTH=data_request.use_rth,
                formatDate=2,
                keepUpToDate=False,
            )
        except Exception as exc:
            self._logger.exception("IB historical data request failed.")
            raise MarketDataError("Failed to fetch Interactive Brokers historical bars.") from exc

        try:
            frame = _bars_to_frame(bars)
            closed = _closed_candles_only(frame, now=now)
        except Exception as exc:
            self._logger.exception("Failed to normalize IB historical bars.")
            raise MarketDataError("Failed to normalize Interactive Brokers historical bars.") from exc

        if frame.empty:
            self._logger.warning("IB historical data request returned no bars.")
        elif closed.empty:
            self._logger.warning("IB historical data contained no closed 1-hour candles.")
        elif len(closed) < len(frame):
            self._logger.info("Dropped %s incomplete 1-hour candle(s).", len(frame) - len(closed))

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
    if not isinstance(request.duration, str) or not request.duration.strip():
        raise MarketDataError("Historical data duration is required.")

    if not isinstance(request.what_to_show, str) or not request.what_to_show.strip():
        raise MarketDataError("Historical data what_to_show is required.")

    if not isinstance(request.use_rth, bool):
        raise MarketDataError("Historical data use_rth must be true or false.")

    if not isinstance(request.end_datetime, str):
        raise MarketDataError("Historical data end_datetime must be a string.")


def _bars_to_frame(bars: Any) -> pd.DataFrame:
    rows = [_bar_to_row(bar) for bar in bars]
    if not rows:
        return _empty_candle_frame()

    frame = pd.DataFrame(rows, columns=CANDLE_COLUMNS)
    frame = frame.dropna(subset=["timestamp"])
    frame = frame.drop_duplicates(subset=["timestamp"], keep="last")
    frame = frame.sort_values("timestamp").reset_index(drop=True)
    return frame


def _bar_to_row(bar: Any) -> dict[str, Any]:
    return {
        "timestamp": _normalize_timestamp(getattr(bar, "date", None)),
        "open": _float_attr(bar, "open"),
        "high": _float_attr(bar, "high"),
        "low": _float_attr(bar, "low"),
        "close": _float_attr(bar, "close"),
        "volume": _float_attr(bar, "volume"),
        "average": _float_attr(bar, "average", fallback_attr="wap"),
        "bar_count": _int_attr(bar, "barCount", fallback_attr="count"),
    }


def _closed_candles_only(
    frame: pd.DataFrame,
    *,
    now: datetime | pd.Timestamp | None = None,
) -> pd.DataFrame:
    if frame.empty:
        return frame

    current_time = _normalize_now(now)
    candle_end = frame["timestamp"] + CANDLE_DURATION + CANDLE_CLOSE_BUFFER
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
    return pd.DataFrame(columns=CANDLE_COLUMNS)


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
