"""Calculate ABM strategy indicators from closed candle data."""

from __future__ import annotations

import pandas as pd


REQUIRED_PRICE_COLUMNS = ("open", "high", "low", "close")

EMA_PERIODS = (5, 10, 20, 50, 100, 200)
SMA_PERIODS = (5, 10, 20, 50)
RSI_PERIOD = 14
MACD_PERIODS = (
    (12, 26, 9),
    (20, 50, 10),
    (8, 21, 6),
)
BOLLINGER_PERIOD = 20
BOLLINGER_STDDEV_MULTIPLIER = 2.0
DEFAULT_ATR_PERIOD = 14
ROC_PERIODS = (13, 27)
BREAKOUT_PERIODS = (5, 17, 21)


class IndicatorError(ValueError):
    """Raised when indicators cannot be calculated safely."""


def add_indicators(
    candles: pd.DataFrame,
    use_heikin_ashi: bool,
    *,
    atr_period: int = DEFAULT_ATR_PERIOD,
) -> pd.DataFrame:
    """Return a new DataFrame with all indicators required by the ABM strategy."""

    result = _prepare_candles(candles)
    result = _add_heikin_ashi_columns(result)
    result = _add_source_columns(result, use_heikin_ashi)
    result[f"atr_{atr_period}"] = calculate_atr(
        result["src_high"],
        result["src_low"],
        result["src_close"],
        atr_period,
    )

    for period in EMA_PERIODS:
        result[f"ema_{period}"] = calculate_ema(result["src_close"], period)

    for period in SMA_PERIODS:
        result[f"sma_{period}"] = calculate_sma(result["src_close"], period)

    result[f"rsi_{RSI_PERIOD}"] = calculate_rsi(result["src_close"], RSI_PERIOD)

    for fast_period, slow_period, signal_period in MACD_PERIODS:
        column_suffix = f"{fast_period}_{slow_period}_{signal_period}"
        macd_line, signal_line, histogram = calculate_macd(
            result["src_close"],
            fast_period,
            slow_period,
            signal_period,
        )
        result[f"macd_{column_suffix}"] = macd_line
        result[f"macd_signal_{column_suffix}"] = signal_line
        result[f"macd_histogram_{column_suffix}"] = histogram

    bb_basis, bb_stddev, bb_upper, bb_lower = calculate_bollinger_bands(
        result["src_close"],
        BOLLINGER_PERIOD,
        BOLLINGER_STDDEV_MULTIPLIER,
    )
    result[f"bb_basis_{BOLLINGER_PERIOD}"] = bb_basis
    result[f"bb_stddev_{BOLLINGER_PERIOD}"] = bb_stddev
    result[f"bb_upper_{BOLLINGER_PERIOD}_2"] = bb_upper
    result[f"bb_lower_{BOLLINGER_PERIOD}_2"] = bb_lower

    for period in ROC_PERIODS:
        result[f"roc_{period}"] = calculate_roc(result["src_close"], period)

    result["daily_change"] = result["src_close"] / result["src_close"].shift(1) - 1

    for period in BREAKOUT_PERIODS:
        breakout_high, breakout_low = calculate_breakout_levels(
            result["src_high"],
            result["src_low"],
            period,
        )
        result[f"breakout_high_{period}"] = breakout_high
        result[f"breakout_low_{period}"] = breakout_low

    return result


def calculate_ema(series: pd.Series, period: int) -> pd.Series:
    """Calculate an exponential moving average."""

    _validate_positive_period(period, "period")
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def calculate_sma(series: pd.Series, period: int) -> pd.Series:
    """Calculate a simple moving average."""

    _validate_positive_period(period, "period")
    return series.rolling(window=period, min_periods=period).mean()


def calculate_rsi(series: pd.Series, period: int) -> pd.Series:
    """Calculate RSI using Wilder-style smoothed gains and losses."""

    _validate_positive_period(period, "period")

    change = series.diff()
    gain = change.clip(lower=0)
    loss = -change.clip(upper=0)

    average_gain = _calculate_rma(gain, period)
    average_loss = _calculate_rma(loss, period)

    relative_strength = average_gain / average_loss
    rsi = 100 - (100 / (1 + relative_strength))
    rsi = rsi.mask((average_loss == 0) & (average_gain > 0), 100)
    rsi = rsi.mask((average_loss == 0) & (average_gain == 0), 50)
    return rsi


