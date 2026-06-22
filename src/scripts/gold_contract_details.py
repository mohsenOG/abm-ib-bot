#!/usr/bin/env python3

import math
import os
from pprint import pprint

from ib_async import IB, Contract


HOST = os.getenv("IB_HOST", "127.0.0.1")
PORT = int(os.getenv("IB_PORT", "7497"))  # 7497=paper, 7496=live
CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "100"))


def banner(text: str) -> None:
    print("\n" + "=" * 120)
    print(f"### {text}")
    print("=" * 120)


def is_valid_number(value) -> bool:
    try:
        return value is not None and not math.isnan(float(value))
    except Exception:
        return False


def print_market_data(ib: IB, contract: Contract, wait_seconds: int = 5) -> None:
    banner("MARKET DATA")

    ticker = ib.reqMktData(contract, "", False, False)
    ib.sleep(wait_seconds)

    print(f"bid      : {ticker.bid}")
    print(f"bidSize  : {ticker.bidSize}")
    print(f"ask      : {ticker.ask}")
    print(f"askSize  : {ticker.askSize}")
    print(f"last     : {ticker.last}")
    print(f"lastSize : {ticker.lastSize}")
    print(f"close    : {ticker.close}")
    print(f"volume   : {ticker.volume}")

    has_bid = is_valid_number(ticker.bid)
    has_ask = is_valid_number(ticker.ask)

    print()
    print(f"HAS_BID  : {has_bid}")
    print(f"HAS_ASK  : {has_ask}")

    if has_bid and has_ask:
        spread = ticker.ask - ticker.bid
        mid = (ticker.ask + ticker.bid) / 2
        spread_pct = spread / mid * 100 if mid else None

        print(f"spread   : {spread}")
        print(f"mid      : {mid}")
        print(f"spread % : {spread_pct:.5f}%")

    ib.cancelMktData(contract)


def inspect_xauusd_cmdty(ib: IB) -> Contract | None:
    banner("REQUEST CONTRACT DETAILS: XAUUSD CMDTY")

    contract = Contract(
        symbol="XAUUSD",
        secType="CMDTY",
        exchange="SMART",
        currency="USD",
    )

    print("REQUEST:")
    pprint(contract)

    details = ib.reqContractDetails(contract)

    print()
    print(f"DETAIL COUNT: {len(details)}")

    if not details:
        print("No contract details found.")
        return None

    detail = details[0]
    resolved_contract = detail.contract

    banner("RESOLVED CONTRACT")
    print(resolved_contract)

    banner("ORDER TYPES")
    print(detail.orderTypes)

    banner("IMPORTANT FIELDS")
    print(f"conId          : {resolved_contract.conId}")
    print(f"symbol         : {resolved_contract.symbol}")
    print(f"localSymbol    : {resolved_contract.localSymbol}")
    print(f"secType        : {resolved_contract.secType}")
    print(f"exchange       : {resolved_contract.exchange}")
    print(f"currency       : {resolved_contract.currency}")
    print(f"tradingClass   : {resolved_contract.tradingClass}")
    print(f"longName       : {detail.longName}")
    print(f"marketName     : {detail.marketName}")
    print(f"validExchanges : {detail.validExchanges}")
    print(f"minTick        : {detail.minTick}")
    print(f"minSize        : {detail.minSize}")
    print(f"sizeIncrement  : {detail.sizeIncrement}")

    banner("FULL CONTRACT DETAILS")
    pprint(detail)

    return resolved_contract


def main() -> int:
    banner("CONNECTING")

    ib = IB()
    ib.RequestTimeout = 10

    ib.connect(
        HOST,
        PORT,
        clientId=CLIENT_ID,
    )

    # 1 = live
    # 2 = frozen
    # 3 = delayed
    # 4 = delayed frozen
    ib.reqMarketDataType(1)

    try:
        contract = inspect_xauusd_cmdty(ib)

        if contract is not None:
            print_market_data(ib, contract)

        return 0

    finally:
        banner("DISCONNECTING")
        ib.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())