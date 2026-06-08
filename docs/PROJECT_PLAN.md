\# Interactive Brokers Gold 1H Trading Bot — Modular Project Plan



\## Project Goal



Build a modular automated trading bot for Interactive Brokers that trades only gold on the 1-hour timeframe.



The bot should:



```text

use closed 1-hour candles only

generate BUY/SELL signals from the existing 21-vote bias strategy

send Telegram notifications

start with alert-only mode

continue into paper trading

later move to controlled small real-money testing

be fully automated once execution mode is enabled

stay modular, maintainable, and understandable for a one-person hobby project

```



The bot is designed for eventual real-money testing, so risk control, duplicate-order prevention, state recovery, and clean logging are important.



\---



\# Project Structure



```text

gold\_ib\_bot/

│

├── requirements.txt

├── settings.yml

├── .env.example

├── docs/

├── src/

│

│   ├── main.py

│   ├── config/

│   ├── logging\_setup/

│   ├── notifications/

│   ├── ib\_gateway/

│   ├── data/

│   ├── strategy/

│   ├── risk/

│   ├── execution/

│   ├── state/

│   ├── monitoring/

│   └── trade\_journal/

```



Each folder is a Python module with one core responsibility.



Design rule:



```text

Each module should do one main job only.

```



\---



\# 1. `config/`



\## Core Feature



Load and validate all bot settings.



\## Suggested Files



```text

src/config/

├── \_\_init\_\_.py

└── settings.py

```

Root-level files:

```text

settings.yml       non-confidential bot settings

.env               confidential or account-specific values

.env.example       placeholder values for confidential or account-specific values

requirements.txt   Python dependencies

docs/              project documentation and task rules

src/               Python source code

```



\## Responsibilities



```text

load confidential environment variables

load non-confidential bot settings from root `settings.yml`

validate IB connection settings

validate Telegram secret settings

validate gold instrument settings

validate timeframe settings

validate strategy settings

validate risk settings

validate trading mode

validate the gold instrument

```



\## Important Settings



```text

Trading mode:

\- alert\_only

\- paper

\- live



Timeframe:

\- 1 hour only



Risk:

\- initial capital

\- capital slots

\- fixed capital per position

\- max concurrent position slots

\- max daily loss if enabled later



IB:

\- host

\- port

\- client ID

\- account ID from environment if needed



Telegram:

\- bot token from environment

\- chat IDs from environment



Config file split:

\- non-confidential global settings belong in root `settings.yml`

\- confidential or account-specific values belong in `.env`

\- `.env.example` must contain placeholders only

```



\## Must Not Do



```text

no IB connection

no strategy logic

no order logic

no Telegram sending

```



\---



\# 2. `logging\_setup/`



\## Core Feature



Configure bot logging.



\## Suggested Files



```text

logging\_setup/

├── \_\_init\_\_.py

└── logger.py

```



\## Responsibilities



```text

create console logger

create file logger

rotate log files

format logs clearly

log exceptions with tracebacks

prevent secrets from appearing in logs

provide reusable logger setup for the whole bot

```



\## Log Types



```text

startup logs

shutdown logs

IB connection logs

data logs

signal logs

risk logs

order logs

fill logs

state logs

monitoring logs

error logs

```



\## Must Not Do



```text

no Telegram sending

no trading decisions

no IB order logic

```



\---



\# 3. `notifications/`



\## Core Feature



Send Telegram notifications.



\## Suggested Files



```text

src/notifications/

├── \_\_init\_\_.py

└── notifier.py

```



\## Responsibilities



```text

send startup message

send shutdown message

send heartbeat message

send IB disconnect/reconnect message

send signal message

send risk-block message

send order-submitted message

send fill message

send rejection/cancellation message

send critical-error message

send emergency-stop message

retry failed Telegram sends safely

never expose secrets

```



\## Telegram Events



```text

Bot started

Bot stopped

IB connected

IB disconnected

New signal

Trade blocked

Order submitted

Order filled

Order rejected

Order cancelled

Position mismatch

Emergency stop activated

Critical error

Heartbeat

```



\## Must Not Do



```text

no strategy calculation

no risk approval

no order submission

no manual approval flow

```



\---



\# 4. `ib\_gateway/`



\## Core Feature



Handle all Interactive Brokers communication.



\## Suggested Files



