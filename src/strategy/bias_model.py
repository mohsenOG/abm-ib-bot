"""Calculate the ABM 21-vote bias model."""

from __future__ import annotations

import pandas as pd

from strategy.indicators import required_indicator_warmup_bars


AGENT_COUNT = 21

EMA_TREND_PAIRS = (
    ("ema_5", "ema_10"),
    ("ema_5", "ema_20"),
    ("ema_10", "ema_20"),
    ("ema_10", "ema_50"),
)
SMA_TREND_PAIRS = (
    ("sma_5", "sma_10"),
    ("sma_5", "sma_20"),
    ("sma_10", "sma_50"),
)
MACD_PAIRS = (
    ("macd_12_26_9", "macd_signal_12_26_9"),
    ("macd_20_50_10", "macd_signal_20_50_10"),
    ("macd_8_21_6", "macd_signal_8_21_6"),
)
EMA_FUNDAMENTALIST_RULES = (
    ("ema_20", 0.01, 0.015),
    ("ema_50", 0.01, 0.02),
    ("ema_100", 0.015, 0.025),
    ("ema_200", 0.015, 0.05),
)
BREAKOUT_PERIODS = (5, 17, 21)

REQUIRED_COLUMNS = (
    "src_close",
    "ema_5",
    "ema_10",
    "ema_20",
    "ema_50",
    "ema_100",
    "ema_200",
    "sma_5",
    "sma_10",
    "sma_20",
    "sma_50",
    "rsi_14",
    "macd_12_26_9",
    "macd_signal_12_26_9",
    "macd_20_50_10",
    "macd_signal_20_50_10",
    "macd_8_21_6",
    "macd_signal_8_21_6",
    "bb_upper_20_2",
    "bb_lower_20_2",
    "breakout_high_5",
    "breakout_low_5",
    "breakout_high_17",
    "breakout_low_17",
    "breakout_high_21",
    "breakout_low_21",
    "roc_13",
    "roc_27",
    "daily_change",
)


class BiasModelError(ValueError):
    """Raised when bias cannot be calculated safely."""


def calculate_bias(indicators: pd.DataFrame) -> pd.DataFrame:
    """Return a new DataFrame with ABM vote details, bias, and confidence."""

    result = _prepare_indicators(indicators)
    src_close = result["src_close"]

    result["ema_trend_vote"] = _sum_pair_votes(result, EMA_TREND_PAIRS)
    result["sma_trend_vote"] = _sum_pair_votes(result, SMA_TREND_PAIRS)
    result["rsi_vote"] = _threshold_vote(
        result["rsi_14"] < 30,
        result["rsi_14"] > 70,
        result.index,
    )

    result["ema_fund_vote"] = _calculate_ema_fundamentalist_vote(result, src_close)
    result["macd_vote"] = _sum_pair_votes(result, MACD_PAIRS)
    result["bb_vote"] = _threshold_vote(
        src_close < result["bb_lower_20_2"],
        src_close > result["bb_upper_20_2"],
        result.index,
    )
    result["breakout_vote"] = _calculate_breakout_vote(result, src_close)
    result["roc_vote"] = _calculate_roc_vote(result)

    vote_columns = (
        "ema_trend_vote",
        "sma_trend_vote",
        "rsi_vote",
        "ema_fund_vote",
        "macd_vote",
        "bb_vote",
        "breakout_vote",
        "roc_vote",
    )
    result["total_votes"] = result.loc[:, vote_columns].sum(axis=1).astype("int64")
    result["bias"] = result["total_votes"].astype("float64") / float(AGENT_COUNT)
    result["confidence"] = result["total_votes"].abs().astype("float64") / float(AGENT_COUNT)

    return result


def _prepare_indicators(indicators: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(indicators, pd.DataFrame):
        raise BiasModelError("Indicators must be provided as a pandas DataFrame.")

    minimum_rows = required_indicator_warmup_bars()
    if len(indicators) < minimum_rows:
        raise BiasModelError(
            f"Indicator warmup is incomplete: {len(indicators)} closed bars available; "
            f"{minimum_rows} required before bias calculation."
        )

    missing_columns = [column for column in REQUIRED_COLUMNS if column not in indicators.columns]
    if missing_columns:
        missing = ", ".join(missing_columns)
        raise BiasModelError(f"Indicator DataFrame is missing required columns: {missing}.")

    result = indicators.copy()
    for column in REQUIRED_COLUMNS:
        result[column] = pd.to_numeric(result[column], errors="raise")

    latest_missing = [column for column in REQUIRED_COLUMNS if pd.isna(result[column].iloc[-1])]
    if latest_missing:
        missing = ", ".join(latest_missing)
        raise BiasModelError(f"Latest indicator row is incomplete after warmup: {missing}.")

    return result


def _sum_pair_votes(
    indicators: pd.DataFrame,
    pairs: tuple[tuple[str, str], ...],
) -> pd.Series:
    vote = pd.Series(0, index=indicators.index, dtype="int64")
    for short_column, long_column in pairs:
        vote = vote + _comparison_vote(indicators[short_column], indicators[long_column])
    return vote.astype("int64")


def _comparison_vote(left: pd.Series, right: pd.Series) -> pd.Series:
    return (left > right).astype("int64") - (left < right).astype("int64")


def _threshold_vote(
    bullish_condition: pd.Series,
    bearish_condition: pd.Series,
    index: pd.Index,
) -> pd.Series:
    vote = pd.Series(0, index=index, dtype="int64")
    vote.loc[bullish_condition.fillna(False)] = 1
    vote.loc[bearish_condition.fillna(False)] = -1
    return vote


def _calculate_ema_fundamentalist_vote(
    indicators: pd.DataFrame,
    src_close: pd.Series,
) -> pd.Series:
    vote = pd.Series(0, index=indicators.index, dtype="int64")
    for ema_column, bullish_discount, bearish_premium in EMA_FUNDAMENTALIST_RULES:
        vote = vote + _threshold_vote(
            src_close < indicators[ema_column] * (1 - bullish_discount),
            src_close > indicators[ema_column] * (1 + bearish_premium),
            indicators.index,
        )
    return vote.astype("int64")


def _calculate_breakout_vote(indicators: pd.DataFrame, src_close: pd.Series) -> pd.Series:
    vote = pd.Series(0, index=indicators.index, dtype="int64")
    for period in BREAKOUT_PERIODS:
        vote = vote + _threshold_vote(
            src_close > indicators[f"breakout_high_{period}"],
            src_close < indicators[f"breakout_low_{period}"],
            indicators.index,
        )
    return vote.astype("int64")


def _calculate_roc_vote(indicators: pd.DataFrame) -> pd.Series:
    daily_change = indicators["daily_change"]
    roc_13_vote = _threshold_vote(
        (indicators["roc_13"] > 3.5) & (daily_change > 0.01),
        (indicators["roc_13"] < -3.5) & (daily_change < -0.01),
        indicators.index,
    )
    roc_27_vote = _threshold_vote(
        (indicators["roc_27"] > 2.0) & (daily_change > 0.01),
        (indicators["roc_27"] < -2.0) & (daily_change < -0.01),
        indicators.index,
    )
    return (roc_13_vote + roc_27_vote).astype("int64")
