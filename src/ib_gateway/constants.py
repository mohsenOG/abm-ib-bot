"""Interactive Brokers protocol and supported instrument constants."""

from __future__ import annotations


IB_UNSET_PRICE = 1e100
LIVE_MARKET_DATA_TYPE = 1
NON_LIVE_MARKET_DATA_TYPES = frozenset({2, 3, 4})

SIGNAL_ASSET_CLASS_CMDTY = "CMDTY"
SIGNAL_SYMBOL_XAUUSD = "XAUUSD"
SMART_EXCHANGE = "SMART"
USD_CURRENCY = "USD"
EXECUTION_SEC_TYPE_IOPT = "IOPT"
EUR_CURRENCY = "EUR"