```text

ib\_gateway/

├── \_\_init\_\_.py

├── connection.py

├── contracts.py

└── account.py

```



\## Responsibilities



```text

connect to TWS or IB Gateway using ib\_insync

disconnect cleanly

detect disconnects

reconnect safely

qualify the configured gold contract

fetch account values

fetch current positions

fetch open orders

fetch recent executions/fills

expose clean IB helper functions to the rest of the bot

```



\## Sub-Responsibilities



\### `connection.py`



```text

connect to IB

check connection status

reconnect

disconnect safely

block IB actions when disconnected

```



\### `contracts.py`



```text

build the gold contract from config

qualify the contract

validate exchange, currency, expiry, and contract type

refuse startup if the contract cannot be qualified

```



\### `account.py`



```text

read positions

read open orders

read executions

read account values

return clean account snapshots

```



\## Must Not Do



```text

no indicators

no signal generation

no position sizing

no order decision logic

```



\---



\# 5. `data/`



\## Core Feature



Fetch and maintain clean 1-hour gold candle data.



\## Suggested Files



```text

src/data/

├── \_\_init\_\_.py

├── market\_data.py

└── candle\_store.py

```



\## Responsibilities



```text

request 1-hour gold candles from IB

use only closed candles

normalize IB bars into OHLCV format

store candle history in memory

deduplicate candles by timestamp

sort candles chronologically

detect missing candles

backfill gaps when needed

track latest processed candle

return clean candle DataFrame to the strategy module

```



\## Important Rules



```text

only closed candles are valid

incomplete live candles must not generate signals

candle timestamps must be consistent

missing candles must be detected

duplicate candles must be removed

```



\## Must Not Do



```text

no strategy voting

no BUY/SELL decision

no risk decision

no order logic

```



\---



\# 6. `strategy/`



\## Core Feature



Convert candles into BUY/SELL signals.



\## Suggested Files



```text

src/strategy/

├── \_\_init\_\_.py

├── indicators.py

├── bias\_model.py

└── signals.py

```



\## Responsibilities



```text

calculate Heikin Ashi candles if enabled

calculate EMA

calculate SMA

calculate RSI

calculate MACD

calculate Bollinger Bands

calculate ROC

calculate ATR

calculate breakout levels

calculate the 21-vote bias model

calculate normalized bias

calculate confidence

detect BUY/SELL threshold crossovers

return signal object or no signal

```



\## Strategy Logic



The strategy uses a 21-vote model:



```text

EMA trend votes

SMA trend votes

RSI vote

EMA fundamentalist votes

MACD votes

Bollinger Band vote

Breakout votes

ROC votes

```



The final values are:



```text

total\_votes

bias = total\_votes / 21

confidence = abs(total\_votes) / 21

```



Signal logic:



```text

BUY signal:

previous bias <= positive threshold

current bias > positive threshold



SELL signal:

previous bias >= negative threshold

current bias < negative threshold

```



\## Important Fix



Breakout logic must compare the current close against previous rolling highs/lows.



Correct concept:



```text

current close > previous N-candle high

current close < previous N-candle low

```



Incorrect concept:



```text

current close > rolling high that includes the current candle

```



\## Must Not Do



```text

no IB calls

no account checks

no position sizing

no order submission

```



\---



\# 7. `risk/`



\## Core Feature



Approve or block trades using simple fixed-capital slot sizing.



\## Suggested Files



```text

src/risk/

├── \_\_init\_\_.py

├── risk\_manager.py

└── sizing.py

```



\## Responsibilities



```text

check trading mode

check current position

check open orders

block duplicate trades

use simple capital slot sizing

calculate order quantity from fixed capital per position

optionally calculate ATR-based stop loss

optionally calculate take profit

return approved trade plan or blocked reason

```



\## Starting Risk Model



Use:



```text

initial\_capital = X

capital\_slots = a

capital\_per\_position = X / a

```



Example:



```text

initial\_capital = 1000

capital\_slots = 10

capital\_per\_position = 100

```



The exact values must come from config.



\## Output



The risk module returns either:



```text

Approved TradePlan

```



or:



```text

Blocked trade with reason

```



\## Must Not Do



```text

no IB order submission

no order status tracking

no Telegram formatting

no strategy calculation

```



\---



\# 8. `execution/`



\## Core Feature



Build, submit, and track orders.



\## Suggested Files