def calculate_macd(
    series: pd.Series,
    fast_period: int,
    slow_period: int,
    signal_period: int,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Calculate MACD line, signal line, and histogram."""

    _validate_positive_period(fast_period, "fast_period")
    _validate_positive_period(slow_period, "slow_period")
    _validate_positive_period(signal_period, "signal_period")
    if fast_period >= slow_period:
        raise IndicatorError("MACD fast_period must be lower than slow_period.")

    fast_ema = calculate_ema(series, fast_period)
    slow_ema = calculate_ema(series, slow_period)
    macd_line = fast_ema - slow_ema
    signal_line = calculate_ema(macd_line, signal_period)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def calculate_bollinger_bands(
    series: pd.Series,
    period: int,
    stddev_multiplier: float,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Calculate Bollinger Band basis, standard deviation, upper, and lower bands."""

    _validate_positive_period(period, "period")
    if stddev_multiplier <= 0:
        raise IndicatorError("stddev_multiplier must be greater than zero.")

    basis = calculate_sma(series, period)
    stddev = series.rolling(window=period, min_periods=period).std(ddof=0)
    upper = basis + stddev_multiplier * stddev
    lower = basis - stddev_multiplier * stddev
    return basis, stddev, upper, lower


def calculate_roc(series: pd.Series, period: int) -> pd.Series:
    """Calculate rate of change as a percentage."""

    _validate_positive_period(period, "period")
    previous = series.shift(period)
    return (series - previous) / previous * 100


def calculate_atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int,
) -> pd.Series:
    """Calculate Average True Range using Wilder-style smoothing."""

    _validate_positive_period(period, "period")
    true_range = calculate_true_range(high, low, close)
    return _calculate_rma(true_range, period)


def calculate_true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """Calculate true range from high, low, and previous close."""

    previous_close = close.shift(1)
    ranges = pd.concat(
        (
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ),
        axis=1,
    )
    return ranges.max(axis=1)


def calculate_breakout_levels(
    high: pd.Series,
    low: pd.Series,
    period: int,
) -> tuple[pd.Series, pd.Series]:
    """Calculate previous rolling high and low levels for breakout checks."""

    _validate_positive_period(period, "period")
    previous_high = high.shift(1).rolling(window=period, min_periods=period).max()
    previous_low = low.shift(1).rolling(window=period, min_periods=period).min()
    return previous_high, previous_low


def _prepare_candles(candles: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(candles, pd.DataFrame):
        raise IndicatorError("Candles must be provided as a pandas DataFrame.")

    missing_columns = [column for column in REQUIRED_PRICE_COLUMNS if column not in candles.columns]
    if missing_columns:
        missing = ", ".join(missing_columns)
        raise IndicatorError(f"Candle DataFrame is missing required columns: {missing}.")

    result = candles.copy()
    for column in REQUIRED_PRICE_COLUMNS:
        result[column] = pd.to_numeric(result[column], errors="raise")

    if result.loc[:, REQUIRED_PRICE_COLUMNS].isna().any().any():
        raise IndicatorError("Candle DataFrame contains missing OHLC values.")

    return result


def _add_heikin_ashi_columns(candles: pd.DataFrame) -> pd.DataFrame:
    result = candles.copy()
    result["ha_close"] = (
        result["open"] + result["high"] + result["low"] + result["close"]
    ) / 4

    ha_open_values: list[float] = []
    for row in result.itertuples(index=False):
        if not ha_open_values:
            ha_open_values.append((float(row.open) + float(row.close)) / 2)
            continue

        previous_ha_open = ha_open_values[-1]
        previous_ha_close = float(result["ha_close"].iloc[len(ha_open_values) - 1])
        ha_open_values.append((previous_ha_open + previous_ha_close) / 2)

    result["ha_open"] = pd.Series(ha_open_values, index=result.index, dtype="float64")
    result["ha_high"] = result.loc[:, ("high", "ha_open", "ha_close")].max(axis=1)
    result["ha_low"] = result.loc[:, ("low", "ha_open", "ha_close")].min(axis=1)
    return result


def _add_source_columns(candles: pd.DataFrame, use_heikin_ashi: bool) -> pd.DataFrame:
    result = candles.copy()
    if use_heikin_ashi:
        result["src_open"] = result["ha_open"]
        result["src_high"] = result["ha_high"]
        result["src_low"] = result["ha_low"]
        result["src_close"] = result["ha_close"]
    else:
        result["src_open"] = result["open"]
        result["src_high"] = result["high"]
        result["src_low"] = result["low"]
        result["src_close"] = result["close"]

    return result


def _calculate_rma(series: pd.Series, period: int) -> pd.Series:
    _validate_positive_period(period, "period")

    values = series.astype("float64")
    rolling_mean = values.rolling(window=period, min_periods=period).mean()
    result = pd.Series(index=series.index, dtype="float64")

    previous_value: float | None = None
    for index, value in values.items():
        if pd.isna(value):
            result.loc[index] = pd.NA
            continue

        if previous_value is None:
            seed_value = rolling_mean.loc[index]
            if pd.isna(seed_value):
                result.loc[index] = pd.NA
                continue
            previous_value = float(seed_value)
        else:
            previous_value = (previous_value * (period - 1) + float(value)) / period

        result.loc[index] = previous_value

    return result


def _validate_positive_period(period: int, name: str) -> None:
    if not isinstance(period, int):
        raise IndicatorError(f"{name} must be an integer.")

    if period <= 0:
        raise IndicatorError(f"{name} must be greater than zero.")
