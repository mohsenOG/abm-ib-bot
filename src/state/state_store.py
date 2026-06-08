"""Persistent bot state with atomic JSON writes."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class StateStoreError(RuntimeError):
    """Raised when persistent state cannot be loaded or saved safely."""


@dataclass
class BotState:
    last_processed_candle_ts: str | None = None
    last_signal_id: str | None = None
    active_trade: dict[str, Any] = field(default_factory=dict)
    known_order_ids: list[int] = field(default_factory=list)
    known_perm_ids: list[int] = field(default_factory=list)
    daily_risk: dict[str, Any] = field(default_factory=dict)
    emergency_stop: bool = False
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> BotState:
        return cls(
            last_processed_candle_ts=_optional_string(raw, "last_processed_candle_ts"),
            last_signal_id=_optional_string(raw, "last_signal_id"),
            active_trade=_dict_field(raw, "active_trade"),
            known_order_ids=_int_list_field(raw, "known_order_ids"),
            known_perm_ids=_int_list_field(raw, "known_perm_ids"),
            daily_risk=_dict_field(raw, "daily_risk"),
            emergency_stop=_bool_field(raw, "emergency_stop"),
            updated_at=_required_string(raw, "updated_at"),
        )


class StateStore:
    """Load and save bot state from a JSON file."""

    def __init__(self, state_file: str | Path) -> None:
        self.state_file = Path(state_file)
        self.tmp_file = self.state_file.with_name(f"{self.state_file.name}.tmp")

    def load(self) -> BotState:
        if not self.state_file.exists():
            return BotState()

        try:
            with self.state_file.open("r", encoding="utf-8") as file_obj:
                raw = json.load(file_obj)
        except json.JSONDecodeError as exc:
            raise StateStoreError(f"State file is corrupt JSON: {self.state_file}") from exc
        except OSError as exc:
            raise StateStoreError(f"Could not read state file: {self.state_file}") from exc

        if not isinstance(raw, dict):
            raise StateStoreError(f"State file must contain a JSON object: {self.state_file}")

        try:
            return BotState.from_dict(raw)
        except (TypeError, ValueError) as exc:
            raise StateStoreError(f"State file has invalid fields: {self.state_file}") from exc

    def save(self, state: BotState) -> None:
        state.updated_at = datetime.now(UTC).isoformat()
        payload = state.to_dict()

        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with self.tmp_file.open("w", encoding="utf-8") as file_obj:
                json.dump(payload, file_obj, indent=2, sort_keys=True)
                file_obj.write("\n")
                file_obj.flush()
                os.fsync(file_obj.fileno())

            self.tmp_file.replace(self.state_file)
            _fsync_parent_directory(self.state_file.parent)
        except OSError as exc:
            raise StateStoreError(f"Could not save state file atomically: {self.state_file}") from exc


def _optional_string(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string or null.")
    return value


def _required_string(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string.")
    return value


def _dict_field(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object.")
    return value


def _int_list_field(raw: dict[str, Any], key: str) -> list[int]:
    value = raw.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list.")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
        raise ValueError(f"{key} must contain integers only.")
    return value


def _bool_field(raw: dict[str, Any], key: str) -> bool:
    value = raw.get(key, False)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean.")
    return value


def _fsync_parent_directory(directory: Path) -> None:
    if os.name == "nt":
        return

    flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
    try:
        dir_fd = os.open(directory, flags)
    except OSError:
        return

    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
