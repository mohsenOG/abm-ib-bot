"""Append-only CSV trade journal."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


JOURNAL_FIELDS = (
    "timestamp",
    "event_type",
    "signal_id",
    "side",
    "quantity",
    "price",
    "order_id",
    "perm_id",
    "status",
    "reason",
    "raw_json",
)

ALLOWED_EVENT_TYPES = {
    "signal",
    "risk_approved",
    "risk_blocked",
    "order_submitted",
    "order_partially_filled",
    "order_filled",
    "order_rejected",
    "order_cancelled",
    "order_inactive",
    "emergency_stop",
    "critical_error",
}

SENSITIVE_RAW_JSON_KEYS = {
    "account_id",
    "api_key",
    "authorization",
    "bot_token",
    "chat_id",
    "chat_ids",
    "password",
    "secret",
    "telegram_bot_token",
    "token",
}


class TradeJournalError(RuntimeError):
    """Raised when the trade journal cannot record an event safely."""


@dataclass(frozen=True)
class JournalEvent:
    event_type: str
    timestamp: str | None = None
    signal_id: str | None = None
    side: str | None = None
    quantity: float | int | str | None = None
    price: float | int | str | None = None
    order_id: int | str | None = None
    perm_id: int | str | None = None
    status: str | None = None
    reason: str | None = None
    raw_json: Any | None = None


class TradeJournal:
    """Write trading events to the configured CSV journal."""

    def __init__(self, journal_file: str | Path) -> None:
        self.journal_file = Path(journal_file)
        self._ensure_header()

    def append(self, event: JournalEvent) -> None:
        """Append one journal event to the CSV file."""

        if not isinstance(event, JournalEvent):
            raise TradeJournalError("event must be a JournalEvent.")

        row = _event_to_row(event)

        try:
            self.journal_file.parent.mkdir(parents=True, exist_ok=True)
            self._ensure_header()
            with self.journal_file.open("a", encoding="utf-8", newline="") as file_obj:
                writer = csv.DictWriter(file_obj, fieldnames=JOURNAL_FIELDS)
                writer.writerow(row)
        except OSError as exc:
            raise TradeJournalError(f"Could not append trade journal event: {self.journal_file}") from exc

    def record(
        self,
        event_type: str,
        *,
        timestamp: str | None = None,
        signal_id: str | None = None,
        side: str | None = None,
        quantity: float | int | str | None = None,
        price: float | int | str | None = None,
        order_id: int | str | None = None,
        perm_id: int | str | None = None,
        status: str | None = None,
        reason: str | None = None,
        raw_json: Any | None = None,
    ) -> None:
        """Create and append a journal event from keyword fields."""

        self.append(
            JournalEvent(
                timestamp=timestamp,
                event_type=event_type,
                signal_id=signal_id,
                side=side,
                quantity=quantity,
                price=price,
                order_id=order_id,
                perm_id=perm_id,
                status=status,
                reason=reason,
                raw_json=raw_json,
            )
        )

    def _ensure_header(self) -> None:
        if self.journal_file.exists() and self.journal_file.stat().st_size > 0:
            return

        try:
            self.journal_file.parent.mkdir(parents=True, exist_ok=True)
            with self.journal_file.open("w", encoding="utf-8", newline="") as file_obj:
                writer = csv.DictWriter(file_obj, fieldnames=JOURNAL_FIELDS)
                writer.writeheader()
        except OSError as exc:
            raise TradeJournalError(f"Could not initialize trade journal: {self.journal_file}") from exc


def _event_to_row(event: JournalEvent) -> dict[str, str]:
    _validate_event(event)

    return {
        "timestamp": event.timestamp or datetime.now(UTC).isoformat(),
        "event_type": event.event_type,
        "signal_id": _to_cell(event.signal_id),
        "side": _to_cell(event.side),
        "quantity": _to_cell(event.quantity),
        "price": _to_cell(event.price),
        "order_id": _to_cell(event.order_id),
        "perm_id": _to_cell(event.perm_id),
        "status": _to_cell(event.status),
        "reason": _to_cell(event.reason),
        "raw_json": _serialize_raw_json(event.raw_json),
    }


def _validate_event(event: JournalEvent) -> None:
    if event.event_type not in ALLOWED_EVENT_TYPES:
        allowed = ", ".join(sorted(ALLOWED_EVENT_TYPES))
        raise TradeJournalError(f"Invalid journal event_type '{event.event_type}'. Allowed: {allowed}.")

    if event.timestamp is not None and not str(event.timestamp).strip():
        raise TradeJournalError("timestamp must be non-empty when provided.")


def _to_cell(value: Any | None) -> str:
    if value is None:
        return ""
    return str(value)


def _serialize_raw_json(value: Any | None) -> str:
    if value is None:
        return ""

    redacted = _redact_sensitive_values(_decode_json_string(value) if isinstance(value, str) else value)

    try:
        return json.dumps(redacted, ensure_ascii=True, sort_keys=True, default=str)
    except (TypeError, ValueError) as exc:
        raise TradeJournalError("raw_json could not be serialized to JSON.") from exc


def _decode_json_string(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _redact_sensitive_values(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.lower() in SENSITIVE_RAW_JSON_KEYS:
                redacted[key_text] = "[REDACTED]"
            else:
                redacted[key_text] = _redact_sensitive_values(item)
        return redacted

    if isinstance(value, list):
        return [_redact_sensitive_values(item) for item in value]

    if isinstance(value, tuple):
        return [_redact_sensitive_values(item) for item in value]

    return value
