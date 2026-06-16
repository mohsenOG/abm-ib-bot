"""Parse IBKR market calendars and classify candle gaps."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd


class MarketCalendarError(ValueError):
    """Raised when an IBKR market calendar cannot be used safely."""


@dataclass(frozen=True)
class TradingSession:
    """UTC market session interval parsed from IBKR contract details."""

    start_utc: pd.Timestamp
    end_utc: pd.Timestamp


@dataclass(frozen=True)
class MissingBar:
    """A missing candle classified against the IBKR market calendar."""

    previous_timestamp: pd.Timestamp
    next_timestamp: pd.Timestamp
    missing_timestamp: pd.Timestamp
    reason: str


@dataclass(frozen=True)
class GapValidationResult:
    """Calendar-aware candle gap validation summary."""

    expected_session_gaps: tuple[MissingBar, ...]
    unexpected_data_gaps: tuple[MissingBar, ...]

    @property
    def signal_processing_allowed(self) -> bool:
        return not self.unexpected_data_gaps


@dataclass(frozen=True)
class MarketCalendar:
    """IBKR market calendar with UTC-normalized trading and liquid sessions."""

    trading_sessions: tuple[TradingSession, ...]
    liquid_sessions: tuple[TradingSession, ...]
    time_zone_id: str
    trading_hours: str
    liquid_hours: str
    refreshed_at_utc: pd.Timestamp

    @classmethod
    def from_contract_details(cls, contract_details: Any, *, refreshed_at: Any | None = None) -> "MarketCalendar":
        trading_hours = _required_text(contract_details, "tradingHours")
        liquid_hours = _required_text(contract_details, "liquidHours")
        time_zone_id = _required_text(contract_details, "timeZoneId")

        trading_sessions = _parse_ibkr_hours(trading_hours, time_zone_id)
        liquid_sessions = _parse_ibkr_hours(liquid_hours, time_zone_id)
        if not trading_sessions:
            raise MarketCalendarError("IBKR tradingHours did not contain any open sessions.")

        return cls(
            trading_sessions=trading_sessions,
            liquid_sessions=liquid_sessions,
            time_zone_id=time_zone_id,
            trading_hours=trading_hours,
            liquid_hours=liquid_hours,
            refreshed_at_utc=_normalize_timestamp(
                refreshed_at if refreshed_at is not None else datetime.now(timezone.utc),
                "refreshed_at",
            ),
        )

    def is_full_bar_inside_trading_session(self, bar_start: Any, candle_interval: pd.Timedelta) -> bool:
        start = _normalize_timestamp(bar_start, "bar_start")
        end = start + candle_interval
        return any(session.start_utc <= start and end <= session.end_utc for session in self.trading_sessions)

    def count_open_session_bars_after(
        self,
        bar_start: Any,
        latest_closed_bar_start: Any,
        candle_interval: pd.Timedelta,
    ) -> int:
        """Count expected open-session bars after one bar through the latest closed bar."""

        start = _normalize_timestamp(bar_start, "bar_start") + candle_interval
        latest_closed = _normalize_timestamp(latest_closed_bar_start, "latest_closed_bar_start")
        if latest_closed < start:
            return 0

        count = 0
        current = start
        while current <= latest_closed:
            if self.is_full_bar_inside_trading_session(current, candle_interval):
                count += 1
            current += candle_interval
        return count

    def validate_candle_gaps(self, gaps: tuple[Any, ...], candle_interval: pd.Timedelta) -> GapValidationResult:
        expected: list[MissingBar] = []
        unexpected: list[MissingBar] = []

        for gap in gaps:
            previous_timestamp = _normalize_timestamp(getattr(gap, "previous_timestamp", None), "previous_timestamp")
            next_timestamp = _normalize_timestamp(getattr(gap, "next_timestamp", None), "next_timestamp")
            missing_timestamp = previous_timestamp + candle_interval

            while missing_timestamp < next_timestamp:
                if self.is_full_bar_inside_trading_session(missing_timestamp, candle_interval):
                    unexpected.append(
                        MissingBar(
                            previous_timestamp=previous_timestamp,
                            next_timestamp=next_timestamp,
                            missing_timestamp=missing_timestamp,
                            reason="unexpected_open_session_gap",
                        )
                    )
                else:
                    expected.append(
                        MissingBar(
                            previous_timestamp=previous_timestamp,
                            next_timestamp=next_timestamp,
                            missing_timestamp=missing_timestamp,
                            reason="expected_session_gap",
                        )
                    )
                missing_timestamp += candle_interval

        return GapValidationResult(expected_session_gaps=tuple(expected), unexpected_data_gaps=tuple(unexpected))

    def format_opening_hours_table(self, *, days: int = 5, start_date_utc: date | None = None) -> str:
        """Return the next UTC dates' parsed IBKR trading and liquid sessions as a log table."""

        if isinstance(days, bool) or days <= 0:
            raise MarketCalendarError("days must be a positive integer.")

        first_date = start_date_utc if start_date_utc is not None else datetime.now(timezone.utc).date()
        rows = [("utc_date", "tradingHours_utc", "liquidHours_utc")]
        for offset in range(days):
            current_date = first_date + timedelta(days=offset)
            rows.append(
                (
                    current_date.isoformat(),
                    _format_sessions_for_utc_date(self.trading_sessions, current_date),
                    _format_sessions_for_utc_date(self.liquid_sessions, current_date),
                )
            )

        widths = [max(len(row[index]) for row in rows) for index in range(3)]
        lines = []
        for index, row in enumerate(rows):
            line = " | ".join(value.ljust(widths[column]) for column, value in enumerate(row))
            lines.append(line)
            if index == 0:
                lines.append("-+-".join("-" * width for width in widths))
        return "\n".join(lines)