```text

src/execution/

├── \_\_init\_\_.py

├── order\_builder.py

└── order\_manager.py

```



\## Responsibilities



```text

convert approved trade plan into IB orders

build market orders

build limit orders if configured

build stop-loss orders if enabled

build take-profit orders if enabled

submit orders through IB

track order statuses

track fills

track partial fills

detect rejected orders

detect cancelled orders

detect inactive orders

prevent duplicate order submission

update state after order events

send Telegram order lifecycle alerts

```



\## Order Statuses To Handle



```text

PendingSubmit

Submitted

PreSubmitted

Filled

PartiallyFilled

Cancelled

Inactive

Rejected

```



\## Detailed Execution Handling



Execution details will be designed at implementation time.



That phase should define:



```text

partial fill behavior

restart during active order

orphan order detection

protective order behavior

permId/orderId recovery

order timeout behavior

cancel behavior

position reconciliation

```



\## Must Not Do



```text

no signal generation

no indicator calculation

no risk approval

no position sizing decision

```



\---



\# 9. `state/`



\## Core Feature



Persist bot state across restarts using atomic writes.



\## Suggested Files



```text

src/state/

├── \_\_init\_\_.py

└── state\_store.py

```



\## Responsibilities



```text

save last processed candle timestamp

save last signal ID

save active trade state

save known IB order IDs

save known IB permIds if available

save daily risk counters if enabled

save emergency-stop flag

load state on startup

prevent duplicate signal processing after restart

prevent duplicate order submission after restart

use atomic write behavior

```



\## Starting Storage



Use an atomic state file.



Recommended files:



```text

bot\_state.json

bot\_state.json.tmp

```



Write flow:



```text

1\. write new state to temporary file

2\. flush file

3\. fsync file

4\. atomically replace old state with new state

```



\## Must Not Do



```text

no calculations

no IB calls

no Telegram alerts

no trading decisions

```



\---



\# 10. `monitoring/`



\## Core Feature



Health checks and emergency stop.



\## Suggested Files



```text

src/monitoring/

├── \_\_init\_\_.py

├── health.py

└── emergency\_stop.py

```



\## Responsibilities



```text

check IB connection health

check market data freshness

check Telegram availability

check last processed candle age

send periodic heartbeat

detect repeated errors

detect stalled bot loop

activate emergency stop after critical failure

block new trades when emergency stop is active

persist emergency-stop state

alert Telegram when emergency stop is activated

```



\## First Version Emergency Stop Behavior



At first, emergency stop should:



```text

block new trades

save emergency flag

send Telegram alert

```



More complex behavior will be defined during implementation when exact execution behavior is known.



Future decisions:



```text

what to do if position is open and IB disconnects

what to do if market data becomes stale

what to do if stop order is missing

whether to cancel open orders

whether to flatten position

```



Do not auto-flatten positions in the first version unless explicitly designed later.



\## Must Not Do



```text

no strategy logic

no position sizing

no normal order submission

no auto-flattening unless explicitly enabled later

```



\---



\# 11. `trade\_journal/`



\## Core Feature



Record all trading activity for review.



\## Suggested Files



```text

src/trade\_journal/

├── \_\_init\_\_.py

└── journal.py

```



\## Responsibilities



```text

record every signal

record every risk decision

record every blocked trade

record every approved trade plan

record every submitted order

record every fill

record every rejection/cancellation

record planned entry, actual entry, planned exit, actual exit

record quantity, price, signal ID, order ID, and timestamp

store data in CSV or JSONL format

keep a clean review history separate from normal logs

```



\## Starting Storage



Use:



```text

trade\_journal.csv

```



or:



```text

trade\_journal.jsonl

```



Recommended first version:



```text

trade\_journal.csv

```



\## Must Not Do



```text

no strategy calculation

no risk approval

no order submission

no IB calls

```



\---



\# `src/main.py`



\## Core Feature



Orchestrate the bot.



\## Responsibilities



```text

load config

start logging

start Telegram notifier

connect to IB

qualify the gold contract

load state

warm up candle history

run the closed-candle loop

call modules in correct order

save state

write trade journal events

handle shutdown

catch critical exceptions

trigger emergency stop when needed

```



\## Main Loop Concept



