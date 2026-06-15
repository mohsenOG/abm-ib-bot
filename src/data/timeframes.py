"""Supported market-data timeframe conversions."""

from __future__ import annotations

import pandas as pd


SUPPORTED_BAR_SIZE_1_HOUR = "1 hour"


class TimeframeError(ValueError):
    """Raised when an unsupported market-data bar size is used."""


def bar_size_to_timedelta(bar_size: str) -> pd.Timedelta:
    """Return the candle interval for a supported IB bar size."""

    if bar_size == SUPPORTED_BAR_SIZE_1_HOUR:
        return pd.Timedelta(hours=1)

    raise TimeframeError(f"Unsupported market_data.bar_size: {bar_size!r}.")