def _parse_ibkr_hours(value: str, time_zone_id: str) -> tuple[TradingSession, ...]:
    zone = _zone_info(time_zone_id)
    sessions: list[TradingSession] = []

    for raw_day in value.split(";"):
        day_entry = raw_day.strip()
        if not day_entry:
            continue

        day_text, separator, intervals_text = day_entry.partition(":")
        if not separator:
            raise MarketCalendarError(f"Invalid IBKR hours day entry: {day_entry}.")
        session_date = _parse_date(day_text)

        if intervals_text.strip().upper() == "CLOSED":
            continue

        for raw_interval in intervals_text.split(","):
            interval_text = raw_interval.strip()
            if not interval_text:
                continue

            start_text, interval_separator, end_text = interval_text.partition("-")
            if not interval_separator:
                raise MarketCalendarError(f"Invalid IBKR hours interval: {interval_text}.")

            start_local = _parse_local_boundary(start_text.strip(), session_date, zone)
            end_local = _parse_local_boundary(end_text.strip(), session_date, zone)
            if end_local <= start_local:
                end_local += timedelta(days=1)

            start_utc = pd.Timestamp(start_local).tz_convert(timezone.utc)
            end_utc = pd.Timestamp(end_local).tz_convert(timezone.utc)
            if end_utc <= start_utc:
                raise MarketCalendarError(f"IBKR hours interval ends before it starts: {interval_text}.")
            sessions.append(TradingSession(start_utc=start_utc, end_utc=end_utc))

    return tuple(sorted(sessions, key=lambda session: session.start_utc))


def _format_sessions_for_utc_date(sessions: tuple[TradingSession, ...], session_date: date) -> str:
    day_start = pd.Timestamp(datetime.combine(session_date, time(0, 0), tzinfo=timezone.utc))
    day_end = day_start + pd.Timedelta(days=1)
    parts: list[str] = []

    for session in sessions:
        start = max(session.start_utc, day_start)
        end = min(session.end_utc, day_end)
        if end <= start:
            continue
        parts.append(f"{start:%H:%M}-{end:%H:%M}")

    return ", ".join(parts) if parts else "CLOSED"


def _parse_local_boundary(value: str, default_date: date, zone: ZoneInfo) -> datetime:
    boundary_date = default_date
    boundary_time = value

    if ":" in value:
        date_text, boundary_time = value.split(":", 1)
        boundary_date = _parse_date(date_text)

    if len(boundary_time) != 4 or not boundary_time.isdigit():
        raise MarketCalendarError(f"Invalid IBKR hours time: {value}.")

    hour = int(boundary_time[:2])
    minute = int(boundary_time[2:])
    if hour == 24 and minute == 0:
        return datetime.combine(boundary_date + timedelta(days=1), time(0, 0), tzinfo=zone)
    if hour > 23 or minute > 59:
        raise MarketCalendarError(f"Invalid IBKR hours time: {value}.")

    return datetime.combine(boundary_date, time(hour, minute), tzinfo=zone)


def _parse_date(value: str) -> date:
    if len(value) != 8 or not value.isdigit():
        raise MarketCalendarError(f"Invalid IBKR hours date: {value}.")
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise MarketCalendarError(f"Invalid IBKR hours date: {value}.") from exc


def _zone_info(time_zone_id: str) -> ZoneInfo:
    value = time_zone_id.strip()
    if not value:
        raise MarketCalendarError("IBKR ContractDetails.timeZoneId is required.")
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise MarketCalendarError(f"Unsupported IBKR ContractDetails.timeZoneId: {value}.") from exc


def _required_text(source: Any, name: str) -> str:
    value = getattr(source, name, None)
    if not isinstance(value, str) or not value.strip():
        raise MarketCalendarError(f"IBKR ContractDetails.{name} is required.")
    return value.strip()


def _normalize_timestamp(value: Any, name: str) -> pd.Timestamp:
    try:
        timestamp = pd.Timestamp(value)
    except Exception as exc:
        raise MarketCalendarError(f"{name} must be a valid timestamp.") from exc

    if pd.isna(timestamp):
        raise MarketCalendarError(f"{name} must be a valid timestamp.")
    if timestamp.tzinfo is None:
        raise MarketCalendarError(f"{name} must be UTC-aware.")
    return timestamp.tz_convert(timezone.utc)