```text

1\. Fetch latest 1h gold candles.

2\. Update candle store.

3\. Check if there is a new closed candle.

4\. If no new candle, sleep.

5\. Calculate indicators.

6\. Calculate bias.

7\. Generate signal.

8\. If no signal, save state and continue.

9\. Check if signal was already processed.

10\. Read IB account, position, and open orders.

11\. Run risk check.

12\. Journal the risk decision.

13\. If blocked, send Telegram alert and save state.

14\. If approved, build order.

15\. Submit/manage order.

16\. Journal order events.

17\. Update state with atomic write.

18\. Send Telegram updates.

19\. Run health checks.

20\. Sleep until next cycle.

```



\---



\# Final Module Map



```text

config              settings only

logging\_setup       logs only

notifications       Telegram alerts only

ib\_gateway          IB connection, contracts, account only

data                candle fetching and candle storage only

strategy            indicators, bias, and signals only

risk                trade approval and sizing only

execution           orders and fills only

state               persistent bot memory with atomic writes only

monitoring          health checks and emergency stop only

trade\_journal       trade history only

src/main.py         orchestration only

```



\---



\# Development Phases



\## Phase 1 — Skeleton + IB Data



Build:



```text

config

logging\_setup

notifications

ib\_gateway

data

state

src/main.py

```



Goal:



```text

Start bot.

Connect to IB.

Qualify the selected gold instrument placeholder.

Fetch 1h gold candles.

Use only closed candles.

Send Telegram startup message.

Log the latest closed candle.

Save basic state with atomic writes.

No strategy.

No orders.

```



\## Phase 2 — Strategy Alerts



Build:



```text

strategy

trade\_journal

```



Goal:



```text

Calculate indicators.

Calculate the 21-vote bias model.

Generate BUY/SELL signals.

Send Telegram signal alerts.

Avoid duplicate candle processing.

Avoid duplicate signals after restart.

Journal all signals.

No orders.

```



\## Phase 3 — Simple Risk Engine



Build:



```text

risk

```



Goal:



```text

Use initial capital X.

Use capital slots a.

Calculate capital\_per\_position = X / a.

Block trades when no slots are available.

Calculate simple quantity.

Return approved TradePlan or blocked reason.

Journal risk decisions.

Still no live orders.

```



\## Phase 4 — Paper Trading Execution



Build:



```text

execution

```



Goal:



```text

Read IB position and open orders.

Approve or block trades.

Submit paper orders only.

Track order status.

Track fills and rejections.

Send Telegram order updates.

Journal order lifecycle.

Start handling detailed execution behavior.

```



\## Phase 5 — Safety Layer



Build:



```text

monitoring

```



Goal:



```text

Heartbeat messages.

IB disconnect detection.

Market data freshness checks.

Repeated error detection.

Emergency stop.

Restart recovery.

New-trade blocking after critical errors.

Clean internet disruption handling.

Correct behavior after disconnections and reconnections.

```



\## Phase 6 — Controlled Live Test



Only after paper trading works.



Goal:



```text

Enable live mode.

Trade very small size.

Use fixed capital slots.

Keep strict exposure limits.

Keep Telegram alerts for every important event.

Keep emergency stop active.

Review logs and trade journal after every session.

```



\---



\# First Real-Money Constraints



For a real-money test around 1000€, the first live version should use:



```text

gold only

1h timeframe only

closed candles only

fixed capital per position

capital\_per\_position = initial\_capital / capital\_slots

example: 1000 / 10 = 100 per position slot

maximum open slots controlled by config

Telegram alerts for every important event

paper trading before live trading

fully automated execution after paper validation

```



The first goal is not maximum profit.



The first goal is:



```text

no duplicate orders

no missing exits

no stale-data trades

no uncontrolled position size

no trading while disconnected

no repeated entries from the same signal

clear Telegram alerts

clean restart behavior

clean internet disruption handling

correct behavior after disconnections and reconnections

complete trade journal

```



\---



\# Design Principle



The bot should be modular, but not enterprise-heavy.



Each module has one job:



```text

config loads settings

logging\_setup writes logs

notifications sends Telegram alerts

ib\_gateway talks to IB

data prepares candles

strategy creates signals

risk approves or blocks trades

execution submits and tracks orders

state remembers bot state safely

monitoring checks safety

trade\_journal records trading activity

main coordinates everything

```



This keeps the project easy to develop, easy to understand, fully automated, and safe enough to grow from alert-only mode into paper trading and then controlled small live testing.



