"""Main runner for the Interactive Brokers gold bot."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd

from config.settings import AppSettings, load_settings
from data.candle_store import CandleStore
from data.market_calendar import MarketCalendar, MarketCalendarError
from data.market_data import HistoricalDataRequest, MarketDataClient
from domain.constants import (
    ALERT_ONLY_MODE,
    EXECUTION_SIDE_LONG,
    EXECUTION_SIDE_SHORT,
    LIVE_MODE,
    PAPER_MODE,
    PROTECTED_TRADING_MODES,
)
from execution.order_manager import OrderManager
from execution.product_selector import ProductSelectionError, ProductSelector
from ib_gateway.account import AccountReader
from ib_gateway.connection import IBConnection
from ib_gateway.contracts import build_execution_product_contract, build_signal_contract, qualify_contract
from logging_setup.logger import get_logger, setup_logging
from monitoring.account_guard import AccountGuard, AccountGuardError, configured_account_id
from monitoring.active_trade_monitor import ActiveTradeMonitor
from monitoring.emergency_stop import EmergencyStop
from monitoring.health import HealthMonitor, HealthReport
from monitoring.live_mode import LiveModeGate, LiveModeGateError
from monitoring.reconciliation import AccountReconciliationError, AccountReconciliationGate
from notifications.notifier import TelegramNotifier
from risk.risk_manager import RiskManager, TradePlan
from state.state_store import BotState, StateStore
from strategy.bias_model import BiasModelError, calculate_bias
from strategy.indicators import IndicatorError, add_indicators, required_indicator_warmup_bars
from strategy.signals import Signal, SignalEngineError, generate_signal
from trade_journal.journal import TradeJournal


class BotRunnerError(RuntimeError):
    """Raised when the main runner cannot continue safely."""


class StrategySignalNotReadyError(RuntimeError):
    """Raised when candle history is not ready for safe signal calculation."""


class NotificationDeliveryError(RuntimeError):
    """Raised when a required notification cannot be delivered."""


@dataclass(frozen=True)
class RuntimeRecovery:
    ib: Any
    signal_contract: Any
    market_calendar: MarketCalendar
    state: BotState


@dataclass(frozen=True)
class CandleGapImpactValidation:
    validation: Any
    blocking_unexpected_data_gaps: tuple[Any, ...]
    degraded_unexpected_data_gaps: tuple[Any, ...]
    repair_attempted_count: int = 0
    repair_rows_received: int = 0

    @property
    def expected_session_gaps(self) -> tuple[Any, ...]:
        return self.validation.expected_session_gaps

    @property
    def unexpected_data_gaps(self) -> tuple[Any, ...]:
        return self.validation.unexpected_data_gaps

    @property
    def signal_processing_allowed(self) -> bool:
        return not self.blocking_unexpected_data_gaps

    @property
    def data_quality(self) -> str:
        return "degraded" if self.degraded_unexpected_data_gaps else "normal"


MAX_SESSION_CANDLES = 1000
CRITICAL_NOTIFICATION_METHODS = frozenset(
    {
        "send_startup",
        "send_order_submitted",
        "send_fill",
        "send_emergency_stop",
        "send_critical_error",
    }
)


def main() -> int:
    args = _parse_args()

    if args.command == "run":
        runner = BotRunner(settings_file=args.settings, env_file=args.env_file)
        asyncio.run(runner.run(once=args.once))
        return 0

    raise BotRunnerError(f"Unsupported command: {args.command}")


class BotRunner:
    """Coordinate config, IB, data, strategy, risk, execution, state, and alerts."""

    def __init__(
        self,
        *,
        settings_file: str | Path | None = None,
        env_file: str | Path | None = None,
    ) -> None:
        self.settings = load_settings(settings_file=settings_file, env_file=env_file)
        _validate_mode_startup(self.settings)
        self.logger = setup_logging(self.settings)
        self.notifier = TelegramNotifier(self.settings, logger=get_logger("notifications.telegram"))
        self.state_store = StateStore(self.settings.paths.state_file)
        self.journal = TradeJournal(journal_file=self.settings.paths.trade_journal_file)
        self.health_monitor = HealthMonitor(
            market_data_max_age=timedelta(seconds=self.settings.health.market_data_max_age_seconds),
            last_processed_candle_max_age=timedelta(
                seconds=self.settings.health.last_processed_candle_max_age_seconds
            ),
            repeated_error_limit=self.settings.health.repeated_error_limit,
            notifier=self.notifier,
        )
        self.emergency_stop = EmergencyStop(
            self.state_store,
            notifier=self.notifier,
            journal=self.journal,
            settings=self.settings,
        )
        self.ib_connection = IBConnection(self.settings)
        self._candle_store: CandleStore | None = None
        self._market_calendar: MarketCalendar | None = None
        self._market_calendar_refresh_date = None

    async def run(self, *, once: bool = False) -> None:
        """Start the bot runner."""

        state = self.state_store.load()
        self.logger.info("Bot starting. mode=%s once=%s", self.settings.trading.mode, once)
        await self._notify("send_startup", f"Bot started. mode={self.settings.trading.mode}", critical=True)

        try:
            ib = await self.ib_connection.connect()
            await self._notify("send_ib_connected")

            await self._run_account_guard(ib=ib, state=state)
            signal_contract = await qualify_contract(ib, build_signal_contract(self.settings))
            await self._refresh_signal_market_calendar(
                ib=ib,
                signal_contract=signal_contract,
                reason="startup",
            )

            if self.settings.trading.mode == LIVE_MODE:
                await self._run_live_preflight_and_stop(ib=ib, signal_contract=signal_contract, state=state)
                return

            await self._run_loop(ib=ib, signal_contract=signal_contract, state=state, once=once)
        except KeyboardInterrupt:
            self.logger.info("Bot interrupted by user.")
        except Exception as exc:
            self._handle_critical_error(exc)
            raise
        finally:
            self._shutdown()

    async def _run_loop(self, *, ib: Any, signal_contract: Any, state: BotState, once: bool) -> None:
        monitor_task = self._create_active_trade_monitor_task(ib=ib, once=once)
        try:
            while True:
                state = self.state_store.load()
                try:
                    state = await self._run_cycle(ib=ib, signal_contract=signal_contract, state=state)
                    self.health_monitor.clear_errors()
                except Exception:
                    error_check = self.health_monitor.record_error()
                    self.logger.exception("Bot cycle failed. repeated_errors=%s", self.health_monitor.repeated_errors)
                    await self._notify(
                        "send_critical_error",
                        message="Runtime recovery started",
                        details=(
                            f"Bot cycle failed; signal processing is paused. "
                            f"repeated_errors={self.health_monitor.repeated_errors} level={error_check.level}"
                        ),
                    )
                    monitor_task = await self._cancel_active_trade_monitor_task(monitor_task)

                    try:
                        recovery = await self._recover_runtime(state=state, cause=error_check.message)
                    except Exception as recovery_exc:
                        reason = f"Runtime recovery failed closed: {recovery_exc}"
                        self.logger.exception("Runtime recovery failed closed.")
                        self.emergency_stop.activate(reason, state=state)
                        raise BotRunnerError(reason) from recovery_exc

                    ib = recovery.ib
                    signal_contract = recovery.signal_contract
                    state = recovery.state
                    self.health_monitor.clear_errors()
                    monitor_task = self._create_active_trade_monitor_task(ib=ib, once=once)
                    await self._notify(
                        "send_critical_error",
                        message="Runtime recovery completed",
                        details="IB session, account guard, reconciliation, contracts, and market data refreshed.",
                    )

                if once:
                    return

                await asyncio.sleep(self.settings.runtime.poll_seconds)
        finally:
            await self._cancel_active_trade_monitor_task(monitor_task)

    async def _cancel_active_trade_monitor_task(self, monitor_task: asyncio.Task[Any] | None) -> None:
        if monitor_task is None:
            return None

        monitor_task.cancel()
        with suppress(asyncio.CancelledError):
            await monitor_task
        return None

    async def _recover_runtime(self, *, state: BotState, cause: str) -> RuntimeRecovery:
        max_attempts = max(1, self.health_monitor.repeated_error_limit)
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                delay_seconds = min(60.0, float(2 ** (attempt - 2)))
                self.logger.warning(
                    "Runtime recovery backing off. attempt=%s/%s delay_seconds=%s",
                    attempt,
                    max_attempts,
                    delay_seconds,
                )
                await asyncio.sleep(delay_seconds)

            await self._notify(
                "send_critical_error",
                message="Runtime recovery attempt",
                details=f"attempt={attempt}/{max_attempts}; cause={cause}",
            )

            try:
                ib = await self.ib_connection.reconnect()
                await self._notify("send_ib_connected", "IB reconnected during runtime recovery")

                recovered_state = self.state_store.load()
                await self._run_recovery_account_checks(ib=ib, state=recovered_state)
                signal_contract = await qualify_contract(ib, build_signal_contract(self.settings))
                market_calendar = await self._refresh_signal_market_calendar(
                    ib=ib,
                    signal_contract=signal_contract,
                    reason="runtime_recovery",
                )
                await self._requalify_active_product_contract(ib=ib, state=recovered_state)
                latest_market_data_ts = await self._refresh_recovery_candle_delta(
                    ib=ib,
                    signal_contract=signal_contract,
                    market_calendar=market_calendar,
                    state=recovered_state,
                )
                health_report = self._run_health_checks(
                    ib=ib,
                    state=recovered_state,
                    latest_market_data_ts=latest_market_data_ts,
                )
                if health_report.critical:
                    raise BotRunnerError(f"Runtime recovery health check failed: {health_report.level}.")
            except Exception as exc:
                last_error = exc
                self.logger.exception(
                    "Runtime recovery attempt failed. attempt=%s/%s",
                    attempt,
                    max_attempts,
                )
                await self._notify(
                    "send_critical_error",
                    message="Runtime recovery attempt failed",
                    details=f"attempt={attempt}/{max_attempts}; error={exc}",
                )
                continue

            self.logger.info("Runtime recovery completed. attempt=%s/%s", attempt, max_attempts)
            return RuntimeRecovery(
                ib=ib,
                signal_contract=signal_contract,
                market_calendar=market_calendar,
                state=recovered_state,
            )

        raise BotRunnerError(f"Runtime recovery exhausted {max_attempts} attempt(s): {last_error}") from last_error

    async def _run_recovery_account_checks(self, *, ib: Any, state: BotState) -> None:
        account_snapshot = await AccountReader(ib, client_id=self.settings.ib.client_id).read_snapshot()
        try:
            AccountGuard(self.settings).run_startup_checks(account_snapshot=account_snapshot)
            AccountReconciliationGate().run_startup_checks(account_snapshot=account_snapshot, state=state)
        except (AccountGuardError, AccountReconciliationError) as exc:
            raise BotRunnerError(f"IB account recovery safety check failed: {exc}.") from exc

    async def _requalify_active_product_contract(self, *, ib: Any, state: BotState) -> Any | None:
        active_trade = state.active_trade
        if not active_trade or active_trade.get("product_con_id") is None:
            return None

        product = SimpleNamespace(
            sec_type=_required_active_trade_text(active_trade, "product_asset_class"),
            con_id=_required_active_trade_int(active_trade, "product_con_id"),
            exchange=_required_active_trade_text(active_trade, "product_exchange"),
            currency=_required_active_trade_text(active_trade, "product_currency"),
        )
        return await qualify_contract(ib, build_execution_product_contract(product))

    async def _refresh_recovery_candle_delta(
        self,
        *,
        ib: Any,
        signal_contract: Any,
        market_calendar: MarketCalendar,
        state: BotState,
    ) -> Any | None:
        delta_duration = _runtime_delta_duration(self.settings)
        delta_settings = _market_data_settings_with_duration(self.settings.market_data, delta_duration)
        candles = await MarketDataClient(ib, settings=delta_settings).fetch_historical_bars(
            signal_contract,
            request=HistoricalDataRequest(duration_str=delta_duration),
        )
        candle_store = CandleStore(
            latest_processed_candle_ts=state.last_processed_candle_ts,
            bar_size=self.settings.market_data.bar_size,
            candle_close_buffer_seconds=self.settings.market_data.candle_close_buffer_seconds,
        )
        update = candle_store.update(candles)
        gap_impact = await self._repair_and_validate_candle_gaps(
            ib=ib,
            signal_contract=signal_contract,
            candle_store=candle_store,
            gaps=update.gaps,
            market_calendar=market_calendar,
        )
        self._log_candle_gap_impact_validation(gap_impact, market_calendar=market_calendar, candle_store=candle_store)
        if not gap_impact.signal_processing_allowed:
            details = _format_missing_bars(gap_impact.blocking_unexpected_data_gaps)
            raise BotRunnerError(f"Runtime recovery candle refresh has unexpected open-session gaps: {details}")
        return candle_store.latest_closed_candle_ts

    def _create_active_trade_monitor_task(self, *, ib: Any, once: bool) -> asyncio.Task[Any] | None:
        if once or self.settings.trading.mode != PAPER_MODE:
            return None

        monitor = ActiveTradeMonitor(
            self.settings,
            ib,
            state_store=self.state_store,
            notifier=self.notifier,
            emergency_stop=self.emergency_stop,
        )
        return asyncio.create_task(monitor.run_forever(), name="active-trade-monitor")

    async def _run_cycle(self, *, ib: Any, signal_contract: Any, state: BotState) -> BotState:
        market_calendar = await self._refresh_signal_market_calendar_if_due(
            ib=ib,
            signal_contract=signal_contract,
        )
        candle_store, update, request_scope = await self._fetch_and_update_candle_store(
            ib=ib,
            signal_contract=signal_contract,
            state=state,
        )

        self.logger.info(
            "Candle store updated. request_scope=%s rows_received=%s rows_stored=%s latest_closed=%s raw_gaps=%s dropped_unfinished=%s",
            request_scope,
            update.rows_received,
            update.rows_stored,
            update.latest_closed_candle_ts,
            len(update.gaps),
            update.rows_dropped_unfinished,
        )

        if update.latest_closed_candle_ts is None:
            self._run_health_checks(ib=ib, state=state, latest_market_data_ts=None)
            return state

        gap_impact = await self._repair_and_validate_candle_gaps(
            ib=ib,
            signal_contract=signal_contract,
            candle_store=candle_store,
            gaps=update.gaps,
            market_calendar=market_calendar,
        )
        self._log_candle_gap_impact_validation(gap_impact, market_calendar=market_calendar, candle_store=candle_store)
        latest_market_data_ts = candle_store.latest_closed_candle_ts
        if not gap_impact.signal_processing_allowed:
            gap_summary = _format_missing_bars(gap_impact.blocking_unexpected_data_gaps)
            await self._notify(
                "send_critical_error",
                message="Unexpected open-session candle gap blocked signal processing",
                details=gap_summary,
            )
            self._run_health_checks(ib=ib, state=state, latest_market_data_ts=latest_market_data_ts)
            return state

        if not candle_store.has_new_closed_candle():
            self.logger.info("No new closed candle to process.")
            self._run_health_checks(ib=ib, state=state, latest_market_data_ts=latest_market_data_ts)
            return state

        try:
            signal = self._calculate_latest_signal(candle_store, state)
        except StrategySignalNotReadyError as exc:
            latest_closed_ts = candle_store.latest_closed_candle_ts
            self.logger.warning("Signal processing blocked until warmup is complete. reason=%s", exc)
            await self._notify(
                "send_critical_error",
                message="Strategy warmup blocked signal processing",
                details=str(exc),
            )
            if latest_closed_ts is not None:
                candle_store.mark_processed(latest_closed_ts)
                state.last_processed_candle_ts = latest_closed_ts.isoformat()
            self.state_store.save(state)
            self._run_health_checks(ib=ib, state=state, latest_market_data_ts=latest_closed_ts)
            return state
        latest_closed_ts = candle_store.latest_closed_candle_ts

        if latest_closed_ts is not None:
            candle_store.mark_processed(latest_closed_ts)
            state.last_processed_candle_ts = latest_closed_ts.isoformat()

        if signal is None:
            self.logger.info("No signal on latest closed candle.")
            self.state_store.save(state)
            self._run_health_checks(ib=ib, state=state, latest_market_data_ts=latest_closed_ts)
            return state

        await self._record_signal(signal)

        if self.settings.trading.mode == ALERT_ONLY_MODE:
            state.last_signal_id = signal.signal_id
            self.state_store.save(state)
            self._run_health_checks(ib=ib, state=state, latest_market_data_ts=latest_closed_ts)
            return state

        if self.settings.trading.mode == PAPER_MODE:
            state = await self._handle_paper_signal(ib=ib, signal=signal, state=state)
            self._run_health_checks(ib=ib, state=state, latest_market_data_ts=latest_closed_ts)
            return state

        raise BotRunnerError(f"Unsupported trading mode during cycle: {self.settings.trading.mode}")

    async def _fetch_and_update_candle_store(
        self,
        *,
        ib: Any,
        signal_contract: Any,
        state: BotState,
    ) -> tuple[CandleStore, Any, str]:
        if getattr(self, "_candle_store", None) is None:
            candles = await MarketDataClient(ib, settings=self.settings.market_data).fetch_historical_bars(
                signal_contract
            )
            self._candle_store = CandleStore(
                latest_processed_candle_ts=state.last_processed_candle_ts,
                bar_size=self.settings.market_data.bar_size,
                candle_close_buffer_seconds=self.settings.market_data.candle_close_buffer_seconds,
            )
            update = self._candle_store.update(candles)
            self._candle_store.trim_to_latest(MAX_SESSION_CANDLES)
            return self._candle_store, update, "warmup"

        delta_duration = _runtime_delta_duration(self.settings)
        delta_settings = _market_data_settings_with_duration(self.settings.market_data, delta_duration)
        candles = await MarketDataClient(ib, settings=delta_settings).fetch_historical_bars(
            signal_contract,
            request=HistoricalDataRequest(duration_str=delta_duration),
        )
        update = self._candle_store.update(candles)
        self._candle_store.trim_to_latest(MAX_SESSION_CANDLES)
        return self._candle_store, update, "delta"

    async def _refresh_signal_market_calendar_if_due(self, *, ib: Any, signal_contract: Any) -> MarketCalendar:
        market_calendar = self._market_calendar
        refresh_date = self._market_calendar_refresh_date
        today_utc = datetime.now(timezone.utc).date()
        if market_calendar is not None and refresh_date == today_utc:
            return market_calendar

        reason = "daily_refresh" if market_calendar is not None else "initial_refresh"
        return await self._refresh_signal_market_calendar(
            ib=ib,
            signal_contract=signal_contract,
            reason=reason,
        )

    async def _refresh_signal_market_calendar(
        self,
        *,
        ib: Any,
        signal_contract: Any,
        reason: str,
    ) -> MarketCalendar:
        try:
            contract_details = await ib.reqContractDetailsAsync(signal_contract)
        except Exception as exc:
            self.logger.exception("Failed to fetch XAUUSD IBKR ContractDetails for market calendar.")
            raise BotRunnerError("Signal processing blocked: could not fetch XAUUSD IBKR market calendar.") from exc

        try:
            contract_details_count = len(contract_details)
        except TypeError as exc:
            self.logger.error("Invalid XAUUSD IBKR ContractDetails response for market calendar.")
            raise BotRunnerError("Signal processing blocked: XAUUSD IBKR market calendar response was invalid.") from exc

        if contract_details_count != 1:
            self.logger.error(
                "Invalid XAUUSD IBKR ContractDetails result count for market calendar. count=%s",
                contract_details_count,
            )
            raise BotRunnerError("Signal processing blocked: XAUUSD IBKR market calendar result was not unique.")

        try:
            market_calendar = MarketCalendar.from_contract_details(contract_details[0])
        except MarketCalendarError as exc:
            self.logger.error("Invalid XAUUSD IBKR market calendar. error=%s", exc)
            raise BotRunnerError(f"Signal processing blocked: invalid XAUUSD IBKR market calendar: {exc}") from exc

        self._market_calendar = market_calendar
        self._market_calendar_refresh_date = market_calendar.refreshed_at_utc.date()
        self.logger.info(
            "XAUUSD IBKR market calendar refreshed. reason=%s time_zone_id=%s trading_sessions=%s liquid_sessions=%s refreshed_at_utc=%s",
            reason,
            market_calendar.time_zone_id,
            len(market_calendar.trading_sessions),
            len(market_calendar.liquid_sessions),
            market_calendar.refreshed_at_utc,
        )
        self.logger.info(
            "XAUUSD IBKR upcoming opening hours. reason=%s time_zone_id=%s table_utc=\n%s",
            reason,
            market_calendar.time_zone_id,
            market_calendar.format_opening_hours_table(days=5),
        )
        return market_calendar

    def _validate_candle_gaps_with_calendar(
        self,
        *,
        candle_store: CandleStore,
        gaps: tuple[Any, ...],
        market_calendar: MarketCalendar | None,
    ) -> Any:
        if market_calendar is None:
            self.logger.error("Candle gap validation failed: XAUUSD IBKR market calendar is unavailable.")
            raise BotRunnerError("Signal processing blocked: XAUUSD IBKR market calendar is unavailable.")
        return market_calendar.validate_candle_gaps(gaps, candle_store.candle_interval)

    async def _repair_and_validate_candle_gaps(
        self,
        *,
        ib: Any,
        signal_contract: Any,
        candle_store: CandleStore,
        gaps: tuple[Any, ...],
        market_calendar: MarketCalendar | None,
    ) -> CandleGapImpactValidation:
        validation = self._validate_candle_gaps_with_calendar(
            candle_store=candle_store,
            gaps=gaps,
            market_calendar=market_calendar,
        )
        if not validation.unexpected_data_gaps:
            return self._classify_candle_gap_impact(
                validation=validation,
                candle_store=candle_store,
                market_calendar=market_calendar,
            )

        repaired_validation, attempted_count, rows_received = await self._attempt_unexpected_gap_backfills(
            ib=ib,
            signal_contract=signal_contract,
            candle_store=candle_store,
            unexpected_data_gaps=validation.unexpected_data_gaps,
            market_calendar=market_calendar,
        )
        return self._classify_candle_gap_impact(
            validation=repaired_validation,
            candle_store=candle_store,
            market_calendar=market_calendar,
            repair_attempted_count=attempted_count,
            repair_rows_received=rows_received,
        )

    async def _attempt_unexpected_gap_backfills(
        self,
        *,
        ib: Any,
        signal_contract: Any,
        candle_store: CandleStore,
        unexpected_data_gaps: tuple[Any, ...],
        market_calendar: MarketCalendar | None,
    ) -> tuple[Any, int, int]:
        backfill_duration = self.settings.market_data.gap_backfill_duration
        missing_bars = _unique_missing_bars(unexpected_data_gaps)
        attempted_count = 0
        rows_received = 0
        client = MarketDataClient(ib, settings=self.settings.market_data)

        for missing_bar in missing_bars:
            missing_timestamp = _missing_bar_timestamp(missing_bar)
            end_timestamp = _centered_backfill_end_timestamp(
                missing_timestamp=missing_timestamp,
                duration=backfill_duration,
                latest_closed_candle_ts=candle_store.latest_closed_candle_ts,
                candle_interval=candle_store.candle_interval,
            )
            end_datetime = _format_ib_end_datetime(end_timestamp)
            self.logger.warning(
                "Attempting targeted IBKR candle gap backfill. missing_timestamp=%s duration=%s end_datetime=%s",
                missing_timestamp,
                backfill_duration,
                end_datetime,
            )

            attempted_count += 1
            try:
                candles = await client.fetch_historical_bars(
                    signal_contract,
                    request=HistoricalDataRequest(end_datetime=end_datetime, duration_str=backfill_duration),
                )
            except Exception:
                self.logger.exception(
                    "Targeted IBKR candle gap backfill failed. missing_timestamp=%s duration=%s end_datetime=%s",
                    missing_timestamp,
                    backfill_duration,
                    end_datetime,
                )
                continue

            rows_received += len(candles)
            candle_store.update(candles)

        candle_store.trim_to_latest(MAX_SESSION_CANDLES)
        repaired_gaps = candle_store.detect_missing_candles()
        repaired_validation = self._validate_candle_gaps_with_calendar(
            candle_store=candle_store,
            gaps=repaired_gaps,
            market_calendar=market_calendar,
        )
        remaining_missing = {
            _missing_bar_timestamp(missing_bar) for missing_bar in repaired_validation.unexpected_data_gaps
        }
        filled_count = sum(
            1 for missing_bar in missing_bars if _missing_bar_timestamp(missing_bar) not in remaining_missing
        )
        self.logger.info(
            "Targeted IBKR candle gap backfill completed. attempted=%s rows_received=%s filled_missing_timestamps=%s remaining_unexpected_data_gaps=%s",
            attempted_count,
            rows_received,
            filled_count,
            len(repaired_validation.unexpected_data_gaps),
        )
        return repaired_validation, attempted_count, rows_received

    def _classify_candle_gap_impact(
        self,
        *,
        validation: Any,
        candle_store: CandleStore,
        market_calendar: MarketCalendar | None,
        repair_attempted_count: int = 0,
        repair_rows_received: int = 0,
    ) -> CandleGapImpactValidation:
        if market_calendar is None:
            raise BotRunnerError("Signal processing blocked: XAUUSD IBKR market calendar is unavailable.")

        latest_closed = candle_store.latest_closed_candle_ts
        blocking: list[Any] = []
        degraded: list[Any] = []
        for missing_bar in validation.unexpected_data_gaps:
            distance = _open_session_bars_from_latest(
                market_calendar=market_calendar,
                missing_bar=missing_bar,
                latest_closed_candle_ts=latest_closed,
                candle_interval=candle_store.candle_interval,
            )
            if distance <= self.settings.market_data.gap_block_recent_bars:
                blocking.append(missing_bar)
            else:
                degraded.append(missing_bar)

        return CandleGapImpactValidation(
            validation=validation,
            blocking_unexpected_data_gaps=tuple(blocking),
            degraded_unexpected_data_gaps=tuple(degraded),
            repair_attempted_count=repair_attempted_count,
            repair_rows_received=repair_rows_received,
        )

    def _log_candle_gap_impact_validation(
        self,
        impact: CandleGapImpactValidation,
        *,
        market_calendar: MarketCalendar,
        candle_store: CandleStore,
    ) -> None:
        self.logger.info(
            "Candle gap validation completed. expected_session_gaps=%s unexpected_data_gaps=%s blocking_unexpected_data_gaps=%s degraded_unexpected_data_gaps=%s signal_processing_allowed=%s data_quality=%s repair_attempted=%s repair_rows_received=%s",
            len(impact.expected_session_gaps),
            len(impact.unexpected_data_gaps),
            len(impact.blocking_unexpected_data_gaps),
            len(impact.degraded_unexpected_data_gaps),
            impact.signal_processing_allowed,
            impact.data_quality,
            impact.repair_attempted_count,
            impact.repair_rows_received,
        )
        for missing_bar in impact.blocking_unexpected_data_gaps:
            distance = _open_session_bars_from_latest(
                market_calendar=market_calendar,
                missing_bar=missing_bar,
                latest_closed_candle_ts=candle_store.latest_closed_candle_ts,
                candle_interval=candle_store.candle_interval,
            )
            self.logger.error(
                "Blocking unrepaired candle gap detail. previous_candle=%s next_candle=%s missing_timestamp=%s reason=%s open_session_bars_from_latest=%s",
                missing_bar.previous_timestamp,
                missing_bar.next_timestamp,
                missing_bar.missing_timestamp,
                missing_bar.reason,
                distance,
            )
        for missing_bar in impact.degraded_unexpected_data_gaps:
            distance = _open_session_bars_from_latest(
                market_calendar=market_calendar,
                missing_bar=missing_bar,
                latest_closed_candle_ts=candle_store.latest_closed_candle_ts,
                candle_interval=candle_store.candle_interval,
            )
            self.logger.warning(
                "Allowed unrepaired candle gap detail. data_quality=degraded previous_candle=%s next_candle=%s missing_timestamp=%s reason=%s open_session_bars_from_latest=%s",
                missing_bar.previous_timestamp,
                missing_bar.next_timestamp,
                missing_bar.missing_timestamp,
                missing_bar.reason,
                distance,
            )

    def _calculate_latest_signal(self, candle_store: CandleStore, state: BotState) -> Signal | None:
        candles = candle_store.get_candles()
        minimum_rows = required_indicator_warmup_bars(atr_period=self.settings.strategy.atr_length)
        if len(candles) < minimum_rows:
            raise StrategySignalNotReadyError(
                f"{len(candles)} closed bars available; {minimum_rows} required before signal calculation."
            )

        try:
            indicators = add_indicators(
                candles,
                use_heikin_ashi=self.settings.strategy.use_heikin_ashi,
                atr_period=self.settings.strategy.atr_length,
            )
            biased_data = calculate_bias(indicators, atr_period=self.settings.strategy.atr_length)
            return generate_signal(
                biased_data,
                self.settings.strategy.bias_threshold,
                last_signal_id=state.last_signal_id,
                underlying_symbol=self.settings.signal_instrument.symbol,
                atr_length=self.settings.strategy.atr_length,
                sl_atr_mult=self.settings.strategy.sl_atr_mult,
                tp_atr_mult=self.settings.strategy.tp_atr_mult,
            )
        except (IndicatorError, BiasModelError, SignalEngineError) as exc:
            raise StrategySignalNotReadyError(str(exc)) from exc

    async def _record_signal(self, signal: Signal) -> None:
        self.logger.info("Signal generated. signal_id=%s side=%s price=%s", signal.signal_id, signal.side, signal.price)
        self.journal.record(
            "signal",
            timestamp=signal.timestamp,
            signal_id=signal.signal_id,
            side=signal.side,
            price=signal.price,
            raw_json=asdict(signal),
        )
        await self._notify(
            "send_signal",
            signal_id=signal.signal_id,
            side=signal.side,
            price=signal.price,
            bias=signal.bias,
            confidence=signal.confidence,
            timestamp=signal.timestamp,
        )

    async def _handle_paper_signal(self, *, ib: Any, signal: Signal, state: BotState) -> BotState:
        self.emergency_stop.assert_trading_allowed(state)
        account_snapshot = await AccountReader(ib, client_id=self.settings.ib.client_id).read_snapshot()
        try:
            selected_product, execution_contract = await ProductSelector(self.settings, ib).select_for_signal(signal.side)
        except ProductSelectionError as exc:
            reason = str(exc)
            self.logger.info("Product selection blocked signal. signal_id=%s reason=%s", signal.signal_id, reason)
            self.journal.record("risk_blocked", signal_id=signal.signal_id, side=signal.side, reason=reason)
            await self._notify("send_risk_blocked", signal_id=signal.signal_id, reason=reason)
            state.last_signal_id = signal.signal_id
            self.state_store.save(state)
            return state

        risk_decision = RiskManager(self.settings).evaluate_signal(
            signal,
            account_snapshot,
            last_signal_id=state.last_signal_id,
            selected_product=selected_product,
            product_price=selected_product.ask,
        )

        if not risk_decision.approved:
            reason = risk_decision.reason or "Risk manager blocked the trade."
            self.logger.info("Risk blocked signal. signal_id=%s reason=%s", signal.signal_id, reason)
            self.journal.record("risk_blocked", signal_id=signal.signal_id, side=signal.side, reason=reason)
            await self._notify("send_risk_blocked", signal_id=signal.signal_id, reason=reason)
            state.last_signal_id = signal.signal_id
            self.state_store.save(state)
            return state

        trade_plan = _require_trade_plan(risk_decision.trade_plan)
        self.journal.record(
            "risk_approved",
            signal_id=trade_plan.signal_id,
            side=trade_plan.signal_side,
            quantity=trade_plan.quantity,
            price=trade_plan.signal_price,
            raw_json=asdict(trade_plan),
        )

        result = await OrderManager(
            self.settings,
            ib,
            state_store=self.state_store,
            journal=self.journal,
            notifier=self.notifier,
            emergency_stop=self.emergency_stop,
        ).submit_trade_plan(contract=execution_contract, trade_plan=trade_plan, state=state)

        updated_state = result.state if result.state is not None else state
        updated_state.last_signal_id = trade_plan.signal_id
        self.state_store.save(updated_state)
        return updated_state

    async def _run_live_preflight_and_stop(self, *, ib: Any, signal_contract: Any, state: BotState) -> None:
        candles = await MarketDataClient(ib, settings=self.settings.market_data).fetch_historical_bars(signal_contract)
        candle_store = CandleStore(
            candles,
            latest_processed_candle_ts=state.last_processed_candle_ts,
            bar_size=self.settings.market_data.bar_size,
            candle_close_buffer_seconds=self.settings.market_data.candle_close_buffer_seconds,
        )
        account_snapshot = await AccountReader(ib, client_id=self.settings.ib.client_id).read_snapshot()
        health_report = self._run_health_checks(
            ib=ib,
            state=state,
            latest_market_data_ts=candle_store.latest_closed_candle_ts,
        )

        LiveModeGate(self.settings, notifier=self.notifier).run_startup_checks(
            state=state,
            account_snapshot=account_snapshot,
            health_report=health_report,
        )
        raise LiveModeGateError("Live mode readiness checks passed, but live execution is not enabled in this task.")

    async def _run_account_guard(self, *, ib: Any, state: BotState) -> None:
        if self.settings.trading.mode not in PROTECTED_TRADING_MODES:
            return

        if self.settings.ib.client_id != 0:
            raise BotRunnerError(
                "IB client_id must be 0 in paper/live mode so manual TWS orders can be bound and reconciled."
            )

        account_snapshot = await AccountReader(ib, client_id=self.settings.ib.client_id).read_snapshot()
        try:
            AccountGuard(self.settings).run_startup_checks(account_snapshot=account_snapshot)
            AccountReconciliationGate().run_startup_checks(account_snapshot=account_snapshot, state=state)
        except (AccountGuardError, AccountReconciliationError) as exc:
            raise BotRunnerError(f"IB account safety check failed: {exc}.") from exc

    def _run_health_checks(
        self,
        *,
        ib: Any,
        state: BotState,
        latest_market_data_ts: Any | None,
    ) -> HealthReport:
        report = self.health_monitor.run_checks(
            ib_client=ib,
            latest_market_data_ts=latest_market_data_ts,
            last_processed_candle_ts=state.last_processed_candle_ts,
        )
        if report.critical:
            self.logger.error("Health check critical. level=%s", report.level)
        return report

    async def _notify(self, method_name: str, *args: Any, critical: bool = False, **kwargs: Any) -> None:
        await asyncio.to_thread(
            _safe_notify,
            self.notifier,
            method_name,
            *args,
            required=self._requires_notification_delivery(method_name, critical=critical),
            logger=self.logger,
            **kwargs,
        )

    def _notify_now(self, method_name: str, *args: Any, critical: bool = False, **kwargs: Any) -> None:
        _safe_notify(
            self.notifier,
            method_name,
            *args,
            required=self._requires_notification_delivery(method_name, critical=critical),
            logger=self.logger,
            **kwargs,
        )

    def _requires_notification_delivery(self, method_name: str, *, critical: bool = False) -> bool:
        telegram_settings = getattr(self.settings, "telegram", None)
        require_delivery = bool(getattr(telegram_settings, "require_critical_delivery", True))
        if not require_delivery:
            return False
        if self.settings.trading.mode not in PROTECTED_TRADING_MODES:
            return False
        return critical or method_name in CRITICAL_NOTIFICATION_METHODS

    def _handle_critical_error(self, exc: Exception) -> None:
        self.logger.exception("Critical bot error.")
        self.journal.record("critical_error", reason=str(exc), raw_json={"error_type": type(exc).__name__})
        with suppress(NotificationDeliveryError):
            self._notify_now("send_critical_error", details=str(exc), critical=True)

    def _shutdown(self) -> None:
        try:
            if self.ib_connection.is_connected():
                self.ib_connection.disconnect()
                self._notify_now("send_ib_disconnected")
        finally:
            self._notify_now("send_shutdown", "Bot stopped")
            self.logger.info("Bot stopped.")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Interactive Brokers gold trading bot.")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Start the bot runner.")
    run_parser.add_argument("--once", action="store_true", help="Run one closed-candle cycle and exit.")
    run_parser.add_argument("--settings", type=Path, default=None, help="Path to settings.yml.")
    run_parser.add_argument("--env-file", type=Path, default=None, help="Path to .env file.")

    parser.set_defaults(command="run")
    return parser.parse_args()


def _validate_mode_startup(settings: AppSettings) -> None:
    mode = settings.trading.mode
    if mode == ALERT_ONLY_MODE:
        return

    if mode == PAPER_MODE:
        _validate_configured_account_id(settings, mode)
        _validate_paper_execution_config(settings)
        return

    if mode == LIVE_MODE:
        _validate_configured_account_id(settings, mode)
        return

    raise BotRunnerError(f"Unsupported trading mode: {mode}")


def _validate_paper_execution_config(settings: AppSettings) -> None:
    if settings.trading.allowed_directions.long:
        _validate_configured_execution_products(settings, EXECUTION_SIDE_LONG)

    if settings.trading.allowed_directions.short:
        _validate_configured_execution_products(settings, EXECUTION_SIDE_SHORT)


def _validate_configured_execution_products(settings: AppSettings, side: str) -> None:
    products = getattr(settings.execution_products, side)
    enabled_products = [product for product in products if product.enabled]
    if not enabled_products:
        raise BotRunnerError(f"At least one enabled execution_products.{side} product is required in paper mode.")

    for product in enabled_products:
        build_execution_product_contract(product)


def _validate_configured_account_id(settings: AppSettings, mode: str) -> None:
    if configured_account_id(settings) is None:
        raise BotRunnerError(f"IB_ACCOUNT_ID is required when trading.mode is {mode}.")


def _require_trade_plan(value: TradePlan | None) -> TradePlan:
    if value is None:
        raise BotRunnerError("Risk decision was approved without a trade plan.")
    return value


def _required_active_trade_text(active_trade: dict[str, Any], name: str) -> str:
    value = active_trade.get(name)
    if not isinstance(value, str) or not value.strip():
        raise BotRunnerError(f"Active trade {name} is required for runtime recovery.")
    return value.strip()


def _required_active_trade_int(active_trade: dict[str, Any], name: str) -> int:
    value = active_trade.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BotRunnerError(f"Active trade {name} must be a positive integer for runtime recovery.")
    return value


def _unique_missing_bars(missing_bars: tuple[Any, ...]) -> tuple[Any, ...]:
    by_timestamp: dict[pd.Timestamp, Any] = {}
    for missing_bar in missing_bars:
        timestamp = _missing_bar_timestamp(missing_bar)
        by_timestamp.setdefault(timestamp, missing_bar)
    return tuple(by_timestamp[timestamp] for timestamp in sorted(by_timestamp))


def _missing_bar_timestamp(missing_bar: Any) -> pd.Timestamp:
    return _required_utc_timestamp(getattr(missing_bar, "missing_timestamp", None), "missing_timestamp")


def _open_session_bars_from_latest(
    *,
    market_calendar: MarketCalendar,
    missing_bar: Any,
    latest_closed_candle_ts: Any | None,
    candle_interval: pd.Timedelta,
) -> int:
    if latest_closed_candle_ts is None:
        return 0
    return market_calendar.count_open_session_bars_after(
        _missing_bar_timestamp(missing_bar),
        latest_closed_candle_ts,
        candle_interval,
    )


def _centered_backfill_end_timestamp(
    *,
    missing_timestamp: pd.Timestamp,
    duration: str,
    latest_closed_candle_ts: Any | None,
    candle_interval: pd.Timedelta,
) -> pd.Timestamp:
    duration_delta = _ib_duration_to_timedelta(duration, "market_data.gap_backfill_duration")
    centered_end = missing_timestamp + (duration_delta / 2)
    cap = _backfill_end_cap(latest_closed_candle_ts=latest_closed_candle_ts, candle_interval=candle_interval)
    return min(centered_end, cap)


def _backfill_end_cap(*, latest_closed_candle_ts: Any | None, candle_interval: pd.Timedelta) -> pd.Timestamp:
    now = pd.Timestamp(datetime.now(timezone.utc))
    if latest_closed_candle_ts is None:
        return now

    latest_closed = _required_utc_timestamp(latest_closed_candle_ts, "latest_closed_candle_ts")
    latest_closed_end = latest_closed + candle_interval
    return min(latest_closed_end, now)


def _format_ib_end_datetime(timestamp: pd.Timestamp) -> str:
    value = _required_utc_timestamp(timestamp, "end_datetime").floor("s")
    return f"{value:%Y%m%d %H:%M:%S} UTC"


def _ib_duration_to_timedelta(value: str, name: str) -> pd.Timedelta:
    parts = value.strip().split()
    if len(parts) != 2:
        raise BotRunnerError(f"{name} must use '<positive integer> <unit>', for example '1 D'.")

    quantity_text, unit_text = parts
    try:
        quantity = int(quantity_text)
    except ValueError as exc:
        raise BotRunnerError(f"{name} quantity must be a positive integer.") from exc

    if quantity <= 0:
        raise BotRunnerError(f"{name} quantity must be a positive integer.")

    unit = unit_text.upper()
    if unit == "S":
        return pd.Timedelta(seconds=quantity)
    if unit == "H":
        return pd.Timedelta(hours=quantity)
    if unit == "D":
        return pd.Timedelta(days=quantity)
    if unit == "W":
        return pd.Timedelta(weeks=quantity)

    raise BotRunnerError(f"{name} unit must be one of S, H, D, or W for centered gap backfill.")


def _required_utc_timestamp(value: Any, name: str) -> pd.Timestamp:
    try:
        timestamp = pd.Timestamp(value)
    except Exception as exc:
        raise BotRunnerError(f"{name} must be a valid timestamp.") from exc

    if pd.isna(timestamp):
        raise BotRunnerError(f"{name} must be a valid timestamp.")

    if timestamp.tzinfo is None:
        return timestamp.tz_localize(timezone.utc)
    return timestamp.tz_convert(timezone.utc)


def _format_missing_bars(missing_bars: tuple[Any, ...]) -> str:
    parts = []
    for missing_bar in missing_bars[:5]:
        parts.append(
            "previous={previous} next={next} missing_timestamp={missing_timestamp} reason={reason}".format(
                previous=getattr(missing_bar, "previous_timestamp", None),
                next=getattr(missing_bar, "next_timestamp", None),
                missing_timestamp=getattr(missing_bar, "missing_timestamp", None),
                reason=getattr(missing_bar, "reason", None),
            )
        )

    if len(missing_bars) > 5:
        parts.append(f"... {len(missing_bars) - 5} more missing bar(s)")

    return "; ".join(parts)


def _runtime_delta_duration(settings: Any) -> str:
    poll_seconds = int(getattr(getattr(settings, "runtime", None), "poll_seconds", 300) or 300)
    hours = max(6, int(poll_seconds // 3600) + 3)
    return f"{hours} H"


def _market_data_settings_with_duration(source: Any, duration: str) -> Any:
    if is_dataclass(source):
        return replace(source, historical_duration=duration)

    values = dict(vars(source))
    values["historical_duration"] = duration
    return SimpleNamespace(**values)


def _safe_notify(
    notifier: Any,
    method_name: str,
    *args: Any,
    required: bool = False,
    logger: Any | None = None,
    **kwargs: Any,
) -> None:
    method = getattr(notifier, method_name, None)
    if not callable(method):
        if required:
            raise NotificationDeliveryError(f"Required notification method is unavailable: {method_name}.")
        return

    try:
        result = method(*args, **kwargs)
    except Exception as exc:
        if logger is not None:
            logger.exception("Notification method failed. method=%s", method_name)
        if required:
            raise NotificationDeliveryError(f"Required notification failed: {method_name}: {exc}") from exc
        return

    attempted = bool(getattr(result, "attempted", False))
    success = bool(getattr(result, "success", True))
    failed_count = getattr(result, "failed_count", None)
    if not attempted:
        message = f"Required notification was not attempted. method={method_name}"
        if logger is not None:
            logger.error(message)
        if required:
            raise NotificationDeliveryError(message)
        return

    if success:
        return

    message = f"Notification delivery failed. method={method_name} failed_count={failed_count}"
    if logger is not None:
        logger.error(message)
    if required:
        raise NotificationDeliveryError(message)


if __name__ == "__main__":
    raise SystemExit(main())
