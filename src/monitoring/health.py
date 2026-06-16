"""Health checks for bot safety monitoring."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from logging_setup.logger import get_logger


HealthLevel = Literal["ok", "warning", "critical"]


class HealthMonitorError(ValueError):
    """Raised when health check inputs are invalid."""


@dataclass(frozen=True)
class HealthCheckResult:
    name: str
    level: HealthLevel
    message: str
    checked_at: str

    @property
    def ok(self) -> bool:
        return self.level == "ok"


@dataclass(frozen=True)
class HealthReport:
    checked_at: str
    checks: tuple[HealthCheckResult, ...]

    @property
    def level(self) -> HealthLevel:
        if any(check.level == "critical" for check in self.checks):
            return "critical"
        if any(check.level == "warning" for check in self.checks):
            return "warning"
        return "ok"

    @property
    def ok(self) -> bool:
        return self.level == "ok"

    @property
    def critical(self) -> bool:
        return self.level == "critical"


class HealthMonitor:
    """Run health checks without making trading decisions."""

    def __init__(
        self,
        *,
        market_data_max_age: timedelta,
        last_processed_candle_max_age: timedelta,
        repeated_error_limit: int,
        notifier: Any | None = None,
    ) -> None:
        if market_data_max_age <= timedelta(0):
            raise HealthMonitorError("market_data_max_age must be greater than zero.")
        if last_processed_candle_max_age <= timedelta(0):
            raise HealthMonitorError("last_processed_candle_max_age must be greater than zero.")
        if repeated_error_limit <= 0:
            raise HealthMonitorError("repeated_error_limit must be greater than zero.")

        self.market_data_max_age = market_data_max_age
        self.last_processed_candle_max_age = last_processed_candle_max_age
        self.repeated_error_limit = repeated_error_limit
        self.notifier = notifier
        self._repeated_errors = 0
        self._logger = get_logger("monitoring.health")

    @property
    def repeated_errors(self) -> int:
        return self._repeated_errors

    def record_error(self) -> HealthCheckResult:
        """Increment and return repeated-error health status."""

        self._repeated_errors += 1
        return self.check_repeated_errors()

    def clear_errors(self) -> None:
        """Reset repeated-error tracking after a healthy cycle."""

        self._repeated_errors = 0

    def check_ib_connection(self, ib_client: Any) -> HealthCheckResult:
        """Return IB connection health."""

        try:
            connected = bool(ib_client.isConnected())
        except Exception:
            self._logger.exception("IB connection health check failed.")
            return _check("ib_connection", "critical", "IB connection status check failed.")

        if not connected:
            return _check("ib_connection", "critical", "Interactive Brokers is disconnected.")

        return _check("ib_connection", "ok", "Interactive Brokers is connected.")

    def check_market_data_freshness(
        self,
        latest_market_data_ts: Any | None,
        *,
        now: datetime | None = None,
    ) -> HealthCheckResult:
        """Return whether the latest known market data timestamp is fresh."""

        if latest_market_data_ts is None:
            return _check("market_data_freshness", "critical", "No market data timestamp is available.")

        age = _now(now) - _timestamp(latest_market_data_ts, "latest_market_data_ts")
        if age > self.market_data_max_age:
            return _check(
                "market_data_freshness",
                "critical",
                f"Market data is stale. age_seconds={int(age.total_seconds())}.",
            )

        return _check("market_data_freshness", "ok", "Market data is fresh.")

    def check_last_processed_candle_age(
        self,
        last_processed_candle_ts: Any | None,
        *,
        now: datetime | None = None,
    ) -> HealthCheckResult:
        """Return whether candle processing appears stalled."""

        if last_processed_candle_ts is None:
            return _check("last_processed_candle", "warning", "No processed candle timestamp is available.")

        age = _now(now) - _timestamp(last_processed_candle_ts, "last_processed_candle_ts")
        if age > self.last_processed_candle_max_age:
            return _check(
                "last_processed_candle",
                "critical",
                f"Last processed candle is too old. age_seconds={int(age.total_seconds())}.",
            )

        return _check("last_processed_candle", "ok", "Last processed candle is recent.")

    def check_repeated_errors(self) -> HealthCheckResult:
        """Return repeated-error health."""

        if self._repeated_errors >= self.repeated_error_limit:
            return _check(
                "repeated_errors",
                "critical",
                f"Repeated error limit reached. count={self._repeated_errors}.",
            )

        if self._repeated_errors > 0:
            return _check("repeated_errors", "warning", f"Recent errors observed. count={self._repeated_errors}.")

        return _check("repeated_errors", "ok", "No repeated errors.")

    def run_checks(
        self,
        *,
        ib_client: Any | None = None,
        latest_market_data_ts: Any | None = None,
        last_processed_candle_ts: Any | None = None,
    ) -> HealthReport:
        """Run available checks and return a report."""

        checks: list[HealthCheckResult] = []
        if ib_client is not None:
            checks.append(self.check_ib_connection(ib_client))
        checks.append(self.check_market_data_freshness(latest_market_data_ts))
        checks.append(self.check_last_processed_candle_age(last_processed_candle_ts))
        checks.append(self.check_repeated_errors())

        report = HealthReport(checked_at=datetime.now(UTC).isoformat(), checks=tuple(checks))
        self._logger.info("Health report completed. level=%s checks=%s", report.level, len(report.checks))
        return report

    def send_heartbeat(self, *, message: str | None = None) -> None:
        """Send a heartbeat through the configured notifier when available."""

        if self.notifier is None:
            return

        method = getattr(self.notifier, "send_heartbeat", None)
        if callable(method):
            method(message or "Heartbeat")


def _check(name: str, level: HealthLevel, message: str) -> HealthCheckResult:
    return HealthCheckResult(
        name=name,
        level=level,
        message=message,
        checked_at=datetime.now(UTC).isoformat(),
    )


def _timestamp(value: Any, name: str) -> datetime:
    if isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, str):
        try:
            timestamp = datetime.fromisoformat(value)
        except ValueError as exc:
            raise HealthMonitorError(f"{name} must be an ISO timestamp.") from exc
    else:
        try:
            timestamp = value.to_pydatetime()
        except AttributeError as exc:
            raise HealthMonitorError(f"{name} must be a datetime-like value.") from exc

    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=UTC)

    return timestamp.astimezone(UTC)


def _now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
