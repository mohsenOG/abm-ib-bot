"""Telegram notification sender."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


TELEGRAM_API_BASE_URL = "https://api.telegram.org"
DEFAULT_TIMEOUT_SECONDS = 10.0


class TelegramNotifierError(RuntimeError):
    """Raised when Telegram notification configuration is invalid."""


@dataclass(frozen=True)
class TelegramSendResult:
    """Summary of one notification send request."""

    attempted: bool
    success: bool
    delivered_count: int
    failed_count: int


class TelegramNotifier:
    """Send plain-text Telegram notifications to configured chat IDs."""

    def __init__(
        self,
        settings: Any | None = None,
        *,
        enabled: bool | None = None,
        bot_token: str | None = None,
        chat_ids: tuple[str, ...] | list[str] | None = None,
        max_retries: int | None = None,
        retry_delay_seconds: float | None = None,
        timeout_seconds: float | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        telegram_settings = getattr(settings, "telegram", settings)

        self.enabled = _resolve_setting(telegram_settings, "enabled", enabled, True)
        self.bot_token = _resolve_setting(telegram_settings, "bot_token", bot_token, None)
        self.chat_ids = tuple(_resolve_setting(telegram_settings, "chat_ids", chat_ids, ()) or ())
        self.max_retries = _resolve_setting(telegram_settings, "max_retries", max_retries, 3)
        self.retry_delay_seconds = float(
            _resolve_setting(telegram_settings, "retry_delay_seconds", retry_delay_seconds, 2.0)
        )
        self.timeout_seconds = float(
            _resolve_setting(telegram_settings, "timeout_seconds", timeout_seconds, DEFAULT_TIMEOUT_SECONDS)
        )
        self.logger = logger or logging.getLogger(__name__)

        self._validate()

    def send_startup(self, message: str = "Bot started") -> TelegramSendResult:
        return self.send_message(message)

    def send_shutdown(self, message: str = "Bot stopped") -> TelegramSendResult:
        return self.send_message(message)

    def send_heartbeat(self, message: str = "Heartbeat") -> TelegramSendResult:
        return self.send_message(message)

    def send_ib_connected(self, message: str = "IB connected") -> TelegramSendResult:
        return self.send_message(message)

    def send_ib_disconnected(self, message: str = "IB disconnected") -> TelegramSendResult:
        return self.send_message(message)

    def send_signal(
        self,
        *,
        signal_id: str | None = None,
        side: str | None = None,
        price: float | int | str | None = None,
        bias: float | int | str | None = None,
        confidence: float | int | str | None = None,
        timestamp: str | None = None,
    ) -> TelegramSendResult:
        return self.send_message(
            _format_message(
                "New signal",
                signal_id=signal_id,
                timestamp=timestamp,
                side=side,
                price=price,
                bias=bias,
                confidence=confidence,
            )
        )

    def send_risk_blocked(self, *, signal_id: str | None = None, reason: str | None = None) -> TelegramSendResult:
        return self.send_message(_format_message("Trade blocked", signal_id=signal_id, reason=reason))

    def send_order_submitted(
        self,
        *,
        order_id: int | str | None = None,
        side: str | None = None,
        quantity: float | int | str | None = None,
        price: float | int | str | None = None,
    ) -> TelegramSendResult:
        return self.send_message(
            _format_message(
                "Order submitted",
                order_id=order_id,
                side=side,
                quantity=quantity,
                price=price,
            )
        )

    def send_fill(
        self,
        *,
        order_id: int | str | None = None,
        perm_id: int | str | None = None,
        side: str | None = None,
        quantity: float | int | str | None = None,
        price: float | int | str | None = None,
    ) -> TelegramSendResult:
        return self.send_message(
            _format_message(
                "Order filled",
                order_id=order_id,
                perm_id=perm_id,
                side=side,
                quantity=quantity,
                price=price,
            )
        )

    def send_order_rejected(
        self,
        *,
        order_id: int | str | None = None,
        reason: str | None = None,
    ) -> TelegramSendResult:
        return self.send_message(_format_message("Order rejected", order_id=order_id, reason=reason))

    def send_order_cancelled(
        self,
        *,
        order_id: int | str | None = None,
        reason: str | None = None,
    ) -> TelegramSendResult:
        return self.send_message(_format_message("Order cancelled", order_id=order_id, reason=reason))

    def send_emergency_stop(self, *, reason: str | None = None) -> TelegramSendResult:
        return self.send_message(_format_message("Emergency stop activated", reason=reason))

    def send_critical_error(self, *, message: str = "Critical error", details: str | None = None) -> TelegramSendResult:
        return self.send_message(_format_message(message, details=details))

    def send_message(self, message: str) -> TelegramSendResult:
        """Send a plain-text Telegram message to all configured chats."""

        if not self.enabled:
            self.logger.debug("Telegram notifier is disabled; message was not sent.")
            return TelegramSendResult(attempted=False, success=True, delivered_count=0, failed_count=0)

        text = self._sanitize_message(message)
        delivered_count = 0
        failed_count = 0

        for chat_id in self.chat_ids:
            if self._send_to_chat(str(chat_id), text):
                delivered_count += 1
            else:
                failed_count += 1

        return TelegramSendResult(
            attempted=True,
            success=failed_count == 0,
            delivered_count=delivered_count,
            failed_count=failed_count,
        )

    def _send_to_chat(self, chat_id: str, message: str) -> bool:
        attempts = self.max_retries + 1

        for attempt_number in range(1, attempts + 1):
            try:
                self._post_send_message(chat_id, message)
                return True
            except TelegramNotifierError as exc:
                self.logger.warning(
                    "Telegram send failed on attempt %s/%s: %s",
                    attempt_number,
                    attempts,
                    exc,
                )

            if attempt_number < attempts and self.retry_delay_seconds > 0:
                time.sleep(self.retry_delay_seconds)

        self.logger.error("Telegram send failed after %s attempts.", attempts)
        return False

    def _post_send_message(self, chat_id: str, message: str) -> None:
        if self.bot_token is None:
            raise TelegramNotifierError("Telegram bot token is missing.")

        url = f"{TELEGRAM_API_BASE_URL}/bot{self.bot_token}/sendMessage"
        payload = urllib.parse.urlencode(
            {
                "chat_id": chat_id,
                "text": message,
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw_body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            reason = _extract_telegram_error(exc)
            raise TelegramNotifierError(f"Telegram API returned HTTP {exc.code}: {reason}") from exc
        except urllib.error.URLError as exc:
            raise TelegramNotifierError(f"Telegram network error: {_safe_reason(exc.reason)}") from exc
        except TimeoutError as exc:
            raise TelegramNotifierError("Telegram request timed out.") from exc
        except OSError as exc:
            raise TelegramNotifierError(f"Telegram request failed: {_safe_reason(exc)}") from exc

        _validate_telegram_response(raw_body)

    def _sanitize_message(self, message: str) -> str:
        text = str(message)
        if self.bot_token:
            text = text.replace(self.bot_token, "[REDACTED]")
        return text

    def _validate(self) -> None:
        if self.max_retries < 0:
            raise TelegramNotifierError("max_retries must be zero or greater.")

        if self.retry_delay_seconds < 0:
            raise TelegramNotifierError("retry_delay_seconds must be zero or greater.")

        if self.timeout_seconds <= 0:
            raise TelegramNotifierError("timeout_seconds must be greater than zero.")

        if not self.enabled:
            return

        if not self.bot_token or not str(self.bot_token).strip():
            raise TelegramNotifierError("Telegram bot token is required when notifications are enabled.")

        if not self.chat_ids:
            raise TelegramNotifierError("At least one Telegram chat ID is required when notifications are enabled.")


def _format_message(title: str, **fields: Any) -> str:
    lines = [title]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        label = key.replace("_", " ")
        lines.append(f"{label}: {value}")
    return "\n".join(lines)


def _resolve_setting(settings: Any | None, name: str, override: Any | None, default: Any) -> Any:
    if override is not None:
        return override
    if settings is None:
        return default
    return getattr(settings, name, default)


def _extract_telegram_error(error: urllib.error.HTTPError) -> str:
    try:
        body = error.read().decode("utf-8")
        parsed = json.loads(body)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return error.reason or "unknown error"

    description = parsed.get("description") if isinstance(parsed, dict) else None
    if isinstance(description, str) and description.strip():
        return description.strip()
    return error.reason or "unknown error"


def _validate_telegram_response(raw_body: str) -> None:
    try:
        parsed = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise TelegramNotifierError("Telegram API returned invalid JSON.") from exc

    if not isinstance(parsed, dict):
        raise TelegramNotifierError("Telegram API returned an unexpected response.")

    if parsed.get("ok") is not True:
        description = parsed.get("description")
        if not isinstance(description, str) or not description.strip():
            description = "unknown error"
        raise TelegramNotifierError(f"Telegram API rejected the message: {description}")


def _safe_reason(reason: Any) -> str:
    return str(reason).replace(TELEGRAM_API_BASE_URL, "[TELEGRAM_API_BASE_URL]")
