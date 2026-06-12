"""Interactive Brokers connection management."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ib_async import IB

from logging_setup.logger import get_logger


class IBConnectionError(RuntimeError):
    """Raised when an Interactive Brokers connection action fails."""


@dataclass(frozen=True)
class IBConnectionConfig:
    host: str
    port: int
    client_id: int
    account_id: str | None


class IBConnection:
    """Manage a single explicit connection to TWS or IB Gateway."""

    def __init__(self, settings: Any, ib_client: IB | None = None) -> None:
        self._config = _load_connection_config(settings)
        self._ib = ib_client if ib_client is not None else IB()
        self._logger = get_logger("ib_gateway.connection")

    @property
    def ib(self) -> IB:
        """Return the connected IB client, failing closed when disconnected."""

        self.require_connected()
        return self._ib

    @property
    def config(self) -> IBConnectionConfig:
        """Return the connection settings used by this connection object."""

        return self._config

    async def connect(self) -> IB:
        """Connect to IB explicitly and return the connected client."""

        if self.is_connected():
            self._logger.info(
                "IB already connected. host=%s port=%s client_id=%s",
                self._config.host,
                self._config.port,
                self._config.client_id,
            )
            return self._ib

        self._logger.info(
            "Connecting to IB. host=%s port=%s client_id=%s",
            self._config.host,
            self._config.port,
            self._config.client_id,
        )

        try:
            await self._ib.connectAsync(
                self._config.host,
                self._config.port,
                clientId=self._config.client_id,
                account=self._config.account_id or "",
            )
        except Exception as exc:
            self._logger.exception(
                "IB connection failed. host=%s port=%s client_id=%s",
                self._config.host,
                self._config.port,
                self._config.client_id,
            )
            raise IBConnectionError("Failed to connect to Interactive Brokers.") from exc

        if not self.is_connected():
            raise IBConnectionError("Interactive Brokers connection attempt completed but is disconnected.")

        self._logger.info("IB connected.")
        return self._ib

    def disconnect(self) -> None:
        """Disconnect safely if a connection is active."""

        if not self.is_connected():
            self._logger.info("IB disconnect skipped; already disconnected.")
            return

        self._logger.info("Disconnecting from IB.")
        try:
            self._ib.disconnect()
        except Exception as exc:
            self._logger.exception("IB disconnect failed.")
            raise IBConnectionError("Failed to disconnect from Interactive Brokers.") from exc

        self._logger.info("IB disconnected.")

    def is_connected(self) -> bool:
        """Return whether the IB client currently reports an active connection."""

        try:
            return bool(self._ib.isConnected())
        except Exception:
            self._logger.exception("IB connection status check failed.")
            return False

    async def reconnect(self) -> IB:
        """Disconnect if needed, then open a fresh IB connection."""

        self._logger.info("Reconnecting to IB.")
        if self.is_connected():
            self.disconnect()
        return await self.connect()

    def require_connected(self) -> None:
        """Raise when an IB action is attempted while disconnected."""

        if not self.is_connected():
            raise IBConnectionError("Interactive Brokers is disconnected.")


def _load_connection_config(settings: Any) -> IBConnectionConfig:
    ib_settings = getattr(settings, "ib", settings)
    host = getattr(ib_settings, "host", None)
    port = getattr(ib_settings, "port", None)
    client_id = getattr(ib_settings, "client_id", None)
    account_id = getattr(ib_settings, "account_id", None)

    if not isinstance(host, str) or not host.strip():
        raise IBConnectionError("IB host is required.")

    if isinstance(port, bool) or not isinstance(port, int):
        raise IBConnectionError("IB port must be an integer.")

    if isinstance(client_id, bool) or not isinstance(client_id, int):
        raise IBConnectionError("IB client_id must be an integer.")

    if account_id is not None and (not isinstance(account_id, str) or not account_id.strip()):
        raise IBConnectionError("IB account_id must be a non-empty string when provided.")

    return IBConnectionConfig(
        host=host.strip(),
        port=port,
        client_id=client_id,
        account_id=account_id.strip() if account_id is not None else None,
    )
