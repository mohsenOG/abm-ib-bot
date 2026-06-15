"""Main runner for the Interactive Brokers gold bot."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from typing import Any

from config.settings import AppSettings, load_settings
from data.candle_store import CandleStore
from data.market_data import MarketDataClient
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
from strategy.bias_model import calculate_bias
from strategy.indicators import add_indicators
from strategy.signals import Signal, generate_signal
from trade_journal.journal import TradeJournal


class BotRunnerError(RuntimeError):
    """Raised when the main runner cannot continue safely."""


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
        self.emergency_stop = EmergencyStop(self.state_store, notifier=self.notifier, journal=self.journal)
        self.ib_connection = IBConnection(self.settings)

        _validate_mode_startup(self.settings)

    async def run(self, *, once: bool = False) -> None:
        """Start the bot runner."""

        state = self.state_store.load()
        self.logger.info("Bot starting. mode=%s once=%s", self.settings.trading.mode, once)
        _safe_notify(self.notifier, "send_startup", f"Bot started. mode={self.settings.trading.mode}")

        try:
            ib = await self.ib_connection.connect()
            _safe_notify(self.notifier, "send_ib_connected")

            await self._run_account_guard(ib=ib, state=state)
            signal_contract = await qualify_contract(ib, build_signal_contract(self.settings))

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
                    if error_check.level == "critical":
                        self.emergency_stop.activate("Repeated bot cycle errors reached the configured limit.", state=state)
                    raise

                if once:
                    return

                await asyncio.sleep(self.settings.runtime.poll_seconds)
        finally:
            if monitor_task is not None:
                monitor_task.cancel()
                with suppress(asyncio.CancelledError):
                    await monitor_task

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
        candles = await MarketDataClient(ib, settings=self.settings.market_data).fetch_historical_bars(signal_contract)
        candle_store = CandleStore(
            latest_processed_candle_ts=state.last_processed_candle_ts,
            bar_size=self.settings.market_data.bar_size,
        )
        update = candle_store.update(candles)

        self.logger.info(
            "Candle store updated. rows_received=%s rows_stored=%s latest_closed=%s gaps=%s",
            update.rows_received,
            update.rows_stored,
            update.latest_closed_candle_ts,
            len(update.gaps),
        )

        if update.latest_closed_candle_ts is None:
            self._run_health_checks(ib=ib, state=state, latest_market_data_ts=None)
            return state

        if not candle_store.has_new_closed_candle():
            self.logger.info("No new closed candle to process.")
            self._run_health_checks(ib=ib, state=state, latest_market_data_ts=update.latest_closed_candle_ts)
            return state

        signal = self._calculate_latest_signal(candle_store, state)
        latest_closed_ts = candle_store.latest_closed_candle_ts

        if latest_closed_ts is not None:
            candle_store.mark_processed(latest_closed_ts)
            state.last_processed_candle_ts = latest_closed_ts.isoformat()

        if signal is None:
            self.logger.info("No signal on latest closed candle.")
            self.state_store.save(state)
            self._run_health_checks(ib=ib, state=state, latest_market_data_ts=latest_closed_ts)
            return state

        self._record_signal(signal)

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

    def _calculate_latest_signal(self, candle_store: CandleStore, state: BotState) -> Signal | None:
        indicators = add_indicators(
            candle_store.get_candles(),
            use_heikin_ashi=self.settings.strategy.use_heikin_ashi,
            atr_period=self.settings.strategy.atr_length,
        )
        biased_data = calculate_bias(indicators)
        return generate_signal(
            biased_data,
            self.settings.strategy.bias_threshold,
            last_signal_id=state.last_signal_id,
            underlying_symbol=self.settings.signal_instrument.symbol,
            atr_length=self.settings.strategy.atr_length,
            sl_atr_mult=self.settings.strategy.sl_atr_mult,
            tp_atr_mult=self.settings.strategy.tp_atr_mult,
        )

    def _record_signal(self, signal: Signal) -> None:
        self.logger.info("Signal generated. signal_id=%s side=%s price=%s", signal.signal_id, signal.side, signal.price)
        self.journal.record(
            "signal",
            timestamp=signal.timestamp,
            signal_id=signal.signal_id,
            side=signal.side,
            price=signal.price,
            raw_json=asdict(signal),
        )
        _safe_notify(
            self.notifier,
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
            _safe_notify(self.notifier, "send_risk_blocked", signal_id=signal.signal_id, reason=reason)
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
            _safe_notify(self.notifier, "send_risk_blocked", signal_id=signal.signal_id, reason=reason)
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

    def _handle_critical_error(self, exc: Exception) -> None:
        self.logger.exception("Critical bot error.")
        self.journal.record("critical_error", reason=str(exc), raw_json={"error_type": type(exc).__name__})
        _safe_notify(self.notifier, "send_critical_error", details=str(exc))

    def _shutdown(self) -> None:
        try:
            if self.ib_connection.is_connected():
                self.ib_connection.disconnect()
                _safe_notify(self.notifier, "send_ib_disconnected")
        finally:
            _safe_notify(self.notifier, "send_shutdown", "Bot stopped")
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


def _safe_notify(notifier: Any, method_name: str, *args: Any, **kwargs: Any) -> None:
    method = getattr(notifier, method_name, None)
    if callable(method):
        method(*args, **kwargs)


if __name__ == "__main__":
    raise SystemExit(main())
