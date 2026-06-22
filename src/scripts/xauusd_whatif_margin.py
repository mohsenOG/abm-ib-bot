#!/usr/bin/env python3

import os
from pprint import pprint

from ib_async import IB, Contract, LimitOrder


HOST = os.getenv("IB_HOST", "127.0.0.1")
PORT = int(os.getenv("IB_PORT", "7497"))
CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "300"))
ACCOUNT_ID = os.getenv("IB_ACCOUNT_ID")

QTY = float(os.getenv("TEST_QTY", "1"))


def banner(text: str):
    print("\n" + "=" * 120)
    print(text)
    print("=" * 120)


def main():
    ib = IB()
    ib.RequestTimeout = 20

    ib.connect(
        HOST,
        PORT,
        clientId=CLIENT_ID,
    )

    try:
        banner("CONTRACT")

        contract = Contract(
            symbol="XAUUSD",
            secType="CMDTY",
            exchange="SMART",
            currency="USD",
        )

        details = ib.reqContractDetails(contract)

        if not details:
            raise RuntimeError("No contract details")

        contract = details[0].contract

        pprint(contract)

        banner("MARKET DATA")

        ticker = ib.reqMktData(contract, "", False, False)

        ib.sleep(3)

        bid = ticker.bid
        ask = ticker.ask

        print("bid =", bid)
        print("ask =", ask)

        ib.cancelMktData(contract)

        tests = [
            ("BUY", ask),
            ("SELL", bid),
        ]

        for action, px in tests:
            banner(f"WHATIF {action}")

            order = LimitOrder(
                action=action,
                totalQuantity=QTY,
                lmtPrice=px,
            )

            order.whatIf = True
            order.tif = "GTC"

            if ACCOUNT_ID:
                order.account = ACCOUNT_ID

            print("ORDER:")
            pprint(order)

            # what-if order
            state = ib.whatIfOrder(contract, order)

            print("\nORDER STATE TYPE:")
            print(type(state))

            print("\nRAW ORDER STATE:")
            pprint(state)

            print("\nATTRIBUTES:")

            for attr in [
                "status",
                "warningText",
                "initMarginBefore",
                "initMarginChange",
                "initMarginAfter",
                "maintMarginBefore",
                "maintMarginChange",
                "maintMarginAfter",
                "equityWithLoanBefore",
                "equityWithLoanChange",
                "equityWithLoanAfter",
                "commission",
                "minCommission",
                "maxCommission",
            ]:
                if hasattr(state, attr):
                    print(f"{attr}: {getattr(state, attr)}")

    finally:
        banner("DISCONNECT")
        ib.disconnect()


if __name__ == "__main__":
    main()