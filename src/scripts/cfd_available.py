from ib_async import *

ib = IB()
ib.connect("127.0.0.1", 7497, clientId=500)

c = Contract(
    symbol="XAUUSD",
    secType="CMDTY",
    exchange="SMART",
    currency="USD",
)

details = ib.reqContractDetails(c)

print("DETAIL COUNT:", len(details))

if details:
    d = details[0]

    print("\nCONTRACT:")
    print(d.contract)

    ticker = ib.reqMktData(d.contract)

    ib.sleep(5)

    print("\nMARKET DATA:\n")
    print("\nMARKET DATA:")
    for key, value in ticker.__dict__.items():
        print(f"{key}: {value}")

ib.disconnect()